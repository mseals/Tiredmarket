# ─── Audit testing note ──────────────────────────────────────────────
#
# When auditing the queue runner, mock at the integration boundary, not
# above it. Tests should use a real tm_cache connection against a temp
# cache.db fixture with sample daily_bars rows. Do NOT use a FakeApp
# that bypasses tm_cache queries entirely — that masks integration bugs
# at the cache lookup boundary. The Phase 1 audit's
# _build_candidate_shortlist returning empty was invisible because the
# audit had no real cache; the spec said pull from cache but the
# implementation pulled from a universe object that was never
# populated. The cadence-honor test (CHECK 8) and candidate-shortlist
# test (CHECK 9) added in May 2026 are the canonical examples of the
# right pattern. See sibling notes in tm_teacher_intercept.py:1417 and
# tm_top_ai_picker.py (above pick_top_ai).
#
# Also: the cadence stamp lives at the START of run_one_pass, not the
# end. This is intentional — the function has multiple early-return
# paths (no candidates, pick failure) that would skip an end-of-function
# stamp. May 2026 had a bug where the runner fired every 60 seconds
# instead of every 15 minutes because the no-candidates early-return
# bypassed the end-of-function stamp.
#
# CHECK 8 (cadence stamp) tested whether the second pass would FIRE,
# but didn't test whether it would LOG. The May 2026 cadence-regression
# bug — second pass suppressed by steady-state state-changed check —
# was invisible to that audit because the suppression logic was
# OUTSIDE the cadence boundary CHECK 8 tested. CHECK 10 added later
# tests the user-visible boundary (does the log line actually appear?)
# — same lesson repeating: test at the boundary that matters to the
# user, not just the engineering boundary one layer above.


"""tm_queue_runner — continuous Recommend queue background worker.

Phase 1 of the v4.15.0 continuous-queue + Verify feature. This module
maintains a ranked queue of candidate stocks in the recommend_queue
SQLite table. The top-picked AI (via tm_top_ai_picker) runs against
candidates from the cache and inserts BUY recommendations into the
queue. Housekeeping pass invalidates picks whose underlying price has
shifted too much, and graduates picks that hit their target or stop.

PHASE 1 SCOPE: infrastructure only. Recommend window does NOT read
from this queue yet (cfg['use_continuous_queue'] defaults False).
The queue accumulates silently in the background. Phase 2 will wire
the Recommend window read + Verify button.

Public API:
    start_queue_runner(app) -> threading.Thread
    stop_queue_runner(app) -> None

Cadence: hybrid. Wakes every cfg['queue_runner_interval_min'] minutes
(default 15) OR immediately when cfg['auto_refresh_last_run'] advances
(meaning the data refresh tick fired). Pause-state respecting: skips
passes when AI is globally paused, a scan is running, the discover
cooldown is active, or game-pause is in effect.

Failure handling: best-effort throughout. Per-candidate exceptions
don't abort the pass. Top-AI failures emit the right system_event
(via tm_teacher_intercept's Phase 2 infrastructure) and continue
to the next cycle.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Optional


# v4.14.3.11 (2026-05-15): the v4.14.3.8 _last_call_by_provider module
# dict was removed. It was the runner-side throttle state for the
# single-provider-per-pass dispatch model — under v4.14.3.11's load
# distribution the rate limiter's per-provider state (tm_rate_limiter
# _LIMITERS dict, consulted by acquire_for_provider inside call_provider)
# is the canonical throttle and the runner-side belt-and-suspenders
# layer would falsely apply one provider's spacing between calls
# hitting different providers.


# v4.14.3.10 (2026-05-15): pass-rotation across the five paths.
# Cursor lives in cfg['queue_runner_path_cursor'] (default 0). Order
# is derived from tm_holdings.PATHS.keys() — declaration order, which
# is slow_safe -> moderate -> aggressive -> lottery -> penny_lottery
# as of v4.14.3.10. We DON'T duplicate the list as a constant — a
# future patch that adds a sixth path to tm_holdings.PATHS will
# extend the rotation automatically. Lazy import inside the helper
# avoids the circular-import risk at module load.
_DEFAULT_PATH_ROTATION_FALLBACK = (
    'slow_safe', 'moderate', 'aggressive', 'lottery', 'penny_lottery'
)


def _get_path_rotation() -> tuple:
    """Return the ordered tuple of path identifiers the queue runner
    rotates through. Reads from tm_holdings.PATHS.keys() so the
    rotation order stays in sync with the canonical PATHS dict. If
    tm_holdings can't be imported (shouldn't happen in production,
    but defensive against test fixtures that monkey-patch sys.modules),
    falls back to the hardcoded tuple."""
    try:
        import tm_holdings
        paths = tuple(tm_holdings.PATHS.keys())
        if paths:
            return paths
    except Exception:
        pass
    return _DEFAULT_PATH_ROTATION_FALLBACK


def _read_rotation_path(app) -> tuple:
    """Return (cursor_idx, rotation_path) for this pass.

    Reads cfg['queue_runner_path_cursor'], defensively handles bad
    values (non-int, out-of-range, missing) by clamping to 0 and
    logging amber. The returned cursor_idx is the ALREADY-normalized
    index (i.e. cursor_idx % len(rotation)) so callers can use it
    directly to compute the next cursor without re-modding.
    """
    rotation = _get_path_rotation()
    raw = None
    try:
        raw = app.cfg.get('queue_runner_path_cursor', 0)
    except Exception as e:
        _log_amber(
            app,
            f"Queue runner: cfg read for path_cursor failed "
            f"({type(e).__name__}: {e}); falling back to 0")
        raw = 0
    try:
        cursor_idx = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        _log_amber(
            app,
            f"Queue runner: queue_runner_path_cursor has non-int "
            f"value {raw!r}; falling back to 0")
        cursor_idx = 0
    # Modulo into range. Negative cursors (someone edited config.json
    # to -1) get normalized too — Python's % is sign-preserving for
    # negatives so we clamp by abs() first.
    if cursor_idx < 0:
        _log_amber(
            app,
            f"Queue runner: queue_runner_path_cursor was negative "
            f"({cursor_idx}); clamping to 0")
        cursor_idx = 0
    cursor_idx = cursor_idx % len(rotation)
    return (cursor_idx, rotation[cursor_idx])


def _compute_required_spacing_seconds(provider_dict: dict,
                                        est_tokens: int = 1500) -> float:
    """v4.14.3.8 (2026-05-14): compute the minimum inter-call
    spacing for a provider in the queue runner's burst pattern.

    Resolution order:
      1. If provider has TPM metadata AND token estimate > 0:
         spacing = tokens_per_call / (tpm / 60). Example: 3000-token
         prompt at 6000 TPM = 30 seconds per call.
      2. Else if RPM metadata: spacing = 60 / rpm. Example: 25 RPM
         = 2.4 seconds per call.
      3. Else fall back to 1.5 seconds (safe for moderate-burst
         unknown-tier providers).
      4. Apply 15% safety margin (multiply by 1.15) so clock skew
         and provider-side enforcement jitter don't trip us.

    Returns: spacing in seconds (float). Caller is responsible for
    comparing against the actual time since last call.
    """
    try:
        import tm_rate_limiter as _trl
    except Exception:
        return 1.5
    preset = (provider_dict.get('preset') or 'custom').lower()
    defaults = _trl.PRESET_DEFAULTS.get(
        preset, _trl.PRESET_DEFAULTS['custom'])
    tpm = (provider_dict.get('rate_limit_tpm')
           or defaults.get('tpm'))
    rpm = (provider_dict.get('rate_limit_rpm')
           or defaults.get('rpm'))
    try:
        tpm = int(tpm) if tpm else None
    except (TypeError, ValueError):
        tpm = None
    try:
        rpm = int(rpm) if rpm else None
    except (TypeError, ValueError):
        rpm = None

    if tpm is not None and tpm > 0 and est_tokens > 0:
        spacing = est_tokens / (tpm / 60.0)
    elif rpm is not None and rpm > 0:
        spacing = 60.0 / rpm
    else:
        spacing = 1.5
    return spacing * 1.15  # 15% safety margin


# ─── Public lifecycle ─────────────────────────────────────────────────

def start_queue_runner(app) -> Optional[threading.Thread]:
    """Spawn the queue runner thread. Returns the Thread object, or
    None if the runner is disabled by cfg. Idempotent if called twice
    — the second call returns the existing thread."""
    try:
        if not app.cfg.get('queue_runner_enabled', True):
            return None
    except Exception:
        pass
    existing = getattr(app, '_queue_runner_thread', None)
    if existing is not None and existing.is_alive():
        return existing
    if getattr(app, '_queue_runner_stop', None) is None:
        app._queue_runner_stop = threading.Event()
    t = threading.Thread(
        target=_runner_loop, args=(app,),
        daemon=True, name='queue-runner')
    app._queue_runner_thread = t
    t.start()
    return t


def stop_queue_runner(app) -> None:
    """Signal the runner to exit, then join briefly. Best-effort —
    doesn't wait forever for a stuck thread."""
    ev = getattr(app, '_queue_runner_stop', None)
    if ev is not None:
        try:
            ev.set()
        except Exception:
            pass
    t = getattr(app, '_queue_runner_thread', None)
    if t is not None and t.is_alive():
        try:
            t.join(timeout=5)
        except Exception:
            pass


# ─── Main loop ────────────────────────────────────────────────────────

def _runner_loop(app) -> None:
    """The daemon thread's body. Polls cadence triggers (legacy mode)
    or event triggers (v4.14.4.0+), runs passes, handles pause states,
    swallows top-level exceptions to keep the loop alive.

    v4.14.4.0: branches on cfg['event_driven_refresh']:
      - False (default): v4.14.3.x cadence behavior unchanged.
      - True: trigger sweep via tm_event_triggers.evaluate_all_triggers;
        fire list capped + prioritized; dispatched via
        run_one_pass_for_triggers.

    Cfg flag flips manually (Mike's choice) OR auto-flips after the
    14-day soak window stamped in cfg['event_driven_refresh_installed_at'].
    """
    stop_event = getattr(app, '_queue_runner_stop', None)
    if stop_event is None:
        return

    # v4.14.6.35-fix-startup-stampede: 30s startup grace before the
    # first event-driven sweep. The sweep is moderate-cost (it walks
    # candidate shortlist + may dispatch an AI call). Spreading the
    # grace across daemons (layer2 60s, queue-runner 30s, fundfile
    # 20s, news 15s, recommend-cache 10s) prevents the v4.14.6.34
    # stampede where all 5 hit at t=0. Stop-event interruptible.
    if stop_event.wait(30.0):
        return

    # v4.14.4.0: dedicated wake event for user-initiated refresh.
    # stop_event semantically means "exit"; don't overload it. The
    # wake event interrupts the loop's sleep early so the next
    # iteration immediately processes the user's request.
    wake_event = getattr(app, '_trigger_wake_event', None)
    if wake_event is None:
        wake_event = threading.Event()
        try:
            app._trigger_wake_event = wake_event
        except Exception:
            pass

    # Wait for disclaimer acceptance before starting — same pattern
    # as _launch_auto_refresh. The continuous queue shouldn't run for
    # users who haven't acknowledged the disclaimer.
    while not _has_accepted_disclaimer(app):
        if stop_event.wait(15):
            return

    _log_muted(app,
               "Queue runner: started (continuous Recommend queue).")

    # v4.14.4.0: stamp installed_at on first run, then check auto-flip.
    # Done OUTSIDE the loop so we don't repeatedly stamp / log on
    # subsequent iterations.
    try:
        _v4_14_4_0_handle_installed_at_and_auto_flip(app)
    except Exception as e:
        _log_amber(
            app,
            f"v4.14.4.0 installed_at/auto-flip handler failed: "
            f"{type(e).__name__}: {e}")

    last_auto_refresh_seen = ''
    consecutive_empty_sweeps = 0  # v4.14.4.0: tiered-backoff state
    # Initialize outcome so the 30-min heartbeat has something to say
    # before the first pass lands.
    _set_run_outcome(app, 'awaiting first pass')
    while not stop_event.is_set():
        try:
            # v4.14.5.14-keep-awake: reset the per-cycle work marker.
            # The two work functions below set it True at their dispatch
            # points; _manage_keepawake reads it at the end of the cycle
            # to decide whether to release the system-awake hold.
            app._keepawake_worked_this_cycle = False

            # Quiet-stretch heartbeat: fires once if no log has been
            # produced in 30+ min. Self-suppressing — _safe_log
            # refreshes the timer.
            _heartbeat_if_quiet(app)

            # v4.14.4.0: branch on cfg['event_driven_refresh'].
            event_driven = False
            try:
                event_driven = bool(
                    app.cfg.get('event_driven_refresh', False))
            except Exception:
                event_driven = False

            sleep_seconds = 60  # default

            if event_driven:
                # NEW path — trigger sweep.
                if not _is_paused(app):
                    fired_count = _run_event_driven_sweep(app)
                    if fired_count > 0:
                        consecutive_empty_sweeps = 0
                    else:
                        consecutive_empty_sweeps += 1
                    # v4.14.5.14a Layer 1: after the trigger sweep,
                    # top up any path whose recommend_cache is below
                    # target with a single-AI throughput pass. This is
                    # what fills Recommend on quiet (e.g. Sunday)
                    # stretches when no triggers fire. Flag-gated +
                    # runaway-capped inside _run_fill_mode.
                    _run_fill_mode(app)
                # Compute sleep from tiered backoff.
                try:
                    import tm_event_triggers as _tet
                    sleep_seconds = _tet.compute_backoff_sleep(
                        consecutive_empty_sweeps)
                except Exception:
                    sleep_seconds = 60
                # v4.14.5.46-coldstart-pick-cadence: the event-sweep idle
                # backoff (which grows to 5–10 min when no news fires) must NOT
                # starve pick-generation. While the fill-mode still has real
                # work (a path below target, provider available), poll at the
                # fast cadence so picks keep accumulating during cold start /
                # catch-up. When there's no fill-mode work (idle), the backoff
                # above governs UNCHANGED — its genuine-idle purpose is intact.
                if _fill_mode_has_pending_work(app):
                    sleep_seconds = min(sleep_seconds, _FILL_FAST_POLL_SECONDS)
            else:
                # LEGACY path — v4.14.3.x cadence behavior unchanged.
                run_now, last_auto_refresh_seen = _should_run_pass(
                    app, last_auto_refresh_seen)
                if run_now and not _is_paused(app):
                    try:
                        run_one_pass(app)
                    except Exception as e:
                        _log_amber(
                            app,
                            f"Queue runner pass error: {e}")
                        try:
                            import tm_teacher_intercept as _tm_ic
                            _tm_ic.emit_system_event(
                                'top_ai_runner_failed', app=app)
                        except Exception:
                            pass
                # Legacy mode always sleeps 60s — original v4.14.3.x
                # cadence behavior.
                sleep_seconds = 60

            # v4.14.5.14-keep-awake: release the system-awake hold once
            # the runner has been idle for two consecutive cycles.
            # Acquisition happens inside the work functions (so the hold
            # is in place BEFORE a multi-minute pass blocks); this is the
            # idle-side release. No-op on non-Windows.
            _manage_keepawake(app)
        except Exception as e:
            # Cadence/pause logic itself errored. Log and keep going.
            _log_amber(app, f"Queue runner cadence error: {e}")
            sleep_seconds = 60

        # v4.14.4.0: sleep is interruptible by stop_event OR
        # wake_event. wake_event fires when user actions push
        # triggers directly (e.g., Watchlist.add in v4.14.4.3).
        # Clear wake_event AFTER waking so we don't lose a wake
        # that fires during processing.
        if stop_event.wait(sleep_seconds):
            return
        if wake_event.is_set():
            wake_event.clear()


def _manage_keepawake(app) -> None:
    """v4.14.5.14-keep-awake: idle-side release of the Windows system-awake
    hold. Called once per runner cycle.

    Acquisition (request_keep_awake) lives at the two dispatch points inside
    _run_fill_mode and _run_event_driven_sweep, so the hold is in place
    BEFORE a pass blocks for what can be minutes on a slow provider. This
    function only handles the release: when a cycle does no work it counts as
    idle, and after two consecutive idle cycles the hold is released so the
    machine returns to normal sleep behaviour. The two-cycle hysteresis
    prevents thrashing between back-to-back bursts. Never raises; no-op on
    non-Windows (release_keep_awake itself is a no-op there)."""
    try:
        from tm_keepawake import release_keep_awake
    except Exception:
        return
    try:
        worked = bool(getattr(app, '_keepawake_worked_this_cycle', False))
        if worked:
            app._keepawake_idle_cycles = 0
            return
        n = int(getattr(app, '_keepawake_idle_cycles', 0) or 0) + 1
        app._keepawake_idle_cycles = n
        if n >= 2:
            release_keep_awake('queue runner idle')
    except Exception:
        # Keep-awake management must never disturb the runner loop.
        pass


def _v4_14_4_0_handle_installed_at_and_auto_flip(app) -> None:
    """Stamp installed_at on first v4.14.4.0+ launch + check auto-flip.

    Done once per session at start of _runner_loop, after disclaimer
    acceptance. Safe to call repeatedly — idempotent on stamped state.
    """
    try:
        import tm_event_triggers as _tet
    except Exception:
        return
    try:
        import tired_market as _tm
    except Exception:
        _tm = None

    cfg = getattr(app, 'cfg', None) or {}

    # Stamp installed_at if missing.
    if cfg.get('event_driven_refresh_installed_at') is None:
        cfg['event_driven_refresh_installed_at'] = time.time()
        try:
            if _tm is not None and hasattr(_tm, 'save_config'):
                _tm.save_config(cfg)
        except Exception as e:
            _log_amber(
                app,
                f"v4.14.4.0: failed to persist installed_at: "
                f"{type(e).__name__}: {e}")
        _log_muted(
            app,
            "Event-driven refresh v4.14.4.0: tracking soak window "
            "from now (14-day auto-flip).")

    # Check auto-flip.
    if _tet.should_auto_flip(cfg):
        cfg['event_driven_refresh'] = True
        try:
            if _tm is not None and hasattr(_tm, 'save_config'):
                _tm.save_config(cfg)
        except Exception as e:
            _log_amber(
                app,
                f"v4.14.4.0: auto-flip save failed: "
                f"{type(e).__name__}: {e}")
        _log_amber(
            app,
            "Event-driven refresh auto-enabled after 14-day soak "
            "window. Quiet stretches are now expected; the runner "
            "fires on news / price / earnings / staleness triggers "
            "rather than a 15-min cadence.")


def _format_fire_inline(fire: dict) -> str:
    """v4.14.4.1: short ticker-level summary for a single fire,
    used in the activity log when a kind has <=5 fires. Format
    per kind:
      price_drift  -> 'AAPL +5.2%'
      target_stop  -> 'AAPL target' or 'AAPL stop'
      staleness    -> 'AAPL'
      (other kinds default to ticker only)"""
    ticker = fire.get('ticker', '?')
    kind = fire.get('kind', 'staleness')
    ctx = fire.get('context') or {}
    if kind == 'price_drift':
        drift = ctx.get('drift_pct')
        try:
            drift = float(drift) if drift is not None else 0.0
        except (TypeError, ValueError):
            drift = 0.0
        sign = '+' if drift >= 0 else ''
        return f"{ticker} {sign}{drift:.1f}%"
    if kind == 'target_stop':
        crossed = ctx.get('crossed') or '?'
        return f"{ticker} {crossed}"
    if kind == 'news':
        # v4.14.4.2: ticker + new-article count. Top headline lives
        # in signal_context for prompt framing, not in the log line
        # (length varies, would make the line unscannable).
        count = ctx.get('new_article_count')
        try:
            count = int(count) if count is not None else 0
        except (TypeError, ValueError):
            count = 0
        return f"{ticker} +{count}"
    if kind == 'earnings':
        # v4.14.4.3: subkind + days delta. 'upcoming 3d' / 'recent 1d'.
        # EPS estimate/actual stay in signal_context for prompt
        # framing, not in the activity log line.
        subkind = ctx.get('subkind') or '?'
        days = ctx.get('days_delta')
        try:
            days = int(days) if days is not None else 0
        except (TypeError, ValueError):
            days = 0
        return f"{ticker} {subkind} {days}d"
    if kind == 'user':
        # v4.14.4.3: action tag — 'watchlist_add' / 'position_open'.
        action = ctx.get('action') or '?'
        return f"{ticker} {action}"
    return ticker


def _format_sweep_summary(all_fires: list,
                            capped_count: int,
                            drop_count: int) -> str:
    """v4.14.4.1: compose the per-sweep activity log line. Counts
    always; inline ticker+context details for any kind with <=5
    fires. Kinds rendered in TRIGGER_PRIORITY order so the line
    reads top-down by importance."""
    try:
        import tm_event_triggers as _tet
        priority = list(_tet.TRIGGER_PRIORITY)
    except Exception:
        priority = ['user', 'target_stop', 'earnings', 'news',
                     'price_drift', 'staleness']

    fires_by_kind: dict = {}
    for f in all_fires:
        fires_by_kind.setdefault(f.get('kind', 'staleness'),
                                   []).append(f)

    parts = []
    # Kinds in priority order.
    for kind in priority:
        kfires = fires_by_kind.get(kind)
        if not kfires:
            continue
        if len(kfires) <= 5:
            details = ', '.join(
                _format_fire_inline(f) for f in kfires)
            parts.append(f"{len(kfires)} {kind} ({details})")
        else:
            parts.append(f"{len(kfires)} {kind}")
    # Any unknown kinds (defensive).
    for kind, kfires in fires_by_kind.items():
        if kind in priority:
            continue
        parts.append(f"{len(kfires)} {kind}")

    summary = '; '.join(parts) if parts else 'no fires'
    cap_str = (f" ({drop_count} capped)"
                if drop_count > 0 else "")
    return (f"[event-driven] sweep: {summary}; processing top "
            f"{capped_count}{cap_str}.")


def _owned_exclusion_set(app) -> set:
    """v4.14.5.22-owned-ticker-scan-partition: the set of currently-OWNED
    tickers to exclude from the queue runner's event-driven (buy-side) scan.

    Owned positions are the PORTFOLIO surface's job — the cloud on-event path
    (Path 1, _run_cloud_on_event_scan) re-analyses them with the sell-oriented
    `owned_position` consensus. The queue runner (Path 2) is for Recommend BUY
    candidates; an owned ticker scanned here burns a buy-side consensus whose
    verdict recommend_cache hides anyway (owned tickers aren't shown in
    Recommend). The two paths share no recency gate, so without this an owned
    ticker in a path pool would double-analyse on the same event.

    Definition MATCHES recommend_cache's owned-exclusion EXACTLY
    (tired_market.py:19601-19613, use_recommend_exclude_owned): ACTIVE holdings
    = status != 'written_off' (written-off = abandoned capital, IS
    re-recommendable). No second notion of 'owned'.

    Fail-open: flag off / no holdings / any error → empty set (the gate adds
    nothing, exact pre-patch behaviour), so a fault here can never wedge the
    event sweep."""
    try:
        if not bool((getattr(app, 'cfg', {}) or {}).get(
                'exclude_owned_from_queue_scan', True)):
            return set()
    except Exception:
        return set()
    try:
        state = getattr(app, '_holdings_state', None) or {}
        mgr = state.get('mgr')
        if mgr is None or not hasattr(mgr, 'holdings'):
            return set()
        out = set()
        for h in (mgr.holdings or []):
            try:
                if str(h.get('status') or 'tradable') != 'written_off':
                    tk = (h.get('ticker') or '').strip().upper()
                    if tk:
                        out.add(tk)
            except Exception:
                continue
        return out
    except Exception:
        return set()


def _exclude_owned_fires(app, fires: list) -> list:
    """Drop fires whose ticker is currently owned (see _owned_exclusion_set).
    Logs each dropped ticker once (muted) so the partition is visible. Returns
    the kept list; fail-open to the input list on any error."""
    try:
        owned = _owned_exclusion_set(app)
        if not owned:
            return fires
        kept = []
        dropped = []
        for f in fires:
            tk = str(f.get('ticker') or '').strip().upper()
            if tk and tk in owned:
                dropped.append(tk)
            else:
                kept.append(f)
        for tk in sorted(set(dropped)):
            _log_routine(
                app,
                f"[event-driven] skipped {tk} — owned (handled by "
                f"Portfolio active-watching).")
        return kept
    except Exception:
        return fires


