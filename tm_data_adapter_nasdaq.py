"""tm_data_adapter_nasdaq — Nasdaq earnings calendar adapter
(v4.14.5.14-nasdaq-earnings, 2026-05-26).

Keyless earnings source. Nasdaq's public calendar endpoint is BULK-BY-DATE
(one request returns every company reporting on a given date):

    GET https://api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD

The router asks PER-TICKER. This adapter bridges the gap with an internal
rolling cache: on the first earnings query (or when the cache is older than
the TTL) it sweeps a bounded near-term window ONCE, building a
{ticker -> next_event} dict, and answers all per-ticker queries from that dict
— no per-ticker network call.

Coverage note (re-probed 2026-05-28): Nasdaq's calendar is DENSE near-term and
fills on a rolling ~55-day "fill horizon" — real data (incl. large caps) out to
~55 days, then a HARD CLIFF to empty (probed: +55d = 143 rows, +56d = empty).
So the sweep window is bounded at 60 calendar days (the ~55d horizon + margin
for the once-daily sweep to advance); Yahoo (earnings priority 2) covers tickers
reporting beyond the horizon. Widening past 60 only sweeps empty days — see the
_WINDOW_DAYS comment below. Keyless-first: see DECISIONS.md 2026-05-26 + the
4.14.5.20 entry ("Nasdaq earnings = keyless primary; Yahoo = keyless fallback;
Finnhub = keyed bonus").

Failure policy: never raises. Any HTTP / parse / throttle failure -> the
affected date is skipped (or, on a total failure, the cache stays empty) and
the router falls through to Yahoo. A browser User-Agent is REQUIRED — Nasdaq
rejects bot-like UAs.

The router's `days_ahead` kwarg is intentionally IGNORED: Nasdaq is
bulk-by-date, so the horizon is the adapter's own bounded `_WINDOW_DAYS`
(sweeping the router's 200-day request as 200 daily calls would be untenable,
and Nasdaq has no confirmed dates past its ~55-day fill horizon anyway).
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

_BASE_URL = "https://api.nasdaq.com/api/calendar/earnings"
# Browser-style UA — Nasdaq's api.nasdaq.com rejects bot-like agents.
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/124.0 Safari/537.36 TiredMarket/4.14")
# Nasdaq earnings calendar has a rolling ~55-day "fill horizon": dense near-term,
# real data (incl. large caps) out to ~55 days, then a hard cliff to empty.
# Probed live 2026-05-28: +55d = 143 rows, +56d = empty. 60 covers the horizon
# plus margin for daily-sweep advance. Do NOT widen further — 56-95d is empty
# today, so a larger window only sweeps empty days at ~1 extra request/day.
# Stocks reporting beyond the horizon are the irreducible Yahoo/Finnhub-dependent
# residual; widening Nasdaq cannot cover them. See DECISIONS 4.14.5.20.
_WINDOW_DAYS = 60              # near-term horizon to sweep (calendar days)
_CACHE_TTL_SECONDS = 24 * 3600
_PER_REQUEST_DELAY = 0.3      # politeness between per-date requests
_HTTP_TIMEOUT = 12

# Internal rolling cache (module-level; one process).
_lock = threading.Lock()
_cache: dict = {}             # ticker (upper) -> normalized event dict
_cache_at: Optional[float] = None    # epoch of last (attempted) sweep

# v4.14.5.70-backfill-and-nasdaq-persist: persist the rolling cache
# to disk so we don't re-sweep the 45-business-day calendar on every
# process restart. Every OTHER lane (daily_bars, fundamentals,
# filings, per-ticker earnings, news) already has disk persistence
# + freshness gating; this adapter was the lone exception. With the
# 24h TTL honored across restarts, a same-day re-launch finds a
# fresh cache and skips the sweep — which in turn stops the boot-
# time Yahoo cooldown cascade (when nasdaq doesn't cover a ticker
# the earnings router falls through to Yahoo, and many fall-throughs
# in quick succession trip the per-minute rate limit).
# v4.14.6.100-data-dir-resolver: lazy central path.
def _disk_cache_path() -> Path:
    import tm_paths
    return tm_paths.get_data_dir() / 'nasdaq_earnings_calendar.json'

# Idempotent flag so we only rehydrate once per process. The first
# _ensure_fresh() call (or test harness call) loads from disk, then
# the normal staleness check honours that timestamp.
_disk_loaded = False


def _load_disk_cache() -> None:
    """Rehydrate _cache + _cache_at from the on-disk JSON. Safe to
    call repeatedly (the _disk_loaded guard makes it a no-op after
    first success). Never raises — a missing / corrupt / unreadable
    file just leaves _cache empty and _cache_at None, which is the
    pre-fix behaviour."""
    global _cache, _cache_at, _disk_loaded
    if _disk_loaded:
        return
    _disk_loaded = True  # set early so a bad file doesn't loop
    try:
        if not _disk_cache_path().exists():
            return
        with open(_disk_cache_path(), 'r', encoding='utf-8') as f:
            blob = json.load(f)
        if not isinstance(blob, dict):
            return
        cache_at = blob.get('as_of')
        events = blob.get('events') or {}
        if not isinstance(events, dict):
            return
        # Be tolerant of partial/malformed payloads — keep only what
        # looks like a normalized event dict and ignore anything
        # else.
        cleaned: dict = {}
        for tk, ev in events.items():
            if isinstance(tk, str) and isinstance(ev, dict) and ev.get('ticker'):
                cleaned[tk.upper()] = ev
        with _lock:
            _cache = cleaned
            try:
                _cache_at = float(cache_at) if cache_at is not None else None
            except Exception:
                _cache_at = None
        _log(f"[nasdaq] cache rehydrated from disk: "
             f"{len(cleaned)} ticker(s); "
             f"as_of={_cache_at}", 'muted')
    except Exception:
        # Defensive: a corrupt cache file must not break boot. Leave
        # state empty/None so the normal sweep proceeds.
        try:
            with _lock:
                _cache = {}
                _cache_at = None
        except Exception:
            pass


def _save_disk_cache() -> None:
    """Write _cache + _cache_at to the JSON file. Side-effect only;
    never raises. Called after a successful _bulk_prefetch so the
    next restart can skip the sweep when fresh."""
    try:
        with _lock:
            snapshot = {
                'as_of': _cache_at,
                'count': len(_cache),
                'events': dict(_cache),
            }
        _disk_cache_path().parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: tmp + rename so a crash mid-write never
        # leaves a half-written file that fails to parse on next
        # rehydrate.
        tmp_path = _disk_cache_path().with_suffix('.json.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, separators=(',', ':'))
        os.replace(tmp_path, _disk_cache_path())
    except Exception:
        # A write failure must not crash a sweep / cache update — the
        # in-memory state is still valid for this process.
        pass

# v4.14.5.14-nasdaq-observability: the router's note-logger, captured at
# register_with(), so the bulk sweep can emit breadcrumbs to the SAME activity
# log as the "All sources failed" line. None until registered (e.g. in tests).
_log_fn = None


def _log(msg: str, color: str = 'muted') -> None:
    """Emit a sweep breadcrumb to the activity log via the router's note
    logger. No-op when unregistered. Never raises."""
    try:
        if _log_fn is not None:
            _log_fn(msg, color)
    except Exception:
        pass


def _safe_float(v) -> Optional[float]:
    """Parse Nasdaq's string numerics ('$1.23', '1,234', '', 'N/A') -> float
    or None. Never raises."""
    if v is None:
        return None
    s = str(v).strip().replace('$', '').replace(',', '')
    if not s or s.upper() in ('N/A', 'NA', '--'):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _http_get_json(url: str) -> Optional[dict]:
    """GET `url` with a browser UA. Returns parsed dict, or None on ANY
    failure (HTTP error, timeout, non-JSON). Never raises."""
    req = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read().decode('utf-8', 'replace')
        return json.loads(raw)
    except Exception:
        return None


def _hour_from_time(t) -> str:
    """Map Nasdaq's `time` field ('time-pre-market' / 'time-after-hours' /
    'time-not-supplied') to our bmo/amc convention ('' when unknown)."""
    s = (t or '').lower()
    if 'pre-market' in s or 'pre market' in s or 'before' in s:
        return 'bmo'
    if 'after' in s:
        return 'amc'
    return ''


def _row_to_event(row, date_iso: str) -> Optional[dict]:
    """Normalize one Nasdaq calendar row + the queried date into the SAME
    event shape the Yahoo / Finnhub earnings adapters return (so the router
    and consumers can't tell which source served it)."""
    if not isinstance(row, dict):
        return None
    sym = (row.get('symbol') or '').strip().upper()
    if not sym:
        return None
    return {
        'ticker':           sym,
        'date':             date_iso,            # Nasdaq rows have no date — it's the query param
        'eps_estimate':     _safe_float(row.get('epsForecast')),
        'eps_actual':       None,                # calendar is forward-looking
        'revenue_estimate': None,                # Nasdaq calendar omits revenue
        'revenue_actual':   None,
        'hour':             _hour_from_time(row.get('time')),
        'quarter':          None,
        'year':             None,
    }


def _fetch_date(date_iso: str) -> Optional[list]:
    """Fetch one date's earnings rows. Returns a list of raw rows (possibly
    empty) on success, or None on a fetch/parse failure (caller skips it)."""
    data = _http_get_json(f"{_BASE_URL}?date={date_iso}")
    if not isinstance(data, dict):
        return None
    inner = data.get('data')
    if not isinstance(inner, dict):
        # Empty day (weekend / no earnings): Nasdaq may return data=null.
        return []
    rows = inner.get('rows')
    return rows if isinstance(rows, list) else []


def _bulk_prefetch() -> None:
    """Sweep the near-term window (business days only) into a fresh cache.
    Ascending sweep => the EARLIEST occurrence of a ticker wins (its next
    earnings). Always stamps `_cache_at` (even on a partial/empty sweep) so we
    don't re-sweep until the TTL elapses — a failed sweep just means Yahoo
    covers earnings until the next refresh."""
    global _cache, _cache_at
    new_cache: dict = {}
    today = date.today()
    # v4.14.5.14-nasdaq-observability: counters + breadcrumbs so the sweep is
    # visible in the activity log (was a DB-only signal before).
    biz_total = sum(1 for o in range(0, _WINDOW_DAYS + 1)
                    if (today + timedelta(days=o)).weekday() < 5)
    _log(f"[nasdaq] startup sweep: fetching ~{biz_total} business days "
         f"(~{_WINDOW_DAYS}d window)…")
    attempted = 0
    dates_ok = 0
    total_events = 0
    for offset in range(0, _WINDOW_DAYS + 1):
        d = today + timedelta(days=offset)
        if d.weekday() >= 5:          # skip Sat/Sun — no US earnings
            continue
        attempted += 1
        rows = _fetch_date(d.isoformat())
        if rows is None:              # fetch failure for this date — skip
            continue
        dates_ok += 1
        total_events += len(rows)
        for row in rows:
            ev = _row_to_event(row, d.isoformat())
            if ev and ev['ticker'] not in new_cache:
                new_cache[ev['ticker']] = ev    # earliest (next) wins
        time.sleep(_PER_REQUEST_DELAY)
    with _lock:
        _cache = new_cache
        _cache_at = time.time()
    if dates_ok < attempted:
        _log(f"[nasdaq] sweep partial: {dates_ok} of {attempted} dates "
             f"fetched ({attempted - dates_ok} failed); "
             f"{len(new_cache)} tickers cached", 'amber')
    else:
        _log(f"[nasdaq] sweep complete: {dates_ok} dates, {total_events} "
             f"events, {len(new_cache)} unique tickers cached")
    # v4.14.5.70-backfill-and-nasdaq-persist: write the fresh sweep
    # to disk so the next restart can rehydrate and skip the sweep
    # while it's still within the 24h TTL.
    _save_disk_cache()


def _is_cache_stale() -> bool:
    """True when the internal cache has never been built or is older than the
    24h TTL."""
    return _cache_at is None or (time.time() - _cache_at) >= _CACHE_TTL_SECONDS


def _ensure_fresh() -> None:
    """Refresh the internal cache if stale. Guarded so concurrent first-callers
    don't double-sweep: the first claimant stamps `_cache_at` before sweeping,
    so others see a fresh stamp and skip.

    v4.14.5.70-backfill-and-nasdaq-persist: rehydrates the disk cache
    on first call so a same-day restart sees the previous sweep's
    timestamp and skips the network sweep. Without this, _cache_at
    was always None at process start and every restart triggered a
    full 45-business-day re-sweep regardless of the 24h TTL."""
    # First call: pull last sweep's state off disk so the staleness
    # check honors yesterday's sweep. Idempotent.
    _load_disk_cache()
    global _cache_at
    if not _is_cache_stale():
        return
    with _lock:
        if not _is_cache_stale():     # re-check inside the lock
            return
        _cache_at = time.time()       # claim the sweep so others skip
    _bulk_prefetch()


def adapter(profile, data_type: str, **kwargs) -> Optional[dict]:
    """Router adapter entrypoint. Serves 'earnings' per-ticker from the
    internal bulk cache. Returns None for any non-earnings type, a cache miss
    (ticker not reporting inside the window — e.g. foreign/OTC/far-future), or
    any failure — letting the router fall through to Yahoo. Never raises."""
    try:
        if data_type != 'earnings':
            return None
        ticker = (kwargs.get('ticker') or '').strip().upper()
        if not ticker:
            return None
        _ensure_fresh()
        with _lock:
            ev = _cache.get(ticker)
        if not ev:
            return None
        return {
            'events': [dict(ev)],
            'count':  1,
            'as_of':  datetime.now().isoformat(timespec='seconds'),
        }
    except Exception:
        return None


def register_with(router) -> None:
    """Standard router registration — mirrors the other adapters. Also captures
    the router's note-logger so the bulk sweep can log to the activity log
    (v4.14.5.14-nasdaq-observability)."""
    global _log_fn
    _log_fn = getattr(router, '_note', None)
    router.register_adapter('nasdaq', adapter)
