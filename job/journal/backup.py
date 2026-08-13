"""Durability — DB snapshots, the raw archive, and a rehearsed restore (SPEC §13.5).

This module owns the three-tier durability policy and **discharges §14 item 1**,
the one gate the spec named on calling itself done: *a backup that has not been
restored is a belief, not a backup.*

The three tiers, and why each exists:

* **Journal DB** — a ``VACUUM INTO`` snapshot at the end of every successful run
  (:func:`snapshot_database`), timestamped, under rolling retention, with **at
  least one copy off this machine**. It is the *irreplaceable* tier: the only
  home of hand-entered stops, setups, confirmed exit reasons and frozen
  snapshots. No broker can reissue any of it. ``VACUUM INTO`` is used rather than
  a file copy because it takes a consistent snapshot of a live database without
  a read lock held over the whole run and defragments as it goes.

* **Raw source documents** — kept forever (:func:`archive_raw`): Flex XML as
  fetched (trades *and* NAV), TC and SoA PDFs as dropped. The tier worth
  defending — it makes the DB reconstructible from scratch and lets a parser fix
  be re-run over history. These documents carry PII (name, address, NPWP/NIK,
  phone, account number), so the archive stays **local or encrypted** and never
  enters a repo (the directory is git-ignored). Content-addressed, so re-dropping
  the same document is a no-op.

* **Restore** — :func:`rehearse_restore` restores a snapshot into a scratch
  location, opens the journal against it, checks integrity, and records what it
  verified. It is a real operation, not a design note.

Directory resolution goes through :func:`snapshots_dir_for`,
:func:`archive_dir_for` and :func:`offsite_dir`: env overrides win so a machine
can point the off-machine copy at a mounted or synced (ideally encrypted) volume,
and the defaults sit beside the DB so a fresh machine just works.
"""

from __future__ import annotations

import glob
import hashlib
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Union

# Keep this many local snapshots. Rolling retention (SPEC §13.5): a day's worth
# of runs times a couple of weeks is cheap on disk, and the off-site copy plus
# the keep-forever raw tier make deeper local history unnecessary.
DEFAULT_RETENTION = 30

# Env seams (SPEC §13.7): the store path already resolves through $JOURNAL_DB;
# these let the snapshot, archive and off-machine copy point wherever the host
# keeps a durable — ideally encrypted — volume, with a working default beside
# the DB when unset.
ENV_SNAPSHOTS = "JOURNAL_SNAPSHOTS_DIR"
ENV_ARCHIVE = "JOURNAL_ARCHIVE_DIR"
ENV_OFFSITE = "JOURNAL_OFFSITE_DIR"


@dataclass(frozen=True)
class SnapshotResult:
    """What one :func:`snapshot_database` call did."""

    path: str                       # the local snapshot just written
    offsite_path: Optional[str]     # the off-machine copy, or None if unconfigured
    retained: list[str]             # local snapshot filenames kept, newest last
    pruned: list[str]               # local snapshot filenames removed by retention


@dataclass(frozen=True)
class RestoreReport:
    """The written-down account of a rehearsed restore (SPEC §13.5, §14.1)."""

    snapshot_path: str
    restored_path: str
    integrity_ok: bool
    tables_present: list[str]
    run_count: int
    fill_count: int
    verified: bool
    checks: list[str]

    def render(self) -> str:
        """A human-readable transcript of what the rehearsal verified."""
        head = "RESTORE REHEARSAL — VERIFIED" if self.verified else "RESTORE REHEARSAL — FAILED"
        lines = [
            head,
            f"  snapshot : {self.snapshot_path}",
            f"  restored : {self.restored_path}",
        ]
        lines.extend(f"  - {c}" for c in self.checks)
        return "\n".join(lines)


# The tables a healthy journal must carry for a restore to count as verified.
# Not the full schema — a representative spine touching each tier: the run
# record, the Fill ledger, derived Trades, the equity denominator, and the
# keep-forever raw tier.
_EXPECTED_TABLES = ("run", "run_book", "fill", "trade", "equity_snapshot", "raw_document")


def snapshots_dir_for(db_path: str) -> str:
    """Where DB snapshots land: ``$JOURNAL_SNAPSHOTS_DIR`` or ``<db>/../snapshots``."""
    override = os.environ.get(ENV_SNAPSHOTS)
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "snapshots")


def archive_dir_for(db_path: str) -> str:
    """Where raw documents land: ``$JOURNAL_ARCHIVE_DIR`` or ``<db>/../archive``."""
    override = os.environ.get(ENV_ARCHIVE)
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "archive")


def offsite_dir() -> Optional[str]:
    """The off-machine copy target, or None when no off-site volume is configured.

    Off-site is a policy the host supplies (a mounted drive, a synced folder):
    the job cannot invent one, so this is opt-in via ``$JOURNAL_OFFSITE_DIR``.
    Unset means the snapshot is local-only and the acceptance criterion is not
    yet met — deliberately visible rather than silently faked.
    """
    return os.environ.get(ENV_OFFSITE) or None


def snapshot_timestamp(now: datetime) -> str:
    """A filesystem- and sort-safe compact stamp, e.g. ``20260813T060000-123456``.

    Microseconds are kept so two runs in the same second do not collide and both
    survive retention as distinct files.
    """
    return now.strftime("%Y%m%dT%H%M%S-%f")


def _db_path_of(conn: sqlite3.Connection) -> str:
    for _seq, name, filename in conn.execute("PRAGMA database_list"):
        if name == "main":
            return filename or ""
    return ""


