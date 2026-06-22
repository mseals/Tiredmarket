"""tm_algo_score — v4.14.6.31 algorithmic tier-1 screen.

PORT of the old single-file build's `ts` technical scorer (the additive
points-based ranking engine), wired to take over tier-1 screening
duties so we bypass ~85% of the AI calls Tier-1 currently makes (the
volume layer) and reserve cloud-AI budget for the deeper Tier-2
consensus where multi-model judgment actually matters.

Design choices:
  - Self-contained. NO imports from tired_market.py — pure functions
    over a normalized feature dict so the scorer can be unit-tested
    and reasoned about in isolation.
  - Two public functions:
        normalize_features(raw_technicals, raw_news) -> dict
        score_for_promotion(features) -> (score, reasons)
    The split exists so the dataset-compatibility contract (the
    SCALE / CONVENTION expected by every rule) lives in ONE place
    (the adapter), and the scorer trusts its inputs are normalized.
  - Static weights (every signal weighted 1.0). Adaptive per-signal
    learning is intentionally OUT of scope for v4.14.6.31 — that's a
    Phase 4 add gated on A/B telemetry actually showing the static
    weights need refinement.
  - Every rule that fires writes a short human-readable string to
    `reasons` so the activity log / shadow JSONL is self-explanatory.

Compat audit vs. tired_market.py's `compute_technicals()` (line 5295):
  rsi              0-100              MATCH (line 5331)
  bb_position      0-100 low=oversold MATCH (line 5339)
  stoch_k          0-100              MATCH (line 5343)
  macd_crossover   tri-state {-1,0,+1} MATCH (line 5320-5322)
  volume_ratio     1.0=avg            MATCH (line 5355)
  daily_return     sign matters       MATCH (line 5358; percent)
  candle_*_engulf  0/1                MATCH (line 5460-5461)
  cci              ±100+ standard     MATCH (line 5382)
  adx              0-100              MATCH (line 5398)
  obv_slope        ratio*100          MATCH (line 5418)
  mean_reversion_z σ units            MATCH (line 5427)
  bb_squeeze       0/1                MATCH (line 5443)
  volume_trend     ratio-1            MATCH (line 5451)
  macd_histogram   price-scale scalar MATCH but absolute magnitude is
                                      ticker-dependent → use SIGN only,
                                      no fixed-magnitude rule

  news sentiment_score  old assumed ±30 thresholds — current actual
                        is [-1, +1] (tm_context_builder.py:157, 190)
                        → NEEDS-ADAPT: rescale to old units by * 100
                        before applying ±30 / ±10 thresholds.

Anything genuinely missing from compute_technicals gets a conservative
default (RSI=50, ADX=20, bb_position=50, …) matching the old build's
`.get(x, default)` pattern so a partial feature set never crashes the
scorer; the corresponding rule simply doesn't fire.
"""
from __future__ import annotations

from typing import Iterable


# ─── Config defaults — keep these in one place ────────────────────────

# Score starts here and is then adjusted by each signal rule.
NEUTRAL_BASELINE = 50.0
# Final score is clamped to [0, 100] so consumers can interpret as
# "0 = strong avoid, 100 = strong buy, ~50 = noise."
SCORE_MIN, SCORE_MAX = 0.0, 100.0

# Defaults applied when a field is missing — matches the old build's
# .get(field, default) convention so the scorer degrades cleanly on a
# fresh-clone with incomplete cache rather than crashing or scoring
# garbage.
_DEFAULTS = {
    'rsi': 50.0,
    'macd_crossover': 0,
    'macd_histogram': 0.0,
    'bb_position': 50.0,
    'stoch_k': 50.0,
    'volume_ratio': 1.0,
    'daily_return': 0.0,
    'candle_bullish_engulf': 0,
    'candle_bearish_engulf': 0,
    'cci': 0.0,
    'adx': 20.0,
    'obv_slope': 0.0,
    'mean_reversion_z': 0.0,
    'bb_squeeze': 0,
    'volume_trend': 0.0,
    # news_sentiment is post-normalization (see normalize_features).
    # Old build's ±30 / ±10 thresholds expect a value in the same
    # numeric range — the adapter rescales [-1,+1] → [-100,+100] so
    # those thresholds apply directly.
    'news_sentiment': 0.0,
    # Convenience flag — set by the adapter when news data wasn't
    # available, so the scorer can suppress news contributions
    # without confusing 0 (truly neutral) with absent.
    '_news_available': False,
    # ALGOGATE (algo-selectivity-2026-06-14): fundamentals plumbing for
    # the selectivity gate (knife-vs-bounce). Absent → treat as
    # not-deteriorating (absence ≠ bad). Sourced from cache.fundamentals.
    # GATEFIX (2026-06-14): per-field "has" flags so the veto can
    # distinguish "data present and bad" from "data missing → unknown".
    # Critical for new users: a cold-start cache has no fundamentals
    # for most tickers and 0.0 defaults must NOT be read as "bad".
    '_fund_available': False,
    '_fund_has_eps': False,
    '_fund_has_margin': False,
    '_fund_has_revenue_growth': False,
    'fund_eps_ttm': 0.0,
    'fund_pe_ratio': 0.0,
    'fund_revenue_growth': 0.0,        # ratio: latest-vs-prior, -1..+1
    'fund_profit_margin': 0.0,         # ratio: -1..+1 (negative = loss)
    # ALGOGATE: gate flag. When True, score_for_promotion suppresses
    # the oversold/Stoch-low bonuses unless the gate qualifies. When
    # False, scorer is byte-identical to pre-gate behavior.
    '_selectivity_gate': False,
    # ALGOGATE: outputs set BY the scorer when the gate runs. Read by
    # the caller (_algo_gate_decide) to stamp algo_skip_reason.
    '_gate_rejected': False,
    '_gate_reason': '',
    # ENRICH (algo-enrichment-2026-06-14): give the algo more of the
    # AI's lens, using cache data already fetched. CHEAP rules (dict
    # reads, free) always run. EXPENSIVE rules (headline text scan,
    # earnings-date math) only run when the ticker is a "survivor"
    # of the cheap pass — the weak-machine guardrail (no per-ticker
    # text work on the whole candidate stream).
    #
    # Hard constraint: USE IF PRESENT, SKIP IF ABSENT. Missing data
    # contributes ZERO — never blocks, never penalises. The technicals-
    # only core score has to stand on its own so a new user's cold
    # cache still produces ranked picks before slow data lands.
    '_enrich_enabled': True,        # cfg['algo_enrichment'] default ON
    '_survivor_threshold': 55.0,    # cheap-score floor to run expensive
    # — cheap-pass fields (dict reads) —
    'tech_momentum_10': 0.0,
    'tech_atr_pct': 0.0,
    'tech_volatility_20d': 0.0,
    '_tech_has_momentum_10': False,
    '_tech_has_atr_pct': False,
    '_tech_has_volatility_20d': False,
    # ISSUE-A FIX (algo-tech-presence-2026-06-14): per-field "has real
    # data" flags for the technicals the gate + oversold scoring rely
    # on. compute_technicals() in tired_market.py only writes these keys
    # when there are enough bars of history (RSI: n>=15, Stoch: n>=14,
    # OBV: n>=20, volume_ratio: n>=10). A thin-history ticker → key
    # absent → flag stays False → oversold bonus + gate's bounce-confirm
    # contribute ZERO. Cold-boot tickers can't fake a 99 pick on
    # default-zeros anymore. Accepted tradeoff: smaller honest board on
    # cold boot.
    '_tech_has_rsi': False,
    '_tech_has_stoch_k': False,
    '_tech_has_obv_slope': False,
    '_tech_has_volume_ratio': False,
    # TREND (algo-trend-filter-2026-06-14): SMA20 / SMA50 + current_price
    # for the trend filter. compute_technicals writes sma20 when n>=20
    # and sma50 when n>=50, so a thin-history ticker may have one and not
    # the other (or neither). Per-field has-flags so the filter handles
    # "trend unknown" correctly → contributes ZERO, never fakes a
    # downtrend, never blocks a pick on absent data.
    'sma20': 0.0,
    'sma50': 0.0,
    'current_price': 0.0,
    '_tech_has_sma20': False,
    '_tech_has_sma50': False,
    '_tech_has_current_price': False,
    # TREND output (set BY the scorer). Values:
    #   'confirmed_downtrend' — px<SMA20<SMA50 (knife structure)
    #   'confirmed_uptrend'   — px>SMA20 AND SMA20>SMA50
    #   'reclaiming'          — px>SMA20 only (reversal candidate)
    #   'neutral'             — has SMAs but no pattern fits
    #   'unknown'             — SMAs missing → never blocks, never fakes
    '_trend_state': 'unknown',
    # TREND config flag — cfg['algo_trend_filter'], default ON.
    '_trend_filter_enabled': True,
    '_fund_has_pe_ratio': False,
    '_fund_has_revenue_dollars': False,
    'fund_revenue': 0.0,
    'fund_net_income': 0.0,
    '_fund_has_net_income': False,
    'fund_analyst_rec_score': 0.0,  # -1..+1 mapped from rec key
    '_fund_has_analyst_rec': False,
    'fund_target_distance_pct': 0.0,  # (target - price) / price
    '_fund_has_target_distance': False,
    'fund_insider_net_flow_usd': 0.0,
    '_fund_has_insider_flow': False,
    # v4.14.6.76-growth-factor: multi-year growth metrics from EDGAR
    # companyfacts (computed at fetch time). None = absent (USE-IF-PRESENT:
    # the scorer adds 0 — NEVER a penalty — when growth data is missing, so
    # microcaps / thin-cache tickers never regress). `_fund_has_growth` is
    # set when ANY of the three is present.
    'fund_revenue_cagr_3y': None,
    'fund_revenue_cagr_1y': None,
    'fund_revenue_growth_stability': None,
    'fund_eps_cagr_3y': None,
    '_fund_has_growth': False,
    # v4.14.6.77-quality-factor: quality / financial-health ratios (EDGAR).
    # None = absent (USE-IF-PRESENT: 0 at score time, NEVER a penalty).
    # `_fund_has_quality` set when ANY arrived.
    'fund_quality_roe': None,
    'fund_quality_debt_to_capital': None,
    'fund_quality_current_ratio': None,
    'fund_quality_interest_coverage': None,
    'fund_quality_cf_to_sales': None,
    '_fund_has_quality': False,
    # — expensive-pass payloads (stashed for survivor read; never used
    # in the cheap pass) —
    '_raw_headlines': (),           # tuple of strings, lower-cased lazily
    '_raw_earnings_days_to_event': None,
    # — outputs / diagnostics —
    '_enrich_ran_expensive': False,
    '_enrich_factors_present': (),  # tuple of factor-name strings that
                                     # contributed (any direction). Mirrors
                                     # into the shadow record so we can
                                     # see how picks change as data fills.
    '_enrich_factors_available': (),  # tuple of factor names whose data
                                       # was present (whether or not it
                                       # nudged the score).
}


