"""
tm_ai_router.py — Smart AI Provider Router (v4.13.56)

What this is:
    The brain that decides "for THIS kind of call, which AI providers
    should we use?" Different call types have different needs:

    - holdings_consensus  : 1-3 holdings, infrequent, quality matters.
                            Fan out wide. Sambanova OK here.
    - lookup_fanout       : User clicked "Run full consensus" on Look Up.
                            Mid-frequency. Most providers eligible.
                            Sambanova LIMITED (1-2 calls max).
    - recommend_run       : User running consensus on a recommendation.
                            Like lookup but treated separately so caps
                            can be tuned. Sambanova LIMITED.
    - scan                : Auto-scan path. HIGH frequency. Burns quota
                            fast. Only HIGH-quota providers eligible.
                            Sambanova FORBIDDEN. GitHub Models LIMITED.

What it DOESN'T do:
    - It does NOT make API calls itself. It only filters provider lists.
    - It does NOT replace tm_provider_health.py — uses it for observed
      state (cooldowns, daily counters). This module is policy; that
      module is state.

How callers use it:
    eligible = select_providers(
        providers=raw_provider_list,
        call_type='scan',
        log_fn=self._log,
    )
    for prov in eligible:
        # call this provider safely
        ...

    After each call:
        record_call_outcome(prov_id, success=True/False,
                            is_rate_limit=True/False)

This replaces both:
    - tm_consensus.ConsensusRunner._resolve_provider_cap (returned cap;
      now we return the filtered list directly)
    - The naive loop in tm_api_providers.run_apis_for_scan_prediction
      that called every enabled provider with no protection

Backward-compat: if tm_provider_health is unavailable, this module
becomes a passthrough — all enabled providers eligible, no caps.
The app still works; just without smart routing.
"""

from __future__ import annotations

import random
import threading
from typing import Callable, Optional

# Soft import — if health module is missing, we degrade to passthrough
try:
    import tm_provider_health
except ImportError:
    tm_provider_health = None  # type: ignore

# Soft import — for resolving preset defaults
try:
    import tm_api_providers as tm_apis
except ImportError:
    tm_apis = None  # type: ignore

# v4.13.58: Soft import for deprecation map. If absent, no proactive
# deprecation detection — we still catch model-not-found errors
# reactively via the regular failure path.
try:
    import tm_model_deprecations as tm_deprecations
except ImportError:
    tm_deprecations = None  # type: ignore

# v4.14.0: Soft import for the canonical model registry. If absent,
# the model-aware select_provider_groups() degrades gracefully — each
# provider's configured model is treated as its own synthetic canonical
# id (no dedup, but no crash). The legacy select_providers() path
# doesn't depend on the registry at all.
try:
    import tm_model_registry
except ImportError:
    tm_model_registry = None  # type: ignore


# ─── Call type policies ───────────────────────────────────────────────
#
# Each policy defines:
#   max_per_run: max providers to call in ONE invocation. None = no limit.
#   provider_blocklist: provider IDs that should NEVER be called for
#                        this type, regardless of enabled state.
#   provider_blocklist_by_endpoint: same but matched by URL substring,
#                                    so it works on user 'custom' presets
#                                    that don't have proper preset IDs.
#   call_type_cap_factor: multiplier on the daily cap. 1.0 = use full
#                         cap. 0.3 = use only 30% of daily cap for
#                         this call type, leaving the rest for other
#                         call types.
#   priority_boost_for: provider IDs that should rank HIGHER for this
#                        type even if their declared priority is lower.

# Pattern keys to match against endpoint URLs (case-insensitive substring)
SAMBANOVA_PATTERN = 'sambanova'
GITHUB_PATTERN = 'models.inference.ai.azure.com'
CEREBRAS_PATTERN = 'cerebras'
GROQ_PATTERN = 'groq.com'
GEMINI_PATTERN = 'generativelanguage.googleapis.com'
MISTRAL_PATTERN = 'api.mistral.ai'
ANTHROPIC_PATTERN = 'api.anthropic.com'
OPENAI_PATTERN = 'api.openai.com'

VALID_CALL_TYPES = (
    'holdings_consensus',
    'lookup_fanout',
    'recommend_run',
    'scan',
    'teacher_ai',
)


# Policy dictionary. Tuned based on actual provider tier sizes:
# - Sambanova free:   20 calls/day total -> SCARCE
# - GitHub Models:    50 calls/day per model -> LIMITED
# - Mistral free:     ~50/day, monthly 1B tokens -> LIMITED
# - Anthropic paid:   pay-per-call, $$$ -> LIMITED
# - Cerebras free:    ~200-500/day -> ABUNDANT
# - Gemini free:      500-1500/day -> ABUNDANT
# - Groq free:        thousands/day -> ABUNDANT
# - OpenAI paid:      pay-per-call -> LIMITED

POLICIES: dict[str, dict] = {
    'holdings_consensus': {
        'max_per_run': None,  # Use all eligible — quality matters
        'blocked_endpoints': [],
        'cap_factor': 1.0,  # Holdings are highest-value calls; use full cap
        'description': (
            'Holdings consensus — fan out wide. All providers eligible.'
        ),
    },
    'lookup_fanout': {
        'max_per_run': None,  # All eligible providers
        'blocked_endpoints': [],
        # v4.14.5.14-429-parsers-and-lookup-tier3 (Fix C): bumped 0.7→1.0.
        # Look Up's "Run full consensus" is a Tier-3 call — the user is
        # actively waiting on a specific ticker — so it gets the full daily
        # cap, matching recommend_run (bumped 0.5→1.0 earlier today) and
        # holdings_consensus (1.0). Never a fanout limit, only daily-quota
        # headroom.
        'cap_factor': 1.0,
        'description': (
            'Look Up full consensus — Tier-3, user actively waiting; full '
            'daily cap (matches recommend_run / holdings_consensus).'
        ),
    },
    'recommend_run': {
        'max_per_run': None,
        'blocked_endpoints': [],
        # v4.14.5.14-recommend-owned-and-verify-tier3: bumped 0.5→1.0.
        # Recommend's per-pick "Verify" is a Tier-3 call — the user is
        # actively waiting on a specific pick — so it deserves the full
        # daily cap, same as holdings_consensus (1.0). The old 0.5 was a
        # mid-tier reservation that under-prioritised the user-waiting
        # case; it never limited fanout (all eligible providers always
        # fanned out), only the daily-quota headroom.
        'cap_factor': 1.0,
        'description': (
            'Recommend "Verify" — Tier-3, user actively waiting on this '
            'pick; full daily cap (matches holdings_consensus).'
        ),
    },
    'scan': {
        'max_per_run': None,
        # SCAN burns quota fast. Block expensive/scarce providers.
        'blocked_endpoints': [
            SAMBANOVA_PATTERN,    # 20/day total — would die in seconds
            ANTHROPIC_PATTERN,    # paid — money burner
            OPENAI_PATTERN,       # paid — money burner
        ],
        # v4.14.5.14a.4: 0.8 (was 0.3). The 0.3 reserved 70% of every
        # provider's daily budget for Layer 2 consensus — which isn't
        # built until v4.14.5.14b, so it was starving the Layer 1
        # fill that actually populates Recommend today. 0.2 still held
        # back for the ad-hoc consensus calls Tired Market already
        # makes (Look Up / Portfolio / Verify). Revisit + rebalance
        # when Layer 2 ships and starts consuming real consensus
        # budget.
        'cap_factor': 0.8,
        'description': (
            'Scan path — high frequency. Sambanova/Anthropic/OpenAI '
            'forbidden. Other providers limited to 30% of daily cap.'
        ),
    },
    'teacher_ai': {
        # v4.15.0-teacher-brain-v1: the conversational "Ask Tired Market AI"
        # brain. SINGLE interactive call (one successful answer), NOT a
        # consensus fan-out — the caller walks select_teacher_provider_order()
        # and stops on the first success. max_per_run stays None so the
        # FAILOVER list survives (the single-pick discipline lives in the
        # caller, not a [:1] truncation here).
        'max_per_run': None,
        # Fast + free only — block the scarce/paid tiers (same set as scan).
        # A chat turn wants a fast provider, never a money-burner.
        'blocked_endpoints': [
            SAMBANOVA_PATTERN,
            ANTHROPIC_PATTERN,
            OPENAI_PATTERN,
        ],
        # Sparse, user-initiated; modest reservation so a handful of
        # questions/day never compete with the scan budget.
        'cap_factor': 0.5,
        'description': (
            'Teacher AI — single fast interactive call (Groq primary, '
            'Cerebras last). One successful answer w/ failover; NOT '
            'consensus fan-out; writes no prediction/accuracy state.'
        ),
    },
}

# v4.15.0-teacher-brain-v1: fast-first preference for the SINGLE teacher_ai
# call. Groq primary (fast + room for the grounding slices); Cerebras LAST —
# fast but its ~8K free-tier context is too tight once cheat-sheet slices +
# portfolio are fed. Keys are canonical provider ids (preset). Unknown
# providers sort to the end.
TEACHER_PROVIDER_PREFERENCE = (
    'groq', 'google', 'github', 'mistral', 'zhipu', 'cerebras',
)


# v4.14.5.69-tier2-backfill: minimum daily-cap floor for tier-2
# validation (call_type='holdings_consensus'). Providers whose
# resolved daily cap is BELOW this floor are excluded from consensus
# selection AND backfill — they cannot sustain validation load (a
# 15/day provider would die within minutes of being pulled into a
# tier-2 cycle that runs ~20 picks/hour x 3 validators).
#
# At 100 the floor admits everything down to OpenAI/Together (100)
# and excludes SambaNova (15), Cohere (33), GitHub Models (40), and
# free-tier Anthropic (50) for sustained tier-2 use. Adjust here
# (one line) when the realistic free-tier landscape shifts.
#
# The floor is STRICT — a user's explicitly-listed favorite that
# falls below the floor is still excluded. The point of the floor is
# that no preference can make a 15/day provider sustainable; we log
# the exclusion clearly so the user understands why their favorite
# isn't voting.
_CONSENSUS_MIN_DAILY_CAP = 100

# Suppress duplicate floor-exclusion log lines within the same
# process. Set is fine — at most a few entries per session (one per
# provider that's below the floor).
_floor_excluded_logged: set = set()


def _resolve_router_log():
    """Best-effort lookup of the App._log callback for router-level
    telemetry (floor exclusion, backfill substitution). Returns the
    bound method or None. Reuses the same canonical-app registry the
    teacher-intercept layer already maintains so we don't thread an
    app through every helper signature."""
    try:
        import tm_teacher_intercept as _tic
        _app = getattr(_tic, '_registered_app', None)
        if _app is not None:
            log = getattr(_app, '_log', None)
            if callable(log):
                return log
    except Exception:
        pass
    return None