def _run_event_driven_sweep(app) -> int:
    """v4.14.4.0 event-driven sweep. Evaluates all triggers, applies
    priority + storm cap, records fires to trigger_fire_log, and
    dispatches each fire through the v4.14.3.11 router-rotation
    analysis path.

    Returns the count of fires processed this sweep (used by the
    backoff logic in _runner_loop). Returns 0 on any error so the
    caller backs off appropriately.
    """
    try:
        import tm_event_triggers as _tet
    except Exception as e:
        _log_amber(
            app,
            f"event-driven sweep: tm_event_triggers import failed: "
            f"{type(e).__name__}: {e}")
        return 0

    try:
        all_fires = _tet.evaluate_all_triggers(app)
    except Exception as e:
        _log_amber(
            app,
            f"event-driven sweep: evaluate_all_triggers raised: "
            f"{type(e).__name__}: {e}")
        return 0

    if not all_fires:
        # Empty sweep — no log line (the activity log would fill with
        # noise during quiet stretches). Heartbeat path covers the
        # "still alive" signal.
        return 0

    # v4.14.5.22-owned-ticker-scan-partition: drop owned tickers BEFORE
    # capping + recording, so an owned position is never dispatched for a
    # queue-runner (buy-side) consensus and never pollutes trigger_fire_log.
    # Owned positions are handled solely by the Portfolio cloud on-event path
    # (Path 1). Scoped to THIS event sweep; Path 1 is untouched. Fail-open +
    # flag-gated (exclude_owned_from_queue_scan, default True).
    all_fires = _exclude_owned_fires(app, all_fires)
    if not all_fires:
        return 0

    capped_fires = _tet.prioritize_and_cap_fires(all_fires)
    drop_count = max(0, len(all_fires) - len(capped_fires))

    # v4.14.4.1: hybrid log shape. Counts always; inline ticker
    # details per kind ONLY when ≤5 fires of that kind. Keeps the
    # activity log scannable during storms while preserving the
    # ticker-level detail during quiet sweeps.
    _log_muted(app, _format_sweep_summary(
        all_fires, len(capped_fires), drop_count))

    # v4.14.5.14-loop-prevention: quota-gate BACKGROUND fires. When
    # every scan-eligible provider is exhausted, dispatching the
    # target_stop / price_drift / news / earnings / staleness fires
    # just spins into PROVIDER_UNAVAILABLE. Defer them to the next
    # sweep — and crucially DON'T record them to trigger_fire_log, so
    # their per-kind dedup doesn't suppress the re-fire once providers
    # recover (the trigger will simply re-evaluate next sweep). USER-
    # initiated fires (refresh-triggers / explicit action) ALWAYS
    # attempt: the user is waiting and a failure surfaces immediately,
    # so they're never silently swallowed. Mirrors fill mode's
    # _scan_availability circuit breaker. Sub-flag
    # cfg['use_event_sweep_quota_gate'] (default True) rolls it back.
    try:
        _gate_on = bool((getattr(app, 'cfg', {}) or {}).get(
            'use_event_sweep_quota_gate', True))
    except Exception:
        _gate_on = True
    if _gate_on:
        _bg = [f for f in capped_fires if f.get('kind') != 'user']
        if _bg:
            try:
                _can, _why = _scan_availability(app)
            except Exception:
                _can, _why = True, ''
            if not _can:
                _user = [f for f in capped_fires
                         if f.get('kind') == 'user']
                _log_muted(
                    app,
                    f"[event-sweep] pause: "
                    f"{_why or 'all scan-eligible providers exhausted'}; "
                    f"{len(_bg)} background fire(s) deferred to next sweep"
                    + (f", {len(_user)} user fire(s) still dispatched"
                       if _user else ""))
                capped_fires = _user

    # Record fires before dispatch — if dispatch crashes mid-way,
    # we still have the audit trail.
    now_ts = int(time.time())
    for fire in capped_fires:
        try:
            _tet.record_trigger_fire(
                app,
                fire.get('ticker', ''),
                fire.get('path', ''),
                fire.get('kind', 'unknown'),
                fire.get('context'),
                now_ts=now_ts)
        except Exception as e:
            # record_trigger_fire is already best-effort with amber
            # logging; this catch is belt-and-suspenders against
            # an import-time-only failure.
            _log_amber(
                app,
                f"event-driven sweep: record_trigger_fire failed: "
                f"{type(e).__name__}: {e}")

    # Dispatch. Group fires by path so we can reuse the existing
    # run_one_pass_body's per-path RouterRun + picker flow.
    fires_by_path: dict = {}
    # v4.14.5.14-target-stop-override-recency: collect, per path, the tickers
    # whose fire this sweep was a `target_stop` (price crossed the recorded
    # target/stop). These bypass the verdict-recency gate downstream — a
    # target/stop crossing is the high-signal event the verdict was waiting
    # for, so it should re-analyse even if the verdict is recent. price_drift
    # / news / earnings / staleness are NOT collected here, so they stay
    # gated as before. The path-pool + price-band gates still apply to all.
    ts_bypass_by_path: dict = {}
    for fire in capped_fires:
        path = fire.get('path', '')
        if not path:
            continue
        fires_by_path.setdefault(path, []).append(fire.get('ticker'))
        if fire.get('kind') == 'target_stop':
            tk = fire.get('ticker')
            if tk:
                ts_bypass_by_path.setdefault(path, set()).add(
                    str(tk).upper())

    # v4.14.5.14-keep-awake: real fires are about to be analysed — a
    # dispatch can block for minutes, so hold the system awake now
    # (released centrally by _manage_keepawake once the runner idles).
    # Guarded on fires_by_path so a fully-gated sweep doesn't hold.
    if fires_by_path:
        try:
            app._keepawake_worked_this_cycle = True
            from tm_keepawake import request_keep_awake
            request_keep_awake('event sweep dispatching')
        except Exception:
            pass

    # v4.14.5.14a.6: only dispatch an event's ticker to a path if it
    # belongs to that path's pool. A SMCI news event should re-analyse
    # SMCI for aggressive/moderate, NOT slow_safe (where it'd just
    # AVOID by construction). Empty pool / flag off → no filter (exact
    # pre-a.6 behaviour).
    #
    # v4.14.5.82-discovery-unlock: per-kind carve-out for the new
    # `fresh_universe_mover` discovery kind ONLY. The trigger already
    # assigned each discovery fire to lottery or aggressive (by price
    # < $5 vs ≥ $5), and those paths' DYNAMIC pools are the
    # price-filtered universe — by construction the right home for a
    # never-analyzed mover. Without bypassing the static-pool check
    # the pool sweeper drops 100% of discovery fires (every mover is
    # not-yet-in-any-pool — that's the point of "discovery"). So we
    # split the per-path ticker list into "discovery" + "other" and
    # apply the pool gate only to "other," routing discovery fires
    # through untouched. The rest of the gate's behavior is byte-
    # identical for every other trigger kind. We need access to the
    # per-fire kind, so we re-walk capped_fires for the kind tags
    # rather than the path-collapsed map.
    use_pools = bool((getattr(app, 'cfg', {}) or {}).get(
        'use_path_candidate_pools', True))
    # Build a (path -> set of discovery tickers) index from the
    # ordered fires list. capped_fires preserves the prioritized
    # ordering; we just need the kind-tag lookup.
    #
    # v4.14.5.83-leading-signals: extended the discovery-kind set to
    # include the two new leading-signal kinds (`insider_buy` and
    # `volume_accumulation`). All three discovery kinds route to
    # lottery/aggressive (their pools are dynamic) so the carve-out
    # bypass is safe; slow_safe/moderate are untouched. Other trigger
    # kinds (user, target_stop, earnings, news, price_drift,
    # staleness) still go through the standard pool-membership gate.
    _DISCOVERY_KINDS = frozenset({
        'fresh_universe_mover',     # v.82 — trailing (price already moved)
        'insider_buy',              # v.83 — leading (Form-4 buy)
        'volume_accumulation',      # v.83 — leading (vol w/o price)
    })
    _disc_by_path: dict = {}
    for _f in capped_fires:
        if _f.get('kind') in _DISCOVERY_KINDS:
            _p = _f.get('path')
            _tk = (_f.get('ticker') or '').upper()
            if _p and _tk:
                _disc_by_path.setdefault(_p, set()).add(_tk)
    for path, tickers in fires_by_path.items():
        if not tickers:
            continue
        if use_pools:
            try:
                import tm_path_candidate_pools as _tpcp
                pl = _tpcp.get_path_universe(app, path) or []
                if pl:
                    poolset = {str(t).upper() for t in pl}
                    _disc_set = _disc_by_path.get(path, set())
                    kept_pool = [
                        t for t in tickers
                        if str(t).upper() in poolset]
                    # Discovery tickers BYPASS the pool gate but are
                    # only ever produced for lottery/aggressive (their
                    # pools are dynamic anyway; this carve-out covers
                    # the cold-cache case where the pool computation
                    # hasn't observed them yet).
                    kept_disc = [
                        t for t in tickers
                        if str(t).upper() in _disc_set
                        and str(t).upper() not in poolset]
                    kept = kept_pool + kept_disc
                    n_dropped = len(tickers) - len(kept)
                    if n_dropped > 0:
                        _log_muted(
                            app,
                            f"[path-pools] event-driven ({path}): "
                            f"{n_dropped} of "
                            f"{len(tickers)} ticker(s) not in pool, "
                            f"skipped")
                    if kept_disc:
                        _log_muted(
                            app,
                            f"[path-pools] event-driven ({path}): "
                            f"{len(kept_disc)} discovery mover(s) "
                            f"routed through "
                            f"(discovery-kind carve-out: "
                            f"fresh_universe_mover / insider_buy / "
                            f"volume_accumulation).")
                    tickers = kept
            except Exception:
                pass
        if not tickers:
            continue
        # v4.14.6.24-fill-terminal: a real event firing on this path is
        # the strongest possible "something changed" signal — wake any
        # structurally-short flag before dispatch so the next fill cycle
        # is ready to resume. Cheap; idempotent if not flagged.
        try:
            _clear_structurally_short(
                app, path, reason='event-driven dispatch')
        except Exception:
            pass
        try:
            run_one_pass_for_triggers(
                app, path, tickers,
                recency_bypass=ts_bypass_by_path.get(path))
        except Exception as e:
            _log_amber(
                app,
                f"event-driven sweep: dispatch for path={path} "
                f"raised: {type(e).__name__}: {e}")

    return len(capped_fires)


def run_one_pass_for_triggers(app, path: str,
                                candidates: list,
                                dispatch_label: str = 'event-driven',
                                recency_bypass=None
                                ) -> None:
    """v4.14.4.0: dispatch a fire list for ONE path through the same
    analysis loop run_one_pass uses, bypassing the universe cursor
    + path rotation (those are cadence-mode concerns). Reuses:
      - tm_top_ai_picker (health gate + provider preference)
      - tm_ai_router begin_scan_run / end_scan_run (v4.14.3.11
        multi-provider rotation)
      - _analyze_candidate (v4.14.3.11 distribution path)
      - _record_analysis_outcome (v4.14.3.9 cursor table writes)
      - _insert_queue_row (BUY-only recommend_queue insertion)
      - provider_calls tally + _emit_summary_log (with path-in-dedup
        from v4.14.3.10)

    No cursor advance (event-driven mode doesn't rotate). The
    v4.14.3.9 cursor table still receives writes via
    _record_analysis_outcome, which serves as the per-(ticker, path)
    staleness anchor that drives the NEXT sweep's staleness trigger.

    v4.14.5.14a.9 (Patch 3): `dispatch_label` tags every log line this
    function emits so fill-mode dispatches read `[fill-mode]` and
    event-driven dispatches read `[event-driven]` in the activity log
    (fill mode reuses this function, so without the tag its work was
    mislabelled as event-driven — the reason the weekend burn was
    invisible). Behaviour is identical either way; label only.

    v4.14.5.14-target-stop-override-recency: `recency_bypass` is an optional
    set of upper-cased tickers (the target_stop fires for this path this
    sweep) that should skip the verdict-recency gate in _eligible_paths_for.
    Forwarded to _unified_dispatch_ticker. None (the default, and what fill
    mode / cadence pass) = gate applies normally for every ticker.
    """
    _src = dispatch_label or 'event-driven'
    _src_from = ('from fill cursor' if _src == 'fill-mode'
                 else 'from triggers')
    pass_started = datetime.now().isoformat(timespec='seconds')
    try:
        app.cfg['queue_runner_last_pass'] = pass_started
    except Exception as e:
        _log_amber(
            app,
            f"{_src} dispatch: failed to stamp last_pass: "
            f"{type(e).__name__}: {e}")

    # v4.14.5.14a.14: price-band eligibility gate. Covers BOTH fill
    # mode (dispatch_label='fill-mode' — already pool-filtered, so a
    # near no-op here) and the event-driven sweep (the real leak: a
    # news/earnings/price/user trigger can route a ticker to a path
    # whose price band excludes it). Logged count below reflects the
    # post-gate list.
    candidates = _eligibility_price_band_filter(
        app, path, candidates, _src)

    _log_muted(
        app,
        f"[{_src}] dispatch ({path}): "
        f"{len(candidates)} ticker"
        f"{'' if len(candidates) == 1 else 's'} {_src_from}.")
    _mark_runner_logged(app)

    # Ensure HoldingsWindow built (v4.14.3.6 pattern).
    try:
        _ensure_fn = getattr(app, '_ensure_holdings_window', None)
        if callable(_ensure_fn):
            built = _ensure_fn()
            if not built:
                _log_amber(
                    app,
                    f"{_src} dispatch ({path}): HoldingsWindow "
                    f"build failed; skipping pass.")
                return
    except Exception as e:
        _log_amber(
            app,
            f"{_src} dispatch ({path}): HoldingsWindow ensure "
            f"raised: {type(e).__name__}: {e}")

    # Pick top AI (health gate). Same handling as run_one_pass —
    # pre-pick failures branch through _emit/_set_run_outcome but
    # don't write outcome rows (analogous to v4.14.3.10's design:
    # picker state isn't a ticker-staleness signal).
    try:
        import tm_top_ai_picker
        override = app.cfg.get('top_ai_override') or None
        chosen = tm_top_ai_picker.pick_top_ai(app, override=override)
    except Exception as e:
        _log_amber(
            app,
            f"{_src} dispatch ({path}): top-AI pick failed: "
            f"{type(e).__name__}: {e}")
        chosen = {'success': False, 'reason': 'all_exhausted'}

    if not chosen.get('success'):
        reason = chosen.get('reason') or 'all_exhausted'
        _log_muted(
            app,
            f"[{_src}] dispatch ({path}): picker returned "
            f"{reason}; skipping fires for this sweep. They will "
            f"re-fire on the next sweep.")
        return

    # Open RouterRun for the v4.14.3.11 distribution path.
    try:
        import tm_ai_router as _router_for_scan
        _router_for_scan.begin_scan_run()
    except Exception as e:
        _log_amber(
            app,
            f"{_src} dispatch ({path}): begin_scan_run "
            f"failed ({type(e).__name__}: {e}); distribution mode "
            f"inactive for this sweep.")

    _log_muted(
        app,
        f"[{_src}] dispatch ({path}): router rotation across "
        f"eligible providers.")

    silent_failures = 0
    skipped = 0  # v4.14.5.14a.3: PROVIDER_UNAVAILABLE (no provider)
    drop_reasons: dict = {}
    provider_calls: dict = {}
    inserted = 0
    _ustats: dict = {}  # v4.14.5.14c-p2 unified-dispatch tallies

    try:
        # v4.14.5.62-concurrent-scan: optional worker-pool dispatch. When
        # it runs, it returns merged tallies and the sequential loop below
        # is bypassed (its iterable becomes empty — body untouched). Flag
        # OFF / no eligible providers → None, and the UNCHANGED sequential
        # loop runs as the proven fallback.
        _concurrent_merged = None
        if _concurrent_enabled(app):
            _concurrent_merged = _run_concurrent_dispatch(
                app, chosen, candidates, path, pass_started,
                _src, recency_bypass)
        if _concurrent_merged is not None:
            inserted += int(_concurrent_merged.get('inserted', 0) or 0)
            silent_failures += int(
                _concurrent_merged.get('silent', 0) or 0)
            skipped += int(_concurrent_merged.get('skipped', 0) or 0)
            for _ck, _cv in (
                    _concurrent_merged.get('drop_reasons') or {}).items():
                drop_reasons[_ck] = drop_reasons.get(_ck, 0) + _cv
            for _ck, _cv in (
                    _concurrent_merged.get('provider_calls') or {}).items():
                provider_calls[_ck] = provider_calls.get(_ck, 0) + _cv
            for _ck, _cv in (
                    _concurrent_merged.get('ustats') or {}).items():
                if _ck == 'provider_calls':
                    _pcd = _ustats.setdefault('provider_calls', {})
                    for _pk, _pv in (_cv or {}).items():
                        _pcd[_pk] = _pcd.get(_pk, 0) + _pv
                else:
                    _ustats[_ck] = _ustats.get(_ck, 0) + _cv
        for ticker in (candidates if _concurrent_merged is None else ()):
            if _stop_set(app):
                return
            # v4.14.5.62-lookup-backoff: a user lookup started mid-pass — stop
            # dispatching NEW fill tickers so it gets the providers (we don't
            # kill the in-flight call; we just start no more). FILL passes only;
            # event-driven dispatch isn't throttled. `break` ends the pass
            # cleanly (summary + end_scan_run still run via the finally).
            if _src == 'fill-mode' and _lookup_backoff_active(app):
                break
            # v4.14.5.14c-p2: one-ticker-all-paths. INERT unless the
            # flag is on; on fault/local/flag-off → None = fall
            # through to today's per-(ticker,path) path unchanged.
            _ud = _unified_dispatch_ticker(
                app, chosen, ticker, path, pass_started,
                _ustats, _src, recency_bypass=recency_bypass)
            if _ud in ('done', 'skip'):
                continue
            try:
                pred = _analyze_candidate(
                    app, chosen, ticker, path,
                    drop_reasons=drop_reasons)
            except Exception as e:
                _log_muted(
                    app,
                    f"{_src} dispatch: {ticker} analysis "
                    f"failed: {type(e).__name__}")
                _record_analysis_outcome(app, ticker, path, 'failed')
                continue
            if pred is PROVIDER_UNAVAILABLE:
                # v4.14.5.14a.2: no provider could attempt this ticker.
                # Do NOT advance the cursor, do NOT count it — it stays
                # "needs analysis" for when a provider recovers.
                # v4.14.5.14a.3: count as skipped (NOT analyzed) so the
                # pass-stats line is honest.
                skipped += 1
                _log_muted(
                    app,
                    f"[fill-mode] skipped {ticker} — no eligible "
                    f"providers (will retry when available)")
                continue
            if pred is None:
                silent_failures += 1
                _record_analysis_outcome(app, ticker, path, 'failed')
                continue
            direction = (pred.get('direction') or '').upper()
            outcome = direction if direction else 'NO_CALL'
            _record_analysis_outcome(app, ticker, path, outcome)
            _provider_label = (
                pred.get('model')
                or chosen.get('display_name')
                or chosen.get('id')
                or '?')
            provider_calls[_provider_label] = (
                provider_calls.get(_provider_label, 0) + 1)
            if direction != 'BUY':
                continue
            try:
                _insert_queue_row(app, ticker, path, pred, chosen,
                                  pass_started)
                inserted += 1
            except Exception as e:
                _log_muted(
                    app,
                    f"{_src} dispatch: insert failed for "
                    f"{ticker}: {e}")
    finally:
        try:
            import tm_ai_router as _router_end
            _router_end.end_scan_run()
        except Exception as e:
            _log_amber(
                app,
                f"{_src} dispatch ({path}): end_scan_run "
                f"failed ({type(e).__name__}: {e}); router state "
                f"may leak to next sweep.")

    if drop_reasons:
        items = sorted(
            drop_reasons.items(),
            key=lambda kv: (-kv[1], kv[0]))
        bits = [f"{count}x {reason}" for (reason, count) in items]
        _log_amber(
            app,
            f"{_src} dispatch ({path}): "
            f"{sum(drop_reasons.values())}/{len(candidates)} dropped "
            f"before AI call — " + ", ".join(bits))
    if silent_failures > 0:
        _log_amber(
            app,
            f"{_src} dispatch ({path}): provider returned no "
            f"prediction for {silent_failures}/{len(candidates)} "
            f"tickers. (See preceding log lines for per-candidate "
            f"reason.)")

    # v4.14.5.14c-p2: fold unified-dispatch tallies into the pass
    # summary so it stays honest when the flag is on (all zeros when
    # off → summary identical to pre-patch).
    inserted += int(_ustats.get('inserted', 0) or 0)
    skipped += int(_ustats.get('skipped', 0) or 0)
    silent_failures += int(_ustats.get('silent', 0) or 0)
    for _k, _v in (_ustats.get('provider_calls', {}) or {}).items():
        provider_calls[_k] = provider_calls.get(_k, 0) + _v

    # v4.14.5.25: fold the unified-dispatch gated-skip count into the summary's
    # gated total. Without this, unified-path tickers gated at "0 eligible
    # paths" (0 AI calls) were counted as analyzed → the misleading
    # "N analyzed via Groq" Mike saw. The already-shipped honest wording
    # (v4.14.5.14-queue-runner-log-honesty) then prints "all gated; 0 AI calls".
    _emit_summary_log(
        app, chosen, candidate_count=len(candidates),
        inserted=inserted, outcome='success', path=path,
        provider_calls=provider_calls, skipped=skipped,
        gated=sum(drop_reasons.values()) + int(_ustats.get('gated', 0) or 0))
    _set_run_outcome(
        app,
        f"{_src} ({path}: {inserted} new pick"
        f"{'' if inserted == 1 else 's'})")

    # Housekeeping: graduations + invalidations.
    try:
        _trim_active_to_cap(app)
    except Exception as e:
        _log_amber(app, f"{_src} trim error: {e}")
    try:
        _run_housekeeping(app)
    except Exception as e:
        _log_amber(app, f"{_src} housekeeping error: {e}")


# ─── v4.14.5.14a: Layer 1 fill mode ───────────────────────────────────
#
# When a path's recommend_cache is below target, dispatch ONE single-AI
# throughput pass for the most-starved path, reusing the universe-cursor
# candidate selector and the run_one_pass_for_triggers analysis loop. No
# new analysis engine — pure orchestration over existing pieces. Runaway-
# capped so a low-BUY-rate path can't spin forever.

# v4.14.5.14a.2: sentinel returned up the analysis chain when a ticker
# could NOT be attempted because every scan-eligible provider was
# exhausted (cooldown / daily cap / blocked). Distinct from None (a
# real per-ticker failure that SHOULD advance the cursor as 'failed').
# A PROVIDER_UNAVAILABLE result must NOT advance the cursor and must
# NOT write a prediction — the ticker stays "needs analysis" for when
# providers recover.
PROVIDER_UNAVAILABLE = object()


# v4.14.6.0-price-band-tiers: four price-band tiers, each filled to the
# same 10/10 target. Legacy time-path keys are aliased so any path-keyed
# config still naming them resolves to the correct band entry.
_FILL_DEFAULT_TARGETS = {
    'lottery':       {'displayed_target': 10, 'bench_floor': 10,
                      'fill_enabled': True},
    'band_5_10':     {'displayed_target': 10, 'bench_floor': 10,
                      'fill_enabled': True},
    'band_10_50':    {'displayed_target': 10, 'bench_floor': 10,
                      'fill_enabled': True},
    'band_50_up':    {'displayed_target': 10, 'bench_floor': 10,
                      'fill_enabled': True},
    # Legacy aliases — kept enabled so any in-flight code still keyed
    # by the old names continues to behave sensibly.
    'aggressive':    {'displayed_target': 10, 'bench_floor': 10,
                      'fill_enabled': True},
    'moderate':      {'displayed_target': 10, 'bench_floor': 10,
                      'fill_enabled': True},
    'slow_safe':     {'displayed_target': 10, 'bench_floor': 10,
                      'fill_enabled': True},
    'penny_lottery': {'displayed_target': 10, 'bench_floor': 10,
                      'fill_enabled': False},
}
_FILL_MAX_CYCLES_PER_SESSION = 50
_FILL_COOLDOWN_SECONDS = 30 * 60

# v4.14.5.91-fill-backoff (2026-06-10): zero-progress + actionable-short
# backoff parameters. Designed so a hard-to-fill path (raw displayed met
# but consensus hiding most picks; or BUY-rate too low this session) caps
# at ~3 wasted cycles instead of the legacy 50-cycle session limit.
#   BASE = 5min  → 10 → 20 → 40 → cap at 60min (5 doubling steps)
# Streak resets on the first cycle that gains actionable picks.
_FILL_ZERO_PROGRESS_BASE_SECONDS = 5 * 60
_FILL_ZERO_PROGRESS_MAX_SECONDS = 60 * 60
# Tighter consecutive-cycle cap specific to the actionable-short
# trigger (raw rows full but actionable below target). Cold-start fills
# (raw rows below target) keep the legacy _FILL_MAX_CYCLES_PER_SESSION
# budget — they're a genuine empty-path case, not a hard-to-fill case.
_ACTIONABLE_SHORT_MAX_CONSECUTIVE = 3
# Fairness anti-starvation overlay: a path not serviced in this many
# fill ticks jumps to the front of the eligibility queue, ahead of a
# more-recently-serviced top-yield path. Primary yield/deficit sort
# still wins until a path is genuinely being starved.
_FILL_FAIRNESS_STARVE_TICKS = 3

# v4.14.5.92-sweep-cursor (2026-06-10): per-path "no analysable
# candidates" cooldown. When a fill cycle finds zero candidates for a
# path (recency-gated, gated tiers empty), exclude the path from the
# fairness yield for this long so it can't dominate the starvation
# ranking without doing any real work. 5 minutes covers a typical news/
# event refresh window — short enough that a real new candidate is
# picked up quickly, long enough to prevent per-tick "yielding to
# slow_safe" spam.
_FILL_NO_CANDIDATES_COOLDOWN_SECONDS = 5 * 60


def _apply_zero_progress_cooldown(app, path: str, prior_streak: int,
                                    reason: str = '') -> None:
    """v4.14.5.91-fill-backoff: increment the path's zero-progress
    streak and set its cooldown to BASE * 2**(streak-1), capped at MAX.
    Reuses the existing _fill_cooldown_until store the session-cap
    backoff writes to, so _fill_needed and _run_fill_mode's single
    cooldown check excludes the path until the cooldown expires. Emits
    one amber log line so the previously-silent spin becomes visible.
    Never raises (called from a try-guarded loop)."""
    try:
        zps = getattr(app, '_fill_zero_progress_streak', None)
        if zps is None:
            zps = {}
            app._fill_zero_progress_streak = zps
        cools = getattr(app, '_fill_cooldown_until', None)
        if cools is None:
            cools = {}
            app._fill_cooldown_until = cools
        new_streak = int(prior_streak) + 1
        zps[path] = new_streak
        secs = min(_FILL_ZERO_PROGRESS_MAX_SECONDS,
                   _FILL_ZERO_PROGRESS_BASE_SECONDS
                   * (2 ** max(0, new_streak - 1)))
        cools[path] = time.time() + secs
        _suffix = f" ({reason})" if reason else ""
        _log_amber(
            app,
            f"[fill-mode] {path}: no actionable progress "
            f"(streak {new_streak}) → backing off {int(secs)}s"
            f"{_suffix}")
    except Exception:
        pass


def _reset_zero_progress_streak(app, path: str) -> None:
    """v4.14.5.91-fill-backoff: clear the path's zero-progress + actionable-
    short streak counters AND any back-off cooldown the streak set —
    called once a fill cycle actually gains actionable picks for the
    path. The legacy session 50-cycle cooldown (which writes to the same
    dict) is also cleared here; that's intentional — the session cap is
    a stuck-path heuristic, and a real-progress cycle is the strongest
    evidence we have that the heuristic was wrong.
    Never raises."""
    try:
        zps = getattr(app, '_fill_zero_progress_streak', None) or {}
        ass = getattr(app, '_fill_actionable_short_streak', None) or {}
        cools = getattr(app, '_fill_cooldown_until', None) or {}
        if path in zps:
            zps.pop(path, None)
        if path in ass:
            ass.pop(path, None)
        if path in cools:
            cools.pop(path, None)
    except Exception:
        pass

# v4.14.5.46-coldstart-pick-cadence: un-throttle pick-generation during a cold
# start / catch-up. The runner loop's sleep normally follows the EVENT-SWEEP
# idle backoff (compute_backoff_sleep: 60s→…→600s when no news fires) — which
# on a fresh launch (no news) starves the FILL-MODE even though it has hundreds
# of candidates and real work to do. While a fill_enabled path is well below
# target we poll at the fast cadence instead (the idle backoff is preserved for
# the genuinely-idle case — see _fill_mode_has_pending_work). We also enlarge
# the per-pass candidate batch while far below target so a pass uses the
# provider's per-minute headroom (overflow is gracefully skip-retried). This
# does NOT make the AI itself faster — that's the separate multi-provider work.
_FILL_FAST_POLL_SECONDS = 60      # cap the loop sleep to this when work pending
# v4.14.6.8-scan-diagnostics-and-pacing (2026-06-11): 20 -> 10. The
# concurrent scan dispatch fires the whole batch at once across 6
# pinned workers; a 20-wide batch produces ~400 calls/min instantaneous
# peak against ~50 calls/min aggregate provider capacity, saturating
# every free-tier RPM/TPM cap and producing the "no eligible providers"
# clusters. A 10-wide batch halves the burst (still comfortably refills
# all 4 bands over a couple cycles) and lets paced completions replace
# capacity-skips. Cold-start batch (_FILL_COLDSTART_BATCH = 30) is
# unchanged — that's only used while far below target, where overrun
# is expected and the limiter catches it.
_FILL_NORMAL_BATCH = 10           # steady-state per-pass candidate batch
# v4.14.5.55: cold-start batch trimmed 50 -> 30. With Speculative now a 4th
# fill path drawing on the SAME single Groq key (TPM 6000), a 50-wide burst
# overruns the per-minute token budget further before the limiter catches up,
# and a shorter pass lets the deficit-yield rotation re-pick MORE often (fairer
# interleave, so Speculative is reached sooner without crowding the high-value
# paths). Conservative starting value — Mike's single-provider tests refine it.
_FILL_COLDSTART_BATCH = 30        # per-pass batch while far below target
_FILL_COLDSTART_DEFICIT = 5       # displayed shortfall >= this == cold/catch-up

