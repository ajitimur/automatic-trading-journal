"""Risk % and Exposure %: the staleness bound and provenance tiers (SPEC §9.3/§9.4, #32).

One denominator asked two questions. Both read the most recent ``EquitySnapshot``
at or before the Trade's entry date, under one lookup rule, one calendar-day
staleness bound (IBKR 7, IDX 45), one null-with-marker past the bound, and one
exclusion-with-count — for Risk % *and* Exposure %, with no divergence in any
condition. The provenance tier does something: an ``estimated`` snapshot still
computes and still flags, but leaves the aggregates with its count reported.
"""

import os
import tempfile
import unittest

from journal import db, equity, risk


def _open_trade(conn, *, book="US", symbol="AAA", entry_date="2026-08-20",
                qty=100.0, avg_price=10.0, stop=8.0):
    """Land one Trade row directly (the confirm path is exercised elsewhere)."""
    cur = conn.execute(
        "INSERT INTO trade (book, symbol, entry_date, entry_qty, entry_avg_price, "
        "status, stop) VALUES (?, ?, ?, ?, ?, 'open', ?)",
        (book, symbol, entry_date, qty, avg_price, stop),
    )
    conn.commit()
    return cur.lastrowid


class LookupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_most_recent_snapshot_at_or_before_entry_is_used(self):
        equity.record_idx_snapshot(self.conn, date="2026-06-30", portfolio=900.0, ledger_balance=100.0)
        equity.record_idx_snapshot(self.conn, date="2026-07-31", portfolio=950.0, ledger_balance=50.0)
        # A snapshot *after* entry must never be reached back to.
        equity.record_idx_snapshot(self.conn, date="2026-08-31", portfolio=800.0, ledger_balance=200.0)
        snap = risk.snapshot_at_or_before(self.conn, "IDX", "2026-08-05")
        self.assertEqual(snap["date"], "2026-07-31")

    def test_no_snapshot_at_or_before_returns_none(self):
        equity.record_idx_snapshot(self.conn, date="2026-08-31", portfolio=800.0, ledger_balance=200.0)
        self.assertIsNone(risk.snapshot_at_or_before(self.conn, "IDX", "2026-08-05"))


class RiskExposureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # ── The numerators, against a fresh stated snapshot ──
    def test_risk_and_exposure_against_a_fresh_snapshot(self):
        # US bound is 7 calendar days; entry 2026-08-20, snapshot 2026-08-18 (2d).
        equity.import_nav_flex_text(
            self.conn,
            '<FlexQueryResponse><EquitySummaryByReportDateInBase reportDate="2026-08-18" '
            'total="100000" cash="10000" stock="90000"/></FlexQueryResponse>',
            fetch_date="2026-08-19",
        )
        tid = _open_trade(self.conn, entry_date="2026-08-20", qty=100.0, avg_price=10.0, stop=8.0)
        r = risk.compute_for_trade(self.conn, tid)
        # exposure = 10*100 / 100000 * 100 = 1.0 ; risk = (10-8)*100 / 100000 * 100 = 0.2
        self.assertAlmostEqual(r.exposure_percentage, 1.0)
        self.assertAlmostEqual(r.risk_percentage, 0.2)
        self.assertEqual(r.provenance, "stated")
        self.assertEqual(r.snapshot_date, "2026-08-18")
        self.assertEqual(r.staleness_days, 2)
        self.assertEqual(r.markers, frozenset())

    # ── Acceptance: both read the same snapshot under the same rule ──
    def test_both_percentages_read_the_same_snapshot(self):
        equity.import_nav_flex_text(
            self.conn,
            '<FlexQueryResponse><EquitySummaryByReportDateInBase reportDate="2026-08-19" '
            'total="50000" cash="5000" stock="45000"/></FlexQueryResponse>',
            fetch_date="2026-08-20",
        )
        tid = _open_trade(self.conn, entry_date="2026-08-20", qty=100.0, avg_price=10.0, stop=8.0)
        r = risk.compute_for_trade(self.conn, tid)
        self.assertEqual(r.snapshot_date, "2026-08-19")
        # Both derive off the *same* equity (50000): exposure 2.0, risk 0.4.
        self.assertAlmostEqual(r.exposure_percentage, 2.0)
        self.assertAlmostEqual(r.risk_percentage, 0.4)

    # ── Acceptance: past the bound both null with a marker ──
    def test_past_us_bound_both_null_with_marker(self):
        # 8 calendar days > US bound of 7.
        equity.import_nav_flex_text(
            self.conn,
            '<FlexQueryResponse><EquitySummaryByReportDateInBase reportDate="2026-08-12" '
            'total="100000" cash="10000" stock="90000"/></FlexQueryResponse>',
            fetch_date="2026-08-12",
        )
        tid = _open_trade(self.conn, entry_date="2026-08-20")
        r = risk.compute_for_trade(self.conn, tid)
        self.assertIsNone(r.risk_percentage)
        self.assertIsNone(r.exposure_percentage)
        self.assertIn(risk.INSUFFICIENT_HISTORY, r.markers)
        self.assertEqual(r.staleness_days, 8)

    def test_idx_bound_is_45_not_7(self):
        # 31 days is stale on a daily US series but fresh on IDX's monthly one —
        # a single global bound is useless (§9.4).
        equity.record_idx_snapshot(self.conn, date="2026-07-20", portfolio=800.0, ledger_balance=200.0)
        tid = _open_trade(self.conn, book="IDX", symbol="BBRI", entry_date="2026-08-20",
                          qty=100.0, avg_price=5.0, stop=4.0)
        r = risk.compute_for_trade(self.conn, tid)
        self.assertEqual(r.staleness_days, 31)
        self.assertIsNotNone(r.risk_percentage)      # fresh under the IDX bound
        self.assertEqual(r.markers, frozenset())

    def test_past_idx_bound_of_45_nulls(self):
        equity.record_idx_snapshot(self.conn, date="2026-06-30", portfolio=800.0, ledger_balance=200.0)
        tid = _open_trade(self.conn, book="IDX", symbol="BBRI", entry_date="2026-08-20",
                          qty=100.0, avg_price=5.0, stop=4.0)
        r = risk.compute_for_trade(self.conn, tid)
        self.assertEqual(r.staleness_days, 51)
        self.assertIsNone(r.risk_percentage)
        self.assertIsNone(r.exposure_percentage)
        self.assertIn(risk.INSUFFICIENT_HISTORY, r.markers)

    def test_no_snapshot_at_all_nulls_both_with_marker(self):
        tid = _open_trade(self.conn, entry_date="2026-08-20")
        r = risk.compute_for_trade(self.conn, tid)
        self.assertIsNone(r.risk_percentage)
        self.assertIsNone(r.exposure_percentage)
        self.assertIsNone(r.snapshot_date)
        self.assertIn(risk.INSUFFICIENT_HISTORY, r.markers)

    # ── Exposure computes without a stop; Risk is held open ──
    def test_exposure_computes_without_a_stop_risk_held_open(self):
        equity.import_nav_flex_text(
            self.conn,
            '<FlexQueryResponse><EquitySummaryByReportDateInBase reportDate="2026-08-19" '
            'total="100000" cash="10000" stock="90000"/></FlexQueryResponse>',
            fetch_date="2026-08-20",
        )
        cur = self.conn.execute(
            "INSERT INTO trade (book, symbol, entry_date, entry_qty, entry_avg_price, status) "
            "VALUES ('US', 'AAA', '2026-08-20', 100, 10.0, 'open')"
        )
        self.conn.commit()
        r = risk.compute_for_trade(self.conn, cur.lastrowid)
        self.assertAlmostEqual(r.exposure_percentage, 1.0)   # no stop needed
        self.assertIsNone(r.risk_percentage)                 # held open (§5.5)
        # A held-open risk is *not* the staleness null — the snapshot is fresh.
        self.assertNotIn(risk.INSUFFICIENT_HISTORY, r.markers)

    # ── Acceptance: estimated computes and flags but is excluded ──
    def test_estimated_snapshot_computes_and_flags_but_is_excluded(self):
        equity.record_idx_snapshot(
            self.conn, date="2026-08-10", portfolio=800.0, ledger_balance=200.0,
            provenance="estimated",
        )
        tid = _open_trade(self.conn, book="IDX", symbol="BBRI", entry_date="2026-08-20",
                          qty=100.0, avg_price=5.0, stop=4.0)
        r = risk.compute_for_trade(self.conn, tid)
        self.assertIsNotNone(r.risk_percentage)      # still computes
        self.assertEqual(r.provenance, "estimated")  # and flags
        self.assertTrue(r.excluded_from_risk_aggregate)


