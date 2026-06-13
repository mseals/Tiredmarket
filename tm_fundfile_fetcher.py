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
FUND_STALE_DAYS = 90                           # quarterly cadence
FUND_STALENESS_PER_CYCLE = 50                  # cold-rotation budget
# v4.14.5.14-fundamentals-empty-cache: once every source confirms "no
# fundamentals for this ticker" (router status 'empty'), cache that and skip
# the ticker for this long instead of re-asking every 30-min cycle. Longer
# than the 24h earnings TTL because fundamentals change quarterly; a quarterly
# earnings trigger re-checks sooner (earnings-triggered tickers bypass this).
FUND_EMPTY_TTL_DAYS = 7
FILINGS_STALE_SECONDS = {1: 6 * 3600, 2: 24 * 3600, 3: 72 * 3600}

# v4.14.5.14-cascade-fixes (Fix 2): pace the per-ticker fundamentals/earnings
# seed loops and stop a cycle once providers are clearly exhausted, so a
# rate-limited/cooled provider can't drive a 40+ line "no eligible source"
# burst. Mirrors tm_news_fetcher's throttle (55/min, buffer under Finnhub's
# 60/min). The break is per-CYCLE only — counters are local, so the next
# 30-min tick retries from scratch (recovered providers get another chance).
FUNDFILE_RATE_LIMIT_PER_MIN = 55               # buffer under Finnhub's 60/min
FUNDFILE_EXHAUSTION_BREAK = 3                  # consecutive no-source/failed → pause cycle


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
        # v4.14.5.14-cascade-fixes (Fix 2): pace the live earnings fetches
        # (<=55/min) and stop the cycle after FUNDFILE_EXHAUSTION_BREAK
        # consecutive 'failed' results (real source faults). A 'failed' here
        # is an infra fault; an 'empty' (no upcoming earnings) is the common
        # honest case and does NOT count toward the breaker. Per-cycle only.
        _throttle = _make_pacer()
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

    # Cold staleness rotation: universe tickers whose fundamentals
    # cache_metadata is older than FUND_STALE_DAYS (or absent), capped.
    # Skip cached-empty tickers BEFORE the budget so the cycle spends its
    # FUND_STALENESS_PER_CYCLE on tickers that might actually have data.
    try:
        import tm_news_fetcher as _scope
        uni = sorted(_scope._universe(app))
    except Exception:
        uni = []
    cutoff = now - FUND_STALE_DAYS * 86400
    stale = []
    for tk in uni:
        if tk in earnings:
            continue
        if tk in empty_set:
            s['cached_empty_skipped'] += 1
            continue
        lr = _last_refresh_epoch(tk, 'fundamentals')
        if lr is None or lr < cutoff:
            stale.append(tk)
        if len(stale) >= FUND_STALENESS_PER_CYCLE:
            break

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
        if not _one(tk, 'earnings_triggered'):
            s['exhausted'] = True
            break
    if not s['exhausted']:
        for tk in stale:
            if not _one(tk, 'staleness_triggered'):
                s['exhausted'] = True
                break
    if s['exhausted']:
        try:
            app._log("[fundamentals] all sources exhausted (rate-limited / "
                      "no eligible provider) — pausing this cycle, will retry "
                      "next tick (~30 min)", 'amber')
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
        if stop.wait(20.0):
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
