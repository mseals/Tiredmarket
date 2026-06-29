"""tm_teacher_retrieval — Teacher AI build slice 3: classify + retrieve + HARD GATE.

The structural fix for slice 2's finding (a 1.5B confabulates app facts / stock
specifics / advice even when the identity sheet forbids it). A cheap, non-model
classifier sorts each question by KIND; retrieval pulls the matching cheat-sheet
chunk; a hard gate decides what — if anything — the model is allowed to answer:

  KIND            GATE
  advice          -> CANNED educational deflection (model NOT called; no invented verdict)
  accuracy/trust  -> CANNED humility frame        (model NOT called)
  app_fact + chunk-> model answers FROM the chunk  (grounded; constrained)
  app_fact + none -> CANNED "I don't have that yet" (model NOT called; no confabulation)
  stock_specific  -> CANNED "no analysis in front of me yet" (data-feed is a later slice)
  general         -> model answers from its own knowledge (proven clean in slice 2)
  unknown         -> treated as app_fact (grounded-or-refuse) — never free generation

So the model is invoked ONLY for (a) grounded app-facts and (b) general
knowledge. It never free-generates app-specifics, stock-specifics, advice, or
accuracy claims — those are structural, not left to the sheet to hold.

Proven on data/internal/faq.json first (15 keyworded entries). Reuses the
existing tm_teacher_ai.match_faq_entry keyword primitive. NOT wired to the UI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
TEACHER_RETRIEVAL_CHAR_CAP = 2000  # grounding context bound (tight local window)

# ── canned, structural responses (consistent with teacher_identity.md) ──
CANNED_ADVICE = (
    "I won't tell you whether to buy or sell — that's your decision. I can "
    "show you what the analysis says and help you understand it. (When the app "
    "shows a verdict like \"BUY,\" that's a label for which way the analysis "
    "leans, not an instruction.)")
# v4.15.0-teacher-features (decision #3): authored + honestly NEGATIVE.
# NEVER model-generated — self-assessment is the one thing a model can't be
# trusted to report truthfully. (Later: cite the real Track Record.)
CANNED_ACCURACY = (
    "Nothing that points at the stock market should be trusted 100%, "
    "including me. I'm free, I'm wrong a fair amount, and even when I'm right "
    "it's a probability, not a promise. I'm one input — you decide.")
CANNED_APP_FACT_NO_CHUNK = (
    "I don't have that detail in front of me yet — I can only explain the "
    "parts of the app I've got notes on. Try rephrasing, or ask me about a "
    "specific feature or how something works.")
CANNED_STOCK_NO_DATA = (
    "I don't have that stock's analysis in front of me right now. Run it "
    "through Look Up or open it in your portfolio, and I can walk you through "
    "what the analysis says.")
# slice 7: cloud-lane canned responses (no model)
CANNED_OFFLINE = (
    "You're offline right now, so I can't look that up — that needs a "
    "connection. Your existing recommendations and data are still here to "
    "review; new results will come once you're back online.")
CANNED_RESEARCH_NO_PROVIDER = (
    "I'd look that up with a cloud AI, but you haven't added one yet. Adding "
    "a free one takes a minute — Groq or Google Gemini just need an email, no "
    "card. Once one's connected I can answer open-world questions like this. "
    "Want to add one?")

# ── keyword/rule patterns (cheap; NO model call) ──
# ADVICE is caught FIRST and GENEROUSLY (typo-tolerant). Legal line: an advice
# question must NEVER escape to the model/cloud. A research question misread as
# advice (canned) is harmless; an advice question misread as research (cloud)
# blows the boundary — so bias hard toward advice near the line.
_ADVICE_RE = re.compile(
    # typo-tolerant "should i ... <buy/sell/...>"
    r"(?i)\b(sh[ou]+ld|shud|shoud|shuld|sould|shld)\s+i\b.*\b"
    r"(buy|sell|invest|get|dump|hold|short|pick|put money)\b"
    r"|\b(should|shall|do|can|would|could|ought)\s+i\b.*\b"
    r"(buy|sell|invest|get|dump|hold|short|pick)\b"
    # "is X a good/worth ... buy/investment/stock"
    r"|\b(is|are)\b.*\b(a |an )?(good|bad|smart|safe|worth|solid|wise)\b.*\b"
    r"(buy|sell|investment|stock|bet|pick|idea|hold|play)\b"
    # "is X a buy/sell" (no qualifier)
    r"|\b(is|are)\b.*\b(a |an )\s*(buy|sell)\b"
    r"|\bworth (buying|selling|investing|holding)\b"
    r"|\bwhat should i (buy|sell|invest|do|pick|get|hold)\b"
    r"|\b(buy or sell|sell or buy)\b"
    r"|\bshould i (get|hold|keep|dump|sell|buy|short)\b"
    # safety net: typo'd "should" anywhere with a trade verb
    r"|\b(shud|shoud|shuld|sould|shld)\b.*\b(buy|sell|invest|short|dump)\b")
# slice 7: open-world RESEARCH (specific/current real-world facts) -> cloud.
# Checked AFTER advice/accuracy/app-fact/symptom so app + advice win.
_RESEARCH_RE = re.compile(
    r"(?i)\bwho (owns|makes|made|founded|runs|leads|created|is the ceo)\b"
    r"|\bwhat (company|country|industry|sector)\b"
    r"|\btell me about\b"
    r"|\bwhat does\b.*\b(make|makes|sell|sells|produce|produces|own|owns)\b"
    r"|\bis\b.*\b(chinese|china|american|u\.?s\.?|usa|foreign|japanese|"
    r"korean|european|german|indian)\b.*\bcompany\b"
    r"|\b(headquarter|based in|located in|ceo of|founder of|parent company|"
    r"owned by|competitors? of|history of|ticker for|stock symbol)\b"
    r"|\bwhere is\b.*\b(based|headquarter|located|from)\b"
    # slice 10: OWNERSHIP-STRUCTURE phrasing (subsidiaries / "how many companies
    # does X own" / "does X own" / "what X owns") is open-world research, NOT the
    # app's stock-analysis lane. Without this, "how many companies does IBM own"
    # fell through to _mentions_ticker (IBM = a ticker) -> stock_specific, which
    # wrongly deflected to "run it through Look Up". This matches the existing
    # "where is X based" / "does X have subsidiaries" escalation. Advice is still
    # caught FIRST (these patterns carry no buy/sell verb), and "analysis of X
    # stock" carries none of these tokens, so it stays stock_specific.
    r"|\b(subsidiaries|subsidiary)\b"
    r"|\bhow many\b.*\b(compan|business|brand|subsidiar|division|firm|own)"
    r"|\bdoes\b.*\bown\b"
    r"|\bwhat\b.*\b(compan(y|ies)|business(es)?|brands?|holdings?)\b.*\bown")
# slice 11: PRECISE positive match for "asking the APP's analysis/opinion of a
# stock" (the Look-Up lane). REPLACES the old ticker catch-all (any ticker-shaped
# token -> stock_specific) that swallowed factual company questions and forced a
# per-phrase treadmill. Now the stock lane is claimed ONLY by a clear
# analysis/opinion/verdict request; everything else with a company mention falls
# through to the research DEFAULT (cloud) -- the milder failure direction. Advice
# is still caught FIRST, so this never runs for an advice question.
_STOCK_ANALYSIS_RE = re.compile(
    r"(?i)"
    r"\byour (analysis|opinion|take|view|read|readout|verdict|rating|"
    r"thoughts?|assessment|call) (of|on|about|for)\b"
    r"|\bin your (view|opinion|assessment|book)\b"
    r"|\bwhat (do|does|did|would) you (think|reckon|make|say) (of|about)\b"
    r"|\bdo you (like|rate|favor|favour)\b"
    r"|\bas an? (buy|sell|investment|hold|pick|play|stock)\b"
    r"|\bwhy is\b.*\b(a |an )?(buy|sell|hold|avoid|strong buy|no.?call|"
    r"good buy|bad buy)\b"
    r"|\b(what'?s|whats|what is)\b.*\b(verdict|call|rating|take|analysis|"
    r"recommendation) (on|for|of|about)\b")
# A stock signal must be PRESENT for the analysis match to claim the Look-Up
# lane -- guards generic opinion phrasings ("what do you think of the movie")
# from being mis-grabbed as stock_specific. A ticker-shaped token counts too.
_STOCK_SIGNAL_RE = re.compile(
    r"(?i)\b(stocks?|shares?|equit(y|ies)|ticker|buy|sell|hold|invest|"
    r"investment|pick|play|position|valuation)\b")
# a capitalized proper-noun mid-sentence (a likely company/entity) -> research
# rather than local general-knowledge. (First word excluded — always capital.)
_PROPER_NOUN_RE = re.compile(r"(?<=\s)[A-Z][a-zA-Z][a-zA-Z]+")
_ACCURACY_RE = re.compile(
    r"(?i)\b(can i trust|should i trust|are you trustworthy|how accurate|"
    r"how reliable|how good are you|are you (accurate|reliable|right|legit|"
    r"any good|trustworthy|ever right|ever wrong)|do you (actually )?work|"
    r"how often are you (right|wrong)|are you usually (right|wrong)|"
    r"is this accurate|how do i know.*trust)\b")
# generic finance/definitional general-knowledge (NOT app features)
_GENERAL_RE = re.compile(
    r"(?i)^\s*(what is|what are|what does|whats|define|explain|how does|"
    r"how do)\b")
# app-feature vocabulary -> app_fact
_FEATURE_TERMS = (
    "recommend", "scan", "track record", "look up", "lookup", "consensus",
    "provider", "providers", "cache", "portfolio", "choices", "watchlist",
    "written off", "button", "screen", "tab", "setting", "settings", "verdict",
    "path", "style", "discover", "api key", "the app", "this app", "tired market",
)
# slice 5: error/troubleshooting symptom words -> route to the local program
# lane (b) so they hit retrieval (faq+features+playbook), grounded-or-refuse.
_SYMPTOM_TERMS = (
    "stuck", "won't", "wont", "not working", "doesn't work", "doesnt work",
    "broken", "error", "frozen", "stopped", "failed", "failing", "crash",
    "empty", "nothing", "offline", "no internet", "rate limit", "rate-limit",
    "cooldown", "paused", "loading", "no providers", "no ai", "not loading",
    "won't load", "wont load", "won't save", "wont save", "not saving",
    "disappeared", "out of budget",
    # slice 6: "get connected" on-ramp intents
    "add an ai", "add a provider", "add ai", "add a key", "add an api key",
    "which ai", "which provider", "get connected", "sign up", "signup",
    "free key", "api key", "invalid key", "key invalid", "key rejected",
    "connect an ai", "set up ai", "groq", "gemini", "mistral",
)
_TICKER_RE = re.compile(r"\b[A-Z]{2,5}\b")
_TICKER_STOP = {"AI", "THE", "AND", "FAQ", "USA", "CEO", "IPO", "ETF", "API",
                "BUY", "SELL", "OK", "FAQ", "AM", "PM"}


def _internal() -> Path:
    return _SCRIPT_DIR / 'data' / 'internal'


def _load_internal_json(name: str) -> list:
    for d in (_internal(), Path('data') / 'internal'):
        try:
            p = d / name
            if p.exists():
                return json.loads(p.read_text(encoding='utf-8')).get(
                    'entries') or []
        except Exception:
            continue
    return []


def _load_faq() -> list:
    return _load_internal_json('faq.json')


def _load_features() -> list:
    return _load_internal_json('features.json')


def _load_playbook() -> list:
    # slice 5: the error/repair playbook joins the PULL (question->fix) lane.
    # The PUSH path (tm_teacher_intercept.emit_system_event) reads the same
    # file independently and is untouched.
    return _load_internal_json('error_recovery_playbook.json')


def _load_getconnected() -> list:
    # slice 6: the offline "get connected" on-ramp (how to add a free cloud
    # key + signup-friction + key troubleshooting). A SEPARATE teacher-owned
    # sheet — NOT provider_signup_specs.json (which is empty-by-design /
    # macro-keyless and drives the Data Providers UI; two audits assert it
    # stays empty). This keeps the on-ramp fully isolated.
    return _load_internal_json('teacher_getconnected.json')


def _any_term_wb(ql: str, terms) -> bool:
    """True if any term appears on a WORD BOUNDARY in ql. Word-boundary (not
    raw substring) so short terms like "tab"/"path"/"scan"/"no" don't match
    inside longer words ("profi-tab-le", "patho-logical"). Multi-word terms
    match as phrases."""
    for t in terms:
        if t and re.search(r'\b' + re.escape(t) + r'\b', ql):
            return True
    return False


def _mentions_feature(q: str) -> bool:
    return _any_term_wb(q.lower(), _FEATURE_TERMS)


def _mentions_ticker(q: str) -> bool:
    for m in _TICKER_RE.findall(q or ''):
        if m not in _TICKER_STOP:
            return True
    return False


def _asks_stock_analysis(q: str) -> bool:
    """slice 11 — True only when the question CLEARLY asks for the APP's
    analysis/opinion/verdict of a stock: an analysis-intent phrase AND a stock
    signal (a stock/trade word or a ticker-shaped token). This is the PRECISE
    positive match that replaced the old ticker catch-all. When it's not clearly
    an analysis request, we let the question fall through to the research default
    (the safe/milder direction) rather than grab it for the Look-Up lane."""
    if not _STOCK_ANALYSIS_RE.search(q):
        return False
    return bool(_STOCK_SIGNAL_RE.search(q)) or _mentions_ticker(q)


def classify(question: str) -> str:
    """Cheap, model-free classification into one of: 'advice', 'accuracy',
    'app_fact', 'research', 'stock_specific', 'general'.

    ORDER IS THE SAFETY MODEL (slice 11 ordering):
      1. advice / accuracy / trust -> canned, model NEVER called (legal line)
      2. app feature / troubleshooting / get-connected -> local grounded
      3. open-world RESEARCH patterns -> cloud
      4. basic general-knowledge concept -> local
      5. CLEARLY asking the app's analysis/opinion of a stock -> stock_specific
         (a PRECISE positive match — not a catch-all)
      6. EVERYTHING ELSE -> research (cloud) — the safe default

    Slice 11 flipped the old catch-all: any ticker-shaped token used to default
    to stock_specific UNLESS a research phrase was whitelisted (a per-phrase
    treadmill that swallowed factual company questions). Now the stock lane is
    claimed ONLY by a clear analysis request (step 5); a factual company
    question falls through to research (step 6). The risk MOVED and is milder: a
    genuine 'what do you think of X stock' that slips past step 5 lands in
    research (answered factually, ask-screen disclaimer present) instead of being
    refused. Advice is still caught FIRST (step 1) — flipping the deeper default
    changed nothing about it; an advice question never reaches step 5/6."""
    q = (question or '').strip()
    if not q:
        return 'app_fact'
    if _ADVICE_RE.search(q):
        return 'advice'
    if _ACCURACY_RE.search(q):
        return 'accuracy'
    if _mentions_feature(q):
        return 'app_fact'
    ql = q.lower()
    if _any_term_wb(ql, _SYMPTOM_TERMS):
        return 'app_fact'
    # open-world research -> cloud (only AFTER advice/app are ruled out)
    if _RESEARCH_RE.search(q):
        return 'research'
    if _GENERAL_RE.search(q):
        # basic concept ("what is a stock") -> local; a named entity
        # ("what is Tesla") -> cloud (a capitalized proper noun signals a
        # specific real-world entity).
        if not _PROPER_NOUN_RE.search(q):
            return 'general'
        return 'research'
    # step 5 (slice 11): PRECISE positive match for the Look-Up lane — only a
    # CLEAR "what's your analysis/opinion/verdict of <stock>" claims it. The old
    # code grabbed ANY ticker-shaped token here (the treadmill); now a factual
    # company question ("how big is IBM's workforce") with no analysis intent
    # falls through to step 6.
    if _asks_stock_analysis(q):
        # "why is FISV a hold" / "what's your analysis of IBM stock" = asking the
        # APP's analysis, which the cloud doesn't have -> canned "no analysis in
        # front of me, run it through Look Up".
        return 'stock_specific'
    # step 6 — DEFAULT is RESEARCH -> cloud. Anything not caught above
    # (advice/accuracy=canned, feature/symptom=local grounded, basic-concept=
    # local, clear-analysis-request=stock_specific) is treated as an open-world
    # factual question ("does Apple have subsidiaries", "how big is IBM's
    # workforce") and escalated. The LOCAL model is never asked an un-routable
    # question (it confabulates); the cloud gets a factual-framed prompt that
    # bans advice; and advice was already caught FIRST (generous + typo-
    # tolerant), so it can never fall through to here. (No provider ->
    # get-connected; offline -> offline catch — handled in answer_question.)
    return 'research'


def _kw_score(q_lower: str, terms) -> int:
    """Count how many DISTINCT terms (keywords/aliases/names) appear as
    substrings in the lowercased question. Distinct on purpose: an entry that
    repeats the same token across keyword + display_name + feature_id (e.g.
    "scan") must not get 3x credit — that would let a single-word feature name
    outscore a more specific multi-keyword playbook match."""
    seen = set()
    for t in (terms or []):
        try:
            ts = str(t).strip().lower()
            if not ts or ts in seen:
                continue
            # WORD-BOUNDARY match, not raw substring — else short keywords like
            # "no" match inside "now"/"know" and a verdict feature hijacks an
            # offline question. \b around the (escaped) term; multi-word phrases
            # match as a unit.
            if re.search(r'\b' + re.escape(ts) + r'\b', q_lower):
                seen.add(ts)
        except Exception:
            pass
    return len(seen)


def retrieve(question: str) -> list[dict]:
    """Best-matching program-knowledge chunk for the question, across BOTH
    faq.json (15 Q&A) AND features.json (69 feature entries) — slice 4 widened
    the local program lane to features.json to fix recall (slice 3 grounded on
    faq alone and refused too often). Keyword/alias/name overlap; faq wins
    exact ties (curated Q&A). Returns [] if nothing matches (-> honest refuse).
    Normalized to {'question','answer'} so the grounded prompt is uniform."""
    q = (question or '').lower()
    if not q.strip():
        return []
    best = None
    best_score = 0
    # faq: score on its keywords
    for e in _load_faq():
        s = _kw_score(q, e.get('keywords') or [])
        if s > best_score:
            best_score = s
            best = {'question': e.get('question', ''),
                    'answer': e.get('answer', '')}
    # features: score on keywords + aliases + display name + feature_id
    for e in _load_features():
        terms = list(e.get('keywords') or []) + list(e.get('aliases') or [])
        terms.append(e.get('display_name') or '')
        terms.append((e.get('feature_id') or '').replace('_', ' '))
        s = _kw_score(q, terms)
        if s > best_score:  # strict > -> faq keeps ties
            best_score = s
            body = (e.get('short_description') or '').strip()
            ld = (e.get('long_description') or '').strip()
            if ld:
                body = (body + ' ' + ld).strip()
            best = {'question': e.get('display_name', ''), 'answer': body}
    # playbook: score on its added user-symptom keywords + a fallback of the
    # short_hint's own words (so even un-keyworded entries have a shot). The
    # rendered chunk is the FIX, with offered_action framed as guidance the
    # USER can do (NOT something the AI did — see build_grounded_prompt).
    for e in _load_playbook():
        terms = list(e.get('keywords') or [])
        terms += [w for w in re.split(r'[^a-z0-9]+',
                                      (e.get('short_hint') or '').lower())
                  if len(w) > 3]
        s = _kw_score(q, terms)
        if s > best_score:  # strict > -> faq/features keep ties
            best_score = s
            title = (e.get('short_hint') or e.get('entry_id') or '').strip()
            parts = []
            if e.get('diagnostic'):
                parts.append(e['diagnostic'].strip())
            if e.get('recommendation'):
                parts.append(e['recommendation'].strip())
            if e.get('manual_fallback'):
                parts.append("How to fix it: " + e['manual_fallback'].strip())
            oa = e.get('offered_action') or {}
            if isinstance(oa, dict) and oa.get('label'):
                parts.append(f"(In the app, the user can: {oa['label']}.)")
            best = {'question': title, 'answer': " ".join(parts).strip()}
    # get-connected on-ramp (slice 6): how to add a key + signup-friction +
    # key troubleshooting. Authored bodies; scored on user-symptom keywords.
    for e in _load_getconnected():
        s = _kw_score(q, e.get('keywords') or [])
        if s > best_score:  # strict > -> earlier sources keep ties
            best_score = s
            best = {'question': e.get('title', ''),
                    'answer': (e.get('body') or '').strip()}
    return [best] if best else []


def _chunks_text(chunks: list[dict]) -> str:
    out, used = [], 0
    for c in chunks:
        block = (f"Q: {c.get('question','')}\nA: {c.get('answer','')}").strip()
        remaining = TEACHER_RETRIEVAL_CHAR_CAP - used
        if remaining <= 0:
            break
        if len(block) > remaining:
            # truncate (don't DROP) so a long feature body still grounds the
            # answer rather than leaving the model with empty notes.
            block = block[:remaining].rstrip() + " …"
        out.append(block)
        used += len(block)
    return "\n\n".join(out)


def build_grounded_prompt(question: str, chunks: list[dict]) -> str:
    """A prompt that constrains the model to the retrieved notes — answer FROM
    them, and if the answer isn't there, say so (don't guess)."""
    notes = _chunks_text(chunks)
    return (
        "Use ONLY the app notes below to answer the question. If the answer "
        "isn't in the notes, say you don't have that detail yet — do not "
        "guess or invent how the app works.\n"
        "If the notes describe a fix or an action, tell the user how THEY can "
        "do it (e.g. \"you can open Settings and...\"). NEVER say you have "
        "done it or will do it yourself — you cannot operate the app.\n\n"
        f"APP NOTES:\n{notes}\n\n"
        f"QUESTION: {question.strip()}")


def build_research_prompt(question: str) -> str:
    """Templated CLOUD prompt for an open-world research question. Frames it as
    factual/educational lookup and explicitly bans advice/predictions (only
    research questions ever reach this — advice is caught upstream). Authored
    draft pending wording review."""
    return (
        "You are answering a factual research question for someone using a "
        "stock-analysis app. Give a brief, plain, factual answer (companies, "
        "products, ownership, who/what/where — that kind of thing). Do NOT "
        "give buy/sell/investment advice, recommendations, or price "
        "predictions; if the question drifts that way, answer only the factual "
        "part. If you're not sure, say so.\n\n"
        f"QUESTION: {question.strip()}")


def frame_cloud_answer(answer: str) -> str:
    """Return the cloud answer CLEAN — no per-answer footer (slice 10).

    Slice 7 appended a "general info / not financial advice / can be wrong /
    your call" footer to every cloud answer. In live use that footer
    self-undercut factual answers ("IBM is in Armonk — but this can be wrong")
    and stapled a financial disclaimer onto non-financial questions. The
    framing now lives as a PERSISTENT disclaimer on the Ask screen (shown
    before the user asks), so the answer itself stands clean. The empty-answer
    fallback stays (a non-answer must still read honestly).

    NOTE: this is NOT a hole in the safety model — advice questions never reach
    the cloud (caught FIRST by classify -> CANNED_ADVICE), and the research
    prompt (build_research_prompt) already bans advice/predictions. The footer
    was framing on FACTUAL answers, not an advice guard."""
    a = (answer or '').strip()
    if not a:
        return ("I looked that up but didn't get a usable answer back — try "
                "asking again in a moment.")
    return a


def route(question: str) -> dict:
    """Decide what happens for this question WITHOUT calling the model/cloud.
    Returns a decision dict the caller executes:
      kind        : the classification
      action      : 'deflect_advice' | 'frame_accuracy' | 'refuse_app_fact'
                    | 'refuse_stock' | 'answer_grounded' | 'answer_general'
                    | 'escalate_cloud'
      canned      : exact text for the non-model actions (else None)
      prompt      : prompt for answer_* / escalate_cloud actions (else None)
      uses_model  : bool — executing this calls the LOCAL model
      uses_cloud  : bool — executing this calls a CLOUD provider
    """
    kind = classify(question)
    if kind == 'advice':
        return {'kind': kind, 'action': 'deflect_advice',
                'canned': CANNED_ADVICE, 'prompt': None,
                'uses_model': False, 'uses_cloud': False}
    if kind == 'accuracy':
        return {'kind': kind, 'action': 'frame_accuracy',
                'canned': CANNED_ACCURACY, 'prompt': None,
                'uses_model': False, 'uses_cloud': False}
    if kind == 'stock_specific':
        return {'kind': kind, 'action': 'refuse_stock',
                'canned': CANNED_STOCK_NO_DATA, 'prompt': None,
                'uses_model': False, 'uses_cloud': False}
    if kind == 'research':
        # v4.14.5.30: research is the DEFAULT lane, but prefer LOCAL grounding
        # when our own notes actually cover the question (e.g. "tell me about
        # Recommend", or a key-troubleshooting question phrased without a
        # symptom keyword like "it says my key is invalid"). Only escalate to
        # the CLOUD when local retrieval finds nothing — so the cloud handles
        # genuinely open-world facts, not things we can answer from notes.
        chunks = retrieve(question)
        if chunks:
            return {'kind': 'app_fact', 'action': 'answer_grounded',
                    'canned': None,
                    'prompt': build_grounded_prompt(question, chunks),
                    'uses_model': True, 'uses_cloud': False, 'chunks': chunks}
        # open-world -> CLOUD. The local model is never asked this (it
        # confabulates real-world specifics). uses_model stays False.
        return {'kind': kind, 'action': 'escalate_cloud', 'canned': None,
                'prompt': build_research_prompt(question),
                'uses_model': False, 'uses_cloud': True}
    if kind == 'general':
        return {'kind': kind, 'action': 'answer_general',
                'canned': None, 'prompt': (question or '').strip(),
                'uses_model': True, 'uses_cloud': False}
    # app_fact (incl. unknown fallthrough): grounded if a chunk matches, else refuse.
    chunks = retrieve(question)
    if chunks:
        return {'kind': 'app_fact', 'action': 'answer_grounded',
                'canned': None,
                'prompt': build_grounded_prompt(question, chunks),
                'uses_model': True, 'uses_cloud': False, 'chunks': chunks}
    return {'kind': 'app_fact', 'action': 'refuse_app_fact',
            'canned': CANNED_APP_FACT_NO_CHUNK, 'prompt': None,
            'uses_model': False, 'uses_cloud': False}


def _eligible_cloud_providers(providers=None):
    """Eligible cloud providers for the chat lane, fast-first, via the SAME
    router seam the brain uses (teacher_ai call_type: cooldown/cap-filtered,
    own budget, fast+free only). Returns [] if none configured/available."""
    try:
        if providers is None:
            import tm_api_providers as _tmap
            providers = _tmap.load_enabled_providers()
        import tm_ai_router as _R
        return _R.select_teacher_provider_order(providers or [])
    except Exception:
        return list(providers or [])


def answer_question(question: str,
                    model_answer_fn: Optional[Callable] = None,
                    cloud_call_fn: Optional[Callable] = None,
                    providers: Optional[list] = None,
                    online: Optional[bool] = None) -> dict:
    """Full gated flow. Returns {kind, action, text, uses_model, uses_cloud}.

    LANES: advice/accuracy/stock -> canned (no model, no cloud). app_fact /
    general -> LOCAL model. research -> CLOUD (escalation). The model and the
    cloud are NEVER reached for advice/accuracy — that's the legal line.

    Injectable for tests: model_answer_fn(prompt, system), cloud_call_fn(
    provider, prompt), providers list, online bool (else read tm_network).
    """
    q = (question or '').strip()
    if not q:
        return {'kind': 'app_fact', 'action': 'refuse_app_fact',
                'text': CANNED_APP_FACT_NO_CHUNK,
                'uses_model': False, 'uses_cloud': False}
    d = route(q)

    # ── CLOUD escalation lane (research) ──
    if d.get('uses_cloud'):
        # offline? (read the existing detector; tests pass `online=`)
        is_on = online
        if is_on is None:
            try:
                import tm_network as _net
                is_on = _net.is_online()
            except Exception:
                is_on = True  # fail-open: assume online, let the call decide
        if not is_on:
            return {'kind': d['kind'], 'action': 'offline_catch',
                    'text': CANNED_OFFLINE,
                    'uses_model': False, 'uses_cloud': False}
        order = _eligible_cloud_providers(providers)
        if not order:
            # no cloud key -> route to the get-connected on-ramp
            return {'kind': d['kind'], 'action': 'research_no_provider',
                    'text': CANNED_RESEARCH_NO_PROVIDER,
                    'uses_model': False, 'uses_cloud': False}
        if cloud_call_fn is None:
            def cloud_call_fn(provider, prompt):
                import tm_api_providers as _tmap
                return _tmap.call_provider(provider, prompt, timeout=45.0)
        raw = None
        for prov in order:  # single successful call, failover on error
            try:
                raw = cloud_call_fn(prov, d['prompt'])
            except Exception:
                continue
            if raw and str(raw).strip():
                break
            raw = None
        if not raw:
            return {'kind': d['kind'], 'action': 'escalate_cloud',
                    'text': frame_cloud_answer(''),  # honest "no answer"
                    'uses_model': False, 'uses_cloud': True}
        # STRUCTURAL frame-wrap, applied by us regardless of cloud content.
        return {'kind': d['kind'], 'action': 'escalate_cloud',
                'text': frame_cloud_answer(str(raw)),
                'uses_model': False, 'uses_cloud': True}

    if not d['uses_model']:
        return {'kind': d['kind'], 'action': d['action'],
                'text': d['canned'], 'uses_model': False, 'uses_cloud': False}

    # v4.14.6.108-standalone-prep: the bundled local model (tm_local_model /
    # qwen) was CUT — its dev-spike path was dead, so this lane already fell
    # through to the canned non-answer for every real user. With NO injected
    # model_answer_fn (the live case), return that grounded canned text
    # directly — behavior is unchanged for real users, and the dead
    # tm_local_model dependency is gone. An injected model_answer_fn (tests, or
    # a future repoint to the dormant cloud brain tm_teacher_brain) is still
    # honored; that repoint is a separate later decision.
    _honest_noanswer = (
        CANNED_APP_FACT_NO_CHUNK if d['action'] == 'answer_grounded'
        else "I can't answer that right now — try again in a moment.")
    if model_answer_fn is None:
        return {'kind': d['kind'], 'action': d['action'],
                'text': _honest_noanswer,
                'uses_model': False, 'uses_cloud': False}
    try:
        text = model_answer_fn(d['prompt'], system=None)
    except Exception:
        return {'kind': d['kind'], 'action': d['action'],
                'text': _honest_noanswer,
                'uses_model': False, 'uses_cloud': False}
    return {'kind': d['kind'], 'action': d['action'],
            'text': (text or '').strip(),
            'uses_model': True, 'uses_cloud': False}
