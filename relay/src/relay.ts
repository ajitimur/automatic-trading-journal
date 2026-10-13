// The relay (ADR 0011): the one component off the Mac. It receives the bot's
// Telegram webhook, parses each Stop Declaration, replies at once with how it
// read it, and holds it until the Mac pulls and acknowledges it. It holds no
// broker credential and does no binding, gating or committing.
//
// Written as a plain module over injected dependencies — Telegram sender,
// store, clock, configuration — so the Worker runtime is a thin adapter
// (worker.ts) and the tests run under `node --test` with fakes.

export interface TelegramSender {
  sendMessage(chatId: number, text: string): Promise<void>;
}

export interface RelayConfig {
  // Telegram echoes this in X-Telegram-Bot-Api-Secret-Token on every webhook call.
  webhookSecret: string;
  // The Mac's bearer secret for pull and acknowledge.
  macSecret: string;
  // The trader's Telegram user ID; every other sender is dropped silently.
  userId: number;
}

export interface RelayDeps {
  telegram: TelegramSender;
  store: RelayStore;
  clock: () => Date;
  config: RelayConfig;
}

export interface Relay {
  fetch(request: Request): Promise<Response>;
  // The cron entry point: applies the TTL.
  scheduled(): Promise<void>;
}

export type Book = 'IDX' | 'US';

export interface Declaration {
  kind: 'declaration';
  book: Book;
  symbol: string;
  stop: number;
  entry_date: string;
}

type Parse = Declaration | { kind: 'error'; reason: string };

// The bits of a Telegram Update the relay reads.
interface TelegramUpdate {
  update_id: number;
  message?: {
    from?: { id: number };
    chat: { id: number };
    date: number;
    text?: string;
  };
}

// One entry of the pull payload — the contract with the Mac, pinned by
// docs/samples/relay-pull-fixture.json.
export interface PullItem {
  // Telegram's update_id: stable across webhook retries.
  id: string;
  // When Telegram received the message, UTC.
  telegram_at: string;
  text: string;
  parse: Declaration;
}

export interface StoredItem {
  item: PullItem;
  // The relay's clock when the item was first stored; the TTL counts from it.
  stored_at: string;
}

// Holds items until acknowledged. `put` ignores an id it already holds, so a
// retried webhook stores once. `list` returns arrival order.
export interface RelayStore {
  put(stored: StoredItem): Promise<void>;
  list(): Promise<StoredItem[]>;
  delete(ids: string[]): Promise<void>;
}

export function memoryStore(): RelayStore {
  const items = new Map<string, StoredItem>();
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
  };
}

// Each Book's exchange time zone: the entry date is the session the message
// was sent in, not the trader's local date.
const EXCHANGE_TZ: Record<Book, string> = {
  IDX: 'Asia/Jakarta',
  US: 'America/New_York',
};

const TTL_MS = 30 * 24 * 60 * 60 * 1000;

const USAGE ='Expected: SYMBOL STOP, e.g. BBRI 4120';

