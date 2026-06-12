"""tm_fill_executor.py — v4.15.0 Step 19 bulk cache fill executor.

Iterates the universe ticker-by-ticker, calls existing per-ticker fetchers
(which write to cache.db via Steps 2-14's side-effect taps), tracks progress,
transitions lanes to 'incremental' when fill is sufficient (~95% per
should_transition_to_incremental).

Single background thread, sequential per lane. Polite delays between fetches.
Interruptible at app close via a stop event.

Universe seeding: if cache.db.tickers is empty when the executor runs, it
constructs tm_discover.Universe and calls refresh() to fetch IWV (Russell 3000)
via iShares, then UPSERTs the result into cache.db.tickers.
"""

from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path


# Polite delays between per-ticker fetches. Conservative — Finnhub free tier is
# 60 req/min, Yahoo doesn't publish a limit but bursts get throttled.
_INTER_TICKER_DELAY_SECONDS = 1.2
_INTER_LANE_DELAY_SECONDS = 0.5

# v4.14.3.4 (2026-05-14): chunked phase 1 for daily_bars. yfinance's
# multi-ticker download() fetches many tickers per HTTP call — restoring
# the speed-of-a-batch-fetch that the original Tired Market used before
# the v4.15.0 cache rework standardized everything to per-ticker loops.
# 100 tickers per chunk is well within Yahoo's per-IP tolerance; 0.5s
# inter-chunk delay keeps us polite without artificially slow-walking.
_CHUNKED_DAILY_BARS_CHUNK_SIZE = 100
_CHUNKED_DAILY_BARS_FALLBACK_CHUNK_SIZE = 50  # used after one chunk error
_CHUNKED_INTER_CHUNK_DELAY_SECONDS = 0.5

# Progress log cadence (per lane, per N completed tickers).
_PROGRESS_LOG_EVERY_N_TICKERS = 25

# v4.15.0 Step 20: slow-mode pacing. Per-lane because daily_bars drives the
# most user-visible signal (analyzes), fundamentals shift quarterly so no rush,
# filings are bursty events that tolerate even pacing.
_SLOW_INTER_TICKER_DELAY_SECONDS = {
    'daily_bars':   8.0,   # ~7h to walk 3,000 tickers
    'fundamentals': 20.0,  # ~17h — quarterly cadence
    'filings':      15.0,  # ~13h — bursty events
}
# Rest between full passes when slow worker reaches end-of-list.
_SLOW_INTER_PASS_REST_SECONDS = 3600

# v4.14.5.44-fundamentals-coldfill: the 20s fundamentals pace above is a
# STEADY-STATE cadence (quarterly data, no rush). On a one-time COLD/catch-up
# fill (many tickers unfilled) it wrongly trickles ~2564 tickers over ~14h.
# For the cold fill we DROP the app-side idle and let EDGAR's OWN limiter
# (tm_data_adapter_edgar._MIN_INTERVAL_SEC = 0.5s = 2/sec, well under SEC's
# 10/sec cap) govern the rate — fast but still polite, NO SEC hammer, no
# contact email required. Steady-state (few unfilled) KEEPS the polite 20s
# cadence so we never re-fetch quarterly fundamentals at full speed forever.
_FUND_COLDFILL_UNFILLED_THRESHOLD = 200   # more unfilled than this = cold/catch-up
_FUND_COLDFILL_DELAY_SECONDS = 0.0        # 0 app-idle; EDGAR's 0.5s limiter paces it

# v4.14.5.67-filings-coldfill: same carve-out for FILINGS, mirroring the
# v4.14.5.44 fundamentals fix. Filings was the last lane still doing the
# polite 15s app-side wait between EVERY ticker on a cold catch-up — at
# ~3,800 unfilled tickers that's ~16h of mostly-idle wall-clock (the
# network sits idle ~97% of the time; EDGAR itself responds in ~0.5s).
# In cold mode we drop the app idle to 0.0 and let EDGAR's own
# tm_data_adapter_edgar._MIN_INTERVAL_SEC=0.5 limiter (2/sec, well
# under SEC's 10/sec ceiling) pace the rate. SEC-SAFE — that limiter
# is the backstop and is NOT touched. Steady-state (few unfilled)
# keeps the polite 15s cadence so we never re-fetch filings at full
# speed forever.
_FILINGS_COLDFILL_UNFILLED_THRESHOLD = 200
_FILINGS_COLDFILL_DELAY_SECONDS = 0.0

# v4.14.5.48-fill-multistream: per-ticker pacing for the Yahoo-deep fundamentals
# STREAM (the concurrent filler alongside the authoritative EDGAR stream). Yahoo
# has no global SEC-style limiter like EDGAR's _polite_wait, so we self-pace this
# stream to ~1/sec (~60/min) — fast but polite to Yahoo, and modest enough that
# it won't spike 429s. EDGAR's own 0.5s limiter still paces the EDGAR stream.
_FUND_YAHOO_STREAM_DELAY_SECONDS = 1.0

# v4.14.5.75-safe-coldfill-speedups (Lever 3): cold-fill news pacing.
# 1.2s per ticker = ~50/min — well under Finnhub's 60/min free-tier cap
# (deep_news_scan fires Finnhub once per ticker). RSS, Yahoo-news, and
# Google News have no published caps but are courteous. Increase only if
# adaptive pacing observes sustained zero 429s.
_COLD_NEWS_FILL_DELAY_SECONDS = 1.2
# Progress log cadence specifically for the cold-news lane.
_COLD_NEWS_LOG_EVERY_N = 50

# v4.14.5.86-news-progress-clarity: cadence for the BACKGROUND-TAIL
# phase only (after the pick-relevant slice has completed). Tail
# tickers are deep-universe names nobody's currently looking at, so
# the log noise should be ~10× lower than Phase-1 priority-slice
# progress — same fetch rate, just quieter logging. Phase-1
# continues to use the existing _COLD_NEWS_LOG_EVERY_N cadence.
_COLD_NEWS_TAIL_LOG_EVERY_N = 500

# v4.14.5.86-news-progress-clarity: master toggle. False → exact
# pre-v.86 single-line cold-fill logging (one headline + every-50
# progress lines for the whole pass). Flag-on splits into two phases
# with a "ready" boundary line; readiness state (`news_priority_
# slice_complete` + count) is set on the app object so a future
# Teacher close-message can read it without re-deriving.
_NEWS_PROGRESS_CLARITY_ENABLED = True


def set_news_progress_clarity_enabled(enabled: bool) -> None:
    """Master toggle for the v.86 two-phase news cold-fill logging.
    Display-only; never changes fetch logic or rate. Flag-off →
    legacy single-line headline + every-50 progress."""
    global _NEWS_PROGRESS_CLARITY_ENABLED
    _NEWS_PROGRESS_CLARITY_ENABLED = bool(enabled)


def is_news_progress_clarity_enabled() -> bool:
    return _NEWS_PROGRESS_CLARITY_ENABLED


# ─── Diagnostic logging helper ───────────────────────────────────────────────
#
# Mirrors _picker_log (tm_top_ai_picker.py), _surface_log
# (tm_teacher_intercept.py), and the queue runner's _safe_log. Used to
# replace bare `except: pass` traps so silent fetcher failures
# (yfinance throttling, network blips) surface in the activity log.
# May 13 2026: this helper landed alongside the bare-except replacement
# in the inter-ticker loop. The bare-except hid 99%+ of slow fill's
# fetcher failures for the prior session.

def _fill_log(log_callback, msg, color='amber'):
    """Best-effort log via the worker's log_callback. Falls back to
    stdout if log_callback is None or raises. Color defaults to amber
    (anomaly) — pass 'muted' for normal info."""
    try:
        if callable(log_callback):
            log_callback(msg, color)
            return
    except Exception:
        pass
    try:
        print(msg)
    except Exception:
        pass


def _format_local_time(dt) -> str:
    """Format a datetime as 'h:MMam/pm' for user-readable rest logs.
    Falls back to ISO if formatting fails."""
    try:
        # %#I on Windows, %-I on POSIX. Try Windows first; fall back.
        try:
            return dt.strftime('%#I:%M%p').lower()
        except Exception:
            return dt.strftime('%-I:%M%p').lower()
    except Exception:
        try:
            return dt.isoformat(timespec='minutes')
        except Exception:
            return str(dt)


def _read_rest_until(app):
    """Return a datetime if cfg has a future slow_fill_rest_until,
    otherwise None. Past timestamps return None (rest already
    elapsed). May 13 2026: replaces the in-memory-only stop_event.wait
    that lost track of rest progress on every app restart."""
    if app is None:
        return None
    try:
        from datetime import datetime as _dt
        raw = app.cfg.get('slow_fill_rest_until')
        if not raw:
            return None
        end = _dt.fromisoformat(raw)
        if end <= _dt.now():
            return None
        return end
    except Exception:
        return None


def _write_rest_until(app, end_dt) -> None:
    """Stamp app.cfg['slow_fill_rest_until'] in ISO format. Best-effort
    — cfg write failure must NOT crash the worker."""
    if app is None:
        return
    try:
        app.cfg['slow_fill_rest_until'] = end_dt.isoformat(
            timespec='seconds')
    except Exception:
        pass


