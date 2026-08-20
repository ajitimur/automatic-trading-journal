"""The two hand-entered fields — chaseable stop, fixed setup, derived provenance.

Covers SPEC §3.2/§3.5/§5.5 and ADR 0002, the acceptance criteria of #28:
- a Trade commits with neither stop nor setup (the chaseable path);
- `stop_provenance` derives from whether the stop arrived before the first Exit
  and nothing about it is typed;
- stop and setup are editable until freeze and locked after it;
- a stop supplied after freeze is refused, leaving no Risk % and no R;
- the setup vocabulary is fixed to the three values.
"""

import os
import tempfile
import unittest

from journal import db, fills, flex, stops, trades


def _buy(ref, symbol, qty, price, when, book="US"):
    return flex.Fill(
        source="ibkr", source_ref=ref, revision=1, book=book,
        symbol=symbol, side="BUY", quantity=float(qty), price=float(price),
        commission=0.0, executed_at=when, order_id="o1",
    )


def _sell(ref, symbol, qty, price, when, book="US"):
    return flex.Fill(
        source="ibkr", source_ref=ref, revision=1, book=book,
        symbol=symbol, side="SELL", quantity=-float(qty), price=float(price),
        commission=0.0, executed_at=when, order_id="o2",
    )


class StopsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _open_trade(self, ref="b1", symbol="AAA", when="2026-08-03T09:30:00-04:00"):
        fills.insert_fills(self.conn, [_buy(ref, symbol, 100, 10.0, when)])
        trades.confirm(self.conn)
        return self.conn.execute(
            "SELECT id FROM trade WHERE symbol = ?", (symbol,)
        ).fetchone()["id"]

    def _close_trade(self, trade_id, ref="s1", symbol="AAA",
                     when="2026-08-07T10:00:00-04:00"):
        fills.insert_fills(self.conn, [_sell(ref, symbol, 100, 25.0, when)])
        trades.confirm(self.conn)

    # ── Acceptance: a Trade commits with neither stop nor setup ──
    def test_commits_with_neither_stop_nor_setup(self):
        trade_id = self._open_trade()
        a = stops.annotations(self.conn, trade_id)
        self.assertIsNone(a["stop"])
        self.assertIsNone(a["setup"])
        self.assertIsNone(a["stop_provenance"])
        self.assertEqual(a["frozen"], 0)  # not frozen; Exposure % needs no stop

    # ── Acceptance: provenance is 'recorded' when the stop precedes any Exit ──
    def test_stop_before_first_exit_is_recorded(self):
        trade_id = self._open_trade()
        self.assertEqual(stops.set_stop(self.conn, trade_id, 9.0), stops.RECORDED)
        self.assertEqual(
            stops.annotations(self.conn, trade_id)["stop_provenance"], "recorded"
        )
        # A later Exit does not retroactively contaminate a stop already recorded.
        self._close_trade(trade_id)
        self.assertEqual(
            stops.annotations(self.conn, trade_id)["stop_provenance"], "recorded"
        )

    # ── Acceptance: provenance is 'reconstructed' when the stop follows an Exit ──
    def test_stop_after_first_exit_is_reconstructed(self):
        trade_id = self._open_trade()
        self._close_trade(trade_id)
        self.assertEqual(
            stops.set_stop(self.conn, trade_id, 9.0), stops.RECONSTRUCTED
        )
        self.assertEqual(
            stops.annotations(self.conn, trade_id)["stop_provenance"], "reconstructed"
        )

    def _bars(self, dates, symbol="AAA", book="US"):
        """Cache trading days for the symbol, so the grace window can be counted."""
        for d in dates:
            self.conn.execute(
                "INSERT INTO bar (book, symbol, date, open, high, low, close, volume) "
                "VALUES (?, ?, ?, 10, 11, 9, 10, 1000)",
                (book, symbol, d),
            )
        self.conn.commit()

    # ── The grace window: a stop set soon after entry is 'recorded' (ADR 0009) ──
    def test_stop_inside_the_grace_window_is_recorded(self):
        trade_id = self._open_trade()  # entered 2026-08-03
        self._close_trade(trade_id)
        self._bars(["2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"])
        # Three trading days after entry — inside the window despite the Exit.
        self.assertEqual(
            stops.set_stop(self.conn, trade_id, 9.0, as_of="2026-08-06"),
            stops.RECORDED,
        )

    # ── Past the window the stop is reconstructed, exits or not ──
    def test_stop_past_the_grace_window_is_reconstructed(self):
        trade_id = self._open_trade()
        self._close_trade(trade_id)
        self._bars(["2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"])
        # Four trading days after entry — one past the window.
        self.assertEqual(
            stops.set_stop(self.conn, trade_id, 9.0, as_of="2026-08-07"),
            stops.RECONSTRUCTED,
        )

    # ── The window counts trading days, so a suspension stretches it ──
    def test_grace_window_counts_trading_days_not_calendar_days(self):
        trade_id = self._open_trade()
        self._close_trade(trade_id)
        # The symbol traded on only two days in the fortnight after entry.
        self._bars(["2026-08-04", "2026-08-14"])
        self.assertEqual(
            stops.set_stop(self.conn, trade_id, 9.0, as_of="2026-08-17"),
            stops.RECORDED,  # 11 calendar days, but only 2 trading days
        )

    # ── The accepted cost: inside the window, a closed Trade still reads recorded ──
    def test_grace_window_certifies_a_closed_trade(self):
        """Deliberate (ADR 0009): the outcome is visible and it still says recorded.

        Without this the window buys nothing on a book where Trades routinely
        open and close within days — but it does mean `recorded` no longer
        promises an uncontaminated stop.
        """
        trade_id = self._open_trade()
        self._close_trade(trade_id)
        self._bars(["2026-08-04", "2026-08-05"])
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM trade WHERE id = ?", (trade_id,)
            ).fetchone()["status"],
            "closed",
        )
        self.assertEqual(
            stops.set_stop(self.conn, trade_id, 9.0, as_of="2026-08-05"),
            stops.RECORDED,
        )

    # ── With no bars cached the window cannot be counted, so the strict rule holds ──
    def test_uncountable_window_falls_back_to_the_strict_rule(self):
        trade_id = self._open_trade()
        self._close_trade(trade_id)
        self.assertEqual(
            stops.set_stop(self.conn, trade_id, 9.0, as_of="2026-08-04"),
            stops.RECONSTRUCTED,
        )

    # ── Acceptance: stop and setup are editable until freeze ──
    def test_stop_and_setup_editable_until_freeze(self):
        trade_id = self._open_trade()
        stops.set_stop(self.conn, trade_id, 9.0)
        stops.set_stop(self.conn, trade_id, 9.5)  # chase it up — still open
        stops.set_setup(self.conn, trade_id, "base_breakout")
        stops.set_setup(self.conn, trade_id, "high_tight_flag")
        a = stops.annotations(self.conn, trade_id)
        self.assertEqual(a["stop"], 9.5)
        self.assertEqual(a["setup"], "high_tight_flag")

    # ── Acceptance: a stop supplied after freeze is refused, leaving no stop ──
    def test_stop_after_freeze_is_refused(self):
        trade_id = self._open_trade()
        stops.freeze(self.conn, trade_id)
        with self.assertRaises(stops.FrozenError):
            stops.set_stop(self.conn, trade_id, 9.0)
        a = stops.annotations(self.conn, trade_id)
        self.assertIsNone(a["stop"])          # the hole stays — no Risk %, no R
        self.assertIsNone(a["stop_provenance"])

    def test_setup_after_freeze_is_refused(self):
        trade_id = self._open_trade()
        stops.set_setup(self.conn, trade_id, "other")
        stops.freeze(self.conn, trade_id)
        with self.assertRaises(stops.FrozenError):
            stops.set_setup(self.conn, trade_id, "base_breakout")
        self.assertEqual(stops.annotations(self.conn, trade_id)["setup"], "other")

    # ── Acceptance: the setup vocabulary is fixed to the three values ──
    def test_setup_vocabulary_is_fixed(self):
        trade_id = self._open_trade()
        self.assertEqual(
            stops.SETUP_VOCABULARY, ("base_breakout", "high_tight_flag", "other")
        )
        for value in stops.SETUP_VOCABULARY:
            stops.set_setup(self.conn, trade_id, value)
        with self.assertRaises(stops.UnknownSetup):
            stops.set_setup(self.conn, trade_id, "cup_and_handle")

    # ── An unknown Trade id is a bug surfaced, not a silent no-op ──
    def test_unknown_trade_is_refused(self):
        with self.assertRaises(stops.UnknownTrade):
            stops.set_stop(self.conn, 999, 9.0)
        with self.assertRaises(stops.UnknownTrade):
            stops.freeze(self.conn, 999)


