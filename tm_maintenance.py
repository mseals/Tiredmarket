"""tm_maintenance — v4.14.6.27 in-app Maintenance / Cleanup tool (v1).

Design (decided with the user):
- In-app "Run Maintenance" button — NOT bolted onto startup or shutdown
  (those are already slow). The app deliberately pauses daemons, cleans,
  resumes — no app restart needed.
- Need-based "due" indicator — counts what's actually reclaimable; doesn't
  nag on a clock.
- Dry-run always first: produce a manifest of exactly what WOULD be
  removed (counts, sizes, sample headlines for gossip), user reviews per
  category, then applies.
- News history is PRESERVED. The only news-touching action in v1 is the
  gossip purge (mis-filed collision-junk that the entity filter now
  identifies thanks to populated SEC names) and the 1970-epoch placeholder
  sentinel — never legitimate articles. Intelligent age-based news
  trimming is a separate future project. The Task plugin pattern below
  is the hook for it.

Architecture:
  Task — abstract base. id, label, description, dry_run(app) -> Manifest,
         apply(app, options) -> ApplyResult. Tasks are independent;
         per-Task failure is isolated.
  TaskRegistry — list of Task instances + a `find_by_id`. v1 ships 5
         tasks; future intelligent-news-trimming lands as a new Task
         here without rewrites elsewhere.
  MaintenanceEngine — orchestrates the run: pause daemons (wait for them
         to halt), apply each selected Task in its own transaction,
         VACUUM affected DBs, resume daemons, write the manifest log.
  Manifest / ApplyResult — small dataclass-style dicts; UI displays them.
  compute_due_status — cheap dry-run-all + "days since last run" check
         that decides whether the UI badge says "maintenance recommended."

Safety:
- v1 Tasks are write-isolated: each Task's apply() runs in its own
  transaction; one failing never stops the others.
- VACUUM only after daemons are confirmed paused (exclusive lock).
- Manifest log written for every real run; dry-runs do not log.
- Backup exemptions hard-coded (current-release rollback path NEVER
  proposed for deletion).
- News history (real articles by age) is NOT pruned in v1. The Task
  pattern leaves a clean hook for the future trimming work.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional


# ─── Constants — config keys + defaults ────────────────────────────────

CFG_LAST_RUN          = '_v414627_maintenance_last_run'         # ISO timestamp
CFG_DUE_BYTES         = 'maintenance_due_reclaimable_bytes'     # >= → due
CFG_DUE_ROWS          = 'maintenance_due_reclaimable_rows'      # >= → due
CFG_DUE_DAYS          = 'maintenance_due_days_since_last_run'   # >= → due

DEFAULT_DUE_BYTES = 500 * 1024 * 1024   # 500 MB
DEFAULT_DUE_ROWS  = 200_000             # 200k items
DEFAULT_DUE_DAYS  = 14                  # 2 weeks

# Recent-article carve-out: never propose to delete a row whose
# published_at is within this many days (defensive — we don't want to
# strip a headline the user / AI just read).
GOSSIP_RECENT_DAYS = 7

# Backup exemption — hard-coded so current-release rollback path is
# NEVER proposed for deletion regardless of age. Update each release.
_PROTECTED_BAK_SUFFIXES = (
    '.pre_v4_14_6_25.bak',
    '.pre_v4_14_6_26.bak',
    '.pre_v4_14_6_27.bak',
)
# data_backups/data_<ts>/ snapshots from the current release date forward
# are also protected; the heuristic below keeps newest-2 always.
_BACKUP_DIR_KEEP_NEWEST = 2
_DATA_BACKUPS_DIR_KEEP_NEWEST = 3  # in data/backups/
_BACKUPS_ROOT_DIR_KEEP_NEWEST = 5  # in _backups/

# Operational-log retention windows (these tables are append-current
# and the readers use much shorter cutoffs).
TRIGGER_FIRE_LOG_KEEP_DAYS = 14   # readers use minutes-to-hours
QR_ANALYSIS_LOG_KEEP_DAYS  = 60   # reader uses 5-7 day cutoff


# ─── Manifest / ApplyResult ────────────────────────────────────────────

def _new_manifest(task_id: str, label: str) -> dict:
    return {
        'task_id':       task_id,
        'label':         label,
        'would_remove':  0,
        'would_archive': 0,
        'bytes_freed':   0,
        'sample':        [],          # list of human-readable strings
        'per_subject':   {},          # {subject: count} — e.g. per ticker, per file
        'notes':         [],          # free-form caveats / warnings
        'error':         None,
    }


def _new_apply_result(task_id: str) -> dict:
    return {
        'task_id':     task_id,
        'removed':     0,
        'archived':    0,
        'bytes_freed': 0,
        'errors':      [],
    }


# ─── Task base class ───────────────────────────────────────────────────

class Task:
    """Abstract maintenance Task. Subclasses must implement dry_run()
    and apply()."""
    id: str = ''
    label: str = ''
    description: str = ''
    touches_dbs: tuple = ()    # which DBs need VACUUM after apply

    def dry_run(self, app) -> dict:                # → Manifest
        raise NotImplementedError

    def apply(self, app, options: dict) -> dict:    # → ApplyResult
        raise NotImplementedError


# ─── Helpers ───────────────────────────────────────────────────────────

def _open_tired_market_db(app):
    """Fresh sqlite3 connection to tired_market.db. Caller closes."""
    try:
        path = app.db_path if hasattr(app, 'db_path') else None
    except Exception:
        path = None
    if path is None:
        # Fallback to default location next to the app.
        path = __import__('tm_paths').get_data_dir() / 'tired_market.db'
    conn = sqlite3.connect(str(path))
    return conn


def _open_cache_db():
    """Fresh sqlite3 connection to cache.db via tm_cache."""
    try:
        import tm_cache
        return tm_cache.get_connection()
    except Exception:
        return None


def _human_bytes(n) -> str:
    try:
        n = int(n)
    except Exception:
        return str(n)
    if n >= 1024**3:
        return f"{n/1024**3:.2f} GB"
    if n >= 1024**2:
        return f"{n/1024**2:.1f} MB"
    if n >= 1024:
        return f"{n/1024:.1f} KB"
    return f"{n} B"


def _is_real_company_name(value) -> bool:
    """Mirror tired_market._is_real_company_name — re-implemented here
    so we don't add an import cycle on a defensive helper."""
    if value is None:
        return False
    s = str(value).strip()
    if not s:
        return False
    if s == '__RATE_LIMITED__':
        return False
    return True


# ─── Task 1 — Gossip removal ───────────────────────────────────────────

# Collision-prone sources (mirrors tired_market._FINANCE_RELEVANCE_COLLISION_SOURCES).
_COLLISION_SOURCES = (
    'yahoo_rss', 'google_news', 'marketwatch', 'cnbc',
    'benzinga', 'investing', 'reuters', 'finviz',
)


