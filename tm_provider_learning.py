"""tm_provider_learning — v4.14.5.14a.4 dynamic provider-limit learning.

Provider free-tier limits are NOT static. The hardcoded URL-detection
caps in tm_ai_router are SEED values only — the first guess before any
real provider response is seen. This module:

  - classify_429(): tells a per-minute throttle ("slow down for a few
    seconds") apart from daily exhaustion ("done until reset"). The old
    code applied a flat 5-min cooldown to every 429, so hitting a 30-RPM
    speed limit cost 5 minutes and Groq's real 14,400/day was never
    approached.
  - note_success_headers(): on every successful call, if the provider
    reports its real daily cap via X-RateLimit-*-Day headers, the
    learned cap is updated to match. Reality wins; seeds defer.
  - note_daily_429(): a daily-classified 429 at N calls lowers the
    learned cap to N.
  - observed-behaviour fallback: 90% of cap reached with no 429s → raise
    20%.
  - get_learned_cap(): the working cap (overrides the seed).
  - persistence: data/provider_learning.json (+ .log audit trail);
    re-verified against the first fresh header after startup.

Pure/defensive: when a provider sends no rate-limit signal, NOTHING
changes (the "no signal" case) — the system never drifts away from
reality just because a provider is quiet. Never raises.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_DATA_DIR = Path(__file__).parent / "data"
_STORE = _DATA_DIR / "provider_learning.json"
_LOG = _DATA_DIR / "provider_learning.log"

# v4.14.5.14a.5: a header-reported day-cap more than this multiple of
# the family's URL seed is almost certainly NOT the free-tier RPD cap
# (e.g. GitHub-via-Azure reports 60000 vs a real ~50/day) — ignored.
_SEED_SANITY_MULT = 20

_lock = threading.Lock()
# v4.14.5.14rot-p3a: the store is now nested per
#   {family: {model_key: {daily_cap, last_updated, source,
#                          today_iso, today_calls, today_429s}}}
# Callers that don't supply a model (every caller until Patch 3b) use
# the _DEFAULT_MODEL slot, which after migration holds EXACTLY what the
# old flat per-family record held — so behaviour is byte-identical with
# the flag off. The old flat-per-family schema is auto-wrapped on load
# (non-destructive, idempotent).
_DEFAULT_MODEL = "_default"
_state: dict = {}
_loaded = False

# v4.14.5.14-classify429-fix: one-shot-per-(family) guard so the
# ambiguous-429 diagnostic line is written once per process per
# provider (not once per 429). classify_429 itself stays a PURE
# no-side-effect function — the write lives in _log_ambiguous_429,
# called by the 429 consumer (_classify_and_record_quota).
_AMBIG_429_LOGGED: set = set()


def _classify429_v2_on() -> bool:
    """True unless use_classify_429_v2 is explicitly False. Default
    True (opt-OUT burn-recovery flag). Fail-open True: a transient
    unreadable config must not silently resurrect the cap-ratchet
    bug, and v2 is strictly burn-safer (only ever makes a 429 LESS
    likely to tighten the daily cap)."""
    try:
        import tm_model_cursor as _mc
        return bool(_mc.classify_429_v2_enabled())
    except Exception:
        return True


def _provider_429_parsers_on() -> bool:
    """v4.14.5.14-retry-and-cleanup-bundle Fix B: True unless
    use_provider_429_parsers is explicitly False. Default True (opt-OUT).
    Gates the provider-specific structured 429-body parsers (Groq today).
    Fail-open True: the parsers are strictly safer (unknown body → falls
    through to the existing generic logic)."""
    try:
        import tm_model_cursor as _mc
        return bool(_mc.provider_429_parsers_enabled())
    except Exception:
        return True


def _parse_groq_429_body(body: str):
    """v4.14.5.14-retry-and-cleanup-bundle Fix B: Groq returns a
    structured 429 JSON body whose error.type names the exact limit hit.
    Read it directly — more reliable than the generic keyword scan.

    Returns a classify_429-shaped dict
        {'type': 'per_minute'|'daily', 'source': 'groq_body',
         'retry_after_seconds': int}
    or None if the body is missing / not JSON / lacks a recognised
    error.type, in which case the caller falls through to the existing
    header + generic-keyword logic. Never raises."""
    if not body:
        return None
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None  # not JSON → fall through
    if not isinstance(data, dict):
        return None
    err = data.get('error')
    err_type = ''
    if isinstance(err, dict):
        err_type = str(err.get('type') or '').strip().lower()
    elif isinstance(err, str):
        err_type = err.strip().lower()
    if not err_type:
        return None
    if ('tokens_per_minute_exceeded' in err_type
            or 'requests_per_minute_exceeded' in err_type):
        return {'type': 'per_minute', 'source': 'groq_body',
                'retry_after_seconds': 60}
    if ('requests_per_day_exceeded' in err_type
            or 'tokens_per_day_exceeded' in err_type):
        return {'type': 'daily', 'source': 'groq_body',
                'retry_after_seconds': _seconds_to_utc_midnight()}
    return None  # unrecognised error type → fall through


def _parse_openai_compatible_429_body(body: str):
    """v4.14.5.14-429-parsers-and-lookup-tier3 (Fix A): parse the 429 body
    of an OpenAI-compatible provider (Mistral / Cerebras / GitHub Models —
    all `{"error": {"code"/"type"/"message": …}}` shaped). Returns a
    classify_429-shaped dict {'type','source','retry_after_seconds'} or
    None when nothing is recognised (→ caller falls through to the existing
    header + generic-keyword logic, same fail-open contract as the Groq
    parser). Never raises.

    Daily signals are checked BEFORE per-minute so a body that mentions
    both (e.g. "rate limit ... per day") classifies as the stronger daily
    signal. No real Mistral/Cerebras/GitHub 429 bodies were captured on
    this install (they rarely 429), so the recognised tokens come from the
    documented OpenAI-compatible shapes; anything unknown safely falls
    through."""
    if not body:
        return None
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None  # not JSON → fall through
    if not isinstance(data, dict):
        return None
    err = data.get('error')
    if isinstance(err, dict):
        err_code = str(err.get('code') or '').strip().lower()
        err_type = str(err.get('type') or '').strip().lower()
        err_msg = str(err.get('message') or '').strip().lower()
    elif isinstance(err, str):
        err_code = err_type = ''
        err_msg = err.strip().lower()
    else:
        err_code = err_type = err_msg = ''
    _blob = ' '.join((err_code, err_type, err_msg))
    if not _blob.strip():
        return None
    # Daily-quota signals first (stronger; longer cooldown).
    if any(s in _blob for s in (
            'requests_per_day', 'tokens_per_day', 'daily_limit',
            'per day', 'daily limit', 'daily quota')):
        return {'type': 'daily', 'source': 'openai_compat_body',
                'retry_after_seconds': _seconds_to_utc_midnight()}
    # Per-minute / short-window signals.
    if any(s in _blob for s in (
            'requests_per_minute', 'tokens_per_minute', 'per minute',
            'rate_limit_exceeded', 'ratelimitreached', 'rate_limit',
            'rate limit', 'too many requests', 'slow down')):
        return {'type': 'per_minute', 'source': 'openai_compat_body',
                'retry_after_seconds': 60}
    return None  # unrecognised → fall through


def _log_ambiguous_429(family: str) -> None:
    """One-shot (per process, per family) audit line for an ambiguous
    429 that v2 defaulted to per-minute. Written to the module's
    existing provider_learning.log audit trail — classify_429 and its
    outcome-recorder callers are deliberately app-log-decoupled (same
    cfg/app-agnostic discipline as tm_model_cursor), so this is the
    natural channel; the user sees the *effect* (fewer cooldown/degradation
    lines) in the activity log. Never raises."""
    fam = (family or 'unknown').strip().lower()
    if fam in _AMBIG_429_LOGGED:
        return
    _AMBIG_429_LOGGED.add(fam)
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        with _LOG.open('a', encoding='utf-8') as f:
            f.write(f"{ts} | {fam} | classify_429 | [classify_429] "
                    f"ambiguous 429 from {fam} — no daily/minute "
                    f"headers, defaulting to per-minute (safer; manual "
                    f"fix recovery if daily was intended).\n")
    except Exception:
        pass


def _log_consecutive_429_gate(family, model, n, threshold,
                               tightened=False) -> None:
    """v4.14.5.14-classify429-part-c (IDEAS Fix 2): record the
    consecutive-429 cap-tightening gate decision to the provider_learning
    .log audit trail (the classify_429 telemetry channel — app-decoupled
    like _log_ambiguous_429; the user sees the *effect* — fewer "exhausted on
    first call" / "AI Reduced" — in the activity log). Logs each gate
    event (not one-shot): blocked single/double 429s and the 3rd that
    actually allows tightening. Never raises."""
    try:
        fam = (family or 'unknown').strip().lower()
        mdl = model or '_default'
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        if tightened:
            body = (f"[classify_429] daily-classified 429 from {fam}/{mdl}; "
                    f"consecutive={n} -> cap-tightening ALLOWED "
                    f"(>= {threshold} consecutive).")
        else:
            body = (f"[classify_429] daily-classified 429 from {fam}/{mdl}; "
                    f"consecutive={n}/{threshold}; cap untouched (need "
                    f"{threshold} consecutive before tightening).")
        with _LOG.open('a', encoding='utf-8') as f:
            f.write(f"{ts} | {fam} | classify_429 | {body}\n")
    except Exception:
        pass

# Provider family from endpoint/name — same buckets as the router's
# URL-detection so learned caps line up with seeds.
_FAMILY_PATTERNS = (
    ('groq', ('groq.com',)),
    ('mistral', ('mistral.ai',)),
    ('cerebras', ('cerebras.ai',)),
    ('github', ('models.inference.ai.azure.com', 'models.github.ai')),
    ('gemini', ('generativelanguage.googleapis.com',)),
)


def provider_family(provider: dict) -> str:
    ep = str((provider or {}).get('endpoint') or '').lower()
    for fam, pats in _FAMILY_PATTERNS:
        if any(p in ep for p in pats):
            return fam
    nm = str((provider or {}).get('name') or
             (provider or {}).get('preset') or '').lower()
    for fam, _ in _FAMILY_PATTERNS:
        if fam in nm:
            return fam
    return nm or 'unknown'


def _model_key(model) -> str:
    """v4.14.5.14rot-p3a: normalise a model id to its store key. An
    absent/blank model → the _DEFAULT_MODEL slot, which preserves the
    pre-patch family-level behaviour exactly."""
    m = str(model or '').strip()
    return m or _DEFAULT_MODEL


def _is_old_flat(rec) -> bool:
    """True if `rec` is a pre-p3a flat per-family record (scalar fields
    sitting directly on the family) rather than a {model_key: {...}}
    container. Model ids are never these reserved field names, so the
    test is unambiguous and idempotent."""
    return isinstance(rec, dict) and (
        'daily_cap' in rec or 'today_iso' in rec
        or 'today_calls' in rec or 'today_429s' in rec)


def _migrate_state_locked() -> None:
    """Wrap any old flat per-family record into {_DEFAULT_MODEL: rec}.
    Non-destructive (the record is preserved verbatim under the default
    slot) and idempotent (already-nested families are left untouched)."""
    global _state
    if not isinstance(_state, dict):
        _state = {}
        return
    for fam, rec in list(_state.items()):
        if _is_old_flat(rec):
            _state[fam] = {_DEFAULT_MODEL: rec}
        elif not isinstance(rec, dict):
            _state[fam] = {}


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def _seconds_to_utc_midnight() -> int:
    now = datetime.now(timezone.utc)
    nxt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    nxt = nxt.fromordinal(now.toordinal() + 1).replace(
        tzinfo=timezone.utc)
    return max(60, int((nxt - now).total_seconds()))


def _load_locked() -> None:
    global _state, _loaded
    if _loaded:
        return
    try:
        if _STORE.exists():
            _state = json.loads(_STORE.read_text('utf-8')).get(
                'providers', {}) or {}
    except Exception:
        _state = {}
    _migrate_state_locked()
    _sanity_wipe_locked()  # v4.14.5.14-classify429-fix Part B
    _loaded = True


def _sanity_wipe_locked() -> None:
    """v4.14.5.14-classify429-fix Part B: one-shot startup recovery of
    burn already incurred from past 429 misclassification. Any
    persisted learned daily_cap that is below 10% of its family's
    URL-detected SEED is wiped (daily_cap → None, source tagged) so it
    re-learns from real provider headers instead of staying pinned at
    a wrong-low value the cap itself prevents calls from correcting.

    Conservative by design: only egregiously-wrong values are caught;
    legitimate small caps (SambaNova ~20) and healthy per-model caps
    (~14,400) are preserved. PRESETS-floor discipline is structurally
    intact — wiping to None falls back to the seed/preset, never below
    it. Runs once per process (inside the _loaded guard). Fail-safe:
    any error → that entry is left untouched (never wipe what can't be
    validated). Gated by use_persisted_cap_sanity_wipe (default True).
    Idempotent: a wiped entry's daily_cap is None so it no longer
    matches; the 'sanity-wiped' source tag persists as an audit trail.
    """
    try:
        import tm_model_cursor as _mc
        if not _mc.persisted_cap_sanity_wipe_enabled():
            return
    except Exception:
        pass  # fail-open: flag defaults True, proceed with the wipe
    try:
        import tm_ai_router as _tar
    except Exception:
        return  # can't resolve seeds → validate nothing → wipe nothing
    if not isinstance(_state, dict):
        return
    changed = False
    for fam, fam_rec in list(_state.items()):
        if not isinstance(fam_rec, dict):
            continue
        try:
            seed = _tar.seed_cap_for_family(fam)
        except Exception:
            seed = None
        if not seed or seed <= 0:
            continue  # B5: don't wipe what we can't validate
        floor = seed * 0.10
        for mk, r in list(fam_rec.items()):
            if not isinstance(r, dict):
                continue
            dc = r.get('daily_cap')
            if dc is None:
                continue  # B4 / idempotent: nothing to wipe
            try:
                dcf = float(dc)
            except (TypeError, ValueError):
                continue
            if dcf < floor:  # strict < (B2 boundary stays)
                old = r.get('daily_cap')
                r['daily_cap'] = None
                r['source'] = 'sanity-wiped 2026-05-19'
                changed = True
                try:
                    _log_change(
                        fam, 'daily_cap', old, None,
                        f'sanity wipe on startup: {old} below 10% of '
                        f'seed={seed} (floor={floor:.0f}) — will '
                        f're-learn from headers',
                        model=(None if mk == _DEFAULT_MODEL else mk))
                except Exception:
                    pass
    if changed:
        try:
            _save_locked()
        except Exception:
            pass


def _save_locked() -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _STORE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(
            {'schema_version': 2, 'providers': _state,
             'saved_at': datetime.now(timezone.utc).isoformat()},
            indent=1), 'utf-8')
        tmp.replace(_STORE)
    except Exception:
        pass


def _log_change(family: str, field: str, old, new, reason: str,
                model=None) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        # Byte-identical to pre-p3a for the default slot; only a real
        # per-model write annotates the model so the audit trail stays
        # readable when Patch 3b starts threading models through.
        mk = _model_key(model)
        label = family if mk == _DEFAULT_MODEL else f"{family}/{mk}"
        with _LOG.open('a', encoding='utf-8') as f:
            f.write(f"{ts} | {label} | {field} | {old} -> {new} | "
                    f"reason: {reason}\n")
    except Exception:
        pass


def _rec_locked(family: str, model=None) -> dict:
    fam_rec = _state.get(family)
    if not isinstance(fam_rec, dict):
        fam_rec = {}
        _state[family] = fam_rec
    mk = _model_key(model)
    r = fam_rec.get(mk)
    if r is None:
        r = {'daily_cap': None, 'last_updated': None, 'source': 'seed',
             'today_iso': _today_utc(), 'today_calls': 0,
             'today_429s': 0}
        fam_rec[mk] = r
    if r.get('today_iso') != _today_utc():
        r['today_iso'] = _today_utc()
        r['today_calls'] = 0
        r['today_429s'] = 0
    return r


# ── retry-after / header parsing ──────────────────────────────────────

def _parse_retry_after(val: str) -> Optional[int]:
    if not val:
        return None
    val = val.strip()
    try:
        return max(1, int(float(val)))
    except (TypeError, ValueError):
        pass
    try:  # HTTP-date form
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(val)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(1, int((dt - datetime.now(timezone.utc))
                              .total_seconds()))
    except Exception:
        pass
    return None


def _hint(s: str, *needles) -> bool:
    s = (s or '').lower()
    return any(n in s for n in needles)


def _google_retry_delay(body: str) -> Optional[int]:
    """v4.14.5.14a.5: Google/Gemini 429s carry the retry hint inside
    the JSON body, not a Retry-After header:
      {"error":{"code":429,"status":"RESOURCE_EXHAUSTED","details":[
        {"@type":".../RetryInfo","retryDelay":"30s"}]}}
    Return the retryDelay in seconds, or None."""
    if not body:
        return None
    try:
        import re as _re
        m = _re.search(r'"retryDelay"\s*:\s*"?(\d+(?:\.\d+)?)s', body)
        if m:
            return max(1, int(float(m.group(1))))
    except Exception:
        pass
    return None


def classify_429(provider_name: str, meta: dict,
                  body: str = '',
                  secs_since_last_success: Optional[float] = None,
                  model=None) -> dict:
    """Return {'type': 'per_minute'|'daily'|'unknown',
               'retry_after_seconds': int}.

    Defensive: any ambiguity → 'unknown' so the caller keeps the old
    conservative 5-min cooldown (never worse than before this patch).

    v4.14.5.14rot-p3a: `model` is accepted for caller uniformity with
    the other learning functions but does NOT affect the result —
    classification is stateless (pure header/body parsing, no _state
    read or write), so there is nothing per-model to key on here.
    """
    headers = (meta or {}).get('headers') or {}
    ra = _parse_retry_after(headers.get('retry-after', ''))
    fam = (provider_name or '').strip().lower()
    _v2 = _classify429_v2_on()  # v4.14.5.14-classify429-fix

    # v4.14.5.14a.5: Google/Gemini put the retry hint in the JSON body
    # (retryDelay), and 429s come as status RESOURCE_EXHAUSTED — almost
    # always a per-minute throttle on the free tier (15 RPM), NOT daily
    # exhaustion. Only treat as daily if the message explicitly says
    # so or retryDelay is very long. This ends the Gemini cooldown loop
    # (was falling to 'unknown' → 5-min legacy).
    g_delay = _google_retry_delay(body)
    g_429 = (_hint(body, 'resource_exhausted',
                   'resource has been exhausted')
             or g_delay is not None)
    if g_429 and not _hint(body, 'per day', 'daily limit',
                            'daily quota', 'quota for the day'):
        if g_delay is not None and g_delay > 600:
            return {'type': 'daily',
                    'retry_after_seconds': g_delay}
        return {'type': 'per_minute',
                'retry_after_seconds':
                g_delay if g_delay else (ra if ra else 30)}

    # v4.14.5.14-retry-and-cleanup-bundle Fix B: Groq's 429 body carries a
    # structured error.type that names the exact limit (per-minute tokens
    # vs daily requests). Read it directly — more reliable than the generic
    # keyword scan below — mirroring the Gemini body special-case above.
    # Flag-gated (use_provider_429_parsers, default True); fail-open: a
    # non-JSON / unknown body returns None and falls through unchanged.
    if fam == 'groq' and _provider_429_parsers_on():
        _gr = _parse_groq_429_body(body)
        if _gr is not None:
            return _gr

    # v4.14.5.14-429-parsers-and-lookup-tier3 (Fix A): the other
    # OpenAI-compatible providers (Mistral / Cerebras / GitHub Models)
    # carry a structured error.code/type/message in their 429 body. Same
    # gate + fail-open contract as Groq above; unknown bodies return None
    # and fall through to the v2 header/keyword rules unchanged.
    if (fam in ('mistral', 'cerebras', 'github')
            and _provider_429_parsers_on()):
        _oai = _parse_openai_compatible_429_body(body)
        if _oai is not None:
            return _oai

    if _v2:
        # ── v4.14.5.14-classify429-fix: 7-rule priority order ──
        # (use_classify_429_v2, default True). The Google/Gemini
        # body special-case above is shared and already returned for
        # those. Strong explicit signals first (day-remaining header,
        # then daily body keyword — preserving the legacy
        # body-before-Retry-After precedence per "keep existing
        # body-text matching"), then Retry-After heuristics, then the
        # load-bearing ambiguous fallback.
        #
        # Rules 1 & 2 — the day-remaining header is authoritative.
        for k in ('x-ratelimit-remaining-requests-day',
                  'x-ratelimit-remaining-tokens-day'):
            v = headers.get(k)
            if v is None:
                continue
            try:
                iv = int(float(v))
            except (TypeError, ValueError):
                continue
            if iv <= 0:
                return {'type': 'daily',          # Rule 1
                        'retry_after_seconds':
                        ra if (ra and ra > 1200)
                        else _seconds_to_utc_midnight()}
            # Rule 2 (NEW): header explicitly says daily budget
            # REMAINS → this 429 must be a shorter-window throttle.
            return {'type': 'per_minute',
                    'retry_after_seconds': ra if ra else 30}
        # Rule 7 — explicit daily body keyword (kept verbatim; strong
        # signal, kept ahead of the Retry-After heuristics exactly as
        # the legacy path ordered it).
        if _hint(body, 'daily limit', 'per day',
                 'quota exceeded for today', 'requests per day',
                 'daily quota', 'rpd'):
            return {'type': 'daily',
                    'retry_after_seconds':
                    ra if (ra and ra > 1200)
                    else _seconds_to_utc_midnight()}
        # Rule 4 — Retry-After ≥ 20 min is a daily-reset-scale wait.
        if ra is not None and ra >= 1200:
            return {'type': 'daily', 'retry_after_seconds': ra}
        # Rules 3 & 5 — ANY Retry-After under 20 min (≤60s or the
        # 60s–20min band) is a shorter window, never a daily reset
        # (those happen at UTC midnight, not minutes from now).
        if ra is not None and 0 < ra < 1200:
            return {'type': 'per_minute', 'retry_after_seconds': ra}
        # Per-minute body / minute-header / recent-success signals
        # (kept verbatim from legacy).
        if _hint(body, 'per minute', 'per second',
                 'requests per minute', 'tokens per minute', 'rpm',
                 'tpm', 'rate limit', 'too many requests',
                 'slow down'):
            return {'type': 'per_minute',
                    'retry_after_seconds': ra if ra else 30}
        for k in ('x-ratelimit-remaining-requests-minute',
                  'x-ratelimit-remaining-requests',
                  'x-ratelimit-remaining-tokens'):
            v = headers.get(k)
            if v is not None:
                try:
                    if int(float(v)) <= 0:
                        return {'type': 'per_minute',
                                'retry_after_seconds':
                                ra if ra else 30}
                except (TypeError, ValueError):
                    pass
        if (secs_since_last_success is not None
                and secs_since_last_success < 60):
            return {'type': 'per_minute',
                    'retry_after_seconds': ra if ra else 30}
        # Rule 6 (LOAD-BEARING) — no daily header, no daily body
        # keyword, no Retry-After: ambiguous. Default PER-MINUTE
        # (safe direction). Misreading a daily as per-minute costs
        # one minute of retries and self-corrects on the next call;
        # misreading a per-minute as daily ratchets the daily cap
        # permanently (the bug this patch closes). 'ambiguous' is a
        # pure return field — classify_429 makes NO side-effect
        # write (A10); the consumer emits the one-shot diagnostic.
        return {'type': 'per_minute',
                'retry_after_seconds': ra if ra else 60,
                'ambiguous': True}

    # ── flag OFF (use_classify_429_v2=False): exact pre-patch path,
    # byte-identical legacy classification for instant rollback ──
    # Daily signals (strongest first).
    for k in ('x-ratelimit-remaining-requests-day',
              'x-ratelimit-remaining-tokens-day'):
        v = headers.get(k)
        if v is not None:
            try:
                if int(float(v)) <= 0:
                    return {'type': 'daily',
                            'retry_after_seconds':
                            ra if (ra and ra > 1200)
                            else _seconds_to_utc_midnight()}
            except (TypeError, ValueError):
                pass
    if _hint(body, 'daily limit', 'per day', 'quota exceeded for today',
             'requests per day', 'daily quota', 'rpd'):
        return {'type': 'daily',
                'retry_after_seconds':
                ra if (ra and ra > 1200)
                else _seconds_to_utc_midnight()}
    if ra is not None and ra >= 1200:
        return {'type': 'daily', 'retry_after_seconds': ra}

    # Per-minute signals.
    if _hint(body, 'per minute', 'per second', 'requests per minute',
             'tokens per minute', 'rpm', 'tpm', 'rate limit',
             'too many requests', 'slow down'):
        return {'type': 'per_minute',
                'retry_after_seconds': ra if ra else 30}
    if ra is not None and 0 < ra < 1200:
        return {'type': 'per_minute', 'retry_after_seconds': ra}
    for k in ('x-ratelimit-remaining-requests-minute',
              'x-ratelimit-remaining-requests',
              'x-ratelimit-remaining-tokens'):
        v = headers.get(k)
        if v is not None:
            try:
                if int(float(v)) <= 0:
                    return {'type': 'per_minute',
                            'retry_after_seconds': ra if ra else 30}
            except (TypeError, ValueError):
                pass
    if (secs_since_last_success is not None
            and secs_since_last_success < 60):
        # Just made a successful call seconds ago → almost certainly a
        # burst/per-minute limit, not a daily wall.
        return {'type': 'per_minute',
                'retry_after_seconds': ra if ra else 30}

    # v4.14.5.14a.5: Gemini is a KNOWN provider whose free-tier 429s
    # are virtually always per-minute (15 RPM). An unparsed Gemini 429
    # should get a SHORT retry, not the 5-min "unknown provider"
    # legacy — that was the cooldown loop. Other unknown providers
    # keep the conservative 300s.
    if fam == 'gemini':
        return {'type': 'per_minute',
                'retry_after_seconds': ra if ra else 60}
    return {'type': 'unknown',
            'retry_after_seconds': ra if ra else 300}


# ── learning from successful responses ────────────────────────────────

def note_success_headers(family: str, meta: dict,
                          seed_cap: Optional[int] = None,
                          model=None) -> Optional[int]:
    """B5a/B5b: parse a successful response's headers. If the provider
    reports its real daily request cap, update the learned cap to
    match. Returns the new learned cap if changed, else None. No-op
    when the provider sends no day-cap header (the 'no signal' case).

    v4.14.5.14a.5 sanity ceiling: some endpoints report a header that
    is NOT the free-tier request/day cap — GitHub Models via the Azure
    endpoint reports X-RateLimit-Limit-Requests-Day = 60000, but the
    real GitHub Models free tier is ~50/day. Learning 60000 then
    crashing into the true wall at ~72 produced the daily-cap loop.
    If a reported value is wildly above the family's URL seed
    (> seed * _SEED_SANITY_MULT), it's the wrong number — ignore it
    and keep the conservative seed. Legit values (Cerebras 2400 vs
    seed 1500 = 1.6x; Groq 14400 vs 14000 = 1.03x) pass fine."""
    if not family or family == 'unknown':
        return None
    headers = (meta or {}).get('headers') or {}
    reported = None
    for k in ('x-ratelimit-limit-requests-day',
              'x-ratelimit-limit-requests'):
        v = headers.get(k)
        if v is not None:
            try:
                iv = int(float(v))
                if iv > 0:
                    reported = iv
                    break
            except (TypeError, ValueError):
                pass
    if (reported is not None and seed_cap and seed_cap > 0
            and reported > seed_cap * _SEED_SANITY_MULT):
        # Bogus header for this small-tier provider — don't learn it.
        try:
            _log_change(family, 'daily_cap',
                        '(reported ' + str(reported) + ')',
                        'IGNORED',
                        f'header {reported} >> seed {seed_cap} x'
                        f'{_SEED_SANITY_MULT} — not a free-tier RPD '
                        f'cap (kept seed)', model=model)
        except Exception:
            pass
        reported = None
    with _lock:
        _load_locked()
        r = _rec_locked(family, model)
        r['today_calls'] = int(r.get('today_calls', 0)) + 1
        changed = None
        if reported is not None and reported != r.get('daily_cap'):
            old = r.get('daily_cap')
            r['daily_cap'] = reported
            r['last_updated'] = datetime.now(
                timezone.utc).isoformat()
            r['source'] = 'X-RateLimit-Limit-Requests-Day header'
            _log_change(family, 'daily_cap', old, reported,
                        'provider header X-RateLimit-Limit-Requests-'
                        'Day reported ' + str(reported), model=model)
            changed = reported
        # Observed-behaviour fallback: at 90% of a known cap with zero
        # 429s today, the provider is clearly allowing more — bump 20%.
        cap = r.get('daily_cap')
        if (reported is None and cap and cap > 0
                and r.get('today_429s', 0) == 0
                and r['today_calls'] >= int(cap * 0.9)):
            old = cap
            new = int(cap * 1.2)
            r['daily_cap'] = new
            r['last_updated'] = datetime.now(timezone.utc).isoformat()
            r['source'] = 'observed: 90% reached, no 429s'
            _log_change(family, 'daily_cap', old, new,
                        f'{r["today_calls"]} calls, 0 429s — at 90% '
                        f'of {old}, raising 20%', model=model)
            changed = new
        _save_locked()
        return changed


def note_daily_429(family: str, trip_count: int, model=None) -> None:
    """B5: a daily-classified 429 at trip_count calls — the real cap is
    here. Lower the learned cap to it."""
    if not family or family == 'unknown' or trip_count <= 0:
        return
    with _lock:
        _load_locked()
        r = _rec_locked(family, model)
        r['today_429s'] = int(r.get('today_429s', 0)) + 1
        old = r.get('daily_cap')
        if old is None or trip_count < old:
            r['daily_cap'] = trip_count
            r['last_updated'] = datetime.now(timezone.utc).isoformat()
            r['source'] = 'daily 429 received'
            _log_change(family, 'daily_cap', old, trip_count,
                        f'daily-classified 429 at {trip_count} calls',
                        model=model)
            _save_locked()


def get_learned_cap(family: str, model=None) -> Optional[int]:
    """The learned working cap, or None if nothing learned yet (caller
    falls back to the hardcoded seed)."""
    if not family or family == 'unknown':
        return None
    with _lock:
        _load_locked()
        fam_rec = _state.get(family)
        if not isinstance(fam_rec, dict):
            return None
        rec = fam_rec.get(_model_key(model))
        if not rec:
            return None
        c = rec.get('daily_cap')
        try:
            return int(c) if c and int(c) > 0 else None
        except (TypeError, ValueError):
            return None


def today_pressure_for(family: str) -> tuple[int, int]:
    """v4.14.5.14-capacity-weighted-scan: READ-ONLY. Returns
    (today_calls, today_429s) summed across ALL of a family's model
    slots for today's UTC date (stale-day slots count as 0). Used by the
    capacity-weighted scan router to de-weight a provider that is
    stressing (throwing many 429s) today. Never mutates state (unlike
    _rec_locked, it does not roll the day or create slots), never
    raises; fail-open to (0, 0)."""
    try:
        if not family:
            return (0, 0)
        with _lock:
            _load_locked()
            fam_rec = _state.get(family)
            if not isinstance(fam_rec, dict):
                return (0, 0)
            today = _today_utc()
            calls = 0
            r429 = 0
            for r in fam_rec.values():
                if not isinstance(r, dict):
                    continue
                if r.get('today_iso') != today:
                    continue  # stale day → counts as 0 for today
                try:
                    calls += int(r.get('today_calls') or 0)
                    r429 += int(r.get('today_429s') or 0)
                except (TypeError, ValueError):
                    continue
            return (calls, r429)
    except Exception:
        return (0, 0)


def clear_learned(family: str, reason: str = 'manual reset',
                  model=None) -> None:
    with _lock:
        _load_locked()
        fam_rec = _state.get(family)
        if isinstance(fam_rec, dict):
            rec = fam_rec.get(_model_key(model))
            if rec is not None:
                old = rec.get('daily_cap')
                rec['daily_cap'] = None
                rec['source'] = 'cleared'
                _log_change(family, 'daily_cap', old, None, reason,
                            model=model)
                _save_locked()


def clear_all_learned(reason: str = 'manual reset') -> int:
    """v4.14.5.28 (Fix 5): UNCONDITIONALLY clear every learned daily_cap
    back to None across all families/models. A None cap makes the engine
    fall back to the documented PRESETS default (`default_max_per_day`),
    i.e. "restore to documented defaults; re-learn from the next real
    429." This is the MANUAL user-action path (Settings → AI Providers →
    Reset learned caps). The conservative auto-path (`_sanity_wipe_locked`,
    threshold-gated) is intentionally NOT touched. Returns the number of
    records cleared. Never raises."""
    cleared = 0
    try:
        with _lock:
            _load_locked()
            for fam, fam_rec in _state.items():
                if not isinstance(fam_rec, dict):
                    continue
                for mkey, rec in fam_rec.items():
                    if not isinstance(rec, dict):
                        continue
                    if rec.get('daily_cap') is not None:
                        old = rec.get('daily_cap')
                        rec['daily_cap'] = None
                        rec['source'] = 'cleared'
                        _log_change(fam, 'daily_cap', old, None, reason,
                                    model=mkey)
                        cleared += 1
            if cleared:
                _save_locked()
    except Exception:
        pass
    return cleared
