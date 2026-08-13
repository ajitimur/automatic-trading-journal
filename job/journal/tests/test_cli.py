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

from journal import cli, db, fills, flex, flex_client, stops
from journal.cli import main

SAMPLES = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "docs", "samples"
)
FIXTURE = os.path.join(SAMPLES, "ibkr-flex-schema-fixture.xml")


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


if __name__ == "__main__":
    unittest.main()
