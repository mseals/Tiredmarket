"""
Tired Market — Holdings analyzer and Signals view (Phase 2A).

The new direction: AI is the analyst. This module is where boss/qwen does
real work on real positions. The chat is a side feature; THIS is where
the value lives.

WHAT'S IN HERE:
    HoldingsManager   — the data layer (read/write portfolio.json)
    DataCacheLayer    — full-data caching with smart deltas (slow first
                        time, fast after)
    PromptBuilder     — assembles structured prompts with REAL data
                        (current price, position, sector, path, etc.)
    SignalsLog        — persistent record of every AI observation
    HoldingsWindow    — the UI; lives next to AI Chat in the header
                        - Holdings list with tradable/locked tags
                        - Per-holding "Re-check" button
                        - "Check Now" button that scans everything
                        - Signals feed showing AI observations over time

WHAT THIS DOESN'T DO YET (Phase 2B):
    - Background analysis on a market-hours-aware schedule
    - Event detection (price moves, news hits, earnings dates)
    - Game detection / auto-pause
    - Morning brief synthesizing overnight events
    - AI as stock picker (Phase 2C)

DESIGN NOTES:
    - All AI calls go through tm_ai (one place to change models)
    - Default model is "qwen" (smarter than boss for analysis)
    - Manual "Check Now" exists from day one; background tick comes in 2B
    - Frozen positions skipped during normal scans, but the scaffolding
      for once-daily checks is in place for 2B
    - Show-prompt-before-sending toggle for first-week debugging — let
      the user verify the AI is being given the right context
"""

from __future__ import annotations

import json
import os
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import ttk, messagebox
from typing import Any, Callable

# tm_ai (the Ollama client) import removed in the Ollama exit (Step 3b2):
# the only live consumers — the Holdings-window analyze and the Discover
# window — are retired/dead. The headless Scan/Discover engine is cloud-only.

# tm_discover is optional — Phase 2C predictions/track-record only get
# wired in if the module is present. Holdings still works without it.
try:
    import tm_discover
    _DISCOVER_AVAILABLE = True
except ImportError:
    tm_discover = None
    _DISCOVER_AVAILABLE = False


# ─── Configuration ─────────────────────────────────────────────────────

# Default model for holdings analysis. Qwen 14B reasons better than 8B
# models, and the 10-15s response time is fine for analysis.
DEFAULT_ANALYSIS_MODEL = "qwen"
# Fallback if qwen isn't installed; tries each in order
ANALYSIS_MODEL_FALLBACKS = ["qwen", "boss", "buddy"]

# How long to wait before re-scanning a holding when the user clicks
# "Check Now" — debounce to prevent spamming Ollama if the button is
# clicked rapidly
CHECK_NOW_DEBOUNCE_SEC = 3

# ─── AI pause/resume state ─────────────────────────────────────────────
# Module-level flag so the main app can pause the AI without needing
# a reference to the Holdings window. When True, all Check Now / Re-check
# attempts get refused with a small notice. The flag is loaded from config
# on launch and persisted on every change.
_AI_PAUSED = False
# v4.8.6: track WHY the AI was paused so subsystems can decide whether
# to auto-resume. Reason strings: 'user' (manual), 'ups' (power loss),
# 'gaming' (game detected), or '' (not paused).
_AI_PAUSE_REASON = ''


def is_ai_paused() -> bool:
    return _AI_PAUSED


def get_ai_pause_reason() -> str:
    """Returns why the AI is paused, or '' if not paused.
    Values: 'user', 'ups', 'gaming', or other custom strings."""
    return _AI_PAUSE_REASON


def set_ai_paused(paused: bool, reason: str = 'user') -> None:
    """Toggle the global AI pause state. Called by the main app's pause
    badge handler.

    The reason argument is informational — it lets subsystems decide
    whether they should auto-resume. For example, if power_monitor
    paused the AI for reason='ups' and then power is restored, it can
    auto-resume only if reason is still 'ups' (not if the user manually
    paused for their own reasons after the UPS event).
    """
    global _AI_PAUSED, _AI_PAUSE_REASON
    _AI_PAUSED = bool(paused)
    if paused:
        _AI_PAUSE_REASON = reason
    else:
        _AI_PAUSE_REASON = ''

# Theme — matches the rest of the app
THEME = {
    'bg':      '#080c14',
    'card':    '#0f1520',
    'card2':   '#161e2e',
    'border':  '#1c2536',
    'text':    '#cfd6e1',
    'muted':   '#7a8497',
    'dim':     '#4a5468',
    'accent':  '#7aa2f7',
    'green':   '#9ece6a',
    'amber':   '#e0af68',
    'red':     '#f7768e',
    'teal':    '#73daca',
    'blue':    '#7dcfff',
    'purple':  '#bb9af7',
}

# v4.14.6.0-price-band-tiers (2026-06-11): tiers revert from time-
# horizon paths back to share-price bands (the project's original
# framing). Each entry's `description` flows into the AI prompt as
# the tier framing — descriptions name the dollar range plainly so
# the AI evaluates picks within that band on their own merits and
# chooses each pick's TIMEFRAME based on the setup, not the tier.
# `display_label` is what the user sees on the UI (dollar range).
PATHS = {
    "lottery": {
        "name": "Under $5",
        "display_label": "Under $5",
        "description": (
            "Stocks priced under $5/share. Cheapest, thinnest, "
            "highest-noise names — speculative, most lose. A deliberate "
            "gamble; the wins can be big but reliable signal is rare. "
            "Calibrate framing to the actual price — a $4 name is not "
            "the same risk profile as a $0.50 name."),
    },
    "band_5_10": {
        "name": "$5–$10",
        "display_label": "$5–$10",
        "description": (
            "Stocks priced $5–$10/share. Low-priced names — more shares "
            "per dollar, often volatile. Includes small-caps with real "
            "businesses; quality and volume matter."),
    },
    "band_10_50": {
        "name": "$10–$50",
        "display_label": "$10–$50",
        "description": (
            "Stocks priced $10–$50/share. Mid-priced names with "
            "broader liquidity and analyst coverage; a sweet spot for "
            "swing setups and growth at reasonable price."),
    },
    "band_50_up": {
        "name": "$50+",
        "display_label": "$50+",
        "description": (
            "Stocks priced $50/share and above. Higher-priced, "
            "generally larger and more established. Includes mega-caps "
            "and quality compounders; positions cost more per share."),
    },
}
DEFAULT_PATH = "band_5_10"

# v4.14.6.0-price-band-tiers: legacy key compatibility. Old time-path
# names still appear in persisted state (predictions.jsonl, signals.jsonl,
# cfg) and in unedited path-keyed dicts across the codebase. Exposing
# them as PATHS aliases keeps lookups working until each site is
# explicitly re-keyed. The remap matches tm_path_candidate_pools.
_LEGACY_PATH_REMAP = {
    'aggressive':    'band_10_50',
    'moderate':      'band_10_50',
    'slow_safe':     'band_50_up',
    'penny_lottery': 'lottery',
}
# v4.14.6.1-price-band-cleanup (2026-06-11): the v4.14.6.0 build
# attached legacy keys INTO PATHS as aliases, which made PATHS.keys()
# return 8 entries — and every consumer that iterates PATHS.keys()
# (path rotation, cadence loop, recommend-cache _paths_for_refresh)
# dispatched against the legacy names too. That was the double-dispatch
# observed as old-key lines in the activity log. The aliases are now
# REMOVED so PATHS.keys() returns ONLY the four band keys.
# `_LEGACY_PATH_REMAP` itself + remap_legacy_path() STAY for
# resolution-on-access — a persisted analysis_path or a legacy-keyed
# historical prediction still resolves to the right band when looked
# up. The iteration side is what's fixed; the lookup side still works.


def remap_legacy_path(path: str) -> str:
    """Resolve a legacy time-path key to its price-band equivalent.
    Idempotent: a current key returns unchanged. Mirrors the same
    helper in tm_path_candidate_pools."""
    if not path:
        return path
    return _LEGACY_PATH_REMAP.get(path, path)

# v4.14.5.1 (Step 3): main vs speculative path classification. Data-
# driven from the accuracy reality check — main paths showed positive
# expected value historically (slow_safe/moderate strongly, aggressive
# ~breakeven); the speculative paths showed negative EV (lottery
# -3.5%/pick, penny_lottery -13.4% with zero winners in 7 decided).
# Both stay VISIBLE; speculative ones carry honest warnings. May be
# revisited once post-stability-fix data accumulates.
# v4.14.6.0-price-band-tiers: only the under-$5 lottery band is
# classified speculative now (cheap, thin, high-noise — the original
# meaning of "speculative"). All other price bands are main-track
# regardless of historical EV — the new tier model doesn't pre-judge
# price ranges, the picks within each band judge themselves on Track
# Record over time. Legacy keys still resolve via the alias loop below.
PATH_TRACK = {
    "lottery":     "speculative",
    "band_5_10":   "main",
    "band_10_50":  "main",
    "band_50_up":  "main",
}
# v4.14.6.1-price-band-cleanup: same alias-loop removal as PATHS.
# PATH_TRACK.keys() now returns four band keys only. get_path_track()
# (defined below) handles a legacy-key lookup via remap_legacy_path.


def get_path_track(path: str) -> str:
    """Return 'main' or 'speculative' for a path. Unknown/blank paths
    default to 'speculative' — fail safe toward showing a warning.
    v4.14.6.1-price-band-cleanup: resolve legacy time-path keys via
    remap_legacy_path() before the PATH_TRACK lookup, so callers
    holding a persisted 'aggressive' / 'moderate' / 'slow_safe' key
    still get the right track classification after the alias loop
    was removed."""
    p = (path or "").strip()
    return PATH_TRACK.get(remap_legacy_path(p), "speculative")


# v4.14.5.14-merge-and-unify — the user's articulated architectural
# simplification (see IDEAS.md 2026-05-19 "penny_lottery merges into
# lottery"). Lottery now MEANS "cheap speculation under $5" and the
# prompt reframes to honest weeks-of-sweating behaviour rather than
# the old days-of-resolution framing. The new lottery description is
# written into PATHS['lottery'] at startup by apply_path_merge…
# below, NOT statically, so flag-off rollback restores the verbatim
# pre-merge text. Keep this constant + the apply function as the
# SINGLE SOURCE for the merged description.
_LOTTERY_MERGED_DESCRIPTION = (
    "Cheap speculation, under $5. Hold and re-check over weeks "
    "while the position thrashes — most positions swing "
    "significantly and resolve over weeks, not days. EV is "
    "honestly negative on average; this is a user-accepted "
    "gamble. AI evaluates whether there is a genuine catalyst or "
    "thesis that could move the price meaningfully over the next "
    "2–6 weeks, not whether it will hit target in days. "
    # v4.14.5.55: name the root cause of the unpredictability — news on
    # names this cheap is rare, so the AI has little to anchor on.
    "There's rarely much news published on names this cheap, so there's "
    "no reliable way to be accurate here — that's the nature of the bet."
)


def apply_path_merge_v414514mu(cfg: dict | None = None) -> bool:
    """v4.14.5.14-merge-and-unify (2026-05-19): pop penny_lottery
    from PATHS + PATH_TRACK and rewrite lottery's description to the
    merged prompt — but only when cfg['use_path_merge'] is True
    (default True; the flag IS the rollback surface).

    Idempotent: re-runs detect the already-merged state via
    'penny_lottery' not in PATH_TRACK and no-op. Returns True only
    on the first successful application this process (so callers can
    fire the one-shot startup log line).

    Path-set rollback design: PATHS still carries penny_lottery as
    a source-of-truth fallback entry; popping it here is the single
    place to flip on the merge. Every other module mirrors the same
    pattern (tm_path_candidate_pools, tm_event_triggers,
    tired_market._RECO_STYLE_LABELS + path_fill_targets), keyed off
    the same cfg flag, so the merge is structurally one switch.
    """
    if not bool((cfg or {}).get('use_path_merge', True)):
        return False
    if 'penny_lottery' not in PATH_TRACK:
        return False  # already merged this process — idempotent
    PATHS.pop('penny_lottery', None)
    PATH_TRACK.pop('penny_lottery', None)
    try:
        if 'lottery' in PATHS:
            PATHS['lottery']['description'] = (
                _LOTTERY_MERGED_DESCRIPTION)
            # v4.14.5.14-stale-cleanup: also normalize the display name to
            # the user-facing "Speculative" (the source name is already
            # "Speculative"; defensive if the source ever drifts).
            PATHS['lottery']['name'] = 'Speculative'
    except Exception:
        pass
    return True


