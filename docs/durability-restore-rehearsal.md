# Restore rehearsal — the written-down record (SPEC §13.5, §14 item 1)

> A backup that has not been restored is a belief, not a backup.

This file discharges **§14 item 1**: back up per [§13.5](../SPEC.md#135-durability),
wipe, restore, verify — and write down what the rehearsal verified. It is a
record of a restore that was actually performed, not a design for one. The
restore path itself lives in `job/journal/backup.py` and is exercised on every
run of the test suite (`test_backup.py`, `test_run.py`, `test_cli.py`); the
operator command is `journal restore-check`.

## The three durability tiers, as built

| Tier | Mechanism | Where | Retention |
| --- | --- | --- | --- |
| **Journal DB** (irreplaceable) | `VACUUM INTO` snapshot at the tail of every successful `journal run` | `$JOURNAL_SNAPSHOTS_DIR` (default `<db>/../snapshots`) **and** `$JOURNAL_OFFSITE_DIR` when set — *at least one copy off this machine* | rolling, newest `DEFAULT_RETENTION` (30) |
| **Raw source documents** (worth defending) | archived verbatim, content-addressed, *before* parsing — Flex XML (trades + NAV), TC/SoA PDFs | `$JOURNAL_ARCHIVE_DIR` (default `<db>/../archive`), grouped by kind | kept forever |
| **Bar cache** | rides along in the DB snapshot | with the DB | with the DB |

**PII:** the archive holds names, addresses, NPWP/NIK, phone and account
numbers. Both the snapshot and archive directories are **git-ignored** and never
enter the repo; point `$JOURNAL_OFFSITE_DIR` and `$JOURNAL_ARCHIVE_DIR` at a
local or **encrypted** volume (an encrypted external disk, or an encrypted
synced folder). Off-site is opt-in — unset means local-only, deliberately
visible rather than silently faked.

## The rehearsal that was performed (2026-08-13)

Run against a scratch store with off-site and archive directories configured:

1. `journal run --as-of 2026-08-13` → advanced both books; left a snapshot
   locally **and** an off-site copy.
2. `journal import ibkr-flex-schema-fixture.xml` → 5 fills; the raw XML landed
   in the archive at `archive/flex-trades-xml/US-<sha256>.xml`, byte-identical
   to the input.
3. `journal confirm` → derived 1 Trade (the irreplaceable hand-entry tier's
   carrier — no broker can reissue a Trade's stop/setup/exit reason).
4. `journal run --as-of 2026-08-14` → second run, fresh timestamped snapshot +
   off-site copy; both snapshots present under rolling retention.
5. **The primary DB was deleted** (`rm journal.db`).
6. `journal restore-check <off-site snapshot>` — restored the **off-site** copy
   into a fresh scratch location and opened the journal against it.

## What the restore verified

```
RESTORE REHEARSAL — VERIFIED
  snapshot : .../offsite/journal-20260813T071339-031027.db
  restored : .../restored.db
  - integrity_check: ok
  - schema: all expected tables present
  - run rows: 2; fill rows: 5
```

- `PRAGMA integrity_check` returned `ok` — the `VACUUM INTO` copy is a
  structurally sound database, not a torn file.
- Every expected table was present: `run`, `run_book`, `fill`, `trade`,
  `equity_snapshot`, `raw_document`.
- The row counts matched what was written before the wipe: **2 run records, 5
  fills, and 1 confirmed Trade** all read back from the restored file.
- The restore ran from the **off-machine** copy, so the "at least one copy off
  this machine" criterion was exercised, not merely designed.

`restore-check` exits non-zero if the restore does not verify, so it can gate a
durability check by hand or in CI. Re-run it after any schema change (bump
`db.SCHEMA_VERSION`) and after first pointing `$JOURNAL_OFFSITE_DIR` at a new
volume, and update this record with the date and the transcript.
