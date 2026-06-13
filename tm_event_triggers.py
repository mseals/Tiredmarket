"""tm_event_triggers — event-driven refresh skeleton (v4.14.4.0).

The architectural reframing from IDEAS.md "Event-driven refresh model":
the queue runner stops being a 15-min cadence "re-analyze 20 tickers
every pass" loop, and becomes an event-driven "watch for changes,
re-analyze only when something specific happened" loop.

v4.14.4.0 shipped the skeleton + staleness; v4.14.4.1 added price-drift
+ target_stop; v4.14.4.2 added news; v4.14.4.3 (this version) adds
earnings (combined kind, upcoming/recent subkinds) and user-signal
(push-based via in-memory pending list + _trigger_wake_event).
Default-flip soak completion comes in v4.14.4.4.

Coexistence model: cfg['event_driven_refresh'] defaults False. The
v4.14.3.x cadence-based behavior runs unchanged when the flag is
False. When True (manually flipped or auto-flipped after 14-day
soak), tm_queue_runner switches to the trigger-driven path that
uses helpers in this module.

Module contents:
  - Per-path staleness window constants
  - Cascading-trigger cooldown (5 min hard floor per (ticker, path))
  - Storm cap + priority order for trigger fires
  - Auto-flip soak window (14 days from installed_at)
  - check_staleness_triggers — the only live trigger evaluator in v4.14.4.0
  - record_trigger_fire — writes to trigger_fire_log table
  - prioritize_and_cap_fires — applies priority + storm cap
  - should_auto_flip — checks the soak window
  - compute_backoff_sleep — tiered backoff for the runner's sleep
"""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Optional


# ─── Constants ────────────────────────────────────────────────────────

# v4.14.4.1 (2026-05-15): per-path price-drift thresholds. Compared
# against |drift| (unsigned magnitude) where drift =
# (current / baseline) - 1.0. Up-moves and down-moves are equally
# newsworthy for re-analysis — direction goes into signal_context
# for the prompt to reason about.
#
# Reasoning:
#   slow_safe (8%): blue-chip moves; 8% is "real news" territory
#   moderate (5%): standard meaningful intraday move
#   aggressive (4%): aggressive plays react fast; tighter trigger
#   lottery (6%): $1-$10 names are volatile; 6% filters daily noise
#   penny_lottery (10%): sub-dollar names routinely move 10-30%
PRICE_DRIFT_THRESHOLDS_PCT = {
    'slow_safe':     0.08,
    'moderate':      0.05,
    'aggressive':    0.04,
    'lottery':       0.06,
    'penny_lottery': 0.10,
}

# v4.14.4.1: per-kind dedup windows (seconds). The 5-min cascading
# cooldown (CASCADING_COOLDOWN_SECONDS, below) is the absolute floor
# per (ticker, path) regardless of kind. The per-kind window is
# the additional dedup applied to a specific (ticker, path, kind)
# tuple. Both apply; either suppression blocks the fire.
#
# Reasoning:
#   target_stop: 5 min — high-signal; let it re-fire fast if price
#     keeps moving past the threshold
#   price_drift: 30 min — oscillation suppression. If a stock
#     hovers around the threshold, we don't want to re-analyze every
#     time it crosses by a fraction
#
# Future kinds (v4.14.4.2 news, v4.14.4.3 earnings + user-signal)
# extend this dict; missing keys default to "no per-kind dedup,
# cascading floor still applies."
PER_KIND_DEDUP_WINDOWS_SECONDS = {
    'target_stop': 5 * 60,
    'price_drift': 30 * 60,
    # v4.14.4.2 (2026-05-15): news. 60min balances "news clusters
    # arrive in waves; re-firing on each wave is noise" vs "new news
    # event 30 min after a previous one might be the second
    # breaking development worth re-analyzing."
    'news':        60 * 60,
    # v4.14.4.3 (2026-05-15): earnings. 6 hours. Earnings are
    # quarterly events; re-firing within hours is noise. Combined
    # with the cascading floor, this means a ticker fires AT MOST
    # ~4 times in the 24h before/after a report.
    'earnings':    6 * 60 * 60,
    # v4.14.4.3 (2026-05-15): user signals. 60s for push-time dedup
    # against UI fumbles (add/remove/re-add within one second). The
    # in-memory pending list also enforces this at push time so
    # double-pushes never reach the sweep at all; this DB-side
    # window covers cross-sweep dedup after a fire has been
    # recorded.
    'user':        60,
    # v4.14.5.3: suspicion-staleness. Once a quiet BUY has fired a
    # re-check, don't re-fire for 24h — the re-analysis itself (or a
    # genuine change event) resets the silence clock anyway.
    'staleness':   24 * 60 * 60,
    # v4.14.5.83-leading-signals (volume-accumulation discovery):
    # 6 hours. Accumulation patterns play out over hours/days, not
    # minutes — re-firing within hours is noise. The persistence
    # window is shorter than insider_buy because the daily-bars-
    # derived signal lives or dies day-by-day.
    'volume_accumulation': 6 * 60 * 60,
    # v4.14.5.83-leading-signals (insider-buy discovery): 7 days.
    # Form-4 filings are durable signals; once we've scanned on the
    # strength of an insider buy, the same row in `insider_flow`
    # shouldn't keep re-firing every sweep. A NEW insider buy
    # later (which would update insider_flow.computed_at) IS
    # treated as a new signal by the freshness gate inside the
    # trigger itself, so the 7-day window is just the floor.
    'insider_buy': 7 * 24 * 60 * 60,
    # v4.14.5.82-discovery-unlock: fresh-universe-mover. Same shape
    # as the news/earnings dedup pattern; 6h matches the natural
    # cadence of a price-drift discovery (a move worth scanning
    # twice in one sweep is the rare exception).
    'fresh_universe_mover': 6 * 60 * 60,
}


# v4.14.4.2 (2026-05-15): per-path news trigger thresholds.
# Format: (article_count, max_age_hours).
#
# Fire condition:
#   COUNT(articles since max(last_analyzed_at, now - max_age_hours))
#     >= article_count
#
# The max_age_hours floor is LOAD-BEARING — without it, never-
# analyzed tickers fire on weeks-old news. The anchor uses MAX of
# (last_analyzed_at, max_age_cutoff) so we always exclude ancient
# articles even if no analysis baseline exists for this path.
#
# Reasoning:
#   slow_safe: needs a meaningful cluster (5 articles in 3 days)
#   moderate: mid-band (3 articles in 2 days)
#   aggressive: tighter (2 articles in 1 day)
#   lottery/penny_lottery: thin names where any single article matters
NEWS_TRIGGER_THRESHOLDS = {
    'slow_safe':     (5, 72),
    'moderate':      (3, 48),
    'aggressive':    (2, 24),
    'lottery':       (1, 24),
    'penny_lottery': (1, 24),
}


# v4.14.4.3 (2026-05-15): per-path earnings trigger windows.
# Format: (upcoming_days, recent_days).
#
# Fire conditions (combined kind, subkind in signal_context):
#   subkind = 'upcoming' if 0 <= days_until <= upcoming_days
#   subkind = 'recent'   if 0 <  days_since <= recent_days
#
# Reasoning:
#   slow_safe: conservative — fires only when an event is close
#   moderate: standard band
#   aggressive / lottery: wider — these paths react fast to events
#   penny_lottery: widest — earnings data is sparse for penny names,
#     so when we DO have a date, treat it as load-bearing
EARNINGS_TRIGGER_WINDOWS = {
    'slow_safe':     (3, 2),
    'moderate':      (5, 3),
    'aggressive':    (7, 5),
    'lottery':       (7, 5),
    'penny_lottery': (14, 7),
}


# v4.14.4.3 (2026-05-15): in-memory pending list for push-based user
# signals (watchlist_add, position_open). Producers append from UI
# threads via record_user_signal(); the runner thread drains in the
# event-driven sweep via drain_user_signals(). Thread-safe via lock.
#
# Crash-loses-signal accepted: pending entries die with the process.
# Worst case the user adds a ticker, the app crashes within 60s, and
# the trigger never fires — staleness picks it up on the next sweep
# after restart. Persisting these would add complexity without
# meaningful payoff.
#
# Module-level app reference (set by tired_market.py at App init via
# set_app(app)) lets UI callers invoke record_user_signal(ticker,
# action) without plumbing `app` through Watchlist / HoldingsManager.
# Falls back to no-op if app hasn't been registered yet (e.g.,
# during early-startup ticker imports that race init).
_pending_user_signals: list = []
_pending_user_signals_lock = threading.Lock()
_app_ref = None  # set by set_app(); read by record_user_signal()


def set_app(app) -> None:
    """v4.14.4.3: register the App reference for module-level
    user-signal recording. Called once during App.__init__ so UI
    code can call record_user_signal(ticker, action) without
    plumbing `app` through every UI layer."""
    global _app_ref
    _app_ref = app


# Per-path staleness windows in seconds. Constants intentionally —
# moving to cfg fields is YAGNI for now (no user has asked to tune).
# Future patch can promote if needed.
#
# Reasoning:
#   slow_safe: long-duration thesis; weekly re-evaluation is plenty
#   moderate: two-day cadence catches news + earnings + price drift
#   aggressive: short-term plays; half-day staleness
#   lottery: high-volatility names; fresh reads matter
#   penny_lottery: highest noise; freshest reads most often
STALENESS_WINDOWS_SECONDS = {
    'slow_safe':     7 * 24 * 60 * 60,  # 7 days
    'moderate':      48 * 60 * 60,       # 48 hours
    'aggressive':    12 * 60 * 60,       # 12 hours
    'lottery':       6 * 60 * 60,        # 6 hours
    'penny_lottery': 4 * 60 * 60,        # 4 hours
}

# v4.14.5.2: Extended windows align with the v4.14.5.1 maturity guard.
# Step 4 (suspicion-staleness, future patch) will reimplement these
# constants with different semantics ("BUY has been silent too long")
# rather than "data is old, re-analyze." Until then these widened
# windows reduce wasted runner work without affecting picks (the
# maturity guard already protects every BUY for >= 3 days regardless
# of how often staleness fires). Selected when
# cfg['use_stable_recommend'] is True (the default); the legacy dict
# above is restored when the flag is False.
STALENESS_WINDOWS_SECONDS_STABLE = {
    # v4.14.6.0-price-band-tiers: price doesn't dictate a clock, so the
    # new bands use a single uniform 5-day re-look cadence. Per-band
    # tuning can come later; uniform here is the "minimum revert" the
    # spec asks for. Legacy time-path keys remain as aliases so any
    # call site still passing them resolves to the same value.
    'lottery':       5 * 24 * 60 * 60,  # 5 days
    'band_5_10':     5 * 24 * 60 * 60,
    'band_10_50':    5 * 24 * 60 * 60,
    'band_50_up':    5 * 24 * 60 * 60,
    # Legacy aliases (drop once every call site is re-keyed).
    'aggressive':    5 * 24 * 60 * 60,
    'moderate':      5 * 24 * 60 * 60,
    'slow_safe':     5 * 24 * 60 * 60,
    'penny_lottery': 5 * 24 * 60 * 60,
}


