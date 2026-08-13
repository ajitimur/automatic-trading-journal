"""The counterfactual and adherence engine — six variants (SPEC §10, issue #35).

Adherence is inverted (§10.1): the engine scores every closed Trade against all
six variants and reports which it best fits, storing signed deltas and never a
boolean verdict. These tests pin the load-bearing invariants — the recorded stop
as a hard leg on every variant, the trail/partial pricing asymmetry, the band
absence rule, the 60-day cap that never fabricates an exit, the six-way fit
vector, and the three R tiers with their reported excluded counts.
"""

import os
import tempfile
import unittest
from datetime import date, timedelta

from journal import db
from journal.bars import Bar
from journal import counterfactual as cf


def _bars(closes, *, start="2026-07-01", opens=None, highs=None, lows=None,
          volume=1000, dividends=None):
    """Build a trading-day series from a list of closes (oldest first).

    High/Low default to a ±1% band around the close; open defaults to the close.
    Every list override is positional-parallel to ``closes``.
    """
    d0 = date.fromisoformat(start)
    out = []
    for i, c in enumerate(closes):
        o = opens[i] if opens else c
        hi = highs[i] if highs else max(o, c) * 1.01
        lo = lows[i] if lows else min(o, c) * 0.99
        dv = dividends[i] if dividends else 0.0
        out.append(Bar(
            date=(d0 + timedelta(days=i)).isoformat(),
            open=o, high=hi, low=lo, close=c, volume=volume, dividend=dv,
        ))
    return out


class RulesetTest(unittest.TestCase):
    def test_v1_is_nominal_ma10_and_effective_from_the_backdating_floor(self):
        rs = cf.ruleset_for("2026-08-01")
        self.assertEqual(rs.version, "ruleset_v1")
        self.assertEqual(rs.nominal_trail, "ma10")
        self.assertEqual(rs.nominal_variant, "ma10/day3")

    def test_entry_before_the_floor_has_no_live_ruleset(self):
        self.assertIsNone(cf.ruleset_for("2026-06-01"))

    def test_the_six_variants_are_exactly_trail_times_partial(self):
        self.assertEqual(
            set(cf.VARIANTS),
            {f"{t}/{p}" for t in ("ma10", "ma20") for p in ("none", "day3", "day5")},
        )
        self.assertEqual(len(cf.VARIANTS), 6)


class VariantSimulationTest(unittest.TestCase):
    def test_all_six_variants_carry_the_recorded_stop_as_a_hard_leg(self):
        # A series that rises then collapses through the stop on day 5. Every
        # variant must exit on the stop, not ride through it (§10.5). Day 5 opens
        # above the stop (100) and its Low pierces intraday, so the fill is the
        # stop price — the gap-through-the-open case is covered separately.
        closes = [100, 102, 104, 103, 90] + [80] * 60
        opens = [100, 102, 104, 103, 100] + [80] * 60  # day-5 opens above stop 95
        lows = [99, 101, 103, 102, 84] + [79] * 60  # day-5 low pierces stop 95
        bars = _bars(closes, opens=opens, lows=lows)
        results = cf.simulate_all(bars, entry_date=bars[0].date, stop=95.0)
        self.assertEqual(set(r.variant for r in results), set(cf.VARIANTS))
        for r in results:
            self.assertEqual(r.status, cf.RESOLVED)
            final = r.legs[-1]
            self.assertEqual(final.trigger, cf.TRIGGER_STOP)
            self.assertAlmostEqual(final.price, 95.0)  # filled at the stop

    def test_stop_gap_fills_at_the_open_below_the_stop(self):
        closes = [100, 100, 80] + [80] * 60
        opens = [100, 100, 78] + [80] * 60  # day 3 gaps below stop 95
        bars = _bars(closes, opens=opens)
        r = cf.simulate(bars, entry_date=bars[0].date, stop=95.0,
                        trail="ma10", partial="none")
        self.assertEqual(r.legs[-1].trigger, cf.TRIGGER_STOP)
        self.assertAlmostEqual(r.legs[-1].price, 78.0)  # the gap open, not 95

    def test_trail_signal_prices_at_the_next_days_open(self):
        # Long rising base so MA10 sits below price, then a close below MA10; the
        # trail leg must fill at the *following* day's open (§10.5).
        closes = [100] * 15 + [90] + [88] * 60  # day 16 closes below the flat MA10
        bars = _bars(closes)
        r = cf.simulate(bars, entry_date=bars[10].date, stop=None,
                        trail="ma10", partial="none")
        trail_leg = r.legs[-1]
        self.assertEqual(trail_leg.trigger, cf.TRIGGER_TRAIL)
        # The signal is day-16's close < MA10; the fill is day-17's open (88).
        signal_idx = 15
        self.assertAlmostEqual(trail_leg.price, bars[signal_idx + 1].open)
        self.assertEqual(trail_leg.date, bars[signal_idx + 1].date)

    def test_scheduled_partial_prices_at_that_days_close(self):
        closes = [100, 101, 102, 103, 104, 105] + [106] * 60
        bars = _bars(closes)
        r = cf.simulate(bars, entry_date=bars[0].date, stop=None,
                        trail="ma10", partial="day3")
        partial = r.legs[0]
        self.assertEqual(partial.trigger, cf.TRIGGER_PARTIAL)
        # Day 3 is entry + 2 trading days; the close of that bar.
        self.assertAlmostEqual(partial.price, bars[2].close)
        self.assertAlmostEqual(partial.fraction, 1 / 3)

    def test_the_none_variant_takes_no_partial(self):
        closes = [100, 101, 102, 103, 104] + [105] * 60
        bars = _bars(closes)
        r = cf.simulate(bars, entry_date=bars[0].date, stop=None,
                        trail="ma10", partial="none")
        self.assertFalse(any(leg.trigger == cf.TRIGGER_PARTIAL for leg in r.legs))


