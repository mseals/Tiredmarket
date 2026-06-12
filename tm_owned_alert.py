"""tm_owned_alert — owned-position WATCH surfacing rules (v4.14.5.64).

SINGLE SOURCE OF TRUTH for how the cloud on-event watcher's per-holding
verdict is classified into a SEVERITY (ACTIONABLE / NOTABLE / ROUTINE),
and what plain-language text the toast/badge surfaces use.

Why this file exists
--------------------
Before v4.14.5.64 the owned-position watcher detected an event, ran a
multi-model consensus, formed a verdict — and then died silently. The
only effect was a transient green activity-log line and a passive
Portfolio re-render. ``send_notification`` (Windows toast, defined in
``tired_market.py``) was never called from anywhere in the codebase.
This module is the missing classifier that decides which results are
worth interrupting the user for and which ones can sit quietly.

Program-owned config: the program decides these rules; the Teacher AI
only EXPLAINS the result. The rules are NOT writable by the AI.

Severity meanings
-----------------
ACTIONABLE  Consensus is SELL/TRIM — the watcher is reporting that the
            owned-position thesis has flipped to "you might want to sell
            faster than you thought." Surfaces: Windows toast (transient)
            PLUS a persistent Portfolio badge (durable until cleared).

NOTABLE     A material verdict change short of a SELL — e.g. BUY -> HOLD,
            HOLD -> BUY MORE. Surfaces: persistent Portfolio badge only,
            no toast interrupt.

ROUTINE     Re-checked, verdict unchanged (or unclassifiable). Surfaces:
            nothing new (the existing activity-log line is fine). Clears
            any prior unacknowledged alert as a freshness signal.

The classifier is deliberately CONSERVATIVE: when in doubt, ROUTINE. A
firehose of toasts would train the user to dismiss them, which is the
opposite of the goal. We surface the result the *program* reached; we
do NOT tell the user what to do.
"""

from __future__ import annotations

from typing import Any, Optional


# ── Severity values ──────────────────────────────────────────────────
ACTIONABLE = 'ACTIONABLE'
NOTABLE = 'NOTABLE'
ROUTINE = 'ROUTINE'

# Directions the consensus parser emits. SELL and TRIM are the two that
# mean "the program thinks you might need to lighten this position."
_SELL_DIRS = {'SELL', 'TRIM'}
# Directions that mean the program still likes the position.
_KEEP_DIRS = {'HOLD', 'BUY', 'BUY MORE', 'BUYMORE'}


def _norm(d) -> str:
    """Normalize a direction string for comparison. Empty/None -> ''."""
    try:
        return str(d or '').strip().upper().replace('  ', ' ')
    except Exception:
        return ''


def winner_from_result(result: dict) -> str:
    """Return the consensus winning direction, preferring the weighted
    tally (matches what's displayed in the panel) and falling back to
    the first vote with a direction. Never raises -> '' on any fault.
    """
    try:
        if not isinstance(result, dict):
            return ''
        tally = result.get('tally') or {}
        w = _norm(tally.get('weighted_winner') or tally.get('raw_winner'))
        if w:
            return w
        for v in (result.get('votes') or []):
            d = _norm(v.get('direction'))
            if d:
                return d
    except Exception:
        pass
    return ''


