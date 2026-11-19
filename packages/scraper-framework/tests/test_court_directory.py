"""Tests for the CourtDirectory base class — snapshot, dedup, and historical lookup."""

from __future__ import annotations

import importlib
import json
import os
import pkgutil
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from framework.court_directory import SEALED_METHODS, CourtDirectory
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

    def test_pdf_raw_uses_pdf_key_and_content_type(
        self, directory: _StubDirectory, s3_client: MagicMock
    ) -> None:
        """A PDF directory (LA weekly directory, #4874) is archived as .pdf / application/pdf."""
        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        raw_pdf = b"%PDF-1.7\n fake directory body"

        directory.save_snapshot(raw_pdf, MAPPING, COURT_ID)

        call_kwargs = s3_client.put_object.call_args.kwargs
        assert call_kwargs["Key"] == f"directories/{COURT_ID}/{sha256_hex(raw_pdf)}.pdf"
        assert call_kwargs["ContentType"] == "application/pdf"
        inserts = [
            c
            for c in cursor.execute.call_args_list
            if "INSERT INTO court_directory_snapshots" in c.args[0]
        ]
        assert len(inserts) == 1
        assert inserts[0].args[1][2] == call_kwargs["Key"]

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

        # The judge seed still runs (and commits) on this path (#5161); it is
        # patched out so only the snapshot write is under test here.
        from unittest.mock import patch

        with patch(
            "ingestion.judge_seed.seed_judges_from_directory_snapshots",
            return_value={"inserted": 0},
        ):
            result = directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

        assert result is False
        snapshot_inserts = [
            c
            for c in cursor.execute.call_args_list
            if "INSERT INTO court_directory_snapshots" in c.args[0]
        ]
        assert snapshot_inserts == []

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

    def test_seeds_judges_when_snapshot_is_duplicate(self, directory: _StubDirectory) -> None:
        """An unchanged roster (no new DB row) still seeds judges (#5161).

        The latest snapshot may never have been seeded (San Bernardino's
        save_snapshot override skipped the seed), or derived.judges may have
        been rebuilt since.  The seed is idempotent, so running it on every
        refresh catches those courts up.
        """
        from unittest.mock import patch

        cursor = directory._conn.cursor.return_value.__enter__.return_value
        # _is_duplicate sees the same content_hash -> duplicate.
        cursor.fetchone.return_value = (sha256_hex(RAW_HTML),)

        with patch(
            "ingestion.judge_seed.seed_judges_from_directory_snapshots",
            return_value={"inserted": 3},
        ) as mock_seed:
            result = directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

            assert result is False
            mock_seed.assert_called_once_with(directory._conn, only_court_id=COURT_ID)
        directory._conn.commit.assert_called_once()

    def test_seed_failure_on_duplicate_does_not_break_save_snapshot(
        self, directory: _StubDirectory
    ) -> None:
        """A seed failure on the unchanged-roster path is logged, rolled
        back, and does not raise."""
        from unittest.mock import patch

        cursor = directory._conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (sha256_hex(RAW_HTML),)

        with patch(
            "ingestion.judge_seed.seed_judges_from_directory_snapshots",
            side_effect=RuntimeError("simulated seed failure"),
        ):
            result = directory.save_snapshot(RAW_HTML, MAPPING, COURT_ID)

        assert result is False
        directory._conn.rollback.assert_called_once()
        directory._conn.commit.assert_not_called()

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


# ---------------------------------------------------------------------------
# Real Postgres: an unchanged roster refresh seeds missing judges (#5161)
# ---------------------------------------------------------------------------

_DSN = os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.skipif(not _DSN, reason="TEST_DATABASE_URL not set")
class TestUnchangedRosterSeedsMissingJudgesPg:
    """Real Postgres: ``fetch_and_snapshot`` on a roster whose content hash
    matches the stored snapshot inserts no snapshot row but still seeds the
    roster judges missing from ``judges`` (#5161).

    San Bernardino's latest snapshot was stored without a seed, and every
    later refresh was unchanged, so its 60 roster judges were never seeded.
    """

    def test_unchanged_roster_seeds_missing_judge(self) -> None:
        import psycopg

        suffix = uuid.uuid4().hex[:12]
        court_code = f"ca-seed5161-{suffix}"
        snapshot_court_id = court_code.replace("-", "_")
        raw = f"<html>roster {suffix}</html>".encode()
        mapping = {"1": "Alice M. Rosterseed", "2": "Bob Q. Presentjudge"}

        conn = psycopg.connect(_DSN, autocommit=False)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO courts (state, county, court_name, court_code) "
                    "VALUES ('CA', 'Seed5161', 'Superior Court', %s) RETURNING id",
                    (court_code,),
                )
                court_uuid = str(cur.fetchone()[0])
                # The roster as already stored: same content hash, so the
                # refresh below is a duplicate.  It was never seeded.
                cur.execute(
                    "INSERT INTO court_directory_snapshots "
                    "(court_id, captured_at, s3_key, mapping, content_hash) "
                    "VALUES (%s, NOW(), %s, %s::jsonb, %s)",
                    (
                        snapshot_court_id,
                        f"directories/{snapshot_court_id}/{sha256_hex(raw)}.html",
                        json.dumps(mapping),
                        sha256_hex(raw),
                    ),
                )
                cur.execute(
                    "INSERT INTO judges (canonical_name, court_id) VALUES (%s, %s::uuid)",
                    ("Bob Q. Presentjudge", court_uuid),
                )
            conn.commit()

            s3 = MagicMock()  # head_object succeeds: raw already archived
            directory = _StubDirectory(
                s3_client=s3, s3_bucket="test-bucket", db_conn=conn, raw=raw, mapping=mapping
            )

            result = directory.fetch_and_snapshot(snapshot_court_id)

            assert result == mapping
            s3.put_object.assert_not_called()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM court_directory_snapshots WHERE court_id = %s",
                    (snapshot_court_id,),
                )
                assert cur.fetchone()[0] == 1
                cur.execute(
                    "SELECT canonical_name FROM judges WHERE court_id = %s::uuid "
                    "ORDER BY canonical_name",
                    (court_uuid,),
                )
                names = [row[0] for row in cur.fetchall()]
            assert names == ["Alice M. Rosterseed", "Bob Q. Presentjudge"]
        finally:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM courts WHERE court_code = %s", (court_code,))
                for (cleanup_court,) in cur.fetchall():
                    cur.execute("DELETE FROM judges WHERE court_id = %s", (cleanup_court,))
                    cur.execute("DELETE FROM courts WHERE id = %s", (cleanup_court,))
                cur.execute(
                    "DELETE FROM court_directory_snapshots WHERE court_id = %s",
                    (snapshot_court_id,),
                )
            conn.commit()
            conn.close()


