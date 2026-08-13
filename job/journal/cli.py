"""The ``journal`` CLI — a plain idempotent command (SPEC §13.7 seam 1).

``launchd`` merely calls this; any scheduler on any host substitutes without
touching the job. Nothing macOS-specific sits in this path.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from . import db, fills, flex, flex_client, secrets, trades
from .run import RunResult, execute_run


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

    fetch_p = sub.add_parser(
        "fetch",
        help="fetch the IBKR Activity Flex Query over the wire into the ledger",
    )
    fetch_p.add_argument(
        "query_id", help="the saved Activity Flex Query id (Executions level of detail)"
    )
    _add_db_argument(fetch_p)
    fetch_p.set_defaults(func=cmd_fetch)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
