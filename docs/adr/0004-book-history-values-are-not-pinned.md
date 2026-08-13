# Book-history values are computed at read time and are never pinned

Values that describe a Trade's place in its book's own history — what preceded it, how many were open beside it, how deep the book's drawdown was when it was entered — are **computed at read time from the current Trade set**. They are not stored on the Trade, not snapshotted at freeze, and a change in their value never fires **drift**.

This is a deliberate exception to the rule that entry-dated derived values are pinned, and it is narrow: it applies only to values whose every input is the journal's own records.

Pinning exists to defend against an **untrusted upstream**. Daily bars come from yfinance, which can restate a series silently, so entry-dated bar values are snapshotted and a later disagreement is surfaced as drift for acknowledgement. Book-history values have no such upstream. Their inputs are Fills and Trades — append-only, revision-keyed, and changed only by a deliberate act of the trader: importing a statement, correcting a fact, or entering a backdated Trade. There is nothing to defend against, so there is nothing to pin.

The case that forces the decision is backdating, which the capture flow makes first-class. Insert a Trade into the middle of a book's history and its neighbours' book-history values change: the Trade after it now has a different predecessor, a different concurrent count, a different position in the sequence. Under pinning, that would mutate an already-frozen Trade and fire drift on a record nothing was wrong with. Under read-time computation it simply reads correctly, because the trader genuinely did live through that Trade before the next one — the journal was merely late in hearing about it.

## Consequences

Drift keeps a single, honest meaning: **a fact from outside the journal moved**. It never fires because the journal learned more about its own past.

Freezing is unaffected. A frozen Trade's hand-entered fields still lock and its bar-derived values are still snapshotted; its book-history values were never part of that snapshot and continue to reflect the book as it is now known.

Ordering is a fact about the book, not about the row. A Trade's ordinal (`seq`) renumbers its successors when a backdated Trade lands ahead of them. Nothing is stored, so nothing has to be migrated.

The daily job gains no work. These values are produced by whatever reads them — in practice the LLM export, which is the only consumer.

Most of the class never becomes a field at all. Prior outcome, gap since the last close, loss streaks and concurrent open count are all recoverable from an export that already carries `seq`, `book`, `entry_date`, `exit_date` and `realized_r` on every row, so they are derived by the reader rather than computed and shipped. Only **Book Drawdown** is stored in the export, and only because it must be computed against the book's complete history, which a sliced export does not contain.
