// The relay (ADR 0011) driven from outside: Telegram updates and Mac calls go
// in as Requests, and the tests read what came back, what the fake Telegram
// sender was asked to send, and what a pull returns. No Worker runtime.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  ack,
  harness,
  MAC_SECRET,
  mac,
  pull,
  sendFixtureMessages,
  TRADER,
  update,
  webhook,
  WEBHOOK_SECRET,
} from './harness.ts';

const USAGE_REPLY = '✗ Expected: [idx|us] SYMBOL STOP [bb|htf|oth], SYMBOL no sl, or /undo. E.g. BBRI 4120 htf';

test('a valid declaration from the trader gets a reply echoing book, symbol, stop and entry date', async () => {
  const h = harness();
  const res = await h.relay.fetch(webhook(update('BBRI 4120', '2026-10-13T02:15:00Z')));
  assert.equal(res.status, 200);
  assert.deepEqual(h.sent, [{ chatId: TRADER, text: '✓ IDX BBRI SL 4,120, entry 13 Oct' }]);
});

test('a stored declaration is pulled with its id, Telegram timestamp, raw text and parse result', async () => {
  const h = harness();
  await h.relay.fetch(webhook(update('bbri 4120', '2026-10-13T02:15:00Z', TRADER, 7001)));
  assert.deepEqual(await pull(h), {
    items: [
      {
        id: '7001',
        telegram_at: '2026-10-13T02:15:00Z',
        text: 'bbri 4120',
        parse: { kind: 'declaration', book: 'IDX', symbol: 'BBRI', stop: 4120, entry_date: '2026-10-13' },
      },
    ],
  });
});

test('a message from any other user gets no reply and stores nothing', async () => {
  const h = harness();
  const res = await h.relay.fetch(webhook(update('BBRI 4120', '2026-10-13T02:15:00Z', 9999)));
  assert.equal(res.status, 200);
  assert.deepEqual(h.sent, []);
  assert.deepEqual(await pull(h), { items: [] });
});

for (const [label, secret] of [
  ['without', null],
  ['with the wrong', 'guess'],
] as const) {
  test(`a webhook call ${label} secret header is rejected`, async () => {
    const h = harness();
    const res = await h.relay.fetch(webhook(update('BBRI 4120', '2026-10-13T02:15:00Z'), secret));
    assert.equal(res.status, 401);
    assert.deepEqual(h.sent, []);
    assert.deepEqual(await pull(h), { items: [] });
  });

  test(`a pull ${label} Mac secret is rejected`, async () => {
    const h = harness();
    await h.relay.fetch(webhook(update('BBRI 4120', '2026-10-13T02:15:00Z')));
    const res = await h.relay.fetch(mac('/pull', { secret }));
    assert.equal(res.status, 401);
    assert.equal(await res.text(), '');
  });

  test(`an acknowledge ${label} Mac secret is rejected and deletes nothing`, async () => {
    const h = harness();
    await h.relay.fetch(webhook(update('BBRI 4120', '2026-10-13T02:15:00Z', TRADER, 7101)));
    const res = await h.relay.fetch(mac('/ack', { method: 'POST', body: { ids: ['7101'] }, secret }));
    assert.equal(res.status, 401);
    assert.equal((await pull(h)).items.length, 1);
  });
}

test('a US declaration sent at 01:00 WIB is dated the previous New York calendar day', async () => {
  const h = harness();
  // 01:00 WIB on 14 Oct is 14:00 in New York on 13 Oct.
  await h.relay.fetch(webhook(update('AMD 182.5', '2026-10-13T18:00:00Z')));
  assert.deepEqual(h.sent.map((m) => m.text), ['✓ US AMD SL 182.5, entry 13 Oct']);
  assert.deepEqual((await pull(h)).items[0]?.parse, {
    kind: 'declaration',
    book: 'US',
    symbol: 'AMD',
    stop: 182.5,
    entry_date: '2026-10-13',
  });
});

test('an IDX declaration is dated in Jakarta, not UTC', async () => {
  const h = harness();
  // 03:00 WIB on 14 Oct is still 13 Oct in UTC.
  await h.relay.fetch(webhook(update('TLKM 2950', '2026-10-13T20:00:00Z')));
  assert.deepEqual(h.sent.map((m) => m.text), ['✓ IDX TLKM SL 2,950, entry 14 Oct']);
});

test('a symbol that is not 4 letters is read as US', async () => {
  const h = harness();
  await h.relay.fetch(webhook(update('nvda1 100', '2026-10-13T14:00:00Z')));
  await h.relay.fetch(webhook(update('MU 95', '2026-10-13T14:00:00Z')));
  assert.deepEqual(h.sent.map((m) => m.text), ['✓ US NVDA1 SL 100, entry 13 Oct', '✓ US MU SL 95, entry 13 Oct']);
});

for (const stop of ['0', '-4120', 'abc', '4120x', 'NaN', 'Infinity', '1e3']) {
  test(`a stop of "${stop}" gets an error reply and is not stored`, async () => {
    const h = harness();
    await h.relay.fetch(webhook(update(`BBRI ${stop}`, '2026-10-13T02:15:00Z')));
    assert.equal(h.sent.length, 1);
    assert.match(h.sent[0]!.text, /^✗ /);
    assert.match(h.sent[0]!.text, /stop/i);
    assert.deepEqual(await pull(h), { items: [] });
  });
}