def _clear_rest_until(app) -> None:
    """Drop the persisted rest stamp once it has elapsed (or once a
    new pass is starting). Keeps cfg tidy."""
    if app is None:
        return
    try:
        if 'slow_fill_rest_until' in app.cfg:
            del app.cfg['slow_fill_rest_until']
    except Exception:
        pass


# ─── v4.14.5.75-safe-coldfill-speedups (Lever 2): pick-relevant ordering ──
#
# Each lane previously filled `sorted(unfilled)` (pure alphabetical). On the
# 7,201-ticker universe that means AAPL-shaped picks become useful only when
# A* names happen to be reached. We reorder unfilled so the names most likely
# to PRODUCE a visible pick — path seed pools + holdings + watchlist + the
# tickers currently displayed in recommend_cache — fill FIRST, with the long
# tail alphabetical after. Same total wall-clock; picks usable in minutes
# instead of hours. Pure ordering — zero rate-limit impact.
#
# Inputs are intentionally defensive: any source that can't be read (no
# holdings yet, no watchlist, no DB) yields an empty set rather than raises.

def _priority_tickers_for_app(app) -> set:
    """Return the set of tickers that should fill FIRST: path seed pools +
    holdings + watchlist + currently-displayed recommend_cache picks.

    Strictly best-effort: every source is guarded so a missing accessor /
    closed DB / pre-init app just shrinks the set rather than raising.
    Returns an empty set on total failure — the caller falls back to plain
    alphabetical order.
    """
    out: set = set()
    if app is None:
        return out
    # 1. Path seed pools (canonical "what counts as a real pick candidate").
    try:
        import tm_path_candidate_pools as _pcp
        for name in ('_SLOW_SAFE_SEED', '_MODERATE_SEED'):
            seed = getattr(_pcp, name, None)
            if seed:
                out.update(str(t).upper() for t in seed)
    except Exception:
        pass
    # 2. Holdings (active positions the user already owns).
    try:
        hs = getattr(app, '_holdings_state', None) or {}
        mgr = hs.get('mgr') if isinstance(hs, dict) else None
        if mgr is not None:
            for h in (getattr(mgr, 'holdings', None) or []):
                try:
                    tk = str(h.get('ticker', '')).upper()
                    if tk:
                        out.add(tk)
                except Exception:
                    continue
    except Exception:
        pass
    # 3. Watchlist (tickers the user flagged for analysis).
    try:
        wl = getattr(app, '_watchlist', None)
        for entry in (getattr(wl, 'tickers', None) or []):
            try:
                tk = str(entry.get('ticker', '')).upper()
                if tk:
                    out.add(tk)
            except Exception:
                continue
    except Exception:
        pass
    # 4. Currently-displayed recommend_cache picks (the names a user is
    # looking at RIGHT NOW — empty on first launch, populated thereafter).
    try:
        db = getattr(app, 'db', None)
        conn = getattr(db, 'conn', None) if db is not None else None
        lock = getattr(db, 'lock', None) if db is not None else None
        if conn is not None:
            if lock is not None:
                with lock:
                    cur = conn.execute(
                        "SELECT DISTINCT ticker FROM recommend_cache "
                        "LIMIT 500")
                    for (tk,) in cur.fetchall() or []:
                        if tk:
                            out.add(str(tk).upper())
            else:
                cur = conn.execute(
                    "SELECT DISTINCT ticker FROM recommend_cache "
                    "LIMIT 500")
                for (tk,) in cur.fetchall() or []:
                    if tk:
                        out.add(str(tk).upper())
    except Exception:
        pass
    return out


def _priority_sorted(unfilled, app) -> list:
    """Return `unfilled` as a list, priority tickers first (alphabetical
    within the priority block), then the alphabetical long tail. Falls
    back to plain `sorted(unfilled)` if priority resolution fails or
    returns an empty set."""
    try:
        priority = _priority_tickers_for_app(app)
    except Exception:
        priority = set()
    if not priority:
        return sorted(unfilled)
    head = sorted(t for t in unfilled if t in priority)
    tail = sorted(t for t in unfilled if t not in priority)
    return head + tail


# ─── Chunked phase 1 for daily_bars (v4.14.3.4) ──────────────────────
#
# Caller supplies a chunked_fetcher with signature:
#   chunked_fetcher(ticker_list: list[str]) -> set[str]
# Returns the set of tickers it successfully filled (a subset of input).
# The fetcher is responsible for the actual yf.download + cache write
# side effects; the executor doesn't know about yfinance.
#
# Why module-level: keeps the executor data-source-agnostic. Per-ticker
# fetchers in `fetchers` dict are caller-supplied today; chunked
# fetchers in `chunked_fetchers` dict follow the same pattern. The
# helper below orchestrates the chunked phase (chunking, progress
# logging, exception fallback) — the per-chunk work itself lives in
# the caller (tired_market.py:_v415_chunked_fetch_daily_bars).


def _run_chunked_phase(lane: str, unfilled_tickers, chunked_fetcher,
                         log_callback, stop_event, lane_prefix: str = 'bulk'):
    """Run a chunked phase 1 fetch over `unfilled_tickers` using the
    caller-supplied chunked_fetcher. Returns the set of tickers
    successfully filled (a subset of the input).

    On chunk-level exception: log amber, try once more at the smaller
    fallback chunk size, then if that still raises, log amber and
    return whatever's been filled so far — the caller falls back to
    per-ticker iteration for the rest.

    Respects stop_event between chunks. Polite 0.5s delay between
    chunks so we don't hammer the upstream.
    """
    filled = set()
    if not chunked_fetcher or not unfilled_tickers:
        return filled

    tickers = list(unfilled_tickers)
    total = len(tickers)
    if total == 0:
        return filled

    _fill_log(
        log_callback,
        f"[{lane_prefix}] {lane} phase 1: chunked fetch starting "
        f"({total} tickers, ~{_CHUNKED_DAILY_BARS_CHUNK_SIZE} per "
        f"chunk)",
        'accent')

    chunk_size = _CHUNKED_DAILY_BARS_CHUNK_SIZE
    fallback_used = False
    i = 0
    chunks_done = 0
    while i < total:
        if stop_event is not None and stop_event.is_set():
            _fill_log(
                log_callback,
                f"[{lane_prefix}] {lane} phase 1: stopped at "
                f"chunk {chunks_done + 1} ({len(filled)}/{total} "
                f"filled so far)",
                'amber')
            return filled

        chunk = tickers[i: i + chunk_size]
        _t0 = time.time()
        try:
            got = chunked_fetcher(list(chunk))
            if got is None:
                got = set()
            else:
                got = set(got)
            filled.update(got)
            i += len(chunk)
            chunks_done += 1
            pct = (len(filled) / total) * 100.0
            _fill_log(
                log_callback,
                f"[{lane_prefix}] {lane} phase 1: chunked fetch "
                f"{len(filled)}/{total} ({pct:.1f}%)",
                'muted')
            # v4.14.5.76-adaptive-lane-pacing: feed Yahoo-price outcome
            # to the adaptive controller. The daily_bars chunked phase
            # is the load-bearing Yahoo entry point; a clean chunk =
            # clean window signal for the controller's tightening logic.
            # NOTE: an empty-but-no-exception chunk can mean
            # `_yahoo_price_in_cooldown` skipped — that's already
            # tracked by `tm_data_providers.Registry.record_failure`
            # elsewhere; reporting it here would double-count. So we
            # only report SUCCESS or EXCEPTION outcomes.
            if lane == 'daily_bars':
                try:
                    import tm_lane_pacing as _lp
                    _lp.record_outcome(
                        'yahoo_price', success=True, was_429=False,
                        latency=max(0.0, time.time() - _t0))
                except Exception:
                    pass
        except Exception as e:
            # v4.14.5.76-adaptive-lane-pacing: classify the exception
            # as 429-shape or generic before deciding whether to feed
            # the back-off signal. tm_data_adapter_yahoo's
            # `_is_yfinance_rate_limit` already knows yfinance's
            # YFRateLimitError + 429-in-message detection (the same
            # check `yahoo_history` uses at line 947).
            if lane == 'daily_bars':
                _was_429 = False
                try:
                    import tm_data_adapter_yahoo as _tday
                    _was_429 = bool(_tday._is_yfinance_rate_limit(e))
                except Exception:
                    pass
                try:
                    import tm_lane_pacing as _lp
                    _lp.record_outcome(
                        'yahoo_price', success=False, was_429=_was_429,
                        latency=max(0.0, time.time() - _t0))
                except Exception:
                    pass
            if not fallback_used:
                _fill_log(
                    log_callback,
                    f"[{lane_prefix}] {lane} phase 1: chunk failed "
                    f"({type(e).__name__}: {e}) — retrying once at "
                    f"smaller chunk size "
                    f"{_CHUNKED_DAILY_BARS_FALLBACK_CHUNK_SIZE}",
                    'amber')
                chunk_size = _CHUNKED_DAILY_BARS_FALLBACK_CHUNK_SIZE
                fallback_used = True
                # Re-attempt this slice at the smaller size on the
                # next loop iteration (don't advance i).
                continue
            _fill_log(
                log_callback,
                f"[{lane_prefix}] {lane} phase 1: chunk failed again "
                f"({type(e).__name__}: {e}) — abandoning chunked "
                f"phase, will fall back to per-ticker for "
                f"{total - len(filled)} remaining",
                'amber')
            return filled

        # Polite delay between chunks. Respect stop event during the
        # wait so app close exits within ~0.5s rather than the whole
        # phase.
        if i < total and stop_event is not None:
            stop_event.wait(_CHUNKED_INTER_CHUNK_DELAY_SECONDS)
        elif i < total:
            time.sleep(_CHUNKED_INTER_CHUNK_DELAY_SECONDS)

    if filled:
        _fill_log(
            log_callback,
            f"[{lane_prefix}] {lane} phase 1: chunked fetch complete "
            f"({len(filled)}/{total} tickers filled, "
            f"{total - len(filled)} need per-ticker fallback)",
            'accent')
    return filled


