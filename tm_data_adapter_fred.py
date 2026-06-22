"""
tm_data_adapter_fred.py — FRED adapter (v4.14.2 stage 4)

What this is:
    The translator between the data router and FRED's REST API
    (Federal Reserve Economic Data, fred.stlouisfed.org). Same
    adapter shape as tm_data_adapter_finnhub: one entrypoint
    `adapter(profile, data_type, **kwargs)` that the router calls.

What it serves:
    - macro (a basket of economic time series via /series/observations)

Key requirement (v4.14.5.14-macro-keyless — NO LONGER REQUIRED):
    FRED is now KEYLESS-FIRST. The default path is FRED's own public
    CSV graph endpoint (fred.stlouisfed.org/graph/fredgraph.csv?id=X
    &cosd=...), date-limited so each response is <1KB. It serves all
    8 series this adapter uses — same agency, same data, same series
    IDs — with no API key. (An earlier note here claimed the CSV
    endpoint "times out under programmatic polling"; that no longer
    reproduces — verified 11/11 fast requests 2026-05-26 — and the
    date-limited query keeps payloads tiny, well clear of any
    anti-scraping threshold.)

    Keyless fallbacks fire only when a FRED-CSV series comes back
    empty: Treasury par-yield-curve XML for 2Y/10Y, and the BLS v1
    public API (keyless, 25 queries/day) for CPI and unemployment.
    Yahoo's keyless macro (VIX, 10Y) is merged in upstream by
    cache.macro(), unchanged.

    A FRED JSON API key remains an OPTIONAL high-ceiling path: if
    profile.key is set, the adapter uses the keyed JSON API (120
    calls/min) instead of the CSV path. Both produce the same output
    shape. The key is a bonus, never required — consistent with the
    keyless-first principle (DECISIONS.md 2026-05-26).

Output shape (macro):
    macro -> {
        'fed_funds':         float | None,    # %, daily DFF
        'treasury_10y':      float | None,    # %, daily DGS10
        'treasury_2y':       float | None,    # %, daily DGS2
        'curve_spread_10y_2y': float | None,  # %, T10Y2Y
        'cpi':               float | None,    # index level, CPIAUCSL
        'cpi_yoy_pct':       float | None,    # derived %
        'unemployment_pct':  float | None,    # %, UNRATE
        'vix':               float | None,    # VIXCLS (FRED's VIX)
        'gdp':               float | None,    # billions $, quarterly GDP
        'series_dates':      {series_id: 'YYYY-MM-DD'},
        'source':            'fred',
        'as_of':             iso_string,
    }

    Returns None when the API key is missing or the network call
    completely fails. Partial results (some series populated, others
    absent) are still returned — the cache merge layer handles
    composition with Yahoo's keyless macro snapshot.

Errors:
    HTTP 400 with "Variable api_key is not set" -> RuntimeError
        ("fred: no API key configured")
    HTTP 429                                    -> RateLimitError
    HTTP 401/403                                -> RuntimeError
        ("fred: HTTP X — check your API key")
    HTTP 5xx                                    -> RuntimeError
        ("fred: HTTP X")
    Network / timeout                           -> ConnectionError
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any

# Import is local to avoid cycles
from tm_data_router import RateLimitError


# ─── Tunable network params ───────────────────────────────────────────

FRED_BASE_URL = 'https://api.stlouisfed.org/fred'
HTTP_TIMEOUT_SEC = 10
USER_AGENT = 'TiredMarket/4.13.42 (Python)'  # frozen Cloudflare signature

# ─── Keyless source endpoints (v4.14.5.14-macro-keyless) ──────────────
# FRED's own public CSV graph endpoint — no key. Date-limited so each
# response is <1KB (just enough rows to contain the latest observation).
FRED_CSV_BASE = 'https://fred.stlouisfed.org/graph/fredgraph.csv'
CSV_LOOKBACK_DAYS = 40

# Treasury par yield curve (keyless XML) — fallback for 2Y/10Y.
TREASURY_YIELD_URL = (
    'https://home.treasury.gov/resource-center/data-chart-center/'
    'interest-rates/pages/xml?data=daily_treasury_yield_curve'
    '&field_tdr_date_value={year}')

# BLS v1 public API (keyless, 25 queries/day) — fallback for CPI +
# unemployment. Map: FRED series id -> (BLS series id, output key).
BLS_V1_URL = 'https://api.bls.gov/publicAPI/v1/timeseries/data/{series_id}'
_BLS_FALLBACK = {
    'CPIAUCSL': ('CUUR0000SA0', 'cpi'),
    'UNRATE':   ('LNS14000000', 'unemployment_pct'),
}


# ─── Default series catalog ──────────────────────────────────────────
#
# Each entry is (FRED series_id, output_key, optional unit hint). The
# adapter fetches all of them in one macro call by default. Callers
# can override via series_ids= kwarg.

_DEFAULT_SERIES = (
    ('DFF',      'fed_funds',          'percent'),
    ('DGS10',    'treasury_10y',       'percent'),
    ('DGS2',     'treasury_2y',        'percent'),
    ('T10Y2Y',   'curve_spread_10y_2y', 'percent'),
    ('CPIAUCSL', 'cpi',                'index'),
    ('UNRATE',   'unemployment_pct',   'percent'),
    ('VIXCLS',   'vix',                'level'),
    ('GDP',      'gdp',                'billions_usd'),
)


# ─── HTTP helper ──────────────────────────────────────────────────────

def _http_get_json(path: str, params: dict, api_key: str) -> Any:
    """GET a FRED endpoint. Adds the api_key + file_type=json. Raises
    structured exceptions for the router to classify."""
    full_params = dict(params)
    full_params['api_key'] = api_key
    full_params['file_type'] = 'json'
    url = f"{FRED_BASE_URL}{path}?{urllib.parse.urlencode(full_params)}"
    req = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT,
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            status = resp.status
            body = resp.read()
            if status == 200:
                try:
                    return json.loads(body.decode('utf-8'))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    raise RuntimeError(f"fred: bad JSON response: {e}")
            elif status == 429:
                raise RateLimitError("fred: 429 Too Many Requests")
            elif status in (401, 403):
                raise RuntimeError(
                    f"fred: HTTP {status} — check your API key")
            else:
                raise RuntimeError(f"fred: HTTP {status}")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimitError("fred: 429 Too Many Requests") from e
        if e.code in (401, 403):
            raise RuntimeError(
                f"fred: HTTP {e.code} — check your API key") from e
        # FRED returns 400 with the helpful 'Variable api_key is not
        # set' body when the key param is absent — not relevant here
        # (we always send the key) but worth normalizing.
        raise RuntimeError(f"fred: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise ConnectionError(f"fred: network error: {e.reason}") from e


# ─── Core fetcher ─────────────────────────────────────────────────────

def _fetch_one_series(api_key: str, series_id: str
                      ) -> tuple[float | None, str | None]:
    """Return (latest_value, latest_date_iso) for a single series, or
    (None, None) if the call returns no usable observation."""
    raw = _http_get_json(
        '/series/observations',
        params={
            'series_id':  series_id,
            'limit':      1,
            'sort_order': 'desc',
        },
        api_key=api_key,
    )
    if not isinstance(raw, dict):
        return None, None
    obs = raw.get('observations') or []
    if not obs:
        return None, None
    o = obs[0]
    raw_val = o.get('value')
    if raw_val is None or raw_val == '.':
        return None, o.get('date')
    try:
        return float(raw_val), o.get('date')
    except (TypeError, ValueError):
        return None, o.get('date')


# ─── Keyless fetchers (v4.14.5.14-macro-keyless) ──────────────────────

def _adapter_log(msg: str, color: str = 'muted') -> None:
    """Route an adapter-side breadcrumb through the app's activity log
    (the router holds the logger; this adapter has no `app` handle).
    print() is only a last-resort fallback that the user won't see."""
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


