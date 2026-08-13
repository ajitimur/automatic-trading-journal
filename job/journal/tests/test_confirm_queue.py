"""The full confirm queue: eight proposal kinds, park and recheck, corrections (#27).

One test per acceptance criterion from the issue, plus a scenario that produces
each of the eight proposal kinds so the taxonomy is provably total (SPEC §5.2).
Every test drives real Fills (or the real Stockbit TC fixture) through the one
door — nothing commits except :func:`trades.confirm` / :func:`bulk_confirm_exits`.
"""

import os
import tempfile
import unittest

from journal import db, fills, flex, stockbit, stops, trades

SAMPLES = os.path.join(os.path.dirname(__file__), "..", "..", "..", "docs", "samples")
TC_FIXTURE = os.path.join(SAMPLES, "stockbit-tc-fixture.txt")
TC_SHIFTED = os.path.join(SAMPLES, "stockbit-tc-column-shift-fixture.txt")


def _buy(ref, symbol, qty, price, when, revision=1, book="US", source="ibkr"):
    return flex.Fill(
        source=source, source_ref=ref, revision=revision, book=book,
        symbol=symbol, side="BUY", quantity=float(qty), price=float(price),
        commission=0.0, executed_at=when, order_id="o1",
    )


def _sell(ref, symbol, qty, price, when, book="US", source="ibkr"):
    return flex.Fill(
        source=source, source_ref=ref, revision=1, book=book,
        symbol=symbol, side="SELL", quantity=-float(qty), price=float(price),
        commission=0.0, executed_at=when, order_id="o2",
    )


class ConfirmQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _kinds(self):
        return [p.kind for p in trades.propose(self.conn)]

    def _trade(self, symbol):
        return self.conn.execute(
            "SELECT * FROM trade WHERE symbol = ?", (symbol,)
        ).fetchone()

    # ── Criterion: all eight proposal kinds exist ─────────────────────────
    def test_the_taxonomy_has_exactly_eight_kinds(self):
        self.assertEqual(len(trades.PROPOSAL_KINDS), 8)
        self.assertEqual(
            set(trades.PROPOSAL_KINDS),
            {"new-trade", "add-fills", "exit-allocation", "restatement",
             "quarantine", "orphan-exit", "enrichment-repair", "drift"},
        )

    def test_new_trade_and_exit_allocation_kinds(self):
        fills.insert_fills(self.conn, [_buy("b1", "AAA", 100, 10.0, "2026-08-03T09:30:00-04:00")])
        self.assertIn("new-trade", self._kinds())
        trades.confirm(self.conn)
        fills.insert_fills(self.conn, [_sell("s1", "AAA", 40, 25.0, "2026-08-07T10:00:00-04:00")])
        (exit_p,) = [p for p in trades.propose(self.conn) if p.kind == "exit-allocation"]
        # A partial sell reads as taking strength off (SPEC §5.8).
        self.assertEqual(exit_p.proposed_reason, "partial_strength")

    def test_add_fills_kind_when_same_day_grows(self):
        fills.insert_fills(self.conn, [_buy("b1", "AAA", 100, 10.0, "2026-08-03T09:30:00-04:00")])
        trades.confirm(self.conn)
        # A second buy the SAME entry day is add-fills, not a new Trade (ADR 0001).
        fills.insert_fills(self.conn, [_buy("b2", "AAA", 100, 12.0, "2026-08-03T15:00:00-04:00")])
        (p,) = [p for p in trades.propose(self.conn) if p.kind == "add-fills"]
        self.assertEqual(p.trade_id, self._trade("AAA")["id"])

        result = trades.confirm(self.conn)
        self.assertEqual(result.added_fills, 1)
        self.assertEqual(result.new_trades, 0)  # same Trade, not a second one
        t = self._trade("AAA")
        self.assertEqual(t["entry_qty"], 200)
        self.assertAlmostEqual(t["entry_avg_price"], 11.0)

    def test_restatement_kind_when_a_buy_is_restated(self):
        fills.insert_fills(self.conn, [_buy("b1", "AAA", 100, 10.0, "2026-08-03T09:30:00-04:00")])
        trades.confirm(self.conn)
        # The broker restates the same logical fill as a higher revision.
        fills.insert_fills(self.conn, [_buy("b1", "AAA", 250, 10.0, "2026-08-03T09:30:00-04:00", revision=2)])
        (p,) = [p for p in trades.propose(self.conn) if p.kind == "restatement"]
        self.assertEqual(p.stored_qty, 100)
        self.assertEqual(p.derived_qty, 250)

        result = trades.confirm(self.conn)
        self.assertEqual(result.restatements, 1)
        self.assertEqual(self._trade("AAA")["entry_qty"], 250)

    def test_orphan_exit_kind_parks_and_confirm_skips_it(self):
        fills.insert_fills(self.conn, [_sell("s1", "AAA", 100, 25.0, "2026-08-07T10:00:00-04:00")])
        (p,) = [p for p in trades.propose(self.conn) if p.kind == "orphan-exit"]
        self.assertTrue(p.blocked)
        result = trades.confirm(self.conn)
        self.assertEqual(result.parked_exits, 1)
        self.assertEqual(len(self.conn.execute("SELECT * FROM trade").fetchall()), 0)

    def test_drift_kind_when_a_restatement_lands_on_a_frozen_trade(self):
        fills.insert_fills(self.conn, [_buy("b1", "AAA", 100, 10.0, "2026-08-03T09:30:00-04:00")])
        trades.confirm(self.conn)
        stops.freeze(self.conn, self._trade("AAA")["id"])
        fills.insert_fills(self.conn, [_buy("b1", "AAA", 250, 10.0, "2026-08-03T09:30:00-04:00", revision=2)])

        (p,) = [p for p in trades.propose(self.conn) if p.kind == "drift"]
        self.assertEqual(p.trade_id, self._trade("AAA")["id"])
        # A plain confirm surfaces drift but NEVER rewrites the frozen snapshot.
        result = trades.confirm(self.conn)
        self.assertEqual(result.drifts, 1)
        self.assertEqual(self._trade("AAA")["entry_qty"], 100)  # untouched
        # Only the explicit override applies a restated fact (SPEC §5.3).
        trades.apply_drift(self.conn, self._trade("AAA")["id"])
        self.assertEqual(self._trade("AAA")["entry_qty"], 250)

    def test_enrichment_repair_kind_when_the_symbol_is_dark(self):
        fills.insert_fills(self.conn, [_buy("b1", "AAA", 100, 10.0, "2026-08-03T09:30:00-04:00")])
        trades.confirm(self.conn)
        # The book has bars for another symbol, but AAA is dark — a repair, not
        # "no data" (SPEC §5.7). An empty cache stays silent.
        self.assertNotIn("enrichment-repair", self._kinds())
        self.conn.execute(
            "INSERT INTO bar (book, symbol, date, open, high, low, close, volume) "
            "VALUES ('US', 'OTHER', '2026-08-01', 1, 1, 1, 1, 100)"
        )
        self.conn.commit()
        (p,) = [p for p in trades.propose(self.conn) if p.kind == "enrichment-repair"]
        self.assertEqual(p.symbol, "AAA")

    def test_quarantine_kind_is_a_proposal_not_an_exception(self):
        with open(TC_SHIFTED, encoding="utf-8") as fh:
            shifted = fh.read()
        parsed, proposal = trades.parse_document_or_quarantine(shifted)
        self.assertEqual(parsed, [])           # nothing lands
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.kind, "quarantine")  # a queue item, never raised

    # ── Criterion: park never stalls; RECHECK clears an orphan by hand ────
    def test_a_parked_orphan_does_not_stall_the_confirmable_items(self):
        fills.insert_fills(self.conn, [
            _buy("b1", "AAA", 100, 10.0, "2026-08-03T09:30:00-04:00"),
            _sell("s1", "ZZZ", 100, 25.0, "2026-08-07T10:00:00-04:00"),  # orphan
        ])
        result = trades.confirm(self.conn)
        self.assertEqual(result.new_trades, 1)     # AAA committed behind the parked orphan
        self.assertEqual(result.parked_exits, 1)

    def test_recheck_clears_an_orphan_once_the_trade_is_hand_entered(self):
        fills.insert_fills(self.conn, [_sell("s1", "ZZZ", 100, 25.0, "2026-08-07T10:00:00-04:00")])
        first = trades.confirm(self.conn)
        self.assertEqual(first.parked_exits, 1)
        self.assertEqual(first.exits_allocated, 0)

        # Enter the missing Trade by hand — through the same door, backdated.
        trades.hand_enter_trade(self.conn, "US", "ZZZ", "2026-08-05", 100, 20.0)
        # RECHECK is inherent: the next confirm re-derives and allocates the sell.
        second = trades.confirm(self.conn)
        self.assertEqual(second.new_trades, 1)
        self.assertEqual(second.exits_allocated, 1)
        self.assertEqual(self._trade("ZZZ")["status"], "closed")

    # ── Criterion: dedupe keys on content, not filename ───────────────────
    def test_the_same_document_under_another_filename_is_a_silent_noop(self):
        with open(TC_FIXTURE, encoding="utf-8") as fh:
            text = fh.read()
        first = fills.import_stockbit_text(self.conn, text)      # "july-soa.txt"
        self.assertGreater(first, 0)
        second = fills.import_stockbit_text(self.conn, text)     # "july-soa (1).txt"
        self.assertEqual(second, 0)  # same content → dedupe on the content hash

    # ── Criterion: a corrected quantity is a fact, not remembered ─────────
    def test_a_corrected_quantity_is_not_remembered(self):
        fills.insert_fills(self.conn, [_buy("b1", "AAA", 100, 10.0, "2026-08-03T09:30:00-04:00")])
        trades.correct_quantity(self.conn, "ibkr", "b1", 150)
        # The correction lands as a new revision; the derivation now uses 150.
        trades.confirm(self.conn)
        self.assertEqual(self._trade("AAA")["entry_qty"], 150)
        # A fact, not a rule: nothing is remembered for future statements.
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM parse_rule").fetchone()["n"], 0
        )

    # ── Criterion: a corrected symbol is a rule — remembered, pre-queue, repairs ──
    def test_a_corrected_symbol_is_remembered_applied_pre_queue_and_repairs(self):
        fills.insert_fills(self.conn, [_buy("b1", "WRNG", 100, 10.0, "2026-08-03T09:30:00-04:00", source="stockbit", book="IDX")])
        trades.confirm(self.conn)
        self.assertIsNotNone(self._trade("WRNG"))

        repaired = trades.remember_symbol_rule(self.conn, "stockbit", "WRNG", "RIGHT")
        self.assertEqual(repaired, 1)                         # already-committed Trade repaired
        self.assertIsNone(self._trade("WRNG"))
        self.assertIsNotNone(self._trade("RIGHT"))

        # Applied before anything reaches the queue: a fresh parse under the wrong
        # symbol is corrected silently, no second confirmation (SPEC §5.4).
        record = _buy("b2", "WRNG", 50, 11.0, "2026-08-04T09:30:00-04:00", source="stockbit", book="IDX")
        (fixed,) = trades.apply_symbol_rules(self.conn, [record])
        self.assertEqual(fixed.symbol, "RIGHT")

    # ── Criterion: bulk confirm covers exit reasons; new Trades & parked untouched ──
    def test_bulk_confirm_takes_exit_reasons_and_leaves_the_rest(self):
        fills.insert_fills(self.conn, [
            _buy("a", "AAA", 100, 10.0, "2026-08-03T09:30:00-04:00"),
            _buy("b", "BBB", 100, 20.0, "2026-08-03T09:30:00-04:00"),
        ])
        trades.confirm(self.conn)
        fills.insert_fills(self.conn, [
            _sell("sa", "AAA", 100, 12.0, "2026-08-07T10:00:00-04:00"),
            _sell("sb", "BBB", 100, 22.0, "2026-08-07T10:00:00-04:00"),
            _buy("c", "CCC", 100, 30.0, "2026-08-06T09:30:00-04:00"),   # a new Trade, pending
            _sell("sz", "ZZZ", 50, 5.0, "2026-08-07T10:00:00-04:00"),   # an orphan, parked
        ])
        result = trades.bulk_confirm_exits(self.conn, reasons={"sa": "stop_hit", "sb": "stop_hit"})
        self.assertEqual(result.exits_allocated, 2)

        # The batch of reasons landed on both exits...
        reasons = [r["reason"] for r in self.conn.execute("SELECT reason FROM trade_exit")]
        self.assertEqual(reasons, ["stop_hit", "stop_hit"])
        # ...and new Trades stayed one-at-a-time; the parked orphan is untouched.
        self.assertIsNone(self._trade("CCC"))
        self.assertIsNone(self._trade("ZZZ"))

    # ── Criterion: the interpreted Trade is the proposal; raw rows one hop away ──
    def test_the_proposal_carries_the_interpreted_trade(self):
        fills.insert_fills(self.conn, [
            _buy("b1", "AAA", 100, 10.0, "2026-08-03T09:30:00-04:00"),
            _buy("b2", "AAA", 300, 12.0, "2026-08-03T09:45:00-04:00"),
        ])
        (p,) = [p for p in trades.propose(self.conn) if p.kind == "new-trade"]
        # Interpreted: symbol, entry date, quantity, quantity-weighted avg price.
        self.assertEqual((p.symbol, p.entry_date, p.quantity), ("AAA", "2026-08-03", 400))
        self.assertAlmostEqual(p.avg_price, 11.5)
        # The raw broker rows are one disclosure away in the Fill ledger.
        raw = trades.latest_fills(self.conn)
        self.assertEqual(len(raw), 2)


if __name__ == "__main__":
    unittest.main()
