"""Tests for the riverside_remediation script (#1985).

All database access is mocked -- these tests verify the query execution flow,
dry-run behavior, gate verification, and snapshot logic without requiring a
live database.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
)
sys.path.insert(0, _SCRIPTS_DIR)
remediation = importlib.import_module("riverside_remediation")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_cursor_with_results(results: list[list[tuple]]) -> MagicMock:
    """Create a mock cursor that returns different results for each execute."""
    cursor = MagicMock()
    cursor.fetchall = MagicMock(side_effect=results)
    cursor.fetchone = MagicMock(side_effect=[r[0] if r else None for r in results])
    cursor.rowcount = 0
    return cursor


# ---------------------------------------------------------------------------
# Snapshot tests
# ---------------------------------------------------------------------------


class TestTakeSnapshot:
    """Tests for take_snapshot()."""

    def test_returns_dict_of_table_counts(self) -> None:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            ("rulings", 526),
            ("documents", 1964),
            ("cases", 200),
            ("unique_s3_keys", 50),
        ]
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        result = remediation.take_snapshot(conn)

        assert result == {
            "rulings": 526,
            "documents": 1964,
            "cases": 200,
            "unique_s3_keys": 50,
        }

    def test_empty_result(self) -> None:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        result = remediation.take_snapshot(conn)

        assert result == {}


# ---------------------------------------------------------------------------
# Orphaned docs tests
# ---------------------------------------------------------------------------


class TestCountOrphanedDocs:
    """Tests for count_orphaned_docs()."""

    def test_returns_count(self) -> None:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (42,)
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        result = remediation.count_orphaned_docs(conn)

        assert result == 42

    def test_returns_zero_when_no_orphans(self) -> None:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (0,)
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        result = remediation.count_orphaned_docs(conn)

        assert result == 0

    def test_returns_zero_on_none_result(self) -> None:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        result = remediation.count_orphaned_docs(conn)

        assert result == 0


class TestDeleteOrphanedDocs:
    """Tests for delete_orphaned_docs()."""

    def test_returns_rowcount(self) -> None:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = 15
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        result = remediation.delete_orphaned_docs(conn)

        assert result == 15

    def test_executes_delete_query(self) -> None:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = 0
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        remediation.delete_orphaned_docs(conn)

        cursor.execute.assert_called_once()
        sql = cursor.execute.call_args[0][0]
        assert "DELETE FROM documents" in sql
        assert "Riverside" in sql


# ---------------------------------------------------------------------------
# Duplicate rulings tests
# ---------------------------------------------------------------------------


class TestCountDuplicateRulings:
    """Tests for count_duplicate_rulings()."""

    def test_returns_count(self) -> None:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (59,)
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        result = remediation.count_duplicate_rulings(conn)

        assert result == 59


class TestDeleteDuplicateRulings:
    """Tests for delete_duplicate_rulings()."""

    def test_returns_rowcount(self) -> None:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = 30
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        result = remediation.delete_duplicate_rulings(conn)

        assert result == 30

    def test_executes_delete_query_preserving_longest(self) -> None:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = 0
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        remediation.delete_duplicate_rulings(conn)

        cursor.execute.assert_called_once()
        sql = cursor.execute.call_args[0][0]
        assert "DELETE FROM rulings" in sql
        assert "LENGTH(r3.ruling_text) DESC" in sql
        assert "DISTINCT ON (ruling_text_hash)" in sql


# ---------------------------------------------------------------------------
# Gate verification tests
# ---------------------------------------------------------------------------


class TestVerifyGates:
    """Tests for verify_gates()."""

    def test_all_gates_pass(self) -> None:
        conn = MagicMock()
        cursor = MagicMock()
        # First call: duplicate hashes = 0, Second call: orphaned docs = 0
        cursor.fetchone = MagicMock(side_effect=[(0,), (0,)])
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        gates = remediation.verify_gates(conn)

        assert len(gates) == 2
        assert all(g["passed"] for g in gates)
        assert gates[0]["name"] == "no_duplicate_ruling_text_hash"
        assert gates[0]["value"] == 0
        assert gates[1]["name"] == "no_orphaned_documents"
        assert gates[1]["value"] == 0

    def test_duplicate_gate_fails(self) -> None:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone = MagicMock(side_effect=[(5,), (0,)])
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        gates = remediation.verify_gates(conn)

        assert not gates[0]["passed"]
        assert gates[0]["value"] == 5
        assert gates[1]["passed"]

    def test_orphan_gate_fails(self) -> None:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone = MagicMock(side_effect=[(0,), (3,)])
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        gates = remediation.verify_gates(conn)

        assert gates[0]["passed"]
        assert not gates[1]["passed"]
        assert gates[1]["value"] == 3


# ---------------------------------------------------------------------------
# run_remediation integration tests (mocked DB)
# ---------------------------------------------------------------------------


class TestRunRemediation:
    """Tests for run_remediation() end-to-end flow with mocked DB."""

    @patch("riverside_remediation.psycopg")
    def test_dry_run_does_not_modify(self, mock_psycopg: MagicMock) -> None:
        """In dry-run mode, no DELETEs should be executed."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = conn

        # Sequence of fetchall/fetchone calls:
        # 1. snapshot (fetchall)
        # 2. count orphans (fetchone)
        # 3. count duplicates (fetchone)
        # 4. post-snapshot (fetchall)
        # 5-6. gate checks (fetchone x2)
        cursor.fetchall.side_effect = [
            [("rulings", 100), ("documents", 200)],
            [("rulings", 100), ("documents", 200)],
        ]
        cursor.fetchone.side_effect = [
            (10,),  # orphan count
            (5,),  # duplicate count
            (0,),  # gate: duplicate hashes (dry-run, so still non-zero before cleanup)
            (0,),  # gate: orphaned docs (dry-run, so still non-zero before cleanup)
        ]

        summary = remediation.run_remediation("postgresql://test", dry_run=True)

        assert summary["orphaned_docs_found"] == 10
        assert summary["orphaned_docs_deleted"] == 0
        assert summary["duplicate_rulings_found"] == 5
        assert summary["duplicate_rulings_deleted"] == 0
        # conn.commit() should not be called in dry-run mode
        conn.commit.assert_not_called()

    @patch("riverside_remediation.psycopg")
    def test_live_run_commits_changes(self, mock_psycopg: MagicMock) -> None:
        """In live mode, DELETEs should be executed and committed."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        cursor.rowcount = 10  # for delete operations
        conn.cursor.return_value = cursor
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = conn

        cursor.fetchall.side_effect = [
            [("rulings", 100), ("documents", 200)],
            [("rulings", 90), ("documents", 190)],
        ]
        cursor.fetchone.side_effect = [
            (10,),  # orphan count
            (5,),  # duplicate count
            (0,),  # gate: duplicate hashes
            (0,),  # gate: orphaned docs
        ]

        summary = remediation.run_remediation("postgresql://test", dry_run=False)

        assert summary["orphaned_docs_found"] == 10
        assert summary["orphaned_docs_deleted"] == 10  # rowcount
        assert summary["duplicate_rulings_found"] == 5
        assert summary["duplicate_rulings_deleted"] == 10  # rowcount
        # commit should be called twice (once for orphans, once for duplicates)
        assert conn.commit.call_count == 2
        assert summary["all_gates_passed"] is True

    @patch("riverside_remediation.psycopg")
    def test_no_changes_when_nothing_to_clean(self, mock_psycopg: MagicMock) -> None:
        """When there are no orphans or duplicates, no DELETEs are executed."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = conn

        cursor.fetchall.side_effect = [
            [("rulings", 100)],
            [("rulings", 100)],
        ]
        cursor.fetchone.side_effect = [
            (0,),  # orphan count = 0
            (0,),  # duplicate count = 0
            (0,),  # gate: duplicate hashes
            (0,),  # gate: orphaned docs
        ]

        summary = remediation.run_remediation("postgresql://test", dry_run=False)

        assert summary["orphaned_docs_deleted"] == 0
        assert summary["duplicate_rulings_deleted"] == 0
        conn.commit.assert_not_called()
        assert summary["all_gates_passed"] is True

    @patch("riverside_remediation.psycopg")
    def test_gate_failure_reported(self, mock_psycopg: MagicMock) -> None:
        """When gates fail, all_gates_passed should be False."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = conn

        cursor.fetchall.side_effect = [
            [("rulings", 100)],
            [("rulings", 100)],
        ]
        cursor.fetchone.side_effect = [
            (0,),  # orphan count
            (0,),  # duplicate count
            (3,),  # gate: duplicate hashes = 3 (FAIL)
            (0,),  # gate: orphaned docs
        ]

        summary = remediation.run_remediation("postgresql://test", dry_run=False)

        assert summary["all_gates_passed"] is False
        assert summary["gates"][0]["passed"] is False
        assert summary["gates"][0]["value"] == 3


# ---------------------------------------------------------------------------
# SQL query content tests
# ---------------------------------------------------------------------------


class TestSqlQueryContent:
    """Verify the SQL queries target the right tables and conditions."""

    def test_snapshot_query_covers_all_tables(self) -> None:
        sql = remediation.SNAPSHOT_QUERY
        assert "rulings" in sql
        assert "documents" in sql
        assert "cases" in sql
        assert "unique_s3_keys" in sql
        assert "Riverside" in sql

    def test_orphan_query_uses_left_join(self) -> None:
        sql = remediation.DELETE_ORPHANED_DOCS_QUERY
        assert "LEFT JOIN rulings" in sql
        assert "r.id IS NULL" in sql

    def test_duplicate_query_keeps_longest(self) -> None:
        sql = remediation.DELETE_DUPLICATE_RULINGS_QUERY
        assert "DISTINCT ON (ruling_text_hash)" in sql
        assert "LENGTH(r3.ruling_text) DESC" in sql
        assert "HAVING COUNT(*) > 1" in sql

    def test_gate_queries_check_riverside(self) -> None:
        assert "Riverside" in remediation.GATE_DUPLICATE_HASHES_QUERY
        assert "Riverside" in remediation.GATE_ORPHANED_DOCS_QUERY
