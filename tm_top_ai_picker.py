"""tm_top_ai_picker — pick the AI to run the continuous Recommend queue.

Continuous-queue Phase 1 module. The queue runner calls pick_top_ai
at the start of each pass to choose ONE AI to drive that pass's
candidate analysis. The chosen AI is the smartest one with budget
remaining today, optionally overridden by Advanced Settings.

Public API:
    pick_top_ai(app, override: str | None = None) -> dict

Return shape is ALWAYS a dict (never None). Callers branch on
``chosen['success']``:

    Success (chose an AI to run):
      {
        "success": True,
        "kind": "cloud" | "local",
        "id": "groq" | "qwen2.5:14b",
        "display_name": "Groq",
        "reason": "<explanation>",
        "fallback_from": "<override_id>" | None
      }

    Failure (no AI was chosen — caller routes to the matching surface):
      {
        "success": False,
        "reason": "none_configured" | "all_disabled" | "all_exhausted"
      }

The three failure reasons let the queue runner emit the right
system_event entry: nothing for `none_configured` (the action_prereq
intercepts cover the click-time surface), `all_providers_disabled` for
`all_disabled`, and `all_ais_budget_exhausted` for `all_exhausted`.

Algorithm — constraint-then-rank:
    1. Gather: enabled cloud providers + installed local Ollama models
       (filtering out 'embed' variants).
    2. Classify state before ranking — distinguish "none configured"
       (registry.all() empty AND no local models) from "all disabled"
       (registry.all() non-empty but registry.enabled() empty) from
       "all exhausted" (enabled set non-empty but every entry's budget
       hit zero).
    3. Filter to those with budget remaining.
    4. Rank by Wilson-CI lower bound from the source_weights table
       (query for the (model, '__global__', '__global__') row).
    5. Cold-start fallback when no provider has accuracy data yet:
       prefer Groq if configured, else first cloud alphabetically,
       else first local.
    6. Override path: if override is set and that provider has budget,
       return it. If override is set but exhausted, fall back to auto
       and set reason to "override exhausted, using auto".
"""

from __future__ import annotations

import threading
from typing import Any, Optional


GLOBAL_KEY = '__global__'  # matches tm_source_weights.GLOBAL_KEY


# v4.14.3.8 (2026-05-14): track which providers we've already
# warned about this session so the operational-fit notice doesn't
# fire on every pick call. Keyed by registry_id. Module-level
# state survives multiple pick_top_ai invocations but resets on
# app restart — fine, the warning is informational not actionable.
_burst_warned_this_session: set = set()
# v4.14.5.14-picker-message-clarity (2026-05-23): the dedup set above
# was checked then mutated with the whole warning body in between, so
# two concurrent picks (the fill loop + an event-driven sweep) could
# BOTH pass the check before either marked the set, printing the note
# twice (live log 2026-05-23 08:13 / 16:46). This lock makes the
# check-and-mark atomic.
_burst_warn_lock = threading.Lock()
# v4.14.6.111: dedup for the operational-fit (was "cold-start") notice. It used
# to emit on EVERY dispatch burst and read like an error; now log at most once
# per provider per session. Atomic check-and-mark reuses _burst_warn_lock.
_coldstart_noted_this_session: set = set()


