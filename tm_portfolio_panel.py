"""
tm_portfolio_panel — v4.11.0 unified portfolio panel.

Replaces the old "PORTFOLIO summary + open Holdings window" two-step UI.
Everything that used to live in HoldingsWindow now lives directly in the
main window's left column:

  PORTFOLIO summary (totals)
  Tradable holding cards (each with consensus + buttons)
  Locked holding cards (collapsed, no AI)
  Add Holding form

Public API:
    PortfolioPanel(parent, app)
        .render()                — (re)build the panel from current state
        .update_portfolio_data() — refresh after sells/adds/etc.

The panel reads from:
    - app.portfolio (the in-memory dict)
    - app._holdings_state['mgr'] (HoldingsManager — for sell/remove/add)
    - app._holdings_state['log'] (SignalsLog — to read cached AI consensus)
    - app.cfg (for consensus model list, freshness thresholds)

The panel triggers actions through:
    - app._log(msg, tag) for activity feed lines
    - app._holdings_state['mgr'].* for portfolio mutations
    - app._render_portfolio_summary() to rebuild itself after changes

v4.11.0 tracer-bullet scope:
    - Layout, freshness dots, all buttons present and wired
    - Add Holding: working
    - Remove: working
    - Sell: working with quick-confirm dialog
    - Run consensus: button works, but pops a "not yet" message; the AI
      pipeline gets ported in a follow-up patch once the layout is approved.
"""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import messagebox
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# v4.14.3.14 (2026-05-15): use the canonical DEFAULT_PATH constant
# for cfg fallbacks instead of literal strings. Aligns with other
# call sites that read cfg.get('analysis_path', ...). The fallback
# only fires if cfg is corrupted/missing the field; canonical
# load_config seeds it to 'moderate'.
import tm_holdings as _tm_holdings_for_default


# Freshness thresholds (in hours)
FRESH_HOURS = 24       # green: < 24h since last consensus
STALE_HOURS = 24 * 7   # yellow: 1-7 days
# beyond STALE_HOURS = red
# never scanned = gray


