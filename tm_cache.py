"""tm_cache.py — Persistent SQLite cache for v4.15.0.

Replaces the scan-every-4-hours fetch model. Holds price bars, fundamentals,
macro indicators, news signals, filings, and social signals. Read from by
Look Up, Consensus, and Recommendations. Written to by lane fetchers (added
in subsequent steps) and by server bundle importer (added later).

Schema matches /opt/tiredmarket-server/ for tables that mirror server-side
data. Windows-only tables: cache_metadata, social_signals, lane_config.

Foundation only — this module does not fetch data. Lane fetchers in
subsequent v4.15.0 steps will use the write APIs here.

v4.14.6.107-revert-compression: the v4.14.6.106 per-row zstd column
compression was REVERTED. Measured net-negative on this data — the bulk
columns (news_cache.headline, news_signals.title, filings.primary_document_url)
are short exact strings near the entropy floor, so per-row zstd frames inflated
them ~4% (scale-tested on the 1.87GB reference DB; only the small JSON columns
compressed). All target columns are back to plain TEXT and the decompress path
is gone. The footprint lever for this data is RETENTION, not compression. (A
separate corpus-level dictionary-coding idea is being measured independently;
it is unaffected by this revert.) Removing the format change also unblocks the
eventual .exe freeze.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Cache file lives alongside tired_market.db in the writable user-data dir.
# v4.14.6.100-data-dir-resolver: resolved LAZILY through the central
# pointer-driven resolver (tm_paths.get_data_dir) so it follows a relocated /
# %LOCALAPPDATA% data dir instead of freezing SCRIPT_DIR/data at import. The
# module-level name CACHE_DB_PATH is preserved for external importers via
# __getattr__ (PEP 562); internal code calls _cache_db_path().
def _cache_db_path() -> Path:
    import tm_paths
    return tm_paths.get_data_dir() / "cache.db"


def __getattr__(name):
    if name == "CACHE_DB_PATH":
        return _cache_db_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Schema version for future migrations.
SCHEMA_VERSION = 1


# --- Helpers --------------------------------------------------------------

def iso_now() -> str:
    """Current UTC time as ISO-8601 string for consistent cache timestamps."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Read schema_version from _meta. Returns 0 if not set."""
    try:
        row = conn.execute(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            return 0
        return int(row[0])
    except sqlite3.OperationalError:
        return 0


def get_cache_size_bytes() -> int:
    """On-disk size of cache.db in bytes. Returns 0 if file doesn't exist."""
    try:
        return os.path.getsize(_cache_db_path())
    except FileNotFoundError:
        return 0


# --- Schema creation ------------------------------------------------------

_CREATE_TABLES = [
    # Mirrored from server schema (column names and types match)
    """
    CREATE TABLE IF NOT EXISTS tickers (
        ticker            TEXT PRIMARY KEY,
        name              TEXT,
        exchange          TEXT,
        cik               TEXT,
        currency          TEXT,
        first_trade_date  TEXT,
        sector            TEXT,
        market_cap_tier   TEXT,
        last_updated      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_bars (
        ticker     TEXT    NOT NULL,
        date       TEXT    NOT NULL,
        open       REAL,
        high       REAL,
        low        REAL,
        close      REAL,
        adj_close  REAL,
        volume     INTEGER,
        PRIMARY KEY (ticker, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fundamentals (
        ticker              TEXT NOT NULL,
        fiscal_period_end   TEXT NOT NULL,
        revenue             REAL,
        net_income          REAL,
        eps                 REAL,
        gross_margin        REAL,
        operating_margin    REAL,
        total_assets        REAL,
        total_liabilities   REAL,
        shares_outstanding  INTEGER,
        source              TEXT,
        fetched_at          TEXT,          -- v4.14.6.25 row-level fetch stamp
        revenue_cagr_3y          REAL,     -- v4.14.6.76 multi-year growth
        revenue_cagr_1y          REAL,
        revenue_growth_stability REAL,
        eps_cagr_3y              REAL,
        quality_roe              REAL,     -- v4.14.6.77 quality / health
        quality_debt_to_capital  REAL,
        quality_current_ratio    REAL,
        quality_interest_coverage REAL,
        quality_cf_to_sales      REAL,
        PRIMARY KEY (ticker, fiscal_period_end)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS macro_indicators (
        series_id  TEXT NOT NULL,
        date       TEXT NOT NULL,
        value      REAL,
        PRIMARY KEY (series_id, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS splits_dividends (
        ticker        TEXT NOT NULL,
        ex_date       TEXT NOT NULL,
        action_type   TEXT NOT NULL,
        value         REAL,
        PRIMARY KEY (ticker, ex_date, action_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS news_signals (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker       TEXT NOT NULL,
        timestamp    TEXT NOT NULL,
        source       TEXT NOT NULL,
        url          TEXT,
        title        TEXT,
        sentiment    REAL,
        topics       TEXT,
        entities     TEXT,
        summary      TEXT,
        ai_provider  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS filings (
        accession_number      TEXT PRIMARY KEY,
        ticker                TEXT NOT NULL,
        cik                   TEXT NOT NULL,
        filing_date           TEXT NOT NULL,
        report_date           TEXT,
        form_type             TEXT NOT NULL,
        primary_document_url  TEXT,
        description           TEXT
    )
    """,
    # v4.14.5.62-insider-flow: per-ticker aggregate of OPEN-MARKET insider
    # buying/selling (Form-4 codes P/S only) over a trailing window. Computed
    # in the background filings fetcher (per-Form-4 XML fetch+parse); read by
    # the FACTS block. Additive table — CREATE IF NOT EXISTS is safe on both
    # fresh and existing DBs.
    """
    CREATE TABLE IF NOT EXISTS insider_flow (
        ticker                TEXT PRIMARY KEY,
        net_open_market_usd   REAL,
        n_buys                INTEGER,
        n_sells               INTEGER,
        window_days           INTEGER,
        computed_at           TEXT
    )
    """,
    # Windows-only tables (not on server)
    """
    CREATE TABLE IF NOT EXISTS social_signals (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker         TEXT    NOT NULL,
        timestamp      TEXT    NOT NULL,
        source         TEXT    NOT NULL,
        sentiment      REAL,
        message_count  INTEGER,
        summary        TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cache_metadata (
        ticker            TEXT    NOT NULL,
        lane              TEXT    NOT NULL,
        have_from_date    TEXT,
        have_to_date      TEXT,
        target_from_date  TEXT,
        last_refresh_at   TEXT,
        fill_source       TEXT,
        notes             TEXT,
        have_to_period    TEXT,
        fundfile_index_cursor TEXT,
        PRIMARY KEY (ticker, lane)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lane_config (
        lane         TEXT PRIMARY KEY,
        fill_mode    TEXT NOT NULL,
        last_updated TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS _meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # v4.14.5.14-earnings-architecture-fix-v2: persisted earnings lane.
    # One row per ticker. events_json = JSON list of event dicts ('[]' is a
    # CONFIRMED no-event, not "unknown"). status ∈ 'ok'|'empty'|'failed'.
    # as_of stamps ok/empty (drives the TTL). next_retry_at + attempts drive
    # the 'failed' backoff. Survives restarts + cache_metadata clears (its own
    # table) → the reader paths are cache-only + this is the only persistence,
    # so there is no startup re-burst.
    """
    CREATE TABLE IF NOT EXISTS earnings (
        ticker        TEXT PRIMARY KEY,
        events_json   TEXT,
        status        TEXT NOT NULL,
        as_of         TEXT,
        next_retry_at REAL,
        attempts      INTEGER DEFAULT 0,
        source        TEXT
    )
    """,
    # v4.14.5.14-fundamentals-empty-cache: per-ticker fundamentals lookup
    # STATUS (mirrors the `earnings` empty-cache pattern). status ∈
    # 'ok'|'empty'. as_of (ISO) drives the TTL: an 'empty' row means every
    # source confirmed "no fundamentals for this ticker" — the fundfile
    # staleness rotation skips it for FUND_EMPTY_TTL_DAYS instead of re-asking
    # every 30-min cycle (the COFS/CPF/CRML "No fundamentals data" spam). NOT
    # written on 'failed'/'no_source' (a source faulted — retry, don't cache).
    """
    CREATE TABLE IF NOT EXISTS fundamentals_status (
        ticker  TEXT PRIMARY KEY,
        status  TEXT NOT NULL,
        as_of   TEXT,
        source  TEXT
    )
    """,
    # v4.14.5.67-filings-coldfill: per-ticker filings lookup STATUS
    # (mirrors fundamentals_status / earnings empty-cache). status ∈
    # 'ok'|'empty'. as_of (ISO) drives the TTL: an 'empty' row means
    # EDGAR authoritatively returned no filings for this ticker (the
    # ticker isn't in EDGAR's CIK map at all, or it IS but has no
    # filings in our form-filter window). get_unfilled_tickers('filings')
    # honors fresh 'empty' rows so a structural non-filer (preferred-
    # share series like ABR-PD, NYSE-encoded preferreds like AGNCL)
    # stops re-entering the unfilled queue on every restart. NOT
    # written on a transient failure (network/5xx/CIK-map-unloaded) —
    # a transient must stay unfilled so it retries.
    """
    CREATE TABLE IF NOT EXISTS filings_status (
        ticker  TEXT PRIMARY KEY,
        status  TEXT NOT NULL,
        as_of   TEXT,
        source  TEXT
    )
    """,
]

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_daily_bars_ticker_date         ON daily_bars      (ticker, date)",
    "CREATE INDEX IF NOT EXISTS idx_filings_ticker_date            ON filings         (ticker, filing_date)",
    "CREATE INDEX IF NOT EXISTS idx_news_signals_ticker_timestamp  ON news_signals    (ticker, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_social_signals_ticker_ts       ON social_signals  (ticker, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_cache_metadata_lane            ON cache_metadata  (lane)",
]


def init_cache_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open (or create) the cache database and ensure schema is current.

    Idempotent — safe to call repeatedly. Returns an open connection with
    row_factory = sqlite3.Row already set.
    """
    if db_path is None:
        db_path = _cache_db_path()
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # v4.14.5.47-fill-wal-concurrent-start: WAL journal mode + busy timeout.
    #   - journal_mode=WAL: under the old rollback journal ('delete'), a writer
    #     (the fill thread) takes an EXCLUSIVE lock for the duration of each
    #     write, so concurrent READERS (the queue-runner reading prices/
    #     fundamentals for analysis) hit SQLITE_BUSY. WAL lets readers proceed
    #     against the last committed snapshot while one writer appends — the
    #     pick thread no longer blocks on the fill thread. WAL is a PERSISTENT
    #     property of the file, so this also performs the one-time migration on
    #     the first open; re-asserting it on every open is a cheap no-op.
    #   - busy_timeout: WAL still allows only ONE writer at a time. Lever 1 runs
    #     two fill lanes (daily_bars + fundamentals) concurrently, so a brief
    #     writer-vs-writer overlap is possible; busy_timeout makes the second
    #     writer WAIT (up to 5s) for the other's short UPSERT to commit instead
    #     of raising. Every cache write is a short `with conn:` transaction, so
    #     real waits are sub-millisecond. Must run OUTSIDE a transaction
    #     (journal_mode can't change mid-transaction) — hence before `with conn`.
    #   - v4.14.6.65-integrity-walsync (F4): synchronous=NORMAL is the
    #     canonical WAL companion. Under WAL+NORMAL, SQLite skips the per-
    #     commit fsync (it still fsyncs on checkpoint), so the heavy universe
    #     fill does one fewer disk-sync per write — a real throughput win.
    #     Crash-safety tradeoff is acceptable HERE: a power-loss can lose only
    #     the last in-flight transaction and CANNOT corrupt the DB (WAL
    #     guarantee), and cache.db is regenerable market data that re-fills
    #     anyway. (NORMAL would NOT be safe for irreplaceable data; it is for
    #     a cache.) journal_mode=WAL itself was added v4.14.5.47 and is a
    #     persistent file property — re-asserting both on every open is cheap.
    #
    #   - WAL BACKUP RULE (no code here — convention for future patches): the
    #     in-app backups copytree the WHOLE data/ folder, so they already grab
    #     cache.db-wal / cache.db-shm together (WAL-consistent — no change
    #     needed). The ONLY exposure is a manual SINGLE-FILE `cache.db` copy in
    #     a patch/installer flow: recent writes may still live in the -wal
    #     sidecar, so run `PRAGMA wal_checkpoint(TRUNCATE)` BEFORE such a copy
    #     (or copy the -wal/-shm sidecars too), else the copy can miss data.
    #     v65 takes no cache.db backup (no schema change) → not exposed.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        # A PRAGMA failure must never block opening the cache — fall back to
        # whatever mode the file already has (correctness is unaffected; only
        # the contention win is lost).
        pass
    try:
        with conn:
            for stmt in _CREATE_TABLES:
                conn.execute(stmt)
            # v4.14.5.62-8k-descriptions: additive `description` column on the
            # filings table. Fresh DBs already have it (CREATE TABLE above);
            # this ALTER backfills the column on a PRE-EXISTING DB that was
            # created before this version. Idempotent + non-breaking: skip
            # when the column already exists (PRAGMA check), and swallow any
            # error so a migration hiccup never blocks opening the cache.
            try:
                _fcols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(filings)")}
                if 'description' not in _fcols:
                    conn.execute(
                        "ALTER TABLE filings ADD COLUMN description TEXT")
            except Exception:
                pass
            # v4.14.6.25-fundamentals-row-fetched-at: per-row fetch
            # timestamp on `fundamentals`. Pre-fix only fundamentals_status
            # carried an `as_of` (one per ticker), so multi-period rows
            # for one ticker couldn't be aged individually. Same
            # idempotent ADD COLUMN pattern as the description column
            # above. Backfill is NULL — acceptable; existing readers
            # never expected this column.
            try:
                _fucols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(fundamentals)")}
                if 'fetched_at' not in _fucols:
                    conn.execute(
                        "ALTER TABLE fundamentals "
                        "ADD COLUMN fetched_at TEXT")
            except Exception:
                pass
            # v4.14.6.76-growth-factor: multi-year growth metrics computed
            # at EDGAR-fetch time from the companyfacts series the app
            # already downloads (no new fetch). Same idempotent ADD COLUMN
            # pattern. Backfill is NULL — the scorer reads these USE-IF-
            # PRESENT (absent → 0 growth points, never a penalty), and the
            # next fundfile pass recomputes + populates them per ticker.
            try:
                _grcols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(fundamentals)")}
                for _gc in ('revenue_cagr_3y', 'revenue_cagr_1y',
                            'revenue_growth_stability', 'eps_cagr_3y'):
                    if _gc not in _grcols:
                        conn.execute(
                            f"ALTER TABLE fundamentals ADD COLUMN {_gc} REAL")
            except Exception:
                pass
            # v4.14.6.77-quality-factor: quality / financial-health ratios,
            # computed at EDGAR-fetch time from the SAME companyfacts JSON (no
            # new fetch). Same idempotent ADD COLUMN pattern; backfill NULL,
            # scorer reads USE-IF-PRESENT (absent → 0, never a penalty), next
            # fundfile pass recomputes + populates per ticker.
            try:
                _qcols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(fundamentals)")}
                for _qc in ('quality_roe', 'quality_debt_to_capital',
                            'quality_current_ratio', 'quality_interest_coverage',
                            'quality_cf_to_sales'):
                    if _qc not in _qcols:
                        conn.execute(
                            f"ALTER TABLE fundamentals ADD COLUMN {_qc} REAL")
            except Exception:
                pass
            # v4.14.6.111 (Tier-2 float): true tradeable float — already in the
            # Yahoo .info response, previously discarded. Same idempotent ADD
            # COLUMN pattern; backfill NULL, scorer reads USE-IF-PRESENT (absent
            # → 0 contribution, never a penalty), next fundamentals fetch fills it.
            try:
                _fcols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(fundamentals)")}
                if 'float_shares' not in _fcols:
                    conn.execute(
                        "ALTER TABLE fundamentals ADD COLUMN float_shares INTEGER")
            except Exception:
                pass
            # v4.14.6.111 (Tier-3 short interest): short % of float (key squeeze
            # field) + the FINRA settlement date (epoch, for the staleness/lag
            # guard) — already in the Yahoo .info response, previously discarded.
            # Same idempotent ADD COLUMN pattern; backfill NULL, scorer reads
            # USE-IF-PRESENT (absent/stale → 0 contribution, never a penalty).
            try:
                _scols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(fundamentals)")}
                if 'short_percent_float' not in _scols:
                    conn.execute("ALTER TABLE fundamentals "
                                 "ADD COLUMN short_percent_float REAL")
                if 'date_short_interest' not in _scols:
                    conn.execute("ALTER TABLE fundamentals "
                                 "ADD COLUMN date_short_interest INTEGER")
            except Exception:
                pass
            # v4.14.6.25-sec-name-bootstrap: ensure the cik column
            # exists on tickers so the SEC bulk bootstrap can fill it
            # alongside the name. Already present in the original
            # CREATE TABLE; this is just the migration safety net for
            # any DB created before that column shipped.
            try:
                _tkcols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(tickers)")}
                if 'cik' not in _tkcols:
                    conn.execute(
                        "ALTER TABLE tickers ADD COLUMN cik TEXT")
            except Exception:
                pass
            # v4.14.6.43-fundamentals-cache-fix: additive `have_to_period`
            # column on cache_metadata. Stores the MAX(fiscal_period_end)
            # the cache holds per (ticker, lane='fundamentals') so the
            # daemon's staleness check can skip tickers whose cached
            # as_of already covers the latest filed quarter — mirrors the
            # have_to_date pattern daily_bars uses (see CREATE TABLE
            # above + upsert_daily_bars). Idempotent + non-breaking: skip
            # when the column already exists (PRAGMA check), swallow any
            # error so a migration hiccup never blocks opening the cache.
            try:
                _cmcols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(cache_metadata)")}
                if 'have_to_period' not in _cmcols:
                    conn.execute(
                        "ALTER TABLE cache_metadata "
                        "ADD COLUMN have_to_period TEXT")
            except Exception:
                pass
            # v4.14.6.44-fundamentals-bulk-index: additive
            # `fundfile_index_cursor` column. Stores the ISO date the
            # fundamentals daemon has already walked SEC's daily filing
            # index THROUGH (single global row at
            # ticker='__INDEX_CURSOR__', lane='fundamentals'). Mirrors
            # the have_to_period migration pattern above; idempotent +
            # non-breaking via PRAGMA-check + swallowed exception.
            try:
                _cmcols2 = {r[1] for r in conn.execute(
                    "PRAGMA table_info(cache_metadata)")}
                if 'fundfile_index_cursor' not in _cmcols2:
                    conn.execute(
                        "ALTER TABLE cache_metadata "
                        "ADD COLUMN fundfile_index_cursor TEXT")
            except Exception:
                pass
            # v4.14.6.111-last-attempted-filing: additive `last_attempted_filing`
            # column. Stores the most-recent EDGAR filing_date the fundamentals
            # daemon already TRIED to ingest for (ticker, lane='fundamentals').
            # The index-diff re-queue gate compares against
            # MAX(have_to_period, last_attempted_filing), so a filing we already
            # attempted — even when the adapter could only extract an OLD period
            # (have_to_period frozen for delinquent/foreign-20-F/amended filers)
            # — is NOT re-queued until a genuinely NEWER filing appears. Same
            # idempotent PRAGMA-check + swallowed-exception pattern as above.
            try:
                _cmcols3 = {r[1] for r in conn.execute(
                    "PRAGMA table_info(cache_metadata)")}
                if 'last_attempted_filing' not in _cmcols3:
                    conn.execute(
                        "ALTER TABLE cache_metadata "
                        "ADD COLUMN last_attempted_filing TEXT")
            except Exception:
                pass
            for stmt in _CREATE_INDEXES:
                conn.execute(stmt)
            conn.execute(
                "INSERT OR IGNORE INTO _meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(
                "INSERT OR IGNORE INTO _meta (key, value) VALUES ('created_at', ?)",
                (iso_now(),),
            )
    except Exception:
        conn.close()
        raise
    return conn


def get_connection() -> sqlite3.Connection:
    """Return a connection to the cache, initializing schema if needed.

    Connection has row_factory set so callers can index columns by name.
    """
    return init_cache_db(_cache_db_path())


# --- Read APIs ------------------------------------------------------------

def get_ticker_info(ticker: str) -> sqlite3.Row | None:
    """Return single tickers row for ticker, or None if absent."""
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM tickers WHERE ticker = ?",
            (ticker,),
        ).fetchone()
    finally:
        conn.close()


def get_daily_bars(
    ticker: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[sqlite3.Row]:
    """Return daily_bars rows for ticker, optionally bounded by date range.

    Dates are ISO YYYY-MM-DD strings; comparisons rely on SQLite text ordering
    which is correct for ISO-formatted dates.
    """
    sql = "SELECT * FROM daily_bars WHERE ticker = ?"
    params: list[Any] = [ticker]
    if start_date is not None:
        sql += " AND date >= ?"
        params.append(start_date)
    if end_date is not None:
        sql += " AND date <= ?"
        params.append(end_date)
    sql += " ORDER BY date ASC"
    conn = get_connection()
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def get_fundamentals(ticker: str) -> list[sqlite3.Row]:
    """Return fundamentals rows for ticker, ordered by fiscal_period_end asc."""
    conn = get_connection()
    try:
        return list(conn.execute(
            "SELECT * FROM fundamentals WHERE ticker = ? ORDER BY fiscal_period_end ASC",
            (ticker,),
        ).fetchall())
    finally:
        conn.close()


def get_macro_indicators(
    series_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[sqlite3.Row]:
    """Return macro_indicators rows for series_id, optionally bounded by date."""
    sql = "SELECT * FROM macro_indicators WHERE series_id = ?"
    params: list[Any] = [series_id]
    if start_date is not None:
        sql += " AND date >= ?"
        params.append(start_date)
    if end_date is not None:
        sql += " AND date <= ?"
        params.append(end_date)
    sql += " ORDER BY date ASC"
    conn = get_connection()
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def get_news_signals(
    ticker: str,
    since: str | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Return news_signals rows for ticker, newest first."""
    sql = "SELECT * FROM news_signals WHERE ticker = ?"
    params: list[Any] = [ticker]
    if since is not None:
        sql += " AND timestamp >= ?"
        params.append(since)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(int(limit))
    conn = get_connection()
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def get_social_signals(
    ticker: str,
    since: str | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Return social_signals rows for ticker, newest first."""
    sql = "SELECT * FROM social_signals WHERE ticker = ?"
    params: list[Any] = [ticker]
    if since is not None:
        sql += " AND timestamp >= ?"
        params.append(since)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(int(limit))
    conn = get_connection()
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def get_filings(
    ticker: str,
    form_type: str | None = None,
) -> list[sqlite3.Row]:
    """Return filings rows for ticker, optionally filtered by form_type."""
    sql = "SELECT * FROM filings WHERE ticker = ?"
    params: list[Any] = [ticker]
    if form_type is not None:
        sql += " AND form_type = ?"
        params.append(form_type)
    sql += " ORDER BY filing_date DESC"
    conn = get_connection()
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def get_cache_metadata(
    ticker: str,
    lane: str | None = None,
) -> list[sqlite3.Row]:
    """Return cache_metadata rows for ticker, optionally filtered by lane."""
    sql = "SELECT * FROM cache_metadata WHERE ticker = ?"
    params: list[Any] = [ticker]
    if lane is not None:
        sql += " AND lane = ?"
        params.append(lane)
    conn = get_connection()
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def get_lane_config(lane: str | None = None) -> list[sqlite3.Row]:
    """Return lane_config rows, optionally filtered to one lane."""
    if lane is None:
        sql = "SELECT * FROM lane_config ORDER BY lane ASC"
        params: tuple = ()
    else:
        sql = "SELECT * FROM lane_config WHERE lane = ?"
        params = (lane,)
    conn = get_connection()
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


# --- Write APIs -----------------------------------------------------------

def _upsert_many(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    rows: Iterable[dict],
    *,
    pk: list[str] | tuple[str, ...] | str,
    coalesce: bool = False,
) -> int:
    """Column-explicit UPSERT for a table with named columns.

    v4.14.6.29-no-clobber-everywhere: replaces the original
    `INSERT OR REPLACE` implementation, which had two latent issues —
    (1) on a PARTIAL-row write (caller supplies only a subset of cols
    via `row.get(c)` defaulting absent keys to None), every omitted
    column was silently nulled out, the same clobber bug v4.14.6.26
    fixed for `tickers` via the scoped `upsert_tickers`; (2) `INSERT
    OR REPLACE` issues a DELETE then an INSERT under the hood, which
    rotates rowid and is rougher on triggers/FKs than `ON CONFLICT
    DO UPDATE`.

    The replacement is `INSERT ... ON CONFLICT(pk) DO UPDATE SET
    <non-pk-cols>`. Two modes via `coalesce`:

      coalesce=False (default — full overwrite):
        Non-PK cols use `col = excluded.col`. Incoming value always
        wins, INCLUDING when incoming is NULL/0. Use for tables where
        a corrected/recomputed value (potentially clearing a field)
        must apply: daily_bars (corrected OHLCV bars), macro_indicators
        (single value per series-date), splits_dividends (single event
        value), insider_flow (recomputed aggregate — a NULL recompute
        must clear stale data).

      coalesce=True:
        Non-PK cols use `col = COALESCE(excluded.col, table.col)`.
        Incoming non-NULL wins; incoming NULL preserves the stored
        value. Use for append/accumulate tables with multi-source
        partial writes: fundamentals (EDGAR fills some fields, Yahoo
        fills others — neither should null the other's data),
        filings (description column added in v4.14.5.62; pre-existing
        rows have description=NULL, and a future refetch that
        re-supplies metadata shouldn't undo the description that
        landed in between).

    `pk` is the primary-key column name(s) — string or list/tuple.
    Required: there is no safe sensible default per-table without
    knowing the schema; callers always know their PK.
    """
    cols_csv = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    pk_cols = [pk] if isinstance(pk, str) else list(pk)
    non_pk = [c for c in columns if c not in pk_cols]
    if non_pk:
        if coalesce:
            update_clause = ", ".join(
                f"{c} = COALESCE(excluded.{c}, {table}.{c})"
                for c in non_pk
            )
        else:
            update_clause = ", ".join(
                f"{c} = excluded.{c}" for c in non_pk
            )
        pk_csv = ", ".join(pk_cols)
        sql = (
            f"INSERT INTO {table} ({cols_csv}) VALUES ({placeholders}) "
            f"ON CONFLICT({pk_csv}) DO UPDATE SET {update_clause}"
        )
    else:
        # PK-only table (no non-PK cols) — on conflict there is nothing
        # to update, so just keep the existing row.
        sql = (
            f"INSERT INTO {table} ({cols_csv}) VALUES ({placeholders}) "
            f"ON CONFLICT DO NOTHING"
        )
    count = 0
    for row in rows:
        values = [row.get(c) for c in columns]
        conn.execute(sql, values)
        count += 1
    return count


def _insert_many(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    rows: Iterable[dict],
) -> int:
    """Generic INSERT (no UPSERT) for tables with autoincrement id."""
    cols_csv = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({cols_csv}) VALUES ({placeholders})"
    count = 0
    for row in rows:
        values = [row.get(c) for c in columns]
        conn.execute(sql, values)
        count += 1
    return count


_TICKERS_COLS = [
    "ticker", "name", "exchange", "cik", "currency",
    "first_trade_date", "sector", "market_cap_tier", "last_updated",
]

_DAILY_BARS_COLS = [
    "ticker", "date", "open", "high", "low", "close", "adj_close", "volume",
]

_FUNDAMENTALS_COLS = [
    "ticker", "fiscal_period_end", "revenue", "net_income", "eps",
    "gross_margin", "operating_margin", "total_assets", "total_liabilities",
    "shares_outstanding", "source",
    # v4.14.6.111 (Tier-2 float): true tradeable float (Yahoo .info), used by
    # the algo low-float score contribution. USE-IF-PRESENT (NULL → 0).
    "float_shares",
    # v4.14.6.111 (Tier-3 short interest): short % of float + FINRA settlement
    # date (epoch, for the lag guard). USE-IF-PRESENT (NULL/stale → 0).
    "short_percent_float", "date_short_interest",
    # v4.14.6.25-fundamentals-row-fetched-at: per-row ingestion stamp.
    # `_upsert_many` reads each row dict via row.get(col); callers that
    # don't pass `fetched_at` leave it NULL (legacy-safe). The
    # `upsert_fundamentals` wrapper below stamps it automatically so
    # every new write gets a fresh value without changing call sites.
    "fetched_at",
    # v4.14.6.76-growth-factor: multi-year growth metrics (computed at
    # EDGAR fetch time from the companyfacts series). USE-IF-PRESENT —
    # rows from callers that don't supply them stay NULL (COALESCE-safe).
    "revenue_cagr_3y", "revenue_cagr_1y", "revenue_growth_stability",
    "eps_cagr_3y",
    # v4.14.6.77-quality-factor: quality/health ratios (same USE-IF-PRESENT,
    # COALESCE-safe semantics — NULL when a caller doesn't supply them).
    "quality_roe", "quality_debt_to_capital", "quality_current_ratio",
    "quality_interest_coverage", "quality_cf_to_sales",
]

_MACRO_COLS = ["series_id", "date", "value"]

_SPLITS_DIV_COLS = ["ticker", "ex_date", "action_type", "value"]

_NEWS_COLS = [
    "ticker", "timestamp", "source", "url", "title", "sentiment",
    "topics", "entities", "summary", "ai_provider",
]

_FILINGS_COLS = [
    "accession_number", "ticker", "cik", "filing_date", "report_date",
    "form_type", "primary_document_url",
    # v4.14.5.62-8k-descriptions: EDGAR primaryDocDescription (the "what" of
    # the filing). Captured going-forward; NULL on pre-existing date-only rows.
    "description",
]

_SOCIAL_COLS = [
    "ticker", "timestamp", "source", "sentiment", "message_count", "summary",
]


def upsert_tickers(rows: Iterable[dict]) -> int:
    """Non-clobbering upsert into tickers.

    v4.14.6.26-seed-no-clobber: the universe seed at
    tm_fill_executor._seed_universe_if_needed (line ~876) builds rows
    with only {'ticker', 'last_updated'}. Pre-fix this called the
    shared `_upsert_many` which does
    `INSERT OR REPLACE INTO tickers (...all 9 cols...) VALUES (...)`,
    so every seed cycle wiped name / cik / exchange / currency /
    first_trade_date / sector / market_cap_tier back to NULL on every
    existing row — silently undoing the v4.14.6.25 SEC name bootstrap
    on every restart.

    Fix is scoped to THIS function (NOT `_upsert_many` — other tables
    like daily_bars and fundamentals legitimately use full-row REPLACE
    semantics; changing the shared helper would regress them).
    Per-row SQLite UPSERT: INSERT new tickers normally, and on a
    PRIMARY KEY(ticker) conflict UPDATE only last_updated unconditionally
    plus `COALESCE(excluded.X, tickers.X)` for every other column —
    meaning a caller that supplies a value still wins, a caller that
    sends NULL keeps the stored value. This preserves SEC names + CIKs
    across every routine seed AND keeps the door open for a future
    seed source that does carry name/sector/etc. to fill them in.

    Returns the number of rows processed (consistent with
    `_upsert_many` behaviour). Best-effort per-row — a single malformed
    row logs and continues; never raises into the seed caller.
    """
    sql = (
        "INSERT INTO tickers ("
        "  ticker, name, exchange, cik, currency, "
        "  first_trade_date, sector, market_cap_tier, last_updated"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(ticker) DO UPDATE SET "
        "  last_updated     = excluded.last_updated, "
        "  name             = COALESCE(excluded.name, tickers.name), "
        "  cik              = COALESCE(excluded.cik, tickers.cik), "
        "  exchange         = COALESCE(excluded.exchange, tickers.exchange), "
        "  currency         = COALESCE(excluded.currency, tickers.currency), "
        "  first_trade_date = COALESCE(excluded.first_trade_date, "
        "                              tickers.first_trade_date), "
        "  sector           = COALESCE(excluded.sector, tickers.sector), "
        "  market_cap_tier  = COALESCE(excluded.market_cap_tier, "
        "                              tickers.market_cap_tier)"
    )
    conn = get_connection()
    count = 0
    try:
        with conn:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                tk = row.get('ticker')
                if not tk:
                    continue
                try:
                    conn.execute(sql, (
                        tk,
                        row.get('name'),
                        row.get('exchange'),
                        row.get('cik'),
                        row.get('currency'),
                        row.get('first_trade_date'),
                        row.get('sector'),
                        row.get('market_cap_tier'),
                        row.get('last_updated') or iso_now(),
                    ))
                    count += 1
                except Exception:
                    # Per-row failure stays scoped — never blocks the seed.
                    pass
    finally:
        conn.close()
    return count


def upsert_daily_bars(rows: Iterable[dict]) -> int:
    """Upsert rows into daily_bars (full-overwrite via ON CONFLICT).

    v4.14.5.6: also writes cache_metadata.have_from_date/have_to_date
    per ticker. This is the SINGLE chokepoint for every daily_bars
    write (yahoo/stooq, bulk/slow/on-demand), so doing it here
    guarantees have_to_date always reflects actual coverage — the
    freshness check in get_unfilled_tickers reads it. Best-effort:
    a metadata failure must never lose the bar write.
    """
    rows = list(rows)  # materialize: iterated twice (upsert + metadata)
    conn = get_connection()
    try:
        with conn:
            n = _upsert_many(
                conn, "daily_bars", _DAILY_BARS_COLS, rows,
                pk=["ticker", "date"], coalesce=False,
            )
    finally:
        conn.close()

    # Per-ticker coverage → cache_metadata (have_from/have_to date).
    try:
        bounds: dict = {}
        for r in rows:
            t = (r.get('ticker') or '').upper()
            d = r.get('date')
            if not t or not d:
                continue
            d = str(d)[:10]
            lo, hi = bounds.get(t, (d, d))
            bounds[t] = (min(lo, d), max(hi, d))
        for t, (lo, hi) in bounds.items():
            try:
                # Widen, never shrink, existing coverage.
                prev = get_cache_metadata(t, 'daily_bars') or []
                p_from = p_to = None
                for pr in prev:
                    p_from = _row_get(pr, 'have_from_date')
                    p_to = _row_get(pr, 'have_to_date')
                    break
                new_from = min(lo, p_from) if p_from else lo
                new_to = max(hi, p_to) if p_to else hi
                upsert_cache_metadata(
                    t, 'daily_bars',
                    have_from_date=new_from,
                    have_to_date=new_to,
                    fill_source='daily_bars')
            except Exception:
                continue
    except Exception:
        pass
    return n


def upsert_fundamentals(rows: Iterable[dict]) -> int:
    """Upsert rows into fundamentals (COALESCE-preserving via ON CONFLICT).

    v4.14.6.29-no-clobber-everywhere: uses COALESCE on every non-PK
    column so a partial write from one source (e.g. EDGAR fills
    revenue/eps/margins) does not null out fields filled by another
    source (e.g. Yahoo's shares_outstanding). Closes the same clobber
    class v4.14.6.26 fixed for tickers.

    v4.14.6.25-fundamentals-row-fetched-at: every row gets a
    `fetched_at` timestamp stamped here if the caller didn't supply
    one. Callers that pre-stamp (e.g. for backfills with historical
    dates) keep their value. None means the column stores NULL —
    same as legacy rows pre-migration. Cheap helper closes the
    "no per-row fetch ts" data-hygiene gap from the audit.
    """
    rows = list(rows)
    _now = iso_now()
    for r in rows:
        if isinstance(r, dict) and not r.get('fetched_at'):
            r['fetched_at'] = _now
    conn = get_connection()
    try:
        with conn:
            n = _upsert_many(
                conn, "fundamentals", _FUNDAMENTALS_COLS, rows,
                pk=["ticker", "fiscal_period_end"], coalesce=True,
            )
    finally:
        conn.close()

    # v4.14.6.43-fundamentals-cache-fix: stamp cache_metadata.have_to_period
    # with the MAX(fiscal_period_end) per ticker — mirrors the
    # have_to_date pattern upsert_daily_bars uses for daily_bars (~ll.776-806).
    # The daemon's staleness check reads have_to_period and skips tickers
    # whose cached as_of already covers the latest filed quarter, killing
    # the per-cycle re-pull of static EDGAR fundamentals. Best-effort: a
    # metadata failure must never lose the fundamentals write.
    try:
        per_ticker_max: dict = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            t = (r.get('ticker') or '').upper()
            fpe = r.get('fiscal_period_end')
            if not t or not fpe:
                continue
            fpe_s = str(fpe)[:10]
            prev = per_ticker_max.get(t)
            if prev is None or fpe_s > prev:
                per_ticker_max[t] = fpe_s
        for t, new_to_period in per_ticker_max.items():
            try:
                # Widen, never shrink, existing have_to_period coverage.
                prev_rows = get_cache_metadata(t, 'fundamentals') or []
                p_period = None
                for pr in prev_rows:
                    p_period = _row_get(pr, 'have_to_period')
                    break
                merged = (max(new_to_period, p_period)
                          if p_period else new_to_period)
                upsert_cache_metadata(
                    t, 'fundamentals',
                    have_to_period=merged,
                    fill_source='fundamentals')
            except Exception:
                continue
    except Exception:
        pass
    return n


# --- v4.14.5.14-earnings-architecture-fix-v2: persisted earnings lane --------
#
# Single-row-per-ticker upsert (not the multi-row _upsert_many pattern — one
# earnings row per ticker keyed on the PK). These are the ONLY persistence for
# the earnings cache; all the hot reader paths (parse_prediction,
# _check_earnings_window, the earnings triggers) go through get_earnings_cache
# and NEVER fetch — see tm_discover.get_earnings_for_ticker.

def get_earnings_cache(ticker: str) -> sqlite3.Row | None:
    """Return the single earnings row for ticker, or None if not yet seeded."""
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM earnings WHERE ticker = ?",
            ((ticker or "").upper(),),
        ).fetchone()
    finally:
        conn.close()


def get_all_earnings_rows(status: str | None = None) -> list[sqlite3.Row]:
    """All earnings rows (optionally filtered by status). Used by the fundfile
    seeder's recent-earnings prioritization (events are JSON, so date filtering
    happens caller-side)."""
    conn = get_connection()
    try:
        if status:
            return list(conn.execute(
                "SELECT * FROM earnings WHERE status = ?", (status,)).fetchall())
        return list(conn.execute("SELECT * FROM earnings").fetchall())
    finally:
        conn.close()


def upsert_earnings_cache(ticker: str, *, events_json: str, status: str,
                          as_of: str | None = None,
                          next_retry_at: float | None = None,
                          attempts: int = 0,
                          source: str | None = None) -> None:
    """Upsert the earnings row for ticker. status ∈ ok|empty|failed.
    Full-overwrite via ON CONFLICT DO UPDATE (see v4.14.6.29)."""
    t = (ticker or "").upper()
    if not t:
        return
    conn = get_connection()
    try:
        with conn:
            # v4.14.6.29-no-clobber-everywhere: switched from
            # INSERT OR REPLACE to ON CONFLICT DO UPDATE. Same
            # full-overwrite semantic — every status flip /
            # next_retry_at update / attempts reset must apply
            # (including clearing to NULL/0) — but column-explicit
            # and rowid-stable.
            conn.execute(
                "INSERT INTO earnings "
                "(ticker, events_json, status, as_of, next_retry_at, attempts, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ticker) DO UPDATE SET "
                "  events_json   = excluded.events_json, "
                "  status        = excluded.status, "
                "  as_of         = excluded.as_of, "
                "  next_retry_at = excluded.next_retry_at, "
                "  attempts      = excluded.attempts, "
                "  source        = excluded.source",
                (t, events_json, status, as_of, next_retry_at, int(attempts or 0), source),
            )
    finally:
        conn.close()


# ── v4.14.5.14-fundamentals-empty-cache: fundamentals lookup STATUS ──────
# Mirrors the earnings empty-cache (get/upsert/get_all). The fundfile
# staleness rotation reads these to skip tickers it has already confirmed
# have no fundamentals data, for FUND_EMPTY_TTL_DAYS.

def get_fundamentals_status(ticker: str) -> sqlite3.Row | None:
    """Return the fundamentals-status row for ticker, or None if never seeded."""
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM fundamentals_status WHERE ticker = ?",
            ((ticker or "").upper(),),
        ).fetchone()
    finally:
        conn.close()


def get_all_fundamentals_status(status: str | None = None) -> list[sqlite3.Row]:
    """All fundamentals-status rows (optionally filtered by status). The
    fundfile staleness pass loads the 'empty' set once per cycle (one query)
    rather than querying per-ticker."""
    conn = get_connection()
    try:
        if status:
            return list(conn.execute(
                "SELECT * FROM fundamentals_status WHERE status = ?",
                (status,)).fetchall())
        return list(conn.execute(
            "SELECT * FROM fundamentals_status").fetchall())
    finally:
        conn.close()


def upsert_fundamentals_status(ticker: str, *, status: str,
                               as_of: str | None = None,
                               source: str | None = None) -> None:
    """Upsert the fundamentals-status row. status ∈ 'ok'|'empty'.
    Full-overwrite via ON CONFLICT DO UPDATE (see v4.14.6.29).
    Writing 'ok' (data found) clears a prior 'empty' so a ticker that later
    gains coverage is no longer skipped."""
    t = (ticker or "").upper()
    if not t:
        return
    conn = get_connection()
    try:
        with conn:
            # v4.14.6.29-no-clobber-everywhere: full-overwrite via
            # ON CONFLICT DO UPDATE. Status flip ('empty' → 'ok')
            # must apply.
            conn.execute(
                "INSERT INTO fundamentals_status "
                "(ticker, status, as_of, source) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(ticker) DO UPDATE SET "
                "  status = excluded.status, "
                "  as_of  = excluded.as_of, "
                "  source = excluded.source",
                (t, status, as_of, source),
            )
    finally:
        conn.close()


# v4.14.5.67-filings-coldfill: filings empty-cache (mirrors the
# fundamentals + earnings empty-cache pattern). 30-day TTL — matches
# the edgar_no_filer_cache window. The fill executor consults this to
# skip tickers EDGAR has authoritatively confirmed as non-filers, so
# they stop re-entering the unfilled queue every restart.
FILINGS_EMPTY_TTL_DAYS = 30


def get_filings_status(ticker: str) -> sqlite3.Row | None:
    """Return the filings-status row for ticker, or None if never seeded."""
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM filings_status WHERE ticker = ?",
            ((ticker or "").upper(),),
        ).fetchone()
    finally:
        conn.close()


def get_all_filings_status(status: str | None = None) -> list[sqlite3.Row]:
    """All filings-status rows (optionally filtered by status). Used by
    the slow-lane filings pass to load the 'empty' set in ONE query
    instead of asking per-ticker."""
    conn = get_connection()
    try:
        if status:
            return list(conn.execute(
                "SELECT * FROM filings_status WHERE status = ?",
                (status,)).fetchall())
        return list(conn.execute(
            "SELECT * FROM filings_status").fetchall())
    finally:
        conn.close()


def upsert_filings_status(ticker: str, *, status: str,
                          as_of: str | None = None,
                          source: str | None = None) -> None:
    """Upsert the filings-status row. status ∈ 'ok'|'empty'.
    Full-overwrite via ON CONFLICT DO UPDATE (see v4.14.6.29).
    Writing 'ok' (data found) clears a prior 'empty' so a ticker that
    later starts filing is no longer skipped."""
    t = (ticker or "").upper()
    if not t:
        return
    conn = get_connection()
    try:
        with conn:
            # v4.14.6.29-no-clobber-everywhere: full-overwrite via
            # ON CONFLICT DO UPDATE.
            conn.execute(
                "INSERT INTO filings_status "
                "(ticker, status, as_of, source) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(ticker) DO UPDATE SET "
                "  status = excluded.status, "
                "  as_of  = excluded.as_of, "
                "  source = excluded.source",
                (t, status, as_of, source),
            )
    finally:
        conn.close()


def get_fresh_empty_filings_tickers(now_ts: float | None = None) -> set:
    """Return the set of tickers with a fresh 'empty' filings-status
    row (within FILINGS_EMPTY_TTL_DAYS). Returns empty set on any
    failure — empties only ever cause work to be SKIPPED, so a read
    failure is safe (we just process more tickers than necessary)."""
    try:
        import time as _t
        from datetime import datetime as _dt
        now = float(now_ts) if now_ts is not None else _t.time()
        cutoff = now - FILINGS_EMPTY_TTL_DAYS * 86400.0
        out: set = set()
        for row in get_all_filings_status('empty'):
            as_of = (_row_get(row, 'as_of') or '')
            if not as_of:
                continue
            try:
                ts = _dt.fromisoformat(as_of).timestamp()
            except Exception:
                continue
            if ts >= cutoff:
                tk = (_row_get(row, 'ticker') or '').upper()
                if tk:
                    out.add(tk)
        return out
    except Exception:
        return set()


def upsert_macro_indicators(rows: Iterable[dict]) -> int:
    """Upsert rows into macro_indicators (full-overwrite via ON CONFLICT)."""
    conn = get_connection()
    try:
        with conn:
            return _upsert_many(
                conn, "macro_indicators", _MACRO_COLS, rows,
                pk=["series_id", "date"], coalesce=False,
            )
    finally:
        conn.close()


def upsert_splits_dividends(rows: Iterable[dict]) -> int:
    """Upsert rows into splits_dividends (full-overwrite via ON CONFLICT)."""
    conn = get_connection()
    try:
        with conn:
            return _upsert_many(
                conn, "splits_dividends", _SPLITS_DIV_COLS, rows,
                pk=["ticker", "ex_date", "action_type"], coalesce=False,
            )
    finally:
        conn.close()


def insert_news_signals(rows: Iterable[dict]) -> int:
    """INSERT rows into news_signals (autoincrement id, no UPSERT)."""
    conn = get_connection()
    try:
        with conn:
            return _insert_many(conn, "news_signals", _NEWS_COLS, rows)
    finally:
        conn.close()


def upsert_filings(rows: Iterable[dict]) -> int:
    """Upsert rows into filings (COALESCE-preserving via ON CONFLICT;
    PK is accession_number). v4.14.6.29: COALESCE protects the
    description column (added in v4.14.5.62) so a future refetch
    that re-supplies only date/url metadata does not null out a
    description that landed in between."""
    conn = get_connection()
    try:
        with conn:
            return _upsert_many(
                conn, "filings", _FILINGS_COLS, rows,
                pk="accession_number", coalesce=True,
            )
    finally:
        conn.close()


_INSIDER_FLOW_COLS = [
    "ticker", "net_open_market_usd", "n_buys", "n_sells",
    "window_days", "computed_at",
]


def upsert_insider_flow(row: dict) -> None:
    """v4.14.5.62-insider-flow: upsert one per-ticker insider-flow
    aggregate (PK is ticker). Full-overwrite via ON CONFLICT — a
    recompute that returns NULL/0 must CLEAR stale values, so
    COALESCE-preserve is wrong here. `row` keys match
    _INSIDER_FLOW_COLS."""
    conn = get_connection()
    try:
        with conn:
            _upsert_many(
                conn, "insider_flow", _INSIDER_FLOW_COLS, [row],
                pk="ticker", coalesce=False,
            )
    finally:
        conn.close()


def get_insider_flow(ticker: str) -> sqlite3.Row | None:
    """v4.14.5.62-insider-flow: return the insider_flow row for ticker, or
    None. Read-only — never triggers a fetch."""
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM insider_flow WHERE ticker = ?",
            (ticker.upper(),),
        ).fetchone()
    finally:
        conn.close()


