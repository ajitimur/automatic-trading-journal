// Answering a nag (#55): a bare reply to the Mac's nag takes its Trade from the
// nag, and a split-day nag's one-tap button declares the offered Stop. Driven
// from outside like relay.test.ts: the Mac's /send, the trader's replies and
// taps go in as Requests; what Telegram was asked to do and what a pull
// returns come out.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { harness, mac, pull, replyTo, say, sendNag, tap, TRADER, type Harness } from './harness.ts';

const USAGE_REPLY = '✗ Expected: [idx|us] SYMBOL STOP [bb|htf|oth], SYMBOL no sl, or /undo. E.g. BBRI 4120 htf';

const BBRI_13 = { book: 'IDX', symbol: 'BBRI', entry_date: '2026-10-13' };
const BBRI_14 = { book: 'IDX', symbol: 'BBRI', entry_date: '2026-10-14' };

// The Mac nags about `trade`; returns the nag's Telegram message id.
async function nag(h: Harness, trade = BBRI_13, offer?: { stop: number; entry_date: string }): Promise<number> {
  const res = await sendNag(h, trade, offer);
  assert.equal(res.status, 200);
  return ((await res.json()) as { message_id: number }).message_id;
}

// -- the bare reply -----------------------------------------------------------

test('a bare stop replied to a nag declares it for the nag’s Trade, dated by the nag', async () => {
  const h = harness('2026-10-14T02:00:00Z');
  h.setNextMessageId(7001);
  const nagId = await nag(h);
  // Sent the day after entry: the entry date still comes from the nag.
  const reply = await replyTo(h, nagId, '4120 htf', '2026-10-14T02:00:00Z', 8001);
  assert.equal(reply, '✓ IDX BBRI SL 4,120, high tight flag, entry 13 Oct');
  assert.deepEqual(await pull(h), {
    items: [
      {
        id: '8001',
        telegram_at: '2026-10-14T02:00:00Z',
        text: '4120 htf',
        parse: {
          kind: 'declaration',
          book: 'IDX',
          symbol: 'BBRI',
          stop: 4120,
          setup: 'high_tight_flag',
          entry_date: '2026-10-13',
        },
        answers: { nag: 7001, via: 'reply' },
      },
    ],
  });
});

test('"no sl" replied to a nag declines that Trade', async () => {
  const h = harness('2026-10-14T02:00:00Z');
  const nagId = await nag(h);
  const reply = await replyTo(h, nagId, 'no sl', '2026-10-14T02:00:00Z');
  assert.equal(reply, '✓ IDX BBRI no SL (declined), entry 13 Oct');
  const [item] = (await pull(h)).items;
  assert.deepEqual(item!.parse, { kind: 'decline', book: 'IDX', symbol: 'BBRI', entry_date: '2026-10-13' });
});

test('a US nag’s reply keeps the US book and its New York entry date', async () => {
  const h = harness('2026-10-14T02:00:00Z');
  const nagId = await nag(h, { book: 'US', symbol: 'AMD', entry_date: '2026-10-13' });
  const reply = await replyTo(h, nagId, '182.5', '2026-10-14T02:00:00Z');
  assert.equal(reply, '✓ US AMD SL 182.5, entry 13 Oct');
});

test('a bare stop not sent as a reply gets the grammar error and is not stored', async () => {
  const h = harness();
  for (const text of ['4120', '4120 htf', 'no sl']) {
    assert.equal(await say(h, text, '2026-10-13T02:15:00Z'), USAGE_REPLY);
  }
  assert.deepEqual(await pull(h), { items: [] });
});

test('a bare stop replied to a message that is not a nag gets the grammar error', async () => {
  const h = harness();
  await say(h, 'BBRI 4120', '2026-10-13T02:15:00Z');
  const ackId = h.sent.length; // the fake numbers messages by count sent
  assert.equal(await replyTo(h, ackId, '4130', '2026-10-13T02:16:00Z'), USAGE_REPLY);
  assert.equal((await pull(h)).items.length, 1);
});

