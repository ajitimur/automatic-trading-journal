// The relay's durable store on Cloudflare D1 (SQLite). Typed against the
// handful of D1 calls it makes rather than the Workers type package, so the
// same code runs under `node --test` over node:sqlite.

import { keyOf, type RecentDeclaration, type RelayStore, type StoredItem } from './relay.ts';

export interface D1Statement {
  bind(...values: unknown[]): D1Statement;
  run(): Promise<unknown>;
  all<T>(): Promise<{ results: T[] }>;
}

export interface D1Database {
  prepare(sql: string): D1Statement;
}

// The tables come from relay/migrations, applied by
// `wrangler d1 migrations apply`.
export function d1Store(db: D1Database): RelayStore {
  return {
    async put(stored) {
      await db
        .prepare('INSERT OR IGNORE INTO items (id, stored_at, item) VALUES (?, ?, ?)')
        .bind(stored.item.id, stored.stored_at, JSON.stringify(stored.item))
        .run();
    },
    async list() {
      const { results } = await db
        .prepare('SELECT stored_at, item FROM items ORDER BY seq')
        .all<{ stored_at: string; item: string }>();
      return results.map((row): StoredItem => ({ stored_at: row.stored_at, item: JSON.parse(row.item) }));
    },
    async delete(ids) {
      if (ids.length === 0) return;
      await db
        .prepare('DELETE FROM items WHERE id IN (SELECT value FROM json_each(?))')
        .bind(JSON.stringify(ids))
        .run();
    },
    async recent() {
      const { results } = await db.prepare('SELECT entry FROM recent').all<{ entry: string }>();
      return results.map((row): RecentDeclaration => JSON.parse(row.entry));
    },
    async putRecent(declared) {
      await db
        .prepare('INSERT OR REPLACE INTO recent (key, id, entry) VALUES (?, ?, ?)')
        .bind(keyOf(declared.parse), declared.id, JSON.stringify(declared))
        .run();
    },
    async deleteRecent(ids) {
      if (ids.length === 0) return;
      await db
        .prepare('DELETE FROM recent WHERE id IN (SELECT value FROM json_each(?))')
        .bind(JSON.stringify(ids))
        .run();
    },
    async knownUsSymbols() {
      const { results } = await db.prepare('SELECT symbols FROM known_us').all<{ symbols: string }>();
      return results.length === 0 ? [] : JSON.parse(results[0]!.symbols);
    },
    async setKnownUsSymbols(symbols) {
      await db
        .prepare('INSERT OR REPLACE INTO known_us (only, symbols) VALUES (1, ?)')
        .bind(JSON.stringify(symbols))
        .run();
    },
  };
}
