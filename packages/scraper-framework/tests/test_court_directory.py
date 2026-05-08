"""Tests for the CourtDirectory base class — snapshot, dedup, and historical lookup."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

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


def _head_object_404(**kwargs: Any) -> None:
    """Simulate S3 HeadObject returning 404 (object not found)."""
    raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")


@pytest.fixture()
def s3_client() -> MagicMock:
    mock = MagicMock()
    # Default: HeadObject returns 404 (object doesn't exist).
    mock.head_object.side_effect = _head_object_404
    return mock


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

    def test_uses_content_addressed_s3_key(
        self, directory: _StubDirectory, s3_client: MagicMock
    ) -> None:
        """S3 key should use content hash, not timestamp."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None

        directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

        expected_hash = sha256_hex(RAW_HTML)
        expected_key = f"directories/{COURT_ID}/{expected_hash}.html"
        call_kwargs = s3_client.put_object.call_args.kwargs
        assert call_kwargs["Key"] == expected_key

    def test_archives_to_s3_when_not_exists(
        self, directory: _StubDirectory, s3_client: MagicMock
    ) -> None:
        """Raw bytes should be uploaded to S3 when HeadObject returns 404."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None

        directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

        s3_client.head_object.assert_called_once()
        s3_client.put_object.assert_called_once()
        call_kwargs = s3_client.put_object.call_args.kwargs
        assert call_kwargs["Bucket"] == "test-bucket"
        assert call_kwargs["Body"] == RAW_HTML
        assert call_kwargs["ContentType"] == "text/html"

    def test_skips_s3_upload_when_already_exists(
        self, directory: _StubDirectory, s3_client: MagicMock
    ) -> None:
        """When HeadObject succeeds (object exists), skip the upload."""
        s3_client.head_object.side_effect = None
        s3_client.head_object.return_value = {}
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None

        directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

        s3_client.head_object.assert_called_once()
        s3_client.put_object.assert_not_called()

    def test_reraises_non_404_head_object_error(
        self, directory: _StubDirectory, s3_client: MagicMock
    ) -> None:
        """Non-404 HeadObject errors should be re-raised."""
        s3_client.head_object.side_effect = ClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject"
        )
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None

        with pytest.raises(ClientError) as exc_info:
            directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

        assert exc_info.value.response["Error"]["Code"] == "403"

    def test_inserts_into_db_when_new(self, directory: _StubDirectory) -> None:
        """A new snapshot should be inserted into the DB."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None  # no existing snapshot

        result = directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

        assert result is True
        # Two commits expected: one for the snapshot insert, one for the
        # seed-judges follow-up (#4370).  The seeder runs in its own
        # transactional unit so a seed failure cannot roll back the
        # snapshot insert that already committed.
        assert directory._conn.commit.call_count == 2
        # Verify the snapshot INSERT was issued.  The seeder adds further
        # SELECTs against court_directory_snapshots and courts plus
        # potential INSERT INTO judges; we only assert the snapshot path
        # was exercised here.
        calls = cursor.execute.call_args_list
        snapshot_inserts = [
            c for c in calls if "INSERT INTO court_directory_snapshots" in c.args[0]
        ]
        assert len(snapshot_inserts) == 1

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
        # Two commits: snapshot insert + seed-judges follow-up (#4370).
        assert directory._conn.commit.call_count == 2

    def test_mapping_stored_as_sorted_json(self, directory: _StubDirectory) -> None:
        """The mapping should be stored as sorted JSON for consistency."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None  # no existing snapshot

        directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

        # Find the snapshot INSERT specifically — the seed step (#4370)
        # adds further SELECTs and possibly judge INSERTs after, so we
        # cannot rely on a fixed call index.
        snapshot_inserts = [
            c
            for c in cursor.execute.call_args_list
            if "INSERT INTO court_directory_snapshots" in c.args[0]
        ]
        assert len(snapshot_inserts) == 1
        insert_params = snapshot_inserts[0].args[1]
        # The mapping JSON is the 4th parameter (index 3)
        stored_json = insert_params[3]
        assert stored_json == json.dumps(MAPPING, sort_keys=True)

    def test_s3_key_uses_content_hash_not_timestamp(
        self, directory: _StubDirectory, s3_client: MagicMock
    ) -> None:
        """S3 key format should be directories/{court_id}/{content_hash}.html."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None

        directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

        expected_hash = sha256_hex(RAW_HTML)
        head_call_kwargs = s3_client.head_object.call_args.kwargs
        assert head_call_kwargs["Key"] == f"directories/{COURT_ID}/{expected_hash}.html"
        assert head_call_kwargs["Bucket"] == "test-bucket"

    def test_db_insert_uses_content_addressed_s3_key(self, directory: _StubDirectory) -> None:
        """The s3_key stored in DB should be the content-addressed key."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None

        directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

        expected_hash = sha256_hex(RAW_HTML)
        expected_key = f"directories/{COURT_ID}/{expected_hash}.html"
        # Find the snapshot INSERT specifically — the seed step (#4370)
        # adds further calls after this one.
        snapshot_inserts = [
            c
            for c in cursor.execute.call_args_list
            if "INSERT INTO court_directory_snapshots" in c.args[0]
        ]
        assert len(snapshot_inserts) == 1
        insert_params = snapshot_inserts[0].args[1]
        # s3_key is the 3rd parameter (index 2)
        assert insert_params[2] == expected_key

    def test_s3_already_exists_but_db_insert_still_happens(
        self, directory: _StubDirectory, s3_client: MagicMock
    ) -> None:
        """When S3 already has the object but DB doesn't, still insert into DB."""
        # HeadObject succeeds — content already in S3
        s3_client.head_object.side_effect = None
        s3_client.head_object.return_value = {}
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        # _is_duplicate returns None (no existing DB row) — not a duplicate
        cursor.fetchone.return_value = None

        result = directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

        assert result is True
        s3_client.put_object.assert_not_called()  # skip upload
        # Two commits: snapshot insert + seed-judges follow-up (#4370).
        assert directory._conn.commit.call_count == 2

    def test_clears_roster_cache_on_new_snapshot(self, directory: _StubDirectory) -> None:
        """Saving a new snapshot should clear the roster name cache."""
        from ingestion.db import _roster_cache, clear_roster_cache

        # Pre-populate the cache with a fake entry
        _roster_cache["some-court-uuid"] = (0.0, ["Judge Cached"])

        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None  # not a duplicate

        directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

        # The cache should have been cleared
        assert "some-court-uuid" not in _roster_cache

        # Clean up
        clear_roster_cache()

    def test_does_not_clear_roster_cache_on_duplicate(self, directory: _StubDirectory) -> None:
        """When content is a duplicate (no DB insert), cache is NOT cleared."""
        from ingestion.db import _roster_cache, clear_roster_cache

        # Pre-populate the cache
        _roster_cache["some-court-uuid"] = (0.0, ["Judge Cached"])

        cursor = directory._conn.cursor.return_value.__enter__.return_value
        # _is_duplicate check: return the same content_hash -> is duplicate
        cursor.fetchone.return_value = (sha256_hex(RAW_HTML),)

        result = directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

        assert result is False
        # Cache should NOT have been cleared (no new snapshot was inserted)
        assert "some-court-uuid" in _roster_cache

        # Clean up
        clear_roster_cache()

    def test_seeds_judges_from_snapshot_after_insert(self, directory: _StubDirectory) -> None:
        """After a new snapshot is inserted, judges are seeded from the
        same DB connection (#4370).

        AC #3: "Helper wired into the daily court-directory snapshot
        refresh — integration test that runs the snapshot writer,
        asserts new judges are inserted on the same DB transaction."
        """
        from unittest.mock import patch

        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None  # not a duplicate

        with patch("ingestion.judge_seed.seed_judges_from_directory_snapshots") as mock_seed:
            mock_seed.return_value = {
                "courts": 1,
                "candidates": 2,
                "inserted": 2,
                "skipped_existing": 0,
                "skipped_invalid": 0,
                "skipped_no_court": 0,
            }

            result = directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

            assert result is True
            # The seeder must be called with the same DB connection the
            # snapshot was just written on, scoped to the just-inserted
            # court_id.
            mock_seed.assert_called_once_with(directory._conn, only_court_id=COURT_ID)

    def test_skips_seed_when_snapshot_is_duplicate(self, directory: _StubDirectory) -> None:
        """When the snapshot is a duplicate (no new DB row), the seeder is
        not called — there's no new mapping to seed from."""
        from unittest.mock import patch

        cursor = directory._conn.cursor.return_value.__enter__.return_value
        # _is_duplicate sees the same content_hash -> duplicate.
        cursor.fetchone.return_value = (sha256_hex(RAW_HTML),)

        with patch("ingestion.judge_seed.seed_judges_from_directory_snapshots") as mock_seed:
            result = directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

            assert result is False
            mock_seed.assert_not_called()

    def test_seed_failure_does_not_break_save_snapshot(self, directory: _StubDirectory) -> None:
        """A seed failure must not roll back the snapshot insert that
        already committed.  Best-effort, defensive — we never want a
        seed-side bug to lose the directory snapshot."""
        from unittest.mock import patch

        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None  # not a duplicate

        with patch(
            "ingestion.judge_seed.seed_judges_from_directory_snapshots",
            side_effect=RuntimeError("simulated seed failure"),
        ):
            # Must not raise — the snapshot insert already happened.
            result = directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

            assert result is True


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
        # Verify S3 upload happened (HeadObject returns 404 by default)
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


