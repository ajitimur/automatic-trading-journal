// The weekly review surface — variant D, "the exit workbench" (SPEC §11, #40).
//
// A per-Trade *reading* instrument, not a dashboard. Everything here is
// presentation over primitives the job already stored — this module reads the
// raw SQLite tables read-only and ports only the light read-side derivations
// (SPEC §6.4): R, ADR and % forms, best-fit, deviation cost, Risk/Exposure %,
// and the remaining freeze fuse. No new enrichment field, no new formula.
//
// Split by book is load-bearing (SPEC §11.2): the counts strip never combines
// the two books, though the list below it may mix freely — a list is not an
// aggregate.

import { DatabaseSync } from 'node:sqlite';

// ── Tunables ported verbatim from the job (single source is the job; these are
// the read-side twins, referenced in the SPEC sections cited). ──
const FREEZE_WINDOW = 20; // trading days after the final exit (post_exit.WINDOW)
const CHASE_ADR = 1.0; // stop_distance_adr > 1.0 is "chased" (SPEC §5.5 table)
const NOMINAL_VARIANT_DEFAULT = 'ma10/day3'; // ruleset_v1 nominal (SPEC §10.6)
const STALENESS_BOUND_DAYS: Record<string, number> = { US: 7, IDX: 45 }; // risk.py

export interface ExitLeg {
  id: number;
  exit_date: string;
  quantity: number;
  price: number;
  reason: string | null;
  day: number | null; // trading-day index from entry (entry = day 1)
  // Per-Exit excursion: entry → *this leg's* date (SPEC §7.5). The grading unit.
  mfe_high: number | null;
  mfe_date: string | null;
  mfe_adr: number | null; // MFE to date, in ADR units
  left_on_table_adr: number | null; // (mfe_high − leg price) in ADR — what the leg left
}

export interface Adherence {
  nominal_variant: string;
  best_fit: string | null;
  partial_state: string;
  partial_timing_delta: number | null;
  trail_exit_delta: number | null;
  nominal_status: string;
  exit_path: string;
  deviation_cost_r: number | null;
  // The nominal variant's own simulated legs, for the timeline markers.
  nominal_legs: Array<{ date: string; day: number | null; trigger: string; limit_locked: boolean }>;
  na_note: string | null;
}

export interface ReviewTrade {
  id: number;
  book: string;
  symbol: string;
  entry_date: string;
  entry_qty: number;
  entry_avg_price: number;
  status: string; // 'open' | 'closed'
  stop: number | null;
  setup: string | null;
  stop_provenance: string | null; // 'recorded' | 'reconstructed' | null
  frozen: number;
  stop_declined: number; // the trader was asked and chose the hole (ADR 0010)
  reviewed_at: string | null;
  note: string | null;

  adr_pct: number | null; // entry-side ADR normalizer

  exits: ExitLeg[];
  final_exit_date: string | null;

  // Trade-level excursion — a *distinct* question from the per-leg grading.
  mfe_high: number | null;
  mfe_date: string | null;
  mfe_adr: number | null;
  mfe_day: number | null;
  mae_low: number | null;
  mae_date: string | null;
  mae_adr: number | null;
  mae_day: number | null;

  realized_r: number | null;
  realized_pct: number | null; // fallback when no stop (no R denominator)
  risk_pct: number | null;
  exposure_pct: number | null;
  risk_provenance: string | null; // equity snapshot provenance
  fwd_return_20d: number | null; // post-exit 20d window, null until it completes

  chased: boolean | null; // stop_distance_adr > 1.0; null when not gradeable
  stop_distance_adr: number | null;

  adherence: Adherence | null;

  // Live-while-open (SPEC §7.6): only the four rolling fields.
  open_r: number | null;
  days_held: number | null;
  last_close: number | null;

  remaining_fuse: number | null; // trading days to freeze; only stop-less & unfrozen
  insufficient_history: string[]; // history-nulled field names (no-action)

  // Timeline: trading-day markers keyed by day number.
  timeline_days: number; // total cells to draw
  entry_day: number;
}

export interface BookCount {
  book: string;
  closed: number;
  chased: { k: number; of: number; na: number };
  partial_in_band: { k: number; of: number; na: number };
  trail_on_signal: { k: number; of: number };
  stop_recorded: { k: number; of: number };
  net_r: { sum: number | null; excluded: number };
}