class GossipRemovalTask(Task):
    """Re-run the live entity filter against stored news_cache rows and
    flag the ones the filter would reject TODAY. These are rows that
    were ingested before SEC names populated (or before v4.14.6.22) and
    are now identifiable as collision-junk (e.g. "credit card" headlines
    filed under CARD when CARD is Bank of Montreal).

    Carve-outs (NEVER delete):
      - ticker whose cache.db.tickers.name isn't a real name
      - row with source NOT in the collision-prone set
      - row whose published_at is within GOSSIP_RECENT_DAYS

    Plus the 8 known MAMA test rows from 2026-06-12T19:07:24 (added by
    Claude's smoke test before v4.14.6.22 shipped) are explicitly
    targeted via a separate rule.

    Dry-run produces per-ticker counts + sample headlines especially for
    high-rejection tickers. Apply respects an `excluded_tickers` option
    so the user can exclude specific tickers if a sample looks
    misclassified.
    """
    id = 'gossip_removal'
    label = 'Gossip removal (mis-filed collision-junk)'
    description = (
        'Re-runs the entity filter against existing news_cache rows. '
        'Removes rows the filter would reject today — collision-junk '
        'like "credit card" headlines mis-filed under CARD (Bank of '
        'Montreal). Legitimate news for the ticker is kept. Skips '
        'tickers without a real SEC name and recent (<7d) headlines.'
    )
    touches_dbs = ('tired_market.db',)

    # Smoke-test residue the user asked us to clean up explicitly.
    _MAMA_TEST_TS = '2026-06-12T19:07:24'

    def _gather_per_ticker(self, app, excluded_tickers=None):
        """Returns {ticker: [(row_id, headline, source, published_at)]}.
        Best-effort; failures return {} so the dry-run shows zero rather
        than crashing."""
        excluded_tickers = {t.upper() for t in (excluded_tickers or set())}
        try:
            import tired_market as tm
        except Exception:
            return {}
        try:
            import tm_cache
            cc = tm_cache.get_connection()
        except Exception:
            return {}
        # Pull all real-named tickers + their aliases.
        try:
            name_map = {}
            for r in cc.execute(
                "SELECT ticker, name FROM tickers "
                "WHERE name IS NOT NULL AND name != '' "
                "AND name != '__RATE_LIMITED__'"
            ).fetchall():
                tk = str(r[0]).upper()
                if tk in excluded_tickers:
                    continue
                name_map[tk] = r[1]
        finally:
            cc.close()
        if not name_map:
            return {}
        # Walk news_cache by ticker — bounded to collision-prone sources
        # so we never touch yahoo/finnhub provider-curated rows.
        per_ticker: dict[str, list] = {}
        try:
            tm_db = _open_tired_market_db(app)
        except Exception:
            return {}
        try:
            qm = ','.join('?' for _ in _COLLISION_SOURCES)
            recent_cutoff = (
                datetime.utcnow().timestamp() - GOSSIP_RECENT_DAYS * 86400
            )
            for tk, name in name_map.items():
                aliases = tm._TICKER_ALIASES.get(tk) if hasattr(
                    tm, '_TICKER_ALIASES') else None
                cur = tm_db.execute(
                    f"SELECT id, headline, source, published_at "
                    f"FROM news_cache "
                    f"WHERE ticker = ? AND source IN ({qm})",
                    (tk, *_COLLISION_SOURCES))
                rejects = []
                for row_id, headline, source, pub_at in cur.fetchall():
                    if not headline:
                        continue
                    # Recent-article carve-out — skip if published_at is
                    # within the recent window. published_at is ISO; cheap
                    # parse with a fallback to "include in candidate" on
                    # error (we'd rather be conservative).
                    if pub_at:
                        try:
                            pub_dt = datetime.fromisoformat(
                                str(pub_at).replace('Z', '+00:00'))
                            if pub_dt.timestamp() > recent_cutoff:
                                continue
                        except Exception:
                            pass
                    try:
                        admit = tm._is_entity_relevant(
                            headline, tk, name, aliases)
                    except Exception:
                        admit = True   # fail-safe — keep on error
                    if not admit:
                        rejects.append(
                            (row_id, headline, source, pub_at))
                if rejects:
                    per_ticker[tk] = rejects
        finally:
            try:
                tm_db.close()
            except Exception:
                pass
        return per_ticker

    def _gather_mama_test(self, app):
        """The 8 specific MAMA test rows from Claude's pre-v4.14.6.22
        smoke test."""
        try:
            tm_db = _open_tired_market_db(app)
        except Exception:
            return []
        try:
            cur = tm_db.execute(
                "SELECT id, headline, source FROM news_cache "
                "WHERE ticker = 'MAMA' "
                "  AND timestamp = ? "
                "  AND headline LIKE '%baby mama%'",
                (self._MAMA_TEST_TS + '%',))
            return cur.fetchall()
        except Exception:
            return []
        finally:
            try:
                tm_db.close()
            except Exception:
                pass

    def dry_run(self, app) -> dict:
        m = _new_manifest(self.id, self.label)
        per_ticker = self._gather_per_ticker(app)
        total_rows = 0
        for tk, rows in per_ticker.items():
            n = len(rows)
            m['per_subject'][tk] = n
            total_rows += n
            # Sample 2 headlines from the highest-rejection tickers
            # (worst-first so the user reviews the most-suspect rows).
        # MAMA test rows
        mama_test = self._gather_mama_test(app)
        if mama_test:
            m['notes'].append(
                f"Plus {len(mama_test)} known MAMA test rows from "
                f"{self._MAMA_TEST_TS} (Claude's pre-v4.14.6.22 smoke "
                f"test).")
            total_rows += len(mama_test)
        m['would_remove'] = total_rows
        # Build samples on the top-10 highest-rejection tickers.
        top = sorted(per_ticker.items(), key=lambda kv: -len(kv[1]))[:10]
        for tk, rows in top:
            for (row_id, headline, source, pub_at) in rows[:2]:
                m['sample'].append(
                    f"[{tk}, {source}] {headline[:100]}")
        # Rough bytes estimate (avg ~200 B/row including indexes).
        m['bytes_freed'] = total_rows * 200
        m['notes'].insert(0,
            f"Re-runs the entity filter against {len(per_ticker)} "
            f"tickers' news_cache rows. Tickers without a real SEC "
            f"name are skipped — the filter can't decide without one.")
        if not total_rows:
            m['notes'].append(
                "Nothing to do — the entity filter rejects no stored "
                "rows. (This is the steady-state once v4.14.6.22 has "
                "been protecting ingest for a while.)")
        return m

    def apply(self, app, options: dict) -> dict:
        r = _new_apply_result(self.id)
        excluded = set(options.get('excluded_tickers', ()))
        per_ticker = self._gather_per_ticker(
            app, excluded_tickers=excluded)
        if not per_ticker and not self._gather_mama_test(app):
            return r
        try:
            tm_db = _open_tired_market_db(app)
        except Exception as e:
            r['errors'].append(f'open tired_market.db: {e}')
            return r
        try:
            # Per-ticker DELETE in its own transaction so one failure
            # doesn't roll back the others.
            for tk, rows in per_ticker.items():
                ids = [row[0] for row in rows]
                if not ids:
                    continue
                try:
                    with tm_db:
                        qm = ','.join('?' * len(ids))
                        tm_db.execute(
                            f"DELETE FROM news_cache "
                            f"WHERE id IN ({qm})", ids)
                    r['removed'] += len(ids)
                except Exception as e:
                    r['errors'].append(f'{tk}: {e}')
            # The known MAMA test rows.
            mama_test = self._gather_mama_test(app)
            if mama_test:
                ids = [row[0] for row in mama_test]
                try:
                    with tm_db:
                        qm = ','.join('?' * len(ids))
                        tm_db.execute(
                            f"DELETE FROM news_cache "
                            f"WHERE id IN ({qm})", ids)
                    r['removed'] += len(ids)
                except Exception as e:
                    r['errors'].append(f'mama_test: {e}')
        finally:
            tm_db.close()
        # Rough bytes-freed estimate (VACUUM is what actually reclaims).
        r['bytes_freed'] = r['removed'] * 200
        return r


# ─── Task 2 — Garbage / 1970-epoch placeholders ────────────────────────

class GarbageRowsTask(Task):
    """Delete unambiguous placeholder/garbage rows. v1 scope:
      - news_signals rows where timestamp == '1970-01-01T08:00:00Z'
        (the exact placeholder sentinel observed in the audit).

    Does NOT blanket-delete pre-2000 timestamps — the hygiene
    investigation found legitimate 1972/1976/1977 archival articles
    that recent news fetches surfaced. Those stay. When in doubt, keep.
    """
    id = 'garbage_rows'
    label = 'Garbage placeholder rows'
    description = (
        "Removes the '1970-01-01T08:00:00Z' placeholder sentinel rows "
        "in news_signals. Real archival articles (1972/1976/...) are "
        "preserved — only the exact placeholder string is touched."
    )
    touches_dbs = ('cache.db',)

    _SENTINEL = '1970-01-01T08:00:00Z'

    def _count(self):
        c = _open_cache_db()
        if c is None:
            return 0
        try:
            r = c.execute(
                "SELECT COUNT(*) FROM news_signals WHERE timestamp = ?",
                (self._SENTINEL,)).fetchone()
            return int(r[0]) if r else 0
        finally:
            c.close()

    def dry_run(self, app) -> dict:
        m = _new_manifest(self.id, self.label)
        n = self._count()
        m['would_remove'] = n
        m['bytes_freed'] = n * 200
        m['notes'].append(
            f"Targets exactly timestamp == '{self._SENTINEL}'. "
            f"Legitimate ancient articles (1972/1976/...) are NOT "
            f"affected.")
        if n == 0:
            m['notes'].append("Nothing to do.")
        return m

    def apply(self, app, options: dict) -> dict:
        r = _new_apply_result(self.id)
        c = _open_cache_db()
        if c is None:
            r['errors'].append('cache.db open failed')
            return r
        try:
            with c:
                cur = c.execute(
                    "DELETE FROM news_signals WHERE timestamp = ?",
                    (self._SENTINEL,))
                r['removed'] = cur.rowcount or 0
                r['bytes_freed'] = r['removed'] * 200
        except Exception as e:
            r['errors'].append(f'delete: {e}')
        finally:
            c.close()
        return r


# ─── Task 3 — Operational logs (oplog) ─────────────────────────────────

class OperationalLogTask(Task):
    """Roll trigger_fire_log and queue_runner_analysis_log past their
    safe retention windows.

    Both are append-current (fired_at / last_analyzed_at = now-epoch at
    write time, never historical), so age-based DELETE has no backfill
    conflict. Readers use much shorter cutoffs (minutes-to-hours for
    trigger_fire_log; 5-7 days for queue_runner_analysis_log).
    """
    id = 'oplog_rolling'
    label = 'Operational log rolling'
    description = (
        'Removes old trigger_fire_log rows (>14 d) and '
        'queue_runner_analysis_log rows (>60 d). Both are append-current '
        'bookkeeping tables — no prediction data lives here. Readers '
        'use windows far shorter than the retention cutoffs.'
    )
    touches_dbs = ('tired_market.db',)

    def _counts(self, app):
        tm_db = _open_tired_market_db(app)
        try:
            tfl_cutoff = int(time.time()) - TRIGGER_FIRE_LOG_KEEP_DAYS * 86400
            qr_cutoff = int(time.time()) - QR_ANALYSIS_LOG_KEEP_DAYS * 86400
            n_tfl = tm_db.execute(
                "SELECT COUNT(*) FROM trigger_fire_log "
                "WHERE fired_at < ?", (tfl_cutoff,)).fetchone()[0]
            n_qr = tm_db.execute(
                "SELECT COUNT(*) FROM queue_runner_analysis_log "
                "WHERE last_analyzed_at < ?", (qr_cutoff,)).fetchone()[0]
            return n_tfl, n_qr, tfl_cutoff, qr_cutoff
        finally:
            tm_db.close()

    def dry_run(self, app) -> dict:
        m = _new_manifest(self.id, self.label)
        try:
            n_tfl, n_qr, _, _ = self._counts(app)
        except Exception as e:
            m['error'] = f'count failed: {e}'
            return m
        m['per_subject']['trigger_fire_log (>14d)'] = n_tfl
        m['per_subject']['queue_runner_analysis_log (>60d)'] = n_qr
        m['would_remove'] = n_tfl + n_qr
        m['bytes_freed'] = (n_tfl + n_qr) * 150
        m['notes'].append(
            "Both tables are append-current — fired_at / "
            "last_analyzed_at are set to time.time() at insert, never "
            "backdated. Safe to age-prune.")
        if m['would_remove'] == 0:
            m['notes'].append("Nothing past the cutoffs yet.")
        return m

    def apply(self, app, options: dict) -> dict:
        r = _new_apply_result(self.id)
        try:
            tm_db = _open_tired_market_db(app)
        except Exception as e:
            r['errors'].append(f'open db: {e}')
            return r
        try:
            n_tfl, n_qr, tfl_cutoff, qr_cutoff = self._counts(app)
            try:
                with tm_db:
                    tm_db.execute(
                        "DELETE FROM trigger_fire_log WHERE fired_at < ?",
                        (tfl_cutoff,))
                r['removed'] += n_tfl
            except Exception as e:
                r['errors'].append(f'trigger_fire_log: {e}')
            try:
                with tm_db:
                    tm_db.execute(
                        "DELETE FROM queue_runner_analysis_log "
                        "WHERE last_analyzed_at < ?", (qr_cutoff,))
                r['removed'] += n_qr
            except Exception as e:
                r['errors'].append(f'queue_runner_analysis_log: {e}')
        finally:
            tm_db.close()
        r['bytes_freed'] = r['removed'] * 150
        return r