def select_teacher_provider_order(providers: list[dict],
                                   log_fn: Optional[
                                       Callable[[str, str], None]] = None
                                   ) -> list[dict]:
    """v4.14.5.62-model-routing: eligible providers for a SINGLE interactive
    teacher_ai (Look Up / AI-question) call, ranked SMARTEST → DUMBEST by
    per-MODEL capability, filtered to the user's installed + working keys,
    cap-aware. Each returned provider is a COPY with its 'model' pinned to its
    SMARTEST available model, so the caller's failover walk uses the smartest
    model first and steps down to the next-smartest — never a dead call.

    Built from INSTALLED keys (no hardcoded names): a 1-key user gets a
    1-rung ladder; a 7-provider user gets the full ladder. Cap-aware:
    `select_providers` already drops providers at/over their teacher_ai cap
    or in cooldown, so the survivors are the with-headroom set, ranked by
    capability. If ALL are capped (eligible empty) we fall back to the full
    installed list (still capability-ranked) — a degraded answer beats none
    (never dead-call). Lookup-tier models (e.g. gemini-2.5-pro) ARE allowed
    here (teacher_ai is not a scan call_type), so the smartest model is used
    when its provider is healthy. Replaces the old fast-first per-provider
    ordering (TEACHER_PROVIDER_PREFERENCE)."""
    try:
        import tm_model_capability as _cap
    except Exception:
        _cap = None
    _DEF = (_cap.DEFAULT_RANK if _cap is not None else 50)

    def _rank_of(model_str):
        if _cap is None:
            return _DEF
        try:
            return _cap.model_capability_rank(model_str)
        except Exception:
            return _DEF

    def _best_model(p):
        """(smartest_model_str, its_rank) for this provider — the lowest-rank
        model it can actually send (from models[] else the singular model)."""
        try:
            ms = p.get('models')
            cands = ([str(m).strip() for m in ms if str(m).strip()]
                     if isinstance(ms, (list, tuple)) else [])
            if not cands:
                one = str(p.get('model') or '').strip()
                cands = [one] if one else []
            if not cands:
                return (None, _DEF)
            best = min(cands, key=_rank_of)
            return (best, _rank_of(best))
        except Exception:
            return (str(p.get('model') or '').strip() or None, _DEF)

    def _pin(p):
        best, rank = _best_model(p)
        cp = dict(p)
        if best:
            cp['model'] = best   # caller's call_provider uses provider['model']
        return (cp, rank)

    eligible = select_providers(providers, 'teacher_ai', log_fn=log_fn)
    pool = eligible if eligible else list(providers or [])  # never dead-call
    pinned = [_pin(p) for p in pool]
    pinned.sort(key=lambda t: t[1])   # smartest (lowest rank) first
    return [cp for cp, _r in pinned]


# ─── Eligibility check ────────────────────────────────────────────────

def _endpoint_matches(provider: dict, patterns: list[str]) -> bool:
    """True if any pattern is a substring of provider's endpoint URL."""
    if not patterns:
        return False
    ep = (provider.get('endpoint') or '').lower()
    return any(p in ep for p in patterns)


def _resolve_provider_cap(provider: dict,
                           canonical_model: Optional[str] = None
                           ) -> Optional[int]:
    """Return the base daily cap for a provider (before call_type
    cap_factor is applied). Resolution order:
      1. Explicit max_calls_per_day on provider record
      2. Endpoint-URL detection (catches custom-preset providers)
      3. Preset's default_max_per_day
      4. None (no cap)

    Mirrors the v4.13.55c helper but lives in the router so the
    consensus runner doesn't need its own copy.

    v4.14.5.14rot Patch 3b: when use_per_model_cap_tracking is on AND
    a canonical_model is supplied (only the model-aware
    is_eligible_for_model passes it), the LEARNED cap is read
    per-(provider-family, model) so a daily wall hit by one model
    doesn't cap its siblings. Flag off OR no model OR any error →
    the family-wide `_default` slot (byte-identical to Patch 3a, the
    legacy provider-wide value).
    """
    # 0. v4.14.5.14a.4 (B5f): a LEARNED cap (from real provider
    # response headers / daily 429s) overrides every static guess.
    # The hardcoded numbers below are SEED values only — the first
    # guess used before any real provider response is seen. Once the
    # learner has a value, reality wins.
    try:
        import tm_provider_learning as _tpl
        fam = _tpl.provider_family(provider)
        _mdl = None
        if canonical_model:
            try:
                import tm_model_cursor as _mc
                if _mc.per_model_caps_enabled():
                    _mdl = canonical_model
            except Exception:
                _mdl = None
        learned = (_tpl.get_learned_cap(fam, model=_mdl)
                   if _mdl else _tpl.get_learned_cap(fam))
        if learned and learned > 0:
            return int(learned)
    except Exception:
        pass

    # 1. Explicit override
    cap = provider.get('max_calls_per_day')
    if cap is not None:
        try:
            cap = int(cap)
            return cap if cap > 0 else None
        except (TypeError, ValueError):
            pass

    # 2. URL-based detection — runs BEFORE preset default so 'custom'
    # configs pointing at known endpoints get the right cap.
    seed = _url_detected_cap((provider.get('endpoint') or '').lower())
    if seed is not None:
        return seed

    # 3. Preset default
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
                        if cap == 0:
                            return None  # 0 means unlimited (ollama)
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass

    # 4. No cap
    return None


def _apply_observed_cap(base_cap: Optional[int],
                         provider_id: str) -> Optional[int]:
    """If the registry has observed a tighter quota (server 429'd before
    we hit our declared cap), return that observed cap. Otherwise the
    declared cap.

    The observed cap is the trip count from the most recent 429,
    minus a 5% safety margin, persisted per-provider.
    """
    if tm_provider_health is None:
        return base_cap
    try:
        state = tm_provider_health.get_state()
        if state is None:
            return base_cap
        rec = state.get(provider_id)
        if rec is None:
            return base_cap
        # Read observed_max_per_day field if present (added by
        # extended health module; falls back to base_cap if absent)
        observed = getattr(rec, 'observed_max_per_day', None)
        if observed and observed > 0:
            if base_cap is None:
                return observed
            return min(base_cap, observed)
    except Exception:
        pass
    return base_cap


def resolve_provider_model(provider: dict) -> str:
    """The SINGLE chokepoint for "which model string does this
    provider use for THIS call" (every model-read in is_eligible /
    is_eligible_for_model / _resolve_canonical_model /
    select_provider_groups routes here, so canonical-id, eligibility
    gate, and the model actually SENT always agree per call).

    v4.14.5.14rot Patch 2b: cursor-aware. When BOTH rotation flags are
    on (tm_model_cursor.rotation_enabled) AND the provider has a
    >1-entry `models` list, return models[cursor_index] (READ ONLY —
    the cursor ADVANCE happens once per dispatch in
    record_call_outcome_for_model, never here, so every read within a
    single call returns the same model). Otherwise — flag off, single
    model, no list, or ANY error — fall back to the singular
    `provider['model']` (byte-identical to Patch 2a). Never raises;
    rotation is convenience, it must never wedge dispatch."""
    try:
        models = (provider or {}).get('models')
        if isinstance(models, (list, tuple)):
            ms = [str(m).strip() for m in models if str(m).strip()]
            if len(ms) > 1:
                import tm_model_cursor as _mc
                if _mc.rotation_enabled():
                    pid = ((provider or {}).get('id')
                           or (provider or {}).get('name') or '?')
                    idx = _mc.get_next_model_index(pid, len(ms))
                    if 0 <= idx < len(ms):
                        return ms[idx]
    except Exception:
        pass  # fail-open → legacy singular model below
    try:
        return (provider or {}).get('model') or ''
    except Exception:
        return ''


def configured_vote_strings(provider: dict) -> list[str]:
    """v4.14.5.27-registry-resolution: every model string this provider
    could actually SEND on a call — i.e. the full rotation set, not just
    the current cursor pick.

    With model rotation on, `resolve_provider_model` returns one entry of
    the `models` list per call, so over time the provider votes with ALL
    of them; discovery and the drift-guard audit both need the complete
    set so every voted string has a real canonical mapping. Source: the
    provider's `models` list (deduped, order-preserved) or, failing that,
    the singular `model`. Never raises; [] on a malformed record."""
    try:
        out: list[str] = []
        seen: set[str] = set()
        models = (provider or {}).get('models')
        if isinstance(models, (list, tuple)):
            for m in models:
                s = str(m).strip()
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)
        if not out:
            # No usable list → the singular model, read through the SINGLE
            # chokepoint (resolve_provider_model) so the rot-p2a invariant
            # "no bare provider.get('model') outside the helper" holds.
            single = str(resolve_provider_model(provider) or '').strip()
            if single:
                out.append(single)
        return out
    except Exception:
        return []


def is_eligible(provider: dict, call_type: str
                  ) -> tuple[bool, str, Optional[int]]:
    """Check if a provider is eligible for a given call type.

    Returns (eligible, reason_if_not, effective_cap).

    `effective_cap` is the daily cap to enforce for THIS call type
    (after policy cap_factor is applied to the provider's base cap).
    None = no cap to enforce.
    """
    # Mode-independent eligibility checks (provider must be enabled,
    # have endpoint+model)
    if not provider.get('enabled'):
        return (False, 'provider disabled', None)
    if not provider.get('endpoint') or not resolve_provider_model(
            provider):
        return (False, 'missing endpoint or model', None)

    # ── v4.13.58: deprecation check ──────────────────────────────────
    # If the provider's model is in the known-deprecated list AND has
    # a known replacement, skip it with a clear message so the user
    # can update. If deprecated WITHOUT a known replacement, we still
    # try the call (might still work; let real 404 bench it via cooldown).
    if tm_deprecations is not None:
        try:
            preset = provider.get('preset', '')
            model_name = resolve_provider_model(provider)
            depr = tm_deprecations.lookup(preset, model_name)
            if depr is not None and depr.get('replacement'):
                return (False,
                        (f"model '{model_name}' deprecated; "
                          f"recommended: '{depr['replacement']}'"),
                        None)
        except Exception:
            pass  # fail-open: don't block on deprecation-check errors

    policy = POLICIES.get(call_type)
    if policy is None:
        # Unknown call type — fail open, use full cap
        policy = {'max_per_run': None, 'blocked_endpoints': [],
                   'cap_factor': 1.0}

    # Block by endpoint pattern (e.g. Sambanova on scan)
    if _endpoint_matches(provider, policy.get('blocked_endpoints', [])):
        return (False,
                f"blocked for call_type={call_type} (endpoint match)",
                None)

    # Compute effective cap = base_cap * cap_factor, then check observed
    base_cap = _resolve_provider_cap(provider)
    cap_factor = policy.get('cap_factor', 1.0)
    if base_cap is not None and cap_factor < 1.0:
        effective_cap = max(1, int(base_cap * cap_factor))
    else:
        effective_cap = base_cap
    # Observed cap (from 429s) takes precedence if tighter
    prov_id = provider.get('id') or provider.get('name', '?')
    effective_cap = _apply_observed_cap(effective_cap, prov_id)

    # v4.14.5.69-tier2-backfill: consensus min-cap floor. Validation
    # is sustained load (~20 picks/hour x 3 validators); a provider
    # whose STRUCTURAL daily cap is below the floor cannot carry it.
    # We deliberately consult the preset's default_max_per_day (the
    # "what tier is this provider" structural signal) — not the
    # learned cap from _resolve_provider_cap, which can be bumped
    # upward by ad-hoc observation and would let a 15/day preset
    # past the floor on lucky sessions. Explicit user override on
    # the provider record beats the preset (user knows their key
    # actually has a higher cap). Strict — log once per
    # (provider_id, call_type) per process so the user sees WHY
    # their favorite isn't voting.
    if call_type == 'holdings_consensus':
        # Structural cap signal: explicit override → preset default.
        # If we can't determine one, skip the floor (fail-open).
        structural_cap = None
        try:
            _override = provider.get('max_calls_per_day')
            if _override is not None:
                structural_cap = int(_override)
        except Exception:
            structural_cap = None
        if structural_cap is None:
            try:
                preset_def = tm_apis.get_preset(
                    provider.get('preset', '')) or {}
                _pcap = preset_def.get('default_max_per_day')
                if _pcap is not None:
                    structural_cap = int(_pcap)
            except Exception:
                structural_cap = None
        if (structural_cap is not None
                and structural_cap < _CONSENSUS_MIN_DAILY_CAP):
            _label = (provider.get('name')
                       or provider.get('display_name')
                       or prov_id)
            _floor_key = (prov_id, call_type)
            if _floor_key not in _floor_excluded_logged:
                _floor_excluded_logged.add(_floor_key)
                try:
                    _logger = _resolve_router_log()
                except Exception:
                    _logger = None
                if callable(_logger):
                    try:
                        _logger(
                            f"[tier2-eligibility] {_label} excluded "
                            f"from consensus (cap "
                            f"{structural_cap}/day < "
                            f"floor {_CONSENSUS_MIN_DAILY_CAP})",
                            'muted')
                    except Exception:
                        pass
            return (False,
                    f"below consensus min-cap floor "
                    f"({structural_cap}/day < "
                    f"{_CONSENSUS_MIN_DAILY_CAP})",
                    effective_cap)

    # Health check — cooldown + daily-count-vs-cap
    if tm_provider_health is not None:
        try:
            state = tm_provider_health.get_state()
            if state is not None:
                safe, reason = state.is_safe_to_call(
                    prov_id, max_per_day=effective_cap)
                if not safe:
                    return (False, reason, effective_cap)
        except Exception:
            pass

    return (True, '', effective_cap)


