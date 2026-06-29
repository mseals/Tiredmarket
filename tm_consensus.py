"""
tm_consensus — v4.11.0 Phase 2: holdings-consensus runner.

Runs N AI models sequentially against a single owned-position prompt,
streams progress back to the caller via callbacks, and writes:
    - one signals.jsonl entry per model (full per-model history)
    - one rollup signals.jsonl entry with kind='consensus' + votes array

The portfolio panel reads the rollup line for fast card render and reads
the per-model entries for the View Reasoning popup.

Public API:
    runner = ConsensusRunner(
        ticker='IONQ',
        holding=holding_dict,
        models=['qwen2.5:14b', 'deepseek-r1:14b', ...],
        path='lottery',
        prompt_builder=PromptBuilder instance,
        signals_log=SignalsLog instance,
        on_model_start=callable,    # called as on_model_start(model_name)
        on_model_done=callable,     # called as on_model_done(model, vote_dict)
        on_model_error=callable,    # called as on_model_error(model, msg)
        on_all_done=callable,       # called as on_all_done(consensus_dict)
        log_callback=callable,      # called as log_callback(msg, tag) for activity
    )
    runner.start()    # spins up a background thread
    runner.cancel()   # signals the in-flight model to bail; subsequent skip

Cancellation: setting `cancel()` flips an event. The current AIRequest is
told to cancel, the orchestrator stops looping, and on_all_done is still
called (with whatever partial results we have) so the caller can finalize.

Error handling: a model that errors out is recorded as an error vote and
we continue to the next model. One bad model doesn't doom the whole run.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

# v4.14.5.14-ollama-purge-3a: the tm_ai (Ollama client) soft-import was
# removed — consensus is cloud-only (RouterRun / providers). The tm_ai.py
# file itself is deleted in the final Ollama-purge patch.

# v4.13.43: cloud LLM providers also feed into consensus.
# Soft-imported so an environment without tm_api_providers still runs
# the local-Ollama-only path correctly.
try:
    import tm_api_providers as tm_apis
except Exception:
    tm_apis = None  # type: ignore

# v4.13.55b: provider health tracking (cooldowns + daily caps).
# Soft-imported. If absent, consensus runs the old "call every enabled
# provider" behavior unchanged.
try:
    import tm_provider_health as tm_health
except Exception:
    tm_health = None  # type: ignore

# v4.13.56: smart AI router. Filters providers based on call type
# (holdings/lookup/scan) before iteration. If absent, falls back to
# the v4.13.55b cooldown-only behavior.
try:
    import tm_ai_router as tm_router
except Exception:
    tm_router = None  # type: ignore


# ─── v4.14.5.62-parallel-consensus: dispatch-mode module gate ──────────
#
# When OFF (default) the consensus canonical-model loop dispatches
# SEQUENTIALLY — byte-identical to pre-patch behavior (same votes, same
# order, same result, same timing). When ON, the loop fans the per-
# canonical-model work across a bounded thread pool so total latency ≈ the
# slowest single model instead of the SUM. Safe because the unit of work
# only touches now-concurrency-safe shared state: the rate limiter
# (reserve-under-lock), RouterRun (RLock; distinct per-model keys anyway),
# and provider health (its own lock). Votes are collected and re-assembled
# in canonical-model order so the result — including _finalize's first-
# winning-vote verdict_target — is identical to sequential.
#
# Set from cfg['parallel_consensus'] at startup + on Settings save
# (tired_market.App), mirroring the tm_context_builder.set_surface_* gate.
_PARALLEL_CONSENSUS = False
_PARALLEL_CONSENSUS_MAX_WORKERS = 5

# v4.14.6.111 (Item 5): a consensus finalizing with fewer than this many
# COMMITTED live voices is "starved" — a verdict resting on 0-1 voices is
# low-confidence (near-unanimous by construction). FLOOR=2 warns only when ≤1
# voice survives (a 2-voice run is still a real cross-check; cooldowns/timeouts
# routinely leave 2, so a higher floor would warn on legitimate runs). Tunable.
CONSENSUS_STARVE_FLOOR = 2


def format_votes_so_far(votes, weight_map=None, accuracy_enabled=False):
    """v4.14.6.111-streaming: one-line running tally over the votes that have
    landed SO FAR, for the live "consensus running" display on every path. e.g.
    "3 HOLD · 1 BUY". Returns "" when no committed vote has a direction yet.

    Partial-safe by construction: it calls the SAME tm_source_accuracy.
    weighted_tally used at finalize, which needs no full-set normalization (the
    weight_map is per-model constants pre-fetched at runner construction), so a
    tally over a subset of voters is valid — the final verdict is just this tally
    when the run completes. Display-only; never raises."""
    try:
        committed = [v for v in (votes or [])
                     if isinstance(v, dict) and (v.get('direction') or '').strip()]
        if not committed:
            return ""
        counts = None
        try:
            import tm_source_accuracy as _tsa
            tally = _tsa.weighted_tally(
                committed, weight_map, bool(accuracy_enabled))
            counts = tally.get('raw_counts') or None
        except Exception:
            counts = None
        if not counts:
            from collections import Counter
            counts = dict(Counter(
                (v.get('direction') or '').upper() for v in committed))
        if not counts:
            return ""
        # Most-agreed direction first, then alphabetical for stable ordering.
        parts = [f"{n} {d}" for d, n in
                 sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        return " · ".join(parts)
    except Exception:
        return ""


def write_consensus_vote_predictions(plog, ticker, path, votes, consensus_id, *,
                                     source='consensus_vote',
                                     consensus_kind='owned',
                                     skip_model_key=None,
                                     current_price=None, log_fn=None):
    """v4.14.6.111 — shared per-model accuracy writer (owned-position AND
    fresh-buy). For each COMMITTED vote (real direction, not skipped/error) that
    has a RESOLVABLE target+stop, write ONE predictions row attributed to that
    model, carrying its OWN parsed direction/target/stop + the provider trailer,
    tagged `source`, `consensus_id`, and `consensus_kind` ('owned'|'fresh_buy' —
    a discriminator; source stays 'consensus_vote' so accuracy groups uniformly).

    Resolves via the SAME tm_discover.check_outcomes (BUY -> target/stop axis,
    HOLD -> hold axis; every row expires at timeframe_days -> none sit
    forever-open). SKIPS: `skip_model_key` (a representative already written by
    the caller -> no double-count), votes with no usable target+stop
    (unresolvable -> would only pad the open pile), and any (consensus_id, model)
    already present (dedup -> idempotent on re-fire).

    SCORING INDEPENDENCE: these rows are scored purely on price vs the model's
    own target/stop/timeframe. Nothing here references holdings/ownership — a
    fresh-buy call on a ticker the user never bought still records and resolves.
    Returns the count written. Never raises.
    """
    try:
        if plog is None:
            return 0
        import tm_discover as _tmd
        seen = set()
        if skip_model_key:
            seen.add(str(skip_model_key).strip().lower())
        # Dedup vs rows already written for THIS consensus_id (re-fire guard).
        try:
            for r in (plog.get_all() or [])[-500:]:
                if r.get('consensus_id') == consensus_id:
                    seen.add((r.get('model') or '').strip().lower())
        except Exception:
            pass
        n = 0
        for v in (votes or []):
            if not isinstance(v, dict):
                continue
            if v.get('skipped') or v.get('error'):
                continue
            if not (v.get('direction') or '').strip():
                continue
            mkey = (v.get('model') or '').strip().lower()
            if not mkey or mkey in seen:
                continue
            resp = v.get('response') or ''
            if not resp:
                continue
            try:
                pred = _tmd.parse_prediction(resp, ticker,
                                             current_price=current_price)
            except Exception:
                continue
            if pred.get('target') is None or pred.get('stop') is None:
                continue
            _dir = (v.get('direction') or pred.get('direction') or '').upper()
            if _dir:
                pred['direction'] = _dir
            pred['source'] = source
            pred['path'] = path
            pred['consensus_id'] = consensus_id
            pred['consensus_kind'] = consensus_kind
            for k in ('model', 'provider_id', 'provider_preset',
                      'canonical_model', 'actual_provider',
                      'actual_model_string', 'lineup_version'):
                if v.get(k) is not None:
                    pred[k] = v.get(k)
            try:
                plog.append(pred)
                seen.add(mkey)
                n += 1
            except Exception:
                pass
        if n and log_fn:
            try:
                log_fn(f"[consensus-accuracy] {ticker}: recorded {n} per-model "
                       f"{consensus_kind} vote(s) for scoring "
                       f"(consensus_id={consensus_id})", 'muted')
            except Exception:
                pass
        return n
    except Exception:
        return 0


def set_parallel_consensus(enabled: bool, max_workers: int = 5) -> None:
    """Push the consensus dispatch-mode flag into this module. enabled=False
    (default) keeps sequential dispatch. max_workers bounds the pool when
    enabled (clamped to >=1)."""
    global _PARALLEL_CONSENSUS, _PARALLEL_CONSENSUS_MAX_WORKERS
    _PARALLEL_CONSENSUS = bool(enabled)
    try:
        mw = int(max_workers)
    except (TypeError, ValueError):
        mw = 5
    _PARALLEL_CONSENSUS_MAX_WORKERS = mw if mw >= 1 else 1


def parallel_consensus_enabled() -> bool:
    """Read-only accessor (testable)."""
    return _PARALLEL_CONSENSUS


# ─── Prompt builder for owned positions ────────────────────────────────

# We don't use PromptBuilder.build_holding_analysis() here because that one
# emits the Discover-style structured block (DIRECTION: BUY/HOLD/AVOID +
# BUY_ZONE/TARGET/STOP_LOSS/TIMEFRAME/CONFIDENCE). For owned positions we
# want the right question (BUY MORE / HOLD / SELL / TRIM, no AVOID) and
# the right output schema.
#
# This function takes the SAME holding dict the existing PromptBuilder
# takes and produces a new prompt. It also calls the existing PromptBuilder
# to get cached technicals/news/market data baked in, so we don't duplicate
# all that fetching logic.

OWNED_POSITION_OUTPUT_SCHEMA = """
At the END of your response, include this structured summary in this EXACT format
(use these labels verbatim so the app can parse them):

