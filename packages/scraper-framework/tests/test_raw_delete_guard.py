"""Tests for ``framework.raw_delete_guard`` (#4991): no raw deleted without a live copy."""

from __future__ import annotations

import hashlib
from typing import NoReturn

import pytest
from helpers.versioned_s3 import ClientError, VersionedS3

from framework.raw_delete_guard import (
    UnsafeRawDeleteError,
    delete_raw_with_live_copy,
    find_live_identical_copy,
    index_live_by_hash,
    list_live_keys,
    object_sha256,
    remove_delete_marker,
)

B = "archive"
OC = "ca/orange/superior_court/raw/"
LA = "ca/los_angeles/superior_court/raw/"


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def live_index(s3: VersionedS3) -> dict[str, list[str]]:
    return index_live_by_hash(list_live_keys(s3, B, "ca/"))


def test_object_sha256_reads_a_specific_version() -> None:
    s3 = VersionedS3()
    old = s3.put(OC + "x.pdf", b"old")
    s3.put(OC + "x.pdf", b"new")
    digest, size, resp = object_sha256(s3, B, OC + "x.pdf", old)
    assert (digest, size) == (sha(b"old"), 3)
    assert "Body" not in resp


def test_index_skips_keys_that_are_not_content_addressed() -> None:
    h = sha(b"a")
    idx = index_live_by_hash([OC + f"{h}.pdf", OC + "2026/01/02/u.pdf", "ca/x/derived/y.json"])
    assert dict(idx) == {h: [OC + f"{h}.pdf"]}


def test_list_live_keys_omits_deleted_keys() -> None:
    s3 = VersionedS3()
    s3.put(OC + "a.pdf", b"a")
    s3.put(OC + "b.pdf", b"b")
    s3.mark_deleted(OC + "b.pdf")
    assert list_live_keys(s3, B, "ca/") == [OC + "a.pdf"]


def test_find_live_identical_copy_prefers_same_prefix() -> None:
    s3 = VersionedS3()
    body = b"ruling"
    h = sha(body)
    s3.put(LA + f"{h}.pdf", body)
    s3.put(OC + f"{h}.pdf", body)
    mislabel = OC + f"{sha(b'other')}.pdf"
    s3.put(mislabel, body)
    twin = find_live_identical_copy(s3, B, h, live_index(s3), exclude_key=mislabel)
    assert twin == OC + f"{h}.pdf"


def test_find_live_identical_copy_hashes_the_candidate_bytes() -> None:
    """A key named <sha> whose bytes differ is not a copy (#2661 mislabels)."""
    s3 = VersionedS3()
    body = b"ruling"
    h = sha(body)
    s3.put(OC + f"{h}.pdf", b"different bytes")
    assert find_live_identical_copy(s3, B, h, live_index(s3), exclude_key=OC + "z.pdf") is None


def test_find_live_identical_copy_excludes_the_key_itself() -> None:
    s3 = VersionedS3()
    body = b"only copy"
    key = OC + f"{sha(body)}.pdf"
    s3.put(key, body)
    assert find_live_identical_copy(s3, B, sha(body), live_index(s3), exclude_key=key) is None


def test_find_live_identical_copy_skips_a_candidate_deleted_since_listing() -> None:
    s3 = VersionedS3()
    body = b"ruling"
    h = sha(body)
    s3.put(OC + f"{h}.pdf", body)
    s3.put(LA + f"{h}.pdf", body)
    idx = live_index(s3)
    s3.mark_deleted(OC + f"{h}.pdf")
    assert find_live_identical_copy(s3, B, h, idx, exclude_key=OC + "z.pdf") == LA + f"{h}.pdf"


def test_find_live_identical_copy_raises_other_errors() -> None:
    class Denied(VersionedS3):
        def get_object(self, **kw: object) -> NoReturn:  # type: ignore[override]
            raise ClientError("AccessDenied")

    s3 = Denied()
    s3.put(OC + f"{sha(b'x')}.pdf", b"x")
    with pytest.raises(ClientError):
        find_live_identical_copy(s3, B, sha(b"x"), live_index(s3), exclude_key=OC + "z.pdf")


def test_delete_raw_with_live_copy_deletes_a_duplicate() -> None:
    s3 = VersionedS3()
    body = b"ruling"
    twin_key = OC + f"{sha(body)}.pdf"
    mislabel = OC + f"{sha(b'other')}.pdf"
    s3.put(twin_key, body)
    s3.put(mislabel, body)
    assert delete_raw_with_live_copy(s3, B, mislabel, live_index(s3)) == twin_key
    assert not s3.is_live(mislabel)
    # A plain delete only adds a marker: the bytes stay as a version.
    assert s3.versions[mislabel][0]["body"] == body


def test_delete_raw_with_live_copy_refuses_a_unique_raw() -> None:
    s3 = VersionedS3()
    key = OC + f"{sha(b'unique')}.pdf"
    s3.put(key, b"unique")
    with pytest.raises(UnsafeRawDeleteError, match="no other live object"):
        delete_raw_with_live_copy(s3, B, key, live_index(s3))
    assert s3.is_live(key)


def test_remove_delete_marker_restores_the_previous_version() -> None:
    s3 = VersionedS3()
    key = OC + f"{sha(b'r')}.pdf"
    s3.put(key, b"r")
    marker = s3.mark_deleted(key)
    remove_delete_marker(s3, B, key, marker)
    assert s3.is_live(key)
    assert s3.deleted_version_ids == [marker]


def test_remove_delete_marker_refuses_a_data_version() -> None:
    s3 = VersionedS3()
    key = OC + f"{sha(b'r')}.pdf"
    data_version = s3.put(key, b"r")
    with pytest.raises(UnsafeRawDeleteError, match="not the latest delete marker"):
        remove_delete_marker(s3, B, key, data_version)
    assert s3.deleted_version_ids == []


def test_remove_delete_marker_refuses_an_old_marker() -> None:
    s3 = VersionedS3()
    key = OC + f"{sha(b'r')}.pdf"
    s3.put(key, b"r")
    old_marker = s3.mark_deleted(key)
    s3.put(key, b"r")  # re-archived: the marker is no longer latest
    with pytest.raises(UnsafeRawDeleteError):
        remove_delete_marker(s3, B, key, old_marker)
    assert s3.deleted_version_ids == []


def test_remove_delete_marker_ignores_a_marker_on_a_longer_key() -> None:
    """``Prefix=key`` also lists ``key`` + suffix; only the exact key counts."""
    s3 = VersionedS3()
    key = OC + "abc.pdf"
    s3.put(key, b"r")
    s3.put(key + ".bak", b"r")
    other = s3.mark_deleted(key + ".bak")
    with pytest.raises(UnsafeRawDeleteError):
        remove_delete_marker(s3, B, key, other)
