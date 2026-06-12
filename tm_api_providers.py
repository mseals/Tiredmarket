"""
Tired Market — API Provider registry (v4.13.40, Phase 1: storage only)

Stores configuration for cloud LLM providers (OpenAI, Groq, Anthropic, Google
Gemini, Mistral, OpenRouter, etc.) so they can later be plugged into the
consensus runner alongside local Ollama models.

This module ONLY handles configuration — registry CRUD, preset definitions,
and persistence to data/api_providers.json. The actual API-calling logic
will land in Phase 2 (v4.13.41) when we wire providers into tm_consensus.

DESIGN NOTES:

1. Keys live in their own file (data/api_providers.json), separate from
   config.json. That makes it easier to .gitignore or share config without
   leaking credentials, and easier to back up / restore providers as a unit.

2. Each provider has a "preset" picker. Picking a known preset auto-fills
   the endpoint URL and tags the request format ('openai_compat',
   'anthropic', 'google'). Custom lets you point at anything that speaks
   OpenAI-compatible chat completions (vLLM, LM Studio, LiteLLM, etc.).

3. Schema is intentionally flat. No nested options yet. Phase 2 will add
   per-provider tuning if needed (temperature, max tokens, system prompt
   override, path-specific overrides), but for v1 we keep it minimal.

4. Plain-text storage. the user's Tired Market install is on his personal
   Windows machine -- single user, local files. Encryption would add
   complexity without real security gain in this context. The data file
   is given an explicit warning header so the user knows not to share it.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path


# ─── Provider preset definitions ────────────────────────────────────────
# Each preset describes how to talk to a provider's chat completions API.
# 'format' tells the future caller which request/response shape to use.
# 'default_endpoint' is pre-filled in the UI but editable.
# 'note' is a hint shown in the form to help the user pick the right preset.

PRESETS = {
    'openai': {
        'name': 'OpenAI',
        'format': 'openai_compat',
        'default_endpoint': 'https://api.openai.com/v1/chat/completions',
        'default_model': 'gpt-4o-mini',
        'sample_models': ['gpt-4o-mini', 'gpt-4o', 'gpt-4-turbo'],
        'note': 'Paid. $5 free trial credit on signup. Reliable.',
        'signup_url': 'https://platform.openai.com/signup',
        # v4.13.55b: conservative default daily cap. User can override
        # in API Providers dialog. 0 = no cap. Caps protect against
        # quota burn on free tiers and runaway $$ on paid tiers.
        'default_max_per_day': 100,
    },
    'groq': {
        'name': 'Groq',
        'format': 'openai_compat',
        'default_endpoint': 'https://api.groq.com/openai/v1/chat/completions',
        'default_model': 'llama-3.1-8b-instant',
        'sample_models': [
            'llama-3.1-8b-instant',  # Highest quota: 14,400 RPD
            'llama-3.3-70b-versatile',  # 1,000 RPD
            'mixtral-8x7b-32768',
            'gemma2-9b-it',
        ],
        'note': ('Free tier with model-dependent quotas. Default model '
                 '(llama-3.1-8b-instant) gets 14,400 RPD; larger models '
                 'like 70B get 1,000 RPD.'),
        'signup_url': 'https://console.groq.com/keys',
        # v4.13.58.1: Quota varies dramatically by model (verified
        # May 2026 against console.groq.com/docs/rate-limits):
        #   - llama-3.1-8b-instant: 14,400 RPD
        #   - llama-3.3-70b-versatile: 1,000 RPD
        # Defaulting to 5000 splits the difference. The smart router's
        # observed-quota learning will tune this for the actual
        # configured model after first 429.
        'default_max_per_day': 5000,
    },
    'anthropic': {
        'name': 'Anthropic Claude',
        'format': 'anthropic',
        'default_endpoint': 'https://api.anthropic.com/v1/messages',
        'default_model': 'claude-haiku-4-5',
        'sample_models': [
            'claude-haiku-4-5',
            'claude-sonnet-4-6',
            'claude-opus-4-7',
        ],
        'note': 'Paid. Best reasoning quality currently. Free trial available.',
        'signup_url': 'https://console.anthropic.com/',
        # Conservative for paid model — protects against $$ burn
        'default_max_per_day': 50,
    },
    'google': {
        'name': 'Google Gemini',
        'format': 'google',
        'default_endpoint': (
            'https://generativelanguage.googleapis.com/v1beta/models'),
        'default_model': 'gemini-2.0-flash',
        'sample_models': [
            'gemini-2.0-flash',
            'gemini-1.5-pro',
            'gemini-1.5-flash',
        ],
        'note': 'Free tier available. Long context window.',
        'signup_url': 'https://aistudio.google.com/app/apikey',
        # v4.13.58.1: Real Gemini free-tier RPD is 1500 on most flash
        # models (verified May 2026 against AI Studio docs). Earlier
        # 500 was unnecessarily conservative.
        'default_max_per_day': 1500,
    },
    'mistral': {
        'name': 'Mistral La Plateforme',
        'format': 'openai_compat',
        'default_endpoint': 'https://api.mistral.ai/v1/chat/completions',
        'default_model': 'mistral-small-latest',
        'sample_models': [
            'mistral-small-latest',
            'mistral-large-latest',
            'open-mistral-nemo',
        ],
        'note': 'Free tier available. EU-based.',
        'signup_url': 'https://console.mistral.ai/',
        # v4.13.58.1: Mistral free tier is TOKEN-capped (1B/month), not
        # request-capped. Earlier value of 50 was a wrong guess that
        # hit users at 15/day after the scan cap_factor (0.3). Real
        # binding constraint is RPS + TPM, not RPD.
        #
        # v4.14.5.14a.3 (2026-05-17): the "Mistral 300/300" exhaustion
        # observed was NOT a real Mistral daily limit. It is this
        # declared cap (was 1000) × the 'scan' call-type cap_factor
        # 0.3 (tm_ai_router POLICIES['scan'], which reserves 70% of
        # every provider's daily budget for high-value consensus
        # calls) = 300 effective scans/day. provider_health shows
        # observed_max_per_day=null for Mistral — it has NEVER hit a
        # real daily 429 wall, confirming 1000 was a conservative
        # internal guess, not a measured limit. Mistral has no
        # published RPD cap (token+RPS only). Raised to 5000 (parity
        # with Groq's identical "well above realistic, let the smart
        # router learn the real wall from 429s" rationale) so fill
        # mode gets 5000×0.3 = 1500 scan calls/day instead of 300.
        # The smart router's observed-quota learning still auto-
        # tightens this if Mistral ever returns sustained daily 429s.
        # NOTE: the deeper lever — the 0.3 scan cap_factor reserving
        # 70% for Layer 2 consensus that isn't built until
        # v4.14.5.14b — is flagged as a follow-up design decision
        # (changing it affects ALL providers; out of scope here).
        'default_max_per_day': 5000,
    },
    'openrouter': {
        'name': 'OpenRouter',
        'format': 'openai_compat',
        'default_endpoint': 'https://openrouter.ai/api/v1/chat/completions',
        'default_model': 'meta-llama/llama-3.3-70b-instruct:free',
        'sample_models': [
            'meta-llama/llama-3.3-70b-instruct:free',
            'google/gemini-2.0-flash-exp:free',
            'qwen/qwen-2.5-72b-instruct:free',
        ],
        'note': 'Aggregator routes to many providers. Some free models.',
        'signup_url': 'https://openrouter.ai/settings/keys',
        'default_max_per_day': 200,
    },
    'together': {
        'name': 'Together.ai',
        'format': 'openai_compat',
        'default_endpoint': 'https://api.together.xyz/v1/chat/completions',
        'default_model': 'meta-llama/Llama-3.3-70B-Instruct-Turbo-Free',
        'sample_models': [
            'meta-llama/Llama-3.3-70B-Instruct-Turbo-Free',
            'meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo',
        ],
        'note': 'Free tier with select models marked "-Free".',
        'signup_url': 'https://api.together.xyz/settings/api-keys',
        'default_max_per_day': 100,
    },
    # v4.14.5.14-ollama-purge-step4: the 'ollama' (local) preset was removed —
    # Ollama is retired; the app is cloud-only. (Cloud `qwen/...` model names
    # under the cloud presets are unrelated and stay.)
    # v4.13.55b: presets for the two providers users hit hardest:
    # Sambanova free is 20 req/day total (not 20 per minute as the name
    # suggests — confirmed via their docs). GitHub Models free is 50/day
    # per model per user. Both are way tighter than the generic 'custom'
    # default, which is why users hit them as walls. Caps are set just
    # under the server limit so we self-skip before the server 429s.
    'sambanova': {
        'name': 'SambaNova',
        'format': 'openai_compat',
        'default_endpoint': 'https://api.sambanova.ai/v1/chat/completions',
        'default_model': 'DeepSeek-V3.1',
        'sample_models': [
            'DeepSeek-V3.1',
            'Meta-Llama-3.3-70B-Instruct',
            'gpt-oss-120b',
        ],
        'note': ('Free tier is TIGHT — 20 calls/day total. Best used '
                 'as a "special occasion" voice (holdings consensus '
                 'only), not a regular fan-out provider.'),
        'signup_url': 'https://cloud.sambanova.ai/',
        'default_max_per_day': 15,  # 20 server cap, leave margin
    },
    'github_models': {
        'name': 'GitHub Models',
        'format': 'openai_compat',
        'default_endpoint': 'https://models.inference.ai.azure.com/chat/completions',
        'default_model': 'gpt-4o',
        'sample_models': ['gpt-4o', 'gpt-4o-mini', 'Phi-3.5-mini-instruct'],
        'note': ('Free tier is 50 calls/day per model per user. '
                 'Reasonable for moderate use. Requires GitHub PAT '
                 'with models scope.'),
        'signup_url': 'https://github.com/marketplace/models',
        'default_max_per_day': 40,  # 50 server cap, leave margin
    },
    'cerebras': {
        'name': 'Cerebras',
        'format': 'openai_compat',
        'default_endpoint': 'https://api.cerebras.ai/v1/chat/completions',
        'default_model': 'llama3.1-8b',
        'sample_models': ['llama3.1-8b', 'llama3.1-70b'],
        'note': ('Free tier: 1M tokens/day, 30 RPM. Very fast inference '
                 '(~1000+ tok/s on WSE-3 chips).'),
        'signup_url': 'https://cloud.cerebras.ai/',
        # v4.13.58.1: Cerebras free tier is TOKEN-capped (1M/day)
        # not request-capped. RPM is 30. No hard daily request count.
        # Verified May 2026 against inference-docs.cerebras.ai.
        # Set to 1500 — well above realistic single-day usage; smart
        # router's observed-quota learning handles the real ceiling.
        'default_max_per_day': 1500,
    },
    'custom': {
        'name': 'Custom (OpenAI-compatible)',
        'format': 'openai_compat',
        'default_endpoint': '',
        'default_model': '',
        'sample_models': [],
        'note': 'Any endpoint that speaks OpenAI-compatible chat completions.',
        'signup_url': '',
        # Custom = unknown tier. Conservative default but user should
        # set it explicitly based on their actual provider.
        'default_max_per_day': 100,
    },
    # ── v4.14.5.14b-prov: four researched free-tier providers
    #    (the user-approved). Data-only preset entries so the existing
    #    Recommended-card / quick-add path prefills them naturally.
    'cohere': {
        'name': 'Cohere',
        'format': 'openai_compat',
        'default_endpoint': 'https://api.cohere.com/compatibility/v1/chat/completions',
        'default_model': 'command-r-plus-08-2024',
        'sample_models': ['command-r-plus-08-2024', 'command-r-08-2024'],
        'note': 'Free tier: 1,000 calls/month, 20 RPM. Command R+ '
                '(grounded answers). OpenAI-compatible /compatibility/v1.',
        'signup_url': 'https://dashboard.cohere.com/api-keys',
        'default_max_per_day': 33,
    },
    'cloudflare': {
        'name': 'Cloudflare Workers AI',
        'format': 'openai_compat',
        # NOTE: replace {ACCOUNT_ID} with your Cloudflare account id
        # (Dashboard → Workers AI) before saving — the endpoint is
        # account-scoped, unlike the other providers.
        'default_endpoint': 'https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/v1/chat/completions',
        'default_model': '@cf/meta/llama-3.3-70b-instruct-fp8-fast',
        'sample_models': ['@cf/meta/llama-3.3-70b-instruct-fp8-fast',
                          '@cf/qwen/qwq-32b'],
        'note': 'Free tier: 10,000 Neurons/day, 40+ models. IMPORTANT: '
                'edit the endpoint and replace {ACCOUNT_ID} with your '
                'Cloudflare account id before saving.',
        'signup_url': 'https://dash.cloudflare.com/profile/api-tokens',
        'default_max_per_day': 200,
    },
    'nvidia': {
        'name': 'NVIDIA NIM',
        'format': 'openai_compat',
        'default_endpoint': 'https://integrate.api.nvidia.com/v1/chat/completions',
        'default_model': 'meta/llama-3.3-70b-instruct',
        'sample_models': ['meta/llama-3.3-70b-instruct',
                          'qwen/qwen3-235b-a22b'],
        'note': 'Free tier: 40 RPM. Llama 3.3 70B, Mistral Large, '
                'Qwen3 235B. OpenAI-compatible.',
        'signup_url': 'https://build.nvidia.com/explore/discover',
        'default_max_per_day': 200,
    },
    'zhipu': {
        'name': 'Zhipu AI (Z.AI)',
        'format': 'openai_compat',
        'default_endpoint': 'https://api.z.ai/api/paas/v4/chat/completions',
        'default_model': 'glm-4.7-flash',
        'sample_models': ['glm-4.7-flash', 'glm-4.5-flash'],
        'note': 'Free GLM-4.7-Flash / GLM-4.5-Flash, 200K context, '
                'English supported. OpenAI-compatible.',
        'signup_url': 'https://open.bigmodel.cn/usercenter/apikeys',
        'default_max_per_day': 200,
    },
}

# ── v4.14.5.14-rot Patch 1: curated per-provider rotation lists ──────
# Free-tier models worth rotating across. The multi-model Edit dialog
# shows these as CLICKABLE suggestions (it never auto-populates — the
# user opts in). Providers with per-MODEL free caps (Groq/GitHub/
# Gemini/Zhipu — see Patch 3) get the budget multiplier; the rest get
# consensus diversity only. Any preset not named here defaults to its
# existing `sample_models` so the dialog still has suggestions.
_RECOMMENDED_MODELS_V414ROT = {
    'groq': ['llama-3.3-70b-versatile', 'llama-3.1-8b-instant',
             'mixtral-8x7b-32768', 'gemma2-9b-it',
             'deepseek-r1-distill-llama-70b'],
    'google': ['gemini-2.5-flash-lite', 'gemini-2.5-flash',
               'gemini-2.5-pro'],
    'mistral': ['mistral-small-latest', 'mistral-medium-latest',
                'ministral-8b-latest'],
    'zhipu': ['glm-4.7-flash', 'glm-4.5-flash'],
    'github_models': ['gpt-4o', 'gpt-4o-mini',
                      'Phi-3.5-mini-instruct'],
    'cohere': ['command-r-plus-08-2024', 'command-r-08-2024'],
    'cerebras': ['llama3.1-8b', 'llama3.1-70b'],
    'nvidia': ['meta/llama-3.3-70b-instruct', 'qwen/qwen3-235b-a22b'],
    'cloudflare': ['@cf/meta/llama-3.3-70b-instruct-fp8-fast',
                   '@cf/qwen/qwq-32b'],
    'sambanova': ['DeepSeek-V3.1', 'Meta-Llama-3.3-70B-Instruct',
                  'gpt-oss-120b'],
    'openrouter': ['meta-llama/llama-3.3-70b-instruct:free',
                   'google/gemini-2.0-flash-exp:free',
                   'qwen/qwen-2.5-72b-instruct:free'],
}
for _pk, _pdef in PRESETS.items():
    if not isinstance(_pdef, dict):
        continue
    _pdef['recommended_models'] = list(
        _RECOMMENDED_MODELS_V414ROT.get(
            _pk, _pdef.get('sample_models', []) or []))


def list_presets() -> list[tuple[str, str]]:
    """Return [(preset_id, display_name), ...] for the UI dropdown."""
    return [(pid, pdef['name']) for pid, pdef in PRESETS.items()]


def get_preset(preset_id: str) -> dict | None:
    """Return preset definition or None."""
    return PRESETS.get((preset_id or '').lower())


def curated_models(preset_id: str, cap: int = 3) -> list:
    """v4.14.5.62-autopopulate-models: the clean multi-model subset to seed a
    new provider's models[] with on add — default_model + the next best
    sample_models, deduped (case-insensitive), order-preserving, capped to
    `cap`.

    Drawn ONLY from the curated PRESETS (default_model + sample_models), which
    are hand-picked CHAT models — NEVER the raw /v1/models catalog. So
    auto-populating from this can't introduce non-chat (embedding/whisper/etc.)
    models into rotation. Returns [] for a preset with no curated models
    (generic 'custom') — the caller leaves models[] as-is in that case and the
    user supplies the model. Returns what's available (no padding) when a
    preset has fewer than `cap` samples."""
    pdef = PRESETS.get((preset_id or '').lower(), {}) or {}
    candidates = []
    dm = (pdef.get('default_model') or '').strip()
    if dm:
        candidates.append(dm)
    for m in (pdef.get('sample_models') or []):
        candidates.append(str(m).strip())
    out, seen = [], set()
    for m in candidates:
        if m and m.lower() not in seen:
            seen.add(m.lower())
            out.append(m)
        if len(out) >= cap:
            break
    return out


def mask_key(key: str | None) -> str:
    """Return a display-safe representation of an API key."""
    if not key:
        return ''
    k = str(key)
    if len(k) <= 10:
        return '•' * len(k)
    return f"{k[:6]}…{'•' * 8}…{k[-2:]}"


class APIProviderRegistry:
    """Loads, saves, and manipulates the API provider list.

    Thread-safe via a single lock. CRUD operations persist immediately to
    avoid losing edits if the app crashes. The registry can be passed
    around but should generally be a single instance per app.
    """

    # v4.14.0 stage 7: schema bumped to 2.
    # v2 adds `last_discovered_at` per provider entry — ISO timestamp
    # of the most recent successful /v1/models discovery call. Used by
    # the weekly auto-refresh loop to skip providers that were checked
    # within the last 7 days, and by startup-catchup to find providers
    # that have never been checked. None means "never discovered."
    SCHEMA_VERSION = 2

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._providers: list[dict] = []
        self._load()

    # ─── persistence ─────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            self._providers = []
            return
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                provs = data.get('providers', [])
                if isinstance(provs, list):
                    # Filter to only valid dicts, ensure required fields
                    self._providers = [self._normalize(p)
                                        for p in provs
                                        if isinstance(p, dict)]
                    return
        except Exception:
            pass
        self._providers = []

    def _save(self) -> None:
        """Write registry to disk. Called after every mutation."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                '_warning': (
                    'CONTAINS API KEYS. Do not share or commit this file. '
                    'If you accidentally share it, rotate the affected keys.'),
                'schema_version': self.SCHEMA_VERSION,
                'providers': self._providers,
            }
            tmp = self.path.with_suffix('.json.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2)
            tmp.replace(self.path)
        except Exception:
            # If save fails, raise so the caller can show an error
            raise

    @staticmethod
    def _normalize(p: dict) -> dict:
        """Fill in missing fields with defaults.

        v4.14.0 stage 7: `last_discovered_at` added (None = never
        discovered). Preserved on read so unmigrated v1 records also
        get the field at load time without needing the one-shot
        migration to have run yet.
        """
        out = {
            'id': p.get('id') or str(uuid.uuid4())[:8],
            'name': str(p.get('name') or '').strip(),
            'preset': str(p.get('preset') or 'custom').lower(),
            'format': str(p.get('format') or 'openai_compat').lower(),
            'endpoint': str(p.get('endpoint') or '').strip(),
            'api_key': str(p.get('api_key') or ''),
            'model': str(p.get('model') or '').strip(),
            'enabled': bool(p.get('enabled', True)),
            'created_at': p.get('created_at') or datetime.now().isoformat(),
            'updated_at': p.get('updated_at') or datetime.now().isoformat(),
            'last_discovered_at': p.get('last_discovered_at'),
        }
        # v4.14.5.14-rot Patch 1: preserve a per-provider model
        # ROTATION list — but ONLY when it's already present and
        # non-empty. _normalize() otherwise rebuilds from a fixed key
        # set and would silently DROP `models`. Adding it
        # unconditionally as [] would mutate every record's on-disk
        # shape even with the rotation flag OFF; gating on "non-empty"
        # keeps the flag-off / no-migration path byte-identical (no
        # record ever gains the key until the flag-gated migration or
        # the multi-model dialog writes one). Nothing READS this list
        # until Patch 2 — purely additive persistence here.
        try:
            _ml = p.get('models')
            if isinstance(_ml, (list, tuple)):
                _ml = [str(m).strip() for m in _ml if str(m).strip()]
                if _ml:
                    out['models'] = _ml
        except Exception:
            pass
        return out

    # ─── public API ─────────────────────────────────────────────────────

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._providers)

    def enabled(self) -> list[dict]:
        with self._lock:
            return [p for p in self._providers if p.get('enabled')]

    def get(self, provider_id: str) -> dict | None:
        with self._lock:
            for p in self._providers:
                if p.get('id') == provider_id:
                    return dict(p)
        return None

    def add(self, provider_data: dict) -> dict:
        """Add a new provider. Returns the normalized stored record."""
        with self._lock:
            new = self._normalize(provider_data)
            new['id'] = str(uuid.uuid4())[:8]
            new['created_at'] = datetime.now().isoformat()
            new['updated_at'] = new['created_at']
            self._providers.append(new)
            self._save()
            return dict(new)

    def update(self, provider_id: str, provider_data: dict) -> bool:
        """Update existing provider. Returns True on success."""
        with self._lock:
            for i, p in enumerate(self._providers):
                if p.get('id') == provider_id:
                    merged = {**p, **provider_data}
                    merged['id'] = provider_id  # never overwrite
                    merged['created_at'] = p.get('created_at')  # keep
                    merged['updated_at'] = datetime.now().isoformat()
                    self._providers[i] = self._normalize(merged)
                    self._save()
                    return True
        return False

    def delete(self, provider_id: str) -> bool:
        with self._lock:
            for i, p in enumerate(self._providers):
                if p.get('id') == provider_id:
                    del self._providers[i]
                    self._save()
                    return True
        return False

    def set_enabled(self, provider_id: str, enabled: bool) -> bool:
        return self.update(provider_id, {'enabled': bool(enabled)})

    def count(self) -> int:
        with self._lock:
            return len(self._providers)