def insert_social_signals(rows: Iterable[dict]) -> int:
    """INSERT rows into social_signals (autoincrement id, no UPSERT)."""
    conn = get_connection()
    try:
        with conn:
            return _insert_many(conn, "social_signals", _SOCIAL_COLS, rows)
    finally:
        conn.close()


_CACHE_META_FIELDS = {
    "have_from_date",
    "have_to_date",
    "target_from_date",
    "fill_source",
    "notes",
    # v4.14.6.43-fundamentals-cache-fix: MAX(fiscal_period_end) for
    # fundamentals — drives filing-aware staleness in
    # tm_fundfile_fetcher.refresh_fundamentals_universe.
    "have_to_period",
    # v4.14.6.44-fundamentals-bulk-index: ISO date the fundamentals
    # daemon has walked SEC's daily filing index through (stored on
    # the global sentinel row ticker='__INDEX_CURSOR__',
    # lane='fundamentals').
    "fundfile_index_cursor",
    # v4.14.6.111-last-attempted-filing: most-recent EDGAR filing_date the
    # fundamentals daemon already TRIED to ingest (drives the index-diff
    # re-queue guard in tm_fundfile_fetcher).
    "last_attempted_filing",
}

# v4.14.5.80-cache-metadata-hygiene: write-guard helpers.
#
# Investigation of the v.79 garbage-have_to_date residue found the
# corruption source: when yfinance returns an HTML error/landing page
# instead of OHLCV data, the DataFrame index is string-typed and
# `_v415_cache_write_bars`'s `str(idx)[:10]` fallback captures literal
# HTML text (`('</script`, `('(async()`) as a "date string." That
# string then flows through the per-ticker rows AND through
# earliest_date/latest_date into upsert_cache_metadata. The OHLCV
# columns are safely None-coerced via _f()/_i(); only the date column
# is at risk.
#
# This guard intercepts BAD date strings at upsert_cache_metadata and
# stores NULL instead of the garbage, so no upstream parser bug can
# poison the metadata's date columns again. Lane-agnostic — covers
# daily_bars, fundamentals, and any future lane that reuses these
# fields. Fail-safe: a guard fault never crashes the write.

