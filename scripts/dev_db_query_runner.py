#!/usr/bin/env python3
# permanent: true
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

Result payload:
    For SELECT queries (cursor.description is not None):
        A JSON array of objects, one per row, with column names as keys.
        Example: [{"id":1,"name":"foo"},{"id":2,"name":"bar"}]

    For non-SELECT queries (cursor.description is None):
        A JSON object with the affected row count.
        Example: {"rowcount": 3}

Framing (#4781):
    The payload is never printed bare. The SSM / ECS Exec transport cuts
    output off anywhere from ~1 KB to ~10 KB and still exits 0, so a bare payload can
    arrive partial with nothing to show it. Instead the runner prints a
    frame that the caller checks (scripts/dev_db_query_result.py):

    Small payloads (<= RESULT_INLINE_MAX_BYTES) go inline:
        @@JM_DEVDB_BEGIN inline bytes=<n> sha256=<hex>@@
        <payload>
        @@JM_DEVDB_END@@

    Larger payloads are written to S3 and only a pointer goes over SSM:
        @@JM_DEVDB_BEGIN s3 bytes=<n> sha256=<hex> uri=s3://<bucket>/tmp/dev-db-query/<id>.json@@
        @@JM_DEVDB_END@@

    The caller downloads the object and checks the byte count and sha256.
    A missing END marker or a mismatched digest makes dev-db-query.sh exit
    non-zero instead of printing partial JSON.

Exit codes:
    0   Success
    1   Usage error, connection failure, or result upload failure
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import uuid

# Payloads up to this size are printed inline. The SSM transport has been
# seen to cut output anywhere from ~1 KB to ~10 KB (#4781: 946 bytes on one
# dev run, 8-10 KB on others), so only small results stay inline. Anything
# larger goes through S3. A cut inline result is still caught (no END
# marker), so this threshold trades S3 round trips against retries, not
# correctness.
RESULT_INLINE_MAX_BYTES = 512

# The frame markers. scripts/dev_db_query_result.py parses these; keep the
# two files in sync (test_dev_db_query_result.py checks that they match).
FRAME_BEGIN = "@@JM_DEVDB_BEGIN"
FRAME_END = "@@JM_DEVDB_END@@"

# S3 prefix for large results. The archive bucket's `tmp/` lifecycle rule
# expires objects after 30 days (infra/terraform/modules/storage/main.tf),
# and the caller deletes the object after it downloads it.
RESULT_S3_PREFIX = "tmp/dev-db-query/"


def frame_inline(payload: str) -> str:
    """Return the inline frame (BEGIN line, payload, END line) for ``payload``."""
    data = payload.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    return (
        f"{FRAME_BEGIN} inline bytes={len(data)} sha256={digest}@@\n"
        f"{payload}\n"
        f"{FRAME_END}"
    )


def frame_s3(payload: str, uri: str) -> str:
    """Return the S3-pointer frame for ``payload`` stored at ``uri``."""
    data = payload.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    return (
        f"{FRAME_BEGIN} s3 bytes={len(data)} sha256={digest} uri={uri}@@\n{FRAME_END}"
    )


def result_bucket() -> str:
    """Return the bucket for large results, or exit 1 if none is configured.

    ``DEV_DB_QUERY_RESULT_BUCKET`` overrides; otherwise the container's
    ``JUDGEMIND_ARCHIVE_BUCKET`` (set on the ingestion worker task, whose
    role can write anywhere in the archive bucket) is used.
    """
    bucket = os.environ.get("DEV_DB_QUERY_RESULT_BUCKET") or os.environ.get(
        "JUDGEMIND_ARCHIVE_BUCKET"
    )
    if not bucket:
        print(
            "Error: result is too large to print inline and neither "
            "DEV_DB_QUERY_RESULT_BUCKET nor JUDGEMIND_ARCHIVE_BUCKET is set "
            "in the container, so it cannot be staged in S3.",
            file=sys.stderr,
        )
        sys.exit(1)
    return bucket


def upload_result(payload: str) -> str:
    """Write ``payload`` to S3 and return its ``s3://`` URI. Exit 1 on failure."""
    import boto3  # lazy import — only available in the ECS container

    bucket = result_bucket()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    key = f"{RESULT_S3_PREFIX}{stamp}-{uuid.uuid4().hex}.json"
    try:
        boto3.client("s3").put_object(
            Bucket=bucket,
            Key=key,
            Body=payload.encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # noqa: BLE001 — any upload failure is fatal here
        print(
            f"Error: could not write the result to s3://{bucket}/{key}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
    return f"s3://{bucket}/{key}"


def emit_result(payload: str, *, inline_max: int = RESULT_INLINE_MAX_BYTES) -> None:
    """Print ``payload`` framed: inline when small, through S3 when large.

    ``inline_max`` exists so an operator can force a large result through
    the inline path (DEV_DB_QUERY_INLINE_MAX_BYTES in dev-db-query.sh) to
    check that a real SSM truncation is caught.
    """
    if len(payload.encode("utf-8")) <= inline_max:
        print(frame_inline(payload))
    else:
        print(frame_s3(payload, upload_result(payload)))


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


def run_query(
    query_b64: str,
    readonly_flag: str,
    *,
    compact: bool = True,
    inline_max: int = RESULT_INLINE_MAX_BYTES,
) -> None:
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
                    emit_result(
                        format_select_results(
                            list(cursor.description), rows, compact=compact
                        ),
                        inline_max=inline_max,
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
                    emit_result(
                        format_non_select_result(cursor.rowcount),
                        inline_max=inline_max,
                    )

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
    if len(sys.argv) not in (3, 4, 5):
        print(
            "Usage: python3 dev-db-query-runner.py <base64-query> <readonly-flag> "
            "[compact-flag] [inline-max-bytes]",
            file=sys.stderr,
        )
        sys.exit(1)

    query_b64 = sys.argv[1]
    readonly_flag = sys.argv[2]
    # compact defaults to True; pass "0" to force historical indent=2 formatting.
    compact_flag = sys.argv[3] if len(sys.argv) >= 4 else "1"
    inline_max_arg = sys.argv[4] if len(sys.argv) == 5 else str(RESULT_INLINE_MAX_BYTES)
    if not inline_max_arg.isdigit():
        print(
            f"Error: inline-max-bytes must be a non-negative integer, got '{inline_max_arg}'",
            file=sys.stderr,
        )
        sys.exit(1)

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

    run_query(
        query_b64,
        readonly_flag,
        compact=(compact_flag == "1"),
        inline_max=int(inline_max_arg),
    )


if __name__ == "__main__":
    main()
