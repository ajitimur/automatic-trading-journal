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

export interface JournalState {
  tradeCount: number;
  latestRun: RunRecord | null;
}

// The tables the job creates (job/journal/db.py). If the file exists but the
// job has never populated it, these are simply empty.
function tableExists(db: DatabaseSync, name: string): boolean {
  const row = db
    .prepare(`SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?`)
    .get(name);
  return row !== undefined;
}

export function readState(dbPath: string): JournalState {
  // Read-only: the UI never writes. The store outlives the UI session because
  // it is a file, not a daemon (SPEC §13.6).
  const db = new DatabaseSync(dbPath, { readOnly: true });
  try {
    const tradeCount = tableExists(db, 'trade')
      ? (db.prepare('SELECT COUNT(*) AS n FROM trade').get() as { n: number }).n
      : 0;

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

    return { tradeCount, latestRun };
  } finally {
    db.close();
  }
}
