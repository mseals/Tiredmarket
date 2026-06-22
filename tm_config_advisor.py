"""Provider Config Advisor — v4.14.6.57

Deterministic, rules-based engine that inspects the user's provider
configuration and emits recommend-only suggestions for improving setup
quality.

NO AI / NO LLM / NO NETWORK. Pure config inspection + a rule table.
Runs instantly on weak end-user hardware. Reads files, never mutates.

Rules implemented (approved by the user, v4.14.6.57):
    R1  AI provider has a key but is disabled              -> suggest
    R2  AI provider enabled but key is empty               -> warn
    R3  Only one news source enabled                       -> suggest
    R4  Only one fundamentals source enabled               -> suggest
    R5  Only one earnings source enabled                   -> suggest
    R6  Zero AI providers enabled                          -> warn
    R7  Only one AI provider enabled (thin consensus)      -> suggest
    R8  Cerebras enabled (context-guard informational)     -> info
    R9  Default model is known-deprecated                  -> warn
    R10 GitHub Models endpoint contains {ACCOUNT_ID}       -> warn
    F2  GitHub Models enabled (50/day cap reminder)        -> info

Key values are NEVER read for content — only presence (truthiness of
.strip()). Recommend-only — no config is ever mutated.

Public entrypoint:
    provider_config_recommendations(api_providers_path=None,
                                     data_providers_path=None,
                                     config_path=None)
        -> list[tuple[str, str]]   # [(severity, message), ...]

Severity values: 'info', 'suggest', 'warn'.

On missing or malformed config files the engine returns a single
informational note rather than crashing. An entirely-empty config
produces sensible onboarding output (e.g. R6 "no AI providers enabled,
add Groq/Gemini/Mistral").
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


# Severity levels (display order; lowest-severity first).
SEVERITY_INFO = 'info'
SEVERITY_SUGGEST = 'suggest'
SEVERITY_WARN = 'warn'

# Data types that should normally have at least 2 enabled sources for
# resilience. Tied to DATA_TYPES in tm_data_providers.py.
_RESILIENCE_TYPES = ('news', 'fundamentals', 'earnings')


def _load_json(path) -> Optional[dict]:
    """Read a JSON file safely. Returns the parsed object or None.

    Never raises. Treats missing file, unreadable file, or invalid JSON
    as 'no config' (None). Caller decides what to do with that.
    """
    if path is None:
        return None
    try:
        p = Path(path)
        if not p.exists():
            return None
        with open(p, encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _ai_providers(data: Optional[dict]) -> list[dict]:
    """Extract the AI-provider record list from api_providers.json shape."""
    if not isinstance(data, dict):
        return []
    out = data.get('providers')
    if not isinstance(out, list):
        return []
    return [p for p in out if isinstance(p, dict)]


def _data_providers(data: Optional[dict]) -> list[dict]:
    """Extract the data-provider record list from data_providers.json."""
    if not isinstance(data, dict):
        return []
    out = data.get('providers')
    if not isinstance(out, list):
        return []
    return [p for p in out if isinstance(p, dict)]


def _has_key(provider: dict) -> bool:
    """True if the provider record holds a non-empty key.

    PRESENCE ONLY — never inspects, logs, or returns the actual value.
    Accepts both `api_key` (AI side) and `key` (data side) field names.
    """
    for fld in ('api_key', 'key'):
        v = provider.get(fld)
        if isinstance(v, str) and v.strip():
            return True
    return False


def _is_ai_enabled(provider: dict) -> bool:
    return bool(provider.get('enabled', False))


def _is_data_enabled_for(provider: dict, data_type: str) -> bool:
    """True if this data provider is enabled, usable, and serves the type."""
    if not provider.get('enabled', False):
        return False
    needs_key = bool(provider.get('needs_key', False))
    if needs_key and not _has_key(provider):
        return False
    priorities = provider.get('priorities')
    if not isinstance(priorities, dict):
        return False
    pri = priorities.get(data_type)
    try:
        return pri is not None and int(pri) > 0
    except (TypeError, ValueError):
        return False


def _provider_display(provider: dict) -> str:
    """Human-friendly name. Falls back gracefully if fields are missing."""
    for fld in ('display_name', 'name', 'id'):
        v = provider.get(fld)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return '(unnamed provider)'


def _deprecation_lookup(preset: str, model: str):
    """Soft-import tm_model_deprecations. Returns the migration dict or
    None. Never raises; if the module isn't on the path, returns None."""
    if not preset or not model:
        return None
    try:
        import tm_model_deprecations as _dep
        info = _dep.lookup(preset, model)
        return info if isinstance(info, dict) else None
    except Exception:
        return None


