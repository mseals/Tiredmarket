"""tm_teacher_intercept — Teacher AI MVP Session 3: click intercept layer.

Wraps toolbar button handlers with prereq checks. When a user clicks an
intercepted button and any prereq fails, Teacher AI surfaces a contextual
message from `data/internal/error_recovery_playbook.json` instead of
invoking the handler. When all prereqs pass, the original handler runs
unchanged.

Public API:
    register_intercept(button_widget, action_id, app_ref)
        Wrap a button's existing command with prereq checking. Idempotent
        per button (second call on the same widget is a no-op).

    check_prereqs(prereq_ids, app_ref) -> str | None
        Run the listed prereqs and return the id of the first one that
        fails, or None if all pass.

    execute_offered_action(action_dict, app_ref)
        Execute a single offered_action from the playbook (open_provider_setup,
        open_choices, start_scan, use_default_style, open_url, dismiss,
        dismiss_and_suppress).

Per design/teacher_ai_mvp.md "Click interception (event-level wrapping)".
"""
from __future__ import annotations

import json
import re
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable, Optional


# ─── Action display labels ────────────────────────────────────────────
#
# Stable mapping from action_id (used in playbook triggers and
# register_intercept calls) to the display label shown to the user
# in {action} token substitutions. Every action_id appearing in any
# playbook entry's trigger.actions array must have an entry here.
# Token substitution (Step 4) reads from this table; entries without
# a label fall through to the action_id literal.
ACTION_LABELS: dict = {
    'verify': 'Verify',
    'recommend': 'Recommend',
    'look_up': 'Look Up',
    'ai_chat': 'AI Chat',
}


# ─── Playbook + features loading (lazy, cached at module level) ───────
#
# New-schema (v4.15.0+) playbook indexes. The file uses a flat
# entries[] array; the loader builds two lookup dicts from it.

# (action_id, prereq_id) tuple → full entry dict. For action_prereq
# triggers. An entry whose trigger.actions lists multiple actions
# expands into one index slot per action.
_INDEX_ACTION_PREREQ: dict = {}

# action_id → ordered list of prereq_ids. Order is taken from the
# entry order in error_recovery_playbook.json — authors order entries
# by importance, most fundamental first, and the first-fail semantics
# in check_prereqs() use that ordering.
_INDEX_ACTION_TO_PREREQS: dict = {}

# event_id → entry dict. For system_event triggers. Loaded eagerly
# for Phase 2 use; Phase 1 does not surface system_event entries.
_INDEX_SYSTEM_EVENT: dict = {}

# Session-only cooldown tracker for system_event entries (Phase 2).
# Keyed by entry_id, value is the unix timestamp of the last surface.
# A 5-minute cooldown prevents intercept-spam when an event fires
# repeatedly (e.g., the same provider rate-limit hitting on every
# retry). Tutorial mode (Ctrl+Shift+`) bypasses this just like it
# bypasses the persistent action_prereq suppressions.
#
# Phase 1 scaffold only — no reads or writes from current code paths.
# Phase 2 wires emit_system_event to consult this dict before
# surfacing.
_session_event_cooldowns: dict = {}

# v4.14.5.14a.5 Component D: popup keys already shown THIS session
# (process-lifetime; clears on restart). One popup per key per
# session. cfg['use_session_popup_dedupe'] (default True) gates it.
_session_popup_shown: set = set()


def _popup_should_show(app, key: str) -> bool:
    """True the first time `key` is seen this session; False on every
    repeat. Flag-gated by cfg['use_session_popup_dedupe'] (default
    True; False = always show, the v4.14.5.14.4 behaviour). Resets on
    restart because the set is in-memory."""
    try:
        enabled = bool((getattr(app, 'cfg', {}) or {}).get(
            'use_session_popup_dedupe', True))
    except Exception:
        enabled = True
    if not enabled:
        return True
    if not key:
        return True
    if key in _session_popup_shown:
        return False
    _session_popup_shown.add(key)
    return True

# (action_id, prereq_id) → entry dict, for entries with category
# action_degraded. Loaded eagerly for Phase 3 use; Phase 1 excludes
# these from the active action_prereq index since they're inherently
# post-flight events (the action proceeds, the entry just adds a
# friendlier explanation of what happened).
_INDEX_ACTION_DEGRADED: dict = {}

# action_id → ordered list of (entry, observation_id) tuples, for
# entries with trigger.kind == "post_action_observation". Order is
# taken from entry order in the playbook file. Pass B uses this in
# observe_action() to walk all observations matching an action and
# fire the first one whose predicate returns True.
_INDEX_ACTION_TO_OBSERVATIONS: dict = {}

# Three-state load flag: None = not yet attempted, True = success,
# False = load failed (missing / unparseable / wrong schema_version).
# On False, the intercept layer is a transparent passthrough.
_PLAYBOOK_LOADED: Optional[bool] = None

_FEATURES: Optional[dict] = None

# Phase 2: the registered app reference, used by emit_system_event
# when callers can't pass `app` explicitly (e.g., background workers
# deep in the cache layer that have no app handle in scope). Set
# once at app init via register_app(self). Stays None until the app
# calls register_app — emits before that point are no-ops, which is
# the right behavior at module-import time.
_registered_app: Any = None


def register_app(app) -> None:
    """Register the App reference for background emits. Call once
    during app init; subsequent calls overwrite. emit_system_event
    falls back to this when called without an explicit app arg
    (typical from non-app worker threads)."""
    global _registered_app
    _registered_app = app


def _data_dir() -> Path:
    return __import__('tm_paths').get_app_asset_dir() / 'internal'


def _load_playbook() -> bool:
    """Load error_recovery_playbook.json (new schema, v4.15.0+) and
    populate the in-memory indexes. Idempotent — repeat calls reuse
    the cached load result.

    Returns True on success, False on any error. On False the indexes
    stay empty; register_intercept / gate_call then behave as
    transparent passthroughs (no prereq checks, no surfaces, original
    handlers fire unchanged).
    """
    global _PLAYBOOK_LOADED
    if _PLAYBOOK_LOADED is not None:
        return _PLAYBOOK_LOADED

    try:
        p = _data_dir() / 'error_recovery_playbook.json'
        with open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        # No app ref at module-import time — stdout is the only sink.
        print(f"[teacher-ai] playbook load failed: {e}")
        _PLAYBOOK_LOADED = False
        return False

    sv = data.get('schema_version')
    if sv != 1:
        print(f"[teacher-ai] playbook schema_version {sv!r} not "
              f"supported (expected 1). Intercept layer disabled.")
        _PLAYBOOK_LOADED = False
        return False

    entries = data.get('entries') or []
    for entry in entries:
        trig = entry.get('trigger') or {}
        kind = trig.get('kind')
        category = entry.get('category') or ''
        if kind == 'action_prereq':
            prereq = trig.get('prereq')
            actions = trig.get('actions') or []
            if not prereq or not actions:
                continue
            # action_degraded entries are inherently post-flight —
            # they describe what happened during a scan, not what
            # blocked it. Phase 1 excludes them from the pre-flight
            # index. They go into _INDEX_ACTION_DEGRADED for Phase 3.
            if category == 'action_degraded':
                for action_id in actions:
                    _INDEX_ACTION_DEGRADED[(action_id, prereq)] = entry
                continue
            for action_id in actions:
                _INDEX_ACTION_PREREQ[(action_id, prereq)] = entry
                lst = _INDEX_ACTION_TO_PREREQS.setdefault(action_id, [])
                if prereq not in lst:
                    lst.append(prereq)
        elif kind == 'system_event':
            event_id = trig.get('event')
            if event_id:
                _INDEX_SYSTEM_EVENT[event_id] = entry
        elif kind == 'post_action_observation':
            observation = trig.get('observation')
            actions = trig.get('actions') or []
            if not observation or not actions:
                continue
            for action_id in actions:
                lst = _INDEX_ACTION_TO_OBSERVATIONS.setdefault(
                    action_id, [])
                lst.append((entry, observation))

    _PLAYBOOK_LOADED = True
    return True


def _get_prereqs_for_action(action_id: str) -> list:
    """Ordered list of prereq_ids associated with an action. Order is
    derived from entry order in the playbook file. Empty list if no
    entries reference this action (or if the playbook failed to load,
    in which case the wrapper degrades to a passthrough)."""
    if _PLAYBOOK_LOADED is None:
        _load_playbook()
    return list(_INDEX_ACTION_TO_PREREQS.get(action_id) or [])


def _get_entry_for(action_id: str, prereq_id: str) -> Optional[dict]:
    """Lookup the playbook entry for an (action_id, prereq_id) pair.
    Returns None if no matching entry exists."""
    if _PLAYBOOK_LOADED is None:
        _load_playbook()
    return _INDEX_ACTION_PREREQ.get((action_id, prereq_id))


def _load_features() -> dict:
    global _FEATURES
    if _FEATURES is not None:
        return _FEATURES
    try:
        p = _data_dir() / 'features.json'
        with open(p, 'r', encoding='utf-8') as f:
            _FEATURES = json.load(f)
    except Exception:
        _FEATURES = {'schema_version': 1, 'entries': []}
    return _FEATURES


# ─── Teacher AI availability ──────────────────────────────────────────
#
# tm_teacher_ai is the module that renders the rich on-canvas surface.
# When it isn't importable (older builds, custom installs that strip
# the Teacher AI module), the intercept layer falls back to a terse
# messagebox using the entry's short_hint + manual_fallback.

_TEACHER_AI_AVAILABLE: Optional[bool] = None


def is_teacher_ai_available() -> bool:
    """True if tm_teacher_ai imports successfully. Result is memoized
    on first call — the import outcome doesn't change at runtime."""
    global _TEACHER_AI_AVAILABLE
    if _TEACHER_AI_AVAILABLE is not None:
        return _TEACHER_AI_AVAILABLE
    try:
        import tm_teacher_ai  # noqa: F401
        _TEACHER_AI_AVAILABLE = True
    except Exception:
        _TEACHER_AI_AVAILABLE = False
    return _TEACHER_AI_AVAILABLE


# ─── Phase 1 message + offered-action composition ─────────────────────
#
# These helpers map the new-schema entry shape onto the surface format
# tm_teacher_ai.show_center expects. Token substitution arrives in
# Step 4; for now diagnostic/recommendation render verbatim, so any
# {action} or {provider} tokens display literally. The offered_action
# field's {label, intent} object is shimmed to the existing
# execute_offered_action dispatch shape — Step 5 restructures the
# dispatcher to consume intents directly.

_TOKEN_PATTERN = re.compile(r'\{(\w+)\}')


def _render(text: str, context: dict,
            entry_id: str = '', app_ref=None) -> str:
    """Substitute {token} patterns in `text` using `context` values.

    Phase 1 supports {action}. {provider} is system_event territory
    (Phase 2); if a Phase-1 entry contains it, the literal {provider}
    passes through and a warning fires.

    Unresolved tokens (not present in context) are content bugs by
    contract — they pass through as literal `{token}` text AND fire
    a warning to the app's activity log (or stdout if no app ref is
    available). Never silent.
    """
    if not text:
        return text or ''

    def _sub(m):
        token = m.group(1)
        if token in context:
            return str(context[token])
        _warn_unresolved_token(token, entry_id, app_ref)
        return m.group(0)  # the literal "{token}"

    return _TOKEN_PATTERN.sub(_sub, text)


def _warn_unresolved_token(token: str, entry_id: str, app_ref) -> None:
    """Log an unresolved-token warning. Tries the app's activity log
    first (amber severity); falls back to stdout when no app ref is
    available (e.g., module-import-time errors)."""
    msg = (f"[teacher-ai] unresolved token "
           f"'{{{token}}}' in entry "
           f"{entry_id or '<unknown>'}")
    if app_ref is not None:
        try:
            log = getattr(app_ref, '_log', None)
            if callable(log):
                log(msg, 'amber')
                return
        except Exception:
            pass
    print(msg)


