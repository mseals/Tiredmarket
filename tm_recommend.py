"""
Tired Market — Portfolio recommendation engine.

Takes the AI's open BUY predictions + a cash amount + a date filter,
produces three deterministic basket recommendations:

- ALL IN: single position, biggest bet on highest-conviction asymmetric setup
- A FEW: 2-3 positions, weighted toward higher conviction
- DIVERSIFY: 4-6 positions, spread cash across smaller stakes

This is PURE MATH on existing prediction data. No new AI calls. The AI
already decided what's a BUY, what the target is, what the stop is, what
the timeframe is — this module just translates those into "given $X, here's
what shares look like."

DESIGN PRINCIPLES (after the user's correct callout):

1. NO probability claims. We don't show "EV at 50% accuracy" or any math
   that pretends the user experiences averages over many trades. With one
   trade and $100, you experience THIS outcome, not an average.

2. SHOW SHAPE, not predictions. Reward/risk ratios, timeframes, dollar
   ranges — these are facts about each setup. They let the user pick
   based on what kind of trade they want, not based on probability magic.

3. HONEST about constraints. At $100 budget, integer share rounding makes
   real diversification hard. We surface this rather than hiding it.

4. NO sell recommendations. Recommendations are for "what to BUY with
   available cash." Sell decisions are separate (and require user
   judgment we're not trying to replace).

5. NO new AI calls. Pure deterministic logic. Fast, transparent,
   reproducible.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional


# v4.13.39: path enable/disable config. Loaded lazily from
# data/path_config.json. Format:
#   {
#     "paths": {
#       "aggressive": {"enabled": true},
#       "lottery": {"enabled": false, "reason": "0% hit rate on 76 closed"},
#       ...
#     }
#   }
# Missing paths default to enabled. Missing/malformed file defaults to
# all paths enabled (legacy behavior).
_PATH_CONFIG_CACHE: dict | None = None
_PATH_CONFIG_LOADED = False


def _load_path_config() -> dict:
    """Return the path config dict, loaded once and cached.

    Returns {} if no config file or malformed. Callers should treat
    missing entries as 'enabled' for backwards compatibility.
    """
    global _PATH_CONFIG_CACHE, _PATH_CONFIG_LOADED
    if _PATH_CONFIG_LOADED:
        return _PATH_CONFIG_CACHE or {}
    _PATH_CONFIG_LOADED = True
    try:
        from pathlib import Path as _P39
        import json as _json39
        candidates = [
            _P39('data') / 'path_config.json',
        ]
        try:
            here = _P39(__file__).parent
            candidates.append(here / 'data' / 'path_config.json')
            candidates.append(here / 'path_config.json')
        except Exception:
            pass
        for p in candidates:
            try:
                if p.exists():
                    with open(p, 'r', encoding='utf-8') as f:
                        data = _json39.load(f)
                    if isinstance(data, dict):
                        _PATH_CONFIG_CACHE = data
                        return data
            except Exception:
                continue
        _PATH_CONFIG_CACHE = {}
        return {}
    except Exception:
        _PATH_CONFIG_CACHE = {}
        return {}


def is_path_enabled(path_name: str | None) -> bool:
    """v4.13.39: return whether the named path is enabled in config.

    Defaults to True for unknown paths or missing config (legacy
    behavior — paths without explicit config are enabled).
    Pass None or empty string → True (no path filter applied).
    """
    if not path_name:
        return True
    cfg = _load_path_config()
    paths = cfg.get('paths', {}) if isinstance(cfg, dict) else {}
    entry = paths.get(str(path_name).strip().lower())
    if not isinstance(entry, dict):
        return True  # unknown path → enabled by default
    return bool(entry.get('enabled', True))


def get_path_config_summary() -> dict:
    """v4.13.39: return a dict of path_name -> {enabled, reason} for
    every configured path. Used for startup logging.
    """
    cfg = _load_path_config()
    paths = cfg.get('paths', {}) if isinstance(cfg, dict) else {}
    out = {}
    if isinstance(paths, dict):
        for name, entry in paths.items():
            if isinstance(entry, dict):
                out[name] = {
                    'enabled': bool(entry.get('enabled', True)),
                    'reason': str(entry.get('reason', '') or ''),
                }
    return out


def _safe_float(v, default: float = 0.0) -> float:
    """Convert to float, return default on any failure."""
    try:
        if v is None:
            return default
        return float(v)
    except (ValueError, TypeError):
        return default


def _parse_timeframe_days(tf) -> tuple[int, int]:
    """Parse a timeframe string like '2-4 weeks' or '30 days' into a
    (min_days, max_days) tuple. Returns (7, 30) on failure as a generic
    default. The AI's stated timeframes are squishy by nature; this is
    just for sorting/display, not precise calculation.

    v4.9.5: also accepts int/float inputs (predictions store
    timeframe_days as an int now). For numeric inputs, returns a
    +/- 50% range around the value for display.
    """
    if tf is None:
        return (7, 30)
    # v4.9.5: handle numeric inputs (int or float)
    if isinstance(tf, (int, float)) and not isinstance(tf, bool):
        try:
            d = int(tf)
            if d <= 0:
                return (7, 30)
            return (max(1, d // 2), int(d * 1.5))
        except Exception:
            return (7, 30)
    if not isinstance(tf, str):
        return (7, 30)
    s = tf.lower().strip()

    # Handle ranges first: "2-4 weeks", "1-2 weeks", "10-30 days"
    import re
    m = re.search(r'(\d+)\s*[-–to]+\s*(\d+)\s*(day|week|month)', s)
    if m:
        a, b, unit = int(m.group(1)), int(m.group(2)), m.group(3)
        mult = 1 if unit == 'day' else 7 if unit == 'week' else 30
        return (a * mult, b * mult)

    # Single value: "2 weeks", "30 days", "1 month"
    m = re.search(r'(\d+)\s*(day|week|month)', s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        mult = 1 if unit == 'day' else 7 if unit == 'week' else 30
        d = n * mult
        # Treat as "around N units" — give a +/- 50% range for display
        return (max(1, d // 2), int(d * 1.5))

    return (7, 30)


def _format_timeframe_short(days_min: int, days_max: int) -> str:
    """Render a (min, max) day range as a short human-readable string.

    Examples:
        (7, 14)  -> "1-2 weeks"
        (14, 28) -> "2-4 weeks"
        (1, 7)   -> "1-7 days"
        (30, 60) -> "1-2 months"
    """
    # If both fit in days, use days
    if days_max <= 14:
        return f"{days_min}-{days_max} days"
    # If both are roughly weeks
    if days_max <= 60:
        wmin = max(1, days_min // 7)
        wmax = max(wmin, days_max // 7)
        if wmin == wmax:
            return f"~{wmin} week{'s' if wmin != 1 else ''}"
        return f"{wmin}-{wmax} weeks"
    # Months
    mmin = max(1, days_min // 30)
    mmax = max(mmin, days_max // 30)
    if mmin == mmax:
        return f"~{mmin} month{'s' if mmin != 1 else ''}"
    return f"{mmin}-{mmax} months"


def _conviction_score(confidence: str | None) -> float:
    """Map confidence label to a numeric weight for sorting/scoring.
    Used to decide which BUY to pick when we have multiple."""
    if not confidence:
        return 0.5
    c = str(confidence).lower()
    if 'high' in c:
        return 1.0
    if 'mod' in c:
        return 0.6
    if 'low' in c:
        return 0.3
    return 0.5


def _normalize_prediction(p: dict) -> dict | None:
    """Validate + extract a prediction into a clean dict ready for the
    recommendation engine. Returns None if the prediction is missing
    required fields (no entry/target/stop = can't size a position).

    v4.9.5: Recommendations was returning empty because the prediction
    format uses buy_zone_low/buy_zone_high (a range) and
    current_price_at_prediction, NOT a single 'entry' field. The
    original code only looked for 'entry' or 'current_price', so every
    real prediction failed the entry > 0 check. Now we try, in order:
      1. midpoint of buy_zone_low/buy_zone_high (what the AI suggested)
      2. current_price_at_prediction (price when AI decided BUY)
      3. legacy 'entry' or 'current_price' fields
    """
    direction = str(p.get('direction', '')).upper()
    if direction != 'BUY':
        return None  # Only BUYs make it into recommendations

    ticker = str(p.get('ticker', '')).upper()
    if not ticker:
        return None

    # v4.9.5: try multiple entry-price sources in order of preference
    # v4.14.5.85-recommend-reprice (Part A): if the AI returned the
    # buy_zone with `low > high` (labels swapped — a real failure mode
    # we see on ~1.6% of BUY predictions in live data, e.g. BTSG's
    # 2026-05-25 record with low=54.08, high=48.13), swap them BEFORE
    # computing the midpoint so the displayed entry sits between the
    # endpoints in the AI's intended ordering rather than below both.
    # Always-on (data-correctness fix, not gated behind the v.85
    # reprice flag).
    entry = 0.0
    bz_low = _safe_float(p.get('buy_zone_low'))
    bz_high = _safe_float(p.get('buy_zone_high'))
    if bz_low > 0 and bz_high > 0:
        if bz_low > bz_high:
            bz_low, bz_high = bz_high, bz_low   # swap to corrected ordering
        entry = (bz_low + bz_high) / 2.0
    elif bz_low > 0:
        entry = bz_low  # only low side present
    elif bz_high > 0:
        entry = bz_high
    if entry <= 0:
        entry = _safe_float(p.get('current_price_at_prediction'))
    if entry <= 0:
        # Legacy fallbacks for older format predictions
        entry = _safe_float(p.get('entry') or p.get('current_price'))

    target = _safe_float(p.get('target'))
    stop = _safe_float(p.get('stop'))

    if entry <= 0 or target <= 0 or stop <= 0:
        return None
    # Sanity: target should be above entry, stop below entry for BUY
    if target <= entry or stop >= entry:
        return None

    upside_dollars = target - entry
    downside_dollars = entry - stop
    if downside_dollars <= 0:
        return None
    reward_to_risk = upside_dollars / downside_dollars

    days_min, days_max = _parse_timeframe_days(
        p.get('timeframe') or p.get('horizon')
        or p.get('timeframe_days'))  # v4.9.5: also try 'timeframe_days' (int)

    confidence = p.get('confidence', 'MODERATE')
    conv = _conviction_score(confidence)

    # Composite score for ranking — higher reward/risk is better, higher
    # conviction is better. Multiplicative because they compound.
    score = reward_to_risk * conv

    return {
        'ticker': ticker,
        'entry': entry,
        'target': target,
        'stop': stop,
        'upside_pct': (target - entry) / entry * 100,
        'downside_pct': (entry - stop) / entry * 100,
        'reward_to_risk': reward_to_risk,
        'confidence': confidence,
        'conviction_score': conv,
        'days_min': days_min,
        'days_max': days_max,
        'timeframe_str': _format_timeframe_short(days_min, days_max),
        'score': score,
        'timestamp': p.get('timestamp', ''),
        'path': p.get('path', ''),
        # v4.14.5.72-spec-news-badge: carry the catalyst flag through
        # normalization so the basket renderer can surface a "News"
        # tag on speculative picks. Boolean derived from news_bonus
        # (set by _read_recommend_cache_picks from recommend_cache).
        # Defaults to False so legacy callers / non-cache predictions
        # are unchanged.
        'has_news': bool(p.get('has_news')),
        'news_bonus': float(p.get('news_bonus') or 0.0),
        # v4.14.5.85-recommend-reprice (Part A+E): carry the corrected
        # buy_zone endpoints so the renderer can flag picks that have
        # run PAST `buy_zone_high` (i.e. above where the AI said to
        # consider entering — the "lost its charm" signal). Already
        # ordering-swapped above when the AI returned them inverted.
        # Defaults to None/0 so legacy callers see no change.
        'buy_zone_low': bz_low if bz_low > 0 else None,
        'buy_zone_high': bz_high if bz_high > 0 else None,
    }


def filter_eligible_predictions(predictions: list[dict],
                                  max_age_days: int = 14,
                                  now: datetime | None = None,
                                  path: str | None = None,
                                  excluded_tickers: set | None = None,
                                  consensus_check=None,
                                  min_model_agreement: int = 1,
                                  post_filter=None,
                                  prefilter_count_out=None) -> list[dict]:
    """Filter a raw predictions log down to current, eligible BUYs.

    Eligibility rules:
    - Must be direction=BUY
    - Must have valid entry/target/stop with sane relationships
    - Must be less than max_age_days old (default 14 days —
      v4.14.5.14-displayed-picks-recovery bumped from 7 so Layer 1
      BUYs aren't dropped before Layer 2 finishes validating them.
      Layer 2 daemon runs ~1 pick / 180s, so full cache validation
      can take 1-2 hours; 7 days was tight when picks date back
      ~13 days but Layer 2 verdict on them is still fresh enough)
    - Must still be 'open' (not target_hit, not stop_hit, not expired)
    - v4.13.7: if `path` is given, only predictions whose stored
      `path` field matches are kept. Predictions saved before the
      path field existed (no `path` key, or empty string) are
      excluded from path-filtered results so old data doesn't
      leak across paths. Pass path=None to disable filtering.
    - v4.13.29: if `min_model_agreement` > 1, predictions whose
      ticker has fewer than that many distinct model BUY votes
      within the eligibility window are dropped. This prevents
      single-model picks from ranking as top recommendations
      when no genuine multi-model consensus exists. Pre-v4.13.29
      behavior was equivalent to min_model_agreement=1.

    v4.10.3: Each returned prediction also carries a 'model_agreement'
    field — the count of UNIQUE models that produced a BUY for the same
    ticker within the eligibility window. This is used as a tiebreaker
    when sorting by conviction tier in the Recommend view.

    Returns a list of normalized prediction dicts, sorted by:
      1. Conviction tier (HIGH > MODERATE > LOW)
      2. Model agreement count (more models agreeing = stronger signal)
      3. Score (reward/risk × conviction)
    """
    if now is None:
        now = datetime.now()
    cutoff = now - timedelta(days=max_age_days)

    # v4.13.39: if a specific path was requested and that path is
    # disabled in path_config.json, return empty immediately. This
    # is the explicit "the user picked lottery in the dropdown but
    # lottery is paused" case.
    if path and not is_path_enabled(path):
        return []

    # v4.13.39: pre-compute the set of disabled paths so we can drop
    # individual predictions whose stored path is disabled (the
    # path=None / cross-path case where we're aggregating).
    _path_summary = get_path_config_summary()
    _disabled_paths = {n.lower() for n, info in _path_summary.items()
                       if not info.get('enabled', True)}

    # First pass — normalize each prediction and pre-filter by status/age.
    # Don't dedupe yet because we want to count unique models per ticker.
    raw_eligible = []
    for p in predictions:
        # v4.13.39: drop predictions from disabled paths regardless
        # of whether a specific path filter was requested. Catches
        # the case where path=None is passed but the predictions
        # themselves come from a paused path like lottery.
        if _disabled_paths:
            pred_path_lower = str(p.get('path') or '').strip().lower()
            if pred_path_lower in _disabled_paths:
                continue

        # v4.13.7: Path filter (when caller specifies a path)
        if path:
            pred_path = str(p.get('path') or '').strip().lower()
            if pred_path != path.strip().lower():
                continue

        # v4.13.28: Just-sold cooldown filter
        if excluded_tickers:
            tk = str(p.get('ticker') or '').strip().upper()
            if tk in excluded_tickers:
                continue

        # v4.13.28: Contradicted-by-consensus filter. If a more
        # recent consensus run went non-BUY for this ticker, drop
        # the stale BUY prediction.
        if consensus_check is not None:
            try:
                tk = str(p.get('ticker') or '').strip().upper()
                if tk and not consensus_check(tk):
                    continue
            except Exception:
                pass  # never let a bad check kill the filter

        # Status filter
        status = str(p.get('status', 'open')).lower()
        if status not in ('open', '', 'pending'):
            continue

        # Age filter
        ts_str = p.get('timestamp', '')
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts < cutoff:
                    continue
            except (ValueError, TypeError):
                # Bad timestamp — keep it (better than dropping recent
                # predictions due to format issues)
                pass

        norm = _normalize_prediction(p)
        if norm is not None:
            # Stash the model name from the original prediction so we
            # can count unique models per ticker.
            norm['_model'] = str(p.get('model', '') or '').strip()
            raw_eligible.append(norm)

    # v4.10.3: count unique models per ticker BEFORE deduplication.
    # Each ticker's "model agreement" is how many distinct models
    # generated a BUY for it within the window.
    ticker_models: dict[str, set] = {}
    for p in raw_eligible:
        m = p.get('_model', '')
        if not m:
            # Older predictions without a model field — count as a
            # generic vote so they still register.
            m = '__unknown__'
        ticker_models.setdefault(p['ticker'], set()).add(m)
    ticker_agreement = {t: len(ms) for t, ms in ticker_models.items()}

    # Sort by score (reward/risk × conviction) descending — primary order
    # before dedup so dedup keeps the highest-scoring prediction per
    # ticker.
    raw_eligible.sort(key=lambda x: x['score'], reverse=True)

    # Deduplicate: if same ticker has multiple predictions, keep the
    # one with the highest score.
    seen_tickers = set()
    deduped = []
    for p in raw_eligible:
        if p['ticker'] in seen_tickers:
            continue
        seen_tickers.add(p['ticker'])
        # Attach the model agreement count to the kept prediction
        p['model_agreement'] = ticker_agreement.get(p['ticker'], 1)
        # Drop the internal _model field from the public shape
        p.pop('_model', None)
        deduped.append(p)

    # v4.13.29: Drop predictions whose ticker has below-threshold model
    # agreement. This prevents the recommendation engine from surfacing
    # single-model picks as top recommendations when no genuine
    # multi-model consensus backs them. The default of 1 preserves
    # legacy behavior; raising it via config makes the engine refuse
    # to display picks that wouldn't survive verify.
    if min_model_agreement > 1:
        deduped = [p for p in deduped
                   if p.get('model_agreement', 1) >= min_model_agreement]

    # v4.14.5.14a.8: HARD post-filter applied to fully-normalized picks
    # (entry/conviction/model_agreement all present here). The
    # Portfolio Recommendations dialog passes a callable enforcing the
    # price tier / min-max, conviction, and the fresh-consensus quality
    # gate — so the deployment columns are built ONLY from surviving
    # picks (single source of truth: columns == the survive counter).
    # `prefilter_count_out` (a 1-element list) receives the pre-filter
    # count so the UI can honestly show "X of Y survive". None = legacy
    # behaviour (no hard post-filter — picks shown soft-reranked).
    if prefilter_count_out is not None:
        try:
            prefilter_count_out[0] = len(deduped)
        except Exception:
            pass
    if post_filter is not None:
        kept = []
        for p in deduped:
            try:
                if post_filter(p):
                    kept.append(p)
            except Exception:
                kept.append(p)  # never let a bad gate empty the list
        deduped = kept

    # v4.10.3: Final sort uses conviction tier as primary key with
    # model agreement as tiebreaker, then score for further breaking.
    # This is what the user actually wants visible in Recommend:
    # HIGH conviction always above MODERATE, more models agreeing wins
    # within the same tier, and score breaks remaining ties.
    def _sort_key(p):
        return (
            -p['conviction_score'],     # higher conviction first (negate for desc)
            -p['model_agreement'],       # more models agreeing first
            -p['score'],                 # higher score first
        )
    deduped.sort(key=_sort_key)

    return deduped


def _make_position(pred: dict, shares: int) -> dict:
    """Build a position dict from a normalized prediction + share count."""
    cost = shares * pred['entry']
    if_target = shares * pred['target']
    if_stop = shares * pred['stop']
    return {
        'ticker': pred['ticker'],
        'shares': shares,
        'entry': pred['entry'],
        'target': pred['target'],
        'stop': pred['stop'],
        'cost': cost,
        'best_case_value': if_target,
        'worst_case_value': if_stop,
        'best_case_gain': if_target - cost,
        'worst_case_loss': cost - if_stop,
        'upside_pct': pred['upside_pct'],
        'downside_pct': pred['downside_pct'],
        'reward_to_risk': pred['reward_to_risk'],
        'confidence': pred['confidence'],
        'timeframe_str': pred['timeframe_str'],
        'days_min': pred['days_min'],
        'days_max': pred['days_max'],
        # v4.10.3: carry model agreement into the position so the UI
        # can show "5 models BUY" next to the ticker.
        'model_agreement': pred.get('model_agreement', 1),
        # v4.14.5.72-spec-news-badge: pass the catalyst flag + path
        # through to the basket renderer. The recommend panel gates
        # the badge to speculative paths (_SPECULATIVE_PATHS in
        # tm_source_accuracy) so news on slow_safe/moderate/aggressive
        # — where news is constant noise — is suppressed; only on
        # lottery/penny_lottery — where news is rare and therefore
        # meaningful — does the badge render.
        'has_news': bool(pred.get('has_news')),
        'path': pred.get('path', ''),
        # v4.14.5.85-recommend-reprice (Part E): carry the corrected
        # buy_zone endpoints into the position so the renderer's
        # past-entry-zone flag can compare current price vs
        # buy_zone_high. Display-only; doesn't affect basket math.
        'buy_zone_low': pred.get('buy_zone_low'),
        'buy_zone_high': pred.get('buy_zone_high'),
    }


def build_basket(positions: list[dict], cash_in: float) -> dict:
    """Aggregate a list of positions into a basket summary.

    Returns dict with totals: cost, cash_left, best_case_total,
    worst_case_total, best_case_pct (vs cash_in), avg_reward_to_risk,
    timeframe_range, position_count.

    v4.13.10: hard budget guard. If incoming positions exceed
    cash_in (which would mean an upstream bug), drop positions
    from the END until the basket fits. The displayed numbers
    must never lie about the user's budget — better to show a
    smaller basket than a basket that 'invests' money the user
    doesn't have.
    """
    if positions and cash_in > 0:
        positions = list(positions)  # don't mutate caller's list
        while positions:
            running_total = sum(p['cost'] for p in positions)
            if running_total <= cash_in + 0.01:  # 1c tolerance
                break
            # Drop the LAST position (lowest priority) and retry
            positions.pop()
    total_cost = sum(p['cost'] for p in positions)
    best = sum(p['best_case_value'] for p in positions)
    worst = sum(p['worst_case_value'] for p in positions)
    cash_left = cash_in - total_cost
    # Add cash_left to best/worst (cash itself doesn't move)
    best_total = best + cash_left
    worst_total = worst + cash_left

    if positions:
        avg_rr = sum(p['reward_to_risk'] for p in positions) / len(positions)
    else:
        avg_rr = 0.0

    if positions:
        d_min = min(p['days_min'] for p in positions)
        d_max = max(p['days_max'] for p in positions)
        tf_str = _format_timeframe_short(d_min, d_max)
    else:
        tf_str = ""

    return {
        'positions': positions,
        'position_count': len(positions),
        'cash_in': cash_in,
        'total_cost': total_cost,
        'cash_left': cash_left,
        'best_case_total': best_total,
        'worst_case_total': worst_total,
        'best_case_gain': best_total - cash_in,  # Pure gain over starting cash
        'worst_case_loss': cash_in - worst_total,  # Pure loss
        'best_case_pct': ((best_total - cash_in) / cash_in * 100
                           if cash_in > 0 else 0),
        'worst_case_pct': ((worst_total - cash_in) / cash_in * 100
                            if cash_in > 0 else 0),
        'avg_reward_to_risk': avg_rr,
        'timeframe_str': tf_str,
    }


def _max_shares_for_budget(price: float, budget: float) -> int:
    """How many whole shares of `price` fit in `budget`. Floors to 0."""
    if price <= 0 or budget <= 0:
        return 0
    return int(budget / price)


# ─── v4.14.5.81-budget-fit-transparency: pure explanation helper ─────
#
# When a basket column (ALL IN / A FEW / DIVERSIFY) would render empty
# OR near-empty because picks exist but don't fit `cash`, the renderer
# currently shows "(no positions fit this budget)" — opaque to a user
# who can't tell whether picks even exist or how close they are. This
# helper distinguishes the two empty states and produces a structured
# dict the renderer turns into labels.
#
# Strict scope: this is PURE LOGIC over the already-computed
# eligible list + cash. No new price source, no new pick selection,
# no change to dollar-math. Lives in tm_recommend.py for clean
# unit-testability.

def compute_budget_fit_explanation(eligible: list,
                                   cash,
                                   basket_kind: str) -> dict:
    """Return a structured explanation for an empty-column case.

    Args:
        eligible: the full filter-eligible BUY-pick list (same shape
            `build_recommendations` already produces). Each entry must
            have 'ticker' and 'entry' (entry price as float).
        cash: user's deploy budget (float, may be None for "Any").
        basket_kind: 'all_in' / 'a_few' / 'diversify' — used to
            describe the minimum-fit requirement in plain words.

    Returns a dict:
        {'state': 'no_picks' | 'budget_mismatch' | 'has_fits' | 'any_mode',
         'pick_count': int,
         'cheapest_ticker': str | None,
         'cheapest_price': float | None,
         'fits_one_share': list[(ticker, price)],  # picks where 1 share fits
         'fits_basket_min': bool,                  # ≥basket_kind's min fits
         'cash': float | None}

    State semantics:
        'no_picks'        — eligible is empty; render honest "no BUY picks".
        'budget_mismatch' — picks exist but NONE fit even 1 share; render
                            the budget explanation with cheapest price.
        'has_fits'        — picks exist, some fit 1 share but basket's
                            normal sizing doesn't; render those single
                            picks plus an explanation.
        'any_mode'        — cash is None ("Any" budget); not an empty-
                            state — caller renders the normal
                            unit-alternatives view.

    Pure: never raises, defensive on missing 'entry'.
    """
    eligible = list(eligible or [])
    if cash is None:
        return {
            'state': 'any_mode', 'pick_count': len(eligible),
            'cheapest_ticker': None, 'cheapest_price': None,
            'fits_one_share': [], 'fits_basket_min': False, 'cash': None,
        }
    try:
        cash_f = float(cash)
    except (TypeError, ValueError):
        cash_f = 0.0

    if not eligible:
        return {
            'state': 'no_picks', 'pick_count': 0,
            'cheapest_ticker': None, 'cheapest_price': None,
            'fits_one_share': [], 'fits_basket_min': False,
            'cash': cash_f,
        }

    # Per-pick fit table.
    fits: list = []
    cheapest = None
    cheapest_ticker = None
    for p in eligible:
        try:
            entry = float(p.get('entry') or 0.0)
        except (TypeError, ValueError):
            continue
        if entry <= 0:
            continue
        tk = str(p.get('ticker') or '').strip().upper()
        if not tk:
            continue
        if cheapest is None or entry < cheapest:
            cheapest = entry
            cheapest_ticker = tk
        if entry <= cash_f:
            fits.append((tk, entry))

    # Basket-min: ALL IN needs ≥1 share of the cheapest pick.
    # A FEW needs ≥1 share each in 2+ DISTINCT picks within cash.
    # DIVERSIFY needs ≥1 share each in 4+ distinct picks within cash.
    # The cash check here is "would the basket builder even START to fit
    # something" — exact builder logic distributes per-position cash, so
    # this is a permissive lower bound; if it returns False the builder
    # would definitely return empty (matches today's "(no positions
    # fit)" state).
    basket_kind = (basket_kind or '').strip().lower()
    if basket_kind == 'all_in':
        fits_basket_min = bool(fits)
    elif basket_kind == 'a_few':
        # A FEW splits cash across 2-3 positions; require enough cheap
        # picks that 2 of them each get a share at cash/3 per position.
        per = cash_f / 3.0
        n_per = sum(1 for _t, e in fits if e <= per)
        fits_basket_min = n_per >= 2
    elif basket_kind == 'diversify':
        per = cash_f / 5.0
        n_per = sum(1 for _t, e in fits if e <= per)
        fits_basket_min = n_per >= 4
    else:
        fits_basket_min = bool(fits)

    if fits and not fits_basket_min:
        state = 'has_fits'
    elif fits and fits_basket_min:
        # This shouldn't normally hit the empty-column branch — caller
        # only invokes us when alternatives is empty. Defensive: treat
        # as has_fits (renderer just lists what fits and lets normal
        # baskets render whatever they built elsewhere).
        state = 'has_fits'
    else:
        state = 'budget_mismatch'

    return {
        'state': state, 'pick_count': len(eligible),
        'cheapest_ticker': cheapest_ticker,
        'cheapest_price': cheapest,
        'fits_one_share': fits[:6],  # cap the list shown; first 6 by score order
        'fits_basket_min': fits_basket_min,
        'cash': cash_f,
    }


def build_all_in(predictions: list[dict], cash: float) -> dict:
    """ALL IN: single position, max shares of the highest-score BUY.

    Returns a basket dict (see build_basket). Empty positions list if
    no eligible predictions or cash doesn't fit a single share.
    """
    if not predictions or cash <= 0:
        return build_basket([], cash)

    # Highest score — already sorted, just take the first
    top = predictions[0]
    shares = _max_shares_for_budget(top['entry'], cash)
    if shares == 0:
        # Can't afford even 1 share. Try the second-highest, third, etc.
        # Find the cheapest stock with the best score-per-dollar.
        for p in predictions:
            shares = _max_shares_for_budget(p['entry'], cash)
            if shares > 0:
                top = p
                break
        else:
            return build_basket([], cash)

    return build_basket([_make_position(top, shares)], cash)


def build_a_few(predictions: list[dict], cash: float) -> dict:
    """A FEW: 2-3 positions, weighted toward higher conviction.

    Strategy: pick top 2-3 by score that fit in budget. Allocate roughly
    proportional to score (so highest-conviction gets more shares).
    Round to whole shares; rebalance leftover cash down to next pick.

    Returns basket dict.
    """
    if not predictions or cash <= 0:
        return build_basket([], cash)

    # Take top candidates that we can afford at least 1 share of
    affordable = [p for p in predictions
                  if _max_shares_for_budget(p['entry'], cash) > 0]
    if not affordable:
        return build_basket([], cash)

    # Try 3 picks first; fall back to 2 if 3 doesn't fit
    for target_count in (3, 2, 1):
        picks = affordable[:target_count]
        if not picks:
            continue
        # Weight allocation by score
        total_score = sum(p['score'] for p in picks)
        if total_score <= 0:
            # Equal allocation
            weights = [1.0 / len(picks)] * len(picks)
        else:
            weights = [p['score'] / total_score for p in picks]

        # First pass: target dollars per pick.
        # v4.13.10: stash _pred_ref on each placed position so we
        # know which prediction it came from. Fixes a latent bug
        # where second-pass logic mixed picks[0] (top by score)
        # with positions[0] (first actually placed) when the
        # top-scoring pick couldn't afford a share.
        positions = []
        position_preds = []  # parallel list — same index as positions
        remaining = cash
        for pred, w in zip(picks, weights):
            target_dollars = cash * w
            shares = _max_shares_for_budget(pred['entry'],
                                              min(target_dollars, remaining))
            if shares > 0:
                pos = _make_position(pred, shares)
                positions.append(pos)
                position_preds.append(pred)
                remaining -= pos['cost']

        # Second pass: try to spend leftover cash on the FIRST
        # placed position. v4.13.10: was using picks[0] which
        # could be a different prediction than positions[0] when
        # picks[0] was too expensive to fit even one share.
        # Now uses position_preds[0] (the actual prediction the
        # first position was built from).
        if positions and remaining >= positions[0]['entry']:
            extra = _max_shares_for_budget(positions[0]['entry'],
                                            remaining)
            if extra > 0:
                top_pred = position_preds[0]
                new_shares = positions[0]['shares'] + extra
                new_pos = _make_position(top_pred, new_shares)
                # Hard guard: don't replace if it would put the
                # basket over budget. Should never happen with
                # the corrected math, but defense-in-depth.
                _other_cost = sum(p['cost']
                                   for p in positions[1:])
                if new_pos['cost'] + _other_cost <= cash + 0.01:
                    positions[0] = new_pos
                    remaining = cash - new_pos['cost'] - _other_cost

        if positions:
            return build_basket(positions, cash)

    return build_basket([], cash)


def build_diversify(predictions: list[dict], cash: float) -> dict:
    """DIVERSIFY: 4-6 positions, smaller stakes, more spread.

    Strategy: try to fit 6 positions, fall back to 5, 4, 3. Use a
    cheapest-first approach (so we maximize the number of positions
    that fit in budget). Allocate roughly equal dollars per position.

    Returns basket dict.
    """
    if not predictions or cash <= 0:
        return build_basket([], cash)

    # Sort by entry price ascending — cheapest first, so we can fit more
    by_price = sorted(predictions, key=lambda p: p['entry'])

    for target_count in (6, 5, 4, 3):
        if len(by_price) < target_count:
            continue
        per_pos = cash / target_count
        # Take the `target_count` cheapest where each fits in per_pos
        picks = []
        for p in by_price:
            if p['entry'] <= per_pos and _max_shares_for_budget(p['entry'], per_pos) > 0:
                picks.append(p)
                if len(picks) == target_count:
                    break

        if len(picks) >= target_count:
            # Allocate equal shares per position (roughly equal dollars)
            positions = []
            remaining = cash
            for pred in picks:
                shares = _max_shares_for_budget(pred['entry'], per_pos)
                if shares > 0:
                    pos = _make_position(pred, shares)
                    positions.append(pos)
                    remaining -= pos['cost']
            if len(positions) >= target_count:
                return build_basket(positions, cash)

    # If we couldn't get 3+, fall through to a "best 3 we can afford"
    # using the same algorithm as build_a_few but always 3 positions
    affordable = [p for p in by_price
                  if _max_shares_for_budget(p['entry'], cash / 3) > 0]
    if len(affordable) >= 3:
        per_pos = cash / 3
        positions = []
        for pred in affordable[:3]:
            shares = _max_shares_for_budget(pred['entry'], per_pos)
            if shares > 0:
                positions.append(_make_position(pred, shares))
        if positions:
            return build_basket(positions, cash)

    return build_basket([], cash)


def build_recommendations(predictions_raw: list[dict],
                            cash: float,
                            max_age_days: int = 14,
                            now: datetime | None = None,
                            max_alternatives: int = 3,
                            path: str | None = None,
                            excluded_tickers: set | None = None,
                            consensus_check=None,
                            min_model_agreement: int = 1,
                            post_filter=None) -> dict:
    """Top-level: takes a raw predictions list + cash amount, returns
    all three basket recommendations plus context.

    v4.10.3: Each tier now returns up to `max_alternatives` distinct
    baskets ranked by conviction tier > model agreement > score. Plus
    a 'top_pick' field showing the single strongest call regardless of
    budget — useful for tracking what the AI is most confident about
    even if you can't afford it right now.

    Returns dict with keys:
    - eligible_count: int — how many BUYs passed the filter
    - eligible: list — full normalized prediction list
    - top_pick: dict | None — single strongest BUY ignoring budget
    - all_in: list of basket dicts (was a single basket pre-v4.10.3)
    - a_few: list of basket dicts
    - diversify: list of basket dicts
    - notes: list of human-readable observations
    """
    # v4.13.7: forward the path filter so basket math respects
    # the Recommend window's path dropdown (Aggressive vs Lottery
    # vs Penny Lottery, etc).
    # v4.14.5.14a.8: thread the hard post-filter + capture the
    # pre-filter count so callers can show an honest "X of Y survive".
    _prefilter = [0]
    eligible = filter_eligible_predictions(
        predictions_raw, max_age_days, now, path=path,
        excluded_tickers=excluded_tickers,
        consensus_check=consensus_check,
        min_model_agreement=min_model_agreement,
        post_filter=post_filter,
        prefilter_count_out=_prefilter)

    # v4.10.3: top pick — ignores budget. The eligibility list is sorted
    # by (conviction_tier, model_agreement, score) so the first entry is
    # already the strongest call.
    top_pick = eligible[0] if eligible else None

    # v4.14.5.54-recs-gate-budgetaware: cash is None == "Any" budget (a brand-new
    # user who hasn't chosen one). There's no cash to size against, so show the
    # picks at one share each (the 1-share view) instead of empty cash-sized
    # columns. Everything else (eligibility, top_pick) is budget-independent.
    _any_mode = cash is None

    # v4.10.3: build multiple alternatives per tier. Each alternative
    # uses a different starting subset of the eligible list, giving the
    # user N distinct options rather than one take-it-or-leave-it pick.
    if _any_mode:
        all_in_alts = _build_unit_alternatives(eligible, 1, max_alternatives)
        a_few_alts = _build_unit_alternatives(
            eligible, A_FEW_TARGET_POSITIONS, max_alternatives)
        diversify_alts = _build_unit_alternatives(
            eligible, DIVERSIFY_TARGET_POSITIONS, max_alternatives)
    else:
        all_in_alts = build_all_in_alternatives(eligible, cash, max_alternatives)
        a_few_alts = build_a_few_alternatives(eligible, cash, max_alternatives)
        diversify_alts = build_diversify_alternatives(eligible, cash,
                                                        max_alternatives)

    # Build context notes for the user
    notes = []
    # v4.13.39: explicit note when the requested path is paused
    if path and not is_path_enabled(path):
        cfg = get_path_config_summary().get(
            str(path).strip().lower(), {})
        reason = cfg.get('reason', '')
        msg = (f"Path '{path}' is currently paused in path_config.json")
        if reason:
            msg += f" — {reason}"
        msg += (". Edit data/path_config.json to re-enable, "
                "or pick a different path from the dropdown.")
        notes.append(msg)

    if not eligible:
        if path:
            notes.append(
                f"No active BUY recommendations on path '{path}'. "
                f"Either no Discover scans have run yet for this "
                f"path, or all candidates expired/closed. Try "
                f"another path, or run a Discover scan with this "
                f"path active.")
        else:
            notes.append(
                "No active BUY recommendations. Run a Discover scan to "
                "generate some, then come back here.")
    elif len(eligible) == 1:
        notes.append(
            f"Only 1 active BUY ({eligible[0]['ticker']}). "
            "All three options will look similar.")

    if not _any_mode and eligible and cash > 0:
        # Check for budget-limited diversification
        cheapest = min(p['entry'] for p in eligible)
        max_positions_at_budget = int(cash / cheapest) if cheapest > 0 else 0
        if cash < cheapest:
            notes.append(
                f"Budget ${cash:.0f} is below the cheapest BUY "
                f"(${cheapest:.2f}). Consider raising your budget or "
                f"waiting for a different scan to surface cheaper picks.")
        elif max_positions_at_budget < 3:
            cheapest_ticker = next(p['ticker'] for p in eligible
                                     if p['entry'] == cheapest)
            notes.append(
                f"At ${cash:.0f} budget, you can only fit "
                f"{max_positions_at_budget} positions even of the cheapest "
                f"name ({cheapest_ticker} @ ${cheapest:.2f}). True "
                f"diversification is hard at this budget; ALL IN or A FEW "
                f"may be more practical than DIVERSIFY.")

    if not _any_mode and top_pick is not None and eligible:
        top_cost = top_pick['entry']
        if top_cost > cash:
            notes.append(
                f"Top pick {top_pick['ticker']} (${top_cost:.2f}) is above "
                f"your ${cash:.0f} budget — shown for reference, not "
                f"included in the columns below.")

    # v4.13.8: surface when distinct-basket targets aren't met.
    # Alts are now non-overlapping, so 'fewer alternatives than
    # expected' is meaningful — tells the user the path doesn't
    # have enough material for full diversification.
    if not _any_mode and eligible and cash > 0:
        n = len(eligible)
        diversify_target = 3 * 5  # 3 alts x 5 picks each
        a_few_target = 3 * 3      # 3 alts x 3 picks each
        if n < a_few_target:
            notes.append(
                f"Only {n} eligible BUY{'s' if n != 1 else ''} on "
                f"this path. For 3 fully-distinct alternatives, "
                f"A FEW needs {a_few_target}+ and DIVERSIFY needs "
                f"{diversify_target}+. You may see 1 or 2 "
                f"alternatives instead of 3 — that's the data "
                f"talking, not a bug.")
        elif n < diversify_target:
            notes.append(
                f"{n} eligible BUYs — enough for 3 distinct A FEW "
                f"baskets, but DIVERSIFY needs {diversify_target}+ "
                f"for 3 distinct 5-pick baskets. DIVERSIFY may "
                f"show fewer alternatives.")

    # Honest framing — always shown
    notes.append(
        "AI predictions, not promises. Recommendation math assumes "
        "perfect target hits, which doesn't happen in real markets. "
        "Track Record will tell us if this AI's calls are worth "
        "following — too early to know yet.")

    return {
        'eligible_count': len(eligible),
        # v4.14.5.14a.8: BUYs that existed pre-hard-filter (the "Y" in
        # "X of Y survive"). Equals eligible_count when no post_filter.
        'prefilter_eligible_count': _prefilter[0] if _prefilter[0]
        else len(eligible),
        'eligible': eligible,
        'cash': cash,
        'path': path,  # v4.13.7: echo back the path filter (if any)
        'top_pick': top_pick,
        # v4.10.3: backward-compat — old UI expected single dicts at
        # 'all_in' / 'a_few' / 'diversify'. Keep those keys pointing at
        # the FIRST alternative for compat, and provide the full list at
        # '*_alternatives'.
        # v4.14.5.54: in "Any" mode cash is None — build_basket does
        # arithmetic on cash_in, so fall back to 0 (an empty basket) rather
        # than passing None through.
        'all_in': all_in_alts[0] if all_in_alts else build_basket([], cash or 0),
        'a_few': a_few_alts[0] if a_few_alts else build_basket([], cash or 0),
        'diversify': (diversify_alts[0] if diversify_alts
                       else build_basket([], cash or 0)),
        'all_in_alternatives': all_in_alts,
        'a_few_alternatives': a_few_alts,
        'diversify_alternatives': diversify_alts,
        'notes': notes,
    }


# ════════════════════════════════════════════════════════════════════════
# v4.10.3 — MULTI-ALTERNATIVE BUILDERS
# ════════════════════════════════════════════════════════════════════════
# Each "alternative" is a distinct basket built from a different starting
# subset of the eligible list. The simplest correct approach: skip the
# first N predictions and rebuild from the rest. That gives you a
# different lead pick each time, which differentiates the alternatives
# meaningfully.
#
# Gotcha: when there are very few eligible predictions, alternatives
# become repetitive or empty. We handle this by deduplicating identical
# baskets and returning fewer than max_alternatives if that's what the
# data supports.

def _basket_signature(basket: dict) -> tuple:
    """A hashable signature for a basket so we can dedupe identical
    alternatives. Two baskets are 'the same' if they hold the same
    tickers in the same share counts."""
    return tuple(sorted(
        (pos['ticker'], pos['shares'])
        for pos in basket.get('positions', [])
    ))


def _label_alternative(basket: dict, rank: int = 0) -> str:
    """v4.10.3: Each alternative gets a short, meaningful label so the
    user knows WHY it's there (not just "alternative #2"). Labels reflect
    the lead position's standout characteristic plus rank ordering so
    similar leads still differentiate.

    rank: 0 for the top alternative, 1 for second, 2 for third, etc.
    Used to pick a label that emphasizes what makes THIS alternative
    distinctive rather than repeating the same praise for each one.
    """
    if not basket.get('positions'):
        return "(empty)"
    lead = basket['positions'][0]
    conv = (lead.get('confidence', '') or '').upper() or 'MOD'
    agreement = lead.get('model_agreement', 1)
    rr = lead.get('reward_to_risk', 0)

    # The TOP alternative gets the strongest praise it earns.
    # Lower-ranked alternatives describe what they offer instead.
    # v4.13.10: 'N models' was misleading — it counts unique
    # models across ALL prior scans in the eligibility window,
    # not models in a single consensus run. Renamed to
    # 'N model BUYs' to be honest about what's counted.
    if rank == 0:
        if conv == 'HIGH':
            return "Highest conviction"
        if agreement >= 5:
            return f"{agreement} model BUYs (historical)"
        if rr >= 3:
            return f"Best reward/risk ({rr:.1f}x)"
        return "Top option"
    elif rank == 1:
        # The second-best — pick a different angle than the first
        if agreement >= 3:
            return f"{agreement} model BUYs (historical)"
        if rr >= 2:
            return f"Solid reward/risk ({rr:.1f}x)"
        return "Alternative"
    else:
        # Third or beyond — describe by characteristic
        return f"Also: {lead['ticker']} ({rr:.1f}x R/R)"


# v4.13.8: per-tier "ideal" position counts. Used by the partition-based
# alternative builders below. Exposed as module constants so a future
# patch can wire them to Settings.
A_FEW_TARGET_POSITIONS = 3
DIVERSIFY_TARGET_POSITIONS = 5


def _basket_tickers(basket: dict) -> set[str]:
    """Set of tickers in a basket — used to detect overlap between alts."""
    return {pos['ticker'] for pos in basket.get('positions', [])
            if pos.get('ticker')}


def _build_unit_alternatives(predictions: list[dict], group_size: int,
                             max_alts: int = 3) -> list[dict]:
    """v4.14.5.54-recs-gate-budgetaware: the "Any" budget (no cash set) builder.

    A brand-new user who hasn't chosen a budget has no cash to size against,
    so instead of an empty cash-sized basket we show the picks THEMSELVES at
    one share each (Option A: the "1-share view"). Partitions the eligible
    list into successive groups of `group_size` distinct tickers — one share
    each — so ALL IN (group_size=1) leads with each pick in turn, A FEW groups
    of 3, DIVERSIFY groups of 5, mirroring the cash builders' distinct-lead
    alternatives. Each unit basket's cash_in is its OWN cost, so build_basket's
    math stays internally consistent (cash_left=0, pct vs deployed) and no
    None ever reaches the arithmetic. Reuses the tested _make_position /
    build_basket / _label_alternative primitives.
    """
    out: list[dict] = []
    if not predictions or group_size < 1:
        return out
    i = 0
    n = len(predictions)
    while i < n and len(out) < max_alts:
        group = predictions[i:i + group_size]
        if not group:
            break
        positions = [_make_position(p, 1) for p in group]
        cost = sum(p['cost'] for p in positions)
        basket = build_basket(positions, cost)
        basket['alt_label'] = _label_alternative(basket, rank=len(out))
        out.append(basket)
        i += group_size
    return out


def build_all_in_alternatives(predictions: list[dict], cash: float,
                                max_alts: int = 3) -> list[dict]:
    """v4.13.8: Build up to max_alts ALL IN baskets, each leading with
    a different ticker. Already distinct by design (single position
    each), so this is a simple walk down the eligibility list.
    """
    if not predictions or cash <= 0:
        return []

    used: set[str] = set()
    out: list[dict] = []
    for pred in predictions:
        if pred['ticker'] in used:
            continue
        # Build a basket from just this one prediction (and any others
        # below it) so the "can't afford the lead, fall back" logic in
        # build_all_in still works.
        sub = [p for p in predictions if p['ticker'] not in used]
        if not sub:
            break
        basket = build_all_in(sub, cash)
        if not basket.get('positions'):
            break
        for pos in basket['positions']:
            used.add(pos['ticker'])
        basket["alt_label"] = _label_alternative(basket, rank=len(out))
        out.append(basket)
        if len(out) >= max_alts:
            break
    return out


def _build_partitioned_alts(predictions: list[dict], cash: float,
                              target_positions: int, max_alts: int,
                              builder) -> list[dict]:
    """v4.13.8: Common partition-and-build helper for A FEW and DIVERSIFY.

    Greedy algorithm:
      1. Build alt #1 from the full eligibility list using `builder`.
         If alt #1 ends up with fewer than target_positions (because
         budget can't fit them), accept that — alt #1 is always as
         good as it can be.
      2. Remove alt #1's tickers from the pool.
      3. Try to build alt #2 from the remaining tickers. ONLY include
         alt #2 if it has a "real" basket: same position count as
         alt #1 (so all alts feel comparable), or at minimum 2 positions
         if alt #1 also fell short. If the remaining pool can't produce
         that, stop here.
      4. Repeat for alt #3.

    Returns a list of 0..max_alts baskets, each guaranteed disjoint from
    the others. Each basket has its alt_label set.
    """
    if not predictions or cash <= 0:
        return []

    out: list[dict] = []
    used_tickers: set[str] = set()

    for alt_idx in range(max_alts):
        # Pool = predictions minus everything already used
        pool = [p for p in predictions if p['ticker'] not in used_tickers]
        if not pool:
            break
        basket = builder(pool, cash)
        positions = basket.get('positions') or []
        if not positions:
            break

        # v4.13.8: For alt #2 and beyond, require the basket to be
        # "comparable" to alt #1 — same position count if possible, or
        # at least 2 positions if alt #1 itself was small. This avoids
        # showing a single-pick "alternative" next to a 5-pick one.
        if alt_idx > 0:
            first_count = out[0]['position_count']
            min_needed = min(first_count, max(2, target_positions - 1))
            if len(positions) < min_needed:
                break

        # Mark these tickers as used and add the basket
        for pos in positions:
            used_tickers.add(pos['ticker'])
        basket["alt_label"] = _label_alternative(basket, rank=len(out))
        out.append(basket)

    return out


def build_a_few_alternatives(predictions: list[dict], cash: float,
                               max_alts: int = 3) -> list[dict]:
    """v4.13.8: Build up to max_alts A FEW baskets with NO ticker
    overlap. To populate 3 alts of 3 positions each, you need at least
    9 distinct affordable BUYs in the eligibility list. Fewer = fewer
    alternatives shown.
    """
    return _build_partitioned_alts(
        predictions, cash, A_FEW_TARGET_POSITIONS, max_alts, build_a_few)


def build_diversify_alternatives(predictions: list[dict], cash: float,
                                    max_alts: int = 3) -> list[dict]:
    """v4.13.8: Build up to max_alts DIVERSIFY baskets with NO ticker
    overlap. To populate 3 alts of 5 positions each, you need at least
    15 distinct affordable BUYs in the eligibility list. Fewer = fewer
    alternatives shown.

    NOTE: build_diversify() targets 6 positions by default. v4.13.8
    reduces the per-alt target to 5 (DIVERSIFY_TARGET_POSITIONS) by
    pre-trimming the pool — but build_diversify still tries 6, then
    falls back to 5 if it can't fit. Either way, alt #2 and #3 will
    only appear if they match alt #1's position count.
    """
    if not predictions or cash <= 0:
        return []

    # v4.13.8: build_diversify uses cheapest-first to maximize
    # positions, so we don't need to pre-trim the pool — it'll naturally
    # build a 5- or 6-pick basket and the partition helper will respect
    # whatever count came out for alt #1.
    return _build_partitioned_alts(
        predictions, cash, DIVERSIFY_TARGET_POSITIONS, max_alts,
        build_diversify)
