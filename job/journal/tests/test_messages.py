"""The journal messages you back (#53, ADR 0012).

The seam is the daily run with an injected fake relay that records what the run
asked it to send, plus Fills seeded straight into the ledger. Every test reads
what went out through that fake and what the run row recorded.
"""

import json
import os
import tempfile
import unittest

from journal import books, db, fills, flex, trades
from journal.relay_client import Offer
from journal.run import execute_run

FIXTURE = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "docs", "samples", "relay-pull-fixture.json"
)


def _buy(ref, symbol, qty, price, when, book="IDX", source="stockbit"):
    return flex.Fill(
        source=source, source_ref=ref, revision=1, book=book, symbol=symbol,
        side="BUY", quantity=float(qty), price=float(price), commission=0.0,
        executed_at=when, order_id="o1",
    )


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


def _fixture_item(relay_id):
    with open(FIXTURE, encoding="utf-8") as fh:
        [item] = [i for i in json.load(fh)["items"] if i["id"] == relay_id]
    return item


class FakeRelay:
    """The relay seam: items to pull in, messages sent out."""

    def __init__(self, items=(), fail_send=None):
        self.items = list(items)
        self.sent = []      # the text of each message
        self.calls = []     # (text, trade, offer) per message
        self.fail_send = fail_send

    def pull(self):
        return [dict(i) for i in self.items]

    def acknowledge(self, ids):
        self.items = [i for i in self.items if i.get("id") not in set(ids)]

    def send(self, text, *, trade=None, offer=None):
        if self.fail_send is not None:
            raise self.fail_send
        self.sent.append(text)
        self.calls.append((text, trade, offer))
        return 500 + len(self.sent)


