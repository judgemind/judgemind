"""Tests for the backfill_previous_version_id script (#2653).

Tests cover the four AC4 scenarios:
  1. Matching case (one active winner sharing case_id) -> returns winner's id.
  2. No-winner case (zero candidates) -> returns None.
  3. Already-linked case (loser.previous_version_id IS NOT NULL) -> excluded
     by the SELECT WHERE clause (string-match assertion on the query constant).
  4. Ambiguous case (>=2 active candidates sharing case_id) -> returns None.

Plus a thin smoke test asserting the UPDATE SQL shape (previous_version_id,
change_type='duplicate_content', idempotence WHERE guard).

All database access is mocked -- these tests verify the pure helper logic,
SELECT filters, and UPDATE shape without requiring live services.

The script depends on psycopg, structlog, and framework (scraper-framework),
which are not available in the lightweight CI scripts-tests environment.
We mock them in sys.modules before importing the script under test.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Pre-import mocking -- the script imports psycopg, structlog, and
# framework.logging at module level, which are not installed in the CI
# scripts-tests environment. Save/replay envelope is centralised in
# ``scripts/tests/_mock_helpers.py`` (#4430).
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "archive"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests._mock_helpers import mock_sys_modules  # noqa: E402

_mock_structlog = MagicMock()
_mock_structlog.get_logger.return_value = MagicMock()

with mock_sys_modules(
    {
        "psycopg": MagicMock(),
        "structlog": _mock_structlog,
        "framework": MagicMock(),
        "framework.logging": MagicMock(),
    }
):
    import backfill_previous_version_id as backfill  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(fetchall_return: list) -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = fetchall_return
    return conn, cur


# ---------------------------------------------------------------------------
# AC4 Scenario 1: Matching case -- one active winner -> winner id returned
# ---------------------------------------------------------------------------


class TestPickWinnerForLoserMatchingCase:
    """AC4 Scenario 1: pick_winner_for_loser with exactly one candidate."""

    def test_single_candidate_returns_winner_id(self) -> None:
        candidates = [{"id": "winner-uuid-1"}]
        result = backfill.pick_winner_for_loser(candidates)
        assert result == "winner-uuid-1"


# ---------------------------------------------------------------------------
# AC4 Scenario 2: No-winner case -- zero candidates -> None
# ---------------------------------------------------------------------------


class TestPickWinnerForLoserNoWinner:
    """AC4 Scenario 2: pick_winner_for_loser with zero candidates."""

    def test_no_candidates_returns_none(self) -> None:
        result = backfill.pick_winner_for_loser([])
        assert result is None


# ---------------------------------------------------------------------------
# AC4 Scenario 3: Already-linked case -- excluded by SELECT WHERE clause
# ---------------------------------------------------------------------------


class TestAlreadyLinkedExcludedBySelect:
    """AC4 Scenario 3: already-linked rows excluded by the SELECT."""

    def test_select_losers_has_previous_version_id_is_null(self) -> None:
        """The _SELECT_LOSERS constant must filter previous_version_id IS NULL."""
        assert "previous_version_id IS NULL" in backfill._SELECT_LOSERS

    def test_select_losers_has_change_type_is_null(self) -> None:
        """The _SELECT_LOSERS constant must filter change_type IS NULL."""
        assert "change_type IS NULL" in backfill._SELECT_LOSERS

    def test_select_losers_targets_superseded_status(self) -> None:
        """The _SELECT_LOSERS constant must filter status='superseded'."""
        assert "superseded" in backfill._SELECT_LOSERS

    def test_fetch_losers_where_clause_excludes_linked_rows(self) -> None:
        """fetch_losers passes the idempotent WHERE clause in the executed query."""
        conn, cur = _make_conn([])
        cur.description = []
        backfill.fetch_losers(conn)
        executed_query = cur.execute.call_args[0][0]
        assert "previous_version_id IS NULL" in executed_query
        assert "change_type IS NULL" in executed_query


# ---------------------------------------------------------------------------
# AC4 Scenario 4: Ambiguous case -- >=2 active candidates -> None
# ---------------------------------------------------------------------------


class TestPickWinnerForLoserAmbiguous:
    """AC4 Scenario 4: pick_winner_for_loser with two or more candidates."""

    def test_two_candidates_returns_none(self) -> None:
        candidates = [{"id": "winner-uuid-1"}, {"id": "winner-uuid-2"}]
        result = backfill.pick_winner_for_loser(candidates)
        assert result is None

    def test_three_candidates_returns_none(self) -> None:
        candidates = [
            {"id": "winner-uuid-1"},
            {"id": "winner-uuid-2"},
            {"id": "winner-uuid-3"},
        ]
        result = backfill.pick_winner_for_loser(candidates)
        assert result is None


# ---------------------------------------------------------------------------
# UPDATE SQL smoke test -- shape, change_type, idempotence WHERE guard
# ---------------------------------------------------------------------------


class TestUpdateSqlShape:
    """Thin smoke test: verify _UPDATE_LOSER SQL has required clauses."""

    def test_update_sets_previous_version_id(self) -> None:
        assert "previous_version_id" in backfill._UPDATE_LOSER

    def test_update_sets_change_type_duplicate_content(self) -> None:
        assert "change_type = 'duplicate_content'" in backfill._UPDATE_LOSER

    def test_update_has_idempotence_guard_previous_version_id(self) -> None:
        """WHERE clause must prevent re-writing an already-linked row."""
        assert "previous_version_id IS NULL" in backfill._UPDATE_LOSER

    def test_update_has_idempotence_guard_change_type(self) -> None:
        assert "change_type IS NULL" in backfill._UPDATE_LOSER

    def test_apply_update_batch_executes_update_sql(self) -> None:
        conn, cur = _make_conn([])
        cur.rowcount = 1
        backfill.apply_update_batch(conn, [("winner-id", "loser-id")], dry_run=False)
        cur.execute.assert_called_once()
        call_sql = cur.execute.call_args[0][0]
        assert "previous_version_id" in call_sql
        assert "duplicate_content" in call_sql
        assert "previous_version_id IS NULL" in call_sql
        assert "change_type IS NULL" in call_sql

    def test_apply_update_batch_dry_run_skips_execute(self) -> None:
        conn, cur = _make_conn([])
        backfill.apply_update_batch(conn, [("winner-id", "loser-id")], dry_run=True)
        cur.execute.assert_not_called()
        conn.commit.assert_not_called()

    def test_apply_update_batch_commits_on_write(self) -> None:
        conn, cur = _make_conn([])
        cur.rowcount = 1
        backfill.apply_update_batch(conn, [("winner-id", "loser-id")], dry_run=False)
        conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# End-to-end main() smoke tests
# ---------------------------------------------------------------------------


class TestMain:
    """Smoke tests for main() with mocked psycopg."""

    def _set_argv(self, *args: str) -> None:
        sys.argv = ["backfill_previous_version_id.py", *args]

    def _setup_conn(
        self, mock_psycopg: MagicMock, loser_rows: list, candidate_rows: list
    ) -> tuple[MagicMock, MagicMock]:
        mock_conn = MagicMock()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # fetchall is called multiple times: first for losers, then per-loser
        # for candidates. Side-effect cycles through the provided values.
        mock_cur.fetchall.side_effect = [loser_rows, candidate_rows]
        mock_cur.rowcount = 1
        # Provide description for fetch_losers column mapping
        mock_cur.description = [
            ("loser_id",),
            ("loser_case_id",),
            ("county",),
        ]
        return mock_conn, mock_cur

    @patch("backfill_previous_version_id.psycopg")
    def test_matching_case_issues_update(self, mock_psycopg: MagicMock) -> None:
        """Single active winner -> UPDATE executed."""
        loser_rows = [("loser-uuid-1", "case-uuid-1", "Orange")]
        candidate_rows = [("winner-uuid-1",)]
        mock_conn, mock_cur = self._setup_conn(mock_psycopg, loser_rows, candidate_rows)
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"}):
            self._set_argv()
            backfill.main()

        update_calls = [
            c for c in mock_cur.execute.call_args_list if "UPDATE" in str(c)
        ]
        assert len(update_calls) >= 1

    @patch("backfill_previous_version_id.psycopg")
    def test_no_candidates_skips_update(self, mock_psycopg: MagicMock) -> None:
        """Zero active winners -> no UPDATE."""
        loser_rows = [("loser-uuid-2", "case-uuid-2", "Los Angeles")]
        candidate_rows: list = []
        mock_conn, mock_cur = self._setup_conn(mock_psycopg, loser_rows, candidate_rows)
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"}):
            self._set_argv()
            backfill.main()

        update_calls = [
            c for c in mock_cur.execute.call_args_list if "UPDATE" in str(c)
        ]
        assert len(update_calls) == 0

    @patch("backfill_previous_version_id.psycopg")
    def test_dry_run_no_update_no_commit(self, mock_psycopg: MagicMock) -> None:
        """--dry-run: no UPDATE, no commit."""
        loser_rows = [("loser-uuid-1", "case-uuid-1", "Orange")]
        candidate_rows = [("winner-uuid-1",)]
        mock_conn, mock_cur = self._setup_conn(mock_psycopg, loser_rows, candidate_rows)
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"}):
            self._set_argv("--dry-run")
            backfill.main()

        update_calls = [
            c for c in mock_cur.execute.call_args_list if "UPDATE" in str(c)
        ]
        assert len(update_calls) == 0
        mock_conn.commit.assert_not_called()

    @patch("backfill_previous_version_id.psycopg")
    def test_empty_fetch_exits_early(self, mock_psycopg: MagicMock) -> None:
        """No losers to process -> no UPDATE, no commit."""
        mock_conn, mock_cur = self._setup_conn(mock_psycopg, [], [])
        # Only one fetchall call expected (losers fetch returns empty)
        mock_cur.fetchall.side_effect = [[]]
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"}):
            self._set_argv()
            backfill.main()

        update_calls = [
            c for c in mock_cur.execute.call_args_list if "UPDATE" in str(c)
        ]
        assert len(update_calls) == 0
        mock_conn.commit.assert_not_called()