_executor_lock = threading.Lock()
_executor_state = {
    'running': False,
    'mode': None,          # v4.15.0 Step 20: 'bulk' | 'slow' | None when idle
    'thread': None,
    'stop_event': None,
    'started_at': None,
    'lane_progress': {},
    'current_ticker': None,
    'current_lane': None,
}


def is_running() -> bool:
    """True if a bulk fill is currently in progress."""
    with _executor_lock:
        return _executor_state['running']


def get_progress_snapshot() -> dict:
    """Snapshot of current progress. Safe to call from any thread."""
    with _executor_lock:
        running = _executor_state['running']
        mode = _executor_state.get('mode')
        started_at = _executor_state['started_at']
        current_lane = _executor_state['current_lane']
        current_ticker = _executor_state['current_ticker']
        lane_progress = {k: dict(v) for k, v in _executor_state['lane_progress'].items()}

    elapsed = int(time.time() - started_at) if started_at else 0
    return {
        'running': running,
        'mode': mode,
        'started_at': started_at,
        'elapsed_seconds': elapsed,
        'current_lane': current_lane,
        'current_ticker': current_ticker,
        'lane_progress': lane_progress,
    }


def stop(timeout_sec: float = 0.0) -> None:
    """Signal the worker thread to stop.

    timeout_sec=0.0 (default, legacy behavior): returns immediately; the
    thread exits at its next ticker boundary on its own time.

    timeout_sec>0: blocks up to timeout_sec waiting for the worker
    thread to actually exit. Used by v4.14.3.3's mode-switch flow in
    tired_market.py (_v415_stop_running_fill_for_mode_switch) so the
    caller can start a new fill on a clean slate. Worker checks the
    stop event at every ticker boundary AND during inter-ticker
    delays, so typical join times are sub-second."""
    with _executor_lock:
        ev = _executor_state.get('stop_event')
        worker_thread = _executor_state.get('thread')
    if ev is not None:
        ev.set()
    if timeout_sec > 0 and worker_thread is not None:
        try:
            if worker_thread.is_alive():
                worker_thread.join(timeout=timeout_sec)
        except Exception:
            pass


def _daily_bars_max_age(app, lane: str):
    """v4.14.5.6: max_age_days to pass to get_unfilled_tickers for the
    daily_bars lane, honoring cfg['use_freshness_fill'] (default True).
    Returns None for other lanes or when the flag is off (legacy
    presence-only behavior)."""
    if lane != 'daily_bars':
        return None
    try:
        if not bool(getattr(app, 'cfg', {}).get(
                'use_freshness_fill', True)):
            return None
    except Exception:
        pass
    import tm_cache  # local import: matches this module's convention
    return tm_cache.DAILY_BARS_MAX_AGE_DAYS


def start_bulk_fill(
    choices: dict,
    fetchers: dict,
    log_callback=None,
    on_lane_complete=None,
    on_finished=None,
    universe_source: str = 'iwv',
    chunked_fetchers: dict | None = None,
) -> bool:
    """Start a bulk fill in a background thread. Returns True if started,
    False if a fill is already in flight.

    choices: user's Choices dict (price ranges + style). Drives scope per lane.
    fetchers: {lane_name: callable(ticker) -> any}. Each callable fetches +
              caches one ticker for that lane (side-effect via cache taps).
              Provided by caller (typically tired_market.py) so this module
              doesn't import everything and avoids circular dependencies.
    log_callback: optional callable(message, color='muted'). Caller marshals
                  thread-safety (e.g., root.after for Tk).
    on_lane_complete: optional callable(lane, progress_dict). Fires per lane.
    on_finished: optional callable(summary_dict). Fires at worker exit.
    """
    import tm_cache

    with _executor_lock:
        if _executor_state['running']:
            return False
        _executor_state['running'] = True
        _executor_state['mode'] = 'bulk'
        _executor_state['started_at'] = time.time()
        _executor_state['stop_event'] = threading.Event()
        _executor_state['lane_progress'] = {}
        _executor_state['current_ticker'] = None
        _executor_state['current_lane'] = None
        stop_event = _executor_state['stop_event']

    def _worker():
        try:
            try:
                _seed_universe_if_needed(
                    log_callback, stop_event, chosen_universe=universe_source)
            except Exception as e:
                if log_callback:
                    log_callback(f"[bulk] Universe seed failed: {e}", 'amber')

            for lane in tm_cache.BULK_FILLABLE_LANES:
                if stop_event.is_set():
                    if log_callback:
                        log_callback(
                            f"[bulk] Stopped by user request before {lane}",
                            'amber')
                    break

                fetcher = fetchers.get(lane)
                if fetcher is None:
                    if log_callback:
                        log_callback(
                            f"[bulk] No fetcher for {lane}, skipping",
                            'amber')
                    continue

                # v4.14.3.3 (2026-05-14): pass None, NOT user choices.
                # Same architectural bug + same one-line fix shape as
                # the slow-fill path at line 652 (May 13 fix) and the
                # queue runner's _build_candidate_shortlist
                # (tm_queue_runner.py:446). Choices is a DISPLAY-TIME
                # filter (price range, sectors); the bulk fill's job
                # is to populate the full universe cache so display-
                # time filters have data to filter from. Before this
                # fix, get_scope_tickers(lane, choices) excluded
                # tickers without a cached daily_bars price — making
                # scope ≈ already-filled set, making unfilled ≈ 0,
                # triggering _maybe_transition immediately and
                # silently flipping lane_config to incremental
                # without any real work done.
                scope = tm_cache.get_scope_tickers(lane, None)
                if not scope:
                    if log_callback:
                        log_callback(
                            f"[bulk] {lane}: empty scope, skipping",
                            'muted')
                    continue

                unfilled = tm_cache.get_unfilled_tickers(
                    lane, scope,
                    max_age_days=_daily_bars_max_age(app, lane))
                if not unfilled:
                    if log_callback:
                        log_callback(
                            f"[bulk] {lane}: already filled "
                            f"({len(scope)} tickers)", 'muted')
                    _maybe_transition(lane, scope, log_callback)
                    continue

                # v4.14.3.4 (2026-05-14): chunked phase 1 for lanes
                # that have a chunked_fetcher registered (today: just
                # daily_bars). Runs BEFORE per-ticker iteration so the
                # bulk pass front-loads its cheapest, highest-leverage
                # work — daily_bars is the queue runner's only gating
                # dependency, so phase-1 chunked fill is what makes
                # the candidate pool usable in minutes instead of
                # hours. Per-ticker iteration below picks up any
                # tickers chunked phase didn't get (timeouts,
                # individual-ticker errors).
                _cf = (chunked_fetchers or {}).get(lane)
                if _cf is not None:
                    chunk_filled = _run_chunked_phase(
                        lane, unfilled, _cf, log_callback, stop_event,
                        lane_prefix='bulk')
                    if chunk_filled:
                        # Recompute unfilled after the chunked phase
                        # so per-ticker only retries stragglers.
                        unfilled = tm_cache.get_unfilled_tickers(
                            lane, scope,
                            max_age_days=_daily_bars_max_age(app, lane))
                        if not unfilled:
                            if log_callback:
                                log_callback(
                                    f"[bulk] {lane}: fully filled by "
                                    f"chunked phase ({len(scope)} "
                                    f"tickers)", 'accent')
                            _maybe_transition(
                                lane, scope, log_callback)
                            if on_lane_complete:
                                try:
                                    on_lane_complete(
                                        lane,
                                        tm_cache.get_fill_progress(
                                            lane, scope))
                                except Exception:
                                    pass
                            if not stop_event.is_set():
                                stop_event.wait(
                                    _INTER_LANE_DELAY_SECONDS)
                            continue

                with _executor_lock:
                    _executor_state['current_lane'] = lane
                    _executor_state['lane_progress'][lane] = {
                        'scope_total': len(scope),
                        'filled': len(scope) - len(unfilled),
                        'started_at': time.time(),
                    }

                if log_callback:
                    log_callback(
                        f"[bulk] {lane}: filling {len(unfilled)} of "
                        f"{len(scope)} tickers", 'accent')

                tickers_done = 0
                tickers_failed = 0
                baseline_filled = len(scope) - len(unfilled)

                # v4.14.5.75-safe-coldfill-speedups (Lever 2): bulk-fill
                # path also benefits — but bulk's start_bulk_fill doesn't
                # receive `app`, so we pass None and degrade to plain
                # alphabetical. The slow-fill path (the primary cold-fill
                # entry today) gets the full priority ordering at lines
                # 994 + 1090.
                for ticker in _priority_sorted(unfilled, None):
                    if stop_event.is_set():
                        if log_callback:
                            log_callback(
                                f"[bulk] {lane}: stopped at {ticker} "
                                f"({tickers_done} filled, "
                                f"{tickers_failed} failed this run)",
                                'amber')
                        break

                    with _executor_lock:
                        _executor_state['current_ticker'] = ticker

                    try:
                        fetcher(ticker)
                        tickers_done += 1
                    except Exception:
                        tickers_failed += 1

                    # Update progress state with local counter — cheap.
                    with _executor_lock:
                        if lane in _executor_state['lane_progress']:
                            _executor_state['lane_progress'][lane]['filled'] = (
                                baseline_filled + tickers_done)

                    # Periodic log line (every Nth completed ticker).
                    if (tickers_done > 0
                            and tickers_done % _PROGRESS_LOG_EVERY_N_TICKERS == 0
                            and log_callback):
                        pct = (baseline_filled + tickers_done) / len(scope) * 100.0
                        log_callback(
                            f"[bulk] {lane}: {baseline_filled + tickers_done}"
                            f"/{len(scope)} ({pct:.1f}%) — last: {ticker}",
                            'muted')

                    if not stop_event.is_set():
                        stop_event.wait(_INTER_TICKER_DELAY_SECONDS)

                if log_callback:
                    log_callback(
                        f"[bulk] {lane}: done — {tickers_done} filled, "
                        f"{tickers_failed} failed",
                        'accent' if tickers_done > 0 else 'amber')

                _maybe_transition(lane, scope, log_callback)

                if on_lane_complete:
                    try:
                        on_lane_complete(
                            lane, tm_cache.get_fill_progress(lane, scope))
                    except Exception:
                        pass

                if not stop_event.is_set():
                    stop_event.wait(_INTER_LANE_DELAY_SECONDS)

            elapsed = int(time.time() - _executor_state['started_at'])
            if log_callback:
                log_callback(
                    f"[bulk] Fill complete in {elapsed}s", 'accent')

            if on_finished:
                try:
                    on_finished({
                        'elapsed_seconds': elapsed,
                        'lane_progress': dict(_executor_state['lane_progress']),
                        'stopped_by_user': stop_event.is_set(),
                    })
                except Exception:
                    pass
        except Exception:
            if log_callback:
                log_callback(
                    f"[bulk] Worker thread crashed: "
                    f"{traceback.format_exc()[:300]}",
                    'amber')
        finally:
            with _executor_lock:
                _executor_state['running'] = False
                _executor_state['mode'] = None
                _executor_state['current_ticker'] = None
                _executor_state['current_lane'] = None

    thread = threading.Thread(
        target=_worker, name='tm_fill_executor', daemon=True)
    with _executor_lock:
        _executor_state['thread'] = thread
    thread.start()
    return True


