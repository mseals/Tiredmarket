"""
tm_provider_discovery.py — Smart provider lookup + endpoint discovery (v4.13.59)

What this is:
    A registry of known cloud AI and data providers. Lets the user
    type a name like "groq", "openrouter", "massive.com", or
    "polygon" and have the app fill in the endpoint, signup URL,
    auth style, and discoverable models endpoint.

Why this exists:
    Adding a new provider used to require knowing:
      - The exact API endpoint URL (varies wildly per provider)
      - The auth header style (Bearer vs X-API-Key vs query param)
      - The signup URL (where to actually GET a key)
      - Which models that provider offers
    The user shouldn't have to know any of this. The app does.

What it does NOT do:
    - It does NOT replace the preset system in tm_api_providers.py.
      Presets are the FORMATTED config records (with 'preset': 'groq').
      This module's data is the LOOKUP TABLE — what we know about
      each provider name. They co-exist.
    - It does NOT make API calls during lookup. Pure dictionary
      lookup + fuzzy matching. Network only happens when the user
      explicitly clicks "Discover models" (which calls /models).

Public API:
    lookup(name: str) -> Optional[ProviderInfo]
        Fuzzy-find a provider by name/domain/alias.

    discover_models(provider_info, api_key: str) -> list[str]
        HTTP GET to {endpoint}/models, returns model name list.

    list_all() -> list[ProviderInfo]
        Returns all known providers, alphabetized.
"""

from __future__ import annotations

import json
import re
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─── v4.14.0 stage 7: shared constants ──────────────────────────────────
#
# User-Agent required on every /v1/models call. Cloudflare-fronted
# endpoints (Groq especially) reject the default 'Python-urllib/3.x'
# with HTTP 403 + Cloudflare error 1010. Mirror the User-Agent used by
# tm_api_providers._http_post_json so both code paths look like the
# same client to upstream providers.
_USER_AGENT = 'TiredMarket/4.13.42 (Python)'


@dataclass
class ProviderInfo:
    """One known provider's metadata."""
    canonical_name: str       # The "real" name we display (e.g. "Groq")
    aliases: list[str]        # All synonyms users might type
    kind: str                 # 'ai' or 'data'
    chat_endpoint: str = ''   # Full URL for chat completions
    models_endpoint: str = '' # Full URL for /models discovery (if any)
    base_url: str = ''        # Domain root (used for derivation)
    signup_url: str = ''      # Where to get a free API key
    auth_style: str = 'bearer'  # 'bearer' / 'x-api-key' / 'query-param' / 'none'
    auth_param_name: str = '' # For non-Bearer styles
    needs_key: bool = True
    api_format: str = 'openai_compat'  # 'openai_compat' / 'anthropic' / 'google' / 'custom'
    free_tier_note: str = ''
    sample_models: list[str] = field(default_factory=list)


# ─── Known provider registry ──────────────────────────────────────────
#
# Order matters loosely: most popular first so list_all() shows them
# at the top. Aliases are case-insensitive — we lower() everything
# during lookup. Domain aliases (e.g. "groq.com") work too.

