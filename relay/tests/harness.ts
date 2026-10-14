// Drives the relay from outside: Telegram updates and Mac calls go in as
// Requests; a fake Telegram sender records what went out; a settable clock
// stands in for time.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import {
  createRelay,
  keyOf,
  type Button,
  type PullItem,
  type SentNag,
  type RecentDeclaration,
  type Relay,
  type RelayStore,
  type StoredItem,
} from '../src/relay.ts';

export const WEBHOOK_SECRET = 'webhook-secret';
export const MAC_SECRET = 'mac-secret';
export const TRADER = 4242;
export const PULL_FIXTURE = join(resolve(import.meta.dirname, '..', '..'), 'docs', 'samples', 'relay-pull-fixture.json');

// The store fake: the same contract as d1Store, held in Maps (insertion order
// is arrival order).
export function memoryStore(): RelayStore {
  const items = new Map<string, StoredItem>();
  const recent = new Map<string, RecentDeclaration>();
  let knownUs: string[] = [];
  const pdfDays = new Set<string>();
  const nags = new Map<number, SentNag>();
  return {
    async put(stored) {
      if (!items.has(stored.item.id)) items.set(stored.item.id, stored);
    },
    async list() {
      return [...items.values()];
    },
    async delete(ids) {
      for (const id of ids) items.delete(id);
    },
    async recent() {
      return [...recent.values()];
    },
    async putRecent(declared) {
      recent.set(keyOf(declared.parse), declared);
    },
    async deleteRecent(ids) {
      for (const [key, declared] of recent) if (ids.includes(declared.id)) recent.delete(key);
    },
    async knownUsSymbols() {
      return [...knownUs];
    },
    async setKnownUsSymbols(symbols) {
      knownUs = [...symbols];
    },
    async pdfDays() {
      return [...pdfDays];
    },
    async putPdfDay(day) {
      pdfDays.add(day);
    },
    async deletePdfDays(days) {
      for (const day of days) pdfDays.delete(day);
    },
    async nags() {
      return [...nags.values()];
    },
    async putNag(nag) {
      nags.set(nag.message_id, nag);
    },
    async deleteNags(messageIds) {
      for (const id of messageIds) nags.delete(id);
    },
  };
}

// What the relay sent: `buttons` only on a message that carried them.
export interface Sent {
  chatId: number;
  text: string;
  buttons?: Button[];
}

export interface Harness {
  relay: Relay;
  sent: Sent[];
  // How the relay answered each button tap, in order.
  answered: { callbackId: string; text?: string }[];
  // The bytes Telegram serves for each file_id; any other id fails to download.
  files: Map<string, Uint8Array>;
  setNow(iso: string): void;
  // The message id Telegram gives the next message sent; by default the
  // count of messages sent so far.
  setNextMessageId(id: number): void;
  store: RelayStore;
}

export function harness(nowIso = '2026-10-13T03:00:00Z', store: RelayStore = memoryStore()): Harness {
  const sent: Sent[] = [];
  const answered: { callbackId: string; text?: string }[] = [];
  const files = new Map<string, Uint8Array>();
  let now = new Date(nowIso);
  let nextMessageId: number | null = null;
  const relay = createRelay({
    telegram: {
      async sendMessage(chatId, text, buttons) {
        sent.push(buttons ? { chatId, text, buttons } : { chatId, text });
        return nextMessageId === null ? sent.length : nextMessageId++;
      },
      async answerCallback(callbackId, text) {
        answered.push(text === undefined ? { callbackId } : { callbackId, text });
      },
      async downloadFile(fileId) {
        const bytes = files.get(fileId);
        if (!bytes) throw new Error(`Telegram has no file ${fileId}`);
        return bytes;
      },
    },
    store,
    clock: () => now,
    config: { webhookSecret: WEBHOOK_SECRET, macSecret: MAC_SECRET, userId: TRADER },
  });
  return {
    relay,
    sent,
    answered,
    files,
    store,
    setNow: (iso) => (now = new Date(iso)),
    setNextMessageId: (id) => (nextMessageId = id),
  };
}

let nextUpdateId = 1000;

export interface DocumentInfo {
  file_name?: string;
  mime_type?: string;
  file_id?: string;
  file_size?: number;
}

// A Telegram update carrying a document, as the Share sheet forwards one.
export function documentUpdate(document: DocumentInfo, sentAtIso: string, fromId = TRADER, updateId = nextUpdateId++) {
  return {
    update_id: updateId,
    message: {
      message_id: updateId,
      from: { id: fromId, is_bot: false, first_name: 'T' },
      chat: { id: fromId, type: 'private' },
      date: Math.floor(Date.parse(sentAtIso) / 1000),
      document: { file_id: `file-${updateId}`, file_unique_id: `unique-${updateId}`, ...document },
    },
  };
}

// The trader forwards a document at `sentAtIso`; returns the relay's reply.
export async function forward(
  h: Harness,
  document: DocumentInfo,
  sentAtIso: string,
  updateId?: number,
  fromId = TRADER,
): Promise<string | undefined> {
  const before = h.sent.length;
  await h.relay.fetch(webhook(documentUpdate(document, sentAtIso, fromId, updateId)));
  return h.sent.slice(before).at(-1)?.text;
}