# ─── Adapter — the dataset-compatibility contract ─────────────────────

def normalize_features(
    raw_technicals: dict | None,
    raw_news: dict | None = None,
    raw_fundamentals: dict | None = None,
    selectivity_gate: bool = False,
    raw_earnings: dict | None = None,
    raw_insider_flow: dict | None = None,
    enrichment_enabled: bool = True,
    survivor_threshold: float = 55.0,
    trend_filter_enabled: bool = True,
) -> dict:
    """Produce a normalized feature dict the scorer can trust.

    Inputs:
      raw_technicals — dict from `compute_technicals(df)` in
        tired_market.py:5295. May be None / partial on a fresh clone.
      raw_news — dict-like with a `sentiment_score` key on the
        [-1, +1] convention used by tm_context_builder, or None. Pass
        whatever `get_news_features(ticker)` returns (or None).

    Output: feature dict on the OLD BUILD's expected scales for every
    rule. Missing fields filled with _DEFAULTS so the scorer never
    crashes on partial data; rules over default values produce no
    score adjustment.

    Compat conversions applied here (one place to maintain):
      • news sentiment_score [-1, +1] → news_sentiment [-100, +100]
        (the old `ts` rules use ±30 / ±10 thresholds on the larger
        scale; rescaling here means those constants stay readable in
        the scorer and the conversion is auditable in ONE place).
      • macd_histogram: sign carried through; magnitude is ticker-
        price-dependent so the scorer only ever uses sign(histogram).
      • Everything else passes through unchanged (audited match).
    """
    feats = dict(_DEFAULTS)
    if raw_technicals:
        for k in (
            'rsi', 'macd_crossover', 'macd_histogram', 'bb_position',
            'stoch_k', 'volume_ratio', 'daily_return', 'cci', 'adx',
            'obv_slope', 'mean_reversion_z', 'bb_squeeze',
            'volume_trend', 'candle_bullish_engulf',
            'candle_bearish_engulf',
        ):
            v = raw_technicals.get(k)
            if v is not None:
                try:
                    feats[k] = float(v)
                except (TypeError, ValueError):
                    # Field present but unparseable — fall back to
                    # the default (no contribution from this rule).
                    pass
        # ISSUE-A FIX (algo-tech-presence-2026-06-14): per-field
        # has-real-data flags. compute_technicals omits these keys when
        # bar count is below their requirement (RSI/Stoch/OBV/vol_ratio
        # all have minimum-bar gates). A missing key here = key absent
        # in raw_technicals → flag stays False → scorer + gate read
        # this as "data not present, contribute zero." Catches the
        # cold-boot new-user case where price history hasn't filled.
        if raw_technicals.get('rsi') is not None:
            feats['_tech_has_rsi'] = True
        if raw_technicals.get('stoch_k') is not None:
            feats['_tech_has_stoch_k'] = True
        if raw_technicals.get('obv_slope') is not None:
            feats['_tech_has_obv_slope'] = True
        if raw_technicals.get('volume_ratio') is not None:
            feats['_tech_has_volume_ratio'] = True
        # TREND: pull SMA20 / SMA50 / current_price + has-flags.
        for src_key, dst_key, flag_key in (
                ('sma20', 'sma20', '_tech_has_sma20'),
                ('sma50', 'sma50', '_tech_has_sma50'),
                ('current_price', 'current_price',
                 '_tech_has_current_price'),
        ):
            v = raw_technicals.get(src_key)
            if v is None:
                continue
            try:
                feats[dst_key] = float(v)
                feats[flag_key] = True
            except (TypeError, ValueError):
                pass
        # EMIT-BRIDGE (algo-tier1-emit-2026-06-14): pull atr ($),
        # support1, resistance1 for the level math. ATR is the
        # load-bearing input for the stop/target — without it, the
        # bridge cannot size a sane stop, so compute_algo_levels()
        # below refuses to emit on a thin-history ticker. support1 /
        # resistance1 are optional chart-structure refinements; absent
        # → raw ATR levels are used.
        for src_key, dst_key in (
                ('atr', 'atr'),
                ('support1', 'support1'),
                ('resistance1', 'resistance1'),
        ):
            v = raw_technicals.get(src_key)
            if v is None:
                continue
            try:
                feats[dst_key] = float(v)
            except (TypeError, ValueError):
                pass

    if raw_news:
        s = raw_news.get('sentiment_score')
        if s is not None:
            try:
                # [-1, +1] → [-100, +100] so the old ±30 / ±10
                # thresholds remain intuitive in the scorer.
                feats['news_sentiment'] = float(s) * 100.0
                feats['_news_available'] = True
            except (TypeError, ValueError):
                pass
        # Some tm_context_builder paths expose a richer dict — if a
        # caller wants to inject a different aggregate (e.g. weighted
        # sentiment by source), they can pass {'sentiment_score': X}
        # directly. We don't second-guess what's in the news dict
        # beyond the canonical 'sentiment_score' key.

    # ALGOGATE (algo-selectivity-2026-06-14): fundamentals for the
    # selectivity gate. cache.fundamentals() returns a dict with keys
    # like {pe_ratio, eps, dividend_yield, market_cap, sector, …}.
    # GATEFIX (2026-06-14): track WHICH fields actually arrived so the
    # veto can fire only on present-and-bad, never on missing-defaulted-to-zero.
    # A new user with empty caches has NO fundamentals — every ticker
    # would have eps=0 + margin=0 by default and get wrongly vetoed as
    # "deteriorating" without these per-field "has" flags.
    if raw_fundamentals:
        feats['_fund_available'] = True
        eps_map     = ('eps', 'eps_ttm')
        margin_map  = ('profit_margin', 'profitMargin')
        rg_map      = ('revenue_growth',)
        # EPS (loss-making test): try the candidate keys in order.
        for k in eps_map:
            v = raw_fundamentals.get(k)
            if v is None:
                continue
            try:
                feats['fund_eps_ttm'] = float(v)
                feats['_fund_has_eps'] = True
                break
            except (TypeError, ValueError):
                pass
        # Margin (value-trap test).
        for k in margin_map:
            v = raw_fundamentals.get(k)
            if v is None:
                continue
            try:
                feats['fund_profit_margin'] = float(v)
                feats['_fund_has_margin'] = True
                break
            except (TypeError, ValueError):
                pass
        # Revenue growth (informational; not currently a veto input).
        for k in rg_map:
            v = raw_fundamentals.get(k)
            if v is None:
                continue
            try:
                feats['fund_revenue_growth'] = float(v)
                feats['_fund_has_revenue_growth'] = True
                break
            except (TypeError, ValueError):
                pass
        # pe_ratio (now used by ENRICH cheap pass — extract + flag).
        v = raw_fundamentals.get('pe_ratio')
        if v is not None:
            try:
                feats['fund_pe_ratio'] = float(v)
                feats['_fund_has_pe_ratio'] = True
            except (TypeError, ValueError):
                pass
        # ENRICH (algo-enrichment-2026-06-14): extra fundamentals dict
        # reads — free, no API call (the dict is already in hand).
        # revenue + net_income for revenue-trend / loss-and-shrinking
        # logic. Analyst recommendation key + target price for the
        # consensus nudge. All gated by per-field "has" flags so absent
        # data contributes ZERO (never a veto, never a penalty).
        v = raw_fundamentals.get('revenue')
        if v is not None:
            try:
                feats['fund_revenue'] = float(v)
                feats['_fund_has_revenue_dollars'] = True
            except (TypeError, ValueError):
                pass
        v = raw_fundamentals.get('net_income')
        if v is not None:
            try:
                feats['fund_net_income'] = float(v)
                feats['_fund_has_net_income'] = True
            except (TypeError, ValueError):
                pass
        # v4.14.6.76-growth-factor: multi-year growth metrics (EDGAR). Each
        # is independently USE-IF-PRESENT; _fund_has_growth flips on if ANY
        # arrived. Absent → stays None → scorer contributes 0 (no penalty).
        for _gk in ('revenue_cagr_3y', 'revenue_cagr_1y',
                    'revenue_growth_stability', 'eps_cagr_3y'):
            _gv = raw_fundamentals.get(_gk)
            if _gv is not None:
                try:
                    feats['fund_' + _gk] = float(_gv)
                    feats['_fund_has_growth'] = True
                except (TypeError, ValueError):
                    pass
        # v4.14.6.77-quality-factor: quality/health ratios (EDGAR). Same
        # independent USE-IF-PRESENT handling; absent → None → scored as 0.
        for _qk in ('quality_roe', 'quality_debt_to_capital',
                    'quality_current_ratio', 'quality_interest_coverage',
                    'quality_cf_to_sales'):
            _qv = raw_fundamentals.get(_qk)
            if _qv is not None:
                try:
                    feats['fund_' + _qk] = float(_qv)
                    feats['_fund_has_quality'] = True
                except (TypeError, ValueError):
                    pass
        # Analyst rec key (string) → numeric lean -1..+1.
        rk = (raw_fundamentals.get('recommendation_key') or '').strip().lower()
        _rec_map = {
            'strong_buy': 1.0, 'buy': 0.6, 'outperform': 0.5,
            'hold': 0.0, 'neutral': 0.0,
            'underperform': -0.5, 'sell': -0.6, 'strong_sell': -1.0,
        }
        if rk and rk in _rec_map:
            feats['fund_analyst_rec_score'] = _rec_map[rk]
            feats['_fund_has_analyst_rec'] = True
        # Distance from current price to analyst mean target (ratio).
        # Caller must stash current price into raw_fundamentals['_cur_price']
        # for this to compute; absent → no nudge.
        tmp = raw_fundamentals.get('target_mean_price')
        cur_p = raw_fundamentals.get('_cur_price')
        if tmp is not None and cur_p is not None:
            try:
                tmp_f = float(tmp)
                cur_f = float(cur_p)
                if cur_f > 0 and tmp_f > 0:
                    feats['fund_target_distance_pct'] = (
                        (tmp_f - cur_f) / cur_f)
                    feats['_fund_has_target_distance'] = True
            except (TypeError, ValueError):
                pass

    # ENRICH: technicals dict already in scope as raw_technicals (above).
    # Pull momentum/ATR/volatility into the features dict with per-field
    # has-flags so the score rules know whether to nudge.
    if raw_technicals:
        for src_key, dst_key, flag_key in (
                ('momentum_10', 'tech_momentum_10', '_tech_has_momentum_10'),
                ('atr_pct', 'tech_atr_pct', '_tech_has_atr_pct'),
                ('volatility_20d', 'tech_volatility_20d',
                 '_tech_has_volatility_20d'),
        ):
            v = raw_technicals.get(src_key)
            if v is None:
                continue
            try:
                feats[dst_key] = float(v)
                feats[flag_key] = True
            except (TypeError, ValueError):
                pass

    # ENRICH: insider flow (separate dict; caller pulls from
    # tm_cache.get_insider_flow). Just the net_open_market_usd field.
    if raw_insider_flow:
        v = raw_insider_flow.get('net_open_market_usd')
        if v is not None:
            try:
                feats['fund_insider_net_flow_usd'] = float(v)
                feats['_fund_has_insider_flow'] = True
            except (TypeError, ValueError):
                pass

    # ENRICH: expensive-pass payloads — stashed for the survivor pass.
    # NOT read during the cheap pass. The headlines tuple is built once
    # (cheap) but the keyword scan only runs if the ticker survives.
    if raw_news:
        hl = (raw_news.get('top_headlines')
              or raw_news.get('headlines') or [])
        try:
            heads = []
            for h in hl[:6]:  # cap at 6 headlines — matches NEWS block
                t = h.get('title') if isinstance(h, dict) else str(h)
                if t:
                    heads.append(str(t))
            feats['_raw_headlines'] = tuple(heads)
        except Exception:
            feats['_raw_headlines'] = ()

    if raw_earnings:
        ne = raw_earnings.get('next_event') or {}
        d = ne.get('date')
        if d:
            try:
                # Compute days-to-event from today (UTC date).
                import datetime as _dt2
                tgt = _dt2.date.fromisoformat(str(d)[:10])
                today = _dt2.date.today()
                feats['_raw_earnings_days_to_event'] = (tgt - today).days
            except Exception:
                pass

    feats['_selectivity_gate'] = bool(selectivity_gate)
    feats['_enrich_enabled'] = bool(enrichment_enabled)
    feats['_survivor_threshold'] = float(survivor_threshold)
    feats['_trend_filter_enabled'] = bool(trend_filter_enabled)
    return feats


