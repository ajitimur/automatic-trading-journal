// Drives the relay from outside: Telegram updates and Mac calls go in as
// Requests; a fake Telegram sender records what went out; a settable clock
// stands in for time.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import {
  createRelay,
  keyOf,
  type PullItem,
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
  };
}

export interface Harness {
  relay: Relay;
  sent: { chatId: number; text: string }[];
  // The bytes Telegram serves for each file_id; any other id fails to download.
  files: Map<string, Uint8Array>;
  setNow(iso: string): void;
  store: RelayStore;
}

export function harness(nowIso = '2026-10-13T03:00:00Z', store: RelayStore = memoryStore()): Harness {
  const sent: { chatId: number; text: string }[] = [];
  const files = new Map<string, Uint8Array>();
  let now = new Date(nowIso);
  const relay = createRelay({
    telegram: {
      async sendMessage(chatId, text) {
        sent.push({ chatId, text });
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
  return { relay, sent, files, store, setNow: (iso) => (now = new Date(iso)) };
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

// A Telegram text-message update from `fromId`, sent at `sentAtIso`.
export function update(text: string, sentAtIso: string, fromId = TRADER, updateId = nextUpdateId++) {
  return {
    update_id: updateId,
    message: {
      message_id: updateId,
      from: { id: fromId, is_bot: false, first_name: 'T' },
      chat: { id: fromId, type: 'private' },
      date: Math.floor(Date.parse(sentAtIso) / 1000),
      text,
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
