"""Tests for scripts/audit_raw_delete_markers.py (#4991)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import NoReturn

from helpers.versioned_s3 import ClientError, VersionedS3

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import audit_raw_delete_markers as audit  # noqa: E402

B = "archive"
OC = "ca/orange/superior_court/raw/"
LA = "ca/los_angeles/superior_court/raw/"


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def run(s3: VersionedS3, *argv: str) -> tuple[list[audit.Verdict], list[str]]:
    lines: list[str] = []
    args = audit.parse_args(["--bucket", B, "--workers", "2", *argv])
    return audit.run(args, s3, out=lines.append), lines


def summary(lines: list[str], county: str) -> dict[str, int]:
    line = next(x for x in lines if x.startswith(f"SUMMARY {county}: "))
    return json.loads(line.split(": ", 1)[1])


def by_key(verdicts: list[audit.Verdict]) -> dict[str, audit.Verdict]:
    return {v.item.key: v for v in verdicts}


def mislabel_with_twin(s3: VersionedS3, body: bytes, twin_prefix: str = OC) -> str:
    """A #2661 mislabel: named by another hash, deleted, its twin live."""
    s3.put(twin_prefix + f"{sha(body)}.pdf", body)
    key = OC + f"{sha(body + b'-synthetic')}.pdf"
    s3.put(key, body, {"content-hash": sha(body)})
    s3.mark_deleted(key)
    return key


def test_classifies_safe_lost_and_no_prior_version() -> None:
    s3 = VersionedS3()
    same = mislabel_with_twin(s3, b"one")
    other = mislabel_with_twin(s3, b"two", twin_prefix=LA)
    lost = OC + f"{sha(b'unique')}.pdf"
    s3.put(lost, b"unique", {"scraper-id": "ca-oc-tentatives"})
    s3.mark_deleted(lost)
    empty = OC + f"{sha(b'never')}.pdf"
    s3.mark_deleted(empty)
    s3.put(OC + "2026/01/02/dated.pdf", b"d")  # not content-addressed: ignored
    s3.mark_deleted("ca/test/test/raw/synthetic.txt")

    verdicts, lines = run(s3)
    v = by_key(verdicts)
    assert v[same].verdict == "safe_same_prefix"
    assert v[same].cause == "mislabel"
    assert v[other].verdict == "safe_other_prefix"
    assert v[lost].verdict == "no_live_copy"
    assert v[lost].cause == "correctly_named"
    assert v[empty].verdict == "no_prior_version"
    assert any(x.startswith(f"LOST key={lost}") and "ca-oc-tentatives" in x for x in lines)
    assert "NOT_RAW key=ca/test/test/raw/synthetic.txt" in lines
    oc = summary(lines, "orange")
    assert oc["safe_same_prefix"] == 1 and oc["no_live_copy"] == 1
    assert sum(n for k, n in oc.items() if k.startswith("mislabel@")) == 2


def test_a_live_key_with_an_old_marker_is_not_deleted() -> None:
    s3 = VersionedS3()
    key = OC + f"{sha(b'r')}.pdf"
    s3.put(key, b"r")
    s3.mark_deleted(key)
    s3.put(key, b"r")
    verdicts, lines = run(s3)
    assert verdicts == []
    assert lines[0].startswith("LISTED 1 live keys")


def test_archived_version_is_provisional_and_thaw_requests_a_restore() -> None:
    s3 = VersionedS3()
    body = b"glacier"
    s3.put(OC + f"{sha(body)}.pdf", body)
    key = OC + f"{sha(b'x')}.pdf"
    vid = s3.put(key, body, {"content-hash": sha(body)}, storage_class="GLACIER")
    s3.mark_deleted(key)

    verdicts, _ = run(s3)
    assert by_key(verdicts)[key].verdict == "archived_safe_thaw_not_requested"
    assert s3.restore_requests == []

    verdicts, _ = run(s3, "--thaw", "Standard")
    assert by_key(verdicts)[key].verdict == "archived_safe_thaw_requested"
    assert s3.restore_requests == [
        {"Key": key, "VersionId": vid, "Days": 7, "GlacierJobParameters": {"Tier": "Standard"}}
    ]

    verdicts, _ = run(s3, "--thaw", "Standard")
    assert by_key(verdicts)[key].verdict == "archived_safe_thaw_in_progress"
    assert len(s3.restore_requests) == 1

    s3.versions[key][0]["restored"] = True
    verdicts, _ = run(s3)
    assert by_key(verdicts)[key].verdict == "safe_same_prefix"


