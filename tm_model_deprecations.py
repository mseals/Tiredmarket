"""
tm_model_deprecations.py — known-deprecated model migration map (v4.13.58)

What this is:
    A small registry of "this model is deprecated, here's the
    recommended replacement." When the app starts (or when a provider
    is edited), the smart router consults this to decide whether to
    auto-skip a deprecated model (when migration target is known) or
    just warn (when no replacement is mapped).

Why this exists:
    Free AI providers rotate their available models constantly. Today
    SambaNova warned that Llama 3.1 8B will be deprecated. If your
    config still pointed at it, scans would silently 404 forever.
    The smart router catches that reactively (after 3 errors → red),
    but proactive detection is way better:
      - Tells you BEFORE you waste a scan
      - Suggests the right replacement
      - Lets you fix in one click instead of debugging a confused error

How callers use it:
    from tm_model_deprecations import lookup
    info = lookup(provider_preset='sambanova', model='Llama 3.1 8B')
    if info:
        if info['replacement']:
            # auto-skip + warn ("Use {replacement}")
        else:
            # warn-only, no known replacement

The map is intentionally LOW-fidelity (just exact-match strings), not
fuzzy matching. If a user has 'llama-3.1-8b' (lowercase) and the map
has 'Llama 3.1 8B', they won't match. That's a feature: false-positive
deprecation warnings are worse than false-negatives, and exact matches
are safer than guesses.

Adding entries:
    Just edit MIGRATIONS below. Each entry is keyed by (preset_id,
    model_name). Preset ids should match what's in tm_api_providers.
    'replacement' is None if the user must pick manually.
"""

from __future__ import annotations
from typing import Optional


# ─── Known deprecations ────────────────────────────────────────────────
# Format: (preset_id, model_name) -> {replacement, replacement_note,
#                                       deprecation_date, source}
# replacement: model name string the user should switch TO (None = no auto-fix)
# replacement_note: optional human-readable context shown in warning
# deprecation_date: ISO date — informational only
# source: where we learned about this (URL or "vendor email")

MIGRATIONS: dict[tuple, dict] = {
    # ── SambaNova (per their May 7 2026 deprecation email) ────────────
    ('sambanova', 'DeepSeek V3.1 Terminus'): {
        'replacement': 'DeepSeek-V3.1',
        'replacement_note': 'Same model family, current production version',
        'deprecation_date': '2026-04-06',
        'source': 'SambaNova Cloud deprecation email',
    },
    ('sambanova', 'DeepSeek V3 0324'): {
        'replacement': 'DeepSeek-R1-0528',
        'replacement_note': 'Recommended successor by SambaNova',
        'deprecation_date': '2026-04-14',
        'source': 'SambaNova Cloud deprecation email',
    },
    ('sambanova', 'DeepSeek R1-0528'): {
        # Itself was deprecated AFTER recommendation as the v3-0324
        # replacement. Recommend the current production DeepSeek line.
        'replacement': 'DeepSeek-V3.1',
        'replacement_note': 'R1-0528 deprecated; switch to current V3.1',
        'deprecation_date': '2026-04-14',
        'source': 'SambaNova Cloud deprecation email',
    },
    ('sambanova', 'Llama 3.1 8B'): {
        'replacement': 'Meta-Llama-3.3-70B-Instruct',
        'replacement_note': 'Larger but more capable; SambaNova recommends',
        'deprecation_date': '2026-04-14',
        'source': 'SambaNova Cloud deprecation email',
    },
    ('sambanova', 'Llama 3.3 Swallow'): {
        'replacement': None,  # No direct replacement
        'replacement_note': ('Removed without direct replacement. '
                              'Consider Meta-Llama-3.3-70B-Instruct.'),
        'deprecation_date': '2026-04-06',
        'source': 'SambaNova Cloud deprecation email',
    },
    ('sambanova', 'Qwen3 235B'): {
        'replacement': 'MiniMax M2.5',
        'replacement_note': 'Recommended replacement by SambaNova',
        'deprecation_date': '2026-04-06',
        'source': 'SambaNova Cloud deprecation email',
    },
    ('sambanova', 'Qwen3 32B'): {
        'replacement': None,
        'replacement_note': ('Removed without replacement. Consider '
                              'gpt-oss-120b or Meta-Llama-3.3-70B-Instruct.'),
        'deprecation_date': '2026-04-06',
        'source': 'SambaNova Cloud deprecation email',
    },
    ('sambanova', 'E5 Mistral'): {
        'replacement': None,
        'replacement_note': ('Available only on SambaStack/SambaManaged; '
                              'no Cloud replacement.'),
        'deprecation_date': '2026-04-06',
        'source': 'SambaNova Cloud deprecation email',
    },
    # ── Groq (announced 2026-06-17) ──────────────────────────────────
    # 70b-versatile / 8b-instant / qwen3-32b / llama-4-scout retired.
    # Survivors: openai/gpt-oss-120b, openai/gpt-oss-20b, qwen/qwen3.6-27b.
    ('groq', 'llama-3.3-70b-versatile'): {
        'replacement': 'openai/gpt-oss-120b',
        'replacement_note': 'Most capable Groq survivor (gpt-oss-120b).',
        'deprecation_date': '2026-06-17',
        'source': 'Groq deprecation announcement 2026-06-17',
    },
    ('groq', 'llama-3.1-8b-instant'): {
        'replacement': 'openai/gpt-oss-20b',
        'replacement_note': 'High-throughput Groq survivor (gpt-oss-20b).',
        'deprecation_date': '2026-06-17',
        'source': 'Groq deprecation announcement 2026-06-17',
    },
    ('groq', 'qwen/qwen3-32b'): {
        'replacement': 'qwen/qwen3.6-27b',
        'replacement_note': 'Current Qwen line on Groq; or gpt-oss-120b.',
        'deprecation_date': '2026-06-17',
        'source': 'Groq deprecation announcement 2026-06-17',
    },
    ('groq', 'meta-llama/llama-4-scout-17b-16e-instruct'): {
        'replacement': 'openai/gpt-oss-120b',
        'replacement_note': 'Use gpt-oss-120b or qwen3.6-27b.',
        'deprecation_date': '2026-06-17',
        'source': 'Groq deprecation announcement 2026-06-17',
    },
}

