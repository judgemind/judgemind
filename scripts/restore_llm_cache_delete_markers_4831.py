#!/usr/bin/env python3
"""Restore llm-cache entries that the old 90-day lifecycle TTL expired (#4831).

Until #4831 the archive bucket had an ``llm-cache-ttl`` lifecycle rule that
expired ``llm-cache/`` objects after 90 days. The bucket is versioned, so an
expiry did not delete data. It added a delete marker on top of the entry, and
the cached value is still there as a noncurrent version. Removing the delete
marker makes that version current again. The next reingest then gets a cache
hit with the original split, instead of a fresh LLM run that can shift
positional split-child ids.

A key is restored only when all of these hold:

- its latest version is a delete marker;
- the marker's timestamp is exactly midnight UTC. Lifecycle expirations are
  stamped at 00:00:00Z, so a deliberate delete made by a person or a script
  is left alone;
- the newest earlier version is at least ``--min-age-days`` (default 89)
  older than the marker, which matches a 90-day expiration;
- that version's storage class can be read with GetObject (STANDARD or
  STANDARD_IA). A GLACIER version would come back as a current object that
  GetObject cannot read, which is still a cache miss, so it is skipped.

Removing a delete marker is a DELETE on the marker's version id. S3 does not
charge for DELETE requests, and no data is copied.

Dry run by default. Pass ``--apply`` to remove the markers.

Usage (local, AWS credentials for the dev account):
    packages/scraper-framework/.venv/bin/python \\
        scripts/restore_llm_cache_delete_markers_4831.py \\
        --bucket judgemind-document-archive-dev [--apply]
"""

# venv: scraper-framework
# one-off: true

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

READABLE_STORAGE_CLASSES = frozenset({"STANDARD", "STANDARD_IA"})
DELETE_BATCH = 1000


@dataclass(frozen=True)
class Version:
    key: str
    version_id: str
    is_latest: bool
    last_modified: datetime
    storage_class: str = "STANDARD"
    is_delete_marker: bool = False


@dataclass(frozen=True)
class Restore:
    key: str
    marker_version_id: str
    restored_version_id: str


def _is_lifecycle_timestamp(ts: datetime) -> bool:
    return ts.hour == 0 and ts.minute == 0 and ts.second == 0 and ts.microsecond == 0


def select_restorable(
    entries: list[Version], min_age_days: int = 89
) -> tuple[list[Restore], Counter]:
    """Return the markers to remove, plus a Counter of reasons keys were skipped."""
    by_key: dict[str, list[Version]] = {}
    for e in entries:
        by_key.setdefault(e.key, []).append(e)

    restores: list[Restore] = []
    skipped: Counter = Counter()
    for key, items in by_key.items():
        latest = [i for i in items if i.is_latest]
        if not latest or not latest[0].is_delete_marker:
            continue
        marker = latest[0]
        prior = sorted(
            (i for i in items if not i.is_delete_marker),
            key=lambda i: i.last_modified,
            reverse=True,
        )
        if not prior:
            skipped["no_prior_version"] += 1
            continue
        newest = prior[0]
        if not _is_lifecycle_timestamp(marker.last_modified):
            skipped["not_lifecycle_marker"] += 1
            continue
        if marker.last_modified - newest.last_modified < timedelta(days=min_age_days):
            skipped["too_young_for_ttl"] += 1
            continue
        if newest.storage_class not in READABLE_STORAGE_CLASSES:
            skipped[f"unreadable_{newest.storage_class}"] += 1
            continue
        restores.append(Restore(key, marker.version_id, newest.version_id))
    return restores, skipped


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


def remove_markers(s3, bucket: str, restores: list[Restore]) -> tuple[int, list[dict]]:
    removed = 0
    errors: list[dict] = []
    for i in range(0, len(restores), DELETE_BATCH):
        batch = restores[i : i + DELETE_BATCH]
        resp = s3.delete_objects(
            Bucket=bucket,
            Delete={
                "Objects": [
                    {"Key": r.key, "VersionId": r.marker_version_id} for r in batch
                ],
                "Quiet": True,
            },
        )
        batch_errors = resp.get("Errors", [])
        errors.extend(batch_errors)
        removed += len(batch) - len(batch_errors)
        print(
            f"batch {i // DELETE_BATCH + 1}: removed {len(batch) - len(batch_errors)}",
            flush=True,
        )
    return removed, errors


def prompt_dir(key: str) -> str:
    parts = key.split("/")
    label = "/".join(parts[1:3]) if len(parts) > 3 else key
    return label + ("/pages" if "/pages/" in key else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", default="llm-cache/")
    parser.add_argument("--min-age-days", type=int, default=89)
    parser.add_argument(
        "--apply", action="store_true", help="remove the markers (default: dry run)"
    )
    args = parser.parse_args(argv)

    import boto3

    s3 = boto3.client("s3")
    entries = list_versions(s3, args.bucket, args.prefix)
    restores, skipped = select_restorable(entries, args.min_age_days)

    print(
        f"listed {len(entries)} versions/markers under s3://{args.bucket}/{args.prefix}"
    )
    print(f"restorable keys: {len(restores)}")
    for reason, n in sorted(skipped.items()):
        print(f"skipped {reason}: {n}")
    for label, n in Counter(prompt_dir(r.key) for r in restores).most_common(15):
        print(f"  {n:6d}  {label}")

    if not args.apply:
        print("dry run: pass --apply to remove the delete markers")
        return 0

    removed, errors = remove_markers(s3, args.bucket, restores)
    print(f"removed {removed} delete markers; errors {len(errors)}")
    for e in errors[:20]:
        print(f"  error {e.get('Code')} {e.get('Key')}: {e.get('Message')}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
