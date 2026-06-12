"""
tm_keepawake.py — Prevent Windows from hibernating during active work.

Uses SetThreadExecutionState with ES_SYSTEM_REQUIRED | ES_CONTINUOUS to keep
the system awake without preventing monitor sleep (we deliberately omit
ES_DISPLAY_REQUIRED, so the user's screen can still turn off normally).

When release_keep_awake() is called, the system reverts to normal sleep
behaviour. The hold is requested ONLY while the queue runner is doing real
work (a fill pass or an event-driven dispatch) and released once the runner
goes idle, so the machine is never held awake 24/7 for no reason.

No-op on non-Windows platforms (safe to call anywhere).
"""

import sys
import logging

log = logging.getLogger(__name__)

# Flag constants from winnt.h
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002  # not used — we allow monitor sleep

_is_windows = sys.platform == 'win32'
_active = False  # tracks current state to avoid redundant calls


def request_keep_awake(reason='active work'):
    """Tell Windows to keep the system awake. Idempotent — safe to call
    repeatedly; only the first call after a release actually touches the
    OS."""
    global _active
    if not _is_windows:
        return
    if _active:
        return  # already active, no-op
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        _active = True
        log.info(f"[keep-awake] requested: {reason}")
    except Exception as e:
        log.warning(f"[keep-awake] failed to acquire: {e}")


def release_keep_awake(reason='work complete'):
    """Release the keep-awake state. System reverts to normal sleep
    behaviour. Idempotent — a no-op if nothing is currently held."""
    global _active
    if not _is_windows:
        return
    if not _active:
        return  # already released, no-op
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        _active = False
        log.info(f"[keep-awake] released: {reason}")
    except Exception as e:
        log.warning(f"[keep-awake] failed to release: {e}")


def is_active():
    """Return True if keep-awake is currently held."""
    return _active