# v4.14.5.49-coldstart-pick-yield (A2): cold-start path ordering by EXPECTED
# YIELD. On a cold start every fill_enabled path is equally empty, so the plain
# "most-starved (displayed×3)" sort leads with whichever path comes first —
# historically slow_safe, whose live BUY rate is ~2.1% — burning the single
# free provider's scarce ~30 calls/cycle on ~0 picks before it cools. Weighting
# each path's shortfall by its BUY rate leads with the HIGH-yield paths
# (aggressive ~27%, moderate ~20%) so first picks appear fast. It is SELF-
# BALANCING: as a high-yield path fills, its shortfall → 0 so the low-yield
# paths rise to the top — never permanently starved (and _FILL_MAX_CYCLES_PER_
# SESSION already rotates a stuck path into cooldown). Applied ONLY while
# cold/catch-up (some path's displayed shortfall >= _FILL_COLDSTART_DEFICIT);
# at/near target it reverts to the exact plain-shortfall sort (steady state
# unchanged). Static defaults (from the live queue_runner_analysis_log, 1550
# analyses) seed a FRESH install with no history; once a path has >=
# _COLDSTART_BUY_RATE_MIN_SAMPLES of its own analyses, its live rate is used.
# v4.14.6.0-price-band-tiers: cold-start BUY-rate weights for path
# selection. Uniform 0.10 across the four bands keeps the cold-start
# yield ordering neutral until each band has accumulated >=30 live
# analyses (then live BUY rate from queue_runner_analysis_log takes
# over per _path_buy_rates). Legacy aliases retain their historical
# values so any persisted state still keyed by old names doesn't
# silently flip behaviour.
_COLDSTART_DEFAULT_BUY_RATES = {
    'lottery':       0.10,
    'band_5_10':     0.10,
    'band_10_50':    0.10,
    'band_50_up':    0.10,
    # Legacy aliases preserved for in-flight references.
    'aggressive':    0.10,
    'moderate':      0.10,
    'slow_safe':     0.10,
    'penny_lottery': 0.10,
}
_COLDSTART_FALLBACK_BUY_RATE = 0.10   # unknown path → neutral-ish weight
_COLDSTART_BUY_RATE_MIN_SAMPLES = 30  # live rate needs at least this many rows


def _get_path_fill_targets(app) -> dict:
    try:
        cfg = (getattr(app, 'cfg', {}) or {}).get('path_fill_targets')
        if isinstance(cfg, dict) and cfg:
            return cfg
    except Exception:
        pass
    return _FILL_DEFAULT_TARGETS


def _recommend_cache_counts(app) -> dict:
    """{path: (displayed_count, bench_count)} from recommend_cache."""
    out: dict = {}
    conn = _conn(app)
    if conn is None:
        return out
    with _db_lock(app):
        try:
            for path, tier, n in conn.execute(
                    "SELECT path, tier, COUNT(*) FROM recommend_cache "
                    "GROUP BY path, tier"):
                d, b = out.get(path, (0, 0))
                if tier == 'displayed':
                    d = n
                elif tier == 'bench':
                    b = n
                out[path] = (d, b)
        except Exception:
            return {}
        return out


def _path_buy_rates(app) -> dict:
    """v4.14.5.49-coldstart-pick-yield (A2): {path: BUY-rate weight in (0,1]}
    for cold-start expected-yield ordering. Uses the path's OWN live BUY rate
    from queue_runner_analysis_log once it has >= _COLDSTART_BUY_RATE_MIN_SAMPLES
    analyses; otherwise the static default (a fresh install has no history, so
    the defaults seed the order). Never raises — on any error returns the static
    defaults so ordering still works. Cheap: one GROUP BY over a small table."""
    rates = dict(_COLDSTART_DEFAULT_BUY_RATES)
    try:
        conn = _conn(app)
        if conn is None:
            return rates
        with _db_lock(app):
            rows = conn.execute(
                "SELECT path, "
                "SUM(CASE WHEN last_outcome='BUY' THEN 1 ELSE 0 END), "
                "COUNT(*) FROM queue_runner_analysis_log "
                "GROUP BY path").fetchall()
        for path, buys, tot in rows:
            if (path and tot
                    and int(tot) >= _COLDSTART_BUY_RATE_MIN_SAMPLES):
                # clamp away from 0 so a 0%-so-far path still gets a turn
                rates[str(path)] = max(0.001, float(buys or 0) / float(tot))
    except Exception:
        pass
    return rates


# v4.14.5.X-actionable-fill (2026-06-10): TTL cache for the per-tick
# consensus-vote maps so _fill_needed can call _path_actionable_displayed
# across all paths without re-reading signals.jsonl or the validation
# table on each path. _ACTIONABLE_CACHE_TTL_SECONDS is small enough that
# fresh user-Verify clicks reflect on the next fill tick (~60s loop), but
# large enough that one _fill_needed call only does ONE read pass.
_ACTIONABLE_CACHE_TTL_SECONDS = 30
_actionable_vote_cache: dict = {'built_at': 0.0,
                                 'fresh_map': {},
                                 'val_map_by_path': {}}


def _refresh_actionable_vote_cache(app) -> None:
    """v4.14.5.X-actionable-fill: rebuild the in-memory map of stored
    consensus votes the dialog gate consults, if the cache is older than
    the TTL. Reads ONLY already-stored data — signals.jsonl (via the
    app's existing _build_fresh_buy_consensus_map) and the
    recommend_cache_validation table. NO AI dispatch, NO heavy fetch.
    Fail-OPEN: any error leaves the cache empty (treated as "no votes →
    actionable", same as the dialog's _consensus_says_buy fail-open)."""
    now = time.time()
    if (now - float(_actionable_vote_cache.get('built_at') or 0.0)
            < _ACTIONABLE_CACHE_TTL_SECONDS):
        return
    fresh_map = {}
    try:
        fn = getattr(app, '_build_fresh_buy_consensus_map', None)
        if callable(fn):
            fresh_map = fn() or {}
    except Exception:
        fresh_map = {}
    val_map_by_path: dict = {}
    conn = _conn(app)
    if conn is not None:
        try:
            with _db_lock(app):
                import json as _json_av
                for path_, tk_, vj_ in conn.execute(
                        "SELECT path, ticker, votes_json "
                        "FROM recommend_cache_validation"):
                    try:
                        votes = _json_av.loads(vj_ or '[]')
                    except Exception:
                        votes = []
                    if not votes:
                        continue
                    val_map_by_path.setdefault(
                        path_ or '', {})[str(tk_).upper()] = votes
        except Exception:
            val_map_by_path = {}
    _actionable_vote_cache['built_at'] = now
    _actionable_vote_cache['fresh_map'] = fresh_map
    _actionable_vote_cache['val_map_by_path'] = val_map_by_path


def _path_actionable_displayed(app, path: str) -> tuple:
    """v4.14.5.X-actionable-fill: count displayed picks that would survive
    the SAME consensus-AVOID drop the Portfolio Recommendations dialog
    applies (_consensus_says_buy / _classify_consensus_votes in
    tired_market.py). Reuses app._classify_consensus_votes verbatim so
    the filler and the dialog cannot diverge: when consensus has hidden
    displayed picks, the filler now sees the path as under-target and
    can dispatch a fresh pass to find replacements.

    Returns (actionable_count, hidden_count, total_displayed). A ticker
    with NO stored consensus votes in EITHER source counts as actionable
    — matches the dialog's behaviour (no votes → keep) AND is the
    anti-spin guarantee: a freshly-dispatched BUY pick counts as
    actionable until cloud models have a chance to vote on it, so newly
    refilled picks DO satisfy the count (the path doesn't dispatch every
    cycle waiting for not-yet-validated picks to get dropped).

    Reads ONLY already-stored vote data — NO AI calls, NO heavy fetch.
    The two source maps are TTL-cached in _actionable_vote_cache so one
    _fill_needed call does a single read pass across every path. Fail-
    OPEN throughout: on any error we report all displayed rows as
    actionable so a measurement fault never wedges fill mode."""
    conn = _conn(app)
    if conn is None:
        return (0, 0, 0)
    displayed = []
    try:
        with _db_lock(app):
            displayed = [str(r[0]).upper() for r in conn.execute(
                "SELECT ticker FROM recommend_cache "
                "WHERE path = ? AND tier = 'displayed' "
                "ORDER BY rank_within_tier ASC", (path,)).fetchall()]
    except Exception:
        return (0, 0, 0)
    total = len(displayed)
    if total == 0:
        return (0, 0, 0)

    classify = getattr(app, '_classify_consensus_votes', None)
    if not callable(classify):
        # Fail-OPEN: no classifier reachable → treat all as actionable
        # (exact pre-patch behaviour where raw count was used).
        return (total, 0, total)

    _refresh_actionable_vote_cache(app)
    fresh_map = _actionable_vote_cache.get('fresh_map') or {}
    val_map = (_actionable_vote_cache.get('val_map_by_path') or {}
               ).get(path, {})

    # Match the dialog's 5-day staleness cap for signals.jsonl entries
    # (recommend_cache_validation rows have no equivalent cap there
    # either — same rules so filler ≡ dialog).
    from datetime import datetime as _dt_av, timedelta as _td_av
    _now_av = _dt_av.now()
    _stale_cap = _td_av(days=5)

    actionable = 0
    hidden = 0
    for tk in displayed:
        drop = False
        # Source 1: signals.jsonl consensus_fresh_buy (user-Verify path).
        fresh = fresh_map.get(tk) if isinstance(fresh_map, dict) else None
        if fresh:
            _stale = False
            try:
                ts = fresh.get('ts', '')
                if ts:
                    cts = _dt_av.fromisoformat(ts)
                    if (_now_av - cts) > _stale_cap:
                        _stale = True
            except Exception:
                _stale = False
            if not _stale:
                try:
                    cls = classify(fresh.get('votes', []) or [])
                    if cls.get('drop'):
                        drop = True
                except Exception:
                    pass  # fail-OPEN
        # Source 2: recommend_cache_validation rows (Layer-2 daemon path).
        if not drop:
            val_votes = val_map.get(tk)
            if val_votes:
                try:
                    cls2 = classify(val_votes)
                    if cls2.get('drop'):
                        drop = True
                except Exception:
                    pass  # fail-OPEN
        if drop:
            hidden += 1
        else:
            actionable += 1
    return (actionable, hidden, total)


# v4.14.6.24-fill-terminal: terminal "structurally short" state for
# bands that can't be filled from the current eligible universe + a
# post-dispatch grace window so freshly-dispatched-but-unvoted picks
# don't restart the cycle. Pre-fix: when consensus hid N of 10
# displayed rows, fill-mode saw `actionable < target` and re-dispatched
# every cooldown tick (capping at 60min on the zero-progress ladder)
# even when the eligible universe genuinely couldn't produce more
# BUYable candidates — ~10 AI calls/h per stuck path forever. This
# adds a SECOND gate: once the cooldown ladder has plateaued AND the
# universe is empty of fresh candidates, flag the path terminal and
# skip from `_fill_needed` until a real change (a Layer 2 vote flips,
# a new universe candidate appears, an event fires, OR a 24h failsafe
# expires). All wake checks are cache-only (no AI dispatch) — same
# pattern as the `_actionable_vote_cache` read-only gate above.
_FILL_POST_DISPATCH_GRACE_SECONDS = 10 * 60         # 600s — give Layer 2 a chance to vote before re-evaluating
_FILL_TERMINAL_AT_STREAK = 5                        # promote to terminal after this many ladder steps
_FILL_TERMINAL_FAILSAFE_SECONDS = 24 * 60 * 60      # 24h safety re-check even when nothing observable changed
_FILL_TERMINAL_PEEK_INTERVAL_SECONDS = 5 * 60       # throttle the candidate-peek wake-check


def _displayed_validation_signature(app, path: str):
    """SHA-1 of (ticker, votes_json, validated_at) across this path's
    displayed-tier rows. Changes whenever Layer 2 writes a new vote OR
    membership shifts — the cheap wake-up trigger for a structurally-
    short band. Cache-only, NO AI."""
    try:
        conn = _conn(app)
        if conn is None:
            return None
        with _db_lock(app):
            rows = conn.execute(
                "SELECT rc.ticker, COALESCE(v.votes_json,''), "
                "       COALESCE(v.validated_at,0) "
                "FROM recommend_cache rc "
                "LEFT JOIN recommend_cache_validation v "
                "  ON v.ticker = rc.ticker AND v.path = rc.path "
                "WHERE rc.path = ? AND rc.tier = 'displayed'",
                (path,)).fetchall()
        import hashlib as _hl
        return _hl.sha1(str(sorted(
            (str(r[0]).upper(), str(r[1]), int(r[2] or 0))
            for r in rows
        )).encode()).hexdigest()
    except Exception:
        return None


def _set_structurally_short(app, path: str, reason: str) -> None:
    """Promote a path to the terminal structurally-short state. Idempotent
    (a second call on an already-flagged path is a no-op). Snapshots the
    displayed-tier validation signature so a later Layer 2 vote change
    wakes the path automatically. Best-effort; never raises."""
    try:
        ss = getattr(app, '_fill_structurally_short', None)
        if ss is None:
            ss = {}
            app._fill_structurally_short = ss
        if path in ss:
            return
        now_set = time.time()
        ss[path] = {
            'set_at': now_set,
            'val_signature': _displayed_validation_signature(app, path),
            'last_candidate_check': now_set,
            'reason': reason,
        }
        # v4.14.6.25-lottery-thrash-log-rate-limit: pre-fix this logged
        # an amber "pausing AI dispatch" line on every promotion, but
        # the lottery path was promote→clear (event-driven dispatch)→
        # promote-again ~52 times overnight, with median 75 s between
        # cycles. Each "pausing AI dispatch" line was misleading because
        # the clear happened within ~75 s. Rate-limit: suppress the log
        # if this same path was cleared less than _SS_LOG_QUIET_SECONDS
        # ago. The promotion logic itself is UNCHANGED — only the log
        # noise. Tracked per-path on `app._fill_ss_last_clear_ts`.
        _quiet_until_ts = getattr(app, '_fill_ss_last_clear_ts', None) or {}
        _last_clear = float(_quiet_until_ts.get(path, 0) or 0)
        _SS_LOG_QUIET_SECONDS = 5 * 60  # 5 min
        if _last_clear and (now_set - _last_clear) < _SS_LOG_QUIET_SECONDS:
            return  # silently set the flag; skip the misleading log line
        _log_amber(
            app,
            f"[fill-mode] {path}: structurally short ({reason}) — "
            f"pausing AI dispatch until candidates / consensus / "
            f"recency change. 24h failsafe re-check.")
    except Exception:
        pass


def _clear_structurally_short(app, path: str, reason: str = '') -> None:
    """Wake a structurally-short path. Used by both the internal
    wake-check inside `_should_skip_structurally_short` and the
    event-driven sweep hook (a real event on this path always wakes).
    """
    try:
        ss = getattr(app, '_fill_structurally_short', None) or {}
        if path in ss:
            ss.pop(path, None)
            # v4.14.6.25-lottery-thrash-log-rate-limit: record the clear
            # time so a fresh promote within the next ~5 min skips its
            # misleading "pausing AI dispatch" log line. The clear log
            # itself stays muted-cadence so its noise is bounded.
            try:
                _t = getattr(app, '_fill_ss_last_clear_ts', None)
                if _t is None:
                    _t = {}
                    app._fill_ss_last_clear_ts = _t
                _t[path] = time.time()
            except Exception:
                pass
            if reason:
                _log_muted(
                    app,
                    f"[fill-mode] {path}: structurally-short cleared "
                    f"({reason}).")
    except Exception:
        pass


def _should_skip_structurally_short(app, path: str) -> bool:
    """Wake-aware skip predicate. Returns True iff path is in terminal
    state AND no wake condition is met (caller skips the path from
    `_fill_needed`). Wake conditions, all cache-only / NO AI dispatch:

      1. Failsafe: ≥ 24h since terminal — re-check unconditionally
         (covers the "something changed but my signals missed it"
         long tail).
      2. Displayed-tier validation/membership signature changed —
         Layer 2 wrote a vote, or recommend_cache row swapped.
      3. `_layered_candidate_batch` peek returns ≥ 1 candidate —
         covers BOTH (a) new universe candidate became eligible, and
         (b) a previously-judged ticker's verdict-recency window
         expired (it re-enters the candidate set). Throttled to once
         per `_FILL_TERMINAL_PEEK_INTERVAL_SECONDS` so the peek's DB
         work doesn't run every tick.

    On any wake, the flag is cleared and the function returns False so
    the caller proceeds with one normal dispatch cycle. Subsequent
    cycles re-evaluate: if `_run_fill_mode` again finds 0 actionable
    gain AND no candidates, the terminal flag is re-applied — no
    perma-loop risk.
    """
    ss = getattr(app, '_fill_structurally_short', None) or {}
    info = ss.get(path)
    if not info:
        return False
    now = time.time()
    set_at = float(info.get('set_at') or 0)
    if (now - set_at) >= _FILL_TERMINAL_FAILSAFE_SECONDS:
        _clear_structurally_short(
            app, path,
            reason=f"failsafe re-check at {int((now-set_at)/3600)}h")
        return False
    try:
        cur_sig = _displayed_validation_signature(app, path)
        if cur_sig is not None and cur_sig != info.get('val_signature'):
            _clear_structurally_short(
                app, path,
                reason='displayed validation/membership changed')
            return False
    except Exception:
        pass
    last_check = float(info.get('last_candidate_check') or 0)
    if (now - last_check) >= _FILL_TERMINAL_PEEK_INTERVAL_SECONDS:
        info['last_candidate_check'] = now
        try:
            cand, _ = _layered_candidate_batch(app, path, limit=1)
            if cand:
                _clear_structurally_short(
                    app, path,
                    reason='new candidate eligible')
                return False
        except Exception:
            pass
    return True


def _fill_needed(app) -> list:
    """(path, deficit_score, displayed, bench, dtarget, bfloor) for
    every fill_enabled path below target. Displayed shortfall is
    weighted 3x bench shortfall so the most-starved path fills first.

    v4.14.5.49-coldstart-pick-yield (A2): while any path is cold/catch-up
    (displayed shortfall >= _FILL_COLDSTART_DEFICIT) the list is ordered by
    EXPECTED YIELD (BUY-rate × deficit) so the scarce early AI calls hit the
    high-BUY-rate paths first; near target it reverts to the plain deficit sort
    (steady state unchanged). Flag use_coldstart_yield_ordering (default True).

    v4.14.5.X-actionable-fill (2026-06-10): the displayed shortfall is
    now computed against ACTIONABLE displayed picks — rows whose most-
    recent consensus votes would NOT cause _consensus_says_buy to drop
    them in the Portfolio Recommendations dialog. Pre-patch the filler
    counted raw recommend_cache rows, so when Layer 2 / user-Verify
    revealed AVOID-majority on the displayed tier the dialog hid those
    picks but the filler still thought the path was full — paths sat
    visibly empty without refilling. Now the filler sees what the user
    sees: 10 rows but 2 actionable → needs fill. Bench is left on the
    raw count for this pass (bench drives the reserve that gets
    re-ranked into displayed on the next cache refresh; the visible
    shortfall is what drives refill). Flag-gated by
    cfg['use_actionable_fill_count'] (default True); flag off reverts
    EXACTLY to the raw-row shortfall — rollback surface preserved."""
    targets = _get_path_fill_targets(app)
    counts = _recommend_cache_counts(app)
    needs = []
    try:
        _use_actionable = bool((getattr(app, 'cfg', {}) or {}).get(
            'use_actionable_fill_count', True))
    except Exception:
        _use_actionable = True
    # v4.14.5.91-fill-backoff: read the per-path zero-progress cooldown
    # so paths that aren't making actionable progress are EXCLUDED from
    # the needs list (and from the summary log line) until their cooldown
    # expires. We reuse the existing _fill_cooldown_until dict the
    # session-cap backoff (50-cycle) writes to — same skip semantics, one
    # place to clear, no two parallel cooldown stores to keep in sync.
    try:
        _use_backoff = bool((getattr(app, 'cfg', {}) or {}).get(
            'use_fill_backoff', True))
    except Exception:
        _use_backoff = True
    _now_fn = time.time()
    _cooldowns_fn = getattr(app, '_fill_cooldown_until', None) or {}
    for path, t in targets.items():
        if not isinstance(t, dict) or not t.get('fill_enabled', False):
            continue
        d_raw, b = counts.get(path, (0, 0))
        dtarget = int(t.get('displayed_target', 10))
        bfloor = int(t.get('bench_floor', 10))
        # v4.14.5.X-actionable-fill: replace d_raw with the count of
        # displayed picks that survive the dialog's consensus-AVOID gate.
        # Fall back to d_raw if the helper fails — pre-patch behaviour
        # exactly (no regression on a measurement fault).
        if _use_actionable:
            try:
                act, hidden, tot = _path_actionable_displayed(app, path)
                # Only OVERRIDE d when displayed actually has rows; on an
                # empty path stick with the raw 0 (no false positives).
                if tot > 0:
                    d = act
                    # Make the previously-silent gap VISIBLE: log when raw
                    # row count meets target but actionable does not.
                    if d_raw >= dtarget and act < dtarget:
                        _log_routine(
                            app,
                            f"[fill-mode] {path}: {d_raw} displayed rows "
                            f"but {act} actionable ({hidden} consensus-"
                            f"hidden) → needs fill")
                else:
                    d = d_raw
            except Exception:
                d = d_raw
        else:
            d = d_raw
        d_short = max(0, dtarget - d)
        b_short = max(0, bfloor - b)
        if d_short > 0 or b_short > 0:
            # v4.14.5.91-fill-backoff: trigger classification — a path is
            # "cold-start" (raw displayed below target → no rows to slide
            # in, has to be filled from scratch) vs "actionable-short"
            # (raw rows fully populate displayed but consensus has hidden
            # most of them). The two get different cycle budgets in
            # _run_fill_mode: cold-start keeps the generous 50-cycle
            # session cap, actionable-short gets a tighter cap (3) before
            # a back-off kicks in, because there's no point dispatching
            # 50 times if the market just isn't giving us actionable
            # picks for this path right now. Cold-start is detected on
            # RAW count vs target (the actionable overlay can hide a
            # genuinely-empty path otherwise).
            _is_actionable_short = (
                _use_backoff
                and d_raw >= dtarget
                and d < dtarget)
            # Skip paths whose per-path back-off cooldown is still active.
            # Flag-gated: if use_fill_backoff is off, we apply zero per-
            # path skipping here (existing line-2271 check in
            # _run_fill_mode still handles the legacy session cooldown).
            if _use_backoff:
                _until = float(_cooldowns_fn.get(path, 0) or 0)
                if _until and _now_fn < _until:
                    continue
            # v4.14.6.24-fill-terminal: post-dispatch grace window. After
            # dispatching a batch, give Layer 2 a chance to vote before
            # re-evaluating "actionable < target" — otherwise an AVOID
            # vote drops actionable count and the path re-enters needs
            # immediately, restarting the cycle. Cache-only; the path
            # is neither "filled" nor "needs fill" during the window —
            # it's "awaiting votes."
            _last_disp = getattr(app, '_fill_last_dispatch', None) or {}
            _last_ts = float(_last_disp.get(path, 0) or 0)
            if (_last_ts > 0
                    and (_now_fn - _last_ts)
                        < _FILL_POST_DISPATCH_GRACE_SECONDS):
                continue
            # v4.14.6.24-fill-terminal: terminal "structurally short"
            # skip. Once a path has plateaued at the cooldown ladder
            # AND the universe is empty of fresh candidates, the path
            # is flagged terminal (see promotion sites in
            # _run_fill_mode). The wake-check is cache-only and self-
            # clears on signature change, candidate availability, or
            # 24h failsafe — so a permanently-silent band is impossible.
            if _should_skip_structurally_short(app, path):
                continue
            needs.append((path, d_short * 3 + b_short, d, b,
                          dtarget, bfloor, _is_actionable_short))

    # v4.14.5.49-coldstart-pick-yield (A2): while cold/catch-up, order by
    # EXPECTED YIELD (BUY-rate × deficit) so the scarce early AI calls hit the
    # high-yield paths first; otherwise plain deficit (steady state unchanged).
    # Self-balancing — a filled high-yield path drops out, low-yield paths rise.
    _cold = any((dt_ - d_) >= _FILL_COLDSTART_DEFICIT
                for (_p, _s, d_, _b, dt_, _bf, _as) in needs)
    try:
        _yield_on = bool((getattr(app, 'cfg', {}) or {}).get(
            'use_coldstart_yield_ordering', True))
    except Exception:
        _yield_on = True
    if _cold and _yield_on:
        rates = _path_buy_rates(app)
        needs.sort(key=lambda x: -(
            rates.get(x[0], _COLDSTART_FALLBACK_BUY_RATE) * x[1]))
    else:
        needs.sort(key=lambda x: -x[1])
    return needs


def _fill_mode_has_pending_work(app) -> bool:
    """v4.14.5.46-coldstart-pick-cadence: True when the fill-mode has pick-
    generation work it can do RIGHT NOW — a fill_enabled path is below target
    AND the fill-mode isn't paused for provider exhaustion. The runner loop
    reads this to keep polling at the FAST cadence during cold start / catch-up
    instead of inheriting the event-sweep's idle backoff. When idle (no path
    below target, or paused), returns False so the backoff governs UNCHANGED.
    Cheap (one recommend_cache count read via _fill_needed). Never raises."""
    try:
        if not bool((getattr(app, 'cfg', {}) or {}).get(
                'use_layer1_fill_mode', True)):
            return False
        # Don't fast-poll when paused for provider exhaustion — there's nothing
        # we can do; let the backoff govern so we don't spin.
        #
        # v4.14.5.49-coldstart-pick-yield (A3) EXCEPTION: when the pause is only a
        # temporary provider COOLDOWN and there's still pending pick-work, keep
        # the FAST cadence so we resume PROMPTLY (within ~60s) when the cooldown
        # expires — instead of collapsing into the event-sweep's growing
        # 60→600s backoff exactly when picks are most starved. We never bypass
        # the cooldown (_run_fill_mode still gates dispatch on _scan_availability,
        # which respects it); we only keep the LOOP checking at ~60s. A pause
        # that is NOT a cooldown (daily scan-cap exhausted / providers disabled /
        # none configured — no "cooldown" in the reason) still returns False so
        # the backoff governs and we don't spin when there's genuinely nothing to
        # do for a while. Flag use_unthrottle_under_cooldown (default True).
        if getattr(app, '_fill_paused', False):
            try:
                _under_cd = bool((getattr(app, 'cfg', {}) or {}).get(
                    'use_unthrottle_under_cooldown', True))
            except Exception:
                _under_cd = True
            reason = str(getattr(app, '_fill_pause_reason', '') or '').lower()
            if _under_cd and 'cooldown' in reason:
                return bool(_fill_needed(app))
            return False
        return bool(_fill_needed(app))
    except Exception:
        return False


# ─── v4.14.5.14a.1: layered fill-mode candidate selection ─────────────
#
# Fill-mode used to pull straight from the universe cursor (oldest-
# analyzed-first), which at startup defaults to alphabetical — biasing
# every batch toward A/B/C tickers so newsworthy names (TSLA, SAVE,
# GOOG) rarely got proactively analyzed. The 20-ticker batch is now
# composed from three priority tiers:
#   1. news-active (an event happened — analyze it first)
#   2. filter momentum rank (moving on price/volume, no explicit news)
#   3. universe cursor (coverage filler — the old v4.14.5.14 default)
# No ticker appears twice; each tier excludes everything already chosen.

_NEWS_ACTIVE_WINDOW_SECONDS = 24 * 60 * 60
_TIER2_MOMENTUM_POOL = 100


