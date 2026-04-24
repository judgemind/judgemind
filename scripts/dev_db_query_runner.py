#!/usr/bin/env python3
"""Run a SQL query against the dev database and return JSON results.

This script is uploaded to an ECS container by dev-db-query.sh and executed
there.  It connects to the database using the DATABASE_URL environment
variable (set in the container), executes the given query, and prints the
results as JSON to stdout.

Usage (called by dev-db-query.sh, not directly):
    python3 dev-db-query-runner.py <base64-encoded-query> <readonly-flag> [compact-flag]

Arguments:
    query_b64       The SQL query, base64-encoded to avoid shell quoting issues.
    readonly_flag   "1" to set the session to read-only mode, "0" for read-write.
    compact-flag    Optional. "1" (default) emits compact JSON with no
                    indentation; "0" emits pretty-printed indent=2 output.
                    Compact is the default because SSM / ECS-exec truncates
                    large outputs mid-stream (#3195) — a smaller payload is
                    more likely to fit under the transport's per-session byte
                    budget.

Output:
    For SELECT queries (cursor.description is not None):
        A JSON array of objects, one per row, with column names as keys.
        Example: [{"id":1,"name":"foo"},{"id":2,"name":"bar"}]

    For non-SELECT queries (cursor.description is None):
        A JSON object with the affected row count.
        Example: {"rowcount": 3}

Exit codes:
    0   Success
    1   Usage error or connection failure
"""
# permanent: true

from __future__ import annotations

import base64
import json
import os
import sys


def decode_query(query_b64: str) -> str:
    """Decode a base64-encoded SQL query string."""
    return base64.b64decode(query_b64).decode("utf-8")


def format_select_results(
    description: list[tuple[str, ...]],
    rows: list[tuple[object, ...]],
    *,
    compact: bool = True,
) -> str:
    """Format SELECT query results as a JSON array of objects.

    Each row becomes a dict mapping column names to values.  Values are
    serialised with ``default=str`` so that dates, decimals, etc. are
    rendered as strings rather than raising ``TypeError``.

    ``compact`` defaults to True (no indentation, minimal separators) so the
    output stays as small as possible for the SSM / ECS Exec transport —
    which has a per-session output byte budget that truncates large
    pretty-printed payloads (see #3195).  Pass ``compact=False`` to get the
    historical indent=2 formatting for interactive use.
    """
    columns = [col[0] for col in description]
    result = [dict(zip(columns, row)) for row in rows]
    if compact:
        return json.dumps(result, default=str, separators=(",", ":"))
    return json.dumps(result, indent=2, default=str)


def format_non_select_result(rowcount: int) -> str:
    """Format a non-SELECT result as a JSON object with the row count."""
    return json.dumps({"rowcount": rowcount})


def run_query(query_b64: str, readonly_flag: str, *, compact: bool = True) -> None:
    """Connect to the database, execute the query, and print results.

    This function imports ``psycopg`` lazily so that the module-level
    formatting helpers can be tested without psycopg installed.
    """
    import psycopg  # lazy import — only available in the ECS container

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("Error: DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    query = decode_query(query_b64)

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cursor:
                if readonly_flag == "1":
                    cursor.execute("SET default_transaction_read_only = on")

                cursor.execute(query)

                if cursor.description is not None:
                    rows = cursor.fetchall()
                    print(
                        format_select_results(
                            list(cursor.description), rows, compact=compact
                        )
                    )
                elif cursor.rowcount == -1:
                    # No description AND no rowcount means nothing actually
                    # executed — typically a comment-only or empty query slipped
                    # past the caller-side guard. Fail loud instead of emitting
                    # the misleading `{"rowcount": -1}` response (#2862).
                    print(
                        "Error: query produced no result set and no affected rows; "
                        "did you pass an empty or comment-only statement?",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                else:
                    print(format_non_select_result(cursor.rowcount))

                # Flush stdout explicitly and do a best-effort sync. The SSM /
                # ECS-exec transport is known to drop trailing bytes when the
                # remote process exits before the WebSocket has delivered the
                # final chunk (see #3195 investigation). Flushing + a brief
                # drain gives the transport time to emit the last frame before
                # the wrapping shell runs `rm` and the session closes.
                sys.stdout.flush()
                try:
                    os.fsync(sys.stdout.fileno())
                except OSError:
                    # stdout may not be a real file descriptor in test harnesses.
                    pass

    except psycopg.Error as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    """Entry point — parse CLI arguments and run the query."""
    if len(sys.argv) not in (3, 4):
        print(
            "Usage: python3 dev-db-query-runner.py <base64-query> <readonly-flag> [compact-flag]",
            file=sys.stderr,
        )
        sys.exit(1)

    query_b64 = sys.argv[1]
    readonly_flag = sys.argv[2]
    # compact defaults to True; pass "0" to force historical indent=2 formatting.
    compact_flag = sys.argv[3] if len(sys.argv) == 4 else "1"

    if readonly_flag not in ("0", "1"):
        print(
            f"Error: readonly_flag must be '0' or '1', got '{readonly_flag}'",
            file=sys.stderr,
        )
        sys.exit(1)

    if compact_flag not in ("0", "1"):
        print(
            f"Error: compact_flag must be '0' or '1', got '{compact_flag}'",
            file=sys.stderr,
        )
        sys.exit(1)

    run_query(query_b64, readonly_flag, compact=(compact_flag == "1"))


if __name__ == "__main__":
    main()
