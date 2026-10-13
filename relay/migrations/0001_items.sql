-- Items held until the Mac acknowledges them (src/d1Store.ts). `seq` is
-- arrival order; `id` is Telegram's update_id, unique so a retry stores once.
CREATE TABLE IF NOT EXISTS items (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  stored_at TEXT NOT NULL,
  item TEXT NOT NULL
);