KNOWN_PROVIDERS: list[ProviderInfo] = [
    # ── AI providers ──
    ProviderInfo(
        canonical_name='Groq',
        aliases=['groq', 'groq.com', 'console.groq.com',
                  'groqcloud', 'groq cloud'],
        kind='ai',
        chat_endpoint='https://api.groq.com/openai/v1/chat/completions',
        models_endpoint='https://api.groq.com/openai/v1/models',
        base_url='https://api.groq.com',
        signup_url='https://console.groq.com/keys',
        auth_style='bearer',
        api_format='openai_compat',
        free_tier_note=(
            'Free: 30 RPM, 14,400 RPD on small models, 1,000 RPD on '
            '70B+ models. No credit card. Very fast inference.'),
        sample_models=[
            'llama-3.1-8b-instant',
            'llama-3.3-70b-versatile',
            'mixtral-8x7b-32768',
        ],
    ),
    ProviderInfo(
        canonical_name='Mistral La Plateforme',
        aliases=['mistral', 'mistral.ai', 'mistralai', 'mistral ai',
                  'la plateforme', 'console.mistral.ai'],
        kind='ai',
        chat_endpoint='https://api.mistral.ai/v1/chat/completions',
        models_endpoint='https://api.mistral.ai/v1/models',
        base_url='https://api.mistral.ai',
        signup_url='https://console.mistral.ai/',
        auth_style='bearer',
        api_format='openai_compat',
        free_tier_note='Free Experiment plan: 1B tokens/month. EU-hosted.',
        sample_models=[
            'mistral-small-latest',
            'mistral-large-latest',
            'open-mistral-nemo',
        ],
    ),
    ProviderInfo(
        canonical_name='Google Gemini',
        aliases=['gemini', 'google', 'googleai', 'google ai',
                  'aistudio', 'ai studio', 'google ai studio',
                  'generativelanguage'],
        kind='ai',
        chat_endpoint='https://generativelanguage.googleapis.com/v1beta/models',
        models_endpoint='https://generativelanguage.googleapis.com/v1beta/models',
        base_url='https://generativelanguage.googleapis.com',
        signup_url='https://aistudio.google.com/app/apikey',
        auth_style='query-param',
        auth_param_name='key',
        api_format='google',
        free_tier_note='Free: 1500 RPD on flash models. Long context window.',
        sample_models=[
            'gemini-2.0-flash',
            'gemini-1.5-pro',
            'gemini-1.5-flash',
        ],
    ),
    ProviderInfo(
        canonical_name='Cerebras',
        aliases=['cerebras', 'cerebras.ai', 'cerebras inference',
                  'cloud.cerebras.ai'],
        kind='ai',
        chat_endpoint='https://api.cerebras.ai/v1/chat/completions',
        models_endpoint='https://api.cerebras.ai/v1/models',
        base_url='https://api.cerebras.ai',
        signup_url='https://cloud.cerebras.ai/',
        auth_style='bearer',
        api_format='openai_compat',
        free_tier_note=(
            'Free: 1M tokens/day, 30 RPM. Fastest inference '
            '(~1000+ tok/s on WSE-3 chips).'),
        sample_models=['llama3.1-8b', 'llama3.1-70b'],
    ),
    ProviderInfo(
        canonical_name='SambaNova Cloud',
        aliases=['sambanova', 'samba', 'samba nova', 'sambanova.ai',
                  'cloud.sambanova.ai'],
        kind='ai',
        chat_endpoint='https://api.sambanova.ai/v1/chat/completions',
        models_endpoint='https://api.sambanova.ai/v1/models',
        base_url='https://api.sambanova.ai',
        signup_url='https://cloud.sambanova.ai/',
        auth_style='bearer',
        api_format='openai_compat',
        free_tier_note=(
            'Free: ~20 calls/day. Models rotate — check '
            'tm_model_deprecations for current available list.'),
        sample_models=[
            'DeepSeek-V3.1',
            'Meta-Llama-3.3-70B-Instruct',
            'gpt-oss-120b',
        ],
    ),
    ProviderInfo(
        canonical_name='GitHub Models',
        aliases=['github', 'github models', 'github.com/marketplace/models',
                  'azure inference', 'models.inference.ai.azure.com'],
        kind='ai',
        chat_endpoint='https://models.inference.ai.azure.com/chat/completions',
        models_endpoint='https://models.inference.ai.azure.com/models',
        base_url='https://models.inference.ai.azure.com',
        signup_url='https://github.com/marketplace/models',
        auth_style='bearer',
        api_format='openai_compat',
        free_tier_note='Free: ~50 calls/day. GitHub PAT with models scope.',
        sample_models=['gpt-4o', 'gpt-4o-mini', 'Phi-4'],
    ),
    ProviderInfo(
        canonical_name='OpenRouter',
        aliases=['openrouter', 'openrouter.ai', 'open router'],
        kind='ai',
        chat_endpoint='https://openrouter.ai/api/v1/chat/completions',
        models_endpoint='https://openrouter.ai/api/v1/models',
        base_url='https://openrouter.ai',
        signup_url='https://openrouter.ai/keys',
        auth_style='bearer',
        api_format='openai_compat',
        free_tier_note='Aggregator. Some free models; others paid via credits.',
        sample_models=[
            'meta-llama/llama-3.3-70b-instruct:free',
            'google/gemini-2.0-flash-exp:free',
            'qwen/qwen-2.5-72b-instruct:free',
        ],
    ),
    ProviderInfo(
        canonical_name='Together AI',
        aliases=['together', 'together.ai', 'togetherai', 'together ai'],
        kind='ai',
        chat_endpoint='https://api.together.xyz/v1/chat/completions',
        models_endpoint='https://api.together.xyz/v1/models',
        base_url='https://api.together.xyz',
        signup_url='https://api.together.xyz/settings/api-keys',
        auth_style='bearer',
        api_format='openai_compat',
        free_tier_note='Trial credits + select free models marked "-Free".',
        sample_models=[
            'meta-llama/Llama-3-70b-chat-hf',
            'mistralai/Mixtral-8x7B-Instruct-v0.1',
        ],
    ),
    ProviderInfo(
        canonical_name='Fireworks AI',
        aliases=['fireworks', 'fireworks.ai', 'fireworksai'],
        kind='ai',
        chat_endpoint='https://api.fireworks.ai/inference/v1/chat/completions',
        models_endpoint='https://api.fireworks.ai/inference/v1/models',
        base_url='https://api.fireworks.ai',
        signup_url='https://fireworks.ai/account/api-keys',
        auth_style='bearer',
        api_format='openai_compat',
        free_tier_note='Free credits on signup. Pay-per-token after.',
        sample_models=[
            'accounts/fireworks/models/llama-v3p3-70b-instruct',
        ],
    ),
    ProviderInfo(
        canonical_name='DeepInfra',
        aliases=['deepinfra', 'deepinfra.com'],
        kind='ai',
        chat_endpoint='https://api.deepinfra.com/v1/openai/chat/completions',
        models_endpoint='https://api.deepinfra.com/v1/openai/models',
        base_url='https://api.deepinfra.com',
        signup_url='https://deepinfra.com/dash/api_keys',
        auth_style='bearer',
        api_format='openai_compat',
        free_tier_note='Free credits on signup.',
        sample_models=[
            'meta-llama/Meta-Llama-3.1-70B-Instruct',
        ],
    ),
    ProviderInfo(
        canonical_name='Anthropic Claude',
        aliases=['anthropic', 'claude', 'anthropic.com',
                  'console.anthropic.com'],
        kind='ai',
        chat_endpoint='https://api.anthropic.com/v1/messages',
        models_endpoint='https://api.anthropic.com/v1/models',
        base_url='https://api.anthropic.com',
        signup_url='https://console.anthropic.com/',
        auth_style='x-api-key',
        auth_param_name='x-api-key',
        api_format='anthropic',
        free_tier_note='Paid only. No free tier on API.',
        sample_models=[
            'claude-3-5-sonnet-latest',
            'claude-3-5-haiku-latest',
        ],
    ),
    ProviderInfo(
        canonical_name='OpenAI',
        aliases=['openai', 'openai.com', 'chatgpt', 'gpt',
                  'platform.openai.com'],
        kind='ai',
        chat_endpoint='https://api.openai.com/v1/chat/completions',
        models_endpoint='https://api.openai.com/v1/models',
        base_url='https://api.openai.com',
        signup_url='https://platform.openai.com/api-keys',
        auth_style='bearer',
        api_format='openai_compat',
        free_tier_note='Paid only. Pay-per-token.',
        sample_models=['gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo'],
    ),
    ProviderInfo(
        canonical_name='xAI Grok',
        aliases=['xai', 'x.ai', 'grok', 'console.x.ai'],
        kind='ai',
        chat_endpoint='https://api.x.ai/v1/chat/completions',
        models_endpoint='https://api.x.ai/v1/models',
        base_url='https://api.x.ai',
        signup_url='https://console.x.ai/',
        auth_style='bearer',
        api_format='openai_compat',
        free_tier_note='Trial credits available, paid after.',
        sample_models=['grok-2', 'grok-2-mini'],
    ),
    # v4.14.5.65-provider-save-fix: Cloudflare Workers AI. Adding this
    # entry stops save-time validation from synthesizing a custom
    # ProviderInfo and GETting /ai/v1/models (which 405s on Cloudflare
    # — every correctly-configured user got blocked at save). With
    # models_endpoint='' the validator early-returns (True,'ok',...)
    # mirroring Perplexity below; the first real scan POSTs to
    # chat_endpoint and validates the key for real.
    # Note: chat_endpoint template contains {ACCOUNT_ID}. The provider
    # dialog substitutes it from a labeled Account-ID field before
    # save (Part 2 of this build) — the user never edits the URL.
    ProviderInfo(
        canonical_name='Cloudflare Workers AI',
        aliases=['cloudflare', 'cloudflare workers ai',
                  'workers ai', 'workers.ai',
                  'api.cloudflare.com'],
        kind='ai',
        chat_endpoint=('https://api.cloudflare.com/client/v4/accounts/'
                       '{ACCOUNT_ID}/ai/v1/chat/completions'),
        models_endpoint='',  # No GETtable /models on Cloudflare REST
        base_url='https://api.cloudflare.com',
        signup_url='https://dash.cloudflare.com/profile/api-tokens',
        auth_style='bearer',
        api_format='openai_compat',
        free_tier_note=(
            'Free: 10,000 neurons/day. Requires your Account ID and a '
            'Workers AI Read+Edit API token.'),
        sample_models=['@cf/meta/llama-3.3-70b-instruct-fp8-fast',
                        '@cf/qwen/qwq-32b'],
    ),
    ProviderInfo(
        canonical_name='Perplexity',
        aliases=['perplexity', 'perplexity.ai', 'pplx'],
        kind='ai',
        chat_endpoint='https://api.perplexity.ai/chat/completions',
        models_endpoint='',  # No public /models endpoint
        base_url='https://api.perplexity.ai',
        signup_url='https://www.perplexity.ai/settings/api',
        auth_style='bearer',
        api_format='openai_compat',
        free_tier_note='$5 monthly free credit on signup.',
        sample_models=['sonar', 'sonar-pro'],
    ),
    # v4.14.5.14-ollama-purge-step4: the 'Ollama (local)' provider descriptor
    # was removed — Ollama is retired (cloud-only). (LM Studio, a different
    # local OpenAI-compatible server, is unrelated and kept.)
    ProviderInfo(
        canonical_name='LM Studio (local)',
        aliases=['lmstudio', 'lm studio', 'localhost:1234'],
        kind='ai',
        chat_endpoint='http://localhost:1234/v1/chat/completions',
        models_endpoint='http://localhost:1234/v1/models',
        base_url='http://localhost:1234',
        signup_url='',
        auth_style='none',
        needs_key=False,
        api_format='openai_compat',
        free_tier_note='Local. Run via the LM Studio desktop app.',
        sample_models=[],
    ),
    # ── Data providers (news / quotes / fundamentals) ──
    ProviderInfo(
        canonical_name='Finnhub',
        aliases=['finnhub', 'finnhub.io'],
        kind='data',
        chat_endpoint='',  # Not a chat provider
        models_endpoint='',
        base_url='https://finnhub.io/api/v1',
        signup_url='https://finnhub.io/register',
        auth_style='x-api-key',
        auth_param_name='X-Finnhub-Token',
        api_format='custom',
        free_tier_note='Free: 60 calls/min. Best for news, fundamentals, earnings.',
    ),
    ProviderInfo(
        canonical_name='Massive (formerly Polygon)',
        aliases=['massive', 'massive.com', 'polygon', 'polygon.io',
                  'polygonio'],
        kind='data',
        chat_endpoint='',
        models_endpoint='',
        base_url='https://api.polygon.io',
        signup_url='https://polygon.io/dashboard/signup',
        auth_style='query-param',
        auth_param_name='apiKey',
        api_format='custom',
        free_tier_note='Free: 5 calls/min. Real-time data on paid plans.',
    ),
    ProviderInfo(
        canonical_name='Alpha Vantage',
        aliases=['alpha vantage', 'alphavantage', 'alphavantage.co'],
        kind='data',
        chat_endpoint='',
        models_endpoint='',
        base_url='https://www.alphavantage.co/query',
        signup_url='https://www.alphavantage.co/support/#api-key',
        auth_style='query-param',
        auth_param_name='apikey',
        api_format='custom',
        free_tier_note='Free: 25 calls/day, 5 calls/min. Limited.',
    ),
    ProviderInfo(
        canonical_name='Marketaux',
        aliases=['marketaux', 'marketaux.com'],
        kind='data',
        chat_endpoint='',
        models_endpoint='',
        base_url='https://api.marketaux.com/v1',
        signup_url='https://www.marketaux.com/account/register',
        auth_style='query-param',
        auth_param_name='api_token',
        api_format='custom',
        free_tier_note='Free: 100 requests/day. Real-time financial news + sentiment.',
    ),
    ProviderInfo(
        canonical_name='NewsAPI',
        aliases=['newsapi', 'newsapi.org', 'news api'],
        kind='data',
        chat_endpoint='',
        models_endpoint='',
        base_url='https://newsapi.org/v2',
        signup_url='https://newsapi.org/register',
        auth_style='x-api-key',
        auth_param_name='X-Api-Key',
        api_format='custom',
        free_tier_note='Free: 100 requests/day. General news, ticker filtering needed.',
    ),
    ProviderInfo(
        canonical_name='Twelve Data',
        aliases=['twelve data', 'twelvedata', 'twelvedata.com'],
        kind='data',
        chat_endpoint='',
        models_endpoint='',
        base_url='https://api.twelvedata.com',
        signup_url='https://twelvedata.com/register',
        auth_style='query-param',
        auth_param_name='apikey',
        api_format='custom',
        free_tier_note='Free: 800 requests/day. Largest free quota for news.',
    ),
    ProviderInfo(
        canonical_name='Tiingo',
        aliases=['tiingo', 'tiingo.com'],
        kind='data',
        chat_endpoint='',
        models_endpoint='',
        base_url='https://api.tiingo.com',
        signup_url='https://www.tiingo.com/account/api/token',
        auth_style='query-param',
        auth_param_name='token',
        api_format='custom',
        free_tier_note='Free tier with 500 requests/hr.',
    ),
    ProviderInfo(
        canonical_name='Financial Modeling Prep',
        aliases=['fmp', 'financial modeling prep', 'financialmodelingprep',
                  'financialmodelingprep.com'],
        kind='data',
        chat_endpoint='',
        models_endpoint='',
        base_url='https://financialmodelingprep.com/api/v3',
        signup_url='https://site.financialmodelingprep.com/developer',
        auth_style='query-param',
        auth_param_name='apikey',
        api_format='custom',
        free_tier_note='Free: 250 calls/day. Fundamentals + financials.',
    ),
]


