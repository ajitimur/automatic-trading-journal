// The Cloudflare Worker entry point: binds the relay to D1, the Telegram Bot
// API and the Worker's secrets. No logic lives here — see relay.ts.

import { createRelay, type Relay, type Telegram } from './relay.ts';
import { d1Store, type D1Database } from './d1Store.ts';

export interface Env {
  DB: D1Database;
  TELEGRAM_BOT_TOKEN: string;
  WEBHOOK_SECRET: string;
  MAC_SECRET: string;
  TELEGRAM_USER_ID: string;
}

function telegramApi(token: string): Telegram {
  return {
    async sendMessage(chatId, text) {
      const res = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ chat_id: chatId, text }),
      });
      if (!res.ok) throw new Error(`Telegram sendMessage failed: ${res.status}`);
    },
    // getFile names a path valid for an hour; the file itself is a plain GET.
    async downloadFile(fileId) {
      const meta = await fetch(`https://api.telegram.org/bot${token}/getFile?file_id=${encodeURIComponent(fileId)}`);
      if (!meta.ok) throw new Error(`Telegram getFile failed: ${meta.status}`);
      const { result } = (await meta.json()) as { result?: { file_path?: string } };
      if (!result?.file_path) throw new Error('Telegram getFile returned no file_path');
      const file = await fetch(`https://api.telegram.org/file/bot${token}/${result.file_path}`);
      if (!file.ok) throw new Error(`Telegram file download failed: ${file.status}`);
      return new Uint8Array(await file.arrayBuffer());
    },
  };
}

function relayFor(env: Env): Relay {
  return createRelay({
    telegram: telegramApi(env.TELEGRAM_BOT_TOKEN),
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
