"""The Fill ledger is append-only and idempotent (ADR 0003, issue #22).

Re-dropping the same file must not duplicate Fills (idempotent on
``(source, source_ref, revision)``), and a restated fill must land as a new
revision with the earlier one retained — never an edit.
"""

import os
import tempfile
import unittest

from journal import db, fills, flex

SAMPLES = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "docs", "samples"
)
FIXTURE = os.path.join(SAMPLES, "ibkr-flex-schema-fixture.xml")


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM fill").fetchone()["n"]

    def test_dropping_the_file_lands_one_fill_per_row(self):
        inserted = fills.import_flex_file(self.conn, FIXTURE)
        self.assertEqual(inserted, 5)
        self.assertEqual(self._count(), 5)

    def test_re_dropping_the_same_file_is_idempotent(self):
        fills.import_flex_file(self.conn, FIXTURE)
        inserted = fills.import_flex_file(self.conn, FIXTURE)
        self.assertEqual(inserted, 0)
        self.assertEqual(self._count(), 5)

    def test_a_restatement_is_a_new_revision_keeping_the_earlier(self):
        original = [
            flex.Fill(
                source="ibkr",
                source_ref="0000d7a7.6a180471.01",
                revision=1,
                book="US",
                symbol="SYM1",
                side="SELL",
                quantity=-100.0,
                price=20.0,
                commission=-0.72907365,
                executed_at="2026-04-01T09:30:00-04:00",
                order_id="5237074970",
            )
        ]
        fills.insert_fills(self.conn, original)

        restated = [
            flex.Fill(**{**original[0].__dict__, "revision": 2, "price": 20.5})
        ]
        inserted = fills.insert_fills(self.conn, restated)
        self.assertEqual(inserted, 1)

        rows = self.conn.execute(
            "SELECT revision, price FROM fill WHERE source_ref = ? ORDER BY revision",
            ("0000d7a7.6a180471.01",),
        ).fetchall()
        # Both revisions are present; the earlier one is untouched.
        self.assertEqual([(r["revision"], r["price"]) for r in rows], [(1, 20.0), (2, 20.5)])


if __name__ == "__main__":
    unittest.main()
