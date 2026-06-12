"""tm_model_cursor — v4.14.5.14rot Patch 2b.

Persistent per-provider model-rotation cursor + the flag gate that
turns rotation on. Sibling of tm_provider_learning.py (same data dir,
same lock + lazy-load + atomic temp→replace persistence pattern).

Why a separate module: tm_ai_router is deliberately app-config-
agnostic (no app.cfg / load_config), so resolve_provider_model can't
read the rotation flags itself. This module reads them straight off
data/config.json (short TTL cache — cheap, no per-call disk hit) and
owns data/provider_model_cursors.json.

Design choices that matter:
  - The stored value is a MONOTONIC counter per provider id, not a
    pre-wrapped index. get_next_model_index() does `counter % n` at
    READ time, so a shrunk/grown models list, or n==0, is handled
    automatically with zero special-casing and can never be out of
    range. That also means advance_cursor() needs ONLY provider_id —
    no models_count — so it fits the single all-outcomes chokepoint
    record_call_outcome_for_model(provider_id, ...) which doesn't
    have the models list.
  - Writes are atomic + merge-preserving (re-read disk, merge our
    keys over it, temp + fsync + os.replace) — concurrent provider
    cursors never clobber each other (the .14a.12 pattern).
  - EVERY entry point fails OPEN / never raises. Rotation is a
    convenience layer; it must never wedge or crash dispatch. Any
    fault → behave as rotation-off (legacy singular model).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_DATA_DIR = Path(__file__).parent / "data"
_STORE = _DATA_DIR / "provider_model_cursors.json"
_CFG = _DATA_DIR / "config.json"

_lock = threading.Lock()
_flag_cache = {"at": 0.0, "on": False}
_pmc_cache = {"at": 0.0, "on": False}
_FLAG_TTL_SECONDS = 5.0

# v4.14.5.62-autoenable-rotation: per-session EFFECTIVE override. The config
# value can now be the sentinel "auto" (resolved against provider/model count),
# but this module is deliberately app-config-agnostic and can't count
# providers itself. So App resolves "auto" → bool ONCE at startup (and on
# Settings-save) with app context and PUSHES the result here via the setters
# below — mirroring how tm_consensus.set_parallel_consensus receives its
# resolved bool. None = no override pushed yet → fall back to the direct
# config read (which treats the "auto" string conservatively as OFF until the
# push lands, and still honors a legacy explicit `True`).
_effective = {"rotation": None, "pmc": None}


def set_rotation_effective(on) -> None:
    """Push the resolved rotation effective value (True/False), or None to
    clear back to the config-read fallback. Authoritative for the session."""
    with _lock:
        _effective["rotation"] = None if on is None else bool(on)


def set_per_model_caps_effective(on) -> None:
    """Push the resolved per-model-cap-tracking effective value (True/False),
    or None to clear back to the config-read fallback."""
    with _lock:
        _effective["pmc"] = None if on is None else bool(on)


def rotation_enabled() -> bool:
    """True only when BOTH use_model_rotation_schema AND
    use_model_rotation_router resolve True. Honors the pushed effective
    override first (set by App after resolving the "auto" sentinel); falling
    back to a direct data/config.json read (cached for _FLAG_TTL_SECONDS).
    The fallback treats a value as on ONLY when it is literally `True` — so the
    "auto" sentinel reads conservatively OFF until the startup push lands,
    while a legacy explicit `True` still works app-less.
    Fail-OPEN to False (→ legacy singular model)."""
    with _lock:
        ov = _effective["rotation"]
    if ov is not None:
        return ov
    now = time.time()
    with _lock:
        if (now - _flag_cache["at"]) < _FLAG_TTL_SECONDS:
            return _flag_cache["on"]
    on = False
    try:
        if _CFG.exists():
            c = json.loads(_CFG.read_text("utf-8"))
            if isinstance(c, dict):
                on = (c.get("use_model_rotation_schema") is True
                      and c.get("use_model_rotation_router") is True)
    except Exception:
        on = False
    with _lock:
        _flag_cache["at"] = now
        _flag_cache["on"] = on
    return on


def per_model_caps_enabled() -> bool:
    """v4.14.5.14rot Patch 3b: True when use_per_model_cap_tracking is
    True in data/config.json. INDEPENDENT of the two rotation flags —
    per-model cap keying is a self-contained capability (Patch 3a
    shipped the store; this is its consumer). The intra-provider
    cursor-SKIP of exhausted models additionally needs
    rotation_enabled() + a >1 models list; the caller ANDs those.
    Own TTL cache (separate from rotation_enabled's, so the two flags
    can't alias). Fail-OPEN to False (→ legacy _default behaviour,
    byte-identical to Patch 3a).

    v4.14.5.62-autoenable-rotation: honors the pushed effective override
    first (App resolves the "auto" sentinel); the config-read fallback treats
    a value as on ONLY when literally `True` (so "auto" reads conservatively
    OFF until the push lands; a legacy explicit `True` still works app-less)."""
    with _lock:
        ov = _effective["pmc"]
    if ov is not None:
        return ov
    now = time.time()
    with _lock:
        if (now - _pmc_cache["at"]) < _FLAG_TTL_SECONDS:
            return _pmc_cache["on"]
    on = False
    try:
        if _CFG.exists():
            c = json.loads(_CFG.read_text("utf-8"))
            if isinstance(c, dict):
                on = (c.get("use_per_model_cap_tracking") is True)
    except Exception:
        on = False
    with _lock:
        _pmc_cache["at"] = now
        _pmc_cache["on"] = on
    return on


# v4.14.5.14-classify429-fix: two opt-OUT burn-recovery flags. Unlike
# the rotation/per-model flags above (opt-IN, default False, fail-open
# to False=legacy), these default True and the patch SHIPS them on.
# Their fix direction is strictly burn-SAFER (only ever makes a 429
# LESS likely to ratchet a daily cap, and only ever clears an
# egregiously-low persisted cap that re-learns from headers — real
# provider 429s + the PRESETS floor still protect against over-spend).
# So the correct fail-open on a transient unreadable config is to the
# DEFAULT (True), not to legacy: a config hiccup must not silently
# resurrect the cap-ratchet bug. Own TTL caches so they can't alias.
_c429_cache = {"at": 0.0, "on": True}
_capwipe_cache = {"at": 0.0, "on": True}
_consec429_cache = {"at": 0.0, "on": True}
_p429parse_cache = {"at": 0.0, "on": True}


def classify_429_v2_enabled() -> bool:
    """True unless use_classify_429_v2 is explicitly False in
    data/config.json. Default True; fail-open True (see note above)."""
    now = time.time()
    with _lock:
        if (now - _c429_cache["at"]) < _FLAG_TTL_SECONDS:
            return _c429_cache["on"]
    on = True
    try:
        if _CFG.exists():
            c = json.loads(_CFG.read_text("utf-8"))
            if isinstance(c, dict):
                on = bool(c.get("use_classify_429_v2", True))
    except Exception:
        on = True
    with _lock:
        _c429_cache["at"] = now
        _c429_cache["on"] = on
    return on


def provider_429_parsers_enabled() -> bool:
    """v4.14.5.14-retry-and-cleanup-bundle Fix B: True unless
    use_provider_429_parsers is explicitly False in data/config.json.
    Default True; fail-open True. When True, classify_429 reads a
    provider's structured 429 body (currently Groq's error.type) before
    the generic keyword scan. Strictly safer — the parser is fail-open
    (unknown/non-JSON → falls through to the existing logic)."""
    now = time.time()
    with _lock:
        if (now - _p429parse_cache["at"]) < _FLAG_TTL_SECONDS:
            return _p429parse_cache["on"]
    on = True
    try:
        if _CFG.exists():
            c = json.loads(_CFG.read_text("utf-8"))
            if isinstance(c, dict):
                on = bool(c.get("use_provider_429_parsers", True))
    except Exception:
        on = True
    with _lock:
        _p429parse_cache["at"] = now
        _p429parse_cache["on"] = on
    return on


def consecutive_429_gate_enabled() -> bool:
    """v4.14.5.14-classify429-part-c (IDEAS Fix 2): True unless
    use_consecutive_429_gate is explicitly False in data/config.json.
    Default True; fail-open True. When True, a daily-classified 429 only
    tightens the learned/observed cap after `consecutive_429s >= 3` on
    that (provider, model)."""
    now = time.time()
    with _lock:
        if (now - _consec429_cache["at"]) < _FLAG_TTL_SECONDS:
            return _consec429_cache["on"]
    on = True
    try:
        if _CFG.exists():
            c = json.loads(_CFG.read_text("utf-8"))
            if isinstance(c, dict):
                on = bool(c.get("use_consecutive_429_gate", True))
    except Exception:
        on = True
    with _lock:
        _consec429_cache["at"] = now
        _consec429_cache["on"] = on
    return on


def persisted_cap_sanity_wipe_enabled() -> bool:
    """True unless use_persisted_cap_sanity_wipe is explicitly False in
    data/config.json. Default True; fail-open True (see note above).
    Read once at startup by tm_provider_learning._load_locked."""
    now = time.time()
    with _lock:
        if (now - _capwipe_cache["at"]) < _FLAG_TTL_SECONDS:
            return _capwipe_cache["on"]
    on = True
    try:
        if _CFG.exists():
            c = json.loads(_CFG.read_text("utf-8"))
            if isinstance(c, dict):
                on = bool(c.get("use_persisted_cap_sanity_wipe", True))
    except Exception:
        on = True
    with _lock:
        _capwipe_cache["at"] = now
        _capwipe_cache["on"] = on
    return on


def _read_store() -> dict:
    try:
        if _STORE.exists():
            d = json.loads(_STORE.read_text("utf-8"))
            if isinstance(d, dict):
                return dict(d.get("cursors", d))  # tolerate both
    except Exception:
        pass
    return {}


def _atomic_merge_write(updates: dict) -> None:
    """Re-read disk, merge `updates` over it, write atomically
    (temp + fsync + os.replace). Merge-preserving so a concurrent
    writer's other provider cursors aren't lost. Never raises."""
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        cur = _read_store()
        cur.update(updates)
        payload = {"schema_version": 1, "cursors": cur}
        tmp = _STORE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _STORE)
    except Exception:
        pass


def get_next_model_index(provider_id, models_count) -> int:
    """Index into the provider's models list for the NEXT call.
    counter % max(1, n). Absent / unreadable / junk → 0. Never
    raises, never out of range."""
    try:
        n = int(models_count or 1)
        if n < 1:
            return 0
        with _lock:
            raw = _read_store().get(str(provider_id), 0)
        try:
            c = int(raw)
        except (TypeError, ValueError):
            c = 0
        if c < 0:
            c = 0
        return c % n
    except Exception:
        return 0


def advance_cursor(provider_id) -> None:
    """Monotonic +1 for this provider (one dispatch attempt = one
    advance). No models_count needed — wrap is applied at read time.
    Atomic merge-preserving write. Never raises."""
    try:
        pid = str(provider_id)
        with _lock:
            cur = _read_store()
            try:
                c = int(cur.get(pid, 0))
            except (TypeError, ValueError):
                c = 0
            if c < 0:
                c = 0
            _atomic_merge_write({pid: c + 1})
    except Exception:
        pass


def reset_cursor(provider_id) -> None:
    """Force this provider's cursor to 0 (list-shrink-below-index is
    already auto-handled by the modulo in get_next_model_index; this
    is for explicit/user resets). Never raises."""
    try:
        _atomic_merge_write({str(provider_id): 0})
    except Exception:
        pass


def describe_rotation(provider: dict):
    """If rotation is active for THIS provider (both flags on AND a
    >1 models list), return a one-line diagnostic string for the
    activity log; else None (so single-model / flag-off providers
    stay silent — no log noise for the common case). Never raises."""
    try:
        if not rotation_enabled():
            return None
        models = provider.get("models")
        if not isinstance(models, (list, tuple)):
            return None
        ms = [str(m).strip() for m in models if str(m).strip()]
        if len(ms) <= 1:
            return None
        pid = (provider.get("id") or provider.get("name")
               or "?")
        idx = get_next_model_index(pid, len(ms))
        if not (0 <= idx < len(ms)):
            return None
        name = (provider.get("name") or provider.get("display_name")
                or pid)
        return (f"[router] {name} [{len(ms)} models]: "
                f"cursor={idx} → {ms[idx]}")
    except Exception:
        return None
