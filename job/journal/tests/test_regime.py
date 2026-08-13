"""RegimeSnapshot per (book, date) — SPEC §8, issue #30.

Regime is a property of a market on a date, not of a trade. These tests pin the
five-level label (top-down, no tunable parameter, identical for both books), the
sign-only slope over five trading days, the two market-level extras, the
prior-close stamping with an honest bar-date fallback, and the fact that the
label re-cuts from stored primitives with no refetch.
"""

import os
import tempfile
import unittest

from journal import db
from journal.bars import Bar, BarCache
from journal.regime import (
    BENCHMARKS,
    INSUFFICIENT_HISTORY,
    RegimeComputer,
    compute_snapshot,
    label_from_primitives,
    read_snapshot,
    recut_labels,
    store_snapshot,
)


def _bar(d, close, high=None, volume=1000):
    high = close if high is None else high
    return Bar(date=d, open=close, high=high, low=close, close=close,
               volume=volume)


def _series(closes, start_ordinal=1, step_up_high=0.0):
    """A run of daily bars with the given closes, dated 2025-01-<n> upward.

    Dates are synthetic and strictly increasing; only their order matters to the
    regime windows.
    """
    bars = []
    y, m, d = 2025, 1, 1
    import datetime as _dt
    day = _dt.date(2025, 1, 1)
    for i, c in enumerate(closes):
        bars.append(_bar(day.isoformat(), c, high=c + step_up_high))
        day += _dt.timedelta(days=1)
    return bars


class LabelRulesTest(unittest.TestCase):
    """§8.3 — the five rules, evaluated top-down, no tunable parameter."""

    def test_strong_uptrend_needs_all_three(self):
        self.assertEqual(
            label_from_primitives([True, True, True], [1, 1, 1]),
            "strong_uptrend",
        )

    def test_uptrend_two_of_three(self):
        self.assertEqual(
            label_from_primitives([True, True, False], [1, 1, -1]),
            "uptrend",
        )

    def test_strong_downtrend_needs_all_zero(self):
        self.assertEqual(
            label_from_primitives([False, False, False], [-1, -1, -1]),
            "strong_downtrend",
        )

    def test_downtrend_at_most_one(self):
        self.assertEqual(
            label_from_primitives([True, False, False], [1, -1, -1]),
            "downtrend",
        )

    def test_neutral_is_everything_else(self):
        # above = 2, rising = 1 -> not uptrend (rising < 2), not downtrend
        # (above > 1): the mixed middle.
        self.assertEqual(
            label_from_primitives([True, True, False], [1, -1, -1]),
            "neutral",
        )

    def test_strong_band_is_strict_not_ge(self):
        # above = 3 but only 2 rising is uptrend, never strong_uptrend.
        self.assertEqual(
            label_from_primitives([True, True, True], [1, 1, -1]),
            "uptrend",
        )

    def test_missing_primitive_is_insufficient_history(self):
        self.assertEqual(
            label_from_primitives([True, True, None], [1, 1, 1]),
            INSUFFICIENT_HISTORY,
        )


