"""
tm_data_adapter_yahoo.py — Yahoo + Stooq adapter (v4.13.55, extended v4.14.2 stage 1)

What this is:
    Thin wrappers around the existing yahoo_quote / yahoo_history /
    stooq_quote functions in tired_market.py. The adapter pattern lets
    the data router treat Yahoo/Stooq the same way it treats Finnhub
    and EDGAR — no special-casing in the router.

What's served (v4.14.2 stage 1):
    - yahoo  serves: 'price', 'history' (priority 1 — primary)
    - yahoo  serves: 'fundamentals', 'earnings', 'news' (priority 2 —
                     keyless backup for users without a Finnhub key, or
                     when Finnhub silently drops a ticker from its bulk
                     calendar — see BUILT.md v4.14.1.3 hotfix entry).
    - stooq  serves: 'price', 'history' (fallback priority for both)

Yahoo as keyless primary:
    Per DECISIONS 2026-05-09 lane-policy amendment, every lane needs a
    keyless primary so users without optional API keys still get usable
    data. Yahoo (via yfinance) covers fundamentals/earnings/news for
    that role. Finnhub stays at priority 1 — Yahoo only fires when
    Finnhub is unavailable, rate-limited, or absent.

Output shapes:
    price -> {
        'price': float,
        'change_pct': float,
        'volume': int,
        'prev_close': float | None,
        'source': str,
        'as_of': iso_string,
    }

    history -> {
        'bars': [
            {'date': 'YYYY-MM-DD', 'open': float, 'high': float,
             'low': float, 'close': float, 'volume': int},
            ...
        ],
        'count': int,
        'period': str,
        'as_of': iso_string,
    }

    fundamentals -> matches Finnhub's adapter shape (company_name,
        sector, industry, market_cap, shares_outstanding, pe_ratio,
        eps, beta, dividend_yield, as_of). yfinance splits sector vs
        industry where Finnhub conflates them.

    earnings -> matches Finnhub's bulk shape (events list with
        ticker, date, eps_estimate, revenue_estimate, hour, ...) but
        always one event per call — yfinance's Ticker.calendar only
        surfaces the next upcoming event. eps_actual / last-quarter
        data require a different yfinance call (Ticker.earnings_dates,
        which needs lxml — deferred to a later stage).

    news -> matches Finnhub's adapter shape (headlines list with
        title, summary, source, url, timestamp_iso, sentiment).
        Sentiment scored locally via the same _score_headline keyword
        pattern Finnhub uses (imported from the Finnhub adapter so the
        scoring logic stays single-source).

Implementation note (v4.14.2):
    Price and history go through the configure()-injected callables
    (so existing yahoo_quote / yahoo_history caching applies). The
    new fundamentals/earnings/news branches import yfinance lazily
    inside each branch — no callable injection — because there's no
    pre-existing in-app fetcher to wrap. If a future stage adds a
    cached fetcher in tired_market.py, those branches can be migrated
    to the injection pattern without changing the adapter contract.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from tm_data_router import RateLimitError

# v4.14.5.14-earnings-architecture-fix: quiet breadcrumb for degraded Yahoo
# fetches. Library-style logger — silent unless the host enables DEBUG, so a
# flaky yfinance call never alarms the user-facing activity log.
_logger = logging.getLogger(__name__)


def _is_yfinance_rate_limit(e: BaseException) -> bool:
    """v4.14.5.14-yahoo-ratelimit-fix: True when `e` is a yfinance rate-limit.

    The fundamentals + earnings branches USED to swallow ALL yfinance
    exceptions to `return None` (Cascade Fix 1 — to avoid demoting Yahoo to
    health='red' on a transient hiccup). That made a Yahoo IP rate-limit
    indistinguishable from "this ticker has no data" — and the fundamentals
    empty-cache (yesterday's patch) then wrote `status='empty'` (a 7-day
    skip) for every real company while Yahoo was throttled. Rate-limits MUST
    classify as 'failed' (retry + cooldown via Cascade Fix 3), not 'empty'.

    Detection prefers the exact class `yfinance.exceptions.YFRateLimitError`;
    falls back to a string sniff (`429` / `rate` / `Too Many Requests`) so a
    yfinance class rename doesn't silently re-introduce the bug."""
    try:
        from yfinance.exceptions import YFRateLimitError as _YFRL
        if isinstance(e, _YFRL):
            return True
    except Exception:
        pass
    msg = (str(e) or '').lower()
    return ('429' in msg
            or 'too many requests' in msg
            or 'rate limit' in msg or 'rate-limit' in msg
            or 'rate_limit' in msg
            or 'ratelimit' in msg)


