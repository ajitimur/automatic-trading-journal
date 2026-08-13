"""The bar cache and its market-neutral seam (SPEC §4.4, ADR 0005).

Everything above the fetch layer speaks the :class:`BarFetcher` interface here
rather than calling ``yfinance`` directly. The seam is what protects against
"Yahoo broke and we need anything else by Friday" — but **no second adapter is
written speculatively**; that restraint is explicit (§4.4). The one adapter
lives in :mod:`journal.yfinance_adapter`.

Four load-bearing rules live at this boundary:

* **A zero-volume row is not a trading day** (ADR 0005). A suspension arrives as
  a row — price flat at the prior close, volume zero — not a gap. It is filtered
  here, before any consumer sees it, uniformly across both books. Left in place
  it deflates ``adr_pct``, collapses realized volatility, shortens the moving
  averages in real terms and lets a trail signal fire on a day nothing traded.
* **The span check is a hard gate.** A series that does not cover the dates it
  must raises a repair-demanding :class:`SpanCheckError`, never "no data, skip".
  This catches both an empty result (a visible loss) and a reused ticker
  returning rows of an unrelated instrument (silent corruption). A filtered
  zero-volume day is counted as *present*, not missing.
* **The cache is part of the design, not an optimisation.** Bars fetched once
  are stored; a covered range serves without refetch. If Yahoo blocks,
  enrichment stops but nothing computed is lost and the journal still opens.
* **Every fetch records** ``fetch_date``, ``source`` and its span-check result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Protocol, Sequence, Tuple


@dataclass(frozen=True)
class Bar:
    """One daily bar, market-neutral.

    Split-adjusted but dividend-unadjusted (``auto_adjust=False``); ``dividend``
    is the cash distribution paid on ``date`` if any, arriving in the same fetch
    (``actions=True``), and zero otherwise.
    """

    date: str  # ISO trading day, YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: int
    dividend: float = 0.0


class BarFetcher(Protocol):
    """The market-neutral fetch seam.

    The yfinance adapter is the only implementation; no second adapter is
    written speculatively (§4.4). Anything above the fetch layer depends on this
    Protocol, never on yfinance.
    """

    source: str

    def fetch(self, symbol: str, start: str, end: str) -> Sequence[Bar]:
        ...


class SpanCheckError(Exception):
    """A fetched series does not cover the dates it must.

    A corruption risk demanding manual repair — never silently downgraded to
    "no data, skip enrichment" (§4.4).
    """


@dataclass(frozen=True)
class SpanCheck:
    """The result of the span-check hard gate, recorded on every fetch."""

    ok: bool
    requested_start: str
    requested_end: str
    covered_start: Optional[str]
    covered_end: Optional[str]
    rows_fetched: int
    zero_volume_filtered: int
    detail: str


def trading_days(raw: Sequence[Bar]) -> List[Bar]:
    """Keep only the days something actually traded (ADR 0005).

    A zero-volume row is a suspension, not a trading day, and every window the
    journal measures counts the days that remain.
    """
    return [b for b in raw if b.volume > 0]


def span_check(raw: Sequence[Bar], requested_start: str, requested_end: str) -> SpanCheck:
    """Judge whether ``raw`` covers ``[requested_start, requested_end]``.

    Coverage is decided over the *raw* dates — a filtered zero-volume day is
    present, not missing — so a suspension on a boundary date does not read as a
    gap. An empty series and a series whose dates fall outside the required
    range (the reused-ticker case) both fail.
    """
    present = sorted({b.date for b in raw})
    zero_volume = sum(1 for b in raw if b.volume == 0)
    rows = len(raw)

    if not present:
        return SpanCheck(
            ok=False,
            requested_start=requested_start,
            requested_end=requested_end,
            covered_start=None,
            covered_end=None,
            rows_fetched=rows,
            zero_volume_filtered=zero_volume,
            detail="empty series — no bars returned",
        )

    covered_start, covered_end = present[0], present[-1]
    ok = covered_start <= requested_start and covered_end >= requested_end
    if ok:
        detail = (
            f"covers {requested_start}..{requested_end}"
            f" ({zero_volume} zero-volume day(s) filtered)"
        )
    else:
        detail = (
            f"series {covered_start}..{covered_end} does not cover required "
            f"{requested_start}..{requested_end} — repair required"
        )
    return SpanCheck(
        ok=ok,
        requested_start=requested_start,
        requested_end=requested_end,
        covered_start=covered_start,
        covered_end=covered_end,
        rows_fetched=rows,
        zero_volume_filtered=zero_volume,
        detail=detail,
    )


class BarCache:
    """The local bar cache over the one SQLite file.

    Reads the cache; the daily job fills it. A range already covered serves
    without a refetch, so if the fetcher's source blocks, enrichment stops but
    nothing already stored is lost.
    """

    def __init__(self, conn, fetcher: BarFetcher) -> None:
        self.conn = conn
        self.fetcher = fetcher

    def _cached_span(self, book: str, symbol: str) -> Tuple[Optional[str], Optional[str]]:
        row = self.conn.execute(
            "SELECT MIN(date) AS lo, MAX(date) AS hi FROM bar "
            "WHERE book = ? AND symbol = ?",
            (book, symbol),
        ).fetchone()
        return (row["lo"], row["hi"])

    def ensure(self, book: str, symbol: str, start: str, end: str) -> SpanCheck:
        """Ensure trading-day bars covering ``[start, end]`` are cached.

        Returns the :class:`SpanCheck`. When the cache already covers the range
        the fetcher is not called (§4.4). On a fetch, the raw series is
        span-checked, the result recorded, zero-volume rows filtered, and the
        remaining trading days stored. A failed span check is recorded and then
        raised as a :class:`SpanCheckError`.
        """
        lo, hi = self._cached_span(book, symbol)
        if lo is not None and lo <= start and hi >= end:
            return SpanCheck(
                ok=True,
                requested_start=start,
                requested_end=end,
                covered_start=lo,
                covered_end=hi,
                rows_fetched=0,
                zero_volume_filtered=0,
                detail="served from cache",
            )

        raw = list(self.fetcher.fetch(symbol, start, end))
        check = span_check(raw, start, end)
        self._record_fetch(book, symbol, check)
        if not check.ok:
            raise SpanCheckError(check.detail)

        self._store(book, symbol, trading_days(raw))
        return check

    def read(self, book: str, symbol: str, start: str, end: str) -> List[Bar]:
        """Return cached trading-day bars in ``[start, end]``, oldest first.

        Zero-volume rows were filtered before storage, so a consumer never sees
        one here.
        """
        rows = self.conn.execute(
            "SELECT date, open, high, low, close, volume, dividend FROM bar "
            "WHERE book = ? AND symbol = ? AND date >= ? AND date <= ? "
            "ORDER BY date",
            (book, symbol, start, end),
        ).fetchall()
        return [
            Bar(
                date=r["date"],
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
                dividend=r["dividend"],
            )
            for r in rows
        ]

    def _store(self, book: str, symbol: str, bars: Sequence[Bar]) -> None:
        self.conn.executemany(
            "INSERT INTO bar (book, symbol, date, open, high, low, close, "
            "volume, dividend) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(book, symbol, date) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, "
            "close=excluded.close, volume=excluded.volume, "
            "dividend=excluded.dividend",
            [
                (book, symbol, b.date, b.open, b.high, b.low, b.close,
                 b.volume, b.dividend)
                for b in bars
            ],
        )
        self.conn.commit()

    def _record_fetch(self, book: str, symbol: str, check: SpanCheck) -> None:
        # Recorded (and committed) whether or not the span check passed, so a
        # failed fetch survives as data even though ``ensure`` then raises.
        self.conn.execute(
            "INSERT INTO bar_fetch (book, symbol, fetch_date, source, "
            "requested_start, requested_end, covered_start, covered_end, "
            "rows_fetched, zero_volume_filtered, span_ok, span_detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                book,
                symbol,
                datetime.now(timezone.utc).date().isoformat(),
                getattr(self.fetcher, "source", "unknown"),
                check.requested_start,
                check.requested_end,
                check.covered_start,
                check.covered_end,
                check.rows_fetched,
                check.zero_volume_filtered,
                1 if check.ok else 0,
                check.detail,
            ),
        )
        self.conn.commit()
