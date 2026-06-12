"""
tm_data_adapter_reddit.py — Reddit adapter (v4.14.2 stage 5)

What this is:
    Translator between the data router and Reddit's OAuth API. Built
    on raw urllib instead of PRAW so the marketable build doesn't
    take a Python dependency the user has to install. Same adapter
    shape as tm_data_adapter_finnhub: one entrypoint
    adapter(profile, data_type, **kwargs) that the router calls.

What it serves:
    - social (per-ticker recent posts from finance subreddits)

Credentials model (FWK with embedded fallback):
    profile.key holds a JSON-encoded credentials dict:
        {"client_id": "...", "client_secret": "...",
         "user_agent": "..."  (optional)}

    Empty profile.key falls back to module-level _EMBEDDED_CREDS,
    which is intentionally blank in the shipped source. the user supplies
    embedded credentials by editing _EMBEDDED_CREDS at install time
    (or via a config write — see the Settings UI Reddit row).

    When neither user-supplied nor embedded credentials are present,
    the adapter returns None cleanly. cache.social() then falls back
    to StockTwits-only and the social block still renders with
    whatever StockTwits returned.

Subreddits queried:
    r/wallstreetbets, r/stocks, r/investing, r/StockMarket. Picked
    for ticker-mention density. Total results capped at MAX_POSTS so
    the prompt block stays compact.

Output shape (social):
    social -> {
        'messages': [
            {'body': str (post title, truncated 200 chars),
             'created_at': iso_string,
             'subreddit': str,
             'score': int,
             'num_comments': int,
             'sentiment_score': float [-1, +1] (from _score_headline),
             'sentiment': 'Bullish' | 'Bearish' | None,  # binned
                                                          # for parity
                                                          # with StockTwits
             'user': str (post author),
             'source': 'reddit'},
            ...
        ],
        'count': int,
        'source': 'reddit',
        'as_of': iso_string,
    }

    Returns None when no credentials or no usable posts.

Sentiment scoring:
    Title-only via _score_headline imported from the Finnhub adapter
    (the same shared helper Yahoo's news branch uses). Body text is
    too long to score uniformly without NLP; titles are the
    high-signal field anyway. Scored values map to the news adapter
    [-1, +1] convention; _binned_sentiment converts the float to a
    Bullish/Bearish/None tag for display parity with StockTwits.

Errors:
    HTTP 429              -> RateLimitError
    HTTP 401/403          -> RuntimeError ("check your credentials")
    HTTP 5xx              -> RuntimeError
    Network / timeout     -> ConnectionError
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

# Import is local to avoid cycles
from tm_data_router import RateLimitError


# ─── Tunable network params ───────────────────────────────────────────

REDDIT_AUTH_URL = 'https://www.reddit.com/api/v1/access_token'
REDDIT_OAUTH_BASE = 'https://oauth.reddit.com'
HTTP_TIMEOUT_SEC = 10

# Per Reddit's API guidelines the User-Agent must identify the app
# and version. Reddit blocks generic UA strings (urllib/Python/etc.)
# with HTTP 429 + a "Too Many Requests" page even on the first call.
USER_AGENT = 'TiredMarket/4.14.2-stage5 (by /u/tiredmarket)'

SUBREDDITS = ('wallstreetbets', 'stocks', 'investing', 'StockMarket')
MAX_POSTS = 30
PER_SUB_LIMIT = 10
TIME_WINDOW = 'day'    # last 24h. 'week' is the next coarser bucket.
BODY_TRUNC_CHARS = 200


# ─── Embedded credentials ────────────────────────────────────────────
#
# Shipped blank. the user supplies the shared developer credentials by
# editing this dict at install time, OR by leaving it blank and
# letting power-user Reddit credentials fill in via the Settings UI.
# When both are blank the adapter returns None and cache.social()
# falls back to StockTwits-only.

_EMBEDDED_CREDS: dict = {
    'client_id':     '',
    'client_secret': '',
    'user_agent':    USER_AGENT,
}


# ─── Token cache (per-process, refreshed on expiry) ──────────────────
#
# Reddit's client_credentials tokens expire after ~24h. Cache the
# token + expiry timestamp so a fresh fetch only runs when the cached
# one is gone or stale.

_token_cache: dict = {
    'token':       None,
    'expires_at':  0.0,
    'creds_hash':  '',  # invalidate when creds change
}


def _creds_hash(client_id: str, client_secret: str) -> str:
    """Cheap fingerprint so a credentials swap forces a fresh token
    instead of serving a cached token bound to the old creds."""
    return f"{client_id[:6]}:{len(client_secret)}"


# ─── Credentials resolution ──────────────────────────────────────────

def _resolve_creds(profile) -> dict | None:
    """Resolve effective credentials for this call.

    Priority:
      1. profile.key (user-supplied, JSON-encoded)
      2. _EMBEDDED_CREDS (the user's shipped shared credentials)
      3. None (no credentials available; adapter returns None)
    """
    raw_key = (getattr(profile, 'key', '') or '').strip()
    if raw_key:
        try:
            user_creds = json.loads(raw_key)
            if (isinstance(user_creds, dict)
                    and user_creds.get('client_id')
                    and user_creds.get('client_secret')):
                return {
                    'client_id':     str(user_creds['client_id']),
                    'client_secret': str(user_creds['client_secret']),
                    'user_agent':    str(user_creds.get('user_agent')
                                          or USER_AGENT),
                }
        except (json.JSONDecodeError, TypeError):
            pass
    if (_EMBEDDED_CREDS.get('client_id')
            and _EMBEDDED_CREDS.get('client_secret')):
        return dict(_EMBEDDED_CREDS)
    return None


# ─── OAuth ────────────────────────────────────────────────────────────

def _get_oauth_token(client_id: str, client_secret: str,
                      user_agent: str) -> str | None:
    """client_credentials grant. Returns access_token string or None
    on failure. Caches the token until expiry."""
    import time as _t
    fp = _creds_hash(client_id, client_secret)
    now = _t.time()
    if (_token_cache['token']
            and _token_cache['creds_hash'] == fp
            and now < _token_cache['expires_at'] - 60):
        return _token_cache['token']

    auth = base64.b64encode(
        f"{client_id}:{client_secret}".encode('utf-8')).decode('ascii')
    body = urllib.parse.urlencode(
        {'grant_type': 'client_credentials'}).encode('utf-8')
    req = urllib.request.Request(REDDIT_AUTH_URL, data=body, headers={
        'Authorization': f"Basic {auth}",
        'User-Agent':    user_agent,
        'Content-Type':  'application/x-www-form-urlencoded',
        'Accept':        'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            if resp.status != 200:
                raise RuntimeError(
                    f"reddit oauth: HTTP {resp.status}")
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimitError(
                "reddit oauth: 429 Too Many Requests") from e
        if e.code in (401, 403):
            raise RuntimeError(
                f"reddit oauth: HTTP {e.code} — check your "
                f"Reddit credentials") from e
        raise RuntimeError(f"reddit oauth: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise ConnectionError(
            f"reddit oauth: network error: {e.reason}") from e

    token = data.get('access_token')
    if not token:
        return None
    expires_in = float(data.get('expires_in') or 86400)
    _token_cache['token'] = token
    _token_cache['expires_at'] = now + expires_in
    _token_cache['creds_hash'] = fp
    return token


# ─── Per-subreddit search ────────────────────────────────────────────

def _search_subreddit(token: str, user_agent: str,
                       subreddit: str, ticker: str) -> list[dict]:
    """GET /r/{sub}/search.json?q={ticker}&restrict_sr=on&sort=new&t=day
    Returns a list of post dicts (title + score + author + subreddit
    + permalink + created_utc). Empty list on any error."""
    params = {
        'q':           ticker,
        'restrict_sr': 'on',
        'sort':        'new',
        't':           TIME_WINDOW,
        'limit':       PER_SUB_LIMIT,
    }
    url = (f"{REDDIT_OAUTH_BASE}/r/{urllib.parse.quote(subreddit)}"
           f"/search.json?{urllib.parse.urlencode(params)}")
    req = urllib.request.Request(url, headers={
        'Authorization': f"bearer {token}",
        'User-Agent':    user_agent,
        'Accept':        'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            if resp.status != 200:
                return []
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimitError(
                f"reddit r/{subreddit}: 429 Too Many Requests") from e
        # 401/403/etc. on a single subreddit shouldn't kill the whole
        # batch — degrade to empty.
        return []
    except urllib.error.URLError:
        return []

    children = ((data.get('data') or {}).get('children') or [])
    posts = []
    for ch in children:
        if not isinstance(ch, dict):
            continue
        d = ch.get('data') or {}
        title = (d.get('title') or '').strip()
        if not title:
            continue
        posts.append({
            'title':        title,
            'subreddit':    d.get('subreddit') or subreddit,
            'score':        int(d.get('score') or 0),
            'num_comments': int(d.get('num_comments') or 0),
            'created_utc':  float(d.get('created_utc') or 0),
            'author':       d.get('author') or '?',
            'permalink':    d.get('permalink') or '',
        })
    return posts


# ─── Sentiment binning (parity with StockTwits) ──────────────────────

def _binned_sentiment(score: float) -> str | None:
    """Map a [-1, +1] sentiment_score to Bullish / Bearish / None
    so the merged social view can show a unified categorical tally
    across Reddit + StockTwits."""
    if score >= 0.25:
        return 'Bullish'
    if score <= -0.25:
        return 'Bearish'
    return None


# ─── Core fetcher ────────────────────────────────────────────────────

def _fetch_social(profile, ticker: str) -> dict | None:
    """Aggregate posts across the configured subreddits, dedup by
    title, score sentiment, return normalized dict. Returns None
    when no credentials are available OR no posts came back."""
    creds = _resolve_creds(profile)
    if creds is None:
        # No credentials configured. Quiet None — caller falls back
        # to StockTwits-only via the cache merge layer.
        return None
    if not ticker:
        return None

    try:
        token = _get_oauth_token(
            creds['client_id'], creds['client_secret'],
            creds['user_agent'])
    except RateLimitError:
        raise
    except Exception:
        token = None
    if not token:
        return None

    # Lazy import the shared sentiment scorer to avoid circular
    # import dependencies at module load.
    try:
        from tm_data_adapter_finnhub import _score_headline
    except Exception:
        _score_headline = lambda _t: 0.0

    seen_titles: set = set()
    out_messages = []
    user_agent = creds['user_agent']
    for sub in SUBREDDITS:
        try:
            posts = _search_subreddit(token, user_agent, sub,
                                       ticker.upper())
        except RateLimitError:
            raise
        except Exception:
            posts = []
        for p in posts:
            t = p['title']
            key = t[:80].lower().strip()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            score = float(_score_headline(t))
            try:
                created_iso = datetime.fromtimestamp(
                    p['created_utc']).isoformat(timespec='seconds')
            except (OSError, ValueError):
                created_iso = ''
            out_messages.append({
                'body':            t[:BODY_TRUNC_CHARS],
                'created_at':      created_iso,
                'subreddit':       p['subreddit'],
                'score':           p['score'],
                'num_comments':    p['num_comments'],
                'sentiment_score': score,
                'sentiment':       _binned_sentiment(score),
                'user':            p['author'],
                'source':          'reddit',
            })
        if len(out_messages) >= MAX_POSTS:
            break

    out_messages = out_messages[:MAX_POSTS]
    if not out_messages:
        return None

    # Sort by created_at desc (newest first) — newest posts most useful.
    out_messages.sort(key=lambda m: m.get('created_at') or '',
                       reverse=True)
    subs = sorted({m['subreddit'] for m in out_messages})
    return {
        'messages':   out_messages,
        'count':      len(out_messages),
        'subreddits': subs,
        'source':     'reddit',
        'as_of':      datetime.now().isoformat(timespec='seconds'),
    }


# ─── Router-facing entry point ────────────────────────────────────────

def adapter(profile, data_type: str, **kwargs):
    """Function the data router calls.

    Args:
        profile: ProviderProfile (uses .key for user-supplied creds;
                                  falls back to _EMBEDDED_CREDS)
        data_type: 'social' (the only kind Reddit serves)
        **kwargs:
            ticker (str): the ticker symbol to fetch

    Returns the normalized social dict (see file header) or None
    when no credentials are available or no posts came back.
    """
    if data_type == 'social':
        return _fetch_social(profile, kwargs.get('ticker', ''))
    return None


# ─── Registration helper ──────────────────────────────────────────────

def register_with(router) -> None:
    """Convenience for the main app: register this adapter under the
    'reddit' provider id."""
    router.register_adapter('reddit', adapter)
