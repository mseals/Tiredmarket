"""tm_news_fetcher — Arc B patch 2 (v4.14.5.8): tiered universe-wide
news refresh.

This is a PURE SCHEDULING/SCOPE LAYER. It does NOT fetch, parse, dedup,
or cache news itself — it calls the existing per-ticker chokepoint
`tired_market.deep_news_scan(ticker, db)` (tired_market.py:3223), the
same function tm_scheduler already uses for the holdings scan. That
function already:
  - falls through RSS -> Yahoo -> Finnhub -> Google News in priority
    order (so NO-KEY SAFETY is inherited: a missing Finnhub key just
    means the Finnhub adapter returns None and the others still run),
  - dedups articles by (url, timestamp) before insert,
  - writes news_signals + news_cache + news_scans, and
  - upserts cache_metadata('<ticker>','news_signals') with
    last_refresh_at on every successful scan.

So correctness needs nothing new. The two levers this module adds:
  1. SCOPE TIERS — hot scope (holdings/Recommend/recent) refreshed
     often, cold universe trickled slowly, so the ~3h full-universe
     sweep never blocks and Finnhub's 60/min free cap is respected.
  2. FRESHNESS SKIP — a ticker whose cache_metadata last_refresh_at
     for the news_signals lane is within NEWS_STALE_SECONDS is skipped
     (the real budget saver: don't re-scan what's already fresh).

DELIBERATELY DEFERRED (matches the v4.14.5.6 gap-only-fetch deferral):
narrowing each deep_news_scan call to from=last_refresh_at instead of
its fixed 7-day window. deep_news_scan exposes no lookback parameter
end-to-end; threading one through it + its four source fetchers is a
cross-module change untestable against live network here, and it is
NOT required for correctness — the (url,timestamp) dedup makes repeat
7-day calls produce zero duplicate rows, and the freshness skip
already eliminates the redundant calls. Window-narrowing is a pure
bandwidth optimization for a later patch.
"""

from __future__ import annotations

import threading
import time

NEWS_REFRESH_INTERVAL_SECONDS = 300          # 5 min per tick (tier 1)
TIER_CADENCE = {1: 1, 2: 6, 3: 12}           # ticks between runs
TIER_3_ROTATION_SLICES = 50                  # universe split into 50
FINNHUB_RATE_LIMIT_PER_MIN = 55              # buffer under the 60 cap
NEWS_STALE_SECONDS = {                       # don't re-scan if fresher
    1: 4 * 3600,    # hot scope: 4h
    2: 12 * 3600,   # warm: 12h
    3: 36 * 3600,   # cold universe: 36h
}

_no_source_warned = False  # session-once "no sources" log guard


def _conn(app):
    db = getattr(app, 'db', None)
    return getattr(db, 'conn', None) if db is not None else None


def _norm(seq) -> list:
    out, seen = [], set()
    for t in seq or []:
        t = (str(t) or '').strip().upper()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _is_active_holding(h: dict) -> bool:
    """v4.14.5.14-cadence-dampening-and-f5a-hygiene Part B (2026-05-20):
    holding is "active" for data-daemon purposes if its status is not
    `written_off`. Written-off positions are Mike's explicit "give up"
    marker — the money is gone, news / EDGAR / fundamentals cycles on
    those tickers are pure waste (UBQU and BRZV today). EventDetector
    already gates on `tradable=False` independently; this filter
    duplicates that protection for the data-refresh chokepoints that
    iterate the raw holdings list. Treat missing/unknown `status` as
    active (fail-OPEN — don't accidentally hide a real holding because
    of a typo). Locked positions ARE active (locked != written_off);
    Mike still wants news/fundamentals on locked holdings because the
    lock may eventually clear."""
    if not isinstance(h, dict):
        return False
    return str(h.get('status') or 'tradable').strip() != 'written_off'


def _holdings_tickers(app) -> list:
    try:
        st = getattr(app, '_holdings_state', None) or {}
        mgr = st.get('mgr')
        h = getattr(mgr, 'holdings', None) if mgr is not None else None
        if h:
            return _norm(x.get('ticker') for x in h
                         if isinstance(x, dict)
                         and _is_active_holding(x))
    except Exception:
        pass
    try:
        return _norm(x.get('ticker') for x in
                     (getattr(app, 'portfolio', None) or [])
                     if isinstance(x, dict)
                     and _is_active_holding(x))
    except Exception:
        return []


