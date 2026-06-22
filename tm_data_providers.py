"""
tm_data_providers.py — Data Provider Registry (v4.13.55)

What this is:
    A registry of all the data sources Tired Market can pull from
    (Yahoo, Stooq, Finnhub, EDGAR, optionally Massive). Each provider
    declares what kinds of data it serves and at what priority.

    This module is the FOUNDATION. The router (tm_data_router.py) uses
    these profiles to decide which source to call for any given request.
    The actual HTTP work happens in adapter modules (tm_data_adapter_*.py).

What it knows:
    - The full list of providers (built-in defaults + user-saved state)
    - Each provider's: enabled, key, priorities per data type, observed
      health, observed limits, recent error count
    - Persistence: data/data_providers.json

What it does NOT know:
    - How to actually call any source (that's the adapters' job)
    - Routing logic (that's the router's job)
    - The current "data mode" (api/free/hybrid) — that's in cfg

Mode taxonomy:
    api_only  - only providers with valid API keys are eligible
    free_only - only providers that need NO key are eligible
    hybrid    - all providers eligible, sorted by priority then health

Data types we care about (Phase 1):
    price       - current / live quote
    history     - historical OHLCV bars
    news        - headlines per ticker
    fundamentals - company financial data
    earnings    - earnings calendar (date / EPS estimate)
    filings     - SEC filings (8-K, 10-Q, 10-K, Form 4)

the user doesn't read code — plain English summaries are at the top of
every section.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# v4.14.5.14-classify429-data-side: reuse the AI-side 429 classifier (per-minute
# vs daily vs unknown) on the DATA side too. Soft import — fail-open to a short
# default cooldown if it's unavailable, never worse than before.
try:
    from tm_provider_learning import classify_429 as _classify_429
except Exception:  # pragma: no cover
    _classify_429 = None

# Escalating, AUTO-CLEARING cooldown curve (seconds) for per-minute/unknown
# 429s and for generic failures past the 3-in-a-row tolerance. Indexed by
# consecutive_failures; a 'daily' 429 cools until midnight instead.
_DATA_COOLDOWN_CURVE = (60, 300, 1800)   # 60s -> 5min -> 30min


# ─── Data type taxonomy ───────────────────────────────────────────────
# Single source of truth — all other modules import these names.

DATA_TYPES = (
    'price',
    'history',
    'news',
    'fundamentals',
    'earnings',
    'filings',
    'macro',         # v4.14.2 stage 4 — global (no ticker), Yahoo + FRED
    # 'social' REMOVED 2026-05-26 — see DECISIONS.md "Social data type —
    # dropped from supported pipeline". Reddit/StockTwits provider profiles
    # + their router registrations (tired_market.py) were removed; the
    # adapter files remain on disk for potential future reactivation.
)


# ─── Provider profile ─────────────────────────────────────────────────
# Pure data class. No behavior. Adapters pick these up and act on them.

@dataclass
class ProviderProfile:
    """Everything we know about one data provider.

    The schema is deliberately flat / JSON-friendly so it persists cleanly.
    Ordering of fields here is the storage order — don't reorder without
    a migration plan.
    """
    # ─ Identity ─
    id: str                              # "finnhub", "yahoo", "edgar", etc.
    display_name: str                    # human-readable
    needs_key: bool                      # if True, blank `key` = unusable

    # ─ User-controllable state ─
    enabled: bool = True
    key: str = ""                        # API key, or "" if needs_key=False
    tier: str = "free"                   # "free", "paid" — informational only

    # ─ Capabilities ─
    # Per-data-type priority. None = this provider does NOT serve this
    # data type. Lower number = higher priority.
    # Use ints starting at 1.
    priorities: dict[str, Optional[int]] = field(default_factory=dict)

    # ─ Limits ─
    # The numbers we BELIEVE based on docs / sign-up info.
    declared_limits: dict[str, int] = field(default_factory=dict)
    # The numbers we LEARN from real failures. Same keys as declared.
    # Whichever is lower (declared vs observed) is treated as the truth.
    observed_limits: dict[str, int] = field(default_factory=dict)

    # ─ Health / stats (rolling, persisted across sessions) ─
    health: str = "unknown"              # "green" / "amber" / "red" / "unknown"
    last_success_at: Optional[float] = None    # epoch seconds
    last_failure_at: Optional[float] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    calls_today: int = 0
    fails_today: int = 0
    today_iso: str = ""                  # YYYY-MM-DD; resets counters at rollover

    # ─ Documentation strings (no behavior) ─
    notes: str = ""                      # short user-visible blurb

    def serves(self, data_type: str) -> bool:
        """True if this provider has a non-None priority for this type."""
        return self.priorities.get(data_type) is not None

    def priority_for(self, data_type: str) -> Optional[int]:
        """Lower = higher priority. None = doesn't serve."""
        return self.priorities.get(data_type)

    def is_usable(self) -> bool:
        """Has the user enabled this provider AND given it a key if needed?"""
        if not self.enabled:
            return False
        if self.needs_key and not self.key:
            return False
        return True

    def to_dict(self) -> dict:
        """JSON-safe representation for persistence."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'ProviderProfile':
        """Inverse of to_dict. Tolerates missing fields by using defaults."""
        # Filter to only keys that map to real fields, so future fields
        # don't crash old saves and old fields don't crash new code.
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        clean = {k: v for k, v in d.items() if k in valid_keys}
        # Backfill defaults for any missing required fields. id/display_name/
        # needs_key are positional, everything else has a default.
        if 'id' not in clean:
            clean['id'] = 'unknown'
        if 'display_name' not in clean:
            clean['display_name'] = clean.get('id', 'Unknown')
        if 'needs_key' not in clean:
            clean['needs_key'] = False
        return cls(**clean)


# ─── Built-in defaults ────────────────────────────────────────────────
#
# These are the providers Tired Market knows about out of the box. The
# user can:
#   - Toggle enabled/disabled
#   - Add/remove keys for keyed sources
#   - Adjust priorities per data type
#   - NOT remove these entirely — they're built-in
#
# The list is the *seed*. After first load, the persisted JSON wins.
# That way new versions can add new built-ins without overwriting user
# customization.

def default_profiles() -> list[ProviderProfile]:
    return [
        ProviderProfile(
            id='yahoo',
            display_name='Yahoo Finance',
            needs_key=False,
            enabled=True,
            tier='free',
            priorities={
                'price': 1,           # primary for prices
                'history': 1,         # primary for history
                # v4.14.2 stage 1: Yahoo serves news/earnings at
                # priority 2 — keyless backup for users without a Finnhub
                # key, AND a per-ticker fallback for tickers Finnhub
                # silently drops from its bulk earnings calendar (the
                # v4.14.1.3 bug). Finnhub stays at priority 1 = primary
                # for NEWS when its key is configured.
                'news': 2,
                # v4.14.5.15-edgar-fundamentals-primary: demoted 1→2.
                # EDGAR (priority 1, authoritative SEC XBRL) is now the
                # keyless primary; Yahoo stays in the chain at priority 2
                # so the derived/market-only fields (market_cap, pe_ratio,
                # beta, dividend_yield, sector, industry) still reach the
                # FACTS consumer via the existing overlay path. When
                # Yahoo throttles, EDGAR alone supplies the statement
                # snapshot; the overlay short-circuits, FACTS just omits
                # the market-derived lines for that pass.
                'fundamentals': 2,
                'earnings': 2,
                'filings': None,      # EDGAR is authoritative here.
                # v4.14.2 stage 4: Yahoo serves macro at priority 2 —
                # ^TNX/^FVX/^IRX/^TYX yields and ^VIX volatility, no
                # API key required. FRED at priority 1 covers the
                # full economic series catalog (Fed funds, CPI,
                # unemployment, GDP) when its free key is configured.
                'macro': 2,
                'social': None,       # v4.14.2 stage 5 — Yahoo is finance-only
            },
            declared_limits={'calls_per_min': 60},
            health='green',
            notes=(
                "Primary source for current prices and historical bars. "
                "Keyless backup for news, fundamentals, earnings, and "
                "macro (Treasury yields + VIX) via yfinance — paid "
                "sources take priority when their keys are configured; "
                "Yahoo only fires when they're absent or unavailable, "
                "so its rate limiter stays light in the common case."
            ),
        ),
        ProviderProfile(
            id='stooq',
            display_name='Stooq',
            needs_key=False,
            enabled=True,
            tier='free',
            priorities={
                'price': 2,           # fallback if Yahoo fails
                'history': 2,         # fallback if Yahoo fails
                'news': None,
                'fundamentals': None,
                'earnings': None,
                'filings': None,
                'macro': None,        # v4.14.2 stage 4 — Stooq is price-only
                'social': None,       # v4.14.2 stage 5
            },
            declared_limits={'calls_per_min': 30},
            health='green',
            notes=(
                "Free fallback for prices and history when Yahoo is "
                "unavailable. Spotty coverage on penny stocks and "
                "very obscure tickers."
            ),
        ),
        # ── Nasdaq earnings calendar (keyless PRIMARY for earnings) ──────
        # Added 2026-05-26. Bulk-by-date public endpoint; the adapter
        # (tm_data_adapter_nasdaq.py) sweeps a ~45-day near-term window into
        # an internal cache and answers per-ticker from it. Keyless-first:
        # Nasdaq(1) + Yahoo(2) are keyless, Finnhub(3) is the keyed bonus.
        # See DECISIONS.md 2026-05-26.
        ProviderProfile(
            id='nasdaq',
            display_name='Nasdaq Earnings Calendar',
            needs_key=False,
            enabled=True,
            tier='free',
            priorities={
                'price': None, 'history': None, 'news': None,
                'fundamentals': None,
                'earnings': 1,        # PRIMARY (keyless) for earnings calendar
                'filings': None, 'macro': None, 'social': None,
            },
            declared_limits={'calls_per_day': 100},
            health='unknown',
            notes=(
                "Nasdaq public earnings calendar (api.nasdaq.com, keyless, "
                "no signup). Bulk-by-date; the adapter sweeps a ~45-day "
                "window once/24h into an internal cache and serves per-ticker "
                "from it. Dense near-term, sparse far-term — Yahoo (priority "
                "2) covers tickers beyond the window. Requires a browser "
                "User-Agent. Keyless-first per DECISIONS.md 2026-05-26."
            ),
        ),
        ProviderProfile(
            id='finnhub',
            display_name='Finnhub',
            needs_key=True,
            enabled=True,
            tier='free',
            key='',  # User pastes their key
            priorities={
                'price': None,        # Yahoo handles prices
                'history': None,      # Yahoo handles history
                'news': 1,            # PRIMARY for news
                'fundamentals': 3,    # v4.14.5.15-edgar-fundamentals-primary: demoted 2→3 — EDGAR(1) is now keyless primary, Yahoo(2) supplies overlay, Finnhub(3) is keyed bonus resilience.
                'earnings': 3,        # demoted 2026-05-26 — keyed BONUS; Nasdaq(1)+Yahoo(2) are keyless. See DECISIONS.md
                'filings': None,      # EDGAR is better for filings
                'macro': None,        # v4.14.2 stage 4 — FRED owns macro
                'social': None,       # v4.14.2 stage 5
            },
            declared_limits={'calls_per_min': 60, 'calls_per_day': 0},
            health='unknown',
            notes=(
                "Free tier provides 60 calls/min, real-time US stock "
                "data. Best free source for news, fundamentals, and "
                "earnings calendar. Sign up at finnhub.io."
            ),
        ),
        ProviderProfile(
            id='edgar',
            display_name='SEC EDGAR',
            needs_key=False,
            enabled=True,
            tier='free',
            priorities={
                'price': None,
                'history': None,
                'news': None,
                # v4.14.5.15-edgar-fundamentals-primary: promoted None→1.
                # The XBRL companyfacts endpoint is keyless, throttle-
                # resistant (SEC infra), and covers ~99.5% of our universe
                # in ONE bulk call/ticker. The adapter's 'fundamentals'
                # branch reuses the existing fetch_fundamentals() + CIK
                # map + polite 2/sec throttle; market-derived fields
                # (market_cap/pe_ratio/beta/dividend_yield) stay None
                # here and are filled by the existing derived-overlay
                # from Yahoo when actually needed. Yahoo(2)+Finnhub(3)
                # remain as fallback when EDGAR returns None.
                'fundamentals': 1,
                'earnings': None,
                'filings': 1,         # PRIMARY (and only) for filings
                'macro': None,        # v4.14.2 stage 4 — EDGAR is filings-only
                'social': None,       # v4.14.2 stage 5
            },
            declared_limits={'calls_per_min': 10},  # SEC requests politeness
            health='green',
            notes=(
                "The official source for SEC filings (8-K, 10-Q, 10-K, "
                "Form 4 insider transactions). Free, no key required. "
                "We rate-limit ourselves to be polite."
            ),
        ),
        # Massive (formerly Polygon) — included as an OPTIONAL provider
        # for users who upgrade to its paid tier. Ships disabled by
        # default. Free tier is too limited (5 calls/min) to be useful
        # for our scan workloads, but the adapter is ready.
        ProviderProfile(
            id='massive',
            display_name='Massive (formerly Polygon)',
            needs_key=True,
            enabled=False,            # OFF by default
            tier='free',
            key='',
            priorities={
                'price': None,        # disabled by default; flip to ~3 if user wants fallback
                'history': None,
                'news': None,
                'fundamentals': None,
                'earnings': None,
                'filings': None,
                'macro': None,        # v4.14.2 stage 4
                'social': None,       # v4.14.2 stage 5
            },
            declared_limits={'calls_per_min': 5},
            health='unknown',
            notes=(
                "Optional paid source. Free tier (5 calls/min) is too "
                "limited for scan workloads. Enable + paste a key only "
                "if you have a paid plan."
            ),
        ),
        # ── v4.13.58: alternate news sources ────────────────────────
        # All three ship DISABLED. User opts in by editing the entry
        # in the Data Providers dialog and pasting a free-tier API
        # key. Once enabled, the data router rotates them based on
        # priority + observed quota.
        ProviderProfile(
            id='marketaux',
            display_name='Marketaux',
            needs_key=True,
            enabled=False,            # OFF by default
            tier='free',
            key='',
            priorities={
                'price': None,
                'history': None,
                'news': 2,            # Secondary news (Finnhub primary)
                'fundamentals': None,
                'earnings': None,
                'filings': None,
                'macro': None,        # v4.14.2 stage 4
                'social': None,       # v4.14.2 stage 5
            },
            declared_limits={'calls_per_day': 100},
            health='unknown',
            notes=(
                "Real-time financial news with per-entity sentiment "
                "scores. Free tier: 100 requests/day. Sign up at "
                "marketaux.com — alternative to Finnhub for news."
            ),
        ),
        ProviderProfile(
            id='newsapi',
            display_name='NewsAPI',
            needs_key=True,
            enabled=False,
            tier='free',
            key='',
            priorities={
                'price': None,
                'history': None,
                'news': 3,
                'fundamentals': None,
                'earnings': None,
                'filings': None,
                'macro': None,        # v4.14.2 stage 4
                'social': None,       # v4.14.2 stage 5
            },
            declared_limits={'calls_per_day': 100},
            health='unknown',
            notes=(
                "Broader news source — searches the web by ticker. "
                "Free tier: 100 requests/day. Best as a fallback when "
                "ticker-specific sources don't have coverage."
            ),
        ),
        ProviderProfile(
            id='twelve_data',
            display_name='Twelve Data',
            needs_key=True,
            enabled=False,
            tier='free',
            key='',
            priorities={
                'price': None,
                'history': None,
                'news': 4,
                'fundamentals': None,
                'earnings': None,
                'filings': None,
                'macro': None,        # v4.14.2 stage 4
                'social': None,       # v4.14.2 stage 5
            },
            declared_limits={'calls_per_day': 800},
            health='unknown',
            notes=(
                "Largest free-tier quota among the news providers "
                "(800/day). News endpoint may require a paid plan on "
                "some accounts — adapter will warn if unavailable."
            ),
        ),
        # ── v4.14.2 stage 4 / v4.14.5.14-macro-keyless: FRED macro ──
        # KEYLESS-FIRST. Yahoo serves the lane keylessly at priority 2
        # (^TNX, ^FVX, ^IRX, ^TYX yields + ^VIX); FRED at priority 1
        # adds the rest (Fed funds, CPI, unemployment, GDP, canonical
        # yields) via its OWN keyless CSV endpoint + Treasury/BLS
        # fallbacks (see tm_data_adapter_fred). The cache layer merges
        # both into one dict. A FRED JSON API key is OPTIONAL — when
        # set, the adapter uses the keyed JSON API for a higher rate
        # ceiling, but it is never required. needs_key=False is what
        # keeps FRED out of the teacher AI's key recommendations (the
        # last keyed-only data capability is now eliminated). See
        # DECISIONS.md 2026-05-26 "Macro keyless".
        ProviderProfile(
            id='fred',
            display_name='FRED Economic Data',
            needs_key=False,     # CHANGED 2026-05-26: key now optional, not required
            enabled=True,
            tier='free',
            key='',
            priorities={
                'price': None,
                'history': None,
                'news': None,
                'fundamentals': None,
                'earnings': None,
                'filings': None,
                'macro': 1,          # PRIMARY for macro (keyless CSV; key optional)
                'social': None,       # v4.14.2 stage 5 — FRED is macro-only
            },
            declared_limits={'calls_per_min': 120},
            health='unknown',
            notes=(
                "Federal Reserve Economic Data — KEYLESS. Fed funds, "
                "CPI, unemployment, GDP, yields + curve spread via "
                "FRED's public CSV endpoint (Treasury/BLS keyless "
                "fallbacks). One batched fetch per 12-hour cache cycle. "
                "An optional FRED API key (free, fredaccount.stlouisfed.org) "
                "switches to the keyed JSON API for a higher rate ceiling, "
                "but is not required."
            ),
        ),
        # ── Social lane (Reddit + StockTwits) REMOVED 2026-05-26 ─────
        # Social is no longer a supported data type. See DECISIONS.md
        # "Social data type — dropped from supported pipeline (decided
        # 2026-05-26)": Reddit's keyless path requires an API-approval
        # process end users effectively never get, and StockTwits alone
        # isn't rich enough to justify the maintenance. The adapter files
        # (tm_data_adapter_reddit.py / tm_data_adapter_stocktwits.py) remain
        # on disk as scaffolding; the matching register_with() calls in
        # tired_market.py were also removed, and 'social' was dropped from
        # DATA_TYPES above. To reactivate (if a usable keyless social source
        # emerges): re-add a ProviderProfile here + the router registration.
    ]


# ─── v4.14.6.64-reliability-persist (S2) constants ────────────────────
#
# Persist v60's source-reliability COOLDOWN signal across restarts as a
# SORT-ONLY hint, so a relaunched app starts already knowing which sources
# were misbehaving last session (kills the ~30-min "re-learn" / re-429
# storm). The fail-rate term (calls_today/fails_today/today_iso) ALREADY
# persists in data_providers.json — untouched here; the cooldown signal is
# the only gap.
#
# 🚨 LANDMINE: self._cooldown gates BOTH the sort (reliability_score) AND
# eligibility (in_cooldown -> _is_eligible). The persisted snapshot is
# restored into a SEPARATE structure (_persisted_cd_hint), NEVER back into
# self._cooldown — restoring it there would resurrect eligibility BLOCKS on
# launch, reversing the deliberate v4.14.6.18 "rested launch = clean slate"
# decision and over-anchoring on stale state. Sort-only, by design.
_RELIABILITY_SNAPSHOT_FILENAME = 'source_reliability.json'
# Restore weight: 0.4 (vs the live 0.8 cooldown weight). Persisted state
# only NUDGES the sort — a single fresh live observation easily overcomes
# it. Structurally can help, structurally cannot dominate.
_RELIABILITY_HINT_WEIGHT = 0.4
# Snapshot ceiling: if the file is older than this, discard the WHOLE
# snapshot on load. Per-minute/error cooldowns max ~30 min, a daily cooldown
# is meaningful only to local midnight; a >6h-old picture is stale by
# definition and must not over-anchor a fresh session.
_RELIABILITY_SNAPSHOT_MAX_AGE_SEC = 6 * 3600
# Debounce disk writes (mirrors lane_pacing's SAVE_DEBOUNCE_SEC).
_RELIABILITY_SAVE_DEBOUNCE_SEC = 30.0


# ─── Registry: load / save / mutate ───────────────────────────────────
#
# Singleton-ish: one Registry per app. Lock-protected. JSON-backed.
# Idempotent — call load() many times safely.

class Registry:
    """Owns the list of provider profiles. Persistent across sessions.

    Thread-safety: operations that read/write the profile list take
    self._lock. Adapters that have already received a profile reference
    can read it lock-free (profiles are simple dataclasses).
    """

    def __init__(self, json_path: 'Path | None' = None):
        self._lock = threading.Lock()
        self._path = json_path
        self._profiles: dict[str, ProviderProfile] = {}
        self._loaded = False
        # v4.14.5.14-classify429-data-side: per-provider time-based cooldowns
        # (IN-MEMORY ONLY — session-scoped, never persisted; an expired
        # cooldown is harmless). provider_id -> {'until': epoch, 'kind': str}.
        # Replaces the old "mis-learn a daily cap from any 429 → ineligible
        # until midnight / stuck-red-until-restart" behaviour. Eligibility is
        # gated on cooldown expiry, so providers self-recover within a session.
        self._cooldown: dict[str, dict] = {}
        # v4.14.6.64-reliability-persist (S2): SORT-ONLY snapshot of last
        # session's active cooldowns, restored from source_reliability.json on
        # load(). Read ONLY by reliability_score (at _RELIABILITY_HINT_WEIGHT);
        # NEVER consulted by in_cooldown/eligibility. Key shape mirrors
        # self._cooldown: (provider_id, data_type) -> {'until': epoch,
        # 'kind': str}. The persist file lives next to data_providers.json so
        # it stays a per-install runtime sibling (never promoted to Public).
        self._persisted_cd_hint: dict[tuple, dict] = {}
        self._reliability_path: 'Path | None' = (
            (self._path.parent / _RELIABILITY_SNAPSHOT_FILENAME)
            if self._path is not None else None)
        self._last_reliability_save_at: float = 0.0

    # ── Public lifecycle ──────────────────────────────────────────────

    def load(self) -> None:
        """Load profiles from disk. If file doesn't exist or is broken,
        seed from defaults. Either way, ensures every default provider
        exists in the registry (so app upgrades that add new built-ins
        get picked up automatically).

        Idempotent — calling twice is fine, second call is a no-op.
        """
        with self._lock:
            if self._loaded:
                return

            # Step 1: seed with defaults
            for p in default_profiles():
                self._profiles[p.id] = p

            # Step 2: overlay persisted state (if any) on top
            if self._path is not None and self._path.exists():
                try:
                    with open(self._path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    saved = data.get('providers', []) if isinstance(data, dict) else []
                    for entry in saved:
                        try:
                            prof = ProviderProfile.from_dict(entry)
                            # Persist user-modified state, but keep
                            # built-in metadata fresh (notes, etc.).
                            existing = self._profiles.get(prof.id)
                            if existing is not None:
                                # Preserve user-controlled fields,
                                # let app-controlled fields refresh.
                                existing.enabled = prof.enabled
                                existing.key = prof.key
                                existing.tier = prof.tier
                                # v4.14.5.18-priority-promotion-overlay:
                                # per-(provider, data_type) priority merge.
                                # Persisted state may OVERRIDE with a more-
                                # specific value (user reorder), but must
                                # NEVER silently demote a code-seeded non-
                                # null priority to null/absent. Pre-fix this
                                # was a wholesale dict replacement, which let
                                # a stale saved `null` strip the v4.14.5.15
                                # EDGAR fundamentals=1 promotion on every
                                # boot (NTAP exhaustion at 10:32 the day of
                                # the patch was the live proof). Rule per
                                # data_type key: persisted non-null wins;
                                # persisted None keeps the seeded value;
                                # both None stays None. Self-heals on next
                                # save() — the corrected in-memory value is
                                # what gets persisted. See DECISIONS 2026-
                                # 05-28 (4.14.5.18) for the broader lesson.
                                _seeded_p = existing.priorities or {}
                                _persisted_p = prof.priorities or {}
                                _merged_p = dict(_seeded_p)
                                for _dtype, _pv in _persisted_p.items():
                                    if _pv is not None:
                                        _merged_p[_dtype] = _pv
                                    # else: keep seeded (no demote)
                                existing.priorities = _merged_p
                                existing.observed_limits = prof.observed_limits
                                existing.health = prof.health
                                existing.last_success_at = prof.last_success_at
                                existing.last_failure_at = prof.last_failure_at
                                existing.last_error = prof.last_error
                                existing.consecutive_failures = prof.consecutive_failures
                                existing.calls_today = prof.calls_today
                                existing.fails_today = prof.fails_today
                                existing.today_iso = prof.today_iso
                            else:
                                # User-added (non-default) provider —
                                # just take it whole.
                                self._profiles[prof.id] = prof
                        except Exception:
                            # Skip malformed entries; don't blow up the
                            # whole registry on one bad row.
                            continue
                except Exception:
                    # Corrupt JSON file — proceed with defaults only.
                    pass

            # Step 3: roll over daily counters if we crossed midnight
            today = _today_iso()
            for p in self._profiles.values():
                if p.today_iso != today:
                    p.calls_today = 0
                    p.fails_today = 0
                    p.today_iso = today

            # v4.14.6.18-yahoo-cooldown-fix (2026-06-12): cooldown state
            # is session-local — never persisted by save(). A fresh
            # Registry construction starts with `_cooldown = {}` anyway,
            # but clear() explicitly here makes the "rested launch =
            # clean slate" intent unmistakable and protects against
            # any future re-init path that might inherit stale state.
            # Near-no-op today; defensive scaffolding.
            self._cooldown.clear()

            # v4.14.6.64-reliability-persist (S2): restore last session's
            # cooldown SNAPSHOT into the SORT-ONLY hint — NOT into _cooldown
            # (which we just cleared, and which gates eligibility). See the
            # LANDMINE note at the module constants. Fully fail-safe.
            self._load_reliability_snapshot_locked()

            self._loaded = True

    def save(self) -> None:
        """Persist current profiles to disk. Best-effort — errors are
        swallowed because we never want to crash the app over save
        failures. Atomic write via temp + rename."""
        if self._path is None:
            return
        with self._lock:
            data = {
                'providers': [p.to_dict() for p in self._profiles.values()],
                'saved_at': time.time(),
                'version': 1,
            }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + '.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            tmp.replace(self._path)
        except Exception:
            pass

    # ── v4.14.6.64-reliability-persist (S2): sort-only snapshot ────────

    def _load_reliability_snapshot_locked(self) -> None:
        """Restore last session's active-cooldown snapshot into the SORT-ONLY
        hint (self._persisted_cd_hint). Caller MUST hold self._lock.

        🚨 Restores into _persisted_cd_hint ONLY — never into self._cooldown
        (eligibility must stay clean-slate per v4.14.6.18). Fully fail-safe:
        missing / corrupt / non-dict / stale file → empty hint → byte-
        identical to pre-S2 behavior; never raises.

        Staleness: (a) whole-snapshot ceiling — if saved_at is older than
        _RELIABILITY_SNAPSHOT_MAX_AGE_SEC, discard everything; (b) per-entry —
        drop any entry whose 'until' is already in the past (expired cooldowns
        auto-decay; a recovered source isn't punished)."""
        self._persisted_cd_hint = {}
        path = self._reliability_path
        if path is None or not path.exists():
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            # Corrupt / unreadable → start fresh (current behavior).
            return
        if not isinstance(data, dict):
            return
        now = time.time()
        # (a) whole-snapshot age ceiling.
        try:
            saved_at = float(data.get('saved_at', 0) or 0)
        except Exception:
            saved_at = 0.0
        if saved_at <= 0 or (now - saved_at) > _RELIABILITY_SNAPSHOT_MAX_AGE_SEC:
            return
        sources = data.get('sources')
        if not isinstance(sources, dict):
            return
        restored: dict[tuple, dict] = {}
        for skey, entry in sources.items():
            try:
                if not isinstance(entry, dict):
                    continue
                until = float(entry.get('until', 0) or 0)
                # (b) per-entry expiry — drop already-expired cooldowns.
                if until <= now:
                    continue
                # Stored key is "provider_id|data_type" (data_type may be the
                # empty string for the provider-wide / data_type=None bucket).
                if '|' not in str(skey):
                    continue
                provider_id, _sep, dtype_raw = str(skey).partition('|')
                if not provider_id:
                    continue
                dtype = dtype_raw if dtype_raw != '' else None
                restored[(provider_id, dtype)] = {
                    'until': until,
                    'kind': str(entry.get('kind', '') or ''),
                }
            except Exception:
                # Skip one malformed entry; never blow up the whole load.
                continue
        self._persisted_cd_hint = restored

    def _save_reliability_snapshot(self, force: bool = False) -> None:
        """Persist a snapshot of currently-active cooldowns to
        source_reliability.json so next launch can deprioritize (sort-only)
        last session's misbehaving sources.

        Best-effort, debounced (mirrors lane_pacing). Builds its own snapshot
        under self._lock then writes OUTSIDE the lock (self._lock is non-
        reentrant — callers like record_failure/record_success already hold it
        when they DECIDE to save, but must release before calling this).

        Persists ONLY (provider, data_type, until, kind) for cooldowns still
        active at save time. Deliberately does NOT persist consecutive_failures
        (a stale streak would wrongly escalate the next failure to a level-2/3
        300s/1800s cooldown) nor fail counts (already in data_providers.json)."""
        path = self._reliability_path
        if path is None:
            return
        now = time.time()
        with self._lock:
            if (not force
                    and (now - self._last_reliability_save_at)
                    < _RELIABILITY_SAVE_DEBOUNCE_SEC):
                return
            self._last_reliability_save_at = now
            sources: dict[str, dict] = {}
            for key, cd in self._cooldown.items():
                if not (isinstance(key, tuple) and len(key) == 2):
                    continue
                if not cd:
                    continue
                until = cd.get('until', 0) or 0
                if until <= now:
                    continue  # expired — don't carry it forward
                provider_id, dtype = key
                skey = f"{provider_id}|{dtype if dtype is not None else ''}"
                sources[skey] = {
                    'until': float(until),
                    'kind': str(cd.get('kind', '') or ''),
                }
            data = {'version': 1, 'saved_at': now, 'sources': sources}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + '.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            tmp.replace(path)
        except Exception:
            # Persistence is best-effort — never crash over a save failure.
            pass

    # ── Public read API ───────────────────────────────────────────────

    def get(self, provider_id: str) -> Optional[ProviderProfile]:
        with self._lock:
            return self._profiles.get(provider_id)

    def all(self) -> list[ProviderProfile]:
        """Returns a snapshot list of all profiles. The dataclass
        instances themselves are mutable; callers mutating them must
        call save() to persist."""
        with self._lock:
            return list(self._profiles.values())

    def serving(self, data_type: str) -> list[ProviderProfile]:
        """All providers that declare priority for this data type.
        NOT filtered by enabled or has-key — that's the router's job."""
        with self._lock:
            out = [p for p in self._profiles.values() if p.serves(data_type)]
        # Sort by priority ascending (1 = best). Stable sort preserves
        # insertion order for ties.
        out.sort(key=lambda p: p.priority_for(data_type) or 999)
        return out

    # ── Public write API ──────────────────────────────────────────────

    def update_key(self, provider_id: str, new_key: str) -> bool:
        """Set / clear an API key. Returns True on success."""
        with self._lock:
            p = self._profiles.get(provider_id)
            if p is None:
                return False
            p.key = new_key.strip() if new_key else ""
            # Invalidate health so the router will re-test on next call.
            p.health = 'unknown'
            p.consecutive_failures = 0
            p.last_error = None
        self.save()
        return True

    def set_enabled(self, provider_id: str, enabled: bool) -> bool:
        with self._lock:
            p = self._profiles.get(provider_id)
            if p is None:
                return False
            p.enabled = bool(enabled)
        self.save()
        return True

    def set_priority(self, provider_id: str, data_type: str,
                      priority: Optional[int]) -> bool:
        """Change a provider's priority for one data type. None = disable
        for this type. Otherwise an int 1+ (lower = higher priority)."""
        if data_type not in DATA_TYPES:
            return False
        with self._lock:
            p = self._profiles.get(provider_id)
            if p is None:
                return False
            if priority is None:
                p.priorities[data_type] = None
            else:
                try:
                    p.priorities[data_type] = max(1, int(priority))
                except (TypeError, ValueError):
                    return False
        self.save()
        return True

    # ── Health / observation API (called by adapters) ─────────────────

    def record_success(self, provider_id: str,
                        data_type: Optional[str] = None) -> None:
        """Adapter calls this after a successful API call.

        v4.14.6.56-per-type-provider-health: cooldowns are now keyed by
        (provider_id, data_type). A success on a specific data_type clears
        that bucket only — a fundamentals success no longer prematurely
        clears a prices cooldown on the same provider, and vice versa.
        When data_type is None (legacy/unknown caller), every bucket for
        this provider is cleared to preserve old behavior."""
        health_changed = False
        with self._lock:
            p = self._profiles.get(provider_id)
            if p is None:
                return
            now = time.time()
            self._roll_over_day_locked(p)
            p.last_success_at = now
            p.consecutive_failures = 0
            p.calls_today += 1
            p.last_error = None
            # v4.14.5.14 / v4.14.6.56: a success ends the relevant cooldown —
            # the trial call after the cooldown expired succeeded.
            # v4.14.6.64: track whether anything was actually cleared so we
            # only re-snapshot the reliability hint when the live picture
            # changed (a success on an already-clear source is the hot path).
            cd_cleared = False
            if data_type is None:
                # Legacy/unknown caller: clear every bucket for this provider.
                for k in [key for key in self._cooldown
                          if isinstance(key, tuple) and key[0] == provider_id]:
                    if self._cooldown.pop(k, None) is not None:
                        cd_cleared = True
            else:
                if self._cooldown.pop((provider_id, data_type), None) is not None:
                    cd_cleared = True
            # Promote health on sustained success
            if p.health != 'green':
                p.health = 'green'
                health_changed = True
        # v4.13.58.1: persist on health-state CHANGE so the Data Providers
        # dialog shows the correct dot color across app sessions. Successive
        # successes still skip saving (the change is already persisted).
        # This is a thin debounce — the disk hit only happens on the first
        # success after a failure or unknown state.
        if health_changed:
            try:
                self.save()
            except Exception:
                pass
        # v4.14.6.64-reliability-persist (S2): a cleared cooldown changed the
        # live picture — re-snapshot (debounced) so the persisted sort-only
        # hint stays current. Outside the lock; no-op without a persist path.
        if cd_cleared:
            self._save_reliability_snapshot()

    def record_failure(self, provider_id: str, error: str = "",
                        is_rate_limit: bool = False,
                        data_type: Optional[str] = None) -> dict:
        """Router calls this after a failed call. Returns a dict describing any
        cooldown applied, for the caller to log:
        {'cooldown': bool, 'seconds': int, 'kind': str, 'until': float}.

        v4.14.5.14-classify429-data-side: a 429 NO LONGER mis-learns a daily
        cap (the old `observed_limits['calls_per_day'] = calls_today*0.95`,
        which treated a per-MINUTE 429 as a permanent daily wall → ineligible
        until midnight). Instead the 429 is classified (per-minute / daily /
        unknown via classify_429) and a TIME-BASED cooldown is applied that
        AUTO-CLEARS; eligibility is gated on the cooldown, so providers
        self-recover within a session. Generic (non-429) failures only cool
        down after 3-in-a-row (preserving the old tolerance for 1-2 transient
        blips), then escalate. `health` stays as the Data-Providers-dialog
        DISPLAY signal only — it no longer gates eligibility (that was the
        stuck-red bug). Cooldown state is in-memory (self._cooldown)."""
        info = {'cooldown': False, 'seconds': 0, 'kind': '', 'until': 0.0}
        with self._lock:
            p = self._profiles.get(provider_id)
            if p is None:
                return info
            now = time.time()
            self._roll_over_day_locked(p)

            # v4.14.6.18-yahoo-cooldown-fix (2026-06-12): coalesce 429s
            # observed while a rate-limit cooldown is ALREADY active.
            # Those failures are the SAME observed per-minute limit
            # from one burst, not 3 distinct sustained failures. The
            # pre-fix bug: a startup burst hits Yahoo's IP cap → first
            # 429 cools 60s and bumps consecutive_failures to 1, but
            # the 2nd/3rd 429 from the same burst land BEFORE the 60s
            # window expires and bump consecutive_failures to 2/3,
            # which on the next genuine post-expiry failure would
            # apply the level-2 (300s) or level-3 (1800s) cooldown —
            # off ONE bad minute, Yahoo gets a 30-minute timeout. With
            # this guard, the additional within-window 429s update the
            # bookkeeping (last_failure_at, last_error, fails_today,
            # calls_today) but do NOT advance consecutive_failures or
            # re-apply the curve. First 429 (no active cooldown) still
            # cools 60s; a NEW 429 AFTER the 60s window expires still
            # escalates to 300s exactly as designed; non-rate-limit
            # failures are unaffected by this guard.
            # v4.14.6.56-per-type-provider-health: cooldown bucket is keyed by
            # (provider_id, data_type). Same provider's other data types are
            # unaffected. data_type=None falls back to a provider-wide bucket
            # so legacy/unknown call sites preserve old behavior.
            _cd_key = (provider_id, data_type)
            _existing_cd = self._cooldown.get(_cd_key)
            if (is_rate_limit
                    and _existing_cd is not None
                    and now < _existing_cd.get('until', 0)):
                p.last_failure_at = now
                p.last_error = (error or "")[:200]
                p.fails_today += 1
                p.calls_today += 1
                # Return the still-active cooldown info so the caller
                # sees a consistent shape (cooldown=True, remaining
                # seconds, original kind).
                return {
                    'cooldown': True,
                    'seconds': int(max(0,
                        _existing_cd.get('until', 0) - now)),
                    'kind': _existing_cd.get('kind', ''),
                    'until': _existing_cd.get('until', 0),
                }

            p.last_failure_at = now
            p.last_error = (error or "")[:200]  # truncate for sanity
            p.consecutive_failures += 1
            p.fails_today += 1
            p.calls_today += 1

            # Health is now a DISPLAY signal only (does NOT gate eligibility).
            if p.consecutive_failures >= 3:
                p.health = 'red'
            elif p.consecutive_failures >= 1:
                p.health = 'amber'

            level = min(p.consecutive_failures - 1,
                        len(_DATA_COOLDOWN_CURVE) - 1)
            seconds = 0
            kind = ''
            if is_rate_limit:
                cls = {'type': 'unknown', 'retry_after_seconds': 0}
                if _classify_429 is not None:
                    try:
                        cls = _classify_429(provider_id, meta={},
                                            body=(error or '')) or cls
                    except Exception:
                        cls = {'type': 'unknown', 'retry_after_seconds': 0}
                ctype = cls.get('type', 'unknown')
                ra = int(cls.get('retry_after_seconds') or 0)
                if ctype == 'daily':
                    # Real daily-quota evidence: honour a long Retry-After if
                    # present, else cool until the next local midnight (where
                    # the day counters roll over). This is the ONLY path that
                    # persists past a short window now.
                    seconds = ra if ra > 0 else self._seconds_to_midnight(now)
                    kind = 'daily'
                else:
                    # per-minute / unknown → short, escalating, auto-clearing.
                    seconds = max(ra, _DATA_COOLDOWN_CURVE[level])
                    kind = 'per-minute' if ctype == 'per_minute' else 'rate-limit'
            elif p.consecutive_failures >= 3:
                # Generic errors: tolerate 1-2 transient blips, then apply an
                # escalating (auto-clearing) cooldown — was a permanent red.
                seconds = _DATA_COOLDOWN_CURVE[level]
                kind = 'error'

            if seconds > 0:
                until = now + seconds
                # v4.14.6.56: per (provider, data_type) bucket.
                self._cooldown[_cd_key] = {'until': until, 'kind': kind,
                                            'data_type': data_type}
                info = {'cooldown': True, 'seconds': int(seconds),
                        'kind': kind, 'until': until,
                        'data_type': data_type}
        # v4.14.6.64-reliability-persist (S2): a NEW cooldown changed the
        # live picture — snapshot it (debounced) so a restart keeps the
        # sort-only hint. Called OUTSIDE the lock (non-reentrant); no-op when
        # no persist path is wired. Coalesced 429s returned early above and
        # deliberately skip this (no new state).
        if info.get('cooldown'):
            self._save_reliability_snapshot()
        return info

    @staticmethod
    def _seconds_to_midnight(now_epoch: float) -> int:
        """Seconds from `now_epoch` to the next LOCAL midnight (when the daily
        counters roll over). Floored at 60s so a near-midnight daily hit still
        backs off briefly."""
        import datetime as _dt
        now = _dt.datetime.fromtimestamp(now_epoch)
        nxt = (now + _dt.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return max(60, int((nxt - now).total_seconds()))

    def in_cooldown(self, provider_id: str,
                     data_type: Optional[str] = None) -> bool:
        """True if `(provider_id, data_type)` is within an unexpired cooldown
        window. An EXPIRED entry is cleared here (lazily) so the next call
        gets through as a 'trial' — a fresh failure re-cools (escalated), a
        success clears it via record_success.

        v4.14.6.56-per-type-provider-health: cooldowns are keyed by (provider,
        data_type), so a Yahoo fundamentals 429 no longer blocks Yahoo prices.
        When data_type is None (legacy/unknown caller), falls back to
        provider-scope behavior: True if ANY bucket for this provider is
        cooling down — safe-side default (never wrongly admits a call)."""
        with self._lock:
            now = time.time()
            if data_type is None:
                # Provider-scope fallback: any active bucket → True.
                any_active = False
                for key in list(self._cooldown.keys()):
                    if not (isinstance(key, tuple) and key[0] == provider_id):
                        continue
                    cd = self._cooldown.get(key)
                    if not cd:
                        continue
                    if now >= cd.get('until', 0):
                        # Lazy-expire so trial calls can proceed next time.
                        self._cooldown.pop(key, None)
                        continue
                    any_active = True
                return any_active
            cd = self._cooldown.get((provider_id, data_type))
            if not cd:
                return False
            if now >= cd.get('until', 0):
                self._cooldown.pop((provider_id, data_type), None)
                return False
            return True

    def cooldown_remaining(self, provider_id: str,
                            data_type: Optional[str] = None) -> float:
        """Seconds left on the (provider, data_type) cooldown (0.0 if none).
        v4.14.6.56: data_type=None returns the MAX remaining across this
        provider's buckets — preserves old provider-scope semantics for legacy
        callers."""
        with self._lock:
            now = time.time()
            if data_type is None:
                remaining = 0.0
                for key, cd in self._cooldown.items():
                    if not (isinstance(key, tuple) and key[0] == provider_id):
                        continue
                    if not cd:
                        continue
                    r = cd.get('until', 0) - now
                    if r > remaining:
                        remaining = r
                return max(0.0, remaining)
            cd = self._cooldown.get((provider_id, data_type))
            if not cd:
                return 0.0
            return max(0.0, cd.get('until', 0) - now)

    def reliability_score(self, provider_id: str,
                           data_type: Optional[str] = None) -> float:
        """v4.14.6.60-source-reliability: per-(provider, data_type)
        reliability score in [0.0, 1.0], 1.0 = perfect.

        Formula (the user-approved, Phase 0):
            score = 1.0
                  − 0.8 × in_cooldown(provider, data_type)
                  − 0.5 × failure_rate(provider)
            clamp [0.0, 1.0]

        Where failure_rate = fails_today / max(1, calls_today), counted
        ONLY when calls_today >= 5 (avoids "1 fail out of 1 call = 100%"
        overpunishment on the first calls of the day).

        Cooldown is the PRIMARY per-type signal (clean, keyed exactly
        (provider, data_type) since v56). Failure rate is the secondary
        provider-scope tiebreaker (smaller weight to avoid bleed across
        unrelated data types). Health field intentionally NOT used —
        it's derived from the same failure counts; using it would
        double-count.

        Pure read; no mutation. Returns 1.0 (neutral) on any lookup
        miss or error, so a missing provider never starves a data type.
        """
        try:
            with self._lock:
                p = self._profiles.get(provider_id)
                if p is None:
                    return 1.0
                # Cooldown penalty (per-type, clean).
                cd_active = 0.0
                try:
                    cd = self._cooldown.get((provider_id, data_type))
                    if cd and time.time() < cd.get('until', 0):
                        cd_active = 1.0
                except Exception:
                    cd_active = 0.0
                # Failure-rate penalty (provider-scope, gated by sample).
                fail_rate = 0.0
                try:
                    calls = int(p.calls_today or 0)
                    fails = int(p.fails_today or 0)
                    if calls >= 5:
                        fail_rate = fails / max(1, calls)
                except Exception:
                    fail_rate = 0.0
                # v4.14.6.64-reliability-persist (S2): SORT-ONLY persisted
                # cooldown hint from last session. Applied ONLY when there is
                # no LIVE cooldown for this key (else cd_active already
                # dominates — avoid double-counting). Weight 0.4 (< live 0.8)
                # so a fresh observation easily overcomes it. Read from
                # _persisted_cd_hint, NEVER from eligibility — purely a sort
                # deprioritizer. Per-entry expiry re-checked here defensively.
                hint_active = 0.0
                try:
                    if cd_active == 0.0:
                        h = self._persisted_cd_hint.get((provider_id, data_type))
                        if h and time.time() < h.get('until', 0):
                            hint_active = 1.0
                except Exception:
                    hint_active = 0.0
                score = (1.0 - 0.8 * cd_active - 0.5 * fail_rate
                         - _RELIABILITY_HINT_WEIGHT * hint_active)
                if score < 0.0:
                    return 0.0
                if score > 1.0:
                    return 1.0
                return score
        except Exception:
            return 1.0

    def effective_limit(self, provider_id: str, key: str) -> int:
        """Returns the smaller of (declared, observed) for a given limit
        key. Observed wins when it's tighter. Returns 0 if neither set."""
        with self._lock:
            p = self._profiles.get(provider_id)
            if p is None:
                return 0
            declared = p.declared_limits.get(key, 0) or 0
            observed = p.observed_limits.get(key, 0) or 0
            if observed > 0 and declared > 0:
                return min(declared, observed)
            return observed or declared

    # ── Private helpers ───────────────────────────────────────────────

    def _roll_over_day_locked(self, p: ProviderProfile) -> None:
        """If we've crossed midnight, reset daily counters. Caller must
        hold self._lock."""
        today = _today_iso()
        if p.today_iso != today:
            p.calls_today = 0
            p.fails_today = 0
            p.today_iso = today


# ─── Module-level singleton ───────────────────────────────────────────

_registry: Optional[Registry] = None
_init_lock = threading.Lock()


def init(json_path: 'Path | None' = None) -> Registry:
    """Get-or-create the registry singleton. Loads from disk on first
    call. Subsequent calls return the same instance and ignore the path
    argument (the first wiring wins)."""
    global _registry
    with _init_lock:
        if _registry is None:
            _registry = Registry(json_path=json_path)
            _registry.load()
    return _registry


def get_registry() -> Optional[Registry]:
    return _registry


# ─── Helpers ──────────────────────────────────────────────────────────

def _today_iso() -> str:
    """Local-date YYYY-MM-DD. We use local for daily rollover so it
    matches the user's mental model — the user thinks of daily quota
    reset relative to their own day."""
    from datetime import datetime
    return datetime.now().strftime('%Y-%m-%d')