# ─── Task 4 — Orphaned file archive ────────────────────────────────────

class AILogArchiveTask(Task):
    """Archive `ai_log.jsonl` — no live writer (last write 2026-05-08)
    and no reader. We MOVE it to data_backups/_orphaned_<ts>/ rather
    than hard-delete, so it's recoverable if it turns out something
    needed it.
    """
    id = 'ai_log_archive'
    label = 'Archive orphaned ai_log.jsonl'
    description = (
        'Moves data/ai_log.jsonl into data_backups/_orphaned_<ts>/. '
        'The writer was removed in May; no live reader. Archived (not '
        'deleted) so it stays recoverable.'
    )

    def _path(self):
        return __import__('tm_paths').get_data_dir() / 'ai_log.jsonl'

    def dry_run(self, app) -> dict:
        m = _new_manifest(self.id, self.label)
        p = self._path()
        if not p.exists():
            m['notes'].append("File already gone — nothing to do.")
            return m
        try:
            sz = p.stat().st_size
        except Exception:
            sz = 0
        m['would_archive'] = 1
        m['bytes_freed'] = sz
        m['sample'].append(f"{p.name}  ({_human_bytes(sz)})")
        m['notes'].append(
            "Moved (not deleted) to data_backups/_orphaned_<ts>/. "
            "Recoverable by hand if anything turns out to need it.")
        return m

    def apply(self, app, options: dict) -> dict:
        r = _new_apply_result(self.id)
        p = self._path()
        if not p.exists():
            return r
        try:
            sz = p.stat().st_size
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            dest_dir = __import__('tm_paths').get_data_dir().parent / 'data_backups' / f'_orphaned_{ts}'
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / p.name
            shutil.move(str(p), str(dest))
            r['archived'] = 1
            r['bytes_freed'] = sz
        except Exception as e:
            r['errors'].append(f'archive ai_log.jsonl: {e}')
        return r


# ─── Task 5 — Backup pile ──────────────────────────────────────────────

class BackupPileTask(Task):
    """Backup pile cleanup. Hard exemptions:
      - the newest N `data_backups/data_<ts>/` directories
        (current-release rollback path)
      - any source `.bak` with a _PROTECTED_BAK_SUFFIXES suffix
      - the giant DB `.bak` from v4.14.6.21/.22 era are NOT included
        in v1 (those need an explicit "release stable a week" gate the user
        opts in to separately).

    Also addresses the nesting issue (loose `.bak` files inside data/
    get copied into every shutdown snapshot, doubling disk use): MOVES
    old loose `.bak` files in data/ to data_backups/_orphaned_<ts>/.
    """
    id = 'backup_pile'
    label = 'Backup pile cleanup'
    description = (
        'Rotates old per-patch backup directories (keep newest 2-3 in '
        'data_backups/, newest 3 in data/backups/, newest 5 in _backups/) '
        'and archives loose stale .bak files from inside data/ so '
        'future snapshots stop nesting them. Current-release rollback '
        'path is protected and never proposed for deletion. The giant '
        'pre-entity-filter / pre-news-rootfix DB backups require a '
        'separate opt-in (release-stability gate) and are NOT in v1.'
    )

    def _scan_backup_dirs(self, root: Path, keep_newest: int,
                          name_prefix_filter: Callable[[str], bool] = None):
        """Returns (would_remove, removable_paths). Sorts dirs by mtime
        descending, keeps newest_N, returns the rest."""
        if not root.exists():
            return [], []
        try:
            dirs = []
            for p in root.iterdir():
                if not p.is_dir():
                    continue
                # Skip the install-snapshot exempt prefix (existing
                # cleanup_resources already does this).
                if p.name.startswith('data_INSTALL_'):
                    continue
                # Optional name filter (e.g. only "data_*" or
                # "pre_v*").
                if name_prefix_filter and not name_prefix_filter(p.name):
                    continue
                dirs.append(p)
            dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return dirs[:keep_newest], dirs[keep_newest:]
        except Exception:
            return [], []

    def _scan_loose_bak_in_data(self):
        """Loose .bak files directly under data/ (not inside subdirs).
        These cause the snapshot-nesting issue: every shutdown copy
        them again.

        Excludes recent files (created in the last 14 days) and
        protected suffixes. Returns list[Path]."""
        data = __import__('tm_paths').get_data_dir()
        if not data.exists():
            return []
        cutoff_age = time.time() - 14 * 86400
        out = []
        try:
            for p in data.iterdir():
                if not p.is_file():
                    continue
                if not p.name.endswith('.bak'):
                    continue
                if any(p.name.endswith(suf)
                       for suf in _PROTECTED_BAK_SUFFIXES):
                    continue
                try:
                    if p.stat().st_mtime > cutoff_age:
                        continue   # recent — leave alone
                except Exception:
                    continue
                out.append(p)
        except Exception:
            pass
        return out

    def dry_run(self, app) -> dict:
        m = _new_manifest(self.id, self.label)
        root = Path(__file__).parent
        # 1. data_backups/ root — keep newest N data_* dirs
        kept1, drop1 = self._scan_backup_dirs(
            root / 'data_backups', _BACKUP_DIR_KEEP_NEWEST,
            lambda n: n.startswith('data_'))
        # 2. data/backups/ — keep newest N
        kept2, drop2 = self._scan_backup_dirs(
            __import__('tm_paths').get_data_dir() / 'backups', _DATA_BACKUPS_DIR_KEEP_NEWEST)
        # 3. _backups/ — keep newest N
        kept3, drop3 = self._scan_backup_dirs(
            root / '_backups', _BACKUPS_ROOT_DIR_KEEP_NEWEST)
        # 4. Loose stale .bak inside data/
        loose = self._scan_loose_bak_in_data()

        def _dir_size(p):
            try:
                return sum(f.stat().st_size
                           for f in p.rglob('*') if f.is_file())
            except Exception:
                return 0

        bytes_total = 0
        for p in drop1:
            sz = _dir_size(p)
            bytes_total += sz
            m['sample'].append(
                f"DROP DIR data_backups/{p.name}  ({_human_bytes(sz)})")
        for p in drop2:
            sz = _dir_size(p)
            bytes_total += sz
            m['sample'].append(
                f"DROP DIR data/backups/{p.name}  ({_human_bytes(sz)})")
        for p in drop3:
            sz = _dir_size(p)
            bytes_total += sz
            m['sample'].append(
                f"DROP DIR _backups/{p.name}  ({_human_bytes(sz)})")
        for p in loose:
            try:
                sz = p.stat().st_size
            except Exception:
                sz = 0
            bytes_total += sz
            m['sample'].append(
                f"ARCHIVE data/{p.name}  ({_human_bytes(sz)})  "
                f"(loose .bak — moved out of data/ so future "
                f"snapshots stop copying it)")

        m['would_remove'] = len(drop1) + len(drop2) + len(drop3)
        m['would_archive'] = len(loose)
        m['bytes_freed'] = bytes_total
        m['per_subject']['data_backups/ dirs dropped'] = len(drop1)
        m['per_subject']['data/backups/ dirs dropped'] = len(drop2)
        m['per_subject']['_backups/ dirs dropped'] = len(drop3)
        m['per_subject']['loose data/*.bak archived'] = len(loose)
        m['notes'].append(
            f"Protected: newest {_BACKUP_DIR_KEEP_NEWEST} "
            f"data_backups/ snapshots (current release rollback "
            f"path); newest {_DATA_BACKUPS_DIR_KEEP_NEWEST} dirs in "
            f"data/backups/; newest {_BACKUPS_ROOT_DIR_KEEP_NEWEST} "
            f"dirs in _backups/; any data_INSTALL_* dir; .pre_v4_*.bak "
            f"with the current-release suffix.")
        m['notes'].append(
            "NOT INCLUDED in v1: cache.db.pre_entity_filter.bak "
            "(1.77 GB) and tired_market.db.pre_news_rootfix.bak "
            "(383 MB). These need a separate release-stability opt-in.")
        if not (drop1 or drop2 or drop3 or loose):
            m['notes'].append("Nothing to do.")
        return m

    def apply(self, app, options: dict) -> dict:
        r = _new_apply_result(self.id)
        root = Path(__file__).parent
        try:
            _, drop1 = self._scan_backup_dirs(
                root / 'data_backups', _BACKUP_DIR_KEEP_NEWEST,
                lambda n: n.startswith('data_'))
            _, drop2 = self._scan_backup_dirs(
                __import__('tm_paths').get_data_dir() / 'backups',
                _DATA_BACKUPS_DIR_KEEP_NEWEST)
            _, drop3 = self._scan_backup_dirs(
                root / '_backups', _BACKUPS_ROOT_DIR_KEEP_NEWEST)
            loose = self._scan_loose_bak_in_data()
        except Exception as e:
            r['errors'].append(f'scan: {e}')
            return r

        def _dir_size(p):
            try:
                return sum(f.stat().st_size
                           for f in p.rglob('*') if f.is_file())
            except Exception:
                return 0

        for p in drop1 + drop2 + drop3:
            try:
                sz = _dir_size(p)
                shutil.rmtree(str(p), ignore_errors=True)
                if not p.exists():
                    r['removed'] += 1
                    r['bytes_freed'] += sz
            except Exception as e:
                r['errors'].append(f'rmtree {p.name}: {e}')

        # Archive loose data/*.bak files OUT of data/ so future snapshots
        # don't copy them. Destination: data_backups/_orphaned_<ts>/
        if loose:
            try:
                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                dest_dir = root / 'data_backups' / f'_orphaned_{ts}'
                dest_dir.mkdir(parents=True, exist_ok=True)
                for p in loose:
                    try:
                        sz = p.stat().st_size
                        shutil.move(str(p), str(dest_dir / p.name))
                        r['archived'] += 1
                        r['bytes_freed'] += sz
                    except Exception as e:
                        r['errors'].append(f'archive {p.name}: {e}')
            except Exception as e:
                r['errors'].append(f'archive dir setup: {e}')
        return r


