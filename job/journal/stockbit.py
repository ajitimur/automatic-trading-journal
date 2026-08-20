"""Parse a Stockbit Trade Confirmation into Fills, gated by the fee identity
(SPEC §4.2/§5.6, issue #26).

No network and no API — none is permitted by the ToS (SPEC §13.2). The TC PDF is
hand-dropped; this module parses the ``pdftotext -layout`` text of one and lands
one Fill per execution row. The TC is the intake path (not the Statement of
Account) precisely because it **preserves individual fills**: fills of one order
share a ``REF #``, which the SoA collapses to a lossy weighted average.

Three facts drive the parsing, each easy to get silently wrong:

- **Shares are canonical from the ``Quantity`` column.** Both ``Lot`` and
  ``Quantity`` are printed (lot = 100 shares), so nothing is inferred; reading
  ``Lot`` where ``Quantity`` belongs is a silent 100× undercount.
- **The fee identity is a document-level gate** — the one place the two brokers
  visibly differ (SPEC §5.6). The printed per-side ``Total Cost`` is recomputed
  from the parsed rows (buy ``+0.15% + Rp10,000`` stamp duty; sell
  ``−0.15% − 0.10%`` tax). A shifted column breaks the identity and
  :func:`parse_tc_text` **quarantines the whole document before a single fill
  lands** — zero fills, not a best-effort partial import.
- **``source_ref`` is a content hash**, not a broker id — over confirmation
  date, symbol, side, quantity, price and the row's ordinal in the document, so
  re-dropping the same TC is idempotent (ADR 0003). ``order_id`` carries the
  ``REF #`` so the fills of one order stay linked.

Cost attaches at **day + side** here, not per fill (SPEC §7.0): the fee block is
itemised per side for the whole day, so a per-fill number would be an allocation,
not a fact. The per-fill ``commission`` is therefore left at 0 and the day+side
allocation is deferred to cost attribution (#27); the gate only *reconciles* the
printed totals, it does not smear them across fills.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime

from . import books

SOURCE = "stockbit"
BOOK = books.IDX

# Stockbit fills carry no restatement/version concept (unlike IBKR's exec seq):
# a corrected TC re-drops as a new content hash, never as a higher revision.
REVISION = 0

# The fee identity (docs/samples/export-findings.md), exact to the rupiah:
#   buy  Total Cost = gross + 0.15% bundle + stamp duty (flat, per document)
#   sell Total Cost = gross − 0.15% bundle − 0.10% income tax + stamp duty
_FEE_RATE = 0.0015           # Commission + V.A.T + IDX Fee + V.A.T Levy, both sides
_INCOME_TAX_RATE = 0.0010    # sell-side only

# **Stamp duty is read, never assumed.** The sample set showed it only on a
# buy-side document, and the parser inferred it from the side at a flat
# Rp10,000. Real statements contradict both halves: it is printed on *both*
# columns, it is charged on sell documents too, and on most days it is zero
# (3 of 34 statements from July–August 2026 carried it). Inferring it silently
# treated a real Rp10,000 charge as absent, which is exactly the kind of
# unstated fact the document-level gate exists to catch — so the printed line
# is the fact, and a document that does not print one is not parsed at all.

# The printed Total Cost is rounded to 2 decimals and the recompute uses exact
# rates, so allow a rupiah of slack. A column shift is off by a factor of ~100 —
# millions of rupiah — so this cleanly separates rounding noise from the gate.
_FEE_TOLERANCE = 1.0

# One execution row of the trade table (pdftotext -layout keeps it on one line):
#   REF #   Board  <TICKER> <name...>   Lot   Quantity   Price   Buy   Sell
# The five trailing numeric columns anchor the row from the right, so the
# free-text company name in the middle cannot be mistaken for a column.
_ROW = re.compile(
    r"^\s*(?P<ref>\d+)\s+(?P<board>[A-Z]+)\s+(?P<ticker>[A-Z0-9]+)\s+"
    r"(?P<name>.+?)\s+(?P<lot>[\d,]+)\s+(?P<qty>[\d,]+\.\d+)\s+"
    r"(?P<price>[\d,]+\.\d+)\s+(?P<buy>[\d,]+\.\d+)\s+(?P<sell>[\d,]+\.\d+)\s*$"
)

_TXN_DATE = re.compile(r"Transaction Date\s+(?P<d>\d{2}/\d{2}/\d{4})")

# The stamp duty line, both columns. A zero column prints as bare ``0`` while a
# charged one prints as ``10,000.00``, so the decimals are optional. Unlike
# ``Total Cost`` this is not anchored left: the fee block shares its lines with
# the address column, which already collides with ``Income Tax`` in real output.
_STAMP_DUTY_LINE = re.compile(
    r"Stamp Duty\s+(?P<buy>[\d,]+(?:\.\d+)?)\s+(?P<sell>[\d,]+(?:\.\d+)?)\s*$",
    re.MULTILINE,
)
_TOTAL_COST = re.compile(
    r"^\s*Total Cost\s+(?P<buy>[\d,]+\.\d+)\s+(?P<sell>[\d,]+\.\d+)\s*$",
    re.MULTILINE,
)


class StockbitError(Exception):
    """A Trade Confirmation that cannot be parsed as one."""


class QuarantineError(StockbitError):
    """The fee identity did not reconcile — the whole document is quarantined.

    Raised before any Fill is returned, so a caller lands zero fills (SPEC §5.6).
    A recomputed side that misses the printed ``Total Cost`` means a column has
    shifted (typically ``Lot`` read as ``Quantity``, a silent 100× error); the
    document is rejected wholesale rather than importing corrupt quantities.
    """


@dataclass(frozen=True)
class Fill:
    """One execution row of the TC — the append-only ledger unit (ADR 0003).

    The same shape the IBKR parser emits, so both brokers land in the one Fill
    ledger. ``commission`` is 0 because Stockbit's cost is day+side, not per fill
    (SPEC §7.0); ``order_id`` is the ``REF #`` shared by fills of one order.
    """

    source: str
    source_ref: str
    revision: int
    book: str
    symbol: str
    side: str          # 'BUY' | 'SELL'
    quantity: float    # signed: buy positive, sell negative
    price: float
    commission: float  # always 0 — Stockbit cost attaches at day+side (SPEC §7.0)
    executed_at: str   # ISO date; the TC carries no execution time
    order_id: str      # the REF #, shared by fills of one order


def extract_text(pdf_path: str) -> str:
    """Extract a TC PDF's text via ``pdftotext -layout`` (the one raw-doc seam).

    ``-layout`` keeps each execution row on a single line with its columns
    aligned, which is what :func:`parse_tc_text` reads. This is the only place
    the raw PDF is touched; it is never copied into the repo (SPEC §4.2, §13.2).
    Raises :class:`StockbitError` if ``pdftotext`` is absent or fails, so a
    missing tool is a stated error, not a silent empty parse.
    """
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:  # poppler's pdftotext not installed
        raise StockbitError(
            "pdftotext not found — install poppler to import a TC PDF"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise StockbitError(f"pdftotext failed on {pdf_path}: {exc.stderr}") from exc
    return result.stdout


def _number(text: str) -> float:
    """Parse a Rupiah figure like ``82,327,000.00`` — thousands separators out."""
    return float(text.replace(",", ""))


def _confirmation_date(text: str) -> str:
    """The Transaction Date as ISO ``YYYY-MM-DD`` (printed ``DD/MM/YYYY``)."""
    match = _TXN_DATE.search(text)
    if not match:
        raise StockbitError("no Transaction Date found — not a Trade Confirmation")
    return datetime.strptime(match.group("d"), "%d/%m/%Y").strftime("%Y-%m-%d")


def _content_ref(date: str, symbol: str, side: str, quantity: float,
                 price: float, ordinal: int) -> str:
    """A deterministic content hash over the fields that identify a fill (§4.2).

    Includes the ordinal so two rows that happen to match on every value still
    get distinct refs, and re-dropping the identical document reproduces them
    exactly — the idempotency key (ADR 0003).
    """
    payload = f"{date}|{symbol}|{side}|{quantity:.4f}|{price:.4f}|{ordinal}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class _Row:
    """A parsed trade-table row, before it becomes a signed Fill."""

    ref: str
    symbol: str
    side: str
    quantity: float    # positive, as printed in the Quantity column
    price: float


def _parse_rows(text: str) -> list[_Row]:
    rows: list[_Row] = []
    for line in text.splitlines():
        match = _ROW.match(line)
        if not match:
            continue
        buy = _number(match.group("buy"))
        sell = _number(match.group("sell"))
        # Exactly one side carries value; anything else is a malformed row.
        if (buy > 0) == (sell > 0):
            raise StockbitError(
                f"row {match.group('ref')} has neither or both of Buy/Sell set"
            )
        rows.append(
            _Row(
                ref=match.group("ref"),
                symbol=match.group("ticker"),
                side="BUY" if buy > 0 else "SELL",
                quantity=_number(match.group("qty")),
                price=_number(match.group("price")),
            )
        )
    if not rows:
        raise StockbitError("no execution rows found — not a Trade Confirmation")
    return rows


def _reconcile_or_quarantine(rows: list[_Row], text: str) -> None:
    """Recompute per-side ``Total Cost`` from the rows and gate on the printed one.

    The document-level tripwire (SPEC §5.6): if either side's recomputed cost
    misses the printed figure, the whole document is quarantined.
    """
    printed = _TOTAL_COST.search(text)
    if not printed:
        raise StockbitError("no Total Cost line found — cannot gate the document")
    printed_buy = _number(printed.group("buy"))
    printed_sell = _number(printed.group("sell"))

    stamp = _STAMP_DUTY_LINE.search(text)
    if not stamp:
        raise StockbitError(
            "no Stamp Duty line found — the charge is a stated fact on every "
            "Trade Confirmation and is never assumed from the side"
        )
    stamp_buy = _number(stamp.group("buy"))
    stamp_sell = _number(stamp.group("sell"))

    gross_buy = sum(r.quantity * r.price for r in rows if r.side == "BUY")
    gross_sell = sum(r.quantity * r.price for r in rows if r.side == "SELL")

    # Stamp duty *adds* to what a buy costs and *subtracts* from what a sell
    # pays out: it is a charge either way, and the sell column is proceeds.
    expected_buy = gross_buy * (1 + _FEE_RATE) + stamp_buy
    expected_sell = (
        gross_sell * (1 - _FEE_RATE - _INCOME_TAX_RATE) - stamp_sell
    )

    for side, expected, printed_total in (
        ("buy", expected_buy, printed_buy),
        ("sell", expected_sell, printed_sell),
    ):
        if abs(expected - printed_total) > _FEE_TOLERANCE:
            raise QuarantineError(
                f"{side} Total Cost does not reconcile: parsed rows imply "
                f"{expected:,.2f} but the document prints {printed_total:,.2f} — "
                f"a shifted column (Lot read as Quantity?) is the usual cause"
            )


def parse_tc_text(text: str) -> list[Fill]:
    """Parse a Trade Confirmation's ``pdftotext -layout`` text into Fills.

    The fee identity is checked *first*: a document that does not reconcile
    raises :class:`QuarantineError` and no Fills are returned (SPEC §5.6). Only a
    reconciled document produces one Fill per execution row.
    """
    date = _confirmation_date(text)
    rows = _parse_rows(text)
    _reconcile_or_quarantine(rows, text)

    fills = []
    for ordinal, row in enumerate(rows):
        signed = row.quantity if row.side == "BUY" else -row.quantity
        fills.append(
            Fill(
                source=SOURCE,
                source_ref=_content_ref(
                    date, row.symbol, row.side, signed, row.price, ordinal
                ),
                revision=REVISION,
                book=BOOK,
                symbol=row.symbol,
                side=row.side,
                quantity=signed,
                price=row.price,
                commission=0.0,
                executed_at=date,
                order_id=row.ref,
            )
        )
    return fills