def _parse_iso(ts: str) -> Optional[datetime]:
    """Lenient ISO-8601 parse. Returns None on any failure."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def _freshness_state(last_scan_ts: Optional[str]) -> str:
    """Return one of: 'fresh', 'stale', 'old', 'never'."""
    if not last_scan_ts:
        return 'never'
    dt = _parse_iso(last_scan_ts)
    if dt is None:
        return 'never'
    age = datetime.now() - dt
    hours = age.total_seconds() / 3600.0
    if hours < FRESH_HOURS:
        return 'fresh'
    if hours < STALE_HOURS:
        return 'stale'
    return 'old'


def _humanize_age(last_scan_ts: Optional[str]) -> str:
    """Turn a timestamp into 'never scanned' / '12 min ago' / '3 days ago'."""
    if not last_scan_ts:
        return "never scanned"
    dt = _parse_iso(last_scan_ts)
    if dt is None:
        return "scan time unknown"
    age = datetime.now() - dt
    secs = age.total_seconds()
    if secs < 60:
        return "scanned just now"
    if secs < 3600:
        m = int(secs // 60)
        return f"scanned {m} min ago"
    if secs < 86400:
        h = int(secs // 3600)
        return f"scanned {h} hour{'s' if h != 1 else ''} ago"
    days = int(secs // 86400)
    return f"scanned {days} day{'s' if days != 1 else ''} ago"


def _format_money(amount: float) -> str:
    """Format dollars. Tiny amounts (< 0.01) get scientific notation."""
    if amount is None:
        return "—"
    a = abs(amount)
    if a < 0.01 and a > 0:
        return f"${amount:.6f}"
    if a < 1:
        return f"${amount:.4f}"
    return f"${amount:,.2f}"


def _format_shares(shares: float) -> str:
    """Format share count: 4 → '4', 1915 → '1,915', 420000 → '420,000'."""
    if shares is None:
        return "—"
    if shares == int(shares):
        return f"{int(shares):,}"
    return f"{shares:,.4f}".rstrip('0').rstrip('.')


def _read_latest_consensus(signals_log, ticker: str) -> Optional[dict]:
    """Read the most recent consensus result for a ticker from signals.jsonl.

    Looks for entries with kind='consensus' (the new v4.11 shape) OR falls
    back to the most recent regular signal entry (the v4.10 shape, single-
    model). Returns a dict with at least:
        {'ts': str, 'verdict': str, 'votes': [{'model','direction','range'}]}
    or None if no signal exists for this ticker.

    v4.14.2 stage 4 follow-up: owned-position consensus only.
    ConsensusRunner tags rollups with kind='consensus' for owned-position
    runs and kind='consensus_fresh_buy' for the Refresh-triggers
    fresh-buy thesis runs (which produce target/stop levels for the
    SELL TRIGGERS block, not the held-position recommendation). Both
    end up in signals.jsonl. Pre-fix this function returned whichever
    was newer, so a Refresh-triggers click on a held position would
    paint the consensus card with BUY/WATCH/AVOID vocabulary instead
    of the user-expected HOLD/SELL vocabulary. Now we skip
    consensus_fresh_buy entries here — they're persisted for
    SELL TRIGGERS consumption only.
    """
    if signals_log is None:
        return None
    try:
        entries = signals_log.read_recent(limit=200)
    except Exception:
        return None
    # Search newest-first for a matching ticker.
    for e in entries:
        if e.get('ticker', '').upper() != ticker.upper():
            continue
        # v4.14.2 stage 4 follow-up: skip fresh-buy rollups for the
        # consensus-card display path. The card is the owned-position
        # surface. Refresh-triggers writes (kind='consensus_fresh_buy')
        # land in signals.jsonl for SELL TRIGGERS data only and
        # shouldn't override the HOLD/SELL display vocabulary.
        if e.get('kind') == 'consensus_fresh_buy':
            continue
        # New v4.11 consensus shape — one log line per consensus run, has 'votes'
        if isinstance(e.get('votes'), list):
            return e
        # Legacy v4.10 single-model shape — promote to a 1-vote consensus
        return {
            'ts': e.get('ts'),
            'ticker': e.get('ticker'),
            'verdict': _direction_from_response(e.get('response', '')),
            'votes': [{
                'model': e.get('model', '?'),
                'direction': _direction_from_response(e.get('response', '')),
                'range': '',
            }],
            'legacy_single_model': True,
        }
    return None


def _direction_from_response(response: str) -> str:
    """Extract DIRECTION: line from a single-model response string.
    Returns '' if not found."""
    if not response:
        return ''
    for line in response.splitlines():
        line = line.strip()
        if line.upper().startswith('DIRECTION:'):
            return line.split(':', 1)[1].strip().upper().split()[0] if ':' in line else ''
    return ''


def _direction_family(d) -> str:
    """v4.14.6.23-outdated-fix: collapse direction strings into a family
    for like-vs-like agreement comparison in is_consensus_outdated.

    BUY-family — 'BUY', 'BUY MORE', 'BUYMORE' — all map to 'BUY'.
    Everything else (HOLD, TRIM, SELL, AVOID, NO_CALL, …) keeps its
    upper-case canonical string. Used by is_consensus_outdated to ask
    "did the prediction and consensus winner actually disagree?" — the
    previous hardcoded BUY-family literal at line 3243 treated EVERY
    non-BUY winner (including HOLD vs HOLD) as a thesis break.

    Mirrors tm_consensus._normalize_direction's canonicalization
    behavior for BUY MORE / BUYMORE without taking a cross-module
    import dependency.
    """
    u = (d or '').strip().upper()
    if u in ('BUY', 'BUY MORE', 'BUYMORE'):
        return 'BUY'
    return u


def _summarize_consensus(consensus: dict) -> tuple[str, str]:
    """Turn a consensus dict into (verdict_line, target_line) for the card.

    v4.13.34: verdict_line now shows the full breakdown of all
    directions plus no-calls, sorted by count desc. This makes
    the noise in a run visible at a glance instead of hiding
    no-calls in a parenthetical.

    Examples (v4.13.34 format):
        '3 HOLD · 1 BUY'                      -- clean 4-model run
        '3 AVOID · 2 no-call · 1 BUY'         -- noisy 6-model run
        'Models did not commit'                -- all no-calls
    """
    votes = consensus.get('votes', [])
    if not votes:
        return ("No models voted", "")
    # Separate committed votes (have a direction) from no-calls
    committed = [v for v in votes
                  if (v.get('direction') or '').strip()]
    no_calls = len(votes) - len(committed)
    if not committed:
        return ("Models did not commit", "")
    # Tally committed votes
    counts: dict = {}
    for v in committed:
        d = v['direction'].upper().strip()
        counts[d] = counts.get(d, 0) + 1
    # Sort by count desc, then alphabetically for stable ordering
    parts = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    pieces = [f"{n} {d}" for d, n in parts]
    # v4.14.6.111 (Item 8): surface timed-out models distinctly from genuine
    # no-calls so a model that missed the finalize deadline is VISIBLE in the
    # holdings verdict line (it folds into no_calls otherwise). Display-only.
    timed_out = sum(1 for v in votes if v.get('timed_out'))
    other_no_calls = no_calls - timed_out
    if other_no_calls > 0:
        pieces.append(f"{other_no_calls} no-call")
    if timed_out > 0:
        pieces.append(f"{timed_out} timed out")
    verdict_line = " · ".join(pieces)
    # Target/stop line — pick the consensus.verdict_target if present, else blank
    target_line = consensus.get('verdict_target', '') or ''
    return (verdict_line, target_line)


# ── v4.14.5.14-ai-verdict-indicator ──────────────────────────────────
# Header hint that the AI no longer recommends BUY on a position you still
# hold. Distinct from the state badges (which describe the prediction's
# status): this describes the AI's CURRENT verdict at the current price.

# Verdicts meaning "the AI still wants you in the position" -> no indicator.
_AI_VERDICT_STILL_BUY = {'BUY', 'BUY MORE', 'BUYMORE'}


def _predominant_verdict(consensus: Optional[dict]) -> Optional[str]:
    """Return the single predominant COMMITTED verdict from a consensus dict
    (the direction with the most votes; ties broken alphabetically — same
    tally rule as _summarize_consensus / the Sell-Triggers picker). Returns
    None when there's no consensus or no model committed a direction."""
    if not consensus:
        return None
    counts: dict = {}
    for v in (consensus.get('votes') or []):
        d = (v.get('direction') or '').upper().strip()
        if d:
            counts[d] = counts.get(d, 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _ai_verdict_indicator(verdict: Optional[str]) -> Optional[tuple]:
    """Map a predominant verdict to (label_text, color_key) for the header
    indicator, or None when no indicator should show. BUY / BUY MORE (AI
    still likes it) and None (no verdict) -> None. HOLD / WATCH -> amber;
    SELL / AVOID / TRIM -> red; any other committed non-buy -> amber."""
    if not verdict:
        return None
    v = verdict.upper().strip()
    if v in _AI_VERDICT_STILL_BUY:
        return None
    if v in ('SELL', 'AVOID', 'TRIM'):
        return (f"AI: {v}", 'red')
    return (f"AI: {v}", 'amber')


def _find_original_buy_prediction(predictions_log,
                                    ticker: str) -> Optional[dict]:
    """v4.13.19: Find the most recent BUY prediction for this ticker.
    This is the trade plan the user committed to when buying.

    Walks predictions newest-first, returns the first BUY entry with
    valid target+stop. Returns None if not found.
    """
    if predictions_log is None or not ticker:
        return None
    # v4.14.6.35-fix-portfolio-paint: surfacing the MOST RECENT BUY
    # for a held ticker is a working-set question, not an all-time
    # one — the latest BUY is always near the tail of insertion
    # order. The v4.14.6.34 audit miscategorised this as needing
    # full history; the resulting get_all_full(timeout=30.0) call
    # blocked the main thread for up to 30s during _build because
    # the portfolio panel paints synchronously while the predictions
    # tail-load is still in progress. Reverting to get_all() reads
    # the working set (last 2000 + all open) which always contains
    # the most recent BUY for any held position the user can see.
    try:
        all_preds = predictions_log.get_all()
    except Exception:
        return None
    ticker_u = ticker.upper()
    # Newest-first scan
    candidates = [
        p for p in all_preds
        if (p.get('ticker') or '').upper() == ticker_u
        and (p.get('direction') or '').upper() == 'BUY'
        and p.get('target') is not None
        and p.get('stop') is not None
    ]
    if not candidates:
        return None
    # Sort by timestamp newest-first
    def _ts(p):
        return p.get('timestamp', '')
    candidates.sort(key=_ts, reverse=True)
    return candidates[0]


def _find_buy_prediction_for_triggers(predictions_log,
                                        ticker: str) -> Optional[dict]:
    """v4.13.33: Find the prediction that should drive the SELL
    TRIGGERS block.

    Prefers a prediction that has ALL THREE fields (target, stop,
    timeframe_days) so target/stop and the deadline always come from
    the same source. Falls back to the most recent prediction with
    target+stop if none has all three (timeframe gets shown as "not
    specified").

    v4.15.0: directions widened beyond just 'BUY'. After the May 2026
    refresh-triggers fix, refresh-triggers writes owned-position
    predictions whose direction is BUY MORE / HOLD / TRIM (not BUY).
    All of these can drive sell triggers — they each include a target
    (price to watch / trim at) and stop (thesis-broken level). Only
    SELL and AVOID are excluded — SELL means exit now (no trigger
    levels apply), AVOID is the fresh-buy "don't enter" verdict which
    isn't actionable for an existing holder. BUY is preserved for
    legacy fresh_buy results so holdings refreshed before the fix
    continue to render.

    Walks newest-first within each tier so we never trade freshness
    for completeness across a big gap. The walk-back is bounded to the
    last 30 candidates to avoid surfacing stale predictions just
    because they happened to include a timeframe.
    """
    if predictions_log is None or not ticker:
        return None
    # v4.14.6.35-fix-portfolio-paint: same revert as
    # _find_buy_prediction_for_levels above. Sell triggers for a held
    # ticker come from the most-recent trigger-bearing prediction;
    # the working set always carries that record.
    try:
        all_preds = predictions_log.get_all()
    except Exception:
        return None
    ticker_u = ticker.upper()
    # v4.15.0: accept any "trigger-bearing" direction. Excludes SELL
    # (exit immediately, no levels) and AVOID (fresh-buy refusal,
    # not relevant to a holder).
    trigger_directions = {'BUY', 'BUY MORE', 'BUYMORE', 'HOLD', 'TRIM'}
    candidates = [
        p for p in all_preds
        if (p.get('ticker') or '').upper() == ticker_u
        and (p.get('direction') or '').upper() in trigger_directions
        and p.get('target') is not None
        and p.get('stop') is not None
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.get('timestamp', ''), reverse=True)
    # Prefer most recent within last 30 that has a timeframe.
    recent = candidates[:30]
    for p in recent:
        if p.get('timeframe_days') is not None:
            return p
    # Fallback: most recent with target+stop (no timeframe constraint)
    return candidates[0]


def _format_money_signed(amount: float) -> str:
    """Format a money amount with explicit sign. Used for +/- gain/loss display."""
    sign = '+' if amount >= 0 else ''
    if abs(amount) < 1:
        return f"{sign}${amount:.2f}"
    return f"{sign}${amount:.2f}"


# ═══════════════════════════════════════════════════════════════════════
# The panel
# ═══════════════════════════════════════════════════════════════════════

class PortfolioPanel:
    """Owns the left-column UI of the v4.11 main window."""

    # v4.14.2 stage 4 follow-up: collapse threshold for holding cards.
    # Portfolios with <= this many tradable holdings default to all
    # cards expanded; >= this+1 collapses by default. Picked at 3
    # because 1-3 cards comfortably fit on a typical screen; 4+
    # starts overflowing and benefits from compact mode.
    _CARD_EXPAND_DEFAULT_THRESHOLD = 3

    def __init__(self, parent: tk.Misc, app: Any):
        self.parent = parent
        self.app = app
        self.c = app.c
        self.fonts = app.fonts
        self.space = app.space
        # v4.14.5.14-density-pass-v3: tighter fonts/spacing for the tradable
        # holding card ONLY. Design target = 1366×768 laptop at 125% Windows
        # scaling (a 7pt source font renders ~9pt-equivalent there). Font floor
        # stays 7pt; v3's main lever is SPACING (md 9→7, sm 6→4, xs 3→2). v3
        # also trims ticker 11→10, dot 9→8, and the SELL TRIGGERS values 8→7.
        # Scoped copies so the shared app.fonts/app.space (header, add-form,
        # written-off cards, activity panel) are untouched. EXCEPTION: the
        # line-2 state badge (OUTDATED/DATED/TARGET/STOP) AND the chevron are
        # kept at 8pt in _build_tradable_card — the badge one step larger than
        # its 7pt neighbours so the glance-signal pops even more in v3.
        self._card_fonts = {
            'caption':   ('Segoe UI', 7),
            'body':      ('Segoe UI', 7),
            'body_bold': ('Segoe UI', 7, 'bold'),
            'mono':      ('Consolas', 7),
            'mono_bold': ('Consolas', 7, 'bold'),
        }
        self._card_space = {'xs': 2, 'sm': 4, 'md': 7, 'lg': 14, 'xl': 22}
        # Form input variables — survive across rebuilds via instance attrs
        self._form_ticker = tk.StringVar()
        self._form_shares = tk.StringVar()
        self._form_price = tk.StringVar()
        self._form_tradable = tk.BooleanVar(value=True)
        # v4.13.2: per-holding path. Empty string = use global default.
        self._form_path = tk.StringVar(value='')
        # v4.14.2 stage 4 follow-up: per-ticker card expand state.
        # Survives across re-renders (Run consensus, Refresh triggers,
        # add/remove holding) so the user's collapse choices stick.
        # Per-session only — not persisted to disk; resets on app
        # restart, which is fine since the smart default kicks in
        # again on the new session.
        self._card_expanded: dict[str, bool] = {}
        # v4.14.5.14-portfolio-narration-and-honesty:
        #  _refresh_failure[ticker] = friendly message when the last
        #    Refresh on that ticker came back with ZERO usable votes
        #    (Fix A — shown red on the card instead of a fake green
        #    "complete"). Cleared when a new Refresh starts / succeeds.
        #  _refresh_narration = the live self-fix narration line for the
        #    single in-flight Refresh (Fix B; only one runs at a time).
        self._refresh_failure: dict[str, str] = {}
        self._refresh_narration: str = ''
        # v4.14.5.14-portfolio-watching-timing-fix (Fix C): the active-
        # watching badge reflects scheduler state only at render time, and the
        # background scheduler starts a beat AFTER the app launches. A
        # Portfolio panel opened during startup can therefore paint "OFF" and
        # (pre-fix) never self-correct, because the panel only re-rendered on
        # a cloud scan. After a render that shows OFF we poll the reason a few
        # times and re-render ONCE if/when it flips to ON, then stop. Bounded
        # by _BADGE_RECHECK_MAX so it can never loop forever; the attempt
        # count resets whenever the badge renders ON (so a later genuine OFF
        # can re-poll). Fix A is the primary safety net (it refreshes the
        # panel the instant the scheduler starts); this is defence-in-depth
        # for "panel opened just before the scheduler came up".
        self._watching_badge_off: bool = False
        self._badge_recheck_attempts: int = 0
        self._BADGE_RECHECK_MAX = 10  # 10 × 3s ≈ 30s — covers worst-case defer
        # v4.14.6.48-portfolio-price-sync: in-place price refresh state.
        # _price_labels[TICKER] = {'pos_lbl', 'pnl_lbl', 'buy_price',
        #   'shares', 'bg', 'green', 'red'} — populated by
        # _build_tradable_card. _holdings_total_lbl is the summary card's
        # "$NNN in holdings" label, populated by _build_summary_card.
        # Both are cleared at the top of render() and repopulated by the
        # rebuild. refresh_card_prices() walks _price_labels and
        # text-only-updates each card on the 15s ticker tick without
        # tearing down widgets (so scroll position is preserved).
        self._price_labels: dict[str, dict] = {}
        self._holdings_total_lbl = None

    # ─── Public API ────────────────────────────────────────────────

    def render(self) -> None:
        """(Re)build the entire panel from current state. Idempotent."""
        # Tear down children
        for child in list(self.parent.winfo_children()):
            try:
                child.destroy()
            except Exception:
                pass

        # v4.14.6.48-portfolio-price-sync: clear stashed price-label refs;
        # the rebuild below will repopulate _price_labels and
        # _holdings_total_lbl. The label widgets we held refs to are now
        # destroyed (above), so drop them so refresh_card_prices() won't
        # try to .config() them between renders.
        self._price_labels = {}
        self._holdings_total_lbl = None

        # Layout strategy:
        #   parent
        #   ├── footer (Add Holding form, packed bottom — fixed)
        #   └── canvas + scrollbar (everything else, packed top — scrolls)
        #
        # The footer gets packed FIRST with side='bottom' so it claims its
        # space; then the scrolling region takes whatever's left. This way
        # the form is always visible no matter how much you scroll.
        self._build_pinned_footer()
        self._build_scrollable_body()

        # Source of truth is the HoldingsManager — it owns portfolio.json
        # and writes on every change. Reading from it here means the panel
        # always reflects current state, including after sells.
        mgr = self._mgr()
        portfolio = (mgr.data if mgr is not None
                     else (getattr(self.app, 'portfolio', {}) or {}))
        # Keep app.portfolio in sync for any other parts of the app that
        # still read from it (the activity feed wiring, etc.).
        try:
            self.app.portfolio = portfolio
        except Exception:
            pass
        holdings = portfolio.get('holdings', [])

        # 1. Summary card at top
        self._build_summary_card(self.body, portfolio)

        # 2. Tradable / locked / written-off, in that order
        # v4.13.2: split into three buckets by status.
        tradable = [h for h in holdings
                    if h.get('status', 'tradable') == 'tradable']
        locked = [h for h in holdings
                  if h.get('status') == 'locked']
        written_off = [h for h in holdings
                       if h.get('status') == 'written_off']

        if not tradable and not locked and not written_off:
            self._build_empty_state(self.body)
        else:
            for h in tradable:
                self._build_tradable_card(self.body, h)
            # Locked section
            if locked:
                tk.Frame(self.body, bg=self.c['bg'], height=8
                         ).pack(fill='x', pady=(self.space['md'], 0))
                tk.Label(self.body, text="LOCKED POSITIONS",
                         bg=self.c['bg'], fg=self.c['dim'],
                         font=('Segoe UI', 8, 'bold'),
                         anchor='w'
                         ).pack(fill='x', padx=self.space['md'])
                for h in locked:
                    self._build_locked_card(self.body, h)
            # Written-off section (v4.13.2)
            if written_off:
                tk.Frame(self.body, bg=self.c['bg'], height=8
                         ).pack(fill='x', pady=(self.space['md'], 0))
                tk.Label(self.body, text="💀 WRITTEN OFF",
                         bg=self.c['bg'], fg=self.c['dim'],
                         font=('Segoe UI', 8, 'bold'),
                         anchor='w'
                         ).pack(fill='x', padx=self.space['md'])
                for h in written_off:
                    self._build_locked_card(self.body, h)

        # Bottom padding so the last card doesn't sit flush against the
        # footer.
        tk.Frame(self.body, bg=self.c['bg'], height=self.space['md']).pack(
            fill='x')

        # v4.14.5.14-portfolio-watching-timing-fix (Fix C): if this render
        # painted the badge OFF, schedule a bounded recheck so an early
        # startup OFF self-corrects once the scheduler finishes starting —
        # without needing a cloud scan to trigger a re-render. Stops the
        # instant it flips ON or the attempt budget runs out.
        try:
            if (self._watching_badge_off
                    and self._badge_recheck_attempts < self._BADGE_RECHECK_MAX):
                self.app.root.after(3000, self._timed_badge_recheck)
        except Exception:
            pass

    def _timed_badge_recheck(self) -> None:
        """v4.14.5.14-portfolio-watching-timing-fix (Fix C): re-evaluate the
        active-watching reason after a render that showed OFF. Re-renders ONCE
        if it has flipped (typically OFF→ON once the background scheduler is
        up), otherwise re-arms up to _BADGE_RECHECK_MAX times then stops.
        winfo_exists-guarded (never touches a destroyed panel) and bounded
        (never loops forever). render() only happens on an actual state
        change, so this is a cheap predicate poll between checks — not a
        repeated full rebuild."""
        try:
            if not self.parent.winfo_exists():
                return
        except Exception:
            return
        self._badge_recheck_attempts += 1
        try:
            _fn = getattr(self.app, '_portfolio_watching_inactive_reason', None)
            _reason = _fn() if callable(_fn) else None
        except Exception:
            _reason = None
        _now_off = bool(_reason)
        if _now_off != self._watching_badge_off:
            # State changed (usually OFF→ON now that the scheduler is alive).
            # A full render redraws the badge correctly and updates
            # _watching_badge_off, which stops the recheck loop.
            try:
                self.render()
            except Exception:
                pass
            return
        # Still OFF — re-arm until the attempt budget is exhausted.
        if _now_off and self._badge_recheck_attempts < self._BADGE_RECHECK_MAX:
            try:
                self.app.root.after(3000, self._timed_badge_recheck)
            except Exception:
                pass

    def update_portfolio_data(self) -> None:
        """Convenience alias so callers reading older code still work."""
        self.render()

    # ─── Building blocks ───────────────────────────────────────────

    def _build_scrollable_body(self) -> None:
        """Wrap the panel in a vertical scroll region so many holdings fit.
        Packed AFTER the footer (which uses side='bottom') so the scroll
        region takes whatever vertical space is left."""
        c = self.c
        canvas = tk.Canvas(self.parent, bg=c['bg'], highlightthickness=0,
                            borderwidth=0)
        sb = tk.Scrollbar(self.parent, orient='vertical',
                           command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

        self.body = tk.Frame(canvas, bg=c['bg'])
        body_window = canvas.create_window((0, 0), window=self.body,
                                            anchor='nw')

        def _on_body_resize(_event):
            canvas.configure(scrollregion=canvas.bbox('all'))

        def _on_canvas_resize(event):
            canvas.itemconfig(body_window, width=event.width)

        self.body.bind('<Configure>', _on_body_resize)
        canvas.bind('<Configure>', _on_canvas_resize)

        # Mouse wheel scroll — bound to the canvas, not bind_all, so it
        # doesn't hijack scroll events in the activity log on the right.
        def _on_mousewheel(event):
            try:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')
            except Exception:
                pass
        canvas.bind('<Enter>',
                     lambda _e: canvas.bind_all('<MouseWheel>', _on_mousewheel))
        canvas.bind('<Leave>',
                     lambda _e: canvas.unbind_all('<MouseWheel>'))
        # Save references for cleanup if panel is rebuilt
        self._canvas = canvas

    def _build_pinned_footer(self) -> None:
        """The Add Holding form lives here — packed at the bottom of the
        column, OUTSIDE the scroll region, so it's always visible."""
        c = self.c
        s = self.space
        footer = tk.Frame(self.parent, bg=c['bg'])
        footer.pack(side='bottom', fill='x')
        # A thin top divider so the form doesn't visually merge with
        # whatever's above it.
        tk.Frame(footer, bg=c['border'], height=1).pack(fill='x',
                                                         padx=s['md'])
        self._build_add_form(footer)

    def _build_summary_card(self, parent, portfolio):
        c = self.c
        s = self.space
        # v4.14.5.14-portfolio-header-density: explicit tightened padding +
        # fonts so the header is proportional to the v1-v3 compacted cards
        # below it (it previously looked oversized). Local literals, NOT the
        # shared self.space/self.fonts — the global scale stays untouched.
        card = tk.Frame(parent, bg=c['card2'], padx=s['md'], pady=8)
        card.pack(fill='x', padx=s['md'], pady=(8, 6))

        # v4.13.35: header row with PORTFOLIO label + Set cash button
        hdr_row = tk.Frame(card, bg=c['card2'])
        hdr_row.pack(fill='x')
        tk.Label(hdr_row, text="PORTFOLIO", bg=c['card2'], fg=c['accent'],
                 font=('Segoe UI', 10, 'bold'), anchor='w'
                 ).pack(side='left')
        try:
            tk.Button(hdr_row, text="Set cash",
                      command=self._on_set_cash,
                      bg=c['card2'], fg=c.get('text', '#fff'),
                      relief='flat', borderwidth=0, cursor='hand2',
                      padx=8, pady=2,
                      font=('Segoe UI', 8, 'bold'),
                      activebackground=c['accent'],
                      activeforeground=c['bg'],
                      highlightbackground=c['border'],
                      highlightthickness=1
                      ).pack(side='right')
        except Exception:
            pass

        # v4.14.5.14-portfolio-watching-honesty: if automatic owned-
        # position watching is OFF (cloud-only on-event path unwired, or
        # event-driven refresh off), say so under the header instead of
        # silently looking like a live surface. The manual 'Refresh
        # triggers' button on each holding still works.
        try:
            _show_ind = True
            try:
                _show_ind = bool(getattr(self.app, 'cfg', {}).get(
                    'show_portfolio_watching_indicator', True))
            except Exception:
                _show_ind = True
            _reason_fn = getattr(
                self.app, '_portfolio_watching_inactive_reason', None)
            _reason = _reason_fn() if callable(_reason_fn) else None
            # v4.14.5.14-portfolio-watching-timing-fix (Fix C): record whether
            # this render painted OFF so render() can decide to re-poll. A
            # render that lands ON resets the attempt budget so a future OFF
            # (e.g. user disables the scheduler mid-session) re-polls cleanly.
            self._watching_badge_off = bool(_show_ind and _reason)
            if not self._watching_badge_off:
                self._badge_recheck_attempts = 0
            if _show_ind and _reason:
                # v4.14.5.14-portfolio-watching-badge-concise: the badge is
                # ONE tight line so it can't wrap into a cramped multi-line
                # block that reads as broken (the prior full-sentence badge
                # wrapped to ~4 lines at 8pt). The full explanation — what
                # works now + the exact cfg key to flip — lives in the hover
                # tooltip (verbatim _reason) and the startup activity-log
                # line (both already correct). Single source of detail:
                # _reason. Badge = glance cue; tooltip/log = the detail.
                _badge_lbl = tk.Label(
                    card,
                    text="⚠ Active-watching OFF — manual "
                         "'Refresh triggers' only (hover for detail)",
                    bg=c['card2'], fg=c.get('amber', '#cc8800'),
                    font=('Segoe UI', 7), anchor='w', justify='left')
                _badge_lbl.pack(fill='x', pady=(1, 0))
                try:
                    from tired_market import Tooltip
                    Tooltip(_badge_lbl, _reason)
                except Exception:
                    pass
            elif _show_ind and callable(_reason_fn) and _reason is None:
                # v4.14.5.14-portfolio-watching-visibility (Fix B): positive
                # confirmation. Pre-patch, when actively watching the badge
                # was simply ABSENT — the user had no signal it was on. Now
                # show a green "Active-watching ON" line so the feature is
                # visibly alive (paired with the card's live-updating
                # scan-age from Fix A). Only when the helper exists AND
                # returned None (genuinely watching) — never as a default.
                _on_lbl = tk.Label(
                    card,
                    text="✓ Active-watching ON — auto-checking holdings on "
                         "price / news / earnings events",
                    bg=c['card2'], fg=c.get('green', '#3a3'),
                    font=('Segoe UI', 7), anchor='w', justify='left')
                _on_lbl.pack(fill='x', pady=(1, 0))
        except Exception:
            pass

        holdings = portfolio.get('holdings', [])
        closed = portfolio.get('closed', [])
        # v4.13.2: separate written_off from active capital math.
        active_holdings = [h for h in holdings
                           if h.get('status', 'tradable') != 'written_off']
        written_off_holdings = [h for h in holdings
                                if h.get('status') == 'written_off']
        active_cost = sum(h.get('total_cost', 0) for h in active_holdings)
        written_off_cost = sum(h.get('total_cost', 0) for h in written_off_holdings)
        n_tradable = sum(1 for h in active_holdings
                          if h.get('status', 'tradable') == 'tradable')
        n_locked = sum(1 for h in active_holdings
                        if h.get('status') == 'locked')
        n_written = len(written_off_holdings)
        realized = sum(t.get('pnl_dollars', 0) for t in closed)

        # v4.13.35: read cash from portfolio. Display total = cash +
        # active holdings cost (cost basis, not market value -- market
        # value would require live quotes which the summary card
        # doesn't have access to).
        cash = float(portfolio.get('cash', 0) or 0)
        total = cash + active_cost

        big_line = f"{_format_money(total)} total"
        big_lbl = tk.Label(card, text=big_line, bg=c['card2'], fg=c['text'],
                           font=('Segoe UI', 14, 'bold'), anchor='w')
        big_lbl.pack(fill='x', pady=(1, 2))
        # v4.13.64: clickable total → opens Trade Log (replaces the
        # toolbar button removed in v4.13.64)
        self._wire_trade_log_click(big_lbl, "Click to view trade history")

        # Breakdown line: cash + holdings
        # v4.14.6.48-portfolio-price-sync: stash this label and the cash
        # value so refresh_card_prices() can swap the "in holdings" total
        # to the live MARKET value (sum of price*shares across tradables)
        # every 15s without tearing down the summary card. At render time
        # we still show the cost-basis figure (no live quotes guaranteed
        # yet); the refresh upgrades it as soon as a quote lands. Cash
        # value is captured so the format stays "{cash} cash  ·  {N} in
        # holdings".
        breakdown = (f"{_format_money(cash)} cash  ·  "
                     f"{_format_money(active_cost)} in holdings")
        _breakdown_lbl = tk.Label(card, text=breakdown, bg=c['card2'],
                                  fg=c['muted'], font=('Segoe UI', 8),
                                  anchor='w')
        _breakdown_lbl.pack(fill='x', pady=(0, 1))
        try:
            self._holdings_total_lbl = _breakdown_lbl
            self._holdings_total_cash = cash
            self._holdings_total_cost_fallback = active_cost
        except Exception:
            pass

        bits = []
        if n_tradable:
            bits.append(f"{n_tradable} tradable")
        if n_locked:
            bits.append(f"{n_locked} locked")
        if n_written:
            bits.append(f"{n_written} written off")
        if closed:
            sign = '+' if realized >= 0 else ''
            bits.append(f"realized {sign}{_format_money(realized)}")
        if bits:
            tk.Label(card, text="  ·  ".join(bits), bg=c['card2'],
                     fg=c['muted'], font=('Segoe UI', 8), anchor='w'
                     ).pack(fill='x')

        # v4.13.2: written-off summary line
        if written_off_cost > 0:
            tickers_w = ", ".join(h.get('ticker', '?')
                                    for h in written_off_holdings)
            wo_line = (f"💀 written off: {_format_money(written_off_cost)} "
                       f"in {tickers_w}")
            tk.Label(card, text=wo_line, bg=c['card2'],
                     fg=c['dim'], font=('Segoe UI', 7), anchor='w'
                     ).pack(fill='x', pady=(1, 0))

    def _wire_trade_log_click(self, lbl, tooltip_text):
        """v4.13.64: Make a label clickable -> opens Trade Log dialog.
        Adds pointer cursor, underline-on-hover, and a tooltip. Used
        on the PORTFOLIO card's dollar total, replacing the toolbar
        Trade Log button so the entry point sits next to the data.

        v4.13.64.1: Tooltip binds <Button-1>/<Enter>/<Leave> WITHOUT
        add='+', so it replaces any prior bindings. Attach Tooltip
        FIRST, then bind our handlers with add='+' so they layer on
        top instead of being stomped."""
        try:
            import tkinter.font as tkfont
            actual = tkfont.Font(font=lbl.cget('font')).actual()
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
            lbl.configure(font=f_normal)
        except Exception:
            f_normal = None
            f_hover = None
        lbl.configure(cursor='hand2')

        # v4.13.64.1: attach Tooltip FIRST (its binds don't use add='+')
        try:
            from tired_market import Tooltip
            Tooltip(lbl, tooltip_text)
        except Exception:
            pass

        def _enter(_e=None):
            if f_hover is not None:
                try: lbl.configure(font=f_hover)
                except Exception: pass
        def _leave(_e=None):
            if f_normal is not None:
                try: lbl.configure(font=f_normal)
                except Exception: pass
        def _click(_e=None):
            try:
                self.app._show_trade_history()
            except Exception as e:
                try: self.app._log(f"Trade Log open failed: {e}", 'red')
                except Exception: pass

        # Now bind with add='+' so Tooltip's binds + ours both fire
        lbl.bind('<Enter>', _enter, add='+')
        lbl.bind('<Leave>', _leave, add='+')
        lbl.bind('<Button-1>', _click, add='+')

    def _handle_save_failure(self, action, context=None):
        """v4.14.5.14-portfolio-atomic-save: a HoldingsManager.save()
        returned False — the atomic write failed (disk full / file locked /
        permissions). The on-disk portfolio.json is INTACT (the atomic save
        never overwrote it), so: (1) roll the in-memory state back to disk so
        the UI can't keep showing the un-persisted change, (2) warn the user
        with an unmissable modal, (3) re-render. Never raises."""
        try:
            self.app._log(
                f"[portfolio] save FAILED during {action} — change NOT "
                f"persisted; rolling back to the on-disk copy.", 'red')
        except Exception:
            pass
        # Strategy B rollback: reload from disk (atomic save left it intact).
        try:
            mgr = self._mgr()
            if mgr is not None and hasattr(mgr, 'reload'):
                mgr.reload()
        except Exception as _re:
            try:
                self.app._log(
                    f"[portfolio] rollback reload also failed: {_re}", 'red')
            except Exception:
                pass
        # Playbook-driven message (fall back to a plain, honest one).
        _data_path = ''
        try:
            _data_path = str(getattr(self._mgr(), 'portfolio_path', '') or '')
        except Exception:
            _data_path = ''
        msg = None
        try:
            msg = self.app._get_playbook_message(
                'portfolio_save_failed', action=action,
                data_path=_data_path)
        except Exception:
            msg = None
        if not msg:
            msg = (f"Couldn't save your {action} — your portfolio on disk "
                   f"is unchanged. Check that you have free disk space and "
                   f"that no other program has the portfolio file open, "
                   f"then try again.")
        try:
            messagebox.showerror("Save Failed", msg)
        except Exception:
            try:
                self.app._log(f"[portfolio] {msg}", 'red')
            except Exception:
                pass
        try:
            self.render()
        except Exception:
            pass

    def _on_set_cash(self):
        """v4.13.35: prompt for cash amount and save it."""
        try:
            from tkinter import simpledialog
            mgr = self._mgr()
            if mgr is None:
                return
            current = 0.0
            try:
                current = float(mgr.get_cash())
            except Exception:
                pass
            new_val = simpledialog.askfloat(
                "Set cash",
                "Cash on hand (in your trading account):",
                initialvalue=current,
                minvalue=0.0,
                parent=self.parent.winfo_toplevel())
            if new_val is None:
                return  # user cancelled
            # v4.14.5.14-portfolio-atomic-save: set_cash now returns the
            # save() success bool — surface a failure instead of silently
            # "succeeding".
            if not mgr.set_cash(float(new_val)):
                self._handle_save_failure('cash update')
                return
            self.app._log(
                f"Cash set to ${new_val:.2f}", 'green')
            self.render()
        except Exception as e:
            try:
                self.app._log(
                    f"Set cash failed: {e}", 'red')
            except Exception:
                pass

    def _build_empty_state(self, parent):
        c = self.c
        s = self.space
        empty = tk.Frame(parent, bg=c['card'], padx=s['lg'], pady=s['xl'])
        empty.pack(fill='x', padx=s['md'], pady=s['sm'])
        tk.Label(empty,
                 text="No holdings yet.\nAdd one below to get started.",
                 bg=c['card'], fg=c['dim'],
                 font=('Segoe UI', 9, 'italic'), justify='center'
                 ).pack()

    # ─── Card collapse/expand helpers ──────────────────────────────
    #
    # v4.14.2 stage 4 follow-up: holding cards grew tall enough
    # (consensus rollup + plan/triggers + action buttons) that 4+
    # holdings are hard to read. Each card's body now collapses
    # behind a chevron toggle in the header. State lives in
    # self._card_expanded[ticker] across re-renders.

    def _count_tradable_holdings(self) -> int:
        """How many tradable holdings does the portfolio have right
        now? Used by the smart-default expand state."""
        try:
            mgr = self._mgr()
            if mgr is None:
                return 0
            return sum(
                1 for h in (mgr.holdings or [])
                if h.get('status', 'tradable') == 'tradable')
        except Exception:
            return 0

    def _get_card_expanded(self, ticker: str) -> bool:
        """Return the current expand state for `ticker`'s card.

        v4.14.5.14-portfolio-collapsed-default (2026-05-26): cards now
        default to COLLAPSED on first encounter — the compact two-line
        collapsed view (header + state badge) is the at-a-glance scan
        surface, so the whole portfolio should start compressed. (Replaces
        the size-based smart default that started small portfolios
        expanded.) State is session-only: `self._card_expanded` is a plain
        instance dict, never persisted, so every app restart resets every
        card to collapsed; within a session a card stays however the user
        last toggled it.

        The `_count_tradable_holdings` / `_CARD_EXPAND_DEFAULT_THRESHOLD`
        machinery is retained (unused by the default now) in case a future
        size-aware default is wanted again."""
        key = (ticker or '').upper()
        if key in self._card_expanded:
            return self._card_expanded[key]
        default = False  # collapsed by default
        self._card_expanded[key] = default
        return default

    def _toggle_card_expand(self, ticker: str, body: tk.Frame,
                              chevron_label: tk.Label,
                              body_pack_kwargs: dict | None = None
                              ) -> None:
        """Flip expanded/collapsed for `ticker`'s card in place. The
        body Frame gets pack/pack_forget'd; chevron updates. No
        re-render needed — geometry manager picks up the change
        automatically."""
        key = (ticker or '').upper()
        new_state = not self._card_expanded.get(key, True)
        self._card_expanded[key] = new_state
        try:
            if new_state:
                body.pack(**(body_pack_kwargs
                              or {'fill': 'x',
                                  'pady': (self.space['xs'], 0)}))
                chevron_label.config(text='▼')
            else:
                body.pack_forget()
                chevron_label.config(text='▶')
        except Exception:
            pass

    def _bind_card_header_click(self, header: tk.Widget,
                                 toggle_callback,
                                 skip_widgets: tuple = ()
                                 ) -> None:
        """Recursively bind <Button-1> on the header Frame and its
        Label children (and the cursor='hand2' affordance) so
        clicking anywhere on the header — except interactive
        widgets passed via `skip_widgets` (e.g. the Remove button on
        locked cards) — toggles the card. Buttons inside the header
        keep their own click handlers; we just don't add a competing
        toggle binding to them."""
        skip_ids = {id(w) for w in skip_widgets}

        def _bind(widget):
            if id(widget) in skip_ids:
                return
            cls_name = widget.winfo_class()
            # tk.Button instances should keep their own command —
            # don't shadow with a toggle. Frame/Label/Canvas/etc.
            # are safe to bind.
            if cls_name == 'Button':
                return
            try:
                widget.bind('<Button-1>',
                             lambda _e: toggle_callback())
                widget.config(cursor='hand2')
            except Exception:
                pass
            for ch in widget.winfo_children():
                _bind(ch)

        _bind(header)

    # ─── Tradable card ─────────────────────────────────────────────

    def _compute_ai_current_verdict(self, ticker, consensus=None):
        """v4.14.5.14-ai-verdict-indicator: the AI's CURRENT predominant
        verdict for a held ticker (from the latest consensus), or None.

        Pass an already-read `consensus` to avoid a second signals read
        (the card builders already hold one). Current-price-relative by
        design — it reflects what the AI's analysis says NOW, independent
        of the user's cost basis (gain/loss is shown separately). Never
        raises."""
        try:
            if consensus is None:
                sl = self._signals_log()
                consensus = (_read_latest_consensus(sl, ticker)
                             if sl else None)
            return _predominant_verdict(consensus)
        except Exception:
            return None

    def _build_tradable_card(self, parent, holding):
        """The main, full-featured holding card with consensus + buttons.

        v4.14.2 stage 4 follow-up: split into a persistent header
        section (status dot + ticker + TRADABLE pill + scan age +
        position/P&L line + chevron) and a collapsible body section
        (consensus block + plan block + action buttons). Smart
        default expand state is determined by portfolio size — see
        _get_card_expanded. Header click toggles state.
        """
        c = self.c
        s = self._card_space
        ticker = holding.get('ticker', '?')
        shares = holding.get('shares', 0)
        buy_price = holding.get('buy_price', 0)
        total_cost = holding.get('total_cost', shares * buy_price)
        last_analyzed = holding.get('last_analyzed')

        card = tk.Frame(parent, bg=c['card'], padx=s['md'], pady=s['md'],
                         highlightbackground=c['border'],
                         highlightthickness=1)
        card.pack(fill='x', padx=s['md'], pady=s['sm'])

        # ── v4.14.5.64-owned-position-surfacing: watcher alert badge ──
        # Persistent red/amber alert pill, rendered ABOVE the existing
        # header so it can't be missed. Reads holding['_owned_alert']
        # which is written by tired_market._run_cloud_on_event_scan
        # after each owned-position consensus completes. Routine
        # results clear this badge, so a "still selling" alert that
        # the program later downgrades stops shouting on its own.
        # Click to acknowledge / clear. No badge if no alert.
        try:
            _alert = holding.get('_owned_alert') or None
            if _alert:
                import tm_owned_alert as _oa
                _btxt, _bcolor = _oa.badge_text(_alert)
                if _btxt:
                    alert_row = tk.Frame(card, bg=c['card'])
                    alert_row.pack(fill='x', pady=(0, s['xs']))
                    _fg = c.get(_bcolor or 'red', c['red'])
                    pill = tk.Label(
                        alert_row, text=_btxt + '  (click to clear)',
                        bg=c['card2'], fg=_fg,
                        font=('Segoe UI', 9, 'bold'),
                        padx=8, pady=2, cursor='hand2')
                    pill.pack(side='left')

                    def _ack(_e=None, t=ticker):
                        try:
                            mgr = self._mgr()
                            if mgr is not None:
                                mgr.clear_owned_alert(t)
                                try:
                                    mgr.save()
                                except Exception:
                                    pass
                            # Re-render to drop the pill.
                            try:
                                self.render()
                            except Exception:
                                pass
                        except Exception:
                            pass
                    pill.bind('<Button-1>', _ack)
        except Exception:
            pass

        # ── HEADER (always visible) — v4.14.5.14-compact-holding-row-fix-2line ──
        # Two PREDICTABLE lines. This revises the single-line compact row,
        # which clipped the state badge AND the chevron off the right edge on
        # narrow windows (pack-based layout, no wraplength → right content is
        # lost when the row overflows). Splitting fixes it:
        #   Line 1 — dot + ticker + TRADABLE pill + position/P&L (left);
        #            scan-age + chevron (right). The chevron is right-packed
        #            FIRST so it is ALWAYS the rightmost, guaranteed-visible
        #            element — the user can always collapse/expand even if the
        #            price detail clips. Right-packed widgets never clip; only
        #            the left position string does.
        #   Line 2 — the state badge (left only), OR omitted entirely when the
        #            holding is FRESH so healthy holdings waste no vertical
        #            space. Line 2's right side is intentionally empty, so the
        #            badge always has room to breathe on narrow windows.
        # The consensus breakdown still lives in the expanded CONSENSUS block;
        # the EXPANDED view is unchanged.
        header_section = tk.Frame(card, bg=c['card'])
        header_section.pack(fill='x')

        # ── Line 1: position info + chevron ──
        line1 = tk.Frame(header_section, bg=c['card'])
        line1.pack(fill='x')

        freshness = _freshness_state(last_analyzed)
        dot_color = {
            'fresh': c['green'],
            'stale': c['amber'],
            'old':   c['red'],
            'never': c['dim'],
        }[freshness]
        tk.Label(line1, text='●', bg=c['card'], fg=dot_color,
                 font=('Segoe UI', 8)).pack(side='left')

        tk.Label(line1, text=ticker, bg=c['card'], fg=c['accent'],
                 font=('Segoe UI', 10, 'bold')
                 ).pack(side='left', padx=(s['xs'], s['sm']))

        # TRADABLE pill — small, green
        tk.Label(line1, text='TRADABLE', bg=c['card2'], fg=c['green'],
                 font=('Segoe UI', 7, 'bold'), padx=6, pady=1
                 ).pack(side='left')

        # Position + P&L — the clip-tolerant left content (if line 1 overflows
        # a narrow window, this is what gets cut, NOT the chevron).
        # v4.14.6.48-portfolio-price-sync: stash both labels (pos + pnl) into
        # self._price_labels[ticker] so the 15s ticker tick can text-only-
        # update them via refresh_card_prices() without tearing down the
        # card (preserves scroll). In the no-current-price branch we still
        # create a placeholder pnl_lbl (empty text) so refresh_card_prices
        # can lazily upgrade the line to include "→ price" + P&L once a
        # quote arrives — without needing to create widgets at refresh time
        # (refresh stays text-only).
        current_price = self._latest_price(ticker)
        if current_price is not None and buy_price:
            pnl = (current_price - buy_price) * shares
            pnl_pct = ((current_price - buy_price) / buy_price) * 100 if buy_price else 0
            sign = '+' if pnl >= 0 else ''
            pnl_color = c['green'] if pnl >= 0 else c['red']
            pos_text = (f"{_format_shares(shares)} sh @ {_format_money(buy_price)} "
                        f"→ {_format_money(current_price)}")
            _pos_lbl = tk.Label(line1, text=pos_text, bg=c['card'], fg=c['muted'],
                                font=self._card_fonts['body'])
            _pos_lbl.pack(side='left', padx=(s['sm'], 0))
            _pnl_lbl = tk.Label(line1,
                                text=f"  {sign}{_format_money(pnl)} ({sign}{pnl_pct:.1f}%)",
                                bg=c['card'], fg=pnl_color,
                                font=self._card_fonts['body_bold'])
            _pnl_lbl.pack(side='left')
        else:
            _pos_lbl = tk.Label(line1,
                                text=(f"{_format_shares(shares)} sh @ "
                                       f"{_format_money(buy_price)} · "
                                       f"cost {_format_money(total_cost)}"),
                                bg=c['card'], fg=c['muted'],
                                font=self._card_fonts['body'])
            _pos_lbl.pack(side='left', padx=(s['sm'], 0))
            # Placeholder P&L label so refresh_card_prices can fill it in
            # later without creating widgets. Empty text → no visual cost
            # while the price is unknown.
            _pnl_lbl = tk.Label(line1, text='', bg=c['card'], fg=c['muted'],
                                font=self._card_fonts['body_bold'])
            _pnl_lbl.pack(side='left')
        try:
            self._price_labels[str(ticker).upper()] = {
                'pos_lbl': _pos_lbl,
                'pnl_lbl': _pnl_lbl,
                'buy_price': buy_price,
                'shares': shares,
                'bg': c['card'],
                'muted': c['muted'],
                'green': c['green'],
                'red': c['red'],
            }
        except Exception:
            pass

        # Chevron — right-packed FIRST so it stays the rightmost element and
        # can never be pushed off-screen by a clipped position string.
        is_expanded = self._get_card_expanded(ticker)
        chevron = tk.Label(line1,
                            text=('▼' if is_expanded else '▶'),
                            bg=c['card'], fg=c['muted'],
                            font=('Segoe UI', 8),
                            cursor='hand2')
        chevron.pack(side='right', padx=(s['sm'], 0))

        # Scan-age, right-packed left of the chevron (right-packed → never
        # clips, so it stays visible alongside the chevron).
        age_text = _humanize_age(last_analyzed)
        tk.Label(line1, text=age_text, bg=c['card'], fg=c['dim'],
                 font=self._card_fonts['caption']).pack(side='right')

        # Consensus is still read here (the BODY's consensus block + the
        # button row both consume it) — only its header summary LINE moved
        # to the expanded view.
        signals_log = self._signals_log()
        consensus = (_read_latest_consensus(signals_log, ticker)
                      if signals_log else None)

        # ── Line 2: the state badge (omitted entirely when FRESH) ──
        # Single at-a-glance state per holding (first matching rule wins;
        # FRESH → empty text → no line 2 at all → clean, short row).
        predictions_log = self._predictions_log()
        pred_for_state = None
        if predictions_log is not None:
            try:
                pred_for_state = _find_buy_prediction_for_triggers(
                    predictions_log, ticker)
            except Exception:
                pred_for_state = None
        try:
            _st_code, _st_text, _st_colorkey = self.compute_holding_state(
                holding, pred_for_state, ticker, datetime.now())
        except Exception:
            _st_code, _st_text, _st_colorkey = 'FRESH', '', None
        # v4.14.5.14-ai-verdict-indicator: a quiet "the AI changed its mind"
        # hint — present when the AI's CURRENT predominant verdict is no
        # longer BUY (HOLD amber, SELL/AVOID/TRIM red). It rides line 2
        # alongside the state badge, so it shows in BOTH collapsed and
        # expanded states. Reuses the consensus already read above.
        _ai_ind = _ai_verdict_indicator(
            self._compute_ai_current_verdict(ticker, consensus))
        if _st_text or _ai_ind:
            # TARGET/STOP hits are bold (urgent); OUTDATED / DATED render
            # non-bold so they read softer than a price hit. Left-packed on
            # their own line; line 2's right side stays empty so content
            # always has room to breathe on narrow windows.
            line2 = tk.Frame(header_section, bg=c['card'])
            line2.pack(fill='x', pady=(s['xs'], 0))
            if _st_text:
                _badge_bold = _st_code in ('TARGET_HIT', 'STOP_HIT')
                tk.Label(line2, text=_st_text, bg=c['card'],
                         fg=c.get(_st_colorkey, c['dim']),
                         font=('Segoe UI', 8,
                               'bold' if _badge_bold else 'normal')
                         ).pack(side='left')
            if _ai_ind:
                _ai_text, _ai_colorkey = _ai_ind
                tk.Label(line2, text=_ai_text, bg=c['card'],
                         fg=c.get(_ai_colorkey, c['amber']),
                         font=('Segoe UI', 8)
                         ).pack(side='left',
                                padx=(s['sm'] if _st_text else 0, 0))

        # ── BODY (toggles with chevron) ─────────────────────────────
        body = tk.Frame(card, bg=c['card'])
        # Pack body only if the card starts expanded.
        body_pack_kwargs = {'fill': 'x', 'pady': (s['xs'], 0)}
        if is_expanded:
            body.pack(**body_pack_kwargs)

        # Consensus block (per-model rollout)
        self._build_consensus_block(body, ticker, consensus, freshness)

        # Plan block (sell triggers) — v4.13.19
        try:
            self._build_plan_block(body, ticker, holding)
        except Exception:
            pass

        # Buttons row
        self._build_card_buttons(body, ticker, freshness,
                                  has_consensus=bool(consensus))

        # Wire the click-to-toggle on the header section. body and
        # chevron captured by closure so the toggle has everything
        # it needs to flip state in place.
        def _toggle(t=ticker, b=body, ch=chevron, kw=body_pack_kwargs):
            self._toggle_card_expand(t, b, ch, body_pack_kwargs=kw)

        self._bind_card_header_click(header_section, _toggle)
        # Chevron itself stays clickable (helpful affordance) — its
        # cursor='hand2' is already set, and it'll inherit the same
        # binding from _bind_card_header_click since it's a Label.

    def _extract_reason_snippet(self, vote, max_chars=110):
        """v4.13.34: Pull a one-line reasoning summary from a vote.

        Tries in order:
        1. Explicit 'reason_one_line' field (most parsers populate this)
        2. Line starting with 'REASON_ONE_LINE:' in the raw response
        3. Line starting with 'REASON:' in the raw response
        4. First non-empty content line from the response

        Truncates to max_chars and strips formatting noise.
        Returns empty string if nothing usable is found.
        """
        try:
            r = (vote.get('reason_one_line') or '').strip()
            if r:
                return self._clean_snippet(r, max_chars)
            response = vote.get('response') or ''
            if not response:
                return ''
            for line in response.splitlines():
                line = line.strip()
                if line.upper().startswith('REASON_ONE_LINE:'):
                    return self._clean_snippet(
                        line.split(':', 1)[1], max_chars)
            for line in response.splitlines():
                line = line.strip()
                if line.upper().startswith('REASON:'):
                    return self._clean_snippet(
                        line.split(':', 1)[1], max_chars)
            # Fall back to first non-empty content line that isn't a
            # structured field
            for line in response.splitlines():
                line = line.strip()
                if not line:
                    continue
                # Skip structured fields and markdown headers
                if (line.upper().startswith(
                        ('DIRECTION:', 'BUY_ZONE:', 'TARGET:',
                         'STOP_LOSS:', 'TIMEFRAME:', 'CONFIDENCE:'))
                        or line.startswith('#')
                        or line.startswith('*')
                        or line.startswith('-')):
                    continue
                return self._clean_snippet(line, max_chars)
        except Exception:
            pass
        return ''

    def _clean_snippet(self, text, max_chars):
        """Strip markdown, collapse whitespace, truncate to max_chars."""
        try:
            t = (text or '').strip()
            # Strip leading/trailing markdown emphasis
            t = t.strip('*_`').strip()
            # Collapse internal whitespace
            t = ' '.join(t.split())
            if len(t) > max_chars:
                t = t[:max_chars - 1].rstrip() + '…'
            return t
        except Exception:
            return ''

    def _build_consensus_block(self, parent, ticker, consensus, freshness):
        c = self.c
        s = self._card_space
        block = tk.Frame(parent, bg=c['card2'], padx=s['md'], pady=s['sm'])
        block.pack(fill='x', pady=(s['sm'], s['sm']))

        # If a consensus run is in progress for this ticker, show a live
        # status — list of model names with done/running/queued state.
        running = self._is_running_for(ticker)
        if running is not None:
            self._render_running_block(block, running)
            return

        # v4.14.5.14-portfolio-narration-and-honesty (Fix A): if the last
        # Refresh on this ticker came back with ZERO usable votes (every
        # provider busy / errored / network drop), show an HONEST red line
        # instead of letting the card fall through to a stale/green-looking
        # "complete". Mirrors lookup_all_voices_failed / verify_all_voices_
        # failed. Cleared when a new Refresh starts or succeeds.
        _fail_msg = self._refresh_failure.get((ticker or '').upper())
        if _fail_msg:
            tk.Label(block, text=_fail_msg,
                     bg=c['card2'], fg=c.get('red', '#c44'),
                     font=('Segoe UI', 7, 'bold'), anchor='w',
                     wraplength=520, justify='left'
                     ).pack(fill='x')
            return

        if consensus is None:
            tk.Label(block,
                     text="No consensus yet.  Click Run consensus below.",
                     bg=c['card2'], fg=c['dim'],
                     font=('Segoe UI', 7, 'italic'), anchor='w'
                     ).pack(fill='x')
            return

        # Header
        verdict_line, target_line = _summarize_consensus(consensus)
        hdr = tk.Frame(block, bg=c['card2'])
        hdr.pack(fill='x')
        tk.Label(hdr, text="CONSENSUS  ·  " + verdict_line,
                 bg=c['card2'], fg=c['muted'],
                 font=('Segoe UI', 7, 'bold'), anchor='w'
                 ).pack(side='left')

        # v4.14.0 stage 7.1 follow-up: canonicalize model labels at
        # display. Stage 6d's sweep covered View Reasoning + Consensus
        # Running inline display in this module but missed THIS render
        # site (the per-holding consensus card in the main window —
        # the one the user sees on FISV with raw "My Groq" / "qwen2.5:14b"
        # labels). Applied via the same lazy-import pattern as the
        # other render sites; signals.jsonl on disk stays untouched.
        try:
            import tm_api_providers as _tmap6d_block
            _norm_block = _tmap6d_block.canonicalize_model_label
        except Exception:
            _norm_block = lambda x: x

        # Vote rows
        votes = consensus.get('votes', [])
        for i, v in enumerate(votes):
            row = tk.Frame(block, bg=c['card2'])
            row.pack(fill='x', pady=(4 if i == 0 else 1, 0))
            model = _norm_block(v.get('model', '?'))
            direction = (v.get('direction', '') or '?').upper()
            range_str = v.get('range', '') or ''

            tk.Label(row, text=model, bg=c['card2'], fg=c['text'],
                     font=self._card_fonts['mono']).pack(side='left')

            # Right side: direction pill + range
            dir_color = self._direction_color(direction)
            tk.Label(row, text=direction, bg=c['card2'], fg=dir_color,
                     font=('Segoe UI', 7, 'bold'), padx=6
                     ).pack(side='right')
            if range_str:
                tk.Label(row, text=range_str, bg=c['card2'], fg=c['muted'],
                         font=self._card_fonts['caption']).pack(side='right',
                                                          padx=(0, s['sm']))

            # v4.13.34: one-line reasoning snippet beneath the vote row.
            # Surfaces the model's actual rationale at a glance instead
            # of forcing a click into View reasoning. Skipped silently
            # if no usable text was found.
            try:
                snippet = self._extract_reason_snippet(v)
                if snippet:
                    snip_row = tk.Frame(block, bg=c['card2'])
                    snip_row.pack(fill='x', padx=(s['md'], 0),
                                   pady=(0, 0))
                    tk.Label(snip_row, text=snippet,
                             bg=c['card2'], fg=c['dim'],
                             font=('Segoe UI', 7, 'italic'),
                             anchor='w', justify='left',
                             wraplength=520
                             ).pack(side='left', anchor='w')
            except Exception:
                pass

        if target_line:
            tk.Label(block, text=target_line, bg=c['card2'], fg=c['muted'],
                     font=self._card_fonts['caption'], anchor='w', justify='left'
                     ).pack(fill='x', pady=(s['xs'], 0))

    def _starve_escalate(self, ticker, committed, run_kind):
        """v4.14.6.111 (Item 5): STARVE escalation for a holdings consensus.
        Banner on EVERY starved run (contextual); emit_system_event only when the
        starve state CHANGES for this ticker (layered on emit's own 5-min dedup),
        so a chronically-starved provider doesn't re-nag. "Starved" =
        0 < committed live voices < CONSENSUS_STARVE_FLOOR (the all-failed
        0-voice case has its own message). READ-ONLY: reads the committed count
        finalize already computed; never alters the verdict, the votes that
        counted, consensus_id, or recording. Never raises."""
        try:
            import tm_consensus as _tc_st
            floor = int(getattr(_tc_st, 'CONSENSUS_STARVE_FLOOR', 2))
            live = len(committed or [])
            starved = (0 < live < floor)
            if starved:
                self.app._log(
                    f"⚠ Consensus {ticker}: STARVED — only {live} live "
                    f"voice(s) (< {floor}); treat the verdict as low confidence "
                    f"(providers cooled/capped/out).", 'amber')
            app = self.app
            ss = getattr(app, '_consensus_starve_state', None)
            if ss is None:
                ss = {}
                app._consensus_starve_state = ss
            key = f"{(ticker or '').upper()}:{run_kind}"
            if starved and ss.get(key) is not True:
                try:
                    import tm_teacher_intercept as _tm_ic_st
                    _tm_ic_st.emit_system_event(
                        'consensus_starved', app=app,
                        context={'ticker': ticker, 'live_voices': live,
                                 'floor': floor})
                except Exception:
                    pass
            ss[key] = starved
        except Exception:
            pass

    def _is_running_for(self, ticker):
        """Return the run_state dict if a consensus is currently running
        for this ticker, else None."""
        runner = getattr(self, '_active_runner', None)
        if runner is None:
            return None
        if getattr(runner, 'ticker', '') != ticker.upper():
            return None
        # Reconstruct lightweight state for display from the runner's
        # results — votes already arrived are in runner._results['votes'].
        try:
            votes_done = list(runner._results.get('votes', []))
        except Exception:
            votes_done = []
        models = list(getattr(runner, 'models', []) or [])
        done_models = {v.get('model') for v in votes_done if v.get('model')}
        # Anything that's not done is either currently running or queued.
        current = getattr(runner, '_current_request', None)
        current_model = None
        if current is not None:
            current_model = getattr(current, 'model', None)
        queued = [m for m in models
                   if m not in done_models and m != current_model]
        return {
            'votes': votes_done,
            'current': current_model,
            'queued': queued,
            'all_models': models,
            # v4.14.6.111-streaming: carry the runner's pre-fetched per-model
            # weights so the running block can show a progressive tally line.
            'weight_map': getattr(runner, 'weight_map', None),
            'accuracy_enabled': getattr(
                runner, 'accuracy_weighting_enabled', False),
        }

    def _render_running_block(self, block, run_state):
        c = self.c
        s = self._card_space
        votes = run_state.get('votes', [])
        current = run_state.get('current')
        queued = run_state.get('queued', [])
        all_models = run_state.get('all_models', [])

        tk.Label(block,
                 text=f"CONSENSUS RUNNING  ·  "
                      f"{len(votes)}/{len(all_models)} done",
                 bg=c['card2'], fg=c['accent'],
                 font=('Segoe UI', 7, 'bold'), anchor='w'
                 ).pack(fill='x')

        # v4.14.6.111-streaming: progressive "so far" tally that updates as each
        # vote lands (3 HOLD · 1 BUY). The FINAL verdict is just this when the run
        # completes — no forced deadline; a slow model simply fills in later.
        try:
            import tm_consensus as _tc_tally
            _so_far = _tc_tally.format_votes_so_far(
                votes, run_state.get('weight_map'),
                run_state.get('accuracy_enabled'))
            if _so_far:
                tk.Label(block, text=f"so far: {_so_far}",
                         bg=c['card2'], fg=c.get('dim', c.get('muted', '#888')),
                         font=('Segoe UI', 7, 'italic'), anchor='w'
                         ).pack(fill='x', pady=(1, 0))
        except Exception:
            pass

        # v4.14.5.14-portfolio-narration-and-honesty (Fix B): self-fix
        # narration line — the engine's recovery events ("An AI was busy
        # - trying another...") translated by the engine-generic
        # _translate_lookup_progress (shared with Look Up / Recommend),
        # set by the wrapped log_callback in the Refresh handlers. Empty
        # until an event fires; only one Refresh runs at a time.
        _narr = getattr(self, '_refresh_narration', '') or ''
        if _narr:
            tk.Label(block, text=_narr, bg=c['card2'],
                     fg=c.get('dim', c.get('muted', '#888')),
                     font=('Segoe UI', 7, 'italic'), anchor='w',
                     wraplength=520, justify='left'
                     ).pack(fill='x', pady=(2, 0))

        # v4.14.0 stage 6d: canonicalize model labels at display.
        try:
            import tm_api_providers as _tmap6d_run
            _norm_run = _tmap6d_run.canonicalize_model_label
        except Exception:
            _norm_run = lambda x: x

        # Done votes
        for v in votes:
            row = tk.Frame(block, bg=c['card2'])
            row.pack(fill='x', pady=(2, 0))
            tk.Label(row, text=_norm_run(v.get('model', '?')),
                     bg=c['card2'], fg=c['text'],
                     font=self._card_fonts['mono']).pack(side='left')
            # v4.14.6.111-finalize-deadline: distinct "timed out" state for a
            # model that didn't answer within the finalize deadline.
            if v.get('timed_out'):
                tk.Label(row, text='timed out', bg=c['card2'],
                         fg=c.get('dim', c.get('muted', '#888')),
                         font=('Segoe UI', 7, 'italic'), padx=6
                         ).pack(side='right')
                continue
            d = (v.get('direction', '') or '?').upper()
            tk.Label(row, text=d, bg=c['card2'],
                     fg=self._direction_color(d),
                     font=('Segoe UI', 7, 'bold'), padx=6
                     ).pack(side='right')

        # Currently running
        if current:
            row = tk.Frame(block, bg=c['card2'])
            row.pack(fill='x', pady=(2, 0))
            tk.Label(row, text=current, bg=c['card2'], fg=c['text'],
                     font=self._card_fonts['mono']).pack(side='left')
            tk.Label(row, text='running...', bg=c['card2'], fg=c['accent'],
                     font=('Segoe UI', 7, 'italic')
                     ).pack(side='right', padx=6)

        # Queued
        for m in queued:
            row = tk.Frame(block, bg=c['card2'])
            row.pack(fill='x', pady=(2, 0))
            tk.Label(row, text=m, bg=c['card2'], fg=c['dim'],
                     font=self._card_fonts['mono']).pack(side='left')
            tk.Label(row, text='queued', bg=c['card2'], fg=c['dim'],
                     font=('Segoe UI', 7, 'italic')
                     ).pack(side='right', padx=6)

    def _build_plan_block(self, parent, ticker, holding):
        """v4.13.19/v4.13.20: Show the AI's sell triggers (target/stop)
        from the most recent BUY prediction. R/R computed at user's
        actual entry price.

        v4.13.20: Clearer labels (Sell up at / Sell down at) and smart
        gain/loss framing — if stop is above entry (stock ran up since
        purchase), say "still profitable" not "loss."

        Renders nothing if no BUY prediction exists for this ticker.
        """
        c = self.c
        s = self._card_space
        try:
            predictions_log = self.app._holdings_state.get('predictions_log')
        except Exception:
            predictions_log = None
        if predictions_log is None:
            return

        pred = _find_buy_prediction_for_triggers(predictions_log, ticker)
        if pred is None:
            return

        try:
            target = float(pred.get('target') or 0)
            stop = float(pred.get('stop') or 0)
        except (ValueError, TypeError):
            return
        if target <= 0 or stop <= 0:
            return

        # v4.14.6.111 AI-target-aggregate: prefer the AI consensus target
        # AGGREGATE (responders-only mean/median + N-of-M) for consistency with
        # Look Up / Recommend. Owned-position predictions are AI-sourced (never
        # the algo screener), so this only refines the displayed target + adds an
        # N-of-M note; it never introduces an algo value. Falls back to the
        # single prediction target when no aggregate is computable. Stop stays
        # the prediction's stop (the labeled mechanical reference). Display-only:
        # the portfolio TOTAL / cost-basis / P&L math is elsewhere and untouched.
        _agg_note = ''
        try:
            _agg = self.app._compute_ai_target_for(ticker, pred.get('path'))
            if _agg and _agg.get('target'):
                target = float(_agg['target'])
                _agg_note = (f" · {int(_agg['n'])} of {int(_agg['m'])}"
                             + (", median" if _agg.get('method') == 'median'
                                else ""))
        except Exception:
            _agg_note = ''

        try:
            shares = float(holding.get('shares') or 0)
            entry = float(holding.get('buy_price') or holding.get('entry_price') or 0)
        except (ValueError, TypeError):
            shares = 0
            entry = 0

        # Compute outcomes vs ENTRY (not vs current price)
        upside = target - entry  # gain if target hits
        downside_raw = stop - entry  # negative if stop is below entry, positive if stop is above entry
        rr = (upside / -downside_raw) if downside_raw < 0 else 0
        upside_total = upside * shares
        downside_total = downside_raw * shares  # signed: negative = loss, positive = profit

        # v4.14.5.14-sell-trigger-dated-rule (2026-05-26): the dim tag is
        # now thesis-based, not clock-based — it reads "DATED — <reason>"
        # only when the prediction's stated timeframe has expired, a sell
        # trigger price has been hit, or a new SEC filing arrived after the
        # prediction was generated; NOT merely because 24h passed. The loud
        # red "OUTDATED — consensus now says X" warning below is a separate
        # consensus-disagreement signal and is left untouched.
        #
        # `_dt`/`_td` are still imported here: the superseded-check block
        # below uses `_dt` to compare consensus vs prediction timestamps.
        from datetime import datetime as _dt, timedelta as _td  # noqa: F401
        dated = False
        dated_reason = None
        try:
            dated, dated_reason = self.is_sell_trigger_dated(
                pred, ticker, datetime.now())
        except Exception:
            dated = False
            dated_reason = None

        # v4.13.30/v4.13.33: Check whether a fresher consensus run
        # exists for this ticker. If the consensus winner is non-BUY,
        # the trigger numbers below are actively misleading -- the
        # model that proposed them no longer thinks they're valid.
        # Flag this loudly so the user ignores the numbers.
        #
        # v4.13.33 change: removed the `if stale` gate. A fresh
        # consensus disagreeing with the displayed BUY direction is
        # important even when the source prediction is recent.
        # Defensive: hasattr-guarded, full try/except so any failure
        # reverts to legacy behavior.
        # v4.14.5.14-compact-holding-row: the superseded computation moved to
        # the reusable is_consensus_outdated() helper so the collapsed-row
        # state badge and this loud expanded warning share one definition.
        try:
            superseded, superseded_winner = self.is_consensus_outdated(
                pred, ticker)
        except Exception:
            superseded, superseded_winner = False, ''

        # ── Render block ──
        block = tk.Frame(parent, bg=c['card2'], padx=s['md'], pady=s['sm'])
        block.pack(fill='x', pady=(0, s['sm']))

        hdr_row = tk.Frame(block, bg=c['card2'])
        hdr_row.pack(fill='x')
        tk.Label(hdr_row, text="SELL TRIGGERS",
                 bg=c['card2'], fg=c['accent'],
                 font=('Segoe UI', 7, 'bold')).pack(side='left')
        tk.Label(hdr_row, text=f"  (AI estimates{_agg_note})",
                 bg=c['card2'], fg=c['dim'],
                 font=('Segoe UI', 7, 'italic')).pack(side='left')
        if superseded:
            # v4.13.30: louder warning when fresh consensus disagrees.
            # Use .get() with fallback in case theme dict missing 'red'.
            try:
                tk.Label(hdr_row,
                         text=(f"  OUTDATED -- consensus now says "
                               f"{superseded_winner}"),
                         bg=c['card2'],
                         fg=c.get('red', '#ff5555'),
                         font=('Segoe UI', 7, 'bold')
                         ).pack(side='left')
            except Exception as _le:
                try:
                    self.app._v4_13_30_debug_log('panel.warning_label', _le)
                except Exception:
                    pass
        elif dated:
            tk.Label(hdr_row, text=f"  DATED — {dated_reason}",
                     bg=c['card2'], fg=c['dim'],
                     font=('Segoe UI', 7, 'italic')
                     ).pack(side='left')

        # v4.13.30: dim the trigger color when superseded so the
        # numbers visually de-emphasize. Use .get() fallback in case
        # theme dict does not have all expected keys.
        if superseded:
            target_color = c.get('dim', '#888888')
            stop_color = c.get('dim', '#888888')
        else:
            target_color = c['green']
            stop_color = c['red']

        # ── Sell up line (target) ──
        target_row = tk.Frame(block, bg=c['card2'])
        target_row.pack(fill='x', pady=(3, 0))
        tk.Label(target_row, text="Sell up at  ",
                 bg=c['card2'], fg=c['muted'],
                 font=('Segoe UI', 7)
                 ).pack(side='left')
        tk.Label(target_row, text=f"${target:.2f}",
                 bg=c['card2'], fg=target_color,
                 font=('Segoe UI', 7, 'bold')
                 ).pack(side='left')
        if shares > 0 and upside_total != 0:
            if upside_total > 0:
                tk.Label(target_row,
                         text=f"  (+${upside_total:.2f} gain)",
                         bg=c['card2'], fg=c['dim'],
                         font=('Segoe UI', 7)
                         ).pack(side='left')
            else:
                # Edge case: target below entry (rare but possible)
                tk.Label(target_row,
                         text=f"  (${upside_total:.2f} - target below entry)",
                         bg=c['card2'], fg=c['amber'],
                         font=('Segoe UI', 7)
                         ).pack(side='left')

        # ── Sell down line (stop) ──
        stop_row = tk.Frame(block, bg=c['card2'])
        stop_row.pack(fill='x', pady=(3, 0))
        tk.Label(stop_row, text="Sell down at ",
                 bg=c['card2'], fg=c['muted'],
                 font=('Segoe UI', 7)
                 ).pack(side='left')
        tk.Label(stop_row, text=f"${stop:.2f}",
                 bg=c['card2'], fg=stop_color,
                 font=('Segoe UI', 7, 'bold')
                 ).pack(side='left')
        if shares > 0 and downside_total != 0:
            if downside_total < 0:
                # Stop is below entry — this is the normal "cut loss" case
                tk.Label(stop_row,
                         text=f"  (${downside_total:.2f} loss)",
                         bg=c['card2'], fg=c['dim'],
                         font=('Segoe UI', 7)
                         ).pack(side='left')
            else:
                # Stop is ABOVE entry — stock ran up. Even if stop fires,
                # you book a small profit. This is good news.
                tk.Label(stop_row,
                         text=f"  (still +${downside_total:.2f} profitable!)",
                         bg=c['card2'], fg=c['green'],
                         font=('Segoe UI', 7, 'italic')
                         ).pack(side='left')

        # ── Reward / Risk ratio ──
        if rr > 0:
            rr_color = (c['green'] if rr >= 2.0
                         else c['amber'] if rr >= 1.0
                         else c['red'])
            rr_row = tk.Frame(block, bg=c['card2'])
            rr_row.pack(fill='x', pady=(3, 0))
            tk.Label(rr_row, text="Reward vs Risk  ",
                     bg=c['card2'], fg=c['muted'],
                     font=('Segoe UI', 7)
                     ).pack(side='left')
            tk.Label(rr_row, text=f"{rr:.2f}x",
                     bg=c['card2'], fg=rr_color,
                     font=('Segoe UI', 7, 'bold')
                     ).pack(side='left')
            if rr < 1.0:
                tk.Label(rr_row, text=" (risk > reward)",
                         bg=c['card2'], fg=c['dim'],
                         font=('Segoe UI', 7, 'italic')
                         ).pack(side='left')
            elif rr >= 3.0:
                tk.Label(rr_row, text=" (excellent)",
                         bg=c['card2'], fg=c['green'],
                         font=('Segoe UI', 7, 'italic')
                         ).pack(side='left')

        # v4.13.32/v4.13.33: Display the prediction's timeframe so the
        # price targets have temporal context. "Sell up at $9" in 2 weeks
        # is a very different bet than $9 in 6 months at the same R/R.
        # v4.13.33: row always renders; if the source prediction has no
        # timeframe_days, show "not specified" instead of hiding the row,
        # so the absence of the data is visible.
        try:
            tf_days = pred.get('timeframe_days')
            try:
                if tf_days is not None:
                    tf_days = int(tf_days)
            except (ValueError, TypeError):
                tf_days = None

            tf_row = tk.Frame(block, bg=c['card2'])
            tf_row.pack(fill='x', pady=(3, 0))
            tk.Label(tf_row, text="Timeframe  ",
                     bg=c['card2'], fg=c['muted'],
                     font=('Segoe UI', 7)
                     ).pack(side='left')

            if tf_days and tf_days > 0:
                if tf_days <= 14:
                    tf_text = f"{tf_days} days"
                elif tf_days <= 60:
                    weeks = round(tf_days / 7)
                    tf_text = f"~{weeks} weeks"
                elif tf_days <= 365:
                    months = round(tf_days / 30)
                    tf_text = f"~{months} months"
                else:
                    years = round(tf_days / 365, 1)
                    tf_text = f"~{years} years"

                deadline_str = ""
                try:
                    pred_ts_str = pred.get('timestamp', '')
                    if pred_ts_str:
                        from datetime import (datetime as _dt32,
                                              timedelta as _td32)
                        pred_dt2 = _dt32.fromisoformat(pred_ts_str)
                        deadline = pred_dt2 + _td32(days=tf_days)
                        deadline_str = (
                            f"  by {deadline.strftime('%b %d')}")
                except Exception:
                    pass

                tk.Label(tf_row, text=tf_text,
                         bg=c['card2'], fg=c.get('text', '#fff'),
                         font=('Segoe UI', 7, 'bold')
                         ).pack(side='left')
                if deadline_str:
                    tk.Label(tf_row, text=deadline_str,
                             bg=c['card2'], fg=c['dim'],
                             font=('Segoe UI', 7, 'italic')
                             ).pack(side='left')
            else:
                # v4.13.33: fallback when no timeframe in the prediction
                tk.Label(tf_row, text="not specified",
                         bg=c['card2'], fg=c['dim'],
                         font=('Segoe UI', 7, 'italic')
                         ).pack(side='left')
                tk.Label(tf_row,
                         text="  (model didn't supply one)",
                         bg=c['card2'], fg=c['dim'],
                         font=('Segoe UI', 7, 'italic')
                         ).pack(side='left')
        except Exception as _tfe:
            try:
                self.app._v4_13_30_debug_log(
                    'panel.timeframe_display', _tfe)
            except Exception:
                pass

    def _direction_color(self, direction: str) -> str:
        """Color-code BUY / HOLD / SELL / AVOID / WATCH."""
        c = self.c
        d = direction.upper()
        if d in ('BUY', 'BUY MORE', 'ADD'):
            return c['green']
        if d in ('SELL', 'TRIM'):
            return c['red']
        if d in ('HOLD',):
            return c['amber']
        # v4.14.2 stage 4: WATCH (the candidate-prompt third option)
        # shares the amber-ish "neither yes nor no" color with HOLD.
        # Different word, same UI weight.
        if d in ('WATCH',):
            return c['amber']
        if d in ('AVOID',):
            return c['dim']
        return c['text']

    def _build_card_buttons(self, parent, ticker, freshness, has_consensus):
        c = self.c
        s = self._card_space
        bar = tk.Frame(parent, bg=c['card'])
        bar.pack(fill='x', pady=(s['sm'], 0))

        # The Run consensus button is the primary action when freshness is
        # red, never, or stale; otherwise it's just one of the options.
        emphasize_run = freshness in ('never', 'old')
        run_bg = c['accent'] if emphasize_run else c['card2']
        run_fg = c['bg'] if emphasize_run else c['accent']
        run_label = "Run consensus" if not has_consensus else "Re-run consensus"

        def _btn(parent, text, cmd, *, primary=False, disabled=False):
            bg = c['accent'] if primary else c['card2']
            fg = c['bg'] if primary else c['text']
            b = tk.Button(parent, text=text, bg=bg, fg=fg,
                          relief='flat', borderwidth=0, cursor='hand2',
                          padx=10, pady=4,
                          font=self._card_fonts['body_bold'], command=cmd,
                          activebackground=c['accent'],
                          activeforeground=c['bg'])
            if disabled:
                b.config(state='disabled', cursor='arrow',
                         disabledforeground=c.get('dim', '#666'))
            return b

        # v4.14.5.14-portfolio-narration-and-honesty (Fix E): only one
        # Refresh/consensus runs at a time. If one is already running on a
        # DIFFERENT holding, disable this card's consensus buttons + show a
        # visible note — instead of the old behaviour where clicking here
        # SILENTLY cancelled the other holding's run. Re-enables on the
        # next render after that run completes (render fires per model
        # event). Same-ticker re-click is still allowed (re-run).
        _active = getattr(self, '_active_runner', None)
        _busy_other = (
            _active is not None
            and getattr(_active, 'ticker', '').upper() != (ticker or '').upper())
        if _busy_other:
            _other_tk = getattr(_active, 'ticker', '') or 'another holding'
            tk.Label(bar, text=f"⏳ refresh running on {_other_tk}",
                     bg=c['card'], fg=c.get('dim', c.get('muted', '#888')),
                     font=('Segoe UI', 7, 'italic')
                     ).pack(side='left', padx=(0, s['xs']))

        _btn(bar, run_label,
             lambda t=ticker: self._on_run_consensus(t),
             primary=emphasize_run, disabled=_busy_other
             ).pack(side='left', padx=(0, s['xs']))

        # v4.13.31: Refresh triggers button. Runs a fresh_buy consensus
        # on this ticker, which writes new BUY predictions with new
        # target/stop. After the run, the SELL TRIGGERS block on the
        # card reads the new prediction and updates -- no more stale
        # $10 targets when the model has shifted.
        _btn(bar, "Refresh triggers",
             lambda t=ticker: self._on_refresh_triggers(t),
             disabled=_busy_other
             ).pack(side='left', padx=(0, s['xs']))

        if has_consensus:
            _btn(bar, "View reasoning",
                 lambda t=ticker: self._on_view_reasoning(t)).pack(side='left',
                                                                     padx=s['xs'])

        _btn(bar, "Sell",
             lambda t=ticker: self._on_sell(t)).pack(side='right',
                                                      padx=(s['xs'], 0))
        _btn(bar, "Remove",
             lambda t=ticker: self._on_remove(t)).pack(side='right')

    # ─── Locked card ───────────────────────────────────────────────

    def _build_locked_card(self, parent, holding):
        """Locked / written-off card render.

        v4.14.2 stage 4 follow-up: same collapse/expand treatment as
        the tradable card. Header (ticker + LOCKED/WRITTEN OFF pill +
        Remove button + chevron) is always visible; the body
        (sub-line + lock_reason note) toggles with the chevron.
        Click anywhere in the header EXCEPT the Remove button
        toggles the card.
        """
        c = self.c
        # v4.14.5.14-writeoff-density: locked/written-off cards reuse the
        # tradable card's scoped _card_space (md 7 / sm 4 / xs 2) so they are
        # at least as tight as the active holdings above — written-off
        # positions are reference-only and shouldn't dominate. Shared
        # self.space is untouched.
        s = self._card_space
        ticker = holding.get('ticker', '?')
        shares = holding.get('shares', 0)
        buy_price = holding.get('buy_price', 0)
        total_cost = holding.get('total_cost', shares * buy_price)
        last_status_check = holding.get('last_status_check') or holding.get('last_analyzed')

        card = tk.Frame(parent, bg=c['card'], padx=s['md'], pady=s['sm'],
                         highlightbackground=c['border'],
                         highlightthickness=1)
        card.pack(fill='x', padx=s['md'], pady=2)

        # Header row (always visible)
        header = tk.Frame(card, bg=c['card'])
        header.pack(fill='x')

        # v4.13.2: differentiate written_off from locked visually
        _status = holding.get('status',
                              'locked' if not holding.get('tradable', True)
                              else 'tradable')
        tk.Label(header, text=ticker, bg=c['card'], fg=c['muted'],
                 font=('Segoe UI', 10, 'bold')).pack(side='left')
        if _status == 'written_off':
            tk.Label(header, text='💀 WRITTEN OFF', bg=c['card2'],
                     fg=c['dim'],
                     font=('Segoe UI', 7, 'bold'), padx=6, pady=1
                     ).pack(side='left', padx=s['sm'])
        else:
            tk.Label(header, text='LOCKED', bg=c['card2'], fg=c['amber'],
                     font=('Segoe UI', 7, 'bold'), padx=6, pady=1
                     ).pack(side='left', padx=s['sm'])

        # Right-aligned remove button (small)
        remove_btn = tk.Button(header, text='Remove',
                                bg=c['card2'], fg=c['muted'],
                                relief='flat', borderwidth=0, cursor='hand2',
                                padx=8, pady=2, font=self.fonts['caption'],
                                activebackground=c['red'],
                                activeforeground=c['bg'],
                                command=lambda t=ticker: self._on_remove(t))
        remove_btn.pack(side='right')

        # v4.14.2 stage 4 follow-up: chevron between Remove and the
        # left-side content. Pack right BEFORE the remove_btn so it
        # appears just left of Remove (right-pack stacks
        # right-to-left, so first packed = rightmost).
        is_expanded = self._get_card_expanded(ticker)
        chevron = tk.Label(header,
                            text=('▼' if is_expanded else '▶'),
                            bg=c['card'], fg=c['dim'],
                            font=('Segoe UI', 8),
                            cursor='hand2')
        chevron.pack(side='right', padx=(s['sm'], s['xs']))

        # v4.14.5.14-ai-verdict-indicator: same "AI no longer says BUY" hint
        # on written-off cards (both card types, per spec). Right-packed so
        # it never clips. Usually None — written-off positions aren't
        # actively analyzed — but surface it when a recent non-BUY consensus
        # exists.
        try:
            _wo_sl = self._signals_log()
            _wo_consensus = (_read_latest_consensus(_wo_sl, ticker)
                             if _wo_sl else None)
            _wo_ind = _ai_verdict_indicator(
                self._compute_ai_current_verdict(ticker, _wo_consensus))
        except Exception:
            _wo_ind = None
        if _wo_ind:
            _wo_text, _wo_colorkey = _wo_ind
            tk.Label(header, text=_wo_text, bg=c['card'],
                     fg=c.get(_wo_colorkey, c['amber']),
                     font=('Segoe UI', 8)
                     ).pack(side='right', padx=(s['sm'], 0))

        # ── BODY (collapsible) ─────────────────────────────────────
        body = tk.Frame(card, bg=c['card'])
        body_pack_kwargs = {'fill': 'x', 'pady': (2, 0)}
        if is_expanded:
            body.pack(**body_pack_kwargs)

        # Sub-line: shares, cost, status
        bits = [
            f"{_format_shares(shares)} sh @ {_format_money(buy_price)}",
            f"cost {_format_money(total_cost)}",
            "now ~$0",
        ]
        if last_status_check:
            bits.append(_humanize_age(last_status_check).replace('scanned',
                                                                  'status'))
        tk.Label(body, text="  ·  ".join(bits),
                 bg=c['card'], fg=c['dim'],
                 font=self._card_fonts['caption'], anchor='w'
                 ).pack(fill='x', pady=(2, 0))

        # v4.13.2: if lock_reason set, show it as italic note
        _lock_reason = holding.get('lock_reason', '')
        if _lock_reason:
            tk.Label(body, text=_lock_reason,
                     bg=c['card'], fg=c['dim'],
                     font=('Segoe UI', 7, 'italic'), anchor='w',
                     wraplength=420, justify='left'
                     ).pack(fill='x', pady=(1, 0))

        # Wire click-to-toggle on the header. Remove button is in
        # skip_widgets so clicking Remove still removes (not toggles).
        def _toggle(t=ticker, b=body, ch=chevron, kw=body_pack_kwargs):
            self._toggle_card_expand(t, b, ch, body_pack_kwargs=kw)

        self._bind_card_header_click(
            header, _toggle, skip_widgets=(remove_btn,))

    # ─── Add form ──────────────────────────────────────────────────

    def _build_add_form(self, parent):
        c = self.c
        s = self.space
        card = tk.Frame(parent, bg=c['card2'], padx=s['md'], pady=s['md'])
        card.pack(fill='x', padx=s['md'], pady=(s['sm'], s['md']))

        tk.Label(card, text="ADD HOLDING", bg=c['card2'], fg=c['muted'],
                 font=('Segoe UI', 8, 'bold'), anchor='w'
                 ).pack(fill='x')

        row = tk.Frame(card, bg=c['card2'])
        row.pack(fill='x', pady=(s['xs'], s['xs']))

        def _entry(parent, var, placeholder, width):
            e = tk.Entry(parent, textvariable=var, bg=c['bg'], fg=c['text'],
                         insertbackground=c['accent'],
                         relief='flat', borderwidth=0,
                         font=self.fonts['body'], width=width)
            e.pack(side='left', padx=(0, s['sm']), ipady=4)
            # Placeholder behavior — gray text that disappears on focus.
            def _set_placeholder():
                if not var.get():
                    e.config(fg=c['dim'])
                    e.insert(0, placeholder)
            def _on_focus_in(_):
                if e.get() == placeholder:
                    e.delete(0, 'end')
                    e.config(fg=c['text'])
            def _on_focus_out(_):
                if not e.get():
                    e.config(fg=c['dim'])
                    e.insert(0, placeholder)
            e.bind('<FocusIn>', _on_focus_in)
            e.bind('<FocusOut>', _on_focus_out)
            _set_placeholder()
            return e

        _entry(row, self._form_ticker, 'Ticker', 8)
        _entry(row, self._form_shares, 'Shares', 10)
        _entry(row, self._form_price, 'Cost / share', 12)

        bottom = tk.Frame(card, bg=c['card2'])
        bottom.pack(fill='x', pady=(s['xs'], 0))
        tk.Checkbutton(bottom, text='Tradable',
                        variable=self._form_tradable,
                        bg=c['card2'], fg=c['muted'],
                        selectcolor=c['bg'],
                        activebackground=c['card2'],
                        activeforeground=c['text'],
                        font=self.fonts['caption']
                        ).pack(side='left')
        # v4.13.2: per-holding path dropdown. Empty = global default.
        try:
            from tkinter import ttk as _ttk
            tk.Label(bottom, text='  Path:', bg=c['card2'],
                     fg=c['muted'],
                     font=self.fonts['caption']).pack(side='left')
            _path_combo = _ttk.Combobox(
                bottom, textvariable=self._form_path,
                values=['(global)', 'slow_safe', 'moderate',
                        'aggressive', 'lottery', 'penny_lottery'],
                width=12, state='readonly')
            _path_combo.pack(side='left', padx=(2, 0))
            if not self._form_path.get():
                _path_combo.set('(global)')
        except Exception:
            pass
        tk.Button(bottom, text='Add', bg=c['accent'], fg=c['bg'],
                  relief='flat', borderwidth=0, cursor='hand2',
                  padx=14, pady=4, font=self.fonts['body_bold'],
                  activebackground=c['green'], activeforeground=c['bg'],
                  command=self._on_add).pack(side='right')

    # ─── Action handlers ───────────────────────────────────────────

    def _on_add(self):
        ticker_raw = self._form_ticker.get().strip().upper()
        shares_raw = self._form_shares.get().strip()
        price_raw = self._form_price.get().strip()
        # Strip placeholders if user never typed
        if ticker_raw in ('', 'TICKER'):
            self.app._log("Add holding: please enter a ticker.", 'amber')
            return
        try:
            shares = float(shares_raw)
        except ValueError:
            self.app._log("Add holding: shares must be a number.", 'amber')
            return
        try:
            price = float(price_raw.lstrip('$'))
        except ValueError:
            self.app._log("Add holding: price must be a number.", 'amber')
            return
        if shares <= 0 or price <= 0:
            self.app._log("Add holding: shares and price must be positive.",
                          'amber')
            return

        # v4.14.5.14-portfolio-narration-and-honesty (Fix C): validate the
        # ticker at submit (reuse Look Up Phase 2's fail-open
        # _is_valid_ticker) so a typo / delisted symbol is caught with a
        # friendly message instead of being silently added. Fail-open: an
        # unknown-but-plausible ticker when the universe isn't loaded still
        # goes through (a validator error never blocks an add).
        try:
            _valid_tk = self.app._is_valid_ticker(ticker_raw)
        except Exception:
            _valid_tk = True
        if not _valid_tk:
            _msg = self.app._get_playbook_message(
                'portfolio_add_holding_invalid_ticker',
                ticker=ticker_raw) or (
                f"'{ticker_raw}' isn't a recognized ticker — check the "
                f"spelling, or it may just not be in the database yet.")
            self.app._log(_msg, 'amber')
            return

        mgr = self._mgr()
        if mgr is None:
            self.app._log("Add holding: portfolio data layer not ready.", 'red')
            return
        try:
            # v4.13.2: include path. '(global)' means leave path=None
            # so the holding follows the global setting.
            _selected_path = self._form_path.get().strip()
            if _selected_path in ('', '(global)'):
                _resolved_path = None
            else:
                _resolved_path = _selected_path
            # v4.13.57: add_holding now returns a dict with cash info
            add_result = mgr.add_holding(
                ticker_raw, shares, price,
                tradable=self._form_tradable.get(),
                path=_resolved_path)
            # v4.14.5.14-portfolio-atomic-save: surface a save failure
            # (atomic save returns False) instead of silently "succeeding".
            if not mgr.save():
                self._handle_save_failure('add')
                return
        except Exception as e:
            self.app._log(f"Add holding failed: {type(e).__name__}: {e}",
                          'red')
            return

        # v4.13.57: if add_holding is the new shape, log cash movement
        cost = float(shares) * float(price)
        if isinstance(add_result, dict) and add_result.get('created'):
            cash_after = add_result.get('cash_after', 0.0)
            self.app._log(
                f"Bought {ticker_raw}: {_format_shares(shares)} sh @ "
                f"{_format_money(price)} "
                f"(-{_format_money(cost)} cash → "
                f"{_format_money(cash_after)} remaining)",
                'green')
        else:
            # Existing-holding update or legacy add_holding return shape
            self.app._log(
                f"Added {ticker_raw}: {_format_shares(shares)} sh @ "
                f"{_format_money(price)}", 'green')
        # Clear form
        self._form_ticker.set('')
        self._form_shares.set('')
        self._form_price.set('')
        self._form_path.set('(global)')   # v4.13.2
        self.render()

    def _on_remove(self, ticker):
        if not messagebox.askyesno(
                "Remove holding",
                f"Remove {ticker} from your portfolio?\n\n"
                "This does NOT record a sale. If you sold this position, "
                "use the Sell button instead so the realized P&L gets "
                "tracked.\n\n"
                "Use Remove only for positions you no longer want to "
                "track (e.g., a locked stock that's truly worthless)."):
            return
        mgr = self._mgr()
        if mgr is None:
            return
        try:
            ok = mgr.remove_holding(ticker)
            if ok and not mgr.save():
                # v4.14.5.14-portfolio-atomic-save: save failed — roll back.
                self._handle_save_failure('remove')
                return
        except Exception as e:
            self.app._log(f"Remove {ticker} failed: {e}", 'red')
            return
        if ok:
            self.app._log(f"Removed {ticker} from portfolio.", 'muted')
            self.render()
        else:
            self.app._log(f"Could not remove {ticker} (not found).", 'amber')

    def _on_sell(self, ticker):
        # Quick-confirm dialog. Pre-fills sell price from latest quote if any.
        mgr = self._mgr()
        if mgr is None:
            return
        holding = next((h for h in self.app.portfolio.get('holdings', [])
                         if h.get('ticker', '').upper() == ticker.upper()),
                        None)
        if holding is None:
            self.app._log(f"Sell: {ticker} not found.", 'amber')
            return

        current_price = self._latest_price(ticker)
        default_price = current_price if current_price else holding.get('buy_price', 0)
        shares = holding.get('shares', 0)

        dlg = tk.Toplevel(self.parent)
        dlg.title(f"Sell {ticker}")
        dlg.configure(bg=self.c['bg'])
        dlg.transient(self.parent.winfo_toplevel())
        dlg.grab_set()
        # Center over parent. Tk's default position is top-left of screen,
        # which looks broken — feels like a separate app window.
        try:
            parent_root = self.parent.winfo_toplevel()
            parent_root.update_idletasks()
            px = parent_root.winfo_rootx()
            py = parent_root.winfo_rooty()
            pw = parent_root.winfo_width()
            ph = parent_root.winfo_height()
            dw, dh = 380, 220
            dx = px + (pw - dw) // 2
            dy = py + (ph - dh) // 2
            dlg.geometry(f"{dw}x{dh}+{dx}+{dy}")
        except Exception:
            dlg.geometry("380x220")

        c = self.c
        s = self.space
        frame = tk.Frame(dlg, bg=c['bg'], padx=s['lg'], pady=s['lg'])
        frame.pack(fill='both', expand=True)

        tk.Label(frame, text=f"Sell {_format_shares(shares)} shares of {ticker}",
                 bg=c['bg'], fg=c['accent'],
                 font=self.fonts['heading']).pack(anchor='w')
        tk.Label(frame, text=f"Cost basis: {_format_money(holding.get('buy_price', 0))}/sh",
                 bg=c['bg'], fg=c['muted'], font=self.fonts['caption']
                 ).pack(anchor='w', pady=(2, s['md']))

        price_row = tk.Frame(frame, bg=c['bg'])
        price_row.pack(fill='x')
        tk.Label(price_row, text="Sell price / share:",
                 bg=c['bg'], fg=c['text'], font=self.fonts['body']
                 ).pack(side='left')
        price_var = tk.StringVar(value=f"{default_price:.4f}".rstrip('0').rstrip('.')
                                  if default_price else '')
        tk.Entry(price_row, textvariable=price_var,
                 bg=c['card'], fg=c['text'], insertbackground=c['accent'],
                 relief='flat', borderwidth=0, font=self.fonts['body'],
                 width=12).pack(side='right', ipady=4)

        result = {'price': None}

        def do_sell():
            try:
                p = float(price_var.get().lstrip('$'))
                if p <= 0:
                    raise ValueError("must be positive")
            except ValueError:
                messagebox.showerror("Invalid price",
                                      "Please enter a positive number.",
                                      parent=dlg)
                return
            result['price'] = p
            dlg.destroy()

        def cancel():
            dlg.destroy()

        btn_row = tk.Frame(frame, bg=c['bg'])
        btn_row.pack(fill='x', pady=(s['md'], 0))
        tk.Button(btn_row, text="Cancel", bg=c['card2'], fg=c['text'],
                  relief='flat', borderwidth=0, cursor='hand2',
                  padx=14, pady=6, font=self.fonts['body_bold'],
                  command=cancel).pack(side='right', padx=(s['xs'], 0))
        tk.Button(btn_row, text="Confirm sale", bg=c['accent'], fg=c['bg'],
                  relief='flat', borderwidth=0, cursor='hand2',
                  padx=14, pady=6, font=self.fonts['body_bold'],
                  activebackground=c['green'], activeforeground=c['bg'],
                  command=do_sell).pack(side='right')

        dlg.wait_window()
        if result['price'] is None:
            return

        # Apply the sale.
        try:
            # v4.13.57: sell_holding now returns dict with cash info
            sell_result = mgr.sell_holding(ticker, result['price'])
            # v4.14.5.14-portfolio-atomic-save: a failed save here was the
            # worst silent-data-loss path — the sale showed as done but
            # never persisted. Now surface it + roll back to disk.
            if not mgr.save():
                self._handle_save_failure('sell', context={'ticker': ticker})
                return
        except Exception as e:
            self.app._log(f"Sell {ticker} failed: {e}", 'red')
            return

        # v4.14.5.14-sold-prediction-tracking: close the AI's open BUY
        # predictions for this ticker as 'sold' at the sale price, so the
        # sale shows up in Track Record. The (now hidden) HoldingsWindow
        # sell dialog always did this; the Portfolio panel — the LIVE sell
        # surface — never did, so every sale here left its predictions
        # dangling 'open' forever (that's why your RIG win was invisible
        # to prediction Track Record even though it's in Trade History).
        # Flag-guarded for instant rollback; fully fail-open.
        _cfg = getattr(self.app, 'cfg', {}) or {}
        if bool(_cfg.get('use_sold_prediction_tracking', True)):
            try:
                plog = self._predictions_log()
                if plog is not None:
                    _upd = plog.mark_position_sold(ticker, result['price'])
                    if _upd:
                        self.app._log(
                            f"[track-record] marked {len(_upd)} open "
                            f"{ticker} prediction(s) as sold at "
                            f"{_format_money(result['price'])}.", 'muted')
            except Exception:
                pass

        sell_total = result['price'] * shares
        cost = holding.get('total_cost', shares * holding.get('buy_price', 0))
        pnl = sell_total - cost
        sign = '+' if pnl >= 0 else ''
        # v4.13.57: include cash impact in log
        cash_after = (sell_result.get('cash_after')
                       if isinstance(sell_result, dict) else None)
        if cash_after is not None:
            self.app._log(
                f"Sold {ticker}: {_format_shares(shares)} sh @ "
                f"{_format_money(result['price'])} → "
                f"{_format_money(sell_total)} "
                f"({sign}{_format_money(pnl)} P&L) "
                f"[+{_format_money(sell_total)} cash → "
                f"{_format_money(cash_after)} total]",
                'green' if pnl >= 0 else 'amber')
        else:
            self.app._log(
                f"Sold {ticker}: {_format_shares(shares)} sh @ "
                f"{_format_money(result['price'])} → "
                f"{_format_money(sell_total)} "
                f"({sign}{_format_money(pnl)} P&L)",
                'green' if pnl >= 0 else 'amber')
        # v4.14.5.14-portfolio-narration-and-honesty (Fix D): post-action
        # coaching hook on the LIVE Sell path. action_id 'sell' has no
        # observations authored yet (inert today), but wiring it now means
        # a future "you sold at a loss / sold a recent BUY" coaching entry
        # fires with no code change. try/except: never breaks the sale.
        try:
            import tm_teacher_intercept as _tm_ic_sell
            _tm_ic_sell.observe_action(self.app, 'sell', {
                'ticker': ticker,
                'shares': shares,
                'realized_pl': pnl,
            })
        except Exception as _e_obs:
            try:
                self.app._log(
                    f"[portfolio] sell observe_action warning: {_e_obs}",
                    'muted')
            except Exception:
                pass
        self.render()

    def _write_refresh_prediction(self, ticker, path, consensus):
        """v4.14.5.14-refresh-triggers-writes-prediction (2026-05-26): after an
        owned-position consensus (Refresh triggers / Re-run consensus)
        completes, write ONE prediction record so the SELL TRIGGERS block
        re-renders from the FRESH verdict (instead of the stale original BUY)
        and the OUTDATED comparison clears on its own.

        Mirrors the Look Up writer (`_write_prediction_from_vote` in
        tired_market.py): re-parses a representative WINNING-direction vote's
        raw response via `tm_discover.parse_prediction` to get NUMERIC
        target/stop/timeframe_days (the consensus rollup only carries free-text
        per-vote strings + a range string — no aggregated numbers), then stamps
        `direction`=consensus winner + `source='refresh_triggers'`. Writes
        nothing when there is no winning-direction vote with a usable
        target+stop, or when the winner is SELL/AVOID (no trigger levels apply
        to a holder). `PredictionsLog.append` adds id/timestamp/status. Never
        raises — a write fault must not break the panel re-render.

        v4.14.6.111 (Option A — per-model consensus accuracy): in ADDITION to the
        single representative display row above (UNCHANGED — it still drives the
        SELL TRIGGERS block via _find_buy_prediction_for_triggers), this now ALSO
        writes ONE accuracy row PER PARTICIPATING MODEL (source='consensus_vote',
        linked by `consensus_id`) so tm_discover's per-model + headline accuracy
        scores EVERY model's vote, not just the representative — fixing the
        under-recording where a 6-model consensus produced at most one scoreable
        row. The representative model is skipped in the per-model pass (no
        double-count); rows resolve via the SAME check_outcomes. Forward-only — no
        backfill of existing data.
        """
        try:
            plog = self._predictions_log()
            if plog is None:
                return
            votes = (consensus or {}).get('votes', []) or []
            committed = [v for v in votes
                         if (v.get('direction') or '').strip()
                         and not v.get('error') and not v.get('skipped')]
            if not committed:
                return
            from collections import Counter as _C
            winner = _C((v.get('direction') or '').upper()
                        for v in committed).most_common(1)[0][0]
            import tm_discover as _tmd
            cprice = self._latest_price(ticker)
            # v4.14.6.111: stable id linking this consensus's rows (and guarding a
            # re-render from duplicating them). Uses the rollup ts when present.
            try:
                from datetime import datetime as _dt, timezone as _tz
                _cts = (consensus or {}).get('ts') or _dt.now(_tz.utc).isoformat()
            except Exception:
                _cts = (consensus or {}).get('ts') or ''
            consensus_id = f"{(ticker or '').upper()}:{_cts}"

            # v4.14.6.111: full dedup — if this consensus was already recorded
            # (same consensus_id, ts-stable), don't re-write the representative or
            # the per-model rows. _write_refresh_prediction is called run-time on
            # consensus completion (not per render), so a genuine re-run yields a
            # NEW ts -> new id -> fresh rows; this only suppresses a re-call of the
            # SAME consensus from multiplying rows.
            try:
                _dup = any((r.get('consensus_id') == consensus_id)
                           for r in (plog.get_all() or [])[-500:])
            except Exception:
                _dup = False
            if _dup:
                return

            # ── (1) representative DISPLAY row — behavior UNCHANGED ──
            # SELL / AVOID winners carry no actionable trigger levels for an
            # existing holder — _find_buy_prediction_for_triggers excludes them
            # anyway, so don't write one. (The per-model accuracy pass below still
            # records every model's vote even in that case.)
            rep_model_key = None
            if winner not in ('SELL', 'AVOID'):
                for v in committed:
                    if (v.get('direction') or '').upper() != winner:
                        continue
                    resp = v.get('response') or ''
                    if not resp:
                        continue
                    try:
                        pred = _tmd.parse_prediction(resp, ticker,
                                                     current_price=cprice)
                    except Exception:
                        continue
                    if pred.get('target') is None or pred.get('stop') is None:
                        continue
                    # The consensus winner is the authoritative call;
                    # parse_prediction may normalise the per-vote direction
                    # (e.g. SELL→AVOID), so stamp the winner explicitly. Tag
                    # provenance + carry the v4.14 model/provider trailer.
                    pred['direction'] = winner
                    pred['source'] = 'refresh_triggers'
                    pred['path'] = path
                    pred['consensus_id'] = consensus_id
                    for k in ('model', 'provider_id', 'provider_preset',
                              'canonical_model', 'actual_provider',
                              'actual_model_string', 'lineup_version'):
                        if v.get(k) is not None:
                            pred[k] = v.get(k)
                    try:
                        plog.append(pred)
                        rep_model_key = (v.get('model') or '').strip().lower()
                        self.app._log(
                            f"[refresh-triggers] wrote prediction for {ticker}: "
                            f"{winner} target={pred.get('target')} "
                            f"stop={pred.get('stop')} (src=refresh_triggers)",
                            'muted')
                    except Exception as e:
                        self.app._log(
                            f"[refresh-triggers] prediction write failed for "
                            f"{ticker}: {type(e).__name__}: {e}", 'amber')
                    break  # one representative prediction per refresh

            # ── (2) per-model accuracy rows — NEW (forward-only) ──
            self._write_consensus_vote_predictions(
                ticker, path, committed, consensus_id, rep_model_key, cprice)
        except Exception:
            pass

    def _write_consensus_vote_predictions(self, ticker, path, committed,
                                          consensus_id, rep_model_key, cprice):
        """v4.14.6.111 (Option A): write ONE accuracy row per committed consensus
        vote (source='consensus_vote', linked by `consensus_id`) so the
        track-record computation scores EVERY participating model — not just the
        representative. Each row carries that model's OWN parsed direction +
        target/stop and resolves via the SAME check_outcomes resolver (BUY →
        target/stop axis, HOLD → hold axis; every row expires at timeframe_days,
        so none sit forever-open). SKIPS: the representative model (already
        written by the caller — no double-count), votes with no usable
        target+stop (unresolvable — would only pad the open pile), and any
        (consensus_id, model) already present (dedup across a re-render). The
        per-model rows feed the per-model matrix + headline (algo-exclusion only
        catches model=='Algorithm', so these AI rows are INCLUDED). Never raises.
        """
        # v4.14.6.111: delegates to the shared tm_consensus writer (extracted so
        # the fresh-buy path reuses the SAME machinery — no parallel impl). Owned-
        # position behavior is unchanged: same per-model rows, same consensus_id
        # dedup, same skip of the representative model (rep_model_key).
        try:
            plog = self._predictions_log()
            if plog is None:
                return
            import tm_consensus as _tc
            _log_fn = (self.app._log
                       if getattr(self, 'app', None) is not None else None)
            _tc.write_consensus_vote_predictions(
                plog, ticker, path, committed, consensus_id,
                source='consensus_vote', consensus_kind='owned',
                skip_model_key=rep_model_key, current_price=cprice,
                log_fn=_log_fn)
        except Exception:
            pass

    def _on_run_consensus(self, ticker):
        """Kick off a consensus scan for one ticker. Live-updates the card
        as each model finishes; saves the rollup to signals.jsonl when
        all models complete; refreshes the panel on completion.

        Concurrency: at most one consensus runs at a time. Clicking
        Re-run while one is in flight cancels the in-flight one first.
        """
        # Lazy import — avoids a hard dep at module load time
        try:
            import tm_consensus
        except Exception as e:
            self.app._log(f"Consensus runner not available: {e}", 'red')
            return

        mgr = self._mgr()
        if mgr is None:
            self.app._log("Consensus: data layer not ready.", 'red')
            return

        # Find the holding
        holding = next((h for h in mgr.holdings
                         if h.get('ticker', '').upper() == ticker.upper()),
                        None)
        if holding is None:
            self.app._log(f"Consensus: {ticker} not in holdings.", 'amber')
            return

        # Resolve consensus models from config; fall back to first
        # installed AI model if user hasn't configured any.
        cfg = getattr(self.app, 'cfg', None) or {}
        models = list(cfg.get('consensus_models') or [])
        # v4.14.5.14-ollama-purge-3a: the "no models configured → borrow one
        # local Ollama model" fallback was removed (Ollama is gone). An empty
        # `models` list is correct — the cloud ConsensusRunner fans out to the
        # configured providers (see the note below).
        # v4.14.5.5: gate on the UNIFIED AI-availability check (cloud OR
        # local), exactly like _on_refresh_triggers. Pre-fix this path
        # refused whenever consensus_models was empty and no Ollama
        # model was installed — even when 6 cloud providers were ready —
        # producing the misleading "Install Ollama" error. An empty
        # `models` list is fine: the router fans out to configured cloud
        # providers (same as Refresh Triggers does).
        try:
            import tm_top_ai_picker
            ai_available = tm_top_ai_picker.has_any_ai_available(
                self.app)
        except Exception:
            ai_available = bool(models)
        if not ai_available:
            # v4.14.6.80-ai-needed-explanations: VISIBLE inline explanation
            # (not just the amber log line below) so the click no longer looks
            # dead. Shown every time — it answers "why nothing happened".
            try:
                self.app._show_ai_needed_explanation(
                    "Consensus asks several AIs to weigh in on this holding.")
            except Exception:
                pass
            self.app._log(
                "Consensus refused: no AI available. Open API "
                "Providers to configure or enable one (cloud or "
                "local), or set Settings → AI → Consensus models.",
                'amber')
            return

        # Cancel any in-flight runner for any ticker. Holdings consensus
        # is single-threaded — one at a time keeps GPU usage predictable.
        # v4.14.5.14-portfolio-narration-and-honesty (Fix E): refuse-with-
        # explanation if a Refresh is already running on ANOTHER holding,
        # instead of the old silent kill-prior. (That card's buttons are
        # already disabled in this state; this is the backstop.) A re-click
        # on the SAME ticker still cancels + restarts (a deliberate re-run).
        existing = getattr(self, '_active_runner', None)
        if existing is not None:
            if getattr(existing, 'ticker', '').upper() != ticker.upper():
                _other = getattr(existing, 'ticker', '?')
                _bm = self.app._get_playbook_message(
                    'portfolio_refresh_already_running',
                    other_ticker=_other) or (
                    f"A refresh is already running on {_other} — wait for "
                    f"it to finish.")
                self.app._log(f"Consensus: {_bm}", 'amber')
                return
            try:
                existing.cancel()
            except Exception:
                pass
            self._active_runner = None
        # Fix A/B: fresh run — clear any prior failure note + stale narration.
        self._refresh_failure.pop(ticker.upper(), None)
        self._refresh_narration = ''

        # Path: v4.13.2 — per-holding path takes precedence over global.
        # v4.14.5.62-per-holding-tier: prefer the holding's OWN tier; when
        # unset, fall back to the safe default (moderate) — NOT the global
        # cfg['analysis_path'] (which could be 'lottery'/Speculative). Owned-
        # position analysis must never inherit the global analysis path.
        path = holding.get('path') or _tm_holdings_for_default.DEFAULT_PATH

        # Wire the runner. All callbacks marshal back to the UI thread
        # via root.after, since the runner spins up its own thread.
        signals_log = self._signals_log()
        prompt_builder = self._prompt_builder()
        if prompt_builder is None:
            self.app._log("Consensus: prompt builder not ready.", 'red')
            return

        root = self.parent.winfo_toplevel()

        # Track per-model card state during the run, so the card can
        # update in place as each model lands.
        run_state = {
            'votes': [],   # list of vote dicts as they arrive
            'errors': [],  # list of (model, error_msg)
            'pending': list(models),
            'current': None,
        }

        def on_model_start(model):
            run_state['current'] = model
            if model in run_state['pending']:
                run_state['pending'].remove(model)
            root.after(0, lambda: self._update_running_consensus(
                ticker, run_state))

        def on_model_done(model, vote):
            run_state['votes'].append(vote)
            run_state['current'] = None
            self.app._log(
                f"Consensus {ticker}: {model} → "
                f"{vote.get('direction','?')} ({vote.get('duration_sec',0):.1f}s)",
                'green')
            root.after(0, lambda: self._update_running_consensus(
                ticker, run_state))

        def on_model_error(model, msg):
            run_state['errors'].append((model, msg))
            run_state['current'] = None
            self.app._log(
                f"Consensus {ticker}: {model} ERROR — {msg[:80]}",
                'red')
            root.after(0, lambda: self._update_running_consensus(
                ticker, run_state))

        def on_all_done(consensus):
            self._active_runner = None
            self._refresh_narration = ''
            # v4.14.5.14-portfolio-narration-and-honesty (Fix A): count
            # REAL votes (ran, no error, has direction). The engine records
            # a skip entry per failed provider, so len(votes) is NOT a
            # success measure. Zero real -> honest red note on the card,
            # NOT a green "complete" + an un-updated/stale consensus.
            _allv = consensus.get('votes', []) or []
            _real = [v for v in _allv
                      if not v.get('error') and not v.get('skipped')
                      and v.get('direction')]
            if _allv and not _real:
                _msg = self.app._get_playbook_message(
                    'portfolio_refresh_all_voices_failed', ticker=ticker) or (
                    f"Couldn't reach any AI for {ticker} — all providers "
                    f"were busy or unavailable. Try again in a few minutes.")
                self._refresh_failure[ticker.upper()] = _msg
                self.app._log(f"Consensus {ticker}: {_msg}", 'amber')
            else:
                self._refresh_failure.pop(ticker.upper(), None)
                # Only stamp last_analyzed on a real result.
                try:
                    mgr.mark_analyzed(ticker)
                    mgr.save()
                except Exception:
                    pass
                verdict = self._summarize_done(consensus)
                # v4.14.5.14-refresh-triggers-writes-prediction: same write as
                # Refresh triggers — both run an owned-position consensus and
                # both expect the SELL TRIGGERS block to reflect the result.
                self._write_refresh_prediction(ticker, path, consensus)
                self.app._log(
                    f"Consensus {ticker}: complete — {verdict}", 'green')
                self._starve_escalate(ticker, _real, 'holdings')
            # Fix D: post-action coaching on the live path.
            self._fire_verify_observation(ticker, consensus)
            root.after(0, self.render)

        runner = tm_consensus.ConsensusRunner(
            ticker=ticker,
            holding=holding,
            models=models,
            path=path,
            prompt_builder=prompt_builder,
            signals_log=signals_log,
            on_model_start=on_model_start,
            on_model_done=on_model_done,
            on_model_error=on_model_error,
            on_all_done=on_all_done,
            log_callback=lambda m, t='muted': root.after(
                0, lambda mm=m, tt=t: self._refresh_log_cb(mm, tt)),
            predictions_log=self._predictions_log(),
            providers=(self.app._load_enabled_api_providers()  # v4.13.41
                       if hasattr(self.app, '_load_enabled_api_providers')
                       else []),
            inference_mode=(self.app._get_inference_settings()[0]  # v4.13.43
                            if hasattr(self.app, '_get_inference_settings')
                            else 'hybrid'),
            game_processes=(self.app._get_inference_settings()[1]  # v4.13.43
                            if hasattr(self.app, '_get_inference_settings')
                            else []),
            call_type='holdings_consensus',  # v4.13.56: smart router
            **self.app._consensus_weight_kwargs(models),
        )
        self._active_runner = runner

        self.app._log(
            f"Consensus {ticker}: running {len(models)} model(s) "
            f"({', '.join(models)}). Each takes ~10-30s.",
            'green')

        # Visually indicate the run started without re-rendering the whole
        # panel (avoids a flash). The next on_model_done will trigger a
        # narrower update via _update_running_consensus.
        self._begin_running_consensus(ticker, run_state)

        runner.start()

    def _on_refresh_triggers(self, ticker):
        """v4.13.31 / v4.15.0: Run a consensus on this ticker so the
        SELL TRIGGERS block gets new target/stop numbers from current
        model thinking. Same shape as _on_run_consensus.

        v4.15.0 (May 2026): switched from prompt_kind='fresh_buy'
        (asks "should someone enter a fresh BUY?") to the runner's
        default owned_position prompt (asks "given the user owns this at
        cost X, should he HOLD / TRIM / SELL / BUY MORE?"). The
        owned-position parser still extracts target + stop_loss
        fields, so the SELL TRIGGERS block continues to render —
        but the underlying question is now the right one for an
        owner. The reader at _find_buy_prediction_for_triggers was
        widened to accept BUY MORE / HOLD / TRIM (and legacy BUY)
        as valid trigger-bearing directions.

        On completion, calls self.render() to rebuild the panel; the
        trigger block then picks up the most recent prediction and
        the OUTDATED warning (if it was showing) goes away.
        """
        try:
            import tm_consensus
        except Exception as e:
            self.app._log(f"Refresh triggers: runner not available: {e}",
                          'red')
            return

        mgr = self._mgr()
        if mgr is None:
            self.app._log("Refresh triggers: data layer not ready.", 'red')
            return

        holding = next((h for h in mgr.holdings
                         if h.get('ticker', '').upper() == ticker.upper()),
                        None)
        if holding is None:
            self.app._log(f"Refresh triggers: {ticker} not in holdings.",
                          'amber')
            return

        cfg = getattr(self.app, 'cfg', None) or {}
        models = list(cfg.get('consensus_models') or [])
        # v4.14.5.14-ollama-purge-3a: the "no models configured → borrow one
        # local Ollama model" fallback was removed (Ollama is gone). An empty
        # `models` list is correct — the cloud ConsensusRunner fans out to the
        # configured providers (see the note below).
        # v4.15.0: gate on the unified AI-availability check (cloud
        # OR local), not just on local Ollama. User-initiated action —
        # surface via Teacher AI in addition to the amber log
        # fallback.
        try:
            import tm_top_ai_picker
            ai_available = tm_top_ai_picker.has_any_ai_available(
                self.app)
        except Exception:
            ai_available = bool(models)
        if not ai_available:
            try:
                import tm_teacher_intercept as _tm_ic
                _tm_ic.emit_system_event(
                    'holding_refresh_no_ai_available', app=self.app)
            except Exception:
                pass
            self.app._log(
                "Refresh triggers refused: no AI available. "
                "Open API Providers to configure or enable one.",
                'amber')
            return

        # v4.14.5.14-portfolio-narration-and-honesty (Fix E): refuse-with-
        # explanation if a Refresh is already running on ANOTHER holding
        # (the old behaviour silently cancelled it). Same-ticker re-click
        # still cancels + restarts.
        existing = getattr(self, '_active_runner', None)
        if existing is not None:
            if getattr(existing, 'ticker', '').upper() != ticker.upper():
                _other = getattr(existing, 'ticker', '?')
                _bm = self.app._get_playbook_message(
                    'portfolio_refresh_already_running',
                    other_ticker=_other) or (
                    f"A refresh is already running on {_other} — wait for "
                    f"it to finish.")
                self.app._log(f"Refresh triggers: {_bm}", 'amber')
                return
            try: existing.cancel()
            except Exception: pass
            self._active_runner = None
        # Fix A/B: fresh run — clear any prior failure note + stale narration.
        self._refresh_failure.pop(ticker.upper(), None)
        self._refresh_narration = ''

        # v4.14.5.62-per-holding-tier: prefer the holding's OWN tier; when
        # unset, fall back to the safe default (moderate) — NOT the global
        # cfg['analysis_path'] (which could be 'lottery'/Speculative). Owned-
        # position analysis must never inherit the global analysis path.
        path = holding.get('path') or _tm_holdings_for_default.DEFAULT_PATH

        signals_log = self._signals_log()
        prompt_builder = self._prompt_builder()
        if prompt_builder is None:
            self.app._log("Refresh triggers: prompt builder not ready.",
                          'red')
            return

        # v4.15.0: pass the REAL holding (with shares + cost basis) so
        # the owned_position prompt can render the user's actual position
        # into the prompt and ask the right question — HOLD / TRIM /
        # SELL / BUY MORE — instead of the fresh-entry question that
        # was producing AVOID verdicts on held stocks (May 2026 bug).
        # The synthetic holding from v4.13.31 was paired with
        # prompt_kind='fresh_buy', which is wrong for owned positions.

        root = self.parent.winfo_toplevel()

        run_state = {
            'votes': [],
            'errors': [],
            'pending': list(models),
            'current': None,
        }

        def on_model_start(model):
            run_state['current'] = model
            if model in run_state['pending']:
                run_state['pending'].remove(model)
            root.after(0, lambda: self._update_running_consensus(
                ticker, run_state))

        def on_model_done(model, vote):
            run_state['votes'].append(vote)
            run_state['current'] = None
            self.app._log(
                f"Refresh {ticker}: {model} -> "
                f"{vote.get('direction','?')} target={vote.get('target','?')} "
                f"({vote.get('duration_sec',0):.1f}s)",
                'green')
            root.after(0, lambda: self._update_running_consensus(
                ticker, run_state))

        def on_model_error(model, msg):
            run_state['errors'].append((model, msg))
            run_state['current'] = None
            self.app._log(
                f"Refresh {ticker}: {model} ERROR -- {msg[:80]}",
                'red')
            root.after(0, lambda: self._update_running_consensus(
                ticker, run_state))

        def on_all_done(consensus):
            self._active_runner = None
            self._refresh_narration = ''
            # v4.14.5.14-portfolio-narration-and-honesty (Fix A): honest
            # zero-real-votes branch (see _on_run_consensus for rationale).
            _allv = consensus.get('votes', []) or []
            _real = [v for v in _allv
                      if not v.get('error') and not v.get('skipped')
                      and v.get('direction')]
            if _allv and not _real:
                _msg = self.app._get_playbook_message(
                    'portfolio_refresh_all_voices_failed', ticker=ticker) or (
                    f"Couldn't reach any AI for {ticker} — all providers "
                    f"were busy or unavailable. Try again in a few minutes.")
                self._refresh_failure[ticker.upper()] = _msg
                self.app._log(f"Refresh {ticker}: {_msg}", 'amber')
            else:
                self._refresh_failure.pop(ticker.upper(), None)
                try:
                    mgr.mark_analyzed(ticker)
                    mgr.save()
                except Exception:
                    pass
                verdict = self._summarize_done(consensus)
                # v4.14.5.14-refresh-triggers-writes-prediction: persist the
                # fresh verdict as a prediction so the SELL TRIGGERS block
                # actually updates and the OUTDATED warning clears.
                self._write_refresh_prediction(ticker, path, consensus)
                self.app._log(
                    f"Refresh {ticker}: complete -- {verdict}. Triggers "
                    f"updated from new {verdict.split()[0] if verdict else ''} "
                    f"prediction.", 'green')
                self._starve_escalate(ticker, _real, 'holdings')
            # Fix D: post-action coaching on the live path.
            self._fire_verify_observation(ticker, consensus)
            root.after(0, self.render)

        runner = tm_consensus.ConsensusRunner(
            ticker=ticker,
            holding=holding,
            models=models,
            path=path,
            prompt_builder=prompt_builder,
            signals_log=signals_log,
            on_model_start=on_model_start,
            on_model_done=on_model_done,
            on_model_error=on_model_error,
            on_all_done=on_all_done,
            log_callback=lambda m, t='muted': root.after(
                0, lambda mm=m, tt=t: self._refresh_log_cb(mm, tt)),
            predictions_log=self._predictions_log(),
            prompt_kind='owned_position',
            providers=(self.app._load_enabled_api_providers()  # v4.13.41
                       if hasattr(self.app, '_load_enabled_api_providers')
                       else []),
            inference_mode=(self.app._get_inference_settings()[0]  # v4.13.43
                            if hasattr(self.app, '_get_inference_settings')
                            else 'hybrid'),
            game_processes=(self.app._get_inference_settings()[1]  # v4.13.43
                            if hasattr(self.app, '_get_inference_settings')
                            else []),
            call_type='holdings_consensus',  # v4.13.56: smart router
            **self.app._consensus_weight_kwargs(models),
        )
        self._active_runner = runner

        self.app._log(
            f"Refresh triggers {ticker}: running {len(models)} model(s) "
            f"in owned_position mode. Each takes ~10-30s. New target/"
            f"stop numbers will replace the SELL TRIGGERS block when done.",
            'green')

        self._begin_running_consensus(ticker, run_state)

        runner.start()

    def _refresh_log_cb(self, m, t='muted'):
        """v4.14.5.14-portfolio-narration-and-honesty (Fix B): the Refresh
        ConsensusRunner's log_callback. Mirrors every engine line to the
        activity log (unchanged) AND narrates recognized self-fix events
        ("An AI was busy - trying another...") on the running holding's
        card via the engine-generic _translate_lookup_progress (shared
        with Look Up / Recommend). Runs on the Tk main thread (callers
        wrap it in root.after), so self.render() is safe. Never raises."""
        try:
            _fr = self.app._translate_lookup_progress(m)
        except Exception:
            _fr = None
        if _fr:
            self._refresh_narration = _fr
            try:
                self.render()
            except Exception:
                pass
        try:
            self.app._log(m, t)
        except Exception:
            pass

    def _fire_verify_observation(self, ticker, consensus):
        """v4.14.5.14-portfolio-narration-and-honesty (Fix D): fire Teacher
        AI post-action coaching for a holding Refresh on the LIVE path. An
        owned-position refresh has 'verify' semantics — the registered
        action whose observations (one_voice_only, voices_skipped_during_
        verify, consensus_split_no_majority) are the relevant ones — so we
        reuse action_id 'verify' (the brief's 'refresh_triggers' has no
        observations and would be inert). try/except: never breaks
        completion."""
        try:
            import tm_teacher_intercept as _tm_ic
            _allv = consensus.get('votes', []) or []
            _bd = {}
            for _v in _allv:
                _m = _v.get('canonical_model') or _v.get('model') or '?'
                _d = (_v.get('direction') or '').upper()
                if _d:
                    _bd[_m] = _d
            _skipped = [(_v.get('canonical_model') or _v.get('model') or '?')
                        for _v in _allv
                        if not (_v.get('direction') or '').strip()]
            _winner = ''
            if _bd:
                from collections import Counter as _C
                _winner = _C(_bd.values()).most_common(1)[0][0]
            _tm_ic.observe_action(self.app, 'verify', {
                'direction': (_winner or 'MIXED'),
                'from_queue': False,
                'voices_skipped': _skipped,
                'voice_breakdown': _bd,
            })
        except Exception as _e:
            try:
                self.app._log(
                    f"[portfolio] refresh observe_action warning: {_e}",
                    'muted')
            except Exception:
                pass

    def _summarize_done(self, consensus):
        """One-liner verdict for the activity feed.

        v4.14.5.19-accuracy-weighted-consensus: when the runner attached
        a 'tally' dict (weighting flag on), append a weighted line that
        shows the Wilson-lower-bound-weighted winner + a data-maturity
        caveat. Raw tally stays (honest about vote counts); the weighted
        line is the explainability surface. When the flag is off or
        weighted_winner == raw_winner and n_mature is 0 (cold-start),
        the weighted line is suppressed -- nothing useful to show yet.
        """
        votes = [v for v in consensus.get('votes', []) if v.get('direction')]
        if not votes:
            return "no models committed"
        from collections import Counter
        counts = Counter(v['direction'] for v in votes)
        winner, n = counts.most_common(1)[0]
        raw = f"{n} of {len(votes)} say {winner}"
        tally = consensus.get('tally') or {}
        if tally.get('weighted_enabled'):
            ww = tally.get('weighted_winner') or winner
            wn = tally.get('weighted_winner_sum') or 0.0
            wt = tally.get('weighted_total_sum') or 0.0
            n_mature = tally.get('n_mature') or 0
            n_total = tally.get('raw_total') or len(votes)
            if n_mature == 0:
                # All voters at neutral 5 -- weighted == flat, nothing to add.
                return raw
            tag = ('accuracy-weighted'
                    if n_mature >= n_total
                    else f'accuracy-weighted, limited data ({n_mature}/{n_total} models with history)')
            return (f"{raw} | {tag}: "
                    f"{ww} (weighted {wn:.1f} of {wt:.1f})")
        return raw

    def _begin_running_consensus(self, ticker, run_state):
        """Mark the IONQ card's consensus block as 'running'. We don't
        re-render the whole panel here — that would flash the screen
        every model. Just mutate the card we already have."""
        # For the tracer-bullet, just trigger a render. The narrower
        # in-place update lives in _update_running_consensus.
        self.render()

    def _update_running_consensus(self, ticker, run_state):
        """Called whenever a model finishes (or starts). For now this
        triggers a full re-render of the panel; the per-card live
        update is a polish item we can add later if it's useful."""
        try:
            self.render()
        except Exception:
            pass

    def _on_view_reasoning(self, ticker):
        # Show the most recent consensus's full per-model responses.
        signals_log = self._signals_log()
        consensus = _read_latest_consensus(signals_log, ticker) if signals_log else None
        if consensus is None:
            messagebox.showinfo("No consensus",
                                 f"No saved consensus found for {ticker}.")
            return
        self._open_reasoning_window(ticker, consensus)

    def _open_reasoning_window(self, ticker, consensus,
                                 source_label=None):
        """v4.14.5.93-recommend-reasoning: thin wrapper around the
        shared tm_reasoning_view.open_reasoning_window helper. Behaviour
        is byte-for-byte identical to the pre-refactor implementation
        when called WITHOUT source_label (Portfolio's default — its
        "View reasoning" button doesn't pass one), so Portfolio's
        existing window looks the same. New keyword argument added for
        Recommend's "Why?" button, which passes an honest one-liner
        (Layer-2 3-model background vs 7-model live Verify) to render
        as a sub-header.
        """
        try:
            import tm_reasoning_view as _rv
        except Exception:
            return  # shared module not present → silent no-op (rare)
        _rv.open_reasoning_window(
            self.parent, ticker, consensus,
            c=self.c, space=self.space, fonts=self.fonts,
            source_label=source_label,
            humanize_age=_humanize_age,
        )

    # ─── Helpers ───────────────────────────────────────────────────

    def _mgr(self):
        state = getattr(self.app, '_holdings_state', None)
        return state.get('mgr') if state else None

    def _signals_log(self):
        state = getattr(self.app, '_holdings_state', None)
        return state.get('log') if state else None

    def _cache(self):
        state = getattr(self.app, '_holdings_state', None)
        return state.get('cache') if state else None

    def _prompt_builder(self):
        state = getattr(self.app, '_holdings_state', None)
        return state.get('builder') if state else None

    def _predictions_log(self):
        state = getattr(self.app, '_holdings_state', None)
        return state.get('predictions_log') if state else None

    def refresh_card_prices(self):
        """v4.14.6.48-portfolio-price-sync: in-place refresh of per-card
        prices + P&L + the summary "in holdings" total. Text-only —
        never creates or destroys widgets — so scroll position is
        preserved. Mirrors the header ticker's 15s cadence so the
        portfolio cards stay visually in sync with the header bar
        (was a drift bug pre-v4.14.6.48: header re-rendered every 15s
        via _render_portfolio_ticker, but the portfolio cards only
        rebuilt on user actions, so they showed stale prices).

        Formatting MUST match _build_tradable_card byte-for-byte so
        the card doesn't visually shift between a render and a
        refresh. Per-ticker work is wrapped in try/except so one bad
        row never blocks the others. Tickers whose latest quote is
        None keep their last-known displayed text (don't blank).
        Widgets that have been destroyed (winfo_exists() False) are
        silently dropped from the map.
        """
        labels_map = getattr(self, '_price_labels', None)
        if not labels_map:
            # also still refresh the summary total even if no cards
            self._refresh_holdings_total()
            return
        dead = []
        for ticker, refs in list(labels_map.items()):
            try:
                pos_lbl = refs.get('pos_lbl')
                pnl_lbl = refs.get('pnl_lbl')
                # Drop refs to destroyed widgets.
                try:
                    if pos_lbl is None or not pos_lbl.winfo_exists():
                        dead.append(ticker)
                        continue
                    if pnl_lbl is not None and not pnl_lbl.winfo_exists():
                        # treat as dead — out of sync with the card
                        dead.append(ticker)
                        continue
                except Exception:
                    dead.append(ticker)
                    continue
                current_price = self._latest_price(ticker)
                if current_price is None:
                    # Keep last-known displayed text — do NOT blank.
                    continue
                buy_price = refs.get('buy_price') or 0
                shares = refs.get('shares') or 0
                if buy_price:
                    pnl = (current_price - buy_price) * shares
                    pnl_pct = (((current_price - buy_price) / buy_price)
                               * 100 if buy_price else 0)
                    sign = '+' if pnl >= 0 else ''
                    pnl_color = refs['green'] if pnl >= 0 else refs['red']
                    pos_text = (f"{_format_shares(shares)} sh @ "
                                f"{_format_money(buy_price)} "
                                f"→ {_format_money(current_price)}")
                    try:
                        pos_lbl.config(text=pos_text)
                    except Exception:
                        pass
                    if pnl_lbl is not None:
                        try:
                            pnl_lbl.config(
                                text=(f"  {sign}{_format_money(pnl)} "
                                      f"({sign}{pnl_pct:.1f}%)"),
                                fg=pnl_color)
                        except Exception:
                            pass
                else:
                    # No buy_price → no P&L; still show live price.
                    try:
                        pos_lbl.config(
                            text=(f"{_format_shares(shares)} sh "
                                  f"→ {_format_money(current_price)}"))
                    except Exception:
                        pass
            except Exception:
                # Per-ticker safety net — never block other rows.
                continue
        for t in dead:
            try:
                labels_map.pop(t, None)
            except Exception:
                pass
        # Recompute the "$NNN in holdings" total using live market value.
        self._refresh_holdings_total()

    def _refresh_holdings_total(self):
        """v4.14.6.48-portfolio-price-sync: recompute the summary card's
        "{cash} cash · {N} in holdings" line using live market value
        (sum of price*shares for tradable holdings whose quote is
        known). If NO tradable has a live quote, leave the label as-is
        (don't downgrade to cost basis mid-session). Text-only update.
        """
        lbl = getattr(self, '_holdings_total_lbl', None)
        if lbl is None:
            return
        try:
            if not lbl.winfo_exists():
                self._holdings_total_lbl = None
                return
        except Exception:
            self._holdings_total_lbl = None
            return
        try:
            cash = float(getattr(self, '_holdings_total_cash', 0) or 0)
        except Exception:
            cash = 0.0
        labels_map = getattr(self, '_price_labels', {}) or {}
        market_value = 0.0
        any_priced = False
        for ticker, refs in labels_map.items():
            try:
                shares = refs.get('shares') or 0
                price = self._latest_price(ticker)
                if price is None:
                    continue
                market_value += float(price) * float(shares)
                any_priced = True
            except Exception:
                continue
        if not any_priced:
            return  # keep last-known text
        try:
            lbl.config(text=(f"{_format_money(cash)} cash  ·  "
                             f"{_format_money(market_value)} in holdings"))
        except Exception:
            pass

    def _latest_price(self, ticker):
        """Return the current price for the owned-position card refresh.

        v4.14.6.61-portfolio-quote: routes through the dedicated portfolio
        quote helper (tired_market.fetch_portfolio_quote) which uses a
        keyless-first cascade (Yahoo → Stooq → optional Finnhub →
        cache.db last-known) and is INSULATED from the shared
        cache.quote() that the universe daily_bars fill throttles.
        Fail-safe: any helper error falls back to the legacy shared
        cache so the card never goes blank during a transient hiccup.
        """
        try:
            import tired_market as _tm
            q = _tm.fetch_portfolio_quote(ticker)
            if isinstance(q, dict):
                p = q.get('price')
                if p is not None:
                    return p
        except Exception:
            pass
        # Fail-safe: legacy shared-cache path (last-known, may be stale
        # during a fill but better than blank).
        cache = self._cache()
        if cache is None:
            return None
        try:
            quote = cache.quote(ticker)
            if isinstance(quote, dict):
                return quote.get('price')
        except Exception:
            return None
        return None

    def is_sell_trigger_dated(self, prediction, ticker, now):
        """v4.14.5.14-sell-trigger-dated-rule (2026-05-26): decide whether
        the dim SELL TRIGGERS tag should read "DATED — <reason>".

        Thesis-based, not clock-based. Returns (True, reason) when ANY of:
          1. "timeframe expired" — the prediction's stated timeframe
             (timestamp + timeframe_days) is now in the past.
          2. "target reached" / "stop reached" — the current price has
             crossed the prediction's target (sell-up) or stop (sell-down).
          3. "new filing" — a SEC filing for this ticker is dated AFTER the
             prediction's timestamp.
        Otherwise returns (False, None), regardless of how old the
        prediction is.

        Condition 3 uses SEC filings ONLY this version: the news and
        earnings sources don't yet carry a reliable publish-date, so
        including them would fire the tag constantly (see DECISIONS
        2026-05-26). They are a parked follow-up.

        Never raises — any fault falls through to the next condition and
        ultimately to (False, None), so a data fault silently leaves the
        tag off rather than crashing the holding card. Does NOT affect the
        loud red "OUTDATED — consensus now says X" consensus-disagreement
        warning, which is computed separately in _build_plan_block.
        """
        # Condition 1: stated timeframe expired.
        try:
            tf_days = prediction.get('timeframe_days')
            generated = _parse_iso(prediction.get('timestamp', ''))
            if tf_days and generated is not None:
                if generated + timedelta(days=int(tf_days)) < now:
                    return True, "timeframe expired"
        except Exception:
            pass

        # Condition 2: a sell trigger price has been hit.
        try:
            current_price = self._latest_price(ticker)
            if current_price is not None:
                current_price = float(current_price)
                target = prediction.get('target')
                stop = prediction.get('stop')
                if target is not None and current_price >= float(target):
                    return True, "target reached"
                if stop is not None and current_price <= float(stop):
                    return True, "stop reached"
        except Exception:
            pass

        # Condition 3: a new SEC filing arrived after the prediction.
        try:
            generated = _parse_iso(prediction.get('timestamp', ''))
            if generated is not None:
                import tm_cache
                for row in (tm_cache.get_filings(ticker) or []):
                    try:
                        fdate = _parse_iso(row['filing_date'])
                    except Exception:
                        fdate = None
                    if fdate is not None and fdate > generated:
                        return True, "new filing"
        except Exception:
            pass

        return False, None

    def is_consensus_outdated(self, prediction, ticker):
        """v4.14.5.14-compact-holding-row (2026-05-26): extracted from
        _build_plan_block's superseded-check so BOTH the loud expanded
        "OUTDATED — consensus now says X" warning AND the new collapsed-row
        state badge compute it from one place.

        Returns (is_outdated, winner_direction). True when a consensus run
        NEWER than `prediction` exists whose winning vote is NOT a BUY-family
        direction — i.e. the model that proposed the prediction's BUY-oriented
        triggers no longer agrees, so those trigger numbers are misleading.
        Never raises — any fault → (False, '').
        """
        try:
            from datetime import datetime as _dt
            if not hasattr(self.app, '_read_latest_consensus_any'):
                return False, ''
            try:
                fresh = self.app._read_latest_consensus_any(ticker)
            except Exception:
                fresh = None
            if not fresh:
                return False, ''
            fresh_ts_str = fresh.get('ts', '')
            fresh_dt = _dt.fromisoformat(fresh_ts_str) if fresh_ts_str else None
            pred_ts_str = (prediction or {}).get('timestamp', '')
            pred_dt = _dt.fromisoformat(pred_ts_str) if pred_ts_str else None
            if fresh_dt is None or pred_dt is None or not (fresh_dt > pred_dt):
                return False, ''
            votes = fresh.get('votes', []) or []
            committed = [v for v in votes
                         if (v.get('direction') or '').strip()]
            if not committed:
                return False, ''
            from collections import Counter as _C
            counts = _C(str(v.get('direction', '')).upper() for v in committed)
            winner = counts.most_common(1)[0][0]
            # v4.14.6.23-outdated-fix: real same-direction comparison.
            # Pre-fix this was a hardcoded BUY-family literal that treated
            # every non-BUY winner (HOLD, TRIM, AVOID, SELL) as a thesis
            # break — including the HOLD-vs-HOLD agreement case, which
            # is why a same-day HOLD rerun still rendered OUTDATED.
            # _find_buy_prediction_for_triggers (line 308-362) accepts
            # HOLD/TRIM predictions as the trigger source, so the
            # OUTDATED check must compare prediction direction against
            # winner — not hardcode BUY as the only "agreement."
            # BUY family (BUY / BUY MORE / BUYMORE) collapses to one
            # bucket so a BUY → BUY MORE rerun reads as agreement.
            # Defensive: an empty/missing prediction.direction will
            # never equal a real winner family, so the function returns
            # OUTDATED — that's intentional ("no direction recorded →
            # let the fresh consensus rule the badge").
            pred_dir = _direction_family(
                (prediction or {}).get('direction'))
            winner_fam = _direction_family(winner)
            if winner_fam != pred_dir:
                return True, winner
            return False, ''
        except Exception:
            return False, ''

    def compute_holding_state(self, holding, prediction, ticker, now):
        """v4.14.5.14-compact-holding-row (2026-05-26): one at-a-glance state
        per holding for the collapsed-row badge. Returns
        (state_code, display_text, color_key) where color_key is a key into
        self.c (or None for no badge). The FIRST matching rule wins:

            1. TARGET_HIT / STOP_HIT — current price crossed the prediction's
               target/stop (most urgent; needs action).  green / red.
            2. OUTDATED — a newer consensus disagrees with the BUY thesis.
               red (rendered non-bold so it reads softer than a price hit).
            3. DATED — thesis-level staleness via is_sell_trigger_dated
               (timeframe expired / trigger price / new filing).  amber.
            5. FRESH — nothing matched → no badge (clean row).

        Note: price-hit (rule 1) outranks DATED even though
        is_sell_trigger_dated also watches target/stop, so a live trigger
        shows TARGET/STOP REACHED rather than DATED. Never raises →
        ('FRESH', '', None) on any fault.
        """
        try:
            # Priority 1: a sell trigger price has been hit.
            try:
                price = self._latest_price(ticker)
                if price is not None and prediction:
                    price = float(price)
                    target = prediction.get('target')
                    stop = prediction.get('stop')
                    if target is not None and price >= float(target):
                        return 'TARGET_HIT', 'TARGET REACHED', 'green'
                    if stop is not None and price <= float(stop):
                        return 'STOP_HIT', 'STOP REACHED', 'red'
            except Exception:
                pass

            # Priority 2: latest consensus disagrees with the BUY thesis.
            try:
                outdated, winner = self.is_consensus_outdated(prediction, ticker)
                if outdated:
                    return 'OUTDATED', f'OUTDATED — {winner}', 'red'
            except Exception:
                pass

            # Priority 3/4: thesis-level staleness (the 2026-05-26 rule).
            try:
                if prediction:
                    is_dated, reason = self.is_sell_trigger_dated(
                        prediction, ticker, now)
                    if is_dated:
                        return 'DATED', f'DATED — {reason}', 'amber'
            except Exception:
                pass
        except Exception:
            pass

        return 'FRESH', '', None