# ─── Public selection API ─────────────────────────────────────────────

def select_providers(providers: list[dict], call_type: str,
                       log_fn: Optional[Callable[[str, str], None]] = None
                       ) -> list[dict]:
    """Pick which providers to actually call for this call_type.

    Args:
        providers: full enabled-provider list from registry
        call_type: one of VALID_CALL_TYPES
        log_fn: optional (msg, color) for activity logging of skips

    Returns:
        List of provider dicts that should be called, in priority order.
        Skipped providers are logged but NOT in the return value.
    """
    if call_type not in VALID_CALL_TYPES:
        # Unknown call type — log and fall through (passthrough behavior)
        if log_fn:
            try:
                log_fn(
                    f"AI router: unknown call_type '{call_type}', "
                    f"using all enabled providers", 'amber')
            except Exception:
                pass
        return [p for p in providers if p.get('enabled')]

    # tm_provider_health required for caps and cooldowns; without it,
    # we degrade to "all enabled" but still respect the blocked_endpoints
    # for the call_type.
    eligible: list[dict] = []
    skipped: list[tuple[str, str]] = []  # (label, reason)

    policy = POLICIES.get(call_type, {})
    blocked = policy.get('blocked_endpoints', [])

    for prov in providers:
        if not prov.get('enabled'):
            continue
        label = (
            prov.get('name')
            or prov.get('display_name')
            or prov.get('id')
            or '?')

        # Hard policy block (works without health module)
        if _endpoint_matches(prov, blocked):
            skipped.append(
                (label,
                 f"forbidden for {call_type} (high-cost or scarce tier)"))
            continue

        # Health + cap check (degrades gracefully if module absent)
        ok, reason, _cap = is_eligible(prov, call_type)
        if not ok:
            skipped.append((label, reason))
            continue
        eligible.append(prov)

    # Apply max_per_run cap if any (currently None for all types, but
    # the hook is here for future use).
    max_per_run = policy.get('max_per_run')
    if max_per_run and len(eligible) > max_per_run:
        eligible = eligible[:max_per_run]

    # Log routing decisions. v4.14.0 stage 7.1: suppress duplicate
    # skip lines within one scan run via _active_skip_dedup. The
    # "routing to N provider(s)" summary line is still per-call
    # because it carries useful per-candidate info.
    if log_fn:
        try:
            if eligible:
                names = [(p.get('name') or '?') for p in eligible]
                log_fn(
                    f"AI router [{call_type}]: routing to "
                    f"{len(eligible)} provider(s): {', '.join(names)}",
                    'muted')
            dedup = _active_skip_dedup
            for label, reason in skipped:
                if dedup is not None:
                    cat = _classify_skip_reason(reason)
                    if not dedup.should_log(label, cat):
                        continue
                log_fn(
                    f"AI router [{call_type}]: skipped {label} — {reason}",
                    'amber')
        except Exception:
            pass

    return eligible


def _url_detected_cap(ep: str) -> Optional[int]:
    """v4.14.5.14a.4: SEED daily-request cap by endpoint URL. These are
    starting guesses only — the dynamic learner (tm_provider_learning)
    overrides them from real provider response headers / daily 429s.
    Numbers researched 2026-05-17 against official provider docs.
    Also the reference ceiling for the per-minute-429 soft floor, so
    it works for `preset:"custom"` providers (Cerebras/GitHub) whose
    preset has no documented default."""
    try:
        ep = (ep or '').lower()
        if SAMBANOVA_PATTERN in ep:
            return 15      # server is 20/day, margin
        if GITHUB_PATTERN in ep:
            return 40      # source: GitHub Models free = 50 RPD
        if CEREBRAS_PATTERN in ep:
            return 1500    # source: Cerebras = 1M tok/day, 30 RPM,
            #                no fixed RPD (~500 calls/day at 2K/call)
        if GROQ_PATTERN in ep:
            return 14000   # source: Groq Llama-3.1-8B = 14,400 RPD
        if GEMINI_PATTERN in ep:
            return 1500    # source: Gemini Flash-Lite = 1500 RPD
        if MISTRAL_PATTERN in ep:
            return 15000   # source: Mistral = no RPD cap; 1B tok/mo,
            #                2 RPM (~16K calls/day token budget)
        if ANTHROPIC_PATTERN in ep:
            return 50      # money-burn protection (paid, intentional)
        if OPENAI_PATTERN in ep:
            return 100     # money-burn protection (paid, intentional)
    except Exception:
        pass
    return None


def seed_cap_for_family(family: str) -> Optional[int]:
    """v4.14.5.14-classify429-fix: the URL-detected SEED daily cap for
    a tm_provider_learning family token ('groq'/'cerebras'/'github'/
    'gemini'/'mistral'/'sambanova'/'anthropic'/'openai'). Single
    source of the numbers — delegates to _url_detected_cap via a
    representative endpoint per family, so the seed values are NEVER
    duplicated. Returns None for unknown families (e.g. zhipu) so the
    sanity wipe skips what it can't validate. Never raises."""
    try:
        ep = {
            'groq': GROQ_PATTERN,
            'cerebras': CEREBRAS_PATTERN,
            'github': GITHUB_PATTERN,
            'gemini': GEMINI_PATTERN,
            'mistral': MISTRAL_PATTERN,
            'sambanova': SAMBANOVA_PATTERN,
            'anthropic': ANTHROPIC_PATTERN,
            'openai': OPENAI_PATTERN,
        }.get((family or '').strip().lower())
        if not ep:
            return None
        return _url_detected_cap(ep)
    except Exception:
        return None


def _floor_reference_cap(provider_id: str) -> Optional[int]:
    """v4.14.5.14a.4: the cap used as the 50%-soft-floor reference by
    the per-minute-429 guard. Uses the URL-detected SEED (works for
    preset:'custom' providers, unlike _documented_default_rpd which
    reads the preset default and returns None for custom Cerebras/
    GitHub — the exact gap that let one 30-RPM 429 false-learn
    Cerebras down to ~48/day)."""
    if tm_apis is None:
        return None
    try:
        for prov in tm_apis.load_enabled_providers():
            if prov.get('id') == provider_id:
                u = _url_detected_cap(
                    (prov.get('endpoint') or '').lower())
                if u:
                    return u
                d = _documented_default_rpd(provider_id)
                return d
    except Exception:
        pass
    return None


def _provider_endpoint_dict(provider_id: str) -> Optional[dict]:
    """Resolve a provider dict by id (best-effort) for family/header
    learning lookups."""
    if tm_apis is None:
        return None
    try:
        for prov in tm_apis.load_enabled_providers():
            if prov.get('id') == provider_id:
                return prov
    except Exception:
        pass
    return None


_note_success_exc_log_state: dict = {}
_NOTE_SUCCESS_RATE_LIMIT_WINDOW_S = 60.0


def _log_note_success_exception(provider_id, canonical_model,
                                  call_site, exc):
    """v4.14.5.14-soft-gate-and-cap-hygiene Part C (2026-05-20):
    instrumentation hook for the silent exception path that
    `_note_success_learning` previously swallowed. The 2026-05-20
    cap investigation found the per-(family, model) call counters
    are undercounting actual call volume by 50-94% — Cerebras
    logged ~585 calls but only 38 tracked, Mistral ~82 vs 27, etc.
    Root cause is exceptions being raised inside `_note_success_
    learning` and silently dropped by the wrapping try/except.
    Part C does NOT fix the underlying cause; it just makes the
    failures visible so a follow-up patch knows WHICH call paths
    are losing writes. Writes to `data/provider_learning.log` (the
    learning module's own audit trail — same destination as
    `_log_change` / `_log_ambiguous_429`, deliberately decoupled
    from the main activity.log per the iso/classify_429-fix
    "module-internal audit trail" convention).

    Rate-limited: one log entry per (provider, model, exception
    type) per 60-second window — a per-key tuple counter
    accumulates suppressed instances and the next emission for
    that key includes the suppressed count so we see "N times in
    the last 60s" rather than spam.
    """
    try:
        import time as _time
        key = (str(provider_id), str(canonical_model or '-'),
               type(exc).__name__)
        now = _time.time()
        state = _note_success_exc_log_state.get(key)
        if state is not None:
            last_t, suppressed = state
            if (now - last_t) < _NOTE_SUCCESS_RATE_LIMIT_WINDOW_S:
                _note_success_exc_log_state[key] = (
                    last_t, suppressed + 1)
                return
            # Window elapsed — emit, include suppressed count.
            suppressed_str = (f" ({suppressed} suppressed in last "
                              f"{int(_NOTE_SUCCESS_RATE_LIMIT_WINDOW_S)}s)"
                              if suppressed else "")
        else:
            suppressed_str = ""
        _note_success_exc_log_state[key] = (now, 0)
        try:
            from datetime import datetime as _dt_pc
            ts = _dt_pc.now().strftime('%Y-%m-%d %H:%M:%S')
            line = (f"{ts} | [provider-learning] "
                    f"_note_success_learning failed for "
                    f"{provider_id}/{canonical_model or '-'} "
                    f"({call_site}): {type(exc).__name__}: {exc}"
                    f"{suppressed_str}\n")
            import tm_provider_learning as _tpl
            _log_path = getattr(_tpl, '_LOG', None)
            if _log_path is not None:
                with _log_path.open('a', encoding='utf-8') as f:
                    f.write(line)
        except Exception:
            pass
    except Exception:
        # Instrumentation must never crash the call path.
        pass


