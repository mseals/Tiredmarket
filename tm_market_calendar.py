"""tm_market_calendar — dependency-free, RULE-BASED NYSE/Nasdaq market calendar.

v4.14.6.97-market-holiday-calendar.

The app previously had NO holiday awareness — get_market_status() and
tm_price_service._market_phase() were pure ET-weekday+clock checks that only
knew weekends, so on Juneteenth they reported "OPEN". This module encodes the
NYSE/Nasdaq schedule as RULES (nth-weekday / fixed-date-with-observed-shift /
Good-Friday-from-Easter), so it is correct for ANY year with ~zero maintenance —
no hardcoded date list, no pip dependency.

Stdlib only: `calendar`, `datetime`. US Eastern / DST is computed manually
(2nd Sunday March → 1st Sunday November) so there is NO dependence on `zoneinfo`
+ `tzdata` (which a public Windows install may lack). 0 AI, 0 network.

Layering: this is the PROACTIVE source (names the reason, e.g. "Juneteenth").
The price-movement liveness gate (tm_queue_runner._market_is_live) stays as the
BACKSTOP for surprise / unscheduled closures the calendar can't predict.
"""
from __future__ import annotations

import calendar as _cal
from datetime import date, datetime, timedelta, timezone

# ── Flag (set from cfg by the app at startup; default ON) ────────────────────
_ENABLED = [True]


def set_enabled(value) -> None:
    try:
        _ENABLED[0] = bool(value)
    except Exception:
        _ENABLED[0] = True


def is_enabled() -> bool:
    try:
        return bool(_ENABLED[0])
    except Exception:
        return True