# ─── Task 7 — On-demand predictions.jsonl cleanup (v4.14.6.36) ─────────

class PredictionsCleanupTask(Task):
    """Tiered retention cleanup of `data/predictions.jsonl`.

    Wraps the existing `PredictionsLog.cleanup_tiered` logic so the
    user explicitly opens the Maintenance dialog and confirms each
    run, instead of the pre-v4.14.6.36 silent-nightly auto-tick.

    Retention (unchanged from the existing well-tested logic):
      * BUY                — kept FOREVER (the user's track record)
      * NO_CALL            — kept 1 day (pure noise)
      * WATCH/AVOID/HOLD   — kept 30 days
      * unknown direction / missing timestamp — KEPT (failsafe)

    The underlying cleanup writes a rotating backup BEFORE deleting
    and uses an atomic rewrite; either failure ABORTs without
    touching the file.

    Dry-run: counts only — no write, no backup, no delete. Apply:
    real cleanup.
    """
    id = 'predictions_cleanup'
    label = 'Predictions log cleanup (tiered retention)'
    description = (
        'Trims data/predictions.jsonl by tiered retention: BUYs kept '
        'forever (your track record); NO_CALL records older than 1 '
        'day removed; WATCH/AVOID/HOLD older than 30 days removed; '
        'anything with an unknown direction or missing timestamp is '
        'kept as a failsafe. Writes a rotating backup before the '
        'delete and uses an atomic rewrite — aborts if either step '
        'fails. v4.14.6.36 moved this here from a silent auto-tick.'
    )
    # No DB touched → no VACUUM list.

    def _predictions_log(self, app):
        """Return the PredictionsLog instance or None if unavailable."""
        try:
            state = getattr(app, '_holdings_state', None) or {}
            pl = state.get('predictions_log')
            if pl is None or not hasattr(pl, 'cleanup_tiered'):
                return None
            return pl
        except Exception:
            return None

    def _run_logic(self, app, dry_run: bool) -> Optional[dict]:
        """Run cleanup_tiered with the given dry-run flag. Returns
        the counts dict or None if the predictions log is missing
        (e.g. discover disabled / fresh install / startup race)."""
        pl = self._predictions_log(app)
        if pl is None:
            return None
        try:
            return pl.cleanup_tiered(dry_run=dry_run)
        except Exception:
            return None

    def dry_run(self, app) -> dict:
        m = _new_manifest(self.id, self.label)
        counts = self._run_logic(app, dry_run=True)
        if counts is None:
            m['notes'].append(
                "Predictions log not available — discover module not "
                "loaded yet, fresh install, or startup race. Nothing "
                "to preview.")
            return m
        kept_buy = int(counts.get('kept_buy', 0) or 0)
        kept_no_call_fresh = int(counts.get('kept_no_call_fresh', 0) or 0)
        kept_other_fresh = int(counts.get('kept_other_fresh', 0) or 0)
        kept_unknown = int(counts.get('kept_unknown', 0) or 0)
        kept_no_ts = int(counts.get('kept_no_timestamp', 0) or 0)
        drop_no_call_old = int(counts.get('dropped_no_call_old', 0) or 0)
        drop_other_old = int(counts.get('dropped_other_old', 0) or 0)
        total_before = int(counts.get('total_before', 0) or 0)
        total_after = int(counts.get('total_after', 0) or 0)
        dropped = drop_no_call_old + drop_other_old

        m['would_remove'] = dropped
        # Rough size estimate — predictions records average ~300 B.
        m['bytes_freed'] = dropped * 300

        m['per_subject']['BUY (kept forever)']           = kept_buy
        m['per_subject']['NO_CALL kept (< 1 day)']       = kept_no_call_fresh
        m['per_subject']['WATCH/AVOID/HOLD kept (< 30d)'] = kept_other_fresh
        m['per_subject']['unknown-direction (failsafe kept)']    = kept_unknown
        m['per_subject']['no-timestamp (failsafe kept)']         = kept_no_ts
        m['per_subject']['NO_CALL would drop (> 1 day)']         = drop_no_call_old
        m['per_subject']['WATCH/AVOID/HOLD would drop (> 30d)']  = drop_other_old

        if total_before:
            pct = 100.0 * dropped / max(1, total_before)
            m['notes'].append(
                f"Before: {total_before:,} records  |  After: "
                f"{total_after:,}  |  Drop: {dropped:,} ({pct:.1f}%).")
        m['notes'].append(
            "BUY records (your track record) are kept forever — never "
            "dropped on age. A rotating backup is written before any "
            "delete; either backup or atomic-rewrite failure aborts "
            "without modifying the file.")
        if dropped == 0:
            m['notes'].append("Nothing past the retention windows yet.")
        return m

    def apply(self, app, options: dict) -> dict:
        r = _new_apply_result(self.id)
        counts = self._run_logic(app, dry_run=False)
        if counts is None:
            r['errors'].append(
                'predictions log unavailable — task skipped')
            return r
        if counts.get('aborted'):
            r['errors'].append(
                'cleanup_tiered aborted (backup or atomic-rewrite '
                'failed; file left intact, no records deleted)')
            return r
        drop_no_call_old = int(counts.get('dropped_no_call_old', 0) or 0)
        drop_other_old = int(counts.get('dropped_other_old', 0) or 0)
        r['removed'] = drop_no_call_old + drop_other_old
        r['bytes_freed'] = r['removed'] * 300
        return r


# ─── Task 6 — On-demand backup snapshot (v4.14.6.34) ───────────────────