def _http_get_raw(url: str, timeout: int = HTTP_TIMEOUT_SEC,
                  accept: str | None = None) -> bytes | None:
    """Plain keyless GET. Returns the body bytes on HTTP 200, None on any
    non-200 / network error. Raises RateLimitError on 429 so the router
    can back off (matching the keyed JSON path's behaviour)."""
    headers = {'User-Agent': USER_AGENT}
    if accept:
        headers['Accept'] = accept
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 429:
                raise RateLimitError("fred-keyless: 429 Too Many Requests")
            if resp.status != 200:
                return None
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimitError("fred-keyless: 429") from e
        return None
    except urllib.error.URLError:
        return None


def _fetch_one_series_csv(series_id: str
                          ) -> tuple[float | None, str | None]:
    """Keyless FRED CSV fetch for one series. Date-limited to the last
    CSV_LOOKBACK_DAYS so the response is <1KB; returns the most-recent
    (value, date) with a real numeric value, or (None, last_date)."""
    cosd = (datetime.now() - timedelta(days=CSV_LOOKBACK_DAYS)
            ).strftime('%Y-%m-%d')
    url = (f"{FRED_CSV_BASE}?id={urllib.parse.quote(series_id)}"
           f"&cosd={cosd}")
    body = _http_get_raw(url)  # RateLimitError propagates by design
    if not body:
        return None, None
    last_val: float | None = None
    last_date: str | None = None
    for line in body.decode('utf-8', 'replace').splitlines():
        line = line.strip()
        if not line or ',' not in line:
            continue
        parts = line.split(',')
        if len(parts) < 2:
            continue
        d, v = parts[0].strip(), parts[1].strip()
        if not d or d[0].isalpha():   # header row (observation_date,DFF)
            continue
        if v == '' or v == '.':        # missing obs — keep date, skip value
            last_date = d
            continue
        try:
            last_val, last_date = float(v), d
        except ValueError:
            continue
    return last_val, last_date


