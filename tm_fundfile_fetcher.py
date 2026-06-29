"""tm_fundfile_fetcher — Arc B patch 3 (v4.14.5.10): fundamentals +
filings incremental.

PURE SCHEDULING/SCOPE LAYER, same shape as v4.14.5.8's news work.
It does NOT fetch/parse/cache itself — it calls the existing per-ticker
router chokepoint `tm_data_router.get_router().fetch(data_type,
ticker=T)`. The finnhub/yahoo fundamentals adapters self-tap
`_v415_cache_write_fundamentals` (writes the fundamentals table +
stamps cache_metadata('fundamentals')); the EDGAR filings adapter
self-taps `_v415_cache_write_filings` (filings table, PK
accession_number = INSERT-OR-REPLACE idempotent dedup + stamps
cache_metadata('filings')). So repeated calls are correct (zero dup
rows) and freshness is tracked — nothing new to invent.

No-key safety is INHERITED: the router falls through its source
priority, EDGAR is keyless, and fundamentals fall finnhub→yahoo. This
module never gates on a key itself.

Adapter-hardening principle (IDEAS.md, v4.14.5.9): filings refresh
checks the EDGAR CIK-map backoff state and SKIPS the pass (logging
once) rather than hammering SEC or exception-storming — no
fail-closed-and-sticky behaviour introduced.

Deferred (matches v4.14.5.6/.8): per-call window-narrowing. The
adapters use their own fixed windows; the (PK/accession) dedup +
freshness-skip already make repeat calls correct and cheap.
"""

from __future__ import annotations

import threading
import time

FUNDAMENTALS_REFRESH_INTERVAL_SECONDS = 1800   # 30 min
FILINGS_REFRESH_INTERVAL_SECONDS = 1800        # 30 min base tick
FILINGS_TIER_CADENCE = {1: 1, 2: 4, 3: 12}     # ticks between runs
FILINGS_TIER3_ROTATION_SLICES = 55
FUND_STALE_DAYS = 90                           # quarterly cadence (active filers)
FUND_STALENESS_PER_CYCLE = 50                  # cold-rotation budget
# v4.14.6.111-age-aware-fund-ttl: the 90d backstop re-pulls a ticker's
# fundamentals once its last fetch ages past FUND_STALE_DAYS. For a name whose
# NEWEST filing is already ancient (foreign/infrequent filers — Gold Fields,
# Daxor: newest filing from 2011–2020), that means re-pulling the SAME stale
# data every quarter forever, only to re-flag it ">18mo stale, not used as
# current". These tiers stretch the recheck interval the OLDER the newest
# filing is — never a permanent skip (so a new filing is still picked up within
# a tier interval, and the EDGAR newly-filed index re-pulls immediately when
# they DO file). Boundaries align with the existing 18-month stale flag
# (_FUNDAMENTALS_MAX_AGE_DAYS=548) and the 5-year fundamentals retention.
# Self-correcting: the tier is derived from the CURRENT newest-filing age each
# cycle — when a stale name files, it drops back to the 90d tier automatically.
FUND_STALE_FILING_DAYS = 548                    # ~18mo: the stale-flag boundary
FUND_ANCIENT_FILING_DAYS = 1825                # ~5y: ancient (rarely-filing)
FUND_BACKSTOP_DAYS_STALE = 180                 # newest filing 18mo–5y → 6-monthly
FUND_BACKSTOP_DAYS_ANCIENT = 365              # newest filing >5y → yearly
# v4.14.5.14-fundamentals-empty-cache: once every source confirms "no
# fundamentals for this ticker" (router status 'empty'), cache that and skip
# the ticker for this long instead of re-asking every 30-min cycle. Longer
# than the 24h earnings TTL because fundamentals change quarterly; a quarterly
# earnings trigger re-checks sooner (earnings-triggered tickers bypass this).
FUND_EMPTY_TTL_DAYS = 7
FILINGS_STALE_SECONDS = {1: 6 * 3600, 2: 24 * 3600, 3: 72 * 3600}

# v4.14.6.78-insider-factor (Option B): max board/held tickers to compute
# insider-flow for per filings-universe cycle. Small so the on-demand
# per-Form-4 EDGAR fetches stay a trickle (dozens/day, never the universe);
# the rest are picked up on subsequent cycles, and the 24h recency/attempt
# gates keep steady-state load near zero.
INSIDER_FLOW_MAX_PER_CYCLE = 8

# v4.14.5.14-cascade-fixes (Fix 2): pace the per-ticker fundamentals/earnings
# seed loops and stop a cycle once providers are clearly exhausted, so a
# rate-limited/cooled provider can't drive a 40+ line "no eligible source"
# burst. Mirrors tm_news_fetcher's throttle (55/min, buffer under Finnhub's
# 60/min). The break is per-CYCLE only — counters are local, so the next
# 30-min tick retries from scratch (recovered providers get another chance).
FUNDFILE_RATE_LIMIT_PER_MIN = 55               # buffer under Finnhub's 60/min
FUNDFILE_EXHAUSTION_BREAK = 3                  # consecutive no-source/failed → pause cycle

# v4.14.6.111: Yahoo EARNINGS inter-call interval (seconds). This is the mutable
# attribute the adaptive lane controller (tm_lane_pacing, lane 'yahoo_earnings')
# tunes — seeded 3.0s and adjusted from observed 429/success outcomes. The
# earnings seed loop honors THIS instead of the flat Finnhub-calibrated 55/min,
# so earnings is paced to Yahoo's real .calendar ceiling. Fundamentals/filings
# loops keep _make_pacer (FUNDFILE_RATE_LIMIT_PER_MIN) unchanged.
_EARNINGS_MIN_INTERVAL_SEC = 3.0


def _make_pacer():
    """Return a throttle() closure pacing calls to <= FUNDFILE_RATE_LIMIT_PER_MIN.
    throttle() sleeps just enough to keep the rate under the cap and returns
    True when it actually slept (for the 'throttled' counter)."""
    min_interval = 60.0 / max(1, FUNDFILE_RATE_LIMIT_PER_MIN)
    last = [0.0]

    def throttle():
        now = time.time()
        gap = now - last[0]
        slept = False
        if gap < min_interval:
            time.sleep(min_interval - gap)
            slept = True
        last[0] = time.time()
        return slept
    return throttle