# ─── Module-level callables, injected by the main app ─────────────────
#
# We keep these as module globals so adapters() doesn't need a context
# argument. The main app calls `configure()` once at startup with the
# functions it already has in scope (yahoo_quote, yahoo_history, etc.).

_yahoo_quote_fn: Callable | None = None
_yahoo_history_fn: Callable | None = None
_stooq_quote_fn: Callable | None = None
# v4.14.5.42-price-spread-foundations: the real Stooq history fetch (+daily_bars
# cache tap), injected by configure(). None until wired (older callers).
_stooq_history_fn: Callable | None = None

# v4.14.5.13: when True, a successful Yahoo *fundamentals* fallback
# writes the row to cache.fundamentals AND stamps
# cache_metadata('fundamentals') — exactly like the Finnhub path
# already does. Before this fix the Yahoo fallback returned data to
# the caller but cached nothing, so any ticker Finnhub couldn't serve
# stayed permanently "stale" and got re-fetched every 30-min daemon
# tick forever (the third instance of the stamp-on-success defect
# class: daily_bars v4.14.5.6, EDGAR v4.14.5.9, this).
# cfg['use_yahoo_fundamentals_stamp']; False restores old behaviour.
_YAHOO_FUND_STAMP_ENABLED = True


def set_yahoo_fundamentals_stamp(enabled: bool) -> None:
    """v4.14.5.13: rollback hook — cfg['use_yahoo_fundamentals_stamp']."""
    global _YAHOO_FUND_STAMP_ENABLED
    _YAHOO_FUND_STAMP_ENABLED = bool(enabled)


def configure(yahoo_quote_fn: Callable,
              yahoo_history_fn: Callable,
              stooq_quote_fn: Callable | None = None,
              stooq_history_fn: Callable | None = None) -> None:
    """Inject the existing tired_market.py fetchers. Called once at
    app startup before any router usage. Idempotent — calling again
    overwrites the registered functions.

    v4.14.5.42-price-spread-foundations: `stooq_history_fn` is new and
    optional (defaults None → the stooq adapter's 'history' branch stays a
    no-op exactly as before for any caller that doesn't wire it)."""
    global _yahoo_quote_fn, _yahoo_history_fn, _stooq_quote_fn, _stooq_history_fn
    _yahoo_quote_fn = yahoo_quote_fn
    _yahoo_history_fn = yahoo_history_fn
    _stooq_quote_fn = stooq_quote_fn
    _stooq_history_fn = stooq_history_fn


# ─── Yahoo adapter ────────────────────────────────────────────────────

