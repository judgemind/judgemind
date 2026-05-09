#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Repoint ``derived.documents`` rows from mislabeled flat-hash S3 keys to
their correctly-named twins.

The 2026-03-28 migration in commit ``fbf8e38`` (archived as
``scripts/archive/migrate_s3_keys.py``) wrote S3 objects under content-addressed
keys whose filename SHA-256 did **not** match the SHA-256 of the bytes — for
documents whose ``content_hash`` was a synthetic split-child value rather than
a real bytes hash. ``CopyObject`` preserved the original metadata
``content-hash`` (the correct bytes hash), so each mislabeled object carries a
metadata header that points to the right value.

Many of the mislabels were paired — at the same `ca/{county}/{court}/raw/`
prefix the bytes also live under a *correctly-named* twin where filename hash
== metadata hash == bytes hash. ``rebuild_db.py`` (without ``--reset``) only
**adds** rows for newly-discovered S3 keys; it does not **update** existing
rows whose ``s3_key`` points at a now-superseded mislabel. As a result
``derived.documents`` still references the mislabel even after a rebuild has
discovered the correctly-named twin.

This script repoints those rows. It does **two passes**:

1. **Enumerate pass:** paginate ``list_objects_v2`` under ``ca/`` (or a single
   county prefix), match keys of shape ``ca/{county}/{court}/raw/<hex64>.<ext>``,
   HEAD each, compare filename hex64 to metadata ``content-hash``. For each
   mislabel, build the ``twin`` key (same prefix, filename = metadata hash) and
   HEAD it. The twin is **valid** only when (a) it exists in S3 AND (b) its
   filename hex64 == its own metadata ``content-hash`` (i.e., the twin is
   correctly-labelled).

2. **Repoint pass:** in --apply mode, for each (mislabel, twin) pair, run
   ``UPDATE derived.documents SET s3_key = <twin> WHERE s3_key = <mislabel>``
   inside a per-county transaction. Before and after the UPDATE, count the
   per-county rows in ``derived.documents JOIN derived.courts`` and ABORT the
   transaction if the counts diverge — repointing must not drop or duplicate
   rows.