def verdict_recency_window_seconds(app, path: str) -> int:
    """v4.14.5.14a.9: the "a verdict this fresh is still valid" window
    for a path, used by the fill-mode verdict-recency skip gate
    (tm_queue_runner._recently_judged_set).

    Reuses the SAME clock-staleness window selection the legacy
    staleness trigger uses — STALENESS_WINDOWS_SECONDS_STABLE when
    cfg['use_stable_recommend'] is True (the default), the legacy
    tighter dict otherwise. Single source of truth so the skip gate
    can never drift from the rest of the staleness machinery.

    Semantics match clock-staleness, NOT suspicion-staleness: "the
    verdict is younger than the window → it still stands, don't
    re-analyze." Defaults to the 'moderate' window for any unknown
    path. Never raises (fail-safe to 'moderate' stable on error)."""
    try:
        stable = bool(getattr(app, 'cfg', {}).get(
            'use_stable_recommend', True))
    except Exception:
        stable = True
    windows = (STALENESS_WINDOWS_SECONDS_STABLE if stable
               else STALENESS_WINDOWS_SECONDS)
    try:
        return int(windows.get(path, windows['moderate']))
    except Exception:
        return int(STALENESS_WINDOWS_SECONDS_STABLE['moderate'])

# v4.14.5.3 (Step 4): SUSPICION-staleness windows, in DAYS. This is a
# different concept from the clock-driven windows above. It does NOT
# mean "the cached data is old." It means "this BUY has had ZERO
# real-world change events (price move, news, consensus, earnings,
# user action) for this many days — the silence itself is suspicious,
# re-check the thesis." Shorter paths expect faster activity, so a
# shorter silence is already suspicious there. Tunable.
SUSPICION_STALENESS_WINDOWS_DAYS = {
    # v4.14.6.0-price-band-tiers: uniform 14-day suspicion-silence
    # threshold across the new bands. Per-band tuning can come later.
    # Legacy keys remain as aliases.
    'lottery':       14,
    'band_5_10':     14,
    'band_10_50':    14,
    'band_50_up':    14,
    'aggressive':    14,
    'moderate':      14,
    'slow_safe':     14,
    'penny_lottery': 14,
}


def apply_path_merge_v414514mu(cfg: dict | None = None) -> bool:
    """v4.14.5.14-merge-and-unify (2026-05-19): the event-triggers
    half of the penny_lottery→lottery merge. When
    cfg['use_path_merge'] is True (default):
      - lottery's suspicion-staleness window aligns with the
        moderate path's window (Mike's articulated weeks-of-sweating
        holding pattern is structurally moderate-pace; SINGLE
        SOURCE OF TRUTH = SUSPICION_STALENESS_WINDOWS_DAYS
        ['moderate'], so future bumps to moderate automatically
        carry forward — no separate '14' hardcoded here).
      - penny_lottery is popped from every per-path dict in this
        module (price-drift, news, earnings, heartbeat, user-signal,
        suspicion).
    Idempotent: re-runs detect via 'penny_lottery' not in
    SUSPICION_STALENESS_WINDOWS_DAYS. Returns True only on first
    successful application this process."""
    if not bool((cfg or {}).get('use_path_merge', True)):
        return False
    if 'penny_lottery' not in SUSPICION_STALENESS_WINDOWS_DAYS:
        return False  # already merged this process
    SUSPICION_STALENESS_WINDOWS_DAYS['lottery'] = (
        SUSPICION_STALENESS_WINDOWS_DAYS['moderate'])
    for d in (PRICE_DRIFT_THRESHOLDS_PCT, NEWS_TRIGGER_THRESHOLDS,
              EARNINGS_TRIGGER_WINDOWS, STALENESS_WINDOWS_SECONDS,
              STALENESS_WINDOWS_SECONDS_STABLE,
              SUSPICION_STALENESS_WINDOWS_DAYS):
        try:
            d.pop('penny_lottery', None)
        except Exception:
            pass
    return True

# "Significant price move" thresholds (fraction) that count as a
# change event, by track. Speculative names move more on noise, so a
# bigger move is required before it counts as a real event.
_SUSPICION_PRICE_MOVE_MAIN = 0.03
_SUSPICION_PRICE_MOVE_SPECULATIVE = 0.05

# F3 cascading-trigger cooldown: don't re-fire ANY trigger for a
# (ticker, path) if it was analyzed within the last 5 minutes. Prevents
# the "analyze X, X moves slightly, fire price trigger, analyze X
# again, X moves again..." cascade. Hard floor regardless of trigger
# kind (except user-initiated which always fires).
CASCADING_COOLDOWN_SECONDS = 5 * 60

# F1 storm cap: maximum fires processed per sweep. Excess overflows
# into the next sweep. In v4.14.4.0 only staleness fires exist so
# this is mostly theoretical; takes effect when other trigger types
# join.
STORM_FIRE_CAP = 20

# Priority order. Lower index = higher priority. v4.14.4.0 only has
# 'staleness' in play; the rest are placeholders that get wired by
# subsequent patches.
#
# v4.14.5.82-discovery-unlock: 'fresh_universe_mover' added at the
# END (lowest priority) — discovery fires only consume sweep capacity
# that genuine signal triggers leave unused. They're the background
# filler, not the headliner. STORM_FIRE_CAP=20 still bounds the
# global total.
#
# v4.14.5.83-leading-signals: two leading-signal discovery kinds
# slotted between real signal triggers and the v.82 price-drift
# discovery. Within the discovery class, ordering reflects signal
# strength: an insider opening their wallet (insider_buy) is the
# strongest leading signal we can read; unusual volume without price
# (volume_accumulation) is next; raw price-drift discovery
# (fresh_universe_mover) is the weakest because the move has already
# started. All three are below the real signal triggers (user /
# target_stop / earnings / news / price_drift) so they never displace
# a genuine event in the storm-cap budget.
TRIGGER_PRIORITY = [
    'user',                   # explicit user-initiated; always wins
    'target_stop',            # price hit target/stop on an active BUY
    'earnings',               # earnings event close
    'news',                   # news arrival
    'price_drift',            # price drifted past threshold
    'staleness',              # catch-all
    'insider_buy',            # v.83: Form-4 open-market buy (leading)
    'volume_accumulation',    # v.83: volume w/o price (leading)
    'fresh_universe_mover',   # v.82: never-analyzed mover (trailing)
]


# v4.14.5.82-discovery-unlock: parameters for the new discovery trigger.
#
# Movement threshold: minimum |drift| over the LOOKBACK_DAYS trading-
# day window for a never-analyzed ticker to earn a fire. 5% catches
# real moves while filtering daily noise. Per-path-style thresholds
# are NOT applied here — discovery is path-agnostic at the trigger
# stage; the path assignment (lottery vs aggressive) happens by price.
FRESH_MOVER_MIN_DRIFT_PCT = 0.05      # 5% absolute drift
FRESH_MOVER_LOOKBACK_DAYS = 5         # 5 trading-day window
# Hard internal cap so discovery never floods the storm-cap budget
# (which is the global cross-kind total). Real triggers fire first
# via prioritize_and_cap_fires; whatever's left of STORM_FIRE_CAP=20
# is available for discovery, up to this cap.
FRESH_MOVER_PER_SWEEP_CAP = 10
# Price split for path assignment. <$5 → lottery; otherwise aggressive.
# These two paths have DYNAMIC pools (price-filtered universe) so the
# Part-B pool-gate carve-out can route discovery fires safely; the
# curated seed-list paths (slow_safe / moderate) are unaffected.
FRESH_MOVER_LOTTERY_MAX_PRICE = 5.0
# Module toggle. Default True; flag-off → trigger returns []
# (no fires, exact pre-v.82 behavior). App init flips this from
# cfg['use_fresh_universe_mover'].
_FRESH_MOVER_ENABLED = True


def set_fresh_universe_mover_enabled(enabled: bool) -> None:
    """Master toggle for the v.82 discovery trigger. False = no
    fresh_universe_mover fires emitted (legacy pre-v.82 behavior)."""
    global _FRESH_MOVER_ENABLED
    _FRESH_MOVER_ENABLED = bool(enabled)


def is_fresh_universe_mover_enabled() -> bool:
    return _FRESH_MOVER_ENABLED


# v4.14.5.83-leading-signals: parameters for the new leading-signal
# triggers.
#
# Volume-accumulation: fires when latest-day volume is unusually high
# relative to the trailing average AND the price hasn't already moved
# meaningfully (the "without price" half — if price already drifted,
# fresh_universe_mover catches it; this trigger is specifically the
# QUIET accumulation signature where someone is buying before price
# reacts).
VOLUME_ACCUMULATION_RATIO_THRESHOLD = 2.5    # latest vol / avg vol
VOLUME_ACCUMULATION_AVG_LOOKBACK_DAYS = 10   # trailing-avg window
VOLUME_ACCUMULATION_PRICE_MAX_ABS_DRIFT = 0.03  # |drift| < 3%
VOLUME_ACCUMULATION_PER_SWEEP_CAP = 8
# Insider-buy: fires when `insider_flow.net_open_market_usd > 0` AND
# at least one open-market BUY (n_buys >= 1) AND the computed_at is
# fresh enough that this is "this week" not "last quarter."
INSIDER_BUY_MIN_NET_USD = 0.0           # any net-positive
INSIDER_BUY_MIN_BUYS = 1                # at least one open-market BUY
INSIDER_BUY_MAX_AGE_DAYS = 14           # signal considered fresh ≤ 14d
INSIDER_BUY_PER_SWEEP_CAP = 6

# Module toggle for BOTH leading-signal kinds. Default True; flag-off
# → both triggers return [] immediately (legacy pre-v.83 behavior).
# Independent of `_FRESH_MOVER_ENABLED` so the v.82 fresh_universe_
# mover can be on/off separately.
_LEADING_SIGNALS_ENABLED = True


def set_leading_signal_triggers_enabled(enabled: bool) -> None:
    """Master toggle for the v.83 leading-signal triggers
    (volume_accumulation + insider_buy). False = neither emits fires
    (legacy pre-v.83 behavior)."""
    global _LEADING_SIGNALS_ENABLED
    _LEADING_SIGNALS_ENABLED = bool(enabled)


def is_leading_signal_triggers_enabled() -> bool:
    return _LEADING_SIGNALS_ENABLED

# Auto-flip soak window: if cfg['event_driven_refresh'] is False
# AND it's been 14+ days since v4.14.4.0 first ran, auto-flip to True.
AUTO_FLIP_DAYS = 14
AUTO_FLIP_SECONDS = AUTO_FLIP_DAYS * 24 * 60 * 60

# Backoff tiers (seconds) keyed by consecutive_empty count.
BACKOFF_TIERS = (
    (3, 60),    # 0-2 empty sweeps -> 60s
    (6, 120),   # 3-5 -> 2 min
    (9, 300),   # 6-8 -> 5 min
    (None, 600),  # 9+ -> 10 min
)


# ─── DB helper (shared with tm_queue_runner pattern) ──────────────────

def _conn(app):
    """Return the App's SQLite connection, or None. Same pattern as
    tm_queue_runner._conn."""
    for attr in ('db', '_db', 'database'):
        db = getattr(app, attr, None)
        if db is not None:
            return getattr(db, 'conn', None)
    return None


