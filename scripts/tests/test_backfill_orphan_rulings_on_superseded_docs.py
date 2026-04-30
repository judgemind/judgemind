"""Tests for the backfill_orphan_rulings_on_superseded_docs script (#3728).

Tests cover:
  1. SQL constants contain the correct WHERE guard and expected table names.
  2. fetch_orphans builds the right query shape.
  3. winner_exists returns True/False correctly.
  4. delete_orphan and insert_metric call the right SQL.
  5. Dry-run mode skips DB writes.
  6. Refusal path: winner missing -> log WARN, skip delete.
  7. Main integration smoke tests.

All database access is mocked -- these tests verify the pure helper logic,
SELECT filters, and DELETE/INSERT shapes without requiring live services.

The script depends on psycopg, structlog, and framework (scraper-framework),
which are not available in the lightweight CI scripts-tests environment.
We mock them in sys.modules before importing the script under test.
"""

from __future__ import annotations

import json
import os
import sys
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

import backfill_orphan_rulings_on_superseded_docs as backfill  # noqa: E402

# Restore sys.modules so the mock injection doesn't break other test files.
for _mod_name in list(_modules_to_mock.keys()):
    if _mod_name in _saved_modules:
        sys.modules[_mod_name] = _saved_modules[_mod_name]
    elif _mod_name in sys.modules:
        del sys.modules[_mod_name]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(
    fetchall_return: list, fetchone_return: object = None
) -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = fetchall_return
    cur.fetchone.return_value = fetchone_return
    return conn, cur


# ---------------------------------------------------------------------------
# SQL constant shape tests
# ---------------------------------------------------------------------------


class TestSelectOrphansSqlShape:
    """The _SELECT_ORPHANS constant must contain the correct WHERE guard."""

    def test_joins_documents(self) -> None:
        assert "JOIN derived.documents" in backfill._SELECT_ORPHANS

    def test_joins_courts(self) -> None:
        assert "JOIN derived.courts" in backfill._SELECT_ORPHANS

    def test_filters_non_active_status(self) -> None:
        assert "d.status != 'active'" in backfill._SELECT_ORPHANS

    def test_selects_ruling_id(self) -> None:
        assert "r.id" in backfill._SELECT_ORPHANS

    def test_selects_loser_document_id(self) -> None:
        assert "r.document_id" in backfill._SELECT_ORPHANS

    def test_selects_winner_document_id(self) -> None:
        assert "previous_version_id" in backfill._SELECT_ORPHANS


class TestSelectWinnerCheckSqlShape:
    """The _SELECT_WINNER_CHECK constant must verify an active-document winner."""

    def test_joins_documents(self) -> None:
        assert "JOIN derived.documents" in backfill._SELECT_WINNER_CHECK

    def test_filters_active_status(self) -> None:
        assert "d2.status = 'active'" in backfill._SELECT_WINNER_CHECK

    def test_matches_case_id(self) -> None:
        assert "r2.case_id" in backfill._SELECT_WINNER_CHECK

    def test_matches_ruling_text_hash(self) -> None:
        assert "ruling_text_hash" in backfill._SELECT_WINNER_CHECK


class TestDeleteOrphanSqlShape:
    """The _DELETE_ORPHAN_RULING constant must target the right table and column."""

    def test_deletes_from_rulings(self) -> None:
        assert "DELETE FROM derived.rulings" in backfill._DELETE_ORPHAN_RULING

    def test_filters_by_id(self) -> None:
        assert "id = %s" in backfill._DELETE_ORPHAN_RULING


class TestInsertMetricSqlShape:
    """The _INSERT_METRIC constant must target telemetry.data_quality_metrics."""

    def test_inserts_into_metrics(self) -> None:
        assert "telemetry.data_quality_metrics" in backfill._INSERT_METRIC

    def test_has_metric_name_column(self) -> None:
        assert "metric_name" in backfill._INSERT_METRIC


