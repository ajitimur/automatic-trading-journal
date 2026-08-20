"""The Fill ledger — append-only writes and reads (ADR 0003, issue #22).

Parsing lives in :mod:`journal.flex`; this module lands the parsed Fills in the
one SQLite file and reads them back for the UI. Writes are ``INSERT OR IGNORE``
on the ``(source, source_ref, revision)`` key, which makes re-dropping the same
file a no-op and lets a restatement (same key, higher revision) land beside the
earlier revision rather than replacing it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

from . import db, flex, stockbit, trades

_INSERT = """
INSERT OR IGNORE INTO fill
    (source, source_ref, revision, book, symbol, side, quantity, price,
     commission, executed_at, order_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def insert_fills(conn: sqlite3.Connection, records: Iterable[flex.Fill]) -> int:
    """Append Fills, ignoring any whose key is already present.

    Returns the number of rows actually inserted (0 when every Fill was a
    duplicate), so callers can report an idempotent re-drop as a visible no-op.
    """
    inserted = 0
    for f in records:
        cur = conn.execute(
            _INSERT,
            (
                f.source,
                f.source_ref,
                f.revision,
                f.book,
                f.symbol,
                f.side,
                f.quantity,
                f.price,
                f.commission,
                f.executed_at,
                f.order_id,
            ),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def import_flex_text(
    conn: sqlite3.Connection, xml_text: str, *, fetched_at: Optional[str] = None
) -> int:
    """Parse a Flex XML string and append its Fills to the ledger.

    The single body path for both a file on disk and a statement just fetched
    over the wire. Returns the number newly inserted; raises
    :class:`flex.FlexError` on an error body rather than a statement (SPEC §4.1).
    Remembered symbol rules are applied here, before anything reaches the confirm
    queue (SPEC §5.4).

    The statement is recorded in the keep-forever tier, which is what gives the
    US intake nag something to read (SPEC §11.4). Recorded *before* parsing, for
    the reason the archive keeps it: an error body is exactly what a later fix
    must be re-run over, and "the fetch ran and returned junk" is a different
    fact from "the fetch never ran".
    """
    db.record_raw_document(
        conn,
        book="US",
        kind="flex-trades-xml",
        fetched_at=fetched_at or datetime.now(timezone.utc).isoformat(),
        content=xml_text,
    )
    return insert_fills(conn, trades.apply_symbol_rules(conn, list(flex.parse_flex(xml_text))))


def import_flex_file(conn: sqlite3.Connection, path: str) -> int:
    """Parse a Flex XML file on disk and append its Fills to the ledger.

    Returns the number newly inserted. Raises :class:`flex.FlexError` if the
    file is an error body rather than a statement (SPEC §4.1).
    """
    with open(path, encoding="utf-8") as fh:
        return import_flex_text(conn, fh.read())


def import_stockbit_text(
    conn: sqlite3.Connection, tc_text: str, *, fetched_at: Optional[str] = None
) -> int:
    """Parse a Stockbit TC's extracted text and append its Fills (issue #26).

    The fee-identity gate runs inside :func:`stockbit.parse_tc_text`, so a
    document that does not reconcile raises :class:`stockbit.QuarantineError`
    before any row is returned — nothing lands. Returns the number newly
    inserted; the content-hash ``source_ref`` makes re-dropping the same TC a
    no-op (SPEC §4.2). Remembered symbol rules are applied before insert, so a
    misparse fixed once is corrected on every future drop (SPEC §5.4).

    The TC is recorded in the keep-forever tier **before** it is parsed, for the
    same reason the filesystem archive keeps it (SPEC §13.5): a quarantined
    document is exactly what a later parser fix must be re-run over. It also
    means the intake nag reports *"you dropped something"* rather than
    *"you forgot to drop"* on a day the drop landed and quarantined — the two
    are different problems and the quarantine already announces itself loudly.
    """
    db.record_raw_document(
        conn,
        book=stockbit.BOOK,
        kind="stockbit-tc",
        fetched_at=fetched_at or datetime.now(timezone.utc).isoformat(),
        content=tc_text,
    )
    return insert_fills(conn, trades.apply_symbol_rules(conn, stockbit.parse_tc_text(tc_text)))


def import_stockbit_file(
    conn: sqlite3.Connection, path: str, *, fetched_at: Optional[str] = None
) -> int:
    """Import a hand-dropped Stockbit TC, from a PDF or its extracted text.

    A ``.pdf`` is run through ``pdftotext -layout`` first (the one place this
    module touches the raw document); a ``.txt`` is taken as already-extracted
    layout text, which is what the tests and the offline path use. The raw PDF
    itself is never copied into the repo (SPEC §4.2, §13.2).
    """
    if path.lower().endswith(".pdf"):
        text = stockbit.extract_text(path)
    else:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    return import_stockbit_text(conn, text, fetched_at=fetched_at)
