"""
tm_provider_health.py — AI provider cooldown + daily-cap tracker
                          (v4.14.0 — model-aware schema v2)

What this is:
    A small, thread-safe state holder that tracks two layers of
    AI-provider health:

        1. Per-(provider, canonical_model) — `ModelHealth`.
           One record per (provider_id, canonical_model) pair. This
           is the layer the v4.14.0 router uses for cooldowns,
           per-model daily caps, observed-quota learning, and
           auto-raised caps. A 429 on Groq Llama 70B records against
           ("groq-id", "meta/llama-3.3-70b-instruct") and DOES NOT
           cool down ("groq-id", "meta/llama-3.1-8b-instruct"),
           which is gap 1 from the routing rework design.

        2. Per-provider org rollup — `ProviderOrgHealth`.
           Tracks shared org-level limits: e.g. Groq's free tier
           has both per-model caps (Llama 70B ~1k RPD) AND a single
           org-level ceiling (~20k RPD across all models). The router
           checks BOTH layers; either failing means "not safe to
           call right now."

What it's NOT:
    - Not the router. This module is state; tm_ai_router.py is policy.
    - Not the registry. The canonical_model strings come from
      tm_model_registry.py; this module just consumes them as opaque
      keys.

Persistence:
    State persists to data/provider_health.json. The file uses
    `schema_version: 2`. On first load of a v1 file (the format used
    through v4.13.x), every old per-provider record is migrated to
    a single ModelHealth with canonical_model = "legacy" plus an
    empty ProviderOrgHealth, and the file is rewritten as v2. The
    legacy record keeps the running count + cooldown intact, so
    nothing about the user's current quota state is lost during the
    schema bump — it just gets keyed differently.

Back-compat method signatures:
    Stage 2 of v4.14.0 ships THIS module ahead of stage-3-5 changes
    to the router/api/consensus that actually feed canonical_model
    into the calls. So every public method accepts an optional
    canonical_model keyword. When omitted (existing callers that
    haven't been updated yet), the call routes to canonical_model =
    "legacy" — exactly where the migration put pre-v4.14.0 state.
    This means stage 2 is behaviorally identical to v4.13.65 for
    end users; the new shape becomes useful once stages 3-5 land.

Default cooldown is 5 minutes. We chose 5 minutes because:
    - Most free-tier limits are per-minute or burst windows. 5 min
      is well past those.
    - For monthly limits, 5 min isn't long enough — but the daily
      cap mechanism handles those. The cooldown is for transient
      bursts.

Daily cap defaults are CONSERVATIVE. The user sets explicit caps
in config. If no cap configured, we don't enforce one (preserves
existing behavior). The cap field on each provider is
`max_calls_per_day` (int).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─── Tunables ──────────────────────────────────────────────────────────

DEFAULT_COOLDOWN_SEC = 300  # 5 minutes after a 429
LONG_COOLDOWN_SEC = 3600    # 1 hour for repeated 429s in a row

# v4.14.5.71-per-minute-cooldown-cap: hard ceiling for per-minute 429
# cooldowns. A per-minute rate limit clears in ~60s by definition; if
# the caller didn't supply a Retry-After value we still must not
# escalate past this. Pre-v4.14.5.71 the consensus-side call site
# passed no cooldown_sec, fell into the dumb 300/300/3600 step curve,
# and benched providers (Groq specifically) for ~1 hour on the 3rd
# per-minute strike. The structural fix here is: never escalate a
# per-minute hit to LONG_COOLDOWN, regardless of strike count.
PER_MINUTE_COOLDOWN_CAP_SEC = 120

# Sentinel canonical_model used when a caller doesn't specify one
# (back-compat with v4.13.x callers). Also the value migration uses
# when rewriting v1 records as v2 ModelHealth entries.
LEGACY_CANONICAL = "legacy"

SCHEMA_VERSION = 2

# Save-debounce window for the periodic save in record_success
# (matches the v4.13.60 cadence). Keeps the JSON on disk roughly in
# sync without hammering the disk during big batch scans.
_SAVE_DEBOUNCE_SEC = 30.0


# ─── Per-(provider, canonical_model) state ────────────────────────────

@dataclass
class ModelHealth:
    """Tracks one (provider, canonical_model) pair's recent behavior."""
    provider_id: str
    canonical_model: str = LEGACY_CANONICAL
    # Cooldown
    cooldown_until_epoch: float = 0.0  # 0 = no cooldown
    last_429_epoch: float = 0.0
    consecutive_429s: int = 0
    # Daily counters (reset at midnight)
    calls_today: int = 0
    fails_today: int = 0
    today_iso: str = ""
    # Last error (informational)
    last_error: str = ""
    last_error_epoch: float = 0.0
    # Observed daily ceiling — learned from real 429s. Set when the
    # server 429s at call N today; we record N-margin as the ceiling.
    # Persists across restarts. Beats the declared cap when tighter.
    observed_max_per_day: Optional[int] = None
    # Auto-raised cap — learned from sustained success past the
    # declared cap. If we make N successful calls without ever
    # hitting a 429 and N is well past the declared cap, the
    # declared was too conservative. Raise to (N + 20% margin) and
    # persist. On the next 429 this resets to None and observed_max
    # takes over (real ceiling found).
    raised_cap: Optional[int] = None

    def in_cooldown(self, now: float) -> bool:
        return self.cooldown_until_epoch > now

    def cooldown_remaining_sec(self, now: float) -> int:
        if not self.in_cooldown(now):
            return 0
        return int(self.cooldown_until_epoch - now)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'ModelHealth':
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        clean = {k: v for k, v in d.items() if k in valid}
        if 'provider_id' not in clean:
            clean['provider_id'] = 'unknown'
        clean.setdefault('canonical_model', LEGACY_CANONICAL)
        return cls(**clean)