def compute_path_track_stats(predictions_path=None,
                             cutoff_ts: float | None = None) -> dict:
    """v4.14.5.2: single source of truth for per-path honest accuracy
    numbers. Used by the Recommend speculative banner, the Track Record
    overhaul, and the audit — so all three show identical figures.

    Reads predictions.jsonl directly (full + delta records merged),
    filters to BUY predictions, and for each path computes the
    market-decided cohort (status target_hit / stop_hit only —
    superseded/contradicted/expired/open are NOT decided and excluded
    from rates, matching the accuracy methodology).

    cutoff_ts: if given, only predictions whose parsed timestamp is at
    or after this unix time are counted (the post-stability-fix view).
    None = all history.

    Returns {path: {total, decided, target, stop, hit_rate_pct,
    avg_rr, avg_winner_pct, avg_loser_pct, ev_pct, thin}} where
    `thin` is True when decided < 30 (directional only). Never raises;
    returns {} on read failure.
    """
    import json as _json
    from pathlib import Path as _Path
    from datetime import datetime as _dtm

    if predictions_path is None:
        predictions_path = _Path(__file__).parent / "data" / "predictions.jsonl"
    predictions_path = _Path(predictions_path)
    if not predictions_path.exists():
        return {}

    merged: dict = {}
    try:
        with open(predictions_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = _json.loads(line)
                except Exception:
                    continue
                if o.get("_d") == 1:
                    rid = o.get("id")
                    if rid in merged:
                        merged[rid].update(o.get("patch") or {})
                    continue
                rid = o.get("id")
                if rid is not None:
                    merged[rid] = o
    except Exception:
        return {}

    def _ts_ok(rec) -> bool:
        if cutoff_ts is None:
            return True
        ts = rec.get("timestamp") or ""
        try:
            return _dtm.fromisoformat(ts).timestamp() >= cutoff_ts
        except Exception:
            return False

    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # v4.14.5.14-merge-and-unify (2026-05-19): when the path-merge
    # has been applied this process, historical penny_lottery rows
    # roll up into lottery (the IDEAS.md "keep-as-penny_lottery
    # with footnote rollup" approach — predictions.jsonl entries
    # are preserved verbatim; only the bucket they aggregate into
    # changes). Detection uses the merge's own SIDE-EFFECT — if
    # 'penny_lottery' is no longer in PATH_TRACK then
    # apply_path_merge_v414514mu has run, so no separate cfg
    # plumbing is needed in this aggregation loop. Flag-off →
    # PATH_TRACK still has 'penny_lottery' → no rollup (legacy
    # bucket separation preserved).
    _merge_active = ('penny_lottery' not in PATH_TRACK)
    out: dict = {}
    for r in merged.values():
        if (r.get("direction") or "").upper() != "BUY":
            continue
        if not _ts_ok(r):
            continue
        p = (r.get("path") or "?").strip() or "?"
        if _merge_active and p == 'penny_lottery':
            p = 'lottery'
        d = out.setdefault(p, {
            "total": 0, "decided": 0, "target": 0, "stop": 0,
            "_rr": [], "_win": [], "_los": []})
        d["total"] += 1
        st = r.get("status")
        if st not in ("target_hit", "stop_hit"):
            continue
        d["decided"] += 1
        lo = _f(r.get("buy_zone_low"))
        hi = _f(r.get("buy_zone_high"))
        entry = ((lo + hi) / 2.0 if (lo and hi)
                 else _f(r.get("current_price_at_prediction")))
        tgt = _f(r.get("target"))
        stp = _f(r.get("stop"))
        if entry and tgt and stp and entry > 0 and entry > stp and tgt > entry:
            d["_rr"].append((tgt - entry) / (entry - stp))
            if st == "target_hit":
                d["target"] += 1
                d["_win"].append(100.0 * (tgt - entry) / entry)
            else:
                d["stop"] += 1
                d["_los"].append(100.0 * (entry - stp) / entry)
        else:
            # still counts as decided for hit-rate even if R:R unparseable
            if st == "target_hit":
                d["target"] += 1
            else:
                d["stop"] += 1

    result: dict = {}
    for p, d in out.items():
        dec = d["decided"]
        tgt = d["target"]
        stp = d["stop"]
        rr = d["_rr"]
        win = d["_win"]
        los = d["_los"]
        hit_rate = (100.0 * tgt / (tgt + stp)) if (tgt + stp) else None
        avg_w = (sum(win) / len(win)) if win else 0.0
        avg_l = (sum(los) / len(los)) if los else 0.0
        if (tgt + stp) > 0:
            hr = tgt / (tgt + stp)
            ev = hr * avg_w - (1.0 - hr) * avg_l
        else:
            ev = None
        result[p] = {
            "total": d["total"], "decided": dec,
            "target": tgt, "stop": stp,
            "hit_rate_pct": hit_rate,
            "avg_rr": (sum(rr) / len(rr)) if rr else None,
            "avg_winner_pct": avg_w if win else None,
            "avg_loser_pct": avg_l if los else None,
            "ev_pct": ev,
            "thin": dec < 30,
        }
    return result


def _now_iso() -> str:
    return datetime.now().isoformat(timespec='seconds')


def _format_money(amount: float) -> str:
    if abs(amount) < 0.01:
        return f"${amount:.4f}"
    if abs(amount) < 1:
        return f"${amount:.4f}"
    return f"${amount:,.2f}"


# ════════════════════════════════════════════════════════════════════════
# DATA LAYER — HoldingsManager owns portfolio.json
# ════════════════════════════════════════════════════════════════════════

class HoldingsManager:
    """Owns the portfolio.json file. Adds per-holding fields needed for
    Phase 2A analysis (tradable flag, last_analyzed timestamp, locked-only
    notes). Maintains backward compat with the existing schema.

    SCHEMA ADDITIONS (Phase 2A):
        Each holding gets:
            tradable: bool         — true = normal cadence, false = once-a-day
            last_analyzed: str|None — ISO timestamp of last AI analysis
            notes: str             — user notes (optional)

    The legacy fields (ticker, shares, buy_price, total_cost, etc.) stay
    untouched.
    """

    def __init__(self, portfolio_path: Path):
        self.portfolio_path = Path(portfolio_path)
        self._lock = threading.Lock()
        self.data = self._load()

    def _load(self) -> dict:
        if self.portfolio_path.exists():
            try:
                with open(self.portfolio_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                # Schema migration: add new fields if missing
                for h in data.get('holdings', []):
                    h.setdefault('tradable', True)
                    h.setdefault('last_analyzed', None)
                    h.setdefault('notes', '')
                    # v4.13.2: status field (tradable / locked / written_off)
                    if 'status' not in h:
                        # Migrate from legacy `tradable` boolean.
                        h['status'] = 'tradable' if h.get('tradable', True) else 'locked'
                    # v4.13.2: per-holding path (None = use global default)
                    h.setdefault('path', None)
                    # v4.13.2: free-form reason for locked/written_off
                    h.setdefault('lock_reason', '')
                    # Keep `tradable` mirror in sync with `status` so legacy
                    # code paths that read `tradable` keep working.
                    h['tradable'] = (h['status'] == 'tradable')
                # v4.13.35: top-level cash field. Migrates legacy
                # portfolios that have no cash key by setting it to 0.
                # User sets actual amount via the "Set cash" button.
                data.setdefault('cash', 0.0)
                return data
            except Exception:
                pass
        return {
            "holdings": [],
            "closed": [],
            "cash": 0.0,
            "total_invested": 0.0,
            "total_pnl": 0.0,
        }

    def save(self) -> bool:
        """v4.14.5.14-portfolio-atomic-save: ATOMIC, error-reporting save.

        Pre-fix this wrote directly over portfolio.json and swallowed every
        error — so a Sell / Set-cash / Add whose write failed (disk full,
        file locked, permissions) reported success but never persisted (gone
        on restart), and a crash mid-write could corrupt the whole file.

        Now: write to a sibling .tmp, flush + fsync (force the OS to commit
        to disk), then os.replace (atomic rename) over the real file — so the
        existing file is never touched until the new bytes are safely on
        disk. Returns True on success, False on failure (the temp is cleaned
        up). NEVER raises — the ~11 existing callers (incl. post-consensus
        mark_analyzed and the legacy HoldingsWindow) keep working unchanged;
        the user-facing Portfolio actions check the bool and surface a modal
        + roll back in-memory state (tm_portfolio_panel._handle_save_failure).
        Byte-identical output to the old json.dump(indent=2, default=str)."""
        with self._lock:
            tmp_path = self.portfolio_path.with_name(
                self.portfolio_path.name + '.tmp')
            try:
                self.portfolio_path.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    json.dump(self.data, f, indent=2, default=str)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self.portfolio_path)
                return True
            except Exception as e:
                # Best-effort temp cleanup; original file is untouched.
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass
                print(f"[portfolio] save failed (data NOT persisted): {e}")
                return False

    def reload(self) -> None:
        """v4.14.5.14-portfolio-atomic-save: re-read portfolio.json from
        disk, discarding any in-memory mutation that failed to persist.
        Used by the panel's save-failure rollback so the UI matches the
        (atomic-save-untouched) on-disk file."""
        with self._lock:
            self.data = self._load()

    @property
    def holdings(self) -> list[dict]:
        return self.data.get('holdings', [])

    def get_cash(self) -> float:
        """v4.13.35: return current cash amount."""
        try:
            return float(self.data.get('cash', 0.0) or 0.0)
        except (ValueError, TypeError):
            return 0.0

    def set_cash(self, amount: float) -> bool:
        """v4.13.35: update cash amount and persist immediately.
        v4.14.5.14-portfolio-atomic-save: returns save() success so the
        caller can surface a save failure (was None / always 'succeeded')."""
        with self._lock:
            try:
                self.data['cash'] = float(amount)
            except (ValueError, TypeError):
                self.data['cash'] = 0.0
        return self.save()

    def add_holding(self, ticker: str, shares: float, buy_price: float,
                    tradable: bool = True, buy_date: str | None = None,
                    notes: str = "",
                    status: str | None = None,
                    path: str | None = None,
                    lock_reason: str = "",
                    deduct_cash: bool = True) -> dict:
        """Add a new holding (or update an existing one).

        v4.13.57: cash accounting. By default this DEDUCTS the purchase
        cost from cash (shares × buy_price). Pass deduct_cash=False to
        suppress that — useful when back-filling an existing position
        that you already paid for at some earlier time.

        For UPDATES (ticker already exists), no cash adjustment happens
        regardless of deduct_cash — adjusting cash on an "edit" would
        double-count. Use sell+add if you really want to net a re-buy.

        Returns:
            dict {
                'created': bool,           # True if new, False if updated
                'cash_change': float,      # negative if cash deducted
                'cash_after': float,       # cash balance after this call
            }
        """
        ticker = ticker.strip().upper()
        result = {'created': False, 'cash_change': 0.0,
                   'cash_after': self.get_cash()}
        with self._lock:
            # If ticker already exists, update it instead of adding twice
            for h in self.data['holdings']:
                if h.get('ticker', '').upper() == ticker:
                    h['shares'] = float(shares)
                    h['buy_price'] = float(buy_price)
                    h['total_cost'] = float(shares) * float(buy_price)
                    h['tradable'] = bool(tradable)
                    # v4.13.2: status takes precedence; tradable is a mirror
                    if status is not None:
                        h['status'] = status
                        h['tradable'] = (status == 'tradable')
                    else:
                        h.setdefault('status',
                                     'tradable' if tradable else 'locked')
                    if path is not None:
                        h['path'] = path
                    else:
                        h.setdefault('path', None)
                    if lock_reason:
                        h['lock_reason'] = lock_reason
                    else:
                        h.setdefault('lock_reason', '')
                    if buy_date:
                        h['buy_date'] = buy_date
                    if notes:
                        h['notes'] = notes
                    self._recalc_totals()
                    # No cash adjustment on update — see docstring
                    result['cash_after'] = float(
                        self.data.get('cash', 0.0) or 0.0)
                    return result
            # New holding
            resolved_status = status or ('tradable' if tradable else 'locked')
            new = {
                'ticker': ticker,
                'shares': float(shares),
                'buy_price': float(buy_price),
                'total_cost': float(shares) * float(buy_price),
                'buy_date': buy_date or _now_iso(),
                'source': 'manual_entry',
                'tradable': (resolved_status == 'tradable'),
                'status': resolved_status,            # v4.13.2
                'path': path,                         # v4.13.2 (None = global)
                'lock_reason': lock_reason,           # v4.13.2
                'last_analyzed': None,
                'notes': notes,
            }
            self.data['holdings'].append(new)
            self._recalc_totals()
            result['created'] = True

            # ── v4.13.57: deduct purchase cost from cash ──────────────
            # Only on NEW holdings (not updates) and only if caller asked.
            # Default is True — clicking Add in the UI means "I bought
            # this, take it out of my cash." If you're back-filling an
            # existing position whose cash was already spent at some
            # earlier time, pass deduct_cash=False.
            if deduct_cash:
                cur_cash = float(self.data.get('cash', 0.0) or 0.0)
                cost = float(shares) * float(buy_price)
                self.data['cash'] = cur_cash - cost
                result['cash_change'] = -cost
                result['cash_after'] = self.data['cash']
            else:
                result['cash_after'] = float(
                    self.data.get('cash', 0.0) or 0.0)
        # v4.14.4.3 (2026-05-15): user-signal trigger on NEW positions
        # only. result['created'] is True only on the create-path
        # above (the update-path exits earlier via the inner
        # `return result` at line ~331 inside the with-block).
        # Re-sizing an existing position isn't the same fresh-entry
        # intent. Fires on the holding's path when set, else falls
        # back to cfg['analysis_path'] inside record_user_signal.
        # Failure non-fatal — portfolio writes must not break here.
        if result.get('created'):
            try:
                import tm_event_triggers as _tet
                _tet.record_user_signal(
                    ticker, 'position_open',
                    path=(path if isinstance(path, str) and path
                          else None),
                    context_str=(notes.strip()[:80] if notes
                                  else "user opened new position"))
            except Exception:
                pass
        return result

    def remove_holding(self, ticker: str) -> bool:
        ticker = ticker.strip().upper()
        with self._lock:
            before = len(self.data['holdings'])
            self.data['holdings'] = [
                h for h in self.data['holdings']
                if h.get('ticker', '').upper() != ticker
            ]
            if len(self.data['holdings']) < before:
                self._recalc_totals()
                return True
            return False

    def sell_holding(self, ticker: str, sell_price: float,
                     sell_date: str | None = None,
                     notes: str = "",
                     credit_cash: bool = True) -> dict | None:
        """Mark a holding as sold. Moves it to the 'closed' list with the
        full P&L computed.

        v4.13.57: cash accounting. By default this CREDITS the sale
        proceeds to cash (shares × sell_price). Pass credit_cash=False
        to suppress that — useful when recording an old sale that
        already happened in the real world and whose proceeds are
        already in your cash balance.

        Returns the closed-position record (dict) — also includes
        'cash_change' and 'cash_after' fields. Returns None if the
        ticker wasn't found.

        Important: this is what gets called after a GTC sell order
        actually fills. It's how we build the realized track record over
        time — every closed position has buy/sell/P&L/days-held captured.
        """
        ticker = ticker.strip().upper()
        sell_date = sell_date or _now_iso()
        try:
            sell_price = float(sell_price)
        except (TypeError, ValueError):
            return None

        with self._lock:
            target = None
            for h in self.data['holdings']:
                if h.get('ticker', '').upper() == ticker:
                    target = h
                    break
            if target is None:
                return None

            shares = float(target.get('shares', 0))
            buy_price = float(target.get('buy_price', 0))
            buy_date_str = target.get('buy_date', '')
            total_cost = float(target.get('total_cost', shares * buy_price))
            total_proceeds = shares * sell_price
            pnl_dollars = total_proceeds - total_cost
            pnl_pct = (pnl_dollars / total_cost * 100) if total_cost else 0.0

            days_held = None
            try:
                buy_dt = datetime.fromisoformat(buy_date_str)
                sell_dt = datetime.fromisoformat(sell_date)
                days_held = max(0, (sell_dt - buy_dt).days)
            except Exception:
                days_held = None

            closed_entry = {
                'ticker': ticker,
                'shares': shares,
                'buy_price': buy_price,
                'buy_date': buy_date_str,
                'sell_price': float(sell_price),
                'sell_date': sell_date,
                'total_cost': total_cost,
                'total_proceeds': total_proceeds,
                'pnl_dollars': pnl_dollars,
                'pnl_pct': pnl_pct,
                'days_held': days_held,
                'notes': (notes or target.get('notes', '')).strip(),
                'closed_at': _now_iso(),
            }

            # Remove from active holdings
            self.data['holdings'] = [
                h for h in self.data['holdings']
                if h.get('ticker', '').upper() != ticker
            ]
            # Append to closed
            self.data.setdefault('closed', []).append(closed_entry)

            # Update aggregate stats
            self._recalc_totals()
            total_pnl = sum(c.get('pnl_dollars', 0)
                             for c in self.data.get('closed', []))
            self.data['total_pnl'] = float(total_pnl)

            # ── v4.13.57: credit sale proceeds to cash ───────────────
            # Default True: selling means proceeds land in cash.
            # Pass credit_cash=False if back-filling an old sale whose
            # proceeds are already reflected elsewhere.
            if credit_cash:
                cur_cash = float(self.data.get('cash', 0.0) or 0.0)
                self.data['cash'] = cur_cash + total_proceeds
                closed_entry['cash_change'] = total_proceeds
                closed_entry['cash_after'] = self.data['cash']
            else:
                closed_entry['cash_change'] = 0.0
                closed_entry['cash_after'] = float(
                    self.data.get('cash', 0.0) or 0.0)

            return closed_entry

    def get_closed(self) -> list[dict]:
        """Return the list of closed (sold) positions, newest-first."""
        closed = self.data.get('closed', [])
        # Sort newest-first by closed_at if available
        try:
            return sorted(closed, key=lambda c: c.get('closed_at', c.get('sell_date', '')),
                          reverse=True)
        except Exception:
            return list(closed)

    def recently_sold_tickers(self, within_days: int = 7) -> set[str]:
        """v4.13.28: Return the set of tickers sold in the last
        within_days days. Used by the recommendation engine to
        avoid suggesting you buy back what you just sold.
        """
        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(days=max(0, int(within_days)))
        out: set[str] = set()
        for c in self.data.get('closed', []) or []:
            ticker = (c.get('ticker') or '').strip().upper()
            if not ticker:
                continue
            ts_str = c.get('closed_at') or c.get('sell_date') or ''
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
            except Exception:
                continue
            if ts >= cutoff:
                out.add(ticker)
        return out

    def get_realized_stats(self) -> dict:
        """Aggregate stats over all closed trades."""
        closed = self.data.get('closed', [])
        if not closed:
            return {'count': 0, 'wins': 0, 'losses': 0, 'flat': 0,
                    'total_pnl': 0.0, 'total_pnl_pct': 0.0,
                    'win_rate': 0.0,
                    'avg_win_pct': 0.0, 'avg_loss_pct': 0.0,
                    'best_trade': None, 'worst_trade': None}

        wins = [c for c in closed if c.get('pnl_dollars', 0) > 0]
        losses = [c for c in closed if c.get('pnl_dollars', 0) < 0]
        flat = [c for c in closed
                if c.get('pnl_dollars', 0) == 0]
        total_cost = sum(c.get('total_cost', 0) for c in closed)
        total_pnl = sum(c.get('pnl_dollars', 0) for c in closed)
        total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0.0
        avg_win = (sum(w.get('pnl_pct', 0) for w in wins) / len(wins)) if wins else 0.0
        avg_loss = (sum(l.get('pnl_pct', 0) for l in losses) / len(losses)) if losses else 0.0
        best = max(closed, key=lambda c: c.get('pnl_pct', float('-inf'))) if closed else None
        worst = min(closed, key=lambda c: c.get('pnl_pct', float('inf'))) if closed else None
        return {
            'count': len(closed),
            'wins': len(wins),
            'losses': len(losses),
            'flat': len(flat),
            'total_pnl': total_pnl,
            'total_pnl_pct': total_pnl_pct,
            'win_rate': (len(wins) / len(closed) * 100) if closed else 0.0,
            'avg_win_pct': avg_win,
            'avg_loss_pct': avg_loss,
            'best_trade': best,
            'worst_trade': worst,
        }

    def update_tradable(self, ticker: str, tradable: bool) -> None:
        ticker = ticker.strip().upper()
        with self._lock:
            for h in self.data['holdings']:
                if h.get('ticker', '').upper() == ticker:
                    h['tradable'] = bool(tradable)
                    return

    def mark_analyzed(self, ticker: str) -> None:
        ticker = ticker.strip().upper()
        with self._lock:
            for h in self.data['holdings']:
                if h.get('ticker', '').upper() == ticker:
                    h['last_analyzed'] = _now_iso()
                    return

    # v4.14.5.64-owned-position-surfacing: persistent per-holding alert
    # state. The cloud on-event watcher writes the latest classified
    # alert here (see tm_owned_alert.classify); the Portfolio panel
    # reads it to render a durable badge above the existing state row.
    # Routine results CLEAR the alert. The alert is "what the watcher
    # last reported" - the program decides; the AI only explains.
    def set_owned_alert(self, ticker: str, alert) -> None:
        """Write or clear the watcher's per-holding alert dict. Pass
        None to clear. Caller is responsible for save()."""
        ticker = ticker.strip().upper()
        with self._lock:
            for h in self.data['holdings']:
                if h.get('ticker', '').upper() == ticker:
                    if alert is None:
                        h.pop('_owned_alert', None)
                    else:
                        h['_owned_alert'] = dict(alert)
                        h['_owned_alert']['ts'] = _now_iso()
                    return

    def clear_owned_alert(self, ticker: str) -> None:
        """Acknowledge / clear the watcher's alert badge."""
        self.set_owned_alert(ticker, None)

    def set_last_verdict(self, ticker: str, verdict: str) -> None:
        """Record the consensus winning direction so the next watcher
        pass can detect a verdict change. Caller is responsible for
        save()."""
        ticker = ticker.strip().upper()
        v = str(verdict or '').strip().upper()
        with self._lock:
            for h in self.data['holdings']:
                if h.get('ticker', '').upper() == ticker:
                    if v:
                        h['_last_verdict'] = v
                    return

    def _recalc_totals(self) -> None:
        # v4.13.2: total_invested now means *active* capital only —
        # excludes written_off positions whose money is effectively gone.
        # We keep total_written_off and total_invested_legacy for any
        # legacy code that needs the old number.
        active = sum(h.get('total_cost', 0.0)
                     for h in self.data['holdings']
                     if h.get('status', 'tradable') != 'written_off')
        written_off = sum(h.get('total_cost', 0.0)
                          for h in self.data['holdings']
                          if h.get('status') == 'written_off')
        self.data['total_invested'] = float(active)
        self.data['total_written_off'] = float(written_off)
        self.data['total_invested_legacy'] = float(active + written_off)


# ════════════════════════════════════════════════════════════════════════
# DATA CACHE LAYER — full data first time, deltas after
# ════════════════════════════════════════════════════════════════════════

class DataCacheLayer:
    """Caches the result of expensive data fetches (Yahoo quote, history,
    news, technicals) so we don't re-fetch unchanged data on every analysis.

    Design:
        - First time we ask about a ticker, fetch everything fresh (slow)
        - On subsequent asks, return cached data unless TTL has expired
        - Different TTLs for different data:
            quote: 60 seconds (changes constantly during market hours)
            history: 4 hours (changes once a day for completed bars)
            news: 30 minutes (new articles arrive periodically)
            technicals: 4 hours (computed from history)

    v4.10.10: Disk persistence + stale-while-revalidate.
        - On init, loads any prior cache from disk so the first window
          open after launch is INSTANT (no Yahoo round-trips for known
          tickers).
        - When a cached entry is past its in-memory TTL but still under
          the disk-keep age, returns the stale value immediately and
          triggers a background refresh. UI never freezes waiting for
          Yahoo.
        - Saves to disk periodically and on shutdown.
        - History/technicals are NOT persisted (too big — pandas frames
          and full indicator dicts). Only quotes + news_features get
          disk storage where the savings are biggest.
    """

    # TTLs per data type (seconds)
    TTL_QUOTE = 60
    TTL_HISTORY = 4 * 3600
    TTL_NEWS = 30 * 60
    TTL_TECHNICALS = 4 * 3600
    # v4.14.1 stage 2: fundamentals + filings extend the cache surface.
    # Fundamentals get a long TTL because P/E and market cap drift
    # slowly and Finnhub's 60/min budget is the binding constraint
    # across multi-ticker scans. Filings refresh daily-ish; intraday
    # refetches aren't useful since 8-Ks file at low frequency.
    TTL_FUNDAMENTALS = 6 * 3600                 # 6 h memory cache for fundamentals
    TTL_FILINGS = 4 * 3600                      # 4 h memory cache for SEC filings
    # v4.14.2 stage 4: macro is global (no ticker), changes daily at
    # fastest (most series are weekly / monthly). 12-hour memory cache
    # is the right grain — fresh enough for once-daily users, doesn't
    # refetch on every prompt build.
    TTL_MACRO = 12 * 3600
    # v4.14.2 stage 5: social moves fast (catalyst posts, sentiment
    # shifts intraday) — but not so fast we burn rate budget. 30-min
    # memory cache balances freshness against StockTwits' polite-use
    # ceiling.
    TTL_SOCIAL = 30 * 60

    # v4.10.10: how long a disk-persisted entry is acceptable to serve
    # stale-while-revalidate. Past this, force a fresh sync fetch.
    MAX_DISK_AGE_QUOTE = 24 * 3600  # 24h: serve yesterday's price while we refresh
    MAX_DISK_AGE_NEWS = 6 * 3600    # 6h: news goes stale fast
    MAX_DISK_AGE_FUNDAMENTALS = 7 * 24 * 3600   # 7-day SWR cap on disk
    MAX_DISK_AGE_FILINGS = 24 * 3600            # 24-hour SWR cap on disk
    MAX_DISK_AGE_MACRO = 7 * 24 * 3600          # 7-day SWR cap (v4.14.2 stage 4)
    MAX_DISK_AGE_SOCIAL = 24 * 3600             # 24-hour SWR cap (v4.14.2 stage 5)

    # Which kinds get persisted to disk (others stay memory-only)
    PERSISTED_KINDS = {'quote', 'news_features',
                        'fundamentals', 'filings',
                        'macro',                # v4.14.2 stage 4
                        'social'}               # v4.14.2 stage 5

    def __init__(self, fetch_fns: dict[str, Callable],
                 disk_cache_path: 'Path | None' = None):
        """
        Args:
            fetch_fns: dict mapping data type to a fetcher function. Caller
                injects the actual Tired Market data functions (yahoo_quote,
                yahoo_history, etc.) so we don't hardcode dependencies.
                Keys: 'quote', 'history', 'news_features', 'technicals',
                      'market_status'
            disk_cache_path: optional Path to a JSON file used for
                cross-launch persistence of quotes + news. None = memory only.
        """
        self.fetch_fns = fetch_fns
        self._cache: dict[tuple[str, str], tuple[Any, float]] = {}
        self._lock = threading.Lock()
        # v4.10.10
        self._disk_path = disk_cache_path
        self._dirty = False  # flag: in-memory state has unsaved changes
        self._refreshing: set[tuple[str, str]] = set()  # in-flight bg refreshes
        self._load_disk()

    # ─── Disk persistence (v4.10.10) ───

    @classmethod
    def _max_disk_age_for(cls, kind: str) -> int:
        """v4.14.1 stage 2: per-kind SWR ceiling lookup.

        Returns the maximum disk-age (seconds) for which a cached
        entry of `kind` is still acceptable to serve under stale-
        while-revalidate. Pre-stage-2 the cache had only two
        persisted kinds and used an inline ternary; with four
        persisted kinds the lookup deserves its own helper.
        """
        if kind == 'quote':
            return cls.MAX_DISK_AGE_QUOTE
        if kind == 'news_features':
            return cls.MAX_DISK_AGE_NEWS
        if kind == 'fundamentals':
            return cls.MAX_DISK_AGE_FUNDAMENTALS
        if kind == 'filings':
            return cls.MAX_DISK_AGE_FILINGS
        if kind == 'macro':                     # v4.14.2 stage 4
            return cls.MAX_DISK_AGE_MACRO
        if kind == 'social':                    # v4.14.2 stage 5
            return cls.MAX_DISK_AGE_SOCIAL
        # Unknown kind — fall back to the shortest ceiling so a stray
        # persisted entry doesn't outlive its useful window.
        return cls.MAX_DISK_AGE_NEWS

    def _load_disk(self) -> None:
        """Load persisted entries from disk into the in-memory cache."""
        if self._disk_path is None or not self._disk_path.exists():
            return
        try:
            with open(self._disk_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            return
        # data format: {"entries": [{"ticker", "kind", "value", "ts"}, ...]}
        now = time.time()
        loaded = 0
        for e in data.get('entries', []):
            try:
                ticker = e['ticker']
                kind = e['kind']
                value = e['value']
                ts = float(e['ts'])
            except (KeyError, ValueError, TypeError):
                continue
            if kind not in self.PERSISTED_KINDS:
                continue
            # Skip extremely stale entries that wouldn't be useful even
            # for stale-while-revalidate
            max_age = self._max_disk_age_for(kind)
            if now - ts > max_age:
                continue
            with self._lock:
                self._cache[(ticker.upper(), kind)] = (value, ts)
            loaded += 1
        # Mark clean — loading from disk shouldn't trigger a save
        self._dirty = False

    def save_disk(self) -> bool:
        """Write persisted entries to disk. Returns True on success.

        Called periodically and on shutdown. Non-atomic (writes to a temp
        file then renames) to avoid leaving a corrupt cache file if we
        crash mid-write.
        """
        if self._disk_path is None or not self._dirty:
            return False
        # Snapshot the cache under lock, then write outside the lock
        with self._lock:
            entries = []
            for (ticker, kind), (value, ts) in self._cache.items():
                if kind not in self.PERSISTED_KINDS:
                    continue
                # Skip non-JSON-serializable values (e.g. pandas frames
                # somehow ended up here). Quotes and news features are
                # plain dicts so this should always succeed.
                try:
                    json.dumps(value)
                except (TypeError, ValueError):
                    continue
                entries.append({
                    'ticker': ticker, 'kind': kind,
                    'value': value, 'ts': ts,
                })
            self._dirty = False
        try:
            self._disk_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._disk_path.with_suffix('.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'entries': entries,
                           'saved_at': datetime.now().isoformat()}, f)
            tmp.replace(self._disk_path)
            return True
        except Exception:
            # Phase 2: surface to Teacher AI so the user knows the
            # cache won't persist across restart. Uses the module
            # registry — DataCacheLayer doesn't carry an app handle.
            try:
                import tm_teacher_intercept as _tm_ic
                _tm_ic.emit_system_event('cache_disk_write_failed')
            except Exception:
                pass
            return False

    def _trigger_bg_refresh(self, ticker: str, kind: str) -> None:
        """Spawn a background thread that refreshes one cache entry.
        Used by stale-while-revalidate. No-op if a refresh for this
        (ticker, kind) is already in flight."""
        key = (ticker.upper(), kind)
        with self._lock:
            if key in self._refreshing:
                return
            self._refreshing.add(key)
        fetch_fn = self.fetch_fns.get(kind)
        if fetch_fn is None:
            with self._lock:
                self._refreshing.discard(key)
            return

        def _do_refresh():
            try:
                value = fetch_fn(ticker)
            except Exception:
                value = None
            with self._lock:
                if value is not None:
                    self._cache[key] = (value, time.time())
                    if kind in self.PERSISTED_KINDS:
                        self._dirty = True
                self._refreshing.discard(key)

        threading.Thread(target=_do_refresh, daemon=True,
                          name=f'cache-refresh-{ticker}-{kind}').start()

    def _get(self, ticker: str, kind: str, ttl: int) -> Any:
        """Return cached value if fresh, else fetch and cache.

        v4.10.10: stale-while-revalidate for persisted kinds. If we have
        a stale entry under MAX_DISK_AGE, return it immediately and
        trigger a background refresh.
        """
        key = (ticker.upper(), kind)
        now = time.time()
        with self._lock:
            entry = self._cache.get(key)

        if entry is not None:
            value, ts = entry
            age = now - ts
            if age < ttl:
                # Fresh — return as-is
                return value
            # Stale. For persisted kinds, serve stale + bg refresh.
            if kind in self.PERSISTED_KINDS:
                max_age = self._max_disk_age_for(kind)
                if age < max_age:
                    self._trigger_bg_refresh(ticker, kind)
                    return value
            # else fall through to sync fetch

        # Fetch fresh (sync)
        fetch_fn = self.fetch_fns.get(kind)
        if fetch_fn is None:
            return None
        try:
            value = fetch_fn(ticker)
        except Exception:
            value = None
        with self._lock:
            self._cache[key] = (value, now)
            if kind in self.PERSISTED_KINDS and value is not None:
                self._dirty = True
        return value

    def quote(self, ticker: str) -> Any:
        return self._get(ticker, 'quote', self.TTL_QUOTE)

    def peek_quote(self, ticker: str) -> bool:
        """Returns True if this ticker's quote is currently in the
        cache and fresh (won't trigger a sync fetch). No side effects:
        does not fetch, does not trigger background refresh. Used by
        Teacher AI's intercept layer to set the 'on_demand_fetch'
        flag on Look Up's observation result dict — letting the
        ticker_not_in_cache observation fire when a Look Up had to
        wait for fresh data."""
        key = (ticker.upper(), 'quote')
        with self._lock:
            entry = self._cache.get(key)
        if entry is None:
            return False
        value, ts = entry
        return (time.time() - ts) < self.TTL_QUOTE

    def history(self, ticker: str) -> Any:
        return self._get(ticker, 'history', self.TTL_HISTORY)

    def news_features(self, ticker: str) -> Any:
        return self._get(ticker, 'news_features', self.TTL_NEWS)

    def technicals(self, ticker: str) -> Any:
        return self._get(ticker, 'technicals', self.TTL_TECHNICALS)

    def market_status(self) -> Any:
        """Market status doesn't take a ticker — special-case it."""
        fetch_fn = self.fetch_fns.get('market_status')
        if fetch_fn is None:
            return None
        try:
            return fetch_fn()
        except Exception:
            return None

    def macro(self) -> Any:
        """v4.14.2 stage 4: global macro snapshot (no ticker).

        Cache key uses synthetic '__macro__' ticker following the
        market_status precedent of global lookups but reusing the
        existing _get / TTL / disk-persistence machinery (instead
        of bypassing _get like market_status does — macro benefits
        from caching whereas market_status is a fast local check).

        Returns a merged dict combining Yahoo's keyless macro
        (Treasury yields + VIX) with FRED's FWK macro (Fed funds,
        CPI, unemployment, GDP, full series) when both are available;
        Yahoo-only when no FRED key is configured; None when both
        sources fail entirely. Stable shape regardless of which
        sources contributed — missing fields just absent.
        """
        return self._get('__macro__', 'macro', self.TTL_MACRO)

    def social(self, ticker: str) -> Any:
        """v4.14.2 stage 5: per-ticker social snapshot.

        Returns a merged dict combining Reddit posts (when embedded
        or user credentials are available) with StockTwits messages
        (always available — keyless). Schema is rolled-up summary
        + per-source breakdowns + a small sample of representative
        messages — enough for the prompt block to render meaningful
        social context without dumping raw post text.

        StockTwits-only is the common case until the user provisions
        embedded Reddit credentials. The merge layer handles either
        source being absent gracefully.
        """
        return self._get(ticker, 'social', self.TTL_SOCIAL)

    # ─── v4.14.1 stage 2: extended cache surface ───────────────────────

    def fundamentals(self, ticker: str) -> Any:
        """Per-ticker fundamentals (company info + financial metrics).

        6 h memory cache + 7-day disk SWR. Aggressive caching protects
        Finnhub's 60/min rate-limit budget across multi-ticker scans.

        Returns dict with keys: company_name, sector, industry,
        market_cap, shares_outstanding, pe_ratio, eps, beta,
        dividend_yield, as_of. Returns None on any failure (no API key,
        rate limit, network error, ticker not in Finnhub's database).

        Cold-start note: first scan post-install will hit Finnhub harder
        than steady-state; the router rate-limits gracefully and warm
        cache eliminates the cost on subsequent scans.
        """
        return self._get(ticker, 'fundamentals', self.TTL_FUNDAMENTALS)

    def earnings(self, ticker: str) -> Any:
        """Per-ticker earnings calendar view.

        Pure transformation over Stage 0's process-wide
        _EARNINGS_CALENDAR_CACHE — no TTL, no disk persistence here.
        Triggers the bulk Finnhub earnings fetch on the first call of
        the process (Stage 0 mechanism); subsequent calls are O(N) over
        events for the ticker.

        v4.14.2 stage 1: per-ticker fallback. If the bulk cache has no
        events for this ticker, fall back to a direct
        router.fetch('earnings', ticker=ticker, days_ahead=200). The
        router routes to Finnhub first (per-ticker call works for
        tickers Finnhub silently drops from its bulk window — the
        v4.14.1.3 AAPL/RIG case) then to Yahoo (keyless backup for
        users without a Finnhub key). Result is seeded into the bulk
        module cache so subsequent calls hit the fast path.

        Returns dict with optional keys 'next_event' and 'last_quarter'
        (each is itself a sub-dict with date/hour/eps_estimate/
        revenue_estimate or eps_actual/eps_estimate). Returns None if
        the ticker has no events in the calendar.
        """
        try:
            from datetime import date as _date
            import tm_discover
            # v4.14.5.52-earnings-offhotpath: CACHE-ONLY read. This method is on
            # the picker's prompt-build hot path. The previous LIVE reader
            # (get_earnings_with_status) fetches on a miss → on a COLD start the
            # FIRST such call synchronously ran the nasdaq WHOLE-UNIVERSE bulk
            # sweep (_ensure_fresh → _bulk_prefetch, ~1m47s) INLINE on the
            # calling thread, freezing the analysis loop before the first
            # verdict (prior investigation: dispatch 10:36:21 → sweep 10:36:24 →
            # first analysis 10:38:12). The fundfile daemon's earnings seeder
            # (startup one-shot + 30-min cadence: _earnings_seed_cycle →
            # get_earnings_with_status, which writes the DB row) is now the SOLE
            # live-fetch/warm path. get_earnings_for_ticker NEVER fetches; a cold
            # miss here just omits the EARNINGS block ('unknown') — the prompt
            # still builds and the pick is NOT blocked (graceful degradation).
            events = tm_discover.get_earnings_for_ticker(ticker)
            if not events:
                return None
            today_iso = _date.today().isoformat()
            next_event = None
            last_quarter = None
            for e in events:
                d = e.get('date') or ''
                if d >= today_iso and next_event is None:
                    next_event = e
                elif d < today_iso:
                    # Keeps overwriting — events are sorted ascending
                    # by date, so the last assignment wins as the
                    # most-recent past event.
                    last_quarter = e
            out = {}
            if next_event:
                out['next_event'] = {
                    'date':             next_event.get('date'),
                    'hour':             next_event.get('hour'),
                    'eps_estimate':     next_event.get('eps_estimate'),
                    'revenue_estimate': next_event.get('revenue_estimate'),
                }
            if last_quarter:
                out['last_quarter'] = {
                    'eps_actual':   last_quarter.get('eps_actual'),
                    'eps_estimate': last_quarter.get('eps_estimate'),
                }
            return out or None
        except Exception:
            return None

    def filings(self, ticker: str) -> Any:
        """Per-ticker SEC filings (8-K, 10-Q, 10-K, Form 4).

        4 h memory cache + 24 h disk SWR. EDGAR is free and unlimited
        (10/sec ceiling, well above any realistic usage).

        Returns dict with keys: filings (list of dicts each with form/
        filing_date/description/url/accession_no/primary_document),
        count, cik, company_name, as_of. Returns None on any failure
        (network error, ticker not in EDGAR's database, etc.).
        """
        return self._get(ticker, 'filings', self.TTL_FILINGS)

    def invalidate(self, ticker: str | None = None) -> None:
        """Force re-fetch on next read. None = invalidate all."""
        with self._lock:
            if ticker is None:
                self._cache.clear()
                self._dirty = True
            else:
                key_prefix = ticker.upper()
                self._cache = {k: v for k, v in self._cache.items()
                               if k[0] != key_prefix}
                self._dirty = True

    def seed(self, ticker: str, kind: str, value: Any) -> None:
        """v4.8.14: Insert externally-fetched data into the cache without
        triggering a fetch. Used by Discover's pre-filter batch — the
        batch already has fresh quotes for every candidate, so we feed
        them in here so the AI scoring phase finds them cached and skips
        the per-ticker yfinance call. Saves ~15 redundant API calls per
        scan and keeps borderline rate-limit conditions from tipping over.
        """
        if not ticker or not kind or value is None:
            return
        key = (ticker.upper(), kind)
        with self._lock:
            self._cache[key] = (value, time.time())
            if kind in self.PERSISTED_KINDS:
                self._dirty = True


# ════════════════════════════════════════════════════════════════════════
# PROMPT BUILDER — assembles structured prompts with real data
# ════════════════════════════════════════════════════════════════════════

class PromptBuilder:
    """Builds the prompts that get sent to the AI for analysis.

    Two main flavors:
        build_holding_analysis — for tradable positions, asks hold/sell
        build_locked_analysis  — for locked positions, watch-only, tax-aware

    Both prompts include:
        - The position itself (ticker, shares, cost basis, P&L)
        - Current market data (price, day change, volume)
        - Technical context (RSI, MA position, volatility)
        - Sector / market status
        - The user's chosen path/goal
        - Recent news headlines if available

    The output is a tuple: (prompt_text, debug_data_dict).
    debug_data_dict is what we show in the "show data sent to AI" panel.
    """

    def __init__(self, cache: DataCacheLayer,
                 predictions_log=None,
                 user_preferences_fn=None):
        self.cache = cache
        # Optional: tm_discover.PredictionsLog for injecting track record
        # into prompts. If None, prompts have no track-record context
        # (the track record still gets logged elsewhere).
        self.predictions_log = predictions_log
        # v4.15.0 Step 16: optional callable returning a one-line natural-
        # language description of the user's current Choices (price ranges +
        # style). Returns '' when choices are at defaults. None means no
        # user-preference line is injected.
        self.user_preferences_fn = user_preferences_fn

    def _get_user_preferences_line(self) -> str:
        """v4.15.0 Step 16: Resolve the current user-preferences line.
        Returns '' on no callable, exception, or empty result. Called by
        build_holding_analysis / build_locked_analysis / HoldingsWindow's
        _build_candidate_prompt just before the QUESTION block."""
        if self.user_preferences_fn is None:
            return ''
        try:
            return self.user_preferences_fn() or ''
        except Exception:
            return ''

    def build_holding_analysis(self, holding: dict, path_key: str) -> tuple[str, dict]:
        ticker = holding['ticker']
        shares = holding.get('shares', 0)
        buy_price = holding.get('buy_price', 0)
        total_cost = holding.get('total_cost', shares * buy_price)
        last_analyzed = holding.get('last_analyzed', None)

        # Gather data
        quote = self.cache.quote(ticker)
        technicals = self.cache.technicals(ticker)
        news = self.cache.news_features(ticker)
        market_status = self.cache.market_status()

        # Build human-readable position section
        current_price = (quote or {}).get('price') if quote else None
        day_change_pct = (quote or {}).get('change_pct') if quote else None
        volume = (quote or {}).get('volume') if quote else None

        if current_price is not None and buy_price:
            position_pnl = (current_price - buy_price) * shares
            position_pnl_pct = ((current_price - buy_price) / buy_price) * 100
        else:
            position_pnl = None
            position_pnl_pct = None

        # ── Build the prompt ──
        path_info = PATHS.get(path_key, PATHS[DEFAULT_PATH])

        lines = []
        lines.append(f"You are analyzing a position for the user. Be direct, commit "
                     f"to a recommendation, explain your reasoning. If the data "
                     f"is insufficient to give a real opinion, say what data "
                     f"you'd need — don't make up numbers.")
        lines.append("")
        lines.append(f"USER'S CHOSEN PATH: {path_info['name']}")
        lines.append(f"  ({path_info['description']})")
        lines.append("")
        lines.append("POSITION:")
        lines.append(f"  Ticker: {ticker}")
        lines.append(f"  Shares: {shares:g}")
        lines.append(f"  Cost basis: {_format_money(buy_price)}/share "
                     f"({_format_money(total_cost)} total)")
        lines.append(f"  Tradable: YES")
        if current_price is not None:
            lines.append(f"  Current price: {_format_money(current_price)}")
            if position_pnl_pct is not None:
                sign = "+" if position_pnl_pct >= 0 else ""
                lines.append(f"  P&L: {_format_money(position_pnl)} "
                             f"({sign}{position_pnl_pct:.1f}%)")
        if day_change_pct is not None:
            sign = "+" if day_change_pct >= 0 else ""
            lines.append(f"  Day change: {sign}{day_change_pct:.2f}%")
        if last_analyzed:
            lines.append(f"  Last AI analysis: {last_analyzed}")
        lines.append("")

        # v4.14.1 stage 5: route TECHNICALS + NEWS + new FACTS /
        # EARNINGS / FILINGS blocks through tm_context_builder. The
        # inline TECHNICALS / NEWS sections that used to live here
        # (with mismatched field names that produced near-empty
        # output) are replaced by a single build_context call. The
        # bare line break before and after preserves the \n\n
        # boundary that tm_consensus's POSITION/QUESTION string-find
        # relies on.
        try:
            import tm_context_builder
            data_context = tm_context_builder.build_context(
                ticker=ticker,
                path=path_key,
                cache=self.cache,
                log_callback=None,
                # v4.14.2 stage 4: prompt_kind drives the per-kind
                # block subset (now includes MACRO for owned-position
                # analysis); aggressive/lottery paths still skip
                # MACRO + FILINGS via _PATH_BLOCK_OVERRIDES.
                prompt_kind='holding_analysis',
            )
        except Exception:
            data_context = ""
        if data_context:
            lines.append(data_context)
            lines.append("")

        if market_status:
            # v4.10.14: get_market_status() returns a 2-tuple
            # (status_string, use_extended_flag). Old code only handled
            # str or dict, so when the function returned a tuple, the
            # whole prompt build crashed with:
            #   AttributeError: 'tuple' object has no attribute 'get'
            # That single line was the root cause of every Holdings
            # scan silently failing for the past several patches —
            # the AI was never being called at all.
            if isinstance(market_status, str):
                ms_text = market_status
            elif isinstance(market_status, tuple) and market_status:
                # First element of the tuple is the status string
                ms_text = str(market_status[0])
            elif isinstance(market_status, dict):
                ms_text = market_status.get('status', '')
            else:
                ms_text = str(market_status)
            if ms_text:
                lines.append(f"MARKET STATUS: {ms_text}")
                lines.append("")

        # v4.14.2 stage 7: epistemic humility prepend. If the assembled
        # context blocks flagged any source disagreement (news or
        # social), prepend the humility instructions BEFORE the
        # QUESTION: header so the AI reasons about uncertainty
        # explicitly. No-op when no flag is present (status quo).
        try:
            import tm_context_builder as _ctx
            _humility = _ctx.get_disagreement_context(data_context)
        except Exception:
            _humility = None
        if _humility:
            lines.append(_humility.rstrip())
            lines.append("")

        # v4.15.0 Step 16: inject user preferences just before QUESTION so the
        # AI reads context first, then the user's stated preferences, then what
        # it's being asked to do. No-op when choices are at defaults.
        _user_pref = self._get_user_preferences_line()
        if _user_pref:
            lines.append(_user_pref)
            lines.append("")

        # v4.14.5.63-tier-timeframe: the recover/grow horizon is tier-driven
        # (this holding's tier row), not a global 90-day literal — aggressive
        # asks about a 7-day window, speculative 45, etc. Single source of
        # truth = tm_watch_tiers; falls back to 90 only if the table can't be
        # read. Resolved from path_key so it matches the USER'S CHOSEN PATH
        # framing already rendered above.
        try:
            import tm_watch_tiers as _wt
            _horizon_days = int(_wt.tier_params(path_key).get(
                'horizon_days', 90))
        except Exception:
            _horizon_days = 90

        lines.append("QUESTION:")
        lines.append(f"  Given the user's chosen path ({path_info['name']}) and "
                     f"the data above, what's your recommendation on this "
                     f"position? Hold or sell? Give a probability (number, "
                     f"not 'low/medium/high') for whether the position will "
                     f"recover/grow within {_horizon_days} days. Be specific "
                     f"about what would change your call.")

        # Phase 2C: ask for a structured prediction at the end so the app
        # can parse and log it for track-record purposes.
        if _DISCOVER_AVAILABLE and tm_discover is not None:
            # Inject track record context first (if we have a log)
            if self.predictions_log is not None:
                try:
                    track_ctx = tm_discover.format_track_record_context(
                        self.predictions_log,
                        path=path_info.get('name', '').lower(),
                        ticker=ticker,
                    )
                    if track_ctx:
                        lines.append("")
                        lines.append(track_ctx)
                except Exception:
                    pass
            lines.append("")
            # v4.14.2 stage 4: owned-position prompt — HOLD remains
            # valid (there IS a position to hold).
            lines.append(
                tm_discover.format_prediction_request_block_owned())

        prompt = "\n".join(lines)

        # v4.14.1 stage 5: derive block presence from the assembled
        # prompt text instead of inspecting the raw cache outputs.
        # has_news / has_technicals now reflect what the AI actually
        # sees (post-build_context filtering), not whether the cache
        # held source data — the two can diverge when a builder
        # short-circuits on missing fields.
        block_markers = (
            '[FACTS]',
            '[NEWS — last 7 days]',
            '[EARNINGS CALENDAR]',
            '[INSIDER ACTIVITY / RECENT FILINGS]',
            '[TECHNICAL INDICATORS]',
        )
        debug = {
            'ticker': ticker,
            'path': path_info['name'],
            'has_quote': bool(quote),
            'has_news': '[NEWS — last 7 days]' in prompt,
            'has_facts': '[FACTS]' in prompt,
            'has_earnings': '[EARNINGS CALENDAR]' in prompt,
            'has_filings': '[INSIDER ACTIVITY / RECENT FILINGS]' in prompt,
            'has_technicals': '[TECHNICAL INDICATORS]' in prompt,
            'has_market_status': bool(market_status),
            'block_count': sum(1 for m in block_markers if m in prompt),
            'prompt_chars': len(prompt),
        }
        return prompt, debug

    def build_locked_analysis(self, holding: dict, path_key: str) -> tuple[str, dict]:
        """Different framing for locked positions — no hold/sell, more
        about 'anything notable that would unlock this or affect tax loss
        harvesting?'."""
        ticker = holding['ticker']
        shares = holding.get('shares', 0)
        buy_price = holding.get('buy_price', 0)
        total_cost = holding.get('total_cost', shares * buy_price)

        # v4.14.1 stage 5 (B1 fix): path_info was previously undefined
        # in this function — only build_holding_analysis derived it
        # from path_key. The track-record block at the bottom of this
        # function references path_info inside a try/except, which
        # silently swallowed the NameError and caused locked-analysis
        # prompts to never receive track-record context. Deriving it
        # the same way build_holding_analysis does fixes the silent
        # gap.
        path_info = PATHS.get(path_key, PATHS[DEFAULT_PATH])

        quote = self.cache.quote(ticker)
        news = self.cache.news_features(ticker)

        current_price = (quote or {}).get('price') if quote else None

        lines = []
        lines.append("You are watching a LOCKED position for the user. He cannot "
                     "sell this — it's frozen, illiquid, or otherwise non-"
                     "tradable. Don't recommend hold or sell. Instead, look "
                     "for anything notable: did the price move significantly? "
                     "Is there news? Anything regulatory? Tax loss harvesting "
                     "considerations?")
        lines.append("")
        lines.append(f"LOCKED POSITION:")
        lines.append(f"  Ticker: {ticker}")
        lines.append(f"  Shares: {shares:g}")
        lines.append(f"  Cost basis: {_format_money(buy_price)}/share "
                     f"({_format_money(total_cost)} total)")
        lines.append(f"  Tradable: NO (locked / illiquid)")
        if current_price is not None:
            lines.append(f"  Current price: {_format_money(current_price)}")
            if buy_price:
                pnl_pct = ((current_price - buy_price) / buy_price) * 100
                lines.append(f"  Loss: {pnl_pct:+.1f}%")
        lines.append("")

        # v4.14.1 stage 5: route the NEWS section (and the new FACTS /
        # EARNINGS / FILINGS blocks) through tm_context_builder.
        # TECHNICALS is excluded here — locked positions can't be
        # sold so technicals are decision-irrelevant.
        try:
            import tm_context_builder
            data_context = tm_context_builder.build_context(
                ticker=ticker,
                path=path_key,
                cache=self.cache,
                blocks=['FACTS', 'NEWS', 'EARNINGS', 'FILINGS'],
                log_callback=None,
            )
        except Exception:
            data_context = ""
        if data_context:
            lines.append(data_context)
            lines.append("")

        # v4.14.2 stage 7: epistemic humility prepend (locked variant).
        try:
            import tm_context_builder as _ctx
            _humility = _ctx.get_disagreement_context(data_context)
        except Exception:
            _humility = None
        if _humility:
            lines.append(_humility.rstrip())
            lines.append("")

        # v4.15.0 Step 16: inject user preferences just before QUESTION. Same
        # pattern as build_holding_analysis. No-op at default choices.
        _user_pref = self._get_user_preferences_line()
        if _user_pref:
            lines.append(_user_pref)
            lines.append("")

        lines.append("QUESTION:")
        lines.append("  Anything notable to flag? If the data shows nothing "
                     "unusual, just say so briefly (1-2 sentences). If "
                     "something IS notable — price spike, news, regulatory "
                     "filing, possible unlock event — describe it and what "
                     "the user should know.")

        # Phase 2C: even for locked positions, ask for the structured
        # summary so we can track AI's accuracy on "is anything happening?"
        # calls. Most locked predictions will be HOLD with low confidence,
        # which is the right answer most of the time.
        if _DISCOVER_AVAILABLE and tm_discover is not None:
            if self.predictions_log is not None:
                try:
                    track_ctx = tm_discover.format_track_record_context(
                        self.predictions_log,
                        path=path_info.get('name', '').lower(),
                        ticker=ticker,
                    )
                    if track_ctx:
                        lines.append("")
                        lines.append(track_ctx)
                except Exception:
                    pass
            lines.append("")
            # v4.14.2 stage 4: locked-position prompt — HOLD remains
            # valid (the position exists, just can't be sold; the AI
            # might still flag tax-loss harvesting timing or unlock
            # signals which are HOLD-shaped recommendations).
            lines.append(
                tm_discover.format_prediction_request_block_owned())

        prompt = "\n".join(lines)
        # v4.14.1 stage 5: same block-marker derivation as
        # build_holding_analysis, minus the technicals marker —
        # locked variants intentionally exclude TECHNICALS.
        block_markers_locked = (
            '[FACTS]',
            '[NEWS — last 7 days]',
            '[EARNINGS CALENDAR]',
            '[INSIDER ACTIVITY / RECENT FILINGS]',
        )
        debug = {
            'ticker': ticker,
            'is_locked': True,
            'has_quote': bool(quote),
            'has_news': '[NEWS — last 7 days]' in prompt,
            'has_facts': '[FACTS]' in prompt,
            'has_earnings': '[EARNINGS CALENDAR]' in prompt,
            'has_filings': '[INSIDER ACTIVITY / RECENT FILINGS]' in prompt,
            'has_technicals': False,  # locked variant excludes technicals
            'block_count': sum(1 for m in block_markers_locked
                                 if m in prompt),
            'prompt_chars': len(prompt),
        }
        return prompt, debug


# ════════════════════════════════════════════════════════════════════════
# SIGNALS LOG — persistent record of every AI observation
# ════════════════════════════════════════════════════════════════════════

class SignalsLog:
    """Persistent append-only log of AI observations.

    Each entry is one JSON line in data/signals.jsonl. Format:
        {ts, ticker, path, model, prompt_summary, response, duration_sec,
         was_locked, manual_trigger}

    The Signals view reads from this file; the analyzer writes to it.
    Persistent across launches (this is the AI's "memory" for retrospective).
    """

    def __init__(self, log_path: Path):
        self.log_path = Path(log_path)
        self._lock = threading.Lock()

    def append(self, entry: dict) -> None:
        # v4.10.12: log failures instead of silently swallowing.
        # The old `pass` here meant a write-permission issue or disk-full
        # condition would just disappear — we'd think the scan worked but
        # nothing got persisted. Now we keep the try/except (since
        # signals are best-effort, we don't want to crash the scan over
        # logging) but at least PRINT the error so it shows up in
        # diagnostic logs even when the GUI is misbehaving.
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            entry = dict(entry)  # don't mutate caller's dict
            entry.setdefault('ts', _now_iso())
            # v4.13.65 schema bump: forward-compat fields for the
            # v4.14.0 routing rework. setdefault preserves caller-
            # supplied values (cloud consensus paths already pass
            # provider_id) and fills in safe defaults for everything
            # else so signals.jsonl has a uniform shape going forward.
            entry.setdefault('provider_id', None)
            entry.setdefault('canonical_model', None)
            entry.setdefault('lineup_version', 'v4.13')
            entry.setdefault('data_version', 'v4.14.1')
            with self._lock:
                with open(self.log_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except Exception as e:
            try:
                print(f"[SignalsLog] Failed to append to "
                      f"{self.log_path}: {type(e).__name__}: {e}")
            except Exception:
                pass  # really really shouldn't fail twice

    def read_recent(self, limit: int = 50) -> list[dict]:
        """Return the most recent N entries, newest first."""
        if not self.log_path.exists():
            return []
        try:
            lines = self.log_path.read_text(encoding='utf-8').splitlines()
            entries = []
            for line in reversed(lines):  # newest first
                if not line.strip():
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
                if len(entries) >= limit:
                    break
            return entries
        except Exception:
            return []


# ════════════════════════════════════════════════════════════════════════
# UI — HoldingsWindow
# ════════════════════════════════════════════════════════════════════════

class HoldingsWindow:
    """The Holdings tab. Lives next to AI Chat in the header.

    Layout:
        Top row: Path selector | Check Now | Show Prompts toggle | Refresh
        Left column: Holdings list (with Re-check buttons per row)
        Right column: Signals feed (timestamped AI observations)
        Bottom: Add holding form
    """

    @staticmethod
    def _build_history_fn():
        """v4.13.15: Returns a callable that fetches daily OHLC history
        for a ticker, formatted for PredictionsLog.check_outcomes.
        Used so outcome evaluation can be WICK-AWARE - only daily
        closes count, not intraday wicks. Returns None on failure.
        """
        def _fetch(ticker: str):
            try:
                import yfinance as _yf
                # 60d is enough for any reasonable prediction window
                t = _yf.Ticker(ticker)
                df = t.history(period='60d', interval='1d',
                                auto_adjust=False)
                if df is None or df.empty:
                    return None
                out = []
                for idx, row in df.iterrows():
                    try:
                        d = idx.strftime('%Y-%m-%d')
                        close = float(row['Close'])
                        out.append({'date': d, 'close': close})
                    except Exception:
                        continue
                return out
            except Exception:
                return None
        return _fetch

    def __init__(self, parent: tk.Misc, holdings_mgr: HoldingsManager,
                 cache: DataCacheLayer, signals_log: SignalsLog,
                 prompt_builder: PromptBuilder, get_path_fn: Callable[[], str],
                 set_path_fn: Callable[[str], None],
                 predictions_log=None,
                 watchlist=None,
                 universe=None,
                 log_callback=None,
                 save_universe_source=None,
                 sync_main_universe=None,
                 get_configured_model=None,
                 cfg=None,
                 app=None):
        self.parent = parent
        self.mgr = holdings_mgr
        self.cache = cache
        self.signals_log = signals_log
        self.prompt_builder = prompt_builder
        self.get_path = get_path_fn
        self.set_path = set_path_fn
        # Phase 2C extensions (all optional)
        self.predictions_log = predictions_log
        self.watchlist = watchlist
        self.universe = universe
        self.log_callback = log_callback
        self.save_universe_source = save_universe_source
        self.sync_main_universe = sync_main_universe
        # v4.8.4: Returns user-configured analysis model from app config,
        # or None to fall back to canonical names.
        self.get_configured_model = get_configured_model
        # v4.8.12: full cfg reference for new toggle-based features
        # (include_recently_analyzed, etc.). Optional for backward compat.
        self.cfg = cfg
        # v4.10.6: optional reference to the App instance. Gives
        # HoldingsWindow access to design tokens (app.fonts, app.space,
        # app.btn) and the standardized popup helpers
        # (app._make_styled_toplevel, app._make_close_button). Backward
        # compatible — if app is None, code falls back to ad-hoc
        # styling. Future patches will progressively use these tokens
        # to make Track Record / Discover panel match the design
        # system.
        self.app = app

        self.window: tk.Toplevel | None = None
        self._show_prompts_var: tk.BooleanVar | None = None
        self._path_var: tk.StringVar | None = None

        # In-flight tracking
        # (was tm_ai.AIRequest | None; Ollama retired — the Holdings-window
        # analyze that used this is dead code, the window is unreachable.)
        self._active_request = None
        self._last_check_now_time = 0.0
        self._check_now_queue: list[dict] = []  # holdings still to scan
        self._current_signal_text_widget: tk.Text | None = None
        self._current_signal_start_idx: str | None = None
        # v4.8.4: discover scan state (used by both panel + headless paths)
        self._discover_running: bool = False
        self._all_paths_running: bool = False  # v4.13.11
        self._headless_scan_done: Callable | None = None
        self._discover_win = None
        self._track_record_win = None
        # v4.9.0: consensus scan state. _consensus_scan_id is the
        # current scan ID being stamped on predictions (None outside
        # a consensus scan). _consensus_running tracks whether a
        # consensus scan is in progress so the UI can prevent overlap.
        self._consensus_scan_id: str | None = None
        self._consensus_running: bool = False

    def _log_to_main(self, msg: str, color: str = 'muted'):
        """Write a message to the main window's activity log, if a
        callback is wired. Best-effort — silently no-ops if not."""
        if self.log_callback is not None:
            try:
                self.log_callback(msg, color)
            except Exception:
                pass

    def _get_configured_model(self) -> str | None:
        """Return user-configured analysis model from app config, or None."""
        if self.get_configured_model is not None:
            try:
                return self.get_configured_model()
            except Exception:
                pass
        return None

    def _main_universe_changed(self, source_key: str):
        """Called by the main window when the header dropdown changes.
        Updates the in-Discover dropdown + state line so they stay in
        sync. No-op if Discover panel isn't currently open.
        """
        try:
            if self.universe is not None:
                self.universe.set_source(source_key)
        except Exception:
            pass
        # Update the in-Discover dropdown if it exists
        try:
            if (hasattr(self, '_discover_src_var')
                    and self._discover_src_var is not None):
                info = self.universe.SOURCES.get(source_key, {})
                label = info.get('label', source_key)
                self._discover_src_var.set(label)
        except Exception:
            pass
        # Update the state line
        try:
            if (hasattr(self, '_discover_u_state_lbl')
                    and self._discover_u_state_lbl is not None):
                self._discover_u_state_lbl.config(
                    text=self._universe_state_text())
        except Exception:
            pass

    def show(self) -> None:
        if self.window is not None and self.window.winfo_exists():
            self.window.lift()
            self.window.focus_force()
            return

        self.window = tk.Toplevel(self.parent)
        self.window.title("Holdings & Signals — Tired Market")
        self.window.configure(bg=THEME['bg'])
        self.window.geometry("1100x720")
        self.window.minsize(820, 540)
        self.window.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._render_holdings()
        self._render_signals()

    # ─── UI construction ───

    def _build_ui(self) -> None:
        c = THEME
        w = self.window

        # ─── Top control bar ───
        top = tk.Frame(w, bg=c['card'], padx=10, pady=8)
        top.pack(side='top', fill='x')

        # Path selector
        tk.Label(top, text="Path:", bg=c['card'], fg=c['muted'],
                 font=('Segoe UI', 9)).pack(side='left', padx=(0, 4))
        path_names = [PATHS[k]['name'] for k in PATHS]
        self._path_var = tk.StringVar(
            value=PATHS.get(self.get_path(), PATHS[DEFAULT_PATH])['name'])
        path_combo = ttk.Combobox(top, textvariable=self._path_var,
                                   values=path_names, width=18,
                                   state='readonly', font=('Segoe UI', 9))
        path_combo.pack(side='left', padx=(0, 8))
        path_combo.bind('<<ComboboxSelected>>', self._on_path_changed)

        # Path description (small text, updates with selection)
        self._path_desc_lbl = tk.Label(top, text=self._current_path_desc(),
                                        bg=c['card'], fg=c['dim'],
                                        font=('Segoe UI', 8),
                                        anchor='w', wraplength=400)
        self._path_desc_lbl.pack(side='left', padx=(0, 12), fill='x', expand=True)

        # Check Now button
        tk.Button(top, text="Check Now", bg=c['green'], fg=c['bg'],
                  relief='flat', padx=14, pady=4,
                  font=('Segoe UI', 9, 'bold'), cursor='hand2',
                  command=self._check_now_all).pack(side='right', padx=(4, 0))

        # Trade History button (shows closed positions + win/loss stats)
        tk.Button(top, text="History", bg=c['card2'], fg=c['amber'],
                  relief='flat', padx=10, pady=4,
                  font=('Segoe UI', 9), cursor='hand2',
                  command=self._show_trade_history).pack(side='right', padx=(4, 0))

        # Phase 2C: Discover + Track Record buttons (only if module loaded)
        if (self.predictions_log is not None and self.watchlist is not None
                and self.universe is not None):
            tk.Button(top, text="Track Record", bg=c['card2'], fg=c['blue'],
                      relief='flat', padx=10, pady=4,
                      font=('Segoe UI', 9), cursor='hand2',
                      command=self._show_track_record
                      ).pack(side='right', padx=(4, 0))
            # "Discover" button removed in the Ollama exit (Step 3b2): the
            # Discover window (local-Ollama only) was retired. Cloud Scan +
            # Recommend replace it. (This Holdings-window builder is itself
            # dead code — the window is unreachable — but kept tidy.)

        # Show Prompts toggle
        self._show_prompts_var = tk.BooleanVar(value=True)
        tk.Checkbutton(top, text="show prompts",
                       variable=self._show_prompts_var,
                       bg=c['card'], fg=c['muted'],
                       selectcolor=c['card2'],
                       activebackground=c['card'], activeforeground=c['text'],
                       font=('Segoe UI', 8), borderwidth=0
                       ).pack(side='right', padx=(8, 4))

        # ─── Main split: left = holdings, right = signals ───
        body = tk.Frame(w, bg=c['bg'])
        body.pack(side='top', fill='both', expand=True, padx=8, pady=4)

        # Use grid so we can size proportionally
        body.columnconfigure(0, weight=2, minsize=380)
        body.columnconfigure(1, weight=3, minsize=440)
        body.rowconfigure(0, weight=1)

        # Left side: holdings list
        left = tk.Frame(body, bg=c['card'])
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 4))
        self._build_holdings_panel(left)

        # Right side: signals feed
        right = tk.Frame(body, bg=c['card'])
        right.grid(row=0, column=1, sticky='nsew', padx=(4, 0))
        self._build_signals_panel(right)

    def _build_holdings_panel(self, parent: tk.Frame) -> None:
        c = THEME
        # Header
        hdr = tk.Frame(parent, bg=c['card'], padx=10, pady=6)
        hdr.pack(side='top', fill='x')
        tk.Label(hdr, text="HOLDINGS", bg=c['card'], fg=c['accent'],
                 font=('Segoe UI', 10, 'bold')).pack(side='left')

        # Scrollable holdings list
        list_outer = tk.Frame(parent, bg=c['card'])
        list_outer.pack(side='top', fill='both', expand=True, padx=4, pady=(0, 4))

        canvas = tk.Canvas(list_outer, bg=c['card'], highlightthickness=0)
        sb = ttk.Scrollbar(list_outer, orient='vertical', command=canvas.yview)
        self._holdings_frame = tk.Frame(canvas, bg=c['card'])
        self._holdings_frame.bind('<Configure>',
            lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.create_window((0, 0), window=self._holdings_frame, anchor='nw')
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)

        # Mousewheel for holdings list
        def _wheel(e):
            delta = -3 if e.delta > 0 else 3
            canvas.yview_scroll(delta, 'units')
            return 'break'
        # v4.10.7: removed focus_set on Enter (same fix as main window —
        # was raising this Toplevel above other popups when mouse moved over it).
        canvas.bind('<MouseWheel>', _wheel)
        self._holdings_frame.bind('<MouseWheel>', _wheel)

        # Add-holding form at bottom
        form = tk.Frame(parent, bg=c['card2'], padx=8, pady=8)
        form.pack(side='bottom', fill='x')

        tk.Label(form, text="Add holding:", bg=c['card2'], fg=c['muted'],
                 font=('Segoe UI', 9, 'bold')).grid(row=0, column=0, columnspan=4,
                                                     sticky='w', pady=(0, 4))
        # Row 1: ticker | shares | buy_price
        tk.Label(form, text="Ticker:", bg=c['card2'], fg=c['muted'],
                 font=('Segoe UI', 8)).grid(row=1, column=0, sticky='w', padx=(0, 2))
        self._ticker_entry = tk.Entry(form, width=8, bg=c['bg'], fg=c['text'],
                                       insertbackground=c['text'],
                                       relief='flat', font=('Segoe UI', 9))
        self._ticker_entry.grid(row=1, column=1, padx=(0, 8))

        tk.Label(form, text="Shares:", bg=c['card2'], fg=c['muted'],
                 font=('Segoe UI', 8)).grid(row=1, column=2, sticky='w', padx=(0, 2))
        self._shares_entry = tk.Entry(form, width=10, bg=c['bg'], fg=c['text'],
                                       insertbackground=c['text'],
                                       relief='flat', font=('Segoe UI', 9))
        self._shares_entry.grid(row=1, column=3, padx=(0, 8))

        tk.Label(form, text="Cost/share:", bg=c['card2'], fg=c['muted'],
                 font=('Segoe UI', 8)).grid(row=1, column=4, sticky='w', padx=(0, 2))
        self._price_entry = tk.Entry(form, width=10, bg=c['bg'], fg=c['text'],
                                      insertbackground=c['text'],
                                      relief='flat', font=('Segoe UI', 9))
        self._price_entry.grid(row=1, column=5, padx=(0, 8))

        # Row 2: tradable + add button
        self._tradable_var = tk.BooleanVar(value=True)
        tk.Checkbutton(form, text="Tradable", variable=self._tradable_var,
                       bg=c['card2'], fg=c['muted'],
                       selectcolor=c['bg'],
                       activebackground=c['card2'], activeforeground=c['text'],
                       font=('Segoe UI', 8), borderwidth=0
                       ).grid(row=2, column=0, columnspan=2, sticky='w', pady=(4, 0))

        tk.Button(form, text="Add", bg=c['accent'], fg=c['bg'],
                  relief='flat', padx=14, pady=2,
                  font=('Segoe UI', 9, 'bold'), cursor='hand2',
                  command=self._on_add_clicked
                  ).grid(row=2, column=5, sticky='e', pady=(4, 0))

    def _build_signals_panel(self, parent: tk.Frame) -> None:
        c = THEME
        # Header with status
        hdr = tk.Frame(parent, bg=c['card'], padx=10, pady=6)
        hdr.pack(side='top', fill='x')
        tk.Label(hdr, text="SIGNALS", bg=c['card'], fg=c['green'],
                 font=('Segoe UI', 10, 'bold')).pack(side='left')

        self._status_lbl = tk.Label(hdr, text="", bg=c['card'], fg=c['muted'],
                                     font=('Segoe UI', 8))
        self._status_lbl.pack(side='right')

        # Scrollable text area for signals
        body = tk.Frame(parent, bg=c['card'])
        body.pack(side='top', fill='both', expand=True, padx=4, pady=(0, 4))

        self.signals_text = tk.Text(body, bg=c['card'], fg=c['text'],
                                     font=('Segoe UI', 9),
                                     wrap='word', relief='flat',
                                     padx=10, pady=8,
                                     highlightthickness=0,
                                     state='normal',
                                     cursor='arrow')
        sb = ttk.Scrollbar(body, orient='vertical',
                            command=self.signals_text.yview)
        self.signals_text.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.signals_text.pack(side='left', fill='both', expand=True)

        # Block edits but allow selection/copy
        def _block_edit(e):
            if e.state & 0x4:  # Ctrl held
                return None
            allowed = {'Up', 'Down', 'Left', 'Right', 'Home', 'End',
                       'Prior', 'Next', 'Shift_L', 'Shift_R',
                       'Control_L', 'Control_R'}
            if e.keysym in allowed:
                return None
            return 'break'
        self.signals_text.bind('<Key>', _block_edit)
        self.signals_text.bind('<<Paste>>', lambda e: 'break')
        self.signals_text.bind('<<Cut>>', lambda e: 'break')

        # Mousewheel scrolls signals when over them
        def _wheel(e):
            delta = -3 if e.delta > 0 else 3
            self.signals_text.yview_scroll(delta, 'units')
            return 'break'
        self.signals_text.bind('<Enter>', lambda e: self.signals_text.focus_set())
        self.signals_text.bind('<MouseWheel>', _wheel)

        # Tags
        self.signals_text.tag_configure('header', foreground=c['accent'],
                                         font=('Segoe UI', 9, 'bold'))
        self.signals_text.tag_configure('meta', foreground=c['dim'],
                                         font=('Segoe UI', 8))
        self.signals_text.tag_configure('body', foreground=c['text'],
                                         font=('Segoe UI', 9))
        self.signals_text.tag_configure('locked_header', foreground=c['amber'],
                                         font=('Segoe UI', 9, 'bold'))
        self.signals_text.tag_configure('error', foreground=c['red'],
                                         font=('Segoe UI', 9, 'italic'))
        self.signals_text.tag_configure('hint', foreground=c['muted'],
                                         font=('Segoe UI', 8, 'italic'))
        self.signals_text.tag_configure('prompt', foreground=c['dim'],
                                         font=('Consolas', 8))

    # ─── Render helpers ───

    def _render_holdings(self) -> None:
        """Refresh the holdings list panel."""
        c = THEME
        # Clear existing rows
        for child in self._holdings_frame.winfo_children():
            child.destroy()

        holdings = self.mgr.holdings
        if not holdings:
            tk.Label(self._holdings_frame,
                     text="No holdings yet. Add one below.",
                     bg=c['card'], fg=c['muted'],
                     font=('Segoe UI', 9, 'italic'),
                     padx=12, pady=12).pack(anchor='w')
            return

        for idx, h in enumerate(holdings):
            self._render_holding_row(idx, h)

    def _render_holding_row(self, idx: int, h: dict) -> None:
        c = THEME
        ticker = h.get('ticker', '?')
        shares = h.get('shares', 0)
        buy_price = h.get('buy_price', 0)
        total_cost = h.get('total_cost', shares * buy_price)
        tradable = h.get('tradable', True)
        last_analyzed = h.get('last_analyzed')

        # Get current quote from cache (cheap if recent, may fetch otherwise)
        quote = self.cache.quote(ticker)
        current_price = (quote or {}).get('price') if quote else None

        row_bg = c['card'] if (idx % 2 == 0) else c['card2']

        row = tk.Frame(self._holdings_frame, bg=row_bg, padx=8, pady=6)
        row.pack(fill='x', pady=1)

        # Top line: ticker + tag + value
        line1 = tk.Frame(row, bg=row_bg)
        line1.pack(fill='x')

        tk.Label(line1, text=ticker, bg=row_bg, fg=c['accent'],
                 font=('Segoe UI', 11, 'bold')).pack(side='left')

        if not tradable:
            tk.Label(line1, text="LOCKED", bg=row_bg, fg=c['amber'],
                     font=('Segoe UI', 7, 'bold'),
                     padx=4).pack(side='left', padx=(6, 0))

        # Right side: action buttons (right-most appears first because pack side='right')
        # Re-check — always shown
        tk.Button(line1, text="Re-check",
                  bg=row_bg, fg=c['teal'],
                  relief='flat', padx=8, pady=1,
                  font=('Segoe UI', 8), cursor='hand2',
                  command=lambda t=ticker: self._check_now_single(t)
                  ).pack(side='right')

        # Remove — visible red so it's findable. Different from Sell:
        # Remove = "I want to delete this from my list, no money was traded"
        # Sell = "I sold this at a price, record the trade and P&L"
        tk.Button(line1, text="Remove",
                  bg=row_bg, fg=c['red'],
                  relief='flat', padx=6, pady=1,
                  font=('Segoe UI', 8), cursor='hand2',
                  command=lambda t=ticker: self._on_remove_clicked(t)
                  ).pack(side='right', padx=(0, 6))

        # Sell — only for tradable positions; locked positions can't be sold
        if tradable:
            tk.Button(line1, text="Sell",
                      bg=c['green'], fg=c['bg'],
                      relief='flat', padx=8, pady=1,
                      font=('Segoe UI', 8, 'bold'), cursor='hand2',
                      command=lambda t=ticker: self._on_sell_clicked(t)
                      ).pack(side='right', padx=(0, 6))

        # Tradable toggle
        tradable_var = tk.BooleanVar(value=tradable)
        cb = tk.Checkbutton(line1, text="tradable",
                             variable=tradable_var,
                             bg=row_bg, fg=c['muted'],
                             selectcolor=c['bg'],
                             activebackground=row_bg, activeforeground=c['text'],
                             font=('Segoe UI', 7), borderwidth=0,
                             command=lambda t=ticker, v=tradable_var:
                                self._on_tradable_toggled(t, v.get()))
        cb.pack(side='right', padx=(0, 8))

        # Second line: position + price + P&L
        line2 = tk.Frame(row, bg=row_bg)
        line2.pack(fill='x', pady=(2, 0))

        tk.Label(line2, text=f"{shares:g} shares @ {_format_money(buy_price)}",
                 bg=row_bg, fg=c['muted'],
                 font=('Segoe UI', 8)).pack(side='left')

        if current_price is not None:
            pnl = (current_price - buy_price) * shares
            pnl_color = c['green'] if pnl >= 0 else c['red']
            sign = "+" if pnl >= 0 else ""
            tk.Label(line2,
                     text=f"  →  now {_format_money(current_price)} "
                          f"({sign}{_format_money(pnl)})",
                     bg=row_bg, fg=pnl_color,
                     font=('Segoe UI', 8)).pack(side='left')
        else:
            tk.Label(line2, text="  (price unavailable)",
                     bg=row_bg, fg=c['dim'],
                     font=('Segoe UI', 8, 'italic')).pack(side='left')

        if last_analyzed:
            try:
                la = datetime.fromisoformat(last_analyzed)
                age = datetime.now() - la
                if age.total_seconds() < 60:
                    age_str = "just now"
                elif age.total_seconds() < 3600:
                    age_str = f"{int(age.total_seconds() / 60)}m ago"
                elif age.total_seconds() < 86400:
                    age_str = f"{int(age.total_seconds() / 3600)}h ago"
                else:
                    age_str = f"{int(age.total_seconds() / 86400)}d ago"
                tk.Label(line2, text=f"  • analyzed {age_str}",
                         bg=row_bg, fg=c['dim'],
                         font=('Segoe UI', 7)).pack(side='right')
            except Exception:
                pass

    def _render_signals(self) -> None:
        """Initial render of recent signals from the persistent log."""
        recent = self.signals_log.read_recent(limit=20)

        self.signals_text.delete('1.0', 'end')
        if not recent:
            self.signals_text.insert('end',
                "No signals yet. Click Check Now or Re-check on a holding.\n",
                'hint')
            return

        for entry in recent:  # already newest-first
            self._append_signal_to_view(entry, scroll_to_bottom=False)
        self.signals_text.see('1.0')  # scroll to top (newest)

    def _append_signal_to_view(self, entry: dict, scroll_to_bottom: bool = True) -> None:
        c = THEME
        ts = entry.get('ts', '')
        try:
            ts_dt = datetime.fromisoformat(ts)
            ts_str = ts_dt.strftime('%b %d %I:%M %p').replace(' 0', ' ')
        except Exception:
            ts_str = ts
        ticker = entry.get('ticker', '?')
        path = entry.get('path', '')
        model = entry.get('model', 'ai')
        duration = entry.get('duration_sec', 0)
        was_locked = entry.get('was_locked', False)
        response = entry.get('response', '')

        # Header line: ticker + locked tag + timestamp
        header_tag = 'locked_header' if was_locked else 'header'
        loc = " (locked)" if was_locked else ""
        self.signals_text.insert('end', f"\n{ticker}{loc}", header_tag)
        self.signals_text.insert('end',
            f"  · {ts_str}  · {model}  · {duration:.1f}s  · {path}\n",
            'meta')

        # Response body
        body = response.strip() if response else "(no response)"
        self.signals_text.insert('end', body + "\n", 'body')
        # Separator
        self.signals_text.insert('end',
            "─" * 60 + "\n", 'meta')

        if scroll_to_bottom:
            self.signals_text.see('end')

    # ─── Event handlers ───

    def _on_close(self) -> None:
        # v4.10.9: DON'T cancel the in-flight scan on close. Before, if
        # you clicked Check Now and got impatient, closing the window
        # would kill the scan and lose all the work. Now the scan
        # continues in the background — its result will be logged to
        # activity feed + predictions_log when complete, even though
        # the window is gone. The streaming callbacks (_on_token,
        # _on_done, _on_error) all check `if self.window is None: return`
        # at the top, so they degrade gracefully without UI updates,
        # but the prediction itself still gets parsed and saved.
        if self._active_request is not None:
            try:
                if self.log_callback:
                    self.log_callback(
                        f"Holdings window closed; in-flight scan for "
                        f"{getattr(self, '_current_ticker', '?')} continues "
                        f"in background — result will be logged here.",
                        'muted')
            except Exception:
                pass
        if self.window is not None:
            self.window.destroy()
            self.window = None

    def _current_path_desc(self) -> str:
        path = self.get_path()
        return PATHS.get(path, PATHS[DEFAULT_PATH])['description']

    def _on_path_changed(self, _event=None) -> None:
        # Map display name back to key
        chosen = self._path_var.get()
        for key, info in PATHS.items():
            if info['name'] == chosen:
                self.set_path(key)
                self._path_desc_lbl.config(text=info['description'])
                return

    def _on_add_clicked(self) -> None:
        ticker = self._ticker_entry.get().strip().upper()
        if not ticker:
            return
        try:
            shares = float(self._shares_entry.get())
            buy_price = float(self._price_entry.get())
        except (ValueError, TypeError):
            messagebox.showerror("Invalid input",
                "Shares and Cost/share must be numbers.")
            return
        if shares <= 0 or buy_price <= 0:
            messagebox.showerror("Invalid input",
                "Shares and price must be positive.")
            return

        tradable = self._tradable_var.get()
        self.mgr.add_holding(ticker, shares, buy_price, tradable=tradable)
        self.mgr.save()

        # Clear form
        self._ticker_entry.delete(0, 'end')
        self._shares_entry.delete(0, 'end')
        self._price_entry.delete(0, 'end')
        self._tradable_var.set(True)

        self._render_holdings()

    def _on_remove_clicked(self, ticker: str) -> None:
        if messagebox.askyesno("Remove holding",
                f"Remove {ticker} from your holdings?\n\n"
                f"This is for cleaning up entries you don't actually own — "
                f"like a typo, or something you tracked here but didn't really buy.\n\n"
                f"If you SOLD this position and want to record the trade with "
                f"P&L, click Cancel and use the Sell button instead."):
            self.mgr.remove_holding(ticker)
            self.mgr.save()
            self._render_holdings()

    def _on_sell_clicked(self, ticker: str) -> None:
        """Open the Sell dialog for a holding. Records the trade with P&L
        and moves it to the closed list (the realized track record)."""
        c = THEME
        # Find the holding to populate defaults
        target = None
        for h in self.mgr.holdings:
            if h.get('ticker', '').upper() == ticker.upper():
                target = h
                break
        if target is None:
            return

        # Try to populate sell price with current quote
        quote = self.cache.quote(ticker)
        suggested_price = (quote or {}).get('price') if quote else None

        win = tk.Toplevel(self.window)
        win.title(f"Sell {ticker}")
        win.configure(bg=c['bg'])
        # Bigger default + minsize so buttons are always visible without resize
        win.geometry("520x540")
        win.minsize(440, 480)
        win.transient(self.window)
        win.grab_set()

        # Reserve the button row at the bottom FIRST, before any content packs.
        # This prevents long content from pushing buttons off-screen.
        btn_row = tk.Frame(win, bg=c['bg'], height=64)
        btn_row.pack(side='bottom', fill='x', padx=20, pady=14)
        btn_row.pack_propagate(False)

        # Header
        tk.Label(win, text=f"Sell {ticker}", bg=c['bg'], fg=c['accent'],
                 font=('Segoe UI', 14, 'bold')).pack(side='top', pady=(14, 4))
        tk.Label(win,
                 text="Record the sale. P&L gets computed and added to your "
                      "trade history.",
                 bg=c['bg'], fg=c['muted'], font=('Segoe UI', 9),
                 wraplength=400).pack(side='top', pady=(0, 12))

        # Position summary
        shares = target.get('shares', 0)
        buy_price = target.get('buy_price', 0)
        total_cost = target.get('total_cost', shares * buy_price)
        info_frame = tk.Frame(win, bg=c['card'], padx=14, pady=10)
        info_frame.pack(side='top', fill='x', padx=20, pady=4)
        tk.Label(info_frame, text="POSITION", bg=c['card'], fg=c['accent'],
                 font=('Segoe UI', 8, 'bold')
                 ).pack(side='top', anchor='w')
        tk.Label(info_frame,
                 text=f"{shares:g} shares @ {_format_money(buy_price)} = "
                      f"{_format_money(total_cost)} total",
                 bg=c['card'], fg=c['text'], font=('Segoe UI', 9)
                 ).pack(side='top', anchor='w', pady=(2, 0))

        # Sell price entry
        price_frame = tk.Frame(win, bg=c['bg'])
        price_frame.pack(side='top', fill='x', padx=20, pady=(8, 4))
        tk.Label(price_frame, text="Sell price per share:", bg=c['bg'],
                 fg=c['text'], font=('Segoe UI', 9)).pack(side='top', anchor='w')
        price_entry = tk.Entry(price_frame, bg=c['card'], fg=c['text'],
                                insertbackground=c['text'],
                                relief='flat', font=('Segoe UI', 11),
                                width=18)
        price_entry.pack(side='top', anchor='w', pady=(4, 0), ipady=4)
        if suggested_price is not None:
            price_entry.insert(0, f"{suggested_price:g}")
        price_entry.focus_set()
        price_entry.select_range(0, 'end')

        if suggested_price is not None:
            tk.Label(price_frame,
                     text=f"(current price: {_format_money(suggested_price)})",
                     bg=c['bg'], fg=c['dim'], font=('Segoe UI', 8)
                     ).pack(side='top', anchor='w', pady=(2, 0))

        # Sell date entry
        date_frame = tk.Frame(win, bg=c['bg'])
        date_frame.pack(side='top', fill='x', padx=20, pady=(8, 4))
        tk.Label(date_frame, text="Sell date (YYYY-MM-DD):",
                 bg=c['bg'], fg=c['text'], font=('Segoe UI', 9)
                 ).pack(side='top', anchor='w')
        date_entry = tk.Entry(date_frame, bg=c['card'], fg=c['text'],
                               insertbackground=c['text'],
                               relief='flat', font=('Segoe UI', 11), width=18)
        date_entry.pack(side='top', anchor='w', pady=(4, 0), ipady=4)
        date_entry.insert(0, datetime.now().strftime('%Y-%m-%d'))

        # Notes (optional)
        notes_frame = tk.Frame(win, bg=c['bg'])
        notes_frame.pack(side='top', fill='x', padx=20, pady=(8, 4))
        tk.Label(notes_frame, text="Notes (optional):",
                 bg=c['bg'], fg=c['text'], font=('Segoe UI', 9)
                 ).pack(side='top', anchor='w')
        notes_entry = tk.Entry(notes_frame, bg=c['card'], fg=c['text'],
                                insertbackground=c['text'],
                                relief='flat', font=('Segoe UI', 9))
        notes_entry.pack(side='top', fill='x', pady=(4, 0), ipady=4)

        # Live P&L preview
        preview_lbl = tk.Label(win, text="", bg=c['bg'], fg=c['muted'],
                                font=('Segoe UI', 10, 'bold'))
        preview_lbl.pack(side='top', pady=(10, 4))

        def _update_preview(*_):
            try:
                p = float(price_entry.get())
                proceeds = shares * p
                pnl = proceeds - total_cost
                pct = (pnl / total_cost * 100) if total_cost else 0
                color = c['green'] if pnl >= 0 else c['red']
                sign = "+" if pnl >= 0 else ""
                preview_lbl.config(
                    text=f"P&L: {sign}{_format_money(pnl)} ({sign}{pct:.1f}%)",
                    fg=color)
            except (ValueError, TypeError):
                preview_lbl.config(text="(enter a valid price to see P&L)",
                                    fg=c['dim'])

        price_entry.bind('<KeyRelease>', _update_preview)
        _update_preview()

        # (btn_row was packed at the very top of this method, before content,
        # so it always reserves space at the bottom regardless of content height)

        def _confirm_sell():
            try:
                p = float(price_entry.get())
                if p <= 0:
                    raise ValueError("must be positive")
            except (ValueError, TypeError):
                messagebox.showerror("Invalid sell price",
                    "Enter a positive number for the sell price.")
                return
            d = date_entry.get().strip()
            try:
                # Validate format and convert to ISO
                dt = datetime.strptime(d, '%Y-%m-%d')
                d_iso = dt.isoformat()
            except ValueError:
                messagebox.showerror("Invalid date",
                    "Date must be in YYYY-MM-DD format.")
                return

            notes = notes_entry.get().strip()
            closed = self.mgr.sell_holding(ticker, p, d_iso, notes)
            self.mgr.save()

            # Phase 2C: close any open BUY predictions for this ticker as
            # 'sold' so they show up correctly in the track record.
            if (self.predictions_log is not None and _DISCOVER_AVAILABLE):
                try:
                    self.predictions_log.mark_position_sold(ticker, p)
                except Exception:
                    pass

            try:
                win.grab_release()
                win.destroy()
            except Exception:
                pass

            self._render_holdings()

            # Append a one-liner to signals view
            if closed:
                pnl = closed.get('pnl_dollars', 0)
                pct = closed.get('pnl_pct', 0)
                sign = "+" if pnl >= 0 else ""
                self._append_to_signals_top(
                    f"[SOLD {ticker}: {sign}{_format_money(pnl)} "
                    f"({sign}{pct:.1f}%) — recorded in trade history]")

                # Show the full sale acknowledgment dialog
                self._show_sale_acknowledgment(closed)

        tk.Button(btn_row, text="Record Sale", bg=c['green'], fg=c['bg'],
                  relief='flat', padx=20, pady=6,
                  font=('Segoe UI', 10, 'bold'), cursor='hand2',
                  command=_confirm_sell).pack(side='right')
        tk.Button(btn_row, text="Cancel", bg=c['card2'], fg=c['muted'],
                  relief='flat', padx=14, pady=4,
                  font=('Segoe UI', 9), cursor='hand2',
                  command=win.destroy).pack(side='right', padx=(0, 8))

    def _show_sale_acknowledgment(self, closed: dict) -> None:
        """Brief dialog after a sale recording. Shows the trade outcome
        and the running realized total. Deliberately does NOT push any
        buy recommendations — that's a different decision and bundling
        them encourages overtrading after a win.

        For the user specifically: he asked about post-sell buy suggestions,
        and we agreed to defer that to Phase 2C when we have real data
        for picks. This dialog is the acknowledgment-only version.
        """
        c = THEME
        ticker = closed.get('ticker', '?')
        pnl = closed.get('pnl_dollars', 0)
        pct = closed.get('pnl_pct', 0)
        days = closed.get('days_held')
        buy = closed.get('buy_price', 0)
        sell = closed.get('sell_price', 0)
        proceeds = closed.get('total_proceeds', 0)

        win = tk.Toplevel(self.window)
        win.title(f"Sale recorded — {ticker}")
        win.configure(bg=c['bg'])
        win.geometry("440x380")
        win.minsize(380, 340)
        win.transient(self.window)

        # Reserve close button area
        btn_row = tk.Frame(win, bg=c['bg'], height=60)
        btn_row.pack(side='bottom', fill='x', padx=20, pady=12)
        btn_row.pack_propagate(False)

        # Headline (P&L is the star here)
        is_win = pnl >= 0
        headline_color = c['green'] if is_win else c['red']
        sign = "+" if is_win else ""

        tk.Label(win, text="Sale Recorded", bg=c['bg'], fg=c['muted'],
                 font=('Segoe UI', 9)).pack(side='top', pady=(16, 4))

        tk.Label(win, text=ticker, bg=c['bg'], fg=c['accent'],
                 font=('Segoe UI', 14, 'bold')).pack(side='top')

        # Big P&L number
        tk.Label(win,
                 text=f"{sign}{_format_money(pnl)}",
                 bg=c['bg'], fg=headline_color,
                 font=('Segoe UI', 22, 'bold')
                 ).pack(side='top', pady=(8, 0))
        tk.Label(win, text=f"{sign}{pct:.2f}%",
                 bg=c['bg'], fg=headline_color,
                 font=('Segoe UI', 12, 'bold')
                 ).pack(side='top')

        # Trade details
        details_frame = tk.Frame(win, bg=c['card'], padx=14, pady=10)
        details_frame.pack(side='top', fill='x', padx=24, pady=(14, 6))

        days_str = f"{days}d" if days is not None else "?"
        details_text = (
            f"Bought @ {_format_money(buy)}  →  Sold @ {_format_money(sell)}\n"
            f"Held {days_str}   ·   Proceeds: {_format_money(proceeds)}"
        )
        tk.Label(details_frame, text=details_text,
                 bg=c['card'], fg=c['text'],
                 font=('Segoe UI', 9), justify='left'
                 ).pack(side='top', anchor='w')

        # Running stats
        stats = self.mgr.get_realized_stats()
        if stats['count'] > 0:
            stats_frame = tk.Frame(win, bg=c['bg'])
            stats_frame.pack(side='top', fill='x', padx=24, pady=(4, 0))
            total_color = c['green'] if stats['total_pnl'] >= 0 else c['red']
            tot_sign = "+" if stats['total_pnl'] >= 0 else ""
            tk.Label(stats_frame,
                     text=f"Realized total: ",
                     bg=c['bg'], fg=c['muted'],
                     font=('Segoe UI', 9)).pack(side='left')
            tk.Label(stats_frame,
                     text=f"{tot_sign}{_format_money(stats['total_pnl'])} "
                          f"across {stats['count']} trade"
                          f"{'s' if stats['count'] != 1 else ''}",
                     bg=c['bg'], fg=total_color,
                     font=('Segoe UI', 9, 'bold')).pack(side='left')

        # Close + History buttons
        def _open_history():
            try: win.destroy()
            except Exception: pass
            self._show_trade_history()

        tk.Button(btn_row, text="View History", bg=c['card2'], fg=c['amber'],
                  relief='flat', padx=12, pady=4,
                  font=('Segoe UI', 9), cursor='hand2',
                  command=_open_history).pack(side='right', padx=(8, 0))
        tk.Button(btn_row, text="Close", bg=c['green'], fg=c['bg'],
                  relief='flat', padx=18, pady=6,
                  font=('Segoe UI', 10, 'bold'), cursor='hand2',
                  command=win.destroy).pack(side='right')

    def _on_tradable_toggled(self, ticker: str, tradable: bool) -> None:
        self.mgr.update_tradable(ticker, tradable)
        self.mgr.save()
        self._render_holdings()  # re-render to show LOCKED tag

    def _show_trade_history(self) -> None:
        """Show all closed (sold) positions with aggregate stats. This is
        the realized track record — every actual buy/sell pair you've
        recorded, with P&L.
        """
        c = THEME
        win = tk.Toplevel(self.window if (self.window and self.window.winfo_exists()) else self.parent)
        win.title("Trade History — Tired Market")
        win.configure(bg=c['bg'])
        win.geometry("760x600")
        # NOT transient — peer window, survives Holdings closing.

        # Header
        hdr_frame = tk.Frame(win, bg=c['bg'])
        hdr_frame.pack(side='top', fill='x', padx=20, pady=(14, 4))
        tk.Label(hdr_frame, text="Trade History", bg=c['bg'], fg=c['accent'],
                 font=('Segoe UI', 16, 'bold')).pack(side='left')

        # Stats summary card
        stats = self.mgr.get_realized_stats()
        stats_frame = tk.Frame(win, bg=c['card'], padx=16, pady=12)
        stats_frame.pack(side='top', fill='x', padx=20, pady=(8, 8))

        if stats['count'] == 0:
            tk.Label(stats_frame,
                     text="No closed trades yet.\n"
                          "When you sell a position, click Sell on the holding "
                          "and the trade goes here with full P&L.",
                     bg=c['card'], fg=c['muted'],
                     font=('Segoe UI', 9), justify='left'
                     ).pack(side='top', anchor='w')
        else:
            # Top-line stats
            top_stats = tk.Frame(stats_frame, bg=c['card'])
            top_stats.pack(side='top', fill='x')

            total_pnl = stats['total_pnl']
            total_pct = stats['total_pnl_pct']
            pnl_color = c['green'] if total_pnl >= 0 else c['red']
            sign = "+" if total_pnl >= 0 else ""
            tk.Label(top_stats,
                     text=f"Total realized P&L: ",
                     bg=c['card'], fg=c['muted'],
                     font=('Segoe UI', 10)).pack(side='left')
            tk.Label(top_stats,
                     text=f"{sign}{_format_money(total_pnl)} ({sign}{total_pct:.1f}%)",
                     bg=c['card'], fg=pnl_color,
                     font=('Segoe UI', 10, 'bold')).pack(side='left')
            tk.Label(top_stats,
                     text=f"   ·   {stats['count']} closed trades",
                     bg=c['card'], fg=c['muted'],
                     font=('Segoe UI', 10)).pack(side='left')

            # Win/loss breakdown
            wl_frame = tk.Frame(stats_frame, bg=c['card'])
            wl_frame.pack(side='top', fill='x', pady=(8, 0))
            tk.Label(wl_frame,
                     text=f"Win rate: {stats['win_rate']:.0f}%   ·   "
                          f"{stats['wins']}W / {stats['losses']}L",
                     bg=c['card'], fg=c['text'],
                     font=('Segoe UI', 9)).pack(side='left')
            if stats['wins'] > 0 and stats['losses'] > 0:
                tk.Label(wl_frame,
                         text=f"   ·   avg win: +{stats['avg_win_pct']:.1f}%   ·   "
                              f"avg loss: {stats['avg_loss_pct']:.1f}%",
                         bg=c['card'], fg=c['muted'],
                         font=('Segoe UI', 9)).pack(side='left')

            # Best / worst
            if stats['best_trade']:
                bt = stats['best_trade']
                wt = stats['worst_trade']
                bw_frame = tk.Frame(stats_frame, bg=c['card'])
                bw_frame.pack(side='top', fill='x', pady=(6, 0))
                tk.Label(bw_frame,
                         text=f"Best: {bt['ticker']} +{bt.get('pnl_pct', 0):.1f}%   ·   "
                              f"Worst: {wt['ticker']} {wt.get('pnl_pct', 0):.1f}%",
                         bg=c['card'], fg=c['dim'],
                         font=('Segoe UI', 8)).pack(side='left')

        # ── Trade list ──
        tk.Label(win, text="ALL TRADES", bg=c['bg'], fg=c['accent'],
                 font=('Segoe UI', 9, 'bold')
                 ).pack(side='top', anchor='w', padx=20, pady=(8, 4))

        list_outer = tk.Frame(win, bg=c['bg'])
        list_outer.pack(side='top', fill='both', expand=True, padx=20, pady=(0, 8))

        canvas = tk.Canvas(list_outer, bg=c['bg'], highlightthickness=0)
        sb = ttk.Scrollbar(list_outer, orient='vertical', command=canvas.yview)
        list_inner = tk.Frame(canvas, bg=c['bg'])
        list_inner.bind('<Configure>',
            lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.create_window((0, 0), window=list_inner, anchor='nw')
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)

        def _wheel(e):
            canvas.yview_scroll(-3 if e.delta > 0 else 3, 'units')
            return 'break'
        # v4.10.7: removed focus_set on Enter (popup-hiding bug fix)
        canvas.bind('<MouseWheel>', _wheel)

        closed_trades = self.mgr.get_closed()

        if not closed_trades:
            tk.Label(list_inner,
                     text="(no closed trades to show)",
                     bg=c['bg'], fg=c['dim'],
                     font=('Segoe UI', 9, 'italic'),
                     padx=20, pady=20).pack()
        else:
            # Auto-resize the canvas's inner frame width
            list_inner.bind('<Configure>',
                lambda e: canvas.itemconfig(canvas.find_all()[0],
                                             width=canvas.winfo_width()))

            for idx, trade in enumerate(closed_trades):
                self._render_trade_row(list_inner, idx, trade)

        # Close button
        tk.Button(win, text="Close", bg=c['card2'], fg=c['text'],
                  relief='flat', padx=14, pady=4,
                  font=('Segoe UI', 9), cursor='hand2',
                  command=win.destroy).pack(side='bottom', pady=12)

    def _render_trade_row(self, parent, idx: int, trade: dict) -> None:
        c = THEME
        ticker = trade.get('ticker', '?')
        shares = trade.get('shares', 0)
        buy = trade.get('buy_price', 0)
        sell = trade.get('sell_price', 0)
        cost = trade.get('total_cost', 0)
        proceeds = trade.get('total_proceeds', 0)
        pnl = trade.get('pnl_dollars', 0)
        pct = trade.get('pnl_pct', 0)
        days = trade.get('days_held')
        sell_date = trade.get('sell_date', '')
        notes = trade.get('notes', '')

        bg = c['card'] if (idx % 2 == 0) else c['card2']
        row = tk.Frame(parent, bg=bg, padx=12, pady=8)
        row.pack(fill='x', pady=1)

        # Top line: ticker + dates + P&L (right-aligned)
        line1 = tk.Frame(row, bg=bg)
        line1.pack(side='top', fill='x')

        tk.Label(line1, text=ticker, bg=bg, fg=c['accent'],
                 font=('Segoe UI', 11, 'bold')).pack(side='left')

        # Format sell date
        try:
            sd = datetime.fromisoformat(sell_date)
            date_str = sd.strftime('%b %d, %Y').replace(' 0', ' ')
        except Exception:
            date_str = sell_date[:10] if sell_date else ""
        if date_str:
            tk.Label(line1, text=f"  ·  sold {date_str}",
                     bg=bg, fg=c['muted'], font=('Segoe UI', 8)
                     ).pack(side='left')
        if days is not None:
            tk.Label(line1, text=f"  ·  held {days}d",
                     bg=bg, fg=c['muted'], font=('Segoe UI', 8)
                     ).pack(side='left')

        # P&L on right
        pnl_color = c['green'] if pnl >= 0 else c['red']
        sign = "+" if pnl >= 0 else ""
        tk.Label(line1,
                 text=f"{sign}{_format_money(pnl)} ({sign}{pct:.1f}%)",
                 bg=bg, fg=pnl_color,
                 font=('Segoe UI', 10, 'bold')).pack(side='right')

        # Second line: bought / sold prices + position size
        line2 = tk.Frame(row, bg=bg)
        line2.pack(side='top', fill='x', pady=(2, 0))
        tk.Label(line2,
                 text=f"{shares:g} sh   ·   bought @ {_format_money(buy)} "
                      f"({_format_money(cost)})   →   sold @ {_format_money(sell)} "
                      f"({_format_money(proceeds)})",
                 bg=bg, fg=c['muted'],
                 font=('Segoe UI', 8)).pack(side='left')

        # Notes (if any)
        if notes:
            tk.Label(row, text=f"   {notes}", bg=bg, fg=c['dim'],
                     font=('Segoe UI', 8, 'italic'),
                     wraplength=600, justify='left'
                     ).pack(side='top', anchor='w', pady=(2, 0))

    # ─── Phase 2C: Discover panel ───────────────────────────────────────


    def _set_discover_status(self, text, color=None):
        if not hasattr(self, '_discover_status') or self._discover_status is None:
            return
        try:
            cfg = {'text': text}
            if color:
                cfg['fg'] = color
            self._discover_status.config(**cfg)
        except Exception:
            pass

    def _universe_state_text(self):
        if self.universe is None:
            return "    Universe not available."
        n = len(self.universe.tickers)
        src = self.universe.source or "unknown"
        label = self.universe.source_label()
        fa = self.universe.fetched_at
        if fa:
            age = (datetime.now() - fa).days
            age_text = f"{age}d ago" if age > 0 else "today"
        else:
            age_text = "never"
        needs = " (refresh recommended)" if self.universe.needs_refresh() else ""
        return f"    {n} tickers · {label} ({src}) · fetched {age_text}{needs}"


    def run_consensus_scan_headless(self,
                                       models: list[str] | None = None,
                                       on_done: Callable | None = None
                                       ) -> None:
        """v4.9.0: Run a Discover scan against multiple models in sequence,
        producing a comparable set of predictions for the consensus card.

        Each model analyzes the SAME candidate set (via the pre-filter
        cache, no re-fetch). Predictions get a shared scan_id so they
        can be grouped later.

        Models that aren't installed in Ollama are skipped with a log
        line; the scan continues with the rest.

        v4.13.46: Honors inference_mode setting.
        - 'api' mode  : Bypass the multi-model Ollama loop entirely.
                         Delegate to run_discover_scan_headless ONCE,
                         which (via v4.13.45) calls APIs only and skips
                         Ollama. Result: one API call per candidate per
                         provider, instead of N (Ollama models) × M
                         (candidates) × P (providers) which would burn
                         daily quotas in seconds.
        - 'local' mode: Run Ollama loop as before. v4.13.45's local-mode
                         skip prevents API spam in this path.
        - 'hybrid'    : Original v4.9.0 behavior (Ollama loop + APIs
                         per candidate via v4.13.44 hook). Auto-switches
                         to api mode if a configured game is running.

        Behavior under load/cooldown:
          - Refuses if a regular Discover or another consensus scan is
            already running
          - Refuses if AI is paused
          - Refuses if a rate-limit cooldown is active (background-style:
            no override — orchestrators shouldn't second-guess cooldown)
          - If a cooldown triggers DURING the scan (model 1 succeeds,
            model 2 hits a rate limit), the remaining models skip
            cleanly with a clear log line

        Args:
            models: list of Ollama model names to run, or None to read
                from cfg['consensus_models']
            on_done: callback when the entire consensus scan finishes
                (success, partial, or refused). Called once.
        """
        # v4.14.5.14-ollama-purge-3b1: cloud-only. The local Ollama multi-model
        # loop was removed; delegate unconditionally to a single discover pass
        # (one call per enabled API provider per candidate).
        self._log_to_main(
            "Consensus scan: delegating to a single discover pass "
            "(one call per enabled API provider per candidate).",
            'muted')
        try:
            self.run_discover_scan_headless(model=None, on_done=on_done)
        except Exception as e:
            self._log_to_main(
                f"Consensus scan: failed to start discover pass: {e}", 'red')
            if on_done is not None:
                try: on_done()
                except Exception: pass
        return

    def run_all_paths_scan(self, model: str | None = None,
                            on_done: Callable | None = None) -> None:
        """v4.13.11: Scan every path sequentially with one model.
        Each path uses adaptive scanning. Restores original global
        path when done. Cancellable via the Stop button.
        """
        if self._discover_running or self._consensus_running:
            self._log_to_main(
                "Scan All Paths refused - a scan is already running.",
                'amber')
            if on_done is not None:
                try: on_done()
                except Exception: pass
            return
        if is_ai_paused():
            self._log_to_main(
                "Can't run Scan All Paths - AI is paused.",
                'amber')
            if on_done is not None:
                try: on_done()
                except Exception: pass
            return

        # v4.14.5.14-ollama-purge-3b1: cloud-only — no local-model pick/refuse;
        # each path delegates to run_discover_scan_headless (cloud).
        model = None

        original_path = self.get_path()
        path_keys = list(PATHS.keys())  # 5 paths

        self._log_to_main(
            f"=== Scan All Paths started ({len(path_keys)} paths, "
            f"model={model}) ===",
            'purple' if 'purple' in THEME else 'green')
        self._log_to_main(
            f"Will scan: {', '.join(path_keys)}. Original path "
            f"({original_path}) restored at end.", 'muted')

        self._all_paths_running = True

        # v4.13.13: cooldown-aware sweep. If yfinance trips its rate
        # limiter mid-sweep (which is what bit the first real run), we
        # WAIT through the cooldown instead of silently skipping the
        # rest of the paths. Clicking Scan All once and walking away
        # now actually fills in all 5 paths.
        SWEEP_MAX_WAIT_MIN = 90  # max minutes to wait per cooldown cycle
        SWEEP_CHECK_INTERVAL_SEC = 10  # how often to re-check during wait

        def _wait_for_cooldown(path_key):
            """Block until cooldown clears or SWEEP_MAX_WAIT_MIN elapses.
            Returns True if cooldown cleared, False if we gave up.
            Also returns False if user hit Stop (sweep cancelled)."""
            try:
                cd = tm_discover.get_cooldown_status()
            except Exception:
                return True  # If we can't read it, assume clear
            if not cd.get('active'):
                return True
            remaining = cd.get('remaining_min', 0)
            until = cd.get('cooldown_until_str', '?')
            self._log_to_main(
                f"{path_key}: cooldown active until {until} ET "
                f"({remaining}min). Waiting...",
                'amber')
            elapsed_sec = 0
            max_wait_sec = SWEEP_MAX_WAIT_MIN * 60
            while elapsed_sec < max_wait_sec:
                if not self._all_paths_running:
                    return False  # sweep cancelled
                time.sleep(SWEEP_CHECK_INTERVAL_SEC)
                elapsed_sec += SWEEP_CHECK_INTERVAL_SEC
                try:
                    cd = tm_discover.get_cooldown_status()
                except Exception:
                    return True
                if not cd.get('active'):
                    self._log_to_main(
                        f"{path_key}: cooldown cleared after "
                        f"{elapsed_sec // 60}min. Resuming.",
                        'green')
                    return True
            self._log_to_main(
                f"{path_key}: cooldown still active after "
                f"{SWEEP_MAX_WAIT_MIN}min wait. Giving up on this path.",
                'red')
            return False

        def _count_preds_on_path(path_key):
            """Count predictions tagged with this path. Used to detect
            whether a per-path scan actually did any work."""
            try:
                if self.predictions_log is not None:
                    return sum(1 for p in self.predictions_log.get_all()
                                if p.get('path') == path_key)
            except Exception:
                pass
            return 0

        def _bg_sweep():
            try:
                aborted = False
                for idx, path_key in enumerate(path_keys, 1):
                    if not self._all_paths_running:
                        self._log_to_main(
                            "Scan All Paths cancelled.", 'amber')
                        return
                    self._log_to_main(
                        f"--- Path {idx}/{len(path_keys)}: "
                        f"{path_key} ---",
                        'purple' if 'purple' in THEME else 'green')

                    # v4.13.13: pre-flight cooldown wait. If active when
                    # we start this path, wait it out before scanning.
                    if not _wait_for_cooldown(path_key):
                        if not self._all_paths_running:
                            return  # cancelled
                        # Gave up after max wait -> abort entire sweep
                        self._log_to_main(
                            f"Aborting remainder of Scan All Paths "
                            f"due to persistent cooldown.", 'red')
                        aborted = True
                        break

                    try:
                        self.set_path(path_key)
                    except Exception as e:
                        self._log_to_main(
                            f"Couldn't switch to {path_key}: {e}",
                            'red')
                        continue

                    # v4.13.13: track predictions before so we can
                    # detect "scan completed but added nothing" which
                    # almost always means cooldown engaged mid-scan.
                    preds_before = _count_preds_on_path(path_key)

                    path_done = threading.Event()
                    def _on_path_done(ev=path_done):
                        ev.set()
                    try:
                        self.run_discover_scan_headless(
                            model=model, on_done=_on_path_done)
                    except Exception as e:
                        self._log_to_main(
                            f"{path_key}: scan failed to start: {e}",
                            'red')
                        continue
                    if not path_done.wait(timeout=60 * 60):
                        self._log_to_main(
                            f"{path_key}: scan timed out at 60min, "
                            f"moving to next path.", 'amber')
                        continue

                    if not self._all_paths_running:
                        return  # cancelled mid-scan

                    # v4.13.13: did the scan actually do anything? If
                    # zero predictions added AND cooldown is now active,
                    # cooldown engaged mid-scan -> wait and retry once.
                    preds_after = _count_preds_on_path(path_key)
                    if preds_after == preds_before:
                        try:
                            cd = tm_discover.get_cooldown_status()
                        except Exception:
                            cd = {'active': False}
                        if cd.get('active'):
                            self._log_to_main(
                                f"{path_key}: scan added 0 predictions "
                                f"and cooldown is now active. Waiting "
                                f"and retrying once.",
                                'amber')
                            if not _wait_for_cooldown(path_key):
                                if not self._all_paths_running:
                                    return
                                self._log_to_main(
                                    f"Aborting remainder due to "
                                    f"persistent cooldown.", 'red')
                                aborted = True
                                break
                            # Retry the path
                            path_done2 = threading.Event()
                            def _on_retry_done(ev=path_done2):
                                ev.set()
                            try:
                                self.run_discover_scan_headless(
                                    model=model, on_done=_on_retry_done)
                            except Exception as e:
                                self._log_to_main(
                                    f"{path_key}: retry failed to "
                                    f"start: {e}", 'red')
                                continue
                            if not path_done2.wait(timeout=60 * 60):
                                self._log_to_main(
                                    f"{path_key}: retry timed out.",
                                    'amber')

                if aborted:
                    self._log_to_main(
                        "=== Scan All Paths ABORTED ===",
                        'red')
                else:
                    self._log_to_main(
                        "=== Scan All Paths complete ===",
                        'purple' if 'purple' in THEME else 'green')
            finally:
                self._all_paths_running = False
                try:
                    self.set_path(original_path)
                    self._log_to_main(
                        f"Restored path to {original_path}.", 'muted')
                except Exception:
                    pass
                if on_done is not None:
                    try: on_done()
                    except Exception: pass

        threading.Thread(target=_bg_sweep, daemon=True).start()

    def run_discover_scan_headless(self, model: str | None = None,
                                     on_done: Callable | None = None) -> None:
        """Run a Discover scan WITHOUT opening the Discover panel.

        Streams progress to the main activity log via self.log_callback.
        Writes predictions to predictions_log just like a panel scan
        would. Honors the same path/universe settings.

        This is what the main-window "Discover" button calls when we
        want a one-click scan with no popup. Result cards aren't rendered
        anywhere visual — predictions are still in Track Record.

        Pre-flight check: refuses to run if path/universe combo is
        obviously incompatible. Logs an explanation and returns.

        Args:
            model: model name to use, or None to auto-pick from fallback list
            on_done: optional callback fired when scan completes (success
                or failure). Useful for re-enabling a button.
        """
        if self._discover_running:
            self._log_to_main(
                "Discover already running — wait for it to finish.",
                'amber')
            if on_done is not None:
                try: on_done()
                except Exception: pass
            return

        if is_ai_paused():
            self._log_to_main(
                "Can't run Discover — AI is paused. Click the badge to resume.",
                'amber')
            if on_done is not None:
                try: on_done()
                except Exception: pass
            return

        # v4.8.12: Background-scheduler scans are STRICTLY blocked during
        # cooldown (no override — the scheduler can't make a sensible
        # judgment about whether the limit really cleared). (The old manual
        # Discover-window override path was removed in the Ollama exit.)
        try:
            cd = tm_discover.get_cooldown_status()
            if cd.get('active'):
                self._log_to_main(
                    f"Skipping background Discover — rate-limit cooldown "
                    f"active until {cd['cooldown_until_str']} ET "
                    f"({cd['remaining_min']}min remaining). Will resume "
                    f"after that.",
                    'amber')
                if on_done is not None:
                    try: on_done()
                    except Exception: pass
                return
        except Exception:
            pass  # cooldown check is best-effort; never block on its failure

        if self.universe is None or self.watchlist is None:
            self._log_to_main(
                "Discover not available (universe or watchlist missing).",
                'red')
            if on_done is not None:
                try: on_done()
                except Exception: pass
            return

        path = self.get_path()
        source_key = self.universe.current_source

        # Pre-flight: is this path/universe combo sane?
        if _DISCOVER_AVAILABLE:
            ok, warning = tm_discover.check_path_universe_compat(
                path, source_key)
            if not ok:
                self._log_to_main(f"Discover refused: {warning}", 'amber')
                self._log_to_main(
                    "Change path or universe and try again.", 'muted')
                if on_done is not None:
                    try: on_done()
                    except Exception: pass
                return

        # Auto-refresh universe if empty
        if not self.universe.tickers:
            self._log_to_main(
                f"Discover: universe is empty, fetching "
                f"{self.universe.source_label()}...", 'muted')
            ok = self.universe.refresh(
                log_fn=lambda m: self._log_to_main(f"  {m}", 'muted'))
            if not self.universe.tickers:
                self._log_to_main(
                    "Discover: universe still empty after refresh attempt. "
                    "Aborting.", 'red')
                if on_done is not None:
                    try: on_done()
                    except Exception: pass
                return

        # v4.14.5.14-ollama-purge-3b1: cloud-only — `model` is unused (the cloud
        # candidate-analyze ignores it). No local-model pick / no-Ollama refuse.

        self._discover_running = True
        self._headless_scan_done = on_done  # Stored so abort path can call it

        path_info = PATHS.get(path, PATHS[DEFAULT_PATH])
        self._log_to_main(
            f"Discover scan started: {self.universe.source_label()} · "
            f"path={path_info['name']} · model={model} · "
            f"{len(self.watchlist.tickers)} watchlist tickers.",
            'purple' if 'purple' in THEME else 'green')

        held_tickers = {h.get('ticker', '').upper()
                          for h in self.mgr.holdings}
        cache_key = (source_key, path,
                     datetime.now().strftime('%Y-%m-%d'))

        def _bg_run():
            try:
                # 1. Watchlist analyses
                wl_entries = self.watchlist.tickers
                if wl_entries:
                    self._log_to_main(
                        f"Discover: analyzing {len(wl_entries)} watchlist "
                        f"tickers...", 'muted')
                    for entry in wl_entries:
                        if is_ai_paused() or not self._discover_running:
                            return
                        self._headless_analyze_candidate(
                            ticker=entry.get('ticker', ''),
                            notes=entry.get('notes', ''),
                            path=path,
                            model=model,
                            source='discover_watchlist',
                        )

                # 2. Universe pre-filter
                self._log_to_main(
                    f"Discover: pre-filtering "
                    f"{len(self.universe.tickers)} tickers from "
                    f"{self.universe.source_label()}...", 'muted')

                last_progress = [time.time()]
                last_phase = ['']
                def _progress(idx, total, phase='batch_fetch'):
                    # Throttle progress logs to every 5 seconds. Earlier
                    # versions used 3 seconds but that still produced a
                    # noisy log on big universes. Distinguish the batch
                    # fetch (slow, network-bound) from the filter loop
                    # (fast, local) so the user doesn't think it's
                    # restarting at 1900/2000.
                    now = time.time()
                    phase_changed = phase != last_phase[0]
                    if phase_changed:
                        last_phase[0] = phase
                        if phase == 'filter_loop':
                            self._log_to_main(
                                "Discover: applying filters... "
                                "(this is fast, takes 1-3 seconds)",
                                'muted')
                            return
                        elif phase == 'filter_done':
                            return  # Final summary logged by caller
                    if now - last_progress[0] >= 5.0 or idx == total:
                        last_progress[0] = now
                        if idx < total and phase == 'batch_fetch':
                            self._log_to_main(
                                f"Discover: fetching quotes... "
                                f"{idx}/{total}",
                                'muted')

                quote_fn = lambda t: self.cache.quote(t)

                # v4.13.0: compute history-based scoring so the prefilter
                # demotes consistently-AVOIDed stocks and promotes consistently-
                # BUY'd ones. Toggleable via cfg['prefilter_history_enabled']
                # (default True).
                history_scores = {}
                history_enabled = bool(self.cfg.get(
                    'prefilter_history_enabled', True)) if self.cfg else True
                # v4.14.5.19-accuracy-weighted-consensus: when the flag is
                # on and we have a DB connection via self.app.db, pass a
                # memoized weight_lookup so historical predictions are
                # scored by their model's Wilson-lower-bound accuracy.
                # Cold-start safe: every n=0/thin model returns neutral
                # 5.0, which divides by NEUTRAL_WEIGHT inside the scorer
                # back to 1.0 per prediction == byte-identical to the
                # pre-patch flat formula. Pass None on any flag-off /
                # missing-conn / lookup-failure path so behavior falls
                # back to flat.
                _weight_lookup = None
                try:
                    _acc_enabled = bool((self.cfg or {}).get(
                        'use_accuracy_weighted_consensus', True))
                    _app = getattr(self, 'app', None)
                    _db = getattr(_app, 'db', None) if _app else None
                    if _acc_enabled and _db is not None and _db.conn:
                        import tm_source_accuracy as _tsa
                        _wcache = {}
                        def _weight_lookup(model_label,
                                            _db=_db, _tsa=_tsa, _wcache=_wcache):
                            if model_label in _wcache:
                                return _wcache[model_label]
                            try:
                                with _db.lock:
                                    w = _tsa.model_consensus_weight(
                                        _db.conn, model_label)
                            except Exception:
                                w = _tsa.NEUTRAL_WEIGHT
                            _wcache[model_label] = w
                            return w
                except Exception:
                    _weight_lookup = None
                try:
                    history_scores = tm_discover.compute_history_scores(
                        self.predictions_log, path,
                        weight_lookup=_weight_lookup)
                except Exception:
                    history_scores = {}

                # v4.13.11: bumped from 15 to 50 to give adaptive
                # AI loop room to extend if BUYs are scarce.
                candidates = tm_discover.filter_candidates(
                    tickers=self.universe.tickers,
                    quote_fn=quote_fn,
                    news_count_fn=None,
                    path=path,
                    held_tickers=held_tickers | {
                        e.get('ticker', '').upper() for e in wl_entries
                    },
                    max_results=50,
                    progress_fn=_progress,
                    cancel_fn=lambda: not self._discover_running,
                    cache_key=cache_key,
                    use_batch=True,
                    on_batch_quote=lambda t, q: self.cache.seed(t, 'quote', q),
                    universe=self.universe,
                    history_scores=history_scores,
                    history_scores_enabled=history_enabled,
                )

                if not self._discover_running:
                    return

                # 3. Skip recently-analyzed
                # v4.8.12: configurable via cfg['include_recently_analyzed'].
                # Default off (preserves current dedup behavior). Turn on
                # for A/B model tests where you WANT to re-analyze the same
                # candidates with a different model.
                pre_filter_count = len(candidates)
                include_recent = bool(self.cfg.get(
                    'include_recently_analyzed', False)) if hasattr(
                    self, 'cfg') and self.cfg is not None else False
                if include_recent:
                    skipped = 0
                else:
                    recently = tm_discover.recently_analyzed_tickers(
                        self.predictions_log, hours=24)
                    recently |= {e.get('ticker', '').upper()
                                  for e in wl_entries}
                    candidates = [c for c in candidates
                                    if c.get('ticker', '').upper()
                                    not in recently]
                    skipped = pre_filter_count - len(candidates)

                # v4.8.12: Detect rate-limiting BEFORE deciding what to
                # report. Both pre-filter pass-count AND rate-limit are
                # independent signals — we want to surface both correctly.
                rej = tm_discover.get_last_filter_rejections()
                is_rate_limited, severity = (
                    tm_discover.detect_rate_limit_in_rejections(rej))
                if is_rate_limited:
                    try:
                        tm_discover.record_rate_limit_event(
                            severity, source='yfinance')
                    except Exception:
                        pass
                else:
                    # Healthy scan — clear any active cooldown
                    try:
                        # Estimate quote success: 100% minus no_quote share
                        total_rej = sum(rej.values())
                        if total_rej > 0:
                            no_quote_pct = (rej.get('no_quote', 0)
                                            / total_rej * 100)
                            quote_success = 100.0 - no_quote_pct
                        else:
                            quote_success = 100.0
                        tm_discover.record_successful_scan(quote_success)
                    except Exception:
                        pass

                # 4. Report pre-filter results — distinguish the two
                # failure modes the original code conflated:
                #   (a) pre-filter itself returned 0 (rate limit, bad
                #       universe/path combo, etc.)
                #   (b) pre-filter returned candidates but dedup killed
                #       them all (you scanned the same universe recently)
                if not candidates:
                    if pre_filter_count == 0:
                        # Case (a): pre-filter genuinely zero
                        explanation = tm_discover.explain_zero_candidates(
                            rej, path, self.universe.source_label())
                        self._log_to_main(
                            f"Discover: pre-filtered "
                            f"{len(self.universe.tickers)} → 0 candidates.",
                            'amber')
                        self._log_to_main(
                            f"Discover: {explanation}", 'amber')
                    else:
                        # Case (b): pre-filter found N, dedup killed all
                        self._log_to_main(
                            f"Discover: pre-filtered "
                            f"{len(self.universe.tickers)} → "
                            f"{pre_filter_count} candidates, but all "
                            f"{skipped} were analyzed in the last 24 hours "
                            f"(skipped). Turn on 'Include recently-analyzed' "
                            f"in Settings if you want to re-scan them with "
                            f"a different model.",
                            'amber')
                        # Even with dedup-zero, surface a rate-limit warning
                        # if we also detected one — both can be true at once
                        if is_rate_limited:
                            self._log_to_main(
                                f"Discover: also seeing rate-limiting from "
                                f"yfinance ({severity:.0f}% no-quote). "
                                f"Cooldown active.",
                                'amber')
                    self._log_to_main(
                        "Discover: scan complete (no candidates).", 'green')
                    return
                else:
                    msg = (f"Discover: pre-filtered "
                           f"{len(self.universe.tickers)} → "
                           f"{pre_filter_count} candidates"
                           + (f" → {len(candidates)} after dedup "
                              f"({skipped} skipped — analyzed in last 24h)"
                              if skipped else ""))
                    self._log_to_main(msg, 'muted')

                # 5. AI score candidates ADAPTIVELY (v4.13.11).
                # Score the first 15 unconditionally, then keep going
                # only as long as we're under target_buys (5).
                ADAPTIVE_INITIAL_BATCH = 15
                ADAPTIVE_TARGET_BUYS = 5
                self._log_to_main(
                    f"Discover: AI scoring up to {len(candidates)} "
                    f"candidates with {model} (adaptive: stop after "
                    f"{ADAPTIVE_TARGET_BUYS} BUYs once "
                    f"{ADAPTIVE_INITIAL_BATCH} have been scored)...",
                    'muted')

                # Snapshot prediction count BEFORE this scan starts
                # so we can count BUYs added during THIS scan only.
                _initial_pred_count = 0
                try:
                    if self.predictions_log is not None:
                        _initial_pred_count = len(
                            self.predictions_log.get_all())
                except Exception:
                    pass

                # v4.14.0 stage 7.1: open a scan-run dedup window so
                # repetitive router skip lines (e.g. SambaNova blocked
                # on scan, Gemini in cooldown) emit ONCE per scan
                # rather than once per candidate. The next call to
                # begin_scan_run() resets the tracker, so even if
                # this scan early-returns without reaching the
                # corresponding end_scan_run() below, the next scan
                # starts with fresh dedup state.
                try:
                    import tm_ai_router as _router_s71a
                    _router_s71a.begin_scan_run()
                except Exception:
                    pass

                completed = 0
                for cand in candidates:
                    if is_ai_paused() or not self._discover_running:
                        return
                    if completed >= ADAPTIVE_INITIAL_BATCH:
                        # Count BUYs added since this scan started
                        _new_buys = 0
                        try:
                            if self.predictions_log is not None:
                                _all_preds = self.predictions_log.get_all()
                                for p in _all_preds[_initial_pred_count:]:
                                    if p.get('direction') == 'BUY':
                                        _new_buys += 1
                        except Exception:
                            pass
                        if _new_buys >= ADAPTIVE_TARGET_BUYS:
                            self._log_to_main(
                                f"Discover: stopped at {completed} "
                                f"candidates - found {_new_buys} BUYs "
                                f"(target {ADAPTIVE_TARGET_BUYS}).",
                                'green')
                            break
                    completed += 1
                    self._log_to_main(
                        f"Discover: ({completed}/{len(candidates)}) "
                        f"analyzing {cand.get('ticker', '?')}...",
                        'muted')
                    self._headless_analyze_candidate(
                        ticker=cand.get('ticker', ''),
                        notes='',
                        path=path,
                        model=model,
                        source='discover_scan',
                    )

                self._log_to_main(
                    f"Discover: scan complete ({completed} candidates "
                    f"scored). Check Track Record to review predictions.",
                    'green')

            except Exception as e:
                err = str(e)
                self._log_to_main(f"Discover error: {err}", 'red')
            finally:
                # v4.14.0 stage 7.1: close the scan-run dedup window.
                try:
                    import tm_ai_router as _router_s71b
                    _router_s71b.end_scan_run()
                except Exception:
                    pass
                self._discover_running = False
                cb = self._headless_scan_done
                self._headless_scan_done = None
                if cb is not None:
                    try: cb()
                    except Exception: pass

        threading.Thread(target=_bg_run, daemon=True).start()


    def _headless_analyze_candidate(self, ticker: str, notes: str,
                                      path: str, model: str,
                                      source: str) -> None:
        """Headless candidate analyze — no UI rendering, only logs to
        activity. Used by run_discover_scan_headless.

        v4.13.45: Honors inference_mode setting. In 'api' mode (or
        hybrid+game), skips the local Ollama call entirely and goes
        straight to API providers. In 'local' mode, runs Ollama only
        (no APIs). In 'hybrid' (default), runs both as v4.13.44 did.
        """
        if not ticker:
            return
        ticker = ticker.upper()

        prompt = self._build_candidate_prompt(ticker, notes, path)

        # v4.14.5.14-ollama-purge-3b1: cloud-only. The local Ollama dispatch was
        # removed; always run the enabled API providers against the prompt.
        try:
            import tm_api_providers as _tmap45
            count = _tmap45.run_apis_for_scan_prediction(
                prompt=prompt,
                ticker=ticker,
                path=path,
                source=source,
                predictions_log=self.predictions_log,
                scan_provider_filter=(
                    (getattr(self, 'cfg', None) or {})
                        .get('scan_api_provider')),
            )
            self._log_to_main(
                f"Discover: {ticker} analyzed via API "
                f"({count} provider(s) voted)",
                'green' if count > 0 else 'amber')
        except Exception as e:
            self._log_to_main(
                f"Discover: {ticker} API call failed: "
                f"{type(e).__name__}: {str(e)[:60]}",
                'red')
        return




    # ─── v4.13.3: Discover sort + actionability re-ordering ──────────────

    # Sort priority for AI directions on Discover results.
    # Lower number = appears higher in the list = more actionable.
    # The "owned" flag flips priorities so SELL/TRIM/HOLD/AVOID on owned
    # positions are far more relevant than on stocks the user doesn't own.
    _DISCOVER_SORT_BUCKETS = {
        ('BUY',     False): 1,   # new opportunity
        ('SELL',    True ): 2,   # exit signal — own
        ('TRIM',    True ): 2,   # exit signal — own
        ('HOLD',    True ): 3,   # confirmation — own
        ('AVOID',   True ): 4,   # reconsider position
        ('BUY',     True ): 5,   # already own — buy more (rare)
        ('HOLD',    False): 6,   # info only
        ('SELL',    False): 7,   # don't own + sell signal — irrelevant
        ('TRIM',    False): 7,
        ('AVOID',   False): 8,   # background noise
        ('NO_CALL', True ): 9,
        ('NO_CALL', False): 9,
    }
    _DISCOVER_CONF_RANK = {'HIGH': 0, 'MODERATE': 1, 'LOW': 2, '': 3, None: 3}

    def _discover_held_tickers(self) -> set[str]:
        """Tickers the user currently owns (any status). Used to flag 'OWNED'
        rows in Discover and to drive the sort priority."""
        try:
            return {h.get('ticker', '').upper() for h in self.mgr.holdings}
        except Exception:
            return set()

    def _discover_sort_key(self, row) -> tuple:
        """Return a sort tuple for a Discover result row.
        Reads the metadata stashed onto the widget at render time."""
        meta = getattr(row, '_tm_sort_meta', None) or {}
        direction = (meta.get('direction') or 'NO_CALL').upper()
        # Treat BUYMORE / BUY MORE as BUY for sort purposes
        if direction in ('BUYMORE', 'BUY MORE'):
            direction = 'BUY'
        owned = bool(meta.get('owned', False))
        bucket = self._DISCOVER_SORT_BUCKETS.get((direction, owned), 99)
        conf = (meta.get('confidence') or '').upper()
        conf_rank = self._DISCOVER_CONF_RANK.get(conf, 3)
        ticker = (meta.get('ticker') or '~').upper()
        return (bucket, conf_rank, ticker)

    def _resort_discover_panel(self, container) -> None:
        """Re-pack all rows in the given container in actionability order.
        Inserts subtle dividers between buckets so groups are visually
        distinct."""
        if container is None:
            return
        try:
            children = [w for w in container.winfo_children()
                        if hasattr(w, '_tm_sort_meta')]
        except Exception:
            return
        if not children:
            return

        # Sort into priority order
        children.sort(key=self._discover_sort_key)

        # Group label per bucket — only shown if at least one row matches
        bucket_labels = {
            1: ("ACTION — NEW BUYS",                'green'),
            2: ("ACTION — EXIT SIGNALS ON YOUR POSITIONS", 'red'),
            3: ("HOLDS ON YOUR POSITIONS",          'amber'),
            4: ("RECONSIDER — AVOID ON YOUR POSITIONS", 'amber'),
            5: ("BUY MORE — YOUR POSITIONS",        'green'),
            6: ("HOLDS — NOT OWNED",                'muted'),
            7: ("EXIT SIGNALS — NOT OWNED",         'muted'),
            8: ("AVOID — NOT OWNED",                'dim'),
            9: ("NO CLEAR CALL",                    'dim'),
        }

        # Detach all sorted rows + remove any old dividers we created
        try:
            for w in container.winfo_children():
                if getattr(w, '_tm_sort_divider', False):
                    try: w.destroy()
                    except Exception: pass
                else:
                    try: w.pack_forget()
                    except Exception: pass
        except Exception:
            pass

        # Re-pack with dividers between buckets
        c = THEME
        last_bucket = None
        for row in children:
            key = self._discover_sort_key(row)
            bucket = key[0]
            if bucket != last_bucket and bucket in bucket_labels:
                # Add a divider label
                label_text, color_name = bucket_labels[bucket]
                color = c.get(color_name) or c.get('muted') or '#888888'
                divider = tk.Frame(container, bg=c['bg'])
                divider._tm_sort_divider = True
                divider.pack(side='top', fill='x', pady=(8, 2))
                tk.Label(divider, text=f"── {label_text} ──",
                         bg=c['bg'], fg=color,
                         font=('Segoe UI', 8, 'bold'),
                         anchor='w'
                         ).pack(side='left', padx=4)
                last_bucket = bucket
            try:
                row.pack(side='top', fill='x', pady=2)
            except Exception:
                pass

    def _resort_all_discover_panels(self) -> None:
        """Convenience: re-sort both watchlist + scan result containers
        if they exist on this Holdings panel instance."""
        for attr in ('_discover_results_frame',):
            frame = getattr(self, attr, None)
            if frame is None:
                continue
            try:
                # The frame contains section frames (wl_section, scan_section)
                # whose children include the result containers.
                for section in frame.winfo_children():
                    for sub in section.winfo_children():
                        # The actual result container has rows with _tm_sort_meta
                        if any(hasattr(c, '_tm_sort_meta')
                               for c in sub.winfo_children()):
                            self._resort_discover_panel(sub)
            except Exception:
                pass

    def _build_candidate_prompt(self, ticker, notes, path,
                                multi_paths=None):
        # v4.14.5.14c-p2: one-ticker-ALL-paths. When `multi_paths` is a
        # non-empty list the prompt evaluates this ONE ticker for EACH
        # listed path in a single call (caller computes eligibility).
        # Goal-line TEXT stays here in tm_holdings.PATHS (so the
        # deferred paths-as-pace migration rewrites it without touching
        # the dispatch/parser); the structured per-path OUTPUT block is
        # tm_discover.format_multi_path_prediction_request_block (Patch
        # 1). multi_paths=None / [] → EXACTLY today's single-path
        # prompt, byte-identical (every existing caller unaffected).
        try:
            _mp = [str(p).strip() for p in (multi_paths or [])
                   if str(p).strip()]
        except Exception:
            _mp = []
        # v4.14.6.6-tier1-singlepath-prompt (2026-06-11): only use the
        # multi-path scaffolding when a ticker is genuinely eligible for
        # 2+ paths. Under price-band tiers each ticker is in exactly ONE
        # band (bands are mutually exclusive by price), so len(_mp) is
        # always 1 — and the unified prompt's `=== PATH: <key> ===`
        # header requirement was the source of every "unparseable"
        # response (long prompt → truncation before DIRECTION; opaque
        # band keys → models substitute display labels that fail the
        # header regex). Bypassing it for the size-1 case routes through
        # the simple single-path prompt that parse_prediction handles
        # robustly. The multi-path machinery stays in code for any
        # future tier model that reintroduces multi-eligibility.
        _unified = bool(_mp) and len(_mp) > 1
        path_info = PATHS.get(path, PATHS[DEFAULT_PATH])

        quote = self.cache.quote(ticker) or {}
        technicals = self.cache.technicals(ticker) or {}
        news = self.cache.news_features(ticker) or {}

        if _unified:
            lines = [
                f"You are evaluating {ticker} as a potential new "
                f"position for the user (he does not own it). Evaluate it "
                f"SEPARATELY for EACH strategy path below — the same "
                f"fundamentals/technicals/news, only the entry bar "
                f"differs per path:",
                "",
            ]
            for _pk in _mp:
                _pi = PATHS.get(_pk, PATHS[DEFAULT_PATH])
                lines.append(
                    f"- PATH {_pk}: {_pi['name']} — "
                    f"{_pi.get('description', '')}")
            lines.append("")
        else:
            lines = [
                f"You are evaluating {ticker} as a potential new "
                f"position for "
                f"the user. He doesn't currently own this — should he "
                f"consider entering?",
                "",
                f"PATH/GOAL: {path_info['name']} — "
                f"{path_info.get('description', '')}",
                "",
            ]
        if notes:
            lines.append(f"USER'S NOTE ON THIS TICKER: {notes}")
            lines.append("")

        price = quote.get('price')
        if price:
            change = quote.get('change_pct', 0)
            volume = quote.get('volume', 0)
            lines.append("CURRENT QUOTE:")
            lines.append(f"  price: ${price:g}")
            lines.append(f"  day change: {change:+.2f}%")
            if volume:
                lines.append(f"  volume: {volume:,.0f} shares")
            lines.append("")

        # v4.14.1 stage 5: route TECHNICALS + RECENT NEWS sections
        # (plus the new FACTS / EARNINGS / FILINGS blocks) through
        # tm_context_builder. Candidate variant uses the full block
        # set — the user is evaluating an entry decision so technicals,
        # fundamentals, news, earnings, and filings are all relevant.
        try:
            import tm_context_builder
            # v4.14.5.14c-p2: unified call must carry the SUPERSET of
            # every eligible path's data blocks. 'moderate' has no
            # _PATH_BLOCK_OVERRIDES entry so it inherits the full
            # prompt-kind block set (FACTS/NEWS/EARNINGS/FILINGS/
            # TECHNICALS/MACRO/SOCIAL) — i.e. the superset. Single-path
            # mode is unchanged (uses its own path).
            _ctx_path = 'moderate' if _unified else path
            data_context = tm_context_builder.build_context(
                ticker=ticker,
                path=_ctx_path,
                cache=self.cache,
                log_callback=None,
                # v4.14.2 stage 4: 'candidate' kind includes MACRO for
                # slow_safe / moderate paths; aggressive / lottery
                # paths skip MACRO + FILINGS for speed via the
                # _PATH_BLOCK_OVERRIDES table.
                prompt_kind='candidate',
            )
        except Exception:
            data_context = ""
        if data_context:
            lines.append(data_context)
            lines.append("")

        # v4.14.2 stage 7: epistemic humility prepend (candidate
        # variant). If the assembled context flagged source
        # disagreement, the AI is asked to reason about uncertainty
        # explicitly and lean toward WATCH over BUY when the picture
        # is genuinely mixed.
        try:
            import tm_context_builder as _ctx
            _humility = _ctx.get_disagreement_context(data_context)
        except Exception:
            _humility = None
        if _humility:
            lines.append(_humility.rstrip())
            lines.append("")

        # v4.15.0 Step 16: inject user preferences just before QUESTION.
        # Read via the same PromptBuilder helper so candidate prompts get the
        # same flavoring as holding/locked analysis.
        try:
            if self.prompt_builder is not None:
                _user_pref = self.prompt_builder._get_user_preferences_line()
                if _user_pref:
                    lines.append(_user_pref)
                    lines.append("")
        except Exception:
            pass

        lines.append("QUESTION:")
        # v4.14.2 stage 4: vocabulary fix. Pre-stage-4 this asked for
        # BUY / HOLD / AVOID; HOLD is meaningless on tickers the user
        # doesn't own. WATCH replaces HOLD with the right semantics:
        # "interesting, wait for a better entry / clearer signal."
        lines.append(f"  Should the user enter a position in {ticker}? "
                     "Consider his path, current technicals, and news. "
                     "If you'd say BUY, give a specific entry zone and "
                     "exit levels. If WATCH, say what would change "
                     "your mind to BUY. If AVOID, say why.")
        # v4.14.5.82-discovery-unlock (peak-safety hardening): one
        # explicit overextension instruction. Today the model can
        # infer overextension from raw technicals (RSI / Bollinger
        # position / mean-reversion z-score all reach the prompt) and
        # the mandatory BUY_ZONE field already forces naming an entry
        # price (so a momentum chase converts to "BUY at lower" =
        # effectively WATCH at current). This sentence makes the
        # weighting explicit. Applies to ALL tier-1 scans (not just
        # discovery), so it hardens every scan against buy-the-peak
        # — exactly the safety property the movement-first discovery
        # investigation required before turning on the unlock.
        lines.append(
            "  If the technicals show this stock is already extended "
            "(high RSI, well above the upper Bollinger band, far "
            "above its 20-day mean), prefer WATCH or AVOID over BUY "
            "— better to wait for a pullback than to chase a move "
            "that may be near its top.")

        if _DISCOVER_AVAILABLE and tm_discover is not None:
            if self.predictions_log is not None:
                try:
                    track_ctx = tm_discover.format_track_record_context(
                        self.predictions_log,
                        path=path,
                        ticker=ticker,
                    )
                    if track_ctx:
                        lines.append("")
                        lines.append(track_ctx)
                except Exception:
                    pass
            lines.append("")
            # v4.14.2 stage 4: candidate prompt (the user doesn't own
            # this ticker yet) — uses BUY / WATCH / AVOID. HOLD
            # was the v4.14.1 framing and produced meaningless
            # "hold what?" verdicts on non-owned tickers.
            # v4.14.5.14c-p2: unified → the N-block request schema
            # (Patch 1); single-path → today's one-block schema.
            if _unified:
                lines.append(
                    tm_discover
                    .format_multi_path_prediction_request_block(_mp))
            else:
                lines.append(
                    tm_discover
                    .format_prediction_request_block_candidate())

        return "\n".join(lines)


    # ─── Phase 2C: Track Record view ─────────────────────────────────────

    def _show_track_record(self, parent_override: tk.Misc | None = None,
                            container: tk.Misc | None = None):
        """Open Track Record. parent_override lets it open standalone
        from main window button without being a Holdings child.

        v4.14.5.14-gui-cleanup-b: `container` enables TAB MODE — render the
        Summary content into a provided frame (the merged Performance
        window's "Summary" tab) instead of a standalone Toplevel. In tab mode
        `win` + `_track_record_win` are pointed at the Performance Toplevel so
        the existing async chunked-render (`win.after`) + `winfo_exists`
        guards keep working unchanged, and the consensus-view dropdown
        re-renders into the same tab instead of destroying the shared window.
        The standalone path (container=None) is unchanged."""
        if self.predictions_log is None:
            return
        c = THEME

        # Reuse existing track record window if open (standalone only — in
        # tab mode the merged Performance window owns the lifecycle).
        if container is None and (hasattr(self, '_track_record_win')
                and self._track_record_win is not None):
            try:
                if self._track_record_win.winfo_exists():
                    self._track_record_win.lift()
                    self._track_record_win.focus_force()
                    return
            except Exception:
                pass

        # Choose parent
        parent = parent_override
        if parent is None:
            if self.window is not None and self.window.winfo_exists():
                parent = self.window
            else:
                parent = self.parent

        # v4.10.7: use the App's design-system helper when available.
        # Falls back to ad-hoc construction if app reference wasn't
        # passed (older code paths).
        use_styled = (self.app is not None
                       and hasattr(self.app, '_make_styled_toplevel'))

        if container is not None:
            # v4.14.5.14-gui-cleanup-b TAB MODE: render into the provided
            # frame; the merged Performance window owns the Toplevel. Point
            # win + _track_record_win at that Toplevel so the existing
            # win.after()/update_idletasks() chunked render + the
            # winfo_exists guards further down keep working unchanged.
            win = container.winfo_toplevel()
            self._track_record_win = win
            tl = None
            body_outer_helper = container
            btn_row = None
            fonts = self.app.fonts if use_styled else None
            space = self.app.space if use_styled else None
            btn_styles = self.app.btn if use_styled else None
        elif use_styled:
            def _close_cb():
                self._track_record_win = None
            tl = self.app._make_styled_toplevel(
                title="AI Track Record",
                subtitle=("How often the AI's calls hit target vs stop. "
                           "Roulette principle: this measures whether "
                           "the game is winnable, NOT what the next "
                           "prediction will do."),
                width=780, height=640,
                min_width=640, min_height=520,
                close_callback=_close_cb)
            win = tl['win']
            body_outer_helper = tl['body']
            btn_row = tl['footer']
            self._track_record_win = win
            # Override transient since this peer-window survives Holdings
            # closing — we don't want it tied to Holdings as transient parent
            try: win.transient(self.parent)
            except Exception: pass
            # Use design tokens for fonts/spacing/buttons throughout
            fonts = self.app.fonts
            space = self.app.space
            btn_styles = self.app.btn
        else:
            # Backward-compat fallback: original ad-hoc construction
            win = tk.Toplevel(parent)
            self._track_record_win = win
            win.title("AI Track Record — Tired Market")
            win.configure(bg=c['bg'])
            win.geometry("780x640")
            win.minsize(640, 520)
            # NOT transient — peer window, survives Holdings closing.

            def _on_tr_close():
                self._track_record_win = None
                try: win.destroy()
                except Exception: pass
            win.protocol("WM_DELETE_WINDOW", _on_tr_close)

            tk.Label(win, text="AI Track Record",
                     bg=c['bg'], fg=c['accent'],
                     font=('Segoe UI', 16, 'bold')
                     ).pack(side='top', pady=(14, 4))
            tk.Label(win,
                     text="How often the AI's calls hit target vs stop. "
                          "Roulette principle: this measures whether "
                          "the game is winnable, NOT what the next "
                          "prediction will do.",
                     bg=c['bg'], fg=c['muted'], font=('Segoe UI', 9),
                     wraplength=720, justify='center'
                     ).pack(side='top', pady=(0, 12))

            btn_row = tk.Frame(win, bg=c['bg'], height=60)
            btn_row.pack(side='bottom', fill='x', padx=20, pady=12)
            btn_row.pack_propagate(False)
            body_outer_helper = None  # use legacy path below
            fonts = None
            space = None
            btn_styles = None

        # v4.13.63.4: removed the check_outcomes call that used to fire
        # here on every Track Record open. It was making one synchronous
        # Yahoo Finance network call per unique open-BUY ticker (10-50
        # calls typical) before the dialog could paint a single widget
        # — a 5-50 second blocking gate at the top of every open. The
        # same check_outcomes runs on its own schedule via:
        #   1. the startup closer (_startup_close_outcomes in
        #      tired_market.py) a few seconds after launch, and
        #   2. the auto-refresh background loop (_auto_refresh_tick)
        #      every ~30 minutes thereafter.
        # Running it AGAIN on every Track Record open was redundant
        # work — this dialog shows the latest resolved state from those
        # scheduled runs. If a target or stop hit RIGHT NOW between
        # auto-refresh ticks it'll surface at the next tick or the next
        # launch's closer; same staleness window every other report has.

        # Close button — standalone only; in tab mode the merged Performance
        # window provides the single Close.
        if container is None and use_styled:
            self.app._make_close_button(
                btn_row, callback=tl['close']
            ).pack(side='right')
        elif container is None:
            tk.Button(btn_row, text="Close", bg=c['card2'], fg=c['text'],
                      relief='flat', padx=14, pady=4,
                      font=('Segoe UI', 9), cursor='hand2',
                      command=win.destroy).pack(side='right')

        # Body — scrollable canvas. Reuses the body_outer from the
        # helper if available, else builds it ad-hoc.
        if body_outer_helper is not None:
            body_outer = body_outer_helper
        else:
            body_outer = tk.Frame(win, bg=c['bg'])
            body_outer.pack(side='top', fill='both', expand=True,
                             padx=20, pady=(0, 4))
        canvas = tk.Canvas(body_outer, bg=c['bg'], highlightthickness=0)
        sb = ttk.Scrollbar(body_outer, orient='vertical',
                            command=canvas.yview)
        body = tk.Frame(canvas, bg=c['bg'])
        body.bind('<Configure>',
            lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        cw = canvas.create_window((0, 0), window=body, anchor='nw')
        canvas.bind('<Configure>',
            lambda e: canvas.itemconfig(cw, width=e.width))
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)

        # v4.10.7: NO focus_set on Enter — same fix as v4.10.6 main
        # window. Focus binding was raising the Track Record window
        # above other popups when mouse hovered the body.
        def _wheel(e):
            canvas.yview_scroll(-3 if e.delta > 0 else 3, 'units')
        canvas.bind('<MouseWheel>', _wheel)

        # ── v4.14.5.2: honest-numbers overhaul (ADDITIVE) ───────────────
        # Churn disclosure + post-fix cutoff toggle + per-path breakdown
        # segmented main vs speculative. Self-contained in `_extra`; the
        # existing aggregate stats below are untouched. Defensive: any
        # failure logs and is skipped so the legacy view still renders.
        try:
            import tired_market as _tmref
            _cutoff = getattr(
                _tmref, 'STABILITY_FIX_CUTOFF_TIMESTAMP', None)
            from datetime import datetime as _dtm2
            _cut_str = (_dtm2.fromtimestamp(_cutoff).strftime('%Y-%m-%d')
                        if _cutoff else 'the stability fix')
            _amber = c.get('amber', '#cc8800')

            _extra = tk.Frame(body, bg=c['bg'])
            _extra.pack(side='top', fill='x', pady=(0, 10))

            # v4.14.5.14-canonical-accuracy-definition: reframed from an
            # interim "numbers based on the 25% that survived" disclosure
            # into a permanent methodology note. The headline + every stat
            # surface now uses the canonical decided-cohort denominator, so
            # this is no longer a caveat about contaminated numbers — it's
            # the standing explanation of how accuracy is defined.
            _disc = (
                f"ℹ  How accuracy is measured: hit rate = target hits / "
                f"(target hits + stop hits) — the market-decided cohort "
                f"only. Historically ~75% of predictions were superseded "
                f"or contradicted by automatic re-analysis before reaching "
                f"a verdict; those are reported as the retraction rate and "
                f"are NOT counted against accuracy. v4.14.5.1 reduced that "
                f"churn; predictions after {_cut_str} accumulate cleaner "
                f"data and are shown by default.")
            _df = tk.Frame(_extra, bg=c['card'],
                           highlightbackground=_amber,
                           highlightthickness=2)
            _df.pack(side='top', fill='x', pady=(0, 8))
            tk.Label(_df, text=_disc, bg=c['card'], fg=_amber,
                     font=('Segoe UI', 9), wraplength=720,
                     justify='left', anchor='w',
                     padx=8, pady=6).pack(side='left',
                                          fill='x', expand=True)

            _show_hist = tk.BooleanVar(
                value=bool(self.app.cfg.get(
                    'track_record_show_historical', False))
                if self.app is not None else False)
            _toggle_row = tk.Frame(_extra, bg=c['bg'])
            _toggle_row.pack(side='top', fill='x', pady=(0, 6))
            _pp_holder = tk.Frame(_extra, bg=c['bg'])
            _pp_holder.pack(side='top', fill='x')

            def _fmt(v, suf='', dash='—'):
                return dash if v is None else f"{v:.1f}{suf}"

            def _render_pp():
                for ch in _pp_holder.winfo_children():
                    try: ch.destroy()
                    except Exception: pass
                cutoff = (None if _show_hist.get() else _cutoff)
                stats = compute_path_track_stats(
                    cutoff_ts=cutoff)
                hdr = (f"{'Path':14}{'Trk':6}{'Tot':>5}{'Dec':>5}"
                       f"{'Tgt':>5}{'Stp':>5}{'Hit%':>7}{'R:R':>6}"
                       f"{'Win%':>7}{'Los%':>7}{'EV%':>8}")
                # v4.14.6.1-price-band-cleanup: Track Record grouping by
                # main/speculative is now band-keyed. Lottery (< $5) is the
                # only speculative band; the rest are main.
                for track, plist, tint in (
                        ('main',
                         ['band_5_10', 'band_10_50', 'band_50_up'],
                         c['text']),
                        ('speculative', ['lottery'],
                         _amber)):
                    tk.Label(_pp_holder,
                             text=f"  {track.upper()} TRACK",
                             bg=c['bg'], fg=tint,
                             font=('Segoe UI', 10, 'bold')).pack(
                                 side='top', anchor='w', pady=(8, 2))
                    tk.Label(_pp_holder, text=hdr, bg=c['bg'],
                             fg=c['muted'],
                             font=('Consolas', 9)).pack(
                                 side='top', anchor='w')
                    for pth in plist:
                        d = stats.get(pth)
                        if not d:
                            line = (f"{pth:14}{'spec' if track=='speculative' else 'main':6}"
                                    f"{0:>5}{0:>5}{0:>5}{0:>5}"
                                    f"{'—':>7}{'—':>6}{'—':>7}{'—':>7}{'—':>8}")
                        else:
                            line = (
                                f"{pth:14}"
                                f"{('spec' if track=='speculative' else 'main'):6}"
                                f"{d['total']:>5}{d['decided']:>5}"
                                f"{d['target']:>5}{d['stop']:>5}"
                                f"{_fmt(d['hit_rate_pct'],'%'):>7}"
                                f"{_fmt(d['avg_rr']):>6}"
                                f"{_fmt(d['avg_winner_pct'],'%'):>7}"
                                f"{_fmt(d['avg_loser_pct'],'%'):>7}"
                                f"{_fmt(d['ev_pct'],'%'):>8}")
                        thin = bool(d and d.get('thin'))
                        tk.Label(
                            _pp_holder,
                            text=line + ('  (thin sample)' if thin else ''),
                            bg=c['bg'], fg=tint,
                            font=('Consolas', 9)).pack(
                                side='top', anchor='w')

            def _on_toggle():
                try:
                    if self.app is not None:
                        self.app.cfg['track_record_show_historical'] = bool(
                            _show_hist.get())
                        from tired_market import save_config as _sc
                        _sc(self.app.cfg)
                except Exception:
                    pass
                _render_pp()

            tk.Checkbutton(
                _toggle_row,
                text="Show all historical (includes churn-era data)",
                variable=_show_hist, command=_on_toggle,
                bg=c['bg'], fg=c['text'], selectcolor=c['card'],
                activebackground=c['bg'], activeforeground=c['text'],
                font=('Segoe UI', 9)).pack(side='left')
            tk.Label(_toggle_row,
                     text="  (default: post-stability-fix only)",
                     bg=c['bg'], fg=c['muted'],
                     font=('Segoe UI', 8, 'italic')).pack(side='left')

            _render_pp()

            tk.Frame(_extra, bg=c['card2'] if 'card2' in c else c['muted'],
                     height=1).pack(side='top', fill='x', pady=(10, 4))
        except Exception as _tre:
            try:
                if self.app is not None:
                    self.app._log(
                        f"Track Record honest-numbers section skipped: "
                        f"{type(_tre).__name__}: {_tre}", 'amber')
            except Exception:
                pass

        # v4.13.62: read the startup-prebuilt stats cache if available
        # and still valid (prediction count unchanged). Falls back to
        # live compute if cache is missing/stale. The cache is built by
        # App's startup-preload thread immediately after the predictions
        # log loads, so first-click of Track Record is instant on a
        # warm install.
        _tr_cache = None
        try:
            if self.app is not None:
                _cached = getattr(self.app, '_track_record_cache', None)
                if _cached:
                    try:
                        cur_count = len(self.predictions_log.get_all())
                    except Exception:
                        cur_count = -1
                    if (_cached.get('pred_count') == cur_count
                            and cur_count >= 0):
                        _tr_cache = _cached
        except Exception:
            _tr_cache = None

        # v4.13.61: ONE-PASS stats computation. Track Record was
        # calling aggregate_stats() 1 + 4 + 3 = 8 times, each iterating
        # the full predictions cache. With 2,000+ predictions on a
        # populated install this took 30+ seconds before the dialog
        # body could even start drawing. compute_all_stats() does it
        # all in a single pass and we slice the result.
        try:
            if _tr_cache and _tr_cache.get('all_stats') is not None:
                _all_stats = _tr_cache['all_stats']
            else:
                _all_stats = self.predictions_log.compute_all_stats()
            overall = _all_stats['overall']
            _stats_by_path = _all_stats['by_path']
            _stats_by_confidence = _all_stats['by_confidence']
        except AttributeError:
            # Backward-compat: older PredictionsLog without
            # compute_all_stats. Fall through to the slow path.
            overall = self.predictions_log.aggregate_stats()
            _stats_by_path = None
            _stats_by_confidence = None
        if overall['total'] == 0:
            tk.Label(body,
                     text="No predictions logged yet.\n\n"
                          "When the AI analyzes a holding (Check Now) or "
                          "evaluates a candidate (Discover), the structured "
                          "prediction gets logged here. Outcomes are tracked "
                          "automatically as prices move and timeframes pass.",
                     bg=c['bg'], fg=c['muted'],
                     font=('Segoe UI', 10), justify='left',
                     wraplength=700, padx=14, pady=20
                     ).pack(side='top', anchor='w')
            return

        # ════════════════════════════════════════════════════════════════
        # v4.13.6 — REAL TRADES CARD
        # Above AI ACCURACY because real outcomes from actual buy/sell
        # actions are more meaningful than auto-detected paper-trade
        # stop hits. Pulls from portfolio.json closed[] list.
        # ════════════════════════════════════════════════════════════════
        try:
            _real_closed = list(self.mgr.data.get('closed', []) or [])
        except Exception:
            _real_closed = []

        rt_card = tk.Frame(body, bg=c['card'], padx=20, pady=14)
        rt_card.pack(side='top', fill='x', pady=(0, 12))
        rt_header_lbl = tk.Label(rt_card,
                                 text="REAL TRADES (your actual buy/sell history)",
                                 bg=c['card'], fg=c['muted'],
                                 font=('Segoe UI', 8, 'bold'))
        rt_header_lbl.pack(side='top', anchor='w')

        if _real_closed:
            wins = [t for t in _real_closed
                    if (t.get('pnl_dollars') or 0) > 0]
            losses = [t for t in _real_closed
                      if (t.get('pnl_dollars') or 0) < 0]
            n_total = len(_real_closed)
            n_wins = len(wins)
            win_rate = (100.0 * n_wins / n_total) if n_total else 0
            total_pnl = sum((t.get('pnl_dollars') or 0)
                            for t in _real_closed)
            total_cost_basis = sum((t.get('total_cost') or 0)
                                   for t in _real_closed)
            total_pnl_pct = ((total_pnl / total_cost_basis * 100)
                             if total_cost_basis else 0)

            # Big number = win rate (or P&L if only 1-2 trades)
            if n_total >= 3:
                if win_rate >= 60:
                    big_color = c['green']
                elif win_rate >= 40:
                    big_color = c['amber']
                else:
                    big_color = c['red']
                rt_big = f"{win_rate:.0f}%"
                rt_sub = (f"{n_wins} win{'s' if n_wins != 1 else ''} of "
                          f"{n_total} closed trade"
                          f"{'s' if n_total != 1 else ''}  \u00b7  "
                          f"realized P&L: "
                          f"{'+' if total_pnl >= 0 else ''}"
                          f"${total_pnl:.2f} ({total_pnl_pct:+.1f}%)")
            else:
                # Show P&L as the big number when sample is tiny
                if total_pnl > 0:
                    big_color = c['green']
                elif total_pnl < 0:
                    big_color = c['red']
                else:
                    big_color = c['dim']
                rt_big = (f"{'+' if total_pnl >= 0 else ''}"
                          f"${total_pnl:.2f}")
                rt_sub = (f"{total_pnl_pct:+.1f}% across "
                          f"{n_total} closed trade"
                          f"{'s' if n_total != 1 else ''}  \u00b7  "
                          f"win rate will populate over more trades")

            rt_big_lbl = tk.Label(rt_card, text=rt_big,
                                  bg=c['card'], fg=big_color,
                                  font=('Segoe UI', 36, 'bold'))
            rt_big_lbl.pack(side='top', anchor='w', pady=(2, 0))
            tk.Label(rt_card, text=rt_sub,
                     bg=c['card'], fg=c['muted'],
                     font=('Segoe UI', 9)
                     ).pack(side='top', anchor='w', pady=(0, 4))

            # Detail line: best win / worst loss / avg hold time
            # When only 1 trade, best == worst — show only one.
            best_win = max(_real_closed,
                           key=lambda t: t.get('pnl_pct') or 0)
            worst = min(_real_closed,
                        key=lambda t: t.get('pnl_pct') or 0)
            hold_days = [t.get('days_held') for t in _real_closed
                         if t.get('days_held') is not None]
            avg_hold = (sum(hold_days) / len(hold_days)
                        if hold_days else 0)

            detail_bits = []
            if (best_win.get('pnl_pct') or 0) > 0:
                detail_bits.append(
                    f"best: {best_win.get('ticker', '?')} "
                    f"+{best_win.get('pnl_pct', 0):.1f}% "
                    f"in {best_win.get('days_held', 0)}d")
            # Only show 'worst' if it's a different trade and is a loss
            if (n_total >= 2
                and worst is not best_win
                and (worst.get('pnl_pct') or 0) < 0):
                detail_bits.append(
                    f"worst: {worst.get('ticker', '?')} "
                    f"{worst.get('pnl_pct', 0):.1f}% "
                    f"in {worst.get('days_held', 0)}d")
            if hold_days and n_total >= 2:
                detail_bits.append(
                    f"avg hold: {avg_hold:.1f}d")
            if detail_bits:
                tk.Label(rt_card,
                         text="  \u00b7  ".join(detail_bits),
                         bg=c['card'], fg=c['dim'],
                         font=('Segoe UI', 9)
                         ).pack(side='top', anchor='w', pady=(0, 0))

            # v4.13.64: Make REAL TRADES card clickable -> opens Trade
            # Log dialog. Replaces the toolbar Trade Log button. Tooltip
            # + underline-on-hover on the big number; click anywhere on
            # the card opens the per-trade ledger.
            self._wire_real_trades_click(rt_card, rt_big_lbl)
        else:
            tk.Label(rt_card, text="\u2014",
                     bg=c['card'], fg=c['dim'],
                     font=('Segoe UI', 36, 'bold')
                     ).pack(side='top', anchor='w', pady=(2, 0))
            tk.Label(rt_card,
                     text=("No closed trades yet. Use the Sell button "
                           "on a holding to record a real sale; this "
                           "card will populate as you trade."),
                     bg=c['card'], fg=c['muted'],
                     font=('Segoe UI', 9), wraplength=700,
                     justify='left'
                     ).pack(side='top', anchor='w', pady=(0, 4))

        # ════════════════════════════════════════════════════════════════
        # v4.10.3 — HEADLINE ACCURACY CARD (relabeled in v4.13.6)
        # Big-number summary at the very top. This is the "old simple
        # version" — one number, lots of context. Detail cards below
        # still show per-path / per-confidence / per-model.
        # ════════════════════════════════════════════════════════════════
        headline_card = tk.Frame(body, bg=c['card'], padx=20, pady=14)
        headline_card.pack(side='top', fill='x', pady=(0, 12))

        # The number itself
        # v4.14.5.14-canonical-accuracy-definition: headline gates on and
        # divides by the DECIDED cohort (target_hit + stop_hit), matching
        # the per-path honest-numbers table below. Previously it divided
        # by ALL closures (incl. superseded/contradicted/expired/sold),
        # which showed ~6% instead of the true ~38%.
        if overall.get('n_decided', 0) > 0:
            acc = overall['target_rate_pct']
            # Color-code: green if >50%, amber if 30-50, red if <30
            if acc >= 50:
                num_color = c['green']
            elif acc >= 30:
                num_color = c['amber']
            else:
                num_color = c['red']
            big_text = f"{acc:.0f}%"
            sub_text = (f"target hit on {overall['target_hit']} of "
                        f"{overall['n_decided']} decided predictions "
                        f"(target vs stop)")
        else:
            num_color = c['dim']
            big_text = "—"
            sub_text = (f"{overall['open']} open, {overall['closed']} resolved "
                        f"but none reached target/stop yet — accuracy "
                        f"populates as predictions hit their target or stop")

        tk.Label(headline_card,
                 text="AI PREDICTION ACCURACY (paper-trade auto-detected, not real trades)",
                 bg=c['card'], fg=c['muted'],
                 font=('Segoe UI', 8, 'bold')
                 ).pack(side='top', anchor='w')
        tk.Label(headline_card, text=big_text,
                 bg=c['card'], fg=num_color,
                 font=('Segoe UI', 36, 'bold')
                 ).pack(side='top', anchor='w', pady=(2, 0))
        tk.Label(headline_card, text=sub_text,
                 bg=c['card'], fg=c['muted'],
                 font=('Segoe UI', 9)
                 ).pack(side='top', anchor='w', pady=(0, 0))

        # v4.14.5.14-canonical-accuracy-definition: methodology note so the
        # headline is self-explaining — and so the shift up from the old
        # ~6% reads as a correction, not a new bug.
        _retr_n = overall.get('superseded', 0) + overall.get('contradicted', 0)
        tk.Label(
            headline_card,
            text=("Accuracy = target hits / (target hits + stop hits). "
                  "Excludes predictions superseded by re-analysis "
                  f"({overall.get('retract_rate_pct', 0):.0f}% retraction "
                  f"rate, {_retr_n} preds), expired without a verdict "
                  f"({overall.get('expired', 0)}), or manually sold "
                  "(those are in REAL TRADES)."),
            bg=c['card'], fg=c['dim'],
            font=('Segoe UI', 8, 'italic'),
            wraplength=700, justify='left'
            ).pack(side='top', anchor='w', pady=(4, 0))

        if overall['sample_size_warning']:
            tk.Label(headline_card,
                     text="⚠ Small sample — this is noisy until you "
                          "have ~20+ closed predictions.",
                     bg=c['card'], fg=c['amber'],
                     font=('Segoe UI', 8, 'italic')
                     ).pack(side='top', anchor='w', pady=(4, 0))

        # v4.14.5.14-hold-grading: HOLD verdicts get their own line, separate
        # from the BUY target/stop accuracy above. A HOLD is "correct" if the
        # price stayed in its target/stop band over the timeframe.
        _hd = overall.get('hold_decided', 0)
        if _hd > 0:
            _hh = overall.get('hold_held', 0)
            _hacc = overall.get('hold_accuracy_pct', 0.0)
            _hcol = (c['green'] if _hacc >= 50
                     else c['amber'] if _hacc >= 30 else c['red'])
            tk.Label(
                headline_card,
                text=(f"HOLD calls: {_hacc:.0f}% stayed in band "
                      f"({_hh} of {_hd} held without hitting target/stop)"),
                bg=c['card'], fg=_hcol,
                font=('Segoe UI', 9)
                ).pack(side='top', anchor='w', pady=(4, 0))

        # v4.14.5.14-trim-buy-more-grading: TRIM and BUY MORE owned-position
        # verdicts get their own lines, separate from BUY/HOLD above. Each is
        # shown ONLY once at least one has been decided (never "0 of 0") — so
        # these stay hidden until the refresh-triggers writer produces the
        # first one. TRIM = correct when the price fell or merely held in band
        # (you trimmed before a decline/plateau); BUY MORE = correct when the
        # price reached target (the stronger-buy conviction paid off).
        _td = overall.get('trim_decided', 0)
        if _td > 0:
            _tc = overall.get('trim_correct', 0)
            _tacc = overall.get('trim_accuracy_pct') or 0.0
            _tcol = (c['green'] if _tacc >= 50
                     else c['amber'] if _tacc >= 30 else c['red'])
            tk.Label(
                headline_card,
                text=(f"TRIM calls: {_tacc:.0f}% correct "
                      f"({_tc} of {_td} where trimming beat holding)"),
                bg=c['card'], fg=_tcol,
                font=('Segoe UI', 9)
                ).pack(side='top', anchor='w', pady=(4, 0))

        _bmd = overall.get('buy_more_decided', 0)
        if _bmd > 0:
            _bmc = overall.get('buy_more_correct', 0)
            _bmacc = overall.get('buy_more_accuracy_pct') or 0.0
            _bmcol = (c['green'] if _bmacc >= 50
                      else c['amber'] if _bmacc >= 30 else c['red'])
            tk.Label(
                headline_card,
                text=(f"BUY MORE calls: {_bmacc:.0f}% correct "
                      f"({_bmc} of {_bmd} reached target before stop)"),
                bg=c['card'], fg=_bmcol,
                font=('Segoe UI', 9)
                ).pack(side='top', anchor='w', pady=(4, 0))

        # v4.14.5.14-sold-prediction-tracking: realized win line that
        # INCLUDES your manual sells. The big % above is the market-
        # decided auto-detected accuracy (target vs stop) and deliberately
        # excludes your manual exits; this line answers "how did the picks
        # I actually sold do" — counting a profitable sell (e.g. RIG) as a
        # win. Only shown once you have at least one recorded sell.
        if overall.get('sold', 0) > 0:
            _rw = overall.get('realized_wins', 0)
            _rl = overall.get('realized_losses', 0)
            _rd = overall.get('realized_decided', 0)
            _rwr = overall.get('realized_win_rate_pct', 0.0)
            tk.Label(
                headline_card,
                text=(f"Including your {overall['sold']} manual sell(s): "
                      f"{_rw} win / {_rl} loss of {_rd} decided "
                      f"({_rwr:.0f}% realized win rate)"),
                bg=c['card'],
                fg=(c['green'] if _rwr >= 50 else c['text']),
                font=('Segoe UI', 9)
                ).pack(side='top', anchor='w', pady=(4, 0))

        # v4.14.5.14-manual-sell-tracking: the line above counts only manual
        # sells of tickers the AI had an OPEN prediction on (those flow through
        # mark_position_sold). It misses positions you bought and sold WITHOUT
        # an AI prediction (a manual buy/sell like RIG) — those are recorded in
        # the holdings Trade History (the `closed` list) but never reach the
        # prediction store, so they were invisible here. Surface the FULL
        # realized trade record from the holdings manager so every actual trade
        # is represented, without injecting synthetic non-AI records into the
        # prediction store (which would pollute the AI accuracy stats above).
        try:
            _rstats = self.mgr.get_realized_stats() if self.mgr else None
        except Exception:
            _rstats = None
        if _rstats and _rstats.get('count', 0) > 0:
            _tp = _rstats.get('total_pnl', 0.0)
            _tsign = '+' if _tp >= 0 else '-'
            _twr = _rstats.get('win_rate', 0.0)
            tk.Label(
                headline_card,
                text=(f"Your actual trades (all closed positions, incl. ones "
                      f"the AI never predicted): {_rstats['count']} closed — "
                      f"{_rstats.get('wins', 0)}W / {_rstats.get('losses', 0)}L "
                      f"({_twr:.0f}% win rate), {_tsign}${abs(_tp):,.0f} "
                      f"realized P&L. Full list in Trade History."),
                bg=c['card'],
                fg=(c['green'] if _tp >= 0 else c['amber']),
                font=('Segoe UI', 9)
                ).pack(side='top', anchor='w', pady=(4, 0))

        # ── DETAIL CARDS BELOW (the existing breakdown) ──

        overall_card = tk.Frame(body, bg=c['card'], padx=16, pady=12)
        overall_card.pack(side='top', fill='x', pady=(0, 10))
        tk.Label(overall_card, text="OVERALL", bg=c['card'],
                 fg=c['accent'], font=('Segoe UI', 9, 'bold')
                 ).pack(side='top', anchor='w')

        line = (f"{overall['total']} predictions  ·  "
                f"{overall['closed']} closed, {overall['open']} open  ·  "
                f"target hit: {overall['target_hit']} "
                f"({overall['target_rate_pct']:.0f}%)  ·  "
                f"stop hit: {overall['stop_hit']} "
                f"({overall['stop_rate_pct']:.0f}%)")
        tk.Label(overall_card, text=line, bg=c['card'], fg=c['text'],
                 font=('Segoe UI', 10), wraplength=700, justify='left'
                 ).pack(side='top', anchor='w', pady=(4, 0))

        # v4.14.6.1-price-band-cleanup: iterate the four band keys.
        for path_label in ('lottery', 'band_5_10',
                            'band_10_50', 'band_50_up'):
            # v4.13.61: use precomputed stats from compute_all_stats
            # if available; fall back to per-call aggregate_stats.
            if _stats_by_path is not None:
                stats = _stats_by_path.get(path_label)
                if stats is None:
                    continue  # no predictions for this path
            else:
                stats = self.predictions_log.aggregate_stats(path=path_label)
            if stats['total'] == 0:
                continue
            card = tk.Frame(body, bg=c['card'], padx=16, pady=10)
            card.pack(side='top', fill='x', pady=(0, 8))
            tk.Label(card, text=f"PATH: {path_label.upper()}",
                     bg=c['card'], fg=c['accent'],
                     font=('Segoe UI', 9, 'bold')
                     ).pack(side='top', anchor='w')
            line = (f"  {stats['total']} predictions, {stats['closed']} closed "
                    f"·  target: {stats['target_hit']} "
                    f"({stats['target_rate_pct']:.0f}%)  ·  "
                    f"stop: {stats['stop_hit']} "
                    f"({stats['stop_rate_pct']:.0f}%)")
            tk.Label(card, text=line, bg=c['card'], fg=c['text'],
                     font=('Segoe UI', 9)
                     ).pack(side='top', anchor='w', pady=(2, 0))

        # v4.13.62: yield to Tk between sections so the user sees the
        # window populate progressively instead of all-at-once after a
        # 30-second wait. Cheap — just flushes pending paint events.
        try:
            win.update_idletasks()
        except Exception:
            pass

        # v4.13.16: per-model stats - filtered to ACTIVE models only.
        # A model is "active" if it has at least one prediction within
        # the last 30 days. Avoids showing eternally-stale cards for
        # deprecated models you stopped running.
        # v4.14.0 stage 6d: canonicalize model labels at display time
        # so past "My Groq" predictions group with new "Groq"
        # predictions under one accuracy row. We don't rewrite
        # predictions.jsonl — we normalize on read.
        try:
            import tm_api_providers as _tmap6d_acc
            _norm_acc = _tmap6d_acc.canonicalize_model_label
        except Exception:
            _norm_acc = lambda x: x
        try:
            from datetime import datetime as _dt, timedelta as _td
            cutoff = _dt.now() - _td(days=30)
            recent_models = set()
            with self.predictions_log._lock:
                for p in self.predictions_log._cache:
                    m = p.get('model')
                    if not m:
                        continue
                    try:
                        ts = _dt.fromisoformat(p.get('timestamp', ''))
                        if ts >= cutoff:
                            recent_models.add(_norm_acc(m))
                    except Exception:
                        continue
            all_models = sorted(recent_models)
        except Exception:
            all_models = []
        for model_label in all_models:
            try:
                # PredictionsLog.aggregate_stats doesn't filter by model
                # natively, so we compute manually. Stats include ALL
                # predictions for the model (not just last 30d) so the
                # win-rate is statistically meaningful.
                # Canonicalize on the filter predicate too so historical
                # records with the original raw name get grouped under
                # the same canonical label.
                with self.predictions_log._lock:
                    preds = [
                        p for p in self.predictions_log._cache
                        if _norm_acc(p.get('model')) == model_label
                    ]
                if not preds:
                    continue
                total = len(preds)
                # v4.14.5.14-canonical-accuracy-definition: rate denominator
                # is the DECIDED cohort (target_hit + stop_hit) only —
                # expired/superseded/contradicted are non-verdicts.
                decided = sum(1 for p in preds
                               if p.get('status') in ('target_hit', 'stop_hit'))
                target_hit = sum(1 for p in preds
                                  if p.get('status') == 'target_hit')
                stop_hit = sum(1 for p in preds
                                if p.get('status') == 'stop_hit')
                if decided == 0:
                    continue
                target_pct = target_hit / decided * 100
                stop_pct = stop_hit / decided * 100
                card = tk.Frame(body, bg=c['card'], padx=16, pady=10)
                card.pack(side='top', fill='x', pady=(0, 8))
                tk.Label(card, text=f"MODEL: {model_label}",
                         bg=c['card'], fg=c['accent'],
                         font=('Segoe UI', 9, 'bold')
                         ).pack(side='top', anchor='w')
                line = (f"  {total} predictions, {decided} decided "
                        f"·  target: {target_hit} "
                        f"({target_pct:.0f}%)  ·  "
                        f"stop: {stop_hit} ({stop_pct:.0f}%)")
                tk.Label(card, text=line, bg=c['card'], fg=c['text'],
                         font=('Segoe UI', 9)
                         ).pack(side='top', anchor='w', pady=(2, 0))
            except Exception:
                continue

        # v4.13.62: yield after the per-model card loop so paint flushes
        try:
            win.update_idletasks()
        except Exception:
            pass

        for conf_label in ('LOW', 'MODERATE', 'HIGH'):
            # v4.13.61: use precomputed stats if available
            if _stats_by_confidence is not None:
                stats = _stats_by_confidence.get(conf_label)
                if stats is None:
                    continue
            else:
                stats = self.predictions_log.aggregate_stats(
                    confidence=conf_label)
            if stats['total'] == 0:
                continue
            card = tk.Frame(body, bg=c['card'], padx=16, pady=10)
            card.pack(side='top', fill='x', pady=(0, 8))
            tk.Label(card, text=f"CONFIDENCE: {conf_label}",
                     bg=c['card'], fg=c['accent'],
                     font=('Segoe UI', 9, 'bold')
                     ).pack(side='top', anchor='w')
            line = (f"  {stats['total']} predictions, {stats['closed']} closed "
                    f"·  target: {stats['target_hit']} "
                    f"({stats['target_rate_pct']:.0f}%)  ·  "
                    f"stop: {stats['stop_hit']} "
                    f"({stats['stop_rate_pct']:.0f}%)")
            tk.Label(card, text=line, bg=c['card'], fg=c['text'],
                     font=('Segoe UI', 9)
                     ).pack(side='top', anchor='w', pady=(2, 0))

        # v4.13.62: yield after per-confidence cards
        try:
            win.update_idletasks()
        except Exception:
            pass

        # ════════════════════════════════════════════════════════════════
        # v4.9.1 — PER-MODEL STATS CARD
        # Aggregates predictions per model: how many calls, distribution by
        # direction, target/stop hit rates once outcomes accumulate. Helps
        # build data-backed trust in models. Honest framing: "Not enough
        # data" is shown for models with fewer than 5 closed predictions.
        # ════════════════════════════════════════════════════════════════
        try:
            if _tr_cache and _tr_cache.get('per_model_stats') is not None:
                model_stats = _tr_cache['per_model_stats']
            else:
                model_stats = tm_discover.compute_per_model_stats(
                    self.predictions_log, hours=168)
        except Exception:
            model_stats = []

        if model_stats:
            stats_card = tk.Frame(body, bg=c['card'], padx=16, pady=10)
            stats_card.pack(side='top', fill='x', pady=(8, 0))
            tk.Label(stats_card,
                     text=f"PER-MODEL STATS  (last 7 days, "
                          f"{len(model_stats)} model(s) used)",
                     bg=c['card'], fg=c['accent'],
                     font=('Segoe UI', 9, 'bold')
                     ).pack(side='top', anchor='w', pady=(0, 4))
            tk.Label(stats_card,
                     text="Hit rates need 5+ closed predictions to be "
                          "meaningful — small samples are noisy. Pay "
                          "attention to direction distribution as a "
                          "calibration signal.",
                     bg=c['card'], fg=c['dim'],
                     font=('Segoe UI', 8, 'italic'),
                     wraplength=600, justify='left'
                     ).pack(side='top', anchor='w', pady=(0, 6))

            # Column header
            stats_hdr = tk.Frame(stats_card, bg=c['card'])
            stats_hdr.pack(side='top', fill='x', pady=(0, 2))
            tk.Label(stats_hdr,
                     text=("MODEL                TOTAL  BUY/HOLD/AVOID/NO-CALL"
                           "       OPEN  CLOSED  TARGET-HIT  STOP-HIT"),
                     bg=c['card'], fg=c['dim'],
                     font=('Consolas', 7, 'bold')
                     ).pack(side='left')

            for s in model_stats:
                row = tk.Frame(stats_card, bg=c['card'])
                row.pack(side='top', fill='x', pady=1)

                model_short = (s['model'][:18] + '…') if len(s['model']) > 19 \
                              else s['model']
                tk.Label(row, text=f"  {model_short:20}",
                         bg=c['card'], fg=c['text'],
                         font=('Consolas', 9, 'bold')
                         ).pack(side='left')
                tk.Label(row, text=f" {s['total']:5} ",
                         bg=c['card'], fg=c['text'],
                         font=('Consolas', 9)
                         ).pack(side='left')
                # Direction distribution
                d = s['directions']
                dist = (f" {d.get('BUY', 0):3}/{d.get('HOLD', 0):3}/"
                         f"{d.get('AVOID', 0):3}/{d.get('NO_CALL', 0):3}      ")
                tk.Label(row, text=dist,
                         bg=c['card'], fg=c['muted'],
                         font=('Consolas', 9)
                         ).pack(side='left')
                tk.Label(row, text=f"  {s['open']:4} ",
                         bg=c['card'], fg=c['text'],
                         font=('Consolas', 9)
                         ).pack(side='left')
                tk.Label(row, text=f" {s['closed']:5}  ",
                         bg=c['card'], fg=c['text'],
                         font=('Consolas', 9)
                         ).pack(side='left')

                # Hit rates — show "—" if not meaningful
                if s['has_meaningful_outcomes']:
                    # v4.14.5.14-canonical-accuracy-definition: fractions are
                    # over the DECIDED cohort, matching the rate %.
                    th_str = (f" {s['target_hits']}/{s.get('decided', 0)} "
                              f"({s['target_hit_rate_pct']:.0f}%)")
                    sh_str = (f"   {s['stop_hits']}/{s.get('decided', 0)} "
                              f"({s['stop_hit_rate_pct']:.0f}%)")
                    th_color = c['green'] if s['target_hit_rate_pct'] >= 50 \
                                else c['amber']
                    sh_color = c['red'] if s['stop_hit_rate_pct'] >= 50 \
                                else c['muted']
                else:
                    th_str = " — (need 5+ closed)"
                    sh_str = "        — "
                    th_color = c['dim']
                    sh_color = c['dim']
                tk.Label(row, text=th_str,
                         bg=c['card'], fg=th_color,
                         font=('Consolas', 9)
                         ).pack(side='left')
                tk.Label(row, text=sh_str,
                         bg=c['card'], fg=sh_color,
                         font=('Consolas', 9)
                         ).pack(side='left')

        # v4.13.62: yield after PER-MODEL STATS card
        try:
            win.update_idletasks()
        except Exception:
            pass

        # ════════════════════════════════════════════════════════════════
        # v4.8.13 — CONSENSUS CARD
        # v4.9.1: now supports filtering by consensus scan_id
        # ════════════════════════════════════════════════════════════════
        # v4.9.1: track which scan_id is being viewed (None = all recent)
        if not hasattr(self, '_consensus_view_scan_id'):
            self._consensus_view_scan_id = None

        # Discover available scan_ids for the dropdown
        try:
            if _tr_cache and _tr_cache.get('scan_ids') is not None:
                available_scans = _tr_cache['scan_ids']
            else:
                available_scans = tm_discover.list_consensus_scan_ids(
                    self.predictions_log, hours=168)
        except Exception:
            available_scans = []

        try:
            # v4.13.62: consensus cache only valid for the default
            # (scan_id=None) view. If the user picked a specific scan
            # from the dropdown, fall through to live compute.
            if (_tr_cache
                    and _tr_cache.get('consensus') is not None
                    and self._consensus_view_scan_id is None):
                consensus_entries = _tr_cache['consensus']
            else:
                consensus_entries = tm_discover.compute_consensus(
                    self.predictions_log, hours=48, min_models=2,
                    scan_id=self._consensus_view_scan_id)
        except Exception:
            consensus_entries = []

        if consensus_entries:
            consensus_card = tk.Frame(body, bg=c['card'], padx=16, pady=10)
            consensus_card.pack(side='top', fill='x', pady=(8, 0))

            # Header
            cons_hdr = tk.Frame(consensus_card, bg=c['card'])
            cons_hdr.pack(side='top', fill='x', pady=(0, 4))
            # Title varies based on whether we're filtering
            if self._consensus_view_scan_id:
                # Find friendly label for current scan
                cur_scan = next(
                    (s for s in available_scans
                     if s['scan_id'] == self._consensus_view_scan_id), None)
                cur_label = (cur_scan['friendly_label']
                              if cur_scan else self._consensus_view_scan_id)
                title = (f"MODEL CONSENSUS — {cur_label}  "
                         f"({len(consensus_entries)} ticker(s))")
            else:
                title = (f"MODEL CONSENSUS  ({len(consensus_entries)} "
                         f"ticker(s) analyzed by 2+ models in last 48h)")
            tk.Label(cons_hdr,
                     text=title,
                     bg=c['card'], fg=c['accent'],
                     font=('Segoe UI', 9, 'bold')
                     ).pack(side='left')

            # v4.9.1: scan_id filter dropdown — only show if scans exist
            if available_scans:
                # ttk is already imported at module level — DON'T re-import
                # locally, that turns ttk into a function-local variable
                # which makes the earlier ttk.Scrollbar(...) reference
                # raise UnboundLocalError. (v4.9.1.1 fix)
                # Build dropdown options
                dropdown_options = ['All recent (48h)']
                option_to_scan_id = {dropdown_options[0]: None}
                for s in available_scans:
                    label = s['friendly_label']
                    dropdown_options.append(label)
                    option_to_scan_id[label] = s['scan_id']

                # Current selection
                if self._consensus_view_scan_id is None:
                    current_option = dropdown_options[0]
                else:
                    current_option = next(
                        (lbl for lbl, sid in option_to_scan_id.items()
                         if sid == self._consensus_view_scan_id),
                        dropdown_options[0])

                tk.Label(cons_hdr, text="  view: ",
                         bg=c['card'], fg=c['dim'],
                         font=('Segoe UI', 8)
                         ).pack(side='right')
                view_var = tk.StringVar(value=current_option)
                view_combo = ttk.Combobox(
                    cons_hdr, textvariable=view_var,
                    values=dropdown_options, state='readonly',
                    width=42, font=('Segoe UI', 8))
                view_combo.pack(side='right', padx=4)

                def _on_view_changed(_event=None):
                    chosen = view_var.get()
                    new_scan_id = option_to_scan_id.get(chosen)
                    if new_scan_id != self._consensus_view_scan_id:
                        self._consensus_view_scan_id = new_scan_id
                        # Refresh. v4.14.5.14-gui-cleanup-b: in TAB MODE
                        # re-render into the SAME tab frame (do NOT destroy
                        # the shared Performance window); standalone destroys
                        # + reopens as before.
                        if container is not None:
                            for _ch in container.winfo_children():
                                try: _ch.destroy()
                                except Exception: pass
                            self._show_track_record(container=container)
                        elif self._track_record_win is not None:
                            try: self._track_record_win.destroy()
                            except Exception: pass
                            self._track_record_win = None
                            self._show_track_record(
                                parent_override=(self.parent
                                                  if self.parent else None))
                view_combo.bind('<<ComboboxSelected>>', _on_view_changed)

            # Helper text explaining the scoring
            tk.Label(consensus_card,
                     text="Sorted by actionability. OWNED positions first, "
                          "then BUY signals, then splits, then AVOIDs at "
                          "the bottom. Click ▸ to expand per-model details.",
                     bg=c['card'], fg=c['dim'],
                     font=('Segoe UI', 8, 'italic'),
                     wraplength=600, justify='left'
                     ).pack(side='top', anchor='w', pady=(0, 8))

            # Column header
            cons_col_hdr = tk.Frame(consensus_card, bg=c['card'])
            cons_col_hdr.pack(side='top', fill='x', pady=(0, 4))
            tk.Label(cons_col_hdr,
                     text="  TICKER  CONSENSUS                                "
                          "       MODELS         CONFS        ",
                     bg=c['card'], fg=c['dim'],
                     font=('Consolas', 7, 'bold')
                     ).pack(side='left')

            # State tracking for expand/collapse
            # v4.8.13.1: Now we keep a registry of detail frames per ticker,
            # and toggle their visibility with pack/pack_forget instead of
            # destroying and reopening the entire Track Record window.
            # That earlier approach worked but felt like "the window
            # closed" to the user — bad UX.
            if not hasattr(self, '_consensus_expanded'):
                self._consensus_expanded = set()  # set of tickers expanded
            # Map of ticker -> (arrow_button, detail_frame) for inline toggle
            row_widgets = {}

            # ── v4.13.4: Re-sort consensus_entries by actionability ──
            # The default sort from compute_consensus puts Unanimous AVOID
            # at the top alongside Unanimous BUY, since both have equal
            # consensus_score. From an actionability standpoint that's
            # backwards: BUY signals are opportunities, AVOID signals
            # are filter-outs.
            #
            # The new sort respects:
            #   - Owned tickers first (the user needs to know about positions)
            #   - Within owned: exit signals (SELL/TRIM/AVOID) > confirmations
            #   - Unowned: BUY signals > splits > AVOID signals
            #   - Stronger consensus before weaker within each group
            try:
                _held = {h.get('ticker', '').upper()
                         for h in self.mgr.holdings}
            except Exception:
                _held = set()

            def _consensus_sort_key(entry):
                t = (entry.get('ticker') or '').upper()
                owned = t in _held
                d = (entry.get('majority_direction') or '').upper()
                # 'BUY MORE' may appear from owned-position consensus runs
                if d in ('BUYMORE', 'BUY MORE'):
                    d = 'BUY'
                score = entry.get('consensus_score', 0)
                # Bucket: lower number = appears higher in list
                if owned and d in ('SELL', 'TRIM', 'AVOID'):
                    bucket = 1   # exit signals on YOUR positions — most urgent
                elif owned and d in ('BUY', 'HOLD'):
                    bucket = 2   # confirmations on YOUR positions
                elif d == 'BUY':
                    bucket = 3   # new opportunity
                elif d == '' or score == 0:
                    bucket = 5   # split / unclear — investigate
                elif d in ('SELL', 'TRIM'):
                    bucket = 6   # exit signals on stocks you don't own — irrelevant
                elif d == 'HOLD':
                    bucket = 4   # holds on unowned — neutral info
                elif d == 'AVOID':
                    bucket = 7   # AVOIDs on unowned — bottom
                else:
                    bucket = 8
                # Within bucket, higher consensus_score first.
                # For AVOID buckets we INVERT — strongest AVOID = least
                # interesting, sinks to the bottom of the AVOID pile.
                if d == 'AVOID' and not owned:
                    score_key = score   # ascending = weak AVOIDs first
                else:
                    score_key = -score  # descending = strong consensus first
                return (bucket, score_key, t)

            try:
                consensus_entries = sorted(consensus_entries,
                                            key=_consensus_sort_key)
            except Exception:
                pass  # fall back to upstream order if anything goes wrong

            # v4.13.62: Render consensus entries in chunks of 5 via
            # win.after(0). Each entry creates ~30 widgets (entry frame,
            # row, arrow button, ticker label, owned badge, consensus
            # badge, model count, confidence summary, detail frame, plus
            # a per-prediction sub-row with 5-6 labels). With 30+
            # tickers a synchronous loop builds 1000+ widgets in one
            # Tk callback — the dominant cost in the dialog's 30-second
            # open. Chunking lets the dialog appear immediately and
            # entries fill in over a few seconds while the user can
            # already scroll, click, or close.
            def _render_consensus_chunk(start_idx=0):
                # Bail if the user closed the window before this fired.
                try:
                    if (not hasattr(self, '_track_record_win')
                            or self._track_record_win is None
                            or not self._track_record_win.winfo_exists()):
                        return
                except Exception:
                    return
                end_idx = min(start_idx + 5, len(consensus_entries))
                for entry in consensus_entries[start_idx:end_idx]:
                    _render_one_consensus_entry(entry)
                if end_idx < len(consensus_entries):
                    try:
                        win.after(0, lambda i=end_idx:
                                   _render_consensus_chunk(i))
                    except Exception:
                        # If after() fails, fall back to synchronous
                        # rendering of remaining entries.
                        for entry in consensus_entries[end_idx:]:
                            _render_one_consensus_entry(entry)

            def _render_one_consensus_entry(entry):
                ticker = entry['ticker']

                # v4.8.13.2: Each ticker gets its own container that holds
                # BOTH the main row AND its detail frame as children. This
                # way pack(side='top') inside the container puts the detail
                # right under its own row — not at the bottom of the
                # consensus_card with all other details piled together.
                entry_container = tk.Frame(consensus_card, bg=c['card'])
                entry_container.pack(side='top', fill='x')

                # Main row (child of entry_container, not consensus_card)
                row = tk.Frame(entry_container, bg=c['card'])
                row.pack(side='top', fill='x', pady=1)

                # Expand toggle button — placed first so we can update its
                # text label when toggled
                is_expanded = ticker in self._consensus_expanded
                arrow_btn = tk.Button(row,
                                       text='▾' if is_expanded else '▸',
                                       bg=c['card'], fg=c['text'],
                                       font=('Consolas', 9, 'bold'),
                                       relief='flat', cursor='hand2',
                                       padx=4, pady=0)
                arrow_btn.pack(side='left')

                # Ticker
                tk.Label(row, text=f" {ticker:6} ",
                         bg=c['card'], fg=c['text'],
                         font=('Consolas', 9, 'bold')
                         ).pack(side='left')

                # v4.13.4: OWNED badge for portfolio tickers
                if ticker.upper() in _held:
                    tk.Label(row, text=' OWNED ',
                             bg=c.get('card2', c['card']),
                             fg=c.get('blue', c['accent']),
                             font=('Segoe UI', 7, 'bold'),
                             padx=4, pady=0
                             ).pack(side='left', padx=(2, 4))

                # Consensus badge — color-coded by consensus_color
                badge_color = c.get(entry['consensus_color'], c['muted'])
                tk.Label(row,
                         text=f" {entry['consensus_label']:46} ",
                         bg=c['card'], fg=badge_color,
                         font=('Consolas', 9, 'bold')
                         ).pack(side='left')

                # Model count
                tk.Label(row,
                         text=f" {entry['model_count']} model(s) ",
                         bg=c['card'], fg=c['dim'],
                         font=('Consolas', 8)
                         ).pack(side='left')

                # Confidence summary
                tk.Label(row,
                         text=f" {entry['confidence_summary']:11} ",
                         bg=c['card'], fg=c['muted'],
                         font=('Consolas', 8)
                         ).pack(side='left')

                # Detail frame — child of entry_container so it packs
                # right below the main row. Hidden until expanded.
                detail_frame = tk.Frame(entry_container, bg=c['bg'])
                if is_expanded:
                    detail_frame.pack(side='top', fill='x',
                                        padx=(24, 0), pady=(2, 6))

                # v4.14.0 stage 6d: canonicalize model label at display
                # so past records frozen with "My Groq" / "my Minstral"
                # show as "Groq" / "Mistral" in Track Record.
                try:
                    import tm_api_providers as _tmap6d_tr
                    _norm_tr = _tmap6d_tr.canonicalize_model_label
                except Exception:
                    _norm_tr = lambda x: x

                # Build the per-model detail rows inside detail_frame
                for p in entry['predictions']:
                    drow = tk.Frame(detail_frame, bg=c['bg'])
                    drow.pack(side='top', fill='x', pady=1)

                    model_name = _norm_tr(p.get('model') or '?')[:18]
                    tk.Label(drow, text=f"  {model_name:18}",
                             bg=c['bg'], fg=c['dim'],
                             font=('Consolas', 8)
                             ).pack(side='left')

                    d = (p.get('direction') or '?').upper()
                    # v4.8.14: NO_CALL gets its own display
                    if d == 'NO_CALL':
                        d_display = 'no-call'
                        d_color = c['muted']
                    else:
                        d_display = d
                        d_color = (c['green'] if d == 'BUY'
                                   else c['amber'] if d == 'HOLD'
                                   else c['red'] if d == 'AVOID'
                                   else c['muted'])
                    tk.Label(drow, text=f"  {d_display:8}",
                             bg=c['bg'], fg=d_color,
                             font=('Consolas', 8, 'bold')
                             ).pack(side='left')

                    cf = (p.get('confidence') or '?').upper()
                    tk.Label(drow, text=f"  {cf:8}",
                             bg=c['bg'], fg=c['text'],
                             font=('Consolas', 8)
                             ).pack(side='left')

                    # Levels if available
                    t_lvl = p.get('target')
                    s_lvl = p.get('stop')
                    if t_lvl is not None or s_lvl is not None:
                        lvl_str = ""
                        if t_lvl is not None:
                            lvl_str += f"target ${t_lvl:g}"
                        if s_lvl is not None:
                            if lvl_str:
                                lvl_str += " / "
                            lvl_str += f"stop ${s_lvl:g}"
                        tk.Label(drow, text=f"  {lvl_str}",
                                 bg=c['bg'], fg=c['text'],
                                 font=('Consolas', 8)
                                 ).pack(side='left')

                    # Timestamp
                    ts = (p.get('timestamp') or '')[:16].replace('T', ' ')
                    tk.Label(drow, text=f"  ({ts})",
                             bg=c['bg'], fg=c['dim'],
                             font=('Consolas', 8)
                             ).pack(side='right')

                # Wire the arrow button to toggle this specific detail
                # frame's visibility. Closure captures arrow_btn + frame.
                def _make_inline_toggle(t=ticker, btn=arrow_btn,
                                          frame=detail_frame):
                    def _toggle():
                        if t in self._consensus_expanded:
                            self._consensus_expanded.discard(t)
                            try: frame.pack_forget()
                            except Exception: pass
                            try: btn.config(text='▸')
                            except Exception: pass
                        else:
                            self._consensus_expanded.add(t)
                            try:
                                frame.pack(side='top', fill='x',
                                            padx=(24, 0), pady=(2, 6))
                            except Exception:
                                pass
                            try: btn.config(text='▾')
                            except Exception: pass
                    return _toggle
                try:
                    arrow_btn.config(command=_make_inline_toggle())
                except Exception:
                    pass

            # v4.13.62: Kick off the chunked render. First batch of 5
            # runs immediately on the next Tk idle so the consensus
            # card frame paints with header + dropdown + column header
            # first, then entries flow in batch-by-batch.
            try:
                win.after(0, _render_consensus_chunk)
            except Exception:
                # Fall back to synchronous render if after() fails
                for entry in consensus_entries:
                    _render_one_consensus_entry(entry)
        # ════════════════════════════════════════════════════════════════
        # End consensus card
        # ════════════════════════════════════════════════════════════════

        recent_card = tk.Frame(body, bg=c['card'], padx=16, pady=10)
        recent_card.pack(side='top', fill='x', pady=(8, 0))

        # Header row: "RECENT PREDICTIONS" label + Clear buttons on the right
        hdr_row = tk.Frame(recent_card, bg=c['card'])
        hdr_row.pack(side='top', fill='x', pady=(0, 4))
        tk.Label(hdr_row, text="RECENT PREDICTIONS",
                 bg=c['card'], fg=c['accent'],
                 font=('Segoe UI', 9, 'bold')
                 ).pack(side='left')

        # Clear buttons on the right side
        all_preds = self.predictions_log.get_all()
        all_preds.sort(key=lambda p: p.get('timestamp', ''), reverse=True)

        if all_preds:
            tk.Button(hdr_row, text="Clear all",
                      bg=c['red'], fg=c['bg'],
                      font=('Segoe UI', 8, 'bold'),
                      relief='flat', padx=8, pady=2, cursor='hand2',
                      command=self._clear_all_predictions
                      ).pack(side='right', padx=(4, 0))
            tk.Button(hdr_row, text="Clear >7 days",
                      bg=c['amber'], fg=c['bg'],
                      font=('Segoe UI', 8, 'bold'),
                      relief='flat', padx=8, pady=2, cursor='hand2',
                      command=lambda: self._clear_old_predictions(7)
                      ).pack(side='right', padx=(4, 0))

        # v4.8.11: Restore from backup button. Always visible if any
        # predictions_backup_*.jsonl files exist — especially useful
        # right after Clear all, where the prior backups still sit on
        # disk but the log itself is empty.
        # v4.8.14: button label distinguishes restored from unrestored
        # backups so the count isn't misleading.
        try:
            backups = self.predictions_log.list_backups()
        except Exception:
            backups = []
        if backups:
            unrestored = sum(1 for b in backups
                              if not b.get('restored_at'))
            if unrestored > 0:
                btn_label = (f"Restore ({unrestored} new)"
                              if unrestored < len(backups)
                              else f"Restore ({len(backups)})")
            else:
                btn_label = f"Restore ({len(backups)} all done)"
            tk.Button(hdr_row, text=btn_label,
                      bg=c['card'], fg=c['text'],
                      font=('Segoe UI', 8, 'bold'),
                      relief='flat', padx=8, pady=2, cursor='hand2',
                      command=self._show_restore_dialog
                      ).pack(side='right', padx=(4, 0))

        # Column header — now includes MODEL column
        hdr = tk.Frame(recent_card, bg=c['card'])
        hdr.pack(side='top', fill='x', pady=(2, 4))
        tk.Label(hdr,
                 text="DATE        TICKER  CALL    CONF      LEVELS                          MODEL          OUTCOME",
                 bg=c['card'], fg=c['dim'],
                 font=('Consolas', 7, 'bold')
                 ).pack(side='left')

        # ── v4.13.60: defer the per-prediction list rendering ────────
        # The recent-predictions table builds 8-10 widgets per row * 20
        # rows = 160-200 widgets. Combined with the BREAKDOWN, AGE
        # BUCKETS, and other cards this dialog easily creates 500+
        # widgets synchronously, which feels slow. Defer the table
        # build to after the dialog is mapped — user sees the stat
        # cards instantly, then the per-prediction list fills in
        # ~50ms later. Total wall-clock work is the same; perceived
        # latency drops substantially.
        def _render_recent_predictions_list():
            try:
                if not (hasattr(self, '_track_record_win')
                        and self._track_record_win is not None
                        and self._track_record_win.winfo_exists()):
                    return  # window was closed before deferred build ran
                for p in all_preds[:20]:
                    ts = p.get('timestamp', '')[:10]
                    tk_lbl = p.get('ticker', '?')
                    d = (p.get('direction') or '?').upper()
                    t = p.get('target')
                    s = p.get('stop')
                    status = p.get('status', '?')
                    conf = p.get('confidence', '?')
                    model = p.get('model', '') or '?'
                    row = tk.Frame(recent_card, bg=c['card'])
                    row.pack(side='top', fill='x', pady=1)
                    # Date
                    tk.Label(row, text=f"  {ts}  ", bg=c['card'], fg=c['dim'],
                             font=('Consolas', 8)).pack(side='left')
                    # Ticker
                    tk.Label(row, text=f"{tk_lbl:6} ", bg=c['card'], fg=c['text'],
                             font=('Consolas', 8, 'bold')).pack(side='left')
                    # Direction
                    dir_color = (c['green'] if d == 'BUY'
                                  else c['amber'] if d == 'HOLD'
                                  else c['red'] if d == 'AVOID'
                                  else c['muted'])
                    tk.Label(row, text=f"{d:5} ", bg=c['card'], fg=dir_color,
                             font=('Consolas', 8, 'bold')).pack(side='left')
                    # Confidence
                    tk.Label(row, text=f"{conf or '?':9}",
                             bg=c['card'], fg=c['text'],
                             font=('Consolas', 8)).pack(side='left')
                    # Levels
                    if d == 'AVOID':
                        level_text = "no entry recommended" + (" " * 14)
                        level_color = c['muted']
                    else:
                        t_str = f"${t:g}" if t is not None else "—"
                        s_str = f"${s:g}" if s is not None else "—"
                        level_text = f"target:{t_str:8} stop:{s_str:8}"
                        level_color = c['text']
                    tk.Label(row, text=level_text,
                             bg=c['card'], fg=level_color,
                             font=('Consolas', 8)).pack(side='left')
                    # Model column
                    model_short = ((model[:13] + '…')
                                    if len(model) > 14 else model)
                    tk.Label(row, text=f"{model_short:14} ",
                             bg=c['card'], fg=c['muted'],
                             font=('Consolas', 8)).pack(side='left')
                    # Outcome status
                    status_color = c['muted']
                    status_label = status
                    if status == 'target_hit':
                        status_color = c['green']
                        status_label = "✓ target hit"
                    elif status == 'stop_hit':
                        status_color = c['red']
                        status_label = "✗ stop hit"
                    elif status == 'expired':
                        status_color = c['amber']
                        status_label = "expired"
                    elif status == 'sold':
                        status_color = c['blue'] if 'blue' in c else c['accent']
                        status_label = "sold"
                    elif status == 'open':
                        status_color = c['text']
                        status_label = "open"
                    tk.Label(row, text=f"  → {status_label}",
                             bg=c['card'], fg=status_color,
                             font=('Consolas', 8)).pack(side='left')
            except Exception:
                # Table render failure should not crash the whole window.
                pass

        # Show a "loading" label that gets replaced by the real rows
        loading_lbl = tk.Label(recent_card,
                                text="  Loading recent predictions...",
                                bg=c['card'], fg=c['dim'],
                                font=('Consolas', 8, 'italic'))
        loading_lbl.pack(side='top', fill='x', pady=2)

        def _do_deferred_load():
            try:
                loading_lbl.destroy()
            except Exception:
                pass
            _render_recent_predictions_list()

        try:
            # 50ms is enough for the dialog to finish mapping; the
            # human eye perceives this as instant.
            self._track_record_win.after(50, _do_deferred_load)
        except Exception:
            # If after() fails for any reason, do it synchronously
            try: loading_lbl.destroy()
            except Exception: pass
            _render_recent_predictions_list()
        # ── end v4.13.60 deferred render ─────────────────────────────

    def _wire_real_trades_click(self, card_frame, big_lbl):
        """v4.13.64: Make the REAL TRADES card open Trade Log on click.
        Replaces the toolbar Trade Log button. Pointer cursor +
        underline-on-hover live on the big number; click on any part
        of the card opens the dialog. Tooltip on the big number.

        v4.13.64.1: Tooltip binds <Button-1>/<Enter>/<Leave> WITHOUT
        add='+', so it replaces any prior bindings on the same widget.
        Attach Tooltip FIRST on the big number, then bind our handlers
        with add='+' so they layer on top. (The card_frame and other
        child labels don't get a Tooltip, so their bindings were never
        at risk — only big_lbl was being stomped, but the per-child
        binds were already correct via add='+'.)"""
        try:
            import tkinter.font as tkfont
            actual = tkfont.Font(font=big_lbl.cget('font')).actual()
            f_normal = tkfont.Font(family=actual.get('family'),
                                   size=actual.get('size'),
                                   weight=actual.get('weight'),
                                   slant=actual.get('slant'),
                                   underline=0)
            f_hover = tkfont.Font(family=actual.get('family'),
                                  size=actual.get('size'),
                                  weight=actual.get('weight'),
                                  slant=actual.get('slant'),
                                  underline=1)
            big_lbl.configure(font=f_normal)
        except Exception:
            f_normal = None
            f_hover = None

        def _enter(_e=None):
            if f_hover is not None:
                try: big_lbl.configure(font=f_hover)
                except Exception: pass
        def _leave(_e=None):
            if f_normal is not None:
                try: big_lbl.configure(font=f_normal)
                except Exception: pass
        def _click(_e=None):
            try:
                if self.app is not None and hasattr(
                        self.app, '_show_trade_history'):
                    self.app._show_trade_history()
                else:
                    self._show_trade_history()
            except Exception as e:
                try:
                    if self.app is not None:
                        self.app._log(
                            f"Trade Log open failed: {e}", 'red')
                except Exception:
                    pass

        # v4.13.64.1: Tooltip FIRST on big_lbl (Tooltip replaces any
        # prior <Button-1>/<Enter>/<Leave> binds — must precede ours)
        try:
            from tired_market import Tooltip
            Tooltip(big_lbl, "Click to view trade history")
        except Exception:
            pass

        # Hover state on big number — bind AFTER Tooltip with add='+'
        big_lbl.configure(cursor='hand2')
        big_lbl.bind('<Enter>', _enter, add='+')
        big_lbl.bind('<Leave>', _leave, add='+')
        big_lbl.bind('<Button-1>', _click, add='+')

        # Click on card frame + every child label (no Tooltip on these,
        # so order doesn't matter for them)
        try:
            card_frame.configure(cursor='hand2')
        except Exception:
            pass
        card_frame.bind('<Button-1>', _click, add='+')
        for child in card_frame.winfo_children():
            if child is big_lbl:
                continue  # already bound above
            try:
                child.configure(cursor='hand2')
            except Exception:
                pass
            try:
                child.bind('<Button-1>', _click, add='+')
            except Exception:
                pass

    def _clear_all_predictions(self):
        """Confirm with user, then nuke all predictions. Auto-backs up
        the file before deleting."""
        if not self.predictions_log:
            return
        all_preds = self.predictions_log.get_all()
        n = len(all_preds)
        if n == 0:
            return

        confirm = tk.Toplevel(self.window or self.parent)
        confirm.title("Clear all predictions?")
        confirm.configure(bg=THEME['bg'])
        confirm.transient(self.window or self.parent)
        confirm.grab_set()

        # Center it
        confirm.geometry("440x200")

        tk.Label(confirm,
                 text="⚠  Delete all predictions?",
                 bg=THEME['bg'], fg=THEME['amber'],
                 font=('Segoe UI', 13, 'bold')
                 ).pack(side='top', pady=(20, 6))
        tk.Label(confirm,
                 text=f"This will remove {n} predictions from the log.\n"
                      f"A backup will be saved to:\n"
                      f"data/predictions_backup_TIMESTAMP.jsonl\n"
                      f"so you can recover if needed.",
                 bg=THEME['bg'], fg=THEME['text'],
                 font=('Segoe UI', 9), justify='center'
                 ).pack(side='top', pady=(0, 12))

        btn_row = tk.Frame(confirm, bg=THEME['bg'])
        btn_row.pack(side='bottom', pady=(8, 16))

        result = {'confirmed': False}
        def _yes():
            result['confirmed'] = True
            confirm.destroy()
        def _no():
            confirm.destroy()

        tk.Button(btn_row, text="Cancel",
                  bg=THEME['card'], fg=THEME['text'],
                  font=('Segoe UI', 9), relief='flat',
                  padx=14, pady=4, cursor='hand2',
                  command=_no).pack(side='left', padx=8)
        tk.Button(btn_row, text=f"Clear all {n}",
                  bg=THEME['red'], fg=THEME['bg'],
                  font=('Segoe UI', 9, 'bold'), relief='flat',
                  padx=14, pady=4, cursor='hand2',
                  command=_yes).pack(side='left', padx=8)

        confirm.wait_window()

        if result['confirmed']:
            cleared = self.predictions_log.clear_all(backup=True)
            self._log_to_main(
                f"Cleared {cleared} predictions (backed up to "
                f"data/predictions_backup_*.jsonl)", 'amber')
            # Refresh the Track Record window
            if self._track_record_win is not None:
                try:
                    self._track_record_win.destroy()
                except Exception:
                    pass
                self._track_record_win = None
                self._show_track_record(
                    parent_override=self.parent if self.parent else None)

    def _clear_old_predictions(self, days: int):
        """Confirm + clear predictions older than `days` days."""
        if not self.predictions_log:
            return
        all_preds = self.predictions_log.get_all()
        if not all_preds:
            return

        # Count how many would be cleared
        cutoff = datetime.now() - timedelta(days=days)
        would_clear = 0
        for p in all_preds:
            ts_str = p.get('timestamp', '')
            if ts_str:
                try:
                    pts = datetime.fromisoformat(ts_str)
                    if pts < cutoff:
                        would_clear += 1
                except (ValueError, TypeError):
                    pass

        if would_clear == 0:
            self._log_to_main(
                f"No predictions older than {days} days to clear.",
                'muted')
            return

        confirm = tk.Toplevel(self.window or self.parent)
        confirm.title(f"Clear predictions older than {days} days?")
        confirm.configure(bg=THEME['bg'])
        confirm.transient(self.window or self.parent)
        confirm.grab_set()
        confirm.geometry("440x200")

        tk.Label(confirm,
                 text=f"Clear predictions older than {days} days?",
                 bg=THEME['bg'], fg=THEME['amber'],
                 font=('Segoe UI', 13, 'bold')
                 ).pack(side='top', pady=(20, 6))
        tk.Label(confirm,
                 text=f"This will remove {would_clear} predictions.\n"
                      f"Recent ones (within {days} days) will be kept.\n"
                      f"A backup will be saved before deletion.",
                 bg=THEME['bg'], fg=THEME['text'],
                 font=('Segoe UI', 9), justify='center'
                 ).pack(side='top', pady=(0, 12))

        btn_row = tk.Frame(confirm, bg=THEME['bg'])
        btn_row.pack(side='bottom', pady=(8, 16))

        result = {'confirmed': False}
        def _yes():
            result['confirmed'] = True
            confirm.destroy()
        def _no():
            confirm.destroy()

        tk.Button(btn_row, text="Cancel",
                  bg=THEME['card'], fg=THEME['text'],
                  font=('Segoe UI', 9), relief='flat',
                  padx=14, pady=4, cursor='hand2',
                  command=_no).pack(side='left', padx=8)
        tk.Button(btn_row, text=f"Clear {would_clear}",
                  bg=THEME['amber'], fg=THEME['bg'],
                  font=('Segoe UI', 9, 'bold'), relief='flat',
                  padx=14, pady=4, cursor='hand2',
                  command=_yes).pack(side='left', padx=8)

        confirm.wait_window()

        if result['confirmed']:
            cleared = self.predictions_log.clear_older_than(days, backup=True)
            self._log_to_main(
                f"Cleared {cleared} predictions older than {days} days "
                f"(backed up).", 'amber')
            if self._track_record_win is not None:
                try:
                    self._track_record_win.destroy()
                except Exception:
                    pass
                self._track_record_win = None
                self._show_track_record(
                    parent_override=self.parent if self.parent else None)

    def _show_cooldown_confirm(self, cd: dict) -> bool:
        """v4.8.12: Inline confirm dialog when user clicks Discover during
        an active rate-limit cooldown. Returns True if user wants to
        proceed anyway, False to cancel."""
        try:
            parent = self._discover_win or self.window or self.parent
        except Exception:
            parent = self.parent

        win = tk.Toplevel(parent)
        win.title("Rate-limit cooldown active")
        win.configure(bg=THEME['bg'])
        win.transient(parent)
        win.grab_set()
        win.geometry("520x280")

        tk.Label(win,
                 text="⏳  Rate-limit cooldown is active",
                 bg=THEME['bg'], fg=THEME['amber'],
                 font=('Segoe UI', 13, 'bold')
                 ).pack(side='top', pady=(18, 4))

        until = cd.get('cooldown_until_str', '?')
        remaining = cd.get('remaining_min', 0)
        severity = cd.get('severity_pct', 0)
        cooldown_min = cd.get('current_cooldown_min', 60)
        history_count = cd.get('history_count', 0)

        info_lines = [
            f"Yahoo Finance was rate-limiting us at "
            f"{severity:.0f}% no-quote during the last scan.",
            f"Cooldown active until {until} ET "
            f"({remaining} min remaining).",
            "",
            f"Current auto-tuned cooldown: {cooldown_min} min "
            f"(based on {history_count} past event(s))",
            "",
            "Run anyway only if you have reason to believe yfinance "
            "has actually unblocked us — otherwise you may extend "
            "the block.",
        ]
        tk.Label(win,
                 text="\n".join(info_lines),
                 bg=THEME['bg'], fg=THEME['text'],
                 font=('Segoe UI', 9), justify='center', wraplength=480
                 ).pack(side='top', pady=(0, 14))

        result = {'override': False}
        def _yes():
            result['override'] = True
            win.destroy()
        def _no():
            win.destroy()

        btn_row = tk.Frame(win, bg=THEME['bg'])
        btn_row.pack(side='bottom', pady=(0, 18))
        tk.Button(btn_row, text="Wait it out (cancel)",
                  bg=THEME['card'], fg=THEME['text'],
                  font=('Segoe UI', 9, 'bold'), relief='flat',
                  padx=14, pady=4, cursor='hand2',
                  command=_no
                  ).pack(side='left', padx=8)
        tk.Button(btn_row, text="Run anyway",
                  bg=THEME['amber'], fg=THEME['bg'],
                  font=('Segoe UI', 9, 'bold'), relief='flat',
                  padx=14, pady=4, cursor='hand2',
                  command=_yes
                  ).pack(side='left', padx=8)

        win.wait_window()
        return result['override']

    def _show_restore_dialog(self):
        """v4.8.11: Dialog listing prediction backups, with merge/replace
        choices. Lets the user recover after a regretted Clear all, or
        consolidate accidentally-split logs."""
        if not self.predictions_log:
            return
        try:
            backups = self.predictions_log.list_backups()
        except Exception:
            backups = []
        if not backups:
            self._log_to_main("No prediction backups found.", 'muted')
            return

        win = tk.Toplevel(self.window or self.parent)
        win.title("Restore predictions from backup")
        win.configure(bg=THEME['bg'])
        win.transient(self.window or self.parent)
        win.grab_set()
        win.geometry("620x440")

        tk.Label(win,
                 text="Restore predictions from a backup",
                 bg=THEME['bg'], fg=THEME['text'],
                 font=('Segoe UI', 12, 'bold')
                 ).pack(side='top', pady=(14, 4))
        tk.Label(win,
                 text=("Pick a backup, then choose Merge (add only entries "
                       "not already in your log) or Replace (overwrite log "
                       "with backup; current log is auto-saved first).") ,
                 bg=THEME['bg'], fg=THEME['dim'],
                 font=('Segoe UI', 8), justify='center', wraplength=560
                 ).pack(side='top', pady=(0, 10))

        # List of backups in a scrollable frame
        list_frame = tk.Frame(win, bg=THEME['card'],
                                bd=1, relief='solid')
        list_frame.pack(side='top', fill='both', expand=True,
                          padx=14, pady=(0, 10))

        canvas = tk.Canvas(list_frame, bg=THEME['card'],
                             highlightthickness=0)
        scroll = tk.Scrollbar(list_frame, orient='vertical',
                                command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        inner = tk.Frame(canvas, bg=THEME['card'])
        canvas.create_window((0, 0), window=inner, anchor='nw')
        def _on_inner_config(_e=None):
            canvas.configure(scrollregion=canvas.bbox('all'))
        inner.bind('<Configure>', _on_inner_config)

        selected = {'idx': 0}  # default to newest
        radio_var = tk.IntVar(value=0)

        for i, b in enumerate(backups):
            row = tk.Frame(inner, bg=THEME['card'])
            row.pack(side='top', fill='x', padx=6, pady=2)
            rb = tk.Radiobutton(
                row, variable=radio_var, value=i,
                bg=THEME['card'], fg=THEME['text'],
                selectcolor=THEME['bg'],
                activebackground=THEME['card'],
                activeforeground=THEME['text'])
            rb.pack(side='left')
            kb = b.get('size', 0) / 1024.0
            # v4.8.14: show restored_at if this backup was previously restored
            restored_at = b.get('restored_at')
            if restored_at:
                # Friendly format: trim ISO to "MM-DD HH:MM"
                ra_short = restored_at[5:16].replace('T', ' ')
                restored_str = f"  ✓ already restored {ra_short}"
                label_color = THEME['dim']
            else:
                restored_str = ""
                label_color = THEME['text']
            label_text = (
                f"{b['timestamp']}  —  {b['count']} prediction(s)  "
                f"({kb:.1f} KB){restored_str}")
            tk.Label(row, text=label_text,
                     bg=THEME['card'], fg=label_color,
                     font=('Consolas', 9), anchor='w'
                     ).pack(side='left', fill='x', expand=True, padx=(4, 0))

        def _do_restore(mode: str):
            i = radio_var.get()
            if i < 0 or i >= len(backups):
                return
            chosen = backups[i]
            try:
                added, total = self.predictions_log.restore_from_backup(
                    chosen['path'], mode=mode)
            except Exception as e:
                self._log_to_main(f"Restore failed: {e}", 'red')
                win.destroy()
                return
            if mode == 'replace':
                self._log_to_main(
                    f"Restored {added} predictions from "
                    f"{chosen['name']} (replace mode; previous log "
                    f"auto-backed up).", 'green')
            else:
                self._log_to_main(
                    f"Merged {added} new predictions from "
                    f"{chosen['name']}. Total now: {total}.", 'green')
            win.destroy()
            # Refresh Track Record window so the restored entries show up
            if self._track_record_win is not None:
                try: self._track_record_win.destroy()
                except Exception: pass
                self._track_record_win = None
                self._show_track_record(
                    parent_override=self.parent if self.parent else None)

        # Action buttons
        btn_row = tk.Frame(win, bg=THEME['bg'])
        btn_row.pack(side='bottom', pady=(0, 14))
        tk.Button(btn_row, text="Cancel",
                  bg=THEME['card'], fg=THEME['text'],
                  font=('Segoe UI', 9), relief='flat',
                  padx=14, pady=4, cursor='hand2',
                  command=win.destroy
                  ).pack(side='left', padx=8)
        tk.Button(btn_row, text="Merge",
                  bg=THEME['card'], fg=THEME['text'],
                  font=('Segoe UI', 9, 'bold'), relief='flat',
                  padx=14, pady=4, cursor='hand2',
                  command=lambda: _do_restore('merge')
                  ).pack(side='left', padx=8)
        tk.Button(btn_row, text="Replace",
                  bg=THEME['amber'], fg=THEME['bg'],
                  font=('Segoe UI', 9, 'bold'), relief='flat',
                  padx=14, pady=4, cursor='hand2',
                  command=lambda: _do_restore('replace')
                  ).pack(side='left', padx=8)

    # ─── Check Now logic ───

    def _check_now_all(self) -> None:
        """Scan all tradable holdings (skip locked ones during normal Check Now)."""
        # v4.10.9: log every code path. See _check_now_single for rationale.
        # Pause gate — refuse if AI is paused
        if is_ai_paused():
            self._set_status("AI is paused — click the badge in the main window to resume",
                              THEME['amber'])
            if self.log_callback:
                try:
                    self.log_callback(
                        "Check Now skipped: AI is paused. Click the AI "
                        "badge in the main window to resume.",
                        'amber')
                except Exception: pass
            return

        # Debounce
        now = time.time()
        if now - self._last_check_now_time < CHECK_NOW_DEBOUNCE_SEC:
            if self.log_callback:
                try:
                    secs_left = int(CHECK_NOW_DEBOUNCE_SEC -
                                     (now - self._last_check_now_time))
                    self.log_callback(
                        f"Check Now skipped: too soon since last check "
                        f"(wait ~{secs_left}s).",
                        'muted')
                except Exception: pass
            return
        self._last_check_now_time = now

        if self._active_request is not None:
            self._set_status("Already analyzing — wait for current request to finish")
            if self.log_callback:
                try:
                    self.log_callback(
                        f"Check Now skipped: another scan is already "
                        f"running ({getattr(self, '_current_ticker', '?')}).",
                        'amber')
                except Exception: pass
            return

        tradable = [h for h in self.mgr.holdings if h.get('tradable', True)]
        skipped = len(self.mgr.holdings) - len(tradable)

        if not tradable:
            self._set_status("No tradable holdings to scan", THEME['amber'])
            if skipped > 0:
                self.signals_text.insert('1.0',
                    f"\n[Skipped {skipped} locked position(s) — they're "
                    f"checked once a day in background mode]\n",
                    'hint')
            if self.log_callback:
                try:
                    self.log_callback(
                        f"Check Now: no tradable holdings to scan "
                        f"({skipped} locked, skipped).",
                        'amber')
                except Exception: pass
            return

        if skipped > 0:
            self._append_to_signals_top(
                f"[Skipped {skipped} locked position(s) — checked once daily]")

        self._check_now_queue = list(tradable)
        self._set_status(f"Scanning {len(tradable)} "
                          f"position{'s' if len(tradable) != 1 else ''}...",
                          THEME['green'])
        if self.log_callback:
            try:
                model = self._pick_model()
                ticker_list = ', '.join(h.get('ticker', '?') for h in tradable[:5])
                more = f" (+{len(tradable)-5} more)" if len(tradable) > 5 else ""
                self.log_callback(
                    f"Check Now started for {len(tradable)} position(s): "
                    f"{ticker_list}{more}. Using '{model}'. Each takes "
                    f"30-90s.",
                    'green')
            except Exception: pass
        self._process_next_in_queue()

    def _check_now_single(self, ticker: str) -> None:
        """Re-check a specific holding (works for both tradable and locked)."""
        # v4.10.9: log every code path so user can see what happened.
        # Before, a skipped Check Now (paused, busy, missing ticker)
        # was completely silent — no signal at all that the click did
        # anything. Now every outcome appears in the activity feed.
        # Pause gate
        if is_ai_paused():
            self._set_status("AI is paused — click the badge in the main window to resume",
                              THEME['amber'])
            if self.log_callback:
                try:
                    self.log_callback(
                        f"Check Now skipped for {ticker}: AI is paused. "
                        f"Click the AI badge in the main window to resume.",
                        'amber')
                except Exception: pass
            return
        if self._active_request is not None:
            self._set_status("Already analyzing — wait for current request to finish")
            if self.log_callback:
                try:
                    self.log_callback(
                        f"Check Now skipped for {ticker}: another scan "
                        f"is already running.",
                        'amber')
                except Exception: pass
            return
        for h in self.mgr.holdings:
            if h.get('ticker', '').upper() == ticker.upper():
                self._check_now_queue = [h]
                self._set_status(f"Analyzing {ticker}...", THEME['green'])
                if self.log_callback:
                    try:
                        model = self._pick_model()
                        self.log_callback(
                            f"Check Now started for {ticker} using "
                            f"'{model}'. Model typically takes 30-90s "
                            f"to respond.",
                            'green')
                    except Exception: pass
                self._process_next_in_queue()
                return
        # Ticker not found in holdings
        if self.log_callback:
            try:
                self.log_callback(
                    f"Check Now: ticker {ticker} not found in holdings.",
                    'red')
            except Exception: pass

    def _process_next_in_queue(self) -> None:
        """Pull the next holding off the queue and start its analysis."""
        if not self._check_now_queue:
            self._set_status("Done.", THEME['green'])
            return
        holding = self._check_now_queue.pop(0)
        self._analyze_holding(holding)

    def _analyze_holding(self, holding: dict) -> None:
        """Build the prompt, optionally show it, send to AI, stream the response
        into the signals view."""
        ticker = holding.get('ticker', '?')
        # v4.13.2: per-holding path takes precedence over global setting.
        # v4.14.5.62-per-holding-tier: when a holding has no tier, fall back to
        # the SAFE default (moderate) — NOT the window's global path, which
        # could be 'lottery'/Speculative and mis-frame an owned large-cap.
        # Owned-position analysis must never inherit the global analysis path.
        path = holding.get('path') or DEFAULT_PATH
        is_locked = not holding.get('tradable', True)

        # v4.13.2: clear the signals text widget so output from a
        # prior streaming run can't visually pollute this one.
        try:
            if hasattr(self, 'signals_text') and self.signals_text is not None:
                self.signals_text.config(state='normal')
                self.signals_text.delete('1.0', 'end')
        except Exception:
            pass

        # Build prompt
        # v4.10.12: catch + log prompt builder failures.
        # v4.10.13: capture full traceback to debug file for analysis.
        # v4.13.2: tradable holdings now use the consensus-style owned-
        # position prompt so single-model Check Now answers the same
        # question consensus does (no more SELL vs HOLD framing drift).
        try:
            if is_locked:
                prompt, debug = self.prompt_builder.build_locked_analysis(holding, path)
            else:
                try:
                    import tm_consensus
                    prompt, debug = tm_consensus.build_owned_position_prompt(
                        holding, path, self.prompt_builder,
                        predictions_log=getattr(self, 'predictions_log', None),
                    )
                except Exception:
                    # Fallback to legacy prompt if tm_consensus import fails
                    prompt, debug = self.prompt_builder.build_holding_analysis(holding, path)
        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}"
            import traceback
            tb = traceback.format_exc()
            # Find the user data dir relative to the signals_log path
            try:
                debug_path = self.signals_log.log_path.parent / \
                    "prompt_build_errors.log"
                with open(debug_path, 'a', encoding='utf-8') as f:
                    f.write(f"\n=== {datetime.now().isoformat()} "
                            f"CHECK_NOW ticker={ticker} "
                            f"is_locked={is_locked} path={path} ===\n")
                    f.write(f"Holding: {holding}\n")
                    f.write(f"Error: {err_msg}\n")
                    f.write(f"Full traceback:\n{tb}\n")
            except Exception:
                pass
            self._set_status(f"Prompt build failed for {ticker}", THEME['red'])
            if self.log_callback:
                try:
                    self.log_callback(
                        f"Check Now FAILED for {ticker} — couldn't build "
                        f"prompt: {err_msg[:120]} "
                        f"(full trace in data/prompt_build_errors.log)",
                        'red')
                except Exception:
                    pass
            self._active_request = None
            # Continue with next ticker if there's a queue
            if self._check_now_queue:
                try:
                    self._process_next_in_queue()
                except Exception:
                    pass
            return

        # Show prompt panel if toggle is on
        if self._show_prompts_var and self._show_prompts_var.get():
            self._show_prompt_preview(ticker, prompt, debug,
                                       on_approve=lambda: self._fire_request(
                                           ticker, prompt, is_locked, path),
                                       on_skip=self._on_user_skip)
        else:
            # Fire directly
            self._fire_request(ticker, prompt, is_locked, path)

    def _show_prompt_preview(self, ticker: str, prompt: str, debug: dict,
                              on_approve: Callable, on_skip: Callable) -> None:
        """Show what's about to be sent to the AI. User approves or skips."""
        c = THEME
        win = tk.Toplevel(self.window)
        win.title(f"Prompt preview — {ticker}")
        win.configure(bg=c['bg'])
        win.geometry("700x520")
        win.transient(self.window)

        tk.Label(win,
                 text=f"About to send this prompt to the AI ({ticker}):",
                 bg=c['bg'], fg=c['muted'],
                 font=('Segoe UI', 9)
                 ).pack(side='top', fill='x', padx=12, pady=(10, 4))

        # Debug summary
        debug_lines = []
        for k, v in debug.items():
            debug_lines.append(f"{k}={v}")
        tk.Label(win, text="  ·  ".join(debug_lines),
                 bg=c['bg'], fg=c['dim'],
                 font=('Consolas', 8), wraplength=660, justify='left'
                 ).pack(side='top', fill='x', padx=12, pady=(0, 6))

        # The prompt itself
        body = tk.Frame(win, bg=c['bg'])
        body.pack(side='top', fill='both', expand=True, padx=12, pady=4)
        txt = tk.Text(body, bg=c['card'], fg=c['text'],
                       font=('Consolas', 9), wrap='word',
                       relief='flat', padx=10, pady=8, highlightthickness=0)
        sb = ttk.Scrollbar(body, orient='vertical', command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        txt.pack(side='left', fill='both', expand=True)
        txt.insert('1.0', prompt)
        txt.config(state='disabled')

        # Buttons
        btns = tk.Frame(win, bg=c['bg'], padx=12, pady=10)
        btns.pack(side='bottom', fill='x')

        def _approve():
            win.destroy()
            on_approve()

        def _skip():
            win.destroy()
            on_skip()

        tk.Button(btns, text="Send to AI", bg=c['green'], fg=c['bg'],
                  relief='flat', padx=18, pady=6,
                  font=('Segoe UI', 10, 'bold'), cursor='hand2',
                  command=_approve).pack(side='right')
        tk.Button(btns, text="Skip this", bg=c['card2'], fg=c['muted'],
                  relief='flat', padx=14, pady=4,
                  font=('Segoe UI', 9), cursor='hand2',
                  command=_skip).pack(side='right', padx=(0, 8))

    def _on_user_skip(self) -> None:
        """User clicked Skip on the prompt preview. Move to next in queue."""
        self.window.after(50, self._process_next_in_queue)

    def _fire_request(self, ticker: str, prompt: str, is_locked: bool,
                      path: str) -> None:
        """Actually send to the AI."""
        # Pick model: try qwen, fall back to boss/buddy
        model = self._pick_model()

        # Add header to signals view BEFORE response streams in
        c = THEME
        self.signals_text.config(state='normal')
        self.signals_text.insert('1.0', "\n")
        # Tag the start position so we can stream into the right place
        # (we're inserting at the TOP, newest-first)
        self._current_signal_start_idx = '1.0'
        # Build the header
        ts_str = datetime.now().strftime('%b %d %I:%M %p').replace(' 0', ' ')
        header_tag = 'locked_header' if is_locked else 'header'
        loc = " (locked)" if is_locked else ""
        self.signals_text.insert('1.0',
            "─" * 60 + "\n", 'meta')
        # We need to track where to insert streamed tokens; use a mark
        self.signals_text.mark_set('current_response', '1.0')
        self.signals_text.mark_gravity('current_response', 'left')
        # Insert placeholder for body — empty for now, mark sits BEFORE it
        self.signals_text.insert('1.0', "\n", 'body')
        # Insert header above
        self.signals_text.insert('1.0',
            f"  · {ts_str}  · {model}  · streaming...  · {path}\n",
            'meta')
        self.signals_text.insert('1.0', f"{ticker}{loc}", header_tag)

        self.signals_text.see('1.0')

        # Track for streaming
        self._stream_start_time = time.time()
        self._stream_token_count = 0
        self._current_ticker = ticker
        self._current_is_locked = is_locked
        self._current_path = path
        self._current_prompt = prompt
        self._current_model = model

        self._set_status(f"Asking {model} about {ticker}...", THEME['green'])

        # Ollama retired (cloud-only): the local Holdings-window AI dispatch is
        # gone. _fire_request is dead code — its only caller chain
        # (_check_now_*/_analyze_holding, the Holdings window's buttons) is
        # unreachable. Husk kept tidy; the tm_ai.AIRequest construction is
        # removed so tm_ai can be unimported.
        self._active_request = None

    def _pick_model(self) -> str:
        """Pick the AI model for analysis. Order:
           1. User-configured model from app config (if installed)
           2. Canonical fallback names (qwen/boss/buddy)
           3. First installed non-embedding model
           4. DEFAULT_ANALYSIS_MODEL as a last sentinel
        """
        # Ollama retired (cloud-only): no local model list. Dead code (the
        # Holdings-window analyze that called this is unreachable).
        installed = []
        cfg_model = self._get_configured_model()
        if cfg_model and cfg_model in installed:
            return cfg_model
        for m in ANALYSIS_MODEL_FALLBACKS:
            if m in installed:
                return m
        # Last resort: first available non-embedding model
        for m in installed:
            if 'embed' not in m.lower():
                return m
        return installed[0] if installed else DEFAULT_ANALYSIS_MODEL

    # ─── Streaming callbacks (background thread → main thread) ───

    def _on_token(self, token: str) -> None:
        # If window is closed, swallow tokens silently — we'll get the
        # full response in _on_done.
        if self.window is None:
            return
        self.window.after(0, self._insert_token, token)

    def _on_done(self, full_response: str) -> None:
        # v4.10.9: if the window is gone (user closed it mid-scan), we
        # still want to save the prediction and log to the activity feed.
        # Before, _on_done early-returned and the entire scan result was
        # discarded.
        if self.window is None:
            self._finalize_headless(full_response)
            return
        self.window.after(0, self._finalize, full_response)

    def _on_error(self, message: str) -> None:
        # v4.10.9: log error to activity feed even if window is closed.
        if self.window is None:
            self._handle_error_headless(message)
            return
        self.window.after(0, self._show_error, message)

    def _finalize_headless(self, full_response: str) -> None:
        """v4.10.9: Save prediction + log result when the Holdings window
        was closed mid-scan. Doesn't update any UI in the Holdings window
        (it's gone), but DOES write to predictions_log and the main
        window's activity feed via log_callback."""
        ticker = getattr(self, '_current_ticker', '?')
        try:
            duration = time.time() - getattr(
                self, '_stream_start_time', time.time())
        except Exception:
            duration = 0

        # Save the prediction (the important data)
        if (self.predictions_log is not None and _DISCOVER_AVAILABLE
                and tm_discover is not None):
            try:
                quote = self.cache.quote(ticker)
                current_price = (quote or {}).get('price')
                pred = tm_discover.parse_prediction(
                    full_response, ticker, current_price=current_price)
                if pred.get('direction'):
                    pred['path'] = getattr(self, '_current_path', '')
                    pred['source'] = ('holdings_locked'
                                       if getattr(self, '_current_is_locked', False)
                                       else 'holdings')
                    pred['model'] = getattr(self, '_current_model', '')
                    self.predictions_log.append(pred)
            except Exception:
                pass

        # Log to activity feed so user sees something happened
        if self.log_callback:
            try:
                self.log_callback(
                    f"Check Now finished for {ticker} ({duration:.0f}s) — "
                    f"window was closed, but result is saved. Reopen "
                    f"Holdings to view full analysis.",
                    'green')
            except Exception:
                pass

        self._active_request = None
        # Continue queue if there are more tickers waiting
        if self._check_now_queue:
            try:
                self._process_next_in_queue()
            except Exception:
                pass

    def _handle_error_headless(self, message: str) -> None:
        """v4.10.9: Log scan error when Holdings window was closed."""
        ticker = getattr(self, '_current_ticker', '?')
        if self.log_callback:
            try:
                self.log_callback(
                    f"Check Now error for {ticker}: {message[:80]}",
                    'red')
            except Exception:
                pass
        self._active_request = None
        if self._check_now_queue:
            try:
                self._process_next_in_queue()
            except Exception:
                pass

    def _insert_token(self, token: str) -> None:
        if self.window is None:
            return
        self._stream_token_count += 1
        self.signals_text.insert('current_response', token, 'body')

    def _finalize(self, full_response: str) -> None:
        if self.window is None:
            return
        duration = time.time() - self._stream_start_time
        ticker = self._current_ticker

        # Update the header line to show duration instead of "streaming..."
        self._patch_streaming_header(duration)

        # Mark the holding as analyzed
        self.mgr.mark_analyzed(ticker)
        self.mgr.save()

        # Append to persistent signals log
        self.signals_log.append({
            'ticker': ticker,
            'path': self._current_path,
            'model': self._current_model,
            'response': full_response,
            'duration_sec': duration,
            'was_locked': self._current_is_locked,
            'manual_trigger': True,
        })

        # Phase 2C: parse a structured prediction out of the response
        # and log it for track-record purposes. Best-effort — if the AI
        # didn't follow the format, the prediction will have None fields
        # and won't get auto-tracked, but we still record it.
        if (self.predictions_log is not None and _DISCOVER_AVAILABLE
                and tm_discover is not None):
            try:
                quote = self.cache.quote(ticker)
                current_price = (quote or {}).get('price')
                pred = tm_discover.parse_prediction(
                    full_response, ticker, current_price=current_price)
                # Only log if we got a direction at minimum
                if pred.get('direction'):
                    pred['path'] = self._current_path
                    pred['source'] = ('holdings_locked'
                                       if self._current_is_locked
                                       else 'holdings')
                    pred['model'] = self._current_model  # v4.8.9
                    self.predictions_log.append(pred)
            except Exception:
                pass

        self._active_request = None

        # v4.10.9: log to activity feed so user sees scan completed
        # even if Holdings window content is buried under other windows.
        if self.log_callback:
            try:
                self.log_callback(
                    f"Check Now done for {ticker} ({duration:.0f}s).",
                    'green')
            except Exception:
                pass

        # Move to next in queue (or finish)
        if self._check_now_queue:
            self._set_status(
                f"Done {ticker}. Next: "
                f"{self._check_now_queue[0].get('ticker', '?')}...",
                THEME['green'])
            self.window.after(500, self._process_next_in_queue)
        else:
            self._set_status(f"Done. ({duration:.1f}s for {ticker})",
                              THEME['green'])
            self._render_holdings()  # update last_analyzed timestamp display

    def _show_error(self, message: str) -> None:
        if self.window is None:
            return
        self.signals_text.insert('current_response',
            f"\n[error: {message}]\n", 'error')
        # Patch header
        try:
            duration = time.time() - self._stream_start_time
            self._patch_streaming_header(duration, failed=True)
        except Exception:
            pass
        self._active_request = None
        # Continue queue regardless of error
        if self._check_now_queue:
            self.window.after(500, self._process_next_in_queue)
        else:
            self._set_status(f"Error: {message[:60]}", THEME['red'])

    def _patch_streaming_header(self, duration: float, failed: bool = False) -> None:
        """Replace 'streaming...' with the actual duration in the header line."""
        # Find line with 'streaming...' and replace
        try:
            txt = self.signals_text
            # Search recent lines (top of buffer) for 'streaming...'
            idx = txt.search('streaming...', '1.0', stopindex='10.end')
            if idx:
                line_start = idx.split('.')[0] + '.0'
                line_end = idx.split('.')[0] + '.end'
                line_text = txt.get(line_start, line_end)
                replacement = line_text.replace(
                    'streaming...',
                    f"{duration:.1f}s" + (" — failed" if failed else ""))
                txt.delete(line_start, line_end)
                txt.insert(line_start, replacement, 'meta')
        except Exception:
            pass

    def _set_status(self, text: str, color: str = None) -> None:
        if self._status_lbl is None:
            return
        try:
            self._status_lbl.config(text=text,
                                    fg=color if color else THEME['muted'])
        except Exception:
            pass

    def _append_to_signals_top(self, text: str) -> None:
        if self.window is None:
            return
        self.signals_text.insert('1.0', f"{text}\n", 'hint')
