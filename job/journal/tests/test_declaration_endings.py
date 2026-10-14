"""Declarations that don't meet a new Trade (#54, ADR 0012).

A live Stop Declaration has three endings besides binding a ``new-trade``: it
lapses when nothing matches it for three trading days, it sets the Stop of a
Trade already committed without one, or it is refused because the Trade already
has a Stop or is frozen.

The seam is the daily run with an injected fake relay that both feeds
declarations in and records what the run sent back, plus Fills, bars and
committed Trades seeded through the journal's own doors. Tests read what the
Trade holds afterwards and what the bot said.
"""

import os
import tempfile
import unittest

from journal import books, db, fills, flex, stops, trades
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


def _declaration(relay_id, book, symbol, stop, entry_date, telegram_at, setup=None):
    parse = {
        "kind": "declaration", "book": book, "symbol": symbol,
        "stop": stop, "entry_date": entry_date,
    }
    if setup is not None:
        parse["setup"] = setup
    return {"id": relay_id, "telegram_at": telegram_at, "text": f"{symbol} {stop}", "parse": parse}


def _decline(relay_id, book, symbol, entry_date, telegram_at):
    return {
        "id": relay_id, "telegram_at": telegram_at, "text": f"{symbol} no sl",
        "parse": {"kind": "decline", "book": book, "symbol": symbol, "entry_date": entry_date},
    }


class FakeRelay:
    """The relay seam: items to pull in, messages sent out."""

    def __init__(self, items=(), fail_send=None):
        self.items = list(items)
        self.sent = []
        self.fail_send = fail_send

    def pull(self):
        return [dict(i) for i in self.items]

    def acknowledge(self, ids):
        self.items = [i for i in self.items if i.get("id") not in set(ids)]

    def send(self, text):
        if self.fail_send is not None:
            raise self.fail_send
        self.sent.append(text)
        return 900 + len(self.sent)


IDX_BENCH = books.BENCHMARKS["IDX"]


class DeclarationEndingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved_archive = os.environ.pop("JOURNAL_ARCHIVE_DIR", None)
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        if self._saved_archive is not None:
            os.environ["JOURNAL_ARCHIVE_DIR"] = self._saved_archive
        self.tmp.cleanup()

    def _fills(self, *rows):
        fills.insert_fills(self.conn, list(rows))
        self.conn.commit()

    def _bars(self, book, symbol, *dates):
        for d in dates:
            self.conn.execute(
                "INSERT INTO bar (book, symbol, date, open, high, low, close, volume) "
                "VALUES (?, ?, ?, 1, 1, 1, 1, 100)",
                (book, symbol, d),
            )
        self.conn.commit()

    def _trade(self, symbol):
        return self.conn.execute(
            "SELECT * FROM trade WHERE symbol = ?", (symbol,)
        ).fetchone()

    # -- lapse --------------------------------------------------------------

    def test_a_declaration_with_no_fills_after_three_trading_days_lapses_once(self):
        # Entry Tue 13 Oct; the book trades 14, 15, 16 — the third trading day
        # after entry is 16 Oct, and still no BBRI Fill has arrived.
        self._bars("IDX", IDX_BENCH, "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16")
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-13", "2026-10-13T02:15:00Z"),
        ])
        execute_run(self.conn, as_of="2026-10-15", relay=relay)
        self.assertEqual(relay.sent, [])  # two trading days in: still live

        execute_run(self.conn, as_of="2026-10-16", relay=relay)
        [text] = relay.sent
        self.assertIn("BBRI SL 4,120 for 13 Oct lapsed, no fills arrived", text)

        execute_run(self.conn, as_of="2026-10-19", relay=relay)
        self.assertEqual(len(relay.sent), 1)  # one lapse message, ever

    def test_fills_arriving_after_the_lapse_do_not_bind_it(self):
        self._bars("IDX", IDX_BENCH, "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16")
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-13", "2026-10-13T02:15:00Z"),
        ])
        execute_run(self.conn, as_of="2026-10-16", relay=relay)

        # The TC turns up late, and BBRI is bought again on 19 Oct.
        self._fills(
            _buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"),
            _buy("b2", "BBRI", 500, 4300, "2026-10-19T09:20:00+07:00"),
        )
        execute_run(self.conn, as_of="2026-10-19", relay=relay)

        self.assertIsNone(self._trade("BBRI"))  # neither entry was committed
        self.assertEqual(
            sorted((p.kind, p.entry_date) for p in trades.propose(self.conn)),
            [("new-trade", "2026-10-13"), ("new-trade", "2026-10-19")],
        )

    def test_with_no_bars_cached_a_declaration_lapses_on_calendar_days(self):
        relay = FakeRelay([
            _decline("1", "IDX", "BBRX", "2026-10-13", "2026-10-13T02:15:00Z"),
        ])
        execute_run(self.conn, as_of="2026-10-15", relay=relay)
        self.assertEqual(relay.sent, [])

        execute_run(self.conn, as_of="2026-10-16", relay=relay)
        [text] = relay.sent
        self.assertIn("BBRX no sl for 13 Oct lapsed", text)

    def test_a_lapse_message_that_failed_to_send_goes_out_on_the_next_run(self):
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-13", "2026-10-13T02:15:00Z"),
        ], fail_send=OSError("relay unreachable"))
        execute_run(self.conn, as_of="2026-10-16", relay=relay)
        self.assertEqual(relay.sent, [])

        relay.fail_send = None
        execute_run(self.conn, as_of="2026-10-19", relay=relay)
        [text] = relay.sent
        self.assertIn("lapsed", text)

    # -- a Trade already committed without a Stop ---------------------------

    def _commit_stopless(self, symbol, price, when, *more):
        """A Trade confirmed at the laptop with no stop answer."""
        self._fills(_buy("b-" + symbol, symbol, 1000, price, when), *more)
        trades.confirm(self.conn)

    def test_a_declaration_sets_the_stop_of_a_committed_stopless_trade(self):
        self._commit_stopless("BBRI", 4200, "2026-10-13T09:20:00+07:00")
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-13", "2026-10-13T02:15:00Z",
                         setup="high_tight_flag"),
        ])
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        trade = self._trade("BBRI")
        self.assertEqual(trade["stop"], 4120)
        self.assertEqual(trade["stop_provenance"], "recorded")
        self.assertEqual(trade["setup"], "high_tight_flag")
        self.assertEqual(trade["auto_committed"], 0)  # it was committed by hand
        self.assertEqual(relay.sent, [])  # nothing to refuse, nothing lapsed

    def test_the_provenance_dates_from_when_the_declaration_was_said(self):
        # Entered 5 Oct, exited 7 Oct. A stop declared on 6 Oct is inside the
        # grace window; the same stop declared on 13 Oct, four trading days
        # after entry with an Exit on record, reads reconstructed.
        days = ("2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
                "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14")
        for symbol in ("BBRI", "TLKM"):
            self._commit_stopless(
                symbol, 4200, "2026-10-05T09:20:00+07:00",
                _sell("s-" + symbol, symbol, 1000, 4500, "2026-10-07T14:00:00+07:00"),
            )
            self._bars("IDX", symbol, *days)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM trade_exit").fetchone()["c"], 2)

        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-05", "2026-10-06T02:15:00Z"),
            _declaration("2", "IDX", "TLKM", 4120, "2026-10-05", "2026-10-13T02:15:00Z"),
        ])
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        self.assertEqual(self._trade("BBRI")["stop_provenance"], "recorded")
        self.assertEqual(self._trade("TLKM")["stop"], 4120)
        self.assertEqual(self._trade("TLKM")["stop_provenance"], "reconstructed")

    def test_a_declared_stop_undeclines_a_declined_trade(self):
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"))
        trades.confirm(self.conn, declined={("IDX", "BBRI", "2026-10-13")})
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-13", "2026-10-13T02:15:00Z"),
        ])
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        trade = self._trade("BBRI")
        self.assertEqual(trade["stop"], 4120)
        self.assertEqual(trade["stop_declined"], 0)

    # -- refused: the Stop is immutable, and a frozen Trade is locked --------

    def test_a_declaration_for_a_trade_that_has_a_stop_is_refused_naming_it(self):
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"))
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-13", "2026-10-13T02:15:00Z"),
        ])
        execute_run(self.conn, as_of="2026-10-13", relay=relay)  # binds and commits
        self.assertEqual(self._trade("BBRI")["stop"], 4120)

        relay.items = [
            _declaration("2", "IDX", "BBRI", 4000, "2026-10-13", "2026-10-14T01:00:00Z",
                         setup="other"),
        ]
        execute_run(self.conn, as_of="2026-10-14", relay=relay)
        execute_run(self.conn, as_of="2026-10-15", relay=relay)

        trade = self._trade("BBRI")
        self.assertEqual(trade["stop"], 4120)  # unchanged
        self.assertIsNone(trade["setup"])
        [text] = relay.sent  # refused once, however many runs see it
        self.assertIn("IDX BBRI", text)
        self.assertIn("already has SL 4,120", text)
        self.assertIn("4,000", text)

    def test_a_no_sl_for_a_trade_that_has_a_stop_is_refused(self):
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"))
        trades.confirm(self.conn, stops_by_trade={("IDX", "BBRI", "2026-10-13"): 4120})
        relay = FakeRelay([_decline("1", "IDX", "BBRI", "2026-10-13", "2026-10-14T01:00:00Z")])
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        trade = self._trade("BBRI")
        self.assertEqual((trade["stop"], trade["stop_declined"]), (4120, 0))
        [text] = relay.sent
        self.assertIn("already has SL 4,120", text)

    def test_a_declaration_for_a_frozen_trade_changes_nothing_and_says_so(self):
        self._commit_stopless("BBRI", 4200, "2026-10-13T09:20:00+07:00")
        stops.freeze(self.conn, self._trade("BBRI")["id"])
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-13", "2026-10-13T02:15:00Z",
                         setup="other"),
        ])
        execute_run(self.conn, as_of="2026-10-14", relay=relay)
        execute_run(self.conn, as_of="2026-10-15", relay=relay)

        trade = self._trade("BBRI")
        self.assertIsNone(trade["stop"])
        self.assertIsNone(trade["setup"])
        [text] = relay.sent
        self.assertIn("IDX BBRI", text)
        self.assertIn("frozen", text)

    def test_a_stop_above_entry_on_a_committed_trade_is_refused_not_raised(self):
        self._commit_stopless("BBRI", 4200, "2026-10-13T09:20:00+07:00")
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4300, "2026-10-13", "2026-10-13T02:15:00Z"),
        ])
        result = execute_run(self.conn, as_of="2026-10-14", relay=relay)

        self.assertIsNone(self._trade("BBRI")["stop"])
        bind = self.conn.execute(
            "SELECT * FROM run_pass WHERE run_id = ? AND name = 'bind'", (result.run_id,)
        ).fetchone()
        self.assertEqual(bind["status"], "ran")
        self.assertIn("1 refused", bind["detail"])
        [text] = relay.sent
        self.assertIn("at or above the entry average 4,200", text)

    def test_a_refused_declaration_never_lapses(self):
        self._commit_stopless("BBRI", 4200, "2026-10-13T09:20:00+07:00")
        stops.freeze(self.conn, self._trade("BBRI")["id"])
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-13", "2026-10-13T02:15:00Z"),
        ])
        execute_run(self.conn, as_of="2026-10-20", relay=relay)

        [text] = relay.sent
        self.assertNotIn("lapsed", text)


if __name__ == "__main__":
    unittest.main()