def yahoo_adapter(profile, data_type: str, **kwargs):
    """Adapter for the 'yahoo' provider. Serves price + history."""
    # v4.15.0 Step 12: offline short-circuit.
    try:
        import tm_network as _tmn
        if not _tmn.is_online():
            return None
    except Exception:
        pass
    if _yahoo_quote_fn is None or _yahoo_history_fn is None:
        # Not configured — caller didn't wire up the existing functions
        return None

    if data_type == 'price':
        ticker = kwargs.get('ticker', '')
        if not ticker:
            return None
        try:
            raw = _yahoo_quote_fn(ticker)
        except Exception as e:
            # If yahoo_quote internally hit a rate limit, that's already
            # handled inside (it falls through to Stooq automatically).
            # We treat exceptions here as generic failures.
            raise RuntimeError(f"yahoo_quote failed: {e}") from e
        if not raw:
            return None
        # yahoo_quote already returns the right shape. Normalize keys.
        return {
            'price': raw.get('price'),
            'change_pct': raw.get('change_pct', 0),
            'volume': raw.get('volume', 0),
            'prev_close': raw.get('prev_close'),
            'source': raw.get('source', 'Yahoo'),
            'ticker': ticker.upper(),
            'as_of': datetime.now().isoformat(timespec='seconds'),
        }

    if data_type == 'history':
        ticker = kwargs.get('ticker', '')
        if not ticker:
            return None
        period = kwargs.get('period', '1y')
        try:
            df = _yahoo_history_fn(ticker, period=period)
        except Exception as e:
            raise RuntimeError(f"yahoo_history failed: {e}") from e
        if df is None or (hasattr(df, 'empty') and df.empty):
            return None
        # Convert pandas DataFrame to our canonical bar list.
        bars = []
        try:
            for idx, row in df.iterrows():
                date_str = (idx.strftime('%Y-%m-%d')
                             if hasattr(idx, 'strftime') else str(idx))
                bars.append({
                    'date': date_str,
                    'open': float(row.get('Open', 0) or 0),
                    'high': float(row.get('High', 0) or 0),
                    'low': float(row.get('Low', 0) or 0),
                    'close': float(row.get('Close', 0) or 0),
                    'volume': int(row.get('Volume', 0) or 0),
                })
        except Exception:
            return None

        if not bars:
            return None

        return {
            'bars': bars,
            'count': len(bars),
            'period': period,
            'ticker': ticker.upper(),
            'as_of': datetime.now().isoformat(timespec='seconds'),
        }

    # ── v4.14.2 stage 1: keyless-primary lane work ───────────────────
    # Yahoo serves fundamentals / earnings / news at priority 2 so
    # users without a Finnhub key (or tickers Finnhub silently drops)
    # still get a usable prompt block. Imports are lazy inside each
    # branch — yfinance is only loaded when the branch fires.

    if data_type == 'fundamentals':
        ticker = kwargs.get('ticker', '')
        if not ticker:
            return None
        try:
            import yfinance as _yf
            info = _yf.Ticker(ticker).info
        except Exception as e:
            # v4.14.5.14-yahoo-ratelimit-fix: distinguish rate-limit (raise
            # RateLimitError → router records 'failed' → empty-cache CORRECTLY
            # skips it + Cascade Fix 3 applies a cooldown) from a genuine
            # transient hiccup (still return None, preserving Cascade Fix 1's
            # "no Yahoo red-demotion on a single bad call"). The earlier
            # blanket swallow-to-None turned a Yahoo IP rate-limit into
            # 104+ false-empty cache rows in a single morning cycle (incl.
            # real companies like MRVL/NTAP/NTNX) because the empty-cache
            # can't distinguish "source faulted" from "source said empty".
            if _is_yfinance_rate_limit(e):
                raise RateLimitError(
                    f"yahoo fundamentals rate-limited: {e}") from e
            _logger.debug("yahoo fundamentals .info unavailable for %s: %s; "
                          "moving on", ticker, e)
            return None
        if not isinstance(info, dict) or not info:
            return None
        # yfinance returns a mostly-empty dict on delisted/invalid
        # tickers. company_name absence is the cleanest sentinel.
        company_name = (info.get('longName')
                         or info.get('shortName')
                         or '')
        if not company_name:
            return None
        result = {
            'company_name': company_name,
            'sector':       info.get('sector') or '',
            # yfinance splits sector vs industry; Finnhub conflates.
            'industry':     info.get('industry') or '',
            'market_cap':   info.get('marketCap'),
            'shares_outstanding': info.get('sharesOutstanding'),
            # v4.14.6.111 (Tier-2 float): true tradeable float — ALREADY in the
            # same .info response (no extra fetch), previously discarded. Feeds
            # the algo's low-float score contribution; None when Yahoo has no
            # float for the ticker (→ stored NULL → 0 contribution downstream).
            'float_shares': info.get('floatShares'),
            # v4.14.6.111 (Tier-3 short interest): squeeze/positioning fields —
            # ALREADY in the same .info response, previously discarded. short_
            # percent_float is the KEY field (short % of float, a FRACTION e.g.
            # 0.0098); date_short_interest (epoch) drives the staleness/lag
            # guard (FINRA bi-monthly settlement → can be ~2 weeks stale). None
            # when absent (→ NULL → 0 contribution downstream, use-if-present).
            'short_percent_float': info.get('shortPercentOfFloat'),
            'date_short_interest': info.get('dateShortInterest'),
            # trailingPE preferred; fall back to forwardPE for tickers
            # with no trailing earnings (e.g. recent IPOs, RIG-style
            # loss-makers).
            'pe_ratio':     (info.get('trailingPE')
                              or info.get('forwardPE')),
            'eps':          info.get('trailingEps'),
            'beta':         info.get('beta'),
            # yfinance's dividendYield is already in percent — no
            # scale fix needed.
            'dividend_yield': info.get('dividendYield'),
            # v4.14.5.62-analyst-facts: carry the analyst consensus +
            # mean price target (already in `.info`, no extra fetch) so
            # the FACTS block can surface them when surface_analyst_facts
            # is on. Defensive None when absent (no analyst coverage).
            'recommendation_key': info.get('recommendationKey'),
            'target_mean_price':  info.get('targetMeanPrice'),
            'as_of':        datetime.now().isoformat(timespec='seconds'),
        }
        # v4.14.5.13: stamp-on-success. Mirrors the Finnhub path's
        # cache.fundamentals write + cache_metadata stamp so a ticker
        # Finnhub can't serve (dual-class/delisted tail) finally
        # converges instead of being re-fetched every daemon tick.
        # Side-effect only; the router still receives `result`.
        if _YAHOO_FUND_STAMP_ENABLED:
            try:
                _v415_cache_write_fundamentals_yahoo(ticker, result)
            except Exception:
                pass
        return result

    if data_type == 'earnings':
        ticker = kwargs.get('ticker', '')
        if not ticker:
            return None
        # v4.14.5.14-earnings-architecture-fix (fix #2): harden the flaky
        # yfinance `.calendar` property. It throws intermittently (Yahoo
        # rate-limits / endpoint hiccups) — previously that raised
        # RuntimeError, which bubbled to the router as a LAST error and
        # surfaced as a red "All sources failed (yahoo: RuntimeError)" alarm.
        # Yahoo is the keyless LAST-resort backup here, so a per-ticker
        # `.calendar` failure means "no earnings data for this ticker right
        # now" — degrade quietly to None (router records 'returned no data',
        # status 'empty') instead of raising. True infra outages still surface
        # via Finnhub's RateLimitError/ConnectionError -> status 'failed'.
        try:
            import yfinance as _yf
            cal = _yf.Ticker(ticker).calendar
        except Exception as e:
            # v4.14.5.14-yahoo-ratelimit-fix: same rate-limit-vs-no-data
            # distinction as the fundamentals branch above. A YFRateLimitError
            # must raise to the router (status='failed' → earnings cache row
            # is NOT written as status='empty'); a normal `.calendar` hiccup
            # still degrades quietly to None so Yahoo stays eligible.
            if _is_yfinance_rate_limit(e):
                raise RateLimitError(
                    f"yahoo earnings rate-limited: {e}") from e
            _logger.debug("yahoo .calendar unavailable for %s: %s; "
                          "moving on", ticker, e)
            return None
        if not isinstance(cal, dict):
            return None
        # 'Earnings Date' is a list (usually len 1) of date objects.
        # Could be empty for tickers with no scheduled earnings.
        dates = cal.get('Earnings Date') or []
        if not dates:
            return None
        first_date = dates[0]
        date_iso = (first_date.isoformat()
                     if hasattr(first_date, 'isoformat')
                     else str(first_date))
        # eps_actual + last_quarter need Ticker.earnings_dates which
        # requires lxml; deferred to a later stage. Per-ticker Yahoo
        # only surfaces the upcoming event, which is the bug-fix
        # target for v4.14.1.3's empty EARNINGS block.
        event = {
            'ticker':           ticker.upper(),
            'date':             date_iso,
            'eps_estimate':     cal.get('Earnings Average'),
            'eps_actual':       None,
            'revenue_estimate': cal.get('Revenue Average'),
            'revenue_actual':   None,
            # Yahoo's calendar doesn't carry bmo/amc; leave blank.
            'hour':             '',
            'quarter':          None,
            'year':             None,
        }
        return {
            'events': [event],
            'count':  1,
            'as_of':  datetime.now().isoformat(timespec='seconds'),
        }

    if data_type == 'news':
        ticker = kwargs.get('ticker', '')
        if not ticker:
            return None
        try:
            import yfinance as _yf
            articles = _yf.Ticker(ticker).news
        except Exception as e:
            raise RuntimeError(
                f"yahoo news failed: {e}") from e
        if not articles:
            return None
        # Sentiment scoring shared with the Finnhub adapter so both
        # sources produce comparable [-1, +1] scores. Lazy import
        # keeps this adapter file standalone.
        try:
            from tm_data_adapter_finnhub import _score_headline
        except Exception:
            _score_headline = lambda _t: 0.0
        headlines = []
        for item in articles:
            if not isinstance(item, dict):
                continue
            # yfinance 1.x wraps article fields under 'content'.
            # Older versions had a flat shape; fall back to item
            # itself if 'content' isn't a dict.
            content = (item.get('content')
                        if isinstance(item.get('content'), dict)
                        else item)
            title = (content.get('title') or '').strip()
            if not title or len(title) < 5:
                continue
            summary = (content.get('summary')
                        or content.get('description')
                        or '').strip()[:500]
            provider = content.get('provider') or {}
            source = (provider.get('displayName')
                       if isinstance(provider, dict)
                       else 'Yahoo')
            canonical = content.get('canonicalUrl') or {}
            url = (canonical.get('url')
                    if isinstance(canonical, dict)
                    else '')
            if not url:
                click = content.get('clickThroughUrl') or {}
                url = (click.get('url')
                        if isinstance(click, dict)
                        else '')
            ts_iso = (content.get('pubDate')
                       or content.get('displayTime')
                       or '')
            headlines.append({
                'title':         title,
                'summary':       summary,
                'source':        source or 'Yahoo',
                'url':           url or '',
                'timestamp_iso': ts_iso,
                'sentiment':     _score_headline(title),
            })
        if not headlines:
            return None
        return {
            'headlines': headlines,
            'count':     len(headlines),
            'as_of':     datetime.now().isoformat(timespec='seconds'),
        }

    # ── v4.14.2 stage 4: macro lane (keyless backup for FRED) ────────
    # Yahoo's index tickers carry the headline yields + VIX cleanly
    # without an API key. FRED owns Fed funds, CPI, unemployment,
    # GDP, and the full series catalog when the user adds a key.
    # Returns global (no ticker) — kwargs.get('ticker') ignored.
    if data_type == 'macro':
        # Map Yahoo index symbol -> normalized output key. yfinance's
        # fast_info / .info return Treasury indices already in
        # percentage form (e.g. ^TNX last_price ~= 4.36 for a 4.36%
        # yield) — NOT the website's display form (43.64). Took a
        # round of in-place verification to nail this down; the
        # original adapter draft assumed the *10 display form and
        # produced sub-1% yields. ^VIX is the VIX level directly,
        # no scaling needed either way.
        symbols = (
            ('^TNX',  'treasury_10y'),
            ('^FVX',  'treasury_5y'),
            ('^IRX',  'treasury_13w'),
            ('^TYX',  'treasury_30y'),
            ('^VIX',  'vix'),
        )
        out: dict = {}
        # v4.15.0 step 8: side dict capturing raw ticker → price so the
        # cache tap can write rows keyed by ^TNX / ^VIX (what Step 7's
        # read helper expects), not the friendly keys that `out` holds.
        _v415_raw_macro_values: dict = {}
        try:
            import yfinance as _yf
        except Exception as e:
            raise RuntimeError(
                f"yahoo macro: yfinance import failed: {e}") from e
        for sym, out_key in symbols:
            try:
                t = _yf.Ticker(sym)
                # fast_info skips the slow .info scrape; macro indexes
                # populate the price field reliably.
                fi = getattr(t, 'fast_info', None)
                price = None
                if fi is not None:
                    for attr in ('last_price', 'last_close',
                                  'previous_close', 'regular_market_price'):
                        v = getattr(fi, attr, None)
                        if v is not None:
                            price = float(v)
                            break
                if price is None:
                    info = t.info or {}
                    p = (info.get('regularMarketPrice')
                          or info.get('currentPrice')
                          or info.get('previousClose'))
                    if p is not None:
                        price = float(p)
                if price is None:
                    continue
                rounded = round(price, 4)
                out[out_key] = rounded
                _v415_raw_macro_values[sym] = rounded
            except Exception:
                # One bad symbol shouldn't tank the whole macro snapshot.
                continue
        if not out:
            return None
        # Derived: yield-curve spread (10Y minus 2Y). Yahoo doesn't
        # publish a 2Y index directly, so we compute spread vs 5Y as
        # an approximation when both are present, and surface 10Y vs
        # 13W as a coarser short-end indicator.
        if 'treasury_10y' in out and 'treasury_5y' in out:
            out['curve_spread_10y_5y'] = round(
                out['treasury_10y'] - out['treasury_5y'], 4)
        if 'treasury_10y' in out and 'treasury_13w' in out:
            out['curve_spread_10y_3m'] = round(
                out['treasury_10y'] - out['treasury_13w'], 4)
        out['source'] = 'yahoo'
        out['as_of'] = datetime.now().isoformat(timespec='seconds')

        # v4.15.0 step 8: tap into cache.macro_indicators. Side-effect
        # only; caller still receives the same dict.
        try:
            _v415_cache_write_macro_yahoo_adapter(_v415_raw_macro_values)
        except Exception:
            pass

        return out

    # Yahoo doesn't serve filings (EDGAR is authoritative there).
    return None