export interface BannerItem {
  kind: 'stop' | 'insufficient_history' | 'run' | 'us_intake' | 'idx_intake' | 'idx_equity' | 'repair';
  severity: 'bad' | 'warn' | 'info';
  title: string;
  body: string;
}

export interface ReviewState {
  as_of: string | null;
  week_from: string | null;
  week_to: string | null;
  week_label: string;
  weekTrades: ReviewTrade[];
  stragglers: ReviewTrade[];
  openTrades: ReviewTrade[];
  counts: BookCount[]; // one per book, US then IDX
  banner: BannerItem[];
  selected: string | null; // selected trade id (as string), for the detail pane
}

// ── small derivation helpers ──
function tableExists(db: DatabaseSync, name: string): boolean {
  return (
    db
      .prepare(`SELECT name FROM sqlite_master WHERE type='table' AND name=?`)
      .get(name) !== undefined
  );
}

// The regime benchmark per book (job/journal/books.py BENCHMARKS). A Book has no
// calendar of its own — bars are keyed (book, symbol) — and its benchmark trades
// every day the book is open, so it stands in for one (SPEC §8.1).
const BENCHMARKS: Record<string, string> = { US: 'QQQ', IDX: '^JKSE' };

// " (7 trading days ago)", or "" when the calendar cannot be counted. Silent
// rather than approximate: an invented count is worse than a bare date (§11.4).
function elapsed(db: DatabaseSync, book: string, since: string, asOf: string | null): string {
  const symbol = BENCHMARKS[book];
  if (!symbol || !asOf || !tableExists(db, 'bar')) return '';
  const cached = db
    .prepare('SELECT COUNT(*) AS n FROM bar WHERE book=? AND symbol=?')
    .get(book, symbol) as { n: number };
  if (!cached.n) return '';
  const row = db
    .prepare('SELECT COUNT(*) AS n FROM bar WHERE book=? AND symbol=? AND date>? AND date<=?')
    .get(book, symbol, since, asOf) as { n: number };
  return ` (${row.n} trading day${row.n === 1 ? '' : 's'} ago)`;
}

function inAdr(px: number | null, entry: number, adr_pct: number | null): number | null {
  if (px === null || adr_pct === null || adr_pct === 0) return null;
  return ((px - entry) / entry) * 100 / adr_pct;
}

function inR(px: number | null, entry: number, stop: number | null): number | null {
  if (px === null || stop === null) return null;
  const denom = entry - stop;
  if (denom === 0) return null;
  return (px - entry) / denom;
}

// The most recent Friday on or before `iso`, then that Friday's Monday. Weekly
// cadence (SPEC §11.3): the strict review week is the last completed Mon–Fri.
function reviewWeek(iso: string): { from: string; to: string } {
  const d = new Date(iso + 'T00:00:00Z');
  // getUTCDay: 0=Sun..6=Sat. Step back to Friday (5).
  const back = (d.getUTCDay() - 5 + 7) % 7;
  const fri = new Date(d.getTime() - back * 86400000);
  const mon = new Date(fri.getTime() - 4 * 86400000);
  return { from: mon.toISOString().slice(0, 10), to: fri.toISOString().slice(0, 10) };
}

// The Friday of the Mon–Fri week containing `iso` — used to clamp the review
// week forward to the one the record starts in.
function fridayOnOrAfter(iso: string): string {
  const d = new Date(iso + 'T00:00:00Z');
  const ahead = (5 - d.getUTCDay() + 7) % 7;
  return new Date(d.getTime() + ahead * 86400000).toISOString().slice(0, 10);
}

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December'];

function weekLabel(from: string, to: string): string {
  const f = from.slice(8), t = to.slice(8), m = MONTHS[Number(to.slice(5, 7)) - 1];
  return `Week of ${Number(f)}–${Number(t)} ${m} ${to.slice(0, 4)}`;
}

function calendarDays(a: string, b: string): number {
  return Math.round(
    (Date.parse(b + 'T00:00:00Z') - Date.parse(a + 'T00:00:00Z')) / 86400000,
  );
}

// One row's realized R, off the recorded stop (SPEC §10.6, counterfactual.realized_r).
function realizedR(entry: number, exit_avg: number | null, stop: number | null): number | null {
  if (stop === null || exit_avg === null) return null;
  const denom = entry - stop;
  if (denom === 0) return null;
  return (exit_avg - entry) / denom;
}

