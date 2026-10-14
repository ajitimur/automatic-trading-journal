// The 20:00 WIB TC reminder (#56), driven through the scheduled handler with a
// fixed clock and the fake Telegram sender.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { forward, harness, pushKnownUs, say, TRADER } from './harness.ts';

const MORNING = '2026-10-13T02:30:00Z'; // 09:30 WIB, 13 Oct
const LATER = '2026-10-13T02:45:00Z'; // 09:45 WIB, 13 Oct
const LUNCH = '2026-10-13T05:00:00Z'; // 12:00 WIB, 13 Oct
const CRON = '2026-10-13T13:00:00Z'; // 20:00 WIB, 13 Oct

// The cron fires; returns what the bot sent the trader during it.
async function cron(h: ReturnType<typeof harness>, nowIso = CRON): Promise<{ chatId: number; text: string }[]> {
  h.setNow(nowIso);
  const before = h.sent.length;
  await h.relay.scheduled();
  return h.sent.slice(before);
}

test('a day with IDX declarations and no PDF gets one reminder naming each declared symbol once', async () => {
  const h = harness(MORNING);
  await say(h, 'BBRI 4120 htf', MORNING);
  await say(h, 'BBRI 4150', LATER); // a replace: still one symbol
  await say(h, 'ANTM no sl', LUNCH);
  assert.deepEqual(await cron(h), [{ chatId: TRADER, text: "📄 Forward today's Stockbit TC (BBRI, ANTM)" }]);
});

test('a day a PDF has arrived gets no reminder', async () => {
  const h = harness(MORNING);
  await say(h, 'BBRI 4120', MORNING);
  await forward(h, { file_name: 'TC_13102026.pdf', mime_type: 'application/pdf' }, '2026-10-13T11:20:00Z');
  assert.deepEqual(await cron(h), []);
});

test('a PDF forwarded on an earlier day does not stand in for today', async () => {
  const h = harness(MORNING);
  await forward(h, { file_name: 'TC_12102026.pdf', mime_type: 'application/pdf' }, '2026-10-12T11:20:00Z');
  await say(h, 'BBRI 4120', MORNING);
  assert.deepEqual(await cron(h), [{ chatId: TRADER, text: "📄 Forward today's Stockbit TC (BBRI)" }]);
});

test('a day with only US declarations gets no reminder', async () => {
  const h = harness(MORNING);
  await pushKnownUs(h, ['NVDA']); // otherwise a 4-letter symbol reads as IDX
  await say(h, 'us AAPL 180', MORNING);
  await say(h, 'NVDA 120', LUNCH);
  assert.deepEqual(await cron(h), []);
});

test('a withdrawn declaration does not count', async () => {
  const h = harness(MORNING);
  await say(h, 'BBRI 4120', MORNING);
  await say(h, 'ANTM 1500', LATER);
  await say(h, '/undo', LUNCH);
  assert.deepEqual(await cron(h), [{ chatId: TRADER, text: "📄 Forward today's Stockbit TC (BBRI)" }]);
});

test('a day whose only IDX declaration was withdrawn gets no reminder', async () => {
  const h = harness(MORNING);
  await say(h, 'BBRI 4120', MORNING);
  await say(h, '/undo', LATER);
  assert.deepEqual(await cron(h), []);
});

test("yesterday's IDX declarations do not trigger today's reminder", async () => {
  const h = harness(MORNING);
  await say(h, 'BBRI 4120', '2026-10-12T02:30:00Z'); // 09:30 WIB, 12 Oct
  assert.deepEqual(await cron(h), []);
});
