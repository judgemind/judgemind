"""Tests for the backfill_la_judge_from_dept script.

All database access is mocked — these tests verify the lookup, update,
pagination, and historical snapshot logic without requiring a live database.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import uuid
from datetime import date, datetime
from unittest.mock import MagicMock, call, patch

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
)
sys.path.insert(0, _SCRIPTS_DIR)
backfill = importlib.import_module("backfill_la_judge_from_dept")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COURT_ID = str(uuid.uuid4())
_CASE_ID = str(uuid.uuid4())
_HEARING_DATE = date(2026, 3, 5)
_DEPT_MAP = {"52": "Jerrold Abeles", "3": "William A. Crowfoot", "F46": "Kerry Duffy-Lewis"}


def _make_ruling_row(
    department: str,
    *,
    court_id: str | None = None,
    case_id: str | None = None,
    hearing_date: date | None = None,
) -> tuple:
    """Return a tuple matching the FETCH_QUERY columns."""
    return (
        str(uuid.uuid4()),  # r.id (ruling_id)
        department,  # r.department
        court_id or _COURT_ID,
        case_id or _CASE_ID,
        hearing_date or _HEARING_DATE,
    )


def _mock_conn_with_rows(rows: list[tuple]) -> MagicMock:
    """Create a mock connection where the first cursor returns rows."""
    conn = MagicMock()
    cur_fetch = MagicMock()
    cur_fetch.fetchall.return_value = rows

    # We need multiple cursor contexts: one for fetch, rest for updates
    call_count = 0

    def cursor_ctx() -> MagicMock:
        nonlocal call_count
        ctx = MagicMock()
        if call_count == 0:
            ctx.__enter__ = MagicMock(return_value=cur_fetch)
        else:
            cur_update = MagicMock()
            ctx.__enter__ = MagicMock(return_value=cur_update)
        ctx.__exit__ = MagicMock(return_value=False)
        call_count += 1
        return ctx

    conn.cursor.side_effect = cursor_ctx
    return conn


# ---------------------------------------------------------------------------
# backfill_batch tests
# ---------------------------------------------------------------------------


class TestBackfillBatch:
    """Tests for backfill_batch()."""

    @patch("backfill_la_judge_from_dept.get_snapshot_mapping", return_value=None)
    def test_no_rows_returns_zero(self, _mock_snapshot: MagicMock) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []

        processed, updated, _cursor = backfill.backfill_batch(
            conn,
            _DEPT_MAP,
            batch_size=10,
            cursor=backfill._CURSOR_MIN_UUID,
        )
        assert processed == 0
        assert updated == 0

    @patch("backfill_la_judge_from_dept.get_snapshot_mapping", return_value=None)
    @patch("backfill_la_judge_from_dept.upsert_case_judge")
    @patch("backfill_la_judge_from_dept.resolve_judge")
    def test_updates_ruling_with_mapped_judge(
        self, mock_resolve: MagicMock, mock_upsert_cj: MagicMock, _mock_snapshot: MagicMock
    ) -> None:
        """Ruling with department in mapping gets judge_id set."""
        row = _make_ruling_row("52")
        conn = _mock_conn_with_rows([row])

        mock_resolve.return_value = "judge-uuid-abeles"

        processed, updated, _cursor = backfill.backfill_batch(
            conn,
            _DEPT_MAP,
            batch_size=10,
            cursor=backfill._CURSOR_MIN_UUID,
        )

        assert processed == 1
        assert updated == 1

        # resolve_judge should be called with the mapped name
        mock_resolve.assert_called_once_with(conn, "Jerrold Abeles", _COURT_ID)

        # case_judges link should be created
        mock_upsert_cj.assert_called_once_with(conn, _CASE_ID, "judge-uuid-abeles", _HEARING_DATE)

    @patch("backfill_la_judge_from_dept.get_snapshot_mapping", return_value=None)
    @patch("backfill_la_judge_from_dept.upsert_case_judge")
    @patch("backfill_la_judge_from_dept.resolve_judge")
    def test_skips_unmapped_department(
        self, mock_resolve: MagicMock, mock_upsert_cj: MagicMock, _mock_snapshot: MagicMock
    ) -> None:
        """Ruling with department NOT in mapping is skipped."""
        row = _make_ruling_row("999")
        conn = _mock_conn_with_rows([row])

        processed, updated, _cursor = backfill.backfill_batch(
            conn,
            _DEPT_MAP,
            batch_size=10,
            cursor=backfill._CURSOR_MIN_UUID,
        )

        assert processed == 1
        assert updated == 0
        mock_resolve.assert_not_called()
        mock_upsert_cj.assert_not_called()

    @patch("backfill_la_judge_from_dept.get_snapshot_mapping", return_value=None)
    @patch("backfill_la_judge_from_dept.upsert_case_judge")
    @patch("backfill_la_judge_from_dept.resolve_judge")
    def test_multiple_rulings_in_batch(
        self, mock_resolve: MagicMock, mock_upsert_cj: MagicMock, _mock_snapshot: MagicMock
    ) -> None:
        """Multiple rulings are processed in a single batch."""
        row1 = _make_ruling_row("52")
        row2 = _make_ruling_row("F46")
        row3 = _make_ruling_row("999")  # unmapped

        conn = _mock_conn_with_rows([row1, row2, row3])
        mock_resolve.side_effect = ["judge-abeles", "judge-duffy-lewis"]

        processed, updated, _cursor = backfill.backfill_batch(
            conn,
            _DEPT_MAP,
            batch_size=10,
            cursor=backfill._CURSOR_MIN_UUID,
        )

        assert processed == 3
        assert updated == 2
        assert mock_resolve.call_count == 2

    @patch("backfill_la_judge_from_dept.get_snapshot_mapping", return_value=None)
    def test_cursor_advances(self, _mock_snapshot: MagicMock) -> None:
        """The cursor should advance to the last ruling_id processed."""
        ruling_id = str(uuid.uuid4())
        row = (ruling_id, "52", _COURT_ID, _CASE_ID, _HEARING_DATE)

        conn = _mock_conn_with_rows([row])
        with patch("backfill_la_judge_from_dept.resolve_judge", return_value="j-id"):
            with patch("backfill_la_judge_from_dept.upsert_case_judge"):
                _, _, cursor = backfill.backfill_batch(
                    conn,
                    _DEPT_MAP,
                    batch_size=10,
                    cursor=backfill._CURSOR_MIN_UUID,
                )

        assert cursor == ruling_id


# ---------------------------------------------------------------------------
# Historical snapshot lookup tests
# ---------------------------------------------------------------------------


class TestGetSnapshotMapping:
    """Tests for get_snapshot_mapping()."""

    def test_returns_mapping_when_found(self) -> None:
        """Should return parsed mapping dict when a snapshot exists."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cursor.fetchone.return_value = ({"52": "Old Judge Smith"},)

        result = backfill.get_snapshot_mapping(conn, "ca_los_angeles", datetime(2025, 6, 15))

        assert result == {"52": "Old Judge Smith"}

    def test_returns_none_when_no_snapshot(self) -> None:
        """Should return None when no snapshot exists."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cursor.fetchone.return_value = None

        result = backfill.get_snapshot_mapping(conn, "ca_los_angeles", datetime(2025, 6, 15))

        assert result is None

    def test_parses_json_string_mapping(self) -> None:
        """If DB returns mapping as JSON string, it should be parsed."""
        mapping = {"52": "Judge X"}
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cursor.fetchone.return_value = (json.dumps(mapping),)

        result = backfill.get_snapshot_mapping(conn, "ca_los_angeles", datetime(2025, 6, 15))

        assert result == mapping

    def test_query_parameters(self) -> None:
        """Should pass correct court_id and as_of to the query."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cursor.fetchone.return_value = None

        as_of = datetime(2025, 6, 15, 12, 0, 0)
        backfill.get_snapshot_mapping(conn, "ca_los_angeles", as_of)

        cursor.execute.assert_called_once()
        sql, params = cursor.execute.call_args.args
        assert "captured_at <=" in sql
        assert params == ("ca_los_angeles", as_of)


