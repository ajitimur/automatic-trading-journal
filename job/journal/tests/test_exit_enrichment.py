"""Exit geometry (section D) and in-trade excursion (section E) — SPEC
§7.4–§7.5, issue #33.

Exit geometry recomputes section B's formulas anchored on the *final exit day's
close* ``C_x`` — the deliberate mirror of entry's prior-close anchor (SPEC §6.3),
an asymmetry that must not be "fixed". Excursions store raw highs, lows and their
dates; R, ADR and % forms derive on read. Two scopes are stored — Trade-level
(entry → final exit) and per-Exit (entry → that Exit's date) — because *"was the
day-3 partial early?"* and *"was the ride good?"* are different questions and only
the per-Exit scope answers the first.
"""

import os
import tempfile
import unittest
from datetime import date, timedelta

from journal import db
from journal.bars import Bar
from journal.enrichment import ALIGNED_UP, INSUFFICIENT_HISTORY, compute_entry_enrichment
from journal.exit_enrichment import (
    Excursion,
    ExcursionStore,
    ExitGeometry,
    ExitGeometryStore,
    compute_excursion,
    compute_exit_geometry,
)


def _series(n, base=100.0, step=1.0, spread=0.02, volume=1000, start="2026-01-01"):
    """n bars, close = base + step*i (oldest first); High/Low ±spread/2."""
    d0 = date.fromisoformat(start)
    bars = []
    for i in range(n):
        close = base + step * i
        high = close * (1 + spread / 2)
        low = close * (1 - spread / 2)
        bars.append(Bar(
            date=(d0 + timedelta(days=i)).isoformat(),
            open=close, high=high, low=low, close=close,
            volume=volume, dividend=0.0,
        ))
    return bars


class ExitAnchorTest(unittest.TestCase):
    def test_anchors_on_the_exit_day_close_not_the_prior_close(self):
        # Entry anchors on the *prior* close; exit anchors on its *own* close.
        # The asymmetry is deliberate (SPEC §6.3) — the exit day's close is a
        # decision input (the exit rule is triggered by a close), not a leak.
        bars = _series(300, start="2026-01-01")
        exit_date = bars[-1].date
        geom = compute_exit_geometry("US", "AAA", exit_date, 250.0, bars, [])
        self.assertEqual(geom.bar_date, exit_date)  # C_x is the exit day's bar

    def test_no_bar_at_or_before_exit_returns_none(self):
        bars = _series(10, start="2026-06-01")
        self.assertIsNone(
            compute_exit_geometry("US", "AAA", "2026-01-01", 100.0, bars, []))


class ExitGeometryFormulaTest(unittest.TestCase):
    def test_recomputes_section_b_formulas_anchored_on_cx(self):
        bars = _series(300, base=100.0, step=1.0)
        exit_date = bars[-1].date
        cx = bars[-1].close
        geom = compute_exit_geometry("US", "AAA", exit_date, cx, bars, [])
        closes = [b.close for b in bars]  # up to and including the exit bar
        for n, ma in ((10, geom.ma_10_at_exit), (200, geom.ma_200_at_exit)):
            self.assertAlmostEqual(ma, sum(closes[-n:]) / n)
        expected_10 = (cx - geom.ma_10_at_exit) / cx * 100 / geom.adr_pct_at_exit
        self.assertAlmostEqual(geom.ma_dist_10_at_exit, expected_10)
        self.assertGreater(geom.ma_dist_10_at_exit, 0)  # rising series
        self.assertEqual(geom.stack_state_at_exit, ALIGNED_UP)

    def test_exit_geometry_differs_from_entry_geometry_same_bars(self):
        # Section B at entry (prior close) and section D at exit (exit close) run
        # the same formulas at different anchors, so on a trending series they
        # land different numbers — proving the anchor, not the formula, moved.
        bars = _series(300, base=100.0, step=1.0)
        entry = compute_entry_enrichment("US", "AAA", bars[100].date, bars, [])
        geom = compute_exit_geometry("US", "AAA", bars[-1].date, bars[-1].close, bars, [])
        self.assertNotAlmostEqual(entry.ma_dist_10, geom.ma_dist_10_at_exit)

    def test_rs_at_exit_is_symbol_minus_benchmark_to_cx(self):
        symbol = _series(200, base=100.0, step=2.0)
        benchmark = _series(200, base=100.0, step=1.0)
        exit_date = symbol[-1].date
        geom = compute_exit_geometry("US", "AAA", exit_date, symbol[-1].close,
                                     symbol, benchmark)
        self.assertGreater(geom.rs_63d_at_exit, 0)

    def test_short_benchmark_nulls_rs_at_exit_with_marker(self):
        symbol = _series(200)
        benchmark = _series(40)
        geom = compute_exit_geometry("US", "AAA", symbol[-1].date,
                                     symbol[-1].close, symbol, benchmark)
        self.assertIsNone(geom.rs_63d_at_exit)
        self.assertEqual(geom.marker("rs_63d_at_exit"), INSUFFICIENT_HISTORY)

    def test_carries_exit_avg_price_beside_cx(self):
        bars = _series(60)
        geom = compute_exit_geometry("US", "AAA", bars[-1].date, 123.45, bars, [])
        self.assertAlmostEqual(geom.exit_avg_price, 123.45)