def _fetch_status(router, data_type, **kw):
    """Call router.fetch(..., return_status=True), tolerating a mock / older
    router that returns just a payload. Returns (payload, status) where status
    is one of 'ok' / 'empty' / 'failed' / 'no_source'."""
    out = router.fetch(data_type, return_status=True, **kw)
    if isinstance(out, tuple) and len(out) == 2:
        return out[0], out[1]
    # Bare payload (mock / legacy): treat presence as 'ok', absence as 'empty'.
    return out, ('ok' if out is not None else 'empty')

_fund_nokey_warned = False
_filings_backoff_warned = False

# v4.14.5.11: universe-list weekly maintenance. cache.db.tickers only
# grew on an explicit fill before; new ITOT constituents never got
# added. _universe_last_refresh_at is per-session; on the very first
# tick it's seeded to "now" WITHOUT fetching (startup fill already
# covered the universe) so it fires ~weekly only in long sessions.
UNIVERSE_REFRESH_INTERVAL_HOURS = 168  # 7 days
_universe_last_refresh_at = None
_universe_src_warned = False


def _count_tickers() -> int:
    try:
        import tm_cache
        conn = tm_cache.get_connection()
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM tickers").fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return -1


def refresh_universe_list(app, force: bool = False) -> dict:
    """Re-fetch the configured universe source and UNION any new
    tickers into cache.db.tickers (never removes — preserves history).
    Pure reuse of tm_fill_executor._seed_universe_if_needed, which
    already fetches + dedup-upserts + handles source-unreachable
    (amber log + return). Never raises."""
    global _universe_src_warned
    s = {'tickers_before': -1, 'tickers_added': 0,
         'tickers_total': -1, 'source': '', 'errors': []}
    try:
        import tired_market as _tm
        import tm_fill_executor as _fx
        uid = (getattr(app, 'cfg', {}) or {}).get(
            'v415_universe', 'sp500')
        src = _tm._v415_universe_source_for(uid)
        s['source'] = src
    except Exception as e:
        s['errors'].append(f'resolve: {type(e).__name__}')
        return s

    before = _count_tickers()
    s['tickers_before'] = before

    def _cb(msg, tag='muted'):
        try:
            app.root.after(0, lambda: app._log(msg, tag))
        except Exception:
            pass

    try:
        _fx._seed_universe_if_needed(_cb, None, src)
    except Exception as e:
        s['errors'].append(f'seed: {type(e).__name__}')
        return s

    after = _count_tickers()
    s['tickers_total'] = after
    if before >= 0 and after >= 0:
        s['tickers_added'] = max(0, after - before)
    return s


def _maybe_refresh_universe(app) -> bool:
    """Weekly cadence wrapper. First call this session seeds the
    timestamp WITHOUT fetching (startup fill already populated the
    universe) so it only fires after a full week of continuous
    running. Flag-gated. Never raises."""
    global _universe_last_refresh_at
    try:
        if not bool((getattr(app, 'cfg', {}) or {}).get(
                'use_universe_maintenance', True)):
            return False
    except Exception:
        pass
    now = time.time()
    if _universe_last_refresh_at is None:
        _universe_last_refresh_at = now  # seed; don't refetch
        return False
    if (now - _universe_last_refresh_at) < (
            UNIVERSE_REFRESH_INTERVAL_HOURS * 3600):
        return False
    _universe_last_refresh_at = now
    try:
        s = refresh_universe_list(app)
        added = s.get('tickers_added', 0)
        tot = s.get('tickers_total', -1)
        src = s.get('source', '?')
        if s.get('errors'):
            app.root.after(0, lambda: app._log(
                "[universe] weekly refresh: source unreachable, "
                "universe unchanged", 'muted'))
        elif added > 0:
            app.root.after(0, lambda: app._log(
                f"[universe] weekly refresh: source={src}, "
                f"+{added} tickers (total {tot})", 'muted'))
        else:
            app.root.after(0, lambda: app._log(
                f"[universe] weekly refresh: source={src}, "
                f"no new tickers", 'muted'))
        return True
    except Exception:
        return False


def _router():
    try:
        import tm_data_router
        return tm_data_router.get_router()
    except Exception:
        return None


def _last_refresh_epoch(ticker: str, lane: str):
    try:
        import tm_cache
        from datetime import datetime as _dt
        rows = tm_cache.get_cache_metadata(ticker, lane) or []
        if not rows:
            return None
        lr = tm_cache._row_get(rows[0], 'last_refresh_at')
        if not lr:
            return None
        return _dt.fromisoformat(str(lr).replace('Z', '')).timestamp()
    except Exception:
        return None


# v4.14.6.43-fundamentals-cache-fix: filing-aware staleness helpers.
# Without these, the daemon (refresh_fundamentals_universe, below) would
# only consult last_refresh_at vs FUND_STALE_DAYS (90) — every ticker
# whose row was written >90 days ago is re-fetched even if the cached
# fiscal_period_end ALREADY covers the latest filed quarter. Combined
# with PART A (tm_cache.get_unfilled_tickers now accepts source='edgar'),
# the practical effect is: once a ticker has an EDGAR-sourced row
# covering, say, 2025-12-31 (the latest filed period), the daemon skips
# it until the next quarter's expected filing window. Backstop
# (FUND_STALE_DAYS=90) is preserved for rows that lack have_to_period
# (pre-patch rows + any future source that doesn't stamp the metadata
# column) so coverage of pre-existing cache rows still rotates after 90
# days. Net: no freeze, no per-cycle re-pull spam.
_FUND_CURRENT_QUARTER_GRACE_DAYS = 95


def _have_to_period(ticker: str):
    """Return cached MAX(fiscal_period_end) for fundamentals (or None).

    Reads cache_metadata.have_to_period — written by
    tm_cache.upsert_fundamentals (single chokepoint every yahoo/finnhub/
    EDGAR/deep adapter funnels through). Best-effort: any error → None →
    caller falls back to the last_refresh_at backstop, so a metadata read
    fault never freezes the staleness rotation."""
    try:
        import tm_cache
        rows = tm_cache.get_cache_metadata(ticker, 'fundamentals') or []
        if not rows:
            return None
        v = tm_cache._row_get(rows[0], 'have_to_period')
        return str(v)[:10] if v else None
    except Exception:
        return None


