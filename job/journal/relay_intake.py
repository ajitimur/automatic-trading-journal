"""The relay intake pass: pull, archive, record, then acknowledge (ADR 0011, #49).

The relay holds what the trader sent the bot until the Mac pulls it. Each run
takes everything it holds and, in this order:

1. files every item verbatim in the keep-forever raw tier, both halves (the
   filesystem archive and ``raw_document``), because a declaration is a source
   document like a Flex statement or a TC (SPEC §13.5);
2. records each declaration as a :class:`~journal.declarations.DeclarationEvent`;
3. commits;
4. and only then acknowledges, which is what lets the relay delete the items.

Acknowledging last is the durability rule. A run that dies anywhere before the
acknowledgement leaves the relay holding everything, so the next run pulls the
same items again, and both the archive and the event store ignore an item they
already hold. Losing an acknowledgement costs a re-pull; acknowledging early
could cost a Stop.

An item that is not a declaration (a grammar this job predates) is archived and
acknowledged but not recorded: the raw tier keeps it, so a later job can replay
it. A *declaration* missing a field it needs is archived but not acknowledged,
and the pass reports it as an error. It stays at the relay rather than being
dropped, and the run record says the wire contract has drifted.

Nothing binds or commits a Trade here.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Protocol, Sequence

from . import backup, db
from .declarations import DeclarationEvent, DeclarationStore

# The raw-tier kind every relay item is filed under.
RELAY_ITEM_KIND = "relay-item"

# The book a relay item is filed under when it names none (an `/undo`, say).
NO_BOOK = "RELAY"


class Relay(Protocol):
    """The relay seam (see :mod:`journal.relay_client` for the real one)."""

    def pull(self) -> list[dict]:
        ...

    def acknowledge(self, ids: Sequence[str]) -> None:
        ...


class RelayIntakeError(Exception):
    """The pass did part of its work and could not finish it.

    The message leads with what *was* done, so the run record still states how
    many items were pulled and archived on a run whose acknowledgement failed.
    """


@dataclass(frozen=True)
class IntakeCounts:
    pulled: int
    archived: int
    recorded: int

    def summary(self) -> str:
        return (
            f"{self.pulled} item(s) pulled, {self.archived} archived, "
            f"{self.recorded} declaration(s) recorded"
        )


def take_in(
    conn: sqlite3.Connection, relay: Relay, *, archive_dir: str, now: str
) -> str:
    """Run the pass against ``relay`` and return the run-record detail.

    Raises whatever the relay raises on pull (nothing has happened yet), and
    :class:`RelayIntakeError` when something fails after items were archived.
    """
    items = relay.pull()
    store = DeclarationStore(conn)
    to_ack: list[str] = []
    malformed: list[str] = []
    archived = recorded = 0

    for item in items:
        relay_id = str(item["id"])
        raw_ref = _archive(conn, item, archive_dir=archive_dir, now=now)
        archived += 1
        parse = item.get("parse") or {}
        if parse.get("kind") != "declaration":
            to_ack.append(relay_id)
            continue
        try:
            event = _declaration(item, parse, raw_ref=raw_ref, now=now)
        except (KeyError, TypeError, ValueError) as exc:
            malformed.append(f"{relay_id} ({exc!r})")
            continue
        if store.record(event):
            recorded += 1
        to_ack.append(relay_id)

    conn.commit()
    counts = IntakeCounts(pulled=len(items), archived=archived, recorded=recorded)

    if to_ack:
        try:
            relay.acknowledge(to_ack)
        except Exception as exc:  # the items are safe; the next run re-pulls them
            raise RelayIntakeError(
                f"{counts.summary()}; acknowledge failed, the relay still holds "
                f"them: {exc}"
            ) from exc
    if malformed:
        raise RelayIntakeError(
            f"{counts.summary()}; {len(malformed)} declaration(s) malformed, "
            f"archived and left at the relay: {', '.join(malformed)}"
        )
    return counts.summary()


def _archive(conn: sqlite3.Connection, item: dict, *, archive_dir: str, now: str) -> int:
    """File one item in both halves of the raw tier and return its row id.

    Serialised back from the parsed payload, keys in the order the relay sent
    them, so the same item pulled twice is byte-identical and dedupes in both
    the content-addressed archive and ``raw_document``.
    """
    content = json.dumps(item, ensure_ascii=False)
    book = (item.get("parse") or {}).get("book") or NO_BOOK
    backup.archive_raw(archive_dir, book=book, kind=RELAY_ITEM_KIND, content=content, ext="json")
    return db.record_raw_document(
        conn, book=book, kind=RELAY_ITEM_KIND, fetched_at=now, content=content
    )


def _declaration(item: dict, parse: dict, *, raw_ref: int, now: str) -> DeclarationEvent:
    stop = parse["stop"]
    if isinstance(stop, bool) or not isinstance(stop, (int, float)):
        raise TypeError(f"stop {stop!r} is not a number")
    return DeclarationEvent(
        relay_id=str(item["id"]),
        book=_text(parse, "book"),
        symbol=_text(parse, "symbol"),
        entry_date=_text(parse, "entry_date"),
        stop=stop,
        telegram_at=_text(item, "telegram_at"),
        recorded_at=now,
        raw_ref=raw_ref,
    )


def _text(source: dict, key: str) -> str:
    value = source[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} {value!r} is not a non-empty string")
    return value
