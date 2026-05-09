"""Tests for ``framework.s3_keys`` shared S3-key helpers (#4447).

Covers each pure helper in isolation:
- ``parse_flat_hash_key`` for valid, malformed, and edge-case keys
- ``is_mislabel`` for matching/mismatching/missing-metadata cases
- ``head_object_metadata_hash`` with mocked S3 client (200, 404, missing field)
- ``build_twin_key`` for valid and non-parseable inputs

The script-side tests
(``scripts/tests/test_cleanup_mislabeled_s3_2661.py``,
``scripts/tests/test_repoint_mislabeled_documents_4439.py``) re-verify the
helpers end-to-end through the script's enumerate / repoint flow; the
unit tests below verify the helper contracts at the module boundary so a
future change to the helpers can be detected without booting the script
mocks.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from framework.s3_keys import (
    KEY_PATTERN,
    build_twin_key,
    head_object_metadata_hash,
    is_mislabel,
    parse_flat_hash_key,
)

HEX64_A = "a" * 64
HEX64_B = "b" * 64
HEX64_C = "c" * 64


# ---------------------------------------------------------------------------
# parse_flat_hash_key
# ---------------------------------------------------------------------------


class TestParseFlatHashKey:
    def test_valid_pdf(self) -> None:
        result = parse_flat_hash_key(f"ca/orange/superior_court/raw/{HEX64_A}.pdf")
        assert result == {
            "state": "ca",
            "county": "orange",
            "court": "superior_court",
            "hash": HEX64_A,
            "ext": "pdf",
        }

    def test_valid_html(self) -> None:
        result = parse_flat_hash_key(f"ca/santa_clara/superior_court/raw/{HEX64_B}.html")
        assert result is not None
        assert result["county"] == "santa_clara"
        assert result["ext"] == "html"

    def test_valid_docx(self) -> None:
        result = parse_flat_hash_key(f"ca/los_angeles/superior_court/raw/{HEX64_A}.docx")
        assert result is not None
        assert result["ext"] == "docx"

    def test_valid_txt(self) -> None:
        result = parse_flat_hash_key(f"ca/orange/superior_court/raw/{HEX64_C}.txt")
        assert result is not None
        assert result["ext"] == "txt"

    def test_date_partitioned_key_returns_none(self) -> None:
        # Legacy date-partitioned keys are NOT in scope for the flat-hash
        # cleanup/repoint flows — those go through different scripts.
        result = parse_flat_hash_key("ca/orange/superior_court/raw/2026/04/01/uuid.pdf")
        assert result is None

    def test_short_hash_returns_none(self) -> None:
        # 63 chars instead of 64.
        result = parse_flat_hash_key(f"ca/orange/superior_court/raw/{'a' * 63}.pdf")
        assert result is None

    def test_uppercase_hash_returns_none(self) -> None:
        # KEY_PATTERN requires lowercase hex — uppercase hex is intentionally
        # rejected so audit callers do not silently treat capitalised legacy
        # keys as flat-hash matches.
        result = parse_flat_hash_key(f"ca/orange/superior_court/raw/{'A' * 64}.pdf")
        assert result is None

    def test_non_raw_path_returns_none(self) -> None:
        # ``transcripts/`` and other path segments are not in scope.
        result = parse_flat_hash_key(f"ca/orange/superior_court/transcripts/{HEX64_A}.txt")
        assert result is None

    def test_court_directory_returns_none(self) -> None:
        # ``court_directory`` snapshots and other non-document paths must not match.
        result = parse_flat_hash_key("court_directory/ca/orange/snapshot.json")
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        assert parse_flat_hash_key("") is None

    def test_two_letter_state(self) -> None:
        # Only two-letter state codes (per KEY_PATTERN). Three-letter must fail.
        result = parse_flat_hash_key(f"cal/orange/superior_court/raw/{HEX64_A}.pdf")
        assert result is None


# ---------------------------------------------------------------------------
# is_mislabel
# ---------------------------------------------------------------------------


class TestIsMislabel:
    def test_matching_hashes_not_mislabel(self) -> None:
        assert is_mislabel(HEX64_A, HEX64_A) is False

    def test_differing_hashes_is_mislabel(self) -> None:
        assert is_mislabel(HEX64_A, HEX64_B) is True

    def test_missing_metadata_not_mislabel(self) -> None:
        # Missing metadata is a separate problem class — handled by the
        # audit script, not by the cleanup/repoint flows.
        assert is_mislabel(HEX64_A, None) is False

    def test_empty_metadata_not_mislabel(self) -> None:
        assert is_mislabel(HEX64_A, "") is False


# ---------------------------------------------------------------------------
# head_object_metadata_hash
# ---------------------------------------------------------------------------


class TestHeadObjectMetadataHash:
    def test_returns_hash_when_present(self) -> None:
        s3 = MagicMock()
        s3.head_object.return_value = {"Metadata": {"content-hash": HEX64_A}}
        assert head_object_metadata_hash(s3, "bucket", "key") == HEX64_A

    def test_returns_none_when_metadata_missing(self) -> None:
        s3 = MagicMock()
        s3.head_object.return_value = {"Metadata": {}}
        assert head_object_metadata_hash(s3, "bucket", "key") is None

    def test_returns_none_when_metadata_field_absent(self) -> None:
        s3 = MagicMock()
        s3.head_object.return_value = {}
        assert head_object_metadata_hash(s3, "bucket", "key") is None

    def test_returns_none_when_object_404(self) -> None:
        s3 = MagicMock()
        s3.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
        assert head_object_metadata_hash(s3, "bucket", "key") is None

    def test_returns_none_when_object_404_via_no_such_key(self) -> None:
        s3 = MagicMock()
        s3.head_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        assert head_object_metadata_hash(s3, "bucket", "key") is None

    def test_returns_none_when_object_404_via_not_found(self) -> None:
        s3 = MagicMock()
        s3.head_object.side_effect = ClientError({"Error": {"Code": "NotFound"}}, "HeadObject")
        assert head_object_metadata_hash(s3, "bucket", "key") is None

    def test_propagates_other_errors(self) -> None:
        # AccessDenied is not "not found" — it surfaces a real problem
        # (IAM, bucket policy) and must propagate.
        s3 = MagicMock()
        s3.head_object.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject")
        with pytest.raises(ClientError):
            head_object_metadata_hash(s3, "bucket", "key")


# ---------------------------------------------------------------------------
# build_twin_key
# ---------------------------------------------------------------------------


class TestBuildTwinKey:
    def test_valid_mislabel_returns_twin(self) -> None:
        mislabel = f"ca/orange/superior_court/raw/{HEX64_A}.pdf"
        result = build_twin_key(mislabel, HEX64_B)
        assert result == f"ca/orange/superior_court/raw/{HEX64_B}.pdf"

    def test_preserves_extension(self) -> None:
        mislabel = f"ca/santa_clara/superior_court/raw/{HEX64_A}.html"
        result = build_twin_key(mislabel, HEX64_B)
        assert result == f"ca/santa_clara/superior_court/raw/{HEX64_B}.html"

    def test_preserves_state_county_court(self) -> None:
        mislabel = f"ca/los_angeles/family_court/raw/{HEX64_A}.docx"
        result = build_twin_key(mislabel, HEX64_C)
        assert result == f"ca/los_angeles/family_court/raw/{HEX64_C}.docx"

    def test_unparseable_key_returns_none(self) -> None:
        # Date-partitioned legacy keys do not parse — cannot build twin.
        result = build_twin_key("ca/orange/superior_court/raw/2026/04/01/x.pdf", HEX64_A)
        assert result is None

    def test_empty_key_returns_none(self) -> None:
        assert build_twin_key("", HEX64_A) is None


# ---------------------------------------------------------------------------
# KEY_PATTERN (regex contract)
# ---------------------------------------------------------------------------


class TestKeyPattern:
    def test_named_groups_match(self) -> None:
        m = KEY_PATTERN.match(f"ca/orange/superior_court/raw/{HEX64_A}.pdf")
        assert m is not None
        assert m.group("state") == "ca"
        assert m.group("county") == "orange"
        assert m.group("court") == "superior_court"
        assert m.group("hash") == HEX64_A
        assert m.group("ext") == "pdf"

    def test_county_can_contain_underscore(self) -> None:
        m = KEY_PATTERN.match(f"ca/santa_clara/superior_court/raw/{HEX64_A}.pdf")
        assert m is not None
        assert m.group("county") == "santa_clara"
