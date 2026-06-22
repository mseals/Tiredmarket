"""tm_layer3_replace — Layer 3 of the Fill-Validate-Replace architecture
(IDEAS.md). v4.14.5.14-layer3-replace (2026-05-23).

Background daemon. Reads `recommend_cache_validation` rows and, when
Layer 2 consensus has CONTRADICTED a Layer 1 BUY with an AVOID-specific
majority (see `_is_layer3_contradicted`), DELETEs the corresponding
`recommend_cache` row. The existing cadence runner refills the open slot
from the candidate pool — Layer 3 builds no shortlist of its own and
inherits the v4.14.5.14-cadence-pool-fix pool restriction.

A cooldown table (`layer3_drop_log`) prevents drop-refill loops: a
dropped (ticker, path) is suppressed from cadence shortlists for
COOLDOWN_SECONDS, UNLESS Layer 1 changes its mind — an entry / target /
stop / status change flips the L1 signature hash and releases the
cooldown early.

Ships DORMANT. Flag `use_layer3_replace` defaults False; the daemon is
not launched and `_is_suppressed_by_cooldown` is a no-op (the cadence
filter is flag-gated) until the user flips it.

VOCABULARY NOTE: `recommend_cache_validation.votes_json` stores per-vote
`direction` as BUY / WATCH / AVOID (the v4.14.5.14-layer2-thesis-
validation parser maps SUPPORT→BUY, NEUTRAL→WATCH, OPPOSE→AVOID at the
boundary). Layer 3's "oppose" signal is therefore AVOID. This predicate
is deliberately TIGHTER than Layer 2's existing `verdict='CONTRADICTED'`
(which is the looser "<=33% BUY", lumping WATCH+AVOID together): a
WATCH-majority must NOT drop a pick (badge-clarity discipline — "wait"
is not "no").
"""

import datetime
import hashlib
import json
import threading
import time

LOOP_INTERVAL_SECONDS = 180          # mirrors the Layer 2 daemon cadence
COOLDOWN_SECONDS = 86400             # 24h (knob: bump to 7d if requested)
IDLE_HEARTBEAT_SECONDS = 3600        # mirrors the -revalidate-and-heartbeat pattern
MIN_VOTERS_FOR_DROP = 3             # a drop requires >=3 voters UNCONDITIONALLY (no sub-3 drop)

# votes_json vocabulary (mapped from SUPPORT/NEUTRAL/OPPOSE at parse time)
_OPPOSE = 'AVOID'
_SUPPORT = 'BUY'
_NEUTRAL = 'WATCH'

# Most-recent-tick processing counts, for `_idle_skip_counts`.
_last_tick_counts = {'scanned': 0, 'contradicted': 0,
                     'dropped_this_tick': 0, 'suppressed': 0}


def _conn(app):
    db = getattr(app, 'db', None)
    return getattr(db, 'conn', None) if db is not None else None


def _log(app, msg: str, color: str = 'muted') -> None:
    try:
        fn = getattr(app, '_log', None)
        if callable(fn):
            fn(msg, color)
    except Exception:
        pass


