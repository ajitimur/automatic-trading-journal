"""The Fill ledger — append-only writes and reads (ADR 0003, issue #22).

Parsing lives in :mod:`journal.flex`; this module lands the parsed Fills in the
one SQLite file and reads them back for the UI. Writes are ``INSERT OR IGNORE``
on the ``(source, source_ref, revision)`` key, which makes re-dropping the same
file a no-op and lets a restatement (same key, higher revision) land beside the
earlier revision rather than replacing it.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable

from . import flex

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


def import_flex_text(conn: sqlite3.Connection, xml_text: str) -> int:
    """Parse a Flex XML string and append its Fills to the ledger.

    The single body path for both a file on disk and a statement just fetched
    over the wire. Returns the number newly inserted; raises
    :class:`flex.FlexError` on an error body rather than a statement (SPEC §4.1).
    """
    return insert_fills(conn, flex.parse_flex(xml_text))


def import_flex_file(conn: sqlite3.Connection, path: str) -> int:
    """Parse a Flex XML file on disk and append its Fills to the ledger.

    Returns the number newly inserted. Raises :class:`flex.FlexError` if the
    file is an error body rather than a statement (SPEC §4.1).
    """
    with open(path, encoding="utf-8") as fh:
        return import_flex_text(conn, fh.read())
