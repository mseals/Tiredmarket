"""migrate_predictions_to_price_bands — one-shot v4.14.6.0 migration.

Reclassifies every prediction in `data/predictions.jsonl` from its old
time-horizon path (aggressive / moderate / slow_safe / penny_lottery)
to its new price-band tier (lottery / band_5_10 / band_10_50 /
band_50_up), based on each pick's recorded entry price.

Also re-keys persisted path-named state in:
  - data/signals.jsonl (consensus_fresh_buy + per_model_fresh_buy
    entries carry a `path` field)
  - data/path_config.json (per-path enable/disable map)
  - data/config.json (path_candidate_pools / path_fill_targets keys
    inside the user override layers)

Designed to be SAFE and IDEMPOTENT:
  - Backs up each file it touches (`<file>.bak.pre-price-band-migration`)
    before modifying.
  - Records that already use a new-band key are left untouched.
  - Records with no usable price keep their original path but get a
    `path_legacy` field marker so they're visible later (do NOT silent-
    drop).
  - Idempotent re-run: a record already migrated (its path is in the
    new band set) is skipped.

Run from the install directory (where `data/` lives), e.g.:
    python migrate_predictions_to_price_bands.py
or with an explicit data dir:
    python migrate_predictions_to_price_bands.py C:\TiredMarket\data

Prints a per-band summary and a fallback count when done.
"""
from __future__ import annotations

import json
import os
import sys
import shutil
from datetime import datetime


# Band cutoffs MUST match tm_path_candidate_pools._CFG_DEFAULT.
_BAND_KEYS = ('lottery', 'band_5_10', 'band_10_50', 'band_50_up')


def classify_by_price(price):
    """Return the band key for a numeric price (or None if no price)."""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if p <= 0:
        return None
    if p < 5.0:
        return 'lottery'
    if p < 10.0:
        return 'band_5_10'
    if p < 50.0:
        return 'band_10_50'
    return 'band_50_up'


def best_price_for_record(rec):
    """Pick the most reliable price field a prediction record carries.
    Prefers the price at prediction time; falls back to buy_zone bounds
    or quote. Returns None if nothing usable."""
    for key in ('current_price_at_prediction', 'current_price',
                'price_at_prediction', 'price'):
        v = rec.get(key)
        if v not in (None, '', 0):
            try:
                p = float(v)
                if p > 0:
                    return p
            except (TypeError, ValueError):
                pass
    # buy_zone midpoint as a last structured fallback
    lo = rec.get('buy_zone_low')
    hi = rec.get('buy_zone_high')
    try:
        lo_f = float(lo) if lo not in (None, '') else None
        hi_f = float(hi) if hi not in (None, '') else None
    except (TypeError, ValueError):
        lo_f = hi_f = None
    if lo_f and hi_f and lo_f > 0 and hi_f > 0:
        return (lo_f + hi_f) / 2.0
    if hi_f and hi_f > 0:
        return hi_f
    if lo_f and lo_f > 0:
        return lo_f
    return None


def _backup(path: str) -> str:
    """Copy `path` to `<path>.bak.pre-price-band-migration` (one-shot;
    if the .bak already exists, it's preserved so a re-run can't
    overwrite the pristine pre-migration state). Returns the .bak
    filename for logging."""
    bak = path + '.bak.pre-price-band-migration'
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    return bak


def migrate_predictions(data_dir: str) -> dict:
    path = os.path.join(data_dir, 'predictions.jsonl')
    if not os.path.exists(path):
        print(f"[migrate] {path} not found — skipping predictions")
        return {}
    _backup(path)
    bands = {b: 0 for b in _BAND_KEYS}
    fallback = 0
    already = 0
    no_price = 0
    out_lines = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line_strip = line.rstrip('\n')
            if not line_strip.strip():
                out_lines.append(line_strip)
                continue
            try:
                rec = json.loads(line_strip)
            except Exception:
                # Malformed line — pass through unchanged.
                out_lines.append(line_strip)
                fallback += 1
                continue
            # Delta-merge log records have shape {"_d": 1, "patch": ...}
            # — pass through unchanged; the patch deltas don't carry path.
            if not isinstance(rec, dict) or rec.get('_d'):
                out_lines.append(line_strip)
                continue
            cur_path = rec.get('path')
            # Already on a new band key — skip.
            if cur_path in _BAND_KEYS:
                bands[cur_path] += 1
                already += 1
                out_lines.append(line_strip)
                continue
            price = best_price_for_record(rec)
            new_band = classify_by_price(price)
            if new_band is None:
                # No usable price → keep old path, stamp the legacy
                # marker so the record is visible without being
                # silently dropped.
                rec['path_legacy'] = rec.get('path', '')
                no_price += 1
                out_lines.append(json.dumps(rec, ensure_ascii=False))
                continue
            # Reclassify.
            rec['path_legacy'] = rec.get('path', '')
            rec['path'] = new_band
            bands[new_band] += 1
            out_lines.append(json.dumps(rec, ensure_ascii=False))
    # Atomic rewrite.
    tmp = path + '.tmp.migration'
    with open(tmp, 'w', encoding='utf-8') as out:
        out.write('\n'.join(out_lines))
        if out_lines and not out_lines[-1].endswith('\n'):
            out.write('\n')
    os.replace(tmp, path)
    print(f"[migrate] predictions.jsonl rewritten "
          f"({sum(bands.values())} records on new bands, "
          f"{no_price} kept on legacy path due to missing price, "
          f"{already} already migrated, "
          f"{fallback} unparseable passed through).")
    return {'bands': bands, 'no_price': no_price,
            'already': already, 'fallback': fallback}