// A Telegram text-message update from `fromId`, sent at `sentAtIso`, replying
// to the bot's message `replyTo` when given.
export function update(
  text: string,
  sentAtIso: string,
  fromId = TRADER,
  updateId = nextUpdateId++,
  replyTo?: number,
) {
  return {
    update_id: updateId,
    message: {
      message_id: updateId,
      from: { id: fromId, is_bot: false, first_name: 'T' },
      chat: { id: fromId, type: 'private' },
      date: Math.floor(Date.parse(sentAtIso) / 1000),
      text,
      ...(replyTo === undefined
        ? {}
        : { reply_to_message: { message_id: replyTo, from: { id: 1, is_bot: true }, chat: { id: fromId }, date: 0 } }),
    },
  };
}

// A Telegram callback_query update: `fromId` tapped the button carrying `data`
// on the bot's message `messageId`.
export function tapUpdate(messageId: number, data: string, fromId = TRADER, updateId = nextUpdateId++) {
  return {
    update_id: updateId,
    callback_query: {
      id: `cb-${updateId}`,
      from: { id: fromId, is_bot: false, first_name: 'T' },
      message: { message_id: messageId, chat: { id: fromId, type: 'private' }, date: 0 },
      chat_instance: 'ci',
      data,
    },
  };
}

export function webhook(body: unknown, secret: string | null = WEBHOOK_SECRET): Request {
  const headers: Record<string, string> = { 'content-type': 'application/json' };
  if (secret !== null) headers['x-telegram-bot-api-secret-token'] = secret;
  return new Request('https://relay.test/telegram', {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  });
}

export function mac(path: string, init: { method?: string; body?: unknown; secret?: string | null } = {}): Request {
  const headers: Record<string, string> = { 'content-type': 'application/json' };
  const secret = init.secret === undefined ? MAC_SECRET : init.secret;
  if (secret !== null) headers.authorization = `Bearer ${secret}`;
  return new Request(`https://relay.test${path}`, {
    method: init.method ?? 'GET',
    headers,
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  });
}

export interface PullBody {
  items: PullItem[];
}

export async function pull(h: Harness): Promise<PullBody> {
  const res = await h.relay.fetch(mac('/pull'));
  assert.equal(res.status, 200);
  return (await res.json()) as PullBody;
}

// Sends the trader messages the shared pull fixture describes, and returns the
// fixture the relay must then pull exactly.
export async function sendFixtureMessages(h: Harness): Promise<PullBody> {
  const fixture = JSON.parse(readFileSync(PULL_FIXTURE, 'utf8')) as PullBody;
  for (const item of fixture.items) {
    const { parse } = item;
    if (item.answers && parse.kind === 'declaration') {
      // The nag it answers goes out first, with the message id the item names.
      // The offer's entry date is the earlier Trade's, which the pull omits.
      const offer = item.answers.via === 'button' ? { stop: parse.stop, entry_date: '2026-10-13' } : undefined;
      h.setNextMessageId(item.answers.nag);
      await sendNag(h, { book: parse.book, symbol: parse.symbol, entry_date: parse.entry_date }, offer);
      if (item.answers.via === 'button') {
        // A tap carries no timestamp of its own; the relay's clock dates it.
        h.setNow(item.telegram_at);
        await h.relay.fetch(webhook(tapUpdate(item.answers.nag, 'yes', TRADER, Number(item.id))));
      } else {
        await h.relay.fetch(webhook(update(item.text, item.telegram_at, TRADER, Number(item.id), item.answers.nag)));
      }
      continue;
    }
    const body =
      parse.kind === 'document'
        ? documentUpdate(
            { file_id: parse.file_id, file_name: parse.file_name, file_size: parse.file_size, mime_type: 'application/pdf' },
            item.telegram_at,
            TRADER,
            Number(item.id),
          )
        : update(item.text, item.telegram_at, TRADER, Number(item.id));
    await h.relay.fetch(webhook(body));
  }
  return fixture;
}

export const ack = (h: Harness, ids: string[]) => h.relay.fetch(mac('/ack', { method: 'POST', body: { ids } }));

// The Mac's push of the symbols it knows on the US book.
export const pushKnownUs = (h: Harness, symbols: unknown) =>
  h.relay.fetch(mac('/known-us-symbols', { method: 'PUT', body: { symbols } }));

// The trader sends `text` at `sentAtIso`; returns the relay's reply.
export async function say(h: Harness, text: string, sentAtIso: string, updateId?: number): Promise<string | undefined> {
  const before = h.sent.length;
  await h.relay.fetch(webhook(update(text, sentAtIso, TRADER, updateId)));
  return h.sent.slice(before).at(-1)?.text;
}

// The Mac nags the trader about one Trade, optionally offering a Stop as a
// one-tap answer; returns the relay's response.
export function sendNag(
  h: Harness,
  trade: { book: string; symbol: string; entry_date: string },
  offer?: { stop: number; entry_date: string },
  text = `⏳ ${trade.book} ${trade.symbol} is waiting for a stop`,
): Promise<Response> {
  return h.relay.fetch(mac('/send', { method: 'POST', body: { text, trade, ...(offer ? { offer } : {}) } }));
}

// The trader replies `text` to the bot's message `messageId`; returns the
// relay's reply.
export async function replyTo(
  h: Harness,
  messageId: number,
  text: string,
  sentAtIso: string,
  updateId?: number,
): Promise<string | undefined> {
  const before = h.sent.length;
  await h.relay.fetch(webhook(update(text, sentAtIso, TRADER, updateId, messageId)));
  return h.sent.slice(before).at(-1)?.text;
}

// The trader taps the button carrying `data` on the bot's message `messageId`.
export function tap(h: Harness, messageId: number, data: string, updateId?: number, fromId = TRADER): Promise<Response> {
  return h.relay.fetch(webhook(tapUpdate(messageId, data, fromId, updateId)));
}
