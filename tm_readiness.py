"""tm_readiness — v4.14.5.87 readiness-snapshot aggregator.

Bundles the fragmented "is the app ready to use" state into ONE cheap,
read-only primitive. Read-only by design: this module calls existing
read functions (`tm_cache.get_fill_progress`, `_priority_tickers_for_app`,
`app.news_priority_slice_complete`, a single COUNT(*) on the
recommend_cache table) and returns a plain dict. It never fetches,
never writes, never touches `_on_close`, never renders UI.

It exists so:
  - the v.86 two-phase news log has a sibling for the other lanes,
  - a future close-time "state, not pressure" message can read ONE
    primitive instead of cobbling 5 things together,
  - the dead `_obs_cache_thin_initial_fill` predicate in
    tm_teacher_intercept (which calls get_fill_progress with no args
    and silently returns False) gets a working data source.

## The product decision (locked in code, not config)

**Scope = pick-relevant.** The "ready" verdict and per-lane numbers
are measured against `_priority_tickers_for_app(app)` (seed pools +
holdings + watchlist + recommend_cache picks — same set the v.86 news
two-phase work uses). The deep-universe tail filling is background;
it does NOT block readiness. Reporting full-universe numbers too
would be cheap but is intentionally OUT — the user cares whether the
tickers that can appear in Recommend/Holdings are current, not
whether some delisted micro-cap deep in the universe still needs
fundamentals.

**Lanes reported:** news_signals, daily_bars, filings, fundamentals.
Each via the existing `get_fill_progress(lane, scope_tickers)`.

**Pick count:** displayed picks across all paths from the
`recommend_cache` table (the same table the Recommend dialog reads
its DISPLAYED tier from). Single `SELECT COUNT(*)` query — cheap,
truthful, no re-invented count.

**Overall ready rule:** conservative + honest:
    ready == news_priority_slice_complete
          AND daily_bars(pick-relevant).pct_complete >= 100.0

Reasoning: the picks need prices (daily_bars) to render meaningfully,
and the pick-relevant news slice has to be in (so news bonuses /
catalysts aren't a vacuum). Filings + fundamentals are useful but
absent-data falls back gracefully in the renderer, so we don't gate
on them. The bar can be lifted later; better to under-claim than
over-claim.

A snapshot called during early startup before any fill completes
returns `ready=False` with graceful lane values. The aggregator
must never raise.
"""
from __future__ import annotations

import threading
from typing import Optional


# ─── Module state ─────────────────────────────────────────────────────

# Master toggle. False → aggregator still callable (returns the
# documented dict) but the Part B "first ready" log line stays silent.
# Flag-off is one flip back to exact pre-v.87 runtime behavior.
_READINESS_SNAPSHOT_ENABLED: bool = True

# v4.14.5.87 Part B: once-per-process dedup so the "[readiness] picks
# ready" log line fires exactly once when the system first reads
# ready. Subsequent passes are no-ops. Resets on app restart (in
# memory; the SECOND start that comes up already-ready will emit ONE
# log line again, which is the desired behavior — one signal per
# launch, not one signal ever).
_FIRST_READY_LOGGED: bool = False

# Guards _FIRST_READY_LOGGED against worker-thread races. Pass loop
# in tm_fill_executor calls log_readiness_first_ready from the slow
# worker thread; the cfg toggle could fire from the main thread. The
# lock is uncontended in the steady state (one log emit, ever).
_LOCK = threading.Lock()


# ─── Toggle API ───────────────────────────────────────────────────────

def set_readiness_snapshot_enabled(enabled: bool) -> None:
    """Master toggle. App init reads `cfg['use_readiness_snapshot']`
    (default True) and calls this. Flag-off = the Part B log line
    stays silent; `get_readiness_snapshot` still works (it has no
    runtime side-effects, only the log emit does)."""
    global _READINESS_SNAPSHOT_ENABLED
    _READINESS_SNAPSHOT_ENABLED = bool(enabled)


def is_readiness_snapshot_enabled() -> bool:
    return _READINESS_SNAPSHOT_ENABLED


def reset_first_ready_dedup() -> None:
    """Test-only: clear the once-per-process "first ready logged"
    flag so audit fixtures can verify the dedup behavior across
    multiple calls. Not exposed to App init — production should never
    reset this within a single process."""
    global _FIRST_READY_LOGGED
    with _LOCK:
        _FIRST_READY_LOGGED = False


# ─── The aggregator ───────────────────────────────────────────────────

# Lanes we report. Order matters only for the log line readability.
_REPORTED_LANES = ('news_signals', 'daily_bars', 'filings',
                   'fundamentals')


def _safe_lane_progress(lane: str, scope_tickers) -> dict:
    """Wrap `tm_cache.get_fill_progress` so a missing module / broken
    lane / empty scope returns a graceful dict instead of raising
    out of the aggregator. Empty scope → pct None (no honest
    percentage to report)."""
    try:
        if not scope_tickers:
            return {'filled': 0, 'total': 0, 'pct': None}
        import tm_cache
        p = tm_cache.get_fill_progress(lane, set(scope_tickers))
        # get_fill_progress returns
        # {lane, scope_total, filled, unfilled, pct_complete}.
        # Re-shape to the snapshot's flatter contract.
        total = int(p.get('scope_total') or 0)
        filled = int(p.get('filled') or 0)
        pct = p.get('pct_complete')
        try:
            pct_f = float(pct) if pct is not None else None
        except (TypeError, ValueError):
            pct_f = None
        return {'filled': filled, 'total': total, 'pct': pct_f}
    except Exception:
        return {'filled': 0, 'total': 0, 'pct': None}


