"""The daily run pulls and archives Stop Declarations from the relay (#49).

The run is driven with a fake relay that serves the *shared* pull fixture — the
same file the relay's own tests assert their pull output against — so the two
sides of the wire cannot drift apart (ADR 0011). Nothing binds or commits yet:
this slice makes declarations land in the journal durably, and acknowledges
them only once they have.
"""

import json
import os
import tempfile
import unittest

from journal import books, db
from journal.declarations import StopDeclarationStore
from journal.run import execute_run

FIXTURE = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "docs", "samples", "relay-pull-fixture.json"
)


def _fixture_items():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)["items"]


class FakeRelay:
    """Serves unacknowledged items the way the relay does, and records acks.

    ``fail_pull`` stands in for an unreachable relay; ``fail_ack`` for a run
    that dies after applying but before acknowledging. ``on_ack`` lets a test
    look at the store at the moment the acknowledgement goes out.
    """

    def __init__(self, items, *, fail_pull=None, fail_ack=None, on_ack=None):
        self.items = list(items)
        self.fail_pull = fail_pull
        self.fail_ack = fail_ack
        self.on_ack = on_ack
        self.acked: list[list[str]] = []

    def pull(self):
        if self.fail_pull is not None:
            raise self.fail_pull
        return [dict(i) for i in self.items]

    def acknowledge(self, ids):
        if self.on_ack is not None:
            self.on_ack(list(ids))
        if self.fail_ack is not None:
            raise self.fail_ack
        self.acked.append(list(ids))
        self.items = [i for i in self.items if i.get("id") not in set(ids)]


class RelayIntakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "journal.db")
        self.archive_dir = os.path.join(self.tmp.name, "archive")
        self._saved_archive = os.environ.pop("JOURNAL_ARCHIVE_DIR", None)
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        if self._saved_archive is not None:
            os.environ["JOURNAL_ARCHIVE_DIR"] = self._saved_archive
        self.tmp.cleanup()

    def _relay_pass(self, result):
        [p] = [p for p in result.passes if p.name == "relay"]
        return p

    def _relay_row(self, run_id):
        return self.conn.execute(
            "SELECT * FROM run_pass WHERE run_id = ? AND name = 'relay'", (run_id,)
        ).fetchone()

    def test_the_fixture_declarations_are_recorded_as_events(self):
        execute_run(self.conn, as_of="2026-10-14", relay=FakeRelay(_fixture_items()))

        events = StopDeclarationStore(self.conn).all()
        self.assertEqual(
            [(e.relay_id, e.book, e.symbol, e.entry_date, e.stop, e.telegram_at) for e in events],
            [
                ("583201001", "IDX", "BBRI", "2026-10-13", 4120, "2026-10-13T02:15:00Z"),
                ("583201002", "US", "AMD", "2026-10-13", 182.5, "2026-10-13T18:00:00Z"),
            ],
        )

    def test_events_are_keyed_by_book_symbol_and_entry_date(self):
        execute_run(self.conn, as_of="2026-10-14", relay=FakeRelay(_fixture_items()))

        store = StopDeclarationStore(self.conn)
        [bbri] = store.for_key("IDX", "BBRI", "2026-10-13")
        self.assertEqual(bbri.stop, 4120)
        self.assertEqual(store.for_key("US", "BBRI", "2026-10-13"), [])
        self.assertEqual(store.for_key("IDX", "BBRI", "2026-10-14"), [])

    def test_each_item_is_archived_verbatim_in_both_halves_of_the_raw_tier(self):
        items = _fixture_items()
        execute_run(self.conn, as_of="2026-10-14", relay=FakeRelay(items))

        rows = self.conn.execute(
            "SELECT book, kind, content FROM raw_document WHERE kind = 'relay-item' ORDER BY id"
        ).fetchall()
        self.assertEqual([json.loads(r["content"]) for r in rows], items)
        self.assertEqual([r["book"] for r in rows], ["IDX", "US"])

        files = sorted(os.listdir(os.path.join(self.archive_dir, "relay-item")))
        self.assertEqual(len(files), 2)
        archived = []
        for name in files:
            with open(os.path.join(self.archive_dir, "relay-item", name), encoding="utf-8") as fh:
                archived.append(json.load(fh))
        self.assertCountEqual(archived, items)

    def test_each_event_points_at_its_archived_item(self):
        execute_run(self.conn, as_of="2026-10-14", relay=FakeRelay(_fixture_items()))

        for event in StopDeclarationStore(self.conn).all():
            content = self.conn.execute(
                "SELECT content FROM raw_document WHERE id = ?", (event.raw_ref,)
            ).fetchone()["content"]
            self.assertEqual(json.loads(content)["id"], event.relay_id)

    def test_items_are_acknowledged_only_after_they_are_applied(self):
        seen_at_ack = []

        def on_ack(ids):
            # Read through a second connection: what is durably committed, not
            # what the run's own transaction can see.
            other = db.connect(self.db_path)
            try:
                seen_at_ack.append(
                    sorted(r["relay_id"] for r in other.execute(
                        "SELECT relay_id FROM stop_declaration_event"
                    ))
                )
            finally:
                other.close()

        relay = FakeRelay(_fixture_items(), on_ack=on_ack)
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        self.assertEqual(relay.acked, [["583201001", "583201002"]])
        self.assertEqual(seen_at_ack, [["583201001", "583201002"]])

    def test_a_run_that_fails_to_acknowledge_is_safe_to_repeat(self):
        items = _fixture_items()
        relay = FakeRelay(items, fail_ack=OSError("connection reset"))
        first = execute_run(self.conn, as_of="2026-10-14", relay=relay)
        self.assertEqual(self._relay_pass(first).status, "error")
        self.assertIn("connection reset", self._relay_pass(first).detail)

        # The relay still holds everything, so the next run re-pulls the same
        # items. Applying them again adds nothing.
        relay.fail_ack = None
        second = execute_run(self.conn, as_of="2026-10-15", relay=relay)

        self.assertEqual(self._relay_pass(second).status, "ran")
        self.assertEqual(len(StopDeclarationStore(self.conn).all()), 2)
        raw = self.conn.execute(
            "SELECT COUNT(*) c FROM raw_document WHERE kind = 'relay-item'"
        ).fetchone()["c"]
        self.assertEqual(raw, 2)
        self.assertEqual(relay.items, [])

    def test_the_run_row_states_how_many_items_were_pulled_and_archived(self):
        result = execute_run(self.conn, as_of="2026-10-14", relay=FakeRelay(_fixture_items()))

        row = self._relay_row(result.run_id)
        self.assertEqual(row["status"], "ran")
        self.assertIn("2 item(s) pulled", row["detail"])
        self.assertIn("2 archived", row["detail"])
        self.assertIn("2 declaration(s) recorded", row["detail"])

    def test_an_empty_relay_pulls_nothing_and_acknowledges_nothing(self):
        relay = FakeRelay([])
        result = execute_run(self.conn, as_of="2026-10-14", relay=relay)

        self.assertEqual(self._relay_row(result.run_id)["status"], "ran")
        self.assertIn("0 item(s) pulled", self._relay_row(result.run_id)["detail"])
        self.assertEqual(relay.acked, [])

    def test_an_unreachable_relay_is_an_error_and_every_other_pass_still_runs(self):
        relay = FakeRelay(_fixture_items(), fail_pull=OSError("relay unreachable"))
        result = execute_run(self.conn, as_of="2026-10-14", relay=relay)

        row = self._relay_row(result.run_id)
        self.assertEqual(row["status"], "error")
        self.assertIn("relay unreachable", row["detail"])

        others = [p for p in result.passes if p.name != "relay"]
        # Five passes per book, plus the single bind pass after the books.
        self.assertEqual(len(others), len(books.BOOKS) * 5 + 1)
        self.assertTrue(all(p.status != "error" for p in others))
        self.assertEqual(StopDeclarationStore(self.conn).all(), [])
        self.assertEqual(relay.acked, [])

    def test_with_no_relay_configured_the_pass_says_it_was_skipped(self):
        result = execute_run(self.conn, as_of="2026-10-14")

        row = self._relay_row(result.run_id)
        self.assertEqual(row["status"], "ran")
        self.assertIn("skipped — no relay configured", row["detail"])
        self.assertIn("JOURNAL_RELAY_URL", row["detail"])

    def test_an_item_that_is_not_a_declaration_is_archived_and_acknowledged_but_not_recorded(self):
        # A later grammar (`/undo`, a forwarded PDF) reaching an older job: it is
        # kept in the raw tier, so nothing is lost by letting the relay drop it.
        other = {
            "id": "583201003",
            "telegram_at": "2026-10-13T03:00:00Z",
            "text": "/undo",
            "parse": {"kind": "undo"},
        }
        relay = FakeRelay([*_fixture_items(), other])
        result = execute_run(self.conn, as_of="2026-10-14", relay=relay)

        self.assertEqual(len(StopDeclarationStore(self.conn).all()), 2)
        self.assertEqual(relay.acked, [["583201001", "583201002", "583201003"]])
        detail = self._relay_row(result.run_id)["detail"]
        self.assertIn("3 item(s) pulled", detail)
        self.assertIn("3 archived", detail)
        self.assertIn("2 declaration(s) recorded", detail)

    def test_a_malformed_declaration_is_archived_but_left_at_the_relay(self):
        # The wire contract has drifted: say so, and keep the item where a fixed
        # job can still pull it, rather than acknowledging a Stop away.
        broken = {
            "id": "583201004",
            "telegram_at": "2026-10-13T04:00:00Z",
            "text": "BBCA 9000",
            "parse": {"kind": "declaration", "book": "IDX", "symbol": "BBCA", "entry_date": "2026-10-13"},
        }
        relay = FakeRelay([*_fixture_items(), broken])
        result = execute_run(self.conn, as_of="2026-10-14", relay=relay)

        row = self._relay_row(result.run_id)
        self.assertEqual(row["status"], "error")
        self.assertIn("1 declaration(s) malformed", row["detail"])
        self.assertIn("583201004", row["detail"])
        self.assertIn("3 archived", row["detail"])
        self.assertEqual(relay.acked, [["583201001", "583201002"]])
        self.assertEqual([i["id"] for i in relay.items], ["583201004"])
        self.assertEqual(len(StopDeclarationStore(self.conn).all()), 2)

    def test_an_item_with_no_id_does_not_hold_back_the_others(self):
        # It can never be acknowledged, so it is reported; the good items still
        # land and are still acknowledged.
        anonymous = {"telegram_at": "2026-10-13T05:00:00Z", "text": "BBCA 9000", "parse": {}}
        relay = FakeRelay([anonymous, *_fixture_items()])
        result = execute_run(self.conn, as_of="2026-10-14", relay=relay)

        row = self._relay_row(result.run_id)
        self.assertEqual(row["status"], "error")
        self.assertIn("1 item(s) without an id", row["detail"])
        self.assertEqual(relay.acked, [["583201001", "583201002"]])
        self.assertEqual(len(StopDeclarationStore(self.conn).all()), 2)


if __name__ == "__main__":
    unittest.main()