def _maybe_warn_tight_burst(app, chosen: dict,
                            eligible_cloud_count: Optional[int] = None
                            ) -> None:
    """v4.14.3.8 (2026-05-14): when the picker chooses a
    'tight'-burst provider, surface a one-time-per-session note about
    the TPM and the expected pacing. No-op for moderate/generous
    providers, or for locals.

    v4.14.5.14-picker-message-clarity (2026-05-23): GATED on rotation
    breadth. The queue runner dispatches every candidate via the
    router's full multi-provider rotation (scan_provider_filter=None
    since v4.14.3.11) — the picker's 'chosen' provider is used only to
    health-gate and label the log; it does NOT pin the pass. So the old
    wording, which named the picker's pick as the single provider the
    whole pass would run on, was misinformation whenever 2+ providers
    are eligible: live logs showed Groq (the picked one) actually
    handling 3-7 of ~20 calls while rotation spread the rest across
    Gemini/Cerebras/Mistral/GitHub.

    The note is TRUE only when exactly ONE cloud provider is eligible
    (nothing to rotate across → every call really does hit it). So we
    emit only when `eligible_cloud_count == 1`, with wording that says
    exactly that, and suppress entirely otherwise (including when the
    count is unknown — never risk a false "only provider" claim)."""
    if not chosen or not chosen.get('success'):
        return
    if chosen.get('kind') == 'local':
        return
    reg_id = chosen.get('registry_id', '')
    if not reg_id or reg_id in _burst_warned_this_session:
        return
    try:
        import tm_rate_limiter as _trl
        # Resolve the provider's burst_category and tpm from
        # PRESET_DEFAULTS (cheaper than re-gathering the full dict).
        preset = chosen.get('id', '')  # legacy 'id' is the preset.
        defaults = _trl.PRESET_DEFAULTS.get(preset)
        if not defaults:
            return
        burst = defaults.get('burst_category', 'moderate')
        if burst != 'tight':
            return
        tpm = defaults.get('tpm')
        # Read max_candidates_per_pass from cfg.
        cfg = getattr(app, 'cfg', {}) or {}
        max_per_pass = int(
            cfg.get('queue_runner_max_candidates_per_pass', 20))
        if max_per_pass <= 5:
            # Small passes won't trip TPM. Skip the warning.
            return
        # Rotation-breadth gate (see docstring): only meaningful when
        # this tight provider is the SOLE eligible cloud provider.
        if eligible_cloud_count != 1:
            return
        # Atomic check-and-mark so two concurrent picks (the fill loop
        # + an event-driven sweep) can't both emit the same note (the
        # 2026-05-23 double-print). The early `in` check above is a
        # cheap fast-path; this is the authoritative guard.
        with _burst_warn_lock:
            if reg_id in _burst_warned_this_session:
                return
            _burst_warned_this_session.add(reg_id)
        _picker_log(
            app,
            f"[picker] Note: "
            f"{chosen.get('display_name', preset)} "
            f"(burst=tight, TPM={tpm}) is your only enabled AI "
            f"provider, so every candidate in a pass of "
            f"{max_per_pass} goes to it and the runner spaces calls "
            f"to stay under its TPM cap. For higher throughput, enable "
            f"a second (generous-tier) provider so the runner can "
            f"rotate across them, or reduce "
            f"queue_runner_max_candidates_per_pass.")
    except Exception:
        # Warning is non-fatal; skip on any error.
        pass


def warn_pinned_tight_burst_once(app) -> None:
    """v4.14.3.8 (2026-05-14): startup hook to warn the user once
    if their cfg['top_ai_override'] pins a tight-burst provider for
    the queue runner's burst pattern. Mirrors _maybe_warn_tight_burst
    but fires at the pin-resolution layer, not at runtime.

    One-time per session per pin value (the same module-level
    _burst_warned_this_session set is reused so a runtime warning
    won't re-fire the startup one and vice versa)."""
    try:
        cfg = getattr(app, 'cfg', {}) or {}
        override = cfg.get('top_ai_override') or ''
        if not override:
            return
        if override in _burst_warned_this_session:
            return
        # Resolve the pin to a provider dict via the same path the
        # picker uses.
        state = _gather_state(app)
        entries = _build_eligible_from_state(state, app)
        match = _resolve_override_in_list(entries, override, app=app,
                                            log=False)
        if match is None:
            return
        if match.get('kind') == 'local':
            return
        import tm_rate_limiter as _trl
        preset = (match.get('id') or '').lower()
        defaults = _trl.PRESET_DEFAULTS.get(preset)
        if not defaults:
            return
        if defaults.get('burst_category') != 'tight':
            return
        max_per_pass = int(
            cfg.get('queue_runner_max_candidates_per_pass', 20))
        if max_per_pass <= 5:
            return
        _picker_log(
            app,
            f"[picker] Pinned provider "
            f"'{match.get('display_name', preset)}' is tight on "
            f"TPM ({defaults.get('tpm')}) for a {max_per_pass}-"
            f"candidate pass. Expect rate limits or rotation. "
            f"Consider switching to a generous-tier provider or "
            f"reducing queue_runner_max_candidates_per_pass.")
        _burst_warned_this_session.add(override)
    except Exception:
        pass


# ─── Diagnostic logging helper ────────────────────────────────────────
#
# Mirrors the _surface_log helper in tm_teacher_intercept. Used to
# replace bare `except Exception: pass` traps so silent failures
# (like the TypeError on no-arg APIProviderRegistry that hid Bug 1)
# show up in the activity log instead of disappearing.

def _resolve_inference_mode(app) -> str:
    """v4.14.5.14-mode-detection-collapse-2c (Ollama exit Phase 2): always
    returns the cloud value 'api'. The former local/hybrid modes are gone —
    candidate enrollment is cloud-only now.

    This is the picker's single mode gate (its sole live caller is
    _gather_state). Returning 'api' unconditionally makes _gather_state's
    `if mode != 'api':` local-Ollama enrollment block UNREACHABLE, so
    state['local_models'] stays [] and every downstream `kind == 'local'`
    branch is dead. Those dead local bodies — and tm_ai.py itself — are
    physically removed in Step 3. Kept callable (signature unchanged) so the
    call site works without edits. Never raises."""
    return 'api'