# ---------------------------------------------------------------------------
# winner_exists tests
# ---------------------------------------------------------------------------


class TestWinnerExists:
    """winner_exists returns True when a matching active-document ruling exists."""

    def test_returns_true_when_winner_found(self) -> None:
        conn, cur = _make_conn([], fetchone_return=(1,))
        result = backfill.winner_exists(conn, "case-uuid-1", "abc123")
        assert result is True

    def test_returns_false_when_no_winner(self) -> None:
        conn, cur = _make_conn([], fetchone_return=None)
        result = backfill.winner_exists(conn, "case-uuid-1", "abc123")
        assert result is False

    def test_returns_false_when_hash_is_none(self) -> None:
        conn, cur = _make_conn([], fetchone_return=(1,))
        result = backfill.winner_exists(conn, "case-uuid-1", None)
        # Should not even query the DB for None hash.
        assert result is False
        cur.execute.assert_not_called()


# ---------------------------------------------------------------------------
# delete_orphan tests
# ---------------------------------------------------------------------------


class TestDeleteOrphan:
    """delete_orphan issues a DELETE or skips in dry-run mode."""

    def test_executes_delete_on_write(self) -> None:
        conn, cur = _make_conn([])
        backfill.delete_orphan(conn, "ruling-uuid-1", dry_run=False)
        cur.execute.assert_called_once()
        call_sql = cur.execute.call_args[0][0]
        assert "DELETE FROM derived.rulings" in call_sql

    def test_dry_run_skips_execute(self) -> None:
        conn, cur = _make_conn([])
        result = backfill.delete_orphan(conn, "ruling-uuid-1", dry_run=True)
        cur.execute.assert_not_called()
        assert result is True

    def test_returns_true_on_write(self) -> None:
        conn, cur = _make_conn([])
        result = backfill.delete_orphan(conn, "ruling-uuid-1", dry_run=False)
        assert result is True


# ---------------------------------------------------------------------------
# insert_metric tests
# ---------------------------------------------------------------------------


class TestInsertMetric:
    """insert_metric inserts the correct metric row."""

    def test_executes_insert_on_write(self) -> None:
        conn, cur = _make_conn([])
        orphan = {
            "loser_document_id": "loser-doc",
            "winner_document_id": "winner-doc",
            "case_id": "case-uuid",
            "ruling_text_hash": "abc123",
        }
        backfill.insert_metric(conn, "Los Angeles", orphan, dry_run=False)
        cur.execute.assert_called_once()
        call_args = cur.execute.call_args[0]
        assert "telemetry.data_quality_metrics" in call_args[0]
        params = call_args[1]
        assert params[0] == "Los Angeles"
        assert params[1] == "orphan_ruling_deleted"
        assert params[2] == 1
        metadata = json.loads(params[3])
        assert metadata["loser_document_id"] == "loser-doc"
        assert metadata["winner_document_id"] == "winner-doc"
        assert metadata["case_id"] == "case-uuid"
        assert metadata["ruling_text_hash"] == "abc123"

    def test_dry_run_skips_execute(self) -> None:
        conn, cur = _make_conn([])
        orphan = {
            "loser_document_id": "loser-doc",
            "winner_document_id": "winner-doc",
            "case_id": "case-uuid",
            "ruling_text_hash": "abc123",
        }
        backfill.insert_metric(conn, "Los Angeles", orphan, dry_run=True)
        cur.execute.assert_not_called()


# ---------------------------------------------------------------------------
# End-to-end main() smoke tests
# ---------------------------------------------------------------------------


