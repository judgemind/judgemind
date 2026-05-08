#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Audit correctly-labeled S3 orphans for Santa Clara and Orange counties.

An S3 object is a "correctly-labeled orphan" when:
  - Its key matches the flat-hash scheme: ca/{county}/{court}/raw/{sha256}.{ext}
  - The filename SHA-256 matches the actual object bytes (correctly labeled)
  - No row in derived.documents references that s3_key

This is distinct from "mislabeled" objects (filename hash ≠ bytes hash, produced
by the 2026-03-28 migration bug in #2638) and from objects that ARE in the DB.

Correctly-labeled orphans arise when S3 archival succeeds but the downstream
document.captured Redis Stream event is lost before the ingestion worker
processes it — leaving the raw object in S3 with no DB record.

Usage:
    scripts/ecs-run-task.sh scripts/audit_correctly_labeled_s3_orphans.py -- \\
        --county both --json
    scripts/ecs-run-task.sh scripts/audit_correctly_labeled_s3_orphans.py -- \\
        --county santa_clara --limit 500 --sample-head-objects 10

Options:
    --county    santa_clara | orange | both  (default: both)
    --limit N   Cap the total number of S3 keys examined (default: unlimited).
    --json      Machine-readable JSON output.
    --sample-head-objects N
                For suspected orphans, HEAD-object up to N of them and
                confirm the byte-hash matches the filename hash (default: 20).

Exit code: 0 if no orphans found, 1 if orphans detected.

See: https://github.com/judgemind/judgemind/issues/2662
"""

from __future__ import annotations

import argparse
import hashlib
import json as json_mod
import logging
import os
import random
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterator

import boto3
import psycopg

# Use ``configure_structlog`` so any ``extra=`` fields passed to logger calls
# surface in CloudWatch Logs Insights output. ``stdlib_bridge=True`` routes
# stdlib ``logging.getLogger(__name__)`` calls through structlog's
# ProcessorFormatter + ExtraAdder, JSON-encoding the LogRecord plus its
# extras as one event per line. The previous ``logging.basicConfig`` format
# string (``"%(asctime)s %(levelname)-8s %(message)s"``) silently dropped
# every ``extra=`` field — see #4368 for the post-deploy verification
# incident that motivated migrating every ``scripts/*.py`` to this pattern
# (#4373).
from framework.logging import configure_structlog  # noqa: E402

configure_structlog(json=True, stdlib_bridge=True)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUCKET = os.environ.get("S3_BUCKET", "judgemind-document-archive-dev")

#: Matches correctly-labeled flat-hash keys only.
#: Rejects: date-partitioned (raw/YYYY/MM/DD/…), LLM-cache, judge-assignments,
#: directory-snapshots, and any other prefix variant.
FLAT_HASH_RE = re.compile(r"^ca/[^/]+/[^/]+/raw/[0-9a-f]{64}\.(pdf|html|docx|txt)$")

COUNTY_PREFIXES: dict[str, str] = {
    "santa_clara": "ca/santa_clara/",
    "orange": "ca/orange/",
}

# Classification result literals
PRESENT_IN_DB = "present_in_db"
PRESENT_IN_DB_VIA_CONTENT_HASH = "present_in_db_via_content_hash"
MISLABELED = "mislabeled"
CORRECTLY_LABELED_ORPHAN = "correctly_labeled_orphan"

# ---------------------------------------------------------------------------
# S3 listing
# ---------------------------------------------------------------------------


def list_raw_flat_hash_keys(
    s3: object,
    prefix: str,
    *,
    limit: int | None = None,
) -> Iterator[tuple[str, datetime, int]]:
    """Paginate list_objects_v2 under *prefix* and yield flat-hash key tuples.

    Yields (key, last_modified, content_length) for each object whose key
    matches FLAT_HASH_RE.  Handles >1000 objects via the paginator.

    Args:
        s3: boto3 S3 client.
        prefix: S3 prefix to list (e.g. "ca/santa_clara/").
        limit: Stop after yielding this many objects (for testing / --limit).
    """
    paginator = s3.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key: str = obj["Key"]
            if FLAT_HASH_RE.match(key):
                yield key, obj["LastModified"], obj["Size"]
                count += 1
                if limit is not None and count >= limit:
                    return


# ---------------------------------------------------------------------------
# DB fetch
# ---------------------------------------------------------------------------


def fetch_db_s3_keys(conn: object, county: str) -> set[str]:
    """Return the set of s3_key values from derived.documents for *county*.

    Queries: SELECT s3_key FROM derived.documents WHERE s3_key LIKE 'ca/<county>/%'
    """
    pattern = f"ca/{county}/%"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s3_key FROM derived.documents WHERE s3_key LIKE %s",
            (pattern,),
        )
        rows = cur.fetchall()
    return {row[0] for row in rows if row[0]}


def fetch_db_content_hashes(conn: object, county: str) -> set[str]:
    """Return the set of content_hash values from derived.documents for *county*.

    Queries: SELECT content_hash FROM derived.documents WHERE s3_key LIKE 'ca/<county>/%'

    Filters by s3_key prefix (not content_hash) to avoid conflating cross-county
    collisions. NULLs are excluded.
    """
    pattern = f"ca/{county}/%"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content_hash FROM derived.documents WHERE s3_key LIKE %s",
            (pattern,),
        )
        rows = cur.fetchall()
    return {row[0] for row in rows if row[0]}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(
    key: str,
    last_modified: datetime,
    s3: object,
    db_keys: set[str],
    db_content_hashes: set[str],
    *,
    verify_bytes: bool = False,
) -> str:
    """Classify one S3 object as present_in_db, present_in_db_via_content_hash, mislabeled, or correctly_labeled_orphan.

    Classification logic:
    1. If `key` is in db_keys → PRESENT_IN_DB (normal, healthy record).
    2. Extract filename_hash from key. If filename_hash is in db_content_hashes →
       PRESENT_IN_DB_VIA_CONTENT_HASH (dedup loser: the content exists in DB under
       a different s3_key; the content-addressed naming contract guarantees the
       filename hash equals the bytes hash, so set membership is sufficient).
    3. If verify_bytes=False (bulk pass): an object absent from both sets is
       classified CORRECTLY_LABELED_ORPHAN.
    4. If verify_bytes=True: HEAD the object, download its bytes, and SHA-256
       them.  If the bytes hash does not match the filename hash → MISLABELED
       (migration artifact, handled by #2638).  If they match → CORRECTLY_LABELED_ORPHAN.

    In practice the mislabeled class is bounded and already audited by #2638.
    The bulk pass (verify_bytes=False) is safe because any mislabeled object
    that also appears in db_keys would be caught in step 1 (present_in_db),
    and mislabeled objects absent from db_keys are already known artifacts —
    the audit's goal is to count *new* correctly-labeled orphans.

    Args:
        key: S3 key to classify.
        last_modified: LastModified timestamp from list_objects_v2.
        s3: boto3 S3 client.
        db_keys: Set of s3_key values from derived.documents for this county.
        db_content_hashes: Set of content_hash values from derived.documents for
            this county. Used to detect dedup-loser keys whose content is already
            present in the DB under a different s3_key.
        verify_bytes: If True, download the object and verify byte hash.
    """
    if key in db_keys:
        return PRESENT_IN_DB

    # Extract the expected hash from the filename (content-addressed naming contract).
    filename = key.rsplit("/", 1)[-1]
    filename_hash = filename.split(".")[0]

    if filename_hash in db_content_hashes:
        return PRESENT_IN_DB_VIA_CONTENT_HASH

    if not verify_bytes:
        return CORRECTLY_LABELED_ORPHAN

    try:
        response = s3.get_object(Bucket=BUCKET, Key=key)
        body = response["Body"].read()
        actual_hash = hashlib.sha256(body).hexdigest()
    except Exception as exc:
        logger.warning("Failed to GET %s for byte-hash verification: %s", key, exc)
        # Cannot verify; treat as correctly labeled to avoid false mislabels.
        return CORRECTLY_LABELED_ORPHAN

    if actual_hash != filename_hash:
        return MISLABELED

    return CORRECTLY_LABELED_ORPHAN


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------


def _month_key(dt: datetime) -> str:
    """Return YYYY-MM string for a LastModified datetime."""
    utc_dt = (
        dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    )
    return utc_dt.strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Main audit logic
# ---------------------------------------------------------------------------


def run_audit(
    s3: object,
    conn: object,
    counties: dict[str, str],
    *,
    limit: int | None,
    sample_head_objects: int,
    output_json: bool,
) -> dict:
    """Run the S3 orphan audit for the given counties.

    Returns a summary dict:
      {
        "all_clean": bool,
        "counties": {
          "<county>": {
            "total": int,
            "in_db": int,
            "orphans": int,
            "mislabeled": int,
            "orphans_by_month": {"YYYY-MM": int, ...},
            "sample_verified": [
              {"key": str, "last_modified": str, "classification": str}, ...
            ]
          }, ...
        }
      }
    """
    result: dict = {"all_clean": True, "counties": {}}

    for county, prefix in counties.items():
        logger.info("Scanning county=%s prefix=%s", county, prefix)

        # Fetch DB keys and content hashes for this county.
        db_keys = fetch_db_s3_keys(conn, county)
        logger.info("  DB keys for %s: %d", county, len(db_keys))
        db_content_hashes = fetch_db_content_hashes(conn, county)
        logger.info("  DB content hashes for %s: %d", county, len(db_content_hashes))

        # Accumulate per-classification lists.
        in_db_count = 0
        in_db_via_content_hash_count = 0
        orphan_keys: list[tuple[str, datetime, int]] = []
        mislabeled_count = 0
        orphans_by_month: dict[str, int] = defaultdict(int)

        for key, last_modified, size in list_raw_flat_hash_keys(
            s3, prefix, limit=limit
        ):
            classification = classify(
                key, last_modified, s3, db_keys, db_content_hashes, verify_bytes=False
            )
            if classification == PRESENT_IN_DB:
                in_db_count += 1
            elif classification == PRESENT_IN_DB_VIA_CONTENT_HASH:
                in_db_via_content_hash_count += 1
            elif classification == MISLABELED:
                mislabeled_count += 1
            else:
                orphan_keys.append((key, last_modified, size))
                orphans_by_month[_month_key(last_modified)] += 1

        orphan_count = len(orphan_keys)
        total = (
            in_db_count + in_db_via_content_hash_count + orphan_count + mislabeled_count
        )

        logger.info(
            "  %s: total=%d in_db=%d in_db_via_content_hash=%d orphans=%d mislabeled=%d",
            county,
            total,
            in_db_count,
            in_db_via_content_hash_count,
            orphan_count,
            mislabeled_count,
        )

        # Sample up to N orphans for byte-hash verification.
        sample_size = min(sample_head_objects, orphan_count)
        sample_pool = random.sample(orphan_keys, sample_size) if sample_size > 0 else []
        sample_verified: list[dict] = []
        for s_key, s_lm, _s_size in sample_pool:
            verified_cls = classify(
                s_key, s_lm, s3, db_keys, db_content_hashes, verify_bytes=True
            )
            sample_verified.append(
                {
                    "key": s_key,
                    "last_modified": s_lm.isoformat(),
                    "classification": verified_cls,
                }
            )
            logger.info(
                "  Sample HEAD-verify %s → %s",
                s_key[-40:],
                verified_cls,
            )

        county_summary = {
            "total": total,
            "in_db": in_db_count,
            "in_db_via_content_hash": in_db_via_content_hash_count,
            "orphans": orphan_count,
            "mislabeled": mislabeled_count,
            "orphans_by_month": dict(sorted(orphans_by_month.items())),
            "sample_verified": sample_verified,
        }
        result["counties"][county] = county_summary

        if orphan_count > 0:
            result["all_clean"] = False

    return result


def _print_report(result: dict) -> None:
    """Print a human-readable audit report to stdout."""
    print("Correctly-Labeled S3 Orphan Audit")
    print("=" * 60)
    for county, stats in result["counties"].items():
        print(f"\n{county.upper()}")
        print(f"  Total flat-hash keys scanned:                  {stats['total']}")
        print(f"  Present in DB:                                 {stats['in_db']}")
        print(
            f"  Present in DB (via content_hash dedup):        {stats['in_db_via_content_hash']}"
        )
        print(f"  Correctly-labeled orphans:                     {stats['orphans']}")
        print(f"  Mislabeled (migration bug):                    {stats['mislabeled']}")
        if stats["orphans_by_month"]:
            print("  Orphans by month:")
            for month, cnt in sorted(stats["orphans_by_month"].items()):
                print(f"    {month}: {cnt}")
        if stats["sample_verified"]:
            print("  Sample byte-hash verification:")
            for sv in stats["sample_verified"]:
                print(f"    {sv['key'][-50:]}  → {sv['classification']}")
    print()
    if result["all_clean"]:
        print("All flat-hash S3 objects have matching DB records.")
    else:
        total_orphans = sum(c["orphans"] for c in result["counties"].values())
        print(f"{total_orphans} correctly-labeled orphan(s) found — see above.")
        print(
            "Recovery: scripts/ecs-run-task.sh scripts/rebuild_db.py -- --county <name>"
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Audit correctly-labeled S3 orphans for Santa Clara and Orange counties. "
            "Orphans are flat-hash objects in S3 with no matching derived.documents row."
        ),
    )
    parser.add_argument(
        "--county",
        choices=["santa_clara", "orange", "both"],
        default="both",
        help="County to audit (default: both).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap total S3 keys examined per county (for testing).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable JSON output.",
    )
    parser.add_argument(
        "--sample-head-objects",
        type=int,
        default=20,
        dest="sample_head_objects",
        help="Number of orphans to HEAD-verify byte hash for (default: 20).",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    logger.info("Bucket: %s | County: %s | Limit: %s", BUCKET, args.county, args.limit)

    s3 = boto3.client("s3")

    if args.county == "both":
        counties = COUNTY_PREFIXES
    else:
        counties = {args.county: COUNTY_PREFIXES[args.county]}

    with psycopg.connect(dsn) as conn:
        result = run_audit(
            s3,
            conn,
            counties,
            limit=args.limit,
            sample_head_objects=args.sample_head_objects,
            output_json=args.json,
        )

    if args.json:
        print(json_mod.dumps(result, indent=2, default=str))
    else:
        _print_report(result)

    sys.exit(0 if result["all_clean"] else 1)


if __name__ == "__main__":
    main()
