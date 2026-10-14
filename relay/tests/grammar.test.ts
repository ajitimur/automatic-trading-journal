// The full declaration grammar (#51), driven from outside like relay.test.ts:
// setup codes, a declined stop, the book prefix, the known-US list, replace and
// /undo. Each test reads the reply the trader got and what the Mac would pull.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ack, harness, mac, pull, pushKnownUs, say } from './harness.ts';

const MORNING = '2026-10-13T02:15:00Z'; // 09:15 WIB, 13 Oct

for (const [code, setup, name] of [
  ['bb', 'base_breakout', 'base breakout'],
  ['htf', 'high_tight_flag', 'high tight flag'],
  ['oth', 'other', 'other'],
] as const) {
  test(`setup code "${code}" is read as ${setup} and echoed`, async () => {
    const h = harness();
    assert.equal(await say(h, `BBRI 4120 ${code.toUpperCase()}`, MORNING), `✓ IDX BBRI SL 4,120, ${name}, entry 13 Oct`);
    assert.deepEqual((await pull(h)).items[0]?.parse, {
      kind: 'declaration',
      book: 'IDX',
      symbol: 'BBRI',
      stop: 4120,
      setup,
      entry_date: '2026-10-13',
    });
  });
}

test('an unknown setup code gets an error reply and is not stored', async () => {
  const h = harness();
  assert.equal(await say(h, 'BBRI 4120 vcp', MORNING), '✗ Setup must be bb, htf or oth, got vcp');
  assert.deepEqual(await pull(h), { items: [] });
});

test('"no sl" is a declined stop, echoed with its book and entry date', async () => {
  const h = harness();
  assert.equal(await say(h, 'bbri NO SL', MORNING), '✓ IDX BBRI no SL (declined), entry 13 Oct');
  assert.deepEqual((await pull(h)).items[0]?.parse, {
    kind: 'decline',
    book: 'IDX',
    symbol: 'BBRI',
    entry_date: '2026-10-13',
  });
});

test('a declined stop may carry a setup too', async () => {
  const h = harness();
  assert.equal(await say(h, 'BBRI no sl htf', MORNING), '✓ IDX BBRI no SL (declined), high tight flag, entry 13 Oct');
  assert.deepEqual((await pull(h)).items[0]?.parse, {
    kind: 'decline',
    book: 'IDX',
    symbol: 'BBRI',
    setup: 'high_tight_flag',
    entry_date: '2026-10-13',
  });
});

test('a "us" prefix reads a 4-letter symbol as US, dated in New York', async () => {
  const h = harness();
  // 01:00 WIB on 14 Oct is 13 Oct in New York.
  assert.equal(await say(h, 'us META 590', '2026-10-13T18:00:00Z'), '✓ US META SL 590, entry 13 Oct');
  assert.equal((await pull(h)).items[0]?.parse.book, 'US');
});

test('an "idx" prefix reads any symbol as IDX, with the whole-rupiah rule', async () => {
  const h = harness();
  assert.equal(await say(h, 'IDX bbri1 100', MORNING), '✓ IDX BBRI1 SL 100, entry 13 Oct');
  assert.equal(await say(h, 'idx goto 50.5', MORNING), '✗ IDX stop must be whole rupiah, got 50.5');
});

test('META reads as IDX until the Mac pushes a known-US list containing it', async () => {
  const h = harness();
  assert.equal(await say(h, 'META 590', '2026-10-13T18:00:00Z'), '✓ IDX META SL 590, entry 14 Oct');

  assert.equal((await pushKnownUs(h, ['meta', 'AAPL'])).status, 204);
  assert.equal(await say(h, 'META 590', '2026-10-13T18:01:00Z'), '✓ US META SL 590, entry 13 Oct');
  // A prefix still wins over the list (and so lands on the first one's key).
  assert.equal(
    await say(h, 'idx META 600', '2026-10-13T18:02:00Z'),
    '✓ IDX META SL 600, entry 14 Oct (replaced 590 → 600)',
  );
});

