"""The bar cache: the market-neutral seam, the zero-volume invariant, the
span-check hard gate, and cache-served reads (SPEC §4.4, ADR 0005)."""

import os
import pathlib
import tempfile
import unittest

from journal import db
from journal.bars import Bar, BarCache, SpanCheckError, span_check


class FakeFetcher:
    """A market-neutral fetcher standing in for the yfinance adapter. Counts
    calls so a test can prove the cache serves without refetching."""

    source = "fake"

    def __init__(self, bars):
        self._bars = list(bars)
        self.calls = 0

    def fetch(self, symbol, start, end):
        self.calls += 1
        return list(self._bars)


def _bar(d, close=100.0, volume=1000, dividend=0.0):
    return Bar(date=d, open=close, high=close, low=close, close=close,
               volume=volume, dividend=dividend)


class SpanCheckTest(unittest.TestCase):
    def test_empty_series_fails(self):
        check = span_check([], "2026-07-01", "2026-07-10")
        self.assertFalse(check.ok)

    def test_series_covering_the_range_passes(self):
        raw = [_bar("2026-07-01"), _bar("2026-07-10")]
        check = span_check(raw, "2026-07-02", "2026-07-09")
        self.assertTrue(check.ok)

    def test_wrong_instrument_not_covering_the_start_fails(self):
        # A reused ticker returns rows of an unrelated instrument that only
        # lists after the Trade's dates — must fail, not silently pass.
        raw = [_bar("2026-08-01"), _bar("2026-08-05")]
        check = span_check(raw, "2026-07-01", "2026-07-10")
        self.assertFalse(check.ok)

    def test_zero_volume_boundary_day_counts_as_present(self):
        # The required end lands on a suspended (zero-volume) day. It is
        # present, not missing — the span check must still pass.
        raw = [_bar("2026-07-01"), _bar("2026-07-10", volume=0)]
        check = span_check(raw, "2026-07-01", "2026-07-10")
        self.assertTrue(check.ok)
        self.assertEqual(check.zero_volume_filtered, 1)


class BarCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "journal.db")
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_zero_volume_rows_filtered_at_the_boundary(self):
        raw = [
            _bar("2026-07-01", volume=1000),
            _bar("2026-07-02", volume=0),   # suspension
            _bar("2026-07-03", volume=1000),
        ]
        cache = BarCache(self.conn, FakeFetcher(raw))
        check = cache.ensure("US", "AAA", "2026-07-01", "2026-07-03")

        # The filtered day is gone from what any consumer reads...
        got = cache.read("US", "AAA", "2026-07-01", "2026-07-03")
        self.assertEqual([b.date for b in got], ["2026-07-01", "2026-07-03"])
        # ...but its count is visible in the diagnostics, not an error.
        self.assertEqual(check.zero_volume_filtered, 1)

    def test_filtered_count_appears_in_fetch_diagnostics(self):
        raw = [_bar("2026-07-01"), _bar("2026-07-02", volume=0), _bar("2026-07-03")]
        BarCache(self.conn, FakeFetcher(raw)).ensure("IDX", "BBB.JK",
                                                     "2026-07-01", "2026-07-03")
        row = self.conn.execute(
            "SELECT * FROM bar_fetch WHERE symbol = 'BBB.JK'").fetchone()
        self.assertEqual(row["zero_volume_filtered"], 1)
        self.assertEqual(row["span_ok"], 1)

    def test_every_fetch_records_fetch_date_source_and_span_result(self):
        raw = [_bar("2026-07-01"), _bar("2026-07-03")]
        fetcher = FakeFetcher(raw)
        BarCache(self.conn, fetcher).ensure("US", "AAA", "2026-07-01", "2026-07-03")
        row = self.conn.execute("SELECT * FROM bar_fetch").fetchone()
        self.assertIsNotNone(row["fetch_date"])
        self.assertEqual(row["source"], "fake")
        self.assertEqual(row["span_ok"], 1)

    def test_empty_result_raises_repair_demanding_error_and_is_recorded(self):
        cache = BarCache(self.conn, FakeFetcher([]))
        with self.assertRaises(SpanCheckError):
            cache.ensure("US", "TWTR", "2026-07-01", "2026-07-10")
        # The failed fetch is still recorded as data (§4.4).
        row = self.conn.execute(
            "SELECT * FROM bar_fetch WHERE symbol = 'TWTR'").fetchone()
        self.assertEqual(row["span_ok"], 0)

    def test_wrong_instrument_result_raises_repair_demanding_error(self):
        raw = [_bar("2026-08-01"), _bar("2026-08-05")]  # unrelated instrument
        cache = BarCache(self.conn, FakeFetcher(raw))
        with self.assertRaises(SpanCheckError):
            cache.ensure("US", "REUSED", "2026-07-01", "2026-07-10")
        # Nothing from the wrong instrument leaks into the cache.
        got = cache.read("US", "REUSED", "2026-01-01", "2026-12-31")
        self.assertEqual(got, [])

    def test_cache_serves_subsequent_reads_with_no_refetch(self):
        raw = [_bar("2026-07-01"), _bar("2026-07-02"), _bar("2026-07-03")]
        fetcher = FakeFetcher(raw)
        cache = BarCache(self.conn, fetcher)
        cache.ensure("US", "AAA", "2026-07-01", "2026-07-03")
        cache.ensure("US", "AAA", "2026-07-01", "2026-07-03")  # fully cached
        self.assertEqual(fetcher.calls, 1)
        self.assertEqual(len(cache.read("US", "AAA", "2026-07-01", "2026-07-03")), 3)

    def test_dividend_arrives_with_the_bar(self):
        raw = [_bar("2026-07-01"), _bar("2026-07-02", dividend=0.24)]
        cache = BarCache(self.conn, FakeFetcher(raw))
        cache.ensure("US", "AAA", "2026-07-01", "2026-07-02")
        got = {b.date: b.dividend for b in
               cache.read("US", "AAA", "2026-07-01", "2026-07-02")}
        self.assertEqual(got["2026-07-02"], 0.24)


class SeamTest(unittest.TestCase):
    def test_only_the_adapter_imports_yfinance(self):
        # Everything above the fetch layer speaks the market-neutral interface;
        # no consumer imports yfinance directly (§4.4).
        pkg = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for path in pkg.rglob("*.py"):
            if path.name == "yfinance_adapter.py" or path.parent.name == "tests":
                continue
            if "import yfinance" in path.read_text():
                offenders.append(path.name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