from contextlib import contextmanager as _cm   # noqa: E402  (used by _db_lock)


@_cm
def _db_lock(app):
    """v4.14.5.14-db-concurrency: acquire app.db.lock for a block of SQL on
    app.db.conn. The Connection is opened `check_same_thread=False`, so
    SQLite itself requires serialization across threads — without this,
    concurrent users return `SQLITE_MISUSE: bad parameter or other API
    misuse`. Fail-OPEN if the lock isn't reachable (preserves headless-test
    behaviour with a stub app)."""
    lock = None
    try:
        lock = getattr(getattr(app, 'db', None), 'lock', None)
    except Exception:
        lock = None
    if lock is None:
        yield
        return
    with lock:
        yield


def _log_amber(app, msg: str) -> None:
    """Same pattern as tm_queue_runner's amber log helper. Uses the
    App's _log if available, falls back to stderr."""
    try:
        log = getattr(app, '_log', None)
        root = getattr(app, 'root', None)
        if callable(log):
            if root is not None:
                root.after(0, lambda m=msg: log(m, 'amber'))
            else:
                log(msg, 'amber')
            return
    except Exception:
        pass
    try:
        import sys
        print(f"[tm_event_triggers] {msg}", file=sys.stderr)
    except Exception:
        pass


def _log_muted(app, msg: str) -> None:
    """Muted log via App._log; stderr fallback."""
    try:
        log = getattr(app, '_log', None)
        root = getattr(app, 'root', None)
        if callable(log):
            if root is not None:
                root.after(0, lambda m=msg: log(m, 'muted'))
            else:
                log(msg, 'muted')
            return
    except Exception:
        pass
    try:
        import sys
        print(f"[tm_event_triggers] {msg}", file=sys.stderr)
    except Exception:
        pass


# ─── Staleness trigger ───────────────────────────────────────────────

def _check_legacy_staleness_triggers(app, path: str,
                                      now_ts: Optional[int] = None
                                      ) -> list:
    """LEGACY clock-driven "data is old, re-analyze" staleness.
    v4.14.5.3: retired as the default — superseded by
    _check_suspicion_staleness_triggers. Kept behind
    cfg['use_suspicion_staleness']=False for rollback only; a later
    cleanup patch removes it once suspicion-staleness is proven.

    Find tickers whose last analysis is older than the path's
    staleness window AND not within the 5-min cascading cooldown.

    Returns list of (ticker, signal_context_dict) tuples.

    Signal context for staleness fires:
        {"kind": "staleness", "last_analyzed_at": <ts>,
         "age_seconds": <int>, "window_seconds": <int>}

    Defensive: a DB read failure logs amber and returns empty so the
    sweep doesn't crash. Unknown path keys fall back to the moderate
    window (48h) so a future path that hasn't been registered here
    still gets analyzed periodically.
    """
    conn = _conn(app)
    if conn is None:
        return []
    with _db_lock(app):

        if now_ts is None:
            now_ts = int(time.time())

        # v4.14.5.2: flag-gated window selection. Stable (default) uses the
        # widened windows aligned with the maturity guard; legacy restores
        # the original tight cadence for rollback.
        try:
            _stable = bool(getattr(app, 'cfg', {}).get(
                'use_stable_recommend', True))
        except Exception:
            _stable = True
        _windows = (STALENESS_WINDOWS_SECONDS_STABLE if _stable
                    else STALENESS_WINDOWS_SECONDS)
        window = _windows.get(path, _windows['moderate'])
        stale_cutoff = now_ts - window
        cooldown_cutoff = now_ts - CASCADING_COOLDOWN_SECONDS

        # Tickers whose last_analyzed_at < stale_cutoff (stale) AND >=
        # cooldown_cutoff means "very recently analyzed" so we SKIP.
        # We want: last_analyzed_at < stale_cutoff AND last_analyzed_at
        # < cooldown_cutoff (which is automatic since stale_cutoff <
        # cooldown_cutoff in normal usage — staleness windows are much
        # longer than the 5-min cooldown).
        #
        # Edge case: never-analyzed tickers (no row in queue_runner
        # _analysis_log for this path) are NOT returned by this query
        # because we only know about tickers via that table. The cursor
        # path in v4.14.3.9 handles never-analyzed via the universe scope
        # query — that integration happens at the run_one_pass level, not
        # here. v4.14.4.0 staleness is "re-analyze EXISTING analyzed
        # tickers when stale"; bootstrap (never-analyzed) flows through
        # the existing cadence-mode universe-cursor path on the rare
        # paths where flag=True before the universe is populated. For
        # Mike's mature install this is fine.
        try:
            cur = conn.execute(
                "SELECT ticker, last_analyzed_at FROM "
                "queue_runner_analysis_log "
                "WHERE path = ? "
                "AND last_analyzed_at < ? "
                "AND last_analyzed_at < ? "
                "ORDER BY last_analyzed_at ASC",
                (path, stale_cutoff, cooldown_cutoff))
            rows = cur.fetchall()
        except Exception as e:
            _log_amber(
                app,
                f"check_staleness_triggers: db read failed for path="
                f"{path}: {type(e).__name__}: {e}")
            return []

        fires = []
        for ticker, last_ts in rows:
            try:
                last_ts = int(last_ts) if last_ts is not None else 0
            except (TypeError, ValueError):
                last_ts = 0
            age = max(0, now_ts - last_ts)
            ctx = {
                'kind': 'staleness',
                'last_analyzed_at': last_ts,
                'age_seconds': age,
                'window_seconds': window,
            }
            fires.append((ticker, ctx))
        return fires


# ─── Suspicion-staleness (v4.14.5.3, Step 4) ─────────────────────────

def _iso_to_epoch(s) -> Optional[float]:
    """Parse an ISO timestamp (with or without trailing 'Z') to epoch
    seconds. Returns None on any failure."""
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '')).timestamp()
    except Exception:
        return None


def _last_significant_price_move_epoch(cache_conn, ticker: str,
                                        pct_threshold: float
                                        ) -> Optional[float]:
    """Most recent daily_bars date where |close/prev_close - 1| >=
    pct_threshold, as epoch seconds. None if no such move / no data.
    Bounded scan (last ~60 bars) — open-BUY set is small so this is
    cheap. Read-only; never raises (returns None on any failure)."""
    if cache_conn is None:
        return None
    try:
        rows = cache_conn.execute(
            "SELECT date, close FROM daily_bars WHERE ticker = ? "
            "ORDER BY date DESC LIMIT 60", (ticker,)).fetchall()
    except Exception:
        return None
    prev = None
    for d, cl in rows:
        try:
            cl = float(cl)
        except (TypeError, ValueError):
            prev = None
            continue
        if prev is not None and cl > 0:
            try:
                if abs(prev / cl - 1.0) >= pct_threshold:
                    # `d` is the NEWER bar of the (d, prev_older) pair
                    # we just compared — its date carries the move.
                    return _iso_to_epoch(str(d) + 'T00:00:00')
            except Exception:
                pass
        prev = cl
    return None


def _build_consensus_ts_map() -> dict:
    """{TICKER: latest signals.jsonl ts epoch}. One pass over the
    small signals.jsonl. Best-effort: {} on any failure."""
    out: dict = {}
    try:
        import os
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'data', 'signals.jsonl')
        if not os.path.exists(path):
            return {}
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                t = (o.get('ticker') or '').upper()
                ts = o.get('ts') or o.get('timestamp')
                e = _iso_to_epoch(ts)
                if t and e is not None:
                    if e > out.get(t, 0):
                        out[t] = e
    except Exception:
        return {}
    return out


def _last_earnings_epoch(ticker: str, now_ts: float) -> Optional[float]:
    """Most recent PAST earnings date for the ticker, epoch. Defensive:
    the earnings calendar is frequently unavailable (Finnhub
    key/network — a separately-tracked gap); this contributes nothing
    when empty rather than failing the sweep."""
    try:
        import tm_discover as _tmd
        get_ev = getattr(_tmd, 'get_earnings_for_ticker', None)
        if get_ev is None:
            return None
        events = get_ev(ticker) or []
    except Exception:
        return None
    best = None
    for ev in events:
        e = _iso_to_epoch((ev.get('date') or '')[:10] + 'T00:00:00')
        if e is None or e > now_ts:
            continue
        if best is None or e > best:
            best = e
    return best


def get_last_change_event_at(ticker: str, path: str, made_at_epoch: float,
                              cache_conn=None,
                              consensus_map: Optional[dict] = None,
                              now_ts: Optional[float] = None) -> float:
    """v4.14.5.3: epoch of the most recent REAL-WORLD change event for
    (ticker, path): significant price move, news arrival, consensus
    result, or earnings event. The BUY's own made_at is the baseline
    floor (silence is measured from when the BUY was made). NOTE: a
    mere re-analysis is deliberately NOT a change event — counting it
    would let the system reset its own suspicion clock without
    anything actually changing."""
    if now_ts is None:
        now_ts = time.time()
    try:
        import tm_holdings as _tmh
        track = _tmh.get_path_track(path)
    except Exception:
        track = 'speculative'
    thr = (_SUSPICION_PRICE_MOVE_MAIN if track == 'main'
           else _SUSPICION_PRICE_MOVE_SPECULATIVE)

    candidates = [made_at_epoch]

    pm = _last_significant_price_move_epoch(cache_conn, ticker, thr)
    if pm is not None:
        candidates.append(pm)

    if cache_conn is not None:
        try:
            row = cache_conn.execute(
                "SELECT MAX(timestamp) FROM news_signals "
                "WHERE ticker = ?", (ticker,)).fetchone()
            ne = _iso_to_epoch(row[0]) if row and row[0] else None
            if ne is not None:
                candidates.append(ne)
        except Exception:
            pass

    if consensus_map:
        ce = consensus_map.get((ticker or '').upper())
        if ce:
            candidates.append(ce)

    ee = _last_earnings_epoch(ticker, now_ts)
    if ee is not None:
        candidates.append(ee)

    return max(candidates)