_CACHE_META_DATE_FIELDS = {
    "have_from_date",
    "have_to_date",
    "target_from_date",
}

_CACHE_METADATA_HYGIENE_ENABLED = True
_HYGIENE_LOG_DEDUP: dict = {}  # field -> last_log_epoch (rate-limited)
_HYGIENE_LOG_INTERVAL_SECONDS = 60.0


def set_cache_metadata_hygiene_enabled(enabled: bool) -> None:
    """Master toggle. When False, upsert_cache_metadata stores whatever
    is passed (legacy behavior). When True (default), date fields that
    don't parse as YYYY-MM-DD (after first-10-char slice) are stored
    as NULL with a rate-limited log line. App init flips this from
    cfg['use_cache_metadata_hygiene']."""
    global _CACHE_METADATA_HYGIENE_ENABLED
    _CACHE_METADATA_HYGIENE_ENABLED = bool(enabled)


def is_cache_metadata_hygiene_enabled() -> bool:
    return _CACHE_METADATA_HYGIENE_ENABLED


def _is_valid_date_string(v) -> bool:
    """True if `v` parses as a valid date when sliced to its first 10
    characters (so 'YYYY-MM-DD' AND 'YYYY-MM-DD HH:MM:SS' both pass —
    fundamentals stores the time-suffixed form legitimately). False
    for HTML fragments, JS snippets, and anything else that isn't an
    ISO date prefix.
    """
    if v is None:
        return True   # NULL is always allowed
    if not isinstance(v, str):
        return False
    if len(v) < 10:
        return False
    from datetime import date as _d
    try:
        _d.fromisoformat(v[:10])
        return True
    except (ValueError, TypeError):
        return False