def _note_success_learning(provider_id: str,
                            canonical_model: Optional[str] = None
                            ) -> None:
    """v4.14.5.14a.4 (B5a/B5b): feed a successful call's captured
    rate-limit headers to the dynamic learner. Side-effect only,
    never raises, no-op when the provider sends no signal.

    v4.14.5.14rot Patch 3b: when use_per_model_cap_tracking is on AND
    a canonical_model is supplied (only the model-aware
    record_call_outcome_for_model passes it; the legacy
    record_call_outcome does not), the header-derived cap is learned
    per-(family, model) — the headers arrived on THIS model's
    response. Flag off / no model / any error → the family-wide
    _default slot (byte-identical to Patch 3a).

    v4.14.5.14-soft-gate-and-cap-hygiene Part C (2026-05-20):
    instrumentation. The pre-fix `except Exception: pass` was
    silently swallowing exceptions that the 2026-05-20 cap
    investigation showed are responsible for a 50-94%
    undercount of `today_calls`. The new except branch emits a
    rate-limited amber line via `_log_note_success_exception`
    (writes to `data/provider_learning.log`) so we can see
    which call paths are losing writes. Pure visibility — no
    behaviour change; instrumentation itself is wrapped in a
    paranoid try/except so a logging-side fault can never crash
    the call path. `call_site='model_aware'` if a model was
    threaded through; `call_site='legacy'` if not.
    """
    _call_site = ('model_aware' if canonical_model else 'legacy')
    try:
        import tm_api_providers as _tap
        import tm_provider_learning as _tpl
        prov = _provider_endpoint_dict(provider_id)
        if not prov:
            return
        fam = _tpl.provider_family(prov)
        meta = _tap.get_last_http_meta()
        # v4.14.5.14a.5: pass the URL seed so the learner can reject a
        # bogus over-large day-cap header (GitHub-via-Azure 60000).
        seed = _url_detected_cap((prov.get('endpoint') or '').lower())
        _mdl = None
        if canonical_model:
            try:
                import tm_model_cursor as _mc
                if _mc.per_model_caps_enabled():
                    _mdl = canonical_model
            except Exception:
                _mdl = None
        if _mdl:
            _tpl.note_success_headers(fam, meta, seed_cap=seed,
                                      model=_mdl)
        else:
            _tpl.note_success_headers(fam, meta, seed_cap=seed)
    except Exception as _exc:
        _log_note_success_exception(
            provider_id, canonical_model, _call_site, _exc)


_CONSECUTIVE_429_THRESHOLD = 3


def _consecutive_429_allows_tighten(rec, fam, canonical_model, _tpl) -> bool:
    """v4.14.5.14-classify429-part-c (IDEAS Fix 2): gate cap-tightening on
    consecutive_429s. Returns True iff a 429 may tighten the cap NOW —
    i.e. the gate is disabled (use_consecutive_429_gate=False) OR this is
    at least the _CONSECUTIVE_429_THRESHOLD-th consecutive 429 on this
    (provider, model). `record_rate_limit` already incremented
    consecutive_429s (and applied the cooldown) before this is called, so
    a single transient 429 only cools down; it does not tighten the cap
    until 3 in a row. The counter resets on success (record_success) or a
    non-429 error (record_failure), so genuine daily exhaustion — which
    produces consecutive 429s — still tightens on the 3rd. Logs the
    decision to provider_learning.log. Fail-OPEN: any error / flag-off →
    True (legacy on-the-spot tighten, exact pre-Part-C behaviour)."""
    try:
        import tm_model_cursor as _mc
        if not _mc.consecutive_429_gate_enabled():
            return True
    except Exception:
        return True
    try:
        n = int(getattr(rec, 'consecutive_429s', 0) or 0)
    except Exception:
        return True
    allowed = n >= _CONSECUTIVE_429_THRESHOLD
    if _tpl is not None:
        try:
            _tpl._log_consecutive_429_gate(
                fam, canonical_model, n, _CONSECUTIVE_429_THRESHOLD,
                tightened=allowed)
        except Exception:
            pass
    return allowed


def _classify_and_record_quota(state, provider_id, canonical_model,
                                 error_msg) -> None:
    """v4.14.5.14a.4: the corrected 429 path. Classify per-minute vs
    daily (Retry-After / X-RateLimit-* headers captured at the HTTP
    chokepoint + body keywords). per-minute → short Retry-After
    cooldown, do NOT tighten the learned daily cap. daily → escalating
    cooldown + tighten + feed the learner. unknown → legacy
    conservative path (default cooldown + soft-floor-guarded tighten,
    the guard now using the URL seed so custom presets are
    protected)."""
    try:
        import tm_api_providers as _tap
        import tm_provider_learning as _tpl
    except Exception:
        _tap = None
        _tpl = None

    meta = {}
    secs_since = None
    try:
        if _tap is not None:
            meta = _tap.get_last_http_meta() or {}
    except Exception:
        meta = {}
    prov = _provider_endpoint_dict(provider_id)
    fam = ''
    try:
        if _tpl is not None and prov:
            fam = _tpl.provider_family(prov)
    except Exception:
        fam = ''

    cls = {'type': 'unknown', 'retry_after_seconds': None}
    try:
        if _tpl is not None:
            cls = _tpl.classify_429(fam, meta, error_msg or '',
                                    secs_since)
    except Exception:
        cls = {'type': 'unknown', 'retry_after_seconds': None}

    ctype = cls.get('type', 'unknown')
    ra = cls.get('retry_after_seconds')

    # v4.14.5.14-classify429-fix: classify_429 v2 flags an ambiguous
    # 429 (no daily/minute headers) it defaulted to per-minute. Emit
    # the one-shot-per-provider diagnostic here (the consumer), so
    # classify_429 stays a pure no-side-effect function.
    if cls.get('ambiguous') and _tpl is not None:
        try:
            _tpl._log_ambiguous_429(fam)
        except Exception:
            pass

    if ctype == 'per_minute':
        # Short cooldown = the Retry-After (seconds), NOT 5 minutes.
        # Do NOT tighten the learned daily cap — this was a burst
        # speed-limit, not a daily wall. THIS is the fix for the
        # Groq/Cerebras "one 429 → 5-min cooldown all day" bottleneck.
        # v4.14.5.71-per-minute-cooldown-cap: also pass the type so
        # the backstop in tm_provider_health refuses to escalate this
        # to LONG_COOLDOWN_SEC even on the 3rd+ consecutive strike.
        cd = int(ra) if (ra and ra > 0) else 30
        try:
            state.record_rate_limit(
                provider_id, canonical_model=canonical_model,
                cooldown_sec=cd, cooldown_type='per_minute')
        except TypeError:
            # Older signature without cooldown_type — still pass
            # cooldown_sec so we at least get the short window.
            try:
                state.record_rate_limit(
                    provider_id, canonical_model=canonical_model,
                    cooldown_sec=cd)
            except TypeError:
                state.record_rate_limit(
                    provider_id, canonical_model=canonical_model)
        return

    if ctype == 'daily':
        try:
            state.record_rate_limit(
                provider_id, canonical_model=canonical_model)
        except TypeError:
            state.record_rate_limit(provider_id,
                                    canonical_model=canonical_model)
        try:
            rec = state.get(provider_id,
                            canonical_model=canonical_model)
            # v4.14.5.14-classify429-part-c: a single daily-classified 429
            # no longer tightens the cap. Gate BOTH the in-memory
            # observed_max_per_day AND the persisted note_daily_429 learned
            # cap behind consecutive_429s >= 3 (record_rate_limit above
            # already cooled down + incremented the counter). Fail-open.
            if (rec is not None
                    and _consecutive_429_allows_tighten(
                        rec, fam, canonical_model, _tpl)):
                trip = max(1, rec.calls_today - 1)
                safe = max(1, int(trip * 0.95))
                cur = getattr(rec, 'observed_max_per_day', None) or 0
                if cur == 0 or safe < cur:
                    rec.observed_max_per_day = safe
                    state.save()
                if _tpl is not None and fam:
                    # v4.14.5.14rot Patch 3b: a DAILY-classified 429
                    # tightens the learned cap for THIS model only
                    # when use_per_model_cap_tracking is on; else the
                    # family-wide _default (byte-identical to 3a).
                    # per-minute 429s never reach here (handled above,
                    # already per-model in tm_provider_health) — so
                    # this keys exactly the daily-budget axis.
                    _mdl = None
                    if canonical_model:
                        try:
                            import tm_model_cursor as _mc
                            if _mc.per_model_caps_enabled():
                                _mdl = canonical_model
                        except Exception:
                            _mdl = None
                    if _mdl:
                        _tpl.note_daily_429(fam, trip, model=_mdl)
                    else:
                        _tpl.note_daily_429(fam, trip)
        except Exception:
            pass
        return

    # unknown → legacy conservative path, with the FIXED soft floor.
    try:
        state.record_rate_limit(provider_id,
                                canonical_model=canonical_model)
    except TypeError:
        state.record_rate_limit(provider_id,
                                canonical_model=canonical_model)
    try:
        rec = state.get(provider_id, canonical_model=canonical_model)
        # v4.14.5.14-classify429-part-c: same consecutive-429 gate on the
        # legacy/unknown tightening path (it also tightens on a single
        # 429). The soft floor below stays as an ADDITIONAL guard — the
        # gate doesn't replace it. Fail-open.
        if (rec is not None
                and _consecutive_429_allows_tighten(
                    rec, fam, canonical_model, _tpl)):
            trip = max(1, rec.calls_today - 1)
            safe = max(1, int(trip * 0.95))
            documented = _floor_reference_cap(provider_id)
            if (documented is not None
                    and trip < int(documented * 0.5)):
                pass  # below soft floor — almost certainly per-minute
            else:
                cur = getattr(rec, 'observed_max_per_day', None) or 0
                if cur == 0 or safe < cur:
                    rec.observed_max_per_day = safe
                    state.save()
    except Exception:
        pass


def _documented_default_rpd(provider_id: str,
                              canonical_model: Optional[str] = None
                              ) -> Optional[int]:
    """v4.14.0 hot patch: return the documented free-tier daily-call
    default (PRESETS.default_max_per_day) for the configured provider,
    or None if it can't be resolved.

    Used as a sanity floor by the cap-learning path and as a
    pathological-state threshold by the startup recovery hook.
    Looks up the provider in data/api_providers.json by id, reads
    its 'preset', then reads PRESETS[preset]['default_max_per_day'].

    canonical_model is accepted for signature symmetry with the
    health API but isn't currently used — the preset default is a
    per-provider value, not per-model. Callers pass it so this can
    grow per-model awareness later without changing call sites.
    """
    if tm_apis is None:
        return None
    try:
        for prov in tm_apis.load_enabled_providers():
            if prov.get('id') != provider_id:
                continue
            preset_def = tm_apis.get_preset(prov.get('preset', ''))
            if not preset_def:
                return None
            cap = preset_def.get('default_max_per_day')
            if isinstance(cap, int) and cap > 0:
                return cap
            return None
    except Exception:
        pass
    return None


