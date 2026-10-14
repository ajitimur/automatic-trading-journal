"""The journal messages you back (#53, ADR 0012).

The seam is the daily run with an injected fake relay that records what the run
asked it to send, plus Fills seeded straight into the ledger. Every test reads
what went out through that fake and what the run row recorded.
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
