"""Tests for cleanup_legacy_date_partitioned_s3 script.

Tests cover:
- DATE_PARTITIONED_RE regex matching
- list_date_partitioned_keys filtering flat-hash keys
- delete_in_batches respects 1000-object batch limit
- db_safety_check returns count and handles both zero and nonzero
- main aborts when DB count is nonzero
- main dry-run prints per-county counts
- main --apply triggers deletes

The script depends on boto3 and psycopg, which may not be available in the
lightweight CI scripts-tests environment. We mock them in sys.modules before
importing the script under test.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Pre-import mocking — the script imports boto3 and psycopg at module level,
# which may not be installed in the CI scripts-tests environment.
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_mock_boto3 = MagicMock()
_mock_psycopg = MagicMock()

_modules_to_mock = {
    "boto3": _mock_boto3,
    "psycopg": _mock_psycopg,
}

_saved_modules: dict[str, object] = {}
for mod_name, mock_mod in _modules_to_mock.items():
    if mod_name in sys.modules:
        _saved_modules[mod_name] = sys.modules[mod_name]
    sys.modules[mod_name] = mock_mod

import cleanup_legacy_date_partitioned_s3  # noqa: E402

# Restore sys.modules so the mock injection doesn't break other test files
# that use @patch("boto3.client") (e.g. test_api_error_check.py). The cleanup
# script's module-level boto3/psycopg bindings remain as mocks (captured at
# import time), so tests in this file continue to work correctly.
for _mod_name in list(_modules_to_mock.keys()):
    if _mod_name in _saved_modules:
        sys.modules[_mod_name] = _saved_modules[_mod_name]
    elif _mod_name in sys.modules:
        del sys.modules[_mod_name]

DATE_PARTITIONED_RE = cleanup_legacy_date_partitioned_s3.DATE_PARTITIONED_RE
list_date_partitioned_keys = (
    cleanup_legacy_date_partitioned_s3.list_date_partitioned_keys
)
db_safety_check = cleanup_legacy_date_partitioned_s3.db_safety_check
delete_in_batches = cleanup_legacy_date_partitioned_s3.delete_in_batches


# ---------------------------------------------------------------------------
# Test 1: DATE_PARTITIONED_RE matches date-partitioned keys but not flat-hash
# ---------------------------------------------------------------------------


class TestRegexMatchesDatePartitionedKeys:
    """Tests for DATE_PARTITIONED_RE pattern."""

    def test_matches_santa_clara_date_partitioned(self) -> None:
        """Matches a Santa Clara date-partitioned key."""
        key = "ca/santa_clara/superior_court/raw/2026/03/28/a1b2c3d4-uuid.pdf"
        assert DATE_PARTITIONED_RE.match(key) is not None

    def test_does_not_match_orange_flat_hash(self) -> None:
        """Does not match an Orange flat-hash (content-addressed) key."""
        # 64-char hex hash — content-addressed, not date-partitioned
        flat_hash = "deadbeef" * 8  # 64 chars
        key = f"ca/orange/superior_court/raw/{flat_hash}.pdf"
        assert DATE_PARTITIONED_RE.match(key) is None

    def test_matches_orange_date_partitioned(self) -> None:
        """Matches an Orange date-partitioned key."""
        key = "ca/orange/superior_court/raw/2025/11/15/some-uuid.html"
        assert DATE_PARTITIONED_RE.match(key) is not None

    def test_does_not_match_santa_clara_flat_hash(self) -> None:
        """Does not match a Santa Clara flat-hash key."""
        flat_hash = "abcdef01" * 8
        key = f"ca/santa_clara/superior_court/raw/{flat_hash}.pdf"
        assert DATE_PARTITIONED_RE.match(key) is None


# ---------------------------------------------------------------------------
# Test 2: list_date_partitioned_keys filters flat-hash keys
# ---------------------------------------------------------------------------


def test_list_date_partitioned_keys_filters_flat_hash() -> None:
    """list_date_partitioned_keys returns only date-partitioned keys from paginator."""
    date_key = "ca/santa_clara/superior_court/raw/2026/03/28/uuid-abc.pdf"
    flat_hash = "deadbeef" * 8
    flat_key = f"ca/santa_clara/superior_court/raw/{flat_hash}.pdf"

    # Build a mock paginator that returns one page with both types
    mock_s3 = MagicMock()
    mock_paginator = MagicMock()
    mock_s3.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [
        {"Contents": [{"Key": date_key}, {"Key": flat_key}]},
    ]

    result = list_date_partitioned_keys(mock_s3, "ca/santa_clara/")

    assert date_key in result
    assert flat_key not in result
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Test 3: delete_in_batches respects 1000-object batch limit
# ---------------------------------------------------------------------------


def test_delete_in_batches_respects_1000_limit() -> None:
    """delete_in_batches sends exactly 3 delete_objects calls for 2500 keys."""
    mock_s3 = MagicMock()
    keys = [
        f"ca/santa_clara/superior_court/raw/2026/03/28/uuid-{i}.pdf"
        for i in range(2500)
    ]

    delete_in_batches(mock_s3, keys, dry_run=False)

    assert mock_s3.delete_objects.call_count == 3
    # Verify batch sizes: 1000, 1000, 500
    calls = mock_s3.delete_objects.call_args_list
    assert len(calls[0][1]["Delete"]["Objects"]) == 1000
    assert len(calls[1][1]["Delete"]["Objects"]) == 1000
    assert len(calls[2][1]["Delete"]["Objects"]) == 500


def test_delete_in_batches_dry_run_skips_delete() -> None:
    """delete_in_batches in dry_run mode does not call delete_objects."""
    mock_s3 = MagicMock()
    keys = [
        f"ca/santa_clara/superior_court/raw/2026/03/28/uuid-{i}.pdf" for i in range(10)
    ]

    deleted = delete_in_batches(mock_s3, keys, dry_run=True)

    mock_s3.delete_objects.assert_not_called()
    assert deleted == 0


# ---------------------------------------------------------------------------
# Test 4: db_safety_check returns count
# ---------------------------------------------------------------------------


def test_db_safety_check_returns_count() -> None:
    """db_safety_check returns the count from the DB for both 0 and 7."""
    for expected_count in (0, 7):
        mock_conn = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchone.return_value = (expected_count,)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = db_safety_check(mock_conn)
        assert result == expected_count


# ---------------------------------------------------------------------------
# Test 5: main aborts when DB count is nonzero
# ---------------------------------------------------------------------------


def test_main_aborts_when_db_count_nonzero() -> None:
    """main() exits nonzero and fires no delete_objects calls when DB has rows."""
    mock_s3_client = MagicMock()
    mock_conn = MagicMock()

    # Paginator returns a date-partitioned key for each county
    mock_paginator = MagicMock()
    mock_s3_client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [
        {"Contents": [{"Key": "ca/santa_clara/superior_court/raw/2026/03/28/x.pdf"}]},
    ]

    # DB returns 3 rows — should abort
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = (3,)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch("cleanup_legacy_date_partitioned_s3.boto3") as mock_boto3_mod:
        with patch("cleanup_legacy_date_partitioned_s3.psycopg") as mock_psycopg_mod:
            mock_boto3_mod.client.return_value = mock_s3_client
            mock_psycopg_mod.connect.return_value.__enter__ = MagicMock(
                return_value=mock_conn
            )
            mock_psycopg_mod.connect.return_value.__exit__ = MagicMock(
                return_value=False
            )

            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                with patch(
                    "sys.argv", ["cleanup_legacy_date_partitioned_s3.py", "--apply"]
                ):
                    with pytest.raises(SystemExit) as exc_info:
                        cleanup_legacy_date_partitioned_s3.main()
                    assert exc_info.value.code != 0

    mock_s3_client.delete_objects.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: main dry-run prints per-county counts
# ---------------------------------------------------------------------------


def test_main_dry_run_prints_per_county_counts(capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
    """main() dry-run prints county key counts without deleting."""
    mock_s3_client = MagicMock()
    mock_conn = MagicMock()

    # Paginator returns date-partitioned keys for each county invocation
    sc_key = "ca/santa_clara/superior_court/raw/2026/03/28/a.pdf"
    oc_key = "ca/orange/superior_court/raw/2025/11/15/b.pdf"

    mock_paginator = MagicMock()
    mock_s3_client.get_paginator.return_value = mock_paginator
    # Called once per county (santa_clara, orange) — return different pages per call
    mock_paginator.paginate.side_effect = [
        [{"Contents": [{"Key": sc_key}]}],
        [{"Contents": [{"Key": oc_key}]}],
    ]

    # DB safety check returns 0 — safe to proceed
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = (0,)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch("cleanup_legacy_date_partitioned_s3.boto3") as mock_boto3_mod:
        with patch("cleanup_legacy_date_partitioned_s3.psycopg") as mock_psycopg_mod:
            mock_boto3_mod.client.return_value = mock_s3_client
            mock_psycopg_mod.connect.return_value.__enter__ = MagicMock(
                return_value=mock_conn
            )
            mock_psycopg_mod.connect.return_value.__exit__ = MagicMock(
                return_value=False
            )

            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                with patch(
                    "sys.argv", ["cleanup_legacy_date_partitioned_s3.py", "--dry-run"]
                ):
                    cleanup_legacy_date_partitioned_s3.main()

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "santa_clara" in output.lower() or "Santa Clara" in output
    assert "orange" in output.lower() or "Orange" in output
    mock_s3_client.delete_objects.assert_not_called()


# ---------------------------------------------------------------------------
# Test 7: main --apply deletes all matching keys
# ---------------------------------------------------------------------------


def test_main_apply_deletes_all_matching_keys() -> None:
    """main() --apply triggers delete_objects for matching date-partitioned keys."""
    mock_s3_client = MagicMock()
    mock_conn = MagicMock()

    sc_key = "ca/santa_clara/superior_court/raw/2026/03/28/a.pdf"
    oc_key = "ca/orange/superior_court/raw/2025/11/15/b.pdf"

    mock_paginator = MagicMock()
    mock_s3_client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.side_effect = [
        [{"Contents": [{"Key": sc_key}]}],
        [{"Contents": [{"Key": oc_key}]}],
    ]

    # DB safety check returns 0 — safe to proceed
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = (0,)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch("cleanup_legacy_date_partitioned_s3.boto3") as mock_boto3_mod:
        with patch("cleanup_legacy_date_partitioned_s3.psycopg") as mock_psycopg_mod:
            mock_boto3_mod.client.return_value = mock_s3_client
            mock_psycopg_mod.connect.return_value.__enter__ = MagicMock(
                return_value=mock_conn
            )
            mock_psycopg_mod.connect.return_value.__exit__ = MagicMock(
                return_value=False
            )

            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                with patch(
                    "sys.argv", ["cleanup_legacy_date_partitioned_s3.py", "--apply"]
                ):
                    cleanup_legacy_date_partitioned_s3.main()

    # delete_objects should have been called
    assert mock_s3_client.delete_objects.call_count >= 1
    # Verify both keys were passed for deletion
    all_deleted_keys: list[str] = []
    for c in mock_s3_client.delete_objects.call_args_list:
        for obj in c[1]["Delete"]["Objects"]:
            all_deleted_keys.append(obj["Key"])
    assert sc_key in all_deleted_keys
    assert oc_key in all_deleted_keys