# ─── HTTP calling (v4.13.41 Phase 2) ─────────────────────────────────────
# Each format function takes a provider dict + prompt, makes the HTTP
# request, and returns the response text. Raises ProviderError on failure
# with a human-readable message. urllib.request keeps us off third-party
# deps (httpx/requests would otherwise need to be bundled).

import urllib.request
import urllib.error


class ProviderError(Exception):
    """Raised when an API provider call fails. Message is suitable for
    display in the activity log."""
    pass


# ── v4.14.5.14a.4: per-thread capture of the last HTTP response's
# rate-limit headers + status. The HTTP chokepoint (_http_post_json)
# discarded response headers, so the 429 handler could not tell a
# per-minute throttle from a daily-exhaustion 429, and successful
# responses' X-RateLimit-*-Day headers (the provider telling us its
# real cap) were thrown away. We stash a small normalized dict here
# at the chokepoint and the router/learner reads it immediately after
# on the SAME thread. No call-signature/return-type changes — same
# established module-state pattern as begin_scan_run/is_scan_run_active.
# Thread-local because scan dispatch + consensus runs are serialized
# per worker thread; a global would race across threads.
_RL_HEADER_KEYS = (
    'retry-after',
    'x-ratelimit-limit-requests', 'x-ratelimit-remaining-requests',
    'x-ratelimit-limit-tokens', 'x-ratelimit-remaining-tokens',
    'x-ratelimit-limit-requests-day',
    'x-ratelimit-remaining-requests-day',
    'x-ratelimit-limit-tokens-day',
    'x-ratelimit-remaining-tokens-day',
    'x-ratelimit-limit-requests-minute',
    'x-ratelimit-remaining-requests-minute',
    'ratelimitreset', 'x-ratelimit-reset-requests',
    'x-ratelimit-reset-tokens',
)
_last_http_meta = threading.local()