DIRECTION: HOLD  (or BUY MORE, or SELL, or TRIM)
TARGET: $X.XX  (price level you'd like to see; "now" if SELL)
STOP_LOSS: $X.XX  (price where the thesis breaks; omit only for SELL)
TIMEFRAME: N days  (or N weeks; how long the thesis takes to play out)
CONFIDENCE: LOW  (or MODERATE or HIGH; be honest, most calls are LOW or MODERATE)
REASON_ONE_LINE: A single sentence summary of WHY this call.

Output the labels EXACTLY as shown — no markdown bold, no asterisks, no
quotes. The app parses these lines literally.

CRITICAL DECISION FRAMING — READ CAREFULLY:

You are evaluating the STOCK at the CURRENT PRICE. Then you map your
stock signal onto the user's existing position size:

  - Stock signal "BUY at this price" → for the user, that's BUY MORE
    (they should add to their existing position because the stock is still
    attractive at today's price)
  - Stock signal "still expect upside, but price is near the target" → for
    the user, that's HOLD (you still expect it to rise, but it's too close to
    the target to justify the added risk of buying more — keep, don't add)
  - Stock signal "SELL / overvalued / thesis broken" → for the user, that's
    SELL (full exit) or TRIM (partial exit if they want to keep some
    exposure)

DO NOT default to HOLD just because the user already owns the stock. If you
would recommend BUYING this stock to a fresh investor at the current
price, you must say BUY MORE — same call, just framed for an existing
holder. The fact that the user is already in the position is context for
sizing and timing, NOT a reason to soften your call.

The choice is among BUY MORE / HOLD / SELL / TRIM. Do not say AVOID —
they already own the stock; AVOID is meaningless here. HOLD is "still
bullish but near target" — NOT neutral: if there's real room to the
target it's BUY MORE; if the thesis is broken or it's past target, SELL
or TRIM.
""".strip()


def build_owned_position_prompt(holding: dict, path_key: str,
                                  prompt_builder, predictions_log=None) -> tuple[str, dict]:
    """Build the consensus-runner prompt for an owned position.

    Strategy: call the existing PromptBuilder.build_holding_analysis to
    get the position+technicals+news header, then strip its trailing
    Discover-style schema and replace with the owned-position schema.

    This keeps all the data-gathering logic in one place (PromptBuilder)
    and just rewrites the question + output schema.
    """
    # v4.14.5.68-tier2-deep-news: on-demand body prefetch for this
    # tier-2 vote. Cache-first (re-fetches only when fresh bodies are
    # below the cap); writes successful bodies back to news_cache so a
    # repeat tier-2 pass within the TTL is a pure cache read. NEVER
    # raises; never blocks the vote — a failed prefetch degrades to
    # the headline-only payload the prompt builder already produces.
    try:
        import tm_news_bodies as _nb
        _app = getattr(prompt_builder, 'app', None)
        _nb.prefetch_bodies_for_tier2(_app, holding.get('ticker', ''))
    except Exception:
        pass

    # Get the full prompt from the existing builder
    prompt, debug = prompt_builder.build_holding_analysis(holding, path_key)

    # Strip everything from "QUESTION:" onward — that's what we replace.
    q_idx = prompt.find('\nQUESTION:\n')
    if q_idx == -1:
        # Fallback: just append the new schema. The existing question
        # is wrong-shaped but at least the data is there.
        new_prompt = prompt + '\n\n' + OWNED_POSITION_OUTPUT_SCHEMA
    else:
        prompt_head = prompt[:q_idx]
        # New question framing: ask first about the STOCK at current price,
        # then translate to a position-aware action. This decouples the
        # stock signal from the "user already owns it" context, which
        # otherwise pushes models to default-HOLD even when they'd recommend
        # buying the stock to a fresh investor.
        new_question = (
            "\nQUESTION:\n"
            "  First, evaluate THE STOCK at its current price. Is it "
            "attractive to buy right now (BUY MORE), fairly valued / "
            "wait-and-see (HOLD), or overvalued / thesis broken (SELL "
            "or TRIM)? Reference the data above where relevant. Don't "
            "make up numbers; say 'insufficient data' if that's the "
            "honest answer.\n\n"
            "  Then translate your call into the user's specific situation: "
            "they already hold 4 shares (or whatever the position info "
            "above says) at their cost basis. Your final DIRECTION should "
            "be the same call you'd give a fresh buyer, just framed as "
            "an action for an existing holder.\n\n"
        )
        new_prompt = prompt_head + new_question + OWNED_POSITION_OUTPUT_SCHEMA

    debug = dict(debug)
    debug['prompt_kind'] = 'owned_position'
    debug['prompt_chars'] = len(new_prompt)
    return new_prompt, debug


# ─── Response parser ───────────────────────────────────────────────────

_DIRECTION_TOKENS = ('BUY MORE', 'BUYMORE', 'BUY', 'HOLD', 'SELL', 'TRIM')

# Strip markdown decoration that models add to field labels:
#   **DIRECTION:** HOLD     → DIRECTION: HOLD
#   - **DIRECTION**: HOLD   → DIRECTION: HOLD
#   ## DIRECTION: HOLD      → DIRECTION: HOLD
#   `DIRECTION`: HOLD       → DIRECTION: HOLD
_LABEL_DECORATION = re.compile(r'^[\s>*\-#`]+|[\s*`]+$')


def _normalize_direction(value: str) -> str:
    """Map a raw direction string to one of the canonical tokens, or ''."""
    v = re.sub(r'\s+', ' ', value.upper().strip())
    # Trim trailing punctuation, parens, etc.
    v = re.sub(r'[.,;:()\[\]"\'].*$', '', v).strip()
    for token in _DIRECTION_TOKENS:
        if v.startswith(token):
            return 'BUY MORE' if token in ('BUY MORE', 'BUYMORE') else token
    return ''


def parse_owned_position_response(response: str) -> dict:
    """Extract the structured fields from a model's full response.

    Strategy:
        1. Walk lines, normalize markdown decoration, look for KEY: VALUE.
        2. If no DIRECTION found via labeled fields, fall back to scanning
           for direction words near "recommend" / "call" / "verdict" /
           the start of the response.

    Returns a dict with whichever fields were found:
        {direction, target, stop_loss, timeframe, confidence,
         reason_one_line}
    """
    out: dict = {}
    if not response:
        return out

    lines = response.splitlines()
    for raw_line in lines:
        # Strip markdown decoration around the WHOLE line so things like
        # "**DIRECTION:** HOLD" become "DIRECTION: HOLD" before we match.
        line = _LABEL_DECORATION.sub('', raw_line.strip())
        # Also strip wrapping bold/italic on the key only:
        # "DIRECTION:** HOLD" → "DIRECTION: HOLD"
        # We'll let the regex handle the rest.
        m = re.match(
            r'^[\s*_`>#-]*([A-Za-z_][A-Za-z_ ]*?)[\s*_`]*\s*:\s*(.+?)\s*$',
            line)
        if not m:
            continue
        key_raw, value = m.group(1), m.group(2).strip()
        # Normalize the key: uppercase, single underscores
        key = re.sub(r'\s+', '_', key_raw.strip().upper())
        # Strip markdown bold/italic and trailing comment/arrow markers
        # from the value:
        #   **HOLD**           → HOLD
        #   HOLD ← (or BUY)    → HOLD
        #   `HOLD`             → HOLD
        value = re.sub(r'^[*_`]+|[*_`]+$', '', value).strip()
        value = re.sub(r'\s*[←<→]+.*$', '', value).strip()
        # Strip trailing parenthetical comments — models echo back the
        # schema's hint comments verbatim, like:
        #   "$48.00  (price level you would like to see)"  →  "$48.00"
        #   "HOLD (or BUY MORE, or SELL)"                  →  "HOLD"
        # Be aggressive: any trailing `(...)` group is treated as a comment.
        # Loop until no more trailing parens, in case models nest them.
        for _ in range(3):
            new_value = re.sub(r'\s*\([^)]*\)\s*$', '', value).strip()
            if new_value == value:
                break
            value = new_value
        # Strip surrounding quotes
        value = value.strip('"\'')
        if not value:
            continue

        if key == 'DIRECTION':
            d = _normalize_direction(value)
            if d:
                out['direction'] = d
        elif key == 'TARGET':
            out['target'] = value
        elif key in ('STOP_LOSS', 'STOP', 'STOPLOSS'):
            out['stop_loss'] = value
        elif key == 'TIMEFRAME':
            out['timeframe'] = value
        elif key == 'CONFIDENCE':
            v_upper = value.upper().split()[0] if value else ''
            v_upper = re.sub(r'[^A-Z]', '', v_upper)
            if v_upper in ('LOW', 'MODERATE', 'MED', 'MEDIUM', 'HIGH'):
                # Normalize MED/MEDIUM to MODERATE for consistency
                out['confidence'] = ('MODERATE'
                                      if v_upper in ('MED', 'MEDIUM')
                                      else v_upper)
        elif key in ('REASON_ONE_LINE', 'REASON', 'WHY'):
            out['reason_one_line'] = value

    # ── Fallback: prose-only direction extraction ──
    # Some models skip the structured block and just write recommendations
    # in prose ("I'd recommend holding this position..."). Try to rescue
    # them by scanning for direction keywords near recommendation cues.
    if 'direction' not in out:
        out['direction'] = _scan_prose_for_direction(response) or ''
        if not out['direction']:
            del out['direction']

    return out


# Phrases that signal "this is the recommendation" — we scan within
# +/- a few words of these for direction tokens. Order matters: more
# specific first.
_PROSE_CUES = [
    r'\brecommendation\s*[:\-]?\s*',
    r'\b(?:my|the|final)\s+call\s*[:\-]?\s*',
    r'\bverdict\s*[:\-]?\s*',
    r'\bI(?:\'?d| would)\s+(?:say|recommend|suggest)\s*[:\-]?\s*',
    r'\brecommend\s+(?:to\s+)?',
]


def _scan_prose_for_direction(response: str) -> str:
    """Scan the response for direction words attached to recommendation
    cues. Returns a canonical direction token or ''.

    This is a fallback for models that don't emit the structured block.
    Matches both bare verbs (HOLD, SELL) and -ing forms (HOLDING, SELLING).
    """
    if not response:
        return ''
    text = response  # keep original case for spotting "BUY MORE" vs "buy more"

    # Map: search-pattern → canonical direction token
    # Order matters: more specific first (BUY MORE before BUY).
    direction_patterns = [
        (r'\bBUY\s+MORE\b', 'BUY MORE'),
        (r'\bBUYMORE\b', 'BUY MORE'),
        (r'\bTRIM(?:MING|S|MED)?\b', 'TRIM'),
        (r'\bSELL(?:ING|S)?\b', 'SELL'),
        (r'\bHOLD(?:ING|S)?\b', 'HOLD'),
        (r'\bBUY(?:ING|S)?\b', 'BUY'),  # 'BUY' alone, post-BUY-MORE
    ]

    # First try cue-anchored scans
    for cue in _PROSE_CUES:
        for m in re.finditer(cue, text, flags=re.IGNORECASE):
            window = text[m.end():m.end() + 80]
            for pat, token in direction_patterns:
                if re.search(pat, window, flags=re.IGNORECASE):
                    # Only return BUY (without MORE) for fresh-buy framing,
                    # which on owned positions usually means BUY MORE.
                    if token == 'BUY':
                        return 'BUY MORE'
                    return token

    # Last-resort: count direction-token mentions in the response and
    # pick the most-mentioned one. Noisy — only used when nothing else
    # works.
    counts: dict = {}
    for pat, token in direction_patterns:
        n = len(re.findall(pat, text, flags=re.IGNORECASE))
        if n:
            counts[token] = counts.get(token, 0) + n
    if counts:
        # Need a clear winner — at least 2x more than the runner-up,
        # otherwise it's too ambiguous to call.
        sorted_counts = sorted(counts.items(), key=lambda kv: -kv[1])
        if len(sorted_counts) == 1 or sorted_counts[0][1] >= 2 * sorted_counts[1][1]:
            winner = sorted_counts[0][0]
            # Same fresh-buy translation
            return 'BUY MORE' if winner == 'BUY' else winner
    return ''


def format_range_for_card(parsed: dict) -> str:
    """Turn a parsed response into a short price-range string for the card.

    Examples:
        {direction: HOLD, target: $48, stop_loss: $42}  →  '$42–48'
        {direction: BUY MORE, target: $55}              →  'target $55'
        {direction: SELL}                               →  'sell now'
        {direction: TRIM, target: $50}                  →  'trim near $50'
    """
    direction = parsed.get('direction', '').upper()
    target = parsed.get('target', '').strip()
    stop = parsed.get('stop_loss', '').strip()

    if direction == 'SELL':
        return 'sell now'
    if direction == 'TRIM':
        return f'trim near {target}' if target else 'trim'
    if direction == 'BUY MORE':
        return f'target {target}' if target else 'buy more'
    # HOLD or unknown — try to show a range
    if target and stop:
        return f'{stop} – {target}'
    if target:
        return f'target {target}'
    return ''


# ─── Fresh-buy (Recommend) prompt + parser ──────────────────────────────
#
# This is the SAME shape as the owned-position pipeline but framed for
# stocks the user DOESN'T own yet. Used by the Recommend window's per-row
# Run consensus button.
#
# The key difference: directions are BUY / HOLD / AVOID — not BUY MORE /
# HOLD / SELL / TRIM. Position-management actions don't apply when you
# don't have a position.

FRESH_BUY_OUTPUT_SCHEMA = """
At the END of your response, include this structured summary in this EXACT format
(use these labels verbatim so the app can parse them):

DIRECTION: SUPPORT  (or NEUTRAL, or OPPOSE)
BUY_ZONE: $X.XX - $Y.YY  (entry price range; from Layer 1 unless you'd revise it)
TARGET: $X.XX  (price level for take-profit; from Layer 1 unless you'd revise it)
STOP_LOSS: $X.XX  (price where the thesis breaks; from Layer 1 unless you'd revise it)
TIMEFRAME: N days  (or N weeks; how long the thesis takes to play out)
CONFIDENCE: LOW  (or MODERATE or HIGH; be honest, most theses warrant MODERATE)
REASON_ONE_LINE: A single sentence summary of WHY this call.

Output the labels EXACTLY as shown — no markdown bold, no asterisks, no
quotes. The app parses these lines literally.

DECISION FRAMING (v4.14.6.75-consensus-momentum-prompt):

STRATEGY CONTEXT — read this first. Layer 1 is a MOMENTUM / breakout
strategy. It INTENTIONALLY buys strength: a high RSI, price near or
breaking recent highs, price above its moving averages, and positive
recent momentum are the INTENDED entry SIGNALS — not risks. "The stock
has already risen" is EXPECTED here; that IS the thesis, not a reason to
wait. You are validating a MOMENTUM-CONTINUATION / breakout entry, NOT a
mean-reversion or buy-the-dip entry. So do NOT down-rate a pick merely
because it looks "extended," sits near its highs, has a high RSI, or has
already moved up — those are the setup working as designed. Judge whether
the UPTREND / breakout is REAL and INTACT, not whether the price is "cheap"
or due for a pullback.

You are NOT being asked whether you would initiate a position from scratch
right now. Layer 1 has already identified this as a BUY with the entry/
target/stop shown above. Your job is to second-opinion that specific
MOMENTUM thesis with the current data:

  - SUPPORT: the momentum thesis holds up — the uptrend / breakout is real
    and intact (price action, trend structure, and volume are consistent
    with continuation) and there is no disqualifying weakness (see OPPOSE).
    The entry/stop framework is reasonable and the trade DIRECTION is sound.
    SUPPORT does NOT mean "I would buy from scratch," and it does NOT mean
    "it went up so buy" — it means "this is a valid momentum entry that
    holds up to scrutiny."
  - NEUTRAL: the MOMENTUM thesis itself is genuinely mixed — momentum is
    stalling or rolling over, volume is NOT confirming the move (price
    rising on weak or declining volume), or the signals conflict (price up
    while MACD / OBV diverge down). "It has already risen / RSI is high / it
    looks extended" is NOT by itself a reason for NEUTRAL on a momentum
    strategy — only call NEUTRAL when the CONTINUATION case is actually in
    doubt.
  - OPPOSE: the thesis is structurally broken or the name has genuine
    weakness — deteriorating fundamentals (net losses, declining revenue),
    bearish confirmation (e.g. MACD AND OBV both negative, clear
    distribution / heavy selling), a momentum move with no support that is
    clearly rolling over, the stop-loss math doesn't work, or a recent
    development (earnings miss, regulatory issue) invalidates the setup. The
    user should NOT act on this call as-is.

TARGET NOTE: judge the DIRECTION and the SETUP, not whether the exact
target price is precisely achievable. Layer 1's target is an optimistic,
ATR-based stretch ceiling; a target that looks ambitious is NOT grounds to
oppose or down-rate a pick whose entry and direction are sound. Ask "is
this a valid momentum entry with favorable risk/reward DIRECTION?", not
"will it hit that exact price?"

If the data is genuinely insufficient to second-opinion the thesis, say
NEUTRAL with LOW confidence and explain what you'd need to see.

Backward-compat: if you find yourself wanting to answer BUY / WATCH /
AVOID instead, those map to SUPPORT / NEUTRAL / OPPOSE respectively.
The app accepts either vocabulary.
""".strip()


# v4.14.5.26-lookup-explain: NET-NEW schema for the Look Up surface ONLY.
# Deliberately SEPARATE from FRESH_BUY_OUTPUT_SCHEMA (which is shared by the
# Layer 2 daemon / Recommend Verify / owned-position and encodes the hard-won
# thesis-validation cascade — must not change). Look Up is EXPLORATORY ("tell
# me about this ticker"), not thesis-validation, so this asks for a rich
# free-text ANALYSIS block (bull/bear/risks/synthesis) in addition to the SAME
# structured verdict labels (verbatim, so parse_fresh_buy_response's field
# extraction + _normalize_fresh_buy_direction still apply unchanged — the
# verdict parses to the same BUY/WATCH/AVOID space). The "no prose" rule is
# scoped to the LABELS block only; the ANALYSIS block is free prose. Mind
# provider context limits — this prompt is longer; on a small-context provider
# (e.g. Cerebras ~8K) a very long ANALYSIS could crowd the labels, but Look Up
# fans out across providers so other voters still return clean structured
# fields; the verdict parse degrades gracefully (prose-scan fallback).
LOOKUP_EXPLAIN_OUTPUT_SCHEMA = """
Write your answer in TWO parts.

PART 1 — ANALYSIS (free prose; normal formatting / bullets are fine):
  ANALYSIS:
    - BULL CASE: the strongest reasons this could be a good buy now.
    - BEAR CASE: the strongest reasons to be cautious or to avoid it.
    - KEY RISKS: specific risks (catalyst, valuation, sector, recent news)
      that could break the thesis.
    - SYNTHESIS: weigh the above against the price / technicals / news /
      fundamentals shown and reach a clear view. Don't make up numbers; if
      data is missing, say so.

PART 2 — STRUCTURED SUMMARY (at the very END, these labels EXACTLY — no
markdown bold, no asterisks, no quotes; the app parses these lines literally):

DIRECTION: BUY  (or WATCH, or AVOID)
BUY_ZONE: $X.XX - $Y.YY  (a sensible entry price range)
TARGET: $X.XX  (take-profit level)
STOP_LOSS: $X.XX  (price where the thesis breaks)
TIMEFRAME: N days  (or N weeks)
CONFIDENCE: LOW  (or MODERATE or HIGH; be honest, most warrant MODERATE)
REASON_ONE_LINE: A single sentence summary of WHY this call.

Only the STRUCTURED SUMMARY labels must be verbatim; the ANALYSIS section
above is free prose. (BUY / WATCH / AVOID map to SUPPORT / NEUTRAL / OPPOSE;
either vocabulary is accepted.)
""".strip()


def build_fresh_buy_prompt(holding: dict, path_key: str,
                            prompt_builder, predictions_log=None) -> tuple[str, dict]:
    """Build the consensus-runner prompt for a stock the user doesn't own.

    Used by the Recommend window's per-row Run consensus button.

    Note: signature mirrors build_owned_position_prompt so ConsensusRunner
    can call either one with the same args. The 'holding' dict here only
    needs {'ticker': X} — shares/cost basis are ignored.

    Strategy: like the owned-position version, call the existing
    PromptBuilder.build_holding_analysis to get all the technicals/news/
    quote data. Then strip the position-aware section and the trailing
    QUESTION + schema, replace with fresh-buy framing. The current quote
    info that lived inside POSITION gets re-extracted into a CURRENT
    QUOTE section so the model still sees today's price.
    """
    ticker = holding.get('ticker', '?').upper()

    # Synthetic holding so build_holding_analysis still works. shares=0
    # makes the cost-basis lines harmless ($0/share, $0 total). We strip
    # the POSITION block anyway and re-build a CURRENT QUOTE block from
    # the cache directly.
    synthetic = dict(holding)
    synthetic.setdefault('shares', 0)
    synthetic.setdefault('buy_price', 0)
    synthetic.setdefault('total_cost', 0)
    synthetic.setdefault('tradable', True)

    # v4.14.5.68-tier2-deep-news: on-demand body prefetch for the
    # fresh-buy tier-2 vote path (same wire as build_owned_position_prompt).
    # Cache-first; writes back to news_cache; failures degrade to
    # headlines. NEVER raises; never blocks the vote.
    try:
        import tm_news_bodies as _nb
        _app = getattr(prompt_builder, 'app', None)
        _nb.prefetch_bodies_for_tier2(_app, ticker)
    except Exception:
        pass

    # Get the full prompt from the existing builder. It includes quote,
    # technicals, news, market status — all the context we need.
    prompt, debug = prompt_builder.build_holding_analysis(synthetic, path_key)

    # Pull the current price/change/volume from the cache directly so we
    # can build a CURRENT QUOTE section that doesn't pretend the user owns
    # any shares.
    cache = getattr(prompt_builder, 'cache', None)
    quote_section = ''
    if cache is not None:
        try:
            quote = cache.quote(ticker) or {}
        except Exception:
            quote = {}
        price = quote.get('price')
        if price:
            change = quote.get('change_pct', 0) or 0
            volume = quote.get('volume', 0) or 0
            ql = ['CURRENT QUOTE:']
            ql.append(f"  Ticker: {ticker}")
            ql.append(f"  Price: ${price:g}")
            sign = '+' if change >= 0 else ''
            ql.append(f"  Day change: {sign}{change:.2f}%")
            if volume:
                ql.append(f"  Volume: {volume:,.0f} shares")
            ql.append('')  # blank line between sections
            quote_section = '\n'.join(ql) + '\n'

    # Strip the POSITION block (irrelevant for unowned stocks).
    pos_idx = prompt.find('\nPOSITION:\n')
    if pos_idx != -1:
        # Find end of POSITION block (next double-newline)
        end_idx = prompt.find('\n\n', pos_idx + 1)
        if end_idx != -1:
            # Replace POSITION block with our CURRENT QUOTE section.
            # +2 to skip over the trailing \n\n.
            prompt = prompt[:pos_idx + 1] + quote_section + prompt[end_idx + 2:]

    # v4.14.5.14-layer2-thesis-validation (2026-05-20): build a
    # Layer-1 thesis block from the most-recent open BUY in
    # predictions_log for this (ticker, path). Anchors the model on
    # Layer 1's specific call rather than asking it to re-derive from
    # zero. The pre-thesis prompt's "would you BUY this from scratch?"
    # framing structurally biased verdicts toward non-BUY (it literally
    # told the model "high AVOID rate is appropriate") and produced
    # the 95% CONTRADICTED rate that drove the three-patch
    # consumer-side filter cascade.
    _layer1 = _layer1_thesis_for(ticker, path_key, predictions_log)
    if _layer1 is None:
        # No Layer 1 thesis to validate — fall back to a softened
        # framing that asks for SUPPORT/NEUTRAL/OPPOSE without an
        # anchor. Better than dropping the validation entirely, and
        # the caller (Layer 2 daemon) can't know whether a thesis
        # exists. The schema's backward-compat clause means models
        # answering BUY/WATCH/AVOID still parse correctly.
        thesis_block = (
            "\nLAYER 1 THESIS:\n"
            "  (Layer 1 thesis details unavailable — second-opinion "
            "the pick on its merits.)\n"
        )
    else:
        _entry = _layer1.get('entry') or _layer1.get('buy_zone') or '?'
        _target = _layer1.get('target') or '?'
        _stop = _layer1.get('stop') or _layer1.get('stop_loss') or '?'
        # path framing from tm_holdings.PATHS[path]['description']
        # — single source of truth, already maintained.
        _path_desc = ''
        try:
            import tm_holdings as _th_l1
            _pi = _th_l1.PATHS.get(path_key) or {}
            _path_desc = (_pi.get('description') or '').strip()
        except Exception:
            _path_desc = ''
        _ts_layer1 = (_layer1.get('timestamp') or '')[:10]
        thesis_block = (
            f"\nLAYER 1 THESIS:\n"
            f"  Layer 1 (initial analyzer) called {ticker} a BUY"
            f"{' on ' + _ts_layer1 if _ts_layer1 else ''}.\n"
            f"  Entry: {_entry}\n"
            f"  Target: {_target}\n"
            f"  Stop: {_stop}\n"
            f"  Path: {path_key}"
            f"{(' — ' + _path_desc) if _path_desc else ''}\n"
        )

    # Replace QUESTION + tail with thesis-validation framing.
    q_idx = prompt.find('\nQUESTION:\n')
    if q_idx == -1:
        new_prompt = (prompt + '\n' + thesis_block + '\n'
                      + FRESH_BUY_OUTPUT_SCHEMA)
    else:
        prompt_head = prompt[:q_idx]
        new_question = (
            f"\nQUESTION:\n"
            f"  Second-opinion the Layer 1 BUY thesis on {ticker} "
            f"above. You are NOT being asked whether you would "
            f"initiate a new position from scratch — you are "
            f"validating whether Layer 1's specific call (with the "
            f"entry/target/stop shown) remains defensible given the "
            f"current data. Answer SUPPORT / NEUTRAL / OPPOSE with "
            f"reasoning. Don't make up numbers; if the data is "
            f"insufficient to validate, say NEUTRAL honestly. "
            f"(Models may answer BUY / WATCH / AVOID equivalently; "
            f"both vocabularies are accepted.)\n\n"
        )
        new_prompt = (prompt_head + thesis_block + new_question
                      + FRESH_BUY_OUTPUT_SCHEMA)

    debug = dict(debug)
    debug['prompt_kind'] = 'fresh_buy'  # kind-string kept stable;
    # the semantic change is in the prompt content, not the label.
    # See Phase 0 rationale: renaming would force a cross-module
    # rename + a file-mutation migration on signals.jsonl for no
    # functional gain (downstream readers ignore the literal label
    # — they fetch entries by `kind` equality). The label is now
    # semantically inaccurate but operationally lower-risk than the
    # rename alternative.
    debug['prompt_chars'] = len(new_prompt)
    debug['thesis_validation'] = (_layer1 is not None)
    return new_prompt, debug


def _layer1_thesis_for(ticker: str, path_key: str,
                        predictions_log) -> Optional[dict]:
    """v4.14.5.14-layer2-thesis-validation: look up Layer 1's most
    recent open BUY prediction for (ticker, path) so the consensus
    prompt can second-opinion the specific thesis instead of asking
    a fresh-start question. Returns None if no current Layer 1 BUY
    exists — caller falls back to softened framing. Tolerant of
    missing/legacy predictions_log shapes; never raises."""
    if predictions_log is None:
        return None
    try:
        rec = predictions_log.get_most_recent_for_ticker_and_path(
            ticker, path_key)
    except Exception:
        return None
    if not rec:
        return None
    if (rec.get('direction') or '').upper() != 'BUY':
        return None
    if rec.get('status') not in (None, '', 'open'):
        return None
    return rec


# Direction tokens for the consensus path. The downstream verdict
# space is BUY / WATCH / AVOID — that contract is unchanged.
#
# v4.14.2 stage 4 added WATCH as the candidate-vocabulary
# replacement for HOLD (HOLD stays in the table for backward-compat
# normalization).
# v4.14.5.14-layer2-thesis-validation (2026-05-20) adds SUPPORT /
# NEUTRAL / OPPOSE as the thesis-validation vocabulary. They map
# semantically to BUY / WATCH / AVOID — same downstream behaviour,
# just the question being asked is different. Order matters: BUY
# MORE must check before BUY (longer prefix wins).
_FRESH_BUY_DIRECTION_TOKENS = (
    'SUPPORT', 'NEUTRAL', 'OPPOSE',  # thesis-validation vocabulary
    'BUY', 'AVOID', 'WATCH', 'HOLD',  # legacy / backward-compat
)


def _normalize_fresh_buy_direction(value: str) -> str:
    """Map a raw direction string to one of BUY / WATCH / AVOID, or ''.

    Accepts BOTH:
      - The v4.14.5.14-layer2-thesis-validation vocabulary
        (SUPPORT / NEUTRAL / OPPOSE) — the question Layer 2 asks
        post-2026-05-20 — mapped to BUY / WATCH / AVOID
        respectively for downstream compat.
      - The legacy fresh-buy vocabulary (BUY / WATCH / AVOID / HOLD)
        — for backward-compat with stored predictions and for models
        that habitually emit BUY/WATCH/AVOID even when asked for
        SUPPORT/NEUTRAL/OPPOSE.

    HOLD is normalized to WATCH (same v4.14.2 collapse). Anything
    unrecognized returns ''.
    """
    v = re.sub(r'\s+', ' ', value.upper().strip())
    v = re.sub(r'[.,;:()\[\]"\'].*$', '', v).strip()
    # If model accidentally said "BUY MORE" (it shouldn't, but let's be
    # forgiving), treat that as BUY.
    if v.startswith('BUY MORE') or v.startswith('BUYMORE'):
        return 'BUY'
    for token in _FRESH_BUY_DIRECTION_TOKENS:
        if v.startswith(token):
            # v4.14.5.14-layer2-thesis-validation: map thesis-
            # validation vocabulary to legacy directions for
            # downstream compat.
            if token == 'SUPPORT':
                return 'BUY'
            if token == 'NEUTRAL':
                return 'WATCH'
            if token == 'OPPOSE':
                return 'AVOID'
            # v4.14.2 stage 4: collapse legacy HOLD -> WATCH at the
            # candidate-vocabulary boundary.
            if token == 'HOLD':
                return 'WATCH'
            return token
    return ''


def parse_fresh_buy_response(response: str) -> dict:
    """Extract the structured fields from a fresh-buy model response.

    Same approach as parse_owned_position_response, but the direction
    domain is BUY / HOLD / AVOID and we capture BUY_ZONE.
    """
    out: dict = {}
    if not response:
        return out

    lines = response.splitlines()
    for raw_line in lines:
        line = _LABEL_DECORATION.sub('', raw_line.strip())
        m = re.match(
            r'^[\s*_`>#-]*([A-Za-z_][A-Za-z_ ]*?)[\s*_`]*\s*:\s*(.+?)\s*$',
            line)
        if not m:
            continue
        key_raw, value = m.group(1), m.group(2).strip()
        key = re.sub(r'\s+', '_', key_raw.strip().upper())
        value = re.sub(r'^[*_`]+|[*_`]+$', '', value).strip()
        value = re.sub(r'\s*[←<→]+.*$', '', value).strip()
        for _ in range(3):
            new_value = re.sub(r'\s*\([^)]*\)\s*$', '', value).strip()
            if new_value == value:
                break
            value = new_value
        value = value.strip('"\'')
        if not value:
            continue

        if key == 'DIRECTION':
            d = _normalize_fresh_buy_direction(value)
            if d:
                out['direction'] = d
        elif key == 'BUY_ZONE' or key == 'BUYZONE' or key == 'ENTRY':
            out['buy_zone'] = value
        elif key == 'TARGET':
            out['target'] = value
        elif key in ('STOP_LOSS', 'STOP', 'STOPLOSS'):
            out['stop_loss'] = value
        elif key == 'TIMEFRAME':
            out['timeframe'] = value
        elif key == 'CONFIDENCE':
            v_upper = value.upper().split()[0] if value else ''
            v_upper = re.sub(r'[^A-Z]', '', v_upper)
            if v_upper in ('LOW', 'MODERATE', 'MED', 'MEDIUM', 'HIGH'):
                out['confidence'] = ('MODERATE'
                                      if v_upper in ('MED', 'MEDIUM')
                                      else v_upper)
        elif key in ('REASON_ONE_LINE', 'REASON', 'WHY'):
            out['reason_one_line'] = value

    # Fallback: scan prose if no DIRECTION was found in structured form.
    # We use a different prose scanner since the direction domain differs.
    if 'direction' not in out:
        d = _scan_prose_for_fresh_buy_direction(response)
        if d:
            out['direction'] = d

    return out


def build_lookup_explain_prompt(holding: dict, path_key: str,
                                prompt_builder,
                                predictions_log=None) -> tuple[str, dict]:
    """v4.14.5.26-lookup-explain: NET-NEW Look Up prompt. SEPARATE from
    build_fresh_buy_prompt (which is shared by Layer 2 / Verify / owned-position
    and must not change). Same input-gathering (the existing PromptBuilder
    data/quote/technicals/news block) but an EXPLORATORY question + the
    analysis-rich LOOKUP_EXPLAIN_OUTPUT_SCHEMA instead of thesis-validation —
    Look Up on a user-typed ticker usually has NO Layer 1 thesis to validate, so
    asking 'analyze this ticker' is the right question. `predictions_log` is
    accepted for signature parity with the other builders (unused here — no
    thesis block). The verdict labels match fresh_buy's, so the verdict still
    parses identically; only the ANALYSIS prose is additive."""
    ticker = holding.get('ticker', '?').upper()

    synthetic = dict(holding)
    synthetic.setdefault('shares', 0)
    synthetic.setdefault('buy_price', 0)
    synthetic.setdefault('total_cost', 0)
    synthetic.setdefault('tradable', True)

    prompt, debug = prompt_builder.build_holding_analysis(synthetic, path_key)

    # v4.14.6.111-lookup-strip-strategy: Look Up evaluates the NAMED ticker on
    # its OWN merits — it must NOT carry the user's price-band / trading-style
    # strategy. The shared body builder prepends a "USER'S CHOSEN PATH: $5–$10
    # (Stocks priced $5–$10/share…)" block (tm_holdings.build_holding_analysis),
    # which made models reject out-of-range tickers ("outside the $5–$10 range")
    # instead of judging them. Strip that block from the LOOK UP body ONLY — the
    # same kind of string-surgery this builder already does for POSITION/QUESTION.
    # build_holding_analysis itself is left intact, so Recommend/scan/Verify/
    # Layer-2 (build_fresh_buy_prompt) keep their legitimate band-awareness.
    _cp = prompt.find("USER'S CHOSEN PATH:")
    if _cp != -1:
        _pp = prompt.find("\nPOSITION:", _cp)
        if _pp != -1:
            # drop the path lines + their trailing blank, leaving the existing
            # blank separator before POSITION (no dangling label / no doubled gap)
            prompt = prompt[:_cp] + prompt[_pp + 1:]
    # Neutralise the "analyzing a position for <user>" ownership framing — Look
    # Up is a fresh evaluation, not a held position. (Literal, user-name-agnostic.)
    prompt = prompt.replace("You are analyzing a position for ",
                            "You are analyzing this ticker for ", 1)

    # Rebuild a CURRENT QUOTE section (same logic as the fresh-buy builder —
    # duplicated deliberately to keep the shared fresh_buy path untouched).
    cache = getattr(prompt_builder, 'cache', None)
    quote_section = ''
    if cache is not None:
        try:
            quote = cache.quote(ticker) or {}
        except Exception:
            quote = {}
        price = quote.get('price')
        if price:
            change = quote.get('change_pct', 0) or 0
            volume = quote.get('volume', 0) or 0
            ql = ['CURRENT QUOTE:']
            ql.append(f"  Ticker: {ticker}")
            ql.append(f"  Price: ${price:g}")
            sign = '+' if change >= 0 else ''
            ql.append(f"  Day change: {sign}{change:.2f}%")
            if volume:
                ql.append(f"  Volume: {volume:,.0f} shares")
            ql.append('')
            quote_section = '\n'.join(ql) + '\n'

    pos_idx = prompt.find('\nPOSITION:\n')
    if pos_idx != -1:
        end_idx = prompt.find('\n\n', pos_idx + 1)
        if end_idx != -1:
            prompt = prompt[:pos_idx + 1] + quote_section + prompt[end_idx + 2:]

    # Replace QUESTION + tail with EXPLORATORY analysis framing (NO thesis
    # block — Look Up is exploratory, not validation).
    new_question = (
        f"\nQUESTION:\n"
        f"  Analyze {ticker} for someone who just looked it up. This is an "
        f"EXPLORATORY analysis — give a clear, substantive read on whether "
        f"it's worth buying now, grounded in the price / technicals / news / "
        f"fundamentals shown above. Make the case both ways before you "
        f"conclude.\n\n")
    q_idx = prompt.find('\nQUESTION:\n')
    if q_idx == -1:
        new_prompt = prompt + '\n' + new_question + LOOKUP_EXPLAIN_OUTPUT_SCHEMA
    else:
        new_prompt = prompt[:q_idx] + new_question + LOOKUP_EXPLAIN_OUTPUT_SCHEMA

    # Context-limit note: this prompt is longer than fresh_buy (the analysis
    # request adds framing). On a small-context provider (e.g. Cerebras ~8K)
    # a very long input could crowd the structured labels — but Look Up fans
    # out across providers, so other voters still return clean fields, and the
    # parser's prose-scan fallback recovers the direction. Degrades gracefully;
    # no hard truncation guard needed at Look Up's low volume.
    debug = dict(debug)
    debug['prompt_kind'] = 'lookup_explain'
    debug['prompt_chars'] = len(new_prompt)
    return new_prompt, debug


def parse_lookup_explain_response(response: str) -> dict:
    """v4.14.5.26-lookup-explain: parse a Look Up response. ADDITIVE over
    parse_fresh_buy_response — the structured verdict fields (direction/target/
    stop/timeframe/confidence/reason_one_line) are extracted by the EXISTING
    parser unchanged (so the verdict parses to the same space), then the new
    free-text ANALYSIS block is captured into `analysis_text`. Absence of an
    ANALYSIS block (any non-lookup_explain response) → no `analysis_text` key,
    a clean no-op for every other consumer. Never raises."""
    out = parse_fresh_buy_response(response)
    if not response:
        return out
    try:
        # Capture text after 'ANALYSIS:' up to the structured DIRECTION label
        # (or end). Falls back to the prose preceding DIRECTION if the model
        # omitted the ANALYSIS header but still wrote prose first.
        m = re.search(
            r'(?is)\bANALYSIS\s*:\s*(.+?)(?=\n\s*DIRECTION\s*:|\Z)', response)
        if m and m.group(1).strip():
            out['analysis_text'] = m.group(1).strip()
        else:
            di = re.search(r'(?im)^\s*DIRECTION\s*:', response)
            if di and di.start() > 60:
                pre = response[:di.start()].strip()
                if pre:
                    out['analysis_text'] = pre
    except Exception:
        pass
    return out


def _scan_prose_for_fresh_buy_direction(response: str) -> str:
    """Fresh-buy prose fallback. Looks for BUY/HOLD/AVOID near
    recommendation cues."""
    if not response:
        return ''
    text = response

    direction_patterns = [
        # v4.14.5.14-layer2-thesis-validation: thesis-validation
        # vocabulary mapped at scan time to the downstream BUY/
        # WATCH/AVOID space. SUPPORT → BUY, NEUTRAL → HOLD (then
        # collapses to WATCH via the normalizer), OPPOSE → AVOID.
        (r'\bSUPPORT(?:S|ING|ED)?\b', 'BUY'),
        (r'\bOPPOSE(?:S|D)?\b', 'AVOID'),
        (r'\bNEUTRAL\b', 'HOLD'),
        # Legacy vocabulary — preserved unchanged.
        (r'\bAVOID(?:ING|S)?\b', 'AVOID'),
        (r'\bSKIP(?:PING|S)?\b', 'AVOID'),  # "skip this" → AVOID
        (r'\bHOLD(?:ING|S)?\b', 'HOLD'),
        (r'\bBUY(?:ING|S)?\b', 'BUY'),
    ]

    for cue in _PROSE_CUES:
        for m in re.finditer(cue, text, flags=re.IGNORECASE):
            window = text[m.end():m.end() + 80]
            for pat, token in direction_patterns:
                if re.search(pat, window, flags=re.IGNORECASE):
                    return token

    counts: dict = {}
    for pat, token in direction_patterns:
        n = len(re.findall(pat, text, flags=re.IGNORECASE))
        if n:
            counts[token] = counts.get(token, 0) + n
    if counts:
        sorted_counts = sorted(counts.items(), key=lambda kv: -kv[1])
        if len(sorted_counts) == 1 or sorted_counts[0][1] >= 2 * sorted_counts[1][1]:
            return sorted_counts[0][0]
    return ''


# ─── The runner ────────────────────────────────────────────────────────

class ConsensusRunner:
    """Sequentially runs N models against an owned-position prompt.

    Threading model:
        - .start() launches one orchestrator thread
        - that thread builds the prompt, then for each model: spins up a
          tm_ai.AIRequest (which has its own internal thread), waits for
          it via Event, collects the response, parses it, fires callbacks
        - cancellation cancels the current AIRequest and breaks the loop

    Callbacks fire from the orchestrator thread, NOT the UI thread. The
    caller is responsible for marshaling to the UI thread (typically via
    root.after(0, ...)).
    """

    PER_MODEL_TIMEOUT_SEC = 180  # 3 minutes per model — plenty for cold-start

    # v4.14.6.111-finalize-deadline: total per-RUN wall-clock cap (from worker
    # spawn) after which the verdict posts on whoever has returned and the rest
    # are marked timed-out. Bounds the user-visible wait so one dead/hung model
    # can't hold the panel for its full PER_MODEL_TIMEOUT_SEC. Chosen 120s:
    # ABOVE the observed ~90s fast-cohort completion (real voters aren't cut),
    # one-third BELOW the 180s per-model cap (a hung model can't wedge the
    # verdict past 2 min vs the prior 3-4). Tunable; tests override it low.
    FINALIZE_DEADLINE_SEC = 120

    def __init__(self,
                 ticker: str,
                 holding: dict,
                 models: list[str],
                 path: str,
                 prompt_builder: Any,
                 signals_log: Any,
                 on_model_start: Optional[Callable[[str], None]] = None,
                 on_model_done: Optional[Callable[[str, dict], None]] = None,
                 on_model_error: Optional[Callable[[str, str], None]] = None,
                 on_all_done: Optional[Callable[[dict], None]] = None,
                 on_late_vote: Optional[Callable[[str, dict], None]] = None,
                 log_callback: Optional[Callable[[str, str], None]] = None,
                 predictions_log: Any = None,
                 prompt_kind: str = 'owned_position',
                 providers: Optional[list] = None,
                 inference_mode: Optional[str] = None,
                 game_processes: Optional[list] = None,
                 call_type: str = 'holdings_consensus',
                 write_consensus_signal: bool = True,
                 weight_map: Optional[dict] = None,
                 accuracy_weighting_enabled: bool = False,
                 providers_bench: Optional[list] = None):
        """Initialize a consensus runner.

        ... (existing docs unchanged) ...

        v4.13.56: `call_type` controls smart routing. Valid values:
            'holdings_consensus' (default) — owned-position refresh.
                All providers eligible, full daily caps used.
            'lookup_fanout' — Look Up "Run full consensus" button.
                All providers eligible but cap_factor=0.7 holds back
                30% of daily quota for higher-value calls.
            'recommend_run' — Recommend consensus.
                cap_factor=0.5 — half the daily cap available.
            'scan' — auto-scan path. Scarce/expensive providers blocked
                (Sambanova, Anthropic, OpenAI), cap_factor=0.3.

        The smart router (tm_ai_router) consumes this value at run-
        time. If the router module isn't loaded, behavior falls back
        to v4.13.55b (cooldowns only, no call-type filtering).
        """
        self.ticker = ticker.upper()
        self.holding = holding
        self.models = list(models)
        self.providers = list(providers or [])  # v4.13.41
        # v4.14.5.69-tier2-backfill: the FULL enabled-provider bench
        # (in priority order, BEFORE any tier-2 narrowing). The
        # selection layer in tm_layer2_validation passes this so the
        # consensus dispatcher can substitute when a picked validator
        # dies mid-run. None / empty → no backfill attempted (legacy
        # behaviour: skip-vote on exhaustion, byte-identical).
        self.providers_bench = list(providers_bench or [])
        # v4.13.43: capture inference mode + game list at construction
        self.inference_mode = (inference_mode or 'hybrid').lower()
        if self.inference_mode not in ('local', 'api', 'hybrid'):
            self.inference_mode = 'hybrid'
        self.game_processes = list(game_processes or [])
        self.path = path
        self.prompt_builder = prompt_builder
        self.signals_log = signals_log
        self.predictions_log = predictions_log
        self.on_model_start = on_model_start
        self.on_model_done = on_model_done
        self.on_model_error = on_model_error
        self.on_all_done = on_all_done
        # v4.14.6.111-finalize-deadline: fired (model, vote) when a timed-out
        # model's vote lands AFTER finalize — for audit only; the caller must
        # NOT use it to change the posted verdict or the scoreboard.
        self.on_late_vote = on_late_vote
        self.log_callback = log_callback
        self.prompt_kind = prompt_kind
        # v4.14.5.14-layer2-decouple (2026-05-20): when False, the
        # rollup `consensus_fresh_buy` / `consensus` signal write at
        # _finalize is skipped. Per-model entries are unaffected
        # (they're not consumed by the dialog gate). Layer 2 daemon
        # passes False so its background validations don't write the
        # signal the Recommend dialog's `_consensus_says_buy` filter
        # reads — preserves Layer 2's diagnostic value (badge from
        # recommend_cache_validation still updates) without the
        # silent-veto side effect that dropped 95% of picks. User-
        # initiated Verify clicks keep the default True so an
        # explicit user action still gates the pick.
        self.write_consensus_signal = bool(write_consensus_signal)

        # v4.14.5.19-accuracy-weighted-consensus: pre-fetched
        # {model_label: weight in [1.0, 9.0]} built by the caller
        # (which holds the DB connection). Runner stays DB-free.
        # Missing entries resolve to NEUTRAL_WEIGHT (5.0) at lookup
        # time. When `accuracy_weighting_enabled` is False or weight_map
        # is None, _finalize uses the flat-tally behavior (byte-
        # identical to pre-v4.14.5.19).
        self.weight_map = (dict(weight_map)
                            if isinstance(weight_map, dict) else None)
        self.accuracy_weighting_enabled = bool(accuracy_weighting_enabled)

        # v4.13.56: smart-router call type. Used to filter the
        # provider list at runtime via tm_ai_router.select_providers.
        valid_call_types = (
            'holdings_consensus', 'lookup_fanout',
            'recommend_run', 'scan')
        self.call_type = (
            call_type if call_type in valid_call_types
            else 'holdings_consensus')

        # Map prompt_kind to the actual prompt builder + parser + signal
        # kind names used when saving. Adding a new prompt_kind in the
        # future means adding one entry here, no changes to the runner
        # core.
        if prompt_kind == 'lookup_explain':
            # v4.14.5.26-lookup-explain: Look Up surface only. NET-NEW prompt
            # + additive parser; reuses the fresh_buy SIGNAL kinds so signal/
            # Track-Record storage is byte-identical to today's Look Up (which
            # used fresh_buy) — no new downstream kind to teach readers about.
            self._prompt_fn = build_lookup_explain_prompt
            self._parse_fn = parse_lookup_explain_response
            self._consensus_kind = 'consensus_fresh_buy'
            self._per_model_kind = 'per_model_fresh_buy'
        elif prompt_kind == 'fresh_buy':
            self._prompt_fn = build_fresh_buy_prompt
            self._parse_fn = parse_fresh_buy_response
            self._consensus_kind = 'consensus_fresh_buy'
            self._per_model_kind = 'per_model_fresh_buy'
        else:
            # Default: owned_position
            self._prompt_fn = build_owned_position_prompt
            self._parse_fn = parse_owned_position_response
            self._consensus_kind = 'consensus'
            self._per_model_kind = 'per_model_owned'

        self._cancel_event = threading.Event()
        self._current_request: Any = None  # AIRequest we're waiting on
        self._thread: Optional[threading.Thread] = None
        # v4.14.5.62-parallel-consensus: serializes log_callback + the
        # on_model_* progress callbacks so they never re-enter concurrently
        # when the canonical-model loop runs on a thread pool. Uncontended
        # (and therefore behavior-identical) under sequential dispatch.
        self._cb_lock = threading.Lock()
        # v4.14.6.111-finalize-deadline: late (post-finalize) votes captured for
        # audit. Guarded by a DEDICATED lock — NOT _cb_lock — so the reverted UI
        # lock discipline is untouched (the UI never reads these; only worker
        # late-callbacks append, on pool threads).
        self._late_votes: list = []
        self._late_lock = threading.Lock()
        self._results: dict = {
            'ts': '',
            'ticker': self.ticker,
            'kind': self._consensus_kind,
            'path': path,
            'votes': [],
            'verdict_target': '',
            # v4.14.0 stage 5: tag the rollup envelope so Track Record
            # can distinguish post-rework rollups from pre-rework ones.
            # canonical_model stays null on the envelope (a rollup spans
            # multiple canonical models by design, so there's no single
            # id that describes it); only the per-vote entries inside
            # the votes array carry canonical_model.
            'lineup_version': 'v4.14.0',
        }

    # ── Public ──

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return  # already running
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f'consensus-{self.ticker}')
        self._thread.start()

    def cancel(self) -> None:
        self._cancel_event.set()
        if self._current_request is not None:
            try:
                self._current_request.cancel()
            except Exception:
                pass

    def is_running(self) -> bool:
        """True while a started run is still in flight and has NOT been
        cancelled. Added v4.14.5.14-lookup-honesty-and-cancel so the
        Look Up dialog's cancel-on-close handler can decide whether
        there's live work to abort.

        A cancelled runner reports False immediately (even before its
        worker thread has fully unwound), so callers get a deterministic
        answer the instant they call cancel() — the orchestrator may
        still be finishing its current iteration, but no *new* provider
        calls will be made once the cancel event is set."""
        if self._cancel_event.is_set():
            return False
        t = self._thread
        return t is not None and t.is_alive()

    # ── Orchestrator ──

    def _run(self) -> None:

        # v4.14.5.14-mode-detection-collapse-2a (Ollama exit Phase 2): the
        # local / hybrid / api_due_to_game mode machine is gone — consensus
        # is CLOUD-ONLY now. self.inference_mode / self.game_processes are
        # still accepted by __init__ for caller compatibility but are no
        # longer consulted here.

        # v4.14.0 stage 5: build canonical-model groups via the
        # model-aware router. Each group is one canonical model with
        # a failover-ordered list of (provider_id, provider_model_string)
        # tuples — exactly what RouterRun consumes for sticky picks.
        #
        # The provider list (runnable_providers) is kept in parallel
        # for back-compat: refusal checks below still want a flat list
        # for "do we have any cloud capacity at all?" gating, and the
        # legacy iteration path still uses it if the router is missing.
        runnable_providers = []
        provider_groups: dict = {}
        if self.providers and tm_apis is not None:
            if tm_router is not None:
                runnable_providers = tm_router.select_providers(
                    self.providers,
                    call_type=self.call_type,
                    log_fn=self._log,
                )
                # Reuse tm_api_providers' registry resolver so we
                # locate the same data/ directory used elsewhere.
                try:
                    registry = tm_apis._resolve_model_registry()
                except Exception:
                    registry = None
                # v4.14.6.52-cerebras-context-guard: defer the
                # select_provider_groups call until AFTER the prompt is
                # built below — so estimated_prompt_chars can flow into
                # is_eligible_for_model and Cerebras gets skipped (with
                # backfill substitution) on prompts that would silently
                # truncate against its ~8K free-tier ceiling. The
                # registry resolve stays here (cheap, independent).
                self._provider_groups = {}
                self._registry = registry
            else:
                # Legacy fallback — same as before v4.13.56
                for prov in self.providers:
                    if not prov.get('enabled'):
                        continue
                    if not prov.get('endpoint') or not prov.get('model'):
                        self._log(
                            f"Consensus: skipping provider "
                            f"'{prov.get('name', '?')}' — missing endpoint "
                            f"or model.", 'amber')
                        continue
                    runnable_providers.append(prov)
                self._provider_groups = {}
                self._registry = None

        # v4.14.5.14-mode-detection-collapse-2a: cloud-only. Local models are
        # never run, so there is no installed-model filter and no local /
        # hybrid refusal cases — just the single cloud refusal. `runnable` /
        # `missing` stay defined as empty for the downstream code that still
        # references them (counts), harmlessly.
        runnable = []
        missing = []
        if not runnable_providers:
            self._log(
                "Consensus refused: no API providers are enabled. Add and "
                "enable a provider via ☰ → API Providers.",
                'red')
            self._finalize()
            return

        # Build the prompt ONCE — same prompt for every model so we get a
        # fair comparison.
        try:
            prompt, debug = self._prompt_fn(
                self.holding, self.path, self.prompt_builder,
                predictions_log=self.predictions_log)
        except Exception as e:
            self._log(f"Consensus: prompt build failed: {type(e).__name__}: {e}",
                      'red')
            self._finalize()
            return

        # v4.14.6.52-cerebras-context-guard: NOW that the prompt is built,
        # finalize provider_groups with the prompt-size hint threaded in
        # so the eligibility chain (is_eligible_for_model -> _context_
        # guard_check) can skip Cerebras when this prompt would exceed
        # its hard context ceiling — backfill substitution then fills the
        # slot. The legacy fallback path (no tm_router) stays empty as
        # set above; that path has no eligibility chain to guard.
        if tm_router is not None and self.providers and tm_apis is not None:
            try:
                _est_chars = len(prompt) if prompt else None
                provider_groups = tm_router.select_provider_groups(
                    runnable_providers,
                    call_type=self.call_type,
                    registry=self._registry,
                    log_fn=self._log,
                    estimated_prompt_chars=_est_chars,
                )
                self._provider_groups = provider_groups
            except Exception:
                # Fail-open: keep whatever we have (possibly empty);
                # downstream loop handles empty by emitting no votes
                # and finalize() will report the refusal.
                pass

        # v4.14.5.14-mode-detection-collapse-2a: cloud-only consensus log.
        # The mode_desc qualifier ("API-only mode" / "hybrid…" / "local…") is
        # dropped — there is no other mode to contrast against. The line shape
        # ("Consensus on TICKER: up to N models (M cloud), prompt N chars") is
        # preserved so log-pattern consumers (narration translators) keep
        # working.
        total = len(runnable_providers)
        self._log(
            f"Consensus on {self.ticker}: up to {total} "
            f"models ({total} cloud), prompt "
            f"{debug.get('prompt_chars', 0)} chars",
            'muted')

        # ════════════════════════════════════════════════════════════
        # v4.14.5.14-mode-detection-collapse-2a — consensus is CLOUD-ONLY.
        # There is exactly one dispatch path: the cloud round below. The
        # former local-only / hybrid-fallback / api_due_to_game ordering is
        # gone (Ollama exit Phase 2).
        # ════════════════════════════════════════════════════════════

        running_idx = 0
        # v4.14.5.14-mode-detection-collapse-2a: the local-only dispatch was
        # removed here. The cloud-iteration block below is the only dispatch
        # path now. (_run_local_consensus_loop is left defined-but-uncalled;
        # Step 3 deletes it with the Ollama client.)

        # ════════════════════════════════════════════════════════════
        # v4.14.0 stage 5: cloud-provider iteration via model groups.
        #
        # Each canonical model is attempted ONCE per run regardless of
        # how many providers serve it (vote dedup at the consensus
        # layer — gap 3 from the routing rework). Within each
        # canonical model: sticky pick + retry/failover per the
        # resolved spec. Falls back to the legacy flat-iteration path
        # if the router or model groups are unavailable.
        # ════════════════════════════════════════════════════════════

        provider_groups = getattr(self, '_provider_groups', None) or {}
        registry = getattr(self, '_registry', None)

        if tm_router is not None and provider_groups:
            run = tm_router.RouterRun(provider_groups)
            providers_by_id = {p.get('id'): p
                                for p in runnable_providers
                                if p.get('id')}

            _TRANSIENT_BACKOFFS = (1, 3)
            _TRANSIENT_BUDGET = len(_TRANSIENT_BACKOFFS)

            # v4.14.5.62-parallel-consensus: the per-canonical-model unit of
            # work is _dispatch_canonical_model(...) — it carries its own
            # sticky-pick / retry / failover state and RETURNS the single vote
            # that model produced (success / fatal / synthetic-skip), or None
            # if the run was cancelled before it started. SEQUENTIAL (flag OFF,
            # the default) appends in iteration order — byte-identical to pre-
            # patch. PARALLEL (flag ON) fans the units across a bounded thread
            # pool and re-assembles the collected votes in canonical-model
            # order, so the vote SET and ORDER (hence _finalize's first-
            # winning-vote verdict_target) are identical — only wall-clock and
            # arrival order change.
            # v4.14.6.111: consensus rotates CAPABLE-FIRST. Order the panel's
            # canonical models most-capable -> least (lower rank = smarter) so the
            # strongest eligible voices anchor the verdict. Graceful DESCENT is
            # already provided downstream: _rotation_pick_model skips cooled/429'd
            # models per provider, and _try_backfill_substitute substitutes a
            # bench provider when a whole canonical model is exhausted — so the
            # panel never collapses to one pinned model. Stable sort keeps the
            # prior order for equal-rank ties. Fail-open (any error -> unordered,
            # byte-identical to pre-patch).
            ordered_models = list(run.all_canonical_models())
            try:
                import tm_model_capability as _cap_order
                ordered_models.sort(
                    key=lambda _cm: _cap_order.model_capability_rank(_cm))
            except Exception:
                pass
            total_models = len(ordered_models)

            if not _PARALLEL_CONSENSUS or total_models <= 1:
                # ── Sequential dispatch (default) — unchanged behavior ──
                # v4.14.5.69-tier2-backfill: track which provider IDs
                # have actually been called so the substitute helper
                # doesn't re-pick one.
                used_provider_ids = set()
                used_canonical_models = set(ordered_models)
                for idx, canonical_model in enumerate(ordered_models, 1):
                    if self._cancel_event.is_set():
                        self._log(
                            f"Consensus on {self.ticker}: cancelled "
                            f"during cloud iteration.",
                            'amber')
                        break
                    vote = self._dispatch_canonical_model(
                        canonical_model, idx, total, run,
                        providers_by_id, prompt, registry)
                    # Record the provider(s) actually attempted for
                    # this canonical model so backfill skips them.
                    for pid in (run.exhausted_providers(canonical_model)
                                 or set()):
                        used_provider_ids.add(pid)
                    if vote and not vote.get('skipped'):
                        if vote.get('provider_id'):
                            used_provider_ids.add(vote['provider_id'])
                    # Mid-dispatch backfill: a skip-vote means every
                    # provider serving this canonical model is
                    # exhausted. Try to substitute from the bench
                    # before accepting the skip.
                    if vote and vote.get('skipped'):
                        sub = self._try_backfill_substitute(
                            dropped_vote=vote, slot_index=idx,
                            total=total, prompt=prompt,
                            registry=registry,
                            used_provider_ids=used_provider_ids,
                            used_canonical_models=used_canonical_models,
                        )
                        if sub is not None and not sub.get('skipped'):
                            vote = sub
                            if sub.get('provider_id'):
                                used_provider_ids.add(sub['provider_id'])
                            if sub.get('canonical_model'):
                                used_canonical_models.add(
                                    sub['canonical_model'])
                    if vote is not None:
                        self._results['votes'].append(vote)
            else:
                # ── Parallel dispatch — bounded pool, one worker per model ──
                # Each worker's per-provider call is still paced by the now-
                # concurrency-safe rate limiter; RouterRun (RLock, distinct
                # per-model keys) and provider health (own lock) are safe too.
                from concurrent.futures import (
                    ThreadPoolExecutor, wait as _f_wait)
                max_workers = min(
                    total_models, max(1, _PARALLEL_CONSENSUS_MAX_WORKERS))
                _deadline = float(self.FINALIZE_DEADLINE_SEC)
                self._log(
                    f"Consensus on {self.ticker}: dispatching "
                    f"{total_models} models concurrently "
                    f"({max_workers} workers); finalize deadline "
                    f"{_deadline:.0f}s.", 'muted')
                votes_by_idx: dict = {}
                # v4.14.6.111-finalize-deadline: do NOT use the executor as a
                # context manager — its __exit__ runs shutdown(wait=True), which
                # blocks on the slowest model (the 180s straggler) and is the
                # multi-minute freeze this build removes. Instead wait() with a
                # wall-clock deadline, finalize on whoever returned, mark the rest
                # timed-out, and shutdown(wait=False) so the run thread proceeds.
                ex = ThreadPoolExecutor(
                    max_workers=max_workers, thread_name_prefix='consensus')
                future_to_idx = {
                    ex.submit(
                        self._dispatch_canonical_model,
                        cm, idx, total, run,
                        providers_by_id, prompt, registry): idx
                    for idx, cm in enumerate(ordered_models, 1)
                }
                done, not_done = _f_wait(
                    list(future_to_idx.keys()), timeout=_deadline)
                deadline_fired = bool(not_done)
                # Votes that returned in time.
                for fut in done:
                    w_idx = future_to_idx[fut]
                    try:
                        vote = fut.result()
                    except Exception as e:
                        self._log(
                            f"Consensus on {self.ticker}: model "
                            f"worker #{w_idx} crashed: "
                            f"{type(e).__name__}: {e}", 'red')
                        vote = None
                    if vote is not None:
                        votes_by_idx[w_idx] = vote
                # Models still out at the deadline → mark TIMED-OUT (a distinct
                # panel state: did not answer in time — not a vote, not an
                # error-skip). Attach a late recorder so a vote landing AFTER
                # finalize is captured for audit WITHOUT touching the verdict.
                if deadline_fired:
                    self._log(
                        f"Consensus on {self.ticker}: finalize deadline "
                        f"({_deadline:.0f}s) reached — posting verdict on "
                        f"{len(done)} returned; {len(not_done)} model(s) "
                        f"timed out.", 'amber')
                    for fut in not_done:
                        w_idx = future_to_idx[fut]
                        cm_to = (ordered_models[w_idx - 1]
                                 if 1 <= w_idx <= len(ordered_models) else '?')
                        votes_by_idx[w_idx] = {
                            'model': cm_to, 'canonical_model': cm_to,
                            'ts': _now_iso(), 'direction': '',
                            'duration_sec': _deadline,
                            'skipped': True, 'timed_out': True,
                            'reason_one_line':
                                'no response within finalize deadline',
                        }
                        try:
                            fut.add_done_callback(
                                self._make_late_recorder(cm_to))
                        except Exception:
                            pass
                # Release the pool WITHOUT waiting — an abandoned straggler
                # finishes in the background (its late recorder fires) then the
                # pool thread exits; no orphan, no UI block.
                try:
                    ex.shutdown(wait=False)
                except Exception:
                    pass
                # Re-assemble in canonical-model order → identical to sequential.
                # v4.14.5.69-tier2-backfill: scan the assembled votes
                # for skips and try to substitute. Done AFTER the
                # parallel collection so worker scheduling is
                # unchanged; backfill itself is sequential (one
                # provider at a time, like the sequential path).
                # v4.14.5.70-backfill-fixes: track EVERY vote's
                # provider_id (including skipped votes), not just
                # non-skipped — pre-fix the dropped provider's id
                # never entered the set, and the bench-walk happily
                # re-picked the just-rate-limited Gemini. Mirror the
                # sequential path which uses run.exhausted_providers
                # to catch the same thing. Same fix for the canonical
                # model tracker.
                used_provider_ids = set()
                used_canonical_models = set(ordered_models)
                final_votes: list = []
                for idx in range(1, total_models + 1):
                    if idx not in votes_by_idx:
                        continue
                    vote = votes_by_idx[idx]
                    # Every vote — success OR skipped — names a
                    # provider that has been TRIED in this run. The
                    # backfill helper must not re-pick any of them.
                    if vote and vote.get('provider_id'):
                        used_provider_ids.add(vote['provider_id'])
                    # Pull every exhausted provider that the
                    # RouterRun saw for this canonical_model into
                    # used_provider_ids too (a canonical may have had
                    # multiple serving-providers attempted; only one
                    # gets stamped on the vote).
                    cm_for_idx = (
                        ordered_models[idx - 1]
                        if 1 <= idx <= len(ordered_models)
                        else None)
                    if cm_for_idx is not None:
                        try:
                            for pid in (run.exhausted_providers(cm_for_idx)
                                         or set()):
                                used_provider_ids.add(pid)
                        except Exception:
                            pass
                    final_votes.append((idx, vote))
                for idx, vote in final_votes:
                    # v4.14.6.111-finalize-deadline: skip backfill entirely when
                    # the deadline fired — backfill is a SEQUENTIAL substitution
                    # of skipped slots and would re-add exactly the post-deadline
                    # latency this build removes. (timed_out votes are skips, so
                    # the deadline_fired guard also leaves them untouched.)
                    if (not deadline_fired) and vote and vote.get('skipped'):
                        sub = self._try_backfill_substitute(
                            dropped_vote=vote, slot_index=idx,
                            total=total, prompt=prompt,
                            registry=registry,
                            used_provider_ids=used_provider_ids,
                            used_canonical_models=used_canonical_models,
                        )
                        if sub is not None and not sub.get('skipped'):
                            vote = sub
                            if sub.get('provider_id'):
                                used_provider_ids.add(sub['provider_id'])
                            if sub.get('canonical_model'):
                                used_canonical_models.add(
                                    sub['canonical_model'])
                    self._results['votes'].append(vote)

        else:
            # ── Legacy flat-iteration fallback ──────────────────────
            # Router unavailable, or no canonical-model groups built
            # (e.g. registry missing AND no recognizable providers).
            # Falls through to the v4.13.x behavior so the consensus
            # still runs.
            for prov in runnable_providers:
                running_idx += 1
                if self._cancel_event.is_set():
                    self._log(
                        f"Consensus on {self.ticker}: cancelled "
                        f"after {running_idx - 1}/{total} models.",
                        'amber')
                    break

                label = tm_apis.display_label(prov)

                if tm_health is not None:
                    try:
                        health_state = tm_health.get_state()
                        if health_state is not None:
                            prov_id = (prov.get('id')
                                        or prov.get('name', '?'))
                            max_cap = self._resolve_provider_cap(prov)
                            safe, reason = (
                                health_state.is_safe_to_call(
                                    prov_id, max_per_day=max_cap))
                            if not safe:
                                self._log(
                                    f"Consensus on {self.ticker}: "
                                    f"skipping {label}: {reason}",
                                    'amber')
                                skip_vote = {
                                    'model': label,
                                    'ts': _now_iso(),
                                    'duration_sec': 0.0,
                                    'error': (
                                        f"skipped — {reason}"),
                                    'provider_id': prov.get('id'),
                                    'provider_preset':
                                        prov.get('preset'),
                                    'skipped': True,
                                }
                                self._results['votes'].append(
                                    skip_vote)
                                if self.on_model_error:
                                    try:
                                        self.on_model_error(
                                            label,
                                            skip_vote['error'])
                                    except Exception:
                                        pass
                                continue
                    except Exception:
                        pass

                self._log(
                    f"Consensus on {self.ticker}: model "
                    f"{running_idx}/{total} → {label} (API: "
                    f"{prov.get('preset', '?')})",
                    'muted')
                if self.on_model_start:
                    try:
                        self.on_model_start(label)
                    except Exception:
                        pass

                vote = self._run_one_provider(prov, prompt)
                self._results['votes'].append(vote)

                try:
                    prov_id = (prov.get('id')
                                or prov.get('name', '?'))
                    err = vote.get('error', '') or ''
                    is_429 = ('429' in err
                               or 'rate-limit' in err.lower()
                               or 'rate_limit' in err.lower())
                    if tm_health is not None:
                        health_state = tm_health.get_state()
                        if health_state is not None:
                            if not err:
                                health_state.record_success(prov_id)
                            elif is_429:
                                # v4.14.5.71-per-minute-cooldown-cap:
                                # classify the 429 from the error body
                                # already on the vote so a per-minute
                                # blip gets a short (~60s) cooldown
                                # instead of falling into the dumb
                                # 300/300/3600 step curve in
                                # record_rate_limit. Mirrors what the
                                # router-side _record_429_outcome
                                # already does at tm_ai_router.py
                                # for per-minute classification.
                                # tm_provider_learning.classify_429
                                # already handles Groq's structured
                                # error body via _parse_groq_429_body,
                                # plus the OpenAI-compat parsers for
                                # Mistral / Cerebras / GitHub. Failure
                                # to classify falls through unchanged.
                                _ct = None
                                _cd = None
                                try:
                                    import tm_provider_learning as _tpl
                                    _cls = _tpl.classify_429(
                                        provider_name=prov_id,
                                        meta={'headers': {}},
                                        body=err)
                                    if isinstance(_cls, dict):
                                        _ct = _cls.get('type')
                                        _ra = _cls.get(
                                            'retry_after_seconds')
                                        if _ra is not None:
                                            try:
                                                _cd = int(_ra)
                                            except Exception:
                                                _cd = None
                                except Exception:
                                    _ct, _cd = None, None
                                try:
                                    health_state.record_rate_limit(
                                        prov_id,
                                        cooldown_sec=_cd,
                                        cooldown_type=_ct)
                                except TypeError:
                                    # Defensive: pre-v4.14.5.71
                                    # signature doesn't accept the new
                                    # kwargs (shouldn't happen but
                                    # never crash on a rollback).
                                    health_state.record_rate_limit(
                                        prov_id)
                            else:
                                health_state.record_failure(
                                    prov_id, err)
                            try:
                                health_state.save()
                            except Exception:
                                pass
                except Exception:
                    pass

                if vote.get('error'):
                    if self.on_model_error:
                        try:
                            self.on_model_error(label, vote['error'])
                        except Exception:
                            pass
                else:
                    if self.on_model_done:
                        try:
                            self.on_model_done(label, vote)
                        except Exception:
                            pass

        # ════════════════════════════════════════════════════════════
        # v4.14.0 stage 6b — Hybrid-mode Ollama fallback.
        #
        # If we're in hybrid mode AND the cloud round just finished
        # with zero successful votes, fall back to the local Ollama
        # consensus loop. The "successful" check excludes errors and
        # skip votes — only votes with an actual direction count.
        #
        # If cloud succeeded (≥1 successful vote), Ollama is skipped.
        # That's the new hybrid semantic: cloud primary, Ollama only
        # as a degraded fallback. Pre-stage-6b ran both as parallel
        # voices; users may notice the vote count dropped.
        # ════════════════════════════════════════════════════════════
        # v4.14.0 stage 6d: also surface "all cloud exhausted" as a
        # degradation log line for api / api_due_to_game modes —
        # those have no Ollama fallback to mask the no-votes case.
        # The hybrid branch below has its own (more specific) log
        # line, so we skip emitting the generic one there.
        # v4.14.5.14-mode-detection-collapse-2a: this [degradation] line is
        # KEPT (the Look-Up / Recommend narration translators key on
        # "[degradation] … exhausted on") and is now unconditional after the
        # cloud round — the effective_mode guard is gone.
        if not self._cancel_event.is_set():
            try:
                cloud_successes_total = sum(
                    1 for v in self._results['votes']
                    if not v.get('error') and not v.get('skipped')
                    and v.get('direction'))
                if (cloud_successes_total == 0
                        and len(self._results['votes']) > 0):
                    self._log(
                        f"[degradation] All cloud providers "
                        f"exhausted on {self.ticker}; no votes "
                        f"for this run.",
                        'amber')
            except Exception:
                pass

        # v4.14.5.14-mode-detection-collapse-2a: the hybrid Ollama fallback
        # was removed here — cloud is the only path, so there is nothing to
        # fall back to. Cloud exhaustion is surfaced by the [degradation]
        # line above.

        self._finalize()

    # v4.14.5.14-ollama-purge-3a: _run_local_consensus_loop + _run_one_model
    # (the dead local Ollama dispatch) were removed. The cloud round
    # (RouterRun / provider_groups) is the only dispatch path now.


    def _resolve_provider_cap(self, provider: dict) -> Optional[int]:
        """Determine the effective daily call cap for a provider.

        Resolution order (first match wins):
          1. Explicit `max_calls_per_day` field on the provider record
          2. Endpoint-URL-based detection (catches custom-preset
             providers that point at known APIs like Sambanova).
             This intentionally runs BEFORE preset defaults, so a
             custom-preset record pointing at api.sambanova.ai gets
             the right 15/day cap instead of custom's generic 100.
          3. Preset's `default_max_per_day`
          4. None (no cap)

        v4.13.55c: Extracted into its own helper so future smart-router
        work can replace this method with priority/observed-quota-aware
        logic without touching the consensus loop. The signature is
        stable: takes a provider dict, returns an int cap or None.

        Returns:
            int cap (calls/day) or None if no cap should apply.
        """
        # Step 1: explicit override on the provider record
        cap = provider.get('max_calls_per_day')
        if cap is not None:
            try:
                cap = int(cap)
                return cap if cap > 0 else None
            except (TypeError, ValueError):
                pass

        # Step 2: endpoint-URL-based detection.
        # Runs BEFORE preset defaults so a 'custom'-preset record
        # pointing at a known endpoint gets the right cap instead of
        # the generic 100 from the custom preset.
        # v4.13.58.1: numbers re-verified May 2026 against provider
        # docs. Cerebras is token-capped (1M/day) not request-capped.
        try:
            ep = (provider.get('endpoint') or '').lower()
            if 'sambanova' in ep:
                return 15  # server limit is 20/day
            if 'models.inference.ai.azure.com' in ep:
                return 40  # GitHub Models server limit is 50/day
            if 'cerebras' in ep:
                return 1500  # 1M tokens/day, no RPD cap
        except Exception:
            pass

        # Step 3: preset default
        if tm_apis is not None:
            try:
                preset_def = tm_apis.get_preset(provider.get('preset', ''))
                if preset_def:
                    cap = preset_def.get('default_max_per_day')
                    if cap is not None:
                        try:
                            cap = int(cap)
                            if cap > 0:
                                return cap
                            # 0 = unlimited (ollama preset)
                            if cap == 0:
                                return None
                        except (TypeError, ValueError):
                            pass
            except Exception:
                pass

        # Step 4: no cap discovered
        return None

    def _dispatch_canonical_model(self, canonical_model, model_index,
                                  total, run, providers_by_id, prompt,
                                  registry):
        """v4.14.5.62-parallel-consensus: the per-canonical-model unit of
        work — sticky-pick a provider, call it, retry transients / fail over
        on quota, and RETURN the single vote this canonical model produced
        (a success vote, a fatal-error vote, or a synthetic skip vote). Returns
        None only if the run was already cancelled at entry (the caller then
        appends nothing).

        Extracted verbatim from the former in-line canonical-model loop body
        so it can run either sequentially (flag off) or on a thread pool (flag
        on). The only changes vs the in-line version: it RETURNS the vote
        instead of appending to self._results['votes'] (the caller appends, in
        canonical-model order), `running_idx` became the passed model_index,
        and the on_model_* progress callbacks are fired under self._cb_lock so
        they can't re-enter concurrently under parallel dispatch. All shared
        state it touches is concurrency-safe: the rate limiter (reserve-under-
        lock), RouterRun (RLock; distinct per-model keys), provider health
        (own lock), and self._log (cb-lock-guarded)."""
        if self._cancel_event.is_set():
            return None

        _TRANSIENT_BACKOFFS = (1, 3)
        _TRANSIENT_BUDGET = len(_TRANSIENT_BACKOFFS)

        # Display name: prefer registry display_name; else the canonical id;
        # else (synthetic 'unknown/...') the provider's user-facing label.
        registry_display = canonical_model
        if registry is not None:
            try:
                d = registry.get_display_name(canonical_model)
                if d:
                    registry_display = d
            except Exception:
                pass
        is_known_canonical = (
            registry is not None
            and not canonical_model.startswith('unknown/'))

        # Per-canonical-model state
        transient_left = _TRANSIENT_BUDGET
        last_failure_err = ''
        last_provider_id = None
        last_provider_label = '?'
        last_actual_provider = None
        last_actual_model_string = None

        while True:
            if self._cancel_event.is_set():
                break

            pick = run.pick(canonical_model)
            if pick is None:
                break  # all providers exhausted for this model

            provider_id, provider_model_string = pick
            prov = providers_by_id.get(provider_id)
            if prov is None:
                run.mark_exhausted(canonical_model, provider_id)
                transient_left = _TRANSIENT_BUDGET
                continue

            label = tm_apis.display_label(prov)
            last_provider_id = provider_id
            last_provider_label = label
            last_actual_provider = (
                tm_router.provider_canonical_id(prov))
            last_actual_model_string = provider_model_string

            # Per-pick eligibility recheck. Catches providers whose health
            # changed mid-run (e.g. another canonical_model just hit a 429
            # on this provider and now its cooldown is in force).
            declared_cap = None
            try:
                ok, reason, declared_cap = (
                    tm_router.is_eligible_for_model(
                        prov, self.call_type, canonical_model))
            except Exception:
                ok, reason = True, ''
            if not ok:
                self._log(
                    f"Consensus on {self.ticker}: skipping "
                    f"{registry_display} via {label}: "
                    f"{reason}", 'amber')
                run.mark_exhausted(canonical_model, provider_id)
                transient_left = _TRANSIENT_BUDGET
                continue

            self._log(
                f"Consensus on {self.ticker}: model "
                f"{model_index}/{total} → "
                f"{registry_display} via {label}",
                'muted')
            if self.on_model_start:
                with self._cb_lock:
                    try:
                        self.on_model_start(label)
                    except Exception:
                        pass

            vote = self._run_one_provider(
                prov, prompt,
                canonical_model=(canonical_model
                                  if is_known_canonical
                                  else None),
                actual_provider=last_actual_provider,
                actual_model_string=last_actual_model_string)

            err = vote.get('error', '') or ''

            if not err:
                # Success. Record outcome, return the vote.
                try:
                    tm_router.record_call_outcome_for_model(
                        provider_id, canonical_model,
                        outcome=tm_router.OUTCOME_SUCCESS,
                        declared_cap=declared_cap)
                except Exception:
                    pass
                if self.on_model_done:
                    with self._cb_lock:
                        try:
                            self.on_model_done(label, vote)
                        except Exception:
                            pass
                return vote

            # Failure path. Classify, record, decide what next.
            outcome = tm_router.classify_failure(error_text=err)
            try:
                tm_router.record_call_outcome_for_model(
                    provider_id, canonical_model,
                    outcome=outcome, error_msg=err)
            except Exception:
                pass

            last_failure_err = err

            if outcome == tm_router.OUTCOME_QUOTA:
                # v4.14.0 stage 6d: tag as a routing-degradation event.
                self._log(
                    f"[degradation] {registry_display} "
                    f"exhausted on {label}; failing over",
                    'amber')
                run.mark_exhausted(canonical_model, provider_id)
                transient_left = _TRANSIENT_BUDGET
                continue

            if outcome == tm_router.OUTCOME_TRANSIENT:
                if transient_left > 0:
                    backoff = _TRANSIENT_BACKOFFS[
                        _TRANSIENT_BUDGET - transient_left]
                    self._log(
                        f"  {label}: transient "
                        f"({err[:60]}) — retry in "
                        f"{backoff}s", 'amber')
                    time.sleep(backoff)
                    transient_left -= 1
                    continue
                # v4.14.0 stage 6d: tag as degradation.
                self._log(
                    f"[degradation] {label}: transient retries "
                    f"exhausted; failing over for "
                    f"{registry_display}",
                    'amber')
                run.mark_exhausted(canonical_model, provider_id)
                transient_left = _TRANSIENT_BUDGET
                continue

            # OUTCOME_FATAL: don't retry, don't fail over.
            self._log(f"  {label}: {err[:100]}", 'red')
            # v4.14.5.62-model-routing Part 3 (on-error prune): a 404 /
            # model-not-found on a rotation model means it's dead/renamed —
            # prune it from this provider's rotation so it isn't rotated into
            # again. Only fires on the model-not-found signature (not generic
            # fatals like auth). Best-effort; never perturbs dispatch.
            try:
                _el = (err or '').lower()
                if ('not found' in _el or '404' in _el
                        or 'does not exist' in _el or 'unknown model' in _el) \
                        and last_actual_model_string:
                    import tm_provider_discovery as _tpd_prune
                    _tpd_prune.prune_model_from_config(
                        last_provider_id, last_actual_model_string,
                        log_fn=self._log)
            except Exception:
                pass
            if self.on_model_error:
                with self._cb_lock:
                    try:
                        self.on_model_error(label, err)
                    except Exception:
                        pass
            return vote

        # If every provider exhausted without a usable response (only
        # quota/transient failures got swallowed by failover, or the run was
        # cancelled mid-loop), surface ONE synthetic skip vote so Track Record
        # sees that this canonical model was attempted but produced nothing.
        skip_reason = (last_failure_err[:160]
                        if last_failure_err
                        else 'all providers exhausted')
        skip_vote = {
            'model': (registry_display
                       or last_provider_label
                       or '?'),
            'ts': _now_iso(),
            'duration_sec': 0.0,
            'error': f"skipped — {skip_reason}",
            'provider_id': last_provider_id,
            'provider_preset': None,
            'skipped': True,
            'canonical_model': (canonical_model
                                  if is_known_canonical
                                  else None),
            'actual_provider': last_actual_provider,
            'actual_model_string': last_actual_model_string,
            'lineup_version': 'v4.14.0',
        }
        # v4.14.0 stage 6d: degradation log line — this canonical model
        # produced no usable vote across every provider that was tried.
        self._log(
            f"[degradation] {skip_vote['model']} "
            f"unavailable on {self.ticker}; "
            f"vote skipped",
            'amber')
        if self.on_model_error:
            with self._cb_lock:
                try:
                    self.on_model_error(
                        skip_vote['model'],
                        skip_vote['error'])
                except Exception:
                    pass
        return skip_vote

    def _run_one_provider(self, provider: dict, prompt: str,
                            *,
                            canonical_model=None,
                            actual_provider=None,
                            actual_model_string=None) -> dict:
        """v4.13.41 / v4.14.0 stage 5: Run a single API provider and
        return a vote dict in the same shape as _run_one_model.

        On success: {model, ts, direction, range, target, stop_loss,
                     timeframe, confidence, reason_one_line, response,
                     duration_sec, provider_id, provider_preset,
                     canonical_model, actual_provider,
                     actual_model_string, lineup_version}
        On failure: {model, ts, error, duration_sec, provider_id,
                     provider_preset, canonical_model, actual_provider,
                     actual_model_string, lineup_version}

        The 'model' field uses the user's display name (e.g., "My
        Groq") so the consensus card + accuracy matrix continue to
        group by their familiar labels through stage 5. canonical_model
        is what stage-6 / Track Record updates will eventually key on
        for vote dedup at the read layer.

        v4.14.0 stage 5 keyword-only args (all optional; pre-stage-5
        callers omit them and the per-vote record drops them — same
        as a v4.13.x signal record):
          canonical_model: e.g. 'meta/llama-3.1-8b-instruct'.
            Pass None to skip writing the field (e.g. for unknown /
            synthetic canonical ids the caller wants not to surface).
          actual_provider: short canonical provider id ('groq',
            'cerebras', etc.) — distinct from provider_id (the
            per-install UUID).
          actual_model_string: provider-specific model string used
            for the call.
        """
        if tm_apis is None:
            return {
                'model': (provider.get('name') or 'api:?'),
                'ts': _now_iso(),
                'duration_sec': 0.0,
                'error': 'tm_api_providers not available',
            }
        label = tm_apis.display_label(provider)
        start = time.time()

        # Build the v4.14.0 trailer that gets stamped onto every vote
        # this method returns (success or failure).
        v14_trailer = {}
        if canonical_model is not None:
            v14_trailer['canonical_model'] = canonical_model
        if actual_provider is not None:
            v14_trailer['actual_provider'] = actual_provider
        if actual_model_string is not None:
            v14_trailer['actual_model_string'] = actual_model_string
        if v14_trailer:
            v14_trailer['lineup_version'] = 'v4.14.0'

        def _err_vote(err_text: str, dur: float) -> dict:
            v = {
                'model': label,
                'ts': _now_iso(),
                'duration_sec': round(dur, 2),
                'error': err_text,
                'provider_id': provider.get('id'),
                'provider_preset': provider.get('preset'),
            }
            v.update(v14_trailer)
            return v

        if self._cancel_event.is_set():
            return _err_vote('cancelled', 0.0)

        # v4.14.5.17-empty-content-retry Change 3: per-provider timeout
        # override (plumbing only). Reads provider['timeout_seconds'] if
        # the profile carries one, else falls through to the class
        # default (PER_MODEL_TIMEOUT_SEC=180s). No provider sets a
        # custom value today — every existing provider still uses 180s
        # exactly as before. This is future-proofing for a genuinely-
        # slow provider; the FISV symptom this patch ships to fix was
        # never a timeout (Zhipu replied in well under 180s with an
        # empty body — see Change 1).
        try:
            _to_raw = provider.get('timeout_seconds')
        except Exception:
            _to_raw = None
        try:
            _per_call_timeout = (float(_to_raw)
                                  if _to_raw not in (None, '', 0)
                                  else float(self.PER_MODEL_TIMEOUT_SEC))
        except (TypeError, ValueError):
            _per_call_timeout = float(self.PER_MODEL_TIMEOUT_SEC)
        try:
            response = tm_apis.call_provider(
                provider, prompt,
                timeout=_per_call_timeout)
        except tm_apis.ProviderError as e:
            return _err_vote(str(e)[:300], time.time() - start)
        except Exception as e:
            return _err_vote(
                f"{type(e).__name__}: {str(e)[:200]}",
                time.time() - start)

        duration = time.time() - start
        if not response or not response.strip():
            return _err_vote('empty response', duration)

        parsed = self._parse_fn(response)
        vote = {
            'model': label,
            'ts': _now_iso(),
            'duration_sec': round(duration, 2),
            'response': response,
            'direction': parsed.get('direction', ''),
            'target': parsed.get('target', ''),
            'stop_loss': parsed.get('stop_loss', ''),
            'timeframe': parsed.get('timeframe', ''),
            'confidence': parsed.get('confidence', ''),
            'reason_one_line': parsed.get('reason_one_line', ''),
            # v4.14.5.26-lookup-explain: free-text analysis (bull/bear/risks/
            # synthesis), present only for the lookup_explain parser; '' for
            # every other prompt_kind (clean no-op for non-Look-Up consumers).
            'analysis_text': parsed.get('analysis_text', ''),
            'range': format_range_for_card(parsed),
            'provider_id': provider.get('id'),
            'provider_preset': provider.get('preset'),
        }
        vote.update(v14_trailer)

        # Persist immediately for crash safety, same as local models.
        self._save_per_model_signal(vote, prompt)
        return vote

    def _save_per_model_signal(self, vote: dict, prompt: str) -> None:
        if self.signals_log is None:
            return
        try:
            entry = {
                'ticker': self.ticker,
                'kind': self._per_model_kind,
                'path': self.path,
                'model': vote.get('model', '?'),
                'response': vote.get('response', ''),
                'duration_sec': vote.get('duration_sec', 0),
                'direction': vote.get('direction', ''),
                'target': vote.get('target', ''),
                'stop_loss': vote.get('stop_loss', ''),
                'confidence': vote.get('confidence', ''),
                'manual_trigger': True,
                'prompt_chars': len(prompt),
            }
            # v4.14.0 stage 5: forward the model-aware trailer fields
            # so signals.jsonl per-model entries carry canonical_model,
            # actual_provider, actual_model_string, and lineup_version
            # alongside the existing display-name model field.
            for k in ('provider_id', 'provider_preset',
                       'canonical_model', 'actual_provider',
                       'actual_model_string', 'lineup_version'):
                if k in vote and vote[k] is not None:
                    entry[k] = vote[k]
            self.signals_log.append(entry)
        except Exception:
            pass

    def _make_late_recorder(self, canonical_model):
        """v4.14.6.111-finalize-deadline: build an add_done_callback for a model
        that missed the finalize deadline. When its future finally completes (the
        straggler returns), record the vote for AUDIT ONLY — tagged
        late_post_finalize — and NEVER:
          - touch the already-posted verdict,
          - mutate the finalized self._results['votes'] / the signals rollup,
          - trigger the forward predictions recording (on_all_done already ran).
        So the posted verdict is immutable and the scoreboard (predictions.jsonl)
        can't be flipped by a late vote. The late vote stays in self._late_votes
        (in-memory audit) + is surfaced via on_late_vote(model, vote) if the
        caller wired a sink. It is deliberately NOT folded into the signals
        consensus rollup, because a future signals→predictions backfill would
        then re-ingest it into the scoreboard. Runs on a pool worker thread; uses
        the dedicated _late_lock (never _cb_lock). Never raises."""
        def _cb(fut):
            try:
                vote = fut.result()
            except Exception:
                return
            if not isinstance(vote, dict):
                return
            if not (vote.get('direction') or '').strip():
                return  # skip/error straggler — no usable vote to audit
            late = dict(vote)
            late['late_post_finalize'] = True
            late['canonical_model'] = (late.get('canonical_model')
                                       or canonical_model)
            with self._late_lock:
                self._late_votes.append(late)
            # v4.14.6.111 (Item 9): persist the late vote AUDIT-ONLY to the
            # append-only signals.jsonl, under a DISTINCT kind 'per_model_late'.
            # That kind is NOT read by the read-bridge (compute_per_model_stats_
            # from_signals only scores 'per_model_owned'/'per_model_fresh_buy')
            # NOR by the verdict scoreboard (which reads predictions.jsonl) — so
            # a late vote can NEVER enter scoring or the verdict; it only records
            # the late arrival (with latency) for later analysis. No double-count:
            # the late recorder is attached ONLY to models that missed the
            # deadline (not_done futures), so an on-time voter never produces a
            # second entry. Never raises.
            try:
                if self.signals_log is not None:
                    _lentry = {
                        'ticker': self.ticker,
                        'kind': 'per_model_late',
                        'path': self.path,
                        'model': late.get('model', canonical_model),
                        'canonical_model': (late.get('canonical_model')
                                            or canonical_model),
                        'response': late.get('response', ''),
                        'duration_sec': late.get('duration_sec', 0),
                        'direction': late.get('direction', ''),
                        'target': late.get('target', ''),
                        'stop_loss': late.get('stop_loss', ''),
                        'confidence': late.get('confidence', ''),
                        'ts': late.get('ts') or _now_iso(),
                        'late': True,
                        'post_deadline': True,
                        'finalize_deadline_sec': float(
                            self.FINALIZE_DEADLINE_SEC),
                        'manual_trigger': True,
                    }
                    for k in ('provider_id', 'provider_preset',
                               'actual_provider', 'actual_model_string',
                               'lineup_version'):
                        if late.get(k) is not None:
                            _lentry[k] = late[k]
                    self.signals_log.append(_lentry)
            except Exception:
                pass
            try:
                self._log(
                    f"Consensus on {self.ticker}: LATE vote from "
                    f"{canonical_model} "
                    f"({(late.get('direction') or '?').upper()}) arrived after "
                    f"finalize — recorded for audit only; verdict unchanged.",
                    'muted')
            except Exception:
                pass
            if self.on_late_vote:
                try:
                    self.on_late_vote(canonical_model, late)
                except Exception:
                    pass
        return _cb

    def _finalize(self) -> None:
        """Save the rollup to signals.jsonl and call on_all_done."""
        self._results['ts'] = _now_iso()
        # v4.14.5.19-accuracy-weighted-consensus: compute BOTH the raw
        # tally (today's behaviour, honest about vote counts) AND the
        # weighted tally (Wilson-lower-bound per-model weights, [1,9]
        # clamped, neutral on missing/thin data). Attach the summary
        # to _results['tally'] so display sites can show both lines
        # without re-querying. Verdict_target's winning direction is
        # the WEIGHTED winner when the flag is on, else the raw
        # winner -- so the rendered verdict matches the displayed
        # weighted line. When weighting is disabled or every weight
        # is neutral (cold-start), weighted_winner == raw_winner, so
        # this collapses to today's behaviour byte-identically.
        votes_with_dir = [v for v in self._results['votes']
                           if v.get('direction')]
        if votes_with_dir:
            # Defensive getattr: some audits construct runners via
            # __new__ to drive _finalize in isolation without going
            # through __init__ -- treat missing attrs as the flat-tally
            # path (no weighting, no map).
            _weight_map = getattr(self, 'weight_map', None)
            _acc_enabled = getattr(self, 'accuracy_weighting_enabled', False)
            try:
                import tm_source_accuracy as _tsa
                tally = _tsa.weighted_tally(
                    votes_with_dir,
                    _weight_map,
                    _acc_enabled,
                )
            except Exception:
                # Defensive: any failure in the weighting helper falls
                # back to the flat behaviour. A consensus must never
                # crash because the weighting bridge had a problem.
                from collections import Counter
                rc = Counter(v['direction'] for v in votes_with_dir)
                rw, rwn = rc.most_common(1)[0]
                tally = {
                    'raw_counts': dict(rc), 'raw_winner': rw,
                    'raw_winner_n': rwn, 'raw_total': sum(rc.values()),
                    'raw_score': rwn / sum(rc.values()) * 100.0,
                    'weighted_enabled': False,
                    'weighted_winner': rw,
                    'weighted_winner_sum': float(rwn),
                    'weighted_total_sum': float(sum(rc.values())),
                    'weighted_score': rwn / sum(rc.values()) * 100.0,
                    'dir_weights': {d: float(n) for d, n in rc.items()},
                    'n_mature': 0, 'per_vote_weights': {},
                }
            self._results['tally'] = tally
            # Expose the weight_map on the result envelope too, so the
            # UI can render per-model weights without a second lookup.
            if _weight_map is not None:
                self._results['weight_map'] = dict(_weight_map)
            self._results['accuracy_weighting_enabled'] = bool(_acc_enabled)
            winner_dir = tally['weighted_winner']
            # Find a typical range for the winning direction
            for v in votes_with_dir:
                if v['direction'] == winner_dir and v.get('range'):
                    self._results['verdict_target'] = v['range']
                    break

        # v4.14.5.14-layer2-decouple (2026-05-20): the rollup signal
        # write is gated on `write_consensus_signal`. Layer 2 daemon
        # passes False (its diagnostic landing zone is
        # `recommend_cache_validation` — read by `_make_layer2_badge`);
        # user-initiated paths (Look Up Run-Full-Consensus, Recommend
        # Run-Consensus pill click) pass True (default) so an explicit
        # user action continues to write the gate-signal the dialog
        # filter reads. Fail-OPEN: any unexpected exception writes the
        # signal (old behaviour) rather than crashing.
        if self.signals_log is not None and self.write_consensus_signal:
            try:
                self.signals_log.append(dict(self._results))
            except Exception:
                pass

        # Mark holding as analyzed (updates last_analyzed timestamp on the
        # mgr's data dict).  Caller still needs to mgr.save() for it to
        # persist — we do it here on the data, and flag the caller via
        # on_all_done so the panel can persist + re-render.
        if self.on_all_done:
            try: self.on_all_done(self._results)
            except Exception: pass

    def _try_backfill_substitute(self, *, dropped_vote: dict,
                                  slot_index: int, total: int,
                                  prompt: str, registry,
                                  used_provider_ids: set,
                                  used_canonical_models: set) -> Optional[dict]:
        """v4.14.5.69-tier2-backfill: when a tier-2 validator returns a
        skip-vote (every provider serving its canonical model is
        exhausted), try to substitute the next eligible bench
        provider so the consensus still reaches `_TIER2_CAP` healthy
        votes.

        Eligibility re-uses `is_eligible(call_type='holdings_consensus')`
        — which already consults health/cooldown AND (post-v4.14.5.69)
        rejects providers below `_CONSENSUS_MIN_DAILY_CAP`. So thin
        providers (SambaNova, Cohere, GitHub Models, free-tier
        Anthropic) are NEVER reached via backfill.

        Returns the substitute's vote dict on success, or None when no
        eligible substitute could be found / the call didn't yield a
        real vote. Never raises — a backfill failure falls back to the
        original skip-vote (the caller appends that instead).

        Sequential by design: scan-fallback in tm_api_providers is
        also sequential per ticker; we mirror that to keep rate-limit
        behaviour predictable.
        """
        # Backfill is only meaningful when we have a bench to draw
        # from. No bench → behaviour identical to pre-patch (skip
        # accepted as-is).
        bench = self.providers_bench
        if not bench:
            return None
        try:
            import tm_ai_router as _r
        except Exception:
            return None
        try:
            import tm_api_providers as _tmap
        except Exception:
            _tmap = None
        # Walk the bench in priority order; pick the first that is
        # (a) not already used in this run,
        # (b) eligible for holdings_consensus per is_eligible (which
        #     already enforces health + min-cap floor + deprecation),
        # (c) whose canonical_model isn't already covered.
        for prov in bench:
            try:
                pid = prov.get('id') or prov.get('name') or '?'
                if pid in used_provider_ids:
                    continue
                # v4.14.6.52-cerebras-context-guard: pass the prompt
                # size so a bench Cerebras candidate is skipped (and
                # the bench-walk continues to the next eligible
                # provider) instead of silently truncating.
                _est_chars = len(prompt) if prompt else None
                ok, _reason, _cap = _r.is_eligible(
                    prov, 'holdings_consensus',
                    estimated_prompt_chars=_est_chars)
                if not ok:
                    continue
                # Resolve this provider's canonical model so we don't
                # accidentally substitute with one whose canonical
                # exhausted in this run (vote-dedup invariant).
                # v4.14.5.70-backfill-fixes: _resolve_canonical_model
                # returns a 2-tuple (canonical_id_or_None, diagnostic_label) —
                # the pre-fix code stored the whole tuple in `cmod` so
                # the `cmod in used_canonical_models` check (set of
                # strings) NEVER matched, and the bench-walk could
                # accidentally re-pick the just-dropped provider.
                # Unpack so the string id is compared, and pass the
                # string (not the tuple) into _run_one_provider.
                try:
                    cmod_resolved = _r._resolve_canonical_model(
                        prov, registry)
                    if (isinstance(cmod_resolved, tuple)
                            and len(cmod_resolved) >= 1):
                        cmod = cmod_resolved[0]
                    else:
                        # Defensive: a future signature change that
                        # returns a bare string still works.
                        cmod = cmod_resolved
                except Exception:
                    cmod = None
                if cmod and cmod in used_canonical_models:
                    continue
                # Drive a single-provider dispatch. _run_one_provider
                # is the primitive that runs ONE provider and returns
                # a vote dict in the same shape as the regular
                # dispatch — exactly what we need to append in place
                # of the skip-vote.
                # v4.14.5.70-backfill-fixes: move the "substituted X"
                # log AFTER a usable vote comes back. Pre-fix the log
                # fired before the dispatch and could lie when the
                # candidate also failed; the user saw "substituted
                # Gemini" for a substitute that returned an error
                # vote, and the consensus still ended 2/3.
                label = (_tmap.display_label(prov)
                          if _tmap is not None
                          else (prov.get('name') or pid))
                dropped_label = (dropped_vote.get('model') or '?')
                vote = self._run_one_provider(
                    prov, prompt,
                    canonical_model=cmod,
                    actual_provider=pid,
                    actual_model_string=(
                        _r.resolve_provider_model(prov)
                        if hasattr(_r, 'resolve_provider_model')
                        else prov.get('model', '')),
                )
                if vote and not vote.get('error'):
                    self._log(
                        f"[tier2-backfill] {self.ticker}: "
                        f"{dropped_label} unavailable -> substituted "
                        f"{label} (slot {slot_index}/{total})",
                        'muted')
                    return vote
                # Vote came back as an error — try the next bench
                # candidate. Treat this provider as "used" so we don't
                # loop back to it. Quiet trace so a failed attempt is
                # still visible to anyone reading the log, but at the
                # 'muted' level so it doesn't shout.
                used_provider_ids.add(pid)
                self._log(
                    f"[tier2-backfill] {self.ticker}: tried {label} "
                    f"as substitute; not callable right now — "
                    f"trying next bench candidate.",
                    'muted')
            except Exception:
                # Defensive: keep walking the bench.
                continue
        return None

    def _log(self, msg: str, tag: str = 'muted') -> None:
        if self.log_callback:
            # v4.14.5.62-parallel-consensus: serialize so concurrent
            # canonical-model workers never call log_callback re-entrantly.
            # Uncontended under sequential dispatch (behavior-identical).
            with self._cb_lock:
                try:
                    self.log_callback(msg, tag)
                except Exception:
                    pass


def _now_iso() -> str:
    return datetime.now().isoformat(timespec='seconds')
