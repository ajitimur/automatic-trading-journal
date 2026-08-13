"""RegimeSnapshot per (book, date): the six primitives, the top-down label,
the two extras, prior-close stamping, and storage (SPEC §8, issue #30)."""

import os
import tempfile
import unittest
from datetime import date, timedelta

from journal import db
from journal.bars import Bar
from journal.regime import (
    DOWNTREND,
    NEUTRAL,
    STRONG_DOWNTREND,
    STRONG_UPTREND,
    UPTREND,
    RegimeStore,
    compute_snapshot,
    derive_label,
)


def _series(n, base=100.0, step=1.0, start="2026-01-01"):
    """A benchmark series of n bars, close = base + step*i (oldest first).

    Sequential calendar dates — the module only needs an ordered, comparable
    date axis, not a real trading calendar.
    """
    d0 = date.fromisoformat(start)
    bars = []
    for i in range(n):
        close = base + step * i
        bars.append(Bar(
            date=(d0 + timedelta(days=i)).isoformat(),
            open=close, high=close, low=close, close=close,
            volume=1000, dividend=0.0,
        ))
    return bars


class DeriveLabelTest(unittest.TestCase):
    def test_strong_uptrend_is_strict(self):
        self.assertEqual(derive_label(3, 3), STRONG_UPTREND)
        # One notch off either primitive drops to plain uptrend, never strong.
        self.assertEqual(derive_label(3, 2), UPTREND)
        self.assertEqual(derive_label(2, 3), UPTREND)

    def test_strong_downtrend_is_strict(self):
        self.assertEqual(derive_label(0, 0), STRONG_DOWNTREND)
        self.assertEqual(derive_label(1, 0), DOWNTREND)
        self.assertEqual(derive_label(0, 1), DOWNTREND)

    def test_uptrend_and_downtrend_bands(self):
        self.assertEqual(derive_label(2, 2), UPTREND)
        self.assertEqual(derive_label(1, 1), DOWNTREND)

    def test_neutral_is_everything_else(self):
        self.assertEqual(derive_label(2, 1), NEUTRAL)
        self.assertEqual(derive_label(3, 0), NEUTRAL)
        self.assertEqual(derive_label(1, 2), NEUTRAL)


class SlopeTest(unittest.TestCase):
    def test_slope_is_sign_of_five_day_change(self):
        up = compute_snapshot("US", "2026-04-01", _series(80, step=1.0))
        self.assertEqual((up.slope_ma10, up.slope_ma20, up.slope_ma50), (1, 1, 1))

        down = compute_snapshot("US", "2026-04-01", _series(80, base=200.0, step=-1.0))
        self.assertEqual((down.slope_ma10, down.slope_ma20, down.slope_ma50), (-1, -1, -1))

    def test_flat_ma_has_zero_slope_no_flat_zone(self):
        snap = compute_snapshot("US", "2026-04-01", _series(80, step=0.0))
        self.assertEqual((snap.slope_ma10, snap.slope_ma20, snap.slope_ma50), (0, 0, 0))