# ─── Per-provider org rollup ──────────────────────────────────────────

@dataclass
class ProviderOrgHealth:
    """Tracks shared, org-level limits for one provider (e.g.
    Groq's ~20k RPD across all models). Distinct from ModelHealth
    so a model-specific 429 doesn't contaminate the org view, and
    so the org ceiling can be enforced independently of any single
    model's cap."""
    provider_id: str
    org_calls_today: int = 0
    org_observed_max_per_day: Optional[int] = None
    today_iso: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'ProviderOrgHealth':
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        clean = {k: v for k, v in d.items() if k in valid}
        if 'provider_id' not in clean:
            clean['provider_id'] = 'unknown'
        return cls(**clean)


# Back-compat alias for any external code that imported the old name.
# All v4.13.x callers used the singleton's methods rather than the
# dataclass directly, but we keep the alias just in case.
ProviderHealth = ModelHealth


# ─── Module-level state container ─────────────────────────────────────

def _classify_failure(error: str) -> str:
    """v4.14.5.14-status-dot-meaning: classify a non-429 error string into
    a dot state. auth = key problem (red, "check your key"); transient =
    server/network (red, "usually clears"); else other."""
    e = (error or "").lower()
    if ("401" in e or "403" in e or "unauthorized" in e
            or "forbidden" in e or "invalid api key" in e
            or "invalid_api_key" in e or "authentication" in e
            or "api key" in e):
        return "failed_auth"
    if ("503" in e or "502" in e or "504" in e or "500" in e
            or "timeout" in e or "timed out" in e
            or "connection" in e or "temporarily unavailable" in e
            or "network" in e):
        return "failed_transient"
    return "failed_other"


