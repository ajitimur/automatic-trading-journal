// The UI reads the same SQLite file the job writes (SPEC §13.6). No interface
// abstracts the store (§13.7) — the UI queries raw SQLite, read-only, and
// simply reflects whatever the job last wrote. It opens even mid-write and
// even if the job has never run.

import { DatabaseSync } from 'node:sqlite';

export interface BookOutcome {
  book: string;
  status: string;
  from_date: string | null;
  to_date: string;
  days_advanced: number;
  error: string | null;
}

export interface RunRecord {
  id: number;
  started_at: string;
  finished_at: string | null;
  as_of_date: string;
  status: string;
  books: BookOutcome[];
}

// One row of the append-only Fill ledger (job/journal/db.py). IBKR gives a
// genuine per-fill commission (SPEC §7.0), so it is shown at fill level.
export interface Fill {
  source: string;
  source_ref: string;
  revision: number;
  book: string;
  symbol: string;
  side: string;
  quantity: number;
  price: number;
  commission: number;
  executed_at: string;
  order_id: string | null;
}

// One allocation of a sell Fill to a Trade (job/journal/db.py trade_exit).
export interface ExitAllocation {
  exit_date: string;
  quantity: number;
  price: number;
}

// A confirmed Trade — an entry-day cohort (ADR 0001) — with its Fills one
// disclosure away (SPEC §5.9): the entry buys that formed it and the exits
// allocated against it.
export interface Trade {
  id: number;
  book: string;
  symbol: string;
  entry_date: string;
  entry_qty: number;
  entry_avg_price: number;
  status: string;
  entryFills: Fill[];
  exits: ExitAllocation[];
}

export interface JournalState {
  tradeCount: number;
  trades: Trade[];
  fillCount: number;
  fills: Fill[];
  latestRun: RunRecord | null;
}

// Show the most recent fills; the ledger grows unbounded, the page is a glance.
const FILL_PREVIEW_LIMIT = 50;

// The tables the job creates (job/journal/db.py). If the file exists but the
// job has never populated it, these are simply empty.
function tableExists(db: DatabaseSync, name: string): boolean {
  const row = db
    .prepare(`SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?`)
    .get(name);
  return row !== undefined;
}

function columnExists(db: DatabaseSync, table: string, column: string): boolean {
  const cols = db.prepare(`PRAGMA table_info(${table})`).all() as Array<{ name: string }>;
  return cols.some((c) => c.name === column);
}

// The confirmed Trades, each with its Fills one disclosure away (SPEC §5.9).
// The entry Fills are the cohort's buys — highest revision per source_ref, since
// a restatement is retained beside the earlier one (ADR 0003).
function readTrades(db: DatabaseSync): Trade[] {
  // A skeleton file may have the old four-column `trade`; only read the ledger
  // once the #23 columns exist, otherwise there are no confirmed Trades to show.
  if (!tableExists(db, 'trade') || !columnExists(db, 'trade', 'entry_avg_price')) {
    return [];
  }
  const trades = db
    .prepare('SELECT * FROM trade ORDER BY entry_date DESC, symbol')
    .all() as unknown as Array<Omit<Trade, 'entryFills' | 'exits'>>;

  const entryStmt = db.prepare(
    `SELECT f.* FROM fill f
     WHERE f.book = ? AND f.symbol = ? AND f.side = 'BUY'
       AND substr(f.executed_at, 1, 10) = ?
       AND f.revision = (SELECT MAX(f2.revision) FROM fill f2
                         WHERE f2.source = f.source AND f2.source_ref = f.source_ref)
     ORDER BY f.executed_at`,
  );
  const exitStmt = db.prepare(
    'SELECT exit_date, quantity, price FROM trade_exit WHERE trade_id = ? ORDER BY exit_date',
  );

  return trades.map((t) => ({
    ...t,
    entryFills: entryStmt.all(t.book, t.symbol, t.entry_date) as unknown as Fill[],
    exits: exitStmt.all(t.id) as unknown as ExitAllocation[],
  }));
}

export function readState(dbPath: string): JournalState {
  // Read-only: the UI never writes. The store outlives the UI session because
  // it is a file, not a daemon (SPEC §13.6).
  const db = new DatabaseSync(dbPath, { readOnly: true });
  try {
    const tradeCount = tableExists(db, 'trade')
      ? (db.prepare('SELECT COUNT(*) AS n FROM trade').get() as { n: number }).n
      : 0;
    const trades = readTrades(db);

    let fillCount = 0;
    let fills: Fill[] = [];
    if (tableExists(db, 'fill')) {
      fillCount = (db.prepare('SELECT COUNT(*) AS n FROM fill').get() as { n: number }).n;
      fills = db
        .prepare('SELECT * FROM fill ORDER BY executed_at DESC, source_ref DESC LIMIT ?')
        .all(FILL_PREVIEW_LIMIT) as unknown as Fill[];
    }

    let latestRun: RunRecord | null = null;
    if (tableExists(db, 'run')) {
      const run = db
        .prepare('SELECT * FROM run ORDER BY id DESC LIMIT 1')
        .get() as Record<string, unknown> | undefined;
      if (run) {
        const books = db
          .prepare('SELECT * FROM run_book WHERE run_id = ? ORDER BY book')
          .all(run.id as number) as unknown as BookOutcome[];
        latestRun = {
          id: run.id as number,
          started_at: run.started_at as string,
          finished_at: (run.finished_at as string | null) ?? null,
          as_of_date: run.as_of_date as string,
          status: run.status as string,
          books,
        };
      }
    }

    return { tradeCount, trades, fillCount, fills, latestRun };
  } finally {
    db.close();
  }
}
