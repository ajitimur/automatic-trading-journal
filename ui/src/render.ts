// Render the journal state as a localhost page (SPEC §13.6). The skeleton
// shows two things: "no Trades yet" and the latest run record. Kept as a pure
// function so it is testable without a live server.

import type { BookOutcome, JournalState, RunRecord } from './db.ts';

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
  const trades =
    state.tradeCount === 0
      ? `<p class="muted">No Trades yet.</p>`
      : `<p>${state.tradeCount} Trade(s).</p>`;

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
  </style>
</head>
<body>
  <h1>Automatic Trading Journal</h1>
  <h2>Trades</h2>
  ${trades}
  <h2>Latest run</h2>
  ${renderRun(state.latestRun)}
</body>
</html>`;
}
