"""
tm_data_router.py — Data Router (v4.13.55)

What this is:
    The brain that decides: "I need news for AAPL — which provider
    should I call?" It picks based on:
        1. Current data mode (api_only / free_only / hybrid)
        2. Provider priorities for the requested data type
        3. Provider health (skip red ones)
        4. Provider quota (skip exhausted ones)
        5. Provider has-key (skip unconfigured keyed sources)

    If the chosen provider fails, the router tries the next one in
    priority order automatically. The caller never sees the fallback —
    it just gets a result or a final "all sources failed" error.

What it does NOT do:
    - Make HTTP calls itself (adapters do that)
    - Know the response format (adapters normalize)
    - Persist anything (registry does that)

Usage:
    router.fetch('news', ticker='AAPL')
        -> {result: {...}, source: 'finnhub'} or None
    router.fetch('filings', ticker='AAPL', form_type='8-K')
        -> {result: {...}, source: 'edgar'} or None

Mode taxonomy (the same three modes as Inference Mode):
    api_only  — only providers with valid keys are eligible
    free_only — only providers that need NO key are eligible
    hybrid    — all eligible providers, sorted by priority

Adapter contract (see _ADAPTERS dict at bottom):
    Each adapter is a callable: fn(profile, data_type, **kwargs) -> dict|None
    On success: returns the data (any shape — caller knows what to expect)
    On rate-limit failure: raise RateLimitError
    On other failure: raise any other Exception, or return None

The router catches both. On RateLimitError it specifically tells the
registry it was rate-limited (so observed-limit learning kicks in).
On other errors it just records a generic failure.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from tm_data_providers import (
    Registry, ProviderProfile, DATA_TYPES,
    get_registry,
)


# ─── Public exception types ───────────────────────────────────────────

class RateLimitError(Exception):
    """Adapters raise this when they detect a 429 / quota-exceeded
    response. The router uses this to tell the registry to learn an
    observed limit."""
    pass


class AllSourcesFailedError(Exception):
    """Raised when no eligible source can serve the request (all of
    them errored, exhausted quota, or were filtered out by mode)."""
    pass


# v4.14.5.13: a sentinel an adapter can return INSTEAD of None to mean
# "I definitively tried and this ticker has no data here — this is NOT a
# transient failure, do not log it as 'All sources failed'." Distinct
# from None (which means "empty/transient, retry-able"). The EDGAR
# adapter returns this for dual-class/delisted tickers that miss the SEC
# CIK map even after variant fallback, so the router stops spamming the
# red "All sources failed for filings" line every pass for the same
# known-unresolvable tickers (the adapter already logs each once).
TICKER_UNRESOLVABLE = object()


# ─── Mode resolution ──────────────────────────────────────────────────

VALID_MODES = ('api_only', 'free_only', 'hybrid')
DEFAULT_MODE = 'hybrid'


def _filter_by_mode(profiles: list[ProviderProfile], mode: str
                     ) -> list[ProviderProfile]:
    """Drop providers that don't fit the current mode."""
    if mode == 'free_only':
        return [p for p in profiles if not p.needs_key]
    if mode == 'api_only':
        return [p for p in profiles if p.needs_key]
    # hybrid: everyone passes through
    return list(profiles)


# ─── Eligibility ──────────────────────────────────────────────────────

