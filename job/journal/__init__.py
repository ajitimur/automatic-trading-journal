"""Automatic Trading Journal — the headless daily job.

The walking skeleton (issue #21): the thinnest complete path through the
system so every later ticket has somewhere to land. No domain logic yet.

Two independent entry points share one SQLite file (SPEC §13.6): this
headless job, which ``launchd`` invokes on schedule, and a TypeScript UI
launched on demand. Nothing is resident — the store is a file, not a daemon.
"""

__all__ = ["bars", "books", "cli", "db", "run", "secrets", "yfinance_adapter"]
