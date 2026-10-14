// The relay over its production store. D1 is SQLite, so a D1-shaped shim over
// node:sqlite runs the store's real SQL; the relay is driven exactly as in
// relay.test.ts.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { d1Store, type D1Database, type D1Statement } from '../src/d1Store.ts';
import {
  ack,
  forward,
  harness,
  mac,
  pull,
  pushKnownUs,
  replyTo,
  say,
  sendFixtureMessages,
  sendNag,
  tap,
  TRADER,
  update,
  webhook,
} from './harness.ts';

// The same migrations `wrangler d1 migrations apply` runs, in its order.
const MIGRATIONS = join(resolve(import.meta.dirname, '..'), 'migrations');

function sqliteD1(): D1Database {
  const db = new DatabaseSync(':memory:');
  for (const name of readdirSync(MIGRATIONS).filter((n) => n.endsWith('.sql')).sort()) {
    db.exec(readFileSync(join(MIGRATIONS, name), 'utf8'));
  }
  return {
    prepare(sql) {
      const statement = (values: unknown[]): D1Statement => ({
        bind: (...more) => statement([...values, ...more]),
        async run() {
          return db.prepare(sql).run(...(values as string[]));
        },
        async all<T>() {
          return { results: db.prepare(sql).all(...(values as string[])) as T[] };
        },
      });
      return statement([]);
    },
  };
}

const d1Harness = (nowIso?: string) => harness(nowIso, d1Store(sqliteD1()));

test('over D1, the relay pulls exactly the shared fixture', async () => {
  const h = d1Harness('2026-10-14T00:00:00Z');
  const fixture = await sendFixtureMessages(h);
  assert.deepEqual(await pull(h), fixture);
});

test('over D1, a retried webhook stores once and items keep arrival order', async () => {
  const h = d1Harness();
  await h.relay.fetch(webhook(update('BBRI 4120', '2026-10-13T02:15:00Z', TRADER, 9003)));
  await h.relay.fetch(webhook(update('BBRI 4120', '2026-10-13T02:15:00Z', TRADER, 9003)));
  await h.relay.fetch(webhook(update('TLKM 2950', '2026-10-13T02:16:00Z', TRADER, 9001)));
  assert.deepEqual((await pull(h)).items.map((i) => i.id), ['9003', '9001']);
});

test('over D1, acknowledging removes those ids, and twice is a no-op', async () => {
  const h = d1Harness();
  await h.relay.fetch(webhook(update('BBRI 4120', '2026-10-13T02:15:00Z', TRADER, 9101)));
  await h.relay.fetch(webhook(update('TLKM 2950', '2026-10-13T02:16:00Z', TRADER, 9102)));
  assert.equal((await ack(h, ['9101', 'never-stored'])).status, 204);
  assert.equal((await ack(h, ['9101'])).status, 204);
  assert.equal((await ack(h, [])).status, 204);
  assert.deepEqual((await pull(h)).items.map((i) => i.id), ['9102']);
});

test('over D1, the scheduled handler deletes items past the 30-day TTL', async () => {
  const h = d1Harness('2026-10-13T03:00:00Z');
  await h.relay.fetch(webhook(update('BBRI 4120', '2026-10-13T02:59:00Z', TRADER, 9201)));
  h.setNow('2026-11-12T03:00:01Z');
  await h.relay.scheduled();
  assert.deepEqual(await h.store.list(), []);
});

test('over D1, a resend says what it replaced even once the Mac has acknowledged the first', async () => {
  const h = d1Harness();
  await say(h, 'BBRI 412', '2026-10-13T02:15:00Z', 9301);
  await ack(h, ['9301']);
  assert.equal(
    await say(h, 'BBRI 4120 htf', '2026-10-13T02:16:00Z', 9302),
    '✓ IDX BBRI SL 4,120, high tight flag, entry 13 Oct (replaced 412 → 4,120 htf)',
  );
});