class TestHearingDateToDatetime:
    """Tests for _hearing_date_to_datetime()."""

    def test_date_converted_to_end_of_day(self) -> None:
        """A plain date should become end-of-day datetime."""
        d = date(2025, 6, 15)
        result = backfill._hearing_date_to_datetime(d)
        assert isinstance(result, datetime)
        assert result == datetime(2025, 6, 15, 23, 59, 59)

    def test_datetime_returned_as_is(self) -> None:
        """A datetime should be returned unchanged."""
        dt = datetime(2025, 6, 15, 14, 30, 0)
        result = backfill._hearing_date_to_datetime(dt)
        assert result is dt


class TestGetEffectiveMapping:
    """Tests for _get_effective_mapping()."""

    def test_uses_snapshot_when_available(self) -> None:
        """Should use the historical snapshot mapping when one exists."""
        snapshot_map = {"52": "Old Judge", "3": "Another Old Judge"}

        with patch(
            "backfill_la_judge_from_dept.get_snapshot_mapping",
            return_value=snapshot_map,
        ) as mock_snapshot:
            cache: dict[date, dict[str, str] | None] = {}
            result = backfill._get_effective_mapping(MagicMock(), _HEARING_DATE, _DEPT_MAP, cache)

        assert result == snapshot_map
        mock_snapshot.assert_called_once()

    def test_falls_back_to_live_when_no_snapshot(self) -> None:
        """Should fall back to live mapping when no snapshot exists."""
        with patch(
            "backfill_la_judge_from_dept.get_snapshot_mapping",
            return_value=None,
        ):
            cache: dict[date, dict[str, str] | None] = {}
            result = backfill._get_effective_mapping(MagicMock(), _HEARING_DATE, _DEPT_MAP, cache)

        assert result is _DEPT_MAP

    def test_caches_result_per_date(self) -> None:
        """Same date should reuse cached result, not query again."""
        snapshot_map = {"52": "Cached Judge"}

        with patch(
            "backfill_la_judge_from_dept.get_snapshot_mapping",
            return_value=snapshot_map,
        ) as mock_snapshot:
            cache: dict[date, dict[str, str] | None] = {}
            conn = MagicMock()

            # First call — should query
            backfill._get_effective_mapping(conn, _HEARING_DATE, _DEPT_MAP, cache)
            # Second call same date — should use cache
            backfill._get_effective_mapping(conn, _HEARING_DATE, _DEPT_MAP, cache)

        # Only one DB query despite two calls
        mock_snapshot.assert_called_once()

    def test_different_dates_query_separately(self) -> None:
        """Different hearing dates should each get their own snapshot query."""
        date1 = date(2025, 1, 15)
        date2 = date(2025, 6, 15)
        snapshot1 = {"52": "Judge Jan"}
        snapshot2 = {"52": "Judge Jun"}

        with patch(
            "backfill_la_judge_from_dept.get_snapshot_mapping",
            side_effect=[snapshot1, snapshot2],
        ) as mock_snapshot:
            cache: dict[date, dict[str, str] | None] = {}
            conn = MagicMock()

            result1 = backfill._get_effective_mapping(conn, date1, _DEPT_MAP, cache)
            result2 = backfill._get_effective_mapping(conn, date2, _DEPT_MAP, cache)

        assert result1 == snapshot1
        assert result2 == snapshot2
        assert mock_snapshot.call_count == 2

    def test_caches_none_result(self) -> None:
        """When no snapshot exists for a date, None should be cached too."""
        with patch(
            "backfill_la_judge_from_dept.get_snapshot_mapping",
            return_value=None,
        ) as mock_snapshot:
            cache: dict[date, dict[str, str] | None] = {}
            conn = MagicMock()

            # Call twice with same date
            backfill._get_effective_mapping(conn, _HEARING_DATE, _DEPT_MAP, cache)
            backfill._get_effective_mapping(conn, _HEARING_DATE, _DEPT_MAP, cache)

        # Should only query once, second call uses cached None
        mock_snapshot.assert_called_once()


