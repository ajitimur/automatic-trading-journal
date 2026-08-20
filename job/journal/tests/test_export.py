"""The LLM export (SPEC §12, #41) — one test per acceptance criterion.

Fixtures write the trade and its derived tables straight through SQL, the same
style as ``test_book_history.py``: the export is a *read* over already-enriched
tables, so the tests seed those tables rather than run the daily passes.
"""

import json
import os
import tempfile
import unittest
from datetime import date, timedelta

from journal import books, cli, db, export


def _bars(conn, book, symbol, start, days, close=10.0):
    d = date.fromisoformat(start)
    for i in range(days):
        dd = (d + timedelta(days=i)).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO bar(book,symbol,date,open,high,low,close,volume) "
            "VALUES(?,?,?,?,?,?,?,1000)",
            (book, symbol, dd, close, close + 1, close - 1, close),
        )
    conn.commit()


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "journal.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # ── a full-trade fixture, all derived tables ──
    def _trade(self, *, tid, book="US", symbol="AAA", entry, exit_date,
               entry_qty=1000, avg=100.0, stop=90.0, provenance="recorded",
               setup="base_breakout", status="closed",
               exits, exit_avg=None, adr_pct=5.0, ma_dist_10=1.0,
               mfe_high=130.0, mae_low=95.0, fwd_return_20d=-5.0, fwd_high=140.0,
               prior_move_63d=20.0, div_drag=None, nominal_status="resolved",
               nominal_legs=None, note="a note", regime_entry="uptrend",
               regime_exit="neutral", equity=100000.0, partial_state="in_band"):
        c = self.conn
        c.execute(
            "INSERT INTO trade(id,book,symbol,entry_date,entry_qty,entry_avg_price,"
            "status,stop,setup,stop_provenance,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (tid, book, symbol, entry, entry_qty, avg, status, stop, setup,
             provenance, note),
        )
        for i, (d, q, p, r) in enumerate(exits):
            c.execute(
                "INSERT INTO trade_exit(trade_id,source,source_ref,exit_date,"
                "quantity,price,reason) VALUES(?,'ibkr',?,?,?,?,?)",
                (tid, f"{tid}-{i}", d, q, p, r),
            )
        if exit_avg is None and exits:
            tot = sum(q for _, q, _, _ in exits)
            exit_avg = sum(q * p for _, q, p, _ in exits) / tot
        c.execute(
            "INSERT INTO trade_enrichment(trade_id,book,symbol,entry_date,bar_date,"
            "adr_pct,ma_dist_10,ma_dist_50,ma_dist_200,stack_state,prior_move_63d,"
            "pct_off_52w_high,rs_63d,volume_ratio) "
            "VALUES(?,?,?,?,?,?,?,3.5,6.0,'aligned_up',?,-2.0,15.0,2.0)",
            (tid, book, symbol, entry, entry, adr_pct, ma_dist_10, prior_move_63d),
        )
        c.execute(
            "INSERT INTO trade_exit_geometry(trade_id,book,symbol,exit_date,bar_date,"
            "exit_avg_price,ma_dist_10_at_exit,ma_dist_50_at_exit) "
            "VALUES(?,?,?,?,?,?,1.2,4.0)",
            (tid, book, symbol, exit_date, exit_date, exit_avg),
        )
        c.execute(
            "INSERT INTO trade_excursion(trade_id,start_date,end_date,mfe_high,"
            "mfe_date,mae_low,mae_date) VALUES(?,?,?,?,?,?,?)",
            (tid, entry, exit_date, mfe_high, exit_date, mae_low, entry),
        )
        c.execute(
            "INSERT INTO trade_post_exit(trade_id,revision,final_exit_date,cx,"
            "exit_avg_price,fwd_return_20d,fwd_close_20d,fwd_high,fwd_high_date,"
            "fwd_low,fwd_low_date,created_at) "
            "VALUES(?,1,?,?,?,?,?,?,?,?,?,?)",
            (tid, exit_date, exit_avg, exit_avg, fwd_return_20d, exit_avg, fwd_high,
             exit_date, mae_low, entry, "2026-01-01"),
        )
        legs = nominal_legs or [
            {"date": exit_date, "price": exit_avg, "fraction": 1.0,
             "trigger": "trail", "limit_locked": False},
        ]
        fit = json.dumps({"ma10/day3": 0, "ma10/none": 2})
        c.execute(
            "INSERT INTO trade_counterfactual(trade_id,book,symbol,entry_date,"
            "entry_qty,entry_avg_price,stop,stop_provenance,ruleset_version,"
            "nominal_variant,stopless,partial_state,exit_path,nominal_status,"
            "fit_vector,dividend_drag_r) "
            "VALUES(?,?,?,?,?,?,?,?,'v1','ma10/day3',?,?,'trail',?,?,?)",
            (tid, book, symbol, entry, entry_qty, avg, stop, provenance,
             1 if stop is None else 0, partial_state, nominal_status, fit, div_drag),
        )
        c.execute(
            "INSERT INTO counterfactual_variant(trade_id,variant,trail,partial,"
            "status,stopless,legs) VALUES(?,'ma10/day3','ma10','day3',?,?,?)",
            (tid, nominal_status, 1 if stop is None else 0, json.dumps(legs)),
        )
        # a second, better resolved variant so best_variant_r has a max to find
        c.execute(
            "INSERT INTO counterfactual_variant(trade_id,variant,trail,partial,"
            "status,stopless,legs) VALUES(?,'ma10/none','ma10','none','resolved',?,?)",
            (tid, 1 if stop is None else 0,
             json.dumps([{"date": exit_date, "price": (exit_avg or avg) + 10,
                          "fraction": 1.0, "trigger": "trail", "limit_locked": False}])),
        )
        if regime_entry:
            c.execute("INSERT OR IGNORE INTO regime_snapshot(book,date,bar_date,label) "
                      "VALUES(?,?,?,?)", (book, entry, entry, regime_entry))
        if regime_exit:
            c.execute("INSERT OR IGNORE INTO regime_snapshot(book,date,bar_date,label) "
                      "VALUES(?,?,?,?)", (book, exit_date, exit_date, regime_exit))
        if equity is not None:
            c.execute("INSERT OR IGNORE INTO equity_snapshot(book,date,equity,"
                      "provenance,source) VALUES(?,?,?,'stated','ibkr')",
                      (book, entry, equity))
        c.commit()
        _bars(self.conn, book, symbol, entry, 25)

    def _lines(self, text):
        """The JSONL object lines (everything after the trade-note comment)."""
        out = []
        in_body = False
        for ln in text.splitlines():
            if ln.startswith("# One JSON object per trade"):
                in_body = True
                continue
            if in_body and ln.strip():
                out.append(json.loads(ln))
        return out

    # ── criteria ──

    def test_jsonl_one_object_per_trade_variable_exits(self):
        self._trade(tid=1, symbol="AAA", entry="2026-04-20", exit_date="2026-05-01",
                    exits=[("2026-04-25", 400, 110.0, "partial_strength"),
                           ("2026-04-28", 300, 115.0, "discretionary"),
                           ("2026-05-01", 300, 120.0, "close_below_ma10")])
        self._trade(tid=2, symbol="BBB", entry="2026-04-22", exit_date="2026-04-23",
                    exits=[("2026-04-23", 1000, 88.0, "stop_hit")])
        rows = self._lines(export.export(self.conn, book="US"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0]["exits"]), 3)
        self.assertEqual(len(rows[1]["exits"]), 1)
        self.assertEqual([e["reason"] for e in rows[1]["exits"]], ["stop_hit"])

    def test_adr_and_r_levels_only_two_prices_no_equity(self):
        self._trade(tid=1, entry="2026-04-20", exit_date="2026-05-01",
                    exits=[("2026-05-01", 1000, 120.0, "close_below_ma10")])
        row = self._lines(export.export(self.conn, book="US"))[0]
        # exactly two price levels
        self.assertIn("entry_avg_price", row)
        self.assertIn("stop", row)
        # the equity level (a price) never ships — only Risk %/Exposure %, which
        # are percents named *_of_equity, do.
        for forbidden in ("equity", "equity_level", "nav", "equity_snapshot"):
            self.assertNotIn(forbidden, row)
        self.assertIn("risk_pct_of_equity", row)
        self.assertIn("exposure_pct_of_equity", row)
        # distances in ADR, levels in R
        self.assertAlmostEqual(row["stop_distance_adr"], 2.0, places=2)  # (100-90)/100*100/5
        self.assertAlmostEqual(row["realized_r"], 2.0, places=2)  # (120-100)/(100-90)
        self.assertAlmostEqual(row["mfe_r"], 3.0, places=2)  # (130-100)/10

    def test_capture_ratio_null_unless_favour_and_profit(self):
        # went in favour AND finished in profit → a ratio
        self._trade(tid=1, symbol="WIN", entry="2026-04-20", exit_date="2026-05-01",
                    exits=[("2026-05-01", 1000, 120.0, "close_below_ma10")],
                    mfe_high=140.0)
        # a loser (realized R < 0) whose MFE was positive → still null
        self._trade(tid=2, symbol="LOS", entry="2026-04-21", exit_date="2026-04-22",
                    exits=[("2026-04-22", 1000, 88.0, "stop_hit")],
                    mfe_high=103.0)
        rows = {r["symbol"]: r for r in self._lines(export.export(self.conn, book="US"))}
        self.assertIsNotNone(rows["WIN"]["capture_ratio"])
        self.assertIsNone(rows["LOS"]["capture_ratio"])

    def test_dividend_drag_omitted_when_null_present_carries_no_pctile(self):
        self._trade(tid=1, symbol="DRY", entry="2026-04-20", exit_date="2026-05-01",
                    exits=[("2026-05-01", 1000, 120.0, "x")], div_drag=None)
        self._trade(tid=2, symbol="WET", entry="2026-04-22", exit_date="2026-05-02",
                    exits=[("2026-05-02", 1000, 120.0, "x")], div_drag=-0.41)
        rows = {r["symbol"]: r for r in self._lines(export.export(self.conn, book="US"))}
        self.assertNotIn("dividend_drag_r", rows["DRY"])       # omitted entirely
        self.assertIn("dividend_drag_r", rows["WET"])
        self.assertNotIn("dividend_drag_r_pctile", rows["WET"])  # no percentile ever

    def test_percentiles_are_export_relative_and_five_of_them(self):
        # three trades with distinct days_held so the percentile spread is real
        self._trade(tid=1, symbol="AAA", entry="2026-04-20", exit_date="2026-04-24",
                    exits=[("2026-04-24", 1000, 120.0, "x")], adr_pct=5.0, ma_dist_10=0.5,
                    prior_move_63d=10.0)
        self._trade(tid=2, symbol="BBB", entry="2026-04-20", exit_date="2026-05-10",
                    exits=[("2026-05-10", 1000, 120.0, "x")], adr_pct=5.0, ma_dist_10=1.5,
                    prior_move_63d=30.0)
        self._trade(tid=3, symbol="CCC", entry="2026-04-20", exit_date="2026-05-20",
                    exits=[("2026-05-20", 1000, 120.0, "x")], adr_pct=5.0, ma_dist_10=2.5,
                    prior_move_63d=50.0)
        rows = self._lines(export.export(self.conn, book="US"))
        pctile_keys = {k for r in rows for k in r if k.endswith("_pctile")}
        self.assertEqual(pctile_keys, {f"{f}_pctile" for f in export.PCTILE_FIELDS})
        # export-relative: the shortest hold ranks 0, the longest 100
        by_sym = {r["symbol"]: r for r in rows}
        self.assertEqual(by_sym["AAA"]["days_held_pctile"], 0)
        self.assertEqual(by_sym["BBB"]["days_held_pctile"], 50)
        self.assertEqual(by_sym["CCC"]["days_held_pctile"], 100)
        # each _pctile is rendered immediately after the field it ranks
        keys = list(rows[0].keys())
        self.assertEqual(keys[keys.index("days_held") + 1], "days_held_pctile")

    def test_book_drawdown_is_absolute_no_pctile(self):
        self._trade(tid=1, entry="2026-04-20", exit_date="2026-05-01",
                    exits=[("2026-05-01", 1000, 120.0, "x")])
        row = self._lines(export.export(self.conn, book="US"))[0]
        self.assertIn("book_drawdown_r_at_entry", row)             # ships
        self.assertNotIn("book_drawdown_r_at_entry_pctile", row)   # absolute, not ranked
        # under the 20-trade floor it is null — and that is NOT a drawdown of zero
        self.assertIsNone(row["book_drawdown_r_at_entry"])

    def test_seq_gaps_stay_uncompacted_in_a_slice(self):
        for i, (sym, entry, exit_date) in enumerate([
            ("AAA", "2026-04-01", "2026-04-10"),
            ("BBB", "2026-05-01", "2026-05-10"),
            ("CCC", "2026-06-01", "2026-06-10"),
        ], start=1):
            self._trade(tid=i, symbol=sym, entry=entry, exit_date=exit_date,
                        exits=[(exit_date, 1000, 120.0, "x")])
        # slice to only the middle trade
        rows = self._lines(export.export(self.conn, book="US",
                                         date_from="2026-05-01", date_to="2026-05-31"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "BBB")
        self.assertEqual(rows[0]["seq"], 2)  # NOT renumbered to 1

    def test_aggregates_carry_n_and_drawdown_exclusion(self):
        self._trade(tid=1, entry="2026-04-20", exit_date="2026-05-01",
                    exits=[("2026-05-01", 1000, 120.0, "x")], stop=90.0)
        # a closed no-stop trade → excluded from the drawdown curve
        self._trade(tid=2, symbol="NOS", entry="2026-04-22", exit_date="2026-05-02",
                    exits=[("2026-05-02", 1000, 120.0, "x")], stop=None,
                    provenance=None)
        text = export.export(self.conn, book="US")
        self.assertIn("n=2", text)
        self.assertIn("Avg R", text)
        self.assertIn("drawdown curve excludes n=1", text)

    def test_legend_ships_and_carries_every_caveat(self):
        self._trade(tid=1, entry="2026-04-20", exit_date="2026-05-01",
                    exits=[("2026-05-01", 1000, 120.0, "x")])
        text = export.export(self.conn, book="US")
        for phrase in [
            "reconstructed",                     # exclude reconstructed stops
            "not_applicable",                    # not a deviation
            "could not exist",                   # null semantics
            "two books never aggregate",         # no cross-book
            "no recorded plan",                  # no intent
            "not against the full history",      # percentiles export-relative
            "absolute",                          # book_drawdown_r_at_entry is absolute
            "seq",                               # sequence caveat
            "precomputed prior-trade fields",    # by design (there are none)
            "insufficient_history is not a drawdown of zero",
            "dividend_drag_r",                   # omit-when-null + deviation nulls
            "deviation_cost_r",
            "setup selection",                   # conditional conclusions
            "anecdote",                          # n < 20
        ]:
            self.assertIn(phrase, text, msg=f"legend missing: {phrase!r}")

    def test_defaults_to_one_book(self):
        self._trade(tid=1, book="US", symbol="AAA", entry="2026-04-20",
                    exit_date="2026-05-01", exits=[("2026-05-01", 1000, 120.0, "x")])
        self._trade(tid=2, book="IDX", symbol="BREN.JK", entry="2026-04-21",
                    exit_date="2026-05-02", exits=[("2026-05-02", 1000, 120.0, "x")],
                    equity=100000.0)
        rows = self._lines(export.export(self.conn, book="US"))
        self.assertEqual({r["book"] for r in rows}, {"US"})
        self.assertNotIn("IDX", export.export(self.conn, book="US")
                         .split("# One JSON object")[1])


    # ── Scope Start bounds the export and is declared in the header (ADR 0008) ──
    def test_scope_start_withholds_earlier_trades_and_says_so(self):
        self._trade(tid=1, book="US", symbol="OLD", entry="2026-04-20",
                    exit_date="2026-05-01", exits=[("2026-05-01", 1000, 120.0, "x")])
        self._trade(tid=2, book="US", symbol="NEW", entry="2026-08-20",
                    exit_date="2026-08-25", exits=[("2026-08-25", 1000, 120.0, "x")])
        books.set_scope_start(self.conn, "US", "2026-08-18")

        text = export.export(self.conn, book="US")
        rows = self._lines(text)
        self.assertEqual([r["symbol"] for r in rows], ["NEW"])
        # Stated, never silent: a reader must know the record begins at a boundary.
        self.assertIn("US Scope Start 2026-08-18", text)
        self.assertIn("1 earlier Trade(s)", text)

    def test_no_scope_start_ships_everything_and_says_nothing(self):
        self._trade(tid=1, book="US", symbol="OLD", entry="2026-04-20",
                    exit_date="2026-05-01", exits=[("2026-05-01", 1000, 120.0, "x")])
        text = export.export(self.conn, book="US")
        self.assertEqual([r["symbol"] for r in self._lines(text)], ["OLD"])
        self.assertNotIn("Scope Start", text)

    def test_cli_export_writes_a_file_for_one_book(self):
        self._trade(tid=1, book="US", symbol="AAA", entry="2026-04-20",
                    exit_date="2026-05-01", exits=[("2026-05-01", 1000, 120.0, "x")])
        out = os.path.join(self.tmp.name, "us.jsonl")
        rc = cli.main(["export", "--book", "US", "--out", out,
                       "--db", self.conn.execute("PRAGMA database_list")
                       .fetchone()["file"]])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("field legend", text)
        self.assertIn("One JSON object per trade", text)
        rows = self._lines(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["book"], "US")


if __name__ == "__main__":
    unittest.main()
