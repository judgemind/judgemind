"""Tests for repoint_mislabeled_documents_4439 script.

Tests cover:
- parse_flat_hash_key for valid, malformed, edge-case keys
  (re-exported from ``framework.s3_keys`` after #4447)
- is_mislabel for matching/mismatching/missing-metadata cases
  (re-exported from ``framework.s3_keys`` after #4447)
- build_twin_key for valid + non-parseable inputs
  (re-exported from ``framework.s3_keys`` after #4447)
- head_object_metadata_hash with mocked S3 client (200, 404, missing field)
  (re-exported from ``framework.s3_keys`` after #4447)
- verify_twin: valid, missing, invalid twin
- enumerate_repoint_pairs end-to-end with mocked paginator + HEAD
  (records valid/missing/invalid twin states)
- count_documents_for_county with mocked psycopg connection
- repoint_pairs_for_county: dry-run, valid-only filter, row-count invariant
  abort, success path
- run_repoint orchestration: empty, dry-run, apply with valid + missing twins
- main CLI: --dry-run default, --apply switch, missing DATABASE_URL,
  abort exit code propagation

The script imports psycopg + boto3 + structlog + framework.* at module level,
which may not be installed in the CI ``scripts-tests`` environment. We use
``tests._mock_helpers.mock_sys_modules`` (the canonical pattern as of #4430)
to inject MagicMocks into sys.modules around the import — the helper restores
sys.modules on exit so other tests in the same pytest run don't see the leaked
mocks (#4426).

After #4447, the small pure helpers (``parse_flat_hash_key``, ``is_mislabel``,
``head_object_metadata_hash``, ``build_twin_key``, ``KEY_PATTERN``) live in
``framework.s3_keys``. We load that real module via ``importlib`` from its
source path inside the mock_sys_modules block so the script imports the
genuine implementation rather than a MagicMock attribute.
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
        "psycopg": MagicMock(),
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
    import repoint_mislabeled_documents_4439  # noqa: E402

parse_flat_hash_key = repoint_mislabeled_documents_4439.parse_flat_hash_key
is_mislabel = repoint_mislabeled_documents_4439.is_mislabel
build_twin_key = repoint_mislabeled_documents_4439.build_twin_key
head_object_metadata_hash = repoint_mislabeled_documents_4439.head_object_metadata_hash
verify_twin = repoint_mislabeled_documents_4439.verify_twin
enumerate_repoint_pairs = repoint_mislabeled_documents_4439.enumerate_repoint_pairs
count_documents_for_county = (
    repoint_mislabeled_documents_4439.count_documents_for_county
)
repoint_pairs_for_county = repoint_mislabeled_documents_4439.repoint_pairs_for_county
report_enumeration = repoint_mislabeled_documents_4439.report_enumeration
run_repoint = repoint_mislabeled_documents_4439.run_repoint
build_parser = repoint_mislabeled_documents_4439.build_parser
main = repoint_mislabeled_documents_4439.main
# After #4447 the ``except ClientError`` branch lives in
# ``framework.s3_keys.head_object_metadata_hash``, which uses the
# ``_FakeClientError`` class that we installed on the mocked
# ``botocore.exceptions``. Tests raise this same class so the ``isinstance``
# check inside the helper succeeds.
ClientError = _FakeClientError


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
# enumerate_repoint_pairs
# ---------------------------------------------------------------------------


def _mock_paginator(pages: list[list[dict[str, str]]]) -> MagicMock:
    """Build a mock S3 paginator that yields *pages*, each a list of object
    dicts with a "Key" field."""
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": page} for page in pages]
    return paginator


class TestEnumerateRepointPairs:
    def test_no_objects_returns_empty(self) -> None:
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator([[]])
        result = enumerate_repoint_pairs(s3, "bucket", "ca/")
        assert result == {}

    def test_correctly_labeled_skipped(self) -> None:
        # filename == metadata: not a mislabel, no pair recorded.
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator(
            [[{"Key": f"ca/orange/superior_court/raw/{HEX64_A}.pdf"}]]
        )
        s3.head_object.return_value = {"Metadata": {"content-hash": HEX64_A}}
        result = enumerate_repoint_pairs(s3, "bucket", "ca/")
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
        result = enumerate_repoint_pairs(s3, "bucket", "ca/")
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
        result = enumerate_repoint_pairs(s3, "bucket", "ca/")
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
        result = enumerate_repoint_pairs(s3, "bucket", "ca/")
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
        result = enumerate_repoint_pairs(s3, "bucket", "ca/")
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
        result = enumerate_repoint_pairs(s3, "bucket", "ca/")
        assert result == {}
        s3.head_object.assert_not_called()

    def test_skips_objects_with_missing_metadata(self) -> None:
        s3 = MagicMock()
        s3.get_paginator.return_value = _mock_paginator(
            [[{"Key": f"ca/orange/superior_court/raw/{HEX64_A}.pdf"}]]
        )
        s3.head_object.return_value = {"Metadata": {}}
        result = enumerate_repoint_pairs(s3, "bucket", "ca/")
        assert result == {}


# ---------------------------------------------------------------------------
# count_documents_for_county
# ---------------------------------------------------------------------------


class TestCountDocumentsForCounty:
    def test_returns_count(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchone.return_value = (1174,)
        # Spaced form passes through unchanged (no underscores to replace).
        result = count_documents_for_county(conn, "Santa Clara")
        assert result == 1174
        cur.execute.assert_called_once()
        sql, params = cur.execute.call_args.args
        assert "JOIN derived.courts" in sql
        assert "LOWER(c.county) = LOWER(%s)" in sql
        assert params == ("Santa Clara",)

    def test_returns_zero_when_none(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchone.return_value = None
        result = count_documents_for_county(conn, "orange")
        assert result == 0

    # ------------------------------------------------------------------
    # Regression: #4566 — multi-word counties degenerated to COUNT(*) = 0
    # ------------------------------------------------------------------
    #
    # Before the fix, the CLI passed ``santa_clara`` (underscore) but
    # ``derived.courts.county`` stores ``'santa clara'`` (space), so the
    # ``LOWER(c.county) = LOWER('santa_clara')`` predicate never matched
    # and the row-count invariant silently degenerated to ``0 == 0``.
    # The fix normalizes underscores to spaces before binding the param.

    def test_underscored_multi_word_county_normalized_to_spaced(self) -> None:
        """``--county santa_clara`` must bind ``'santa clara'`` to the SQL,
        which is what ``derived.courts.county`` actually stores."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchone.return_value = (1174,)
        result = count_documents_for_county(conn, "santa_clara")
        assert result == 1174
        sql, params = cur.execute.call_args.args
        # Underscores must be replaced with spaces in the bound param.
        assert params == ("santa clara",)

    @pytest.mark.parametrize(
        ("input_county", "expected_param"),
        [
            # All five multi-word counties from #2661 / #4566.
            ("santa_clara", "santa clara"),
            ("los_angeles", "los angeles"),
            ("contra_costa", "contra costa"),
            ("san_bernardino", "san bernardino"),
            ("san_francisco", "san francisco"),
            # Single-word counties pass through unchanged.
            ("orange", "orange"),
            ("riverside", "riverside"),
            ("fresno", "fresno"),
            ("ventura", "ventura"),
            # Triple-underscore (defense-in-depth).
            ("a_b_c", "a b c"),
        ],
    )
    def test_county_param_normalization(
        self, input_county: str, expected_param: str
    ) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchone.return_value = (42,)
        count_documents_for_county(conn, input_county)
        _, params = cur.execute.call_args.args
        assert params == (expected_param,)