def _check_suspicion_staleness_triggers(app, path: str,
                                         now_ts: Optional[int] = None
                                         ) -> list:
    """v4.14.5.3 (Step 4): fire a re-check for OPEN BUYs on this path
    that have had ZERO change events for >= the path's suspicion
    window AND are past the maturity guard. This is a CHECK, not a
    kill — the runner re-analyzes; the result then keeps or exits the
    BUY through the normal flow. Return shape matches the legacy
    function so the runner needs no rewiring.
    """
    if now_ts is None:
        now_ts = int(time.time())

    conn = _conn(app)  # tired_market.db — for dedup / cascading cooldown
    with _db_lock(app):

        state = getattr(app, '_holdings_state', None) or {}
        plog = state.get('predictions_log')
        if plog is None:
            return []
        # v4.14.6.35-fix-startup-stampede: working-set sufficient.
        # Suspicion-staleness checks only OPEN predictions (the rule
        # is "this BUY has gone quiet"); the working set always loads
        # every record with status=OUTCOME_OPEN regardless of age.
        # Calling get_all_full on the event-trigger first tick was
        # one of the stampede contributors that blocked the post-paint
        # window in v4.14.6.34.
        try:
            records = plog.get_all() if hasattr(plog, 'get_all') else list(
                getattr(plog, '_cache', []) or [])
        except Exception as e:
            _log_amber(
                app,
                f"suspicion-staleness: predictions read failed: "
                f"{type(e).__name__}: {e}")
            return []

        try:
            from tm_discover import (MIN_MATURITY_DAYS_FLOOR as _MMF,
                                      MIN_MATURITY_TIMEFRAME_RATIO as _MMR)
        except Exception:
            _MMF, _MMR = 3, 0.25

        window_days = SUSPICION_STALENESS_WINDOWS_DAYS.get(path, 14)
        consensus_map = _build_consensus_ts_map()

        cache_conn = None
        try:
            try:
                import tm_cache as _tmc
                cache_conn = _tmc.get_connection()
            except Exception:
                cache_conn = None

            fires = []
            for r in records:
                try:
                    if (r.get('status') != 'open'
                            or (r.get('direction') or '').upper() != 'BUY'):
                        continue
                    if (r.get('path') or '').strip() != path:
                        continue
                    t = (r.get('ticker') or '').upper()
                    if not t:
                        continue
                    made_at = _iso_to_epoch(r.get('timestamp'))
                    if made_at is None:
                        continue

                    # Maturity guard (same rule as v4.14.5.1 supersession).
                    try:
                        tf_days = int(r.get('timeframe_days') or 30)
                    except (TypeError, ValueError):
                        tf_days = 30
                    age_days = (now_ts - made_at) / 86400.0
                    maturity_days = max(_MMF, tf_days * _MMR)
                    if age_days < maturity_days:
                        continue

                    # Cascading cooldown + per-kind dedup (24h) so a quiet
                    # BUY fires a re-check at most once/day, not every sweep.
                    if conn is not None and _within_cascading_cooldown(
                            conn, t, path, now_ts):
                        continue
                    if conn is not None and _within_kind_dedup(
                            conn, t, path, 'staleness', now_ts):
                        continue

                    last_change = get_last_change_event_at(
                        t, path, made_at, cache_conn=cache_conn,
                        consensus_map=consensus_map, now_ts=now_ts)
                    silent_days = (now_ts - last_change) / 86400.0
                    if silent_days >= window_days:
                        fires.append((t, {
                            'kind': 'staleness',
                            'subkind': 'suspicion',
                            'last_change_event_at': int(last_change),
                            'silent_days': int(silent_days),
                            'window_days': window_days,
                        }))
                except Exception:
                    # One bad record must not abort the sweep.
                    continue
            return fires
        finally:
            if cache_conn is not None:
                try:
                    cache_conn.close()
                except Exception:
                    pass


def check_staleness_triggers(app, path: str,
                              now_ts: Optional[int] = None) -> list:
    """Dispatcher (v4.14.5.3). Default = suspicion-staleness (Step 4).
    cfg['use_suspicion_staleness']=False restores the legacy
    clock-driven trigger for rollback. Return shape is identical
    either way, so evaluate_all_triggers / the runner are unchanged."""
    try:
        use_susp = bool(getattr(app, 'cfg', {}).get(
            'use_suspicion_staleness', True))
    except Exception:
        use_susp = True
    if use_susp:
        return _check_suspicion_staleness_triggers(app, path, now_ts)
    return _check_legacy_staleness_triggers(app, path, now_ts)


# ─── Per-kind dedup helper ───────────────────────────────────────────

def _within_kind_dedup(conn, ticker: str, path: str,
                        kind: str, now_ts: int) -> bool:
    """Returns True if a fire of this kind for (ticker, path) is
    within its per-kind dedup window — meaning we should SUPPRESS
    a new fire. v4.14.4.1.

    Kinds not in PER_KIND_DEDUP_WINDOWS_SECONDS have no per-kind
    dedup; only the cascading cooldown applies (handled separately
    by callers).

    Defensive: DB read failure returns False (don't suppress; let
    the fire happen). Amber log captured upstream by the caller's
    sweep wrapper. Cascading cooldown is the safety net.
    """
    window = PER_KIND_DEDUP_WINDOWS_SECONDS.get(kind)
    if window is None:
        return False
    if conn is None:
        return False
    cutoff = now_ts - window
    try:
        cur = conn.execute(
            "SELECT fired_at FROM trigger_fire_log "
            "WHERE ticker = ? AND path = ? AND trigger_kind = ? "
            "AND fired_at >= ? "
            "ORDER BY fired_at DESC LIMIT 1",
            (ticker, path, kind, cutoff))
        row = cur.fetchone()
        return row is not None
    except Exception:
        # No amber here — would spam during chronic db issues. The
        # cascading floor still protects against runaway re-fires.
        return False


def _within_cascading_cooldown(conn, ticker: str, path: str,
                                 now_ts: int) -> bool:
    """v4.14.4.1: returns True if (ticker, path) was analyzed within
    CASCADING_COOLDOWN_SECONDS. Reads queue_runner_analysis_log.

    Used by check_price_triggers to suppress fires that would
    immediately cascade into re-analysis after a just-completed
    analysis. The cascading floor applies to ALL trigger kinds
    (target_stop, price_drift, etc.) regardless of per-kind
    windows.
    """
    if conn is None:
        return False
    cutoff = now_ts - CASCADING_COOLDOWN_SECONDS
    try:
        cur = conn.execute(
            "SELECT last_analyzed_at FROM queue_runner_analysis_log "
            "WHERE ticker = ? AND path = ? "
            "AND last_analyzed_at >= ? LIMIT 1",
            (ticker, path, cutoff))
        return cur.fetchone() is not None
    except Exception:
        return False


# ─── Price-drift + target/stop triggers ──────────────────────────────

def check_price_triggers(app, path: str,
                          now_ts: Optional[int] = None) -> list:
    """v4.14.4.1: returns list of fires for the given path covering
    BOTH `price_drift` (drift exceeds path threshold) and
    `target_stop` (current_price crossed target or stop on a
    prediction with target/stop set).

    Both kinds can fire for the same ticker — they're independent
    signals with independent per-kind dedup. Caller dispatches one
    analysis per (ticker, path) collating both contexts (the F4
    pattern from v4.14.4.0).

    Returns: list of (ticker, signal_context_dict) tuples where
    signal_context['kind'] is either 'price_drift' or 'target_stop'.
    The outer evaluate_all_triggers wrapper uses the inner kind
    to tag the fire dict.

    Iterates queue_runner_analysis_log for the path (same source
    as check_staleness_triggers). For each analyzed ticker:
      - Fetch most-recent prediction via PredictionsLog
      - Read baseline (current_price_at_prediction); skip if None
      - Read current price via cache.quote(); skip if None
      - Check cascading 5-min cooldown; skip if within
      - Check target_stop crossing (if target+stop present, not
        within per-kind dedup); record fire if crossed
      - Check price_drift threshold (not within per-kind dedup);
        record fire if exceeded

    Defensive: each per-ticker step is wrapped so one bad ticker
    doesn't abort the sweep. Cache failures log amber ONCE per
    sweep (deduped via a local seen-set), not per failing ticker.
    """
    conn = _conn(app)
    if conn is None:
        return []
    with _db_lock(app):
        if now_ts is None:
            now_ts = int(time.time())

        # Resolve PredictionsLog + cache from app._holdings_state. Same
        # pattern tm_queue_runner uses.
        state = getattr(app, '_holdings_state', None) or {}
        plog = state.get('predictions_log')
        cache = state.get('cache')
        if plog is None or cache is None:
            return []

        threshold = PRICE_DRIFT_THRESHOLDS_PCT.get(path)
        if threshold is None:
            # Unknown path — no drift check (target_stop still possible
            # if target/stop set on the prediction, but the path
            # threshold-driven drift kind is undefined).
            threshold = 0.0

        # Pull all analyzed tickers for this path. Cheap SQL.
        try:
            cur = conn.execute(
                "SELECT ticker FROM queue_runner_analysis_log "
                "WHERE path = ?",
                (path,))
            analyzed_tickers = [r[0] for r in cur.fetchall()]
        except Exception as e:
            _log_amber(
                app,
                f"check_price_triggers: analysis_log read failed for "
                f"path={path}: {type(e).__name__}: {e}")
            return []

        if not analyzed_tickers:
            return []

        fires = []
        quote_fail_logged = False

        for ticker in analyzed_tickers:
            # Cascading cooldown.
            if _within_cascading_cooldown(conn, ticker, path, now_ts):
                continue

            # Baseline from most-recent prediction.
            pred = plog.get_most_recent_for_ticker_and_path(ticker, path)
            if pred is None:
                continue
            baseline = pred.get('current_price_at_prediction')
            try:
                baseline = float(baseline) if baseline is not None else None
            except (TypeError, ValueError):
                baseline = None
            if baseline is None or baseline <= 0:
                continue

            # Current price.
            try:
                q = cache.quote(ticker)
            except Exception as e:
                if not quote_fail_logged:
                    _log_amber(
                        app,
                        f"check_price_triggers ({path}): cache.quote() "
                        f"raised for at least one ticker "
                        f"({type(e).__name__}); skipping affected "
                        f"tickers this sweep.")
                    quote_fail_logged = True
                continue
            if q is None:
                continue
            current = (q or {}).get('price')
            try:
                current = float(current) if current is not None else None
            except (TypeError, ValueError):
                current = None
            if current is None or current <= 0:
                continue

            # target_stop check — only if both fields are set.
            target = pred.get('target')
            stop = pred.get('stop')
            try:
                target = float(target) if target is not None else None
            except (TypeError, ValueError):
                target = None
            try:
                stop = float(stop) if stop is not None else None
            except (TypeError, ValueError):
                stop = None

            if (target is not None and stop is not None
                    and not _within_kind_dedup(
                        conn, ticker, path, 'target_stop', now_ts)):
                # "Crossed" detection handles both long (target above
                # baseline, stop below) and short/inverse (target below
                # baseline, stop above) prediction shapes. The check is
                # symmetric: did current land on the wrong side of the
                # baseline-anchored target or stop?
                crossed_target = (
                    (target >= baseline and current >= target)
                    or (target <= baseline and current <= target))
                crossed_stop = (
                    (stop <= baseline and current <= stop)
                    or (stop >= baseline and current >= stop))
                if crossed_target or crossed_stop:
                    fires.append((ticker, {
                        'kind': 'target_stop',
                        'baseline': baseline,
                        'current': current,
                        'target': target,
                        'stop': stop,
                        'crossed': ('target' if crossed_target
                                     else 'stop'),
                        'baseline_prediction_ts': pred.get('timestamp'),
                    }))

            # price_drift check — independent of target_stop.
            if not _within_kind_dedup(
                    conn, ticker, path, 'price_drift', now_ts):
                drift = (current / baseline) - 1.0  # signed
                if abs(drift) >= threshold:
                    fires.append((ticker, {
                        'kind': 'price_drift',
                        'baseline': baseline,
                        'current': current,
                        'drift_pct': drift * 100.0,
                        'threshold_pct': threshold * 100.0,
                        'baseline_prediction_ts': pred.get('timestamp'),
                    }))

        return fires


# ─── News trigger ────────────────────────────────────────────────────

