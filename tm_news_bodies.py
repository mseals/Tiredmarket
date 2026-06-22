"""tm_news_bodies — on-demand article-body fetcher for tier-2 votes.

The read-only investigation (v4.14.5.67 era) found that both tier-1 and
tier-2 votes saw the SAME compressed news payload — up to 5 headlines
(title strings only, 120-char truncated) + aggregate sentiment + a
disagreement flag. The news daemon stores headlines in `news_cache`
but the `raw_data` column was hardcoded NULL — no body text anywhere.
So tier-2 was a vote-count re-poll on identical shallow inputs, not a
real news double-check.

This module gives the tier-2 paths (only) access to article BODY text:

  1. cache-check `news_cache.raw_data` for FRESH bodies (TTL hours).
  2. on miss, fetch up to N article bodies via the URLs already stored
     in `news_cache` by the daemon. Network only fires on the tier-2
     background thread.
  3. UPDATE the row's `raw_data` column with the extracted body, so a
     repeat tier-2 pass on the same name within the TTL reads from
     cache instead of re-fetching.
  4. emit a one-line telemetry record (`[tier2-news] …`) so the caps
     below can be calibrated from real data.

ALL CAPS are NAMED CONSTANTS (not magic numbers). They are deliberately
conservative to start; the runner can be told to raise them after we
see telemetry from a live run.

Guarantees:
- A body fetch that fails / times out / returns junk degrades to
  whatever bodies WERE cached (or zero); the caller's prompt builder
  then falls back to today's headline rendering. A body-fetch failure
  must NEVER block, error, or skip a vote.
- Network only inside `prefetch_bodies_for_tier2`. The renderer in
  `tm_context_builder` reads the cache only.
- Tier-1 (`prompt_kind='candidate'`) NEVER calls this module.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional


# ─── Tunable caps (named, not magic) ─────────────────────────────────

# Max article bodies to fetch+render per ticker. Start cautious so
# context windows stay manageable and we don't hammer feeds.
_TIER2_NEWS_BODY_MAX_ARTICLES = 5

# How many tickers' bodies to prefetch per call. 1 = strict on-demand
# (one ticker at a time, fetch-then-vote). Higher values let a caller
# batch multiple upcoming tickers. The current call sites pass exactly
# one ticker, so this is a forward-looking hook.
_TIER2_NEWS_PREFETCH_BATCH = 1

# Reuse-from-cache window. A body cached within this many hours is
# read; older bodies trigger a re-fetch. Matches the existing
# DataCacheLayer.MAX_DISK_AGE_NEWS = 6h freshness window.
_TIER2_NEWS_BODY_TTL_HOURS = 6

# Per-article network timeout (seconds). A stalling feed can't hang
# a background vote longer than this.
_TIER2_NEWS_BODY_FETCH_TIMEOUT = 8

# bodyfix-2026-06-15: max concurrent body fetches per
# prefetch_bodies_for_tier2 call. Bodies come from many distinct news
# domains so modest parallelism is safe. Kept small for the 4 GB /
# weak-CPU target — large enough to overlap ~5 articles on network
# wait, small enough not to fight the UI thread or spawn a thread storm.
_TIER2_NEWS_BODY_PARALLEL = 5

# Per-article body cap in the rendered prompt. Long-form articles get
# truncated so the assembled prompt stays under provider context
# windows. The renderer in tm_context_builder reuses this constant.
_TIER2_NEWS_BODY_RENDER_MAX_CHARS = 1500

# Overall body-section cap per ticker (sum across all bodies).
_TIER2_NEWS_BODY_RENDER_TOTAL_MAX_CHARS = 6000

# What we tell the feeds we are.
_USER_AGENT = (
    'Tired Market/4.14.5.68 (educational research tool; '
    'contact via app settings)')


# ─── Internal helpers ────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now().isoformat()


def _ttl_cutoff_iso() -> str:
    """Return the ISO timestamp older than which bodies count as stale."""
    return (datetime.now()
            - timedelta(hours=_TIER2_NEWS_BODY_TTL_HOURS)
            ).isoformat()


# Light HTML → text extractor. We deliberately don't pull in a heavy
# parser (beautifulsoup/readability) — the project is stdlib-leaning
# and the failure mode of "got HTML cruft" degrades to headlines
# (the renderer truncates anyway and the model can ignore noise).
_TAG_RE = re.compile(r'<[^>]+>')
_SCRIPT_RE = re.compile(
    r'<(script|style|nav|aside|footer|form)[^>]*>.*?</\1>',
    re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r'[ \t ]+')
_NL_RE = re.compile(r'\n{3,}')
_HTML_ENTITY_RE = re.compile(r'&(amp|lt|gt|quot|#39|nbsp);')
_HTML_ENTITY_MAP = {'amp': '&', 'lt': '<', 'gt': '>',
                     'quot': '"', '#39': "'", 'nbsp': ' '}


def _strip_html(html: str) -> str:
    """Crude but defensive HTML → text. Returns '' on any failure."""
    try:
        if not html:
            return ''
        text = _SCRIPT_RE.sub(' ', html)
        text = _TAG_RE.sub(' ', text)
        text = _HTML_ENTITY_RE.sub(
            lambda m: _HTML_ENTITY_MAP.get(m.group(1), ' '),
            text)
        text = _WS_RE.sub(' ', text)
        text = _NL_RE.sub('\n\n', text)
        return text.strip()
    except Exception:
        return ''


def _looks_like_paywall_or_junk(text: str) -> bool:
    """True if extracted text is obviously not real article content."""
    if not text or len(text) < 200:
        return True
    low = text.lower()
    paywall_signals = (
        'subscribe to continue', 'subscribe to read',
        'sign in to read', 'create a free account',
        'this content is only available to subscribers',
    )
    if any(s in low for s in paywall_signals):
        # Don't auto-reject — many paywall pages also include the
        # article lead. Reject only if the body is short AND has
        # paywall markers.
        if len(text) < 800:
            return True
    return False


def _http_fetch(url: str, timeout: float) -> Optional[bytes]:
    """GET url with a sane User-Agent. Returns bytes on 2xx, None on
    any failure. Never raises."""
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(url, headers={
            'User-Agent': _USER_AGENT,
            'Accept': ('text/html,application/xhtml+xml,'
                       'application/xml;q=0.9,*/*;q=0.8'),
            'Accept-Language': 'en-US,en;q=0.8',
            'Accept-Encoding': 'identity',
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(2_000_000)  # 2MB cap per article
            return raw
    except Exception:
        return None


# ─── Cache I/O (talks to the existing Database `news_cache` table) ───


def _get_recent_rows(db, ticker: str,
                      max_rows: int = 20) -> list:
    """Read the most-recent N news_cache rows for ticker. Returns a
    list of dicts {id, timestamp, headline, source, url, sentiment,
    has_body, body}. Never raises.

    v4.14.6.39-tier2-body-read-fix (bodyfix-2026-06-15): the SELECT
    now includes the `url` and `published_at` columns that the
    news_cache writers populate (added by ALTER TABLE in the schema
    migration). Pre-fix, the SELECT omitted `url`, so this function
    parsed URLs out of the `headline` column via a ' :: ' split —
    a legacy/obsolete format the live writers never produce. Result:
    every parsed url was '', every candidate row was dropped at the
    `if not url` check in prefetch_bodies_for_tier2, and every
    Tier-2 call logged "0 fetched (no fetchable URLs in cache)" —
    Tier-2 was voting on headlines alone in every mode for as long
    as deep-news has shipped. Now: prefer the real `url` column;
    fall back to the headline split ONLY when the column is empty
    (preserves back-compat for old pre-migration rows). Read-side
    fix only — schema, writers, and call sites unchanged.
    """
    if db is None or not ticker:
        return []
    try:
        with getattr(db, 'lock', _NullLock()):
            cur = db.conn.execute(
                "SELECT id, timestamp, ticker, headline, source, "
                "       sentiment_score, raw_data, "
                "       url, published_at "
                "FROM news_cache "
                "WHERE UPPER(ticker) = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                ((ticker or '').upper(), int(max_rows)))
            rows = cur.fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        rid = ts = tk = hl = src = sent = raw = None
        col_url = None
        col_pub = None
        # Positional unpack works whether row_factory=sqlite3.Row is
        # set or not (Row also supports tuple unpacking).
        try:
            rid, ts, tk, hl, src, sent, raw, col_url, col_pub = r
        except Exception:
            # Named-access fallback for already-mapped row dicts
            # (none of the current callers, but cheap insurance).
            try:
                rid = r['id']; ts = r['timestamp']; tk = r['ticker']
                hl = r['headline']; src = r['source']
                sent = r['sentiment_score']; raw = r['raw_data']
                try:    col_url = r['url']
                except Exception: col_url = None
                try:    col_pub = r['published_at']
                except Exception: col_pub = None
            except Exception:
                continue
        # bodyfix: prefer the dedicated `url` column. Only fall back
        # to the legacy " :: " split when the column is empty/NULL
        # (old pre-migration rows). Title is the headline with any
        # " :: URL" suffix stripped so the rendered prompt stays clean.
        title = (hl or '')
        url = ''
        if isinstance(col_url, str) and col_url.strip():
            url = col_url.strip()
            if isinstance(hl, str) and ' :: ' in hl:
                title = hl.partition(' :: ')[0]
        elif isinstance(hl, str) and ' :: ' in hl:
            t, _, u = hl.partition(' :: ')
            title, url = t, u
        out.append({
            'id': rid, 'timestamp': ts, 'ticker': tk,
            'title': title, 'source': src,
            'sentiment_score': sent,
            'url': url, 'body': raw,
            'published_at': col_pub,
        })
    return out


class _NullLock:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _write_body(db, row_id: int, body: str) -> None:
    """UPDATE news_cache.raw_data for a single row. Never raises."""
    if db is None or row_id is None:
        return
    try:
        with getattr(db, 'lock', _NullLock()):
            db.conn.execute(
                "UPDATE news_cache SET raw_data = ? WHERE id = ?",
                (body, int(row_id)))
            db.conn.commit()
    except Exception:
        pass


# ─── Public API ──────────────────────────────────────────────────────


def get_cached_bodies(db, ticker: str) -> list:
    """Return a list of {title, source, url, body, timestamp} for
    rows that have a fresh body cached (within
    _TIER2_NEWS_BODY_TTL_HOURS). Empty list if no fresh bodies. Pure
    read — no network, no writes."""
    if db is None or not ticker:
        return []
    cutoff = _ttl_cutoff_iso()
    out = []
    try:
        for r in _get_recent_rows(db, ticker,
                                    max_rows=_TIER2_NEWS_BODY_MAX_ARTICLES * 3):
            body = r.get('body')
            ts = r.get('timestamp') or ''
            if not body or not isinstance(body, str):
                continue
            if ts < cutoff:
                continue
            out.append({
                'title': r.get('title') or '',
                'source': r.get('source') or '',
                'url': r.get('url') or '',
                'body': body,
                'timestamp': ts,
            })
            if len(out) >= _TIER2_NEWS_BODY_MAX_ARTICLES:
                break
    except Exception:
        return []
    return out


def prefetch_bodies_for_tier2(
        app, ticker: str,
        log_callback: Optional[Callable[[str, str], None]] = None
        ) -> list:
    """Ensure article bodies are available in `news_cache.raw_data`
    for `ticker`, fetching on demand if the fresh-cache set is below
    the cap.

    Cache-first: if there are already
    >=_TIER2_NEWS_BODY_MAX_ARTICLES fresh bodies, return them and
    DO NOT fetch. Otherwise, fetch the missing ones in order from
    the most-recent headlines (those with a URL), up to the cap.
    Each fetch is wall-time-capped at
    _TIER2_NEWS_BODY_FETCH_TIMEOUT seconds.

    Side-effect: writes successful body extractions back to
    `news_cache.raw_data`.

    Telemetry: emits one line via log_callback (or app._log) of the
    form:
      [tier2-news] AAPL: 5 headlines, 4 bodies ok, 1 failed,
                    18KB, 3.2s

    Returns the list of body dicts the caller may want to use
    (post-fetch). Caller should treat this as best-effort; a result
    of [] means "no bodies available — fall back to headlines."

    NEVER raises. The vote path is the only caller; it must not be
    interrupted by network noise.
    """
    t_start = time.time()
    # Resolve app: caller may pass None — fall back to the module-wide
    # registry tm_teacher_intercept maintains (the same pattern
    # emit_system_event uses). Without an app we have no `db` handle
    # so we degrade silently to "no bodies."
    if app is None:
        try:
            import tm_teacher_intercept as _tm_ic
            app = getattr(_tm_ic, '_registered_app', None)
        except Exception:
            app = None
    db = getattr(app, 'db', None) if app is not None else None
    if db is None or not ticker:
        return []

    tk = (ticker or '').upper()
    log = log_callback
    if log is None and app is not None:
        # Marshal off the vote thread? No — log_callback is already
        # expected to be thread-safe by every other tm_consensus
        # site. Mirror that.
        log = getattr(app, '_log', None)

    def _emit(msg: str, tone: str = 'muted') -> None:
        try:
            if callable(log):
                log(msg, tone)
        except Exception:
            pass

    try:
        # 1. Cache-first read.
        cached = get_cached_bodies(db, tk)
        if len(cached) >= _TIER2_NEWS_BODY_MAX_ARTICLES:
            elapsed = time.time() - t_start
            _emit(
                f"[tier2-news] {tk}: {len(cached)} body(ies) from cache "
                f"(no fetch, {elapsed:.2f}s).",
                'muted')
            return cached

        # 2. Find candidates to fetch. Pull the most-recent rows; pick
        # those that have a URL and don't already have a fresh body.
        rows = _get_recent_rows(
            db, tk,
            max_rows=_TIER2_NEWS_BODY_MAX_ARTICLES * 3)
        cutoff = _ttl_cutoff_iso()
        already_keys = {(c.get('url'), c.get('title')) for c in cached}
        to_fetch = []
        for r in rows:
            url = r.get('url') or ''
            title = r.get('title') or ''
            body = r.get('body')
            ts = r.get('timestamp') or ''
            # already have a fresh body for this one
            if body and isinstance(body, str) and ts >= cutoff:
                continue
            if not url or not url.startswith(('http://', 'https://')):
                continue
            if (url, title) in already_keys:
                continue
            to_fetch.append(r)
            if (len(to_fetch) + len(cached)
                    >= _TIER2_NEWS_BODY_MAX_ARTICLES):
                break

        # If there are no fetchable URLs (the daemon never stored
        # them) we degrade gracefully: emit one line, return what's
        # cached (probably empty), and let the renderer fall back to
        # headlines.
        n_headlines = len(rows)
        if not to_fetch:
            elapsed = time.time() - t_start
            _emit(
                f"[tier2-news] {tk}: {n_headlines} headline(s), "
                f"{len(cached)} body(ies) cached, 0 fetched "
                f"(no fetchable URLs in cache), "
                f"{elapsed:.2f}s.",
                'muted')
            return cached

        # 3. Fetch in parallel — small ThreadPoolExecutor.
        # bodyfix-2026-06-15: bodies come from many different news
        # domains (finnhub/yahoo/google_news/rss surfaced URLs), not
        # one rate-limited API, so modest parallelism is safe. Cap
        # at _TIER2_NEWS_BODY_PARALLEL=5 — small enough to keep the
        # 4 GB / weak-CPU target's UI thread free and not spawn 50
        # threads, large enough that 5 articles overlap on network
        # wait instead of stacking sequentially. Per-article timeout
        # (_TIER2_NEWS_BODY_FETCH_TIMEOUT=8s) is unchanged, so one
        # bad URL still can't stall the others.
        from concurrent.futures import (
            ThreadPoolExecutor, as_completed)
        fetched_ok = 0
        fetched_fail = 0
        total_bytes = 0

        def _fetch_one(r):
            url = r.get('url') or ''
            raw = _http_fetch(url, _TIER2_NEWS_BODY_FETCH_TIMEOUT)
            if not raw:
                return (r, None, 'no_http')
            try:
                html = raw.decode('utf-8', errors='replace')
            except Exception:
                return (r, None, 'decode_fail')
            body = _strip_html(html)
            if not body or _looks_like_paywall_or_junk(body):
                return (r, None, 'junk_or_paywall')
            body = body[:_TIER2_NEWS_BODY_RENDER_MAX_CHARS]
            return (r, body, 'ok')

        max_workers = max(1, min(_TIER2_NEWS_BODY_PARALLEL,
                                  len(to_fetch)))
        with ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix='tier2-news') as ex:
            futures = [ex.submit(_fetch_one, r) for r in to_fetch]
            for fut in as_completed(futures):
                try:
                    r, body, status = fut.result()
                except Exception:
                    fetched_fail += 1
                    continue
                if status != 'ok' or not body:
                    fetched_fail += 1
                    continue
                # Persist + count under the (single-threaded) main
                # path so DB writes don't race. _write_body has its
                # own lock guard but funneling here keeps the order
                # of operations explicit.
                _write_body(db, r.get('id'), body)
                fetched_ok += 1
                total_bytes += len(body)
                cached.append({
                    'title': r.get('title') or '',
                    'source': r.get('source') or '',
                    'url': r.get('url') or '',
                    'body': body,
                    'timestamp': r.get('timestamp') or _now_iso(),
                })

        elapsed = time.time() - t_start
        kb = total_bytes / 1024.0
        _emit(
            f"[tier2-news] {tk}: {n_headlines} headlines, "
            f"{fetched_ok} bodies ok, {fetched_fail} failed, "
            f"{kb:.1f}KB, {elapsed:.2f}s.",
            'muted')
        return cached

    except Exception as e:
        # The whole module is best-effort. NEVER take the vote path
        # down. Surface a single amber line so a real bug is visible.
        _emit(f"[tier2-news] {tk}: prefetch failed silently "
              f"({type(e).__name__}); falling back to headlines.",
              'amber')
        return []
