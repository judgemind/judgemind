#!/usr/bin/env python3
"""Thin psycopg wrapper for dispatcher hooks to record a failure row.

Called from Claude Code hooks running inside a ``claude -p`` subprocess.
Opens a connection via ``DATABASE_URL``, inserts one row into the failures
table, closes. The insert is wrapped in try/except — any DB error is
logged to stderr and the process exits 0. The hook MUST NOT propagate an
error that would block the triggering tool call.

Spike 0.2 (issue #2684) uses ``dispatcher_spike.failures`` as the target
table via ``--table dispatcher_spike.failures``. Phase 1 will point this
at ``dispatcher.failures`` via the same flag with the production schema.

Usage::

    scripts/dispatcher/emit_failure.py \\
        --category spike_test \\
        --agent-id <id> \\
        --details '{"tool": "Read"}' \\
        [--table dispatcher_spike.failures]

Exit codes:
    0   Always — the hook must never propagate a non-zero exit.

The process captures a high-resolution wall-clock timestamp before the
insert and a second after commit; both are emitted on stderr as a JSON
line prefixed with ``EMIT_FAILURE_TIMING`` so the spike harness can
cross-reference them against the DB-side ``now()``.
"""
# venv: scraper-framework
# permanent: true

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Insert one dispatcher failure row (never blocks)."
    )
    parser.add_argument("--category", required=True, help="Failure category string.")
    parser.add_argument(
        "--agent-id",
        required=True,
        help="Agent identifier — the worktree/agent this hook fired from.",
    )
    parser.add_argument(
        "--details",
        default="{}",
        help="Free-form JSON object with additional context (default: '{}').",
    )
    parser.add_argument(
        "--table",
        default="dispatcher_spike.failures",
        help="Fully-qualified target table (default: dispatcher_spike.failures).",
    )
    return parser.parse_args(argv)


def _validated_table(table: str) -> str:
    """Raise ValueError if ``table`` is not a safe ``schema.table`` identifier.

    We interpolate the table name directly into the SQL string because
    psycopg does not support parameterized table identifiers. Guard against
    injection by restricting to the exact ``schema.table`` shape with only
    alphanumerics and underscores on each side.
    """
    import re

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", table):
        raise ValueError(
            f"refusing unsafe table identifier: {table!r} "
            "(expected schema.table with alnum/underscore only)"
        )
    return table


def _parse_details(raw: str) -> dict[str, object]:
    """Parse ``--details`` JSON; return ``{}`` on any error."""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _emit_timing(
    *,
    failure_id: str,
    started_wallclock: str,
    completed_wallclock: str,
    elapsed_ms: float,
    status: str,
) -> None:
    """Emit a one-line timing summary to stderr for the harness to capture."""
    payload = {
        "failure_id": failure_id,
        "started_wallclock": started_wallclock,
        "completed_wallclock": completed_wallclock,
        "elapsed_ms": round(elapsed_ms, 3),
        "status": status,
    }
    sys.stderr.write(f"EMIT_FAILURE_TIMING {json.dumps(payload)}\n")
    sys.stderr.flush()


def _insert(
    *,
    database_url: str,
    table: str,
    failure_id: str,
    category: str,
    agent_id: str,
    details: dict[str, object],
    hook_fire_ts: str,
) -> None:
    """Run the single INSERT. Psycopg is imported lazily."""
    import psycopg

    # #3356: bound per-query wall clock at 30s in addition to
    # ``connect_timeout`` so a runaway lock or replica failover can't
    # wedge the agent-runner-entrypoint subprocess. Mirror constant from
    # daemon.py inline so this module stays import-free of daemon.py.
    with psycopg.connect(
        database_url,
        connect_timeout=10,
        options="-c statement_timeout=30000",
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {table} "
                "(failure_id, category, agent_id, details, hook_fire_ts) "
                "VALUES (%s, %s, %s, %s, %s)",
                (failure_id, category, agent_id, json.dumps(details), hook_fire_ts),
            )
        conn.commit()


def emit_failure(argv: list[str]) -> int:
    """Entry point. Always returns 0 — hooks must never propagate errors."""
    try:
        args = _parse_args(argv)
    except SystemExit:
        # argparse failed; nothing we can reliably log to the DB about it.
        sys.stderr.write("emit_failure: bad arguments, swallowing.\n")
        return 0

    failure_id = str(uuid.uuid4())
    details = _parse_details(args.details)

    try:
        table = _validated_table(args.table)
    except ValueError as exc:
        sys.stderr.write(f"emit_failure: {exc}\n")
        return 0

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        sys.stderr.write("emit_failure: DATABASE_URL not set, swallowing.\n")
        _emit_timing(
            failure_id=failure_id,
            started_wallclock="",
            completed_wallclock="",
            elapsed_ms=0.0,
            status="no_db_url",
        )
        return 0

    started_wallclock = datetime.now(UTC).isoformat()
    start_ns = time.monotonic_ns()

    try:
        _insert(
            database_url=database_url,
            table=table,
            failure_id=failure_id,
            category=args.category,
            agent_id=args.agent_id,
            details=details,
            hook_fire_ts=started_wallclock,
        )
        status = "ok"
    except Exception as exc:  # pragma: no cover — generic swallow by design
        sys.stderr.write(f"emit_failure: insert failed: {exc}\n")
        status = "error"

    elapsed_ms = (time.monotonic_ns() - start_ns) / 1_000_000.0
    completed_wallclock = datetime.now(UTC).isoformat()

    _emit_timing(
        failure_id=failure_id,
        started_wallclock=started_wallclock,
        completed_wallclock=completed_wallclock,
        elapsed_ms=elapsed_ms,
        status=status,
    )
    return 0


def main() -> int:
    """CLI wrapper."""
    return emit_failure(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
