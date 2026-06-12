"""
tm_rate_limiter — v4.13.44 per-provider rate limiter for API calls.

Purpose: stop free-tier API providers from being hammered above their
allowed RPM (requests per minute) and RPD (per day) limits during heavy
scans. Without this, a Scan All on the universe with 2 cloud providers
enabled would fire ~400 calls in seconds, get rate-limited, and waste
calls on 429 errors.

Design:
- One ProviderRateLimiter per provider id (singleton via _get_limiter)
- In-memory deque of recent request timestamps for RPM enforcement
- Persistent daily counter (data/api_provider_usage.json) for RPD
- Counter resets at midnight LOCAL time (close enough to provider
  quota windows to be useful in practice)
- acquire(provider) blocks until a slot opens (RPM) or raises
  QuotaExhausted if the daily limit is hit
- Caller is expected to handle QuotaExhausted as a soft failure
  (skip this provider, continue with others)

Defaults per preset (calibrated below the real free-tier caps to
leave headroom for retries / clock skew):
    groq:        25 RPM, no daily limit
    google:      12 RPM, 1400 RPD  (gemini free tier)
    anthropic:   30 RPM, no daily  (paid tier assumption)
    mistral:     50 RPM, no daily
    openai:      30 RPM, no daily
    openrouter:  20 RPM, 50 RPD
    together:    50 RPM, no daily
    ollama:      no limits (local)
    custom:      30 RPM, no daily

User can override in the API Provider config via rate_limit_rpm and
rate_limit_rpd fields.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime, date
from pathlib import Path


# Default rate limits per preset. Values are conservative — set below
# the real free-tier caps so a single burst doesn't trip the provider.
# v4.13.44a: Google numbers reflect Gemini 2.5 Flash-Lite free tier
# (15 RPM / 1000 RPD as of early 2026). Older guides showing higher
# Gemini limits were for the now-deprecated 1.5 and 2.0 model families.
# v4.14.3.8 (2026-05-14): added tpm (tokens-per-minute) and
# burst_category fields. Both load-bearing for the burst-tolerant
# queue runner foundation:
#   - tpm: many providers throttle on tokens, not requests. Groq's
#     Llama 3.1 8B free tier is ~6,000 TPM — at ~3,000 tokens per
#     prompt, 2 calls/min ceiling. RPM tracking wouldn't catch that.
#     None = no enforced TPM cap (generous tier, no documentation,
#     or local).
#   - burst_category: 'tight' / 'moderate' / 'generous'. The
#     cold-start strategy in tm_top_ai_picker.py uses this to pick
#     an operationally-fit provider when no accuracy data exists
#     yet. 'generous' > 'moderate' > 'tight' for queue runner
#     burst patterns.
# v4.14.5.14a.4: RPM tuned to researched 2026-05-17 real free-tier
# limits, kept conservatively UNDER the real ceiling so a burst
# doesn't trip a 429 in the first place (proactive spacing). 429s are
# now cheap anyway (per-minute ones get a short Retry-After cooldown,
# not 5 min — see tm_ai_router._classify_and_record_quota), so erring
# slightly slow is the safe trade. RPD here is mostly None: the daily
# ceiling is enforced by the router cap layer + the dynamic learner
# (tm_provider_learning), not duplicated here.
PRESET_DEFAULTS = {
    # v4.14.5.55: groq TPM cap pulled to 5000 (real free-tier ceiling is 6000).
    # The limiter estimates INPUT tokens only (len/4 in call_provider), but the
    # real 6000 TPM counts input+output — so an input-only estimate that just
    # fits 6000 actually overruns once the response tokens land, which is how
    # the steady-state 429s happen. ~17% headroom (5000) absorbs typical scan-
    # verdict output so Groq drips steadily under the cap instead of bursting
    # into the wall, especially now that Speculative adds a 4th fill path on the
    # same key. Conservative starting value — the user's single-provider tests
    # refine it (raise toward 5500 if 429s stay rare; lower if they don't).
    'groq':       {'rpm': 25, 'rpd': None,  # real 30 RPM / 14,400 RPD
                   'tpm': 5000, 'burst_category': 'tight'},
    'google':     {'rpm': 12, 'rpd': 1400,  # real 15 RPM / 1500 RPD
                   'tpm': 32000, 'burst_category': 'moderate'},
    'anthropic':  {'rpm': 30, 'rpd': None,
                   'tpm': 40000, 'burst_category': 'generous'},
    'mistral':    {'rpm': 30, 'rpd': None,  # real ~1 RPS; 50 was too
                   #            bursty (constant 429s). 30≈2s spacing.
                   'tpm': None, 'burst_category': 'moderate'},
    'openai':     {'rpm': 30, 'rpd': None,
                   'tpm': 90000, 'burst_category': 'generous'},
    'openrouter': {'rpm': 20, 'rpd': 50,
                   'tpm': None, 'burst_category': 'tight'},
    'together':   {'rpm': 50, 'rpd': None,
                   'tpm': 90000, 'burst_category': 'generous'},
    'ollama':     {'rpm': None, 'rpd': None,
                   'tpm': None, 'burst_category': 'generous'},
    # 'custom' covers the user's Cerebras (30 RPM real) AND GitHub (10 RPM
    # real, but daily-capped at 40 so RPM rarely binds). 25 is safe
    # for Cerebras; a GitHub per-minute 429 now costs a short retry,
    # not 5 min. Per-endpoint custom RPM keying is a flagged follow-up.
    'custom':     {'rpm': 25, 'rpd': None,
                   'tpm': 6000, 'burst_category': 'moderate'},
    # v4.14.5.77-provider-rate-config: Zhipu (Z.AI) was previously
    # falling through to the generic 'custom' default of 25 RPM, but
    # Zhipu's free GLM-flash tier is much tighter — documented at ~5
    # RPM with effectively 1 concurrent request. Under concurrent
    # consensus dispatch that produced timeouts + 503s + a retry storm
    # that held a consensus slot for minutes. 5 RPM matches the real
    # ceiling so the pre-dispatch gate now holds Zhipu back before it
    # hits the wall. TPM kept at 6000 (custom default) — Zhipu publishes
    # no TPM ceiling and 6000 is the same conservative budget we use
    # for similarly-shaped tiers. RPD None — Zhipu's daily isn't the
    # binding constraint; RPM is. Paired with `timeout_seconds=45` on
    # the Zhipu provider entry in api_providers.json so a hung call
    # fails over fast.
    'zhipu':      {'rpm': 5, 'rpd': None,
                   'tpm': 6000, 'burst_category': 'tight'},
}


# ─── v4.14.5.78-per-model-rate-gate: per-MODEL rate ceilings ─────────
#
# The v4.14.5.77 investigation pinned the bug: ONE provider id can host
# several models with sharply different free-tier ceilings (Google
# hosts gemini-2.5-pro ~5 RPM AND gemini-2.5-flash ~10 RPM under one
# provider entry; the v4.14.5.62 reserve-under-lock fix correctly held
# the shared 12-RPM bucket, but a Pro call could still 429 because Pro
# alone is over its own ceiling while the shared bucket reports
# headroom). v4.14.5.77 worked around this by removing Pro from
# rotation; v4.14.5.78 keys the limiter on (provider_id, model) so the
# workaround is no longer load-bearing.
#
# Resolution order for a (provider_id, model) bucket's rpm/rpd/tpm:
#   1. per-model override from provider['model_limits'][model] (config)
#   2. MODEL_DEFAULTS[model] (this table, documented divergent cases)
#   3. provider['rate_limit_rpm']/'rpd'/'tpm' (existing provider override)
#   4. PRESET_DEFAULTS[preset]                     (today's behavior)
#
# A model NOT listed in MODEL_DEFAULTS resolves through #3+#4 EXACTLY
# the way it does in v4.14.5.77 — byte-identical for the common case.
# So adding a new provider with a single model is unaffected.
#
# Seed entries below are documented FREE-TIER figures (Google AI Studio,
# 2026 era). These will drift over time; update or override via
# provider['model_limits'] for paid tiers or vendor changes.

MODEL_DEFAULTS: dict[str, dict] = {
    # Google Gemini family — divergent within one preset:
    #   2.5-pro:        ~5 RPM / ~50 RPD on AI Studio free tier
    #   2.5-flash:      ~10 RPM (no published daily under reasonable use)
    #   2.5-flash-lite: leaves provider/preset default (12 RPM is fine)
    'gemini-2.5-pro':         {'rpm': 5,  'rpd': 50},
    'gemini-2.5-flash':       {'rpm': 10, 'rpd': None},
}


# Master toggle. False = limiter reverts to per-provider keying (v.77
# behavior); the workaround config (Pro out of rotation, zhipu preset,
# Zhipu timeout) remains in place as the safety net.
_PER_MODEL_ENABLED = True


def set_per_model_enabled(enabled: bool) -> None:
    """Master kill switch for per-(provider, model) keying. Default True.

    When True (v4.14.5.78 and later), each (provider_id, model) gets its
    own bucket with model-aware rpm/rpd/tpm via the resolution order
    above.

    When False, the limiter reverts to v4.14.5.77 per-provider keying
    (one bucket per provider_id, regardless of which model is in play).
    The v.77 config workarounds (Pro out of rotation, zhipu preset,
    Zhipu timeout=45) remain effective in this fallback mode.
    """
    global _PER_MODEL_ENABLED
    _PER_MODEL_ENABLED = bool(enabled)


def is_per_model_enabled() -> bool:
    return _PER_MODEL_ENABLED


def _normalize_model(model) -> str:
    """Normalize a model string for bucket keying. None / blank / non-str
    → empty string (per-provider bucket); otherwise lowercase + strip."""
    if not model:
        return ''
    try:
        return str(model).strip().lower()
    except Exception:
        return ''


def get_burst_category(provider: dict) -> str:
    """v4.14.3.8 (2026-05-14): return the burst_category for a
    provider dict. Honors explicit 'burst_category' override; else
    falls through to PRESET_DEFAULTS via the provider's preset; else
    'moderate' as the safest unknown-tier default."""
    override = (provider.get('burst_category') or '').strip().lower()
    if override in ('tight', 'moderate', 'generous'):
        return override
    preset = (provider.get('preset') or 'custom').lower()
    defaults = PRESET_DEFAULTS.get(preset, PRESET_DEFAULTS['custom'])
    return defaults.get('burst_category') or 'moderate'


class QuotaExhausted(Exception):
    """Raised when the daily quota has been spent. Caller should treat
    this as a soft failure -- skip the provider for the rest of the day,
    continue with others."""
    pass


class ProviderRateLimiter:
    """One instance per provider id. Tracks recent calls in memory and
    persists a daily counter to data/api_provider_usage.json."""

    def __init__(self, provider_id: str, rpm: int | None,
                 rpd: int | None, usage_path: Path | None = None,
                 tpm: int | None = None,
                 model: str = ''):
        self.provider_id = provider_id
        # v4.14.5.78-per-model-rate-gate: model component of the bucket
        # key. Empty string means "provider-level bucket" (legacy /
        # kill-switch-off behavior); a non-empty model gives this
        # bucket its own per-minute deque, daily counter, and persist
        # key — distinct from any sibling-model bucket on the same
        # provider_id.
        self.model = _normalize_model(model)
        self.rpm = rpm if rpm and rpm > 0 else None
        self.rpd = rpd if rpd and rpd > 0 else None
        # v4.14.3.8 (2026-05-14): tokens-per-minute cap. Set from
        # provider's rate_limit_tpm field if present, else
        # PRESET_DEFAULTS[preset]['tpm']. None means no TPM
        # enforcement.
        self.tpm = tpm if tpm and tpm > 0 else None
        self.usage_path = usage_path
        self._lock = threading.Lock()
        # Deque of timestamps of recent calls (newest at right)
        self._recent: deque[float] = deque()
        # v4.14.3.8: deque of (timestamp, token_count) tuples for
        # the rolling 60-second TPM window. Parallel structure to
        # _recent but tracks tokens per call instead of just
        # whether a call happened. Falls dormant when self.tpm is
        # None.
        self._recent_tokens: deque[tuple[float, int]] = deque()
        # Daily counter
        self._daily_count = 0
        self._daily_date: str = date.today().isoformat()
        self._load_daily()

    def _persist_key(self) -> str:
        """v4.14.5.78-per-model-rate-gate: the JSON key under which this
        bucket's daily counter is persisted. Composite (`pid::model`)
        when a model is set; plain `pid` when not (legacy / kill-switch
        off). Composite keys are forward-compatible alongside any
        legacy entries — both shapes coexist in the same JSON file."""
        return (f"{self.provider_id}::{self.model}"
                if self.model else self.provider_id)

    def _load_daily(self) -> None:
        """Load today's counter from disk, or reset if it's a new day.

        v4.14.5.78-per-model-rate-gate: tries the composite per-model
        key first; on miss, falls back to the legacy provider-only
        key (so an existing api_provider_usage.json with the old
        shape is read as the SEED for the first per-model bucket
        that resolves it). Subsequent saves write under the composite
        key, alongside any legacy entry — we never delete or rewrite
        legacy data.
        """
        if self.usage_path is None or not self.usage_path.exists():
            return
        try:
            with open(self.usage_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            entry = data.get(self._persist_key())
            # Legacy fallback: composite key absent but the bare
            # provider_id entry exists from a pre-v4.14.5.78 install
            # — read it as seed. Only used when the composite key is
            # actually different (avoids double-read for the
            # plain-pid keys that already match).
            if entry is None and self._persist_key() != self.provider_id:
                entry = data.get(self.provider_id)
            entry = entry or {}
            saved_date = entry.get('date')
            today = date.today().isoformat()
            if saved_date == today:
                self._daily_count = int(entry.get('count', 0))
            # If saved_date is older, leave counter at 0 (new day)
        except Exception:
            pass

    def _save_daily(self) -> None:
        """Persist today's counter for this provider.

        v4.14.5.78-per-model-rate-gate: writes under the composite
        `pid::model` key when a model is set; bare `pid` otherwise.
        Legacy bare-pid entries written by earlier versions are left
        intact (we read them as seed but never overwrite or delete).
        """
        if self.usage_path is None:
            return
        try:
            self.usage_path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if self.usage_path.exists():
                try:
                    with open(self.usage_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            data[self._persist_key()] = {
                'date': self._daily_date,
                'count': self._daily_count,
            }
            tmp = self.usage_path.with_suffix('.json.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            tmp.replace(self.usage_path)
        except Exception:
            pass

    def _check_day_rollover(self) -> None:
        """If the date has changed, reset the counter."""
        today = date.today().isoformat()
        if today != self._daily_date:
            self._daily_date = today
            self._daily_count = 0
            self._save_daily()

    def acquire(self, log_fn=None) -> float:
        """Block until an RPM slot opens, or raise QuotaExhausted if RPD is hit.

        Returns: seconds waited (for logging).

        v4.14.5.62-limiter-concurrency: the slot is RESERVED under the SAME
        lock that computes the wait (reserve-under-lock), BEFORE the lock is
        released to sleep. Previously the append happened in a SEPARATE locked
        block after the sleep, so two threads acquiring the same provider
        concurrently could both see an open slot (neither had appended yet),
        both compute the same wait, and both admit — over-admitting the cap by
        N-1. Reserving the slot (timestamped for when the call will actually
        fire) under the wait-computing lock makes the next concurrent caller
        see it and compute a later wait, so callers serialize into distinct
        slots. The RPD counter is likewise incremented under this lock (so the
        daily cap can't be over-admitted either). The sleep STAYS OUTSIDE the
        lock — holding it across the sleep would serialize every caller and
        destroy throughput. Sequential behavior is unchanged: a single caller
        sees the same window state, computes the same wait, and reserves the
        very fire-time the post-sleep append used to record.
        """
        with self._lock:
            self._check_day_rollover()
            # Daily quota check
            if self.rpd is not None and self._daily_count >= self.rpd:
                raise QuotaExhausted(
                    f"daily quota reached ({self._daily_count}/{self.rpd})")
            # RPM check — if rpm is None, no per-minute limit
            now = time.monotonic()
            wait_time = 0.0
            if self.rpm is not None:
                # Drop entries older than 60s
                cutoff = now - 60.0
                while self._recent and self._recent[0] < cutoff:
                    self._recent.popleft()
                if len(self._recent) >= self.rpm:
                    # Need to wait until the oldest entry rolls off
                    oldest = self._recent[0]
                    wait_time = max(0.0, (oldest + 60.0) - now)
            # RESERVE the slot UNDER THIS LOCK (the fix). Timestamp it for when
            # the call will actually fire (now + wait + buffer) — the same
            # instant the old post-sleep append recorded — so the window a
            # concurrent caller reads already accounts for it. Appended
            # unconditionally, matching the prior unconditional append (when
            # rpm is None the deque is never read, exactly as before).
            fire_at = now + wait_time + (0.1 if wait_time > 0 else 0.0)
            self._recent.append(fire_at)
            self._daily_count += 1
            self._save_daily()
        # v4.14.6.5-cache-ungate-tpm-skip: skip-don't-wait under
        # nonblocking_scope() — raise NonBlockingBusy so the caller
        # (scan fallback chain) can move to the next eligible provider
        # instantly instead of blocking this worker thread on RPM.
        if wait_time > 0 and nonblocking_active():
            raise NonBlockingBusy(wait_time, self.provider_id)
        if wait_time > 0 and log_fn:
            try:
                log_fn(
                    f"Rate-limit: {self.provider_id} waiting {wait_time:.1f}s "
                    f"({self.rpm} RPM cap)",
                    'muted')
            except Exception:
                pass
        if wait_time > 0:
            time.sleep(wait_time + 0.1)  # small buffer
        return wait_time

    def acquire_with_tokens(self, estimated_tokens: int,
                              log_fn=None) -> float:
        """v4.14.3.8 (2026-05-14): same as acquire() but also enforces
        the TPM (tokens-per-minute) ceiling against `estimated_tokens`.

        When TPM is None (preset has no TPM cap or user hasn't set one),
        the TPM check is a no-op and the call behaves identically to
        acquire().

        Behavior when TPM is set:
        - Drop entries from the token-time deque older than 60s.
        - Sum remaining token counts.
        - If `sum + estimated_tokens > tpm`, sleep until enough oldest
          tokens roll off to fit the new call.
        - Reserve (fire_time, estimated_tokens) in the deque UNDER the
          wait-computing lock (before sleeping) so concurrent same-provider
          callers serialize instead of over-admitting the TPM window.
        - Then fall through to the existing acquire() RPM/RPD logic.

        Edge case: if estimated_tokens > tpm (a single call that
        physically can't fit), raise QuotaExhausted immediately. The
        caller treats this like daily-quota-exhausted — skip the
        provider, surface a clear message.

        Returns: seconds waited (TPM + RPM combined).
        """
        # Defensive: clamp estimated_tokens to a sensible minimum.
        # Some callers may pass 0 or None during early scaffolding.
        try:
            est = int(estimated_tokens) if estimated_tokens else 0
        except (TypeError, ValueError):
            est = 0
        if est <= 0:
            # Conservative fallback for callers that haven't wired
            # the estimate through yet. 1500 tokens is a reasonable
            # middle-ground for short scan prompts. Log a one-time
            # breadcrumb so the call site can be fixed later.
            est = 1500

        if self.tpm is not None and est > self.tpm:
            raise QuotaExhausted(
                f"single call needs {est} tokens but "
                f"{self.provider_id} TPM cap is {self.tpm} — "
                f"this call can never fit. Reduce prompt size or "
                f"switch to a generous-tier provider.")

        wait_time_tpm = 0.0
        if self.tpm is not None:
            with self._lock:
                now = time.monotonic()
                cutoff = now - 60.0
                # Drop expired entries.
                while (self._recent_tokens
                        and self._recent_tokens[0][0] < cutoff):
                    self._recent_tokens.popleft()
                window_total = sum(
                    c for (_t, c) in self._recent_tokens)
                if window_total + est > self.tpm:
                    # Need to wait until enough oldest tokens roll off
                    # so that (current_total - rolled_off) + est <= tpm.
                    needed_room = (window_total + est) - self.tpm
                    rolled = 0
                    rolloff_at = now  # default: no wait needed
                    for (ts, tc) in self._recent_tokens:
                        rolled += tc
                        if rolled >= needed_room:
                            rolloff_at = ts + 60.0
                            break
                    wait_time_tpm = max(0.0, rolloff_at - now)
                # v4.14.5.62-limiter-concurrency: RESERVE the token slot UNDER
                # THIS LOCK (the same one that computed the wait), timestamped
                # for when the call fires (now + wait + buffer) — the exact
                # instant the old post-sleep append recorded. A concurrent
                # caller then sees these tokens already in the window and
                # computes its own wait against them, instead of both seeing
                # room and both admitting (TPM over-admission by N-1). The
                # sleep below stays OUTSIDE the lock.
                fire_at = now + wait_time_tpm + (
                    0.1 if wait_time_tpm > 0 else 0.0)
                self._recent_tokens.append((fire_at, est))
        if wait_time_tpm > 0 and log_fn:
            try:
                log_fn(
                    f"Rate-limit: {self.provider_id} waiting "
                    f"{wait_time_tpm:.1f}s for TPM ({self.tpm} "
                    f"tokens/min); planned ~{est} tokens this call",
                    'muted')
            except Exception:
                pass
        # v4.14.6.5-cache-ungate-tpm-skip: skip-don't-wait under
        # nonblocking_scope() — TPM is the binding constraint that
        # was stalling fill for ~50s a hit; raising NonBlockingBusy
        # lets the scan fallback chain reach a free provider INSTANTLY.
        if wait_time_tpm > 0 and nonblocking_active():
            raise NonBlockingBusy(wait_time_tpm, self.provider_id)
        if wait_time_tpm > 0:
            time.sleep(wait_time_tpm + 0.1)

        # Fall through to the existing RPM + RPD logic. The
        # standard acquire() method increments _daily_count and
        # _recent (RPM timestamps).
        wait_time_rpm = self.acquire(log_fn=log_fn)
        return wait_time_tpm + wait_time_rpm

    def daily_used(self) -> tuple[int, int | None]:
        """Return (used_today, daily_limit) for display."""
        with self._lock:
            self._check_day_rollover()
            return (self._daily_count, self.rpd)

    def peek_remaining(self) -> int:
        """Return the number of calls remaining today, WITHOUT
        consuming any. Parallel to DataCacheLayer.peek_quote — used
        by the continuous-queue top-AI picker to query budget
        without burning quota.

        - When rpd is None (no daily cap, e.g., Groq with no RPD set
          today), returns a large sentinel (10_000_000) so callers
          treating "lots remaining" as truthy work naturally.
        - When rpd is set, returns max(0, rpd - daily_count).
        - Day-rollover is honored: the counter resets at midnight so
          the peek reflects today's true remaining."""
        with self._lock:
            self._check_day_rollover()
            if self.rpd is None:
                return 10_000_000
            remaining = self.rpd - self._daily_count
            return max(0, remaining)


# Module-level singleton registry of limiters.
#
# v4.14.5.78-per-model-rate-gate: keys are now COMPOSITE — `(pid, model)`
# tuples. The model component is empty-string ('') when per-model is
# disabled OR no model is supplied — that gives back exactly today's
# per-provider single-bucket behavior. Multiple models on the same
# provider_id each get their own bucket: deque, daily counter, persist
# key.
_LIMITERS: dict[tuple[str, str], ProviderRateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()
_USAGE_PATH: Path | None = None


# v4.14.6.5-cache-ungate-tpm-skip (2026-06-11): non-blocking acquire
# scope for Tier-1 scan dispatch. The scan-fallback chain (in
# tm_api_providers.run_apis_for_scan_prediction) wraps its call_provider
# attempts in nonblocking_scope(); inside that scope, an acquire that
# would otherwise time.sleep on TPM/RPM raises NonBlockingBusy instead.
# The scan loop then catches it (re-raised as ProviderError with a 429-
# style message), classify_failure routes it as a quota outcome, and
# the chain advances to the next eligible provider INSTANTLY instead of
# stalling 50s. Tier-2 (consensus / lookup_fanout / holdings_consensus)
# does NOT use this scope, so blocking waits remain for paths that
# legitimately want a specific provider's vote. Thread-local so
# concurrent dispatch workers each get their own flag independently.
_TLS = threading.local()


class NonBlockingBusy(Exception):
    """Raised by ProviderRateLimiter.acquire / acquire_with_tokens when
    the calling thread is inside nonblocking_scope() AND the limiter
    would otherwise wait. Carries the would-wait time so the caller can
    log / decide. Caught by tm_api_providers.call_provider and re-raised
    as a ProviderError so the scan-fallback's quota-outcome handler
    advances the chain."""
    def __init__(self, wait_time: float, provider_id: str = ''):
        self.wait_time = float(wait_time or 0.0)
        self.provider_id = str(provider_id or '')
        super().__init__(
            f"non-blocking: would wait {self.wait_time:.1f}s on "
            f"{self.provider_id}")


def nonblocking_active() -> bool:
    """True iff the current thread is inside nonblocking_scope()."""
    return bool(getattr(_TLS, 'nonblock', False))


class nonblocking_scope:
    """Context manager — flips the thread-local nonblock flag for the
    duration of the `with` block. Nestable: an inner scope restores
    the outer's value on exit. Use only on the Tier-1 scan dispatch
    path; Tier-2 must keep blocking waits."""

    def __enter__(self):
        self._prev = bool(getattr(_TLS, 'nonblock', False))
        _TLS.nonblock = True
        return self

    def __exit__(self, exc_type, exc, tb):
        _TLS.nonblock = self._prev
        return False  # never suppress exceptions


def set_usage_path(path: Path) -> None:
    """Set the persistent usage file path. Called once at app start."""
    global _USAGE_PATH
    _USAGE_PATH = Path(path)


def _resolve_limits_for_model(provider: dict, model: str) -> dict:
    """v4.14.5.78-per-model-rate-gate: resolve rpm/rpd/tpm for a
    (provider, model) pair following the documented order:
      1. provider['model_limits'][model] (config override per provider)
      2. MODEL_DEFAULTS[normalized_model] (table for known divergent
         free-tier models like gemini-2.5-pro)
      3. provider['rate_limit_rpm'] / 'rpd' / 'tpm' (existing per-
         provider override)
      4. PRESET_DEFAULTS[preset]            (v.77 behavior — fallback)

    Returns {'rpm': int|None, 'rpd': int|None, 'tpm': int|None}. The
    ProviderRateLimiter constructor's own `if rpm and rpm > 0` gating
    already turns None into "no enforcement" for that axis, so missing
    entries naturally disable the corresponding check.
    """
    preset = (provider.get('preset') or 'custom').lower()
    defaults = PRESET_DEFAULTS.get(preset, PRESET_DEFAULTS['custom'])
    model_norm = _normalize_model(model)

    # Layer 4: preset
    rpm = defaults.get('rpm')
    rpd = defaults.get('rpd')
    tpm = defaults.get('tpm')

    # Layer 3: provider-level override
    _prv_rpm = provider.get('rate_limit_rpm')
    _prv_rpd = provider.get('rate_limit_rpd')
    _prv_tpm = provider.get('rate_limit_tpm')
    if _prv_rpm not in (None, '', 0):
        rpm = _prv_rpm
    if _prv_rpd not in (None, '', 0):
        rpd = _prv_rpd
    if _prv_tpm not in (None, '', 0):
        tpm = _prv_tpm

    if model_norm:
        # Layer 2: MODEL_DEFAULTS table (documented divergent cases).
        # Keyed on the normalized model string so users don't have to
        # match casing. Only applies overrides for fields the table
        # actually contains — a missing 'rpd' for a model keeps
        # whatever the layer-3/-4 chain produced (don't reset to None
        # by accident).
        md = MODEL_DEFAULTS.get(model_norm)
        if md:
            if 'rpm' in md:
                rpm = md.get('rpm')
            if 'rpd' in md:
                rpd = md.get('rpd')
            if 'tpm' in md:
                tpm = md.get('tpm')

        # Layer 1: per-provider per-model override (highest priority).
        # Shape: provider['model_limits'] = {model_name: {rpm, rpd, tpm}}
        ml = provider.get('model_limits')
        if isinstance(ml, dict):
            mp = ml.get(model_norm) or ml.get(str(model)) if model else None
            if isinstance(mp, dict):
                if 'rpm' in mp:
                    rpm = mp.get('rpm')
                if 'rpd' in mp:
                    rpd = mp.get('rpd')
                if 'tpm' in mp:
                    tpm = mp.get('tpm')

    # Coerce to int|None — accept '' / 0 / non-numeric as "missing".
    def _coerce(v):
        if v in (None, '', 0):
            return None
        try:
            iv = int(v)
            return iv if iv > 0 else None
        except (ValueError, TypeError):
            return None
    return {'rpm': _coerce(rpm), 'rpd': _coerce(rpd), 'tpm': _coerce(tpm)}


def get_limiter(provider: dict,
                model: 'str | None' = None) -> ProviderRateLimiter:
    """Return (or create) the rate limiter for a (provider, model) pair.

    v4.14.5.78-per-model-rate-gate: when per-model keying is enabled
    AND a model is provided (or readable from `provider['model']`),
    each (provider_id, model) gets its own bucket with model-aware
    rpm/rpd/tpm via the resolution order in `_resolve_limits_for_model`.

    When `_PER_MODEL_ENABLED is False`, OR no model is resolvable, the
    bucket falls back to a single per-provider one (model='') — exactly
    today's behavior. A provider with one model + no per-model config
    is byte-identical to v4.14.5.77.

    The `model` argument is optional — most legacy callers pass only
    the provider dict and get the provider's configured `model` field.
    Newer callers (call_provider) thread the dispatcher-selected model
    through explicitly so a router-pinned model gets its OWN bucket
    even when `provider['model']` is the configured default.
    """
    pid = str(provider.get('id') or 'unknown')
    # Resolve the model component of the key.
    if not _PER_MODEL_ENABLED:
        model_key = ''
    else:
        # Prefer explicit arg; else read provider['model']; else ''.
        m = model if model is not None else provider.get('model')
        model_key = _normalize_model(m)
    bucket_key = (pid, model_key)
    with _LIMITERS_LOCK:
        if bucket_key in _LIMITERS:
            return _LIMITERS[bucket_key]
        limits = _resolve_limits_for_model(provider, model_key)
        lim = ProviderRateLimiter(
            pid, limits['rpm'], limits['rpd'],
            usage_path=_USAGE_PATH,
            tpm=limits['tpm'],
            model=model_key)
        _LIMITERS[bucket_key] = lim
        return lim


def acquire_for_provider(provider: dict, log_fn=None,
                            estimated_tokens: int = 0,
                            model: 'str | None' = None) -> float:
    """Convenience: acquire a slot for the given provider+model pair.
    Raises QuotaExhausted if daily limit is hit.

    v4.14.3.8 (2026-05-14): when estimated_tokens > 0, the limiter
    also enforces TPM. Callers that don't pass a token estimate get
    legacy RPM/RPD-only behavior — no TPM enforcement. The queue
    runner (and call_provider, by default) computes the estimate
    from the prompt length.

    v4.14.5.78-per-model-rate-gate: `model` (optional) lets the
    caller specify which model is about to be dispatched, so a
    provider hosting multiple models with divergent per-minute
    ceilings (e.g. Google's flash vs pro) is gated on the RIGHT
    bucket. When omitted, falls back to provider['model'] — so
    legacy callers' behavior is unchanged.
    """
    lim = get_limiter(provider, model=model)
    if estimated_tokens and estimated_tokens > 0 and lim.tpm is not None:
        return lim.acquire_with_tokens(estimated_tokens, log_fn=log_fn)
    return lim.acquire(log_fn=log_fn)


def _reset_for_tests() -> None:
    """v4.14.5.78-per-model-rate-gate: clear the in-memory registry +
    re-enable per-model keying. NOT a production API — used by audit
    so each test starts from a clean slate."""
    global _PER_MODEL_ENABLED
    with _LIMITERS_LOCK:
        _LIMITERS.clear()
    _PER_MODEL_ENABLED = True


def daily_status() -> dict:
    """Return {provider_id: (used, limit_or_None)} for every limiter
    that's been touched. Useful for UI / activity log."""
    with _LIMITERS_LOCK:
        return {pid: lim.daily_used() for pid, lim in _LIMITERS.items()}


def peek_remaining_for_provider(provider: dict) -> int:
    """Convenience: return the remaining daily calls for a provider
    config dict. Mirrors acquire_for_provider's shape. Used by the
    continuous-queue top-AI picker to filter providers by budget.

    Defensive: if the provider isn't registered with a limiter yet
    (e.g., never called this session), creates one via get_limiter
    and reads its post-load remaining — usually a fresh-day full
    quota or whatever was persisted last session."""
    try:
        return get_limiter(provider).peek_remaining()
    except Exception:
        # If quota tracking fails entirely, assume plenty remaining
        # — better to over-pick than to falsely exhaust the picker.
        return 10_000_000