def _picker_log(app, msg: str, level: str = 'amber') -> None:
    """Best-effort log (default amber). Falls back to stdout if app._log isn't
    callable in the current context. `level` lets low-signal notices (e.g. the
    operational-fit fallback) emit muted instead of shouting in the main log."""
    try:
        log = getattr(app, '_log', None) if app is not None else None
        if callable(log):
            log(msg, level)
            return
    except Exception:
        pass
    print(msg)


# ─── Audit testing note ──────────────────────────────────────────────
#
# When auditing pick_top_ai, instantiate a REAL APIProviderRegistry
# against a temp-file fixture with sample preset data. Do NOT use a
# FakeApp that bypasses the registry entirely — that masks
# integration-level bugs at the registry construction boundary. The
# TypeError that hid Bug 1 (May 2026) was invisible to the Phase 1
# picker audit because the FakeApp had no registry to instantiate;
# the no-arg constructor that was about to TypeError was never
# exercised. The audit added in the May 2026 bugfix session
# (CHECK3-CHECK5 in _audit_picker_fix.py — see git history /
# STATUS.md) is the canonical example of the right pattern: write
# a real providers.json to a temp dir, monkey-patch
# tired_market.DATA_DIR to point at it, then call pick_top_ai
# against an App stub. Same lesson as the audit-testing comment
# near _present_surface in tm_teacher_intercept.py — mock at the
# integration boundary, not above it.


# ─── Public API ───────────────────────────────────────────────────────

def has_any_ai_available(app) -> bool:
    """True if any AI voice (cloud or local) can run right now —
    i.e., pick_top_ai would return success. Cheap to call: shares
    the same gather + filter logic the queue runner uses, without
    actually running the chosen AI.

    Used by features that need to gate on 'AI is available' without
    invoking the picker's choice downstream. Returns False if no
    provider is configured, all are disabled, all are exhausted, OR
    all are in cooldown (v4.14.3.5).

    Delegates to pick_top_ai so the cooldown awareness added in
    v4.14.3.5 is shared — the two functions can never disagree about
    whether AI is available (audit CHECK N+7 enforces this).

    Defensive: if pick_top_ai itself raises (which would indicate a
    bug worth knowing about), the wrapper catches and returns False
    with an amber log. Callers shouldn't crash because the picker
    had an exception — they should treat it as 'no AI available' for
    this gate."""
    try:
        result = pick_top_ai(app)
    except Exception as e:
        _picker_log(
            app,
            f"[teacher-ai] picker: has_any_ai_available wrapper "
            f"caught: {type(e).__name__}: {e}")
        return False
    return bool(result and result.get('success'))


def _resolve_override_in_list(entries: list, override: str,
                                app=None, log: bool = True):
    """v4.14.3.7 (2026-05-14): three-step override resolution.

    1. Match by registry_id (case-sensitive exact). The canonical
       form going forward.
    2. Match by display_name (case-insensitive). Legacy form;
       'Cerebras' / 'Groq' / 'My Mistral' / etc.
    3. Match by id (case-insensitive). Even older legacy form;
       'groq' / 'custom' / 'google'. Note: 'custom' is ambiguous
       in the user's setup (three providers share it), so this step
       returns the first hit AND logs the ambiguity if there's
       more than one match.

    Returns the matched entry dict (with kind/id/registry_id/etc.)
    or None if nothing matches.

    log: if True, emits a muted breadcrumb naming the resolution
    path (id / name / preset). Helpful when the user's saved cfg
    is using a legacy form so they know it still works and what
    to expect going forward."""
    if not override:
        return None
    needle_raw = str(override).strip()
    needle_lower = needle_raw.lower()

    # Step 1: registry_id (case-sensitive).
    match = next(
        (e for e in entries
         if str(e.get('registry_id') or '') == needle_raw),
        None)
    if match is not None:
        return match

    # Step 2: display_name (case-insensitive).
    match = next(
        (e for e in entries
         if str(e.get('display_name') or '').lower() == needle_lower),
        None)
    if match is not None:
        if log:
            _picker_log(
                app,
                f"[picker] override '{override}' matched by display "
                f"name (legacy path); consider updating cfg to use "
                f"the provider's unique id "
                f"'{match.get('registry_id', '?')}'")
        return match

    # Step 3: id / preset (case-insensitive). Ambiguous for
    # 'custom' — there can be multiple. Log the ambiguity.
    matches = [
        e for e in entries
        if str(e.get('id') or '').lower() == needle_lower]
    if matches:
        if log and len(matches) > 1:
            ids = [m.get('display_name') or '?' for m in matches]
            _picker_log(
                app,
                f"[picker] override '{override}' is ambiguous as a "
                f"preset — {len(matches)} providers share it "
                f"({', '.join(ids)}); using the first "
                f"({matches[0].get('display_name')!r}). Update cfg "
                f"to a unique id to disambiguate.")
        elif log:
            _picker_log(
                app,
                f"[picker] override '{override}' matched by preset "
                f"(legacy path); consider updating cfg to use the "
                f"provider's unique id "
                f"'{matches[0].get('registry_id', '?')}'")
        return matches[0]

    return None