class BackupSnapshotTask(Task):
    """Create a full snapshot of `data/` as `data_backups/data_<ts>/`.

    The same shutil.copytree behavior the close-time auto-backup
    used to run on every clean exit — but now USER-TRIGGERED via
    the Maintenance dialog instead of blocking shutdown.

    Dry-run reports what would be copied (data/ size, current
    backup count, how many old snapshots the prune step would drop).
    Apply runs the copy, then prunes data_backups/data_* to keep
    the newest N (cfg['cleanup_keep_backups'], default 2).

    Notes:
      * Touches NO DB — pure filesystem copy. No VACUUM needed.
      * Uses the same ignore patterns the close-time path used
        (.lock / .write_test / __pycache__).
      * Errors during prune don't fail the task (the copy is the
        load-bearing part; rotation is housekeeping).
    """
    id = 'backup_snapshot'
    label = 'Create backup snapshot of data/'
    description = (
        'Snapshots the data/ folder into data_backups/data_<ts>/ '
        'and rotates old snapshots (newest 2 by default). The '
        'pre-v4.14.6.34 auto-backup-on-exit ran this every clean '
        'close; it now lives here on-demand so close stays fast. '
        'Touches no DB — pure filesystem copy.'
    )
    # No DB touched → no VACUUM list.

    _KEEP_NEWEST_DEFAULT = 2

    def _root(self) -> Path:
        return Path(__file__).parent

    def _data_dir(self) -> Path:
        return self._root() / 'data'

    def _backup_root(self) -> Path:
        return self._root() / 'data_backups'

    def _data_size_bytes(self) -> int:
        d = self._data_dir()
        if not d.exists():
            return 0
        try:
            return sum(f.stat().st_size for f in d.rglob('*')
                       if f.is_file())
        except Exception:
            return 0

    def _existing_data_snapshots(self) -> list[Path]:
        br = self._backup_root()
        if not br.exists():
            return []
        try:
            return sorted(
                [p for p in br.iterdir()
                 if p.is_dir() and p.name.startswith('data_')],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except Exception:
            return []

    def dry_run(self, app) -> dict:
        m = _new_manifest(self.id, self.label)
        size = self._data_size_bytes()
        snaps = self._existing_data_snapshots()
        try:
            keep = int((getattr(app, 'cfg', None) or {}).get(
                'cleanup_keep_backups',
                self._KEEP_NEWEST_DEFAULT))
        except Exception:
            keep = self._KEEP_NEWEST_DEFAULT
        # After this run there will be (existing + 1) snapshots;
        # prune will drop the (existing + 1 - keep) oldest.
        n_will_drop = max(0, len(snaps) + 1 - keep)
        m['would_archive'] = 1
        m['would_remove'] = n_will_drop
        m['bytes_freed'] = 0  # backup ADDS bytes; doesn't free any
        m['per_subject']['data/ size to copy'] = _human_bytes(size)
        m['per_subject']['existing data_* snapshots'] = len(snaps)
        m['per_subject']['after-run keep newest'] = keep
        m['per_subject']['snapshots prune would drop'] = n_will_drop
        m['sample'].append(
            f"COPY {self._data_dir().name}/ "
            f"({_human_bytes(size)}) -> "
            f"{self._backup_root().name}/data_<ts>/")
        for p in snaps[keep - 1:]:  # the ones that will rotate out
            m['sample'].append(f"ROTATE OUT {p.name}")
        m['notes'].append(
            f"Adds {_human_bytes(size)} to data_backups/. "
            f"After the copy, prune rotates to keep newest {keep} "
            f"snapshots.")
        if size == 0:
            m['notes'].append("data/ is empty — nothing to copy.")
        return m

    def apply(self, app, options: dict) -> dict:
        r = _new_apply_result(self.id)
        data_dir = self._data_dir()
        if not data_dir.exists():
            r['errors'].append('data/ missing — nothing to snapshot')
            return r
        try:
            self._backup_root().mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            dest = self._backup_root() / f'data_{ts}'
            shutil.copytree(
                str(data_dir), str(dest),
                ignore=shutil.ignore_patterns(
                    '.lock', '.write_test', '__pycache__'))
            r['archived'] = 1
            # bytes_freed reported as the copied size for the manifest
            # (technically bytes WRITTEN, not freed, but the field
            # name is the manifest's accumulator) — the UI shows it
            # with a "+disk_used" note via the task description.
            try:
                r['bytes_freed'] = sum(
                    f.stat().st_size for f in dest.rglob('*')
                    if f.is_file())
            except Exception:
                pass
        except Exception as e:
            r['errors'].append(f'copytree: {e}')
            return r
        # Prune old snapshots, best-effort.
        try:
            keep = int((getattr(app, 'cfg', None) or {}).get(
                'cleanup_keep_backups',
                self._KEEP_NEWEST_DEFAULT))
            snaps = self._existing_data_snapshots()
            for old in snaps[keep:]:
                try:
                    shutil.rmtree(str(old), ignore_errors=True)
                    if not old.exists():
                        r['removed'] += 1
                except Exception as _e:
                    r['errors'].append(f'rmtree {old.name}: {_e}')
        except Exception as _ep:
            r['errors'].append(f'prune: {_ep}')
        return r


# ─── Registry — the place future Tasks plug in ─────────────────────────

CFG_ENABLE_GOSSIP_REMOVAL = 'maintenance_enable_gossip_removal'


def get_task_registry(cfg: dict = None) -> list:
    """Return the ordered list of Task instances for v1.

    v4.14.6.28-public-safe: gossip_removal is gated behind
    `cfg['maintenance_enable_gossip_removal']` (default False). Per the
    entity-filter investigations, the gossip-removal rule over-rejects
    legitimate news for multi-word / suffixed SEC names (e.g. "Amazon"
    headlines under AMZN whose normalized core is "amazon com", or
    "Qualcomm" headlines under QCOM whose normalized core is
    "qualcomm inc/de"). On other users' machines the deletion wouldn't
    self-heal (live filter re-rejects on next ingest), so it stays off
    by default. the user's MAIN config sets the flag True; the public ship
    leaves it unset (default False) so the gossip card never appears in
    a fresh install. When the news pipeline / entity filter gets fixed,
    flipping the default re-enables it.

    Future intelligent news-trimming lands as a new Task here without
    rewrites elsewhere — append it to the list and it gets dry-run,
    apply, manifest, due-check, and UI display for free.
    """
    cfg = cfg or {}
    out = []
    if bool(cfg.get(CFG_ENABLE_GOSSIP_REMOVAL, False)):
        out.append(GossipRemovalTask())
    out.extend([
        GarbageRowsTask(),
        OperationalLogTask(),
        AILogArchiveTask(),
        BackupPileTask(),
        # v4.14.6.34-async-shutdown: on-demand backup snapshot. The
        # close-time auto-backup used to be the dominant shutdown
        # cost (~2.3 GB shutil.copytree blocking close for 30-120s on
        # HDD). It now runs detached after exit when the cfg flag is
        # on; the cfg flag default is now False for new installs so
        # backups happen here when the user clicks Apply.
        BackupSnapshotTask(),
        # v4.14.6.36-cleanup-era: relocated predictions retention.
        # The pre-v4.14.6.36 nightly auto-tick deleted non-BUY
        # records older than 30d silently with no UI surface. Now
        # user-triggered here — same cleanup_tiered logic (BUYs
        # forever, backup + atomic rewrite), but visible.
        PredictionsCleanupTask(),
    ])
    return out


def find_task(task_id: str) -> Optional[Task]:
    for t in get_task_registry():
        if t.id == task_id:
            return t
    return None


# ─── Daemon pause / resume ─────────────────────────────────────────────

def _signal_all_daemons_stop(app) -> list:
    """Signal every background thread/daemon to stop. Best-effort per
    daemon — one failing doesn't block the others. Returns a list of
    status strings for the manifest."""
    log = []
    # The same shutdown sequence _on_close uses, condensed:
    try:
        if getattr(app, '_scheduler', None) is not None:
            app._scheduler.stop()
            log.append('scheduler: stopped')
    except Exception as e:
        log.append(f'scheduler: stop failed ({e})')
    try:
        if getattr(app, '_auto_refresh_stop', None) is not None:
            app._auto_refresh_stop.set()
            log.append('auto-refresh: signalled')
    except Exception as e:
        log.append(f'auto-refresh: stop failed ({e})')
    try:
        import tm_queue_runner
        tm_queue_runner.stop_queue_runner(app)
        log.append('queue runner: signalled')
    except Exception as e:
        log.append(f'queue runner: stop failed ({e})')
    try:
        if getattr(app, '_discovery_refresh_stop', None) is not None:
            app._discovery_refresh_stop.set()
            log.append('discovery refresh: signalled')
    except Exception as e:
        log.append(f'discovery refresh: stop failed ({e})')
    try:
        import tm_fill_executor
        if tm_fill_executor.is_running():
            tm_fill_executor.stop()
            log.append('bulk fill: signalled (settles in ~1.2s)')
    except Exception as e:
        log.append(f'bulk fill: stop failed ({e})')
    try:
        if getattr(app, '_layer2_stop', None) is not None:
            app._layer2_stop.set()
            log.append('layer2 validation: signalled')
    except Exception as e:
        log.append(f'layer2: stop failed ({e})')
    try:
        import tm_layer3_replace
        if hasattr(tm_layer3_replace, 'stop_layer3_replace'):
            tm_layer3_replace.stop_layer3_replace(app)
            log.append('layer3 replace: signalled')
    except Exception as e:
        log.append(f'layer3: stop failed ({e})')
    try:
        # Fundfile daemon
        if hasattr(app, '_fundfile_stop') and app._fundfile_stop is not None:
            app._fundfile_stop.set()
            log.append('fundfile: signalled')
    except Exception:
        pass
    return log


def _wait_for_daemons_quiescent(app, timeout_sec: float = 8.0) -> bool:
    """Poll until all known daemon threads report stopped/no longer
    alive, or timeout. Returns True if quiescent, False on timeout."""
    deadline = time.time() + timeout_sec
    alive_attrs = [
        '_scheduler_thread',
        '_queue_runner_thread',
        '_layer2_thread',
        '_layer3_thread',
        '_fundfile_thread',
        '_discovery_refresh_thread',
    ]
    while time.time() < deadline:
        any_alive = False
        for attr in alive_attrs:
            t = getattr(app, attr, None)
            if t is not None and hasattr(t, 'is_alive') and t.is_alive():
                any_alive = True
                break
        if not any_alive:
            return True
        time.sleep(0.25)
    return False


def _resume_all_daemons(app) -> list:
    """Restart the daemons we previously stopped. Best-effort."""
    log = []
    try:
        # Re-launch scheduler.
        if hasattr(app, '_start_scheduler'):
            app.root.after(0, app._start_scheduler)
            log.append('scheduler: restart scheduled')
    except Exception as e:
        log.append(f'scheduler: restart failed ({e})')
    try:
        import tm_queue_runner
        if hasattr(tm_queue_runner, 'start_queue_runner'):
            tm_queue_runner.start_queue_runner(app)
            log.append('queue runner: restarted')
    except Exception as e:
        log.append(f'queue runner: restart failed ({e})')
    try:
        import tm_layer2_validation as _tl2
        if hasattr(_tl2, 'launch_layer2_validation'):
            _tl2.launch_layer2_validation(app)
            log.append('layer2 validation: restarted')
    except Exception as e:
        log.append(f'layer2: restart failed ({e})')
    try:
        import tm_layer3_replace as _tl3
        if hasattr(_tl3, 'launch_layer3_replace'):
            _tl3.launch_layer3_replace(app)
            log.append('layer3 replace: restarted')
    except Exception as e:
        log.append(f'layer3: restart failed ({e})')
    try:
        import tm_fundfile_fetcher as _tff
        if hasattr(_tff, 'launch_fundfile_refresh'):
            _tff.launch_fundfile_refresh(app)
            log.append('fundfile: restarted')
    except Exception as e:
        log.append(f'fundfile: restart failed ({e})')
    return log


# ─── v4.14.6.30 — post-resume liveness verification ────────────────────
#
# The pre-v4.14.6.30 resume was fire-and-forget: the manifest recorded
# "restarted" the moment the launch call returned, even if the daemon
# crashed on init. A silently-dead daemon would sit dead until the user
# restarted the app — invisible to them since the maintenance dialog
# reported success.
#
# This block adds a deferred (~2.5s) liveness check after resume:
#   - For each daemon `_resume_all_daemons` is supposed to start, look
#     up its thread handle (same attrs `_wait_for_daemons_quiescent`
#     uses) and `.is_alive()` it.
#   - If DEAD, retry the restart ONCE, recheck.
#   - Outcome per daemon ∈ {'alive', 'recovered_on_retry', 'DEAD',
#     'unverified'}. 'unverified' covers the case where a module
#     doesn't expose a thread handle — we'd rather stay silent than
#     cry wolf.
#   - Surviving DEAD daemons are surfaced to the user (via the UI
#     completion dialog) AND to activity.log (via app._log if
#     available) so they're never silently lost.
#
# The check is async (scheduled via app.root.after) and does NOT block
# the engine or UI. The manifest log is rewritten with the verified
# truth once the check completes.

# Display name (for the UI) + thread attr on `app`. ORDER matches
# `_resume_all_daemons`. Daemons whose modules don't currently expose a
# thread handle can be added here later (they'll naturally report
# 'unverified' until then).
_RESUMED_DAEMONS = (
    ('scheduler',    'Scheduler',    '_scheduler_thread'),
    ('queue_runner', 'Queue runner', '_queue_runner_thread'),
    ('layer2',       'Layer-2 validation', '_layer2_thread'),
    ('layer3',       'Layer-3 replace',    '_layer3_thread'),
    ('fundfile',     'Fundfile refresh',   '_fundfile_thread'),
)

# Delay before the post-resume check fires. Long enough for the
# scheduler's `root.after(0, ...)` dispatch + the queue runner / layer
# threads to come up; short enough that the user perceives the
# completion dialog as snappy.
_RESUME_VERIFY_DELAY_MS = 2500
# Delay between retry and re-check, if the first verify finds DEAD.
_RESUME_VERIFY_RETRY_DELAY_MS = 1500


def _check_daemon_alive(app, thread_attr: str) -> str:
    """Returns 'alive' / 'DEAD' / 'unverified'.

    'unverified' = no thread handle exposed (attr missing / None / no
    .is_alive). NOT a failure; the daemon module simply hasn't been
    instrumented for thread-level checks. We log it so it's visible
    but don't cry wolf.
    """
    if not thread_attr:
        return 'unverified'
    t = getattr(app, thread_attr, None)
    if t is None:
        return 'unverified'
    try:
        return 'alive' if t.is_alive() else 'DEAD'
    except Exception:
        return 'unverified'


def _retry_resume_one(app, daemon_id: str) -> tuple[bool, str]:
    """Retry the restart for ONE daemon. Returns (ok, message).
    Mirrors the per-daemon logic in `_resume_all_daemons`, isolated so
    the verifier can target a single daemon."""
    try:
        if daemon_id == 'scheduler':
            if hasattr(app, '_start_scheduler'):
                app.root.after(0, app._start_scheduler)
                return True, 'scheduler: retry restart scheduled'
            return False, 'scheduler: no _start_scheduler attr'
        if daemon_id == 'queue_runner':
            import tm_queue_runner
            if hasattr(tm_queue_runner, 'start_queue_runner'):
                tm_queue_runner.start_queue_runner(app)
                return True, 'queue runner: retry restart issued'
            return False, 'queue runner: no start_queue_runner'
        if daemon_id == 'layer2':
            import tm_layer2_validation as _tl2
            if hasattr(_tl2, 'launch_layer2_validation'):
                _tl2.launch_layer2_validation(app)
                return True, 'layer2: retry restart issued'
            return False, 'layer2: no launch fn'
        if daemon_id == 'layer3':
            import tm_layer3_replace as _tl3
            if hasattr(_tl3, 'launch_layer3_replace'):
                _tl3.launch_layer3_replace(app)
                return True, 'layer3: retry restart issued'
            return False, 'layer3: no launch fn'
        if daemon_id == 'fundfile':
            import tm_fundfile_fetcher as _tff
            if hasattr(_tff, 'launch_fundfile_refresh'):
                _tff.launch_fundfile_refresh(app)
                return True, 'fundfile: retry restart issued'
            return False, 'fundfile: no launch fn'
        return False, f'{daemon_id}: unknown daemon id'
    except Exception as e:
        return False, f'{daemon_id}: retry raised {type(e).__name__}: {e}'


def _verify_resume(app, run: dict,
                   on_done: Optional[Callable[[dict], None]] = None,
                   _retry_phase: bool = False) -> None:
    """Walk every daemon `_resume_all_daemons` is supposed to start,
    check `.is_alive()`, retry once for any found DEAD, recheck, and
    record the verified outcome in `run['resume_verified']`. Then
    rewrite the manifest log on disk and fire the on_done callback.

    Two phases (driven by `_retry_phase`):
      Phase 1 (initial): build initial verdict; if any DEAD, retry each
                         and re-call this function with retry_phase=True.
      Phase 2 (recheck): finalize state, write manifest, surface dead
                         daemons via app._log + on_done callback.
    """
    verified: dict = run.setdefault('resume_verified', {})

    if not _retry_phase:
        # Phase 1: initial check across all resumed daemons.
        dead_ids: list = []
        for daemon_id, display, thread_attr in _RESUMED_DAEMONS:
            state = _check_daemon_alive(app, thread_attr)
            verified[daemon_id] = {
                'display': display, 'state': state, 'retried': False,
            }
            if state == 'DEAD':
                dead_ids.append(daemon_id)

        if dead_ids:
            # Retry each dead daemon once, then schedule the recheck.
            retry_log: list = run.setdefault('resume_retry_log', [])
            for daemon_id in dead_ids:
                ok, msg = _retry_resume_one(app, daemon_id)
                retry_log.append(msg)
                verified[daemon_id]['retried'] = True
                verified[daemon_id]['retry_ok'] = bool(ok)
            try:
                app.root.after(
                    _RESUME_VERIFY_RETRY_DELAY_MS,
                    lambda: _verify_resume(
                        app, run, on_done=on_done, _retry_phase=True))
                return
            except Exception:
                # Fall through to finalization inline (no event loop
                # available — used by smoke tests).
                time.sleep(_RESUME_VERIFY_RETRY_DELAY_MS / 1000.0)
                _verify_resume(
                    app, run, on_done=on_done, _retry_phase=True)
                return
        # No dead daemons — fall through to finalize.

    else:
        # Phase 2: recheck the daemons we retried.
        for daemon_id, display, thread_attr in _RESUMED_DAEMONS:
            entry = verified.get(daemon_id) or {
                'display': display, 'state': 'unverified',
                'retried': False,
            }
            if not entry.get('retried'):
                continue
            new_state = _check_daemon_alive(app, thread_attr)
            if new_state == 'alive':
                entry['state'] = 'recovered_on_retry'
            else:
                # Still not alive (or 'unverified' on a missing attr)
                # — leave it as DEAD so the UI surfaces it. Unverified
                # after retry is essentially indistinguishable from
                # DEAD from the user's perspective (the daemon was
                # supposed to be live and isn't reporting it).
                entry['state'] = 'DEAD'
            verified[daemon_id] = entry

    # Finalize: compute the surviving DEAD list, log warnings, rewrite
    # the manifest, fire the on_done callback.
    survivors_dead = [
        v['display'] for v in verified.values()
        if v.get('state') == 'DEAD'
    ]
    unverified = [
        v['display'] for v in verified.values()
        if v.get('state') == 'unverified'
    ]
    recovered = [
        v['display'] for v in verified.values()
        if v.get('state') == 'recovered_on_retry'
    ]

    # Surface dead daemons to activity.log via the app's logger if
    # available (so non-maintenance-UI users — and any post-hoc log
    # review — see the warning).
    if survivors_dead:
        try:
            warn = (f"[maintenance] WARNING: background task(s) failed "
                    f"to resume after cleanup: "
                    f"{', '.join(survivors_dead)}. Restart the app to "
                    f"restore them.")
            if hasattr(app, '_log'):
                app._log(warn, 'amber')
        except Exception:
            pass
    if recovered:
        try:
            if hasattr(app, '_log'):
                app._log(
                    f"[maintenance] background task(s) recovered on "
                    f"retry after resume: {', '.join(recovered)}.",
                    'green')
        except Exception:
            pass

    # Rewrite the manifest log on disk with verified truth.
    mp = run.get('manifest_path')
    if mp:
        try:
            with open(mp, 'w', encoding='utf-8') as f:
                json.dump(run, f, indent=2, default=str)
        except Exception as e:
            run.setdefault('log', []).append(
                f'manifest rewrite failed: {e}')

    # Fire the UI callback (best-effort; never block on its raise).
    if on_done is not None:
        try:
            on_done(run)
        except Exception:
            pass


# ─── VACUUM helpers ────────────────────────────────────────────────────

def _vacuum_db(db_path: Path, log_fn: Callable[[str], None] = None) -> bool:
    """VACUUM one DB. Requires no other connections — daemons must be
    paused first. Returns True on success."""
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("VACUUM")
            conn.commit()
            if log_fn:
                log_fn(f"vacuum {db_path.name}: ok")
            return True
        finally:
            conn.close()
    except Exception as e:
        if log_fn:
            log_fn(f"vacuum {db_path.name}: failed ({e})")
        return False


# ─── v4.14.6.55-cache-pruning: cache.db row retention ─────────────────
# Age-based row retention for cache.db append-only tables. Approved
# windows (2026-06-17, the user-confirmed):
#   news_signals: 90 days   (prompt window is 7d; 12x safety)
#   daily_bars:   540 days  (~380 trading days; 1.9x safety over the
#                            longest code lookback of 200 trading days)
#   filings:      730 days  (2y)
#   fundamentals: 1825 days (5y)
# Pair: feeds v4.14.6.54 _maybe_vacuum_dbs — DELETE marks pages free,
# VACUUM reclaims them in the same cleanup cycle.
_CACHE_RETENTION_DAYS = {
    'news_signals':  90,
    'daily_bars':    540,
    'filings':       730,
    'fundamentals':  1825,
}
_CACHE_DATE_COLUMNS = {
    'news_signals':  'timestamp',
    'daily_bars':    'date',
    'filings':       'filing_date',
    'fundamentals':  'fiscal_period_end',
}


def prune_cache_db_retention(log_fn=None) -> dict:
    """v4.14.6.55-cache-pruning: DELETE rows older than per-table
    retention. Returns {table: rows_deleted_or_negative_on_error}.
    Each table independently wrapped — one failure does not stop the
    others. Headless-safe. All four target columns are TEXT ISO dates
    (verified 2026-06-17): lexicographic comparison vs a 'YYYY-MM-DD'
    cutoff is valid (e.g. '2026-03-19' < '2026-03-19T08:00:00Z')."""
    import sqlite3
    from pathlib import Path
    from datetime import datetime, timedelta, timezone
    result = {}
    here = Path(__file__).resolve().parent
    db_path = __import__('tm_paths').get_data_dir() / 'cache.db'
    if not db_path.exists():
        if log_fn:
            log_fn(f"[cleanup] prune cache.db: missing ({db_path})",
                   'amber')
        return result
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
    except Exception as e:
        if log_fn:
            log_fn(f"[cleanup] prune cache.db: connect failed: {e}",
                   'amber')
        return result
    try:
        for table, days in _CACHE_RETENTION_DAYS.items():
            col = _CACHE_DATE_COLUMNS[table]
            cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
            cutoff = cutoff_dt.strftime('%Y-%m-%d')
            try:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
                n = cur.rowcount if cur.rowcount is not None else 0
                conn.commit()
                result[table] = n
                if log_fn:
                    log_fn(
                        f"[cleanup] pruned {table}: deleted {n} rows "
                        f"older than {days}d (cutoff {cutoff})",
                        'muted')
            except Exception as e:
                result[table] = -1
                if log_fn:
                    log_fn(
                        f"[cleanup] prune {table}: skipped — "
                        f"{type(e).__name__}: {e}", 'amber')
                continue
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return result


# ─── v4.14.6.98-news-cache-retention: tired_market.db row retention ───
# news_cache was the ONE big append-only table with no scheduled age
# retention — it climbed ~205 MB/month unbounded while every other big
# table was already trim-bounded. A 60-day window caps it without harm:
#   - cycle-safe: the news ingest only ever fetches 7 days back (Finnhub
#     days_back=7; Yahoo/Google/RSS are latest-items feeds), so 60d >> 7d
#     can never delete anything the fetcher will re-request → no
#     delete→re-fetch loop. (Proven by news_signals: same 7d ingest +
#     90d prune = stable, not refilling.)
#   - read-safe: every news_cache reader is ≤7 days by date
#     (get_recent_news hours=24/168; trigger + scheduler "since last
#     check") or latest-N rows with no age bound (_get_recent_rows
#     LIMIT 20). Nothing reads the 7–60d band, so 60d starves no read.
# Prune on `timestamp` — the SAME column every reader filters on (TEXT
# ISO, e.g. '2026-06-19T10:30:02.563235'). A `published_at` column also
# exists, but readers use `timestamp`, so pruning on `timestamp`
# guarantees we never delete a row a reader could still return.
# Lexicographic compare vs a 'YYYY-MM-DD' cutoff is valid for ISO TEXT
# (same idiom as news_signals). DELETE runs in bounded batches so a large
# first-run backlog can't hold a long write lock (readers interleave
# between batches).
_NEWS_CACHE_RETENTION_DAYS = 60
_NEWS_CACHE_DELETE_BATCH = 50000


def prune_tired_market_db_retention(log_fn=None, retention_days=None,
                                    batch_size=None) -> dict:
    """v4.14.6.98-news-cache-retention: age-prune news_cache in
    tired_market.db to retention_days (default 60). Mirrors
    prune_cache_db_retention but targets tired_market.db and DELETEs in
    bounded batches (default 50k rows/commit) so a large first-run
    backlog releases the write lock between chunks instead of locking
    the DB in one shot. Returns {'news_cache': rows_deleted_or_-1}.
    Headless-safe; failsafe-wrapped — never raises to the caller."""
    import sqlite3
    from pathlib import Path
    from datetime import datetime, timedelta, timezone
    days = int(retention_days if retention_days is not None
               else _NEWS_CACHE_RETENTION_DAYS)
    batch = int(batch_size if batch_size is not None
                else _NEWS_CACHE_DELETE_BATCH)
    result = {}
    here = Path(__file__).resolve().parent
    db_path = __import__('tm_paths').get_data_dir() / 'tired_market.db'
    if not db_path.exists():
        if log_fn:
            log_fn(f"[maintenance] prune tired_market.db: missing "
                   f"({db_path})", 'amber')
        return result
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
    except Exception as e:
        if log_fn:
            log_fn(f"[maintenance] prune tired_market.db: connect "
                   f"failed: {e}", 'amber')
        return result
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=days)).strftime('%Y-%m-%d')
    deleted = 0
    try:
        # Bounded-batch DELETE via an id subquery — portable (does not
        # need SQLite compiled with SQLITE_ENABLE_UPDATE_DELETE_LIMIT).
        # Commit per batch so the write lock is short. max_iters is a
        # runaway backstop: batch * cap (=5e8 rows) >> any real backlog.
        max_iters = 10000
        for _ in range(max_iters):
            cur = conn.execute(
                "DELETE FROM news_cache WHERE id IN ("
                "SELECT id FROM news_cache WHERE timestamp < ? "
                "LIMIT ?)", (cutoff, batch))
            n = cur.rowcount if cur.rowcount is not None else 0
            conn.commit()
            if n <= 0:
                break
            deleted += n
        result['news_cache'] = deleted
        if log_fn:
            log_fn(
                f"[maintenance] news_cache: deleted {deleted} rows "
                f"older than {days}d (cutoff {cutoff})", 'muted')
    except Exception as e:
        result['news_cache'] = -1
        if log_fn:
            log_fn(
                f"[maintenance] prune news_cache: skipped — "
                f"{type(e).__name__}: {e}", 'amber')
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return result