def _iso_to_epoch(s) -> float:
    """Parse a news_signals 'YYYY-MM-DDThh:mm:ssZ' UTC timestamp to a
    true epoch. The 'Z' (UTC) MUST be honoured — parsing it as a naive
    local time skews the 24h news window by the machine's UTC offset
    (caught by _audit_layered_candidates Test 5)."""
    try:
        from datetime import datetime, timezone
        raw = str(s).split('.')[0].strip()
        raw = raw.replace('Z', '+00:00')
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _last_analyzed_map(app, path: str) -> dict:
    """{ticker: last_analyzed_at epoch int} for this path from
    queue_runner_analysis_log (tired_market.db)."""
    out = {}
    conn = _conn(app)
    if conn is None:
        return out
    with _db_lock(app):
        try:
            for tk, ts in conn.execute(
                    "SELECT ticker, last_analyzed_at "
                    "FROM queue_runner_analysis_log WHERE path = ?",
                    (path,)):
                out[str(tk).upper()] = ts
        except Exception:
            return {}
        return out


def _filled_universe(app) -> set:
    """Tickers with cached daily_bars (analysis has price data).
    Mirrors _build_candidate_shortlist's scope ∩ filled so no tier
    dispatches a ticker with no data to analyze."""
    try:
        import tm_cache
        scope = tm_cache.get_scope_tickers('daily_bars', None)
        if not scope:
            return set()
        unfilled = tm_cache.get_unfilled_tickers('daily_bars', scope)
        return {str(t).upper() for t in (scope - unfilled)}
    except Exception:
        return set()


def _tier1_news_active_candidates(app, path: str, limit: int,
                                   exclude=None) -> list:
    """Tickers with news in the last 24h that the runner hasn't
    analyzed since that news (or never on this path). Ranked newest
    news first, then highest news volume. Restricted to tickers with
    cached daily_bars so the analysis has data to work with."""
    if limit <= 0:
        return []
    excl = {str(t).upper() for t in (exclude or set())}
    try:
        import tm_cache
        nconn = tm_cache.get_connection()
    except Exception:
        return []
    now = time.time()
    cutoff_epoch = now - _NEWS_ACTIVE_WINDOW_SECONDS
    rows = []
    try:
        # Pull recent-news tickers with max recency + volume. The 24h
        # filter is applied in Python (timestamp is an ISO 'Z' string;
        # a string >= comparison is correct for this fixed format but
        # we parse to be safe across any legacy rows).
        for tk, mx, vol in nconn.execute(
                "SELECT ticker, MAX(timestamp), COUNT(*) "
                "FROM news_signals GROUP BY ticker"):
            if not tk:
                continue
            e = _iso_to_epoch(mx)
            if e < cutoff_epoch:
                continue
            rows.append((str(tk).upper(), e, int(vol or 0)))
    except Exception:
        return []
    finally:
        try:
            nconn.close()
        except Exception:
            pass
    if not rows:
        return []
    filled = _filled_universe(app)
    la = _last_analyzed_map(app, path)
    picked = []
    for tk, news_e, vol in rows:
        if tk in excl:
            continue
        if filled and tk not in filled:
            continue
        last = la.get(tk)
        if last is not None:
            try:
                if float(last) >= news_e:
                    continue  # news already analyzed for this path
            except (TypeError, ValueError):
                pass
        picked.append((tk, news_e, vol))
    picked.sort(key=lambda x: (-x[1], -x[2], x[0]))
    return [tk for tk, _e, _v in picked[:limit]]


def _tier2_filter_ranked_candidates(app, path: str, limit: int,
                                     exclude=None) -> list:
    """Top momentum-ranked in-band tickers from the (now demoted)
    filter, minus anything already chosen. The filter output is
    already ranked by score DESC."""
    if limit <= 0:
        return []
    excl = {str(t).upper() for t in (exclude or set())}
    try:
        import tm_recommend_filter as _trf
        import tm_recommend_cache as _trc
        bands = _trc._selected_bands(app)
        fres = _trf.compute_filter(bands)
    except Exception:
        return []
    cands = (fres or {}).get('candidates') or []
    out = []
    for c in cands[:_TIER2_MOMENTUM_POOL]:
        tk = (c.get('ticker') or '').upper()
        if tk and tk not in excl and tk not in out:
            out.append(tk)
        if len(out) >= limit:
            break
    return out


# v4.14.5.14a.9: verdict-recency skip gate. A ticker whose most-recent
# verdict for THIS path is younger than the path's verdict-recency
# window is "still judged" — its verdict stands until it ages out or a
# real event fires. Fill mode must NOT re-burn budget re-confirming it
# (the 2026-05-18 weekend burn: 140 ticker/path pairs re-analyzed 3,903
# times because nothing treated a fresh WATCH/AVOID as a reason to
# skip). Event-driven dispatch never calls this — a real trigger on a
# recently-judged ticker still goes through.
#
# Verdicts that BLOCK re-analysis: BUY / WATCH / AVOID (a real decision
# was reached). NON-verdicts that do NOT block: NO_CALL and 'failed'
# (the AI didn't actually decide / never ran — retrying is legitimate,
# not waste).
_VERDICT_OUTCOMES = ('BUY', 'WATCH', 'AVOID')


# v4.14.5.14-loop-prevention: cooldown for non-verdict outcomes
# (NO_CALL / failed). A genuine NO_CALL means the model wouldn't commit
# on the data it had; re-attempting it every fill cycle just re-burns
# budget on the same non-verdict. 24h is long enough that new news /
# price movement can shift the picture, short enough that a
# now-judgeable ticker isn't stuck. See the BFLY/BHE/BJRI ~16-24
# calls/day loops of 2026-05-20/21 (all NO_CALL, ~hourly).
_NO_CALL_COOLDOWN_SECONDS = 24 * 60 * 60

# v4.14.5.94-watch-phase1 (2026-06-11): WATCH gets its own SHORTER
# re-look cutoff. Pre-patch WATCH shared BUY/AVOID's full path window
# (aggressive 72h / moderate 7d / slow_safe 21d), so a "wait" went
# silent for days-to-weeks and the named entry condition often passed
# before the next re-look. WATCH is a deferred maybe; reconsider it
# sooner than a firm decision. The fraction is per-path-uniform — the
# WATCH cutoff for a path is the path's regular window × FRACTION:
#   aggressive 72h × 1/3 →  24h
#   moderate    7d × 1/3 → 2.3d
#   slow_safe  21d × 1/3 →   7d
# BUY/AVOID firm decisions are UNAFFECTED — they keep their full
# windows. NO_CALL/failed unchanged (still 24h via the existing
# _NO_CALL_COOLDOWN_SECONDS). Flag use_watch_short_recheck default
# True; off → WATCH uses the full path window exactly as pre-patch.
_WATCH_RECHECK_FRACTION = 1.0 / 3.0


def _recently_judged_set(app, path: str) -> set:
    """Tickers on `path` to EXCLUDE from fill-mode's three candidate
    tiers this cycle:
      - committed verdicts (BUY/WATCH/AVOID) younger than the path's
        verdict-recency window (tm_event_triggers
        .verdict_recency_window_seconds), and
      - v4.14.5.14-loop-prevention: NO_CALL / failed outcomes younger
        than _NO_CALL_COOLDOWN_SECONDS (24h) — closes the persistent-
        NO_CALL re-loop hole (the committed-verdict gate never matched
        NO_CALL, so a ticker the model won't judge looped at the fill
        cadence). Sub-flag cfg['use_no_call_cooldown'] (default True)
        rolls JUST this half back; the committed-verdict gate is
        unchanged either way.

    Flag cfg['use_verdict_recency_skip'] (default True) governs the
    whole gate. Flag off, no DB, or any error → empty set: the gate
    fails OPEN (exact pre-patch fill behaviour), so a gate fault can
    never wedge fill mode."""
    try:
        if not bool((getattr(app, 'cfg', {}) or {}).get(
                'use_verdict_recency_skip', True)):
            return set()
    except Exception:
        return set()
    conn = _conn(app)
    if conn is None:
        return set()
    with _db_lock(app):
        try:
            import tm_event_triggers as _tet
            window = int(_tet.verdict_recency_window_seconds(app, path))
        except Exception as e:
            _log_amber(
                app,
                f"[fill-mode] verdict-recency window lookup failed (gate "
                f"open this cycle): {type(e).__name__}: {e}")
            return set()
        now = int(time.time())
        cutoff = now - window
        # v4.14.5.94-watch-phase1: WATCH gets a shorter re-look cutoff
        # so "wait" doesn't go silent for the full path window. BUY and
        # AVOID firm decisions keep the full cutoff. NO_CALL/failed is
        # handled below by its own _NO_CALL_COOLDOWN_SECONDS path (24h).
        try:
            _short_watch = bool((getattr(app, 'cfg', {}) or {}).get(
                'use_watch_short_recheck', True))
        except Exception:
            _short_watch = True
        watch_cutoff = (now - int(window * _WATCH_RECHECK_FRACTION)
                        if _short_watch else cutoff)
        out: set = set()
        try:
            # BUY/AVOID firm verdicts — full path window (unchanged).
            cur = conn.execute(
                "SELECT ticker FROM queue_runner_analysis_log "
                "WHERE path = ? AND last_analyzed_at >= ? "
                "AND last_outcome IN ('BUY', 'AVOID')",
                (path, cutoff))
            out = {str(r[0]).upper() for r in cur.fetchall()}
            # WATCH — shorter cutoff when the flag is on; identical to
            # the old behaviour when off (watch_cutoff == cutoff).
            cur = conn.execute(
                "SELECT ticker FROM queue_runner_analysis_log "
                "WHERE path = ? AND last_analyzed_at >= ? "
                "AND last_outcome = 'WATCH'",
                (path, watch_cutoff))
            out.update(str(r[0]).upper() for r in cur.fetchall())
        except Exception as e:
            _log_amber(
                app,
                f"[fill-mode] verdict-recency query failed (gate open "
                f"this cycle): {type(e).__name__}: {e}")
            return set()
        # v4.14.5.14-loop-prevention: add NO_CALL / failed tickers inside
        # the shorter 24h cooldown. Case-insensitive match against the
        # exact values _record_analysis_outcome writes ('NO_CALL', 'failed').
        # Independent try/except so a fault here never drops the
        # committed-verdict skips computed above.
        try:
            if bool((getattr(app, 'cfg', {}) or {}).get(
                    'use_no_call_cooldown', True)):
                nc_cutoff = now - _NO_CALL_COOLDOWN_SECONDS
                cur = conn.execute(
                    "SELECT ticker FROM queue_runner_analysis_log "
                    "WHERE path = ? AND last_analyzed_at >= ? "
                    "AND UPPER(last_outcome) IN ('NO_CALL', 'FAILED')",
                    (path, nc_cutoff))
                out.update(str(r[0]).upper() for r in cur.fetchall())
        except Exception as e:
            _log_amber(
                app,
                f"[fill-mode] NO_CALL-cooldown query failed (committed-"
                f"verdict gate still applied): {type(e).__name__}: {e}")
        return out


def _latest_close_for(ticker):
    """Latest cached daily_bars close for ONE ticker, or None. Same
    data basis the path pools use to decide price-band membership.
    Cache-only; None on any miss → caller fails open."""
    try:
        import tm_cache
        conn = tm_cache.get_connection()
        if conn is None:
            return None
        row = conn.execute(
            "SELECT close FROM daily_bars WHERE ticker = ? "
            "ORDER BY date DESC LIMIT 1",
            (str(ticker).upper(),)).fetchone()
        if not row or row[0] is None:
            return None
        return float(row[0])
    except Exception:
        return None


_eligibility_skip_log_state: dict = {}
_ELIGIBILITY_PRICE_MOVE_THRESHOLD_PCT = 0.05  # 5% price movement re-logs
_ELIGIBILITY_HEARTBEAT_SECONDS = 3600.0  # re-log once per hour anyway


def _should_log_eligibility_skip(ticker, path, src_label, price,
                                  band_lo, band_hi):
    """v4.14.5.14-cadence-dampening-and-f5a-hygiene Part C (2026-05-20):
    decide whether to emit a per-ticker `[eligibility] skipped` log
    line, given the current (ticker, path, src_label) state. Returns
    True for the first sight of a (ticker, path, src_label) tuple,
    when the band edges move, when the price has moved more than 5%
    since the last logged price, or after a 1-hour heartbeat window
    elapses. Returns False otherwise — the filter still drops the
    candidate; only the log line is suppressed.

    Reason this matters: ACRS / ALGM / ADT each fired 200+ identical
    `[eligibility] skipped` lines today (one every ~3 min) for prices
    that hadn't moved meaningfully relative to a band that hadn't
    changed at all. That's pure log noise — every cycle re-stated
    the same fact. The rollup line "(<path>, <src>): N filtered;
    M remain" still fires every cycle as the heartbeat that
    confirms the cadence is running.

    State is in-memory (resets on restart). First cycle after restart
    naturally re-logs once per filtered ticker — that's correct
    "I'm starting fresh" behaviour, not a bug.
    """
    try:
        import time as _t
        key = (str(ticker).upper(), str(path), str(src_label))
        now = _t.time()
        state = _eligibility_skip_log_state.get(key)
        if state is None:
            _eligibility_skip_log_state[key] = (now, price,
                                                 band_lo, band_hi)
            return True  # first sight — always log
        last_t, last_px, last_lo, last_hi = state
        # Band edges changed? (Path config got mutated mid-session.)
        if last_lo != band_lo or last_hi != band_hi:
            _eligibility_skip_log_state[key] = (now, price,
                                                 band_lo, band_hi)
            return True
        # Heartbeat: re-log once per hour even if nothing changed.
        if (now - last_t) >= _ELIGIBILITY_HEARTBEAT_SECONDS:
            _eligibility_skip_log_state[key] = (now, price,
                                                 band_lo, band_hi)
            return True
        # Price moved meaningfully? (>=5% from last logged price.)
        try:
            if last_px and last_px > 0:
                pct = abs(price - last_px) / float(last_px)
                if pct >= _ELIGIBILITY_PRICE_MOVE_THRESHOLD_PCT:
                    _eligibility_skip_log_state[key] = (
                        now, price, band_lo, band_hi)
                    return True
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        return False  # quiet — same state as last time
    except Exception:
        return True  # fail-OPEN: never silently lose a log line


def _eligibility_price_band_filter(app, path, candidates,
                                   src_label='dispatch', quiet=False):
    """v4.14.5.14a.14: BEFORE any AI call, drop (ticker, path) pairs
    whose price is outside `path`'s pool price band — on EVERY dispatch
    path (fill, event-driven, cadence). The verdict for such a pair is
    structurally guaranteed to be a content-free "wrong price band"
    AVOID (the 2026-05-18 ABNB-on-lottery waste).

    Single source of truth = the EXISTING pool cfg
    (tm_path_candidate_pools._cfg → min_price_usd / max_price_usd). No
    new band definition; pool definitions are not touched. Seed-list /
    unbounded paths (no min & no max) get no price gate. Fail-OPEN:
    flag off, band lookup error, or UNKNOWN price (cache miss) → keep
    the candidate (the gate filters known-bad pairs, it never silently
    drops a ticker whose price we just haven't fetched). Flag
    cfg['use_eligibility_price_band_gate'] (default True). Logs each
    skip + a per-pass summary; returns the kept list."""
    cands = list(candidates or [])
    try:
        if not bool((getattr(app, 'cfg', {}) or {}).get(
                'use_eligibility_price_band_gate', True)):
            return cands
    except Exception:
        return cands
    try:
        import tm_path_candidate_pools as _tpcp
        pcfg = _tpcp._cfg(app, path) or {}
    except Exception as e:
        _log_amber(
            app,
            f"[eligibility] band lookup failed for {path} (gate "
            f"open this pass): {type(e).__name__}: {e}")
        return cands
    lo = pcfg.get('min_price_usd')
    hi = pcfg.get('max_price_usd')
    if lo is None and hi is None:
        return cands  # seed-list / unbounded path → no price gate
    if lo is not None and hi is not None:
        band = f"${float(lo):g}-${float(hi):g}"
    elif lo is not None:
        band = f">=${float(lo):g}"
    else:
        band = f"<=${float(hi):g}"
    kept = []
    n_skip = 0
    for tk in cands:
        price = _latest_close_for(tk)
        if price is None:
            kept.append(tk)  # fail-open: never drop on missing price
            continue
        if (lo is not None and price < float(lo)) or \
                (hi is not None and price > float(hi)):
            n_skip += 1
            # v4.14.5.14-cadence-dampening-and-f5a-hygiene Part C
            # (2026-05-20): suppress the per-ticker log line when the
            # (ticker, path, src_label) state hasn't meaningfully
            # changed since the last log. The filter still drops the
            # candidate; only the log line is dampened. The rollup
            # below ALWAYS fires as the heartbeat.
            # v4.14.5.14-recommend-cache-band-gate: `quiet` suppresses ALL
            # logging from this gate. Used by the cache-write caller
            # (refresh_recommend_cache), which runs often and just needs the
            # rows excluded silently — the dispatch + Layer 2 callers keep
            # their (dampened) logging. Default False = unchanged.
            if not quiet and _should_log_eligibility_skip(
                    tk, path, src_label, price, lo, hi):
                _log_muted(
                    app,
                    f"[eligibility] skipped {tk} on {path} — price "
                    f"${price:g} outside band {band}")
            continue
        kept.append(tk)
    if n_skip and not quiet:
        _log_muted(
            app,
            f"[eligibility] ({path}, {src_label}): {n_skip} "
            f"candidate(s) filtered (price-band mismatch); "
            f"{len(kept)} remain.")
    return kept


def _path_candidate_pool(app, path):
    """The path's candidate-pool ticker set (upper-cased), or None when
    pools are disabled / empty / unavailable.

    v4.14.5.14-cadence-pool-fix: single source for the `restrict=`
    argument that _build_candidate_shortlist must receive on the
    cadence (_run_one_pass_body) and legacy-fill paths so their
    shortlist is drawn from the path's pool — the SAME population
    _eligible_paths_for later requires. Without this, those paths
    shortlisted the whole universe, every non-pool ticker gated out at
    dispatch ("0 eligible paths"), and — because the gate skips before
    recording an outcome — the staleness cursor never advanced, pinning
    the same non-pool tickers at the top forever (the 2026-05-22 gating
    drought). Mirrors the pool computation _layered_candidate_batch
    already uses for its tiers. Fail-OPEN to None (no restriction) so a
    pool fault never blocks dispatch."""
    try:
        if not bool((getattr(app, 'cfg', {}) or {}).get(
                'use_path_candidate_pools', True)):
            return None
        import tm_path_candidate_pools as _tpcp
        pl = _tpcp.get_path_universe(app, path) or []
        if pl:
            return {str(t).upper() for t in pl}
    except Exception:
        return None
    return None


def _layered_candidate_batch(app, path: str, limit: int = 20):
    """Compose the fill-mode batch from the three priority tiers.
    Returns (batch, (n_tier1, n_tier2, n_tier3)). Flag-gated by
    cfg['use_layered_candidate_selection'] (default True); False
    restores v4.14.5.14's universe-cursor-only behaviour.

    v4.14.5.14a.9: a verdict-recency skip set (recently-judged tickers
    for this path) is excluded from EVERY tier so fill mode stops
    re-confirming valid verdicts. Count stashed on
    app._fill_last_skip_count for the dispatch log. Return shape is
    unchanged (audits unpack the 3-tuple)."""
    skip = _recently_judged_set(app, path)
    try:
        app._fill_last_skip_count = len(skip)
    except Exception:
        pass

    if not bool((getattr(app, 'cfg', {}) or {}).get(
            'use_layered_candidate_selection', True)):
        # v4.14.5.14-cadence-pool-fix: pool-restrict the legacy
        # (use_layered_candidate_selection=False) fill branch too, for the
        # same reason as the cadence path. Dormant on the default config
        # (this branch is bypassed when the flag is True), but closes the
        # gap so a future rollback-flag flip can't reintroduce the
        # whole-universe shortlist. Fail-open to None when pools are off.
        batch = _build_candidate_shortlist(
            app, path, exclude=skip, limit=limit,
            restrict=_path_candidate_pool(app, path))
        return batch, (0, 0, len(batch))

    # v4.14.5.14a.6: per-path candidate pool. slow_safe/moderate =
    # curated seed lists; aggressive/lottery/penny = dynamic price
    # pools. Each tier is intersected with the pool so a path only
    # ever analyses path-appropriate tickers (this is the fix for the
    # 0%-BUY "momentum small-caps routed to slow_safe" mismatch).
    # Empty pool OR flag off → no restriction (exact pre-a.6
    # behaviour; a pool fault never blocks fill).
    pool = None
    if bool((getattr(app, 'cfg', {}) or {}).get(
            'use_path_candidate_pools', True)):
        try:
            import tm_path_candidate_pools as _tpcp
            pl = _tpcp.get_path_universe(app, path) or []
            if pl:
                pool = {str(t).upper() for t in pl}
        except Exception:
            pool = None

    def _inpool(seq):
        if pool is None:
            return list(seq)
        return [t for t in seq if str(t).upper() in pool]

    # v4.14.5.14a.9: every tier excludes the running batch AND the
    # verdict-recency skip set, so a recently-judged ticker can't slip
    # in via news-active or momentum ranking (the two tiers that had
    # NO recency awareness — the core 2026-05-18 burn mechanism).
    batch: list = []
    t1 = _inpool(_tier1_news_active_candidates(
        app, path, limit=limit, exclude=set(batch) | skip))
    batch.extend(t1)
    t2 = []
    if len(batch) < limit:
        t2 = _inpool(_tier2_filter_ranked_candidates(
            app, path, limit=max(limit * 3, 100),
            exclude=set(batch) | skip))
        t2 = t2[:limit - len(batch)]
        batch.extend(t2)
    t3 = []
    if len(batch) < limit:
        t3 = _build_candidate_shortlist(
            app, path, exclude=set(batch) | skip,
            limit=limit - len(batch), restrict=pool)
        batch.extend(t3)
    batch = batch[:limit]
    return batch, (len(t1), len(t2), len(t3))


def _scan_availability(app):
    """v4.14.5.14a.3: (can_scan, reason).

    AUTHORITATIVE decision = tm_api_providers.scan_can_run(), which
    runs the exact scan-eligibility path the dispatch uses (applies
    the 'scan' call-type cap_factor 0.3 + cooldown + caps). This
    replaces the v4.14.5.14a.2 pick_top_ai proxy, which evaluated
    GENERAL provider caps and so reported "available" while the scan
    router rejected everything — the circuit breaker never engaged
    (the 2:41pm-2026-05-17 dispatch-into-a-wall bug).

    pick_top_ai is now consulted ONLY to phrase the human-readable
    pause reason (cooldown seconds / all-capped), never for the
    decision. Fail-OPEN on internal error (scan_can_run already
    fail-opens) so a check bug never wedges fill mode.
    """
    try:
        import tm_api_providers as _tapi
        can = _tapi.scan_can_run()
    except Exception:
        return True, ''
    if can:
        return True, ''
    # Not eligible — derive a friendly reason (best-effort only).
    reason = 'unknown'
    try:
        import tm_top_ai_picker
        r = tm_top_ai_picker.pick_top_ai(app) or {}
        reason = r.get('reason') or 'unknown'
        if reason == 'all_cooldown':
            sec = int(r.get('cooldown_remaining_sec', 0) or 0)
            return False, (f"all scan-eligible providers in cooldown "
                           f"(shortest clears in ~{sec}s)")
        if reason == 'all_exhausted':
            return False, ("all scan-eligible providers at their "
                           "daily scan cap")
        if reason == 'all_disabled':
            return False, "all AI providers are disabled"
        if reason == 'none_configured':
            return False, "no AI providers configured"
    except Exception:
        pass
    return False, ("all scan-eligible providers exhausted "
                   "(cooldown or scan-cap)")