def record_call_outcome(provider_id: str,
                          *,
                          success: bool,
                          is_rate_limit: bool = False,
                          error_msg: str = "",
                          declared_cap: Optional[int] = None) -> None:
    """Update the health tracker with the outcome of a call. Called by
    both the consensus runner and the scan API runner after each call.

    Bridge that enables observed-quota learning AND auto-raise:
      - On 429: sets observed_max_per_day = trip_count * 0.95
      - On success past declared_cap: bumps raised_cap = calls_today * 1.20

    Args:
        provider_id: stable provider id
        success: True if the call returned a usable response
        is_rate_limit: True if the failure was specifically a 429
        error_msg: free-text error description for diagnostics
        declared_cap: v4.13.59 — the cap the router used for eligibility.
            Pass this so record_success can detect when we've sailed
            past it without trouble and auto-raise. Pass None to skip
            the auto-raise behavior.
    """
    if tm_provider_health is None:
        return
    try:
        state = tm_provider_health.get_state()
        if state is None:
            return
        if success:
            # v4.13.59: pass declared_cap so record_success can auto-raise
            # if we've past it without trouble. Forward-compatible: older
            # tm_provider_health versions ignore the kwarg.
            try:
                state.record_success(provider_id, declared_cap=declared_cap)
            except TypeError:
                # Old signature — fall back
                state.record_success(provider_id)
            _note_success_learning(provider_id)  # v4.14.5.14a.4
        elif is_rate_limit:
            # v4.14.5.14a.4: classify per-minute vs daily and record
            # accordingly (was: flat 5-min cooldown + preset-based
            # soft floor that didn't protect custom-preset providers).
            _classify_and_record_quota(state, provider_id, None,
                                       error_msg)
        else:
            state.record_failure(provider_id, error_msg)
    except Exception:
        pass


def routing_summary() -> dict:
    """Diagnostic — returns current state of the router for the UI.
    Each provider entry has: id, base_cap, effective_caps_by_type,
    health, calls_today, observed_cap.
    """
    out = {
        'call_types': list(VALID_CALL_TYPES),
        'policies': {ct: {k: v for k, v in p.items() if k != 'description'}
                      for ct, p in POLICIES.items()},
    }
    if tm_provider_health is not None:
        try:
            state = tm_provider_health.get_state()
            if state is not None:
                records = []
                import time
                now = time.time()
                for rec in state.all():
                    records.append({
                        'provider_id': rec.provider_id,
                        'health': (
                            'red' if rec.consecutive_429s >= 3 else
                            'amber' if rec.consecutive_429s >= 1 else
                            'green' if rec.calls_today > 0 else
                            'unknown'),
                        'in_cooldown': rec.in_cooldown(now),
                        'cooldown_remaining_sec': rec.cooldown_remaining_sec(now),
                        'calls_today': rec.calls_today,
                        'fails_today': rec.fails_today,
                        'last_error': rec.last_error,
                        'observed_max_per_day': getattr(
                            rec, 'observed_max_per_day', None),
                    })
                out['providers'] = records
        except Exception:
            pass
    return out


# ════════════════════════════════════════════════════════════════════════
# v4.14.0 — model-aware router APIs
#
# What this section adds (without changing anything above):
#   - provider_canonical_id()       → resolves a provider dict to the
#                                       canonical provider id used by
#                                       the model registry
#                                       (e.g. 'groq', 'cerebras')
#   - select_provider_groups()      → returns {canonical_model:
#                                       [(provider_id, provider_model_string)]}
#                                       grouped + failover-ordered
#   - RouterRun                     → per-run sticky-pick state
#   - classify_failure()            → triages an error into quota /
#                                       transient / fatal per the spec
#   - record_call_outcome_for_model → model-aware outcome recorder
#                                       (drop-in replacement for the
#                                       legacy record_call_outcome
#                                       once stages 4-5 land)
#
# Stages 4-5 will switch the consensus runner and the scan runner to
# call select_provider_groups() + RouterRun() + classify_failure() +
# record_call_outcome_for_model(). Until then, the legacy paths
# (select_providers / record_call_outcome) keep working unchanged.
# ════════════════════════════════════════════════════════════════════════


# ─── Provider id resolution ───────────────────────────────────────────

def _endpoint_to_canonical_provider_id(endpoint: str) -> Optional[str]:
    """Map an endpoint URL to the canonical provider id used by the
    model registry (e.g. 'groq'). Substring match against the same
    pattern set used elsewhere in this module. Returns None if the
    URL doesn't match any known provider."""
    if not endpoint:
        return None
    ep = endpoint.lower()
    if GROQ_PATTERN in ep:
        return 'groq'
    if GEMINI_PATTERN in ep:
        return 'google'
    if MISTRAL_PATTERN in ep:
        return 'mistral'
    if CEREBRAS_PATTERN in ep:
        return 'cerebras'
    if GITHUB_PATTERN in ep:
        return 'github'
    if SAMBANOVA_PATTERN in ep:
        return 'sambanova'
    if ANTHROPIC_PATTERN in ep:
        return 'anthropic'
    if OPENAI_PATTERN in ep:
        return 'openai'
    return None


def provider_canonical_id(provider: dict) -> Optional[str]:
    """Return the canonical provider id used by the model registry
    for this provider dict. Resolution order:

      1. The provider's `preset` field if it's set and not 'custom'
         / 'unknown'. Most users have preset='groq' / 'mistral' /
         'google' etc., so this is the fast path.
      2. Endpoint URL pattern detection — covers user-configured
         'custom' providers that point at known endpoints (Cerebras,
         GitHub Models, Sambanova).
      3. None — caller treats as unmapped.
    """
    preset = (provider.get('preset') or '').strip().lower()
    if preset and preset not in ('custom', 'unknown', ''):
        return preset
    return _endpoint_to_canonical_provider_id(
        provider.get('endpoint', '') or '')


# ─── Canonical model resolution + model-aware eligibility ─────────────

def _resolve_canonical_model(provider: dict, registry
                              ) -> tuple[Optional[str], str]:
    """Look up the canonical model id for a provider's configured
    model. Returns (canonical_id_or_None, diagnostic_label).

    diagnostic_label is for log lines, never user-facing copy:
      'via registry' / 'no registry' / 'unknown provider' /
      'no model configured' / 'unmapped model'.
    """
    if registry is None:
        return (None, 'no registry')
    canonical_pid = provider_canonical_id(provider)
    if canonical_pid is None:
        return (None, 'unknown provider')
    model_str = resolve_provider_model(provider)
    if not model_str:
        return (None, 'no model configured')
    cid = registry.get_canonical(canonical_pid, model_str)
    if cid is None:
        return (None, 'unmapped model')
    return (cid, 'via registry')


def _apply_observed_cap_for_model(base_cap: Optional[int],
                                    provider_id: str,
                                    canonical_model: str
                                    ) -> Optional[int]:
    """Model-aware version of _apply_observed_cap. Reads the
    per-(provider, canonical_model) ModelHealth record so an
    observed quota learned for Groq Llama 70B doesn't tighten the
    cap on Groq Llama 8B, and vice versa."""
    if tm_provider_health is None:
        return base_cap
    try:
        state = tm_provider_health.get_state()
        if state is None:
            return base_cap
        rec = state.get(provider_id, canonical_model=canonical_model)
        if rec is None:
            return base_cap
        observed = getattr(rec, 'observed_max_per_day', None)
        if observed and observed > 0:
            if base_cap is None:
                return observed
            return min(base_cap, observed)
    except Exception:
        pass
    return base_cap


def is_eligible_for_model(provider: dict, call_type: str,
                           canonical_model: str
                           ) -> tuple[bool, str, Optional[int]]:
    """Like is_eligible (above) but health checks are scoped to the
    specific (provider, canonical_model) pair. Used by the model-
    aware select_provider_groups path. Mirrors is_eligible's checks
    one for one — only the health-check call differs."""
    if not provider.get('enabled'):
        return (False, 'provider disabled', None)
    if not provider.get('endpoint') or not resolve_provider_model(
            provider):
        return (False, 'missing endpoint or model', None)

    if tm_deprecations is not None:
        try:
            preset = provider.get('preset', '')
            model_name = resolve_provider_model(provider)
            depr = tm_deprecations.lookup(preset, model_name)
            if depr is not None and depr.get('replacement'):
                return (False,
                        (f"model '{model_name}' deprecated; "
                          f"recommended: '{depr['replacement']}'"),
                        None)
        except Exception:
            pass

    policy = POLICIES.get(call_type) or {
        'max_per_run': None, 'blocked_endpoints': [], 'cap_factor': 1.0}

    if _endpoint_matches(provider, policy.get('blocked_endpoints', [])):
        return (False,
                f"blocked for call_type={call_type} (endpoint match)",
                None)

    # v4.14.5.14rot Patch 3b: model-aware base-cap read (per-model
    # learned cap when use_per_model_cap_tracking is on; else the
    # family-wide _default — byte-identical to Patch 3a).
    base_cap = _resolve_provider_cap(provider, canonical_model)
    cap_factor = policy.get('cap_factor', 1.0)
    if base_cap is not None and cap_factor < 1.0:
        effective_cap = max(1, int(base_cap * cap_factor))
    else:
        effective_cap = base_cap

    prov_id = provider.get('id') or provider.get('name', '?')
    effective_cap = _apply_observed_cap_for_model(
        effective_cap, prov_id, canonical_model)

    if tm_provider_health is not None:
        try:
            state = tm_provider_health.get_state()
            if state is not None:
                safe, reason = state.is_safe_to_call(
                    prov_id,
                    canonical_model=canonical_model,
                    max_per_day=effective_cap)
                if not safe:
                    return (False, reason, effective_cap)
        except Exception:
            pass

    return (True, '', effective_cap)