class ComputeSnapshotTest(unittest.TestCase):
    def test_a_clean_uptrend_labels_strong_uptrend(self):
        # A monotonically rising series: close above every MA, every MA rising.
        closes = [100.0 + i for i in range(60)]
        bars = _series(closes)
        # date one day past the last bar so the last bar is the prior close.
        as_of = "2025-04-01"
        snap = compute_snapshot(bars, "US", as_of)
        self.assertEqual(snap.label, "strong_uptrend")
        self.assertTrue(snap.above_ma10)
        self.assertTrue(snap.above_ma20)
        self.assertTrue(snap.above_ma50)
        self.assertEqual(snap.slope_ma10, 1)
        self.assertEqual(snap.slope_ma20, 1)
        self.assertEqual(snap.slope_ma50, 1)

    def test_a_clean_downtrend_labels_strong_downtrend(self):
        closes = [200.0 - i for i in range(60)]
        bars = _series(closes)
        snap = compute_snapshot(bars, "US", "2025-04-01")
        self.assertEqual(snap.label, "strong_downtrend")
        self.assertFalse(snap.above_ma10)
        self.assertEqual(snap.slope_ma10, -1)

    def test_slope_is_sign_only_over_five_trading_days(self):
        # Flat for a while then the last five bars tick up: the MA10 slope over
        # the last five trading days is positive, sign only.
        closes = [100.0] * 20 + [100.0 + i for i in range(1, 6)]
        bars = _series(closes)
        snap = compute_snapshot(bars, "US", "2025-04-01")
        self.assertEqual(snap.slope_ma10, 1)

    def test_prior_close_stamping_uses_the_last_bar_before_the_date(self):
        closes = [100.0 + i for i in range(60)]
        bars = _series(closes)  # last bar dated 2025-03-01
        # The as-of decision date is well past the last bar; the snapshot is
        # stamped as of the prior trading day's close, and records the bar used.
        snap = compute_snapshot(bars, "US", "2025-06-01")
        self.assertEqual(snap.date, "2025-06-01")
        self.assertEqual(snap.bar_date, bars[-1].date)

    def test_missing_bar_falls_back_and_the_as_of_date_stays_honest(self):
        closes = [100.0 + i for i in range(60)]
        bars = _series(closes)
        last = bars[-1].date
        # Ask for a date landing exactly on the day after the last bar: the bar
        # on the as-of date is missing, so it falls back to the last close and
        # records that bar_date — the as-of date itself does not slide.
        snap = compute_snapshot(bars, "US", "2025-05-15")
        self.assertEqual(snap.date, "2025-05-15")
        self.assertEqual(snap.bar_date, last)
        self.assertLess(snap.bar_date, snap.date)

    def test_pct_off_52w_high_null_under_252_bars(self):
        closes = [100.0 + i for i in range(60)]
        snap = compute_snapshot(_series(closes), "US", "2025-06-01")
        self.assertIsNone(snap.pct_off_52w_high)

    def test_pct_off_52w_high_zero_or_negative_at_the_high(self):
        # A long rising series: the prior close IS the 52-week high, so the
        # distance is zero (never positive, per §7.2).
        closes = [100.0 + i for i in range(300)]
        bars = _series(closes)
        snap = compute_snapshot(bars, "US", "2027-01-01")
        self.assertIsNotNone(snap.pct_off_52w_high)
        self.assertEqual(snap.pct_off_52w_high, 0.0)

    def test_realized_vol_is_present_and_nonnegative(self):
        closes = [100.0 + (i % 3) for i in range(60)]
        snap = compute_snapshot(_series(closes), "US", "2025-06-01")
        self.assertIsNotNone(snap.realized_vol_20d)
        self.assertGreaterEqual(snap.realized_vol_20d, 0.0)

    def test_a_flat_series_has_zero_realized_vol(self):
        snap = compute_snapshot(_series([100.0] * 60), "US", "2025-06-01")
        self.assertEqual(snap.realized_vol_20d, 0.0)

    def test_no_prior_bar_raises(self):
        bars = _series([100.0, 101.0])  # first bar 2025-01-01
        with self.assertRaises(ValueError):
            compute_snapshot(bars, "US", "2025-01-01")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_snapshot_persists_per_book_date(self):
        closes = [100.0 + i for i in range(60)]
        snap = compute_snapshot(_series(closes), "US", "2025-06-01")
        store_snapshot(self.conn, snap)
        got = read_snapshot(self.conn, "US", "2025-06-01")
        self.assertEqual(got.label, snap.label)
        self.assertEqual(got.bar_date, snap.bar_date)
        self.assertEqual(got.slope_ma10, snap.slope_ma10)

    def test_store_is_idempotent_on_book_date(self):
        snap = compute_snapshot(_series([100.0 + i for i in range(60)]),
                                "US", "2025-06-01")
        store_snapshot(self.conn, snap)
        store_snapshot(self.conn, snap)  # once more, same key
        rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM regime WHERE book='US' AND date='2025-06-01'"
        ).fetchone()
        self.assertEqual(rows["n"], 1)

    def test_relabel_recuts_from_stored_primitives_with_no_refetch(self):
        snap = compute_snapshot(_series([100.0 + i for i in range(60)]),
                                "US", "2025-06-01")
        store_snapshot(self.conn, snap)
        # Corrupt the stored label; a re-cut restores it purely from the six
        # stored primitives — no bars, no fetch involved.
        self.conn.execute(
            "UPDATE regime SET label = 'neutral' WHERE book='US' AND date='2025-06-01'"
        )
        self.conn.commit()
        n = recut_labels(self.conn)
        self.assertEqual(n, 1)
        self.assertEqual(read_snapshot(self.conn, "US", "2025-06-01").label,
                         "strong_uptrend")

    def test_trade_references_a_snapshot_and_copies_no_values(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(trade)")}
        # References by (book, date): the book is already on the trade, and the
        # two regime dates point at the snapshots.
        self.assertIn("entry_regime_date", cols)
        self.assertIn("exit_regime_date", cols)
        # The trade never carries a copy of a primitive or the label.
        for copied in ("label", "above_ma10", "slope_ma10", "pct_off_52w_high"):
            self.assertNotIn(copied, cols)


class BooksIndependentTest(unittest.TestCase):
    def test_benchmarks_are_per_book(self):
        self.assertEqual(BENCHMARKS["US"], "QQQ")
        self.assertEqual(BENCHMARKS["IDX"], "^JKSE")


class _FakeFetcher:
    source = "fake"

    def __init__(self, bars):
        self._bars = list(bars)
        self.calls = 0

    def fetch(self, symbol, start, end):
        self.calls += 1
        return list(self._bars)


class ComputerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_computer_reads_the_benchmark_from_the_cache_and_stores(self):
        closes = [100.0 + i for i in range(60)]
        bars = _series(closes)
        cache = BarCache(self.conn, _FakeFetcher(bars))
        cache.ensure("US", "QQQ", bars[0].date, bars[-1].date)
        computer = RegimeComputer(self.conn, cache)
        snap = computer.compute("US", "2025-06-01")
        self.assertEqual(snap.label, "strong_uptrend")
        self.assertEqual(read_snapshot(self.conn, "US", "2025-06-01").label,
                         "strong_uptrend")

    def test_us_regime_uses_only_the_us_benchmark(self):
        # Two books cached with different weather; the US snapshot must not fold
        # in any IDX term (§8.1 strict independence).
        up = _series([100.0 + i for i in range(60)])
        down = _series([200.0 - i for i in range(60)])
        cache = BarCache(self.conn, _FakeFetcher(up))
        cache.ensure("US", "QQQ", up[0].date, up[-1].date)
        cache = BarCache(self.conn, _FakeFetcher(down))
        cache.ensure("IDX", "^JKSE", down[0].date, down[-1].date)
        computer = RegimeComputer(self.conn, cache)
        self.assertEqual(computer.compute("US", "2025-06-01").label,
                         "strong_uptrend")
        self.assertEqual(computer.compute("IDX", "2025-06-01").label,
                         "strong_downtrend")


if __name__ == "__main__":
    unittest.main()
