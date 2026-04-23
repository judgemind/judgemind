#!/usr/bin/env python3
# venv: scraper-framework
"""Spotcheck sampler — randomly sample entities for quality review.

Produces a manifest JSON listing entity IDs for subsequent fetching
and review. Designed to be extensible: new entity types are added by
implementing a sampler function and registering it in SAMPLERS.

Usage (rulings — requires DB access, run via ECS):
    scripts/ecs-run-task.sh scripts/spotcheck/sample.py -- \\
        --from rulings --county "Los Angeles" --n 10

Usage (originals — S3 only, can run locally):
    scripts/run-py.sh scripts/spotcheck/sample.py \\
        --from originals --county "Los Angeles" --n 10

Output: manifest JSON to stdout (or to --output path).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any


# ---------------------------------------------------------------------------
# Sampler registry — add new entity types here
# ---------------------------------------------------------------------------


def _sample_rulings(county: str, n: int) -> list[str]:
    """Sample random ruling IDs from a county via direct DB query.

    Requires DATABASE_URL environment variable (available on ECS).
    """
    import psycopg

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print(
            "ERROR: DATABASE_URL not set. Run this via ECS for DB-backed entities:\n"
            '  scripts/ecs-run-task.sh scripts/spotcheck/sample.py -- --from rulings --county "Los Angeles" --n 10',
            file=sys.stderr,
        )
        sys.exit(1)

    query = """
        SELECT r.id::text
        FROM rulings r
        JOIN courts c ON r.court_id = c.id
        WHERE c.county = %s
        ORDER BY random()
        LIMIT %s
    """
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (county, n))
            return [row[0] for row in cur.fetchall()]


def _sample_originals(county: str, n: int) -> list[str]:
    """Sample random original document IDs from a county via S3 listing.

    Lists S3 keys under the county prefix and randomly samples from them.
    Can run locally (only needs AWS credentials, not DB access).
    """
    import random

    import boto3

    s3 = boto3.client("s3")
    bucket = os.environ.get("S3_BUCKET", "judgemind-document-archive-dev")

    # Build the S3 prefix matching storage.py's build_s3_key convention:
    # {state_lower}/{county_lower_underscored}/
    prefix = f"ca/{county.lower().replace(' ', '_')}/"

    paginator = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])

    if not keys:
        print(
            f"WARNING: No S3 objects found under s3://{bucket}/{prefix}",
            file=sys.stderr,
        )
        return []

    sample_size = min(n, len(keys))
    sampled = random.sample(keys, sample_size)
    return sampled


# Registry mapping entity type names to sampler functions.
# Each function signature: (county: str, n: int) -> list[str]
SAMPLERS: dict[str, Any] = {
    "rulings": _sample_rulings,
    "originals": _sample_originals,
}


# ---------------------------------------------------------------------------
# Manifest generation
# ---------------------------------------------------------------------------


def build_manifest(entity: str, county: str, ids: list[str]) -> dict[str, Any]:
    """Build a manifest dict from sampled IDs."""
    return {
        "entity": entity,
        "county": county,
        "sampled_at": datetime.now(UTC).isoformat(),
        "ids": ids,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sample entities for spotcheck review",
    )
    parser.add_argument(
        "--from",
        dest="entity",
        required=True,
        choices=sorted(SAMPLERS.keys()),
        help="Entity type to sample",
    )
    parser.add_argument(
        "--county",
        required=True,
        help="County name to filter (e.g. 'Los Angeles', 'Orange')",
    )
    parser.add_argument(
        "--n",
        type=int,
        default=10,
        help="Number of samples (default: 10)",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Write manifest to file instead of stdout",
    )
    args = parser.parse_args()

    sampler_fn = SAMPLERS[args.entity]
    ids = sampler_fn(args.county, args.n)

    if not ids:
        print(
            f"ERROR: No {args.entity} found for county '{args.county}'",
            file=sys.stderr,
        )
        sys.exit(1)

    manifest = build_manifest(args.entity, args.county, ids)
    manifest_json = json.dumps(manifest, indent=2)

    if args.output:
        from pathlib import Path

        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(manifest_json + "\n", encoding="utf-8")
        print(f"Manifest written to {out_path}", file=sys.stderr)
    else:
        print(manifest_json)


if __name__ == "__main__":
    main()
