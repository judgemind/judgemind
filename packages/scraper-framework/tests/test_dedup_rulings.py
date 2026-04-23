"""Tests for the dedup_rulings script (issue #302).

All database interactions are mocked — these tests verify the SQL logic
and batch processing without requiring a live database.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

# Ensure the scripts directory is importable
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts"),
)

from dedup_rulings import _CURSOR_MIN_UUID  # noqa: E402


def _make_mock_conn() -> MagicMock:
    """Return a mock psycopg connection with cursor context manager."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cur


def test_count_duplicates_returns_count() -> None:
    """count_duplicates should return the count from the query."""
    from dedup_rulings import count_duplicates

    mock_conn, mock_cur = _make_mock_conn()
    mock_cur.fetchone.return_value = (42,)

    result = count_duplicates(mock_conn)

    assert result == 42
    mock_cur.execute.assert_called_once()
    sql = str(mock_cur.execute.call_args)
    assert "ROW_NUMBER" in sql
    assert "rn > 1" in sql


def test_count_duplicates_returns_zero_when_no_duplicates() -> None:
    """count_duplicates should return 0 when there are no duplicates."""
    from dedup_rulings import count_duplicates

    mock_conn, mock_cur = _make_mock_conn()
    mock_cur.fetchone.return_value = (0,)

    result = count_duplicates(mock_conn)

    assert result == 0


def test_dedup_batch_deletes_duplicates() -> None:
    """dedup_batch should delete duplicate rulings and orphaned documents."""
    from dedup_rulings import dedup_batch

    mock_conn = MagicMock()
    cursors: list[MagicMock] = []

    def make_cursor() -> MagicMock:
        cur = MagicMock()
        cursors.append(cur)
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    mock_conn.cursor.side_effect = make_cursor

    # First cursor: FIND_DUPLICATES_QUERY returns 2 duplicate rows
    # Second cursor: DELETE FROM rulings
    # Third cursor: DELETE FROM documents (orphans)

    # We need to set up the cursors' return values after they're created
    # Use a counter to track which cursor call we're on
    call_count = [0]
    original_side_effect = mock_conn.cursor.side_effect

    def cursor_factory() -> MagicMock:
        ctx = original_side_effect()
        cur = cursors[-1]
        idx = call_count[0]
        call_count[0] += 1

        if idx == 0:
            # FIND_DUPLICATES_QUERY
            cur.fetchall.return_value = [
                ("ruling-uuid-2", "doc-uuid-2"),
                ("ruling-uuid-3", "doc-uuid-3"),
            ]
        elif idx == 1:
            # DELETE FROM rulings
            cur.rowcount = 2
        elif idx == 2:
            # DELETE FROM documents (orphans)
            cur.rowcount = 2
        return ctx

    mock_conn.cursor.side_effect = cursor_factory

    stats, next_cursor = dedup_batch(mock_conn, batch_size=100, cursor=_CURSOR_MIN_UUID)

    assert stats["rulings_deleted"] == 2
    assert stats["documents_deleted"] == 2
    assert next_cursor == "ruling-uuid-3"


def test_dedup_batch_returns_zeros_when_no_duplicates() -> None:
    """dedup_batch should return zero stats when no duplicates found."""
    from dedup_rulings import dedup_batch

    mock_conn, mock_cur = _make_mock_conn()
    mock_cur.fetchall.return_value = []

    stats, next_cursor = dedup_batch(mock_conn, batch_size=100, cursor=_CURSOR_MIN_UUID)

    assert stats["rulings_deleted"] == 0
    assert stats["documents_deleted"] == 0
    assert next_cursor == _CURSOR_MIN_UUID


@patch("dedup_rulings.psycopg")
def test_run_dedup_dry_run_rolls_back(mock_psycopg: MagicMock) -> None:
    """In dry-run mode, run_dedup should rollback instead of commit."""
    from dedup_rulings import run_dedup

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_psycopg.connect.return_value = mock_conn

    cursors: list[MagicMock] = []
    call_idx = [0]

    def cursor_factory() -> MagicMock:
        cur = MagicMock()
        cursors.append(cur)
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur)
        ctx.__exit__ = MagicMock(return_value=False)

        idx = call_idx[0]
        call_idx[0] += 1

        if idx == 0:
            # count_duplicates
            cur.fetchone.return_value = (3,)
        elif idx == 1:
            # FIND_DUPLICATES_QUERY
            cur.fetchall.return_value = [
                ("r1", "d1"),
                ("r2", "d2"),
                ("r3", "d3"),
            ]
        elif idx == 2:
            # DELETE FROM rulings
            cur.rowcount = 3
        elif idx == 3:
            # DELETE FROM documents
            cur.rowcount = 3
        elif idx == 4:
            # Next batch: no more duplicates past cursor "r3"
            cur.fetchall.return_value = []
        return ctx

    mock_conn.cursor.side_effect = cursor_factory

    stats = run_dedup("postgresql://localhost/test", dry_run=True)

    assert stats["total_rulings_deleted"] == 3
    assert stats["total_documents_deleted"] == 3
    # Should rollback, not commit
    mock_conn.rollback.assert_called()
    mock_conn.commit.assert_not_called()