def _capture_http_meta(header_obj, status: int) -> None:
    """Stash the rate-limit-relevant headers (lowercased) + status of
    the just-finished HTTP call for the router/learner to read. Never
    raises — capture failure must not break the call path."""
    try:
        hdrs = {}
        try:
            items = header_obj.items() if header_obj is not None else []
        except Exception:
            items = []
        for k, v in items:
            lk = str(k).strip().lower()
            if lk in _RL_HEADER_KEYS:
                hdrs[lk] = str(v).strip()
        _last_http_meta.value = {'status': int(status),
                                 'headers': hdrs}
    except Exception:
        try:
            _last_http_meta.value = {'status': int(status),
                                     'headers': {}}
        except Exception:
            pass


def get_last_http_meta() -> dict:
    """Return {'status': int, 'headers': {lowercased: str}} for the
    most recent HTTP call on THIS thread, or {} if none captured."""
    try:
        return dict(getattr(_last_http_meta, 'value', None) or {})
    except Exception:
        return {}


def clear_last_http_meta() -> None:
    try:
        _last_http_meta.value = {}
    except Exception:
        pass


def _is_localhost_url(url: str) -> bool:
    """v4.15.0 Step 12: True if url targets the local machine (Ollama, etc.).

    The offline gate skips localhost URLs because local services keep working
    when the public network is down.
    """
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or '').lower()
    except Exception:
        return False
    if host in ('localhost', '127.0.0.1', '::1', '0.0.0.0'):
        return True
    if host.startswith('127.'):
        return True
    return False


def _http_post_json(url: str, headers: dict, body: dict,
                     timeout: float) -> dict:
    """POST a JSON body to url, return parsed JSON response.

    Raises ProviderError with a useful message on any failure.

    v4.13.42: send a real User-Agent and Accept headers. Without these,
    urllib uses 'Python-urllib/3.x' as User-Agent, which Cloudflare-
    fronted endpoints (Groq, sometimes others) reject as bot traffic
    with HTTP 403 + error code 1010. The official Groq Python SDK
    avoids this by sending 'Groq/Python <ver>'; we use a self-
    identifying header that's similarly accepted everywhere we've
    tested. Browser-mimic User-Agents would also work but feel
    deceitful for a server-to-server API call.
    """
    # v4.15.0 Step 12: offline short-circuit for cloud endpoints. Localhost
    # (Ollama) is exempt so local inference keeps working when offline.
    if not _is_localhost_url(url):
        try:
            import tm_network as _tmn
            if not _tmn.is_online():
                raise ProviderError("network offline — call skipped")
        except ProviderError:
            raise
        except Exception:
            pass  # Defensive: detector failure must not break the call path.

    try:
        data = json.dumps(body).encode('utf-8')
    except Exception as e:
        raise ProviderError(f"request encode failed: {e}")
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Content-Type', 'application/json')
    # v4.13.42: identify ourselves with something Cloudflare trusts.
    req.add_header('User-Agent', 'TiredMarket/4.13.42 (Python)')
    req.add_header('Accept', 'application/json')
    req.add_header('Accept-Encoding', 'identity')  # don't ask for gzip
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # v4.14.5.14a.4: capture success-response rate-limit
            # headers (the provider telling us its real remaining/cap)
            # for the dynamic-learning layer. Side-effect only.
            _capture_http_meta(getattr(resp, 'headers', None),
                               getattr(resp, 'status', 200) or 200)
            raw = resp.read()
            try:
                return json.loads(raw.decode('utf-8'))
            except Exception as e:
                raise ProviderError(f"bad JSON response: {e}")
    except urllib.error.HTTPError as e:
        # v4.14.5.14a.4: capture the error response's rate-limit
        # headers (Retry-After, X-RateLimit-*) so the 429 handler can
        # tell a per-minute throttle from daily exhaustion.
        _capture_http_meta(getattr(e, 'headers', None),
                           getattr(e, 'code', 0) or 0)
        # Try to extract API error message from the body
        body_msg = ''
        try:
            body_raw = e.read().decode('utf-8', errors='replace')
            try:
                parsed = json.loads(body_raw)
                # Common shapes: {error: {message: "..."}} or {error: "..."}
                err = parsed.get('error') if isinstance(parsed, dict) else None
                if isinstance(err, dict):
                    body_msg = err.get('message', '') or str(err)[:120]
                elif isinstance(err, str):
                    body_msg = err
                else:
                    body_msg = body_raw[:200]
            except Exception:
                body_msg = body_raw[:200]
        except Exception:
            pass
        if e.code == 401:
            raise ProviderError(f"401 unauthorized — check API key. {body_msg}")
        if e.code == 403:
            raise ProviderError(f"403 forbidden. {body_msg}")
        if e.code == 404:
            raise ProviderError(f"404 not found — check endpoint URL or model name. {body_msg}")
        if e.code == 429:
            raise ProviderError(f"429 rate-limited — slow down or upgrade tier. {body_msg}")
        if 500 <= e.code < 600:
            raise ProviderError(f"{e.code} server error from provider. {body_msg}")
        raise ProviderError(f"HTTP {e.code}: {body_msg or e.reason}")
    except urllib.error.URLError as e:
        raise ProviderError(f"network error: {e.reason}")
    except TimeoutError:
        raise ProviderError(f"timeout after {timeout}s")
    except Exception as e:
        raise ProviderError(f"{type(e).__name__}: {e}")


def _call_openai_compat(provider: dict, prompt: str,
                          timeout: float) -> str:
    """OpenAI-compatible chat completions (also Groq, Mistral, OpenRouter,
    Together.ai, Ollama via /v1, custom)."""
    endpoint = (provider.get('endpoint') or '').strip()
    if not endpoint:
        raise ProviderError("no endpoint URL configured")
    model = (provider.get('model') or '').strip()
    if not model:
        raise ProviderError("no model name configured")
    api_key = (provider.get('api_key') or '').strip()
    headers = {}
    if api_key:
        headers['Authorization'] = f"Bearer {api_key}"
    body = {
        'model': model,
        'messages': [
            {'role': 'user', 'content': prompt},
        ],
        'temperature': 0.7,
        # v4.14.6.7-verdict-parse-and-schema (2026-06-11): 2048 -> 3072.
        # Verbose models (Zhipu/GLM/Mistral) front-load analysis bullets
        # and hit the cap before reaching the structured summary;
        # captured failing scans confirmed this in the truncated raw_text
        # samples. Verdict-first schema (tm_discover.py) is the load-
        # bearing fix; this bump is breathing room. Applied at the
        # provider transport layer so it affects BOTH scan + holding
        # calls — extra headroom never hurts, and Tier-2 owned-position
        # responses occasionally truncated too. Global, not scan-scoped.
        'max_tokens': 3072,
        'stream': False,
    }
    data = _http_post_json(endpoint, headers, body, timeout)
    # Standard OpenAI shape: {choices: [{message: {content: "..."}}]}
    try:
        choices = data.get('choices', [])
        if not choices:
            raise ProviderError(f"empty choices in response")
        msg = choices[0].get('message', {})
        content = msg.get('content', '')
        if not content:
            raise ProviderError("empty content in response")
        return str(content)
    except ProviderError:
        raise
    except Exception as e:
        raise ProviderError(f"unexpected response shape: {e}")


def _call_anthropic(provider: dict, prompt: str, timeout: float) -> str:
    """Anthropic Claude messages API."""
    endpoint = (provider.get('endpoint') or '').strip()
    if not endpoint:
        raise ProviderError("no endpoint URL configured")
    model = (provider.get('model') or '').strip()
    if not model:
        raise ProviderError("no model name configured")
    api_key = (provider.get('api_key') or '').strip()
    if not api_key:
        raise ProviderError("API key required for Anthropic")
    headers = {
        'x-api-key': api_key,
        'anthropic-version': '2023-06-01',
    }
    body = {
        'model': model,
        # v4.14.6.7-verdict-parse-and-schema (2026-06-11): 2048 -> 3072
        # (mirror of the OpenAI-compatible bump above).
        'max_tokens': 3072,
        'messages': [
            {'role': 'user', 'content': prompt},
        ],
    }
    data = _http_post_json(endpoint, headers, body, timeout)
    # Shape: {content: [{type: "text", text: "..."}]}
    try:
        content_list = data.get('content', [])
        if not content_list:
            raise ProviderError("empty content list in response")
        # Concatenate all text blocks (in case of multi-block response)
        parts = []
        for block in content_list:
            if isinstance(block, dict) and block.get('type') == 'text':
                parts.append(block.get('text', ''))
        text = ''.join(parts)
        if not text:
            raise ProviderError("no text blocks in response")
        return text
    except ProviderError:
        raise
    except Exception as e:
        raise ProviderError(f"unexpected response shape: {e}")


def _call_google(provider: dict, prompt: str, timeout: float) -> str:
    """Google Gemini generateContent API."""
    endpoint = (provider.get('endpoint') or '').strip().rstrip('/')
    if not endpoint:
        raise ProviderError("no endpoint URL configured")
    model = (provider.get('model') or '').strip()
    if not model:
        raise ProviderError("no model name configured")
    api_key = (provider.get('api_key') or '').strip()
    if not api_key:
        raise ProviderError("API key required for Google")
    # Google's URL pattern: {base}/{model}:generateContent?key={key}
    # The bundled default endpoint is the base; we append the model+method.
    url = f"{endpoint}/{model}:generateContent?key={api_key}"
    headers = {}
    body = {
        'contents': [
            {'parts': [{'text': prompt}]},
        ],
        'generationConfig': {
            'temperature': 0.7,
            'maxOutputTokens': 2048,
        },
    }
    data = _http_post_json(url, headers, body, timeout)
    # Shape: {candidates: [{content: {parts: [{text: "..."}]}}]}
    try:
        candidates = data.get('candidates', [])
        if not candidates:
            # Could be a content filter — check promptFeedback
            pf = data.get('promptFeedback', {})
            if pf.get('blockReason'):
                raise ProviderError(
                    f"blocked by Google: {pf.get('blockReason')}")
            raise ProviderError("no candidates in response")
        cand = candidates[0]
        content = cand.get('content', {})
        parts = content.get('parts', [])
        text = ''.join(p.get('text', '') for p in parts
                        if isinstance(p, dict))
        if not text:
            raise ProviderError("empty text in response")
        return text
    except ProviderError:
        raise
    except Exception as e:
        raise ProviderError(f"unexpected response shape: {e}")


_FORMAT_DISPATCH = {
    'openai_compat': _call_openai_compat,
    'anthropic': _call_anthropic,
    'google': _call_google,
}


