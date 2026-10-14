-- The nags the Mac sent through POST /send, by Telegram message id, so a reply
-- to one or a tap on its button resolves to its Trade (src/d1Store.ts, #55).
-- `nag` is the SentNag as JSON. Kept for 7 days, like `recent`.
CREATE TABLE IF NOT EXISTS nags (
  message_id INTEGER PRIMARY KEY,
  nag TEXT NOT NULL
);