class DriftTest(unittest.TestCase):
    """A late-arriving snapshot recomputes; a null becoming a number is hole-filling."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_backdated_snapshot_fills_a_previously_nulled_risk(self):
        # Only a too-far snapshot exists: risk is null with the staleness marker.
        equity.import_nav_flex_text(
            self.conn,
            '<FlexQueryResponse><EquitySummaryByReportDateInBase reportDate="2026-08-01" '
            'total="100000" cash="10000" stock="90000"/></FlexQueryResponse>',
            fetch_date="2026-08-01",
        )
        tid = _open_trade(self.conn, entry_date="2026-08-20", qty=100.0, avg_price=10.0, stop=8.0)
        before = risk.compute_for_trade(self.conn, tid)
        self.assertIsNone(before.risk_percentage)
        self.assertIn(risk.INSUFFICIENT_HISTORY, before.markers)

        # A backdated snapshot arrives closer to the entry date (2 days before).
        equity.import_nav_flex_text(
            self.conn,
            '<FlexQueryResponse><EquitySummaryByReportDateInBase reportDate="2026-08-18" '
            'total="100000" cash="10000" stock="90000"/></FlexQueryResponse>',
            fetch_date="2026-08-25",
        )
        after = risk.compute_for_trade(self.conn, tid)
        # Read-time recompute: the hole is filled, no drift (nothing was pinned).
        self.assertAlmostEqual(after.risk_percentage, 0.2)
        self.assertEqual(after.markers, frozenset())
        self.assertEqual(after.snapshot_date, "2026-08-18")


class AggregateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_aggregate_reports_included_and_every_excluded_count(self):
        # One fresh stated, one stale, one estimated, one no-stop — all IDX.
        equity.record_idx_snapshot(self.conn, date="2026-08-15", portfolio=900.0, ledger_balance=100.0)
        equity.record_idx_snapshot(self.conn, date="2026-05-15", portfolio=900.0, ledger_balance=100.0)
        equity.record_idx_snapshot(self.conn, date="2026-08-14", portfolio=900.0, ledger_balance=100.0,
                                   provenance="estimated")

        _open_trade(self.conn, book="IDX", symbol="STATED", entry_date="2026-08-20",
                    qty=10.0, avg_price=5.0, stop=4.0)         # fresh stated -> included
        _open_trade(self.conn, book="IDX", symbol="STALE", entry_date="2026-08-01",
                    qty=10.0, avg_price=5.0, stop=4.0)         # only the May snap -> stale
        # A no-stop Trade against the fresh stated snapshot.
        self.conn.execute(
            "INSERT INTO trade (book, symbol, entry_date, entry_qty, entry_avg_price, status) "
            "VALUES ('IDX', 'NOSTOP', '2026-08-20', 10, 5.0, 'open')"
        )
        self.conn.commit()

        results = risk.compute_book(self.conn, "IDX")
        agg = risk.aggregate(results, metric="risk")
        self.assertEqual(agg.included, 1)
        self.assertEqual(agg.excluded_stale, 1)
        self.assertEqual(agg.excluded_no_stop, 1)
        self.assertEqual(agg.excluded_estimated, 0)  # no estimated Trade here

    def test_estimated_trade_lands_in_the_estimated_bucket(self):
        equity.record_idx_snapshot(self.conn, date="2026-08-14", portfolio=900.0, ledger_balance=100.0,
                                   provenance="estimated")
        _open_trade(self.conn, book="IDX", symbol="EST", entry_date="2026-08-20",
                    qty=10.0, avg_price=5.0, stop=4.0)
        agg = risk.aggregate(risk.compute_book(self.conn, "IDX"), metric="risk")
        self.assertEqual(agg.included, 0)
        self.assertEqual(agg.excluded_estimated, 1)

    def test_exposure_aggregate_excludes_stale_and_estimated_but_not_no_stop(self):
        # Exposure needs no stop, so a no-stop Trade is *included* in exposure.
        equity.record_idx_snapshot(self.conn, date="2026-08-15", portfolio=900.0, ledger_balance=100.0)
        self.conn.execute(
            "INSERT INTO trade (book, symbol, entry_date, entry_qty, entry_avg_price, status) "
            "VALUES ('IDX', 'NOSTOP', '2026-08-20', 10, 5.0, 'open')"
        )
        self.conn.commit()
        agg = risk.aggregate(risk.compute_book(self.conn, "IDX"), metric="exposure")
        self.assertEqual(agg.included, 1)
        self.assertEqual(agg.excluded_no_stop, 0)


class BannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_staleness_marker_reaches_a_banner_line(self):
        equity.record_idx_snapshot(self.conn, date="2026-06-30", portfolio=800.0, ledger_balance=200.0)
        tid = _open_trade(self.conn, book="IDX", symbol="BBRI", entry_date="2026-08-20",
                          qty=10.0, avg_price=5.0, stop=4.0)
        r = risk.compute_for_trade(self.conn, tid)
        line = risk.banner_line(r)
        self.assertIsNotNone(line)
        self.assertIn("IDX", line)
        self.assertIn("2026-06-30", line)      # the last snapshot's date is stated

    def test_a_fresh_trade_produces_no_banner_line(self):
        equity.record_idx_snapshot(self.conn, date="2026-08-15", portfolio=800.0, ledger_balance=200.0)
        tid = _open_trade(self.conn, book="IDX", symbol="BBRI", entry_date="2026-08-20",
                          qty=10.0, avg_price=5.0, stop=4.0)
        r = risk.compute_for_trade(self.conn, tid)
        self.assertIsNone(risk.banner_line(r))


if __name__ == "__main__":
    unittest.main()
