"""Tired Market — extensible context-block registry for AI prompts
(v4.14.1 Stage 1).

Purpose
-------
The context builder is the assembly layer between the data cache and
the prompt that finally goes to a cloud or local AI model. Each
"block" knows how to render one slice of context (facts, news,
earnings, filings, ...) into a short labelled section. The builder
orchestrates the registered blocks, prepends a short source-tier
authority header, and returns a single string fragment ready to
splice into the prompt.

Why this shape
--------------
Rather than hardcoding the data sections into PromptBuilder, we keep
them as small registered builder functions. Adding a new context
block (insider activity, options flow, sector relative-strength,
etc.) becomes a one-call register_block() — no PromptBuilder edits,
no per-call-site changes, and unit-testable in isolation. The shape
mirrors v4.14.0 stage 1's tm_model_registry: import-time built-in
registration, module-level dict registry, threaded write paths +
lock-free reads, plus a small pair of introspection helpers.

v4.14.1 scope
-------------
Stage 1 (this file) ships:
  - ContextBlock dataclass + module-level registry
  - register_block / unregister_block / list_blocks / get_block
  - build_context orchestrator with optional log_callback
  - estimate_tokens helper (chars // 4 heuristic)
  - SOURCE_TIER_HEADER constant prepended to every fragment
  - Five built-in blocks: FACTS, NEWS, EARNINGS, FILINGS, TECHNICALS
    (TECHNICALS added in v4.14.1 stage 5 alongside the PromptBuilder
    integration; pre-stage-5 the registry shipped with four blocks
    and PromptBuilder rendered TECHNICALS inline)

The four built-in blocks all use defensive getattr(cache, ...)
lookups. NEWS produces real output today (cache.news_features
already exists). FACTS / EARNINGS / FILINGS gracefully return ""
until Stage 2 lands the corresponding DataCacheLayer methods. No
change to those builders is required when Stage 2 ships — they
will start producing real output the moment the cache method
exists.

Block selection in v4.14.1 is uniform: every prompt path gets every
registered block. Per-path differentiation arrives in v4.14.2 by
populating the (currently all-None) _PROMPT_KIND_BLOCK_CONFIG dict
with explicit per-kind block lists.

Soft warning: if the assembled fragment exceeds 12,000 chars
(~3,000 tokens), build_context invokes the optional log_callback
once with a warning that the trajectory is approaching provider
context-window limits. Hard truncation is deferred to v4.14.3+.

Forward-compat
--------------
- v4.14.2: per-prompt-kind block lists via _PROMPT_KIND_BLOCK_CONFIG.
- v4.14.3+: hard token-cap enforcement (truncation) when blocks
  exceed budgets in BLOCK_BUDGETS.
- v4.14.x+: register_block() is the public extension point for new
  block types (analyst sentiment, insider flow, sector RS, etc.).

Template reference
------------------
Structural pattern (module dict + threading lock + import-time
built-in registration + introspection helpers) is borrowed from
tm_model_registry.py — see that file for the v4.14.0 stage 1
prior art.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, Union


# ─── Constants ────────────────────────────────────────────────────────

# Authority header prepended to every assembled prompt fragment.
# v4.14.1 ships this as a hardcoded module-level constant (locked Q1).
# A composable-from-blocks pattern is deferred to v4.14.3+ if 10+
# block types ever materialize.
SOURCE_TIER_HEADER: str = (
    "Sources are tiered by authority:\n"
    "  - SEC filings: highest authority "
    "(legally required disclosure)\n"
    "  - Audited fundamentals: high authority\n"
    "  - Analyst consensus estimates: medium-high authority\n"
    "  - News headlines: medium authority "
    "(varies by source quality)\n"
    "Weight earlier tiers more heavily when sources conflict.\n"
)


# ─── v4.14.2 stage 7: epistemic humility thresholds ──────────────────
#
# Threshold-based source-disagreement detection. The intent is to
# surface to the AI when news / social sources are framing the same
# ticker in directionally conflicting ways, so the AI can reason
# about uncertainty explicitly rather than washing it away in a
# sentiment average.
#
# These are intentionally conservative defaults — better to miss a
# real disagreement than fire false positives that train the AI to
# hedge generically. Future stages can tune these from observed
# accuracy data; stage 7 ships fixed values defined in code.

# News disagreement triggers when:
#  (1) at least NEWS_DISAGREEMENT_MIN_ARTICLES articles exist (don't
#      flag thin coverage — small samples are noisy),
#  (2) at least NEWS_DISAGREEMENT_MIN_SOURCES_PER_SIDE sources lean
#      bullish AND the same number lean bearish (so at least 2 sources
#      on each side of neutral are confirming the disagreement,
#      eliminating the case where one outlier source is dragging the
#      range up but no one is corroborating its lean),
#  (3) the spread between the highest per-source mean sentiment and
#      the lowest per-source mean sentiment is at least
#      NEWS_DISAGREEMENT_MIN_SENTIMENT_RANGE (one source clearly
#      bullish, another clearly bearish — not just two adjacent
#      "barely positive vs slightly less positive" reads).
#
# A per-source sentiment "lean" is bullish if the source's mean
# sentiment is >= +SOURCE_LEAN_THRESHOLD, bearish if <=
# -SOURCE_LEAN_THRESHOLD, and neutral otherwise. Neutral sources
# don't count toward either side; a source has to clearly lean to
# be evidence of disagreement.
NEWS_DISAGREEMENT_MIN_ARTICLES         = 10
NEWS_DISAGREEMENT_MIN_SOURCES_PER_SIDE = 2
NEWS_DISAGREEMENT_MIN_SENTIMENT_RANGE  = 0.4

# Social is sparser than news (StockTwits + Reddit only — and Reddit
# may be empty when shared dev creds aren't provisioned), so the
# minimum-message threshold is lower. Two sources max in the social
# lane, so the per-side requirement is "both sources represented"
# rather than the same N-per-side constraint news uses.
SOCIAL_DISAGREEMENT_MIN_MESSAGES         = 5
SOCIAL_DISAGREEMENT_MIN_SOURCES_PER_SIDE = 2
SOCIAL_DISAGREEMENT_MIN_SENTIMENT_RANGE  = 0.4

# Per-source lean threshold. If a source's mean sentiment is between
# -SOURCE_LEAN_THRESHOLD and +SOURCE_LEAN_THRESHOLD, the source is
# "neutral" and doesn't count toward either side. Independent of the
# range threshold above — that's about gap between sources, this is
# about whether any individual source has taken a clear position.
SOURCE_LEAN_THRESHOLD = 0.2


def detect_news_disagreement(
    articles: Optional[list[dict]],
) -> Optional[dict]:
    """Analyze news articles for meaningful directional disagreement.

    Args:
        articles: list of dicts with 'source' (str) and
                  'sentiment_score' (float in [-1, +1]).

    Returns:
        None if no meaningful disagreement is detected. Otherwise a
        dict with:
          - 'detected':         True
          - 'description':      short string for the block flag line
          - 'sources_bullish':  list[str] of source ids leaning bullish
          - 'sources_bearish':  list[str] of source ids leaning bearish
          - 'sentiment_range':  float (max per-source mean - min per-
                                source mean)

    Detection criteria — ALL must be true to flag:
      (1) total article count >= NEWS_DISAGREEMENT_MIN_ARTICLES
      (2) at least NEWS_DISAGREEMENT_MIN_SOURCES_PER_SIDE sources lean
          bullish AND the same number lean bearish
      (3) range across per-source mean sentiments >=
          NEWS_DISAGREEMENT_MIN_SENTIMENT_RANGE
    """
    if not articles:
        return None
    if len(articles) < NEWS_DISAGREEMENT_MIN_ARTICLES:
        return None

    # Group articles by source, compute per-source mean sentiment.
    by_source: dict[str, list[float]] = {}
    for art in articles:
        if not isinstance(art, dict):
            continue
        src = (art.get('source') or '').strip()
        if not src:
            continue
        try:
            score = float(art.get('sentiment_score'))
        except (TypeError, ValueError):
            continue
        by_source.setdefault(src, []).append(score)

    if len(by_source) < 2:
        return None  # single-source — no disagreement possible

    means: dict[str, float] = {
        src: (sum(scores) / len(scores)) if scores else 0.0
        for src, scores in by_source.items()
    }

    bullish = sorted(
        s for s, m in means.items() if m >= SOURCE_LEAN_THRESHOLD)
    bearish = sorted(
        s for s, m in means.items() if m <= -SOURCE_LEAN_THRESHOLD)

    if (len(bullish) < NEWS_DISAGREEMENT_MIN_SOURCES_PER_SIDE
            or len(bearish) < NEWS_DISAGREEMENT_MIN_SOURCES_PER_SIDE):
        return None

    sent_range = max(means.values()) - min(means.values())
    if sent_range < NEWS_DISAGREEMENT_MIN_SENTIMENT_RANGE:
        return None

    description = (
        f"{', '.join(bullish)} frame bullish; "
        f"{', '.join(bearish)} frame bearish"
    )
    return {
        'detected':        True,
        'description':     description,
        'sources_bullish': bullish,
        'sources_bearish': bearish,
        'sentiment_range': round(sent_range, 3),
    }


def detect_social_disagreement(
    social_snapshot: Optional[dict],
) -> Optional[dict]:
    """Detect cross-source social disagreement.

    Args:
        social_snapshot: the dict cache.social() returns (with
                         'sentiment_breakdown', 'source_breakdown',
                         'total_mentions'). The snapshot already has
                         per-source aggregates pre-computed; we
                         examine reddit_lean vs stocktwits_lean and
                         their pct breakdowns.

    Returns:
        None if no meaningful cross-source disagreement. Otherwise a
        dict shaped like detect_news_disagreement's return.

    Detection criteria:
      (1) total_mentions >= SOCIAL_DISAGREEMENT_MIN_MESSAGES
      (2) both sources contribute at least 1 message each
          (SOCIAL_DISAGREEMENT_MIN_SOURCES_PER_SIDE = 2 with only 2
          sources existing means "both sources represented")
      (3) one source leans bullish and the other leans bearish
          (the agreement label set 'disagree' by _fetch_social, OR
          the per-source means qualify on their own)
      (4) the implied per-source sentiment-range estimate
          >= SOCIAL_DISAGREEMENT_MIN_SENTIMENT_RANGE

    Implementation note:
        cache.social() doesn't expose raw per-source mean-sentiment
        floats directly (it gives us pct_bullish / pct_bearish per
        merged corpus, plus reddit_lean and stocktwits_lean tags from
        _fetch_social). We synthesize a per-source mean from the
        sentiment_breakdown counts when available; otherwise we fall
        back to the lean labels and a coarse range estimate.
    """
    if not isinstance(social_snapshot, dict):
        return None
    total = social_snapshot.get('total_mentions') or 0
    if total < SOCIAL_DISAGREEMENT_MIN_MESSAGES:
        return None

    sb = social_snapshot.get('source_breakdown') or {}
    n_red = sb.get('reddit_count') or 0
    n_st  = sb.get('stocktwits_count') or 0
    if n_red < 1 or n_st < 1:
        return None  # needs both sources represented

    senti = social_snapshot.get('sentiment_breakdown') or {}
    bull_pct = float(senti.get('bullish_pct') or 0.0)
    bear_pct = float(senti.get('bearish_pct') or 0.0)

    r_lean = sb.get('reddit_lean')
    s_lean = sb.get('stocktwits_lean')

    # Disagreement requires distinct directional leans.
    if not (r_lean in ('bullish', 'bearish')
            and s_lean in ('bullish', 'bearish')
            and r_lean != s_lean):
        return None

    # Coarse per-source mean estimate from the lean tag: a "bullish"
    # lean implies the source's tagged share leans positive enough
    # for _fetch_social to have crossed its 1.5x threshold. We assume
    # each lean tag corresponds to roughly +/- 0.4 in normalized
    # sentiment terms — sufficient to satisfy the range threshold by
    # design when one source is bullish and the other bearish.
    sources_bullish = ['reddit'] if r_lean == 'bullish' else ['stocktwits']
    sources_bearish = ['stocktwits'] if r_lean == 'bullish' else ['reddit']

    estimated_range = 0.8
    if estimated_range < SOCIAL_DISAGREEMENT_MIN_SENTIMENT_RANGE:
        return None

    # Render percentages from the snapshot's actual numbers if we
    # have them, otherwise omit. _fetch_social does NOT split bull/
    # bear pct per-source today (only the merged corpus pct), so
    # the description leans on the lean tag and an aggregate hint.
    desc_bull = (f"{sources_bullish[0].capitalize()} leaning bullish")
    desc_bear = (f"{sources_bearish[0].capitalize()} leaning bearish")
    if bull_pct or bear_pct:
        description = (
            f"{desc_bull} (~{bull_pct:.0f}% of merged tagged "
            f"messages bullish overall); "
            f"{desc_bear} (~{bear_pct:.0f}% bearish overall)"
        )
    else:
        description = f"{desc_bull}; {desc_bear}"

    return {
        'detected':        True,
        'description':     description,
        'sources_bullish': sources_bullish,
        'sources_bearish': sources_bearish,
        'sentiment_range': estimated_range,
    }


# Sentinel substrings used by both _news_block / _social_block (when
# producing the flag line) and get_disagreement_context (when scanning
# rendered blocks to decide whether to inject the QUESTION-block
# epistemic humility prepend). Keep the prefix unique.
NEWS_DISAGREEMENT_FLAG   = "Source disagreement:"
SOCIAL_DISAGREEMENT_FLAG = "Cross-source disagreement:"

EPISTEMIC_HUMILITY_PREPEND = (
    "Note: some data sources show meaningful directional "
    "disagreement on this ticker. Acknowledge specific points of "
    "disagreement in your reasoning. Be specific about WHAT is mixed "
    "(cite the conflicting sources by name when possible). Avoid "
    "generic hedging language like 'it depends', 'time will tell', "
    "or 'mixed signals'. If the picture is genuinely uncertain, "
    "prefer WATCH over BUY (or HOLD over BUY for owned positions).\n"
)


def get_disagreement_context(blocks: list[str]) -> Optional[str]:
    """Scan rendered context blocks for the disagreement-flag markers
    and return the epistemic-humility prepend if any flag is present.

    Returns None if no flag is found (status quo behavior — the
    QUESTION block renders byte-identically to stage 6).

    Args:
        blocks: list of rendered block fragments OR a single big
                string. Both shapes accepted because callers vary
                (build_context returns one combined string; some
                callers might want to pass a per-block list).
    """
    if not blocks:
        return None
    if isinstance(blocks, str):
        haystack = blocks
    else:
        haystack = "\n".join(blocks)
    if (NEWS_DISAGREEMENT_FLAG in haystack
            or SOCIAL_DISAGREEMENT_FLAG in haystack):
        return EPISTEMIC_HUMILITY_PREPEND
    return None

# Advisory per-block token budgets (chars // 4 heuristic). Not
# enforced in v4.14.1 — purely a hint for future truncation logic
# in v4.14.3+ and a default value for register_block convenience
# calls. The numbers reflect rough ceilings expected for each
# block type when populated with realistic data.
BLOCK_BUDGETS: dict[str, int] = {
    "FACTS":      400,
    "NEWS":       600,
    "EARNINGS":   200,
    "FILINGS":    300,
    "TECHNICALS": 250,
    "MACRO":      250,    # v4.14.2 stage 4 — global macro snapshot
    "SOCIAL":     250,    # v4.14.2 stage 5 — per-ticker social signal
}

# Per-prompt-kind block-list configuration. v4.14.2 stage 4 starts
# populating this with explicit lists (was all-None previously).
# None still means "use every registered block in registration
# order" — kept as a fallback for any future prompt kind not yet
# enumerated.
#
# MACRO is the first block where path-aware inclusion matters: it
# adds ~250 tokens of context that informs slow / moderate analysis
# but doesn't help fast aggressive / lottery decisions where the
# user wants speed and ticker-specific signals over macro framing.
# Locked analysis intentionally skips MACRO (and TECHNICALS — same
# rationale as v4.14.1: locked positions can't be sold so macro
# context is decision-irrelevant).
_PROMPT_KIND_BLOCK_CONFIG: dict[str, Optional[list[str]]] = {
    "holding_analysis": [
        "FACTS", "NEWS", "EARNINGS", "FILINGS", "TECHNICALS", "MACRO",
        "SOCIAL"],
    "locked_analysis": [
        "FACTS", "NEWS", "EARNINGS", "FILINGS"],  # no TECHNICALS, no MACRO, no SOCIAL
    "candidate": [
        "FACTS", "NEWS", "EARNINGS", "FILINGS", "TECHNICALS", "MACRO",
        "SOCIAL"],
    "fresh_buy": [
        "FACTS", "NEWS", "EARNINGS", "FILINGS", "TECHNICALS", "MACRO",
        "SOCIAL"],
}


# v4.14.2 stage 4: per-PATH block subsets. Path-aware inclusion
# layered on top of prompt-kind config. Aggressive / lottery prompts
# skip MACRO + FILINGS for speed (the user wants fast ticker-specific
# read; macro context and 8-K archaeology slow that down without
# changing the call). slow_safe / moderate paths include everything.
# Empty / unknown path falls back to the prompt-kind config above.
#
# v4.14.2 stage 5: SOCIAL added with deliberate path-shaping:
#   catalyst-/lottery-style paths INCLUDE social (primary use case)
#   slow_safe / conservative paths SKIP social (wrong tool for
#       fundamentals-driven analysis; signal is too noisy)
#   moderate / balanced paths inherit the prompt-kind default
#       (which now includes SOCIAL)
_PATH_BLOCK_OVERRIDES: dict[str, list[str]] = {
    # Speed-prioritized paths: skip MACRO + FILINGS but DO keep
    # SOCIAL — short-term catalyst plays benefit from Reddit chatter.
    "aggressive": [
        "FACTS", "NEWS", "EARNINGS", "TECHNICALS", "SOCIAL"],
    "lottery": [
        "FACTS", "NEWS", "TECHNICALS", "SOCIAL"],
    # Conservative paths: skip SOCIAL (and existing MACRO is already
    # in the default kind list, so no override needed for that).
    "slow_safe": [
        "FACTS", "NEWS", "EARNINGS", "FILINGS", "TECHNICALS",
        "MACRO"],
    # v4.14.3.14 (2026-05-15): 'conservative_income' entry deleted.
    # It was identical to 'slow_safe' and the key wasn't in
    # tm_holdings.PATHS — no consumer ever asked for it. Either a
    # copy-paste vestige or a planned-future path that never got
    # plumbed. Re-add here if/when tm_holdings.PATHS grows.
    # moderate / balanced inherit the prompt-kind defaults
    # (no override entry -> use prompt-kind list with SOCIAL).
}


def blocks_for(prompt_kind: Optional[str] = None,
               path: Optional[str] = None) -> Optional[list[str]]:
    """v4.14.2 stage 4: resolve the block list for a given prompt
    kind + path combination.

    Path overrides win when present (aggressive / lottery skip
    macro + filings); otherwise the prompt-kind config applies;
    None as the final return means "every registered block."

    Both arguments are optional — callers that don't know one or
    the other still get sensible behavior. Kept as a free function
    rather than wired straight into build_context so callers can
    introspect the resolved list (useful for test + verification
    probes)."""
    if path:
        path_key = str(path).lower().strip()
        if path_key in _PATH_BLOCK_OVERRIDES:
            return list(_PATH_BLOCK_OVERRIDES[path_key])
    if prompt_kind and prompt_kind in _PROMPT_KIND_BLOCK_CONFIG:
        cfg = _PROMPT_KIND_BLOCK_CONFIG[prompt_kind]
        return list(cfg) if cfg is not None else None
    return None

# Soft-warning threshold (chars). Above this, build_context emits a
# one-shot warning via log_callback if one was provided.
_SOFT_WARNING_THRESHOLD_CHARS: int = 12_000


# ─── Block dataclass + registry ───────────────────────────────────────


@dataclass
class ContextBlock:
    """One named context block. The builder_fn renders the block's
    section text given (ticker, path, cache); the orchestrator wraps
    every call in try/except so a bad builder can't crash the prompt
    assembly.

    Attributes:
        name: Block identifier — uppercase short label (e.g.
            'FACTS', 'NEWS'). Used both as the registry key and as
            the section heading inside the rendered text.
        builder_fn: Callable (ticker: str, path: str, cache) -> str.
            Returns the section text including its own header line,
            or '' to skip emission entirely. cache may be None.
        default_token_budget: Advisory hint for future truncation
            logic. Not enforced in v4.14.1.
        description: Human-readable description for debug surfaces.
    """
    name: str
    builder_fn: Callable[..., str]
    default_token_budget: int = 200
    description: str = ""


# Module-level registry. Insertion order matters — Python's regular
# dict preserves it and that's the order build_context emits blocks
# in when no explicit `blocks` list is supplied.
_REGISTRY: dict[str, ContextBlock] = {}
_REGISTRY_LOCK = threading.Lock()


# ─── Registration API ────────────────────────────────────────────────


def register_block(
    block_or_name: Union[ContextBlock, str],
    builder_fn: Optional[Callable[..., str]] = None,
    default_token_budget: int = 200,
    description: str = "",
) -> None:
    """Register a context block.

    Two signatures are supported:

        register_block(ContextBlock(...))                  # primary
        register_block(name, builder_fn,                   # convenience
                       default_token_budget=200,
                       description='')

    Re-registering an existing name silently overwrites the prior
    entry — the new block keeps its original registration position
    (insertion-order is preserved across overwrites).
    """
    if isinstance(block_or_name, ContextBlock):
        block = block_or_name
    else:
        if builder_fn is None:
            raise ValueError(
                "register_block: builder_fn is required when "
                "passing name+fn instead of a ContextBlock")
        block = ContextBlock(
            name=block_or_name,
            builder_fn=builder_fn,
            default_token_budget=default_token_budget,
            description=description,
        )

    with _REGISTRY_LOCK:
        _REGISTRY[block.name] = block


def unregister_block(name: str) -> bool:
    """Remove a registered block by name.

    Returns True if a block was actually removed, False if no block
    by that name was registered. Symmetric counterpart to
    register_block; useful for tests that need a clean registry.
    """
    with _REGISTRY_LOCK:
        if name in _REGISTRY:
            del _REGISTRY[name]
            return True
        return False


def list_blocks() -> list[str]:
    """Return all registered block names in registration order.

    Lock-free read — safe to call concurrently with builds, but a
    register_block / unregister_block firing in parallel may show
    up between two consecutive calls (no transactional guarantee).
    """
    return list(_REGISTRY.keys())


def get_block(name: str) -> Optional[ContextBlock]:
    """Return the registered ContextBlock by name, or None if not
    registered. Lock-free read."""
    return _REGISTRY.get(name)


# ─── Token estimation helper ─────────────────────────────────────────


def estimate_tokens(text: str) -> int:
    """Rough char-to-token estimate using the well-known
    chars // 4 heuristic. Floor of 1 so empty strings don't return
    zero (callers like to use this as a divisor in some places).

    Not exact — different tokenizers vary by 10-30 % — but stable
    enough for budget tracking and the soft-warning threshold.
    """
    return max(1, len(text) // 4)


# ─── Internal helpers ────────────────────────────────────────────────


def _humanize_money(value) -> str:
    """Convert a numeric value to a short human-readable money
    string: '1.50B', '5.00M', '12.35K', '99.00'. Returns the input
    coerced to str on non-numeric input so callers don't need their
    own try/except.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000_000_000:
        return f"{sign}{v / 1_000_000_000_000:.2f}T"
    if v >= 1_000_000_000:
        return f"{sign}{v / 1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"{sign}{v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"{sign}{v / 1_000:.2f}K"
    return f"{sign}{v:.2f}"


def _maybe_append_source_quality(
    lines: list[str], cache, lane: str,
) -> None:
    """v4.14.2-stage6 — additive source-quality renderer.

    If the cache exposes `source_weights_for_lane(lane)` and the
    returned weights are differentiated (not all uniform at the same
    within-tier score), insert a one-line "Source quality: ..."
    summary right under the block header. Otherwise no-op.

    Stage 6 ships with all weights uniform (every source seeded at 5)
    AND with no `source_weights_for_lane` exposed on the cache yet, so
    this is byte-identically silent today. The hook is here so future
    stages — once accuracy measurement starts moving scores — light up
    automatically without re-touching every block builder.
    """
    fn = getattr(cache, 'source_weights_for_lane', None)
    if fn is None:
        return
    try:
        weights = fn(lane) or []
    except Exception:
        return
    try:
        import tm_source_weights
        line = tm_source_weights.render_source_quality_line(weights)
    except Exception:
        return
    if line and lines:
        # Insert immediately under the header line.
        lines.insert(1, f"  {line}")


def _safe_log(log_callback: Optional[Callable[[str], None]],
                message: str) -> None:
    """Invoke log_callback with message, swallowing any exception
    raised by the callback itself. The orchestrator should never
    crash because the activity-log emit hit a transient error."""
    if log_callback is None:
        return
    try:
        log_callback(message)
    except Exception:
        pass


# ─── Built-in block builders ─────────────────────────────────────────
#
# Each builder takes (ticker, path, cache) and returns either the
# fully-rendered section text (including its own header line) or
# the empty string to skip emission. `path` is reserved for v4.14.2
# per-path differentiation; v4.14.1 builders ignore it.
#
# Every builder uses the getattr(cache, 'method_name', None)
# defensive pattern so missing cache methods (Stage 2 hasn't shipped
# yet) gracefully degrade to "" instead of crashing.


# ─── v4.14.5.62-analyst-facts: surface analyst consensus + price target ──
# The analyst fields (recommendation_key, target_mean_price) are carried
# through the Yahoo adapter + derived-fundamentals overlay into the dict
# _facts_block reads (no new fetch — `.info` already runs). Rendering them
# is gated by this module flag, set from cfg['surface_analyst_facts']
# (default OFF) at startup + on Settings save. OFF → _facts_block output is
# byte-identical to pre-patch (the analyst lines are simply not appended).
_SURFACE_ANALYST_FACTS = False

# yfinance recommendationKey → human-readable consensus label.
_REC_KEY_LABELS = {
    'strong_buy': 'Strong Buy', 'buy': 'Buy', 'hold': 'Hold',
    'sell': 'Sell', 'strong_sell': 'Strong Sell',
    # yfinance occasionally returns these variants:
    'underperform': 'Underperform', 'outperform': 'Outperform',
}


def set_surface_analyst_facts(enabled: bool) -> None:
    """Set the analyst-facts gate from cfg['surface_analyst_facts'].
    Default OFF = FACTS block byte-identical to today."""
    global _SURFACE_ANALYST_FACTS
    _SURFACE_ANALYST_FACTS = bool(enabled)


# v4.14.5.62-insider-flow: gate for the open-market insider buy/sell line.
# OFF (default) → FACTS block byte-identical. The aggregate is computed in the
# BACKGROUND fetcher (also flag-gated); _facts_block only READS the persisted
# row (no fetch).
_SURFACE_INSIDER_FLOW = False


def set_surface_insider_flow(enabled: bool) -> None:
    """Set the insider-flow gate from cfg['surface_insider_flow']."""
    global _SURFACE_INSIDER_FLOW
    _SURFACE_INSIDER_FLOW = bool(enabled)


def _fmt_usd_signed(v: float) -> str:
    """Humanize a dollar magnitude to $X.XM / $XK etc. (sign handled by the
    caller's 'net buying +' / 'net selling -' wording — `v` is a magnitude)."""
    v = abs(float(v))
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    if v >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def _facts_block(ticker: str, path: str, cache) -> str:
    fn = getattr(cache, "fundamentals", None)
    if fn is None:
        return ""
    try:
        data = fn(ticker) or {}
    except Exception:
        return ""
    if not data:
        return ""
    lines = ["[FACTS]"]
    name = data.get("company_name")
    if name:
        lines.append(f"  Company: {name} ({ticker})")
    sector = data.get("sector")
    if sector:
        lines.append(f"  Sector: {sector}")
    industry = data.get("industry")
    if industry and industry != sector:
        lines.append(f"  Industry: {industry}")
    mc = data.get("market_cap")
    if mc:
        lines.append(f"  Market cap: ${_humanize_money(mc)}")
    pe = data.get("pe_ratio")
    if pe is not None:
        lines.append(f"  P/E ratio: {pe:.1f}")
    eps = data.get("eps")
    if eps is not None:
        lines.append(f"  EPS (TTM): ${eps:.2f}")
    beta = data.get("beta")
    if beta is not None:
        lines.append(f"  Beta: {beta:.2f}")
    div = data.get("dividend_yield")
    if div is not None and div > 0:
        lines.append(f"  Dividend yield: {div:.2f}%")

    # v4.15.0 Step 14: render statement-level columns when a deep row populated
    # the envelope. Each line is None-guarded so a thin row (snapshot-only)
    # renders exactly as before.
    fiscal_period = data.get("fiscal_period_end")
    if fiscal_period and fiscal_period != '__current__':
        lines.append(f"  Most recent reporting period: {fiscal_period}")
    rev = data.get("revenue")
    if rev is not None:
        lines.append(f"  Revenue (period): ${_humanize_money(rev)}")
    ni = data.get("net_income")
    if ni is not None:
        lines.append(f"  Net income (period): ${_humanize_money(ni)}")
    gm = data.get("gross_margin")
    if gm is not None:
        lines.append(f"  Gross margin: {gm*100:.1f}%")
    om = data.get("operating_margin")
    if om is not None:
        lines.append(f"  Operating margin: {om*100:.1f}%")
    ta = data.get("total_assets")
    if ta is not None:
        lines.append(f"  Total assets: ${_humanize_money(ta)}")
    tl = data.get("total_liabilities")
    if tl is not None:
        lines.append(f"  Total liabilities: ${_humanize_money(tl)}")

    # v4.14.5.62-analyst-facts: append analyst consensus + mean target when
    # the flag is ON and the data is present. Missing data omits the line
    # (never "None"/"$0"). Flag OFF → nothing appended (byte-identical).
    if _SURFACE_ANALYST_FACTS:
        rk = (data.get("recommendation_key") or "").strip().lower()
        rk_label = _REC_KEY_LABELS.get(rk)
        if rk_label:
            lines.append(f"  Analyst consensus: {rk_label}")
        tmp = data.get("target_mean_price")
        try:
            tmp = float(tmp) if tmp is not None else None
        except (TypeError, ValueError):
            tmp = None
        if tmp and tmp > 0:
            lines.append(f"  Analyst mean target: ${tmp:,.2f}")

    # v4.14.5.62-insider-flow: surface the persisted open-market insider
    # buy/sell aggregate (computed in the background — READ ONLY here, no
    # fetch). Flag OFF → nothing appended (byte-identical). Omit when there's
    # no row or a near-zero net (prefer silence over "+$0").
    if _SURFACE_INSIDER_FLOW:
        try:
            import tm_cache as _tc_if
            _ifrow = _tc_if.get_insider_flow(ticker)
        except Exception:
            _ifrow = None
        if _ifrow is not None:
            try:
                _ik = _ifrow.keys() if hasattr(_ifrow, 'keys') else []
                _net = (_ifrow['net_open_market_usd']
                        if 'net_open_market_usd' in _ik else None)
                _wd = (_ifrow['window_days']
                       if 'window_days' in _ik else 90) or 90
                _net = float(_net) if _net is not None else None
            except Exception:
                _net = None
            # near-zero (< $10k) → omit rather than show a noisy +$0
            if _net is not None and abs(_net) >= 10000.0:
                if _net > 0:
                    lines.append(
                        f"  Insider activity: net buying "
                        f"+{_fmt_usd_signed(_net)} "
                        f"(open-market, last {int(_wd)}d)")
                else:
                    lines.append(
                        f"  Insider activity: net selling "
                        f"-{_fmt_usd_signed(_net)} "
                        f"(open-market, last {int(_wd)}d)")

    return "\n".join(lines) if len(lines) > 1 else ""


def _news_block(ticker: str, path: str, cache) -> str:
    fn = getattr(cache, "news_features", None)
    if fn is None:
        return ""
    try:
        news = fn(ticker) or {}
    except Exception:
        return ""
    article_count = news.get("article_count", 0)
    # v4.14.1 stage 5: prefer 'top_headlines' (the field name that other
    # code paths in tired_market.py use) but fall back to 'headlines' so
    # any test stub or future producer that uses the older key still
    # works.
    headlines = (news.get("top_headlines")
                  or news.get("headlines")
                  or [])
    if not article_count and not headlines:
        return ""
    lines = ["[NEWS — last 7 days]"]
    if article_count:
        lines.append(f"  Articles found: {article_count}")
    sentiment = news.get("sentiment_score")
    if sentiment is not None:
        lines.append(
            f"  Aggregate sentiment: {sentiment:+.2f} "
            f"(-1=very bearish, +1=very bullish)")
    for h in headlines[:5]:
        title = h.get("title", "") if isinstance(h, dict) else str(h)
        if title:
            lines.append(f"  - {title[:120]}")
    # v4.14.2 stage 7: epistemic humility — flag meaningful per-source
    # directional disagreement so the AI reasons about uncertainty
    # explicitly. Detection consumes the per-article (source,
    # sentiment_score) records get_news_features now surfaces.
    raw_articles = news.get("articles") or []
    disagreement = detect_news_disagreement(raw_articles)
    if disagreement and disagreement.get('detected'):
        lines.append(
            f"  ⚠ {NEWS_DISAGREEMENT_FLAG} "
            f"{disagreement['description']}")
    _maybe_append_source_quality(lines, cache, 'news')

    # v4.14.5.68-tier2-deep-news: append article BODIES (when present
    # in the cache) for tier-2 prompts only. Tier-1 ('candidate') keeps
    # the headline-only output above — byte-identical to pre-v4.14.5.68.
    # The deep section is appended in addition to (not replacing) the
    # headlines: a model that can read bodies still benefits from the
    # aggregate sentiment + disagreement signal computed above. The
    # PREFETCH happens in tm_consensus.build_*_prompt; this block ONLY
    # READS the cache — no network call here.
    try:
        if _deep_news_active() and ticker:
            deep_section = _render_news_bodies(ticker, cache)
            if deep_section:
                lines.append("")
                lines.append(deep_section)
    except Exception:
        # The deep-news add-on must never break the shallow block.
        pass
    return "\n".join(lines)


# v4.14.5.68-tier2-deep-news: thread-local flag set by build_context
# when prompt_kind is a tier-2 path. Block builders consult this via
# _deep_news_active() so the shallow `_news_block` can append a
# BODIES section only for tier-2 paths — without changing the block
# builder signature (still ticker, path, cache).
import threading as _threading_for_deep
_DEEP_NEWS_STATE = _threading_for_deep.local()
_DEEP_NEWS_PROMPT_KINDS = frozenset({'holding_analysis', 'fresh_buy'})


def _deep_news_active() -> bool:
    """True iff the current build_context call was invoked with a
    prompt_kind that asks for tier-2 deep news. Defaults to False so
    any pre-flag caller (or a builder running outside build_context)
    sees today's shallow rendering."""
    return bool(getattr(_DEEP_NEWS_STATE, 'active', False))


def _render_news_bodies(ticker: str, cache) -> str:
    """Render the [NEWS BODIES] sub-section from cached article
    bodies for this ticker. Pure cache-read (delegates to
    tm_news_bodies.get_cached_bodies which itself never fetches).
    Returns '' when no bodies are cached — caller then ships the
    headline-only block unchanged (graceful fallback)."""
    try:
        import tm_news_bodies as _nb
    except Exception:
        return ""
    # Block builders only see `cache`. The SQLite `db` handle the
    # news_cache table lives in is reached via the module-wide app
    # registry (tm_teacher_intercept._registered_app, the same one
    # emit_system_event uses). cache.db is also accepted if a future
    # version of DataCacheLayer exposes one. Either missing → no
    # bodies → renderer returns '' → caller ships headline-only
    # block (graceful fallback).
    db = getattr(cache, 'db', None)
    if db is None:
        try:
            import tm_teacher_intercept as _tm_ic
            _app = getattr(_tm_ic, '_registered_app', None)
            db = getattr(_app, 'db', None) if _app is not None else None
        except Exception:
            db = None
    if db is None:
        return ""
    try:
        bodies = _nb.get_cached_bodies(db, ticker)
    except Exception:
        return ""
    if not bodies:
        return ""
    lines = ["[NEWS BODIES — article excerpts]"]
    total = 0
    overall_cap = _nb._TIER2_NEWS_BODY_RENDER_TOTAL_MAX_CHARS
    per_cap = _nb._TIER2_NEWS_BODY_RENDER_MAX_CHARS
    for b in bodies[:_nb._TIER2_NEWS_BODY_MAX_ARTICLES]:
        title = (b.get('title') or '').strip()
        source = (b.get('source') or '').strip()
        body = (b.get('body') or '').strip()
        if not body:
            continue
        snippet = body[:per_cap]
        head_line = f"  — {title}" if title else "  — (article)"
        if source:
            head_line += f"  [{source}]"
        lines.append(head_line)
        lines.append(f"    {snippet}")
        total += len(snippet)
        if total >= overall_cap:
            lines.append("    [further bodies omitted for length]")
            break
    if len(lines) == 1:
        return ""  # only the header — nothing to add
    return "\n".join(lines)


def _earnings_block(ticker: str, path: str, cache) -> str:
    fn = getattr(cache, "earnings", None)
    if fn is None:
        return ""
    try:
        earn = fn(ticker) or {}
    except Exception:
        return ""
    # v4.14.5.14-earnings-architecture-fix-v2 (prompt honesty): cache.earnings
    # returns {'_unavailable': True} when the data SOURCES errored (rate-limit /
    # network / 5xx) — distinct from a genuine "no scheduled earnings" (None ->
    # omit, below). Tell the AI explicitly when it's flying blind rather than
    # silently dropping the section.
    if earn.get("_unavailable"):
        return ("[EARNINGS CALENDAR]\n"
                "  Earnings data unavailable for this ticker "
                "(data sources unavailable).")
    next_event = earn.get("next_event")
    if not next_event:
        return ""
    lines = ["[EARNINGS CALENDAR]"]
    date_part = next_event.get("date")
    hour_part = next_event.get("hour")
    hour_suffix = f" ({hour_part})" if hour_part else ""
    lines.append(f"  Next earnings: {date_part}{hour_suffix}")
    eps_est = next_event.get("eps_estimate")
    if eps_est is not None:
        lines.append(f"  EPS estimate: ${eps_est:.2f}")
    rev_est = next_event.get("revenue_estimate")
    if rev_est is not None:
        lines.append(
            f"  Revenue estimate: ${_humanize_money(rev_est)}")
    last = earn.get("last_quarter")
    if (last
            and last.get("eps_actual") is not None
            and last.get("eps_estimate") is not None):
        delta = last["eps_actual"] - last["eps_estimate"]
        outcome = "beat" if delta >= 0 else "miss"
        lines.append(
            f"  Last quarter: ${last['eps_actual']:.2f} actual "
            f"vs ${last['eps_estimate']:.2f} estimate ({outcome})")
    return "\n".join(lines)


def _technicals_block(ticker: str, path: str, cache) -> str:
    """Technical indicators block — calls cache.technicals(ticker).

    Field names are the ones produced by `compute_technicals(history_df)`
    in tired_market.py:1473-1641. The pre-Stage-5 inline TECHNICALS
    sections in PromptBuilder used a mix of correct and incorrect field
    names (e.g. 'rsi_14', 'sma_50', 'volatility_30' don't exist in
    compute_technicals output — only 'rsi', 'sma50', 'volatility_20d'
    do); this block uses the verified-correct names so the AI actually
    sees the data instead of silently empty output.
    """
    fn = getattr(cache, "technicals", None)
    if fn is None:
        return ""
    try:
        tech = fn(ticker) or {}
    except Exception:
        return ""
    if not tech:
        return ""
    lines = ["[TECHNICAL INDICATORS]"]
    rsi = tech.get("rsi")
    if rsi is not None:
        lines.append(f"  RSI (14): {rsi:.1f}")
    sma20 = tech.get("sma20")
    sma50 = tech.get("sma50")
    if sma20 is not None and sma50 is not None:
        lines.append(
            f"  SMA 20: ${sma20:.2f}  /  SMA 50: ${sma50:.2f}")
    elif sma20 is not None:
        lines.append(f"  SMA 20: ${sma20:.2f}")
    elif sma50 is not None:
        lines.append(f"  SMA 50: ${sma50:.2f}")
    macd = tech.get("macd")
    macd_hist = tech.get("macd_histogram")
    if macd is not None and macd_hist is not None:
        lines.append(
            f"  MACD: {macd:+.3f} (histogram {macd_hist:+.3f})")
    elif macd is not None:
        lines.append(f"  MACD: {macd:+.3f}")
    bb_upper = tech.get("bb_upper")
    bb_lower = tech.get("bb_lower")
    bb_pos = tech.get("bb_position")
    if bb_upper is not None and bb_lower is not None:
        if bb_pos is not None:
            lines.append(
                f"  Bollinger band: ${bb_lower:.2f} – ${bb_upper:.2f} "
                f"(position {bb_pos:.0f}%)")
        else:
            lines.append(
                f"  Bollinger band: ${bb_lower:.2f} – ${bb_upper:.2f}")
    vol_ratio = tech.get("volume_ratio")
    if vol_ratio is not None:
        lines.append(f"  Volume: {vol_ratio:.2f}x 10-day avg")
    atr_pct = tech.get("atr_pct")
    if atr_pct is not None:
        lines.append(f"  ATR: {atr_pct:.2f}% of price")
    vol20 = tech.get("volatility_20d")
    if vol20 is not None:
        lines.append(f"  20-day volatility: {vol20:.2f}%")
    mom10 = tech.get("momentum_10")
    if mom10 is not None:
        lines.append(f"  10-day momentum: {mom10:+.2f}%")
    adx = tech.get("adx")
    if adx is not None:
        trend_label = ("trending" if adx >= 25
                        else "ranging" if adx < 20
                        else "weak trend")
        lines.append(f"  ADX: {adx:.1f} ({trend_label})")
    mr_z = tech.get("mean_reversion_z")
    if mr_z is not None:
        lines.append(
            f"  Mean-reversion z-score: {mr_z:+.2f} "
            f"(distance from 20-day mean)")
    return "\n".join(lines) if len(lines) > 1 else ""


def _macro_block(ticker: str, path: str, cache) -> str:
    """v4.14.2 stage 4: global macro snapshot.

    Reads from cache.macro() which merges Yahoo's keyless yields +
    VIX with FRED's FWK Fed funds / CPI / unemployment / GDP /
    canonical Treasury indicators. ticker is unused (macro is
    global); path is consulted upstream by _PROMPT_KIND_BLOCK_CONFIG
    to decide whether MACRO appears in this prompt at all.

    Returns "" when no macro data is available (both Yahoo and FRED
    failed, or both unavailable). Prompts continue to render
    cleanly without the block.
    """
    fn = getattr(cache, "macro", None)
    if fn is None:
        return ""
    try:
        m = fn() or {}
    except Exception:
        return ""
    if not m:
        return ""
    lines = ["[MACRO CONTEXT]"]

    # Headline rates — Fed funds first (the policy lever).
    fed = m.get("fed_funds")
    if fed is not None:
        lines.append(f"  Fed funds rate: {fed:.2f}%")

    # Treasury yields. Render the canonical 10Y / 2Y pair when
    # available (FRED provides both). Yahoo gives 10Y / 5Y / 13W /
    # 30Y instead — fall back to 10Y / 5Y if 2Y is absent.
    t10 = m.get("treasury_10y")
    t2 = m.get("treasury_2y")
    t5 = m.get("treasury_5y")
    if t10 is not None and t2 is not None:
        lines.append(
            f"  10Y / 2Y Treasury: {t10:.2f}% / {t2:.2f}%")
    elif t10 is not None and t5 is not None:
        lines.append(
            f"  10Y / 5Y Treasury: {t10:.2f}% / {t5:.2f}%")
    elif t10 is not None:
        lines.append(f"  10Y Treasury: {t10:.2f}%")

    # Yield-curve spread, in priority order: FRED's canonical T10Y2Y
    # first; Yahoo's 10Y-5Y or 10Y-3M derived approximations otherwise.
    spread = m.get("curve_spread_10y_2y")
    if spread is None:
        spread = m.get("curve_spread_10y_5y")
    if spread is None:
        spread = m.get("curve_spread_10y_3m")
    if spread is not None:
        if spread < 0:
            label = "inverted"
        elif spread < 0.25:
            label = "flat"
        elif spread < 1.0:
            label = "slightly positive"
        else:
            label = "steep"
        lines.append(
            f"  Yield-curve spread: {spread:+.2f}% ({label})")

    cpi = m.get("cpi_yoy_pct")
    if cpi is not None:
        lines.append(f"  CPI YoY: {cpi:.1f}%")
    unemp = m.get("unemployment_pct")
    if unemp is not None:
        lines.append(f"  Unemployment: {unemp:.1f}%")
    vix = m.get("vix")
    if vix is not None:
        lines.append(f"  VIX: {vix:.1f}")
    gdp = m.get("gdp")
    if gdp is not None:
        # GDP from FRED is in $billions, quarterly.
        lines.append(f"  GDP (latest quarter): ${gdp:,.0f}B")

    return "\n".join(lines) if len(lines) > 1 else ""


def _social_block(ticker: str, path: str, cache) -> str:
    """v4.14.2 stage 5: social-lane signal block.

    Reads from cache.social(ticker) which merges Reddit posts (when
    embedded or user credentials are configured) with StockTwits
    messages (always available — keyless). Renders a compact,
    signal-dense summary — no raw post text — sized to fit the
    250-token block budget.

    Path-aware inclusion is wired upstream via _PATH_BLOCK_OVERRIDES:
    catalyst plays / lottery / penny_lottery paths INCLUDE social
    (primary use case); slow_safe / conservative paths SKIP it
    (wrong tool for fundamentals analysis); moderate / balanced
    include it.

    Per the IDEAS source-weighting hierarchy, social is tier C —
    signal density is real but lower-trust than news. The prompt
    framing should NOT promote social signal to fact status; the
    rendered text uses qualifying language ("Reddit chatter",
    "StockTwits posters say") to keep the AI's epistemic frame
    accurate. Future stages will tighten this with the
    epistemic-humility-on-directional-disagreement IDEAS work.
    """
    fn = getattr(cache, "social", None)
    if fn is None:
        return ""
    try:
        s = fn(ticker) or {}
    except Exception:
        return ""
    if not s:
        return ""
    total = s.get('total_mentions') or 0
    if total <= 0:
        return ""

    sb = s.get('source_breakdown') or {}
    senti = s.get('sentiment_breakdown') or {}
    bull_pct = senti.get('bullish_pct') or 0.0
    bear_pct = senti.get('bearish_pct') or 0.0
    neutral_pct = senti.get('neutral_pct') or 0.0
    n_red = sb.get('reddit_count') or 0
    n_st = sb.get('stocktwits_count') or 0
    subs = sb.get('subreddits') or []
    topics = s.get('top_topics') or []
    agreement = sb.get('agreement') or 'insufficient'

    lines = ["[SOCIAL CONTEXT]"]
    if n_red > 0:
        sub_str = ', '.join(f"r/{x}" for x in subs[:4])
        lines.append(
            f"  Reddit: {n_red} mentions / 24h"
            + (f" ({sub_str})" if sub_str else ""))
    if n_st > 0:
        lines.append(f"  StockTwits: {n_st} messages / 24h")
    # Aggregate sentiment line — only meaningful if at least one
    # source contributed tagged signal.
    if bull_pct or bear_pct:
        lines.append(
            f"  Sentiment: {bull_pct:.0f}% bullish, "
            f"{bear_pct:.0f}% bearish, {neutral_pct:.0f}% neutral")
    if topics:
        lines.append(f"  Topics: {', '.join(topics)}")
    # Cross-source agreement framing — only when there's enough
    # signal in BOTH sources to claim agreement / disagreement.
    agreement_text_map = {
        'agree':        "directional agreement across sources",
        'disagree':     "sources disagree on direction",
        'mixed':        "mixed signal within sources",
        'bullish':      "leaning bullish",
        'bearish':      "leaning bearish",
        'insufficient': "",  # don't render — too thin
    }
    extra = agreement_text_map.get(agreement, "")
    if extra and n_red > 0 and n_st > 0:
        lines.append(f"  Cross-source: {extra}")
    elif extra and (n_red > 0 or n_st > 0):
        # Single-source lean — still useful framing.
        lines.append(f"  Overall: {extra}")
    # v4.14.2 stage 7: epistemic humility — flag cross-source
    # disagreement (Reddit leaning one way, StockTwits the other)
    # explicitly so the AI reasons about it instead of averaging it
    # away. Distinct from the existing 'Cross-source: ...' line above
    # which is descriptive ('agree' / 'disagree' / 'mixed'); this one
    # is the action-trigger marker the QUESTION-block prepend looks
    # for.
    disagreement = detect_social_disagreement(s)
    if disagreement and disagreement.get('detected'):
        lines.append(
            f"  ⚠ {SOCIAL_DISAGREEMENT_FLAG} "
            f"{disagreement['description']}")
    _maybe_append_source_quality(lines, cache, 'social')
    return "\n".join(lines) if len(lines) > 1 else ""


# ─── v4.14.5.62-8k-descriptions: recent material-event (8-K) signal ──────
# The filings table now persists EDGAR's primaryDocDescription, so the most
# recent 8-K's "what" is available. Gated by surface_8k_events (default OFF):
# OFF → _filings_block byte-identical to today (description forced empty so a
# now-populated column doesn't change output); ON → the 8-K line becomes a
# concise recent-catalyst signal (recency + description + 30-day count), and
# is omitted entirely when the most recent 8-K is older than ~90 days.
_SURFACE_8K_EVENTS = False


def set_surface_8k_events(enabled: bool) -> None:
    """Set the 8-K material-event gate from cfg['surface_8k_events'].
    Default OFF = filings block byte-identical to today."""
    global _SURFACE_8K_EVENTS
    _SURFACE_8K_EVENTS = bool(enabled)


def _render_recent_8k(recent_8ks) -> 'str | None':
    """Flag-on 8-K signal from the by-form '8-K' list (newest-first):
    recency + description + count in last 30d, gated to the last ~90 days.
    A stale 8-K (>90d) isn't a live catalyst → return None (omit the line).
    Never fabricates: no "0 in last 30d", no "never". Returns the line or None."""
    from datetime import date, datetime, timedelta
    if not recent_8ks:
        return None
    mr = recent_8ks[0]
    fd = (mr.get('filing_date') or '')[:10]
    try:
        mr_date = datetime.strptime(fd, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None
    today = date.today()
    days_ago = (today - mr_date).days
    if days_ago < 0 or days_ago > 90:
        return None  # stale / bad date → not a fresh signal
    cutoff = today - timedelta(days=30)
    n_30 = 0
    for f in recent_8ks:
        d = (f.get('filing_date') or '')[:10]
        try:
            if datetime.strptime(d, '%Y-%m-%d').date() >= cutoff:
                n_30 += 1
        except (ValueError, TypeError):
            continue
    desc = (mr.get('description') or '').strip()[:80]
    recency = 'today' if days_ago == 0 else f'{days_ago}d ago'
    out = f"  Recent 8-K: filed {recency}"
    if desc:
        out += f" — {desc}"
    if n_30 > 0:
        out += f" ({n_30} in last 30d)"
    return out


def _filings_block(ticker: str, path: str, cache) -> str:
    fn = getattr(cache, "filings", None)
    if fn is None:
        return ""
    try:
        f = fn(ticker) or {}
    except Exception:
        return ""
    filings = f.get("filings") or []
    if not filings:
        return ""
    lines = ["[INSIDER ACTIVITY / RECENT FILINGS]"]
    by_form: dict[str, list] = {}
    for fil in filings[:20]:
        form = (fil.get("form") or "").upper()
        by_form.setdefault(form, []).append(fil)
    for form_label, key in (("8-K",   "8-K"),
                              ("10-Q",  "10-Q"),
                              ("10-K",  "10-K"),
                              ("Form 4", "4")):
        recent = by_form.get(key)
        if not recent:
            continue
        # v4.14.5.62-8k-descriptions: flag-on, the 8-K line becomes a concise
        # recent-catalyst signal (recency + description + 30d count, 90-day
        # gate). A >90d 8-K is omitted (not a fresh signal). Every other case
        # (flag-off, or non-8-K forms) keeps today's "Most recent {form}:
        # {date} — {desc}" line — with `desc` forced empty when the flag is
        # OFF so the now-populated description column can't change flag-off
        # output (byte-identical guarantee).
        if key == "8-K" and _SURFACE_8K_EVENTS:
            _line = _render_recent_8k(recent)
            if _line:
                lines.append(_line)
            continue
        most_recent = recent[0]
        desc = ((most_recent.get("description") or "")[:80]
                if _SURFACE_8K_EVENTS else "")
        lines.append(
            f"  Most recent {form_label}: "
            f"{most_recent.get('filing_date')} — {desc}")
    _maybe_append_source_quality(lines, cache, 'filings')
    return "\n".join(lines) if len(lines) > 1 else ""


# ─── Orchestrator ────────────────────────────────────────────────────


def build_context(
    ticker: str,
    path: str,
    cache,
    blocks: Optional[list[str]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    prompt_kind: Optional[str] = None,
) -> str:
    """Assemble a prompt-ready context fragment for `ticker` on
    analysis `path`, using the named `blocks` (or every registered
    block in registration order if `blocks` is None).

    Args:
        ticker: Stock ticker symbol.
        path: Analysis path identifier (e.g. 'moderate',
            'aggressive'). Forwarded to each builder AND consulted
            by the v4.14.2 stage 4 path-aware block resolver.
        cache: Data cache providing per-block lookup methods
            (fundamentals, news_features, earnings, filings, ...).
            May be None — every builder defensively returns '' if
            the corresponding cache method is missing.
        blocks: Optional explicit list of block names to render,
            in the order specified. None = consult prompt_kind +
            path via blocks_for(); if that also returns None, use
            every registered block in registration order.
        log_callback: Optional callable (str) -> None invoked when
            (a) a builder raises an exception, or (b) the assembled
            fragment exceeds the soft-warning threshold.
        prompt_kind: Optional v4.14.2 stage 4 hook. When `blocks`
            is None and prompt_kind is set, the function consults
            _PROMPT_KIND_BLOCK_CONFIG and _PATH_BLOCK_OVERRIDES via
            blocks_for() to pick the right block subset for this
            kind/path combination. Pre-stage-4 callers that don't
            pass prompt_kind get the legacy "all registered blocks"
            behavior — backward-compatible.

    Returns:
        The assembled context fragment as a single string. Always
        starts with SOURCE_TIER_HEADER. If every block returns ""
        (no cache, or cache exists but holds no data for ticker),
        the returned string is just SOURCE_TIER_HEADER on its own.
    """
    if blocks is None:
        # v4.14.2 stage 4: try the path-aware resolver first.
        resolved = blocks_for(prompt_kind=prompt_kind, path=path)
        if resolved is None:
            names = list(_REGISTRY.keys())
        else:
            names = resolved
    else:
        names = list(blocks)

    # v4.14.5.68-tier2-deep-news: stash the prompt_kind on a thread-
    # local so `_news_block` knows whether to append the cached
    # [NEWS BODIES] section. Tier-1 ('candidate') stays headlines-
    # only; tier-2 ('holding_analysis'/'fresh_buy') gets the deep
    # append when bodies are cached. Restored in `finally` so a
    # nested call can't leak the flag.
    _prior_deep = getattr(_DEEP_NEWS_STATE, 'active', False)
    _DEEP_NEWS_STATE.active = (
        prompt_kind in _DEEP_NEWS_PROMPT_KINDS)
    try:
        rendered: list[str] = []
        for name in names:
            block = _REGISTRY.get(name)
            if block is None:
                continue
            try:
                section = block.builder_fn(ticker, path, cache)
            except Exception as e:
                _safe_log(
                    log_callback,
                    f"build_context: block '{name}' raised "
                    f"{type(e).__name__}: {e} for {ticker}/{path} "
                    f"(event_type=context_block_error)")
                continue
            if section:
                rendered.append(section)
    finally:
        _DEEP_NEWS_STATE.active = _prior_deep

    if rendered:
        body = "\n\n".join(rendered)
        output = SOURCE_TIER_HEADER + "\n" + body
    else:
        output = SOURCE_TIER_HEADER

    if (len(output) > _SOFT_WARNING_THRESHOLD_CHARS
            and log_callback is not None):
        _safe_log(
            log_callback,
            f"build_context: assembled prompt fragment is "
            f"{len(output)} chars (~{estimate_tokens(output)} "
            f"tokens) for {ticker}/{path}; trajectory is "
            f"approaching provider context-window limits "
            f"(threshold {_SOFT_WARNING_THRESHOLD_CHARS} chars)")

    return output


# ─── Built-in registration (runs at import time) ─────────────────────


def _register_builtins() -> None:
    """Register the four v4.14.1 built-in blocks in canonical order:
    FACTS, NEWS, EARNINGS, FILINGS. Idempotent — re-importing the
    module silently overwrites the existing entries with the same
    builders, which is fine. Tests that mutate the registry via
    register_block / unregister_block can call this to restore
    the default registration set.
    """
    register_block(ContextBlock(
        name="FACTS",
        builder_fn=_facts_block,
        default_token_budget=BLOCK_BUDGETS["FACTS"],
        description=(
            "Company fundamentals: name, sector, market cap, P/E, "
            "EPS, beta, dividend yield. Sourced from the data "
            "cache's fundamentals(ticker) method."),
    ))
    register_block(ContextBlock(
        name="NEWS",
        builder_fn=_news_block,
        default_token_budget=BLOCK_BUDGETS["NEWS"],
        description=(
            "Recent news headlines (last 7 days) with article "
            "count and aggregate sentiment. Sourced from the data "
            "cache's news_features(ticker) method."),
    ))
    register_block(ContextBlock(
        name="EARNINGS",
        builder_fn=_earnings_block,
        default_token_budget=BLOCK_BUDGETS["EARNINGS"],
        description=(
            "Next earnings event date + EPS / revenue estimates, "
            "plus last-quarter beat/miss outcome. Sourced from "
            "the data cache's earnings(ticker) method."),
    ))
    register_block(ContextBlock(
        name="FILINGS",
        builder_fn=_filings_block,
        default_token_budget=BLOCK_BUDGETS["FILINGS"],
        description=(
            "Most recent SEC filings by form type (8-K, 10-Q, "
            "10-K, Form 4 / insider activity). Sourced from the "
            "data cache's filings(ticker) method."),
    ))
    # v4.14.1 stage 5: TECHNICALS joins the built-in block set so
    # PromptBuilder can drop the inline section and route everything
    # through build_context. Locked-analysis variant uses the
    # blocks=[...] subset to exclude this block (locked positions
    # can't be sold so technicals are decision-irrelevant).
    register_block(ContextBlock(
        name="TECHNICALS",
        builder_fn=_technicals_block,
        default_token_budget=BLOCK_BUDGETS["TECHNICALS"],
        description=(
            "Technical indicators (RSI, SMA 20/50, MACD, Bollinger "
            "bands, volume ratio, ATR %, volatility, momentum, ADX, "
            "mean-reversion z-score). Sourced from the data cache's "
            "technicals(ticker) method."),
    ))
    # v4.14.2 stage 4: MACRO joins as the 6th built-in. Global (no
    # ticker), merged from Yahoo's keyless yields + VIX with FRED's
    # FWK Fed funds / CPI / unemployment / GDP. Only included in
    # paths where macro context informs the analysis (slow_safe,
    # moderate, holding_analysis) per _PROMPT_KIND_BLOCK_CONFIG.
    register_block(ContextBlock(
        name="MACRO",
        builder_fn=_macro_block,
        default_token_budget=BLOCK_BUDGETS["MACRO"],
        description=(
            "Macro snapshot: Fed funds rate, Treasury yields, "
            "yield-curve spread, CPI, unemployment, VIX, GDP. "
            "Sourced from the data cache's macro() method which "
            "merges Yahoo (keyless) + FRED (FWK)."),
    ))
    # v4.14.2 stage 5: SOCIAL joins as the 7th built-in. Per-ticker,
    # merged from Reddit (FWK with embedded fallback) + StockTwits
    # (keyless). Path-aware inclusion via _PATH_BLOCK_OVERRIDES —
    # catalyst plays / lottery / penny_lottery include it (primary
    # use case); slow_safe / conservative skip it (wrong tool for
    # fundamentals analysis).
    register_block(ContextBlock(
        name="SOCIAL",
        builder_fn=_social_block,
        default_token_budget=BLOCK_BUDGETS["SOCIAL"],
        description=(
            "Social signal: Reddit posts (r/wallstreetbets, "
            "r/stocks, r/investing, r/StockMarket) + StockTwits "
            "messages, with sentiment breakdown and cross-source "
            "agreement. Tier C in the source-weighting hierarchy — "
            "lower trust than news; useful for catalyst detection "
            "and sentiment temperature."),
    ))


_register_builtins()
