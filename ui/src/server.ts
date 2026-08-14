// The on-demand localhost UI (SPEC §13.6). Launched when you want to look,
// serves 127.0.0.1 only, and exits on demand — nothing is resident. Access is
// local-only: a weekly review is a sit-down activity, so no auth, no
// certificates, no public attack surface.
//
// `/` is the weekly review surface (SPEC §11, #40); `/raw` keeps the diagnostic
// skeleton (Trades/Fills/latest run). Actions POST to `/action/<name>` and are
// shelled straight through the single CLI door (SPEC §5.1/§11.3), then redirect
// back — the UI itself never writes SQLite.

import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { execFile } from 'node:child_process';
import { resolve } from 'node:path';
import { readState } from './db.ts';
import { renderPage } from './render.ts';
import { readReview } from './review.ts';
import { renderReview } from './reviewRender.ts';

export interface UiServer {
  port: number;
  url: string;
  close(): Promise<void>;
}

const JOB_DIR = resolve(import.meta.dirname, '..', '..', 'job');

// The single CLI door. Every write action shells this — the UI holds the store
// read-only and never mutates it directly.
function runJournal(dbPath: string, args: string[]): Promise<{ ok: boolean; message: string }> {
  return new Promise((res) => {
    execFile(
      'python3',
      ['-m', 'journal', ...args, '--db', dbPath],
      { cwd: JOB_DIR, env: { ...process.env, PYTHONPATH: JOB_DIR } },
      (err, stdout, stderr) => {
        if (err) res({ ok: false, message: (stderr || stdout || String(err)).trim() });
        else res({ ok: true, message: stdout.trim() });
      },
    );
  });
}

// Map an action name + form fields to CLI arguments. Unknown actions are refused.
function actionArgs(name: string, form: URLSearchParams): string[] | null {
  const tradeId = form.get('trade_id') ?? '';
  switch (name) {
    case 'stop':
      return ['stop', tradeId, form.get('price') ?? ''];
    case 'setup':
      return ['setup', tradeId, form.get('value') ?? ''];
    case 'review':
      return ['review', tradeId];
    case 'note':
      return ['note', tradeId, form.get('text') ?? ''];
    case 'exit-reason':
      return ['exit-reason', form.get('exit_id') ?? '', form.get('reason') ?? ''];
    case 'equity-idx':
      return [
        'equity-idx', '--date', form.get('date') ?? '',
        '--portfolio', form.get('portfolio') ?? '',
        '--ledger-balance', form.get('ledger_balance') ?? '',
      ];
    default:
      return null;
  }
}

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((res, rej) => {
    const chunks: Buffer[] = [];
    req.on('data', (c) => chunks.push(c as Buffer));
    req.on('end', () => res(Buffer.concat(chunks).toString('utf8')));
    req.on('error', rej);
  });
}

export function createRequestHandler(dbPath: string) {
  return (req: IncomingMessage, res: ServerResponse): void => {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1');

    // Exit on demand: a GET to /shutdown closes the session (nothing resident).
    if (url.pathname === '/shutdown') {
      res.writeHead(200, { 'content-type': 'text/plain' });
      res.end('bye\n');
      setImmediate(() => process.exit(0));
      return;
    }
    if (url.pathname === '/health') {
      res.writeHead(200, { 'content-type': 'text/plain' });
      res.end('ok\n');
      return;
    }

    // Write-through actions (SPEC §11.3): shell the CLI, then redirect back.
    if (req.method === 'POST' && url.pathname.startsWith('/action/')) {
      const name = url.pathname.slice('/action/'.length);
      void readBody(req).then(async (body) => {
        const form = new URLSearchParams(body);
        const args = actionArgs(name, form);
        if (args === null) {
          res.writeHead(400, { 'content-type': 'text/plain' });
          res.end(`unknown action: ${name}\n`);
          return;
        }
        const out = await runJournal(dbPath, args);
        const trade = form.get('trade_id');
        const back = new URL('http://127.0.0.1/');
        if (trade) back.searchParams.set('trade', trade);
        back.searchParams.set('msg', out.ok ? out.message || `${name} done` : `${name} refused: ${out.message}`);
        res.writeHead(303, { location: back.pathname + back.search });
        res.end();
      });
      return;
    }

    if (url.pathname === '/raw') {
      try {
        res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
        res.end(renderPage(readState(dbPath)));
      } catch (err) {
        res.writeHead(500, { 'content-type': 'text/plain' });
        res.end(`journal store unavailable: ${(err as Error).message}\n`);
      }
      return;
    }

    // The weekly review surface.
    try {
      const state = readReview(dbPath, {
        asOf: url.searchParams.get('as_of') ?? undefined,
        selected: url.searchParams.get('trade') ?? undefined,
      });
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      res.end(renderReview(state, url.searchParams.get('msg') ?? undefined));
    } catch (err) {
      res.writeHead(500, { 'content-type': 'text/plain' });
      res.end(`journal store unavailable: ${(err as Error).message}\n`);
    }
  };
}

// Bind to loopback only. host '127.0.0.1' keeps the surface local (SPEC §13.6).
export function startServer(dbPath: string, port = 0): Promise<UiServer> {
  const server = createServer(createRequestHandler(dbPath));
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', () => {
      const addr = server.address();
      const boundPort = typeof addr === 'object' && addr ? addr.port : port;
      resolve({
        port: boundPort,
        url: `http://127.0.0.1:${boundPort}/`,
        close: () => new Promise<void>((r) => server.close(() => r())),
      });
    });
  });
}

// Entry point: `node ui/src/server.ts`. Ctrl-C (SIGINT) exits cleanly — the
// on-demand session ends when you are done.
async function main(): Promise<void> {
  const dbPath = process.env.JOURNAL_DB ?? process.argv[2];
  if (!dbPath) {
    console.error('usage: node ui/src/server.ts <path-to-journal.db>  (or set $JOURNAL_DB)');
    process.exit(2);
  }
  const port = process.env.PORT ? Number(process.env.PORT) : 4319;
  const ui = await startServer(dbPath, port);
  console.log(`journal UI on ${ui.url}  (store: ${dbPath})`);
  console.log('press Ctrl-C to exit, or GET /shutdown');
  const stop = () => {
    void ui.close().then(() => process.exit(0));
  };
  process.on('SIGINT', stop);
  process.on('SIGTERM', stop);
}

// Run only when invoked directly, not when imported by tests.
if (process.argv[1] && process.argv[1].endsWith('server.ts')) {
  void main();
}
