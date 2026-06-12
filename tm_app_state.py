"""tm_app_state.py — Stage 6c of the v4.14.0 routing rework.

Per the 2026-05-08 "embedded assistant as primary UI layer" decision in
DECISIONS.md (lines 485-496), v4.14.0 ships an in-process, no-network
state-query API that the embedded local AI assistant will consume when
it lands in v4.14.x+. Nothing reads it yet — the hook is here so the
assistant can plug in cleanly without us refactoring six surfaces at
once when it arrives.

The six questions the assistant needs to answer (DECISIONS.md 488-493):

    1. Is a scan running?
    2. How many candidates remaining?
    3. What's the current AI mode (local / api / hybrid)?
    4. What's the current UI mode (simple / advanced)?
    5. Are there any providers configured?
    6. What does this error mean?

This module exposes one class — AppStateQuery — that the App holds a
reference to. Every read is wrapped in try/except: a broken state source
must not crash the assistant later. When a value genuinely isn't
available right now (e.g. the live scan loop doesn't publish progress
yet), the method returns None rather than fabricating a number.

Stage 6c is purely additive. It pulls from existing App surfaces and
existing helpers in tm_ai_router. It does not refactor scan internals,
re-shape config, or change runtime behavior anywhere.
"""

from __future__ import annotations

from typing import Any, Optional


# ── Plain-English explanations for the three router failure categories
#
# tm_ai_router.classify_failure() returns one of OUTCOME_QUOTA /
# OUTCOME_TRANSIENT / OUTCOME_FATAL. The assistant needs short, plain-
# English copy it can paraphrase to the user. Keep these tight — the
# assistant will rephrase in context.
_ERROR_COPY = {
    'quota': {
        'plain_english': (
            "This provider's daily call limit is reached for today."
        ),
        'recovery_hint': (
            "The router will skip it for the rest of today and try "
            "other providers. Quota resets overnight."
        ),
    },
    'transient': {
        'plain_english': (
            "The provider had a temporary problem (network, timeout, "
            "or 5xx server error)."
        ),
        'recovery_hint': (
            "The router retries on the same provider before failing "
            "over. Usually clears on its own within a minute or two."
        ),
    },
    'fatal': {
        'plain_english': (
            "The provider rejected the call in a way that won't "
            "self-heal (bad key, missing model, malformed request)."
        ),
        'recovery_hint': (
            "Check the provider's API key in Settings, or check "
            "whether the model name is still valid for that provider."
        ),
    },
    'unknown': {
        'plain_english': (
            "The error didn't match any known category."
        ),
        'recovery_hint': (
            "If it keeps happening, capture the exact text and "
            "check the activity log for related lines."
        ),
    },
}


