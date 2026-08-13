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
SCHEMA_VERSION = 5

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
-- ADR 0001): one symbol, one book, one entry date. Recomputed from Fills, never
-- matched — nothing lands here except through a confirm (SPEC §5.1, issue #23).
-- `entry_avg_price` is the quantity-weighted mean of the cohort's entry fills,
-- so `entry_avg_price * entry_qty` is the cash that actually left the account.
-- A different entry day is a *different* Trade, never an addition (ADR 0001);
-- the (book, symbol, entry_date) uniqueness enforces that at the table.
--
-- `stop` and `setup` are the only two hand-entered fields in the system (SPEC
-- §3.2, #28): both nullable because a Trade commits with neither (§5.5, the
-- chaseable path). `stop_provenance` is *derived, never typed* — 'recorded' if
-- the stop arrived before the Trade's first Exit, 'reconstructed' if after
-- (ADR 0002). `frozen` locks the two hand-entered fields once the freeze fuse
-- fires (§3.5); a stop supplied after freeze is refused, so a Trade frozen
-- without one keeps no Risk % and no R, ever.
CREATE TABLE IF NOT EXISTS trade (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    book            TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    entry_date      TEXT NOT NULL,
    entry_qty       REAL NOT NULL DEFAULT 0,
    entry_avg_price REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'closed'
    stop            REAL,                           -- hand-entered, NULL until supplied
    setup           TEXT,                           -- hand-entered: base_breakout | high_tight_flag | other
    stop_provenance TEXT,                           -- derived: 'recorded' | 'reconstructed'
    frozen          INTEGER NOT NULL DEFAULT 0,     -- 1 once the freeze fuse locks the hand-entered fields
    UNIQUE (book, symbol, entry_date)
);

-- An Exit is an allocation of one sell Fill to a Trade (SPEC §3.1, §3.4). Sells
-- allocate FIFO across open Trades — oldest first — overridable at confirm, and
-- an override may never allocate a Trade more than it holds open. A single sell
-- can split across two Trades, so it lands as several rows all carrying the same
-- `source_ref`; that shared ref is what makes re-confirming the same sell a
-- no-op (the sell is already allocated).
CREATE TABLE IF NOT EXISTS trade_exit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id   INTEGER NOT NULL REFERENCES trade(id),
    source     TEXT NOT NULL,       -- the sell Fill's source
    source_ref TEXT NOT NULL,       -- the sell Fill's logical execution
    exit_date  TEXT NOT NULL,
    quantity   REAL NOT NULL,       -- positive shares allocated to this Trade
    price      REAL NOT NULL,
    reason     TEXT                 -- exit reason (SPEC §5.8): proposed from bars,
                                    -- accepted in bulk, overridable on the review surface
);