test('each push replaces the known-US list', async () => {
  const h = harness();
  await pushKnownUs(h, ['META']);
  await pushKnownUs(h, ['AAPL']);
  assert.equal(await say(h, 'META 590', MORNING), '✓ IDX META SL 590, entry 13 Oct');
  assert.equal(await say(h, 'AAPL 200', MORNING), '✓ US AAPL SL 200, entry 12 Oct');
});

test('a known-US push without the Mac secret is rejected and changes nothing', async () => {
  const h = harness();
  const res = await h.relay.fetch(mac('/known-us-symbols', { method: 'PUT', body: { symbols: ['META'] }, secret: 'guess' }));
  assert.equal(res.status, 401);
  assert.equal(await say(h, 'META 590', MORNING), '✓ IDX META SL 590, entry 13 Oct');
});

test('a known-US push with a malformed body is a 400 and changes nothing', async () => {
  const h = harness();
  await pushKnownUs(h, ['META']);
  for (const symbols of ['META', [1, 2], null]) {
    assert.equal((await pushKnownUs(h, symbols)).status, 400);
  }
  assert.equal(await say(h, 'META 590', MORNING), '✓ US META SL 590, entry 12 Oct');
});

test('a resend for the same book, symbol and entry date says what it replaced', async () => {
  const h = harness();
  await say(h, 'BBRI 412', MORNING);
  assert.equal(await say(h, 'BBRI 4120', '2026-10-13T02:16:00Z'), '✓ IDX BBRI SL 4,120, entry 13 Oct (replaced 412 → 4,120)');
  // Both are on their way to the Mac: the history is events, not overwrites.
  assert.deepEqual((await pull(h)).items.map((i) => i.text), ['BBRI 412', 'BBRI 4120']);
});

test('a replace reads the value it replaced, stop or decline, with its setup', async () => {
  const h = harness();
  await say(h, 'BBRI no sl', MORNING);
  assert.equal(await say(h, 'BBRI 4120 htf', MORNING), '✓ IDX BBRI SL 4,120, high tight flag, entry 13 Oct (replaced no SL → 4,120 htf)');
  assert.equal(await say(h, 'BBRI no sl', MORNING), '✓ IDX BBRI no SL (declined), entry 13 Oct (replaced 4,120 htf → no SL)');
});

test('a resend still says what it replaced after the Mac has pulled the first', async () => {
  const h = harness();
  await say(h, 'BBRI 412', MORNING, 6001);
  await pull(h);
  await ack(h, ['6001']);
  assert.match((await say(h, 'BBRI 4120', '2026-10-13T05:00:00Z')) ?? '', /\(replaced 412 → 4,120\)$/);
});

test('another entry date, book or symbol is not a replace', async () => {
  const h = harness();
  await say(h, 'BBRI 4120', MORNING);
  assert.equal(await say(h, 'BBRI 4150', '2026-10-14T02:15:00Z'), '✓ IDX BBRI SL 4,150, entry 14 Oct');
  assert.equal(await say(h, 'us BBRI 41', MORNING), '✓ US BBRI SL 41, entry 12 Oct');
  assert.equal(await say(h, 'TLKM 2950', MORNING), '✓ IDX TLKM SL 2,950, entry 13 Oct');
});

test('a retried replace replies the same and never reads as replacing itself', async () => {
  const h = harness();
  await say(h, 'BBRI 412', MORNING, 6101);
  const first = await say(h, 'BBRI 4120', MORNING, 6102);
  assert.equal(await say(h, 'BBRI 4120', MORNING, 6102), first);
  assert.deepEqual((await pull(h)).items.map((i) => i.id), ['6101', '6102']);
});

