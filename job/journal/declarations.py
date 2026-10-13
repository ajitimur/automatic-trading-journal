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

    def live(
        self, exclude_relay_ids: "set[str] | None" = None
    ) -> dict[tuple[str, str, str], StopDeclarationEvent]:
        """The one live declaration per key, ``(book, symbol, entry_date) -> event``.

        A key can carry several events once a resend replaces an earlier one
        (#51); the *latest said* — the greatest ``telegram_at`` — is the live one.
        ``exclude_relay_ids`` drops any already bound to a Trade, so a run that
        already committed a declaration does not look at it again (the binding is
        what makes auto-commit idempotent, ADR 0012).
        """
        exclude = exclude_relay_ids or set()
        latest: dict[tuple[str, str, str], StopDeclarationEvent] = {}
        for e in self.all():  # id-ascending, so ties on telegram_at keep the newer
            key = (e.book, e.symbol, e.entry_date)
            current = latest.get(key)
            if current is None or e.telegram_at >= current.telegram_at:
                latest[key] = e
        return {k: e for k, e in latest.items() if e.relay_id not in exclude}


class StopDeclarationBindingStore:
    """Which Stop Declaration bound to which Trade (ADR 0012, #50).

    One row per declaration event that bound and committed a Trade. Recording is
    idempotent on the relay id: a declaration the relay still holds, or one a
    re-run re-derives, binds at most once.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(self, relay_id: str, trade_id: int, bound_at: str) -> bool:
        """Record that ``relay_id`` bound ``trade_id``. Does not commit."""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO stop_declaration_binding "
            "(relay_id, trade_id, bound_at) VALUES (?, ?, ?)",
            (relay_id, trade_id, bound_at),
        )
        return cur.rowcount == 1

    def bound_relay_ids(self) -> set[str]:
        return {
            r["relay_id"]
            for r in self._conn.execute(
                "SELECT relay_id FROM stop_declaration_binding"
            )
        }


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
