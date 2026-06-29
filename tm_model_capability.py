"""tm_model_capability.py — v4.14.5.62-model-routing.

DATA-driven per-model capability ranking + scan/lookup tier, used by:
  - tm_ai_router._rotation_pick_model  → exclude lookup-tier models (e.g.
    gemini-2.5-pro) from the high-volume SCAN rotation.
  - the AI-question (teacher) ladder    → order installed models smartest →
    dumbest (capability_rank, lower = smarter) and step down on cap.

This is intentionally a small editable table (not learned): models change
slowly and a rough order is robust (models within ~2-3% MMLU are near-equal).
Keys are matched case-insensitively against the provider model STRING as it
appears in a provider's `models[]` / `model` (substring fallback so e.g.
"models/gemini-2.5-pro" still matches "gemini-2.5-pro").

To maintain: edit _CAPABILITY below. Unknown models default to mid-rank /
scan-tier (safe: usable everywhere, ranked in the middle).
"""
from __future__ import annotations

# rank: lower = smarter (1 = top reasoning).  tier: "scan" = usable on the
# high-volume scan/fill rotation AND everywhere else; "lookup" = high-
# intelligence / rate-capped → EXCLUDED from scan, kept for Look Up /
# AI-question / Verify only.
_CAPABILITY = {
    # ── lookup-tier (top reasoning, tight rate caps → NOT for scan) ──
    'gemini-2.5-pro':            {'rank': 1,  'tier': 'lookup'},
    # v4.14.5.62-gemini-pro-tier: gemini-1.5-pro is the older Pro line but
    # still a tight free-tier-RPM model — same Pro-out-of-scan rationale as
    # 2.5-pro. lookup-tier → excluded from the high-volume scan rotation, kept
    # for Look Up / Ask-AI / Verify. The substring match also covers the
    # 'gemini-1.5-pro-latest' registry alias. (Auto-populate seeds it into
    # Google's curated subset, so without this a fresh user's SCAN could rotate
    # into it and hit the exact 429s the Pro-out-of-scan fix solved.)
    'gemini-1.5-pro':            {'rank': 2,  'tier': 'lookup'},
    # ── scan-tier, smartest → dumbest ──
    'gpt-4o':                    {'rank': 2,  'tier': 'scan'},
    'gpt-oss-120b':              {'rank': 3,  'tier': 'scan'},
    # Groq 2026-06-17 survivors (substring-matched: 'openai/gpt-oss-120b' etc.).
    'gpt-oss-20b':               {'rank': 6,  'tier': 'scan'},
    'qwen3.6-27b':               {'rank': 6,  'tier': 'scan'},
    'deepseek-v3.1':             {'rank': 4,  'tier': 'scan'},
    'llama-3.3-70b-versatile':   {'rank': 5,  'tier': 'scan'},
    'llama-3.3-70b':             {'rank': 5,  'tier': 'scan'},
    'mistral-medium-latest':     {'rank': 6,  'tier': 'scan'},
    'mistral-medium':            {'rank': 6,  'tier': 'scan'},
    'gemini-2.5-flash':          {'rank': 7,  'tier': 'scan'},
    'glm-4.5-flash':             {'rank': 8,  'tier': 'scan'},
    'gemini-2.5-flash-lite':     {'rank': 9,  'tier': 'scan'},
    'mistral-small-latest':      {'rank': 10, 'tier': 'scan'},
    'mistral-small':             {'rank': 10, 'tier': 'scan'},
    'mixtral-8x7b-32768':        {'rank': 11, 'tier': 'scan'},
    'llama-3.1-8b-instant':      {'rank': 12, 'tier': 'scan'},
    'llama-3.1-8b':              {'rank': 12, 'tier': 'scan'},
}

DEFAULT_RANK = 50          # unknown model → mid (ranked below all known)
DEFAULT_TIER = 'scan'      # unknown model → usable everywhere (safe)

# Call types treated as high-volume SCAN (lookup-tier excluded from rotation).
SCAN_CALL_TYPES = frozenset({'scan'})


def _lookup(model_str):
    """Return the _CAPABILITY entry for a model string (exact-lower first,
    then substring), or None. Never raises."""
    try:
        s = str(model_str or '').strip().lower()
        if not s:
            return None
        if s in _CAPABILITY:
            return _CAPABILITY[s]
        # substring fallback: a configured string like
        # "models/gemini-2.5-pro" or "azureml://.../gpt-4o/..." still maps.
        for key, val in _CAPABILITY.items():
            if key in s:
                return val
        return None
    except Exception:
        return None


def model_tier(model_str) -> str:
    """'scan' (usable everywhere) or 'lookup' (high-intel, scan-excluded).
    Unknown → DEFAULT_TIER ('scan'). Never raises."""
    e = _lookup(model_str)
    return (e or {}).get('tier', DEFAULT_TIER)


def model_capability_rank(model_str) -> int:
    """Capability rank (lower = smarter). Unknown → DEFAULT_RANK. Never raises."""
    e = _lookup(model_str)
    try:
        return int((e or {}).get('rank', DEFAULT_RANK))
    except Exception:
        return DEFAULT_RANK


def is_scan_eligible_model(model_str, call_type) -> bool:
    """True if `model_str` may be used on a call of `call_type`. On a SCAN
    call_type, lookup-tier models are excluded; on every other call_type
    (lookup/teacher/verify/consensus), all models are eligible. Never raises."""
    try:
        if str(call_type or '').strip().lower() in SCAN_CALL_TYPES:
            return model_tier(model_str) != 'lookup'
        return True
    except Exception:
        return True
