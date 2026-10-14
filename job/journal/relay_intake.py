"""The relay intake pass: pull, archive, record, acknowledge, push (ADR 0011, #49, #51, #52).

The relay holds what the trader sent the bot until the Mac pulls it. Each run
takes everything it holds and, in this order:

1. files every item verbatim in the keep-forever raw tier, both halves (the
   filesystem archive and ``raw_document``), because a declaration is a source
   document like a Flex statement or a TC (SPEC §13.5);
2. records each declaration, decline and undo as a
   :class:`~journal.declarations.StopDeclarationEvent`, and drops each forwarded
   TC PDF into the Fill ledger (see below);
3. commits;
4. and only then acknowledges, which is what lets the relay delete the items;
5. then pushes the symbols the journal knows on the US book, so the relay reads
   a 4-letter one of them as US rather than IDX.

Acknowledging last is the durability rule. A run that dies anywhere before the
acknowledgement leaves the relay holding everything, so the next run pulls the
same items again, and both the archive and the event store ignore an item they
already hold. Losing an acknowledgement costs a re-pull; acknowledging early
could cost a Stop.

An item of a kind this job does not know (a grammar it predates) is archived and
acknowledged but not recorded: the raw tier keeps it, so a later job can replay
it. An item of a *known* kind missing a field it needs is archived but not
acknowledged, and the pass reports it as an error. It stays at the relay rather
than being dropped, and the run record says the wire contract has drifted.

A forwarded TC goes through exactly what ``journal drop`` does: its bytes are
fetched from the relay and archived as a ``stockbit-tc`` (the same name a hand
drop of the same file gets), then parsed by the Stockbit path, so content-hash
dedupe, the fee-identity quarantine and the symbol rules all apply unchanged.
One that cannot be fetched or extracted (a relay or Telegram outage, a missing
``pdftotext``) stays at the relay for the next run. One that parses to a verdict
(landed, quarantined, not a TC) is acknowledged: it is archived, so a parser fix
can re-run over it, and pulling it again would only repeat the verdict. A
quarantine or a non-TC is reported as an error, as loudly as a hand drop's.

Nothing binds or commits a Trade here.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from typing import Protocol, Sequence

from . import backup, db, fills, stockbit
from .declarations import DECLARED, DECLINED, WITHDRAWN, StopDeclarationEvent, StopDeclarationStore
from .stops import SETUP_VOCABULARY

# The raw-tier kind every relay item is filed under.
RELAY_ITEM_KIND = "relay-item"

# The book a relay item is filed under when it names none.
NO_BOOK = "RELAY"

# The relay's parse kind for a forwarded TC PDF.
DOCUMENT = "document"

# The relay's parse kinds, and the event each records.
_EVENT_KIND = {"declaration": DECLARED, "decline": DECLINED, "undo": WITHDRAWN}


class Relay(Protocol):
    """The relay seam (see :mod:`journal.relay_client` for the real one)."""

    def pull(self) -> list[dict]:
        ...

    def acknowledge(self, ids: Sequence[str]) -> None:
        ...

    def push_known_us_symbols(self, symbols: Sequence[str]) -> None:
        ...

    def document(self, item_id: str) -> bytes:
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
    tcs: int = 0
    new_fills: int = 0

    def summary(self) -> str:
        return (
            f"{self.pulled} item(s) pulled, {self.archived} archived, "
            f"{self.recorded} declaration event(s) recorded, "
            f"{self.tcs} TC PDF(s) taken in, {self.new_fills} new fill(s)"
        )


class _LeaveAtRelay(Exception):
    """A forwarded TC this run could not get to a verdict on; the next run retries."""


class _Rejected(Exception):
    """A forwarded TC that parsed to a verdict other than landing: reported, let go."""


def take_in(
    conn: sqlite3.Connection, relay: Relay, *, archive_dir: str, now: str
) -> str:
    """Run the pass against ``relay`` and return the run-record detail.

    Raises whatever the relay raises on pull (nothing has happened yet), and
    :class:`RelayIntakeError` when something fails after items were archived.
    """
    items = relay.pull()
    store = StopDeclarationStore(conn)
    to_ack: list[str] = []
    malformed: list[str] = []
    tc_problems: list[str] = []
    archived = recorded = anonymous = tcs = new_fills = 0

    for item in items:
        raw_ref = _archive(conn, item, archive_dir=archive_dir, now=now)
        archived += 1
        if not item.get("id"):
            # Nothing to acknowledge it by; archived, reported, never blocking.
            anonymous += 1
            continue
        relay_id = str(item["id"])
        parse = item.get("parse") or {}
        if parse.get("kind") == DOCUMENT:
            try:
                new_fills += _take_in_tc(conn, relay, relay_id, archive_dir=archive_dir, now=now)
                tcs += 1
            except _LeaveAtRelay as exc:
                tc_problems.append(str(exc))
                continue
            except _Rejected as exc:
                tc_problems.append(str(exc))
            to_ack.append(relay_id)
            continue
        if parse.get("kind") not in _EVENT_KIND:
            to_ack.append(relay_id)
            continue
        try:
            event = _event(item, parse, raw_ref=raw_ref, now=now)
        except (KeyError, TypeError, ValueError) as exc:
            malformed.append(f"{relay_id} ({exc!r})")
            continue
        if store.record(event):
            recorded += 1
        to_ack.append(relay_id)

    conn.commit()
    counts = IntakeCounts(
        pulled=len(items), archived=archived, recorded=recorded, tcs=tcs, new_fills=new_fills
    )

    if to_ack:
        try:
            relay.acknowledge(to_ack)
        except Exception as exc:  # the items are safe; the next run re-pulls them
            raise RelayIntakeError(
                f"{counts.summary()}; acknowledge failed, the relay still holds "
                f"them: {exc}"
            ) from exc
    problems = list(tc_problems)
    try:
        relay.push_known_us_symbols(known_us_symbols(conn))
    except Exception as exc:  # the relay keeps the last list it had
        problems.append(f"known-US push failed: {exc}")
    if malformed:
        problems.append(
            f"{len(malformed)} item(s) malformed, archived and left at "
            f"the relay: {', '.join(malformed)}"
        )
    if anonymous:
        problems.append(f"{anonymous} item(s) without an id, archived only")
    if problems:
        raise RelayIntakeError(f"{counts.summary()}; {'; '.join(problems)}")
    return counts.summary()


def known_us_symbols(conn: sqlite3.Connection) -> list[str]:
    """Every symbol the journal has a Fill or a Trade for on the US book, sorted."""
    return [
        r["symbol"]
        for r in conn.execute(
            "SELECT symbol FROM fill WHERE book = 'US' "
            "UNION SELECT symbol FROM trade WHERE book = 'US' ORDER BY symbol"
        )
    ]


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


def _take_in_tc(
    conn: sqlite3.Connection, relay: Relay, relay_id: str, *, archive_dir: str, now: str
) -> int:
    """Fetch, archive and drop one forwarded TC; return the Fills it newly added.

    Raises :class:`_LeaveAtRelay` when it never got the text to parse, and
    :class:`_Rejected` when the Stockbit path refused the document.
    """
    try:
        content = relay.document(relay_id)
    except Exception as exc:
        raise _LeaveAtRelay(f"TC {relay_id} could not be fetched, left at the relay: {exc}") from exc
    # Where PDF readers look for the header: anywhere in the first 1024 bytes.
    is_pdf = b"%PDF" in content[:1024]
    # Archived before it is parsed, as `journal drop` does: a quarantined
    # document is what a later parser fix needs.
    backup.archive_raw(
        archive_dir, book=stockbit.BOOK, kind=stockbit.RAW_KIND, content=content, ext="pdf" if is_pdf else "txt"
    )
    try:
        text = _tc_text(content) if is_pdf else content.decode("utf-8")
    except stockbit.StockbitError as exc:
        raise _LeaveAtRelay(f"TC {relay_id} could not be read, left at the relay: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise _Rejected(f"TC {relay_id} not taken in: neither a PDF nor text") from exc
    try:
        return fills.import_stockbit_text(conn, text, fetched_at=now)
    except stockbit.QuarantineError as exc:
        raise _Rejected(f"TC {relay_id} quarantined, nothing committed: {exc}") from exc
    except stockbit.StockbitError as exc:
        raise _Rejected(f"TC {relay_id} not taken in: {exc}") from exc


def _tc_text(pdf: bytes) -> str:
    """The ``pdftotext -layout`` text of a PDF held in memory."""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "tc.pdf")
        with open(path, "wb") as fh:
            fh.write(pdf)
        return stockbit.extract_text(path)


def _event(item: dict, parse: dict, *, raw_ref: int, now: str) -> StopDeclarationEvent:
    kind = _EVENT_KIND[parse["kind"]]
    stop = None
    if kind == DECLARED:
        stop = parse["stop"]
        if isinstance(stop, bool) or not isinstance(stop, (int, float)):
            raise TypeError(f"stop {stop!r} is not a number")
    setup = None
    if kind != WITHDRAWN:
        setup = parse.get("setup")
        if setup is not None and setup not in SETUP_VOCABULARY:
            raise ValueError(f"setup {setup!r} is not one of {', '.join(SETUP_VOCABULARY)}")
    return StopDeclarationEvent(
        relay_id=str(item["id"]),
        kind=kind,
        book=_text(parse, "book"),
        symbol=_text(parse, "symbol"),
        entry_date=_text(parse, "entry_date"),
        stop=stop,
        setup=setup,
        telegram_at=_text(item, "telegram_at"),
        recorded_at=now,
        raw_ref=raw_ref,
    )


def _text(source: dict, key: str) -> str:
    value = source[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} {value!r} is not a non-empty string")
    return value