test('over D1, /undo withdraws the last declaration once, however often Telegram retries it', async () => {
  const h = d1Harness();
  await say(h, 'BBRI 4120', '2026-10-13T02:15:00Z', 9401);
  await say(h, 'TLKM no sl', '2026-10-13T02:16:00Z', 9402);
  const withdrewTlkm = '↩ Withdrew IDX TLKM no SL (declined), entry 13 Oct';
  assert.equal(await say(h, '/undo', '2026-10-13T02:17:00Z', 9403), withdrewTlkm);
  assert.equal(await say(h, '/undo', '2026-10-13T02:17:00Z', 9403), withdrewTlkm);
  assert.equal(await say(h, '/undo', '2026-10-13T02:18:00Z', 9404), '↩ Withdrew IDX BBRI SL 4,120, entry 13 Oct');
  assert.equal(await say(h, '/undo', '2026-10-13T02:19:00Z', 9405), '✗ Nothing to undo');
});

test('over D1, the known-US list is replaced on each push', async () => {
  const h = d1Harness();
  assert.equal(await say(h, 'META 590', '2026-10-13T18:00:00Z'), '✓ IDX META SL 590, entry 14 Oct');
  await pushKnownUs(h, ['META']);
  assert.equal(await say(h, 'META 590', '2026-10-13T18:00:00Z'), '✓ US META SL 590, entry 13 Oct');
  await pushKnownUs(h, []);
  assert.match((await say(h, 'META 600', '2026-10-13T18:01:00Z')) ?? '', /^✓ IDX META SL 600, entry 14 Oct/);
});

test('over D1, the scheduled handler forgets declarations past 7 days', async () => {
  const h = d1Harness('2026-10-13T03:00:00Z');
  await say(h, 'BBRI 4120', '2026-10-13T02:15:00Z');
  h.setNow('2026-10-20T03:00:00Z');
  await h.relay.scheduled();
  assert.deepEqual(await h.store.recent(), []);
});

test('over D1, a forwarded PDF is held and fetched, marks its Jakarta day once, and the day goes after 7', async () => {
  const h = d1Harness('2026-10-13T12:00:00Z');
  h.files.set('file-9501', new Uint8Array([0x25, 0x50, 0x44, 0x46]));
  const tc = { file_name: 'TC.pdf', mime_type: 'application/pdf' };
  await forward(h, tc, '2026-10-13T11:20:00Z', 9501);
  await forward(h, tc, '2026-10-13T11:20:00Z', 9501);
  await forward(h, tc, '2026-10-13T11:25:00Z', 9502);
  assert.deepEqual((await pull(h)).items.map((i) => i.id), ['9501', '9502']);
  assert.deepEqual(await (await h.relay.fetch(mac('/documents/9501'))).json(), { id: '9501', content_base64: 'JVBERg==' });
  assert.deepEqual(await h.store.pdfDays(), ['2026-10-13']);
  assert.equal(await h.relay.pdfArrivedOn('2026-10-13'), true);

  h.setNow('2026-10-20T17:00:00Z'); // 00:00 WIB, 21 Oct
  await h.relay.scheduled();
  assert.equal(await h.relay.pdfArrivedOn('2026-10-13'), false);
});

test('over D1, a nag is remembered: a reply and a tap resolve to its Trade, and it goes after 7 days', async () => {
  const h = d1Harness('2026-10-14T11:00:00Z');
  h.setNextMessageId(9601);
  await sendNag(h, { book: 'IDX', symbol: 'ADRO', entry_date: '2026-10-13' });
  await sendNag(h, { book: 'IDX', symbol: 'BBRI', entry_date: '2026-10-14' }, { stop: 4120, entry_date: '2026-10-13' });
  assert.equal(await replyTo(h, 9601, '2650', '2026-10-14T11:01:00Z', 9603), '✓ IDX ADRO SL 2,650, entry 13 Oct');
  await tap(h, 9602, 'yes', 9604);
  await tap(h, 9602, 'yes', 9605);
  assert.deepEqual(
    (await pull(h)).items.map((i) => [i.id, i.parse.kind === 'declaration' && i.parse.stop, i.answers?.via]),
    [
      ['9603', 2650, 'reply'],
      ['9604', 4120, 'button'],
    ],
  );

  h.setNow('2026-10-21T11:00:01Z');
  await h.relay.scheduled();
  assert.deepEqual(await h.store.nags(), []);
});
