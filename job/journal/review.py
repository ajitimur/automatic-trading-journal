"""The review-surface write actions (SPEC §11.3, #40).

The weekly review surface is a *reading* instrument, but it carries the few
actions that revise rather than commit: mark a Trade **reviewed**, edit its
free-text **note**, and **override the exit reason** the confirm queue accepted
unread. None of these was ever queue-committed, so writing straight through does
not breach the one-door rule (SPEC §5.1, §11.3) — the queue confirms at import,
the review surface revises on inspection.

These sit apart from the two hand-entered fields (`stop`, `setup`, in
:mod:`journal.stops`): those are locked by freeze, but a review, a note and a
corrected reason are not. A straggler is reviewed *because* it is old; a note or
a reason correction is a post-hoc revision, meaningful long after freeze.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from journal import trades
from journal.stops import UnknownTrade


class UnknownExit(ValueError):
    """No Exit allocation with the given id — nothing to re-reason."""


class UnknownReason(ValueError):
    """A reason outside the fixed vocabulary (SPEC §5.8). Refused, never coerced."""


def _require_trade(conn: sqlite3.Connection, trade_id: int) -> None:
    row = conn.execute("SELECT id FROM trade WHERE id = ?", (trade_id,)).fetchone()
    if row is None:
        raise UnknownTrade(f"no Trade with id {trade_id}")


def mark_reviewed(conn: sqlite3.Connection, trade_id: int, *, at: str) -> None:
    """Stamp the Trade reviewed at ``at`` — this is what *Reviewed →* drains.

    Not locked by freeze: a straggler is reviewed precisely because it is old.
    Re-marking simply overwrites with the later timestamp (idempotent enough for
    a weekly rhythm — the field answers "has this been looked at", not "how often").
    """
    _require_trade(conn, trade_id)
    conn.execute("UPDATE trade SET reviewed_at = ? WHERE id = ?", (at, trade_id))
    conn.commit()


def set_note(conn: sqlite3.Connection, trade_id: int, note: str) -> None:
    """Set (or replace) the free-text note. Editable at any time, freeze or not."""
    _require_trade(conn, trade_id)
    conn.execute("UPDATE trade SET note = ? WHERE id = ?", (note, trade_id))
    conn.commit()


def override_exit_reason(conn: sqlite3.Connection, exit_id: int, reason: str) -> None:
    """Revise one Exit's reason, bounded to the fixed vocabulary (SPEC §5.8).

    Bulk confirm accepts exit reasons unread, so some wrong ones land; a later
    correction, once the timeline shows the proposal was wrong, is the path bulk
    confirm structurally requires, not a second door.
    """
    if reason not in trades.EXIT_REASONS:
        raise UnknownReason(
            f"exit reason {reason!r} is not one of {', '.join(trades.EXIT_REASONS)}"
        )
    row = conn.execute(
        "SELECT id FROM trade_exit WHERE id = ?", (exit_id,)
    ).fetchone()
    if row is None:
        raise UnknownExit(f"no Exit with id {exit_id}")
    conn.execute("UPDATE trade_exit SET reason = ? WHERE id = ?", (reason, exit_id))
    conn.commit()


def get(conn: sqlite3.Connection, trade_id: int) -> Optional[sqlite3.Row]:
    """The review-surface state for one Trade — its review stamp and note."""
    return conn.execute(
        "SELECT reviewed_at, note FROM trade WHERE id = ?", (trade_id,)
    ).fetchone()