# ─── Lookup helpers ───────────────────────────────────────────────────

def _normalize(s: str) -> str:
    """Lowercase + strip + collapse separators for fuzzy match."""
    if not s:
        return ''
    s = s.strip().lower()
    # Strip URL scheme/path so "https://api.groq.com/v1" -> "groq.com"
    if '://' in s:
        s = s.split('://', 1)[1]
    if '/' in s:
        s = s.split('/', 1)[0]
    # Strip subdomain prefixes for matching (api.groq.com -> groq.com)
    for prefix in ('api.', 'console.', 'cloud.', 'www.'):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s.strip()


def lookup(name: str) -> Optional[ProviderInfo]:
    """Find a known provider by name, alias, or domain.

    Args:
        name: User-typed string. Can be canonical name ("Groq"),
              alias ("groq.com"), URL ("https://api.groq.com/..."),
              or close approximation. Case-insensitive.

    Returns:
        ProviderInfo if found. None if no match.
    """
    if not name or not name.strip():
        return None
    target = _normalize(name)
    if not target:
        return None
    # Pass 1: exact match against canonical name (lowered) or aliases
    for p in KNOWN_PROVIDERS:
        if target == _normalize(p.canonical_name):
            return p
        for alias in p.aliases:
            if target == _normalize(alias):
                return p
    # Pass 2: substring match — target appears in any alias or canonical
    for p in KNOWN_PROVIDERS:
        for alias in [p.canonical_name] + p.aliases:
            norm = _normalize(alias)
            if norm and (target in norm or norm in target):
                return p
    return None


