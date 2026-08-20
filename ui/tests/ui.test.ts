// The UI reads the same SQLite file the Python job writes, renders "no Trades
// yet" and the latest run record, and serves on localhost.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { readState } from '../src/db.ts';
import { renderPage } from '../src/render.ts';
import { startServer } from '../src/server.ts';

const REPO = resolve(import.meta.dirname, '..', '..');
const JOB_DIR = join(REPO, 'job');
const FIXTURE = join(REPO, 'docs', 'samples', 'ibkr-flex-schema-fixture.xml');

// Drive the real Python job so the UI reads a real file, not a hand-built one.
function runJob(dbPath: string, asOf: string): void {
  execFileSync('python3', ['-m', 'journal', 'run', '--db', dbPath, '--as-of', asOf], {
    cwd: JOB_DIR,
    env: { ...process.env, PYTHONPATH: JOB_DIR },
    stdio: 'pipe',
  });
}

// Drop the sample Flex file into the same file the UI reads.
function importFlex(dbPath: string): void {
  execFileSync('python3', ['-m', 'journal', 'import', FIXTURE, '--db', dbPath], {
    cwd: JOB_DIR,
    env: { ...process.env, PYTHONPATH: JOB_DIR },
    stdio: 'pipe',
  });
}

// Seed a buy fill and confirm it into a Trade, all through the real job, so the
// UI reads a genuinely confirmed Trade rather than a hand-built row.
function seedConfirmedTrade(dbPath: string): void {
  const py =
    'from journal import db, fills, flex; ' +
    "c=db.connect('" + dbPath + "'); " +
    "fills.insert_fills(c,[flex.Fill(source='ibkr',source_ref='b1',revision=1,book='US'," +
    "symbol='AAA',side='BUY',quantity=100.0,price=10.0,commission=0.0," +
    "executed_at='2026-08-03T09:30:00-04:00',order_id='o1')]); c.close()";
  execFileSync('python3', ['-c', py], {
    cwd: JOB_DIR,
    env: { ...process.env, PYTHONPATH: JOB_DIR },
    stdio: 'pipe',
  });
  // The stop is demanded at the door (ADR 0010); these fixtures decline, then
  // set the stop through the real `journal stop` command where they need one.
  execFileSync('python3', ['-m', 'journal', 'confirm', '--no-stop', 'AAA', '--db', dbPath], {
    cwd: JOB_DIR,
    env: { ...process.env, PYTHONPATH: JOB_DIR },
    stdio: 'pipe',
  });
}

// The two hand-entered fields, set through the real CLI door (#28).
function setStopAndSetup(dbPath: string, tradeId: number): void {
  execFileSync('python3', ['-m', 'journal', 'stop', String(tradeId), '9', '--db', dbPath], {
    cwd: JOB_DIR,
    env: { ...process.env, PYTHONPATH: JOB_DIR },
    stdio: 'pipe',
  });
  execFileSync(
    'python3',
    ['-m', 'journal', 'setup', String(tradeId), 'base_breakout', '--db', dbPath],
    { cwd: JOB_DIR, env: { ...process.env, PYTHONPATH: JOB_DIR }, stdio: 'pipe' },
  );
}

