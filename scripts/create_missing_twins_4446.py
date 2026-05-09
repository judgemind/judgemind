#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Create correctly-labelled twin S3 objects for missing-twin mislabels.

The 2026-03-28 migration in commit ``fbf8e38`` (archived as
``scripts/archive/migrate_s3_keys.py``) wrote some S3 objects under
content-addressed keys whose filename SHA-256 did **not** match the SHA-256
of the bytes — for documents whose ``content_hash`` was a synthetic
split-child value rather than a real bytes hash. ``CopyObject`` preserved
the original metadata ``content-hash`` (the correct bytes hash), so each
mislabeled object carries a metadata header that points to the right value.

The companion script ``repoint_mislabeled_documents_4439.py`` repoints
``derived.documents`` rows from mislabel keys to their correctly-named
twins — but only when a valid twin already exists. Across orange,
riverside, and santa_clara, ~970 mislabeled keys have **no** correctly-named
twin in S3 today (per the 2026-05-09 dry-run of the repoint script). Those
rows cannot be repointed by the surgical UPDATE path because there is
nothing valid to point them at.

This script (Path A from issue #4446) materialises the missing twins by
``CopyObject``-ing each missing-twin mislabel to its correctly-named twin
location. Because ``copy_object`` defaults to ``MetadataDirective: COPY``,
the source metadata ``content-hash`` is preserved verbatim on the new
object — so the resulting twin satisfies the invariant
``filename hex64 == metadata content-hash`` by construction.

After this script runs and ``repoint_mislabeled_documents_4439.py --apply``
finishes the corresponding DB repoints, every ``derived.documents`` row
points at a correctly-named content-addressed key, the cleanup script
``cleanup_mislabeled_s3_2661.py`` can complete its DB-safety check, and
the mislabeled S3 objects can be deleted (issue #2661).

This script does **two passes**:

1. **Enumerate pass:** paginate ``list_objects_v2`` under ``ca/`` (or a
   single county prefix), match keys of shape
   ``ca/{county}/{court}/raw/<hex64>.<ext>``, HEAD each, compare filename
   hex64 to metadata ``content-hash``. For each mislabel, build the
   ``twin`` key (same prefix, filename = metadata hash) and HEAD the
   twin to determine ``twin_status`` ∈ {``valid``, ``missing``,
   ``invalid``}.

2. **Create pass:** in --apply mode, for each (mislabel, twin) pair with
   ``twin_status == "missing"``:
     - Pre-HEAD the twin one more time (handles the race where another
       writer materialised the twin between enumerate and create).
     - If the twin now exists and is correctly-labelled, count
       ``already_exists`` and skip.
     - If the twin now exists but is itself mislabeled (collision with a
       sibling mislabel), count ``collision_skipped``, log an error, and
       skip — operator must resolve.
     - Otherwise call ``s3.copy_object(Bucket=bucket,
       CopySource={"Bucket": bucket, "Key": mislabel}, Key=twin)``. The
       default ``MetadataDirective: COPY`` preserves the source metadata
       ``content-hash`` on the new object.
     - Post-HEAD the new twin and confirm filename hex64 == metadata
       ``content-hash``. If verification fails, count ``verify_failed``
       and log an error (do NOT raise; we still want the per-county
       summary to be reported).

Idempotence: the pre-HEAD-then-decide flow makes the script safe to
re-run. A second --apply pass over the same prefix counts every twin
created by the first pass under ``already_exists`` and is otherwise a
no-op. Errors and verification failures are recorded as counters and do
not abort the per-county loop.

This script does **not** touch the DB. The companion repoint script
handles the DB side once the twins are created. ``DATABASE_URL`` is not
required.

Usage:
    scripts/ecs-run-task.sh scripts/create_missing_twins_4446.py -- --dry-run
    scripts/ecs-run-task.sh scripts/create_missing_twins_4446.py -- --apply
    scripts/ecs-run-task.sh scripts/create_missing_twins_4446.py -- --apply --county santa_clara

Options:
    --dry-run   Enumerate missing-twin mislabels and show planned
                CopyObject calls; do not write to S3 (default).
    --apply     Run CopyObject calls for every missing-twin mislabel.
    --county    Restrict to a single county (e.g. santa_clara, orange).
    --state     State prefix to scan (default: ca).
    --bucket    S3 bucket to scan (default: $S3_BUCKET or
                judgemind-document-archive-dev).

Exit codes:
    0  Dry-run completed, or apply succeeded with errors == 0 AND
       verify_failed == 0.
    1  Apply completed but at least one error or verification failure
       was recorded (operator must investigate the affected keys).
    2  Unrecoverable error (S3 client construction, argument).

See: https://github.com/judgemind/judgemind/issues/4446
Blocks: https://github.com/judgemind/judgemind/issues/2661
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from collections.abc import Iterable

import boto3
from botocore.exceptions import ClientError

from framework.logging import configure_structlog
from framework.s3_keys import (
    build_twin_key,
    head_object_metadata_hash,
    is_mislabel,
    parse_flat_hash_key,
)

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

# Sample size to log per county before truncating.
SAMPLE_SIZE = 20


# ---------------------------------------------------------------------------
# S3 enumeration
# ---------------------------------------------------------------------------
#
# ``parse_flat_hash_key``, ``is_mislabel``, ``head_object_metadata_hash``,
# and ``build_twin_key`` (and the underlying ``KEY_PATTERN`` regex) are
# imported from ``framework.s3_keys`` (see #4447). The ``framework.*``
# imports are reachable inside the ECS oneshot container because the
# helpers are bundled into the scraper-framework Docker image — same
# precedent as the ``framework.logging`` import above. ``verify_twin``
# stays inline as a thin wrapper that composes the framework helpers
# (#4455).


def verify_twin(s3_client: object, bucket: str, twin_key: str) -> bool:
    """Return True if *twin_key* exists AND is correctly-labelled.

    A twin is "valid" only when its filename hex64 == its own metadata
    ``content-hash``. This is the safety guard that prevents repointing /
    creating against another mislabeled key.

    Returns False if the twin is missing (404), has no metadata, or is
    itself mislabeled.
    """
    parsed = parse_flat_hash_key(twin_key)
    if parsed is None:
        return False
    twin_metadata_hash = head_object_metadata_hash(s3_client, bucket, twin_key)
    if not twin_metadata_hash:
        return False
    return parsed["hash"] == twin_metadata_hash


def enumerate_mislabel_pairs(
    s3_client: object,
    bucket: str,
    prefix: str,
) -> dict[str, list[dict[str, str]]]:
    """Enumerate (mislabel, twin) candidates under *prefix*, grouped by county.

    For each key under *prefix* that matches ``KEY_PATTERN``:
      1. HEAD the object to read its metadata ``content-hash``.
      2. If filename hash != metadata hash, build the twin key
         (same prefix, filename = metadata hash).
      3. HEAD the twin and compare its filename to its metadata hash to
         determine ``twin_status``.
      4. Record the pair.

    Returns a dict mapping county name to a list of pair records, each a
    dict with keys ``mislabel_key``, ``twin_key``, ``filename_hash``,
    ``metadata_hash``, ``twin_status``.

    ``twin_status`` is one of:
      * ``"valid"``   — twin exists and is correctly-labelled (separate
        repoint script handles these; this script no-ops on them).
      * ``"missing"`` — twin does not exist in S3 (this is the work
        unit — Pass 2 ``CopyObject``s these to the twin location).
      * ``"invalid"`` — twin exists but is itself mislabeled (operator
        intervention required; this script logs and skips).

    The dry-run output is the source-of-truth for what this script will
    ``CopyObject`` when run with --apply.
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
# S3 twin creation
# ---------------------------------------------------------------------------


def create_twin(
    s3_client: object,
    bucket: str,
    pair: dict[str, str],
) -> str:
    """Create the correctly-named twin for *pair* and verify it.

    *pair* must have keys ``mislabel_key``, ``twin_key``,
    ``metadata_hash``, ``twin_status``. Only ``twin_status == "missing"``
    pairs are processed; all other statuses return a status code without
    side effects so the caller's counters stay aligned with the
    enumeration.

    Returns one of these status strings:
      * ``"created"``           — pre-HEAD confirmed twin missing, ``copy_object``
                                  called, post-HEAD verified the new twin is
                                  correctly-labelled.
      * ``"already_exists"``    — pre-HEAD found the twin already exists and
                                  is correctly-labelled (idempotent re-run, or
                                  another writer materialised the twin between
                                  enumerate and create). No ``copy_object``
                                  call.
      * ``"collision_skipped"`` — pre-HEAD found the twin exists but is itself
                                  mislabeled (filename hash != metadata hash).
                                  Operator intervention required; no
                                  ``copy_object`` call.
      * ``"verify_failed"``     — ``copy_object`` succeeded but post-HEAD
                                  showed the new twin is NOT correctly-labelled.
                                  Logs an error; does not raise.
      * ``"error"``             — ``copy_object`` raised a non-404 ``ClientError``,
                                  or the pre-HEAD raised a non-404 ``ClientError``.
                                  Logs the error; does not raise.
      * ``"skipped_non_missing"``— pair's ``twin_status`` was ``valid`` or
                                  ``invalid`` at enumeration time. No work
                                  performed (separate code paths handle these).

    The function never raises on S3 errors; it logs them and returns
    ``"error"`` so the caller's per-county loop can keep running.
    """
    twin_status_at_enum = pair.get("twin_status")
    if twin_status_at_enum != "missing":
        # Defensive: the caller already filters to missing, but gate here too
        # so the pure helper contract is unambiguous.
        return "skipped_non_missing"

    mislabel_key = pair["mislabel_key"]
    twin_key = pair["twin_key"]
    expected_metadata_hash = pair["metadata_hash"]

    # Pre-HEAD the twin: handle the case where another writer materialised
    # the twin between enumerate and create (idempotence on re-runs, race
    # safety on concurrent writers).
    try:
        pre_metadata_hash = head_object_metadata_hash(s3_client, bucket, twin_key)
    except ClientError as exc:
        logger.error(
            "create_twin: pre-HEAD failed for twin %s (mislabel %s): %s",
            twin_key,
            mislabel_key,
            exc,
        )
        return "error"

    if pre_metadata_hash is not None:
        # Twin already exists. Decide based on whether its filename matches
        # its own metadata hash.
        twin_parsed = parse_flat_hash_key(twin_key)
        assert twin_parsed is not None  # build_twin_key always produces a parseable key
        if twin_parsed["hash"] == pre_metadata_hash:
            logger.info(
                "create_twin: twin %s already exists and is correctly-labelled "
                "(idempotent skip; mislabel %s)",
                twin_key,
                mislabel_key,
            )
            return "already_exists"
        logger.error(
            "create_twin: twin %s exists but is itself mislabeled "
            "(filename=%s metadata=%s; mislabel=%s) — operator must resolve",
            twin_key,
            twin_parsed["hash"],
            pre_metadata_hash,
            mislabel_key,
        )
        return "collision_skipped"

    # Twin is missing — copy from mislabel.
    try:
        s3_client.copy_object(
            Bucket=bucket,
            CopySource={"Bucket": bucket, "Key": mislabel_key},
            Key=twin_key,
        )
    except ClientError as exc:
        logger.error(
            "create_twin: copy_object failed for %s -> %s: %s",
            mislabel_key,
            twin_key,
            exc,
        )
        return "error"

    # Post-HEAD verification: confirm the new twin is correctly-labelled.
    try:
        post_metadata_hash = head_object_metadata_hash(s3_client, bucket, twin_key)
    except ClientError as exc:
        logger.error(
            "create_twin: post-HEAD failed for new twin %s (mislabel %s): %s",
            twin_key,
            mislabel_key,
            exc,
        )
        return "error"

    twin_parsed = parse_flat_hash_key(twin_key)
    assert twin_parsed is not None
    if (
        post_metadata_hash != expected_metadata_hash
        or post_metadata_hash != twin_parsed["hash"]
    ):
        logger.error(
            "create_twin: post-HEAD verification FAILED for new twin %s "
            "(filename=%s expected_metadata=%s observed_metadata=%s; mislabel=%s)",
            twin_key,
            twin_parsed["hash"],
            expected_metadata_hash,
            post_metadata_hash,
            mislabel_key,
        )
        return "verify_failed"

    logger.info(
        "create_twin: created twin %s from mislabel %s (verified)",
        twin_key,
        mislabel_key,
    )
    return "created"


def create_twins_for_county(
    s3_client: object,
    pairs: Iterable[dict[str, str]],
    *,
    county: str,
    bucket: str,
    dry_run: bool,
) -> dict[str, int]:
    """Create twins for the missing-twin pairs in *pairs*.

    Filters to ``twin_status == "missing"`` and accumulates per-status
    counters. Logs a per-county summary at the end. Returns a dict with
    keys: ``planned``, ``created``, ``already_exists``,
    ``collision_skipped``, ``verify_failed``, ``errors``,
    ``skipped_invalid``, ``skipped_valid``.
    """
    pairs_list = list(pairs)
    missing = [p for p in pairs_list if p.get("twin_status") == "missing"]
    skipped_valid = sum(1 for p in pairs_list if p.get("twin_status") == "valid")
    skipped_invalid = sum(1 for p in pairs_list if p.get("twin_status") == "invalid")

    counters = {
        "planned": len(missing),
        "created": 0,
        "already_exists": 0,
        "collision_skipped": 0,
        "verify_failed": 0,
        "errors": 0,
        "skipped_invalid": skipped_invalid,
        "skipped_valid": skipped_valid,
    }

    if not missing:
        logger.info(
            "[%s] No missing-twin mislabels (planned=0, valid=%d, invalid=%d)",
            county,
            skipped_valid,
            skipped_invalid,
        )
        return counters

    if dry_run:
        logger.info(
            "[%s] DRY-RUN: would create %d twin(s) (valid=%d, invalid=%d)",
            county,
            len(missing),
            skipped_valid,
            skipped_invalid,
        )
        for p in missing[:SAMPLE_SIZE]:
            logger.info(
                "  %s -> %s",
                p["mislabel_key"],
                p["twin_key"],
            )
        if len(missing) > SAMPLE_SIZE:
            logger.info("  ... and %d more", len(missing) - SAMPLE_SIZE)
        return counters

    for p in missing:
        result = create_twin(s3_client, bucket, p)
        if result == "created":
            counters["created"] += 1
        elif result == "already_exists":
            counters["already_exists"] += 1
        elif result == "collision_skipped":
            counters["collision_skipped"] += 1
        elif result == "verify_failed":
            counters["verify_failed"] += 1
        elif result == "error":
            counters["errors"] += 1
        # "skipped_non_missing" is an internal-only return; the filter
        # above means it should never fire in practice.

    logger.info(
        "[%s] Done. created=%d, already_exists=%d, collision_skipped=%d, "
        "verify_failed=%d, errors=%d (planned=%d)",
        county,
        counters["created"],
        counters["already_exists"],
        counters["collision_skipped"],
        counters["verify_failed"],
        counters["errors"],
        counters["planned"],
    )
    return counters


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
    print("Mislabel enumeration summary (Path A target = `missing` column)")
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


def report_creation(
    by_county_counters: dict[str, dict[str, int]],
) -> dict[str, int]:
    """Log per-county creation counters; return aggregate totals."""
    totals: dict[str, int] = defaultdict(int)
    for county in sorted(by_county_counters.keys()):
        counters = by_county_counters[county]
        for k, v in counters.items():
            totals[k] += v

    print()
    print("=" * 96)
    print("Twin creation summary")
    print("=" * 96)
    print(
        f"  {'county':<24s} {'planned':>8s} {'created':>8s} {'exists':>8s} "
        f"{'collide':>8s} {'verify!':>8s} {'errors':>8s}"
    )
    for county in sorted(by_county_counters.keys()):
        c = by_county_counters[county]
        print(
            f"  {county:<24s} {c['planned']:>8d} {c['created']:>8d} "
            f"{c['already_exists']:>8d} {c['collision_skipped']:>8d} "
            f"{c['verify_failed']:>8d} {c['errors']:>8d}"
        )
    print(
        f"  {'TOTAL':<24s} {totals['planned']:>8d} {totals['created']:>8d} "
        f"{totals['already_exists']:>8d} {totals['collision_skipped']:>8d} "
        f"{totals['verify_failed']:>8d} {totals['errors']:>8d}"
    )
    return dict(totals)


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def run_create(
    s3_client: object,
    *,
    bucket: str,
    state: str,
    county: str | None,
    dry_run: bool,
) -> dict[str, int]:
    """Run the two-pass create. Returns a dict with summary counts.

    Summary keys: ``mislabels_found``, ``valid_twins``, ``missing_twins``,
    ``invalid_twins``, ``planned``, ``created``, ``already_exists``,
    ``collision_skipped``, ``verify_failed``, ``errors``.
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
    by_county = enumerate_mislabel_pairs(s3_client, bucket, prefix)
    enum_totals = report_enumeration(by_county)

    if enum_totals.get("total", 0) == 0:
        logger.info("No mislabels found. Nothing to do.")
        return {
            "mislabels_found": 0,
            "valid_twins": 0,
            "missing_twins": 0,
            "invalid_twins": 0,
            "planned": 0,
            "created": 0,
            "already_exists": 0,
            "collision_skipped": 0,
            "verify_failed": 0,
            "errors": 0,
        }

    # Pass 2: create per county.
    by_county_counters: dict[str, dict[str, int]] = {}
    for county_name in sorted(by_county.keys()):
        by_county_counters[county_name] = create_twins_for_county(
            s3_client,
            by_county[county_name],
            county=county_name,
            bucket=bucket,
            dry_run=dry_run,
        )

    create_totals = report_creation(by_county_counters)

    return {
        "mislabels_found": enum_totals.get("total", 0),
        "valid_twins": enum_totals.get("valid", 0),
        "missing_twins": enum_totals.get("missing", 0),
        "invalid_twins": enum_totals.get("invalid", 0),
        "planned": create_totals.get("planned", 0),
        "created": create_totals.get("created", 0),
        "already_exists": create_totals.get("already_exists", 0),
        "collision_skipped": create_totals.get("collision_skipped", 0),
        "verify_failed": create_totals.get("verify_failed", 0),
        "errors": create_totals.get("errors", 0),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create correctly-labelled twin S3 objects for missing-twin "
            "mislabels (Path A from issue #4446). Two-pass: enumerate "
            "(mislabel, twin) pairs, then CopyObject the missing twins "
            "into existence. CopyObject's default MetadataDirective: COPY "
            "preserves the source metadata content-hash, so the new twin "
            "is correctly-labelled by construction."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Enumerate pairs and show planned creates; do not write (default).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Run the per-county CopyObject calls.",
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

    s3_client = boto3.client("s3")

    result = run_create(
        s3_client,
        bucket=args.bucket,
        state=args.state,
        county=args.county,
        dry_run=dry_run,
    )

    # Apply mode: surface verify failures and errors as exit code 1 so the
    # operator can detect partial success without parsing logs.
    if not dry_run and (result.get("errors", 0) or result.get("verify_failed", 0)):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