def _maybe_transition(lane: str, scope_tickers, log_callback) -> None:
    """If lane fill >=95%, flip lane_config to 'incremental'."""
    import tm_cache
    try:
        if tm_cache.should_transition_to_incremental(lane, scope_tickers):
            tm_cache.set_lane_config(lane, tm_cache.FILL_MODE_INCREMENTAL)
            if log_callback:
                log_callback(
                    f"[bulk] {lane}: transitioned to incremental mode",
                    'accent')
    except Exception:
        pass


# ─── Universe seed bug surface (May 2026) ────────────────────────────
#
# Universe seed guard removed May 2026: previously this function bailed
# out when tickers > 100, meaning the first universe seeded was
# permanently the only one. Universe switches in the UI wrote cfg
# but never affected actual fill scope. Removed because
# tm_cache.upsert_tickers already handles dedup at the DB level —
# calling seed multiple times with different universes naturally
# produces the union. Future contributors: do NOT re-add an "already
# populated" early-return here. See sibling audit comments in
# tm_queue_runner.py and tm_top_ai_picker.py.

def _seed_universe_if_needed(log_callback, stop_event=None,
                                chosen_universe: str = 'iwv') -> None:
    """Populate cache.db.tickers from tm_discover.Universe.

    chosen_universe: tm_discover source key (e.g. 'iwv', 'ivv', 'iwb', 'iwm',
    'dow', 'nasdaq100', 'itot'). v4.15.0 Step 22a wires the picker's universe
    selection through to here.

    May 13 2026: this function previously bailed out when tickers > 100,
    meaning the FIRST universe seeded was permanently the only one. The
    picker would write cfg['v415_universe'] but the cache scope never
    updated. Removed because tm_cache.upsert_tickers handles dedup at
    the DB level — calling seed multiple times with different universes
    naturally produces the union (Russell 3000 contains S&P 500, etc.,
    and overlaps just refresh last_updated). Future contributors:
    do NOT re-add an "already populated" early-return here. If a user
    wants to ABANDON old universes and start fresh, that's a separate
    explicit-reset path (currently only achievable by wiping the
    tickers table manually). See BUILT.md May 13 entries.
    """
    import tm_cache

    if stop_event is not None and stop_event.is_set():
        return

    if log_callback:
        log_callback(
            f"[bulk] Seeding universe from {chosen_universe!r}...",
            'accent')

    try:
        universe_tickers = _fetch_universe_tickers(chosen_universe)
    except Exception as e:
        if log_callback:
            log_callback(f"[bulk] Universe seed unavailable: {e}", 'amber')
        return

    if not universe_tickers:
        if log_callback:
            log_callback("[bulk] Universe seed returned empty", 'amber')
        return

    now_iso = tm_cache.iso_now()
    rows = []
    for t in universe_tickers:
        if not t or not isinstance(t, str):
            continue
        sym = t.strip().upper()
        if not sym:
            continue
        rows.append({'ticker': sym, 'last_updated': now_iso})

    if not rows:
        if log_callback:
            log_callback(
                "[bulk] Universe seed produced no valid rows", 'amber')
        return

    try:
        tm_cache.upsert_tickers(rows)
    except Exception as e:
        if log_callback:
            log_callback(
                f"[bulk] Universe seed write failed: {e}", 'amber')
        return

    if log_callback:
        log_callback(
            f"[bulk] Seeded {len(rows)} universe tickers", 'accent')


def _fetch_universe_tickers(universe_source: str = 'iwv') -> list:
    """v4.15.0 Step 22a: Construct tm_discover.Universe for the given source,
    fetch if the disk cache is empty, return the ticker list.

    universe_source: a key in tm_discover.Universe.SOURCES — 'iwv', 'ivv',
    'iwb', 'iwm', 'dow', 'nasdaq100', 'itot'. The picker passes the friendly
    label's mapped value (e.g., 'sp500' → 'ivv') so this function only sees
    real source keys.

    Calls _fetch_one(source) directly instead of refresh() to avoid pulling
    iwv+ivv+iwb every time the user picked a different universe."""
    import tm_discover
    import tm_cache

    data_dir = tm_cache.CACHE_DB_PATH.parent
    try:
        universe = tm_discover.Universe(
            data_dir, current_source=universe_source)
    except Exception as e:
        raise RuntimeError(f"Universe construction failed: {e}")

    tickers = list(universe.tickers)
    if tickers:
        return tickers

    # Cache empty — fetch this specific source. IWM is computed from IWV - IWB
    # so refresh() is the only path that produces an IWM list; everything
    # else can be fetched via _fetch_one.
    try:
        if universe_source == 'iwm':
            universe.refresh(log_fn=None)
        else:
            universe._fetch_one(universe_source, log_fn=None)
    except Exception as e:
        raise RuntimeError(f"Universe refresh failed: {e}")

    return list(universe.tickers)