class CapTest(unittest.TestCase):
    def test_sixty_trading_day_cap_records_capped_and_no_pseudo_exit(self):
        # A series that never triggers the trail and never hits the stop within
        # the window: MA10 stays below a monotonically rising close.
        closes = list(range(100, 100 + 80))
        bars = _bars(closes)
        r = cf.simulate(bars, entry_date=bars[0].date, stop=1.0,
                        trail="ma10", partial="none")
        self.assertEqual(r.status, cf.CAPPED)
        cap_leg = r.legs[-1]
        self.assertEqual(cap_leg.trigger, cf.TRIGGER_CAP)
        self.assertIsNone(cap_leg.price)  # never a fabricated exit price
        # The cap lands on the 60th trading day, entry being day 1.
        self.assertEqual(cap_leg.date, bars[59].date)


class LimitLockTest(unittest.TestCase):
    def test_open_high_low_close_equal_with_volume_marks_limit_locked(self):
        closes = [100] * 15 + [90] + [88] * 60
        bars = _bars(closes)
        # Make the trail-fill day (day 17) a limit lock.
        lock = Bar(date=bars[16].date, open=88, high=88, low=88, close=88, volume=500)
        bars[16] = lock
        r = cf.simulate(bars, entry_date=bars[10].date, stop=None,
                        trail="ma10", partial="none")
        self.assertTrue(r.legs[-1].limit_locked)


