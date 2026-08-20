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

from . import backup, books, counterfactual, db, doh, equity, export, fills, flex, flex_client, flex_transport, review, risk, secrets, stockbit, stops, trades
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
        for p in result.passes:
            if p.book != book.book:
                continue
            if p.status in ("gated", "error"):
                lines.append(f"      {p.name}: {p.status} — {p.detail}")
            else:
                lines.append(f"      {p.name}: {p.detail}")
    # The nags, as stated facts (SPEC §11.4): read off the banner, never alarms.
    for n in result.nags:
        lines.append(f"  ! {n.detail}")
    if result.snapshot is not None:
        off = f" (+ off-site {result.snapshot.offsite_path})" if result.snapshot.offsite_path else ""
        lines.append(f"snapshot: {result.snapshot.path}{off}")
    elif result.snapshot_error is not None:
        lines.append(f"snapshot: skipped — {result.snapshot_error}")
    return "\n".join(lines)


# The env seam that turns the nightly fetch off (§13.7): ``JOURNAL_BARS=off``.
# It exists because the bar pass is the one part of ``run`` that touches the
# network, and an integration test of the CLI must exercise the whole command
# without a socket. Checking whether ``yfinance`` imports is not a substitute —
# the adapter defers that import to the moment of a real fetch, by design, so
# the package being absent stays invisible until the socket is opened.
ENV_BARS = "JOURNAL_BARS"


def _build_bar_cache(conn):
    """Assemble the real bar cache, or ``None`` when bars are switched off.

    A seam, like the Flex client's: the concrete fetcher is named only here, so
    nothing above the composition root depends on yfinance (§4.4). ``None``
    reaches the run as a *gated* bars pass — stated on the run record, never a
    silent no-op — so an operator reads the reason off the banner instead of
    wondering why the regime never stamped.
    """
    if os.environ.get(ENV_BARS, "").strip().lower() in {"off", "0", "false"}:
        return None

    from .bars import BarCache
    from .yfinance_adapter import YFinanceFetcher

    return BarCache(conn, YFinanceFetcher())


def cmd_run(args: argparse.Namespace) -> int:
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        result = execute_run(
            conn, as_of=args.as_of, bar_cache=_build_bar_cache(conn)
        )
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
    park = " [parked]" if p.blocked else ""
    if p.kind in ("new-trade", "add-fills", "restatement", "drift"):
        avg = f"{p.avg_price:.4f}" if p.avg_price is not None else "?"
        return (
            f"  {p.kind:<16}{park} {p.book} {p.symbol} {p.entry_date}  "
            f"{p.quantity:g} @ {avg}  — {p.note}"
        )
    if p.kind in ("exit-allocation", "orphan-exit"):
        where = ", ".join(f"{a.quantity:g}→{a.entry_date}" for a in p.allocations) or "nothing open"
        reason = f"  reason:{p.proposed_reason}" if p.proposed_reason else ""
        return (
            f"  {p.kind:<16}{park} {p.book} {p.symbol} {p.exit_date}  "
            f"{p.quantity:g} @ {p.price}  [{where}]{reason}  — {p.note}"
        )
    if p.kind == "enrichment-repair":
        return f"  {p.kind:<16}{park} {p.book} {p.symbol} {p.entry_date}  — {p.note}"
    if p.kind == "quarantine":
        return f"  {p.kind:<16}{park}  — {p.detail}"
    return f"  {p.kind:<16}{park} {p.book} {p.symbol}  — {p.note}"


