"""The daily run creates the store, writes a run record, and is idempotent."""

import os
import tempfile
import unittest

from journal import books, db
from journal.run import execute_run


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "sub", "journal.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_creates_file_and_writes_run_record(self):
        # File absent to start (and a missing parent dir).
        self.assertFalse(os.path.exists(self.db_path))

        conn = db.connect(self.db_path)
        result = execute_run(conn, as_of="2026-08-13")

        self.assertTrue(os.path.exists(self.db_path))
        self.assertEqual(result.status, "ok")

        # A run row exists with per-book rows carrying dates advanced.
        run_rows = conn.execute("SELECT * FROM run").fetchall()
        self.assertEqual(len(run_rows), 1)
        self.assertEqual(run_rows[0]["as_of_date"], "2026-08-13")
        self.assertIsNotNone(run_rows[0]["finished_at"])

        book_rows = {r["book"]: r for r in conn.execute("SELECT * FROM run_book")}
        self.assertEqual(set(book_rows), set(books.BOOKS))
        for book in books.BOOKS:
            row = book_rows[book]
            self.assertEqual(row["status"], "advanced")
            self.assertEqual(row["from_date"], None)  # first ever
            self.assertEqual(row["to_date"], "2026-08-13")
            self.assertGreater(row["days_advanced"], 0)
        conn.close()

    def test_second_run_is_a_noop(self):
        conn = db.connect(self.db_path)
        first = execute_run(conn, as_of="2026-08-13")
        self.assertEqual(first.status, "ok")

        second = execute_run(conn, as_of="2026-08-13")
        self.assertTrue(second.is_noop)
        self.assertEqual(second.status, "no-op")
        for outcome in second.books:
            self.assertEqual(outcome.status, "no-op")
            self.assertEqual(outcome.days_advanced, 0)

        # The cursor did not move between the two runs.
        cursors = {
            r["book"]: r["last_processed_trading_date"]
            for r in conn.execute("SELECT * FROM book_cursor")
        }
        self.assertEqual(cursors, {books.US: "2026-08-13", books.IDX: "2026-08-13"})
        conn.close()

    def test_run_advances_across_days(self):
        conn = db.connect(self.db_path)
        execute_run(conn, as_of="2026-08-13")
        later = execute_run(conn, as_of="2026-08-20")

        self.assertEqual(later.status, "ok")
        for outcome in later.books:
            self.assertEqual(outcome.status, "advanced")
            self.assertEqual(outcome.from_date, "2026-08-13")
            self.assertEqual(outcome.to_date, "2026-08-20")
            self.assertEqual(outcome.days_advanced, 7)
        conn.close()

    def test_first_run_at_the_floor_is_a_noop(self):
        conn = db.connect(self.db_path)
        result = execute_run(conn, as_of=books.BACKDATING_FLOOR)
        # Nothing to advance yet: floor == as-of.
        self.assertTrue(result.is_noop)
        conn.close()

    def test_successful_run_leaves_a_snapshot(self):
        # Every successful run leaves a timestamped VACUUM INTO snapshot under
        # rolling retention (SPEC §13.5, #39).
        conn = db.connect(self.db_path)
        result = execute_run(conn, as_of="2026-08-13")
        conn.close()

        self.assertIsNotNone(result.snapshot)
        self.assertTrue(os.path.exists(result.snapshot.path))
        snaps_dir = os.path.join(os.path.dirname(self.db_path), "snapshots")
        files = [n for n in os.listdir(snaps_dir) if n.endswith(".db")]
        self.assertEqual(len(files), 1)

    def test_snapshot_failure_does_not_sink_the_run(self):
        # The snapshot is best-effort: a run that advanced its books is still a
        # success even if the durability copy could not be written (§13.6 —
        # errors are data). Point the snapshots dir at a file to force failure.
        clash = os.path.join(self.tmp.name, "not-a-dir")
        with open(clash, "w") as fh:
            fh.write("x")
        os.environ["JOURNAL_SNAPSHOTS_DIR"] = clash
        try:
            conn = db.connect(self.db_path)
            result = execute_run(conn, as_of="2026-08-13")
            conn.close()
        finally:
            del os.environ["JOURNAL_SNAPSHOTS_DIR"]

        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.snapshot)
        self.assertIsNotNone(result.snapshot_error)


if __name__ == "__main__":
    unittest.main()
