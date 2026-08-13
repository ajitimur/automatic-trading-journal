"""Entry-dated setup enrichment: ADR% the normalizer, the SMA stack, setup
geometry, the prior-close anchor with volume_ratio's flagged exception, the
insufficient-history markers, and storage (SPEC §7.1–§7.2, issue #29)."""

import os
import tempfile
import unittest
from datetime import date, timedelta

from journal import db
from journal.bars import Bar
from journal.enrichment import (
    ALIGNED_DOWN,
    ALIGNED_UP,
    INSUFFICIENT_HISTORY,
    MIXED,
    EnrichmentStore,
    compute_entry_enrichment,
)


def _series(n, base=100.0, step=1.0, spread=0.02, volume=1000, start="2026-01-01"):
    """A symbol series of n bars, close = base + step*i (oldest first).

    ``spread`` sets High/Low as ±spread/2 around the close so ADR% is non-zero
    (a flat high==low series would make ADR% zero and every ADR-unit field
    degenerate). Sequential calendar dates — the module needs an ordered date
    axis, not a real trading calendar.
    """
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


class AnchorTest(unittest.TestCase):
    def test_anchors_on_prior_close_ignoring_entry_day_bar(self):
        # A bar exists on the entry date; the anchor must be the last completed
        # bar strictly before it (the entry day's close is post-decision).
        bars = _series(60, start="2026-03-01")
        entry = bars[-1].date
        enr = compute_entry_enrichment("US", "AAA", entry, bars, [])
        self.assertEqual(enr.bar_date, bars[-2].date)
        self.assertLess(enr.bar_date, entry)

    def test_no_prior_bar_returns_none(self):
        bars = _series(10, start="2026-06-01")
        self.assertIsNone(compute_entry_enrichment("US", "AAA", "2026-01-01", bars, []))


class AdrPctTest(unittest.TestCase):
    def test_adr_pct_is_mean_daily_range_over_20_bars(self):
        # Constant 2% spread → every bar's (high/low - 1)*100 is the same, so the
        # 20-bar mean is that value.
        bars = _series(60, spread=0.02)
        enr = compute_entry_enrichment("US", "AAA", bars[-1].date, bars, [])
        one = (bars[0].high / bars[0].low - 1) * 100
        self.assertAlmostEqual(enr.adr_pct, one)


class MaStackTest(unittest.TestCase):
    def test_all_five_smas_and_signed_distance_in_adr_units(self):
        bars = _series(300, base=100.0, step=1.0)
        enr = compute_entry_enrichment("US", "AAA", bars[-1].date, bars, [])
        p = bars[-2].close  # P₋₁
        closes = [b.close for b in bars[:-1]]
        for n, ma in ((10, enr.ma_10), (20, enr.ma_20), (50, enr.ma_50),
                      (100, enr.ma_100), (200, enr.ma_200)):
            self.assertAlmostEqual(ma, sum(closes[-n:]) / n)
        # Rising series → P₋₁ is above every MA → all distances positive.
        expected_10 = (p - enr.ma_10) / p * 100 / enr.adr_pct
        self.assertAlmostEqual(enr.ma_dist_10, expected_10)
        self.assertGreater(enr.ma_dist_10, 0)
        self.assertGreater(enr.ma_dist_200, 0)

    def test_stack_state_aligned_up_and_down(self):
        up = compute_entry_enrichment("US", "AAA", _series(300, step=1.0)[-1].date,
                                      _series(300, step=1.0), [])
        self.assertEqual(up.stack_state, ALIGNED_UP)
        down_bars = _series(300, base=500.0, step=-1.0)
        down = compute_entry_enrichment("US", "AAA", down_bars[-1].date, down_bars, [])
        self.assertEqual(down.stack_state, ALIGNED_DOWN)

    def test_stack_state_mixed_when_not_monotone(self):
        # A long uptrend then a sharp short pullback: the recent drop pulls MA10
        # below MA20 while the long MAs are still stacked up, so the ordering is
        # neither strictly ascending nor strictly descending.
        rising = _series(250, base=100.0, step=1.0)
        pullback = _series(20, base=rising[-1].close, step=-4.0,
                           start=(date.fromisoformat(rising[-1].date)
                                  + timedelta(days=1)).isoformat())
        bars = rising + pullback
        enr = compute_entry_enrichment("US", "AAA", bars[-1].date, bars, [])
        self.assertEqual(enr.stack_state, MIXED)


