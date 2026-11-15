#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Audit delete markers on raw keys; optionally restore any raw without a live copy.

The archive bucket is versioned, so deleting a raw key only adds a delete
marker and the bytes survive as a noncurrent version.  A deleted raw is safe
only when another live key holds the same bytes (the rule in
``docs/specs/architecture-spec-v1.md`` §3.3.3, #4991).  For every
content-addressed raw key (``<state>/<county>/<court>/raw/<sha>.<ext>``)
whose latest version is a delete marker, this script reads the newest real
version before the marker, SHA-256s its bytes and classifies it:

- ``safe_same_prefix``   a live key under the same court prefix has the bytes;
- ``safe_other_prefix``  a live key under another prefix has the bytes;
- ``no_live_copy``       no live key has the bytes: a lost-from-view raw;
- ``no_prior_version``   only delete markers remain (nothing to recover).

A version in GLACIER / DEEP_ARCHIVE cannot be read until it is thawed.  It
gets a provisional ``archived_safe_thaw_*`` / ``archived_no_live_copy_thaw_*``
verdict from its metadata ``content-hash`` (the live candidate's bytes are
still hashed).  ``--thaw Standard|Bulk|Expedited`` requests a 7-day restored
copy of each one (``RestoreObject``: no version changes); run again once the
copies are ready (Standard: 3-5 hours) for a verdict on the bytes.

Each row also gets a cause: ``mislabel`` when the filename hash is not the
bytes hash (the 2026-03-28 migration mislabels that #2661 cleaned up),
otherwise ``correctly_named``.  Output: one ``LOST`` line per ``no_live_copy`` key,
``SUMMARY <county>: {json}`` per county (counts by class and by
``cause@marker-date``) and ``SUMMARY_ALL``.

``--restore`` makes every ``no_live_copy`` raw current again.  A correctly
named one (filename hash = bytes hash) gets its delete marker removed.  A
mislabeled one is copied from its noncurrent version to the key its bytes
belong at (``<prefix>/<bytes sha>.<ext>``); an existing target is left alone.
Real versions are never deleted or overwritten (``framework.raw_delete_guard``).
``--write-keys URI`` writes the restored keys, one per line, for
``reingest_from_s3.py --s3-key-list``.

Usage (run on ECS, the oneshot role can list versions and get)::

    scripts/ecs-run-task.sh --detach scripts/audit_raw_delete_markers.py
    scripts/ecs-wait-task.sh

    # --thaw needs s3:RestoreObject; --restore needs s3:DeleteObjectVersion
    # and s3:PutObject on raw keys, which no ECS oneshot role grants: run
    # those with operator credentials for the dev account.
    packages/scraper-framework/.venv/bin/python \\
        scripts/audit_raw_delete_markers.py --restore \\
        --write-keys s3://judgemind-document-archive-dev/tmp/keys/restored-4991.txt

Options: ``--prefix P`` (default ``ca/``), ``--workers N`` (default 16),
``--thaw TIER``, ``--restore``, ``--write-keys URI``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# The scraper-framework source: repo checkout locally, /app in the ECS image.
for _path in (
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "packages",
        "scraper-framework",
        "src",
    ),
    "/app/packages/scraper-framework/src",
    "/app/src",
    "/app",
):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.insert(0, _path)

from framework.raw_delete_guard import (  # noqa: E402
    find_live_identical_copy,
    index_live_by_hash,
    object_sha256,
    remove_delete_marker,
)
from framework.s3_keys import parse_flat_hash_key  # noqa: E402

BUCKET = os.environ.get("JUDGEMIND_ARCHIVE_BUCKET", "judgemind-document-archive-dev")


@dataclass(frozen=True)
class Deleted:
    """A raw key whose latest version is a delete marker."""

    key: str
    county: str
    name_hash: str
    ext: str
    marker_version_id: str
    marker_time: datetime
    prior_version_id: str | None


@dataclass(frozen=True)
class Verdict:
    item: Deleted
    verdict: str
    cause: str
    sha256: str | None = None
    size: int = 0
    twin: str | None = None
    metadata: dict[str, str] | None = None
    content_type: str | None = None

    @property
    def restore_key(self) -> str:
        """Where the bytes belong: the key itself unless it is mislabeled."""
        if self.cause == "mislabel" and self.sha256:
            prefix = self.item.key.rsplit("/", 1)[0]
            return f"{prefix}/{self.sha256}.{self.item.ext}"
        return self.item.key


def scan_versions(
    s3: Any, bucket: str, prefix: str
) -> tuple[list[str], list[Deleted], list[str]]:
    """Live keys, deleted raw keys, and deleted keys that are not raw."""
    versions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    markers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for v in page.get("Versions", []):
            versions[v["Key"]].append(v)
        for m in page.get("DeleteMarkers", []):
            markers[m["Key"]].append(m)

    live = sorted(k for k, vs in versions.items() if any(v["IsLatest"] for v in vs))
    deleted: list[Deleted] = []
    not_raw: list[str] = []
    for key in sorted(markers):
        latest = next((m for m in markers[key] if m["IsLatest"]), None)
        if latest is None:
            continue  # an old marker under a live version: nothing deleted
        parsed = parse_flat_hash_key(key)
        if parsed is None:
            not_raw.append(key)
            continue
        earlier = [
            v
            for v in versions.get(key, [])
            if v["LastModified"] <= latest["LastModified"]
        ]
        prior = max(earlier, key=lambda v: v["LastModified"], default=None)
        deleted.append(
            Deleted(
                key=key,
                county=parsed["county"],
                name_hash=parsed["hash"],
                ext=parsed["ext"],
                marker_version_id=latest["VersionId"],
                marker_time=latest["LastModified"],
                prior_version_id=prior["VersionId"] if prior else None,
            )
        )
    return live, deleted, not_raw


def _error_code(exc: Exception) -> str | None:
    return (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")


def classify_archived(
    s3: Any,
    bucket: str,
    item: Deleted,
    live_by_hash: dict[str, list[str]],
    thaw_tier: str | None,
) -> Verdict:
    """A version in GLACIER / DEEP_ARCHIVE: its bytes cannot be read until thawed.

    The verdict is provisional, from the metadata ``content-hash`` the
    archiver stamped (the live candidate's bytes are still hashed).  With
    *thaw_tier*, a temporary restored copy is requested (it changes no
    version); re-run once it is ready for a verdict on the bytes.
    """
    head = s3.head_object(Bucket=bucket, Key=item.key, VersionId=item.prior_version_id)
    metadata = dict(head.get("Metadata") or {})
    meta_hash = metadata.get("content-hash") or item.name_hash
    cause = "correctly_named" if meta_hash == item.name_hash else "mislabel"
    restore_header = head.get("Restore") or ""
    if 'ongoing-request="true"' in restore_header:
        thaw = "in_progress"
    elif thaw_tier:
        s3.restore_object(
            Bucket=bucket,
            Key=item.key,
            VersionId=item.prior_version_id,
            RestoreRequest={"Days": 7, "GlacierJobParameters": {"Tier": thaw_tier}},
        )
        thaw = "requested"
    else:
        thaw = "not_requested"
    twin = find_live_identical_copy(
        s3, bucket, meta_hash, live_by_hash, exclude_key=item.key
    )
    verdict = f"archived_{'safe' if twin else 'no_live_copy'}_thaw_{thaw}"
    return Verdict(
        item,
        verdict,
        cause,
        sha256=meta_hash,
        size=int(head.get("ContentLength", 0)),
        twin=twin,
        metadata=metadata,
        content_type=head.get("ContentType"),
    )


def classify(
    s3: Any,
    bucket: str,
    item: Deleted,
    live_by_hash: dict[str, list[str]],
    thaw_tier: str | None = None,
) -> Verdict:
    if item.prior_version_id is None:
        return Verdict(item, "no_prior_version", "unknown")
    try:
        sha256, size, resp = object_sha256(s3, bucket, item.key, item.prior_version_id)
    except Exception as exc:  # botocore ClientError
        if _error_code(exc) != "InvalidObjectState":
            raise
        return classify_archived(s3, bucket, item, live_by_hash, thaw_tier)
    cause = "correctly_named" if sha256 == item.name_hash else "mislabel"
    twin = find_live_identical_copy(
        s3, bucket, sha256, live_by_hash, exclude_key=item.key
    )
    if twin is None:
        verdict = "no_live_copy"
    elif twin.rsplit("/", 1)[0] == item.key.rsplit("/", 1)[0]:
        verdict = "safe_same_prefix"
    else:
        verdict = "safe_other_prefix"
    return Verdict(
        item,
        verdict,
        cause,
        sha256=sha256,
        size=size,
        twin=twin,
        metadata=dict(resp.get("Metadata") or {}),
        content_type=resp.get("ContentType"),
    )


def restore(s3: Any, bucket: str, v: Verdict) -> str:
    """Make a ``no_live_copy`` raw current again; returns ``removed_marker``,
    ``copied`` or ``exists``."""
    if v.restore_key == v.item.key:
        remove_delete_marker(s3, bucket, v.item.key, v.item.marker_version_id)
        return "removed_marker"
    target = v.restore_key
    try:
        s3.head_object(Bucket=bucket, Key=target)
        return "exists"
    except Exception as exc:  # botocore ClientError; only "not found" proceeds
        if _error_code(exc) not in ("404", "NoSuchKey", "NotFound"):
            raise
    kwargs: dict[str, Any] = {
        "Bucket": bucket,
        "Key": target,
        "CopySource": {
            "Bucket": bucket,
            "Key": v.item.key,
            "VersionId": v.item.prior_version_id,
        },
        "MetadataDirective": "REPLACE",
        "Metadata": {
            **(v.metadata or {}),
            "content-hash": v.sha256 or "",
            "restored-from": f"{v.item.key}?versionId={v.item.prior_version_id}",
        },
    }
    if v.content_type:
        kwargs["ContentType"] = v.content_type
    s3.copy_object(**kwargs)
    head = s3.head_object(Bucket=bucket, Key=target)
    if int(head.get("ContentLength", -1)) != v.size:
        raise RuntimeError(f"copy size mismatch for {target}")
    return "copied"


def write_uri(s3: Any, uri: str, text: str) -> None:
    if not uri.startswith("s3://"):
        with open(uri, "w") as fh:
            fh.write(text)
        return
    bucket, _, key = uri[len("s3://") :].partition("/")
    s3.put_object(Bucket=bucket, Key=key, Body=text.encode())


def run(
    args: argparse.Namespace, s3: Any, out: Callable[[str], None] = print
) -> list[Verdict]:
    live, deleted, not_raw = scan_versions(s3, args.bucket, args.prefix)
    live_by_hash = index_live_by_hash(live)
    out(
        f"LISTED {len(live)} live keys under {args.prefix}; "
        f"{len(deleted)} deleted raw keys; {len(not_raw)} deleted non-raw keys"
    )
    for key in not_raw:
        out(f"NOT_RAW key={key}")

    by_county: dict[str, Counter[str]] = defaultdict(Counter)
    verdicts: list[Verdict] = []

    def work(item: Deleted) -> Verdict | tuple[Deleted, str]:
        try:
            return classify(s3, args.bucket, item, live_by_hash, args.thaw)
        except Exception as exc:  # noqa: BLE001 — report and continue
            return item, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for done, result in enumerate(pool.map(work, deleted), start=1):
            if isinstance(result, tuple):
                item, err = result
                by_county[item.county]["error"] += 1
                out(f"ERROR key={item.key} error={err}")
                continue
            v = result
            verdicts.append(v)
            counts = by_county[v.item.county]
            counts[v.verdict] += 1
            counts[f"{v.cause}@{v.item.marker_time.date().isoformat()}"] += 1
            if "no_live_copy" in v.verdict or v.verdict == "no_prior_version":
                md = v.metadata or {}
                out(
                    f"LOST key={v.item.key} verdict={v.verdict} cause={v.cause} "
                    f"sha256={v.sha256} bytes={v.size} "
                    f"scraper_id={md.get('scraper-id')} source_url={md.get('source-url')} "
                    f"restore_to={v.restore_key}"
                )
            if done % 500 == 0:
                out(f"PROGRESS {done}/{len(deleted)}")

    restored: list[str] = []
    if args.restore:
        results: Counter[str] = Counter()
        for v in sorted(verdicts, key=lambda x: x.item.key):
            if v.verdict != "no_live_copy":
                continue
            try:
                results[restore(s3, args.bucket, v)] += 1
                restored.append(v.restore_key)
            except Exception as exc:  # noqa: BLE001 — report and continue
                results["error"] += 1
                out(f"RESTORE_ERROR key={v.item.key} error={exc}")
        out(f"RESTORED {json.dumps(dict(results), sort_keys=True)}")

    if args.write_keys:
        keys = sorted(set(restored))
        write_uri(s3, args.write_keys, "".join(f"{k}\n" for k in keys))
        out(f"WROTE_KEYS {len(keys)} -> {args.write_keys}")

    total: Counter[str] = Counter()
    for county in sorted(by_county):
        out(f"SUMMARY {county}: {json.dumps(dict(by_county[county]), sort_keys=True)}")
        total.update(by_county[county])
    out(f"SUMMARY_ALL: {json.dumps(dict(total), sort_keys=True)}")
    return verdicts


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument("--prefix", default="ca/")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--thaw", choices=("Standard", "Bulk", "Expedited"))
    parser.add_argument("--write-keys", metavar="URI")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    import boto3

    args = parse_args(argv)
    run(args, boto3.client("s3"), out=lambda line: print(line, flush=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
