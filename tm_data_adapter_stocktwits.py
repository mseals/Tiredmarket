"""
tm_data_adapter_stocktwits.py — StockTwits adapter (v4.14.2 stage 5)

What this is:
    Translator between the data router and StockTwits' public stream
    API. Keyless — no auth required. Same adapter shape as
    tm_data_adapter_yahoo: one entrypoint adapter(profile, data_type,
    **kwargs) that the router calls.

What it serves:
    - social (per-ticker recent messages with Bullish/Bearish tags
              when the poster set one)

Endpoint:
    GET https://api.stocktwits.com/api/2/streams/symbol/{TICKER}.json

    Returns up to ~30 most-recent messages for the ticker. Probe
    confirms the public endpoint is still keyless as of 2026-05-09;
    if StockTwits ever requires auth, the keyless path returns
    HTTP 4xx and the adapter degrades to [].

Output shape (social):
    social -> {
        'messages': [
            {'body': str (truncated 200 chars),
             'created_at': iso_string,
             'sentiment': 'Bullish' | 'Bearish' | None,
             'sentiment_score': float [-1, +1],   # mapped from
                                                  # Bullish/Bearish/None
             'user': str,
             'source': 'stocktwits'},
            ...
        ],
        'count': int,
        'source': 'stocktwits',
        'as_of': iso_string,
    }

    Returns None on parse failure. Empty messages list on a
    successful call with zero results (rare).

Sentiment scale:
    StockTwits' Bullish/Bearish maps to the news-adapter convention:
    Bullish -> +1.0, Bearish -> -1.0, None (untagged) -> 0.0.
    Same numeric range Finnhub/Yahoo news adapters produce so the
    cache merge math stays uniform.

Errors:
    HTTP 429              -> RateLimitError
    HTTP 4xx other        -> RuntimeError
    HTTP 5xx              -> RuntimeError
    Network / timeout     -> ConnectionError
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

# Import is local to avoid cycles
from tm_data_router import RateLimitError


# ─── Tunable network params ───────────────────────────────────────────

STOCKTWITS_BASE_URL = 'https://api.stocktwits.com/api/2'
HTTP_TIMEOUT_SEC = 10
# StockTwits' public stream rejects the Cloudflare-frozen UA the
# Groq-targeting User-Agent header uses (HTTP 403 on the first
# request). The general app-identity string matching what
# verified-working in stage 5's investigation probe is what flies
# here. Don't unify with the other adapters' frozen UA — different
# upstream, different bot-detection rules.
USER_AGENT = 'TiredMarket/4.14.2 (StockTwits)'
MAX_MESSAGES = 30
BODY_TRUNC_CHARS = 200


# ─── HTTP helper ──────────────────────────────────────────────────────

def _http_get_json(url: str) -> Any:
    """GET the StockTwits endpoint. Raises structured exceptions for
    the router to classify."""
    req = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT,
        'Accept':     'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            status = resp.status
            body = resp.read()
            if status == 200:
                try:
                    return json.loads(body.decode('utf-8'))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    raise RuntimeError(
                        f"stocktwits: bad JSON response: {e}")
            elif status == 429:
                raise RateLimitError(
                    "stocktwits: 429 Too Many Requests")
            else:
                raise RuntimeError(f"stocktwits: HTTP {status}")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimitError(
                "stocktwits: 429 Too Many Requests") from e
        if e.code == 404:
            # Ticker not found — treat as empty rather than error so
            # the merge layer doesn't surface a scary message for
            # tickers StockTwits doesn't cover.
            return None
        raise RuntimeError(f"stocktwits: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise ConnectionError(
            f"stocktwits: network error: {e.reason}") from e


# ─── Sentiment mapping ────────────────────────────────────────────────

def _sentiment_to_score(basic: str | None) -> float:
    """StockTwits' Bullish/Bearish/None -> the [-1, +1] scale the
    news adapters use. Untagged messages contribute neutrally to
    aggregate sentiment without dragging the average toward zero."""
    if basic == 'Bullish':
        return 1.0
    if basic == 'Bearish':
        return -1.0
    return 0.0


# ─── Core fetcher ─────────────────────────────────────────────────────

def _fetch_stream(ticker: str) -> dict | None:
    """Fetch the recent message stream for a ticker. Normalizes the
    response into the per-message shape the cache layer expects."""
    if not ticker:
        return None
    safe = urllib.parse.quote(ticker.upper().strip())
    url = f"{STOCKTWITS_BASE_URL}/streams/symbol/{safe}.json"
    raw = _http_get_json(url)
    if raw is None:
        # 404 from upstream — treat as empty.
        return None
    if not isinstance(raw, dict):
        return None
    raw_messages = raw.get('messages') or []
    if not isinstance(raw_messages, list):
        return None
    out_messages = []
    for m in raw_messages[:MAX_MESSAGES]:
        if not isinstance(m, dict):
            continue
        body = (m.get('body') or '').strip()
        if not body:
            continue
        ent = m.get('entities') or {}
        sent_obj = (ent.get('sentiment') if isinstance(ent, dict)
                     else None)
        basic = (sent_obj.get('basic')
                  if isinstance(sent_obj, dict) else None)
        user_obj = m.get('user') or {}
        username = (user_obj.get('username')
                     if isinstance(user_obj, dict) else None) or '?'
        out_messages.append({
            'body':            body[:BODY_TRUNC_CHARS],
            'created_at':      m.get('created_at') or '',
            'sentiment':       basic,
            'sentiment_score': _sentiment_to_score(basic),
            'user':            username,
            'source':          'stocktwits',
        })
    if not out_messages:
        return None

    # v4.15.0 step 5: tap into cache.social_signals. Side-effect only;
    # caller still receives the same dict.
    try:
        _v415_cache_write_social(ticker, out_messages)
    except Exception:
        pass

    return {
        'messages': out_messages,
        'count':    len(out_messages),
        'source':   'stocktwits',
        'as_of':    datetime.now().isoformat(timespec='seconds'),
    }


def _v415_cache_write_social(ticker: str, messages: list) -> None:
    """v4.15.0 step 5: StockTwits message stream → social_signals rows.

    One row per message (granular). Dedups by (ticker, timestamp) against
    the most recent 100 cached rows for this ticker, since StockTwits'
    public stream returns the same recent messages on repeat fetches.

    Sentiment is the per-message normalized score in [-1, +1] (already
    computed in _fetch_stream via _sentiment_to_score). message_count
    is always 1 since rows are per-message. summary holds the truncated
    body text.

    Side-effect only.
    """
    if not messages or not ticker:
        return

    try:
        import tm_cache
    except ImportError:
        return

    ticker_up = ticker.upper()

    # Build dedup set from recent cached rows.
    try:
        recent = tm_cache.get_social_signals(ticker_up, since=None, limit=100)
        existing_keys = set()
        for r in recent:
            try:
                keys = r.keys() if hasattr(r, 'keys') else []
                ts = r['timestamp'] if 'timestamp' in keys else None
                existing_keys.add(ts)
            except Exception:
                continue
    except Exception:
        existing_keys = set()

    rows = []
    earliest_date = None
    latest_date = None

    for m in messages:
        if not isinstance(m, dict):
            continue
        ts = (m.get('created_at') or '').strip()
        if not ts:
            continue
        if ts in existing_keys:
            continue
        existing_keys.add(ts)  # In-batch dedup too.

        score = m.get('sentiment_score')
        try:
            sentiment_val = float(score) if score is not None else None
            if sentiment_val is not None and sentiment_val != sentiment_val:
                sentiment_val = None
        except (TypeError, ValueError):
            sentiment_val = None

        body = m.get('body') or None
        rows.append({
            'ticker': ticker_up,
            'timestamp': ts,
            'source': 'stocktwits',
            'sentiment': sentiment_val,
            'message_count': 1,
            'summary': body,
        })

        date_part = ts[:10]
        if earliest_date is None or date_part < earliest_date:
            earliest_date = date_part
        if latest_date is None or date_part > latest_date:
            latest_date = date_part

    if not rows:
        return

    try:
        tm_cache.insert_social_signals(rows)
    except Exception:
        return

    try:
        existing = tm_cache.get_cache_metadata(ticker_up, 'social_signals')
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
            ticker_up, 'social_signals',
            have_from_date=new_from,
            have_to_date=new_to,
            fill_source='direct',
        )
    except Exception:
        pass


# ─── Router-facing entry point ────────────────────────────────────────

def adapter(profile, data_type: str, **kwargs):
    """Function the data router calls.

    Args:
        profile: ProviderProfile (unused — StockTwits is keyless)
        data_type: 'social' (the only kind StockTwits serves)
        **kwargs:
            ticker (str): the ticker symbol to fetch

    Returns the normalized social dict (see file header) or None
    when no messages are available.

    Raises:
        RateLimitError on 429
        RuntimeError on auth/server errors
        ConnectionError on network issues
    """
    if data_type == 'social':
        # v4.15.0 Step 12: offline short-circuit.
        try:
            import tm_network as _tmn
            if not _tmn.is_online():
                return None
        except Exception:
            pass
        return _fetch_stream(kwargs.get('ticker', ''))
    return None


# ─── Registration helper ──────────────────────────────────────────────

def register_with(router) -> None:
    """Convenience for the main app: register this adapter under the
    'stocktwits' provider id."""
    router.register_adapter('stocktwits', adapter)
