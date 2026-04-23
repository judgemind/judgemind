#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""S3 orphan-rate regression guard for California counties.

An S3 object is an "orphan" when its key matches the flat-hash scheme
(ca/{county}/{court}/raw/{sha256}.{ext}) but no row in derived.documents
references that s3_key.

This script samples N flat-hash keys from S3 for a given county, checks each
against derived.documents, and computes the orphan rate. It exits 1 if the
rate meets or exceeds --threshold.

Usage (via ECS — needs DATABASE_URL and S3 credentials):
    scripts/ecs-run-task.sh scripts/spotcheck/check_s3_orphan_rate.py -- \\
        --county "Santa Clara" --n 100

    scripts/ecs-run-task.sh scripts/spotcheck/check_s3_orphan_rate.py -- \\
        --county "Orange" --n 100 --threshold 0.10

Output: one-line JSON to stdout:
    {"county": "santa_clara", "total_flat_hash": 5832, "sampled": 100,
     "orphan_count": 4, "orphan_rate": 0.04, "threshold": 0.05, "passed": true}

Exit code: 0 if orphan_rate < threshold, 1 if orphan_rate >= threshold.

See: https://github.com/judgemind/judgemind/issues/2629
"""

from __future__ import annotations

import argparse
import json as json_mod
import logging
import os
import random
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

#: Matches correctly-labeled flat-hash keys only.
#: Rejects: date-partitioned (raw/YYYY/MM/DD/…), LLM-cache, judge-assignments,
#: directory-snapshots, and any other prefix variant.
FLAT_HASH_RE = re.compile(r"^ca/[^/]+/[^/]+/raw/[0-9a-f]{64}\.(pdf|html|docx|txt)$")


# ---------------------------------------------------------------------------
# Pure helpers (testable without boto3/psycopg)
# ---------------------------------------------------------------------------


def county_prefix(county: str) -> str:
    """Return the S3 prefix for *county*.

    Normalises the county name by lowercasing and replacing spaces with
    underscores, matching the convention used by storage.py and sample.py.

    Examples:
        >>> county_prefix("Santa Clara")
        'ca/santa_clara/'
        >>> county_prefix("Orange")
        'ca/orange/'
    """
    return f"ca/{county.lower().replace(' ', '_')}/"


def is_flat_hash(key: str) -> bool:
    """Return True iff *key* matches the flat-hash S3 keyspace.

    Rejects date-partitioned keys (raw/YYYY/MM/DD/…) and any other
    non-flat-hash prefix variants.
    """
    return FLAT_HASH_RE.match(key) is not None


# ---------------------------------------------------------------------------
# S3 listing
# ---------------------------------------------------------------------------


def list_flat_hash_keys(s3: object, prefix: str) -> list[str]:
    """Paginate list_objects_v2 under *prefix* and return all flat-hash keys.

    Args:
        s3: boto3 S3 client.
        prefix: S3 prefix to list (e.g. "ca/santa_clara/").

    Returns:
        List of S3 keys that match FLAT_HASH_RE.
    """
    paginator = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key: str = obj["Key"]
            if is_flat_hash(key):
                keys.append(key)
    return keys


# ---------------------------------------------------------------------------
# DB cross-reference
# ---------------------------------------------------------------------------


def count_orphans(conn: object, sampled_keys: list[str]) -> int:
    """Return the number of keys in *sampled_keys* absent from derived.documents.

    Issues a single O(n) query using ANY(%s) instead of N individual lookups.

    Args:
        conn: psycopg connection.
        sampled_keys: List of S3 keys to check.

    Returns:
        Count of keys not found in derived.documents.s3_key.
    """
    if not sampled_keys:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s3_key FROM derived.documents WHERE s3_key = ANY(%s)",
            (sampled_keys,),
        )
        rows = cur.fetchall()
    found_keys = {row[0] for row in rows if row[0]}
    return sum(1 for k in sampled_keys if k not in found_keys)


# ---------------------------------------------------------------------------
# Main guard logic
# ---------------------------------------------------------------------------


def run_check(
    s3: object,
    conn: object,
    county: str,
    n: int,
    threshold: float,
) -> dict:
    """Run the orphan-rate check for *county*.

    Returns a result dict:
      {
        "county": str,          # snake_case county name
        "total_flat_hash": int, # total flat-hash keys found in S3
        "sampled": int,         # number of keys sampled
        "orphan_count": int,    # keys absent from derived.documents
        "orphan_rate": float,   # orphan_count / sampled  (0.0 if sampled == 0)
        "threshold": float,     # --threshold value
        "passed": bool,         # True iff orphan_rate < threshold
      }
    """
    county_snake = county.lower().replace(" ", "_")
    prefix = county_prefix(county)

    logger.info("Listing flat-hash keys under prefix=%s", prefix)
    all_flat_hash_keys = list_flat_hash_keys(s3, prefix)
    total_flat_hash = len(all_flat_hash_keys)
    logger.info("Found %d flat-hash keys", total_flat_hash)

    sample_size = min(n, total_flat_hash)
    sampled_keys = (
        random.sample(all_flat_hash_keys, sample_size) if sample_size > 0 else []
    )
    logger.info("Sampled %d keys for DB cross-reference", sample_size)

    orphan_count = count_orphans(conn, sampled_keys)
    orphan_rate = orphan_count / sample_size if sample_size > 0 else 0.0
    passed = orphan_rate < threshold

    logger.info(
        "county=%s sampled=%d orphans=%d rate=%.4f threshold=%.4f passed=%s",
        county_snake,
        sample_size,
        orphan_count,
        orphan_rate,
        threshold,
        passed,
    )

    return {
        "county": county_snake,
        "total_flat_hash": total_flat_hash,
        "sampled": sample_size,
        "orphan_count": orphan_count,
        "orphan_rate": orphan_rate,
        "threshold": threshold,
        "passed": passed,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "S3 orphan-rate regression guard. "
            "Samples N flat-hash S3 keys for a county and cross-references "
            "against derived.documents. Exits 1 if orphan rate >= threshold."
        ),
    )
    parser.add_argument(
        "--county",
        required=True,
        help='County name (e.g. "Santa Clara"). Case-insensitive.',
    )
    parser.add_argument(
        "--n",
        type=int,
        default=100,
        help="Number of flat-hash keys to sample (default: 100).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.05,
        help="Orphan-rate threshold for failure (default: 0.05).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON (default: True; always emits JSON).",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    logger.info(
        "Bucket: %s | County: %s | N: %d | Threshold: %.4f",
        BUCKET,
        args.county,
        args.n,
        args.threshold,
    )

    s3 = boto3.client("s3")

    with psycopg.connect(dsn) as conn:
        result = run_check(
            s3,
            conn,
            county=args.county,
            n=args.n,
            threshold=args.threshold,
        )

    print(json_mod.dumps(result))

    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
