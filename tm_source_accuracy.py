"""
tm_source_accuracy.py — accuracy → source-weight bridge (v4.14.3).

What this is:
    The missing connector that activates the dormant infrastructure
    from stages 6 and 7. Reads closed predictions out of
    PredictionsLog, computes per-model accuracy, and writes scores
    into the source_weights table.

    Stage 6 built the schema and read API. Stage 7 built prompt
    rendering hooks that fire when scores differentiate. Neither
    knew where the scores came from. This module is where they
    come from.

Scope (v4.14.3):
    - Per-(model, context, ticker) accuracy from BUY closures.
    - Wilson 95% confidence intervals (stored as integer percent
      bounds — confidence_band_low / confidence_band_high).
    - Score mapping: accuracy + sample_size -> within_tier_score.
    - Cooldown-guarded triggers (5-min minimum gap between runs).
    - Idempotent UPSERT into source_weights.

Explicitly out of scope (deferred to later stages):
    - State transitions (active <-> watched <-> removed) firing
      automatically. We compute and store within_tier_score; the
      state field is updated to match the boundary the score lands
      in, but no extra action is taken when a source crosses a
      boundary.
    - Per-data-source attribution. predictions.jsonl currently
      records 'model' (the AI that voted) but NOT which specific
      news / social sources informed each prediction. So this stage
      measures MODEL accuracy (tier 'M'), not data-source accuracy.
      A future stage extending the prediction schema unlocks per-
      data-source measurement.
    - Decay. Old predictions count the same as new ones today.
    - Per-context scoring beyond per-path (e.g. high_variance vs
      standard) — needs a context detector that doesn't exist yet.

Accuracy definition:
    Only BUY predictions count toward accuracy. A BUY whose status
    is 'target_hit' is a hit; 'stop_hit' is a miss; 'expired' /
    'superseded' / 'contradicted' are excluded from the
    accuracy_rate denominator (they're decisions about the
    prediction's lifecycle, not about whether the directional call
    was right).

    accuracy_rate = target_hits / (target_hits + stop_hits)

    With small samples this number is volatile — the Wilson score
    interval gives realistic uncertainty bounds.
"""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from datetime import datetime
from statistics import NormalDist
from typing import Any, Optional

import tm_source_weights as sw


# ─── Cooldown ─────────────────────────────────────────────────────────
#
# The bridge can be triggered multiple times in quick succession
# (startup closer + first auto-refresh tick + subsequent ticks). Most
# of those runs find nothing new. A 5-minute minimum gap between runs
# avoids hammering the work loop without losing meaningful freshness.

COOLDOWN_SEC = 5 * 60

_last_run_at: float = 0.0
_run_lock = threading.Lock()


def _cooldown_ok() -> bool:
    """True if enough time has passed since the last run."""
    with _run_lock:
        return (time.time() - _last_run_at) >= COOLDOWN_SEC


def _stamp_run() -> None:
    with _run_lock:
        global _last_run_at
        _last_run_at = time.time()


# ─── Wilson score interval ────────────────────────────────────────────