# ─── Scorer — the additive `ts` engine ────────────────────────────────

def score_for_promotion(features: dict) -> tuple[float, list[str]]:
    """Run the `ts` additive scorer over a NORMALIZED feature dict.

    Returns (score, reasons) where:
      score   — float in [0, 100]; ~50 = noise, higher = more
                bullish, lower = more bearish.
      reasons — list of short strings describing every rule that
                fired and its contribution. Used for explainability
                in the shadow log and for tuning.

    NOTE: callers must pass a dict produced by normalize_features().
    Feeding raw compute_technicals output works (the dict keys are
    compatible) BUT news sentiment will be on the wrong scale unless
    it went through the adapter — the ±30 / ±10 news thresholds
    assume [-100, +100] not [-1, +1]. Always call the adapter.
    """
    ts: float = NEUTRAL_BASELINE
    reasons: list[str] = []

    def _add(points: float, reason: str) -> None:
        nonlocal ts
        ts += points
        reasons.append(f"{reason} ({points:+.1f})")

    # ── TREND (algo-trend-filter-2026-06-14): the missing rule ──
    #
    # The single most universal rule in technical analysis: TREND. Pros
    # are unanimous on the falling knife — an oversold reading inside
    # a confirmed downtrend is NOT a buy. RSI/MACD "can falsely signal
    # market turns during steep declines"; the fix is to wait for price
    # to RECLAIM the short-term moving average before trusting oversold
    # as a dip-buy.
    #
    # Trend state from SMA20 / SMA50 / current_price (all already in
    # cache, no new fetch):
    #
    #   confirmed_downtrend — px < SMA20 < SMA50  (falling structure)
    #   confirmed_uptrend   — px > SMA20 AND SMA20 > SMA50
    #   reclaiming          — px > SMA20 only (basic reversal signal)
    #   neutral             — has SMAs but no clean pattern
    #   unknown             — SMAs missing (thin history)
    #
    # USE-IF-PRESENT: unknown trend never blocks, never fakes a downtrend.
    # New users with thin history get the same behavior as before — the
    # filter simply doesn't apply until real SMAs exist.
    #
    # Suppression choice: ZERO-OUT the oversold bonus when trend is
    # confirmed_downtrend AND no reversal sign is present. This is the
    # CLEANER of the two options the user spec'd (the other was "require
    # explicit reversal-confirm"). Reasoning: zero-out keeps the
    # decision local to this block — no second-pass reconsideration —
    # and naturally interoperates with the existing selectivity gate's
    # _suppress_oversold flag (we OR them).
    #
    # The reclaiming / confirmed_uptrend states ALLOW oversold (that's
    # exactly the dip-buy the pros wait for — price has come back
    # above SMA20, real reversal in motion).
    #
    # Optional light trend nudge (separate from knife suppression):
    #   confirmed_uptrend  → +3
    #   reclaiming         → +2
    #   confirmed_downtrend → -3
    # Trend informs the rank generally, not just the oversold gate.
    # Kept small — the algo stays mean-reversion; trend is filter, not
    # the main driver.
    _trend_filter_on = bool(features.get('_trend_filter_enabled'))
    _trend_has_sma = (features.get('_tech_has_sma20')
                      and features.get('_tech_has_sma50')
                      and features.get('_tech_has_current_price'))
    trend_state = 'unknown'
    if _trend_filter_on and _trend_has_sma:
        cp_t  = features['current_price']
        s20_t = features['sma20']
        s50_t = features['sma50']
        if cp_t < s20_t and cp_t < s50_t and s20_t < s50_t:
            trend_state = 'confirmed_downtrend'
        elif cp_t > s20_t and s20_t > s50_t:
            trend_state = 'confirmed_uptrend'
        elif cp_t > s20_t:
            trend_state = 'reclaiming'
        else:
            trend_state = 'neutral'
    features['_trend_state'] = trend_state

    # Light trend nudge (small, always applied when state is known —
    # doesn't depend on oversold; this is the "trend context" score).
    # v4.14.6.66-algo-momentum-align (Move B): reward "going up" at parity
    # with the oversold tier. confirmed_uptrend raised +3 → +18 so a genuine
    # uptrend can clear the 65 emit threshold on its own merits (the AI is a
    # momentum/breakout buyer — SHADOWTEST_FINDINGS: 0% dip-buyer). REUSE-ONLY:
    # SMA20/50 + volume_ratio are already computed; the 3 NEW features
    # (rel-strength vs SPY, new-20/50d-high, HH-LL structure) are Phase 2.
    if trend_state == 'confirmed_uptrend':
        _add(+18, 'trend: confirmed uptrend (px>SMA20>SMA50)')
    elif trend_state == 'reclaiming':
        _add(+2, 'trend: reclaiming SMA20 (reversal candidate)')
    elif trend_state == 'confirmed_downtrend':
        _add(-3, 'trend: confirmed downtrend (px<SMA20<SMA50)')
    # 'neutral' / 'unknown' contribute 0 — never block, never fake.

    # Reclaim/uptrend CONFIRMED BY VOLUME (≥1.5× avg on an up-day) — a real
    # buyer-backed move, not a low-volume drift. USE-IF-PRESENT: volume_ratio
    # gated on its has-flag; daily_return defaults 0 so it can't fire absent.
    if (trend_state in ('reclaiming', 'confirmed_uptrend')
            and features.get('_tech_has_volume_ratio')
            and features['volume_ratio'] >= 1.5
            and features['daily_return'] > 0):
        _add(+12, 'trend: reclaim confirmed by volume '
                  f"({features['volume_ratio']:.1f}x up-day)")

    # Knife-block flag: set when oversold is meaningless because we're
    # in a falling structure with no reversal sign. Fed into the
    # selectivity gate's existing _suppress_oversold mechanism below.
    _is_oversold_now = (features['rsi'] < 40.0
                        or features['stoch_k'] < 20.0)
    _trend_knife_block = (
        trend_state == 'confirmed_downtrend' and _is_oversold_now)
    if _trend_knife_block:
        reasons.append(
            '[trend] oversold but confirmed downtrend '
            '(px<SMA20<SMA50) → knife, oversold bonus suppressed')
        # Also flag _gate_rejected so the caller's promote=False
        # override kicks in — a confirmed-downtrend knife is the exact
        # case the gate exists to block, regardless of any other
        # positive bonuses.
        features['_gate_rejected'] = True
        features['_gate_reason'] = (
            f'trend: confirmed downtrend (oversold knife)')

    # ── ALGOGATE (algo-selectivity-2026-06-14): knife-vs-bounce gate ──
    #
    # The algo's job is to find mean-reversion dip-buys. Tonight's
    # divergence finding: of 9 algo would-promote picks, 8/9 oversold,
    # 6/9 with OBV still falling — the falling-knife signature. The
    # oversold +20-25 / Stoch +14 bonuses drown out the warning signals
    # (OBV-down -3, news-negative -8, etc.) so blind dips promote.
    #
    # When the gate is ON, we COMPUTE the oversold-qualifies decision
    # BEFORE applying the oversold/Stoch-low bonuses. If the gate
    # rejects, those bonuses are SKIPPED — the ticker can still score
    # on its other factors, it just doesn't get the dip bonus, so it
    # won't promote on oversold alone.
    #
    # Gate definitions (thresholds documented for tuning):
    #   is_oversold:                 RSI < 40 OR Stoch %K < 20
    #   obv_turning_up:              OBV slope > -2 (no longer falling
    #                                hard; sellers exhausting)
    #   capitulation_volume_spike:   volume_ratio >= 2.0 (a puke-out
    #                                bottom)
    #   news_negative:               _news_available AND news_sentiment
    #                                <= -10 (known bad-news cause)
    #   fundamentals_deteriorating:  _fund_available AND (eps_ttm <= 0
    #                                AND profit_margin <= 0) — a loss-
    #                                making, money-losing company (a
    #                                value trap, not a bounce candidate)
    #
    # oversold_qualifies =
    #     is_oversold
    #     AND (obv_turning_up OR capitulation_volume_spike)
    #     AND NOT news_negative
    #     AND NOT fundamentals_deteriorating
    rsi_for_gate   = features['rsi']
    stoch_for_gate = features['stoch_k']
    is_oversold = (rsi_for_gate < 40.0) or (stoch_for_gate < 20.0)
    gate_enabled = bool(features.get('_selectivity_gate'))
    gate_active  = gate_enabled and is_oversold
    oversold_qualifies = True
    gate_reject_reason = ''
    if gate_active:
        # ISSUE-A FIX: bounce-confirm signals only count when their
        # underlying data is REALLY present. obv_slope default = 0.0
        # would have read as "obv_turning_up=True" (0 > -2) on a
        # thin-history ticker, fake-confirming a bounce that never
        # had data. Same for volume_ratio default = 1.0 vs the 2.0
        # capitulation threshold (1.0 < 2.0 so volume default already
        # failed correctly — but gating on the has-flag here makes the
        # intent explicit and survives any future change to the
        # capitulation threshold).
        obv_has = bool(features.get('_tech_has_obv_slope'))
        vr_has  = bool(features.get('_tech_has_volume_ratio'))
        obv_for_gate = features['obv_slope']
        vr_for_gate  = features['volume_ratio']
        obv_turning_up     = obv_has and obv_for_gate > -2.0
        capitulation_spike = vr_has  and vr_for_gate >= 2.0
        # bounce-confirm: at least ONE positive signal must fire AND
        # be backed by real data. Missing data CANNOT confirm a bounce.
        if not (obv_turning_up or capitulation_spike):
            oversold_qualifies = False
            obv_label = (f"{obv_for_gate:+.1f}" if obv_has else "n/a")
            vr_label  = (f"{vr_for_gate:.1f}x"  if vr_has  else "n/a")
            gate_reject_reason = (
                f'no bounce confirm (OBV={obv_label}, '
                f'vol×={vr_label})')
        # news veto
        if oversold_qualifies and features.get('_news_available'):
            ns_for_gate = features['news_sentiment']
            if ns_for_gate <= -10.0:
                oversold_qualifies = False
                gate_reject_reason = (
                    f'news negative ({ns_for_gate:+.0f})')
        # fundamentals veto (light): loss-making AND negative-margin
        # company is a value trap, not a bounce candidate. Absent
        # fundamentals → don't veto (absence ≠ bad).
        # GATEFIX (2026-06-14): fire ONLY when BOTH eps and margin
        # came from real cached data (the _fund_has_* per-field flags).
        # A new-user cold cache has eps=margin=0.0 by default, which
        # used to wrongly trip this veto on every oversold candidate.
        # Now the veto requires the data to be present AND strictly
        # negative (real loss + real negative margin), not "absent
        # → defaulted-to-zero → looks-like-loss."
        if oversold_qualifies and features.get('_fund_available'):
            has_eps    = bool(features.get('_fund_has_eps'))
            has_margin = bool(features.get('_fund_has_margin'))
            if has_eps and has_margin:
                eps_g = features['fund_eps_ttm']
                pm_g  = features['fund_profit_margin']
                if eps_g < 0.0 and pm_g < 0.0:
                    oversold_qualifies = False
                    gate_reject_reason = (
                        f'fundamentals deteriorating (EPS={eps_g:.2f}, '
                        f'margin={pm_g:.2f})')
        if not oversold_qualifies:
            features['_gate_rejected'] = True
            features['_gate_reason']   = gate_reject_reason
            reasons.append(
                f'[algo-gate] oversold rejected: '
                f'{gate_reject_reason} → dip bonus suppressed')

    # Local switch: when True (gate active + oversold did not qualify),
    # the RSI/Stoch oversold-bonus branches below SKIP their _add calls
    # so the dip bonus is suppressed. All other rules fire as normal.
    # TREND: OR with _trend_knife_block so confirmed-downtrend oversold
    # candidates also have their RSI/Stoch bonus zeroed out.
    _suppress_oversold = (gate_active and not oversold_qualifies) \
                         or _trend_knife_block

    # v4.14.6.66-algo-momentum-align: regime flags consumed by Moves A & C.
    #   Move A (_in_uptrend): in a CONFIRMED uptrend, high RSI/Bollinger/Stoch
    #     is MOMENTUM, not overbought-sell — flip those penalties to rewards.
    #     The penalties stay fully intact in non-uptrend (flat/down) regimes
    #     where overbought IS correctly bearish.
    #   Move C (_oversold_ok): the oversold dip bonus is the AI-liked setup
    #     ONLY as a pullback IN an uptrend/reclaim (80% of AI buys are
    #     pullback-to-support). In flat/neutral/unknown it's an ambiguous dip
    #     and earns nothing; the knife gate already kills downtrend-oversold.
    _in_uptrend = (trend_state == 'confirmed_uptrend')
    _oversold_ok = ((not _suppress_oversold)
                    and trend_state in ('confirmed_uptrend', 'reclaiming'))

    # ── RSI bands ─────────────────────────────────────────────────────
    # Old build: extreme oversold <20 → +25; moderate oversold 20-30 →
    # +20; mild oversold 30-40 → +15; mild overbought 60-70 → -15;
    # moderate 70-80 → -20; extreme >80 → -25. Tiny ±5 in 40-50 / 50-60
    # noise band.
    # ISSUE-A FIX: RSI rules only fire when real RSI data is present.
    # A missing-key default (50) would have read as "above midline -5"
    # — small but wrong; gating on the has-flag means absent data
    # contributes exactly 0, matching the rule used everywhere else.
    # `rsi` is bound UNCONDITIONALLY so downstream rules (indicator-
    # agreement composite) can read it as the neutral default (50) when
    # data is absent — preserves the "missing data = neutral" intent.
    rsi = features['rsi']
    if features.get('_tech_has_rsi'):
        if rsi < 20:
            # Move C: oversold rewarded ONLY as a pullback-in-uptrend, and
            # cut +25→+15 (×0.6 across tiers) so a dip can't clear 65 on
            # oversold alone — it must add trend/volume confirmation.
            if _oversold_ok:
                _add(+15, f"RSI {rsi:.0f} extreme oversold (uptrend pullback)")
        elif rsi < 30:
            if _oversold_ok:
                _add(+12, f"RSI {rsi:.0f} oversold (uptrend pullback)")
        elif rsi < 40:
            if _oversold_ok:
                _add(+9,  f"RSI {rsi:.0f} mild oversold (uptrend pullback)")
        elif rsi < 50:  _add(+5,  f"RSI {rsi:.0f} below midline")
        elif rsi < 60:
            # Move A: 50-60 is momentum strength in a confirmed uptrend
            # (reward); overbought-lean penalty unchanged in other regimes.
            if _in_uptrend: _add(+12, f"RSI {rsi:.0f} momentum zone (uptrend)")
            else:           _add(-5,  f"RSI {rsi:.0f} above midline")
        elif rsi < 70:
            if _in_uptrend: _add(+15, f"RSI {rsi:.0f} strong momentum (uptrend)")
            else:           _add(-15, f"RSI {rsi:.0f} mild overbought")
        elif rsi < 80:  _add(-20, f"RSI {rsi:.0f} overbought")
        else:           _add(-25, f"RSI {rsi:.0f} extreme overbought")

    # ── MACD crossover + histogram ─────────────────────────────────────
    mx = int(features['macd_crossover']) if features['macd_crossover'] in (-1, 0, 1) else 0
    if mx > 0:
        _add(+12, "MACD bullish crossover")
    elif mx < 0:
        _add(-12, "MACD bearish crossover")
    # Histogram: sign-only (magnitude is ticker-price-dependent).
    hist = features['macd_histogram']
    if hist > 0:
        _add(+4, "MACD histogram positive")
    elif hist < 0:
        _add(-4, "MACD histogram negative")

    # ── Bollinger position ─────────────────────────────────────────────
    bbp = features['bb_position']
    if bbp < 5:
        # v4.14.6.67 (Gap 2): trend-gate the Bollinger-LOW oversold reward
        # behind the SAME _oversold_ok v66 applied to RSI/Stoch — fires ONLY
        # as a pullback-in-uptrend, off in unknown/downtrend/flat. Closes the
        # last side door a pure dip used to leak through. The HIGH-side
        # breakout reward below (Move A) is deliberately NOT gated.
        if _oversold_ok:
            _add(+18, f"Price far below lower Bollinger ({bbp:.0f}) — uptrend pullback")
    elif bbp < 20:
        if _oversold_ok:
            _add(+12, f"Price near lower Bollinger ({bbp:.0f}) — uptrend pullback")
    elif bbp > 95:
        # Move A: riding the UPPER band in a confirmed uptrend is a breakout
        # (reward), not overbought-sell. Penalty stays in non-uptrend regimes.
        if _in_uptrend: _add(+8,  f"Price at upper Bollinger ({bbp:.0f}) — breakout (uptrend)")
        else:           _add(-18, f"Price far above upper Bollinger ({bbp:.0f})")
    elif bbp > 80:
        if _in_uptrend: pass  # neutral in a confirmed uptrend (riding the band)
        else:           _add(-12, f"Price near upper Bollinger ({bbp:.0f})")

    # Bollinger squeeze — narrow band often precedes breakout. Small
    # nudge (direction unknown) so squeeze alone doesn't promote, but
    # combined with bullish RSI / MACD it tips a borderline case in.
    if features['bb_squeeze']:
        # Direction follows current price drift / MACD sign.
        if features['daily_return'] > 0 or hist > 0:
            _add(+3, "Bollinger squeeze + bullish drift (breakout setup)")
        elif features['daily_return'] < 0 or hist < 0:
            _add(-3, "Bollinger squeeze + bearish drift")

    # ── Stochastic %K extremes ─────────────────────────────────────────
    # ISSUE-A FIX: Stoch %K only fires when real data is present.
    # `sk` bound unconditionally to default for safety, same as RSI.
    sk = features['stoch_k']
    if features.get('_tech_has_stoch_k'):
        if sk < 10:
            # Move C: trend-gated oversold (pullback-in-uptrend only).
            if _oversold_ok:
                _add(+14, f"Stoch %K {sk:.0f} extreme oversold (uptrend pullback)")
        elif sk < 20:
            if _oversold_ok:
                _add(+8,  f"Stoch %K {sk:.0f} oversold (uptrend pullback)")
        elif sk > 90:
            # Move A: high Stoch in a confirmed uptrend is strength, not a
            # sell — neutral; penalty stays in non-uptrend regimes.
            if not _in_uptrend: _add(-14, f"Stoch %K {sk:.0f} extreme overbought")
        elif sk > 80:
            if not _in_uptrend: _add(-8,  f"Stoch %K {sk:.0f} overbought")

    # ── Volume × direction ─────────────────────────────────────────────
    # Old build: volume ratio gates a ±5 nudge in the direction of
    # today's move. Only fires on meaningful volume (>1.5x avg) since
    # ~1.0x is just noise.
    vr = features['volume_ratio']
    dr = features['daily_return']
    if vr >= 1.5:
        if dr > 0.5:
            _add(+5, f"Volume {vr:.1f}x avg with +{dr:.1f}% move")
        elif dr < -0.5:
            _add(-5, f"Volume {vr:.1f}x avg with {dr:.1f}% move")

    # ── Candle engulfing patterns ──────────────────────────────────────
    if features['candle_bullish_engulf']:
        _add(+7, "Bullish engulfing candle")
    if features['candle_bearish_engulf']:
        _add(-7, "Bearish engulfing candle")

    # ── CCI (Commodity Channel Index) ──────────────────────────────────
    cci = features['cci']
    if cci < -200:   _add(+4, f"CCI {cci:.0f} deeply oversold")
    elif cci < -100: _add(+2, f"CCI {cci:.0f} oversold")
    elif cci > 200:  _add(-4, f"CCI {cci:.0f} deeply overbought")
    elif cci > 100:  _add(-2, f"CCI {cci:.0f} overbought")

    # ── OBV slope (volume confirmation / divergence) ───────────────────
    # Old build: ±4 for confirmation (slope agrees with price drift),
    # ±3 for divergence. The OBV slope is on the *100 ratio scale here.
    obv = features['obv_slope']
    if dr > 0:
        if obv > 5:    _add(+4, f"OBV up {obv:+.1f} confirms price up")
        elif obv < -5: _add(-3, f"OBV down {obv:+.1f} diverges from price up")
    elif dr < 0:
        if obv < -5:   _add(-4, f"OBV down {obv:+.1f} confirms price down")
        elif obv > 5:  _add(+3, f"OBV up {obv:+.1f} diverges from price down")

    # ── Mean-reversion z-score ─────────────────────────────────────────
    z = features['mean_reversion_z']
    if z < -2.0:
        # v4.14.6.67 (Gap 2): trend-gate the negative-z (oversold) reward,
        # same _oversold_ok as RSI/Stoch/BB-low — pullback-in-uptrend only.
        # The positive-z (overbought) PENALTY side stays ungated (correct in
        # every regime).
        if _oversold_ok:
            _add(+10, f"z={z:.1f} stretched below 20d mean (uptrend pullback)")
    elif z < -1.5:
        if _oversold_ok:
            _add(+5,  f"z={z:.1f} moderately below 20d mean (uptrend pullback)")
    elif z > 2.0:   _add(-10, f"z={z:.1f} stretched above 20d mean")
    elif z > 1.5:   _add(-5,  f"z={z:.1f} moderately above 20d mean")

    # ── News sentiment (normalized to [-100, +100] by adapter) ─────────
    if features.get('_news_available'):
        ns = features['news_sentiment']
        if ns >= 30:    _add(+8, f"News sentiment +{ns:.0f} strongly positive")
        elif ns >= 10:  _add(+4, f"News sentiment +{ns:.0f} positive")
        elif ns <= -30: _add(-8, f"News sentiment {ns:.0f} strongly negative")
        elif ns <= -10: _add(-4, f"News sentiment {ns:.0f} negative")

    # ── Indicator agreement / conflict ─────────────────────────────────
    # Old build: when RSI + MACD + Bollinger all lean the same way,
    # add a small confidence bonus. When they conflict, pull score
    # toward neutral (the indicators disagree → reduce conviction).
    rsi_lean   = -1 if rsi > 60 else +1 if rsi < 40 else 0
    macd_lean  = mx if mx else (+1 if hist > 0 else -1 if hist < 0 else 0)
    bb_lean    = +1 if bbp < 30 else -1 if bbp > 70 else 0
    leans = [x for x in (rsi_lean, macd_lean, bb_lean) if x]
    if leans:
        if all(x > 0 for x in leans) and len(leans) >= 2:
            _add(+6, "All major indicators bullish (agreement)")
        elif all(x < 0 for x in leans) and len(leans) >= 2:
            _add(-6, "All major indicators bearish (agreement)")
        elif len(leans) >= 2 and len(set(leans)) > 1:
            # Conflict — gently pull current adjustment toward neutral.
            delta = NEUTRAL_BASELINE - ts
            pull = round(delta * 0.10, 1)  # 10% toward neutral
            if abs(pull) >= 0.5:  # don't bother for tiny adjustments
                _add(pull, "Indicator conflict — pulling toward neutral")

    # ── ADX trend-strength modulation (final pass) ─────────────────────
    # Strong trend (>40) amplifies the current adjustment; no trend
    # (<15) pulls the score toward neutral 50. Modulation is the LAST
    # step so it scales the cumulative signal-direction rather than
    # any individual rule.
    adx = features['adx']
    if adx > 40:
        # Amplify the signed delta from neutral by +20%.
        delta = ts - NEUTRAL_BASELINE
        _add(round(delta * 0.20, 1),
             f"ADX {adx:.0f} strong trend (amplify signal)")
    elif adx < 15:
        # No trend — pull 30% back toward neutral.
        delta = NEUTRAL_BASELINE - ts
        _add(round(delta * 0.30, 1),
             f"ADX {adx:.0f} no trend (pull to neutral)")

    # ── ENRICH (algo-enrichment-2026-06-14): widen the algo's lens ──
    #
    # CHEAP PASS — dict reads, free per ticker. Runs every time the
    # enrichment flag is on. USE IF PRESENT / SKIP IF ABSENT: every
    # rule reads its `_*_has_*` flag and contributes ZERO if missing.
    # The technicals-only core score above is unchanged; these are
    # ADDITIVE nudges on top, never gates.
    #
    # EXPENSIVE PASS — runs only when the ticker is a "survivor" of
    # the cheap pass: cheap_score >= survivor_threshold (cfg, default
    # 55, ~10 below the 65 promote threshold) OR the ticker is in the
    # oversold zone where the trap-preventer gates matter most. The
    # text headline scan + earnings-proximity math run here, on the
    # ~5-15% of candidates that have a chance to either promote or
    # be saved from a knife. Weak-machine guardrail: never iterate
    # headlines on a clear non-promote.
    #
    # NB: feats['_gate_rejected'] from the selectivity gate is
    # authoritative — gate-rejected knives skip enrichment entirely.
    # _algo_gate_decide will force algo_would_promote=False anyway,
    # so no point spending CPU here on them.
    _enrich_factors_present: list[str] = []
    _enrich_factors_available: list[str] = []
    enrich_on = bool(features.get('_enrich_enabled'))
    if enrich_on and not features.get('_gate_rejected'):
        # ── CHEAP PASS ─────────────────────────────────────────────────
        # momentum_10: persistent uptrend = soft +nudge; deep downtrend
        # = soft -nudge. Already a 0-bound ratio in the technicals dict.
        if features.get('_tech_has_momentum_10'):
            _enrich_factors_available.append('momentum_10')
            m10 = features['tech_momentum_10']
            if m10 >= 10.0:
                # Move B: momentum is a primary "going up" signal — raised
                # +4→+12 (≥10%) / +2→+6 (≥5%) to matter against the 65 gate.
                _add(+12, f"10d momentum +{m10:.1f}% (strong uptrend)")
                _enrich_factors_present.append('momentum_10')
            elif m10 >= 5.0:
                _add(+6, f"10d momentum +{m10:.1f}% (uptrend)")
                _enrich_factors_present.append('momentum_10')
            elif m10 <= -10.0:
                _add(-4, f"10d momentum {m10:.1f}% (deep down)")
                _enrich_factors_present.append('momentum_10')
            elif m10 <= -3.0:
                _add(-2, f"10d momentum {m10:.1f}% (mild down)")
                _enrich_factors_present.append('momentum_10')

        # ATR%: high volatility = lottery character; no direction, but a
        # small penalty in moderate/conservative interpretation. Keep
        # it tiny — this is informational.
        if features.get('_tech_has_atr_pct'):
            _enrich_factors_available.append('atr_pct')
            atr = features['tech_atr_pct']
            if atr >= 8.0:
                _add(-2, f"ATR {atr:.1f}% (very volatile — wide swings)")
                _enrich_factors_present.append('atr_pct')

        # P/E sanity: absurdly high P/E on an oversold dip is a bad sign
        # (paying premium for a falling knife). Only fires when P/E is
        # PRESENT and >50; below that → no contribution. Negative P/E
        # means losing money — also a yellow flag but the fundamentals
        # gate already covers the eps<0+margin<0 case.
        if features.get('_fund_has_pe_ratio'):
            _enrich_factors_available.append('pe_ratio')
            pe = features['fund_pe_ratio']
            if pe > 100.0:
                _add(-3, f"P/E {pe:.0f} extreme (overpriced)")
                _enrich_factors_present.append('pe_ratio')
            elif pe > 50.0:
                _add(-1, f"P/E {pe:.0f} high")
                _enrich_factors_present.append('pe_ratio')

        # Analyst consensus: present + buy → small +nudge; present + sell
        # → small -nudge. The rec_map yields ±0.5..±1.0 — scale to ±4.
        if features.get('_fund_has_analyst_rec'):
            _enrich_factors_available.append('analyst_rec')
            rec = features['fund_analyst_rec_score']
            if abs(rec) >= 0.3:
                pts = round(rec * 4.0, 1)
                label = ('analyst consensus buy' if rec > 0
                         else 'analyst consensus sell')
                _add(pts, f"{label} ({rec:+.1f})")
                _enrich_factors_present.append('analyst_rec')

        # Analyst mean target distance from current price: meaningful
        # upside → small +nudge.
        if features.get('_fund_has_target_distance'):
            _enrich_factors_available.append('target_distance')
            td = features['fund_target_distance_pct']
            if td >= 0.20:
                _add(+3, f"analyst target {td*100:+.0f}% upside")
                _enrich_factors_present.append('target_distance')
            elif td <= -0.10:
                _add(-2, f"analyst target {td*100:+.0f}% downside")
                _enrich_factors_present.append('target_distance')

        # Insider open-market net flow: large net buying = small +nudge,
        # large net selling = small -nudge. Caller already gated on
        # >$10k as "meaningful."
        # v4.14.6.78-insider-factor: combined-fundamentals tally + cap. Defined
        # HERE (before the insider block — insider runs first) so growth +
        # quality + insider-BUY together stay <= _FUND_COMBINED_CAP, keeping
        # the positive fundamental stack from promoting a no-momentum stock
        # alone (50 + 14 = 64 < 65). The insider SELL side is an UNCAPPED
        # deduction (a deduction can't over-promote — it only dings), so it
        # never feeds the tally.
        _fund_factor_pts_used = 0.0
        _FUND_COMBINED_CAP = 14.0

        if features.get('_fund_has_insider_flow'):
            _enrich_factors_available.append('insider_flow')
            ifl = features['fund_insider_net_flow_usd']
            if ifl >= 100_000.0:
                # Net BUYING → small + nudge, bounded by the combined cap.
                _ipts = min(3.0, _FUND_COMBINED_CAP - _fund_factor_pts_used)
                if _ipts > 0:
                    _add(_ipts, f"insider net buying ${ifl/1000:.0f}k")
                    _enrich_factors_present.append('insider_flow')
                    _fund_factor_pts_used += _ipts
            elif ifl <= -100_000.0:
                # Net SELLING → small − deduction (uncapped; can't over-promote).
                _add(-3, f"insider net selling ${-ifl/1000:.0f}k")
                _enrich_factors_present.append('insider_flow')

        # Revenue trend gate-widen (light): present + losing money AND
        # net income getting MORE negative → mild penalty. Distinct from
        # the gate's veto (fundamentals_deteriorating already vetoes a
        # full oversold-dip promotion); this is a score nudge for ALL
        # candidates with present-and-bad fundamentals, oversold or not.
        if (features.get('_fund_has_net_income')
                and features.get('_fund_has_revenue_dollars')):
            _enrich_factors_available.append('income_trend')
            ni = features['fund_net_income']
            rev = features['fund_revenue']
            if ni < 0 and rev > 0:
                loss_ratio = abs(ni) / max(rev, 1.0)
                if loss_ratio > 0.5:
                    _add(-3, f"net income loss {loss_ratio*100:.0f}% of revenue")
                    _enrich_factors_present.append('income_trend')

        # v4.14.6.78: `_fund_factor_pts_used` / `_FUND_COMBINED_CAP` are now
        # initialized ABOVE (before the insider block) — do NOT re-init here
        # or it would wipe the insider-buy contribution before growth/quality.

        # ── GROWTH (v4.14.6.76-growth-factor) ──────────────────────────
        # Credit multi-year revenue growth + steadiness — CFRA's #1 driver,
        # invisible to the algo until now. MODEST + USE-IF-PRESENT:
        #   - absent series → contributes EXACTLY 0, NEVER a penalty (a
        #     microcap / thin-cache / no-EDGAR ticker scores as it does
        #     today; band_5_10 must not regress);
        #   - capped at +12 total so growth REFINES the momentum core
        #     (v66/v67), never overrides it (one strong momentum signal is
        #     ~+15);
        #   - flat / declining growth → 0, NOT a negative (no veto here —
        #     the gate already handles genuine deterioration).
        if features.get('_fund_has_growth'):
            _enrich_factors_available.append('growth')
            _g3 = features.get('fund_revenue_cagr_3y')
            _g1 = features.get('fund_revenue_cagr_1y')
            _gs = features.get('fund_revenue_growth_stability')
            _ge = features.get('fund_eps_cagr_3y')
            _gpts = 0.0
            if _g3 is not None:
                # 3-yr revenue CAGR is the primary signal (tiered).
                if _g3 >= 0.30:    _gpts += 8.0   # ~30%+ sustained (MAMA-class)
                elif _g3 >= 0.15:  _gpts += 5.0
                elif _g3 >= 0.07:  _gpts += 2.0
                # flat / declining → 0 (no penalty)
            elif _g1 is not None:
                # No 3-yr window yet (newer issuer) → lean lightly on 1-yr.
                if _g1 >= 0.30:    _gpts += 4.0
                elif _g1 >= 0.15:  _gpts += 2.0
            # Steadiness bonus only when growth is actually positive (don't
            # reward a smoothly-DECLINING series).
            if _gpts > 0 and _gs is not None and _gs >= 0.67:
                _gpts += 2.0
            # EPS compounding confirmation (small; EPS series is noisy).
            if _gpts > 0 and _ge is not None and _ge >= 0.15:
                _gpts += 2.0
            _gpts = min(_gpts, 12.0)   # individual hard cap
            # v4.14.6.78: also bound by the COMBINED fundamentals cap (insider
            # may have already used some), so growth+quality+insider-buy <= 14.
            _gpts = max(0.0, min(_gpts, _FUND_COMBINED_CAP - _fund_factor_pts_used))
            if _gpts > 0:
                _lbl = ("multi-year growth: rev CAGR3y "
                        f"{_g3*100:.0f}%" if _g3 is not None
                        else f"multi-year growth: rev CAGR1y {_g1*100:.0f}%")
                _add(_gpts, _lbl)
                _enrich_factors_present.append('growth')
                _fund_factor_pts_used += _gpts   # feeds the combined cap

        # ── QUALITY / financial-health (v4.14.6.77-quality-factor) ─────
        # Credit profitability / leverage / liquidity / coverage — CFRA
        # "Quality" + "Financial Health", LSEG "Fundamental". Same discipline
        # as growth:
        #   - MODEST + capped at +12 (quality alone never dominates momentum);
        #   - COMBINED with growth, soft-capped at +14 so the fundamentals
        #     stack ALONE stays below the 65 promote threshold (base 50 + 14 =
        #     64) — i.e. growth+quality REFINE the ranking of momentum picks
        #     but can NEVER promote a no-momentum stock on fundamentals alone
        #     (matches v76's deliberate growth-alone=62<65 design);
        #   - USE-IF-PRESENT: absent ratios → EXACTLY 0, NEVER a penalty
        #     (microcaps/thin-cache/no-EDGAR must score as before; band_5_10
        #     must not regress);
        #   - PROFITABILITY/HEALTH only, NOT valuation (the P/E nudge already
        #     handles that); weak health → small-or-zero, never a big veto
        #     (the consensus panel handles genuine-weakness rejection, v75).
        _FUND_COMBINED_CAP = 14.0
        if features.get('_fund_has_quality'):
            _enrich_factors_available.append('quality')
            _roe = features.get('fund_quality_roe')
            _d2c = features.get('fund_quality_debt_to_capital')
            _cr  = features.get('fund_quality_current_ratio')
            _ic  = features.get('fund_quality_interest_coverage')
            _cfs = features.get('fund_quality_cf_to_sales')
            _qpts = 0.0
            if _roe is not None:                       # profitability
                if _roe >= 0.12:   _qpts += 4.0
                elif _roe >= 0.06: _qpts += 2.0
            if _d2c is not None:                       # leverage (lower=healthier)
                if _d2c <= 0.20:   _qpts += 3.0
                elif _d2c <= 0.40: _qpts += 1.0
            if _cr is not None:                        # liquidity
                if _cr >= 2.0:     _qpts += 2.0
                elif _cr >= 1.2:   _qpts += 1.0
            if _ic is not None and _ic >= 5.0:         # interest coverage
                _qpts += 2.0
            if _cfs is not None and _cfs >= 0.08:      # earnings quality
                _qpts += 1.0
            _qpts = min(_qpts, 12.0)                   # quality hard cap
            # Combined soft-cap: never let growth+quality exceed +18 total.
            _qpts = max(0.0, min(_qpts, _FUND_COMBINED_CAP - _fund_factor_pts_used))
            if _qpts > 0:
                _add(_qpts, f"financial quality/health (+{_qpts:.0f})")
                _enrich_factors_present.append('quality')
                _fund_factor_pts_used += _qpts

        # ── EXPENSIVE PASS — survivors only ────────────────────────────
        # A "survivor" is one of:
        #   (a) cheap_score >= survivor_threshold (default 55), OR
        #   (b) the ticker is in the oversold zone (RSI<40 or Stoch<20)
        #       — where the trap-preventer text/earnings gates matter
        #       most.
        # Non-survivors skip the text scan entirely → no per-ticker
        # iteration cost. Weak-machine safe.
        cheap_score = ts
        thr = features.get('_survivor_threshold') or 55.0
        is_oversold_now = (features['rsi'] < 40.0
                           or features['stoch_k'] < 20.0)
        is_survivor = (cheap_score >= thr) or is_oversold_now
        if is_survivor:
            features['_enrich_ran_expensive'] = True

            # Bad-news catalyst veto — hard knock to score (effectively
            # gates the promotion when present). USE IF PRESENT.
            heads = features.get('_raw_headlines') or ()
            if heads:
                _enrich_factors_available.append('headline_scan')
                # Lower-case once, scan whole-token + substring.
                joined = ' | '.join(heads).lower()
                _NEG_KEYS = (
                    'lawsuit', 'sued ', ' sue ', 'sec probe',
                    'investigation', 'fraud', 'bankrupt',
                    'going concern', 'guidance cut', 'lowered guidance',
                    'slashed guidance', 'fda reject',
                    'complete response letter', ' crl ', 'recall',
                    'going-concern',
                )
                _POS_KEYS = (
                    'beat estimates', 'earnings beat', ' beats ',
                    'fda approval', 'fda approved', 'fda cleared',
                    'upgrade', 'upgraded', 'buyback', 'repurchase',
                    'raised guidance', 'raised outlook',
                )
                hit_neg = next((k for k in _NEG_KEYS if k in joined), None)
                hit_pos = next((k for k in _POS_KEYS if k in joined), None)
                if hit_neg:
                    # Hard penalty — a present bad-news catalyst is the
                    # AI's exact "lean WATCH/AVOID" signal.
                    _add(-15, f"headline: bad-news catalyst ({hit_neg!r})")
                    _enrich_factors_present.append('headline_negative')
                    # Mark as gate-rejected so the caller's promote=False
                    # override kicks in (this IS a knife — known cause).
                    features['_gate_rejected'] = True
                    features['_gate_reason'] = (
                        f'bad-news headline catalyst ({hit_neg!r})')
                if hit_pos:
                    _add(+5, f"headline: positive catalyst ({hit_pos!r})")
                    _enrich_factors_present.append('headline_positive')

            # Earnings proximity: suppress an oversold-dip promotion when
            # earnings are within ~14 days. USE IF PRESENT — no earnings
            # data → no gate.
            d2e = features.get('_raw_earnings_days_to_event')
            if d2e is not None:
                _enrich_factors_available.append('earnings_proximity')
                if 0 <= d2e <= 14 and is_oversold_now:
                    # Same authority as the selectivity gate: mark
                    # rejected, the caller will force promote=False.
                    features['_gate_rejected'] = True
                    features['_gate_reason'] = (
                        f'earnings in {d2e}d — dip-buy suppressed')
                    _add(-10,
                         f"earnings in {d2e}d → oversold dip suppressed")
                    _enrich_factors_present.append('earnings_proximity')
                elif 0 <= d2e <= 14:
                    # Not oversold but earnings imminent → very mild
                    # caution nudge (don't fully gate, just less keen).
                    _add(-2, f"earnings in {d2e}d (event risk)")
                    _enrich_factors_present.append('earnings_proximity')

    # Stash the diagnostic tuples for the caller / shadow logger.
    features['_enrich_factors_present'] = tuple(_enrich_factors_present)
    features['_enrich_factors_available'] = tuple(_enrich_factors_available)

    # Clamp.
    if ts < SCORE_MIN:
        ts = SCORE_MIN
    elif ts > SCORE_MAX:
        ts = SCORE_MAX

    return round(ts, 2), reasons