-- Remembered parse rules — "a fact once, a rule forever" (SPEC §5.4, #27). A
-- wrong *quantity* is a fact about one fill (corrected in place as a new Fill
-- revision, never remembered); a wrong *symbol* is a *rule* about the parser,
-- remembered here and applied to every future statement **before it reaches the
-- confirm queue**, and used to repair Trades already committed under the wrong
-- symbol. Keyed (source, from_symbol) so one remap is stored once per broker.
CREATE TABLE IF NOT EXISTS parse_rule (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,      -- 'stockbit' | 'ibkr' — the parser the rule corrects
    from_symbol TEXT NOT NULL,      -- the symbol as mis-parsed
    to_symbol   TEXT NOT NULL,      -- the symbol it should be
    created_at  TEXT NOT NULL,
    UNIQUE (source, from_symbol)
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

-- Entry-dated setup enrichment: sections A and B of the field list (SPEC
-- §7.1–§7.2, #29), one row per Trade keyed by trade_id. Everything anchors on
-- the prior close P₋₁ (SPEC §6.3) except volume_ratio, which reads the entry
-- day's own volume. Continuous primitives are stored and booleans/orderings/
-- units derived on read (SPEC §6.4): ma_dist_N carries the signed distance in
-- ADR units and "above MA50" is ma_dist_50 > 0; stack_state is the one stored
-- categorical, the symbol's own MA ordering (setup selection only, never the
-- benchmark's regime). A field is NULL when the instrument's history is too
-- short to compute it honestly (SPEC §7.8); insufficient_history is the
-- comma-joined list of those field names, kept strictly distinct from a
-- span-check failure (which raises before this row is ever written). bar_date
-- records the prior-close bar actually used so the as-of date stays honest.
CREATE TABLE IF NOT EXISTS trade_enrichment (
    trade_id             INTEGER PRIMARY KEY REFERENCES trade(id),
    book                 TEXT NOT NULL,
    symbol               TEXT NOT NULL,
    entry_date           TEXT NOT NULL,
    bar_date             TEXT NOT NULL,     -- prior close P₋₁ actually used
    adr_pct              REAL,              -- normalizer, NULL if < 20 bars
    ma_10                REAL,
    ma_20                REAL,
    ma_50                REAL,
    ma_100               REAL,
    ma_200               REAL,              -- SMA, NULL if < N bars
    ma_dist_10           REAL,
    ma_dist_20           REAL,
    ma_dist_50           REAL,
    ma_dist_100          REAL,
    ma_dist_200          REAL,              -- signed, in ADR units
    stack_state          TEXT,              -- aligned_up|aligned_down|mixed, NULL if any MA null
    prior_move_21d       REAL,
    prior_move_63d       REAL,
    prior_move_126d      REAL,              -- close-to-close over N trading days
    pct_off_52w_high     REAL,              -- NULL if < 252 bars
    rs_63d               REAL,              -- symbol - benchmark, NULL if either < 63 bars
    volume_ratio         REAL,              -- entry-day volume / 50-bar mean
    avg_turnover_20d     REAL,              -- native currency, NULL if < 20 bars
    insufficient_history TEXT NOT NULL DEFAULT ''  -- comma-joined nulled field names
);

-- The keep-forever raw tier (SPEC §13.5, #31). Raw source documents as fetched
-- or dropped — Flex NAV XML today, SoA/TC PDFs later — kept verbatim so the DB
-- stays reconstructible and a parser fix can be re-run over history. It is what
-- lets the rolling-365 IBKR NAV window survive: once a reportDate ages out of
-- the reachable window it is gone from Flex, but the captured XML stays here.
CREATE TABLE IF NOT EXISTS raw_document (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    book       TEXT NOT NULL,
    kind       TEXT NOT NULL,      -- 'nav-flex-xml' | 'soa-pdf' | ...
    fetched_at TEXT NOT NULL,      -- when it was captured
    content    TEXT NOT NULL       -- the raw document, verbatim
);

-- The risk/exposure denominator, and nothing more (SPEC §9). Mark-to-market
-- NAV, not deposited capital; one job, not the book's equity curve. Identity is
-- (book, date) on a *calendar* axis (§9.5) — a NAV row exists on days nothing
-- traded, so this never joins the trading-day bar cache by counting rows.
-- `equity` is the stored denominator, captured and persisted, never re-derived
-- (§9.2): IBKR's `total` and IDX's Equity NAB. Components are book-specific and
-- stored beside it (§9.3) so switching the IDX denominator is a config change
-- rather than a re-read of PDFs: US carries `cash`/`stock`, IDX `portfolio`/
-- `ledger_balance`/`cash_investor`. `provenance` is 'stated' | 'estimated'
-- (§9.3); `raw_ref` points at the keep-forever document the row was read from.
-- Both books write straight through here — no confirm queue (§9.7).
CREATE TABLE IF NOT EXISTS equity_snapshot (
    book           TEXT NOT NULL,
    date           TEXT NOT NULL,      -- calendar axis (§9.5), ISO 8601
    equity         REAL NOT NULL,      -- the denominator: IBKR total, IDX Equity NAB
    provenance     TEXT NOT NULL DEFAULT 'stated',   -- 'stated' | 'estimated'
    source         TEXT NOT NULL,      -- 'ibkr' | 'idx'
    fetch_date     TEXT,               -- when captured/typed
    raw_ref        INTEGER REFERENCES raw_document(id),
    cash           REAL,               -- US component
    stock          REAL,               -- US component
    portfolio      REAL,               -- IDX component
    ledger_balance REAL,               -- IDX component
    cash_investor  REAL,               -- IDX component
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
    _migrate_trade_columns(conn)
    _migrate_trade_exit_columns(conn)
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


# The trade columns added for #23. The skeleton (#21) created `trade` with only
# (id, book, symbol, entry_date); a Trade now also carries its derived entry
# quantity, quantity-weighted average price and lifecycle status. Same ALTER
# migration as the fill table — non-destructive and idempotent, and the table is
# empty at this point since nothing lands without a confirm.
_TRADE_COLUMNS = {
    "entry_qty": "REAL NOT NULL DEFAULT 0",
    "entry_avg_price": "REAL NOT NULL DEFAULT 0",
    "status": "TEXT NOT NULL DEFAULT 'open'",
    # The two hand-entered fields and their derived companions (#28).
    "stop": "REAL",
    "setup": "TEXT",
    "stop_provenance": "TEXT",
    "frozen": "INTEGER NOT NULL DEFAULT 0",
}


def _migrate_trade_columns(conn: sqlite3.Connection) -> None:
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(trade)")}
    for name, decl in _TRADE_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE trade ADD COLUMN {name} {decl}")


# The exit reason added for the full confirm queue (#27). An earlier build
# created `trade_exit` without it; bring it forward with the same non-destructive
# ALTER. Nullable, because a reason is proposed at confirm and may be left at the
# proposal until the review surface overrides it (SPEC §5.8).
_TRADE_EXIT_COLUMNS = {
    "reason": "TEXT",
}


def _migrate_trade_exit_columns(conn: sqlite3.Connection) -> None:
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(trade_exit)")}
    for name, decl in _TRADE_EXIT_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE trade_exit ADD COLUMN {name} {decl}")
