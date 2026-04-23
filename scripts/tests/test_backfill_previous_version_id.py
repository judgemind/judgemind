"""Tests for the backfill_previous_version_id script (#2653).

All database access is mocked — these tests verify the SELECT filters,
UPDATE shape, ambiguous/no-winner counters, dry-run no-commit behavior,
and idempotence without requiring live services.

The script depends on psycopg, structlog, and framework (scraper-framework),
which are not available in the lightweight CI scripts-tests environment.
We mock them in sys.modules before importing the script under test.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Pre-import mocking — the script imports psycopg, structlog, and
# framework.logging at module level, which are not installed in the CI
# scripts-tests environment.
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_mock_psycopg = MagicMock()
_mock_structlog = MagicMock()
_mock_structlog.get_logger.return_value = MagicMock()
_mock_framework = MagicMock()
_mock_framework_logging = MagicMock()

_modules_to_mock = {
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

backfill = importlib.import_module("backfill_previous_version_id")

# Restore sys.modules so the mock injection doesn't break other test files.
# The script's module-level bindings remain as mocks (captured at import time).
for _mod_name in list(_modules_to_mock.keys()):
    if _mod_name in _saved_modules:
        sys.modules[_mod_name] = _saved_modules[_mod_name]
    elif _mod_name in sys.modules:
        del sys.modules[_mod_name]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LOSER_ROW = (
    "loser-uuid-1",
    "case-uuid-1",
    "s3/path/loser.html",
    "Los Angeles",
    "winner-uuid-1",
)
_NO_WINNER_ROW = (
    "loser-uuid-2",
    "case-uuid-2",
    "s3/path/loser2.html",
    "Orange",
    None,
)
_AMBIGUOUS_ROW = (
    "loser-uuid-3",
    "case-uuid-3",
    "s3/path/loser3.html",
    "San Diego",
    "AMBIGUOUS",
)


def _make_conn(rows: list[Any]) -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = rows
    return conn, cur


# ---------------------------------------------------------------------------
# TestFetchLoserWinnerPairs — unit tests for the fetch function
# ---------------------------------------------------------------------------


class TestFetchLoserWinnerPairs:
    """Tests for fetch_loser_winner_pairs()."""

    def test_matching_case_returns_winner_id(self) -> None:
        """Matching case: 1 unique ruling for case → winner_id returned."""
        conn, _ = _make_conn([_LOSER_ROW])
        rows = backfill.fetch_loser_winner_pairs(conn)
        assert len(rows) == 1
        assert rows[0]["loser_id"] == "loser-uuid-1"
        assert rows[0]["winner_id"] == "winner-uuid-1"
        assert rows[0]["county"] == "Los Angeles"
        assert rows[0]["case_id"] == "case-uuid-1"

    def test_no_winner_case_returns_none_winner(self) -> None:
        """No-winner case: row has no rulings → winner_id is None."""
        conn, _ = _make_conn([_NO_WINNER_ROW])
        rows = backfill.fetch_loser_winner_pairs(conn)
        assert len(rows) == 1
        assert rows[0]["winner_id"] is None

    def test_ambiguous_case_returns_sentinel(self) -> None:
        """Ambiguous case: multiple distinct hashes → winner_id is 'AMBIGUOUS'."""
        conn, _ = _make_conn([_AMBIGUOUS_ROW])
        rows = backfill.fetch_loser_winner_pairs(conn)
        assert len(rows) == 1
        assert rows[0]["winner_id"] == "AMBIGUOUS"

    def test_already_linked_excluded_by_where_clause(self) -> None:
        """Already-linked rows are excluded: WHERE clause requires previous_version_id IS NULL."""
        conn, cur = _make_conn([])
        backfill.fetch_loser_winner_pairs(conn)
        executed_query = cur.execute.call_args[0][0]
        assert "previous_version_id IS NULL" in executed_query
        assert "change_type IS NULL" in executed_query
        assert "superseded" in executed_query

    def test_sql_filters_ruling_text_hash_not_null(self) -> None:
        """The rulings JOIN filters ruling_text_hash IS NOT NULL."""
        conn, cur = _make_conn([])
        backfill.fetch_loser_winner_pairs(conn)
        executed_query = cur.execute.call_args[0][0]
        assert "ruling_text_hash IS NOT NULL" in executed_query

    def test_limit_applied_when_provided(self) -> None:
        conn, cur = _make_conn([])
        backfill.fetch_loser_winner_pairs(conn, limit=5)
        executed_query = cur.execute.call_args[0][0]
        assert "LIMIT 5" in executed_query

    def test_no_limit_when_not_provided(self) -> None:
        conn, cur = _make_conn([])
        backfill.fetch_loser_winner_pairs(conn)
        executed_query = cur.execute.call_args[0][0]
        assert not re.search(r"LIMIT\s+\d+\s*$", executed_query.strip())


# ---------------------------------------------------------------------------
# TestApplyLink — unit tests for the update function
# ---------------------------------------------------------------------------


class TestApplyLink:
    """Tests for apply_link()."""

    def test_executes_update_with_correct_params(self) -> None:
        conn, cur = _make_conn([])
        backfill.apply_link(conn, "loser-uuid-1", "winner-uuid-1")
        cur.execute.assert_called_once()
        params = cur.execute.call_args[0][1]
        assert "winner-uuid-1" in str(params)
        assert "loser-uuid-1" in str(params)

    def test_update_sets_previous_version_id_and_change_type(self) -> None:
        conn, cur = _make_conn([])
        backfill.apply_link(conn, "loser-uuid-1", "winner-uuid-1")
        executed_query = cur.execute.call_args[0][0]
        assert "previous_version_id" in executed_query
        assert "duplicate_content" in executed_query

    def test_update_has_idempotent_where_clause(self) -> None:
        """UPDATE WHERE clause guards against re-linking already-linked rows."""
        conn, cur = _make_conn([])
        backfill.apply_link(conn, "loser-uuid-1", "winner-uuid-1")
        executed_query = cur.execute.call_args[0][0]
        assert "previous_version_id IS NULL" in executed_query
        assert "change_type IS NULL" in executed_query


# ---------------------------------------------------------------------------
# TestSummarize — unit tests for outcome aggregation
# ---------------------------------------------------------------------------


class TestSummarize:
    """Tests for summarize()."""

    def test_counts_by_county_for_updated(self) -> None:
        rows = [
            {"county": "Los Angeles", "outcome": "updated"},
            {"county": "Los Angeles", "outcome": "updated"},
            {"county": "Orange", "outcome": "updated"},
        ]
        result = backfill.summarize(rows)
        assert result["updated_per_county"]["Los Angeles"] == 2
        assert result["updated_per_county"]["Orange"] == 1

    def test_counts_ambiguous(self) -> None:
        rows = [
            {"county": "Los Angeles", "outcome": "ambiguous"},
            {"county": "Orange", "outcome": "ambiguous"},
        ]
        result = backfill.summarize(rows)
        assert result["ambiguous"] == 2

    def test_counts_no_winner(self) -> None:
        rows = [{"county": "Riverside", "outcome": "no_winner"}]
        result = backfill.summarize(rows)
        assert result["no_winner"] == 1

    def test_mixed_outcomes(self) -> None:
        rows = [
            {"county": "Los Angeles", "outcome": "updated"},
            {"county": "Los Angeles", "outcome": "ambiguous"},
            {"county": "San Diego", "outcome": "no_winner"},
            {"county": "Orange", "outcome": "updated"},
        ]
        result = backfill.summarize(rows)
        assert result["updated_per_county"]["Los Angeles"] == 1
        assert result["updated_per_county"]["Orange"] == 1
        assert result["ambiguous"] == 1
        assert result["no_winner"] == 1

    def test_empty_returns_zeroes(self) -> None:
        result = backfill.summarize([])
        assert result["ambiguous"] == 0
        assert result["no_winner"] == 0
        assert result["updated_per_county"] == {}


# ---------------------------------------------------------------------------
# TestMain — integration tests for the main() entry point
# ---------------------------------------------------------------------------


class TestMain:
    """Integration tests for main() with mocked psycopg."""

    def _set_argv(self, *args: str) -> None:
        sys.argv = ["backfill_previous_version_id.py", *args]

    def _make_mock_conn(
        self, mock_psycopg: MagicMock, fetch_rows: list[Any]
    ) -> tuple[MagicMock, MagicMock]:
        mock_conn = MagicMock()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchall.return_value = fetch_rows
        return mock_conn, mock_cur

    @patch("backfill_previous_version_id.psycopg")
    def test_matching_case_links_loser_to_winner(self, mock_psycopg: MagicMock) -> None:
        """Matching case: single candidate winner → UPDATE issued with correct IDs."""
        mock_conn, mock_cur = self._make_mock_conn(mock_psycopg, [_LOSER_ROW])
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"}):
            self._set_argv()
            backfill.main()

        update_calls = [
            c for c in mock_cur.execute.call_args_list if "UPDATE" in str(c)
        ]
        assert len(update_calls) >= 1
        update_sql = str(update_calls[0])
        assert "winner-uuid-1" in update_sql or "loser-uuid-1" in update_sql

    @patch("backfill_previous_version_id.psycopg")
    def test_no_winner_case_skips_update(self, mock_psycopg: MagicMock) -> None:
        """No-winner case: winner_id is None → no UPDATE called, counted as no_winner."""
        mock_conn, mock_cur = self._make_mock_conn(mock_psycopg, [_NO_WINNER_ROW])
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"}):
            self._set_argv()
            backfill.main()

        update_calls = [
            c for c in mock_cur.execute.call_args_list if "UPDATE" in str(c)
        ]
        assert len(update_calls) == 0

    @patch("backfill_previous_version_id.psycopg")
    def test_already_linked_skipped_by_select(self, mock_psycopg: MagicMock) -> None:
        """Already-linked rows excluded by SELECT (previous_version_id IS NULL filter)
        — fetch returns empty, no UPDATE called, no commit."""
        mock_conn, mock_cur = self._make_mock_conn(mock_psycopg, [])
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"}):
            self._set_argv()
            backfill.main()

        update_calls = [
            c for c in mock_cur.execute.call_args_list if "UPDATE" in str(c)
        ]
        assert len(update_calls) == 0
        mock_conn.commit.assert_not_called()

    @patch("backfill_previous_version_id.psycopg")
    def test_ambiguous_case_skips_update(self, mock_psycopg: MagicMock) -> None:
        """Ambiguous case: winner_id is 'AMBIGUOUS' → no UPDATE, counted as ambiguous."""
        mock_conn, mock_cur = self._make_mock_conn(mock_psycopg, [_AMBIGUOUS_ROW])
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"}):
            self._set_argv()
            backfill.main()

        update_calls = [
            c for c in mock_cur.execute.call_args_list if "UPDATE" in str(c)
        ]
        assert len(update_calls) == 0

    @patch("backfill_previous_version_id.psycopg")
    def test_dry_run_no_commit(self, mock_psycopg: MagicMock) -> None:
        """--dry-run flag: UPDATE not called and commit not called."""
        mock_conn, mock_cur = self._make_mock_conn(mock_psycopg, [_LOSER_ROW])
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"}):
            self._set_argv("--dry-run")
            backfill.main()

        update_calls = [
            c for c in mock_cur.execute.call_args_list if "UPDATE" in str(c)
        ]
        assert len(update_calls) == 0
        mock_conn.commit.assert_not_called()

    @patch("backfill_previous_version_id.psycopg")
    def test_write_path_commits(self, mock_psycopg: MagicMock) -> None:
        """Write path (no --dry-run): commit is called after updates."""
        mock_conn, _mock_cur = self._make_mock_conn(mock_psycopg, [_LOSER_ROW])
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"}):
            self._set_argv()
            backfill.main()

        mock_conn.commit.assert_called()