After this script runs and commits, ``cleanup_mislabeled_s3_2661.py``'s DB
safety check will pass and the mislabeled S3 objects can be deleted (issue
#2661).

Usage:
    scripts/ecs-run-task.sh scripts/repoint_mislabeled_documents_4439.py -- --dry-run
    scripts/ecs-run-task.sh scripts/repoint_mislabeled_documents_4439.py -- --apply
    scripts/ecs-run-task.sh scripts/repoint_mislabeled_documents_4439.py -- --apply --county santa_clara

Options:
    --dry-run   Enumerate mislabel/twin pairs and show planned repoints; do not write (default).
    --apply     Run the per-county UPDATEs after the row-count invariant check.
    --county    Restrict to a single county (e.g. santa_clara, orange).
    --state     State prefix to scan (default: ca).
    --bucket    S3 bucket to scan (default: $S3_BUCKET or judgemind-document-archive-dev).

Exit codes:
    0  Dry-run completed, or apply succeeded with all repoints committed.
    1  Apply aborted because the row-count invariant failed for at least one county.
    2  Unrecoverable error (S3, DB, or argument).

See: https://github.com/judgemind/judgemind/issues/4439
Blocks: https://github.com/judgemind/judgemind/issues/2661
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections import defaultdict
from collections.abc import Iterable

import boto3
import psycopg
from botocore.exceptions import ClientError

from framework.logging import configure_structlog

# Canonical stdout/CloudWatch logger pattern (#4368/#4373).  Routes
# stdlib ``logging.getLogger(__name__)`` calls through structlog's
# ProcessorFormatter + ExtraAdder so any ``extra=`` field reaches
# CloudWatch Logs Insights as a structured JSON field.
configure_structlog(json=True, stdlib_bridge=True)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BUCKET = os.environ.get("S3_BUCKET", "judgemind-document-archive-dev")

# Matches content-addressed flat-hash keys: ca/{county}/{court}/raw/{hex64}.{ext}.
# Captures county and the filename hash so the enumerate pass can group results
# and build the twin key without re-parsing.
KEY_PATTERN = re.compile(
    r"^(?P<state>[a-z]{2})/(?P<county>[^/]+)/(?P<court>[^/]+)/raw/(?P<hash>[0-9a-f]{64})\.(?P<ext>\w+)$"
)

# Sample size to log per county before truncating.
SAMPLE_SIZE = 20


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------


def parse_flat_hash_key(key: str) -> dict[str, str] | None:
    """Parse a flat-hash content-addressed key.

    Returns a dict with keys ``state``, ``county``, ``court``, ``hash``, ``ext``
    on match, or ``None`` if the key does not match the expected shape.

    Anything that does not match (e.g. date-partitioned legacy keys,
    court_directory snapshots, non-raw paths) is silently skipped.

    NOTE: this duplicates the helper in ``cleanup_mislabeled_s3_2661.py``
    deliberately. ECS oneshot scripts cannot import from other ``scripts/*.py``
    files (per CLAUDE.md / docs/agent/code-standards.md), so the small parser
    is copied rather than shared.
    """
    m = KEY_PATTERN.match(key)
    if m is None:
        return None
    return dict(m.groupdict())


def is_mislabel(filename_hash: str, metadata_hash: str | None) -> bool:
    """Return True if *filename_hash* differs from *metadata_hash*.

    A missing metadata hash (``None`` or empty) does **not** count as a
    mislabel — that is a separate problem class handled by
    ``audit_s3_raw_mislabels.py``.
    """
    if not metadata_hash:
        return False
    return filename_hash != metadata_hash


def build_twin_key(mislabel_key: str, metadata_hash: str) -> str | None:
    """Build the correctly-named twin key for *mislabel_key*.

    The twin key reuses the same ``state/county/court/raw/`` prefix and file
    extension but replaces the filename hash with *metadata_hash* — i.e. the
    location at which a correctly-named copy of the same bytes would live if
    one exists.

    Returns ``None`` if *mislabel_key* doesn't parse.
    """
    parsed = parse_flat_hash_key(mislabel_key)
    if parsed is None:
        return None
    return (
        f"{parsed['state']}/{parsed['county']}/{parsed['court']}/raw/"
        f"{metadata_hash}.{parsed['ext']}"
    )


# ---------------------------------------------------------------------------
# S3 enumeration
# ---------------------------------------------------------------------------


def head_object_metadata_hash(s3_client: object, bucket: str, key: str) -> str | None:
    """Return the metadata ``content-hash`` for *key*, or ``None`` if missing.

    Returns ``None`` for both a missing metadata field AND a 404 — callers
    must treat both as "not a mislabel" / "no twin available."
    """
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    metadata: dict[str, str] = head.get("Metadata", {}) or {}
    return metadata.get("content-hash") or None


def verify_twin(s3_client: object, bucket: str, twin_key: str) -> bool:
    """Return True if *twin_key* exists AND is correctly-labelled.

    A twin is "valid" only when its filename hex64 == its own metadata
    ``content-hash``. This is the safety guard that prevents repointing a
    DB row at *another* mislabeled key (which would just kick the can down
    the road).

    Returns False if the twin is missing (404), has no metadata, or is itself
    mislabeled.
    """
    parsed = parse_flat_hash_key(twin_key)
    if parsed is None:
        return False
    twin_metadata_hash = head_object_metadata_hash(s3_client, bucket, twin_key)
    if not twin_metadata_hash:
        return False
    return parsed["hash"] == twin_metadata_hash


def enumerate_repoint_pairs(
    s3_client: object,
    bucket: str,
    prefix: str,
) -> dict[str, list[dict[str, str]]]:
    """Enumerate (mislabel, twin) repoint candidates under *prefix*, grouped by county.

    For each key under *prefix* that matches ``KEY_PATTERN``:
      1. HEAD the object to read its metadata ``content-hash``.
      2. If filename hash != metadata hash, build the twin key
         (same prefix, filename = metadata hash).
      3. HEAD the twin and confirm filename == metadata on the twin.
      4. Record the (mislabel, twin) pair.

    Returns a dict mapping county name to a list of pair records, each a dict
    with keys ``mislabel_key``, ``twin_key``, ``filename_hash``,
    ``metadata_hash``, ``twin_status``.

    ``twin_status`` is one of:
      * ``"valid"``   — twin exists and is correctly-labelled (will repoint).
      * ``"missing"`` — twin does not exist in S3 (cannot repoint).
      * ``"invalid"`` — twin exists but is itself mislabeled (cannot repoint).

    The dry-run output is the source-of-truth for what this script will UPDATE
    when run with --apply.
    """
    paginator = s3_client.get_paginator("list_objects_v2")
    by_county: dict[str, list[dict[str, str]]] = defaultdict(list)
    total_seen = 0
    total_skipped_shape = 0

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            parsed = parse_flat_hash_key(key)
            if parsed is None:
                total_skipped_shape += 1
                continue
            total_seen += 1
            metadata_hash = head_object_metadata_hash(s3_client, bucket, key)
            if not is_mislabel(parsed["hash"], metadata_hash):
                continue
            assert metadata_hash is not None  # is_mislabel rejects None
            twin_key = build_twin_key(key, metadata_hash)
            assert twin_key is not None  # parsed already non-None
            twin_metadata_hash = head_object_metadata_hash(s3_client, bucket, twin_key)
            if twin_metadata_hash is None:
                twin_status = "missing"
            elif twin_metadata_hash != metadata_hash:
                # The twin exists but its metadata disagrees with the filename
                # we built it under — i.e. the twin is itself mislabeled.
                twin_status = "invalid"
            else:
                twin_status = "valid"
            by_county[parsed["county"]].append(
                {
                    "mislabel_key": key,
                    "twin_key": twin_key,
                    "filename_hash": parsed["hash"],
                    "metadata_hash": metadata_hash,
                    "twin_status": twin_status,
                }
            )

    logger.info(
        "Scanned prefix %r: %d flat-hash keys, %d skipped (non-matching shape)",
        prefix,
        total_seen,
        total_skipped_shape,
    )
    return dict(by_county)


# ---------------------------------------------------------------------------
# DB row-count invariant
# ---------------------------------------------------------------------------


def count_documents_for_county(conn: object, county: str) -> int:
    """Return the number of ``derived.documents`` rows in *county*.

    Used as the row-count invariant: pre-UPDATE count must equal post-UPDATE
    count. Otherwise the repoint dropped or duplicated rows.

    The SELECT joins ``derived.courts`` because ``derived.documents`` does not
    carry county directly — county lives on the joined courts row.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM derived.documents d
            JOIN derived.courts c ON d.court_id = c.id
            WHERE LOWER(c.county) = LOWER(%s)
            """,
            (county,),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# DB repoint
# ---------------------------------------------------------------------------


def repoint_pairs_for_county(
    conn: object,
    pairs: Iterable[dict[str, str]],
    *,
    county: str,
    dry_run: bool,
) -> dict[str, int]:
    """Apply (mislabel → twin) repoints for *county* in a single transaction.

    Filters *pairs* to those with ``twin_status == "valid"`` (the only ones
    safe to repoint) and runs one ``UPDATE`` per pair inside a single
    transaction so the per-county work is atomic. The pre/post row-count
    invariant runs inside the same transaction; a mismatch ABORTs the
    transaction (psycopg's ``with conn:`` block handles ROLLBACK on raise).

    In dry-run mode no DB writes are issued; the function returns the planned
    repoint count.

    Returns a dict with keys: ``planned``, ``updated``, ``skipped_invalid``,
    ``skipped_missing``, ``aborted`` (1/0).
    """
    pairs_list = list(pairs)
    valid = [p for p in pairs_list if p.get("twin_status") == "valid"]
    skipped_missing = sum(1 for p in pairs_list if p.get("twin_status") == "missing")
    skipped_invalid = sum(1 for p in pairs_list if p.get("twin_status") == "invalid")

    if not valid:
        logger.info(
            "[%s] No valid twins to repoint (planned=0, missing=%d, invalid=%d)",
            county,
            skipped_missing,
            skipped_invalid,
        )
        return {
            "planned": 0,
            "updated": 0,
            "skipped_invalid": skipped_invalid,
            "skipped_missing": skipped_missing,
            "aborted": 0,
        }

    if dry_run:
        logger.info(
            "[%s] DRY-RUN: would repoint %d row(s) (missing=%d, invalid=%d)",
            county,
            len(valid),
            skipped_missing,
            skipped_invalid,
        )
        for p in valid[:SAMPLE_SIZE]:
            logger.info(
                "  %s -> %s",
                p["mislabel_key"],
                p["twin_key"],
            )
        if len(valid) > SAMPLE_SIZE:
            logger.info("  ... and %d more", len(valid) - SAMPLE_SIZE)
        return {
            "planned": len(valid),
            "updated": 0,
            "skipped_invalid": skipped_invalid,
            "skipped_missing": skipped_missing,
            "aborted": 0,
        }

    pre_count = count_documents_for_county(conn, county)
    updated = 0
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                for p in valid:
                    cur.execute(
                        """
                        UPDATE derived.documents
                        SET s3_key = %s
                        WHERE s3_key = %s
                        """,
                        (p["twin_key"], p["mislabel_key"]),
                    )
                    updated += cur.rowcount or 0
            post_count = count_documents_for_county(conn, county)
            if post_count != pre_count:
                logger.error(
                    "[%s] ROW-COUNT INVARIANT VIOLATED: pre=%d post=%d "
                    "— transaction will be ABORTED",
                    county,
                    pre_count,
                    post_count,
                )
                raise RuntimeError(
                    f"Row-count invariant violated for {county}: "
                    f"pre={pre_count} post={post_count}"
                )
            logger.info(
                "[%s] Repointed %d row(s); pre/post row count invariant held at %d",
                county,
                updated,
                pre_count,
            )
    except RuntimeError:
        return {
            "planned": len(valid),
            "updated": 0,
            "skipped_invalid": skipped_invalid,
            "skipped_missing": skipped_missing,
            "aborted": 1,
        }

    return {
        "planned": len(valid),
        "updated": updated,
        "skipped_invalid": skipped_invalid,
        "skipped_missing": skipped_missing,
        "aborted": 0,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def report_enumeration(by_county: dict[str, list[dict[str, str]]]) -> dict[str, int]:
    """Log per-county counts; return summary totals (valid/missing/invalid)."""
    totals: dict[str, int] = defaultdict(int)
    for county in sorted(by_county.keys()):
        records = by_county[county]
        valid = sum(1 for r in records if r.get("twin_status") == "valid")
        missing = sum(1 for r in records if r.get("twin_status") == "missing")
        invalid = sum(1 for r in records if r.get("twin_status") == "invalid")
        totals["valid"] += valid
        totals["missing"] += missing
        totals["invalid"] += invalid
        totals["total"] += len(records)
        logger.info(
            "%s: %d mislabel(s) — %d valid twin(s), %d missing, %d invalid",
            county,
            len(records),
            valid,
            missing,
            invalid,
        )

    print()
    print("=" * 72)
    print("Mislabel/twin enumeration summary")
    print("=" * 72)
    print(
        f"  {'county':<24s} {'total':>8s} {'valid':>8s} {'missing':>8s} {'invalid':>8s}"
    )
    for county in sorted(by_county.keys()):
        records = by_county[county]
        valid = sum(1 for r in records if r.get("twin_status") == "valid")
        missing = sum(1 for r in records if r.get("twin_status") == "missing")
        invalid = sum(1 for r in records if r.get("twin_status") == "invalid")
        print(
            f"  {county:<24s} {len(records):>8d} {valid:>8d} {missing:>8d} {invalid:>8d}"
        )
    print(
        f"  {'TOTAL':<24s} {totals['total']:>8d} {totals['valid']:>8d} "
        f"{totals['missing']:>8d} {totals['invalid']:>8d}"
    )
    return dict(totals)


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def run_repoint(
    s3_client: object,
    db_conn: object,
    *,
    bucket: str,
    state: str,
    county: str | None,
    dry_run: bool,
) -> dict[str, int]:
    """Run the two-pass repoint. Returns a dict with summary counts.

    Summary keys: ``mislabels_found``, ``valid_twins``, ``missing_twins``,
    ``invalid_twins``, ``planned``, ``updated``, ``aborted_counties``.
    """
    if county:
        prefix = f"{state}/{county}/"
    else:
        prefix = f"{state}/"

    logger.info(
        "Mode: %s | Bucket: %s | Prefix: %s",
        "DRY-RUN" if dry_run else "APPLY",
        bucket,
        prefix,
    )

    # Pass 1: enumerate.
    by_county = enumerate_repoint_pairs(s3_client, bucket, prefix)
    enum_totals = report_enumeration(by_county)

    if enum_totals.get("total", 0) == 0:
        logger.info("No mislabels found. Nothing to do.")
        return {
            "mislabels_found": 0,
            "valid_twins": 0,
            "missing_twins": 0,
            "invalid_twins": 0,
            "planned": 0,
            "updated": 0,
            "aborted_counties": 0,
        }

    # Pass 2: repoint per county.
    total_planned = 0
    total_updated = 0
    aborted_counties = 0
    for county_name in sorted(by_county.keys()):
        result = repoint_pairs_for_county(
            db_conn,
            by_county[county_name],
            county=county_name,
            dry_run=dry_run,
        )
        total_planned += result["planned"]
        total_updated += result["updated"]
        aborted_counties += result["aborted"]

    logger.info(
        "Done. planned=%d, updated=%d, aborted_counties=%d",
        total_planned,
        total_updated,
        aborted_counties,
    )
    return {
        "mislabels_found": enum_totals.get("total", 0),
        "valid_twins": enum_totals.get("valid", 0),
        "missing_twins": enum_totals.get("missing", 0),
        "invalid_twins": enum_totals.get("invalid", 0),
        "planned": total_planned,
        "updated": total_updated,
        "aborted_counties": aborted_counties,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Repoint derived.documents rows from mislabeled flat-hash S3 "
            "keys to their correctly-named twins. Two-pass: enumerate "
            "(mislabel, twin) pairs, then UPDATE per-county within a "
            "transaction guarded by a row-count invariant."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Enumerate pairs and show planned repoints; do not write (default).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Run the per-county UPDATEs after the row-count invariant check.",
    )
    parser.add_argument(
        "--county",
        default=None,
        help="Restrict to a single county prefix (e.g. santa_clara, orange).",
    )
    parser.add_argument(
        "--state",
        default="ca",
        help="State prefix to scan (default: ca).",
    )
    parser.add_argument(
        "--bucket",
        default=DEFAULT_BUCKET,
        help=f"S3 bucket to scan (default: ${{S3_BUCKET}} or {DEFAULT_BUCKET}).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # --apply overrides --dry-run.
    dry_run = not args.apply

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        return 2

    s3_client = boto3.client("s3")

    with psycopg.connect(dsn) as conn:
        result = run_repoint(
            s3_client,
            conn,
            bucket=args.bucket,
            state=args.state,
            county=args.county,
            dry_run=dry_run,
        )

    return 1 if result["aborted_counties"] else 0


if __name__ == "__main__":
    sys.exit(main())
