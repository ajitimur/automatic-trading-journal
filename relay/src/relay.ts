// The relay (ADR 0011): the one component off the Mac. It receives the bot's
// Telegram webhook, parses each Stop Declaration, replies at once with how it
// read it, and holds it until the Mac pulls and acknowledges it. A forwarded
// TC PDF is held the same way, as Telegram's reference to it, and its day is
// remembered for the evening reminder. It holds no broker credential and does
// no binding, gating or committing.
//
// Written as a plain module over injected dependencies — Telegram API,
// store, clock, configuration — so the Worker runtime is a thin adapter
// (worker.ts) and the tests run under `node --test` with fakes.

// One inline button under a message: its label, and the callback data a tap
// sends back.
export interface Button {
  text: string;
  data: string;
}

// The Bot API calls the relay makes. The bot token stays behind this, on the
// relay: the Mac sends and fetches a forwarded document through the relay.
export interface Telegram {
  // Resolves to the sent message's Telegram message_id. `buttons` go in one
  // row under the message.
  sendMessage(chatId: number, text: string, buttons?: Button[]): Promise<number>;
  // Stops the tapped button's spinner, showing `text` as a toast when given.
  answerCallback(callbackId: string, text?: string): Promise<void>;
  downloadFile(fileId: string): Promise<Uint8Array>;
}

export interface RelayConfig {
  // Telegram echoes this in X-Telegram-Bot-Api-Secret-Token on every webhook call.
  webhookSecret: string;
  // The Mac's bearer secret for pull, acknowledge and the known-US list.
  macSecret: string;
  // The trader's Telegram user ID; every other sender is dropped silently.
  userId: number;
}

export interface RelayDeps {
  telegram: Telegram;
  store: RelayStore;
  clock: () => Date;
  config: RelayConfig;
}

export interface Relay {
  fetch(request: Request): Promise<Response>;
  // The cron entry point, at 20:00 WIB: applies the TTLs and sends the TC
  // reminder.
  scheduled(): Promise<void>;
  // Whether a TC PDF was forwarded on IDX trading day `day` (YYYY-MM-DD,
  // Jakarta), for the evening reminder. Answers for the last 7 days only.
  pdfArrivedOn(day: string): Promise<boolean>;
}

export type Book = 'IDX' | 'US';

// The Mac's setup vocabulary (journal.stops.SETUP_VOCABULARY).
export type Setup = 'base_breakout' | 'high_tight_flag' | 'other';

const SETUP_CODES: Record<string, Setup> = { bb: 'base_breakout', htf: 'high_tight_flag', oth: 'other' };

// A Stop stated for a symbol on a Book on a date. `setup` is present only when
// the trader gave one.
export interface Declaration {
  kind: 'declaration';
  book: Book;
  symbol: string;
  stop: number;
  setup?: Setup;
  entry_date: string;
}

// `SYMBOL no sl`: a deliberate no-stop decision, bound as a Declined Stop.
export interface Decline {
  kind: 'decline';
  book: Book;
  symbol: string;
  setup?: Setup;
  entry_date: string;
}

// `/undo`, resolved by the relay to the declaration it withdraws.
export interface Undo {
  kind: 'undo';
  book: Book;
  symbol: string;
  entry_date: string;
  // The item id of the declaration it withdraws.
  withdraws: string;
}

// A forwarded Stockbit TC PDF. The relay holds only Telegram's reference to
// it; the Mac fetches the bytes through GET /documents/<id>.
export interface TcDocument {
  kind: 'document';
  book: 'IDX';
  file_id: string;
  file_name?: string;
  file_size?: number;
}

export type Declared = Declaration | Decline;

// The Trade a nag is about, as a declaration binds to it.
export interface TradeKey {
  book: Book;
  symbol: string;
  entry_date: string;
}

// A Stop a split-day nag offers as a one-tap answer (#55): the stop of the
// earlier day's Trade, entered on `entry_date`.
export interface Offer {
  stop: number;
  entry_date: string;
}

