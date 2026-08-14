"""The daily run's two book-scoped enrichment passes, gating and nags (#38).

SPEC §13.1/§13.3: one job, two book-scoped passes, each gating on whether that
book's prior trading day has actually closed rather than trusting the clock. The
job enriches but never commits a Trade; it also carries the nags (§11.4), which
surface as stated facts, never alarms.
"""

import os
import tempfile
import unittest

from journal import books, db
from journal.run import execute_run


def _seed_benchmark_bar(conn, book, date_):
    """A closed benchmark bar — the signal that the book's prior day closed."""
    conn.execute(
        "INSERT INTO bar (book, symbol, date, open, high, low, close, volume) "
        "VALUES (?, ?, ?, 1, 1, 1, 1, 100)",
        (book, books.BENCHMARKS[book], date_),
    )
    conn.commit()


def _seed_closed_trade(conn, book, symbol, entry_date, stop=None, setup=None):
    conn.execute(
        "INSERT INTO trade (book, symbol, entry_date, status, stop, setup) "
        "VALUES (?, ?, ?, 'closed', ?, ?)",
        (book, symbol, entry_date, stop, setup),
    )
    conn.commit()


class GatingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "journal.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_passes_gate_when_prior_close_absent(self):
        # No benchmark bar for either book: neither book's prior trading day has
        # closed as far as the job can see, so every enrichment pass gates off.
        conn = db.connect(self.db_path)
        result = execute_run(conn, as_of="2026-08-13")

        self.assertEqual(len(result.passes), len(books.BOOKS) * 3)
        self.assertTrue(all(p.status == "gated" for p in result.passes))

        rows = conn.execute("SELECT * FROM run_pass").fetchall()
        self.assertEqual(len(rows), len(books.BOOKS) * 3)
        self.assertTrue(all(r["status"] == "gated" for r in rows))
        conn.close()

    def test_passes_run_when_prior_close_present(self):
        conn = db.connect(self.db_path)
        # US's benchmark closed the day before; IDX's did not.
        _seed_benchmark_bar(conn, books.US, "2026-08-12")
        result = execute_run(conn, as_of="2026-08-13")

        by_book = {}
        for p in result.passes:
            by_book.setdefault(p.book, {})[p.name] = p.status
        self.assertEqual(
            set(by_book[books.US].values()), {"ran"}, by_book[books.US]
        )
        self.assertEqual(
            set(by_book[books.IDX].values()), {"gated"}, by_book[books.IDX]
        )
        conn.close()

    def test_run_never_commits_a_trade(self):
        conn = db.connect(self.db_path)
        _seed_benchmark_bar(conn, books.US, "2026-08-12")
        _seed_closed_trade(conn, books.US, "AAA", "2026-08-01")
        before = conn.execute("SELECT COUNT(*) c FROM trade").fetchone()["c"]

        execute_run(conn, as_of="2026-08-13")

        after = conn.execute("SELECT COUNT(*) c FROM trade").fetchone()["c"]
        self.assertEqual(before, after)
        conn.close()


class NagsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "journal.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_stop_and_setup_surface_as_nags(self):
        conn = db.connect(self.db_path)
        _seed_closed_trade(conn, books.US, "AAA", "2026-08-01")  # no stop, no setup
        result = execute_run(conn, as_of="2026-08-13")

        kinds = {n.kind for n in result.nags}
        self.assertIn("missing_stop", kinds)
        self.assertIn("missing_setup", kinds)

        rows = conn.execute(
            "SELECT * FROM run_nag WHERE kind = 'missing_stop'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["book"], books.US)
        conn.close()

    def test_idx_equity_and_intake_are_stated_facts_even_when_never(self):
        conn = db.connect(self.db_path)
        result = execute_run(conn, as_of="2026-08-13")

        kinds = {n.kind for n in result.nags}
        self.assertIn("idx_equity", kinds)
        self.assertIn("idx_intake", kinds)
        conn.close()

    def test_a_supplied_stop_and_setup_raise_no_nag(self):
        conn = db.connect(self.db_path)
        _seed_closed_trade(
            conn, books.US, "AAA", "2026-08-01", stop=10.0, setup="base_breakout"
        )
        result = execute_run(conn, as_of="2026-08-13")

        kinds = {(n.kind, n.book) for n in result.nags}
        self.assertNotIn(("missing_stop", books.US), kinds)
        self.assertNotIn(("missing_setup", books.US), kinds)
        conn.close()


if __name__ == "__main__":
    unittest.main()