# ---------------------------------------------------------------------------
# Sealed base-class methods (#5163)
# ---------------------------------------------------------------------------


def _court_directory_subclasses() -> list[type[CourtDirectory]]:
    """Import every module under ``courts`` and return its CourtDirectory subclasses.

    Walks the package rather than a registry (``framework.runner`` and
    ``scripts/fetch_rosters.py`` list different courts), so a new court is
    covered as soon as its module exists.  An import that raises the
    ``__init_subclass__`` TypeError fails the calling test with its message.
    """
    import courts

    for info in pkgutil.walk_packages(courts.__path__, prefix="courts."):
        importlib.import_module(info.name)

    found: set[type[CourtDirectory]] = set()
    pending = list(CourtDirectory.__subclasses__())
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if cls.__module__.startswith("courts."):
            found.add(cls)
    return sorted(found, key=lambda c: (c.__module__, c.__qualname__))


def test_save_snapshot_not_overridden_by_any_court_directory() -> None:
    """No court directory defines its own save_snapshot or seeding helpers.

    SB and SF copies of save_snapshot skipped the derived.judges seed and the
    roster-cache clear (#5161).  Per-court storage needs belong in a hook on
    the base method, not a copy of it.
    """
    subclasses = _court_directory_subclasses()
    names = {cls.__name__ for cls in subclasses}
    # Guard against a vacuous walk: these courts must be discovered.
    assert {"SanBernardinoCourtDirectory", "SFCourtDirectory", "LACourtDirectory"} <= names
    assert len(subclasses) >= 11

    offenders = [
        f"{cls.__module__}.{cls.__qualname__}.{method}"
        for cls in subclasses
        for method in sorted(SEALED_METHODS.intersection(vars(cls)))
    ]
    assert offenders == [], (
        "CourtDirectory subclasses must not override sealed base methods; "
        f"extend CourtDirectory.save_snapshot instead: {offenders}"
    )


@pytest.mark.parametrize("method", sorted(SEALED_METHODS))
def test_save_snapshot_not_overridden_rejected_at_class_creation(method: str) -> None:
    """Defining a sealed method on a subclass raises TypeError naming the base hook."""

    def _override(self: Any, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        return None

    with pytest.raises(TypeError) as excinfo:
        type(
            "_OverridingDirectory",
            (CourtDirectory,),
            {"fetch_current": lambda self: (b"", {}), method: _override},
        )
    message = str(excinfo.value)
    assert method in message
    assert "CourtDirectory.save_snapshot" in message
    assert "fetch_current()" in message


def test_save_snapshot_not_overridden_allows_non_sealed_overrides() -> None:
    """fetch_current and fetch_and_snapshot overrides stay legal."""

    class _Allowed(CourtDirectory):
        def fetch_current(self) -> tuple[bytes, dict[str, str]]:  # pragma: no cover
            return b"", {}

        def fetch_and_snapshot(self, court_id: str | None = None) -> dict[str, str]:
            return super().fetch_and_snapshot(court_id or "ca_test")  # pragma: no cover

    assert "fetch_and_snapshot" in vars(_Allowed)


def test_every_court_fetch_and_snapshot_keeps_base_contract() -> None:
    """Each court's fetch_and_snapshot delegates to the base contract.

    Courts override fetch_and_snapshot only to default ``court_id``.  The base
    method skips saving an empty mapping (#4880) and otherwise calls
    save_snapshot once; an override that bypassed super() would lose both.
    """
    for cls in _court_directory_subclasses():
        for mapping, expected_calls in (({}, 0), ({"1": "Alice Smith"}, 1)):
            directory = cls.__new__(cls)
            directory.fetch_current = MagicMock(return_value=(b"<html/>", mapping))  # type: ignore[method-assign]
            directory.save_snapshot = MagicMock(return_value=True)  # type: ignore[method-assign]

            result = directory.fetch_and_snapshot("ca_contract_test")

            assert result == mapping, cls.__name__
            assert directory.save_snapshot.call_count == expected_calls, cls.__name__
            if expected_calls:
                directory.save_snapshot.assert_called_once_with(
                    b"<html/>", mapping, "ca_contract_test"
                )