def _last_attempted_filing(ticker: str):
    """v4.14.6.111: return cache_metadata.last_attempted_filing for fundamentals
    (the most-recent EDGAR filing_date we've already TRIED to ingest), or None.
    Best-effort — any error → None → the index gate falls back to have_to_period
    alone (its prior behaviour), so a read fault never freezes the rotation."""
    try:
        import tm_cache
        rows = tm_cache.get_cache_metadata(ticker, 'fundamentals') or []
        if not rows:
            return None
        v = tm_cache._row_get(rows[0], 'last_attempted_filing')
        return str(v)[:10] if v else None
    except Exception:
        return None


def _quarter_is_current(have_to_period: str, now_ts: float) -> bool:
    """True when the cached MAX(fiscal_period_end) is "current enough" —
    i.e. it lies within _FUND_CURRENT_QUARTER_GRACE_DAYS of today.

    Rationale: filers report quarterly. A cache row whose fiscal_period_end
    is at most ~95 days old IS the latest filed quarter for that issuer
    (a brand-new quarter wouldn't be available yet). Once the new
    quarter's filing lands and our adapter ingests it, have_to_period
    rolls forward and this function returns False until the NEXT quarter
    is filed. Dirt-simple, no remote-quarter lookup needed.

    Defensive: any parse failure → False (treat as stale, re-pull) — the
    rule is "skip ONLY when we're confident the cache is current."
    """
    if not have_to_period:
        return False
    try:
        from datetime import datetime as _dt
        p = _dt.fromisoformat(str(have_to_period)[:10]).timestamp()
        age = now_ts - p
        # Negative age (future date) is suspicious — treat as stale.
        if age < 0:
            return False
        return age <= _FUND_CURRENT_QUARTER_GRACE_DAYS * 86400.0
    except Exception:
        return False


def _fund_backstop_days(have_to_period, now_ts) -> int:
    """v4.14.6.111-age-aware-fund-ttl: the backstop recheck interval (DAYS) for
    a ticker, keyed on the age of its NEWEST filing (have_to_period).

      newest filing < ~18mo   → FUND_STALE_DAYS (90, unchanged active cadence)
      newest filing 18mo–5y    → FUND_BACKSTOP_DAYS_STALE   (180, 6-monthly)
      newest filing > ~5y      → FUND_BACKSTOP_DAYS_ANCIENT (365, yearly)

    NEVER a permanent skip — even the oldest tier rechecks yearly, and the
    EDGAR newly-filed index re-pulls a name the moment it actually files. The
    tier is recomputed from CURRENT have_to_period each cycle, so a stale name
    that files drops straight back to the 90d tier (self-correcting, no stored
    state). have_to_period None (pre-patch rows) → 90d default so coverage of
    those still rotates exactly as before. Never raises."""
    if not have_to_period:
        return FUND_STALE_DAYS
    try:
        from datetime import datetime as _dt
        age_d = (now_ts - _dt.fromisoformat(
            str(have_to_period)[:10]).timestamp()) / 86400.0
        if age_d > FUND_ANCIENT_FILING_DAYS:
            return FUND_BACKSTOP_DAYS_ANCIENT
        if age_d > FUND_STALE_FILING_DAYS:
            return FUND_BACKSTOP_DAYS_STALE
    except Exception:
        pass
    return FUND_STALE_DAYS


def _edgar_in_backoff() -> bool:
    """Adapter-hardening principle: don't hammer SEC while the CIK map
    is in its retry backoff window (v4.14.5.9)."""
    try:
        import tm_data_adapter_edgar as E
        if getattr(E, '_ticker_cik_map', None):
            return False  # loaded — fine
        lf = getattr(E, '_last_failed_load_at', None)
        if lf is None:
            return False
        return (time.time() - lf) < getattr(
            E, '_FAILED_LOAD_BACKOFF_SECONDS', 300)
    except Exception:
        return False


# v4.14.5.14-earnings-architecture-fix-v2: this daemon is the SOLE throttled
# bulk earnings seeder. The hot reader paths (parse_prediction, the triggers,
# _check_earnings_window) are cache-only and never fetch; the prompt-build path
# fetches the ≤20 analyzed candidates/pass on demand. This seeder fills the rest
# of the universe's earnings cache at a controlled rate so the trigger path has
# coverage without storming.
EARNINGS_SEED_PER_CYCLE = 30   # per 30-min tick → ~1/min, far under 60/min


def _earnings_recent_tickers(now_ts: float) -> list:
    """Tickers whose earnings date is within [yesterday, today], read from the
    PERSISTED earnings cache (v2 — the bulk calendar is retired). Used only to
    prioritize the fundamentals refresh; empty when nothing's seeded yet →
    caller falls back to staleness rotation (graceful)."""
    try:
        import tm_cache
        from datetime import datetime as _dt, timedelta as _td
        rows = tm_cache.get_all_earnings_rows(status='ok')
        if not rows:
            return []
        import json as _json
        today = _dt.fromtimestamp(now_ts).date()
        lo = today - _td(days=1)
        out = []
        for r in rows:
            try:
                evs = _json.loads(r['events_json'] or '[]')
            except Exception:
                evs = []
            for ev in (evs or []):
                d = (ev.get('date') or '')[:10]
                try:
                    ed = _dt.fromisoformat(d).date()
                except Exception:
                    continue
                if lo <= ed <= today:
                    out.append((r['ticker'] or '').upper())
                    break
        return out
    except Exception:
        return []


