// The weekly review surface — variant D (SPEC §11, #40). The render is a pure
// function over ReviewState, so the acceptance criteria are checked against a
// hand-built state; readReview and the write-through actions are exercised
// against a real store driven through the same CLI door the surface POSTs to.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { readReview, type ReviewState, type ReviewTrade } from '../src/review.ts';
import { renderReview } from '../src/reviewRender.ts';
import { startServer } from '../src/server.ts';

const REPO = resolve(import.meta.dirname, '..', '..');
const JOB_DIR = join(REPO, 'job');

function journal(dbPath: string, args: string[]): void {
  execFileSync('python3', ['-m', 'journal', ...args, '--db', dbPath], {
    cwd: JOB_DIR,
    env: { ...process.env, PYTHONPATH: JOB_DIR },
    stdio: 'pipe',
  });
}

function withDb(fn: (dbPath: string) => Promise<void> | void) {
  return async () => {
    const dir = mkdtempSync(join(tmpdir(), 'journal-review-'));
    try {
      await fn(join(dir, 'journal.db'));
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  };
}

// ── a fully-populated ReviewTrade for the pure-render tests ──
function tradeFixture(over: Partial<ReviewTrade> = {}): ReviewTrade {
  return {
    id: 1, book: 'US', symbol: 'AVGO', entry_date: '2026-07-31',
    entry_qty: 60, entry_avg_price: 318.4, status: 'closed',
    stop: 302, setup: 'base_breakout', stop_provenance: 'recorded', frozen: 0,
    reviewed_at: null, note: 'Took the partial a day late on purpose.',
    adr_pct: 3.1,
    exits: [
      { id: 11, exit_date: '2026-08-04', quantity: 20, price: 336.1, reason: 'partial_strength',
        day: 3, mfe_high: 338, mfe_date: '2026-08-04', mfe_adr: 1.99, left_on_table_adr: 0.19 },
      { id: 12, exit_date: '2026-08-07', quantity: 40, price: 341.9, reason: 'close_below_ma10',
        day: 6, mfe_high: 349.1, mfe_date: '2026-08-05', mfe_adr: 3.11, left_on_table_adr: 2.4 },
    ],
    final_exit_date: '2026-08-07',
    mfe_high: 349.1, mfe_date: '2026-08-05', mfe_adr: 3.11, mfe_day: 4,
    mae_low: 311.2, mae_date: '2026-08-03', mae_adr: -0.73, mae_day: 2,
    realized_r: 1.42, realized_pct: 7.0, risk_pct: 0.98, exposure_pct: 11.4,
    risk_provenance: 'stated', fwd_return_20d: null,
    chased: true, stop_distance_adr: 1.66,
    adherence: {
      nominal_variant: 'ma10/day3', best_fit: 'ma10/day3', partial_state: 'in_band',
      partial_timing_delta: null, trail_exit_delta: 0, nominal_status: 'resolved',
      exit_path: 'trail', deviation_cost_r: 0,
      nominal_legs: [
        { date: '2026-08-04', day: 3, trigger: 'partial', limit_locked: false },
        { date: '2026-08-06', day: 5, trigger: 'trail', limit_locked: false },
      ],
      na_note: null,
    },
    open_r: null, days_held: null, last_close: null,
    remaining_fuse: null, insufficient_history: [],
    timeline_days: 8, entry_day: 1,
    ...over,
  };
}

function stateFixture(over: Partial<ReviewState> = {}): ReviewState {
  const week = tradeFixture();
  const idxWeek = tradeFixture({
    id: 2, book: 'IDX', symbol: 'BBCA', entry_avg_price: 9825, stop: 9500,
    stop_provenance: 'reconstructed', chased: null, realized_r: 0.61,
  });
  const straggler = tradeFixture({
    id: 3, book: 'US', symbol: 'SMCI', entry_date: '2026-07-10',
    final_exit_date: '2026-07-17', realized_r: -0.42, reviewed_at: null,
  });
  const open = tradeFixture({
    id: 4, book: 'US', symbol: 'META', status: 'open', stop: null, setup: null,
    exits: [], final_exit_date: null, realized_r: null, adherence: null,
    open_r: null, days_held: 4, last_close: 762.2, chased: null,
  });
  return {
    as_of: '2026-08-12', week_from: '2026-08-03', week_to: '2026-08-07',
    week_label: 'Week of 3–7 August 2026',
    weekTrades: [week, idxWeek],
    stragglers: [straggler],
    openTrades: [open],
    counts: [
      { book: 'US', closed: 1, chased: { k: 1, of: 1, na: 0 },
        partial_in_band: { k: 1, of: 1, na: 0 }, trail_on_signal: { k: 1, of: 1 },
        stop_recorded: { k: 1, of: 1 }, net_r: { sum: 1.42, excluded: 0 } },
      { book: 'IDX', closed: 1, chased: { k: 0, of: 0, na: 1 },
        partial_in_band: { k: 1, of: 1, na: 0 }, trail_on_signal: { k: 1, of: 1 },
        stop_recorded: { k: 1, of: 1 }, net_r: { sum: 0.61, excluded: 0 } },
    ],
    banner: [
      { kind: 'stop', severity: 'bad', title: 'CRWD — closed with no stop',
        body: 'Freezes in 14 trading days. After that the R is permanently missing.' },
      { kind: 'insufficient_history', severity: 'info',
        title: 'ANTM — 3 fields null (insufficient_history)',
        body: 'ma_200, pct_off_52w_high, rs_63d. A fact about the instrument, not a fault. No action.' },
    ],
    selected: '1',
    ...over,
  };
}

// ── Acceptance: one Trade on a day-by-day timeline with entry, Exits, MFE/MAE
//    and the nominal variant's exit ──
test('renders a day-by-day timeline with entry, exits, MFE/MAE and nominal-variant markers', () => {
  const html = renderReview(stateFixture());
  assert.match(html, /class="timeline"/);
  assert.match(html, /data-m="E"/); // entry
  assert.match(html, /data-m="X1"/); // first exit
  assert.match(html, /data-m="X2"/); // second exit
  assert.match(html, /mark sim/); // the nominal variant's own leg
  assert.match(html, /data-m="H"/); // MFE (trade-level high)
});

// ── Acceptance: each exit leg graded on its own excursion; Trade MFE distinct ──
test('grades each exit leg on its own excursion, with Trade-level MFE a distinct question', () => {
  const html = renderReview(stateFixture());
  assert.match(html, /Exit legs — each graded on its own excursion/);
  assert.match(html, /MFE to date/);
  assert.match(html, /Left on table/);
  // The per-leg MFE-to-date dates differ from the Trade-level MFE date.
  assert.match(html, /1\.99 ADR/); // leg 1 MFE-to-date
  assert.match(html, /answers a different question/); // Trade-level MFE set apart
});

// ── Acceptance: counts with denominators, never rates, split by book ──
test('shows counts with denominators, never rates, split by book', () => {
  const html = renderReview(stateFixture());
  assert.match(html, /1 of 1/); // a count with its denominator
  assert.match(html, /\(1 n\/a\)/); // n/a renders distinctly from a miss
  // No percentage rate anywhere in the counts strip itself.
  const stripStart = html.indexOf('the week in counts');
  const strip = html.slice(stripStart, html.indexOf('</section>', stripStart));
  assert.doesNotMatch(strip, /\d%/); // never a rate
  // Two book rows in the strip, and no number combines them.
  assert.match(html, /<td><b>US<\/b><\/td>/);
  assert.match(html, /<td><b>IDX<\/b><\/td>/);
  assert.match(html, /Counts, never rates/);
});

// ── Acceptance: no grouping axis anywhere ──
test('has no grouping axis anywhere on the surface', () => {
  const html = renderReview(stateFixture());
  assert.doesNotMatch(html, /by setup/i);
  assert.doesNotMatch(html, /by stack_state/i);
  assert.doesNotMatch(html, /by regime/i);
  assert.doesNotMatch(html, /by exit reason/i);
  assert.doesNotMatch(html, /Groupings/);
});

// ── Acceptance: scope is the week + stragglers + open, open outside the counts ──
test('scopes to the week, unreviewed stragglers and open Trades, open outside the counts', () => {
  const html = renderReview(stateFixture());
  assert.match(html, /Closed this week/);
  assert.match(html, /Unreviewed from earlier/);
  assert.match(html, /Open — not in the counts/);
  // The open Trade (META) is in the list but not in either book's Closed count.
  assert.match(html, /META/);
  const usRow = html.match(/<td><b>US<\/b><\/td>\s*<td class="num">(\d+)<\/td>/);
  assert.equal(usRow?.[1], '1'); // one US Trade closed this week — the open one excluded
});

// ── Acceptance: the action doors all render as write-through forms ──
test('renders write-through action forms for the closed-Trade workbench', () => {
  const html = renderReview(stateFixture({ selected: '1' }));
  assert.match(html, /action="\/action\/note"/);
  assert.match(html, /action="\/action\/review"/);
  assert.match(html, /action="\/action\/exit-reason"/);
  assert.match(html, /Reviewed →/);
  assert.match(html, /override/); // exit-reason override on each leg
  assert.match(html, /action="\/action\/equity-idx"/); // add IDX equity writes through
});

// ── Acceptance: add stop is unavailable on a frozen Trade ──
test('add stop is unavailable on a frozen Trade', () => {
  const frozen = tradeFixture({ id: 9, symbol: 'CRWD', stop: null, frozen: 1, chased: null,
    stop_provenance: null, realized_r: null, adherence: null });
  const html = renderReview(stateFixture({ weekTrades: [frozen], stragglers: [], openTrades: [], selected: '9' }));
  assert.match(html, /Stop locked \(frozen\)/);
  // No stop-price input is offered on the frozen Trade.
  assert.doesNotMatch(html, /name="price"/);
});

// ── Acceptance: banner states the fuse per stop-less item and insufficient_history as no-action ──
test('the banner states the remaining fuse and renders insufficient_history as no-action', () => {
  const html = renderReview(stateFixture());
  assert.match(html, /Needs attention/);
  assert.match(html, /Freezes in 14 trading days/);
  assert.match(html, /insufficient_history/);
  assert.match(html, /No action/);
  assert.match(html, /stated facts, not alarms/);
});

// ── readReview: scope, week filtering, and derived R over a real store ──
test('readReview scopes the week and derives R from stored primitives', withDb((dbPath) => {
  // A run fixes the as-of date; the week derives to 3–7 Aug 2026.
  journal(dbPath, ['run', '--as-of', '2026-08-12']);

  // Seed two closed Trades and one open one directly (writable handle), plus the
  // bars, equity and excursion the derivations read.
  const db = new DatabaseSync(dbPath);
  db.exec(`
    INSERT INTO trade (id, book, symbol, entry_date, entry_qty, entry_avg_price, status, stop, stop_provenance, frozen)
    VALUES (1,'US','AVGO','2026-07-31',60,318.4,'closed',302,'recorded',0),
           (2,'US','SMCI','2026-07-10',300,58.2,'closed',55.4,'recorded',0),
           (3,'US','META','2026-08-06',22,742.1,'open',715,'recorded',0);
    INSERT INTO trade_exit (id, trade_id, source, source_ref, exit_date, quantity, price, reason)
    VALUES (11,1,'ibkr','s1','2026-08-07',60,341.9,'close_below_ma10'),
           (21,2,'ibkr','s2','2026-07-17',300,57.0,'discretionary');
    INSERT INTO trade_exit_geometry (trade_id, book, symbol, exit_date, bar_date, exit_avg_price)
    VALUES (1,'US','AVGO','2026-08-07','2026-08-07',341.9),
           (2,'US','SMCI','2026-07-17','2026-07-17',57.0);
    INSERT INTO trade_enrichment (trade_id, book, symbol, entry_date, bar_date, adr_pct, insufficient_history)
    VALUES (1,'US','AVGO','2026-07-31','2026-07-30',3.1,''),
           (2,'US','SMCI','2026-07-10','2026-07-09',4.6,'ma_200,pct_off_52w_high');
    INSERT INTO trade_excursion (trade_id, start_date, end_date, mfe_high, mfe_date, mae_low, mae_date)
    VALUES (1,'2026-07-31','2026-08-07',349.1,'2026-08-05',311.2,'2026-08-03');
    INSERT INTO equity_snapshot (book, date, equity, provenance, source)
    VALUES ('US','2026-07-30',2000000,'stated','ibkr');
    INSERT INTO bar (book, symbol, date, open, high, low, close, volume)
    VALUES ('US','META','2026-08-06',740,745,738,742,1000),
           ('US','META','2026-08-07',742,760,741,762.2,1000);
  `);
  db.close();

  const state = readReview(dbPath, { asOf: '2026-08-12' });
  assert.equal(state.week_from, '2026-08-03');
  assert.equal(state.week_to, '2026-08-07');

  // AVGO closed in-week; SMCI closed earlier and unreviewed → straggler; META open.
  assert.deepEqual(state.weekTrades.map((t) => t.symbol), ['AVGO']);
  assert.deepEqual(state.stragglers.map((t) => t.symbol), ['SMCI']);
  assert.deepEqual(state.openTrades.map((t) => t.symbol), ['META']);

  // Realized R = (341.9 − 318.4) / (318.4 − 302).
  const avgo = state.weekTrades[0]!;
  assert.ok(avgo.realized_r !== null && Math.abs(avgo.realized_r - (341.9 - 318.4) / (318.4 - 302)) < 1e-9);
  // Exposure % = 318.4 * 60 / 2_000_000 * 100.
  assert.ok(avgo.exposure_pct !== null && Math.abs(avgo.exposure_pct - (318.4 * 60) / 2_000_000 * 100) < 1e-9);

  // The US strip: one Trade closed this week, and the straggler stays out of it.
  const us = state.counts.find((c) => c.book === 'US')!;
  assert.equal(us.closed, 1);
  assert.equal(us.stop_recorded.k, 1);

  // The open META, held two bars, computes an open R and days-held.
  const meta = state.openTrades[0]!;
  assert.equal(meta.days_held, 2);
  assert.ok(meta.open_r !== null && Math.abs(meta.open_r - (762.2 - 742.1) / (742.1 - 715)) < 1e-9);

  // SMCI's two null enrichment fields surface as a no-action banner item.
  assert.ok(state.banner.some((b) => b.kind === 'insufficient_history' && b.title.includes('SMCI')));
}));

// ── the write-through door: a POST shells the CLI and the store changes ──
test('an action POST writes straight through the CLI door', withDb(async (dbPath) => {
  journal(dbPath, ['run', '--as-of', '2026-08-12']);
  // Seed a confirmed open Trade through the real job.
  const py =
    'from journal import db, fills, flex; ' +
    "c=db.connect('" + dbPath + "'); " +
    "fills.insert_fills(c,[flex.Fill(source='ibkr',source_ref='b1',revision=1,book='US'," +
    "symbol='AAA',side='BUY',quantity=100.0,price=10.0,commission=0.0," +
    "executed_at='2026-08-03T09:30:00-04:00',order_id='o1')]); c.close()";
  execFileSync('python3', ['-c', py], { cwd: JOB_DIR, env: { ...process.env, PYTHONPATH: JOB_DIR }, stdio: 'pipe' });
  journal(dbPath, ['confirm']);
  const tradeId = readReview(dbPath, { asOf: '2026-08-12' }).openTrades[0]!.id;

  const ui = await startServer(dbPath, 0);
  try {
    // Add a stop through the surface's write-through door.
    const res = await fetch(`http://127.0.0.1:${ui.port}/action/stop`, {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ trade_id: String(tradeId), price: '9' }).toString(),
      redirect: 'manual',
    });
    assert.equal(res.status, 303); // redirect back to the surface

    const after = readReview(dbPath, { asOf: '2026-08-12' }).openTrades[0]!;
    assert.equal(after.stop, 9);
    assert.equal(after.stop_provenance, 'recorded');

    // Mark it reviewed straight through, too.
    await fetch(`http://127.0.0.1:${ui.port}/action/review`, {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ trade_id: String(tradeId) }).toString(),
      redirect: 'manual',
    });
    const db = new DatabaseSync(dbPath, { readOnly: true });
    const reviewed = db.prepare('SELECT reviewed_at FROM trade WHERE id=?').get(tradeId) as { reviewed_at: string | null };
    db.close();
    assert.ok(reviewed.reviewed_at, 'reviewed_at was written through the CLI door');
  } finally {
    await ui.close();
  }
}));
