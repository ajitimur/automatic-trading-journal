"""The daily run pulls and archives Stop Declarations from the relay (#49, #51).

The run is driven with a fake relay that serves the *shared* pull fixture — the
same file the relay's own tests assert their pull output against — so the two
sides of the wire cannot drift apart (ADR 0011). The fixture carries every form
the grammar has: a declaration, its replacement with a setup, a decline, an
undo, and a US declaration with and without the book prefix. These tests cover
what lands and what is acknowledged; test_binding covers what then commits.
"""

import json
import os
import sqlite3
import tempfile
import unittest

from journal import books, db, fills, flex
from journal.declarations import StopDeclarationStore
from journal.run import execute_run

FIXTURE = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "docs", "samples", "relay-pull-fixture.json"
)


def _fixture_items():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)["items"]


def _fixture_ids():
    return [i["id"] for i in _fixture_items()]


def _fill(ref, book, symbol, when):
    return flex.Fill(
        source="ibkr" if book == "US" else "stockbit", source_ref=ref, revision=1,
        book=book, symbol=symbol, side="BUY", quantity=10.0, price=100.0,
        commission=0.0, executed_at=when, order_id=ref,
    )


class FakeRelay:
    """Serves unacknowledged items the way the relay does, and records acks.

    ``fail_pull`` stands in for an unreachable relay; ``fail_ack`` for a run
    that dies after applying but before acknowledging; ``fail_push`` for a
    known-US push the relay refuses. ``on_ack`` lets a test look at the store at
    the moment the acknowledgement goes out.
    """

    def __init__(self, items, *, fail_pull=None, fail_ack=None, on_ack=None, fail_push=None):
        self.items = list(items)
        self.fail_pull = fail_pull
        self.fail_ack = fail_ack
        self.on_ack = on_ack
        self.fail_push = fail_push
        self.acked: list[list[str]] = []
        self.pushed: list[list[str]] = []

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

    def push_known_us_symbols(self, symbols):
        if self.fail_push is not None:
            raise self.fail_push
        self.pushed.append(list(symbols))


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

    def test_every_fixture_item_is_recorded_as_an_event_of_its_kind(self):
        execute_run(self.conn, as_of="2026-10-14", relay=FakeRelay(_fixture_items()))

        events = StopDeclarationStore(self.conn).all()
        self.assertEqual(
            [(e.relay_id, e.kind, e.book, e.symbol, e.entry_date, e.stop, e.setup, e.telegram_at)
             for e in events],
            [
                ("583201001", "declared", "IDX", "BBRI", "2026-10-13", 412, None, "2026-10-13T02:15:00Z"),
                ("583201002", "declared", "IDX", "BBRI", "2026-10-13", 4120, "high_tight_flag",
                 "2026-10-13T02:16:00Z"),
                ("583201003", "declined", "IDX", "TLKM", "2026-10-13", None, None, "2026-10-13T02:30:00Z"),
                ("583201004", "declared", "IDX", "ASII", "2026-10-13", 5000, "base_breakout",
                 "2026-10-13T03:00:00Z"),
                ("583201005", "withdrawn", "IDX", "ASII", "2026-10-13", None, None, "2026-10-13T03:01:00Z"),
                ("583201006", "declared", "US", "AMD", "2026-10-13", 182.5, None, "2026-10-13T18:00:00Z"),
                ("583201007", "declared", "US", "META", "2026-10-13", 590, "other", "2026-10-13T18:05:00Z"),
            ],
        )

    def test_events_are_keyed_by_book_symbol_and_entry_date(self):
        execute_run(self.conn, as_of="2026-10-14", relay=FakeRelay(_fixture_items()))

        store = StopDeclarationStore(self.conn)
        # The typo and its replacement are both on record, oldest said first.
        self.assertEqual([e.stop for e in store.for_key("IDX", "BBRI", "2026-10-13")], [412, 4120])
        self.assertEqual(store.for_key("US", "BBRI", "2026-10-13"), [])
        self.assertEqual(store.for_key("IDX", "BBRI", "2026-10-14"), [])

    def test_the_live_declaration_per_key_is_the_newest_and_a_withdrawn_key_has_none(self):
        execute_run(self.conn, as_of="2026-10-14", relay=FakeRelay(_fixture_items()))

        live = StopDeclarationStore(self.conn).live()
        self.assertEqual(
            {key: (e.kind, e.stop, e.setup) for key, e in live.items()},
            {
                ("IDX", "BBRI", "2026-10-13"): ("declared", 4120, "high_tight_flag"),
                ("IDX", "TLKM", "2026-10-13"): ("declined", None, None),
                ("US", "AMD", "2026-10-13"): ("declared", 182.5, None),
                ("US", "META", "2026-10-13"): ("declared", 590, "other"),
            },
        )

    def test_a_withdrawal_pulled_on_a_later_run_still_withdraws(self):
        items = _fixture_items()
        asii, undo = items[3], items[4]
        relay = FakeRelay([asii])
        execute_run(self.conn, as_of="2026-10-13", relay=relay)
        self.assertIn(("IDX", "ASII", "2026-10-13"), StopDeclarationStore(self.conn).live())

        relay.items = [undo]
        execute_run(self.conn, as_of="2026-10-14", relay=relay)
        store = StopDeclarationStore(self.conn)
        self.assertNotIn(("IDX", "ASII", "2026-10-13"), store.live())
        self.assertEqual(
            [e.kind for e in store.for_key("IDX", "ASII", "2026-10-13")],
            ["declared", "withdrawn"],
        )

    def test_each_item_is_archived_verbatim_in_both_halves_of_the_raw_tier(self):
        items = _fixture_items()
        execute_run(self.conn, as_of="2026-10-14", relay=FakeRelay(items))

        rows = self.conn.execute(
            "SELECT book, kind, content FROM raw_document WHERE kind = 'relay-item' ORDER BY id"
        ).fetchall()
        self.assertEqual([json.loads(r["content"]) for r in rows], items)
        self.assertEqual([r["book"] for r in rows], ["IDX"] * 5 + ["US"] * 2)

        files = sorted(os.listdir(os.path.join(self.archive_dir, "relay-item")))
        self.assertEqual(len(files), 7)
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

        self.assertEqual(relay.acked, [_fixture_ids()])
        self.assertEqual(seen_at_ack, [_fixture_ids()])

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
        self.assertEqual(len(StopDeclarationStore(self.conn).all()), 7)
        raw = self.conn.execute(
            "SELECT COUNT(*) c FROM raw_document WHERE kind = 'relay-item'"
        ).fetchone()["c"]
        self.assertEqual(raw, 7)
        self.assertEqual(relay.items, [])

    def test_the_run_row_states_how_many_items_were_pulled_and_archived(self):
        result = execute_run(self.conn, as_of="2026-10-14", relay=FakeRelay(_fixture_items()))

        row = self._relay_row(result.run_id)
        self.assertEqual(row["status"], "ran")
        self.assertIn("7 item(s) pulled", row["detail"])
        self.assertIn("7 archived", row["detail"])
        self.assertIn("7 declaration event(s) recorded", row["detail"])

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
        self.assertEqual(relay.pushed, [])

    def test_with_no_relay_configured_the_pass_says_it_was_skipped(self):
        result = execute_run(self.conn, as_of="2026-10-14")

        row = self._relay_row(result.run_id)
        self.assertEqual(row["status"], "ran")
        self.assertIn("skipped — no relay configured", row["detail"])
        self.assertIn("JOURNAL_RELAY_URL", row["detail"])

    def test_an_item_of_a_kind_this_job_predates_is_archived_and_acknowledged_but_not_recorded(self):
        # A later grammar (a forwarded PDF, say) reaching an older job: it is
        # kept in the raw tier, so nothing is lost by letting the relay drop it.
        other = {
            "id": "583201099",
            "telegram_at": "2026-10-13T19:00:00Z",
            "text": "",
            "parse": {"kind": "document"},
        }
        relay = FakeRelay([*_fixture_items(), other])
        result = execute_run(self.conn, as_of="2026-10-14", relay=relay)

        self.assertEqual(len(StopDeclarationStore(self.conn).all()), 7)
        self.assertEqual(relay.acked, [[*_fixture_ids(), "583201099"]])
        detail = self._relay_row(result.run_id)["detail"]
        self.assertIn("8 item(s) pulled", detail)
        self.assertIn("8 archived", detail)
        self.assertIn("7 declaration event(s) recorded", detail)

    def test_a_malformed_item_of_any_known_kind_is_archived_but_left_at_the_relay(self):
        # The wire contract has drifted: say so, and keep the item where a fixed
        # job can still pull it, rather than acknowledging a Stop away.
        key = {"book": "IDX", "symbol": "BBCA", "entry_date": "2026-10-13"}
        broken = [
            {"id": "b1", "telegram_at": "2026-10-13T04:00:00Z", "text": "BBCA 9000",
             "parse": {"kind": "declaration", **key}},
            {"id": "b2", "telegram_at": "2026-10-13T04:01:00Z", "text": "BBCA 9000 vcp",
             "parse": {"kind": "declaration", **key, "stop": 9000, "setup": "vcp"}},
            {"id": "b3", "telegram_at": "2026-10-13T04:02:00Z", "text": "no sl",
             "parse": {"kind": "decline", "book": "IDX", "entry_date": "2026-10-13"}},
            {"id": "b4", "telegram_at": "2026-10-13T04:03:00Z", "text": "/undo",
             "parse": {"kind": "undo"}},
        ]
        relay = FakeRelay([*_fixture_items(), *broken])
        result = execute_run(self.conn, as_of="2026-10-14", relay=relay)

        row = self._relay_row(result.run_id)
        self.assertEqual(row["status"], "error")
        self.assertIn("4 item(s) malformed", row["detail"])
        for relay_id in ("b1", "b2", "b3", "b4"):
            self.assertIn(relay_id, row["detail"])
        self.assertIn("11 archived", row["detail"])
        self.assertEqual(relay.acked, [_fixture_ids()])
        self.assertEqual([i["id"] for i in relay.items], ["b1", "b2", "b3", "b4"])
        self.assertEqual(len(StopDeclarationStore(self.conn).all()), 7)

    def test_an_item_with_no_id_does_not_hold_back_the_others(self):
        # It can never be acknowledged, so it is reported; the good items still
        # land and are still acknowledged.
        anonymous = {"telegram_at": "2026-10-13T05:00:00Z", "text": "BBCA 9000", "parse": {}}
        relay = FakeRelay([anonymous, *_fixture_items()])
        result = execute_run(self.conn, as_of="2026-10-14", relay=relay)

        row = self._relay_row(result.run_id)
        self.assertEqual(row["status"], "error")
        self.assertIn("1 item(s) without an id", row["detail"])
        self.assertEqual(relay.acked, [_fixture_ids()])
        self.assertEqual(len(StopDeclarationStore(self.conn).all()), 7)

    def test_each_run_pushes_the_symbols_the_journal_knows_on_the_us_book(self):
        fills.insert_fills(self.conn, [
            _fill("u1", "US", "META", "2026-10-01T10:00:00-04:00"),
            _fill("u2", "US", "AAPL", "2026-10-02T10:00:00-04:00"),
            _fill("u3", "US", "META", "2026-10-03T10:00:00-04:00"),
            _fill("i1", "IDX", "BBRI", "2026-10-01T09:20:00+07:00"),
        ])
        self.conn.commit()
        relay = FakeRelay([])
        execute_run(self.conn, as_of="2026-10-14", relay=relay)

        self.assertEqual(relay.pushed, [["AAPL", "META"]])

    def test_the_push_goes_out_even_with_nothing_to_pull(self):
        relay = FakeRelay([])
        execute_run(self.conn, as_of="2026-10-14", relay=relay)
        self.assertEqual(relay.pushed, [[]])

    def test_a_failed_push_is_an_error_but_the_items_are_still_recorded_and_acknowledged(self):
        relay = FakeRelay(_fixture_items(), fail_push=OSError("push refused"))
        result = execute_run(self.conn, as_of="2026-10-14", relay=relay)

        row = self._relay_row(result.run_id)
        self.assertEqual(row["status"], "error")
        self.assertIn("push refused", row["detail"])
        self.assertIn("7 declaration event(s) recorded", row["detail"])
        self.assertEqual(relay.acked, [_fixture_ids()])
        self.assertEqual(len(StopDeclarationStore(self.conn).all()), 7)