// A nag the Mac sent through POST /send, kept so that a reply to it, or a tap
// on its button, resolves to its Trade. Kept for RECENT_MS, like a declaration.
export interface SentNag {
  message_id: number;
  trade: TradeKey;
  offer?: Offer;
  sent_at: string;
  // The update id of the Yes tap that took the offer, so a second tap is not a
  // second declaration.
  taken_by?: string;
}

// The nag a pulled declaration answered, and how: a bare reply or a tap on Yes.
export interface Answers {
  nag: number;
  via: 'reply' | 'button';
}

type Parsed = Declared | { kind: 'undo' } | { kind: 'error'; reason: string };

// The bits of a Telegram Update the relay reads.
interface TelegramUpdate {
  update_id: number;
  message?: {
    from?: { id: number };
    chat: { id: number };
    date: number;
    text?: string;
    caption?: string;
    document?: { file_id: string; file_name?: string; mime_type?: string; file_size?: number };
    reply_to_message?: { message_id: number };
  };
  // A tap on an inline button.
  callback_query?: {
    id: string;
    from: { id: number };
    message?: { message_id: number };
    data?: string;
  };
}

// One entry of the pull payload — the contract with the Mac, pinned by
// docs/samples/relay-pull-fixture.json.
export interface PullItem {
  // Telegram's update_id: stable across webhook retries.
  id: string;
  // When Telegram received the message, UTC.
  telegram_at: string;
  // The message text, or a document's caption ('' when it has none).
  text: string;
  parse: Declared | Undo | TcDocument;
  // Present when the declaration answered a nag rather than naming its symbol.
  answers?: Answers;
}

export interface StoredItem {
  item: PullItem;
  // The relay's clock when the item was first stored; the TTL counts from it.
  stored_at: string;
}

// The latest declaration said for one (book, symbol, entry date). Kept apart
// from the items so a replace and /undo still work after the Mac has pulled and
// acknowledged it, and only for RECENT_MS, so the relay never becomes a second
// copy of the trading history.
export interface RecentDeclaration {
  // The item id of the declaration.
  id: string;
  telegram_at: string;
  parse: Declared;
  // What it replaced, so a retried webhook replies exactly as the first did.
  replaced?: Declared;
  // The /undo item that withdrew it.
  withdrawn_by?: string;
}

// Holds items until acknowledged. `put` ignores an id it already holds, so a
// retried webhook stores once. `list` returns arrival order. `putRecent`
// overwrites the entry for the same (book, symbol, entry date).
export interface RelayStore {
  put(stored: StoredItem): Promise<void>;
  list(): Promise<StoredItem[]>;
  delete(ids: string[]): Promise<void>;
  recent(): Promise<RecentDeclaration[]>;
  putRecent(declared: RecentDeclaration): Promise<void>;
  deleteRecent(ids: string[]): Promise<void>;
  knownUsSymbols(): Promise<string[]>;
  setKnownUsSymbols(symbols: string[]): Promise<void>;
  // The Jakarta dates a TC PDF was forwarded on. Adding one already held is a
  // no-op.
  pdfDays(): Promise<string[]>;
  putPdfDay(day: string): Promise<void>;
  deletePdfDays(days: string[]): Promise<void>;
  // `putNag` overwrites the nag with the same message id.
  nags(): Promise<SentNag[]>;
  putNag(nag: SentNag): Promise<void>;
  deleteNags(messageIds: number[]): Promise<void>;
}

// The key a declaration binds on, and replaces on.
export function keyOf(parse: { book: Book; symbol: string; entry_date: string }): string {
  return `${parse.book} ${parse.symbol} ${parse.entry_date}`;
}

// Each Book's exchange time zone: the entry date is the calendar date there
// when the message was sent, not the trader's local date.
const EXCHANGE_TZ: Record<Book, string> = {
  IDX: 'Asia/Jakarta',
  US: 'America/New_York',
};

const DAY_MS = 24 * 60 * 60 * 1000;
const TTL_MS = 30 * DAY_MS;
// Long enough to cover the 3-trading-day lapse window across a weekend or a
// holiday, after which no replace or /undo can matter.
const RECENT_MS = 7 * DAY_MS;

