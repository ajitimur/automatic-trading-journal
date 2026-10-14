// Forwarding the Stockbit TC PDF (#52), driven from outside like relay.test.ts:
// the receipt reply, what the Mac pulls, the document download, and the day
// record the evening reminder reads.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ack, forward, harness, mac, pull, say } from './harness.ts';

const EVENING = '2026-10-13T11:20:00Z'; // 18:20 WIB, 13 Oct
const TC = { file_name: 'TC_13102026.pdf', mime_type: 'application/pdf', file_size: 48213 };

test('a forwarded PDF from the trader gets a receipt reply and is pulled', async () => {
  const h = harness();
  assert.equal(await forward(h, TC, EVENING, 9001), '✓ Got TC_13102026.pdf; it goes in on the next run');
  assert.deepEqual(await pull(h), {
    items: [
      {
        id: '9001',
        telegram_at: EVENING,
        text: '',
        parse: { kind: 'document', book: 'IDX', file_id: 'file-9001', file_name: 'TC_13102026.pdf', file_size: 48213 },
      },
    ],
  });
});

for (const doc of [
  { file_name: 'screenshot.png', mime_type: 'image/png' },
  { file_name: 'TC_13102026.xlsx', mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' },
  { file_name: 'notes' },
]) {
  test(`a non-PDF document (${doc.file_name}) gets told only TC PDFs are accepted and is not pulled`, async () => {
    const h = harness();
    assert.equal(await forward(h, doc, EVENING), '✗ Only Stockbit TC PDFs are accepted');
    assert.deepEqual(await pull(h), { items: [] });
  });
}

test('a PDF sent without a MIME type is still taken by its name', async () => {
  const h = harness();
  await forward(h, { file_name: 'tc.PDF' }, EVENING, 9002);
  assert.deepEqual((await pull(h)).items.map((i) => i.id), ['9002']);
});

test('a document from any other user gets no reply and stores nothing', async () => {
  const h = harness();
  assert.equal(await forward(h, TC, EVENING, 9003, 9999), undefined);
  assert.deepEqual(await pull(h), { items: [] });
});

test('a forward Telegram retries is stored once, and the trader still gets a reply', async () => {
  const h = harness();
  await forward(h, TC, EVENING, 9004);
  assert.ok(await forward(h, TC, EVENING, 9004));
  assert.deepEqual((await pull(h)).items.map((i) => i.id), ['9004']);
});

test('the Mac fetches a held PDF by its item id, as base64', async () => {
  const h = harness();
  const bytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0xff, 0x00]); // "%PDF-" then binary
  h.files.set('file-9005', bytes);
  await forward(h, TC, EVENING, 9005);
  const res = await h.relay.fetch(mac('/documents/9005'));
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { id: '9005', content_base64: 'JVBERi3/AA==' });
});

test('a document fetch without the Mac secret is rejected', async () => {
  const h = harness();
  h.files.set('file-9006', new Uint8Array([1]));
  await forward(h, TC, EVENING, 9006);
  assert.equal((await h.relay.fetch(mac('/documents/9006', { secret: 'guess' }))).status, 401);
});

test('a document fetch for an id the relay does not hold as a document is a 404', async () => {
  const h = harness();
  h.files.set('file-9007', new Uint8Array([1]));
  await forward(h, TC, EVENING, 9007);
  await say(h, 'BBRI 4120', EVENING, 9008);
  await ack(h, ['9007']);
  for (const id of ['9007', '9008', 'nope']) {
    assert.equal((await h.relay.fetch(mac(`/documents/${id}`))).status, 404, id);
  }
});

// What the 20:00 WIB reminder asks: did a TC come in on IDX trading day D?
test('the relay can say a PDF arrived on the Jakarta day it was forwarded, and not on another', async () => {
  const h = harness();
  assert.equal(await h.relay.pdfArrivedOn('2026-10-13'), false);
  await forward(h, TC, EVENING);
  assert.equal(await h.relay.pdfArrivedOn('2026-10-13'), true);
  assert.equal(await h.relay.pdfArrivedOn('2026-10-12'), false);
  assert.equal(await h.relay.pdfArrivedOn('2026-10-14'), false);
});

test('a PDF forwarded after midnight WIB counts for the Jakarta day, not the UTC one', async () => {
  const h = harness();
  await forward(h, TC, '2026-10-13T18:00:00Z'); // 01:00 WIB, 14 Oct
  assert.equal(await h.relay.pdfArrivedOn('2026-10-13'), false);
  assert.equal(await h.relay.pdfArrivedOn('2026-10-14'), true);
});

test('the day keeps its PDF after the Mac has pulled and acknowledged it', async () => {
  const h = harness();
  await forward(h, TC, EVENING, 9010);
  await ack(h, ['9010']);
  assert.equal(await h.relay.pdfArrivedOn('2026-10-13'), true);
});

test('a rejected non-PDF document does not count as the day\'s PDF', async () => {
  const h = harness();
  await forward(h, { file_name: 'screenshot.png', mime_type: 'image/png' }, EVENING);
  assert.equal(await h.relay.pdfArrivedOn('2026-10-13'), false);
});

test('the scheduled handler forgets a PDF day once it is more than 7 Jakarta days old', async () => {
  const h = harness('2026-10-13T12:00:00Z');
  await forward(h, TC, EVENING);
  h.setNow('2026-10-20T16:59:00Z'); // 23:59 WIB, 20 Oct
  await h.relay.scheduled();
  assert.equal(await h.relay.pdfArrivedOn('2026-10-13'), true);
  h.setNow('2026-10-20T17:00:00Z'); // 00:00 WIB, 21 Oct
  await h.relay.scheduled();
  assert.equal(await h.relay.pdfArrivedOn('2026-10-13'), false);
});

test('a Telegram download failure is a 502, and the item stays held', async () => {
  const h = harness();
  await forward(h, TC, EVENING, 9009); // no bytes registered: the download throws
  assert.equal((await h.relay.fetch(mac('/documents/9009'))).status, 502);
  assert.deepEqual((await pull(h)).items.map((i) => i.id), ['9009']);
});