# ─── Stooq adapter (price-only fallback) ──────────────────────────────

def stooq_adapter(profile, data_type: str, **kwargs):
    """Adapter for 'stooq' provider. Serves price and — as of
    v4.14.5.42-price-spread-foundations — REAL history (was a no-op).

    Stooq is per-ticker (no batch API; ~30 req/min); the 'history' branch is
    a genuine per-ticker daily-bars fetch, NOT a faked batch. It writes the
    daily_bars cache via the wired stooq_history_fn (same tap yahoo_history's
    Stooq tier uses) and returns the SAME canonical bar-list shape
    yahoo_adapter('history') returns, so a consumer can't tell whether a bar
    came from Yahoo or Stooq.
    """
    if data_type not in ('price', 'history'):
        return None
    # v4.15.0 Step 12: offline short-circuit (applies to both branches).
    try:
        import tm_network as _tmn
        if not _tmn.is_online():
            return None
    except Exception:
        pass

    if data_type == 'price':
        if _stooq_quote_fn is None:
            return None
        ticker = kwargs.get('ticker', '')
        if not ticker:
            return None
        try:
            raw = _stooq_quote_fn(ticker)
        except Exception as e:
            raise RuntimeError(f"stooq_quote failed: {e}") from e
        if not raw:
            return None
        return {
            'price': raw.get('price'),
            'change_pct': raw.get('change_pct', 0),
            'volume': raw.get('volume', 0),
            'prev_close': raw.get('prev_close'),
            'source': 'Stooq',
            'ticker': ticker.upper(),
            'as_of': datetime.now().isoformat(timespec='seconds'),
        }

    # data_type == 'history' — v4.14.5.42 real Stooq daily bars.
    if _stooq_history_fn is None:
        return None  # not wired (older caller) → behave as the old no-op
    ticker = kwargs.get('ticker', '')
    if not ticker:
        return None
    period = kwargs.get('period', '1y')
    try:
        df = _stooq_history_fn(ticker, period=period)
    except Exception as e:
        raise RuntimeError(f"stooq_history failed: {e}") from e
    if df is None or (hasattr(df, 'empty') and df.empty):
        return None
    # Convert the DataFrame to the canonical bar list (identical shape to
    # yahoo_adapter('history')). Stooq CSV columns match yfinance's
    # Open/High/Low/Close/Volume after the Date index is set.
    bars = []
    try:
        for idx, row in df.iterrows():
            date_str = (idx.strftime('%Y-%m-%d')
                         if hasattr(idx, 'strftime') else str(idx))
            bars.append({
                'date': date_str,
                'open': float(row.get('Open', 0) or 0),
                'high': float(row.get('High', 0) or 0),
                'low': float(row.get('Low', 0) or 0),
                'close': float(row.get('Close', 0) or 0),
                'volume': int(row.get('Volume', 0) or 0),
            })
    except Exception:
        return None
    if not bars:
        return None
    return {
        'bars': bars,
        'count': len(bars),
        'period': period,
        'ticker': ticker.upper(),
        'source': 'Stooq',
        'as_of': datetime.now().isoformat(timespec='seconds'),
    }


