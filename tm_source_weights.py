"""
tm_source_weights.py — Source weighting schema, registry, and read API
                       (v4.14.2 stage 6).

What this is:
    The data layer that future stages of source-quality measurement will
    build on. Stage 6 ships:

      - The canonical source registry (SOURCE_TIERS) — hardcoded category
        tiers for every source the multi-source merge layer touches.
      - The lane-to-sources mapping (LANE_SOURCES) — which sources serve
        which data lane (news / social / filings).
      - The migration helper (initialize_source_weights) that seeds the
        source_weights table with every registered source at the
        mid-active default (within_tier=5, state='active') the first
        time it runs. Idempotent.
      - The read API (get_source_weight, list_active_sources_for_lane)
        that prompt construction and (eventually) accuracy measurement
        consume.

What stage 6 does NOT do:
    - Measure source accuracy (no prediction outcomes yet)
    - Drive state transitions (active <-> watched <-> removed)
    - Render anything user-visible (settings UI ships in a later stage)
    - Influence the prompt today (all weights uniform => no extra render)

The whole point of stage 6 is to put the schema and access path in
place so future stages have a clean foundation.

Two-dimensional weighting:
    category tier (A/B/C/D, hardcoded) x within-tier score (1-15).

    A — SEC filings, regulatory, primary sources (most authoritative).
    B — News (Yahoo, Finnhub, RSS, Google News, etc.).
    C — Social (Reddit, StockTwits).
    D — Reserved for future fringe sources (none initially).

    Within-tier score state boundaries:
       1- 8  -> active
       9-13  -> watched
      14-15  -> removed

    Cross-tier movement is hardcoded; only the within-tier number is
    eventually data-driven.

Per-(source x context x ticker) scoring:
    A row is keyed (source_id, context_id, ticker). Specificity falls
    back: ticker -> '__global__', context -> '__global__'. The migration
    seeds the most general row (both '__global__'). Per-ticker /
    per-context overrides are written as future stages need them.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional


# ─── Canonical source registry ────────────────────────────────────────
#
# Tier is hardcoded. Adding a new source means adding it here in code
# with an explicit tier assignment. Tiers never move — only the
# within-tier number does (eventually, when accuracy data is available).

SOURCE_TIERS: dict[str, str] = {
    # Tier A — SEC filings, regulatory, primary sources.
    'sec_edgar':      'A',

    # Tier B — News (multi-source merged).
    'yahoo_news':     'B',
    'finnhub_news':   'B',
    'rss_news':       'B',
    'google_news':    'B',

    # Tier C — Social.
    'reddit':         'C',
    'stocktwits':     'C',

    # Tier D — Reserved for future fringe sources. None initially.
}


# ─── Model registry (v4.14.3) ─────────────────────────────────────────
#
# Tier 'M' — AI models that produce predictions. Distinct from data
# sources (A/B/C/D) because their accuracy is measured differently:
# data sources are scored on framing reliability when they cover an
# event; models are scored on whether their BUY predictions hit
# target vs stop.
#
# Names match the 'model' field exactly as it appears in
# predictions.jsonl. Variants like 'Github' / 'GitHub' / 'My Groq' /
# 'Groq' are deliberately kept separate because they reflect
# different provider configurations the user set up — accuracy may
# legitimately differ between, say, "My Groq" (the user's free-tier
# Groq config) and "Groq" (a recommended-default config).
#
# Adding a model means adding it here. Unknown models discovered in
# predictions.jsonl are skipped silently by the accuracy bridge — we
# don't auto-register sources.

MODEL_TIERS: dict[str, str] = {
    # Local Ollama models (observed in the user's predictions.jsonl).
    'qwen2.5:14b':       'M',
    'qwen2.5:3b':        'M',
    'qwen3.5:9b':        'M',
    'phi3:3.8b':         'M',
    'phi4:14b':          'M',
    'gemma4:e4b':        'M',
    'llama3.1:8b':       'M',
    'hermes3:8b':        'M',
    # Cloud providers (config display names — variants kept separate).
    'Groq':              'M',
    'My Groq':           'M',
    'Mistral':           'M',
    'My Minstral':       'M',     # the user's typo; preserved verbatim
    'my Minstral':       'M',
    'Mistral Small':     'M',
    'Github':            'M',
    'GitHub':            'M',
    'Sambanova':         'M',
    'SambaNova':         'M',
    'Cerebras':          'M',
    'Gemini':            'M',
    'My Gemini':         'M',
    'Gemini 2.5 Flash Lite': 'M',
}


# ─── Combined registry helpers ────────────────────────────────────────

def all_registered_source_ids() -> set[str]:
    """Every source_id known to the system — data sources + models.
    Used by get_source_weight to validate input."""
    return set(SOURCE_TIERS.keys()) | set(MODEL_TIERS.keys())


def tier_for(source_id: str) -> Optional[str]:
    """Return the category_tier for any registered source_id.
    Returns None if the source isn't registered."""
    if source_id in SOURCE_TIERS:
        return SOURCE_TIERS[source_id]
    if source_id in MODEL_TIERS:
        return MODEL_TIERS[source_id]
    return None