interface Bars {
  dates: string[]; // sorted ascending, trading days for this (book, symbol)
  closeByDate: Map<string, number>;
}

function loadBars(db: DatabaseSync, book: string, symbol: string, from: string): Bars {
  const rows = db
    .prepare('SELECT date, close FROM bar WHERE book=? AND symbol=? AND date>=? ORDER BY date')
    .all(book, symbol, from) as Array<{ date: string; close: number }>;
  const dates = rows.map((r) => r.date);
  const closeByDate = new Map(rows.map((r) => [r.date, r.close] as const));
  return { dates, closeByDate };
}

// Trading-day index of `date` relative to entry (entry = day 1). Counts bars in
// [entry, date]; null if the date precedes entry or there are no bars.
function dayNumber(bars: Bars, entry_date: string, date: string): number | null {
  if (date < entry_date) return null;
  let n = 0;
  for (const d of bars.dates) {
    if (d < entry_date) continue;
    if (d > date) break;
    n++;
  }
  return n > 0 ? n : null;
}

// deviation_cost_r ported from TradeCounterfactual (SPEC §10.7). On a bare DB
// read the ex-date dividend set is empty, so the ex-date-mismatch null never
// fires — this matches CounterfactualStore.get(...).deviation_cost_r() exactly.
function deviationCostR(
  entry_qty: number,
  entry_avg_price: number,
  stop: number | null,
  nominalLegs: Array<{ price: number | null; fraction: number; limit_locked: boolean }>,
  nominalStatus: string,
  actualExits: Array<{ quantity: number; price: number }>,
): number | null {
  if (stop === null || nominalStatus === 'capped' || !nominalLegs.length) return null;
  if (!actualExits.length || !entry_qty) return null;
  const actualPps = actualExits.reduce((a, e) => a + e.quantity * e.price, 0) / entry_qty;
  const nominalPps = nominalLegs.reduce(
    (a, l) => (l.price === null ? a : a + l.fraction * l.price),
    0,
  );
  const cost = (nominalPps - actualPps) * entry_qty;
  const denom = entry_avg_price - stop;
  if (denom === 0) return null;
  if (nominalLegs.some((l) => l.limit_locked)) return null;
  return cost / denom / entry_qty;
}

