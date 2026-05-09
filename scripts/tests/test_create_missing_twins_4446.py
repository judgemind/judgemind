"""Tests for create_missing_twins_4446 script.

Tests cover:
- parse_flat_hash_key for valid, malformed, edge-case keys
  (re-exported from ``framework.s3_keys`` after #4447 / #4455)
- is_mislabel for matching/mismatching/missing-metadata cases
  (re-exported from ``framework.s3_keys`` after #4447 / #4455)
- build_twin_key for valid + non-parseable inputs
  (re-exported from ``framework.s3_keys`` after #4447 / #4455)
- head_object_metadata_hash with mocked S3 client (200, 404, missing field)
  (re-exported from ``framework.s3_keys`` after #4447 / #4455)
- verify_twin: valid, missing, invalid twin
- enumerate_mislabel_pairs end-to-end with mocked paginator + HEAD
  (records valid/missing/invalid twin states)
- create_twin: each return-status branch (created, already_exists,
  collision_skipped, verify_failed, error, skipped_non_missing)
- create_twins_for_county: dry-run, missing-only filter, per-status counters
- run_create orchestration: empty, dry-run, apply with happy path + errors
- main CLI: --dry-run default, --apply switch, error/verify_failed exit code
- report_enumeration / report_creation smoke tests

The script imports boto3 + structlog + framework.* at module level, which
may not be installed in the CI ``scripts-tests`` environment. We use
``tests._mock_helpers.mock_sys_modules`` (the canonical pattern as of
#4430) to inject MagicMocks into sys.modules around the import — the
helper restores sys.modules on exit so other tests in the same pytest run
don't see the leaked mocks (#4426).

After #4455, the small pure helpers (``parse_flat_hash_key``,
``is_mislabel``, ``head_object_metadata_hash``, ``build_twin_key``,
``KEY_PATTERN``) live in ``framework.s3_keys``. We load that real module
via ``importlib`` from its source path inside the mock_sys_modules block
so the script imports the genuine implementation rather than a MagicMock
attribute. Mirrors the recipe in
``test_repoint_mislabeled_documents_4439.py`` (#4447).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add scripts/ to sys.path so the script-under-test imports cleanly, and
# scripts/tests/ to sys.path so the helper module is reachable as
# ``tests._mock_helpers``.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests._mock_helpers import mock_sys_modules  # noqa: E402


# ClientError needs a real exception class so `except ClientError` works in
# the script under test. We define a minimal subclass that mirrors the
# botocore ClientError signature (response dict + operation_name).
class _FakeClientError(Exception):
    def __init__(self, response: dict, operation_name: str = "Unknown"):
        super().__init__(f"{operation_name}: {response}")
        self.response = response
        self.operation_name = operation_name


_mock_botocore = MagicMock()
_mock_botocore_exceptions = MagicMock()
_mock_botocore_exceptions.ClientError = _FakeClientError
_mock_botocore.exceptions = _mock_botocore_exceptions

_mock_structlog = MagicMock()
_mock_structlog.get_logger.return_value = MagicMock()


def _load_real_s3_keys() -> object:
    """Load ``framework.s3_keys`` from source so the script-under-test
    imports the real helpers, not a MagicMock attribute. Pure Python over
    ``re`` and ``botocore.exceptions.ClientError``; invoked INSIDE the
    ``mock_sys_modules`` block so s3_keys' import of ``ClientError``
    resolves to ``_FakeClientError`` — keeping the ``except ClientError``
    branch reachable from tests that raise ``_FakeClientError``."""
    s3_keys_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "packages",
        "scraper-framework",
        "src",
        "framework",
        "s3_keys.py",
    )
    spec = importlib.util.spec_from_file_location("framework.s3_keys", s3_keys_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


with mock_sys_modules(
    {
        "boto3": MagicMock(),
        "botocore": _mock_botocore,
        "botocore.exceptions": _mock_botocore_exceptions,
        "structlog": _mock_structlog,
        "framework": MagicMock(),
        "framework.logging": MagicMock(),
        # Placeholder — replaced below with the real loaded module so the
        # ``mock_sys_modules`` restore path cleans it up on exit.
        "framework.s3_keys": MagicMock(),
    }
):
    # Replace the placeholder with the genuine ``framework.s3_keys`` module
    # loaded via importlib. The load happens INSIDE the mock context so the
    # ``from botocore.exceptions import ClientError`` inside s3_keys.py
    # resolves to ``_FakeClientError`` — keeping the ``except ClientError``
    # branch reachable from tests that raise ``_FakeClientError``.
    import sys as _sys

    _sys.modules["framework.s3_keys"] = _load_real_s3_keys()
    import create_missing_twins_4446  # noqa: E402

parse_flat_hash_key = create_missing_twins_4446.parse_flat_hash_key
is_mislabel = create_missing_twins_4446.is_mislabel
build_twin_key = create_missing_twins_4446.build_twin_key
head_object_metadata_hash = create_missing_twins_4446.head_object_metadata_hash
verify_twin = create_missing_twins_4446.verify_twin
enumerate_mislabel_pairs = create_missing_twins_4446.enumerate_mislabel_pairs
create_twin = create_missing_twins_4446.create_twin
create_twins_for_county = create_missing_twins_4446.create_twins_for_county
report_enumeration = create_missing_twins_4446.report_enumeration
report_creation = create_missing_twins_4446.report_creation
run_create = create_missing_twins_4446.run_create
build_parser = create_missing_twins_4446.build_parser
main = create_missing_twins_4446.main
ClientError = create_missing_twins_4446.ClientError


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


HEX64_A = "a" * 64
HEX64_B = "b" * 64
HEX64_C = "c" * 64
HEX64_D = "d" * 64


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

    def test_short_hash_returns_none(self) -> None:
        result = parse_flat_hash_key(f"ca/orange/superior_court/raw/{'a' * 63}.pdf")
        assert result is None

    def test_uppercase_hash_returns_none(self) -> None:
        result = parse_flat_hash_key(f"ca/orange/superior_court/raw/{'A' * 64}.pdf")
        assert result is None

    def test_date_partitioned_key_returns_none(self) -> None:
        result = parse_flat_hash_key("ca/orange/superior_court/raw/2026/04/01/uuid.pdf")
        assert result is None

    def test_non_raw_path_returns_none(self) -> None:
        result = parse_flat_hash_key(
            f"ca/orange/superior_court/transcripts/{HEX64_A}.txt"
        )
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        assert parse_flat_hash_key("") is None


# ---------------------------------------------------------------------------
# is_mislabel
# ---------------------------------------------------------------------------


class TestIsMislabel:
    def test_matching_hashes_not_mislabel(self) -> None:
        assert is_mislabel(HEX64_A, HEX64_A) is False

    def test_differing_hashes_is_mislabel(self) -> None:
        assert is_mislabel(HEX64_A, HEX64_B) is True

    def test_missing_metadata_not_mislabel(self) -> None:
        assert is_mislabel(HEX64_A, None) is False

    def test_empty_metadata_not_mislabel(self) -> None:
        assert is_mislabel(HEX64_A, "") is False


# ---------------------------------------------------------------------------
# build_twin_key
# ---------------------------------------------------------------------------


class TestBuildTwinKey:
    def test_replaces_filename_hash_preserves_rest(self) -> None:
        mislabel = f"ca/orange/superior_court/raw/{HEX64_A}.pdf"
        twin = build_twin_key(mislabel, HEX64_B)
        assert twin == f"ca/orange/superior_court/raw/{HEX64_B}.pdf"

    def test_preserves_extension_html(self) -> None:
        mislabel = f"ca/santa_clara/superior_court/raw/{HEX64_A}.html"
        twin = build_twin_key(mislabel, HEX64_C)
        assert twin == f"ca/santa_clara/superior_court/raw/{HEX64_C}.html"

    def test_preserves_court_segment(self) -> None:
        mislabel = f"ca/los_angeles/some_court/raw/{HEX64_A}.docx"
        twin = build_twin_key(mislabel, HEX64_D)
        assert twin == f"ca/los_angeles/some_court/raw/{HEX64_D}.docx"

    def test_returns_none_for_non_parseable(self) -> None:
        # Date-partitioned legacy key — does not match KEY_PATTERN.
        assert (
            build_twin_key("ca/orange/superior_court/raw/2026/04/01/x.pdf", HEX64_A)
            is None
        )

    def test_returns_none_for_empty(self) -> None:
        assert build_twin_key("", HEX64_A) is None


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
        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404"}}, "HeadObject"
        )
        assert head_object_metadata_hash(s3, "bucket", "key") is None

    def test_returns_none_when_no_such_key(self) -> None:
        s3 = MagicMock()
        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey"}}, "HeadObject"
        )
        assert head_object_metadata_hash(s3, "bucket", "key") is None

    def test_propagates_other_errors(self) -> None:
        s3 = MagicMock()
        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "HeadObject"
        )
        with pytest.raises(ClientError):
            head_object_metadata_hash(s3, "bucket", "key")


# ---------------------------------------------------------------------------
# verify_twin
# ---------------------------------------------------------------------------


class TestVerifyTwin:
    def test_valid_twin_returns_true(self) -> None:
        # Twin filename hash == twin metadata hash → valid.
        s3 = MagicMock()
        s3.head_object.return_value = {"Metadata": {"content-hash": HEX64_B}}
        twin_key = f"ca/orange/superior_court/raw/{HEX64_B}.pdf"
        assert verify_twin(s3, "bucket", twin_key) is True

    def test_missing_twin_returns_false(self) -> None:
        s3 = MagicMock()
        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404"}}, "HeadObject"
        )
        twin_key = f"ca/orange/superior_court/raw/{HEX64_B}.pdf"
        assert verify_twin(s3, "bucket", twin_key) is False

    def test_invalid_twin_returns_false(self) -> None:
        # Twin exists but its filename hash != its own metadata hash → invalid.
        s3 = MagicMock()
        s3.head_object.return_value = {"Metadata": {"content-hash": HEX64_C}}
        twin_key = f"ca/orange/superior_court/raw/{HEX64_B}.pdf"
        assert verify_twin(s3, "bucket", twin_key) is False

    def test_twin_with_no_metadata_returns_false(self) -> None:
        s3 = MagicMock()
        s3.head_object.return_value = {"Metadata": {}}
        twin_key = f"ca/orange/superior_court/raw/{HEX64_B}.pdf"
        assert verify_twin(s3, "bucket", twin_key) is False

    def test_non_parseable_twin_returns_false(self) -> None:
        s3 = MagicMock()
        # No HEAD should occur for a key that doesn't even parse.
        assert verify_twin(s3, "bucket", "garbage") is False
        s3.head_object.assert_not_called()


# ---------------------------------------------------------------------------
# enumerate_mislabel_pairs
# ---------------------------------------------------------------------------


def _mock_paginator(pages: list[list[dict[str, str]]]) -> MagicMock:
    """Build a mock S3 paginator that yields *pages*, each a list of object
    dicts with a "Key" field."""
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": page} for page in pages]
    return paginator


class TestEnumerateMislabelPairs:
    def test_no_objects_returns_empty(self) -> None:
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator([[]])
        result = enumerate_mislabel_pairs(s3, "bucket", "ca/")
        assert result == {}

    def test_correctly_labeled_skipped(self) -> None:
        # filename == metadata: not a mislabel, no pair recorded.
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator(
            [[{"Key": f"ca/orange/superior_court/raw/{HEX64_A}.pdf"}]]
        )
        s3.head_object.return_value = {"Metadata": {"content-hash": HEX64_A}}
        result = enumerate_mislabel_pairs(s3, "bucket", "ca/")
        assert result == {}

    def test_mislabel_with_valid_twin_recorded(self) -> None:
        mislabel_key = f"ca/orange/superior_court/raw/{HEX64_A}.pdf"
        twin_key = f"ca/orange/superior_court/raw/{HEX64_B}.pdf"
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator([[{"Key": mislabel_key}]])

        def fake_head(*, Bucket: str, Key: str) -> dict:
            if Key == mislabel_key:
                return {"Metadata": {"content-hash": HEX64_B}}
            if Key == twin_key:
                # Twin's filename HEX64_B == metadata HEX64_B — valid.
                return {"Metadata": {"content-hash": HEX64_B}}
            raise AssertionError(f"unexpected HEAD on {Key}")

        s3.head_object.side_effect = fake_head
        result = enumerate_mislabel_pairs(s3, "bucket", "ca/")
        assert "orange" in result
        assert len(result["orange"]) == 1
        record = result["orange"][0]
        assert record["mislabel_key"] == mislabel_key
        assert record["twin_key"] == twin_key
        assert record["filename_hash"] == HEX64_A
        assert record["metadata_hash"] == HEX64_B
        assert record["twin_status"] == "valid"

    def test_mislabel_with_missing_twin_recorded(self) -> None:
        mislabel_key = f"ca/orange/superior_court/raw/{HEX64_A}.pdf"
        twin_key = f"ca/orange/superior_court/raw/{HEX64_B}.pdf"
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator([[{"Key": mislabel_key}]])

        def fake_head(*, Bucket: str, Key: str) -> dict:
            if Key == mislabel_key:
                return {"Metadata": {"content-hash": HEX64_B}}
            if Key == twin_key:
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            raise AssertionError(f"unexpected HEAD on {Key}")

        s3.head_object.side_effect = fake_head
        result = enumerate_mislabel_pairs(s3, "bucket", "ca/")
        assert len(result["orange"]) == 1
        assert result["orange"][0]["twin_status"] == "missing"

    def test_mislabel_with_invalid_twin_recorded(self) -> None:
        # Twin exists but its metadata disagrees with its filename → invalid.
        mislabel_key = f"ca/orange/superior_court/raw/{HEX64_A}.pdf"
        twin_key = f"ca/orange/superior_court/raw/{HEX64_B}.pdf"
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator([[{"Key": mislabel_key}]])

        def fake_head(*, Bucket: str, Key: str) -> dict:
            if Key == mislabel_key:
                return {"Metadata": {"content-hash": HEX64_B}}
            if Key == twin_key:
                # Twin claims content is HEX64_C, not HEX64_B — itself mislabeled.
                return {"Metadata": {"content-hash": HEX64_C}}
            raise AssertionError(f"unexpected HEAD on {Key}")

        s3.head_object.side_effect = fake_head
        result = enumerate_mislabel_pairs(s3, "bucket", "ca/")
        assert result["orange"][0]["twin_status"] == "invalid"

    def test_groups_by_county(self) -> None:
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator(
            [
                [
                    {"Key": f"ca/orange/superior_court/raw/{HEX64_A}.pdf"},
                    {"Key": f"ca/santa_clara/superior_court/raw/{HEX64_A}.pdf"},
                ]
            ]
        )

        def fake_head(*, Bucket: str, Key: str) -> dict:
            # Every mislabel with metadata HEX64_B; every twin valid.
            if HEX64_A in Key:
                return {"Metadata": {"content-hash": HEX64_B}}
            if HEX64_B in Key:
                return {"Metadata": {"content-hash": HEX64_B}}
            return {"Metadata": {}}

        s3.head_object.side_effect = fake_head
        result = enumerate_mislabel_pairs(s3, "bucket", "ca/")
        assert sorted(result.keys()) == ["orange", "santa_clara"]

    def test_skips_non_matching_shape(self) -> None:
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator(
            [
                [
                    {"Key": "ca/orange/superior_court/raw/2026/04/01/x.pdf"},
                    {"Key": "court_directory/snapshot.json"},
                ]
            ]
        )
        result = enumerate_mislabel_pairs(s3, "bucket", "ca/")
        assert result == {}
        s3.head_object.assert_not_called()

    def test_skips_objects_with_missing_metadata(self) -> None:
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator(
            [[{"Key": f"ca/orange/superior_court/raw/{HEX64_A}.pdf"}]]
        )
        s3.head_object.return_value = {"Metadata": {}}
        result = enumerate_mislabel_pairs(s3, "bucket", "ca/")
        assert result == {}


# ---------------------------------------------------------------------------
# create_twin
# ---------------------------------------------------------------------------


def _missing_pair() -> dict[str, str]:
    return {
        "mislabel_key": f"ca/orange/superior_court/raw/{HEX64_A}.pdf",
        "twin_key": f"ca/orange/superior_court/raw/{HEX64_B}.pdf",
        "filename_hash": HEX64_A,
        "metadata_hash": HEX64_B,
        "twin_status": "missing",
    }


class TestCreateTwin:
    def test_skipped_for_non_missing_status(self) -> None:
        s3 = MagicMock()
        pair = dict(_missing_pair())
        pair["twin_status"] = "valid"
        assert create_twin(s3, "bucket", pair) == "skipped_non_missing"
        s3.head_object.assert_not_called()
        s3.copy_object.assert_not_called()

    def test_happy_path_creates_and_verifies(self) -> None:
        # Pre-HEAD: 404 (missing). copy_object: succeeds. Post-HEAD: matches.
        s3 = MagicMock()
        pair = _missing_pair()

        head_calls = {"n": 0}

        def fake_head(*, Bucket: str, Key: str) -> dict:
            head_calls["n"] += 1
            if head_calls["n"] == 1:
                # Pre-HEAD: twin is missing.
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            # Post-HEAD: new twin is correctly-labelled.
            return {"Metadata": {"content-hash": HEX64_B}}

        s3.head_object.side_effect = fake_head
        result = create_twin(s3, "bucket", pair)
        assert result == "created"
        s3.copy_object.assert_called_once_with(
            Bucket="bucket",
            CopySource={"Bucket": "bucket", "Key": pair["mislabel_key"]},
            Key=pair["twin_key"],
        )
        assert s3.head_object.call_count == 2

    def test_already_exists_skips_copy(self) -> None:
        # Pre-HEAD: twin exists AND is correctly-labelled — idempotent skip.
        s3 = MagicMock()
        pair = _missing_pair()
        s3.head_object.return_value = {"Metadata": {"content-hash": HEX64_B}}
        result = create_twin(s3, "bucket", pair)
        assert result == "already_exists"
        s3.copy_object.assert_not_called()
        # Only the pre-HEAD ran.
        assert s3.head_object.call_count == 1

    def test_collision_skipped_when_twin_mislabeled(self) -> None:
        # Pre-HEAD: twin exists but metadata != filename → another mislabel.
        s3 = MagicMock()
        pair = _missing_pair()
        s3.head_object.return_value = {"Metadata": {"content-hash": HEX64_C}}
        result = create_twin(s3, "bucket", pair)
        assert result == "collision_skipped"
        s3.copy_object.assert_not_called()

    def test_verify_failed_when_post_head_mismatch(self) -> None:
        # Pre-HEAD: 404. copy_object: succeeds. Post-HEAD: returns wrong hash.
        s3 = MagicMock()
        pair = _missing_pair()

        head_calls = {"n": 0}

        def fake_head(*, Bucket: str, Key: str) -> dict:
            head_calls["n"] += 1
            if head_calls["n"] == 1:
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            # Post-HEAD: bytes ended up labelled wrong somehow.
            return {"Metadata": {"content-hash": HEX64_C}}

        s3.head_object.side_effect = fake_head
        result = create_twin(s3, "bucket", pair)
        assert result == "verify_failed"
        s3.copy_object.assert_called_once()

    def test_verify_failed_when_post_head_missing(self) -> None:
        # Pre-HEAD: 404. copy_object: succeeds. Post-HEAD: also 404 (eventual
        # consistency / something deleted it). Treat as verify_failed.
        s3 = MagicMock()
        pair = _missing_pair()

        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404"}}, "HeadObject"
        )
        result = create_twin(s3, "bucket", pair)
        assert result == "verify_failed"
        s3.copy_object.assert_called_once()

    def test_error_when_copy_object_raises(self) -> None:
        # Pre-HEAD: 404. copy_object: raises non-404 ClientError.
        s3 = MagicMock()
        pair = _missing_pair()
        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404"}}, "HeadObject"
        )
        s3.copy_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "CopyObject"
        )
        result = create_twin(s3, "bucket", pair)
        assert result == "error"

    def test_error_when_pre_head_raises_non_404(self) -> None:
        s3 = MagicMock()
        pair = _missing_pair()
        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "HeadObject"
        )
        result = create_twin(s3, "bucket", pair)
        assert result == "error"
        s3.copy_object.assert_not_called()

    def test_error_when_post_head_raises_non_404(self) -> None:
        # Pre-HEAD: 404. copy_object: succeeds. Post-HEAD: raises AccessDenied.
        s3 = MagicMock()
        pair = _missing_pair()

        head_calls = {"n": 0}

        def fake_head(*, Bucket: str, Key: str) -> dict:
            head_calls["n"] += 1
            if head_calls["n"] == 1:
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject")

        s3.head_object.side_effect = fake_head
        result = create_twin(s3, "bucket", pair)
        assert result == "error"
        s3.copy_object.assert_called_once()


# ---------------------------------------------------------------------------
# create_twins_for_county
# ---------------------------------------------------------------------------


class TestCreateTwinsForCounty:
    def test_no_missing_pairs_returns_zero(self) -> None:
        s3 = MagicMock()
        pairs = [
            {
                "mislabel_key": "k1",
                "twin_key": "k1t",
                "filename_hash": HEX64_A,
                "metadata_hash": HEX64_B,
                "twin_status": "valid",
            }
        ]
        result = create_twins_for_county(
            s3, pairs, county="orange", bucket="b", dry_run=False
        )
        assert result["planned"] == 0
        assert result["created"] == 0
        assert result["skipped_valid"] == 1
        s3.head_object.assert_not_called()
        s3.copy_object.assert_not_called()

    def test_dry_run_does_not_write(self) -> None:
        s3 = MagicMock()
        pairs = [
            _missing_pair(),
            {
                "mislabel_key": "k2",
                "twin_key": "k2t",
                "filename_hash": HEX64_C,
                "metadata_hash": HEX64_D,
                "twin_status": "invalid",
            },
        ]
        result = create_twins_for_county(
            s3, pairs, county="orange", bucket="b", dry_run=True
        )
        assert result["planned"] == 1
        assert result["created"] == 0
        assert result["skipped_invalid"] == 1
        s3.head_object.assert_not_called()
        s3.copy_object.assert_not_called()

    def test_apply_aggregates_counters(self) -> None:
        # Build three missing pairs and walk them through three different
        # outcomes via head_object/copy_object stubs:
        #   pair1 → created (pre-404, copy ok, post-match)
        #   pair2 → already_exists (pre-HEAD shows valid twin)
        #   pair3 → verify_failed (pre-404, copy ok, post-mismatch)
        s3 = MagicMock()

        pair1 = {
            "mislabel_key": "ca/orange/sc/raw/" + ("1" * 64) + ".pdf",
            "twin_key": "ca/orange/sc/raw/" + ("2" * 64) + ".pdf",
            "filename_hash": "1" * 64,
            "metadata_hash": "2" * 64,
            "twin_status": "missing",
        }
        pair2 = {
            "mislabel_key": "ca/orange/sc/raw/" + ("3" * 64) + ".pdf",
            "twin_key": "ca/orange/sc/raw/" + ("4" * 64) + ".pdf",
            "filename_hash": "3" * 64,
            "metadata_hash": "4" * 64,
            "twin_status": "missing",
        }
        pair3 = {
            "mislabel_key": "ca/orange/sc/raw/" + ("5" * 64) + ".pdf",
            "twin_key": "ca/orange/sc/raw/" + ("6" * 64) + ".pdf",
            "filename_hash": "5" * 64,
            "metadata_hash": "6" * 64,
            "twin_status": "missing",
        }

        # Track per-key call counts so post-HEAD differs from pre-HEAD.
        head_calls: dict[str, int] = {}

        def fake_head(*, Bucket: str, Key: str) -> dict:
            head_calls[Key] = head_calls.get(Key, 0) + 1
            n = head_calls[Key]
            if Key == pair1["twin_key"]:
                if n == 1:
                    raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
                # Post-HEAD: correct.
                return {"Metadata": {"content-hash": "2" * 64}}
            if Key == pair2["twin_key"]:
                # Pre-HEAD: twin already exists, correctly-labelled.
                return {"Metadata": {"content-hash": "4" * 64}}
            if Key == pair3["twin_key"]:
                if n == 1:
                    raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
                # Post-HEAD: filename != metadata (mismatch).
                return {"Metadata": {"content-hash": "f" * 64}}
            raise AssertionError(f"unexpected HEAD on {Key}")

        s3.head_object.side_effect = fake_head

        result = create_twins_for_county(
            s3, [pair1, pair2, pair3], county="orange", bucket="b", dry_run=False
        )

        assert result["planned"] == 3
        assert result["created"] == 1
        assert result["already_exists"] == 1
        assert result["verify_failed"] == 1
        assert result["errors"] == 0
        assert result["collision_skipped"] == 0

    def test_apply_aggregates_collision_and_error(self) -> None:
        # Aggregator-level coverage for collision_skipped + errors counters.
        s3 = MagicMock()

        # Pair1 → collision_skipped (pre-HEAD shows mislabeled twin).
        pair1 = {
            "mislabel_key": "ca/orange/sc/raw/" + ("1" * 64) + ".pdf",
            "twin_key": "ca/orange/sc/raw/" + ("2" * 64) + ".pdf",
            "filename_hash": "1" * 64,
            "metadata_hash": "2" * 64,
            "twin_status": "missing",
        }
        # Pair2 → error (pre-HEAD raises non-404).
        pair2 = {
            "mislabel_key": "ca/orange/sc/raw/" + ("3" * 64) + ".pdf",
            "twin_key": "ca/orange/sc/raw/" + ("4" * 64) + ".pdf",
            "filename_hash": "3" * 64,
            "metadata_hash": "4" * 64,
            "twin_status": "missing",
        }

        def fake_head(*, Bucket: str, Key: str) -> dict:
            if Key == pair1["twin_key"]:
                # Pre-HEAD: twin exists but its metadata != filename.
                return {"Metadata": {"content-hash": "f" * 64}}
            if Key == pair2["twin_key"]:
                raise ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject")
            raise AssertionError(f"unexpected HEAD on {Key}")

        s3.head_object.side_effect = fake_head

        result = create_twins_for_county(
            s3, [pair1, pair2], county="orange", bucket="b", dry_run=False
        )

        assert result["planned"] == 2
        assert result["collision_skipped"] == 1
        assert result["errors"] == 1
        assert result["created"] == 0
        s3.copy_object.assert_not_called()

    def test_dry_run_truncates_sample_log(self) -> None:
        # Drive the SAMPLE_SIZE truncation log line in the dry-run branch.
        s3 = MagicMock()
        pairs = [
            {
                "mislabel_key": f"ca/orange/sc/raw/{format(i, 'x'):>064}.pdf",
                "twin_key": f"ca/orange/sc/raw/{format(i + 1000, 'x'):>064}.pdf",
                "filename_hash": format(i, "x").rjust(64, "0"),
                "metadata_hash": format(i + 1000, "x").rjust(64, "0"),
                "twin_status": "missing",
            }
            for i in range(25)  # > SAMPLE_SIZE (20) to trigger the truncate log.
        ]
        result = create_twins_for_county(
            s3, pairs, county="orange", bucket="b", dry_run=True
        )
        assert result["planned"] == 25
        assert result["created"] == 0
        s3.copy_object.assert_not_called()


# ---------------------------------------------------------------------------
# run_create orchestration
# ---------------------------------------------------------------------------


class TestRunCreate:
    def test_no_mislabels_returns_clean(self) -> None:
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator([[]])
        result = run_create(s3, bucket="b", state="ca", county=None, dry_run=True)
        assert result["mislabels_found"] == 0
        assert result["planned"] == 0
        assert result["created"] == 0

    def test_dry_run_with_missing_twin_plans_create(self) -> None:
        mislabel = f"ca/orange/superior_court/raw/{HEX64_A}.pdf"
        twin = f"ca/orange/superior_court/raw/{HEX64_B}.pdf"
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator([[{"Key": mislabel}]])

        def fake_head(*, Bucket: str, Key: str) -> dict:
            if Key == mislabel:
                return {"Metadata": {"content-hash": HEX64_B}}
            if Key == twin:
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            raise AssertionError(f"unexpected HEAD on {Key}")

        s3.head_object.side_effect = fake_head
        result = run_create(s3, bucket="b", state="ca", county=None, dry_run=True)
        assert result["mislabels_found"] == 1
        assert result["missing_twins"] == 1
        assert result["planned"] == 1
        assert result["created"] == 0
        s3.copy_object.assert_not_called()

    def test_county_scoping_uses_specific_prefix(self) -> None:
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator([[]])
        run_create(s3, bucket="b", state="ca", county="orange", dry_run=True)
        s3.get_paginator.return_value.paginate.assert_called_with(
            Bucket="b", Prefix="ca/orange/"
        )

    def test_apply_happy_path_creates_twin(self) -> None:
        mislabel = f"ca/orange/superior_court/raw/{HEX64_A}.pdf"
        twin = f"ca/orange/superior_court/raw/{HEX64_B}.pdf"
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator([[{"Key": mislabel}]])

        # head_object call sequence:
        #   1. enumerate: HEAD on mislabel → metadata = HEX64_B
        #   2. enumerate: HEAD on twin → 404 (missing)
        #   3. create_twin pre-HEAD on twin → 404 (still missing)
        #   4. create_twin post-HEAD on twin → metadata = HEX64_B (verified)
        head_calls: list[tuple[str, str]] = []

        def fake_head(*, Bucket: str, Key: str) -> dict:
            head_calls.append((Bucket, Key))
            if Key == mislabel:
                return {"Metadata": {"content-hash": HEX64_B}}
            # Twin: first two HEADs are missing; third HEAD is post-create
            # verification — return correctly-labelled metadata.
            twin_count = sum(1 for _, k in head_calls if k == twin)
            if twin_count <= 2:
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            return {"Metadata": {"content-hash": HEX64_B}}

        s3.head_object.side_effect = fake_head
        result = run_create(s3, bucket="b", state="ca", county=None, dry_run=False)
        assert result["planned"] == 1
        assert result["created"] == 1
        assert result["errors"] == 0
        assert result["verify_failed"] == 0
        s3.copy_object.assert_called_once()


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_default_is_dry_run(self) -> None:
        args = build_parser().parse_args([])
        assert args.dry_run is True
        assert args.apply is False

    def test_apply_flag(self) -> None:
        args = build_parser().parse_args(["--apply"])
        assert args.apply is True

    def test_county_flag(self) -> None:
        args = build_parser().parse_args(["--county", "santa_clara"])
        assert args.county == "santa_clara"

    def test_state_default(self) -> None:
        args = build_parser().parse_args([])
        assert args.state == "ca"

    def test_bucket_override(self) -> None:
        args = build_parser().parse_args(["--bucket", "my-bucket"])
        assert args.bucket == "my-bucket"


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


class TestMain:
    def test_dry_run_returns_0(self) -> None:
        with patch.object(create_missing_twins_4446, "boto3") as boto3_mock:
            s3 = MagicMock()
            s3.get_paginator.return_value = _mock_paginator([[]])
            boto3_mock.client.return_value = s3
            assert main(["--dry-run"]) == 0

    def test_apply_with_errors_returns_1(self) -> None:
        # One missing-twin pair where copy_object raises AccessDenied.
        mislabel = f"ca/orange/superior_court/raw/{HEX64_A}.pdf"
        twin = f"ca/orange/superior_court/raw/{HEX64_B}.pdf"
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator([[{"Key": mislabel}]])

        def fake_head(*, Bucket: str, Key: str) -> dict:
            if Key == mislabel:
                return {"Metadata": {"content-hash": HEX64_B}}
            if Key == twin:
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            raise AssertionError(f"unexpected HEAD on {Key}")

        s3.head_object.side_effect = fake_head
        s3.copy_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "CopyObject"
        )

        with patch.object(create_missing_twins_4446, "boto3") as boto3_mock:
            boto3_mock.client.return_value = s3
            assert main(["--apply"]) == 1

    def test_apply_with_verify_failed_returns_1(self) -> None:
        # One missing-twin pair: copy succeeds but post-HEAD shows mismatch.
        mislabel = f"ca/orange/superior_court/raw/{HEX64_A}.pdf"
        twin = f"ca/orange/superior_court/raw/{HEX64_B}.pdf"
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator([[{"Key": mislabel}]])

        head_calls: list[tuple[str, str]] = []

        def fake_head(*, Bucket: str, Key: str) -> dict:
            head_calls.append((Bucket, Key))
            if Key == mislabel:
                return {"Metadata": {"content-hash": HEX64_B}}
            twin_count = sum(1 for _, k in head_calls if k == twin)
            if twin_count <= 2:
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            # Post-HEAD: wrong metadata.
            return {"Metadata": {"content-hash": HEX64_C}}

        s3.head_object.side_effect = fake_head

        with patch.object(create_missing_twins_4446, "boto3") as boto3_mock:
            boto3_mock.client.return_value = s3
            assert main(["--apply"]) == 1

    def test_apply_happy_path_returns_0(self) -> None:
        # One missing-twin pair: pre-404, copy ok, post-verified.
        mislabel = f"ca/orange/superior_court/raw/{HEX64_A}.pdf"
        twin = f"ca/orange/superior_court/raw/{HEX64_B}.pdf"
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator([[{"Key": mislabel}]])

        head_calls: list[tuple[str, str]] = []

        def fake_head(*, Bucket: str, Key: str) -> dict:
            head_calls.append((Bucket, Key))
            if Key == mislabel:
                return {"Metadata": {"content-hash": HEX64_B}}
            twin_count = sum(1 for _, k in head_calls if k == twin)
            if twin_count <= 2:
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            return {"Metadata": {"content-hash": HEX64_B}}

        s3.head_object.side_effect = fake_head

        with patch.object(create_missing_twins_4446, "boto3") as boto3_mock:
            boto3_mock.client.return_value = s3
            assert main(["--apply"]) == 0


# ---------------------------------------------------------------------------
# report_enumeration / report_creation (smoke tests)
# ---------------------------------------------------------------------------


class TestReportEnumeration:
    def test_returns_totals(self, capsys: pytest.CaptureFixture[str]) -> None:
        by_county = {
            "orange": [
                {
                    "mislabel_key": "k1",
                    "twin_key": "k1t",
                    "filename_hash": HEX64_A,
                    "metadata_hash": HEX64_B,
                    "twin_status": "valid",
                },
                {
                    "mislabel_key": "k2",
                    "twin_key": "k2t",
                    "filename_hash": HEX64_C,
                    "metadata_hash": HEX64_D,
                    "twin_status": "missing",
                },
            ],
            "santa_clara": [
                {
                    "mislabel_key": "k3",
                    "twin_key": "k3t",
                    "filename_hash": HEX64_A,
                    "metadata_hash": HEX64_B,
                    "twin_status": "invalid",
                }
            ],
        }
        totals = report_enumeration(by_county)
        assert totals["total"] == 3
        assert totals["valid"] == 1
        assert totals["missing"] == 1
        assert totals["invalid"] == 1
        captured = capsys.readouterr()
        assert "orange" in captured.out
        assert "santa_clara" in captured.out
        assert "TOTAL" in captured.out


class TestReportCreation:
    def test_aggregates_per_county(self, capsys: pytest.CaptureFixture[str]) -> None:
        by_county_counters = {
            "orange": {
                "planned": 5,
                "created": 4,
                "already_exists": 1,
                "collision_skipped": 0,
                "verify_failed": 0,
                "errors": 0,
                "skipped_invalid": 0,
                "skipped_valid": 0,
            },
            "santa_clara": {
                "planned": 3,
                "created": 2,
                "already_exists": 0,
                "collision_skipped": 0,
                "verify_failed": 1,
                "errors": 0,
                "skipped_invalid": 0,
                "skipped_valid": 0,
            },
        }
        totals = report_creation(by_county_counters)
        assert totals["planned"] == 8
        assert totals["created"] == 6
        assert totals["already_exists"] == 1
        assert totals["verify_failed"] == 1
        captured = capsys.readouterr()
        assert "orange" in captured.out
        assert "santa_clara" in captured.out
        assert "TOTAL" in captured.out