# ─── Convenience top-level for callers ────────────────────────────────

def score_ticker(
    raw_technicals: dict | None,
    raw_news: dict | None = None,
) -> tuple[float, list[str]]:
    """One-shot: adapter + scorer. Use when the caller doesn't need
    to inspect the normalized feature dict separately."""
    feats = normalize_features(raw_technicals, raw_news)
    return score_for_promotion(feats)


# ─── Promotion gate logic ─────────────────────────────────────────────

def should_promote(
    score: float,
    threshold: float,
    event_triggered: bool,
    trigger_bypass: bool = True,
) -> tuple[bool, str]:
    """Standard promotion decision. Returns (promote, reason)."""
    if event_triggered and trigger_bypass:
        return True, f"event-trigger bypass (score {score:.1f})"
    if score >= threshold:
        return True, f"score {score:.1f} >= threshold {threshold:.1f}"
    return False, f"score {score:.1f} < threshold {threshold:.1f}"


# ─── EMIT-BRIDGE (algo-tier1-emit-2026-06-14): level math ─────────────
#
# Self-contained level helper used when cfg['algo_tier1_emit'] is on.
# Computes (buy_zone, target, stop, timeframe_days) from cache.technicals
# fields ONLY — no extra fetch. USE-IF-PRESENT discipline: ATR is the
# load-bearing input, and if it isn't in the cache (thin history) the
# function returns None → the caller skips the emit. NEVER fabricates a
# stop on default values; a thin-history ticker gets no pick rather
# than a garbage pick.
#
# Stop / target / buy_zone math (defaults below; cfg-able for tuning):
#   stop      = max(price - stop_mult * ATR, support1)
#               where support1 may only pin the stop if it sits BELOW
#               buy_zone_low AND above raw ATR stop. The buy_zone_low
#               constraint is critical — without it, a support level
#               sitting INSIDE the buy_zone would put the stop above
#               part of the entry range (the ABOS bug found in the
#               throwaway verification: stop=2.32 with buy_zone_low=
#               2.30 → triggers immediately on entry at the lower end).
#   target_raw = price + target_mult * ATR
#   target    = min(target_raw, resistance1) if (resistance1 > price
#               AND capping doesn't crush RR below 1:1); else
#               target_raw. The "don't crush RR" guard means we never
#               emit a pick with terrible RR just to respect near
#               resistance (the ACDC case from the throwaway).
#   buy_zone  = (price - buyzone_mult * ATR, price)
#
# Timeframe estimate:
#   base = (target_distance_pct / max(|momentum_10|, momentum_floor)) * scale
#   vol_adj = 1.0 / max(1.0, volatility_20d / vol_baseline)
#   adx_adj = 1.0 + max(0, (adx - 25) / 100.0)
#   days = clamp(base * vol_adj * adx_adj, [days_floor, days_cap])
# Tuning fix in this promotion: days_floor = 7 (was 3 in throwaway).
# A 3-day window was floor-ing high-momentum tickers (e.g. ACDC) — 7
# gives even fast movers a real swing window.

