"""Tests for cleanup_mislabeled_s3_2661 script.

Tests cover:
- parse_flat_hash_key for valid, malformed, and edge-case keys
  (re-exported from ``framework.s3_keys`` after #4447)
- is_mislabel for matching/mismatching/missing-metadata cases
  (re-exported from ``framework.s3_keys`` after #4447)
- head_object_metadata_hash with mocked S3 client (200, 404, missing field)
  (re-exported from ``framework.s3_keys`` after #4447)
- enumerate_mislabels end-to-end with mocked paginator + HEAD
- find_referenced_keys with mocked psycopg connection
- delete_in_batches in dry-run and apply modes, batch boundary
- run_cleanup orchestration: dry-run, apply with abort, apply with success
- main CLI: --dry-run default, --apply switch, missing DATABASE_URL,
  abort exit code propagation

The script imports psycopg + boto3 at module level, which may not be
installed in the CI scripts-tests environment. We mock those in
sys.modules before importing the script under test via the
``mock_sys_modules`` context manager from ``_mock_helpers`` (#4430) —
the helper restores ``sys.modules`` automatically on exit so the mocks
do not leak into later test files in the same pytest process (#4426).

After #4447, the small pure helpers (``parse_flat_hash_key``,
``is_mislabel``, ``head_object_metadata_hash``, ``KEY_PATTERN``) live in
``framework.s3_keys``. We load that real module via ``importlib`` from
its source path inside the mock_sys_modules block so the script under
test imports the genuine implementation rather than a MagicMock — the
helpers are pure-Python over ``re`` and the mocked ``botocore`` ``ClientError``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Pre-import mocking — the script imports boto3 and psycopg at module level,
# which may not be installed in the CI scripts-tests environment.
# ---------------------------------------------------------------------------

# The script-under-test was archived to scripts/archive/ in #4565 after its
# runtime apply landed on dev (#2661 deleted 3734 mislabels 2026-05-09). The
# tests stay in scripts/tests/ to keep running under the existing
# `pytest scripts/tests/` shard — matching the precedent set by
# scripts/archive/cleanup_legacy_date_partitioned_s3.py whose test still lives
# at scripts/tests/test_cleanup_legacy_date_partitioned_s3.py.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "archive"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests._mock_helpers import mock_sys_modules  # noqa: E402

_mock_psycopg = MagicMock()
_mock_boto3 = MagicMock()
_mock_botocore = MagicMock()
_mock_botocore_exceptions = MagicMock()
_mock_structlog = MagicMock()
_mock_structlog.get_logger.return_value = MagicMock()
_mock_framework = MagicMock()
_mock_framework_logging = MagicMock()


# ClientError needs a real exception class so `except ClientError` works in
# the script under test. We define a minimal subclass that mirrors the
# botocore ClientError signature (response dict + operation_name).
class _FakeClientError(Exception):
    def __init__(self, response: dict, operation_name: str = "Unknown"):
        super().__init__(f"{operation_name}: {response}")
        self.response = response
        self.operation_name = operation_name


_mock_botocore_exceptions.ClientError = _FakeClientError
_mock_botocore.exceptions = _mock_botocore_exceptions


def _load_real_s3_keys() -> object:
    """Load ``framework.s3_keys`` from source so the script-under-test
    imports the real helpers, not a MagicMock attribute. The module is
    pure Python over ``re`` and ``botocore.exceptions.ClientError``;
    this function is invoked INSIDE the ``mock_sys_modules`` block so
    s3_keys' ``from botocore.exceptions import ClientError`` resolves to
    the mocked ``_FakeClientError`` — making the ``except ClientError``
    branch in ``head_object_metadata_hash`` reachable from tests that
    raise ``_FakeClientError``."""
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
        "psycopg": _mock_psycopg,
        "boto3": _mock_boto3,
        "botocore": _mock_botocore,
        "botocore.exceptions": _mock_botocore_exceptions,
        "structlog": _mock_structlog,
        "framework": _mock_framework,
        "framework.logging": _mock_framework_logging,
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
    import cleanup_mislabeled_s3_2661  # noqa: E402

# The script's module-level boto3/psycopg bindings remain as mocks
# (captured at import time inside the context manager), so tests in
# this file continue to work correctly. The ``mock_sys_modules``
# helper has already restored sys.modules so other test files that
# use @patch("boto3.client") (e.g. test_api_error_check.py) see the
# real modules.

parse_flat_hash_key = cleanup_mislabeled_s3_2661.parse_flat_hash_key
is_mislabel = cleanup_mislabeled_s3_2661.is_mislabel
head_object_metadata_hash = cleanup_mislabeled_s3_2661.head_object_metadata_hash
enumerate_mislabels = cleanup_mislabeled_s3_2661.enumerate_mislabels
find_referenced_keys = cleanup_mislabeled_s3_2661.find_referenced_keys
delete_in_batches = cleanup_mislabeled_s3_2661.delete_in_batches
report_enumeration = cleanup_mislabeled_s3_2661.report_enumeration
run_cleanup = cleanup_mislabeled_s3_2661.run_cleanup
build_parser = cleanup_mislabeled_s3_2661.build_parser
main = cleanup_mislabeled_s3_2661.main
# After #4447 the ``except ClientError`` branch lives in
# ``framework.s3_keys.head_object_metadata_hash``, which uses the
# ``_FakeClientError`` class that we installed on the mocked
# ``botocore.exceptions``. Tests raise this same class so the ``isinstance``
# check inside the helper succeeds.
ClientError = _FakeClientError


# ---------------------------------------------------------------------------
# parse_flat_hash_key
# ---------------------------------------------------------------------------


HEX64_A = "a" * 64
HEX64_B = "b" * 64
HEX64_C = "c" * 64


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
        result = parse_flat_hash_key(
            f"ca/santa_clara/superior_court/raw/{HEX64_B}.html"
        )
        assert result is not None
        assert result["county"] == "santa_clara"
        assert result["ext"] == "html"

    def test_valid_docx(self) -> None:
        result = parse_flat_hash_key(
            f"ca/los_angeles/superior_court/raw/{HEX64_A}.docx"
        )
        assert result is not None
        assert result["ext"] == "docx"

    def test_valid_txt(self) -> None:
        result = parse_flat_hash_key(f"ca/orange/superior_court/raw/{HEX64_C}.txt")
        assert result is not None
        assert result["ext"] == "txt"

    def test_date_partitioned_key_returns_none(self) -> None:
        # Legacy date-partitioned keys are NOT in scope (#2627 covers those).
        result = parse_flat_hash_key("ca/orange/superior_court/raw/2026/04/01/uuid.pdf")
        assert result is None

    def test_short_hash_returns_none(self) -> None:
        # 63 chars instead of 64.
        result = parse_flat_hash_key(f"ca/orange/superior_court/raw/{'a' * 63}.pdf")
        assert result is None

    def test_uppercase_hash_returns_none(self) -> None:
        # KEY_PATTERN requires lowercase hex.
        result = parse_flat_hash_key(f"ca/orange/superior_court/raw/{'A' * 64}.pdf")
        assert result is None

    def test_non_raw_path_returns_none(self) -> None:
        # transcripts/ or other path segments are not in scope.
        result = parse_flat_hash_key(
            f"ca/orange/superior_court/transcripts/{HEX64_A}.txt"
        )
        assert result is None

    def test_court_directory_returns_none(self) -> None:
        # court_directory snapshots and other non-document paths must not match.
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
        # Missing metadata is a separate problem class — handled by
        # audit_s3_raw_mislabels.py, not by this script.
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
        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404"}}, "HeadObject"
        )
        assert head_object_metadata_hash(s3, "bucket", "key") is None

    def test_returns_none_when_object_404_via_no_such_key(self) -> None:
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
# enumerate_mislabels
# ---------------------------------------------------------------------------


def _mock_paginator(pages: list[list[dict[str, str]]]) -> MagicMock:
    """Build a mock S3 paginator that yields *pages*, each a list of object
    dicts with a "Key" field."""
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": page} for page in pages]
    return paginator


class TestEnumerateMislabels:
    def test_no_objects_returns_empty(self) -> None:
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator([[]])
        result = enumerate_mislabels(s3, "bucket", "ca/")
        assert result == {}

    def test_correctly_labeled_skipped(self) -> None:
        # filename == metadata: not a mislabel.
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator(
            [[{"Key": f"ca/orange/superior_court/raw/{HEX64_A}.pdf"}]]
        )
        s3.head_object.return_value = {"Metadata": {"content-hash": HEX64_A}}
        result = enumerate_mislabels(s3, "bucket", "ca/")
        assert result == {}

    def test_mislabel_recorded(self) -> None:
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator(
            [[{"Key": f"ca/orange/superior_court/raw/{HEX64_A}.pdf"}]]
        )
        # filename = HEX64_A but metadata says HEX64_B → mislabel.
        s3.head_object.return_value = {"Metadata": {"content-hash": HEX64_B}}
        result = enumerate_mislabels(s3, "bucket", "ca/")
        assert "orange" in result
        assert len(result["orange"]) == 1
        record = result["orange"][0]
        assert record["key"] == f"ca/orange/superior_court/raw/{HEX64_A}.pdf"
        assert record["filename_hash"] == HEX64_A
        assert record["metadata_hash"] == HEX64_B

    def test_groups_by_county(self) -> None:
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator(
            [
                [
                    {"Key": f"ca/orange/superior_court/raw/{HEX64_A}.pdf"},
                    {"Key": f"ca/santa_clara/superior_court/raw/{HEX64_A}.pdf"},
                    {"Key": f"ca/orange/superior_court/raw/{HEX64_B}.pdf"},
                ]
            ]
        )
        s3.head_object.return_value = {"Metadata": {"content-hash": HEX64_C}}
        result = enumerate_mislabels(s3, "bucket", "ca/")
        assert sorted(result.keys()) == ["orange", "santa_clara"]
        assert len(result["orange"]) == 2
        assert len(result["santa_clara"]) == 1

    def test_skips_non_matching_shape(self) -> None:
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator(
            [
                [
                    {"Key": "ca/orange/superior_court/raw/2026/04/01/x.pdf"},
                    {"Key": "court_directory/snapshot.json"},
                    {"Key": f"ca/orange/superior_court/raw/{HEX64_A}.pdf"},
                ]
            ]
        )
        s3.head_object.return_value = {"Metadata": {"content-hash": HEX64_B}}
        result = enumerate_mislabels(s3, "bucket", "ca/")
        # Only the third (matching-shape) key was HEAD'd and recorded.
        assert s3.head_object.call_count == 1
        assert len(result["orange"]) == 1

    def test_skips_objects_with_missing_metadata(self) -> None:
        # Missing metadata is not in scope — the audit script handles those.
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator(
            [[{"Key": f"ca/orange/superior_court/raw/{HEX64_A}.pdf"}]]
        )
        s3.head_object.return_value = {"Metadata": {}}
        result = enumerate_mislabels(s3, "bucket", "ca/")
        assert result == {}

    def test_skips_objects_that_404(self) -> None:
        # Race between list and HEAD — silently skip.
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator(
            [[{"Key": f"ca/orange/superior_court/raw/{HEX64_A}.pdf"}]]
        )
        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404"}}, "HeadObject"
        )
        result = enumerate_mislabels(s3, "bucket", "ca/")
        assert result == {}


# ---------------------------------------------------------------------------
# find_referenced_keys
# ---------------------------------------------------------------------------


class TestFindReferencedKeys:
    def test_empty_input_returns_empty(self) -> None:
        conn = MagicMock()
        assert find_referenced_keys(conn, []) == []
        # No cursor was used.
        conn.cursor.assert_not_called()

    def test_returns_referenced_subset(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        # psycopg uses a context manager for cursor().
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchall.return_value = [
            ("ca/orange/superior_court/raw/aaa.pdf",),
            ("ca/orange/superior_court/raw/bbb.pdf",),
        ]
        keys = [
            "ca/orange/superior_court/raw/aaa.pdf",
            "ca/orange/superior_court/raw/bbb.pdf",
            "ca/orange/superior_court/raw/ccc.pdf",
        ]
        result = find_referenced_keys(conn, keys)
        assert sorted(result) == [
            "ca/orange/superior_court/raw/aaa.pdf",
            "ca/orange/superior_court/raw/bbb.pdf",
        ]
        # Verify the SQL was an ANY() lookup (single round-trip — no full scan).
        cur.execute.assert_called_once()
        sql, params = cur.execute.call_args.args
        assert "= ANY(%s)" in sql
        assert params == (keys,)

    def test_all_unreferenced_returns_empty(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchall.return_value = []
        result = find_referenced_keys(conn, ["k1", "k2"])
        assert result == []


# ---------------------------------------------------------------------------
# delete_in_batches
# ---------------------------------------------------------------------------


class TestDeleteInBatches:
    def test_dry_run_returns_zero(self) -> None:
        s3 = MagicMock()
        result = delete_in_batches(s3, "bucket", ["a", "b"], dry_run=True)
        assert result == 0
        s3.delete_objects.assert_not_called()

    def test_empty_returns_zero(self) -> None:
        s3 = MagicMock()
        result = delete_in_batches(s3, "bucket", [], dry_run=False)
        assert result == 0
        s3.delete_objects.assert_not_called()

    def test_single_batch_apply(self) -> None:
        s3 = MagicMock()
        keys = [f"k{i}" for i in range(3)]
        result = delete_in_batches(s3, "bucket", keys, dry_run=False)
        assert result == 3
        s3.delete_objects.assert_called_once()
        kwargs = s3.delete_objects.call_args.kwargs
        assert kwargs["Bucket"] == "bucket"
        assert kwargs["Delete"]["Objects"] == [{"Key": k} for k in keys]
        assert kwargs["Delete"]["Quiet"] is True

    def test_batch_boundary_2500_keys(self) -> None:
        # 2500 keys → 1000 + 1000 + 500 → three calls.
        s3 = MagicMock()
        keys = [f"k{i}" for i in range(2500)]
        result = delete_in_batches(s3, "bucket", keys, dry_run=False)
        assert result == 2500
        assert s3.delete_objects.call_count == 3
        # First two batches are 1000 each, last is 500.
        first_batch = s3.delete_objects.call_args_list[0].kwargs["Delete"]["Objects"]
        last_batch = s3.delete_objects.call_args_list[2].kwargs["Delete"]["Objects"]
        assert len(first_batch) == 1000
        assert len(last_batch) == 500


# ---------------------------------------------------------------------------
# run_cleanup orchestration
# ---------------------------------------------------------------------------


def _stub_s3_with_mislabels(
    keys_to_mislabel: list[str], all_keys: list[str] | None = None
) -> MagicMock:
    """Build an S3 client mock that lists *all_keys* and reports each key in
    *keys_to_mislabel* as a mislabel (metadata != filename)."""
    if all_keys is None:
        all_keys = keys_to_mislabel
    s3 = MagicMock()
    s3.get_paginator.return_value = _mock_paginator([[{"Key": k} for k in all_keys]])

    def fake_head(*, Bucket: str, Key: str) -> dict:
        if Key in keys_to_mislabel:
            # Return a metadata hash that differs from the filename hash.
            return {"Metadata": {"content-hash": HEX64_C}}
        # Correctly-labelled key — metadata equals filename.
        parsed = parse_flat_hash_key(Key)
        return {"Metadata": {"content-hash": parsed["hash"]}}

    s3.head_object.side_effect = fake_head
    return s3


def _stub_db_no_references() -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = []
    return conn


def _stub_db_with_references(referenced: list[str]) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = [(k,) for k in referenced]
    return conn


class TestRunCleanup:
    def test_no_mislabels_returns_clean(self) -> None:
        s3 = _stub_s3_with_mislabels([])
        conn = _stub_db_no_references()
        result = run_cleanup(
            s3, conn, bucket="b", state="ca", county=None, dry_run=True
        )
        assert result == {
            "mislabels_found": 0,
            "referenced_count": 0,
            "deleted": 0,
            "aborted": 0,
        }
        s3.delete_objects.assert_not_called()

    def test_dry_run_does_not_delete(self) -> None:
        keys = [f"ca/orange/superior_court/raw/{HEX64_A}.pdf"]
        s3 = _stub_s3_with_mislabels(keys)
        conn = _stub_db_no_references()
        result = run_cleanup(
            s3, conn, bucket="b", state="ca", county=None, dry_run=True
        )
        assert result["mislabels_found"] == 1
        assert result["deleted"] == 0
        assert result["aborted"] == 0
        s3.delete_objects.assert_not_called()

    def test_apply_aborts_when_db_references_present(self) -> None:
        keys = [f"ca/orange/superior_court/raw/{HEX64_A}.pdf"]
        s3 = _stub_s3_with_mislabels(keys)
        conn = _stub_db_with_references(keys)  # row still pointing at the mislabel
        result = run_cleanup(
            s3, conn, bucket="b", state="ca", county=None, dry_run=False
        )
        assert result["mislabels_found"] == 1
        assert result["referenced_count"] == 1
        assert result["deleted"] == 0
        assert result["aborted"] == 1
        s3.delete_objects.assert_not_called()

    def test_apply_succeeds_when_no_references(self) -> None:
        keys = [
            f"ca/orange/superior_court/raw/{HEX64_A}.pdf",
            f"ca/santa_clara/superior_court/raw/{HEX64_B}.pdf",
        ]
        s3 = _stub_s3_with_mislabels(keys)
        conn = _stub_db_no_references()
        result = run_cleanup(
            s3, conn, bucket="b", state="ca", county=None, dry_run=False
        )
        assert result["mislabels_found"] == 2
        assert result["referenced_count"] == 0
        assert result["deleted"] == 2
        assert result["aborted"] == 0
        s3.delete_objects.assert_called_once()

    def test_county_scoping_uses_specific_prefix(self) -> None:
        s3 = _stub_s3_with_mislabels([])
        conn = _stub_db_no_references()
        run_cleanup(s3, conn, bucket="b", state="ca", county="orange", dry_run=True)
        # Verify the paginator was called with the county-scoped prefix.
        s3.get_paginator.return_value.paginate.assert_called_with(
            Bucket="b", Prefix="ca/orange/"
        )


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
    def test_missing_database_url_returns_2(self) -> None:
        # Save and clear DATABASE_URL.
        saved = os.environ.pop("DATABASE_URL", None)
        try:
            assert main(["--dry-run"]) == 2
        finally:
            if saved is not None:
                os.environ["DATABASE_URL"] = saved

    def test_dry_run_returns_0(self) -> None:
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://stub"}):
            with patch.object(cleanup_mislabeled_s3_2661, "boto3") as boto3_mock:
                with patch.object(
                    cleanup_mislabeled_s3_2661, "psycopg"
                ) as psycopg_mock:
                    boto3_mock.client.return_value = _stub_s3_with_mislabels([])
                    psycopg_mock.connect.return_value.__enter__.return_value = (
                        _stub_db_no_references()
                    )
                    assert main(["--dry-run"]) == 0

    def test_apply_with_db_references_returns_1(self) -> None:
        keys = [f"ca/orange/superior_court/raw/{HEX64_A}.pdf"]
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://stub"}):
            with patch.object(cleanup_mislabeled_s3_2661, "boto3") as boto3_mock:
                with patch.object(
                    cleanup_mislabeled_s3_2661, "psycopg"
                ) as psycopg_mock:
                    boto3_mock.client.return_value = _stub_s3_with_mislabels(keys)
                    psycopg_mock.connect.return_value.__enter__.return_value = (
                        _stub_db_with_references(keys)
                    )
                    assert main(["--apply"]) == 1

    def test_apply_no_references_returns_0(self) -> None:
        keys = [f"ca/orange/superior_court/raw/{HEX64_A}.pdf"]
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://stub"}):
            with patch.object(cleanup_mislabeled_s3_2661, "boto3") as boto3_mock:
                with patch.object(
                    cleanup_mislabeled_s3_2661, "psycopg"
                ) as psycopg_mock:
                    boto3_mock.client.return_value = _stub_s3_with_mislabels(keys)
                    psycopg_mock.connect.return_value.__enter__.return_value = (
                        _stub_db_no_references()
                    )
                    assert main(["--apply"]) == 0


# ---------------------------------------------------------------------------
# report_enumeration (smoke test)
# ---------------------------------------------------------------------------


class TestReportEnumeration:
    def test_returns_total(self, capsys: pytest.CaptureFixture[str]) -> None:
        by_county = {
            "orange": [
                {
                    "key": f"ca/orange/superior_court/raw/{HEX64_A}.pdf",
                    "filename_hash": HEX64_A,
                    "metadata_hash": HEX64_B,
                }
            ],
            "santa_clara": [
                {
                    "key": f"ca/santa_clara/superior_court/raw/{HEX64_C}.pdf",
                    "filename_hash": HEX64_C,
                    "metadata_hash": HEX64_A,
                },
                {
                    "key": f"ca/santa_clara/superior_court/raw/{HEX64_B}.pdf",
                    "filename_hash": HEX64_B,
                    "metadata_hash": HEX64_A,
                },
            ],
        }
        total = report_enumeration(by_county)
        assert total == 3
        captured = capsys.readouterr()
        # Per-county counts appear in stdout.
        assert "orange" in captured.out
        assert "santa_clara" in captured.out
        assert "TOTAL" in captured.out
