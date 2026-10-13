// The relay over its production store. D1 is SQLite, so a D1-shaped shim over
// node:sqlite runs the store's real SQL; the relay is driven exactly as in
// relay.test.ts.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { d1Store, SCHEMA, type D1Database, type D1Statement } from '../src/d1Store.ts';
import { ack, harness, pull, PULL_FIXTURE, TRADER, update, webhook, type PullBody } from './harness.ts';

function sqliteD1(): D1Database {
  const db = new DatabaseSync(':memory:');
  db.exec(SCHEMA);
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
  const fixture = JSON.parse(readFileSync(PULL_FIXTURE, 'utf8')) as PullBody;
  const h = d1Harness('2026-10-14T00:00:00Z');
  for (const item of fixture.items) {
    await h.relay.fetch(webhook(update(item.text, item.telegram_at, TRADER, Number(item.id))));
  }
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
