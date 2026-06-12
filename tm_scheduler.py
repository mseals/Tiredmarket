"""
Tired Market — Phase 2B: scheduler and event detector.

This module owns the "AI runs in the background when something changes"
behavior. Three layers:

    SchedulerMode  — figures out what time-based mode we're in
                     (active/reduced/idle/morning-brief/paused/gaming)
    EventDetector  — watches for things worth analyzing on
                     (price moves, news, earnings, regime changes)
    Scheduler      — orchestrates the two: at the right cadence for the
                     current mode, ask the detector what events fired,
                     hand those to the AI for analysis

Design principles:
    - The AI runs ONLY when there's a real reason to (an event fired).
      We don't burn GPU on "nothing changed" ticks.
    - The schedule gates HOW OFTEN the detector even runs. Active = every
      60-120s. Reduced = every 15-30 min. Idle = not at all.
    - Manual user actions (clicking Check Now) bypass everything — they
      go straight to the AI regardless of mode.
    - Game detection is two-layered: a known-process list + a GPU usage
      fallback. If the user is gaming, the scheduler stays asleep.
    - State lives in the app's existing persistent files. The detector
      compares "last seen" data to "current" data to spot changes.

What this module does NOT do:
    - It doesn't talk to Ollama directly. It calls back into the App's
      _run_silent_scan to do AI work — same path as the launch auto-scan.
    - It doesn't handle the locked-positions daily check directly. That's
      a separate trigger (once per day) that calls the same scan path
      with locked=True.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


# ─── Modes ─────────────────────────────────────────────────────────────

# Mode constants. Used by the App to update the status badge.
MODE_PAUSED = "paused"        # User clicked the pause badge
MODE_GAMING = "gaming"        # Game detected; AI suppressed
MODE_ACTIVE = "active"        # Market hours, full attention
MODE_REDUCED = "reduced"      # Pre/post market, big events only
MODE_IDLE = "idle"            # Overnight/weekends/holidays
MODE_BRIEF = "brief"          # Morning brief window (~30min before open)
MODE_WORKING = "working"      # Currently running an analysis

MODE_LABELS = {
    MODE_PAUSED:  "⏸ Paused",
    MODE_GAMING:  "🎮 Gaming",
    MODE_ACTIVE:  "● Active",
    MODE_REDUCED: "◐ Reduced",
    MODE_IDLE:    "○ Idle",
    MODE_BRIEF:   "☀ Brief",
    MODE_WORKING: "● Working",
}


# Cadence (seconds between scheduler ticks) per mode.
# These are how often we ASK THE DETECTOR if anything fired. The detector
# is cheap (just data comparisons). The AI only fires when events trigger.
CADENCE_ACTIVE = 90              # 1.5 min during market hours
CADENCE_REDUCED = 20 * 60        # 20 min after-hours / pre-market
CADENCE_IDLE = 60 * 60           # 1 hour overnight (just checks if mode changed)
CADENCE_GAMING = 5 * 60          # 5 min while gaming (just checks if game ended)
CADENCE_PAUSED = 30              # 30s while paused (just checks if user resumed)

# Event detector thresholds
PRICE_MOVE_THRESHOLD_PCT = 3.0      # Single-tick move that triggers analysis
DRIFT_THRESHOLD_PCT = 5.0           # Cumulative drift over recent ticks
VOLUME_RATIO_TRIGGER = 2.0          # Volume vs avg that confirms a move
EARNINGS_DAYS_AHEAD = 3             # Flag earnings within N days
LOCKED_PRICE_MOVE_PCT = 50.0        # For locked positions, only big moves matter

# How often to run the locked-position daily check (in hours since last)
LOCKED_DAILY_CHECK_HOURS = 24


def _et_now() -> datetime:
    """Return current time in approximate US Eastern Time.
    Uses the same DST approximation as get_market_status — UTC-4 in
    Mar-Nov, UTC-5 otherwise. Good enough for our purposes; we don't
    need second-precision boundary handling.
    """
    utc_now = datetime.now(timezone.utc)
    offset_h = -4 if 3 <= utc_now.month <= 11 else -5
    return (utc_now + timedelta(hours=offset_h)).replace(tzinfo=None)


def get_schedule_mode(is_paused: bool, is_gaming: bool) -> str:
    """Determine current scheduler mode based on time + user state.
    Order of precedence:
        Paused (user override) > Gaming (system override) > time-of-day
    """
    if is_paused:
        return MODE_PAUSED
    if is_gaming:
        return MODE_GAMING

    et = _et_now()
    weekday = et.weekday()  # 0=Mon, 6=Sun
    hour, minute = et.hour, et.minute

    if weekday >= 5:
        return MODE_IDLE  # Saturday or Sunday

    # Morning brief window: 9:00-9:30 AM ET (last 30 min before open)
    if hour == 9 and minute < 30:
        return MODE_BRIEF
    # Active market: 9:30 AM - 4:00 PM ET
    if (hour == 9 and minute >= 30) or (10 <= hour < 16):
        return MODE_ACTIVE
    # Pre-market: 4 AM - 9 AM ET
    if 4 <= hour < 9:
        return MODE_REDUCED
    # After-hours: 4 PM - 8 PM ET
    if 16 <= hour < 20:
        return MODE_REDUCED
    # Overnight: 8 PM - 4 AM ET
    return MODE_IDLE


def cadence_for(mode: str) -> int:
    """How long to sleep between scheduler ticks for this mode."""
    return {
        MODE_ACTIVE:  CADENCE_ACTIVE,
        MODE_REDUCED: CADENCE_REDUCED,
        MODE_BRIEF:   CADENCE_ACTIVE,   # check often during brief window
        MODE_IDLE:    CADENCE_IDLE,
        MODE_GAMING:  CADENCE_GAMING,
        MODE_PAUSED:  CADENCE_PAUSED,
        MODE_WORKING: CADENCE_ACTIVE,
    }.get(mode, CADENCE_ACTIVE)


# ─── Game detection ───────────────────────────────────────────────────

# v4.13.27: Launchers (Steam, Epic, Battle.net, etc.) removed from
# the default detection list because they show as 'running' the
# moment they're in the system tray, which falsely flags gaming
# mode any time those clients are open. Only actual game
# executables remain. User can still add more via config's
# 'game_executables' list (Settings -> Performance).
DEFAULT_GAME_PROCS = {
    # Common game engines that signal active gameplay
    'unrealengine.exe', 'unityplayer.exe',
    # Specific popular games (illustrative; user adds more)
    'cs2.exe', 'csgo.exe', 'dota2.exe',
    'r5apex.exe', 'apex.exe',
    'fortniteclient-win64-shipping.exe',
    'valorant.exe', 'valorant-win64-shipping.exe',
    'rdr2.exe', 'gta5.exe', 'gtav.exe',
    'eldenring.exe', 'darksoulsiii.exe',
    'cyberpunk2077.exe',
    'minecraft.exe',
}


def detect_running_games(extra_procs: set[str] | None = None) -> list[str]:
    """Return a list of currently running process names that match the
    known-game list. Empty list = no games detected. Cheap call: uses
    psutil if available, falls back to Windows tasklist.

    Extra processes can be added via config['game_executables'].
    """
    procs_to_check = set(p.lower() for p in DEFAULT_GAME_PROCS)
    if extra_procs:
        procs_to_check |= set(p.lower() for p in extra_procs)

    found = []
    # Try psutil first (cleaner, cross-platform)
    try:
        import psutil  # type: ignore
        for p in psutil.process_iter(['name']):
            try:
                name = (p.info.get('name') or '').lower()
                if name in procs_to_check:
                    found.append(name)
            except Exception:
                continue
        return list(set(found))
    except Exception:
        pass

    # Fall back to Windows tasklist
    if os.name == 'nt':
        try:
            import subprocess
            out = subprocess.check_output(
                ['tasklist', '/fo', 'csv', '/nh'],
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                timeout=5,
            )
            text = out.decode('utf-8', errors='ignore').lower()
            for proc in procs_to_check:
                if proc in text:
                    found.append(proc)
        except Exception:
            pass
    return list(set(found))


def gpu_busy_externally() -> bool:
    """Return True if the GPU appears busy with non-AI work.
    Used as a fallback when game-process detection misses something
    (browser games, indie launchers, etc.). Only relevant on Windows
    with NVIDIA. If we can't determine, return False (safer to assume
    AI is fine to run).

    Not 100% reliable — there's no clean Python API for this. We try
    nvidia-smi if available. If it's not, we can't fall back further,
    so just return False.
    """
    try:
        import subprocess
        out = subprocess.check_output(
            ['nvidia-smi', '--query-gpu=utilization.gpu', '--format=csv,noheader,nounits'],
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            timeout=3,
        )
        utilization = int(out.decode('utf-8').strip().split('\n')[0])
        # >70% sustained suggests something else is using the GPU.
        # We can't tell if it's our own AI or a game, so this is only
        # used in conjunction with "AI not currently working" — i.e.,
        # if the AI is idle and the GPU is busy, something else is
        # using it.
        return utilization >= 70
    except Exception:
        return False


# ─── Event detector ────────────────────────────────────────────────────

class EventDetector:
    """Compares current state to last-seen state and reports anything
    worth re-analyzing. Cheap to run (just data comparisons, no AI).

    State is tracked per-ticker in `_last_state`:
        {
            'TICKER': {
                'last_price': float,
                'last_seen': iso timestamp,
                'last_news_count': int,
                'last_news_check': iso timestamp,
                'cumulative_drift_pct': float,
                'last_full_analysis': iso timestamp,
            },
            ...
        }

    The detector NEVER calls the AI. It returns event objects describing
    what fired; the scheduler decides whether to dispatch them.
    """

    def __init__(self, holdings_mgr, cache, db=None,
                 state_path=None, state_log_fn=None):
        self.mgr = holdings_mgr
        self.cache = cache
        self.db = db
        # v4.14.5.14-phantom-news-fix: persisted baseline. state_path is
        # a Path-like (or None for in-memory-only / tests). state_log_fn
        # is an optional callable(str) for amber-level diagnostics on
        # load/save failures — silent if not provided, matching the
        # rest of this module's logging convention.
        self.state_path = state_path
        self._state_log_fn = state_log_fn
        self._last_state: dict[str, dict] = self._load_last_state()
        self._lock = threading.Lock()

    # ── Persistence (v4.14.5.14-phantom-news-fix) ─────────────────────
    #
    # `_last_state` is the per-ticker baseline (`last_price`,
    # `last_news_count`, `cumulative_drift_pct`, etc.) used to decide
    # whether a real change happened. Pre-fix it was in-memory only —
    # every process restart reset `last_news_count` to 0, so the next
    # scan's legitimate `total_headlines=161` became a "161 new" event.
    # Persisting the dict to a small JSON sidecar (parallel to the
    # Scheduler's existing `scheduler_state.json`) closes that gap.
    # Atomic writes (tmp + os.replace); never raises into the caller.

    def _load_last_state(self) -> dict:
        if self.state_path is None:
            return {}
        try:
            sp = Path(self.state_path)
        except Exception:
            return {}
        if not sp.exists():
            return {}
        try:
            import json
            with open(sp, encoding='utf-8') as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                self._state_log(
                    f"[event-detector] state file {sp.name} is not a "
                    "JSON object — ignoring (treat all tickers as "
                    "first observation).")
                return {}
            return data
        except Exception as e:
            self._state_log(
                f"[event-detector] failed to load {sp.name} "
                f"({type(e).__name__}: {e}); treating all tickers as "
                "first observation.")
            return {}

    def _save_last_state(self) -> None:
        if self.state_path is None:
            return
        try:
            import json
            sp = Path(self.state_path)
            sp.parent.mkdir(parents=True, exist_ok=True)
            tmp = sp.with_suffix(sp.suffix + '.tmp')
            with self._lock:
                snapshot = dict(self._last_state)
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(snapshot, fh, indent=2)
            os.replace(str(tmp), str(sp))
        except Exception as e:
            try:
                self._state_log(
                    f"[event-detector] failed to persist baseline "
                    f"({type(e).__name__}: {e}); in-memory state "
                    "still tracked, next successful write recovers.")
            except Exception:
                pass

    def _state_log(self, msg: str) -> None:
        fn = self._state_log_fn
        if fn is None:
            return
        try:
            fn(msg)
        except Exception:
            pass

    def detect(self, mode: str) -> list[dict]:
        """Return list of events worth analyzing right now.
        Mode-aware: in REDUCED mode, only major events fire. In ACTIVE,
        smaller moves count too.

        Each event dict has at minimum:
            ticker, reason, severity ('minor'/'major'/'critical')
        """
        events = []
        dirty = False
        for h in self.mgr.holdings:
            ticker = h.get('ticker', '?').upper()
            tradable = h.get('tradable', True)

            # Locked positions only get checked once a day (handled separately)
            if not tradable:
                continue

            ticker_events = self._detect_for_ticker(h, mode)
            events.extend(ticker_events)
            dirty = True  # _detect_for_ticker always writes _last_state
        if dirty:
            self._save_last_state()
        return events

    def _detect_for_ticker(self, holding: dict, mode: str) -> list[dict]:
        ticker = holding.get('ticker', '?').upper()
        events = []

        # Get current data
        quote = self.cache.quote(ticker)
        if not quote:
            return events
        current_price = quote.get('price')
        if not current_price:
            return events

        # v4.14.5.63-tier-timeframe: per-tier sell-trigger sensitivity. Source
        # the price-move / drift thresholds from THIS holding's tier (its
        # `path`, set in Build 1) instead of the global constants — aggressive
        # names fire on smaller moves, speculative names only on big ones.
        # Single source of truth = tm_watch_tiers; the global PRICE_MOVE_/
        # DRIFT_THRESHOLD_PCT constants remain the fallback if the table can't
        # be read. Lazy import keeps tm_scheduler free of module-load deps.
        _pm_thr = PRICE_MOVE_THRESHOLD_PCT
        _dr_thr = DRIFT_THRESHOLD_PCT
        try:
            import tm_watch_tiers as _wt
            _row = _wt.tier_params(holding.get('path'))
            _pm_thr = float(_row.get('price_move_pct', _pm_thr))
            _dr_thr = float(_row.get('drift_pct', _dr_thr))
        except Exception:
            pass

        # Get last-seen state (or initialize). v4.14.5.14-phantom-news-fix:
        # `last_news_count` defaults to None (not 0) so the firing logic
        # below can distinguish "never observed" from "legitimately zero
        # last time." If the stored state is malformed (non-dict / wrong
        # types), treat the ticker as first observation rather than
        # crashing.
        with self._lock:
            state = self._last_state.setdefault(ticker, {})
            if not isinstance(state, dict):
                state = {}
                self._last_state[ticker] = state
            last_price = state.get('last_price')
            try:
                cumulative_drift = float(
                    state.get('cumulative_drift_pct', 0.0) or 0.0)
            except (TypeError, ValueError):
                cumulative_drift = 0.0
            last_news_check = state.get('last_news_check')
            last_news_count = state.get('last_news_count')  # None ⇒ unseen
            last_full_analysis = state.get('last_full_analysis')

        # ── Price move check ──
        if last_price is not None and last_price > 0:
            pct_change = (current_price - last_price) / last_price * 100
            abs_change = abs(pct_change)

            # Major: single-tick move past the tier threshold
            if abs_change >= _pm_thr:
                events.append({
                    'ticker': ticker,
                    'reason': f"price moved {pct_change:+.1f}% "
                              f"(${last_price:g} → ${current_price:g})",
                    'severity': 'major' if abs_change < 7 else 'critical',
                    'event_type': 'price_move',
                })
                # Reset cumulative drift after major move
                cumulative_drift = 0.0
            else:
                # Minor: accumulate drift (only fires above the tier threshold)
                cumulative_drift += pct_change
                if abs(cumulative_drift) >= _dr_thr:
                    events.append({
                        'ticker': ticker,
                        'reason': f"cumulative drift {cumulative_drift:+.1f}% "
                                  f"over recent ticks (now ${current_price:g})",
                        'severity': 'minor',
                        'event_type': 'drift',
                    })
                    cumulative_drift = 0.0  # reset after firing

        # ── News check ──
        # Only re-check news every 30 minutes (it's slower than quotes)
        should_check_news = False
        if last_news_check is None:
            should_check_news = True
        else:
            try:
                last_check_dt = datetime.fromisoformat(last_news_check)
                if (datetime.now() - last_check_dt).total_seconds() > 30 * 60:
                    should_check_news = True
            except Exception:
                should_check_news = True

        new_news_count = last_news_count  # may be None ⇒ stays None
        if should_check_news:
            # ── v4.13.55b: actively populate the news cache ────────
            # Previously this only READ from a cache that nothing
            # populated — so news_sentiment was always 0 and Finnhub
            # never fired. Now we trigger an actual scan in a
            # background thread (so the scheduler doesn't block).
            # The scan writes to news_cache + news_scans tables which
            # the rest of the app reads from.
            if self.db is not None:
                try:
                    import threading
                    import importlib
                    # Late-import to avoid circular dependency at
                    # module load time.
                    tm = importlib.import_module('tired_market')
                    if hasattr(tm, 'deep_news_scan'):
                        def _bg_news_scan(tk=ticker, db=self.db):
                            try:
                                tm.deep_news_scan(tk, db)
                            except Exception:
                                # Silent — scheduler shouldn't crash
                                # on news scan failures
                                pass
                        threading.Thread(
                            target=_bg_news_scan,
                            daemon=True,
                            name=f'news-scan-{ticker}'
                        ).start()
                except Exception:
                    pass
            try:
                news = self.cache.news_features(ticker)
                if news and isinstance(news, dict):
                    new_news_count = news.get('article_count', 0)
                    # v4.14.5.14-phantom-news-fix Part A: suppress on
                    # first observation (last_news_count is None means
                    # we've never seen this ticker in-process AND no
                    # persisted baseline was loaded — every restart
                    # used to fire "<total> new" for every tradable
                    # holding because the in-memory dict reset to 0).
                    # Record the baseline without emitting an event;
                    # the next tick will compute a real delta. Part B
                    # then persists `_last_state` so subsequent restarts
                    # skip the "first observation" branch entirely.
                    if last_news_count is None:
                        pass  # baseline recorded via state update below
                    elif new_news_count > last_news_count:
                        delta = new_news_count - last_news_count
                        # In reduced mode, only fire on substantial news
                        threshold = 1 if mode == MODE_ACTIVE else 3
                        if delta >= threshold:
                            # v4.14.5.14-phantom-news-fix Part C: the old
                            # "<N> new news articles in last 30 min" line
                            # claimed a freshness window that doesn't
                            # exist — the 30 min is the check cadence, not
                            # a per-article filter. RSS feeds routinely
                            # publish back-catalog items that legitimately
                            # bump the merged scan count. Reword to
                            # describe what the delta actually IS.
                            events.append({
                                'ticker': ticker,
                                'reason': f"{delta} more headline"
                                          f"{'s' if delta>1 else ''} "
                                          f"than at last check (checked "
                                          f"every 30 min)",
                                'severity': 'major' if delta >= 3 else 'minor',
                                'event_type': 'news',
                            })
            except Exception:
                pass

        # ── Earnings date check ──
        # If earnings within EARNINGS_DAYS_AHEAD, flag it once per day
        # (TODO: check_earnings_date is in tired_market.py — caller must
        # provide a way to get it; for now we skip if cache doesn't have it)

        # v4.14.5.14-phantom-news-fix Part A: the legacy
        #     if is_first_observation: pass
        # block here was dead code — the news event was already appended
        # to `events` above, and price-move / drift events are guarded
        # by `last_price is not None` so they don't fire on first sight
        # anyway. The suppression intent now lives inline in the news
        # block (`if last_news_count is None: pass`) so it actually
        # works. Block deleted to prevent future readers from thinking
        # it was load-bearing.

        # ── Update state ──
        with self._lock:
            self._last_state[ticker] = {
                'last_price': current_price,
                'last_seen': datetime.now().isoformat(),
                'last_news_count': new_news_count,
                'last_news_check': datetime.now().isoformat() if should_check_news
                                    else last_news_check,
                'cumulative_drift_pct': cumulative_drift,
                'last_full_analysis': last_full_analysis,
            }

        return events

    def detect_locked_changes(self) -> list[dict]:
        """Special check for locked positions. Only flags BIG changes
        because locked positions don't move much normally. Called once
        a day from the locked-position daily check."""
        events = []
        for h in self.mgr.holdings:
            if h.get('tradable', True):
                continue  # skip tradable here
            ticker = h.get('ticker', '?').upper()
            quote = self.cache.quote(ticker)
            if not quote:
                continue
            current_price = quote.get('price')
            if not current_price:
                continue

            with self._lock:
                state = self._last_state.setdefault(f"LOCKED_{ticker}", {})
                last_price = state.get('last_price')

            if last_price is not None and last_price > 0:
                pct_change = (current_price - last_price) / last_price * 100
                if abs(pct_change) >= LOCKED_PRICE_MOVE_PCT:
                    events.append({
                        'ticker': ticker,
                        'reason': f"locked position moved {pct_change:+.1f}% "
                                  f"(${last_price:g} → ${current_price:g}) — "
                                  "possible unlock event?",
                        'severity': 'critical',
                        'event_type': 'locked_move',
                        'is_locked': True,
                    })

            with self._lock:
                self._last_state[f"LOCKED_{ticker}"] = {
                    'last_price': current_price,
                    'last_seen': datetime.now().isoformat(),
                }
        # v4.14.5.14-phantom-news-fix: persist locked-position baseline
        # too — same restart-resets-to-zero shape, smaller blast radius
        # since locked positions only get checked once per day.
        self._save_last_state()
        return events

    def mark_analyzed(self, ticker: str):
        """Record that a full analysis happened (for cooldown tracking)."""
        ticker = ticker.upper()
        with self._lock:
            state = self._last_state.setdefault(ticker, {})
            state['last_full_analysis'] = datetime.now().isoformat()
        # v4.14.5.14-phantom-news-fix: durable cooldown across restarts.
        self._save_last_state()


# ─── Scheduler ─────────────────────────────────────────────────────────

class Scheduler:
    """Orchestrator. Runs in a background thread. On each tick:
        1. Determine current mode based on time + user state
        2. Sleep cadence_for(mode)
        3. If mode is IDLE/PAUSED/GAMING: nothing else, loop
        4. Otherwise: call detector, get events
        5. For each event, dispatch to the scan callback (which uses the
           AI)

    Also handles two special triggers:
        - Morning brief: at the BRIEF window, if no brief has run today,
          run a comprehensive scan
        - Locked daily check: once per 24 hours, scan all locked positions
    """

    def __init__(self, holdings_mgr, cache, signals_log,
                 scan_callback: Callable[[list[dict]], None],
                 game_check_callback: Callable[[], bool],
                 paused_check_callback: Callable[[], bool],
                 mode_change_callback: Callable[[str], None] | None = None,
                 db=None,
                 state_path: Path | None = None,
                 event_detector_state_path: Path | None = None,
                 event_detector_log_fn: Callable[[str], None] | None = None):
        """
        Args:
            holdings_mgr: HoldingsManager instance
            cache: DataCacheLayer instance
            signals_log: SignalsLog instance (for the AI's own track record)
            scan_callback: function(events) — called with list of event
                dicts when something fires. Caller does the AI work.
            game_check_callback: function() -> bool — returns True if a
                game is detected (caller maintains state)
            paused_check_callback: function() -> bool — returns True if
                AI is paused
            mode_change_callback: optional function(new_mode) — called when
                the scheduler's mode changes (for UI updates)
            db: optional Database for richer detector state
            state_path: where to persist scheduler state (last brief date,
                last locked check, etc.)
        """
        self.mgr = holdings_mgr
        self.cache = cache
        self.signals_log = signals_log
        self.scan_callback = scan_callback
        self.game_check_callback = game_check_callback
        self.paused_check_callback = paused_check_callback
        self.mode_change_callback = mode_change_callback
        # v4.14.5.14-phantom-news-fix: EventDetector now persists its
        # per-ticker baseline (`_last_state`) to its own sidecar JSON
        # so process restarts don't fire phantom "<total> new news" on
        # every tradable holding. If the caller didn't supply an
        # explicit path, derive a sibling of the Scheduler's
        # state_path (`scheduler_state.json` → `event_detector_state.json`)
        # so the two state stores live together. None ⇒ in-memory
        # only (test path).
        if event_detector_state_path is None and state_path is not None:
            try:
                event_detector_state_path = (
                    state_path.with_name('event_detector_state.json'))
            except Exception:
                event_detector_state_path = None
        self.detector = EventDetector(
            holdings_mgr, cache, db,
            state_path=event_detector_state_path,
            state_log_fn=event_detector_log_fn)

        self.state_path = state_path
        self._sched_state = self._load_state()

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._current_mode = MODE_IDLE
        self._last_tick: float | None = None

    # ─── Persistence ───

    def _load_state(self) -> dict:
        if self.state_path is None or not self.state_path.exists():
            return {}
        try:
            import json
            with open(self.state_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        try:
            import json
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_path, 'w') as f:
                json.dump(self._sched_state, f, indent=2)
        except Exception:
            pass

    # ─── Lifecycle ───

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                         name='TM-Scheduler')
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def current_mode(self) -> str:
        return self._current_mode

    # ─── Main loop ───

    def _run(self):
        # Small initial delay so the app finishes launching before the
        # first tick. Avoids racing with the launch auto-scan.
        if self._stop_event.wait(timeout=15):
            return

        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                pass  # never crash the scheduler thread

            # Sleep based on current mode's cadence. Wake early on stop.
            cadence = cadence_for(self._current_mode)
            if self._stop_event.wait(timeout=cadence):
                return

    def _tick(self):
        """One scheduler iteration: figure out mode, run detector if
        appropriate, dispatch events."""
        # 1. Determine mode
        is_paused = False
        is_gaming = False
        try:
            is_paused = self.paused_check_callback()
        except Exception:
            pass
        try:
            is_gaming = self.game_check_callback()
        except Exception:
            pass

        new_mode = get_schedule_mode(is_paused, is_gaming)
        mode_changed = (new_mode != self._current_mode)
        self._current_mode = new_mode

        if mode_changed and self.mode_change_callback is not None:
            try:
                self.mode_change_callback(new_mode)
            except Exception:
                pass

        # 2. Skip work in modes that don't analyze
        if new_mode in (MODE_IDLE, MODE_PAUSED, MODE_GAMING):
            return

        # 3. Morning brief check (BRIEF mode + haven't run today)
        if new_mode == MODE_BRIEF:
            if self._should_run_morning_brief():
                self._run_morning_brief()
                return

        # 4. Daily locked-position check (once per 24h)
        if self._should_run_locked_check():
            self._run_locked_check()
            # don't return — also do the regular detector this tick

        # 5. Regular event detector
        try:
            events = self.detector.detect(new_mode)
        except Exception:
            events = []

        if events:
            self._dispatch_events(events)

        # v4.14.5.76-adaptive-lane-pacing: piggyback the per-lane
        # adaptive controller on the existing scheduler tick. Cheap —
        # walks two lanes' sliding outcome windows and possibly writes
        # one float to each lane's adapter module. Best-effort: any
        # error here is swallowed so the scheduler thread cannot be
        # taken down by a controller bug. Skip in market-IDLE/PAUSED/
        # GAMING modes (no fill traffic → no signal to act on, and we
        # already returned from those branches above; this code is
        # reached only on tick-active modes).
        try:
            import tm_lane_pacing as _lp
            if _lp.is_enabled():
                _log = getattr(self, '_lane_pacing_log_fn', None)
                _lp.tick(log_fn=_log)
        except Exception:
            pass

    # ─── Special triggers ───

    def _should_run_morning_brief(self) -> bool:
        last = self._sched_state.get('last_morning_brief')
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
            # Run again if it's been a different calendar day in ET
            now_et = _et_now()
            last_et_date = last_dt.date()
            return last_et_date != now_et.date()
        except Exception:
            return True

    def _run_morning_brief(self):
        """The morning brief is just "Check Now on every tradable holding,
        triggered automatically before market open." The AI sees the
        overnight news, the latest prices, etc. and gives a fresh take."""
        tradable = [h for h in self.mgr.holdings if h.get('tradable', True)]
        if not tradable:
            return
        events = [{
            'ticker': h.get('ticker', '?'),
            'reason': 'morning brief — fresh overnight read',
            'severity': 'minor',
            'event_type': 'morning_brief',
        } for h in tradable]

        self._sched_state['last_morning_brief'] = datetime.now().isoformat()
        self._save_state()
        self._dispatch_events(events)

    def _should_run_locked_check(self) -> bool:
        last = self._sched_state.get('last_locked_check')
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
            return (datetime.now() - last_dt).total_seconds() > \
                LOCKED_DAILY_CHECK_HOURS * 3600
        except Exception:
            return True

    def _run_locked_check(self):
        try:
            locked_events = self.detector.detect_locked_changes()
        except Exception:
            locked_events = []
        self._sched_state['last_locked_check'] = datetime.now().isoformat()
        self._save_state()
        if locked_events:
            self._dispatch_events(locked_events)

    # ─── Dispatch ───

    def _dispatch_events(self, events: list[dict]):
        """Hand events to the AI scan path. The callback (in the App)
        runs the analysis on each event's ticker."""
        if not events:
            return
        try:
            self.scan_callback(events)
        except Exception:
            pass


# ─── Game state tracker ────────────────────────────────────────────────

class GameStateTracker:
    """Maintains a rolling assessment of whether the user is gaming.
    Polled by the scheduler. Updated on its own tick (so we don't pay
    the process-list cost more often than needed).

    Two rules:
      - If a known game process is running -> gaming = True
      - Else if AI not currently busy AND GPU >70% sustained -> gaming = True
      - Else gaming = False

    The "AI not currently busy" check is important: if WE are the reason
    the GPU is busy, that's not gaming. The caller passes a callback
    that returns True if AI is currently working.
    """

    POLL_INTERVAL = 60  # seconds between game-detection polls

    def __init__(self, ai_busy_callback: Callable[[], bool],
                 extra_procs_callback: Callable[[], set[str]] | None = None):
        self.ai_busy_callback = ai_busy_callback
        self.extra_procs_callback = extra_procs_callback or (lambda: set())
        self._is_gaming = False
        self._last_poll = 0.0
        self._gpu_busy_streak = 0  # consecutive ticks where GPU was busy
        self._lock = threading.Lock()

    def is_gaming(self) -> bool:
        """Return current gaming state. Polls only every POLL_INTERVAL
        seconds — caller can call this freely without hammering the system.
        """
        now = time.time()
        with self._lock:
            if now - self._last_poll < self.POLL_INTERVAL:
                return self._is_gaming
            self._last_poll = now

        # Layer 1: known game processes
        try:
            games = detect_running_games(self.extra_procs_callback())
        except Exception:
            games = []

        if games:
            with self._lock:
                self._is_gaming = True
                self._gpu_busy_streak = 0
            return True

        # Layer 2: GPU watchdog (only counts if AI isn't the cause)
        ai_busy = False
        try:
            ai_busy = self.ai_busy_callback()
        except Exception:
            pass

        with self._lock:
            if ai_busy:
                # AI is using GPU; can't tell if anything else is
                self._gpu_busy_streak = 0
                self._is_gaming = False
                return False

        try:
            gpu_busy = gpu_busy_externally()
        except Exception:
            gpu_busy = False

        with self._lock:
            if gpu_busy:
                self._gpu_busy_streak += 1
                # Need 2 consecutive busy polls (~2 minutes) to trigger
                # — avoids false positives from brief GPU spikes
                if self._gpu_busy_streak >= 2:
                    self._is_gaming = True
                    return True
            else:
                self._gpu_busy_streak = 0
                self._is_gaming = False

            return self._is_gaming
