# Deploy — the daily job under `launchd`

v1 runs on one machine under `launchd` (SPEC §13). The job is a **plain
idempotent CLI command** that `launchd` merely calls (§13.7 seam 1); any
scheduler on any host substitutes without touching the job.

## Install

1. Point the store and the one secret at real locations — both resolve through
   indirection, nothing is hardcoded in the job (§13.4, §13.7):

   ```sh
   export JOURNAL_DB="$HOME/Library/Application Support/automatic-trading-journal/journal.db"
   export JOURNAL_SECRET_IBKR_FLEX_TOKEN="…"   # scoped, rotatable; expires 2027-07-14
   ```

2. Copy the plist template, substituting `__REPO__`, `__DATA__` and the token:

   ```sh
   mkdir -p "$(dirname "$JOURNAL_DB")/logs"
   sed -e "s#__REPO__#$PWD#g" \
       -e "s#__DATA__#$(dirname "$JOURNAL_DB")#g" \
       deploy/launchd/com.automatic-trading-journal.daily.plist \
       > ~/Library/LaunchAgents/com.automatic-trading-journal.daily.plist
   launchctl load ~/Library/LaunchAgents/com.automatic-trading-journal.daily.plist
   ```

## Two logs, two failure modes

- **The run record in the DB** is the primary observability channel (§13.6):
  every run writes per-book status, dates advanced and errors. You see it on
  next open of the UI.
- **The plain `launchd` log file** (`StandardOutPath` / `StandardErrorPath`)
  exists for the case the run record cannot cover: **the app itself will not
  start**, so nothing was written to the DB. Tail it when a scheduled run
  produced no run record at all.

## Why `launchd`, not `cron`

A `StartCalendarInterval` job fires **on wake** if its window passed while the
machine slept, so a missed daily run self-heals before backfill is even reached
(§13.1). Because every run advances each book from its cursor to the present, a
missed day is not an error.
