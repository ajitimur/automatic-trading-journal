// Render the journal state as a localhost page (SPEC §13.6). The skeleton
// shows two things: "no Trades yet" and the latest run record. Kept as a pure
// function so it is testable without a live server.

import type { BookOutcome, Fill, JournalState, RunRecord, Trade } from './db.ts';

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// The human-readable "Dates advanced" cell, one per book status.
function describeOutcome(b: BookOutcome): string {
  if (b.status === 'advanced') {
    return `${b.days_advanced} day(s): ${escapeHtml(b.from_date ?? 'floor')} → ${escapeHtml(b.to_date)}`;
  }
  if (b.status === 'no-op') {
    return `no-op (at ${escapeHtml(b.to_date)})`;
  }
  return `error: ${escapeHtml(b.error ?? 'unknown')}`;
}

// The Fill ledger preview. Commission is shown per fill because the broker
// provides it there (SPEC §7.0) — not flattened to an order total.
function renderFills(fills: Fill[], fillCount: number): string {
  if (fillCount === 0) {
    return `<p class="muted">No Fills yet — drop a Flex file with <code>journal import &lt;file.xml&gt;</code>.</p>`;
  }
  const rows = fills
    .map((f) => {
      return `<tr><td>${escapeHtml(f.executed_at)}</td><td>${escapeHtml(f.book)}</td><td>${escapeHtml(f.symbol)}</td><td>${escapeHtml(f.side)}</td><td>${f.quantity}</td><td>${f.price}</td><td>${f.commission}</td></tr>`;
    })
    .join('');
  const capped =
    fillCount > fills.length
      ? `<p class="muted">Showing the ${fills.length} most recent of ${fillCount}.</p>`
      : '';
  return `
    <p>${fillCount} Fill(s).</p>
    <table>
      <thead><tr><th>Executed (ET)</th><th>Book</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Commission</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    ${capped}`;
}

// One Trade: the interpreted cohort up front, its Fills one disclosure away
// (SPEC §5.9). The <details> is the disclosure — collapsed until opened.
// The stop cell states its provenance and whether it is locked; a missing stop
// reads as "—", the hole the daily job nags about (SPEC §5.5), not a blank.
function renderStop(t: Trade): string {
  if (t.stop === null) {
    return t.frozen ? '<td>— <span class="muted">(no stop, frozen)</span></td>' : '<td>—</td>';
  }
  const prov = t.stop_provenance ? ` <span class="muted">(${escapeHtml(t.stop_provenance)})</span>` : '';
  return `<td>${t.stop}${prov}</td>`;
}

function renderTrade(t: Trade): string {
  const notional = t.entry_qty * t.entry_avg_price;
  const entryRows = t.entryFills
    .map(
      (f) =>
        `<tr><td>${escapeHtml(f.executed_at)}</td><td>BUY</td><td>${f.quantity}</td><td>${f.price}</td></tr>`,
    )
    .join('');
  const exitRows = t.exits
    .map(
      (x) =>
        `<tr><td>${escapeHtml(x.exit_date)}</td><td>SELL</td><td>${x.quantity}</td><td>${x.price}</td></tr>`,
    )
    .join('');
  const exitsSection = t.exits.length
    ? `<p class="muted">Exits allocated to this Trade:</p>
       <table><thead><tr><th>Date</th><th>Side</th><th>Qty</th><th>Price</th></tr></thead>
       <tbody>${exitRows}</tbody></table>`
    : '';
  return `
    <tr>
      <td>${escapeHtml(t.book)}</td>
      <td>${escapeHtml(t.symbol)}</td>
      <td>${escapeHtml(t.entry_date)}</td>
      <td>${t.entry_qty}</td>
      <td>${t.entry_avg_price.toFixed(4)}</td>
      <td>${notional.toFixed(2)}</td>
      ${renderStop(t)}
      <td>${t.setup ? escapeHtml(t.setup) : '—'}</td>
      <td>${escapeHtml(t.status)}</td>
    </tr>
    <tr class="disclosure"><td colspan="9">
      <details>
        <summary>Fills</summary>
        <table><thead><tr><th>Executed (ET)</th><th>Side</th><th>Qty</th><th>Price</th></tr></thead>
        <tbody>${entryRows}</tbody></table>
        ${exitsSection}
      </details>
    </td></tr>`;
}

function renderTrades(trades: Trade[]): string {
  if (trades.length === 0) {
    return `<p class="muted">No Trades yet.</p>`;
  }
  const rows = trades.map(renderTrade).join('');
  return `
    <p>${trades.length} confirmed Trade(s).</p>
    <table>
      <thead><tr><th>Book</th><th>Symbol</th><th>Entry date</th><th>Qty</th><th>Avg price</th><th>Notional</th><th>Stop</th><th>Setup</th><th>Status</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderRun(run: RunRecord | null): string {
  if (run === null) {
    return `<p class="muted">No run yet — start the job with <code>journal run</code>.</p>`;
  }
  const rows = run.books
    .map((b) => {
      return `<tr><td>${escapeHtml(b.book)}</td><td>${escapeHtml(b.status)}</td><td>${describeOutcome(b)}</td></tr>`;
    })
    .join('');
  return `
    <p>Run <strong>#${run.id}</strong> — as-of <strong>${escapeHtml(run.as_of_date)}</strong> —
       status <strong>${escapeHtml(run.status)}</strong></p>
    <p class="muted">started ${escapeHtml(run.started_at)}${run.finished_at ? ` · finished ${escapeHtml(run.finished_at)}` : ''}</p>
    <table>
      <thead><tr><th>Book</th><th>Status</th><th>Dates advanced</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

export function renderPage(state: JournalState): string {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Automatic Trading Journal</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 44rem; color: #1a1a1a; }
    h1 { font-size: 1.4rem; } h2 { font-size: 1.05rem; margin-top: 2rem; }
    .muted { color: #666; }
    table { border-collapse: collapse; margin-top: 0.5rem; }
    th, td { text-align: left; padding: 0.3rem 0.8rem; border-bottom: 1px solid #eee; }
    code { background: #f4f4f4; padding: 0 0.3rem; border-radius: 3px; }
    details summary { cursor: pointer; color: #555; }
    tr.disclosure td { padding-top: 0; border-bottom: 1px solid #ddd; }
  </style>
</head>
<body>
  <h1>Automatic Trading Journal</h1>
  <h2>Trades</h2>
  ${renderTrades(state.trades)}
  <h2>Fills</h2>
  ${renderFills(state.fills, state.fillCount)}
  <h2>Latest run</h2>
  ${renderRun(state.latestRun)}
</body>
</html>`;
}