# ---------------------------------------------------------------------------
# repoint_pairs_for_county
# ---------------------------------------------------------------------------


def _stub_db_with_count(value: int) -> MagicMock:
    """Build a psycopg-shaped mock conn whose count_documents_for_county
    returns *value*. Cursor.rowcount returns 1 for each UPDATE."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = (value,)
    cur.rowcount = 1
    # transaction() returns a context manager that does nothing.
    conn.transaction.return_value.__enter__.return_value = None
    conn.transaction.return_value.__exit__.return_value = None
    return conn


class TestRepointPairsForCounty:
    def test_no_valid_pairs_returns_zero(self) -> None:
        conn = MagicMock()
        pairs = [
            {
                "mislabel_key": "k1",
                "twin_key": "k1t",
                "filename_hash": HEX64_A,
                "metadata_hash": HEX64_B,
                "twin_status": "missing",
            }
        ]
        result = repoint_pairs_for_county(conn, pairs, county="orange", dry_run=False)
        assert result == {
            "planned": 0,
            "updated": 0,
            "skipped_invalid": 0,
            "skipped_missing": 1,
            "aborted": 0,
        }
        conn.cursor.assert_not_called()

    def test_dry_run_does_not_write(self) -> None:
        conn = MagicMock()
        pairs = [
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
        ]
        result = repoint_pairs_for_county(conn, pairs, county="orange", dry_run=True)
        assert result["planned"] == 1
        assert result["updated"] == 0
        assert result["skipped_missing"] == 1
        assert result["aborted"] == 0
        conn.cursor.assert_not_called()
        conn.transaction.assert_not_called()

    def test_apply_success_rowcount_invariant_holds(self) -> None:
        # Pre-count and post-count both 100; transaction should commit.
        conn = _stub_db_with_count(100)
        pairs = [
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
                "filename_hash": HEX64_B,
                "metadata_hash": HEX64_C,
                "twin_status": "valid",
            },
        ]
        result = repoint_pairs_for_county(conn, pairs, county="orange", dry_run=False)
        assert result["planned"] == 2
        assert result["updated"] == 2  # rowcount=1 per UPDATE x 2 calls
        assert result["aborted"] == 0
        conn.transaction.assert_called_once()

    def test_apply_aborts_on_invariant_violation(self) -> None:
        # Pre-count 100, post-count 99 → invariant violated, abort.
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        # First fetchone (pre-count) = 100, second (post-count) = 99.
        cur.fetchone.side_effect = [(100,), (99,)]
        cur.rowcount = 0
        # transaction() context manager re-raises on exit when body raises.
        tx_cm = MagicMock()
        tx_cm.__enter__.return_value = None
        # __exit__ must return False (or None) so the RuntimeError propagates.
        tx_cm.__exit__.return_value = False
        conn.transaction.return_value = tx_cm

        pairs = [
            {
                "mislabel_key": "k1",
                "twin_key": "k1t",
                "filename_hash": HEX64_A,
                "metadata_hash": HEX64_B,
                "twin_status": "valid",
            }
        ]
        result = repoint_pairs_for_county(conn, pairs, county="orange", dry_run=False)
        assert result["aborted"] == 1
        assert result["updated"] == 0
        assert result["planned"] == 1


# ---------------------------------------------------------------------------
# run_repoint orchestration
# ---------------------------------------------------------------------------


def _stub_s3_with_pairs(
    mislabels_to_pairs: dict[str, str],
    list_keys: list[str] | None = None,
    invalid_twins: set[str] | None = None,
    missing_twins: set[str] | None = None,
) -> MagicMock:
    """Build an S3 client mock that:
    - lists *list_keys* (defaults to mislabels_to_pairs.keys())
    - HEAD on a mislabel key returns metadata == its mapped twin's filename hash
    - HEAD on a twin key returns metadata == its filename (valid) unless in
      *invalid_twins* (returns mismatching) or *missing_twins* (raises 404)
    """
    if list_keys is None:
        list_keys = list(mislabels_to_pairs.keys())
    invalid_twins = invalid_twins or set()
    missing_twins = missing_twins or set()
    s3 = MagicMock()
    s3.get_paginator.return_value = _mock_paginator([[{"Key": k} for k in list_keys]])

    twin_to_mislabel = {v: k for k, v in mislabels_to_pairs.items()}

    def fake_head(*, Bucket: str, Key: str) -> dict:
        if Key in mislabels_to_pairs:
            twin = mislabels_to_pairs[Key]
            twin_parsed = parse_flat_hash_key(twin)
            assert twin_parsed is not None
            return {"Metadata": {"content-hash": twin_parsed["hash"]}}
        if Key in missing_twins:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        if Key in twin_to_mislabel:
            twin_parsed = parse_flat_hash_key(Key)
            assert twin_parsed is not None
            if Key in invalid_twins:
                # Return a metadata hash that disagrees with the filename.
                return {"Metadata": {"content-hash": HEX64_D}}
            return {"Metadata": {"content-hash": twin_parsed["hash"]}}
        # Correctly-labelled standalone key.
        parsed = parse_flat_hash_key(Key)
        if parsed:
            return {"Metadata": {"content-hash": parsed["hash"]}}
        return {"Metadata": {}}

    s3.head_object.side_effect = fake_head
    return s3


class TestRunRepoint:
    def test_no_mislabels_returns_clean(self) -> None:
        s3 = _stub_s3_with_pairs({})
        conn = MagicMock()
        result = run_repoint(
            s3, conn, bucket="b", state="ca", county=None, dry_run=True
        )
        assert result["mislabels_found"] == 0
        assert result["valid_twins"] == 0
        assert result["planned"] == 0
        assert result["aborted_counties"] == 0

    def test_dry_run_with_valid_twin_plans_repoint(self) -> None:
        mislabel = f"ca/orange/superior_court/raw/{HEX64_A}.pdf"
        twin = f"ca/orange/superior_court/raw/{HEX64_B}.pdf"
        s3 = _stub_s3_with_pairs({mislabel: twin})
        conn = MagicMock()
        result = run_repoint(
            s3, conn, bucket="b", state="ca", county=None, dry_run=True
        )
        assert result["mislabels_found"] == 1
        assert result["valid_twins"] == 1
        assert result["planned"] == 1
        assert result["updated"] == 0
        assert result["aborted_counties"] == 0
        conn.cursor.assert_not_called()

    def test_dry_run_with_missing_twin_does_not_plan(self) -> None:
        mislabel = f"ca/orange/superior_court/raw/{HEX64_A}.pdf"
        twin = f"ca/orange/superior_court/raw/{HEX64_B}.pdf"
        s3 = _stub_s3_with_pairs({mislabel: twin}, missing_twins={twin})
        conn = MagicMock()
        result = run_repoint(
            s3, conn, bucket="b", state="ca", county=None, dry_run=True
        )
        assert result["mislabels_found"] == 1
        assert result["valid_twins"] == 0
        assert result["missing_twins"] == 1
        assert result["planned"] == 0

    def test_county_scoping_uses_specific_prefix(self) -> None:
        s3 = _stub_s3_with_pairs({})
        conn = MagicMock()
        run_repoint(s3, conn, bucket="b", state="ca", county="orange", dry_run=True)
        s3.get_paginator.return_value.paginate.assert_called_with(
            Bucket="b", Prefix="ca/orange/"
        )

    def test_apply_success_updates_rows(self) -> None:
        mislabel = f"ca/orange/superior_court/raw/{HEX64_A}.pdf"
        twin = f"ca/orange/superior_court/raw/{HEX64_B}.pdf"
        s3 = _stub_s3_with_pairs({mislabel: twin})
        conn = _stub_db_with_count(50)
        result = run_repoint(
            s3, conn, bucket="b", state="ca", county=None, dry_run=False
        )
        assert result["planned"] == 1
        assert result["updated"] == 1
        assert result["aborted_counties"] == 0


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
        saved = os.environ.pop("DATABASE_URL", None)
        try:
            assert main(["--dry-run"]) == 2
        finally:
            if saved is not None:
                os.environ["DATABASE_URL"] = saved

    def test_dry_run_returns_0(self) -> None:
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://stub"}):
            with patch.object(repoint_mislabeled_documents_4439, "boto3") as boto3_mock:
                with patch.object(
                    repoint_mislabeled_documents_4439, "psycopg"
                ) as psycopg_mock:
                    boto3_mock.client.return_value = _stub_s3_with_pairs({})
                    psycopg_mock.connect.return_value.__enter__.return_value = (
                        MagicMock()
                    )
                    assert main(["--dry-run"]) == 0

    def test_apply_with_invariant_violation_returns_1(self) -> None:
        # Build an S3 stub with one valid mislabel/twin pair.
        mislabel = f"ca/orange/superior_court/raw/{HEX64_A}.pdf"
        twin = f"ca/orange/superior_court/raw/{HEX64_B}.pdf"
        s3 = _stub_s3_with_pairs({mislabel: twin})

        # Build a psycopg-shaped mock whose pre/post counts mismatch.
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchone.side_effect = [(100,), (99,)]
        cur.rowcount = 0
        tx_cm = MagicMock()
        tx_cm.__enter__.return_value = None
        tx_cm.__exit__.return_value = False
        conn.transaction.return_value = tx_cm

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://stub"}):
            with patch.object(repoint_mislabeled_documents_4439, "boto3") as boto3_mock:
                with patch.object(
                    repoint_mislabeled_documents_4439, "psycopg"
                ) as psycopg_mock:
                    boto3_mock.client.return_value = s3
                    psycopg_mock.connect.return_value.__enter__.return_value = conn
                    assert main(["--apply"]) == 1


# ---------------------------------------------------------------------------
# report_enumeration (smoke test)
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