def _v415_cache_write_macro_yahoo_adapter(values_dict) -> None:
    """v4.15.0 step 8: Side-effect write of Yahoo macro adapter values
    into tm_cache.macro_indicators.

    Accepts a dict of {symbol: value} where symbol is a Yahoo macro
    ticker (^TNX, ^VIX, etc.) and value is the current scalar value.
    Writes one macro_indicators row per symbol, dated today UTC. UPSERT
    semantics mean same-day re-fetches overwrite cleanly.

    Series IDs are uppercased to match Step 5's macro-ticker redirect
    convention (`_v415_cache_write_macro_yahoo` in tired_market.py),
    so both write paths produce identical row shapes regardless of
    whether the symbol arrived via `yahoo_history` or via this adapter.

    No `cache_metadata` write — macro is universe-wide per Step 5's
    design decision; staleness is computed from row dates directly by
    Step 7's `_v415_cache_read_macro` read helper.

    Side-effect only. Failures are silently swallowed so the data path
    is never disturbed.
    """
    if not values_dict:
        return
    try:
        import tm_cache
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        rows = []
        for symbol, value in values_dict.items():
            try:
                if value is None:
                    continue
                fv = float(value)
                if fv != fv:  # NaN check
                    continue
                sid = str(symbol).upper()
                if not sid:
                    continue
                rows.append({
                    'series_id': sid,
                    'date': today,
                    'value': fv,
                })
            except (TypeError, ValueError):
                continue
        if rows:
            tm_cache.upsert_macro_indicators(rows)
    except Exception:
        return


