// The on-demand localhost UI (SPEC §13.6). Launched when you want to look,
// serves 127.0.0.1 only, and exits on demand — nothing is resident. Access is
// local-only: a weekly review is a sit-down activity, so no auth, no
// certificates, no public attack surface.

import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { readState } from './db.ts';
import { renderPage } from './render.ts';

export interface UiServer {
  port: number;
  url: string;
  close(): Promise<void>;
}

export function createRequestHandler(dbPath: string) {
  return (req: IncomingMessage, res: ServerResponse): void => {
    // Exit on demand: a GET to /shutdown closes the session (nothing resident).
    if (req.url === '/shutdown') {
      res.writeHead(200, { 'content-type': 'text/plain' });
      res.end('bye\n');
      // Defer so the response flushes before the process ends.
      setImmediate(() => process.exit(0));
      return;
    }
    if (req.url === '/health') {
      res.writeHead(200, { 'content-type': 'text/plain' });
      res.end('ok\n');
      return;
    }
    try {
      const state = readState(dbPath);
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      res.end(renderPage(state));
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
        close: () =>
          new Promise<void>((res) => server.close(() => res())),
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
