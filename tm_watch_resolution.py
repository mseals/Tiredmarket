"""tm_watch_resolution — Phase 2 WATCH classifier.

v4.14.5.95-watch-phase2 (2026-06-11). Pure functions, no I/O at the
classifier core, no AI calls anywhere. The point of this module:
recognize from daily-bar history whether a WATCH'd thesis has ALREADY
played out while the app was closed — so the system doesn't re-offer a
spent setup as if it's fresh.

Public API
----------
classify_watch_progress(pred, history, fallback_levels=None) -> dict
    Pure classifier. Returns one of:
        {'state': 'waiting',           'entry_hit_date': None,    'target_hit_date': None}
        {'state': 'entry_actionable',  'entry_hit_date': <iso>,   'target_hit_date': None}
        {'state': 'resolved',          'entry_hit_date': <iso>,   'target_hit_date': <iso>}
        {'state': 'unknown',           'entry_hit_date': None,    'target_hit_date': None}
    `unknown` means we can't classify — usually no entry_high / target
    on the WATCH and no inheritable Layer-1 BUY thesis. Callers must
    treat 'unknown' as "leave on the Watching list as raw prose; do
    NOT write watch_resolved."

find_inherited_levels(plog, ticker, path) -> dict | None
    For WATCH preds missing structured BUY_ZONE / target, look up the
    most-recent prior open BUY for (ticker, path) and return its
    {'buy_zone_low','buy_zone_high','target'}. None if no prior BUY.

fetch_history_for(ticker, since_iso) -> list[dict]
    Read local daily_bars (tm_cache) — NO network call. Returns
    [{'date','open','high','low','close'}, ...] ordered by date ASC
    starting at since_iso (YYYY-MM-DD). [] on any error.

format_state_text(state_dict, ticker_levels, current_price) -> str
    Honest one-line display string for the Watching list. Phrases
    case (2) as "entry reached on <date>, now <price>", never "buy
    now."

Design note — HIGH/LOW, NOT close
---------------------------------
PredictionsLog.check_outcomes uses daily CLOSE deliberately to avoid
crediting wick spikes as accuracy wins. Phase 2's purpose is the
opposite: detect that the move HAPPENED so the user isn't re-offered
a spent setup. A wick low into the entry zone IS evidence the named
pullback occurred — the user couldn't have caught it (the
already-closed-app architecture limit), but we know it happened, so
we refuse to re-offer. **Use HIGH/LOW deliberately here.** The bias is
toward marking resolved — safer to retire a maybe-spent setup than to
re-offer a definitely-spent one. Do not "fix" this to match
check_outcomes.
"""

from __future__ import annotations


# ── Part A: the classifier (pure) ───────────────────────────────────


def classify_watch_progress(pred: dict, history: list,
                              fallback_levels: dict | None = None
                              ) -> dict:
    """Pure classifier. See module docstring.

    pred: prediction record dict (one row from predictions.jsonl).
    history: list of daily candle dicts, each carrying at minimum
        'date' (ISO YYYY-MM-DD), 'high', 'low'. Ordered any way; we
        date-filter against the WATCH timestamp ourselves.
    fallback_levels: optional dict {'buy_zone_low', 'buy_zone_high',
        'target'} from a prior BUY thesis (when the WATCH's own row is
        missing structured levels). Caller is expected to fetch this
        via find_inherited_levels(); we read it defensively here.

    Returns a 3-key dict — never raises.
    """
    none_result = {'state': 'unknown',
                   'entry_hit_date': None,
                   'target_hit_date': None}
    try:
        if not isinstance(pred, dict):
            return none_result

        # ── Resolve entry_high + target with fallback ─────────────
        def _f(name):
            v = pred.get(name)
            if v in (None, '', 0):
                fb = fallback_levels or {}
                v = fb.get(name)
            try:
                return float(v) if v not in (None, '') else None
            except (TypeError, ValueError):
                return None
        entry_hi = _f('buy_zone_high')
        target = _f('target')
        # entry_high is the upper bound of the entry ZONE — we want
        # "price was at OR BELOW entry_hi" to flag the pullback. If
        # only buy_zone_low is present, treat that as entry_hi too
        # (degenerate, single-point entry); without either we can't
        # classify.
        if entry_hi is None:
            entry_hi = _f('buy_zone_low')
        if entry_hi is None or target is None:
            return none_result
        if entry_hi <= 0 or target <= 0:
            return none_result

        # ── Resolve the made_at cutoff (skip candles BEFORE it) ───
        made_at_iso = (pred.get('timestamp') or '').strip()
        # Use the YYYY-MM-DD prefix for string comparison against the
        # candle 'date' field (which is also ISO date-only). This
        # avoids re-parsing the full ISO datetime per candle.
        made_at_date = made_at_iso[:10] if made_at_iso else ''
        if not made_at_date:
            return none_result

        # ── Walk candles (in order) and find first entry/target hit
        try:
            sorted_history = sorted(
                history or [],
                key=lambda c: str(c.get('date') or ''))
        except Exception:
            sorted_history = list(history or [])
        entry_hit_date = None
        target_hit_date = None
        for c in sorted_history:
            try:
                cdate = str(c.get('date') or '')
                if not cdate or cdate < made_at_date:
                    continue
                lo = c.get('low')
                hi = c.get('high')
                lo = float(lo) if lo not in (None, '') else None
                hi = float(hi) if hi not in (None, '') else None
            except (TypeError, ValueError):
                continue
            if entry_hit_date is None and lo is not None and lo <= entry_hi:
                entry_hit_date = cdate
            if target_hit_date is None and hi is not None and hi >= target:
                target_hit_date = cdate
            if entry_hit_date is not None and target_hit_date is not None:
                break

        # ── Classify ───────────────────────────────────────────────
        if entry_hit_date is None and target_hit_date is None:
            return {'state': 'waiting',
                    'entry_hit_date': None,
                    'target_hit_date': None}
        if entry_hit_date is not None and target_hit_date is None:
            return {'state': 'entry_actionable',
                    'entry_hit_date': entry_hit_date,
                    'target_hit_date': None}
        # Both set — the move played out (target hit on the same day
        # as entry OR after; either way the thesis fired since the
        # WATCH was set). Bias toward marking resolved: target hit
        # BEFORE entry is rare on a "wait for pullback" thesis, but
        # it still means the price made the projected move during
        # the WATCH window — re-offering the old entry would mislead.
        return {'state': 'resolved',
                'entry_hit_date': entry_hit_date,
                'target_hit_date': target_hit_date}
    except Exception:
        return none_result