def _sql_str(value: str) -> str:
    """Quote a path as a SQLite string literal (``VACUUM INTO`` takes no binds)."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def snapshot_database(
    conn: sqlite3.Connection,
    *,
    snapshots_dir: str,
    timestamp: str,
    retention: int = DEFAULT_RETENTION,
    offsite_dir: Optional[str] = None,
) -> SnapshotResult:
    """Take a ``VACUUM INTO`` snapshot, prune to ``retention``, copy off-machine.

    The snapshot is a consistent, defragmented copy of the live DB — safe to run
    at the tail of a successful run with the connection still open. The filename
    is ``journal-<timestamp>.db``; re-snapshotting the same instant replaces in
    place (``VACUUM INTO`` refuses to overwrite, so an existing target is removed
    first) rather than raising. After writing, the oldest files beyond
    ``retention`` are pruned, and — when an off-site directory is configured — a
    byte-identical copy is placed there too and pruned to the same depth.
    """
    os.makedirs(snapshots_dir, exist_ok=True)
    target = os.path.join(snapshots_dir, f"journal-{timestamp}.db")
    # VACUUM INTO will not overwrite; an idempotent re-snapshot clears the way.
    if os.path.exists(target):
        os.remove(target)
    conn.execute(f"VACUUM INTO {_sql_str(target)}")

    pruned = _prune(snapshots_dir, retention)

    offsite_path: Optional[str] = None
    if offsite_dir:
        os.makedirs(offsite_dir, exist_ok=True)
        offsite_path = os.path.join(offsite_dir, os.path.basename(target))
        shutil.copy2(target, offsite_path)
        _prune(offsite_dir, retention)

    retained = sorted(_list_snapshots(snapshots_dir))
    return SnapshotResult(
        path=target, offsite_path=offsite_path, retained=retained, pruned=pruned
    )


def _list_snapshots(directory: str) -> list[str]:
    if not os.path.isdir(directory):
        return []
    return [
        n
        for n in os.listdir(directory)
        if n.startswith("journal-") and n.endswith(".db")
    ]


def _prune(directory: str, retention: int) -> list[str]:
    """Delete all but the ``retention`` newest snapshots; return names removed.

    Ordering is by filename — the compact timestamp sorts chronologically — so
    no ``stat`` call is needed and clock skew on the filesystem cannot reorder.
    """
    names = sorted(_list_snapshots(directory))
    if retention <= 0 or len(names) <= retention:
        return []
    doomed = names[: len(names) - retention]
    for name in doomed:
        os.remove(os.path.join(directory, name))
    return doomed


def archive_raw(
    archive_dir: str,
    *,
    book: str,
    kind: str,
    content: Union[str, bytes],
    ext: str,
) -> str:
    """Keep a raw source document forever, content-addressed and git-ignored.

    Files are grouped ``<archive_dir>/<kind>/<book>-<sha256>.<ext>`` so a parser
    fix can be re-run over one class of document. The name is the SHA-256 of the
    bytes, which makes a re-drop of the identical document a no-op and dedupes
    the rolling-365 NAV window's overlapping fetches. Text is stored UTF-8;
    bytes (a PDF) are stored verbatim. The archive is PII-bearing — this writes
    only under ``archive_dir``, which never enters the repo (SPEC §13.5).
    """
    payload = content.encode("utf-8") if isinstance(content, str) else content
    digest = hashlib.sha256(payload).hexdigest()
    kind_dir = os.path.join(archive_dir, kind)
    os.makedirs(kind_dir, exist_ok=True)
    path = os.path.join(kind_dir, f"{book}-{digest}.{ext}")
    if not os.path.exists(path):
        with open(path, "wb") as fh:
            fh.write(payload)
    return path


def rehearse_restore(snapshot_path: str, scratch_dir: str) -> RestoreReport:
    """Restore a snapshot into a scratch location and verify the journal opens.

    The whole point of §14 item 1: not a design, a real operation. The snapshot
    is copied into ``scratch_dir`` (never opened in place, so the artefact under
    review stays untouched), then opened as a fresh SQLite connection and put
    through ``PRAGMA integrity_check`` plus a check that the expected tables are
    present and the run record reads back. The returned report — and
    :meth:`RestoreReport.render` — is the *written-down* account of what passed.
    """
    os.makedirs(scratch_dir, exist_ok=True)
    restored = os.path.join(scratch_dir, "restored.db")
    shutil.copy2(snapshot_path, restored)

    checks: list[str] = []
    integrity_ok = False
    tables_present: list[str] = []
    run_count = 0
    fill_count = 0

    try:
        conn = sqlite3.connect(restored)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            integrity_ok = bool(integrity) and integrity[0] == "ok"
            checks.append(
                f"integrity_check: {'ok' if integrity_ok else integrity and integrity[0]}"
            )

            tables_present = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            missing = [t for t in _EXPECTED_TABLES if t not in tables_present]
            checks.append(
                "schema: all expected tables present"
                if not missing
                else f"schema: MISSING {', '.join(missing)}"
            )

            run_count = conn.execute("SELECT COUNT(*) FROM run").fetchone()[0]
            fill_count = conn.execute("SELECT COUNT(*) FROM fill").fetchone()[0]
            checks.append(f"run rows: {run_count}; fill rows: {fill_count}")
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        checks.append(f"open FAILED: {exc}")
        missing = list(_EXPECTED_TABLES)

    verified = integrity_ok and not missing
    return RestoreReport(
        snapshot_path=snapshot_path,
        restored_path=restored,
        integrity_ok=integrity_ok,
        tables_present=tables_present,
        run_count=run_count,
        fill_count=fill_count,
        verified=verified,
        checks=checks,
    )
