"""The ``journal`` CLI — a plain idempotent command (SPEC §13.7 seam 1).

``launchd`` merely calls this; any scheduler on any host substitutes without
touching the job. Nothing macOS-specific sits in this path.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import Optional, Sequence

from . import backup, db, equity, fills, flex, flex_client, secrets, stockbit, stops, trades
from .run import RunResult, execute_run


def _archive_source_file(db_path: str, *, book: str, kind: str, path: str) -> None:
    """Copy a dropped/imported source file into the keep-forever raw archive.

    The raw tier is what makes the DB reconstructible and lets a parser fix be
    re-run over history (SPEC §13.5), so a document is archived *before* it is
    parsed — even one that then quarantines or errors is kept. The archive dir
    never enters the repo; the document is stored content-addressed and verbatim.
    """
    with open(path, "rb") as fh:
        content = fh.read()
    ext = path.rsplit(".", 1)[-1].lower() if "." in os.path.basename(path) else "bin"
    backup.archive_raw(
        backup.archive_dir_for(db_path), book=book, kind=kind, content=content, ext=ext
    )


def _format_summary(result: RunResult, db_path: str) -> str:
    lines = [
        f"run #{result.run_id}  as-of {result.as_of_date}  status: {result.status}",
        f"store: {db_path}",
    ]
    for book in result.books:
        if book.status == "advanced":
            detail = f"advanced {book.days_advanced} day(s) ({book.from_date or 'floor'} -> {book.to_date})"
        elif book.status == "no-op":
            detail = f"no-op (already at {book.to_date})"
        else:
            detail = f"error: {book.error}"
        lines.append(f"  {book.book:<3} {detail}")
    if result.snapshot is not None:
        off = f" (+ off-site {result.snapshot.offsite_path})" if result.snapshot.offsite_path else ""
        lines.append(f"snapshot: {result.snapshot.path}{off}")
    elif result.snapshot_error is not None:
        lines.append(f"snapshot: skipped — {result.snapshot_error}")
    return "\n".join(lines)


def cmd_run(args: argparse.Namespace) -> int:
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        result = execute_run(conn, as_of=args.as_of)
    finally:
        conn.close()
    print(_format_summary(result, db_path))
    # Errors are recorded in the run record, not surfaced as a non-zero exit
    # (SPEC §13.6): the job always exits zero so a missed book reads as data.
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        _archive_source_file(db_path, book="US", kind="flex-trades-xml", path=args.file)
        inserted = fills.import_flex_file(conn, args.file)
        total = conn.execute("SELECT COUNT(*) AS n FROM fill").fetchone()["n"]
    except flex.FlexError as exc:
        # A Flex error body is an error, never an empty statement (SPEC §4.1).
        # Surface it and exit non-zero — unlike `run`, an import is an explicit
        # operator action, so a bad file should not read as success.
        print(f"import failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"imported {inserted} new fill(s) from {args.file}  ({total} in ledger)")
    return 0


def cmd_drop(args: argparse.Namespace) -> int:
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        # Archive the TC verbatim first (SPEC §13.5): a quarantined document is
        # exactly what a later parser fix needs to be re-run over, so it is kept
        # even though nothing lands in the ledger.
        _archive_source_file(db_path, book="IDX", kind="stockbit-tc", path=args.file)
        inserted = fills.import_stockbit_file(conn, args.file)
        total = conn.execute("SELECT COUNT(*) AS n FROM fill").fetchone()["n"]
    except stockbit.QuarantineError as exc:
        # The fee identity is a document-level gate (SPEC §5.6): a mismatch
        # quarantines the WHOLE document with zero fills committed. Surface it
        # loudly and exit non-zero — a shifted column must not land silently.
        print(f"quarantined — nothing committed: {exc}", file=sys.stderr)
        return 1
    except stockbit.StockbitError as exc:
        print(f"drop failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"dropped {inserted} new fill(s) from {args.file}  ({total} in ledger)")
    return 0


def _format_proposal(p: trades.Proposal) -> str:
    if p.kind == "new-trade":
        return (
            f"  new-trade   {p.book} {p.symbol} {p.entry_date}  "
            f"{p.quantity:g} @ {p.avg_price:.4f}  — {p.note}"
        )
    where = ", ".join(f"{a.quantity:g}→{a.entry_date}" for a in p.allocations) or "nothing open"
    return (
        f"  exit-alloc  {p.book} {p.symbol} {p.exit_date}  "
        f"{p.quantity:g} @ {p.price}  [{where}]  — {p.note}"
    )


def cmd_confirm(args: argparse.Namespace) -> int:
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        proposals = trades.propose(conn)
        if args.dry_run:
            # The one door, shown before it commits (SPEC §5.1): nothing is written.
            if not proposals:
                print("no proposals — the Trade ledger matches the Fills")
            else:
                print(f"{len(proposals)} proposal(s) — nothing committed (dry run):")
                for p in proposals:
                    print(_format_proposal(p))
            return 0
        result = trades.confirm(conn)
    finally:
        conn.close()
    print(
        f"confirmed: {result.new_trades} new Trade(s), "
        f"{result.exits_allocated} exit(s) allocated"
        + (f", {result.parked_exits} parked" if result.parked_exits else "")
    )
    for closed in result.closed_trades:
        print(f"  closed: {closed}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        provenance = stops.set_stop(conn, args.trade_id, args.price)
    except (stops.UnknownTrade, stops.FrozenError) as exc:
        # Setting a stop is an explicit operator action: a missing Trade or a
        # frozen one is a refusal to surface, not something to swallow.
        print(f"stop refused: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    # Provenance is not typed — it falls out of when the stop arrived (SPEC §3.2).
    print(f"stop {args.price:g} set on Trade {args.trade_id}  (provenance: {provenance})")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        stops.set_setup(conn, args.trade_id, args.value)
    except (stops.UnknownTrade, stops.FrozenError, stops.UnknownSetup) as exc:
        print(f"setup refused: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"setup {args.value} set on Trade {args.trade_id}")
    return 0


def _build_flex_client(warn):
    # A seam: the real DoH resolver + HTTP transport are assembled here, and
    # tests substitute a fake so the wire is never touched.
    return flex_client.build_default_client(warn=warn)


def cmd_fetch(args: argparse.Namespace) -> int:
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    warnings: list[str] = []
    client = _build_flex_client(warn=warnings.append)
    try:
        statement = client.fetch_statement(args.query_id)
        # The fetched XML joins the keep-forever tier on disk too (SPEC §13.5):
        # once a report ages out of Flex's reachable window it is gone, so the
        # bytes as fetched are archived verbatim before parsing.
        backup.archive_raw(
            backup.archive_dir_for(db_path),
            book="US", kind="flex-trades-xml", content=statement, ext="xml",
        )
        inserted = fills.import_flex_text(conn, statement)
        total = conn.execute("SELECT COUNT(*) AS n FROM fill").fetchone()["n"]
    except (
        flex.FlexError,
        flex_client.InterceptionError,
        flex_client.EmptyResponseError,
        secrets.SecretNotFound,
    ) as exc:
        # The network path lies (SPEC §13.3): a Flex error body, DNS
        # interception, an empty series or a missing token are all failures to
        # surface loudly, not empty statements to swallow.
        print(f"fetch failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    for notice in warnings:
        print(notice, file=sys.stderr)
    print(
        f"fetched {inserted} new fill(s) from Flex query {args.query_id}"
        f"  ({total} in ledger)"
    )
    return 0


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def cmd_import_nav(args: argparse.Namespace) -> int:
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        _archive_source_file(db_path, book="US", kind="nav-flex-xml", path=args.file)
        with open(args.file, encoding="utf-8") as fh:
            captured = equity.import_nav_flex_text(conn, fh.read(), fetch_date=_today_iso())
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM equity_snapshot WHERE book='US'"
        ).fetchone()["n"]
    except equity.EquityError as exc:
        # A NAV error body is an error, never an empty series (SPEC §9.2).
        print(f"NAV import failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"captured {captured} NAV snapshot(s) from {args.file}  ({total} US snapshots)")
    return 0


def cmd_fetch_nav(args: argparse.Namespace) -> int:
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    warnings: list[str] = []
    client = _build_flex_client(warn=warnings.append)
    try:
        statement = client.fetch_statement(args.query_id)
        backup.archive_raw(
            backup.archive_dir_for(db_path),
            book="US", kind="nav-flex-xml", content=statement, ext="xml",
        )
        captured = equity.import_nav_flex_text(conn, statement, fetch_date=_today_iso())
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM equity_snapshot WHERE book='US'"
        ).fetchone()["n"]
    except (
        equity.EquityError,
        flex.FlexError,
        flex_client.InterceptionError,
        flex_client.EmptyResponseError,
        secrets.SecretNotFound,
    ) as exc:
        # The NAV XML joins the keep-forever tier only once it is captured; a
        # failed fetch surfaces loudly rather than recording "no equity".
        print(f"NAV fetch failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    for notice in warnings:
        print(notice, file=sys.stderr)
    print(
        f"captured {captured} NAV snapshot(s) from Flex query {args.query_id}"
        f"  ({total} US snapshots)"
    )
    return 0


def cmd_equity_idx(args: argparse.Namespace) -> int:
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        if args.file:
            with open(args.file, encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            written = equity.record_idx_series(conn, rows, fetch_date=_today_iso())
        else:
            if args.date is None or args.portfolio is None or args.ledger_balance is None:
                raise ValueError(
                    "single entry needs --date, --portfolio and --ledger-balance "
                    "(or pass --file for a month-end series)"
                )
            equity.record_idx_snapshot(
                conn,
                date=args.date,
                portfolio=args.portfolio,
                ledger_balance=args.ledger_balance,
                cash_investor=args.cash_investor,
                provenance="estimated" if args.estimated else "stated",
                fetch_date=_today_iso(),
            )
            written = 1
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM equity_snapshot WHERE book='IDX'"
        ).fetchone()["n"]
    except (KeyError, ValueError) as exc:
        print(f"IDX equity entry failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"recorded {written} IDX snapshot(s)  ({total} IDX snapshots)")
    return 0


def cmd_restore_check(args: argparse.Namespace) -> int:
    """Rehearse a restore end to end and print what was verified (SPEC §13.5, §14.1).

    A backup that has not been restored is a belief. This restores a snapshot
    into a scratch location, opens the journal against it, checks integrity and
    the expected schema, and prints the transcript. Exits non-zero if the
    restore did not verify, so it can gate a durability check in CI or by hand.
    """
    snapshot = args.snapshot
    if snapshot is None:
        snaps_dir = args.snapshots_dir or backup.snapshots_dir_for(
            args.db or db.default_db_path()
        )
        snapshot = _latest_snapshot(snaps_dir)
        if snapshot is None:
            print(f"no snapshot found under {snaps_dir}", file=sys.stderr)
            return 1

    with tempfile.TemporaryDirectory(prefix="journal-restore-") as scratch:
        report = backup.rehearse_restore(snapshot, scratch)
        print(report.render())
    return 0 if report.verified else 1


def _latest_snapshot(snaps_dir: str) -> Optional[str]:
    if not os.path.isdir(snaps_dir):
        return None
    names = sorted(
        n for n in os.listdir(snaps_dir) if n.startswith("journal-") and n.endswith(".db")
    )
    return os.path.join(snaps_dir, names[-1]) if names else None


def _add_db_argument(subparser: argparse.ArgumentParser) -> None:
    # Both subcommands take the same store path; keep the one help string here.
    subparser.add_argument(
        "--db",
        default=None,
        help="path to the SQLite store (default: $JOURNAL_DB or ~/.automatic-trading-journal/journal.db)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="journal",
        description="Automatic Trading Journal — headless daily job.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="advance each book and write a run record")
    _add_db_argument(run_p)
    run_p.add_argument(
        "--as-of",
        default=None,
        metavar="YYYY-MM-DD",
        help="frontier date to advance to (default: today, UTC)",
    )
    run_p.set_defaults(func=cmd_run)

    import_p = sub.add_parser(
        "import", help="parse an IBKR Flex XML file into the Fill ledger"
    )
    import_p.add_argument("file", help="path to the Flex XML file on disk")
    _add_db_argument(import_p)
    import_p.set_defaults(func=cmd_import)

    drop_p = sub.add_parser(
        "drop",
        help="parse a Stockbit Trade Confirmation (PDF or extracted text) into the Fill ledger",
    )
    drop_p.add_argument(
        "file", help="path to the TC PDF (or its pdftotext -layout text)"
    )
    _add_db_argument(drop_p)
    drop_p.set_defaults(func=cmd_drop)

    confirm_p = sub.add_parser(
        "confirm",
        help="derive Trades from Fills and commit them (the one confirm door)",
    )
    confirm_p.add_argument(
        "--dry-run",
        action="store_true",
        help="show the proposals without committing anything",
    )
    _add_db_argument(confirm_p)
    confirm_p.set_defaults(func=cmd_confirm)

    stop_p = sub.add_parser(
        "stop",
        help="record or chase a Trade's stop (provenance derives from when it arrives)",
    )
    stop_p.add_argument("trade_id", type=int, help="the Trade id to set the stop on")
    stop_p.add_argument("price", type=float, help="the stop price")
    _add_db_argument(stop_p)
    stop_p.set_defaults(func=cmd_stop)

    setup_p = sub.add_parser(
        "setup",
        help="set a Trade's setup (base_breakout | high_tight_flag | other)",
    )
    setup_p.add_argument("trade_id", type=int, help="the Trade id to set the setup on")
    setup_p.add_argument(
        "value", choices=stops.SETUP_VOCABULARY, help="the setup name"
    )
    _add_db_argument(setup_p)
    setup_p.set_defaults(func=cmd_setup)

    fetch_p = sub.add_parser(
        "fetch",
        help="fetch the IBKR Activity Flex Query over the wire into the ledger",
    )
    fetch_p.add_argument(
        "query_id", help="the saved Activity Flex Query id (Executions level of detail)"
    )
    _add_db_argument(fetch_p)
    fetch_p.set_defaults(func=cmd_fetch)

    import_nav_p = sub.add_parser(
        "import-nav",
        help="capture an IBKR NAV Summary in Base Flex XML file as EquitySnapshots",
    )
    import_nav_p.add_argument("file", help="path to the NAV Flex XML file on disk")
    _add_db_argument(import_nav_p)
    import_nav_p.set_defaults(func=cmd_import_nav)

    fetch_nav_p = sub.add_parser(
        "fetch-nav",
        help="fetch the second (NAV Summary in Base) Flex query and capture snapshots",
    )
    fetch_nav_p.add_argument(
        "query_id", help="the saved NAV Summary in Base Flex query id (a second query)"
    )
    _add_db_argument(fetch_nav_p)
    fetch_nav_p.set_defaults(func=cmd_fetch_nav)

    idx_p = sub.add_parser(
        "equity-idx",
        help="hand-enter IDX EquitySnapshot(s) — one, or a month-end series from CSV",
    )
    idx_p.add_argument(
        "--file",
        default=None,
        help="CSV of a month-end series (columns: date,portfolio,ledger_balance"
        "[,cash_investor][,provenance]) — entered in one sitting (SPEC §9.6)",
    )
    idx_p.add_argument("--date", metavar="YYYY-MM-DD", help="snapshot date (single entry)")
    idx_p.add_argument("--portfolio", type=float, help="Portfolio figure (single entry)")
    idx_p.add_argument(
        "--ledger-balance", type=float, dest="ledger_balance",
        help="ledger closing balance (single entry)",
    )
    idx_p.add_argument(
        "--cash-investor", type=float, dest="cash_investor", default=None,
        help="Cash Investor figure (stored for the deferred denominator question)",
    )
    idx_p.add_argument(
        "--estimated", action="store_true",
        help="mark provenance 'estimated' (typed from memory) rather than 'stated'",
    )
    _add_db_argument(idx_p)
    idx_p.set_defaults(func=cmd_equity_idx)

    restore_p = sub.add_parser(
        "restore-check",
        help="rehearse a restore: restore a DB snapshot to scratch and verify it opens",
    )
    restore_p.add_argument(
        "snapshot",
        nargs="?",
        default=None,
        help="path to a snapshot .db (default: the newest under the snapshots dir)",
    )
    restore_p.add_argument(
        "--snapshots-dir",
        default=None,
        dest="snapshots_dir",
        help="where to look for the newest snapshot (default: $JOURNAL_SNAPSHOTS_DIR or <db>/../snapshots)",
    )
    _add_db_argument(restore_p)
    restore_p.set_defaults(func=cmd_restore_check)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