class PriorMoveTest(unittest.TestCase):
    def test_prior_move_is_close_to_close_over_trading_days(self):
        bars = _series(200, base=100.0, step=1.0)
        enr = compute_entry_enrichment("US", "AAA", bars[-1].date, bars, [])
        closes = [b.close for b in bars[:-1]]
        p = closes[-1]
        self.assertAlmostEqual(enr.prior_move_21d, (p / closes[-1 - 21] - 1) * 100)
        self.assertAlmostEqual(enr.prior_move_63d, (p / closes[-1 - 63] - 1) * 100)
        self.assertAlmostEqual(enr.prior_move_126d, (p / closes[-1 - 126] - 1) * 100)


class Rs63dTest(unittest.TestCase):
    def test_rs_is_symbol_move_minus_benchmark_move(self):
        # Symbol climbs faster than the benchmark → positive relative strength.
        symbol = _series(200, base=100.0, step=2.0)
        benchmark = _series(200, base=100.0, step=1.0)
        enr = compute_entry_enrichment("US", "AAA", symbol[-1].date, symbol, benchmark)
        bench_enr = compute_entry_enrichment("US", "QQQ", benchmark[-1].date,
                                             benchmark, [])
        self.assertAlmostEqual(
            enr.rs_63d, enr.prior_move_63d - bench_enr.prior_move_63d)
        self.assertGreater(enr.rs_63d, 0)

    def test_rs_null_when_benchmark_too_short(self):
        symbol = _series(200)
        benchmark = _series(40)  # < 64 bars, cannot form a 63-day move
        enr = compute_entry_enrichment("US", "AAA", symbol[-1].date, symbol, benchmark)
        self.assertIsNone(enr.rs_63d)
        self.assertEqual(enr.marker("rs_63d"), INSUFFICIENT_HISTORY)


class VolumeRatioTest(unittest.TestCase):
    def test_volume_ratio_uses_entry_day_volume_not_prior_close(self):
        # 50 prior bars at volume 1000; the entry-day bar carries volume 3000.
        prior = _series(60, volume=1000, start="2026-01-01")
        entry_day = date.fromisoformat(prior[-1].date) + timedelta(days=1)
        entry_bar = Bar(date=entry_day.isoformat(), open=200, high=204, low=196,
                        close=200, volume=3000)
        bars = prior + [entry_bar]
        enr = compute_entry_enrichment("US", "AAA", entry_bar.date, bars, [])
        # Denominator is the 50 completed bars before entry, all volume 1000.
        self.assertAlmostEqual(enr.volume_ratio, 3.0)
        # And it is the only field on the entry day's bar — the anchor is still
        # the prior close.
        self.assertEqual(enr.bar_date, prior[-1].date)


class AvgTurnoverTest(unittest.TestCase):
    def test_avg_turnover_is_native_currency_close_times_volume(self):
        bars = _series(60, base=100.0, step=0.0, volume=2000)
        enr = compute_entry_enrichment("US", "AAA", bars[-1].date, bars, [])
        self.assertAlmostEqual(enr.avg_turnover_20d, 100.0 * 2000)


