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

- **`stop_provenance` is derived, never typed** (ADR 0002, amended by ADR 0009):
  ``recorded`` if the stop arrived before the Trade's first Exit **or** within
  ``GRACE_TRADING_DAYS`` trading days of entry, ``reconstructed`` otherwise —
  read off *when* the stop was set, never a self-reported confidence scale.
  Chasing (editing) the stop re-derives it, so the provenance always describes
  the value currently on the row.
- **Stops are never backfilled** (ADR 0009). Past the grace window a stop can
  still be set, but it derives ``reconstructed`` and is barred from adherence
  and chase scoring — there is no path that makes an old Trade gradeable.
- **The two fields are editable until freeze and locked after it** (SPEC §3.5).
  A stop supplied after freeze is refused, so a Trade frozen without one keeps no
  Risk % and no Realized R, ever — the known and accepted cost of making the stop
  chaseable.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Optional, Tuple

# The fixed setup vocabulary (SPEC §3.2). Accumulating ``other`` is the signal to
# name a third setup — so a value outside these three is refused, never coerced.
SETUP_VOCABULARY: Tuple[str, str, str] = ("base_breakout", "high_tight_flag", "other")

RECORDED = "recorded"
RECONSTRUCTED = "reconstructed"

# How long after entry a stop still counts as ``recorded`` even though an Exit is
# already on record (ADR 0009). Three, because the strategy's first planned
# decision is ``planned_partial_day3`` — a window wider than that would start
# certifying stops set after the trader had already acted on the Trade.
GRACE_TRADING_DAYS = 3


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


class StopAboveEntry(ValueError):
    """A stop at or above the Trade's entry price — impossible on a long.

    Every Trade in this journal is long, so the stop sits below the entry by
    construction: `entry_avg_price − stop` is the risk, and it must be positive.

    The guard exists because the failure is **silent, not loud**. A stop above
    entry inverts the sign of every R the Trade produces — a winner reads as a
    loss — and a stop *equal* to entry divides by zero. Neither announces itself;
    they surface much later as a distribution that quietly disagrees with the
    trader's memory. A fat-fingered decimal (430 typed as 4300) is exactly the
    shape this catches, and it is caught at the moment of entry when the trader
    still remembers what they meant.
    """


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


def _trading_days_since_entry(
    conn: sqlite3.Connection, trade_id: int, as_of: str
) -> Optional[int]:
    """Trading days elapsed since the Trade's entry, or ``None`` if uncountable.

    Counted off the symbol's own cached bars, so a suspension stretches the
    window in calendar time rather than filling it with a day that did not
    happen (SPEC §7.1). ``None`` when no bars are cached — the caller then falls
    back to the stricter rule rather than guessing the window in calendar days.
    """
    row = conn.execute(
        "SELECT book, symbol, entry_date FROM trade WHERE id = ?", (trade_id,)
    ).fetchone()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM bar "
        "WHERE book = ? AND symbol = ? AND date > ? AND date <= ?",
        (row["book"], row["symbol"], row["entry_date"], as_of),
    ).fetchone()["n"]
    have_any = conn.execute(
        "SELECT COUNT(*) AS n FROM bar WHERE book = ? AND symbol = ?",
        (row["book"], row["symbol"]),
    ).fetchone()["n"]
    return n if have_any else None


def _derive_provenance(
    conn: sqlite3.Connection, trade_id: int, as_of: str
) -> str:
    """``recorded`` if set before the first Exit **or** inside the grace window.

    Derived from *when* the stop is being set — never self-reported. The base
    rule is that a stop entered once an Exit is on record is contaminated by
    hindsight (ADR 0002), but that rule alone permanently holes every Trade
    that opens and closes inside the same few days, which on a fast book is
    most of them.

    So a stop set within ``GRACE_TRADING_DAYS`` trading days **of the entry
    date** counts as ``recorded`` regardless of exits (ADR 0009). Anchored to
    entry rather than to the first Exit because only the entry date bounds
    "before I knew" honestly — anchoring to the Exit would certify a stop typed
    months into a Trade that happened to run.

    The cost is real and deliberate: inside the window a stop may be entered on
    an already-closed Trade with its outcome fully visible, so ``recorded`` no
    longer guarantees an uncontaminated stop the way SPEC §10.6's tier table
    once implied. Trades whose bars are not cached keep the strict rule.
    """
    allocated = conn.execute(
        "SELECT COUNT(*) AS n FROM trade_exit WHERE trade_id = ?", (trade_id,)
    ).fetchone()["n"]
    if not allocated:
        return RECORDED
    elapsed = _trading_days_since_entry(conn, trade_id, as_of)
    if elapsed is not None and elapsed <= GRACE_TRADING_DAYS:
        return RECORDED
    return RECONSTRUCTED


def set_stop(
    conn: sqlite3.Connection,
    trade_id: int,
    stop: float,
    as_of: Optional[str] = None,
) -> str:
    """Record or chase the stop; returns the derived provenance. Refused once frozen.

    Editable until freeze (SPEC §3.5), so a busy-day Trade can be filled in or
    adjusted later. The provenance is re-derived on every set, so it always
    describes the stop currently stored. ``as_of`` is the date the stop is being
    set, defaulting to today — it is a parameter so the grace window is testable
    and so a backfilled run reproduces.
    """
    _require_unfrozen(conn, trade_id, "stop")
    entry = conn.execute(
        "SELECT entry_avg_price FROM trade WHERE id = ?", (trade_id,)
    ).fetchone()["entry_avg_price"]
    # Guarded only against a real entry price: a cohort still deriving reads 0,
    # and refusing there would block a legitimate stop for no reason.
    if entry and stop >= entry:
        raise StopAboveEntry(
            f"stop {stop:g} is at or above the entry price {entry:g} — "
            "on a long that inverts every R the Trade produces"
        )
    as_of = as_of or date.today().isoformat()
    provenance = _derive_provenance(conn, trade_id, as_of)
    # A stop arriving clears the decline: the trader changed their mind, and a
    # Trade that has a stop is not one that went without (ADR 0010). Declining is
    # a decision about *this moment*, never a door that locks behind you.
    conn.execute(
        "UPDATE trade SET stop = ?, stop_provenance = ?, stop_declined = 0 WHERE id = ?",
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