const USAGE = 'Expected: [idx|us] SYMBOL STOP [bb|htf|oth], SYMBOL no sl, or /undo. E.g. BBRI 4120 htf';

// The split-day nag's one-tap answer and its way out (#55).
const YES = 'yes';
const OTHER = 'other';
const OFFER_BUTTONS: Button[] = [
  { text: 'Yes', data: YES },
  { text: 'Other', data: OTHER },
];

export function createRelay(deps: RelayDeps): Relay {
  const { telegram, store, clock, config } = deps;

  // Always 200 once authenticated, except when storing or replying fails:
  // Telegram retries anything else, and a body it sent once it will send again.
  async function onWebhook(request: Request): Promise<Response> {
    const ok = new Response(null, { status: 200 });
    const update = (await readJson(request)) as TelegramUpdate | null;
    const callback = update?.callback_query;
    if (update && callback) {
      if (callback.from?.id === config.userId) await onTap(String(update.update_id), callback);
      return ok;
    }
    const message = update?.message;
    if (!update || !message || message.from?.id !== config.userId) return ok;
    const sentAt = new Date(message.date * 1000);
    const id = String(update.update_id);
    const telegramAt = isoSeconds(sentAt);
    const holdWithText = (text: string) => (parse: PullItem['parse']) =>
      store.put({ item: { id, telegram_at: telegramAt, text, parse }, stored_at: clock().toISOString() });

    if (message.document) {
      const doc = message.document;
      let reply = '✗ Only Stockbit TC PDFs are accepted';
      if (isPdf(doc)) {
        const { file_name, file_size } = doc;
        await holdWithText(message.caption ?? '')({ kind: 'document', book: 'IDX', file_id: doc.file_id, file_name, file_size });
        // Kept apart from the item, which goes once the Mac acknowledges it.
        await store.putPdfDay(dateIn(sentAt, EXCHANGE_TZ.IDX));
        reply = `✓ Got ${file_name ?? 'the PDF'}; it goes in on the next run`;
      }
      await telegram.sendMessage(message.chat.id, reply);
      return ok;
    }
    if (typeof message.text !== 'string') return ok;
    const text = message.text;
    const repliedTo = message.reply_to_message?.message_id;
    const nag = repliedTo === undefined ? undefined : await findNag(repliedTo);
    // A bare stop takes its Trade from the nag it answers (#55); anywhere else
    // it names no symbol, and is the grammar error.
    const bare = isBare(text);
    const answers: Answers | undefined = bare && nag ? { nag: nag.message_id, via: 'reply' } : undefined;

    const parse: Parsed = !bare
      ? parseMessage(text, sentAt, new Set(await store.knownUsSymbols()))
      : nag
        ? parseStop(text.trim().split(/\s+/), nag.trade)
        : { kind: 'error', reason: USAGE };
    let reply: string;
    if (parse.kind === 'error') {
      reply = `✗ ${parse.reason}`;
    } else if (parse.kind === 'undo') {
      const withdrawn = await withdrawLast(id);
      if (withdrawn === null) {
        reply = '✗ Nothing to undo';
      } else {
        const { book, symbol, entry_date } = withdrawn.parse;
        await holdWithText(text)({ kind: 'undo', book, symbol, entry_date, withdraws: withdrawn.id });
        reply = `↩ Withdrew ${describe(withdrawn.parse)}`;
      }
    } else {
      reply = await declare({ id, telegram_at: telegramAt, parse }, text, answers);
    }
    await telegram.sendMessage(message.chat.id, reply);
    return ok;
  }

  // Records a declaration as the latest for its key, holds it for the Mac as
  // said in `text` (answering a nag, when `answers` says so), and returns the
  // reply echoing how it was read.
  async function declare(declared: RecentDeclaration, text: string, answers?: Answers): Promise<string> {
    const replaced = await remember(declared);
    const { id, telegram_at, parse } = declared;
    await store.put({
      item: { id, telegram_at, text, parse, ...(answers ? { answers } : {}) },
      stored_at: clock().toISOString(),
    });
    return `✓ ${describe(parse)}${replaced ? ` (replaced ${value(replaced)} → ${value(parse)})` : ''}`;
  }

  // A tap on a split-day nag's button (#55). Yes declares the offered Stop for
  // the nag's Trade, once however often it is tapped; Other points at the
  // reply. A tap carries no send time, so the relay's clock dates it.
  async function onTap(id: string, callback: NonNullable<TelegramUpdate['callback_query']>): Promise<void> {
    const messageId = callback.message?.message_id;
    const nag = messageId === undefined ? undefined : await findNag(messageId);
    if (!nag) {
      await telegram.answerCallback(callback.id, 'This question has expired: send SYMBOL STOP instead');
      return;
    }
    if (callback.data !== YES || !nag.offer) {
      await telegram.answerCallback(callback.id, 'Reply to this message with the stop, e.g. 4120, or no sl');
      return;
    }
    if (nag.taken_by !== undefined && nag.taken_by !== id) {
      await telegram.answerCallback(callback.id, 'Already answered');
      return;
    }
    const telegramAt = isoSeconds(clock());
    const parse: Declaration = { kind: 'declaration', ...nag.trade, stop: nag.offer.stop };
    await store.putNag({ ...nag, taken_by: id });
    const reply = await declare({ id, telegram_at: telegramAt, parse }, 'Yes', { nag: nag.message_id, via: 'button' });
    await telegram.answerCallback(callback.id);
    await telegram.sendMessage(config.userId, reply);
  }

  // The nag sent as Telegram message `messageId`, if sent within RECENT_MS.
  async function findNag(messageId: number): Promise<SentNag | undefined> {
    const cutoff = clock().getTime() - RECENT_MS;
    return (await store.nags()).find((n) => n.message_id === messageId && Date.parse(n.sent_at) >= cutoff);
  }

  // The declarations said within RECENT_MS of the clock.
  async function recent(): Promise<RecentDeclaration[]> {
    const cutoff = clock().getTime() - RECENT_MS;
    return (await store.recent()).filter((r) => Date.parse(r.telegram_at) >= cutoff);
  }

  // Records `declared` as the latest for its key and returns what it replaced:
  // the key's previous declaration, unless that was withdrawn. A retried
  // webhook finds itself already recorded and returns what it replaced then.
  async function remember(declared: RecentDeclaration): Promise<Declared | undefined> {
    const previous = (await recent()).find((r) => keyOf(r.parse) === keyOf(declared.parse));
    if (previous?.id === declared.id) return previous.replaced;
    // An older message arriving late (a retry after a newer one landed) never
    // displaces the newer; the Mac orders by when each was said, too.
    if (previous && bySaid(previous, declared) > 0) return undefined;
    const replaced = previous && !previous.withdrawn_by ? previous.parse : undefined;
    await store.putRecent(replaced ? { ...declared, replaced } : declared);
    return replaced;
  }

  // Withdraws the most recently said declaration still standing and returns
  // it, or null when there is none. A retried /undo gets back the one it
  // withdrew the first time rather than taking another.
  async function withdrawLast(undoId: string): Promise<RecentDeclaration | null> {
    const all = await recent();
    const already = all.find((r) => r.withdrawn_by === undoId);
    if (already) return already;
    const last = all.filter((r) => !r.withdrawn_by).sort(bySaid).at(-1);
    if (!last) return null;
    await store.putRecent({ ...last, withdrawn_by: undoId });
    return last;
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

  async function purgeRecent(): Promise<void> {
    const cutoff = clock().getTime() - RECENT_MS;
    const stale = (await store.recent()).filter((r) => Date.parse(r.telegram_at) < cutoff);
    if (stale.length > 0) await store.deleteRecent(stale.map((r) => r.id));
    const staleNags = (await store.nags()).filter((n) => Date.parse(n.sent_at) < cutoff);
    if (staleNags.length > 0) await store.deleteNags(staleNags.map((n) => n.message_id));
  }

  // A PDF day goes once it is more than RECENT_MS old in Jakarta days, long
  // after the reminder for it could fire.
  async function purgePdfDays(): Promise<void> {
    const cutoff = dateIn(new Date(clock().getTime() - RECENT_MS), EXCHANGE_TZ.IDX);
    const stale = (await store.pdfDays()).filter((day) => day < cutoff);
    if (stale.length > 0) await store.deletePdfDays(stale);
  }

  // The cron fires at 20:00 WIB, after the TC has normally landed (17:50 to
  // 19:35 WIB). On a Jakarta day with IDX declarations still standing (Declined
  // Stops included: the trade happened all the same) and no forwarded PDF, the
  // trader gets one reminder naming each declared symbol. A day the trader
  // declared IDX trades is taken as an IDX trading day.
  async function remindTc(): Promise<void> {
    const today = dateIn(clock(), EXCHANGE_TZ.IDX);
    if (await pdfArrivedOn(today)) return;
    const symbols = (await recent())
      .filter((r) => r.parse.book === 'IDX' && r.parse.entry_date === today && !r.withdrawn_by)
      .sort(bySaid)
      .map((r) => r.parse.symbol);
    if (symbols.length === 0) return;
    await telegram.sendMessage(config.userId, `📄 Forward today's Stockbit TC (${symbols.join(', ')})`);
  }

  async function pdfArrivedOn(day: string): Promise<boolean> {
    return (await store.pdfDays()).includes(day);
  }

  async function onPull(): Promise<Response> {
    const items = (await purgeExpired()).map((stored) => stored.item);
    return Response.json({ items });
  }

  // A held document's bytes, fetched from Telegram on demand and sent as
  // base64 in JSON so the Mac's text-only transport carries them unchanged.
  // Only while the item is held: once acknowledged or expired it is a 404.
  async function onDocument(id: string): Promise<Response> {
    const held = (await purgeExpired()).find((stored) => stored.item.id === id)?.item.parse;
    if (held?.kind !== 'document') return new Response(null, { status: 404 });
    let bytes: Uint8Array;
    try {
      bytes = await telegram.downloadFile(held.file_id);
    } catch {
      return new Response(null, { status: 502 });
    }
    return Response.json({ id, content_base64: base64(bytes) });
  }

  // Deleting an id the store no longer holds is a no-op, so a repeated
  // acknowledge after a network blip is harmless.
  async function onAcknowledge(request: Request): Promise<Response> {
    const body = (await readJson(request)) as { ids?: unknown } | null;
    const ids = Array.isArray(body?.ids) ? body.ids.filter((id): id is string => typeof id === 'string') : null;
    if (ids === null) return new Response(null, { status: 400 });
    await store.delete(ids);
    return new Response(null, { status: 204 });
  }

  // Each run, the Mac replaces the list of symbols it knows on the US book, so
  // a 4-letter symbol on it reads as US rather than IDX.
  async function onKnownUs(request: Request): Promise<Response> {
    const body = (await readJson(request)) as { symbols?: unknown } | null;
    const symbols = body?.symbols;
    if (!Array.isArray(symbols) || !symbols.every((s): s is string => typeof s === 'string')) {
      return new Response(null, { status: 400 });
    }
    await store.setKnownUsSymbols(symbols.map((s) => s.toUpperCase()));
    return new Response(null, { status: 204 });
  }

  // The Mac's outbound channel (#53): the journal messages the trader through
  // here, so the bot token stays on the Worker. A private chat's id is the
  // user's id, so the only recipient is the trader. A Telegram failure throws,
  // and the Mac reads the 5xx as a failed send.
  //
  // A message about one Trade names it as `trade`, and the relay remembers it
  // as a nag, so a bare reply to it resolves to that Trade (#55). An `offer`
  // adds the one-tap Yes / Other buttons.
  async function onSend(request: Request): Promise<Response> {
    const body = await readJson(request);
    const text = body?.text;
    const bad = new Response(null, { status: 400 });
    if (typeof text !== 'string' || text === '') return bad;
    const trade = body?.trade === undefined ? undefined : tradeKeyOf(body.trade);
    const offer = body?.offer === undefined ? undefined : offerOf(body.offer);
    if (trade === null || offer === null || (offer && !trade)) return bad;
    const messageId = await telegram.sendMessage(config.userId, text, offer ? OFFER_BUTTONS : undefined);
    if (trade) {
      await store.putNag({ message_id: messageId, trade, ...(offer ? { offer } : {}), sent_at: clock().toISOString() });
    }
    return Response.json({ message_id: messageId });
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
      if (route === 'PUT /known-us-symbols') return fromMac(request) ? onKnownUs(request) : unauthorized();
      if (route === 'POST /send') return fromMac(request) ? onSend(request) : unauthorized();
      const document = /^GET \/documents\/([^/]+)$/.exec(route);
      if (document) return fromMac(request) ? onDocument(decodeURIComponent(document[1]!)) : unauthorized();
      return new Response(null, { status: 404 });
    },
    // The reminder goes first: a failed cron isn't retried, and a purge
    // that throws can wait for tomorrow's run where the reminder can't.
    async scheduled() {
      await remindTc();
      await purgeExpired();
      await purgeRecent();
      await purgePdfDays();
    },
    pdfArrivedOn,
  };
}