def _rotation_pick_model(prov: dict, call_type: str, registry,
                          class_filter: Optional[str]
                          ) -> Optional[tuple]:
    """v4.14.5.14rot Patch 3b — intra-provider skip-the-dead-models.

    PHASE-0 ARCHITECTURE NOTE: the brief framed this as "make
    resolve_provider_model skip exhausted models", but that helper has
    no call_type (needed for the effective cap) and is called from
    INSIDE is_eligible / is_eligible_for_model (→ recursion). The
    correct seam is HERE, in the model-aware selector: it already has
    call_type, the registry, the provider loop and the p2b diagnostic
    hook. The p2b read-only monotonic cursor is left untouched — we
    only SCAN list offsets starting at the cursor position; the once-
    per-dispatch advance in record_call_outcome_for_model is unchanged.

    Returns one of:
      - None                       → not applicable (flag off /
                                      rotation off / ≤1 model / error);
                                      caller uses the legacy path,
                                      byte-identical to Patch 3a.
      - (cid, model_str, skips)    → first non-exhausted model found
                                      at/after the cursor; `skips` =
                                      [(model, reason), …] passed-over.
      - (None, None, skips)        → ALL configured models are
                                      ineligible/exhausted this call.

    Never raises (any fault → None → legacy path)."""
    try:
        import tm_model_cursor as _mc
        if not (_mc.per_model_caps_enabled() and _mc.rotation_enabled()):
            return None
        models = (prov or {}).get('models')
        if not isinstance(models, (list, tuple)):
            return None
        ms = [str(m).strip() for m in models if str(m).strip()]
        # v4.14.5.62-model-routing Part 1: on a high-volume SCAN call_type,
        # exclude lookup-tier models (e.g. gemini-2.5-pro — tight free-tier
        # RPM, meant for Look Up / AI-question / Verify only) from the
        # rotation. Other call_types (lookup/teacher/verify/consensus) keep
        # all models. If the filter would empty the list, keep the original
        # (never strand a provider — its scan-tier model, if any, still wins
        # via the normal eligibility loop). Fail-open on any import error.
        try:
            import tm_model_capability as _cap
            _filtered = [m for m in ms if _cap.is_scan_eligible_model(
                m, call_type)]
            if _filtered:
                ms = _filtered
        except Exception:
            pass
        if len(ms) <= 1:
            return None
        pid = (prov.get('id') or prov.get('name') or '?')
        base = _mc.get_next_model_index(pid, len(ms))
        if not (0 <= base < len(ms)):
            base = 0
        canon_pid = provider_canonical_id(prov) or 'unknown'
        skips: list[tuple] = []
        n = len(ms)
        for off in range(n):
            cand = ms[(base + off) % n]
            cid = None
            if registry is not None:
                try:
                    cid = registry.get_canonical(canon_pid, cand)
                except Exception:
                    cid = None
            if cid is None:
                cid = f"unknown/{canon_pid}/{cand}"
            # honour class_filter per-candidate (same intent as the
            # legacy per-provider check, just at model granularity)
            if class_filter is not None and registry is not None:
                try:
                    if registry.get_class(cid) != class_filter:
                        skips.append((cand, f"class!={class_filter}"))
                        continue
                except Exception:
                    pass
            ok, reason, _cap = is_eligible_for_model(
                prov, call_type, cid)
            if ok:
                return (cid, cand, skips)
            skips.append((cand, reason))
        return (None, None, skips)  # every model unavailable
    except Exception:
        return None  # fail-open → legacy single-model path


# ─── Public selection: model-aware ────────────────────────────────────

def select_provider_groups(providers: list[dict],
                             call_type: str,
                             registry=None,
                             class_filter: Optional[str] = None,
                             log_fn: Optional[
                                 Callable[[str, str], None]] = None
                             ) -> dict[str, list[tuple[str, str]]]:
    """Model-aware provider selection (v4.14.0).

    Returns a dict keyed by canonical_model id, where each value is
    a failover-ordered list of `(provider_id, provider_model_string)`
    tuples. The order within each list mirrors the order of the
    input `providers` list — caller controls preference by sorting
    before calling.

    A canonical_model is included only if at least one provider
    serving it passes:
      - enabled, has endpoint+model, not deprecated
      - not blocked by call_type policy (e.g. Sambanova on scan)
      - per-(provider, model) health check (no cooldown, under cap)
      - matches class_filter ('A', 'B', or None for both) when given

    `class_filter` only takes effect when `registry` is non-None and
    knows the model's class. Unknown classes are skipped if filter
    is set.

    If the registry is missing OR a provider's model is unmapped,
    the function falls back to a synthetic canonical id of the form
    `unknown/<provider_id>/<model_string>` so the call still routes
    cleanly. The caller should treat unknown ids as un-deduped.
    """
    groups: dict[str, list[tuple[str, str]]] = {}
    skipped: list[tuple[str, str]] = []  # (label, reason)

    for prov in providers:
        if not prov.get('enabled'):
            continue
        label = (prov.get('name') or prov.get('display_name')
                 or prov.get('id') or '?')

        # ── v4.14.5.14rot Patch 3b: intra-provider skip-the-dead ──
        # Applicable ONLY when use_per_model_cap_tracking + rotation
        # are on AND the provider has a >1 model list; otherwise
        # _rotation_pick_model returns None and we fall through to the
        # byte-identical legacy path below.
        _pick = _rotation_pick_model(prov, call_type, registry,
                                     class_filter)
        if _pick is not None:
            _sel_cid, _sel_model, _skips = _pick
            # Part 4 diagnostics — make per-model exhaustion visible,
            # deduped per scan-run (same noise discipline as the
            # standard skip lines). Supplements, never replaces.
            if log_fn:
                _dd = _active_skip_dedup
                for _sm, _sr in _skips:
                    if (_dd is None or _dd.should_log(
                            f"{label}/{_sm}", "rotation_model_skip")):
                        try:
                            log_fn(
                                f"[router] {label}/{_sm}: skipped "
                                f"({_sr}). Trying next model in "
                                f"rotation.", 'amber')
                        except Exception:
                            pass
            if _sel_cid is None:
                # every configured model unavailable this call →
                # provider unavailable, exactly like a fully-exhausted
                # single-model provider today (also pushed to `skipped`
                # so the standard summary + dedup cover it too).
                _n = len(_skips) if _skips else 0
                if log_fn and (
                        _active_skip_dedup is None
                        or _active_skip_dedup.should_log(
                            label, "all_models_exhausted")):
                    try:
                        log_fn(
                            f"[router] {label}: all {_n} configured "
                            f"models unavailable for {call_type} "
                            f"(daily cap / cooldown). Provider "
                            f"unavailable for this call.", 'amber')
                    except Exception:
                        pass
                skipped.append(
                    (label, "all rotation models exhausted"))
                continue
            # a live model was found (possibly after skipping dead
            # ones) — dispatch THAT model under ITS canonical id.
            groups.setdefault(_sel_cid, []).append(
                (prov.get('id') or prov.get('name', '?'), _sel_model))
            # The p2b describe_rotation line is deliberately NOT
            # emitted in this branch: it reports the cursor's BASE
            # model, which can differ from the model we actually
            # picked after skipping dead ones. The per-model skip
            # lines above + the standard group summary already show
            # the real choice, accurately.
            continue

        # ── legacy single-model path (flag/rotation off, ≤1 model, or
        # _rotation_pick_model fault) — byte-identical to pre-3b ──
        cid, _diag = _resolve_canonical_model(prov, registry)
        if cid is None:
            canonical_pid = provider_canonical_id(prov) or 'unknown'
            cid = (f"unknown/{canonical_pid}/"
                   f"{resolve_provider_model(prov)}")

        if class_filter is not None:
            cls = None
            if registry is not None:
                try:
                    cls = registry.get_class(cid)
                except Exception:
                    cls = None
            if cls != class_filter:
                continue

        ok, reason, _cap = is_eligible_for_model(prov, call_type, cid)
        if not ok:
            skipped.append((label, reason))
            continue

        groups.setdefault(cid, []).append(
            (prov.get('id') or prov.get('name', '?'),
             resolve_provider_model(prov)))

        # v4.14.5.14rot Patch 2b: surface rotation in the activity
        # log — ONLY for a >1-model provider with both flags on
        # (describe_rotation returns None otherwise, so single-model /
        # flag-off providers stay silent: no noise for the common
        # case). Read-only (no cursor advance here). Never raises.
        if log_fn:
            try:
                import tm_model_cursor as _mc
                _rl = _mc.describe_rotation(prov)
                if _rl:
                    log_fn(_rl, 'muted')
            except Exception:
                pass

    if log_fn:
        try:
            if groups:
                summary_bits = []
                for cm, plist in groups.items():
                    short_cm = cm.split('/')[-1] if '/' in cm else cm
                    if len(plist) == 1:
                        summary_bits.append(
                            f"{short_cm}={plist[0][0]}")
                    else:
                        provs = ','.join(p[0] for p in plist)
                        summary_bits.append(
                            f"{short_cm}=[{provs}]")
                log_fn(
                    f"AI router [{call_type}]: "
                    f"{len(groups)} canonical model(s); "
                    f"{', '.join(summary_bits)}",
                    'muted')
            # v4.14.0 stage 7.1: dedup skip lines via active scan-run
            # tracker. Same key shape as select_providers above —
            # (label, classified_reason) so cooldown countdowns don't
            # bypass dedup.
            dedup = _active_skip_dedup
            for label, reason in skipped:
                if dedup is not None:
                    cat = _classify_skip_reason(reason)
                    if not dedup.should_log(label, cat):
                        continue
                log_fn(
                    f"AI router [{call_type}]: skipped {label} — "
                    f"{reason}", 'amber')
        except Exception:
            pass

    return groups


# ─── v4.14.0 stage 7.1: per-scan-run skip-log dedup ──────────────────
#
# Scan loops process N candidates, each calling select_providers /
# select_provider_groups, each of which can emit "skipped X — Y" log
# lines for the same providers (e.g. SambaNova blocked for call_type
# =scan, Gemini in cooldown for 286s, ...). Without dedup, the user
# sees N copies of the same line per scan — pure noise that hides
# real events.
#
# the user's stage 7.1 spec says "maintain a per-RouterRun set." But
# RouterRun in practice is per-canonical-call (constructed AFTER
# select_provider_groups), not per-scan — and the skip emission lives
# INSIDE select_provider_groups, before any RouterRun exists. So the
# dedup state lives at module scope, gated by explicit
# begin_scan_run() / end_scan_run() markers the scan loop calls.
# Module-level state is safe because scans don't overlap (existing
# scan locks in tm_holdings prevent it).
#
# Dedup key: (provider_label, classified_reason). The reason field
# from is_eligible() is verbose and includes a ticking countdown for
# cooldown ("in cooldown for another 286s"). Classifying it into a
# coarse category ("cooldown_pending") keeps the dedup correct even
# as the countdown ticks.

class SkipLogDedup:
    """v4.14.0 stage 7.1: tracker that suppresses duplicate skip
    log lines within one scan run.

    Construct via begin_scan_run(). Drop via end_scan_run(). Used
    automatically by select_providers + select_provider_groups when
    a scan run is active. Single-threaded — scans don't overlap.
    """

    def __init__(self):
        # Set of (provider_label, classified_reason) tuples already
        # logged this run.
        self._logged: set = set()

    def should_log(self, provider_label: str,
                    classified_reason: str) -> bool:
        """Return True if this is the first time the (provider,
        reason) pair has been seen this run; False if duplicate.
        Side effect: marks the pair as seen on first call."""
        key = (provider_label or '?',
               classified_reason or 'unknown')
        if key in self._logged:
            return False
        self._logged.add(key)
        return True


# Module-level slot. Set by begin_scan_run, cleared by end_scan_run.
# When None, no dedup happens — skip lines emit as they did pre-7.1.
_active_skip_dedup: Optional['SkipLogDedup'] = None

