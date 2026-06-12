"""
tm_price_service.py — Background price service (v4.13.54)

What this is:
    A background thread that quietly keeps prices fresh for the tickers
    you actually care about. It uses Yahoo for ONE job — bare price
    quotes — and runs continuously while the app is open.

Why this exists:
    The old design was "fetch when asked." Every Look Up, Scan All,
    and refresh would trigger a fresh round of Yahoo calls. When
    multiple operations stacked up, Yahoo throttled us, and you'd hit
    cooldowns that blocked work.

    The new design is "fetch ahead." A background thread keeps prices
    warm for your holdings, watchlist, and recent activity. By the time
    you click Look Up or Scan All, the price is already cached. No
    waiting. No cooldowns. The load on Yahoo is steady and predictable
    instead of bursty.

How it integrates:
    The service writes into the SAME quote_cache.json that DataCacheLayer
    in tm_holdings.py already reads from. That means existing call sites
    DON'T need to change — they just suddenly find the cache always-warm.

    DataCacheLayer's TTL_QUOTE is 60s. The service refreshes at <=60s
    cadence during market hours, so callers will essentially always
    hit a fresh in-memory entry.

What this is NOT:
    - Not an AI router (that's the next project)
    - Not a multi-source data layer (that's the project after)
    - Not a replacement for yahoo_quote() (it CALLS yahoo_quote())
    - Not in scope: history, fundamentals, news. Those keep their old
      paths. This module ONLY handles bare price quotes.

Threading model:
    One worker thread, one watchlist, one cache writer. The thread
    sleeps between cycles. start() and stop() are idempotent. The
    service is safe to leave off entirely — nothing else depends on
    it being running.

— the user doesn't read code. Plain English summary lives at the top of
  the file and in each section comment so future-Claude can navigate.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional


# ─── Tunable cadence ──────────────────────────────────────────────────
#
# These are the "how often do we refresh" intervals, in seconds.
# Market-hours cadence is the most important one — that's when prices
# actually change. Off-hours is mostly to keep the cache from going
# completely stale, but isn't worth burning bandwidth over.

CADENCE_MARKET_OPEN_SEC      = 60     # 1 min during regular market hours
CADENCE_EXTENDED_HOURS_SEC   = 300    # 5 min during pre/after-market
CADENCE_CLOSED_SEC           = 1800   # 30 min when market closed

# Maximum tickers to ask Yahoo about in one batch call. Higher = fewer
# calls but more risk of one big call being slow or throttled. 50 is
# what tm_discover.batch_fetch_quotes uses, so we match it.
BATCH_SIZE = 50

# How long a "recently looked up" ticker stays on the active list,
# in seconds. After this it falls off (assuming it's not in holdings
# or watchlist where it lives forever).
RECENT_LOOKUP_TTL_SEC   = 3600        # 1 hour
RECENT_SCAN_TTL_SEC     = 6 * 3600    # 6 hours

# How long to sleep between cycles when the watchlist is empty.
# Avoids spinning when there's literally nothing to do.
IDLE_SLEEP_SEC = 30


# ─── Module-level state ───────────────────────────────────────────────
#
# We keep one global service instance because there's only ever one app
# instance and one set of holdings/watchlist. Callers use the
# module-level start()/stop()/track() functions; the singleton is an
# implementation detail.

_service: Optional['PriceService'] = None
_service_lock = threading.Lock()


# ─── Helper: market hours awareness ────────────────────────────────────
#
# Mirrors the logic in tired_market.py. Duplicated here so this module
# stays standalone-importable (no circular import risk).

def _et_now() -> datetime:
    utc_now = datetime.now(timezone.utc)
    month = utc_now.month
    offset = timedelta(hours=-4 if 3 <= month <= 11 else -5)
    return utc_now + offset


def _market_phase() -> str:
    """Returns one of: 'open', 'extended', 'closed'."""
    et = _et_now()
    if et.weekday() >= 5:
        return 'closed'
    h, m = et.hour, et.minute
    # Regular session: 9:30 AM - 4:00 PM ET
    if (h == 9 and m >= 30) or (10 <= h < 16):
        return 'open'
    # Pre-market 4:00 AM - 9:30 AM, after-hours 4:00 PM - 8:00 PM
    if 4 <= h < 9 or (h == 9 and m < 30) or 16 <= h < 20:
        return 'extended'
    return 'closed'


def _cadence_for_phase(phase: str) -> int:
    return {
        'open':     CADENCE_MARKET_OPEN_SEC,
        'extended': CADENCE_EXTENDED_HOURS_SEC,
        'closed':   CADENCE_CLOSED_SEC,
    }.get(phase, CADENCE_CLOSED_SEC)


# ─── The service itself ───────────────────────────────────────────────

class PriceService:
    """Background thread that keeps prices fresh for active tickers.

    Design notes for future-me:

    - Caller injects everything we need (fetch function, cache writer,
      watchlist accessors). This module knows NOTHING about specific
      classes elsewhere in the app. Loose coupling = easy to test, easy
      to refactor later, and easy to swap Yahoo for another source.

    - The "watch list" is computed fresh each cycle from a callback the
      caller provides. That callback should return a list of tickers
      this cycle should refresh. We keep our own short-lived "recent"
      list inside the service so callers can call track_lookup() and
      track_scan() to register transient interest in tickers.

    - We never hold the lock during a network call. The lock guards
      our own data structures only. Yahoo can be slow; we don't want
      to block other threads waiting on it.

    - Errors are swallowed and logged. The service NEVER crashes the
      app. If Yahoo is down, the cache just doesn't refresh — old
      values stay valid until they expire normally.
    """

    def __init__(self,
                 batch_fetch_fn: Callable[[list[str]], dict[str, dict]],
                 cache_writer: Callable[[str, dict], None],
                 get_static_tickers_fn: Callable[[], list[str]],
                 log_fn: Optional[Callable[[str, str], None]] = None,
                 is_paused_fn: Optional[Callable[[], bool]] = None,
                 ):
        """
        Args:
            batch_fetch_fn:   func(tickers) -> {TICKER: quote_dict}
                              Should match tm_discover.batch_fetch_quotes
                              signature (ignoring optional progress/cancel).
            cache_writer:     func(ticker, quote_dict). Called for every
                              successfully-fetched ticker. Caller's job
                              is to put the data wherever DataCacheLayer
                              looks for it (typically: seed the same
                              in-memory cache that handles 'quote' kind).
            get_static_tickers_fn: func() -> [ticker, ...]. Returns the
                              "always watch" set: holdings + watchlist.
                              Called fresh every cycle so additions
                              propagate quickly.
            log_fn:           Optional func(message, color). Same shape
                              as the main app's _log().
            is_paused_fn:     Optional func() -> bool. If returns True,
                              the service skips that cycle. Lets us
                              respect the existing AI-paused flag.
        """
        self._batch_fetch = batch_fetch_fn
        self._cache_writer = cache_writer
        self._get_static_tickers = get_static_tickers_fn
        self._log = log_fn
        self._is_paused = is_paused_fn

        # Recent-interest tracking — these are tickers the user touched
        # via Look Up or that came out of a recent scan. They stay on
        # the active list for a TTL, then drop off.
        # Key: ticker (uppercase). Value: (kind, expires_at_epoch).
        self._recent: dict[str, tuple[str, float]] = {}
        self._recent_lock = threading.Lock()

        # Thread management
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._started_at: Optional[float] = None

        # Diagnostics — exposed via stats() for the UI to show.
        self._cycles_run = 0
        self._last_cycle_at: Optional[float] = None
        self._last_cycle_count = 0  # tickers refreshed in last cycle
        self._last_cycle_duration_sec = 0.0
        self._last_error: Optional[str] = None
        self._total_quotes_fetched = 0

    # ─── Public lifecycle ────────────────────────────────────────────

    def start(self) -> None:
        """Start the background thread. Idempotent — calling twice is
        a no-op."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._started_at = time.time()
        self._thread = threading.Thread(
            target=self._run,
            name='PriceService',
            daemon=True,
        )
        self._thread.start()
        self._note("Price service started", 'muted')

    def stop(self, timeout_sec: float = 5.0) -> None:
        """Signal the thread to stop and wait for it to exit."""
        self._stop_event.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout_sec)
        self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ─── Public registration API ─────────────────────────────────────

    def track_lookup(self, ticker: str) -> None:
        """Mark a ticker as recently looked up. Stays on the active
        watch list for RECENT_LOOKUP_TTL_SEC. Idempotent / overwrite —
        calling again resets the TTL."""
        if not ticker:
            return
        ticker = ticker.strip().upper()
        if not ticker:
            return
        with self._recent_lock:
            self._recent[ticker] = ('lookup', time.time() + RECENT_LOOKUP_TTL_SEC)

    def track_scan(self, tickers: list[str]) -> None:
        """Mark scan-survivor tickers as worth watching for a while.
        Stays active for RECENT_SCAN_TTL_SEC."""
        if not tickers:
            return
        expires = time.time() + RECENT_SCAN_TTL_SEC
        with self._recent_lock:
            for t in tickers:
                if not t:
                    continue
                tk = t.strip().upper()
                if tk:
                    self._recent[tk] = ('scan', expires)

    def stats(self) -> dict:
        """Snapshot of current service state for diagnostics / UI."""
        with self._recent_lock:
            recent_count = len(self._recent)
        try:
            static_count = len(self._get_static_tickers() or [])
        except Exception:
            static_count = 0
        return {
            'running': self.is_running(),
            'started_at': self._started_at,
            'cycles_run': self._cycles_run,
            'last_cycle_at': self._last_cycle_at,
            'last_cycle_count': self._last_cycle_count,
            'last_cycle_duration_sec': round(self._last_cycle_duration_sec, 2),
            'total_quotes_fetched': self._total_quotes_fetched,
            'static_tickers': static_count,
            'recent_tickers': recent_count,
            'last_error': self._last_error,
            'phase': _market_phase(),
            'next_cadence_sec': _cadence_for_phase(_market_phase()),
        }

    # ─── Internal: the worker loop ───────────────────────────────────

    def _run(self) -> None:
        """The main loop. Runs until stop_event is set."""
        # Small initial delay so the app finishes loading before we
        # start hitting the network.
        if self._stop_event.wait(timeout=2.0):
            return

        while not self._stop_event.is_set():
            try:
                cadence = self._do_one_cycle()
            except Exception as e:
                # Log and back off. Never crash the thread.
                self._last_error = f"{type(e).__name__}: {e}"
                self._note(f"Price service error: {e}", 'amber')
                cadence = CADENCE_CLOSED_SEC  # back off on error

            # Sleep until the next cycle, but wake immediately on stop.
            if self._stop_event.wait(timeout=cadence):
                break

        self._note("Price service stopped", 'muted')

    def _do_one_cycle(self) -> int:
        """Run one refresh cycle. Returns the cadence (sleep seconds)
        to use before the next cycle."""
        # Honor the global pause flag if the caller wired one up.
        if self._is_paused is not None:
            try:
                if self._is_paused():
                    return IDLE_SLEEP_SEC
            except Exception:
                pass

        # Build the active watch list for this cycle.
        tickers = self._build_active_set()
        if not tickers:
            return IDLE_SLEEP_SEC

        # Decide cadence based on market phase. We compute it BEFORE
        # the fetch so it reflects the moment the cycle started.
        phase = _market_phase()
        cadence = _cadence_for_phase(phase)

        # Do the fetch in chunks of BATCH_SIZE.
        cycle_start = time.time()
        fetched = 0
        for i in range(0, len(tickers), BATCH_SIZE):
            if self._stop_event.is_set():
                break
            chunk = tickers[i:i + BATCH_SIZE]
            try:
                results = self._batch_fetch(chunk) or {}
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                continue
            # Hand each result to the cache writer.
            for tk, quote in results.items():
                if not quote:
                    continue
                try:
                    self._cache_writer(tk, quote)
                    fetched += 1
                except Exception:
                    # Cache writer failed — ignore, keep going.
                    pass

        duration = time.time() - cycle_start

        # Update diagnostics.
        self._cycles_run += 1
        self._last_cycle_at = time.time()
        self._last_cycle_count = fetched
        self._last_cycle_duration_sec = duration
        self._total_quotes_fetched += fetched
        if fetched > 0:
            self._last_error = None

        return cadence

    def _build_active_set(self) -> list[str]:
        """Combine the always-watch set (holdings + watchlist) with
        the transient recent-interest set, deduped and uppercased.

        Also: prunes expired entries from the recent set so it doesn't
        grow forever."""
        # Static set (caller refreshes their own data internally —
        # we just call the accessor each cycle).
        try:
            static = self._get_static_tickers() or []
        except Exception:
            static = []

        # Recent set, with expiry pruning.
        now = time.time()
        with self._recent_lock:
            expired = [tk for tk, (_, exp) in self._recent.items() if exp < now]
            for tk in expired:
                self._recent.pop(tk, None)
            recent = list(self._recent.keys())

        # Combine + dedupe (preserve order: static first, then recent).
        seen: set[str] = set()
        out: list[str] = []
        for tk in static:
            if not tk:
                continue
            tk = tk.strip().upper()
            if tk and tk not in seen:
                seen.add(tk)
                out.append(tk)
        for tk in recent:
            if tk and tk not in seen:
                seen.add(tk)
                out.append(tk)
        return out

    def _note(self, msg: str, color: str = 'muted') -> None:
        """Best-effort log call. Swallows errors."""
        if self._log is None:
            return
        try:
            self._log(msg, color)
        except Exception:
            pass


