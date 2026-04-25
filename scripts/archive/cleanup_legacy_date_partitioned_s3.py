#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Delete legacy date-partitioned S3 keys for Santa Clara and Orange counties.

Old S3 key scheme: ca/{county}/{court}/raw/{YYYY}/{MM}/{DD}/{document_id}.{ext}
New S3 key scheme: ca/{county}/{court}/raw/{content_hash}.{ext}

This script removes the leftover date-partitioned objects under
s3://judgemind-document-archive-dev/ for Santa Clara and Orange. Contra Costa
has been verified to have 0 such keys and is excluded from the default scope.

Before deleting, a DB safety check confirms no derived.documents rows still
reference date-partitioned s3_key values — if any remain, the script aborts.

Usage:
    scripts/ecs-run-task.sh scripts/cleanup_legacy_date_partitioned_s3.py -- --dry-run
    scripts/ecs-run-task.sh scripts/cleanup_legacy_date_partitioned_s3.py -- --apply
    scripts/ecs-run-task.sh scripts/cleanup_legacy_date_partitioned_s3.py -- --apply --county santa_clara

Options:
    --dry-run   List keys that would be deleted, grouped by county (default).
    --apply     Actually delete the date-partitioned S3 objects.
    --county    Restrict to a single county (santa_clara or orange).

See: https://github.com/judgemind/judgemind/issues/2627
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys

import boto3
import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUCKET = os.environ.get("S3_BUCKET", "judgemind-document-archive-dev")

# Matches date-partitioned keys: ca/{state}/{court}/raw/YYYY/MM/DD/...
DATE_PARTITIONED_RE = re.compile(r"^ca/[^/]+/[^/]+/raw/\d{4}/\d{2}/\d{2}/")

# Counties in scope for this cleanup. Contra Costa has 0 date-partitioned keys.
COUNTY_PREFIXES: dict[str, str] = {
    "santa_clara": "ca/santa_clara/",
    "orange": "ca/orange/",
}

# DB query — count documents rows still using date-partitioned s3_keys.
DB_SAFETY_QUERY = (
    "SELECT COUNT(*) FROM derived.documents "
    "WHERE s3_key ~ '^ca/.+/.+/raw/[0-9]{4}/[0-9]{2}/[0-9]{2}/'"
)


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def list_date_partitioned_keys(s3: object, prefix: str) -> list[str]:
    """Return all S3 keys under *prefix* that match DATE_PARTITIONED_RE.

    Uses list_objects_v2 paginator so it handles >1000 objects correctly.
    """
    paginator = s3.get_paginator("list_objects_v2")
    matched: list[str] = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if DATE_PARTITIONED_RE.match(key):
                matched.append(key)
    return matched


def db_safety_check(conn: object) -> int:
    """Return count of derived.documents rows still referencing date-partitioned keys.

    If the count is >0, the caller should abort — those rows must be migrated
    before the corresponding S3 objects can be removed.
    """
    with conn.cursor() as cur:
        cur.execute(DB_SAFETY_QUERY)
        row = cur.fetchone()
    return row[0] if row else 0


def delete_in_batches(s3: object, keys: list[str], *, dry_run: bool) -> int:
    """Delete *keys* from S3 in batches of 1000 (the delete_objects maximum).

    In dry-run mode, no deletions are performed and 0 is returned.
    Returns the number of objects deleted.
    """
    if dry_run or not keys:
        return 0

    deleted = 0
    total = len(keys)
    for i in range(0, total, 1000):
        batch = keys[i : i + 1000]
        s3.delete_objects(
            Bucket=BUCKET,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
        deleted += len(batch)
        logger.info("Deleted %d/%d objects...", deleted, total)

    return deleted


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Delete legacy date-partitioned S3 keys for Santa Clara and Orange. "
            "Runs a DB safety check before any deletion."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="List keys that would be deleted, grouped by county (default).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete the date-partitioned S3 objects.",
    )
    parser.add_argument(
        "--county",
        choices=list(COUNTY_PREFIXES.keys()),
        default=None,
        help="Restrict to a single county (santa_clara or orange).",
    )
    args = parser.parse_args()

    # --apply overrides --dry-run
    dry_run = not args.apply

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    logger.info("Mode: %s | Bucket: %s", "DRY-RUN" if dry_run else "APPLY", BUCKET)

    s3 = boto3.client("s3")

    # Determine which counties to scan.
    if args.county:
        counties = {args.county: COUNTY_PREFIXES[args.county]}
    else:
        counties = COUNTY_PREFIXES

    # Step 1: List date-partitioned keys per county.
    all_keys: list[str] = []
    county_keys: dict[str, list[str]] = {}
    for county, prefix in counties.items():
        keys = list_date_partitioned_keys(s3, prefix)
        county_keys[county] = keys
        all_keys.extend(keys)
        sample = keys[:20]
        logger.info(
            "%s: %d date-partitioned keys found",
            county,
            len(keys),
        )
        for k in sample:
            logger.info("  %s", k)
        if len(keys) > 20:
            logger.info("  ... and %d more", len(keys) - 20)

    print(f"Santa Clara: {len(county_keys.get('santa_clara', []))} keys")
    print(f"Orange: {len(county_keys.get('orange', []))} keys")
    print(f"Total: {len(all_keys)} date-partitioned keys")

    if not all_keys:
        logger.info("No date-partitioned keys found. Nothing to do.")
        return

    # Step 2: DB safety check.
    with psycopg.connect(dsn) as conn:
        db_count = db_safety_check(conn)

    if db_count > 0:
        logger.error(
            "ABORT: %d derived.documents rows still reference date-partitioned s3_keys. "
            "Run the s3_key migration before cleanup.",
            db_count,
        )
        sys.exit(1)

    logger.info("DB safety check passed: 0 rows reference date-partitioned keys.")

    # Step 3: Delete (or report in dry-run).
    if dry_run:
        logger.info(
            "DRY-RUN: would delete %d objects. Re-run with --apply to proceed.",
            len(all_keys),
        )
        return

    deleted = delete_in_batches(s3, all_keys, dry_run=False)
    logger.info("Done. Deleted %d date-partitioned S3 objects.", deleted)


if __name__ == "__main__":
    main()
