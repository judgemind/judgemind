"""The only sanctioned way to delete from the raw archive (#4991).

Raw S3 objects are the source of truth: a tentative ruling disappears from
the court site within days, so a raw deleted without a live copy is a ruling
that can never be rebuilt.  The rule (``docs/specs/architecture-spec-v1.md``
§3.3.3) is: **no raw is deleted unless a live, byte-identical copy exists**.
"No database row references it" is not enough: #4988 found 746 unique dated
captures that a DB-reference-only cleanup would have deleted.

This module is the one place that may call ``delete_object`` on a raw key.
``scripts/check-no-unguarded-s3-delete.sh`` fails CI when any other file under
``scripts/`` or ``packages/`` calls an S3 delete without a
``# s3-delete-not-raw: <reason>`` marker.

- :func:`delete_raw_with_live_copy` deletes the current version of a raw key
  only after SHA-256-ing its bytes and finding another live key whose bytes
  hash the same.
- :func:`remove_delete_marker` restores a deleted key by removing its delete
  marker.  It deletes only a version it has just confirmed is the latest
  delete marker, never a real version.
- :func:`find_live_identical_copy` is the uniqueness check itself, used by
  ``scripts/audit_raw_delete_markers.py`` to classify past deletions.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from framework.s3_keys import parse_flat_hash_key


class UnsafeRawDeleteError(RuntimeError):
    """A delete was refused because it could lose a raw capture."""


def object_sha256(
    s3: Any, bucket: str, key: str, version_id: str | None = None
) -> tuple[str, int, dict[str, Any]]:
    """SHA-256 of an object's bytes (optionally one version): ``(hex, size, resp)``.

    ``resp`` is the GetObject response without its body, for metadata.
    """
    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if version_id:
        kwargs["VersionId"] = version_id
    resp = s3.get_object(**kwargs)
    body = resp.pop("Body").read()
    return hashlib.sha256(body).hexdigest(), len(body), resp


def index_live_by_hash(keys: Iterable[str]) -> dict[str, list[str]]:
    """Content-addressed raw keys indexed by their filename hash."""
    by_hash: dict[str, list[str]] = defaultdict(list)
    for key in keys:
        parsed = parse_flat_hash_key(key)
        if parsed is not None:
            by_hash[parsed["hash"]].append(key)
    return by_hash


def list_live_keys(s3: Any, bucket: str, prefix: str) -> list[str]:
    """Every current (not deleted) key under *prefix*."""
    paginator = s3.get_paginator("list_objects_v2")
    return [
        obj["Key"]
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for obj in page.get("Contents", [])
    ]


def find_live_identical_copy(
    s3: Any,
    bucket: str,
    sha256: str,
    live_by_hash: dict[str, list[str]],
    *,
    exclude_key: str,
) -> str | None:
    """A live key, other than *exclude_key*, whose bytes hash to *sha256*.

    Candidates are the live content-addressed keys named ``<sha256>.<ext>``
    under any prefix (*live_by_hash*, see :func:`index_live_by_hash`).  Each
    candidate's current bytes are read and hashed: a name is not proof of
    content, as the 2026-03-28 mislabels showed (#2661).  Same-prefix
    candidates are tried first.
    """
    prefix = exclude_key.rsplit("/", 1)[0] + "/"
    candidates = sorted(
        (k for k in live_by_hash.get(sha256, []) if k != exclude_key),
        key=lambda k: (not k.startswith(prefix), k),
    )
    for candidate in candidates:
        try:
            digest, _, _ = object_sha256(s3, bucket, candidate)
        except Exception as exc:  # botocore ClientError: gone since listing
            code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")
            if code in ("404", "NoSuchKey", "NotFound"):
                continue
            raise
        if digest == sha256:
            return candidate
    return None


def delete_raw_with_live_copy(
    s3: Any, bucket: str, key: str, live_by_hash: dict[str, list[str]]
) -> str:
    """Delete the current version of raw *key* only if a live copy exists.

    Returns the key of the live byte-identical copy.  Raises
    :class:`UnsafeRawDeleteError`, deleting nothing, when there is none.
    The bucket is versioned, so the delete adds a delete marker; the bytes
    stay as a noncurrent version.
    """
    sha256, _, _ = object_sha256(s3, bucket, key)
    twin = find_live_identical_copy(s3, bucket, sha256, live_by_hash, exclude_key=key)
    if twin is None:
        raise UnsafeRawDeleteError(
            f"refusing to delete s3://{bucket}/{key}: no other live object has "
            f"its bytes (sha256 {sha256})"
        )
    s3.delete_object(Bucket=bucket, Key=key)
    return twin


def remove_delete_marker(s3: Any, bucket: str, key: str, marker_version_id: str) -> None:
    """Restore *key* by deleting its latest delete marker.

    Re-lists the key's versions first and raises
    :class:`UnsafeRawDeleteError` unless *marker_version_id* is a delete
    marker and the latest version.  The previous real version becomes
    current again; no data version is ever deleted.
    """
    resp = s3.list_object_versions(Bucket=bucket, Prefix=key)
    marker = next(
        (
            m
            for m in resp.get("DeleteMarkers", [])
            if m["Key"] == key and m["VersionId"] == marker_version_id
        ),
        None,
    )
    if marker is None or not marker.get("IsLatest"):
        raise UnsafeRawDeleteError(
            f"refusing to delete version {marker_version_id} of s3://{bucket}/{key}: "
            "it is not the latest delete marker"
        )
    s3.delete_object(Bucket=bucket, Key=key, VersionId=marker_version_id)