def _run_fill_mode(app) -> None:
    """Layer 1 orchestrator. Flag-gated by cfg['use_layer1_fill_mode']
    (default True). Picks the most-starved fill_enabled path not in
    cooldown, runaway-caps it, dispatches one single-AI pass via the
    existing trigger-dispatch loop, then refreshes recommend_cache so
    the new BUYs surface immediately. Never raises."""
    try:
        if not bool((getattr(app, 'cfg', {}) or {}).get(
                'use_layer1_fill_mode', True)):
            return
        if _is_paused(app) or _stop_set(app):
            return
        # v4.14.5.62-lookup-backoff: a user lookup is waiting on the providers —
        # don't START a new fill pass this cycle (yield the shared limiter).
        # No-op unless lookup_priority_backoff is on AND a lookup is in flight.
        # The runner loop re-ticks shortly; fill resumes when the lookup ends.
        if _lookup_backoff_active(app):
            return
        needs = _fill_needed(app)
        if not needs:
            return

        # v4.14.5.14a.2 Component B: circuit breaker. If there's work
        # to do but no provider can do it, PAUSE (don't dispatch into
        # the void writing nothing). Resume when one recovers. Only
        # fill mode pauses — the event-driven sweep runs independently
        # in _runner_loop and still fires (B3). Flag-gated.
        if bool((getattr(app, 'cfg', {}) or {}).get(
                'use_fill_mode_pause_on_exhaustion', True)):
            can_scan, reason = _scan_availability(app)
            nowt = time.time()
            if not can_scan:
                if not getattr(app, '_fill_paused', False):
                    app._fill_paused = True
                    app._fill_pause_reason = reason
                    app._fill_pause_since = nowt
                    app._fill_pause_last_log = nowt
                    _log_amber(
                        app,
                        f"[fill-mode] pause: {reason}. Will retry "
                        f"when a provider becomes available.")
                else:
                    app._fill_pause_reason = reason
                    last = float(getattr(app, '_fill_pause_last_log',
                                         0) or 0)
                    if nowt - last >= 300:
                        app._fill_pause_last_log = nowt
                        _log_muted(
                            app,
                            f"[fill-mode] still paused: {reason}")
                return
            if getattr(app, '_fill_paused', False):
                app._fill_paused = False
                app._fill_pause_reason = None
                _log_muted(
                    app,
                    "[fill-mode] resumed: a provider is available "
                    "again")

        now = time.time()
        cooldowns = getattr(app, '_fill_cooldown_until', None)
        if cooldowns is None:
            cooldowns = {}
            app._fill_cooldown_until = cooldowns
        cycles = getattr(app, '_fill_cycle_count', None)
        if cycles is None:
            cycles = {}
            app._fill_cycle_count = cycles
        # v4.14.5.91-fill-backoff: zero-progress streak per path drives
        # the exponential cooldown; actionable-short streak drives the
        # tighter consecutive-cycle cap (so an actionable-short path
        # yields after 3 cycles instead of 50). `_fill_serviced_tick`
        # records, per path, the value of `_fill_tick_counter` the last
        # time we dispatched for it — that's the fairness key (oldest
        # = least-recently-serviced). All four are app-attrs (per-
        # process state, like the legacy cooldowns/cycles dicts).
        zps = getattr(app, '_fill_zero_progress_streak', None)
        if zps is None:
            zps = {}
            app._fill_zero_progress_streak = zps
        ass = getattr(app, '_fill_actionable_short_streak', None)
        if ass is None:
            ass = {}
            app._fill_actionable_short_streak = ass
        serviced = getattr(app, '_fill_serviced_tick', None)
        if serviced is None:
            serviced = {}
            app._fill_serviced_tick = serviced
        # v4.14.5.92-sweep-cursor: per-path "no candidates" timer. When a
        # fill cycle for path P finds zero analysable candidates (every
        # ticker recent-verdict-valid OR the gated tiers empty), set
        # P's entry to `now + _FILL_NO_CANDIDATES_COOLDOWN_SECONDS`.
        # While the timer holds, the fairness overlay treats P as NOT
        # eligible for the fairness yield (it can't be "starving" while
        # there is nothing it could do). Cleared the moment P does
        # find candidates again. Solves the v4.14.5.91 fairness deadlock
        # where slow_safe (no candidates) was yielded to every tick,
        # starving aggressive (which had real work).
        no_cand = getattr(app, '_fill_path_no_candidates_until', None)
        if no_cand is None:
            no_cand = {}
            app._fill_path_no_candidates_until = no_cand
        tick_n = int(getattr(app, '_fill_tick_counter', 0)) + 1
        app._fill_tick_counter = tick_n

        try:
            _use_backoff = bool((getattr(app, 'cfg', {}) or {}).get(
                'use_fill_backoff', True))
        except Exception:
            _use_backoff = True
        try:
            _use_fairness_skip_empty = bool(
                (getattr(app, 'cfg', {}) or {}).get(
                    'use_fairness_skip_empty', True))
        except Exception:
            _use_fairness_skip_empty = True

        summ = "; ".join(f"{p}: {d}/{dt} displayed"
                         for p, _s, d, _b, dt, _bf, _as in needs)
        _log_muted(app, f"[fill-mode] tick: {len(needs)} path(s) "
                        f"need fill ({summ})")

        # v4.14.5.91-fill-backoff: build the eligibility-ordered list
        # once, then apply the fairness overlay. The needs list is
        # already primary-sorted by yield × deficit (or plain deficit);
        # we honour that order EXCEPT when a path has gone
        # _FILL_FAIRNESS_STARVE_TICKS or more ticks without service AND
        # the would-be top pick was serviced more recently than the
        # un-serviced one — then the starving path jumps to the front.
        # This pairs with the per-path back-off: when the hardest path
        # is cooling, the runner-up gets its natural turn; even WITHOUT
        # a back-off-active path, no eligible path is starved more than
        # ~3 ticks. Flag-off → legacy primary-sort first-eligible pick.
        _eligible = []
        for p, _s, d, b, dt, bf, _as in needs:
            until = cooldowns.get(p, 0)
            if until and now < until:
                continue
            _eligible.append((p, d, b, dt, bf, _as))
        if not _eligible:
            return
        # v4.14.5.92-sweep-cursor: separate eligibility (path needs fill +
        # not in cooldown) from FAIRNESS-eligibility (also has candidates
        # right now). A path in the "no-candidates" cooldown can't make
        # progress this tick, so it must NOT win the fairness yield —
        # otherwise the fairness overlay starves productive paths to
        # service an idle one (the v4.14.5.91 slow_safe deadlock).
        if _use_fairness_skip_empty:
            _fairness_pool = [
                ent for ent in _eligible
                if float(no_cand.get(ent[0], 0) or 0) <= now
            ]
        else:
            _fairness_pool = list(_eligible)
        # All-paths-idle dampener: every eligible path is in the no-
        # candidates cooldown. Log one concise line and bail (no
        # dispatch, no "yielding to" spam). Re-arms after a short
        # quiet window so a genuinely stuck idle still pings the log.
        if not _fairness_pool:
            _last_idle_log = float(getattr(
                app, '_fill_all_idle_last_log', 0) or 0)
            if (now - _last_idle_log) >= 300:
                _log_routine(
                    app,
                    "[fill-mode] all paths idle this cycle — every "
                    "fill_enabled path is recency-gated or has no "
                    "analysable candidates. Waiting for verdicts to "
                    "age out / events to fire.")
                app._fill_all_idle_last_log = now
            return
        chosen = None
        if _use_backoff and len(_fairness_pool) > 1:
            top_p = _fairness_pool[0][0]
            top_last = int(serviced.get(top_p, 0) or 0)
            top_age = tick_n - top_last if top_last else tick_n
            # Find the fairness-eligible path with the oldest service
            # timestamp.
            starve_p, starve_age = None, -1
            starve_tuple = None
            for ent in _fairness_pool:
                p_ = ent[0]
                last = int(serviced.get(p_, 0) or 0)
                age = tick_n - last if last else tick_n
                if age > starve_age:
                    starve_age = age
                    starve_p = p_
                    starve_tuple = ent
            if (starve_p is not None and starve_p != top_p
                    and starve_age >= _FILL_FAIRNESS_STARVE_TICKS
                    and starve_age > top_age):
                chosen = starve_tuple
                _log_muted(
                    app,
                    f"[fill-mode] fairness: yielding to {starve_p} "
                    f"(unserviced {starve_age} tick(s); top {top_p} "
                    f"was serviced {top_age} tick(s) ago)")
        if chosen is None:
            chosen = _fairness_pool[0]
        path, d, b, dt, bf, _as = chosen

        c = int(cycles.get(path, 0))
        # v4.14.5.91-fill-backoff: tighter cycle cap for the
        # actionable-short trigger. The legacy 50-cycle cap was
        # designed for genuine cold-start fills; if raw rows already
        # meet target and the bottleneck is consensus hiding picks,
        # 3 consecutive cycles without reaching target is enough
        # evidence that another cycle right now won't help — back off.
        _short_streak = int(ass.get(path, 0) or 0)
        if (_use_backoff and _as
                and _short_streak >= _ACTIONABLE_SHORT_MAX_CONSECUTIVE):
            _apply_zero_progress_cooldown(app, path, _short_streak,
                reason='actionable-short cap reached')
            # v4.14.6.24-fill-terminal: also promote to terminal here.
            # The actionable-short cap fires when raw rows already
            # meet target but consensus has hidden enough that
            # actionable still doesn't — i.e. the dispatch found
            # candidates but they're not converting. If on top of
            # that the universe peek is also empty, there's nothing
            # left to try; the hourly cooldown retry is wasted budget.
            try:
                _peek_cand, _ = _layered_candidate_batch(
                    app, path, limit=1)
                if not _peek_cand:
                    _set_structurally_short(
                        app, path,
                        reason='actionable-short cap + universe swept')
            except Exception:
                pass
            cycles[path] = 0
            ass[path] = 0
            return
        if c >= _FILL_MAX_CYCLES_PER_SESSION:
            cooldowns[path] = now + _FILL_COOLDOWN_SECONDS
            cycles[path] = 0
            _log_amber(
                app,
                f"[fill-mode] backoff: path '{path}' analyzed "
                f"~{_FILL_MAX_CYCLES_PER_SESSION * 20} tickers without "
                f"reaching target (current: {d}/{dt} displayed, BUY "
                f"rate too low). Cooling down 30 min.")
            return

        # v4.14.5.46-coldstart-pick-cadence: enlarge the per-pass candidate
        # batch while this path is FAR below target (cold start / catch-up) so
        # a pass uses the provider's per-minute headroom; revert to the normal
        # batch near target (steady-state). Overflow beyond a single provider's
        # cap is gracefully skip-retried (PROVIDER_UNAVAILABLE handling).
        _batch = (_FILL_COLDSTART_BATCH
                  if (dt - d) >= _FILL_COLDSTART_DEFICIT
                  else _FILL_NORMAL_BATCH)
        candidates, (n1, n2, n3) = _layered_candidate_batch(
            app, path, limit=_batch)
        if not candidates:
            # v4.14.5.92-sweep-cursor: mark this path as "no candidates"
            # for the next ~5 min so the fairness overlay doesn't yield
            # to it every tick. Also stamp serviced[path]=tick_n so the
            # starvation age clock resets — an idle path is "satisfied
            # for now," not "starving."
            no_cand[path] = now + _FILL_NO_CANDIDATES_COOLDOWN_SECONDS
            serviced[path] = tick_n
            # v4.14.6.24-fill-terminal: if this path has ALREADY been
            # struggling (zero-progress or actionable-short streak > 0)
            # and the universe just gave us nothing, that's the
            # terminal signal — the cycle has confirmed it can't fill.
            # First-time no-candidates is benign (just "wait 5 min and
            # try again"), so don't promote on a clean streak.
            try:
                _zps_v = int((getattr(app, '_fill_zero_progress_streak',
                                       {}) or {}).get(path, 0) or 0)
                _ass_v = int(ass.get(path, 0) or 0)
                if (_zps_v + _ass_v) > 0:
                    _set_structurally_short(
                        app, path,
                        reason='no candidates after prior struggle')
            except Exception:
                pass
            _nskip = int(getattr(app, '_fill_last_skip_count', 0))
            if _nskip > 0:
                _log_routine(
                    app,
                    f"[fill-mode] ({path}): nothing to analyze this "
                    f"cycle — {_nskip} candidate(s) skipped (recent "
                    f"verdict still valid). Fill is idle until a "
                    f"verdict ages out or an event fires. This is the "
                    f"v4.14.5.14a.9 verdict-recency gate working.")
            else:
                _log_routine(
                    app,
                    f"[fill-mode] ({path}): no candidates this cycle "
                    f"(no news, filter empty, universe sweep "
                    f"exhausted; will recycle).")
            return
        # v4.14.5.92-sweep-cursor: candidates available again → clear
        # the no-candidates cooldown so the path is back in the normal
        # fairness pool.
        no_cand.pop(path, None)
        cycles[path] = c + 1
        # v4.14.5.91-fill-backoff: record this tick as a service event for
        # the fairness overlay, and capture actionable_before so we can
        # compute Δactionable at cycle-complete.
        serviced[path] = tick_n
        if _as:
            ass[path] = _short_streak + 1
        _actionable_before = d  # d already == actionable count (per .90)
        _log_muted(
            app,
            f"[fill-mode] dispatch ({path}): {len(candidates)} tickers "
            f"({n1} news-active, {n2} momentum-ranked, {n3} universe "
            f"cursor; {int(getattr(app, '_fill_last_skip_count', 0))} "
            f"skipped: recent verdict still valid; target deficit: "
            f"{max(0, dt - d)} displayed, {max(0, bf - b)} bench "
            f"floor; cycle "
            f"{cycles[path]}/{_FILL_MAX_CYCLES_PER_SESSION})")

        # v4.14.5.14-keep-awake: a real fill pass is about to dispatch
        # (this blocks while the candidates are analysed — minutes on a
        # slow provider), so hold the system awake now. Released
        # centrally by _manage_keepawake once the runner idles.
        try:
            app._keepawake_worked_this_cycle = True
            from tm_keepawake import request_keep_awake
            request_keep_awake('fill mode dispatching')
        except Exception:
            pass

        run_one_pass_for_triggers(app, path, candidates,
                                  dispatch_label='fill-mode')

        # v4.14.6.24-fill-terminal: stamp dispatch ts so _fill_needed's
        # post-dispatch grace window can skip this path until Layer 2
        # has a chance to vote (typical: ~5 min from dispatch to vote).
        try:
            _ldp = getattr(app, '_fill_last_dispatch', None)
            if _ldp is None:
                _ldp = {}
                app._fill_last_dispatch = _ldp
            _ldp[path] = time.time()
        except Exception:
            pass

        # Refresh recommend_cache so the new BUYs surface now (and the
        # deficit shrinks, closing the fill loop) instead of waiting
        # for the separate 5-min cache daemon.
        try:
            import tm_recommend_cache as _trc
            _trc.refresh_recommend_cache(app, paths=[path])
        except Exception as e:
            _log_amber(
                app,
                f"[fill-mode] post-pass cache refresh failed: "
                f"{type(e).__name__}: {e}")
        counts = _recommend_cache_counts(app)
        nd, nb = counts.get(path, (d, b))
        # v4.14.5.91-fill-backoff: measure Δactionable to drive the
        # zero-progress streak / back-off. Reuses _path_actionable_displayed
        # (TTL-cached, same data the dialog reads — no AI dispatch). Force
        # a fresh classification here by clearing the cache built_at so
        # post-dispatch validation rows aren't masked by the pre-dispatch
        # snapshot. Fail-OPEN: any error in the measurement leaves the
        # streak/cooldown unchanged (legacy behaviour, no regression).
        _actionable_after = nd  # fallback to raw if helper unavailable
        try:
            _actionable_vote_cache['built_at'] = 0.0
            _aft_a, _aft_h, _aft_t = _path_actionable_displayed(app, path)
            if _aft_t > 0:
                _actionable_after = _aft_a
        except Exception:
            pass
        try:
            if _use_backoff:
                _delta = int(_actionable_after) - int(_actionable_before)
                if _delta > 0:
                    # Progress! Clear streaks + any back-off cooldown so
                    # the path resumes its normal fill cadence.
                    if (path in (getattr(app,
                                         '_fill_zero_progress_streak',
                                         {}) or {})
                            or path in (getattr(app,
                                                '_fill_cooldown_until',
                                                {}) or {})):
                        _log_muted(
                            app,
                            f"[fill-mode] {path}: actionable +{_delta} "
                            f"this cycle → back-off cleared")
                    _reset_zero_progress_streak(app, path)
                else:
                    _prior = int((getattr(
                        app, '_fill_zero_progress_streak', {})
                        or {}).get(path, 0) or 0)
                    _apply_zero_progress_cooldown(
                        app, path, _prior,
                        reason=f'Δactionable={_delta}')
                    # Reset the per-session cycle count so the path comes
                    # back fresh after its cooldown rather than carrying a
                    # near-cap counter into the next attempt.
                    cycles[path] = 0
                    # v4.14.6.24-fill-terminal: terminal promotion. When
                    # the zero-progress streak plateaus AND the universe
                    # is empty of fresh candidates, the band is
                    # structurally unfillable from the current eligible
                    # set. Promote to terminal so the ~hourly cooldown
                    # retry stops burning AI calls; wake-check in
                    # `_fill_needed` will resume dispatch the moment a
                    # signal genuinely changes.
                    try:
                        _new_streak = _prior + 1
                        if _new_streak >= _FILL_TERMINAL_AT_STREAK:
                            _peek_cand, _ = _layered_candidate_batch(
                                app, path, limit=1)
                            if not _peek_cand:
                                _set_structurally_short(
                                    app, path,
                                    reason=(
                                        f'Δactionable=0 streak '
                                        f'{_new_streak}, universe '
                                        f'swept'))
                    except Exception:
                        pass
        except Exception:
            pass
        _log_muted(
            app,
            f"[fill-mode] cycle complete ({path}): displayed now "
            f"{nd}/{dt} raw, {_actionable_after}/{dt} actionable, "
            f"bench {nb}/{bf}")
    except Exception as e:
        _log_amber(app, f"[fill-mode] error: {type(e).__name__}: {e}")


def run_one_pass(app) -> None:
    """One full pass: pick the top AI, analyze candidates, housekeep
    existing rows. Exposed publicly (no leading underscore) so audits
    + tests can invoke a single pass deterministically.

    v4.14.3.10 (2026-05-15): pass-rotation across the five paths in
    tm_holdings.PATHS. Each pass picks ONE path off the rotation
    cursor, runs the v4.14.3.9 universe-cursor pass for that path,
    and advances the rotation cursor regardless of pass outcome
    (success, no-candidates, pick-failure, exception). The cursor
    advance lives in a finally block — without that guarantee, a
    path that hits all-cooldown on one cycle would lock the rotation
    on itself forever.

    cfg['analysis_path'] is the USER's view selector and is NEVER
    written by the runner during a pass. The user looking at the
    Moderate Recommend window keeps seeing Moderate; the runner
    rotates independently via cfg['queue_runner_path_cursor']."""
    # v4.14.3.10: read rotation cursor at the START of the pass so the
    # whole pass operates on one path. The finally block at the end
    # advances + persists.
    rotation = _get_path_rotation()
    cursor_idx, rotation_path = _read_rotation_path(app)

    # Mark wall-clock so the next pass knows when this one ran.
    # STAMP AT START, not end — the function has multiple early-return
    # paths (no candidates, pick failure) that would skip an
    # end-of-function stamp, which would make the time-based cadence
    # check fire every poll instead of every interval. May 2026 bug.
    pass_started = datetime.now().isoformat(timespec='seconds')
    try:
        app.cfg['queue_runner_last_pass'] = pass_started
    except Exception as e:
        _log_amber(
            app,
            f"Queue runner: failed to stamp last_pass: "
            f"{type(e).__name__}: {e}")

    try:
        # v4.14.5.14a.10: pass_started MUST be threaded into the body.
        # It is a local of run_one_pass (stamped at start, above), but
        # _run_one_pass_body's BUY-insert path calls _insert_queue_row(
        # ..., pass_started). Before this fix the body referenced an
        # undefined name → every cadence-path BUY raised NameError:
        # name 'pass_started' is not defined and the pick was dropped
        # (2026-05-18 ACGL: a real Mistral BUY lost on the moderate
        # path). Threading the existing value keeps ONE consistent
        # timestamp for both the cfg last_pass stamp and the queue row.
        _run_one_pass_body(app, rotation_path, pass_started)
    finally:
        # v4.14.3.10: ALWAYS advance the cursor — even on exception,
        # no-candidates, or pick-failure. Without this, a path that
        # fails once locks the rotation. The single save_config call
        # below persists both the cursor and last_pass stamp in one
        # filesystem write.
        try:
            next_cursor = (cursor_idx + 1) % len(rotation)
            app.cfg['queue_runner_path_cursor'] = next_cursor
        except Exception as e:
            _log_amber(
                app,
                f"Queue runner: cursor advance write failed "
                f"({type(e).__name__}: {e}); next pass will retry "
                f"the same path")
        try:
            import tired_market as _tm
            _tm.save_config(app.cfg)
        except Exception as e:
            _log_amber(
                app,
                f"Queue runner: save_config after pass failed "
                f"({type(e).__name__}: {e}); cursor advance held "
                f"in memory only")


def _run_one_pass_body(app, rotation_path: str,
                       pass_started: str) -> None:
    """Inner body of run_one_pass. Extracted so the cursor advance
    in run_one_pass's finally block runs regardless of how this body
    terminates (return, raise, etc.).

    v4.14.5.14a.10: `pass_started` (the ISO start-of-pass timestamp
    stamped by run_one_pass) is now an explicit parameter. The
    BUY-insert path calls _insert_queue_row(..., pass_started); when
    the body was extracted from run_one_pass the name was left
    referencing the caller's local, so every cadence-path BUY raised
    `NameError: name 'pass_started' is not defined` and the pick was
    silently dropped. Threading the value keeps one consistent
    timestamp across the cfg stamp and the queue-row insert."""
    # Pass-start heartbeat. Lands BEFORE the pick so a pick-failure
    # branch (which routes through _emit and may not log) still leaves
    # a breadcrumb that the pass started. May 13 2026: Mike's prior
    # session was a black hole for 45 min because all paths from
    # pick-failure suppressed activity-log output.
    _log_muted(app, "Queue runner pass starting...")
    _mark_runner_logged(app)

    # v4.14.3.6 (2026-05-14): make sure the HoldingsWindow + state are
    # built before we hit _analyze_candidate. Pre-v4.14.3.6 the
    # queue runner's first pass on a fresh launch always silently
    # failed all candidates because _holdings_window was None
    # (lazy-built on first user action, which the queue runner can't
    # wait for). _ensure_holdings_window is idempotent + thread-safe
    # via a per-App lock. If construction fails, log amber once and
    # let the pass fall through — _analyze_candidate's drop tracking
    # will still report holdings_window_not_ready and the per-pass
    # summary will fire cleanly.
    try:
        _ensure_fn = getattr(app, '_ensure_holdings_window', None)
        if callable(_ensure_fn):
            built = _ensure_fn()
            if not built:
                _log_amber(
                    app,
                    "Queue runner: HoldingsWindow build failed — "
                    "this pass will skip candidate analysis. Will "
                    "retry on next interval.")
    except Exception as e:
        _log_amber(
            app,
            f"Queue runner: HoldingsWindow ensure raised: "
            f"{type(e).__name__}: {e}")

    # Pick top AI for this pass.
    try:
        import tm_top_ai_picker
        override = app.cfg.get('top_ai_override') or None
        chosen = tm_top_ai_picker.pick_top_ai(app, override=override)
    except Exception as e:
        _log_amber(app, f"Queue runner: top-AI pick failed: {e}")
        # Synthesize a failure dict so the routing below still works.
        chosen = {'success': False, 'reason': 'all_exhausted'}

    # Pick-failure branch — three distinct reasons, three different
    # surface routes (one of them silent).
    # v4.14.3.10: include rotation path in muted log lines so Mike
    # can scan the activity log and see WHICH path got skipped. Do
    # NOT call _record_analysis_outcome here — picker state has
    # nothing to do with ticker freshness; falsely demoting tickers
    # for picker-state problems would poison oldest-first sort when
    # the path becomes runnable again. The cursor still advances
    # via the finally block in run_one_pass so rotation doesn't lock.
    if not chosen.get('success'):
        reason = chosen.get('reason') or 'all_exhausted'
        if reason == 'none_configured':
            # SILENT: the action_prereq intercepts (no_ai_voices_configured)
            # cover the click-time surface. Queue-runner re-surfacing this
            # every 60 seconds would be noisy. Log muted for diagnostic
            # visibility only.
            _log_muted(
                app,
                f"Queue runner ({rotation_path}): no AI configured, "
                f"skipping pass.")
            _set_run_outcome(app, 'no AI configured')
        elif reason == 'all_disabled':
            # May 13 2026: previously emitted system_event ONLY with no
            # activity log breadcrumb. Mike's session went silent for
            # 45+ min while the runner correctly hit this branch every
            # 15 min — surface popups dismiss, log lines stick around.
            _log_muted(
                app,
                f"Queue runner ({rotation_path}): all AI providers "
                f"disabled — surfacing.")
            _emit(app, 'all_providers_disabled')
            _set_run_outcome(app, 'all providers disabled')
        elif reason == 'all_exhausted':
            # See note above on all_disabled.
            _log_muted(
                app,
                f"Queue runner ({rotation_path}): all AI providers "
                f"out of budget today — surfacing.")
            _emit(app, 'all_ais_budget_exhausted')
            _set_run_outcome(app, 'all AIs out of budget')
        elif reason == 'all_cooldown':
            # v4.14.3.5 (2026-05-14): every configured AI is in a
            # short rate-limit cooldown (typically 5 min after a 429).
            # Distinct from all_exhausted (daily quota) and
            # all_disabled (toggled off). Surface fires once per
            # entry (5-min Teacher AI cooldown), log line per pass
            # so the user sees we noticed and we're waiting.
            cooldown_sec = int(
                chosen.get('cooldown_remaining_sec', 0) or 0)
            _log_muted(
                app,
                f"Queue runner ({rotation_path}): all AI providers "
                f"in cooldown — shortest clears in ~{cooldown_sec}s. "
                f"Surfacing.")
            _emit(
                app, 'all_ais_in_cooldown',
                context={'cooldown_remaining_sec': cooldown_sec})
            _set_run_outcome(
                app,
                f"all AIs cooled down "
                f"({cooldown_sec}s remaining)")
        else:
            # Defensive: shouldn't happen, but log so we notice if
            # a new reason value gets added without queue-runner
            # routing.
            _log_amber(
                app,
                f"Queue runner: unexpected picker reason '{reason}'")
        # Run housekeeping anyway — graduations + invalidations
        # don't need an AI.
        try:
            _run_housekeeping(app)
        except Exception as e:
            _log_amber(app, f"Queue runner housekeeping error: {e}")
        return

    # Success path. Fallback-from-override means the override was set
    # but couldn't be used. Emit the right system_event for transparency.
    if chosen.get('fallback_from'):
        _emit(app, 'override_ai_budget_exhausted',
              context={'provider': chosen.get('fallback_from',
                                                 'override')})

    # Build the candidate shortlist for the rotated path.
    # v4.14.3.10: _build_candidate_shortlist takes path as an explicit
    # argument now (was: implicit cfg['analysis_path'] read).
    # v4.14.5.14-cadence-pool-fix: restrict the cadence shortlist to this
    # path's candidate pool (the SAME population _eligible_paths_for
    # requires at dispatch) so the runner stops shortlisting non-pool
    # tickers that always gate out and never advance the cursor. Mirrors
    # the fill-mode tier-3 call's restrict=pool. Fail-open to None.
    candidates = _build_candidate_shortlist(
        app, rotation_path,
        restrict=_path_candidate_pool(app, rotation_path))
    if not candidates:
        _emit_summary_log(
            app, chosen, candidate_count=0, inserted=0,
            outcome='no_candidates', path=rotation_path)
        _set_run_outcome(
            app, f'no candidates in cache ({rotation_path})')
        # Still run housekeeping.
        try:
            _run_housekeeping(app)
        except Exception as e:
            _log_amber(app, f"Queue runner housekeeping error: {e}")
        return

    # v4.14.5.14a.11: path-INDEPENDENT verdict-recency gate. The a.9
    # gate lived only in fill mode (_recently_judged_set called from
    # _layered_candidate_batch -> _run_fill_mode, the EVENT-DRIVEN
    # branch). When the runner is on the CADENCE branch (this function;
    # entered when cfg['event_driven_refresh'] is off/absent) the
    # 2026-05-18 Sunday burn pattern ran unchecked — ENPH-137x. Apply
    # the SAME _recently_judged_set predicate/window here so cadence
    # dispatch also skips tickers whose recent verdict for this path is
    # still valid. Reuses the existing cfg['use_verdict_recency_skip']
    # flag (default True; _recently_judged_set checks it internally and
    # fail-OPENs to an empty set on flag-off / no-DB / error) so the
    # behaviour is identical regardless of which branch runs — the
    # whole point of this patch. Event-driven TRIGGERS are unaffected:
    # they dispatch via run_one_pass_for_triggers and never reach
    # _run_one_pass_body, so a real news/earnings/price/target/stop/
    # user/suspicion event on a recently-judged ticker still goes
    # through. Belt-and-suspenders try/except so the gate can never
    # wedge the cadence path (in addition to _recently_judged_set's
    # own internal fail-open).
    try:
        _vr_skip = _recently_judged_set(app, rotation_path)
    except Exception as e:
        _vr_skip = set()
        _log_amber(
            app,
            f"Queue runner ({rotation_path}): verdict-recency gate "
            f"errored (proceeding UNGATED this pass): "
            f"{type(e).__name__}: {e}")
    if _vr_skip:
        _vr_before = len(candidates)
        candidates = [c for c in candidates
                      if str(c).upper() not in _vr_skip]
        _vr_n = _vr_before - len(candidates)
        if _vr_n > 0:
            _log_muted(
                app,
                f"Queue runner ({rotation_path}): {_vr_n} "
                f"candidate(s) skipped — recent verdict still valid "
                f"(verdict-recency gate, cadence path).")
    if not candidates:
        _log_muted(
            app,
            f"Queue runner ({rotation_path}): nothing to analyze "
            f"this cycle — all shortlisted candidates have a recent "
            f"valid verdict. The v4.14.5.14a.11 verdict-recency gate "
            f"is working on the cadence path. Idle until a verdict "
            f"ages out or an event fires.")
        _emit_summary_log(
            app, chosen, candidate_count=0, inserted=0,
            outcome='no_candidates', path=rotation_path)
        _set_run_outcome(
            app, f'all candidates recently judged ({rotation_path})')
        try:
            _run_housekeeping(app)
        except Exception as e:
            _log_amber(app, f"Queue runner housekeeping error: {e}")
        return

    # v4.14.5.14a.14: price-band eligibility gate on the CADENCE path
    # too (same as the trigger path), so a ticker outside this path's
    # pool price band is never sent to the AI just to come back
    # "wrong price band". Fail-open inside the helper.
    candidates = _eligibility_price_band_filter(
        app, rotation_path, candidates, 'cadence')
    if not candidates:
        _log_muted(
            app,
            f"Queue runner ({rotation_path}): nothing to analyze "
            f"this cycle — all shortlisted candidates were outside "
            f"this path's price band (v4.14.5.14a.14 eligibility "
            f"gate).")
        _emit_summary_log(
            app, chosen, candidate_count=0, inserted=0,
            outcome='no_candidates', path=rotation_path)
        _set_run_outcome(
            app, f'all candidates off price band ({rotation_path})')
        try:
            _run_housekeeping(app)
        except Exception as e:
            _log_amber(app, f"Queue runner housekeeping error: {e}")
        return

    # Analysis pass.
    path = rotation_path
    inserted = 0
    # v4.14.3.1 hotfix 2026-05-14: count candidates that returned
    # `pred is None` so we can emit one summary line at end-of-pass.
    # Without this, a stretch where the picked AI is in cooldown
    # produces 20 silent skips and the runner looks broken.
    # v4.14.3.6 (2026-05-14): pre-AI drop tracking. _analyze_candidate
    # mutates drop_reasons whenever it returns None for a reason that
    # ISN'T an AI failure (HoldingsWindow not built, prompt-build
    # exception, empty prompt). End-of-pass summary lists each reason
    # with count so the cause is always visible. Pre-v4.14.3.6 these
    # were silent and the amber line's "common cause: cooldown" hint
    # was misleading after the v4.14.3.5 picker-cooldown fix.
    silent_failures = 0
    skipped = 0  # v4.14.5.14a.3: PROVIDER_UNAVAILABLE (no provider)
    drop_reasons: dict = {}
    # v4.14.3.11 (2026-05-15): open the router's scan-run window so
    # the per-candidate dispatch uses the multi-provider rotation
    # mode (v4.14.1.1's next_scan_canonical_pick round-robin across
    # canonical models, with the router's sticky-pick + retry +
    # failover within each model). Mirrors tm_holdings.py:3933-3988's
    # Discover-scan pattern. Wrapped in try/finally below so
    # end_scan_run fires on every termination path - success,
    # no-candidates, pick-failure, exception. Without the finally,
    # RouterRun state would leak across passes.
    try:
        import tm_ai_router as _router_for_scan
        _router_for_scan.begin_scan_run()
    except Exception as e:
        # If the router can't open the window, distribution mode
        # just doesn't activate - the call below to
        # run_apis_for_scan_prediction will fall through to the
        # legacy multi-provider-fan-out path (one prediction per
        # provider serving each canonical model). Surface so we
        # notice if this starts firing in production.
        _log_amber(
            app,
            f"Queue runner: begin_scan_run failed "
            f"({type(e).__name__}: {e}); distribution mode inactive "
            f"for this pass")

    # v4.14.3.11: announce distribution mode once per pass. The
    # router itself logs a similar line ('Scan single-provider mode:
    # rotating across N canonical model(s)') the first time it
    # dispatches under is_scan_run_active(). This runner-side
    # announcement fires earlier and includes the path so Mike sees
    # the rotation start on the same line as the pass-start
    # heartbeat.
    _log_muted(
        app,
        f"Queue runner ({rotation_path}): dispatching with router "
        f"rotation across eligible providers.")

    # v4.14.3.11: per-pass provider-mix tally. Keyed by the display
    # label (pred['model'] for cloud calls, chosen.display_name for
    # local Ollama). Reported in the end-of-pass summary so the user
    # can see which providers actually served the work.
    provider_calls: dict = {}
    _ustats: dict = {}  # v4.14.5.14c-p2 unified-dispatch tallies

    try:
        for ticker in candidates:
            if _stop_set(app):
                return

            # v4.14.5.14c-p2: one-ticker-all-paths (cadence). INERT
            # unless the flag is on; None → fall through to today's
            # per-(ticker,path) path, byte-identical.
            _ud = _unified_dispatch_ticker(
                app, chosen, ticker, path, pass_started,
                _ustats, 'Queue runner')
            if _ud in ('done', 'skip'):
                continue

            try:
                pred = _analyze_candidate(
                    app, chosen, ticker, path,
                    drop_reasons=drop_reasons)
            except Exception as e:
                _log_muted(
                    app,
                    f"Queue runner: {ticker} analysis failed: "
                    f"{type(e).__name__}")
                # v4.14.3.9: record the analysis attempt so this
                # ticker doesn't cycle back to the top of the queue
                # next pass.
                _record_analysis_outcome(app, ticker, path, 'failed')
                continue
            if pred is PROVIDER_UNAVAILABLE:
                # v4.14.5.14a.2: no provider available — skip without
                # advancing the cursor (ticker retried when providers
                # recover; prevents the identical-NO_CALL loop).
                # v4.14.5.14a.3: count as skipped (NOT analyzed).
                skipped += 1
                _log_muted(
                    app,
                    f"[fill-mode] skipped {ticker} — no eligible "
                    f"providers (will retry when available)")
                continue
            if pred is None:
                silent_failures += 1
                _record_analysis_outcome(app, ticker, path, 'failed')
                continue
            # v4.14.3.9: record outcome BEFORE the BUY-gate so WATCH/
            # AVOID results (the dominant case in a large universe)
            # also get marked analyzed. Pre-v4.14.3.9 only BUY rows
            # entered recommend_queue, leaving WATCH/AVOID untracked
            # and causing the alphabetical first-20 cycle. Outcome
            # string uses the raw direction (BUY / WATCH / AVOID)
            # when present, falling back to 'NO_CALL' for the unusual
            # case where the AI returned a structured response
            # without a direction.
            direction = (pred.get('direction') or '').upper()
            outcome = direction if direction else 'NO_CALL'
            _record_analysis_outcome(app, ticker, path, outcome)

            # v4.14.3.11: tally provider that actually served this
            # call. pred['model'] is the display label written by
            # tm_api_providers.run_apis_for_scan_prediction (see
            # base_extra at tm_api_providers.py:1650-1677). For local
            # Ollama responses, pred['model'] is the canonical model
            # name (e.g. 'qwen2.5:14b'); fall back to chosen's display
            # name when unset.
            _provider_label = (
                pred.get('model')
                or chosen.get('display_name')
                or chosen.get('id')
                or '?')
            provider_calls[_provider_label] = (
                provider_calls.get(_provider_label, 0) + 1)

            if direction != 'BUY':
                continue
            # Insert (or update) the row.
            try:
                _insert_queue_row(app, ticker, path, pred, chosen,
                                  pass_started)
                inserted += 1
            except Exception as e:
                _log_muted(
                    app,
                    f"Queue runner: insert failed for {ticker}: {e}")
    finally:
        # v4.14.3.11: ALWAYS close the scan-run window. Even on
        # uncaught exception inside the loop, leaving the window
        # open would dedup router skip lines across the NEXT pass
        # (RouterRun's SkipLogDedup state survives until end_scan_run
        # is called).
        try:
            import tm_ai_router as _router_end
            _router_end.end_scan_run()
        except Exception as e:
            _log_amber(
                app,
                f"Queue runner: end_scan_run failed "
                f"({type(e).__name__}: {e}); router state may leak "
                f"to next pass")

    # v4.14.3.6 (2026-05-14): end-of-pass DROP summary. Reports
    # candidates that returned None BEFORE the AI call was even
    # attempted (HoldingsWindow not built, prompt construction
    # failed, etc.). Fires before the silent-failure summary so the
    # activity log reads in order: pre-call drops → AI-call silent
    # failures → headline outcome.
    if drop_reasons:
        # Compose "20x holdings_window_not_ready, 3x empty_prompt"
        # form, sorted by count descending so the dominant cause is
        # first.
        items = sorted(
            drop_reasons.items(),
            key=lambda kv: (-kv[1], kv[0]))
        bits = [f"{count}x {reason}" for (reason, count) in items]
        total_drops = sum(drop_reasons.values())
        _log_amber(
            app,
            f"Queue runner: {total_drops}/{len(candidates)} "
            f"candidates dropped before AI call — "
            + ", ".join(bits))

    # End-of-pass silent-failure summary. One amber line, before the
    # pass summary, so the activity log reads in order: diagnostic
    # explanation → headline outcome.
    # v4.14.3.6: copy rewrite. The old "Common cause: provider
    # cooldown after a 429" hint was authored when cooldown was the
    # dominant cause (v4.14.3.1). After the v4.14.3.5 picker-cooldown
    # fix the hint became misleading. New copy points the user at the
    # log lines that now explain the actual cause.
    if silent_failures > 0:
        _log_amber(
            app,
            f"Queue runner: provider returned no prediction for "
            f"{silent_failures}/{len(candidates)} candidates this "
            f"pass. (See preceding log lines for per-candidate "
            f"reason.)")

    # v4.14.5.14c-p2: fold unified-dispatch tallies (all zeros when
    # the flag is off → summary identical to pre-patch).
    inserted += int(_ustats.get('inserted', 0) or 0)
    skipped += int(_ustats.get('skipped', 0) or 0)
    silent_failures += int(_ustats.get('silent', 0) or 0)
    for _k, _v in (_ustats.get('provider_calls', {}) or {}).items():
        provider_calls[_k] = provider_calls.get(_k, 0) + _v

    _emit_summary_log(
        app, chosen, candidate_count=len(candidates),
        inserted=inserted, outcome='success', path=rotation_path,
        provider_calls=provider_calls, skipped=skipped,
        gated=sum(drop_reasons.values()))
    _set_run_outcome(
        app,
        f"success ({rotation_path}: {inserted} new pick"
        f"{'' if inserted == 1 else 's'})")

    # Trim the active queue if it exceeded the cap.
    try:
        _trim_active_to_cap(app)
    except Exception as e:
        _log_amber(app, f"Queue runner trim error: {e}")

    # Housekeeping: graduations + invalidations on all active rows.
    try:
        _run_housekeeping(app)
    except Exception as e:
        _log_amber(app, f"Queue runner housekeeping error: {e}")

    # NOTE: cadence stamp lives at the START of run_one_pass, not here.
    # See the audit testing note at the top of this file.