def _hygiene_log_once(field: str, lane: str, ticker: str,
                       bad_value) -> None:
    """Rate-limited (60s per field) log of a rejected date write.
    Uses Python's stderr because tm_cache has no app-aware logger
    and we don't want to spam activity.log."""
    import sys
    import time as _time
    now = _time.time()
    last = _HYGIENE_LOG_DEDUP.get(field, 0.0)
    if (now - last) < _HYGIENE_LOG_INTERVAL_SECONDS:
        return
    _HYGIENE_LOG_DEDUP[field] = now
    try:
        # Truncate bad value for safety (HTML can be long).
        bv = repr(bad_value)[:80]
        print(
            f"[cache_metadata hygiene] rejected non-date value for "
            f"field {field!r} (lane={lane!r} ticker={ticker!r}): {bv} "
            f"— stored NULL instead.",
            file=sys.stderr)
    except Exception:
        pass


def upsert_cache_metadata(ticker: str, lane: str, **fields: Any) -> None:
    """Upsert a single (ticker, lane) cache_metadata row.

    `last_refresh_at` is set automatically to iso_now(). Other fields are
    optional kwargs; only recognized field names are accepted.

    v4.14.5.80-cache-metadata-hygiene: date fields (have_from_date,
    have_to_date, target_from_date) are validated before write. A
    non-string / non-ISO-date value is replaced with NULL (and logged
    once per 60s per field, to stderr), so no upstream parser bug can
    poison the metadata's date columns. Toggled by
    `set_cache_metadata_hygiene_enabled(False)` for rollback. Fail-
    safe: a guard fault never crashes the write — we fall back to
    the legacy "store whatever was passed" behavior on any exception
    inside the guard.
    """
    unknown = set(fields.keys()) - _CACHE_META_FIELDS
    if unknown:
        raise ValueError(f"unknown cache_metadata fields: {sorted(unknown)}")

    # v4.14.5.80-cache-metadata-hygiene: write-guard on date columns.
    if _CACHE_METADATA_HYGIENE_ENABLED:
        for _df in _CACHE_META_DATE_FIELDS:
            if _df in fields:
                try:
                    if not _is_valid_date_string(fields[_df]):
                        _hygiene_log_once(_df, lane, ticker, fields[_df])
                        fields[_df] = None  # store NULL, not the garbage
                except Exception:
                    pass  # guard fault → legacy write proceeds

    data: dict[str, Any] = {
        "ticker": ticker,
        "lane": lane,
        "last_refresh_at": iso_now(),
    }
    for k in _CACHE_META_FIELDS:
        if k in fields:
            data[k] = fields[k]
    columns = list(data.keys())
    cols_csv = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    # v4.14.6.29-no-clobber-everywhere: cache_metadata UPSERT
    # converted from INSERT OR REPLACE to ON CONFLICT DO UPDATE.
    # Same full-overwrite semantic — `last_refresh_at` is stamped
    # every call and the per-field updates (have_to_date etc.) are
    # always intentional — but column-explicit and rowid-stable.
    # Only updates the columns the caller actually provided (plus
    # last_refresh_at), so an unrelated field already in the row
    # is not touched.
    pk_cols = {"ticker", "lane"}
    update_cols = [c for c in columns if c not in pk_cols]
    if update_cols:
        update_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
        sql = (
            f"INSERT INTO cache_metadata ({cols_csv}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(ticker, lane) DO UPDATE SET {update_clause}"
        )
    else:
        sql = (
            f"INSERT INTO cache_metadata ({cols_csv}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(ticker, lane) DO NOTHING"
        )
    conn = get_connection()
    try:
        with conn:
            conn.execute(sql, [data[c] for c in columns])
    finally:
        conn.close()


