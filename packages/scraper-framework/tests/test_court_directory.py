"""Tests for the CourtDirectory base class — snapshot, dedup, and historical lookup."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from framework.court_directory import CourtDirectory
from framework.hashing import sha256_hex

# ---------------------------------------------------------------------------
# Concrete test subclass
# ---------------------------------------------------------------------------


class _StubDirectory(CourtDirectory):
    """Minimal concrete subclass for testing the base class methods."""

    def __init__(
        self,
        s3_client: Any,
        s3_bucket: str,
        db_conn: Any,
        raw: bytes = b"<html>stub</html>",
        mapping: dict[str, str] | None = None,
    ) -> None:
        super().__init__(s3_client, s3_bucket, db_conn)
        self._raw = raw
        self._mapping = mapping or {"1": "Alice Smith", "2": "Bob Jones"}

    def fetch_current(self) -> tuple[bytes, dict[str, str]]:
        return self._raw, self._mapping


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

COURT_ID = "ca_los_angeles"
RAW_HTML = b"<html><table><tr><td>Judge Smith</td><td>Dept 1</td></tr></table></html>"
MAPPING = {"1": "Judge Smith", "2": "Judge Jones"}


@pytest.fixture()
def s3_client() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def db_conn() -> MagicMock:
    """Mock psycopg connection with cursor context manager."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


@pytest.fixture()
def directory(s3_client: MagicMock, db_conn: MagicMock) -> _StubDirectory:
    return _StubDirectory(
        s3_client=s3_client,
        s3_bucket="test-bucket",
        db_conn=db_conn,
        raw=RAW_HTML,
        mapping=MAPPING,
    )


# ---------------------------------------------------------------------------
# save_snapshot tests
# ---------------------------------------------------------------------------


class TestSaveSnapshot:
    """Tests for CourtDirectory.save_snapshot()."""

    def test_archives_to_s3(self, directory: _StubDirectory, s3_client: MagicMock) -> None:
        """Raw bytes should be uploaded to S3 with correct key pattern."""
        # No existing snapshot -> not a duplicate
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None

        directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

        s3_client.put_object.assert_called_once()
        call_kwargs = s3_client.put_object.call_args.kwargs
        assert call_kwargs["Bucket"] == "test-bucket"
        assert call_kwargs["Key"].startswith(f"directories/{COURT_ID}/")
        assert call_kwargs["Key"].endswith(".html")
        assert call_kwargs["Body"] == RAW_HTML
        assert call_kwargs["ContentType"] == "text/html"

    def test_inserts_into_db_when_new(self, directory: _StubDirectory) -> None:
        """A new snapshot should be inserted into the DB."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None  # no existing snapshot

        result = directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

        assert result is True
        directory._conn.commit.assert_called_once()
        # Verify the INSERT was called
        calls = cursor.execute.call_args_list
        # First call is the dedup SELECT, second is the INSERT
        assert len(calls) == 2
        insert_sql = calls[1].args[0]
        assert "INSERT INTO court_directory_snapshots" in insert_sql

    def test_skips_db_insert_when_duplicate(self, directory: _StubDirectory) -> None:
        """When content_hash matches the latest snapshot, skip the DB insert."""
        content_hash = sha256_hex(RAW_HTML)
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (content_hash,)  # same hash

        result = directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

        assert result is False
        directory._conn.commit.assert_not_called()

    def test_inserts_when_different_hash(self, directory: _StubDirectory) -> None:
        """When content_hash differs from latest, insert a new snapshot."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ("old_hash_different",)

        result = directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

        assert result is True
        directory._conn.commit.assert_called_once()

    def test_mapping_stored_as_sorted_json(self, directory: _StubDirectory) -> None:
        """The mapping should be stored as sorted JSON for consistency."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None  # no existing snapshot

        directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

        calls = cursor.execute.call_args_list
        insert_params = calls[1].args[1]
        # The mapping JSON is the 4th parameter (index 3)
        stored_json = insert_params[3]
        assert stored_json == json.dumps(MAPPING, sort_keys=True)


# ---------------------------------------------------------------------------
# get_snapshot tests
# ---------------------------------------------------------------------------


class TestGetSnapshot:
    """Tests for CourtDirectory.get_snapshot()."""

    def test_returns_mapping_when_found(self, directory: _StubDirectory) -> None:
        """Should return the parsed mapping dict when a snapshot exists."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (MAPPING,)

        result = directory.get_snapshot(COURT_ID, datetime.now(UTC))

        assert result == MAPPING

    def test_returns_none_when_no_snapshot(self, directory: _StubDirectory) -> None:
        """Should return None when no snapshot exists before the given date."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None

        result = directory.get_snapshot(COURT_ID, datetime.now(UTC))

        assert result is None

    def test_query_uses_correct_parameters(self, directory: _StubDirectory) -> None:
        """The SELECT should filter by court_id and captured_at <= as_of."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None

        as_of = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        directory.get_snapshot(COURT_ID, as_of)

        cursor.execute.assert_called_once()
        sql, params = cursor.execute.call_args.args
        assert "captured_at <=" in sql
        assert "ORDER BY captured_at DESC" in sql
        assert "LIMIT 1" in sql
        assert params == (COURT_ID, as_of)

    def test_handles_json_string_mapping(self, directory: _StubDirectory) -> None:
        """If the DB returns mapping as a JSON string, it should be parsed."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (json.dumps(MAPPING),)

        result = directory.get_snapshot(COURT_ID, datetime.now(UTC))

        assert result == MAPPING


# ---------------------------------------------------------------------------
# fetch_and_snapshot tests
# ---------------------------------------------------------------------------


class TestFetchAndSnapshot:
    """Tests for CourtDirectory.fetch_and_snapshot()."""

    def test_calls_fetch_and_save(self, directory: _StubDirectory) -> None:
        """Should call fetch_current() then save_snapshot() and return mapping."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None  # no existing snapshot

        result = directory.fetch_and_snapshot(COURT_ID)

        assert result == MAPPING
        # Verify S3 upload happened
        directory._s3.put_object.assert_called_once()

    def test_returns_mapping_from_fetch(self, directory: _StubDirectory) -> None:
        """The returned mapping should come from fetch_current()."""
        custom_mapping = {"10": "Custom Judge"}
        directory._mapping = custom_mapping
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None

        result = directory.fetch_and_snapshot(COURT_ID)

        assert result == custom_mapping


# ---------------------------------------------------------------------------
# _is_duplicate tests
# ---------------------------------------------------------------------------


class TestIsDuplicate:
    """Tests for CourtDirectory._is_duplicate()."""

    def test_returns_false_when_no_snapshots(self, directory: _StubDirectory) -> None:
        """No prior snapshots means it's not a duplicate."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None

        assert directory._is_duplicate(COURT_ID, "abc123") is False

    def test_returns_true_when_hash_matches(self, directory: _StubDirectory) -> None:
        """Matching content hash means it's a duplicate."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ("abc123",)

        assert directory._is_duplicate(COURT_ID, "abc123") is True

    def test_returns_false_when_hash_differs(self, directory: _StubDirectory) -> None:
        """Different content hash means it's not a duplicate."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ("old_hash",)

        assert directory._is_duplicate(COURT_ID, "new_hash") is False