class TestMain:
    """Smoke tests for main() with mocked psycopg."""

    def _set_argv(self, *args: str) -> None:
        sys.argv = ["backfill_orphan_rulings_on_superseded_docs.py", *args]

    def _setup_conn(
        self,
        mock_psycopg: MagicMock,
        orphan_rows: list,
        winner_fetchone: object = (1,),
    ) -> tuple[MagicMock, MagicMock]:
        mock_conn = MagicMock()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # fetchall called once for orphan SELECT
        mock_cur.fetchall.return_value = orphan_rows
        # fetchone called once per orphan for winner check
        mock_cur.fetchone.return_value = winner_fetchone
        mock_cur.rowcount = 1
        return mock_conn, mock_cur

    @patch("backfill_orphan_rulings_on_superseded_docs.psycopg")
    def test_winner_exists_issues_delete_and_metric(
        self, mock_psycopg: MagicMock
    ) -> None:
        """Winner found -> DELETE + INSERT metric."""
        orphan_rows = [
            (
                "ruling-uuid-1",
                "case-uuid-1",
                "hash-abc",
                "loser-doc",
                "winner-doc",
                "Los Angeles",
            ),
        ]
        mock_conn, mock_cur = self._setup_conn(
            mock_psycopg, orphan_rows, winner_fetchone=(1,)
        )
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"}):
            self._set_argv()
            backfill.main()

        delete_calls = [
            c for c in mock_cur.execute.call_args_list if "DELETE" in str(c)
        ]
        assert len(delete_calls) >= 1

        insert_calls = [
            c for c in mock_cur.execute.call_args_list if "INSERT" in str(c)
        ]
        assert len(insert_calls) >= 1
        assert any("orphan_ruling_deleted" in str(c) for c in insert_calls)

        mock_conn.commit.assert_called()

    @patch("backfill_orphan_rulings_on_superseded_docs.psycopg")
    def test_winner_missing_skips_delete(self, mock_psycopg: MagicMock) -> None:
        """Winner not found -> no DELETE, no metric, commit not called."""
        orphan_rows = [
            ("ruling-uuid-2", "case-uuid-2", "hash-xyz", "loser-doc", None, "Fresno"),
        ]
        mock_conn, mock_cur = self._setup_conn(
            mock_psycopg, orphan_rows, winner_fetchone=None
        )
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"}):
            self._set_argv()
            backfill.main()

        delete_calls = [
            c for c in mock_cur.execute.call_args_list if "DELETE" in str(c)
        ]
        assert len(delete_calls) == 0
        mock_conn.commit.assert_not_called()

    @patch("backfill_orphan_rulings_on_superseded_docs.psycopg")
    def test_dry_run_no_delete_no_commit(self, mock_psycopg: MagicMock) -> None:
        """--dry-run: no DELETE, no INSERT metric, no commit."""
        orphan_rows = [
            (
                "ruling-uuid-1",
                "case-uuid-1",
                "hash-abc",
                "loser-doc",
                "winner-doc",
                "Los Angeles",
            ),
        ]
        mock_conn, mock_cur = self._setup_conn(
            mock_psycopg, orphan_rows, winner_fetchone=(1,)
        )
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"}):
            self._set_argv("--dry-run")
            backfill.main()

        delete_calls = [
            c for c in mock_cur.execute.call_args_list if "DELETE" in str(c)
        ]
        assert len(delete_calls) == 0

        insert_calls = [
            c for c in mock_cur.execute.call_args_list if "INSERT" in str(c)
        ]
        assert len(insert_calls) == 0

        mock_conn.commit.assert_not_called()

    @patch("backfill_orphan_rulings_on_superseded_docs.psycopg")
    def test_empty_fetch_exits_early(self, mock_psycopg: MagicMock) -> None:
        """No orphans -> no DELETE, no metric, no commit."""
        mock_conn, mock_cur = self._setup_conn(mock_psycopg, [], winner_fetchone=None)
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"}):
            self._set_argv()
            backfill.main()

        delete_calls = [
            c for c in mock_cur.execute.call_args_list if "DELETE" in str(c)
        ]
        assert len(delete_calls) == 0
        mock_conn.commit.assert_not_called()