def _fetch_treasury_yields() -> dict:
    """Keyless Treasury par-yield-curve XML. Returns {'treasury_2y': x,
    'treasury_10y': y} for the most recent entry (best-effort; {} on any
    failure). Fallback for DGS2/DGS10 when FRED misses."""
    import re
    out: dict = {}
    try:
        url = TREASURY_YIELD_URL.format(year=datetime.now().year)
        body = _http_get_raw(url, accept='application/xml')
        if not body:
            return out
        text = body.decode('utf-8', 'replace')

        def _last(tag: str):
            m = re.findall(rf'<d:{tag}[^>]*>([^<]+)</d:{tag}>', text)
            return m[-1] if m else None

        for tag, key in (('BC_2YEAR', 'treasury_2y'),
                         ('BC_10YEAR', 'treasury_10y')):
            raw = _last(tag)
            if raw is not None:
                try:
                    out[key] = float(raw)
                except ValueError:
                    pass
    except RateLimitError:
        return out
    except Exception:
        return out
    return out


def _fetch_bls_latest(bls_series_id: str) -> float | None:
    """Keyless BLS v1 fetch for one series — returns the latest value,
    or None on failure. Fallback for CPI / unemployment when FRED misses."""
    try:
        url = BLS_V1_URL.format(
            series_id=urllib.parse.quote(bls_series_id))
        body = _http_get_raw(url, accept='application/json')
        if not body:
            return None
        data = json.loads(body.decode('utf-8', 'replace'))
        series = (data.get('Results') or {}).get('series') or []
        if not series:
            return None
        points = series[0].get('data') or []
        for p in points:                       # prefer the flagged latest
            if str(p.get('latest', '')).lower() == 'true':
                return float(p.get('value'))
        if points:                             # else newest-first default
            return float(points[0].get('value'))
    except RateLimitError:
        return None
    except Exception:
        return None
    return None


def _fetch_macro_snapshot(api_key: str,
                           series_ids: list[str] | None = None
                           ) -> dict | None:
    """Batch-fetch the default macro series (or a caller-supplied
    subset) and assemble the normalized snapshot. Returns None only
    when EVERY series fetch failed; partial results otherwise."""
    if series_ids is None:
        catalog = _DEFAULT_SERIES
    else:
        # Filter the default catalog to the requested ids; preserves
        # the canonical output_key mapping.
        wanted = {sid.upper() for sid in series_ids}
        catalog = tuple(
            entry for entry in _DEFAULT_SERIES
            if entry[0].upper() in wanted)
        if not catalog:
            return None

    out: dict = {}
    dates: dict[str, str] = {}
    any_success = False
    keyed = bool(api_key)
    misses: list[tuple[str, str]] = []   # (series_id, out_key) FRED missed
    for series_id, out_key, _unit in catalog:
        try:
            if keyed:
                value, date = _fetch_one_series(api_key, series_id)
            else:
                value, date = _fetch_one_series_csv(series_id)
        except RateLimitError:
            raise
        except Exception:
            value, date = None, None
        if value is not None:
            out[out_key] = value
            any_success = True
        else:
            misses.append((series_id, out_key))
        if date:
            dates[series_id] = date

    # ── Keyless fallbacks — fire ONLY for the series FRED missed ──
    fb_notes: list[str] = []
    miss_keys = {ok for _sid, ok in misses}
    if {'treasury_2y', 'treasury_10y'} & miss_keys:
        ty = _fetch_treasury_yields()
        for ok in ('treasury_2y', 'treasury_10y'):
            if ok in miss_keys and ok in ty:
                out[ok] = ty[ok]
                any_success = True
                fb_notes.append(f"{ok}->treasury")
    for series_id, out_key in misses:
        if series_id in _BLS_FALLBACK and out.get(out_key) is None:
            val = _fetch_bls_latest(_BLS_FALLBACK[series_id][0])
            if val is not None:
                out[out_key] = val
                any_success = True
                fb_notes.append(f"{out_key}->bls")

    if not any_success:
        _adapter_log("[fred] all macro sources failed this cycle", 'amber')
        return None

    # Breadcrumb (observability lives here, not in the cache schema).
    path = 'json(keyed)' if keyed else 'csv(keyless)'
    n_fields = len([1 for _s, ok, _u in catalog if ok in out])
    if fb_notes:
        _adapter_log(
            f"[fred] {path}: {n_fields} fields, {len(fb_notes)} via "
            f"fallback ({', '.join(fb_notes)})")
    else:
        _adapter_log(
            f"[fred] {path}: fetched {n_fields} series, all present")

    if dates:
        out['series_dates'] = dates
    out['source'] = 'fred'
    out['as_of'] = datetime.now().isoformat(timespec='seconds')

    # v4.15.0 step 5: tap into cache.macro_indicators. Side-effect only;
    # caller still receives the same dict.
    try:
        _v415_cache_write_macro_fred(out, catalog)
    except Exception:
        pass

    return out


