"""Salience (standout) layer — v4.14.6.58

Reads ALREADY-DETECTED signals attached to a recommendation and returns at
most ONE short why-tag ("⚡ insider buy" / "⚡ news catalyst" / etc.) so the
user can spot live-catalyst picks at a glance.

ADDITIVE ONLY. Never changes verdict, score, ranking, or pick selection.
Pure function; no AI, no network, no DB write.

Approved rules (the user, v4.14.6.58):
    S1  Insider Form-4 net BUY in last window         HIGH  "⚡ insider buy"
    S2  Has news + meaningful bonus / fresh count     HIGH  "⚡ news catalyst"
    S3  Earnings within ±5 days                       MED   "⚡ earnings"
    S4  Live price ±5%+ vs entry (today's move)       MED   "⚡ big move"
    S5  Pick added during this app session            LOW   "⚡ fresh pick"

Priority: HIGH > MED > LOW. If multiple fire, the HIGHEST-priority single tag
wins (one chip per card; silence on steady-state picks).

Public entrypoint:
    compute_salience(pos, current_price=None, insider_row=None,
                      app_session_start=None) -> tuple[str, str] | None
        Returns (priority, tag) or None.

All inputs except `pos` are optional. Any missing input simply skips its
rule (fail-safe) — never raises, never blocks the card from rendering.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional


PRIORITY_HIGH = 'high'
PRIORITY_MED = 'med'
PRIORITY_LOW = 'low'

# Ordering for "highest wins" — lower index = higher priority.
_PRIORITY_ORDER = {PRIORITY_HIGH: 0, PRIORITY_MED: 1, PRIORITY_LOW: 2}

# S2: news threshold matching what the recommend pipeline already weights.
_NEWS_BONUS_THRESHOLD = 0.3

# S3: earnings window — the user-approved.
_EARNINGS_LOOKAHEAD_DAYS = 5
_EARNINGS_LOOKBACK_DAYS = 2

# S4: today's-move threshold (percent).
_BIG_MOVE_PCT = 5.0


def _coerce_float(v) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_iso(s) -> Optional[datetime]:
    """Tolerant ISO parser. Strips trailing Z, returns None on failure."""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        s2 = s.strip()
        if s2.endswith('Z'):
            s2 = s2[:-1]
        return datetime.fromisoformat(s2)
    except Exception:
        return None


def _rule_insider(insider_row) -> Optional[tuple[str, str]]:
    """S1: Form-4 net BUY in the rolling window."""
    if insider_row is None:
        return None
    try:
        n_buys = insider_row['n_buys'] if 'n_buys' in insider_row.keys() else None
        net = (insider_row['net_open_market_usd']
                if 'net_open_market_usd' in insider_row.keys() else None)
    except Exception:
        try:
            n_buys = insider_row.get('n_buys')
            net = insider_row.get('net_open_market_usd')
        except Exception:
            return None
    n = _coerce_float(n_buys)
    nm = _coerce_float(net)
    if n is None or nm is None:
        return None
    if n > 0 and nm > 0:
        return (PRIORITY_HIGH, '⚡ insider buy')
    return None


def _rule_news(pos: dict) -> Optional[tuple[str, str]]:
    """S2: has_news AND (news_bonus >= threshold)."""
    if not pos.get('has_news'):
        return None
    bonus = _coerce_float(pos.get('news_bonus'))
    if bonus is not None and bonus >= _NEWS_BONUS_THRESHOLD:
        return (PRIORITY_HIGH, '⚡ news catalyst')
    return None


def _rule_earnings(pos: dict) -> Optional[tuple[str, str]]:
    """S3: earnings within ±5 days (5 ahead, 2 back)."""
    raw = pos.get('earnings_event_date')
    dt = _parse_iso(raw)
    if dt is None:
        return None
    now = datetime.now()
    delta = (dt.date() - now.date()).days
    if -_EARNINGS_LOOKBACK_DAYS <= delta <= _EARNINGS_LOOKAHEAD_DAYS:
        return (PRIORITY_MED, '⚡ earnings')
    return None


def _rule_big_move(pos: dict,
                    current_price: Optional[float]) -> Optional[tuple[str, str]]:
    """S4: live price moved ±5%+ vs entry."""
    cp = _coerce_float(current_price)
    entry = _coerce_float(pos.get('entry'))
    if cp is None or entry is None or entry <= 0:
        return None
    pct = (cp - entry) / entry * 100.0
    if abs(pct) >= _BIG_MOVE_PCT:
        return (PRIORITY_MED, '⚡ big move')
    return None


def _rule_fresh_pick(pos: dict,
                      app_session_start: Optional[datetime]
                      ) -> Optional[tuple[str, str]]:
    """S5: pick timestamp >= app session start."""
    if app_session_start is None:
        return None
    ts = _parse_iso(pos.get('timestamp'))
    if ts is None:
        return None
    if ts >= app_session_start:
        return (PRIORITY_LOW, '⚡ fresh pick')
    return None


def compute_salience(pos: dict,
                      current_price=None,
                      insider_row=None,
                      app_session_start=None
                      ) -> Optional[tuple[str, str]]:
    """Run the approved S1-S5 rules and return the single highest-priority
    firing tag, or None if no rule fires (steady-state pick = no chip).

    Args:
        pos: a recommendation/position dict from the rec pipeline. Expected
             keys (all optional; missing keys gracefully skip rules):
             'ticker', 'entry', 'has_news', 'news_bonus',
             'earnings_event_date', 'timestamp'.
        current_price: today's live price for this ticker (float). Caller
             usually has this in scope already (see reprice block in
             tired_market.py:25156-25174). None skips S4.
        insider_row: a sqlite3.Row or dict from cache.db.insider_flow for
             this ticker, or None. None skips S1.
        app_session_start: datetime when the current app session began.
             None skips S5.

    Returns:
        (priority, tag) tuple where priority is 'high'/'med'/'low' and tag
        is the user-facing chip text (e.g. '⚡ insider buy'). None if no
        rule fires.

    Fail-safe: any unexpected input shape returns None rather than raising.
    """
    if not isinstance(pos, dict):
        return None

    findings: list[tuple[str, str]] = []

    # S1 — insider
    try:
        r = _rule_insider(insider_row)
        if r is not None:
            findings.append(r)
    except Exception:
        pass

    # S2 — news
    try:
        r = _rule_news(pos)
        if r is not None:
            findings.append(r)
    except Exception:
        pass

    # S3 — earnings
    try:
        r = _rule_earnings(pos)
        if r is not None:
            findings.append(r)
    except Exception:
        pass

    # S4 — big move
    try:
        r = _rule_big_move(pos, current_price)
        if r is not None:
            findings.append(r)
    except Exception:
        pass

    # S5 — fresh pick
    try:
        r = _rule_fresh_pick(pos, app_session_start)
        if r is not None:
            findings.append(r)
    except Exception:
        pass

    if not findings:
        return None

    # Highest-priority wins. Stable order within same priority preserves
    # the S1-S5 declaration order.
    findings.sort(key=lambda x: _PRIORITY_ORDER.get(x[0], 9))
    return findings[0]