# v4.14.6.53-fundamentals-backfill: one-shot idempotent migration.
# Pre-v4.14.6.43 fundamentals rows have NULL have_to_period -- the
# smart skip (v4.14.6.43/.44) can't engage and the 90-day backstop
# wastefully re-pulls them. Stamp the MAX(fiscal_period_end) per
# ticker so the smart skip works immediately. Idempotent: only
# touches NULL rows. Cheap re-scan (filtered NULL query).
def backfill_have_to_period() -> dict:
    """Stamp cache_metadata.have_to_period for every (ticker, lane='fundamentals')
    row whose stamp is NULL. Value = MAX(fiscal_period_end) from the
    fundamentals table for that ticker (truncated to YYYY-MM-DD).
    Returns {'stamped': N, 'no_period_left_null': M, 'already_filled_skipped': K}.
    Idempotent and safe to re-run; the NULL filter makes it a no-op
    after the first run."""
    conn = get_connection()
    try:
        # v4.14.6.110: gate have_to_period backfill (skip when nothing to stamp).
        # Cheap probe instead of a permanent marker: new tickers are stamped by
        # the normal fundamentals write path, but a probe stays correct even if a
        # NULL row ever reappears later. Uses idx_cache_metadata_lane; LIMIT 1
        # early-exits — avoids the full NULL-SELECT + the informational COUNT(*)
        # that ran every startup over the fundamentals-lane rows.
        _probe = conn.execute(
            "SELECT 1 FROM cache_metadata "
            "WHERE lane='fundamentals' AND have_to_period IS NULL LIMIT 1"
        ).fetchone()
        if _probe is None:
            return {'stamped': 0, 'no_period_left_null': 0,
                    'already_filled_skipped': 0, 'skipped': True}
        # Tickers with NULL have_to_period that have rows in fundamentals
        cur = conn.execute(
            "SELECT cm.ticker, "
            "       (SELECT MAX(SUBSTR(f.fiscal_period_end,1,10)) "
            "        FROM fundamentals f WHERE f.ticker = cm.ticker "
            "        AND f.fiscal_period_end IS NOT NULL) AS max_period "
            "FROM cache_metadata cm "
            "WHERE cm.lane='fundamentals' AND cm.have_to_period IS NULL")
        rows = cur.fetchall()
        stamped = 0
        no_period = 0
        for r in rows:
            ticker = _row_get(r, 'ticker') if hasattr(r, 'keys') else r[0]
            max_period = (_row_get(r, 'max_period')
                          if hasattr(r, 'keys') else r[1])
            if not max_period:
                no_period += 1
                continue
            try:
                upsert_cache_metadata(ticker, 'fundamentals',
                                     have_to_period=str(max_period))
                stamped += 1
            except Exception:
                # never raise; migration is best-effort
                continue
        # already-stamped count (informational)
        try:
            already = conn.execute(
                "SELECT COUNT(*) FROM cache_metadata "
                "WHERE lane='fundamentals' AND have_to_period IS NOT NULL"
            ).fetchone()[0]
        except Exception:
            already = -1
        return {'stamped': stamped,
                'no_period_left_null': no_period,
                'already_filled_skipped': int(already),
                'skipped': False}
    finally:
        try: conn.close()
        except Exception: pass


