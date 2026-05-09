#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Delete mislabeled flat-hash S3 objects from the 2026-03-28 migration.

The 2026-03-28 migration in commit ``fbf8e38`` (archived as
``scripts/archive/migrate_s3_keys.py``) wrote S3 objects under content-addressed
keys whose filename SHA-256 did **not** match the SHA-256 of the bytes — for
documents whose ``content_hash`` was a synthetic split-child value rather than
a real bytes hash. ``CopyObject`` preserved the original metadata
``content-hash`` (the correct bytes hash), so each mislabeled object carries a
metadata header that points to the right value.

After ``rebuild_db.py`` re-pointed ``derived.documents`` rows to correctly-named
content-addressed keys (#2630), the mislabeled copies are pure detritus and
safe to delete — provided no DB rows still reference them.

This script does **two passes**:

1. **Enumerate pass:** paginate ``list_objects_v2`` under ``ca/``, match keys of
   shape ``ca/{county}/{court}/raw/<hex64>.<ext>``, HEAD each, and compare the
   filename hex64 to metadata ``content-hash``. Records any mismatches with
   their filename hash, metadata hash, and county.

2. **Delete pass:** in --apply mode, runs a DB safety check
   (``SELECT s3_key FROM derived.documents WHERE s3_key = ANY(...)``) against
   the enumerated mislabels. **Aborts** if any DB row still references a
   mislabeled key — those rows must be repointed (re-run ``rebuild_db.py``)
   before the corresponding S3 objects can be removed. Otherwise batch-deletes
   the mislabels via ``s3.delete_objects`` (1000/batch).

Usage:
    scripts/ecs-run-task.sh scripts/cleanup_mislabeled_s3_2661.py -- --dry-run
    scripts/ecs-run-task.sh scripts/cleanup_mislabeled_s3_2661.py -- --apply
    scripts/ecs-run-task.sh scripts/cleanup_mislabeled_s3_2661.py -- --apply --county santa_clara

Options:
    --dry-run   List mislabels grouped by county; do not delete (default).
    --apply     Run DB safety check and, if it passes, delete the mislabels.
    --county    Restrict to a single county (e.g. santa_clara, orange).
    --state     State prefix to scan (default: ca).
    --bucket    S3 bucket to scan (default: $S3_BUCKET or judgemind-document-archive-dev).

Exit codes:
    0  Dry-run completed, or apply succeeded with all deletes.
    1  Apply aborted because DB rows still reference mislabels.
    2  Unrecoverable error (S3, DB, or argument).

See: https://github.com/judgemind/judgemind/issues/2661
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BUCKET = os.environ.get("S3_BUCKET", "judgemind-document-archive-dev")

# Matches content-addressed flat-hash keys: ca/{county}/{court}/raw/{hex64}.{ext}
# Captures county and the filename hash for downstream grouping/comparison.
KEY_PATTERN = re.compile(
    r"^(?P<state>[a-z]{2})/(?P<county>[^/]+)/(?P<court>[^/]+)/raw/(?P<hash>[0-9a-f]{64})\.(?P<ext>\w+)$"
)

# Sample size to log per county before truncating.
SAMPLE_SIZE = 20

# delete_objects S3 API caps at 1000 keys per request.
DELETE_BATCH_SIZE = 1000


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------


def parse_flat_hash_key(key: str) -> dict[str, str] | None:
    """Parse a flat-hash content-addressed key.

    Returns a dict with keys ``state``, ``county``, ``court``, ``hash``, ``ext``
    on match, or ``None`` if the key does not match the expected shape.

    The function is the source of truth for what counts as a "flat-hash" key
    in this script — anything that does not match (e.g. date-partitioned legacy
    keys, court_directory snapshots, non-raw paths) is silently skipped.

    Examples::

        parse_flat_hash_key("ca/orange/superior_court/raw/aabb...64.pdf")
        # -> {"state": "ca", "county": "orange", "court": "superior_court",
        #     "hash": "aabb...64", "ext": "pdf"}

        parse_flat_hash_key("ca/orange/superior_court/raw/2026/04/01/x.pdf")
        # -> None
    """
    m = KEY_PATTERN.match(key)
    if m is None:
        return None
    return dict(m.groupdict())


def is_mislabel(filename_hash: str, metadata_hash: str | None) -> bool:
    """Return True if *filename_hash* differs from *metadata_hash*.

    The mislabel signature from the 2026-03-28 migration is:
    filename hex64 ≠ metadata ``content-hash``. Both must be hex64 strings (we
    do not normalise here — the caller is expected to have read the metadata
    via ``HeadObject`` which preserves the original casing).

    A missing metadata hash (``None`` or empty) does **not** count as a
    mislabel — the audit script (``audit_s3_raw_mislabels.py``) treats missing
    metadata as a separate problem class and is the right place to surface it.
    This script is narrowly scoped to "filename ≠ metadata" (issue #2661).
    """
    if not metadata_hash:
        return False
    return filename_hash != metadata_hash


# ---------------------------------------------------------------------------
# S3 enumeration
# ---------------------------------------------------------------------------


def head_object_metadata_hash(s3_client: object, bucket: str, key: str) -> str | None:
    """Return the metadata ``content-hash`` for *key*, or ``None`` if missing.

    Returns ``None`` for both a missing metadata field AND a 404 — callers are
    expected to handle both cases as "not a mislabel" (the audit script is
    the right place to surface those edge cases).
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


def enumerate_mislabels(
    s3_client: object,
    bucket: str,
    prefix: str,
) -> dict[str, list[dict[str, str]]]:
    """Enumerate mislabeled flat-hash keys under *prefix*, grouped by county.

    For each key under *prefix* that matches ``KEY_PATTERN``:
      1. HEAD the object to read its metadata ``content-hash``.
      2. If the metadata hash differs from the filename hash, record it.

    Returns a dict mapping county name to a list of mislabel records, each
    record being a dict with keys ``key``, ``filename_hash``, ``metadata_hash``.

    Keys with missing metadata or 404s are silently skipped — they are a
    separate problem class (see ``audit_s3_raw_mislabels.py``). The dry-run
    output is the source-of-truth for what this script will delete.
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
            if is_mislabel(parsed["hash"], metadata_hash):
                by_county[parsed["county"]].append(
                    {
                        "key": key,
                        "filename_hash": parsed["hash"],
                        "metadata_hash": metadata_hash or "",
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
# DB safety check
# ---------------------------------------------------------------------------


def find_referenced_keys(conn: object, keys: Iterable[str]) -> list[str]:
    """Return the subset of *keys* that are still referenced by ``derived.documents``.

    Uses ``s3_key = ANY(%s)`` so PostgreSQL plans the lookup against the
    existing index on ``s3_key`` (no full scan) regardless of how large *keys*
    is. The caller should treat a non-empty result as a hard ABORT signal —
    those rows must be re-pointed (run ``rebuild_db.py``) before the
    corresponding S3 objects can be deleted.
    """
    keys_list = list(keys)
    if not keys_list:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s3_key FROM derived.documents WHERE s3_key = ANY(%s)",
            (keys_list,),
        )
        rows = cur.fetchall()
    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# S3 deletion
# ---------------------------------------------------------------------------


def delete_in_batches(
    s3_client: object, bucket: str, keys: list[str], *, dry_run: bool
) -> int:
    """Delete *keys* in batches of ``DELETE_BATCH_SIZE``.

    In dry-run mode, no deletions are performed and 0 is returned.
    Returns the number of objects deleted.
    """
    if dry_run or not keys:
        return 0

    deleted = 0
    total = len(keys)
    for i in range(0, total, DELETE_BATCH_SIZE):
        batch = keys[i : i + DELETE_BATCH_SIZE]
        s3_client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
        deleted += len(batch)
        logger.info("Deleted %d/%d objects...", deleted, total)
    return deleted


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def report_enumeration(by_county: dict[str, list[dict[str, str]]]) -> int:
    """Log per-county counts + first SAMPLE_SIZE keys; return total count."""
    total = 0
    for county in sorted(by_county.keys()):
        records = by_county[county]
        total += len(records)
        logger.info("%s: %d mislabeled key(s)", county, len(records))
        for record in records[:SAMPLE_SIZE]:
            logger.info(
                "  %s (filename=%s metadata=%s)",
                record["key"],
                record["filename_hash"][:16] + "...",
                record["metadata_hash"][:16] + "..."
                if record["metadata_hash"]
                else "<missing>",
            )
        if len(records) > SAMPLE_SIZE:
            logger.info("  ... and %d more", len(records) - SAMPLE_SIZE)

    print()
    print("=" * 60)
    print("Mislabel enumeration summary")
    print("=" * 60)
    for county in sorted(by_county.keys()):
        print(f"  {county:<24s} {len(by_county[county]):>6d}")
    print(f"  {'TOTAL':<24s} {total:>6d}")
    return total


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def run_cleanup(
    s3_client: object,
    db_conn: object,
    *,
    bucket: str,
    state: str,
    county: str | None,
    dry_run: bool,
) -> dict[str, int]:
    """Run the two-pass cleanup. Returns a dict with summary counts.

    Summary keys: ``mislabels_found``, ``referenced_count``, ``deleted``,
    ``aborted`` (1/0).
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
    by_county = enumerate_mislabels(s3_client, bucket, prefix)
    total_mislabels = report_enumeration(by_county)

    if total_mislabels == 0:
        logger.info("No mislabels found. Nothing to do.")
        return {
            "mislabels_found": 0,
            "referenced_count": 0,
            "deleted": 0,
            "aborted": 0,
        }

    # Flatten the keys for the DB and S3 calls.
    all_keys: list[str] = [
        record["key"] for records in by_county.values() for record in records
    ]

    # Pass 2a: DB safety check.
    referenced = find_referenced_keys(db_conn, all_keys)
    print()
    print(
        f"DB safety check: {len(referenced)} mislabeled key(s) still referenced "
        f"by derived.documents"
    )
    for ref_key in referenced[:SAMPLE_SIZE]:
        print(f"  REFERENCED: {ref_key}")
    if len(referenced) > SAMPLE_SIZE:
        print(f"  ... and {len(referenced) - SAMPLE_SIZE} more")

    if referenced:
        logger.error(
            "ABORT: %d mislabeled S3 key(s) are still referenced by "
            "derived.documents rows. Run rebuild_db.py against the affected "
            "counties to repoint rows to correctly-named content-addressed "
            "keys, then re-run this script.",
            len(referenced),
        )
        return {
            "mislabels_found": total_mislabels,
            "referenced_count": len(referenced),
            "deleted": 0,
            "aborted": 1,
        }

    logger.info(
        "DB safety check passed: 0 mislabels are DB-referenced. Safe to delete %d object(s).",
        total_mislabels,
    )

    # Pass 2b: delete (or simulate in dry-run).
    if dry_run:
        logger.info(
            "DRY-RUN: would delete %d object(s). Re-run with --apply to proceed.",
            total_mislabels,
        )
        return {
            "mislabels_found": total_mislabels,
            "referenced_count": 0,
            "deleted": 0,
            "aborted": 0,
        }

    deleted = delete_in_batches(s3_client, bucket, all_keys, dry_run=False)
    logger.info("Done. Deleted %d mislabeled S3 object(s).", deleted)
    return {
        "mislabels_found": total_mislabels,
        "referenced_count": 0,
        "deleted": deleted,
        "aborted": 0,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Delete mislabeled flat-hash S3 objects from the 2026-03-28 migration. "
            "Two-pass: enumerate, then delete with DB safety check."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="List mislabels grouped by county; do not delete (default).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Run DB safety check and, if it passes, delete the mislabels.",
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
        result = run_cleanup(
            s3_client,
            conn,
            bucket=args.bucket,
            state=args.state,
            county=args.county,
            dry_run=dry_run,
        )

    return 1 if result["aborted"] else 0


if __name__ == "__main__":
    sys.exit(main())