function readOne(db: DatabaseSync, row: Record<string, unknown>, hasCf: boolean, hasEnrich: boolean,
  hasExitGeom: boolean, hasTradeExc: boolean, hasExitExc: boolean, hasPostExit: boolean): ReviewTrade {
  const id = row.id as number;
  const book = row.book as string;
  const symbol = row.symbol as string;
  const entry_date = row.entry_date as string;
  const entry_qty = row.entry_avg_price !== undefined ? (row.entry_qty as number) : 0;
  const entry_avg_price = (row.entry_avg_price as number) ?? 0;
  const stop = (row.stop as number | null) ?? null;

  const bars = loadBars(db, book, symbol, entry_date);

  // Exit allocations.
  const exitRows = db
    .prepare('SELECT id, exit_date, quantity, price, reason FROM trade_exit WHERE trade_id=? ORDER BY exit_date, id')
    .all(id) as Array<{ id: number; exit_date: string; quantity: number; price: number; reason: string | null }>;
  const final_exit_date = exitRows.length ? exitRows[exitRows.length - 1]!.exit_date : null;

  // Entry-side ADR normalizer (SPEC §7.1).
  const adr_pct = hasEnrich
    ? ((db.prepare('SELECT adr_pct FROM trade_enrichment WHERE trade_id=?').get(id) as
        { adr_pct: number | null } | undefined)?.adr_pct ?? null)
    : null;

  const insuffRow = hasEnrich
    ? (db.prepare('SELECT insufficient_history FROM trade_enrichment WHERE trade_id=?').get(id) as
        { insufficient_history: string } | undefined)
    : undefined;
  const insufficient_history = insuffRow && insuffRow.insufficient_history
    ? insuffRow.insufficient_history.split(',').filter(Boolean)
    : [];

  // Per-Exit excursion → each leg graded on its own window (SPEC §7.5, §11.1).
  const exits: ExitLeg[] = exitRows.map((e) => {
    const exc = hasExitExc
      ? (db.prepare('SELECT mfe_high, mfe_date FROM exit_excursion WHERE exit_id=?').get(e.id) as
          { mfe_high: number | null; mfe_date: string | null } | undefined)
      : undefined;
    const mfe_high = exc?.mfe_high ?? null;
    const mfe_adr = inAdr(mfe_high, entry_avg_price, adr_pct);
    const left = mfe_high !== null && adr_pct !== null && adr_pct !== 0
      ? ((mfe_high - e.price) / entry_avg_price) * 100 / adr_pct
      : null;
    return {
      id: e.id,
      exit_date: e.exit_date,
      quantity: e.quantity,
      price: e.price,
      reason: e.reason,
      day: dayNumber(bars, entry_date, e.exit_date),
      mfe_high,
      mfe_date: exc?.mfe_date ?? null,
      mfe_adr,
      left_on_table_adr: left,
    };
  });

  // Trade-level excursion (SPEC §7.5) — the distinct question.
  const tExc = hasTradeExc
    ? (db.prepare('SELECT mfe_high, mfe_date, mae_low, mae_date FROM trade_excursion WHERE trade_id=?').get(id) as
        { mfe_high: number | null; mfe_date: string | null; mae_low: number | null; mae_date: string | null } | undefined)
    : undefined;
  const mfe_high = tExc?.mfe_high ?? null;
  const mae_low = tExc?.mae_low ?? null;

  // Exit-side average price for Realized R (SPEC §10.6).
  const exitAvg = hasExitGeom
    ? ((db.prepare('SELECT exit_avg_price FROM trade_exit_geometry WHERE trade_id=?').get(id) as
        { exit_avg_price: number | null } | undefined)?.exit_avg_price ?? null)
    : null;
  const exitAvgFallback = exitAvg ?? (entry_qty && exitRows.length
    ? exitRows.reduce((a, e) => a + e.quantity * e.price, 0) / entry_qty
    : null);
  const realized_r = realizedR(entry_avg_price, exitAvgFallback, stop);
  const realized_pct = exitAvgFallback !== null
    ? (exitAvgFallback / entry_avg_price - 1) * 100
    : null;

  // Risk % / Exposure % off the equity snapshot at-or-before entry (SPEC §9).
  let risk_pct: number | null = null;
  let exposure_pct: number | null = null;
  let risk_provenance: string | null = null;
  const snap = db
    .prepare('SELECT date, equity, provenance FROM equity_snapshot WHERE book=? AND date<=? ORDER BY date DESC LIMIT 1')
    .get(book, entry_date) as { date: string; equity: number; provenance: string } | undefined;
  if (snap && snap.equity) {
    const stale = calendarDays(snap.date, entry_date) > (STALENESS_BOUND_DAYS[book] ?? 45);
    if (!stale) {
      risk_provenance = snap.provenance;
      exposure_pct = (entry_avg_price * entry_qty) / snap.equity * 100;
      risk_pct = stop === null ? null : (entry_avg_price - stop) * entry_qty / snap.equity * 100;
    }
  }

  // chased = stop_distance_adr > 1.0 (SPEC §5.5); only gradeable on a *recorded* stop.
  let stop_distance_adr: number | null = null;
  let chased: boolean | null = null;
  if (stop !== null && adr_pct !== null && adr_pct !== 0) {
    stop_distance_adr = ((entry_avg_price - stop) / entry_avg_price) * 100 / adr_pct;
    if (row.stop_provenance === 'recorded') chased = stop_distance_adr > CHASE_ADR;
  }

  // Adherence / counterfactual (SPEC §10.7).
  let adherence: Adherence | null = null;
  if (hasCf) {
    const cf = db.prepare('SELECT * FROM trade_counterfactual WHERE trade_id=?').get(id) as
      Record<string, unknown> | undefined;
    if (cf) {
      const nominal_variant = (cf.nominal_variant as string) || NOMINAL_VARIANT_DEFAULT;
      let best_fit: string | null = null;
      try {
        const fv = JSON.parse(cf.fit_vector as string) as Record<string, number>;
        const keys = Object.keys(fv);
        if (keys.length) {
          best_fit = keys.reduce((best, v) => {
            if (fv[v]! < fv[best]!) return v;
            if (fv[v] === fv[best]) return best === nominal_variant ? best : (v === nominal_variant ? v : best);
            return best;
          });
        }
      } catch { /* leave best_fit null on a malformed vector */ }

      const variantRow = db
        .prepare('SELECT legs, status FROM counterfactual_variant WHERE trade_id=? AND variant=?')
        .get(id, nominal_variant) as { legs: string; status: string } | undefined;
      let nominal_legs: Adherence['nominal_legs'] = [];
      let devCost: number | null = null;
      if (variantRow) {
        const legs = JSON.parse(variantRow.legs) as Array<{ date: string; price: number | null; fraction: number; limit_locked: boolean; trigger: string }>;
        nominal_legs = legs
          .filter((l) => l.trigger !== 'cap')
          .map((l) => ({ date: l.date, day: dayNumber(bars, entry_date, l.date), trigger: l.trigger, limit_locked: !!l.limit_locked }));
        devCost = deviationCostR(
          entry_qty, entry_avg_price, stop, legs, cf.nominal_status as string,
          exitRows.map((e) => ({ quantity: e.quantity, price: e.price })),
        );
      }
      const partial_state = cf.partial_state as string;
      const na_note = partial_state === 'not_applicable' && final_exit_date &&
        (dayNumber(bars, entry_date, final_exit_date) ?? 0) < 3
        ? 'stopped out before the day 3–5 band — partial never became applicable'
        : null;
      adherence = {
        nominal_variant,
        best_fit,
        partial_state,
        partial_timing_delta: (cf.partial_timing_delta as number | null) ?? null,
        trail_exit_delta: (cf.trail_exit_delta as number | null) ?? null,
        nominal_status: cf.nominal_status as string,
        exit_path: cf.exit_path as string,
        deviation_cost_r: devCost,
        nominal_legs,
        na_note,
      };
    }
  }

  // Forward 20d (post-exit window), null until it completes.
  const fwd_return_20d = hasPostExit
    ? ((db.prepare('SELECT fwd_return_20d FROM trade_post_exit WHERE trade_id=? ORDER BY revision DESC LIMIT 1').get(id) as
        { fwd_return_20d: number | null } | undefined)?.fwd_return_20d ?? null)
    : null;

  // Live-while-open (SPEC §7.6): four rolling fields only.
  let open_r: number | null = null;
  let days_held: number | null = null;
  let last_close: number | null = null;
  if (row.status === 'open' && bars.dates.length) {
    const lastDate = bars.dates[bars.dates.length - 1]!;
    last_close = bars.closeByDate.get(lastDate) ?? null;
    days_held = dayNumber(bars, entry_date, lastDate);
    open_r = inR(last_close, entry_avg_price, stop);
  }

  // Remaining freeze fuse — only meaningful for a stop-less, closed, unfrozen Trade.
  let remaining_fuse: number | null = null;
  if (row.status === 'closed' && !row.frozen && stop === null && final_exit_date) {
    const after = bars.dates.filter((d) => d > final_exit_date).length;
    remaining_fuse = Math.max(0, FREEZE_WINDOW - after);
  }

  // Timeline span: to the furthest marker, min 6 days, +2 breathing room.
  const entry_day = 1;
  const markerDays = [
    ...exits.map((e) => e.day),
    ...(adherence?.nominal_legs.map((l) => l.day) ?? []),
    tExc?.mfe_date ? dayNumber(bars, entry_date, tExc.mfe_date) : null,
    tExc?.mae_date ? dayNumber(bars, entry_date, tExc.mae_date) : null,
    days_held,
  ].filter((d): d is number => d !== null);
  const timeline_days = Math.max(6, ...(markerDays.length ? markerDays : [6])) + 2;

  return {
    id, book, symbol, entry_date, entry_qty, entry_avg_price,
    status: row.status as string,
    stop, setup: (row.setup as string | null) ?? null,
    stop_provenance: (row.stop_provenance as string | null) ?? null,
    frozen: (row.frozen as number) ?? 0,
    stop_declined: (row.stop_declined as number) ?? 0,
    reviewed_at: (row.reviewed_at as string | null) ?? null,
    note: (row.note as string | null) ?? null,
    adr_pct,
    exits, final_exit_date,
    mfe_high, mfe_date: tExc?.mfe_date ?? null, mfe_adr: inAdr(mfe_high, entry_avg_price, adr_pct),
    mfe_day: tExc?.mfe_date ? dayNumber(bars, entry_date, tExc.mfe_date) : null,
    mae_low, mae_date: tExc?.mae_date ?? null, mae_adr: inAdr(mae_low, entry_avg_price, adr_pct),
    mae_day: tExc?.mae_date ? dayNumber(bars, entry_date, tExc.mae_date) : null,
    realized_r, realized_pct, risk_pct, exposure_pct, risk_provenance, fwd_return_20d,
    chased, stop_distance_adr,
    adherence,
    open_r, days_held, last_close,
    remaining_fuse, insufficient_history,
    timeline_days, entry_day,
  };
}

