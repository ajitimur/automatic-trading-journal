# Fills are append-only; Trades are derived and recomputable

Fills are the journal's source of truth and are never edited. Each is keyed by `(source, source_ref, revision)`; a broker restatement arrives as a new revision, with earlier revisions retained. Everything about a Trade except its two hand-entered fields is a pure function of its fills and the daily bars, and is recomputable at any time.

IBKR's Flex export supplies a native execution id for `source_ref`. Stockbit's trade confirmation is a PDF with no such id, so its `source_ref` is a deterministic hash over the confirmation date, symbol, side, quantity, price, and ordinal within the document — which makes re-dropping the same statement idempotent.

## Consequences

A Trade **freezes** 20 trading days after its final exit: hand-entered fields lock, and derived values are snapshotted. Derived values remain recomputable forever, so a later recomputation that disagrees with the snapshot is detectable as **drift** — surfaced in the confirm queue for acknowledgement rather than silently overwriting the record or silently discarding the correction.

This is the structure that lets the journal answer "why did this number change six months later," which is the entire point of freezing. It costs storage of superseded revisions and snapshots, which for a personal journal is negligible.