class DeviationCostNullTest(unittest.TestCase):
    """`deviation_cost_r` nulls where the number cannot be trusted (§10.8, #36).

    Beyond the cap and the absent-stop tiers, the R form nulls on a limit-locked
    nominal leg (a fill nobody could have obtained) and on a mismatched ex-date
    crossing between the actual and nominal windows (else the rule reads as
    outperforming when it merely dodged a dividend). Nulling is stronger than a
    flag, because a flag can be read past.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _trade(self, *, entry, qty, avg, stop, symbol="AAA"):
        cur = self.conn.execute(
            "INSERT INTO trade (book, symbol, entry_date, entry_qty, "
            "entry_avg_price, status, stop, stop_provenance) "
            "VALUES ('US', ?, ?, ?, ?, 'closed', ?, 'recorded')",
            (symbol, entry, qty, avg, stop))
        self.conn.commit()
        return cur.lastrowid

    def _exit(self, trade_id, exit_date, qty, price, reason=None):
        self.conn.execute(
            "INSERT INTO trade_exit (trade_id, source, source_ref, exit_date, "
            "quantity, price, reason) VALUES (?, 'ibkr', ?, ?, ?, ?, ?)",
            (trade_id, f"ref-{exit_date}", exit_date, qty, price, reason))
        self.conn.commit()

    def test_deviation_cost_r_nulls_on_a_limit_locked_nominal_leg(self):
        # The nominal (ma10/day3) trail fill lands on a limit-locked bar.
        closes = [100] * 15 + [90] + [88] * 60
        bars = _bars(closes)
        lock = Bar(date=bars[16].date, open=88, high=88, low=88, close=88,
                   volume=500)
        bars[16] = lock
        tid = self._trade(entry=bars[10].date, qty=30, avg=100.0, stop=1.0)
        self._exit(tid, bars[20].date, 30, 85.0, reason="discretionary")
        tc = cf.compute_trade(self.conn, tid, bars)
        self.assertEqual(tc.nominal_status, cf.RESOLVED)
        self.assertIsNotNone(tc.deviation_cost())      # cash still reads
        self.assertIsNone(tc.deviation_cost_r())        # but the R form nulls

    def test_deviation_cost_r_nulls_on_a_mismatched_ex_date_crossing(self):
        # Nominal trail exits at index 16; the actual runs to index 30, so a
        # dividend at index 20 falls in the actual window but not the nominal's.
        closes = [100] * 15 + [90] + [88] * 60
        divs = [0.0] * 20 + [2.0] + [0.0] * 60
        bars = _bars(closes, dividends=divs)
        tid = self._trade(entry=bars[10].date, qty=30, avg=100.0, stop=1.0)
        self._exit(tid, bars[30].date, 30, 85.0, reason="discretionary")
        tc = cf.compute_trade(self.conn, tid, bars)
        self.assertEqual(tc.nominal_status, cf.RESOLVED)
        self.assertIsNotNone(tc.deviation_cost())
        self.assertIsNone(tc.deviation_cost_r())


class DividendDragTest(unittest.TestCase):
    """`dividend_drag_r` — a corporate-actions field beside Realized R (§7.7, #36).

    Detecting the drop is free (yfinance ships dividends in the same call); it
    sits *beside* Realized R, never folded in. Null — not zero — where the window
    crossed no ex-date, so absent coverage reads as *unknown* rather than *no
    dividend*. Trade-level only, with no per-variant equivalent.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _trade(self, *, entry, qty, avg, stop, provenance="recorded", symbol="AAA"):
        cur = self.conn.execute(
            "INSERT INTO trade (book, symbol, entry_date, entry_qty, "
            "entry_avg_price, status, stop, stop_provenance) "
            "VALUES ('US', ?, ?, ?, ?, 'closed', ?, ?)",
            (symbol, entry, qty, avg, stop, provenance))
        self.conn.commit()
        return cur.lastrowid

    def _exit(self, trade_id, exit_date, qty, price, reason=None):
        self.conn.execute(
            "INSERT INTO trade_exit (trade_id, source, source_ref, exit_date, "
            "quantity, price, reason) VALUES (?, 'ibkr', ?, ?, ?, ?, ?)",
            (trade_id, f"ref-{exit_date}", exit_date, qty, price, reason))
        self.conn.commit()

    def test_drag_computes_from_dividends_and_leaves_realized_r_untouched(self):
        # A 3.5-per-share dividend crosses the window; stop 8 below a 100 entry.
        divs = [0.0] * 3 + [3.5] + [0.0] * 3
        bars = _bars([100, 101, 102, 103, 104, 105, 106], dividends=divs)
        tid = self._trade(entry=bars[0].date, qty=30, avg=100.0, stop=92.0)
        self._exit(tid, bars[6].date, 30, 106.0, reason="discretionary")
        tc = cf.compute_trade(self.conn, tid, bars)
        # sum(dividend) / (entry_avg_price − stop) = 3.5 / 8 = 0.4375R phantom.
        self.assertAlmostEqual(tc.dividend_drag_r, 3.5 / 8.0)
        # Realized R is a pure price measure — the dividend never enters it.
        self.assertAlmostEqual(
            cf.realized_r(100.0, 106.0, 92.0), (106.0 - 100.0) / 8.0)

    def test_null_not_zero_when_no_ex_date_was_crossed(self):
        # No dividend anywhere in the window: unknown, never a zero.
        bars = _bars([100, 101, 102, 103, 104, 105, 106])
        tid = self._trade(entry=bars[0].date, qty=30, avg=100.0, stop=92.0)
        self._exit(tid, bars[6].date, 30, 106.0, reason="discretionary")
        tc = cf.compute_trade(self.conn, tid, bars)
        self.assertIsNone(tc.dividend_drag_r)

    def test_dividend_outside_the_window_does_not_count(self):
        # The only dividend sits after the final exit — the window crossed none.
        divs = [0.0] * 6 + [3.5]
        bars = _bars([100, 101, 102, 103, 104, 105, 106], dividends=divs)
        tid = self._trade(entry=bars[0].date, qty=30, avg=100.0, stop=92.0)
        self._exit(tid, bars[3].date, 30, 103.0, reason="discretionary")
        tc = cf.compute_trade(self.conn, tid, bars)
        self.assertIsNone(tc.dividend_drag_r)

    def test_no_stop_trade_has_no_drag_denominator(self):
        divs = [0.0] * 3 + [3.5] + [0.0] * 3
        bars = _bars([100, 101, 102, 103, 104, 105, 106], dividends=divs)
        tid = self._trade(entry=bars[0].date, qty=30, avg=100.0, stop=None,
                          provenance=None)
        self._exit(tid, bars[6].date, 30, 106.0, reason="discretionary")
        tc = cf.compute_trade(self.conn, tid, bars)
        self.assertIsNone(tc.dividend_drag_r)

    def test_trade_level_only_with_no_per_variant_drag(self):
        divs = [0.0] * 3 + [3.5] + [0.0] * 3
        bars = _bars([100, 101, 102, 103, 104, 105, 106], dividends=divs)
        tid = self._trade(entry=bars[0].date, qty=30, avg=100.0, stop=92.0)
        self._exit(tid, bars[6].date, 30, 106.0, reason="discretionary")
        tc = cf.compute_trade(self.conn, tid, bars)
        # The drag lives on the Trade; no VariantResult carries an equivalent.
        for v in tc.variants:
            self.assertFalse(hasattr(v, "dividend_drag_r"))

    def test_drag_is_pinned_and_roundtrips_through_the_store(self):
        divs = [0.0] * 3 + [3.5] + [0.0] * 3
        bars = _bars([100, 101, 102, 103, 104, 105, 106], dividends=divs)
        tid = self._trade(entry=bars[0].date, qty=30, avg=100.0, stop=92.0)
        self._exit(tid, bars[6].date, 30, 106.0, reason="discretionary")
        tc = cf.compute_trade(self.conn, tid, bars)
        store = cf.CounterfactualStore(self.conn)
        store.upsert(tid, tc)
        got = store.get(tid)
        self.assertAlmostEqual(got.dividend_drag_r, 3.5 / 8.0)


class TradeLevelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _trade(self, *, entry, qty, avg, stop, provenance="recorded", symbol="AAA"):
        cur = self.conn.execute(
            "INSERT INTO trade (book, symbol, entry_date, entry_qty, "
            "entry_avg_price, status, stop, stop_provenance) "
            "VALUES ('US', ?, ?, ?, ?, 'closed', ?, ?)",
            (symbol, entry, qty, avg, stop, provenance))
        self.conn.commit()
        return cur.lastrowid

    def _exit(self, trade_id, exit_date, qty, price, reason=None):
        self.conn.execute(
            "INSERT INTO trade_exit (trade_id, source, source_ref, exit_date, "
            "quantity, price, reason) VALUES (?, 'ibkr', ?, ?, ?, ?, ?)",
            (trade_id, f"ref-{exit_date}", exit_date, qty, price, reason))
        self.conn.commit()

    def test_stopped_out_before_the_band_is_not_applicable_with_a_null_delta(self):
        # Stopped out on day 2 — the partial window (days 3–5) was never reached.
        bars = _bars([100, 90] + [80] * 60)
        tid = self._trade(entry=bars[0].date, qty=30, avg=100.0, stop=95.0)
        self._exit(tid, bars[1].date, 30, 90.0, reason="stop_hit")
        tc = cf.compute_trade(self.conn, tid, bars)
        self.assertEqual(tc.partial_state, cf.NOT_APPLICABLE)
        self.assertIsNone(tc.partial_timing_delta)

    def test_cap_nulls_trail_delta_and_deviation_cost_with_a_reason(self):
        # The nominal (ma10) variant never fires; both derived numbers null.
        closes = list(range(100, 100 + 80))
        bars = _bars(closes)
        tid = self._trade(entry=bars[0].date, qty=30, avg=100.0, stop=1.0)
        self._exit(tid, bars[10].date, 30, closes[10], reason="discretionary")
        tc = cf.compute_trade(self.conn, tid, bars)
        self.assertEqual(tc.nominal_status, cf.CAPPED)
        self.assertIsNone(tc.trail_exit_delta)
        self.assertIsNone(tc.deviation_cost())
        self.assertIsNone(tc.deviation_cost_r())

    def test_full_six_way_fit_vector_is_stored_and_best_fit_derived_on_read(self):
        closes = [100, 101, 102, 103, 104, 105] + [106] * 60
        bars = _bars(closes)
        tid = self._trade(entry=bars[0].date, qty=30, avg=100.0, stop=90.0)
        self._exit(tid, bars[5].date, 30, closes[5], reason="close_below_ma10")
        tc = cf.compute_trade(self.conn, tid, bars)
        self.assertEqual(set(tc.fit_vector.keys()), set(cf.VARIANTS))
        for d in tc.fit_vector.values():
            self.assertIsInstance(d, (int, float))
        self.assertIn(tc.best_fit(), cf.VARIANTS)

    def test_no_stop_trade_runs_trail_only_and_is_flagged_stopless(self):
        closes = [100] * 15 + [90] + [88] * 60
        bars = _bars(closes)
        tid = self._trade(entry=bars[10].date, qty=30, avg=100.0, stop=None,
                          provenance=None)
        self._exit(tid, bars[20].date, 30, 88.0, reason="close_below_ma10")
        tc = cf.compute_trade(self.conn, tid, bars)
        self.assertTrue(tc.stopless)
        for r in tc.variants:
            self.assertFalse(any(leg.trigger == cf.TRIGGER_STOP for leg in r.legs))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _trade(self, entry, symbol="AAA"):
        cur = self.conn.execute(
            "INSERT INTO trade (book, symbol, entry_date, entry_qty, "
            "entry_avg_price, status, stop, stop_provenance) "
            "VALUES ('US', ?, ?, 30, 100.0, 'closed', 90.0, 'recorded')",
            (symbol, entry))
        self.conn.commit()
        return cur.lastrowid

    def test_roundtrip_preserves_legs_deltas_and_fit(self):
        closes = [100, 101, 102, 103, 104, 105] + [106] * 60
        bars = _bars(closes)
        tid = self._trade(bars[0].date)
        self.conn.execute(
            "INSERT INTO trade_exit (trade_id, source, source_ref, exit_date, "
            "quantity, price, reason) VALUES (?, 'ibkr', 'r1', ?, 30, ?, "
            "'close_below_ma10')", (tid, bars[5].date, closes[5]))
        self.conn.commit()
        tc = cf.compute_trade(self.conn, tid, bars)
        store = cf.CounterfactualStore(self.conn)
        store.upsert(tid, tc)
        store.upsert(tid, tc)  # idempotent
        got = store.get(tid)
        self.assertEqual(got.ruleset_version, tc.ruleset_version)
        self.assertEqual(got.fit_vector, tc.fit_vector)
        self.assertEqual(len(got.variants), 6)
        n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM counterfactual_variant WHERE trade_id=?",
            (tid,)).fetchone()["c"]
        self.assertEqual(n, 6)


class RTierTest(unittest.TestCase):
    def test_three_tiers_and_the_aggregate_reports_every_excluded_count(self):
        recorded = cf.TradeR(realized_r=1.5, stop=90.0, provenance="recorded")
        reconstructed = cf.TradeR(realized_r=0.5, stop=90.0, provenance="reconstructed")
        absent = cf.TradeR(realized_r=None, stop=None, provenance=None)
        agg = cf.r_aggregate([recorded, reconstructed, absent])
        # R aggregates: recorded + reconstructed in, absent out (§10.6).
        self.assertEqual(agg.included, 2)
        self.assertEqual(agg.excluded_no_stop, 1)
        self.assertAlmostEqual(agg.mean, 1.0)
        # Adherence scoring: only recorded (§10.6).
        adh = cf.r_aggregate([recorded, reconstructed, absent], scope="adherence")
        self.assertEqual(adh.included, 1)
        self.assertEqual(adh.excluded_reconstructed, 1)
        self.assertEqual(adh.excluded_no_stop, 1)


if __name__ == "__main__":
    unittest.main()
