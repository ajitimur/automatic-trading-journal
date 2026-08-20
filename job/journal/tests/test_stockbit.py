"""The Stockbit TC parser and its fee-identity document gate (SPEC §4.2/§5.6, #26).

The Trade Confirmation is the IDX intake path because it preserves individual
fills (fills of one order share a ``REF #``). Two facts are load-bearing and
each has a test here:

- Shares are stored canonically from the ``Quantity`` column — no 100× inference
  from ``Lot`` — and fills of one order share their ``REF #`` (the order id).
- The printed per-side ``Total Cost`` is recomputed from the parsed rows using
  the fee identity (buy ``+0.15% + Rp10,000`` stamp; sell ``−0.15% − 0.10%``).
  A column shift that reads ``Lot`` as ``Quantity`` breaks the identity and
  **quarantines the whole document before a single fill lands.**
"""

import os
import tempfile
import unittest

from journal import db, fills, stockbit

SAMPLES = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "docs", "samples"
)
GOOD = os.path.join(SAMPLES, "stockbit-tc-fixture.txt")
SHIFTED = os.path.join(SAMPLES, "stockbit-tc-column-shift-fixture.txt")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class ParseTcTest(unittest.TestCase):
    def setUp(self):
        self.fills = stockbit.parse_tc_text(_read(GOOD))

    def test_one_fill_per_execution_row(self):
        self.assertEqual(len(self.fills), 9)

    def test_shares_are_canonical_from_the_quantity_column(self):
        # MEDC: the Quantity column reads 61,900 — never the Lot column's 619.
        medc = next(f for f in self.fills if f.symbol == "MEDC")
        self.assertEqual(medc.symbol, "MEDC")
        self.assertEqual(medc.side, "BUY")
        self.assertEqual(medc.quantity, 61900.0)
        self.assertEqual(medc.price, 1330.0)
        self.assertEqual(medc.book, "IDX")
        self.assertEqual(medc.source, "stockbit")
        self.assertEqual(medc.executed_at, "2026-08-11")

    def test_sells_are_signed_negative(self):
        arto = next(f for f in self.fills if f.symbol == "ARTO")
        self.assertEqual(arto.side, "SELL")
        self.assertEqual(arto.quantity, -25500.0)

    def test_fills_of_one_order_share_their_ref(self):
        # The three FUTR rows are one order (REF # 0477722), three fills.
        futr = [f for f in self.fills if f.symbol == "FUTR"]
        self.assertEqual(len(futr), 3)
        self.assertEqual({f.order_id for f in futr}, {"0477722"})
        # ...but each fill is its own ledger row (distinct content-hash ref).
        self.assertEqual(len({f.source_ref for f in futr}), 3)


class SourceRefTest(unittest.TestCase):
    def test_source_ref_is_a_deterministic_content_hash(self):
        a = stockbit.parse_tc_text(_read(GOOD))
        b = stockbit.parse_tc_text(_read(GOOD))
        self.assertEqual([f.source_ref for f in a], [f.source_ref for f in b])


class FeeGateTest(unittest.TestCase):
    def test_good_document_reconciles(self):
        # No exception: buy +0.15% +Rp10,000 and sell −0.15% −0.10% both tie back.
        stockbit.parse_tc_text(_read(GOOD))

    def test_column_shift_quarantines_the_whole_document(self):
        # Lot read as Quantity is a silent 100× error; the fee identity catches
        # it and quarantines the document rather than landing 100× quantities.
        with self.assertRaises(stockbit.QuarantineError):
            stockbit.parse_tc_text(_read(SHIFTED))


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM fill").fetchone()["n"]

    def test_dropping_a_tc_lands_one_fill_per_row(self):
        inserted = fills.import_stockbit_file(self.conn, GOOD)
        self.assertEqual(inserted, 9)
        self.assertEqual(self._count(), 9)

    def test_re_dropping_the_same_tc_is_idempotent(self):
        fills.import_stockbit_file(self.conn, GOOD)
        inserted = fills.import_stockbit_file(self.conn, GOOD)
        self.assertEqual(inserted, 0)
        self.assertEqual(self._count(), 9)

    def test_a_column_shift_lands_zero_fills(self):
        # The gate quarantines before any write: not a single fill commits.
        with self.assertRaises(stockbit.QuarantineError):
            fills.import_stockbit_file(self.conn, SHIFTED)
        self.assertEqual(self._count(), 0)

    def _raw_docs(self):
        return self.conn.execute(
            "SELECT book, kind, fetched_at FROM raw_document WHERE kind = 'stockbit-tc'"
        ).fetchall()

    # ── A drop records itself, so the intake nag has something to read (§11.4) ──
    def test_dropping_a_tc_records_it_in_the_keep_forever_tier(self):
        fills.import_stockbit_file(self.conn, GOOD, fetched_at="2026-08-18T02:00:00+00:00")
        (doc,) = self._raw_docs()
        self.assertEqual(doc["book"], "IDX")
        self.assertEqual(doc["fetched_at"], "2026-08-18T02:00:00+00:00")

    # ── Re-dropping the same TC does not invent a second intake ──
    def test_re_dropping_the_same_tc_records_one_document(self):
        fills.import_stockbit_file(self.conn, GOOD, fetched_at="2026-08-18T02:00:00+00:00")
        fills.import_stockbit_file(self.conn, GOOD, fetched_at="2026-09-01T02:00:00+00:00")
        (doc,) = self._raw_docs()
        # Dated to its first arrival: re-dropping August's TC in September does
        # not make the book's data current.
        self.assertEqual(doc["fetched_at"], "2026-08-18T02:00:00+00:00")

    # ── A quarantined drop still counts as intake: dropped, not forgotten ──
    def test_a_quarantined_drop_is_still_recorded(self):
        with self.assertRaises(stockbit.QuarantineError):
            fills.import_stockbit_file(self.conn, SHIFTED, fetched_at="2026-08-18T02:00:00+00:00")
        self.assertEqual(self._count(), 0)          # nothing landed in the ledger
        self.assertEqual(len(self._raw_docs()), 1)  # but the drop is on record


if __name__ == "__main__":
    unittest.main()
