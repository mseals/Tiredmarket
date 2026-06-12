"""
Tired Market — UPS / battery state monitoring.

Polls Windows for battery state and emits events on transitions:
- AC → Battery (power loss)
- Battery → AC (power restored)
- Low battery (warning threshold crossed)

Reads via PowerShell call to Win32_Battery (the wmic command was
deprecated in newer Windows versions). Polling cost: about 50-100ms
per query, runs every 10 seconds — negligible.

Designed for a CyberPower 1500VA UPS connected via USB, but works with
any UPS that registers as a HID battery in Windows. Verified working
with the LX1500GU model (Geek Squad / Best Buy rebrand of CyberPower
CP1500AVRLCD).

USAGE:
    from tm_power import PowerMonitor
    pm = PowerMonitor(
        on_state_change=lambda old, new: print(f"{old} -> {new}"),
        on_low_battery=lambda info: print(f"low: {info}"),
    )
    pm.start()
    # ... later ...
    pm.stop()

The thresholds (when to consider "low battery") are passed in via
config and can be tuned:
- low_battery_minutes: trigger low-battery event when EstimatedRunTime
  drops below this many minutes. Default 5.
- low_battery_pct: same trigger but for charge percentage. Default 20.

Both thresholds are checked; whichever triggers first wins.

THREADING NOTE: callbacks fire on the polling thread, NOT the main UI
thread. Caller is responsible for marshalling to UI thread (e.g. via
root.after(0, ...)). Don't call tk operations directly from callbacks.

THREE STATES:
- "online" / "ac" — running on wall power, normal
- "on_battery" — power lost, running on UPS battery
- "unknown" — couldn't query Windows (no UPS, USB unplugged, etc.)
"""

from __future__ import annotations

import subprocess
import threading
import time
from datetime import datetime
from typing import Callable, Optional


# Battery status codes from Win32_Battery
# https://docs.microsoft.com/en-us/windows/win32/cimwin32prov/win32-battery
BATTERY_STATUS_OTHER = 1               # Discharging (on battery)
BATTERY_STATUS_UNKNOWN = 2             # Wait — confusing — let me get this right
# Actually:
# 1 = "The battery is discharging" (on battery)
# 2 = "The system has access to AC" (on AC, fully charged or charging)
# 3 = Fully Charged
# 4 = Low
# 5 = Critical
# 6 = Charging
# 7 = Charging and High
# 8 = Charging and Low
# 9 = Charging and Critical
# 10 = Undefined
# 11 = Partially Charged

# Statuses that mean "we are on battery, not AC"
ON_BATTERY_STATUSES = {1, 4, 5}

# Statuses that mean "we are on AC (charging or full)"
ON_AC_STATUSES = {2, 3, 6, 7, 8, 9, 11}


class BatteryInfo:
    """Snapshot of battery state at a point in time."""

    def __init__(self, name: str = "",
                 status: int = 0,
                 charge_pct: int = 0,
                 runtime_minutes: int = 0,
                 raw: dict | None = None):
        self.name = name
        self.status = status
        self.charge_pct = charge_pct
        self.runtime_minutes = runtime_minutes
        self.timestamp = datetime.now()
        self.raw = raw or {}

    @property
    def on_battery(self) -> bool:
        return self.status in ON_BATTERY_STATUSES

    @property
    def on_ac(self) -> bool:
        return self.status in ON_AC_STATUSES

    @property
    def state_label(self) -> str:
        if self.on_battery:
            return "on_battery"
        elif self.on_ac:
            return "online"
        return "unknown"

    @property
    def is_valid(self) -> bool:
        """True if this looks like a real battery reading. False if
        we got nothing back (no UPS detected, query failed)."""
        return bool(self.name) and self.status > 0

    def __repr__(self):
        return (f"BatteryInfo(name={self.name!r}, status={self.status}, "
                f"charge={self.charge_pct}%, runtime={self.runtime_minutes}min, "
                f"state={self.state_label})")