def resolve_override_to_registry_id(app, override: str
                                      ) -> Optional[str]:
    """v4.14.3.7 (2026-05-14): translate a (possibly legacy)
    override value to a registry id. Used by the App's startup
    migration to rewrite cfg['top_ai_override'] from preset/name
    forms to the canonical UUID form.

    Returns the registry_id string if the override resolves to a
    known cloud provider, OR returns the override unchanged if it
    matches a local Ollama model (model names ARE the id). Returns
    None if nothing resolves — caller leaves cfg untouched in that
    case (don't silently change a value we can't validate)."""
    if not override:
        return None
    state = _gather_state(app)
    entries = _build_eligible_from_state(state, app)
    match = _resolve_override_in_list(entries, override, app=app,
                                        log=False)
    if match is None:
        return None
    if match.get('kind') == 'local':
        # Local: model name is the id. No translation needed; the
        # canonical override form for locals is the model name.
        return match.get('id')
    rid = match.get('registry_id') or ''
    return rid or None


def pick_top_ai(app, override: Optional[str] = None) -> dict:
    """See module docstring. `override` is a preset id (cloud) or
    model name (local) — typically read from cfg['top_ai_override'].

    Always returns a dict. Check chosen['success'] before reading the
    other fields.

    v4.14.3.5 (2026-05-14): now consults provider cooldowns from
    tm_provider_health. Providers in active cooldown are excluded from
    the eligible set before ranking. New failure reason 'all_cooldown'
    distinguishes 'everyone is rate-limited and waiting' from
    'all_exhausted' (everyone hit their daily cap) and 'all_disabled'
    (everyone toggled off). On all_cooldown, the result dict carries
    `cooldown_remaining_sec` = the smallest cooldown remaining so the
    Teacher AI surface can tell the user how long the wait is.
    """
    state = _gather_state(app)

    all_count = len(state['all_providers'])
    enabled_count = len(state['enabled_providers'])
    local_count = len(state['local_models'])

    # Classification (order matters — check most-fundamental first).
    if all_count == 0 and local_count == 0:
        return {'success': False, 'reason': 'none_configured'}
    if enabled_count == 0 and local_count == 0:
        return {'success': False, 'reason': 'all_disabled'}

    eligible = _build_eligible_from_state(state, app)
    if not eligible:
        # Should be unreachable given the classification above, but
        # guards against unexpected edge cases (e.g., both registry
        # accessors empty after the early-return above passed).
        return {'success': False, 'reason': 'none_configured'}

    # Budget filter. Local entries always pass (no daily cap).
    with_budget = [
        e for e in eligible
        if e['kind'] == 'local' or e.get('remaining', 0) > 0
    ]
    if not with_budget:
        return {'success': False, 'reason': 'all_exhausted'}

    # v4.14.3.5: cooldown filter — exclude providers currently in
    # cooldown from the eligible set. Local entries pass through
    # unchanged (no meaningful cooldown semantics). If after the
    # cooldown filter nothing remains AND the only thing we removed
    # was cooldown (not other failure modes), return 'all_cooldown'
    # with the smallest remaining cooldown so the surface can tell
    # the user when picks will resume.
    with_budget_uncooled = [
        e for e in with_budget
        if e['kind'] == 'local' or not e.get('in_cooldown', False)
    ]
    if not with_budget_uncooled:
        # Every budget-passing entry was cooled — surface the wait.
        # Compute smallest remaining cooldown across the excluded
        # cloud entries; ignore locals (they shouldn't be here unless
        # something put a stale cooldown stamp on a local id, but
        # belt-and-suspenders).
        cooldowns = [
            int(e.get('cooldown_remaining_sec', 0) or 0)
            for e in with_budget
            if e['kind'] == 'cloud'
        ]
        cooldowns = [c for c in cooldowns if c > 0]
        smallest = min(cooldowns) if cooldowns else 0
        return {
            'success': False,
            'reason': 'all_cooldown',
            'cooldown_remaining_sec': smallest,
        }
    # Use the cooldown-filtered set for the rest of the pick logic.
    with_budget = with_budget_uncooled

    # Override path.
    if override:
        # v4.14.3.7 (2026-05-14): override resolution chain:
        #   1. Exact match by registry_id (unique UUID)
        #   2. Case-insensitive match by display_name
        #   3. Case-insensitive match by id (preset)
        # Backward compat with cfg values from pre-v4.14.3.7 sessions
        # that wrote 'groq' / 'Cerebras' instead of the UUID. The
        # migration step in App.__init__ rewrites legacy values to
        # registry IDs on next launch; this resolver handles them
        # gracefully until that runs.
        match = _resolve_override_in_list(
            with_budget, override, app=app)
        if match is not None:
            return {
                'success': True,
                'kind': match['kind'],
                'id': match['id'],
                'registry_id': match.get('registry_id', ''),
                'display_name': match['display_name'],
                'reason': 'user-pinned override',
                'fallback_from': None,
            }
        # Override set but unavailable — distinguish exhausted from
        # not-configured from cooled from the reason text. v4.14.3.5:
        # cooled-down override falls through to auto and reports
        # fallback_from so the indicator can surface why.
        configured_match = _resolve_override_in_list(
            eligible, override, app=app, log=False)
        if configured_match is not None:
            fallback_from = override
            if configured_match.get('in_cooldown'):
                reason_prefix = 'override in cooldown, using auto'
            else:
                reason_prefix = 'override exhausted, using auto'
        else:
            fallback_from = override
            reason_prefix = 'override not configured, using auto'
        auto = _rank_and_pick(app, with_budget)
        if auto is None:
            return {'success': False, 'reason': 'all_exhausted'}
        return {
            'success': True,
            'kind': auto['kind'],
            'id': auto['id'],
            'registry_id': auto.get('registry_id', ''),
            'display_name': auto['display_name'],
            'reason': f"{reason_prefix} ({auto['reason']})",
            'fallback_from': fallback_from,
        }

    # No override — pure auto path.
    auto = _rank_and_pick(app, with_budget)
    if auto is None:
        return {'success': False, 'reason': 'all_exhausted'}
    result = {
        'success': True,
        'kind': auto['kind'],
        'id': auto['id'],
        'registry_id': auto.get('registry_id', ''),
        'display_name': auto['display_name'],
        'reason': auto['reason'],
        'fallback_from': None,
    }
    # v4.14.3.8: surface a one-time-per-session note if a tight-burst
    # provider got picked for a multi-candidate pass.
    # v4.14.5.14-picker-message-clarity: pass the count of eligible
    # cloud providers (the router's rotation pool) so the note only
    # fires when there's exactly one — see _maybe_warn_tight_burst.
    # `with_budget` here is the post-budget, post-cooldown eligible set.
    try:
        _n_cloud_eligible = sum(
            1 for e in with_budget if e.get('kind') == 'cloud')
        _maybe_warn_tight_burst(
            app, result, eligible_cloud_count=_n_cloud_eligible)
    except Exception:
        pass
    return result


