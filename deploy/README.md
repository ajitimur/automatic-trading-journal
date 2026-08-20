# Deploy — the daily job under `launchd`

v1 runs on one machine under `launchd` (SPEC §13). The job is a **plain
idempotent CLI command** that `launchd` merely calls (§13.7 seam 1); any
scheduler on any host substitutes without touching the job.

## Install

0. Install the job's Python dependencies (yfinance, the one bar source behind
   the seam — §4.4): `pip install -r job/requirements.txt`.

1. Point the store and the one secret at real locations — both resolve through
   indirection, nothing is hardcoded in the job (§13.4, §13.7):

   ```sh
   export JOURNAL_DB="$HOME/Library/Application Support/automatic-trading-journal/journal.db"
   export JOURNAL_SECRET_IBKR_FLEX_TOKEN="…"   # scoped, rotatable; expires 2027-07-14
   # The saved Activity Flex Query the `intake` pass fetches unattended (§4.1).
   # Config, not a secret: it names a report, it does not authorise one. Leave it
   # unset and the pass states that it is skipped — it never fails the run.
   export JOURNAL_IBKR_FLEX_QUERY_ID="…"
   # Durability (SPEC §13.5). Snapshots and the raw archive default beside the
   # DB; point the off-machine copy at a durable, ideally encrypted, volume so
   # at least one DB copy lands off this machine. The archive is PII-bearing —
   # keep it local or encrypted, never a plain synced folder.
   export JOURNAL_OFFSITE_DIR="/Volumes/encrypted-backup/atj/snapshots"
   # export JOURNAL_SNAPSHOTS_DIR=…   # default: <db>/../snapshots
   # export JOURNAL_ARCHIVE_DIR=…     # default: <db>/../archive
   ```

2. Copy the plist template, substituting `__REPO__`, `__DATA__`, the token, the
   query id and the off-site directory. The `logs` directory must exist before
   the agent loads — `launchd` will not create `StandardOutPath` for you:

   ```sh
   mkdir -p "$(dirname "$JOURNAL_DB")/logs"
   sed -e "s#__REPO__#$PWD#g" \
       -e "s#__DATA__#$(dirname "$JOURNAL_DB")#g" \
       deploy/launchd/com.automatic-trading-journal.daily.plist \
       > ~/Library/LaunchAgents/com.automatic-trading-journal.daily.plist
   # Then fill the three __SET_AT_INSTALL__ values by hand. The token is
   # deliberately not substituted from the environment: piping a secret through
   # `sed` puts it in shell history.
   plutil -lint ~/Library/LaunchAgents/com.automatic-trading-journal.daily.plist
   launchctl load ~/Library/LaunchAgents/com.automatic-trading-journal.daily.plist
   ```

3. Confirm it is registered and will fire:

   ```sh
   launchctl list | grep automatic-trading-journal
   launchctl start com.automatic-trading-journal.daily   # run once, now
   ```

   A first run reports each pass per book. `intake: skipped` means the query id
   is unset; `bars: gated — no bar fetcher configured` means the interpreter has
   no `yfinance` (see step 0 — `job/bin/journal` prefers a `.venv` beside the
   repo, which is where the dependency usually lives).

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