# ─── Module-level convenience API ─────────────────────────────────────
#
# The main app calls these. There's exactly one service per app.

def init(batch_fetch_fn: Callable[[list[str]], dict[str, dict]],
         cache_writer: Callable[[str, dict], None],
         get_static_tickers_fn: Callable[[], list[str]],
         log_fn: Optional[Callable[[str, str], None]] = None,
         is_paused_fn: Optional[Callable[[], bool]] = None,
         ) -> PriceService:
    """Create the singleton service. Safe to call multiple times — only
    the first call constructs; later calls return the existing service."""
    global _service
    with _service_lock:
        if _service is None:
            _service = PriceService(
                batch_fetch_fn=batch_fetch_fn,
                cache_writer=cache_writer,
                get_static_tickers_fn=get_static_tickers_fn,
                log_fn=log_fn,
                is_paused_fn=is_paused_fn,
            )
    return _service


def get_service() -> Optional[PriceService]:
    """Return the singleton service, or None if init() hasn't been
    called yet."""
    return _service


def start() -> None:
    """Start the service if it has been init'd. No-op otherwise."""
    s = _service
    if s is not None:
        s.start()


def stop(timeout_sec: float = 5.0) -> None:
    """Stop the service if running. No-op otherwise."""
    s = _service
    if s is not None:
        s.stop(timeout_sec=timeout_sec)


def track_lookup(ticker: str) -> None:
    """Convenience: tell the service the user just looked up this ticker.
    The service will keep its price warm for the next hour."""
    s = _service
    if s is not None:
        s.track_lookup(ticker)


def track_scan(tickers: list[str]) -> None:
    """Convenience: tell the service these are recent scan survivors.
    The service will keep their prices warm for the next 6 hours."""
    s = _service
    if s is not None:
        s.track_scan(tickers)


def stats() -> dict:
    """Return diagnostic snapshot, or empty dict if service not init'd."""
    s = _service
    if s is None:
        return {'running': False, 'reason': 'not initialized'}
    return s.stats()