class InsufficientHistoryTest(unittest.TestCase):
    def test_under_20_bars_nulls_adr_and_every_adr_normalized_field(self):
        bars = _series(15)  # < 20 completed bars once the entry day is excluded
        enr = compute_entry_enrichment("US", "AAA", bars[-1].date, bars, [])
        self.assertIsNone(enr.adr_pct)
        self.assertEqual(enr.marker("adr_pct"), INSUFFICIENT_HISTORY)
        for field, value in (("ma_dist_10", enr.ma_dist_10),
                             ("ma_dist_20", enr.ma_dist_20),
                             ("ma_dist_50", enr.ma_dist_50)):
            self.assertIsNone(value)
            self.assertEqual(enr.marker(field), INSUFFICIENT_HISTORY)

    def test_ma_null_propagates_to_dist_and_stack_state(self):
        # 30 bars: MA10/MA20 exist, MA50/100/200 cannot — stack_state is null.
        bars = _series(31)
        enr = compute_entry_enrichment("US", "AAA", bars[-1].date, bars, [])
        self.assertIsNotNone(enr.ma_10)
        self.assertIsNone(enr.ma_50)
        self.assertEqual(enr.marker("ma_50"), INSUFFICIENT_HISTORY)
        self.assertIsNone(enr.ma_dist_50)
        self.assertEqual(enr.marker("ma_dist_50"), INSUFFICIENT_HISTORY)
        self.assertIsNone(enr.stack_state)
        self.assertEqual(enr.marker("stack_state"), INSUFFICIENT_HISTORY)

    def test_pct_off_52w_high_null_under_252_bars(self):
        bars = _series(100)
        enr = compute_entry_enrichment("US", "AAA", bars[-1].date, bars, [])
        self.assertIsNone(enr.pct_off_52w_high)
        self.assertEqual(enr.marker("pct_off_52w_high"), INSUFFICIENT_HISTORY)

    def test_marker_is_only_set_for_history_nulls(self):
        # A fully-historied Trade carries no markers at all.
        bars = _series(300)
        enr = compute_entry_enrichment("US", "AAA", bars[-1].date, bars, bars)
        self.assertEqual(enr.insufficient_history, frozenset())
        self.assertIsNone(enr.marker("adr_pct"))

    def test_pct_off_52w_high_present_at_252_bars(self):
        bars = _series(253)  # exactly 252 completed bars before the entry day
        enr = compute_entry_enrichment("US", "AAA", bars[-1].date, bars, [])
        self.assertIsNotNone(enr.pct_off_52w_high)
        self.assertLessEqual(enr.pct_off_52w_high, 0.0)


class EnrichmentStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _trade(self, trade_id, symbol="AAA", entry="2026-10-01"):
        # trade_enrichment.trade_id is a foreign key into trade(id); land the
        # parent Trade first so the enrichment has something to hang on.
        self.conn.execute(
            "INSERT INTO trade (id, book, symbol, entry_date) VALUES (?, ?, ?, ?)",
            (trade_id, "US", symbol, entry),
        )
        self.conn.commit()

    def test_upsert_and_get_roundtrip(self):
        bars = _series(300)
        enr = compute_entry_enrichment("US", "AAA", bars[-1].date, bars, bars)
        self._trade(1)
        store = EnrichmentStore(self.conn)
        store.upsert(1, enr)
        got = store.get(1)
        self.assertEqual(got.stack_state, enr.stack_state)
        self.assertAlmostEqual(got.adr_pct, enr.adr_pct)
        self.assertAlmostEqual(got.ma_dist_200, enr.ma_dist_200)
        self.assertAlmostEqual(got.rs_63d, enr.rs_63d)
        self.assertEqual(got.bar_date, enr.bar_date)

    def test_markers_survive_storage(self):
        # A short-history Trade: its insufficient_history set must round-trip so
        # the *reason* for each null is not lost — and stays distinguishable from
        # a span-check failure (which never lands a row here at all).
        bars = _series(31)
        enr = compute_entry_enrichment("US", "AAA", bars[-1].date, bars, [])
        self._trade(7)
        store = EnrichmentStore(self.conn)
        store.upsert(7, enr)
        got = store.get(7)
        self.assertEqual(got.insufficient_history, enr.insufficient_history)
        self.assertEqual(got.marker("stack_state"), INSUFFICIENT_HISTORY)

    def test_upsert_is_idempotent_on_trade_id(self):
        bars = _series(300)
        enr = compute_entry_enrichment("US", "AAA", bars[-1].date, bars, bars)
        self._trade(1)
        store = EnrichmentStore(self.conn)
        store.upsert(1, enr)
        store.upsert(1, enr)
        n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM trade_enrichment").fetchone()["c"]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