# v4.14.1.1: per-scan-run round-robin counter for the single-provider
# scan mode. Incremented on every next_scan_canonical_pick() call so
# candidate N picks canonical_models[N % len(models)]. Reset by
# begin_scan_run / end_scan_run alongside the dedup state. The
# announcement flag gates the one-time "Scan single-provider mode:
# rotating across N canonical models" log line so it fires once per
# scan instead of once per candidate.
_active_scan_rotation_counter: int = 0
_active_scan_first_pick_logged: bool = False


def begin_scan_run() -> SkipLogDedup:
    """Start a new scan-run dedup window. Caller is the scan loop
    (run_discover_scan_headless, run_all_paths_scan, etc.). Pair
    with end_scan_run() in a try/finally so an exception in the
    loop doesn't leave the dedup hot for the next scan.

    Returns the new SkipLogDedup so the caller can also consult it
    directly if needed.

    v4.14.1.1: also resets the scan rotation counter + announcement
    flag so the single-provider-mode round-robin starts fresh.
    """
    global _active_skip_dedup
    global _active_scan_rotation_counter, _active_scan_first_pick_logged
    _active_skip_dedup = SkipLogDedup()
    _active_scan_rotation_counter = 0
    _active_scan_first_pick_logged = False
    return _active_skip_dedup


def end_scan_run() -> None:
    """Clear the active scan-run dedup window. Idempotent — calling
    when nothing is active is a no-op."""
    global _active_skip_dedup
    global _active_scan_rotation_counter, _active_scan_first_pick_logged
    _active_skip_dedup = None
    _active_scan_rotation_counter = 0
    _active_scan_first_pick_logged = False


def is_scan_run_active() -> bool:
    """v4.14.1.1: True if begin_scan_run() has been called and
    end_scan_run() has not. The scan single-provider mode in
    tm_api_providers.run_apis_for_scan_prediction gates on this."""
    return _active_skip_dedup is not None


# ─── v4.14.5.14-capacity-weighted-scan: bulk-scan capacity weights ────
#
# Static per-family scan-capacity tier. HIGHER = a larger share of
# bulk-scan picks. Heuristic, NOT a contract — tuned to bias bulk load
# toward providers with real daily-capacity headroom (Mistral unmetered
# + ~30 RPM; Gemini unmetered/1500 daily) and AWAY from the tight
# per-minute ceilings (Groq/Cerebras ~25-30 RPM) that throw the all-day
# burst 429s the investigation traced. Daily caps were barely scratched;
# the per-minute wall was the problem, so this is a routing-SHAPE fix,
# not a volume fix. SCAN-ONLY: consensus/lookup/recommend_run never call
# this picker (they fan out to every eligible model — see POLICIES).
# Calibrated so Mistral+Gemini land ~50-55% combined and Groq ~15-20%
# (down from ~30%) when all providers are healthy and each serves one
# model; the per-provider normalisation below keeps that true even when
# a provider exposes several models. Gemini is intentionally NOT the
# top weight despite unmetered daily because its real free-tier RPM
# (~12-15) is the tightest per-minute of the bunch.
#
# v4.14.6.4-tier1-routing (2026-06-11): Gemini demoted 1.6 → 0.5 (~24%
# of picks → ~7%). The 1.6 weight was calibrated for *daily* headroom
# (Gemini is unmetered daily), but Tier-1 burst fill is bottlenecked on
# per-minute RPM, not daily — and Gemini's per-minute (~12-15) is the
# tightest. With Gemini at 1.6 a 30-ticker burst sent ~7 tickers to
# Gemini, which caps after ~6 and stalls the rest. Mistral + Groq +
# Cerebras (each weight 1.0+) are the workhorses; Gemini becomes
# genuine fallback. Other weights unchanged. Live de-weighting via
# tm_provider_learning (429 pressure + headroom) still tunes around
# this static base.
_STATIC_SCAN_WEIGHTS = {
    'mistral':   2.0,
    'gemini':    0.5,   # v4.14.6.4: was 1.6 — RPM-tight, demoted
    'github':    1.0,
    'groq':      1.0,
    'cerebras':  1.0,
    'sambanova': 0.2,   # 20/day — already blocked from scan anyway
}
_DEFAULT_SCAN_WEIGHT = 1.0  # unknown / unreadable provider → neutral


def _provider_scan_weight(provider: dict) -> float:
    """Capacity-aware base weight for ONE provider's share of bulk-scan
    picks (higher = more share). Combines a static per-family tier with
    two live signals from tm_provider_learning:
      - today's 429 pressure (a provider stressing today is de-weighted),
      - daily headroom (a FINITE-cap provider near its cap is de-weighted;
        an unmetered provider — learned cap None — keeps full weight).
    Never raises; unknown/unreadable → neutral _DEFAULT_SCAN_WEIGHT. The
    weights are heuristic, so missing data simply falls back to the
    static tier (see the decision authority in the patch / DECISIONS)."""
    try:
        import tm_provider_learning as _tpl
    except Exception:
        return _DEFAULT_SCAN_WEIGHT
    try:
        fam = (_tpl.provider_family(provider) or '').lower()
    except Exception:
        fam = ''
    base = _STATIC_SCAN_WEIGHTS.get(fam, _DEFAULT_SCAN_WEIGHT)
    # Live 429-pressure de-weight: recent stress lowers the share so we
    # stop feeding a provider that's already throwing throttles today.
    try:
        calls, r429 = _tpl.today_pressure_for(fam)
        if r429 and r429 > 5:
            base = base / (1.0 + r429 * 0.1)
    except Exception:
        calls = None
    # Daily-headroom de-weight: only for finite-cap providers. Unmetered
    # (cap None) keeps full weight — that's the whole point of biasing
    # bulk work to them.
    try:
        cap = _tpl.get_learned_cap(fam)
        if cap and cap > 0:
            used = calls if isinstance(calls, int) else 0
            headroom = max(0, cap - used) / cap
            base *= (0.25 + 0.75 * headroom)  # full headroom→1×, none→0.25×
    except Exception:
        pass
    try:
        return max(0.01, float(base))
    except Exception:
        return _DEFAULT_SCAN_WEIGHT


def _weighted_scan_pick(sorted_cms: list[str],
                        groups: dict,
                        providers_by_id: dict) -> Optional[str]:
    """Capacity-weighted pick of one canonical model for a scan call.

    Each canonical model is weighted by the capacity of the provider
    that would actually SERVE it (the first/preferred entry in its
    group), divided by how many canonical models that provider exposes
    this run — so a provider that lists 3 models doesn't get 3× the
    share just for listing more models (the exact accidental-bias the
    investigation found for Groq). Then samples ONE model proportional
    to those weights. Returns None on any structural gap so the caller
    falls back to the legacy even round-robin. Never raises."""
    # Serving provider id per canonical model = first preference entry.
    serving: dict[str, Optional[str]] = {}
    for cm in sorted_cms:
        plist = groups.get(cm) or []
        serving[cm] = plist[0][0] if plist else None
    # How many canonical models each provider serves-first this run.
    cm_count: dict[str, int] = {}
    for pid in serving.values():
        if pid:
            cm_count[pid] = cm_count.get(pid, 0) + 1
    weights: list[float] = []
    for cm in sorted_cms:
        pid = serving.get(cm)
        prov = providers_by_id.get(pid) if pid else None
        base = (_provider_scan_weight(prov) if prov
                else _DEFAULT_SCAN_WEIGHT)
        n = cm_count.get(pid, 1) or 1
        weights.append(max(0.0001, base / n))
    if not weights or not any(w > 0 for w in weights):
        return None
    return random.choices(sorted_cms, weights=weights, k=1)[0]


def next_scan_canonical_pick(canonical_models: list[str],
                              groups: Optional[dict] = None,
                              providers_by_id: Optional[dict] = None
                              ) -> Optional[str]:
    """v4.14.1.1: pick one canonical model per candidate within a scan
    run. v4.14.5.14-capacity-weighted-scan: when `groups` +
    `providers_by_id` are supplied, the pick is CAPACITY-WEIGHTED (see
    _weighted_scan_pick) so bulk load biases toward high-headroom
    providers and away from tight per-minute ceilings. Without them (or
    on any weighting fault) it falls back to the original even
    round-robin across the sorted models — byte-identical to pre-patch.

    Returns None when the list is empty (caller falls back to the
    all-providers-exhausted no-call path).

    Single-threaded — scans don't overlap. The existing scan locks
    in tm_holdings.py prevent concurrent scan runs, same precondition
    SkipLogDedup relies on.
    """
    global _active_scan_rotation_counter
    if not canonical_models:
        return None
    sorted_cms = sorted(canonical_models)
    # Capacity-weighted path (scan-only; gated by the caller passing the
    # group/provider maps). Fail-OPEN to the legacy round-robin below.
    if groups is not None and providers_by_id is not None:
        try:
            picked = _weighted_scan_pick(sorted_cms, groups,
                                         providers_by_id)
            if picked is not None:
                _active_scan_rotation_counter += 1
                return picked
        except Exception:
            pass
    idx = _active_scan_rotation_counter % len(sorted_cms)
    _active_scan_rotation_counter += 1
    return sorted_cms[idx]


def scan_first_pick_announced() -> bool:
    """v4.14.1.1: True if the run-start "single-provider mode" log
    line has already been emitted this scan run. Used by the scan
    runner so the announcement fires once per scan, not once per
    candidate."""
    return _active_scan_first_pick_logged


def mark_scan_first_pick_announced() -> None:
    """v4.14.1.1: latch the announcement flag. Called by the scan
    runner right after it emits the run-start log line."""
    global _active_scan_first_pick_logged
    _active_scan_first_pick_logged = True


def _classify_skip_reason(reason: str) -> str:
    """v4.14.0 stage 7.1: bin a verbose skip-reason string from
    is_eligible / is_eligible_for_model into a coarse category for
    dedup keying.

    Categories cover the deterministic cases that fire repeatedly:
      - 'blocked_by_call_type_policy' — endpoint match in
        POLICIES.blocked_endpoints (e.g. SambaNova on scan)
      - 'cooldown_pending'           — 429-induced cooldown active
      - 'daily_cap_reached'          — observed cap hit
      - 'no_endpoint_or_model'       — config gap (missing fields)
      - 'no_key'                     — api_key missing
      - 'deprecated'                 — known-deprecated model
      - 'disabled'                   — provider disabled
      - 'unknown'                    — fallback (still dedupes;
                                        first-seen wins per scan)

    Coarse on purpose: countdown text in "in cooldown for another
    286s" must NOT make each candidate log a distinct message.
    """
    r = (reason or '').lower()
    if 'blocked' in r or 'forbidden for' in r:
        return 'blocked_by_call_type_policy'
    if 'cooldown' in r:
        return 'cooldown_pending'
    if 'cap' in r or 'quota' in r or 'exceeded' in r:
        return 'daily_cap_reached'
    if 'missing endpoint' in r or 'missing model' in r:
        return 'no_endpoint_or_model'
    if 'no api key' in r or 'no key' in r:
        return 'no_key'
    if 'deprecated' in r:
        return 'deprecated'
    if 'disabled' in r:
        return 'disabled'
    return 'unknown'


# ─── Per-run sticky-pick state ────────────────────────────────────────