def check_news_triggers(app, path: str,
                         now_ts: Optional[int] = None) -> list:
    """v4.14.4.2: returns list of (ticker, signal_context) fires
    for tickers with enough new news since the analysis baseline.

    Fire condition (per NEWS_TRIGGER_THRESHOLDS[path]):
      COUNT(news_cache rows where timestamp > anchor) >= threshold
    where anchor = max(last_analyzed_at, now - max_age_hours).

    The max_age_hours floor prevents never-analyzed tickers from
    firing on weeks-old news. It's the load-bearing piece — without
    it, the trigger would fire incorrectly on stale data for any
    ticker that's never had a baseline.

    Uses direct SQL against news_cache (not cache.news_features()).
    Two reasons: (a) cache.news_features triggers SWR-style refresh
    which we don't want during passive sweep, (b) the trigger only
    needs the count and the top headline, not the full features
    dict.

    Iterates queue_runner_analysis_log for the path. Tickers with
    no news_cache rows produce no fires (silent skip — that's the
    common case, not an error). cache failures log amber ONCE per
    sweep via a local flag, not per failing ticker.

    Returns list of (ticker, signal_context) tuples where
    context['kind'] = 'news'. Sparsity reality on day-one ship:
    only ~8 of Mike's ~2,500 tickers have news_cache data, so
    fires will be sparse until background news refresh lands in
    a future patch.
    """
    conn = _conn(app)
    if conn is None:
        return []
    with _db_lock(app):
        if now_ts is None:
            now_ts = int(time.time())

        thresholds = NEWS_TRIGGER_THRESHOLDS.get(path)
        if thresholds is None:
            # Unknown path — skip news trigger for it. Other triggers
            # may still fire.
            return []
        threshold_count, max_age_hours = thresholds

        # Anchor cutoff for "fresh news" (ISO string for lexicographic
        # comparison with news_cache.timestamp which is also a naive
        # ISO string per Database.save_news_batch's datetime.now()
        # .isoformat()).
        max_age_cutoff_ts = now_ts - (max_age_hours * 3600)
        max_age_cutoff_iso = datetime.fromtimestamp(
            max_age_cutoff_ts).isoformat()

        # Pull analyzed-tickers list with last_analyzed_at.
        try:
            cur = conn.execute(
                "SELECT ticker, last_analyzed_at "
                "FROM queue_runner_analysis_log "
                "WHERE path = ?",
                (path,))
            analyzed = list(cur.fetchall())
        except Exception as e:
            _log_amber(
                app,
                f"check_news_triggers ({path}): analysis_log read "
                f"failed: {type(e).__name__}: {e}")
            return []

        if not analyzed:
            return []

        fires = []
        news_sql_fail_logged = False

        for ticker, last_ts in analyzed:
            # Cascading cooldown (5-min floor on any kind).
            if _within_cascading_cooldown(conn, ticker, path, now_ts):
                continue
            # Per-kind dedup (60-min for news).
            if _within_kind_dedup(conn, ticker, path, 'news', now_ts):
                continue

            # Compute anchor — max of (last_analyzed_at, max_age_cutoff).
            try:
                last_ts_int = (int(last_ts)
                                if last_ts is not None else 0)
            except (TypeError, ValueError):
                last_ts_int = 0
            last_ana_iso = (
                datetime.fromtimestamp(last_ts_int).isoformat()
                if last_ts_int > 0 else '')
            # Lexicographic max — ISO strings sort correctly.
            anchor_iso = max(last_ana_iso, max_age_cutoff_iso)

            # Count new articles + grab top headline in one query.
            # news_cache schema (per tired_market.py:411):
            #   timestamp, ticker, headline, source, sentiment_score,
            #   raw_data
            # Index idx_nc on (ticker, timestamp) makes this fast.
            try:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM news_cache "
                    "WHERE ticker = ? AND timestamp > ?",
                    (ticker, anchor_iso))
                row = cur.fetchone()
                count = int(row[0]) if row and row[0] is not None else 0
            except Exception as e:
                if not news_sql_fail_logged:
                    _log_amber(
                        app,
                        f"check_news_triggers ({path}): news_cache "
                        f"query failed for at least one ticker "
                        f"({type(e).__name__}); skipping affected "
                        f"tickers this sweep.")
                    news_sql_fail_logged = True
                continue

            if count < threshold_count:
                continue

            # Top headline for prompt framing (best-effort; failure
            # leaves it as None).
            top_headline = None
            try:
                cur = conn.execute(
                    "SELECT headline FROM news_cache "
                    "WHERE ticker = ? AND timestamp > ? "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (ticker, anchor_iso))
                row = cur.fetchone()
                if row and row[0]:
                    top_headline = str(row[0])
            except Exception:
                pass  # headline is non-essential context

            fires.append((ticker, {
                'kind': 'news',
                'new_article_count': count,
                'since_ts': anchor_iso,
                'top_headline': top_headline,
                'threshold': threshold_count,
                'max_age_hours': max_age_hours,
            }))

        return fires


# ─── Earnings trigger ────────────────────────────────────────────────

def _parse_event_date(date_str: str) -> Optional[date]:
    """Parse Finnhub's 'YYYY-MM-DD' to a date. Returns None on bad
    input — caller silently skips that event."""
    if not date_str or not isinstance(date_str, str):
        return None
    try:
        return datetime.fromisoformat(date_str[:10]).date()
    except (ValueError, TypeError):
        return None


def check_earnings_triggers(app, path: str,
                              now_ts: Optional[int] = None) -> list:
    """v4.14.4.3: returns list of (ticker, signal_context) fires for
    tickers with an earnings event inside the path's upcoming or
    recent window.

    ONE 'earnings' kind, with signal_context['subkind'] distinguishing
    'upcoming' (event ahead) from 'recent' (event just passed).
    Combined kind keeps the priority slot and dedup tuple simple.

    Iterates queue_runner_analysis_log for the path (same source as
    other triggers). Per ticker:
      - Skip if within cascading 5-min cooldown
      - Skip if within per-kind 6-hour dedup
      - Look up earnings via tm_discover.get_earnings_for_ticker
        (module-cached, lock-free fast path)
      - Find the SOONEST upcoming OR most-recent past event inside
        the path's windows
      - Build signal_context with subkind + days_delta + estimates/
        actuals if known

    Defensive: missing earnings data, bad date strings, and import
    failures all silent-skip. tm_discover import wrapped in try/
    except so an environment without that module (vanishingly
    unlikely but defensive) doesn't crash the sweep.

    Tickers with no earnings data are the common case (Finnhub free
    tier coverage is sparse beyond S&P 500). Silent skip.
    """
    conn = _conn(app)
    if conn is None:
        return []
    with _db_lock(app):
        if now_ts is None:
            now_ts = int(time.time())

        windows = EARNINGS_TRIGGER_WINDOWS.get(path)
        if windows is None:
            # Unknown path — skip earnings for it. Other triggers may fire.
            return []
        upcoming_days, recent_days = windows

        today = datetime.fromtimestamp(now_ts).date()
        upcoming_cutoff = today + timedelta(days=upcoming_days)
        recent_cutoff = today - timedelta(days=recent_days)

        # Pull analyzed-tickers list.
        try:
            cur = conn.execute(
                "SELECT ticker FROM queue_runner_analysis_log "
                "WHERE path = ?",
                (path,))
            analyzed_tickers = [r[0] for r in cur.fetchall()]
        except Exception as e:
            _log_amber(
                app,
                f"check_earnings_triggers ({path}): analysis_log read "
                f"failed: {type(e).__name__}: {e}")
            return []

        if not analyzed_tickers:
            return []

        # Lazy import to keep tm_event_triggers free of circular import
        # risk with tm_discover (which imports a lot).
        try:
            import tm_discover as _tm_discover
            get_events = getattr(
                _tm_discover, 'get_earnings_for_ticker', None)
        except Exception as e:
            _log_amber(
                app,
                f"check_earnings_triggers: tm_discover import failed: "
                f"{type(e).__name__}: {e}")
            return []
        if get_events is None:
            # Defensive: accessor missing means we silently skip rather
            # than crash. Indicates a stale tm_discover.py — the audit
            # catches that case independently.
            return []

        fires = []
        for ticker in analyzed_tickers:
            if _within_cascading_cooldown(conn, ticker, path, now_ts):
                continue
            if _within_kind_dedup(conn, ticker, path, 'earnings', now_ts):
                continue

            try:
                events = get_events(ticker) or []
            except Exception:
                # Defensive — bad ticker shape, accessor exception. Skip.
                continue
            if not events:
                continue

            # Walk events: pick the SOONEST upcoming event in window,
            # else the MOST RECENT past event in window. Events are
            # already sorted ascending by date per _load_earnings_calendar
            # / set_earnings_for_ticker (Stage 0 / Stage 1).
            best_subkind = None
            best_event = None
            best_days_delta = None

            for ev in events:
                ev_date = _parse_event_date(ev.get('date'))
                if ev_date is None:
                    continue

                if today <= ev_date <= upcoming_cutoff:
                    days_until = (ev_date - today).days
                    # Soonest upcoming wins. We only need the first
                    # qualifying event since the list is sorted asc.
                    if best_subkind is None:
                        best_subkind = 'upcoming'
                        best_event = ev
                        best_days_delta = days_until
                        break  # any later event is further away

                elif recent_cutoff <= ev_date < today:
                    # Most-recent past — keep iterating in case there's
                    # something more recent OR an upcoming event we
                    # haven't seen yet (upcoming wins if found).
                    days_since = (today - ev_date).days
                    # Replace only if more recent (smaller days_since).
                    if (best_subkind is None
                            or (best_subkind == 'recent'
                                and days_since < (best_days_delta or 999))):
                        best_subkind = 'recent'
                        best_event = ev
                        best_days_delta = days_since

            if best_subkind is None:
                continue

            # Build context. Numeric fields normalized; failures fall to
            # None rather than crashing.
            def _num(v):
                try:
                    return float(v) if v is not None else None
                except (TypeError, ValueError):
                    return None

            ctx = {
                'kind': 'earnings',
                'subkind': best_subkind,
                'earnings_date': best_event.get('date'),
                'days_delta': best_days_delta,
                'hour': best_event.get('hour') or '',
                'eps_estimate': _num(best_event.get('eps_estimate')),
                'eps_actual': _num(best_event.get('eps_actual')),
                'revenue_estimate': _num(
                    best_event.get('revenue_estimate')),
                'revenue_actual': _num(best_event.get('revenue_actual')),
                'quarter': best_event.get('quarter'),
                'year': best_event.get('year'),
            }
            fires.append((ticker, ctx))

        return fires


# ─── User-signal trigger (push-based) ────────────────────────────────

