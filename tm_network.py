"""tm_network.py — v4.15.0 Step 12 network state awareness.

Provides is_online() — a fast cached check that returns True if the network
is reachable. Background tick refreshes the state every 60s; on-demand calls
refresh if the cache is older than 30s.

Does NOT do fancy classification (partial connectivity, captive portal). Single
bit: online or offline. Adapters short-circuit on offline.

Probes TCP-connect to public DNS hosts rather than HTTP-GET, so the check is
fast (~50ms when online) and doesn't depend on any single provider's uptime.
"""

from __future__ import annotations
import socket
import threading
import time


_state_lock = threading.Lock()
_last_check_time: float = 0.0
_last_online: bool = True  # Optimistic default — assume online until proven otherwise.

# Public DNS resolvers, both highly available. TCP 443 on Cloudflare (HTTPS),
# TCP 53 on Google (DNS-over-TCP). Either succeeding means we have outbound IP.
_PROBE_HOSTS = [
    ('1.1.1.1', 443),
    ('8.8.8.8', 53),
]
_PROBE_TIMEOUT_SECONDS = 3.0
_CACHE_TTL_SECONDS = 30.0
_PERIODIC_INTERVAL_SECONDS = 60.0


def _probe_once() -> bool:
    """Try a TCP connect to each probe host. True if any succeeds."""
    for host, port in _PROBE_HOSTS:
        try:
            sock = socket.create_connection((host, port),
                                             timeout=_PROBE_TIMEOUT_SECONDS)
            sock.close()
            return True
        except (socket.timeout, OSError):
            continue
    return False


def is_online(force_recheck: bool = False) -> bool:
    """Return True if the network appears reachable.

    Uses a 30s cache. If force_recheck=True or the cache is stale, probes.
    Thread-safe. Returns the last known state on probe exception.
    """
    global _last_check_time, _last_online
    now = time.time()
    with _state_lock:
        if not force_recheck and (now - _last_check_time) < _CACHE_TTL_SECONDS:
            return _last_online
    # Probe outside the lock so a slow probe doesn't block other readers.
    try:
        new_state = _probe_once()
    except Exception:
        # Don't update _last_check_time so the next call retries soon.
        with _state_lock:
            return _last_online
    with _state_lock:
        _last_online = new_state
        _last_check_time = time.time()
        return new_state


def get_last_check_age_seconds() -> float:
    """How long ago the last check was. inf if never. Diagnostic only."""
    with _state_lock:
        if _last_check_time == 0.0:
            return float('inf')
        return time.time() - _last_check_time


# State-transition callbacks — wired by the app to log state changes.
_transition_callbacks: list = []
_callbacks_lock = threading.Lock()


def register_transition_callback(callback) -> None:
    """Register callable(was_online: bool, now_online: bool). Fires on flip."""
    with _callbacks_lock:
        _transition_callbacks.append(callback)


def _check_with_transition_notify() -> bool:
    """Probe and fire transition callbacks if state flipped. Returns new state."""
    global _last_check_time, _last_online
    try:
        new_state = _probe_once()
    except Exception:
        with _state_lock:
            return _last_online

    with _state_lock:
        previous = _last_online
        _last_online = new_state
        _last_check_time = time.time()

    if previous != new_state:
        with _callbacks_lock:
            cbs = list(_transition_callbacks)
        for cb in cbs:
            try:
                cb(previous, new_state)
            except Exception:
                pass

    return new_state


def periodic_tick() -> bool:
    """Refresh cached state and fire transition callbacks. Returns current state.

    Caller is expected to invoke this on roughly _PERIODIC_INTERVAL_SECONDS cadence
    from a thread other than the GUI thread (the probe blocks up to ~6s offline).
    """
    return _check_with_transition_notify()