test('/undo withdraws the last declaration and names it', async () => {
  const h = harness();
  await say(h, 'BBRI 4120', MORNING, 6201);
  await say(h, 'ASII 5000 bb', '2026-10-13T03:00:00Z', 6202);
  assert.equal(await say(h, '/undo', '2026-10-13T03:01:00Z', 6203), '↩ Withdrew IDX ASII SL 5,000, base breakout, entry 13 Oct');
  assert.deepEqual((await pull(h)).items.at(-1), {
    id: '6203',
    telegram_at: '2026-10-13T03:01:00Z',
    text: '/undo',
    parse: { kind: 'undo', book: 'IDX', symbol: 'ASII', entry_date: '2026-10-13', withdraws: '6202' },
  });
});

test('a second /undo withdraws the declaration before', async () => {
  const h = harness();
  await say(h, 'BBRI 4120', MORNING, 6301);
  await say(h, 'TLKM no sl', '2026-10-13T02:20:00Z', 6302);
  await say(h, '/undo', '2026-10-13T02:21:00Z');
  assert.equal(await say(h, '/undo', '2026-10-13T02:22:00Z'), '↩ Withdrew IDX BBRI SL 4,120, entry 13 Oct');
  assert.equal(await say(h, '/undo', '2026-10-13T02:23:00Z'), '✗ Nothing to undo');
});

test('/undo withdraws a declined stop too', async () => {
  const h = harness();
  await say(h, 'TLKM no sl', MORNING);
  assert.equal(await say(h, '/undo', MORNING), '↩ Withdrew IDX TLKM no SL (declined), entry 13 Oct');
});

test('/undo with nothing to withdraw gets an error reply and is not stored', async () => {
  const h = harness();
  assert.equal(await say(h, '/undo', MORNING), '✗ Nothing to undo');
  assert.deepEqual(await pull(h), { items: [] });
});

test('/undo after a replace withdraws the key, and a fresh declaration is not a replace', async () => {
  const h = harness();
  await say(h, 'BBRI 412', MORNING);
  await say(h, 'BBRI 4120', MORNING);
  assert.equal(await say(h, '/undo', MORNING), '↩ Withdrew IDX BBRI SL 4,120, entry 13 Oct');
  assert.equal(await say(h, '/undo', MORNING), '✗ Nothing to undo');
  assert.equal(await say(h, 'BBRI 4100', MORNING), '✓ IDX BBRI SL 4,100, entry 13 Oct');
});

test('a retried /undo withdraws once', async () => {
  const h = harness();
  await say(h, 'BBRI 4120', MORNING, 6401);
  await say(h, 'TLKM 2950', MORNING, 6402);
  const first = await say(h, '/undo', MORNING, 6403);
  assert.equal(await say(h, '/undo', MORNING, 6403), first);
  assert.deepEqual(
    (await pull(h)).items.map((i) => i.id),
    ['6401', '6402', '6403'],
  );
  // BBRI is still there to withdraw: the retry did not take it.
  assert.equal(await say(h, '/undo', MORNING), '↩ Withdrew IDX BBRI SL 4,120, entry 13 Oct');
});

test('the relay forgets declarations after 7 days, so /undo cannot reach back further', async () => {
  const h = harness('2026-10-13T03:00:00Z');
  await say(h, 'BBRI 4120', MORNING);
  h.setNow('2026-10-20T03:00:00Z');
  await h.relay.scheduled();
  assert.deepEqual(await h.store.recent(), []);
  assert.equal(await say(h, '/undo', '2026-10-20T03:00:00Z'), '✗ Nothing to undo');
});

test('/undo ignores a declaration past 7 days even before the scheduled purge', async () => {
  const h = harness('2026-10-13T03:00:00Z');
  await say(h, 'BBRI 4120', MORNING);
  h.setNow('2026-10-20T03:00:00Z');
  assert.equal(await say(h, '/undo', '2026-10-20T03:00:00Z'), '✗ Nothing to undo');
});