// Scope Start per book, from the store (ADR 0008). Absent table or absent row
// means no boundary — a journal that was never restarted counts everything.
function readScopeStarts(db: DatabaseSync): Record<string, string> {
  if (!tableExists(db, 'book_scope')) return {};
  const rows = db.prepare('SELECT book, scope_start FROM book_scope').all() as
    Array<{ book: string; scope_start: string }>;
  return Object.fromEntries(rows.map((r) => [r.book, r.scope_start]));
}

function countFor(book: string, ts: ReviewTrade[]): BookCount {
  const gradeable = ts.filter((t) => t.stop_provenance === 'recorded' && t.chased !== null);
  const band = ts.filter((t) => t.adherence && t.adherence.partial_state !== 'not_applicable');
  const trail = ts.filter((t) => t.adherence && t.adherence.trail_exit_delta !== null);
  const usableR = ts.filter((t) => t.realized_r !== null);
  return {
    book,
    closed: ts.length,
    chased: { k: gradeable.filter((t) => t.chased).length, of: gradeable.length, na: ts.length - gradeable.length },
    // 'in_band' carries a *null* timing delta (only early/late carry a number),
    // so the in-band count keys off the state, not the delta (SPEC §10.7).
    partial_in_band: {
      k: band.filter((t) => t.adherence!.partial_state === 'in_band').length,
      of: band.length,
      na: ts.length - band.length,
    },
    trail_on_signal: { k: trail.filter((t) => t.adherence!.trail_exit_delta === 0).length, of: trail.length },
    stop_recorded: { k: ts.filter((t) => t.stop !== null).length, of: ts.length },
    net_r: {
      sum: usableR.length ? usableR.reduce((a, t) => a + (t.realized_r ?? 0), 0) : null,
      excluded: ts.length - usableR.length,
    },
  };
}