def list_all(kind: Optional[str] = None) -> list[ProviderInfo]:
    """Return all known providers, optionally filtered by kind ('ai' | 'data')."""
    if kind is None:
        return list(KNOWN_PROVIDERS)
    return [p for p in KNOWN_PROVIDERS if p.kind == kind]


def discover_models(provider: ProviderInfo,
                      api_key: str = '',
                      timeout: float = 8.0) -> list[str]:
    """HTTP GET {provider.models_endpoint} with the user's key.
    Returns a list of model name strings.

    Most OpenAI-compatible providers return:
        {"data": [{"id": "model-name", ...}, ...]}

    Some return a flat list, some return a different schema. We try
    the most common shapes and fall back to the sample_models if all
    parsing fails.

    Args:
        provider: ProviderInfo from lookup() or list_all()
        api_key: user's API key (or empty for keyless providers)
        timeout: HTTP timeout in seconds

    Returns:
        List of model name strings. May be empty if discovery fails.
        On any error, returns provider.sample_models as fallback.
    """
    if not provider.models_endpoint:
        return list(provider.sample_models)
    try:
        url = provider.models_endpoint
        headers = {}
        # Apply auth based on style
        if provider.auth_style == 'bearer' and api_key:
            headers['Authorization'] = f'Bearer {api_key}'
        elif provider.auth_style == 'x-api-key' and api_key:
            param = provider.auth_param_name or 'x-api-key'
            headers[param] = api_key
        elif provider.auth_style == 'query-param' and api_key:
            param = provider.auth_param_name or 'key'
            sep = '&' if '?' in url else '?'
            url = f"{url}{sep}{param}={api_key}"
        # 'none' style: no auth needed (Ollama, LM Studio)

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
        data = json.loads(raw)
    except Exception:
        return list(provider.sample_models)

    # Parse known shapes
    models = _extract_models(data)
    if models:
        return models
    return list(provider.sample_models)


def _extract_models(data) -> list[str]:
    """Pull model name strings from common API response shapes.

    Handles:
      {"data": [{"id": "..."} | str, ...]}
      [{"id": "..."} | str, ...]
      {"models": [...]}            # Google, Ollama
      {"object": "list", "data": [...]}
    """
    candidates = []
    if isinstance(data, dict):
        for key in ('data', 'models'):
            if key in data and isinstance(data[key], list):
                candidates = data[key]
                break
    elif isinstance(data, list):
        candidates = data
    out = []
    for item in candidates:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            for k in ('id', 'name', 'model_id', 'modelId'):
                if k in item and isinstance(item[k], str):
                    out.append(item[k])
                    break
    # Dedup while preserving order
    seen = set()
    deduped = []
    for m in out:
        if m and m not in seen:
            seen.add(m)
            deduped.append(m)
    return deduped


def open_signup_url(provider: ProviderInfo) -> bool:
    """Launch the provider's signup page in the user's default browser.

    Returns True if the browser was launched (or attempt was made),
    False if the provider has no signup URL or the launch failed.
    """
    if not provider.signup_url:
        return False
    try:
        import webbrowser
        webbrowser.open(provider.signup_url, new=2)  # new=2 = new tab
        return True
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════════════
# v4.14.0 stage 7 — model discovery integration
# ════════════════════════════════════════════════════════════════════════
#
# Three responsibilities added to this module in stage 7:
#
#   1. validate_key(provider_info, api_key) — make a minimal
#      /v1/models call with the user's key and return a structured
#      result the caller can paint inline. Distinguishes "key invalid"
#      from "network blip" from "provider 5xx" — each gets its own
#      plain-English message + recovery hint.
#
#   2. match_canonical(provider_id, model_string) — pattern-match a
#      provider's model string to a canonical_id from
#      data/model_registry.default.json. Returns None if no pattern
#      matches OR if the matched canonical_id doesn't exist in the
#      registry (Q4 + I-2 of the locked stage-7 spec — discovery does
#      NOT auto-add new canonical models).
#
#   3. log_discovery_event(...) — append a JSON line to
#      data/discovery.log.jsonl. Used by stage 7's discovery loops to
#      record unmapped models, validation failures, discovery errors
#      WITHOUT cluttering the user-facing activity log (Q4 explicitly
#      excludes activity log for these).
#
# discover_models() above stays untouched — its always-return-something
# semantic is what legacy callers expect. Stage 7 calls validate_key()
# instead when it needs to know if the call actually succeeded.
# ════════════════════════════════════════════════════════════════════════


