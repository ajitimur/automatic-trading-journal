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
BACKDATING_FLOOR = "2026-07-01"