# ─── Cadence + pause helpers ─────────────────────────────────────────

def _should_run_pass(app, last_auto_refresh_seen: str) -> tuple:
    """Returns (run_now: bool, new_last_auto_refresh_seen: str).
    Hybrid cadence: True when the configured interval has elapsed
    since the last pass OR when the auto-refresh tick has advanced
    (event-driven trigger)."""
    cfg = app.cfg
    now = datetime.now()

    # Event-driven: auto-refresh tick advanced.
    auto_refresh_last = str(cfg.get('auto_refresh_last_run', ''))
    if (auto_refresh_last and last_auto_refresh_seen
            and auto_refresh_last != last_auto_refresh_seen):
        return (True, auto_refresh_last)
    if not last_auto_refresh_seen:
        # First poll — initialize so we don't fire immediately on
        # whatever was already in cfg.
        last_auto_refresh_seen = auto_refresh_last

    # Time-based: interval elapsed since last queue pass.
    interval_min = int(cfg.get('queue_runner_interval_min', 15))
    last_pass = cfg.get('queue_runner_last_pass', '')
    if not last_pass:
        # Never run — fire immediately.
        return (True, last_auto_refresh_seen)
    try:
        last_dt = datetime.fromisoformat(last_pass)
    except Exception:
        return (True, last_auto_refresh_seen)
    if now - last_dt >= timedelta(minutes=interval_min):
        return (True, last_auto_refresh_seen)

    return (False, last_auto_refresh_seen)


def _is_paused(app) -> bool:
    """Returns True if any pause condition blocks the queue runner.
    Mirrors _auto_refresh_tick's pause checks."""
    # AI globally paused (user, UPS, gaming).
    try:
        import tm_holdings
        if tm_holdings.is_ai_paused():
            return True
    except Exception:
        pass
    # Active scan (Discover or Consensus) — don't compete.
    try:
        hw = getattr(app, '_holdings_window', None)
        if hw is not None:
            if (getattr(hw, '_discover_running', False)
                    or getattr(hw, '_consensus_running', False)):
                return True
    except Exception:
        pass
    # Yfinance cooldown active.
    try:
        import tm_discover
        cd = tm_discover.get_cooldown_status() or {}
        if cd.get('active'):
            return True
    except Exception:
        pass
    return False


def _stop_set(app) -> bool:
    ev = getattr(app, '_queue_runner_stop', None)
    return ev is not None and ev.is_set()


# v4.14.5.62-lookup-backoff: failsafe ceiling. Even if a lookup's clear is
# somehow missed (async cancel/leak), the fill loop treats the "lookup active"
# mark as stale after this many seconds and resumes — so fill can NEVER be
# paused forever. Set well above a normal lookup's worst-case (~60s).
_LOOKUP_BACKOFF_MAX_SEC = 120.0


def _lookup_backoff_active(app) -> bool:
    """v4.14.5.62-lookup-backoff: True iff a user lookup is in flight AND the
    backoff feature is on — meaning the background fill should yield provider
    slots this cycle. Flag-gated (cfg['lookup_priority_backoff'], read live);
    counter-based (handles overlapping lookups); timestamp-failsafed (a stuck
    counter older than _LOOKUP_BACKOFF_MAX_SEC is ignored). Never raises →
    False (fill behaves exactly as today on any fault)."""
    try:
        if not bool((getattr(app, 'cfg', {}) or {}).get(
                'lookup_priority_backoff', False)):
            return False
        lk = getattr(app, '_lookup_backoff_lock', None)
        if lk is None:
            return False
        with lk:
            cnt = int(getattr(app, '_lookup_backoff_count', 0) or 0)
            last = float(getattr(app, '_lookup_backoff_last_begin', 0.0) or 0.0)
        if cnt <= 0:
            return False
        return (time.time() - last) < _LOOKUP_BACKOFF_MAX_SEC
    except Exception:
        return False


def _has_accepted_disclaimer(app) -> bool:
    """Mirror tired_market._has_accepted_disclaimer without importing
    (avoid circular import at module load)."""
    try:
        import tired_market as _tm
        return bool(_tm._has_accepted_disclaimer())
    except Exception:
        return True  # If we can't check, don't gate forever.


# ─── Candidate shortlist + analysis ───────────────────────────────────

def _build_candidate_shortlist(app, path: str, exclude=None,
                                limit=None, restrict=None) -> list:
    """Pull a candidate ticker list ordered by analysis staleness for
    the given path. v4.14.3.9 universe cursor; v4.14.3.10 made path
    an explicit argument (was: implicit cfg['analysis_path'] read).

    Universe source unchanged from v4.14.3.6: tm_cache.get_scope_tickers
    (no Choices filter — that's a display-time concern) intersected
    with get_unfilled_tickers's complement (tickers WITH cached
    daily_bars data).

    v4.14.3.9 change is the ordering: instead of alphabetical-first-N
    (which left 2,470 of 2,490 ITOT tickers untouched because non-BUY
    predictions never entered the recommend_queue exclusion set),
    sort by last_analyzed_at from queue_runner_analysis_log. Never-
    analyzed tickers (NULL last_analyzed_at) float to the top; among
    analyzed tickers, the oldest analysis comes first. Alphabetical
    ticker is the deterministic tiebreak. With a 20-cap and 15-min
    cadence, ITOT cycles in ~125 passes (~31 hours).

    Storage shape is path-aware from day one (PRIMARY KEY (ticker,
    path) on queue_runner_analysis_log) so v4.14.3.10's path-aware
    allocation work inherits the schema without migration. Runtime in
    v4.14.3.9 still only operates on cfg['analysis_path'].

    Exclusions (unchanged): active recommend_queue picks + 24h AVOID
    cooldown rows.

    Failure modes: any tm_cache failure logs amber and returns empty.
    A read failure on queue_runner_analysis_log falls back to
    alphabetical sort (matches v4.14.3.8 behavior) with an amber log —
    a fresh install with an empty table is NOT a failure (NULL ts
    sentinel naturally puts everything at the top of the queue)."""
    try:
        import tm_cache
    except Exception as e:
        _log_amber(
            app,
            f"Queue runner: tm_cache import failed: "
            f"{type(e).__name__}: {e}")
        return []

    # Phase 2 fix: do NOT pass user Choices to get_scope_tickers. The
    # Choices price-range filter is a *display-time* concern handled by
    # the Recommend window's filter row. At the candidate-selection
    # stage, we want the full universe of tickers with cached
    # daily_bars data — let the AI analyze them and return BUY/WATCH/
    # AVOID, then let Recommend filter the displayed picks.
    try:
        scope = tm_cache.get_scope_tickers('daily_bars', None)
    except Exception as e:
        _log_amber(
            app,
            f"Queue runner: get_scope_tickers failed: "
            f"{type(e).__name__}: {e}")
        return []
    if not scope:
        return []

    try:
        unfilled = tm_cache.get_unfilled_tickers('daily_bars', scope)
    except Exception as e:
        _log_amber(
            app,
            f"Queue runner: get_unfilled_tickers failed: "
            f"{type(e).__name__}: {e}")
        return []

    filled = scope - unfilled
    if not filled:
        return []

    # Exclusions: active picks + 24h AVOID cooldown.
    conn = _conn(app)
    with _db_lock(app):
        excluded = set()
        if conn is not None:
            try:
                cur = conn.execute(
                    "SELECT DISTINCT ticker FROM recommend_queue "
                    "WHERE status = 'active'")
                excluded.update(r[0] for r in cur.fetchall())
            except Exception as e:
                _log_amber(
                    app,
                    f"Queue runner: active-queue exclusion query failed: "
                    f"{type(e).__name__}: {e}")
            try:
                cooldown_h = int(app.cfg.get(
                    'queue_avoid_cooldown_hours', 24))
                cutoff = (datetime.now()
                           - timedelta(hours=cooldown_h)).isoformat()
                cur = conn.execute(
                    "SELECT DISTINCT ticker FROM recommend_queue "
                    "WHERE status = 'verified_avoid' "
                    "AND last_seen_at > ?",
                    (cutoff,))
                excluded.update(r[0] for r in cur.fetchall())
            except Exception as e:
                _log_amber(
                    app,
                    f"Queue runner: AVOID-cooldown exclusion query failed: "
                    f"{type(e).__name__}: {e}")

        # v4.14.5.14a.1: caller-supplied exclusion (tickers already chosen
        # by a higher tier in the layered selector). Default None = legacy
        # behaviour unchanged for run_one_pass's cadence-path caller.
        extra_excl = {str(t).upper() for t in (exclude or set())}
        # v4.14.5.14a.6: restrict to this path's candidate pool when one
        # is supplied (Tier 3 must draw from the path-appropriate pool,
        # not the whole universe). None/empty = no restriction (safe
        # fallback — a pool fault never blocks fill).
        restrict_set = ({str(t).upper() for t in restrict}
                        if restrict else None)
        candidate_pool = [
            t for t in filled
            if t not in excluded and t not in extra_excl
            and (restrict_set is None or t in restrict_set)]
        if not candidate_pool:
            return []

        # v4.14.3.9 cursor: pull last_analyzed_at for the supplied path.
        # v4.14.3.10: path is now an explicit argument from the rotation
        # cursor, not a cfg read. cfg['analysis_path'] is the user's view
        # selector and stays untouched by the runner.
        staleness: dict = {}
        if conn is not None:
            try:
                cur = conn.execute(
                    "SELECT ticker, last_analyzed_at "
                    "FROM queue_runner_analysis_log "
                    "WHERE path = ?",
                    (path,))
                for row in cur.fetchall():
                    staleness[row[0]] = row[1]
            except Exception as e:
                _log_amber(
                    app,
                    f"Queue runner: analysis-log read failed (falling "
                    f"back to alphabetical order): "
                    f"{type(e).__name__}: {e}")
                staleness = {}

        # Sort key:
        #   Tier 0 = never-analyzed (no row in queue_runner_analysis_log
        #            for this (ticker, path))  → top of queue
        #   Tier 1 = analyzed       → ordered by last_analyzed_at ASC
        # Ticker is the deterministic alphabetical tiebreak in both tiers.
        def _sort_key(ticker: str):
            ts = staleness.get(ticker)
            if ts is None:
                return (0, 0, ticker)
            try:
                return (1, int(ts), ticker)
            except (TypeError, ValueError):
                # Defensive: malformed row, treat as never-analyzed so it
                # surfaces and gets re-analyzed (overwriting the bad row).
                return (0, 0, ticker)

        candidate_pool.sort(key=_sort_key)
        # v4.14.5.14-layer3-replace: drop (ticker, path) pairs Layer 3
        # recently dropped where Layer 1 hasn't changed its mind. Gated by
        # cfg['use_layer3_replace'] (default False → no-op). Inside the
        # shared helper so it covers BOTH callers (cadence _run_one_pass_body
        # AND fill-mode tier 3) automatically. Filters the full pool BEFORE
        # the cap slice so suppressed tickers don't consume cap slots. Fail-
        # open: any error leaves the list intact (never wedges selection).
        try:
            if bool((getattr(app, 'cfg', {}) or {}).get(
                    'use_layer3_replace', False)):
                import tm_layer3_replace as _l3
                candidate_pool = [
                    t for t in candidate_pool
                    if not _l3._is_suppressed_by_cooldown(app, t, path)]
        except Exception:
            pass
        if limit is not None:
            cap = int(limit)
        else:
            cap = int(app.cfg.get(
                'queue_runner_max_candidates_per_pass', 20))

        # v4.14.5.92-sweep-cursor: windowed positional cursor for the
        # never-analyzed (Tier-0) portion. Pre-patch behaviour: take the
        # alphabetically-first `cap` never-analyzed tickers — which under
        # provider saturation re-presents the same front-of-alphabet
        # every cycle, because PROVIDER_UNAVAILABLE failures never get a
        # row in queue_runner_analysis_log and stay Tier-0 forever. The
        # 3-hour live observation: never reached U-V-W-X-Y-Z. Windowed
        # cursor replaces the alphabetical front-slice with a moving
        # window over the SAME stably-sorted Tier-0 list, advanced by
        # `cap` per dispatch (NOT per completion — the whole point is
        # that provider-saturated cycles still march forward; failures
        # are retried next lap when the window wraps). Tier-1 (analyzed)
        # ordering is UNCHANGED — re-analysis cadence still resurfaces
        # by oldest staleness as today. Pattern mirrors the existing
        # T3 universe sweep in tm_news_fetcher (50-h universe lap), now
        # applied to AI candidate selection. Flag-gated by
        # cfg['use_sweep_cursor'] (default True); off → exact pre-patch
        # alphabetical front-slice for instant rollback.
        try:
            _use_sweep = bool((getattr(app, 'cfg', {}) or {}).get(
                'use_sweep_cursor', True))
        except Exception:
            _use_sweep = True
        if not _use_sweep:
            return candidate_pool[:cap]
        # Partition the already-sorted candidate_pool into Tier-0
        # (never-analyzed: alphabetical sub-order — what we window over)
        # and Tier-1 (analyzed: oldest-staleness-first, used as
        # fall-through when Tier-0 is short of `cap`).
        tier0 = [t for t in candidate_pool if staleness.get(t) is None]
        tier1 = [t for t in candidate_pool if staleness.get(t) is not None]
        if not tier0:
            # Pool is fully covered at least once — fall through to the
            # Tier-1 staleness re-cycle exactly as today.
            return candidate_pool[:cap]
        pool_size = len(tier0)
        offsets_map = (app.cfg.get('fill_universe_offset')
                       if isinstance(app.cfg.get('fill_universe_offset'),
                                     dict) else None)
        if offsets_map is None:
            offsets_map = {}
            app.cfg['fill_universe_offset'] = offsets_map
        # Clamp on read — pool size changes hourly when the momentum
        # pool refreshes; an offset past the end just wraps to the
        # start. Negative / non-int values defensively reset to 0.
        try:
            raw_offset = int(offsets_map.get(path, 0) or 0)
            if raw_offset < 0:
                raw_offset = 0
            offset = raw_offset % pool_size
        except Exception:
            offset = 0
        take_n = min(cap, pool_size)
        if offset + take_n <= pool_size:
            windowed = tier0[offset:offset + take_n]
        else:
            tail = tier0[offset:]
            head = tier0[:take_n - len(tail)]
            windowed = tail + head
        # Advance ON DISPATCH (these are about to be analysed — the
        # caller dispatches the returned list). Persist to cfg so the
        # offset survives restarts, mirroring queue_runner_path_cursor.
        new_offset = (offset + take_n) % pool_size
        offsets_map[path] = new_offset
        try:
            import tired_market as _tm_sweep
            _tm_sweep.save_config(app.cfg)
        except Exception:
            pass  # in-memory advance still holds for this session
        selection = list(windowed)
        # Fall-through: if the Tier-0 window came up short (small pool),
        # top up from Tier-1 staleness — keeps cap honored.
        if len(selection) < cap and tier1:
            for tk in tier1:
                if tk in selection:
                    continue
                selection.append(tk)
                if len(selection) >= cap:
                    break
        return selection[:cap]


def _record_analysis_outcome(app, ticker: str, path: str,
                              outcome: str) -> None:
    """v4.14.3.9: UPSERT into queue_runner_analysis_log so the next
    pass's _build_candidate_shortlist orders this (ticker, path) at
    the bottom of the queue.

    Records EVERY processed candidate regardless of outcome — BUY /
    WATCH / AVOID / NO_CALL / failed. The point of the timestamp is
    'we LOOKED at this ticker, move on' — not 'we successfully
    predicted it.' If we only recorded successes, failed tickers
    would cycle back to the top of the queue every pass and we'd re-
    burn budget on them.

    Best-effort: failures log amber and the pass continues. A bad
    write never blocks the rest of the pass."""
    conn = _conn(app)
    if conn is None:
        return
    with _db_lock(app):
        now_ts = int(time.time())
        try:
            conn.execute(
                "INSERT INTO queue_runner_analysis_log "
                "(ticker, path, last_analyzed_at, last_outcome) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(ticker, path) DO UPDATE SET "
                "last_analyzed_at = excluded.last_analyzed_at, "
                "last_outcome = excluded.last_outcome",
                (ticker, path, now_ts, outcome))
            conn.commit()
        except Exception as e:
            _log_amber(
                app,
                f"Queue runner: analysis-log UPSERT failed for "
                f"{ticker}/{path}: {type(e).__name__}: {e}")


