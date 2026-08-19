"""Filling the bar cache: what to fetch, and what a short series means (§4.4).

The fetcher is a fake throughout — these are tests of the *plan* (which symbols,
which windows, derived from the ledger) and of the one judgement call the pass
makes: telling a security that listed late apart from a series that is actually
broken.
"""

import os
import tempfile
import unittest
from datetime import date, timedelta

from journal import bar_sync, books, db
from journal.bars import Bar, BarCache


def _series(start: str, days: int, close: float = 100.0):
    """A contiguous daily series — every day a trading day, for arithmetic ease."""
    d0 = date.fromisoformat(start)
    return [
        Bar(date=(d0 + timedelta(days=i)).isoformat(), open=close, high=close,
            low=close, close=close, volume=1_000, dividend=0.0)
        for i in range(days)
    ]


class FakeFetcher:
    source = "fake"

    def __init__(self, table):
        # {(book, symbol): [Bar, ...]}; a missing key returns nothing at all.
        self._table = table
        self.calls = []

    def fetch(self, book, symbol, start, end):
        self.calls.append((book, symbol, start, end))
        bars = self._table.get((book, symbol), [])
        return [b for b in bars if start <= b.date <= end]


def _seed_trade(conn, book, symbol, entry_date):
    conn.execute(
        "INSERT INTO trade (book, symbol, entry_date, status) VALUES (?, ?, ?, 'open')",
        (book, symbol, entry_date),
    )
    conn.commit()


class PlannedWindowsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "j.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_benchmark_is_always_planned_even_with_no_trades(self):
        # The run's own gate reads the benchmark, so a book with an empty ledger
        # must still fetch it — otherwise nothing ever lifts the gate.
        windows = bar_sync.planned_windows(self.conn, books.US, "2026-08-19")
        self.assertEqual(len(windows), 1)
        symbol, start, end = windows[0]
        self.assertEqual(symbol, books.BENCHMARKS[books.US])
        self.assertLess(start, books.BACKDATING_FLOOR)
        self.assertEqual(end, "2026-08-19")

    def test_each_traded_symbol_gets_a_window_back_from_its_first_entry(self):
        _seed_trade(self.conn, books.US, "AAA", "2026-07-10")
        _seed_trade(self.conn, books.US, "AAA", "2026-08-01")  # later, ignored
        _seed_trade(self.conn, books.US, "BBB", "2026-08-05")

        windows = {s: (a, b) for s, a, b in
                   bar_sync.planned_windows(self.conn, books.US, "2026-08-19")}
        self.assertIn("AAA", windows)
        self.assertIn("BBB", windows)
        # The window starts a lookback before the EARLIEST entry, so the MA200
        # at that entry has history behind it.
        self.assertEqual(
            windows["AAA"][0],
            (date.fromisoformat("2026-07-10")
             - timedelta(days=bar_sync.SYMBOL_LOOKBACK_DAYS)).isoformat(),
        )

    def test_a_books_symbols_never_leak_into_the_other_book(self):
        _seed_trade(self.conn, books.IDX, "ADRO", "2026-07-10")
        us = [s for s, _, _ in bar_sync.planned_windows(self.conn, books.US, "2026-08-19")]
        self.assertNotIn("ADRO", us)


class SyncBookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "j.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _sync(self, table, as_of="2026-08-19"):
        cache = BarCache(self.conn, FakeFetcher(table))
        return bar_sync.sync_book(self.conn, books.US, as_of, cache), cache

    def test_a_covered_series_is_fetched_then_served_from_cache(self):
        bench = books.BENCHMARKS[books.US]
        table = {(books.US, bench): _series("2025-01-01", 900)}
        result, cache = self._sync(table)
        self.assertEqual([o.status for o in result.outcomes], ["fetched"])

        again = bar_sync.sync_book(self.conn, books.US, "2026-08-19", cache)
        self.assertEqual([o.status for o in again.outcomes], ["cached"])
        self.assertEqual(len(cache.fetcher.calls), 1)

    def test_a_late_listing_is_cached_from_its_first_bar_not_failed(self):
        # The security simply did not exist at the requested start. Failing it
        # would leave the symbol with NO bars rather than the shorter series it
        # genuinely has.
        bench = books.BENCHMARKS[books.US]
        _seed_trade(self.conn, books.US, "NEW", "2026-08-01")
        table = {
            (books.US, bench): _series("2025-01-01", 900),
            (books.US, "NEW"): _series("2026-06-01", 200),
        }
        result, _ = self._sync(table)
        by_symbol = {o.symbol: o for o in result.outcomes}
        self.assertEqual(by_symbol["NEW"].status, "short")
        self.assertIn("2026-06-01", by_symbol["NEW"].detail)
        self.assertEqual(result.errors, [])

        rows = self.conn.execute(
            "SELECT COUNT(*) c FROM bar WHERE book = ? AND symbol = 'NEW'",
            (books.US,),
        ).fetchone()["c"]
        self.assertGreater(rows, 0)

    def test_a_series_ending_yesterday_is_current_not_an_error(self):
        # The job runs before any close, so today's bar does not exist. This is
        # every symbol on every run — it must not read as a failure.
        bench = books.BENCHMARKS[books.US]
        _seed_trade(self.conn, books.US, "AAA", "2026-07-01")
        table = {
            (books.US, bench): _series("2025-01-01", 596),   # ends 2026-08-18
            (books.US, "AAA"): _series("2025-01-01", 596),
        }
        result, _ = self._sync(table, as_of="2026-08-19")
        self.assertEqual(result.errors, [])
        self.assertEqual({o.status for o in result.outcomes}, {"fetched"})

    def test_a_series_stale_beyond_the_tolerance_is_an_error(self):
        # Delisted, suspended, or a reused ticker: an end far behind as-of is
        # what the span check is really protecting, so this stays a failure.
        bench = books.BENCHMARKS[books.US]
        _seed_trade(self.conn, books.US, "GONE", "2026-07-01")
        table = {
            (books.US, bench): _series("2025-01-01", 900),
            (books.US, "GONE"): _series("2025-06-01", 200),  # ends 2025-12
        }
        result, _ = self._sync(table)
        by_symbol = {o.symbol: o for o in result.outcomes}
        self.assertEqual(by_symbol["GONE"].status, "error")
        self.assertEqual(len(result.errors), 1)

    def test_one_bad_symbol_never_stops_the_others(self):
        bench = books.BENCHMARKS[books.US]
        _seed_trade(self.conn, books.US, "GONE", "2026-07-01")
        _seed_trade(self.conn, books.US, "FINE", "2026-07-01")
        table = {
            (books.US, bench): _series("2025-01-01", 900),
            (books.US, "FINE"): _series("2025-01-01", 900),
            # GONE returns nothing at all.
        }
        result, _ = self._sync(table)
        by_symbol = {o.symbol: o.status for o in result.outcomes}
        self.assertEqual(by_symbol["GONE"], "error")
        self.assertEqual(by_symbol["FINE"], "fetched")
        self.assertIn("1 error", result.summary())


if __name__ == "__main__":
    unittest.main()