def _is_eligible(profile: ProviderProfile, registry: Registry,
                  data_type: Optional[str] = None) -> bool:
    """True if this profile is currently usable for a real call.
    Checks: enabled, has-key-if-needed, not in cooldown, not over a declared
    daily cap.

    v4.14.5.14-classify429-data-side: eligibility is gated on a TIME-BASED
    cooldown (which auto-clears — set by record_failure via classify_429), NOT
    on health=='red'. The old health=='red' block was the stuck-red bug: red
    only cleared on a success the provider was never allowed to attempt, so a
    few transient failures sidelined a provider for the whole session. `health`
    is now display-only; a provider whose cooldown has expired is eligible
    again (a 'trial' call) and self-recovers on success.

    v4.14.6.56-per-type-provider-health: the cooldown check is now keyed by
    (provider, data_type), so a Yahoo fundamentals 429 no longer blocks Yahoo
    prices. The account-wide daily cap (effective_limit) stays provider-scoped
    — enforced ALONGSIDE the per-type cooldown. data_type=None falls back to
    provider-scope cooldown (safe-side default)."""
    if not profile.is_usable():
        return False
    try:
        if registry.in_cooldown(profile.id, data_type):
            return False
    except Exception:
        pass
    # Legitimate DECLARED daily cap still enforced (e.g. GitHub Models ~50/day).
    # v4.14.6.56: this is the account-wide cap — stays SHARED across data
    # types, never split. Per-endpoint cooldowns are handled above by the
    # data_type-keyed in_cooldown check.
    eff_day = registry.effective_limit(profile.id, 'calls_per_day')
    if eff_day > 0 and profile.calls_today >= eff_day:
        return False
    return True


# ─── The router ───────────────────────────────────────────────────────