def record_user_signal(ticker: str, action: str,
                        app=None,
                        path: Optional[str] = None,
                        context_str: str = "") -> bool:
    """v4.14.4.3: push-based trigger for user-initiated actions.

    Called from UI code at the moment of action (watchlist add,
    position open, etc.). Appends to in-memory pending list and
    wakes the runner via _trigger_wake_event so the next sweep
    fires within ~1s instead of waiting for the backoff window.

    Args:
      ticker: stock symbol (case-normalized internally)
      action: short tag — 'watchlist_add' or 'position_open' for
        v4.14.4.3; future tags may be added
      app: App reference. Defaults to module-level _app_ref set
        by set_app() at App init. Pass explicit app to override.
      path: target path. Defaults to app.cfg['analysis_path'] (the
        user's currently-viewed path) so analysis happens where
        they're looking.
      context_str: optional short note for prompt framing.

    Returns:
      True if signal was queued; False if suppressed by push-time
      dedup OR if app/path resolution failed.

    Push-time dedup: same (ticker, path, action) tuple within 60s
    is silently dropped. Prevents UI-fumble storms (add → remove →
    add within one second).

    Thread-safe: lock guards pending list. Safe to call from any
    UI thread; the runner drains from its own thread.

    Defensive: missing app, missing cfg, bad ticker all return
    False rather than crash. Wake-event signal is best-effort —
    failure to wake just means the next scheduled sweep handles
    it instead of immediate.
    """
    if not ticker or not isinstance(ticker, str):
        return False
    ticker = ticker.strip().upper()
    if not ticker:
        return False
    if not action:
        return False

    # Resolve app + path.
    if app is None:
        app = _app_ref
    if app is None:
        return False

    if path is None:
        try:
            cfg = getattr(app, 'cfg', None) or {}
            path = cfg.get('analysis_path')
        except Exception:
            path = None
        if not path:
            # Fall back to canonical default. tm_holdings.DEFAULT_PATH
            # is the project-wide source of truth; lazy import.
            try:
                import tm_holdings as _tm_holdings
                path = getattr(_tm_holdings, 'DEFAULT_PATH', 'moderate')
            except Exception:
                path = 'moderate'

    if not isinstance(path, str) or not path:
        return False

    now = time.time()
    dedup_window = PER_KIND_DEDUP_WINDOWS_SECONDS.get('user', 60)

    with _pending_user_signals_lock:
        # Push-time dedup against pending list itself. Same
        # (ticker, path, action) within 60s → suppress. This is
        # separate from the trigger_fire_log per-kind dedup which
        # kicks in across sweeps after a fire is recorded.
        for sig in _pending_user_signals:
            if (sig.get('ticker') == ticker
                    and sig.get('path') == path
                    and sig.get('action') == action
                    and (now - sig.get('ts', 0)) < dedup_window):
                return False
        _pending_user_signals.append({
            'ticker': ticker,
            'path': path,
            'action': action,
            'context_str': context_str or '',
            'ts': now,
        })

    # Wake the runner — best effort.
    try:
        wake = getattr(app, '_trigger_wake_event', None)
        if wake is not None:
            wake.set()
    except Exception:
        pass
    return True


def drain_user_signals(app, now_ts: Optional[int] = None) -> list:
    """v4.14.4.3: pop all pending user signals; return as fires.

    Called once per sweep by evaluate_all_triggers — NOT per-path,
    because each pending signal already carries its target path.

    Returns list of fire dicts shaped like other trigger fires:
        {'ticker': str, 'path': str, 'kind': 'user', 'context': dict}

    Applies the same cascading + per-kind dedup as other triggers.
    The cascading 5-min floor catches "user signal arrives right
    after the same ticker was analyzed for any reason" — analysis
    just ran, re-firing would be cascade noise. The per-kind 60s
    floor against trigger_fire_log catches re-pushes that the
    push-time dedup missed (e.g., if push-time dedup expired but
    the actual analysis from the prior push hasn't completed yet).

    Suppressed signals are DROPPED (not re-queued). Per design:
    the cascade cooldown means the ticker just got analyzed; the
    user's intent has already effectively been served.

    Thread-safe via the same lock as record_user_signal.
    """
    if now_ts is None:
        now_ts = int(time.time())

    with _pending_user_signals_lock:
        pending = list(_pending_user_signals)
        _pending_user_signals.clear()

    if not pending:
        return []

    conn = _conn(app)
    with _db_lock(app):
        fires = []
        for sig in pending:
            ticker = sig.get('ticker')
            path = sig.get('path')
            action = sig.get('action', '?')
            if not ticker or not path:
                continue
            # Cascading floor — same as other triggers.
            if conn is not None and _within_cascading_cooldown(
                    conn, ticker, path, now_ts):
                continue
            # Per-kind dedup against the durable log.
            if conn is not None and _within_kind_dedup(
                    conn, ticker, path, 'user', now_ts):
                continue

            pushed_at_iso = datetime.fromtimestamp(
                sig.get('ts', now_ts)).isoformat()
            fires.append({
                'ticker': ticker,
                'path': path,
                'kind': 'user',
                'context': {
                    'kind': 'user',
                    'action': action,
                    'context_str': sig.get('context_str') or '',
                    'pushed_at': pushed_at_iso,
                },
            })
        return fires


# ─── Trigger fire recording ──────────────────────────────────────────

def record_trigger_fire(app, ticker: str, path: str,
                         trigger_kind: str,
                         signal_context: Optional[dict] = None,
                         now_ts: Optional[int] = None) -> None:
    """Append a row to trigger_fire_log. Best-effort: failure logs
    amber and the sweep continues. A failed write doesn't block
    analysis (the analysis itself happens via the dispatch path
    which has its own write of queue_runner_analysis_log)."""
    conn = _conn(app)
    if conn is None:
        return
    with _db_lock(app):
        if now_ts is None:
            now_ts = int(time.time())
        try:
            ctx_json = json.dumps(signal_context or {})
        except Exception:
            ctx_json = '{}'
        try:
            conn.execute(
                "INSERT OR REPLACE INTO trigger_fire_log "
                "(ticker, path, trigger_kind, fired_at, signal_context) "
                "VALUES (?, ?, ?, ?, ?)",
                (ticker, path, trigger_kind, now_ts, ctx_json))
            conn.commit()
        except Exception as e:
            _log_amber(
                app,
                f"record_trigger_fire failed for "
                f"{ticker}/{path}/{trigger_kind}: "
                f"{type(e).__name__}: {e}")


# ─── v4.14.5.82-discovery-unlock: fresh_universe_mover trigger ───────
#
# All other event triggers gate on `queue_runner_analysis_log`
# (analyzed-only), so a ticker that has never been analyzed is
# invisible to the sweep regardless of how much it moves. This trigger
# is the ONE place in the sweep that's allowed to look at never-
# analyzed tickers. It reads recent bars from `tm_cache` (cache.db,
# the daily_bars table), computes a 5-day price drift, filters to
# tickers absent from `queue_runner_analysis_log` (app.db), and
# returns the strongest movers up to FRESH_MOVER_PER_SWEEP_CAP.
#
# Path assignment is by price (lottery for sub-$5, aggressive
# otherwise) — both paths have DYNAMIC pools so the Part-B pool-gate
# carve-out can route discovery fires there without touching the
# slow_safe / moderate seed-list paths.

def check_fresh_universe_mover_triggers(app,
                                          now_ts: 'int | None' = None
                                          ) -> list:
    """v4.14.5.82-discovery-unlock: return a list of fire dicts for
    never-analyzed tickers that are currently moving. This is the ONLY
    trigger that escapes the analyzed-only gate the other event triggers
    enforce.

    Shape (matches other trigger fire dicts):
        {'ticker': str, 'path': str, 'kind': 'fresh_universe_mover',
         'context': {'drift_pct': float, 'lookback_days': int,
                     'last_close': float, 'reference_close': float}}

    Returns at most FRESH_MOVER_PER_SWEEP_CAP fires. Sorted by
    |drift_pct| descending (strongest movers first within the kind).
    Defensive: any failure returns [], never raises, never crashes
    the sweep. When `_FRESH_MOVER_ENABLED` is False (flag-off), returns
    [] immediately — exact pre-v.82 behavior.
    """
    if not _FRESH_MOVER_ENABLED:
        return []
    if now_ts is None:
        now_ts = int(time.time())

    # 1. Read the analyzed-ticker set from the APP db. Any path counts —
    # discovery is per-ticker (universe-wide), not per-path. A ticker
    # analyzed under any path is no longer a fresh discovery candidate.
    conn = _conn(app)
    if conn is None:
        return []
    analyzed: set = set()
    try:
        with _db_lock(app):
            cur = conn.execute(
                "SELECT DISTINCT ticker FROM queue_runner_analysis_log")
            analyzed = {(r[0] or '').upper() for r in cur.fetchall()}
            analyzed.discard('')
    except Exception as e:
        _log_amber(
            app,
            f"check_fresh_universe_mover_triggers: analysis_log read "
            f"failed: {type(e).__name__}: {e}")
        return []

    # 2. Read recent bars from cache.db (the daily_bars store). We want
    # the most recent close per ticker AND a close from
    # ~FRESH_MOVER_LOOKBACK_DAYS trading days ago. The cheapest path is
    # one SQL query that pulls the last N+1 bars per ticker; Python-side
    # we'll pick the latest + the Nth back per ticker.
    try:
        import tm_cache as _tc
        cache_conn = _tc.get_connection()
    except Exception as e:
        _log_amber(
            app,
            f"check_fresh_universe_mover_triggers: cache connect "
            f"failed: {type(e).__name__}: {e}")
        return []

    # Window: only look at bars in the last ~3 calendar weeks (more than
    # enough for a 5-trading-day lookback even across holidays).
    from datetime import datetime as _dt, timedelta as _td
    window_cutoff = (_dt.now() - _td(days=21)).strftime('%Y-%m-%d')

    try:
        cur = cache_conn.execute(
            "SELECT ticker, date, close FROM daily_bars "
            "WHERE date >= ? AND close IS NOT NULL "
            "ORDER BY ticker, date ASC",
            (window_cutoff,))
        rows = cur.fetchall()
    except Exception as e:
        _log_amber(
            app,
            f"check_fresh_universe_mover_triggers: daily_bars query "
            f"failed: {type(e).__name__}: {e}")
        try:
            cache_conn.close()
        except Exception:
            pass
        return []
    finally:
        try:
            cache_conn.close()
        except Exception:
            pass

    # 3. Group bars per ticker and compute drift over the lookback.
    # Group on the fly via a dict; rows arrive sorted by (ticker, date).
    per_ticker: dict = {}
    for tk, d, close in rows:
        tk_u = (tk or '').upper()
        if not tk_u:
            continue
        try:
            close_f = float(close)
        except (TypeError, ValueError):
            continue
        if close_f <= 0:
            continue
        per_ticker.setdefault(tk_u, []).append((str(d)[:10], close_f))

    fires: list = []
    for tk_u, bars in per_ticker.items():
        # Skip if already analyzed (the load-bearing filter).
        if tk_u in analyzed:
            continue
        # Need at least 2 bars (latest + N-back). We pull
        # FRESH_MOVER_LOOKBACK_DAYS bars back; if fewer exist (recent
        # listing), use the oldest available.
        if len(bars) < 2:
            continue
        # bars is sorted ascending by date — last is latest.
        latest_date, latest_close = bars[-1]
        # Reference: the bar from N trading-days back. With ascending
        # order, that's index max(0, len - 1 - LOOKBACK).
        ref_idx = max(0, len(bars) - 1 - int(FRESH_MOVER_LOOKBACK_DAYS))
        ref_date, ref_close = bars[ref_idx]
        if ref_close <= 0:
            continue
        drift = (latest_close - ref_close) / ref_close
        if abs(drift) < FRESH_MOVER_MIN_DRIFT_PCT:
            continue
        # Path assignment by current price.
        if latest_close < FRESH_MOVER_LOTTERY_MAX_PRICE:
            path = 'lottery'
        else:
            path = 'aggressive'
        fires.append({
            'ticker': tk_u,
            'path': path,
            'kind': 'fresh_universe_mover',
            'context': {
                'kind': 'fresh_universe_mover',
                'drift_pct': float(drift),
                'lookback_days': int(FRESH_MOVER_LOOKBACK_DAYS),
                'last_close': float(latest_close),
                'reference_close': float(ref_close),
                'latest_date': latest_date,
                'reference_date': ref_date,
            },
        })

    # 4. Sort by |drift| descending, cap to per-sweep ceiling.
    fires.sort(key=lambda f: -abs(
        float(f['context'].get('drift_pct') or 0.0)))
    return fires[:FRESH_MOVER_PER_SWEEP_CAP]