function buildBanner(db: DatabaseSync, closed: ReviewTrade[], open: ReviewTrade[], asOf: string | null): BannerItem[] {
  const items: BannerItem[] = [];
  const scopeStarts = readScopeStarts(db);
  const inScope = (t: ReviewTrade) => t.entry_date >= (scopeStarts[t.book] ?? '0000-01-01');

  // No stop before freeze — the fuse per item (SPEC §11.4, §3.6).
  //
  // Two exclusions, both about not crying wolf. A **declined** stop is an
  // answered question (ADR 0010) — re-raising it teaches the reader to skim the
  // banner, which is how the one item that matters gets missed. A
  // **pre-boundary** Trade is outside the record entirely (ADR 0008); its fuse
  // is not a call to action, and ~90 of them drown everything else.
  const stopWorthRaising = (t: ReviewTrade) =>
    t.stop === null && !t.frozen && !t.stop_declined && inScope(t);
  for (const t of closed.filter(stopWorthRaising)) {
    const fuse = t.remaining_fuse ?? 0;
    items.push({
      kind: 'stop', severity: 'bad',
      title: `${t.symbol} — closed with no stop`,
      body: `Freezes in ${fuse} trading day${fuse === 1 ? '' : 's'}. After that the R is permanently missing.`,
    });
  }
  for (const t of open.filter(stopWorthRaising)) {
    items.push({
      kind: 'stop', severity: 'warn',
      title: `${t.symbol} — open, no stop`,
      body: `Entered ${t.days_held ?? 0} trading day${t.days_held === 1 ? '' : 's'} ago. Risk % and chase are unavailable until the stop lands.`,
    });
  }

  // insufficient_history — an explicit *no-action* item (SPEC §7.8, §11.4).
  for (const t of [...closed, ...open].filter((x) => x.insufficient_history.length)) {
    const n = t.insufficient_history.length;
    items.push({
      kind: 'insufficient_history', severity: 'info',
      title: `${t.symbol} — ${n} field${n === 1 ? '' : 's'} null (insufficient_history)`,
      body: `${t.insufficient_history.join(', ')}. A fact about the instrument, not a fault. No action.`,
    });
  }

  // Stated facts, never alarms (SPEC §11.4): last run, last IDX intake, last IDX equity.
  if (tableExists(db, 'run')) {
    const run = db.prepare('SELECT as_of_date, status, finished_at FROM run ORDER BY id DESC LIMIT 1').get() as
      { as_of_date: string; status: string; finished_at: string | null } | undefined;
    if (run) {
      items.push({
        kind: 'run', severity: 'info',
        title: 'Last run',
        body: `as-of ${run.as_of_date} — status ${run.status}.`,
      });
    }
  }
  // Intake means "when did a hand-dropped TC last land" (SPEC §13.2) — a fact
  // about the trader's own routine. It was read off `bar` here, which is Yahoo
  // market data and arrives whether or not anything was dropped, so the banner
  // reported a healthy intake on a book that had not seen a TC in months.
  if (tableExists(db, 'raw_document')) {
    // US intake runs unattended (SPEC §4.1), which is exactly why it needs a
    // stated fact: a thing that runs by itself is silent when it stops.
    const fetched = db
      .prepare("SELECT MAX(fetched_at) AS d FROM raw_document WHERE book='US' AND kind='flex-trades-xml'")
      .get() as { d: string | null };
    items.push({
      kind: 'us_intake', severity: 'info', title: 'US intake',
      body: fetched.d
        ? `last fetch ${fetched.d.slice(0, 10)}${elapsed(db, 'US', fetched.d.slice(0, 10), asOf)}.`
        : 'no fetch recorded.',
    });
    const drop = db
      .prepare("SELECT MAX(fetched_at) AS d FROM raw_document WHERE book='IDX' AND kind='stockbit-tc'")
      .get() as { d: string | null };
    const body = drop.d
      ? `last drop ${drop.d.slice(0, 10)}${elapsed(db, 'IDX', drop.d.slice(0, 10), asOf)}.`
      : 'no drop recorded.';
    items.push({ kind: 'idx_intake', severity: 'info', title: 'IDX intake', body });
  }
  if (tableExists(db, 'equity_snapshot')) {
    const eq = db.prepare("SELECT MAX(date) AS d FROM equity_snapshot WHERE book='IDX'").get() as { d: string | null };
    if (eq.d) items.push({ kind: 'idx_equity', severity: 'info', title: 'IDX equity', body: `last snapshot ${eq.d}${elapsed(db, 'IDX', eq.d, asOf)}.` });
  }
  // Enrichment held for repair — a span check that failed (SPEC §11.4).
  //
  // Scoped to the symbols actually on this surface. `bar_fetch` accumulates a
  // row per symbol ever fetched, so unscoped it raised a repair for every name
  // traded before the boundary — ~130 items about Trades the reader has
  // deliberately stopped looking at, which buries the handful that are real.
  const onSurface = new Set([...closed, ...open].map((t) => `${t.book}\u0000${t.symbol}`));
  if (tableExists(db, 'bar_fetch')) {
    const repairs = (db
      .prepare('SELECT book, symbol, span_detail FROM bar_fetch WHERE span_ok=0 GROUP BY book, symbol')
      .all() as Array<{ book: string; symbol: string; span_detail: string }>)
      .filter((r) => onSurface.has(`${r.book}\u0000${r.symbol}`));
    for (const r of repairs) {
      items.push({
        kind: 'repair', severity: 'bad',
        title: `${r.symbol} — span check failed`,
        body: `${r.span_detail}. Enrichment held; needs manual repair.`,
      });
    }
  }
  return items;
}