class Router:
    """Picks providers and dispatches requests. One instance per app.

    Adapters register themselves at module load time via
    register_adapter(). The router doesn't know which adapters exist
    until it's asked to dispatch."""

    def __init__(self, registry: Registry,
                 mode_provider: Callable[[], str] | None = None,
                 log_fn: Callable[[str, str], None] | None = None):
        """
        Args:
            registry:      The data provider registry (already loaded)
            mode_provider: Callable returning current mode string. If
                           None, defaults to 'hybrid' always. The main
                           app wires this to read cfg['data_mode'].
            log_fn:        Optional logging callback (msg, color).
        """
        self._registry = registry
        self._get_mode = mode_provider or (lambda: DEFAULT_MODE)
        self._log = log_fn
        self._lock = threading.Lock()
        # adapter_id -> callable. Adapters register at startup.
        self._adapters: dict[str, Callable] = {}

    # ── Adapter registration (called by adapter modules at import) ──

    def register_adapter(self, provider_id: str,
                          adapter_fn: Callable) -> None:
        """Adapter modules call this to register themselves.

        The function signature must be:
            adapter_fn(profile, data_type, **kwargs) -> result | None

        Where result is whatever shape makes sense for the data_type.
        The router doesn't inspect it.
        """
        with self._lock:
            self._adapters[provider_id] = adapter_fn

    def has_adapter(self, provider_id: str) -> bool:
        with self._lock:
            return provider_id in self._adapters

    # ── Public dispatch API ─────────────────────────────────────────

    def fetch(self, data_type: str, return_status: bool = False, **kwargs):
        """Try to fetch `data_type` data using any available source.

        Returns:
            By default: a dict with 'result'/'source' keys, or None if no
            source yielded. The exact 'result' shape depends on data_type.

            v4.14.5.14-earnings-architecture-fix: with return_status=True,
            returns a (payload, status) tuple exposing the distinction the
            router already tracks internally — status ∈
              'ok'        : a source returned data (payload = result dict)
              'empty'     : sources reached, none had data (payload None) —
                            i.e. a genuine "no data for this item", NOT a fault
              'failed'    : a source ERRORED (rate-limit / network / 5xx);
                            payload None — caller should treat as "temporarily
                            unavailable", not "no data"
              'no_source' : no eligible/usable source for this data_type
            Callers (e.g. earnings prompt honesty) use this to tell "no
            earnings event" apart from "data source unavailable". Default
            (False) is byte-for-byte the old behavior.

        Raises:
            ValueError if data_type is not recognized.

        Does NOT raise on adapter failure — falls back to the next
        source. Only raises if NO source could even be tried.
        """
        if data_type not in DATA_TYPES:
            raise ValueError(
                f"Unknown data_type: {data_type!r}. "
                f"Valid: {DATA_TYPES}")

        def _ret(payload, status):
            return (payload, status) if return_status else payload

        mode = self._get_mode_safe()
        candidates = self._candidates(data_type, mode)

        if not candidates:
            self._note(
                f"No eligible source for {data_type} (mode={mode})",
                'amber')
            return _ret(None, 'no_source')

        last_error: Optional[str] = None
        # v4.14.5.5: capture WHY each source didn't yield, so the
        # "All sources failed" message is actionable even when no
        # exception was raised (the prior "last: None" hid the real
        # cause — typically no adapter registered / no API key, or
        # adapters returning cleanly empty).
        skip_reasons: list = []
        # v4.14.5.13: set when an adapter signals TICKER_UNRESOLVABLE
        # (definitive "no data here, not transient"). If the ONLY reason
        # every source failed is definitive-unresolvable, suppress the
        # red "All sources failed" line — the adapter already logged the
        # ticker once; repeating it every pass is the filings spam.
        definitive_unresolvable = False
        for profile in candidates:
            adapter = self._get_adapter(profile.id)
            if adapter is None:
                # No adapter wired up for this provider (often: no API
                # key configured for it). Record so it's visible.
                skip_reasons.append(f"{profile.id}: no adapter/key")
                continue

            try:
                result = adapter(profile, data_type, **kwargs)
            except RateLimitError as e:
                cd = self._registry.record_failure(
                    profile.id, error=f"rate_limit: {e}",
                    is_rate_limit=True, data_type=data_type)
                self._log_cooldown(profile, cd, data_type)
                last_error = f"{profile.id}: rate_limit"
                continue
            except Exception as e:
                cd = self._registry.record_failure(
                    profile.id, error=f"{type(e).__name__}: {e}",
                    data_type=data_type)
                self._log_cooldown(profile, cd, data_type)
                last_error = f"{profile.id}: {type(e).__name__}"
                continue

            if result is TICKER_UNRESOLVABLE:
                # v4.14.5.13: adapter confirmed this ticker has no data
                # here and it is NOT transient (e.g. EDGAR dual-class
                # miss after variant fallback). Treat like "no data" for
                # fallback purposes but don't add noise to skip_reasons.
                definitive_unresolvable = True
                continue

            if result is None:
                # Adapter returned cleanly but had no data — not a
                # failure, just an empty result. Try next source.
                skip_reasons.append(f"{profile.id}: returned no data")
                continue

            # Success!
            self._registry.record_success(profile.id, data_type)
            return _ret({
                'result': result,
                'source': profile.id,
                'source_display': profile.display_name,
            }, 'ok')

        # All sources exhausted. v4.14.5.5: report the actual reason —
        # an exception (last_error) if one occurred, otherwise the
        # per-source skip reasons (no adapter/key, returned no data) so
        # the failure is diagnosable instead of a bare "last: None".
        if (definitive_unresolvable and not last_error
                and not skip_reasons):
            # v4.14.5.13: every source's only "failure" was a definitive
            # "this ticker has no data here" (e.g. a dual-class/delisted
            # ticker that isn't an EDGAR filer). The adapter already
            # logged it once this session. Returning None silently here
            # is correct and stops the per-pass red-line spam.
            return _ret(None, 'empty')
        if last_error:
            detail = f"last error: {last_error}"
            status = 'failed'           # a source raised — infra fault
        elif skip_reasons:
            detail = "; ".join(skip_reasons)
            status = 'empty'            # reached, no data — not a fault
        else:
            detail = "no eligible sources had a usable adapter"
            status = 'no_source'
        # v4.14.5.14-cascade-fixes (Fix 4 / log honesty): reserve the red
        # "All sources failed" alarm for ACTUAL faults (a source raised →
        # status 'failed'). An 'empty' result means every source was reached
        # and honestly had no data for this item (e.g. a ticker with no
        # upcoming earnings) — that is NOT a failure, so log it quietly in
        # 'muted'. This stops honest no-data conditions from crying wolf in
        # red. Status semantics + return value are unchanged.
        _tick = kwargs.get('ticker')
        _suffix = f" for {_tick}" if _tick else ""
        if status == 'failed':
            self._note(
                f"All sources failed for {data_type}{_suffix} ({detail})",
                'red')
        else:
            self._note(
                f"No {data_type} data{_suffix} from any source ({detail})",
                # v4.14.5.25-activity-log-tiering: 'routine' so App._log
                # collapses the per-ticker "No earnings/… data for X" wall
                # (a non-fault honest no-data) to ~one line per window under
                # normal verbosity. The 'failed' (red) branch above stays a
                # SIGNAL line. Routing is transparent (_note → app._log(msg,
                # tag)); presentation only, no behaviour change.
                'routine')
        return _ret(None, status)

    def fetch_or_raise(self, data_type: str, **kwargs) -> dict:
        """Like fetch() but raises AllSourcesFailedError on None."""
        out = self.fetch(data_type, **kwargs)
        if out is None:
            raise AllSourcesFailedError(
                f"No source could serve {data_type}")
        return out

    # ── Diagnostic API ──────────────────────────────────────────────

    def routing_for(self, data_type: str) -> list[dict]:
        """Returns the ordered list of providers the router would TRY
        for this data type, given current mode. UI uses this to show
        users where their data comes from."""
        mode = self._get_mode_safe()
        cands = self._candidates(data_type, mode)
        return [
            {
                'id': p.id,
                'display_name': p.display_name,
                'priority': p.priority_for(data_type),
                'health': p.health,
                'has_adapter': self.has_adapter(p.id),
                'enabled': p.enabled,
                'usable': p.is_usable(),
            }
            for p in cands
        ]

    def stats(self) -> dict:
        """High-level snapshot of router state."""
        with self._lock:
            adapter_count = len(self._adapters)
            adapter_ids = list(self._adapters.keys())
        return {
            'mode': self._get_mode_safe(),
            'registered_adapters': adapter_count,
            'adapter_ids': adapter_ids,
        }

    # ── Internals ───────────────────────────────────────────────────

    def _candidates(self, data_type: str, mode: str
                     ) -> list[ProviderProfile]:
        """Return the in-priority-order list of providers we'd consider
        for this data type, after mode and eligibility filtering.

        v4.14.6.60-source-reliability: within each priority tier, the
        list is re-ordered by reliability_score DESCENDING (cooldown +
        failure_rate signals). The reordering NEVER crosses priority
        tiers — a declared priority-1 source always outranks
        priority-2 regardless of score (authoritative guardrail).

        Gated by module flag _RELIABILITY_SORT_ENABLED (default True,
        flip via set_reliability_sort_enabled(False) at app startup
        from cfg['use_data_source_reliability']). When False, behavior
        is byte-identical to v59's static sort.
        """
        all_serving = self._registry.serving(data_type)
        mode_filtered = _filter_by_mode(all_serving, mode)
        eligible = [p for p in mode_filtered
                     if _is_eligible(p, self._registry, data_type)]

        if not _RELIABILITY_SORT_ENABLED or len(eligible) < 2:
            return eligible

        # Group by priority tier and sort within each tier by
        # reliability_score DESC. Stable order preserved across tiers.
        try:
            from collections import OrderedDict
            tiers: 'OrderedDict[int, list[ProviderProfile]]' = OrderedDict()
            for p in eligible:
                pr = p.priority_for(data_type)
                tier_key = int(pr) if pr is not None else 999
                tiers.setdefault(tier_key, []).append(p)

            reordered: list[ProviderProfile] = []
            for tier_key, group in tiers.items():
                if len(group) < 2:
                    reordered.extend(group)
                    continue
                # Score each provider in this tier; sort DESC by score.
                # Stable: ties preserve original (priority-asc) order.
                scored = [
                    (self._registry.reliability_score(p.id, data_type), p)
                    for p in group]
                scored.sort(key=lambda x: -x[0])
                new_group = [p for _s, p in scored]
                # Log only when the TOP of the tier actually changes —
                # i.e. reliability picked a different first-call source
                # than the static priority sort would have. Tail-of-tier
                # shuffles don't matter (only matter as fallback after
                # the primary fails) and would just be log noise.
                try:
                    static_top = group[0]
                    new_top, new_top_score = scored[0][1], scored[0][0]
                    if static_top.id != new_top.id:
                        # Find static top's reliability for context.
                        static_top_score = next(
                            (s for s, p in scored
                             if p.id == static_top.id), 1.0)
                        self._note(
                            f"[source-reliability] {new_top.id}/"
                            f"{data_type} promoted above "
                            f"{static_top.id} within priority tier "
                            f"{tier_key} (score "
                            f"{new_top_score:.2f} vs "
                            f"{static_top_score:.2f})", 'muted')
                except Exception:
                    pass
                reordered.extend(new_group)
            return reordered
        except Exception:
            # Any sort failure → fall back to today's static order.
            return eligible

    def _get_mode_safe(self) -> str:
        try:
            mode = self._get_mode()
        except Exception:
            mode = DEFAULT_MODE
        if mode not in VALID_MODES:
            mode = DEFAULT_MODE
        return mode

    def _get_adapter(self, provider_id: str) -> Callable | None:
        with self._lock:
            return self._adapters.get(provider_id)

    def _note(self, msg: str, color: str = 'muted') -> None:
        if self._log is None:
            return
        try:
            self._log(msg, color)
        except Exception:
            pass

    def _log_cooldown(self, profile, cd: dict,
                       data_type: Optional[str] = None) -> None:
        """v4.14.5.14-classify429-data-side: announce a cooldown applied by
        record_failure (amber), so a transient rate-limit is visible and
        honest ('retry at HH:MM:SS') instead of a silent demotion. No-op when
        no cooldown was set (the 1-2 transient-blip tolerance).

        v4.14.6.56-per-type-provider-health: log line names the affected
        data_type so it's visible that only that endpoint is cooling down
        (e.g. 'yahoo/fundamentals' — yahoo/prices unaffected)."""
        try:
            if not cd or not cd.get('cooldown'):
                return
            import time as _t
            secs = int(cd.get('seconds', 0))
            kind = cd.get('kind', 'rate-limit')
            retry_at = _t.strftime('%H:%M:%S', _t.localtime(cd.get('until', 0)))
            name = getattr(profile, 'display_name', None) or getattr(
                profile, 'id', '?')
            dt = data_type or cd.get('data_type')
            if dt:
                name = f"{name}/{dt}"
            if secs >= 3600:
                human = f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
            elif secs >= 60:
                human = f"{secs // 60}m{secs % 60:02d}s"
            else:
                human = f"{secs}s"
            self._note(
                f"{name} {kind} — cooldown {human}, retry at {retry_at}",
                'amber')
        except Exception:
            pass