# v4.14.6.44-fundamentals-bulk-index: single global cursor row for the
# SEC daily-filing-index reader. Stored on the sentinel
# ticker='__INDEX_CURSOR__' + lane='fundamentals' so we reuse the
# existing cache_metadata pipeline (no new table, no new migrations
# beyond the column already added).
def get_fundfile_index_cursor() -> str | None:
    """Return the ISO date the fundamentals daemon has walked SEC's
    daily filing index through, or None if never set. Best-effort."""
    try:
        rows = get_cache_metadata('__INDEX_CURSOR__', 'fundamentals') or []
        if not rows:
            return None
        v = _row_get(rows[0], 'fundfile_index_cursor')
        return str(v) if v else None
    except Exception:
        return None


def set_fundfile_index_cursor(date_iso: str) -> None:
    """Advance the fundamentals daemon's SEC daily-index cursor to
    `date_iso` (YYYY-MM-DD). Best-effort: a cursor write fault must
    never break the daemon."""
    try:
        upsert_cache_metadata(
            '__INDEX_CURSOR__', 'fundamentals',
            fundfile_index_cursor=date_iso)
    except Exception:
        pass


def set_lane_config(lane: str, fill_mode: str) -> None:
    """Set fill_mode for a lane; updates last_updated to now."""
    conn = get_connection()
    try:
        with conn:
            # v4.14.6.29-no-clobber-everywhere: full-overwrite via
            # ON CONFLICT DO UPDATE.
            conn.execute(
                "INSERT INTO lane_config (lane, fill_mode, last_updated) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(lane) DO UPDATE SET "
                "  fill_mode    = excluded.fill_mode, "
                "  last_updated = excluded.last_updated",
                (lane, fill_mode, iso_now()),
            )
    finally:
        conn.close()


# --- Import APIs (stubs) --------------------------------------------------

def import_from_archive(archive_path: str) -> dict:
    """Import a server bundle into the cache.

    Stub for now — bundle format is TBD and will be defined when the
    donor-pull flow is wired up. Returns a stub result for callers that
    want to feature-detect.
    """
    # TODO: server bundle format TBD, contact server team when ready to
    # pull bundles. Expected to land mid-v4.15.0 when donor flow lights up.
    return {"imported": 0, "format": "unknown", "status": "stub"}


# --- v4.15.0 Step 9 lane gate -------------------------------------------

