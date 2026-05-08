#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Audit S3 raw/ objects for content-hash mislabels.

Walks ``{state}/{county}/{court}/raw/`` prefixes in the S3 document archive,
calls ``verify_key_matches_bytes()`` for each object, and reports any keys
where the filename hash, stored metadata content-hash, and actual bytes SHA-256
do not agree.

Usage:
    scripts/with-secret.sh \\
        -e S3_BUCKET=judgemind/dev/s3/document-archive:.bucket \\
        -- packages/scraper-framework/.venv/bin/python3 \\
        scripts/audit_s3_raw_mislabels.py

Options:
    --county NAME   Limit scan to a specific county prefix (e.g. orange).
    --sample N      Check at most N objects per county prefix.
    --output PATH   Write JSON report to PATH in addition to stdout.
    --bucket NAME   S3 bucket to scan (defaults to S3_BUCKET env var or
                    'judgemind-document-archive-dev').
    --state NAME    Limit scan to a specific state prefix (default: ca).

Exit code: 0 if no mismatches found, 1 if mismatches detected, 2 on error.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import boto3

from framework.logging import configure_structlog
from framework.s3_integrity import S3MislabelError, verify_key_matches_bytes

# Use ``configure_structlog`` so any ``extra=`` fields passed to logger calls
# surface in CloudWatch Logs Insights output. ``stdlib_bridge=True`` routes
# stdlib ``logging.getLogger(__name__)`` calls through structlog's
# ProcessorFormatter + ExtraAdder, JSON-encoding the LogRecord plus its
# extras as one event per line. The previous ``logging.basicConfig`` format
# string silently dropped every ``extra=`` field — see #4368 / #4373.
configure_structlog(json=True, stdlib_bridge=True)
logger = logging.getLogger(__name__)

DEFAULT_BUCKET = os.environ.get("S3_BUCKET", "judgemind-document-archive-dev")


# ---------------------------------------------------------------------------
# Core audit logic
# ---------------------------------------------------------------------------


def _list_raw_keys(
    s3_client: object,
    bucket: str,
    prefix: str,
    sample: int | None,
) -> list[str]:
    """Return all keys under *prefix* matching the raw/ path segment.

    If *sample* is set, stop after collecting that many keys.
    """
    paginator = s3_client.get_paginator("list_objects_v2")
    keys: list[str] = []

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # Only audit raw/ paths — skip non-raw objects (court_directory,
            # ballotpedia, llm_extractor cache, etc.)
            if "/raw/" not in key:
                continue
            keys.append(key)
            if sample is not None and len(keys) >= sample:
                return keys

    return keys


def audit_county(
    s3_client: object,
    bucket: str,
    county_prefix: str,
    sample: int | None,
) -> dict:
    """Audit all raw/ objects under *county_prefix*.

    Returns a dict with keys: prefix, total_checked, mislabels, errors.
    ``mislabels`` is a list of dicts with keys: key, error.
    ``errors``   is a list of dicts with keys: key, error (unexpected errors).
    """
    logger.info("Scanning prefix s3://%s/%s", bucket, county_prefix)
    keys = _list_raw_keys(s3_client, bucket, county_prefix, sample)
    logger.info("  Found %d raw/ keys to check", len(keys))

    mislabels: list[dict] = []
    errors: list[dict] = []

    for key in keys:
        try:
            result = verify_key_matches_bytes(s3_client, bucket, key)
            if not result:
                # Key does not exist (404) — race between list and verify
                logger.warning("  Key disappeared during scan: %s", key)
        except S3MislabelError as exc:
            logger.warning("  MISLABEL: %s — %s", key, exc)
            mislabels.append({"key": key, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.error("  ERROR checking %s: %s", key, exc)
            errors.append({"key": key, "error": str(exc)})

    return {
        "prefix": county_prefix,
        "total_checked": len(keys),
        "mislabels": mislabels,
        "errors": errors,
    }


def run_audit(
    s3_client: object,
    bucket: str,
    *,
    state: str = "ca",
    county: str | None = None,
    sample: int | None = None,
) -> dict:
    """Run the S3 mislabel audit.

    Returns a dict with:
        all_clean       bool
        total_checked   int
        total_mislabels int
        counties        list[dict]  per-county results
    """
    # Enumerate county prefixes to scan
    if county:
        # Single county: construct the prefix directly
        county_key = county.lower().replace(" ", "_")
        prefixes = [f"{state}/{county_key}/"]
    else:
        # Discover county prefixes from the bucket listing
        paginator = s3_client.get_paginator("list_objects_v2")
        prefixes = []
        for page in paginator.paginate(
            Bucket=bucket, Prefix=f"{state}/", Delimiter="/"
        ):
            for cp in page.get("CommonPrefixes", []):
                prefixes.append(cp["Prefix"])

    logger.info("Auditing %d county prefix(es) in s3://%s", len(prefixes), bucket)

    county_results = []
    total_checked = 0
    total_mislabels = 0

    for prefix in sorted(prefixes):
        result = audit_county(s3_client, bucket, prefix, sample)
        county_results.append(result)
        total_checked += result["total_checked"]
        total_mislabels += len(result["mislabels"])

    return {
        "all_clean": total_mislabels == 0,
        "total_checked": total_checked,
        "total_mislabels": total_mislabels,
        "counties": county_results,
    }


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def _print_report(result: dict) -> None:
    """Print a human-readable summary of the audit to stdout."""
    print("S3 Raw/ Mislabel Audit")
    print("=" * 60)
    print(f"Total objects checked: {result['total_checked']}")
    print(f"Total mislabels found: {result['total_mislabels']}")
    print()

    for county in result["counties"]:
        n_mislabels = len(county["mislabels"])
        n_errors = len(county["errors"])
        status = "OK" if n_mislabels == 0 else "MISLABELS"
        print(
            f"  {county['prefix']:<50s}  "
            f"checked={county['total_checked']:>6d}  "
            f"mislabels={n_mislabels:>4d}  "
            f"errors={n_errors:>4d}  "
            f"[{status}]"
        )
        if n_mislabels > 0:
            for entry in county["mislabels"][:5]:
                print(f"    MISLABEL: {entry['key']}")
                print(f"             {entry['error'][:120]}")
            if n_mislabels > 5:
                print(f"    ... and {n_mislabels - 5} more")

    print()
    if result["all_clean"]:
        print("All objects passed integrity checks.")
    else:
        print(
            f"WARNING: {result['total_mislabels']} mislabeled object(s) detected. "
            f"See above for details."
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit S3 raw/ objects for content-hash mislabels.",
    )
    parser.add_argument(
        "--bucket",
        default=DEFAULT_BUCKET,
        help="S3 bucket to scan (default: S3_BUCKET env var or judgemind-document-archive-dev).",
    )
    parser.add_argument(
        "--state",
        default="ca",
        help="State prefix to scan (default: ca).",
    )
    parser.add_argument(
        "--county",
        default=None,
        help="Limit scan to a specific county prefix (e.g. orange).",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        metavar="N",
        help="Check at most N objects per county prefix.",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="Write JSON report to PATH.",
    )
    args = parser.parse_args()

    s3_client = boto3.client("s3")

    try:
        result = run_audit(
            s3_client,
            args.bucket,
            state=args.state,
            county=args.county,
            sample=args.sample,
        )
    except Exception as exc:
        logger.error("Audit failed: %s", exc)
        sys.exit(2)

    _print_report(result)

    if args.output:
        output_path = args.output
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        logger.info("JSON report written to %s", output_path)

    sys.exit(0 if result["all_clean"] else 1)


if __name__ == "__main__":
    main()
