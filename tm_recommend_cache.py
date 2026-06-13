"""tm_recommend_cache — Step 2 of the three-surface arc (v4.14.5.4).

Persists, per path, the filter-ranked ∩ current-BUY candidate set into
the `recommend_cache` table with displayed/bench tiers, and exposes a
pure refresh function. The Recommend window reads the displayed tier
from this table instead of the runner's churn-prone recency window.

Pure-function oriented: refresh reads tm_recommend_filter +
PredictionsLog, writes only `recommend_cache`. No tk/UI, no global
state. The background tick / startup / choices-change callers live in
tired_market.py and just invoke refresh_recommend_cache(app).
"""

from __future__ import annotations

import time

# Top N per path shown in Recommend; next M held in reserve. 10+20=30
# matches the filter's top-30-per-band output. Tunable.
DISPLAYED_TIER_CAPACITY = 10
BENCH_TIER_CAPACITY = 20
TOTAL_TIER_CAPACITY = DISPLAYED_TIER_CAPACITY + BENCH_TIER_CAPACITY

RECOMMEND_CACHE_REFRESH_INTERVAL_SECONDS = 300  # 5 min background tick

_ALL_BANDS = ('penny', 'low', 'mid', 'high')


def _paths_for_refresh() -> tuple:
    """v4.14.5.14-displayed-picks-recovery Part C2 (2026-05-20):
    read the path set at call time from `tm_holdings.PATHS` instead
    of a stale module-level literal. The original `_PATHS` literal
    was `('slow_safe', 'moderate', 'aggressive', 'lottery',
    'penny_lottery')` — when the v4.14.5.14-merge-and-unify patch
    folded `penny_lottery` into `lottery`, `tm_holdings.PATHS` and
    most other path-iteration sites were updated, but this literal
    was missed. Result: every refresh tick iterated penny_lottery,
    found the 3 historical penny_lottery BUY predictions still in
    predictions.jsonl, and re-wrote a row to `recommend_cache` —
    which is why ACH/penny_lottery kept reappearing despite the
    F5a-replay one-shot cleanup. Fail-OPEN to the historical
    5-path tuple if tm_holdings can't be imported (preserves
    pre-fix behaviour on the import-failure path rather than
    silently iterating zero paths)."""
    try:
        import tm_holdings as _th_rc
        return tuple(sorted(_th_rc.PATHS.keys()))
    except Exception:
        return ('slow_safe', 'moderate', 'aggressive',
                'lottery', 'penny_lottery')


# Legacy alias for any external reader; kept for backward-compat
# but the live read is `_paths_for_refresh()` below.
_PATHS = _paths_for_refresh()


def _conn(app):
    db = getattr(app, 'db', None)
    return getattr(db, 'conn', None) if db is not None else None


def _selected_bands(app) -> list:
    """v4.14.5.14a.7: the Choices dialog (and its `v415_price_ranges`
    key) was removed — user price intent now lives in Portfolio
    Recommendations' price-tier filter, applied at view time. This
    always returns ALL bands so the momentum filter scores every
    candidate uniformly (no Choices-driven in-band sort tiebreak).
    Signature/return shape unchanged so callers are untouched."""
    return list(_ALL_BANDS)


def _epoch_from_rec(rec, fallback: int) -> float:
    """v4.14.5.14a: parse a prediction record's timestamp to epoch
    seconds for the FIFO tiebreak. Falls back to `fallback` on any
    parse problem so sorting stays deterministic."""
    ts = (rec.get('timestamp') or '') if isinstance(rec, dict) else ''
    if not ts:
        return float(fallback)
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.split('.')[0]).timestamp()
    except Exception:
        return float(fallback)