test('a bare reply to a nag is checked like any stop', async () => {
  const h = harness('2026-10-14T02:00:00Z');
  const nagId = await nag(h);
  assert.equal(await replyTo(h, nagId, '0', '2026-10-14T02:00:00Z'), '✗ Stop must be a positive number, got 0');
  assert.equal(await replyTo(h, nagId, '4120.5', '2026-10-14T02:00:00Z'), '✗ IDX stop must be whole rupiah, got 4120.5');
  assert.equal(await replyTo(h, nagId, '4120 xyz', '2026-10-14T02:00:00Z'), '✗ Setup must be bb, htf or oth, got xyz');
  assert.deepEqual(await pull(h), { items: [] });
});

test('a full declaration replied to a nag reads as itself, not as the nag’s Trade', async () => {
  const h = harness('2026-10-14T02:00:00Z');
  const nagId = await nag(h);
  const reply = await replyTo(h, nagId, 'TLKM 2900', '2026-10-14T02:00:00Z');
  assert.equal(reply, '✓ IDX TLKM SL 2,900, entry 14 Oct');
  const [item] = (await pull(h)).items;
  assert.equal(item!.answers, undefined);
});

test('a bare reply replaces an earlier declaration for the nag’s Trade', async () => {
  const h = harness('2026-10-14T02:00:00Z');
  await say(h, 'BBRI 412', '2026-10-13T02:15:00Z');
  const nagId = await nag(h);
  const reply = await replyTo(h, nagId, '4120', '2026-10-14T02:00:00Z');
  assert.equal(reply, '✓ IDX BBRI SL 4,120, entry 13 Oct (replaced 412 → 4,120)');
});

test('the relay forgets a nag after 7 days, so a reply to it is a grammar error', async () => {
  const h = harness('2026-10-14T02:00:00Z');
  const nagId = await nag(h);
  h.setNow('2026-10-21T02:00:01Z');
  await h.relay.scheduled();
  assert.equal(await replyTo(h, nagId, '4120', '2026-10-21T02:00:01Z'), USAGE_REPLY);
  assert.deepEqual(await pull(h), { items: [] });
});

// -- the split-day button -----------------------------------------------------

test('a nag with an offer carries Yes and Other buttons; one without carries none', async () => {
  const h = harness();
  await nag(h, BBRI_13);
  await nag(h, BBRI_14, { stop: 4120, entry_date: '2026-10-13' });
  assert.equal(h.sent[0]!.buttons, undefined);
  assert.deepEqual(h.sent[1]!.buttons, [
    { text: 'Yes', data: 'yes' },
    { text: 'Other', data: 'other' },
  ]);
});

test('tapping Yes declares the offered stop for the nag’s Trade', async () => {
  const h = harness('2026-10-14T11:00:00Z');
  h.setNextMessageId(7002);
  const nagId = await nag(h, BBRI_14, { stop: 4120, entry_date: '2026-10-13' });
  h.setNow('2026-10-14T11:05:00Z');
  const res = await tap(h, nagId, 'yes', 8002);
  assert.equal(res.status, 200);

  assert.deepEqual(await pull(h), {
    items: [
      {
        id: '8002',
        telegram_at: '2026-10-14T11:05:00Z',
        text: 'Yes',
        parse: { kind: 'declaration', book: 'IDX', symbol: 'BBRI', stop: 4120, entry_date: '2026-10-14' },
        answers: { nag: 7002, via: 'button' },
      },
    ],
  });
  assert.deepEqual(h.answered, [{ callbackId: 'cb-8002' }]);
  assert.equal(h.sent.at(-1)!.text, '✓ IDX BBRI SL 4,120, entry 14 Oct');
});

