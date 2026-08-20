"""The Book discriminator (SPEC §3.1).

A Book is a discriminator on one model, not two models. It carries currency,
benchmark, broker and lot convention. **Nothing is ever aggregated across
books.** The skeleton only needs the identities and the backdating floor; the
richer attributes land with the enrichment tickets.
"""

# Book identities. Order is the pass order of a run (SPEC §13.3).
US = "US"
IDX = "IDX"

BOOKS = (US, IDX)

# The regime benchmark per book (SPEC §8.1). QQQ is the chart the US trader
# actually watches; ^JKSE (IHSG) is the whole-market Indonesian index, since
# the trader trades beyond the large-cap 45. The two books' regimes stay
# strictly independent — nothing aggregates across them.
BENCHMARKS = {US: "QQQ", IDX: "^JKSE"}

# Backdating starts July 2026 (SPEC §2 "Deep historical backdating"). A book
# that has never been processed advances from this floor, never from epoch.
#
# **Not the same thing as SCOPE_START below** and deliberately kept apart: this
# is how far back the *daily run* reaches, a question about enrichment work.
BACKDATING_FLOOR = "2026-07-01"

# No Scope Start set means no boundary — every Trade counts. A fresh journal has
# never been restarted, so this is the honest default (ADR 0008).
NO_SCOPE_START = "0000-01-01"


def scope_start(conn, book: str) -> str:
    """The Book's Scope Start: the date from which its Trades count (ADR 0008).

    **Stored per book in the journal, not compiled in.** It records that *this*
    record was restarted on a particular day, which is a fact about the trader's
    history and not about the software — a second journal would have its own, and
    a fresh one has none. Keeping it in the store is also what lets it move
    without a release.

    Per book because nothing here is ever aggregated across books, and a single
    global date would be the first thing that is.
    """
    row = conn.execute(
        "SELECT scope_start FROM book_scope WHERE book = ?", (book,)
    ).fetchone()
    return row["scope_start"] if row else NO_SCOPE_START


def set_scope_start(conn, book: str, start: str) -> None:
    """Move a Book's Scope Start. Trades before it stay stored but stop counting."""
    conn.execute(
        "INSERT INTO book_scope (book, scope_start) VALUES (?, ?) "
        "ON CONFLICT(book) DO UPDATE SET scope_start = excluded.scope_start",
        (book, start),
    )
    conn.commit()


def in_scope(conn, book: str, entry_date: str) -> bool:
    """Whether a Trade counts, judged on its **entry date** (ADR 0008).

    Entry date governs permanently, never exit date. Risk %, Exposure %, Ruleset
    Version and Book History all resolve as-of entry; letting a Trade join the
    aggregates because it *closed* inside the journal's life would put one field
    meaning "as of entry" and another meaning "as of exit" in the same number.
    """
    return entry_date >= scope_start(conn, book)
