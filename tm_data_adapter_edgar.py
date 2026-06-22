"""
tm_data_adapter_edgar.py — SEC EDGAR adapter (v4.13.55)

What this is:
    Adapter for SEC's EDGAR system, the official source of US public
    company filings. Free, no API key, but they require a User-Agent
    header with contact info and rate-limit at 10 req/second.

What it serves:
    - filings (form lists per company: 8-K, 10-Q, 10-K, Form 4, etc.)

What it does NOT serve:
    - Anything else. EDGAR is filings-only.

Two-step lookup:
    EDGAR identifies companies by CIK (Central Index Key), not ticker.
    To resolve a ticker we fetch a small JSON file once per session
    that maps tickers→CIKs. After that everything is direct CIK calls.

Output shape for filings:
    {
        'filings': [
            {'form': '8-K', 'filing_date': '2026-05-01',
             'accession_no': '0001234567-26-000123',
             'primary_document': 'whatever.htm',
             'url': 'https://www.sec.gov/...',
             'description': '...'},
            ...
        ],
        'count': int,
        'cik': '0000123456',
        'company_name': 'COMPANY INC',
        'as_of': iso_string,
    }

Politeness rate limiting:
    SEC's hard cap is ~10/sec. We self-limit to 2/sec at this adapter
    layer. Even if we're called 20 times in a row, we'll stretch them
    out so we never put pressure on SEC's infrastructure.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional

from tm_data_router import RateLimitError, TICKER_UNRESOLVABLE


# ─── Network params ────────────────────────────────────────────────────

EDGAR_TICKERS_URL = 'https://www.sec.gov/files/company_tickers.json'
EDGAR_SUBMISSIONS_URL = 'https://data.sec.gov/submissions/CIK{cik:010d}.json'
EDGAR_FILING_URL_FMT = (
    'https://www.sec.gov/Archives/edgar/data/{cik_int}/'
    '{accession_nodash}/{primary_doc}'
)

# ─── SEC contact resolver (v4.14.5.15-edgar-fundamentals-primary) ─────
#
# EDGAR's fair-use policy asks every client to identify a real operator
# contact so the SEC can warn before blocking a misbehaving client. The
# pre-promotion default shipped a placeholder ('research@example.com')
# which is exactly the shape SEC has been known to soft-block. Now that
# EDGAR is the primary fundamentals source (~50× the prior call volume),
# the contact is resolved from operator config at module-load time:
#
#   1. TIREDMARKET_SEC_CONTACT environment variable (operator-set)
#   2. local config field 'sec_contact_email' (4.14.5.16: WIRED — read
#      directly from data/config.json so this module stays GUI-free;
#      written by the Teacher-AI first-run prompt in tired_market.py)
#   3. a safe GENERIC fallback — no email (real or fake), no personal
#      data; what every un-configured public copy will send. SEC can't
#      warn-before-block on this UA, but they won't see a placeholder
#      either.
#
# The fallback is intentionally honest about its state.
_GENERIC_SEC_CONTACT = 'open-source-research-tool (no contact configured)'


def _read_sec_contact_from_config() -> str:
    """4.14.5.16: read sec_contact_email from data/config.json directly.
    GUI-free (no tired_market import) so the adapter stays independent.
    Never raises — a config read fault must not break EDGAR fetches."""
    try:
        import json as _json
        from pathlib import Path as _Path
        # data/config.json relative to this module's parent dir
        cfg_path = __import__('tm_paths').get_data_dir() / 'config.json'
        if not cfg_path.exists():
            return ''
        with open(cfg_path, encoding='utf-8') as _f:
            data = _json.load(_f)
        if not isinstance(data, dict):
            return ''
        val = data.get('sec_contact_email', '')
        if not isinstance(val, str):
            return ''
        return val.strip()
    except Exception:
        return ''


def _resolve_sec_contact() -> str:
    """Return the operator SEC contact string per the documented order."""
    try:
        contact = (os.environ.get('TIREDMARKET_SEC_CONTACT', '') or '').strip()
    except Exception:
        contact = ''
    if contact:
        return contact
    # (2) local config field — written by the first-run Teacher-AI
    #     prompt in tired_market.py. Lenient sanity check: must contain
    #     '@' to be used; anything else falls through to the generic
    #     fallback so we never ship obvious garbage in the UA.
    cfg_contact = _read_sec_contact_from_config()
    if cfg_contact and '@' in cfg_contact:
        return cfg_contact
    return _GENERIC_SEC_CONTACT


def _build_user_agent() -> str:
    """Compose the EDGAR User-Agent string. Format keeps the app name +
    version family + contact, matching what SEC expects to see in logs."""
    return f'TiredMarket/4.14 ({_resolve_sec_contact()})'


# REQUIRED by SEC. Resolved at import time from operator config (env
# var) so a hardcoded placeholder can never leak into requests. If no
# contact is set, the generic fallback ships — un-configured but not a
# fake email.
DEFAULT_USER_AGENT = _build_user_agent()

HTTP_TIMEOUT_SEC = 15

# Self-imposed rate limit (SEC's actual cap is 10/sec, we go slower).
_MIN_INTERVAL_SEC = 0.5  # 2/sec
_last_call_at = 0.0
_call_lock = threading.Lock()


def _polite_wait():
    """Block until at least _MIN_INTERVAL_SEC has passed since the last
    EDGAR call. Thread-safe.

    v4.14.5.76-adaptive-lane-pacing: `_MIN_INTERVAL_SEC` is now a
    RUNTIME-MUTABLE knob — `tm_lane_pacing` writes to it each
    scheduler tick based on recent outcomes. This function re-reads
    the module attribute every call (Python lookup), so a controller
    update takes effect on the next request without restart.
    """
    global _last_call_at
    with _call_lock:
        now = time.time()
        wait = (_last_call_at + _MIN_INTERVAL_SEC) - now
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.time()


# v4.14.5.76-adaptive-lane-pacing: outcome tap for the controller. Each
# HTTP site calls _record_outcome(t0, http_status_or_None, exc_or_None)
# after the request completes — success path AND exception path. The
# helper is best-effort: any failure inside it (e.g. tm_lane_pacing
# unimported in some bootstrap path) is swallowed so the data fetch
# itself stays the only thing that matters.
def _record_outcome(t0: float,
                    http_status: 'int | None' = None,
                    exc: 'BaseException | None' = None) -> None:
    """Feed one EDGAR-lane outcome to the adaptive controller.

    success = http_status==200 AND exc is None.
    was_429 = http_status==429 OR (exc is urllib.error.HTTPError with .code==429).
    Retry-After parsing is best-effort from exception headers if present.
    """
    try:
        import tm_lane_pacing as _lp
    except Exception:
        return
    latency = max(0.0, time.time() - t0)
    success = False
    was_429 = False
    retry_after: 'float | None' = None
    if exc is not None:
        try:
            import urllib.error as _ue
            if isinstance(exc, _ue.HTTPError):
                if exc.code == 429:
                    was_429 = True
                    # Best-effort Retry-After parse.
                    try:
                        ra = exc.headers.get('Retry-After')
                        if ra is not None:
                            retry_after = float(ra)
                    except Exception:
                        pass
        except Exception:
            pass
    else:
        if http_status is not None:
            if http_status == 200:
                success = True
            elif http_status == 429:
                was_429 = True
    try:
        _lp.record_outcome('edgar',
                           success=success,
                           was_429=was_429,
                           retry_after=retry_after,
                           latency=latency)
    except Exception:
        pass


# ─── Ticker → CIK cache (in-memory, refresh once per session) ─────────

# v4.14.5.9: success-only cache. None means "not yet loaded OR last
# load failed" — never an empty dict cached as if it were a successful
# load (the pre-v4.14.5.9 bug that froze filings for a whole session
# after a single early-session hiccup). Failed loads back off so we
# don't hammer SEC.
_ticker_cik_map: dict[str, dict] | None = None
_ticker_cik_lock = threading.Lock()
_last_failed_load_at: float | None = None
_FAILED_LOAD_BACKOFF_SECONDS = 300  # retry a failed map at most every 5 min

# Rollback switch. App.__init__ calls set_edgar_retry_safe(cfg flag);
# default True. False restores the exact pre-v4.14.5.9 sticky-empty
# behaviour.
_EDGAR_RETRY_SAFE = True
_map_warned = False  # session-once "CIK index not loaded" log guard


def set_edgar_retry_safe(enabled: bool) -> None:
    """v4.14.5.9: rollback hook — cfg['use_edgar_retry_safe']."""
    global _EDGAR_RETRY_SAFE
    _EDGAR_RETRY_SAFE = bool(enabled)


# ─── v4.14.5.13: ticker symbol-format variant fallback ────────────────
#
# The stock universe (from iShares/ITOT) writes dual-class shares with
# NO separator: BRKB, BFB, LENB, UHALB. SEC's company_tickers.json uses
# a hyphen: BRK-B, BF-B, LEN-B, UHAL-B. Exact-match lookup misses ~29
# tickers every pass — ~20 of them real large-caps (Berkshire B,
# Brown-Forman, Lennar, Liberty Media, Under Armour, HEICO, U-Haul,
# Moog, Greif). We try data-driven format variants before giving up,
# and remember the answer per ticker for the session so we resolve (and
# log) each exactly once.

_resolved_ticker_cache: dict[str, tuple | None] = {}
_resolved_cache_lock = threading.Lock()
_VARIANT_FALLBACK_ENABLED = True          # cfg['use_edgar_variant_fallback']
_variant_resolved_count = 0               # session counter (success)
_unresolvable_count = 0                   # session counter (confirmed miss)
_summary_last_logged = (-1, -1)           # (resolved, unresolvable) last summarized

# v4.14.5.14-cadence-dampening-and-f5a-hygiene Part A (2026-05-20):
# persistent no-filer cache layer. The in-memory `_resolved_ticker_cache`
# above only suppresses the "No SEC filings for X" log line WITHIN a
# session; every restart re-resolves the same dead tickers and
# re-fires the log line. Persist confirmed-misses to cache.db with a
# 30-day TTL: company status doesn't flip from "not filed" to "filed
# with SEC" overnight; if it does, the 30-day expiry catches it
# eventually. Cold-start: every confirmed miss writes to disk; subsequent
# startup short-circuits before any HTTP call. Fail-OPEN on any cache
# I/O error — never block a real EDGAR fetch on a cache fault.
_NO_FILER_CACHE_TTL_SECONDS = 30 * 86400  # 30 days
_no_filer_disk_init_lock = threading.Lock()
_no_filer_disk_init_done = False


def _no_filer_cache_init() -> None:
    """One-shot table create. Idempotent + silent on any failure."""
    global _no_filer_disk_init_done
    if _no_filer_disk_init_done:
        return
    with _no_filer_disk_init_lock:
        if _no_filer_disk_init_done:
            return
        try:
            import tm_cache
            conn = tm_cache.get_connection()
            if conn is not None:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS "
                    "edgar_no_filer_cache ("
                    "  ticker TEXT PRIMARY KEY,"
                    "  cached_at INTEGER NOT NULL,"
                    "  expires_at INTEGER NOT NULL)")
                conn.commit()
            _no_filer_disk_init_done = True
        except Exception:
            # Cache layer never blocks the real path.
            _no_filer_disk_init_done = True  # don't retry on every call


def _no_filer_cache_hit(ticker: str) -> bool:
    """Return True iff `ticker` is in the persistent no-filer cache and
    the entry hasn't expired. Any I/O error → False (fall through to
    the live EDGAR resolution path)."""
    try:
        _no_filer_cache_init()
        import tm_cache
        conn = tm_cache.get_connection()
        if conn is None:
            return False
        row = conn.execute(
            "SELECT expires_at FROM edgar_no_filer_cache "
            "WHERE ticker = ?", (ticker,)).fetchone()
        if not row:
            return False
        return int(row[0]) > int(time.time())
    except Exception:
        return False


def _no_filer_cache_write(ticker: str) -> None:
    """Mark `ticker` as a confirmed non-filer for the next TTL window.
    Silent on any I/O error — cache is best-effort, never load-bearing."""
    try:
        _no_filer_cache_init()
        import tm_cache
        conn = tm_cache.get_connection()
        if conn is None:
            return
        now = int(time.time())
        conn.execute(
            "INSERT OR REPLACE INTO edgar_no_filer_cache "
            "(ticker, cached_at, expires_at) VALUES (?, ?, ?)",
            (ticker, now, now + _NO_FILER_CACHE_TTL_SECONDS))
        conn.commit()
    except Exception:
        pass


def set_edgar_variant_fallback(enabled: bool) -> None:
    """v4.14.5.13: rollback hook — cfg['use_edgar_variant_fallback'].
    False restores exact-match-only behaviour (pre-v4.14.5.13)."""
    global _VARIANT_FALLBACK_ENABLED
    _VARIANT_FALLBACK_ENABLED = bool(enabled)


def _adapter_log(msg: str, color: str = 'muted') -> None:
    """Route an adapter-side line through the app's activity log.

    The EDGAR adapter has no `app` handle, and bare print() does NOT
    land in data/activity.log (it goes to the stdout console log the user
    doesn't see). The router DOES hold the activity logger, so we emit
    through it; print() is only a last-resort fallback.
    """
    try:
        import tm_data_router as _r
        rt = _r.get_router()
        if rt is not None:
            rt._note(msg, color)
            return
    except Exception:
        pass
    try:
        print(msg)
    except Exception:
        pass


def _ticker_variants(ticker: str) -> list[str]:
    """Ordered symbol-format variants to try against SEC's CIK map.

    Data-driven, not a hardcoded ticker list:
      1. original                 (BRKB)
      2. hyphen before last char  (BRK-B)
      3. dot before last char     (BRK.B)
      4. hyphen before last 2     (BR-KB)

    Only generates variants for 4+ char tickers whose trailing chars
    are letters (so short tickers like F/T and numeric suffixes aren't
    mangled). Original is always first; first hit wins.
    """
    tk = (ticker or '').strip().upper()
    variants = [tk]
    if len(tk) < 4:
        return variants

    def _add(v: str) -> None:
        if v and v != tk and v not in variants:
            variants.append(v)

    if tk[-1].isalpha():
        _add(tk[:-1] + '-' + tk[-1])      # BRKB -> BRK-B
        _add(tk[:-1] + '.' + tk[-1])      # BRKB -> BRK.B
    if len(tk) >= 4 and tk[-1].isalpha() and tk[-2].isalpha():
        _add(tk[:-2] + '-' + tk[-2:])     # BRKB -> BR-KB
    return variants


def maybe_log_session_summary(force: bool = False) -> None:
    """v4.14.5.13: emit the glance-able session total at most once per
    change, so an overnight activity.log shows the running tally
    without needing to grep. Called at the end of each filings daemon
    pass (tm_fundfile_fetcher)."""
    global _summary_last_logged
    cur = (_variant_resolved_count, _unresolvable_count)
    if not force and cur == _summary_last_logged:
        return
    if cur == (0, 0):
        return
    _summary_last_logged = cur
    _adapter_log(
        f"[edgar] Session summary: {cur[0]} ticker(s) resolved via "
        f"variant fallback, {cur[1]} ticker(s) confirmed unresolvable",
        'muted')


def _parse_cik_index(raw: bytes) -> dict[str, dict]:
    data = json.loads(raw.decode('utf-8'))
    out: dict[str, dict] = {}
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        tk = (entry.get('ticker') or '').upper()
        cik = entry.get('cik_str')
        name = entry.get('title') or ''
        if tk and cik is not None:
            out[tk] = {'cik': int(cik), 'name': name}
    return out


def _load_ticker_cik_map(user_agent: str) -> dict[str, dict] | None:
    """Fetch + parse SEC's ticker→CIK index.

    Returns the populated map on success (cached for the session).
    Returns None when the map is NOT available (never loaded, or last
    load failed and we're within the backoff window) — callers MUST
    treat None as "retry later", distinct from "loaded, ticker absent".

    v4.14.5.9: only a NON-EMPTY successful parse is cached. Failures
    (and successful-but-empty responses) set a backoff timestamp and
    return None so a transient early-session hiccup self-recovers
    instead of freezing the lane until app restart.

    _EDGAR_RETRY_SAFE=False restores the legacy sticky-empty behaviour.
    """
    global _ticker_cik_map, _last_failed_load_at

    with _ticker_cik_lock:
        if _ticker_cik_map is not None:
            return _ticker_cik_map
        if not _EDGAR_RETRY_SAFE:
            pass  # legacy path handled below (no backoff gate)
        elif _last_failed_load_at is not None:
            if (time.time() - _last_failed_load_at
                    < _FAILED_LOAD_BACKOFF_SECONDS):
                return None  # within backoff — signal "retry later"

    try:
        _polite_wait()
        _t0 = time.time()
        req = urllib.request.Request(EDGAR_TICKERS_URL, headers={
            'User-Agent': user_agent,
            'Accept': 'application/json',
        })
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as r:
                raw = r.read()
                _status = getattr(r, 'status', 200)
        except Exception as _e_inner:
            _record_outcome(_t0, http_status=None, exc=_e_inner)
            raise
        _record_outcome(_t0, http_status=_status, exc=None)
        out = _parse_cik_index(raw)
        if out:
            with _ticker_cik_lock:
                _ticker_cik_map = out
                _last_failed_load_at = None
            return out
        # Successful HTTP but empty/garbage parse — treat as failure.
        with _ticker_cik_lock:
            _last_failed_load_at = time.time()
            if not _EDGAR_RETRY_SAFE and _ticker_cik_map is None:
                _ticker_cik_map = {}
                return _ticker_cik_map
        return None
    except Exception:
        with _ticker_cik_lock:
            _last_failed_load_at = time.time()
            if not _EDGAR_RETRY_SAFE:
                # Legacy: sticky empty (the old bug, for rollback only).
                if _ticker_cik_map is None:
                    _ticker_cik_map = {}
                return _ticker_cik_map
        return None  # retry-safe: signal "not loaded, retry later"


def _ticker_to_cik(ticker: str,
                   user_agent: str) -> tuple[int, str] | None | str:
    """Resolve ticker → (cik_int, company_name).

    Returns:
      (cik, name)            — resolved
      None                   — ticker genuinely absent from a LOADED map
      '__MAP_NOT_LOADED__'   — the CIK index isn't loaded this session
                               (transient; caller should report it as
                               such and retry later, NOT as "no data")
    """
    if not ticker:
        return None
    tk = ticker.strip().upper()

    # v4.14.5.13: per-ticker session cache. A resolved (cik,name) or a
    # confirmed-miss (None) is remembered so we resolve+log each ticker
    # exactly once. _MAP_NOT_LOADED is transient and never cached.
    with _resolved_cache_lock:
        if tk in _resolved_ticker_cache:
            return _resolved_ticker_cache[tk]

    # v4.14.5.14-cadence-dampening-and-f5a-hygiene Part A (2026-05-20):
    # persistent no-filer cache short-circuit. Before doing the
    # CIK-map load + variant lookup work, ask the persistent cache.
    # If `tk` was confirmed-not-a-filer on a recent (≤30d) startup,
    # avoid the lookup entirely. Populate the in-memory cache too
    # so subsequent session calls also bypass even this check.
    # Silent on the cold-path (no log) — the goal is QUIET restarts.
    if _no_filer_cache_hit(tk):
        with _resolved_cache_lock:
            _resolved_ticker_cache[tk] = None
        return None

    mp = _load_ticker_cik_map(user_agent)
    if mp is None:
        return _MAP_NOT_LOADED  # transient — do NOT cache, retry later

    if not _VARIANT_FALLBACK_ENABLED:
        # Legacy exact-match path (pre-v4.14.5.13). No cache, no
        # variants, no per-ticker logging — restores old behaviour.
        entry = mp.get(tk)
        if entry is None:
            return None
        return (int(entry['cik']), entry.get('name', ''))

    global _variant_resolved_count, _unresolvable_count
    for v in _ticker_variants(tk):
        entry = mp.get(v)
        if entry is not None:
            resolved = (int(entry['cik']), entry.get('name', ''))
            with _resolved_cache_lock:
                _resolved_ticker_cache[tk] = resolved
            if v != tk:
                _variant_resolved_count += 1
                _adapter_log(
                    f"[edgar] {tk} → {v} resolved (lookup successful)",
                    'muted')
            return resolved

    # Every variant missed — confirmed not an EDGAR filer. Cache the
    # miss (so we never re-log it), log ONCE, return None; the adapter
    # turns this into the TICKER_UNRESOLVABLE sentinel for the router.
    with _resolved_cache_lock:
        _resolved_ticker_cache[tk] = None
    # v4.14.5.14-cadence-dampening-and-f5a-hygiene Part A (2026-05-20):
    # persist the miss to cache.db so future startups short-circuit
    # without the log line.
    _no_filer_cache_write(tk)
    _unresolvable_count += 1
    _adapter_log(
        f"[edgar] No SEC filings for {tk} (not an EDGAR filer — "
        f"likely dual-class variant or delisted)", 'muted')
    return None


_MAP_NOT_LOADED = '__MAP_NOT_LOADED__'


# ─── v4.14.6.44-fundamentals-bulk-index: SEC daily-index reader ───────
#
# Replaces the universe-wide per-ticker scan in the fundamentals daemon.
# Instead of polling 7,200 tickers per cycle to find what's stale, we
# read SEC's DAILY FILINGS INDEX (one HTTP call per business day) and
# diff against our cached have_to_period per ticker. Non-filers never
# appear -> never queued -> no log spam, no Yahoo/Finnhub cooldowns
# from pointless follow-up fetches.
#
# Index format (form.YYYYMMDD.idx):
#   Header lines, then a divider row of dashes, then fixed-width data:
#     Form Type [0:12]    Company Name [12:74]   CIK [74:86]
#     Date Filed [86:98]  File Name [98:]
#   Verified live against form.20260612.idx (HTTP 200, 5,500 lines).

_EDGAR_DAILY_INDEX_URL_FMT = (
    'https://www.sec.gov/Archives/edgar/daily-index/'
    '{year}/QTR{q}/form.{ymd}.idx'
)

_cik_to_ticker_map: dict | None = None
_cik_to_ticker_lock = threading.Lock()


def _get_cik_to_ticker_map(user_agent: str) -> dict:
    """Reverse {cik_int: TICKER} map, built once from
    _load_ticker_cik_map. Returns {} on failure (caller treats every
    daily-index row as a miss and the cycle becomes a no-op rather than
    blowing up). Thread-safe double-checked locking -- build cost only
    happens once per session."""
    global _cik_to_ticker_map
    if _cik_to_ticker_map is not None:
        return _cik_to_ticker_map
    with _cik_to_ticker_lock:
        if _cik_to_ticker_map is not None:
            return _cik_to_ticker_map
        forward = _load_ticker_cik_map(user_agent) or {}
        rev: dict = {}
        for tk, entry in forward.items():
            try:
                if not isinstance(entry, dict):
                    continue
                # _parse_cik_index stores the int CIK under 'cik'.
                cik = entry.get('cik')
                if cik is None:
                    cs = entry.get('cik_str') or entry.get('cikStr')
                    if cs:
                        cik = int(cs)
                if cik is not None:
                    rev[int(cik)] = str(tk).upper()
            except Exception:
                continue
        _cik_to_ticker_map = rev
        return rev


def _fetch_daily_index(date_obj, user_agent: str) -> list:
    """Fetch ONE SEC daily filings index. Returns list of
    (form, cik_int, date_yyyymmdd_str, primary_doc) tuples.

    Weekend dates: skipped without an HTTP call (no index file).
    404 (holidays): returns []. Other failures: returns [].
    Best-effort -- never raises; index reads must never break the
    fundamentals daemon."""
    try:
        # Skip Saturday(5) / Sunday(6).
        if date_obj.weekday() >= 5:
            return []
        y = date_obj.year
        q = (date_obj.month - 1) // 3 + 1
        ymd = date_obj.strftime('%Y%m%d')
        url = _EDGAR_DAILY_INDEX_URL_FMT.format(year=y, q=q, ymd=ymd)
        _polite_wait()
        _t0 = time.time()
        req = urllib.request.Request(url, headers={
            'User-Agent': user_agent,
            'Accept': 'text/plain',
        })
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as r:
                status = getattr(r, 'status', 200)
                raw = r.read()
            _record_outcome(_t0, http_status=status, exc=None)
            if status != 200:
                return []
        except urllib.error.HTTPError as e:
            _record_outcome(_t0, http_status=None, exc=e)
            return []
        except Exception as e:
            _record_outcome(_t0, http_status=None, exc=e)
            return []
        try:
            text = raw.decode('utf-8', errors='replace')
        except Exception:
            return []
        rows: list = []
        lines = text.splitlines()
        # Skip header until the divider row of dashes.
        data_started = False
        for ln in lines:
            if not data_started:
                stripped = ln.strip()
                if stripped and set(stripped) == {'-'}:
                    data_started = True
                continue
            if not ln.strip():
                continue
            # Fixed-width slice. Column positions verified live against
            # form.20260612.idx (regex match across 5 sample data lines):
            #   CIK at [74:86] (12-wide, right-justified) - digits land
            #     in the trailing chars of the slot
            #   Date Filed at [86:99] (13-wide: leading spaces + the
            #     8-digit YYYYMMDD - regex showed digits start at char 91)
            #   File Name from [99:]
            # We strip whitespace then validate the digits explicitly;
            # any malformed row is skipped rather than poisoning results.
            try:
                form = ln[0:12].strip()
                cik_s = ln[74:86].strip()
                date_s = ln[86:99].strip()
                primary = ln[99:].strip()
                if not form or not cik_s or not date_s:
                    continue
                if not cik_s.isdigit():
                    continue
                if not (len(date_s) == 8 and date_s.isdigit()):
                    continue
                cik_i = int(cik_s)
                rows.append((form, cik_i, date_s, primary))
            except Exception:
                continue
        return rows
    except Exception:
        return []


def iter_newly_filed_tickers(since_date_iso: str,
                              forms=None,
                              user_agent: str | None = None) -> dict:
    """Walk SEC daily indexes from (since_date+1) through today, return
    {TICKER: latest_iso_filing_date} for universe tickers whose filings
    matched `forms`.

    forms default: 10-K, 10-Q, 20-F, 10-K/A, 10-Q/A.
    Returns {} on any failure (best-effort).
    Honors _polite_wait() between fetches -> SEC-friendly pacing."""
    try:
        from datetime import date as _date, timedelta as _td
        if forms is None:
            forms = {'10-K', '10-Q', '20-F', '10-K/A', '10-Q/A'}
        forms_up = {str(f).strip().upper() for f in forms}
        ua = user_agent or DEFAULT_USER_AGENT
        try:
            start = _date.fromisoformat(str(since_date_iso)[:10])
        except Exception:
            return {}
        today = _date.today()
        cik_to_tk = _get_cik_to_ticker_map(ua)
        if not cik_to_tk:
            return {}
        out: dict = {}
        d = start + _td(days=1)
        # Bound the walk so a stale cursor can't hammer SEC forever.
        max_days = 120
        steps = 0
        while d <= today and steps < max_days:
            steps += 1
            rows = _fetch_daily_index(d, ua)
            for (form, cik_i, date_s, _doc) in rows:
                f_up = (form or '').strip().upper()
                if f_up not in forms_up:
                    continue
                tk = cik_to_tk.get(int(cik_i))
                if not tk:
                    continue
                # YYYYMMDD -> YYYY-MM-DD
                if len(date_s) == 8 and date_s.isdigit():
                    iso = f"{date_s[0:4]}-{date_s[4:6]}-{date_s[6:8]}"
                else:
                    iso = date_s[:10]
                prev = out.get(tk)
                if prev is None or iso > prev:
                    out[tk] = iso
            d = d + _td(days=1)
        return out
    except Exception:
        return {}


# ─── Submissions fetcher ──────────────────────────────────────────────

def _fetch_submissions(cik: int, user_agent: str) -> dict | None:
    """GET /submissions/CIK{cik:010d}.json. Returns parsed JSON or None."""
    url = EDGAR_SUBMISSIONS_URL.format(cik=cik)
    _polite_wait()
    _t0 = time.time()
    req = urllib.request.Request(url, headers={
        'User-Agent': user_agent,
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as r:
            status = r.status
            body = r.read()
        _record_outcome(_t0, http_status=status, exc=None)
        if status == 200:
            return json.loads(body.decode('utf-8'))
        if status == 429:
            raise RateLimitError("edgar: 429 Too Many Requests")
        return None
    except urllib.error.HTTPError as e:
        _record_outcome(_t0, http_status=None, exc=e)
        if e.code == 429:
            raise RateLimitError("edgar: 429 Too Many Requests") from e
        if e.code == 404:
            return None  # CIK doesn't exist
        raise RuntimeError(f"edgar: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        _record_outcome(_t0, http_status=None, exc=e)
        raise ConnectionError(f"edgar: network error: {e.reason}") from e


def _build_filing_url(cik_int: int, accession_no: str,
                       primary_doc: str) -> str:
    """Construct the URL to the filing's primary document."""
    accession_nodash = accession_no.replace('-', '')
    return EDGAR_FILING_URL_FMT.format(
        cik_int=cik_int,
        accession_nodash=accession_nodash,
        primary_doc=primary_doc or 'index.json',
    )


# ─── v4.14.5.62-insider-flow: Form-4 open-market insider buy/sell signal ──
# Open-market DISCRETIONARY transaction codes — the only ones that reflect an
# insider's own-money bet. P = open-market/private PURCHASE (bullish),
# S = open-market SALE (bearish). EXCLUDED as comp/non-discretionary noise:
# A (grant/award), M (option/derivative exercise), F (tax-withholding
# disposition), G (gift), C/W/J/etc. Verified against real JPM/WMT/NVDA
# Form-4 XML (2026-05).
_FORM4_OPEN_MARKET_CODES = {'P', 'S'}


def _form4_raw_doc(primary_doc: str) -> str:
    """The submissions `primaryDocument` for a Form-4 points at the XSL-
    rendered HTML (e.g. 'xslF345X06/doc4.xml'); the RAW ownership XML is the
    same file WITHOUT the leading 'xsl<...>/' transform-dir segment. Strip it."""
    import re as _re
    return _re.sub(r'^xsl[^/]*/', '', primary_doc or '')


def _fetch_form4_xml(cik_int: int, accession_no: str, primary_doc: str,
                     user_agent: str) -> 'str | None':
    """Fetch ONE Form-4's raw ownership XML, keyless + polite-paced
    (_polite_wait). Returns the XML text, or None on any failure — best-effort
    so one bad filing never breaks the batch. BACKGROUND-ONLY caller."""
    raw_doc = _form4_raw_doc(primary_doc)
    if not raw_doc:
        return None
    url = _build_filing_url(cik_int, accession_no, raw_doc)
    _polite_wait()
    _t0 = time.time()
    req = urllib.request.Request(url, headers={
        'User-Agent': user_agent, 'Accept': 'application/xml'})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as r:
            _status = r.status
            if r.status != 200:
                _record_outcome(_t0, http_status=_status, exc=None)
                return None
            body = r.read()
        _record_outcome(_t0, http_status=_status, exc=None)
        txt = body.decode('utf-8', errors='replace')
        return txt if '<ownershipDocument' in txt else None
    except Exception as _e:
        _record_outcome(_t0, http_status=None, exc=_e)
        return None


def _parse_form4_open_market(xml_text: str) -> list:
    """Parse a Form-4 ownership XML → list of OPEN-MARKET transactions
    [{'code','date','signed_usd'}]. Only non-derivative P (purchase → +$) and
    S (sale → −$); $ = transactionShares × transactionPricePerShare. Defensive:
    a malformed transaction is skipped, never raised."""
    import xml.etree.ElementTree as _ET
    out: list = []
    try:
        root = _ET.fromstring(xml_text)
    except Exception:
        return out
    nd = root.find('nonDerivativeTable')
    if nd is None:
        return out
    for tx in nd.findall('nonDerivativeTransaction'):
        try:
            def _v(path):
                e = tx.find(path)
                if e is None:
                    return None
                vv = e.find('value')
                return (vv.text if vv is not None else e.text)
            code = (_v('transactionCoding/transactionCode') or '').strip().upper()
            if code not in _FORM4_OPEN_MARKET_CODES:
                continue  # exclude grants/exercises/withholding/gifts
            shares = float(_v('transactionAmounts/transactionShares') or 0)
            price = float(_v('transactionAmounts/transactionPricePerShare') or 0)
            if shares <= 0 or price <= 0:
                continue
            usd = shares * price
            out.append({
                'code': code,
                'signed_usd': (usd if code == 'P' else -usd),
                'date': (_v('transactionDate') or '')[:10],
            })
        except Exception:
            continue
    return out


def compute_and_store_insider_flow(ticker: str, filings_payload: dict,
                                   window_days: int = 90,
                                   max_filings: int = 25) -> 'dict | None':
    """v4.14.5.62-insider-flow: from a filings payload (the _normalize_filings
    output — carries Form-4 rows + the cik), fetch+parse each recent Form-4
    within `window_days`, aggregate NET OPEN-MARKET insider $ (+counts), and
    persist to the insider_flow table. BACKGROUND-ONLY — does per-filing EDGAR
    fetches; NEVER call from the lookup/prompt path. Returns the stored row or
    None (nothing open-market in the window). Never raises into the caller."""
    try:
        from datetime import date, datetime, timedelta
        if not isinstance(filings_payload, dict) or not ticker:
            return None
        filings = filings_payload.get('filings') or []
        try:
            cik_int = int(str(filings_payload.get('cik') or '').lstrip('0')
                          or '0')
        except Exception:
            cik_int = 0
        if not cik_int:
            return None
        cutoff = (date.today() - timedelta(days=window_days)).isoformat()
        form4s = [f for f in filings
                  if isinstance(f, dict)
                  and (f.get('form') or '').strip().upper() == '4'
                  and (f.get('filing_date') or '')[:10] >= cutoff
                  and (f.get('primary_document') or '')]
        form4s = form4s[:max_filings]
        if not form4s:
            return None
        ua = _build_user_agent()
        net = 0.0
        n_buys = n_sells = 0
        for f in form4s:
            xml_text = _fetch_form4_xml(
                cik_int, f.get('accession_no') or '',
                f.get('primary_document') or '', ua)
            if not xml_text:
                continue
            for t in _parse_form4_open_market(xml_text):
                net += t['signed_usd']
                if t['code'] == 'P':
                    n_buys += 1
                else:
                    n_sells += 1
        if n_buys == 0 and n_sells == 0:
            return None  # no open-market activity → no (misleading 0) row
        row = {
            'ticker': ticker.upper(),
            'net_open_market_usd': round(net, 2),
            'n_buys': n_buys,
            'n_sells': n_sells,
            'window_days': window_days,
            'computed_at': datetime.now().isoformat(timespec='seconds'),
        }
        try:
            import tm_cache
            tm_cache.upsert_insider_flow(row)
        except Exception:
            return None
        return row
    except Exception:
        return None


def _normalize_filings(submissions: dict, cik: int,
                        company_name: str,
                        form_filter: list[str] | None = None,
                        max_filings: int = 50) -> dict | None:
    """Take raw submissions JSON and return our normalized shape.

    EDGAR puts recent filings in submissions['filings']['recent'] as
    parallel arrays — one array of forms, another of dates, etc.
    We zip them into row-shape and pick the most recent N.
    """
    recent = (submissions.get('filings') or {}).get('recent') or {}
    forms = recent.get('form') or []
    dates = recent.get('filingDate') or []
    accessions = recent.get('accessionNumber') or []
    primary_docs = recent.get('primaryDocument') or []
    descriptions = recent.get('primaryDocDescription') or []

    n = min(len(forms), len(dates), len(accessions), len(primary_docs))
    if n == 0:
        return None

    # Normalize form filter to upper-stripped set
    if form_filter:
        wanted = {f.strip().upper() for f in form_filter}
    else:
        wanted = None

    out_filings = []
    for i in range(n):
        form = (forms[i] or '').upper()
        if wanted is not None and form not in wanted:
            continue
        out_filings.append({
            'form': form,
            'filing_date': dates[i] or '',
            'accession_no': accessions[i] or '',
            'primary_document': primary_docs[i] or '',
            'description': (descriptions[i] if i < len(descriptions) else '') or '',
            'url': _build_filing_url(cik, accessions[i] or '', primary_docs[i] or ''),
        })
        if len(out_filings) >= max_filings:
            break

    if not out_filings:
        return None

    return {
        'filings': out_filings,
        'count': len(out_filings),
        'cik': f'{cik:010d}',
        'company_name': company_name or submissions.get('name', ''),
        'as_of': datetime.now().isoformat(timespec='seconds'),
    }


# ─── Router-facing entry point ────────────────────────────────────────

# ─── Fundamentals via XBRL CompanyFacts (v4.14.5.14-edgar-fundamentals) ──
#
# Keyless authoritative statement data from data.sec.gov/api/xbrl/companyfacts.
# This is a DEEP-statement source (revenue/net_income/margins/assets) — it does
# NOT serve the router 'fundamentals' data_type (that path is the derived-field
# snapshot). It's called directly by tired_market._fetch_fundamentals as the
# top-priority deep fetcher, mirroring the yfinance/finnhub deep fetchers, and
# its row is merged with the others in _v415_cache_read_fundamentals.
EDGAR_COMPANYFACTS_URL = 'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json'

# System field -> ordered XBRL tag alternates. Selection picks the most-recent
# ANNUAL (fp='FY') value ACROSS alternates, so a company that switched tags
# (e.g. Apple: us-gaap:Revenues froze in 2018, now uses
# RevenueFromContractWithCustomerExcludingAssessedTax) gets the fresh value, not
# the stale one. Unit keys: USD for money, 'shares' for share counts,
# 'USD/shares' for EPS.
_XBRL_MONEY = {
    'revenue':           ['RevenueFromContractWithCustomerExcludingAssessedTax',
                          'Revenues', 'RevenueFromContractWithCustomerIncludingAssessedTax'],
    'net_income':        ['NetIncomeLoss'],
    'gross_profit':      ['GrossProfit'],
    'operating_income':  ['OperatingIncomeLoss'],
    'total_assets':      ['Assets'],
    'total_liabilities': ['Liabilities'],
}
_XBRL_SHARES = ['CommonStockSharesOutstanding', 'CommonStockSharesIssued']
_XBRL_EPS = ['EarningsPerShareDiluted', 'EarningsPerShareBasic']


def _latest_fy(us_gaap: dict, tags, unit_keys) -> Optional[tuple]:
    """Across the alternate `tags`, return (end_date, value) for the most-recent
    ANNUAL (fp='FY' or form 10-K) data point. None if no annual point exists.
    Picking by most-recent `end` across alternates avoids stale frozen tags."""
    best = None
    for tag in tags:
        units = (((us_gaap.get(tag) or {}).get('units')) or {})
        for uk in unit_keys:
            for e in (units.get(uk) or []):
                try:
                    if (e.get('fp') == 'FY' or e.get('form') == '10-K') \
                            and e.get('val') is not None and e.get('end'):
                        if best is None or e['end'] > best[0]:
                            best = (e['end'], e['val'])
                except Exception:
                    continue
    return best


def _annual_series(us_gaap: dict, tags, unit_keys, max_years: int = 5) -> list:
    """v4.14.6.76-growth-factor: all ANNUAL (fp='FY' / 10-K) (end_date, value)
    points across the alternate `tags`, deduped to one value per fiscal-year-
    end, sorted ASCENDING by end date, trimmed to the last `max_years`. Mirrors
    _latest_fy's selection but keeps the whole series the companyfacts JSON
    already carries (NO new fetch). Returns [] if none. Never raises."""
    by_end: dict = {}
    try:
        for tag in tags:
            units = (((us_gaap.get(tag) or {}).get('units')) or {})
            for uk in unit_keys:
                for e in (units.get(uk) or []):
                    try:
                        if (e.get('fp') == 'FY' or e.get('form') == '10-K') \
                                and e.get('val') is not None and e.get('end'):
                            by_end[e['end']] = float(e['val'])
                    except Exception:
                        continue
        series = sorted(by_end.items(), key=lambda kv: kv[0])
        return series[-max_years:] if series else []
    except Exception:
        return []


def _cagr(base, latest, years) -> Optional[float]:
    """Compound annual growth rate, or None when undefined (non-positive base/
    latest, zero years). Clamped to [-0.99, 5.0] so a tiny denominator can't
    produce an absurd value. Never raises."""
    try:
        base = float(base); latest = float(latest); years = float(years)
        if base <= 0 or latest <= 0 or years <= 0:
            return None
        v = (latest / base) ** (1.0 / years) - 1.0
        return max(-0.99, min(5.0, v))
    except Exception:
        return None


def _compute_growth_metrics(rev_series: list, eps_series: list) -> dict:
    """v4.14.6.76-growth-factor: derive multi-year growth metrics from the
    ascending annual (end, val) series. Any/all values may be None when the
    window is too short or undefined (negative/zero base). EPS CAGR is computed
    ONLY when the window is clean (all-positive — EPS is noisy / can flip sign).
    Never raises."""
    out = {'revenue_cagr_3y': None, 'revenue_cagr_1y': None,
           'revenue_growth_stability': None, 'eps_cagr_3y': None}
    try:
        rs = [v for _e, v in (rev_series or []) if v is not None]
        n = len(rs)
        if n >= 2:
            out['revenue_cagr_1y'] = _cagr(rs[-2], rs[-1], 1)
            base_idx = max(0, n - 4)          # 4 annual points span ~3 years
            years = (n - 1) - base_idx
            if years >= 2:
                out['revenue_cagr_3y'] = _cagr(rs[base_idx], rs[-1], years)
            ups = sum(1 for i in range(1, n) if rs[i] > rs[i - 1])
            out['revenue_growth_stability'] = round(ups / (n - 1), 3)
        es = [v for _e, v in (eps_series or []) if v is not None]
        m = len(es)
        if m >= 2:
            b_idx = max(0, m - 4)
            yrs = (m - 1) - b_idx
            window = es[b_idx:]
            if yrs >= 2 and all(x > 0 for x in window):
                out['eps_cagr_3y'] = _cagr(es[b_idx], es[-1], yrs)
    except Exception:
        pass
    return out


# v4.14.6.77-quality-factor: additional latest-FY XBRL tags for quality /
# financial-health ratios (extracted from the SAME companyfacts JSON — no new
# fetch). Alternates ordered most-preferred-first.
_XBRL_QUALITY = {
    'equity':              ['StockholdersEquity',
                            'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'],
    'long_term_debt':      ['LongTermDebtNoncurrent', 'LongTermDebt'],
    'debt_current':        ['LongTermDebtCurrent', 'DebtCurrent'],
    'current_assets':      ['AssetsCurrent'],
    'current_liabilities': ['LiabilitiesCurrent'],
    'interest_expense':    ['InterestExpense'],
    'op_cash_flow':        ['NetCashProvidedByUsedInOperatingActivities',
                            'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations'],
}


def _safe_ratio(num, den, lo=None, hi=None) -> Optional[float]:
    """num/den with a positive-denominator guard; optional clamp. None when
    undefined. Never raises."""
    try:
        if num is None or den is None:
            return None
        num = float(num); den = float(den)
        if den <= 0:
            return None
        v = num / den
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v
    except Exception:
        return None


def _compute_quality_metrics(net_income, revenue, operating_income, equity,
                             long_term_debt, debt_current, current_assets,
                             current_liabilities, interest_expense,
                             op_cash_flow) -> dict:
    """v4.14.6.77-quality-factor: profitability / leverage / liquidity /
    coverage / earnings-quality ratios from latest-FY values. Each is guarded
    (positive denominators only) → None when undefined. Never raises. PROFIT-
    ABILITY/HEALTH only — no valuation here (the algo already has a P/E nudge)."""
    out = {'quality_roe': None, 'quality_debt_to_capital': None,
           'quality_current_ratio': None, 'quality_interest_coverage': None,
           'quality_cf_to_sales': None}
    try:
        # ROE = net income / shareholders' equity (equity > 0).
        out['quality_roe'] = _safe_ratio(net_income, equity, lo=-5.0, hi=5.0)
        # Debt-to-capital = total debt / (total debt + equity).
        ltd = float(long_term_debt) if long_term_debt is not None else 0.0
        dc = float(debt_current) if debt_current is not None else 0.0
        total_debt = ltd + dc
        if (long_term_debt is not None or debt_current is not None) \
                and equity is not None:
            cap = total_debt + float(equity)
            if cap > 0:
                out['quality_debt_to_capital'] = max(0.0, min(1.0, total_debt / cap))
        # Current ratio = current assets / current liabilities.
        out['quality_current_ratio'] = _safe_ratio(
            current_assets, current_liabilities, hi=50.0)
        # Interest coverage = operating income / interest expense.
        out['quality_interest_coverage'] = _safe_ratio(
            operating_income, interest_expense, lo=-50.0, hi=200.0)
        # Cash-flow-to-sales = operating cash flow / revenue.
        out['quality_cf_to_sales'] = _safe_ratio(
            op_cash_flow, revenue, lo=-5.0, hi=5.0)
    except Exception:
        pass
    return out


def fetch_fundamentals(ticker: str) -> Optional[list]:
    """Fetch authoritative fundamentals from EDGAR XBRL CompanyFacts and return
    a list with ONE deep-statement row (cache schema), or None.

    Uses the most-recent ANNUAL (10-K / fp='FY') figures for flow + balance-
    sheet items so the row is internally consistent (one fiscal year), with
    margins computed from gross/operating profit. Reuses the EDGAR CIK lookup +
    politeness + User-Agent. Never raises — returns None on CIK miss, transient
    map-not-loaded, HTTP/parse error, or no us-gaap facts (caller falls through
    to the other deep fetchers / Yahoo)."""
    try:
        if not ticker:
            return None
        ua = DEFAULT_USER_AGENT
        resolved = _ticker_to_cik(ticker, ua)
        if resolved is None or resolved == _MAP_NOT_LOADED \
                or not isinstance(resolved, tuple):
            return None
        cik = resolved[0]

        url = EDGAR_COMPANYFACTS_URL.format(cik=int(cik))
        _polite_wait()
        _t0 = time.time()
        req = urllib.request.Request(url, headers={
            'User-Agent': ua, 'Accept': 'application/json',
        })
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as r:
                _status = r.status
                if r.status != 200:
                    _record_outcome(_t0, http_status=_status, exc=None)
                    return None
                data = json.loads(r.read().decode('utf-8'))
            _record_outcome(_t0, http_status=_status, exc=None)
        except urllib.error.HTTPError as e:
            # 404 = no XBRL facts for this CIK; 429/5xx = transient — either
            # way fall through to the next deep fetcher, don't raise.
            _record_outcome(_t0, http_status=None, exc=e)
            return None
        except Exception as _e:
            _record_outcome(_t0, http_status=None, exc=_e)
            return None

        ug = ((data.get('facts') or {}).get('us-gaap')) or {}
        if not ug:
            return None

        vals = {}
        ends = []
        for field, tags in _XBRL_MONEY.items():
            hit = _latest_fy(ug, tags, ('USD',))
            if hit:
                vals[field] = hit[1]
                ends.append(hit[0])
        sh = _latest_fy(ug, _XBRL_SHARES, ('shares',))
        eps = _latest_fy(ug, _XBRL_EPS, ('USD/shares',))
        if sh:
            ends.append(sh[0])

        revenue = vals.get('revenue')
        gross_profit = vals.get('gross_profit')
        operating_income = vals.get('operating_income')
        gross_margin = (gross_profit / revenue
                        if gross_profit is not None and revenue else None)
        operating_margin = (operating_income / revenue
                            if operating_income is not None and revenue else None)
        fiscal_period_end = max(ends) if ends else None

        # v4.14.6.76-growth-factor: retain the multi-year ANNUAL series for
        # revenue + EPS (already present in the companyfacts JSON just parsed)
        # and derive growth metrics. Pure parse — NO new HTTP. The latest-FY
        # fields above are UNCHANGED; growth metrics are ADDED alongside. Any
        # may be None when the history is too short / undefined.
        try:
            _rev_series = _annual_series(ug, _XBRL_MONEY['revenue'], ('USD',))
            _eps_series = _annual_series(ug, _XBRL_EPS, ('USD/shares',))
            _growth = _compute_growth_metrics(_rev_series, _eps_series)
        except Exception:
            _growth = {}

        # v4.14.6.77-quality-factor: extract the extra latest-FY tags for
        # quality/health ratios from the SAME companyfacts JSON (no new fetch)
        # and derive the ratios. Reuses net_income/revenue/operating_income
        # already in `vals`. Any absent tag → that ratio is None (0 at score
        # time, never a penalty).
        try:
            _qv = {}
            for _qf, _qtags in _XBRL_QUALITY.items():
                _qhit = _latest_fy(ug, _qtags, ('USD',))
                if _qhit:
                    _qv[_qf] = _qhit[1]
            _quality = _compute_quality_metrics(
                net_income=vals.get('net_income'), revenue=revenue,
                operating_income=operating_income, equity=_qv.get('equity'),
                long_term_debt=_qv.get('long_term_debt'),
                debt_current=_qv.get('debt_current'),
                current_assets=_qv.get('current_assets'),
                current_liabilities=_qv.get('current_liabilities'),
                interest_expense=_qv.get('interest_expense'),
                op_cash_flow=_qv.get('op_cash_flow'))
        except Exception:
            _quality = {}

        row = {
            'ticker':            ticker.upper(),
            'fiscal_period_end': fiscal_period_end,
            'revenue':           revenue,
            'net_income':        vals.get('net_income'),
            'eps':               (eps[1] if eps else None),
            'gross_margin':      gross_margin,
            'operating_margin':  operating_margin,
            'total_assets':      vals.get('total_assets'),
            'total_liabilities': vals.get('total_liabilities'),
            'shares_outstanding': (int(sh[1]) if sh and sh[1] else None),
            'source':            'edgar',
            'revenue_cagr_3y':          _growth.get('revenue_cagr_3y'),
            'revenue_cagr_1y':          _growth.get('revenue_cagr_1y'),
            'revenue_growth_stability': _growth.get('revenue_growth_stability'),
            'eps_cagr_3y':              _growth.get('eps_cagr_3y'),
            'quality_roe':              _quality.get('quality_roe'),
            'quality_debt_to_capital':  _quality.get('quality_debt_to_capital'),
            'quality_current_ratio':    _quality.get('quality_current_ratio'),
            'quality_interest_coverage': _quality.get('quality_interest_coverage'),
            'quality_cf_to_sales':      _quality.get('quality_cf_to_sales'),
        }
        n_fields = sum(1 for k, v in row.items()
                       if k not in ('ticker', 'source', 'fiscal_period_end')
                       and v is not None)
        if n_fields == 0:
            return None
        # v4.14.6.25-fundfile-log-tag-parity: dual-tag so a tail-grep
        # for `[fundfile]` (the daemon's start-line tag) catches its
        # operational ticks. Pre-fix the start logged `[fundfile]` but
        # every per-ticker fetch logged `[edgar]` only, so the audit's
        # daemon-liveness grep mistakenly thought fundfile had stalled
        # overnight when it was healthy (110 fetches in the first hour).
        _adapter_log(
            f"[fundfile] [edgar] fundamentals fetched for "
            f"{ticker.upper()}: {n_fields} fields, "
            f"as_of={fiscal_period_end}", 'muted')
        return [row]
    except Exception:
        return None


def adapter(profile, data_type: str, **kwargs):
    """Router entry point.

    Args:
        profile: ProviderProfile
        data_type: 'filings' (anything else returns None)
        **kwargs:
            ticker: str (required)
            form_filter: optional list of forms to include (e.g.
                         ['8-K', '10-Q']). None = all forms.
            max_filings: int (default 50)
            user_agent: optional override (default uses adapter constant)

    Returns:
        Normalized filings dict, or None if not found / no filings.

    Raises:
        RateLimitError on 429
        ConnectionError on network issues
        RuntimeError on other server errors
    """
    # v4.14.5.15-edgar-fundamentals-primary: serve the fundamentals
    # data_type as keyless primary. The branch reuses the existing
    # XBRL-companyfacts fetcher (one bulk HTTP call/ticker, already
    # polite-throttled at 2/sec) + the CIK map for company_name.
    # Market-derived fields (market_cap, pe_ratio, beta, dividend_yield)
    # stay None here — the existing _v415_overlay_derived_fundamentals
    # fills them from the legacy snapshot router call. Routing falls
    # through to Yahoo (priority 2) when EDGAR returns None.
    if data_type == 'fundamentals':
        try:
            import tm_network as _tmn
            if not _tmn.is_online():
                return None
        except Exception:
            pass
        ticker = kwargs.get('ticker', '')
        if not ticker:
            return None
        # CIK + company_name resolution (same path filings uses; the
        # CIK map is session-cached so this is in-memory after the first
        # call). _MAP_NOT_LOADED is transient — return None so the
        # router falls back to Yahoo this pass and EDGAR retries next.
        ua = kwargs.get('user_agent') or DEFAULT_USER_AGENT
        resolved = _ticker_to_cik(ticker, ua)
        if resolved is _MAP_NOT_LOADED or resolved == _MAP_NOT_LOADED:
            return None
        if resolved is None or not isinstance(resolved, tuple):
            # Ticker is a confirmed non-EDGAR filer (variant fallback
            # already tried + logged once + cached). Surface the
            # router-level "definitive, do not log every pass" sentinel
            # so the router doesn't spam "All sources failed" for it
            # when Yahoo also rejects it. The Yahoo path will still get
            # tried because the router falls through on None/sentinel.
            if _VARIANT_FALLBACK_ENABLED:
                return TICKER_UNRESOLVABLE
            return None
        _cik, company_name = resolved
        # Existing keyless XBRL fetcher: writes the deep-statement row to
        # cache.fundamentals as a side-effect (so _v415_cache_read_funda-
        # mentals' EDGAR-first merge picks it up) and returns the row.
        rows = fetch_fundamentals(ticker)
        if not rows:
            return None
        row = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(row, dict):
            return None
        # Snapshot key-set MUST match Yahoo's fundamentals snapshot
        # exactly (tm_data_adapter_yahoo.py:272-290) so the FACTS
        # consumer renders identically regardless of source. EDGAR
        # supplies the statement-derived fields (eps, shares_outstanding,
        # fiscal_period_end as 'as_of'); the four market-derived fields
        # (market_cap / pe_ratio / beta / dividend_yield) AND
        # sector/industry stay None — the existing derived-overlay in
        # tired_market._v415_overlay_derived_fundamentals fills them
        # from a separate Yahoo .info call ONLY when actually needed.
        return {
            'company_name':       company_name or '',
            'sector':             None,
            'industry':           None,
            'market_cap':         None,
            'shares_outstanding': row.get('shares_outstanding'),
            'pe_ratio':           None,
            'eps':                row.get('eps'),
            'beta':               None,
            'dividend_yield':     None,
            'as_of':              row.get('fiscal_period_end'),
            'source':             'edgar',
        }

    if data_type != 'filings':
        return None
    # v4.15.0 Step 12: offline short-circuit.
    try:
        import tm_network as _tmn
        if not _tmn.is_online():
            return None
    except Exception:
        pass

    ticker = kwargs.get('ticker', '')
    if not ticker:
        return None

    # Use adapter constant unless caller overrides.
    user_agent = kwargs.get('user_agent') or DEFAULT_USER_AGENT
    form_filter = kwargs.get('form_filter')
    max_filings = int(kwargs.get('max_filings', 50))

    resolved = _ticker_to_cik(ticker, user_agent)
    if resolved is _MAP_NOT_LOADED or resolved == _MAP_NOT_LOADED:
        # v4.14.5.9: the ticker→CIK index isn't loaded this session
        # (transient — will retry after backoff). Surface the REAL
        # failing layer ONCE per session (not 1888 times, not the
        # misleading per-ticker "no data"), then return None quietly
        # so the pass doesn't exception-storm.
        global _map_warned
        if not _map_warned:
            _map_warned = True
            try:
                print("[edgar] ticker->CIK index not loaded this "
                      "session (transient; auto-retry after "
                      "backoff) — filings paused until it loads")
            except Exception:
                pass
        return None
    if resolved is None:
        # Ticker genuinely absent from a LOADED map even after variant
        # fallback (dual-class miss, non-US listing, ETF, delisted) —
        # legitimately no EDGAR filings. _ticker_to_cik already logged
        # + cached this once. v4.14.5.13: return the definitive
        # TICKER_UNRESOLVABLE sentinel so the router stops re-logging
        # "All sources failed for filings" for it every pass. With the
        # rollback flag off, fall back to the legacy bare None.
        # v4.14.5.67-filings-coldfill: tombstone the empty so
        # get_unfilled_tickers('filings') skips it for FILINGS_EMPTY_TTL_DAYS.
        # Was: returned None without persisting → ticker re-entered the
        # unfilled queue on every restart (hundreds of preferred-share
        # series like ABR-PD never get a filings row written).
        try:
            _v415_cache_write_filings_status(ticker, 'empty')
        except Exception:
            pass
        if _VARIANT_FALLBACK_ENABLED:
            return TICKER_UNRESOLVABLE
        return None
    cik, company_name = resolved

    subs = _fetch_submissions(cik, user_agent)
    if subs is None:
        return None

    result = _normalize_filings(
        subs, cik=cik, company_name=company_name,
        form_filter=form_filter, max_filings=max_filings,
    )

    # v4.15.0 step 5: tap into cache.filings. Side-effect only;
    # router still receives the same dict.
    try:
        _v415_cache_write_filings(ticker, result)
    except Exception:
        pass

    # v4.14.5.67-filings-coldfill: tombstone bookkeeping. If EDGAR
    # resolved the ticker BUT _normalize_filings produced no rows
    # (filer with no filings in our form-filter / lookback window),
    # mark 'empty' so the slow-lane stops re-fetching it every pass.
    # If we have a non-empty payload, mark 'ok' to clear any prior
    # 'empty' (a ticker that later starts filing must come back into
    # the unfilled queue).
    try:
        _has_rows = bool(
            result and isinstance(result, dict)
            and (result.get('filings') or []))
        _v415_cache_write_filings_status(
            ticker, 'ok' if _has_rows else 'empty')
    except Exception:
        pass

    return result


def _v415_cache_write_filings_status(ticker: str, status: str) -> None:
    """v4.14.5.67-filings-coldfill: filings-status tombstone writer.
    `status` ∈ 'ok'|'empty'. 'empty' is honored by tm_cache.
    get_unfilled_tickers('filings') for FILINGS_EMPTY_TTL_DAYS, so
    structural non-filers stop re-entering the unfilled queue every
    restart. Caller wraps in try/except — side-effect only."""
    from datetime import datetime as _dt
    try:
        import tm_cache
        tm_cache.upsert_filings_status(
            ticker, status=status,
            as_of=_dt.now().isoformat(),
            source='edgar')
    except Exception:
        pass


def _v415_cache_write_filings(ticker: str, payload: dict | None) -> None:
    """v4.15.0 step 5: EDGAR filings payload → tm_cache.filings rows.

    `payload` is the dict returned by `_normalize_filings` (or None).
    Schema PK is accession_number (globally unique per filing), so UPSERT
    means re-fetched filings overwrite cleanly. `report_date` isn't in
    the normalized shape today — left None; can be filled later if
    EDGAR's reportDate field gets surfaced.

    Side-effect only.
    """
    if not payload or not isinstance(payload, dict) or not ticker:
        return

    filings = payload.get('filings') or []
    if not filings:
        return

    try:
        import tm_cache
    except ImportError:
        return

    cik = payload.get('cik') or ''
    ticker_up = ticker.upper()

    rows = []
    earliest_date = None
    latest_date = None
    for f in filings:
        if not isinstance(f, dict):
            continue
        accession = (f.get('accession_no') or '').strip()
        if not accession:
            continue
        filing_date = (f.get('filing_date') or '').strip()
        if not filing_date:
            continue
        form_type = (f.get('form') or '').strip().upper()
        if not form_type:
            continue
        rows.append({
            'accession_number': accession,
            'ticker': ticker_up,
            'cik': cik,
            'filing_date': filing_date,
            'report_date': None,
            'form_type': form_type,
            'primary_document_url': f.get('url') or None,
            # v4.14.5.62-8k-descriptions: persist EDGAR's primaryDocDescription
            # (already in the normalized payload — no new fetch). NULL when
            # the filing carried no description.
            'description': (f.get('description') or None),
        })
        date_part = filing_date[:10]
        if earliest_date is None or date_part < earliest_date:
            earliest_date = date_part
        if latest_date is None or date_part > latest_date:
            latest_date = date_part

    if not rows:
        return

    try:
        tm_cache.upsert_filings(rows)
    except Exception:
        return

    try:
        existing = tm_cache.get_cache_metadata(ticker_up, 'filings')
        if existing:
            md = existing[0]
            md_keys = md.keys() if hasattr(md, 'keys') else []
            current_from = md['have_from_date'] if 'have_from_date' in md_keys else None
            current_to = md['have_to_date'] if 'have_to_date' in md_keys else None
            new_from = min(earliest_date, current_from) if current_from else earliest_date
            new_to = max(latest_date, current_to) if current_to else latest_date
        else:
            new_from = earliest_date
            new_to = latest_date
        tm_cache.upsert_cache_metadata(
            ticker_up, 'filings',
            have_from_date=new_from,
            have_to_date=new_to,
            fill_source='direct',
        )
    except Exception:
        pass


def register_with(router) -> None:
    router.register_adapter('edgar', adapter)
