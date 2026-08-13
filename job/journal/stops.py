"""The two hand-entered fields — a chaseable stop and its setup (SPEC §3.2/§5.5, #28).

`stop` and `setup` are the **only two hand-entered fields in the system**; nothing
else on the import path is typed. Both earn their keystrokes because nothing else
can supply them: the stop is genuinely discretionary and is not derivable from
bars, and the setup is the only input the setup-selection goal has.

Neither is demanded at confirm (SPEC §5.5, the chaseable path): a Trade commits
with no stop and no setup. Exposure % is computed regardless; Risk % and Realized
R stay held open until a stop arrives. The nag lives in the daily job's review
banner, not the confirm queue.

Two derived rules ride along:

- **`stop_provenance` is derived, never typed** (ADR 0002): ``recorded`` if the
  stop arrived before the Trade's first Exit, ``reconstructed`` if after — read
  off *when* the stop was set relative to the exits on record, never a
  self-reported confidence scale. Chasing (editing) the stop re-derives it, so
  the provenance always describes the value currently on the row.
- **The two fields are editable until freeze and locked after it** (SPEC §3.5).
  A stop supplied after freeze is refused, so a Trade frozen without one keeps no
  Risk % and no Realized R, ever — the known and accepted cost of making the stop
  chaseable.
"""

from __future__ import annotations

import sqlite3
from typing import Optional, Tuple

# The fixed setup vocabulary (SPEC §3.2). Accumulating ``other`` is the signal to
# name a third setup — so a value outside these three is refused, never coerced.
SETUP_VOCABULARY: Tuple[str, str, str] = ("base_breakout", "high_tight_flag", "other")

RECORDED = "recorded"
RECONSTRUCTED = "reconstructed"


class UnknownTrade(ValueError):
    """No Trade with the given id — nothing to annotate."""


class FrozenError(Exception):
    """A hand-entered field was edited after freeze locked it (SPEC §3.5).

    Its own type, not a plain ``ValueError``: freezing without a stop makes the
    hole permanent, and a caller may want to distinguish that refusal from a
    validation slip.
    """


class UnknownSetup(ValueError):
    """A setup outside the fixed three-value vocabulary (SPEC §3.2)."""


def _row(conn: sqlite3.Connection, trade_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, frozen FROM trade WHERE id = ?", (trade_id,)
    ).fetchone()
    if row is None:
        raise UnknownTrade(f"no Trade with id {trade_id}")
    return row


def _require_unfrozen(conn: sqlite3.Connection, trade_id: int, field: str) -> None:
    if _row(conn, trade_id)["frozen"]:
        raise FrozenError(
            f"Trade {trade_id} is frozen — {field} is locked and cannot be changed"
        )


def _derive_provenance(conn: sqlite3.Connection, trade_id: int) -> str:
    """``recorded`` if no Exit has landed yet, ``reconstructed`` if one has.

    Derived from *when* the stop is being set relative to the exits on record —
    a stop entered while the Trade has an Exit is contaminated by hindsight and
    must be excludable from discipline scoring without the trader self-reporting.
    """
    allocated = conn.execute(
        "SELECT COUNT(*) AS n FROM trade_exit WHERE trade_id = ?", (trade_id,)
    ).fetchone()["n"]
    return RECONSTRUCTED if allocated else RECORDED


def set_stop(conn: sqlite3.Connection, trade_id: int, stop: float) -> str:
    """Record or chase the stop; returns the derived provenance. Refused once frozen.

    Editable until freeze (SPEC §3.5), so a busy-day Trade can be filled in or
    adjusted later. The provenance is re-derived on every set from whether an
    Exit is already on record, so it always describes the stop currently stored.
    """
    _require_unfrozen(conn, trade_id, "stop")
    provenance = _derive_provenance(conn, trade_id)
    conn.execute(
        "UPDATE trade SET stop = ?, stop_provenance = ? WHERE id = ?",
        (stop, provenance, trade_id),
    )
    conn.commit()
    return provenance


def set_setup(conn: sqlite3.Connection, trade_id: int, setup: str) -> None:
    """Set the setup, bounded to the three-value vocabulary. Refused once frozen."""
    if setup not in SETUP_VOCABULARY:
        raise UnknownSetup(
            f"setup {setup!r} is not one of {', '.join(SETUP_VOCABULARY)}"
        )
    _require_unfrozen(conn, trade_id, "setup")
    conn.execute("UPDATE trade SET setup = ? WHERE id = ?", (setup, trade_id))
    conn.commit()


def freeze(conn: sqlite3.Connection, trade_id: int) -> None:
    """Lock the two hand-entered fields (SPEC §3.5).

    Freeze fires 20 trading days after the final Exit; wiring that fuse to the
    daily job is a later step. This is the primitive it calls — once set, the
    stop and setup are locked, and a Trade frozen without a stop keeps the hole.
    """
    _row(conn, trade_id)  # a freeze on an unknown Trade is a bug, not a no-op
    conn.execute("UPDATE trade SET frozen = 1 WHERE id = ?", (trade_id,))
    conn.commit()


def annotations(conn: sqlite3.Connection, trade_id: int) -> Optional[sqlite3.Row]:
    """The hand-entered fields and their derived companions for one Trade."""
    return conn.execute(
        "SELECT stop, setup, stop_provenance, frozen FROM trade WHERE id = ?",
        (trade_id,),
    ).fetchone()