def _compose_message(entry: dict, action_id, app_ref=None,
                     context: Optional[dict] = None) -> str:
    """Build the surface body from a playbook entry. Renders diagnostic
    and recommendation with {action} / {provider} token substitution
    via _render. Tokens that can't be filled at this surface (e.g.,
    {provider} on an action_prereq trigger that has no provider in
    scope) pass through as literals and the renderer logs a warning.

    action_id: the user-facing action that triggered this surface
    (looked up in ACTION_LABELS for {action}). Pass None for
    system_event flows where there's no user-action context.

    context: optional dict of additional substitutions for tokens
    beyond {action}. Phase 2 uses this for {provider} on
    system_event flows. Caller passes
    {'provider': 'Groq'} (or whichever)."""
    if not entry:
        return _fallback_message('unknown')
    entry_id = entry.get('entry_id') or ''
    # Build the substitution context. {action} comes from action_id
    # via ACTION_LABELS (omitted when action_id is None or unmapped
    # — the renderer will log an unresolved-token warning if a
    # template references {action} without a value).
    base_context: dict = {}
    if action_id and action_id in ACTION_LABELS:
        base_context['action'] = ACTION_LABELS[action_id]
    elif action_id:
        # Fallback: use the literal action_id as the label.
        base_context['action'] = action_id
    if context:
        base_context.update(context)
    diag_raw = entry.get('diagnostic') or ''
    rec_raw = entry.get('recommendation') or ''
    diag = _render(diag_raw, base_context,
                   entry_id=entry_id, app_ref=app_ref)
    rec = _render(rec_raw, base_context,
                  entry_id=entry_id, app_ref=app_ref)
    parts = [s for s in (diag, rec) if s]
    if not parts:
        # Last-resort fallback — should never trigger with the
        # current 30 entries, but guards against partial entries.
        # short_hint is intentionally NOT token-rendered (per the
        # schema contract: short_hint never contains tokens).
        return entry.get('short_hint') or _fallback_message('unknown')
    return "\n\n".join(parts)


def _entry_is_blocking(entry) -> bool:
    """v4.14.5.40-live-gate: True for a HARD floor — an action that genuinely
    cannot run (playbook severity 'blocking' / category 'action_blocked',
    e.g. no_ai_voices_configured). Blocking floors are NON-SUPPRESSIBLE and
    NON-BYPASSABLE: their steer carries only the constructive action (no
    Dismiss / Don't-show-again), and a stale suppression marker must never
    let the broken action run."""
    if not isinstance(entry, dict):
        return False
    return (entry.get('severity') == 'blocking'
            or entry.get('category') == 'action_blocked')


def _compose_offered_action_dicts(entry: dict,
                                  is_blocking_floor: bool = False) -> list:
    """Convert the entry's offered_action (singular, optional) into
    the list-of-dicts format _present_surface expects. Appends
    Dismiss + Don't show again so the user can close the surface —
    EXCEPT for a blocking floor (is_blocking_floor=True), which carries
    ONLY its constructive action.

    v4.14.5.40-live-gate: a blocking demand-steer (action genuinely can't
    run, e.g. "no AI connected") must not be silenceable — letting the user
    "Don't show again" the one steer that unblocks them, or "Dismiss" it into
    a permanent suppression, defeats the gate. Esc / click-outside still
    dismiss the bubble (tm_teacher_ai._wire_dismissal), so dropping the
    buttons never traps the user. Only the action_prereq gate passes
    is_blocking_floor=True; system_event / coaching callers keep the default
    (False) so their surfaces are unchanged.

    The intent string flows directly through as the dict's 'action'
    field. execute_offered_action's _INTENT_DISPATCH table maps it
    to the actual callable."""
    result = []
    oa = entry.get('offered_action') if isinstance(entry, dict) else None
    if isinstance(oa, dict):
        label = oa.get('label') or 'OK'
        intent = oa.get('intent') or ''
        if intent:
            result.append({'label': label, 'action': intent})
    if is_blocking_floor:
        # Hard floor: no Dismiss, no Don't-show-again. The action can't run
        # without the prereq; silencing the steer would only hide the one
        # path that fixes it. Esc / click-away still close the surface.
        return result
    result.append({'label': 'Dismiss', 'action': 'dismiss'})
    # Coaching entries skip "Don't show again" by design — the
    # trigger condition is the implicit suppression key (when the
    # condition becomes false, the surface stops naturally; when it
    # becomes true again later, it should re-fire). Persistent
    # suppression on coaching risks users dismissing tips they
    # later need.
    if isinstance(entry, dict) and entry.get('category') != 'coaching':
        result.append({'label': "Don't show again",
                       'action': 'dismiss_and_suppress'})
    return result


# ─── Prereq check primitives ─────────────────────────────────────────

def _check_at_least_one_ai_provider(app) -> bool:
    """True if at least one cloud AI provider is configured + enabled."""
    try:
        state = getattr(app, 'state', None)
        if state is not None:
            try:
                return bool(state.has_providers_configured())
            except Exception:
                pass
        loader = getattr(app, '_load_enabled_api_providers', None)
        if callable(loader):
            return bool(loader())
    except Exception:
        pass
    return False


def _check_trading_style_selected(app) -> bool:
    """True if cfg['v415_style'] is set to a non-empty value.

    Mostly defensive — Step 22a-cleanup's silent defaults set this to
    'moderate' on fresh installs, so the check almost always passes in
    normal use. Guards against pre-22a installs and config corruption.
    """
    try:
        return bool(app.cfg.get('v415_style'))
    except Exception:
        return False


def _check_at_least_one_open_buy_prediction(app) -> bool:
    """True if PredictionsLog has at least one open BUY prediction.

    Builds holdings_state lazily if it hasn't been built — matches what
    _show_recommendations does on real entry, so we're not creating new
    side effects, just shifting the build moment slightly earlier.
    """
    try:
        if getattr(app, '_holdings_state', None) is None:
            try:
                app._holdings_state = app._build_holdings_state()
            except Exception:
                return False
        plog = (app._holdings_state or {}).get('predictions_log')
        if plog is None:
            return False
        try:
            opens = plog.get_open()
        except Exception:
            return False
        for p in opens:
            d = (p.get('direction') or '').upper()
            if d == 'BUY':
                return True
        return False
    except Exception:
        return False


# _check_ollama_installed REMOVED in v4.14.5.14-ollama-purge-step4: Ollama is
# retired (cloud-only). Its 'ollama_installed' prereq + the setting_ai_model
# feature that used it are removed from features.json. (Ollama client deleted.)


# ─── Phase 1 prereq predicates (new playbook) ─────────────────────────
#
# Each function takes the App reference and returns bool. False means
# "prereq is not met, fire the intercept." True means "prereq met,
# action proceeds." When a check can't decide (e.g., a module import
# failed for an unrelated reason), the convention is to return True
# rather than block — over-warning is worse than under-warning for
# orthogonal failures.

def _check_at_least_one_voice(app) -> bool:
    """True if any AI voice is available. v4.14.5.14-ollama-purge-step4: Ollama
    retired (cloud-only) — an AI voice is now exactly a configured cloud
    provider, so this is just the cloud-provider check (the old local-Ollama
    fallback always returned False on this setup and was removed)."""
    return _check_at_least_one_ai_provider(app)


def _noai_board_passthrough(app) -> bool:
    """v4.14.6.81-noai-board-works: True when the user is running WITHOUT an
    AI by choice — either the Piece-1 opt-out flag (cfg
    ['teacher_ai_allow_no_provider'], set by "Continue without an AI") OR the
    resolved tier-1 mode is 'algo_only' (automatic with zero providers, or an
    explicit algo_only setting). In that state the `at_least_one_voice` floor
    must NOT wall the action: Recommend should open the algorithm-driven
    board, and Look Up should reach its own v4.14.6.80 "this uses an AI"
    explanation at its refusal site (a single, correct surface — never a
    second wall stacked on top of it).

    STRICTLY no-AI-gated: when an AI mode is active or providers are present
    ('automatic' with >=1 provider resolves to 'ai_plus_algo'), this returns
    False and the gate behaves EXACTLY as before. Lazy-imports the resolver
    to avoid a module-load circular import with tm_queue_runner."""
    try:
        cfg = getattr(app, 'cfg', {}) or {}
        if bool(cfg.get('teacher_ai_allow_no_provider', False)):
            return True
        try:
            import tm_queue_runner as _qr
            if _qr._resolve_tier1_mode(cfg) == 'algo_only':
                return True
        except Exception:
            pass
    except Exception:
        pass
    return False


def _check_ai_not_paused(app) -> bool:
    try:
        import tm_holdings
        return not tm_holdings.is_ai_paused()
    except Exception:
        # If we can't check, assume not paused — don't block on a
        # failure orthogonal to the actual prereq.
        return True


def _check_no_concurrent_scan(app) -> bool:
    """True if no scan is in flight. Reads _discover_running and
    _consensus_running flags on the HoldingsWindow (mirrors the
    guards at tm_holdings.py:3034 and tm_holdings.py:3562). If the
    window hasn't been built yet, no scan can be running."""
    try:
        hw = getattr(app, '_holdings_window', None)
        if hw is None:
            return True
        return not (getattr(hw, '_discover_running', False)
                    or getattr(hw, '_consensus_running', False))
    except Exception:
        return True


def _check_consensus_models_configured(app) -> bool:
    try:
        models = list(app.cfg.get('consensus_models', []) or [])
        return len(models) > 0
    except Exception:
        return False


def _check_configured_models_installed(app) -> bool:
    """True if at least one configured consensus model is actually
    present in Ollama. Mirrors the filter at tm_holdings.py:3072-3099."""
    try:
        configured = set(app.cfg.get('consensus_models', []) or [])
        if not configured:
            return False
        # v4.14.5.14-ollama-purge-step4: Ollama retired — no local model list.
        # (Unused by the live playbooks; kept registered + behavior-neutral.)
        installed = set()
        return bool(configured & installed)
    except Exception:
        return False


def _check_two_or_more_models_available(app) -> bool:
    """True if 2+ configured-AND-installed models are available for
    consensus. Mirrors tm_holdings.py:3101-3109."""
    try:
        configured = set(app.cfg.get('consensus_models', []) or [])
        if not configured:
            return False
        # v4.14.5.14-ollama-purge-step4: Ollama retired — no local model list.
        # (Unused by the live playbooks; kept registered + behavior-neutral.)
        installed = set()
        return len(configured & installed) >= 2
    except Exception:
        return False


def _check_no_active_cooldown(app) -> bool:
    """True if no rate-limit cooldown is active. Wraps
    tm_discover.get_cooldown_status()."""
    try:
        import tm_discover
        cd = tm_discover.get_cooldown_status() or {}
        return not cd.get('active', False)
    except Exception:
        # No tm_discover available, no cooldown to worry about.
        return True


def _check_predictions_log_loadable(app) -> bool:
    """True if holdings state builds and predictions_log is non-None.
    Same lazy-build pattern as _check_at_least_one_open_buy_prediction
    so we don't introduce a new side effect."""
    try:
        if getattr(app, '_holdings_state', None) is None:
            try:
                app._holdings_state = app._build_holdings_state()
            except Exception:
                return False
        plog = (app._holdings_state or {}).get('predictions_log')
        return plog is not None
    except Exception:
        return False


def _check_universe_non_empty(app) -> bool:
    """Pre-flight check: True unless the holdings_window has tried to
    populate its universe and got nothing back. Universe == None means
    'not yet fetched' which is fine (it'll be fetched on demand);
    universe == [] means a fetch happened and produced nothing."""
    try:
        hw = getattr(app, '_holdings_window', None)
        if hw is None:
            return True
        u = getattr(hw, 'universe', None)
        if u is None:
            return True
        return len(u) > 0
    except Exception:
        return True


def _check_path_universe_compatible(app) -> bool:
    """Pre-flight sanity check: True if an analysis_path is set in
    config. The deeper compatibility check (would this path × universe
    combination produce any candidates?) lives at tm_holdings.py:3612-3623
    and runs post-flight; that surface comes via system_event in
    Phase 3. Pre-flight we only verify path is configured."""
    try:
        return bool(app.cfg.get('analysis_path'))
    except Exception:
        return True


def _check_recommend_module_loaded(app) -> bool:
    try:
        import tm_recommend  # noqa: F401
        return True
    except Exception:
        return False


