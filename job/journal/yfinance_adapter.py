"""The one bar adapter: yfinance behind the market-neutral seam (SPEC §4.4).

This module is the **only** place that imports ``yfinance``. The seam exists to
make a second adapter possible by Friday if Yahoo breaks — not to maintain two
now — so **no second adapter is written speculatively** (§4.4). Everything above
the fetch layer depends on :class:`journal.bars.BarFetcher`, never on this.

yfinance's cookie/crumb session handling is load-bearing: a hand-rolled HTTP
client against Yahoo gets a 429 on the first request, so the library does the
fetching. Bars are read ``auto_adjust=False`` (split-adjusted,
dividend-unadjusted) with ``actions=True`` so dividends arrive in the same call.
Transient failures — Yahoo throttling, a dropped connection — are retried with
exponential backoff before giving up.

The import of ``yfinance`` is deferred to the moment of a real fetch: the
package is a heavy, network-facing dependency, and the rest of the pipeline (and
its tests) must import and run without it installed.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Callable, List, Sequence

from .bars import Bar

SOURCE = "yfinance"

# Yahoo's market suffix per book (docs/research/ohlcv-sources.md): IDX equities
# are ``.JK``, US equities are bare. Index tickers already arrive in Yahoo's own
# form (``^JKSE``, ``QQQ``) and are passed through untouched — appending a
# suffix to ``^JKSE`` would silently request a security that does not exist.
_SUFFIX = {"IDX": ".JK", "US": ""}


def vendor_symbol(book: str, symbol: str) -> str:
    """The Yahoo ticker for a journal ``symbol`` on ``book``."""
    if symbol.startswith("^"):
        return symbol
    return symbol + _SUFFIX.get(book, "")


class YFinanceFetcher:
    """A :class:`journal.bars.BarFetcher` backed by the yfinance library."""

    source = SOURCE

    def __init__(
        self,
        retries: int = 3,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.retries = max(1, retries)
        self.backoff = backoff
        self._sleep = sleep

    def fetch(
        self, book: str, symbol: str, start: str, end: str
    ) -> Sequence[Bar]:
        """Fetch daily bars for ``symbol`` over ``[start, end]`` (inclusive).

        ``book`` selects the vendor ticker: the journal stores the symbol the
        broker prints, and Yahoo wants a market suffix on IDX names.

        Retries transient failures with exponential backoff; re-raises the last
        error once the retries are exhausted, leaving the span-check/repair
        decision to the caller.
        """
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            try:
                return self._download_bars(vendor_symbol(book, symbol), start, end)
            except Exception as exc:  # noqa: BLE001 — any failure is retryable
                last_exc = exc
                if attempt + 1 < self.retries:
                    self._sleep(self.backoff * (2 ** attempt))
        assert last_exc is not None
        raise last_exc

    def _download_bars(self, symbol: str, start: str, end: str) -> List[Bar]:
        """The single network call, isolated so the retry policy is testable.

        yfinance treats ``end`` as exclusive, so it is bumped one day to include
        the requested end date.
        """
        import yfinance  # deferred: the seam's one dependency, not needed to import

        end_exclusive = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
        frame = yfinance.Ticker(symbol).history(
            start=start,
            end=end_exclusive,
            auto_adjust=False,
            actions=True,
        )
        return _frame_to_bars(frame)


def _frame_to_bars(frame) -> List[Bar]:
    """Convert a yfinance history DataFrame into market-neutral bars.

    Kept trivial and dependency-light: the daily index becomes the ISO date and
    the columns map straight across, with dividends defaulting to zero when the
    ``actions=True`` column is absent.
    """
    bars: List[Bar] = []
    for index, row in frame.iterrows():
        bars.append(
            Bar(
                date=index.date().isoformat(),
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=int(row["Volume"]),
                dividend=float(row["Dividends"]) if "Dividends" in row else 0.0,
            )
        )
    return bars
