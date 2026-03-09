"""Tests for the cleanup_org_judges script.

All database access is mocked -- these tests verify the org-name detection,
batch processing, and the run_cleanup flow without requiring a live database.
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
)
sys.path.insert(0, _SCRIPTS_DIR)
cleanup = importlib.import_module("cleanup_org_judges")


# ---------------------------------------------------------------------------
# looks_like_org_name tests
# ---------------------------------------------------------------------------


class TestLooksLikeOrgName:
    """Tests for the looks_like_org_name() heuristic."""

    @pytest.mark.parametrize(
        "name",
        [
            "Heritage Medical Group",
            "Bank of America, N.A.",
            "ABC Corporation",
            "Smith & Associates",
            "XYZ Holdings LLC",
            "County of Los Angeles",
            "City of San Francisco",
            "State of California",
            "People of the State of California",
            "Department of Motor Vehicles",
            "First National Bank",
            "St. Joseph Hospital",
            "Kaiser Foundation",
            "University of California",
            "Los Angeles Unified School District",
            "Metropolitan Water District",
            "Pacific Gas Company",
            "Acme Industries",
            "Global Financial Services",
            "Premier Healthcare Solutions",
            "Legacy Trust",
            "Jones D/B/A Jones Plumbing",
            "Smith DBA Smith Auto",
        ],
    )
    def test_detects_org_names(self, name: str) -> None:
        assert cleanup.looks_like_org_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "James I. Montgomery",
            "Bobby P. Luna",
            "William A. Crowfoot",
            "John Smith",
            "Maria Garcia",
            "Robert Chen-Williams",
        ],
    )
    def test_accepts_person_names(self, name: str) -> None:
        assert cleanup.looks_like_org_name(name) is False

    def test_detects_case_title_with_vs(self) -> None:
        assert cleanup.looks_like_org_name("Smith vs Jones") is True

    def test_detects_case_title_with_v_dot(self) -> None:
        assert cleanup.looks_like_org_name("Smith v. Jones") is True

    def test_detects_california_corporation(self) -> None:
        assert cleanup.looks_like_org_name("Acme, a California Limited Partnership") is True

    def test_detects_too_long_name(self) -> None:
        long_name = "A" * 61
        assert cleanup.looks_like_org_name(long_name) is True

    def test_rejects_empty_name(self) -> None:
        assert cleanup.looks_like_org_name("") is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JUDGE_ID = str(uuid.uuid4())
_DEFAULT_CURSOR = cleanup._CURSOR_MIN_UUID


def _make_judge_row(
    canonical_name: str,
    judge_id: str | None = None,
) -> tuple:
    """Return a tuple matching the FETCH_JUDGES_QUERY columns."""
    return (
        judge_id if judge_id is not None else _JUDGE_ID,
        canonical_name,
    )


def _make_mock_conn_with_judges(
    rows: list[tuple],
    ruling_count: int = 2,
    case_judge_count: int = 1,
) -> MagicMock:
    """Create a mock connection that returns the given judge rows.

    For org judges, also mocks the COUNT queries and the cleanup queries.
    """
    conn = MagicMock()
    call_index = [0]

    def cursor_ctx() -> MagicMock:
        ctx = MagicMock()
        cur = MagicMock()
        idx = call_index[0]
        call_index[0] += 1

        if idx == 0:
            # First call: FETCH_JUDGES_QUERY
            cur.fetchall.return_value = rows
        elif idx == 1:
            # COUNT_RULINGS_QUERY
            cur.fetchone.return_value = (ruling_count,)
        elif idx == 2:
            # COUNT_CASE_JUDGES_QUERY
            cur.fetchone.return_value = (case_judge_count,)
        # Remaining calls are the mutation queries (nullify, delete)

        ctx.__enter__ = MagicMock(return_value=cur)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    conn.cursor.side_effect = cursor_ctx
    return conn


# ---------------------------------------------------------------------------
# cleanup_batch tests
# ---------------------------------------------------------------------------


class TestCleanupBatch:
    """Tests for cleanup_batch()."""

    def test_no_rows_returns_zero(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []

        processed, cleaned, rulings, case_judges, next_cursor = cleanup.cleanup_batch(
            conn, batch_size=10, cursor=_DEFAULT_CURSOR
        )
        assert processed == 0
        assert cleaned == 0
        assert rulings == 0
        assert case_judges == 0
        assert next_cursor == _DEFAULT_CURSOR

    def test_cleans_org_judge(self) -> None:
        """Judge with org name is cleaned up."""
        judge_id = str(uuid.uuid4())
        rows = [_make_judge_row("Heritage Medical Group", judge_id=judge_id)]
        conn = _make_mock_conn_with_judges(rows, ruling_count=3, case_judge_count=2)

        processed, cleaned, rulings, case_judges, _cursor = cleanup.cleanup_batch(
            conn, batch_size=10, cursor=_DEFAULT_CURSOR
        )

        assert processed == 1
        assert cleaned == 1
        assert rulings == 3
        assert case_judges == 2

    def test_skips_real_judge(self) -> None:
        """Judge with real person name is not cleaned up."""
        judge_id = str(uuid.uuid4())
        rows = [_make_judge_row("James I. Montgomery", judge_id=judge_id)]

        conn = MagicMock()
        cur_fetch = MagicMock()
        cur_fetch.fetchall.return_value = rows

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur_fetch)
        ctx.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = ctx

        processed, cleaned, rulings, case_judges, _cursor = cleanup.cleanup_batch(
            conn, batch_size=10, cursor=_DEFAULT_CURSOR
        )

        assert processed == 1
        assert cleaned == 0
        assert rulings == 0
        assert case_judges == 0

    def test_mixed_batch(self) -> None:
        """Batch with both real and org judges cleans only org judges."""
        real_id = str(uuid.uuid4())
        org_id = str(uuid.uuid4())
        # Ensure org_id sorts after real_id for stable cursor testing
        rows = [
            _make_judge_row("James I. Montgomery", judge_id=real_id),
            _make_judge_row("Bank of America, N.A.", judge_id=org_id),
        ]

        conn = _make_mock_conn_with_judges(rows, ruling_count=1, case_judge_count=1)

        # Override the first cursor call to return both rows
        call_index = [0]

        def cursor_ctx() -> MagicMock:
            ctx = MagicMock()
            cur = MagicMock()
            idx = call_index[0]
            call_index[0] += 1

            if idx == 0:
                cur.fetchall.return_value = rows
            elif idx == 1:
                cur.fetchone.return_value = (1,)
            elif idx == 2:
                cur.fetchone.return_value = (1,)

            ctx.__enter__ = MagicMock(return_value=cur)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        conn.cursor.side_effect = cursor_ctx

        processed, cleaned, rulings, case_judges, _cursor = cleanup.cleanup_batch(
            conn, batch_size=10, cursor=_DEFAULT_CURSOR
        )

        assert processed == 2
        assert cleaned == 1
        assert rulings == 1
        assert case_judges == 1

    def test_cursor_advances(self) -> None:
        """Next cursor is set to the last judge_id in the batch."""
        judge_id_1 = str(uuid.uuid4())
        judge_id_2 = str(uuid.uuid4())
        rows = [
            _make_judge_row("John Smith", judge_id=judge_id_1),
            _make_judge_row("Jane Doe", judge_id=judge_id_2),
        ]

        conn = MagicMock()
        cur_fetch = MagicMock()
        cur_fetch.fetchall.return_value = rows

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur_fetch)
        ctx.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = ctx

        _, _, _, _, next_cursor = cleanup.cleanup_batch(conn, batch_size=10, cursor=_DEFAULT_CURSOR)

        assert next_cursor == judge_id_2


# ---------------------------------------------------------------------------
# run_cleanup tests
# ---------------------------------------------------------------------------


class TestRunCleanup:
    """Tests for run_cleanup() end-to-end flow."""

    @patch("cleanup_org_judges.psycopg")
    @patch("cleanup_org_judges.cleanup_batch")
    def test_dry_run_rolls_back_per_batch(
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
        mock_batch.side_effect = [
            (50, 5, 10, 5, cursor_1),
            (10, 2, 4, 2, cursor_2),
        ]

        stats = cleanup.run_cleanup("postgresql://test", batch_size=50, dry_run=True)

        assert mock_conn.rollback.call_count == 2
        mock_conn.commit.assert_not_called()
        assert stats["total_processed"] == 60
        assert stats["total_judges_cleaned"] == 7
        assert stats["total_rulings_nullified"] == 14
        assert stats["total_case_judges_removed"] == 7

    @patch("cleanup_org_judges.psycopg")
    @patch("cleanup_org_judges.cleanup_batch")
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
        mock_batch.side_effect = [
            (50, 3, 6, 3, cursor_1),
            (20, 1, 2, 1, cursor_2),
        ]

        stats = cleanup.run_cleanup("postgresql://test", batch_size=50)

        assert mock_conn.commit.call_count == 2
        mock_conn.rollback.assert_not_called()
        assert stats["total_processed"] == 70
        assert stats["total_judges_cleaned"] == 4

    @patch("cleanup_org_judges.psycopg")
    @patch("cleanup_org_judges.cleanup_batch")
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
        mock_batch.side_effect = [(25, 2, 4, 2, cursor_1)]

        stats = cleanup.run_cleanup("postgresql://test", batch_size=100, limit=25)

        call_args = mock_batch.call_args_list[0]
        assert call_args[0][1] == 25  # effective_batch = min(100, 25)
        assert stats["total_processed"] == 25

    def test_query_uses_keyset_not_offset(self) -> None:
        """The query must use keyset pagination, not OFFSET."""
        assert "OFFSET" not in cleanup.FETCH_JUDGES_QUERY
        assert "j.id > %s::uuid" in cleanup.FETCH_JUDGES_QUERY