// The body as a JSON object, or null when it is anything else.
async function readJson(request: Request): Promise<Record<string, unknown> | null> {
  try {
    const body: unknown = await request.json();
    return typeof body === 'object' && body !== null && !Array.isArray(body) ? (body as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

// A Trade key from the Mac's /send body, or null when it is not one.
function tradeKeyOf(raw: unknown): TradeKey | null {
  const t = raw as Partial<Record<keyof TradeKey, unknown>> | null;
  if (typeof t !== 'object' || t === null) return null;
  const { book, symbol, entry_date } = t;
  if (book !== 'IDX' && book !== 'US') return null;
  if (typeof symbol !== 'string' || symbol === '') return null;
  if (typeof entry_date !== 'string' || !ISO_DATE.test(entry_date)) return null;
  return { book, symbol, entry_date };
}

// An offered Stop from the Mac's /send body, or null when it is not one.
function offerOf(raw: unknown): Offer | null {
  const o = raw as Partial<Record<keyof Offer, unknown>> | null;
  if (typeof o !== 'object' || o === null) return null;
  const { stop, entry_date } = o;
  if (typeof stop !== 'number' || !(stop > 0) || !Number.isFinite(stop)) return null;
  if (typeof entry_date !== 'string' || !ISO_DATE.test(entry_date)) return null;
  return { stop, entry_date };
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

// `[idx|us] SYMBOL STOP [SETUP]`, `[idx|us] SYMBOL no sl [SETUP]`, or `/undo`.
// An explicit prefix wins; otherwise a 4-letter symbol is IDX unless the Mac
// knows it on the US book, and anything else is US.
function parseMessage(text: string, sentAt: Date, knownUs: Set<string>): Parsed {
  const words = text.trim().split(/\s+/);
  if (words.length === 1 && words[0]!.toLowerCase() === '/undo') return { kind: 'undo' };
  const prefix = words[0]!.toLowerCase();
  const explicit: Book | null = prefix === 'idx' ? 'IDX' : prefix === 'us' ? 'US' : null;
  if (explicit) words.shift();

  const [rawSymbol = '', ...rest] = words;
  const symbol = rawSymbol.toUpperCase();
  if (!/^[A-Z][A-Z0-9.]*$/.test(symbol) || rest.length === 0) return { kind: 'error', reason: USAGE };
  const book: Book = explicit ?? (/^[A-Z]{4}$/.test(symbol) && !knownUs.has(symbol) ? 'IDX' : 'US');
  return parseStop(rest, { book, symbol, entry_date: dateIn(sentAt, EXCHANGE_TZ[book]) });
}

// A bare stop or decline, `STOP [SETUP]` or `no sl [SETUP]`, with no symbol:
// how a reply to a nag reads (#55).
function isBare(text: string): boolean {
  const [first = '', second = ''] = text.trim().toLowerCase().split(/\s+/);
  return /^\d/.test(first) || (first === 'no' && second === 'sl');
}

// `STOP [SETUP]` or `no sl [SETUP]`, the words after the symbol, for `trade`.
function parseStop(rest: string[], trade: TradeKey): Parsed {
  const { book, symbol, entry_date } = trade;
  const declined = rest[0]!.toLowerCase() === 'no';
  if (declined && rest[1]?.toLowerCase() !== 'sl') return { kind: 'error', reason: USAGE };
  const setupWords = rest.slice(declined ? 2 : 1);
  if (setupWords.length > 1) return { kind: 'error', reason: USAGE };
  const code = setupWords[0];
  const setup = code === undefined ? undefined : SETUP_CODES[code.toLowerCase()];
  if (code !== undefined && setup === undefined) {
    return { kind: 'error', reason: `Setup must be bb, htf or oth, got ${code}` };
  }
  const withSetup = setup ? { setup } : {};

  if (declined) return { kind: 'decline', book, symbol, ...withSetup, entry_date };

  const rawStop = rest[0]!;
  // Plain decimals only: `Number` alone would also take `1e3`, `Infinity` or `0x10`.
  const stop = /^\d+(\.\d+)?$/.test(rawStop) ? Number(rawStop) : NaN;
  if (!(stop > 0)) return { kind: 'error', reason: `Stop must be a positive number, got ${rawStop}` };
  // IDX trades in whole rupiah, and `4.120` there means 4,120, not 4.12.
  if (book === 'IDX' && !Number.isInteger(stop)) {
    return { kind: 'error', reason: `IDX stop must be whole rupiah, got ${rawStop}` };
  }
  return { kind: 'declaration', book, symbol, stop, ...withSetup, entry_date };
}

// The Stockbit TC is a PDF; the Mac's parse path takes nothing else. The name
// covers a client that sends no MIME type.
function isPdf(doc: { file_name?: string; mime_type?: string }): boolean {
  return doc.mime_type === 'application/pdf' || /\.pdf$/i.test(doc.file_name ?? '');
}

// Oldest said first; Telegram's update ids break a tie within one second.
function bySaid(a: RecentDeclaration, b: RecentDeclaration): number {
  return a.telegram_at.localeCompare(b.telegram_at) || Number(a.id) - Number(b.id);
}

// The calendar date of `instant` in `timeZone`, as YYYY-MM-DD.
function dateIn(instant: Date, timeZone: string): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(
    instant,
  );
}

// btoa over a binary string, in chunks so a large file never overflows the
// argument list. Workers have btoa but no Buffer.
function base64(bytes: Uint8Array): string {
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

// Telegram dates are whole seconds; keep the wire timestamp that way.
function isoSeconds(instant: Date): string {
  return instant.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

const PRICE = new Intl.NumberFormat('en-US', { maximumFractionDigits: 6 });
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const SETUP_NAMES: Record<Setup, string> = {
  base_breakout: 'base breakout',
  high_tight_flag: 'high tight flag',
  other: 'other',
};
const SETUP_CODE_OF = Object.fromEntries(
  Object.entries(SETUP_CODES).map(([code, setup]) => [setup, code]),
) as Record<Setup, string>;

// How the relay read a declaration: book, symbol, stop, setup and entry date.
function describe(parse: Declared): string {
  const [, month, day] = parse.entry_date.split('-').map(Number) as [number, number, number];
  const stop = parse.kind === 'decline' ? 'no SL (declined)' : `SL ${PRICE.format(parse.stop)}`;
  const setup = parse.setup ? `, ${SETUP_NAMES[parse.setup]}` : '';
  return `${parse.book} ${parse.symbol} ${stop}${setup}, entry ${day} ${MONTHS[month - 1]}`;
}

// The short form of what a declaration says, for "replaced 412 → 4,120".
function value(parse: Declared): string {
  const stop = parse.kind === 'decline' ? 'no SL' : PRICE.format(parse.stop);
  return parse.setup ? `${stop} ${SETUP_CODE_OF[parse.setup]}` : stop;
}