def is_model(source_id: str) -> bool:
    """True if source_id is a registered AI model (tier 'M')."""
    return source_id in MODEL_TIERS


# ─── Lane membership ──────────────────────────────────────────────────
#
# Which sources are scored against which data lane. Used by
# list_active_sources_for_lane() so prompt construction (and future
# accuracy measurement) can ask "what's currently active for news?"
# and get a clean list.

LANE_SOURCES: dict[str, list[str]] = {
    'filings': ['sec_edgar'],
    'news':    ['yahoo_news', 'finnhub_news', 'rss_news', 'google_news'],
    'social':  ['reddit', 'stocktwits'],
}


# ─── State boundaries ─────────────────────────────────────────────────

ACTIVE_MAX  = 8     # within_tier_score 1-8  -> active
WATCHED_MAX = 13    # within_tier_score 9-13 -> watched
REMOVED_MAX = 15    # within_tier_score 14-15 -> removed

DEFAULT_WITHIN_TIER = 5     # mid-active. Every source starts here.
DEFAULT_STATE       = 'active'

GLOBAL_KEY = '__global__'


def _state_for_score(score: int) -> str:
    """Map a within-tier score to its operational state. Used by the
    migration to seed state correctly and (eventually) by the
    state-transition logic."""
    if score <= ACTIVE_MAX:
        return 'active'
    if score <= WATCHED_MAX:
        return 'watched'
    return 'removed'


# ─── Migration: seed defaults ─────────────────────────────────────────

def initialize_source_weights(conn: sqlite3.Connection) -> int:
    """Seed the source_weights table with every registered source at
    the mid-active default. Idempotent: re-running this never duplicates
    rows and never resets existing scores.

    Args:
        conn: open sqlite3 connection to tired_market.db. The table
              must already exist (Database._init creates it).

    Returns:
        Number of rows inserted (0 on subsequent runs once seeded).
    """
    now = datetime.now().isoformat(timespec='seconds')
    inserted = 0
    # v4.14.3: seed BOTH data sources (A/B/C/D) AND models (M).
    # Same INSERT OR IGNORE idempotency rule.
    combined: dict[str, str] = {}
    combined.update(SOURCE_TIERS)
    combined.update(MODEL_TIERS)
    for source_id, tier in combined.items():
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO source_weights (
                source_id, context_id, ticker,
                category_tier, within_tier_score, state,
                sample_size, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (source_id, GLOBAL_KEY, GLOBAL_KEY,
             tier, DEFAULT_WITHIN_TIER, DEFAULT_STATE, now),
        )
        if cur.rowcount:
            inserted += 1
    conn.commit()
    return inserted