def test_archived_version_without_a_live_copy_is_reported_lost() -> None:
    s3 = VersionedS3()
    key = OC + f"{sha(b'cold')}.pdf"
    s3.put(key, b"cold", storage_class="GLACIER")
    s3.mark_deleted(key)
    verdicts, lines = run(s3)
    assert by_key(verdicts)[key].verdict == "archived_no_live_copy_thaw_not_requested"
    assert any(x.startswith(f"LOST key={key}") for x in lines)


def test_restore_removes_the_marker_of_a_correctly_named_raw(tmp_path: Path) -> None:
    s3 = VersionedS3()
    safe = mislabel_with_twin(s3, b"dup")
    lost = OC + f"{sha(b'unique')}.pdf"
    s3.put(lost, b"unique")
    marker = s3.mark_deleted(lost)
    out = tmp_path / "keys.txt"

    _, lines = run(s3, "--restore", "--write-keys", str(out))
    assert s3.is_live(lost)
    assert not s3.is_live(safe)  # a safe delete stays deleted
    assert s3.deleted_version_ids == [marker]
    assert 'RESTORED {"removed_marker": 1}' in lines
    assert out.read_text() == f"{lost}\n"


def test_restore_copies_a_mislabeled_raw_to_the_key_its_bytes_belong_at() -> None:
    s3 = VersionedS3()
    body = b"mislabeled and unique"
    key = OC + f"{sha(b'synthetic')}.pdf"
    s3.put(key, body, {"content-hash": sha(body), "scraper-id": "ca-oc"})
    s3.mark_deleted(key)

    _, lines = run(s3, "--restore", "--write-keys", "s3://archive/tmp/keys/r.txt")
    target = OC + f"{sha(body)}.pdf"
    assert s3.is_live(target)
    assert not s3.is_live(key)  # the mislabeled name stays deleted
    assert s3.deleted_version_ids == []  # no version was deleted
    restored = s3.latest(target)
    assert restored is not None and restored["body"] == body
    assert restored["metadata"]["restored-from"].startswith(key + "?versionId=")
    assert s3.puts["archive/tmp/keys/r.txt"] == f"{target}\n".encode()
    assert 'RESTORED {"copied": 1}' in lines


def test_restore_never_overwrites_an_existing_target() -> None:
    s3 = VersionedS3()
    body = b"bytes"
    key = OC + f"{sha(b'synthetic')}.pdf"
    s3.put(key, body)
    s3.mark_deleted(key)
    v = audit.Verdict(
        audit.Deleted(
            key, "orange", sha(b"synthetic"), "pdf", "dm", s3.latest(key)["LastModified"], "v1"
        ),
        "no_live_copy",
        "mislabel",
        sha256=sha(body),
        size=len(body),
    )
    s3.put(v.restore_key, b"already here")
    assert audit.restore(s3, B, v) == "exists"
    assert s3.latest(v.restore_key)["body"] == b"already here"


def test_restore_errors_are_reported_and_do_not_stop_the_run() -> None:
    class Broken(VersionedS3):
        def delete_object(self, **kw: object) -> NoReturn:  # type: ignore[override]
            raise ClientError("AccessDenied")

    s3 = Broken()
    key = OC + f"{sha(b'u')}.pdf"
    s3.put(key, b"u")
    s3.mark_deleted(key)
    _, lines = run(s3, "--restore")
    assert any(x.startswith(f"RESTORE_ERROR key={key}") for x in lines)
    assert 'RESTORED {"error": 1}' in lines


def test_classify_errors_are_counted() -> None:
    class Denied(VersionedS3):
        def get_object(self, **kw: object) -> NoReturn:  # type: ignore[override]
            raise ClientError("AccessDenied")

    s3 = Denied()
    key = OC + f"{sha(b'u')}.pdf"
    s3.put(key, b"u")
    s3.mark_deleted(key)
    _, lines = run(s3)
    assert any(x.startswith(f"ERROR key={key}") for x in lines)
    assert summary(lines, "orange") == {"error": 1}
