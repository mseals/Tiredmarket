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
}


# ─── Adapter — the dataset-compatibility contract ─────────────────────

def normalize_features(
    raw_technicals: dict | None,
    raw_news: dict | None = None,
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

    # ── RSI bands ─────────────────────────────────────────────────────
    # Old build: extreme oversold <20 → +25; moderate oversold 20-30 →
    # +20; mild oversold 30-40 → +15; mild overbought 60-70 → -15;
    # moderate 70-80 → -20; extreme >80 → -25. Tiny ±5 in 40-50 / 50-60
    # noise band.
    rsi = features['rsi']
    if rsi < 20:    _add(+25, f"RSI {rsi:.0f} extreme oversold")
    elif rsi < 30:  _add(+20, f"RSI {rsi:.0f} oversold")
    elif rsi < 40:  _add(+15, f"RSI {rsi:.0f} mild oversold")
    elif rsi < 50:  _add(+5,  f"RSI {rsi:.0f} below midline")
    elif rsi < 60:  _add(-5,  f"RSI {rsi:.0f} above midline")
    elif rsi < 70:  _add(-15, f"RSI {rsi:.0f} mild overbought")
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
    if bbp < 5:    _add(+18, f"Price far below lower Bollinger ({bbp:.0f})")
    elif bbp < 20: _add(+12, f"Price near lower Bollinger ({bbp:.0f})")
    elif bbp > 95: _add(-18, f"Price far above upper Bollinger ({bbp:.0f})")
    elif bbp > 80: _add(-12, f"Price near upper Bollinger ({bbp:.0f})")

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
    sk = features['stoch_k']
    if sk < 10:    _add(+14, f"Stoch %K {sk:.0f} extreme oversold")
    elif sk < 20:  _add(+8,  f"Stoch %K {sk:.0f} oversold")
    elif sk > 90:  _add(-14, f"Stoch %K {sk:.0f} extreme overbought")
    elif sk > 80:  _add(-8,  f"Stoch %K {sk:.0f} overbought")

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
    if z < -2.0:    _add(+10, f"z={z:.1f} stretched below 20d mean")
    elif z < -1.5:  _add(+5,  f"z={z:.1f} moderately below 20d mean")
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