def _v415_cache_write_macro_fred(snapshot: dict, catalog) -> None:
    """v4.15.0 step 5: FRED macro snapshot → macro_indicators rows.

    Inverts the (series_id → output_key) mapping in the catalog so the
    cache stores rows keyed by canonical FRED series IDs (DFF, DGS10,
    CPIAUCSL, ...) rather than the friendly output keys the prompt
    builder uses. Each (series_id, date) is one row; UPSERT means
    re-fetched values overwrite cleanly.

    Macro is universe-wide so cache_metadata is not maintained per series
    here — daily refresh staleness is handled by the existing 12h memory
    TTL on the cache.macro() path. Side-effect only.
    """
    if not isinstance(snapshot, dict) or not catalog:
        return
    try:
        import tm_cache
    except ImportError:
        return

    dates = snapshot.get('series_dates') or {}
    rows = []
    for entry in catalog:
        try:
            series_id, out_key = entry[0], entry[1]
        except (TypeError, IndexError):
            continue
        value = snapshot.get(out_key)
        if value is None:
            continue
        date = dates.get(series_id)
        if not date:
            continue
        try:
            v = float(value)
            if v != v:  # NaN guard
                continue
        except (TypeError, ValueError):
            continue
        rows.append({
            'series_id': series_id,
            'date': date,
            'value': v,
        })

    if not rows:
        return

    try:
        tm_cache.upsert_macro_indicators(rows)
    except Exception:
        return


# ─── Router-facing entry point ────────────────────────────────────────

def adapter(profile, data_type: str, **kwargs):
    """Function the data router calls.

    Args:
        profile: ProviderProfile (we use profile.key)
        data_type: 'macro' (the only kind FRED serves)
        **kwargs:
            series_ids (list[str], optional): subset of the default
                catalog to fetch. None = all defaults.

    Returns the normalized macro dict (see file header) or None when
    no observations are available.

    Raises:
        RateLimitError on 429
        RuntimeError on auth / server errors (keyed JSON path only)
        ConnectionError on network issues
    No key configured is NOT an error — the keyless CSV path is used.
    """
    # v4.15.0 Step 9: lane_config gate — silent skip when user opted out.
    try:
        import tm_cache as _tm_cache
        _should, _ = _tm_cache.lane_should_fetch('fred')
        if not _should:
            return None
    except Exception:
        pass  # Defensive: cache failure shouldn't break the data path.
    # v4.15.0 Step 12: offline short-circuit.
    try:
        import tm_network as _tmn
        if not _tmn.is_online():
            return None
    except Exception:
        pass

    # v4.14.5.14-macro-keyless: a key is NO LONGER required. When
    # profile.key is empty, _fetch_macro_snapshot uses the keyless FRED
    # CSV path (+ Treasury/BLS fallbacks); when set, it uses the keyed
    # JSON API as an optional higher-ceiling path.
    api_key = profile.key or ''

    if data_type == 'macro':
        return _fetch_macro_snapshot(
            api_key, series_ids=kwargs.get('series_ids'))

    return None


# ─── Registration helper ──────────────────────────────────────────────

def register_with(router) -> None:
    """Convenience for the main app: register this adapter under the
    'fred' provider id."""
    router.register_adapter('fred', adapter)