# ─── v4.15.0 Step 20: Slow fill ──────────────────────────────────────────────
#
# Slow fill bug surface from May 2026 — preserve these fixes:
#
# 1. Don't pass user `choices` to tm_cache.get_scope_tickers in the pass
#    loop. Choices are a DISPLAY-TIME filter (price range, sectors).
#    Slow fill's purpose is to populate the full universe cache so the
#    display-time filter has data to filter from. Passing choices here
#    knocked scope to ~0 for narrow filters and dropped fill rate from
#    ~50/hour to ~6/day. Same architectural bug + same one-line fix
#    shape as the queue runner's _build_candidate_shortlist (see
#    tm_queue_runner.py:410).
#
# 2. Persist rest end-time to cfg['slow_fill_rest_until']. The prior
#    in-memory-only stop_event.wait reset on every app restart, so a
#    restart 5 min after a pass completed effectively delayed the next
#    pass by an hour. _read_rest_until / _write_rest_until / _clear_rest_until
#    handle the persistence; the worker honors a prior-session rest at
#    entry and stamps a new one before each pass-end wait.
#
# 3. Skip the long rest if a pass did zero work. Prior code slept the
#    full 3600s after no-op passes (lanes fully filled or scope-empty),
#    locking out an hour of background fill for no reason. Now: 60s
#    short-rest if tickers_done_this_pass == 0, full 3600s otherwise.
#
# 4. Don't `except: pass` in the inter-ticker fetcher loop. Use _fill_log
#    instead. The bare except hid yfinance throttling and every other
#    fetcher failure for hours — pass-complete logs lied about what
#    actually happened.
#
# All four bugs landed fixes May 13 2026. See BUILT.md May 13 entries
# and the audit-pattern comments in tm_queue_runner.py and
# tm_top_ai_picker.py for the broader "audit at integration boundary,
# not above it" pattern.

def _run_slow_lane(lane, pass_count, app, fetchers, chunked_fetchers,
                   log_callback, stop_event):
    """Fill ONE slow-fill lane for one pass; return the count of tickers
    filled (chunked phase + per-ticker).

    v4.14.5.47-fill-wal-concurrent-start: factored out of start_slow_fill's
    worker so the fundamentals lane can run on its OWN thread concurrent with
    daily_bars during the cold fill (pass 1). For a single lane the behaviour
    is identical to the prior inline body. Safe to run two lanes at once: the
    sources are independent (daily_bars=Yahoo, fundamentals=EDGAR-first deep
    chain), they write different cache.db tables under short WAL transactions,
    and every _executor_state write is either per-lane (lane_progress[lane]) or
    a cosmetic last-activity pointer (current_lane/current_ticker, which have
    no consumers). Honors stop_event at entry and at every ticker boundary."""
    import tm_cache
    done = 0
    if stop_event.is_set():
        return done

    fetcher = fetchers.get(lane)
    if fetcher is None:
        return done

    # May 13 2026 fix: pass None, NOT user choices — slow fill populates the
    # FULL universe cache; choices is a display-time filter.
    scope = tm_cache.get_scope_tickers(lane, None)
    if not scope:
        return done

    unfilled = tm_cache.get_unfilled_tickers(
        lane, scope, max_age_days=_daily_bars_max_age(app, lane))
    if not unfilled:
        # Lane fully covered for this pass — quiet skip.
        return done

    # v4.14.3.4: chunked phase 1 for slow fill — pass-1 only.
    if pass_count == 1:
        _cf = (chunked_fetchers or {}).get(lane)
        if _cf is not None:
            # v4.14.5.50-cleanup: user-visible prefix reads [data] (consistent
            # with the rest of the fill log); the internal 'slow' mode key /
            # _SLOW_* constants are unchanged — display-only.
            # v4.14.5.75-safe-coldfill-speedups (Lever 2): pass the
            # unfilled set in priority order so the daily_bars chunked
            # phase fills pick-relevant names in its FIRST chunk(s) —
            # the queue runner gates on daily_bars, so priority chunks
            # unblock picks within minutes.
            chunk_filled = _run_chunked_phase(
                lane, _priority_sorted(unfilled, app), _cf,
                log_callback, stop_event, lane_prefix='data')
            if chunk_filled:
                done += len(chunk_filled)
                unfilled = tm_cache.get_unfilled_tickers(
                    lane, scope,
                    max_age_days=_daily_bars_max_age(app, lane))
                if not unfilled:
                    return done

    delay = _SLOW_INTER_TICKER_DELAY_SECONDS.get(lane, 15.0)
    # v4.14.5.44-fundamentals-coldfill: on a COLD/catch-up fundamentals fill
    # (many unfilled), drop the steady-state idle — EDGAR's own 0.5s limiter
    # still governs the rate (SEC-safe). Steady-state keeps the polite cadence.
    if (lane == 'fundamentals'
            and len(unfilled) > _FUND_COLDFILL_UNFILLED_THRESHOLD):
        delay = _FUND_COLDFILL_DELAY_SECONDS
    # v4.14.5.67-filings-coldfill: same lever for FILINGS. The 15s
    # steady-state pace is fine for incremental updates but pathological
    # on a fresh-install/catch-up — same shape as the fundamentals fix
    # above. EDGAR's 0.5s adapter limiter still paces actual requests.
    if (lane == 'filings'
            and len(unfilled) > _FILINGS_COLDFILL_UNFILLED_THRESHOLD):
        delay = _FILINGS_COLDFILL_DELAY_SECONDS

    baseline_filled = len(scope) - len(unfilled)
    with _executor_lock:
        _executor_state['current_lane'] = lane
        _executor_state['lane_progress'][lane] = {
            'scope_total': len(scope),
            'filled': baseline_filled,
            'started_at': time.time(),
        }

    if log_callback:
        # v4.14.5.44: honest pace note (delay 0 = source-limiter paced).
        pace_note = (f"{int(delay)}s per ticker" if delay >= 1
                     else "fast — source-rate-limited (cold/catch-up)")
        # v4.14.5.67-filings-coldfill: surface how many tickers were
        # excluded from the unfilled queue because EDGAR has already
        # confirmed them empty within the TTL — proves the tombstone
        # is working without a separate "I skipped X" line.
        skipped_note = ''
        if lane == 'filings':
            try:
                empties = tm_cache.get_fresh_empty_filings_tickers()
                n_skipped = len(scope & empties)
                if n_skipped > 0:
                    skipped_note = f", cached_empty_skipped={n_skipped}"
            except Exception:
                pass
        log_callback(
            f"[data] {lane}: pass {pass_count} — "
            f"{len(unfilled)} unfilled, {pace_note}{skipped_note}",
            'muted')

    tickers_done = 0
    # v4.14.5.75-safe-coldfill-speedups (Lever 2): pick-relevant tickers
    # (seed pools + holdings + watchlist + currently-displayed picks)
    # fill FIRST, alphabetical long tail after. Defensive: falls back to
    # plain sorted() if priority sources are empty or fail.
    for ticker in _priority_sorted(unfilled, app):
        if stop_event.is_set():
            break

        with _executor_lock:
            _executor_state['current_ticker'] = ticker

        try:
            fetcher(ticker)
            tickers_done += 1
            done += 1
        except Exception as _fe:
            # May 13 2026: surface fetcher failures amber instead of
            # swallowing them (was a bare except: pass).
            _fill_log(
                log_callback,
                f"[data] {lane} fetcher failed for {ticker}: "
                f"{type(_fe).__name__}: {_fe}",
                'amber')

        with _executor_lock:
            if lane in _executor_state['lane_progress']:
                _executor_state['lane_progress'][lane]['filled'] = (
                    baseline_filled + tickers_done)

        # Slower reporting cadence than bulk (50 vs 25).
        if (tickers_done > 0
                and tickers_done % 50 == 0
                and log_callback):
            pct = ((baseline_filled + tickers_done)
                    / len(scope) * 100.0)
            log_callback(
                f"[data] {lane}: "
                f"{baseline_filled + tickers_done}"
                f"/{len(scope)} ({pct:.1f}%)",
                'muted')

        if not stop_event.is_set():
            stop_event.wait(delay)

    return done