def _analyze_candidate(app, chosen: dict, ticker: str,
                       path: str,
                       drop_reasons: Optional[dict] = None,
                       scan_provider_filter: Optional[str] = None
                       ) -> Optional[dict]:
    """Run the top-AI against one candidate. Returns the parsed
    prediction dict (with 'direction', 'target', 'stop', etc.) or
    None on any failure. Doesn't write predictions_log — Phase 1
    keeps the queue separate from the predictions log.

    v4.14.3.6 (2026-05-14): `drop_reasons` is an optional mutable dict
    the caller passes in to track WHY candidates returned None across
    a pass. Keys are short reason codes (e.g. 'holdings_window_not_ready',
    'empty_prompt', 'prompt_build_failed:TypeError'); values are counts.
    Caller emits one summary log at end-of-pass listing the counts.
    Pre-v4.14.3.6 these three None-return paths were silent — every
    candidate looked identical to a real AI failure even when the cause
    was actually upstream of the AI call entirely. See the May 14
    silent-failures investigation.

    v4.14.6.31: optional algorithmic tier-1 gate. The algo score + the
    promote/skip decision are ALWAYS computed when shadow mode is on
    (default) so the user can compare what the algo would have done
    vs. what the AI did over a few days of normal use. When
    cfg['use_algorithmic_tier1'] is True AND the algo says skip, this
    function returns None BEFORE the per-candidate AI call — that's
    where the rate-limit relief comes from. When the algo says promote
    or the flag is off, the AI path runs as today. See
    tm_algo_score.py for the scoring contract.
    """
    if drop_reasons is None:
        drop_reasons = {}

    def _bump(reason: str):
        drop_reasons[reason] = drop_reasons.get(reason, 0) + 1

    state = getattr(app, '_holdings_state', None) or {}
    hw = getattr(app, '_holdings_window', None)
    if hw is None:
        _bump('holdings_window_not_ready')
        return None

    # ── v4.14.6.31: algorithmic tier-1 gate (shadow + optionally live) ──
    cfg = getattr(app, 'cfg', None) or {}
    algo_live = bool(cfg.get('use_algorithmic_tier1', False))
    algo_shadow = bool(cfg.get('algo_tier1_shadow', True))
    algo_decision = None  # populated by the gate when it runs
    if algo_live or algo_shadow:
        try:
            algo_decision = _algo_gate_decide(app, chosen, ticker, path, cfg)
        except Exception as e:
            # Algo failure must NEVER break tier-1. Fall back to AI path.
            algo_decision = {
                'error': f'{type(e).__name__}: {e}',
                'algo_would_promote': True,  # safe-fail: don't drop the candidate
                'score': None, 'reasons': [],
            }
        # LIVE-mode action: drop the candidate before the AI call when
        # the algo says skip. Shadow mode never acts — only logs.
        if algo_live and not algo_decision.get('algo_would_promote', True):
            _bump('algo_tier1_skip')
            _algo_log_shadow(app, ticker, path, algo_decision,
                              ai_ran=False, ai_prediction=None)
            return None

    # Build the candidate prompt via the existing helper.
    try:
        prompt = hw._build_candidate_prompt(ticker, '', path)
    except Exception as e:
        _bump(f"prompt_build_failed:{type(e).__name__}:{e}")
        return None
    if not prompt:
        _bump('empty_prompt')
        return None

    # v4.14.5.14-ollama-purge-3a: the kind=='local' dispatch branch was
    # removed — the picker is cloud-only (2c), so chosen is always cloud.
    # v4.14.5.62-concurrent-scan: scan_provider_filter pins this call to one
    # provider (a concurrent worker passes its own provider id); None keeps
    # the full router rotation (the sequential default — unchanged).
    pred = _run_cloud_one(app, prompt, ticker, path, chosen,
                          scan_provider_filter=scan_provider_filter)
    # v4.14.6.31: shadow log — capture both decisions for A/B review.
    # Skipped when the algo gate didn't run (feature + shadow both off).
    if algo_decision is not None:
        try:
            _algo_log_shadow(app, ticker, path, algo_decision,
                              ai_ran=True, ai_prediction=pred)
        except Exception:
            pass  # logging must never affect dispatch
    return pred


# ─── v4.14.6.31 — algorithmic tier-1 helpers ──────────────────────────

def _algo_gate_decide(app, chosen: dict, ticker: str, path: str,
                      cfg: dict) -> dict:
    """Compute the algo score + would-promote decision for one
    candidate. Pure read; no side effects on app state. Returns:

        {
          'score': float | None,
          'reasons': list[str],
          'algo_would_promote': bool,
          'gate_reason': str,
          'event_triggered': bool,
          'threshold': float,
        }
    """
    import tm_algo_score as _algo
    threshold = float(cfg.get('algo_tier1_threshold', 65.0))
    trigger_bypass = bool(cfg.get('algo_tier1_trigger_bypass', True))

    # Pull features. `_holdings_window.cache` is the same cache the
    # candidate-prompt builder reads from, so this is consistent with
    # what the AI is being shown.
    hw = getattr(app, '_holdings_window', None)
    cache = getattr(hw, 'cache', None) if hw is not None else None
    raw_tech = None
    raw_news = None
    if cache is not None:
        try:
            raw_tech = cache.technicals(ticker) or None
        except Exception:
            raw_tech = None
        try:
            raw_news = cache.news_features(ticker) or None
        except Exception:
            raw_news = None

    feats = _algo.normalize_features(raw_tech, raw_news)
    score, reasons = _algo.score_for_promotion(feats)

    # Event-triggered: best-effort heuristic. The candidate dict
    # carries the trigger 'kind' when it originated from the
    # event-driven path; universe-sweep candidates do not. Defaults
    # safely to False (algo gates on score in that case).
    kind = (chosen or {}).get('kind') or (chosen or {}).get('fire_kind')
    event_triggered = bool(kind) and str(kind).lower() not in (
        'universe', 'sweep', '', 'staleness')

    promote, gate_reason = _algo.should_promote(
        score, threshold, event_triggered, trigger_bypass)
    return {
        'score': score,
        'reasons': reasons,
        'algo_would_promote': promote,
        'gate_reason': gate_reason,
        'event_triggered': event_triggered,
        'threshold': threshold,
    }


def _algo_log_shadow(app, ticker: str, path: str, decision: dict,
                      ai_ran: bool,
                      ai_prediction: Optional[dict]) -> None:
    """Append one line to data/algo_shadow.jsonl capturing both the
    algorithmic decision and (when present) the AI's decision for the
    same candidate. This is the A/B telemetry the user reviews to
    decide whether to flip cfg['use_algorithmic_tier1'].

    Schema (newline-delimited JSON, one record per call):
      {
        ts:                ISO timestamp,
        ticker:            str,
        path:              str (slow_safe / moderate / aggressive / ...),
        algo_score:        float | null,
        algo_would_promote: bool,
        algo_gate_reason:  str,
        algo_reasons:      [str, ...],  # rules that fired
        event_triggered:   bool,
        threshold:         float,
        ai_ran:            bool,
        ai_promoted:       bool | null,   # true if AI emitted a BUY
        ai_direction:      str  | null,
        algo_error:        str | null,
      }

    Best-effort; failures here are swallowed (logging must not block
    dispatch).
    """
    import json
    import os
    from datetime import datetime as _dt
    try:
        ai_promoted: Optional[bool] = None
        ai_direction: Optional[str] = None
        if ai_ran and isinstance(ai_prediction, dict):
            d = str(ai_prediction.get('direction') or '').upper()
            ai_direction = d or None
            # BUY-family verdicts mean "promoted to tier-2" downstream.
            ai_promoted = d in ('BUY', 'STRONG_BUY')
        record = {
            'ts':                 _dt.utcnow().isoformat(timespec='seconds') + 'Z',
            'ticker':             ticker,
            'path':               path,
            'algo_score':         decision.get('score'),
            'algo_would_promote': bool(decision.get('algo_would_promote')),
            'algo_gate_reason':   decision.get('gate_reason', ''),
            'algo_reasons':       list(decision.get('reasons', [])),
            'event_triggered':    bool(decision.get('event_triggered')),
            'threshold':          decision.get('threshold'),
            'ai_ran':             bool(ai_ran),
            'ai_promoted':        ai_promoted,
            'ai_direction':       ai_direction,
            'algo_error':         decision.get('error'),
        }
        # Path: alongside other operational data files.
        log_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'data', 'algo_shadow.jsonl')
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, default=str) + '\n')
    except Exception:
        pass  # never raise from a logging helper




def _run_cloud_one(app, prompt: str, ticker: str, path: str,
                   chosen: dict,
                   scan_provider_filter: Optional[str] = None
                   ) -> Optional[dict]:
    """Run a single cloud provider against the prompt. The smart
    router's `scan_provider_filter` parameter lets us target one
    provider out of the configured set. We invoke through the
    existing run_apis_for_scan_prediction path which writes to
    predictions_log as a side effect — for Phase 1 we accept this
    (the predictions_log already exists; the queue row mirrors)."""
    try:
        import tm_api_providers as _tmap
    except Exception:
        return None
    state = getattr(app, '_holdings_state', None) or {}
    plog = state.get('predictions_log')
    if plog is None:
        return None
    # Snapshot predictions before the call so we can pick up the new
    # row after the call returns. The call writes to predictions_log
    # internally; reading-after is the cleanest way to recover the
    # parsed prediction without re-running the parser.
    try:
        before_count = len(plog._cache)
    except Exception:
        before_count = 0
    try:
        # v4.14.3.6 (2026-05-14): wire log_fn through so the smart
        # router's existing diagnostic output (cooldown skips,
        # transient retries, [degradation] tags, all-exhausted
        # breadcrumbs) reaches Mike's activity log. Pre-v4.14.3.6
        # this was None — all of the router's wonderful diagnostic
        # surface vanished into a no-op gate, leaving the queue
        # runner's amber summary as the only signal that anything
        # had gone wrong. The router emits via the (msg, color)
        # signature; we wrap with _safe_log so the call is thread-
        # safe via root.after.
        def _router_log(msg, color='muted'):
            _safe_log(app, msg, color)
        # v4.14.3.11 (2026-05-15): scan_provider_filter=None opens
        # the router's full multi-provider rotation. Pre-v4.14.3.11
        # the runner narrowed every candidate to one provider via
        # chosen.registry_id, concentrating load on the picker's
        # winner and cascading NO_CALLs whenever that provider
        # 429'd mid-pass. With None, the router's existing
        # v4.14.1.1 single-provider-per-candidate-with-rotation
        # mode picks one canonical model round-robin per candidate
        # and within each model uses sticky-pick + retry-on-transient
        # + failover-on-quota across all eligible providers. The
        # picker's chosen registry_id stays available on `chosen`
        # for activity-log display purposes but no longer controls
        # dispatch.
        _rmeta: dict = {}
        _tmap.run_apis_for_scan_prediction(
            prompt=prompt,
            ticker=ticker,
            path=path,
            source='queue_runner',
            predictions_log=plog,
            log_fn=_router_log,
            scan_provider_filter=scan_provider_filter,
            result_meta=_rmeta,
        )
    except Exception:
        return None
    # v4.14.5.62-concurrent-scan: prefer the record the router handed back
    # directly (result_meta['prediction']) — self-contained, no dependence
    # on shared predictions_log ordering, so it's safe when many workers
    # append at once. Falls back to the by-position recovery below only if
    # the router didn't populate it (older path / multi-write callers).
    _meta_pred = _rmeta.get('prediction')
    if _meta_pred is not None:
        return _meta_pred
    if _rmeta.get('provider_unavailable'):
        return PROVIDER_UNAVAILABLE
    # Find the newly-added prediction(s) for this ticker.
    try:
        new_rows = list(plog._cache)[before_count:]
    except Exception:
        return None
    if not new_rows:
        # v4.14.5.14a.2: nothing written. If the scan runner signalled
        # that no provider was available, propagate the sentinel so the
        # caller skips the cursor entirely (ticker retried when a
        # provider recovers). Otherwise it's an ordinary empty result.
        if _rmeta.get('provider_unavailable'):
            return PROVIDER_UNAVAILABLE
        return None
    # Pick the most recent BUY for this ticker — or just the most
    # recent if no BUY found.
    candidates = [p for p in new_rows
                   if (p.get('ticker', '').upper() == ticker.upper())]
    if not candidates:
        return None
    buys = [p for p in candidates
             if (p.get('direction') or '').upper() == 'BUY']
    if buys:
        return buys[-1]
    # Non-BUY return value — caller will skip.
    return candidates[-1]


# ─── v4.14.5.14c-p2: one-ticker-ALL-paths unified dispatch ───────────
# All of this is INERT unless cfg['use_unified_multi_path_prompts'] is
# True (default False). Every entry point fails OPEN: any error →
# return None / fall back to today's per-(ticker,path) path. The OFF
# path is byte-identical to v4.14.5.14c-p1.

_ALL_PATH_KEYS = ('slow_safe', 'moderate', 'aggressive',
                  'lottery', 'penny_lottery')
# Parsed-prediction fields a per-path fan-out record overrides on top
# of the (shared) primary record's provider attribution.
_FANOUT_PRED_FIELDS = ('direction', 'buy_zone_low', 'buy_zone_high',
                       'target', 'stop', 'timeframe_days',
                       'confidence', 'raw_text',
                       'current_price_at_prediction')


def _unified_enabled(app) -> bool:
    try:
        return bool((getattr(app, 'cfg', {}) or {}).get(
            'use_unified_multi_path_prompts', False))
    except Exception:
        return False


def _eligible_paths_for(app, ticker: str, recency_bypass=None) -> list:
    """Paths this ONE ticker is eligible for RIGHT NOW, applying the
    SAME gates the per-path dispatch already applies: per-path pool
    membership + price-band eligibility (.14a.14) + verdict-recency
    (.14a.9/.11). Never raises (→ [] on any fault, caller skips).

    v4.14.5.14-target-stop-override-recency: if `recency_bypass` is a set
    containing this ticker (i.e. it fired a target_stop this sweep), the
    verdict-recency gate is SKIPPED — a price crossing its target/stop is
    the high-signal event the verdict was waiting for, so re-analyse even
    if the verdict is recent. The pool + price-band gates below STILL apply
    (a target_stop on a ticker outside the path's pool is still skipped)."""
    out = []
    try:
        tk = str(ticker).upper()
        _bypass_recency = bool(recency_bypass and tk in recency_bypass)
        # v4.14.5.14-merge-and-unify-fix Fix 6 (2026-05-19): iterate
        # the LIVE tm_holdings.PATHS at call time, not the static
        # _ALL_PATH_KEYS tuple captured at import. After the merge
        # pops penny_lottery from PATHS, the prompt builder must
        # reflect that — otherwise every multi-path unified call
        # asks the AI for a penny_lottery block, the model returns
        # it, and the parser drops it as unknown ("; 1 dropped by
        # model (penny_lottery)" lines in the activity log). Token
        # waste per call. Reading PATHS fresh per dispatch also means
        # future path-set mutations automatically propagate without a
        # second code edit (HANDOFF item 19). Fail-OPEN to the legacy
        # _ALL_PATH_KEYS tuple if tm_holdings is unimportable.
        try:
            import tm_holdings as _th_v
            _path_keys = list(_th_v.PATHS.keys())
        except Exception:
            _path_keys = list(_ALL_PATH_KEYS)
        for p in _path_keys:
            # pool membership (same as _layered_candidate_batch /
            # _run_event_driven_sweep)
            try:
                if bool((getattr(app, 'cfg', {}) or {}).get(
                        'use_path_candidate_pools', True)):
                    import tm_path_candidate_pools as _tpcp
                    pl = _tpcp.get_path_universe(app, p) or []
                    if pl and tk not in {str(t).upper()
                                         for t in pl}:
                        continue
            except Exception:
                pass
            # price-band gate (.14a.14) — reuse the exact helper
            try:
                if not _eligibility_price_band_filter(
                        app, p, [tk], 'unified'):
                    continue
            except Exception:
                pass
            # verdict-recency gate (.14a.9/.11) — bypassed for target_stop
            # fires (v4.14.5.14-target-stop-override-recency).
            if not _bypass_recency:
                try:
                    if tk in _recently_judged_set(app, p):
                        continue
                except Exception:
                    pass
            out.append(p)
    except Exception:
        return []
    return out


def _run_cloud_multi(app, prompt, ticker, eligible_paths, chosen,
                     scan_provider_filter=None, status_out=None):
    """One cloud call (full router rotation, unchanged) for a
    multi-path prompt → {path: prediction_record}. Reuses
    run_apis_for_scan_prediction verbatim by passing a capturing
    parse_fn (records the raw response + returns the PRIMARY path's
    block so the ONE record the router writes is correct + fully
    attributed), then clones that primary record's provider
    attribution into one extra predictions_log record per OTHER
    parsed path. run_apis_for_scan_prediction itself is NOT modified.
    Returns PROVIDER_UNAVAILABLE / {} (fail) / {path: rec}.

    v4.14.6.8-scan-diagnostics-and-pacing (2026-06-11): optional
    `status_out` dict that the caller passes in to receive a small
    diagnostic stamp describing WHY an empty result occurred. Keys:
      - 'cause' in {'empty_upstream', 'parsed_no_direction',
                    'provider_unavailable', 'setup_fault',
                    'cache_read_fault', None}
      - 'model' (best-effort) the model label that was attempted
      - 'has_text' bool — whether any response text reached the parser
    Behavior is unchanged whether status_out is passed or not; this
    is log-only diagnostics so the caller can disambiguate the three
    distinct empty-result failure modes that v4.14.6.7's investigation
    found were all being logged as a generic 'unparseable' message.
    """
    def _stamp(cause, **extra):
        if status_out is not None:
            try:
                status_out['cause'] = cause
                for k, v in extra.items():
                    status_out[k] = v
            except Exception:
                pass

    try:
        import tm_api_providers as _tmap
        import tm_discover as _tmd
    except Exception:
        _stamp('setup_fault')
        return {}
    state = getattr(app, '_holdings_state', None) or {}
    plog = state.get('predictions_log')
    if plog is None:
        _stamp('setup_fault')
        return {}
    primary = eligible_paths[0]
    cprice = None
    try:
        cache = state.get('cache')
        if cache is not None:
            cprice = (cache.quote(ticker) or {}).get('price')
    except Exception:
        cprice = None
    box: dict = {}

    def _capturing_parse(text, _ticker=None):
        # Stash raw text for the post-call fan-out; hand the router
        # the PRIMARY path's parsed block so its single written
        # record is correct + carries real provider attribution.
        box['raw'] = text
        try:
            mp = _tmd.parse_multi_path_prediction(
                text, ticker, eligible_paths, current_price=cprice)
        except Exception:
            mp = {}
        if mp.get(primary):
            return mp[primary]
        # Fall back to a whole-text single parse so the router still
        # writes *something* attributed (caller treats absent paths
        # as "retry next cycle").
        try:
            return _tmd.parse_prediction(text, ticker,
                                         current_price=cprice)
        except Exception:
            return {'ticker': str(ticker).upper(),
                    'direction': None, 'raw_text': text}

    try:
        before = len(plog._cache)
    except Exception:
        before = 0
    _rmeta: dict = {}
    try:
        def _router_log(msg, color='muted'):
            _safe_log(app, msg, color)
        _tmap.run_apis_for_scan_prediction(
            prompt=prompt, ticker=ticker, path=primary,
            source='queue_runner', parse_fn=_capturing_parse,
            predictions_log=plog, log_fn=_router_log,
            scan_provider_filter=scan_provider_filter,
            result_meta=_rmeta)
    except Exception as e:
        # v4.14.6.10-fix-trl-import (2026-06-11): the pre-patch
        # `except Exception: return {}` swallowed a NameError silently
        # for over a day (v4.14.6.5 added `_trl._TLS` references to
        # run_apis_for_scan_prediction without the matching import,
        # turning every scan call into an empty {} that looked like a
        # content problem). Capture the exception type + message so
        # future surprises surface in the activity log on the first
        # call, not after six investigations. The empty-result
        # behavior is unchanged — this is logging only.
        _err_str = f"{type(e).__name__}: {e}"
        _stamp('empty_upstream',
               has_text=bool(box.get('raw')),
               error=_err_str)
        try:
            _log_amber(
                app,
                f"[unified-scan] {ticker}: run_apis raised "
                f"{_err_str}")
        except Exception:
            pass
        return {}
    # v4.14.5.62-concurrent-scan: prefer the record the router handed back
    # (result_meta['prediction']) for the PRIMARY path — self-contained, no
    # shared-cache positional read, so it's safe under concurrent appends.
    # box['raw'] (captured in the closure above, call-local) still drives the
    # other-path fan-out. Falls back to the by-position recovery only if the
    # router didn't populate the meta.
    primary_rec = _rmeta.get('prediction')
    if primary_rec is None:
        try:
            new_rows = list(plog._cache)[before:]
        except Exception:
            _stamp('cache_read_fault', has_text=bool(box.get('raw')))
            return {}
        if not new_rows:
            if _rmeta.get('provider_unavailable'):
                _stamp('provider_unavailable')
                return PROVIDER_UNAVAILABLE
            _stamp('empty_upstream', has_text=bool(box.get('raw')))
            return {}
        prim_recs = [r for r in new_rows
                     if str(r.get('ticker', '')).upper()
                     == str(ticker).upper()]
        if not prim_recs:
            _stamp('empty_upstream', has_text=bool(box.get('raw')))
            return {}
        primary_rec = prim_recs[-1]
    raw = box.get('raw') or primary_rec.get('raw_text') or ''
    try:
        parsed = _tmd.parse_multi_path_prediction(
            raw, ticker, eligible_paths, current_price=cprice)
    except Exception:
        parsed = {}
    result: dict = {}
    # Primary path: the router already wrote primary_rec for it.
    result[primary] = primary_rec
    # Fan out the remaining parsed paths by cloning primary_rec's
    # attribution and overriding the per-path parsed fields.
    for p, pp in parsed.items():
        if p == primary or p not in eligible_paths:
            continue
        try:
            rec = dict(primary_rec)
            rec['path'] = p
            for k in _FANOUT_PRED_FIELDS:
                if k in pp:
                    rec[k] = pp.get(k)
            rec.pop('id', None)  # let the log assign a fresh id
            plog.append(rec)
            result[p] = rec
        except Exception:
            continue
    # If the primary block itself never parsed a direction, drop it
    # from the result so the caller records 'failed' for primary and
    # retries it next cycle (the record still exists, harmlessly).
    if not (primary_rec.get('direction') or ''):
        # v4.14.6.8-scan-diagnostics-and-pacing: stamp the parsed-but-
        # missing-direction case so the caller can log the genuine
        # parser-miss separately from upstream/empty bails. has_text
        # is True because we reached the parser at all (box['raw'] may
        # still be empty if the model returned no content but the
        # router still produced a primary_rec via NO_CALL writer —
        # surface what we observed).
        _stamp('parsed_no_direction',
               has_text=bool(box.get('raw')),
               model=(primary_rec.get('model')
                      or primary_rec.get('actual_provider')
                      or '?'))
        result.pop(primary, None)
    return result


def _run_local_multi(app, prompt, ticker, eligible_paths):
    """Ollama variant: _run_local_one already returns the raw single
    response; for unified we re-run the local model once with the
    multi-path prompt and multi-parse. Writes nothing to
    predictions_log itself (local single path didn't either) — the
    caller records outcomes/BUYs from the returned {path: pred}."""
    try:
        import tm_discover as _tmd
        state = getattr(app, '_holdings_state', None) or {}
        cprice = None
        try:
            cache = state.get('cache')
            if cache is not None:
                cprice = (cache.quote(ticker) or {}).get('price')
        except Exception:
            cprice = None
        # Reuse _run_local_one's transport by asking it for the raw
        # parsed dict; but we need raw text → call the model directly
        # the same way _run_local_one does is over-engineering here.
        # Local is rare for Mike (cloud-only); fail OPEN to per-path.
        return None
    except Exception:
        return None


def _unified_dispatch_ticker(app, chosen, ticker, loop_path,
                             pass_started, stats, src_label,
                             recency_bypass=None,
                             scan_provider_filter=None):
    """Per-ticker unified dispatch shared by BOTH dispatch loops.
    Returns one of: 'done' (handled — caller `continue`s),
    'skip' (no eligible paths / unavailable — caller `continue`s),
    None (FALL BACK to legacy per-(ticker,path) for this ticker —
    flag off, local model, or any fault). Never raises.

    v4.14.5.14-target-stop-override-recency: `recency_bypass` (set of
    upper-cased tickers, or None) is forwarded to _eligible_paths_for so
    target_stop fires skip the verdict-recency gate. None = normal gating."""
    try:
        if not _unified_enabled(app):
            return None
        hw = getattr(app, '_holdings_window', None)
        if hw is None:
            return None
        if chosen.get('kind') == 'local':
            return None  # local → legacy per-path (fail-open)
        elig = _eligible_paths_for(app, ticker, recency_bypass=recency_bypass)
        if not elig:
            # v4.14.5.25: count this gated skip so the pass summary's honest
            # accounting (analyzed = checked − gated − skipped) sees it — the
            # gate ran BEFORE any AI call, so this ticker cost 0 calls and must
            # NOT be reported as "analyzed". Routine-tagged so the per-ticker
            # line collapses; the count rolls into the one summary line.
            stats['gated'] = stats.get('gated', 0) + 1
            _log_routine(
                app,
                f"[unified-scan] {ticker}: 0 eligible paths "
                f"(gated) — skipped.")
            return 'skip'
        try:
            prompt = hw._build_candidate_prompt(
                ticker, '', elig[0], multi_paths=elig)
        except Exception as e:
            _log_amber(
                app,
                f"[unified-scan] {ticker}: prompt build failed "
                f"({type(e).__name__}: {e}); falling back to "
                f"per-path.")
            return None
        if not prompt:
            return None
        # v4.14.6.8-scan-diagnostics-and-pacing (2026-06-11): pass a
        # status dict so the failure branch can log WHICH empty-result
        # mode occurred. The investigation found the prior catch-all
        # "response unparseable" line covered three distinct causes —
        # empty/upstream bail, parsed-but-no-direction, and capacity —
        # making it impossible to count the real parser-miss rate. We
        # now select the log message from `_status['cause']`. Behavior
        # below (record_analysis_outcome, return code) is unchanged.
        _status: dict = {}
        res = _run_cloud_multi(app, prompt, ticker, elig, chosen,
                               scan_provider_filter=scan_provider_filter,
                               status_out=_status)
        if res is PROVIDER_UNAVAILABLE:
            stats['skipped'] = stats.get('skipped', 0) + 1
            _log_muted(
                app,
                f"[unified-scan] {ticker}: no eligible providers "
                f"(will retry when available)")
            return 'skip'
        if not res:
            # Nothing parsed cleanly — record 'failed' for the
            # loop's path so it isn't starved; retry next cycle.
            _record_analysis_outcome(app, ticker, loop_path,
                                     'failed')
            stats['silent'] = stats.get('silent', 0) + 1
            _cause = _status.get('cause')
            _model = _status.get('model') or '?'
            if _cause == 'parsed_no_direction':
                _log_amber(
                    app,
                    f"[unified-scan] {ticker}: response had no "
                    f"parseable DIRECTION ({_model}) — retry next "
                    f"cycle.")
            elif _cause == 'empty_upstream':
                _log_muted(
                    app,
                    f"[unified-scan] {ticker}: empty/no-content from "
                    f"provider — retry next cycle.")
            elif _cause == 'cache_read_fault':
                _log_amber(
                    app,
                    f"[unified-scan] {ticker}: predictions_log read "
                    f"fault — retry next cycle.")
            elif _cause == 'setup_fault':
                _log_amber(
                    app,
                    f"[unified-scan] {ticker}: dispatch setup fault "
                    f"(no predictions_log / import error) — retry "
                    f"next cycle.")
            else:
                _log_amber(
                    app,
                    f"[unified-scan] {ticker}: response unparseable "
                    f"for all {len(elig)} path(s) (uncategorized) — "
                    f"retry next cycle.")
            return 'done'
        wrote = 0
        for p, pred in res.items():
            direction = (pred.get('direction') or '').upper()
            outcome = direction if direction else 'NO_CALL'
            _record_analysis_outcome(app, ticker, p, outcome)
            _lbl = (pred.get('model')
                    or chosen.get('display_name')
                    or chosen.get('id') or '?')
            pc = stats.setdefault('provider_calls', {})
            pc[_lbl] = pc.get(_lbl, 0) + 1
            wrote += 1
            if direction == 'BUY':
                try:
                    _insert_queue_row(app, ticker, p, pred, chosen,
                                      pass_started)
                    stats['inserted'] = stats.get('inserted', 0) + 1
                except Exception as e:
                    _log_muted(
                        app,
                        f"[unified-scan] insert failed for "
                        f"{ticker}/{p}: {e}")
        missing = [p for p in elig if p not in res]
        _log_muted(
            app,
            f"[unified-scan] {ticker}: {len(elig)} eligible path(s) "
            f"→ 1 call → {wrote} record(s) written"
            + (f"; {len(missing)} dropped by model "
               f"({', '.join(missing)})" if missing else "")
            + ".")
        return 'done'
    except Exception as e:
        try:
            _log_amber(
                app,
                f"[unified-scan] {ticker}: unexpected "
                f"{type(e).__name__}: {e}; falling back to "
                f"per-path.")
        except Exception:
            pass
        return None