def _current_buy_tickers_for_path(plog, path: str,
                                    no_rescind: bool = True) -> list:
    """v4.14.5.14a: every ticker whose MOST-RECENT (ticker, path)
    record is a currently-open BUY. Drives the open-BUY-sourced
    Recommend population (Component B). Uses the same
    most-recent-wins / open / BUY semantics as _is_current_buy so a
    ticker later superseded by WATCH/AVOID is correctly excluded.

    v4.14.5.94-watch-phase1 (2026-06-11): under the no-rescind rule
    (no_rescind=True, default), a later WATCH (or HOLD or NO_CALL)
    does NOT supersede a prior BUY — WATCH means "wait", not
    "rescind." A later AVOID still supersedes (the AVOID-drop is
    preserved). Caller (refresh_recommend_cache) resolves the
    cfg['use_watch_no_rescind'] flag and passes it through. A
    pure-WATCH ticker (no prior BUY) is NOT surfaced — the Watching
    list (separate surface) handles those."""
    # v4.14.6.35-fix-startup-stampede: working-set sufficient. This
    # filters to status=open BUYs, and the working set already loads
    # every open record regardless of age. A later AVOID that would
    # disqualify a BUY is itself recent (it has to follow the open
    # BUY in time and is in the recent-N window). Reverting from
    # get_all_full so the recommend-cache daemon's first tick doesn't
    # block on the predictions tail-load — it's one of the v4.14.6.34
    # stampede contributors.
    try:
        allrecs = plog.get_all()
    except Exception:
        return []
    seen = set()
    cand = []
    for r in allrecs:
        if (r.get('path') or '').strip() != path:
            continue
        if (r.get('direction') or '').upper() != 'BUY':
            continue
        if r.get('status') not in (None, '', 'open'):
            continue
        tk = (r.get('ticker') or '').upper()
        if tk and tk not in seen:
            seen.add(tk)
            cand.append(tk)
    out = []
    for tk in cand:
        rec = _is_current_buy(plog, tk, path, no_rescind=no_rescind)
        if rec is not None:
            out.append((tk, rec))
    return out


def _is_current_buy(plog, ticker: str, path: str,
                     no_rescind: bool = True):
    """Return a currently-eligible BUY record for (ticker, path), or
    None.

    v4.14.5.14a baseline: the most-recent prediction must be BUY +
    status=open.

    v4.14.5.94-watch-phase1 (2026-06-11): under the no-rescind rule
    (no_rescind=True, default), eligibility loosens to "an open BUY
    exists for (ticker, path) AND no AVOID has been cast after that
    BUY." A later WATCH/HOLD/NO_CALL does NOT disqualify (WATCH means
    "wait," not "rescind"); a later AVOID still does (AVOID means
    "don't touch" — drop, consistent with the dialog's AVOID-drop).
    no_rescind=False → exact pre-patch "most-recent == BUY" rule
    (instant rollback when cfg['use_watch_no_rescind'] is False).
    Returns the BUY record (so the caller has its target / entry /
    timeframe / etc.) or None."""
    if not no_rescind:
        try:
            rec = plog.get_most_recent_for_ticker_and_path(ticker, path)
        except Exception:
            return None
        if not rec:
            return None
        if (rec.get('direction') or '').upper() != 'BUY':
            return None
        if rec.get('status') not in (None, '', 'open'):
            return None
        return rec
    # No-rescind path: walk per-ticker history for the latest BUY and
    # check no AVOID has been written after it.
    # v4.14.6.35-fix-startup-stampede: working-set sufficient — same
    # reasoning as _current_buy_tickers_for_path above (the latest
    # open BUY + any disqualifying later AVOID are both recent).
    try:
        allrecs = plog.get_all()
    except Exception:
        return None
    tk_u = (ticker or '').upper()
    path_s = (path or '').strip()
    latest_buy = None
    latest_buy_ts = ''
    latest_avoid_ts = ''
    for r in allrecs:
        if (r.get('ticker') or '').upper() != tk_u:
            continue
        if (r.get('path') or '').strip() != path_s:
            continue
        d = (r.get('direction') or '').upper()
        ts = r.get('timestamp', '') or ''
        if d == 'BUY':
            # Open-status BUYs only — a target_hit / stop_hit / expired
            # / sold BUY is closed, not eligible.
            if r.get('status') not in (None, '', 'open'):
                continue
            if ts > latest_buy_ts:
                latest_buy = r
                latest_buy_ts = ts
        elif d == 'AVOID':
            # AVOID rescinds — track its newest timestamp regardless of
            # status. (AVOIDs aren't supposed to have outcome-close
            # statuses but the predicate stays defensive.)
            if ts > latest_avoid_ts:
                latest_avoid_ts = ts
    if latest_buy is None:
        return None
    # AVOID-after-BUY disqualifies; AVOID-before-BUY is irrelevant
    # (the BUY came after, the user's signal is fresh).
    if latest_avoid_ts and latest_avoid_ts > latest_buy_ts:
        return None
    return latest_buy