@patch("dedup_rulings.psycopg")
def test_run_dedup_no_duplicates_exits_early(mock_psycopg: MagicMock) -> None:
    """When no duplicates exist, run_dedup should exit without deleting."""
    from dedup_rulings import run_dedup

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_psycopg.connect.return_value = mock_conn

    mock_cur = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=mock_cur)
    ctx.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = ctx

    # count_duplicates returns 0
    mock_cur.fetchone.return_value = (0,)

    stats = run_dedup("postgresql://localhost/test")

    assert stats["total_rulings_deleted"] == 0
    assert stats["total_documents_deleted"] == 0
    mock_conn.commit.assert_not_called()


@patch("dedup_rulings.psycopg")
def test_run_dedup_commits_each_batch(mock_psycopg: MagicMock) -> None:
    """In non-dry-run mode, each batch should be committed."""
    from dedup_rulings import run_dedup

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_psycopg.connect.return_value = mock_conn

    call_idx = [0]

    def cursor_factory() -> MagicMock:
        cur = MagicMock()
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur)
        ctx.__exit__ = MagicMock(return_value=False)

        idx = call_idx[0]
        call_idx[0] += 1

        if idx == 0:
            # count_duplicates
            cur.fetchone.return_value = (2,)
        elif idx == 1:
            # First batch: FIND_DUPLICATES_QUERY
            cur.fetchall.return_value = [("r1", "d1"), ("r2", "d2")]
        elif idx == 2:
            # DELETE FROM rulings
            cur.rowcount = 2
        elif idx == 3:
            # DELETE FROM documents
            cur.rowcount = 1
        elif idx == 4:
            # Second batch: FIND_DUPLICATES_QUERY (no more duplicates)
            cur.fetchall.return_value = []
        return ctx

    mock_conn.cursor.side_effect = cursor_factory

    stats = run_dedup("postgresql://localhost/test", batch_size=10)

    assert stats["total_rulings_deleted"] == 2
    assert stats["total_documents_deleted"] == 1
    mock_conn.commit.assert_called()


def test_query_uses_keyset_not_offset() -> None:
    """The FIND_DUPLICATES_QUERY must use keyset pagination, not OFFSET."""
    from dedup_rulings import FIND_DUPLICATES_QUERY

    assert "OFFSET" not in FIND_DUPLICATES_QUERY
    assert "id > %s::uuid" in FIND_DUPLICATES_QUERY


# ---------------------------------------------------------------------------
# Hash backfill tests
# ---------------------------------------------------------------------------


def test_count_null_hashes() -> None:
    """count_null_hashes should return the count from the query."""
    from dedup_rulings import count_null_hashes

    mock_conn, mock_cur = _make_mock_conn()
    mock_cur.fetchone.return_value = (150,)

    result = count_null_hashes(mock_conn)

    assert result == 150
    mock_cur.execute.assert_called_once()
    sql = str(mock_cur.execute.call_args)
    assert "ruling_text_hash IS NULL" in sql


def test_backfill_hash_batch_updates_rows() -> None:
    """backfill_hash_batch should compute and update ruling_text_hash."""
    from dedup_rulings import backfill_hash_batch

    mock_conn = MagicMock()
    cursors: list[MagicMock] = []
    call_idx = [0]

    def cursor_factory() -> MagicMock:
        cur = MagicMock()
        cursors.append(cur)
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur)
        ctx.__exit__ = MagicMock(return_value=False)

        idx = call_idx[0]
        call_idx[0] += 1

        if idx == 0:
            # FETCH_NULL_HASH_BATCH_QUERY
            cur.fetchall.return_value = [
                ("ruling-1", "Motion GRANTED"),
                ("ruling-2", "Motion DENIED"),
            ]
        return ctx

    mock_conn.cursor.side_effect = cursor_factory

    count, next_cursor, dupes = backfill_hash_batch(mock_conn, batch_size=500)

    assert count == 2
    assert dupes == 0
    assert next_cursor == "ruling-2"
    # Individual UPDATEs use separate cursors (with savepoints)
    # Cursor 0: fetch batch; then per-row cursors for savepoint + update
    assert len(cursors) >= 2