def compute_wilson_ci(
    hits: int, total: int, confidence: float = 0.95,
) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Returns (low_bound, high_bound) in [0.0, 1.0]. Wilson is a better
    fit for small samples than the naive normal approximation —
    handles edge cases at p=0 and p=1 cleanly and stays inside [0,1]
    by construction.
    """
    if total <= 0 or hits < 0 or hits > total:
        return (0.0, 0.0)

    if confidence == 0.95:
        z = 1.96
    elif confidence == 0.90:
        z = 1.645
    elif confidence == 0.99:
        z = 2.576
    else:
        # Two-sided z-score from inverse normal CDF.
        z = NormalDist().inv_cdf(1.0 - (1.0 - confidence) / 2.0)

    p = hits / total
    n = total
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    margin = (
        z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    ) / denom
    low = max(0.0, center - margin)
    high = min(1.0, center + margin)
    return (low, high)


# ─── Score mapping ────────────────────────────────────────────────────

INSUFFICIENT_DATA_THRESHOLD = 10  # below this, return DEFAULT (5)

# v4.14.5.60-speculative-accuracy-exclude: Speculative/lottery is a deliberate
# gamble with a poor, low-data track record. Its closed BUYs must NOT drag the
# HEADLINE per-model accuracy (shown to users) or the consensus vote-weighting —
# so they're excluded from the global per-model + per-ticker accuracy and from
# the canonical weight map. The PER-PATH accuracy is KEPT (so the Speculative
# banner's own honest track-record stats still compute). penny_lottery is
# included for completeness (merged into lottery, but old rows may carry it).
_SPECULATIVE_PATHS = {'lottery', 'penny_lottery'}

# ─── v4.14.5.62-validated-accuracy: tier-2-validated attribution ──────
# Accuracy was tier-blind + source-blind: it counted EVERY closed tier-1
# BUY, with no idea which picks tier 2 actually validated. The fix stamps
# `tier2_validation` onto the prediction record when tier 2 validates it
# (done in tm_layer2_validation), and gates every accuracy/hit-rate
# surface through ONE shared predicate, `is_attributable`, so the
# headline accuracy, vote weights, track-record display and realized
# rollup all agree on the same population.
#
# Gating is governed by a SINGLE module flag, _ATTRIBUTABLE_ONLY, set
# once from cfg['use_validated_accuracy'] at startup (and on Settings
# save). When the flag is OFF (the default), is_attributable() ALWAYS
# returns True, so every surface that ANDs it in is byte-identical to
# pre-patch — no params threaded through callers, no drift. When ON,
# only records that were app-recommended AND tier-2-validated as a real
# consensus BUY count. The stamping itself runs unconditionally (it's
# additive + harmless); only this filtering is flag-gated.
_VERDICT_VALIDATED_BUY = 'VALIDATED_BUY'   # mirrors tm_layer2_validation._score
_APP_RECOMMENDED_SOURCES = {'queue_runner', 'discover_scan'}

_ATTRIBUTABLE_ONLY = False


def set_attributable_only(enabled: bool) -> None:
    """Set the module-wide attribution gate from cfg['use_validated_accuracy'].
    Call once at startup and on Settings save. Default OFF = today's
    tier-blind behaviour."""
    global _ATTRIBUTABLE_ONLY
    _ATTRIBUTABLE_ONLY = bool(enabled)


def attributable_only_enabled() -> bool:
    return _ATTRIBUTABLE_ONLY


def is_attributable(p) -> bool:
    """True iff this prediction record should COUNT toward accuracy.

    Flag OFF (default) → ALWAYS True (byte-identical to pre-patch: a
    surface doing `... and is_attributable(p)` is unchanged). Flag ON →
    only records that were app-recommended (source in queue_runner /
    discover_scan) AND carry a tier-2 validation stamp whose verdict is
    VALIDATED_BUY. SINGLE_VOICE / MIXED / CONTRADICTED / INCONCLUSIVE /
    unstamped / user-originated all return False. Never raises."""
    if not _ATTRIBUTABLE_ONLY:
        return True
    try:
        tv = p.get('tier2_validation')
        if not isinstance(tv, dict):
            return False
        if tv.get('verdict') != _VERDICT_VALIDATED_BUY:
            return False
        # Belt-and-suspenders: a tier-2 stamp already implies the pick came
        # through recommend_cache (queue_runner), but guard the source too.
        if (p.get('source') or '') not in _APP_RECOMMENDED_SOURCES:
            return False
        return True
    except Exception:
        return False


def map_accuracy_to_within_tier_score(
    accuracy_rate: float, sample_size: int,
) -> int:
    """Map an accuracy rate (0.0-1.0) + sample size to a within-tier
    score on the stage-6 1-15 scale.

      1-3   best (>= 70%)
      4-5   strong (60-70%)
      6-7   solidly active (55-60%)
      8     active threshold (50-55%)
      9-10  just-watched (45-50%)
      11-13 watched, lower confidence (40-45%)
      14-15 removed candidates (< 40%)

    Within each band, larger sample sizes pull toward the better end
    of the band. With < INSUFFICIENT_DATA_THRESHOLD samples the
    function returns DEFAULT_WITHIN_TIER (5) — we don't have enough
    data to differentiate.
    """
    if sample_size < INSUFFICIENT_DATA_THRESHOLD:
        return sw.DEFAULT_WITHIN_TIER

    a = accuracy_rate
    n = sample_size

    # ≥ 0.70 — best band
    if a >= 0.70:
        if n >= 30:
            return 1
        if n >= 20:
            return 2
        return 3

    # 0.60-0.70 — strong
    if a >= 0.60:
        return 4 if n >= 20 else 5

    # 0.55-0.60 — solidly active
    if a >= 0.55:
        return 6 if n >= 20 else 7

    # 0.50-0.55 — at the active threshold
    if a >= 0.50:
        return 8

    # 0.45-0.50 — just-watched
    if a >= 0.45:
        return 9 if n >= 20 else 10

    # 0.40-0.45 — watched, lower confidence
    if a >= 0.40:
        if n >= 30:
            return 11
        if n >= 20:
            return 12
        return 13

    # < 0.40 — removed
    return 14 if n >= 20 else 15


# ─── Accuracy computation ─────────────────────────────────────────────

# Statuses that close a prediction without informing accuracy.
# These are decisions about the prediction's lifecycle, not about
# whether the directional call was right.
_NON_ACCURACY_CLOSURES = {'expired', 'superseded', 'contradicted'}


def _accuracy_record_template() -> dict:
    return {
        'hit_count':         0,
        'miss_count':        0,
        'expired_count':     0,
        'superseded_count':  0,
        'contradicted_count': 0,
    }


def _finalize_record(rec: dict) -> dict:
    """Compute derived fields (sample_size, accuracy_rate, CI) from
    the raw counters."""
    target = rec['hit_count']
    stop = rec['miss_count']
    decided = target + stop
    expired = rec['expired_count']
    super_ = rec['superseded_count']
    contra = rec['contradicted_count']

    # sample_size = decided BUYs (target+stop). Other closure types
    # are tracked but don't enter the accuracy denominator.
    rec['sample_size'] = decided
    rec['total_closed'] = decided + expired + super_ + contra

    if decided > 0:
        rec['accuracy_rate'] = target / decided
    else:
        rec['accuracy_rate'] = None

    if decided >= INSUFFICIENT_DATA_THRESHOLD:
        low, high = compute_wilson_ci(target, decided, confidence=0.95)
        rec['confidence_band_low'] = round(low * 100)
        rec['confidence_band_high'] = round(high * 100)
    else:
        rec['confidence_band_low'] = None
        rec['confidence_band_high'] = None

    rec['within_tier_score'] = map_accuracy_to_within_tier_score(
        rec['accuracy_rate'] if rec['accuracy_rate'] is not None
        else 0.5,
        rec['sample_size'],
    )
    return rec


def _bump_record(rec: dict, status: str) -> None:
    """Increment the right counter on an accuracy record template."""
    if status == 'target_hit':
        rec['hit_count'] += 1
    elif status == 'stop_hit':
        rec['miss_count'] += 1
    elif status == 'expired':
        rec['expired_count'] += 1
    elif status == 'superseded':
        rec['superseded_count'] += 1
    elif status == 'contradicted':
        rec['contradicted_count'] += 1


def effective_canonical(rec: Any) -> Optional[str]:
    """v4.14.5.24: the stable model-identity key for accuracy at CANONICAL
    grain, computed IDENTICALLY on the compute side and the vote-lookup side
    so they always agree. Returns `canonical_model` when the registry knew the
    model (alias-free, separates a provider's small vs large models); else a
    stable `unknown/<provider>/<model>` id (still per-real-model, just not
    registry-named); else None (caller falls back to the display-label weight
    for old records that predate these fields). Pure function of the record's
    fields — no DB / registry call, so it's safe in the DB-free runner path."""
    try:
        cm = (rec.get('canonical_model') or '').strip()
        if cm:
            return cm
        ap = (rec.get('actual_provider') or '').strip()
        ams = (rec.get('actual_model_string') or '').strip()
        if ap and ams:
            return f"unknown/{ap}/{ams}"
    except Exception:
        pass
    return None


def compute_canonical_weight_map(predictions_log: Any) -> dict:
    """v4.14.5.24-accuracy-canonical-grain: consensus vote-weights at
    CANONICAL-MODEL grain, computed FRESH from predictions.jsonl (the source of
    truth — it carries canonical_model / actual_provider / actual_model_string).

    This deliberately does NOT read or write `source_weights`: that table stays
    display-label-keyed so its existing consumers (the provider picker's
    Wilson-CI ranking, the Track Record per-model matrix, the source-quality
    line) keep working byte-identically. The consensus weighting gets its own
    canonical-grain view here instead.

    Returns {canonical: weight, f'{canonical}@@{provider}': weight} in [1,9],
    with the hierarchical rule folded in: a (canonical, provider) bucket uses
    its OWN Wilson-lower-bound weight when mature (sample_size >=
    INSUFFICIENT_DATA_THRESHOLD), else falls back to the canonical-model rollup
    (all providers combined); thin canonicals are omitted entirely so the
    caller resolves a missing key to NEUTRAL_WEIGHT. Same Wilson math as the
    bridge (reuses _finalize_record / compute_wilson_ci) — consumes, doesn't
    reimplement. Decided-BUY accuracy only (target_hit/stop_hit), matching
    compute_model_accuracy's denominator. Never raises; {} on any failure ->
    caller degrades to the display-label weights (v4.14.5.23 behaviour)."""
    try:
        all_preds = predictions_log.get_all_full(timeout=30.0) or []
    except Exception:
        return {}
    from collections import defaultdict
    canon: dict = defaultdict(_accuracy_record_template)
    canon_prov: dict = defaultdict(_accuracy_record_template)
    _CLOSED = ('target_hit', 'stop_hit', 'expired',
               'superseded', 'contradicted')
    for p in all_preds:
        try:
            if (p.get('direction') or '').upper() != 'BUY':
                continue
            st = p.get('status')
            if st not in _CLOSED:
                continue
            # v4.14.5.60: Speculative/lottery excluded from the consensus weight
            # map too — a model's poor speculative track record must not lower
            # its vote weight on the real (non-speculative) paths. (Weight-map
            # sanity check: this is the live canonical-grain weighting; the
            # display-label build_weight_map already inherits the exclusion via
            # compute_model_accuracy's now-filtered global key.)
            if (p.get('path') or '').strip().lower() in _SPECULATIVE_PATHS:
                continue
            # v4.14.5.62-validated-accuracy: vote-weights also reflect only
            # validated picks when the flag is ON (no-op when OFF).
            if not is_attributable(p):
                continue
            c = effective_canonical(p)
            if not c:
                continue
            _bump_record(canon[c], st)
            prov = (p.get('actual_provider') or '').strip()
            if prov:
                _bump_record(canon_prov[(c, prov)], st)
        except Exception:
            continue

    def _weight(rec: dict):
        rec = _finalize_record(rec)
        if rec['sample_size'] < INSUFFICIENT_DATA_THRESHOLD:
            return None
        cbl = rec.get('confidence_band_low')
        if cbl is None:
            return None
        w = float(cbl) / 10.0
        if w < FLOOR_WEIGHT:
            return FLOOR_WEIGHT
        if w > CEILING_WEIGHT:
            return CEILING_WEIGHT
        return w

    out: dict = {}
    canon_w = {c: _weight(r) for c, r in canon.items()}
    for c, w in canon_w.items():
        if w is not None:
            out[c] = w
    for (c, prov), r in canon_prov.items():
        wp = _weight(r)
        final = wp if wp is not None else canon_w.get(c)
        if final is not None:
            out[f"{c}@@{prov}"] = final
    return out


def compute_model_accuracy(predictions_log: Any) -> dict:
    """Compute per-model accuracy stats from closed predictions.

    Returns a dict keyed by 3-tuple (model, context_id, ticker)
    where context_id is either '__global__' or a path name, and
    ticker is either '__global__' or a real ticker.

    Three groupings populated:
      - (model, '__global__', '__global__')  — overall model accuracy
      - (model, path,        '__global__')   — per-path accuracy
      - (model, '__global__', ticker)        — per-ticker, gated on
                                                sample_size >= 5

    Each value dict has counters + derived fields:
      hit_count, miss_count, expired_count, superseded_count,
      contradicted_count, sample_size, accuracy_rate,
      confidence_band_low, confidence_band_high, within_tier_score,
      total_closed.
    """
    try:
        all_preds = predictions_log.get_all_full(timeout=30.0) or []
    except Exception:
        return {}

    # Closed BUYs only — the only kind we can score on accuracy.
    # AVOID/WATCH/HOLD/NO_CALL don't have target/stop semantics, so
    # we can't say whether they were "right."
    # TODO(watch-tiers): exclude Speculative/lottery-tier predictions from
    # accuracy/track-record scoring — deferred to the scoring build. A sub-$5
    # lottery play is a low-probability bet and shouldn't drag the win-rate
    # stat. Watched (loosely), but not graded. The exclusion would add, to the
    # buy_closed filter below, something like:
    #   and tm_holdings.get_path_track(p.get('path')) != 'speculative'
    # Do NOT enable here — Build 2 (tier-timeframe) intentionally leaves
    # scoring untouched.
    buy_closed = [
        p for p in all_preds
        if (p.get('direction') or '').upper() == 'BUY'
        and p.get('status') in (
            'target_hit', 'stop_hit', 'expired',
            'superseded', 'contradicted',
        )
        # v4.14.5.62-validated-accuracy: when the flag is ON, count only
        # tier-2-validated app picks. No-op (always True) when OFF.
        and is_attributable(p)
    ]

    # 3-way grouping bucket.
    buckets: dict[tuple, dict] = {}

    def _bump(key: tuple, status: str) -> None:
        rec = buckets.setdefault(key, _accuracy_record_template())
        if status == 'target_hit':
            rec['hit_count'] += 1
        elif status == 'stop_hit':
            rec['miss_count'] += 1
        elif status == 'expired':
            rec['expired_count'] += 1
        elif status == 'superseded':
            rec['superseded_count'] += 1
        elif status == 'contradicted':
            rec['contradicted_count'] += 1

    for p in buy_closed:
        model = (p.get('model') or '').strip()
        if not model:
            continue
        path = (p.get('path') or '').strip().lower() or None
        ticker = (p.get('ticker') or '').strip().upper() or None
        status = p.get('status')
        # v4.14.5.60: Speculative/lottery is excluded from the HEADLINE (global)
        # and per-ticker accuracy so its poor track record can't drag the real
        # numbers shown to users. Per-path (Grouping 2) is KEPT, so the
        # Speculative banner's own honest stats still compute.
        is_spec = path in _SPECULATIVE_PATHS

        # Grouping 1: global per-model — EXCLUDE Speculative.
        if not is_spec:
            _bump((model, sw.GLOBAL_KEY, sw.GLOBAL_KEY), status)

        # Grouping 2: per-path per-model (only if path present) — KEEP all paths.
        if path:
            _bump((model, path, sw.GLOBAL_KEY), status)

        # Grouping 3: per-ticker per-model — EXCLUDE Speculative (consistency).
        if ticker and not is_spec:
            _bump((model, sw.GLOBAL_KEY, ticker), status)

    # Finalize derived fields. Then prune per-ticker entries below
    # the per-ticker sample threshold (5 decided).
    finalized: dict[tuple, dict] = {}
    for key, rec in buckets.items():
        rec = _finalize_record(rec)
        model, ctx, ticker = key
        # Per-ticker rows: gate on sample_size >= 5.
        if ticker != sw.GLOBAL_KEY and rec['sample_size'] < 5:
            continue
        finalized[key] = rec
    return finalized


# ─── Bridge: compute → write ──────────────────────────────────────────

def update_source_weights_from_accuracy(
    conn: sqlite3.Connection,
    predictions_log: Any,
    *,
    dry_run: bool = False,
    respect_cooldown: bool = True,
) -> dict:
    """Read closed predictions, compute accuracy per (model, context,
    ticker), UPSERT into source_weights.

    Args:
        conn: open sqlite3.Connection to tired_market.db.
        predictions_log: PredictionsLog instance (from tm_discover).
        dry_run: if True, compute but skip the writes.
        respect_cooldown: if True (default), no-op when the last run
            was within COOLDOWN_SEC ago.

    Returns a summary dict:
        {
          'rows_updated': int,
          'rows_inserted': int,
          'models_skipped_unregistered': int,
          'models_skipped_insufficient_data': int,
          'cooldown_skipped': bool,
          'computed_records': int,
          'timestamp': iso str,
        }
    """
    out = {
        'rows_updated': 0,
        'rows_inserted': 0,
        'models_skipped_unregistered': 0,
        'models_skipped_insufficient_data': 0,
        'cooldown_skipped': False,
        'computed_records': 0,
        'timestamp': datetime.now().isoformat(timespec='seconds'),
    }

    if respect_cooldown and not _cooldown_ok():
        out['cooldown_skipped'] = True
        return out

    # v4.14.5.14-db-concurrency: stamp the cooldown HERE (right after the
    # check passes), not at the end of the run. The old "stamp at end" path
    # left a TOCTOU window: between the cooldown-OK check and the stamp,
    # `compute_model_accuracy` + the SQL loop run (could be ~1s), during
    # which a second concurrent caller (auto-refresh tick during the startup
    # closer's run) would also see "cooldown OK" and start a second bridge
    # run on the same Connection → SQLITE_MISUSE race. Stamping early shuts
    # that window; it also stops a failed run from immediately retrying
    # (which was wasteful — the now-removed late stamp).
    if not dry_run:
        _stamp_run()

    accuracy = compute_model_accuracy(predictions_log)
    out['computed_records'] = len(accuracy)
    if not accuracy:
        return out

    now = datetime.now().isoformat(timespec='seconds')

    # Track unique models we encountered for skip accounting.
    seen_unregistered: set[str] = set()

    for (model, context_id, ticker), rec in accuracy.items():
        # Only registered models get written.
        if not sw.is_model(model):
            seen_unregistered.add(model)
            continue

        # Below INSUFFICIENT_DATA_THRESHOLD we'd write the default
        # score (5) — which is what the row already has from the
        # initialization migration. Skip to avoid a no-op UPSERT
        # storm. The exception: if the row genuinely doesn't exist
        # yet (per-path / per-ticker grouping), we DO want to insert
        # it so the row appears with sample_size > 0 even if the
        # within_tier_score is still 5.
        global_default_row = (
            context_id == sw.GLOBAL_KEY and ticker == sw.GLOBAL_KEY
        )
        if (rec['sample_size'] < INSUFFICIENT_DATA_THRESHOLD
                and global_default_row):
            out['models_skipped_insufficient_data'] += 1
            continue

        score = rec['within_tier_score']
        state = sw._state_for_score(score)
        tier = sw.tier_for(model)  # 'M'
        cb_low = rec['confidence_band_low']
        cb_high = rec['confidence_band_high']
        sample_size = rec['sample_size']

        if dry_run:
            continue

        # UPSERT. Detect whether this row existed pre-write.
        existing = conn.execute(
            "SELECT 1 FROM source_weights "
            "WHERE source_id=? AND context_id=? AND ticker=?",
            (model, context_id, ticker),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE source_weights
                SET category_tier=?, within_tier_score=?, state=?,
                    sample_size=?, last_updated=?,
                    confidence_band_low=?, confidence_band_high=?
                WHERE source_id=? AND context_id=? AND ticker=?
                """,
                (tier, score, state, sample_size, now,
                 cb_low, cb_high,
                 model, context_id, ticker),
            )
            out['rows_updated'] += 1
        else:
            conn.execute(
                """
                INSERT INTO source_weights (
                    source_id, context_id, ticker,
                    category_tier, within_tier_score, state,
                    sample_size, last_updated,
                    confidence_band_low, confidence_band_high
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (model, context_id, ticker,
                 tier, score, state,
                 sample_size, now, cb_low, cb_high),
            )
            out['rows_inserted'] += 1

    if not dry_run:
        conn.commit()
        # v4.14.5.14-db-concurrency: _stamp_run was MOVED to bridge start
        # (right after the cooldown check) to close the TOCTOU window.
        # Only the commit remains here.

    out['models_skipped_unregistered'] = len(seen_unregistered)
    out['unregistered_model_names'] = sorted(seen_unregistered)
    return out


# ─── Human-readable summary ──────────────────────────────────────────

def get_accuracy_summary(
    conn: sqlite3.Connection, predictions_log: Any,
) -> dict:
    """Return a human-readable summary of current source-weight state
    for tier-M (model) sources.

    Reads the live source_weights table — does NOT recompute. Useful
    for debugging and future UI surfacing.
    """
    rows = conn.execute(
        "SELECT source_id, context_id, ticker, category_tier, "
        "within_tier_score, state, sample_size, "
        "confidence_band_low, confidence_band_high, last_updated "
        "FROM source_weights WHERE category_tier=? "
        "ORDER BY within_tier_score, sample_size DESC",
        ('M',),
    ).fetchall()

    breakdown = []
    counts = {'active': 0, 'watched': 0, 'removed': 0}
    for r in rows:
        info = {
            'source_id':            r[0],
            'context_id':           r[1],
            'ticker':               r[2],
            'category_tier':        r[3],
            'within_tier_score':    r[4],
            'state':                r[5],
            'sample_size':          r[6],
            'confidence_band_low':  r[7],
            'confidence_band_high': r[8],
            'last_updated':         r[9],
        }
        breakdown.append(info)
        st = info['state']
        counts[st] = counts.get(st, 0) + 1

    # Total unique tier-M source_ids in the registry (both ones we've
    # measured and ones that have only the seed default row).
    total_models_tracked = len(
        {r['source_id'] for r in breakdown}
    )

    return {
        'total_models_tracked':           total_models_tracked,
        'models_above_threshold':         counts.get('active', 0),
        'models_at_watch_threshold':      counts.get('watched', 0),
        'models_below_threshold':         counts.get('removed', 0),
        'per_model_breakdown':            breakdown,
    }


# ─── v4.14.5.19-accuracy-weighted-consensus: vote weighting ──────────
#
# Consumes the Wilson-lower-bound accuracy already computed above and
# stored in source_weights.confidence_band_low. The consensus engine
# (tm_consensus._finalize and its display sites) calls these helpers to
# weight each model's vote.
#
# Rails (the user's design, locked):
#   - Scale 0-10, clamped to [1.0, 9.0]. No model silenced (floor 1);
#     no model the sole voice (ceiling 9 < two neutrals' 10, so one
#     accurate model can't unilaterally override).
#   - No/thin data (sample_size < INSUFFICIENT_DATA_THRESHOLD) -> 5.0
#     neutral. This means a brand-new install (every model n=0) gets
#     every weight == 5.0 -> weighted tally == flat tally (correct
#     cold-start; weighting code path stays correct/honest at every
#     data-maturity stage).
#   - Missing / lookup error / null -> 5.0 neutral. Never raise, never
#     return 0. A consensus must not crash or silently zero a vote
#     because accuracy data is missing -- missing == neutral.
#   - Strictly positive multipliers. Low accuracy means "trust less"
#     (toward floor 1), NEVER "trust the opposite" (no inverse /
#     contrarian weighting -- separate someday-maybe).

NEUTRAL_WEIGHT = 5.0
FLOOR_WEIGHT = 1.0
CEILING_WEIGHT = 9.0


def model_consensus_weight(
    conn: sqlite3.Connection,
    model_label: str,
    context_id: str = sw.GLOBAL_KEY,
    ticker: str = sw.GLOBAL_KEY,
) -> float:
    """Return the consensus vote weight for `model_label` in [1.0, 9.0].

    Reads source_weights.confidence_band_low (Wilson 95% CI lower bound,
    0-100 integer percent), normalizes to 0-10, clamps to [1, 9].
    Anything that isn't a usable mature score returns NEUTRAL_WEIGHT
    (5.0): unregistered model, no row yet, sample_size below
    INSUFFICIENT_DATA_THRESHOLD, missing/null confidence_band_low, or
    any exception during lookup. Never raises. Never returns 0.
    """
    try:
        info = sw.get_source_weight(conn, model_label, context_id, ticker)
        if info is None:
            return NEUTRAL_WEIGHT
        n = info.get('sample_size') or 0
        if n < INSUFFICIENT_DATA_THRESHOLD:
            return NEUTRAL_WEIGHT
        cb_low = info.get('confidence_band_low')
        if cb_low is None:
            return NEUTRAL_WEIGHT
        w = float(cb_low) / 10.0  # 0-100 -> 0-10
        if w < FLOOR_WEIGHT:
            return FLOOR_WEIGHT
        if w > CEILING_WEIGHT:
            return CEILING_WEIGHT
        return w
    except Exception:
        return NEUTRAL_WEIGHT


def build_weight_map(
    conn: sqlite3.Connection,
    model_labels,
    context_id: str = sw.GLOBAL_KEY,
    ticker: str = sw.GLOBAL_KEY,
) -> dict:
    """Pre-fetch consensus weights for a list of model labels.

    Caller (which holds the DB connection) builds this once and passes
    it into the ConsensusRunner / Layer-1 reranker -- keeps the runner
    thread DB-free. Anything missing resolves to NEUTRAL_WEIGHT, so
    callers don't need to defend against gaps.
    """
    out = {}
    for m in (model_labels or []):
        if not m:
            continue
        try:
            out[m] = model_consensus_weight(
                conn, m, context_id=context_id, ticker=ticker)
        except Exception:
            out[m] = NEUTRAL_WEIGHT
    return out


def weighted_tally(votes, weight_map, accuracy_weighting_enabled: bool) -> dict:
    """Shared aggregation used by _finalize and the consensus display
    sites. Always returns BOTH the raw tally and the weighted summary
    so callers can show both lines (honest about vote counts AND about
    the weighted result). Cold-start safe: when every weight is 5.0
    (or weighting is disabled), weighted_winner == raw_winner.

    votes: iterable of vote dicts with 'direction' and 'model' fields.
    weight_map: {model_label: weight in [1.0, 9.0]} or None.
    accuracy_weighting_enabled: cfg flag.

    Returns dict:
      raw_counts          : {direction: count}
      raw_winner          : str
      raw_winner_n        : int
      raw_total           : int
      raw_score           : winner_n / total * 100
      weighted_enabled    : bool (effective; False if disabled OR no map)
      weighted_winner     : str (== raw_winner if not enabled)
      weighted_winner_sum : float
      weighted_total_sum  : float
      weighted_score      : winner_sum / total_sum * 100
      dir_weights         : {direction: weighted_sum}
      n_mature            : int (votes whose weight != NEUTRAL_WEIGHT)
      per_vote_weights    : {model_label: weight} (only for the models
                            that actually voted with a direction)
    """
    from collections import Counter, defaultdict

    committed = [v for v in (votes or []) if v.get('direction')]
    raw_counts = Counter(v['direction'] for v in committed)
    raw_total = sum(raw_counts.values())
    if raw_counts:
        raw_winner, raw_winner_n = raw_counts.most_common(1)[0]
    else:
        raw_winner, raw_winner_n = '', 0
    raw_score = (raw_winner_n / raw_total * 100.0) if raw_total else 0.0

    enabled = bool(accuracy_weighting_enabled and weight_map is not None)
    if not enabled:
        return {
            'raw_counts':          dict(raw_counts),
            'raw_winner':          raw_winner,
            'raw_winner_n':        raw_winner_n,
            'raw_total':           raw_total,
            'raw_score':           raw_score,
            'weighted_enabled':    False,
            'weighted_winner':     raw_winner,
            'weighted_winner_sum': float(raw_winner_n),
            'weighted_total_sum':  float(raw_total),
            'weighted_score':      raw_score,
            'dir_weights':         {d: float(n) for d, n in raw_counts.items()},
            'n_mature':            0,
            'per_vote_weights':    {},
        }

    dir_weights = defaultdict(float)
    per_vote_weights = {}
    n_mature = 0
    for v in committed:
        d = v['direction']
        m = v.get('model') or ''
        # v4.14.5.24: resolve the weight at CANONICAL grain first, so a vote's
        # weight tracks its real model identity (Groq's 8b vs 70b are distinct
        # canonicals, not both merged under "Groq"). Lookup hierarchy on the
        # combined map: (canonical@@provider) -> canonical -> display-label
        # (m, the v4.14.5.23 key, fallback for votes/old-rows lacking canonical
        # fields) -> NEUTRAL. The hierarchical (sub-grain -> rollup) fallback is
        # already folded into the canonical-keyed values by
        # compute_canonical_weight_map, so first-key-present wins here.
        _c = effective_canonical(v)
        _prov = (v.get('actual_provider') or '').strip()
        w = None
        if _c and _prov:
            w = weight_map.get(f"{_c}@@{_prov}")
        if w is None and _c:
            w = weight_map.get(_c)
        if w is None:
            w = weight_map.get(m, NEUTRAL_WEIGHT)
        try:
            wf = float(w)
        except (TypeError, ValueError):
            wf = NEUTRAL_WEIGHT
        # Re-clamp defensively in case caller handed in a value outside
        # the rails (e.g. a future bug builds the map wrong).
        if wf < FLOOR_WEIGHT:
            wf = FLOOR_WEIGHT
        elif wf > CEILING_WEIGHT:
            wf = CEILING_WEIGHT
        dir_weights[d] += wf
        per_vote_weights[m] = wf
        if wf != NEUTRAL_WEIGHT:
            n_mature += 1

    total_w = sum(dir_weights.values())
    if total_w > 0:
        weighted_winner = max(dir_weights, key=dir_weights.get)
        weighted_winner_sum = dir_weights[weighted_winner]
        weighted_score = weighted_winner_sum / total_w * 100.0
    else:
        weighted_winner = raw_winner
        weighted_winner_sum = 0.0
        weighted_score = 0.0

    return {
        'raw_counts':          dict(raw_counts),
        'raw_winner':          raw_winner,
        'raw_winner_n':        raw_winner_n,
        'raw_total':           raw_total,
        'raw_score':           raw_score,
        'weighted_enabled':    True,
        'weighted_winner':     weighted_winner,
        'weighted_winner_sum': weighted_winner_sum,
        'weighted_total_sum':  total_w,
        'weighted_score':      weighted_score,
        'dir_weights':         dict(dir_weights),
        'n_mature':            n_mature,
        'per_vote_weights':    per_vote_weights,
    }


def is_ollama_model_label(model_label: str) -> bool:
    """v4.14.5.19 (Change 6): identify Ollama / local-model names so the
    Track Record per-model matrix can filter them out (display only;
    rows stay in source_weights / predictions.jsonl as historical
    record). Cloud provider display names never contain a colon
    (they're proper-cased English: 'Groq', 'Mistral', 'GitHub', ...);
    Ollama model strings always do ('qwen2.5:14b', 'phi4:14b', etc.).
    Single-rule heuristic: colon present -> Ollama-shape.
    """
    if not isinstance(model_label, str):
        return False
    return ':' in model_label
