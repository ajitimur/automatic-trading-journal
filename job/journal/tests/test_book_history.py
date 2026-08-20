"""Book history — read-time projections `seq` and `book_drawdown_r_at_entry`
(SPEC §7.9, ADR 0004, issue #37).

Neither value is a stored field. They are computed at read time from the current
Trade set, never written to the Trade, never in the freeze snapshot, and a change
in either never fires drift (ADR 0004). These tests pin the load-bearing
invariants: `seq` is a 1-based ordinal by entry_date that a backdated Trade
renumbers silently; the drawdown is the peak-minus-cumulative closed-Trade R
curve, built off realized R and never `EquitySnapshot`, skipping no-stop Trades
with the excluded count shipped per book, and reading `insufficient_history` —
distinct from zero — below the 20 closed stop-bearing Trades threshold. Books
never combine.
"""

import os
import tempfile
import unittest

from journal import book_history, books, db
from journal import book_history as bh


class BookHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # -- fixtures ---------------------------------------------------------- #

    def _trade(self, *, entry, book="US", symbol="AAA", status="open",
               avg=100.0, stop=90.0, provenance="recorded"):
        cur = self.conn.execute(
            "INSERT INTO trade (book, symbol, entry_date, entry_qty, "
            "entry_avg_price, status, stop, stop_provenance) "
            "VALUES (?, ?, ?, 30, ?, ?, ?, ?)",
            (book, symbol, entry, avg, status, stop, provenance))
        self.conn.commit()
        return cur.lastrowid

    def _closed(self, *, entry, exit_date, r, book="US", symbol=None,
                stop=90.0, provenance="recorded"):
        """A closed Trade whose realized R equals ``r`` (entry 100, stop 90)."""
        symbol = symbol or f"S{exit_date}"
        avg, denom = 100.0, 100.0 - (stop if stop is not None else 90.0)
        tid = self._trade(entry=entry, book=book, symbol=symbol, status="closed",
                          avg=avg, stop=stop, provenance=provenance)
        exit_price = avg + r * denom if stop is not None else avg
        self.conn.execute(
            "INSERT INTO trade_exit (trade_id, source, source_ref, exit_date, "
            "quantity, price, reason) VALUES (?, 'ibkr', ?, ?, 30, ?, 'x')",
            (tid, f"ref-{symbol}", exit_date, exit_price))
        self.conn.commit()
        return tid

    def _curve_of_twenty(self, book="US"):
        """20 closed stop-bearing Trades: exits July 2 (+2), 3 (+3), 4 (−1),
        5 (−2), then 16 flat. cum ends 2, peak 5."""
        rs = [2.0, 3.0, -1.0, -2.0] + [0.0] * 16
        for i, r in enumerate(rs, start=2):
            self._closed(entry="2026-07-01", exit_date=f"2026-07-{i:02d}", r=r,
                         book=book, symbol=f"C{i:02d}")

    # -- seq --------------------------------------------------------------- #

    def test_seq_is_one_based_by_entry_date(self):
        self._trade(entry="2026-07-10", symbol="B")
        self._trade(entry="2026-07-03", symbol="A")
        self._trade(entry="2026-07-20", symbol="C")
        proj = bh.project(self.conn, "US")
        got = {r.symbol: r.seq for r in proj.rows}
        self.assertEqual(got, {"A": 1, "B": 2, "C": 3})

    def test_backdated_trade_renumbers_successors_without_firing_drift(self):
        self._trade(entry="2026-07-03", symbol="A")
        self._trade(entry="2026-07-20", symbol="C")
        before = {r.symbol: r.seq for r in bh.project(self.conn, "US").rows}
        self.assertEqual(before, {"A": 1, "C": 2})
        # A backdated Trade lands in the middle.
        self._trade(entry="2026-07-10", symbol="B")
        after = {r.symbol: r.seq for r in bh.project(self.conn, "US").rows}
        self.assertEqual(after, {"A": 1, "B": 2, "C": 3})
        # The read-time projection writes nothing, so nothing can fire drift.
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM trade_post_exit").fetchone()[0],
            0)

    # -- drawdown ---------------------------------------------------------- #

    def test_drawdown_is_peak_minus_cumulative_at_entry_date(self):
        self._curve_of_twenty()
        # Probe Trades entering at chosen dates; open, so not on the curve.
        self._trade(entry="2026-07-04", symbol="P1")   # after +2,+3
        self._trade(entry="2026-07-05", symbol="P2")   # after +2,+3,−1
        self._trade(entry="2026-07-06", symbol="P3")   # after +2,+3,−1,−2
        self._trade(entry="2026-08-01", symbol="P4")   # after everything
        dd = {r.symbol: r.book_drawdown_r_at_entry for r in bh.project(self.conn, "US").rows}
        self.assertAlmostEqual(dd["P1"], 0.0)
        self.assertAlmostEqual(dd["P2"], 1.0)
        self.assertAlmostEqual(dd["P3"], 3.0)
        self.assertAlmostEqual(dd["P4"], 3.0)

    def test_drawdown_is_built_from_r_never_from_equity_snapshot(self):
        self._curve_of_twenty()
        self._trade(entry="2026-08-01", symbol="P")
        # A wildly different equity curve — including a cash withdrawal dip — that
        # would imply another drawdown entirely if it were the source.
        for d, eq in (("2026-07-01", 100000), ("2026-07-05", 10000),
                      ("2026-08-01", 500000)):
            self.conn.execute(
                "INSERT INTO equity_snapshot (book, date, equity, source) "
                "VALUES ('US', ?, ?, 'ibkr')", (d, eq))
        self.conn.commit()
        dd = {r.symbol: r.book_drawdown_r_at_entry for r in bh.project(self.conn, "US").rows}
        self.assertAlmostEqual(dd["P"], 3.0)   # the R curve, untouched by equity

    # -- stop provenance --------------------------------------------------- #

    def test_no_stop_trades_are_skipped_and_counted_per_book(self):
        self._curve_of_twenty()
        self._closed(entry="2026-07-01", exit_date="2026-07-22", r=0.0,
                     symbol="NOSTOP1", stop=None)
        self._closed(entry="2026-07-01", exit_date="2026-07-23", r=0.0,
                     symbol="NOSTOP2", stop=None)
        proj = bh.project(self.conn, "US")
        self.assertEqual(proj.excluded_no_stop, 2)
        self.assertEqual(proj.closed_with_stop, 20)
        # The no-stop Trades still get a seq — seq is a fact about every Trade.
        self.assertIn("NOSTOP1", {r.symbol for r in proj.rows})

    def test_reconstructed_stops_stay_on_the_r_curve(self):
        # An R measure, not adherence: a reconstructed stop still carries R.
        for i in range(2, 21):
            self._closed(entry="2026-07-01", exit_date=f"2026-07-{i:02d}", r=0.0,
                         symbol=f"C{i:02d}")
        self._closed(entry="2026-07-01", exit_date="2026-07-21", r=-1.0,
                     symbol="RECON", provenance="reconstructed")
        proj = bh.project(self.conn, "US")
        self.assertEqual(proj.closed_with_stop, 20)
        self.assertEqual(proj.excluded_no_stop, 0)

    # -- insufficient history ---------------------------------------------- #

    def test_below_twenty_closed_stop_trades_is_insufficient_history(self):
        for i in range(2, 21):   # only 19 closed stop-bearing Trades
            self._closed(entry="2026-07-01", exit_date=f"2026-07-{i:02d}", r=-1.0,
                         symbol=f"C{i:02d}")
        self._trade(entry="2026-08-01", symbol="P")
        proj = bh.project(self.conn, "US")
        self.assertEqual(proj.closed_with_stop, 19)
        self.assertFalse(proj.sufficient_history)
        probe = next(r for r in proj.rows if r.symbol == "P")
        # insufficient_history is distinct from a drawdown of zero.
        self.assertIsNone(probe.book_drawdown_r_at_entry)
        self.assertEqual(probe.drawdown_marker, bh.INSUFFICIENT_HISTORY)

    # -- books never combine ----------------------------------------------- #

    def test_books_never_combine(self):
        self._curve_of_twenty(book="US")
        self._closed(entry="2026-07-01", exit_date="2026-07-02", r=-5.0,
                     book="IDX", symbol="IDXA", stop=None)
        us = bh.project(self.conn, "US")
        idx = bh.project(self.conn, "IDX")
        self.assertEqual(us.excluded_no_stop, 0)      # IDX's no-stop is not US's
        self.assertEqual(us.closed_with_stop, 20)
        self.assertEqual(idx.excluded_no_stop, 1)
        self.assertFalse(idx.sufficient_history)      # one Trade only
        self.assertEqual({r.symbol for r in idx.rows}, {"IDXA"})