def _check_ai_module_loaded(app) -> bool:
    # v4.14.5.14-ollama-purge-step4: tm_ai (the Ollama client) was retired and
    # is deleted in the final purge patch. The "AI module" is now the cloud
    # engine — probe tm_holdings (the analysis surface) instead, which is the
    # module the 6 features gated on 'ai_module_loaded' actually need.
    try:
        import tm_holdings  # noqa: F401
        return True
    except Exception:
        return False


# _check_ollama_reachable REMOVED in v4.14.5.14-ollama-purge-step4: Ollama is
# retired (cloud-only). Its 'ollama_reachable' prereq + the error_recovery entry
# that used it are removed from error_recovery_playbook.json.


def _check_ticker_recognized(app) -> bool:
    """PHASE 1 NOTE: ticker_recognized is logically a post-submission
    check — the user hasn't typed a ticker when the Look Up button is
    clicked, so the pre-flight predicate has nothing to validate. It
    always returns True. The real failure detection happens at
    submission time in tm_holdings.py:4019 and is wired in Phase 2 via
    emit_system_event (see the lookup_ticker_not_recognized entry's
    wiring_note in error_recovery_playbook.json)."""
    return True


def _check_network_online(app) -> bool:
    """Wraps tm_network's cached is_online() probe (30s TTL — free
    in the steady state)."""
    try:
        import tm_network
        return bool(tm_network.is_online())
    except Exception:
        # Without tm_network, assume online — don't block.
        return True


# ─── Phase B observation predicates (post_action_observation) ─────────
#
# Result-dict contract for observe_action(app, action_id, result):
#
#   'look_up'   : {'on_demand_fetch': bool,
#                  'ticker': str,
#                  'cache_hit_count': int (optional)}
#   'recommend' : {} (no result data needed; observations query
#                  system state directly)
#   'verify'    : {'direction': str,             # documented but
#                  'from_queue': bool,           # stubbed until
#                  'voices_skipped': list[str],  # Verify ships
#                  'voice_breakdown': dict[str, str]}
#
# Predicate signature: (app, result_dict_or_none) -> bool. False on
# uncertainty (don't surface coaching when we can't decide). True
# triggers the entry to surface in observe_action's first-fire walk.
# Stubbed predicates always return False with an explicit grep-able
# `# STUB:` comment naming the feature dependency they're waiting on.

def _obs_one_voice_only(app, result) -> bool:
    """True when the user has exactly one AI voice configured (cloud
    or local). Pure system-state check — ignores result_dict.

    Counts cloud providers via _check_at_least_one_ai_provider's
    underlying loader, plus the count of local Ollama models. Returns
    True only when total == 1 (zero is handled by no_ai_voices_configured;
    two or more means real consensus is possible)."""
    cloud_count = 0
    try:
        loader = getattr(app, '_load_enabled_api_providers', None)
        if callable(loader):
            cloud_count = len(loader() or [])
        else:
            state = getattr(app, 'state', None)
            if state is not None:
                cloud_count = (1 if bool(
                    state.has_providers_configured()) else 0)
    except Exception:
        cloud_count = 0
    # v4.14.5.14-ollama-purge-step4: Ollama retired (cloud-only) — no local
    # voices. "One voice only" now means exactly one configured cloud provider.
    local_count = 0
    return (cloud_count + local_count) == 1


def _obs_cache_thin_initial_fill(app, result) -> bool:
    """True when the data cache is still in its initial-fill phase —
    i.e. the user just ran Verify or Look Up while the pick-relevant
    data isn't current yet, and the AI reasoned over a thinner
    snapshot than it normally would. Gates the soft-warn surface
    `cache_warming_up_initial_fill` ("results may improve once the
    cache is full").

    v4.14.5.87-readiness-snapshot: this predicate was previously
    dead — it called `tm_cache.get_fill_progress()` with no args,
    but the function requires `(lane, scope_tickers)`. The call
    raised, was caught, and the predicate returned False
    unconditionally. The coaching surface NEVER fired.

    Now reads the new readiness snapshot. Predicate is True iff the
    overall ready verdict is False AND pick-relevant daily_bars
    coverage is meaningfully thin (<50%). The double gate avoids
    spamming users whose news priority is incomplete but whose
    price data IS in (a state where the surface's "thinner than
    you'll have" framing would be misleading).

    Side-effect noted in build report: this revives a soft-warn
    coaching popup that hasn't fired since the predicate broke.
    Behavior is unchanged from the ORIGINAL intent (fire when cache
    is thin during initial fill). 5-min cooldown + per-session
    popup dedup gate the surface from spamming."""
    try:
        import tm_readiness
        snap = tm_readiness.get_readiness_snapshot(app)
    except Exception:
        return False
    if not isinstance(snap, dict):
        return False
    # Already ready → not "thin," not the coaching moment.
    if bool(snap.get('ready')):
        return False
    db_lane = (snap.get('lanes') or {}).get('daily_bars') or {}
    pct = db_lane.get('pct')
    # No pct (early startup, empty scope) → don't fire the surface;
    # we can't honestly claim the cache is "thin" if we have no
    # measurement.
    if pct is None:
        return False
    try:
        return float(pct) < 50.0
    except (TypeError, ValueError):
        return False


def _obs_ticker_not_in_cache(app, result) -> bool:
    """True when Look Up triggered an on-demand fetch (the ticker
    wasn't in the cache and we had to go fetch it). Read from the
    result_dict's 'on_demand_fetch' flag."""
    if not isinstance(result, dict):
        return False
    return bool(result.get('on_demand_fetch'))


def _obs_voices_skipped_during_verify(app, result) -> bool:
    """Live as of Phase 2. True when Verify completed but at least
    one configured AI didn't return a vote (rate limit, error, etc.)."""
    if not isinstance(result, dict):
        return False
    skipped = result.get('voices_skipped') or []
    return len(skipped) > 0


def _obs_consensus_avoid_on_queue_pick(app, result) -> bool:
    """Live as of Phase 2. True when Verify on a continuous-queue
    pick returns AVOID — the queue-pop trigger moment (currently
    surfaces as the in-place AVOID badge per the user's design call;
    auto-rotate is deferred to Phase 3)."""
    if not isinstance(result, dict):
        return False
    return (bool(result.get('from_queue'))
            and (result.get('direction') or '').upper() == 'AVOID')