# ─── v4.14.5.83-leading-signals: volume-accumulation trigger ─────────
#
# Fires on QUIET volume — unusually high volume relative to the
# trailing average WITHOUT a corresponding price move yet. The "without
# price" part is the load-bearing distinction from v.82's
# fresh_universe_mover: if price already moved big, the mover trigger
# catches it; this trigger is specifically the accumulation signature
# (someone's buying before the move).
#
# This is a LEADING signal — most won't pan out, which is exactly why
# it routes to lottery/aggressive (speculative paths). Honestly framed:
# "a reason to look," not "a prediction."

def check_volume_accumulation_triggers(app,
                                        now_ts: 'int | None' = None
                                        ) -> list:
    """v4.14.5.83-leading-signals: return fire dicts for tickers
    showing unusual volume with small price movement.

    Shape (matches other trigger fire dicts):
        {'ticker': str, 'path': str, 'kind': 'volume_accumulation',
         'context': {'volume_ratio': float, 'recent_volume': int,
                     'avg_volume': float, 'price_drift_pct': float,
                     'last_close': float}}

    Eligibility:
      - Latest volume / VOLUME_ACCUMULATION_AVG_LOOKBACK_DAYS-trailing-
        avg >= VOLUME_ACCUMULATION_RATIO_THRESHOLD (2.5x default).
      - |price drift over same window| <
        VOLUME_ACCUMULATION_PRICE_MAX_ABS_DRIFT (3% default).
      - Ticker is NOT already analyzed under any path (same filter as
        fresh_universe_mover — analyzed-and-stale tickers go through
        the staleness trigger, not discovery).
      - Per-kind dedup window (6h) — same row shouldn't re-fire each
        sweep, but it's evaluated INSIDE the dispatch loop via
        `_within_kind_dedup`, not here. The trigger emits all
        candidates; downstream dedup filters them.

    Sorted by volume_ratio descending; capped at
    VOLUME_ACCUMULATION_PER_SWEEP_CAP (8).

    Defensive: any failure returns []. Skips immediately when
    `_LEADING_SIGNALS_ENABLED` is False (legacy pre-v.83 behavior).
    """
    if not _LEADING_SIGNALS_ENABLED:
        return []
    if now_ts is None:
        now_ts = int(time.time())

    conn = _conn(app)
    if conn is None:
        return []
    analyzed: set = set()
    try:
        with _db_lock(app):
            cur = conn.execute(
                "SELECT DISTINCT ticker FROM queue_runner_analysis_log")
            analyzed = {(r[0] or '').upper() for r in cur.fetchall()}
            analyzed.discard('')
    except Exception as e:
        _log_amber(
            app,
            f"check_volume_accumulation_triggers: analysis_log read "
            f"failed: {type(e).__name__}: {e}")
        return []

    try:
        import tm_cache as _tc
        cache_conn = _tc.get_connection()
    except Exception as e:
        _log_amber(
            app,
            f"check_volume_accumulation_triggers: cache connect "
            f"failed: {type(e).__name__}: {e}")
        return []

    from datetime import datetime as _dt, timedelta as _td
    window_cutoff = (_dt.now() - _td(days=21)).strftime('%Y-%m-%d')

    try:
        cur = cache_conn.execute(
            "SELECT ticker, date, close, volume FROM daily_bars "
            "WHERE date >= ? AND close IS NOT NULL "
            "AND volume IS NOT NULL "
            "ORDER BY ticker, date ASC",
            (window_cutoff,))
        rows = cur.fetchall()
    except Exception as e:
        _log_amber(
            app,
            f"check_volume_accumulation_triggers: daily_bars query "
            f"failed: {type(e).__name__}: {e}")
        try:
            cache_conn.close()
        except Exception:
            pass
        return []
    finally:
        try:
            cache_conn.close()
        except Exception:
            pass

    per_ticker: dict = {}
    for tk, d, close, vol in rows:
        tk_u = (tk or '').upper()
        if not tk_u:
            continue
        try:
            close_f = float(close)
            vol_i = int(vol or 0)
        except (TypeError, ValueError):
            continue
        if close_f <= 0 or vol_i <= 0:
            continue
        per_ticker.setdefault(tk_u, []).append(
            (str(d)[:10], close_f, vol_i))

    fires: list = []
    lookback = int(VOLUME_ACCUMULATION_AVG_LOOKBACK_DAYS)
    for tk_u, bars in per_ticker.items():
        if tk_u in analyzed:
            continue
        # Need at least lookback+1 bars to compute trailing avg.
        if len(bars) < lookback + 1:
            continue
        # bars sorted ascending. Latest = last; trailing avg from the
        # PREVIOUS `lookback` bars (excluding latest).
        latest_date, latest_close, latest_vol = bars[-1]
        prior = bars[-1 - lookback:-1]
        if len(prior) != lookback:
            continue
        avg_vol = sum(b[2] for b in prior) / float(lookback)
        if avg_vol <= 0:
            continue
        vol_ratio = latest_vol / avg_vol
        if vol_ratio < VOLUME_ACCUMULATION_RATIO_THRESHOLD:
            continue
        # Price-drift gate: small price move over the trailing window.
        ref_close = prior[0][1]   # close at start of trailing window
        if ref_close <= 0:
            continue
        drift = (latest_close - ref_close) / ref_close
        if abs(drift) >= VOLUME_ACCUMULATION_PRICE_MAX_ABS_DRIFT:
            continue   # price has already moved; mover trigger covers
        # Path assignment by current price (same split as v.82).
        if latest_close < FRESH_MOVER_LOTTERY_MAX_PRICE:
            path = 'lottery'
        else:
            path = 'aggressive'
        fires.append({
            'ticker': tk_u,
            'path': path,
            'kind': 'volume_accumulation',
            'context': {
                'kind': 'volume_accumulation',
                'volume_ratio': float(vol_ratio),
                'recent_volume': int(latest_vol),
                'avg_volume': float(avg_vol),
                'price_drift_pct': float(drift),
                'last_close': float(latest_close),
                'lookback_days': lookback,
                'latest_date': latest_date,
            },
        })

    # Sort by volume ratio descending (strongest accumulation first).
    fires.sort(
        key=lambda f: -float(f['context'].get('volume_ratio') or 0.0))
    return fires[:VOLUME_ACCUMULATION_PER_SWEEP_CAP]


# ─── v4.14.5.83-leading-signals: insider-buy trigger ─────────────────
#
# Fires on a FRESH insider open-market BUY signal from the
# `insider_flow` table (populated by tm_data_adapter_edgar.
# compute_and_store_insider_flow during filings refresh — see the
# build-note caveat: this table can be empty on machines whose
# filings refresh hasn't populated insider_flow yet; this trigger
# fires zero in that case and ramps up as the cache fills).
#
# Insider open-market BUYS only — sells, planned transactions, gifts,
# tax-withholding dispositions, and option exercises are all
# EXCLUDED at the parse layer (`_FORM4_OPEN_MARKET_CODES = {'P', 'S'}`
# at tm_data_adapter_edgar.py:632, and the trigger requires net-
# positive USD with at least one buy). The signal is "someone with
# inside view opening their own wallet" — that's the leading edge.

def check_insider_buy_triggers(app,
                                now_ts: 'int | None' = None
                                ) -> list:
    """v4.14.5.83-leading-signals: return fire dicts for tickers
    where the insider_flow row shows net-positive open-market BUY
    activity within the last INSIDER_BUY_MAX_AGE_DAYS days.

    Shape (matches other trigger fire dicts):
        {'ticker': str, 'path': str, 'kind': 'insider_buy',
         'context': {'net_open_market_usd': float, 'n_buys': int,
                     'n_sells': int, 'window_days': int,
                     'computed_at': str, 'last_close': float}}

    Eligibility:
      - `net_open_market_usd > INSIDER_BUY_MIN_NET_USD` (>= 0, i.e.
        any net-positive flow).
      - `n_buys >= INSIDER_BUY_MIN_BUYS` (at least one BUY).
      - `computed_at` is within INSIDER_BUY_MAX_AGE_DAYS days (14d
        default) — stale insider activity isn't an entry signal.
      - Ticker is NOT already analyzed under any path (same filter
        used by the other discovery triggers).
      - Per-kind dedup (7 days) — same insider_flow row doesn't
        re-fire every sweep; downstream `_within_kind_dedup` enforces.

    Sorted by `net_open_market_usd` descending (biggest insider
    conviction first); capped at INSIDER_BUY_PER_SWEEP_CAP (6).

    Defensive: any failure returns []. Skips immediately when
    `_LEADING_SIGNALS_ENABLED` is False.
    """
    if not _LEADING_SIGNALS_ENABLED:
        return []
    if now_ts is None:
        now_ts = int(time.time())

    conn = _conn(app)
    if conn is None:
        return []
    analyzed: set = set()
    try:
        with _db_lock(app):
            cur = conn.execute(
                "SELECT DISTINCT ticker FROM queue_runner_analysis_log")
            analyzed = {(r[0] or '').upper() for r in cur.fetchall()}
            analyzed.discard('')
    except Exception as e:
        _log_amber(
            app,
            f"check_insider_buy_triggers: analysis_log read failed: "
            f"{type(e).__name__}: {e}")
        return []

    try:
        import tm_cache as _tc
        cache_conn = _tc.get_connection()
    except Exception as e:
        _log_amber(
            app,
            f"check_insider_buy_triggers: cache connect failed: "
            f"{type(e).__name__}: {e}")
        return []

    from datetime import datetime as _dt, timedelta as _td

    try:
        # Pull every insider_flow row that's net-positive with at
        # least the min buys — sqlite filter so we don't pull the
        # full table.
        cur = cache_conn.execute(
            "SELECT ticker, net_open_market_usd, n_buys, n_sells, "
            "       window_days, computed_at "
            "FROM insider_flow "
            "WHERE net_open_market_usd > ? AND n_buys >= ?",
            (float(INSIDER_BUY_MIN_NET_USD),
             int(INSIDER_BUY_MIN_BUYS)))
        flow_rows = cur.fetchall()
    except Exception as e:
        _log_amber(
            app,
            f"check_insider_buy_triggers: insider_flow query failed: "
            f"{type(e).__name__}: {e}")
        try:
            cache_conn.close()
        except Exception:
            pass
        return []

    # We also need each candidate's latest close for path assignment.
    candidates: list = []
    age_cutoff = _dt.now() - _td(days=int(INSIDER_BUY_MAX_AGE_DAYS))
    for tk, net_usd, n_buys, n_sells, window_days, computed_at in flow_rows:
        tk_u = (tk or '').upper()
        if not tk_u:
            continue
        if tk_u in analyzed:
            continue
        # Freshness: parse computed_at; if it can't be parsed or is
        # older than the cutoff, skip.
        try:
            ca = _dt.fromisoformat(str(computed_at))
        except (ValueError, TypeError):
            continue
        if ca < age_cutoff:
            continue
        candidates.append((tk_u, float(net_usd), int(n_buys),
                           int(n_sells or 0),
                           int(window_days or 0),
                           str(computed_at)))

    # Pull latest closes for the candidates we still have, in ONE
    # query — much cheaper than per-ticker.
    closes: dict = {}
    if candidates:
        try:
            tk_list = [c[0] for c in candidates]
            # SQLite's IN clause with a parameter list (small N — at
            # most a few thousand insider_flow rows; cap protects the
            # query size if it ever explodes).
            placeholders = ",".join("?" * len(tk_list))
            cur = cache_conn.execute(
                f"SELECT ticker, MAX(date) AS d, close "
                f"FROM daily_bars "
                f"WHERE ticker IN ({placeholders}) "
                f"GROUP BY ticker",
                tk_list)
            for tk, _d, close in cur.fetchall():
                try:
                    closes[(tk or '').upper()] = float(close)
                except (TypeError, ValueError):
                    continue
        except Exception:
            # If we can't read closes, every candidate falls into the
            # 'aggressive' path by default (graceful degradation).
            closes = {}
    try:
        cache_conn.close()
    except Exception:
        pass

    fires: list = []
    for tk_u, net_usd, n_buys, n_sells, window_days, computed_at in candidates:
        last_close = closes.get(tk_u)
        if last_close is not None and last_close < FRESH_MOVER_LOTTERY_MAX_PRICE:
            path = 'lottery'
        else:
            path = 'aggressive'
        fires.append({
            'ticker': tk_u,
            'path': path,
            'kind': 'insider_buy',
            'context': {
                'kind': 'insider_buy',
                'net_open_market_usd': net_usd,
                'n_buys': n_buys,
                'n_sells': n_sells,
                'window_days': window_days,
                'computed_at': computed_at,
                'last_close': last_close,
            },
        })

    # Sort by net USD descending (biggest insider conviction first).
    fires.sort(
        key=lambda f: -float(
            f['context'].get('net_open_market_usd') or 0.0))
    return fires[:INSIDER_BUY_PER_SWEEP_CAP]