# ── Part B: level inheritance ───────────────────────────────────────


def find_inherited_levels(plog, ticker: str, path: str) -> dict | None:
    """For a WATCH lacking structured levels, return the most-recent
    prior OPEN BUY's {buy_zone_low, buy_zone_high, target} for the
    same (ticker, path). Returns None if no such BUY exists.

    Reads plog only — no AI call, no network. Defensive: any error
    returns None.
    """
    try:
        allrecs = plog.get_all()
    except Exception:
        return None
    tk_u = (ticker or '').upper()
    path_s = (path or '').strip()
    latest_ts = ''
    latest = None
    for r in allrecs:
        try:
            if (r.get('ticker') or '').upper() != tk_u:
                continue
            if (r.get('path') or '').strip() != path_s:
                continue
            if (r.get('direction') or '').upper() != 'BUY':
                continue
            if r.get('status') not in (None, '', 'open'):
                continue
            ts = r.get('timestamp', '') or ''
            if ts > latest_ts:
                latest_ts = ts
                latest = r
        except Exception:
            continue
    if latest is None:
        return None
    out = {
        'buy_zone_low': latest.get('buy_zone_low'),
        'buy_zone_high': latest.get('buy_zone_high'),
        'target': latest.get('target'),
    }
    # Only useful if at least one of (buy_zone_high/buy_zone_low) AND
    # target is present.
    has_entry = out['buy_zone_high'] or out['buy_zone_low']
    if not has_entry or not out['target']:
        return None
    return out


# ── Part C: history accessor (local DB; no network) ─────────────────


def fetch_history_for(ticker: str, since_iso: str) -> list:
    """Read local daily_bars rows for ticker on/after since_iso. Returns
    a list of dicts [{'date','open','high','low','close'}, ...] ordered
    by date ASC. NO network call — pulls from the already-cached
    tm_cache.daily_bars table. Returns [] on any error or empty cache.

    since_iso: ISO datetime or date string; we slice to the first 10
    chars (YYYY-MM-DD) to bound the SQL.
    """
    try:
        import tm_cache
    except Exception:
        return []
    if not ticker:
        return []
    start_date = (since_iso or '')[:10] or None
    try:
        rows = tm_cache.get_daily_bars(
            ticker.upper(), start_date=start_date)
    except Exception:
        return []
    out = []
    for row in rows or []:
        try:
            # sqlite3.Row supports dict-style access by column name.
            out.append({
                'date': row['date'],
                'open': row['open'],
                'high': row['high'],
                'low': row['low'],
                'close': row['close'],
            })
        except Exception:
            continue
    return out


# ── Part D: honest display strings ──────────────────────────────────


def format_state_text(state_dict: dict,
                       ticker_levels: dict | None = None,
                       current_price: float | None = None) -> str:
    """One-line display string for the Watching list. Phrases case (2)
    honestly: "entry reached on <date>, now <price> — re-check if still
    in play," NEVER "buy now." Case (3) explains the setup played out.
    Defensive — never raises; on any error returns ''.
    """
    try:
        st = (state_dict or {}).get('state', '')
        levels = ticker_levels or {}
        ent_lo = levels.get('buy_zone_low')
        ent_hi = levels.get('buy_zone_high')
        target = levels.get('target')

        def _money(v):
            try:
                v = float(v)
            except (TypeError, ValueError):
                return None
            if v <= 0:
                return None
            return f"${v:g}" if v < 10 else f"${v:.2f}"

        def _entry_str():
            lo = _money(ent_lo)
            hi = _money(ent_hi)
            if lo and hi and lo != hi:
                return f"{lo}–{hi}"
            return hi or lo or "?"

        if st == 'waiting':
            cp = _money(current_price)
            return (f"waiting for entry {_entry_str()}"
                    + (f" (now {cp})" if cp else ""))
        if st == 'entry_actionable':
            hd = state_dict.get('entry_hit_date') or '?'
            cp = _money(current_price)
            return (f"entry zone reached on {hd}"
                    + (f"; now {cp}" if cp else "")
                    + " — re-check if still in play")
        if st == 'resolved':
            ehd = state_dict.get('entry_hit_date') or '?'
            thd = state_dict.get('target_hit_date') or '?'
            tg = _money(target)
            return (f"setup already played out "
                    f"(entry {_entry_str()} touched {ehd} "
                    f"→ target {tg or '?'} touched {thd}) — "
                    f"re-evaluating from scratch")
        return ''
    except Exception:
        return ''