def provider_config_recommendations(
        api_providers_path=None,
        data_providers_path=None,
        config_path=None) -> list[tuple[str, str]]:
    """Run the approved rule table against the current config files.

    All three paths are optional; pass None to skip a file (it is then
    treated as a missing/empty config, which still produces sensible
    onboarding output for the others). Missing files do NOT raise.

    Returns:
        A list of (severity, message) tuples in display order: warns
        first, then suggests, then infos. The list is empty only if the
        config is fully populated and clean.
    """
    api_data = _load_json(api_providers_path)
    data_data = _load_json(data_providers_path)
    # config.json reserved for future rules (refresh-rate / ticker count)
    _cfg = _load_json(config_path)

    ai_providers = _ai_providers(api_data)
    dp_providers = _data_providers(data_data)

    # Cannot-read note: only emit if BOTH provider files were missing
    # (i.e. a brand-new install with no on-disk config at all). This is
    # the Public-derived install case; the user needs a single sign-post
    # that config is empty rather than a flood of warnings.
    findings: list[tuple[str, str]] = []
    if not ai_providers and api_providers_path and not Path(
            api_providers_path).exists():
        findings.append((SEVERITY_INFO,
            "No api_providers.json yet — copy api_providers.example.json "
            "to api_providers.json and add your keys to get started."))
    if not dp_providers and data_providers_path and not Path(
            data_providers_path).exists():
        findings.append((SEVERITY_INFO,
            "No data_providers.json yet — the app will load built-in "
            "defaults (Yahoo, EDGAR, FRED, etc.) on first launch."))

    # ── AI-side rules (R1, R2, R6, R7, R8, R9, R10, F2) ──────────────
    ai_enabled = [p for p in ai_providers if _is_ai_enabled(p)]

    # R6 — zero enabled AI providers
    if not ai_enabled:
        findings.append((SEVERITY_WARN,
            "No cloud AI providers are enabled. Consensus has no voices. "
            "Enable Groq, Gemini, or Mistral (free tiers) for redundancy."))
    elif len(ai_enabled) == 1:
        # R7 — only one enabled AI voice
        only = _provider_display(ai_enabled[0])
        findings.append((SEVERITY_SUGGEST,
            f"Only one AI voice enabled ({only}). Consensus has no "
            "diversity — adding a 2nd or 3rd provider dramatically improves "
            "verdict quality."))

    for p in ai_providers:
        name = _provider_display(p)
        preset = (p.get('preset') or '').strip().lower()
        model = (p.get('model') or '').strip()
        enabled = _is_ai_enabled(p)
        has_key = _has_key(p)

        # R1 — key present but disabled
        if has_key and not enabled:
            findings.append((SEVERITY_SUGGEST,
                f"{name}: you have an API key but the provider is disabled. "
                "Enable it to put it to work."))

        # R2 — enabled but no key (AI providers always need keys)
        if enabled and not has_key:
            findings.append((SEVERITY_WARN,
                f"{name}: enabled but has no API key. Paste a key or "
                "disable the provider."))

        # R8 — Cerebras informational (context-guarded since v52)
        if enabled and preset == 'cerebras':
            findings.append((SEVERITY_INFO,
                "Cerebras is enabled — the app context-guards it to 7,500 "
                "tokens. It may refuse on very long prompts; that's by "
                "design (v52 guard)."))

        # F2 — GitHub Models 50/day reminder
        if enabled and preset in ('github_models', 'github'):
            findings.append((SEVERITY_INFO,
                "GitHub Models is enabled — daily cap is ~50 calls per "
                "user. Don't rely on it as your only consensus voice; "
                "the cap benches the whole provider when hit."))

        # R10 — GitHub Models placeholder still in endpoint
        if (enabled and preset in ('github_models', 'github')):
            endpoint = (p.get('endpoint') or '')
            if isinstance(endpoint, str) and '{ACCOUNT_ID}' in endpoint:
                findings.append((SEVERITY_WARN,
                    f"{name}: endpoint still contains the {{ACCOUNT_ID}} "
                    "placeholder. Replace it with your Azure account ID "
                    "or the provider won't work."))

        # R9 — deprecated default model
        if enabled and preset and model:
            mig = _deprecation_lookup(preset, model)
            if mig is not None:
                rep = mig.get('replacement') or '(see provider docs)'
                findings.append((SEVERITY_WARN,
                    f"{name}: default model '{model}' is deprecated. "
                    f"Switch to '{rep}'."))

    # ── Data-side rules (R3, R4, R5) ─────────────────────────────────
    for data_type in _RESILIENCE_TYPES:
        enabled_count = sum(
            1 for p in dp_providers if _is_data_enabled_for(p, data_type))
        label = data_type
        if enabled_count == 0:
            findings.append((SEVERITY_WARN,
                f"No enabled {label} source. The app will have no "
                f"{label} data. Enable a provider that serves "
                f"{label} in the Data Providers config."))
        elif enabled_count == 1:
            findings.append((SEVERITY_SUGGEST,
                f"Only one {label} source enabled. No fallback if it "
                f"stumbles. Consider enabling a backup."))

    # ── Sort: warns first, then suggests, then infos. Stable within. ─
    order = {SEVERITY_WARN: 0, SEVERITY_SUGGEST: 1, SEVERITY_INFO: 2}
    findings.sort(key=lambda x: order.get(x[0], 9))
    return findings


def severity_icon(severity: str) -> str:
    """Return a short UI-friendly marker for a severity level."""
    if severity == SEVERITY_WARN:
        return '⚠'   # ⚠
    if severity == SEVERITY_SUGGEST:
        return '•'   # •
    return 'ℹ'        # ℹ


def has_warnings(findings) -> bool:
    """True if any finding is severity 'warn'. Used for warn-only gating."""
    try:
        return any(sev == SEVERITY_WARN for sev, _msg in findings)
    except Exception:
        return False
