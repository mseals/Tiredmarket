"""tm_lane_pacing — v4.14.5.76-adaptive-lane-pacing.

Per-data-lane adaptive pacing controller. The data lanes (EDGAR for
filings+fundamentals, Yahoo-price for daily_bars) have historically used
STATIC pacing constants tuned for one operating condition. That's wrong
for end users on variable/shared/fluctuating connections (household
bandwidth, gaming, multi-device contention, Wi-Fi switches). This module
is the controller that watches real request outcomes and self-tunes each
lane's request interval — tighten when clean, back off on throttle
signals — so each user converges on the safe edge of their OWN
connection without a probe.

Architecture
============

Shape modeled on tm_provider_learning (the AI-provider cap learner —
same defensive-half + offensive-half + persistence pattern, just on the
inter-request-interval axis instead of the daily-count axis).

  • Per-lane outcome deque (sliding ~60s window) of
    (timestamp, success, was_429, retry_after, latency) tuples. Filled
    by `record_outcome()` calls at the data adapters' HTTP sites.
  • `tick(log_fn)` — called from the existing tm_scheduler tick (no
    new thread). Walks managed lanes, reads recent outcomes, decides:
      - any 429 → back off (× BACKOFF_MULT, honor explicit Retry-After
        if larger), reset clean-window counter.
      - zero 429s AND enough clean windows → tighten by × TIGHTEN_MULT,
        clamped to the per-lane floor.
      - latency rising sharply with no 429s yet → HOLD (don't tighten),
        a soft brake before the hard 429 brake.
  • Writes the chosen interval to the lane's existing mutable module
    attribute (e.g. tm_data_adapter_edgar._MIN_INTERVAL_SEC). The
    investigation confirmed those constants are runtime-mutable; no
    refactor needed.
  • State persisted to `data/lane_pacing.json` so learned pacing
    survives restarts — with a re-seed sanity check so a one-off bad
    session doesn't permanently pin a slow pace.

Safety
======

The controller sits ON TOP of the existing backstop machinery — it does
NOT replace any of:
  • EDGAR `_polite_wait` (still enforces the interval the controller
    sets, whatever that is — and the lock serializes globally).
  • Yahoo reactive cooldown (`_yahoo_price_in_cooldown` /
    `tm_data_providers.Registry.record_failure` 60s/5min/30min ladder).
  • News per-source timeout `_NEWS_PER_SOURCE_TIMEOUT_SEC`.
  • `tm_provider_health.record_rate_limit` + the v4.14.5.71 per-minute
    cooldown cap.

If the controller tightens too far and a 429 results, BOTH the existing
cooldown trips AND the controller's next tick sees the 429 and backs
off. Two independent brakes.

If the controller is buggy or misbehaves: set `cfg['use_adaptive_lane_
pacing'] = False` and restart. The lanes return to their compile-time
static constants. Audit-enforced.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable


# ─── Per-lane bounds + tuning ─────────────────────────────────────────
#
# `seed`     = the starting interval used when no persistence exists or
#              re-seed sanity triggers. Matches today's static constant
#              so byte-identical first-tick behavior pre/post-patch.
# `floor`    = hardest tightening allowed. EDGAR's SEC cap is 10/sec
#              (0.1s); we floor at 0.15s for ~6.7/sec, leaving documented
#              buffer rather than crashing into the wall. Yahoo has no
#              published cap and already trips reactive cooldowns, so
#              its floor stays cautious at 0.3s (~3.3/sec).
# `ceiling`  = the slowest interval the controller will ever pin —
#              prevents a bad streak from permanently throttling.
# `attr`     = (module-name, attribute-name) the controller writes to.
# `module`   = imported lazily so this module is safe to import early.

LANE_BOUNDS = {
    'edgar': {
        'seed':    0.5,
        'floor':   0.15,
        'ceiling': 2.0,
        'attr':    ('tm_data_adapter_edgar', '_MIN_INTERVAL_SEC'),
    },
    'yahoo_price': {
        'seed':    0.5,
        'floor':   0.3,
        'ceiling': 5.0,
        'attr':    ('tm_fill_executor',
                    '_CHUNKED_INTER_CHUNK_DELAY_SECONDS'),
    },
    # v4.14.6.111: Yahoo EARNINGS (Ticker.calendar) lane. Earnings was previously
    # unpaced by this controller — its only throttle was the fundfile daemon's
    # flat 55/min (sized as a buffer under FINNHUB's 60/min, NOT Yahoo's lower
    # .calendar ceiling), so a ~33s burst of 30 seed fetches overran Yahoo and
    # tripped a reactive 60s cooldown. SEEDED CONSERVATIVELY: 3.0s (~20/min) is
    # clearly under the 55/min that demonstrably trips it; the controller tightens
    # toward the floor on clean windows and backs off toward the ceiling on a 429,
    # self-tuning to Yahoo's real (unknown) earnings ceiling from observed
    # outcomes. floor 1.5s (~40/min) stays under the trip rate even fully tight.
    'yahoo_earnings': {
        'seed':    3.0,
        'floor':   1.5,
        'ceiling': 20.0,
        'attr':    ('tm_fundfile_fetcher', '_EARNINGS_MIN_INTERVAL_SEC'),
    },
}

# Tighten/back-off multipliers. Conservative on the way down (×0.85 ≈
# 15% faster per tick), more aggressive on the way up (×1.5) — easier
# to recover from being too slow than from being throttled.
TIGHTEN_MULT = 0.85
BACKOFF_MULT = 1.5

# How many consecutive clean ticks before the controller starts
# tightening from a backed-off state. Avoids tightening into a brief
# lull between throttle bursts.
MIN_CLEAN_TICKS_BEFORE_TIGHTEN = 2

# Sliding outcome window — entries older than this are dropped on each
# tick. ~60s lines up with provider Retry-After windows + AI sliding-
# window deques, and is wide enough to absorb a single transient blip
# without thrashing.
OUTCOME_WINDOW_SECONDS = 60.0

# Latency-rise hold (soft brake). If the median latency of the last
# OUTCOME_WINDOW_SECONDS is more than 2× the long-run median (or 2× the
# seed) AND zero 429s have hit, HOLD steady this tick instead of
# tightening. This is the "leading indicator" — connection getting
# slow, predict the 429 before it lands. Conservative: only HOLDS, never
# backs off without an actual 429.
LATENCY_RISE_MULT = 2.0

# Re-seed sanity: on startup, if the persisted interval is within this
# fraction of the ceiling, the last session ended throttled. Don't
# blindly trust it — re-seed to the lane's `seed` and let the
# controller re-discover the edge. A bad night must not pin slow forever.
NEAR_CEILING_FRAC = 0.8

# Persistence cadence — debounce save() so we don't hammer disk.
SAVE_DEBOUNCE_SEC = 30.0


# ─── Module state ─────────────────────────────────────────────────────

_lock = threading.RLock()
_enabled: bool = True
_state: dict = {}  # lane -> {interval, last_change_at, consecutive_clean_windows}
_outcomes: dict = {}  # lane -> deque[(ts, success, was_429, retry_after, latency)]
_persist_path: Optional[Path] = None
_last_save_at: float = 0.0


def _now_iso() -> str:
    return datetime.now().isoformat(timespec='seconds')


def set_enabled(enabled: bool) -> None:
    """Master kill switch. When False, the controller becomes inert:
    `tick()` returns immediately without adjusting any lane, and
    `record_outcome()` still appends (cheap) but nothing acts on it.
    Restart in this state restores the lanes' compile-time constants."""
    global _enabled
    with _lock:
        _enabled = bool(enabled)