# ─── Internal: state gathering ────────────────────────────────────────

def _gather_state(app) -> dict:
    """Read the live registry + local model list once, returning the
    raw inputs the classifier and ranker need. Encapsulates registry
    construction so callers don't have to know the path.

    Returns:
      {
        'all_providers': list of dicts (entire registry, incl. disabled),
        'enabled_providers': list of dicts (just enabled),
        'local_models': list of model name strings (non-'embed'),
      }
    """
    state = {
        'all_providers': [],
        'enabled_providers': [],
        'local_models': [],
    }

    # Build the APIProviderRegistry. The picker needs both .all() (for
    # the all_disabled classification) and .enabled() (for the normal
    # path), so direct construction is cleaner than the App's
    # _load_enabled_api_providers helper (which only returns enabled).
    # We fall through to the helper if construction fails.
    registry_built = False
    try:
        import tm_api_providers as _tmap
        path = None
        try:
            import tired_market as _tm
            path = _tm.DATA_DIR / 'api_providers.json'
        except Exception:
            from pathlib import Path
            path = Path('data') / 'api_providers.json'
        registry = _tmap.APIProviderRegistry(path)
        state['all_providers'] = list(registry.all() or [])
        state['enabled_providers'] = list(registry.enabled() or [])
        registry_built = True
    except Exception as e:
        _picker_log(
            app,
            f"[teacher-ai] picker: registry construction raised: "
            f"{type(e).__name__}: {e}")

    if not registry_built:
        # Fallback: try the App's helper for the enabled list. We
        # lose access to .all() (so all_disabled can't be detected
        # cleanly) but we can still produce a working enabled list.
        try:
            loader = getattr(app, '_load_enabled_api_providers', None)
            if callable(loader):
                state['enabled_providers'] = list(loader() or [])
                # Without registry.all(), assume "all configured are
                # enabled" — collapses the all_disabled detection to
                # nothing, but the picker still works for the common
                # case where the registry built successfully on the
                # primary path above.
                state['all_providers'] = list(
                    state['enabled_providers'])
        except Exception as e:
            _picker_log(
                app,
                f"[teacher-ai] picker: enabled-providers fallback "
                f"raised: {type(e).__name__}: {e}")

    # v4.14.5.14-iso: inference_mode is now authoritative for
    # candidate enrollment. Stored on state so _build_eligible_from_
    # state can also drop CLOUD entries when mode == 'local'.
    mode = _resolve_inference_mode(app)
    state['inference_mode'] = mode

    # Local Ollama models — in 'api' (cloud-only) mode they are NOT
    # enrolled and Ollama is NOT even probed (no wasted HTTP call,
    # and — crucially — the queue runner + has_any_ai_available can
    # no longer silently pick local just because Ollama is running).
    # Silent by design: api is the user's steady state, per-pick logging
    # would be noise; the user-visible signal lives in _run_silent_
    # scan's skip line and the simple absence of local picks.
    # v4.14.5.14-ollama-purge-3c: Ollama retired (cloud-only). The local-model
    # enrollment branch was already unreachable since 2c made the resolver
    # return 'api'; its Ollama import + model-list probe are removed here. No
    # local candidates are ever enrolled — state's 'local_models' stays []
    # (initialized above), so every downstream `kind == 'local'` path is dead.

    return state