def _run_cold_news_fill(pass_count, app, fetchers,
                         log_callback, stop_event, start_yahoo_event):
    """v4.14.5.75-safe-coldfill-speedups (Lever 3): cold-fill the
    news_signals lane in parallel with the data lanes.

    News is NOT in BULK_FILLABLE_LANES — today it fills only via the
    scheduler's event-trigger sweep, which on a 7,201-ticker fresh
    install takes days to reach steady state. This worker drains the
    news-signals unfilled set at a bounded ~50 tickers/min (delay
    `_COLD_NEWS_FILL_DELAY_SECONDS = 1.2s`), well under Finnhub's
    60/min free-tier cap. Cold pass only — subsequent passes pick up
    where this one stopped via the normal unfilled-set check.

    Defensive gating:
      • `start_yahoo_event` blocks the worker until the daily_bars
        price batch finishes, so Yahoo-news calls (a separate endpoint
        but same domain) never stack with `yf.download` on yahoo.com.
        Until the event fires, the worker SLEEPS — it does NOT fire
        any news source. Once set, all four sources fire in parallel
        per ticker (RSS / Finnhub / Yahoo-news / Google News).
      • `stop_event` checked between every ticker so user-stop exits
        within one ticker-pace cycle (~1.2s).
      • The existing per-source timeout `_NEWS_PER_SOURCE_TIMEOUT_SEC`
        inside `deep_news_scan` and the news-sweep backoff in
        `tm_event_triggers.BACKOFF_TIERS` remain the backstop.

    Returns the count of tickers filled, or 0 if nothing to do / fetcher
    absent / start_yahoo_event never fires before stop_event.
    """
    import tm_cache
    lane = 'news_signals'
    if stop_event.is_set():
        return 0

    news_fetcher = fetchers.get('news_signals')
    if news_fetcher is None:
        return 0

    # Wait for the Yahoo PRICE batch to finish before firing any news
    # fetch — Yahoo-news is a different endpoint but the same domain,
    # and we don't want to stack consumers on yahoo.com during the
    # initial high-throughput chunked batch.
    while not start_yahoo_event.is_set():
        if stop_event.is_set():
            return 0
        # Cheap poll — the event is usually set within ~3min (price
        # batch duration), so a 1s poll is plenty fine.
        stop_event.wait(1.0)
        if start_yahoo_event.is_set():
            break

    scope = tm_cache.get_scope_tickers(lane, None)
    if not scope:
        return 0
    # No max_age_days — presence-mode unfilled (any row counts).
    unfilled = tm_cache.get_unfilled_tickers(lane, scope)
    if not unfilled:
        if log_callback:
            log_callback(
                f"[data] {lane}: cold-fill — already filled "
                f"({len(scope)} tickers)", 'muted')
        return 0

    # v4.14.5.86-news-progress-clarity: identify the pick-relevant
    # priority slice within `unfilled` so logging can announce
    # readiness when picks are newsed, then label the deep-universe
    # tail as background work. Fetch logic itself is BYTE-IDENTICAL
    # to pre-v.86 — same `_priority_sorted` ordering, same delay,
    # same dedup, same source. Only logging + readiness state change.
    # Flag-off path below preserves the single-line legacy log.
    _clarity = False
    try:
        _clarity = bool(_NEWS_PROGRESS_CLARITY_ENABLED)
    except Exception:
        _clarity = False

    priority_set: set = set()
    priority_unfilled: set = set()
    if _clarity:
        try:
            priority_set = _priority_tickers_for_app(app) or set()
        except Exception:
            priority_set = set()
        priority_unfilled = unfilled & priority_set

    # Headline: behavior depends on whether ANY priority tickers
    # are still unfilled. On a repeat-restart the priority slice is
    # typically already 100% covered (the investigation found this
    # is the user's live state), so we say "picks already current,
    # background continues" instead of leading with the scary
    # "5000 unfilled" counter.
    if log_callback:
        if _clarity and priority_set and not priority_unfilled:
            log_callback(
                f"[data] {lane}: picks already current; "
                f"background universe pass continues "
                f"({len(unfilled)} ticker(s), ~50/min — fills "
                f"in the background)",
                'muted')
            # Readiness state is true from the start of this pass.
            try:
                setattr(app, 'news_priority_slice_complete', True)
                setattr(app, 'news_priority_slice_count',
                        int(len(priority_set)))
            except Exception:
                pass
        elif _clarity and priority_unfilled:
            log_callback(
                f"[data] {lane}: pick-relevant {len(priority_unfilled)}"
                f" of {len(priority_set)} to fill, "
                f"then background universe ({len(unfilled)} total "
                f"unfilled, ~50/min — Finnhub-cap safe)",
                'accent')
            try:
                setattr(app, 'news_priority_slice_complete', False)
                setattr(app, 'news_priority_slice_count',
                        int(len(priority_set)))
            except Exception:
                pass
        else:
            # Flag-off OR no priority set resolvable → legacy
            # single-line headline.
            log_callback(
                f"[data] {lane}: cold-fill — {len(unfilled)} "
                f"unfilled, ~50/min (Finnhub-cap safe), "
                f"pick-relevant first",
                'muted')

    done = 0
    priority_done = 0   # filled WITHIN the priority slice this pass
    boundary_emitted = (
        _clarity and priority_set and not priority_unfilled)
    delay = _COLD_NEWS_FILL_DELAY_SECONDS
    for ticker in _priority_sorted(unfilled, app):
        if stop_event.is_set():
            break
        with _executor_lock:
            _executor_state['current_ticker'] = ticker
        _in_priority = (
            _clarity and ticker in priority_unfilled)
        try:
            news_fetcher(ticker)
            done += 1
            if _in_priority:
                priority_done += 1
        except Exception:
            # Silent per-ticker — deep_news_scan already routes errors
            # through tired_market's data-fetch-errors log via its
            # internal _log_data_fetch_error.
            pass
        # v4.14.5.86-news-progress-clarity: phase-aware progress log.
        # Phase 1 (priority slice): every _COLD_NEWS_LOG_EVERY_N=50
        # accent-tag against the PRIORITY count — the count that
        # matters. Phase 2 (universe tail): every _COLD_NEWS_TAIL_
        # LOG_EVERY_N=500 muted-tag against the FULL unfilled count,
        # framed as background work. Flag-off → original every-50
        # against full unfilled (legacy).
        if _clarity:
            # Boundary emit at priority completion. Two trigger
            # conditions:
            #  (a) we've filled at least as many priority tickers as
            #      were unfilled at pass start → clean completion.
            #  (b) iteration has CROSSED into the non-priority tail
            #      (current ticker not in priority_unfilled). This
            #      handles the silent-fetch-failure case where
            #      priority_done < len(priority_unfilled) but we've
            #      already moved past the priority slice in
            #      _priority_sorted order.
            if (not boundary_emitted
                    and priority_unfilled
                    and (priority_done >= len(priority_unfilled)
                         or not _in_priority)):
                if log_callback:
                    log_callback(
                        f"[data] {lane}: pick-relevant slice "
                        f"complete ({len(priority_set)} ticker(s) "
                        f"newsed) — app is ready; background "
                        f"universe fills below",
                        'green')
                boundary_emitted = True
                try:
                    setattr(app,
                            'news_priority_slice_complete', True)
                except Exception:
                    pass
            # Phase-specific progress.
            if (not boundary_emitted
                    and priority_done > 0
                    and priority_done % _COLD_NEWS_LOG_EVERY_N == 0
                    and log_callback):
                log_callback(
                    f"[data] {lane}: pick-relevant "
                    f"{priority_done}/{len(priority_unfilled)} "
                    f"(~50/min)",
                    'accent')
            elif (boundary_emitted
                    and done > 0
                    and done % _COLD_NEWS_TAIL_LOG_EVERY_N == 0
                    and log_callback):
                log_callback(
                    f"[data] {lane}: background universe "
                    f"{done}/{len(unfilled)} (~50/min — fills "
                    f"in the background)",
                    'muted')
        else:
            # Legacy: every-50 muted progress against full unfilled.
            if (done > 0 and done % _COLD_NEWS_LOG_EVERY_N == 0
                    and log_callback):
                log_callback(
                    f"[data] {lane}: cold-fill "
                    f"{done}/{len(unfilled)} "
                    f"(~{int(done * 60 / max(1, delay * done))}/min)",
                    'muted')
        if not stop_event.is_set():
            stop_event.wait(delay)

    if log_callback:
        log_callback(
            f"[data] {lane}: cold-fill pass {pass_count} done — "
            f"{done} filled this pass",
            'muted')

    # v4.14.5.87-readiness-snapshot Part B: emit the once-per-launch
    # "[readiness] picks ready" log line if the overall readiness
    # verdict has just flipped True (pick-relevant daily_bars at
    # 100% AND v.86 news_priority_slice_complete). Dedup is internal
    # to tm_readiness so this call is cheap on every subsequent
    # pass (one boolean check + return). Flag-off → silent no-op.
    try:
        import tm_readiness as _tm_rdy
        _tm_rdy.log_readiness_first_ready(app, log_callback)
    except Exception:
        pass
    return done