class RouterRun:
    """Per-run sticky-pick state for the model-aware router (v4.14.0).

    Lifetime: ONE scan or consensus run. NOT persisted. The next
    run starts fresh — picks could land on the same provider or a
    different one based on current health/quota state. (This is
    the user's resolved Q2: per-run, not per-day.)

    Within one run:
      - Once a provider is picked for a canonical model via pick(),
        subsequent pick() calls for the same canonical model return
        the same provider (the "sticky" pick).
      - On a quota error, call mark_exhausted(canonical_model,
        provider_id). The next pick() for that canonical model will
        skip the exhausted provider and return the next preference.
      - On transient or fatal errors, the caller decides whether to
        keep using the sticky pick (transient = retry same provider
        2x then failover) or stop entirely (fatal = no retry, no
        failover) — see classify_failure() for the rule.
    """

    def __init__(self,
                  groups: dict[str, list[tuple[str, str]]]):
        # Defensive copy so callers can mutate their groups dict
        # without affecting in-flight runs.
        self._groups: dict[str, list[tuple[str, str]]] = {
            cm: list(plist) for cm, plist in groups.items()}
        self._sticky: dict[str, tuple[str, str]] = {}
        self._exhausted: dict[str, set[str]] = {}
        # v4.14.5.62-limiter-concurrency: guard the sticky/exhausted
        # read-modify-writes. Today RouterRun is driven sequentially (one
        # scan/consensus run at a time), so this RLock is uncontended and
        # behavior is byte-identical. It future-proofs parallel consensus
        # dispatch (one thread per canonical-model voice) so two threads
        # picking the SAME canonical model can't both read None and both
        # pick-fresh (last-writer-wins divergence). RLock so pick() can call
        # _pick_fresh() while holding it.
        self._lock = threading.RLock()

    def all_canonical_models(self) -> list[str]:
        """All canonical models the run will iterate. Order matches
        the dict the caller passed in (Python 3.7+ insertion order).
        """
        return list(self._groups.keys())

    def pick(self, canonical_model: str
              ) -> Optional[tuple[str, str]]:
        """Return the sticky pick for this canonical model, or pick
        a fresh one from the preference list (skipping exhausted
        providers). None if no preference list option remains."""
        with self._lock:
            existing = self._sticky.get(canonical_model)
            if existing is not None:
                return existing
            return self._pick_fresh(canonical_model)

    def _pick_fresh(self, canonical_model: str
                     ) -> Optional[tuple[str, str]]:
        prov_list = self._groups.get(canonical_model, [])
        exhausted = self._exhausted.get(canonical_model, set())
        for prov_id, model_str in prov_list:
            if prov_id not in exhausted:
                self._sticky[canonical_model] = (prov_id, model_str)
                return (prov_id, model_str)
        return None

    def mark_exhausted(self, canonical_model: str,
                         provider_id: str) -> None:
        """Quota-hit / explicit exhaustion: drop this provider from
        rotation for this canonical model for the rest of the run.
        Clears the sticky pick if it was this provider so the next
        pick() returns the next preference."""
        with self._lock:
            self._exhausted.setdefault(
                canonical_model, set()).add(provider_id)
            cur = self._sticky.get(canonical_model)
            if cur is not None and cur[0] == provider_id:
                del self._sticky[canonical_model]

    def sticky_pick(self, canonical_model: str
                     ) -> Optional[tuple[str, str]]:
        """Read-only: current sticky pick for this canonical model
        (or None if pick() hasn't been called yet)."""
        return self._sticky.get(canonical_model)

    def exhausted_providers(self, canonical_model: str) -> set[str]:
        """Read-only: set of provider_ids that have been marked
        exhausted for this canonical model in this run."""
        return set(self._exhausted.get(canonical_model, set()))

    def remaining_providers(self, canonical_model: str
                              ) -> list[tuple[str, str]]:
        """Read-only: providers still in rotation for this
        canonical model (preference order preserved)."""
        exhausted = self._exhausted.get(canonical_model, set())
        return [(pid, ms) for pid, ms
                in self._groups.get(canonical_model, [])
                if pid not in exhausted]


# ─── Failure classification ───────────────────────────────────────────

# Per the resolved spec (v4.14.0_routing_design.md §3 "Failover
# trigger"), call outcomes split into four buckets that drive
# different next-step behavior. Stages 4-5 use these to decide
# whether to retry (transient), failover (quota), stop entirely
# (fatal), or record the vote (success).

OUTCOME_QUOTA = 'quota'
OUTCOME_TRANSIENT = 'transient'
OUTCOME_FATAL = 'fatal'
OUTCOME_SUCCESS = 'success'

VALID_OUTCOMES = (OUTCOME_QUOTA, OUTCOME_TRANSIENT,
                    OUTCOME_FATAL, OUTCOME_SUCCESS)


def classify_failure(*, status_code: Optional[int] = None,
                       error_text: str = "",
                       exception: Optional[BaseException] = None
                       ) -> str:
    """Triage a failed call into OUTCOME_QUOTA / OUTCOME_TRANSIENT /
    OUTCOME_FATAL.

    All three inputs are optional — pass whatever the call site has.
    Resolution order:
      1. HTTP status_code if available (most common path)
         429       → OUTCOME_QUOTA
         5xx       → OUTCOME_TRANSIENT
         other 4xx → OUTCOME_FATAL
      2. error_text body — looks for 'rate limit'/'429'/'quota
         exceeded' phrases (used when status code is absent because
         the failure was an exception)
      3. exception class name — looks for 'timeout' / 'connection'
         signatures
      4. Default: OUTCOME_FATAL (be conservative; don't loop)

    Note: the spec calls 4xx-other-than-429 'fatal' meaning no
    retry, no failover. That includes 401/403 (bad key) and 404
    (model not found). If those become a real pain point, the
    classification can be loosened; for now it follows the spec
    verbatim.
    """
    if status_code is not None:
        if status_code == 429:
            return OUTCOME_QUOTA
        if 500 <= status_code < 600:
            return OUTCOME_TRANSIENT
        if 400 <= status_code < 500:
            return OUTCOME_FATAL

    text = (error_text or '').lower()

    # Many call sites raise an Exception whose message starts with
    # the HTTP status code as a leading token (e.g. ProviderError
    # in tm_api_providers raises "429 rate-limited — ...",
    # "500 server error from provider...", etc.). Sniff that leading
    # code so callers don't have to pre-parse it.
    head = (text.split(None, 1) or [''])[0]
    if head.isdigit():
        try:
            code = int(head)
            if 100 <= code < 600:
                if code == 429:
                    return OUTCOME_QUOTA
                if 500 <= code < 600:
                    return OUTCOME_TRANSIENT
                if 400 <= code < 500:
                    return OUTCOME_FATAL
        except Exception:
            pass

    if any(p in text for p in ('rate limit', 'rate-limit',
                                  'rate_limit', '429',
                                  'quota exceeded',
                                  'quota_exceeded',
                                  'too many requests')):
        return OUTCOME_QUOTA

    if exception is not None:
        ename = type(exception).__name__.lower()
        if any(p in ename for p in ('timeout', 'connection',
                                      'connectionerror', 'network',
                                      'socket')):
            return OUTCOME_TRANSIENT

    if any(p in text for p in ('timeout', 'connection error',
                                  'network error', 'socket',
                                  'server error')):
        return OUTCOME_TRANSIENT

    # v4.14.5.17-empty-content-retry: an HTTP 200 with an empty
    # `content` field (Zhipu on FISV: returned empty, then a full
    # answer one minute later on an identical call) is an occasional
    # hiccup, not a permanent failure. Route it through the existing
    # transient retry loop (bounded by _TRANSIENT_BUDGET=2 in
    # tm_consensus.py / tm_api_providers.py) so the provider gets one
    # or two re-asks before the vote is dropped. A provider that
    # returns empty deterministically still gives up at the cap and
    # records vote-as-error exactly as the pre-patch FATAL path did —
    # just two attempts later. Substring-specific so it can't broaden
    # into other fatals: matches the exact ProviderError strings
    # raised at tm_api_providers.py:748,752 ("empty choices in
    # response" / "empty content in response").
    if 'empty content' in text or 'empty choices' in text:
        return OUTCOME_TRANSIENT

    return OUTCOME_FATAL


# ─── Outcome recorder (model-aware) ──────────────────────────────────

def record_call_outcome_for_model(provider_id: str,
                                     canonical_model: str,
                                     *,
                                     outcome: str,
                                     error_msg: str = "",
                                     declared_cap:
                                         Optional[int] = None
                                     ) -> None:
    """Model-aware outcome recorder — drop-in replacement for
    record_call_outcome once stages 4-5 wire it up.

    outcome is one of VALID_OUTCOMES:
      OUTCOME_SUCCESS   → record_success (counter + auto-raise check)
      OUTCOME_QUOTA     → record_rate_limit (cooldown + observed-max
                          learning), keyed on (provider, model) so
                          the cooldown isolates correctly
      OUTCOME_TRANSIENT → record_failure (counter only, no cooldown)
      OUTCOME_FATAL     → record_failure (counter only, no cooldown)
    """
    if outcome not in VALID_OUTCOMES:
        return
    if tm_provider_health is None:
        return
    try:
        state = tm_provider_health.get_state()
        if state is None:
            return

        # v4.14.5.14rot Patch 2b: ONE model-cursor advance per
        # RECORDED dispatch attempt. This is the single point all of
        # SUCCESS / QUOTA / TRANSIENT / FATAL pass through before they
        # branch+return; the only paths that skip it are the
        # non-attempt guards above (invalid outcome / no health
        # state) — correctly NOT advancing on a non-attempt. Gated by
        # rotation_enabled(); advancing a single-model (or flag-off
        # but enabled) provider is harmless (read-time idx = c % 1 =
        # 0). Self-contained try/except + advance_cursor never raises
        # → can never perturb outcome recording.
        try:
            import tm_model_cursor as _mc
            if _mc.rotation_enabled():
                _mc.advance_cursor(provider_id)
        except Exception:
            pass

        if outcome == OUTCOME_SUCCESS:
            try:
                state.record_success(
                    provider_id,
                    canonical_model=canonical_model,
                    declared_cap=declared_cap)
            except TypeError:
                state.record_success(
                    provider_id, canonical_model=canonical_model)
            # v4.14.5.14rot Patch 3b: thread the model so header-
            # derived caps learn per-(family, model) when the flag is
            # on (fam-wide _default otherwise).
            _note_success_learning(provider_id, canonical_model)
            return

        if outcome == OUTCOME_QUOTA:
            # v4.14.5.14a.4: classify per-minute vs daily (Retry-After
            # / X-RateLimit-* headers captured at the HTTP chokepoint)
            # and record accordingly. per-minute → short cooldown, no
            # daily-cap tighten (fixes the "one 30-RPM 429 → 5-min
            # cooldown all day, Cerebras false-learned to 48" bug).
            # daily → escalating cooldown + tighten + feed learner.
            # unknown → legacy path with the soft floor now using the
            # URL seed (works for preset:'custom' providers).
            _classify_and_record_quota(
                state, provider_id, canonical_model, error_msg)
            return

        # Transient and fatal → counter only.
        state.record_failure(
            provider_id, error_msg,
            canonical_model=canonical_model)
    except Exception:
        pass
