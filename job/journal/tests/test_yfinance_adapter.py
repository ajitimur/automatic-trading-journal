"""The one bar adapter: yfinance behind the seam, with retry and backoff.

The network is never touched here — the yfinance call is overridden so the
retry/backoff policy and the source label can be tested deterministically.
"""

import inspect
import unittest

from journal import yfinance_adapter
from journal.bars import Bar


class RecordingFetcher(yfinance_adapter.YFinanceFetcher):
    """A YFinanceFetcher whose one network method is replaced by a scripted
    sequence of outcomes, so the surrounding retry policy is what's tested."""

    def __init__(self, outcomes, **kw):
        super().__init__(**kw)
        self._outcomes = list(outcomes)
        self.download_calls = 0

    def _download_bars(self, symbol, start, end):
        self.download_calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class YFinanceAdapterTest(unittest.TestCase):
    def _sleeps(self):
        recorded = []
        return recorded, lambda s: recorded.append(s)

    def test_source_label_is_yfinance(self):
        self.assertEqual(yfinance_adapter.YFinanceFetcher().source, "yfinance")

    def test_retries_transient_failures_with_exponential_backoff(self):
        bars = [Bar("2026-07-01", 1, 1, 1, 1, 100)]
        sleeps, sleeper = self._sleeps()
        fetcher = RecordingFetcher(
            [RuntimeError("429"), RuntimeError("429"), bars],
            retries=3, backoff=1.0, sleep=sleeper,
        )
        got = fetcher.fetch("AAA", "2026-07-01", "2026-07-01")
        self.assertEqual(got, bars)
        self.assertEqual(fetcher.download_calls, 3)
        self.assertEqual(sleeps, [1.0, 2.0])  # exponential: 1, 2

    def test_exhausting_retries_reraises(self):
        sleeps, sleeper = self._sleeps()
        fetcher = RecordingFetcher(
            [RuntimeError("boom")] * 3, retries=3, backoff=0.5, sleep=sleeper,
        )
        with self.assertRaises(RuntimeError):
            fetcher.fetch("AAA", "2026-07-01", "2026-07-01")
        self.assertEqual(fetcher.download_calls, 3)

    def test_reads_unadjusted_with_actions(self):
        # auto_adjust=False keeps bars split-adjusted but dividend-unadjusted,
        # and actions=True brings dividends in the same call (§4.4). Guard the
        # source so the fetch options can't silently drift.
        source = inspect.getsource(yfinance_adapter)
        self.assertIn("auto_adjust=False", source)
        self.assertIn("actions=True", source)


if __name__ == "__main__":
    unittest.main()
