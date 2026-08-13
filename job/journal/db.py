"""The one SQLite file (SPEC §13.6, §13.7).

The store is deliberately **not** abstracted behind an interface — that
speculative second implementation is refused (§13.7). Both entry points speak
raw SQLite against the same file; the UI reads what this job writes.

The schema is applied idempotently on every open, so ``journal run`` creates
the file if absent and is safe to run against an existing one. Later tickets
add columns and tables here; the empty ``trade`` table is what lets the UI
render "no Trades yet" today.
"""

from __future__ import annotations

import os
import sqlite3

# Bumped when the schema changes so a later ticket can migrate rather than guess.
SCHEMA_VERSION = 3

SCHEMA = """
-- Per-book cursor: how far each book has been advanced (SPEC §13.1). NULL
-- last_processed_trading_date means the book has never been processed.
CREATE TABLE IF NOT EXISTS book_cursor (
    book                        TEXT PRIMARY KEY,
    last_processed_trading_date TEXT
);

-- One row per invocation of `journal run`. The observability record the UI
-- surfaces and push channels were rejected in favour of (SPEC §13.6).
CREATE TABLE IF NOT EXISTS run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    as_of_date  TEXT NOT NULL,
    status      TEXT NOT NULL          -- 'ok' | 'no-op' | 'error'
);

-- Per-book status, dates advanced and errors for one run (SPEC §13.6). A run
-- that catches up several days records that it did; a gap reads as a fact.
CREATE TABLE IF NOT EXISTS run_book (
    run_id        INTEGER NOT NULL REFERENCES run(id),
    book          TEXT NOT NULL,
    status        TEXT NOT NULL,       -- 'advanced' | 'no-op' | 'error'
    from_date     TEXT,                -- cursor before this run (NULL if first ever)
    to_date       TEXT NOT NULL,       -- cursor after this run
    days_advanced INTEGER NOT NULL,
    error         TEXT,
    PRIMARY KEY (run_id, book)
);

-- Append-only source of truth (SPEC §3.1, ADR 0003). The IBKR Flex parser
-- (#22) lands one row per execution here; the Stockbit TC drop lands too. A
-- broker restatement arrives as a new `revision` with earlier ones retained
-- (never an edit). Cost attaches at fill level because IBKR provides it there
-- (SPEC §7.0): `commission` is the per-fill pro-rata share, and summing it
-- across an `order_id` reconciles to the broker's order total.
CREATE TABLE IF NOT EXISTS fill (
    source      TEXT NOT NULL,
    source_ref  TEXT NOT NULL,       -- logical execution (ibExecID base)
    revision    INTEGER NOT NULL,    -- version (ibExecID seq); highest wins
    book        TEXT NOT NULL,
    symbol      TEXT NOT NULL DEFAULT '',
    side        TEXT NOT NULL DEFAULT '',   -- 'BUY' | 'SELL'
    quantity    REAL NOT NULL DEFAULT 0,    -- signed, as the broker reports
    price       REAL NOT NULL DEFAULT 0,
    commission  REAL NOT NULL DEFAULT 0,    -- signed, per-fill (SPEC §7.0)
    executed_at TEXT NOT NULL DEFAULT '',   -- ISO 8601 US Eastern, offset kept
    order_id    TEXT,                       -- ibOrderID (cost reconciliation)
    PRIMARY KEY (source, source_ref, revision)
);

-- The unit of analysis, an entry-day cohort derived from fills (SPEC §3.1,
-- ADR 0001). Empty in the skeleton, so the UI renders "no Trades yet".
CREATE TABLE IF NOT EXISTS trade (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    book       TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    entry_date TEXT NOT NULL
);

-- The local bar cache (SPEC §4.4, #24). A trading-day axis: rows are
-- split-adjusted, dividend-unadjusted daily bars, keyed per (book, symbol,
-- date). Zero-volume rows are filtered at the cache boundary (ADR 0005) before
-- they land here, so a stored bar is always a day something traded. The cache
-- is part of the design, not an optimisation: reads come from here, the daily
-- job fills it, and if the source blocks nothing already stored is lost.
CREATE TABLE IF NOT EXISTS bar (
    book     TEXT NOT NULL,
    symbol   TEXT NOT NULL,
    date     TEXT NOT NULL,            -- ISO trading day
    open     REAL NOT NULL,
    high     REAL NOT NULL,
    low      REAL NOT NULL,
    close    REAL NOT NULL,
    volume   INTEGER NOT NULL,         -- always > 0 (zero-volume filtered out)
    dividend REAL NOT NULL DEFAULT 0,  -- cash distribution on this date, if any
    PRIMARY KEY (book, symbol, date)
);

-- Per-fetch metadata (SPEC §4.4). A Trade is fetched more than once (entry,
-- daily while open, post-exit window), so this is per-fetch, not per-Trade.
-- Every fetch records its fetch_date, source and span-check result; a failed
-- span check is recorded here (span_ok = 0) even though the fetch then raises,
-- and the filtered zero-volume count rides along so a long suspension is
-- visible in diagnostics without being an error.
CREATE TABLE IF NOT EXISTS bar_fetch (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    book                 TEXT NOT NULL,
    symbol               TEXT NOT NULL,
    fetch_date           TEXT NOT NULL,
    source               TEXT NOT NULL,
    requested_start      TEXT NOT NULL,
    requested_end        TEXT NOT NULL,
    covered_start        TEXT,
    covered_end          TEXT,
    rows_fetched         INTEGER NOT NULL,
    zero_volume_filtered INTEGER NOT NULL,
    span_ok              INTEGER NOT NULL,   -- 1 pass | 0 repair required
    span_detail          TEXT NOT NULL
);

-- Market regime as a property of a market on a date (SPEC §8, #30), keyed
-- (book, date) and computed once per book per day from the book's benchmark
-- series. A Trade holds two references into this table — entry and exit — and
-- never copies the values, so the cross-market question stays answerable by
-- joining on date. All six primitives are stored regardless of the label
-- (close above/below MA10/20/50 and each MA's slope sign) so the label can be
-- re-cut retroactively with no refetch. label and the primitives are NULL when
-- the benchmark has too little history to compute them (SPEC §7.8); bar_date
-- is the bar actually used for the prior-close stamp (SPEC §8.5).
CREATE TABLE IF NOT EXISTS regime_snapshot (
    book             TEXT NOT NULL,
    date             TEXT NOT NULL,        -- the (book, date) key — decision date
    bar_date         TEXT NOT NULL,        -- prior trading day's close actually used
    label            TEXT,                 -- 5-level ordinal, NULL if insufficient history
    close_above_ma10 INTEGER,              -- boolean 0/1, NULL if < 10 bars
    close_above_ma20 INTEGER,
    close_above_ma50 INTEGER,
    slope_ma10       INTEGER,              -- sign -1/0/1, NULL if < 15 bars
    slope_ma20       INTEGER,
    slope_ma50       INTEGER,
    pct_off_52w_high REAL,                 -- (close/max(high,252) - 1)*100, NULL if < 252 bars
    realized_vol_20d REAL,                 -- std of 20 daily log returns, NULL if < 21 bars
    PRIMARY KEY (book, date)
);
"""


