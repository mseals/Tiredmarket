"""tm_teacher_brain — Teacher AI Phase 2 cloud reasoning brain (v1).

The reasoning brain behind the existing "Ask Tired Market AI" surface
(Ctrl+`). Given a free-form question, it:
  1. retrieves relevant cheat-sheet slices (faq / features / playbook),
  2. assembles a COMPACT user-state summary (portfolio / recommendations /
     a named ticker's recent prediction),
  3. builds a boundary-encoded, PROSE-ONLY teacher prompt,
  4. makes a SINGLE fast-provider call via the router's provider-call path
     (Groq primary, failover down the fast list, stop on first success),
  5. returns the prose answer.

ISOLATION (critical): this reuses ONLY the router selection + the
`tm_api_providers.call_provider` HTTP primitive. It writes NO prediction /
signal rows and touches NO accuracy / track-record state — a Q&A turn is
ephemeral conversation, not a tracked stock prediction. (Contrast
`lookup_explain`, which deliberately reuses fresh_buy SIGNAL storage.)

ADVICE BOUNDARY: the prompt encodes the app's existing not-financial-advice
framing (educational; explain what the engine computed and why; teach how to
read it; NO personalized buy/sell directives; honest about uncertainty; if
data is missing, say so). It is PROSE-ONLY — no per-ticker verdict labels
(DIRECTION / TARGET / STOP_LOSS), which would be trade calls.

v1 scope: single-turn, no streaming, no offered-action envelope, no local
embedded model — all deferred. FAQ stays the graceful fallback in the surface
when this returns (False, None).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional, Callable

# Keep the grounding context bounded so it fits a fast provider's window
# comfortably (and a small-context failover like Cerebras ~8K if reached).
TEACHER_SLICES_CHAR_CAP = 9000        # cheat-sheet slices budget
TEACHER_USERSTATE_CHAR_CAP = 2500     # compact user-state budget
_MAX_FEATURE_SLICES = 6
_MAX_PLAYBOOK_SLICES = 4
_MAX_FAQ_SLICES = 6

# The advice boundary, drawn from the app's own disclaimer
# (tired_market.py:9455-9461 / :9317 / :4322-4330). The prompt must stay
# inside this.
TEACHER_SYSTEM_FRAMING = (
    "You are Tired Market AI, the built-in helper inside the Tired Market "
    "stock-analysis app. You explain how the app works and what its engine "
    "computed and why, in plain, friendly language.\n\n"
    "HARD RULES (never break these):\n"
    "- EDUCATIONAL ONLY. Tired Market is for educational and research use; it "
    "is NOT a registered investment advisor or broker. Your answers do NOT "
    "constitute financial advice.\n"
    "- NEVER give a personalized buy/sell/hold directive. Do not tell the user "
    "to buy, sell, or hold anything. You may EXPLAIN what the engine's "
    "consensus/verdict says and WHY, and TEACH the user how to read it — but "
    "the decision is always theirs. ('I help you think; I won't think for "
    "you.')\n"
    "- BE HONEST ABOUT UNCERTAINTY. The engine is wrong sometimes; say so. No "
    "model can reliably predict any stock.\n"
    "- DON'T MAKE UP NUMBERS OR FACTS. If the data needed to answer isn't in "
    "the context below, say you don't have it and point the user to the "
    "relevant feature instead of guessing.\n"
    "- Stay on Tired Market and general investing-literacy topics. Point at "
    "the app's features when relevant."
)

TEACHER_ANSWER_GUIDE = (
    "Answer the user's question in clear plain prose (a few short paragraphs "
    "or bullets — no rigid template, no JSON). Ground your answer in the "
    "context above. Explain the 'why', teach how to read it, and name the "
    "relevant app feature if useful. Remember the hard rules: educational "
    "only, no buy/sell/hold directives, honest about uncertainty, no made-up "
    "numbers."
)

# Verdict labels that must NEVER appear in a teacher answer (they're per-ticker
# trade calls). Used by the parser to strip any that a model leaks in.
_VERDICT_LABEL_RE = re.compile(
    r'^\s*(DIRECTION|BUY_ZONE|TARGET|STOP_LOSS|STOP|TIMEFRAME|CONFIDENCE|'
    r'REASON_ONE_LINE)\s*:.*$',
    re.IGNORECASE | re.MULTILINE)


def _internal_dir() -> Path:
    return __import__('tm_paths').get_app_asset_dir() / 'internal'


def _load_json(name: str) -> dict:
    try:
        p = _internal_dir() / name
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r'[a-z0-9]+', (text or '').lower())
            if len(w) > 2]


def _score(query_tokens: set, hay: str) -> int:
    h = (hay or '').lower()
    return sum(1 for t in query_tokens if t in h)


def select_cheat_sheet_slices(question: str) -> str:
    """Keyword-select the cheat-sheet entries most relevant to `question`
    from faq.json / features.json / error_recovery_playbook.json, capped to
    TEACHER_SLICES_CHAR_CAP. Reuses a simple keyword-overlap score (same
    family as tm_teacher_ai.match_faq_entry) — the full ~139KB set is far too
    large to inline, so we ground on a relevant slice. Never raises."""
    qt = set(_tokens(question))
    if not qt:
        return ""
    sections: list[str] = []
    budget = TEACHER_SLICES_CHAR_CAP

    def _emit(header: str, lines: list[str]) -> None:
        nonlocal budget
        if not lines:
            return
        block = header + "\n" + "\n".join(lines)
        if len(block) <= budget:
            sections.append(block)
            budget -= len(block)

    # FAQ (small, high-signal): question + answer
    try:
        faq = _load_json('faq.json').get('entries') or []
        scored = sorted(
            ((_score(qt, e.get('question', '') + ' '
                     + ' '.join(e.get('keywords') or []) + ' '
                     + e.get('answer', '')), e) for e in faq),
            key=lambda x: x[0], reverse=True)
        lines = [f"Q: {e.get('question','')}\nA: {e.get('answer','')}"
                 for s, e in scored[:_MAX_FAQ_SLICES] if s > 0]
        _emit("HOW THE APP WORKS (FAQ):", lines)
    except Exception:
        pass

    # Features: what each surface does
    try:
        feats = _load_json('features.json').get('entries') or []
        scored = sorted(
            ((_score(qt, e.get('display_name', '') + ' '
                     + e.get('short_description', '') + ' '
                     + e.get('long_description', '') + ' '
                     + e.get('category', '')), e) for e in feats),
            key=lambda x: x[0], reverse=True)
        lines = [f"- {e.get('display_name','')}: "
                 f"{e.get('short_description','') or e.get('long_description','')}"
                 for s, e in scored[:_MAX_FEATURE_SLICES] if s > 0]
        _emit("FEATURES:", lines)
    except Exception:
        pass

    # Playbook: troubleshooting / why-did-X
    try:
        pb = _load_json('error_recovery_playbook.json').get('entries') or []
        scored = sorted(
            ((_score(qt, e.get('short_hint', '') + ' '
                     + e.get('diagnostic', '') + ' '
                     + e.get('recommendation', '')), e) for e in pb),
            key=lambda x: x[0], reverse=True)
        lines = [f"- {e.get('short_hint','')} "
                 f"({e.get('recommendation','')})"
                 for s, e in scored[:_MAX_PLAYBOOK_SLICES] if s > 0]
        _emit("TROUBLESHOOTING:", lines)
    except Exception:
        pass

    return "\n\n".join(sections)


_TICKER_RE = re.compile(r'\b[A-Z]{1,5}\b')
_TICKER_STOPWORDS = {
    'A', 'I', 'AI', 'THE', 'IS', 'IT', 'ON', 'IN', 'OF', 'TO', 'MY', 'DO',
    'BUY', 'SELL', 'HOLD', 'WHY', 'HOW', 'AND', 'OR', 'FISV',  # FISV kept below
}
# FISV is a real ticker — don't let the stopword set hide it.
_TICKER_STOPWORDS.discard('FISV')


def build_user_state_summary(app) -> str:
    """Compact, best-effort snapshot of the user's actual state so the brain
    can answer 'why is X a hold?' / 'what does my Track Record show?' in
    context. Reads (never writes) the App's existing handles. Each section is
    defensive; any failure simply omits that section. Bounded to
    TEACHER_USERSTATE_CHAR_CAP. Never raises."""
    parts: list[str] = []
    try:
        hs = getattr(app, '_holdings_state', None) or {}
    except Exception:
        hs = {}

    # Portfolio (holdings + cash)
    try:
        mgr = hs.get('mgr') if isinstance(hs, dict) else None
        if mgr is not None:
            holds = list(getattr(mgr, 'holdings', []) or [])
            cash = None
            try:
                cash = mgr.get_cash()
            except Exception:
                cash = None
            if holds:
                rows = []
                for h in holds[:25]:
                    tk = (h.get('ticker') or '?').upper()
                    sh = h.get('shares')
                    rows.append(f"{tk} ({sh} sh)" if sh else tk)
                line = "PORTFOLIO: holds " + ", ".join(rows)
                if cash is not None:
                    line += f"; cash ${cash:,.0f}"
                parts.append(line)
            else:
                parts.append("PORTFOLIO: no holdings recorded"
                             + (f"; cash ${cash:,.0f}" if cash is not None
                                else ""))
    except Exception:
        pass

    # Current recommendations (best-effort, configured path)
    try:
        path = None
        try:
            path = (app.cfg.get('analysis_path')
                    or app.cfg.get('path') or 'moderate')
        except Exception:
            path = 'moderate'
        picks = []
        if hasattr(app, '_read_recommend_cache_picks'):
            picks = app._read_recommend_cache_picks(path) or []
        tks = []
        for p in picks[:10]:
            t = (p.get('ticker') if isinstance(p, dict) else None)
            if t:
                tks.append(str(t).upper())
        if tks:
            parts.append(f"CURRENT RECOMMENDATIONS ({path}): "
                         + ", ".join(tks))
    except Exception:
        pass

    # Named-ticker recent prediction (if the question references one)
    try:
        plog = hs.get('predictions_log') if isinstance(hs, dict) else None
        tickers = getattr(build_user_state_summary, '_q_tickers', None)
        if plog is not None and tickers:
            recs = []
            try:
                allp = plog.get_all() or []
            except Exception:
                allp = []
            for tk in list(tickers)[:2]:
                latest = None
                for r in allp:
                    if (r.get('ticker') or '').upper() == tk:
                        latest = r  # last wins (file is append-order)
                if latest is not None:
                    recs.append(
                        f"{tk}: latest engine call "
                        f"{latest.get('direction','?')} "
                        f"(status {latest.get('status','?')}, "
                        f"model {latest.get('canonical_model') or latest.get('model','?')})")
            if recs:
                parts.append("NAMED TICKER HISTORY: " + "; ".join(recs))
    except Exception:
        pass

    text = "\n".join(parts)
    if len(text) > TEACHER_USERSTATE_CHAR_CAP:
        text = text[:TEACHER_USERSTATE_CHAR_CAP] + " …(truncated)"
    return text


def _extract_tickers(question: str) -> list[str]:
    out = []
    for m in _TICKER_RE.findall(question or ''):
        if m not in _TICKER_STOPWORDS and m not in out:
            out.append(m)
    return out


def build_teacher_prompt(question: str, slices_text: str,
                         user_state_text: str) -> str:
    """Assemble the PROSE-ONLY teacher prompt: boundary framing + retrieved
    cheat-sheet slices + compact user-state + the question + answer guide.
    Contains NO per-ticker verdict labels (those are trade calls)."""
    blocks = [TEACHER_SYSTEM_FRAMING]
    if user_state_text:
        blocks.append("THE USER'S CURRENT STATE (read-only snapshot):\n"
                      + user_state_text)
    if slices_text:
        blocks.append("RELEVANT APP KNOWLEDGE:\n" + slices_text)
    blocks.append("USER QUESTION:\n" + (question or '').strip())
    blocks.append(TEACHER_ANSWER_GUIDE)
    return "\n\n".join(blocks)


def parse_teacher_response(response: str) -> str:
    """Trivial parser: return the prose answer, stripped. Defensively removes
    any per-ticker verdict labels a model might leak in (the prompt never asks
    for them). Never raises."""
    try:
        txt = (response or '').strip()
        if not txt:
            return ""
        txt = _VERDICT_LABEL_RE.sub('', txt)
        # collapse the blank lines a stripped label may leave behind
        txt = re.sub(r'\n{3,}', '\n\n', txt).strip()
        return txt
    except Exception:
        return (response or '').strip()


def answer_question(app, question: str, *,
                    call_fn: Optional[Callable] = None,
                    providers: Optional[list] = None,
                    log_fn: Optional[Callable] = None,
                    timeout: float = 30.0) -> tuple[bool, Optional[str]]:
    """Answer a free-form question with the cloud reasoning brain.

    Returns (True, prose) on the first successful provider call, or
    (False, None) on no question / no eligible provider / all calls failed —
    the surface then falls back to the canned FAQ answer.

    SINGLE successful call with failover (NOT consensus fan-out). Reuses ONLY
    the router selection + `call_provider` HTTP path; writes NO prediction /
    signal / accuracy state. `call_fn` / `providers` are injectable for tests.
    """
    q = (question or '').strip()
    if not q:
        return (False, None)

    # Stash detected tickers for the user-state summary (named-ticker history).
    try:
        build_user_state_summary._q_tickers = _extract_tickers(q)
    except Exception:
        pass

    try:
        slices_text = select_cheat_sheet_slices(q)
    except Exception:
        slices_text = ""
    try:
        user_state = build_user_state_summary(app)
    except Exception:
        user_state = ""
    prompt = build_teacher_prompt(q, slices_text, user_state)

    # Provider order (single fast pick + failover) via the router.
    if providers is None:
        try:
            import tm_api_providers as _tmap
            providers = _tmap.load_enabled_providers()
        except Exception:
            providers = []
    try:
        import tm_ai_router as _R
        order = _R.select_teacher_provider_order(providers, log_fn=log_fn)
    except Exception:
        order = list(providers or [])
    if not order:
        return (False, None)

    if call_fn is None:
        try:
            import tm_api_providers as _tmap
            call_fn = _tmap.call_provider
        except Exception:
            return (False, None)

    # ONE successful call; walk the fast list only on failure. No prediction
    # or accuracy writes anywhere on this path.
    for prov in order:
        try:
            resp = call_fn(prov, prompt, timeout=timeout, log_fn=log_fn)
        except Exception:
            continue  # failover to the next fast provider
        text = parse_teacher_response(resp)
        if text:
            return (True, text)
    return (False, None)