def _recommend_cache_tickers(app, tier: str) -> list:
    conn = _conn(app)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT ticker FROM recommend_cache WHERE tier = ?",
            (tier,)).fetchall()
        return _norm(r[0] for r in rows)
    except Exception:
        return []


def _watchlist_tickers(app) -> list:
    try:
        import tm_discover
        wl = getattr(tm_discover, 'Watchlist', None)
        st = getattr(app, '_holdings_state', None) or {}
        inst = st.get('watchlist')
        if inst is None and wl is not None:
            return []
        items = getattr(inst, 'tickers', None) or (
            inst.all() if hasattr(inst, 'all') else None)
        return _norm(items)
    except Exception:
        return []


def _recent_lookup_tickers(app) -> list:
    try:
        ps = getattr(app, '_price_service', None)
        rec = getattr(ps, '_recent', None) if ps is not None else None
        if rec:
            return _norm(rec.keys() if hasattr(rec, 'keys') else rec)
    except Exception:
        pass
    return []


def _universe(app) -> list:
    try:
        import tm_cache
        return _norm(tm_cache._load_universe_tickers())
    except Exception:
        return []


def get_tier_1_scope(app) -> list:
    """Holdings + Recommend displayed tier + recent Look Ups."""
    s = (_holdings_tickers(app)
         + _recommend_cache_tickers(app, 'displayed')
         + _recent_lookup_tickers(app))
    return _norm(s)


def get_tier_2_scope(app) -> list:
    """Recommend bench + watchlist, excluding tier 1."""
    t1 = set(get_tier_1_scope(app))
    s = _recommend_cache_tickers(app, 'bench') + _watchlist_tickers(app)
    return [t for t in _norm(s) if t not in t1]


def get_tier_3_scope(app, rotation_index: int) -> list:
    """One alphabetical slice of the universe, excluding tiers 1+2."""
    uni = sorted(_universe(app))
    if not uni:
        return []
    hot = set(get_tier_1_scope(app)) | set(get_tier_2_scope(app))
    uni = [t for t in uni if t not in hot]
    if not uni:
        return []
    n = TIER_3_ROTATION_SLICES
    idx = rotation_index % n
    per = (len(uni) + n - 1) // n
    return uni[idx * per:(idx + 1) * per]


def _last_news_refresh_epoch(ticker: str):
    try:
        import tm_cache
        from datetime import datetime as _dt
        rows = tm_cache.get_cache_metadata(ticker, 'news_signals') or []
        if not rows:
            return None
        lr = tm_cache._row_get(rows[0], 'last_refresh_at')
        if not lr:
            return None
        return _dt.fromisoformat(str(lr).replace('Z', '')).timestamp()
    except Exception:
        return None