# Common error message patterns that indicate a deprecated model.
# When the smart router sees these in a 404/400 response body, it
# treats the failure as a deprecation event (logs it differently,
# triggers the migration suggestion path).
DEPRECATION_ERROR_PATTERNS = (
    'model not found',
    'model_not_found',
    'no such model',
    'unsupported model',
    'this model has been deprecated',
    'model is no longer available',
    '404',  # last-resort catch-all
)


def lookup(preset: str, model: str) -> Optional[dict]:
    """Return migration info if (preset, model) is known-deprecated.

    Args:
        preset: provider preset id (e.g. 'sambanova', 'groq')
        model: model name string as it appears in user config

    Returns:
        Migration dict with keys {replacement, replacement_note,
        deprecation_date, source}, or None if not in the map.

    v4.13.58: handles common variations in how the model name is
    written. Vendors sometimes use spaces ("Llama 3.1 8B"), sometimes
    hyphens ("Llama-3.1-8B"), sometimes mixed case. We normalize both
    sides before comparing so any of these match the same map entry:
        "DeepSeek V3.1 Terminus"
        "deepseek-v3.1-terminus"
        "DEEPSEEK_V3.1_TERMINUS"
        "DeepSeek-V3.1-Terminus"
    """
    if not preset or not model:
        return None

    def _normalize(s: str) -> str:
        # Lower, strip, collapse spaces/hyphens/underscores into single space
        out = s.strip().lower()
        for sep in ('-', '_', '.', ' '):
            out = out.replace(sep, ' ')
        # Collapse multiple spaces
        while '  ' in out:
            out = out.replace('  ', ' ')
        return out.strip()

    target_preset = preset.strip().lower()
    target_model_norm = _normalize(model)

    # Direct case-sensitive match first (fast path)
    key = (target_preset, model.strip())
    info = MIGRATIONS.get(key)
    if info is not None:
        return info

    # Fall through: normalize both sides of every entry and compare
    for (p, m), v in MIGRATIONS.items():
        if p == target_preset and _normalize(m) == target_model_norm:
            return v
    return None


def looks_like_deprecation_error(error_text: str) -> bool:
    """True if the error message string looks like a deprecation event
    (404, model not found, etc.). Used by the smart router to upgrade
    a generic failure into a deprecation-aware one."""
    if not error_text:
        return False
    et = error_text.lower()
    return any(p in et for p in DEPRECATION_ERROR_PATTERNS)


def all_known_deprecated() -> list[tuple]:
    """Return list of (preset, model) tuples that are known deprecated.
    Used at app startup for a one-shot config audit."""
    return list(MIGRATIONS.keys())