def cmd_scope_start(args: argparse.Namespace) -> int:
    """Show or move a Book's Scope Start (ADR 0008)."""
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        if args.date:
            try:
                datetime.strptime(args.date, "%Y-%m-%d")
            except ValueError:
                print(f"scope-start: {args.date!r} is not an ISO date", file=sys.stderr)
                return 2
            books.set_scope_start(conn, args.book, args.date)
            held = conn.execute(
                "SELECT COUNT(*) AS n FROM trade WHERE book = ? AND entry_date < ?",
                (args.book, args.date),
            ).fetchone()["n"]
            print(
                f"{args.book} Scope Start set to {args.date} — "
                f"{held} earlier Trade(s) stay in the journal and stop counting"
            )
            return 0
        for book in books.BOOKS:
            start = books.scope_start(conn, book)
            if start == books.NO_SCOPE_START:
                print(f"{book}: no Scope Start — every Trade counts")
            else:
                held = conn.execute(
                    "SELECT COUNT(*) AS n FROM trade WHERE book = ? AND entry_date < ?",
                    (book, start),
                ).fetchone()["n"]
                print(f"{book}: {start}  ({held} earlier Trade(s) withheld)")
    finally:
        conn.close()
    return 0


def _parse_stop_args(pairs: Optional[Sequence[str]]) -> dict:
    """``["AVGO=302", ...]`` → ``{"AVGO": 302.0}``, rejecting anything ambiguous."""
    out: dict = {}
    for raw in pairs or ():
        symbol, sep, price = raw.partition("=")
        if not sep or not symbol.strip():
            raise ValueError(f"--stop expects SYMBOL=PRICE, got {raw!r}")
        symbol = symbol.strip().upper()
        try:
            value = float(price)
        except ValueError:
            raise ValueError(f"--stop {symbol}: {price!r} is not a price") from None
        if symbol in out and out[symbol] != value:
            raise ValueError(f"--stop {symbol} given twice with different prices")
        out[symbol] = value
    return out


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
        try:
            stops_by_symbol = _parse_stop_args(args.stop)
        except ValueError as exc:
            print(f"confirm failed: {exc}", file=sys.stderr)
            return 2
        try:
            result = trades.confirm(
                conn,
                stops_by_symbol=stops_by_symbol,
                declined=args.no_stop,
                demand_stop=True,
            )
        except stops.StopAboveEntry as exc:
            # Refuse the batch rather than land a Trade whose R would be inverted
            # from the moment it commits. The Fills are untouched; fix the number
            # and re-run.
            print(f"confirm refused: {exc}", file=sys.stderr)
            print("nothing committed — correct the stop and confirm again",
                  file=sys.stderr)
            return 1
    finally:
        conn.close()
    # The demand, stated with the exact commands that answer it (ADR 0010).
    if result.unanswered:
        print(
            f"{len(result.unanswered)} new Trade(s) held — each needs a stop or an "
            "explicit decline before it commits:",
            file=sys.stderr,
        )
        for held in result.unanswered:
            print(f"  {held}", file=sys.stderr)
        print(
            "\n  --stop SYMBOL=PRICE   record the stop you were working to\n"
            "  --no-stop SYMBOL      commit without one, accepting that this Trade\n"
            "                        has no Risk % and no R once it freezes\n"
            "\nTheir Fills are untouched; they re-propose on the next confirm.",
            file=sys.stderr,
        )
    extra = "".join(
        f", {n} {label}"
        for n, label in (
            (result.added_fills, "add-fills"),
            (result.restatements, "restated"),
            (result.drifts, "drift"),
        )
        if n
    )
    print(
        f"confirmed: {result.new_trades} new Trade(s), "
        f"{result.exits_allocated} exit(s) allocated" + extra
        + (f", {result.parked_exits} parked" if result.parked_exits else "")
    )
    for closed in result.closed_trades:
        print(f"  closed: {closed}")
    return 0


def cmd_bulk_confirm(args: argparse.Namespace) -> int:
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        result = trades.bulk_confirm_exits(conn)
    finally:
        conn.close()
    # Exit reasons only (SPEC §5.8): new Trades and parked items are left alone.
    print(
        f"bulk-confirmed {result.exits_allocated} exit(s) at their proposed reasons "
        "— new Trades and parked items untouched"
    )
    for closed in result.closed_trades:
        print(f"  closed: {closed}")
    return 0