export function readReview(dbPath: string, opts: { asOf?: string; selected?: string } = {}): ReviewState {
  const db = new DatabaseSync(dbPath, { readOnly: true });
  try {
    if (!tableExists(db, 'trade')) {
      return {
        as_of: null, week_from: null, week_to: null, week_label: 'No review week',
        weekTrades: [], stragglers: [], openTrades: [], counts: [], banner: [], selected: null,
      };
    }
    const cols = db.prepare('PRAGMA table_info(trade)').all() as Array<{ name: string }>;
    const hasReview = cols.some((c) => c.name === 'entry_avg_price');
    const hasCf = tableExists(db, 'trade_counterfactual');
    const hasEnrich = tableExists(db, 'trade_enrichment');
    const hasExitGeom = tableExists(db, 'trade_exit_geometry');
    const hasTradeExc = tableExists(db, 'trade_excursion');
    const hasExitExc = tableExists(db, 'exit_excursion');
    const hasPostExit = tableExists(db, 'trade_post_exit');

    // as-of: caller override → latest run as-of → latest exit date → nothing.
    let asOf = opts.asOf ?? null;
    if (!asOf && tableExists(db, 'run')) {
      asOf = (db.prepare('SELECT as_of_date FROM run ORDER BY id DESC LIMIT 1').get() as { as_of_date: string } | undefined)?.as_of_date ?? null;
    }
    if (!asOf) {
      asOf = (db.prepare('SELECT MAX(exit_date) AS d FROM trade_exit').get() as { d: string | null } | undefined)?.d ?? null;
    }

    const allRows = hasReview
      ? (db.prepare('SELECT * FROM trade ORDER BY entry_date DESC, symbol').all() as Array<Record<string, unknown>>)
      : [];
    const all = allRows.map((r) =>
      readOne(db, r, hasCf, hasEnrich, hasExitGeom, hasTradeExc, hasExitExc, hasPostExit));

    // Scope Start governs the **whole surface**, not only the aggregates
    // (ADR 0008). The record begins on a date, and a reader opening the app
    // should meet that record — not 197 closed Trades from the stretch it was
    // deliberately restarted to leave behind.
    //
    // One exception, and it is about risk rather than tidiness: a Trade **still
    // open** stays visible however old it is. It can never count — inclusion is
    // judged on entry date, permanently — but it is live money, and the review
    // surface is where it gets managed. Hiding a position the trader actually
    // holds is the one way this boundary could do harm.
    const scopeStarts = readScopeStarts(db);
    const inScope = (t: ReviewTrade) =>
      t.entry_date >= (scopeStarts[t.book] ?? '0000-01-01');
    const onSurface = (t: ReviewTrade) => inScope(t) || t.status === 'open';
    const visible = all.filter(onSurface);

    // A week that closes before the record opens can only ever be empty, so the
    // strip would read "no Trades closed this week" forever rather than saying
    // the record simply has not reached a full week yet. Clamp forward to the
    // week the record starts in — partial, and labelled as such.
    const earliestStart = Object.values(scopeStarts).sort()[0] ?? null;
    let week = asOf ? reviewWeek(asOf) : null;
    let firstWeek = false;
    if (week !== null && earliestStart !== null && week.to < earliestStart) {
      week = reviewWeek(fridayOnOrAfter(earliestStart));
      firstWeek = true;
    }

    const closed = visible.filter((t) => t.status === 'closed');
    const openTrades = visible.filter((t) => t.status === 'open');
    const inWeek = (t: ReviewTrade) =>
      week !== null && t.final_exit_date !== null &&
      t.final_exit_date >= week.from && t.final_exit_date <= week.to;

    const weekTrades = closed.filter(inWeek);
    const stragglers = closed.filter((t) => !inWeek(t) && !t.reviewed_at);

    // Open Trades are never part of the week's counts (§11.3), so the exception
    // above cannot leak a pre-boundary Trade into a number.
    const counts = ['US', 'IDX'].map((b) =>
      countFor(b, weekTrades.filter((t) => t.book === b && inScope(t))));
    const banner = buildBanner(db, closed, openTrades, asOf);

    // Default selection: first week trade, else first straggler, else first open.
    const pool = [...weekTrades, ...stragglers, ...openTrades];
    let selected = opts.selected ?? null;
    if (selected === null || !pool.some((t) => String(t.id) === selected)) {
      selected = pool.length ? String(pool[0]!.id) : null;
    }

    return {
      as_of: asOf,
      week_from: week?.from ?? null,
      week_to: week?.to ?? null,
      week_label: week
        ? weekLabel(week.from, week.to) + (firstWeek ? ' · first week of the record' : '')
        : 'No review week',
      weekTrades, stragglers, openTrades, counts, banner, selected,
    };
  } finally {
    db.close();
  }
}
