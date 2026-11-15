"""Tests for scripts/audit_dated_raw_keys.py (#4988).

A fake in-memory S3 stands in for boto3: list/get/head/copy/put.
"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import UTC, datetime
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import audit_dated_raw_keys as audit

BUCKET = "test-archive"
PREFIX = "ca/orange/superior_court/raw/"


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class _NotFound(Exception):
    def __init__(self) -> None:
        super().__init__("404")
        self.response = {"Error": {"Code": "404"}}


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3:
    def __init__(self, objects: dict[str, dict[str, Any]]) -> None:
        # key -> {"body": bytes, "metadata": {...}, "last_modified": dt, "content_type": str}
        self.objects = objects
        self.copies: list[dict[str, Any]] = []
        self.puts: dict[str, bytes] = {}

    def get_paginator(self, name: str) -> FakeS3:
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket: str, Prefix: str):  # noqa: N803
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        yield {"Contents": [{"Key": k} for k in keys[:2]]}
        yield {"Contents": [{"Key": k} for k in keys[2:]]}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        o = self.objects[Key]
        return {
            "Body": _Body(o["body"]),
            "LastModified": o.get("last_modified"),
            "Metadata": dict(o.get("metadata", {})),
            "ContentType": o.get("content_type"),
        }

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise _NotFound()
        return {"ContentLength": len(self.objects[Key]["body"])}

    def copy_object(self, **kw: Any) -> None:
        self.copies.append(kw)
        src = self.objects[kw["CopySource"]["Key"]]
        self.objects[kw["Key"]] = {
            "body": src["body"],
            "metadata": kw["Metadata"],
            "content_type": kw.get("ContentType"),
        }

    def put_object(self, Bucket: str, Key: str, Body: bytes) -> None:  # noqa: N803
        self.puts[f"{Bucket}/{Key}"] = Body


def _dated(day: str, name: str) -> str:
    y, m, d = day.split("-")
    return f"{PREFIX}{y}/{m}/{d}/{name}.pdf"


def _args(*argv: str):
    return audit.parse_args(["--bucket", BUCKET, "--samples", "0", *argv])


@pytest.fixture
def s3() -> FakeS3:
    dup, uniq, uniq2, other = b"%PDF dup", b"%PDF unique", b"%PDF uniq2", b"%PDF other"
    lm = datetime(2026, 3, 4, 20, 28, tzinfo=UTC)
    return FakeS3(
        {
            # content-addressed copy of the dup bytes
            f"{PREFIX}{_sha(dup)}.pdf": {"body": dup},
            # content-addressed copy of "other" under another county
            f"ca/riverside/superior_court/raw/{_sha(other)}.pdf": {"body": other},
            _dated("2026-03-04", "a"): {"body": dup, "last_modified": lm},
            _dated("2026-03-04", "b"): {
                "body": uniq,
                "last_modified": lm,
                "metadata": {
                    "capture-timestamp": "2026-03-04T20:26:18",
                    "scraper-id": "x",
                },
                "content_type": "application/pdf",
            },
            # same unique bytes captured twice: one target
            _dated("2026-03-05", "c"): {"body": uniq, "last_modified": lm},
            _dated("2026-03-06", "d"): {"body": other, "last_modified": lm},
            _dated("2026-03-07", "e"): {"body": uniq2, "last_modified": lm},
            "ca/orange/superior_court/raw/notes.txt": {"body": b"x"},
        }
    )


def test_split_keys_separates_dated_and_content_addressed() -> None:
    h = "a" * 64
    dated, by_hash = audit.split_keys(
        [_dated("2026-03-04", "u"), f"{PREFIX}{h}.pdf", "ca/x/y/raw/notes.txt"]
    )
    assert dated == [_dated("2026-03-04", "u")]
    assert by_hash == {h: [f"{PREFIX}{h}.pdf"]}


def test_content_key_for_and_path_date() -> None:
    key = f"{PREFIX}2026/03/04/u.PDF"
    assert audit.content_key_for(key, "f" * 64) == f"{PREFIX}{'f' * 64}.pdf"
    assert audit.path_date(key) == "2026-03-04"
    with pytest.raises(ValueError):
        audit.content_key_for(f"{PREFIX}{'f' * 64}.pdf", "f" * 64)
    with pytest.raises(ValueError):
        audit.path_date("ca/x/y/raw/u.pdf")


def test_audit_classifies_and_reports_per_county(s3: FakeS3) -> None:
    lines: list[str] = []
    reports = audit.run(_args(), s3, db_hashes={_sha(b"%PDF dup")}, out=lines.append)
    summary = reports["orange"].summary()
    assert summary["dated"] == 5
    assert summary["dup_same_prefix"] == 1
    assert summary["dup_other_prefix"] == 1
    assert summary["unique"] == 3
    assert summary["hash_in_db"] == 1
    assert summary["unique_date_range"] == ["2026-03-04", "2026-03-07"]
    assert sum(1 for line in lines if line.startswith("UNIQUE ")) == 3
    assert any(line.startswith("SUMMARY_ALL") for line in lines)
    assert s3.copies == []  # audit only: nothing written


def test_migrate_copies_unique_once_with_capture_metadata(s3: FakeS3) -> None:
    before = {k: dict(v) for k, v in s3.objects.items()}
    lines: list[str] = []
    uri = "s3://test-archive/tmp/keys/dated.txt"
    audit.run(_args("--migrate", "--write-keys", uri), s3, None, out=lines.append)

    uniq_target = f"{PREFIX}{_sha(b'%PDF unique')}.pdf"
    uniq2_target = f"{PREFIX}{_sha(b'%PDF uniq2')}.pdf"
    assert sorted(c["Key"] for c in s3.copies) == sorted([uniq_target, uniq2_target])
    # originals are untouched, nothing deleted
    for key, obj in before.items():
        assert s3.objects[key]["body"] == obj["body"]
    meta = s3.objects[uniq_target]["metadata"]
    assert meta["content-hash"] == _sha(b"%PDF unique")
    assert meta["capture-timestamp"] == "2026-03-04T20:26:18"
    assert meta["scraper-id"] == "x"
    assert meta["migrated-from"] == _dated("2026-03-04", "b")
    assert s3.objects[uniq_target]["content_type"] == "application/pdf"
    assert 'MIGRATED {"copied": 2}' in lines
    assert s3.puts[uri[len("s3://") :]].decode().splitlines() == sorted(
        [uniq_target, uniq2_target]
    )

    # a second run finds every target in place: no new copies
    s3.copies.clear()
    lines.clear()
    audit.run(_args("--migrate"), s3, None, out=lines.append)
    assert s3.copies == []
    assert "SUMMARY_ALL" in lines[-1]


def test_migrate_one_never_overwrites_existing_target(s3: FakeS3) -> None:
    obj = audit.fetch_dated(s3, BUCKET, _dated("2026-03-04", "b"))
    target = audit.content_key_for(obj.key, obj.sha256)
    s3.objects[target] = {"body": b"already here"}
    assert audit.migrate_one(s3, BUCKET, obj) == "exists"
    assert s3.objects[target]["body"] == b"already here"
    assert s3.copies == []


def test_migrate_one_raises_on_size_mismatch_and_other_head_errors(s3: FakeS3) -> None:
    obj = audit.fetch_dated(s3, BUCKET, _dated("2026-03-07", "e"))
    bad = audit.DatedObject(
        obj.key, obj.sha256, obj.size + 1, None, {}, None
    )  # claims a size the copy won't have
    with pytest.raises(RuntimeError, match="size mismatch"):
        audit.migrate_one(s3, BUCKET, bad)

    class Denied(Exception):
        response = {"Error": {"Code": "403"}}

    def head(**_: Any) -> None:
        raise Denied()

    s3.head_object = head  # type: ignore[method-assign]
    with pytest.raises(Denied):
        audit.migrate_one(s3, BUCKET, obj)


def test_capture_timestamp_prefers_metadata_then_last_modified_then_path_date() -> None:
    key = _dated("2026-03-04", "z")

    def obj(lm: datetime | None, meta: dict[str, str] | None = None) -> Any:
        return audit.DatedObject(key, "0" * 64, 1, lm, meta or {}, None)

    assert audit.capture_timestamp(obj(None, {"capture-timestamp": "T"})) == "T"
    lm = datetime(2026, 3, 4, 21, 0, tzinfo=UTC)
    assert audit.capture_timestamp(obj(lm)) == lm.isoformat()
    naive = datetime(2026, 3, 5, 1, 0)
    assert audit.capture_timestamp(obj(naive)).startswith("2026-03-05T01:00")
    # rewritten months later (a copy): the path date is the capture time
    late = datetime(2026, 9, 1, tzinfo=UTC)
    assert audit.capture_timestamp(obj(late)) == "2026-03-04T00:00:00+00:00"
    assert audit.capture_timestamp(obj(None)) == "2026-03-04T00:00:00+00:00"


def test_fetch_errors_are_reported_not_fatal(s3: FakeS3) -> None:
    real_get = s3.get_object

    def flaky(Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key.endswith("/e.pdf"):
            raise OSError("boom")
        return real_get(Bucket=Bucket, Key=Key)

    s3.get_object = flaky  # type: ignore[method-assign]
    lines: list[str] = []
    reports = audit.run(_args(), s3, None, out=lines.append)
    assert reports["orange"].summary()["error"] == 1
    assert any(line.startswith("ERROR key=") for line in lines)


def test_sample_text_html_and_pdf_fallback(s3: FakeS3) -> None:
    assert audit.sample_text(b"<p>Tentative  <b>ruling</b></p>", "a.html") == (
        "Tentative ruling"
    )
    # extract_text_from_pdf is unavailable in the scripts-tests env, or fails
    # on these fake bytes: the sample degrades, it never raises.
    assert isinstance(audit.sample_text(b"%PDF not really", "a.pdf"), str)
    lines: list[str] = []
    audit.run(
        audit.parse_args(["--bucket", BUCKET, "--samples", "1"]), s3, None, lines.append
    )
    assert any(line.startswith("SAMPLE orange key=") for line in lines)


def test_write_s3_uri_local_path(tmp_path: Any) -> None:
    path = tmp_path / "keys.txt"
    audit.write_s3_uri(None, str(path), "a\nb\n")
    assert path.read_text() == "a\nb\n"


def test_main_runs_with_boto3_client(
    monkeypatch: pytest.MonkeyPatch, s3: FakeS3
) -> None:
    import types

    monkeypatch.setitem(
        sys.modules, "boto3", types.SimpleNamespace(client=lambda name: s3)
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert audit.main(["--bucket", BUCKET, "--samples", "0"]) == 0