def resolve_dot_state(enabled: bool, last_call_state: str,
                      in_cooldown: bool, cooldown_sec: int = 0,
                      health_enabled: bool = True,
                      deprecated: bool = False) -> tuple[str, str]:
    """v4.14.5.14-status-dot-meaning: PURE resolver from health inputs to
    (color, tooltip). color is one of 'green' / 'red' / 'amber' / 'gray' /
    'deprecated'. Single source of truth shared by the AI Providers dialog dot
    and the audit (per HANDOFF item 16 the audit drives this directly).

    Priority (per the patch spec D1):
      disabled -> gray; DEPRECATED (model retired, no replacement) -> deprecated;
      no calls yet -> gray; in cooldown -> amber; last call succeeded -> green;
      last call failed -> red.

    v4.14.6.111 (Item 5): `deprecated` is a PERSISTENT state for an ENABLED
    provider whose configured model is vendor-retired with no live replacement
    (autoheal swaps the replaceable ones at startup, so this only lights up for
    no-replacement retirements the user must resolve by hand). It outranks the
    transient cooled/last-call states (a retired model is a standing problem),
    but never shows for a DISABLED provider (it doesn't dispatch).

    health_enabled=False -> legacy/rollback: the dot just mirrors the
    enable toggle (green if enabled, gray if not)."""
    if not health_enabled:
        return (("green", "Provider enabled.") if enabled
                else ("gray", "Provider disabled."))
    if not enabled:
        return ("gray", "Provider disabled.")
    if deprecated:
        return ("deprecated",
                "Model retired, no replacement available — choose another "
                "model for this provider.")
    lcs = last_call_state or "unknown"
    if lcs == "unknown":
        return ("gray", "Not yet called this session.")
    if in_cooldown:
        return ("amber",
                f"Cooling down {int(cooldown_sec)}s after a rate limit.")
    if lcs == "success":
        return ("green", "Last call: succeeded.")
    if lcs == "failed_auth":
        return ("red", "Last call: failed (401 unauthorized) — "
                       "check your API key.")
    if lcs == "failed_transient":
        return ("red", "Last call: failed (server/network issue) — the "
                       "provider's servers may be having trouble; usually "
                       "clears on its own.")
    if lcs == "failed_429":
        return ("red", "Last call: rate-limited (429), not currently "
                       "cooling down — daily quota likely reached.")
    if lcs.startswith("failed"):
        return ("red", "Last call: failed — see the activity log for "
                       "detail.")
    return ("gray", "Not yet called this session.")