if __name__ == "__main__":
    unittest.main()


class StopAboveEntryTest(unittest.TestCase):
    """A stop at or above entry is impossible on a long, and fails loudly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _trade(self):
        fills.insert_fills(self.conn, [
            _buy("b1", "FPNI", 100, 458.0, "2026-08-19T09:30:00-04:00"),
        ])
        trades.confirm(self.conn)
        return self.conn.execute("SELECT id FROM trade").fetchone()["id"]

    def test_a_fat_fingered_decimal_is_refused(self):
        """The real case: 430 typed as 4300 on a Rp458 entry."""
        tid = self._trade()
        with self.assertRaises(stops.StopAboveEntry):
            stops.set_stop(self.conn, tid, 4300.0)
        self.assertIsNone(
            self.conn.execute("SELECT stop FROM trade").fetchone()["stop"]
        )

    def test_a_stop_equal_to_entry_is_refused(self):
        # Not merely wrong — (entry − stop) is zero, so R divides by zero.
        tid = self._trade()
        with self.assertRaises(stops.StopAboveEntry):
            stops.set_stop(self.conn, tid, 458.0)

    def test_a_stop_below_entry_is_accepted(self):
        tid = self._trade()
        self.assertEqual(stops.set_stop(self.conn, tid, 430.0), stops.RECORDED)

    def test_confirm_refuses_before_committing_anything(self):
        """The batch must be atomic: set_stop commits, so validate up front."""
        fills.insert_fills(self.conn, [
            _buy("b1", "AAA", 100, 100.0, "2026-08-19T09:30:00-04:00"),
            _buy("b2", "BBB", 100, 200.0, "2026-08-19T09:30:00-04:00"),
        ])
        with self.assertRaises(stops.StopAboveEntry):
            trades.confirm(
                self.conn,
                stops_by_symbol={"AAA": 90.0, "BBB": 900.0},  # BBB is the bad one
                demand_stop=True,
            )
        # AAA's stop was fine, but nothing lands while the batch contains a bad one.
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM trade").fetchone()["n"], 0
        )
