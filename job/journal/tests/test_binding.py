"""A bound Stop Declaration commits the Trade, without confirm (ADR 0012, #50).

The seam is the daily run with an injected fake relay (the same one #49's tests
use) plus Fills seeded straight into the ledger, since the auto-commit door reads
the proposal queue the ledger derives. Every test asserts observable outcomes:
what committed, with which Stop and provenance, what stayed in the queue, and
what the run row recorded.
"""

import os
import tempfile
import unittest

from journal import books, db, fills, flex, trades
from journal.run import execute_run


def _buy(ref, symbol, qty, price, when, book="IDX", source="stockbit"):
    return flex.Fill(
        source=source, source_ref=ref, revision=1, book=book, symbol=symbol,
        side="BUY", quantity=float(qty), price=float(price), commission=0.0,
        executed_at=when, order_id="o1",
    )


def _sell(ref, symbol, qty, price, when, book="IDX", source="stockbit"):
    return flex.Fill(
        source=source, source_ref=ref, revision=1, book=book, symbol=symbol,
        side="SELL", quantity=-float(qty), price=float(price), commission=0.0,
        executed_at=when, order_id="o2",
    )


class FakeRelay:
    def __init__(self, items):
        self.items = list(items)
        self.acked = []

    def pull(self):
        return [dict(i) for i in self.items]

    def acknowledge(self, ids):
        self.acked.append(list(ids))
        self.items = [i for i in self.items if i.get("id") not in set(ids)]


def _declaration(relay_id, book, symbol, stop, entry_date, telegram_at):
    return {
        "id": relay_id,
        "telegram_at": telegram_at,
        "text": f"{symbol} {stop}",
        "parse": {
            "kind": "declaration", "book": book, "symbol": symbol,
            "stop": stop, "entry_date": entry_date,
        },
    }


class BindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "journal.db")
        self._saved_archive = os.environ.pop("JOURNAL_ARCHIVE_DIR", None)
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        if self._saved_archive is not None:
            os.environ["JOURNAL_ARCHIVE_DIR"] = self._saved_archive
        self.tmp.cleanup()

    def _trade(self, symbol):
        return self.conn.execute(
            "SELECT * FROM trade WHERE symbol = ?", (symbol,)
        ).fetchone()

    def _bind_row(self, run_id):
        return self.conn.execute(
            "SELECT * FROM run_pass WHERE run_id = ? AND name = 'bind'", (run_id,)
        ).fetchone()

    def test_a_declaration_and_matching_fills_commit_the_trade_with_recorded_stop(self):
        fills.insert_fills(self.conn, [
            _buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"),
        ])
        self.conn.commit()
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-13", "2026-10-13T02:15:00Z"),
        ])
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        trade = self._trade("BBRI")
        self.assertIsNotNone(trade)
        self.assertEqual(trade["stop"], 4120)
        self.assertEqual(trade["stop_provenance"], "recorded")
        self.assertEqual(trade["entry_qty"], 1000)
        self.assertEqual(trade["auto_committed"], 1)

    def test_a_confirm_committed_trade_carries_no_auto_commit_marker(self):
        # The marker is the daily run's, not the human door's: a Trade the trader
        # confirms by hand reads 0, so the review can tell the two apart (#57).
        fills.insert_fills(self.conn, [
            _buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"),
        ])
        self.conn.commit()
        trades.confirm(self.conn, stops_by_trade={("IDX", "BBRI", "2026-10-13"): 4120})

        self.assertEqual(self._trade("BBRI")["auto_committed"], 0)

    def test_a_stop_at_or_above_entry_is_refused_unbound_and_counted(self):
        # Avg entry 4000; a declared stop of 4120 inverts R, so it never binds.
        fills.insert_fills(self.conn, [
            _buy("b1", "BBRI", 1000, 4000, "2026-10-13T09:20:00+07:00"),
        ])
        self.conn.commit()
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-13", "2026-10-13T02:15:00Z"),
        ])
        result = execute_run(self.conn, as_of="2026-10-14", relay=relay)

        self.assertIsNone(self._trade("BBRI"))  # uncommitted
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM stop_declaration_binding"
        ).fetchone()["c"], 0)  # unbound
        detail = self._bind_row(result.run_id)["detail"]
        self.assertIn("0 declaration(s) bound", detail)
        self.assertIn("1 refused", detail)

    def test_a_new_trade_with_no_declaration_stays_held(self):
        fills.insert_fills(self.conn, [
            _buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"),
        ])
        self.conn.commit()
        result = execute_run(self.conn, as_of="2026-10-14", relay=FakeRelay([]))

        self.assertIsNone(self._trade("BBRI"))
        kinds = [p.kind for p in trades.propose(self.conn)]
        self.assertIn("new-trade", kinds)
        self.assertIn("0 Trade(s) committed", self._bind_row(result.run_id)["detail"])

    def test_no_other_proposal_kind_is_committed_by_the_run(self):
        # A committed BBRI Trade whose ledger now derives more shares in the same
        # entry day is an add-fills proposal — the run must never auto-commit it.
        fills.insert_fills(self.conn, [
            _buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"),
        ])
        self.conn.commit()
        trades.confirm(self.conn, stops_by_trade={("IDX", "BBRI", "2026-10-13"): 4120})
        fills.insert_fills(self.conn, [
            _buy("b2", "BBRI", 500, 4300, "2026-10-13T10:00:00+07:00"),
        ])
        self.conn.commit()
        self.assertIn("add-fills", [p.kind for p in trades.propose(self.conn)])

        execute_run(self.conn, as_of="2026-10-14", relay=FakeRelay([]))

        # The add-fills was left for the human door: the entry side is unchanged.
        self.assertEqual(self._trade("BBRI")["entry_qty"], 1000)
        self.assertIn("add-fills", [p.kind for p in trades.propose(self.conn)])

    def test_a_same_day_round_trip_commits_the_entry_and_leaves_its_exit_in_queue(self):
        fills.insert_fills(self.conn, [
            _buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"),
            _sell("s1", "BBRI", 1000, 4500, "2026-10-13T15:00:00+07:00"),
        ])
        self.conn.commit()
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-13", "2026-10-13T02:15:00Z"),
        ])
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        trade = self._trade("BBRI")
        self.assertEqual(trade["auto_committed"], 1)
        self.assertEqual(trade["status"], "open")  # the exit did not land
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM trade_exit"
        ).fetchone()["c"], 0)
        # The sell still sits in the queue as an exit-allocation.
        self.assertIn("exit-allocation", [p.kind for p in trades.propose(self.conn)])

    def test_a_declaration_before_scope_start_commits_but_stays_out_of_scope(self):
        books.set_scope_start(self.conn, "IDX", "2026-08-18")
        fills.insert_fills(self.conn, [
            _buy("b1", "BBRI", 1000, 4200, "2026-07-10T09:20:00+07:00"),
        ])
        self.conn.commit()
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-07-10", "2026-07-10T02:15:00Z"),
        ])
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        trade = self._trade("BBRI")
        self.assertIsNotNone(trade)  # it commits
        self.assertEqual(trade["auto_committed"], 1)
        # ...but its entry date is before Scope Start, so it counts nowhere.
        self.assertFalse(books.in_scope(self.conn, "IDX", trade["entry_date"]))

    def test_the_run_row_states_bound_committed_and_refused_counts(self):
        fills.insert_fills(self.conn, [
            _buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"),  # binds
            _buy("b2", "TLKM", 1000, 3000, "2026-10-13T09:20:00+07:00"),  # refused
        ])
        self.conn.commit()
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-13", "2026-10-13T02:15:00Z"),
            _declaration("2", "IDX", "TLKM", 3100, "2026-10-13", "2026-10-13T02:16:00Z"),
        ])
        result = execute_run(self.conn, as_of="2026-10-14", relay=relay)

        row = self._bind_row(result.run_id)
        self.assertEqual(row["status"], "ran")
        self.assertIn("1 declaration(s) bound", row["detail"])
        self.assertIn("1 Trade(s) committed", row["detail"])
        self.assertIn("1 refused", row["detail"])

    def test_binding_is_idempotent_across_runs(self):
        fills.insert_fills(self.conn, [
            _buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"),
        ])
        self.conn.commit()
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-13", "2026-10-13T02:15:00Z"),
        ])
        execute_run(self.conn, as_of="2026-10-14", relay=relay)
        # The relay held the item only until acknowledged; feed it again anyway.
        relay.items = [
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-13", "2026-10-13T02:15:00Z"),
        ]
        second = execute_run(self.conn, as_of="2026-10-15", relay=relay)

        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM trade WHERE symbol = 'BBRI'"
        ).fetchone()["c"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM stop_declaration_binding"
        ).fetchone()["c"], 1)
        detail = self._bind_row(second.run_id)["detail"]
        self.assertIn("0 declaration(s) bound", detail)
        self.assertIn("0 refused", detail)


if __name__ == "__main__":
    unittest.main()
