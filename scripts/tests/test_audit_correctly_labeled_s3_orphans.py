"""Tests for audit_correctly_labeled_s3_orphans script.

Covers:
- FLAT_HASH_RE rejects date-partitioned and LLM-cache keys
- list_raw_flat_hash_keys handles paginator with >1000 objects
- classify returns correctly_labeled_orphan when key absent from db_keys
- classify returns present_in_db when key is in db_keys
- classify returns mislabeled when byte hash does not match filename hash
- sample head_object verification in classify (verify_bytes=True)
- --json output schema is stable
- fetch_db_s3_keys issues correct LIKE query

The script imports boto3, psycopg, structlog, and framework.logging at module
level; we mock them before importing to support the CI scripts-tests (python)
environment which only installs ``pytest pytest-xdist boto3 -e
packages/judgemind-config``. Mirrors the post-#4368 pattern used in
``test_drain_splitter_carry_forward_clusters.py`` /
``test_backfill_split_supersede.py``. Migrated as part of #4373.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Pre-import mocking — inject boto3, psycopg, structlog, and framework.logging
# mocks before the script loads. The script's top-level
# ``from framework.logging import configure_structlog`` would otherwise raise
# ModuleNotFoundError in the lightweight CI scripts-tests (python) shard
# (which only installs pytest, pytest-xdist, boto3, judgemind-config). See #4373.
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_mock_boto3 = MagicMock()
_mock_psycopg = MagicMock()
_mock_structlog = MagicMock()
_mock_structlog.get_logger.return_value = MagicMock()
_mock_framework = MagicMock()
_mock_framework_logging = MagicMock()

_modules_to_mock = {
    "boto3": _mock_boto3,
    "psycopg": _mock_psycopg,
    "structlog": _mock_structlog,
    "framework": _mock_framework,
    "framework.logging": _mock_framework_logging,
}

_saved_modules: dict[str, object] = {}
for _mod_name, _mock_mod in _modules_to_mock.items():
    if _mod_name in sys.modules:
        _saved_modules[_mod_name] = sys.modules[_mod_name]
    sys.modules[_mod_name] = _mock_mod

import audit_correctly_labeled_s3_orphans as _script  # noqa: E402

# Restore sys.modules to avoid polluting other test files.
for _mod_name in list(_modules_to_mock.keys()):
    if _mod_name in _saved_modules:
        sys.modules[_mod_name] = _saved_modules[_mod_name]
    elif _mod_name in sys.modules:
        del sys.modules[_mod_name]

FLAT_HASH_RE = _script.FLAT_HASH_RE
list_raw_flat_hash_keys = _script.list_raw_flat_hash_keys
fetch_db_s3_keys = _script.fetch_db_s3_keys
fetch_db_content_hashes = _script.fetch_db_content_hashes
classify = _script.classify
run_audit = _script.run_audit

PRESENT_IN_DB = _script.PRESENT_IN_DB
PRESENT_IN_DB_VIA_CONTENT_HASH = _script.PRESENT_IN_DB_VIA_CONTENT_HASH
MISLABELED = _script.MISLABELED
CORRECTLY_LABELED_ORPHAN = _script.CORRECTLY_LABELED_ORPHAN

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_HASH = "a" * 64  # 64 hex chars
_NOW = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_obj(key: str, size: int = 1024) -> dict:
    """Build a minimal list_objects_v2 Contents entry."""
    return {"Key": key, "LastModified": _NOW, "Size": size}


# ---------------------------------------------------------------------------
# 1. FLAT_HASH_RE — regex acceptance tests
# ---------------------------------------------------------------------------


class TestFlatHashRegex:
    """FLAT_HASH_RE must accept correctly-labeled flat-hash keys and reject everything else."""

    def test_accepts_sc_pdf(self) -> None:
        key = f"ca/santa_clara/superior_court/raw/{_VALID_HASH}.pdf"
        assert FLAT_HASH_RE.match(key) is not None

    def test_accepts_oc_html(self) -> None:
        key = f"ca/orange/superior_court/raw/{_VALID_HASH}.html"
        assert FLAT_HASH_RE.match(key) is not None

    def test_accepts_docx(self) -> None:
        key = f"ca/santa_clara/superior_court/raw/{_VALID_HASH}.docx"
        assert FLAT_HASH_RE.match(key) is not None

    def test_accepts_txt(self) -> None:
        key = f"ca/orange/superior_court/raw/{_VALID_HASH}.txt"
        assert FLAT_HASH_RE.match(key) is not None

    def test_rejects_date_partitioned_key(self) -> None:
        """Date-partitioned keys must NOT match — they are a different keyspace."""
        key = "ca/santa_clara/superior_court/raw/2026/03/28/some-uuid.pdf"
        assert FLAT_HASH_RE.match(key) is None

    def test_rejects_llm_cache_key(self) -> None:
        """LLM cache keys (different prefix) must NOT match."""
        key = "ca/santa_clara/superior_court/llm-cache/abcdef.json"
        assert FLAT_HASH_RE.match(key) is None

    def test_rejects_judge_assignments_key(self) -> None:
        """judge-assignments prefix must NOT match."""
        key = "ca/santa_clara/judge-assignments/2026-03.json"
        assert FLAT_HASH_RE.match(key) is None

    def test_rejects_short_hash(self) -> None:
        """Hash shorter than 64 hex chars must not match."""
        short_hash = "a" * 40
        key = f"ca/santa_clara/superior_court/raw/{short_hash}.pdf"
        assert FLAT_HASH_RE.match(key) is None

    def test_rejects_long_hash(self) -> None:
        """Hash longer than 64 hex chars must not match."""
        long_hash = "a" * 65
        key = f"ca/santa_clara/superior_court/raw/{long_hash}.pdf"
        assert FLAT_HASH_RE.match(key) is None

    def test_rejects_non_hex_in_hash(self) -> None:
        """Hash containing non-hex characters must not match."""
        bad_hash = "g" * 64  # 'g' is not a hex digit
        key = f"ca/santa_clara/superior_court/raw/{bad_hash}.pdf"
        assert FLAT_HASH_RE.match(key) is None

    def test_rejects_unsupported_extension(self) -> None:
        """Only pdf/html/docx/txt are allowed extensions."""
        key = f"ca/santa_clara/superior_court/raw/{_VALID_HASH}.bin"
        assert FLAT_HASH_RE.match(key) is None

    def test_rejects_directory_snapshots(self) -> None:
        """directory-snapshots keyspace must NOT match."""
        key = "ca/santa_clara/court-directory-snapshots/2026-04-01.json"
        assert FLAT_HASH_RE.match(key) is None


# ---------------------------------------------------------------------------
# 2. list_raw_flat_hash_keys — paginator handles >1000 keys
# ---------------------------------------------------------------------------


class TestListRawFlatHashKeys:
    def test_paginator_yields_only_flat_hash_keys(self) -> None:
        """list_raw_flat_hash_keys filters out non-flat-hash keys."""
        valid_key = f"ca/santa_clara/superior_court/raw/{_VALID_HASH}.pdf"
        date_key = "ca/santa_clara/superior_court/raw/2026/03/28/uuid.pdf"
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_s3.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Contents": [_make_obj(valid_key), _make_obj(date_key)]},
        ]
        results = list(list_raw_flat_hash_keys(mock_s3, "ca/santa_clara/"))
        keys = [r[0] for r in results]
        assert valid_key in keys
        assert date_key not in keys
        assert len(results) == 1

    def test_paginator_handles_more_than_1000_keys(self) -> None:
        """list_raw_flat_hash_keys consumes all pages even with >1000 objects."""
        # Build 1500 valid keys across two pages (1000 + 500).
        keys_page1 = [
            f"ca/santa_clara/superior_court/raw/{'a' * 63}{i % 16:x}.pdf"
            for i in range(1000)
        ]
        keys_page2 = [
            f"ca/santa_clara/superior_court/raw/{'b' * 63}{i % 16:x}.pdf"
            for i in range(500)
        ]
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_s3.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Contents": [_make_obj(k) for k in keys_page1]},
            {"Contents": [_make_obj(k) for k in keys_page2]},
        ]
        results = list(list_raw_flat_hash_keys(mock_s3, "ca/santa_clara/"))
        # All 1500 distinct hash keys must be returned.
        returned_keys = {r[0] for r in results}
        # Due to duplicate hashes (keys collide by modulo), count unique.
        expected_unique = set(keys_page1) | set(keys_page2)
        assert returned_keys == expected_unique

    def test_paginator_handles_empty_page(self) -> None:
        """list_raw_flat_hash_keys handles pages with no Contents key."""
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_s3.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{}]
        results = list(list_raw_flat_hash_keys(mock_s3, "ca/santa_clara/"))
        assert results == []

    def test_limit_stops_early(self) -> None:
        """list_raw_flat_hash_keys stops yielding after limit keys."""
        keys = [
            f"ca/santa_clara/superior_court/raw/{'a' * 63}{i:x}.pdf" for i in range(16)
        ]
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_s3.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Contents": [_make_obj(k) for k in keys]},
        ]
        results = list(list_raw_flat_hash_keys(mock_s3, "ca/santa_clara/", limit=5))
        assert len(results) == 5


# ---------------------------------------------------------------------------
# 3. classify — returns correct classification
# ---------------------------------------------------------------------------


class TestClassify:
    def test_present_in_db(self) -> None:
        """classify returns PRESENT_IN_DB when key is in db_keys."""
        key = f"ca/santa_clara/superior_court/raw/{_VALID_HASH}.pdf"
        db_keys = {key}
        mock_s3 = MagicMock()
        result = classify(key, _NOW, mock_s3, db_keys, set(), verify_bytes=False)
        assert result == PRESENT_IN_DB

    def test_correctly_labeled_orphan_bulk_pass(self) -> None:
        """classify returns CORRECTLY_LABELED_ORPHAN when key absent from db_keys and db_content_hashes (bulk)."""
        key = f"ca/santa_clara/superior_court/raw/{_VALID_HASH}.pdf"
        db_keys: set[str] = set()
        mock_s3 = MagicMock()
        result = classify(key, _NOW, mock_s3, db_keys, set(), verify_bytes=False)
        assert result == CORRECTLY_LABELED_ORPHAN
        # Should not call S3 in bulk pass.
        mock_s3.get_object.assert_not_called()

    def test_present_in_db_via_content_hash_bulk_pass(self) -> None:
        """classify returns PRESENT_IN_DB_VIA_CONTENT_HASH when filename hash is in db_content_hashes."""
        key = f"ca/santa_clara/superior_court/raw/{_VALID_HASH}.pdf"
        db_keys: set[str] = set()
        db_content_hashes = {_VALID_HASH}
        mock_s3 = MagicMock()
        result = classify(
            key, _NOW, mock_s3, db_keys, db_content_hashes, verify_bytes=False
        )
        assert result == PRESENT_IN_DB_VIA_CONTENT_HASH
        # Should not call S3 — dedup is determined by set membership.
        mock_s3.get_object.assert_not_called()

    def test_present_in_db_via_content_hash_with_verify_bytes(self) -> None:
        """classify returns PRESENT_IN_DB_VIA_CONTENT_HASH before byte-verify when hash matches."""
        key = f"ca/santa_clara/superior_court/raw/{_VALID_HASH}.pdf"
        db_keys: set[str] = set()
        db_content_hashes = {_VALID_HASH}
        mock_s3 = MagicMock()
        result = classify(
            key, _NOW, mock_s3, db_keys, db_content_hashes, verify_bytes=True
        )
        assert result == PRESENT_IN_DB_VIA_CONTENT_HASH
        # Content-hash check is a short-circuit; S3 should not be called.
        mock_s3.get_object.assert_not_called()

    def test_correctly_labeled_orphan_with_byte_verify(self) -> None:
        """classify returns CORRECTLY_LABELED_ORPHAN when byte hash matches filename."""
        content = b"test content for orphan"
        actual_hash = hashlib.sha256(content).hexdigest()
        key = f"ca/santa_clara/superior_court/raw/{actual_hash}.pdf"
        db_keys: set[str] = set()
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": BytesIO(content)}
        result = classify(key, _NOW, mock_s3, db_keys, set(), verify_bytes=True)
        assert result == CORRECTLY_LABELED_ORPHAN
        mock_s3.get_object.assert_called_once()

    def test_mislabeled_when_bytes_hash_differs(self) -> None:
        """classify returns MISLABELED when byte hash does not match filename hash."""
        content = b"some content"
        actual_hash = hashlib.sha256(content).hexdigest()
        wrong_hash = "f" * 64  # not the actual hash
        key = f"ca/santa_clara/superior_court/raw/{wrong_hash}.pdf"
        db_keys: set[str] = set()
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": BytesIO(content)}
        result = classify(key, _NOW, mock_s3, db_keys, set(), verify_bytes=True)
        assert result == MISLABELED
        # Confirm actual hash != wrong hash so we're testing the right thing.
        assert actual_hash != wrong_hash

    def test_verify_bytes_s3_error_treated_as_correctly_labeled(self) -> None:
        """classify falls back to CORRECTLY_LABELED_ORPHAN if S3 GET fails."""
        key = f"ca/santa_clara/superior_court/raw/{_VALID_HASH}.pdf"
        db_keys: set[str] = set()
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = Exception("S3 error")
        result = classify(key, _NOW, mock_s3, db_keys, set(), verify_bytes=True)
        assert result == CORRECTLY_LABELED_ORPHAN

    def test_present_in_db_skips_byte_verify(self) -> None:
        """classify short-circuits to PRESENT_IN_DB without calling S3."""
        key = f"ca/santa_clara/superior_court/raw/{_VALID_HASH}.pdf"
        db_keys = {key}
        mock_s3 = MagicMock()
        result = classify(key, _NOW, mock_s3, db_keys, set(), verify_bytes=True)
        assert result == PRESENT_IN_DB
        mock_s3.get_object.assert_not_called()


# ---------------------------------------------------------------------------
# 4. fetch_db_s3_keys — correct LIKE query and return type
# ---------------------------------------------------------------------------


class TestFetchDbS3Keys:
    def test_queries_with_correct_like_pattern(self) -> None:
        """fetch_db_s3_keys issues LIKE 'ca/santa_clara/%' query."""
        mock_conn = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = [
            (f"ca/santa_clara/superior_court/raw/{_VALID_HASH}.pdf",),
        ]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        keys = fetch_db_s3_keys(mock_conn, "santa_clara")

        assert isinstance(keys, set)
        assert f"ca/santa_clara/superior_court/raw/{_VALID_HASH}.pdf" in keys

        # Verify the query uses LIKE and passes the right pattern.
        call_args = cursor_mock.execute.call_args
        query = call_args[0][0]
        pattern = call_args[0][1][0]
        assert "LIKE" in query
        assert pattern == "ca/santa_clara/%"

    def test_returns_empty_set_when_no_rows(self) -> None:
        """fetch_db_s3_keys returns an empty set when DB has no matching rows."""
        mock_conn = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        keys = fetch_db_s3_keys(mock_conn, "orange")
        assert keys == set()

    def test_filters_none_s3_keys(self) -> None:
        """fetch_db_s3_keys omits rows with NULL s3_key."""
        mock_conn = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = [
            (f"ca/orange/superior_court/raw/{_VALID_HASH}.pdf",),
            (None,),
        ]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        keys = fetch_db_s3_keys(mock_conn, "orange")
        assert None not in keys
        assert len(keys) == 1


# ---------------------------------------------------------------------------
# 5. fetch_db_content_hashes — correct LIKE query and return type
# ---------------------------------------------------------------------------


class TestFetchDbContentHashes:
    def test_queries_with_correct_like_pattern(self) -> None:
        """fetch_db_content_hashes issues LIKE 'ca/santa_clara/%' query on s3_key column."""
        mock_conn = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = [
            (_VALID_HASH,),
        ]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        hashes = fetch_db_content_hashes(mock_conn, "santa_clara")

        assert isinstance(hashes, set)
        assert _VALID_HASH in hashes

        # Verify the query uses LIKE on s3_key with the right pattern.
        call_args = cursor_mock.execute.call_args
        query = call_args[0][0]
        pattern = call_args[0][1][0]
        assert "LIKE" in query
        assert pattern == "ca/santa_clara/%"
        assert "content_hash" in query
        assert "s3_key" in query

    def test_returns_empty_set_when_no_rows(self) -> None:
        """fetch_db_content_hashes returns an empty set when DB has no matching rows."""
        mock_conn = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        hashes = fetch_db_content_hashes(mock_conn, "orange")
        assert hashes == set()

    def test_filters_none_content_hashes(self) -> None:
        """fetch_db_content_hashes omits rows with NULL content_hash."""
        mock_conn = MagicMock()
        cursor_mock = MagicMock()
        hash_val = "b" * 64
        cursor_mock.fetchall.return_value = [
            (hash_val,),
            (None,),
        ]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        hashes = fetch_db_content_hashes(mock_conn, "orange")
        assert None not in hashes
        assert len(hashes) == 1
        assert hash_val in hashes

    def test_orange_county_pattern(self) -> None:
        """fetch_db_content_hashes uses 'ca/orange/%' pattern for orange county."""
        mock_conn = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        fetch_db_content_hashes(mock_conn, "orange")

        call_args = cursor_mock.execute.call_args
        pattern = call_args[0][1][0]
        assert pattern == "ca/orange/%"


# ---------------------------------------------------------------------------
# 6. run_audit — JSON output schema is stable
# ---------------------------------------------------------------------------


class TestRunAuditJsonSchema:
    def _make_s3_with_keys(self, keys: list[str]) -> MagicMock:
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_s3.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Contents": [_make_obj(k) for k in keys]},
        ]
        return mock_s3

    def _make_conn_with_db_keys(self, db_keys: list[str]) -> MagicMock:
        mock_conn = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = [(k,) for k in db_keys]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return mock_conn

    def test_json_schema_all_clean(self) -> None:
        """run_audit returns stable JSON schema when all objects are in DB."""
        key = f"ca/santa_clara/superior_court/raw/{_VALID_HASH}.pdf"
        mock_s3 = self._make_s3_with_keys([key])
        mock_conn = self._make_conn_with_db_keys([key])

        result = run_audit(
            mock_s3,
            mock_conn,
            {"santa_clara": "ca/santa_clara/"},
            limit=None,
            sample_head_objects=0,
            output_json=True,
        )

        assert "all_clean" in result
        assert result["all_clean"] is True
        assert "counties" in result
        county = result["counties"]["santa_clara"]
        assert "total" in county
        assert "in_db" in county
        assert "in_db_via_content_hash" in county
        assert "orphans" in county
        assert "mislabeled" in county
        assert "orphans_by_month" in county
        assert "sample_verified" in county

        # Verify it's JSON-serializable.
        json.dumps(result, default=str)

    def test_json_schema_with_orphans(self) -> None:
        """run_audit sets all_clean=False and includes orphan counts when orphans found."""
        orphan_key = f"ca/orange/superior_court/raw/{_VALID_HASH}.pdf"
        in_db_key = f"ca/orange/superior_court/raw/{'b' * 64}.pdf"
        mock_s3 = self._make_s3_with_keys([orphan_key, in_db_key])
        mock_conn = self._make_conn_with_db_keys([in_db_key])

        result = run_audit(
            mock_s3,
            mock_conn,
            {"orange": "ca/orange/"},
            limit=None,
            sample_head_objects=0,
            output_json=True,
        )

        assert result["all_clean"] is False
        county = result["counties"]["orange"]
        assert county["orphans"] == 1
        assert county["in_db"] == 1
        assert county["total"] == 2

    def test_in_db_via_content_hash_key_present_in_output(self) -> None:
        """run_audit always includes in_db_via_content_hash key in county summary."""
        key = f"ca/santa_clara/superior_court/raw/{_VALID_HASH}.pdf"
        mock_s3 = self._make_s3_with_keys([key])
        mock_conn = self._make_conn_with_db_keys([key])

        result = run_audit(
            mock_s3,
            mock_conn,
            {"santa_clara": "ca/santa_clara/"},
            limit=None,
            sample_head_objects=0,
            output_json=True,
        )

        county = result["counties"]["santa_clara"]
        assert "in_db_via_content_hash" in county
        assert isinstance(county["in_db_via_content_hash"], int)

    def test_dedup_loser_counted_as_in_db_via_content_hash(self) -> None:
        """run_audit classifies a key as in_db_via_content_hash when its filename hash is in DB content hashes."""
        dedup_loser_key = f"ca/orange/superior_court/raw/{_VALID_HASH}.pdf"

        mock_s3 = self._make_s3_with_keys([dedup_loser_key])

        # s3_keys query returns empty (key not present by s3_key)
        # content_hashes query returns the hash (content present under different key)
        mock_conn = MagicMock()
        cursor_mock = MagicMock()
        # First call → fetch_db_s3_keys → empty; second call → fetch_db_content_hashes → has the hash
        cursor_mock.fetchall.side_effect = [[], [(_VALID_HASH,)]]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = run_audit(
            mock_s3,
            mock_conn,
            {"orange": "ca/orange/"},
            limit=None,
            sample_head_objects=0,
            output_json=True,
        )

        county = result["counties"]["orange"]
        assert county["in_db_via_content_hash"] == 1
        assert county["orphans"] == 0
        assert county["in_db"] == 0
        # total = in_db + in_db_via_content_hash + orphans + mislabeled
        assert county["total"] == 1
        # all_clean still True because there are no true orphans
        assert result["all_clean"] is True

    def test_json_schema_sample_verified_fields(self) -> None:
        """sample_verified entries contain key, last_modified, and classification."""
        content = b"sample doc content"
        actual_hash = hashlib.sha256(content).hexdigest()
        orphan_key = f"ca/orange/superior_court/raw/{actual_hash}.pdf"

        mock_s3 = self._make_s3_with_keys([orphan_key])
        mock_s3.get_object.return_value = {"Body": BytesIO(content)}
        mock_conn = self._make_conn_with_db_keys([])

        result = run_audit(
            mock_s3,
            mock_conn,
            {"orange": "ca/orange/"},
            limit=None,
            sample_head_objects=1,
            output_json=True,
        )

        county = result["counties"]["orange"]
        assert len(county["sample_verified"]) == 1
        sv = county["sample_verified"][0]
        assert "key" in sv
        assert "last_modified" in sv
        assert "classification" in sv
        assert sv["classification"] == CORRECTLY_LABELED_ORPHAN

    def test_both_counties_in_output(self) -> None:
        """run_audit returns both counties when counties dict has both."""
        sc_key = f"ca/santa_clara/superior_court/raw/{_VALID_HASH}.pdf"
        oc_key = f"ca/orange/superior_court/raw/{'c' * 64}.pdf"

        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_s3.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.side_effect = [
            [{"Contents": [_make_obj(sc_key)]}],
            [{"Contents": [_make_obj(oc_key)]}],
        ]

        mock_conn = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = run_audit(
            mock_s3,
            mock_conn,
            {"santa_clara": "ca/santa_clara/", "orange": "ca/orange/"},
            limit=None,
            sample_head_objects=0,
            output_json=True,
        )

        assert "santa_clara" in result["counties"]
        assert "orange" in result["counties"]

    def test_orphans_by_month_aggregation(self) -> None:
        """orphans_by_month aggregates orphan counts by YYYY-MM."""
        jan_key = f"ca/santa_clara/superior_court/raw/{_VALID_HASH}.pdf"
        feb_key = f"ca/santa_clara/superior_court/raw/{'d' * 64}.pdf"

        jan_dt = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        feb_dt = datetime(2026, 2, 20, 10, 0, 0, tzinfo=timezone.utc)

        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_s3.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": jan_key, "LastModified": jan_dt, "Size": 100},
                    {"Key": feb_key, "LastModified": feb_dt, "Size": 200},
                ]
            }
        ]

        mock_conn = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = run_audit(
            mock_s3,
            mock_conn,
            {"santa_clara": "ca/santa_clara/"},
            limit=None,
            sample_head_objects=0,
            output_json=True,
        )

        county = result["counties"]["santa_clara"]
        assert county["orphans_by_month"].get("2026-01") == 1
        assert county["orphans_by_month"].get("2026-02") == 1


# ---------------------------------------------------------------------------
# 6. main() — CLI integration
# ---------------------------------------------------------------------------


class TestMainCli:
    def test_json_flag_produces_parseable_output(
        self, capsys: pytest.CaptureFixture
    ) -> None:  # type: ignore[type-arg]
        """main() with --json prints valid JSON to stdout."""
        key = f"ca/santa_clara/superior_court/raw/{_VALID_HASH}.pdf"

        mock_s3_client = MagicMock()
        mock_paginator = MagicMock()
        mock_s3_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{"Contents": [_make_obj(key)]}]

        mock_conn = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = [(key,)]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("audit_correctly_labeled_s3_orphans.boto3") as mock_boto3_mod:
            with patch(
                "audit_correctly_labeled_s3_orphans.psycopg"
            ) as mock_psycopg_mod:
                mock_boto3_mod.client.return_value = mock_s3_client
                mock_psycopg_mod.connect.return_value.__enter__ = MagicMock(
                    return_value=mock_conn
                )
                mock_psycopg_mod.connect.return_value.__exit__ = MagicMock(
                    return_value=False
                )

                with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                    with patch(
                        "sys.argv",
                        [
                            "audit_correctly_labeled_s3_orphans.py",
                            "--county",
                            "santa_clara",
                            "--json",
                            "--sample-head-objects",
                            "0",
                        ],
                    ):
                        with pytest.raises(SystemExit) as exc_info:
                            _script.main()
                        assert exc_info.value.code == 0

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert "all_clean" in payload
        assert "counties" in payload

    def test_exits_1_when_orphans_found(self, capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
        """main() exits with code 1 when orphans are detected."""
        orphan_key = f"ca/orange/superior_court/raw/{_VALID_HASH}.pdf"

        mock_s3_client = MagicMock()
        mock_paginator = MagicMock()
        mock_s3_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{"Contents": [_make_obj(orphan_key)]}]

        mock_conn = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("audit_correctly_labeled_s3_orphans.boto3") as mock_boto3_mod:
            with patch(
                "audit_correctly_labeled_s3_orphans.psycopg"
            ) as mock_psycopg_mod:
                mock_boto3_mod.client.return_value = mock_s3_client
                mock_psycopg_mod.connect.return_value.__enter__ = MagicMock(
                    return_value=mock_conn
                )
                mock_psycopg_mod.connect.return_value.__exit__ = MagicMock(
                    return_value=False
                )

                with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                    with patch(
                        "sys.argv",
                        [
                            "audit_correctly_labeled_s3_orphans.py",
                            "--county",
                            "orange",
                            "--json",
                            "--sample-head-objects",
                            "0",
                        ],
                    ):
                        with pytest.raises(SystemExit) as exc_info:
                            _script.main()
                        assert exc_info.value.code == 1

    def test_missing_database_url_exits_1(self, capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
        """main() exits with code 1 when DATABASE_URL is not set."""
        env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
        with patch.dict(os.environ, env, clear=True):
            with patch("sys.argv", ["audit_correctly_labeled_s3_orphans.py"]):
                with pytest.raises(SystemExit) as exc_info:
                    _script.main()
                assert exc_info.value.code == 1