def _earnings_seed_cycle(app, now_ts: float) -> dict:
    """v2: the bounded bulk earnings SEEDER. Picks up to EARNINGS_SEED_PER_CYCLE
    universe tickers whose earnings cache row is missing / past its 24h TTL /
    past its 'failed' backoff, and live-fetches+seeds each via
    tm_discover.get_earnings_with_status (which writes the DB row). Capped per
    tick so the universe fills at ~1 fetch/min — no storm. Never raises."""
    s = {'earnings_seeded': 0, 'errors': 0}
    try:
        import tm_discover, tm_cache
        from datetime import datetime as _dt
        try:
            import tm_news_fetcher as _scope
            uni = sorted(_scope._universe(app))
        except Exception:
            uni = []
        if not uni:
            return s
        ttl = getattr(tm_discover, 'EARNINGS_CACHE_TTL_SECONDS', 24 * 3600)
        picked = 0
        # v4.14.5.14-cascade-fixes (Fix 2): pace the live earnings fetches and
        # stop the cycle after FUNDFILE_EXHAUSTION_BREAK consecutive 'failed'
        # results (real source faults). A 'failed' here is an infra fault; an
        # 'empty' (no upcoming earnings) is the common honest case and does NOT
        # count toward the breaker. Per-cycle only.
        # v4.14.6.111: pace via the adaptive 'yahoo_earnings' lane interval
        # (_EARNINGS_MIN_INTERVAL_SEC, controller-tuned) instead of the flat
        # Finnhub-calibrated 55/min, and feed each outcome back to tm_lane_pacing
        # so the controller tightens on a rate-limit and relaxes when clean. The
        # reactive per-source cooldown (tm_data_providers _DATA_COOLDOWN_CURVE)
        # still applies as a backstop — this proactive pace just prevents most
        # 429s from happening in the first place. Re-reads the module global each
        # call so a mid-cycle controller adjustment takes effect immediately.
        try:
            import tm_lane_pacing as _lp_earn
        except Exception:
            _lp_earn = None
        _earn_last = [0.0]

        def _throttle():
            try:
                iv = float(globals().get('_EARNINGS_MIN_INTERVAL_SEC', 1.09))
            except Exception:
                iv = 1.09
            now = time.time()
            gap = now - _earn_last[0]
            slept = False
            if gap < iv:
                time.sleep(iv - gap)
                slept = True
            _earn_last[0] = time.time()
            return slept
        _consec = 0
        for tk in uni:
            if picked >= EARNINGS_SEED_PER_CYCLE:
                break
            row = tm_cache.get_earnings_cache(tk)
            needs = False
            if row is None:
                needs = True
            else:
                st = row['status'] or ''
                if st == 'failed':
                    try:
                        needs = now_ts >= float(row['next_retry_at'] or 0)
                    except Exception:
                        needs = True
                elif st == 'empty':
                    # v4.14.5.28: confirmed no-coverage uses the long
                    # next_retry_at backoff (7d), not the 24h TTL — don't
                    # re-fetch structurally-uncovered tickers (BRKB/HEIA/
                    # pennies) every day. Old rows without next_retry_at
                    # fall back to the TTL so they re-confirm once and get
                    # the backoff stamped.
                    try:
                        nra = float(row['next_retry_at'] or 0)
                    except Exception:
                        nra = 0
                    if nra > 0:
                        needs = now_ts >= nra
                    else:
                        try:
                            needs = (row['as_of'] is None or
                                     (now_ts - _dt.fromisoformat(row['as_of']).timestamp())
                                     >= ttl)
                        except Exception:
                            needs = True
                else:  # ok → reseed when past the 24h TTL (earnings change)
                    try:
                        needs = (row['as_of'] is None or
                                 (now_ts - _dt.fromisoformat(row['as_of']).timestamp())
                                 >= ttl)
                    except Exception:
                        needs = True
            if not needs:
                continue
            picked += 1
            _throttle()
            try:
                _evs, _st = tm_discover.get_earnings_with_status(tk)  # fetch+seed
                s['earnings_seeded'] += 1
            except Exception:
                s['errors'] += 1
                _st = 'failed'
            # v4.14.6.111: feed the outcome to the adaptive yahoo_earnings lane.
            # 'ok'/'empty' = a clean call (relax); 'failed' = treat as a throttle
            # signal (tighten). get_earnings_with_status collapses rate_limit into
            # 'failed' (status is only ok/empty/failed), so we mark failed as
            # was_429 — a deliberately CONSERVATIVE choice for this low-urgency
            # lane: over-pacing earnings is harmless, under-pacing is what trips
            # the Yahoo cooldown. The controller relaxes again after clean windows.
            if _lp_earn is not None:
                try:
                    _lp_earn.record_outcome(
                        'yahoo_earnings',
                        success=(_st in ('ok', 'empty')),
                        was_429=(_st == 'failed'))
                except Exception:
                    pass
            if _st == 'failed':
                _consec += 1
                if _consec >= FUNDFILE_EXHAUSTION_BREAK:
                    try:
                        app._log("[earnings] sources failing (rate-limited / "
                                  "unavailable) — pausing this seed cycle, will "
                                  "retry next tick", 'amber')
                    except Exception:
                        pass
                    break
            else:
                _consec = 0
    except Exception:
        pass
    return s


def _load_fresh_empty_fundamentals(now: float) -> set:
    """v4.14.5.14-fundamentals-empty-cache: the set of tickers whose
    fundamentals lookup was confirmed EMPTY (no data from any source) within
    the last FUND_EMPTY_TTL_DAYS — loaded in ONE query so the staleness pass
    can skip them without a per-ticker DB read. Stale 'empty' rows (past TTL)
    are NOT included, so they get rechecked. Fail-open to empty set."""
    out = set()
    try:
        import tm_cache
        from datetime import datetime as _dt
        cutoff = now - FUND_EMPTY_TTL_DAYS * 86400
        for row in tm_cache.get_all_fundamentals_status('empty'):
            as_of = row['as_of']
            try:
                ts = _dt.fromisoformat(as_of).timestamp() if as_of else 0
            except Exception:
                ts = 0
            if ts >= cutoff:
                out.add((row['ticker'] or '').upper())
    except Exception:
        return set()
    return out


def _mark_fundamentals_status(ticker: str, status: str) -> None:
    """v4.14.5.14-fundamentals-empty-cache: record a fundamentals lookup
    outcome ('ok'|'empty') with an ISO as_of for the TTL. Fail-safe — a cache
    hiccup must never break the fetch loop."""
    try:
        import tm_cache
        from datetime import datetime as _dt
        tm_cache.upsert_fundamentals_status(
            ticker, status=status, as_of=_dt.now().isoformat(),
            source='fundfile')
    except Exception:
        pass