EMIT_STOP_ATR_MULT_DEFAULT     = 2.0
EMIT_TARGET_ATR_MULT_DEFAULT   = 4.0
EMIT_BUYZONE_ATR_MULT_DEFAULT  = 0.5
EMIT_TIMEFRAME_SCALE_DEFAULT   = 2.0
EMIT_MOMENTUM_FLOOR_DEFAULT    = 1.0
EMIT_VOL_BASELINE_DEFAULT      = 3.0
EMIT_DAYS_FLOOR_DEFAULT        = 7   # promotion tuning fix (was 3)
EMIT_DAYS_CAP_DEFAULT          = 30  # PATH_EXPIRATION_DAYS for every band


def compute_algo_levels(features: dict,
                        cfg: dict | None = None
                        ) -> dict | None:
    """Compute entry/target/stop/timeframe for an algo BUY emission.

    Returns dict with keys:
        buy_zone_low, buy_zone_high, target, stop, timeframe_days,
        rr_ratio, notes, inputs
    OR returns None when the required inputs (ATR, current_price) aren't
    real (USE-IF-PRESENT: thin history → no emit, never fabricate).

    `cfg` is the app cfg dict (or None). Tunable knobs read from cfg:
        algo_tier1_stop_atr_mult      (default 2.0)
        algo_tier1_target_atr_mult    (default 4.0)
        algo_tier1_buyzone_atr_mult   (default 0.5)
        algo_tier1_timeframe_scale    (default 2.0)
        algo_tier1_momentum_floor     (default 1.0)
        algo_tier1_vol_baseline       (default 3.0)
        algo_tier1_days_floor         (default 7)
        algo_tier1_days_cap           (default 30)
    """
    cfg = cfg or {}
    # USE-IF-PRESENT gate.
    if not features.get('_tech_has_current_price'):
        return None
    price = float(features.get('current_price') or 0.0)
    if price <= 0:
        return None
    atr = features.get('atr')
    atr_pct = features.get('tech_atr_pct')
    if atr is None and not features.get('_tech_has_atr_pct'):
        return None
    if atr is None:
        atr = (float(atr_pct) / 100.0) * price
    try:
        atr = float(atr)
    except (TypeError, ValueError):
        return None
    if atr <= 0:
        return None

    stop_mult     = float(cfg.get('algo_tier1_stop_atr_mult',
                                   EMIT_STOP_ATR_MULT_DEFAULT))
    target_mult   = float(cfg.get('algo_tier1_target_atr_mult',
                                   EMIT_TARGET_ATR_MULT_DEFAULT))
    buyzone_mult  = float(cfg.get('algo_tier1_buyzone_atr_mult',
                                   EMIT_BUYZONE_ATR_MULT_DEFAULT))

    raw_stop   = price - stop_mult   * atr
    raw_target = price + target_mult * atr
    buy_zone_low  = round(price - buyzone_mult * atr, 4)
    buy_zone_high = round(price, 4)

    # Stop refinement: pin to support1 ONLY if it sits BELOW buy_zone_low
    # AND above raw_stop. The buy_zone_low constraint is the bug fix —
    # without it support1 can land inside the entry range (ABOS/ADT
    # cases in the throwaway).
    support1 = features.get('support1')
    notes_parts = []
    final_stop = raw_stop
    try:
        if (support1 is not None
                and isinstance(support1, (int, float))
                and float(support1) > 0
                and float(support1) < buy_zone_low
                and float(support1) > raw_stop):
            final_stop = float(support1)
            notes_parts.append(
                f"stop pinned to support1 ({final_stop:.2f}) "
                f"above raw ATR stop ({raw_stop:.2f})")
    except Exception:
        pass

    # Target refinement: cap at resistance1 if it's a real ceiling AND
    # capping doesn't crush RR below 1:1.
    resistance1 = features.get('resistance1')
    final_target = raw_target
    try:
        if (resistance1 is not None
                and isinstance(resistance1, (int, float))
                and float(resistance1) > price
                and float(resistance1) < raw_target):
            cap_target = float(resistance1)
            cap_rr = (cap_target - price) / max(price - final_stop, 1e-9)
            if cap_rr >= 1.0:
                final_target = cap_target
                notes_parts.append(
                    f"target capped at resistance1 ({final_target:.2f}) "
                    f"(RR {cap_rr:.1f}:1)")
            else:
                notes_parts.append(
                    f"resistance1 ({float(resistance1):.2f}) capping would "
                    f"crush RR to {cap_rr:.1f}:1; kept raw ATR target")
    except Exception:
        pass

    risk = max(price - final_stop, 1e-9)
    reward = max(final_target - price, 0.0)
    rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0

    # Timeframe estimate.
    scale          = float(cfg.get('algo_tier1_timeframe_scale',
                                    EMIT_TIMEFRAME_SCALE_DEFAULT))
    momentum_floor = float(cfg.get('algo_tier1_momentum_floor',
                                    EMIT_MOMENTUM_FLOOR_DEFAULT))
    vol_baseline   = float(cfg.get('algo_tier1_vol_baseline',
                                    EMIT_VOL_BASELINE_DEFAULT))
    days_floor     = int(cfg.get('algo_tier1_days_floor',
                                  EMIT_DAYS_FLOOR_DEFAULT))
    days_cap       = int(cfg.get('algo_tier1_days_cap',
                                  EMIT_DAYS_CAP_DEFAULT))
    target_distance_pct = (final_target - price) / price * 100.0
    momentum_10 = features.get('tech_momentum_10', 0.0)
    try:
        momentum_pct = float(momentum_10)
    except (TypeError, ValueError):
        momentum_pct = 0.0
    abs_momentum = max(abs(momentum_pct), momentum_floor)
    base_days = (target_distance_pct / abs_momentum) * scale
    vol = features.get('tech_volatility_20d', 0.0)
    try:
        vol_v = float(vol)
    except (TypeError, ValueError):
        vol_v = 0.0
    vol_adj = 1.0 / max(1.0, vol_v / max(vol_baseline, 1e-9))
    adx = features.get('adx', 25.0)
    try:
        adx_v = float(adx)
    except (TypeError, ValueError):
        adx_v = 25.0
    adx_adj = 1.0 + max(0.0, (adx_v - 25.0) / 100.0)
    raw_days = base_days * vol_adj * adx_adj
    if raw_days < days_floor:
        raw_days = days_floor
    if raw_days > days_cap:
        raw_days = days_cap
    timeframe_days = int(round(raw_days))

    return {
        'buy_zone_low':   float(buy_zone_low),
        'buy_zone_high':  float(buy_zone_high),
        'target':         round(float(final_target), 4),
        'stop':           round(float(final_stop), 4),
        'timeframe_days': timeframe_days,
        'rr_ratio':       rr_ratio,
        'notes':          '; '.join(notes_parts) if notes_parts else '',
        'inputs': {
            'price': price, 'atr': atr,
            'stop_mult': stop_mult, 'target_mult': target_mult,
            'buyzone_mult': buyzone_mult,
            'support1': support1, 'resistance1': resistance1,
            'momentum_10': momentum_pct,
            'volatility_20d': vol_v, 'adx': adx_v,
        },
    }