class TestHistoricalBackfillBatch:
    """Tests for backfill_batch() with historical snapshot behavior."""

    @patch("backfill_la_judge_from_dept.upsert_case_judge")
    @patch("backfill_la_judge_from_dept.resolve_judge")
    @patch("backfill_la_judge_from_dept.get_snapshot_mapping")
    def test_uses_historical_snapshot_for_ruling(
        self,
        mock_snapshot: MagicMock,
        mock_resolve: MagicMock,
        mock_upsert_cj: MagicMock,
    ) -> None:
        """When a historical snapshot exists, uses the snapshot judge name."""
        # Snapshot has a different judge for dept 52 than the live map
        mock_snapshot.return_value = {"52": "Historical Judge"}

        row = _make_ruling_row("52")
        conn = _mock_conn_with_rows([row])
        mock_resolve.return_value = "judge-historical"

        backfill.backfill_batch(
            conn,
            _DEPT_MAP,
            batch_size=10,
            cursor=backfill._CURSOR_MIN_UUID,
        )

        # Should resolve with the historical judge, not the live one
        mock_resolve.assert_called_once_with(conn, "Historical Judge", _COURT_ID)

    @patch("backfill_la_judge_from_dept.upsert_case_judge")
    @patch("backfill_la_judge_from_dept.resolve_judge")
    @patch("backfill_la_judge_from_dept.get_snapshot_mapping")
    def test_falls_back_to_live_when_no_snapshot(
        self,
        mock_snapshot: MagicMock,
        mock_resolve: MagicMock,
        mock_upsert_cj: MagicMock,
    ) -> None:
        """When no snapshot exists, falls back to live mapping."""
        mock_snapshot.return_value = None

        row = _make_ruling_row("52")
        conn = _mock_conn_with_rows([row])
        mock_resolve.return_value = "judge-live"

        backfill.backfill_batch(
            conn,
            _DEPT_MAP,
            batch_size=10,
            cursor=backfill._CURSOR_MIN_UUID,
        )

        # Should resolve with the live judge name
        mock_resolve.assert_called_once_with(conn, "Jerrold Abeles", _COURT_ID)

    @patch("backfill_la_judge_from_dept.upsert_case_judge")
    @patch("backfill_la_judge_from_dept.resolve_judge")
    @patch("backfill_la_judge_from_dept.get_snapshot_mapping")
    def test_different_dates_get_different_snapshots(
        self,
        mock_snapshot: MagicMock,
        mock_resolve: MagicMock,
        mock_upsert_cj: MagicMock,
    ) -> None:
        """Rulings with different hearing dates look up different snapshots."""
        date_jan = date(2025, 1, 15)
        date_jun = date(2025, 6, 15)

        # Different judges for same dept at different times
        snapshot_jan = {"52": "Judge January"}
        snapshot_jun = {"52": "Judge June"}

        mock_snapshot.side_effect = [snapshot_jan, snapshot_jun]

        row1 = _make_ruling_row("52", hearing_date=date_jan)
        row2 = _make_ruling_row("52", hearing_date=date_jun)

        conn = _mock_conn_with_rows([row1, row2])
        mock_resolve.side_effect = ["judge-jan-id", "judge-jun-id"]

        backfill.backfill_batch(
            conn,
            _DEPT_MAP,
            batch_size=10,
            cursor=backfill._CURSOR_MIN_UUID,
        )

        # Two different snapshot queries (one per unique date)
        assert mock_snapshot.call_count == 2

        # Two different judge names resolved
        resolve_calls = mock_resolve.call_args_list
        assert resolve_calls[0] == call(conn, "Judge January", _COURT_ID)
        assert resolve_calls[1] == call(conn, "Judge June", _COURT_ID)

    @patch("backfill_la_judge_from_dept.upsert_case_judge")
    @patch("backfill_la_judge_from_dept.resolve_judge")
    @patch("backfill_la_judge_from_dept.get_snapshot_mapping")
    def test_snapshot_cache_reused_across_same_dates(
        self,
        mock_snapshot: MagicMock,
        mock_resolve: MagicMock,
        mock_upsert_cj: MagicMock,
    ) -> None:
        """Rulings on the same date should share a cached snapshot lookup."""
        snapshot = {"52": "Snapshot Judge", "3": "Another Judge"}
        mock_snapshot.return_value = snapshot

        # Two rulings on the same date
        row1 = _make_ruling_row("52")
        row2 = _make_ruling_row("3")

        conn = _mock_conn_with_rows([row1, row2])
        mock_resolve.side_effect = ["j1", "j2"]

        backfill.backfill_batch(
            conn,
            _DEPT_MAP,
            batch_size=10,
            cursor=backfill._CURSOR_MIN_UUID,
        )

        # Only one snapshot query despite two rulings (same date -> cached)
        mock_snapshot.assert_called_once()

    @patch("backfill_la_judge_from_dept.upsert_case_judge")
    @patch("backfill_la_judge_from_dept.resolve_judge")
    @patch("backfill_la_judge_from_dept.get_snapshot_mapping")
    def test_snapshot_cache_shared_across_batches(
        self,
        mock_snapshot: MagicMock,
        mock_resolve: MagicMock,
        mock_upsert_cj: MagicMock,
    ) -> None:
        """When a shared snapshot_cache is passed, it avoids repeat queries."""
        snapshot = {"52": "Cached Judge"}
        mock_snapshot.return_value = snapshot

        row = _make_ruling_row("52")
        conn = _mock_conn_with_rows([row])
        mock_resolve.return_value = "j1"

        # Pre-populated cache
        cache: dict[date, dict[str, str] | None] = {_HEARING_DATE: snapshot}

        backfill.backfill_batch(
            conn,
            _DEPT_MAP,
            batch_size=10,
            cursor=backfill._CURSOR_MIN_UUID,
            snapshot_cache=cache,
        )

        # No new snapshot query — cache already had the date
        mock_snapshot.assert_not_called()
        mock_resolve.assert_called_once_with(conn, "Cached Judge", _COURT_ID)