def refresh_recommend_cache(app, paths=None, force: bool = False) -> dict:
    """Recompute recommend_cache: filter candidates ∩ current BUYs,
    tiered displayed/bench, per path. Preserves first_seen_at for
    surviving tickers; drops tickers no longer in the top-30.

    Returns a summary dict. Never raises — degrades to a status/message.
    """
    conn = _conn(app)
    if conn is None:
        return {'refreshed_paths': [], 'total_displayed': 0,
                'total_bench': 0, 'status': 'error',
                'message': 'no db connection'}

    state = getattr(app, '_holdings_state', None) or {}
    plog = state.get('predictions_log')
    if plog is None:
        return {'refreshed_paths': [], 'total_displayed': 0,
                'total_bench': 0, 'status': 'error',
                'message': 'no predictions_log'}

    try:
        import tm_recommend_filter as _trf
    except Exception as e:
        return {'refreshed_paths': [], 'total_displayed': 0,
                'total_bench': 0, 'status': 'error',
                'message': f'filter import failed: {e}'}

    bands = _selected_bands(app)
    try:
        fres = _trf.compute_filter(bands)
    except Exception as e:
        return {'refreshed_paths': [], 'total_displayed': 0,
                'total_bench': 0, 'status': 'error',
                'message': f'compute_filter raised: {e}'}

    status = fres.get('status', 'ok')
    # v4.14.5.57-empty-cache-bypass: the coverage gate exists to NOT replace a
    # POPULATED snapshot with a thin one while the price cache fills. When
    # recommend_cache is EMPTY there is no snapshot to protect — and bailing
    # leaves it empty forever on a single-key cold start (80% universe coverage
    # takes hours), pinning the user at 0/10 while real BUYs pile up in
    # recommend_queue. So bail on cache_filling ONLY when the cache already has
    # rows; when it's empty, fall through to the per-path build, which populates
    # displayed/bench from the open BUYs (coverage-independent: open-BUY-sourced
    # + fail-open band gate). Empty vs thin-but-populated is a row-count
    # distinction, so warm-refresh protection is untouched.
    try:
        cache_has_rows = bool(conn.execute(
            "SELECT EXISTS(SELECT 1 FROM recommend_cache)").fetchone()[0])
    except Exception:
        cache_has_rows = True  # fail-safe: on any doubt, preserve the guard
    if status == 'cache_filling' and not force and cache_has_rows:
        # Coverage gate: do NOT replace a good existing (populated) snapshot
        # with a thin one while the price cache is still filling.
        return {'refreshed_paths': [], 'total_displayed': 0,
                'total_bench': 0, 'status': 'cache_filling',
                'message': fres.get('note')
                or 'price cache still filling; cache left intact'}

    # Filter candidates already ranked desc by score (flat list).
    candidates = fres.get('candidates') or []
    # v4.14.5.14a Component B: momentum is now a SORT KEY over the
    # open-BUY pool, not a population gate. Build a ticker→cand map so
    # an open BUY that the filter scored carries its momentum/volume
    # breakdown; open BUYs the filter didn't score (no cached price /
    # out of selected band) still appear, with neutral score 0.0.
    score_map = {}
    for _c in candidates:
        _tk = (_c.get('ticker') or '').upper()
        if _tk:
            score_map[_tk] = _c
    use_open_src = bool((getattr(app, 'cfg', {}) or {}).get(
        'use_open_buys_as_recommend_source', True))
    # v4.14.5.14-displayed-picks-recovery Part C2: call-time path
    # read so post-merge `tm_holdings.PATHS` is the source of truth.
    target_paths = list(paths) if paths else list(_paths_for_refresh())
    now = int(time.time())
    total_disp = 0
    total_bench = 0
    refreshed = []
    swaps_by_path = {}

    for path in target_paths:
        # Pre-existing rows (for first_seen_at preservation + swap diff).
        try:
            prev = {
                r[0]: (r[1], r[2]) for r in conn.execute(
                    "SELECT ticker, tier, first_seen_at "
                    "FROM recommend_cache WHERE path = ?", (path,))
            }
        except Exception:
            prev = {}
        prev_displayed = {t for t, (tier, _) in prev.items()
                          if tier == 'displayed'}

        if use_open_src:
            # v4.14.5.14a Component B: source = ALL current open BUYs
            # for this path. Sort by momentum DESC, then FIFO
            # (first_seen_at ASC — preserved cache age if already
            # shown, else the prediction's own age), then ticker for
            # determinism. Take up to 30. This stops the momentum
            # filter from discarding ~96% of valid AI BUYs.
            # v4.14.5.94-watch-phase1: resolve the no-rescind flag from
            # the app's cfg here (refresh_recommend_cache has app in
            # scope) and pass it through. Default True; off → exact
            # pre-patch "most-recent must be BUY" rule.
            try:
                _no_rescind = bool((getattr(app, 'cfg', {}) or {}).get(
                    'use_watch_no_rescind', True))
            except Exception:
                _no_rescind = True
            ob = _current_buy_tickers_for_path(
                plog, path, no_rescind=_no_rescind)
            ranked = []
            for tk, rec in ob:
                cand = score_map.get(tk) or {
                    'ticker': tk, 'score': 0.0, 'breakdown': {}}
                score = float(cand.get('score') or 0.0)
                if tk in prev:
                    fs = prev[tk][1]
                    try:
                        fs = float(fs)
                    except Exception:
                        fs = float(now)
                else:
                    fs = _epoch_from_rec(rec, now)
                ranked.append((score, fs, tk, cand, rec))
            ranked.sort(key=lambda x: (-x[0], x[1], x[2]))
            kept = [(tk, cand, rec)
                    for _s, _f, tk, cand, rec
                    in ranked[:TOTAL_TIER_CAPACITY]]
        else:
            # Legacy v4.14.5.13 path: walk filter candidates in rank
            # order; keep those that are a current BUY for THIS path
            # until we have up to 30 (filter acts as a gate).
            kept = []
            for cand in candidates:
                if len(kept) >= TOTAL_TIER_CAPACITY:
                    break
                tk = (cand.get('ticker') or '').upper()
                if not tk:
                    continue
                rec = _is_current_buy(plog, tk, path)
                if rec is None:
                    continue
                kept.append((tk, cand, rec))

        # v4.14.5.14-recommend-cache-band-gate: apply the SAME price-band
        # gate the dispatch + Layer 2 paths use, here at the cache-WRITE
        # site (the third consumer that was bypassing it). Without this,
        # refresh re-derived out-of-band rows (e.g. ALGM $46 / ADT $7 /
        # AMPX $16 on lottery $0-$5) from still-open BUY predictions every
        # refresh — the R2 one-shot sweep purged them but they came right
        # back, and Layer 2 then logged 6 "[eligibility] skipped … outside
        # band" lines per cycle. Excluding them from `kept` here means they
        # never enter the cache (and any existing out-of-band row falls
        # into `stale` below and is DELETEd), so Layer 2 has nothing to
        # validate → the noise disappears at its source. quiet=True (this
        # refresh runs often; the dispatch/Layer 2 gates already log when
        # relevant). Fail-OPEN: any error / missing price / flag-off leaves
        # `kept` unchanged (the gate never drops a ticker we can't price).
        try:
            import tm_queue_runner as _qr_band
            _band_kept = set(_qr_band._eligibility_price_band_filter(
                app, path, [tk for tk, _, _ in kept],
                src_label='recommend-cache-refresh', quiet=True))
            kept = [(tk, cand, rec) for (tk, cand, rec) in kept
                    if tk in _band_kept]
        except Exception:
            pass

        new_tickers = {tk for tk, _, _ in kept}

        # Atomic rebuild for this path: delete rows not surviving, then
        # upsert survivors with correct tier/rank, preserving
        # first_seen_at. Single transaction.
        try:
            cur = conn.cursor()
            # Drop tickers that fell off entirely.
            stale = [t for t in prev if t not in new_tickers]
            if stale:
                cur.executemany(
                    "DELETE FROM recommend_cache "
                    "WHERE path = ? AND ticker = ?",
                    [(path, t) for t in stale])
            for idx, (tk, cand, _rec) in enumerate(kept):
                if idx < DISPLAYED_TIER_CAPACITY:
                    tier = 'displayed'
                    rank = idx
                else:
                    tier = 'bench'
                    rank = idx - DISPLAYED_TIER_CAPACITY
                bd = cand.get('breakdown') or {}
                first_seen = (prev[tk][1] if tk in prev else now)
                cur.execute(
                    "INSERT INTO recommend_cache "
                    "(ticker, path, tier, rank_within_tier, "
                    " filter_score, momentum_score, rel_volume_score, "
                    " social_bonus, news_bonus, "
                    " last_buy_confirmation_at, first_seen_at, "
                    " last_refresh_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(ticker, path) DO UPDATE SET "
                    " tier=excluded.tier, "
                    " rank_within_tier=excluded.rank_within_tier, "
                    " filter_score=excluded.filter_score, "
                    " momentum_score=excluded.momentum_score, "
                    " rel_volume_score=excluded.rel_volume_score, "
                    " social_bonus=excluded.social_bonus, "
                    " news_bonus=excluded.news_bonus, "
                    " last_buy_confirmation_at="
                    "   excluded.last_buy_confirmation_at, "
                    " last_refresh_at=excluded.last_refresh_at",
                    (tk, path, tier, rank,
                     float(cand.get('score') or 0.0),
                     bd.get('momentum'), bd.get('rel_volume'),
                     bd.get('social_bonus'), bd.get('news_bonus'),
                     now, first_seen, now))
                if tier == 'displayed':
                    total_disp += 1
                else:
                    total_bench += 1
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return {'refreshed_paths': refreshed,
                    'total_displayed': total_disp,
                    'total_bench': total_bench, 'status': 'error',
                    'message': f'cache write failed for {path}: {e}'}

        new_displayed = {tk for i, (tk, _, _) in enumerate(kept)
                         if i < DISPLAYED_TIER_CAPACITY}
        swaps = len(prev_displayed.symmetric_difference(new_displayed))
        if swaps:
            swaps_by_path[path] = swaps // 2 if swaps >= 2 else swaps
        refreshed.append(path)

    msg = 'ok'
    if swaps_by_path:
        msg = "; ".join(f"{p}: {n} swap(s)"
                        for p, n in swaps_by_path.items())
    return {'refreshed_paths': refreshed,
            'total_displayed': total_disp,
            'total_bench': total_bench,
            'status': status if status == 'ok' else status,
            'message': msg}
