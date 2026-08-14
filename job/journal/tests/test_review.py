"""The review-surface write actions (SPEC §11.3, #40).

The weekly review surface revises on inspection — it marks a Trade reviewed
(so a skipped week does not silently lose its Trades and *Reviewed →* has
something to drain), edits a free-text note, and overrides the exit reason the
confirm queue accepted unread. None of these were ever queue-committed, so
writing them straight through does not breach the one-door rule (SPEC §5.1).

Unlike the two hand-entered fields, none of these is locked by freeze: a
straggler is reviewed precisely because it is old, and a note or a corrected
reason is a post-hoc revision, not a chaseable input.
"""

import os
import tempfile
import unittest

from journal import db, fills, flex, review, trades


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


class ReviewActionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _open_trade(self, symbol="AAA"):
        fills.insert_fills(
            self.conn, [_buy("b1", symbol, 100, 10.0, "2026-08-03T09:30:00-04:00")]
        )
        trades.confirm(self.conn)
        return self.conn.execute(
            "SELECT id FROM trade WHERE symbol = ?", (symbol,)
        ).fetchone()["id"]

    def _close_trade(self, symbol="AAA"):
        fills.insert_fills(
            self.conn, [_sell("s1", symbol, 100, 12.0, "2026-08-07T10:00:00-04:00")]
        )
        trades.confirm(self.conn)

    # ── mark reviewed writes a timestamp; a fresh Trade is unreviewed ──
    def test_mark_reviewed_records_the_timestamp(self):
        trade_id = self._open_trade()
        self.assertIsNone(review.get(self.conn, trade_id)["reviewed_at"])
        review.mark_reviewed(self.conn, trade_id, at="2026-08-14T12:00:00Z")
        self.assertEqual(
            review.get(self.conn, trade_id)["reviewed_at"], "2026-08-14T12:00:00Z"
        )

    # ── reviewing survives freeze — a straggler is reviewed *because* it is old ──
    def test_mark_reviewed_allowed_after_freeze(self):
        trade_id = self._open_trade()
        from journal import stops
        stops.freeze(self.conn, trade_id)
        review.mark_reviewed(self.conn, trade_id, at="2026-08-14T12:00:00Z")
        self.assertEqual(
            review.get(self.conn, trade_id)["reviewed_at"], "2026-08-14T12:00:00Z"
        )

    # ── a note round-trips and is editable ──
    def test_set_note_round_trips(self):
        trade_id = self._open_trade()
        review.set_note(self.conn, trade_id, "Took the partial a day late on purpose.")
        self.assertEqual(
            review.get(self.conn, trade_id)["note"],
            "Took the partial a day late on purpose.",
        )
        review.set_note(self.conn, trade_id, "Reconsidered.")
        self.assertEqual(review.get(self.conn, trade_id)["note"], "Reconsidered.")

    # ── override the exit reason the queue accepted unread ──
    def test_override_exit_reason(self):
        trade_id = self._open_trade()
        self._close_trade()
        exit_row = self.conn.execute(
            "SELECT id FROM trade_exit WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        review.override_exit_reason(self.conn, exit_row["id"], "discretionary")
        self.assertEqual(
            self.conn.execute(
                "SELECT reason FROM trade_exit WHERE id = ?", (exit_row["id"],)
            ).fetchone()["reason"],
            "discretionary",
        )

    # ── a reason outside the vocabulary is refused, never coerced ──
    def test_override_exit_reason_rejects_unknown(self):
        trade_id = self._open_trade()
        self._close_trade()
        exit_id = self.conn.execute(
            "SELECT id FROM trade_exit WHERE trade_id = ?", (trade_id,)
        ).fetchone()["id"]
        with self.assertRaises(review.UnknownReason):
            review.override_exit_reason(self.conn, exit_id, "made_up")

    # ── unknown ids surface, they are not silent no-ops ──
    def test_unknown_ids_raise(self):
        with self.assertRaises(review.UnknownTrade):
            review.mark_reviewed(self.conn, 9999, at="2026-08-14T12:00:00Z")
        with self.assertRaises(review.UnknownTrade):
            review.set_note(self.conn, 9999, "x")
        with self.assertRaises(review.UnknownExit):
            review.override_exit_reason(self.conn, 9999, "discretionary")


if __name__ == "__main__":
    unittest.main()