# ─── v4.14.6.99-news-signals-slim: one-time blob reclaim ─────────────
# news_signals.summary (~dead, zero readers) + news_signals.url (read
# ONLY as the insert dedup equality key — never followed/displayed) hold
# ~596 MB of Google-News redirect blobs. This migration nulls summary and
# replaces url with a 16-char hash; the write side (tired_market.py) does
# the same for new rows, so the bloat never returns (self-maintaining).

def news_url_key(u):
    """Canonical news url -> dedup key. MUST stay byte-identical to
    tired_market._news_url_key (same sha1[:16]) so migrated rows dedup
    against new write-side rows. Idempotent: <=16-char input passes
    through unchanged (already-hashed / trivially short)."""
    if not u:
        return None
    s = str(u)
    if len(s) <= 16:
        return s
    import hashlib
    return hashlib.sha1(s.encode('utf-8')).hexdigest()[:16]


def migrate_news_signals_slim_storage(log_fn=None, batch_size=50000,
                                      do_vacuum=True) -> dict:
    """v4.14.6.99-news-signals-slim: one-time, idempotent migration of
    cache.db's news_signals table:
      - summary -> NULL  (zero readers anywhere)
      - url     -> 16-char sha1 key (the column's only use is the insert
        dedup, which now hashes both sides — see news_url_key)
    Backs up cache.db to cache.db.bak.pre-v4.14.6.99 BEFORE mutating (the
    restore point; never mutates without it). Batched UPDATEs (commit per
    batch) so the ~1M-row table is never locked in one shot. Idempotent:
    rows already <=16 chars / summary already NULL drop out of the WHERE,
    so a re-run is a no-op. VACUUMs to reclaim freed pages (non-fatal if
    it can't — the scheduled vacuum gets it later). Never raises."""
    import sqlite3
    import shutil
    from pathlib import Path
    out = {'backed_up': False, 'summary_nulled': 0, 'urls_hashed': 0,
           'vacuumed': False, 'error': None}
    here = Path(__file__).resolve().parent
    db_path = __import__('tm_paths').get_data_dir() / 'cache.db'
    if not db_path.exists():
        out['error'] = f'missing {db_path}'
        if log_fn:
            log_fn(f"[migrate] news_signals slim: {out['error']}", 'amber')
        return out
    bak = db_path.with_name('cache.db.bak.pre-v4.14.6.99')
    try:
        if not bak.exists():
            shutil.copy2(str(db_path), str(bak))
            if log_fn:
                log_fn(f"[migrate] cache.db backed up -> {bak.name} "
                       f"(restore point before slim migration)", 'muted')
        out['backed_up'] = True
    except Exception as e:
        out['error'] = f'backup failed: {e}'
        if log_fn:
            log_fn(f"[migrate] news_signals slim ABORTED (no backup) — "
                   f"{out['error']}", 'amber')
        return out  # never mutate without a restore point
    try:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
    except Exception as e:
        out['error'] = f'connect failed: {e}'
        if log_fn:
            log_fn(f"[migrate] news_signals slim: {out['error']}", 'amber')
        return out
    try:
        # 1. summary -> NULL, batched (rows drop out of WHERE as nulled).
        while True:
            cur = conn.execute(
                "UPDATE news_signals SET summary = NULL WHERE id IN ("
                "SELECT id FROM news_signals "
                "WHERE summary IS NOT NULL LIMIT ?)", (int(batch_size),))
            n = cur.rowcount if cur.rowcount is not None else 0
            conn.commit()
            if n <= 0:
                break
            out['summary_nulled'] += n
        # 2. url -> hash, batched. Only real (>16-char) urls; hashed rows
        #    become 16 chars and drop out of the WHERE -> loop terminates
        #    and a re-run is a no-op.
        while True:
            rows = conn.execute(
                "SELECT id, url FROM news_signals "
                "WHERE url IS NOT NULL AND length(url) > 16 "
                "LIMIT ?", (int(batch_size),)).fetchall()
            if not rows:
                break
            conn.executemany(
                "UPDATE news_signals SET url = ? WHERE id = ?",
                [(news_url_key(u), rid) for (rid, u) in rows])
            conn.commit()
            out['urls_hashed'] += len(rows)
    except Exception as e:
        out['error'] = f'update failed: {e}'
        if log_fn:
            log_fn(f"[migrate] news_signals slim: {out['error']}", 'amber')
        try:
            conn.close()
        except Exception:
            pass
        return out
    # 3. VACUUM to reclaim freed pages on disk (the UPDATEs only mark
    #    pages free). Autocommit so VACUUM isn't inside a transaction.
    if do_vacuum:
        try:
            conn.isolation_level = None
            conn.execute("VACUUM")
            out['vacuumed'] = True
        except Exception as e:
            if log_fn:
                log_fn(f"[migrate] news_signals slim: VACUUM deferred "
                       f"({type(e).__name__}) — scheduled vacuum will "
                       f"reclaim.", 'muted')
    try:
        conn.close()
    except Exception:
        pass
    if log_fn:
        log_fn(f"[migrate] news_signals slim complete: summary nulled="
               f"{out['summary_nulled']}, urls hashed={out['urls_hashed']}"
               f", vacuumed={out['vacuumed']}.", 'muted')
    return out