class AppStateQuery:
    """Read-only window into current app state for the embedded
    assistant (v4.14.x+).

    Construction: pass the App instance. We hold a weak reference in
    spirit (we never mutate app state through this object — only read).

    Lifecycle: created once in App.__init__. Lives as long as the App.
    """

    def __init__(self, app: Any) -> None:
        self._app = app
        # Slot for scan progress. The live scan loop in tm_discover.py
        # doesn't publish progress today, and refactoring it just for an
        # absent consumer would be over-engineering. When the assistant
        # lands, we wire one publish_scan_progress() call into the loop
        # body and this method starts returning a real count — no API
        # change for the assistant.
        self._scan_progress: Optional[dict] = None

    # ── Q1: Is a scan running? + Q1b: which kind? ─────────────────

    def is_scan_running(self) -> bool:
        """True if any of the three scan flags on the holdings window
        are set: discover scan, consensus scan, or all-paths sweep."""
        return self.current_scan_kind() is not None

    def current_scan_kind(self) -> Optional[str]:
        """Return 'discover' / 'consensus' / 'all_paths' for the
        currently running scan, or None if nothing is running.

        Reads the same flags the auto-refresh tick already checks
        (tired_market.py around line 2872-2887), so this answer agrees
        with whatever the rest of the app is doing.
        """
        try:
            hw = getattr(self._app, '_holdings_window', None)
            if hw is None:
                return None
            if getattr(hw, '_discover_running', False):
                return 'discover'
            if getattr(hw, '_consensus_running', False):
                return 'consensus'
            if getattr(hw, '_all_paths_running', False):
                return 'all_paths'
            return None
        except Exception:
            return None

    # ── Q2: How many candidates remaining? ───────────────────────

    def publish_scan_progress(self, done: int, total: int,
                                kind: Optional[str] = None) -> None:
        """Called by the scan loop (when wired) to publish progress.
        No call sites today; the assistant will trigger the wire-up.
        """
        try:
            self._scan_progress = {
                'done': int(done),
                'total': int(total),
                'kind': kind,
            }
        except Exception:
            self._scan_progress = None

    def clear_scan_progress(self) -> None:
        """Called by the scan loop on completion or cancel."""
        self._scan_progress = None

    def candidates_remaining(self) -> Optional[int]:
        """Return remaining count if a scan is publishing progress, or
        None if no scan is running / progress is not being published.

        None is the honest answer — the live discover loop doesn't
        publish today. The assistant should treat None as "I don't have
        a number for you right now" rather than zero.
        """
        try:
            if not self.is_scan_running():
                return None
            sp = self._scan_progress
            if not sp:
                return None
            done = int(sp.get('done', 0))
            total = int(sp.get('total', 0))
            return max(0, total - done)
        except Exception:
            return None

    # ── Q3: AI mode ─────────────────────────────────────────────

    def ai_mode(self) -> str:
        """Return 'local' / 'api' / 'hybrid'. Defaults to 'api' for
        unconfigured installs, matching _get_inference_settings()."""
        try:
            mode, _games = self._app._get_inference_settings()
            return mode or 'api'
        except Exception:
            return 'api'

    # ── Q4: UI mode ─────────────────────────────────────────────

    def ui_mode(self) -> str:
        """Return 'simple' / 'advanced'. Defaults to 'simple' for
        unconfigured installs, matching _get_ui_mode()."""
        try:
            return self._app._get_ui_mode()
        except Exception:
            return 'simple'

    # ── Q5: Providers configured? ───────────────────────────────

    def providers_configured(self) -> list:
        """Return the list of currently enabled API provider IDs.
        Empty list means no cloud providers are configured (the app
        will run local-only or refuse depending on AI mode).
        """
        try:
            loader = getattr(self._app, '_load_enabled_api_providers',
                              None)
            if loader is None:
                return []
            result = loader() or []
            return list(result)
        except Exception:
            return []

    def has_providers_configured(self) -> bool:
        """Convenience: True if at least one provider is enabled."""
        return bool(self.providers_configured())

    # ── Q6: Explain error ───────────────────────────────────────

    def explain_error(self, *,
                       status_code: Optional[int] = None,
                       error_text: str = "",
                       exception: Optional[BaseException] = None
                       ) -> dict:
        """Classify a failure and return a dict the assistant can read:

            {
              'category':       'quota' | 'transient' | 'fatal' | 'unknown',
              'plain_english':  short one-line explanation,
              'recovery_hint':  what'll happen / what user can do,
            }

        Wraps tm_ai_router.classify_failure() so the assistant uses the
        same triage as the live router. If the router import fails (it
        shouldn't, but be paranoid), returns the 'unknown' bucket.
        """
        category = 'unknown'
        try:
            import tm_ai_router as _r
            category = _r.classify_failure(
                status_code=status_code,
                error_text=error_text or '',
                exception=exception,
            )
            if category not in _ERROR_COPY:
                category = 'unknown'
        except Exception:
            category = 'unknown'

        copy = _ERROR_COPY.get(category, _ERROR_COPY['unknown'])
        return {
            'category': category,
            'plain_english': copy['plain_english'],
            'recovery_hint': copy['recovery_hint'],
        }

    # ── Bundle: snapshot for first context-grab ─────────────────

    def snapshot(self) -> dict:
        """One-shot dict the assistant can read on first turn to seed
        its context. Cheap to call — every value is read on demand."""
        return {
            'scan': {
                'running': self.is_scan_running(),
                'kind': self.current_scan_kind(),
                'candidates_remaining': self.candidates_remaining(),
            },
            'ai_mode': self.ai_mode(),
            'ui_mode': self.ui_mode(),
            'providers': {
                'enabled': self.providers_configured(),
                'count': len(self.providers_configured()),
            },
        }
