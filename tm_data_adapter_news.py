"""
tm_data_adapter_news.py — Multi-source news adapters (v4.13.58)

What this is:
    Three news adapters in one file (Marketaux, NewsAPI, Twelve Data),
    each wired into the data router under the same `news` data type.
    All return the standard `{'headlines': [...], 'fetched_at': ts}`
    shape so the rest of the app doesn't care which source produced
    the headlines.

Why three new adapters:
    Finnhub (the existing news source) caps free at 60 calls/min. That
    means with even moderate scanning, you'll burn through quota during
    a busy market hour. Adding three free alternatives means:
      - More aggregate coverage per day
      - The data router can rotate between them based on observed quota
      - If one goes down, the others keep the news cache populated

Provider quotas (free tier, as of May 2026):
    - Marketaux:    100 requests/day, real-time stock news
    - NewsAPI:      100 requests/day, broader news (filtered by ticker)
    - Twelve Data:  800 requests/day, includes news endpoint

All three are disabled by default in the registry — user has to
explicitly enable + paste an API key. Once enabled, the data router's
priority logic decides which to call for each ticker (typically by
declared priority; observed-quota learning will adjust).

Output shape (common across all three):
    {
        'headlines': [
            {
                'title': str,
                'summary': str,
                'source': str,
                'url': str,
                'published_at': str (ISO 8601),
                'sentiment': float (-1..+1),
            },
            ...
        ],
        'fetched_at': float (epoch),
        'provider': str ('marketaux' | 'newsapi' | 'twelve_data'),
    }
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Optional


HTTP_TIMEOUT_SEC = 10
USER_AGENT = 'TiredMarket/4.13.58'


# Reuse Finnhub's RateLimitError so router treats them identically
class RateLimitError(RuntimeError):
    """Raised on HTTP 429."""
    pass


# ─── Shared sentiment scorer (mirrors Finnhub adapter) ────────────────

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
    if not text:
        return 0.0
    t = text.lower()
    pos = sum(1 for term in _POSITIVE_TERMS if term in t)
    neg = sum(1 for term in _NEGATIVE_TERMS if term in t)
    if pos == 0 and neg == 0:
        return 0.0
    raw = (pos - neg) / max(pos + neg, 1)
    return max(-1.0, min(1.0, raw))


def _http_get_json(url: str, headers: Optional[dict] = None,
                     provider_label: str = 'news') -> Any:
    """Standard HTTP fetcher with consistent error handling."""
    req = urllib.request.Request(url, headers=headers or {
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
                    raise RuntimeError(
                        f"{provider_label}: bad JSON response: {e}")
            elif status == 429:
                raise RateLimitError(f"{provider_label}: 429 rate limited")
            elif status in (401, 403):
                raise RuntimeError(
                    f"{provider_label}: HTTP {status} — check API key")
            else:
                raise RuntimeError(f"{provider_label}: HTTP {status}")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimitError(
                f"{provider_label}: 429 rate limited") from e
        elif e.code in (401, 403):
            raise RuntimeError(
                f"{provider_label}: HTTP {e.code} — check API key") from e
        else:
            raise RuntimeError(
                f"{provider_label}: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise ConnectionError(
            f"{provider_label}: network error: {e.reason}") from e


# ═══════════════════════════════════════════════════════════════════════
# Marketaux — https://www.marketaux.com/
# ═══════════════════════════════════════════════════════════════════════

MARKETAUX_BASE = 'https://api.marketaux.com/v1/news/all'


def _marketaux_news(api_key: str, ticker: str,
                     limit: int = 20) -> Optional[dict]:
    """Fetch ticker news from Marketaux. Returns standard news shape
    or None if no headlines available."""
    params = {
        'api_token': api_key,
        'symbols': ticker.upper(),
        'language': 'en',
        'filter_entities': 'true',
        'limit': str(limit),
    }
    url = f"{MARKETAUX_BASE}?{urllib.parse.urlencode(params)}"
    data = _http_get_json(url, provider_label='marketaux')
    if not data or 'data' not in data:
        return None

    headlines = []
    for item in data.get('data', []):
        title = (item.get('title') or '').strip()
        if not title:
            continue
        summary = (item.get('description') or '')[:500]
        published = item.get('published_at', '')
        # Marketaux gives entity-level sentiment. Use it if present;
        # otherwise fall back to keyword scoring.
        sentiment_provided = None
        for ent in item.get('entities') or []:
            if (ent.get('symbol', '') or '').upper() == ticker.upper():
                ss = ent.get('sentiment_score')
                if ss is not None:
                    try:
                        sentiment_provided = float(ss)
                        break
                    except (TypeError, ValueError):
                        pass
        sentiment = (
            sentiment_provided
            if sentiment_provided is not None
            else _score_headline(f"{title} {summary}"))
        headlines.append({
            'title': title,
            'summary': summary,
            'source': item.get('source', 'marketaux'),
            'url': item.get('url', ''),
            'published_at': published,
            'sentiment': sentiment,
        })
    if not headlines:
        return None
    return {
        'headlines': headlines,
        'fetched_at': time.time(),
        'provider': 'marketaux',
    }


# ═══════════════════════════════════════════════════════════════════════
# NewsAPI — https://newsapi.org/
# ═══════════════════════════════════════════════════════════════════════

NEWSAPI_BASE = 'https://newsapi.org/v2/everything'


def _newsapi_news(api_key: str, ticker: str,
                   limit: int = 20) -> Optional[dict]:
    """Fetch ticker news from NewsAPI. Note: NewsAPI doesn't filter
    by ticker symbol natively — we use the ticker as a query string.
    For better results, configure provider with a query template that
    includes company name (handled at registry level via 'query_hint')."""
    # Use ticker as the search term
    params = {
        'q': ticker.upper(),
        'language': 'en',
        'sortBy': 'publishedAt',
        'pageSize': str(min(limit, 100)),
        'apiKey': api_key,
    }
    url = f"{NEWSAPI_BASE}?{urllib.parse.urlencode(params)}"
    data = _http_get_json(url, provider_label='newsapi')
    if not data or data.get('status') != 'ok':
        return None

    headlines = []
    for item in data.get('articles', []):
        title = (item.get('title') or '').strip()
        if not title or title == '[Removed]':
            continue
        summary = (item.get('description') or '')[:500]
        source = (item.get('source') or {}).get('name', 'newsapi')
        sentiment = _score_headline(f"{title} {summary}")
        headlines.append({
            'title': title,
            'summary': summary,
            'source': source,
            'url': item.get('url', ''),
            'published_at': item.get('publishedAt', ''),
            'sentiment': sentiment,
        })
    if not headlines:
        return None
    return {
        'headlines': headlines,
        'fetched_at': time.time(),
        'provider': 'newsapi',
    }


# ═══════════════════════════════════════════════════════════════════════
# Twelve Data — https://twelvedata.com/
# ═══════════════════════════════════════════════════════════════════════

TWELVE_DATA_NEWS_BASE = 'https://api.twelvedata.com/news'


def _twelve_data_news(api_key: str, ticker: str,
                       limit: int = 20) -> Optional[dict]:
    """Fetch ticker news from Twelve Data. Note: Twelve Data's news
    endpoint is part of their Pro tier on some plans — this adapter
    will surface a clear error message if the user's key doesn't have
    access."""
    params = {
        'symbol': ticker.upper(),
        'apikey': api_key,
        'outputsize': str(limit),
    }
    url = f"{TWELVE_DATA_NEWS_BASE}?{urllib.parse.urlencode(params)}"
    data = _http_get_json(url, provider_label='twelve_data')
    if not data:
        return None
    # Twelve Data returns errors as {status: 'error', code: N, message: ...}
    if isinstance(data, dict) and data.get('status') == 'error':
        msg = data.get('message', 'unknown error')
        if 'plan' in msg.lower() or 'access' in msg.lower():
            raise RuntimeError(
                f"twelve_data: news endpoint not available on this plan "
                f"({msg})")
        raise RuntimeError(f"twelve_data: {msg}")

    items = data.get('data') if isinstance(data, dict) else data
    if not items:
        return None

    headlines = []
    for item in items:
        title = (item.get('title') or '').strip()
        if not title:
            continue
        summary = (item.get('content') or item.get('description') or '')[:500]
        sentiment = _score_headline(f"{title} {summary}")
        headlines.append({
            'title': title,
            'summary': summary,
            'source': item.get('source', 'twelve_data'),
            'url': item.get('url', ''),
            'published_at': (
                item.get('datetime') or item.get('published_at') or ''),
            'sentiment': sentiment,
        })
    if not headlines:
        return None
    return {
        'headlines': headlines,
        'fetched_at': time.time(),
        'provider': 'twelve_data',
    }


# ═══════════════════════════════════════════════════════════════════════
# Public adapter functions — what the data router calls
# ═══════════════════════════════════════════════════════════════════════

def adapter_marketaux(profile: dict, data_type: str,
                       **kwargs) -> Optional[dict]:
    """Marketaux entry point for the data router."""
    if data_type != 'news':
        return None  # only news supported
    # v4.15.0 Step 9: lane_config gate — silent skip when user opted out.
    try:
        import tm_cache as _tm_cache
        _should, _ = _tm_cache.lane_should_fetch('marketaux')
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
    api_key = profile.get('api_key', '')
    if not api_key:
        raise RuntimeError(
            "marketaux: no API key configured. "
            "Get one free at https://www.marketaux.com/")
    ticker = kwargs.get('ticker', '').strip().upper()
    if not ticker:
        return None
    limit = int(kwargs.get('limit', 20))
    return _marketaux_news(api_key, ticker, limit)


def adapter_newsapi(profile: dict, data_type: str,
                     **kwargs) -> Optional[dict]:
    """NewsAPI entry point for the data router."""
    if data_type != 'news':
        return None
    # v4.15.0 Step 9: lane_config gate — silent skip when user opted out.
    try:
        import tm_cache as _tm_cache
        _should, _ = _tm_cache.lane_should_fetch('newsapi')
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
    api_key = profile.get('api_key', '')
    if not api_key:
        raise RuntimeError(
            "newsapi: no API key configured. "
            "Get one free at https://newsapi.org/")
    ticker = kwargs.get('ticker', '').strip().upper()
    if not ticker:
        return None
    limit = int(kwargs.get('limit', 20))
    return _newsapi_news(api_key, ticker, limit)


def adapter_twelve_data(profile: dict, data_type: str,
                         **kwargs) -> Optional[dict]:
    """Twelve Data entry point for the data router."""
    if data_type != 'news':
        return None
    # v4.15.0 Step 9: lane_config gate — silent skip when user opted out.
    try:
        import tm_cache as _tm_cache
        _should, _ = _tm_cache.lane_should_fetch('twelve_data')
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
    api_key = profile.get('api_key', '')
    if not api_key:
        raise RuntimeError(
            "twelve_data: no API key configured. "
            "Get one free at https://twelvedata.com/")
    ticker = kwargs.get('ticker', '').strip().upper()
    if not ticker:
        return None
    limit = int(kwargs.get('limit', 20))
    return _twelve_data_news(api_key, ticker, limit)


def register_with(router) -> None:
    """Convenience for the main app: register all three news adapters
    with the data router under their respective provider ids."""
    router.register_adapter('marketaux', adapter_marketaux)
    router.register_adapter('newsapi', adapter_newsapi)
    router.register_adapter('twelve_data', adapter_twelve_data)