def _obs_consensus_split_no_majority(app, result) -> bool:
    """Live as of Phase 2. True when Verify completed with split
    votes and no direction has a strict majority (>50%) of the
    voices that responded."""
    if not isinstance(result, dict):
        return False
    breakdown = result.get('voice_breakdown') or {}
    if not breakdown:
        return False
    from collections import Counter
    counts = Counter(breakdown.values())
    total = sum(counts.values())
    if total == 0:
        return False
    top_count = counts.most_common(1)[0][1]
    return top_count <= (total // 2)


def _any_populated_path(app) -> bool:
    """v4.14.5.51-recs-gatekeeping: True if ANY trading style has a displayable
    pick (a recommend_cache displayed-tier row). Cheap; never raises."""
    try:
        import tm_queue_runner as _qr
        counts = _qr._recommend_cache_counts(app) or {}
        for dv in counts.values():
            d = dv[0] if isinstance(dv, (tuple, list)) else 0
            if d and int(d) > 0:
                return True
    except Exception:
        return False
    return False


def _obs_queue_empty_initial_fill(app, result) -> bool:
    """Live as of Phase 2. True when recommend_queue is empty AND the
    background data fill has NOT been confirmed complete — i.e. it's still
    populating OR we can't read the fill state. Distinguishes 'warming up,
    give it time' from 'cache full but no candidates met criteria'.

    v4.14.5.33-firstrun-honesty: UNREADABLE fill (exception / pct None) now
    routes here ('still warming up') instead of to no_candidates. On a fresh
    install the fill state is often unknown, and 'still warming up' is the
    honest read — claiming 'the AI scanned and found nothing' is not.

    v4.14.5.51-recs-gatekeeping: once ANY style has displayable picks, 'warming
    up / queue empty' is contradictory — recommendations DO exist (just maybe
    not on the panel's current style). Suppress the popup in that case."""
    if _any_populated_path(app):
        return False
    if not _queue_is_empty(app):
        return False
    try:
        import tm_cache
        progress = tm_cache.get_fill_progress() or {}
    except Exception:
        # Can't confirm the fill is done -> treat as still warming.
        return True
    pct = progress.get('completion_pct')
    if pct is None:
        return True
    try:
        return float(pct) < 50.0
    except Exception:
        return True


def _obs_queue_empty_no_candidates(app, result) -> bool:
    """Live as of Phase 2. True when recommend_queue is empty AND an AI is
    connected AND the cache is CONFIRMED largely populated (>= 50% fill).
    Means the top AI ran against cached data but found no BUY candidates
    matching path criteria — different from the warming-up state.

    v4.14.5.33-firstrun-honesty: two honesty guards added so this 'the AI
    scanned and found nothing' message only fires when it's actually true —
    (1) require at least one AI provider (no-AI is handled by the Recommend
    at_least_one_voice pre-flight gate, never by claiming a scan happened);
    (2) require the fill state to be CONFIRMED >= 50% — unreadable/None fill
    routes to queue_empty_initial_fill ('still warming up'), not here."""
    if not _queue_is_empty(app):
        return False
    # (1) never claim 'the AI scanned' when no AI is connected.
    if not _check_at_least_one_ai_provider(app):
        return False
    try:
        import tm_cache
        progress = tm_cache.get_fill_progress() or {}
    except Exception:
        # (2) can't confirm a scan ran against a full cache -> don't claim it.
        return False
    pct = progress.get('completion_pct')
    if pct is None:
        return False
    try:
        return float(pct) >= 50.0
    except Exception:
        return False


def _queue_is_empty(app) -> bool:
    """Helper: True when the surface the Recommend dialog ACTUALLY shows
    has nothing to display.

    v4.14.5.14-recommend-responsiveness (Fix #4): the legacy / filter-
    cache mode (cfg['use_continuous_queue'] off — the user's default) drives
    the Recommend dialog from `recommend_cache`, NOT `recommend_queue`.
    The original implementation always counted `recommend_queue` rows, so
    in legacy mode (where that table is effectively unused) it returned
    True even when the dialog was full of cached picks — firing a false
    "no candidates" coaching popup — and it never fired on a genuinely
    empty cache. Now we check the same source the dialog reads
    (`app._get_recommendation_source(path)`) in legacy mode, and keep the
    original `recommend_queue` count for continuous-queue mode (byte-
    identical there). Fail-safe: any uncertainty returns False so we
    never resurrect the false-positive.
    """
    try:
        cfg = getattr(app, 'cfg', {}) or {}
    except Exception:
        cfg = {}

    # Legacy / filter-cache mode: check the cache the dialog displays.
    try:
        if not cfg.get('use_continuous_queue', False):
            get_src = getattr(app, '_get_recommendation_source', None)
            if not callable(get_src):
                return False  # can't tell → don't false-fire
            path = (getattr(app, '_recommend_path_override', None)
                    or cfg.get('analysis_path'))
            if not path:
                return False
            try:
                picks = get_src(path)
            except Exception:
                return False
            return not picks
    except Exception:
        return False

    # Continuous-queue mode: original recommend_queue check (unchanged).
    try:
        db = getattr(app, 'db', None)
        conn = getattr(db, 'conn', None) if db is not None else None
        if conn is None:
            return False  # can't check; assume not empty
        cur = conn.execute(
            "SELECT COUNT(*) FROM recommend_queue "
            "WHERE status IN ('active', 'verified_buy')")
        row = cur.fetchone()
        return (row[0] if row else 0) == 0
    except Exception:
        return False


OBSERVATION_CHECKS: dict = {
    'one_voice_only': _obs_one_voice_only,
    'cache_thin_initial_fill': _obs_cache_thin_initial_fill,
    'ticker_not_in_cache': _obs_ticker_not_in_cache,
    'voices_skipped_during_verify': _obs_voices_skipped_during_verify,
    'consensus_avoid_on_queue_pick':
        _obs_consensus_avoid_on_queue_pick,
    'consensus_split_no_majority': _obs_consensus_split_no_majority,
    'queue_empty_initial_fill': _obs_queue_empty_initial_fill,
    'queue_empty_no_candidates': _obs_queue_empty_no_candidates,
}


PREREQ_CHECKS: dict = {
    # Existing predicates from the old schema. Kept registered for
    # backward compat even though the new playbook doesn't reference
    # them by these exact names.
    'at_least_one_ai_provider': _check_at_least_one_ai_provider,
    'trading_style_selected': _check_trading_style_selected,
    'at_least_one_open_buy_prediction':
        _check_at_least_one_open_buy_prediction,
    # New playbook: the recommend entry references this name.
    # Aliased to the existing implementation.
    'open_buy_predictions': _check_at_least_one_open_buy_prediction,
    # New playbook predicates (Phase 1).
    'at_least_one_voice': _check_at_least_one_voice,
    'ai_not_paused': _check_ai_not_paused,
    'no_concurrent_scan': _check_no_concurrent_scan,
    'consensus_models_configured': _check_consensus_models_configured,
    'configured_models_installed': _check_configured_models_installed,
    'two_or_more_models_available': _check_two_or_more_models_available,
    'no_active_cooldown': _check_no_active_cooldown,
    'predictions_log_loadable': _check_predictions_log_loadable,
    'universe_non_empty': _check_universe_non_empty,
    'path_universe_compatible': _check_path_universe_compatible,
    'recommend_module_loaded': _check_recommend_module_loaded,
    'ai_module_loaded': _check_ai_module_loaded,
    'ticker_recognized': _check_ticker_recognized,
    'network_online': _check_network_online,
}


def is_suppressed(sup_key: str, app_ref) -> bool:
    """True if the given "action_id:prereq_id" key is suppressed for this user.

    Tutorial mode (Ctrl+Shift+`) overrides suppression: when the App's
    _teacher_ai_tutorial_mode_on flag is True, every suppression is treated
    as inactive so Teacher AI re-fires its tutorials even on actions the
    user previously dismissed with "Don't show again".
    """
    try:
        if getattr(app_ref, '_teacher_ai_tutorial_mode_on', False):
            return False
    except Exception:
        pass
    try:
        suppressed = list(app_ref.cfg.get('teacher_ai_suppressions') or [])
    except Exception:
        suppressed = []
    return sup_key in suppressed


# ─── Overlay slice 1: proportional, state-aware response ──────────────
#
# v4.14.5.34-overlay-proportional: upgrades the binary gate (block | pass)
# into a three-way response that scales to how far the user sits above or
# below the provider FLOOR. The dimension is the number of working cloud
# providers — read from the SAME source the at_least_one_voice floor uses
# (_count_working_providers below), so the count and the floor can never
# disagree:
#
#   0 providers   (below floor)  -> DEMAND-STEER. The existing
#                                   at_least_one_voice prereq already blocks
#                                   here; its playbook copy is warmed to a
#                                   parental "let's get you set up so I can
#                                   do this" steer. The real action does NOT
#                                   run.
#   1 provider    (at the floor)  -> PASS-THROUGH + gentle NUDGE. The action
#                                   runs unblocked; a non-blocking, peer-toned
#                                   suggestion to add another provider surfaces
#                                   (once per action per session).
#   >= 2 providers(above floor)  -> PASS-THROUGH, quiet. The action runs; no
#                                   nudge — a well-set-up user isn't nagged.
#
# Posture tracks LIVE state because the count is re-read on every click:
# dropping 1 -> 0 returns to demand-steer; rising 1 -> 2 silences the nudge.
#
# This is Path-A (rules-based) per design/teacher_ai_mvp.md — threshold
# logic, not the v4.15.1 model. The plumbing (prereq registry, intent
# dispatch, surface rendering, suppression/dedupe) is reused, not rebuilt.

# Actions that carry the provider floor + proportional nudge. Keep in sync
# with the register_intercept map in tired_market.py (only Look Up +
# Recommend are gated today — wiring the rest is a later overlay slice).
_PROVIDER_FLOOR_ACTIONS = {'recommend', 'look_up'}

# Provider count at/above which the nudge goes quiet (comfortably above the
# 1-provider floor). Below this (i.e. exactly 1) we nudge; at/above, silent.
_PROVIDER_COMFORT_COUNT = 2

# Nudge copy (v4.14.5.35-overlay-copy: real strings, good-enough-to-ship;
# expect a fuller in-context voice pass once the tutorial flow is built).
# Peer tone: the action works, this is only "it's sharper with more." NOT
# parental — that posture is reserved for the below-floor demand-steer.
_NUDGE_COPY = {
    'recommend': (
        "Running this on a single AI - I can do it, but a second voice "
        "sharpens the read and gives me more room before hitting limits. "
        "Want to add one?"
    ),
    'look_up': (
        "You're on one AI right now - adding another gives me a second "
        "opinion to cross-check. Worth doing when you have a minute."
    ),
}


def _enabled_provider_records(app):
    """Return the enabled-provider records (list of dicts), or None if the
    registry couldn't be read at all. Reads the SAME source the floor uses
    (AppStateQuery.providers_configured / _load_enabled_api_providers)."""
    state = getattr(app, 'state', None)
    if state is not None:
        try:
            provs = state.providers_configured()
            if provs is not None:
                return list(provs)
        except Exception:
            pass
    loader = getattr(app, '_load_enabled_api_providers', None)
    if callable(loader):
        return list(loader() or [])
    return None


def _provider_health_usable(prov) -> bool:
    """v4.14.5.40-live-gate: True if this enabled provider is also a usable
    voice RIGHT NOW per tm_provider_health — i.e. not in cooldown and not
    over a learned/observed daily cap. Mirrors the router's own provider_id
    keying (record 'id', else 'name'; tm_ai_router.is_eligible).

    Fail-OPEN: if health can't be read we treat the provider as usable. It
    was validated at add-time; a health-read glitch must never lock a user
    who has a real key out of their own app. (Decision B: no fresh re-
    validation at launch — trust add-time validation + live health.)"""
    try:
        pid = ''
        if isinstance(prov, dict):
            pid = str(prov.get('id') or prov.get('name') or '')
        elif isinstance(prov, str):
            pid = prov
        if not pid:
            return True  # can't key health -> don't penalise; count it
        import tm_provider_health as _h
        state = _h.get_state()
        if state is None:
            return True
        safe, _reason = state.is_safe_to_call(pid)
        return bool(safe)
    except Exception:
        return True


def _count_working_providers(app) -> int:
    """Number of cloud providers that are a WORKING voice right now: enabled
    (add-time validated) AND not flagged unusable by live health (cooldown /
    learned daily-cap exhaustion).

    v4.14.5.40-live-gate tightened this from a plain enabled() count: an
    expired-key / cooled-down 'enabled' provider must NOT count as a voice
    anywhere — the launch gate and the proportional gate read the same
    definition. Returns 0 when the registry itself can't be read (fail-safe:
    0 routes to demand-steer, never a false 'comfortable'). Health-unreadable
    is fail-OPEN per provider (see _provider_health_usable)."""
    try:
        provs = _enabled_provider_records(app)
        if not provs:
            return 0
        return sum(1 for p in provs if _provider_health_usable(p))
    except Exception:
        return 0


def has_working_provider(app) -> bool:
    """v4.14.5.40-live-gate: public — True if there is >=1 working provider
    right now (enabled + health-usable). This is the LIVE, every-launch gate
    the splash branch and the setup wizard read, replacing the old one-time
    teacher_ai_first_launch_complete flag (which a restart bypassed)."""
    try:
        return _count_working_providers(app) >= 1
    except Exception:
        return False


# v4.14.5.62-autoenable-multiprovider: number of working providers at/above
# which the multi-provider performance upgrades (concurrent scan dispatch,
# parallel consensus) auto-enable. Both give NO benefit at 1 provider (one
# worker = sequential; one voice = nothing to parallelize) and real benefit
# at >=2, so 2 is the natural threshold.
_MULTIPROVIDER_AUTO_THRESHOLD = 2


def multiprovider_autoflag(value, app) -> bool:
    """v4.14.5.62-autoenable-multiprovider: resolve a multi-provider perf flag
    whose config value may be the sentinel "auto".

      - value == "auto" (or None / missing) → AUTO: effective =
        (working-provider count >= _MULTIPROVIDER_AUTO_THRESHOLD). A multi-
        provider user (the user's 7, or any new user with >=2 keys) gets the
        upgrade automatically; a single-provider user does not (byte-identical
        to today's off state).
      - value is a real bool (True/False) → EXPLICIT user override, honored
        exactly (explicit off stays off even with many providers; explicit on
        stays on even with one).

    Fail-safe: any error → False (the safe, byte-identical-to-off result)."""
    try:
        if isinstance(value, str) and value.strip().lower() == 'auto':
            return _count_working_providers(app) >= _MULTIPROVIDER_AUTO_THRESHOLD
        if value is None:
            return _count_working_providers(app) >= _MULTIPROVIDER_AUTO_THRESHOLD
        return bool(value)
    except Exception:
        return False


def multiprovider_autoflag_reason(value, app) -> str:
    """Human-readable one-liner for the startup log: why a flag is on/off."""
    try:
        if isinstance(value, str) and value.strip().lower() == 'auto' or value is None:
            n = _count_working_providers(app)
            on = n >= _MULTIPROVIDER_AUTO_THRESHOLD
            return f"AUTO {'ON' if on else 'OFF'} ({n} working provider"  \
                   f"{'s' if n != 1 else ''})"
        return f"EXPLICIT {'ON' if bool(value) else 'OFF'} (user)"
    except Exception:
        return "AUTO OFF (provider count unavailable)"


# ── v4.14.5.62-autoenable-rotation: model-rotation auto-enable ──────────
# Rotation (use_model_rotation_schema + use_model_rotation_router) and
# per-model cap tracking (use_per_model_cap_tracking) are a win only when a
# provider has >=2 entries in its models[] list — that's when there's
# something to rotate AND the per-model rate-limit headroom kicks in. With a
# single model there's nothing to rotate, so auto stays OFF (byte-identical to
# a fresh single-model user). Safe to auto-enable now: Pro is tier-tagged out
# of scan and the self-maintaining scanner prunes dead models, so rotation
# only ever cycles valid, scan-appropriate models. Mirrors multiprovider_autoflag.
_MODEL_ROTATION_AUTO_MIN_MODELS = 2


def _max_models_per_working_provider(app) -> int:
    """Largest models[] length across providers that are a WORKING voice right
    now (same enabled+health-usable definition the launch gate uses). 0 when
    the registry can't be read (fail-safe → AUTO OFF)."""
    try:
        provs = _enabled_provider_records(app)
        if not provs:
            return 0
        best = 0
        for p in provs:
            if not isinstance(p, dict):
                continue
            if not _provider_health_usable(p):
                continue
            models = p.get('models')
            if isinstance(models, (list, tuple)):
                n = sum(1 for m in models if str(m).strip())
                if n > best:
                    best = n
        return best
    except Exception:
        return 0


def model_rotation_autoflag(value, app) -> bool:
    """Resolve a rotation/cap flag whose config value may be the sentinel
    "auto". "auto"/None → AUTO: True when some working provider has
    >=_MODEL_ROTATION_AUTO_MIN_MODELS models. A real bool → EXPLICIT user
    override, honored exactly (explicit off stays off even with many models;
    explicit on stays on even with one). Fail-safe: any error → False."""
    try:
        if value is None or (isinstance(value, str)
                             and value.strip().lower() == 'auto'):
            return (_max_models_per_working_provider(app)
                    >= _MODEL_ROTATION_AUTO_MIN_MODELS)
        return bool(value)
    except Exception:
        return False


def model_rotation_autoflag_reason(value, app) -> str:
    """Human-readable one-liner for the startup log: why rotation is on/off."""
    try:
        if value is None or (isinstance(value, str)
                             and value.strip().lower() == 'auto'):
            n = _max_models_per_working_provider(app)
            on = n >= _MODEL_ROTATION_AUTO_MIN_MODELS
            return f"AUTO {'ON' if on else 'OFF'} (max {n} models on a working provider)"
        return f"EXPLICIT {'ON' if bool(value) else 'OFF'} (user)"
    except Exception:
        return "AUTO OFF (provider models unavailable)"


def model_rotation_is_auto(value) -> bool:
    """True when the config value is the auto sentinel (vs an explicit bool) —
    used to decide whether to surface the 'I turned rotation on' coaching
    (only on AUTO-enable, not when the user explicitly set it)."""
    return value is None or (isinstance(value, str)
                             and value.strip().lower() == 'auto')


def _maybe_surface_provider_nudge(action_id: str, app_ref) -> None:
    """Slice-1 proportional NUDGE. Non-blocking by construction: the caller
    (_proceed_with_proportional_nudge) always runs the real action; this
    only adds an optional, dismissible suggestion. Fires only when the user
    sits exactly AT the provider floor (count == 1) on a floor action, and
    only once per action per session (reuses the _popup_should_show dedupe).

    count == 0 never reaches a 'proceed' in the live gate (the
    at_least_one_voice floor blocks first), and the count < 1 guard here is
    a belt-and-braces second line; count >= _PROVIDER_COMFORT_COUNT is
    comfortably above the floor and stays silent."""
    if action_id not in _PROVIDER_FLOOR_ACTIONS:
        return
    try:
        count = _count_working_providers(app_ref)
    except Exception:
        return
    # Proportional: only the at-floor, low-headroom state earns a nudge.
    if count < 1 or count >= _PROVIDER_COMFORT_COUNT:
        return
    # Once per action per session — a nudge that nags is a worse nudge.
    if not _popup_should_show(app_ref, f"nudge:add_provider:{action_id}"):
        return
    message = _NUDGE_COPY.get(action_id) or _NUDGE_COPY['recommend']
    action_dicts = [
        {'label': 'Add a provider', 'action': 'open_api_providers_dialog'},
        {'label': 'Not now', 'action': 'dismiss'},
    ]
    # Minimal entry so the unloaded (no tm_teacher_ai) fallback stays silent
    # — a nudge is optional, and severity 'info' suppresses the messagebox
    # path. The loaded path ignores `entry` and renders message + actions.
    entry = {'severity': 'info',
             'short_hint': 'Adding another AI provider sharpens the read.'}
    _present_surface(app_ref, entry, message, action_dicts)


def _proceed_with_proportional_nudge(action_id, app_ref, proceed_fn) -> None:
    """Pass-through router shared by register_intercept's _wrapped() and
    gate_call(): the action ALWAYS runs (this layer never blocks), and a
    floor action sitting at the floor first gets a gentle, non-blocking
    nudge.

    Order is nudge-then-proceed deliberately: there is one Teacher AI
    surface per canvas (last writer wins), so surfacing the generic nudge
    BEFORE the action lets anything the action itself surfaces (e.g. a
    post-action 'queue empty / warming up' coaching message) supersede the
    nudge — the specific, situational message should win over the generic
    suggestion, not the other way round. The nudge is best-effort; a failure
    in it never stops the real action."""
    try:
        _maybe_surface_provider_nudge(action_id, app_ref)
    except Exception:
        pass
    proceed_fn()


def check_prereqs(prereq_ids, app_ref) -> Optional[str]:
    """Run the listed prereqs in order; return the first failing id or None."""
    for pid in (prereq_ids or []):
        check = PREREQ_CHECKS.get(pid)
        if check is None:
            # Unknown prereq — fail safe so the surface still appears
            # (better to over-warn than silently let the action through).
            return pid
        try:
            ok = check(app_ref)
        except Exception:
            ok = False
        if not ok:
            return pid
    return None


# ─── Intent dispatch table ────────────────────────────────────────────
#
# One callable per intent string from the playbook's offered_action.intent
# enum (see _intents_doc at the top of error_recovery_playbook.json).
# All eight functions take the App reference and return None. Each is
# best-effort — exceptions are swallowed so a broken intent doesn't
# crash the surface flow. To add a new intent: write the function,
# register it in _INTENT_DISPATCH below, and add the intent to the
# playbook's _intents_doc.

def _intent_open_api_providers_dialog(app_ref) -> None:
    try:
        # v4.14.5.39-guided-add: prefer the guided opener — it shows the
        # first-run Groq how-to on the main screen (once, only when the user
        # has no provider yet) and then opens the providers window. Falls
        # back to the bare opener / Settings if it isn't wired.
        fn = getattr(app_ref, '_open_providers_with_guide', None)
        if callable(fn):
            fn()
            return
        fn = getattr(app_ref, '_show_api_providers', None)
        if callable(fn):
            fn()
            return
        # Fallback: open Settings if the standalone dialog isn't wired.
        fn = getattr(app_ref, '_show_settings', None)
        if callable(fn):
            fn()
    except Exception:
        pass


def _intent_open_settings_ai_sources(app_ref) -> None:
    """Phase 1: opens the Settings dialog. The 'scrolled to AI Sources'
    behavior is post-MVP — wiring scroll targets requires Settings to
    expose section anchors, which is a separate task. For now the user
    lands at Settings and scrolls to the section themselves."""
    try:
        fn = getattr(app_ref, '_show_settings', None)
        if callable(fn):
            fn()
    except Exception:
        pass


# _intent_open_ollama_install_guide REMOVED in v4.14.5.14-ollama-purge-step4:
# Ollama is retired (cloud-only) — there is no Ollama to install. The
# 'open_ollama_install_guide' intent + the error_recovery entries that used it
# are removed from error_recovery_playbook.json.


def _intent_unpause_ai(app_ref) -> None:
    try:
        import tm_holdings
        tm_holdings.set_ai_paused(False)
    except Exception:
        pass


def _intent_trigger_data_refresh(app_ref) -> None:
    """Trigger a manual data refresh. _refresh_all_holdings is the
    canonical entry point that drives the freshness indicator; older
    builds may use a differently-named method, so we try a few."""
    try:
        for name in ('_refresh_all_holdings', '_refresh_data',
                     '_manual_refresh', '_refresh_quotes'):
            fn = getattr(app_ref, name, None)
            if callable(fn):
                fn()
                return
    except Exception:
        pass


def _intent_open_data_folder(app_ref) -> None:
    """Open the app's data folder in the OS file manager. Pulls the
    canonical path from tired_market.DATA_DIR so we stay in sync with
    wherever the rest of the app writes."""
    try:
        import os
        try:
            import tired_market as _tm
            data_dir = str(getattr(_tm, 'DATA_DIR', None) or '')
        except Exception:
            data_dir = ''
        if not data_dir:
            return
        if os.name == 'nt':
            os.startfile(data_dir)
        else:
            import subprocess
            subprocess.run(['xdg-open', data_dir])
    except Exception:
        pass


def _intent_open_data_providers_dialog(app_ref) -> None:
    """Opens the Data Providers manager dialog (data-side keys, lane
    health). The actual method on the app is _show_data_providers
    (confirmed via grep at tired_market.py:13142). Falls back to
    Settings if the dedicated method is unavailable in some build."""
    try:
        fn = getattr(app_ref, '_show_data_providers', None)
        if callable(fn):
            fn()
            return
        fn = getattr(app_ref, '_show_settings', None)
        if callable(fn):
            fn()
    except Exception:
        pass


def _intent_start_verify(app_ref) -> None:
    """Re-runs Verify on the pick last verified. The pick identity is
    stashed on the app at app._teacher_ai_last_verify_pick by Verify's
    completion handler (when Verify ships). Until then this dispatcher
    is a no-op forward placeholder — coaching surfaces that offer
    'verify the next pick' will quietly do nothing."""
    pick_id = getattr(app_ref, '_teacher_ai_last_verify_pick', None)
    if not pick_id:
        try:
            log = getattr(app_ref, '_log', None)
            if callable(log):
                log(
                    "[teacher-ai] start_verify: no recent verify "
                    "context (Verify hasn't shipped yet)", 'muted')
        except Exception:
            pass
        return
    # When Verify ships, route through whichever method runs Verify
    # for a single pick. Try common names; fall back silently.
    try:
        for name in ('_run_verify', '_start_verify', '_verify_pick'):
            fn = getattr(app_ref, name, None)
            if callable(fn):
                fn(pick_id)
                return
    except Exception:
        pass


def _intent_open_recommend_settings(app_ref) -> None:
    """Opens Settings dialog. The 'Advanced Settings → Recommend'
    section is part of the v4.15.0 unbuilt feature set; until that
    section exists, this dispatcher opens Settings without a specific
    anchor (same compromise as open_settings_ai_sources)."""
    try:
        fn = getattr(app_ref, '_show_settings', None)
        if callable(fn):
            fn()
    except Exception:
        pass


def _intent_open_recommendations(app_ref) -> None:
    """Open the Recommendations view itself (not its settings). Used by
    the recs-ready proactive moment's 'Show me' button — v4.14.5.37."""
    try:
        fn = getattr(app_ref, '_show_recommendations', None)
        if callable(fn):
            fn()
    except Exception:
        pass


def _intent_open_backups_folder(app_ref) -> None:
    """Open the data_backups/ folder (sibling of the install location)
    in the OS file manager. Uses tired_market.SCRIPT_DIR as the base
    so we match the auto-backup destination."""
    try:
        import os
        try:
            import tired_market as _tm
            script_dir = getattr(_tm, 'SCRIPT_DIR', None)
        except Exception:
            script_dir = None
        if script_dir is None:
            return
        backups = Path(script_dir) / 'data_backups'
        if not backups.exists():
            return
        path = str(backups)
        if os.name == 'nt':
            os.startfile(path)
        else:
            import subprocess
            subprocess.run(['xdg-open', path])
    except Exception:
        pass


# v4.14.5.14-stale-cleanup: _intent_open_universe_picker removed along with
# the path_universe_mismatch playbook entry (its only consumer). The universe
# picker is no longer surfaced through the intercept layer.


_INTENT_DISPATCH: dict = {
    'open_api_providers_dialog': _intent_open_api_providers_dialog,
    'open_settings_ai_sources': _intent_open_settings_ai_sources,
    'open_data_providers_dialog': _intent_open_data_providers_dialog,
    'unpause_ai': _intent_unpause_ai,
    'start_verify': _intent_start_verify,
    'trigger_data_refresh': _intent_trigger_data_refresh,
    'open_data_folder': _intent_open_data_folder,
    'open_backups_folder': _intent_open_backups_folder,
    'open_recommend_settings': _intent_open_recommend_settings,
    'open_recommendations': _intent_open_recommendations,
}


# ─── Action executors ─────────────────────────────────────────────────

def execute_offered_action(action_dict, app_ref) -> None:
    """Dispatch one offered_action button click.

    Two action types:
    - Infrastructure actions ('dismiss', 'dismiss_and_suppress') that
      _compose_offered_action_dicts appends to every surface.
    - Intent actions: the 8 values from the playbook's intent enum,
      dispatched via _INTENT_DISPATCH.

    Unknown actions log a muted-severity warning and return. Safe with
    malformed input."""
    if not isinstance(action_dict, dict):
        return
    action = action_dict.get('action')
    if not action:
        return

    if action == 'dismiss':
        # v4.14.5.51-recs-gatekeeping: explicitly dismiss the active surface
        # (this was a no-op that relied on the fragile generic post-click
        # animate-out — which failed when another surface had replaced the
        # one on the canvas, so 'Not now' appeared to do nothing). The
        # once-ever suppression marker is already persisted at emit time, so
        # the banner won't re-pop.
        try:
            import tm_teacher_ai
            canvas = getattr(app_ref, 'content_canvas', None)
            if canvas is not None:
                tm_teacher_ai.dismiss_active(canvas)
        except Exception:
            pass
        return

    if action == 'dismiss_and_suppress':
        _suppress_current(app_ref)
        return

    fn = _INTENT_DISPATCH.get(action)
    if fn is not None:
        try:
            fn(app_ref)
        except Exception:
            pass
        return

    # Unknown action — log and continue. Reaching this branch usually
    # means the playbook references an intent that hasn't been wired
    # in _INTENT_DISPATCH (or a typo in either place).
    try:
        log = getattr(app_ref, '_log', None)
        if callable(log):
            log(f"[teacher-ai] unknown action '{action}' ignored", 'muted')
    except Exception:
        pass


def _suppress_current(app_ref) -> None:
    """Append the currently-pending intercept key to cfg suppressions."""
    key = getattr(app_ref, '_teacher_ai_current_intercept_key', None)
    if not key:
        return
    try:
        lst = list(app_ref.cfg.get('teacher_ai_suppressions') or [])
        if key not in lst:
            lst.append(key)
            app_ref.cfg['teacher_ai_suppressions'] = lst
            _save_config_if_possible(app_ref)
    except Exception:
        pass


def _save_config_if_possible(app_ref) -> None:
    """Best-effort persist of app.cfg. Prefer the App's own helper; fall
    back to the module-level save_config from tired_market."""
    try:
        fn = getattr(app_ref, '_save_config', None)
        if callable(fn):
            fn()
            return
    except Exception:
        pass
    try:
        import tired_market as _tm
        save_fn = getattr(_tm, 'save_config', None)
        if callable(save_fn):
            save_fn(app_ref.cfg)
    except Exception:
        pass


# ─── Public API: register_intercept ──────────────────────────────────

def register_intercept(button_widget, action_id: str, app_ref) -> None:
    """Wrap `button_widget`'s existing command with prereq-checking logic.

    Idempotent per widget: a second call on the same button is a no-op.
    """
    if button_widget is None or not action_id:
        return
    if getattr(button_widget, '_teacher_ai_wrapped', False):
        return

    # Capture the widget's existing Tcl command name. When you pass a
    # Python callable as `command=` to tk.Button, tkinter registers it as
    # a Tcl command and stores the Tcl name on the widget's option list.
    # Reading via cget('command') returns that name. The Tcl command stays
    # callable for the life of the widget even after we replace `command`,
    # so we can invoke the original via `widget.tk.call(tcl_name)`.
    try:
        tcl_name = button_widget.cget('command')
    except Exception:
        tcl_name = None
    if not tcl_name:
        tcl_name = None

    def _call_original() -> None:
        if not tcl_name:
            return
        try:
            button_widget.tk.call(tcl_name)
        except Exception:
            pass

    # Stash the original on the App for cross-intercept references
    # (e.g., the 'start_scan' offered_action needs to invoke Scan's
    # original handler from inside Recommend's intercept surface).
    try:
        if not hasattr(app_ref, '_teacher_ai_originals'):
            app_ref._teacher_ai_originals = {}
        app_ref._teacher_ai_originals[action_id] = _call_original
    except Exception:
        pass

    # NOTE FOR FUTURE EDITORS: this nested _wrapped function and the
    # top-level gate_call() below share an identical body pattern
    # (prereq check → suppression gate → surface compose → present).
    # When updating either, update BOTH. A Pass B edit using
    # Edit(replace_all=True) updated only this one because of
    # indentation differences (nested 8-space vs. top-level 4-space)
    # — gate_call shipped with a stale signature for a release until
    # caught later. Treat the two as a contract pair.
    def _wrapped() -> None:
        prereqs = _get_prereqs_for_action(action_id)

        failing = check_prereqs(prereqs, app_ref)
        if failing is None:
            # All floors met -> pass through, but the proportional nudge
            # layer may add a gentle "add another provider" suggestion if
            # the user sits exactly at the provider floor (overlay slice 1).
            _proceed_with_proportional_nudge(action_id, app_ref, _call_original)
            return

        # Look up the playbook entry now so we know whether this is a HARD
        # floor (action genuinely can't run) before consulting suppression.
        entry = _get_entry_for(action_id, failing) or {}
        blocking = _entry_is_blocking(entry)

        # v4.14.6.81-noai-board-works: a no-AI-by-choice user must not be
        # walled by the AI-voice floor. Pass the action through to its real
        # handler — Recommend opens the algorithm-driven board; Look Up
        # reaches its own v4.14.6.80 "this uses an AI" explanation (no double
        # wall). Scoped to the at_least_one_voice prereq + the no-AI state, so
        # the AI path (providers present / an AI mode) is byte-for-byte as
        # before.
        if failing == 'at_least_one_voice' and _noai_board_passthrough(app_ref):
            _call_original()
            return

        # Suppression: keyed by "action_id:prereq_id" pair. Tutorial mode
        # (Ctrl+Shift+`) overrides this in is_suppressed. A NON-blocking
        # prereq the user has silenced passes through (the action works). A
        # BLOCKING floor is never suppressible — even a stale suppression
        # marker must NOT run the broken action (v4.14.5.40-live-gate).
        sup_key = f"{action_id}:{failing}"
        if not blocking and is_suppressed(sup_key, app_ref):
            _proceed_with_proportional_nudge(action_id, app_ref, _call_original)
            return

        # Stash the current intercept key for dismiss_and_suppress.
        try:
            app_ref._teacher_ai_current_intercept_key = sup_key
        except Exception:
            pass

        message = _compose_message(entry, action_id, app_ref=app_ref)
        action_dicts = _compose_offered_action_dicts(
            entry, is_blocking_floor=blocking)
        _present_surface(app_ref, entry, message, action_dicts)

    try:
        button_widget.config(command=_wrapped)
        button_widget._teacher_ai_wrapped = True
    except Exception:
        pass


# NOTE FOR FUTURE EDITORS: gate_call() and the _wrapped() nested
# function inside register_intercept() above share an identical body
# pattern. When updating either, update BOTH. They are a contract
# pair — Edit(replace_all=True) won't catch both because the
# indentation contexts differ.
def gate_call(action_id: str, app_ref, on_pass: Callable) -> None:
    """Run prereq checks for `action_id` and either invoke `on_pass()` (if
    all pass or the user has suppressed) or surface the AI guidance message.

    Companion to register_intercept for entry points that aren't tk.Button
    command attributes — e.g., right-click bindings, menu items, internal
    callers. Same playbook lookup + suppression semantics.
    """
    prereqs = _get_prereqs_for_action(action_id)

    failing = check_prereqs(prereqs, app_ref)
    if failing is None:
        # Pass through with the proportional nudge layer (overlay slice 1) —
        # twin of register_intercept._wrapped's proceed path; keep in sync.
        try:
            _proceed_with_proportional_nudge(action_id, app_ref, on_pass)
        except Exception:
            pass
        return

    # Look up the entry now so we know whether this is a HARD floor before
    # consulting suppression (twin of register_intercept._wrapped).
    entry = _get_entry_for(action_id, failing) or {}
    blocking = _entry_is_blocking(entry)

    # v4.14.6.81-noai-board-works: twin of register_intercept._wrapped — a
    # no-AI-by-choice user passes the at_least_one_voice floor through to the
    # real handler (Recommend opens the algo board; Look Up reaches its own
    # v4.14.6.80 explanation). No-AI-gated; AI path unchanged.
    if failing == 'at_least_one_voice' and _noai_board_passthrough(app_ref):
        try:
            on_pass()
        except Exception:
            pass
        return

    sup_key = f"{action_id}:{failing}"
    if not blocking and is_suppressed(sup_key, app_ref):
        try:
            _proceed_with_proportional_nudge(action_id, app_ref, on_pass)
        except Exception:
            pass
        return

    try:
        app_ref._teacher_ai_current_intercept_key = sup_key
    except Exception:
        pass

    message = _compose_message(entry, action_id, app_ref=app_ref)
    action_dicts = _compose_offered_action_dicts(
        entry, is_blocking_floor=blocking)
    _present_surface(app_ref, entry, message, action_dicts)


# ─── Phase B: post-action observation runtime ─────────────────────────

# Cooldown duration for the same coaching entry firing repeatedly
# within a session. Prevents intercept-spam on rapid back-to-back
# actions when the underlying observation is still true.
_COACHING_COOLDOWN_SECONDS = 5 * 60


def observe_action(app, action_id: str,
                   result: Optional[dict] = None) -> None:
    """Called by an action handler after the action completes. Walks
    the post_action_observation entries matching action_id, evaluates
    each observation predicate against (app, result), and surfaces
    the first entry whose predicate returns True.

    First-fire wins. If multiple observations match the same action,
    the entry that appears earlier in the playbook file fires; the
    others wait for the user's next action of the same kind. Authors
    order coaching entries by importance.

    Per-predicate exceptions are isolated — a broken observation
    doesn't block the others. Tutorial mode (Ctrl+Shift+`) bypasses
    the session cooldown so coaching re-fires every time."""
    if _PLAYBOOK_LOADED is None:
        _load_playbook()
    matches = _INDEX_ACTION_TO_OBSERVATIONS.get(action_id) or []
    if not matches:
        return

    tutorial_mode = False
    try:
        tutorial_mode = bool(
            getattr(app, '_teacher_ai_tutorial_mode_on', False))
    except Exception:
        pass

    now = time.time()

    for entry, observation_id in matches:
        entry_id = entry.get('entry_id') or ''

        # Cooldown gate: skip if this entry fired recently (unless
        # tutorial mode bypasses).
        if not tutorial_mode and entry_id:
            last = _session_event_cooldowns.get(entry_id)
            if last is not None and (now - last) < _COACHING_COOLDOWN_SECONDS:
                continue

        # Evaluate the predicate. Per-predicate try/except so one
        # broken observation doesn't poison the rest.
        predicate = OBSERVATION_CHECKS.get(observation_id)
        if predicate is None:
            try:
                log = getattr(app, '_log', None)
                if callable(log):
                    log(
                        f"[teacher-ai] no predicate registered for "
                        f"observation '{observation_id}' "
                        f"(entry {entry_id})", 'muted')
            except Exception:
                pass
            continue

        try:
            fired = bool(predicate(app, result))
        except Exception as e:
            try:
                log = getattr(app, '_log', None)
                if callable(log):
                    log(
                        f"[teacher-ai] observation '{observation_id}' "
                        f"raised: {e}", 'muted')
            except Exception:
                pass
            continue

        if not fired:
            continue

        # Predicate matched — fire the surface, stamp the cooldown,
        # log it, return (first-fire wins).
        if entry_id:
            _session_event_cooldowns[entry_id] = now
        try:
            log = getattr(app, '_log', None)
            if callable(log):
                log(
                    f"[teacher-ai] coaching: {entry_id} fired", 'muted')
        except Exception:
            pass

        message = _compose_message(entry, action_id, app_ref=app)
        action_dicts = _compose_offered_action_dicts(entry)
        _present_surface(app, entry, message, action_dicts)
        return


# ─── Phase 2: system_event emission (worker-thread-safe) ─────────────

def emit_system_event(event_id: str, app=None,
                      context: Optional[dict] = None) -> None:
    """Fire the playbook entry registered for this system_event.
    Safe to call from any thread — internally marshals the surface
    flow back to the Tk main thread via root.after when needed.

    event_id: the event name (matches entry.trigger.event in the
    playbook).
    app: the App reference. If None, falls back to the module
    registry (`_registered_app`) set via register_app at startup.
    Without either, the call is a silent no-op.
    context: optional dict of token substitutions. Most system_event
    entries use {provider} — pass {'provider': '<name>'} for those.

    Same 5-minute cooldown semantics as observe_action, keyed by
    entry_id. Tutorial mode bypasses cooldown."""
    target_app = app if app is not None else _registered_app
    if target_app is None:
        return  # No app in context — silent no-op.

    # Worker-thread-safe marshaling: if we're called from a non-Tk
    # thread, schedule the work on the main thread via root.after.
    # Always-on marshaling is simpler than detecting the current
    # thread; root.after is cheap and works from any thread.
    root = getattr(target_app, 'root', None)
    if root is not None:
        try:
            root.after(
                0, lambda: _do_emit_system_event(
                    target_app, event_id, context))
            return
        except Exception:
            pass
    # Fallback when no root is available (tests, headless callers):
    # call directly. _do_emit_system_event handles thread-unsafe
    # operations defensively.
    _do_emit_system_event(target_app, event_id, context)


# v4.14.5.54-recs-gate-budgetaware: recs-panel coaching events that fire WHILE
# the Recommend window (a separate Toplevel) is open. Their surface draws on the
# MAIN content_canvas BEHIND that window — occluded and awkward to dismiss, and
# racing the window-open it was the "Dismiss did nothing" resurrection. Each has
# an in-panel equivalent (the empty-column text / survivor summary), so we
# suppress the occluded duplicate and let the panel explain in context. (The
# pre-open blocks — recs_gate_not_ready / recs_gate_empty_path /
# recs_gate_budget_blocks_all — fire before the window opens, render on the main
# screen the user is actually looking at, and are NOT suppressed.)
_RECS_PANEL_OCCLUDED_EVENTS = frozenset(
    {'recs_gate_budget_no_fit', 'recs_gate_filter_hides_all'})


def _recs_window_is_open(app) -> bool:
    """True when the Recommend Toplevel exists and is mapped. Never raises."""
    try:
        win = getattr(app, '_recommend_win', None)
        return win is not None and bool(win.winfo_exists())
    except Exception:
        return False


def _do_emit_system_event(app, event_id: str,
                          context: Optional[dict]) -> None:
    """Internal: the main-thread half of emit_system_event. Runs
    cooldown check, predicate-free lookup, and surface fire."""
    if _PLAYBOOK_LOADED is None:
        _load_playbook()
    entry = _INDEX_SYSTEM_EVENT.get(event_id)
    if not entry:
        return  # No matching entry — silent no-op.

    # v4.14.5.54: don't draw a recs-panel coaching surface onto the occluded
    # main canvas while the Recommend window is open in front of it.
    # v4.14.6.12-fix-recs-paint-race (2026-06-12): the v4.14.6.11
    # asynchronous repaint (root.after(0, _recommend_render)) was
    # racing the dropdown handler's own synchronous render in
    # _on_path_change, which already calls _render_recommendations via
    # _recommend_apply_style_change. Two paints per switch — one from
    # the intercept's deferred callback, one from the handler — could
    # land in either order on different machines, and the deferred one
    # sometimes ran while the handler's widget destruction was still
    # in flight, leaving the panel with no children. Returning to the
    # original log-only behavior — the dropdown handler is the single
    # authoritative paint site per switch. Tier-1 survivor summary +
    # any in-panel "no picks" text is painted by that same render
    # pass, not by this intercept.
    if event_id in _RECS_PANEL_OCCLUDED_EVENTS and _recs_window_is_open(app):
        try:
            log = getattr(app, '_log', None)
            if callable(log):
                log(f"[teacher-ai] {event_id} suppressed (recs window open; "
                    f"shown in-panel instead)", 'muted')
        except Exception:
            pass
        return

    entry_id = entry.get('entry_id') or ''

    tutorial_mode = False
    try:
        tutorial_mode = bool(
            getattr(app, '_teacher_ai_tutorial_mode_on', False))
    except Exception:
        pass

    # Cooldown gate (5 min, keyed by entry_id, bypassed by tutorial
    # mode). Reuses the shared _session_event_cooldowns dict so
    # coaching and system_event share cool-off behavior.
    now = time.time()
    if not tutorial_mode and entry_id:
        last = _session_event_cooldowns.get(entry_id)
        if last is not None and (now - last) < _COACHING_COOLDOWN_SECONDS:
            return

    if entry_id:
        _session_event_cooldowns[entry_id] = now
    try:
        log = getattr(app, '_log', None)
        if callable(log):
            log(f"[teacher-ai] system_event: {event_id} fired", 'muted')
    except Exception:
        pass

    # action_id is None for system_event flows — no user-action
    # context exists. Token rendering relies on the context dict
    # (typically {'provider': name}) for {provider} substitutions.
    message = _compose_message(
        entry, None, app_ref=app, context=context)
    action_dicts = _compose_offered_action_dicts(entry)

    # v4.14.5.14a.5 Component D: per-session popup deduplication. The
    # SAME error type (e.g. "Gemini rate-limit") should pop up ONCE
    # per session, not every 5 minutes — but a NEW type (different
    # provider or different event) still pops. The event is STILL
    # logged to activity.log above; only the popup surface is
    # suppressed. Keyed by event + provider so Gemini::rate_limit and
    # Mistral::rate_limit are distinct. Resets on restart (in-memory).
    # Separate from the permanent "Don't show again" dismissal.
    prov_ctx = ''
    try:
        prov_ctx = str((context or {}).get('provider') or '').strip()
    except Exception:
        prov_ctx = ''
    dedupe_key = f"{entry_id}::{prov_ctx}"
    if not _popup_should_show(app, dedupe_key):
        try:
            log = getattr(app, '_log', None)
            if callable(log):
                log(f"[teacher-ai] popup suppressed (already shown "
                    f"this session): {dedupe_key}", 'muted')
        except Exception:
            pass
        return

    _present_surface(app, entry, message, action_dicts)


# ─── Proactive POSITIVE guidance: feature intros (v4.14.5.36) ─────────
#
# emit_system_event (above) is the proactive PUSH bus, but every entry it
# carries today is an ERROR/failure moment, gated only by session dedupe +
# a 5-min cooldown. A feature intro is different: it's a one-time POSITIVE
# "here's what this is for" moment that should fire ONCE EVER (persisted),
# can point AT the control it's introducing, and re-fires under tutorial
# mode for demonstration. It reuses everything: the playbook entry supplies
# the copy, _present_surface renders it (now target-aware -> show_near), and
# the persistent teacher_ai_suppressions list is the once-ever marker (the
# same list "Don't show again" uses, and which is_suppressed already
# bypasses in tutorial mode).


def _mark_suppressed(app, key: str) -> None:
    """Append `key` to the persistent teacher_ai_suppressions list (the
    once-ever 'introduced' marker) and save. Idempotent. Best-effort."""
    if not key:
        return
    try:
        lst = list(app.cfg.get('teacher_ai_suppressions') or [])
        if key not in lst:
            lst.append(key)
            app.cfg['teacher_ai_suppressions'] = lst
            _save_config_if_possible(app)
    except Exception:
        pass


def emit_feature_intro(event_id: str, suppress_key: str, app=None,
                       target_widget=None,
                       context: Optional[dict] = None,
                       actions: Optional[list] = None) -> None:
    """Fire a one-time positive feature-intro moment.

    event_id: the playbook system_event entry id carrying the intro copy.
    suppress_key: persistent once-ever marker (e.g. 'feature_intro:ask_ai').
        Checked before showing (skip if already introduced), set after.
        is_suppressed() bypasses it under tutorial mode, so tutorial mode
        re-fires; the marker is NOT persisted in tutorial mode so a real
        first intro is still tracked normally afterwards.
    target_widget: optional control to dock the surface against (arrow
        points at it via show_near). None -> center-screen.
    actions: optional list of {label, action} dicts for the surface
        buttons. None -> a single warm 'Got it' acknowledge. (action values
        are dispatched by execute_offered_action: 'dismiss' or any intent in
        _INTENT_DISPATCH, e.g. 'open_recommendations'.)

    Thread-safe like emit_system_event (marshals to the Tk main thread)."""
    target_app = app if app is not None else _registered_app
    if target_app is None:
        return
    root = getattr(target_app, 'root', None)
    if root is not None:
        try:
            root.after(0, lambda: _do_emit_feature_intro(
                target_app, event_id, suppress_key, target_widget,
                context, actions))
            return
        except Exception:
            pass
    _do_emit_feature_intro(target_app, event_id, suppress_key,
                           target_widget, context, actions)


def _do_emit_feature_intro(app, event_id: str, suppress_key: str,
                           target_widget, context: Optional[dict],
                           actions: Optional[list] = None) -> None:
    """Main-thread half of emit_feature_intro: once-ever gate, surface,
    then persist the marker (unless tutorial mode)."""
    if _PLAYBOOK_LOADED is None:
        _load_playbook()
    entry = _INDEX_SYSTEM_EVENT.get(event_id)
    if not entry:
        return  # no copy registered — silent no-op

    # Once-ever gate (persistent). is_suppressed() returns False under
    # tutorial mode, so tutorial mode always re-fires the intro.
    if suppress_key and is_suppressed(suppress_key, app):
        return

    tutorial_mode = False
    try:
        tutorial_mode = bool(getattr(app, '_teacher_ai_tutorial_mode_on',
                                     False))
    except Exception:
        pass

    try:
        log = getattr(app, '_log', None)
        if callable(log):
            log(f"[teacher-ai] feature_intro: {event_id} fired", 'muted')
    except Exception:
        pass

    message = _compose_message(entry, None, app_ref=app, context=context)
    # Default to a warm single acknowledge button — the intro asks nothing
    # of the user. Callers can pass richer actions (e.g. recs-ready offers
    # 'Show me' -> open_recommendations). Built inline rather than via
    # _compose_offered_action_dicts so the positive moment isn't a cold
    # 'Dismiss' + 'Don't show again'. _on_action_click dismisses after.
    action_dicts = actions if actions else [
        {'label': 'Got it', 'action': 'dismiss'}]
    _present_surface(app, entry, message, action_dicts,
                     target_widget=target_widget)

    # Persist the once-ever marker — but NOT in tutorial mode, so a tutorial
    # walkthrough doesn't burn the user's real first-intro flag.
    if suppress_key and not tutorial_mode:
        _mark_suppressed(app, suppress_key)


def _fallback_message(prereq_id: str) -> str:
    """Use the prereq_definitions copy from features.json as fallback if
    a playbook entry is missing on_missing copy."""
    feats = _load_features()
    defs = feats.get('prereq_definitions') or {}
    return (defs.get(prereq_id)
            or f"This action needs '{prereq_id}' but it's not set up yet.")


# ─── Audit testing note ──────────────────────────────────────────────
#
# Future audit scripts must NOT mock _present_surface directly. That
# mock point is too high — it bypasses the actual surface helpers
# (_present_surface_loaded / _present_surface_unloaded), which is
# where silent-exception bugs hide. Mock at the deeper level:
# tm_teacher_ai.show_center for the loaded path, messagebox.showwarning
# for the unloaded path. Pass B and Phase 2 audits passed GREEN while
# the surface was silently broken because they mocked at the wrong
# level — the show_center call was raising silently, the bare
# `except: pass` swallowed it, and the mock at _present_surface never
# exercised the layer where the bug lived. Don't repeat that mistake.

def _present_surface(app_ref, entry: dict,
                     message: str, action_dicts,
                     target_widget=None) -> None:
    """Render the surface for a fired intercept. Two paths:
    - Loaded: rich on-canvas surface via tm_teacher_ai. When
      `target_widget` is given the surface docks NEXT TO that control
      (show_near, with an arrow pointing at it); otherwise it renders
      center-screen (show_center) as before.
    - Unloaded: terse messagebox.showwarning fallback using the
      entry's short_hint + manual_fallback. info-severity entries
      stay silent in unloaded mode.

    `target_widget` (v4.14.5.36-proactive-guidance): additive — every
    existing caller omits it and keeps the center-screen default; only
    proactive feature-intro moments pass a control to point at."""
    if is_teacher_ai_available():
        _present_surface_loaded(app_ref, message, action_dicts,
                                target_widget=target_widget)
    else:
        _present_surface_unloaded(app_ref, entry)


def _surface_log(app_ref, msg: str) -> None:
    """Best-effort amber log used by the silent-fail diagnostic
    points. Falls back to stdout if app._log isn't reachable."""
    try:
        log = getattr(app_ref, '_log', None)
        if callable(log):
            log(msg, 'amber')
            return
    except Exception:
        pass
    print(msg)


def _present_surface_loaded(app_ref, message: str, action_dicts,
                            target_widget=None) -> None:
    """Render the rich Teacher AI surface. Convert offered-action
    dicts to the tm_teacher_ai action format, defer execution by one
    event-loop tick so the surface animation starts before any heavy
    work blocks the main thread.

    When `target_widget` is a live widget, dock the surface next to it
    via the (previously dormant) tm_teacher_ai.show_near — same bubble,
    plus an arrow pointing at the control. Otherwise render center-screen
    via show_center, exactly as before. show_near is reused as-is."""
    try:
        import tm_teacher_ai
    except Exception as e:
        _surface_log(
            app_ref,
            f"[teacher-ai] _present_surface_loaded: tm_teacher_ai "
            f"import failed: {e}")
        return
    canvas = getattr(app_ref, 'content_canvas', None)
    if canvas is None:
        _surface_log(
            app_ref,
            "[teacher-ai] _present_surface_loaded: content_canvas "
            "not available — surface attempt abandoned")
        return

    root = getattr(app_ref, 'root', None)

    def _defer(ad):
        def _cb():
            try:
                if root is not None:
                    root.after(10,
                                lambda: execute_offered_action(ad, app_ref))
                else:
                    execute_offered_action(ad, app_ref)
            except Exception:
                try:
                    execute_offered_action(ad, app_ref)
                except Exception:
                    pass
        return _cb

    surface_actions = []
    for ad in action_dicts:
        if not isinstance(ad, dict):
            continue
        surface_actions.append({
            'label': ad.get('label') or 'OK',
            'callback': _defer(ad),
        })

    # Dock next to a control when one is given (proactive feature
    # intros), else center-screen (every existing caller). show_near is
    # reused verbatim — it translates the widget's screen rect into
    # canvas coords (toolbar/canvas-child agnostic) and draws the arrow.
    if target_widget is not None:
        try:
            tm_teacher_ai.show_near(canvas, target_widget, message,
                                    actions=surface_actions)
            return
        except Exception as e:
            # Never strand the message: fall back to center-screen.
            _surface_log(
                app_ref,
                f"[teacher-ai] show_near raised, falling back to center: "
                f"{type(e).__name__}: {e}")
    try:
        tm_teacher_ai.show_center(canvas, message, actions=surface_actions)
    except Exception as e:
        _surface_log(
            app_ref,
            f"[teacher-ai] show_center raised: "
            f"{type(e).__name__}: {e}")


# ─── v4.14.5.88-close-message: close-time "state, not pressure" surface ───
#
# Wired into App._on_close at the top, BEFORE any teardown step. Renders
# a brief Teacher AI surface that reports pick-relevant fill state and
# reassures the user that closing is fine. NEVER traps a user:
#   - 5-second auto-dismiss → 'proceed' (the X-and-walk-away case ALWAYS
#     gets a closed app).
#   - Any exception in the helper → returns 'skip' (proceed silently).
#   - snapshot ready=True → no surface, returns 'skip' (don't nag).
#   - Render failure (canvas gone, show_center raises) → returns 'skip'.
#   - cfg `use_close_message` flag-off → caller skips this helper entirely
#     (no pause at all, exact pre-v.88 close behavior).
#
# Modal blocking uses tk.BooleanVar + root.wait_variable — the canonical
# Tk modal pattern that re-enters the EXISTING mainloop pumping events.
# It does NOT spin a second mainloop. _on_close is itself dispatched from
# the mainloop (WM_DELETE_WINDOW protocol handler), so re-entry is safe.
#
# The wording is STATE, NOT PRESSURE. No "are you sure," no "needs to run
# for accurate results," no implied data loss. The picks shown ARE current
# — we say so ("you have N picks ready now"). The rest of the universe
# filling in the background is reported as honest progress, not as a
# warning. Auto-dismiss to 'proceed' (not 'cancel') preserves the X-click
# default: walking away closes the app.

# v4.14.5.89-close-message-polish: 60s, not 5s. The timer is a
# TRAP-PREVENTION net (a walked-away user must eventually get a closed
# app), NOT a "hurry up" timer that nudges a present user to decide.
# v.88 used 5s, which reliably released a walked-away user but trapped
# a present-and-reading one — the app could close out from under
# someone reading the line. 60s is long enough that a present reader
# reliably gets to choose; the trap-prevention property is identical
# at 60s as at 5s (the user just isn't there to be released).
_CLOSE_MESSAGE_AUTODISMISS_MS = 60_000


def _build_close_message_copy(snapshot: dict) -> str:
    """v4.14.5.89-close-message-polish: first-person assistant voice.

    The v.88 copy led with a synthetic "Still scanning\\n\\n" header
    and described the system in third person ("The background scan is
    still filling in..."). Stacked under the framework's standard
    "Tired Market AI" header label, that read as a titled-modal-dialog
    rather than the AI noticing something. This version drops the
    synthetic header (the framework already labels the speaker) and
    speaks in first person — same animated `show_center` surface, but
    the framing reads as the assistant.

    Two cases (the v.89 trigger fix means we ONLY enter this function
    when there's a genuine "still filling" story to tell):

      - pick_count > 0 with daily_bars < 100% → "Heads up — I've got
        N picks ready for you so far. I'm still pulling in the latest
        price data {and news} in the background, but you're good to
        close whenever; I'll pick up where I left off next time."

      - pick_count == 0 (fresh install before first picks land) →
        "Hang tight — I'm still working on your first picks. You're
        good to close whenever; I'll pick up where I left off next
        time."

    The case where daily_bars is 100% AND pick_count > 0 — even if
    news priority is still trailing — does NOT enter this function;
    `maybe_show_close_message` returns 'skip' before calling here.
    See the trigger-logic comment there.

    Audit-pinned wording rules (carried forward from v.88):
      - MUST contain a reassuring "picks ready" / "first picks"
        phrase (audit asserts).
      - MUST NOT contain "accurate results" / "are you sure" /
        "incomplete" / "will be wrong" / "unsafe" / "untrustworthy"
        / "don't close" / word-boundary "lose"/"lost" / "wait until"
        / "incorrect" (audit asserts). The picks shown ARE current
        — the copy says so, in the assistant's voice.
    """
    try:
        pick_count = int(snapshot.get('pick_count') or 0)
    except (TypeError, ValueError):
        pick_count = 0
    npc = bool(snapshot.get('news_priority_complete'))

    if pick_count <= 0:
        # Fresh-install / pre-first-picks. Don't fake a count; pivot
        # to "still working on your first picks." Same reassurance
        # tail (close anytime / resumes next time).
        return (
            "Hang tight — I'm still working on your first picks. "
            "You're good to close whenever; I'll pick up where I "
            "left off next time."
        )
    # Healthy pick count but pick-relevant price data still filling.
    # If news priority is also incomplete we mention it; if news is
    # already in, we just say "the latest price data."
    pulling_bit = ("the latest price data and news"
                   if not npc
                   else "the latest price data")
    picks_word = "pick" if pick_count == 1 else "picks"
    return (
        f"Heads up — I've got {pick_count} {picks_word} ready for "
        f"you so far. I'm still pulling in {pulling_bit} in the "
        f"background, but you're good to close whenever; I'll pick "
        f"up where I left off next time."
    )


def maybe_show_close_message(app) -> str:
    """Called at the TOP of App._on_close, BEFORE any teardown step.

    Returns one of:
      'proceed' — show the message, user chose Close now (or 5s timer
                  fired). Caller continues teardown.
      'cancel'  — user chose Keep open. Caller MUST return from
                  _on_close without tearing down — nothing was stopped.
      'skip'    — not meaningful / couldn't compute / render failed.
                  Caller continues teardown silently (no surface shown).

    Safety guarantees (audit-asserted):
      - Any exception falls through to 'skip'. Never raises out.
      - 5s auto-dismiss is mandatory. No code path waits indefinitely.
      - Re-entrant: second call after a surface has been shown returns
        'skip' immediately. The `app._close_message_shown` flag carries
        across cancel → re-close, by design (user already heard the
        state once; second close = they accept).
      - When `ready=True` per the readiness snapshot, returns 'skip'
        without rendering. Don't nag users whose picks are current.
    """
    try:
        # ─── Re-entrancy guard ───
        if getattr(app, '_close_message_shown', False):
            return 'skip'

        # ─── Snapshot gate ───
        try:
            import tm_readiness
            snap = tm_readiness.get_readiness_snapshot(app)
        except Exception:
            return 'skip'
        if not isinstance(snap, dict):
            return 'skip'
        if snap.get('ready'):
            # Picks current per the v.87 conservative rule — close
            # silently.
            return 'skip'
        # v4.14.5.89-close-message-polish: EFFECTIVE-READY skip.
        # The v.87 `ready` rule is conservative (needs both
        # daily_bars==100 AND news_priority_complete). But news-tail-
        # only-incomplete with prices in and a healthy displayed-pick
        # count means the user is effectively ready to act — there's
        # nothing they can see or do that's affected by the trailing
        # news fill. v.88 fired "Still scanning" in this state, which
        # contradicted the body's "100% of price data, N picks ready"
        # — the header lied. v.89 skips silently here. The "still
        # filling" message stays for the genuine case where prices
        # themselves are still pending (daily_bars < 100) OR there
        # are no picks yet (fresh install).
        try:
            _db_pct = (snap.get('lanes') or {}).get(
                'daily_bars', {}).get('pct')
            _pick_n = int(snap.get('pick_count') or 0)
        except Exception:
            _db_pct, _pick_n = None, 0
        try:
            _db_complete = (_db_pct is not None
                            and float(_db_pct) >= 100.0)
        except (TypeError, ValueError):
            _db_complete = False
        if _db_complete and _pick_n > 0:
            return 'skip'

        # ─── Surface prereqs ───
        canvas = getattr(app, 'content_canvas', None)
        root = getattr(app, 'root', None)
        if canvas is None or root is None:
            return 'skip'
        try:
            import tkinter as tk
            import tm_teacher_ai
        except Exception:
            return 'skip'

        # Mark shown EARLY so a parallel close-trigger can't double-fire
        # the surface. Set before any work that might block.
        try:
            setattr(app, '_close_message_shown', True)
        except Exception:
            pass

        message = _build_close_message_copy(snap)

        # The result var. The first writer wins; subsequent _resolve
        # calls (e.g. the timer firing AFTER a click) no-op.
        result_var = tk.StringVar(master=root, value='')
        timer_holder = {'id': None}

        def _resolve(choice: str) -> None:
            try:
                tid = timer_holder.get('id')
                if tid is not None:
                    try:
                        root.after_cancel(tid)
                    except Exception:
                        pass
                    timer_holder['id'] = None
            except Exception:
                pass
            try:
                if not result_var.get():
                    result_var.set(choice)
            except Exception:
                pass

        # v4.14.5.89-close-message-polish: "Stay open" reads more
        # naturally than "Keep open" (which sounds like a fridge).
        # The label is the only thing that changed — callback
        # semantics are identical.
        actions = [
            {'label': 'Close now',
             'callback': lambda: _resolve('proceed')},
            {'label': 'Stay open',
             'callback': lambda: _resolve('cancel')},
        ]

        # Render. If show_center raises, fall through to 'skip' — the
        # close path proceeds normally.
        try:
            tm_teacher_ai.show_center(canvas, message, actions=actions)
        except Exception:
            return 'skip'

        # Mandatory auto-dismiss timer → 'proceed' (the walk-away path
        # ALWAYS closes the app — that's the safety property).
        try:
            timer_holder['id'] = root.after(
                _CLOSE_MESSAGE_AUTODISMISS_MS,
                lambda: _resolve('proceed'))
        except Exception:
            # Couldn't schedule the timer — refuse to block on an open
            # surface that can't auto-resolve. Dismiss + skip.
            try:
                tm_teacher_ai.dismiss_active(canvas)
            except Exception:
                pass
            return 'skip'

        # Block on the existing mainloop until result_var is set.
        # wait_variable re-enters the running mainloop processing events
        # (button clicks, the timer's after callback, animation frames)
        # — it does NOT spin a second mainloop.
        try:
            root.wait_variable(result_var)
        except Exception:
            # Couldn't wait — dismiss the surface and skip.
            try:
                tm_teacher_ai.dismiss_active(canvas)
            except Exception:
                pass
            return 'skip'

        # Force-dismiss the surface in case the action-click path didn't
        # auto-dismiss (timer path, or replaced surface).
        try:
            tm_teacher_ai.dismiss_active(canvas)
        except Exception:
            pass

        choice = result_var.get() or 'proceed'
        if choice not in ('proceed', 'cancel'):
            # Defensive: any unexpected value treats as 'proceed' so
            # the app still closes.
            return 'proceed'
        return choice
    except Exception:
        # Last-ditch catch — under no circumstance does this raise.
        return 'skip'


def _present_surface_unloaded(app_ref, entry: dict) -> None:
    """Fallback surface when tm_teacher_ai isn't importable. Renders
    the entry's short_hint + manual_fallback as a tk messagebox.
    info-severity entries stay silent (no surface in unloaded mode);
    blocking and soft_warn both fire showwarning."""
    if not entry:
        return
    sev = entry.get('severity') or ''
    if sev == 'info':
        return
    short = entry.get('short_hint') or ''
    fallback = entry.get('manual_fallback') or ''
    body_parts = [s for s in (short, fallback) if s]
    if not body_parts:
        return
    body = "\n\n".join(body_parts)
    title = "Action blocked" if sev == 'blocking' else "Heads up"
    try:
        from tkinter import messagebox
        messagebox.showwarning(title, body)
    except Exception as e:
        _surface_log(
            app_ref,
            f"[teacher-ai] showwarning raised: "
            f"{type(e).__name__}: {e}")