# ─── Engine — orchestrates the run ─────────────────────────────────────

class MaintenanceEngine:
    """Driver: dry-run-all, apply-selected, write manifest log.

    Designed to be called from the in-app Maintenance window. NOT bolted
    onto startup or shutdown — the user's app burst-uses (run for minutes
    at a time); the user explicitly triggers maintenance as an
    in-session pause."""

    def __init__(self, app, log_fn: Callable[[str], None] = None,
                 cfg: dict = None):
        self.app = app
        self.log_fn = log_fn or (lambda m: None)
        # v4.14.6.28-public-safe: thread cfg through so gossip_removal
        # gating works wherever the engine is constructed.
        if cfg is None:
            cfg = getattr(app, 'cfg', None) or {}
        self.cfg = cfg
        self.tasks = get_task_registry(cfg)

    def dry_run_all(self,
                    progress_callback: Callable[[str, dict], None] = None
                    ) -> list:
        """Run each Task's dry_run sequentially. v4.14.6.28: when a
        `progress_callback(task_id, manifest)` is supplied, fire it as
        soon as each task finishes — lets the UI update its per-card
        status incrementally instead of waiting for all 5 to finish."""
        out = []
        for t in self.tasks:
            try:
                m = t.dry_run(self.app)
            except Exception as e:
                m = _new_manifest(t.id, t.label)
                m['error'] = f'{type(e).__name__}: {e}'
            out.append(m)
            if progress_callback is not None:
                try:
                    progress_callback(t.id, m)
                except Exception:
                    pass  # never let a UI callback abort the engine
        return out

    def apply_selected(self, selected_ids: list,
                       options_by_id: dict = None,
                       on_resume_verified: Optional[
                           Callable[[dict], None]] = None) -> dict:
        """Apply the selected tasks. Pauses daemons before any DB write,
        runs each task in its own transaction, VACUUMs affected DBs,
        resumes daemons, writes the manifest log.
        Returns {results: [ApplyResult], log: [str], manifest_path: str,
                 resume_verified: dict}.

        v4.14.6.30 — `on_resume_verified` callback:
          When supplied, after the synchronous resume completes the
          engine schedules a deferred (~2.5s) liveness check via
          `app.root.after`. The check verifies each resumed daemon is
          actually alive, retries any found DEAD once, surfaces
          unrecovered DEAD daemons to the user via `app._log` AND via
          the callback. The manifest log on disk is REWRITTEN with the
          verified truth.

          During the window between resume and verification,
          `run['resume_verified']` carries the dict
              {'state': 'pending', 'message': 'verification in progress'}
          so any caller that reads the dict immediately knows it's not
          yet final.

          If `app.root.after` is unavailable (no Tk root — e.g. smoke
          tests), the engine runs the check inline (after time.sleep),
          so the return semantics stay the same.
        """
        options_by_id = options_by_id or {}
        selected_ids = set(selected_ids)
        run = {
            'started_at': datetime.now().isoformat(timespec='seconds'),
            'results':    [],
            'log':        [],
            'pause_log':  [],
            'resume_log': [],
            # v4.14.6.30 — populated by _verify_resume after the
            # deferred check. Initial 'pending' so consumers know the
            # final state isn't yet decided.
            'resume_verified': {
                '_status': {
                    'state': 'pending',
                    'message': 'verification in progress',
                }
            },
            'resume_retry_log': [],
            'manifest_path': '',
        }

        # 1. Pause daemons.
        self.log_fn('Pausing background tasks…')
        run['pause_log'] = _signal_all_daemons_stop(self.app)
        if not _wait_for_daemons_quiescent(self.app, timeout_sec=10.0):
            run['log'].append(
                'WARN: daemons did not all confirm stop within 10s; '
                'proceeding anyway (WAL keeps writes safe but VACUUM '
                'may abort if another writer touches the DB).')
        else:
            run['log'].append('All daemons confirmed quiescent.')

        # 2. Apply selected tasks. Per-Task isolation.
        affected_dbs = set()
        for t in self.tasks:
            if t.id not in selected_ids:
                continue
            self.log_fn(f'Applying: {t.label}…')
            try:
                opts = options_by_id.get(t.id, {})
                res = t.apply(self.app, opts)
            except Exception as e:
                res = _new_apply_result(t.id)
                res['errors'].append(
                    f'apply uncaught: {type(e).__name__}: {e}')
            run['results'].append(res)
            for db in t.touches_dbs:
                affected_dbs.add(db)
            removed = res.get('removed', 0) + res.get('archived', 0)
            self.log_fn(
                f'  → {removed} item(s), {_human_bytes(res.get("bytes_freed", 0))} freed'
                + (f', errors: {len(res["errors"])}' if res['errors'] else ''))

        # 3. VACUUM affected DBs.
        for db_name in affected_dbs:
            self.log_fn(f'VACUUM {db_name}…')
            root = Path(__file__).parent
            db_path = __import__('tm_paths').get_data_dir() / db_name
            ok = _vacuum_db(db_path,
                            log_fn=lambda m: run['log'].append(m))
            if ok:
                self.log_fn(f'  → VACUUM {db_name} complete')

        # 4. Resume daemons.
        self.log_fn('Resuming background tasks…')
        run['resume_log'] = _resume_all_daemons(self.app)

        # 5. Stamp config + manifest log.
        try:
            cfg = getattr(self.app, 'cfg', None)
            if cfg is not None:
                cfg[CFG_LAST_RUN] = datetime.now().isoformat(
                    timespec='seconds')
                try:
                    import tired_market
                    if hasattr(tired_market, 'save_config'):
                        tired_market.save_config(cfg)
                except Exception:
                    pass
        except Exception:
            pass

        # 6. Write manifest log (initial — v4.14.6.30 rewrites it after
        # the post-resume liveness check below completes, so the
        # on-disk record reflects verified truth, not the optimistic
        # resume_log).
        try:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            log_path = __import__('tm_paths').get_data_dir() / f'cleanup_manifest_{ts}.log'
            with open(log_path, 'w', encoding='utf-8') as f:
                json.dump(run, f, indent=2, default=str)
            run['manifest_path'] = str(log_path)
        except Exception as e:
            run['log'].append(f'manifest write failed: {e}')

        # 7. Schedule the deferred liveness verification.
        # v4.14.6.30 — only when we actually paused/resumed (i.e. there
        # was a resume_log). If nothing ticked, skip the check.
        if run.get('resume_log'):
            try:
                app_root = getattr(self.app, 'root', None)
                if app_root is not None and hasattr(app_root, 'after'):
                    # UI / event loop available — schedule async.
                    app_root.after(
                        _RESUME_VERIFY_DELAY_MS,
                        lambda: _verify_resume(
                            self.app, run,
                            on_done=on_resume_verified))
                else:
                    # No Tk root (smoke tests, headless). Run inline
                    # after a short sleep so semantics match.
                    time.sleep(_RESUME_VERIFY_DELAY_MS / 1000.0)
                    _verify_resume(
                        self.app, run, on_done=on_resume_verified)
            except Exception as e:
                run['log'].append(
                    f'resume verification scheduling failed: {e}')
        else:
            # No pause/resume cycle — mark verified state as N/A.
            run['resume_verified'] = {
                '_status': {
                    'state': 'skipped',
                    'message': 'no pause/resume cycle (no tasks ran)',
                }
            }

        return run


