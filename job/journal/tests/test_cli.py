"""The `journal run` command creates the file, exits zero, and reads as a no-op."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from journal.cli import main


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


if __name__ == "__main__":
    unittest.main()
