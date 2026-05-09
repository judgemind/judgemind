"""Tests for the backfill_split_supersede script (#3510).

Tests cover:
  1. SQL constants contain the idempotent WHERE guard.
  2. Candidate picker selects newest sibling with rulings.
  3. Falls back to 'split_supersede_unverified' when no valid sibling.
  4. UPDATE SQL shapes for both paths.
  5. Dry-run mode skips DB writes.

All database access is mocked -- these tests verify the pure helper logic,
SELECT filters, and UPDATE shapes without requiring live services.

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
    import backfill_split_supersede as backfill  # noqa: E402


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
# SQL constant shape tests
# ---------------------------------------------------------------------------


class TestSelectLosersSqlShape:
    """The _SELECT_LOSERS constant must contain the correct WHERE guards."""

    def test_filters_superseded_status(self) -> None:
        assert "superseded" in backfill._SELECT_LOSERS

    def test_filters_previous_version_id_is_null(self) -> None:
        assert "previous_version_id IS NULL" in backfill._SELECT_LOSERS

    def test_filters_change_type_is_null(self) -> None:
        assert "change_type IS NULL" in backfill._SELECT_LOSERS


class TestUpdateWithSiblingSqlShape:
    """_UPDATE_LOSER_WITH_SIBLING must set both fields and have idempotent guard."""

    def test_sets_previous_version_id(self) -> None:
        assert "previous_version_id" in backfill._UPDATE_LOSER_WITH_SIBLING

    def test_sets_change_type_split_supersede(self) -> None:
        assert "split_supersede" in backfill._UPDATE_LOSER_WITH_SIBLING

    def test_has_idempotent_guard_previous_version_id(self) -> None:
        assert "previous_version_id IS NULL" in backfill._UPDATE_LOSER_WITH_SIBLING

    def test_has_idempotent_guard_change_type(self) -> None:
        assert "change_type IS NULL" in backfill._UPDATE_LOSER_WITH_SIBLING


class TestUpdateUnverifiedSqlShape:
    """_UPDATE_LOSER_UNVERIFIED must set change_type and have idempotent guard."""

    def test_sets_change_type_split_supersede_unverified(self) -> None:
        assert "split_supersede_unverified" in backfill._UPDATE_LOSER_UNVERIFIED

    def test_has_idempotent_guard_previous_version_id(self) -> None:
        assert "previous_version_id IS NULL" in backfill._UPDATE_LOSER_UNVERIFIED

    def test_has_idempotent_guard_change_type(self) -> None:
        assert "change_type IS NULL" in backfill._UPDATE_LOSER_UNVERIFIED


# ---------------------------------------------------------------------------
# pick_sibling_for_loser tests
# ---------------------------------------------------------------------------


class TestPickSiblingForLoser:
    """pick_sibling_for_loser selects newest sibling (first after ORDER BY DESC)."""

    def test_single_candidate_returns_its_id(self) -> None:
        candidates = [{"id": "sibling-uuid-1", "created_at": "2026-01-01"}]
        result = backfill.pick_sibling_for_loser(candidates)
        assert result == "sibling-uuid-1"

    def test_multiple_candidates_returns_first_newest(self) -> None:
        # Candidates are pre-sorted newest-first by SQL ORDER BY created_at DESC.
        candidates = [
            {"id": "newest-uuid", "created_at": "2026-03-01"},
            {"id": "older-uuid", "created_at": "2025-12-01"},
        ]
        result = backfill.pick_sibling_for_loser(candidates)
        assert result == "newest-uuid"

    def test_empty_candidates_returns_none(self) -> None:
        result = backfill.pick_sibling_for_loser([])
        assert result is None


# ---------------------------------------------------------------------------
# apply_update_with_sibling tests
# ---------------------------------------------------------------------------


class TestApplyUpdateWithSibling:
    """apply_update_with_sibling executes UPDATE SQL with correct shape."""

    def test_executes_update_with_sibling_sql(self) -> None:
        conn, cur = _make_conn([])
        cur.rowcount = 1
        backfill.apply_update_with_sibling(
            conn, [("sibling-id", "loser-id")], dry_run=False
        )
        cur.execute.assert_called_once()
        call_sql = cur.execute.call_args[0][0]
        assert "previous_version_id" in call_sql
        assert "split_supersede" in call_sql
        assert "previous_version_id IS NULL" in call_sql
        assert "change_type IS NULL" in call_sql

    def test_dry_run_skips_execute(self) -> None:
        conn, cur = _make_conn([])
        backfill.apply_update_with_sibling(
            conn, [("sibling-id", "loser-id")], dry_run=True
        )
        cur.execute.assert_not_called()
        conn.commit.assert_not_called()

    def test_commits_on_write(self) -> None:
        conn, cur = _make_conn([])
        cur.rowcount = 1
        backfill.apply_update_with_sibling(
            conn, [("sibling-id", "loser-id")], dry_run=False
        )
        conn.commit.assert_called_once()

    def test_returns_count_of_intended_updates_on_dry_run(self) -> None:
        conn, cur = _make_conn([])
        updates = [("s1", "l1"), ("s2", "l2")]
        count = backfill.apply_update_with_sibling(conn, updates, dry_run=True)
        assert count == 2


# ---------------------------------------------------------------------------
# apply_update_unverified tests
# ---------------------------------------------------------------------------


class TestApplyUpdateUnverified:
    """apply_update_unverified sets change_type='split_supersede_unverified'."""

    def test_executes_unverified_update_sql(self) -> None:
        conn, cur = _make_conn([])
        cur.rowcount = 1
        backfill.apply_update_unverified(conn, ["loser-id"], dry_run=False)
        cur.execute.assert_called_once()
        call_sql = cur.execute.call_args[0][0]
        assert "split_supersede_unverified" in call_sql
        assert "previous_version_id IS NULL" in call_sql
        assert "change_type IS NULL" in call_sql

    def test_dry_run_skips_execute(self) -> None:
        conn, cur = _make_conn([])
        backfill.apply_update_unverified(conn, ["loser-id"], dry_run=True)
        cur.execute.assert_not_called()
        conn.commit.assert_not_called()

    def test_commits_on_write(self) -> None:
        conn, cur = _make_conn([])
        cur.rowcount = 1
        backfill.apply_update_unverified(conn, ["loser-id"], dry_run=False)
        conn.commit.assert_called_once()

    def test_returns_count_of_intended_updates_on_dry_run(self) -> None:
        conn, cur = _make_conn([])
        count = backfill.apply_update_unverified(conn, ["l1", "l2", "l3"], dry_run=True)
        assert count == 3


# ---------------------------------------------------------------------------
# End-to-end main() smoke tests
# ---------------------------------------------------------------------------


class TestMain:
    """Smoke tests for main() with mocked psycopg."""

    def _set_argv(self, *args: str) -> None:
        sys.argv = ["backfill_split_supersede.py", *args]

    def _setup_conn(
        self,
        mock_psycopg: MagicMock,
        loser_rows: list,
        sibling_rows: list,
    ) -> tuple[MagicMock, MagicMock]:
        mock_conn = MagicMock()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # fetchall is called once for losers, then per-loser for sibling candidates.
        mock_cur.fetchall.side_effect = [loser_rows, sibling_rows]
        mock_cur.rowcount = 1
        return mock_conn, mock_cur

    @patch("backfill_split_supersede.psycopg")
    def test_sibling_found_issues_update_with_sibling(
        self, mock_psycopg: MagicMock
    ) -> None:
        """Single active sibling with rulings -> UPDATE with previous_version_id."""
        loser_rows = [("loser-uuid-1", "case-uuid-1", "Los Angeles")]
        sibling_rows = [("sibling-uuid-1", "2026-03-01")]
        mock_conn, mock_cur = self._setup_conn(mock_psycopg, loser_rows, sibling_rows)
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"}):
            self._set_argv()
            backfill.main()

        update_calls = [
            c for c in mock_cur.execute.call_args_list if "UPDATE" in str(c)
        ]
        assert len(update_calls) >= 1
        # The UPDATE should reference previous_version_id
        assert any("previous_version_id" in str(c) for c in update_calls)

    @patch("backfill_split_supersede.psycopg")
    def test_no_sibling_issues_unverified_update(self, mock_psycopg: MagicMock) -> None:
        """No active siblings with rulings -> UPDATE change_type='split_supersede_unverified'."""
        loser_rows = [("loser-uuid-2", "case-uuid-2", "Fresno")]
        sibling_rows: list = []
        mock_conn, mock_cur = self._setup_conn(mock_psycopg, loser_rows, sibling_rows)
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"}):
            self._set_argv()
            backfill.main()

        update_calls = [
            c for c in mock_cur.execute.call_args_list if "UPDATE" in str(c)
        ]
        assert len(update_calls) >= 1
        assert any("split_supersede_unverified" in str(c) for c in update_calls)

    @patch("backfill_split_supersede.psycopg")
    def test_dry_run_no_update_no_commit(self, mock_psycopg: MagicMock) -> None:
        """--dry-run: no UPDATE, no commit."""
        loser_rows = [("loser-uuid-1", "case-uuid-1", "Los Angeles")]
        sibling_rows = [("sibling-uuid-1", "2026-03-01")]
        mock_conn, mock_cur = self._setup_conn(mock_psycopg, loser_rows, sibling_rows)
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"}):
            self._set_argv("--dry-run")
            backfill.main()

        update_calls = [
            c for c in mock_cur.execute.call_args_list if "UPDATE" in str(c)
        ]
        assert len(update_calls) == 0
        mock_conn.commit.assert_not_called()

    @patch("backfill_split_supersede.psycopg")
    def test_empty_fetch_exits_early(self, mock_psycopg: MagicMock) -> None:
        """No losers -> no UPDATE, no commit."""
        mock_conn, mock_cur = self._setup_conn(mock_psycopg, [], [])
        mock_cur.fetchall.side_effect = [[]]
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"}):
            self._set_argv()
            backfill.main()

        update_calls = [
            c for c in mock_cur.execute.call_args_list if "UPDATE" in str(c)
        ]
        assert len(update_calls) == 0
        mock_conn.commit.assert_not_called()
