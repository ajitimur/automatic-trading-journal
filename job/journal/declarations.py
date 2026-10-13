"""The store of Stop Declaration events (ADR 0011, #49).

A **Stop Declaration** is a Stop stated for a symbol on a Book on a date, said at
the moment the stop-loss was placed, before the journal has seen the Fills it
belongs to (CONTEXT.md). Each one reaches the journal as a relay item; this
module keeps them as *events*, one row per item, never updated or deleted, so
the history of what the trader said and when stays reconstructible even once
later slices let a declaration replace or withdraw an earlier one.

Recording is idempotent on the relay's id. The relay keeps an item until it is
acknowledged, and can resend one Telegram retried, so the same item can arrive
on several runs; only the first lands.

Nothing here binds a declaration to a Trade or commits anything. That is a
later slice, which reads these events by the key they bind on:
``(book, symbol, entry_date)``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class StopDeclarationEvent:
    relay_id: str
    book: str
    symbol: str
    entry_date: str
    stop: float
    telegram_at: str
    recorded_at: str
    raw_ref: int


_COLUMNS = "relay_id, book, symbol, entry_date, stop, telegram_at, recorded_at, raw_ref"


class StopDeclarationStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(self, event: StopDeclarationEvent) -> bool:
        """Record ``event`` unless its relay id is already on record.

        Returns whether a row was added. Does not commit: the caller commits
        before it acknowledges the item to the relay.
        """
        cur = self._conn.execute(
            f"INSERT OR IGNORE INTO stop_declaration_event ({_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.relay_id,
                event.book,
                event.symbol,
                event.entry_date,
                event.stop,
                event.telegram_at,
                event.recorded_at,
                event.raw_ref,
            ),
        )
        return cur.rowcount == 1

    def for_key(self, book: str, symbol: str, entry_date: str) -> list[StopDeclarationEvent]:
        """Every declaration for one (book, symbol, entry date), oldest said first."""
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM stop_declaration_event "
            "WHERE book = ? AND symbol = ? AND entry_date = ? "
            "ORDER BY telegram_at, id",
            (book, symbol, entry_date),
        ).fetchall()
        return [_event(r) for r in rows]

    def all(self) -> list[StopDeclarationEvent]:
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM stop_declaration_event ORDER BY id"
        ).fetchall()
        return [_event(r) for r in rows]


def _event(row: sqlite3.Row) -> StopDeclarationEvent:
    return StopDeclarationEvent(
        relay_id=row["relay_id"],
        book=row["book"],
        symbol=row["symbol"],
        entry_date=row["entry_date"],
        stop=row["stop"],
        telegram_at=row["telegram_at"],
        recorded_at=row["recorded_at"],
        raw_ref=row["raw_ref"],
    )
