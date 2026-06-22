"""tm_quarantine — v4.14.6.63 fill-quarantine for daily_bars (S1).

Tracks consecutive bulk-fetch failures per (lane, ticker) and quarantines
tickers that fail N times in a row so they stop consuming both bulk-
fetch slots and the slow per-ticker fallback. A quarantined ticker is
skipped entirely for a 30-day window, then granted one retry attempt:
success clears the quarantine; failure re-quarantines for another 30
days.

Why this exists
---------------
The v4.14.6.63 diagnostic (S1) showed that ~98% of the "169 need
per-ticker fallback" cohort is a PERSISTENT never-fillable set —
warrants/units/preferred symbol classes Yahoo's bulk endpoint doesn't
serve, plus delisted/consolidated residue. They have always re-failed,
will always re-fail, and were burning ~18 min of wall-clock per fill
cycle (8 s/ticker × ~137 tickers) on guaranteed-doomed attempts.

A regex pre-filter on symbol-class suffixes was rejected after Phase 0
showed 1,130 false positives (real fillable stocks like BIDU, CHTR,
DLTR, INTU matched the trailing-W/U/R rule). The seed-universe filter
was rejected because all 137 cohort tickers ARE in the seeded universe
(the broad iwv Russell 3000 set DOES include warrants). Quarantine is
behavioral, zero false-positive risk, and converges in 3 fill cycles.

Locked parameters (user-approved, v4.14.6.63):
    QUARANTINE_THRESHOLD = 3 consecutive bulk-fails
    QUARANTINE_WINDOW    = 30 days

State file: data/quarantine.json (per-install runtime state — not
shipped in Public). Fail-safe: any read/write error treats state as
empty (no tickers quarantined). Never crashes the fill.

Boundaries (must not regress):
    - v62 NULL-close guard: quarantine acts at fetch ENTRY (pre-yfinance);
      the v62 write-path guard is downstream and untouched.
    - Freshness gate: quarantined tickers are skipped entirely — their
      cache_metadata.have_to_date is NEVER advanced as a side-effect of
      the skip, so the gate behaves identically.
    - v56 per-type cooldowns and v60 reliability sort key on
      (provider, data_type) — never see ticker-level state.
    - Legitimate transient failures still get their per-ticker fallback:
      quarantine only triggers after N=3 CONSECUTIVE bulk-fails, so a
      one-off transient never marks a ticker.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

QUARANTINE_THRESHOLD = 3
QUARANTINE_WINDOW_DAYS = 30

_STATE_FILENAME = 'quarantine.json'
# RLock so nested acquisition by the same thread (e.g.
# filter_eligible → _lane_bucket → _load) does not deadlock.
_lock = threading.RLock()

# In-memory cache of the state file. Loaded lazily on first access.
# Shape:
#   {
#     'version': 1,
#     'lanes': {
#       'daily_bars': {
#         'fails':              {ticker: int},
#         'quarantined_until':  {ticker: 'YYYY-MM-DD'},
#       },
#       ...
#     },
#   }
_state: Optional[dict] = None
_state_path: Optional[Path] = None


def _resolve_state_path() -> Path:
    """Resolve data/quarantine.json next to the app's other JSON state
    files. Uses `data/` next to this module — equivalent to
    tired_market.USER_DATA_DIR for both main and Public installs, and
    avoids the cost (and potential hang) of importing the 1.6 MB
    tired_market module just to read one constant.

    If the user's install lives elsewhere via a USER_DATA_DIR override,
    callers can override this by reassigning the module-level
    `_state_path` to the desired Path before any other call.
    """
    global _state_path
    if _state_path is not None:
        return _state_path
    import tm_paths
    _state_path = tm_paths.get_data_dir() / _STATE_FILENAME
    return _state_path


def _empty_state() -> dict:
    return {'version': 1, 'lanes': {}}


def _load() -> dict:
    """Load and return the state dict (cached). Fail-safe: any read or
    parse error returns an empty state — never raises."""
    global _state
    with _lock:
        if _state is not None:
            return _state
        path = _resolve_state_path()
        try:
            if not path.exists():
                _state = _empty_state()
                return _state
            with open(path, encoding='utf-8') as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                _state = _empty_state()
                return _state
            data.setdefault('version', 1)
            data.setdefault('lanes', {})
            if not isinstance(data['lanes'], dict):
                data['lanes'] = {}
            _state = data
            return _state
        except Exception:
            _state = _empty_state()
            return _state


def _save_locked(state: dict) -> None:
    """Persist state to disk via atomic write. Caller holds _lock.
    Fail-safe: write errors are silently swallowed."""
    path = _resolve_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(state, fh, indent=2)
        os.replace(str(tmp), str(path))
    except Exception:
        pass


def _lane_bucket(lane: str) -> dict:
    """Return the per-lane sub-dict (creating it if absent). Caller holds
    _lock OR is fine with last-writer-wins on a brand-new lane."""
    state = _load()
    lanes = state.setdefault('lanes', {})
    bucket = lanes.setdefault(lane, {})
    bucket.setdefault('fails', {})
    bucket.setdefault('quarantined_until', {})
    return bucket


def _today_iso() -> str:
    return date.today().isoformat()


def is_quarantined(lane: str, ticker: str, today_iso: Optional[str] = None
                    ) -> bool:
    """True iff ticker is under an unexpired quarantine for this lane.
    A retry-window expiry is NOT auto-cleared here — the next call to
    filter_eligible() handles the one-attempt-on-expiry path."""
    if not lane or not ticker:
        return False
    today_iso = today_iso or _today_iso()
    with _lock:
        bucket = _lane_bucket(lane)
        until = bucket['quarantined_until'].get(ticker)
        return bool(until and until > today_iso)


def filter_eligible(lane: str, tickers,
                     today_iso: Optional[str] = None
                     ) -> tuple[list[str], list[str], list[str]]:
    """Partition `tickers` into (eligible, skipped, retry).

    eligible — tickers neither quarantined nor on a retry window today.
    skipped  — tickers under an unexpired quarantine (skip entirely).
    retry    — tickers whose `quarantined_until <= today` — granted ONE
               bulk attempt this cycle. They appear in `eligible` too
               (caller submits them to bulk), and `record_bulk_outcome`
               handles the success/failure verdict to either clear or
               re-quarantine.

    Preserves caller's order. Never raises.
    """
    today_iso = today_iso or _today_iso()
    eligible: list[str] = []
    skipped: list[str] = []
    retry: list[str] = []
    if not lane:
        return list(tickers), [], []
    with _lock:
        bucket = _lane_bucket(lane)
        until_map = bucket['quarantined_until']
        for t in tickers:
            until = until_map.get(t)
            if not until:
                eligible.append(t)
                continue
            if until > today_iso:
                skipped.append(t)
            else:
                # Retry window has opened — grant ONE attempt and let
                # record_bulk_outcome decide whether to clear or
                # re-quarantine based on what happens.
                eligible.append(t)
                retry.append(t)
    return eligible, skipped, retry


def record_bulk_outcome(lane: str, submitted, filled,
                         today_iso: Optional[str] = None) -> dict:
    """Update fail counters and auto-quarantine after a chunked bulk
    phase. Call EXACTLY ONCE per phase with the full submitted set
    and the resulting filled set.

    Behavior:
      - A ticker in `filled` resets its fail counter to 0 and clears
        any quarantine (success on a retry-window attempt clears it).
      - A ticker in `submitted` but NOT in `filled` increments its
        fail counter. At THRESHOLD, it gets `quarantined_until = today
        + WINDOW_DAYS`. If it was already on a retry-window attempt
        this cycle (state had a quarantined_until <= today), the
        failure re-quarantines it (counter is reset on entry to the
        retry path so re-quarantine is symmetric with the first
        quarantine).
      - State is persisted atomically. Returns a summary dict for
        logging:
            {'newly_quarantined': [...],
             'cleared': [...],
             'retry_failed_requarantined': [...]}

    Never raises. submitted/filled may be any iterable.
    """
    submitted = set(submitted or [])
    filled = set(filled or [])
    not_filled = submitted - filled
    today_iso = today_iso or _today_iso()
    new_until = (date.fromisoformat(today_iso)
                  + timedelta(days=QUARANTINE_WINDOW_DAYS)).isoformat()
    summary = {
        'newly_quarantined': [],
        'cleared': [],
        'retry_failed_requarantined': [],
    }
    if not lane or not submitted:
        return summary
    with _lock:
        bucket = _lane_bucket(lane)
        fails = bucket['fails']
        until_map = bucket['quarantined_until']

        # Successes: clear counter, clear quarantine entirely (whether
        # this was a normal success or a retry-window success).
        for t in filled:
            if t in fails:
                fails.pop(t, None)
            if t in until_map:
                until_map.pop(t, None)
                summary['cleared'].append(t)

        # Failures: increment counter; quarantine at threshold.
        for t in not_filled:
            was_on_retry = (t in until_map
                             and until_map[t] <= today_iso)
            if was_on_retry:
                # Re-quarantine immediately; reset counter so the
                # next 3-strike clock starts from this re-quarantine
                # event.
                until_map[t] = new_until
                fails[t] = 0
                summary['retry_failed_requarantined'].append(t)
                continue
            n = int(fails.get(t, 0)) + 1
            fails[t] = n
            if n >= QUARANTINE_THRESHOLD:
                until_map[t] = new_until
                fails[t] = 0  # reset for symmetry; quarantine_until is the gate now
                summary['newly_quarantined'].append(t)

        _save_locked(_load())
    return summary


def status_snapshot(lane: Optional[str] = None) -> dict:
    """Read-only snapshot for diagnostics / UI. If lane is None, returns
    all lanes. Returns a deep-ish copy so callers can mutate freely."""
    with _lock:
        state = _load()
        lanes = state.get('lanes', {})
        if lane is None:
            return {k: {
                'fails': dict(v.get('fails', {})),
                'quarantined_until': dict(v.get('quarantined_until', {})),
            } for k, v in lanes.items()}
        v = lanes.get(lane, {})
        return {
            'fails': dict(v.get('fails', {})),
            'quarantined_until': dict(v.get('quarantined_until', {})),
        }


def reset_lane(lane: str) -> None:
    """Wipe all quarantine state for a lane. Intended for tests and
    one-off operational use; not wired to any UI."""
    with _lock:
        state = _load()
        if lane in state.get('lanes', {}):
            state['lanes'][lane] = {'fails': {}, 'quarantined_until': {}}
            _save_locked(state)
