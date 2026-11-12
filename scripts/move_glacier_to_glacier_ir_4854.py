#!/usr/bin/env python3
"""Move archive objects stuck in GLACIER to GLACIER_IR so GetObject can read them (#4854).

Until #4854 the archive bucket's ``tiered-storage`` lifecycle rule moved
current objects to GLACIER (Flexible Retrieval) at 365 days and noncurrent
versions at 90 days. GetObject cannot read a GLACIER object until a restore
request finishes; it fails with ``InvalidObjectState``. The rule now targets
GLACIER_IR (Instant Retrieval), but lifecycle cannot move an object from
GLACIER to GLACIER_IR. This script does it once:

1. ``--restore`` asks S3 for a temporary readable copy of each target version
   (Bulk tier by default, the cheapest; it finishes within about 12 hours).
   ``--tier Expedited`` finishes in minutes but costs $10 per 1000 requests,
   so use it only for a few objects; the dry run prints the estimate.
2. ``--copy`` copies each restored version onto its own key with storage class
   GLACIER_IR. The copy becomes the key's current version, which GetObject can
   read. The old GLACIER version stays as a noncurrent version.

Run ``--restore`` first, then re-run ``--copy`` until it reports no pending
restores. Both steps are idempotent: a version already copied is no longer a
target, and a version whose restore is in progress is not requested again.

A GLACIER version is a target only when copying it cannot hide newer data:

- ``current``: it is the key's latest version;
- ``expired_llm_cache``: under ``llm-cache/``, the key's latest version is a
  delete marker that the removed ``llm-cache-ttl`` lifecycle rule left
  (stamped at midnight UTC, see #4831), and this is the newest real version.
  Copying it makes the cached entry current again, like
  ``restore_llm_cache_delete_markers_4831.py`` did for readable versions.

Every other GLACIER version is skipped and counted: a noncurrent version
behind a newer version (copying it would replace the newer data), or a
version behind a delete marker someone made on purpose.

Dry run by default: it lists the bucket and prints counts, bytes and a cost
estimate per top-level prefix, then exits.

Usage (local, AWS credentials for the dev account):
    packages/scraper-framework/.venv/bin/python \\
        scripts/move_glacier_to_glacier_ir_4854.py \\
        --bucket judgemind-document-archive-dev [--restore | --copy]
"""

# venv: scraper-framework
# one-off: true

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

COLD_STORAGE_CLASSES = frozenset({"GLACIER", "DEEP_ARCHIVE"})
TARGET_STORAGE_CLASS = "GLACIER_IR"
LLM_CACHE_PREFIX = "llm-cache/"
GB = 1024**3

# us-west-2 list prices, rounded up. Bulk retrievals from GLACIER are free
# today; the estimate charges the older Bulk price so it stays an upper bound.
# Retrieval price per tier: (USD per GB, USD per 1000 restore requests).
# Bulk finishes within ~12 hours, Standard 3-5 hours, Expedited 1-5 minutes.
RESTORE_PRICES = {
    "Bulk": (0.0025, 0.025),
    "Standard": (0.01, 0.05),
    "Expedited": (0.03, 10.0),
}
PRICE_GLACIER_IR_PUT_PER_1000 = 0.02
PRICE_GLACIER_IR_GB_MONTH = 0.004
PRICE_GLACIER_GB_MONTH = 0.0036
PRICE_STANDARD_GB_MONTH = 0.023


@dataclass(frozen=True)
class Version:
    key: str
    version_id: str
    is_latest: bool
    last_modified: datetime
    storage_class: str = "STANDARD"
    size: int = 0
    is_delete_marker: bool = False


@dataclass(frozen=True)
class Target:
    key: str
    version_id: str
    size: int
    reason: str  # "current" or "expired_llm_cache"


def _is_lifecycle_timestamp(ts: datetime) -> bool:
    return ts.hour == 0 and ts.minute == 0 and ts.second == 0 and ts.microsecond == 0


def select_targets(entries: list[Version]) -> tuple[list[Target], Counter]:
    """Return the GLACIER versions to move, plus a Counter of skipped versions."""
    by_key: dict[str, list[Version]] = {}
    for e in entries:
        by_key.setdefault(e.key, []).append(e)

    targets: list[Target] = []
    skipped: Counter = Counter()
    for key, items in by_key.items():
        real = sorted(
            (i for i in items if not i.is_delete_marker),
            key=lambda i: i.last_modified,
            reverse=True,
        )
        latest = next((i for i in items if i.is_latest), None)
        for v in real:
            if v.storage_class not in COLD_STORAGE_CLASSES:
                continue
            if v.is_latest:
                targets.append(Target(key, v.version_id, v.size, "current"))
                continue
            if v is not real[0]:
                reason = "noncurrent_behind_newer_version"
            elif latest is None or not latest.is_delete_marker:
                reason = "noncurrent_no_latest"
            elif key.startswith(LLM_CACHE_PREFIX) and _is_lifecycle_timestamp(
                latest.last_modified
            ):
                targets.append(Target(key, v.version_id, v.size, "expired_llm_cache"))
                continue
            else:
                reason = "behind_deliberate_delete_marker"
            skipped[(reason, top_prefix(key))] += 1
    return targets, skipped


def top_prefix(key: str) -> str:
    return key.split("/", 1)[0] + "/" if "/" in key else key