# ─── Priority + storm cap ────────────────────────────────────────────

def prioritize_and_cap_fires(fires: list) -> list:
    """Apply priority order + storm cap to a flat list of fires.

    Each fire is expected to be a dict with at least:
        {'ticker': str, 'path': str, 'kind': str, 'context': dict}

    Returns a list of up to STORM_FIRE_CAP fires, sorted so higher-
    priority kinds come first. Ties within a kind preserve input
    order (stable sort).

    In v4.14.4.0 only 'staleness' fires exist, so this function is
    mostly a passthrough + cap. The priority logic is here for
    v4.14.4.1+ where other kinds join.
    """
    if not fires:
        return []
    # Stable sort by TRIGGER_PRIORITY index; unknown kinds sort last.
    priority_index = {k: i for i, k in enumerate(TRIGGER_PRIORITY)}
    fallback_priority = len(TRIGGER_PRIORITY)

    def _key(fire):
        return priority_index.get(fire.get('kind'), fallback_priority)

    sorted_fires = sorted(fires, key=_key)
    return sorted_fires[:STORM_FIRE_CAP]


# ─── Auto-flip soak ──────────────────────────────────────────────────

def should_auto_flip(cfg: dict, now_ts: Optional[int] = None) -> bool:
    """Returns True if cfg['event_driven_refresh'] is False AND
    the soak window (AUTO_FLIP_DAYS = 14) has elapsed since
    cfg['event_driven_refresh_installed_at'] was first stamped.

    Returns False if:
    - The flag is already True (no need to flip)
    - installed_at is missing or unparseable (haven't seen v4.14.4.0
      run before; the runner will stamp it this session)
    - The soak window has NOT elapsed yet
    """
    if cfg.get('event_driven_refresh', False):
        return False
    installed_at = cfg.get('event_driven_refresh_installed_at')
    if installed_at is None:
        return False
    try:
        installed_ts = float(installed_at)
    except (TypeError, ValueError):
        return False
    if now_ts is None:
        now_ts = time.time()
    return (now_ts - installed_ts) >= AUTO_FLIP_SECONDS


# ─── Backoff ─────────────────────────────────────────────────────────

def compute_backoff_sleep(consecutive_empty: int) -> int:
    """Return sleep seconds for the runner's wait given the number
    of consecutive empty sweeps.

      0-2 empty -> 60s
      3-5 -> 120s
      6-8 -> 300s (5 min)
      9+ -> 600s (10 min)

    Reset to consecutive_empty=0 on any non-empty sweep before
    calling this again."""
    if consecutive_empty < 0:
        consecutive_empty = 0
    for threshold, seconds in BACKOFF_TIERS:
        if threshold is None:
            return seconds
        if consecutive_empty < threshold:
            return seconds
    # Defensive — BACKOFF_TIERS always ends with (None, ...) so this
    # is unreachable in normal flow.
    return 600


# ─── Sweep orchestration ─────────────────────────────────────────────

def evaluate_all_triggers(app) -> list:
    """Walk all triggers across all paths. v4.14.4.0 only runs the
    staleness trigger; subsequent patches add price/news/earnings/
    user. Returns a flat list of fire dicts ready for
    prioritize_and_cap_fires.

    Each fire dict shape:
        {'ticker': str, 'path': str, 'kind': str, 'context': dict}
    """
    # Lazy-resolve the path rotation to avoid circular imports.
    try:
        import tm_queue_runner
        rotation = tm_queue_runner._get_path_rotation()
    except Exception:
        rotation = tuple(STALENESS_WINDOWS_SECONDS.keys())

    fires: list = []
    now_ts = int(time.time())

    # v4.14.4.3: drain user signals ONCE per sweep (not per path).
    # Each pending signal already carries its target path. User
    # signals run first so they get priority slot in the sorted
    # output and are visible in the activity log even if a later
    # per-path check raises.
    try:
        user_fires = drain_user_signals(app, now_ts=now_ts)
    except Exception as e:
        _log_amber(
            app,
            f"evaluate_all_triggers: drain_user_signals raised: "
            f"{type(e).__name__}: {e}")
        user_fires = []
    fires.extend(user_fires)

    for path in rotation:
        try:
            staleness_fires = check_staleness_triggers(
                app, path, now_ts=now_ts)
        except Exception as e:
            _log_amber(
                app,
                f"evaluate_all_triggers: staleness check failed for "
                f"path={path}: {type(e).__name__}: {e}")
            staleness_fires = []
        for ticker, ctx in staleness_fires:
            fires.append({
                'ticker': ticker,
                'path': path,
                'kind': 'staleness',
                'context': ctx,
            })

        # v4.14.4.1: price-drift + target_stop sweep. Returns fires
        # tagged via context['kind'] = 'price_drift' or 'target_stop'.
        # Both kinds can fire for the same ticker — they're
        # independent signals collated at dispatch.
        try:
            price_fires = check_price_triggers(
                app, path, now_ts=now_ts)
        except Exception as e:
            _log_amber(
                app,
                f"evaluate_all_triggers: price check failed for "
                f"path={path}: {type(e).__name__}: {e}")
            price_fires = []
        for ticker, ctx in price_fires:
            fires.append({
                'ticker': ticker,
                'path': path,
                'kind': ctx.get('kind', 'price_drift'),
                'context': ctx,
            })

        # v4.14.4.2: news sweep. Fires when >= path-threshold articles
        # arrived since the analysis baseline. Sparsity reality on
        # day one: only ~8 of Mike's ~2,500 tickers have news_cache
        # data, so fires are sparse until background news refresh
        # lands in a future patch. Failure here doesn't suppress
        # staleness/price fires for the same path.
        try:
            news_fires = check_news_triggers(
                app, path, now_ts=now_ts)
        except Exception as e:
            _log_amber(
                app,
                f"evaluate_all_triggers: news check failed for "
                f"path={path}: {type(e).__name__}: {e}")
            news_fires = []
        for ticker, ctx in news_fires:
            fires.append({
                'ticker': ticker,
                'path': path,
                'kind': 'news',
                'context': ctx,
            })

        # v4.14.4.3: earnings sweep. Combined kind ('earnings') with
        # subkind ('upcoming'/'recent') in context. Per-path windows
        # via EARNINGS_TRIGGER_WINDOWS. Failure here doesn't suppress
        # other kinds for the same path.
        try:
            earnings_fires = check_earnings_triggers(
                app, path, now_ts=now_ts)
        except Exception as e:
            _log_amber(
                app,
                f"evaluate_all_triggers: earnings check failed for "
                f"path={path}: {type(e).__name__}: {e}")
            earnings_fires = []
        for ticker, ctx in earnings_fires:
            fires.append({
                'ticker': ticker,
                'path': path,
                'kind': 'earnings',
                'context': ctx,
            })

    # v4.14.5.82-discovery-unlock: ONE call (universe-wide, not per
    # path) for the discovery trigger. Lowest priority via
    # TRIGGER_PRIORITY; STORM_FIRE_CAP still bounds the global total
    # after `prioritize_and_cap_fires` sorts. Flag-off (gated inside
    # the trigger itself) → returns [] and this loop is a no-op.
    try:
        mover_fires = check_fresh_universe_mover_triggers(
            app, now_ts=now_ts)
    except Exception as e:
        _log_amber(
            app,
            f"evaluate_all_triggers: fresh_universe_mover check "
            f"failed: {type(e).__name__}: {e}")
        mover_fires = []
    fires.extend(mover_fires)

    # v4.14.5.83-leading-signals: two universe-wide leading-signal
    # discovery passes (insider_buy + volume_accumulation). Both
    # gated by `_LEADING_SIGNALS_ENABLED` inside the triggers
    # themselves; flag-off → both no-op. Same shape as the v.82
    # discovery wiring — append to the flat fires list and let
    # `prioritize_and_cap_fires` apply the global ordering + cap.
    try:
        insider_fires = check_insider_buy_triggers(
            app, now_ts=now_ts)
    except Exception as e:
        _log_amber(
            app,
            f"evaluate_all_triggers: insider_buy check failed: "
            f"{type(e).__name__}: {e}")
        insider_fires = []
    fires.extend(insider_fires)
    try:
        vol_fires = check_volume_accumulation_triggers(
            app, now_ts=now_ts)
    except Exception as e:
        _log_amber(
            app,
            f"evaluate_all_triggers: volume_accumulation check "
            f"failed: {type(e).__name__}: {e}")
        vol_fires = []
    fires.extend(vol_fires)
    return fires
