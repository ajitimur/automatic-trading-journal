"""Section F — the post-exit counterfactual window, the freeze trigger, and drift
(SPEC §3.5/§3.6/§7.5, issue #34).

The post-exit window is **20 trading days beginning the day after the final exit
date**, baselined on ``C_x``. Its fields are **null until the window completes,
and completing it *is* the freeze trigger** — no second clock. At freeze the
hand-entered fields lock and the derived values snapshot, while staying
recomputable so a later disagreement is detectable as **drift**. Drift carries a
cause: a **broker restatement** may be applied (superseded snapshot retained); a
**revised bar series** is acknowledge-only and can never overwrite. A
``written_off`` Exit freezes immediately with post-exit and all variants
``not_applicable``. The fuse counts **traded days**, so a suspension stretches it
in calendar time rather than burning through it.
"""

import os
import tempfile
import unittest
from datetime import date, timedelta

from journal import db
from journal.bars import Bar
from journal import post_exit
from journal.post_exit import (
    NOT_APPLICABLE,
    WINDOW,
    PostExit,
    PostExitStore,
    compute_post_exit,
    detect_bar_drift,
    freeze_sweep,
    is_window_complete,
    remaining_fuse,
    settle,
    written_off_post_exit,
)
from journal import stops, trades


def _series(n, base=100.0, step=1.0, spread=0.02, volume=1000, start="2026-01-01"):
    """n consecutive daily bars (oldest first); High/Low ±spread/2 around close."""
    d0 = date.fromisoformat(start)
    bars = []
    for i in range(n):
        close = base + step * i
        bars.append(Bar(
            date=(d0 + timedelta(days=i)).isoformat(),
            open=close, high=close * (1 + spread / 2), low=close * (1 - spread / 2),
            close=close, volume=volume, dividend=0.0,
        ))
    return bars


class WindowCompletionTest(unittest.TestCase):
    def test_null_until_twenty_trading_days_after_the_final_exit(self):
        # 19 trading days after the exit is not enough — the window is incomplete
        # and every field is null (compute returns None, not a zero-filled row).
        bars = _series(1 + 19, start="2026-03-02")  # exit bar + 19 after
        exit_date = bars[0].date
        self.assertIsNone(compute_post_exit(bars, exit_date, 100.0))
        self.assertFalse(is_window_complete(bars, exit_date))

    def test_the_twentieth_trading_day_completes_the_window(self):
        bars = _series(1 + 20, start="2026-03-02")  # exit bar + 20 after
        exit_date = bars[0].date
        pe = compute_post_exit(bars, exit_date, 100.0)
        self.assertIsNotNone(pe)
        self.assertTrue(is_window_complete(bars, exit_date))

    def test_fwd_fields_baseline_on_cx_the_final_exit_close(self):
        # close = 100 + i; exit bar is the first (close 100 = C_x); the window is
        # the next 20 bars, C_20 = close of the 20th (100 + 20 = 120).
        bars = _series(1 + 20, base=100.0, step=1.0, start="2026-03-02")
        exit_date = bars[0].date
        pe = compute_post_exit(bars, exit_date, 99.5)
        self.assertAlmostEqual(pe.cx, 100.0)
        self.assertAlmostEqual(pe.fwd_close_20d, 120.0)
        self.assertAlmostEqual(pe.fwd_return_20d, (120.0 / 100.0 - 1) * 100)
        self.assertAlmostEqual(pe.exit_avg_price, 99.5)  # sits beside C_x
        # highest High / lowest Low fall on the last / first window bar here.
        self.assertEqual(pe.fwd_high_date, bars[20].date)
        self.assertEqual(pe.fwd_low_date, bars[1].date)


class FuseCountsTradedDaysTest(unittest.TestCase):
    def test_a_suspension_stretches_the_fuse_in_calendar_time(self):
        # The fuse counts *bars* (traded days), never calendar days. A gap in the
        # series — a suspension — leaves the remaining count untouched.
        after = _series(10, start="2026-03-03")               # 10 traded days
        gap = _series(5, start="2026-05-01")                   # resumes weeks later
        exit_bar = _series(1, start="2026-03-02")
        bars = exit_bar + after + gap
        exit_date = exit_bar[0].date
        # 15 traded days on record → 5 remain, regardless of the calendar gap.
        self.assertEqual(remaining_fuse(bars, exit_date), WINDOW - 15)
        self.assertFalse(is_window_complete(bars, exit_date))


class WrittenOffTest(unittest.TestCase):
    def test_written_off_records_not_applicable_everywhere(self):
        pe = written_off_post_exit("2026-03-02", 4.0)
        self.assertTrue(pe.not_applicable)
        self.assertEqual(pe.marker("fwd_return_20d"), NOT_APPLICABLE)
        self.assertEqual(pe.marker("fwd_high"), NOT_APPLICABLE)
        self.assertIsNone(pe.fwd_return_20d)
        self.assertEqual(pe.exit_avg_price, 4.0)  # hand-entered residual price


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.connect(os.path.join(self.tmp, "j.db"))

    def _trade_with_exit(self, *, symbol="AAA", entry="2026-02-01",
                         exit_date="2026-03-02", reason="close_below_ma10",
                         exit_price=110.0):
        cur = self.conn.execute(
            "INSERT INTO trade (book, symbol, entry_date, entry_qty, "
            "entry_avg_price, status) VALUES ('US', ?, ?, 100, 100.0, 'closed')",
            (symbol, entry),
        )
        trade_id = cur.lastrowid
        self.conn.execute(
            "INSERT INTO trade_exit (trade_id, source, source_ref, exit_date, "
            "quantity, price, reason) VALUES (?, 'ibkr', ?, ?, 100, ?, ?)",
            (trade_id, f"ref-{symbol}", exit_date, exit_price, reason),
        )
        self.conn.commit()
        return trade_id