# ─── Pattern registry: provider model string → canonical_id ────────────
#
# Iteration order matters — first match wins. More specific patterns
# (e.g. "gpt-4o-mini") MUST come before more general ones ("gpt-4o").
#
# Patterns map to canonical_ids that exist in data/model_registry
# .default.json (or that the user has added to data/model_registry
# .json). When match_canonical returns one of these and the registry
# doesn't know about it, the caller treats it as unmapped (per Q4).
#
# Adding a new pattern: it only "lights up" once a corresponding
# canonical_id exists in the registry. Adding the canonical_id without
# adding a pattern is also fine — pattern-match is the discovery
# fallback; static registry entries already do the mapping for
# whatever they cover directly.

MODEL_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # (compiled_regex, canonical_id, label_for_diagnostics)

    # Llama family
    (re.compile(r'(?i)llama.*?3[.\-_ ]?3.*?70b'),
     'meta/llama-3.3-70b-instruct', 'Llama 3.3 70B'),
    (re.compile(r'(?i)llama.*?3[.\-_ ]?1.*?8b'),
     'meta/llama-3.1-8b-instruct', 'Llama 3.1 8B'),

    # GPT family — mini BEFORE non-mini so first-match-wins picks the
    # specific one when both could match.
    (re.compile(r'(?i)gpt.{0,3}4o.*?mini'),
     'openai/gpt-4o-mini', 'GPT-4o Mini'),
    (re.compile(r'(?i)gpt.{0,3}4o'),
     'openai/gpt-4o', 'GPT-4o'),
    # gpt-oss has no '4o' so it can't hit the patterns above; distinct id.
    (re.compile(r'(?i)gpt[.\-_ ]?oss'),
     'openai/gpt-oss-120b', 'GPT-OSS 120B'),

    # Mistral family — large / medium / small are keyword-exclusive, so
    # order among them is harmless; mixtral ('x') never matches 'mistral'.
    (re.compile(r'(?i)mistral.*?large'),
     'mistral/mistral-large', 'Mistral Large'),
    (re.compile(r'(?i)mistral.*?medium'),
     'mistral/mistral-medium', 'Mistral Medium'),
    (re.compile(r'(?i)mistral.*?small'),
     'mistral/mistral-small', 'Mistral Small'),
    (re.compile(r'(?i)mixtral.*?8x7b'),
     'mistral/mixtral-8x7b', 'Mixtral 8x7B'),

    # Gemini family — flash-lite MUST precede plain flash (first-match-
    # wins), then pro. 2.5 vs 1.5 keep them distinct.
    (re.compile(r'(?i)gemini.*?2[.\-_ ]?5.*?flash.*?lite'),
     'google/gemini-2.5-flash-lite', 'Gemini 2.5 Flash Lite'),
    (re.compile(r'(?i)gemini.*?2[.\-_ ]?5.*?flash'),
     'google/gemini-2.5-flash', 'Gemini 2.5 Flash'),
    (re.compile(r'(?i)gemini.*?2[.\-_ ]?5.*?pro'),
     'google/gemini-2.5-pro', 'Gemini 2.5 Pro'),
    (re.compile(r'(?i)gemini.*?1[.\-_ ]?5.*?pro'),
     'google/gemini-1.5-pro', 'Gemini 1.5 Pro'),

    # DeepSeek
    (re.compile(r'(?i)deepseek.*?v3'),
     'deepseek/deepseek-v3', 'DeepSeek V3'),

    # Zhipu GLM family — 4.5 / 4.7 flash
    (re.compile(r'(?i)glm.*?4[.\-_ ]?5.*?flash'),
     'zhipu/glm-4.5-flash', 'GLM-4.5 Flash'),
    (re.compile(r'(?i)glm.*?4[.\-_ ]?7.*?flash'),
     'zhipu/glm-4.7-flash', 'GLM-4.7 Flash'),
]


# ─── Module-level lazy-cached registry handle ──────────────────────────
# match_canonical needs to verify the candidate canonical_id actually
# exists in tm_model_registry. We lazy-import + cache the registry to
# avoid re-reading model_registry.default.json on every match call,
# and to avoid an import-time dependency on tm_model_registry (which
# pulls in pathlib / threading).

_registry_cache = None
_registry_cache_data_dir = None


def _resolve_data_dir(data_dir=None) -> Optional[Path]:
    """Find data/ relative to the script. Mirrors the resolution used
    by tm_api_providers.load_enabled_providers."""
    if data_dir is not None:
        return Path(data_dir)
    candidates = [__import__('tm_paths').get_data_dir(), Path('data')]
    try:
        here = Path(__file__).parent
        candidates.append(__import__('tm_paths').get_data_dir())
    except Exception:
        pass
    for d in candidates:
        try:
            if d.exists():
                return d
        except Exception:
            continue
    return None


def _get_registry(data_dir=None):
    """Return a cached tm_model_registry.ModelRegistry instance, or
    None if it can't be loaded. Cached per data_dir so different
    callers reusing the default path share one instance."""
    global _registry_cache, _registry_cache_data_dir
    resolved = _resolve_data_dir(data_dir)
    if resolved is None:
        return None
    if (_registry_cache is not None
            and _registry_cache_data_dir == resolved):
        return _registry_cache
    try:
        import tm_model_registry as _tmr
        _registry_cache = _tmr.ModelRegistry(resolved)
        _registry_cache_data_dir = resolved
        return _registry_cache
    except Exception:
        return None