def refresh_fundamentals_universe(app, force_mode=None) -> dict:
    """Earnings-triggered + staleness-fallback fundamentals refresh.
    Pure: reads earnings cal + cache_metadata + universe; delegates
    every fetch/cache to the router/adapter. Never raises."""
    global _fund_nokey_warned
    s = {'earnings_triggered': 0, 'staleness_triggered': 0,
         'total_refreshed': 0, 'cached_empty_skipped': 0,
         'sources_used': {}, 'errors': [], 'note': ''}
    # v4.14.6.53-fundamentals-backfill: per-cycle dedup. Earnings-
    # priority loop and the staleness/index-driven loop independently
    # iterate, so a ticker present in both (e.g. AIOT) was double-
    # fetched within one cycle. Set is local to this call -> next
    # cycle starts clean. Fail-safe: a duplicate fetch is wasteful,
    # not wrong, so the wrappers below swallow any set fault.
    _fetched_this_cycle = set()
    r = _router()
    if r is None:
        s['note'] = 'router not ready'
        return s
    now = time.time()

    earnings = set(_earnings_recent_tickers(now))
    if not earnings:
        s['note'] = 'no earnings calendar — staleness rotation only'

    # v4.14.5.14-fundamentals-empty-cache: tickers confirmed to have NO
    # fundamentals data within the TTL — the staleness pass skips them so we
    # don't re-ask sources every cycle. Earnings-TRIGGERED tickers are NOT
    # skipped (a new quarter may add data → that loop bypasses this set).
    empty_set = _load_fresh_empty_fundamentals(now)

    # v4.14.6.44-fundamentals-bulk-index: bulk EDGAR daily-index reader
    # replaces the universe-wide per-ticker scan. We ask SEC "who filed
    # something new" in one call/day instead of polling 7,200 tickers.
    # Non-filers (closed-end funds, preferreds, dead tickers) never
    # appear in the form-filtered index -> never queued -> no log spam,
    # no Yahoo/Finnhub cooldowns from useless follow-ups. The 90-day
    # FUND_STALE_DAYS backstop is preserved below as a belt-and-
    # suspenders safety net for any ticker the index might miss (e.g.
    # pre-patch rows with no have_to_period).
    import tm_cache as _tmcache
    import tm_data_adapter_edgar as _edgar
    from datetime import datetime as _dt, timedelta as _td

    cursor = _tmcache.get_fundfile_index_cursor()
    if not cursor:
        # Cold-start backfill: walk ~42 calendar days (~30 business).
        cursor = (_dt.fromtimestamp(now) - _td(days=42)).date().isoformat()

    s['filing_current_skipped'] = 0
    s['index_cursor_was'] = cursor
    s['index_skipped_backoff'] = False
    try:
        if _edgar_in_backoff():
            s['index_skipped_backoff'] = True
            newly_filed = {}
        else:
            newly_filed = _edgar.iter_newly_filed_tickers(
                since_date_iso=cursor,
                forms={'10-K', '10-Q', '20-F', '10-K/A', '10-Q/A'},
                user_agent=_edgar.DEFAULT_USER_AGENT,
            )
    except Exception as e:
        s['errors'].append(f'index_reader: {type(e).__name__}')
        newly_filed = {}

    s['index_hits_total'] = len(newly_filed)

    # Diff against MAX(have_to_period, last_attempted_filing): queue ONLY
    # tickers whose filing is newer than anything we've already INGESTED or
    # ATTEMPTED. v4.14.6.111-last-attempted-filing: the last_attempted guard is
    # what breaks the perpetual re-queue loop — a delinquent/foreign-20-F/
    # amended filer (DXR/GFI/…) whose adapter-extracted have_to_period is FROZEN
    # in the past was re-queued on every cycle (filing_date always > the stuck
    # have_to_period). Now, once we've attempted that filing, last_attempted_
    # filing == filing_date, so it's skipped until a genuinely NEWER filing
    # lands. Never permanent (a newer filing clears the gate); self-correcting
    # (an extractable new filing advances have_to_period back to normal).
    stale = []
    _index_filing: dict = {}   # tk -> filing_date we're attempting (for stamping)
    for tk, filing_date_iso in newly_filed.items():
        if tk in earnings:
            continue
        if tk in empty_set:
            s['cached_empty_skipped'] += 1
            continue
        htp = _have_to_period(tk)
        la = _last_attempted_filing(tk)
        # Lexicographic compare on 'YYYY-MM-DD' is valid. Gate on the LATER of
        # the two so an already-attempted (even un-advancing) filing is skipped.
        guard = max((str(htp)[:10] if htp else ''),
                    (str(la)[:10] if la else ''))
        if guard and guard >= filing_date_iso[:10]:
            s['filing_current_skipped'] += 1
            # v4.14.6.111 (Item 5): make the build-82 deferral AUDITABLE. When
            # the skip is driven by last_attempted_filing (we already TRIED this
            # filing and have_to_period couldn't advance — the stuck delinquent/
            # foreign-20-F filer) rather than have_to_period already covering it,
            # emit ONE concise [fundfile] line per name per cycle (this loop runs
            # once per refresh cycle) so the backoff is VISIBLE in the log
            # instead of inferred from absence. Log-only — the guard logic
            # (build-80/82) is unchanged.
            la10 = str(la)[:10] if la else ''
            htp10 = str(htp)[:10] if htp else ''
            if (la10 and la10 >= filing_date_iso[:10]
                    and (not htp10 or htp10 < filing_date_iso[:10])):
                try:
                    app._log(
                        f"[fundfile] {tk} fundamentals deferred — filing "
                        f"{filing_date_iso[:10]} already attempted "
                        f"(have_to_period {htp10 or 'none'}; next on a newer "
                        f"filing)", 'muted')
                except Exception:
                    pass
            continue
        stale.append(tk)
        _index_filing[tk] = filing_date_iso[:10]
        if len(stale) >= FUND_STALENESS_PER_CYCLE:
            break
    s['index_queued'] = len(stale)

    # Belt-and-suspenders 90-day backstop, bounded by the remaining
    # budget. Catches pre-patch rows with no have_to_period AND any
    # ticker the index reader might miss.
    try:
        import tm_news_fetcher as _scope
        uni = sorted(_scope._universe(app))
    except Exception:
        uni = []
    pre_backstop = len(stale)
    if len(stale) < FUND_STALENESS_PER_CYCLE:
        in_queue = set(stale)
        for tk in uni:
            if len(stale) >= FUND_STALENESS_PER_CYCLE:
                break
            if tk in earnings or tk in empty_set or tk in in_queue:
                continue
            lr = _last_refresh_epoch(tk, 'fundamentals')
            htp = _have_to_period(tk)
            if htp and _quarter_is_current(htp, now):
                s['filing_current_skipped'] += 1
                continue
            # v4.14.6.111-age-aware-fund-ttl: stretch the recheck interval for
            # ancient-filing names (DXR/GFI-class) so we stop re-pulling the
            # same decade-old data every 90 days. Active filers stay at 90d.
            cutoff = now - _fund_backstop_days(htp, now) * 86400
            if lr is None or lr < cutoff:
                stale.append(tk)
                in_queue.add(tk)
    s['backstop_queued'] = len(stale) - pre_backstop

    # v4.14.5.14-cascade-fixes (Fix 2): pace per-call (<=55/min, under
    # Finnhub's 60) and break the cycle after FUNDFILE_EXHAUSTION_BREAK
    # consecutive provider-exhaustion results (no eligible source / a source
    # raised). Each cycle's counters are local, so a provider that recovers
    # (e.g. quota reset) is retried next tick.
    _throttle = _make_pacer()
    s['throttled'] = 0
    s['exhausted'] = False
    _consec = [0]

    def _one(tk, bucket):
        """Fetch one ticker's fundamentals. Returns False when the cycle
        should STOP (sources exhausted), True to keep going."""
        if _throttle():
            s['throttled'] += 1
        try:
            payload, status = _fetch_status(r, 'fundamentals', ticker=tk)
        except Exception as e:
            s['errors'].append(f'{tk}: {type(e).__name__}')
            payload, status = None, 'failed'
        # Provider exhaustion (a source raised, or none was eligible) — count
        # toward the circuit breaker. A clean 'empty' (reached, no data) is a
        # WORKING response and resets the counter.
        if status in ('failed', 'no_source'):
            _consec[0] += 1
            return _consec[0] < FUNDFILE_EXHAUSTION_BREAK
        _consec[0] = 0
        if payload is not None:
            s['total_refreshed'] += 1
            s[bucket] += 1
            src = (payload.get('source') if isinstance(payload, dict)
                   else None) or 'router'
            s['sources_used'][src] = (
                s['sources_used'].get(src, 0) + 1)
            # v4.14.5.14-fundamentals-empty-cache: data found → clear any
            # prior 'empty' marker so this ticker isn't skipped going forward.
            _mark_fundamentals_status(tk, 'ok')
        elif status == 'empty':
            # Every source confirmed "no fundamentals here" — cache it so the
            # staleness pass skips this ticker for FUND_EMPTY_TTL_DAYS (kills
            # the per-cycle "No fundamentals data for X" spam). NOT done on
            # 'failed'/'no_source' (handled above as exhaustion — retry later).
            _mark_fundamentals_status(tk, 'empty')
        return True

    for tk in sorted(earnings):
        try:
            if tk in _fetched_this_cycle:
                continue
            _fetched_this_cycle.add(tk)
        except Exception:
            pass  # fail-safe: a duplicate fetch is wasteful, not wrong
        if not _one(tk, 'earnings_triggered'):
            s['exhausted'] = True
            break
    if not s['exhausted']:
        for tk in stale:
            try:
                if tk in _fetched_this_cycle:
                    continue
                _fetched_this_cycle.add(tk)
            except Exception:
                pass  # fail-safe
            _cont = _one(tk, 'staleness_triggered')
            # v4.14.6.111-last-attempted-filing: stamp the filing date we just
            # ATTEMPTED for index-queued names (whether or not have_to_period
            # advanced — the point is to not re-try this same filing). The
            # upsert also refreshes last_refresh_at, which settles any lingering
            # lr-is-None backstop backlog for the same ticker. Backstop-queued
            # names (not in _index_filing) are unaffected.
            _fd = _index_filing.get(tk)
            if _fd:
                try:
                    import tm_cache as _tmc_stamp
                    _tmc_stamp.upsert_cache_metadata(
                        tk, 'fundamentals', last_attempted_filing=_fd)
                except Exception:
                    pass
            if not _cont:
                s['exhausted'] = True
                break
    if s['exhausted']:
        try:
            app._log("[fundamentals] all sources exhausted (rate-limited / "
                      "no eligible provider) — pausing this cycle, will retry "
                      "next tick (~30 min)", 'amber')
        except Exception:
            pass

    # v4.14.6.44-fundamentals-bulk-index: advance the cursor only on a
    # clean cycle so a circuit-broken / source-exhausted run can be
    # retried next tick against the same window (no silent drop of
    # filings we never got to fetch).
    if not s['exhausted']:
        try:
            today_iso = _dt.fromtimestamp(now).date().isoformat()
            _tmcache.set_fundfile_index_cursor(today_iso)
            s['index_cursor_now'] = today_iso
        except Exception:
            pass

    # No-key note (inherited fallback already handled by the adapter;
    # this is just an informative session-once line).
    try:
        import tm_cache
        ok, mode = tm_cache.lane_should_fetch('finnhub')
        if not ok and not _fund_nokey_warned:
            _fund_nokey_warned = True
            try:
                app._log("[fundamentals] Finnhub unavailable — "
                          "Yahoo-only for fundamentals", 'muted')
            except Exception:
                pass
    except Exception:
        pass
    return s