# ---------------------------------------------------------------------------
# run_backfill tests
# ---------------------------------------------------------------------------


class TestRunBackfill:
    """Tests for run_backfill() end-to-end flow."""

    @patch("backfill_la_judge_from_dept.psycopg")
    @patch("backfill_la_judge_from_dept.backfill_batch")
    def test_dry_run_rolls_back(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = str(uuid.uuid4())
        # One partial batch (signals end)
        mock_batch.side_effect = [(5, 3, cursor_1)]

        stats = backfill.run_backfill(
            "postgresql://test",
            dept_map=_DEPT_MAP,
            batch_size=100,
            dry_run=True,
        )

        mock_conn.rollback.assert_called_once()
        mock_conn.commit.assert_not_called()
        assert stats["total_processed"] == 5
        assert stats["total_updated"] == 3

    @patch("backfill_la_judge_from_dept.psycopg")
    @patch("backfill_la_judge_from_dept.backfill_batch")
    def test_commits_per_batch(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = str(uuid.uuid4())
        cursor_2 = str(uuid.uuid4())
        # Two batches: first full, second partial
        mock_batch.side_effect = [(100, 80, cursor_1), (30, 20, cursor_2)]

        stats = backfill.run_backfill(
            "postgresql://test",
            dept_map=_DEPT_MAP,
            batch_size=100,
        )

        assert mock_conn.commit.call_count == 2
        mock_conn.rollback.assert_not_called()
        assert stats["total_processed"] == 130
        assert stats["total_updated"] == 100

    @patch("backfill_la_judge_from_dept.psycopg")
    @patch("backfill_la_judge_from_dept.backfill_batch")
    def test_limit_respected(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = str(uuid.uuid4())
        mock_batch.side_effect = [(50, 40, cursor_1)]

        stats = backfill.run_backfill(
            "postgresql://test",
            dept_map=_DEPT_MAP,
            batch_size=100,
            limit=50,
        )

        # The effective batch size should have been capped to 50
        call_args = mock_batch.call_args_list[0]
        assert call_args[0][2] == 50  # effective_batch = min(100, 50)
        assert stats["total_processed"] == 50

    @patch("backfill_la_judge_from_dept.psycopg")
    @patch("backfill_la_judge_from_dept.backfill_batch")
    def test_passes_snapshot_cache_to_batches(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        """The snapshot_cache kwarg should be passed to each batch call."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = str(uuid.uuid4())
        mock_batch.side_effect = [(5, 3, cursor_1)]

        backfill.run_backfill(
            "postgresql://test",
            dept_map=_DEPT_MAP,
            batch_size=100,
        )

        # Verify snapshot_cache kwarg was passed
        call_kwargs = mock_batch.call_args_list[0].kwargs
        assert "snapshot_cache" in call_kwargs
        assert isinstance(call_kwargs["snapshot_cache"], dict)

    def test_query_uses_keyset_not_offset(self) -> None:
        """The query must use keyset pagination, not OFFSET."""
        assert "OFFSET" not in backfill.FETCH_QUERY
        assert "r.id > %s::uuid" in backfill.FETCH_QUERY