class HealthState:
    """Thread-safe holder for all provider health records (v4.14.0).

    Tracks two layers:
      - Per-(provider_id, canonical_model) → ModelHealth
      - Per-provider                       → ProviderOrgHealth

    Public methods accept canonical_model as an optional keyword. When
    omitted, the call routes to canonical_model="legacy" so existing
    v4.13.x callers continue to work unchanged.
    """

    def __init__(self, json_path: Optional[Path] = None):
        self._lock = threading.Lock()
        self._path = json_path
        self._model_records: dict[tuple[str, str], ModelHealth] = {}
        self._org_records: dict[str, ProviderOrgHealth] = {}
        self._loaded = False
        self._last_persist_at = 0.0
        # v4.14.5.14-status-dot-meaning: SESSION-ONLY per-provider last-call
        # result. Deliberately NOT loaded from disk and NOT persisted —
        # "SambaNova was red yesterday" says nothing about today, so every
        # restart starts blank ('unknown' = gray dot) and fills in as
        # providers get called. Keys: provider_id -> 'success' /
        # 'failed_auth' / 'failed_transient' / 'failed_429' / 'failed_other'.
        # Drives the AI Providers health dot + the toolbar heartbeat so a
        # 401-failing provider stops showing green.
        self._last_call_state: dict[str, str] = {}

    # ── Lifecycle ──

    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            if self._path is None or not self._path.exists():
                return
            try:
                with open(self._path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                return

            schema = data.get('schema_version')
            # v1 detection: either explicit `version: 1` (the old
            # field name) or a v1-shaped payload (no schema_version
            # and a top-level `records` array).
            is_v1 = (schema is None
                     and ('records' in data or 'version' in data)
                     and 'models' not in data)

            if is_v1:
                data = self._migrate_v1_to_v2_locked(data)
                # Mark for post-load save outside the lock (save()
                # acquires self._lock).
                self._needs_post_load_save = True
            else:
                self._needs_post_load_save = False

            for entry in (data.get('models') or []):
                try:
                    rec = ModelHealth.from_dict(entry)
                    key = (rec.provider_id, rec.canonical_model)
                    self._model_records[key] = rec
                except Exception:
                    continue

            for entry in (data.get('orgs') or []):
                try:
                    rec = ProviderOrgHealth.from_dict(entry)
                    self._org_records[rec.provider_id] = rec
                except Exception:
                    continue

        # Save outside the lock so save()'s lock-acquire doesn't
        # deadlock against the load lock.
        if getattr(self, '_needs_post_load_save', False):
            self._needs_post_load_save = False
            try:
                self.save()
            except Exception:
                pass

    @staticmethod
    def _migrate_v1_to_v2_locked(v1: dict) -> dict:
        """Rewrite a v1 payload as v2: each per-provider record
        becomes a ModelHealth with canonical_model='legacy' plus an
        empty ProviderOrgHealth. Existing counters/cooldowns are
        preserved verbatim — only the keying changes."""
        v1_records = v1.get('records') or []
        model_records: list[dict] = []
        org_records: list[dict] = []
        seen_orgs: set[str] = set()

        for entry in v1_records:
            provider_id = entry.get('provider_id', 'unknown')
            mh = {k: v for k, v in entry.items()}
            mh['canonical_model'] = LEGACY_CANONICAL
            model_records.append(mh)
            if provider_id not in seen_orgs:
                seen_orgs.add(provider_id)
                org_records.append({
                    'provider_id': provider_id,
                    'org_calls_today': 0,
                    'org_observed_max_per_day': None,
                    'today_iso': entry.get('today_iso', ''),
                })

        return {
            'schema_version': SCHEMA_VERSION,
            'models': model_records,
            'orgs': org_records,
            'saved_at': time.time(),
            'migrated_from_v1_at': datetime.now().isoformat(),
        }

    def save(self) -> None:
        if self._path is None:
            return
        with self._lock:
            data = {
                'schema_version': SCHEMA_VERSION,
                'models': [r.to_dict() for r
                           in self._model_records.values()],
                'orgs':   [r.to_dict() for r
                           in self._org_records.values()],
                'saved_at': time.time(),
            }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + '.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            tmp.replace(self._path)
        except Exception:
            pass

    # ── Read API ──

    def is_safe_to_call(self, provider_id: str, *,
                         canonical_model: Optional[str] = None,
                         max_per_day: Optional[int] = None,
                         org_max_per_day: Optional[int] = None
                         ) -> tuple[bool, str]:
        """Check whether (provider_id, canonical_model) can be called
        right now, and whether the provider's org ceiling allows it.

        Args:
            provider_id: stable provider id from config
            canonical_model: canonical model string from
                tm_model_registry. None = LEGACY_CANONICAL (back-
                compat for callers not yet updated).
            max_per_day: per-(provider, model) declared daily cap.
                None = no per-model cap to enforce.
            org_max_per_day: provider-level org cap (e.g. Groq's
                ~20k RPD across all models). None = no org cap.

        Returns:
            (is_safe, reason_if_not_safe)

        Both layers must pass. The first failing layer is the
        reported reason.
        """
        cm = canonical_model or LEGACY_CANONICAL
        now = time.time()
        with self._lock:
            rec = self._model_records.get((provider_id, cm))
            self._roll_over_day_locked(rec) if rec else None

            # Per-(provider, model) cooldown
            if rec is not None and rec.in_cooldown(now):
                remaining = rec.cooldown_remaining_sec(now)
                cm_label = '' if cm == LEGACY_CANONICAL else f" [{cm}]"
                return (False,
                        f"in cooldown for another {remaining}s "
                        f"(after {rec.consecutive_429s} rate-limit"
                        f"{'s' if rec.consecutive_429s != 1 else ''})"
                        f"{cm_label}")

            # Per-(provider, model) cap resolution. Same precedence
            # as v4.13.59:
            #   1. observed_max_per_day (real 429) — wins always
            #   2. max(declared, raised_cap) — if past declared OK
            #   3. declared (max_per_day arg)
            effective = max_per_day
            cap_source = 'declared'
            if (rec is not None and rec.raised_cap is not None
                    and (effective is None or rec.raised_cap > effective)):
                effective = rec.raised_cap
                cap_source = 'auto-raised'
            if rec is not None and rec.observed_max_per_day:
                effective = rec.observed_max_per_day
                cap_source = 'observed-from-429'

            if effective is not None and effective > 0:
                used = rec.calls_today if rec else 0
                if used >= effective:
                    if cap_source == 'observed-from-429':
                        suffix = " (learned from 429)"
                    elif cap_source == 'auto-raised':
                        suffix = " (auto-raised after success)"
                    else:
                        suffix = ""
                    cm_label = '' if cm == LEGACY_CANONICAL else f" [{cm}]"
                    return (False,
                            f"daily cap reached "
                            f"({used}/{effective}){suffix}{cm_label}")

            # Org-level cap (across all models for this provider)
            org = self._org_records.get(provider_id)
            self._roll_over_day_org_locked(org) if org else None
            if org_max_per_day is not None and org_max_per_day > 0:
                # Use observed org cap if tighter than declared
                org_eff = org_max_per_day
                if (org is not None
                        and org.org_observed_max_per_day
                        and org.org_observed_max_per_day < org_eff):
                    org_eff = org.org_observed_max_per_day
                org_used = org.org_calls_today if org else 0
                if org_used >= org_eff:
                    return (False,
                            f"provider-wide daily cap reached "
                            f"({org_used}/{org_eff})")

            return (True, "")

    def get(self, provider_id: str,
             canonical_model: Optional[str] = None
             ) -> Optional[ModelHealth]:
        """Fetch one ModelHealth record. canonical_model defaults to
        LEGACY for back-compat; the legacy record is also where v1
        migration parked all pre-v4.14.0 state."""
        cm = canonical_model or LEGACY_CANONICAL
        with self._lock:
            return self._model_records.get((provider_id, cm))

    def get_org(self, provider_id: str) -> Optional[ProviderOrgHealth]:
        with self._lock:
            return self._org_records.get(provider_id)

    def all(self) -> list[ModelHealth]:
        """All ModelHealth records, in insertion order. Stage 2 alone
        will mostly contain canonical_model='legacy' records (one per
        provider). Stages 3+ will start producing real
        canonical_model values as the router/api stages roll out."""
        with self._lock:
            return list(self._model_records.values())

    def all_orgs(self) -> list[ProviderOrgHealth]:
        with self._lock:
            return list(self._org_records.values())

    # ── v4.14.5.14-status-dot-meaning: session dot state accessors ──

    def get_last_call_state(self, provider_id: str) -> str:
        """Session-only last-call result for a provider: 'unknown' (no
        calls yet this session — gray dot) / 'success' / 'failed_auth' /
        'failed_transient' / 'failed_429' / 'failed_other'."""
        with self._lock:
            return self._last_call_state.get(provider_id, "unknown")

    def provider_in_cooldown(self, provider_id: str,
                             now: Optional[float] = None
                             ) -> tuple[bool, int]:
        """Is ANY model under this provider currently cooling down?
        Returns (in_cooldown, max_seconds_remaining). Used for the amber
        dot state. 'any' semantics mirror _provider_chip_is_green."""
        if now is None:
            now = time.time()
        worst = 0
        with self._lock:
            for (pid, _cm), rec in self._model_records.items():
                if pid == provider_id and rec.in_cooldown(now):
                    worst = max(worst, rec.cooldown_remaining_sec(now))
        return (worst > 0, worst)

    def recover_pathological_caps(self,
                                    presets_floor_fn=None) -> int:
        """v4.14.0 hot patch: one-shot startup cleanup.

        Earlier releases had a cap-learning bug that could pin a
        provider at a learned cap far below its real free-tier
        ceiling (e.g. Groq pinned at 23 against a documented
        14,400 RPD). This pass clears observed_max_per_day on
        records where the learned value is dramatically below the
        documented default — a safe heuristic for "this cap is
        clearly wrong, let the system re-learn from real signal."

        Args:
            presets_floor_fn: callable (provider_id, canonical_model)
                -> int | None. Returns the documented free-tier
                daily-call default for that provider (PRESETS
                default_max_per_day). If it returns None for a
                record, that record is left alone.

        Threshold: clears records whose observed_max_per_day is
        below 10 % of the documented default. A learned 23 against
        a documented 5,000 (0.46 %) is obviously broken; a learned
        800 against a documented 1,500 (53 %) is plausible (the
        provider may have downgraded the user's tier) and is left
        alone.

        Returns the number of records actually cleared.
        """
        if presets_floor_fn is None:
            return 0
        cleared = 0
        with self._lock:
            for rec in self._model_records.values():
                cur = getattr(rec, 'observed_max_per_day', None)
                if not cur or cur <= 0:
                    continue
                try:
                    documented = presets_floor_fn(
                        rec.provider_id, rec.canonical_model)
                except Exception:
                    documented = None
                if documented is None or documented <= 0:
                    continue
                if cur < int(documented * 0.10):
                    rec.observed_max_per_day = None
                    cleared += 1
        if cleared > 0:
            try:
                self.save()
            except Exception:
                pass
        return cleared

    def reset_all_observed_caps(self) -> int:
        """v4.14.5.28 (Fix 5): MANUAL user-action reset — UNCONDITIONALLY
        clear observed_max_per_day AND the auto-raised cap on every model
        record, so caps re-learn from documented defaults on the next real
        429. Distinct from `recover_pathological_caps`, which is the
        conservative threshold-gated AUTO path (left unchanged). Returns the
        number of records whose observed_max_per_day was cleared. Never
        raises."""
        cleared = 0
        try:
            with self._lock:
                for rec in self._model_records.values():
                    if getattr(rec, 'observed_max_per_day', None) is not None:
                        rec.observed_max_per_day = None
                        cleared += 1
                    # also drop any auto-raised ceiling so it re-derives
                    if getattr(rec, 'raised_cap', None) is not None:
                        rec.raised_cap = None
            if cleared > 0:
                try:
                    self.save()
                except Exception:
                    pass
        except Exception:
            pass
        return cleared

    # ── Write API ──

    def record_success(self, provider_id: str, *,
                        canonical_model: Optional[str] = None,
                        declared_cap: Optional[int] = None) -> None:
        """Register a successful call. Increments per-model
        calls_today AND the provider's org_calls_today. Auto-raises
        the per-model cap if calls_today exceeds the declared cap
        without ever 429ing (same heuristic as v4.13.59).

        Periodically saves to disk (debounced to once per 30 s).
        """
        cm = canonical_model or LEGACY_CANONICAL
        now = time.time()
        with self._lock:
            rec = self._ensure_model_locked(provider_id, cm)
            self._roll_over_day_locked(rec)
            rec.calls_today += 1
            rec.consecutive_429s = 0
            # v4.14.5.14-status-dot-meaning: stamp the session dot state.
            self._last_call_state[provider_id] = "success"

            # Auto-raise per-model cap (same logic as v4.13.59)
            if (declared_cap is not None and declared_cap > 0
                    and rec.observed_max_per_day is None
                    and rec.calls_today > declared_cap):
                new_cap = int(rec.calls_today * 1.20)
                if rec.raised_cap is None or new_cap > rec.raised_cap:
                    rec.raised_cap = new_cap

            # Org rollup
            org = self._ensure_org_locked(provider_id)
            self._roll_over_day_org_locked(org)
            org.org_calls_today += 1

        # Debounced save outside the lock.
        try:
            if now - self._last_persist_at >= _SAVE_DEBOUNCE_SEC:
                self._last_persist_at = now
                self.save()
        except Exception:
            pass

    def record_rate_limit(self, provider_id: str, *,
                            canonical_model: Optional[str] = None,
                            cooldown_sec: Optional[int] = None,
                            cooldown_type: Optional[str] = None) -> None:
        """Register a 429 / rate-limit response.

        Sets a cooldown on the (provider, model) record, escalating
        on repeats. ALSO increments the org_calls_today (the call
        did go to the provider; it just got rejected). Resets the
        per-model raised_cap to None — the real ceiling has been
        found, no need for the heuristic raise anymore.

        v4.14.5.71-per-minute-cooldown-cap: `cooldown_type` is the
        classifier verdict ('per_minute' / 'daily' / 'unknown' / None).
        When 'per_minute' is supplied (or `cooldown_sec` itself is
        small enough to imply per-minute), the cooldown is HARD-
        CAPPED at PER_MINUTE_COOLDOWN_CAP_SEC and the LONG_COOLDOWN
        escalation is SKIPPED — a transient burst-speed hit must not
        be punished like a daily-cap exhaustion, no matter how many
        consecutive strikes. Daily/unknown keep the existing
        300 / 300 / 3600 step curve so a genuine daily wall still
        benches the provider for the rest of the day.
        """
        cm = canonical_model or LEGACY_CANONICAL
        now = time.time()
        # v4.14.5.71-per-minute-cooldown-cap: a small explicit
        # cooldown_sec (<= PER_MINUTE_COOLDOWN_CAP_SEC) from a
        # classifier-aware caller is a strong signal this was a
        # per-minute hit even if the caller didn't pass the type.
        _is_per_minute = (
            cooldown_type == 'per_minute'
            or (cooldown_type is None
                and cooldown_sec is not None
                and cooldown_sec <= PER_MINUTE_COOLDOWN_CAP_SEC))
        with self._lock:
            rec = self._ensure_model_locked(provider_id, cm)
            self._roll_over_day_locked(rec)
            rec.calls_today += 1
            rec.fails_today += 1
            rec.last_429_epoch = now
            rec.consecutive_429s += 1
            rec.last_error = "429 rate-limited"
            rec.last_error_epoch = now
            rec.raised_cap = None
            # v4.14.5.14-status-dot-meaning: stamp the session dot state.
            # (The amber "cooling down" dot is derived from the cooldown
            # set just below; this records the underlying reason so the
            # dot goes red once the cooldown clears if still quota-blocked.)
            self._last_call_state[provider_id] = "failed_429"
            if _is_per_minute:
                # Per-minute is transient by definition. Honor an
                # explicit Retry-After (cooldown_sec) when given;
                # otherwise apply the cap. NEVER escalate to LONG.
                if cooldown_sec is None:
                    cooldown_sec = PER_MINUTE_COOLDOWN_CAP_SEC
                else:
                    cooldown_sec = min(
                        int(cooldown_sec),
                        PER_MINUTE_COOLDOWN_CAP_SEC)
            elif cooldown_sec is None:
                if rec.consecutive_429s >= 3:
                    cooldown_sec = LONG_COOLDOWN_SEC
                else:
                    cooldown_sec = DEFAULT_COOLDOWN_SEC
            rec.cooldown_until_epoch = max(rec.cooldown_until_epoch,
                                             now + cooldown_sec)

            # Org rollup — the call counts even though it failed
            org = self._ensure_org_locked(provider_id)
            self._roll_over_day_org_locked(org)
            org.org_calls_today += 1

        # v4.14.3.14 (2026-05-15): persist consecutive_429s +
        # cooldown_until_epoch debounced like record_success does. Pre-
        # v4.14.3.14 this method was the only counter-mutating method
        # that didn't save — the counter lived in memory only between
        # 429s and reset to 0 on disk every app restart. That trapped
        # chronic-429 providers in 5-min cooldown loops forever despite
        # the >=3-strike LONG_COOLDOWN_SEC (1-hour) escalation at line
        # 540 above being correct. STATUS.md's framing missed that the
        # in-session logic was fine; the bug was persistence. Debounce
        # window matches _SAVE_DEBOUNCE_SEC (30s) so a burst of 429s
        # gets ONE save at the start, not one per call.
        try:
            if now - self._last_persist_at >= _SAVE_DEBOUNCE_SEC:
                self._last_persist_at = now
                self.save()
        except Exception as e:
            try:
                print(
                    f"[tm_provider_health] record_rate_limit save "
                    f"failed: {type(e).__name__}: {e}")
            except Exception:
                pass

    def record_failure(self, provider_id: str, error: str = "", *,
                        canonical_model: Optional[str] = None) -> None:
        """Register a non-429 error. Does NOT trigger a cooldown —
        only 429s do that. Other errors (timeouts, 500s) we just log.
        Still counts against the org rollup."""
        cm = canonical_model or LEGACY_CANONICAL
        now = time.time()
        with self._lock:
            rec = self._ensure_model_locked(provider_id, cm)
            self._roll_over_day_locked(rec)
            rec.calls_today += 1
            rec.fails_today += 1
            rec.last_error = (error or "")[:200]
            rec.last_error_epoch = now
            # v4.14.5.14-status-dot-meaning: stamp the session dot state,
            # classifying the error (auth / transient / other) for the dot
            # tooltip — this is the SambaNova-401 case that used to stay
            # green: a 401 now lands as 'failed_auth' -> red dot.
            self._last_call_state[provider_id] = _classify_failure(error)
            # v4.14.5.14-classify429-part-c (IDEAS Fix 2): a non-429 error
            # (timeout, 5xx, auth) breaks the consecutive-429 streak — it
            # says nothing about quota, so it must not count toward the
            # 3-consecutive cap-tightening gate. Reset like record_success.
            rec.consecutive_429s = 0

            # Org rollup
            org = self._ensure_org_locked(provider_id)
            self._roll_over_day_org_locked(org)
            org.org_calls_today += 1

    def clear_cooldown(self, provider_id: str,
                         canonical_model: Optional[str] = None) -> None:
        """Manually clear a cooldown — used by the UI when the user
        resets a provider. canonical_model=None clears EVERY
        ModelHealth record for that provider, which is what v4.13.x
        UI behavior implicitly did (one provider == one cooldown).
        Pass a specific canonical_model to clear just one entry."""
        with self._lock:
            if canonical_model is None:
                # Clear every model record under this provider_id
                for (pid, cm), rec in self._model_records.items():
                    if pid == provider_id:
                        rec.cooldown_until_epoch = 0.0
                        rec.consecutive_429s = 0
            else:
                rec = self._model_records.get(
                    (provider_id, canonical_model))
                if rec:
                    rec.cooldown_until_epoch = 0.0
                    rec.consecutive_429s = 0

    # ── Helpers ──

    def _ensure_model_locked(self, provider_id: str,
                               canonical_model: str) -> ModelHealth:
        key = (provider_id, canonical_model)
        rec = self._model_records.get(key)
        if rec is None:
            rec = ModelHealth(provider_id=provider_id,
                                canonical_model=canonical_model)
            self._model_records[key] = rec
        return rec

    def _ensure_org_locked(self,
                             provider_id: str) -> ProviderOrgHealth:
        rec = self._org_records.get(provider_id)
        if rec is None:
            rec = ProviderOrgHealth(provider_id=provider_id)
            self._org_records[provider_id] = rec
        return rec

    def _roll_over_day_locked(self,
                                rec: Optional[ModelHealth]) -> None:
        if rec is None:
            return
        today = datetime.now().strftime('%Y-%m-%d')
        if rec.today_iso != today:
            rec.calls_today = 0
            rec.fails_today = 0
            rec.today_iso = today
            # v4.14.0 hot patch: learned caps are a per-day signal,
            # not permanent state. Clear at midnight so a single bad
            # day (a transient 429 storm, a Cloudflare edge block,
            # an expired key) doesn't permanently encode a low cap.
            # If the provider really has a low daily limit, the next
            # day's calls will re-learn it; if it was a transient
            # blip, today's call budget is unconstrained.
            rec.observed_max_per_day = None

    def _roll_over_day_org_locked(self,
                                    rec: Optional[ProviderOrgHealth]
                                    ) -> None:
        if rec is None:
            return
        today = datetime.now().strftime('%Y-%m-%d')
        if rec.today_iso != today:
            rec.org_calls_today = 0
            rec.today_iso = today


# ─── Singleton ─────────────────────────────────────────────────────────

_state: Optional[HealthState] = None
_init_lock = threading.Lock()


def init(json_path: Optional[Path] = None) -> HealthState:
    """Get-or-create the singleton. Idempotent. Safe to call
    repeatedly. Triggers v1→v2 migration on first load if the
    on-disk file is still the old shape."""
    global _state
    with _init_lock:
        if _state is None:
            _state = HealthState(json_path=json_path)
            _state.load()
    return _state


def get_state() -> Optional[HealthState]:
    return _state