class ExcursionTest(unittest.TestCase):
    def test_stores_raw_high_low_and_their_dates(self):
        # A hump: the high lands mid-window, the low at the start.
        bars = _series(10, base=100.0, step=1.0, start="2026-02-01")
        # Force a distinct spike on day index 3.
        bars[3] = Bar(date=bars[3].date, open=100, high=999.0, low=90.0,
                      close=100, volume=1000)
        exc = compute_excursion(bars, bars[0].date, bars[-1].date)
        self.assertAlmostEqual(exc.mfe_high, 999.0)
        self.assertEqual(exc.mfe_date, bars[3].date)
        self.assertAlmostEqual(exc.mae_low, 90.0)
        self.assertEqual(exc.mae_date, bars[3].date)

    def test_window_is_inclusive_of_both_endpoints(self):
        bars = _series(20, start="2026-03-01")
        exc = compute_excursion(bars, bars[5].date, bars[10].date)
        window = bars[5:11]
        self.assertAlmostEqual(exc.mfe_high, max(b.high for b in window))
        self.assertAlmostEqual(exc.mae_low, min(b.low for b in window))

    def test_earliest_date_wins_a_tie(self):
        # Two bars share the same high; the earlier date is the finding.
        bars = _series(6, base=100.0, step=0.0, start="2026-04-01")
        exc = compute_excursion(bars, bars[0].date, bars[-1].date)
        self.assertEqual(exc.mfe_date, bars[0].date)

    def test_empty_window_yields_null_primitives(self):
        bars = _series(5, start="2026-05-01")
        exc = compute_excursion(bars, "2027-01-01", "2027-02-01")
        self.assertIsNone(exc.mfe_high)
        self.assertIsNone(exc.mfe_date)
        self.assertIsNone(exc.mae_low)


class ExcursionDerivedFormsTest(unittest.TestCase):
    def test_r_adr_and_pct_derive_from_stored_primitives(self):
        exc = Excursion(start_date="2026-01-01", end_date="2026-01-10",
                        mfe_high=130.0, mfe_date="2026-01-05",
                        mae_low=90.0, mae_date="2026-01-02")
        entry, stop, adr = 100.0, 95.0, 2.0
        self.assertAlmostEqual(exc.mfe_r(entry, stop), (130 - 100) / (100 - 95))
        self.assertAlmostEqual(exc.mfe_adr(entry, adr), (130 - 100) / 100 * 100 / adr)
        self.assertAlmostEqual(exc.mfe_pct(entry), (130 / 100 - 1) * 100)
        # MAE runs the same formula; below entry it is naturally negative.
        self.assertAlmostEqual(exc.mae_r(entry, stop), (90 - 100) / (100 - 95))
        self.assertLess(exc.mae_r(entry, stop), 0)

    def test_r_forms_null_without_a_stop(self):
        # No stop → no R (SPEC §6.4): the primitives stay usable as prices.
        exc = Excursion("2026-01-01", "2026-01-10", 130.0, "2026-01-05",
                        90.0, "2026-01-02")
        self.assertIsNone(exc.mfe_r(100.0, None))
        self.assertIsNone(exc.mae_r(100.0, None))
        # % never needs a stop.
        self.assertIsNotNone(exc.mfe_pct(100.0))

    def test_adr_form_null_without_adr(self):
        exc = Excursion("2026-01-01", "2026-01-10", 130.0, "2026-01-05",
                        90.0, "2026-01-02")
        self.assertIsNone(exc.mfe_adr(100.0, None))
        self.assertIsNone(exc.mfe_adr(100.0, 0.0))


class ExitGeometryStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _trade(self, trade_id, symbol="AAA", entry="2026-10-01"):
        self.conn.execute(
            "INSERT INTO trade (id, book, symbol, entry_date) VALUES (?, ?, ?, ?)",
            (trade_id, "US", symbol, entry))
        self.conn.commit()

    def test_geometry_roundtrip(self):
        bars = _series(300)
        geom = compute_exit_geometry("US", "AAA", bars[-1].date, 400.0, bars, bars)
        self._trade(1)
        store = ExitGeometryStore(self.conn)
        store.upsert(1, geom)
        got = store.get(1)
        self.assertAlmostEqual(got.exit_avg_price, 400.0)
        self.assertAlmostEqual(got.adr_pct_at_exit, geom.adr_pct_at_exit)
        self.assertAlmostEqual(got.ma_dist_200_at_exit, geom.ma_dist_200_at_exit)
        self.assertEqual(got.stack_state_at_exit, geom.stack_state_at_exit)
        self.assertEqual(got.bar_date, geom.bar_date)

    def test_geometry_markers_survive_and_upsert_idempotent(self):
        bars = _series(31)
        geom = compute_exit_geometry("US", "AAA", bars[-1].date, 10.0, bars, [])
        self._trade(2)
        store = ExitGeometryStore(self.conn)
        store.upsert(2, geom)
        store.upsert(2, geom)
        got = store.get(2)
        self.assertEqual(got.insufficient_history, geom.insufficient_history)
        self.assertEqual(got.marker("stack_state_at_exit"), INSUFFICIENT_HISTORY)
        n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM trade_exit_geometry").fetchone()["c"]
        self.assertEqual(n, 1)


class ExcursionStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _trade(self, trade_id, symbol="AAA", entry="2026-10-01"):
        self.conn.execute(
            "INSERT INTO trade (id, book, symbol, entry_date) VALUES (?, ?, ?, ?)",
            (trade_id, "US", symbol, entry))
        self.conn.commit()

    def _exit(self, trade_id, exit_date, qty=10.0, price=110.0):
        cur = self.conn.execute(
            "INSERT INTO trade_exit (trade_id, source, source_ref, exit_date, "
            "quantity, price) VALUES (?, 'ibkr', ?, ?, ?, ?)",
            (trade_id, f"ref-{exit_date}", exit_date, qty, price))
        self.conn.commit()
        return cur.lastrowid

    def test_trade_level_excursion_roundtrip(self):
        exc = Excursion("2026-10-01", "2026-10-20", 130.0, "2026-10-05",
                        90.0, "2026-10-02")
        self._trade(1)
        store = ExcursionStore(self.conn)
        store.upsert_trade(1, exc)
        got = store.get_trade(1)
        self.assertAlmostEqual(got.mfe_high, 130.0)
        self.assertEqual(got.mfe_date, "2026-10-05")
        self.assertEqual(got.end_date, "2026-10-20")

    def test_multi_exit_trade_carries_a_distinct_window_per_exit(self):
        # The strategy sells a partial on day 3 and rides the rest: each Exit gets
        # its own excursion over entry → that Exit's date, never a blend.
        self._trade(1, entry="2026-10-01")
        e1 = self._exit(1, "2026-10-03")
        e2 = self._exit(1, "2026-10-20")
        store = ExcursionStore(self.conn)
        store.upsert_exit(e1, 1, Excursion("2026-10-01", "2026-10-03",
                                           115.0, "2026-10-03", 98.0, "2026-10-02"))
        store.upsert_exit(e2, 1, Excursion("2026-10-01", "2026-10-20",
                                           140.0, "2026-10-15", 90.0, "2026-10-02"))
        g1, g2 = store.get_exit(e1), store.get_exit(e2)
        self.assertEqual(g1.end_date, "2026-10-03")
        self.assertEqual(g2.end_date, "2026-10-20")
        self.assertNotAlmostEqual(g1.mfe_high, g2.mfe_high)

    def test_exit_excursion_idempotent(self):
        self._trade(1)
        e1 = self._exit(1, "2026-10-03")
        store = ExcursionStore(self.conn)
        exc = Excursion("2026-10-01", "2026-10-03", 115.0, "2026-10-03",
                        98.0, "2026-10-02")
        store.upsert_exit(e1, 1, exc)
        store.upsert_exit(e1, 1, exc)
        n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM exit_excursion").fetchone()["c"]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