def classify(prior_verdict: Optional[str],
             new_verdict: str,
             *,
             vote_count: int = 0,
             prior_alert: Optional[dict] = None) -> dict:
    """Decide severity for a freshly-completed owned-position consensus.

    Args:
        prior_verdict: the verdict stored on the holding from the prior
            owned-position consensus (or None if first run).
        new_verdict: the just-computed winner (use winner_from_result).
        vote_count: number of valid votes in this consensus. A single
            vote (or zero) cannot drive an ACTIONABLE; floor to NOTABLE
            so a one-model lottery can't fire a toast. Conservative.
        prior_alert: the unacknowledged alert dict already on the
            holding, if any. Used so we don't keep re-firing the toast
            on every recency-gated re-check for the same SELL state.

    Returns:
        dict {'severity': ACTIONABLE|NOTABLE|ROUTINE,
              'code': short stable code,
              'verdict': new_verdict,
              'prior_verdict': prior_verdict or '',
              'reason': plain-language one-line description}

    Always returns a dict; never raises.
    """
    pv = _norm(prior_verdict)
    nv = _norm(new_verdict)

    # Empty / unclassifiable new verdict -> ROUTINE. The watcher already
    # ran, but if the parser couldn't extract a direction there is
    # nothing to surface honestly.
    if not nv:
        return {
            'severity': ROUTINE, 'code': 'NO_VERDICT',
            'verdict': '', 'prior_verdict': pv,
            'reason': 'no clear verdict extracted from this consensus',
        }

    # SELL / TRIM consensus -> ACTIONABLE, but require >=2 votes so a
    # single-model fluke can never trigger a Windows toast. With 0 or 1
    # vote we still surface it as NOTABLE (badge only, no interrupt).
    if nv in _SELL_DIRS:
        if vote_count >= 2:
            severity = ACTIONABLE
        else:
            severity = NOTABLE
        # Suppress re-firing the toast every recheck when the prior
        # alert was already ACTIONABLE for the same direction.
        if (severity == ACTIONABLE and prior_alert
                and _norm(prior_alert.get('verdict')) == nv
                and prior_alert.get('severity') == ACTIONABLE):
            severity = NOTABLE  # badge stays, no fresh toast
        return {
            'severity': severity,
            'code': 'SELL_CONSENSUS' if nv == 'SELL' else 'TRIM_CONSENSUS',
            'verdict': nv, 'prior_verdict': pv,
            'reason': (f"consensus moved to {nv}"
                       if pv and pv != nv
                       else f"consensus is {nv}"),
        }

    # Material verdict change (not a sell) -> NOTABLE. Examples:
    #   BUY -> HOLD          (program cooled on the thesis)
    #   HOLD -> BUY MORE     (program warmed up)
    #   BUY MORE -> HOLD     (program cooled)
    # Same-bucket transitions (BUY -> BUY MORE) are NOT notable on
    # their own -- both keep the position. We only fire on cross-bucket
    # changes to keep the badge meaningful.
    if pv and pv in _KEEP_DIRS and nv in _KEEP_DIRS and pv != nv:
        pv_buy = pv in ('BUY', 'BUY MORE', 'BUYMORE')
        nv_buy = nv in ('BUY', 'BUY MORE', 'BUYMORE')
        if pv_buy != nv_buy:
            return {
                'severity': NOTABLE, 'code': 'VERDICT_CHANGED',
                'verdict': nv, 'prior_verdict': pv,
                'reason': f"verdict changed: {pv} -> {nv}",
            }

    # Anything else is ROUTINE: a non-sell verdict, unchanged or
    # same-bucket, on a holding the program still expects you to keep.
    return {
        'severity': ROUTINE, 'code': 'STABLE',
        'verdict': nv, 'prior_verdict': pv,
        'reason': (f"verdict unchanged ({nv})" if pv == nv or not pv
                   else f"verdict: {nv}"),
    }


def toast_text(ticker: str, alert: dict) -> tuple:
    """Compose plain-language (title, message) for the Windows toast.

    Reports what the *program* concluded. Does NOT tell the user what to do.
    """
    try:
        tk = str(ticker or '?').upper()
        sev = alert.get('severity', ROUTINE)
        v = _norm(alert.get('verdict'))
        pv = _norm(alert.get('prior_verdict'))
        if sev == ACTIONABLE:
            title = f"Tired Market - {tk}: consensus {v}"
            if pv and pv != v:
                msg = (f"The watcher's consensus moved {pv} -> {v}. "
                       f"Open Portfolio to see the breakdown.")
            else:
                msg = (f"The watcher's consensus is {v}. Open Portfolio "
                       f"to see the breakdown.")
            return title, msg
        # NOTABLE / ROUTINE: no toast normally, but keep a usable
        # composer for callers that want the same text on the badge.
        title = f"Tired Market - {tk}: {v or 'updated'}"
        msg = alert.get('reason') or ''
        return title, msg
    except Exception:
        return f"Tired Market - {ticker}", "Owned-position consensus updated."


def badge_text(alert: dict) -> tuple:
    """Compose (short_label, color_key) for the Portfolio panel badge.

    color_key is a key into the panel's color map; 'red' for actionable,
    'amber' for notable. Returns ('', None) when there is nothing to
    surface (severity ROUTINE).
    """
    try:
        sev = alert.get('severity', ROUTINE)
        v = _norm(alert.get('verdict'))
        pv = _norm(alert.get('prior_verdict'))
        if sev == ACTIONABLE:
            if pv and pv != v:
                return (f"ALERT - consensus {pv} -> {v}", 'red')
            return (f"ALERT - consensus {v}", 'red')
        if sev == NOTABLE:
            if alert.get('code') == 'VERDICT_CHANGED' and pv and pv != v:
                return (f"NOTABLE - verdict {pv} -> {v}", 'amber')
            return (f"NOTABLE - consensus {v}", 'amber')
    except Exception:
        pass
    return ('', None)