def default_db_path() -> str:
    """Resolve the store path without hardcoding one (SPEC §13.7 seam 3).

    ``$JOURNAL_DB`` wins so ``launchd`` and the tests point it wherever they
    like; the fallback is a plain dotfile directory under HOME — no
    macOS-specific location sits in the job path.
    """
    from_env = os.environ.get("JOURNAL_DB")
    if from_env:
        return from_env
    return os.path.join(
        os.path.expanduser("~"), ".automatic-trading-journal", "journal.db"
    )


def connect(db_path: str) -> sqlite3.Connection:
    """Open (creating if absent) the SQLite file and apply the schema.

    Idempotent: ``CREATE TABLE IF NOT EXISTS`` means an existing file is left
    intact. The parent directory is created so a first run on a fresh machine
    just works.
    """
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate_fill_columns(conn)
    conn.commit()
    return conn


# The fill columns added for #22. ``CREATE TABLE IF NOT EXISTS`` leaves an
# existing skeleton table (four columns) untouched, so bring it forward with
# ALTER — non-destructive and idempotent, since the table is append-only and
# empty at this point in the build.
_FILL_COLUMNS = {
    "symbol": "TEXT NOT NULL DEFAULT ''",
    "side": "TEXT NOT NULL DEFAULT ''",
    "quantity": "REAL NOT NULL DEFAULT 0",
    "price": "REAL NOT NULL DEFAULT 0",
    "commission": "REAL NOT NULL DEFAULT 0",
    "executed_at": "TEXT NOT NULL DEFAULT ''",
    "order_id": "TEXT",
}


def _migrate_fill_columns(conn: sqlite3.Connection) -> None:
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(fill)")}
    for name, decl in _FILL_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE fill ADD COLUMN {name} {decl}")