# ─── v4.14.5.62-concurrent-scan: optional worker-pool dispatch ───────
#
# All INERT unless cfg['use_concurrent_scan_dispatch'] is True (default
# False). When ON, a fill/scan pass fans its candidate list across a small
# pool of worker THREADS — one per enabled scan-eligible provider, each
# PINNED to that provider via scan_provider_filter — all pulling tickers
# from ONE shared thread-safe queue (so no two workers ever take the same
# ticker: natural no-duplication). Every DB write still goes through
# _db_lock / _record_analysis_outcome (already thread-safe), and the
# per-provider rate limiter is already thread-safe — so concurrency
# changes SPEED, not results. When OFF, the sequential per-ticker loop in
# run_one_pass_for_triggers runs UNCHANGED as the proven fallback.

_CONCURRENT_DEFAULT_MAX_WORKERS = 6


def _concurrent_enabled(app) -> bool:
    # v4.14.5.62-autoenable-multiprovider: the config value may be the sentinel
    # "auto" (default) → effective = (>=2 working providers); a real bool is an
    # explicit user override, honored exactly. Resolved LIVE at scan time, so
    # it reflects the current working-provider count. Fail-safe → False (the
    # byte-identical-to-off result).
    try:
        _val = (getattr(app, 'cfg', {}) or {}).get(
            'use_concurrent_scan_dispatch', 'auto')
        import tm_teacher_intercept as _tmic
        return _tmic.multiprovider_autoflag(_val, app)
    except Exception:
        return False


def _concurrent_scan_providers(app) -> list:
    """Enabled, scan-eligible providers to assign workers to — one worker
    per provider, in returned order, capped at concurrent_scan_max_workers.
    An optional cfg['concurrent_scan_provider_whitelist'] (list of preset /
    name / id strings, case-insensitive) limits the set so the first
    rollout can run a small pool (e.g. groq + cerebras + mistral). Empty
    whitelist = all enabled scan-eligible providers. Never raises → [] on
    any fault (caller then falls back to the sequential loop)."""
    try:
        import tm_api_providers as _tmap
        import tm_ai_router as _router
    except Exception:
        return []
    try:
        provs = _tmap.load_enabled_providers() or []
    except Exception:
        return []
    cfg = getattr(app, 'cfg', {}) or {}
    wl_raw = cfg.get('concurrent_scan_provider_whitelist') or []
    whitelist = set()
    try:
        for w in wl_raw:
            s = str(w).strip().lower()
            if s:
                whitelist.add(s)
    except Exception:
        whitelist = set()

    def _matches_wl(p):
        if not whitelist:
            return True
        for key in (p.get('preset'), p.get('name'), p.get('id')):
            if key is not None and str(key).strip().lower() in whitelist:
                return True
        return False

    eligible = []
    for p in provs:
        try:
            ok, _reason, _cap = _router.is_eligible(p, 'scan')
        except Exception:
            ok = False
        if not ok:
            continue
        if not _matches_wl(p):
            continue
        eligible.append(p)

    try:
        cap = int(cfg.get('concurrent_scan_max_workers',
                          _CONCURRENT_DEFAULT_MAX_WORKERS))
    except Exception:
        cap = _CONCURRENT_DEFAULT_MAX_WORKERS
    if cap < 1:
        cap = 1
    return eligible[:cap]


def _merge_tally(dst: dict, src: dict) -> None:
    """Fold one worker's local tally into the shared merged tally (caller
    holds the merge lock). Mirrors the sequential loop's counter split:
    legacy-branch counters direct, unified-branch counts under 'ustats'
    (the existing post-loop code folds ustats into the summary once)."""
    for k in ('inserted', 'silent', 'skipped'):
        dst[k] = dst.get(k, 0) + int(src.get(k, 0) or 0)
    for sub in ('drop_reasons', 'provider_calls'):
        d = dst.setdefault(sub, {})
        for _k, _v in (src.get(sub) or {}).items():
            d[_k] = d.get(_k, 0) + _v
    us_dst = dst.setdefault('ustats', {})
    for _k, _v in (src.get('ustats') or {}).items():
        if _k == 'provider_calls':
            pc = us_dst.setdefault('provider_calls', {})
            for _pk, _pv in (_v or {}).items():
                pc[_pk] = pc.get(_pk, 0) + _pv
        else:
            us_dst[_k] = us_dst.get(_k, 0) + int(_v or 0)


def _dispatch_one_ticker(app, chosen, ticker, path, pass_started,
                         src, recency_bypass, scan_provider_filter,
                         tally) -> None:
    """Concurrent twin of the sequential per-ticker loop body
    (run_one_pass_for_triggers). Pinned to ONE provider via
    scan_provider_filter; mutates the per-WORKER `tally` dict (no shared
    state → no lock needed). Deliberately mirrors the sequential body so
    concurrency changes speed, not results."""
    # Unified one-ticker-all-paths first (the default-on live path), pinned.
    # Its counts land in the per-worker ustats sub-dict (folded like the
    # sequential loop folds _ustats).
    _ud = _unified_dispatch_ticker(
        app, chosen, ticker, path, pass_started,
        tally.setdefault('ustats', {}), src,
        recency_bypass=recency_bypass,
        scan_provider_filter=scan_provider_filter)
    if _ud in ('done', 'skip'):
        return
    # Legacy per-(ticker,path) fallback (unified off / fault), pinned.
    try:
        pred = _analyze_candidate(
            app, chosen, ticker, path,
            drop_reasons=tally.setdefault('drop_reasons', {}),
            scan_provider_filter=scan_provider_filter)
    except Exception as e:
        _log_muted(
            app,
            f"{src} dispatch: {ticker} analysis failed: "
            f"{type(e).__name__}")
        _record_analysis_outcome(app, ticker, path, 'failed')
        return
    if pred is PROVIDER_UNAVAILABLE:
        tally['skipped'] = tally.get('skipped', 0) + 1
        _log_muted(
            app,
            f"[fill-mode] skipped {ticker} — no eligible providers "
            f"(will retry when available)")
        return
    if pred is None:
        tally['silent'] = tally.get('silent', 0) + 1
        _record_analysis_outcome(app, ticker, path, 'failed')
        return
    direction = (pred.get('direction') or '').upper()
    outcome = direction if direction else 'NO_CALL'
    _record_analysis_outcome(app, ticker, path, outcome)
    _provider_label = (pred.get('model')
                       or chosen.get('display_name')
                       or chosen.get('id') or '?')
    pc = tally.setdefault('provider_calls', {})
    pc[_provider_label] = pc.get(_provider_label, 0) + 1
    if direction != 'BUY':
        return
    try:
        _insert_queue_row(app, ticker, path, pred, chosen, pass_started)
        tally['inserted'] = tally.get('inserted', 0) + 1
    except Exception as e:
        _log_muted(
            app,
            f"{src} dispatch: insert failed for {ticker}: {e}")


def _run_concurrent_dispatch(app, chosen, candidates, path, pass_started,
                             src, recency_bypass):
    """Worker-pool dispatch: one thread per scan-eligible provider, each
    pinned to its provider, all draining ONE shared queue of candidates.
    Returns a merged tally dict (folded by the caller) when it ran, or
    None to signal "no eligible providers — run the sequential fallback".
    Never raises → None on any setup fault (safe fallback). The caller's
    begin_scan_run/end_scan_run window already wraps this (workers join
    before the caller's finally clears the scan-run state)."""
    try:
        import queue as _queue
        import threading as _threading
    except Exception:
        return None
    try:
        providers = _concurrent_scan_providers(app)
    except Exception:
        providers = []
    if not providers:
        _log_amber(
            app,
            f"[{src}] concurrent dispatch ({path}): no eligible "
            f"providers for a worker pool — falling back to the "
            f"sequential loop.")
        return None

    q = _queue.Queue()
    for t in (candidates or []):
        q.put(t)

    merged = {'inserted': 0, 'silent': 0, 'skipped': 0,
              'drop_reasons': {}, 'provider_calls': {}, 'ustats': {}}
    merge_lock = _threading.Lock()
    labels = [str(p.get('preset') or p.get('name') or p.get('id') or '?')
              for p in providers]
    _log_muted(
        app,
        f"[{src}] concurrent dispatch ({path}): {len(providers)} "
        f"worker(s) [{', '.join(labels)}] over {q.qsize()} ticker(s).")

    def _worker(prov):
        pid = str(prov.get('id') or '').strip() or None
        local = {}
        try:
            while not _stop_set(app):
                try:
                    ticker = q.get_nowait()
                except _queue.Empty:
                    break
                try:
                    _dispatch_one_ticker(
                        app, chosen, ticker, path, pass_started,
                        src, recency_bypass, pid, local)
                except Exception as e:
                    _log_muted(
                        app,
                        f"{src} [concurrent] worker error on "
                        f"{ticker}: {type(e).__name__}: {e}")
                finally:
                    q.task_done()
        finally:
            with merge_lock:
                _merge_tally(merged, local)

    threads = []
    for prov in providers:
        th = _threading.Thread(
            target=_worker, args=(prov,),
            name=f"scan-{prov.get('preset') or prov.get('id')}",
            daemon=True)
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    return merged


# ─── Pass summary logging (with steady-state suppression) ────────────

def _emit_summary_log(app, chosen: dict, candidate_count: int,
                       inserted: int, outcome: str,
                       path: str = '',
                       provider_calls: Optional[dict] = None,
                       skipped: int = 0, gated: int = 0) -> None:
    """One summary line per pass. Suppresses repeats when the pass is
    steady-state (same top AI, same insertion count, same outcome,
    same path, same provider mix) — avoids the every-15-minute
    "still picking Groq, nothing happening" spam. The first pass
    after app startup always logs so the user knows the runner is
    alive.

    outcome: 'success' (analyzed candidates, possibly inserted) or
             'no_candidates' (cache empty for current scope).

    v4.14.3.10: path is included in the user-visible message AND in
    the dedup tuple. Without path-in-dedup, consecutive different-
    path passes with identical (chosen_id, inserted, outcome) would
    silently dedup against each other.

    v4.14.3.11: provider_calls (display_label -> count) is also
    included in the message AND dedup tuple. Without provider-mix-
    in-dedup, two passes with identical totals but different
    distributions ('Mistral 8 Cerebras 6 Gemini 4' vs 'Mistral 12
    Cerebras 6') would falsely dedup against each other.
    """
    chosen_id = chosen.get('id', '?')
    chosen_label = chosen.get('display_name', chosen_id or '?')

    # Normalize provider_calls to a hashable representation for the
    # dedup tuple. Sorted tuple of (label, count) pairs gives a
    # deterministic order regardless of dict iteration.
    if provider_calls is None:
        provider_calls = {}
    provider_calls_signature = tuple(
        sorted(provider_calls.items()))

    # Pull previous-pass summary state from the app. First call sees
    # None and always logs.
    prev = getattr(app, '_queue_runner_prev_summary', None)
    is_first_pass = prev is None
    cur = {
        'chosen_id': chosen_id,
        'inserted': inserted,
        'outcome': outcome,
        'path': path,
        'provider_mix': provider_calls_signature,
        'skipped': int(skipped or 0),
        'gated': int(gated or 0),
    }

    state_changed = (prev is None) or (
        prev.get('chosen_id') != cur['chosen_id']
        or prev.get('inserted') != cur['inserted']
        or prev.get('outcome') != cur['outcome']
        or prev.get('path') != cur['path']
        or prev.get('provider_mix') != cur['provider_mix']
        or prev.get('skipped') != cur['skipped']
        or prev.get('gated') != cur['gated'])

    # v4.14.3.10: include path in the user-visible summary line.
    # Empty path fallback (defensive — callers should always pass one).
    path_tag = f" ({path})" if path else ""

    # v4.14.3.11: format the provider mix as "Mistral 8, Cerebras 6,
    # Gemini 4" (sorted by count descending so the workhorse comes
    # first). Empty mix renders as empty string — happens on no-
    # candidates and on success passes where every candidate's
    # router dispatch failed before writing a prediction.
    if provider_calls:
        mix_sorted = sorted(
            provider_calls.items(), key=lambda kv: (-kv[1], kv[0]))
        mix_str = ", ".join(
            f"{label} {count}" for (label, count) in mix_sorted)
    else:
        mix_str = ''

    # Compose the summary line in the right shape for the outcome.
    if outcome == 'no_candidates':
        msg = (f"Queue runner pass{path_tag}: {chosen_label}, "
               f"no fresh candidates in cache.")
    else:
        # v4.14.3.11: lead with the actual provider mix (the
        # picker's chosen label is now informational — the router's
        # rotation chose who actually served the calls). When mix
        # is empty (all candidates failed before writing a
        # prediction) fall back to the picker's chosen label so
        # the line still names a provider.
        served_by = mix_str if mix_str else chosen_label
        # v4.14.5.14-queue-runner-log-honesty: count REAL AI calls
        # separately from gated/skipped candidates so the reader sees at a
        # glance whether real work happened. The old line called
        # (candidate_count − skipped) "analyzed", which counted gated
        # candidates (dropped before any AI call) as analyzed — so a pass
        # where everything was gated read identically to a pass that did 16
        # real analyses. Definitions:
        #   gated   = dropped before an AI call (verdict-recency / prompt-
        #             build / window-not-ready, via drop_reasons)
        #   skipped = no provider available to attempt the call
        #   analyzed = candidates actually sent to the AI
        #            = checked − gated − skipped
        sk = int(skipped or 0)
        gt = int(gated or 0)
        analyzed = max(0, candidate_count - sk - gt)
        _cand = (f"{candidate_count} candidate"
                 f"{'' if candidate_count == 1 else 's'} checked")
        _picks = (f"{inserted} new pick"
                  f"{'' if inserted == 1 else 's'}")
        if candidate_count == 0:
            msg = (f"Queue runner pass{path_tag}: nothing to check "
                   f"this cycle.")
        elif analyzed == 0 and gt > 0 and sk == 0:
            # Everything gated — no AI work happened this pass.
            msg = (f"Queue runner pass{path_tag}: {_cand}, all gated; "
                   f"0 AI calls, {_picks}.")
        else:
            # "via {mix}" keeps the v4.14.3.11 per-provider counts (load-
            # distribution visibility) — and they're MORE honest than a bare
            # name: "3 analyzed via Groq 2" reveals 1 of the 3 AI calls
            # produced no verdict. served_by falls back to the picker's label
            # when the mix is empty (every call failed before writing).
            segs = [_cand]
            if gt:
                segs.append(f"{gt} gated")
            segs.append(
                f"{analyzed} analyzed"
                f"{(' via ' + served_by) if analyzed else ''}")
            if sk:
                segs.append(f"{sk} skipped (no providers)")
            segs.append(f"{_picks} queued")
            msg = f"Queue runner pass{path_tag}: " + ", ".join(segs) + "."

    # Never suppress no_candidates — it's the user's only signal that
    # the runner is alive and trying when nothing is happening. Spam
    # concern doesn't apply: no_candidates only fires once per 15 min
    # (or on event-driven trigger), not once per outer-loop poll.
    # May 2026 cadence-regression bug.
    #
    # Also never suppress "success, 0 inserts" — that's a signal the
    # user wants to see (every candidate returned non-BUY, or every
    # cloud call silently failed). v4.14.3.1 hotfix 2026-05-14:
    # without this branch, a stretch where the picked AI is repeatedly
    # in cooldown looks identical to a dead runner.
    should_log = (
        is_first_pass
        or outcome == 'no_candidates'
        or (outcome == 'success' and inserted == 0)
        or state_changed)
    if should_log:
        _log_muted(app, msg)

    # Stamp current state so the next pass knows what changed.
    try:
        app._queue_runner_prev_summary = cur
    except Exception:
        pass


# ─── Queue mutations ──────────────────────────────────────────────────

def _conn(app):
    """App's SQLite connection, or None."""
    for attr in ('db', '_db', 'database'):
        db = getattr(app, attr, None)
        if db is not None:
            return getattr(db, 'conn', None)
    return None


from contextlib import contextmanager as _cm   # noqa: E402  (used by _db_lock)


@_cm
def _db_lock(app):
    """v4.14.5.14-db-concurrency: acquire app.db.lock for the duration of a
    block of SQL on app.db.conn. The Connection is opened with
    `check_same_thread=False`, so SQLite itself (not Python) requires the
    caller to serialize concurrent use — without this, two background
    threads racing on the same Connection return `SQLITE_MISUSE: bad
    parameter or other API misuse`. Fail-OPEN: if the lock isn't reachable
    (no app / no db / no lock — e.g. headless tests with a stub app), yields
    without locking so behaviour stays identical to pre-fix."""
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


def _insert_queue_row(app, ticker: str, path: str, pred: dict,
                      chosen: dict, pass_started: str) -> None:
    conn = _conn(app)
    if conn is None:
        return
    with _db_lock(app):
        now = pass_started
        state = getattr(app, '_holdings_state', None) or {}
        cache = state.get('cache')
        price_at_creation = None
        if cache is not None:
            try:
                q = cache.quote(ticker)
                price_at_creation = (q or {}).get('price')
            except Exception:
                pass
        try:
            conn.execute(
                """
                INSERT INTO recommend_queue (
                    ticker, path, direction, target, stop, timeframe,
                    confidence, conviction, reasoning,
                    source_model, source_provider_kind,
                    created_at, last_seen_at, status,
                    price_at_creation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker, path,
                    (pred.get('direction') or 'BUY').upper(),
                    pred.get('target'),
                    pred.get('stop'),
                    pred.get('timeframe'),
                    pred.get('confidence'),
                    pred.get('conviction'),
                    pred.get('reasoning') or pred.get('reason_one_line'),
                    chosen.get('id'),
                    chosen.get('kind'),
                    now, now, 'active',
                    price_at_creation,
                ),
            )
            conn.commit()
        except Exception:
            pass


def _trim_active_to_cap(app) -> None:
    """If the active queue exceeds queue_runner_max_picks, evict the
    oldest active rows (FIFO by created_at) until within cap. Evicted
    rows are flagged 'invalidated' with reason 'queue_cap'."""
    conn = _conn(app)
    if conn is None:
        return
    with _db_lock(app):
        cap = int(app.cfg.get('queue_runner_max_picks', 30))
        try:
            cur = conn.execute(
                "SELECT pick_id FROM recommend_queue "
                "WHERE status = 'active' "
                "ORDER BY created_at ASC")
            rows = [r[0] for r in cur.fetchall()]
        except Exception:
            return
        if len(rows) <= cap:
            return
        to_evict = rows[:len(rows) - cap]
        for pick_id in to_evict:
            try:
                conn.execute(
                    "UPDATE recommend_queue "
                    "SET status = 'invalidated', "
                    "    invalidation_reason = 'queue_cap', "
                    "    last_seen_at = ? "
                    "WHERE pick_id = ?",
                    (datetime.now().isoformat(timespec='seconds'),
                     pick_id))
            except Exception:
                continue
        try:
            conn.commit()
        except Exception:
            pass


# v4.14.5.2 (Step 3 fast-follow). Per-path price-shift invalidation
# thresholds (fractions). A 5% intraday move on a penny stock is noise;
# a 10% move on a slow_safe blue-chip is meaningful. Tunable later.
PRICE_SHIFT_INVALIDATION_THRESHOLDS = {
    'slow_safe':     0.10,
    'moderate':      0.12,
    'aggressive':    0.15,
    'lottery':       0.25,
    'penny_lottery': 0.30,
}
DEFAULT_PRICE_SHIFT_THRESHOLD = 0.15  # unknown path

# recommend_queue has no reliable timeframe_days (the `timeframe`
# column is TEXT and frequently NULL), so the maturity guard uses
# these per-path defaults to mirror predictions.jsonl maturity logic.
HOUSEKEEPING_TIMEFRAME_DAYS_DEFAULT = {
    'slow_safe':     60,
    'moderate':      30,
    'aggressive':    14,
    'lottery':        7,
    'penny_lottery':  5,
}


def _run_housekeeping(app) -> None:
    """Walk active rows; mark graduated_target, graduated_stop, or
    invalidated based on current price vs. target/stop/creation.

    v4.14.5.2: when cfg['use_stable_recommend'] is True (default), the
    price-shift `invalidated` path is (a) maturity-gated — a pick
    younger than max(MIN_MATURITY_DAYS_FLOOR, path_timeframe * ratio)
    is never invalidated on a price wobble — and (b) uses a per-path
    threshold instead of the global 5%. graduated_target/graduated_stop
    are legitimate market resolutions and are NEVER maturity-gated
    (same principle as check_outcomes vs check_supersessions). When the
    flag is False, the legacy global 5% / no-maturity behavior is
    restored."""
    conn = _conn(app)
    if conn is None:
        return
    with _db_lock(app):
        state = getattr(app, '_holdings_state', None) or {}
        cache = state.get('cache')
        if cache is None:
            return
        legacy_threshold_pct = float(app.cfg.get(
            'queue_invalidation_pct_threshold', 5.0))
        try:
            stable = bool(app.cfg.get('use_stable_recommend', True))
        except Exception:
            stable = True
        try:
            from tm_discover import (MIN_MATURITY_DAYS_FLOOR as _MMF,
                                      MIN_MATURITY_TIMEFRAME_RATIO as _MMR)
        except Exception:
            _MMF, _MMR = 3, 0.25
        try:
            cur = conn.execute(
                "SELECT pick_id, ticker, path, target, stop, "
                "price_at_creation, created_at FROM recommend_queue "
                "WHERE status = 'active'")
            rows = cur.fetchall()
        except Exception:
            return
        now_dt = datetime.now()
        now = now_dt.isoformat(timespec='seconds')
        for (pick_id, ticker, path, target, stop,
                price_at_creation, created_at) in rows:
            try:
                q = cache.quote(ticker)
                current = (q or {}).get('price') if q else None
            except Exception:
                current = None
            if current is None:
                continue
            path = (path or '').strip()
            new_status = None
            new_reason = None
            if target is not None and current >= float(target):
                new_status = 'graduated_target'
            elif stop is not None and current <= float(stop):
                new_status = 'graduated_stop'
            elif (price_at_creation is not None
                    and price_at_creation > 0):
                shift_pct = abs(
                    (current - price_at_creation)
                    / price_at_creation * 100.0)
                if stable:
                    # Maturity gate (price-shift invalidation only).
                    premature = False
                    tf_days = HOUSEKEEPING_TIMEFRAME_DAYS_DEFAULT.get(
                        path, 30)
                    try:
                        created_dt = datetime.fromisoformat(created_at)
                        age_days = (
                            (now_dt - created_dt).total_seconds() / 86400.0)
                        maturity_days = max(
                            _MMF, tf_days * _MMR)
                        if age_days < maturity_days:
                            premature = True
                    except Exception:
                        premature = False  # unparseable -> don't protect
                    thr_pct = (
                        PRICE_SHIFT_INVALIDATION_THRESHOLDS.get(
                            path, DEFAULT_PRICE_SHIFT_THRESHOLD) * 100.0)
                    if (not premature) and shift_pct > thr_pct:
                        new_status = 'invalidated'
                        new_reason = 'price_shift'
                else:
                    if shift_pct > legacy_threshold_pct:
                        new_status = 'invalidated'
                        new_reason = 'price_shift'
            if new_status:
                try:
                    conn.execute(
                        "UPDATE recommend_queue "
                        "SET status = ?, "
                        "    invalidation_reason = ?, "
                        "    last_seen_at = ? "
                        "WHERE pick_id = ?",
                        (new_status, new_reason, now, pick_id))
                except Exception:
                    pass
        try:
            conn.commit()
        except Exception:
            pass


# ─── Logging + emit helpers ──────────────────────────────────────────

def _log_muted(app, msg: str) -> None:
    _safe_log(app, msg, 'muted')


def _log_routine(app, msg: str) -> None:
    """v4.14.5.25-activity-log-tiering: repetitive per-tick steady-state
    breadcrumb. Tagged 'routine' so App._log collapses it under
    activity_log_verbosity='normal' (one per ~15min per signature) and
    suppresses it under 'quiet', while 'verbose' restores the full firehose.
    Identical plumbing to _log_muted otherwise (renders muted) — purely a
    presentation tag, no behaviour."""
    _safe_log(app, msg, 'routine')


def _log_amber(app, msg: str) -> None:
    _safe_log(app, msg, 'amber')


def _safe_log(app, msg: str, color: str) -> None:
    try:
        log = getattr(app, '_log', None)
        root = getattr(app, 'root', None)
        if callable(log):
            if root is not None:
                root.after(0, lambda m=msg, c=color: log(m, c))
            else:
                log(msg, color)
    except Exception:
        pass
    # Always stamp the last-log time so the 30-min heartbeat suppresses
    # while the runner is producing normal output. May 13 2026.
    _mark_runner_logged(app)


def _mark_runner_logged(app) -> None:
    """Stamp app._queue_runner_last_log_time to NOW. The 30-min
    heartbeat in _runner_loop reads this to decide whether to fire."""
    try:
        app._queue_runner_last_log_time = datetime.now()
    except Exception:
        pass


def _set_run_outcome(app, outcome: str) -> None:
    """Stamp app._queue_runner_last_outcome — read by the 30-min
    heartbeat to describe what last happened ('success', 'no-budget',
    'disabled', 'no-candidates', 'awaiting first pass')."""
    try:
        app._queue_runner_last_outcome = outcome
    except Exception:
        pass


_HEARTBEAT_INTERVAL = timedelta(minutes=15)


def _heartbeat_if_quiet(app) -> None:
    """v4.14.5.25-activity-log-tiering: emit a periodic [heartbeat] liveness
    pulse on a FIXED interval, independent of other logging — so Mike always
    has a visible sign of life. (The pre-v4.14.5.25 trigger was '30+ min since
    the runner last logged anything', but with routine breadcrumbs now collapsed
    that timer keeps getting refreshed — _safe_log stamps last-log even on
    suppressed routine lines — so the old condition would never fire. A fixed
    own-interval timer pulses regardless.) One reassuring line per interval;
    tagged muted (NOT routine) so the verbosity filter never suppresses it.
    Reads existing in-memory state only — no new/expensive work, no behaviour.
    May 13 2026 observability fix, reworked v4.14.5.25."""
    try:
        now = datetime.now()
        last_hb = getattr(app, '_last_heartbeat_at', None)
        if last_hb is not None and (now - last_hb) < _HEARTBEAT_INTERVAL:
            return
        app._last_heartbeat_at = now
        outcome = getattr(
            app, '_queue_runner_last_outcome', 'awaiting first pass')
        interval_min = int(app.cfg.get('queue_runner_interval_min', 15))
        last_pass = app.cfg.get('queue_runner_last_pass', '')
        next_in = '?'
        if last_pass:
            try:
                last_dt = datetime.fromisoformat(last_pass)
                remaining = (
                    last_dt + timedelta(minutes=interval_min) - now)
                rem_min = max(0, int(remaining.total_seconds() // 60))
                next_in = f"{rem_min} min"
            except Exception:
                pass
        _log_muted(
            app,
            f"[heartbeat] alive — next pass in {next_in}, "
            f"last outcome: {outcome}.")
    except Exception:
        pass


def _emit(app, event_id: str,
          context: Optional[dict] = None) -> None:
    """Fire a Phase 2 system_event. Worker-thread-safe via emit's
    internal marshaling."""
    try:
        import tm_teacher_intercept as _tm_ic
        _tm_ic.emit_system_event(event_id, app=app, context=context)
    except Exception:
        pass
