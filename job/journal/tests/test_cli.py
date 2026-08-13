"""The `journal run` command creates the file, exits zero, and reads as a no-op.

Also covers `journal import <flex.xml>`: it lands one Fill per execution row
and a re-drop is a visible no-op (issue #22).
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from journal import db
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


if __name__ == "__main__":
    unittest.main()