def refresh_news_universe(app, force_tier=None) -> dict:
    """Run whichever tiers are due (or just force_tier). Calls the
    reused deep_news_scan per due-and-stale ticker, rate-limited.
    Pure: reads scope + cache_metadata, delegates all fetch/cache to
    deep_news_scan. Never raises — degrades to a summary."""
    global _no_source_warned
    summary = {'tier1_refreshed': 0, 'tier2_refreshed': 0,
               'tier3_refreshed': 0, 'total_new_articles': 0,
               'sources_used': {}, 'errors': [],
               'no_sources_available': False, 'throttled': 0}

    db = getattr(app, 'db', None)
    if db is None:
        summary['errors'].append('no db')
        return summary
    try:
        import tired_market as _tm
        scan = getattr(_tm, 'deep_news_scan', None)
    except Exception as e:
        summary['errors'].append(f'import: {e}')
        return summary
    if scan is None:
        summary['errors'].append('deep_news_scan missing')
        return summary

    tick = int(getattr(app, '_news_tick_count', 0))
    rot = int(getattr(app, '_news_tier3_rotation', 0))

    due = []
    if force_tier in (1, 2, 3):
        due = [force_tier]
    else:
        for t in (1, 2, 3):
            if tick % TIER_CADENCE[t] == 0:
                due.append(t)

    scopes = {}
    if 1 in due:
        scopes[1] = get_tier_1_scope(app)
    if 2 in due:
        scopes[2] = get_tier_2_scope(app)
    if 3 in due:
        scopes[3] = get_tier_3_scope(app, rot)

    # Rate-limit the per-ticker scans (deep_news_scan does <=1 Finnhub
    # call/ticker; pacing the whole loop at FINNHUB_RATE_LIMIT_PER_MIN
    # keeps the free cap safe regardless of source mix).
    min_interval = 60.0 / max(1, FINNHUB_RATE_LIMIT_PER_MIN)
    last_call = [0.0]

    def _scan_one(tk, tier):
        now = time.time()
        gap = now - last_call[0]
        if gap < min_interval:
            time.sleep(min_interval - gap)
            summary['throttled'] += 1
        last_call[0] = time.time()
        before = None
        try:
            res = scan(tk, db) or {}
            n = int(res.get('total') or 0)
            summary['total_new_articles'] += max(0, n)
            for src, c in (res.get('by_source') or {}).items():
                summary['sources_used'][src] = (
                    summary['sources_used'].get(src, 0) + int(c or 0))
            return True
        except Exception as e:
            summary['errors'].append(f'{tk}: {type(e).__name__}')
            return False

    for tier in due:
        stale = NEWS_STALE_SECONDS.get(tier, 12 * 3600)
        cutoff = time.time() - stale
        done = 0
        for tk in scopes.get(tier, []):
            lr = _last_news_refresh_epoch(tk)
            if lr is not None and lr >= cutoff:
                continue  # fresh enough — skip (the budget lever)
            if _scan_one(tk, tier):
                done += 1
        summary[f'tier{tier}_refreshed'] = done

    if (not summary['sources_used']
            and summary['total_new_articles'] == 0
            and any(scopes.values())):
        # Could not get anything from any source this run.
        summary['no_sources_available'] = True
        if not _no_source_warned:
            _no_source_warned = True
            try:
                app._log("[news] no sources returned data this "
                          "run (will retry next tick)", 'amber')
            except Exception:
                pass

    if not force_tier:
        try:
            app._news_tick_count = tick + 1
            if 3 in due:
                app._news_tier3_rotation = rot + 1
        except Exception:
            pass
    return summary


def launch_news_refresh(app) -> None:
    """Daemon tick (same pattern as recommend_cache / auto-refresh).
    Startup one-shot (tier 1) then every NEWS_REFRESH_INTERVAL_SECONDS.
    Pause-aware. Flag-gated by cfg['use_news_incremental']."""
    if not bool(getattr(app, 'cfg', {}).get(
            'use_news_incremental', True)):
        return
    # v4.14.5.12: idempotent — the startup wiring and the toolbar
    # Stop->Resume handler can both call this; never run two news
    # daemons at once.
    existing = getattr(app, '_news_refresh_thread', None)
    if existing is not None and existing.is_alive():
        return
    stop = threading.Event()
    app._news_refresh_stop = stop

    def _paused():
        try:
            import tm_holdings
            return bool(tm_holdings.is_ai_paused())
        except Exception:
            return False

    def _summ(tag, s):
        try:
            if s.get('total_new_articles', 0) > 0 or s.get('errors'):
                srcs = ", ".join(f"{k} {v}" for k, v in
                                 (s.get('sources_used') or {}).items())
                app.root.after(0, lambda: app._log(
                    f"[news] {tag}: "
                    f"t1={s['tier1_refreshed']} "
                    f"t2={s['tier2_refreshed']} "
                    f"t3={s['tier3_refreshed']}, "
                    f"{s['total_new_articles']} new"
                    + (f" ({srcs})" if srcs else ""), 'muted'))
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
        # v4.14.6.35-fix-startup-stampede: 15s startup grace before
        # the first tier-1 refresh. News tier-1 was hitting 132
        # articles in a single burst at t=0 alongside layer2+queue
        # runner+fundfile; spreading the grace prevents the GIL +
        # AI-provider stampede. Stop-event interruptible.
        if stop.wait(15.0):
            return
        if not _paused():
            try:
                _summ('startup', refresh_news_universe(
                    app, force_tier=1))
            except Exception as e:
                try:
                    app.root.after(0, lambda e=e: app._log(
                        f"news startup refresh error: {e}", 'amber'))
                except Exception:
                    pass
        while not stop.is_set():
            if stop.wait(NEWS_REFRESH_INTERVAL_SECONDS):
                return
            if _paused():
                continue
            try:
                _summ('tick', refresh_news_universe(app))
            except Exception as e:
                try:
                    app.root.after(0, lambda e=e: app._log(
                        f"news refresh tick error: {e}", 'amber'))
                except Exception:
                    pass

    t = threading.Thread(target=_loop, daemon=True,
                          name='news-refresh')
    app._news_refresh_thread = t
    t.start()
