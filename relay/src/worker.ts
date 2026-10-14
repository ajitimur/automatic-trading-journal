// The Cloudflare Worker entry point: binds the relay to D1, the Telegram Bot
// API and the Worker's secrets. No logic lives here — see relay.ts.

import { createRelay, type Relay, type TelegramSender } from './relay.ts';
import { d1Store, type D1Database } from './d1Store.ts';

export interface Env {
  DB: D1Database;
  TELEGRAM_BOT_TOKEN: string;
  WEBHOOK_SECRET: string;
  MAC_SECRET: string;
  TELEGRAM_USER_ID: string;
}

function telegramSender(token: string): TelegramSender {
  return {
    async sendMessage(chatId, text) {
      const res = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ chat_id: chatId, text }),
      });
      if (!res.ok) throw new Error(`Telegram sendMessage failed: ${res.status}`);
      const body = (await res.json()) as { result: { message_id: number } };
      return body.result.message_id;
    },
  };
}

function relayFor(env: Env): Relay {
  return createRelay({
    telegram: telegramSender(env.TELEGRAM_BOT_TOKEN),
    store: d1Store(env.DB),
    clock: () => new Date(),
    config: {
      webhookSecret: env.WEBHOOK_SECRET,
      macSecret: env.MAC_SECRET,
      userId: Number(env.TELEGRAM_USER_ID),
    },
  });
}

export default {
  fetch(request: Request, env: Env): Promise<Response> {
    return relayFor(env).fetch(request);
  },
  scheduled(_controller: unknown, env: Env): Promise<void> {
    return relayFor(env).scheduled();
  },
};
