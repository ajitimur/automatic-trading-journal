"""Trades are derived from Fills through a confirm step (SPEC §3.4/§5.1, #23).

Every test drives real Fills into the ledger and asserts on what proposes and
confirms — the one-door rule, the entry-day cohort (ADR 0001), FIFO exit
allocation with a bounded override, and quantity-weighted average price.
"""

import os
import tempfile
import unittest

from journal import db, fills, flex, trades


def _buy(ref, symbol, qty, price, when, revision=1, book="US"):
    return flex.Fill(
        source="ibkr", source_ref=ref, revision=revision, book=book,
        symbol=symbol, side="BUY", quantity=float(qty), price=float(price),
        commission=0.0, executed_at=when, order_id="o1",
    )


def _sell(ref, symbol, qty, price, when, book="US"):
    return flex.Fill(
        source="ibkr", source_ref=ref, revision=1, book=book,
        symbol=symbol, side="SELL", quantity=-float(qty), price=float(price),
        commission=0.0, executed_at=when, order_id="o2",
    )


class TradesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _trades(self):
        return self.conn.execute(
            "SELECT symbol, entry_date, entry_qty, entry_avg_price, status "
            "FROM trade ORDER BY entry_date, symbol"
        ).fetchall()

    # ── Acceptance: nothing reaches the Trade table without a confirm ──
    def test_proposals_commit_nothing(self):
        fills.insert_fills(self.conn, [_buy("b1", "AAA", 100, 10.0, "2026-08-03T09:30:00-04:00")])
        proposals = trades.propose(self.conn)
        self.assertEqual([p.kind for p in proposals], ["new-trade"])
        self.assertEqual(len(self._trades()), 0)  # propose wrote nothing

        trades.confirm(self.conn)
        self.assertEqual(len(self._trades()), 1)  # the confirm is the only writer

    # ── Acceptance: quantity-weighted entry price ties back to cash ──
    def test_entry_avg_price_is_quantity_weighted(self):
        fills.insert_fills(self.conn, [
            _buy("b1", "AAA", 100, 10.0, "2026-08-03T09:30:00-04:00"),
            _buy("b2", "AAA", 300, 12.0, "2026-08-03T09:45:00-04:00"),
        ])
        trades.confirm(self.conn)
        (t,) = self._trades()
        # 400 shares; cash out = 100*10 + 300*12 = 4600 → avg 11.50.
        self.assertEqual(t["entry_qty"], 400)
        self.assertAlmostEqual(t["entry_avg_price"], 11.5)
        self.assertAlmostEqual(t["entry_avg_price"] * t["entry_qty"], 4600.0)

    # ── Acceptance: same day merges, a different day is a second Trade ──
    def test_same_day_merges_different_day_is_a_second_trade(self):
        fills.insert_fills(self.conn, [
            _buy("b1", "AAA", 100, 10.0, "2026-08-03T09:30:00-04:00"),
            _buy("b2", "AAA", 100, 11.0, "2026-08-03T15:00:00-04:00"),
            _buy("b3", "AAA", 50, 20.0, "2026-08-05T10:00:00-04:00"),
        ])
        proposals = [p for p in trades.propose(self.conn) if p.kind == "new-trade"]
        self.assertEqual(len(proposals), 2)  # two entry days → two Trades
        later = next(p for p in proposals if p.entry_date == "2026-08-05")
        self.assertIn("different Trade", later.note)  # the proposal states it

        trades.confirm(self.conn)
        rows = self._trades()
        self.assertEqual(
            [(r["entry_date"], r["entry_qty"]) for r in rows],
            [("2026-08-03", 200), ("2026-08-05", 50)],
        )

    # ── Acceptance: two open Trades allocate exits FIFO ──
    def test_two_open_trades_allocate_exit_fifo(self):
        fills.insert_fills(self.conn, [
            _buy("b1", "AAA", 100, 10.0, "2026-08-03T09:30:00-04:00"),
            _buy("b2", "AAA", 100, 20.0, "2026-08-05T09:30:00-04:00"),
        ])
        trades.confirm(self.conn)
        # Sell 150: FIFO takes 100 from the 08-03 Trade (closing it) and 50 from 08-05.
        fills.insert_fills(self.conn, [_sell("s1", "AAA", 150, 25.0, "2026-08-07T10:00:00-04:00")])
        trades.confirm(self.conn)

        rows = {r["entry_date"]: r for r in self._trades()}
        self.assertEqual(rows["2026-08-03"]["status"], "closed")
        self.assertEqual(rows["2026-08-05"]["status"], "open")
        exits = self.conn.execute(
            "SELECT t.entry_date, x.quantity FROM trade_exit x "
            "JOIN trade t ON t.id = x.trade_id ORDER BY t.entry_date"
        ).fetchall()
        self.assertEqual(
            [(e["entry_date"], e["quantity"]) for e in exits],
            [("2026-08-03", 100), ("2026-08-05", 50)],
        )

    # ── Acceptance: an override, bounded by what each Trade holds open ──
    def test_override_reallocates_and_is_bounded(self):
        fills.insert_fills(self.conn, [
            _buy("b1", "AAA", 100, 10.0, "2026-08-03T09:30:00-04:00"),
            _buy("b2", "AAA", 100, 20.0, "2026-08-05T09:30:00-04:00"),
        ])
        trades.confirm(self.conn)
        fills.insert_fills(self.conn, [_sell("s1", "AAA", 100, 25.0, "2026-08-07T10:00:00-04:00")])

        # Override: take the whole 100 from the *newer* Trade instead of FIFO.
        trades.confirm(self.conn, overrides={"s1": [("2026-08-05", 100.0)]})
        rows = {r["entry_date"]: r for r in self._trades()}
        self.assertEqual(rows["2026-08-03"]["status"], "open")
        self.assertEqual(rows["2026-08-05"]["status"], "closed")

    def test_override_cannot_exceed_open_quantity(self):
        fills.insert_fills(self.conn, [
            _buy("b1", "AAA", 100, 10.0, "2026-08-03T09:30:00-04:00"),
            _buy("b2", "AAA", 100, 20.0, "2026-08-05T09:30:00-04:00"),
        ])
        trades.confirm(self.conn)
        fills.insert_fills(self.conn, [_sell("s1", "AAA", 100, 25.0, "2026-08-07T10:00:00-04:00")])
        with self.assertRaises(trades.AllocationError):
            # 150 from a Trade that only holds 100 open.
            trades.confirm(self.conn, overrides={"s1": [("2026-08-05", 150.0)]})

    # ── Acceptance: recomputable from Fills, idempotent confirm ──
    def test_confirm_is_idempotent(self):
        fills.insert_fills(self.conn, [
            _buy("b1", "AAA", 100, 10.0, "2026-08-03T09:30:00-04:00"),
            _sell("s1", "AAA", 100, 25.0, "2026-08-07T10:00:00-04:00"),
        ])
        first = trades.confirm(self.conn)
        self.assertEqual(first.new_trades, 1)
        self.assertEqual(first.exits_allocated, 1)

        second = trades.confirm(self.conn)  # re-confirm the same ledger
        self.assertEqual(second.new_trades, 0)
        self.assertEqual(second.exits_allocated, 0)
        self.assertEqual(len(self._trades()), 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM trade_exit").fetchone()["n"], 1
        )

    # ── A sell with no journalled entry parks, never guesses (SPEC §5.2) ──
    def test_orphan_exit_parks(self):
        fills.insert_fills(self.conn, [_sell("s1", "AAA", 100, 25.0, "2026-08-07T10:00:00-04:00")])
        (proposal,) = [p for p in trades.propose(self.conn) if p.kind == "orphan-exit"]
        self.assertTrue(proposal.blocked)  # nothing open — parks

        result = trades.confirm(self.conn)
        self.assertEqual(result.parked_exits, 1)
        self.assertEqual(len(self._trades()), 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM trade_exit").fetchone()["n"], 0
        )

    # ── An over-allocating sell commits the part that fits (SPEC §3.4) ──
    def test_over_allocating_sell_closes_what_it_can(self):
        """The IWDA shape: 29 bought inside the export window, 58 sold.

        The sell exceeds everything journalled as open because the position was
        built before the broker's window. Refusing it outright left the Trade
        reading ``open`` for five months after it was sold — §3.4 forbids
        allocating a Trade *more* than it holds open, not filling what fits.
        """
        fills.insert_fills(self.conn, [
            _buy("b1", "IWDA", 29, 122.71, "2026-08-03T09:30:00-04:00"),
            _sell("s1", "IWDA", 58, 123.89, "2026-08-07T10:00:00-04:00"),
        ])
        (proposal,) = [p for p in trades.propose(self.conn) if p.kind == "exit-allocation"]
        self.assertEqual(proposal.over_allocated, 29)
        self.assertFalse(proposal.blocked)  # the fitting part is confirmable

        result = trades.confirm(self.conn)
        self.assertEqual(result.exits_allocated, 1)
        self.assertEqual(result.parked_exits, 1)  # the 29-share remainder

        (t,) = self._trades()
        self.assertEqual(t["status"], "closed")  # the 29 held open were allocated
        self.assertEqual(
            self.conn.execute("SELECT SUM(quantity) q FROM trade_exit").fetchone()["q"], 29
        )

    # ── The unallocated remainder resurfaces rather than vanishing ──
    def test_over_allocation_remainder_becomes_an_orphan_exit(self):
        fills.insert_fills(self.conn, [
            _buy("b1", "IWDA", 29, 122.71, "2026-08-03T09:30:00-04:00"),
            _sell("s1", "IWDA", 58, 123.89, "2026-08-07T10:00:00-04:00"),
        ])
        trades.confirm(self.conn)

        # Re-deriving sees the Fill as *partly* allocated, not simply "seen":
        # 29 of 58 landed, so the residual 29 has no open Trade left to take it.
        (orphan,) = [p for p in trades.propose(self.conn) if p.kind == "orphan-exit"]
        self.assertEqual(orphan.quantity, 29)
        self.assertTrue(orphan.blocked)

        # And it stays put — re-confirming never double-allocates the same Fill.
        again = trades.confirm(self.conn)
        self.assertEqual(again.exits_allocated, 0)
        self.assertEqual(
            self.conn.execute("SELECT SUM(quantity) q FROM trade_exit").fetchone()["q"], 29
        )

    # ── A restatement changes the derivation: highest revision wins (ADR 0003) ──
    def test_highest_revision_drives_the_cohort(self):
        fills.insert_fills(self.conn, [_buy("b1", "AAA", 100, 10.0, "2026-08-03T09:30:00-04:00")])
        # Broker restates the quantity up to 250 as a new revision.
        fills.insert_fills(self.conn, [_buy("b1", "AAA", 250, 10.0, "2026-08-03T09:30:00-04:00", revision=2)])
        trades.confirm(self.conn)
        (t,) = self._trades()
        self.assertEqual(t["entry_qty"], 250)


if __name__ == "__main__":
    unittest.main()
