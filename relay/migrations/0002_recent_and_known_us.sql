-- The latest declaration said per (book, symbol, entry date), for replace and
-- /undo (src/d1Store.ts). Kept for 7 days, apart from `items`, so both still
-- work after the Mac has acknowledged the item. `id` is the declaration's item
-- id; `entry` is the RecentDeclaration as JSON.
CREATE TABLE IF NOT EXISTS recent (
  key TEXT PRIMARY KEY,
  id TEXT NOT NULL,
  entry TEXT NOT NULL
);

-- The symbols the Mac knows on the US book, as one JSON array in one row, so
-- each run's push replaces the list in a single statement.
CREATE TABLE IF NOT EXISTS known_us (
  only INTEGER PRIMARY KEY CHECK (only = 1),
  symbols TEXT NOT NULL
);