def lane_should_fetch(lane: str) -> tuple[bool, str]:
    """v4.15.0 Step 9: Return (should_fetch, fill_mode) for a lane.

    Reads lane_config and returns:
    - (True,  'keyless') — keyless lane, always fetch
    - (True,  'direct')  — user has a key, fetch with it
    - (False, 'skip')    — user opted out, don't fetch
    - (False, 'server')  — donor-server pull (NOT WIRED YET; treated as
                           skip for now)
    - (True,  'keyless') — fallback when lane has no config row yet
                           (defensive: pre-bootstrap or fresh install)

    Called by adapter entry points before any network work. Lives in
    tm_cache because the adapters already lazy-import tm_cache for the
    Step 5 write taps; routing the gate through here keeps the adapters
    module-runtime-independent (no cross-import into tired_market.py).
    """
    try:
        rows = get_lane_config(lane)
        if not rows:
            return (True, 'keyless')  # Defensive default
        row = rows[0]
        fill_mode = row['fill_mode'] if 'fill_mode' in row.keys() else None
        if fill_mode in ('keyless', 'direct'):
            return (True, fill_mode)
        # 'skip' and 'server' (until server-pull wired) both mean don't fetch.
        return (False, fill_mode or 'skip')
    except Exception:
        return (True, 'keyless')  # Defensive on any cache failure


# --- v4.15.0 Step 17: Fill-mode state machine + scope calculator -------------

FILL_MODE_BULK = 'bulk'
FILL_MODE_SERVER = 'server'
FILL_MODE_INCREMENTAL = 'incremental'
FILL_MODE_DIRECT = 'direct'
FILL_MODE_SKIP = 'skip'
FILL_MODE_KEYLESS = 'keyless'

# Lanes that participate in the bulk-fill flow (and thus the state machine).
# Other lanes (macro_indicators) stay on-demand and don't go through
# bulk/incremental transitions.
#
# Lane fill order is INTENTIONAL: daily_bars must come first because it's
# the queue runner's only gating dependency for candidate eligibility
# (see tm_queue_runner._build_candidate_shortlist). Fundamentals second
# (biggest accuracy contribution per ticker filled). Filings last.
#
# This was a frozenset prior to v4.14.3.4 — iteration order depended on
# PYTHONHASHSEED, which made bulk and slow fill randomly choose which
# lane to fill first per launch. On the user's 2026-05-14 morning bulk run,
# the coin flip put fundamentals first, leaving the queue runner with
# zero new candidates from ITOT for over 30 minutes while daily_bars
# stayed at 575/2490. A tuple makes the intent explicit and the order
# version-controllable; future reorderings happen as visible diffs, not
# silent hash-seed flips.
BULK_FILLABLE_LANES = (
    'daily_bars',
    'fundamentals',
    'filings',
)

# Lanes with an active state but different orchestration. News/social fill
# at scheduler pace today; may fold into bulk later.
SCHEDULER_LANES = frozenset({
    'news_signals',
    'social_signals',
})

# Modes that mean "actively filling right now"
ACTIVE_FILL_MODES = frozenset({FILL_MODE_BULK, FILL_MODE_SERVER})

# Modes that mean "data is being maintained, just check for new"
STEADY_STATE_MODES = frozenset({
    FILL_MODE_INCREMENTAL, FILL_MODE_DIRECT, FILL_MODE_KEYLESS,
})


def _row_get(row, key):
    """sqlite3.Row dict-style accessor that swallows KeyError. Rows don't
    have .get(), so this is the safe equivalent."""
    try:
        if key in row.keys():
            return row[key]
    except Exception:
        pass
    return None


def _load_universe_tickers() -> set:
    """v4.15.0 Step 17: Load the set of tradable ticker symbols.

    Resolution order (first non-empty wins):
      1. data/universe.txt or universe.txt in CWD
      2. cache.db.tickers (one row per ticker — populated by future steps)
      3. DISTINCT ticker from cache.db.daily_bars (organic fallback —
         scope is limited to what the program has organically cached)

    Returns empty set on total failure. The universe-source question gets
    more rigorous in Step 18+ when the bulk-fill executor lands.
    """
    candidates = [
        _cache_db_path().parent / 'universe.txt',
        Path('universe.txt'),
        _cache_db_path().parent / 'tickers.txt',
    ]
    for path in candidates:
        try:
            if path.exists():
                with open(path, 'r', encoding='utf-8') as f:
                    out = set()
                    for line in f:
                        sym = line.strip().upper()
                        if sym and not sym.startswith('#'):
                            out.add(sym)
                if out:
                    return out
        except Exception:
            continue

    try:
        conn = get_connection()
        rows = conn.execute("SELECT ticker FROM tickers").fetchall()
        out = {(_row_get(r, 'ticker') or '').upper() for r in rows}
        out.discard('')
        if out:
            return out
    except Exception:
        pass

    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM daily_bars"
        ).fetchall()
        out = {(_row_get(r, 'ticker') or '').upper() for r in rows}
        out.discard('')
        return out
    except Exception:
        return set()


def _price_matches_ranges(price: float, price_ranges: set) -> bool:
    """Check if a given price falls into any of the user's selected ranges.
    Range buckets are inclusive-low, exclusive-high to match the Choices UI."""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return False
    if 'penny' in price_ranges and p < 1.0:
        return True
    if 'low' in price_ranges and 1.0 <= p < 10.0:
        return True
    if 'mid' in price_ranges and 10.0 <= p < 50.0:
        return True
    if 'high' in price_ranges and p >= 50.0:
        return True
    return False


def _get_ticker_latest_prices(tickers: set) -> dict:
    """Fetch latest known close per ticker from cache.db.daily_bars.

    Returns {ticker: price}. Tickers not in cache are absent from the dict.
    Single grouped query — much faster than per-ticker."""
    if not tickers:
        return {}
    try:
        conn = get_connection()
        ticker_list = list(tickers)
        placeholders = ','.join('?' * len(ticker_list))
        query = f"""
            SELECT db.ticker, db.close
            FROM daily_bars db
            INNER JOIN (
                SELECT ticker, MAX(date) AS max_date
                FROM daily_bars
                WHERE ticker IN ({placeholders})
                GROUP BY ticker
            ) latest ON db.ticker = latest.ticker
                    AND db.date = latest.max_date
        """
        rows = conn.execute(query, ticker_list).fetchall()
        out = {}
        for r in rows:
            t = _row_get(r, 'ticker')
            c = _row_get(r, 'close')
            if t and c is not None:
                try:
                    out[t.upper()] = float(c)
                except (TypeError, ValueError):
                    continue
        return out
    except Exception:
        return {}


def get_scope_tickers(lane: str,
                       choices: dict | None = None,
                       universe_path: str | None = None) -> set:
    """v4.15.0 Step 17: Compute which tickers belong in cache for a lane,
    given the user's current Choices (price-range filter).

    lane: 'daily_bars', 'fundamentals', etc. Reserved for future per-lane
          scope rules; today all bulk-fillable lanes share the same scope.
    choices: {'price_ranges': [...], 'style': '...'} — usually from
             _v415_get_choices(cfg). None / no price_ranges → no filter
             (full universe).
    universe_path: optional explicit path to the universe file. None →
                   use _load_universe_tickers() resolution order.

    Tickers without cached price data are EXCLUDED from a filtered scope
    (we can't bucket a ticker we have no price for). When no choices are
    given, the full universe returns regardless of price knowledge.
    """
    if universe_path:
        universe = set()
        try:
            with open(universe_path, 'r', encoding='utf-8') as f:
                for line in f:
                    sym = line.strip().upper()
                    if sym and not sym.startswith('#'):
                        universe.add(sym)
        except Exception:
            universe = _load_universe_tickers()
    else:
        universe = _load_universe_tickers()

    if not universe:
        return set()

    if not choices or not choices.get('price_ranges'):
        return universe

    price_map = _get_ticker_latest_prices(universe)
    price_ranges = set(choices.get('price_ranges', []))

    in_scope = set()
    for ticker in universe:
        price = price_map.get(ticker)
        if price is None:
            continue
        if _price_matches_ranges(price, price_ranges):
            in_scope.add(ticker)
    return in_scope


# v4.14.5.6: default staleness window for the daily_bars freshness
# check. 2 days absorbs a Fri→Mon weekend gap without firing on
# Sat/Sun for tickers that are otherwise fully current. Tunable.
DAILY_BARS_MAX_AGE_DAYS = 2

# v4.14.5.79-daily-bars-incremental: calendar-aware staleness toggle.
# When True (default), the daily_bars freshness check counts BUSINESS
# days only — weekends don't accrue staleness. A ticker filled Friday
# is fresh through Monday morning's restart. When False, the legacy
# naive-calendar cutoff applies (today - max_age_days), which falsely
# stales the whole universe on a Monday morning restart after Friday
# closes.
_INCREMENTAL_DAILY_BARS_ENABLED = True


def set_incremental_daily_bars_enabled(enabled: bool) -> None:
    """Master toggle. Both the calendar-aware staleness cutoff here
    AND the incremental-refetch path in tired_market.py read this
    flag, so one App-init call drives both halves of the build."""
    global _INCREMENTAL_DAILY_BARS_ENABLED
    _INCREMENTAL_DAILY_BARS_ENABLED = bool(enabled)


def is_incremental_daily_bars_enabled() -> bool:
    return _INCREMENTAL_DAILY_BARS_ENABLED


def _business_day_cutoff(today, max_age_days: int):
    """v4.14.5.79-daily-bars-incremental: roll `today` back by
    `max_age_days` BUSINESS days (Mon-Fri), returning the earliest
    date a `have_to_date` can carry and still count as fresh.

    Semantic preserved from naive `today - max_age_days`: a Tue
    restart with Mon-filled bars stays fresh; a Wed restart with
    Mon-filled bars stays fresh (2 business days back from Wed is
    Mon); a Wed restart with Fri-filled bars STALES (3 business days
    back > 2). The fix is purely for weekend gaps: a Mon restart with
    Fri-filled bars previously stalemated under naive cutoff (Mon-2 =
    Sat → Fri 06-05 < Sat 06-06 = stale) but is correctly fresh under
    business-day cutoff (2 business days back from Mon is the prior
    Thursday).

    Holiday awareness is NOT included — that'd need a market calendar
    dependency and adds little (one extra weekday of slack absorbs
    most single-day holidays). Note as future polish.

    Example with max_age_days=2:
      Today=Sun  → walks back to Fri (1) → Thu (2). Cutoff=Thu.
      Today=Mon  → walks back over Sun+Sat → Fri (1) → Thu (2). Cutoff=Thu.
      Today=Tue  → walks back to Mon (1) → Fri (2). Cutoff=Fri.
      Today=Wed  → walks back to Tue (1) → Mon (2). Cutoff=Mon.
    """
    from datetime import timedelta as _td
    d = today
    days_left = int(max_age_days)
    if days_left <= 0:
        return d
    # Walk backwards, only counting weekdays (Mon=0..Fri=4).
    # Hard upper bound on iterations defends against pathological inputs.
    safety = 0
    while days_left > 0 and safety < 1000:
        d = d - _td(days=1)
        safety += 1
        if d.weekday() < 5:
            days_left -= 1
    return d