def match_canonical(provider_id: str, model_string: str,
                    registry=None) -> Optional[str]:
    """Pattern-match a provider's model string to a canonical_id.

    Returns canonical_id if a pattern matches AND the canonical_id
    is known to the model registry. Returns None if:
      - model_string is empty/None
      - no pattern matches
      - a pattern matches but the canonical_id is unknown to the
        registry (Q4 + I-2: discovery does NOT auto-add new canonicals)

    `registry` is a tm_model_registry.ModelRegistry instance. When
    omitted, a module-level cached instance is used (loads
    data/model_registry.default.json + override on first call).

    `provider_id` is currently informational — patterns aren't
    provider-scoped today. Reserved for future per-provider patterns
    if the same string maps to different canonicals on different
    providers.
    """
    if not model_string:
        return None
    candidate = None
    for regex, canonical_id, _label in MODEL_PATTERNS:
        if regex.search(model_string):
            candidate = canonical_id
            break
    if candidate is None:
        return None
    reg = registry if registry is not None else _get_registry()
    if reg is None:
        # Registry unavailable — be conservative and skip rather than
        # auto-add. Caller will log_discovery_event as unmapped.
        return None
    try:
        if reg.get_display_name(candidate) is None:
            return None
        return candidate
    except Exception:
        return None


# ─── /v1/models validation with structured failure categories ──────────

def validate_key(provider: ProviderInfo,
                  api_key: str = '',
                  timeout: float = 8.0
                  ) -> tuple[bool, str, str, list[str]]:
    """Validate an API key by hitting the provider's /v1/models endpoint.

    Returns (ok, category, plain_english, models):
      - ok: True if the call succeeded with a usable response.
      - category: one of:
          'ok'            — success
          'invalid_key'   — 401 / no key supplied for keyed provider
          'forbidden'     — 403 (key valid but lacks scope)
          'network'       — couldn't reach server
          'timeout'       — request took too long
          'server_error'  — 5xx from provider
          'unknown'       — other (4xx-other, parse errors, etc.)
      - plain_english: ready-to-display message including a recovery
        hint per the marketable-build-user-target decision in
        DECISIONS.md (2026-05-08).
      - models: list of discovered model name strings on success;
        empty list on any failure.

    Sends 'TiredMarket/4.13.42 (Python)' User-Agent — Cloudflare-fronted
    endpoints (notably Groq) reject the default urllib UA with HTTP
    403 + Cloudflare error 1010 (per I-8 of the stage 7 spec).

    Provider has no models_endpoint? We can't actually test the key
    against /v1/models, so we return (True, 'ok', '', sample_models)
    and let the caller fall back to PRESETS[preset_id]['default_model']
    per Q6 of the locked spec.
    """
    name = provider.canonical_name or 'the provider'

    if not provider.models_endpoint:
        # No /v1/models endpoint to call. Can't validate the key here;
        # caller decides what to do. Return models from sample list so
        # legacy callers get something useful.
        return (True, 'ok', '', list(provider.sample_models))

    if provider.needs_key and not api_key:
        return (False, 'invalid_key',
                f"No API key was provided for {name}.", [])

    url = provider.models_endpoint
    headers = {
        'User-Agent': _USER_AGENT,
        'Accept': 'application/json',
        'Accept-Encoding': 'identity',
    }
    if provider.auth_style == 'bearer' and api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    elif provider.auth_style == 'x-api-key' and api_key:
        param = provider.auth_param_name or 'x-api-key'
        headers[param] = api_key
    elif provider.auth_style == 'query-param' and api_key:
        param = provider.auth_param_name or 'key'
        sep = '&' if '?' in url else '?'
        url = f"{url}{sep}{param}={api_key}"
    # 'none' style: no auth needed (Ollama, LM Studio)

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
        data = json.loads(raw)
        models = _extract_models(data)
        if not models:
            # 200 OK but no recognizable model list — treat as
            # success (validation passed) but caller may want to
            # fall back to sample_models or PRESETS.
            return (True, 'ok', '', list(provider.sample_models))
        return (True, 'ok', '', models)
    except urllib.error.HTTPError as e:
        signup = provider.signup_url or 'the provider site'
        if e.code == 401:
            return (False, 'invalid_key',
                    f"That key was rejected by {name}. "
                    f"Double-check the key, or get a new one at "
                    f"{signup}.", [])
        if e.code == 403:
            return (False, 'forbidden',
                    f"{name} accepted the key but refused the request "
                    f"(403). The key may not have the right scope. "
                    f"Check the key's permissions in your {name} "
                    f"dashboard.", [])
        if 500 <= e.code < 600:
            return (False, 'server_error',
                    f"{name}'s server returned {e.code}. The key "
                    f"looks valid; this isn't your fault. Try again "
                    f"in a few minutes.", [])
        # v4.14.5.66-provider-error-coaching: 404 — endpoint or model
        # not found. Break it out as its own category so the save
        # dialog can route to the matching Teacher playbook entry
        # (provider_endpoint_not_found) instead of the generic
        # "unknown" backstop. Common cause: a typo in a custom
        # endpoint URL, or a model name the provider has retired.
        if e.code == 404:
            return (False, 'not_found',
                    f"{name} couldn't find the endpoint or model "
                    f"requested. Check the model name in the "
                    f"provider's dashboard, or — for a custom "
                    f"provider — that the endpoint URL is correct.",
                    [])
        # v4.14.5.65-provider-save-fix: HTTP 405 Method Not Allowed
        # means the URL exists but doesn't accept GET. This happens
        # on providers whose /v1/models endpoint is POST-only or
        # whose REST API just doesn't expose a GETtable model list
        # at all (Cloudflare Workers AI is the motivating case).
        # The chat endpoint is still reachable — the actual scan POST
        # will validate the key on first use. Returning a false
        # rejection here would block every correctly-configured user.
        # Pass with sample_models so the dropdown still has options.
        if e.code == 405:
            return (True, 'ok',
                    f"{name} reachable; model list unavailable via "
                    f"GET (will validate on first use).",
                    list(provider.sample_models))
        return (False, 'unknown',
                f"{name} returned HTTP {e.code}: {e.reason}. "
                f"Check the endpoint URL is correct for this "
                f"provider.", [])
    except urllib.error.URLError as e:
        reason = str(getattr(e, 'reason', e)).lower()
        if 'timed out' in reason or 'timeout' in reason:
            return (False, 'timeout',
                    f"{name} took too long to respond. Try again. "
                    f"If it keeps timing out, the provider may be "
                    f"overloaded.", [])
        return (False, 'network',
                f"Couldn't reach {name}'s servers right now. Check "
                f"your internet connection and try again. If your "
                f"connection is fine, the provider may be down — "
                f"try again in a few minutes.", [])
    except TimeoutError:
        return (False, 'timeout',
                f"{name} took too long to respond. Try again. "
                f"If it keeps timing out, the provider may be "
                f"overloaded.", [])
    except Exception as e:
        return (False, 'unknown',
                f"Couldn't validate the {name} key: "
                f"{type(e).__name__}: {str(e)[:120]}", [])