def _run_fundamentals_multistream(pass_count, app, fetchers, chunked_fetchers,
                                  log_callback, stop_event, start_yahoo_event):
    """v4.14.5.48-fill-multistream: fill the fundamentals lane with TWO
    concurrent streams draining ONE shared claim-per-ticker queue:

      • EDGAR stream  — the authoritative EDGAR-first deep chain
                        (fetchers['fundamentals']); EDGAR's own 0.5s _polite_wait
                        limiter paces it (SEC-safe, UNTOUCHED).
      • Yahoo stream  — Yahoo-deep statements (fetchers['fundamentals_yahoo']);
                        self-paced ~1/sec, never calls EDGAR.

    Each ticker is claimed exactly once (queue.get_nowait), so NO ticker is
    fetched by both streams — that's the dedup. EDGAR stays primary/authoritative;
    Yahoo is the speed/gap filler, and because the fundamentals fetcher is
    EDGAR-first, a later steady-state pass UPGRADES any Yahoo-served ticker to
    EDGAR's SEC data (EDGAR authoritative on merge). Net: ~the fundamentals fill
    is split across two sources so it completes faster, with no quality
    regression (Yahoo's path is deep statements, not the shallow snapshot).

    The Yahoo stream is DEFERRED until `start_yahoo_event` fires — the slow
    worker sets it once the daily_bars PRICE batch finishes — so the Yahoo price
    batch and this Yahoo stream are never loaded at the same time (avoids
    stacking two Yahoo consumers → 429s). The EDGAR stream runs the whole time,
    concurrent with daily_bars (different source). Returns the count filled.
    Falls back to single-stream _run_slow_lane when the Yahoo fetcher is absent."""
    import tm_cache
    import queue as _queue
    lane = 'fundamentals'
    if stop_event.is_set():
        return 0

    edgar_fetcher = fetchers.get('fundamentals')
    yahoo_fetcher = fetchers.get('fundamentals_yahoo')
    if edgar_fetcher is None:
        return 0
    if yahoo_fetcher is None:
        # No Yahoo stream available — behave exactly like the single-stream lane.
        return _run_slow_lane(lane, pass_count, app, fetchers,
                              chunked_fetchers, log_callback, stop_event)

    scope = tm_cache.get_scope_tickers(lane, None)
    if not scope:
        return 0
    unfilled = tm_cache.get_unfilled_tickers(
        lane, scope, max_age_days=_daily_bars_max_age(app, lane))
    if not unfilled:
        return 0

    q: "_queue.Queue" = _queue.Queue()
    # v4.14.5.75-safe-coldfill-speedups (Lever 2): seed the queue in
    # priority order — priority tickers drain first, alphabetical tail
    # after. EDGAR + Yahoo streams share this queue (claim-once) so the
    # ordering benefits both lanes.
    for t in _priority_sorted(unfilled, app):
        q.put(t)

    total_scope = len(scope)
    baseline_filled = total_scope - len(unfilled)
    with _executor_lock:
        _executor_state['current_lane'] = lane
        _executor_state['lane_progress'][lane] = {
            'scope_total': total_scope,
            'filled': baseline_filled,
            'started_at': time.time(),
        }

    # Cold/catch-up vs steady-state pacing for the EDGAR stream (mirrors
    # _run_slow_lane). The Yahoo stream uses its own polite ~1/sec pacing.
    cold = len(unfilled) > _FUND_COLDFILL_UNFILLED_THRESHOLD
    edgar_delay = (_FUND_COLDFILL_DELAY_SECONDS if cold
                   else _SLOW_INTER_TICKER_DELAY_SECONDS.get(lane, 20.0))
    yahoo_delay = _FUND_YAHOO_STREAM_DELAY_SECONDS

    if log_callback:
        log_callback(
            f"[data] {lane}: pass {pass_count} — {len(unfilled)} unfilled, "
            f"multi-stream (EDGAR authoritative + Yahoo filler)", 'muted')

    done_lock = threading.Lock()
    done = {'n': 0}

    def _stream_worker(fetcher, delay, label):
        while not stop_event.is_set():
            try:
                ticker = q.get_nowait()
            except _queue.Empty:
                break
            with _executor_lock:
                _executor_state['current_ticker'] = ticker
            try:
                fetcher(ticker)
                with done_lock:
                    done['n'] += 1
            except Exception as _fe:
                _fill_log(
                    log_callback,
                    f"[data] {lane} {label}-stream fetcher failed for "
                    f"{ticker}: {type(_fe).__name__}: {_fe}", 'amber')
            with done_lock:
                filled_now = baseline_filled + done['n']
            with _executor_lock:
                if lane in _executor_state['lane_progress']:
                    _executor_state['lane_progress'][lane]['filled'] = filled_now
            if not stop_event.is_set():
                stop_event.wait(delay)

    # EDGAR stream starts NOW — concurrent with daily_bars (different source).
    edgar_thread = threading.Thread(
        target=_stream_worker, args=(edgar_fetcher, edgar_delay, 'edgar'),
        name='tm_fill_fund_edgar', daemon=True)
    edgar_thread.start()

    # Yahoo stream is held back until the price batch frees Yahoo (or stop /
    # queue already drained by EDGAR). Poll the event so a stop is honored.
    while not stop_event.is_set() and not start_yahoo_event.wait(0.5):
        pass
    yahoo_thread = None
    if not stop_event.is_set() and not q.empty():
        if log_callback:
            log_callback(
                "[data] fundamentals: Yahoo stream joining now that the price "
                "batch is done (EDGAR + Yahoo draining the queue together)",
                'muted')
        yahoo_thread = threading.Thread(
            target=_stream_worker, args=(yahoo_fetcher, yahoo_delay, 'yahoo'),
            name='tm_fill_fund_yahoo', daemon=True)
        yahoo_thread.start()

    edgar_thread.join()
    if yahoo_thread is not None:
        yahoo_thread.join()

    return done['n']


