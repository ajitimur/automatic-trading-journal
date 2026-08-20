// Render the weekly review surface — variant D (SPEC §11, #40). A pure function
// over the ReviewState so it is testable without a live server. The visual
// language is lifted from docs/prototypes/weekly-review-surface.prototype.html
// (the settled design), rendered here over real primitives instead of fixtures.
//
// Actions "write straight through" (SPEC §11.3): each is a plain <form> POST to
// the server, which shells the single CLI door and redirects back. No client JS
// — a weekly review is a sit-down at localhost, and server-rendered forms keep
// the surface a reading instrument.

import type { BannerItem, BookCount, ExitLeg, ReviewState, ReviewTrade } from './review.ts';

function esc(v: string): string {
  return v
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

const REASON_LABEL: Record<string, string> = {
  partial_strength: 'scheduled partial',
  close_below_ma10: 'trail MA10',
  close_below_ma20: 'trail MA20',
  stop_hit: 'stop hit',
  written_off: 'written off',
  discretionary: 'discretionary',
};

const EXIT_REASONS = [
  'partial_strength', 'close_below_ma10', 'close_below_ma20', 'stop_hit', 'written_off', 'discretionary',
];
const SETUPS = ['base_breakout', 'high_tight_flag', 'other'];

function fmtR(r: number | null): string {
  return r === null ? '—' : (r > 0 ? '+' : '') + r.toFixed(2) + 'R';
}
function cls(n: number | null): string {
  return n === null ? 'muted' : n > 0 ? 'pos' : n < 0 ? 'neg' : '';
}
function signD(n: number | null): string {
  return n === null ? '—' : (n > 0 ? '+' : '') + n + 'd';
}
function px(book: string, v: number | null): string {
  if (v === null) return '—';
  return book === 'IDX' ? v.toLocaleString('en-US') : v.toFixed(2);
}
function day(iso: string | null): string {
  return iso ? iso.slice(5).replace('-', '/') : '—';
}
// Realized outcome label: R off the recorded stop, the % fallback when there is
// no stop (no R denominator), else '—'.
function realizedLabel(t: ReviewTrade): string {
  if (t.realized_r !== null) return fmtR(t.realized_r);
  if (t.realized_pct === null) return '—';
  return (t.realized_pct > 0 ? '+' : '') + t.realized_pct.toFixed(1) + '%';
}

// ── the day-by-day strip (SPEC §11.1) ──
function timeline(t: ReviewTrade): string {
  const n = t.timeline_days;
  const marks: Record<number, { c: string; m: string }> = {};
  marks[t.entry_day] = { c: 'entry', m: 'E' };
  const coincide: string[] = [];
  t.exits.forEach((e, i) => {
    if (e.day) marks[e.day] = { c: 'exit', m: 'X' + (t.exits.length > 1 ? i + 1 : '') };
  });
  // The nominal variant's own legs, drawn against the actual (SPEC §11.1). Where
  // a sim leg lands on a real exit the marker would be invisible, so it is
  // stated in words instead — the most interesting case (you did what the rule said).
  (t.adherence?.nominal_legs ?? []).forEach((s) => {
    if (s.day === null) return;
    if (marks[s.day] && marks[s.day]!.c === 'exit') coincide.push('d' + s.day);
    else if (!marks[s.day]) marks[s.day] = { c: 'sim', m: s.trigger === 'partial' ? 'p' : s.trigger === 'stop' ? 's' : 't' };
  });
  if (t.mfe_day && !marks[t.mfe_day]) marks[t.mfe_day] = { c: 'mfe', m: 'H' };
  if (t.mae_day && !marks[t.mae_day]) marks[t.mae_day] = { c: 'mae', m: 'L' };

  const heldTo = t.status === 'closed'
    ? (t.exits.length ? t.exits[t.exits.length - 1]!.day ?? 1 : 1)
    : (t.days_held ?? 1);
  const cells: string[] = [], axis: string[] = [];
  for (let d = 1; d <= n; d++) {
    const mk = marks[d];
    cells.push(`<div class="tl-cell ${d <= heldTo ? 'held' : ''} ${mk ? 'mark ' + mk.c : ''}"${mk ? ` data-m="${mk.m}"` : ''}></div>`);
    axis.push(`<div>${d}</div>`);
  }
  const cols = `grid-template-columns: repeat(${n}, minmax(0,1fr));`;
  return `
    <div class="timeline">
      <div class="tl-days" style="${cols}">${cells.join('')}</div>
      <div class="tl-axis" style="${cols}">${axis.join('')}</div>
    </div>
    <div class="legend">
      <span><i style="background:#16181d"></i>entry</span>
      <span><i style="background:var(--accent)"></i>your exit</span>
      <span><i style="background:var(--warn)"></i>nominal variant would have</span>
      <span><i style="background:var(--good)"></i>high (MFE)</span>
      <span><i style="background:var(--bad)"></i>low (MAE)</span>
    </div>
    ${coincide.length ? `<p class="muted" style="font-size:11.5px;margin:6px 0 0">The nominal variant exited on the same day you did at ${coincide.join(' and ')} — its marker sits underneath yours.</p>` : ''}`;
}

function actionForm(action: string, tradeId: number, inner: string, extra = ''): string {
  return `<form method="post" action="/action/${action}" style="display:inline">
    <input type="hidden" name="trade_id" value="${tradeId}">${extra}${inner}</form>`;
}

function closedDetail(t: ReviewTrade): string {
  const a = t.adherence;
  const realized = realizedLabel(t);
  const legRows = t.exits.map((e: ExitLeg) => `
    <tr>
      <td>d${e.day ?? '—'} ${esc(REASON_LABEL[e.reason ?? ''] ?? e.reason ?? 'exit')}
        <div class="muted" style="font-size:11px">${day(e.exit_date)}</div>
        ${e.reason ? overrideReason(t.id, e.id, e.reason) : ''}</td>
      <td class="num">${e.quantity.toLocaleString('en-US')}</td>
      <td class="num">${px(t.book, e.price)}</td>
      <td class="num">${e.mfe_adr === null ? '—' : e.mfe_adr.toFixed(2) + ' ADR'}<div class="muted" style="font-size:11px">${day(e.mfe_date)}</div></td>
      <td class="num ${e.left_on_table_adr !== null && e.left_on_table_adr > 1 ? 'neg' : ''}">${e.left_on_table_adr === null ? '—' : e.left_on_table_adr.toFixed(2) + ' ADR'}</td>
    </tr>`).join('');

  const chasedPill = t.chased ? `<span class="pill warn">chased ${t.stop_distance_adr?.toFixed(2)} ADR</span>` : '';
  const provPill = t.stop_provenance && t.stop_provenance !== 'recorded'
    ? `<span class="pill ${t.stop === null ? 'bad' : 'mute'}">stop ${esc(t.stop_provenance)}</span>` : '';

  return `<div class="card">
    <div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:2px">
      <h3 style="font-size:20px;margin:0">${esc(t.symbol)}</h3>
      <span class="pill mute">${esc(t.book)}</span>
      ${t.setup ? `<span class="pill mute">${esc(t.setup.replace(/_/g, ' '))}</span>` : ''}
      ${chasedPill}${provPill}
      ${t.reviewed_at ? '<span class="pill good">reviewed</span>' : ''}
      <span style="margin-left:auto;font-size:19px" class="${cls(t.realized_r)}">${realized}</span>
    </div>
    <p class="muted" style="font-size:12px;margin:0 0 14px">
      entry ${day(t.entry_date)} @ ${px(t.book, t.entry_avg_price)} · ${t.entry_qty.toLocaleString('en-US')} sh ·
      stop ${t.stop === null ? '—' : px(t.book, t.stop)} · risk ${t.risk_pct === null ? '—' : t.risk_pct.toFixed(2) + '%'} ·
      exposure ${t.exposure_pct === null ? '—' : t.exposure_pct.toFixed(1) + '%'}
    </p>

    <h2>The move, day by day</h2>
    ${timeline(t)}

    ${a?.na_note ? `<div class="qflag">${esc(a.na_note)}. <b>Absence is not deviation</b> — no cost is shown.</div>` : ''}
    ${t.stop === null ? `<div class="qflag">No stop recorded, so this Trade has no R, no chase grade and no deviation cost.${t.remaining_fuse !== null ? ` Freezes in ${t.remaining_fuse} trading days.` : ''}</div>` : ''}

    <div class="grid2" style="margin-top:18px">
      <div>
        <h2>Exit legs — each graded on its own excursion</h2>
        <table>
          <thead><tr><th>Leg</th><th class="num">Qty</th><th class="num">Price</th><th class="num">MFE to date</th><th class="num">Left on table</th></tr></thead>
          <tbody>${legRows}</tbody>
        </table>
        <p class="muted" style="font-size:11.5px;margin:8px 0 0">
          Per-Exit excursion runs entry → that leg's own date. Trade-level MFE
          ${t.mfe_adr === null ? '—' : t.mfe_adr.toFixed(2) + ' ADR'} on ${day(t.mfe_date)} answers a different question.
        </p>
      </div>
      <div>
        <h2>Against the nominal variant — ${esc(a?.nominal_variant ?? '—')}</h2>
        <dl class="kv">
          <dt>Best fit</dt><dd>${esc(a?.best_fit ?? '—')}</dd>
          <dt>Partial</dt><dd>${a ? esc(a.partial_state.replace(/_/g, ' ')) + (a.partial_timing_delta !== null ? ` · ${signD(a.partial_timing_delta)}` : '') : '—'}</dd>
          <dt>Trail</dt><dd>${a && a.trail_exit_delta !== null ? signD(a.trail_exit_delta) + ' vs signal' : 'not applicable'}</dd>
          <dt>Deviation cost</dt><dd class="${cls(a?.deviation_cost_r ?? null)}">${a && a.deviation_cost_r !== null ? fmtR(a.deviation_cost_r) : 'not applicable'}</dd>
        </dl>
        <h2 style="margin-top:16px">Trade-level excursion</h2>
        <dl class="kv">
          <dt>MFE</dt><dd>${t.mfe_adr === null ? '—' : t.mfe_adr.toFixed(2) + ' ADR'} on ${day(t.mfe_date)}</dd>
          <dt>MAE</dt><dd>${t.mae_adr === null ? '—' : t.mae_adr.toFixed(2) + ' ADR'} on ${day(t.mae_date)}</dd>
          <dt>20d forward</dt><dd class="muted">${t.fwd_return_20d === null ? 'window open' : t.fwd_return_20d.toFixed(1) + '%'}</dd>
        </dl>
      </div>
    </div>

    ${t.note ? `<p class="soft" style="font-size:13px;font-style:italic;margin:16px 0 0">“${esc(t.note)}”</p>` : ''}

    <div class="actions">
      ${actionForm('stop', t.id,
        t.stop !== null || t.frozen
          ? `<button type="submit" disabled>${t.frozen ? 'Stop locked (frozen)' : 'Stop set'}</button>`
          : `<input class="mini" type="number" step="any" name="price" placeholder="stop" required><button type="submit">Add stop</button>`)}
      ${actionForm('note', t.id, `<input class="mini" type="text" name="text" placeholder="note" value="${t.note ? esc(t.note) : ''}"><button type="submit">Edit note</button>`)}
      ${actionForm('review', t.id, `<button type="submit" class="${t.reviewed_at ? '' : 'active'}">${t.reviewed_at ? 'Reviewed ✓' : 'Reviewed →'}</button>`)}
    </div>
  </div>`;
}

function overrideReason(tradeId: number, exitId: number, current: string): string {
  const opts = EXIT_REASONS.map((r) =>
    `<option value="${r}"${r === current ? ' selected' : ''}>${esc(REASON_LABEL[r] ?? r)}</option>`).join('');
  return actionForm('exit-reason', tradeId,
    `<select name="reason" style="font-size:11px">${opts}</select><button type="submit" style="font-size:11px;padding:2px 6px">override</button>`,
    `<input type="hidden" name="exit_id" value="${exitId}">`);
}

function openDetail(t: ReviewTrade): string {
  return `<div class="card">
    <div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap">
      <h3 style="font-size:20px;margin:0">${esc(t.symbol)}</h3>
      <span class="pill mute">${esc(t.book)}</span>
      <span class="pill">open</span>
      ${t.stop === null ? '<span class="pill bad">no stop</span>' : ''}
      ${t.setup === null ? '<span class="pill bad">no setup</span>' : ''}
      <span style="margin-left:auto;font-size:19px" class="${cls(t.open_r)}">${t.open_r === null ? '—' : fmtR(t.open_r) + ' open'}</span>
    </div>
    <p class="muted" style="font-size:12px;margin:6px 0 14px">
      entry ${day(t.entry_date)} @ ${px(t.book, t.entry_avg_price)} · held ${t.days_held ?? 0}d · last ${px(t.book, t.last_close)}
    </p>
    <h2>The move so far</h2>
    ${timeline(t)}
    <div class="grid2" style="margin-top:16px">
      <dl class="kv">
        <dt>Days held</dt><dd>${t.days_held ?? 0}</dd>
        <dt>Open R</dt><dd>${fmtR(t.open_r)}</dd>
        <dt>Exposure</dt><dd>${t.exposure_pct === null ? '—' : t.exposure_pct.toFixed(1) + '%'}</dd>
      </dl>
      <dl class="kv">
        <dt>Stop</dt><dd>${t.stop === null ? '<span class="neg">not set</span>' : px(t.book, t.stop)}</dd>
        <dt>Setup</dt><dd>${t.setup === null ? '<span class="neg">not set</span>' : esc(t.setup.replace(/_/g, ' '))}</dd>
        <dt>Risk</dt><dd>${t.risk_pct === null ? '—' : t.risk_pct.toFixed(2) + '%'}</dd>
      </dl>
    </div>
    ${t.stop === null ? '<div class="qflag">Open R needs a stop — supply one and it starts computing immediately.</div>' : ''}
    <div class="actions">
      ${actionForm('stop', t.id, t.stop !== null
        ? `<button type="submit" disabled>Stop set</button>`
        : `<input class="mini" type="number" step="any" name="price" placeholder="stop" required><button type="submit">Add stop</button>`)}
      ${actionForm('setup', t.id, t.setup !== null
        ? `<button type="submit" disabled>Setup set</button>`
        : `<select name="value">${SETUPS.map((s) => `<option value="${s}">${esc(s.replace(/_/g, ' '))}</option>`).join('')}</select><button type="submit">Add setup</button>`)}
    </div>
  </div>`;
}

function countRow(c: BookCount): string {
  if (c.closed === 0) {
    return `<tr><td><b>${esc(c.book)}</b></td><td colspan="6" class="muted">no Trades closed this week</td></tr>`;
  }
  const na = (n: number) => (n ? ` <span class="muted">(${n} n/a)</span>` : '');
  return `<tr>
    <td><b>${esc(c.book)}</b></td>
    <td class="num">${c.closed}</td>
    <td class="num">${c.chased.k} of ${c.chased.of}${na(c.chased.na)}</td>
    <td class="num">${c.partial_in_band.k} of ${c.partial_in_band.of}${na(c.partial_in_band.na)}</td>
    <td class="num">${c.trail_on_signal.k} of ${c.trail_on_signal.of}</td>
    <td class="num">${c.stop_recorded.k} of ${c.stop_recorded.of}</td>
    <td class="num ${cls(c.net_r.sum)}">${fmtR(c.net_r.sum)}${c.net_r.excluded ? ` <span class="muted">(−${c.net_r.excluded})</span>` : ''}</td>
  </tr>`;
}

// The IDX equity snapshot is hand-entered and writes straight through (SPEC §9.7)
// — no confirm queue ever governed it. It sits by the banner's stated equity fact.
const idxEquityForm = `<form method="post" action="/action/equity-idx" class="eqform">
    <span class="muted" style="font-size:11.5px">Add IDX equity:</span>
    <input class="mini" type="date" name="date" required>
    <input class="mini" type="number" step="any" name="portfolio" placeholder="portfolio" required>
    <input class="mini" type="number" step="any" name="ledger_balance" placeholder="ledger" required>
    <button type="submit">Record</button>
  </form>`;

function bannerSection(items: BannerItem[]): string {
  const li = items.map((x) =>
    `<li class="sev-${x.severity}"><b>${esc(x.title)}</b> — ${esc(x.body)}</li>`).join('');
  return `<section class="banner"><h2>Needs attention</h2>
    ${items.length ? `<ul>${li}</ul>` : '<p class="muted" style="font-size:13px;margin:0">Nothing needs attention — no stop holes, no held enrichment, no drift.</p>'}
    <p class="muted" style="font-size:11.5px;margin:8px 0 0">Intake and equity are stated facts, not alarms; <b>insufficient_history</b> is a no-action item, distinct from a repair.</p>
    ${idxEquityForm}</section>`;
}

function listRow(t: ReviewTrade, selected: string | null): string {
  const sel = String(t.id) === selected ? ' sel' : '';
  const right = t.status === 'closed'
    ? `<span class="${cls(t.realized_r)}" style="float:right">${realizedLabel(t)}</span>`
    : `<span class="muted" style="float:right">${t.open_r === null ? 'open' : fmtR(t.open_r) + ' open'}</span>`;
  const tail = t.status === 'closed'
    ? ` → ${day(t.final_exit_date)}`
    : ` · held ${t.days_held ?? 0}d`;
  const noStop = t.stop === null ? ' · <span style="color:var(--bad)">no stop</span>' : '';
  // `#work` because selecting a Trade is a full page load — the surface has no
  // JavaScript — and without a fragment the browser lands at the top, putting
  // the banner and the counts table between the reader and the workbench they
  // just clicked into. The fragment scrolls the list and the detail pane, which
  // share the top of `.work`, into view together, so clicking down the list
  // reads as switching panes rather than as the page jumping.
  return `<a class="ti${sel}" href="?trade=${t.id}#work">
    <span class="s">${esc(t.symbol)}</span><span class="muted" style="font-size:11px"> ${esc(t.book)}</span>
    ${right}<span class="m">${day(t.entry_date)}${tail}${noStop}</span></a>`;
}

function group(label: string, ts: ReviewTrade[], selected: string | null, note = ''): string {
  if (!ts.length) return '';
  return `<div class="grp">${esc(label)}${note ? ` <span style="font-weight:400;text-transform:none;letter-spacing:0">${esc(note)}</span>` : ''}</div>
    ${ts.map((t) => listRow(t, selected)).join('')}`;
}

export function renderReview(state: ReviewState, flash?: string): string {
  const all = [...state.weekTrades, ...state.stragglers, ...state.openTrades];
  const sel = all.find((t) => String(t.id) === state.selected) ?? null;
  const detail = sel
    ? (sel.status === 'closed' ? closedDetail(sel) : openDetail(sel))
    : '<div class="card"><p class="muted">No Trades in scope — the week is empty, and there are no stragglers or open Trades to review.</p></div>';

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Weekly review — Automatic Trading Journal</title>
<style>
  :root{--ink:#16181d;--ink-soft:#5a606b;--ink-faint:#8b919c;--line:#e3e5ea;--paper:#fbfbfc;--card:#fff;
    --accent:#1f6feb;--accent-soft:#eaf1fe;--warn:#a8580a;--warn-soft:#fdf3e6;--bad:#b3261e;--bad-soft:#fdecea;--good:#17694a;--good-soft:#e8f4ee;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
  .wrap{max-width:1240px;margin:0 auto;padding:32px 28px 90px}
  header.top{border-bottom:1px solid var(--line);padding-bottom:18px;margin-bottom:22px}
  h1{font-size:24px;margin:0 0 4px;letter-spacing:-0.01em}
  h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-faint);margin:0 0 12px;font-weight:700}
  h3{font-size:15px;margin:0 0 6px}
  p{margin:0 0 12px;max-width:80ch}
  section{margin-bottom:26px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
  .muted{color:var(--ink-faint)} .soft{color:var(--ink-soft)}
  .pill{display:inline-block;font-size:10.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;padding:2px 7px;border-radius:20px;background:var(--accent-soft);color:var(--accent);white-space:nowrap}
  .pill.bad{background:var(--bad-soft);color:var(--bad)} .pill.warn{background:var(--warn-soft);color:var(--warn)}
  .pill.good{background:var(--good-soft);color:var(--good)} .pill.mute{background:#eff0f3;color:var(--ink-soft)}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th{text-align:left;font-weight:700;color:var(--ink-faint);font-size:11px;letter-spacing:.05em;text-transform:uppercase;padding:6px 8px;border-bottom:1px solid var(--line)}
  td{padding:7px 8px;border-bottom:1px solid #f0f1f4;vertical-align:top}
  td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
  .pos{color:var(--good);font-weight:600} .neg{color:var(--bad);font-weight:600}
  button{font:inherit;font-size:13px;cursor:pointer;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:7px;padding:6px 11px}
  button:hover{border-color:var(--accent);color:var(--accent)}
  button.active{background:var(--accent-soft);border-color:var(--accent);color:var(--accent);font-weight:600}
  button:disabled{color:var(--ink-faint);cursor:default;background:#f6f7f9}
  input.mini,select{font:inherit;font-size:12px;padding:5px 7px;border:1px solid var(--line);border-radius:6px}
  input.mini{width:88px}
  .banner{border:1px solid #f0dcc0;background:var(--warn-soft);border-radius:9px;padding:13px 16px;margin-bottom:24px}
  .banner h2{color:var(--warn)} .banner ul{margin:0;padding-left:18px;font-size:13.5px} .banner li{margin-bottom:3px}
  .banner li.sev-info{color:var(--ink-soft)} .banner li.sev-bad b{color:var(--bad)}
  .work{display:grid;grid-template-columns:268px minmax(0,1fr);gap:24px;align-items:start;scroll-margin-top:20px}
  @media(max-width:1020px){.work{grid-template-columns:1fr}}
  .tradelist{border:1px solid var(--line);border-radius:10px;background:var(--card);overflow:hidden}
  .tradelist .grp{font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;font-weight:700;color:var(--ink-faint);background:#f6f7f9;padding:6px 13px;border-bottom:1px solid var(--line)}
  .ti{display:block;width:100%;text-align:left;text-decoration:none;color:inherit;border-bottom:1px solid #f0f1f4;padding:10px 13px;background:transparent}
  .ti:hover{background:var(--accent-soft)} .ti.sel{background:var(--accent-soft);box-shadow:inset 3px 0 0 var(--accent)}
  .ti .s{font-weight:700} .ti .m{font-size:11.5px;color:var(--ink-faint);display:block}
  .timeline{display:grid;gap:2px;margin:6px 0 4px} .tl-days{display:grid;gap:2px}
  .tl-cell{height:30px;border-radius:3px;background:#eef0f3;position:relative} .tl-cell.held{background:#dbe6fb}
  .tl-cell.mark::after{content:attr(data-m);position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;color:#fff;border-radius:3px}
  .tl-cell.entry::after{background:var(--ink)} .tl-cell.exit::after{background:var(--accent)}
  .tl-cell.mfe::after{background:var(--good)} .tl-cell.mae::after{background:var(--bad)} .tl-cell.sim::after{background:var(--warn)}
  .tl-axis{display:grid;gap:2px;font-size:9.5px;color:var(--ink-faint);text-align:center;font-variant-numeric:tabular-nums}
  .legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--ink-soft);margin-top:6px}
  .legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:-1px}
  .actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;border-top:1px solid var(--line);padding-top:12px;margin-top:14px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px} @media(max-width:780px){.grid2{grid-template-columns:1fr}}
  .kv{display:grid;grid-template-columns:max-content 1fr;gap:3px 14px;font-size:12.5px}
  .kv dt{color:var(--ink-faint)} .kv dd{margin:0;font-variant-numeric:tabular-nums}
  .qflag{font-size:12px;color:var(--warn);background:var(--warn-soft);border:1px solid #f0dcc0;border-radius:6px;padding:7px 10px;margin-top:10px}
  .flash{background:var(--good-soft);border:1px solid #bfe0cf;color:var(--good);border-radius:8px;padding:8px 12px;margin-bottom:16px;font-size:13px}
  .eqform{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:10px;padding-top:10px;border-top:1px solid #f0dcc0}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <h1>Weekly review</h1>
    <p class="muted" style="margin:0">${esc(state.week_label)}${state.as_of ? ` · as-of ${esc(state.as_of)}` : ''} — a per-Trade exit workbench (SPEC §11).</p>
  </header>
  ${flash ? `<div class="flash">${esc(flash)}</div>` : ''}
  ${bannerSection(state.banner)}
  <section>
    <h2>${esc(state.week_label)} · the week in counts</h2>
    <div class="card" style="padding:4px 14px 8px">
      <table>
        <thead><tr><th>Book</th><th class="num">Closed</th><th class="num">Chased</th><th class="num">Partial in band</th><th class="num">Trail on signal</th><th class="num">Stop recorded</th><th class="num">Net R</th></tr></thead>
        <tbody>${state.counts.map(countRow).join('')}</tbody>
      </table>
      <p class="muted" style="font-size:11.5px;margin:8px 0 4px">
        Counts, never rates — at this sample a percentage would invent precision that isn't there. Split by book because no number combines the two; the list below may mix, a number may not. <b>n/a</b> is absence, not deviation.
      </p>
    </div>
  </section>
  <section class="work" id="work">
    <div class="tradelist">
      ${group('Closed this week', state.weekTrades, state.selected)}
      ${group('Unreviewed from earlier', state.stragglers, state.selected, state.stragglers.length ? `· ${state.stragglers.length} waiting` : '')}
      ${group('Open — not in the counts', state.openTrades, state.selected)}
      ${all.length === 0 ? '<div class="grp">Nothing in scope</div>' : ''}
    </div>
    <div>${detail}</div>
  </section>
</div>
</body>
</html>`;
}