# ─── Discovery debug log (data/discovery.log.jsonl) ────────────────────

# Valid event types for log_discovery_event. Centralizing avoids typos
# in callers and makes future filter/triage easier when the assistant
# subscribes to these events.
DISCOVERY_EVENT_TYPES = (
    'unmapped_model',
    'validation_failure',
    'discovery_error',
)


# ─── Data-provider canary endpoints for validate-on-save ───────────────
#
# Data providers don't expose a /v1/models endpoint we can probe for
# key validity. Instead, each known data provider has a tiny canary
# call — a known-cheap endpoint that returns 200 if the key is valid
# and 401/403 if not. The canary is keyed by the provider's profile
# id (matching tm_data_providers.default_profiles()).
#
# Providers not in this dict skip validation: validate_data_key
# returns (True, 'no_canary', '') and the caller logs a
# discovery_error event so we know to add coverage later. Per Q2
# of the locked stage 7 spec, missing canary does NOT block save —
# the user can still save a key, it just won't be validated.

_DATA_CANARY_ENDPOINTS: dict = {
    # Each entry: profile_id -> (url_template, auth_style, auth_param)
    # url_template uses {key} placeholder.
    'finnhub': (
        'https://finnhub.io/api/v1/quote?symbol=AAPL&token={key}',
        'in_url', None),
    'marketaux': (
        'https://api.marketaux.com/v1/news/all'
        '?api_token={key}&symbols=AAPL&limit=1',
        'in_url', None),
    'newsapi': (
        'https://newsapi.org/v2/everything'
        '?q=AAPL&pageSize=1&apiKey={key}',
        'in_url', None),
    'twelve_data': (
        'https://api.twelvedata.com/quote?symbol=AAPL&apikey={key}',
        'in_url', None),
    'massive': (
        'https://api.polygon.io/v3/reference/tickers/AAPL'
        '?apiKey={key}',
        'in_url', None),
}


def validate_data_key(profile_id: str, api_key: str,
                       display_name: str = '',
                       timeout: float = 8.0
                       ) -> tuple[bool, str, str]:
    """v4.14.0 stage 7: canary-check a data provider's API key.

    Returns (ok, category, plain_english):
      - ok: True if validation passed OR no canary is defined for
            this provider (skip case — see note below).
      - category: 'ok' / 'invalid_key' / 'forbidden' / 'network' /
                  'timeout' / 'server_error' / 'unknown' / 'no_canary'
      - plain_english: ready-to-display message including a recovery
                       hint. Empty when ok=True.

    'no_canary' case: the provider isn't in _DATA_CANARY_ENDPOINTS.
    Per Q2 of the stage 7 spec, this should NOT block the user from
    saving — we return ok=True so the save proceeds. The caller logs
    a discovery_error event so future-the user can add coverage.

    Sends 'TiredMarket/4.13.42 (Python)' User-Agent (per I-8).
    """
    name = display_name or profile_id

    if not api_key:
        return (False, 'invalid_key',
                f"No API key was provided for {name}.")

    canary = _DATA_CANARY_ENDPOINTS.get(profile_id)
    if canary is None:
        # Unknown provider — skip validation per spec.
        return (True, 'no_canary', '')

    url_template, auth_style, _auth_param = canary
    url = url_template.format(key=api_key)
    headers = {
        'User-Agent': _USER_AGENT,
        'Accept': 'application/json',
        'Accept-Encoding': 'identity',
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # 200-range and the body parses as JSON-ish — we don't
            # actually inspect the content, just that the call
            # succeeded. Some providers return 200 with a JSON
            # error body for invalid keys; if that's a real problem
            # in practice, add per-provider body-checking later.
            _ = resp.read(1024)  # discard, we just need the status
        return (True, 'ok', '')
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return (False, 'invalid_key',
                    f"That key was rejected by {name}. "
                    f"Double-check the key, or get a new one from "
                    f"the provider's dashboard.")
        if e.code == 403:
            return (False, 'forbidden',
                    f"{name} accepted the key but refused the "
                    f"request (403). The key may not have access "
                    f"to this endpoint, or your free tier may not "
                    f"include this data type.")
        if 500 <= e.code < 600:
            return (False, 'server_error',
                    f"{name}'s server returned {e.code}. The key "
                    f"looks valid; this isn't your fault. Try "
                    f"again in a few minutes.")
        return (False, 'unknown',
                f"{name} returned HTTP {e.code}: {e.reason}.")
    except urllib.error.URLError as e:
        reason = str(getattr(e, 'reason', e)).lower()
        if 'timed out' in reason or 'timeout' in reason:
            return (False, 'timeout',
                    f"{name} took too long to respond. Try again. "
                    f"If it keeps timing out, the provider may be "
                    f"overloaded.")
        return (False, 'network',
                f"Couldn't reach {name}'s servers right now. "
                f"Check your internet connection and try again.")
    except TimeoutError:
        return (False, 'timeout',
                f"{name} took too long to respond. Try again.")
    except Exception as e:
        return (False, 'unknown',
                f"Couldn't validate the {name} key: "
                f"{type(e).__name__}: {str(e)[:120]}")


def log_discovery_event(event_type: str,
                         provider_id: str,
                         provider_string: str = '',
                         reason: str = '',
                         data_dir=None) -> None:
    """Append one JSON line to data/discovery.log.jsonl.

    Schema per line:
      {ts, event_type, provider_id, provider_string, reason}

    Used by stage 7's discovery + validation paths to record:
      - unmapped_model:     a provider model string didn't match any
                            MODEL_PATTERN, OR matched a canonical_id
                            that's unknown to the registry (Q4 + I-2)
      - validation_failure: validate_key() returned ok=False during
                            startup-catchup or weekly refresh
      - discovery_error:    unexpected exception inside the discovery
                            machinery itself

    Per Q4 of the stage 7 spec: this file is the debug surface, NOT
    the activity log. Activity log stays clean of unmapped-string
    noise; the assistant (when it lands) can subscribe to this file
    for richer diagnostics.

    Failure-tolerant: any error writing the log is swallowed. The
    discovery loop must not crash because the log file is read-only,
    full, etc.
    """
    if event_type not in DISCOVERY_EVENT_TYPES:
        # Unknown event type — still log it but tag for triage.
        event_type = f'unknown:{event_type}'
    try:
        resolved = _resolve_data_dir(data_dir)
        if resolved is None:
            return
        resolved.mkdir(parents=True, exist_ok=True)
        log_path = resolved / 'discovery.log.jsonl'
        entry = {
            'ts': datetime.now().isoformat(),
            'event_type': event_type,
            'provider_id': provider_id or '',
            'provider_string': provider_string or '',
            'reason': reason or '',
        }
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════════
# v4.14.5.62-model-routing Part 3 — self-maintaining rotation lists
# ════════════════════════════════════════════════════════════════════════
# The rotation models[] lists go stale (a provider deprecates/renames a model
# — e.g. Groq's mixtral-8x7b-32768, or a never-real glm-4.7-flash). These
# prune a provider's rotation list against the LIVE model IDs from
# discover_models. STARTUP probes + prunes; ON-ERROR prunes a single model on
# a 404. SAFETY: a failed/empty probe NEVER prunes. Writes data/api_providers
# .json (the registry reloads it).


def reconcile_rotation_models(rotation_models, live_model_ids):
    """PURE: keep rotation models that match a LIVE id (case-insensitive exact
    OR substring either way), prune the rest. Empty/None live -> keep all (no
    prune on a failed probe). Returns {'kept': [...], 'pruned': [...]}."""
    try:
        rot = [str(m).strip() for m in (rotation_models or [])
               if str(m).strip()]
        live = [str(m).strip().lower() for m in (live_model_ids or [])
                if str(m).strip()]
        if not live:
            return {'kept': rot, 'pruned': []}
        live_set = set(live)

        def _alive(m):
            ml = m.lower()
            if ml in live_set:
                return True
            return any(ml in lv or lv in ml for lv in live_set)

        return {'kept': [m for m in rot if _alive(m)],
                'pruned': [m for m in rot if not _alive(m)]}
    except Exception:
        return {'kept': list(rotation_models or []), 'pruned': []}


def _api_providers_path(data_dir=None):
    from pathlib import Path as _P
    if data_dir is not None:
        return _P(data_dir) / 'api_providers.json'
    for c in (__import__('tm_paths').get_data_dir(), _P('data')):
        if (c / 'api_providers.json').exists():
            return c / 'api_providers.json'
    return _P('data') / 'api_providers.json'


def _load_providers_file(data_dir=None):
    p = _api_providers_path(data_dir)
    try:
        raw = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return (None, None, p)
    provs = (raw if isinstance(raw, list)
             else (raw.get('providers') if isinstance(raw, dict) else None))
    return (raw, provs, p)


def _save_providers_file(raw, path):
    try:
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False),
                       encoding='utf-8')
        tmp.replace(path)
        return True
    except Exception:
        return False