def _build_eligible_from_state(state: dict, app) -> list:
    """Convert the gathered state into the eligible-candidate list
    the ranker consumes. Each entry:
      {'kind': 'cloud'|'local', 'id': str, 'display_name': str,
       'remaining': int (cloud only), '_provider_dict': dict (cloud only)}

    Note on 'id': for cloud entries, this is the preset name
    (e.g., "groq"), NOT the registry id (e.g., "b1158f8c"). The two
    are different — the rate limiter keys on the registry id, but
    the picker uses the preset name for display + Wilson-CI lookup.
    Both coexist because peek_remaining_for_provider() takes the
    whole provider dict and looks up the limiter via the registry id
    internally. Future contributors: don't try to query the limiter
    using chosen['id'] directly — it won't match.

    v4.14.3.7 (2026-05-14): each entry now also carries
    'registry_id' — the per-install UUID (e.g. "b1158f8c"). This
    is the field downstream dispatch (queue runner ->
    scan_provider_filter -> router) uses to identify the chosen
    provider unambiguously. The legacy 'id' (preset) is preserved
    for Wilson-CI lookup and display continuity, but every dispatch
    surface that has been routing by preset/name has been migrated
    to registry_id. Without this split, every user whose top-ranked
    provider has preset='custom' (Cerebras, GitHub, SambaNova for
    the user's setup) would silently 20/20-fail every queue runner pass
    because the filter 'custom' wouldn't match against display
    names like 'Cerebras' or 'GitHub'.
    """
    out: list = []

    # Cloud entries from enabled providers.
    try:
        import tm_rate_limiter as _trl
    except Exception:
        _trl = None

    # v4.14.3.5 (2026-05-14): cooldown awareness. Read provider health
    # once per pick call so the picker can exclude cooled-down providers
    # before ranking — instead of letting the picker hand them off to
    # the router only for the router to (correctly) refuse the call
    # and leave the queue runner with no prediction. See the May 14
    # picker-vs-cooldown investigation for the diagnosis.
    _ph_state = None
    try:
        import tm_provider_health as _tph
        _ph_state = _tph.get_state()
    except Exception as e:
        _picker_log(
            app,
            f"[teacher-ai] picker: tm_provider_health.get_state raised: "
            f"{type(e).__name__}: {e}")
        # Falling through with _ph_state=None means we treat all
        # providers as uncooled. That's safe because the router still
        # checks cooldowns at the call layer — worst case the picker
        # picks a cooled-down provider, the router refuses, the queue
        # runner sees None per candidate (v4.14.3.1 amber summary
        # surfaces it). Same failure mode as pre-v4.14.3.5.

    import time as _time
    _now = _time.time()

    # v4.14.5.14-iso: in 'local' mode, cloud providers are NOT
    # enrolled (mirror of api-mode dropping local in _gather_state).
    # 'api' and 'hybrid' enroll cloud as before. Local Ollama is
    # already absent from state in api mode, so no cloud-side guard
    # is needed for that case.
    _cloud_src = ([] if state.get('inference_mode') == 'local'
                  else (state.get('enabled_providers') or []))
    for prov in _cloud_src:
        pid = (prov.get('preset') or prov.get('id') or '').lower()
        if not pid:
            continue
        remaining = 10_000_000  # sentinel: assume plenty if check fails
        if _trl is not None:
            try:
                remaining = _trl.peek_remaining_for_provider(prov)
            except Exception as e:
                _picker_log(
                    app,
                    f"[teacher-ai] picker: peek_remaining for {pid} "
                    f"raised: {type(e).__name__}: {e}")
                # Leave remaining at sentinel — don't block on a
                # quota-check failure.

        # v4.14.3.5: cooldown check. Look up the (provider_id, '*')
        # records in provider_health and find the largest active
        # cooldown remaining across all canonical-model records for
        # this provider (a single provider can have multiple model
        # records, each with its own cooldown).
        in_cd = False
        cd_remaining = 0
        if _ph_state is not None:
            try:
                # tm_provider_health stores per-(provider, canonical_model)
                # records. A single registry-id can have multiple
                # canonical-model records (e.g. 'legacy' + 'meta/llama-
                # 3.1-8b-instruct') each with its own cooldown. Iterate
                # all records via the public .all() accessor, filter
                # to this provider's registry id, and take the LARGEST
                # remaining cooldown — if ANY model record is cooled,
                # the provider is effectively cooled for the queue
                # runner's locked-provider call path.
                prov_id = prov.get('id') or ''
                cd_consec = 0
                for rec in _ph_state.all():
                    if rec.provider_id != prov_id:
                        continue
                    if rec.in_cooldown(_now):
                        rem = rec.cooldown_remaining_sec(_now)
                        if rem > cd_remaining:
                            cd_remaining = rem
                            in_cd = True
                            cd_consec = rec.consecutive_429s
                if in_cd:
                    _picker_log(
                        app,
                        f"[picker] Excluded {prov.get('name') or pid}: "
                        f"in cooldown for another {cd_remaining}s "
                        f"(after {cd_consec} rate-limit"
                        f"{'s' if cd_consec != 1 else ''})")
            except Exception as e:
                _picker_log(
                    app,
                    f"[teacher-ai] picker: cooldown check for {pid} "
                    f"raised: {type(e).__name__}: {e}")
                # Leave in_cd=False — treat as uncooled and let the
                # router catch it at call time. Same fall-through
                # behavior as before this fix; the router's cooldown
                # awareness is still in place.

        out.append({
            'kind': 'cloud',
            'id': pid,
            # v4.14.3.7 (2026-05-14): registry_id is the per-install
            # UUID downstream dispatch matches against. 'id' (preset)
            # stays for Wilson-CI + display continuity.
            'registry_id': prov.get('id') or '',
            'display_name': prov.get('name') or pid.title(),
            'remaining': remaining,
            'in_cooldown': in_cd,
            'cooldown_remaining_sec': cd_remaining,
            '_provider_dict': prov,
        })

    # Local entries.
    for model in state.get('local_models') or []:
        out.append({
            'kind': 'local',
            'id': model,
            # Locals have no registry id — the model name IS the
            # unique identifier. Set registry_id to model for shape
            # consistency so downstream code can read either field
            # without a kind check.
            'registry_id': model,
            'display_name': model,
        })

    return out