def _v415_cache_write_fundamentals_yahoo(ticker: str, data: dict) -> None:
    """v4.14.5.13: Yahoo fundamentals snapshot → tm_cache.fundamentals
    row + cache_metadata stamp.

    Deliberately mirrors `tm_data_adapter_finnhub._v415_cache_write_
    fundamentals` (same synthetic '__current__' fiscal_period_end so
    successive snapshots UPSERT into one row per (ticker, source); same
    cache_metadata widen-and-stamp). The ONLY differences are source=
    'yahoo' and reading from Yahoo's result keys ('eps',
    'shares_outstanding'). Statement columns stay None — Yahoo's free
    .info doesn't expose quarterly statements.

    Side-effect only. Failures are silently swallowed so the data path
    is never disturbed.
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
        # v4.14.6.111 (Tier-2 float): persist the float carried in `data`.
        'float_shares': _i(data.get('float_shares')),
        # v4.14.6.111 (Tier-3 short interest): persist the short fields in `data`.
        'short_percent_float': _f(data.get('short_percent_float')),
        'date_short_interest': _i(data.get('date_short_interest')),
        'source': 'yahoo',
    }

    try:
        tm_cache.upsert_fundamentals([row])
    except Exception:
        return

    try:
        today = tm_cache.iso_now()[:10]
        existing = tm_cache.get_cache_metadata(ticker.upper(),
                                               'fundamentals')
        if existing:
            md = existing[0]
            md_keys = md.keys() if hasattr(md, 'keys') else []
            current_from = (md['have_from_date']
                            if 'have_from_date' in md_keys else None)
            current_to = (md['have_to_date']
                          if 'have_to_date' in md_keys else None)
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
        return


# ─── Registration helper ──────────────────────────────────────────────

def register_with(router) -> None:
    """Register both Yahoo and Stooq adapters with the router."""
    router.register_adapter('yahoo', yahoo_adapter)
    router.register_adapter('stooq', stooq_adapter)