def refresh_filings_universe(app, force_tier=None) -> dict:
    """Tier-based incremental filings refresh over the EDGAR adapter
    (keyless, retry-safe v4.14.5.9). Skips entirely while the CIK map
    is in backoff (adapter-hardening principle)."""
    global _filings_backoff_warned
    s = {'tier1_refreshed': 0, 'tier2_refreshed': 0,
         'tier3_refreshed': 0, 'new_filings_total': 0,
         'sources_used': {}, 'cik_map_unavailable': False,
         'errors': []}
    if _edgar_in_backoff():
        s['cik_map_unavailable'] = True
        if not _filings_backoff_warned:
            _filings_backoff_warned = True
            try:
                app._log("[filings] CIK map unavailable, skipping "
                          "pass (will retry next cycle)", 'muted')
            except Exception:
                pass
        return s
    _filings_backoff_warned = False  # reset once healthy
    r = _router()
    if r is None:
        s['errors'].append('router not ready')
        return s

    try:
        import tm_news_fetcher as _scope
    except Exception:
        s['errors'].append('scope module unavailable')
        return s

    tick = int(getattr(app, '_fundfile_tick_count', 0))
    rot = int(getattr(app, '_filings_tier3_rotation', 0))
    due = ([force_tier] if force_tier in (1, 2, 3)
           else [t for t in (1, 2, 3)
                 if tick % FILINGS_TIER_CADENCE[t] == 0])

    scopes = {}
    if 1 in due:
        scopes[1] = _scope.get_tier_1_scope(app)
    if 2 in due:
        scopes[2] = _scope.get_tier_2_scope(app)
    if 3 in due:
        uni = sorted(_scope._universe(app))
        if uni:
            n = FILINGS_TIER3_ROTATION_SLICES
            per = (len(uni) + n - 1) // n
            i = rot % n
            scopes[3] = uni[i * per:(i + 1) * per]

    for tier in due:
        stale = FILINGS_STALE_SECONDS.get(tier, 24 * 3600)
        cutoff = time.time() - stale
        done = 0
        for tk in scopes.get(tier, []):
            lr = _last_refresh_epoch(tk, 'filings')
            if lr is not None and lr >= cutoff:
                continue
            try:
                res = r.fetch('filings', ticker=tk)
                if res is not None:
                    done += 1
                    cnt = (res.get('count')
                           if isinstance(res, dict) else None) or 0
                    s['new_filings_total'] += int(cnt) if cnt else 0
                    s['sources_used']['edgar'] = (
                        s['sources_used'].get('edgar', 0) + 1)
                    # v4.14.5.62-insider-flow: BACKGROUND-ONLY. After a fresh
                    # filings fetch for this ticker (res carries the Form-4
                    # rows + cik), compute its open-market insider buy/sell
                    # aggregate. Flag-gated (zero work + zero EDGAR load when
                    # the feature is off) + daily freshness gate (don't re-pull
                    # Form-4 XML every cycle). This is the ONLY place the
                    # per-Form-4 fetch+parse runs — never on the lookup path.
                    try:
                        if bool((getattr(app, 'cfg', {}) or {}).get(
                                'surface_insider_flow', True)) and isinstance(
                                res, dict):
                            import tm_cache as _tc_if
                            _exist = _tc_if.get_insider_flow(tk)
                            _fresh = False
                            if _exist is not None:
                                try:
                                    _ek = (_exist.keys()
                                           if hasattr(_exist, 'keys') else [])
                                    _ca = (_exist['computed_at']
                                           if 'computed_at' in _ek else None)
                                    if _ca:
                                        from datetime import datetime as _dtif
                                        _age = (_dtif.now()
                                                - _dtif.fromisoformat(_ca)
                                                ).total_seconds()
                                        _fresh = _age < 24 * 3600
                                except Exception:
                                    _fresh = False
                            if not _fresh:
                                import tm_data_adapter_edgar as _edg_if
                                _edg_if.compute_and_store_insider_flow(tk, res)
                    except Exception:
                        pass
            except Exception as e:
                s['errors'].append(f'{tk}: {type(e).__name__}')
        s[f'tier{tier}_refreshed'] = done

    # ── v4.14.6.78-insider-factor (Option B): on-demand insider-flow ───
    # The in-loop insider computation above only fires on a FRESH filings
    # fetch — but a warm filings cache never triggers one, so insider_flow
    # stays empty (starved). Compute it ON-DEMAND for the SMALL board+held
    # set (tier-1 scope = holdings + displayed picks), DECOUPLED from the
    # filings-freshness skip, gated on insider_flow's OWN 24h recency PLUS a
    # 24h per-ticker ATTEMPT gate (so tickers with no open-market activity —
    # which write NO row — aren't re-fetched every cycle), and THROTTLED to
    # INSIDER_FLOW_MAX_PER_CYCLE so EDGAR load stays tiny (dozens, never the
    # ~7k universe). Flag-gated on surface_insider_flow. NOTE: this does NOT
    # touch the filings-freshness skip for the rest of the loop above.
    try:
        if bool((getattr(app, 'cfg', {}) or {}).get(
                'surface_insider_flow', True)):
            import tm_cache as _tc_if2
            import tm_data_adapter_edgar as _edg_if2
            from datetime import datetime as _dt_if2
            _now_if = time.time()
            _attempted = getattr(app, '_insider_flow_attempted', None)
            if not isinstance(_attempted, dict):
                _attempted = {}
                try:
                    app._insider_flow_attempted = _attempted
                except Exception:
                    pass
            _targets, _seen_if = [], set()
            try:
                for _tk in (_scope.get_tier_1_scope(app) or []):
                    _u = str(_tk or '').upper()
                    if _u and _u not in _seen_if:
                        _seen_if.add(_u)
                        _targets.append(_u)
            except Exception:
                _targets = []
            _if_done = 0
            for _tk in _targets:
                if _if_done >= INSIDER_FLOW_MAX_PER_CYCLE:
                    break  # throttle — the rest get picked up next cycle
                # Daily recency on the stored aggregate.
                try:
                    _exist = _tc_if2.get_insider_flow(_tk)
                except Exception:
                    _exist = None
                if _exist is not None:
                    try:
                        _ek = (_exist.keys() if hasattr(_exist, 'keys') else [])
                        _ca = (_exist['computed_at'] if 'computed_at' in _ek else None)
                        if _ca:
                            _age = (_dt_if2.now()
                                    - _dt_if2.fromisoformat(_ca)).total_seconds()
                            if _age < 24 * 3600:
                                continue
                    except Exception:
                        pass
                # Per-ticker attempt gate (covers no-activity → no-row tickers).
                _la = _attempted.get(_tk)
                if _la is not None and (_now_if - _la) < 24 * 3600:
                    continue
                _attempted[_tk] = _now_if
                try:
                    _ires = r.fetch('filings', ticker=_tk)
                    if isinstance(_ires, dict):
                        _edg_if2.compute_and_store_insider_flow(_tk, _ires)
                        _if_done += 1
                except Exception:
                    continue
            if _if_done:
                s['insider_flow_computed'] = _if_done
                try:
                    app._log(
                        f"[fundfile] insider-flow computed for {_if_done} "
                        f"board/held ticker(s) (throttled, daily).", 'muted')
                except Exception:
                    pass
    except Exception:
        pass

    # v4.14.5.13: glance-able session tally of variant-resolved vs
    # confirmed-unresolvable tickers, so the overnight log is readable
    # without grepping. Logs only when the numbers changed.
    try:
        import tm_data_adapter_edgar as _tedg
        _tedg.maybe_log_session_summary()
    except Exception:
        pass

    return s


