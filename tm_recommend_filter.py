"""tm_recommend_filter — Step 1 of the three-surface arc (v4.14.5.0).

The two-stage Recommend filter. Pure-function, cache-reads-only, no AI
calls, no global state, no side effects. Step 2 (a separate patch) wires
this into Recommend population; this patch only ships the callable module
plus its audit. the user sees no behaviour change from this patch alone.

Design decisions locked by the user (filter-design investigation Q1-Q5):

  1. Path is NOT a filter dimension. Price band is the per-ticker filter
     dimension; path is an analysis lens applied downstream in the
     prompt, not here. No per-ticker path classification anywhere in
     this module.
  2. Option A — band is the per-ticker filter dimension.
  3. v415_style is orthogonal (prompt flavor); not this module's concern.
  4. Stage-two ranking is price/volume momentum primary; news/social
     are sparse additive bonuses, never gates.
  5. Top ~30 per band; recompute is cheap (cache reads only). This
     module exposes the pure function; the slow-tick wiring is Step 2.

The collapse-bug guard (Section "coverage gate") is the load-bearing
fix for the failure that demoted the original candidate-selection price
filter: filtering before daily_bars is filled collapsed scope to ~0.
Here, if price coverage is below MIN_PRICE_COVERAGE we return a
'cache_filling' status with a partial result instead of a silently
empty list.

Output contract (consumed by Step 2 later):

    {
        'bands': [...selected band names...],
        'price_coverage': 0.0-1.0,
        'status': 'ok' | 'cache_filling',
        'note': '' | '<human-readable qualifier>',
        'candidates': [
            {'ticker', 'price', 'band', 'score',
             'breakdown': {'momentum', 'rel_volume',
                           'social_bonus', 'news_bonus'}},
            ...  # ranked desc by score, alpha tie-break
        ],
    }
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import tm_cache

# Canonical band order. Single source of truth for "which band does this
# price fall in" is tm_cache._price_matches_ranges — we never duplicate
# the numeric thresholds here; _band_for_price probes that function.
BAND_ORDER = ('penny', 'low', 'mid', 'high')

# Collapse-bug guard. If fewer than this fraction of the universe has a
# cached price, the price cache is still filling — return 'cache_filling'
# with a partial result rather than a misleadingly-empty 'ok'.
#
# v4.14.6.5-cache-ungate-tpm-skip (2026-06-11): lowered 0.80 → 0.70.
# Investigation found the universe's structural priceable ceiling is
# ~74% (the remaining ~26% are warrants/delisted/dual-class/foreign
# with no daily-bars source), so 0.80 was permanently triggered against
# a snapshot that's as full as it will ever be — the recommend_cache
# refresh bailed every tick and the new band keys never got rows. 0.70
# fires only when the cache is GENUINELY thin (real cold-start), not
# when it's at the structural ceiling.
MIN_PRICE_COVERAGE = 0.70

# v4.14.6.5-cache-ungate-tpm-skip (2026-06-11): stuck-ceiling detection.
# Even at 0.70, a smaller universe could plateau below the threshold; we
# want the gate to self-clear when the priced_count has STOPPED growing
# (= "this is as full as it gets"). We track the count + an unchanged-
# streak across consecutive compute_filter calls; once the count is
# stable for STUCK_CEILING_TICKS reads, treat the cache as ready
# regardless of the coverage percentage. Reset on growth (real fill is
# resuming → resume normal gating). Tuned generously: 3 consecutive
# stable reads ≈ 15 minutes at the daemon's 5-min refresh tick, long
# enough that genuine cold-start (where the count IS still climbing
# slowly) doesn't false-positive.
STUCK_CEILING_TICKS = 3
_STUCK_CEILING_STATE = {
    'last_priced': -1,   # last seen priced_count
    'streak': 0,         # how many consecutive ticks with priced unchanged
}


def _stuck_ceiling_check(priced: int) -> bool:
    """v4.14.6.5: True when the price cache has plateaued — priced count
    unchanged for STUCK_CEILING_TICKS consecutive compute_filter calls.
    Resets on any growth. Module-level state; pure given the input."""
    st = _STUCK_CEILING_STATE
    last = int(st.get('last_priced', -1))
    if last < 0 or priced > last:
        # First read OR genuine growth → reset.
        st['last_priced'] = priced
        st['streak'] = 0
        return False
    if priced == last:
        st['streak'] = int(st.get('streak', 0)) + 1
        return st['streak'] >= STUCK_CEILING_TICKS
    # priced < last — universe shrunk (e.g. fresh wipe). Reset.
    st['last_priced'] = priced
    st['streak'] = 0
    return False

# v4.14.5.14a: the momentum filter is DEMOTED from a population gate to
# a sort key. When True (default) the per-band top-30 truncation is
# skipped — every in-band candidate is returned, ranked by momentum, so
# Recommend can see all open AI BUYs instead of only the momentum
# leaders (the filter was discarding ~96% of valid BUYs). The
# MIN_PRICE_COVERAGE coverage gate is UNAFFECTED — it stays a hard
# safety. False restores the v4.14.5.13 top-30-per-band gate.
# Wired from cfg['use_filter_as_sort_key'] via set_filter_as_sort_key().
_FILTER_AS_SORT_KEY = True


def set_filter_as_sort_key(enabled: bool) -> None:
    """v4.14.5.14a rollback hook — cfg['use_filter_as_sort_key']."""
    global _FILTER_AS_SORT_KEY
    _FILTER_AS_SORT_KEY = bool(enabled)

# Stage-two scoring constants. Tunable; deliberately simple for v1.
WEIGHT_MOMENTUM = 0.5
WEIGHT_REL_VOLUME = 0.5
SOCIAL_BONUS = 0.10
NEWS_BONUS = 0.05
REL_VOLUME_CAP = 5.0

# Per-band truncation.
TOP_N_PER_BAND = 30

# Lookback bars pulled per ticker: 1 (today) + 5 (momentum) + 20 (volume
# average) with headroom. 21 rows covers a 5-day % change and a 20-bar
# average comfortably.
_LOOKBACK_BARS = 21

# SQLite default host parameter limit is 999; chunk IN() lists safely.
_SQL_IN_CHUNK = 900


def _band_for_price(price: float) -> Optional[str]:
    """Return the single band id a price falls in, or None.

    Reuses tm_cache._price_matches_ranges as the authority on band
    thresholds — this module never hardcodes the $1/$10/$50 cutoffs.
    """
    for band in BAND_ORDER:
        try:
            if tm_cache._price_matches_ranges(price, {band}):
                return band
        except Exception:
            return None
    return None


def _chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _fetch_recent_signal_tickers(conn, table: str, tickers: list,
                                  cutoff_iso: str) -> set:
    """Return the subset of `tickers` that have >=1 row in `table` with
    timestamp >= cutoff_iso. One grouped query per chunk. Best-effort:
    any failure yields an empty set (bonus simply doesn't apply — it is
    never a gate)."""
    if not tickers:
        return set()
    found: set = set()
    # table is a fixed internal literal ('social_signals'/'news_signals'),
    # never user input — safe to interpolate.
    for chunk in _chunked(tickers, _SQL_IN_CHUNK):
        placeholders = ','.join('?' * len(chunk))
        sql = (f"SELECT DISTINCT ticker FROM {table} "
               f"WHERE ticker IN ({placeholders}) "
               f"AND timestamp >= ?")
        try:
            rows = conn.execute(sql, (*chunk, cutoff_iso)).fetchall()
        except Exception:
            return set()
        for r in rows:
            t = r[0]
            if t:
                found.add(str(t).upper())
    return found


def _fetch_bars(conn, tickers: list) -> dict:
    """Return {ticker: [(date, close, volume), ...]} newest-first, up to
    _LOOKBACK_BARS rows per ticker. One windowed query per chunk."""
    out: dict = {}
    if not tickers:
        return out
    for chunk in _chunked(tickers, _SQL_IN_CHUNK):
        placeholders = ','.join('?' * len(chunk))
        sql = (
            "SELECT ticker, date, close, volume FROM ("
            "  SELECT ticker, date, close, volume, "
            "         ROW_NUMBER() OVER ("
            "             PARTITION BY ticker ORDER BY date DESC) AS rn "
            "  FROM daily_bars "
            f"  WHERE ticker IN ({placeholders})"
            ") WHERE rn <= ? ORDER BY ticker, date DESC"
        )
        try:
            rows = conn.execute(
                sql, (*chunk, _LOOKBACK_BARS)).fetchall()
        except Exception:
            # Defensive: a SQL/engine failure here must not crash the
            # filter. Affected tickers fall back to neutral scoring.
            continue
        for r in rows:
            t = str(r[0]).upper()
            out.setdefault(t, []).append((r[1], r[2], r[3]))
    return out


def _raw_momentum(bars: list) -> float:
    """5-day % close change, up-moves only (fallers clamp to 0). Needs >=5
    bars (newest-first). <5 bars -> 0.0 (keeps the ticker rankable, just
    neutral)."""
    if len(bars) < 5:
        return 0.0
    try:
        close_now = float(bars[0][1])
        close_then = float(bars[4][1])
    except (TypeError, ValueError, IndexError):
        return 0.0
    if close_then <= 0:
        return 0.0
    # v4.14.6.110: directional momentum (no abs; clamp negatives to 0)
    return max(0.0, (close_now - close_then) / close_then)


def _raw_rel_volume(bars: list) -> float:
    """today_volume / avg(volume over the prior up-to-20 bars), capped at
    REL_VOLUME_CAP. Insufficient data -> 1.0 (neutral)."""
    if len(bars) < 2:
        return 1.0
    try:
        today_vol = float(bars[0][2])
    except (TypeError, ValueError, IndexError):
        return 1.0
    prior = []
    for b in bars[1:21]:
        try:
            v = float(b[2])
        except (TypeError, ValueError):
            continue
        prior.append(v)
    if not prior:
        return 1.0
    avg = sum(prior) / len(prior)
    if avg <= 0:
        return 1.0
    rv = today_vol / avg
    if rv < 0:
        return 1.0
    return min(rv, REL_VOLUME_CAP)


def _normalize(values: dict) -> dict:
    """Min-max normalize {key: raw} to [0,1]. Degenerate range (all
    equal, or single element) -> 0.0 for every key (deterministic)."""
    if not values:
        return {}
    lo = min(values.values())
    hi = max(values.values())
    if hi <= lo:
        return {k: 0.0 for k in values}
    span = hi - lo
    return {k: (v - lo) / span for k, v in values.items()}


def compute_filter(selected_bands,
                   universe: Optional[set] = None,
                   now: Optional[datetime] = None,
                   config: Optional[dict] = None) -> dict:
    """Run the two-stage Recommend filter. Pure: cache reads + arithmetic
    only. No AI calls, no writes, no global state.

    selected_bands: iterable of band ids ('penny'/'low'/'mid'/'high').
    universe:        optional explicit ticker set; default loads via
                     tm_cache._load_universe_tickers().
    now:             optional datetime for the 24h social/news bonus
                     window (tests inject; default datetime.now(utc)).
    config:          reserved for future tuning knobs; unused in v1.

    Returns the output-contract dict documented at module top.
    """
    bands = {str(b).strip().lower() for b in (selected_bands or [])}
    bands = {b for b in bands if b in BAND_ORDER}

    # Universe resolution.
    if universe is None:
        try:
            universe = tm_cache._load_universe_tickers()
        except Exception:
            universe = set()
    universe = {str(t).upper() for t in (universe or set())}
    universe_size = len(universe)

    # Latest cached price per ticker (grouped query, daily_bars).
    try:
        price_map = tm_cache._get_ticker_latest_prices(universe)
    except Exception:
        price_map = {}
    price_map = {str(k).upper(): v for k, v in (price_map or {}).items()}

    priced_count = sum(1 for t in universe if t in price_map)
    price_coverage = (priced_count / universe_size
                      if universe_size else 0.0)

    # Coverage gate (collapse-bug guard). Determined independently of
    # band selection so callers always learn cache health.
    status = ('ok' if price_coverage >= MIN_PRICE_COVERAGE
              else 'cache_filling')
    # v4.14.6.5-cache-ungate-tpm-skip: stuck-ceiling override. When the
    # priced_count has plateaued (cache is as full as it gets), promote
    # cache_filling → ok regardless of coverage percentage so the
    # recommend_cache refresh stops bailing. _stuck_ceiling_check()
    # tracks the count across compute_filter calls and auto-resets on
    # any growth.
    _stuck_promoted = False
    if status == 'cache_filling':
        try:
            if _stuck_ceiling_check(int(priced_count)):
                status = 'ok'
                _stuck_promoted = True
        except Exception:
            pass
    note = ''
    if universe_size == 0:
        note = 'universe empty or unresolved'
    elif _stuck_promoted:
        note = (f'price cache at structural ceiling '
                f'({priced_count}/{universe_size} priced, '
                f'{price_coverage:.0%}); promoted to ok by stuck-'
                f'ceiling detector (count unchanged for '
                f'{STUCK_CEILING_TICKS} consecutive checks)')
    elif status == 'cache_filling':
        note = (f'price cache still filling '
                f'({priced_count}/{universe_size} priced, '
                f'{price_coverage:.0%})')

    # No bands selected: well-defined empty result, never an error.
    if not bands:
        return {
            'bands': [],
            'price_coverage': price_coverage,
            'status': status,
            'note': note or 'no bands selected',
            'candidates': [],
        }

    # Stage 1: band membership over priced tickers. Missing-price
    # tickers are skipped here (held out, not "excluded forever") —
    # the coverage gate above already reflected them.
    in_band: dict = {}  # ticker -> (price, band)
    for ticker in universe:
        price = price_map.get(ticker)
        if price is None:
            continue
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            continue
        band = _band_for_price(price_f)
        if band is not None and band in bands:
            in_band[ticker] = (price_f, band)

    if not in_band:
        return {
            'bands': sorted(bands),
            'price_coverage': price_coverage,
            'status': status,
            'note': note,
            'candidates': [],
        }

    # Stage 2: activity ranking. Cache reads only.
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff_iso = (now - timedelta(hours=24)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    tickers = sorted(in_band.keys())

    conn = None
    bars_by_ticker: dict = {}
    social_tickers: set = set()
    news_tickers: set = set()
    try:
        conn = tm_cache.get_connection()
        bars_by_ticker = _fetch_bars(conn, tickers)
        social_tickers = _fetch_recent_signal_tickers(
            conn, 'social_signals', tickers, cutoff_iso)
        news_tickers = _fetch_recent_signal_tickers(
            conn, 'news_signals', tickers, cutoff_iso)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    raw_mom: dict = {}
    raw_rv: dict = {}
    for t in tickers:
        bars = bars_by_ticker.get(t, [])
        raw_mom[t] = _raw_momentum(bars)
        raw_rv[t] = _raw_rel_volume(bars)

    norm_mom = _normalize(raw_mom)
    norm_rv = _normalize(raw_rv)

    scored: list = []
    for t in tickers:
        price_f, band = in_band[t]
        social_b = SOCIAL_BONUS if t in social_tickers else 0.0
        news_b = NEWS_BONUS if t in news_tickers else 0.0
        nm = norm_mom.get(t, 0.0)
        nrv = norm_rv.get(t, 0.0)
        score = (WEIGHT_MOMENTUM * nm
                 + WEIGHT_REL_VOLUME * nrv
                 + social_b + news_b)
        scored.append({
            'ticker': t,
            'price': price_f,
            'band': band,
            'score': score,
            'breakdown': {
                'momentum': nm,
                'rel_volume': nrv,
                'social_bonus': social_b,
                'news_bonus': news_b,
            },
        })

    # Per-band truncation to TOP_N_PER_BAND, then a single globally
    # ranked list. Sort key: score desc, ticker asc (deterministic).
    by_band: dict = {}
    for c in scored:
        by_band.setdefault(c['band'], []).append(c)

    candidates: list = []
    for band_id, items in by_band.items():
        items.sort(key=lambda c: (-c['score'], c['ticker']))
        if _FILTER_AS_SORT_KEY:
            # v4.14.5.14a: no gate — every in-band candidate flows
            # through, ranked by momentum. The downstream consumer
            # (recommend_cache) intersects with open BUYs.
            candidates.extend(items)
        else:
            # Legacy v4.14.5.13 behaviour: hard top-30-per-band cut.
            candidates.extend(items[:TOP_N_PER_BAND])
    candidates.sort(key=lambda c: (-c['score'], c['ticker']))

    return {
        'bands': sorted(bands),
        'price_coverage': price_coverage,
        'status': status,
        'note': note,
        'candidates': candidates,
    }