# ─── Module-level singleton ───────────────────────────────────────────

_router: Router | None = None
_init_lock = threading.Lock()

# v4.14.6.60-source-reliability: feature gate. Default ON. The main app
# flips this at startup from cfg['use_data_source_reliability']. When
# False, _candidates() returns the static-priority ordering (byte-
# identical to v59 behavior).
_RELIABILITY_SORT_ENABLED: bool = True


def set_reliability_sort_enabled(enabled: bool) -> None:
    """Toggle the within-tier reliability sort. Called at app startup.
    Idempotent; safe to call multiple times."""
    global _RELIABILITY_SORT_ENABLED
    _RELIABILITY_SORT_ENABLED = bool(enabled)


def init(registry: Registry,
         mode_provider: Callable[[], str] | None = None,
         log_fn: Callable[[str, str], None] | None = None) -> Router:
    """Create the singleton router. Idempotent."""
    global _router
    with _init_lock:
        if _router is None:
            _router = Router(
                registry=registry,
                mode_provider=mode_provider,
                log_fn=log_fn,
            )
    return _router


def get_router() -> Router | None:
    return _router


def fetch(data_type: str, **kwargs) -> dict | None:
    """Convenience wrapper."""
    r = _router
    if r is None:
        return None
    return r.fetch(data_type, **kwargs)