def prune_model_from_config(provider_id, model_str, data_dir=None,
                            log_fn=None):
    """ON-ERROR: remove model_str from the matching provider's rotation
    models[] in api_providers.json. Returns True iff removed + persisted."""
    try:
        raw, provs, path = _load_providers_file(data_dir)
        if not provs:
            return False
        pid = str(provider_id or '').strip().lower()
        target = str(model_str or '').strip().lower()
        if not pid or not target:
            return False
        changed = False
        for p in provs:
            if not isinstance(p, dict):
                continue
            if (str(p.get('id') or '').strip().lower() != pid
                    and str(p.get('name') or '').strip().lower() != pid):
                continue
            ms = p.get('models')
            if not isinstance(ms, (list, tuple)):
                continue
            kept = [m for m in ms if str(m).strip().lower() != target]
            if len(kept) != len(ms):
                p['models'] = kept
                changed = True
                if log_fn:
                    try:
                        log_fn(f"[model-scan] pruned {model_str} from "
                               f"{p.get('name') or pid} "
                               f"(404 / repeated failure)", 'amber')
                    except Exception:
                        pass
        return _save_providers_file(raw, path) if changed else False
    except Exception:
        return False


def validate_and_prune_models(discover_fn=None, data_dir=None, log_fn=None):
    """STARTUP: for each enabled provider with a multi-entry models[], probe
    its live model list and prune dead entries. discover_fn(provider_cfg) ->
    list[str] | None is injectable for tests; default uses discover_models via
    lookup(). None/empty probe -> skip (no prune). Returns count pruned."""
    try:
        raw, provs, path = _load_providers_file(data_dir)
        if not provs:
            return 0

        def _default_discover(pcfg):
            try:
                info = lookup(str(pcfg.get('preset') or pcfg.get('name') or ''))
                key = (pcfg.get('api_key') or pcfg.get('key') or '')
                if info is None or not key:
                    return None
                return discover_models(info, key)
            except Exception:
                return None

        df = discover_fn or _default_discover
        total = 0
        changed = False
        for p in provs:
            if not isinstance(p, dict) or not p.get('enabled'):
                continue
            ms = p.get('models')
            if not isinstance(ms, (list, tuple)) or len(ms) <= 1:
                continue
            try:
                live = df(p)
            except Exception:
                live = None
            if not live:
                continue
            res = reconcile_rotation_models(ms, live)
            if res['pruned']:
                p['models'] = res['kept'] or list(ms)  # never strand to empty
                changed = True
                total += len(res['pruned'])
                if log_fn:
                    for m in res['pruned']:
                        try:
                            log_fn(f"[model-scan] pruned {m} from "
                                   f"{p.get('name') or p.get('id')} "
                                   f"(not in live model list)", 'amber')
                        except Exception:
                            pass
        if changed:
            _save_providers_file(raw, path)
        return total
    except Exception:
        return 0