class MessagesTest(unittest.TestCase):
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

    def _messages_row(self, run_id):
        return self.conn.execute(
            "SELECT * FROM run_pass WHERE run_id = ? AND name = 'messages'", (run_id,)
        ).fetchone()

    # -- the undeclared Trade nag -------------------------------------------

    def test_an_undeclared_new_trade_gets_one_nag_stating_the_trading_days_left(self):
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"))
        self._bars("IDX", "BBRI", "2026-10-13", "2026-10-14")
        relay = FakeRelay()
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        [text] = relay.sent
        self.assertIn("IDX BBRI", text)
        self.assertIn("1,000", text)
        self.assertIn("2026-10-13", text)
        self.assertIn("2 trading days left", text)

    def test_the_nag_states_the_window_has_closed_once_it_has(self):
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-10-08T09:20:00+07:00"))
        self._bars("IDX", "BBRI", "2026-10-08", "2026-10-09", "2026-10-12",
                   "2026-10-13", "2026-10-14")
        relay = FakeRelay()
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        [text] = relay.sent
        self.assertIn("grace window has closed", text)

    def test_a_symbol_with_no_bars_yet_is_counted_on_the_book_calendar(self):
        # Bars are fetched for committed Trades, so a first entry in a symbol has
        # none; the book's benchmark is its calendar until they arrive.
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"))
        self._bars("IDX", books.BENCHMARKS["IDX"], "2026-10-13", "2026-10-14")
        relay = FakeRelay()
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        [text] = relay.sent
        self.assertIn("2 trading days left", text)

    def test_the_nag_says_so_when_the_window_cannot_be_counted(self):
        # No bars for the symbol or its book: an invented count is worse than none.
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"))
        relay = FakeRelay()
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        [text] = relay.sent
        self.assertIn("cannot be counted", text)
        self.assertNotIn("days left", text)

    def test_a_second_run_the_same_day_does_not_resend_and_a_later_day_does(self):
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"))
        self._bars("IDX", "BBRI", "2026-10-13", "2026-10-14", "2026-10-15")
        relay = FakeRelay()
        execute_run(self.conn, as_of="2026-10-14", relay=relay)
        execute_run(self.conn, as_of="2026-10-14", relay=relay)
        self.assertEqual(len(relay.sent), 1)

        execute_run(self.conn, as_of="2026-10-15", relay=relay)
        self.assertEqual(len(relay.sent), 2)
        self.assertIn("1 trading day left", relay.sent[1])

    def test_each_undeclared_trade_gets_its_own_nag(self):
        self._fills(
            _buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"),
            _buy("b2", "TLKM", 500, 3000, "2026-10-13T09:30:00+07:00"),
        )
        relay = FakeRelay()
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        self.assertEqual(len(relay.sent), 2)
        self.assertTrue(any("IDX BBRI" in t for t in relay.sent))
        self.assertTrue(any("IDX TLKM" in t for t in relay.sent))

    def test_a_bound_declaration_is_not_nagged(self):
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"))
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-13", "2026-10-13T02:15:00Z"),
        ])
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        self.assertEqual(relay.sent, [])

    def test_declined_and_frozen_trades_are_never_nagged(self):
        self._fills(
            _buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"),
            _buy("b2", "TLKM", 500, 3000, "2026-10-13T09:30:00+07:00"),
        )
        trades.confirm(
            self.conn,
            declined=[("IDX", "BBRI", "2026-10-13"), ("IDX", "TLKM", "2026-10-13")],
            demand_stop=True,
        )
        self.conn.execute("UPDATE trade SET frozen = 1 WHERE symbol = 'TLKM'")
        self.conn.commit()
        relay = FakeRelay()
        execute_run(self.conn, as_of="2026-10-14", relay=relay)
        execute_run(self.conn, as_of="2026-10-15", relay=relay)

        self.assertEqual(relay.sent, [])

    def test_the_nag_is_recorded_with_its_trade_and_telegram_message_id(self):
        # What a later reply to the nag reads to find its Trade.
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"))
        relay = FakeRelay()
        result = execute_run(self.conn, as_of="2026-10-14", relay=relay)

        row = self.conn.execute("SELECT * FROM outbound_message").fetchone()
        self.assertEqual(
            (row["kind"], row["book"], row["symbol"], row["entry_date"]),
            ("undeclared", "IDX", "BBRI", "2026-10-13"),
        )
        self.assertEqual(row["telegram_message_id"], 501)
        self.assertEqual(row["run_id"], result.run_id)
        self.assertEqual(row["text"], relay.sent[0])

    # -- answering a nag (#55) ------------------------------------------------

    def test_the_nag_names_its_trade_to_the_relay_so_a_reply_finds_it(self):
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"))
        relay = FakeRelay()
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        [(_, trade, offer)] = relay.calls
        self.assertEqual(trade, ("IDX", "BBRI", "2026-10-13"))
        self.assertIsNone(offer)

    def test_a_refusal_names_its_trade_too_so_a_bare_resend_finds_it(self):
        self._fills(_buy("b1", "BBRI", 1000, 4000, "2026-10-13T09:20:00+07:00"))
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-13", "2026-10-13T02:15:00Z"),
        ])
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        [(_, trade, _)] = relay.calls
        self.assertEqual(trade, ("IDX", "BBRI", "2026-10-13"))

    def test_a_reply_to_a_nag_binds_on_the_next_run(self):
        # The shared fixture's bare reply `2650 bb`, resolved by the relay to the
        # nag's ADRO Trade of 13 Oct.
        self._fills(_buy("a1", "ADRO", 1000, 2800, "2026-10-13T09:20:00+07:00"))
        relay = FakeRelay()
        execute_run(self.conn, as_of="2026-10-14", relay=relay)
        self.assertEqual(len(relay.sent), 1)

        relay.items = [_fixture_item("583201009")]
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        trade = self.conn.execute("SELECT * FROM trade WHERE symbol = 'ADRO'").fetchone()
        self.assertEqual(trade["entry_date"], "2026-10-13")
        self.assertEqual(trade["stop"], 2650)
        self.assertEqual(trade["setup"], "base_breakout")
        self.assertEqual(trade["auto_committed"], 1)

    def _split_day(self, first_stop=4120):
        """BBRI bought on 13 and 14 Oct; the 13th committed with ``first_stop``."""
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"))
        items = []
        if first_stop is not None:
            items.append(_declaration(
                "1", "IDX", "BBRI", first_stop, "2026-10-13", "2026-10-13T02:15:00Z"
            ))
        relay = FakeRelay(items)
        execute_run(self.conn, as_of="2026-10-13", relay=relay)
        self._fills(_buy("b2", "BBRI", 500, 4250, "2026-10-14T09:10:00+07:00"))
        self._bars("IDX", "BBRI", "2026-10-13", "2026-10-14", "2026-10-15")
        return relay

    def test_a_split_day_second_trade_is_offered_the_first_days_stop(self):
        relay = self._split_day()
        relay.sent.clear(); relay.calls.clear()
        execute_run(self.conn, as_of="2026-10-15", relay=relay)

        [(text, trade, offer)] = relay.calls
        self.assertEqual(trade, ("IDX", "BBRI", "2026-10-14"))
        self.assertEqual(offer, Offer(stop=4120, entry_date="2026-10-13"))
        self.assertIn("Same as BBRI 13 Oct (4,120)?", text)

    def test_tapping_yes_declares_the_first_stop_for_the_second_trade(self):
        # The shared fixture's tap: the relay's declaration of 4,120 for 14 Oct.
        relay = self._split_day()
        execute_run(self.conn, as_of="2026-10-15", relay=relay)
        relay.items = [_fixture_item("583201010")]
        execute_run(self.conn, as_of="2026-10-15", relay=relay)

        second = self.conn.execute(
            "SELECT * FROM trade WHERE symbol = 'BBRI' AND entry_date = '2026-10-14'"
        ).fetchone()
        self.assertEqual(second["stop"], 4120)
        self.assertEqual(second["stop_provenance"], "recorded")
        self.assertEqual(second["auto_committed"], 1)

    def test_no_offer_when_the_first_trade_has_no_stop(self):
        relay = self._split_day(first_stop=None)
        relay.sent.clear(); relay.calls.clear()
        execute_run(self.conn, as_of="2026-10-15", relay=relay)

        offers = {trade: offer for _, trade, offer in relay.calls}
        self.assertIsNone(offers[("IDX", "BBRI", "2026-10-14")])

    def test_no_offer_from_a_trade_entered_more_than_three_trading_days_before(self):
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-10-08T09:20:00+07:00"))
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-08", "2026-10-08T02:15:00Z"),
        ])
        execute_run(self.conn, as_of="2026-10-08", relay=relay)
        self._fills(_buy("b2", "BBRI", 500, 4250, "2026-10-14T09:10:00+07:00"))
        self._bars("IDX", "BBRI", "2026-10-08", "2026-10-09", "2026-10-12",
                   "2026-10-13", "2026-10-14")
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        [(text, trade, offer)] = relay.calls
        self.assertEqual(trade, ("IDX", "BBRI", "2026-10-14"))
        self.assertIsNone(offer)
        self.assertNotIn("Same as", text)

    def test_no_offer_from_an_old_trade_whose_bars_stopped_short(self):
        # The symbol's cached bars ended weeks ago, so they count no trading
        # days since; the calendar still says it is a separate entry.
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-09-01T09:20:00+07:00"))
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-09-01", "2026-09-01T02:15:00Z"),
        ])
        execute_run(self.conn, as_of="2026-09-01", relay=relay)
        self._bars("IDX", "BBRI", "2026-09-01", "2026-09-02")
        self._fills(_buy("b2", "BBRI", 500, 4250, "2026-10-14T09:10:00+07:00"))
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        [(_, trade, offer)] = relay.calls
        self.assertEqual(trade, ("IDX", "BBRI", "2026-10-14"))
        self.assertIsNone(offer)

    def test_no_offer_from_the_same_symbol_on_the_other_book(self):
        self._fills(_buy("u1", "BBRI", 10, 42, "2026-10-13T10:00:00-04:00", book="US", source="ibkr"))
        relay = FakeRelay([
            _declaration("1", "US", "BBRI", 40, "2026-10-13", "2026-10-13T14:15:00Z"),
        ])
        execute_run(self.conn, as_of="2026-10-13", relay=relay)
        self._fills(_buy("b2", "BBRI", 500, 4250, "2026-10-14T09:10:00+07:00"))
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        [(_, trade, offer)] = relay.calls
        self.assertEqual(trade, ("IDX", "BBRI", "2026-10-14"))
        self.assertIsNone(offer)

    # -- the refusal ----------------------------------------------------------

    def test_a_refused_stop_produces_one_refusal_naming_both_prices(self):
        self._fills(_buy("b1", "BBRI", 1000, 4000, "2026-10-13T09:20:00+07:00"))
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-13", "2026-10-13T02:15:00Z"),
        ])
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        # One message for the Trade this run: the refusal, not a nag beside it.
        [text] = relay.sent
        self.assertIn("IDX BBRI", text)
        self.assertIn("4,120", text)
        self.assertIn("4,000", text)
        self.assertIn("resend", text.lower())

    def test_a_refusal_is_sent_once_however_many_runs_see_the_declaration(self):
        self._fills(_buy("b1", "BBRI", 1000, 4000, "2026-10-13T09:20:00+07:00"))
        relay = FakeRelay([
            _declaration("1", "IDX", "BBRI", 4120, "2026-10-13", "2026-10-13T02:15:00Z"),
        ])
        execute_run(self.conn, as_of="2026-10-14", relay=relay)
        execute_run(self.conn, as_of="2026-10-14", relay=relay)
        execute_run(self.conn, as_of="2026-10-15", relay=relay)

        refusals = [t for t in relay.sent if "4,120" in t]
        self.assertEqual(len(refusals), 1)

    # -- failure and absence -------------------------------------------------

    def test_a_failed_send_is_recorded_on_the_run_row_and_does_not_fail_the_run(self):
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"))
        relay = FakeRelay(fail_send=ConnectionError("relay unreachable"))
        result = execute_run(self.conn, as_of="2026-10-14", relay=relay)

        row = self._messages_row(result.run_id)
        self.assertEqual(row["status"], "error")
        self.assertIn("relay unreachable", row["detail"])
        self.assertNotEqual(result.status, "error")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM run WHERE id = ?", (result.run_id,)
        ).fetchone()["status"], result.status)

    def test_a_message_that_failed_to_send_goes_out_on_the_next_run(self):
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"))
        relay = FakeRelay(fail_send=ConnectionError("relay unreachable"))
        execute_run(self.conn, as_of="2026-10-14", relay=relay)
        relay.fail_send = None
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        self.assertEqual(len(relay.sent), 1)

    def test_the_run_row_counts_what_was_sent(self):
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"))
        result = execute_run(self.conn, as_of="2026-10-14", relay=FakeRelay())

        row = self._messages_row(result.run_id)
        self.assertEqual(row["status"], "ran")
        self.assertIn("1 message(s) sent", row["detail"])

    def test_no_relay_configured_skips_the_pass(self):
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"))
        result = execute_run(self.conn, as_of="2026-10-14")

        row = self._messages_row(result.run_id)
        self.assertEqual(row["status"], "ran")
        self.assertIn("skipped", row["detail"])

    def test_the_banner_nags_stay(self):
        self._fills(_buy("b1", "BBRI", 1000, 4200, "2026-10-13T09:20:00+07:00"))
        result = execute_run(self.conn, as_of="2026-10-14", relay=FakeRelay())
        kinds = {n.kind for n in result.nags}
        self.assertIn("idx_intake", kinds)


if __name__ == "__main__":
    unittest.main()