# ── Date helpers (stdlib only) ───────────────────────────────────────────────
def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th `weekday` (Mon=0..Sun=6) of (year, month).
    n >= 1 → nth (e.g. 3rd Monday); n == -1 → last (e.g. last Monday)."""
    if n == -1:
        last_day = _cal.monthrange(year, month)[1]
        d = date(year, month, last_day)
        while d.weekday() != weekday:
            d -= timedelta(days=1)
        return d
    d = date(year, month, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d + timedelta(days=7 * (n - 1))


def easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous/Meeus computus). Good Friday is −2d."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def observed(d: date) -> date:
    """NYSE weekend-shift for FIXED-date holidays: Sat → observed Fri (prev),
    Sun → observed Mon (next). Weekday → unchanged."""
    wd = d.weekday()
    if wd == 5:                       # Saturday
        return d - timedelta(days=1)
    if wd == 6:                       # Sunday
        return d + timedelta(days=1)
    return d


# ── Eastern time / US DST (manual rule — no tzdata dependency) ───────────────
def _us_dst_active(d: date) -> bool:
    """US DST: 2nd Sunday of March 02:00 → 1st Sunday of November 02:00.
    Date-granular (the 02:00 transition edge doesn't affect market-hours)."""
    start = nth_weekday(d.year, 3, 6, 2)   # 2nd Sunday March (Sun=6)
    end = nth_weekday(d.year, 11, 6, 1)    # 1st Sunday November
    return start <= d < end


def et_now() -> datetime:
    """Current US/Eastern wall-clock as a naive datetime, computed from UTC +
    the manual DST rule (EDT=UTC-4 in DST, EST=UTC-5 otherwise)."""
    utc = datetime.now(timezone.utc)
    approx_et_date = (utc + timedelta(hours=-5)).date()
    off = -4 if _us_dst_active(approx_et_date) else -5
    return (utc + timedelta(hours=off)).replace(tzinfo=None)


# ── Holiday rules ────────────────────────────────────────────────────────────
def holidays(year: int) -> dict:
    """{date: name} of full-close NYSE/Nasdaq holidays for `year`."""
    h = {}
    h[observed(date(year, 1, 1))] = "New Year's Day"
    h[nth_weekday(year, 1, 0, 3)] = "MLK Day"               # 3rd Mon Jan
    h[nth_weekday(year, 2, 0, 3)] = "Presidents' Day"       # 3rd Mon Feb
    h[easter(year) - timedelta(days=2)] = "Good Friday"
    h[nth_weekday(year, 5, 0, -1)] = "Memorial Day"         # last Mon May
    h[observed(date(year, 6, 19))] = "Juneteenth"
    h[observed(date(year, 7, 4))] = "Independence Day"
    h[nth_weekday(year, 9, 0, 1)] = "Labor Day"             # 1st Mon Sep
    h[nth_weekday(year, 11, 3, 4)] = "Thanksgiving"         # 4th Thu Nov
    h[observed(date(year, 12, 25))] = "Christmas"
    return h


def early_closes(year: int) -> dict:
    """{date: name} of 1:00pm-ET half-days. Day-after-Thanksgiving (Friday) and
    Christmas Eve when it's a weekday. NOTE: NYSE's Dec-24 rule has weekday-
    dependent edges; we implement the standard "Dec 24 is a 1pm close when it's
    a weekday" — and a full-holiday on Dec 24 (when Dec 25 falls Saturday so
    Christmas is observed Fri Dec 24) is correctly caught by holidays() first
    (full-close wins over early-close in market_status)."""
    e = {}
    thanksgiving = nth_weekday(year, 11, 3, 4)
    e[thanksgiving + timedelta(days=1)] = "Day after Thanksgiving"
    dec24 = date(year, 12, 24)
    if dec24.weekday() < 5:           # weekday only
        e[dec24] = "Christmas Eve"
    return e


def is_holiday(d: date):
    """(True, name) if `d` is a full-close holiday, else (False, None)."""
    try:
        name = holidays(d.year).get(d)
        return (name is not None, name)
    except Exception:
        return (False, None)


def is_early_close(d: date):
    """(True, name) if `d` is a 1pm half-day, else (False, None)."""
    try:
        name = early_closes(d.year).get(d)
        return (name is not None, name)
    except Exception:
        return (False, None)


EARLY_CLOSE_HOUR = 13   # 1:00 PM ET


def market_status(et: datetime | None = None):
    """Single source of truth → (status_str, is_closed_bool, reason|None).
    Keeps the existing OPEN string shape ("OPEN (HH:MM ET)") so callers that
    parse split('(')[0] still read 'OPEN'. Holiday names ride the CLOSED string
    so the header/AI context can show the reason."""
    if et is None:
        et = et_now()
    d = et.date()
    if et.weekday() >= 5:
        return ("CLOSED (Weekend)", True, "Weekend")
    is_hol, hol_name = is_holiday(d)
    if is_hol:
        return (f"CLOSED — {hol_name}", True, hol_name)
    h, m = et.hour, et.minute
    is_ec, ec_name = is_early_close(d)
    if h < 4:
        return ("CLOSED (Pre-market opens 4AM ET)", True, "Overnight")
    if h < 9 or (h == 9 and m < 30):
        return (f"PRE-MARKET ({h}:{m:02d} ET)", False, None)
    if is_ec and h >= EARLY_CLOSE_HOUR:
        return (f"CLOSED — {ec_name} (early close 1PM ET)", True, ec_name)
    if h < 16:
        return (f"OPEN ({h}:{m:02d} ET)", False, None)
    if h < 20:
        return (f"AFTER-HOURS ({h}:{m:02d} ET)", False, None)
    return ("CLOSED", True, None)


def market_phase(et: datetime | None = None) -> str:
    """'open' / 'extended' / 'closed' for price-poll cadence — mirrors
    tm_price_service._market_phase but holiday-aware."""
    if et is None:
        et = et_now()
    if et.weekday() >= 5:
        return 'closed'
    if is_holiday(et.date())[0]:
        return 'closed'
    h, m = et.hour, et.minute
    is_ec = is_early_close(et.date())[0]
    if is_ec and h >= EARLY_CLOSE_HOUR:
        return 'closed'
    if (h == 9 and m >= 30) or (10 <= h < 16):
        return 'open'
    if 4 <= h < 9 or (h == 9 and m < 30) or 16 <= h < 20:
        return 'extended'
    return 'closed'