def query_battery() -> BatteryInfo | None:
    """Query Windows for current battery state via PowerShell (wmic
    deprecated). Returns BatteryInfo or None on failure.

    Single shot. Costs ~50-200ms (PowerShell startup is the bottleneck,
    not the query itself).

    Windows reports EstimatedRunTime as a 32-bit unsigned int with
    71582788 (0x44617474 / huge number) meaning "infinity / on AC". We
    map that to 0 since it's meaningless when on AC anyway.
    """
    cmd = [
        'powershell',
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        # CSV output is the most compact + parseable; avoid Format-Table
        # since it adds whitespace/header-line variability.
        ("Get-CimInstance Win32_Battery | "
          "Select-Object Name, BatteryStatus, EstimatedChargeRemaining, "
          "EstimatedRunTime | "
          "ConvertTo-Csv -NoTypeInformation"),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5,
            # Hide the PowerShell window flash on Windows
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        if result.returncode != 0:
            return None
        out = result.stdout.strip()
        if not out:
            return None
    except Exception:
        return None

    # Parse CSV — first line is header, second line is data
    lines = out.splitlines()
    if len(lines) < 2:
        return None

    # Strip quotes around CSV fields
    def _strip(s):
        s = s.strip()
        if s.startswith('"') and s.endswith('"'):
            s = s[1:-1]
        return s

    header = [_strip(h) for h in lines[0].split(',')]
    data = [_strip(d) for d in lines[1].split(',')]
    if len(header) != len(data):
        return None
    raw = dict(zip(header, data))

    name = raw.get('Name', '')
    try:
        status = int(raw.get('BatteryStatus', '0') or '0')
    except ValueError:
        status = 0
    try:
        charge = int(raw.get('EstimatedChargeRemaining', '0') or '0')
    except ValueError:
        charge = 0
    try:
        runtime = int(raw.get('EstimatedRunTime', '0') or '0')
    except ValueError:
        runtime = 0

    # Windows returns 71582788 (or similar very large) for "infinity"
    # when on AC. Anything over 1440 (24 hours) is bogus — clamp to 0.
    if runtime > 1440:
        runtime = 0

    return BatteryInfo(name=name, status=status, charge_pct=charge,
                        runtime_minutes=runtime, raw=raw)


class PowerMonitor:
    """Background thread that polls battery state and emits state-change
    events.

    Callbacks (all optional, all best-effort, exceptions caught):
    - on_state_change(old_state: str, new_state: str, info: BatteryInfo)
        Fires on transitions: 'online' <-> 'on_battery' <-> 'unknown'
    - on_low_battery(info: BatteryInfo)
        Fires once when crossing into low-battery threshold (charge or
        runtime). Won't fire repeatedly during a single battery session.
    - on_critical(info: BatteryInfo)
        Fires once when crossing into critical-battery threshold.
        Used as the trigger for graceful shutdown if enabled.

    State of the monitor itself:
    - thread runs in background (daemon=True)
    - poll_interval seconds between checks (default 10)
    - thresholds for low/critical from constructor or set_thresholds()
    """

    def __init__(self,
                 on_state_change: Callable | None = None,
                 on_low_battery: Callable | None = None,
                 on_critical: Callable | None = None,
                 poll_interval: float = 10.0,
                 low_battery_minutes: int = 5,
                 low_battery_pct: int = 20,
                 critical_battery_minutes: int = 2,
                 critical_battery_pct: int = 10):
        self.on_state_change = on_state_change
        self.on_low_battery = on_low_battery
        self.on_critical = on_critical
        self.poll_interval = poll_interval
        self.low_battery_minutes = low_battery_minutes
        self.low_battery_pct = low_battery_pct
        self.critical_battery_minutes = critical_battery_minutes
        self.critical_battery_pct = critical_battery_pct

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_state: str = 'unknown'
        self._last_info: BatteryInfo | None = None
        # Latched flags so we don't fire low/critical events every poll
        self._low_fired_this_session = False
        self._critical_fired_this_session = False
        # When did we go on battery? Used for "first 90 seconds, don't
        # trust EstimatedRunTime" logic.
        self._battery_session_start: datetime | None = None

    @property
    def last_info(self) -> BatteryInfo | None:
        return self._last_info

    @property
    def state(self) -> str:
        return self._last_state

    @property
    def battery_session_seconds(self) -> int:
        """How long we've been on battery, 0 if on AC."""
        if self._battery_session_start is None:
            return 0
        return int((datetime.now() - self._battery_session_start).total_seconds())

    def set_thresholds(self, low_minutes: int = None,
                        low_pct: int = None,
                        critical_minutes: int = None,
                        critical_pct: int = None):
        """Update thresholds. Caller can pass any combination."""
        if low_minutes is not None:
            self.low_battery_minutes = low_minutes
        if low_pct is not None:
            self.low_battery_pct = low_pct
        if critical_minutes is not None:
            self.critical_battery_minutes = critical_minutes
        if critical_pct is not None:
            self.critical_battery_pct = critical_pct

    def start(self):
        """Begin polling. No-op if already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                          name='tm_power_monitor')
        self._thread.start()

    def stop(self):
        """Stop polling. Blocks briefly until thread exits."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def poll_once(self) -> BatteryInfo | None:
        """Single poll, no thread. Useful for one-time checks (e.g.
        when the app first launches, before starting the thread, to
        determine whether a UPS is detected at all)."""
        info = query_battery()
        if info is not None and info.is_valid:
            self._last_info = info
        return info

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._poll()
            except Exception:
                # Defensive: don't let one bad poll kill the thread
                pass
            # Sleep with stop-event check so .stop() returns quickly
            self._stop_event.wait(self.poll_interval)

    def _poll(self):
        info = query_battery()
        if info is None or not info.is_valid:
            # Couldn't query — if we were online before, stay online
            # (the UPS USB might have disconnected; that's not the same
            # as "going on battery"). Only fire 'unknown' once.
            if self._last_state != 'unknown':
                old = self._last_state
                self._last_state = 'unknown'
                self._fire_state_change(old, 'unknown', None)
            return

        self._last_info = info
        new_state = info.state_label

        if new_state != self._last_state:
            old = self._last_state
            self._last_state = new_state
            # Reset session flags on transition
            if new_state == 'on_battery':
                self._battery_session_start = datetime.now()
                self._low_fired_this_session = False
                self._critical_fired_this_session = False
            elif new_state == 'online':
                self._battery_session_start = None
                # Reset latches so a future battery session can fire again
                self._low_fired_this_session = False
                self._critical_fired_this_session = False
            self._fire_state_change(old, new_state, info)

        # If on battery, check threshold events
        if new_state == 'on_battery':
            # Only trust runtime/charge after 90 seconds — early reads
            # are conservative and could trigger false alarms
            session_s = self.battery_session_seconds
            if session_s >= 90:
                self._check_thresholds(info)
            elif session_s == 0:
                # Just transitioned — early state, don't trigger yet
                pass

    def _check_thresholds(self, info: BatteryInfo):
        # Critical takes precedence over low
        critical = (
            (info.runtime_minutes > 0
              and info.runtime_minutes <= self.critical_battery_minutes)
            or info.charge_pct <= self.critical_battery_pct
        )
        low = (
            (info.runtime_minutes > 0
              and info.runtime_minutes <= self.low_battery_minutes)
            or info.charge_pct <= self.low_battery_pct
        )

        if critical and not self._critical_fired_this_session:
            self._critical_fired_this_session = True
            # Critical implies low — fire low first, then critical
            if not self._low_fired_this_session:
                self._low_fired_this_session = True
                self._fire_low_battery(info)
            self._fire_critical(info)
        elif low and not self._low_fired_this_session:
            self._low_fired_this_session = True
            self._fire_low_battery(info)

    def _fire_state_change(self, old: str, new: str, info: BatteryInfo | None):
        if self.on_state_change is not None:
            try:
                self.on_state_change(old, new, info)
            except Exception:
                pass

    def _fire_low_battery(self, info: BatteryInfo):
        if self.on_low_battery is not None:
            try:
                self.on_low_battery(info)
            except Exception:
                pass

    def _fire_critical(self, info: BatteryInfo):
        if self.on_critical is not None:
            try:
                self.on_critical(info)
            except Exception:
                pass


# v4.8.7: Removed Windows shutdown helpers. Earlier versions had
# trigger_graceful_shutdown() and cancel_shutdown() which would call
# `shutdown /s /t 60`, but that's overkill — Windows has its own UPS
# handling and not everyone has a UPS to test it with. Better to focus
# on saving the app's own data and let the OS do its own thing.
