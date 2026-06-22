"""tm_watch_tiers — per-tier owned-position WATCH parameters (v4.14.5.63).

SINGLE SOURCE OF TRUTH for how aggressively the owned-position watcher treats
a holding, by its tier (the holding's `path`, set in Build 1). Both the
scheduler's event detector (tm_scheduler._detect_for_ticker) and the prompt
builder (tm_holdings.build_holding_analysis) read THIS table, plus the cloud
on-event recency gate (tired_market._run_cloud_on_event_scan) — the numbers
live in exactly one place, so the user can tune any value here without hunting
through code. No literal thresholds/horizons are duplicated in the scheduler
or prompt builder; they all defer to this module.

Program-owned config: the program decides these; the AI only EXPLAINS them.
This table is not writable by the AI.

Fields per tier:
  display         — human label (matches PATHS display names)
  horizon_days    — the recover/grow window the owned-position question asks
  price_move_pct  — single-tick % move that fires a 'price_move' event
  drift_pct       — cumulative % drift that fires a 'drift' event
  recheck_min     — per-holding recency gate (minutes) for the cloud on-event
                    re-analysis: aggressive re-checks soonest, speculative
                    least often. Floored at the data-refresh cadence — the
                    detector re-checks news ~every 30 min and quotes on the
                    active 90s tick, so 30 min is the tightest honest gate
                    (we never promise resolution finer than the data delivers).

Tier keys are the canonical PATHS keys. 'penny_lottery' (the legacy /
written-off tag) ALIASES to 'lottery' (Speculative). This is consistent with
tm_holdings.get_path_track: lottery + penny_lottery == the 'speculative'
track == the single loose row here; the three 'main' tracks
(aggressive/moderate/slow_safe) fan out into their own rows.

All four tiers are WATCHED. Speculative is watched LOOSELY (wide thresholds,
long horizon, infrequent recheck) — it is deliberately NOT excluded from the
watcher.
"""

from __future__ import annotations

# Starting points — ALL tunable. One row per canonical tier.
WATCH_TIERS = {
    'aggressive': {
        'display': 'Aggressive',
        'horizon_days': 7,
        'price_move_pct': 2.5,
        'drift_pct': 4.0,
        'recheck_min': 30,    # tightest the existing refresh cadence supports
    },
    'moderate': {
        'display': 'Moderate',
        'horizon_days': 30,
        'price_move_pct': 4.0,
        'drift_pct': 7.0,
        'recheck_min': 120,
    },
    'slow_safe': {
        'display': 'Conservative',
        'horizon_days': 90,
        'price_move_pct': 6.0,
        'drift_pct': 10.0,
        'recheck_min': 360,
    },
    'lottery': {
        'display': 'Speculative',
        'horizon_days': 45,
        'price_move_pct': 10.0,
        'drift_pct': 15.0,
        'recheck_min': 720,   # watched, but loosely — least frequent
    },
}

# Fallback tier when a holding's path can't be resolved. The SAFE default is
# 'moderate' — NEVER 'lottery' (an unknown holding must not be downgraded to a
# throwaway speculative bet). Mirrors tm_holdings.DEFAULT_PATH.
DEFAULT_TIER = 'moderate'

# Legacy / alias path keys that fold into a canonical row.
_ALIASES = {'penny_lottery': 'lottery'}


def resolve_tier_key(path) -> str:
    """Normalize a holding's `path` to a canonical WATCH_TIERS key.

    Applies the penny_lottery→lottery alias; unknown/blank → DEFAULT_TIER
    ('moderate'), never lottery. Never raises."""
    try:
        key = str(path or '').strip()
        key = _ALIASES.get(key, key)
        return key if key in WATCH_TIERS else DEFAULT_TIER
    except Exception:
        return DEFAULT_TIER


def tier_params(path) -> dict:
    """Return the WATCH_TIERS row for a holding's `path` (resolved + aliased +
    safe-defaulted). Always returns a valid row; never raises."""
    try:
        return WATCH_TIERS[resolve_tier_key(path)]
    except Exception:
        return WATCH_TIERS[DEFAULT_TIER]