class EventTableMigrationTest(unittest.TestCase):
    """A journal file from #49/#50 opens with its declarations and bindings intact."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "journal.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_v12_file(self):
        old = sqlite3.connect(self.db_path)
        old.executescript("""
            CREATE TABLE raw_document (
                id INTEGER PRIMARY KEY AUTOINCREMENT, book TEXT NOT NULL,
                kind TEXT NOT NULL, fetched_at TEXT NOT NULL, content TEXT NOT NULL);
            CREATE TABLE trade (
                id INTEGER PRIMARY KEY, book TEXT NOT NULL, symbol TEXT NOT NULL,
                entry_date TEXT NOT NULL, UNIQUE (book, symbol, entry_date));
            CREATE TABLE stop_declaration_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT, relay_id TEXT NOT NULL UNIQUE,
                book TEXT NOT NULL, symbol TEXT NOT NULL, entry_date TEXT NOT NULL,
                stop REAL NOT NULL, telegram_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
                raw_ref INTEGER NOT NULL REFERENCES raw_document(id));
            CREATE TABLE stop_declaration_binding (
                relay_id TEXT PRIMARY KEY REFERENCES stop_declaration_event(relay_id),
                trade_id INTEGER NOT NULL REFERENCES trade(id), bound_at TEXT NOT NULL);
            INSERT INTO raw_document VALUES (1, 'IDX', 'relay-item', 't', '{}');
            INSERT INTO trade VALUES (1, 'IDX', 'BBRI', '2026-10-13');
            INSERT INTO stop_declaration_event VALUES
                (1, '583201001', 'IDX', 'BBRI', '2026-10-13', 4120,
                 '2026-10-13T02:15:00Z', '2026-10-13T12:00:00Z', 1);
            INSERT INTO stop_declaration_binding VALUES ('583201001', 1, '2026-10-13T12:00:00Z');
        """)
        old.commit()
        old.close()

    def test_old_events_become_declared_and_their_bindings_survive(self):
        self._write_v12_file()
        conn = db.connect(self.db_path)
        try:
            [event] = StopDeclarationStore(conn).all()
            self.assertEqual((event.relay_id, event.kind, event.stop, event.setup),
                             ("583201001", "declared", 4120, None))
            self.assertEqual(
                [tuple(r) for r in conn.execute("SELECT relay_id, trade_id FROM stop_declaration_binding")],
                [("583201001", 1)],
            )
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertIn(
                "stop_declaration_event_key",
                [r["name"] for r in conn.execute("PRAGMA index_list(stop_declaration_event)")],
            )
            # The migrated table takes the new kinds.
            conn.execute(
                "INSERT INTO stop_declaration_event (relay_id, kind, book, symbol, entry_date, "
                "telegram_at, recorded_at, raw_ref) VALUES "
                "('2', 'declined', 'IDX', 'TLKM', '2026-10-13', 't', 't', 1)"
            )
        finally:
            conn.close()

    def test_opening_a_migrated_file_again_changes_nothing(self):
        self._write_v12_file()
        db.connect(self.db_path).close()
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(len(StopDeclarationStore(conn).all()), 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