def is_enabled() -> bool:
    return _enabled


def init(data_dir: Optional[Path] = None,
         enabled: bool = True,
         log_fn: Optional[Callable] = None) -> None:
    """One-time setup at app start. Loads persisted state, applies the
    re-seed sanity check, writes the initial interval to each lane's
    module attribute. Safe to call multiple times (idempotent — second
    call is a no-op other than rewiring log_fn).
    """
    global _persist_path
    set_enabled(enabled)
    if data_dir is not None:
        _persist_path = Path(data_dir) / 'lane_pacing.json'
    with _lock:
        _load_locked()
        # Re-seed sanity per lane. AFTER load_locked so we see the
        # actually-persisted value.
        for lane, bounds in LANE_BOUNDS.items():
            rec = _state.setdefault(lane, _default_lane_state(lane))
            cur = float(rec.get('interval', bounds['seed']))
            near_ceiling = (cur >= bounds['ceiling'] * NEAR_CEILING_FRAC)
            if near_ceiling:
                rec['interval'] = bounds['seed']
                rec['last_change_at'] = _now_iso()
                rec['reseed_reason'] = (
                    f"persisted interval {cur:.3f}s was near ceiling "
                    f"({bounds['ceiling']:.2f}s) — re-seeded to "
                    f"{bounds['seed']:.3f}s; controller will rediscover edge")
                if log_fn:
                    try:
                        log_fn(
                            f"[lane_pacing] {lane}: re-seed "
                            f"{cur:.3f}s -> {bounds['seed']:.3f}s "
                            f"(was near ceiling {bounds['ceiling']:.2f}s)",
                            'muted')
                    except Exception:
                        pass
            # Apply to the lane's module attribute right now so first
            # request after init uses the right interval.
            _apply_locked(lane, float(rec['interval']))
        _outcomes.setdefault('edgar', deque())
        _outcomes.setdefault('yahoo_price', deque())
        _outcomes.setdefault('yahoo_earnings', deque())   # v4.14.6.111
        _save_locked(force=True)