class SnapshotStoreTest(_StoreCase):
    def test_snapshot_round_trips_and_stays_recomputable(self):
        bars = _series(1 + 20, start="2026-03-02")
        exit_date = bars[0].date
        pe = compute_post_exit(bars, exit_date, 100.0)
        tid = self._trade_with_exit()
        store = PostExitStore(self.conn)
        store.snapshot(tid, pe)
        got = store.get(tid)
        self.assertAlmostEqual(got.fwd_return_20d, pe.fwd_return_20d)
        # recompute from the same bars still yields the same numbers.
        again = compute_post_exit(bars, exit_date, 100.0)
        self.assertAlmostEqual(again.fwd_return_20d, got.fwd_return_20d)


class FreezeTriggerTest(_StoreCase):
    def test_completing_the_window_freezes_the_trade(self):
        tid = self._trade_with_exit(exit_date="2026-03-02")
        bars = _series(1 + 20, start="2026-03-02")
        froze = settle(self.conn, tid, bars)
        self.assertTrue(froze)
        self.assertEqual(
            self.conn.execute("SELECT frozen FROM trade WHERE id=?", (tid,)).fetchone()["frozen"], 1)
        self.assertIsNotNone(PostExitStore(self.conn).get(tid))

    def test_an_incomplete_window_does_not_freeze(self):
        tid = self._trade_with_exit(exit_date="2026-03-02")
        bars = _series(1 + 5, start="2026-03-02")
        self.assertFalse(settle(self.conn, tid, bars))
        self.assertEqual(
            self.conn.execute("SELECT frozen FROM trade WHERE id=?", (tid,)).fetchone()["frozen"], 0)

    def test_written_off_exit_freezes_immediately(self):
        tid = self._trade_with_exit(exit_date="2026-03-02", reason="written_off")
        bars = _series(1 + 2, start="2026-03-02")  # nowhere near 20 days
        self.assertTrue(settle(self.conn, tid, bars))
        self.assertEqual(
            self.conn.execute("SELECT frozen FROM trade WHERE id=?", (tid,)).fetchone()["frozen"], 1)
        pe = PostExitStore(self.conn).get(tid)
        self.assertTrue(pe.not_applicable)


class FreezeSweepTest(_StoreCase):
    def _cache_bars(self, symbol, bars):
        for b in bars:
            self.conn.execute(
                "INSERT INTO bar (book, symbol, date, open, high, low, close, "
                "volume, dividend) VALUES ('US', ?, ?, ?, ?, ?, ?, ?, 0)",
                (symbol, b.date, b.open, b.high, b.low, b.close, b.volume),
            )
        self.conn.commit()

    def test_only_landed_windows_freeze_and_the_pass_is_idempotent(self):
        # AAA's window has landed (20 bars after exit); BBB's has not (5 bars).
        ripe = self._trade_with_exit(symbol="AAA", exit_date="2026-03-02")
        green = self._trade_with_exit(symbol="BBB", exit_date="2026-03-02")
        self._cache_bars("AAA", _series(1 + 20, start="2026-03-02"))
        self._cache_bars("BBB", _series(1 + 5, start="2026-03-02"))

        froze = freeze_sweep(self.conn)
        self.assertEqual(froze, [ripe])
        # A second pass freezes nothing new and writes no second snapshot.
        self.assertEqual(freeze_sweep(self.conn), [])
        self.assertEqual(len(PostExitStore(self.conn).history(ripe)), 1)
        self.assertEqual(
            self.conn.execute("SELECT frozen FROM trade WHERE id=?", (green,)).fetchone()["frozen"], 0)


class DriftTest(_StoreCase):
    def test_broker_restatement_may_overwrite_retaining_the_superseded(self):
        bars = _series(1 + 20, start="2026-03-02")
        exit_date = bars[0].date
        tid = self._trade_with_exit()
        store = PostExitStore(self.conn)
        original = compute_post_exit(bars, exit_date, 100.0)
        store.snapshot(tid, original)
        # A broker restates the final exit price; exit_avg_price is corrected.
        corrected = compute_post_exit(bars, exit_date, 111.0)
        store.apply_restatement(tid, corrected)
        self.assertAlmostEqual(store.get(tid).exit_avg_price, 111.0)  # corrected on top
        prices = [r.exit_avg_price for r in store.history(tid)]
        self.assertIn(100.0, prices)                                  # superseded kept
        self.assertIn(111.0, prices)
        self.assertEqual(store.get(tid).cause, post_exit.BROKER_RESTATEMENT)

    def test_revised_bars_surface_as_acknowledge_only_drift(self):
        bars = _series(1 + 20, base=100.0, start="2026-03-02")
        exit_date = bars[0].date
        stored = compute_post_exit(bars, exit_date, 100.0)
        # Yahoo re-serves the window with different highs — a revised bar series.
        revised = _series(1 + 20, base=100.0, step=1.0, spread=0.20, start="2026-03-02")
        drift = detect_bar_drift(stored, revised, exit_date)
        self.assertIsNotNone(drift)
        self.assertEqual(drift.cause, post_exit.REVISED_BAR_SERIES)
        self.assertFalse(drift.applicable)   # acknowledge only, never applied

    def test_no_drift_when_the_bars_are_unchanged(self):
        bars = _series(1 + 20, start="2026-03-02")
        exit_date = bars[0].date
        stored = compute_post_exit(bars, exit_date, 100.0)
        self.assertIsNone(detect_bar_drift(stored, bars, exit_date))


if __name__ == "__main__":
    unittest.main()