def get_unfilled_tickers(lane: str,
                          scope_tickers: set,
                          max_age_days: int | None = None) -> set:
    """v4.15.0 Step 17: Return tickers in-scope but NOT adequately
    represented in cache for this lane.

    "Adequately represented" per-lane definitions (presence mode):
      daily_bars     — at least one row exists
      fundamentals   — at least one row with source in (finnhub_deep,
                       yahoo_deep) — snapshot-only rows don't count
      filings        — at least one row exists
      news_signals   — at least one row exists
      social_signals — at least one row exists

    v4.14.5.6: max_age_days is now honored for lane=='daily_bars'.
    When given, a daily_bars ticker counts as adequately represented
    ONLY if it has rows AND a cache_metadata.have_to_date for
    (ticker,'daily_bars') no older than max_age_days days. Tickers
    with zero rows, no cache_metadata row, or a stale have_to_date are
    returned as unfilled — so the slow/bulk fill keeps daily_bars
    fresh universe-wide instead of freezing it after first fill.
    Without max_age_days (or for other lanes) behavior is unchanged
    (presence-only). This is the correctness fix for the v4.14.5.0
    filter, which scores momentum/relative-volume off daily_bars.

    Unknown lane → conservative behavior: treat all scope as unfilled.
    """
    if not scope_tickers:
        return set()

    try:
        conn = get_connection()
    except Exception:
        return set(scope_tickers)

    if lane == 'daily_bars':
        query = "SELECT DISTINCT ticker FROM daily_bars"
    elif lane == 'fundamentals':
        # v4.14.6.43-fundamentals-cache-fix: EDGAR-sourced rows count as
        # filled — without this, every EDGAR-cached ticker stayed
        # "unfilled" forever and the bulk-fill flood re-pulled identical
        # data on every cold-fill. EDGAR is the authoritative SEC source
        # and writes source='edgar' (tm_data_adapter_edgar.py:960).
        query = ("SELECT DISTINCT ticker FROM fundamentals "
                  "WHERE source IN ('finnhub_deep', 'yahoo_deep', 'edgar')")
    elif lane == 'filings':
        # v4.14.5.67-filings-coldfill: a ticker is "adequately
        # represented" if it has filing rows OR a fresh 'empty' status
        # (EDGAR authoritatively returned nothing within the TTL). The
        # empty-merge happens after the row query below — keep the
        # query itself unchanged so any existing read paths are safe.
        query = "SELECT DISTINCT ticker FROM filings"
    elif lane == 'news_signals':
        query = "SELECT DISTINCT ticker FROM news_signals"
    elif lane == 'social_signals':
        query = "SELECT DISTINCT ticker FROM social_signals"
    else:
        return set(scope_tickers)

    try:
        rows = conn.execute(query).fetchall()
        cached = {(_row_get(r, 'ticker') or '').upper() for r in rows}
        cached.discard('')
    except Exception:
        return set(scope_tickers)

    # v4.14.5.67-filings-coldfill: merge in tickers EDGAR has
    # authoritatively confirmed as empty within the TTL — they count
    # as "adequately represented" and should NOT come back into the
    # unfilled queue on every restart. Safe on read failure (the
    # helper returns empty set, so worst case we process more tickers
    # than strictly necessary; never the other way around).
    if lane == 'filings':
        try:
            cached = cached | get_fresh_empty_filings_tickers()
        except Exception:
            pass

    # Presence-only (legacy) for non-daily_bars lanes or when no
    # staleness window is requested.
    if max_age_days is None or lane != 'daily_bars':
        return set(scope_tickers) - cached

    # v4.14.5.6 freshness-aware path (daily_bars only). A ticker is
    # "fresh" iff it has rows AND a cache_metadata.have_to_date for
    # the daily_bars lane that is >= the cutoff date. have_to_date is
    # written as a 'YYYY-MM-DD' string (see upsert_daily_bars), so a
    # lexicographic compare against an ISO cutoff is correct.
    #
    # v4.14.5.79-daily-bars-incremental: cutoff is now BUSINESS-day
    # aware when the flag is on, so a Monday-morning restart with
    # Friday-filled bars doesn't false-stale the whole universe over
    # the weekend (the naive `today - max_age_days` produces Saturday
    # which Friday < ; business-day rollback walks back over weekends
    # and produces the prior Thursday which Friday >= ). Flag off →
    # exact legacy naive-calendar cutoff.
    try:
        from datetime import date as _d
        _today = _d.today()
        if _INCREMENTAL_DAILY_BARS_ENABLED:
            cutoff = _business_day_cutoff(
                _today, int(max_age_days)).isoformat()
        else:
            from datetime import timedelta as _td
            cutoff = (_today - _td(
                days=int(max_age_days))).isoformat()
    except Exception:
        # Defensive: bad max_age_days -> fall back to presence-only.
        return set(scope_tickers) - cached

    fresh: set = set()
    try:
        for r in conn.execute(
                "SELECT ticker, have_to_date FROM cache_metadata "
                "WHERE lane = 'daily_bars'"):
            t = (_row_get(r, 'ticker') or '').upper()
            htd = _row_get(r, 'have_to_date')
            if t and t in cached and htd and str(htd) >= cutoff:
                fresh.add(t)
    except Exception:
        # cache_metadata unreadable -> conservative: treat the
        # presence set as the only "filled" signal (legacy).
        return set(scope_tickers) - cached

    return set(scope_tickers) - fresh


def get_fill_progress(lane: str, scope_tickers: set) -> dict:
    """v4.15.0 Step 17: Progress envelope for a fill operation.

    Returns {'lane', 'scope_total', 'filled', 'unfilled', 'pct_complete'}.
    Empty scope returns 100.0% (nothing to do is the same as done)."""
    if not scope_tickers:
        return {
            'lane': lane,
            'scope_total': 0,
            'filled': 0,
            'unfilled': 0,
            'pct_complete': 100.0,
        }
    unfilled = get_unfilled_tickers(lane, scope_tickers)
    total = len(scope_tickers)
    filled = total - len(unfilled)
    pct = (filled / total * 100.0) if total > 0 else 100.0
    return {
        'lane': lane,
        'scope_total': total,
        'filled': filled,
        'unfilled': len(unfilled),
        'pct_complete': round(pct, 1),
    }


def should_transition_to_incremental(lane: str,
                                       scope_tickers: set,
                                       threshold_pct: float = 95.0) -> bool:
    """v4.15.0 Step 17: True when fill progress >= threshold_pct, default 95%.
    The leeway lets bulk fills complete even when some tickers legitimately
    have no data (delisted, foreign listings, etc.) without blocking the
    state transition forever."""
    progress = get_fill_progress(lane, scope_tickers)
    return progress['pct_complete'] >= threshold_pct


# Rough per-ticker fetch times by lane + source. Tuned for live observation;
# revise as actual fill runs surface real numbers.
_FILL_TIME_PER_TICKER_SECONDS = {
    'daily_bars': {
        'bulk':   1.2,    # 1y bars via yfinance, with politeness delay
        'server': 0.05,   # bulk download from donation server
    },
    'fundamentals': {
        'bulk':   2.5,    # Finnhub /stock/financials-reported
        'server': 0.05,
    },
    'filings': {
        'bulk':   1.5,    # EDGAR submission + filings list
        'server': 0.05,
    },
}


def estimate_fill_seconds(lane: str, mode: str, scope_size: int) -> int:
    """v4.15.0 Step 17: Estimate fill time for one lane.

    Returns 0 for unknown lane/mode combos. Single-source-of-truth for the
    per-ticker time constants lives in _FILL_TIME_PER_TICKER_SECONDS."""
    if scope_size <= 0:
        return 0
    per_ticker = _FILL_TIME_PER_TICKER_SECONDS.get(lane, {}).get(mode)
    if per_ticker is None:
        return 0
    return int(per_ticker * scope_size)


def _format_duration_human(seconds: int) -> str:
    """Plain-English duration: 'about 45 minutes', 'about 2 hours', etc."""
    if seconds <= 0:
        return "no time needed"
    if seconds < 60:
        return f"about {seconds} seconds"
    minutes = seconds // 60
    if minutes < 90:
        return f"about {minutes} minutes"
    hours = minutes // 60
    if hours < 4:
        half_hours = round(minutes / 30)
        if half_hours % 2 == 0:
            return f"about {half_hours // 2} hours"
        return f"about {half_hours / 2:.1f} hours"
    return f"about {hours} hours"


def estimate_full_fill_seconds(choices: dict | None, mode: str) -> dict:
    """v4.15.0 Step 17: Estimate fill time across all bulk-fillable lanes.

    Returns {'mode', 'lanes': {lane: {'scope_size', 'seconds'}, ...},
             'total_seconds', 'human'}."""
    scope = get_scope_tickers('daily_bars', choices)
    scope_size = len(scope)

    lanes = {}
    total = 0
    for lane in BULK_FILLABLE_LANES:
        seconds = estimate_fill_seconds(lane, mode, scope_size)
        lanes[lane] = {'scope_size': scope_size, 'seconds': seconds}
        total += seconds

    return {
        'mode': mode,
        'lanes': lanes,
        'total_seconds': total,
        'human': _format_duration_human(total),
    }


def get_lanes_needing_scope_expansion(old_choices: dict | None,
                                        new_choices: dict | None) -> dict:
    """v4.15.0 Step 17: Diff old vs new Choices, return which tickers a fill
    needs to cover after the change.

    Returns {'changed', 'new_ticker_count', 'lanes_affected', 'new_tickers'}.

    Adding ranges → expansion (added tickers need filling).
    Removing ranges → no fetch (cached data isn't wrong, just unused).
    Same ranges → changed=False, no work."""
    old_scope = get_scope_tickers('daily_bars', old_choices)
    new_scope = get_scope_tickers('daily_bars', new_choices)
    added = new_scope - old_scope
    return {
        'changed': bool(added),
        'new_ticker_count': len(added),
        'lanes_affected': list(BULK_FILLABLE_LANES),
        'new_tickers': added,
    }
