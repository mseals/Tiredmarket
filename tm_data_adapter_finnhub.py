"""
tm_data_adapter_finnhub.py — Finnhub adapter (v4.13.55)

What this is:
    The translator between the data router and finnhub.io's REST API.
    The router calls one entrypoint: adapter(profile, data_type, **kw).
    This module translates that into the right HTTP call, parses the
    response, and returns data in a shape the rest of the app expects.

What it serves:
    - news         (per-ticker company news)
    - fundamentals (P/E, market cap, basic financials)
    - earnings     (earnings calendar — date and EPS estimate)

What it does NOT serve:
    - price        (Yahoo's job per architecture decision)
    - history      (Yahoo's job)
    - filings      (EDGAR is the source of truth)

Errors:
    - HTTP 429              -> raises RateLimitError
    - HTTP 401/403          -> raises RuntimeError (bad key)
    - HTTP 5xx              -> raises RuntimeError (server-side)
    - Network / timeout     -> raises ConnectionError
    - Empty or malformed    -> returns None (not a hard failure)

Output shapes (what adapters MUST return on success):
    news -> {
        'headlines': [
            {'title': str, 'summary': str, 'source': str,
             'url': str, 'timestamp_iso': str, 'sentiment': float},
            ...
        ],
        'count': int,
        'as_of': iso_string,
    }

    fundamentals -> {
        'market_cap': float | None,
        'pe_ratio': float | None,
        'eps': float | None,
        'beta': float | None,
        'dividend_yield': float | None,
        'shares_outstanding': float | None,
        'industry': str,
        'sector': str,
        'company_name': str,
        'as_of': iso_string,
    }

    earnings -> {
        'events': [
            {'ticker': str, 'date': 'YYYY-MM-DD', 'eps_estimate': float | None,
             'eps_actual': float | None, 'revenue_estimate': float | None,
             'hour': str},  # 'bmo' / 'amc' / null
            ...
        ],
        'count': int,
    }

The other modules (router, news cache, etc.) already know to expect
these shapes — they came from a deliberate normalization design.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any

# Import is local to avoid cycles
from tm_data_router import RateLimitError


# ─── Tunable network params ───────────────────────────────────────────

FINNHUB_BASE_URL = 'https://finnhub.io/api/v1'
HTTP_TIMEOUT_SEC = 10
USER_AGENT = 'TiredMarket/4.13.55'


# ─── Sentiment scoring ────────────────────────────────────────────────
#
# Finnhub doesn't return per-headline sentiment. We do a lightweight
# keyword scan to assign a -1..+1 score so the existing news features
# code (which expects a 'sentiment' field) keeps working.

_POSITIVE_TERMS = (
    'beats', 'beat', 'surge', 'surges', 'jumps', 'jumped', 'rallies',
    'rally', 'soars', 'soared', 'gains', 'gained', 'upgrade', 'upgraded',
    'buy', 'bullish', 'outperform', 'positive', 'breakout', 'record high',
    'partnership', 'acquires', 'acquisition', 'wins', 'won', 'approval',
    'approved', 'launches', 'launched', 'expansion', 'profit', 'profits',
    'raises guidance', 'beat estimates', 'exceeded',
)
_NEGATIVE_TERMS = (
    'miss', 'misses', 'missed', 'plunge', 'plunges', 'plunged', 'drops',
    'dropped', 'falls', 'fell', 'tumbles', 'tumbled', 'crashes', 'crashed',
    'downgrade', 'downgraded', 'sell', 'bearish', 'underperform',
    'negative', 'breakdown', 'record low', 'lawsuit', 'sued', 'investigation',
    'fraud', 'scandal', 'recall', 'cuts', 'cut', 'loss', 'losses',
    'lowers guidance', 'missed estimates', 'shortfall', 'concern',
)


def _score_headline(text: str) -> float:
    """Lightweight sentiment score from -1 (bad) to +1 (good).
    Mirrors the existing _score_headline pattern in tired_market.py
    but is self-contained here."""
    if not text:
        return 0.0
    t = text.lower()
    pos = sum(1 for term in _POSITIVE_TERMS if term in t)
    neg = sum(1 for term in _NEGATIVE_TERMS if term in t)
    if pos == 0 and neg == 0:
        return 0.0
    raw = (pos - neg) / max(pos + neg, 1)
    return max(-1.0, min(1.0, raw))


# ─── HTTP helper ──────────────────────────────────────────────────────

def _http_get_json(url: str, params: dict, api_key: str) -> Any:
    """GET a Finnhub endpoint with standard error handling.

    Raises:
        RateLimitError on HTTP 429
        RuntimeError on auth or server errors
        ConnectionError on network issues
    Returns parsed JSON on 200.
    """
    # Finnhub takes the key in either header or query param.
    # Header is cleaner — keys don't end up in logs / Referer.
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full_url, headers={
        'X-Finnhub-Token': api_key,
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
                    raise RuntimeError(f"finnhub: bad JSON response: {e}")
            elif status == 429:
                raise RateLimitError("finnhub: 429 Too Many Requests")
            elif status in (401, 403):
                raise RuntimeError(
                    f"finnhub: HTTP {status} — check your API key")
            else:
                raise RuntimeError(f"finnhub: HTTP {status}")
    except urllib.error.HTTPError as e:
        # urlopen raises HTTPError for 4xx/5xx by default
        if e.code == 429:
            raise RateLimitError("finnhub: 429 Too Many Requests") from e
        elif e.code in (401, 403):
            raise RuntimeError(
                f"finnhub: HTTP {e.code} — check your API key") from e
        else:
            raise RuntimeError(f"finnhub: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise ConnectionError(f"finnhub: network error: {e.reason}") from e


# ─── Endpoint-specific fetchers ───────────────────────────────────────

def _fetch_company_news(api_key: str, ticker: str, days_back: int = 7
                         ) -> dict | None:
    """GET /company-news. Returns normalized news shape or None on
    empty result."""
    end = datetime.now().date()
    start = end - timedelta(days=max(1, days_back))
    raw = _http_get_json(
        f'{FINNHUB_BASE_URL}/company-news',
        params={
            'symbol': ticker.upper(),
            'from': start.isoformat(),
            'to': end.isoformat(),
        },
        api_key=api_key,
    )
    if not isinstance(raw, list):
        return None
    headlines = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = (item.get('headline') or '').strip()
        if not title or len(title) < 5:
            continue
        ts_unix = item.get('datetime') or 0
        try:
            ts_iso = datetime.fromtimestamp(int(ts_unix)).isoformat() if ts_unix else ''
        except (ValueError, OSError):
            ts_iso = ''
        headlines.append({
            'title': title,
            'summary': (item.get('summary') or '').strip()[:500],
            'source': (item.get('source') or 'finnhub').strip(),
            'url': (item.get('url') or '').strip(),
            'timestamp_iso': ts_iso,
            'sentiment': _score_headline(title),
        })
    if not headlines:
        return None
    return {
        'headlines': headlines,
        'count': len(headlines),
        'as_of': datetime.now().isoformat(timespec='seconds'),
    }


def _fetch_fundamentals(api_key: str, ticker: str) -> dict | None:
    """Combine /stock/profile2 (company info) + /stock/metric (financials).
    Two calls but both light. Returns None if both fail or are empty."""
    profile = None
    metrics = None
    # Profile call
    try:
        profile = _http_get_json(
            f'{FINNHUB_BASE_URL}/stock/profile2',
            params={'symbol': ticker.upper()},
            api_key=api_key,
        )
    except RateLimitError:
        raise  # propagate
    except Exception:
        profile = None

    # Metrics call
    try:
        metrics = _http_get_json(
            f'{FINNHUB_BASE_URL}/stock/metric',
            params={'symbol': ticker.upper(), 'metric': 'all'},
            api_key=api_key,
        )
    except RateLimitError:
        raise
    except Exception:
        metrics = None

    if not profile and not metrics:
        return None

    # Profile fields
    company_name = (profile or {}).get('name', '') or ''
    industry = (profile or {}).get('finnhubIndustry', '') or ''
    market_cap = (profile or {}).get('marketCapitalization')  # in $M
    shares_out = (profile or {}).get('shareOutstanding')      # in M

    # Metrics — Finnhub puts the actual numbers in metric.metric
    metric_dict = ((metrics or {}).get('metric') or {}) if isinstance(metrics, dict) else {}

    def _num(key: str) -> float | None:
        v = metric_dict.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    result = {
        'company_name': company_name,
        'industry': industry,
        'sector': industry,  # Finnhub doesn't split sector vs industry
        'market_cap': float(market_cap) * 1_000_000 if market_cap else None,
        'shares_outstanding': float(shares_out) * 1_000_000 if shares_out else None,
        'pe_ratio': _num('peBasicExclExtraTTM') or _num('peTTM'),
        'eps': _num('epsBasicExclExtraTTM') or _num('epsTTM'),
        'beta': _num('beta'),
        'dividend_yield': _num('currentDividendYieldTTM'),
        'as_of': datetime.now().isoformat(timespec='seconds'),
    }

    # v4.15.0 step 5: tap into cache.fundamentals. Side-effect only;
    # router still receives the same dict.
    try:
        _v415_cache_write_fundamentals(ticker, result)
    except Exception:
        pass

    return result


def _v415_cache_write_fundamentals(ticker: str, data: dict) -> None:
    """v4.15.0 step 5: Finnhub fundamentals snapshot → tm_cache.fundamentals row.

    Finnhub returns a current-snapshot dict (eps, pe_ratio, shares_outstanding,
    market_cap, etc.) rather than quarterly financial-statement rows. The
    schema's fiscal_period_end column is therefore a synthetic '__current__'
    marker so successive snapshots UPSERT into one row per (ticker, source).
    Statement columns (revenue, net_income, total_assets, ...) stay None —
    Finnhub's free tier doesn't expose them.

    Side-effect only. Failures are silently swallowed so the data path is
    never disturbed.
    """
    if not data or not isinstance(data, dict) or not ticker:
        return

    try:
        import tm_cache
    except ImportError:
        return

    def _f(v):
        try:
            if v is None:
                return None
            fv = float(v)
            if fv != fv:
                return None
            return fv
        except (TypeError, ValueError):
            return None

    def _i(v):
        try:
            if v is None:
                return None
            fv = float(v)
            if fv != fv:
                return None
            return int(fv)
        except (TypeError, ValueError):
            return None

    row = {
        'ticker': ticker.upper(),
        'fiscal_period_end': '__current__',
        'revenue': None,
        'net_income': None,
        'eps': _f(data.get('eps')),
        'gross_margin': None,
        'operating_margin': None,
        'total_assets': None,
        'total_liabilities': None,
        'shares_outstanding': _i(data.get('shares_outstanding')),
        'source': 'finnhub',
    }

    try:
        tm_cache.upsert_fundamentals([row])
    except Exception:
        return

    try:
        today = tm_cache.iso_now()[:10]
        existing = tm_cache.get_cache_metadata(ticker.upper(), 'fundamentals')
        if existing:
            md = existing[0]
            md_keys = md.keys() if hasattr(md, 'keys') else []
            current_from = md['have_from_date'] if 'have_from_date' in md_keys else None
            current_to = md['have_to_date'] if 'have_to_date' in md_keys else None
            new_from = min(today, current_from) if current_from else today
            new_to = max(today, current_to) if current_to else today
        else:
            new_from = today
            new_to = today
        tm_cache.upsert_cache_metadata(
            ticker.upper(), 'fundamentals',
            have_from_date=new_from,
            have_to_date=new_to,
            fill_source='direct',
        )
    except Exception:
        pass


def _fetch_financials_finnhub(api_key: str, ticker: str,
                                freq: str = 'quarterly') -> list[dict] | None:
    """v4.15.0 Step 14: Fetch detailed financial statements from Finnhub.

    Hits /stock/financials-reported and returns one row per reporting period
    with the cache-schema columns populated wherever Finnhub provides them.
    Each row is also written to cache as a side-effect (UPSERT keyed by
    (ticker, fiscal_period_end, source)).

    freq: 'quarterly' (default) or 'annual'. Anything else coerced to quarterly.

    Returns None on missing key, ticker, network/quota failure, empty payload,
    or any translator-level failure that produces zero usable rows.
    """
    if not api_key or not ticker:
        return None
    if freq not in ('quarterly', 'annual'):
        freq = 'quarterly'

    try:
        result = _http_get_json(
            f'{FINNHUB_BASE_URL}/stock/financials-reported',
            params={'symbol': ticker.upper(), 'freq': freq},
            api_key=api_key,
        )
    except RateLimitError:
        raise
    except Exception:
        return None

    if not result or not isinstance(result, dict):
        return None
    data_array = result.get('data')
    if not data_array or not isinstance(data_array, list):
        return None

    output_rows = []
    for period in data_array:
        try:
            row = _v415_translate_finnhub_financials_period(ticker, period)
            if row:
                output_rows.append(row)
        except Exception:
            continue

    if not output_rows:
        return None

    try:
        _v415_cache_write_deep_fundamentals(ticker, output_rows, source='finnhub_deep')
    except Exception:
        pass

    return output_rows


def _v415_translate_finnhub_financials_period(ticker: str, period: dict) -> dict | None:
    """v4.15.0 Step 14: Map one Finnhub /financials-reported period to a cache row.

    Finnhub's bs/ic/cf arrays are not a fixed schema — labels vary by filer
    (e.g. "Total Revenue" vs "Revenues" vs "Net Sales"). Fuzzy substring match
    on a candidate list per column handles the common variants. NaN- and
    type-guarded at every numeric extraction.

    Margins are derived when the raw numerator + revenue are present and
    revenue is non-zero; otherwise None.
    """
    if not period or not isinstance(period, dict):
        return None

    end_date = period.get('endDate')
    if not end_date:
        return None

    report = period.get('report') or {}
    bs = report.get('bs') or []  # balance sheet
    ic = report.get('ic') or []  # income statement

    def _find_value(items: list, candidate_labels: list) -> float | None:
        for item in items:
            try:
                label = (item.get('label') or '').lower()
                for candidate in candidate_labels:
                    if candidate.lower() in label:
                        value = item.get('value')
                        if value is None:
                            continue
                        try:
                            fv = float(value)
                            if fv != fv:  # NaN
                                continue
                            return fv
                        except (TypeError, ValueError):
                            continue
            except Exception:
                continue
        return None

    revenue = _find_value(ic, ['total revenue', 'revenues', 'net sales', 'sales'])
    net_income = _find_value(ic, ['net income', 'net earnings'])
    total_assets = _find_value(bs, ['total assets'])
    total_liabilities = _find_value(bs, ['total liabilities'])
    gross_profit = _find_value(ic, ['gross profit'])
    operating_income = _find_value(ic, ['operating income', 'operating earnings'])

    gross_margin = ((gross_profit / revenue)
                     if (gross_profit is not None and revenue and revenue != 0)
                     else None)
    operating_margin = ((operating_income / revenue)
                         if (operating_income is not None and revenue and revenue != 0)
                         else None)

    eps = _find_value(ic, [
        'earnings per share diluted',
        'diluted earnings per share',
        'earnings per share',
    ])

    shares_outstanding = _find_value(bs, [
        'shares outstanding',
        'common shares outstanding',
    ])

    return {
        'ticker': ticker.upper(),
        'fiscal_period_end': end_date,
        'revenue': revenue,
        'net_income': net_income,
        'eps': eps,
        'gross_margin': gross_margin,
        'operating_margin': operating_margin,
        'total_assets': total_assets,
        'total_liabilities': total_liabilities,
        'shares_outstanding': int(shares_outstanding) if shares_outstanding else None,
        'source': 'finnhub_deep',
    }


def _v415_cache_write_deep_fundamentals(ticker: str, rows: list,
                                          source: str = 'finnhub_deep') -> None:
    """v4.15.0 Step 14: Bulk-write deep fundamentals rows to tm_cache.

    UPSERT keyed by (ticker, fiscal_period_end, source). Widens cache_metadata
    have_from/have_to to span the fetched period range. No-op on empty input.
    All failures silently swallowed — never disturb the data path.
    """
    if not rows:
        return
    try:
        import tm_cache
    except ImportError:
        return

    try:
        tm_cache.upsert_fundamentals(rows)
    except Exception:
        return

    try:
        period_dates = [r['fiscal_period_end'] for r in rows
                         if r.get('fiscal_period_end')]
        if not period_dates:
            return
        earliest = min(period_dates)
        latest = max(period_dates)

        existing_md = tm_cache.get_cache_metadata(ticker.upper(), 'fundamentals')
        if existing_md and len(existing_md) > 0:
            md = existing_md[0]
            md_keys = md.keys() if hasattr(md, 'keys') else []
            current_from = md['have_from_date'] if 'have_from_date' in md_keys else None
            current_to = md['have_to_date'] if 'have_to_date' in md_keys else None
            new_from = min(earliest, current_from) if current_from else earliest
            new_to = max(latest, current_to) if current_to else latest
        else:
            new_from = earliest
            new_to = latest

        tm_cache.upsert_cache_metadata(
            ticker.upper(), 'fundamentals',
            have_from_date=new_from,
            have_to_date=new_to,
            fill_source='direct',
        )
    except Exception:
        pass


def _fetch_earnings_calendar(api_key: str,
                              days_back: int = 0,
                              days_ahead: int = 14,
                              ticker: str | None = None) -> dict | None:
    """GET /calendar/earnings. Returns events for the date window.

    v4.14.2 stage 1: when ticker is set, the symbol= parameter is
    passed in the request URL so Finnhub returns just that ticker's
    events instead of the truncated bulk response.

    Pre-v4.14.2 the function accepted ticker= but only used it for
    client-side filtering of the bulk response; the API request
    itself was identical to the ticker=None bulk call. Finnhub's
    /calendar/earnings silently caps unfiltered bulk responses at
    1500 events and returns the FAR end of the date window — so a
    per-ticker call without symbol= got the same truncated 1500
    events the bulk call did, then filtered client-side, and
    typically returned None for any near-term event.

    With symbol= in the URL, Finnhub returns the actual events for
    that ticker (verified against AAPL's 2026-07-29 event during
    the v4.14.1.3 diagnostic). The bulk path (ticker=None) keeps
    the existing behavior — no symbol filter, accepts the
    truncation as a known limitation for the cheap-bulk-fetch
    pattern.
    """
    today = datetime.now().date()
    start = today - timedelta(days=max(0, days_back))
    end = today + timedelta(days=max(1, days_ahead))
    params = {
        'from': start.isoformat(),
        'to':   end.isoformat(),
    }
    if ticker:
        params['symbol'] = ticker.upper()
    raw = _http_get_json(
        f'{FINNHUB_BASE_URL}/calendar/earnings',
        params=params,
        api_key=api_key,
    )
    if not isinstance(raw, dict):
        return None
    raw_events = raw.get('earningsCalendar') or []
    out_events = []
    target_ticker = ticker.upper() if ticker else None
    for e in raw_events:
        if not isinstance(e, dict):
            continue
        sym = (e.get('symbol') or '').upper()
        if not sym:
            continue
        if target_ticker and sym != target_ticker:
            continue
        out_events.append({
            'ticker': sym,
            'date': e.get('date') or '',
            'eps_estimate': _safe_float(e.get('epsEstimate')),
            'eps_actual': _safe_float(e.get('epsActual')),
            'revenue_estimate': _safe_float(e.get('revenueEstimate')),
            'revenue_actual': _safe_float(e.get('revenueActual')),
            'hour': e.get('hour') or '',
            'quarter': e.get('quarter'),
            'year': e.get('year'),
        })
    if not out_events:
        return None
    return {
        'events': out_events,
        'count': len(out_events),
        'window_start': start.isoformat(),
        'window_end': end.isoformat(),
        'as_of': datetime.now().isoformat(timespec='seconds'),
    }


def _safe_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─── v4.15.0 Step 10: social-sentiment fetcher + cache tap ───────────

def _fetch_social_sentiment(api_key: str, ticker: str,
                             days: int = 7) -> dict | None:
    """Fetch Finnhub /stock/social-sentiment for a ticker.

    Returns aggregated Reddit + Twitter mention data over the last
    `days` calendar days, normalized to the existing _fetch_* style:

        {
            'symbol': str,
            'reddit':  [{'atTime': str, 'mention': int,
                         'positiveScore': float, 'negativeScore': float,
                         'positiveMention': int, 'negativeMention': int,
                         'score': float}, ...],
            'twitter': [...same shape...],
            'as_of':   iso_string,
        }

    Returns None when the API returns no entries (not an error — just
    empty) or when the response shape is malformed.

    Raises:
        RateLimitError on 429
        RuntimeError on auth / server errors
        ConnectionError on network issues

    Side-effect: on a non-empty result, the dict is written into
    tm_cache.social_signals via `_v415_cache_write_finnhub_social`.
    """
    today = datetime.now().date()
    start = today - timedelta(days=max(1, days))
    raw = _http_get_json(
        f'{FINNHUB_BASE_URL}/stock/social-sentiment',
        params={
            'symbol': ticker.upper(),
            'from':   start.isoformat(),
            'to':     today.isoformat(),
        },
        api_key=api_key,
    )
    if not isinstance(raw, dict):
        return None
    reddit_arr = raw.get('reddit') or []
    twitter_arr = raw.get('twitter') or []
    if not isinstance(reddit_arr, list):
        reddit_arr = []
    if not isinstance(twitter_arr, list):
        twitter_arr = []
    if not reddit_arr and not twitter_arr:
        return None

    result = {
        'symbol':  (raw.get('symbol') or ticker).upper(),
        'reddit':  reddit_arr,
        'twitter': twitter_arr,
        'as_of':   datetime.now().isoformat(timespec='seconds'),
    }

    # v4.15.0 Step 10: tap into cache.social_signals. Side-effect only.
    try:
        _v415_cache_write_finnhub_social(ticker, result)
    except Exception:
        pass

    return result


def _v415_cache_write_finnhub_social(ticker: str,
                                      sentiment_data: dict) -> None:
    """v4.15.0 Step 10: Side-effect write of Finnhub social-sentiment to
    tm_cache.social_signals.

    Translates Finnhub's reddit + twitter sub-arrays into individual
    rows tagged `source='finnhub_reddit'` / `source='finnhub_twitter'`.
    Dedups by (source, timestamp) against the most recent 100 cached
    social rows for the ticker. Sentiment is computed as
    `positiveScore - negativeScore`, clamped to [-1, +1] to match the
    schema's expected range (same convention used by StockTwits in
    Step 5).

    Coexists with StockTwits' source='stocktwits' rows in the same
    table; the source column is the discriminator. message_count is
    Finnhub's `mention` field per dated entry.

    Updates cache_metadata for the (ticker, 'social_signals') lane
    with widening date range. Best-effort: all failures silently
    swallowed.
    """
    if not sentiment_data or not isinstance(sentiment_data, dict) or not ticker:
        return
    try:
        import tm_cache
    except ImportError:
        return

    ticker_up = ticker.upper()

    # Build dedup set from the most recent 100 cached social rows.
    try:
        recent = tm_cache.get_social_signals(ticker_up, since=None, limit=100)
        existing_keys = set()
        for r in recent:
            try:
                rkeys = r.keys() if hasattr(r, 'keys') else []
                src = r['source'] if 'source' in rkeys else None
                ts = r['timestamp'] if 'timestamp' in rkeys else None
                existing_keys.add((src, ts))
            except Exception:
                continue
    except Exception:
        existing_keys = set()

    rows_to_insert = []
    earliest_date = None
    latest_date = None

    for platform_key in ('reddit', 'twitter'):
        platform_data = sentiment_data.get(platform_key) or []
        if not isinstance(platform_data, list):
            continue
        source_name = f'finnhub_{platform_key}'

        for entry in platform_data:
            if not isinstance(entry, dict):
                continue
            try:
                ts = entry.get('atTime')
                if not ts:
                    continue
                ts = str(ts)

                key = (source_name, ts)
                if key in existing_keys:
                    continue
                existing_keys.add(key)  # In-batch dedup too.

                positive = entry.get('positiveScore')
                negative = entry.get('negativeScore')
                sentiment_val = None
                if positive is not None and negative is not None:
                    try:
                        fv = float(positive) - float(negative)
                        if fv == fv:  # NaN guard
                            sentiment_val = max(-1.0, min(1.0, fv))
                    except (TypeError, ValueError):
                        sentiment_val = None

                mention = entry.get('mention')
                msg_count = None
                if mention is not None:
                    try:
                        mv = float(mention)
                        if mv == mv:
                            msg_count = int(mv)
                    except (TypeError, ValueError):
                        msg_count = None

                rows_to_insert.append({
                    'ticker': ticker_up,
                    'timestamp': ts,
                    'source': source_name,
                    'sentiment': sentiment_val,
                    'message_count': msg_count,
                    'summary': None,
                })

                date_part = ts[:10]
                if earliest_date is None or date_part < earliest_date:
                    earliest_date = date_part
                if latest_date is None or date_part > latest_date:
                    latest_date = date_part
            except Exception:
                continue

    if not rows_to_insert:
        return

    try:
        tm_cache.insert_social_signals(rows_to_insert)
    except Exception:
        return

    # Extend cache_metadata coverage for the (ticker, social_signals) lane.
    try:
        existing_md = tm_cache.get_cache_metadata(ticker_up, 'social_signals')
        if existing_md:
            md = existing_md[0]
            md_keys = md.keys() if hasattr(md, 'keys') else []
            current_from = md['have_from_date'] if 'have_from_date' in md_keys else None
            current_to = md['have_to_date'] if 'have_to_date' in md_keys else None
            new_from = min(earliest_date, current_from) if current_from else earliest_date
            new_to = max(latest_date, current_to) if current_to else latest_date
        else:
            new_from = earliest_date
            new_to = latest_date
        tm_cache.upsert_cache_metadata(
            ticker_up, 'social_signals',
            have_from_date=new_from,
            have_to_date=new_to,
            fill_source='direct',
        )
    except Exception:
        pass


# ─── Router-facing entry point ────────────────────────────────────────

def adapter(profile, data_type: str, **kwargs):
    """The function the data router calls.

    Args:
        profile: ProviderProfile (we use profile.key)
        data_type: one of 'news', 'fundamentals', 'financials_deep',
                   'earnings', 'social'
        **kwargs: data-type-specific arguments

    Returns:
        Normalized result dict (see file header for shapes), or None
        if there's no data (not an error — just empty).

    Raises:
        RateLimitError on 429
        RuntimeError on auth / server errors
        ConnectionError on network issues
    """
    # v4.15.0 Step 9: lane_config gate — silent skip when user opted out.
    try:
        import tm_cache as _tm_cache
        _should, _ = _tm_cache.lane_should_fetch('finnhub')
        if not _should:
            return None
    except Exception:
        pass
    # v4.15.0 Step 12: offline short-circuit.
    try:
        import tm_network as _tmn
        if not _tmn.is_online():
            return None
    except Exception:
        pass

    api_key = profile.key
    if not api_key:
        raise RuntimeError("finnhub: no API key configured")

    if data_type == 'news':
        ticker = kwargs.get('ticker', '')
        if not ticker:
            return None
        days_back = int(kwargs.get('days_back', 7))
        return _fetch_company_news(api_key, ticker, days_back=days_back)

    if data_type == 'fundamentals':
        ticker = kwargs.get('ticker', '')
        if not ticker:
            return None
        return _fetch_fundamentals(api_key, ticker)

    # v4.15.0 Step 14: deep financial statements via /stock/financials-reported.
    # Returns a list of per-period row dicts (cache-shaped). Side-effect cache
    # write happens inside the fetcher.
    if data_type == 'financials_deep':
        ticker = kwargs.get('ticker', '')
        if not ticker:
            return None
        freq = kwargs.get('freq', 'quarterly')
        return _fetch_financials_finnhub(api_key, ticker, freq=freq)

    if data_type == 'earnings':
        return _fetch_earnings_calendar(
            api_key,
            days_back=int(kwargs.get('days_back', 0)),
            days_ahead=int(kwargs.get('days_ahead', 14)),
            ticker=kwargs.get('ticker'),
        )

    # v4.15.0 Step 10: social-sentiment via /stock/social-sentiment.
    # Aggregates Reddit + Twitter mention data. Step 9 gate above
    # already short-circuits when the user opts out of Finnhub.
    if data_type == 'social':
        ticker = kwargs.get('ticker', '')
        if not ticker:
            return None
        days = int(kwargs.get('days', 7))
        return _fetch_social_sentiment(api_key, ticker, days=days)

    # Data type we don't serve. Router shouldn't call us, but be safe.
    return None


# ─── Registration helper ──────────────────────────────────────────────

def register_with(router) -> None:
    """Convenience for the main app: register this adapter with the
    router under the 'finnhub' provider id."""
    router.register_adapter('finnhub', adapter)
