-- Items held until the Mac acknowledges them. Mirrors SCHEMA in src/d1Store.ts.
CREATE TABLE IF NOT EXISTS items (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  stored_at TEXT NOT NULL,
  item TEXT NOT NULL
);