def call_provider(provider: dict, prompt: str,
                    timeout: float = 60.0,
                    log_fn=None) -> str:
    """v4.13.41: Send a prompt to an API provider and return the text
    response.

    Dispatches to the right adapter based on provider['format'].
    Raises ProviderError on any failure with a message suitable for
    the activity log.

    v4.13.44: Now applies per-provider rate limiting before the call.
    Will block (sleep) up to ~60s if RPM cap reached. Raises
    ProviderError immediately if daily quota is exhausted (caller
    should treat as a soft failure -- skip provider, continue).
    """
    # v4.13.44: rate-limit gate before HTTP. If quota is dead, fail fast
    # with a clear message rather than firing the call and wasting it.
    #
    # v4.14.3.8 (2026-05-14): pass an estimated token count so the
    # limiter can enforce TPM in addition to RPM. Heuristic:
    # len(prompt) / 4 — a reasonable rough estimate for English text
    # via standard tokenizers (BPE-style averages 3.5-4.5 chars per
    # token; we round to 4 for simplicity). More accurate counting
    # via real tokenizer libraries is a future refinement; the
    # heuristic is good enough to prevent the Groq TPM blowouts the
    # queue runner has been hitting (3,000-token prompts × 2 calls
    # blowing the 6,000 TPM ceiling). When the limiter has no TPM
    # cap configured, the estimate is ignored and behavior matches
    # pre-v4.14.3.8 RPM-only enforcement.
    try:
        import tm_rate_limiter as _trl
        try:
            _est_tokens = max(1, int(len(prompt) / 4)) if prompt else 0
            # v4.14.5.78-per-model-rate-gate: thread the dispatcher-
            # selected model through to the limiter so a (provider_id,
            # model) bucket is consulted — NOT just the provider-level
            # one. `provider['model']` is authoritative here: the
            # router copies the provider dict and sets cp['model']
            # to the chosen model before calling us (see
            # tm_ai_router.py:328-330 + queue runner sticky-pick).
            # The limiter handles the empty-model case as a single
            # per-provider bucket (kill-switch fallback / unknown
            # model), so passing None or '' is safe.
            _mdl = provider.get('model')
            _trl.acquire_for_provider(
                provider, log_fn=log_fn,
                estimated_tokens=_est_tokens,
                model=_mdl)
        except _trl.QuotaExhausted as qe:
            raise ProviderError(f"daily quota exhausted ({qe})")
        except _trl.NonBlockingBusy as nbb:
            # v4.14.6.5-cache-ungate-tpm-skip: the calling thread is in
            # nonblocking_scope() (Tier-1 scan dispatch) and the limiter
            # would have blocked. Surface as a 429-style ProviderError so
            # the scan-fallback loop's classify_failure routes it as an
            # OUTCOME_QUOTA (mark_exhausted + continue chain) and reaches
            # the next eligible provider instantly. Message prefixed with
            # the literal "429" so _extract_leading_status_code picks it
            # up cleanly.
            raise ProviderError(
                f"429 rate-limited (TPM/RPM hold; non-blocking skip) — "
                f"would wait {nbb.wait_time:.1f}s on {nbb.provider_id}")
    except ProviderError:
        raise
    except Exception:
        # Limiter import or attribute failure -- proceed without it.
        # We never want a missing limiter to block a working call.
        pass

    fmt = (provider.get('format') or 'openai_compat').lower()
    fn = _FORMAT_DISPATCH.get(fmt)
    if fn is None:
        raise ProviderError(f"unknown provider format: {fmt}")
    try:
        return fn(provider, prompt, timeout)
    except ProviderError as pe:
        # Phase 2 Teacher AI: classify the error and surface a
        # system_event entry before re-raising. The classification
        # matches the canonical messages from the openai_compat
        # HTTP handler (tm_api_providers.py:506-515). Best-effort —
        # if the intercept layer isn't available, just re-raise.
        try:
            import tm_teacher_intercept as _tm_ic
            msg = str(pe).lower()
            provider_name = (
                provider.get('name')
                or provider.get('preset')
                or 'this provider')
            if '401' in msg or 'unauthorized' in msg:
                _tm_ic.emit_system_event(
                    'provider_auth_failed',
                    context={'provider': provider_name})
            elif '403' in msg or 'forbidden' in msg:
                _tm_ic.emit_system_event(
                    'provider_auth_failed',
                    context={'provider': provider_name})
            # v4.14.5.66-provider-error-coaching: parity with the save
            # path — 404 and 405 at scan/consensus time now surface
            # Teacher coaching instead of a silent skip/log line. The
            # canonical error strings come from _http_post_json (404
            # not found, "HTTP 405:" for catch-all 405). 401/403 above
            # stay; 429 below stays demoted to log (deliberate, §55).
            elif '404' in msg or 'not found' in msg:
                _tm_ic.emit_system_event(
                    'provider_endpoint_not_found',
                    context={'provider': provider_name})
            elif '405' in msg or 'method not allowed' in msg:
                _tm_ic.emit_system_event(
                    'provider_endpoint_method_not_allowed',
                    context={'provider': provider_name})
            elif '429' in msg or 'rate-limited' in msg:
                # v4.14.5.55: a routine free-tier rate-limit is AUTO-HANDLED
                # (short cooldown + retry by the router) — firing a modal
                # surface here just alarms a new user who doesn't know what a
                # TPM cap is. Demote to a quiet activity-log line instead of
                # emit_system_event's popup. (401/403 above STAYS a real popup
                # — a dead/invalid key is something the user MUST act on.)
                try:
                    _logf = log_fn if callable(log_fn) else None
                    if _logf is None:
                        _app = getattr(_tm_ic, '_registered_app', None)
                        _logf = getattr(_app, '_log', None) if _app else None
                    if callable(_logf):
                        _logf(
                            f"{provider_name}: hit its per-minute rate limit "
                            f"(free-tier cap) — pausing briefly and retrying "
                            f"on its own; no action needed.", 'muted')
                except Exception:
                    pass
        except Exception:
            pass
        raise


def display_label(provider: dict) -> str:
    """v4.13.41: Return the string used as the 'model' field in vote dicts
    and predictions for this provider. Uses the user's display name so it
    appears naturally in the consensus card and accuracy matrix.

    v4.14.0 stage 6d: passed through canonicalize_model_label so the
    chip strip / consensus card / activity log all show canonical names
    (Groq, Gemini, Mistral, Cerebras, GitHub, SambaNova) regardless of
    what the user typed at add-provider time.
    """
    raw = (provider.get('name') or
            f"api:{provider.get('preset', 'unknown')}").strip()
    return canonicalize_model_label(raw)


# ─── v4.14.0 stage 6d: display-time label normalization ──────────────────
#
# Rules per Decision 5/6 in the locked stage 6d+6e spec:
#
#   1. Drop "My "/"my " when it appears as a leading prefix or right
#      after "via " (the activity-log "Llama 3.1 8B Instruct via My
#      Groq" form).
#   2. Spelling fix: "Minstral" → "Mistral" (the user's typo on his Mistral
#      provider entry).
#   3. Capitalization: "Github" → "GitHub" (brand's own form).
#   4. Capitalization: "Sambanova" → "SambaNova" (brand's own form).
#
# v4.14.0 stage 7.1 polish pass:
#   5. Ollama tag → canonical display name lookup. Cloud providers got
#      canonical labels via the rules above; Ollama strings remained in
#      raw tag form ("qwen2.5:14b", "phi4:14b", etc.). Stage 7.1 adds
#      a static lookup for the tags actually present in the user's
#      prediction history. Unknown tags pass through unchanged.
#
# Applied at display time. We do NOT rewrite predictions.jsonl /
# signals.jsonl — historical records keep their original strings on
# disk. Every read surface that shows a model label calls this
# function before rendering, so the user sees canonical names
# regardless of what was frozen into past data.
#
# Idempotent — running twice produces the same string.

# v4.14.5.14-ollama-purge-step4: _OLLAMA_TAG_DISPLAY (Ollama tag → display name)
# removed — Ollama is retired; no local tags ever reach this function now. The
# cloud provider/model label normalization below is unchanged.


def canonicalize_model_label(label):
    """Normalize a provider/model display label per v4.14.0 stage 6d.

    Handles two shapes:
      - Bare cloud provider names ("My Groq" → "Groq")
      - Embedded "via X" forms ("...via my Minstral" → "...via Mistral")

    Returns the input unchanged for None / empty / non-string inputs.
    (v4.14.5.14-ollama-purge-step4: the Ollama-tag lookup short-circuit was
    removed with the Ollama exit.)
    """
    if not label:
        return label
    s = str(label)
    # 1a. "via My X" / "via my X" → "via X" (case-insensitive)
    s = re.sub(r'(?i)(\bvia\s+)my\s+', r'\1', s)
    # 1b. Leading "My " / "my " on a bare label
    s = re.sub(r'(?i)^\s*my\s+', '', s)
    # 2. Mistral spelling
    s = re.sub(r'(?i)\bminstral\b', 'Mistral', s)
    # 3. GitHub canonical capitalization
    s = re.sub(r'(?i)\bgithub\b', 'GitHub', s)
    # 4. SambaNova canonical capitalization
    s = re.sub(r'(?i)\bsambanova\b', 'SambaNova', s)
    # v4.14.5.23-weighting-actually-wired (Bug B): bridge current provider
    # DISPLAY names to the canonical keys their accuracy history is stored
    # under in source_weights, so accuracy-weighted consensus resolves a
    # provider's full record instead of defaulting to neutral. History was
    # written under the bare provider name ("Mistral" n=61, "Gemini" n=12)
    # before the registry adopted the longer vendor display names
    # ("Mistral La Plateforme", "Google Gemini"); without these the two
    # highest-history providers miss their own rows.
    # 5. "Mistral La Plateforme" → "Mistral"
    s = re.sub(r'(?i)\bmistral\s+la\s+plateforme\b', 'Mistral', s)
    # 6. "Google Gemini" → "Gemini"
    s = re.sub(r'(?i)\bgoogle\s+gemini\b', 'Gemini', s)
    return s.strip()


# ─── v4.14.0 stage 6d: one-shot api_providers.json migration ─────────────
#
# Rewrites the `name` field on each provider entry to its canonical
# form using the same rules as canonicalize_model_label, then saves
# back to disk. Writes a one-time .bak alongside the live file
# (api_providers.pre_v4.14.0.<timestamp>.bak) the first time the
# migration runs; subsequent runs are no-ops because the cfg flag
# guards re-entry.
#
# Idempotent: running twice produces the same result. Safe to call
# unconditionally on every launch — the cfg flag short-circuits.