def cmd_remember_symbol(args: argparse.Namespace) -> int:
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        repaired = trades.remember_symbol_rule(conn, args.source, args.from_symbol, args.to_symbol)
    finally:
        conn.close()
    # A rule forever (SPEC §5.4): applied pre-queue on every future statement,
    # and it repairs Trades already committed under the wrong symbol.
    print(
        f"remembered {args.source}: {args.from_symbol} -> {args.to_symbol}  "
        f"({repaired} committed Trade(s) repaired)"
    )
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        provenance = stops.set_stop(conn, args.trade_id, args.price)
    except (stops.UnknownTrade, stops.FrozenError, stops.StopAboveEntry) as exc:
        # Setting a stop is an explicit operator action: a missing Trade, a frozen
        # one, or a stop above entry is a refusal to surface, not to swallow.
        print(f"stop refused: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    # Provenance is not typed — it falls out of when the stop arrived (SPEC §3.2).
    print(f"stop {args.price:g} set on Trade {args.trade_id}  (provenance: {provenance})")
    return 0


def cmd_no_stop(args: argparse.Namespace) -> int:
    """Record a Trade as deliberately stop-less (ADR 0010)."""
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        for trade_id in args.trade_id:
            stops.decline_stop(conn, trade_id)
    except (stops.UnknownTrade, stops.FrozenError) as exc:
        print(f"decline refused: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    ids = ", ".join(str(t) for t in args.trade_id)
    print(
        f"Trade(s) {ids} recorded as deliberately stop-less — "
        "no Risk % and no R, permanently once frozen"
    )
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


def cmd_review(args: argparse.Namespace) -> int:
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        review.mark_reviewed(conn, args.trade_id, at=_now_iso())
    except review.UnknownTrade as exc:
        print(f"review refused: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"Trade {args.trade_id} marked reviewed")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        review.set_note(conn, args.trade_id, args.text)
    except review.UnknownTrade as exc:
        print(f"note refused: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"note set on Trade {args.trade_id}")
    return 0


def cmd_exit_reason(args: argparse.Namespace) -> int:
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        review.override_exit_reason(conn, args.exit_id, args.reason)
    except (review.UnknownExit, review.UnknownReason) as exc:
        print(f"exit-reason refused: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"Exit {args.exit_id} reason set to {args.reason}")
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
        flex_transport.TransportError,
        doh.DohError,
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        flex_transport.TransportError,
        doh.DohError,
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


def _format_pct(value: Optional[float], marker: Optional[str]) -> str:
    if value is not None:
        return f"{value:.3f}%"
    return f"null ({marker})" if marker else "null (no stop)"


def cmd_risk(args: argparse.Namespace) -> int:
    """Report Risk % and Exposure % per book, with the staleness banner and counts.

    Read-time computation over the current snapshots (§9.4): one lookup, one
    calendar-day bound per book, both percentages null-with-marker past it, and
    the ``estimated`` tier excluded from the aggregates with its count reported.
    The staleness marker surfaces as a plain banner line (§9.7), never an alarm.
    """
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        selected = [args.book] if args.book else list(books.BOOKS)
        for book in selected:
            results = risk.compute_book(conn, book)
            print(f"{book}  ({len(results)} Trade(s))")
            for r in results:
                marker = risk.INSUFFICIENT_HISTORY if r.stale else None
                print(
                    f"  Trade {r.trade_id} {r.entry_date}  "
                    f"risk {_format_pct(r.risk_percentage, marker)}  "
                    f"exposure {_format_pct(r.exposure_percentage, marker)}"
                    + (f"  [{r.provenance}]" if r.provenance == risk.ESTIMATED else "")
                )
            for r in results:
                line = risk.banner_line(r)
                if line:
                    print(f"  ! {line}")
            agg = risk.aggregate(results, metric="risk")
            print(
                f"  risk aggregate: {agg.included} included; excluded "
                f"{agg.excluded_stale} stale, {agg.excluded_estimated} estimated, "
                f"{agg.excluded_no_stop} no-stop"
            )
    finally:
        conn.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Write the curated LLM export for one book (SPEC §12): legend, aggregates,
    then one JSON object per Trade normalized to R and ADR.

    One book per export (§12.4) — a two-book export would put two incomparable
    drawdown curves in one column. The output goes to ``--out`` or stdout so it
    pipes straight into whatever consumes it.
    """
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        text = export.export(
            conn, book=args.book, date_from=args.date_from, date_to=args.date_to
        )
    finally:
        conn.close()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"Wrote {args.book} export to {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_counterfactual(args: argparse.Namespace) -> int:
    """Score every closed Trade against all six variants and report the deltas.

    Adherence is inverted (SPEC §10.1): the engine derives *which rule a Trade
    best fits*, storing signed deltas against the nominal variant and never a
    verdict. Runs on closed Trades only (§10.9), recomputing in place until every
    variant is resolved | capped. Best fit is derived at read time from the stored
    six-way distance vector; no-stop Trades run trail-only and are flagged.
    """
    db_path = args.db or db.default_db_path()
    conn = db.connect(db_path)
    try:
        store = counterfactual.CounterfactualStore(conn)
        selected = [args.book] if args.book else list(books.BOOKS)
        for book in selected:
            results = counterfactual.compute_book(conn, book)
            print(f"{book}  ({len(results)} closed Trade(s))")
            for tc in results:
                store.upsert(tc.trade_id, tc)
                cost = tc.deviation_cost()
                cost_str = f"{cost:+.2f}" if cost is not None else "—"
                flags = " [stopless]" if tc.stopless else ""
                # dividend_drag_r sits *beside* Realized R and is omitted entirely
                # when null (§7.7) — absent, it reads as unknown, not zero.
                if tc.dividend_drag_r is not None:
                    flags += f" [div_drag {tc.dividend_drag_r:.2f}R]"
                print(
                    f"  Trade {tc.trade_id} {tc.symbol} {tc.entry_date}  "
                    f"fit {tc.best_fit()}  partial {tc.partial_state}  "
                    f"exit {tc.exit_path}  status {tc.nominal_status}  "
                    f"cost {cost_str}{flags}"
                )
            eligible = [tc for tc in results if not tc.stopless]
            print(
                f"  {len(eligible)} in cross-Trade aggregates; "
                f"{len(results) - len(eligible)} no-stop excluded"
            )
    finally:
        conn.close()
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

    scope_p = sub.add_parser(
        "scope-start",
        help="show or move the date from which a Book's Trades count (ADR 0008)",
    )
    scope_p.add_argument("book", nargs="?", choices=list(books.BOOKS),
                         help="the book to move; omit to show both")
    scope_p.add_argument("date", nargs="?", help="ISO date, inclusive")
    _add_db_argument(scope_p)
    scope_p.set_defaults(func=cmd_scope_start)

    confirm_p = sub.add_parser(
        "confirm",
        help="derive Trades from Fills and commit them (the one confirm door)",
    )
    confirm_p.add_argument(
        "--dry-run",
        action="store_true",
        help="show the proposals without committing anything",
    )
    confirm_p.add_argument(
        "--stop",
        action="append",
        metavar="SYMBOL=PRICE",
        help="the stop for a new Trade, repeatable (ADR 0010)",
    )
    confirm_p.add_argument(
        "--no-stop",
        action="append",
        default=[],
        metavar="SYMBOL",
        help="commit this new Trade without a stop, accepting the permanent hole",
    )
    _add_db_argument(confirm_p)
    confirm_p.set_defaults(func=cmd_confirm)

    bulk_p = sub.add_parser(
        "bulk-confirm",
        help="confirm every confirmable exit at its proposed reason (SPEC §5.8) — "
        "new Trades and parked items untouched",
    )
    _add_db_argument(bulk_p)
    bulk_p.set_defaults(func=cmd_bulk_confirm)

    remember_p = sub.add_parser(
        "remember-symbol",
        help="remember a symbol misparse as a parser rule and repair committed Trades (SPEC §5.4)",
    )
    remember_p.add_argument("source", help="the parser the rule corrects (e.g. 'stockbit', 'ibkr')")
    remember_p.add_argument("from_symbol", help="the symbol as mis-parsed")
    remember_p.add_argument("to_symbol", help="the symbol it should be")
    _add_db_argument(remember_p)
    remember_p.set_defaults(func=cmd_remember_symbol)

    stop_p = sub.add_parser(
        "stop",
        help="record or chase a Trade's stop (provenance derives from when it arrives)",
    )
    stop_p.add_argument("trade_id", type=int, help="the Trade id to set the stop on")
    stop_p.add_argument("price", type=float, help="the stop price")
    _add_db_argument(stop_p)
    stop_p.set_defaults(func=cmd_stop)

    no_stop_p = sub.add_parser(
        "no-stop",
        help="record a committed Trade as deliberately stop-less (ADR 0010)",
    )
    no_stop_p.add_argument(
        "trade_id", type=int, nargs="+", help="the Trade id(s) going without a stop"
    )
    _add_db_argument(no_stop_p)
    no_stop_p.set_defaults(func=cmd_no_stop)

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

    review_p = sub.add_parser(
        "review",
        help="mark a Trade reviewed on the weekly surface (drains the stragglers)",
    )
    review_p.add_argument("trade_id", type=int, help="the Trade id to mark reviewed")
    _add_db_argument(review_p)
    review_p.set_defaults(func=cmd_review)

    note_p = sub.add_parser("note", help="set a Trade's free-text review note")
    note_p.add_argument("trade_id", type=int, help="the Trade id to annotate")
    note_p.add_argument("text", help="the note text")
    _add_db_argument(note_p)
    note_p.set_defaults(func=cmd_note)

    exit_reason_p = sub.add_parser(
        "exit-reason",
        help="override an Exit's reason the confirm queue accepted unread (SPEC 5.8)",
    )
    exit_reason_p.add_argument("exit_id", type=int, help="the trade_exit id to re-reason")
    exit_reason_p.add_argument(
        "reason", choices=trades.EXIT_REASONS, help="the corrected exit reason"
    )
    _add_db_argument(exit_reason_p)
    exit_reason_p.set_defaults(func=cmd_exit_reason)

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

    risk_p = sub.add_parser(
        "risk",
        # argparse expands help through %-formatting, so a literal percent is doubled.
        help="report Risk %% and Exposure %% per book with the staleness bound and counts",
    )
    risk_p.add_argument(
        "--book", choices=books.BOOKS, default=None,
        help="limit to one book (default: every book — never aggregated across)",
    )
    _add_db_argument(risk_p)
    risk_p.set_defaults(func=cmd_risk)

    cf_p = sub.add_parser(
        "counterfactual",
        help="score closed Trades against all six variants and report the adherence deltas",
    )
    cf_p.add_argument(
        "--book", choices=books.BOOKS, default=None,
        help="limit to one book (default: every book — never aggregated across)",
    )
    _add_db_argument(cf_p)
    cf_p.set_defaults(func=cmd_counterfactual)

    export_p = sub.add_parser(
        "export",
        help="write the curated LLM export (JSONL, legend + aggregates) for one book",
    )
    export_p.add_argument(
        "--book", choices=books.BOOKS, required=True,
        help="the one book to export — one book per export (never aggregated across)",
    )
    export_p.add_argument(
        "--from", dest="date_from", default=None,
        help="earliest entry date to include (ISO); seq gaps below it stay uncompacted",
    )
    export_p.add_argument(
        "--to", dest="date_to", default=None,
        help="latest entry date to include (ISO)",
    )
    export_p.add_argument(
        "--out", default=None, help="write to this file instead of stdout",
    )
    _add_db_argument(export_p)
    export_p.set_defaults(func=cmd_export)

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
