"""tm_path_candidate_pools — v4.14.5.14a.6 per-path candidate sourcing.

Root cause of the 0%-BUY problem (v4.14.5.14a.5 investigation): the
momentum filter handed the SAME volatile small-caps to every path, so
"is RCEL a good slow_safe pick?" was correctly answered AVOID every
time. Fix: each path draws candidates from a pool that semantically
matches its analysis lens.

Hybrid (Option 3, the user 2026-05-17): the cache has rich price/history
data but NO market_cap / dividend_yield (only 671/2490 have
shares_outstanding; market_cap_tier is all-NULL). So:

  - slow_safe / moderate  → curated SEED LISTS (config.json), filtered
    to tickers with enough price history. These are exactly the
    large-cap quality names where a slow_safe BUY is a real question.
  - aggressive / lottery / penny_lottery → DYNAMIC pools from price /
    volatility / history (data we have, complete for ~2,480 tickers).

Future arc (flagged, NOT this patch): once the fundamentals fetcher
backfills market_cap + dividend_yield universe-wide, slow_safe and
moderate can migrate from seed lists to fully-dynamic pools.

Pure/defensive: every query is cache-only, wrapped, and falls back to
an empty pool (→ caller treats "no pool" as "no restriction", safe).
Pools cache in memory with a 1h TTL.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

# v4.14.6.0-price-band-tiers (2026-06-11): the tier model reverts from
# time-horizon paths (aggressive/moderate/slow_safe) back to share-price
# bands (the project's original framing before the time pivot). The
# eligibility gate at tm_queue_runner._eligibility_price_band_filter is
# UNCHANGED — it already reads min_price_usd / max_price_usd from this
# dict and applies on every dispatch path. Adding the new band keys
# here automatically makes the gate enforce them.
_PATHS = ('lottery', 'band_5_10', 'band_10_50', 'band_50_up')

_POOL_TTL_SECONDS = 3600  # recompute hourly as slow-fill adds data

# v4.14.6.0-price-band-tiers: legacy key compatibility. Files that still
# pass 'aggressive' / 'moderate' / 'slow_safe' / 'penny_lottery' as the
# `path` argument silently resolve via this remap. Lets the revert ship
# without re-keying every minor path-keyed dict in one go (each one is
# a mechanical follow-up; this shim keeps the app loading + dispatching
# while those land). The remap is reused by _cfg() below AND can be
# read by any caller that wants to migrate a legacy path name.
_LEGACY_PATH_REMAP = {
    'aggressive':    'band_10_50',  # was momentum-driven, now $10-50
    'moderate':      'band_10_50',  # was seed-list, mid-priced today
    'slow_safe':     'band_50_up',  # was seed-list, large-cap > $50
    'penny_lottery': 'lottery',     # was the merge target; same band
}


def remap_legacy_path(path: str) -> str:
    """Resolve a legacy time-path key to its price-band equivalent.
    Idempotent: a current-key in input returns unchanged. Empty / None
    → returns input verbatim. Public so other modules can migrate keys
    on persisted state (predictions.jsonl, signals.jsonl, etc.) without
    importing the private remap dict directly."""
    if not path:
        return path
    return _LEGACY_PATH_REMAP.get(path, path)


_CFG_DEFAULT = {
    # < $5/share. Cheapest, thinnest, highest-noise names.
    'lottery':    {'source': 'dynamic', 'max_price_usd': 5.0,
                   'min_price_history_days': 30,
                   'min_pool_size': 50, 'max_pool_size': 300},
    # $5-$10/share. Low-priced names; more shares per dollar; volatile.
    'band_5_10':  {'source': 'dynamic',
                   'min_price_usd': 5.0, 'max_price_usd': 10.0,
                   'min_price_history_days': 60,
                   'rank_by': 'momentum',
                   'min_pool_size': 100, 'max_pool_size': 300},
    # $10-$50/share. Mid-priced; broader liquidity and coverage.
    'band_10_50': {'source': 'dynamic',
                   'min_price_usd': 10.0, 'max_price_usd': 50.0,
                   'min_price_history_days': 100,
                   'rank_by': 'momentum',
                   'min_pool_size': 100, 'max_pool_size': 300},
    # $50+/share. Higher-priced, generally larger/more-established.
    'band_50_up': {'source': 'dynamic',
                   'min_price_usd': 50.0,
                   'min_price_history_days': 100,
                   'rank_by': 'momentum',
                   'min_pool_size': 100, 'max_pool_size': 300},
}

_VOL_DAYS = 21
_MOM_DAYS = 21

_lock = threading.Lock()
_cache: dict = {}   # path -> (computed_at_epoch, [tickers])


def apply_path_merge_v414514mu(cfg: dict | None = None) -> bool:
    """v4.14.5.14-merge-and-unify legacy hook — under v4.14.6.0-price-
    band-tiers the path model already defines lottery cleanly (max_price
    = 5) and the legacy penny_lottery / aggressive / moderate / slow_safe
    keys aren't in _CFG_DEFAULT at all (they remap via _LEGACY_PATH_REMAP
    when callers reference them). This function stays as a CALL-COMPAT
    no-op so startup code that still invokes it keeps working without
    KeyErrors; returns False (no migration applied this process).
    """
    global _cache
    if not bool((cfg or {}).get('use_path_merge', True)):
        return False
    # Drop any persisted penny_lottery override (file stays honest).
    try:
        if isinstance(cfg, dict):
            pcp = cfg.get('path_candidate_pools')
            if isinstance(pcp, dict):
                pcp.pop('penny_lottery', None)
            pft = cfg.get('path_fill_targets')
            if isinstance(pft, dict):
                pft.pop('penny_lottery', None)
    except Exception:
        pass
    return False
    # ── legacy body preserved below for rollback — unreachable ────
    if 'penny_lottery' not in _CFG_DEFAULT:
        return False  # already merged this process
    lot = _CFG_DEFAULT.get('lottery')
    if isinstance(lot, dict):
        lot['min_price_usd'] = 0.0
        lot['max_price_usd'] = 5.0
    _CFG_DEFAULT.pop('penny_lottery', None)
    _cache = {}  # any cached penny_lottery universe is now stale

    # v4.14.5.14-merge-and-unify-fix Fix 2 (2026-05-19): also mutate
    # the user-override layers in app.cfg so they don't stomp the
    # _CFG_DEFAULT mutation at runtime. The original merge-and-unify
    # patch missed this — _cfg(app, path) merges app.cfg's
    # path_candidate_pools[path] on top of _CFG_DEFAULT[path] via
    # dict.update(), so a persisted `lottery: {min_price_usd: 5.0}`
    # user override silently stomped the merge's min:0 back to 5,
    # producing the broken $5-$5 runtime band. Mutate the cfg layer
    # in lockstep with _CFG_DEFAULT. The App startup hook calls
    # save_config after we return True so the change durably persists.
    # See IDEAS.md "Discipline patterns surfaced 2026-05-19" Pattern
    # in HANDOFF item 18 (Phase 0 must follow the call chain).
    try:
        if isinstance(cfg, dict):
            pcp = cfg.get('path_candidate_pools')
            if isinstance(pcp, dict):
                pcp.pop('penny_lottery', None)
                lot_o = pcp.get('lottery')
                if isinstance(lot_o, dict):
                    lot_o['min_price_usd'] = 0.0
                    lot_o['max_price_usd'] = 5.0
            # Also pop penny_lottery from path_fill_targets if the user
            # (or any user) has it set. Inert at runtime (lottery has
            # fill_enabled=False so the legacy fill loop wouldn't have
            # consulted penny_lottery anyway) but the file stays
            # honest — HANDOFF item 19, parallel state stores.
            pft = cfg.get('path_fill_targets')
            if isinstance(pft, dict):
                pft.pop('penny_lottery', None)
    except Exception:
        pass  # never block the merge on a cfg-mutation error
    return True


def _cfg(app, path: str) -> dict:
    # v4.14.6.0-price-band-tiers: remap legacy path keys (aggressive /
    # moderate / slow_safe / penny_lottery) to their price-band
    # equivalents before lookup. Lets unedited path-keyed dicts +
    # historical persisted state continue to resolve until each call
    # site is updated to the new keys.
    resolved = _LEGACY_PATH_REMAP.get(path, path)
    d = dict(_CFG_DEFAULT.get(resolved, {}))
    try:
        u = ((getattr(app, 'cfg', {}) or {})
             .get('path_candidate_pools', {}) or {}).get(resolved)
        if isinstance(u, dict):
            d.update(u)
    except Exception:
        pass
    return d


def _conn():
    try:
        import tm_cache
        return tm_cache.get_connection()
    except Exception:
        return None


def _log(app, msg: str) -> None:
    try:
        fn = getattr(app, '_log', None)
        if callable(fn):
            fn(msg, 'muted')
    except Exception:
        pass


# ── cache-only price/history/volatility/momentum readers ──────────────

def _history_counts(conn) -> dict:
    out = {}
    try:
        for tk, n in conn.execute(
                "SELECT ticker, COUNT(*) FROM daily_bars "
                "GROUP BY ticker"):
            out[str(tk).upper()] = int(n)
    except Exception:
        return {}
    return out


def _latest_close(conn) -> dict:
    out = {}
    try:
        for tk, c in conn.execute(
                "SELECT d.ticker, d.close FROM daily_bars d "
                "JOIN (SELECT ticker, MAX(date) md FROM daily_bars "
                "      GROUP BY ticker) m "
                "  ON m.ticker=d.ticker AND m.md=d.date"):
            try:
                out[str(tk).upper()] = float(c)
            except (TypeError, ValueError):
                pass
    except Exception:
        return {}
    return out


def _recent_window(conn, days: int) -> dict:
    """{ticker: [closes newest-first up to `days`]} — one windowed
    query, cache-only."""
    out: dict = {}
    try:
        rows = conn.execute(
            "SELECT ticker, close FROM ("
            "  SELECT ticker, date, close, ROW_NUMBER() OVER ("
            "    PARTITION BY ticker ORDER BY date DESC) rn "
            "  FROM daily_bars) WHERE rn <= ? "
            "ORDER BY ticker, rn", (days,))
        for tk, c in rows:
            try:
                out.setdefault(str(tk).upper(), []).append(float(c))
            except (TypeError, ValueError):
                pass
    except Exception:
        return {}
    return out


def _vol_pct(closes: list) -> float:
    """Range volatility over the window: (max-min)/min * 100."""
    cs = [c for c in closes if c and c > 0]
    if len(cs) < 2:
        return 0.0
    lo, hi = min(cs), max(cs)
    return ((hi - lo) / lo * 100.0) if lo > 0 else 0.0


def _momentum_pct(closes: list) -> float:
    """Newest-first window → % change from oldest to newest in it."""
    cs = [c for c in closes if c and c > 0]
    if len(cs) < 2:
        return 0.0
    newest, oldest = cs[0], cs[-1]
    return ((newest - oldest) / oldest * 100.0) if oldest > 0 else 0.0


# ── pool builders ─────────────────────────────────────────────────────

def _seed_pool(app, path: str, cfg: dict, conn) -> list:
    seeds = [str(t).upper() for t in (cfg.get('seed_tickers') or [])]
    min_hist = int(cfg.get('min_price_history_days', 0) or 0)
    hist = _history_counts(conn)
    pool = [t for t in seeds if hist.get(t, 0) >= min_hist]
    # Per the user: curated lists — do NOT loosen if short; ship what's
    # valid (even 50-60 is fine). Only warn if it fell under floor.
    return pool


def _dynamic_pool(app, path: str, cfg: dict, conn) -> list:
    hist = _history_counts(conn)
    close = _latest_close(conn)
    min_hist = int(cfg.get('min_price_history_days', 30) or 30)
    min_px = cfg.get('min_price_usd')
    max_px = cfg.get('max_price_usd')
    max_n = int(cfg.get('max_pool_size', 300) or 300)
    min_n = int(cfg.get('min_pool_size', 100) or 100)

    excl: set = set()
    for sl in (cfg.get('exclude_seed_lists') or []):
        excl |= {str(t).upper()
                 for t in (_cfg(app, sl).get('seed_tickers') or [])}

    def base(min_hist_eff, vol_floor):
        cand = []
        win = (_recent_window(conn, _VOL_DAYS)
               if (vol_floor or cfg.get('rank_by') == 'momentum')
               else {})
        for tk, px in close.items():
            if tk in excl:
                continue
            if hist.get(tk, 0) < min_hist_eff:
                continue
            if min_px is not None and px < float(min_px):
                continue
            if max_px is not None and px >= float(max_px):
                continue
            if vol_floor:
                if _vol_pct(win.get(tk, [])) < float(vol_floor):
                    continue
            cand.append(tk)
        return cand, win

    vol_floor = cfg.get('min_recent_volatility_pct')
    cand, win = base(min_hist, vol_floor)

    # Progressive loosen ONLY for dynamic pools below floor: drop the
    # volatility gate first, then the history requirement.
    if len(cand) < min_n and vol_floor:
        _log(app, f"[path-pools] {path}: {len(cand)} < floor "
                  f"{min_n}, dropping volatility gate")
        cand, win = base(min_hist, None)
    if len(cand) < min_n and min_hist > 30:
        _log(app, f"[path-pools] {path}: {len(cand)} < floor "
                  f"{min_n}, dropping history requirement")
        cand, win = base(30, None)

    rank_by = cfg.get('rank_by')
    if rank_by == 'momentum':
        mwin = _recent_window(conn, _MOM_DAYS)
        cand.sort(key=lambda t: -_momentum_pct(mwin.get(t, [])))
    elif vol_floor or path == 'lottery':
        cand.sort(key=lambda t: -_vol_pct(win.get(t, [])))
    else:
        cand.sort()  # deterministic
    return cand[:max_n]


def get_path_universe(app, path: str) -> list:
    """Return the ticker pool for `path` (cached 1h). Empty list on
    any failure or unknown path — the caller treats an empty pool as
    'no restriction' so a pool fault never blocks fill mode."""
    if path not in _PATHS:
        return []
    now = time.time()
    with _lock:
        hit = _cache.get(path)
        if hit and (now - hit[0]) < _POOL_TTL_SECONDS:
            return list(hit[1])
    conn = _conn()
    if conn is None:
        return []
    try:
        cfg = _cfg(app, path)
        if cfg.get('source') == 'seed_list':
            pool = _seed_pool(app, path, cfg, conn)
        else:
            pool = _dynamic_pool(app, path, cfg, conn)
        floor = int(cfg.get('min_pool_size', 0) or 0)
        if floor and len(pool) < floor:
            _log(app, f"[path-pools] {path}: pool {len(pool)} is "
                      f"below min {floor} — proceeding with what's "
                      f"available (path runs, weaker coverage)")
    except Exception as e:
        _log(app, f"[path-pools] {path}: build failed "
                  f"({type(e).__name__}: {e}); no pool restriction")
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    with _lock:
        prev = _cache.get(path)
        _cache[path] = (now, list(pool))
    if prev is not None and len(prev[1]) != len(pool):
        _log(app, f"[path-pools] {path} pool refreshed: "
                  f"{len(prev[1])} → {len(pool)} tickers")
    return list(pool)