test('a second tap on Yes declares nothing more', async () => {
  const h = harness();
  const nagId = await nag(h, BBRI_14, { stop: 4120, entry_date: '2026-10-13' });
  await tap(h, nagId, 'yes', 8101);
  const sentAfterFirst = h.sent.length;
  await tap(h, nagId, 'yes', 8102);
  assert.equal((await pull(h)).items.length, 1);
  assert.equal(h.sent.length, sentAfterFirst);
  assert.deepEqual(h.answered.at(-1), { callbackId: 'cb-8102', text: 'Already answered' });
});

test('a retried tap stores once and answers as the first did', async () => {
  const h = harness();
  const nagId = await nag(h, BBRI_14, { stop: 4120, entry_date: '2026-10-13' });
  await tap(h, nagId, 'yes', 8201);
  await tap(h, nagId, 'yes', 8201);
  assert.equal((await pull(h)).items.length, 1);
  assert.deepEqual(h.answered, [{ callbackId: 'cb-8201' }, { callbackId: 'cb-8201' }]);
});

test('tapping Other declares nothing and asks for a reply', async () => {
  const h = harness();
  const nagId = await nag(h, BBRI_14, { stop: 4120, entry_date: '2026-10-13' });
  await tap(h, nagId, 'other', 8301);
  assert.deepEqual(await pull(h), { items: [] });
  assert.match(h.answered[0]!.text ?? '', /^Reply to this message with the stop/);
});

test('after Other, a bare reply to the same nag still declares', async () => {
  const h = harness('2026-10-14T11:00:00Z');
  const nagId = await nag(h, BBRI_14, { stop: 4120, entry_date: '2026-10-13' });
  await tap(h, nagId, 'other');
  assert.equal(await replyTo(h, nagId, '4150', '2026-10-14T11:01:00Z'), '✓ IDX BBRI SL 4,150, entry 14 Oct');
});

test('a tap from any other user is dropped silently', async () => {
  const h = harness();
  const nagId = await nag(h, BBRI_14, { stop: 4120, entry_date: '2026-10-13' });
  const before = h.sent.length;
  const res = await tap(h, nagId, 'yes', 8401, 9999);
  assert.equal(res.status, 200);
  assert.deepEqual(h.answered, []);
  assert.equal(h.sent.length, before);
  assert.deepEqual(await pull(h), { items: [] });
});

test('a tap on a nag the relay no longer holds declares nothing and says so', async () => {
  const h = harness();
  await tap(h, 4444, 'yes', 8501);
  assert.deepEqual(await pull(h), { items: [] });
  assert.match(h.answered[0]!.text ?? '', /expired/);
});

// -- the Mac's side of /send ---------------------------------------------------

test('a send with a malformed trade or offer is a 400 and messages nobody', async () => {
  const h = harness();
  const bodies: unknown[] = [
    { text: 'hi', trade: 'IDX BBRI' },
    { text: 'hi', trade: { book: 'XX', symbol: 'BBRI', entry_date: '2026-10-13' } },
    { text: 'hi', trade: { book: 'IDX', symbol: '', entry_date: '2026-10-13' } },
    { text: 'hi', trade: { book: 'IDX', symbol: 'BBRI', entry_date: '13 Oct' } },
    { text: 'hi', trade: BBRI_14, offer: { stop: 0, entry_date: '2026-10-13' } },
    { text: 'hi', trade: BBRI_14, offer: { stop: 4120 } },
    { text: 'hi', offer: { stop: 4120, entry_date: '2026-10-13' } },
  ];
  for (const body of bodies) {
    const res = await h.relay.fetch(mac('/send', { method: 'POST', body }));
    assert.equal(res.status, 400, JSON.stringify(body));
  }
  assert.deepEqual(h.sent, []);
});

test('a plain send, with no trade, is remembered as no nag', async () => {
  const h = harness();
  const res = await h.relay.fetch(mac('/send', { method: 'POST', body: { text: 'hello' } }));
  const { message_id } = (await res.json()) as { message_id: number };
  assert.equal(await replyTo(h, message_id, '4120', '2026-10-13T02:15:00Z'), USAGE_REPLY);
  assert.deepEqual(h.sent[0], { chatId: TRADER, text: 'hello' });
});