def _ensure_table(conn) -> None:
    """Create the drop-log table + index if missing. Idempotent. The
    App startup migration also creates it (so the cooldown query never
    blows up before the daemon's first tick); the daemon ensures it
    defensively too, mirroring Layer 2's `_ensure_table`."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS layer3_drop_log ("
        " ticker TEXT NOT NULL, path TEXT NOT NULL, "
        " dropped_at REAL NOT NULL, l1_signature TEXT NOT NULL, "
        " PRIMARY KEY (ticker, path, dropped_at))")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_layer3_drop_recent "
        "ON layer3_drop_log(dropped_at)")


def _plog(app):
    state = getattr(app, '_holdings_state', None) or {}
    return state.get('predictions_log')


def _open_buy_record(app, ticker: str, path: str):
    """Most-recent (ticker, path) prediction iff it is a currently-open
    BUY, else None. Same most-recent-wins / open / BUY semantics as
    tm_recommend_cache._is_current_buy. Predictions live in
    predictions.jsonl via PredictionsLog (NOT a DB table), so this reads
    `app._holdings_state['predictions_log']`. Fail-safe → None."""
    try:
        plog = _plog(app)
        if plog is None:
            return None
        rec = plog.get_most_recent_for_ticker_and_path(ticker, path)
        if not rec:
            return None
        if (rec.get('direction') or '').upper() != 'BUY':
            return None
        if rec.get('status') not in (None, '', 'open'):
            return None
        return rec
    except Exception:
        return None


def _l1_signature(app, ticker: str, path: str) -> str:
    """Stable 16-char hash of the open Layer 1 BUY's defining fields
    (buy_zone / target / stop / status / created-at). Returns '' when no
    open BUY exists. Used at drop time and at cooldown-check time so a
    real Layer 1 thesis change (or the BUY disappearing) releases the
    cooldown early. Fail-safe → ''."""
    try:
        rec = _open_buy_record(app, ticker, path)
        if rec is None:
            return ''
        parts = (
            rec.get('buy_zone_low'), rec.get('buy_zone_high'),
            rec.get('target'), rec.get('stop'),
            rec.get('status') or 'open',
            rec.get('timestamp') or rec.get('id') or '')
        raw = '|'.join('' if p is None else str(p) for p in parts)
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]
    except Exception:
        return ''


def _is_layer3_contradicted(app, validation_row, votes_list) -> bool:
    """Single source of truth: True iff Layer 3 should DROP this pick.

    Drops iff the pick is a currently-open Layer 1 BUY AND consensus is
    an AVOID-specific majority:
      - len(votes) >= MIN_VOTERS_FOR_DROP (3) AND AVOID strictly greater
        than BOTH the BUY count and the WATCH count (strict OPPOSE majority).

    v4.14.5.62-layer3-min3voters: a drop requires >=3 voters UNCONDITIONALLY.
    The old "unanimous AVOID at any n>=1" carve-out is REMOVED, so a thin/new
    user with 1-2 AIs never sees a pick dropped until at least 3 AIs have
    weighed in (new-user predictability over early removal). At n>=3, unanimous
    AVOID is still a drop — the majority branch covers it (n_oppose=n > 0).

    A WATCH-majority does NOT drop. Fail-safe → False on malformed row,
    missing/empty votes, or no current open BUY for (ticker, path).
    Never raises."""
    try:
        if not isinstance(validation_row, dict):
            return False
        tk = (validation_row.get('ticker') or '').upper()
        pth = validation_row.get('path') or ''
        if not tk or not pth:
            return False
        # Only currently-open Layer 1 BUYs are subject to a Layer 3 drop
        # (excludes WATCH/AVOID-tagged picks and closed positions).
        if _open_buy_record(app, tk, pth) is None:
            return False
        if not votes_list:
            return False
        dirs = []
        for v in votes_list:
            if not isinstance(v, dict):
                continue
            d = (v.get('direction') or '').upper().strip()
            if d in (_SUPPORT, _NEUTRAL, _OPPOSE):
                dirs.append(d)
        n = len(dirs)
        if n == 0:
            return False
        # v4.14.5.62-layer3-min3voters: require >=3 committed voters
        # UNCONDITIONALLY before any drop. This removes the old unanimous-
        # AVOID-at-any-n carve-out — a thin/new user (1-2 AIs) never sees a
        # pick dropped until real consensus (>=3 voices) exists. At n>=3 the
        # AVOID-majority branch below still drops a unanimous-AVOID set
        # (n_oppose=n > n_support+n_neutral=0), so genuine consensus is intact.
        if n < MIN_VOTERS_FOR_DROP:
            return False
        n_oppose = dirs.count(_OPPOSE)
        n_support = dirs.count(_SUPPORT)
        n_neutral = dirs.count(_NEUTRAL)
        # Strict OPPOSE MAJORITY (more AVOID than all other committed
        # votes combined) at n >= MIN_VOTERS_FOR_DROP. This is majority,
        # not mere plurality: audit A5 — [AVOID, AVOID, BUY, WATCH],
        # 2 vs 2 combined — must NOT drop. (n_oppose > n_support +
        # n_neutral ⇔ 2*n_oppose > n ⇔ strict majority of committed votes.)
        if (n >= MIN_VOTERS_FOR_DROP
                and n_oppose > (n_support + n_neutral)):
            return True
        return False
    except Exception:
        return False


def _is_suppressed_by_cooldown(app, ticker: str, path: str) -> bool:
    """True iff (ticker, path) was dropped by Layer 3 within
    COOLDOWN_SECONDS AND the stored L1 signature still matches the
    current one (Layer 1 hasn't changed its mind). Read-only — never
    writes. Fail-safe → False (on error, prefer re-analysis over wrongly
    hiding a candidate)."""
    try:
        conn = _conn(app)
        if conn is None:
            return False
        cutoff = time.time() - COOLDOWN_SECONDS
        cur = conn.execute(
            "SELECT l1_signature FROM layer3_drop_log "
            "WHERE ticker = ? AND path = ? AND dropped_at >= ? "
            "ORDER BY dropped_at DESC LIMIT 1",
            (str(ticker).upper(), str(path), cutoff))
        row = cur.fetchone()
        if not row:
            return False
        stored_sig = row[0] or ''
        current_sig = _l1_signature(app, ticker, path)
        # Empty current_sig (no open BUY now) or a changed signature
        # releases the cooldown — only suppress when L1 still stands.
        return bool(stored_sig) and stored_sig == current_sig
    except Exception:
        return False


def _idle_skip_counts(app, conn) -> dict:
    """Most-recent tick's processing counts. Fail-safe → zeros.
    Mirrors the -revalidate-and-heartbeat helper of the same name."""
    try:
        return dict(_last_tick_counts)
    except Exception:
        return {'scanned': 0, 'contradicted': 0,
                'dropped_this_tick': 0, 'suppressed': 0}


def _ensure_armed(app, persist_cfg=None) -> float:
    """First-run safety: stamp cfg['layer3_armed_at'] (epoch seconds) if
    absent, persist via the supplied callback, and log the armed line.
    Idempotent — an existing value is returned unchanged (never
    re-stamped), so verdicts written before the daemon first armed are
    ignored on the first scan (no UX cliff) and the armed point survives
    restarts. Returns the armed-at timestamp. Fail-safe."""
    cfg = getattr(app, 'cfg', {}) or {}
    existing = cfg.get('layer3_armed_at')
    if existing:
        try:
            return float(existing)
        except (TypeError, ValueError):
            pass
    now = time.time()
    try:
        cfg['layer3_armed_at'] = now
    except Exception:
        pass
    if callable(persist_cfg):
        try:
            persist_cfg()
        except Exception:
            pass
    try:
        _log(app,
             f"[layer3] armed at "
             f"{datetime.datetime.fromtimestamp(now).isoformat(timespec='seconds')}; "
             f"verdicts before this point will be ignored on first scan.")
    except Exception:
        pass
    return now


# ── v4.14.6.91-layer3-low-opportunity-replace ────────────────────────────
# SECOND drop condition: replace genuinely played-out displayed picks with a
# clearly-better bench pick from the same band, using the SAME Formula C score
# that drives the v90 sort. Conservative + protective by three anti-churn gates:
# a played-out floor, a bench-beats-by-margin test, and the existing 24h
# cooldown. 0 AI calls, cache-only price. Flag-gated, default OFF.
_LOW_OPP_SCORE_FLOOR = 0.30      # score_C below this == played-out (eligible)
_LOW_OPP_BENCH_MARGIN = 1.30     # bench must be >30% better to displace


def _cache_only_price(app, ticker: str):
    """Return a LIVE-ish price for `ticker` using ONLY cached sources — never a
    network fetch. Order: (1) the warm quote cache via peek_quote() (which
    guarantees the following quote() read won't fetch), then (2) the last
    daily_bars close from cache.db (a local DB read). None when neither is
    available. Never raises."""
    # 1) warm quote cache — peek_quote() is the no-fetch guard.
    try:
        cache = (getattr(app, '_holdings_state', None) or {}).get('cache')
        if cache is not None and cache.peek_quote(ticker):
            q = cache.quote(ticker)
            pr = (q or {}).get('price')
            if pr:
                return float(pr)
    except Exception:
        pass
    # 2) cache-only fallback: last daily_bars close (local DB, no network).
    try:
        import tm_cache as _tc
        bars = _tc.get_daily_bars(ticker)
        if bars:
            cl = bars[-1]['close']
            return float(cl) if cl else None
    except Exception:
        pass
    return None


def _pick_score_c(app, tk: str, pth: str, price_fn) -> float | None:
    """Formula C score for one (ticker, path) — REUSES tm_recommend's
    _normalize_prediction (entry/target/reward_to_risk) and _score_c (the exact
    score the v90 sort uses). Returns None when there's no open BUY, no
    cache-only price (skip this tick), or bad data. Never raises."""
    try:
        rec = _open_buy_record(app, tk, pth)
        if not rec:
            return None
        import tm_recommend as _tr
        norm = _tr._normalize_prediction(rec)
        if not norm:
            return None
        live = price_fn(tk)
        if live is None:
            return None          # no fresh cached price → skip (no fetch)
        return _tr._score_c(norm, lambda _t, _v=live: _v)
    except Exception:
        return None


def _low_opportunity_pass(app, conn) -> int:
    """One low-opportunity replacement sweep over recommend_cache. Drops a
    DISPLAYED pick only when ALL THREE hold: (1) its score_C < the played-out
    floor, (2) a BENCH pick in the same band scores > displaced × the margin,
    (3) it's not in the 24h cooldown. Same DELETE + layer3_drop_log path as the
    AVOID drop; the cadence runner refills the slot from the bench (no
    synchronous promote/validate → stays 0 AI). Returns the number dropped.
    Never raises."""
    dropped = 0
    try:
        rows = conn.execute(
            "SELECT ticker, path, tier FROM recommend_cache").fetchall()
    except Exception:
        return 0

    def _price(t):
        return _cache_only_price(app, t)

    # Score every row once (one cache-only price read apiece); skip unscoreable.
    by_path: dict = {}
    for r in rows:
        tk = (r[0] or '').upper()
        pth = r[1] or ''
        tier = (r[2] or '').lower()
        sc = _pick_score_c(app, tk, pth, _price)
        if sc is None:
            continue
        d = by_path.setdefault(pth, {'displayed': [], 'bench': []})
        if tier == 'displayed':
            d['displayed'].append((tk, sc))
        elif tier == 'bench':
            d['bench'].append(sc)

    for pth, d in by_path.items():
        best_bench = max(d['bench']) if d['bench'] else None
        for tk, sc in d['displayed']:
            # (1) played-out floor — protects healthy picks (score_C ≥ floor).
            if sc >= _LOW_OPP_SCORE_FLOOR:
                continue
            # (2) bench must be clearly better; no qualifying bench → keep the
            #     shown pick (a mediocre shown pick beats an empty slot).
            if best_bench is None or best_bench <= sc * _LOW_OPP_BENCH_MARGIN:
                continue
            # (3) existing 24h cooldown (same check the AVOID drop uses).
            if _is_suppressed_by_cooldown(app, tk, pth):
                continue
            try:
                sig = _l1_signature(app, tk, pth)
                conn.execute(
                    "DELETE FROM recommend_cache "
                    "WHERE ticker = ? AND path = ?", (tk, pth))
                conn.execute(
                    "INSERT INTO layer3_drop_log "
                    "(ticker, path, dropped_at, l1_signature) "
                    "VALUES (?,?,?,?)", (tk, pth, time.time(), sig))
                conn.commit()
                dropped += 1
                _log(app,
                     f"[layer3] dropped {tk}/{pth} — low opportunity "
                     f"(score_C {sc:.2f} < {_LOW_OPP_SCORE_FLOOR:.2f}; bench "
                     f"{best_bench:.2f} > {_LOW_OPP_BENCH_MARGIN:.0%} better); "
                     f"cadence will refill from bench.")
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                _log(app, f"[layer3] low-opp drop failed for {tk}/{pth}: "
                          f"{type(e).__name__}: {e}", 'amber')
    return dropped


def _process_tick(app, conn, armed_at: float) -> dict:
    """One scan: read validation rows since armed_at whose recommend_cache
    row still exists, drop the AVOID-contradicted ones (respecting the
    cooldown), and return the tick counts. Each drop is its own committed
    DELETE + drop-log INSERT so a mid-tick failure can't lose prior drops.
    Never raises (caller's loop also guards)."""
    scanned = contradicted = dropped = suppressed = 0
    try:
        rows = conn.execute(
            "SELECT v.ticker, v.path, v.votes_json "
            "FROM recommend_cache_validation v "
            "JOIN recommend_cache c "
            "  ON c.ticker = v.ticker AND c.path = v.path "
            "WHERE v.validated_at >= ?",
            (int(armed_at),)).fetchall()
    except Exception:
        rows = []
    for r in rows:
        scanned += 1
        tk = (r[0] or '').upper()
        pth = r[1] or ''
        try:
            votes = json.loads(r[2]) if r[2] else []
        except Exception:
            votes = []
        if not _is_layer3_contradicted(app, {'ticker': tk, 'path': pth},
                                       votes):
            continue
        contradicted += 1
        if _is_suppressed_by_cooldown(app, tk, pth):
            suppressed += 1
            continue
        try:
            sig = _l1_signature(app, tk, pth)
            conn.execute(
                "DELETE FROM recommend_cache "
                "WHERE ticker = ? AND path = ?", (tk, pth))
            conn.execute(
                "INSERT INTO layer3_drop_log "
                "(ticker, path, dropped_at, l1_signature) "
                "VALUES (?,?,?,?)", (tk, pth, time.time(), sig))
            conn.commit()
            dropped += 1
            _log(app, f"[layer3] dropped {tk}/{pth} — consensus "
                      f"contradicted (AVOID-majority); cadence will "
                      f"refill the slot.")
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            _log(app, f"[layer3] drop failed for {tk}/{pth}: "
                      f"{type(e).__name__}: {e}", 'amber')
    # v4.14.6.91-layer3-low-opportunity-replace: SECOND drop condition (flag-
    # gated, default OFF). Runs in the SAME tick; adds to `dropped` so the
    # live-render hook below fires for low-opportunity drops too. AVOID-only
    # behavior is byte-identical when the flag is off.
    low_opp = 0
    try:
        if bool((getattr(app, 'cfg', {}) or {}).get(
                'recommend_layer3_low_opportunity_replace', False)):
            low_opp = _low_opportunity_pass(app, conn)
            dropped += low_opp
    except Exception:
        pass
    counts = {'scanned': scanned, 'contradicted': contradicted,
              'dropped_this_tick': dropped, 'suppressed': suppressed,
              'low_opportunity': low_opp}
    _last_tick_counts.update(counts)
    # v4.14.5.62-layer3-replace-default: if this tick dropped any pick, ask the
    # app to surface the one-time Teacher-AI explanation ("I removed a pick…").
    # Decoupled: we only call a method the app may expose — the daemon keeps no
    # teacher dependency, and emit is once-ever + thread-safe on the app side.
    if dropped:
        try:
            fn = getattr(app, '_fire_layer3_drop_feature_intro', None)
            if callable(fn):
                fn()
        except Exception:
            pass
        # v4.14.6.86-live-render-reco-on-change (Stage 0): notify the OPEN
        # Recommend window so the dropped pick disappears LIVE instead of only
        # on close-and-reopen. Same decoupled, daemon-safe getattr pattern as
        # the feature-intro hook above — zero new coupling, no-op if the app
        # doesn't expose it. The app side marshals to the Tk main thread via
        # root.after(0, ...) and only acts when the window is open; a tick-level
        # call is sufficient because the handler invalidates the whole memoized
        # result cache and re-renders.
        try:
            cb = getattr(app, '_on_reco_pick_removed', None)
            if callable(cb):
                cb()
        except Exception:
            pass
    return counts


def _loop(app, stop_event, interval: int = LOOP_INTERVAL_SECONDS,
          persist_cfg=None) -> None:
    """The daemon. 180s tick, idle-log heartbeat hourly, no bare except —
    any tick error logs and the daemon stays up (silent-death prevention,
    -revalidate-and-heartbeat discipline)."""
    _idle_logged_at = None
    while not stop_event.is_set():
        try:
            cfg = getattr(app, 'cfg', {}) or {}
            # Belt-and-suspenders: daemon should not be started without
            # the flag, but go dormant cheaply if it was flipped off.
            if not bool(cfg.get('use_layer3_replace', False)):
                stop_event.wait(interval)
                continue
            conn = _conn(app)
            if conn is None:
                stop_event.wait(interval)
                continue
            _ensure_table(conn)
            armed_at = _ensure_armed(app, persist_cfg)
            counts = _process_tick(app, conn, armed_at)
            now_ts = time.time()
            if counts['dropped_this_tick'] > 0:
                _idle_logged_at = None
                _log(app,
                     f"[layer3] active — scanned {counts['scanned']}, "
                     f"contradicted {counts['contradicted']}, dropped "
                     f"{counts['dropped_this_tick']} (suppressed "
                     f"{counts['suppressed']}). Next check in {interval}s.")
            else:
                should_log = (
                    _idle_logged_at is None
                    or (now_ts - _idle_logged_at) >= IDLE_HEARTBEAT_SECONDS)
                if should_log:
                    _log(app,
                         f"[layer3] idle — scanned {counts['scanned']} "
                         f"validated rows, {counts['contradicted']} met "
                         f"contradicted predicate, "
                         f"{counts['dropped_this_tick']} dropped this "
                         f"tick ({counts['suppressed']} suppressed by "
                         f"cooldown). Next check in {interval}s.")
                    _idle_logged_at = now_ts
        except Exception as e:
            _log(app, f"[layer3] error in loop tick: "
                      f"{type(e).__name__}: {e}", 'amber')
        stop_event.wait(interval)


def launch_layer3_replace(app, persist_cfg=None):
    """Idempotent. Returns the daemon thread, or None if disabled /
    already running / launch failed. Mirrors launch_layer2_validation.
    The App startup hook logs the 'daemon started' line. `persist_cfg`
    is a no-arg callback the App supplies (lambda: save_config(app.cfg))
    so the daemon can durably stamp layer3_armed_at without importing
    the GUI module."""
    try:
        cfg = getattr(app, 'cfg', {}) or {}
        if not bool(cfg.get('use_layer3_replace', False)):
            return None
        ex = getattr(app, '_layer3_thread', None)
        if ex is not None and ex.is_alive():
            return None  # already running (Resume re-entry guard)
        stop = getattr(app, '_layer3_stop', None)
        if stop is None:
            stop = threading.Event()
            app._layer3_stop = stop
        stop.clear()
        try:
            interval = int(cfg.get('layer3_interval_seconds',
                                   LOOP_INTERVAL_SECONDS))
        except (TypeError, ValueError):
            interval = LOOP_INTERVAL_SECONDS
        if interval < 30:
            interval = 30  # never a tight loop
        t = threading.Thread(
            target=_loop, args=(app, stop, interval, persist_cfg),
            daemon=True, name='layer3-replace')
        app._layer3_thread = t
        t.start()
        return t
    except Exception as e:
        _log(app, f"[layer3] launch failed (non-fatal): "
                  f"{type(e).__name__}: {e}", 'amber')
        return None
