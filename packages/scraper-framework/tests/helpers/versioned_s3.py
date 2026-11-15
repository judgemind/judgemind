"""An in-memory versioned S3 bucket for tests of raw-archive tooling (#4991).

Models what the delete-marker audit relies on: a plain delete adds a delete
marker, a delete with a VersionId removes that version, GLACIER versions
cannot be read until restored, and listing returns only current keys.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

_T0 = datetime(2026, 3, 28, tzinfo=UTC)


class ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class VersionedS3:
    def __init__(self) -> None:
        # key -> list of versions, oldest first; a version with body None
        # is a delete marker.
        self.versions: dict[str, list[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        self._clock = itertools.count(0)
        self.deleted_version_ids: list[str] = []
        self.restore_requests: list[dict[str, Any]] = []
        self.puts: dict[str, bytes] = {}

    # ---- setup helpers -------------------------------------------------
    def _now(self) -> datetime:
        return _T0 + timedelta(minutes=next(self._clock))

    def put(
        self,
        key: str,
        body: bytes,
        metadata: dict[str, str] | None = None,
        storage_class: str = "STANDARD",
    ) -> str:
        vid = f"v{next(self._ids)}"
        self.versions.setdefault(key, []).append(
            {
                "VersionId": vid,
                "body": body,
                "metadata": dict(metadata or {}),
                "LastModified": self._now(),
                "StorageClass": storage_class,
                "restored": False,
                "ContentType": "application/pdf",
            }
        )
        return vid

    def mark_deleted(self, key: str) -> str:
        vid = f"dm{next(self._ids)}"
        self.versions.setdefault(key, []).append(
            {"VersionId": vid, "body": None, "LastModified": self._now()}
        )
        return vid

    def latest(self, key: str) -> dict[str, Any] | None:
        vs = self.versions.get(key) or []
        return vs[-1] if vs else None

    def is_live(self, key: str) -> bool:
        v = self.latest(key)
        return v is not None and v["body"] is not None

    # ---- boto3 surface -------------------------------------------------
    def get_paginator(self, name: str) -> _Paginator:
        return _Paginator(self, name)

    def list_object_versions(self, Bucket: str, Prefix: str) -> dict[str, Any]:  # noqa: N803
        page: dict[str, Any] = {"Versions": [], "DeleteMarkers": []}
        for key in sorted(k for k in self.versions if k.startswith(Prefix)):
            vs = self.versions[key]
            for i, v in enumerate(vs):
                row = {
                    "Key": key,
                    "VersionId": v["VersionId"],
                    "IsLatest": i == len(vs) - 1,
                    "LastModified": v["LastModified"],
                }
                if v["body"] is None:
                    page["DeleteMarkers"].append(row)
                else:
                    row["StorageClass"] = v["StorageClass"]
                    page["Versions"].append(row)
        return page

    def _version(self, key: str, version_id: str | None) -> dict[str, Any]:
        vs = self.versions.get(key) or []
        if version_id is None:
            if not vs or vs[-1]["body"] is None:
                raise ClientError("NoSuchKey")
            return vs[-1]
        for v in vs:
            if v["VersionId"] == version_id:
                return v
        raise ClientError("NoSuchVersion")

    def get_object(self, Bucket: str, Key: str, VersionId: str | None = None) -> dict[str, Any]:  # noqa: N803
        v = self._version(Key, VersionId)
        if v["StorageClass"] in ("GLACIER", "DEEP_ARCHIVE") and not v["restored"]:
            raise ClientError("InvalidObjectState")
        return {
            "Body": _Body(v["body"]),
            "Metadata": dict(v["metadata"]),
            "ContentType": v["ContentType"],
            "LastModified": v["LastModified"],
        }

    def head_object(self, Bucket: str, Key: str, VersionId: str | None = None) -> dict[str, Any]:  # noqa: N803
        try:
            v = self._version(Key, VersionId)
        except ClientError as exc:
            raise ClientError("404") from exc
        out = {
            "ContentLength": len(v["body"]),
            "Metadata": dict(v["metadata"]),
            "ContentType": v["ContentType"],
            "StorageClass": v["StorageClass"],
        }
        if v.get("restore_requested") and not v["restored"]:
            out["Restore"] = 'ongoing-request="true"'
        return out

    def delete_object(self, Bucket: str, Key: str, VersionId: str | None = None) -> None:  # noqa: N803
        if VersionId is None:
            self.mark_deleted(Key)
            return
        self.deleted_version_ids.append(VersionId)
        self.versions[Key] = [v for v in self.versions[Key] if v["VersionId"] != VersionId]

    def restore_object(self, Bucket: str, Key: str, VersionId: str, RestoreRequest: Any) -> None:  # noqa: N803
        self.restore_requests.append({"Key": Key, "VersionId": VersionId, **RestoreRequest})
        self._version(Key, VersionId)["restore_requested"] = True

    def copy_object(self, **kw: Any) -> None:
        src = kw["CopySource"]
        v = self._version(src["Key"], src.get("VersionId"))
        self.put(kw["Key"], v["body"], kw.get("Metadata", v["metadata"]))

    def put_object(self, Bucket: str, Key: str, Body: bytes) -> None:  # noqa: N803
        self.puts[f"{Bucket}/{Key}"] = Body


class _Paginator:
    def __init__(self, s3: VersionedS3, name: str) -> None:
        self.s3 = s3
        self.name = name

    def paginate(self, Bucket: str, Prefix: str) -> Iterator[dict[str, Any]]:  # noqa: N803
        if self.name == "list_object_versions":
            yield self.s3.list_object_versions(Bucket=Bucket, Prefix=Prefix)
            return
        assert self.name == "list_objects_v2"
        keys = sorted(k for k in self.s3.versions if k.startswith(Prefix) and self.s3.is_live(k))
        yield {"Contents": [{"Key": k} for k in keys[:1]]}
        yield {"Contents": [{"Key": k} for k in keys[1:]]}