def start_slow_fill(
    choices: dict,
    fetchers: dict,
    log_callback=None,
    on_finished=None,
    universe_source: str = 'iwv',
    app=None,
    chunked_fetchers: dict | None = None,
) -> bool:
    """Start a slow background fill in a worker thread. Returns True if started,
    False if any fill is already running.

    Differences from start_bulk_fill:
      - Per-lane delays are much longer (see _SLOW_INTER_TICKER_DELAY_SECONDS).
      - Worker loops indefinitely — finishes one pass, sleeps 1h, starts another
        pass to pick up newly stale / newly in-scope tickers.
      - No transition to incremental (slow IS the steady state for these users).
      - Progress log cadence is 50 tickers (vs 25 for bulk).

    Stop signal exits cleanly at the next ticker boundary OR during the
    inter-pass rest, whichever fires first.
    """
    import tm_cache

    with _executor_lock:
        if _executor_state['running']:
            return False
        _executor_state['running'] = True
        _executor_state['mode'] = 'slow'
        _executor_state['started_at'] = time.time()
        _executor_state['stop_event'] = threading.Event()
        _executor_state['lane_progress'] = {}
        _executor_state['current_ticker'] = None
        _executor_state['current_lane'] = None
        stop_event = _executor_state['stop_event']

    def _slow_worker():
        try:
            try:
                _seed_universe_if_needed(
                    log_callback, stop_event, chosen_universe=universe_source)
            except Exception as e:
                if log_callback:
                    log_callback(f"[data] Universe seed failed: {e}", 'amber')

            # May 13 2026: honor a persisted rest end-time from a prior
            # session. Without this, every app restart effectively
            # delayed the next pass by up to a full hour by restarting
            # the in-memory rest timer.
            from datetime import datetime as _dt, timedelta as _td
            prior_rest_end = _read_rest_until(app)
            if prior_rest_end is not None:
                remaining = (prior_rest_end - _dt.now()).total_seconds()
                if remaining > 0:
                    if log_callback:
                        log_callback(
                            f"[data] Prior session rest has "
                            f"{int(remaining // 60)} min remaining "
                            f"— waiting until "
                            f"{_format_local_time(prior_rest_end)}.",
                            'muted')
                    stop_event.wait(remaining)
                _clear_rest_until(app)
            elif app is not None and app.cfg.get(
                    'slow_fill_rest_until'):
                # Stamp existed but was in the past — the rest already
                # elapsed naturally during downtime. Clean up + log.
                if log_callback:
                    log_callback(
                        "[data] Prior session rest elapsed — starting "
                        "Pass 1 immediately.",
                        'muted')
                _clear_rest_until(app)

            pass_count = 0
            while not stop_event.is_set():
                pass_count += 1
                tickers_done_this_pass = 0
                if log_callback:
                    log_callback(
                        f"[data] Starting pass {pass_count}", 'muted')

                # v4.14.5.47-fill-wal-concurrent-start (Lever 1): during the
                # COLD fill (pass 1 only) run the fundamentals lane CONCURRENT
                # with daily_bars. The two use independent sources (Yahoo prices
                # vs the EDGAR-first deep fundamentals chain), so fundamentals no
                # longer waits for the whole price phase to finish — it overlaps
                # the ~3-min price batch instead. We JOIN fundamentals BEFORE the
                # `filings` lane (also EDGAR) so two EDGAR lanes NEVER run at
                # once — EDGAR's 0.5s SEC politeness limiter is untouched and
                # trivially respected (fundamentals only ever overlaps Yahoo).
                # Pass 2+ (steady-state refresh) stays fully sequential and
                # byte-identical, so the gentle background cadence is unchanged.
                # Flag-gated: cfg['use_concurrent_fill_lanes'] (default True);
                # False restores the exact pre-patch sequential lane loop.
                lanes = list(tm_cache.BULK_FILLABLE_LANES)
                try:
                    _concurrent = (pass_count == 1 and bool(
                        getattr(app, 'cfg', {}).get(
                            'use_concurrent_fill_lanes', True)))
                except Exception:
                    _concurrent = (pass_count == 1)

                _lane_done: dict = {}
                _fund_thread = None
                # v4.14.5.48-fill-multistream: gate that releases the Yahoo
                # fundamentals stream only after the daily_bars price batch
                # finishes (set below), so the Yahoo price batch and the Yahoo
                # fundamentals stream never load Yahoo at the same time. Created
                # unconditionally; only consumed when multi-stream is active.
                _start_yahoo_event = threading.Event()
                if (_concurrent and 'daily_bars' in lanes
                        and 'fundamentals' in lanes
                        and fetchers.get('fundamentals') is not None):
                    try:
                        _multistream = (
                            bool(getattr(app, 'cfg', {}).get(
                                'use_multistream_fundamentals', True))
                            and fetchers.get('fundamentals_yahoo') is not None)
                    except Exception:
                        _multistream = (
                            fetchers.get('fundamentals_yahoo') is not None)

                    def _fund_runner():
                        try:
                            if _multistream:
                                _lane_done['fundamentals'] = (
                                    _run_fundamentals_multistream(
                                        pass_count, app, fetchers,
                                        chunked_fetchers, log_callback,
                                        stop_event, _start_yahoo_event))
                            else:
                                _lane_done['fundamentals'] = _run_slow_lane(
                                    'fundamentals', pass_count, app, fetchers,
                                    chunked_fetchers, log_callback, stop_event)
                        except Exception:
                            _lane_done['fundamentals'] = 0
                    _fund_thread = threading.Thread(
                        target=_fund_runner,
                        name='tm_fill_executor_slow_fund', daemon=True)

                # v4.14.5.75-safe-coldfill-speedups (Lever 3): cold-fill
                # news in parallel on pass 1. Today news is not in
                # BULK_FILLABLE_LANES — it fills only via the scheduler's
                # event-trigger sweep (STORM_FIRE_CAP=20/sweep w/ backoff),
                # so a 7,201-ticker cold install takes DAYS to reach news
                # steady-state. The news fetchers (RSS + Yahoo-news +
                # Finnhub + Google News, deep_news_scan) are network-
                # independent of EDGAR and the Yahoo PRICE batch, so they
                # can safely fill alongside the data lanes. Bounded at
                # _COLD_NEWS_FILL_DELAY (1.2s = ~50 tickers/min) — well
                # under Finnhub's 60/min free-tier cap. Gated on
                # _start_yahoo_event so the Yahoo-news endpoint never
                # stacks with the Yahoo PRICE batch (separate endpoint,
                # but both target yahoo.com — defensive gating). Cold-pass
                # only; steady-state news refresh stays scheduler-driven.
                _news_thread = None
                if (_concurrent and fetchers.get('news_signals') is not None):
                    def _cold_news_runner():
                        try:
                            _lane_done['news_signals'] = (
                                _run_cold_news_fill(
                                    pass_count, app, fetchers,
                                    log_callback, stop_event,
                                    _start_yahoo_event))
                        except Exception:
                            _lane_done['news_signals'] = 0
                    _news_thread = threading.Thread(
                        target=_cold_news_runner,
                        name='tm_fill_executor_slow_news', daemon=True)

                for lane in lanes:
                    if stop_event.is_set():
                        break
                    if lane == 'fundamentals' and _fund_thread is not None:
                        # Handled on its own thread, started alongside
                        # daily_bars below.
                        continue
                    if lane == 'daily_bars' and _fund_thread is not None:
                        if log_callback:
                            log_callback(
                                "[data] fundamentals: filling concurrently "
                                "with daily_bars (independent sources)",
                                'muted')
                        _fund_thread.start()
                        # v4.14.5.75-safe-coldfill-speedups (Lever 3):
                        # spin up the cold-news worker alongside
                        # fundamentals. It self-gates on _start_yahoo_event
                        # before issuing any Yahoo-news call, so the early
                        # window only uses RSS + Finnhub + Google News.
                        if _news_thread is not None:
                            if log_callback:
                                log_callback(
                                    "[data] news_signals: cold-fill in "
                                    "parallel (~50/min, RSS + Finnhub + "
                                    "Google News + Yahoo-news once price "
                                    "batch done)",
                                    'muted')
                            _news_thread.start()
                    _lane_done[lane] = _run_slow_lane(
                        lane, pass_count, app, fetchers, chunked_fetchers,
                        log_callback, stop_event)
                    if lane == 'daily_bars' and _fund_thread is not None:
                        # Price batch done → release the Yahoo fundamentals
                        # stream (multi-stream mode); harmless no-op otherwise.
                        _start_yahoo_event.set()
                        # v4.14.5.75-safe-coldfill-speedups (Lever 1): the
                        # previous build joined the fundamentals thread HERE,
                        # before filings dispatched, on the theory that "two
                        # EDGAR lanes must never overlap." But EDGAR's
                        # `_polite_wait` (tm_data_adapter_edgar.py:149) is a
                        # MODULE-LEVEL `threading.Lock` + `_last_call_at`
                        # timestamp that serializes EVERY EDGAR caller to
                        # `_MIN_INTERVAL_SEC = 0.5s` spacing globally —
                        # verified empirically: 8 threads × 10 calls held at
                        # 2.02/sec, zero gap violations. So joining here
                        # is redundant safety that costs ~40min on cold
                        # start (forces sequential 50min + 70min instead
                        # of interleaved max(50,70)=70min). Removed; the
                        # combined join below now reaps both EDGAR lanes
                        # at the end of the pass, and `_polite_wait` keeps
                        # SEC happy regardless of how many lanes call it.

                # v4.14.5.75-safe-coldfill-speedups (Lever 1): single
                # end-of-pass join — waits for fundamentals to finish
                # whether the loop completed or was stopped. `_polite_wait`
                # has already ensured fundamentals' EDGAR stream and
                # filings' EDGAR stream interleaved at ≤2/sec total.
                if _fund_thread is not None and _fund_thread.is_alive():
                    _start_yahoo_event.set()
                    _fund_thread.join()
                # v4.14.5.75-safe-coldfill-speedups (Lever 3): reap the
                # cold-news worker the same way. News is bounded at
                # ~50/min and can outlast the data lanes on a fresh
                # install (7,201 tickers / 50/min ≈ 2.4h); the pass
                # WON'T sleep for that — we set the stop_event when the
                # data lanes finish and the news worker exits at its
                # next ticker boundary, OR we let it ride and complete
                # in a later pass. Simpler: just reap it here, and the
                # news worker checks stop_event between tickers so a
                # genuine user-stop exits within seconds.
                if _news_thread is not None and _news_thread.is_alive():
                    _start_yahoo_event.set()
                    _news_thread.join()

                tickers_done_this_pass += sum(
                    int(v or 0) for v in _lane_done.values())

                if stop_event.is_set():
                    break

                # May 13 2026: previously slept the full 3600s after
                # EVERY pass — including no-op passes where every lane
                # was either fully filled or scope-empty. Combined with
                # in-memory-only rest tracking (also fixed today), this
                # locked slow fill out for an hour after each restart.
                # Now: short rest if zero work happened.
                if tickers_done_this_pass == 0:
                    short_rest = 60
                    rest_end = _dt.now() + _td(seconds=short_rest)
                    if log_callback:
                        log_callback(
                            f"[data] Pass {pass_count} had nothing to "
                            f"do — checking again in {short_rest}s "
                            f"(at {_format_local_time(rest_end)}).",
                            'muted')
                    _write_rest_until(app, rest_end)
                    stop_event.wait(short_rest)
                    _clear_rest_until(app)
                else:
                    rest_end = _dt.now() + _td(
                        seconds=_SLOW_INTER_PASS_REST_SECONDS)
                    if log_callback:
                        log_callback(
                            f"[data] Pass {pass_count} complete "
                            f"({tickers_done_this_pass} ticker"
                            f"{'' if tickers_done_this_pass == 1 else 's'}"
                            f" filled) — resting until "
                            f"{_format_local_time(rest_end)}.",
                            'muted')
                    _write_rest_until(app, rest_end)
                    stop_event.wait(_SLOW_INTER_PASS_REST_SECONDS)
                    _clear_rest_until(app)

            if log_callback:
                log_callback(
                    f"[data] Stopped after {pass_count} pass(es)",
                    'amber')

            if on_finished:
                try:
                    on_finished({
                        'pass_count': pass_count,
                        'lane_progress': dict(
                            _executor_state['lane_progress']),
                    })
                except Exception:
                    pass
        except Exception:
            if log_callback:
                log_callback(
                    f"[data] Worker crashed: "
                    f"{traceback.format_exc()[:300]}",
                    'amber')
        finally:
            with _executor_lock:
                _executor_state['running'] = False
                _executor_state['mode'] = None
                _executor_state['current_ticker'] = None
                _executor_state['current_lane'] = None

    thread = threading.Thread(
        target=_slow_worker, name='tm_fill_executor_slow', daemon=True)
    with _executor_lock:
        _executor_state['thread'] = thread
    thread.start()
    return True


# ─── v4.15.0 Step 21: Server pull (stub) ─────────────────────────────────────

def start_server_pull(
    choices: dict,
    fetchers: dict,
    log_callback=None,
    on_finished=None,
    universe_source: str = 'iwv',
    app=None,
) -> bool:
    """v4.15.0 Step 21: STUB — donor server pull not yet wired.

    Real implementation will:
      1. Authenticate with donor token.
      2. Download bulk archive from server.
      3. Import to cache.db.
      4. Transition lanes to incremental.

    For now: logs a clear amber message and falls back to slow fill so the
    user gets data eventually instead of silent failure. When the real path
    lands, this function's body changes; the App-level wiring stays put.
    """
    if log_callback:
        log_callback(
            "[server] Donor server pull is not yet wired in this build. "
            "Falling back to slow background fill — your cache will populate "
            "gradually. The 'From server' option will be available in a "
            "future update.",
            'amber')

    return start_slow_fill(
        choices=choices,
        fetchers=fetchers,
        log_callback=log_callback,
        on_finished=on_finished,
        universe_source=universe_source,
        app=app,
    )