if __name__ == "__main__":
    unittest.main()


class ScopeStartTest(unittest.TestCase):
    """Scope Start bounds the drawdown curve (ADR 0008)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _trade(self, tid, entry_date, entry, stop, exit_price, exit_date):
        self.conn.execute(
            "INSERT INTO trade (id, book, symbol, entry_date, entry_qty, "
            "entry_avg_price, status, stop, stop_provenance) "
            "VALUES (?, 'US', ?, ?, 100, ?, 'closed', ?, 'recorded')",
            (tid, f"S{tid}", entry_date, entry, stop),
        )
        self.conn.execute(
            "INSERT INTO trade_exit (trade_id, source, source_ref, exit_date, quantity, price) "
            "VALUES (?, 'ibkr', ?, ?, 100, ?)",
            (tid, f"x{tid}", exit_date, exit_price),
        )
        self.conn.commit()

    def test_no_scope_start_counts_everything(self):
        self._trade(1, "2026-07-01", 100, 90, 120, "2026-07-05")
        self._trade(2, "2026-08-20", 100, 90, 80, "2026-08-25")
        self.assertEqual(len(book_history.project(self.conn, "US").rows), 2)

    def test_pre_boundary_trades_leave_the_curve(self):
        self._trade(1, "2026-07-01", 100, 90, 120, "2026-07-05")
        self._trade(2, "2026-08-20", 100, 90, 80, "2026-08-25")
        books.set_scope_start(self.conn, "US", "2026-08-18")

        rows = book_history.project(self.conn, "US").rows
        self.assertEqual([r.trade_id for r in rows], [2])

    def test_a_pre_boundary_win_no_longer_sets_the_high_water_mark(self):
        """The reason the curve must be scoped, not just the row list.

        A +2R Trade from the old record would set a peak the restarted record is
        measured against, so the first new losing Trade would read as a drawdown
        from a run it never had.
        """
        self._trade(1, "2026-07-01", 100, 90, 120, "2026-07-05")   # +2R, old record
        self._trade(2, "2026-08-20", 100, 90, 80, "2026-08-25")    # −2R, new record
        books.set_scope_start(self.conn, "US", "2026-08-18")

        (row,) = book_history.project(self.conn, "US").rows
        # Measured against its own record's mark (0), not the old +2R peak.
        self.assertEqual(row.trade_id, 2)
        self.assertNotEqual(row.book_drawdown_r_at_entry, 2.0)