# ─── Internal: ranking ────────────────────────────────────────────────

def _rank_and_pick(app, eligible: list) -> Optional[dict]:
    """Rank eligible entries by Wilson-CI lower bound; pick the
    highest. Falls back to the cold-start rule when no entry has
    accuracy data. Returns a partial dict (without 'success' /
    'fallback_from') — caller wraps with the success envelope."""
    if not eligible:
        return None

    # Query Wilson-CI low for each. Returns 0 (no data) by default.
    # v4.14.3.12 (2026-05-15): source_id is the provider's display_name
    # ('Mistral', 'Cerebras', 'GitHub'), NOT the preset name
    # ('mistral', 'custom'). The accuracy writer
    # (tm_source_accuracy.compute_model_accuracy) keys rows by
    # pred.get('model') which carries the display label written by
    # tm_api_providers at base_extra:1658. The seed
    # (tm_source_weights.initialize_source_weights) iterates
    # MODEL_TIERS which is also display-label keyed. Pre-v4.14.3.12
    # this lookup used e['id'] (preset) and never matched any row —
    # the picker fell back to cold-start operational-fit ranking
    # every pass since the accuracy bridge shipped (v4.14.3). With
    # this fix, the 93 decided BUYs already in source_weights start
    # influencing rankings on first launch post-upgrade.
    #
    # Do NOT change to e['registry_id']; registry_id is the dispatch
    # identifier (v4.14.3.7), not the accuracy key. They serve
    # different purposes and should remain separate. See
    # v4.14.3.12_investigation.md for full background.
    #
    # Normalize via .strip() to match the writer's
    # (pred.get('model') or '').strip() at tm_source_accuracy.py:301.
    scored = []
    db_conn = _get_db_conn(app)
    for e in eligible:
        # v4.14.6.111: read accuracy under the SAME canonicalized key the writer
        # stamps. The accuracy bridge keys rows by pred['model'], which is set at
        # prediction time to display_label(provider) = canonicalize_model_label(
        # provider.name). Reading the RAW prov.get('name') here meant a
        # non-canonically-named provider ("My Groq", "Minstral", "Github",
        # "Sambanova") never matched its own rows -> permanently cold-start, never
        # learns to rank it. Mirror the write key via display_label. FAIL-OPEN:
        # any fault / empty result falls back to the raw display_name (prior
        # behavior). Cloud entries carry '_provider_dict'; locals keep raw.
        _src_id = (e.get('display_name') or '').strip()
        try:
            _prov = e.get('_provider_dict')
            if isinstance(_prov, dict):
                import tm_api_providers as _tmap_key
                _canon = (_tmap_key.display_label(_prov) or '').strip()
                if _canon:
                    _src_id = _canon
        except Exception:
            pass  # keep the raw display_name key (unchanged behavior)
        ci_low = _query_wilson_ci_low(db_conn, _src_id)
        scored.append((e, ci_low))

    # If anyone has real data (ci_low > 0), pick highest. Otherwise
    # cold-start fallback.
    with_data = [(e, s) for (e, s) in scored if s and s > 0]
    if with_data:
        with_data.sort(key=lambda pair: pair[1], reverse=True)
        winner = with_data[0][0]
        ci = with_data[0][1]
        return {
            'kind': winner['kind'],
            'id': winner['id'],
            'registry_id': winner.get('registry_id', ''),
            'display_name': winner['display_name'],
            'reason': f"highest Wilson-CI lower bound ({ci}%)",
        }

    # v4.14.3.8 (2026-05-14): cold-start strategy now considers
    # operational fit. Pre-v4.14.3.8 the chain was:
    #   Groq if present -> first cloud alphabetical by preset
    #   -> first local alphabetical
    # This baked Groq in as the default and broke for users without
    # Groq configured. New strategy:
    #   1. Group eligible providers by burst_category. Prefer
    #      'generous' > 'moderate' > 'tight'.
    #   2. Within the same burst category, alphabetical by
    #      display name (NOT preset). So the user's three preset='custom'
    #      providers sort by 'Cerebras' < 'GitHub' < 'SambaNova'.
    #   3. Local Ollama always treated as 'generous'. Within local,
    #      alphabetical by model name.
    #   4. Missing burst_category defaults to 'moderate' (the
    #      cautious unknown-tier choice).
    #   5. If NO provider has metadata at all (e.g., legacy custom
    #      preset entries with no defaults), fall back to today's
    #      alphabetical-by-preset for backward compat.
    try:
        import tm_rate_limiter as _trl_for_burst
        _get_burst = _trl_for_burst.get_burst_category
    except Exception:
        _get_burst = lambda _prov: 'moderate'  # noqa: E731

    _BURST_ORDER = {'generous': 0, 'moderate': 1, 'tight': 2}

    def _burst_for_entry(entry):
        if entry.get('kind') == 'local':
            return 'generous'
        prov = entry.get('_provider_dict') or {}
        return _get_burst(prov)

    def _display_name_for_sort(entry):
        return (entry.get('display_name') or '').lower()

    # Sort eligible by (burst_priority, display_name).
    ranked_by_fit = sorted(
        eligible,
        key=lambda e: (
            _BURST_ORDER.get(_burst_for_entry(e), 1),
            _display_name_for_sort(e),
        ))

    if ranked_by_fit:
        chosen = ranked_by_fit[0]
        burst = _burst_for_entry(chosen)
        _name = chosen.get('display_name', '?')
        # v4.14.6.111: reword + dedup. This is a graceful fallback (rank by
        # operational fit while decided-BUY accuracy is still accruing), NOT a
        # failure — drop the alarming "cold-start / no accuracy data yet"
        # phrasing, emit muted, and only once per provider per session (was once
        # per dispatch burst). Selection is unchanged.
        try:
            with _burst_warn_lock:
                _first = _name not in _coldstart_noted_this_session
                if _first:
                    _coldstart_noted_this_session.add(_name)
            if _first:
                _picker_log(
                    app,
                    f"[picker] ranking by operational fit — accuracy still "
                    f"accruing ({_name}, burst={burst})",
                    'muted')
        except Exception:
            pass
        return {
            'kind': chosen['kind'],
            'id': chosen['id'],
            'registry_id': chosen.get('registry_id', ''),
            'display_name': chosen['display_name'],
            'reason': (
                f"ranking by operational fit — accuracy still accruing "
                f"(burst={burst})"),
        }
    return None


def _get_db_conn(app):
    """Return the app's SQLite connection, or None if unavailable.
    Different attribute paths in the app for legacy reasons —
    try the most common ones."""
    if app is None:
        return None
    for attr in ('db', '_db', 'database'):
        db = getattr(app, attr, None)
        if db is not None:
            conn = getattr(db, 'conn', None)
            if conn is not None:
                return conn
    return None


def _query_wilson_ci_low(conn, source_id: str) -> int:
    """Look up the (source_id, '__global__', '__global__') row in
    source_weights and return confidence_band_low. Returns 0 when no
    data or query fails — interpreted as cold-start by callers."""
    if conn is None or not source_id:
        return 0
    try:
        cur = conn.execute(
            "SELECT confidence_band_low FROM source_weights "
            "WHERE source_id = ? AND context_id = ? AND ticker = ? "
            "LIMIT 1",
            (source_id, GLOBAL_KEY, GLOBAL_KEY),
        )
        row = cur.fetchone()
        if row is None:
            return 0
        val = row[0]
        if val is None:
            return 0
        return int(val)
    except Exception:
        return 0
