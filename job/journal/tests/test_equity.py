"""EquitySnapshot — the risk/exposure denominator (SPEC §9, issue #31).

Two structurally different creation mechanisms with one storage shape:

* IBKR NAV is captured automatically — a second Flex query, one snapshot per
  ``reportDate`` row, ``total`` the denominator with ``cash``/``stock`` beside
  it, and the raw XML persisted to the keep-forever tier so the rolling-365
  window cannot take history with it.
* IDX is hand-typed — no SoA parser — with book-specific components
  (``portfolio``, ``ledger_balance``, ``cash_investor``) so switching the IDX
  denominator is a config change, not a re-read of PDFs.

Both write straight through, never the confirm queue (SPEC §9.7).
"""

import os
import tempfile
import unittest

from journal import db, equity

SAMPLES = os.path.join(os.path.dirname(__file__), "..", "..", "..", "docs", "samples")
NAV_FIXTURE = os.path.join(SAMPLES, "ibkr-nav-flex-schema-fixture.xml")


def _read_nav_fixture() -> str:
    with open(NAV_FIXTURE, encoding="utf-8") as fh:
        return fh.read()


class ParseNavFlexTest(unittest.TestCase):
    def setUp(self):
        self.snaps = equity.parse_nav_flex(_read_nav_fixture())

    def test_one_snapshot_per_report_date(self):
        self.assertEqual(len(self.snaps), 3)
        self.assertEqual([s.date for s in self.snaps],
                         ["2026-08-10", "2026-08-11", "2026-08-12"])

    def test_total_is_the_denominator_with_cash_and_stock_beside_it(self):
        s = self.snaps[0]
        self.assertEqual(s.book, "US")
        self.assertEqual(s.source, "ibkr")
        self.assertEqual(s.equity, 100000.00)   # total, not cash
        self.assertEqual(s.cash, 10000.00)
        self.assertEqual(s.stock, 90000.00)
        self.assertEqual(s.provenance, "stated")

    def test_total_is_not_reconstructed_from_cash_plus_stock(self):
        # The residual row: total carries accruals outside cash+stock (§9.2).
        s = self.snaps[1]
        self.assertEqual(s.equity, 101506.10)
        self.assertNotEqual(s.equity, s.cash + s.stock)

    def test_a_flex_error_body_is_rejected_not_read_as_empty(self):
        body = (
            '<FlexStatementResponse><Status>Fail</Status>'
            "<ErrorCode>1015</ErrorCode><ErrorMessage>Token is invalid.</ErrorMessage>"
            "</FlexStatementResponse>"
        )
        with self.assertRaises(equity.EquityError):
            equity.parse_nav_flex(body)


class StoreNavTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_backfill_lands_every_report_date_row(self):
        n = equity.import_nav_flex_text(self.conn, _read_nav_fixture(), fetch_date="2026-08-13")
        self.assertEqual(n, 3)
        rows = self.conn.execute(
            "SELECT date, equity FROM equity_snapshot WHERE book='US' ORDER BY date"
        ).fetchall()
        self.assertEqual([r["date"] for r in rows],
                         ["2026-08-10", "2026-08-11", "2026-08-12"])
        self.assertEqual(rows[2]["equity"], 102800.00)

    def test_the_nav_xml_is_persisted_to_the_keep_forever_tier(self):
        equity.import_nav_flex_text(self.conn, _read_nav_fixture(), fetch_date="2026-08-13")
        raw = self.conn.execute(
            "SELECT book, kind, content FROM raw_document WHERE kind='nav-flex-xml'"
        ).fetchone()
        self.assertIsNotNone(raw)
        self.assertEqual(raw["book"], "US")
        self.assertIn("EquitySummaryByReportDateInBase", raw["content"])
        # Every snapshot points back at the raw document it came from.
        ref = self.conn.execute(
            "SELECT raw_ref FROM equity_snapshot WHERE book='US' AND date='2026-08-10'"
        ).fetchone()["raw_ref"]
        self.assertIsNotNone(ref)

    def test_re_fetch_of_overlapping_window_is_idempotent(self):
        equity.import_nav_flex_text(self.conn, _read_nav_fixture(), fetch_date="2026-08-13")
        equity.import_nav_flex_text(self.conn, _read_nav_fixture(), fetch_date="2026-08-14")
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM equity_snapshot WHERE book='US'"
        ).fetchone()["n"]
        self.assertEqual(n, 3)


class IdxHandEntryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_equity_nab_is_portfolio_plus_ledger_balance(self):
        # Equity NAB = Portfolio + <ledger closing balance> (§9.2, exact).
        equity.record_idx_snapshot(
            self.conn, date="2026-07-31",
            portfolio=800.0, ledger_balance=200.0, cash_investor=50.0,
        )
        row = self.conn.execute(
            "SELECT * FROM equity_snapshot WHERE book='IDX' AND date='2026-07-31'"
        ).fetchone()
        self.assertEqual(row["equity"], 1000.0)
        self.assertEqual(row["portfolio"], 800.0)
        self.assertEqual(row["ledger_balance"], 200.0)
        self.assertEqual(row["cash_investor"], 50.0)
        self.assertEqual(row["source"], "idx")
        self.assertEqual(row["provenance"], "stated")

    def test_switching_the_denominator_is_a_config_change_over_stored_components(self):
        # Components are stored, so the deferred Cash Investor question is a
        # pure recomputation — no re-read of PDFs (§9.3).
        components = {"portfolio": 800.0, "ledger_balance": 200.0, "cash_investor": 50.0}
        self.assertEqual(equity.idx_equity(components, "equity_nab"), 1000.0)
        self.assertEqual(equity.idx_equity(components, "portfolio_plus_cash"), 1050.0)

    def test_estimated_provenance_is_recorded(self):
        equity.record_idx_snapshot(
            self.conn, date="2026-06-30",
            portfolio=800.0, ledger_balance=200.0, provenance="estimated",
        )
        row = self.conn.execute(
            "SELECT provenance FROM equity_snapshot WHERE book='IDX' AND date='2026-06-30'"
        ).fetchone()
        self.assertEqual(row["provenance"], "estimated")

    def test_a_month_end_series_can_be_entered_in_one_sitting(self):
        series = [
            {"date": "2026-07-31", "portfolio": 800.0, "ledger_balance": 200.0},
            {"date": "2026-06-30", "portfolio": 780.0, "ledger_balance": 190.0},
            {"date": "2026-05-31", "portfolio": 770.0, "ledger_balance": 180.0},
        ]
        n = equity.record_idx_series(self.conn, series)
        self.assertEqual(n, 3)
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM equity_snapshot WHERE book='IDX'"
        ).fetchone()["n"]
        self.assertEqual(count, 3)

    def test_snapshots_write_straight_through_not_the_confirm_queue(self):
        # A snapshot has no cohort, no FIFO, no exit — a plain write, visible at
        # once with nothing to confirm (§9.7).
        equity.record_idx_snapshot(
            self.conn, date="2026-07-31", portfolio=800.0, ledger_balance=200.0,
        )
        row = self.conn.execute(
            "SELECT equity FROM equity_snapshot WHERE book='IDX' AND date='2026-07-31'"
        ).fetchone()
        self.assertEqual(row["equity"], 1000.0)


if __name__ == "__main__":
    unittest.main()