function withDb(fn: (dbPath: string) => Promise<void> | void) {
  return async () => {
    const dir = mkdtempSync(join(tmpdir(), 'journal-ui-'));
    try {
      await fn(join(dir, 'journal.db'));
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  };
}

test(
  'reads the job-written file and renders no-trades + latest run',
  withDb(async (dbPath) => {
    runJob(dbPath, '2026-08-13');

    const state = readState(dbPath);
    assert.equal(state.tradeCount, 0);
    assert.ok(state.latestRun, 'a run record is present');
    assert.equal(state.latestRun?.as_of_date, '2026-08-13');
    assert.equal(state.latestRun?.status, 'ok');
    assert.equal(state.latestRun?.books.length, 2);

    const html = renderPage(state);
    assert.match(html, /No Trades yet\./);
    assert.match(html, /Run <strong>#1<\/strong>/);
    assert.match(html, /2026-08-13/);
  }),
);

test(
  'imported fills are visible in the state and the page',
  withDb(async (dbPath) => {
    runJob(dbPath, '2026-08-13');
    importFlex(dbPath);

    const state = readState(dbPath);
    assert.equal(state.fillCount, 5);
    assert.equal(state.fills.length, 5);

    // The US-Eastern timestamp and a symbol survive into the rendered page.
    const html = renderPage(state);
    assert.match(html, /5 Fill\(s\)/);
    assert.match(html, /SYM1/);
    assert.match(html, /2026-04-01T09:30:00-04:00/);
    assert.doesNotMatch(html, /No Fills yet\./);
  }),
);

test(
  'a confirmed Trade renders with its Fills one disclosure away',
  withDb(async (dbPath) => {
    runJob(dbPath, '2026-08-13');
    seedConfirmedTrade(dbPath);

    const state = readState(dbPath);
    assert.equal(state.tradeCount, 1);
    assert.equal(state.trades.length, 1);
    const trade = state.trades[0];
    assert.ok(trade, 'a confirmed Trade is present');
    assert.equal(trade.symbol, 'AAA');
    assert.equal(trade.entry_avg_price, 10);
    assert.equal(trade.entryFills.length, 1);

    const html = renderPage(state);
    assert.match(html, /1 confirmed Trade\(s\)/);
    assert.match(html, /AAA/);
    // The Fills are behind a disclosure, not on the surface.
    assert.match(html, /<details>[\s\S]*<summary>Fills<\/summary>/);
    assert.doesNotMatch(html, /No Trades yet\./);
    // A Trade with neither stop nor setup renders — the hole shows as "—".
    assert.equal(trade.stop, null);
    assert.equal(trade.setup, null);
  }),
);

test(
  'a Trade with a stop and setup renders them with the derived provenance',
  withDb(async (dbPath) => {
    runJob(dbPath, '2026-08-13');
    seedConfirmedTrade(dbPath);
    setStopAndSetup(dbPath, readState(dbPath).trades[0]!.id);

    const state = readState(dbPath);
    const trade = state.trades[0]!;
    assert.equal(trade.stop, 9);
    assert.equal(trade.setup, 'base_breakout');
    assert.equal(trade.stop_provenance, 'recorded'); // set before any Exit

    const html = renderPage(state);
    assert.match(html, /base_breakout/);
    assert.match(html, /recorded/);
  }),
);

test(
  'a second run shows as a no-op in the latest run record',
  withDb(async (dbPath) => {
    runJob(dbPath, '2026-08-13');
    runJob(dbPath, '2026-08-13');

    const state = readState(dbPath);
    assert.equal(state.latestRun?.status, 'no-op');
    assert.match(renderPage(state), /status <strong>no-op<\/strong>/);
  }),
);

test(
  'serves the page over localhost and shuts down on demand',
  withDb(async (dbPath) => {
    runJob(dbPath, '2026-08-13');
    const ui = await startServer(dbPath, 0);
    try {
      assert.match(ui.url, /^http:\/\/127\.0\.0\.1:\d+\/$/);
      const res = await fetch(ui.url);
      assert.equal(res.status, 200);
      const body = await res.text();
      // `/` is now the weekly review surface (SPEC §11); the diagnostic skeleton
      // moved to `/raw`.
      assert.match(body, /Automatic Trading Journal/);
      assert.match(body, /Weekly review/);
      assert.match(body, /no Trades closed this week/);

      const raw = await fetch(`http://127.0.0.1:${ui.port}/raw`);
      assert.match(await raw.text(), /No Trades yet\./);

      const health = await fetch(`http://127.0.0.1:${ui.port}/health`);
      assert.equal(health.status, 200);
    } finally {
      await ui.close();
    }
  }),
);

test(
  'opens even when the job has never populated the file',
  withDb(async (dbPath) => {
    // A fresh empty SQLite file with no journal tables at all.
    const { DatabaseSync } = await import('node:sqlite');
    new DatabaseSync(dbPath).close();

    const state = readState(dbPath);
    assert.equal(state.tradeCount, 0);
    assert.equal(state.latestRun, null);
    assert.match(renderPage(state), /No Trades yet\./);
    assert.match(renderPage(state), /No run yet/);
  }),
);