def estimate_cost(
    targets: list[Target], restore_days: int, tier: str = "Bulk"
) -> dict[str, float]:
    gb = sum(t.size for t in targets) / GB
    n = len(targets)
    per_gb, per_1000 = RESTORE_PRICES[tier]
    return {
        f"restore_{tier.lower()}": gb * per_gb + n / 1000 * per_1000,
        "restored_copy_storage": gb * PRICE_STANDARD_GB_MONTH * restore_days / 30,
        "copy_requests": n / 1000 * PRICE_GLACIER_IR_PUT_PER_1000,
        "glacier_ir_per_month": gb * PRICE_GLACIER_IR_GB_MONTH,
        "old_glacier_versions_per_month": gb * PRICE_GLACIER_GB_MONTH,
    }


def summarize(targets: list[Target]) -> list[tuple[str, str, int, int]]:
    """Rows of (prefix, reason, count, bytes), largest count first."""
    counts: Counter = Counter()
    sizes: Counter = Counter()
    for t in targets:
        counts[(top_prefix(t.key), t.reason)] += 1
        sizes[(top_prefix(t.key), t.reason)] += t.size
    return [(p, r, n, sizes[(p, r)]) for (p, r), n in counts.most_common()]


def list_versions(s3, bucket: str, prefix: str) -> list[Version]:
    out: list[Version] = []
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for v in page.get("Versions", []):
            out.append(
                Version(
                    key=v["Key"],
                    version_id=v["VersionId"],
                    is_latest=v["IsLatest"],
                    last_modified=v["LastModified"],
                    storage_class=v.get("StorageClass", "STANDARD"),
                    size=v.get("Size", 0),
                )
            )
        for m in page.get("DeleteMarkers", []):
            out.append(
                Version(
                    key=m["Key"],
                    version_id=m["VersionId"],
                    is_latest=m["IsLatest"],
                    last_modified=m["LastModified"],
                    is_delete_marker=True,
                )
            )
    return out


def restore_state(s3, bucket: str, t: Target) -> str:
    """Return "none", "in_progress" or "done" for a version's restore."""
    head = s3.head_object(Bucket=bucket, Key=t.key, VersionId=t.version_id)
    header = head.get("Restore")
    if not header:
        return "none"
    return "in_progress" if 'ongoing-request="true"' in header else "done"


def request_restores(
    s3, bucket: str, targets: list[Target], days: int, tier: str
) -> Counter:
    result: Counter = Counter()
    for t in targets:
        state = restore_state(s3, bucket, t)
        if state != "none":
            result[f"already_{state}"] += 1
            continue
        try:
            s3.restore_object(
                Bucket=bucket,
                Key=t.key,
                VersionId=t.version_id,
                RestoreRequest={"Days": days, "GlacierJobParameters": {"Tier": tier}},
            )
            result["requested"] += 1
        except s3.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "RestoreAlreadyInProgress":
                result["already_in_progress"] += 1
            else:
                result[f"error_{code}"] += 1
                print(f"  restore error {code} {t.key} {t.version_id}", flush=True)
    return result


def _still_latest(s3, bucket: str, t: Target) -> bool:
    """Check the key's latest version has not changed since the listing."""
    try:
        head = s3.head_object(Bucket=bucket, Key=t.key)
    except s3.exceptions.ClientError as exc:
        # HEAD of a key whose latest version is a delete marker returns 404
        # with x-amz-delete-marker: true.
        headers = exc.response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        return (
            t.reason == "expired_llm_cache"
            and headers.get("x-amz-delete-marker") == "true"
        )
    return t.reason == "current" and head.get("VersionId") == t.version_id


def copy_restored(s3, bucket: str, targets: list[Target]) -> Counter:
    result: Counter = Counter()
    for t in targets:
        state = restore_state(s3, bucket, t)
        if state != "done":
            result[f"pending_{state}"] += 1
            continue
        if not _still_latest(s3, bucket, t):
            result["skipped_key_changed"] += 1
            continue
        s3.copy_object(
            Bucket=bucket,
            Key=t.key,
            CopySource={"Bucket": bucket, "Key": t.key, "VersionId": t.version_id},
            StorageClass=TARGET_STORAGE_CLASS,
            MetadataDirective="COPY",
        )
        result["copied"] += 1
        if result["copied"] % 200 == 0:
            print(f"copied {result['copied']}", flush=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", default="")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--restore", action="store_true", help="request restores")
    mode.add_argument("--copy", action="store_true", help="copy restored versions")
    parser.add_argument("--restore-days", type=int, default=14)
    parser.add_argument("--tier", default="Bulk", choices=sorted(RESTORE_PRICES))
    args = parser.parse_args(argv)

    import boto3

    s3 = boto3.client("s3")
    entries = list_versions(s3, args.bucket, args.prefix)
    targets, skipped = select_targets(entries)

    total = sum(t.size for t in targets)
    print(
        f"listed {len(entries)} versions/markers under s3://{args.bucket}/{args.prefix}"
    )
    print(f"targets: {len(targets)} versions, {total / GB:.3f} GiB")
    for prefix, reason, n, size in summarize(targets):
        print(f"  {n:7d}  {size / GB:9.3f} GiB  {reason:18s} {prefix}")
    for (reason, prefix), n in sorted(skipped.items()):
        print(f"skipped {reason} {prefix}: {n}")
    for item, usd in estimate_cost(targets, args.restore_days, args.tier).items():
        print(f"estimate {item}: ${usd:.2f}")

    if args.restore:
        result = request_restores(
            s3, args.bucket, targets, args.restore_days, args.tier
        )
    elif args.copy:
        result = copy_restored(s3, args.bucket, targets)
    else:
        print("dry run: pass --restore, then --copy once the restores finish")
        return 0

    for k, n in sorted(result.items()):
        print(f"{k}: {n}")
    return 1 if any(k.startswith("error_") for k in result) else 0


if __name__ == "__main__":
    sys.exit(main())