# ─── Due-check ─────────────────────────────────────────────────────────

def compute_due_status(app, cfg: dict = None) -> dict:
    """Cheap dry-run-all + last-run check. Returns:
        {due: bool, reason: str, reclaimable_bytes: int,
         reclaimable_rows: int, days_since_last_run: float | None}
    """
    cfg = cfg or getattr(app, 'cfg', {}) or {}
    threshold_bytes = int(cfg.get(CFG_DUE_BYTES, DEFAULT_DUE_BYTES))
    threshold_rows  = int(cfg.get(CFG_DUE_ROWS,  DEFAULT_DUE_ROWS))
    threshold_days  = int(cfg.get(CFG_DUE_DAYS,  DEFAULT_DUE_DAYS))
    out = {
        'due': False,
        'reason': '',
        'reclaimable_bytes': 0,
        'reclaimable_rows':  0,
        'days_since_last_run': None,
    }
    # Days since last run.
    last = cfg.get(CFG_LAST_RUN)
    if last:
        try:
            dt = datetime.fromisoformat(str(last))
            out['days_since_last_run'] = (
                datetime.now() - dt).total_seconds() / 86400.0
        except Exception:
            pass
    # Reclaimable.
    try:
        engine = MaintenanceEngine(app)
        manifests = engine.dry_run_all()
        for m in manifests:
            out['reclaimable_bytes'] += int(m.get('bytes_freed', 0) or 0)
            out['reclaimable_rows'] += (
                int(m.get('would_remove', 0) or 0)
                + int(m.get('would_archive', 0) or 0))
    except Exception:
        pass
    # Decide.
    reasons = []
    if out['reclaimable_bytes'] >= threshold_bytes:
        reasons.append(f"{_human_bytes(out['reclaimable_bytes'])} reclaimable")
    if out['reclaimable_rows'] >= threshold_rows:
        reasons.append(f"{out['reclaimable_rows']:,} items past their window")
    if (out['days_since_last_run'] is not None
            and out['days_since_last_run'] >= threshold_days):
        reasons.append(
            f"{out['days_since_last_run']:.0f} days since last "
            f"maintenance")
    if reasons:
        out['due'] = True
        out['reason'] = '; '.join(reasons)
    return out