class ComputeSnapshotTest(unittest.TestCase):
    def test_strong_uptrend_all_six_primitives(self):
        snap = compute_snapshot("US", "2026-06-01", _series(300, step=1.0))
        self.assertEqual(snap.label, STRONG_UPTREND)
        self.assertTrue(snap.close_above_ma10)
        self.assertTrue(snap.close_above_ma20)
        self.assertTrue(snap.close_above_ma50)
        self.assertEqual(snap.slope_ma10, 1)
        self.assertEqual(snap.slope_ma20, 1)
        self.assertEqual(snap.slope_ma50, 1)

    def test_strong_downtrend(self):
        snap = compute_snapshot("US", "2026-06-01", _series(300, base=500.0, step=-1.0))
        self.assertEqual(snap.label, STRONG_DOWNTREND)
        self.assertFalse(snap.close_above_ma10)
        self.assertEqual(snap.slope_ma10, -1)

    def test_two_extras_computed(self):
        snap = compute_snapshot("US", "2026-11-01", _series(300, step=1.0))
        # Ascending series: last close is the max → distance from 52w high is 0.
        self.assertIsNotNone(snap.pct_off_52w_high)
        self.assertLessEqual(snap.pct_off_52w_high, 0.0)
        self.assertIsNotNone(snap.realized_vol_20d)
        self.assertGreater(snap.realized_vol_20d, 0.0)

    def test_both_books_use_the_rule_identically(self):
        bars = _series(300, step=1.0)
        us = compute_snapshot("US", "2026-06-01", bars)
        idx = compute_snapshot("IDX", "2026-06-01", bars)
        self.assertEqual(us.label, idx.label)
        self.assertEqual(us.book, "US")
        self.assertEqual(idx.book, "IDX")

    def test_insufficient_history_nulls_the_label_but_keeps_primitives(self):
        # 30 bars: MA10/MA20 exist, MA50 cannot — label is null, not guessed.
        snap = compute_snapshot("US", "2026-06-01", _series(30, step=1.0))
        self.assertIsNone(snap.label)
        self.assertIsNone(snap.close_above_ma50)
        self.assertIsNone(snap.slope_ma50)
        self.assertIsNotNone(snap.close_above_ma10)
        self.assertIsNone(snap.pct_off_52w_high)  # < 252 bars

    def test_no_prior_bar_returns_none(self):
        bars = _series(10, start="2026-06-01")
        self.assertIsNone(compute_snapshot("US", "2026-01-01", bars))


class StampingTest(unittest.TestCase):
    def test_stamped_as_of_prior_trading_day_close(self):
        # A bar exists on the key date itself; the stamp must ignore it and use
        # the last completed bar before it (the entry day's close is unknown).
        bars = _series(60, start="2026-03-01")
        key = bars[-1].date
        snap = compute_snapshot("US", key, bars)
        self.assertEqual(snap.bar_date, bars[-2].date)
        self.assertLess(snap.bar_date, key)

    def test_missing_bar_falls_back_and_records_the_bar_date_used(self):
        # No bar on the key date or the days just before it (holiday / backdated
        # entry). The stamp falls back to the last available close and records
        # which bar it actually used, so the as-of date stays honest.
        bars = _series(60, start="2026-03-01")
        last = date.fromisoformat(bars[-1].date)
        key = (last + timedelta(days=7)).isoformat()
        snap = compute_snapshot("US", key, bars)
        self.assertEqual(snap.bar_date, bars[-1].date)


class RegimeStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_upsert_and_get_roundtrip_per_book_date(self):
        snap = compute_snapshot("US", "2026-06-01", _series(300, step=1.0))
        store = RegimeStore(self.conn)
        store.upsert(snap)
        got = store.get("US", "2026-06-01")
        self.assertEqual(got.label, snap.label)
        self.assertEqual(got.bar_date, snap.bar_date)
        self.assertEqual(got.close_above_ma50, snap.close_above_ma50)
        self.assertEqual(got.slope_ma20, snap.slope_ma20)
        self.assertAlmostEqual(got.realized_vol_20d, snap.realized_vol_20d)

    def test_upsert_is_idempotent_on_book_date(self):
        store = RegimeStore(self.conn)
        store.upsert(compute_snapshot("US", "2026-06-01", _series(300, step=1.0)))
        store.upsert(compute_snapshot("US", "2026-06-01", _series(300, step=1.0)))
        n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM regime_snapshot").fetchone()["c"]
        self.assertEqual(n, 1)

    def test_label_recut_from_stored_primitives_needs_no_refetch(self):
        # Store the snapshot, drop the bars, and re-cut the label from the
        # stored primitives alone — it must match (SPEC §8.3).
        store = RegimeStore(self.conn)
        store.upsert(compute_snapshot("US", "2026-06-01", _series(300, step=1.0)))
        loaded = store.get("US", "2026-06-01")
        self.assertEqual(loaded.relabel(), STRONG_UPTREND)


if __name__ == "__main__":
    unittest.main()