# ---------------------------------------------------------------------------
# get_mapping_for_date tests
# ---------------------------------------------------------------------------


class TestGetMappingForDate:
    """Tests for CourtDirectory.get_mapping_for_date()."""

    def test_returns_snapshot_when_found(self, directory: _StubDirectory) -> None:
        """Should return the snapshot mapping when one exists before as_of."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        historical = {"1": "Old Judge", "2": "Other Judge"}
        cursor.fetchone.return_value = (historical,)

        result = directory.get_mapping_for_date(COURT_ID, datetime(2026, 3, 1, tzinfo=UTC))

        assert result == historical

    def test_returns_fallback_when_no_snapshot(self, directory: _StubDirectory) -> None:
        """Should return fallback when no snapshot predates as_of."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None

        fallback = {"10": "Fallback Judge"}
        result = directory.get_mapping_for_date(
            COURT_ID, datetime(2026, 3, 1, tzinfo=UTC), fallback=fallback
        )

        assert result == fallback

    def test_returns_none_when_no_snapshot_and_no_fallback(self, directory: _StubDirectory) -> None:
        """Should return None when no snapshot exists and no fallback provided."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None

        result = directory.get_mapping_for_date(COURT_ID, datetime(2026, 3, 1, tzinfo=UTC))

        assert result is None

    def test_prefers_snapshot_over_fallback(self, directory: _StubDirectory) -> None:
        """When a snapshot exists, fallback should be ignored."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        historical = {"1": "Historical Judge"}
        cursor.fetchone.return_value = (historical,)

        fallback = {"1": "Fallback Judge"}
        result = directory.get_mapping_for_date(
            COURT_ID, datetime(2026, 3, 1, tzinfo=UTC), fallback=fallback
        )

        assert result == historical
        assert result != fallback

    def test_passes_correct_parameters(self, directory: _StubDirectory) -> None:
        """Should pass court_id and as_of to get_snapshot."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None

        as_of = datetime(2026, 2, 15, 10, 30, 0, tzinfo=UTC)
        directory.get_mapping_for_date(COURT_ID, as_of)

        cursor.execute.assert_called_once()
        sql, params = cursor.execute.call_args.args
        assert params == (COURT_ID, as_of)