def migrate_provider_names_v4140(cfg=None, data_dir=None) -> bool:
    """One-shot migration. Returns True on success (no error raised),
    False if something went wrong reading/writing the registry.

    `cfg` is the App's config dict. If provided, we read/write the
    `provider_name_migration_v4140_done` flag to short-circuit re-runs.
    The caller is responsible for persisting cfg via save_config().

    `data_dir` is the data directory; defaults to the same resolution
    as load_enabled_providers().
    """
    flag = 'provider_name_migration_v4140_done'
    if cfg is not None and cfg.get(flag):
        return True

    # Resolve data dir same way load_enabled_providers does
    try:
        from pathlib import Path as _P
        if data_dir is None:
            candidates = [_P('data')]
            try:
                here = _P(__file__).parent
                candidates.append(here / 'data')
            except Exception:
                pass
        else:
            candidates = [_P(data_dir)]
        registry_path = None
        for d in candidates:
            try:
                p = d / 'api_providers.json'
                if p.exists():
                    registry_path = p
                    break
            except Exception:
                continue
        if registry_path is None:
            # Nothing to migrate — fresh install. Mark done so we don't
            # re-check every launch.
            if cfg is not None:
                cfg[flag] = True
            return True
    except Exception:
        return False

    # Load registry
    try:
        with open(registry_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return False

    if not isinstance(data, dict):
        return False
    providers = data.get('providers')
    if not isinstance(providers, list):
        if cfg is not None:
            cfg[flag] = True
        return True

    # Transform names
    changes = []  # list of (provider_id, old_name, new_name)
    now_iso = datetime.now().isoformat()
    for p in providers:
        if not isinstance(p, dict):
            continue
        old = str(p.get('name') or '')
        new = canonicalize_model_label(old)
        if new != old:
            p['name'] = new
            p['updated_at'] = now_iso
            p['migrated_v4140_at'] = now_iso
            changes.append((p.get('id', '?'), old, new))

    # If anything changed, write .bak (only if it doesn't exist) then save
    if changes:
        try:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            bak_path = registry_path.with_name(
                f'api_providers.pre_v4.14.0.{ts}.bak')
            # Don't overwrite an existing .bak from a prior run. Skip
            # writing if any pre_v4.14.0 .bak already exists for this
            # registry — that means we've already preserved the original.
            existing_baks = list(registry_path.parent.glob(
                'api_providers.pre_v4.14.0.*.bak'))
            if not existing_baks:
                bak_path.write_bytes(registry_path.read_bytes())
        except Exception:
            # If .bak write fails, do NOT save the transformed registry
            # — we'd lose the original.
            return False

        try:
            payload = {
                '_warning': data.get('_warning',
                    'CONTAINS API KEYS. Do not share or commit this '
                    'file. If you accidentally share it, rotate the '
                    'affected keys.'),
                'schema_version': data.get(
                    'schema_version',
                    APIProviderRegistry.SCHEMA_VERSION),
                'providers': providers,
            }
            tmp = registry_path.with_suffix('.json.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2)
            tmp.replace(registry_path)
        except Exception:
            return False

    # Mark complete (whether or not anything changed)
    if cfg is not None:
        cfg[flag] = True
    return True


# ─── v4.14.0 stage 7: api_providers.json schema v1 → v2 migration ────────
#
# Adds `last_discovered_at: null` per provider entry and bumps
# schema_version to 2. The new field is the timestamp of the most
# recent successful /v1/models call for that provider — used by
# stage 7's startup-catchup + weekly auto-refresh loops to skip
# providers that are already up to date.
#
# Idempotent. Independent of migrate_provider_names_v4140 (which only
# touches the `name` field). Both run on first v4.14.0 launch and
# both short-circuit via cfg flag on subsequent launches.

def migrate_api_providers_v4140_stage7(cfg=None, data_dir=None) -> bool:
    """One-shot migration. Returns True on success, False on error.

    Adds `last_discovered_at` field (initially None) to each provider
    entry and bumps schema_version to 2. Writes a one-time .bak named
    `api_providers.pre_v4.14.0_stage7.<timestamp>.bak` next to the
    live file. Sets `provider_discovery_migration_v4140_stage7_done`
    in cfg on success so subsequent launches short-circuit.

    Idempotent — running twice produces the same result. Safe to
    call unconditionally on every launch.
    """
    flag = 'provider_discovery_migration_v4140_stage7_done'
    if cfg is not None and cfg.get(flag):
        return True

    # Resolve data dir (mirrors load_enabled_providers / stage 6d migration)
    try:
        from pathlib import Path as _P
        if data_dir is None:
            candidates = [_P('data')]
            try:
                here = _P(__file__).parent
                candidates.append(here / 'data')
            except Exception:
                pass
        else:
            candidates = [_P(data_dir)]
        registry_path = None
        for d in candidates:
            try:
                p = d / 'api_providers.json'
                if p.exists():
                    registry_path = p
                    break
            except Exception:
                continue
        if registry_path is None:
            # Nothing to migrate — fresh install.
            if cfg is not None:
                cfg[flag] = True
            return True
    except Exception:
        return False

    # Load registry
    try:
        with open(registry_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return False

    if not isinstance(data, dict):
        return False
    providers = data.get('providers')
    if not isinstance(providers, list):
        if cfg is not None:
            cfg[flag] = True
        return True

    current_version = data.get('schema_version', 1)

    # If already at v2 AND every provider has the field, nothing to do.
    needs_field_add = any(
        isinstance(p, dict) and 'last_discovered_at' not in p
        for p in providers)
    if (current_version >= APIProviderRegistry.SCHEMA_VERSION
            and not needs_field_add):
        if cfg is not None:
            cfg[flag] = True
        return True

    # Add the field where missing. None = never discovered.
    for p in providers:
        if not isinstance(p, dict):
            continue
        if 'last_discovered_at' not in p:
            p['last_discovered_at'] = None

    # Write .bak safety net (skip if any prior pre_v4.14.0_stage7 .bak
    # already exists — idempotency).
    try:
        existing_baks = list(registry_path.parent.glob(
            'api_providers.pre_v4.14.0_stage7.*.bak'))
        if not existing_baks:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            bak_path = registry_path.with_name(
                f'api_providers.pre_v4.14.0_stage7.{ts}.bak')
            bak_path.write_bytes(registry_path.read_bytes())
    except Exception:
        # If .bak write fails, do NOT save the migrated registry —
        # we'd lose the safety net.
        return False

    # Save back with bumped schema_version
    try:
        payload = {
            '_warning': data.get('_warning',
                'CONTAINS API KEYS. Do not share or commit this '
                'file. If you accidentally share it, rotate the '
                'affected keys.'),
            'schema_version': APIProviderRegistry.SCHEMA_VERSION,
            'providers': providers,
        }
        tmp = registry_path.with_suffix('.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
        tmp.replace(registry_path)
    except Exception:
        return False

    if cfg is not None:
        cfg[flag] = True
    return True


# ─── v4.14.0 stage 7: helper for stage 7 callers to update
# `last_discovered_at` after a successful discovery call. Atomic via
# the registry's existing _save() path.

def update_last_discovered_at(provider_id: str,
                                ts_iso: Optional[str] = None,
                                data_dir=None) -> bool:
    """Stamp `last_discovered_at` on the provider entry. Used by
    stage 7's discovery loops after a successful validate_key +
    discover_models pass — even when zero new mappings were added,
    so the weekly-refresh loop knows this provider was checked
    today and won't re-fire it tomorrow (per I-10).

    Returns True on success, False if the registry can't be opened
    or the provider id isn't found.
    """
    try:
        from pathlib import Path as _P
        if data_dir is None:
            candidates = [_P('data')]
            try:
                here = _P(__file__).parent
                candidates.append(here / 'data')
            except Exception:
                pass
        else:
            candidates = [_P(data_dir)]
        registry_path = None
        for d in candidates:
            try:
                p = d / 'api_providers.json'
                if p.exists():
                    registry_path = p
                    break
            except Exception:
                continue
        if registry_path is None:
            return False
        reg = APIProviderRegistry(registry_path)
        return reg.update(provider_id,
                            {'last_discovered_at':
                             ts_iso or datetime.now().isoformat()})
    except Exception:
        return False


def migrate_custom_to_first_class_v414b(cfg=None,
                                        data_dir=None) -> list:
    """v4.14.5.14b-prov: one-time, idempotent, NON-destructive
    relabel. Entries added before the first-class quick-add cards
    existed are typed preset='custom' even though their endpoint is
    really Cerebras / GitHub Models / SambaNova, so the Recommended
    cards' "already added" check doesn't recognise them and they
    duplicate in the configured list.

    For every registry entry whose preset is custom/unknown/blank,
    resolve its canonical provider id from the endpoint URL (reusing
    tm_ai_router.provider_canonical_id — the SAME primitive the chip
    strip uses; no new matching logic). If it resolves to one of the
    first-class providers that have a Recommended card
    (cerebras / github / sambanova), set `preset` to that id via a
    PARTIAL update — api_key / model / enabled / id / everything else
    is preserved untouched.

    Idempotent by construction: only preset∈{custom,unknown,''}
    entries are considered, and only when the endpoint resolves; a
    re-run finds nothing left to change. Returns a list of
    (name, old_preset, new_preset) for logging. Never raises."""
    targets = {'cerebras', 'github', 'sambanova'}
    changed: list = []
    try:
        from pathlib import Path as _P
        if data_dir is None:
            candidates = [_P('data')]
            try:
                candidates.append(_P(__file__).parent / 'data')
            except Exception:
                pass
        else:
            candidates = [_P(data_dir)]
        registry_path = None
        for d in candidates:
            try:
                p = d / 'api_providers.json'
                if p.exists():
                    registry_path = p
                    break
            except Exception:
                continue
        if registry_path is None:
            return changed
        reg = APIProviderRegistry(registry_path)
    except Exception:
        return changed

    try:
        import tm_ai_router as _r
    except Exception:
        return changed  # no canonical resolver → safest is no-op

    try:
        for p in reg.all():
            preset = (p.get('preset') or '').strip().lower()
            if preset not in ('custom', 'unknown', ''):
                continue  # already first-class — idempotent skip
            try:
                cid = _r.provider_canonical_id(p)
            except Exception:
                cid = None
            if cid in targets:
                pid = p.get('id')
                if pid and reg.update(pid, {'preset': cid}):
                    changed.append(
                        (p.get('name', '?'), preset or 'custom', cid))
    except Exception:
        return changed
    return changed


def migrate_models_list_v414rot(cfg=None, data_dir=None) -> list:
    """v4.14.5.14-rot Patch 1: one-time, idempotent, NON-destructive.
    For every provider with a non-empty singular `model` and no
    `models` rotation list yet, add `models = [model]`. The singular
    `model` is PRESERVED untouched (the router still reads it until
    Patch 2 swaps the read — removing it now would break dispatch).

    Idempotent: a provider that already has a non-empty `models` is
    skipped, so a re-run changes nothing. Returns [(name, [models])]
    for logging. Never raises. The caller only invokes this when
    cfg['use_model_rotation_schema'] is on, so the flag-off path
    never migrates (byte-identical to pre-patch)."""
    changed: list = []
    try:
        from pathlib import Path as _P
        if data_dir is None:
            candidates = [_P('data')]
            try:
                candidates.append(_P(__file__).parent / 'data')
            except Exception:
                pass
        else:
            candidates = [_P(data_dir)]
        registry_path = None
        for d in candidates:
            try:
                p = d / 'api_providers.json'
                if p.exists():
                    registry_path = p
                    break
            except Exception:
                continue
        if registry_path is None:
            return changed
        reg = APIProviderRegistry(registry_path)
    except Exception:
        return changed
    try:
        for p in reg.all():
            existing = p.get('models')
            if isinstance(existing, (list, tuple)) and [
                    m for m in existing if str(m).strip()]:
                continue  # already has a rotation list — idempotent
            m = str(p.get('model') or '').strip()
            if not m:
                continue  # nothing to seed from
            pid = p.get('id')
            if pid and reg.update(pid, {'models': [m]}):
                changed.append((p.get('name', '?'), [m]))
    except Exception:
        return changed
    return changed


def load_enabled_providers(data_dir=None) -> list:
    """v4.13.44: Module-level helper to load enabled providers from disk
    without needing access to the App instance. Used by scan paths in
    tm_holdings.py and the lookup paths in tired_market.py to avoid
    plumbing app references through to leaf functions.

    Returns [] on any failure (missing registry, bad JSON, etc.).
    """
    try:
        from pathlib import Path as _P44
        if data_dir is None:
            # Try common locations
            candidates = [_P44('data')]
            try:
                here = _P44(__file__).parent
                candidates.append(here / 'data')
            except Exception:
                pass
        else:
            candidates = [_P44(data_dir)]
        for d in candidates:
            try:
                p = d / 'api_providers.json'
                if p.exists():
                    reg = APIProviderRegistry(p)
                    return reg.enabled()
            except Exception:
                continue
        return []
    except Exception:
        return []


def _resolve_model_registry():
    """Locate and load the model registry alongside data/api_providers
    .json (mirrors load_enabled_providers' data-dir resolution).
    Returns a ModelRegistry instance or None if unavailable. Stage 4
    uses this to translate provider model strings into canonical ids
    so the scan runner can dedupe + fail over by canonical model."""
    try:
        import tm_model_registry as _reg_mod
    except ImportError:
        return None
    try:
        from pathlib import Path as _P
        candidates = [_P('data')]
        try:
            here = _P(__file__).parent
            candidates.append(here / 'data')
        except Exception:
            pass
        for d in candidates:
            try:
                if (d / 'model_registry.default.json').exists():
                    return _reg_mod.ModelRegistry(d)
            except Exception:
                continue
    except Exception:
        pass
    return None


# v4.14.0 stage 4: lineup version stamped onto every prediction the
# new model-aware scan path writes. v4.13.x and v4.13.65 still write
# "v4.13" via the schema-bump default. Track Record can use this to
# distinguish pre-rework vs post-rework predictions cleanly.
#
# v4.14.1.1: bumped because scans now write ONE prediction per
# candidate (single-provider mode with round-robin across candidates)
# instead of N. Same prediction schema, but the per-candidate vote
# count changes — Track Record stats split cleanly on this version
# field.
LINEUP_VERSION = "v4.14.1.1"

# Per-provider transient-retry budget (linear backoff: 1s, 3s, 5s, then
# failover). Matches the retry policy in the resolved spec §3.
# v4.14.5.14-retry-and-cleanup-bundle Fix A: bumped 2→3 (backoffs (1,3)→
# (1,3,5)). The lone 503-driven skip in the user's 2026-05-23 10:51 log (PG on
# Gemini) would have recovered with one more retry. cfg['transient_retry_count']
# overrides via set_transient_retry_count() at startup.
_TRANSIENT_RETRY_BUDGET = 3
_TRANSIENT_BACKOFFS = (1, 3, 5)  # seconds, indexed by attempt number

# Base backoff schedule used when the budget is overridden via cfg. Long
# enough that any allowed budget (clamped to 8) is fully covered, so the
# retry loop's `_TRANSIENT_BACKOFFS[budget - attempts_left]` index can
# never go out of range.
_TRANSIENT_BACKOFF_BASE = (1, 3, 5, 10, 15, 20, 30, 45)


def set_transient_retry_count(n) -> None:
    """v4.14.5.14-retry-and-cleanup-bundle Fix A: cfg['transient_retry_count']
    (default 3) sets the transient-503 retry budget at startup, with a
    matching backoff schedule sliced from _TRANSIENT_BACKOFF_BASE so the
    index in the retry loop is always valid. Clamped to [0, 8]. Fail-safe:
    a non-integer / bad value leaves the (1,3,5)/3 defaults untouched."""
    global _TRANSIENT_RETRY_BUDGET, _TRANSIENT_BACKOFFS
    try:
        n = int(n)
    except (TypeError, ValueError):
        return
    n = max(0, min(8, n))
    _TRANSIENT_RETRY_BUDGET = n
    # Always provide at least one entry so an indexing fallback is safe
    # even at n=0 (no retries → never indexed, but keep the tuple valid).
    _TRANSIENT_BACKOFFS = _TRANSIENT_BACKOFF_BASE[:n] or (1,)


def _extract_leading_status_code(err_text: str):
    """Pull the leading HTTP status code out of a ProviderError-style
    message ('429 rate-limited — ...' → 429). Returns None if the
    text doesn't start with a 3-digit code in 100-599."""
    try:
        head = (err_text or '').split(None, 1)[0]
        if head.isdigit():
            code = int(head)
            if 100 <= code < 600:
                return code
    except Exception:
        pass
    return None


# v4.14.5.14a.2: when no provider can be reached (all in cooldown / at
# daily cap / blocked) the scan runner used to write a fake NO_CALL
# prediction. That polluted predictions.jsonl and advanced the analysis
# cursor as if the ticker had been judged — causing the 75-min identical
# NO_CALL loop seen 2026-05-17. Default behaviour is now: write nothing,
# advance nothing, signal the caller via result_meta['provider_unavailable']
# so it can skip the cursor cleanly. cfg['use_no_call_on_provider_exhaustion']
# (default False) restores the legacy fake-NO_CALL write for rollback.
_NO_CALL_ON_EXHAUSTION = False


def set_no_call_on_provider_exhaustion(enabled: bool) -> None:
    """v4.14.5.14a.2 rollback hook — cfg['use_no_call_on_provider_exhaustion'].
    True = legacy (write a NO_CALL when no provider is available)."""
    global _NO_CALL_ON_EXHAUSTION
    _NO_CALL_ON_EXHAUSTION = bool(enabled)


# v4.14.5.14-scan-canonical-fallback (2026-05-23): scan mode picks ONE
# canonical model per candidate (single-provider-per-candidate, v4.14.1.1).
# Investigation 2026-05-23 found that when that one model's provider is
# momentarily per-minute-rate-limited (or 503s) mid-burst, the ticker is
# SKIPPED even though other eligible canonical models (on idle providers
# like Cerebras/Mistral/GitHub) are sitting right there in the same
# select_provider_groups result. This flag lets the scan, on full
# exhaustion of the picked model, fall through to the NEXT eligible
# canonical model instead of skipping — provider-aware (won't re-attempt a
# provider that already 429'd this run) and depth-capped (picked model +
# up to N-1 fallbacks) to bound burst-amplification. Still ONE *successful*
# call per candidate (breaks on first success), so the v4.14.1.1
# load-spreading intent is preserved. cfg['use_scan_canonical_fallback']
# default True; False = legacy single-model-then-skip (byte-identical).
#
# v4.14.6.4-tier1-routing (2026-06-11): the chain depth cap is raised
# from 3 to 99 so the fail-over can walk every eligible canonical
# provider before giving up. Old cap of 3 was a burst-amplification
# heuristic from when fewer providers were configured; the actual
# amplification is already bounded by the per-run provider de-dupe
# downstream (the loop refuses to re-attempt a provider that already
# 429'd this run), so walking N providers vs 3 costs at most one extra
# ~50ms iteration per *stalled* ticker. With this raised cap the
# `no eligible providers` outcome can only happen when EVERY eligible
# provider in select_provider_groups' result is genuinely capped —
# matching the user's Tier-1 spec ("no stock returns 'no eligible
# providers' while any capable provider has capacity"). The constant
# stays a generous-fixed number rather than computed from the live
# provider count so the routing constant doesn't have to know about
# loader internals; 99 covers any realistic configuration.
_SCAN_CANONICAL_FALLBACK = True
_SCAN_FALLBACK_MAX_MODELS = 99  # picked + fallback walks every eligible
                                # provider; per-run de-dupe still bounds
                                # actual calls to ONE per provider.


def set_scan_canonical_fallback(enabled: bool) -> None:
    """v4.14.5.14-scan-canonical-fallback rollback hook —
    cfg['use_scan_canonical_fallback']. True = on exhaustion, try the next
    eligible canonical model before skipping the ticker; False = legacy."""
    global _SCAN_CANONICAL_FALLBACK
    _SCAN_CANONICAL_FALLBACK = bool(enabled)


def scan_can_run(providers: list = None) -> bool:
    """v4.14.5.14a.3: authoritative "can a scan run right now?" — uses
    the EXACT eligibility path run_apis_for_scan_prediction uses
    (load providers → resolve registry → select_provider_groups for
    call_type='scan'), so the fill-mode pre-check can never disagree
    with what the dispatch actually accepts.

    The v4.14.5.14.2 pre-check used tm_top_ai_picker.pick_top_ai,
    which evaluates a provider's GENERAL daily cap — it does NOT
    apply the 'scan' call-type cap_factor (0.3). So when Mistral had
    used 300 of its 1000 general cap it reported "available" while
    the scan router (300/300 effective) rejected it — fill mode
    dispatched into a wall and the circuit breaker never engaged.
    select_provider_groups applies the scan policy + cooldown + caps
    exactly, so bool(groups) is the ground truth.

    Returns True if at least one canonical-model group is scan-
    eligible right now; False if every provider is in cooldown / at
    its scan-effective cap / blocked. Fail-OPEN (returns True) on any
    internal error so a check bug never wedges fill mode.
    """
    try:
        import tm_ai_router as _router
        if providers is None:
            providers = load_enabled_providers()
        if not providers:
            return False
        registry = _resolve_model_registry()
        groups = _router.select_provider_groups(
            providers, call_type='scan', registry=registry,
            log_fn=None)
        return bool(groups)
    except Exception:
        return True


def run_apis_for_scan_prediction(prompt: str, ticker: str, path: str,
                                    source: str,
                                    predictions_log,
                                    providers: list = None,
                                    log_fn=None,
                                    parse_fn=None,
                                    timeout: float = 60.0,
                                    extra_fields: dict = None,
                                    scan_provider_filter: str = None,
                                    result_meta: dict = None) -> int:
    """v4.14.0: Model-aware scan runner.

    Iterates canonical-model groups (one prediction per canonical
    model, regardless of how many providers serve it) using sticky
    picks + retry/failover per the resolved routing spec:

      - For each canonical model in the eligible-providers list,
        pick the first provider in preference order (sticky).
      - On a transient error (5xx / timeout / network), retry the
        SAME provider up to 2 times with linear backoff (1s, 3s),
        then fail over to the next provider for that canonical
        model.
      - On a quota error (429 / observed-cap hit), mark the provider
        exhausted for this canonical model and fail over to the
        next preference.
      - On a fatal error (4xx other than 429), don't retry, don't
        fail over — record the failure for that canonical model
        and move on.
      - On success, write ONE prediction tagged with the canonical
        model id, the actual provider that served the call, and
        lineup_version='v4.14.0'. The Track Record reader sees a
        single Llama-3.1-8B vote regardless of whether Groq or
        Cerebras served it — fixing vote multiplication (gap 3).

    Behaviors preserved from v4.13.x:
      - Loads providers from disk if not passed in
      - 'scan' call-type policy (blocks Sambanova/Anthropic/OpenAI;
        cap_factor 0.3)
      - scan_provider_filter narrows to one provider by display name
      - extra_fields fold into each prediction record
      - Parse errors and unexpected exceptions are swallowed at the
        per-prediction level so one bad call doesn't kill the rest

    Args:
        prompt: the prompt that was sent to Ollama. APIs get the same.
        ticker: ticker symbol (informational, used in log messages)
        path: prediction path (e.g., 'aggressive', 'lottery')
        source: prediction source string (e.g., 'discover', 'lookup')
        predictions_log: PredictionsLog instance with append() method
        providers: enabled provider list. If None, loads from disk.
        log_fn: optional callable(msg, tag) for activity log output
        parse_fn: optional response-parsing function. Defaults to
            tm_discover.parse_prediction.
        timeout: per-call timeout in seconds
        extra_fields: optional dict of fields to add to each prediction
        scan_provider_filter: if set, narrows providers to the one
            matching display name (case-insensitive) BEFORE building
            model groups.

    Returns: number of predictions written (one per canonical model
    that produced a usable response).
    """
    import time as _time
    # v4.14.6.10-fix-trl-import (2026-06-11): THE long-hidden bug.
    # v4.14.6.5-cache-ungate-tpm-skip added three `_trl._TLS` references
    # below (~:2032, ~:2034, ~:2480) for the scan-only nonblocking-TPM
    # scope, but never added the matching import in THIS function's
    # scope (`call_provider` imports `_trl` locally, but that binding
    # isn't visible here). So on every scan call, the first `_trl`
    # reference raised NameError, which `_run_cloud_multi`'s broad
    # `except Exception: return {}` swallowed silently — producing the
    # 100% `empty/no-content from provider` log floods we chased
    # across six versions. Imported defensively (try/except, with
    # `_trl = None` on any fault) so the scan keeps working even if
    # tm_rate_limiter ever becomes unimportable — the nonblock
    # optimization is opt-in scaffolding, not a hard dependency.
    try:
        import tm_rate_limiter as _trl
    except Exception:
        _trl = None
    if providers is None:
        providers = load_enabled_providers()
    if not providers:
        # v4.14.6.9-scan-sequential-and-bail-logging (2026-06-11):
        # stamp result_meta so the caller sees this as PROVIDER_UNAVAILABLE
        # (logs as "no eligible providers") instead of an unlabeled
        # empty return that the caller mislogs as "empty/no-content".
        if result_meta is not None:
            result_meta['provider_unavailable'] = True
        return 0

    # scan_provider_filter narrows BEFORE grouping so a single-provider
    # filter still goes through the new model-aware code path (it just
    # produces single-provider groups with no failover possible).
    #
    # v4.14.3.7 (2026-05-14): match against provider unique ID (the
    # per-install UUID like 'b1158f8c'), not display name. The legacy
    # name-based match silently failed whenever the picker's chosen
    # provider had a name that didn't equal the user-typed preset
    # (every preset='custom' provider — Cerebras / GitHub / SambaNova
    # — was unreachable through this filter). Backward compat: if
    # no provider matches by id, fall through to name-based match
    # (case-insensitive) so external callers that still pass display
    # names keep working. A pure-name match logs a soft warning so
    # the caller can migrate.
    if scan_provider_filter:
        filter_lower = str(scan_provider_filter).strip().lower()
        if filter_lower:
            # Primary: match by registry id (case-sensitive exact —
            # UUIDs are case-sensitive).
            matched = [p for p in providers
                       if str(p.get('id') or '') == str(
                           scan_provider_filter).strip()]
            if not matched:
                # Fallback: name match for legacy callers. Case-
                # insensitive to match the pre-v4.14.3.7 contract.
                matched = [p for p in providers
                           if str(p.get('name', '')).strip().lower()
                               == filter_lower]
                if matched and log_fn:
                    try:
                        log_fn(
                            f"  Scan provider filter "
                            f"'{scan_provider_filter}' matched by "
                            f"display name (legacy path); the "
                            f"caller should migrate to passing the "
                            f"provider's unique id.",
                            'muted')
                    except Exception:
                        pass
            providers = matched
            if not providers:
                if log_fn:
                    log_fn(
                        f"  Scan provider filter set to "
                        f"'{scan_provider_filter}' but no enabled "
                        f"provider matches (by id or name). No API "
                        f"call will be made.",
                        'amber')
                # v4.14.6.9-scan-sequential-and-bail-logging: see :1836.
                if result_meta is not None:
                    result_meta['provider_unavailable'] = True
                return 0

    if parse_fn is None:
        try:
            import tm_discover as _tmd
            parse_fn = _tmd.parse_prediction
        except Exception:
            # v4.14.6.9-scan-sequential-and-bail-logging: see :1836.
            if result_meta is not None:
                result_meta['provider_unavailable'] = True
            return 0

    # Soft-import the router. Required for the model-aware path; if
    # genuinely missing (which shouldn't happen since v4.13.56) the
    # function bails cleanly so a botched install doesn't crash scans.
    try:
        import tm_ai_router as _router
    except Exception:
        if log_fn:
            try:
                log_fn(
                    "  AI router module unavailable — scan skipped.",
                    'red')
            except Exception:
                pass
        return 0

    registry = _resolve_model_registry()

    # Build canonical-model groups. With registry: providers serving
    # the same canonical model collapse into one entry with multiple
    # providers (the dedup that fixes gap 3). Without registry: every
    # provider becomes its own group under a synthetic 'unknown/*' id
    # so the scan still runs end-to-end (graceful degrade).
    groups = _router.select_provider_groups(
        providers, call_type='scan', registry=registry, log_fn=log_fn)
    if not groups:
        # v4.14.5.14a.2: no provider could be selected (every eligible
        # one is in cooldown / at daily cap / blocked). This is NOT an
        # AI verdict — the ticker was never analyzed. Default: write
        # nothing, signal the caller so it does NOT advance the cursor,
        # so the ticker is retried when a provider recovers (instead of
        # the 75-min identical-NO_CALL loop). Legacy fake-NO_CALL write
        # is restorable via cfg['use_no_call_on_provider_exhaustion'].
        if not _NO_CALL_ON_EXHAUSTION:
            if result_meta is not None:
                result_meta['provider_unavailable'] = True
            if log_fn:
                try:
                    log_fn(
                        f"  [scan] skipped {ticker} — no eligible "
                        f"providers (will retry when providers "
                        f"available)", 'muted')
                except Exception:
                    pass
            return 0
        # ── legacy path (flag on) ───────────────────────────────────
        if log_fn:
            try:
                filt = scan_provider_filter or '(none)'
                log_fn(
                    f"  run_apis_for_scan_prediction: no eligible "
                    f"providers (filter={filt}) — writing NO_CALL "
                    f"for {ticker}",
                    'amber')
            except Exception:
                pass
        try:
            no_call = {
                'ticker': (ticker or '').upper(),
                'direction': 'NO_CALL',
                'confidence': 'NONE',
                'path': path,
                'source': source,
                'model': 'no_eligible_provider',
                'provider_id': None,
                'canonical_model': None,
                'actual_provider': None,
                'actual_model_string': None,
                'lineup_version': LINEUP_VERSION,
                'error_text': (
                    f"no eligible providers; filter="
                    f"{scan_provider_filter or '(none)'}"),
                'notes': (
                    'select_provider_groups returned empty — every '
                    'eligible provider failed health check (cooldown, '
                    'cap, or blocked endpoint).'),
            }
            if extra_fields:
                no_call.update(extra_fields)
            predictions_log.append(no_call)
        except Exception:
            # Don't let a NO_CALL-write failure cascade. The log line
            # above already surfaced the underlying state.
            pass
        return 0

    run = _router.RouterRun(groups)

    # Index providers by id once for cheap lookups inside the loop.
    providers_by_id = {p.get('id'): p for p in providers
                        if p.get('id')}

    written = 0

    # v4.14.1.1: single-provider scan mode.
    #
    # Pre-v4.14.1.1 the scan runner iterated EVERY canonical-model
    # group per candidate, firing N AI calls per candidate (~4
    # providers × 50 candidates = ~200 calls per scan, blowing
    # through free-tier daily caps in one pass). For scans we want
    # ONE provider per candidate, with round-robin across candidates
    # so load spreads instead of pinning one provider.
    #
    # The gate is is_scan_run_active() — set by begin_scan_run() in
    # the scan loop's try/finally. Other call sites (consensus,
    # lookup_fanout, recommend) don't open a scan-run window and
    # therefore retain the multi-provider fan-out. Per BUILT.md
    # line 24 the call-type policy already exists; this is the
    # missing "one per candidate" rule.
    all_cms = run.all_canonical_models()
    # v4.14.5.14-scan-canonical-fallback: True only for a scan run with the
    # flag on. Drives both the iterate_cms chain (below) and the
    # exhaustion-handling (break on first success, defer the skip emit).
    _scan_active = _router.is_scan_run_active()
    _scan_fallback = bool(_scan_active and _SCAN_CANONICAL_FALLBACK)
    # v4.14.6.5-cache-ungate-tpm-skip (2026-06-11): scan-only non-
    # blocking TPM/RPM. Set the thread-local nonblock flag for the
    # remainder of this scan call so the limiter raises NonBlockingBusy
    # instead of time.sleep — the scan-fallback chain catches it (via
    # call_provider's NonBlockingBusy → ProviderError("429 ...") path)
    # and advances to the next eligible provider INSTANTLY. Only Tier-1
    # scan dispatch reaches here (begin_scan_run/is_scan_run_active
    # gates it). Tier-2 (consensus / lookup_fanout / holdings_consensus)
    # does NOT call run_apis_for_scan_prediction → keeps blocking waits.
    # Cleared at every return path below so an early return or exception
    # can't leak the flag to other code on this worker thread.
    # v4.14.6.10-fix-trl-import: guard against _trl being None (when the
    # rate-limiter module failed to import above). Same shape, but never
    # NameErrors — the scan-only nonblocking optimization is simply
    # skipped if _trl is unavailable, which is strictly safer than the
    # pre-patch crash-and-burn behavior.
    _prev_nonblock_v465 = (
        bool(getattr(getattr(_trl, '_TLS', None), 'nonblock', False))
        if _trl is not None else False)
    if _scan_active and _trl is not None:
        _trl._TLS.nonblock = True
    if _scan_active:
        # v4.14.5.14-capacity-weighted-scan: pass the group + provider
        # maps so the pick is weighted by serving-provider capacity
        # (bias bulk load toward high-headroom providers, away from tight
        # per-minute ceilings like Groq). Fail-open to even round-robin
        # inside the picker if anything's missing.
        pick_cm = _router.next_scan_canonical_pick(
            all_cms, groups=groups, providers_by_id=providers_by_id)
        if (log_fn is not None
                and not _router.scan_first_pick_announced()):
            try:
                log_fn(
                    f"Scan single-provider mode: rotating across "
                    f"{len(all_cms)} canonical model(s)"
                    + ("; on exhaustion, falls over to the next "
                       "eligible model" if _scan_fallback else ""),
                    'muted')
            except Exception:
                pass
            _router.mark_scan_first_pick_announced()
        if _scan_fallback and pick_cm:
            # Ordered chain: the round-robin pick first, then the other
            # eligible canonical models as fallbacks. Depth-capped. We
            # still write only ONE successful record (break on success).
            _rest = [cm for cm in all_cms if cm != pick_cm]
            iterate_cms = ([pick_cm] + _rest)[:_SCAN_FALLBACK_MAX_MODELS]
        else:
            iterate_cms = [pick_cm] if pick_cm else []
    else:
        iterate_cms = all_cms

    # v4.14.5.14-scan-canonical-fallback: extracted so the
    # all-providers-exhausted handling can fire either per-canonical-model
    # (consensus / legacy scan, unchanged) OR once at the end of the
    # fallback chain (scan-fallback). Body is verbatim the pre-patch
    # `if not succeeded:` block.
    def _emit_exhausted(canonical_model, registry_display,
                        is_known_canonical, last_provider_label,
                        last_error):
        if not _NO_CALL_ON_EXHAUSTION:
            if result_meta is not None:
                result_meta['provider_unavailable'] = True
        else:
            err_pred = {
                'ticker': (ticker or '').upper(),
                'direction': 'NO_CALL',
                'confidence': 'NONE',
                'path': path,
                'source': source,
                'model': (registry_display
                           or last_provider_label
                           or canonical_model),
                'provider_id': None,
                'canonical_model': (canonical_model
                                     if is_known_canonical
                                     else None),
                'actual_provider': None,
                'actual_model_string': None,
                'lineup_version': LINEUP_VERSION,
                'error_text': (last_error[:200] if last_error
                                else 'all providers exhausted'),
                'notes': ('All providers exhausted for '
                           'this canonical model.'),
            }
            if extra_fields:
                err_pred.update(extra_fields)
            try:
                predictions_log.append(err_pred)
            except Exception:
                pass
        if log_fn:
            try:
                log_fn(
                    f"[degradation] "
                    f"{registry_display or canonical_model} "
                    f"unavailable on {ticker}; vote skipped",
                    'amber')
            except Exception:
                pass

    # v4.14.5.14-scan-canonical-fallback loop state.
    any_succeeded = False
    _ex_state = None  # (cm, reg_disp, is_known, last_lbl, last_err) of the
    #                   last failed attempt — used for the deferred emit.

    for canonical_model in iterate_cms:
        # v4.14.5.14-scan-canonical-fallback: stop once we've written a
        # successful record (one successful call per candidate), and skip
        # a FALLBACK model whose providers ALL already 429'd/failed-over
        # this run (re-attempting them just burns another rejected call —
        # exhaustion is tracked per (cm, provider) by RouterRun, so we
        # union across the cms tried so far).
        if _scan_fallback:
            if any_succeeded:
                break
            if canonical_model != iterate_cms[0]:
                _dead = set()
                for _c in all_cms:
                    _dead |= run.exhausted_providers(_c)
                _provs = [pid for pid, _ in groups.get(canonical_model, [])]
                if _provs and all(pid in _dead for pid in _provs):
                    continue
        # Display name resolution — for the activity log + the
        # prediction's `model` field. Prefer registry display_name;
        # fall back to the provider's user-given label so legacy
        # Track Record stats keep grouping the way users expect.
        registry_display = None
        if registry is not None:
            try:
                registry_display = registry.get_display_name(
                    canonical_model)
            except Exception:
                registry_display = None
        is_known_canonical = (registry is not None
                               and registry_display is not None
                               and not canonical_model.startswith('unknown/'))

        # Per-canonical-model retry budget. Reset whenever we fail
        # over to a fresh provider.
        transient_attempts_left = _TRANSIENT_RETRY_BUDGET

        succeeded = False
        last_error = ''
        last_provider_label = '?'

        # Loop until either: (a) a successful or no-call response was
        # written, (b) a fatal failure on the current provider stops
        # us, or (c) all providers for this canonical model exhausted.
        while True:
            pick = run.pick(canonical_model)
            if pick is None:
                # Every provider for this canonical model has been
                # marked exhausted (or had a fatal error). Stop and
                # write a single all-providers-failed NO_CALL below.
                break

            provider_id, provider_model_string = pick
            prov = providers_by_id.get(provider_id)
            if prov is None:
                # Defensive: the group references a provider we don't
                # have. Mark it exhausted and try the next one.
                run.mark_exhausted(canonical_model, provider_id)
                transient_attempts_left = _TRANSIENT_RETRY_BUDGET
                continue

            label = display_label(prov)
            last_provider_label = label

            # Resolve the per-(provider, canonical_model) declared
            # cap so the success path can pass it to the auto-raise
            # heuristic. Don't fail the call on a cap-resolution
            # error — just default to None (skip auto-raise).
            declared_cap = None
            try:
                _ok, _reason, declared_cap = (
                    _router.is_eligible_for_model(
                        prov, 'scan', canonical_model))
            except Exception:
                pass

            start = _time.time()
            try:
                response = call_provider(
                    prov, prompt, timeout=timeout, log_fn=log_fn)
            except ProviderError as e:
                err_str = str(e)
                code = _extract_leading_status_code(err_str)
                outcome = _router.classify_failure(
                    status_code=code, error_text=err_str)

                _router.record_call_outcome_for_model(
                    provider_id, canonical_model,
                    outcome=outcome, error_msg=err_str)

                last_error = err_str

                if outcome == _router.OUTCOME_QUOTA:
                    if log_fn:
                        try:
                            # v4.14.0 stage 6d: tag as routing
                            # degradation event.
                            log_fn(
                                f"[degradation] "
                                f"{registry_display or canonical_model} "
                                f"exhausted on {label} ({ticker}); "
                                f"failing over",
                                'amber')
                        except Exception:
                            pass
                    run.mark_exhausted(canonical_model, provider_id)
                    transient_attempts_left = _TRANSIENT_RETRY_BUDGET
                    continue

                if outcome == _router.OUTCOME_TRANSIENT:
                    if transient_attempts_left > 0:
                        backoff_idx = (_TRANSIENT_RETRY_BUDGET
                                        - transient_attempts_left)
                        backoff = _TRANSIENT_BACKOFFS[backoff_idx]
                        if log_fn:
                            try:
                                log_fn(
                                    f"  {label} ({ticker}): "
                                    f"transient ({err_str[:60]}) — "
                                    f"retry in {backoff}s",
                                    'amber')
                            except Exception:
                                pass
                        _time.sleep(backoff)
                        transient_attempts_left -= 1
                        # Sticky pick stays — same provider retries.
                        continue
                    # Out of retries. Fail over.
                    if log_fn:
                        try:
                            # v4.14.0 stage 6d: tag as degradation.
                            log_fn(
                                f"[degradation] {label} ({ticker}): "
                                f"transient retries exhausted; "
                                f"failing over for "
                                f"{registry_display or canonical_model}",
                                'amber')
                        except Exception:
                            pass
                    run.mark_exhausted(canonical_model, provider_id)
                    transient_attempts_left = _TRANSIENT_RETRY_BUDGET
                    continue

                # OUTCOME_FATAL: don't retry, don't fail over.
                if log_fn:
                    try:
                        log_fn(
                            f"  {label} ({ticker}): {err_str[:100]}",
                            'red')
                    except Exception:
                        pass
                break

            except Exception as e:
                err_str = f"{type(e).__name__}: {e}"
                outcome = _router.classify_failure(
                    error_text=str(e), exception=e)
                _router.record_call_outcome_for_model(
                    provider_id, canonical_model,
                    outcome=outcome, error_msg=err_str)
                last_error = err_str
                if (outcome == _router.OUTCOME_TRANSIENT
                        and transient_attempts_left > 0):
                    backoff_idx = (_TRANSIENT_RETRY_BUDGET
                                    - transient_attempts_left)
                    backoff = _TRANSIENT_BACKOFFS[backoff_idx]
                    _time.sleep(backoff)
                    transient_attempts_left -= 1
                    continue
                if outcome != _router.OUTCOME_FATAL:
                    run.mark_exhausted(canonical_model, provider_id)
                    transient_attempts_left = _TRANSIENT_RETRY_BUDGET
                    continue
                if log_fn:
                    try:
                        log_fn(
                            f"  {label} ({ticker}): unexpected error: "
                            f"{type(e).__name__}", 'red')
                    except Exception:
                        pass
                break

            # Success: parse + write exactly one prediction for this
            # canonical model.
            duration = _time.time() - start
            _router.record_call_outcome_for_model(
                provider_id, canonical_model,
                outcome=_router.OUTCOME_SUCCESS,
                declared_cap=declared_cap)

            try:
                pred = parse_fn(response, ticker)
            except Exception:
                pred = {}

            base_extra = {
                'path': path,
                'source': source,
                # Display field — keep the user's provider label as
                # the visible "model" string for Track Record
                # continuity. Stage 5 will switch consensus tally to
                # canonical_model; until then `model` keeps doing
                # what v4.13.x did.
                'model': label,
                'provider_id': provider_id,
                'provider_preset': prov.get('preset'),
                # New v4.14.0 fields. canonical_model is the dedup
                # key; actual_provider is which provider actually
                # served the call (canonical short id like 'groq'
                # or 'cerebras' — distinct from provider_id which is
                # the per-install UUID); actual_model_string is the
                # provider-specific model string. Together they let
                # Track Record show degradation honestly when a
                # smaller-model fallback or cross-provider failover
                # served the call.
                'canonical_model': (canonical_model
                                     if is_known_canonical
                                     else None),
                'actual_provider': _router.provider_canonical_id(prov),
                'actual_model_string': provider_model_string,
                'lineup_version': LINEUP_VERSION,
                'duration_sec': round(duration, 2),
            }

            if pred.get('direction'):
                pred.update(base_extra)
                if extra_fields:
                    pred.update(extra_fields)
                predictions_log.append(pred)
                written += 1
                # v4.14.5.62-concurrent-scan: hand the just-written record
                # back to the caller directly. Lets the queue runner return
                # the row WITHOUT re-reading predictions_log by position —
                # the by-position recovery is fragile when many workers
                # append to the shared log at once. Single-provider scan
                # mode writes exactly one record per call, so this is it.
                if result_meta is not None:
                    result_meta['prediction'] = pred
                if log_fn:
                    try:
                        nice_model = (registry_display
                                       or canonical_model)
                        log_fn(
                            f"  {nice_model} via {label} ({ticker}): "
                            f"{pred.get('direction')} "
                            f"({duration:.1f}s)",
                            'green')
                    except Exception:
                        pass
            else:
                no_call = {
                    'ticker': (ticker or '').upper(),
                    'direction': 'NO_CALL',
                    'confidence': 'NONE',
                    'raw_text': (response[:500] if response else ''),
                    'notes': 'API returned no parseable direction.',
                }
                no_call.update(base_extra)
                if extra_fields:
                    no_call.update(extra_fields)
                predictions_log.append(no_call)
                # v4.14.5.62-concurrent-scan: same return-the-row handoff
                # for the no-direction case (caller treats a non-BUY record
                # as "skip" — but still needs the record, not a None).
                if result_meta is not None:
                    result_meta['prediction'] = no_call
                if log_fn:
                    try:
                        nice_model = (registry_display
                                       or canonical_model)
                        log_fn(
                            f"  {nice_model} via {label} ({ticker}): "
                            f"no-call ({duration:.1f}s)",
                            'muted')
                    except Exception:
                        pass

            succeeded = True
            break  # done with this canonical model

        # v4.14.5.14-scan-canonical-fallback: success/exhaustion handling.
        if succeeded:
            any_succeeded = True
            if _scan_fallback:
                break  # one SUCCESSFUL call per candidate — stop the chain
            # consensus / legacy scan: proceed to the next canonical model
            # (unchanged — the for-loop would have continued anyway).
            continue
        # Every provider for this canonical model was exhausted.
        if _scan_fallback:
            # Defer the give-up: only emit the skip once the WHOLE chain
            # has failed. Remember this attempt's display state for that
            # final emit, and try the next eligible canonical model.
            _ex_state = (canonical_model, registry_display,
                         is_known_canonical, last_provider_label,
                         last_error)
            if log_fn:
                try:
                    log_fn(
                        f"  [scan-fallback] "
                        f"{registry_display or canonical_model} "
                        f"unavailable on {ticker}; trying next "
                        f"eligible canonical model",
                        'muted')
                except Exception:
                    pass
            continue
        # Consensus / legacy scan: write a single all-exhausted signal per
        # canonical model (v4.13.x wrote one per provider; the new shape
        # writes ONE per canonical model). Behaviour unchanged from
        # pre-patch — the body now lives in _emit_exhausted.
        _emit_exhausted(canonical_model, registry_display,
                        is_known_canonical, last_provider_label,
                        last_error)

    # v4.14.5.14-scan-canonical-fallback: the entire fallback chain failed
    # → nothing was written, so emit the skip ONCE (using the last failed
    # attempt's display state) so the caller still gets provider_unavailable.
    if _scan_fallback and not any_succeeded and _ex_state is not None:
        _emit_exhausted(*_ex_state)
    elif not any_succeeded:
        # v4.14.6.9-scan-sequential-and-bail-logging (2026-06-11):
        # belt-and-braces — any function exit that produced NO successful
        # record AND didn't flow through _emit_exhausted MUST stamp
        # provider_unavailable so the caller logs honestly. The pre-fix
        # silent path was: for-loop body never runs (iterate_cms empty or
        # pick_cm None) → _ex_state stays None → the guarded
        # _emit_exhausted above never fires → flag never set → caller
        # mislogged as "empty/no-content from provider". This branch
        # guarantees the flag is set in that case AND surfaces a single
        # diagnostic line (Fix C2) saying WHY the for-loop produced
        # nothing — pick_cm value, iterate_cms length, and the
        # scan_provider_filter that narrowed things.
        if result_meta is not None:
            result_meta['provider_unavailable'] = True
        if log_fn:
            try:
                # _scan_active and pick_cm are local-scope above; refer
                # to them defensively in case some early branch left
                # them undefined.
                _pcm = locals().get('pick_cm', '<unset>')
                _icms = locals().get('iterate_cms', None)
                _icms_len = (len(_icms) if isinstance(_icms, list)
                             else '<unset>')
                _scan_act = locals().get('_scan_active', '<unset>')
                log_fn(
                    f"  [scan-bail] {ticker}: no successful call, "
                    f"_ex_state unset (scan_active={_scan_act}, "
                    f"pick_cm={_pcm!r}, iterate_cms_len={_icms_len}, "
                    f"scan_provider_filter={scan_provider_filter!r}). "
                    f"Flagging provider_unavailable so the caller "
                    f"logs 'no eligible providers' instead of "
                    f"'empty/no-content'.",
                    'amber')
            except Exception:
                pass

    # v4.14.6.5-cache-ungate-tpm-skip: restore the thread-local nonblock
    # flag to whatever it was before this scan call (nested scans
    # supported; outer scope is preserved).
    try:
        _trl._TLS.nonblock = _prev_nonblock_v465
    except Exception:
        pass
    return written
