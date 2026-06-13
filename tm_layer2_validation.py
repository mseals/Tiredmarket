"""tm_layer2_validation — v4.14.5.14b.

Fill-Validate-Replace **Layer 2 (Validate)**. A background daemon that
runs the EXISTING multi-AI consensus engine (the same one user-clicked
"Verify" uses) against picks already sitting in `recommend_cache`,
attaches a validation score per (ticker, path) in a sibling table
`recommend_cache_validation`, and otherwise leaves Layer 1 completely
alone.

Design contract (see IDEAS.md "Fill-Validate-Replace architecture"):
  - NEVER blocks or rewrites Layer 1 fill. Validation is additive.
  - Data-only this patch — no UI. The UI-labels patch consumes the
    score later; Layer 3 consumes `verdict` later.
  - Background, never a tight loop: one pick per
    cfg['layer2_validation_interval_seconds'] (default 180).
  - Pauses when no provider can run, resumes when one can.
  - Respects a validation-recency window (parallel to the analysis
    verdict-recency gate) + the price-band eligibility gate.
  - Fully fail-open: any fault logs amber and the daemon either skips
    the tick or dies quietly WITHOUT dragging other daemons down.
  - Single-voice reality (Mistral-alone): runs with whatever the
    consensus engine selects, marks the row single_voice=1, and never
    claims strong "consensus" from one model.

Mirrors the recommend_cache / news / fundfile daemon pattern: an
idempotent launch_*() started from App._v45012_launch_arc_b_daemons,
a threading.Event for sleep + graceful stop.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime

_LAYER2_DEFAULT_INTERVAL = 180          # seconds between validations
_CONSENSUS_TIMEOUT_SECONDS = 180        # wait cap for one consensus run
_NO_MODELS_BACKOFF = 900                # idle longer when unconfigured
_CANDIDATE_SCAN_LIMIT = 60              # rows examined per selection

# v4.14.5.14b-layer2-fix: one-shot guard so the honest cloud-fallback
# log line fires once per process instead of every 180 s validation.
_CLOUD_FALLBACK_LOGGED = False


# ── small shared helpers (same shapes as tm_recommend_cache) ──────────

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


# ── v4.14.5.14-tier2-budget-and-unification (Part A): Tier-2 cap ──────

_TIER2_CAP = 3  # never validate one pick with more than this many AIs


# ── v4.14.5.62-tier2-validator-select: flexible-validator module gate ─
#
# OFF (default) → _select_tier2_providers (strict favorites) is used,
# byte-identical to pre-patch. ON → _select_tier2_providers_flexible picks
# up to 3 DISTINCT validators from whatever the user has enabled (favorites
# first, then their other enabled providers), cooldown-aware (short transient
# cooldown → wait; long/daily exhaustion → backfill from a spare). Set from
# cfg['tier2_flexible_validators'] at startup + Settings save (tired_market
# App), mirroring the other module gates.
_FLEXIBLE_VALIDATORS = False
_WAIT_THRESHOLD_SEC = 12  # short-cooldown ceiling worth waiting for (seconds)


def set_flexible_validators(enabled: bool, wait_threshold_sec: int = 12) -> None:
    """Push the flexible-validator selection gate into this module. enabled=
    False (default) keeps the strict favorites selection. wait_threshold_sec
    is the 'short cooldown worth waiting for' ceiling (clamped to >= 0)."""
    global _FLEXIBLE_VALIDATORS, _WAIT_THRESHOLD_SEC
    _FLEXIBLE_VALIDATORS = bool(enabled)
    try:
        t = int(wait_threshold_sec)
    except (TypeError, ValueError):
        t = 12
    _WAIT_THRESHOLD_SEC = t if t >= 0 else 0


def flexible_validators_enabled() -> bool:
    """Read-only accessor (testable)."""
    return _FLEXIBLE_VALIDATORS


def _parse_pref(entry):
    """Normalize one layer2_validation_models entry to (provider_key,
    model_or_None). Accepts ['Provider', 'model'], {'provider':...,
    'model':...}, or a bare 'Provider' string. Never raises."""
    try:
        if isinstance(entry, dict):
            return (str(entry.get('provider') or '').strip() or None,
                    (str(entry.get('model')).strip()
                     if entry.get('model') else None))
        if isinstance(entry, (list, tuple)):
            prov = str(entry[0]).strip() if len(entry) >= 1 else ''
            model = (str(entry[1]).strip()
                     if len(entry) >= 2 and entry[1] else None)
            return (prov or None, model)
        if isinstance(entry, str):
            return (entry.strip() or None, None)
    except Exception:
        pass
    return (None, None)


def _select_tier2_providers(app, providers, cfg):
    """Pick up to _TIER2_CAP enabled+available providers matching the
    cfg['layer2_validation_models'] preferences (in order), each pinned
    to its preferred model for the validation call.

    Returns:
      - None  → cap DISABLED (empty/absent key): caller keeps the full
                all-provider fanout (pre-patch behaviour).
      - [...]  → 0..3 curated provider-dict COPIES. An empty list means
                the cap is ON but no preferred model is available right
                now (caller should skip + retry, NOT fall back to the
                full fanout — the cap is strict).
    Never raises; on any internal error returns None (fail-open to the
    existing fanout)."""
    try:
        prefs = cfg.get('layer2_validation_models') or []
        if not prefs:
            return None
        try:
            import tm_ai_router as _r
        except Exception:
            return None  # router missing → fail-open to full fanout
        by_id, by_name = {}, {}
        for p in (providers or []):
            if not isinstance(p, dict):
                continue
            try:
                cid = _r.provider_canonical_id(p)
            except Exception:
                cid = None
            if cid:
                by_id.setdefault(str(cid).strip().lower(), p)
            nm = str(p.get('name') or '').strip().lower()
            if nm:
                by_name.setdefault(nm, p)
        picked, seen = [], set()
        for entry in prefs:
            prov_key, model = _parse_pref(entry)
            if not prov_key:
                continue
            k = prov_key.strip().lower()
            base = by_name.get(k) or by_id.get(k)
            if base is None:
                continue
            if id(base) in seen:
                continue
            # Same availability gate the fanout uses downstream.
            try:
                ok, _reason, _cap = _r.is_eligible(
                    base, 'holdings_consensus')
            except Exception:
                ok = True
            if not ok:
                continue
            copy = dict(base)
            if model:
                copy['model'] = model
                copy['models'] = [model]   # pin (resolve_provider_model)
            picked.append(copy)
            seen.add(id(base))
            if len(picked) >= _TIER2_CAP:
                break
        return picked
    except Exception:
        return None  # fail-open


def _callability(app, provider, call_type='holdings_consensus'):
    """v4.14.5.62-tier2-validator-select: classify a provider's current
    callability for tier-2 selection.

    Returns one of:
      ('now',  0)     — eligible right now.
      ('wait', secs)  — not eligible only because of a SHORT transient
                        cooldown clearing in `secs` (<= the module wait
                        threshold); worth waiting for (keeps the voice).
      ('skip', 0)     — long-exhausted (daily cap / long cooldown) or
                        otherwise uncallable; selection should backfill.
    Never raises → ('skip', 0) on any fault (conservative: don't wait on an
    unknown state)."""
    try:
        import tm_ai_router as _r
    except Exception:
        return ('skip', 0)
    try:
        ok, _reason, _cap = _r.is_eligible(provider, call_type)
    except Exception:
        ok = True
    if ok:
        return ('now', 0)
    # Not eligible. Is it ONLY a short transient cooldown about to clear?
    try:
        import tm_provider_health as _h
        state = _h.get_state()
        if state is not None:
            pid = provider.get('id') or provider.get('name') or '?'
            in_cd, secs = state.provider_in_cooldown(pid)
            if in_cd and 0 < secs <= _WAIT_THRESHOLD_SEC:
                return ('wait', int(secs))
    except Exception:
        pass
    # Long cooldown / daily-cap exhaustion / disabled / deprecated → backfill.
    return ('skip', 0)


def _select_tier2_providers_flexible(app, providers, cfg):
    """v4.14.5.62-tier2-validator-select (flag ON): pick up to _TIER2_CAP
    DISTINCT validators from whatever the user has ENABLED — favorites first
    (the cfg['layer2_validation_models'] order, each pinned to its preferred
    model), then the user's other enabled providers to fill remaining slots.
    Favorites are a PREFERENCE/ORDERING, not a gate: never strict-skip for
    lack of favorites; only return [] when zero providers are callable at all.

    Cooldown-aware: a chosen validator in a SHORT transient cooldown (clears
    within the module wait threshold) is kept and a single bounded wait is
    taken before returning (tier-2 is background — a brief wait keeps a voice);
    a long-exhausted one (daily cap / long cooldown) is skipped so the next
    available provider BACKFILLS the slot, preserving distinctness and floating
    the voice count toward 3.

    Returns a list of 0.._TIER2_CAP provider-dict COPIES (favorites pinned).
    Empty list → genuinely nothing callable this cycle (caller skips + retries,
    same as strict). Never returns None. Never raises → falls back to the
    strict selector on any fault."""
    try:
        try:
            import tm_ai_router as _r
        except Exception:
            _r = None

        avail = [p for p in (providers or []) if isinstance(p, dict)]
        if not avail:
            return []

        # Index available providers by canonical id + name (lowercased) so we
        # can match favorites and dedup robustly by the provider's unique id.
        def _pid(p):
            return p.get('id') or p.get('name') or id(p)

        by_id, by_name = {}, {}
        for p in avail:
            cid = None
            if _r is not None:
                try:
                    cid = _r.provider_canonical_id(p)
                except Exception:
                    cid = None
            if cid:
                by_id.setdefault(str(cid).strip().lower(), p)
            nm = str(p.get('name') or '').strip().lower()
            if nm:
                by_name.setdefault(nm, p)

        # 1) Build the preference-ordered candidate list:
        #    favorites first (matched, with their pinned model), then the
        #    remaining enabled providers (no pin) in their given order.
        ordered = []          # list of (provider_dict, pinned_model_or_None)
        used_pids = set()
        prefs = cfg.get('layer2_validation_models') or []
        for entry in prefs:
            prov_key, model = _parse_pref(entry)
            if not prov_key:
                continue
            k = prov_key.strip().lower()
            base = by_name.get(k) or by_id.get(k)
            if base is None:
                continue
            bp = _pid(base)
            if bp in used_pids:
                continue
            used_pids.add(bp)
            ordered.append((base, model))
        for p in avail:                       # other enabled providers
            bp = _pid(p)
            if bp in used_pids:
                continue
            used_pids.add(bp)
            ordered.append((p, None))

        # 2) Walk the ordered candidates; select up to cap DISTINCT callable
        #    (or short-waitable) providers, skipping long-exhausted ones so the
        #    next candidate backfills the slot.
        picked = []           # provider-dict copies
        picked_pids = set()
        max_wait = 0
        for base, model in ordered:
            if len(picked) >= _TIER2_CAP:
                break
            bp = _pid(base)
            if bp in picked_pids:
                continue
            status, secs = _callability(app, base)
            if status == 'skip':
                continue       # long-exhausted → backfill from next candidate
            copy = dict(base)
            if model:
                copy['model'] = model
                copy['models'] = [model]   # pin (resolve_provider_model)
            picked.append(copy)
            picked_pids.add(bp)
            if status == 'wait':
                max_wait = max(max_wait, secs)

        # 3) If any selected validator is in a short cooldown, take ONE bounded
        #    wait (the max among them) so it's callable when the consensus
        #    rechecks eligibility at dispatch. Bounded by the threshold.
        if picked and max_wait > 0:
            _wait = min(int(max_wait), int(_WAIT_THRESHOLD_SEC)) + 1
            _log(app, f"[layer2] waiting {_wait}s for {len(picked)} validator(s) "
                      f"to clear a short cooldown (flexible select).")
            try:
                time.sleep(_wait)
            except Exception:
                pass
        return picked
    except Exception:
        # Fail-safe: fall back to the strict selector (never block validation
        # on a bug in the flexible path).
        try:
            res = _select_tier2_providers(app, providers, cfg)
            return res if res is not None else []
        except Exception:
            return []


def _ensure_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS recommend_cache_validation ("
        "ticker TEXT NOT NULL, path TEXT NOT NULL, "
        "validated_at INTEGER NOT NULL, n_models INTEGER NOT NULL, "
        "n_buy INTEGER NOT NULL, n_nonbuy INTEGER NOT NULL, "
        "n_nocall INTEGER NOT NULL, agreement_pct REAL NOT NULL, "
        "conviction_consensus TEXT, single_voice INTEGER NOT NULL, "
        "validation_score REAL NOT NULL, verdict TEXT NOT NULL, "
        "votes_json TEXT, "
        "PRIMARY KEY (ticker, path))")
    conn.commit()


def _window_seconds(app, path: str) -> int:
    """Validation-recency window. cfg override wins; else reuse the
    per-path verdict-recency window so Layer 2 cadence stays aligned
    with the analysis gate. Fail-safe to 7 days."""
    try:
        ov = (getattr(app, 'cfg', {}) or {}).get(
            'layer2_validation_recency_window_seconds')
        if ov is not None:
            return int(ov)
    except Exception:
        pass
    try:
        import tm_event_triggers as _tet
        return int(_tet.verdict_recency_window_seconds(app, path))
    except Exception:
        return 7 * 24 * 60 * 60


def _providers_available(app) -> bool:
    """Coarse 'can any AI run right now' gate (fail-OPEN). Uses the
    same authoritative scan-eligibility check fill mode uses; the
    consensus engine itself still degrades gracefully if it ends up
    with one/zero providers."""
    try:
        import tm_api_providers as _tapi
        return bool(_tapi.scan_can_run())
    except Exception:
        return True


def _ai_paused(app) -> bool:
    try:
        import tm_holdings
        return bool(tm_holdings.is_ai_paused())
    except Exception:
        return False


# ── pick selection ────────────────────────────────────────────────────

def _idle_skip_counts(app, conn) -> dict:
    """v4.14.5.14-layer2-revalidate-and-heartbeat Part B
    (2026-05-20): compute the why-is-daemon-idle counts for the
    idle-log heartbeat. Read-only; fail-safe — any error returns
    zeros with the configured window hours. Used ONLY for the log
    line, never for gating; counts a row as 'recency-fresh' iff
    its validation is within the window AND it would also pass
    the band gate (otherwise it's classified as 'band-mismatched'
    even if also fresh — the user-meaningful failure mode wins)."""
    out = {'recency': 0, 'band': 0, 'total_cache': 0,
           'window_h': 168}
    try:
        rows = conn.execute(
            "SELECT rc.ticker, rc.path, v.validated_at "
            "FROM recommend_cache rc "
            "LEFT JOIN recommend_cache_validation v "
            "  ON v.ticker = rc.ticker AND v.path = rc.path"
        ).fetchall()
    except Exception:
        return out
    out['total_cache'] = len(rows)
    now = int(time.time())
    # Window: report in hours using the first row's path as the
    # representative (most installations have a uniform global
    # override anyway). Best-effort.
    try:
        if rows:
            out['window_h'] = max(
                1, int(_window_seconds(app, rows[0][1]) / 3600))
    except Exception:
        pass
    try:
        _valid_paths = None
        try:
            import tm_holdings as _th_isc
            _valid_paths = set(_th_isc.PATHS.keys())
        except Exception:
            _valid_paths = None
        try:
            import tm_queue_runner as _qr_isc
            _filter = _qr_isc._eligibility_price_band_filter
        except Exception:
            _filter = None
        for tk, path, validated_at in rows:
            tk_u = str(tk).upper()
            path_s = str(path)
            # Stale-path rows already invisible to _select_next_pick;
            # don't count them in either bucket.
            if (_valid_paths is not None
                    and path_s not in _valid_paths):
                continue
            # Band-mismatch check (matches _select_next_pick's
            # gating order).
            band_keeps = True
            if _filter is not None:
                try:
                    band_keeps = bool(_filter(
                        app, path_s, [tk_u], 'layer2-heartbeat'))
                except Exception:
                    band_keeps = True
            if not band_keeps:
                out['band'] += 1
                continue
            # Recency check.
            if validated_at is not None:
                try:
                    if int(validated_at) >= (
                            now - _window_seconds(app, path_s)):
                        out['recency'] += 1
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    return out


def _select_next_pick(app, conn):
    """The highest-priority recommend_cache pick that needs (re)
    validation: displayed-tier before bench, newer first_seen_at
    first, skipping any whose validation is still inside its per-path
    window, and skipping anything the price-band eligibility gate
    would drop. Returns (ticker, path) or None.

    v4.14.5.14-merge-and-unify-fix Fix 5b (2026-05-19): also skips
    rows whose `path` is no longer in the current `tm_holdings.PATHS`
    set. Without this filter, a stale-path row in recommend_cache
    (e.g. an ACH penny_lottery row left over from before the merge)
    would keep getting picked up forever, even after the path was
    removed from PATHS. Read at call time so the filter reflects
    runtime mutations (e.g. apply_path_merge_v414514mu popping
    penny_lottery) without requiring a process restart. Fail-OPEN
    to "no filter" if tm_holdings can't be queried (preserves prior
    behaviour rather than blocking validation entirely).
    """
    try:
        import tm_holdings as _th_v
        _valid_paths = set(_th_v.PATHS.keys())
    except Exception:
        _valid_paths = None
    try:
        rows = conn.execute(
            "SELECT rc.ticker, rc.path, rc.tier, rc.first_seen_at, "
            "       v.validated_at "
            "FROM recommend_cache rc "
            "LEFT JOIN recommend_cache_validation v "
            "  ON v.ticker = rc.ticker AND v.path = rc.path "
            "ORDER BY CASE rc.tier WHEN 'displayed' THEN 0 ELSE 1 "
            "         END, rc.first_seen_at DESC "
            "LIMIT ?", (_CANDIDATE_SCAN_LIMIT,)).fetchall()
    except Exception as e:
        _log(app, f"[layer2] candidate query failed: "
                  f"{type(e).__name__}: {e}", 'amber')
        return None
    now = int(time.time())
    for tk, path, _tier, _fs, validated_at in rows:
        tk = str(tk).upper()
        path = str(path)
        # Fix 5b: skip stale-path rows (path no longer in PATHS).
        if (_valid_paths is not None
                and path not in _valid_paths):
            continue
        if validated_at is not None:
            try:
                if int(validated_at) >= now - _window_seconds(
                        app, path):
                    continue  # still fresh — recency gate
            except (TypeError, ValueError):
                pass
        # Price-band eligibility gate (same as Layer 1 dispatch).
        try:
            import tm_queue_runner as _qr
            keep = _qr._eligibility_price_band_filter(
                app, path, [tk], 'layer2')
            if not keep:
                continue
        except Exception:
            pass  # gate fault → fail-open, validate anyway
        return (tk, path)
    return None


# ── consensus run (reuse the EXISTING engine) ─────────────────────────

def _run_consensus_blocking(app, ticker: str, path: str):
    """Run the existing ConsensusRunner for one candidate, blocking
    until done (or timeout). Returns ('OK', result) | ('NO_MODELS',
    None) | ('NOT_READY', None) | ('TIMEOUT', None) | ('ERR', None)."""
    state = getattr(app, '_holdings_state', None) or {}
    builder = state.get('builder')
    slog = state.get('log')
    plog = state.get('predictions_log')
    if builder is None or slog is None:
        return ('NOT_READY', None)
    cfg = getattr(app, 'cfg', {}) or {}
    try:
        providers = (app._load_enabled_api_providers()
                     if hasattr(app, '_load_enabled_api_providers')
                     else [])
    except Exception:
        providers = []
    try:
        inf = (app._get_inference_settings()
               if hasattr(app, '_get_inference_settings')
               else ('hybrid', []))
        inference_mode, game_procs = inf[0], inf[1]
    except Exception:
        inference_mode, game_procs = 'hybrid', []

    models = list(cfg.get('consensus_models') or [])
    # v4.14.5.69-tier2-backfill: capture the FULL bench BEFORE any
    # cap-narrowing branch runs. Both the empty-consensus_models
    # cloud-fanout path (below) and the non-empty path (skips the
    # branch entirely) need this defined when constructing the
    # ConsensusRunner with `providers_bench=_full_bench`. Hoisting
    # avoids an UnboundLocalError on the non-empty path that doesn't
    # enter the `if not models:` block.
    _full_bench = list(providers or [])
    if not models:
        # v4.14.5.14b-layer2-fix: mirror the v4.14.5.5
        # _on_run_consensus empty-models fallback. An empty
        # consensus_models list is the DEFAULT state; the existing
        # ConsensusRunner + router fan out to the enabled cloud
        # providers exactly as the user-facing path does (identical
        # providers list, inference_mode, and
        # call_type='holdings_consensus' below — models=[] is passed
        # straight through, same as _on_run_consensus does). Without
        # this, Layer 2 dead-ends at NO_MODELS forever and never
        # writes a single recommend_cache_validation row. Guards, in
        # order:
        #   - flag off  → legacy NO_MODELS early-return (rollback).
        #   - local mode → NO_MODELS (cloud-only discipline in
        #     reverse: a user who explicitly chose local-only and
        #     configured no consensus models is NOT silently routed
        #     to cloud).
        #   - no AI available → NO_MODELS (genuinely nothing to run;
        #     the SAME has_any_ai_available gate _on_run_consensus
        #     uses, fail-open to providers presence).
        # Otherwise fall through with models=[] (cloud fanout).
        if not bool(cfg.get('use_layer2_cloud_fallback', True)):
            return ('NO_MODELS', None)
        if str(inference_mode or '').strip().lower() == 'local':
            return ('NO_MODELS', None)
        try:
            import tm_top_ai_picker
            _ai_ok = bool(
                tm_top_ai_picker.has_any_ai_available(app))
        except Exception:
            _ai_ok = bool(providers)
        if not _ai_ok:
            return ('NO_MODELS', None)
        global _CLOUD_FALLBACK_LOGGED
        # v4.14.5.14-layer2-gemini-flash: only announce a genuine FULL
        # fanout when there is NO tier-2 preference list. With a
        # non-empty layer2_validation_models the cap below narrows the
        # dispatch to <=3 preferred models, so the old "fanning out to
        # N provider(s)" line was misleading — it printed the pre-cap
        # provider count (e.g. 6) one line before the cap actually
        # dispatched 3. When the cap is active the "(cap=3, tier2)"
        # line below is the single source of truth, so suppress this.
        if (not _CLOUD_FALLBACK_LOGGED
                and not (cfg.get('layer2_validation_models') or [])):
            _CLOUD_FALLBACK_LOGGED = True
            _log(app,
                 f"[layer2] using cloud fanout — consensus_models "
                 f"empty, inference_mode="
                 f"{str(inference_mode or 'api').strip().lower()}, "
                 f"fanning out to {len(providers)} provider(s).")

        # v4.14.5.14-tier2-budget-and-unification (Part A): cap the
        # cloud fanout to <=3 larger-class models. Strict cap — if the
        # key is set but none are available right now, SKIP and retry
        # next cycle rather than falling back to the full fanout.
        #
        # v4.14.5.62-tier2-validator-select: when tier2_flexible_validators is
        # ON, select up to 3 DISTINCT validators from whatever is enabled
        # (favorites first, cooldown-aware backfill) instead of strict-skipping
        # for lack of favorites. Flag OFF → the strict selector below,
        # byte-identical. Both return the same contract (None = cap disabled /
        # full fanout; [] = nothing callable → SKIP; non-empty = use).
        # v4.14.5.69-tier2-backfill: _full_bench was hoisted above
        # the empty-consensus_models branch so both code paths reach
        # the runner with it defined. Layer 2's flexible selector
        # narrows the local `providers` variable next; the bench
        # passed to ConsensusRunner is still the pre-narrow snapshot.
        if _FLEXIBLE_VALIDATORS:
            _tier2 = _select_tier2_providers_flexible(app, providers, cfg)
        else:
            _tier2 = _select_tier2_providers(app, providers, cfg)
        if _tier2 is not None:
            if not _tier2:
                try:
                    _target = min(_TIER2_CAP, len(
                        cfg.get('layer2_validation_models') or []))
                except Exception:
                    _target = _TIER2_CAP
                if _FLEXIBLE_VALIDATORS:
                    _log(app, f"[layer2] skipped {ticker}/{path}: no enabled "
                              f"provider is callable right now (flexible "
                              f"select); will retry when one is available.",
                         'amber')
                else:
                    _log(app, f"[layer2] skipped {ticker}/{path}: 0 of "
                              f"{_target} preferred validation model(s) "
                              f"available; will retry when cooldowns clear.",
                         'amber')
                return ('SKIP', None)
            providers = _tier2
            _log(app, f"[layer2] consensus on {ticker}/{path}: "
                      f"{len(providers)} model(s) dispatched "
                      f"(cap={_TIER2_CAP}, tier2).")

    done = threading.Event()
    box: dict = {}

    def _on_all_done(res):
        box['res'] = res
        done.set()

    try:
        import tm_consensus
        runner = tm_consensus.ConsensusRunner(
            ticker=ticker,
            holding={'ticker': ticker, 'path': path},
            models=models,
            path=path,
            prompt_builder=builder,
            signals_log=slog,
            on_all_done=_on_all_done,
            log_callback=lambda m, t='muted': _log(app, m, t),
            predictions_log=plog,
            prompt_kind='fresh_buy',          # not an owned position
            providers=providers,
            # v4.14.5.69-tier2-backfill: full enabled bench (pre-
            # narrowing) so ConsensusRunner can substitute mid-run
            # when a picked validator drops.
            providers_bench=_full_bench,
            inference_mode=inference_mode,
            game_processes=game_procs,
            call_type='holdings_consensus',   # NOT scan (cap_factor)
            # v4.14.5.14-layer2-decouple (2026-05-20): suppress the
            # rollup `consensus_fresh_buy` signal write. Layer 2's
            # diagnostic value lives in `recommend_cache_validation`
            # (read by the badge); we deliberately don't write the
            # gate-signal the Recommend dialog's `_consensus_says_buy`
            # filter reads. Closes the 2026-05-19 regression where
            # daemon-written gate-signals dropped 95% of picks.
            # User-initiated Verify clicks (Look Up + Recommend pill
            # click) still write the signal — they're explicit
            # "tell me what consensus thinks and let it gate this
            # specific pick" actions.
            write_consensus_signal=False,
        )
        runner.start()
    except Exception as e:
        _log(app, f"[layer2] consensus start failed for "
                  f"{ticker}/{path}: {type(e).__name__}: {e}",
             'amber')
        return ('ERR', None)
    if not done.wait(timeout=_CONSENSUS_TIMEOUT_SECONDS):
        return ('TIMEOUT', None)
    return ('OK', box.get('res'))


# ── scoring ───────────────────────────────────────────────────────────

def _score(result: dict) -> dict:
    """Turn a consensus result envelope into a validation row. Only
    committed votes (a real direction, not skipped/error) count toward
    agreement. Single-voice (<2 distinct models) is flagged and capped
    — one model is never strong 'consensus'."""
    votes = (result or {}).get('votes') or []
    committed = []   # (model, direction, confidence)
    nocall = 0
    for v in votes:
        if not isinstance(v, dict):
            continue
        if v.get('skipped') or v.get('error'):
            continue
        d = str(v.get('direction') or '').upper().strip()
        if not d:
            nocall += 1
            continue
        committed.append((
            str(v.get('model') or '?'),
            d,
            str(v.get('confidence') or '').upper().strip()))
    n_models = len(committed)
    n_buy = sum(1 for _m, d, _c in committed if d == 'BUY')
    n_nonbuy = n_models - n_buy
    agreement = (n_buy / n_models * 100.0) if n_models else 0.0
    confs = [c for _m, _d, c in committed if c]
    if confs and all(c == confs[0] for c in confs):
        conv = confs[0]
    elif confs:
        conv = 'MIXED'
    else:
        conv = ''
    single_voice = n_models < 2

    if n_models == 0:
        score, verdict = 0.0, 'INCONCLUSIVE'
    elif single_voice:
        # One voice can corroborate weakly at best.
        score = min(60.0, agreement * 0.6)
        verdict = 'SINGLE_VOICE'
    else:
        score = agreement
        if conv == 'HIGH':
            score = min(100.0, score + 10.0)
        elif conv == 'MIXED':
            score = max(0.0, score - 10.0)
        if agreement >= 66.0:
            verdict = 'VALIDATED_BUY'
        elif agreement <= 33.0:
            verdict = 'CONTRADICTED'
        else:
            verdict = 'MIXED'

    votes_json = json.dumps(
        [{'model': m, 'direction': d, 'confidence': c}
         for m, d, c in committed][:12])
    return {
        'n_models': n_models, 'n_buy': n_buy,
        'n_nonbuy': n_nonbuy, 'n_nocall': nocall,
        'agreement_pct': round(agreement, 1),
        'conviction_consensus': conv,
        'single_voice': 1 if single_voice else 0,
        'validation_score': round(score, 1),
        'verdict': verdict, 'votes_json': votes_json}


def _persist(conn, ticker: str, path: str, sc: dict) -> None:
    conn.execute(
        "INSERT INTO recommend_cache_validation "
        "(ticker, path, validated_at, n_models, n_buy, n_nonbuy, "
        " n_nocall, agreement_pct, conviction_consensus, "
        " single_voice, validation_score, verdict, votes_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ticker, path) DO UPDATE SET "
        "validated_at=excluded.validated_at, "
        "n_models=excluded.n_models, n_buy=excluded.n_buy, "
        "n_nonbuy=excluded.n_nonbuy, n_nocall=excluded.n_nocall, "
        "agreement_pct=excluded.agreement_pct, "
        "conviction_consensus=excluded.conviction_consensus, "
        "single_voice=excluded.single_voice, "
        "validation_score=excluded.validation_score, "
        "verdict=excluded.verdict, votes_json=excluded.votes_json",
        (ticker, path, int(time.time()), sc['n_models'], sc['n_buy'],
         sc['n_nonbuy'], sc['n_nocall'], sc['agreement_pct'],
         sc['conviction_consensus'], sc['single_voice'],
         sc['validation_score'], sc['verdict'], sc['votes_json']))
    conn.commit()


# ── daemon loop ───────────────────────────────────────────────────────

def _loop(app, stop_event, interval: int) -> None:
    _no_model_logged = False
    _pause_logged = False
    # v4.14.5.14-layer2-revalidate-and-heartbeat Part B
    # (2026-05-20): idle-log heartbeat. The `pick is None` branch
    # below used to silently wait + loop, which the 2026-05-20
    # investigation found indistinguishable from a daemon crash
    # (cost Mike 2.5h of "is this thing alive?" uncertainty after
    # the thesis-validation restart). `_idle_logged_at` mirrors
    # the existing one-shot patterns (`_no_model_logged`,
    # `_pause_logged`): log once on transition into idle, then
    # heartbeat every hour while idle, reset on any non-None pick.
    _idle_logged_at: float | None = None
    _IDLE_HEARTBEAT_SECONDS = 3600.0
    # v4.14.5.14-tier2-budget-and-unification (Part C): hourly Layer-2
    # burn metric, emitted only when the window saw activity (no idle
    # spam). Layer-1 unified-scan call/record counts already surface in
    # the existing `[unified-scan]` lines, so this measures the Layer-2
    # cap lever specifically.
    _bm_validations = 0
    _bm_models_sum = 0
    _bm_window_start = time.time()
    _BURN_METRICS_SECONDS = 3600.0
    # v4.14.6.35-fix-startup-stampede: per-daemon startup grace.
    # Layer-2 is the HEAVIEST single burst on the post-paint window —
    # a single first tick dispatches a 3-AI cloud consensus. Pre-
    # v4.14.6.35 it fired at t=0 alongside news / fundfile / queue-
    # runner / recommend-cache, contending main thread + GIL + AI
    # providers (Gemini hit its per-minute cap 34s into startup as
    # direct evidence). 60s grace lets the UI settle + lets the
    # other daemons' lighter first ticks land first. The wait is
    # interruptible by stop_event so shutdown during grace returns
    # immediately (matters for the v4.14.6.34 async-close path).
    if stop_event.wait(60.0):
        return
    while not stop_event.is_set():
        sleep_for = interval
        _now_bm = time.time()
        if (_now_bm - _bm_window_start) >= _BURN_METRICS_SECONDS:
            if _bm_validations > 0:
                _avg = _bm_models_sum / _bm_validations
                _log(app, f"[burn-metrics] Layer2: {_bm_validations} "
                          f"validation(s) in last hour, avg "
                          f"{_avg:.1f} model(s)/validation "
                          f"(cap={_TIER2_CAP}).")
            _bm_validations = 0
            _bm_models_sum = 0
            _bm_window_start = _now_bm
        try:
            cfg = getattr(app, 'cfg', {}) or {}
            if not bool(cfg.get('use_layer2_validation', True)):
                # Flag flipped off at runtime — go dormant cheaply.
                stop_event.wait(interval)
                continue
            if _ai_paused(app):
                stop_event.wait(interval)
                continue
            conn = _conn(app)
            if conn is None:
                stop_event.wait(interval)
                continue
            _ensure_table(conn)
            if not _providers_available(app):
                if not _pause_logged:
                    _pause_logged = True
                    _log(app, "[layer2] paused — no AI provider can "
                              "run right now; will resume when one "
                              "recovers.")
                stop_event.wait(interval)
                continue
            if _pause_logged:
                _pause_logged = False
                _log(app, "[layer2] resumed — a provider is "
                          "available again.")
            pick = _select_next_pick(app, conn)
            if pick is None:
                # v4.14.5.14-layer2-revalidate-and-heartbeat Part B
                # idle-log: first transition into idle OR hourly
                # heartbeat while idle. Skip-reason counts pulled
                # from a quick read so future "daemon silent"
                # investigations see WHY the daemon is idle in one
                # line. Fail-safe: any error in the count read
                # → still emit the line with empty counts.
                now_ts = time.time()
                _should_log = (
                    _idle_logged_at is None
                    or (now_ts - _idle_logged_at)
                    >= _IDLE_HEARTBEAT_SECONDS)
                if _should_log:
                    try:
                        _skip = _idle_skip_counts(app, conn)
                    except Exception:
                        _skip = {'recency': 0, 'band': 0,
                                  'total_cache': 0,
                                  'window_h': 168}
                    try:
                        _log(app,
                             f"[layer2] idle — no candidates "
                             f"eligible for validation. "
                             f"{_skip.get('recency', 0)} picks "
                             f"fresh-validated within last "
                             f"{int(_skip.get('window_h', 168))}h, "
                             f"{_skip.get('band', 0)} picks "
                             f"band-mismatched (of "
                             f"{_skip.get('total_cache', 0)} "
                             f"recommend_cache rows). Next "
                             f"check in {interval}s.")
                    except Exception:
                        pass
                    _idle_logged_at = now_ts
                stop_event.wait(interval)
                continue
            # Non-None pick: reset idle-log flag so the next idle
            # transition re-logs immediately rather than waiting an
            # hour. Mirrors the existing _no_model_logged reset
            # pattern below.
            _idle_logged_at = None
            tk, path = pick
            status, res = _run_consensus_blocking(app, tk, path)
            if status == 'NO_MODELS':
                if not _no_model_logged:
                    _no_model_logged = True
                    _log(app, "[layer2] idle — no consensus_models "
                              "configured; nothing to validate with.")
                sleep_for = max(interval, _NO_MODELS_BACKOFF)
                stop_event.wait(sleep_for)
                continue
            if status == 'SKIP':
                # v4.14.5.14-tier2-budget-and-unification (Part A):
                # 0 preferred Tier-2 models available. Reason already
                # logged inside _run_consensus_blocking. Don't persist
                # — the pick stays eligible and retries next cycle when
                # a preferred model's cooldown clears.
                stop_event.wait(interval)
                continue
            _no_model_logged = False
            if status != 'OK' or res is None:
                _log(app, f"[layer2] {tk}/{path}: consensus "
                          f"{status.lower()} — will retry next cycle.",
                     'amber')
                stop_event.wait(interval)
                continue
            sc = _score(res)
            _persist(conn, tk, path, sc)
            # v4.14.5.62-validated-accuracy: ADDITIVELY stamp the validation
            # result onto the prediction record this pick came from — the
            # SAME most-recent (ticker, path) record recommend_cache built the
            # pick from — so accuracy can later count only tier-2-validated
            # picks. Unconditional (additive + harmless; only the accuracy
            # FILTER is flag-gated). Best-effort: a missing record (it closed
            # between selection and now) or any fault just skips the stamp.
            try:
                _plog = (getattr(app, '_holdings_state', None)
                         or {}).get('predictions_log')
                if _plog is not None:
                    _rec = _plog.get_most_recent_for_ticker_and_path(tk, path)
                    if _rec is not None and _rec.get('id'):
                        _plog.patch_record(_rec['id'], {'tier2_validation': {
                            'verdict': sc.get('verdict'),
                            'validation_score': sc.get('validation_score'),
                            'n_models': sc.get('n_models'),
                            'single_voice': sc.get('single_voice'),
                            'validated_at': datetime.now().isoformat(
                                timespec='seconds'),
                        }})
            except Exception as _se:
                _log(app, f"[layer2] stamp skipped for {tk}/{path}: "
                          f"{type(_se).__name__}", 'muted')
            _bm_validations += 1
            _bm_models_sum += int(sc.get('n_models') or 0)
            _log(app,
                 f"[layer2] validated {tk}/{path}: {sc['verdict']} "
                 f"score={sc['validation_score']} "
                 f"({sc['n_buy']}/{sc['n_models']} BUY, "
                 f"{sc['n_nocall']} no-call"
                 f"{', SINGLE-VOICE' if sc['single_voice'] else ''}"
                 f"{', conv=' + sc['conviction_consensus'] if sc['conviction_consensus'] else ''})")
        except Exception as e:
            _log(app, f"[layer2] tick error (daemon stays up): "
                      f"{type(e).__name__}: {e}", 'amber')
        stop_event.wait(sleep_for)


def launch_layer2_validation(app):
    """Idempotent. Returns the daemon thread, or None if disabled /
    already running / launch failed. Mirrors the recommend_cache /
    news / fundfile launchers; the App startup hook logs the
    'daemon started' line (consistent with the others)."""
    try:
        cfg = getattr(app, 'cfg', {}) or {}
        if not bool(cfg.get('use_layer2_validation', True)):
            return None
        ex = getattr(app, '_layer2_thread', None)
        if ex is not None and ex.is_alive():
            return None  # already running (Resume re-entry guard)
        stop = getattr(app, '_layer2_stop', None)
        if stop is None:
            stop = threading.Event()
            app._layer2_stop = stop
        stop.clear()
        try:
            interval = int(cfg.get(
                'layer2_validation_interval_seconds',
                _LAYER2_DEFAULT_INTERVAL))
        except (TypeError, ValueError):
            interval = _LAYER2_DEFAULT_INTERVAL
        if interval < 30:
            interval = 30  # never a tight loop
        t = threading.Thread(
            target=_loop, args=(app, stop, interval),
            daemon=True, name='layer2-validation')
        app._layer2_thread = t
        t.start()
        return t
    except Exception as e:
        _log(app, f"[layer2] launch failed (non-fatal): "
                  f"{type(e).__name__}: {e}", 'amber')
        return None