def test_backfill_hash_batch_returns_zero_when_empty() -> None:
    """backfill_hash_batch should return 0 when no rows need backfill."""
    from dedup_rulings import backfill_hash_batch

    mock_conn, mock_cur = _make_mock_conn()
    mock_cur.fetchall.return_value = []

    count, next_cursor, dupes = backfill_hash_batch(mock_conn)

    assert count == 0
    assert dupes == 0
    assert next_cursor == _CURSOR_MIN_UUID


def test_backfill_hash_batch_handles_unique_violation() -> None:
    """When a UniqueViolation occurs, the duplicate ruling is deleted."""
    import psycopg.errors
    from dedup_rulings import backfill_hash_batch

    mock_conn = MagicMock()
    cursors: list[MagicMock] = []
    call_idx = [0]
    raise_on_update = [False, True]  # second row triggers violation

    def cursor_factory() -> MagicMock:
        cur = MagicMock()
        cursors.append(cur)
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur)
        ctx.__exit__ = MagicMock(return_value=False)

        idx = call_idx[0]
        call_idx[0] += 1

        if idx == 0:
            # FETCH_NULL_HASH_BATCH_QUERY
            cur.fetchall.return_value = [
                ("ruling-1", "Same text here"),
                ("ruling-2", "Same text here"),
            ]
        return ctx

    # Track execute calls to raise on the right UPDATE
    update_count = [0]
    original_cursor_factory = cursor_factory

    def cursor_with_violation() -> MagicMock:
        ctx = original_cursor_factory()
        cur = cursors[-1]

        original_execute = cur.execute

        def smart_execute(sql: str, params: object = None) -> None:
            if "UPDATE rulings SET ruling_text_hash" in str(sql):
                idx = update_count[0]
                update_count[0] += 1
                if idx < len(raise_on_update) and raise_on_update[idx]:
                    raise psycopg.errors.UniqueViolation(
                        "duplicate key value violates unique constraint"
                    )
            return original_execute(sql, params)

        cur.execute = MagicMock(side_effect=smart_execute)
        # For the violation handler, need fetchone to return doc_id
        cur.fetchone.return_value = ("doc-uuid-2",)
        return ctx

    mock_conn.cursor.side_effect = cursor_with_violation

    count, next_cursor, dupes = backfill_hash_batch(mock_conn, batch_size=500)

    assert count == 1  # first row updated
    assert dupes == 1  # second row deleted as duplicate
    assert next_cursor == "ruling-2"


@patch("dedup_rulings.psycopg")
def test_run_backfill_hashes_dry_run(mock_psycopg: MagicMock) -> None:
    """Dry-run mode should rollback instead of commit."""
    from dedup_rulings import run_backfill_hashes

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_psycopg.connect.return_value = mock_conn

    call_idx = [0]

    def cursor_factory() -> MagicMock:
        cur = MagicMock()
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur)
        ctx.__exit__ = MagicMock(return_value=False)

        idx = call_idx[0]
        call_idx[0] += 1

        if idx == 0:
            # count_null_hashes
            cur.fetchone.return_value = (2,)
        elif idx == 1:
            # First batch: FETCH_NULL_HASH_BATCH_QUERY
            cur.fetchall.return_value = [
                ("r1", "Ruling text one"),
                ("r2", "Ruling text two"),
            ]
        else:
            # Subsequent batches: no more rows
            cur.fetchall.return_value = []
        return ctx

    mock_conn.cursor.side_effect = cursor_factory

    totals = run_backfill_hashes("postgresql://localhost/test", dry_run=True)

    assert totals["total_updated"] == 2
    assert totals["total_dupes_deleted"] == 0
    mock_conn.rollback.assert_called()
    mock_conn.commit.assert_not_called()


def test_backfill_query_uses_keyset_not_offset() -> None:
    """The FETCH_NULL_HASH_BATCH_QUERY must use keyset pagination, not OFFSET."""
    from dedup_rulings import FETCH_NULL_HASH_BATCH_QUERY

    assert "OFFSET" not in FETCH_NULL_HASH_BATCH_QUERY
    assert "id > %s::uuid" in FETCH_NULL_HASH_BATCH_QUERY