# ─── Read API ─────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    """Convert a sqlite3.Row (or tuple from cursor) into the public
    dict shape callers expect."""
    return {
        'source_id':            row[0],
        'context_id':           row[1],
        'ticker':               row[2],
        'category_tier':        row[3],
        'within_tier_score':    row[4],
        'state':                row[5],
        'sample_size':          row[6],
        'confidence_band_low':  row[7],
        'confidence_band_high': row[8],
    }


def get_source_weight(
    conn: sqlite3.Connection,
    source_id: str,
    context_id: str = GLOBAL_KEY,
    ticker: str = GLOBAL_KEY,
) -> Optional[dict]:
    """Return weight info for a source. Falls back through specificity:

      1. (source_id, context_id, ticker)             — most specific
      2. (source_id, context_id, '__global__')       — context default
      3. (source_id, '__global__', '__global__')     — global default

    Returns the dict {category_tier, within_tier_score, state,
    sample_size, confidence_band_low, confidence_band_high, ...} or
    None if source_id isn't registered at all (no global row exists).
    """
    # v4.14.3: data sources (SOURCE_TIERS) and AI models (MODEL_TIERS)
    # both qualify. Single registry view via all_registered_source_ids.
    if source_id not in SOURCE_TIERS and source_id not in MODEL_TIERS:
        return None

    sql = (
        "SELECT source_id, context_id, ticker, category_tier, "
        "within_tier_score, state, sample_size, "
        "confidence_band_low, confidence_band_high "
        "FROM source_weights WHERE source_id = ? AND context_id = ? "
        "AND ticker = ?"
    )

    # Step 1: most specific.
    if context_id != GLOBAL_KEY or ticker != GLOBAL_KEY:
        row = conn.execute(sql, (source_id, context_id, ticker)).fetchone()
        if row:
            return _row_to_dict(row)

    # Step 2: context default (drop ticker specificity).
    if context_id != GLOBAL_KEY:
        row = conn.execute(
            sql, (source_id, context_id, GLOBAL_KEY)
        ).fetchone()
        if row:
            return _row_to_dict(row)

    # Step 3: global default.
    row = conn.execute(
        sql, (source_id, GLOBAL_KEY, GLOBAL_KEY)
    ).fetchone()
    if row:
        return _row_to_dict(row)

    return None


def list_active_sources_for_lane(
    conn: sqlite3.Connection,
    lane: str,
    context_id: str = GLOBAL_KEY,
    ticker: str = GLOBAL_KEY,
) -> list[dict]:
    """Return all sources currently 'active' for a data lane (news,
    social, filings, ...). Filters out 'watched' and 'removed' sources.
    Sorted by category_tier ascending (A first), then within-tier
    score descending (best first).

    Per-(context, ticker) specificity is honored via get_source_weight's
    fallback chain — each source is resolved using the same 3-step rule.
    """
    members = LANE_SOURCES.get(lane, [])
    out: list[dict] = []
    for sid in members:
        info = get_source_weight(conn, sid, context_id, ticker)
        if info is None:
            continue
        if info.get('state') != 'active':
            continue
        out.append(info)

    # Tier 'A' < 'B' < 'C' < 'D' alphabetically — we want A first.
    # Within a tier, lower within-tier score = better quality (1=best,
    # 15=worst). Sort ascending on score so the strongest source leads.
    out.sort(
        key=lambda r: (r.get('category_tier', 'Z'),
                       r.get('within_tier_score', 99)),
    )
    return out


def list_active_models(
    conn: sqlite3.Connection,
    context_id: str = GLOBAL_KEY,
    ticker: str = GLOBAL_KEY,
) -> list[dict]:
    """v4.14.3 — Return all registered AI models currently 'active'.
    Mirrors list_active_sources_for_lane but for tier-M sources.

    Sorted by within-tier score ascending (best first). Skips models
    with state != 'active' so the prompt-side caller never sees a
    watched/removed model in the active list.
    """
    out: list[dict] = []
    for model_id in MODEL_TIERS.keys():
        info = get_source_weight(conn, model_id, context_id, ticker)
        if info is None:
            continue
        if info.get('state') != 'active':
            continue
        out.append(info)
    out.sort(key=lambda r: r.get('within_tier_score', 99))
    return out


# ─── Helpers for prompt-construction integration ──────────────────────

def weights_are_uniform(weights: list[dict]) -> bool:
    """True if every weight in the list shares the same within-tier
    score (i.e. nothing yet differentiates them, so rendering a
    'source quality' line is just noise)."""
    if not weights:
        return True
    scores = {w.get('within_tier_score') for w in weights}
    return len(scores) <= 1


def render_source_quality_line(
    weights: list[dict],
    *,
    when_uniform_label: str = "equal (insufficient data yet)",
) -> Optional[str]:
    """Build a one-line summary suitable for a prompt block header.

    Stage 6 contract: callers wire this up but the renderer is allowed
    to return None (or the 'equal' label) when scores are uniform.
    Stage 6 ships with all scores at 5, so this returns None today.
    Once future stages start moving scores, this surface goes live
    without further wiring.
    """
    if not weights:
        return None
    if weights_are_uniform(weights):
        # Stage 6: uniform => render nothing. Keeps existing prompts
        # byte-identical today. Future stages can flip this to return
        # `f"Source quality: {when_uniform_label}"` if visibility is
        # wanted.
        return None

    # Future-state path (no live consumer yet, but the shape is here
    # so stage 7+ can light it up without re-architecting).
    parts = []
    for w in weights:
        parts.append(
            f"{w['source_id']}={w['category_tier']}/"
            f"{w['within_tier_score']}"
        )
    return "Source quality: " + ", ".join(parts)
