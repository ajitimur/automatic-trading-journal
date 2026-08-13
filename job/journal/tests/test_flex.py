"""The IBKR Flex parser turns execution rows into Fills (SPEC §4.1, issue #22).

Three facts are load-bearing and easy to get silently wrong, so each has a
test: commission is per-fill and reconciles to the order total when summed;
timestamps are US Eastern; the exec id splits into a logical execution
(``source_ref``) and a version (``revision``).
"""

import os
import unittest

from journal import flex

SAMPLES = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "docs", "samples"
)
FIXTURE = os.path.join(SAMPLES, "ibkr-flex-schema-fixture.xml")


def _read_fixture() -> str:
    with open(FIXTURE, encoding="utf-8") as fh:
        return fh.read()


class ParseExecIdTest(unittest.TestCase):
    def test_splits_on_the_last_dot(self):
        # <base>.<seq>: base is the logical execution, seq the version.
        base, seq = flex.parse_exec_id("0000d7a7.6a180471.01.01")
        self.assertEqual(base, "0000d7a7.6a180471.01")
        self.assertEqual(seq, 1)


class ParseTimestampTest(unittest.TestCase):
    def test_datetime_is_us_eastern_with_offset(self):
        # 20260401;093000 in April is EDT (-04:00), stored unambiguously.
        self.assertEqual(
            flex.parse_timestamp("20260401;093000"),
            "2026-04-01T09:30:00-04:00",
        )


class ParseFlexTest(unittest.TestCase):
    def setUp(self):
        self.fills = flex.parse_flex(_read_fixture())

    def test_one_fill_per_execution_row(self):
        self.assertEqual(len(self.fills), 5)

    def test_fill_fields_from_first_row(self):
        f = self.fills[0]
        self.assertEqual(f.source, "ibkr")
        self.assertEqual(f.source_ref, "0000d7a7.6a180471.01")
        self.assertEqual(f.revision, 1)
        self.assertEqual(f.symbol, "SYM1")
        self.assertEqual(f.side, "SELL")
        self.assertEqual(f.quantity, -100.0)
        self.assertEqual(f.price, 20.0)
        self.assertEqual(f.commission, -0.72907365)
        self.assertEqual(f.executed_at, "2026-04-01T09:30:00-04:00")
        self.assertEqual(f.order_id, "5237074970")

    def test_commission_sums_pro_rata_to_the_order_total(self):
        # The correction: commission is on every fill, not the first only.
        # Summing the per-fill values across an ibOrderID reconciles to the
        # broker's order total (docs/samples/ibkr-flex-findings.md).
        order = [f for f in self.fills if f.order_id == "5237074970"]
        self.assertEqual(len(order), 4)
        total = sum(f.commission for f in order)
        self.assertAlmostEqual(total, -1.815393388, places=9)


if __name__ == "__main__":
    unittest.main()
