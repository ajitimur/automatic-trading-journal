"""The `journal run` command creates the file, exits zero, and reads as a no-op.

Also covers `journal import <flex.xml>`: it lands one Fill per execution row
and a re-drop is a visible no-op (issue #22).
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from journal import cli, db, equity, fills, flex, flex_client, stops
from journal.cli import main

SAMPLES = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "docs", "samples"
)
FIXTURE = os.path.join(SAMPLES, "ibkr-flex-schema-fixture.xml")
TC_FIXTURE = os.path.join(SAMPLES, "stockbit-tc-fixture.txt")
TC_SHIFTED = os.path.join(SAMPLES, "stockbit-tc-column-shift-fixture.txt")
NAV_FIXTURE = os.path.join(SAMPLES, "ibkr-nav-flex-schema-fixture.xml")


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "journal.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["run", "--db", self.db_path, "--as-of", "2026-08-13"])
        return code, buf.getvalue()

    def _archive_files(self):
        archive = os.path.join(self.tmp.name, "archive")
        found = []
        for root, _dirs, files in os.walk(archive):
            for f in files:
                found.append(os.path.join(root, f))
        return found

    def test_import_archives_the_raw_flex_xml(self):
        # The Flex XML joins the keep-forever raw tier and never the repo
        # (SPEC §13.5, #39).
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["import", FIXTURE, "--db", self.db_path])
        archived = self._archive_files()
        self.assertEqual(len(archived), 1)
        self.assertIn("flex-trades-xml", archived[0])
        with open(FIXTURE, "rb") as a, open(archived[0], "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_quarantined_tc_is_still_archived(self):
        # A shifted column quarantines with zero fills — but the raw document is
        # kept so a parser fix can be re-run over it (SPEC §13.5).
        buf = io.StringIO()
        with redirect_stderr(buf):
            code = main(["drop", TC_SHIFTED, "--db", self.db_path])
        self.assertEqual(code, 1)  # quarantined
        archived = self._archive_files()
        self.assertEqual(len(archived), 1)
        self.assertIn("stockbit-tc", archived[0])

    def test_restore_check_verifies_a_snapshot_from_a_run(self):
        # A run leaves a snapshot; restore-check restores it and reports OK.
        self._run()
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["restore-check", "--db", self.db_path])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("VERIFIED", out)
        self.assertIn("integrity_check: ok", out)

    def test_run_exits_zero_and_creates_file(self):
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.db_path))
        self.assertIn("status: ok", out)
        self.assertIn("advanced", out)

    def test_second_run_is_visibly_a_noop(self):
        self._run()
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIn("status: no-op", out)
        self.assertIn("no-op (already at 2026-08-13)", out)

    def _import(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["import", FIXTURE, "--db", self.db_path])
        return code, buf.getvalue()

    def test_import_lands_fills_and_re_drop_is_a_noop(self):
        code, out = self._import()
        self.assertEqual(code, 0)
        self.assertIn("5", out)

        conn = db.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) AS n FROM fill").fetchone()["n"]
        conn.close()
        self.assertEqual(count, 5)

        code, out = self._import()
        self.assertEqual(code, 0)
        self.assertIn("0", out)

    def _drop(self, path):
        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = main(["drop", path, "--db", self.db_path])
        return code, buf.getvalue(), err.getvalue()

    def test_drop_lands_a_tc_and_re_drop_is_a_noop(self):
        code, out, _ = self._drop(TC_FIXTURE)
        self.assertEqual(code, 0)
        self.assertIn("9", out)

        conn = db.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) AS n FROM fill").fetchone()["n"]
        conn.close()
        self.assertEqual(count, 9)

        code, out, _ = self._drop(TC_FIXTURE)
        self.assertEqual(code, 0)
        self.assertIn("0", out)

    def test_drop_quarantines_a_column_shift_and_lands_zero_fills(self):
        code, _out, err = self._drop(TC_SHIFTED)
        self.assertEqual(code, 1)
        self.assertIn("quarantined", err)

        conn = db.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) AS n FROM fill").fetchone()["n"]
        conn.close()
        self.assertEqual(count, 0)

    def _seed_buy(self):
        conn = db.connect(self.db_path)
        fills.insert_fills(conn, [
            flex.Fill(
                source="ibkr", source_ref="b1", revision=1, book="US",
                symbol="AAA", side="BUY", quantity=100.0, price=10.0,
                commission=0.0, executed_at="2026-08-03T09:30:00-04:00", order_id="o1",
            )
        ])
        conn.close()

    def _confirm(self, *extra):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["confirm", "--db", self.db_path, *extra])
        return code, buf.getvalue()

    def test_confirm_dry_run_commits_nothing_then_confirm_lands_the_trade(self):
        self._seed_buy()

        code, out = self._confirm("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("nothing committed", out)
        self.assertIn("new-trade", out)

        conn = db.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM trade").fetchone()["n"], 0)
        conn.close()

        code, out = self._confirm()
        self.assertEqual(code, 0)
        self.assertIn("1 new Trade", out)

        conn = db.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM trade").fetchone()["n"], 1)
        conn.close()

    def test_bulk_confirm_commits_exits_only(self):
        self._seed_buy()
        self._confirm()  # land the AAA Trade
        conn = db.connect(self.db_path)
        fills.insert_fills(conn, [
            flex.Fill(
                source="ibkr", source_ref="s1", revision=1, book="US",
                symbol="AAA", side="SELL", quantity=-100.0, price=12.0,
                commission=0.0, executed_at="2026-08-07T10:00:00-04:00", order_id="o2",
            )
        ])
        conn.close()

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["bulk-confirm", "--db", self.db_path])
        self.assertEqual(code, 0)
        self.assertIn("bulk-confirmed 1 exit", buf.getvalue())

        conn = db.connect(self.db_path)
        row = conn.execute("SELECT reason FROM trade_exit").fetchone()
        conn.close()
        self.assertEqual(row["reason"], "close_below_ma10")  # the proposed default

    def test_remember_symbol_repairs_a_committed_trade(self):
        conn = db.connect(self.db_path)
        fills.insert_fills(conn, [
            flex.Fill(
                source="stockbit", source_ref="w1", revision=1, book="IDX",
                symbol="WRNG", side="BUY", quantity=100.0, price=10.0,
                commission=0.0, executed_at="2026-08-03T09:30:00-04:00", order_id="o1",
            )
        ])
        conn.close()
        self._confirm()

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["remember-symbol", "stockbit", "WRNG", "RIGHT", "--db", self.db_path])
        self.assertEqual(code, 0)
        self.assertIn("1 committed Trade(s) repaired", buf.getvalue())

        conn = db.connect(self.db_path)
        symbols = [r["symbol"] for r in conn.execute("SELECT symbol FROM trade")]
        conn.close()
        self.assertEqual(symbols, ["RIGHT"])

    def _trade_id(self):
        conn = db.connect(self.db_path)
        tid = conn.execute("SELECT id FROM trade").fetchone()["id"]
        conn.close()
        return tid

    def test_stop_and_setup_commands_set_the_hand_entered_fields(self):
        self._seed_buy()
        self._confirm()
        tid = self._trade_id()

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["stop", str(tid), "9.0", "--db", self.db_path])
        self.assertEqual(code, 0)
        self.assertIn("provenance: recorded", buf.getvalue())  # no Exit yet

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["setup", str(tid), "base_breakout", "--db", self.db_path])
        self.assertEqual(code, 0)

        conn = db.connect(self.db_path)
        row = conn.execute(
            "SELECT stop, setup, stop_provenance FROM trade WHERE id = ?", (tid,)
        ).fetchone()
        conn.close()
        self.assertEqual((row["stop"], row["setup"], row["stop_provenance"]),
                         (9.0, "base_breakout", "recorded"))

    def test_stop_after_freeze_exits_nonzero(self):
        self._seed_buy()
        self._confirm()
        tid = self._trade_id()
        conn = db.connect(self.db_path)
        stops.freeze(conn, tid)
        conn.close()

        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["stop", str(tid), "9.0", "--db", self.db_path])
        self.assertEqual(code, 1)
        self.assertIn("frozen", err.getvalue())

    def _fetch(self, client):
        # The real DoH/HTTP client is replaced with a fake — the wire is not
        # touched, only the wiring from fetched XML into the ledger.
        buf, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_build_flex_client", return_value=client):
            with redirect_stdout(buf), redirect_stderr(err):
                code = main(["fetch", "QUERYID", "--db", self.db_path])
        return code, buf.getvalue(), err.getvalue()

    def test_fetch_lands_fetched_fills(self):
        with open(FIXTURE, encoding="utf-8") as fh:
            statement = fh.read()
        client = mock.Mock()
        client.fetch_statement.return_value = statement

        code, out, _ = self._fetch(client)
        self.assertEqual(code, 0)
        client.fetch_statement.assert_called_once_with("QUERYID")
        self.assertIn("5", out)

        conn = db.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) AS n FROM fill").fetchone()["n"]
        conn.close()
        self.assertEqual(count, 5)

    def test_fetch_surfaces_interception_and_exits_nonzero(self):
        client = mock.Mock()
        client.fetch_statement.side_effect = flex_client.InterceptionError("mismatch")
        code, _out, err = self._fetch(client)
        self.assertEqual(code, 1)
        self.assertIn("mismatch", err)

    # --- EquitySnapshot: NAV capture and IDX hand entry (issue #31) ---

    def _fetch_nav(self, client):
        buf, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_build_flex_client", return_value=client):
            with redirect_stdout(buf), redirect_stderr(err):
                code = main(["fetch-nav", "NAVQUERY", "--db", self.db_path])
        return code, buf.getvalue(), err.getvalue()

    def test_fetch_nav_captures_snapshots_from_a_second_query(self):
        with open(NAV_FIXTURE, encoding="utf-8") as fh:
            statement = fh.read()
        client = mock.Mock()
        client.fetch_statement.return_value = statement

        code, out, _ = self._fetch_nav(client)
        self.assertEqual(code, 0)
        client.fetch_statement.assert_called_once_with("NAVQUERY")
        self.assertIn("3", out)

        conn = db.connect(self.db_path)
        snaps = conn.execute("SELECT COUNT(*) AS n FROM equity_snapshot WHERE book='US'").fetchone()["n"]
        raw = conn.execute("SELECT COUNT(*) AS n FROM raw_document WHERE kind='nav-flex-xml'").fetchone()["n"]
        conn.close()
        self.assertEqual(snaps, 3)
        self.assertEqual(raw, 1)

    def test_import_nav_captures_a_file(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["import-nav", NAV_FIXTURE, "--db", self.db_path])
        self.assertEqual(code, 0)
        self.assertIn("3", buf.getvalue())

    def test_equity_idx_single_entry_writes_straight_through(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main([
                "equity-idx", "--db", self.db_path,
                "--date", "2026-07-31", "--portfolio", "800", "--ledger-balance", "200",
            ])
        self.assertEqual(code, 0)
        conn = db.connect(self.db_path)
        row = conn.execute(
            "SELECT equity FROM equity_snapshot WHERE book='IDX' AND date='2026-07-31'"
        ).fetchone()
        conn.close()
        self.assertEqual(row["equity"], 1000.0)

    def test_equity_idx_month_end_series_from_csv_in_one_sitting(self):
        csv_path = os.path.join(self.tmp.name, "series.csv")
        with open(csv_path, "w", encoding="utf-8") as fh:
            fh.write("date,portfolio,ledger_balance,cash_investor,provenance\n")
            fh.write("2026-07-31,800,200,50,stated\n")
            fh.write("2026-06-30,780,190,,stated\n")
            fh.write("2026-05-31,770,180,,estimated\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["equity-idx", "--file", csv_path, "--db", self.db_path])
        self.assertEqual(code, 0)
        self.assertIn("3", buf.getvalue())
        conn = db.connect(self.db_path)
        n = conn.execute("SELECT COUNT(*) AS n FROM equity_snapshot WHERE book='IDX'").fetchone()["n"]
        conn.close()
        self.assertEqual(n, 3)


    def test_risk_reports_percentages_bound_and_excluded_counts(self):
        conn = db.connect(self.db_path)
        # A fresh stated IDX snapshot and a Trade against it.
        equity.record_idx_snapshot(conn, date="2026-08-15", portfolio=900.0, ledger_balance=100.0)
        conn.execute(
            "INSERT INTO trade (book, symbol, entry_date, entry_qty, entry_avg_price, status, stop) "
            "VALUES ('IDX', 'BBRI', '2026-08-20', 10, 5.0, 'open', 4.0)"
        )
        # A stale one — entered 2026-07-25, whose nearest prior snapshot is the
        # June one, 54 days back and past the 45-day IDX bound.
        equity.record_idx_snapshot(conn, date="2026-06-01", portfolio=900.0, ledger_balance=100.0)
        conn.execute(
            "INSERT INTO trade (book, symbol, entry_date, entry_qty, entry_avg_price, status, stop) "
            "VALUES ('IDX', 'TLKM', '2026-07-25', 10, 5.0, 'open', 4.0)"
        )
        conn.commit()
        conn.close()

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["risk", "--book", "IDX", "--db", self.db_path])
        out = buf.getvalue()
        self.assertEqual(code, 0)
        # equity 1000: risk (5-4)*10/1000*100 = 1.0%, exposure 5*10/1000*100 = 5.0%.
        self.assertIn("risk 1.000%", out)
        self.assertIn("exposure 5.000%", out)
        self.assertIn("insufficient_history", out)    # the stale Trade's marker
        self.assertIn("! IDX equity", out)            # the banner line
        self.assertIn("1 included", out)
        self.assertIn("1 stale", out)


    def test_counterfactual_scores_closed_trades_and_stores_them(self):
        from datetime import date, timedelta

        conn = db.connect(self.db_path)
        # A closed IDX Trade with a recorded stop, and its symbol's bar series so
        # the engine can seat the trail MA and simulate all six variants.
        d0 = date.fromisoformat("2026-07-01")
        closes = [100, 101, 102, 103, 104, 105] + [106] * 20
        rows = []
        for i, c in enumerate(closes):
            day = (d0 + timedelta(days=i)).isoformat()
            rows.append(("IDX", "BBRI", day, c, c * 1.01, c * 0.99, c, 1000, 0.0))
        conn.executemany(
            "INSERT INTO bar (book, symbol, date, open, high, low, close, volume, "
            "dividend) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        cur = conn.execute(
            "INSERT INTO trade (book, symbol, entry_date, entry_qty, entry_avg_price, "
            "status, stop, stop_provenance) "
            "VALUES ('IDX', 'BBRI', ?, 30, 100.0, 'closed', 90.0, 'recorded')",
            (rows[0][2],))
        tid = cur.lastrowid
        conn.execute(
            "INSERT INTO trade_exit (trade_id, source, source_ref, exit_date, "
            "quantity, price, reason) VALUES (?, 'ibkr', 'r1', ?, 30, ?, "
            "'close_below_ma10')", (tid, rows[5][2], closes[5]))
        conn.commit()
        conn.close()

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["counterfactual", "--book", "IDX", "--db", self.db_path])
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("1 closed Trade(s)", out)
        self.assertIn(f"Trade {tid} BBRI", out)

        # The Trade-level row and its six variant rows are persisted.
        conn = db.connect(self.db_path)
        one = conn.execute(
            "SELECT COUNT(*) AS n FROM trade_counterfactual WHERE trade_id=?",
            (tid,)).fetchone()["n"]
        six = conn.execute(
            "SELECT COUNT(*) AS n FROM counterfactual_variant WHERE trade_id=?",
            (tid,)).fetchone()["n"]
        conn.close()
        self.assertEqual(one, 1)
        self.assertEqual(six, 6)


if __name__ == "__main__":
    unittest.main()
