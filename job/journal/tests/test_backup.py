"""Durability (SPEC §13.5, issue #39): DB snapshots, the raw archive, restore.

Three tiers get exercised here:
  * the ``VACUUM INTO`` DB snapshot with rolling retention and an off-machine
    copy — the irreplaceable tier, the only home of hand-entered stops/setups;
  * the keep-forever raw archive, PII-bearing and therefore never in the repo;
  * a *rehearsed* restore — a backup that has not been restored is a belief.
"""

import os
import sqlite3
import tempfile
import unittest

from journal import backup, db
from journal.run import execute_run


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "journal.db")
        # Send the run's own auto-snapshot somewhere the manual tests won't see,
        # so retention/idempotency assertions count only what they write.
        os.environ["JOURNAL_SNAPSHOTS_DIR"] = os.path.join(self.tmp.name, "auto")
        self.conn = db.connect(self.db_path)
        execute_run(self.conn, as_of="2026-08-13")

    def tearDown(self):
        self.conn.close()
        del os.environ["JOURNAL_SNAPSHOTS_DIR"]
        self.tmp.cleanup()

    def test_snapshot_writes_a_vacuumed_copy_that_opens(self):
        snaps_dir = os.path.join(self.tmp.name, "snapshots")
        result = backup.snapshot_database(
            self.conn, snapshots_dir=snaps_dir, timestamp="20260813T060000"
        )
        # A timestamped file lands under the snapshots directory.
        self.assertTrue(os.path.exists(result.path))
        self.assertTrue(os.path.basename(result.path).startswith("journal-20260813T060000"))
        self.assertEqual(os.path.dirname(result.path), snaps_dir)

        # It is a real, independent SQLite database carrying the run record.
        snap = sqlite3.connect(result.path)
        n = snap.execute("SELECT COUNT(*) FROM run").fetchone()[0]
        snap.close()
        self.assertEqual(n, 1)

    def test_snapshot_is_idempotent_for_the_same_timestamp(self):
        snaps_dir = os.path.join(self.tmp.name, "snapshots")
        first = backup.snapshot_database(self.conn, snapshots_dir=snaps_dir, timestamp="20260813T060000")
        second = backup.snapshot_database(self.conn, snapshots_dir=snaps_dir, timestamp="20260813T060000")
        # VACUUM INTO refuses to overwrite; re-snapshotting the same instant
        # replaces in place rather than raising.
        self.assertEqual(first.path, second.path)
        self.assertEqual(len(_snapshot_files(snaps_dir)), 1)

    def test_rolling_retention_prunes_the_oldest(self):
        snaps_dir = os.path.join(self.tmp.name, "snapshots")
        stamps = [f"20260813T0600{i:02d}" for i in range(5)]
        for ts in stamps:
            backup.snapshot_database(
                self.conn, snapshots_dir=snaps_dir, timestamp=ts, retention=3
            )
        kept = sorted(_snapshot_files(snaps_dir))
        # Only the three newest survive; the two oldest are pruned.
        self.assertEqual(len(kept), 3)
        self.assertEqual(
            kept, [f"journal-{ts}.db" for ts in stamps[-3:]]
        )

    def test_offsite_copy_lands_a_second_copy(self):
        snaps_dir = os.path.join(self.tmp.name, "snapshots")
        offsite = os.path.join(self.tmp.name, "offsite")
        result = backup.snapshot_database(
            self.conn,
            snapshots_dir=snaps_dir,
            timestamp="20260813T060000",
            offsite_dir=offsite,
        )
        # At least one copy off this machine (SPEC §13.5): the off-site path
        # exists and is byte-identical to the local snapshot.
        self.assertIsNotNone(result.offsite_path)
        self.assertTrue(os.path.exists(result.offsite_path))
        with open(result.path, "rb") as a, open(result.offsite_path, "rb") as b:
            self.assertEqual(a.read(), b.read())


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive = os.path.join(self.tmp.name, "archive")

    def tearDown(self):
        self.tmp.cleanup()

    def test_archive_keeps_the_raw_document_verbatim(self):
        content = "<FlexQueryResponse>…</FlexQueryResponse>"
        path = backup.archive_raw(
            self.archive, book="US", kind="nav-flex-xml", content=content, ext="xml"
        )
        self.assertTrue(os.path.exists(path))
        # Grouped by kind so a parser fix can be re-run over one class of doc.
        self.assertIn("nav-flex-xml", path)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), content)

    def test_archive_is_content_addressed_and_idempotent(self):
        content = "raw bytes"
        first = backup.archive_raw(self.archive, book="US", kind="flex-xml", content=content, ext="xml")
        second = backup.archive_raw(self.archive, book="US", kind="flex-xml", content=content, ext="xml")
        # Same content → same file, dropped twice is a no-op (keep-forever, but
        # not keep-twice).
        self.assertEqual(first, second)

    def test_archive_accepts_bytes_for_a_pdf(self):
        pdf = b"%PDF-1.4 binary\x00bytes"
        path = backup.archive_raw(
            self.archive, book="IDX", kind="tc-pdf", content=pdf, ext="pdf"
        )
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), pdf)


class RestoreRehearsalTest(unittest.TestCase):
    def test_restore_into_scratch_and_verify_it_opens(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = os.path.join(tmp.name, "journal.db")
        conn = db.connect(db_path)
        execute_run(conn, as_of="2026-08-13")
        snaps_dir = os.path.join(tmp.name, "snapshots")
        snap = backup.snapshot_database(conn, snapshots_dir=snaps_dir, timestamp="20260813T060000")
        conn.close()

        scratch = os.path.join(tmp.name, "scratch")
        report = backup.rehearse_restore(snap.path, scratch)

        self.assertTrue(report.integrity_ok)
        self.assertTrue(report.verified)
        self.assertIn("run", report.tables_present)
        self.assertIn("trade", report.tables_present)
        self.assertEqual(report.run_count, 1)
        # The restored file is a distinct artefact under the scratch location.
        self.assertTrue(os.path.exists(report.restored_path))
        self.assertNotEqual(report.restored_path, snap.path)
        # A human-readable account of what was checked is produced.
        self.assertTrue(report.checks)

    def test_restore_flags_a_corrupt_snapshot(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bad = os.path.join(tmp.name, "journal-broken.db")
        with open(bad, "wb") as fh:
            fh.write(b"this is not a sqlite database at all")
        report = backup.rehearse_restore(bad, os.path.join(tmp.name, "scratch"))
        self.assertFalse(report.verified)


def _snapshot_files(snaps_dir):
    if not os.path.isdir(snaps_dir):
        return []
    return [n for n in os.listdir(snaps_dir) if n.startswith("journal-") and n.endswith(".db")]


if __name__ == "__main__":
    unittest.main()