for (const text of ['BBRI', 'hello there friend friend friend', '', '/start', 'BB-RI 4120', 'BBRI no', 'us BBRI']) {
  test(`a malformed message ${JSON.stringify(text)} gets the expected form and is not stored`, async () => {
    const h = harness();
    await h.relay.fetch(webhook(update(text, '2026-10-13T02:15:00Z')));
    assert.deepEqual(h.sent.map((m) => m.text), [USAGE_REPLY]);
    assert.deepEqual(await pull(h), { items: [] });
  });
}

// IDX prices are whole rupiah, and Indonesians write 4.120 for four thousand
// one hundred twenty: reading it as 4.12 would bind a nonsense Stop for good.
for (const stop of ['4.120', '4120.5']) {
  test(`an IDX stop with a fraction ("${stop}") gets an error reply and is not stored`, async () => {
    const h = harness();
    await h.relay.fetch(webhook(update(`BBRI ${stop}`, '2026-10-13T02:15:00Z')));
    assert.deepEqual(h.sent.map((m) => m.text), [`✗ IDX stop must be whole rupiah, got ${stop}`]);
    assert.deepEqual(await pull(h), { items: [] });
  });
}

function rawPost(path: string, body: string, headers: Record<string, string>): Request {
  return new Request(`https://relay.test${path}`, { method: 'POST', headers, body });
}

test('a webhook body that is not a Telegram update is dropped, not failed, so Telegram stops retrying', async () => {
  const h = harness();
  for (const body of ['not json', 'null', '[]']) {
    const res = await h.relay.fetch(rawPost('/telegram', body, { 'x-telegram-bot-api-secret-token': WEBHOOK_SECRET }));
    assert.equal(res.status, 200);
  }
  assert.deepEqual(h.sent, []);
  assert.deepEqual(await pull(h), { items: [] });
});

test('an acknowledge with a malformed body is a 400 and deletes nothing', async () => {
  const h = harness();
  await h.relay.fetch(webhook(update('BBRI 4120', '2026-10-13T02:15:00Z')));
  for (const body of ['not json', 'null', '{"ids": "7001"}']) {
    const res = await h.relay.fetch(rawPost('/ack', body, { authorization: `Bearer ${MAC_SECRET}` }));
    assert.equal(res.status, 400);
  }
  assert.equal((await pull(h)).items.length, 1);
});

test('pull returns items in arrival order, and acknowledging removes exactly those', async () => {
  const h = harness();
  await h.relay.fetch(webhook(update('BBRI 4120', '2026-10-13T02:15:00Z', TRADER, 8003)));
  await h.relay.fetch(webhook(update('TLKM 2950', '2026-10-13T02:16:00Z', TRADER, 8001)));
  await h.relay.fetch(webhook(update('MU 95', '2026-10-13T14:00:00Z', TRADER, 8002)));
  assert.deepEqual((await pull(h)).items.map((i) => i.id), ['8003', '8001', '8002']);

  assert.equal((await ack(h, ['8003', '8002'])).status, 204);
  assert.deepEqual((await pull(h)).items.map((i) => i.id), ['8001']);
});

test('acknowledging the same ids twice is a no-op', async () => {
  const h = harness();
  await h.relay.fetch(webhook(update('BBRI 4120', '2026-10-13T02:15:00Z', TRADER, 8101)));
  await h.relay.fetch(webhook(update('TLKM 2950', '2026-10-13T02:16:00Z', TRADER, 8102)));
  assert.equal((await ack(h, ['8101'])).status, 204);
  assert.equal((await ack(h, ['8101'])).status, 204);
  assert.deepEqual((await pull(h)).items.map((i) => i.id), ['8102']);
});

test('a webhook Telegram retries is stored once, and the trader still gets a reply', async () => {
  const h = harness();
  const retried = update('BBRI 4120', '2026-10-13T02:15:00Z', TRADER, 8201);
  await h.relay.fetch(webhook(retried));
  await h.relay.fetch(webhook(retried));
  assert.deepEqual((await pull(h)).items.map((i) => i.id), ['8201']);
  assert.equal(h.sent.length, 2);
});

test('an item never acknowledged is still pulled at 29 days and gone after 30', async () => {
  const h = harness('2026-10-13T03:00:00Z');
  await h.relay.fetch(webhook(update('BBRI 4120', '2026-10-13T02:59:00Z', TRADER, 8401)));
  h.setNow('2026-11-11T03:00:00Z');
  await h.relay.fetch(webhook(update('TLKM 2950', '2026-11-11T02:59:00Z', TRADER, 8402)));
  assert.deepEqual((await pull(h)).items.map((i) => i.id), ['8401', '8402']);

  h.setNow('2026-11-12T03:00:01Z');
  assert.deepEqual((await pull(h)).items.map((i) => i.id), ['8402']);
});

test('the scheduled handler deletes expired items even when the Mac never pulls', async () => {
  const h = harness('2026-10-13T03:00:00Z');
  await h.relay.fetch(webhook(update('BBRI 4120', '2026-10-13T02:59:00Z', TRADER, 8501)));
  h.setNow('2026-11-12T03:00:01Z');
  await h.relay.scheduled();
  assert.deepEqual(await h.store.list(), []);
});

// The pull contract both sides read (#46): the job's tests feed this payload to
// the Mac side, so the relay must produce exactly it from the same messages.
test('the relay pulls exactly the shared fixture from the messages it describes', async () => {
  const h = harness('2026-10-14T00:00:00Z');
  const fixture = await sendFixtureMessages(h);
  assert.deepEqual(await pull(h), fixture);
});