export function createRelay(deps: RelayDeps): Relay {
  const { telegram, store, clock, config } = deps;

  async function onWebhook(request: Request): Promise<Response> {
    const update = (await request.json()) as TelegramUpdate;
    const message = update.message;
    if (!message || message.from?.id !== config.userId || message.text === undefined) {
      return new Response(null, { status: 200 });
    }
    const sentAt = new Date(message.date * 1000);
    const parse = parseDeclaration(message.text, sentAt);
    if (parse.kind === 'declaration') {
      await store.put({
        item: { id: String(update.update_id), telegram_at: isoSeconds(sentAt), text: message.text, parse },
        stored_at: clock().toISOString(),
      });
    }
    await telegram.sendMessage(message.chat.id, describe(parse));
    return new Response(null, { status: 200 });
  }

  // Anything the Mac never acknowledged is deleted after the TTL, so a dead
  // laptop doesn't leave the trader's history on a server. Returns what's left.
  async function purgeExpired(): Promise<StoredItem[]> {
    const cutoff = clock().getTime() - TTL_MS;
    const all = await store.list();
    const expired = all.filter((stored) => Date.parse(stored.stored_at) < cutoff);
    if (expired.length > 0) await store.delete(expired.map((stored) => stored.item.id));
    return all.filter((stored) => !expired.includes(stored));
  }

  async function onPull(): Promise<Response> {
    const items = (await purgeExpired()).map((stored) => stored.item);
    return Response.json({ items });
  }

  // Deleting an id the store no longer holds is a no-op, so a repeated
  // acknowledge after a network blip is harmless.
  async function onAcknowledge(request: Request): Promise<Response> {
    const body = (await request.json()) as { ids?: unknown };
    const ids = Array.isArray(body.ids) ? body.ids.filter((id): id is string => typeof id === 'string') : null;
    if (ids === null) return new Response(null, { status: 400 });
    await store.delete(ids);
    return new Response(null, { status: 204 });
  }

  const fromTelegram = (request: Request) =>
    secretMatches(request.headers.get('x-telegram-bot-api-secret-token'), config.webhookSecret);
  const fromMac = (request: Request) =>
    secretMatches(request.headers.get('authorization'), `Bearer ${config.macSecret}`);
  const unauthorized = () => new Response(null, { status: 401 });

  return {
    async fetch(request) {
      const { pathname } = new URL(request.url);
      const route = `${request.method} ${pathname}`;
      if (route === 'POST /telegram') return fromTelegram(request) ? onWebhook(request) : unauthorized();
      if (route === 'GET /pull') return fromMac(request) ? onPull() : unauthorized();
      if (route === 'POST /ack') return fromMac(request) ? onAcknowledge(request) : unauthorized();
      return new Response(null, { status: 404 });
    },
    async scheduled() {
      await purgeExpired();
    },
  };
}

// Compares without an early exit on the first differing character, so the
// response time says nothing about how much of a guess was right.
function secretMatches(given: string | null, expected: string): boolean {
  if (given === null || expected === '') return false;
  let diff = given.length ^ expected.length;
  for (let i = 0; i < expected.length; i++) {
    diff |= (given.charCodeAt(i) || 0) ^ expected.charCodeAt(i);
  }
  return diff === 0;
}

// `SYMBOL STOP`. A 4-letter symbol is IDX, anything else US.
function parseDeclaration(text: string, sentAt: Date): Parse {
  const words = text.trim().split(/\s+/);
  if (words.length !== 2) return { kind: 'error', reason: USAGE };
  const [rawSymbol, rawStop] = words as [string, string];
  const symbol = rawSymbol.toUpperCase();
  if (!/^[A-Z][A-Z0-9.]*$/.test(symbol)) return { kind: 'error', reason: USAGE };
  const book: Book = /^[A-Z]{4}$/.test(symbol) ? 'IDX' : 'US';
  // Plain decimals only: `Number` alone would also take `1e3`, `Infinity` or `0x10`.
  const stop = /^\d+(\.\d+)?$/.test(rawStop) ? Number(rawStop) : NaN;
  if (!(stop > 0)) return { kind: 'error', reason: `Stop must be a positive number, got ${rawStop}` };
  return { kind: 'declaration', book, symbol, stop, entry_date: dateIn(sentAt, EXCHANGE_TZ[book]) };
}

// The calendar date of `instant` in `timeZone`, as YYYY-MM-DD.
function dateIn(instant: Date, timeZone: string): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(
    instant,
  );
}

// Telegram dates are whole seconds; keep the wire timestamp that way.
function isoSeconds(instant: Date): string {
  return instant.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

const PRICE =new Intl.NumberFormat('en-US', { maximumFractionDigits: 6 });
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function describe(parse: Parse): string {
  if (parse.kind === 'error') return `✗ ${parse.reason}`;
  const [, month, day] = parse.entry_date.split('-').map(Number) as [number, number, number];
  return `✓ ${parse.book} ${parse.symbol} SL ${PRICE.format(parse.stop)}, entry ${day} ${MONTHS[month - 1]}`;
}