def launch_fundfile_refresh(app) -> None:
    """Single daemon: fundamentals every tick + filings on tier
    cadence; 30-min base; startup one-shot; pause-aware. Each half
    flag-gated independently."""
    use_f = bool(getattr(app, 'cfg', {}).get(
        'use_fundamentals_incremental', True))
    use_g = bool(getattr(app, 'cfg', {}).get(
        'use_filings_incremental', True))
    _cfg = getattr(app, 'cfg', {}) or {}
    use_e = bool(_cfg.get('use_earnings_daily_refresh', True))
    use_u = bool(_cfg.get('use_universe_maintenance', True))
    # v4.14.5.11: the daemon also carries the daily-earnings + weekly-
    # universe maintenance checks, so launch if ANY of the four
    # subsystems is enabled (independent flags).
    if not (use_f or use_g or use_e or use_u):
        return
    # v4.14.5.12: idempotent — the startup wiring and the toolbar
    # Stop->Resume handler can both call this; never run two fundfile
    # daemons at once.
    existing = getattr(app, '_fundfile_thread', None)
    if existing is not None and existing.is_alive():
        return
    stop = threading.Event()
    app._fundfile_stop = stop

    def _paused():
        try:
            import tm_holdings
            return bool(tm_holdings.is_ai_paused())
        except Exception:
            return False

    def _log(tag, d):
        try:
            interesting = (d.get('total_refreshed', 0)
                           or d.get('new_filings_total', 0)
                           or d.get('cached_empty_skipped', 0)
                           or d.get('errors'))
            if interesting:
                app.root.after(0, lambda: app._log(
                    f"[{tag}] " + ", ".join(
                        f"{k}={v}" for k, v in d.items()
                        if k in ('earnings_triggered',
                                 'staleness_triggered',
                                 'total_refreshed', 'cached_empty_skipped',
                                 'tier1_refreshed',
                                 'tier2_refreshed', 'tier3_refreshed',
                                 'new_filings_total')), 'muted'))
        except Exception:
            pass

    def _cycle(startup=False):
        if _paused():
            return
        # v4.14.5.14-earnings-architecture-fix-v2: this daemon is the SOLE
        # throttled bulk earnings seeder — seed up to EARNINGS_SEED_PER_CYCLE
        # universe tickers needing a (re)fetch into the persisted earnings
        # cache (~1 fetch/min). The retired bulk-calendar refresh is a no-op.
        # Own flag (use_earnings_daily_refresh); never break the cycle.
        try:
            if use_e:
                _es = _earnings_seed_cycle(app, time.time())
                if _es.get('earnings_seeded'):
                    _log('earnings-seed', _es)
        except Exception:
            pass
        try:
            _maybe_refresh_universe(app)
        except Exception:
            pass
        if use_f:
            try:
                _log('fundamentals',
                     refresh_fundamentals_universe(app))
            except Exception as e:
                try:
                    app.root.after(0, lambda e=e: app._log(
                        f"fundamentals tick error: {e}", 'amber'))
                except Exception:
                    pass
        if use_g:
            try:
                _log('filings', refresh_filings_universe(
                    app, force_tier=1 if startup else None))
            except Exception as e:
                try:
                    app.root.after(0, lambda e=e: app._log(
                        f"filings tick error: {e}", 'amber'))
                except Exception:
                    pass
        if not startup:
            try:
                app._fundfile_tick_count = (
                    int(getattr(app, '_fundfile_tick_count', 0)) + 1)
                if (3 in [t for t in (1, 2, 3)
                          if app._fundfile_tick_count
                          % FILINGS_TIER_CADENCE[t] == 0]):
                    app._filings_tier3_rotation = (
                        int(getattr(app, '_filings_tier3_rotation', 0))
                        + 1)
            except Exception:
                pass

    def _loop():
        try:
            from tired_market import _has_accepted_disclaimer as _ok
        except Exception:
            _ok = lambda: True
        while not _ok():
            if stop.wait(15):
                return
        # v4.14.6.35-fix-startup-stampede: 20s startup grace before
        # the first fundamentals/filings/earnings/universe cycle.
        # EDGAR + finnhub calls were stacking with layer2's consensus
        # burst at t=0; staggering by 20s lets the heaviest work
        # (layer2 60s) start last and lets news (15s) fire just
        # before. Stop-event interruptible.
        # v4.14.6.41-ui-log-coalesce: widened 20 → 45 so the first
        # fundamentals cycle no longer overlaps with the daily_bars
        # chunked phase (the cold-fill burst that was tripping the
        # UI "Not Responding" guard before the log-coalesce drainer).
        if stop.wait(45.0):
            return
        _cycle(startup=True)
        while not stop.is_set():
            if stop.wait(FUNDAMENTALS_REFRESH_INTERVAL_SECONDS):
                return
            _cycle()

    t = threading.Thread(target=_loop, daemon=True,
                          name='fundfile-refresh')
    app._fundfile_thread = t
    t.start()