def migrate_signals(data_dir: str) -> dict:
    """Re-key the `path` field on consensus rollup + per-model signals.
    Uses the same classify-by-price rule where the entry has a price;
    otherwise legacy remap (aggressive→band_10_50 etc.).
    """
    path = os.path.join(data_dir, 'signals.jsonl')
    if not os.path.exists(path):
        return {}
    _backup(path)
    legacy = {
        'aggressive':    'band_10_50',
        'moderate':      'band_10_50',
        'slow_safe':     'band_50_up',
        'penny_lottery': 'lottery',
    }
    n_rewrote = 0
    n_pass = 0
    out_lines = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line_strip = line.rstrip('\n')
            if not line_strip.strip():
                out_lines.append(line_strip)
                continue
            try:
                rec = json.loads(line_strip)
            except Exception:
                out_lines.append(line_strip)
                continue
            if not isinstance(rec, dict):
                out_lines.append(line_strip)
                continue
            p = rec.get('path')
            if p in _BAND_KEYS:
                out_lines.append(line_strip)
                n_pass += 1
                continue
            if p in legacy:
                rec['path_legacy'] = p
                rec['path'] = legacy[p]
                n_rewrote += 1
                out_lines.append(json.dumps(rec, ensure_ascii=False))
                continue
            out_lines.append(line_strip)
            n_pass += 1
    tmp = path + '.tmp.migration'
    with open(tmp, 'w', encoding='utf-8') as out:
        out.write('\n'.join(out_lines))
        if out_lines and not out_lines[-1].endswith('\n'):
            out.write('\n')
    os.replace(tmp, path)
    print(f"[migrate] signals.jsonl rewritten "
          f"({n_rewrote} rekeyed, {n_pass} unchanged).")
    return {'rewrote': n_rewrote, 'passed': n_pass}


def _remap_dict_keys(d, legacy_map):
    """Mutate dict `d` in place: for any key matching legacy_map, copy
    its entry to the mapped new band key and pop the legacy key. Idempo-
    tent. Skips keys already on a new band."""
    if not isinstance(d, dict):
        return 0
    n = 0
    for legacy_key, new_key in list(legacy_map.items()):
        if legacy_key in d and new_key not in d:
            d[new_key] = d.pop(legacy_key)
            n += 1
        elif legacy_key in d and new_key in d:
            # Already migrated — drop the legacy entry.
            d.pop(legacy_key, None)
            n += 1
    return n


def migrate_path_config(data_dir: str) -> dict:
    legacy = {
        'aggressive':    'band_10_50',
        'moderate':      'band_10_50',
        'slow_safe':     'band_50_up',
        'penny_lottery': 'lottery',
    }
    out = {}
    for fname in ('path_config.json', 'config.json'):
        full = os.path.join(data_dir, fname)
        if not os.path.exists(full):
            continue
        _backup(full)
        try:
            with open(full, encoding='utf-8') as f:
                obj = json.load(f)
        except Exception:
            continue
        changed = 0
        if fname == 'path_config.json':
            changed += _remap_dict_keys(obj, legacy)
            # Inner per-path dicts (e.g. {'paths': {...}}).
            for v in (obj.values() if isinstance(obj, dict) else ()):
                if isinstance(v, dict):
                    changed += _remap_dict_keys(v, legacy)
        else:  # config.json
            for nested_key in ('path_candidate_pools',
                                'path_fill_targets',
                                'path_overrides'):
                inner = obj.get(nested_key) if isinstance(obj, dict) else None
                if isinstance(inner, dict):
                    changed += _remap_dict_keys(inner, legacy)
        if changed:
            tmp = full + '.tmp.migration'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(obj, f, indent=2, ensure_ascii=False)
            os.replace(tmp, full)
        print(f"[migrate] {fname}: {changed} key remap(s).")
        out[fname] = changed
    return out


def main():
    if len(sys.argv) >= 2:
        data_dir = sys.argv[1]
    else:
        # Default: ./data relative to this script's parent (the install
        # dir).
        here = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(here, 'data')
    if not os.path.isdir(data_dir):
        print(f"[migrate] data dir not found: {data_dir}", file=sys.stderr)
        sys.exit(2)
    print(f"[migrate] starting v4.14.6.0 price-band migration "
          f"({datetime.now().isoformat(timespec='seconds')})")
    print(f"[migrate] data dir: {data_dir}")
    print(f"[migrate] backups suffix: .bak.pre-price-band-migration")
    print()
    summary = migrate_predictions(data_dir)
    migrate_signals(data_dir)
    migrate_path_config(data_dir)
    print()
    print("[migrate] band summary:")
    for b in _BAND_KEYS:
        n = (summary.get('bands') or {}).get(b, 0)
        print(f"  {b:12s}: {n:6d}")
    print(f"  no_price    : {summary.get('no_price', 0):6d} "
          f"(kept on legacy path; path_legacy stamped)")
    print()
    print("[migrate] done. Backups are at "
          "data/<file>.bak.pre-price-band-migration .")


if __name__ == '__main__':
    main()