def _default_lane_state(lane: str) -> dict:
    bounds = LANE_BOUNDS.get(lane, {})
    return {
        'interval': float(bounds.get('seed', 0.5)),
        'last_change_at': _now_iso(),
        'consecutive_clean_windows': 0,
        'reseed_reason': '',
    }


def _load_locked() -> None:
    """Read the persisted JSON if it exists. Best-effort: any error
    leaves state at defaults rather than raising."""
    if _persist_path is None or not _persist_path.exists():
        for lane in LANE_BOUNDS:
            _state.setdefault(lane, _default_lane_state(lane))
        return
    try:
        with open(_persist_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        for lane in LANE_BOUNDS:
            _state.setdefault(lane, _default_lane_state(lane))
        return
    if not isinstance(data, dict):
        for lane in LANE_BOUNDS:
            _state.setdefault(lane, _default_lane_state(lane))
        return
    for lane in LANE_BOUNDS:
        rec = data.get(lane)
        if isinstance(rec, dict) and 'interval' in rec:
            # Coerce to a sane float between (floor, ceiling) — never trust
            # a corrupt persisted value to bypass bounds.
            b = LANE_BOUNDS[lane]
            try:
                iv = float(rec.get('interval'))
            except Exception:
                iv = b['seed']
            iv = max(b['floor'], min(b['ceiling'], iv))
            _state[lane] = {
                'interval': iv,
                'last_change_at': rec.get(
                    'last_change_at', _now_iso()),
                'consecutive_clean_windows': int(
                    rec.get('consecutive_clean_windows', 0) or 0),
                'reseed_reason': str(rec.get('reseed_reason') or ''),
            }
        else:
            _state[lane] = _default_lane_state(lane)


def _save_locked(force: bool = False) -> None:
    """Persist state to disk. Debounced unless force=True."""
    global _last_save_at
    if _persist_path is None:
        return
    now = time.time()
    if not force and (now - _last_save_at) < SAVE_DEBOUNCE_SEC:
        return
    _last_save_at = now
    try:
        _persist_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = _persist_path.with_suffix('.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_state, f, indent=2)
        tmp.replace(_persist_path)
    except Exception:
        # Persistence is best-effort — never crash the controller.
        pass


def _apply_locked(lane: str, interval: float) -> None:
    """Write the new interval to the lane's mutable module attribute."""
    bounds = LANE_BOUNDS.get(lane)
    if not bounds:
        return
    mod_name, attr_name = bounds['attr']
    try:
        import importlib
        mod = importlib.import_module(mod_name)
        setattr(mod, attr_name, float(interval))
    except Exception:
        # If the target module isn't importable yet, the apply is a no-op
        # — the lane keeps its compile-time constant. Next tick retries.
        pass


# ─── Outcome recording (called from adapters) ──────────────────────────

def record_outcome(lane: str,
                   success: bool,
                   was_429: bool = False,
                   retry_after: Optional[float] = None,
                   latency: Optional[float] = None) -> None:
    """Append one outcome to the lane's sliding window. Called by data
    adapters right after a request completes (success OR failure). Safe
    to call before init() (silently buffers — entries discarded if no
    matching lane).

    Args:
        lane:       'edgar' / 'yahoo_price' (see LANE_BOUNDS keys).
        success:    True if the request returned usable data, False on
                    any HTTP error / exception / timeout.
        was_429:    True ONLY if the failure was a rate-limit 429. Other
                    errors (404, network, parse) MUST set was_429=False —
                    they're not throttle signals.
        retry_after: explicit Retry-After seconds from the 429, if
                    parseable. The controller will honor this over its
                    own BACKOFF_MULT when larger.
        latency:    elapsed seconds from request start. Used by the
                    soft-brake latency-rise heuristic. Optional.
    """
    if lane not in LANE_BOUNDS:
        return
    with _lock:
        dq = _outcomes.setdefault(lane, deque())
        dq.append((
            time.time(),
            bool(success),
            bool(was_429),
            float(retry_after) if retry_after is not None else None,
            float(latency) if latency is not None else None,
        ))


def _trim_locked(lane: str, now: float) -> None:
    dq = _outcomes.get(lane)
    if dq is None:
        return
    cutoff = now - OUTCOME_WINDOW_SECONDS
    while dq and dq[0][0] < cutoff:
        dq.popleft()


# ─── Per-tick adjustment (called from scheduler) ───────────────────────

def tick(log_fn: Optional[Callable] = None) -> dict:
    """Walk managed lanes, adjust intervals based on the last
    OUTCOME_WINDOW_SECONDS of outcomes. Called by the existing scheduler
    tick — no new thread, no new daemon. Cheap: ~O(lanes × outcomes).

    Returns a dict {lane: {old, new, reason}} for any lane whose
    interval changed this tick (empty dict if nothing changed). Useful
    for tests + activity-log surfacing.
    """
    if not _enabled:
        return {}
    changes: dict = {}
    now = time.time()
    with _lock:
        for lane, bounds in LANE_BOUNDS.items():
            _trim_locked(lane, now)
            rec = _state.setdefault(lane, _default_lane_state(lane))
            dq = _outcomes.get(lane) or deque()
            cur = float(rec.get('interval', bounds['seed']))
            new = cur
            reason = ''

            n_total = len(dq)
            n_429s = sum(1 for e in dq if e[2])
            max_retry_after = max(
                (e[3] for e in dq if e[2] and e[3] is not None),
                default=None)
            latencies = [e[4] for e in dq if e[4] is not None]
            med_latency = (sorted(latencies)[len(latencies) // 2]
                           if latencies else None)

            if n_429s > 0:
                # Defensive: back off. ×BACKOFF_MULT OR Retry-After,
                # whichever's larger. Clamp to ceiling so we never
                # exceed it (the re-seed sanity check rescues us if we
                # somehow do).
                base = cur * BACKOFF_MULT
                if max_retry_after is not None:
                    base = max(base, max_retry_after)
                new = min(bounds['ceiling'], base)
                reason = (f'{n_429s} 429(s) in last '
                          f'{int(OUTCOME_WINDOW_SECONDS)}s; '
                          f'back off ×{BACKOFF_MULT}')
                if max_retry_after is not None and max_retry_after > cur * BACKOFF_MULT:
                    reason += (f' (honoring Retry-After '
                               f'{max_retry_after:.1f}s)')
                rec['consecutive_clean_windows'] = 0
            elif n_total == 0:
                # No outcomes this window — likely an idle lane between
                # passes. Do nothing; don't blindly tighten on no data.
                rec['consecutive_clean_windows'] = (
                    int(rec.get('consecutive_clean_windows', 0)) + 1)
            else:
                # Clean window.
                rec['consecutive_clean_windows'] = (
                    int(rec.get('consecutive_clean_windows', 0)) + 1)
                # Soft brake: latency rising sharply → hold steady this
                # tick instead of tightening. We compare median latency
                # to (LATENCY_RISE_MULT × seed) as a stable baseline —
                # not to a rolling baseline that would drift with us.
                latency_hot = (
                    med_latency is not None
                    and med_latency >= bounds['seed'] * LATENCY_RISE_MULT)
                if latency_hot:
                    reason = (f'clean window but median latency '
                              f'{med_latency:.2f}s >= '
                              f'{LATENCY_RISE_MULT}× seed — HOLD')
                    # No change; soft brake.
                elif (rec['consecutive_clean_windows']
                      >= MIN_CLEAN_TICKS_BEFORE_TIGHTEN
                      and cur > bounds['floor']):
                    tightened = max(bounds['floor'], cur * TIGHTEN_MULT)
                    new = tightened
                    reason = (f'clean window {n_total} req '
                              f'(no 429s, {rec["consecutive_clean_windows"]} '
                              f'consecutive clean) — tighten ×{TIGHTEN_MULT}')
                # else: not yet enough clean ticks, OR already at floor —
                # hold.

            if abs(new - cur) > 1e-6:
                rec['interval'] = new
                rec['last_change_at'] = _now_iso()
                rec['reseed_reason'] = ''  # any change clears the re-seed note
                _apply_locked(lane, new)
                changes[lane] = {
                    'old': cur, 'new': new, 'reason': reason,
                }
                if log_fn:
                    try:
                        log_fn(
                            f"[lane_pacing] {lane}: "
                            f"{cur:.3f}s -> {new:.3f}s — {reason}",
                            'muted')
                    except Exception:
                        pass

        _save_locked(force=bool(changes))
    return changes


# ─── Read accessors (UI / audit / debugging) ──────────────────────────

def get_interval(lane: str) -> float:
    """Current interval for the lane. Reads in-memory state; falls back
    to the seed if the lane isn't initialized."""
    with _lock:
        rec = _state.get(lane)
        if rec and 'interval' in rec:
            try:
                return float(rec['interval'])
            except Exception:
                pass
        return float(LANE_BOUNDS.get(lane, {}).get('seed', 0.5))


def get_state_snapshot() -> dict:
    """Diagnostic snapshot for the activity log / settings panel."""
    with _lock:
        return {
            'enabled': _enabled,
            'lanes': {
                lane: {
                    'interval': float(_state.get(lane, {}).get(
                        'interval', LANE_BOUNDS[lane]['seed'])),
                    'floor': LANE_BOUNDS[lane]['floor'],
                    'ceiling': LANE_BOUNDS[lane]['ceiling'],
                    'consecutive_clean_windows': int(_state.get(
                        lane, {}).get('consecutive_clean_windows', 0)),
                    'recent_outcomes': len(_outcomes.get(lane, [])),
                    'recent_429s': sum(
                        1 for e in (_outcomes.get(lane) or [])
                        if e[2]),
                }
                for lane in LANE_BOUNDS
            },
        }


def _reset_for_tests() -> None:
    """Clear all in-memory state. NOT a production API — used only by
    the audit so each test starts from a clean slate."""
    global _enabled, _last_save_at
    with _lock:
        _enabled = True
        _state.clear()
        _outcomes.clear()
        _last_save_at = 0.0