def _safe_pick_count(app) -> int:
    """Count of DISPLAYED picks across all paths in
    `recommend_cache`. Same table the Recommend dialog reads from
    (`_read_recommend_cache_picks` at tired_market.py:21364). One
    indexed COUNT — cheap. Defensive: missing db / closed conn /
    schema drift all → 0."""
    if app is None:
        return 0
    try:
        db = getattr(app, 'db', None)
        if db is None:
            return 0
        conn = getattr(db, 'conn', None)
        if conn is None:
            return 0
        row = conn.execute(
            "SELECT COUNT(*) FROM recommend_cache "
            "WHERE tier = 'displayed'"
        ).fetchone()
        if row is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError, IndexError):
            return 0
    except Exception:
        return 0


def _safe_priority_scope(app) -> set:
    """Read the pick-relevant ticker set via `tm_fill_executor.
    _priority_tickers_for_app`. Defensive: missing module / app=None
    / helper raising → empty set (aggregator then reports lanes with
    total=0 / pct=None instead of crashing)."""
    if app is None:
        return set()
    try:
        import tm_fill_executor
        helper = getattr(
            tm_fill_executor, '_priority_tickers_for_app', None)
        if helper is None:
            return set()
        out = helper(app)
        return set(out) if out else set()
    except Exception:
        return set()


def _news_priority_complete(app) -> bool:
    """Read the v.86 readiness boolean off the app. Missing attribute
    (pre-v.86 state / early startup race) → False, not crash."""
    if app is None:
        return False
    try:
        return bool(getattr(app, 'news_priority_slice_complete', False))
    except Exception:
        return False


def _compute_ready(snapshot: dict) -> bool:
    """The locked product rule, applied to an already-assembled
    snapshot. Kept as a separate function so tests can pin it without
    re-running the whole aggregator. Conservative — both arms must
    be True; either missing → False."""
    if not snapshot.get('news_priority_complete'):
        return False
    db_lane = (snapshot.get('lanes') or {}).get('daily_bars') or {}
    pct = db_lane.get('pct')
    if pct is None:
        return False
    try:
        return float(pct) >= 100.0
    except (TypeError, ValueError):
        return False


def get_readiness_snapshot(app) -> dict:
    """Read-only snapshot. Cheap (one priority-set computation + one
    `get_fill_progress` call per lane + one COUNT on recommend_cache).
    Never raises. Safe to call at any point in the app lifecycle,
    including before any fill completes or before the app is fully
    initialized.

    Returns:
        {
          'ready':                 bool,    # overall verdict
          'pick_count':            int,     # displayed picks total
          'news_priority_complete': bool,
          'lanes': {
            'daily_bars':   {'filled': N, 'total': M, 'pct': P|None},
            'news_signals': {'filled': N, 'total': M, 'pct': P|None},
            'filings':      {'filled': N, 'total': M, 'pct': P|None},
            'fundamentals': {'filled': N, 'total': M, 'pct': P|None},
          },
          'scope': 'pick_relevant',
        }
    """
    scope = _safe_priority_scope(app)
    lanes = {
        lane: _safe_lane_progress(lane, scope)
        for lane in _REPORTED_LANES
    }
    npc = _news_priority_complete(app)
    snapshot = {
        'ready': False,  # filled in by _compute_ready below
        'pick_count': _safe_pick_count(app),
        'news_priority_complete': npc,
        'lanes': lanes,
        'scope': 'pick_relevant',
    }
    snapshot['ready'] = _compute_ready(snapshot)
    return snapshot


# ─── Part B: first-ready log emit ─────────────────────────────────────

def _format_first_ready_line(snapshot: dict) -> str:
    """One-line readable summary of a ready snapshot. Format:
        '[readiness] picks ready — N current picks; daily_bars 100%, '
        'news priority complete; universe fill continues'
    The phrasing is HONEST: it says "picks ready," not "data complete"
    — the deep-universe tail is still filling in the background and
    we don't pretend otherwise."""
    pick_n = int(snapshot.get('pick_count') or 0)
    db = (snapshot.get('lanes') or {}).get('daily_bars') or {}
    db_pct = db.get('pct')
    db_str = (f"{int(round(db_pct))}%"
              if isinstance(db_pct, (int, float)) else "n/a")
    return (
        f"[readiness] picks ready — {pick_n} current pick(s); "
        f"daily_bars {db_str}, news priority complete; "
        f"universe fill continues"
    )


def log_readiness_first_ready(app, log_callback) -> bool:
    """Emit the Part B once-per-launch '[readiness] picks ready' log
    line if (and only if) the snapshot now reads ready AND we haven't
    already logged this launch. Returns True iff the line was emitted
    on this call.

    Designed to be called cheaply from the slow-fill pass loop — the
    pass already runs every few seconds; this call adds one snapshot
    read (cheap) + one boolean check. On the steady-state second
    call the dedup short-circuits before any snapshot work.

    Flag-off (`use_readiness_snapshot=False`) → silent no-op;
    `get_readiness_snapshot` still works for direct callers."""
    global _FIRST_READY_LOGGED
    # Dedup short-circuit BEFORE any snapshot work — keeps the hot
    # path free.
    if _FIRST_READY_LOGGED:
        return False
    if not _READINESS_SNAPSHOT_ENABLED:
        return False
    if not callable(log_callback):
        return False
    try:
        snap = get_readiness_snapshot(app)
    except Exception:
        return False
    if not snap.get('ready'):
        return False
    with _LOCK:
        # Re-check inside the lock to avoid double-log on a race.
        if _FIRST_READY_LOGGED:
            return False
        _FIRST_READY_LOGGED = True
    try:
        log_callback(_format_first_ready_line(snap), 'green')
    except Exception:
        # Don't unset the dedup on log failure — we tried, we don't
        # want to retry forever.
        pass
    return True
