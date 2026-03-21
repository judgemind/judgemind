"""Tests for the dedup_split_rulings script.

Tests cover:
  - find_duplicate_groups() finding and grouping duplicates
  - find_duplicate_groups() returning empty when no duplicates exist
  - apply_dedup() deleting correct rulings and cleaning orphaned documents
  - run_dedup() dry-run mode not committing
  - run_dedup() apply mode committing
  - Department filtering
  - Backfilling NULL ruling_text_hash values
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# The script lives in scripts/ — add it to the path for import.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# Import the script module via importlib (it's not a package module).
_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "dedup_split_rulings.py"
)
_spec = importlib.util.spec_from_file_location("dedup_split_rulings", _SCRIPT_PATH)
assert _spec is not None
assert _spec.loader is not None
dedup_mod = importlib.util.module_from_spec(_spec)
sys.modules["dedup_split_rulings"] = dedup_mod
_spec.loader.exec_module(dedup_mod)

# Pull in the names we need.
DuplicateGroup = dedup_mod.DuplicateGroup
find_duplicate_groups = dedup_mod.find_duplicate_groups
apply_dedup = dedup_mod.apply_dedup
_backfill_null_hashes = dedup_mod._backfill_null_hashes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_conn_with_queries(query_results: dict[str, list[Any]]) -> MagicMock:
    """Create a mock connection that returns different results based on query content.

    query_results maps a substring of the SQL query to the rows to return.
    """
    conn = MagicMock()
    mock_cursor = MagicMock()

    def execute_side_effect(query: str, params: Any = None) -> None:
        mock_cursor._last_query = query

    def fetchall_side_effect() -> list[Any]:
        query = mock_cursor._last_query
        for key, result in query_results.items():
            if key in query:
                return result
        return []

    mock_cursor.execute.side_effect = execute_side_effect
    mock_cursor.fetchall.side_effect = fetchall_side_effect
    mock_cursor._last_query = ""
    conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
# DuplicateGroup tests
# ---------------------------------------------------------------------------


class TestDuplicateGroup:
    def test_dataclass_construction(self) -> None:
        group = DuplicateGroup(
            case_id="case-1",
            ruling_text_hash="abc123",
            count=3,
            kept_ruling_id="ruling-1",
            deleted_ruling_ids=["ruling-2", "ruling-3"],
            deleted_document_ids=["doc-2", "doc-3"],
        )
        assert group.count == 3
        assert len(group.deleted_ruling_ids) == 2


# ---------------------------------------------------------------------------
# find_duplicate_groups tests
# ---------------------------------------------------------------------------


class TestFindDuplicateGroups:
    @patch("dedup_split_rulings._backfill_null_hashes")
    def test_no_duplicates(self, mock_backfill: MagicMock) -> None:
        """When no duplicates exist, returns empty list."""
        conn = _mock_conn_with_queries({"GROUP BY": []})
        groups = find_duplicate_groups(conn)
        assert groups == []

    @patch("dedup_split_rulings._backfill_null_hashes")
    def test_finds_duplicate_group(self, mock_backfill: MagicMock) -> None:
        """Finds a group of duplicates and determines which to keep/delete."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        call_count = 0

        def fetchall_side_effect() -> list[Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # FIND_DUPLICATES_QUERY result (_backfill_null_hashes is patched)
                return [("case-id-1", "hash-abc", 2)]
            if call_count == 2:
                # GET_DUPLICATE_DETAILS_QUERY result — earliest first
                return [
                    ("ruling-1", "doc-1", "2026-03-01 10:00:00"),
                    ("ruling-2", "doc-2", "2026-03-02 10:00:00"),
                ]
            return []

        mock_cursor.fetchall.side_effect = fetchall_side_effect

        groups = find_duplicate_groups(conn)

        assert len(groups) == 1
        group = groups[0]
        assert group.case_id == "case-id-1"
        assert group.ruling_text_hash == "hash-abc"
        assert group.kept_ruling_id == "ruling-1"
        assert group.deleted_ruling_ids == ["ruling-2"]
        assert group.deleted_document_ids == ["doc-2"]

    @patch("dedup_split_rulings._backfill_null_hashes")
    def test_department_filter(self, mock_backfill: MagicMock) -> None:
        """Department filter is passed to the query."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = []

        find_duplicate_groups(conn, departments=["N15", "N16"])

        # Verify the department parameters were passed.
        execute_calls = mock_cursor.execute.call_args_list
        # The first execute call should have departments as params
        first_call_params = execute_calls[0][0][1] if len(execute_calls[0][0]) > 1 else []
        assert "N15" in first_call_params
        assert "N16" in first_call_params


# ---------------------------------------------------------------------------
# apply_dedup tests
# ---------------------------------------------------------------------------


class TestApplyDedup:
    def test_deletes_correct_rulings(self) -> None:
        """Deletes the duplicate rulings but keeps the earliest."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 0  # No orphaned documents
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        groups = [
            DuplicateGroup(
                case_id="case-1",
                ruling_text_hash="hash-abc",
                count=3,
                kept_ruling_id="ruling-1",
                deleted_ruling_ids=["ruling-2", "ruling-3"],
                deleted_document_ids=["doc-2", "doc-3"],
            ),
        ]

        stats = apply_dedup(conn, groups)

        assert stats["deleted_rulings"] == 2
        # Verify DELETE was called for ruling-2 and ruling-3
        delete_calls = [
            c for c in mock_cursor.execute.call_args_list if "DELETE FROM rulings" in str(c)
        ]
        assert len(delete_calls) == 2

    def test_cleans_orphaned_documents(self) -> None:
        """Deletes document records that have no remaining rulings."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1  # Document was orphaned and deleted
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        groups = [
            DuplicateGroup(
                case_id="case-1",
                ruling_text_hash="hash-abc",
                count=2,
                kept_ruling_id="ruling-1",
                deleted_ruling_ids=["ruling-2"],
                deleted_document_ids=["doc-2"],
            ),
        ]

        stats = apply_dedup(conn, groups)

        assert stats["cleaned_documents"] == 1

    def test_empty_groups(self) -> None:
        """No groups means no work."""
        conn = MagicMock()
        stats = apply_dedup(conn, [])
        assert stats["deleted_rulings"] == 0
        assert stats["cleaned_documents"] == 0


# ---------------------------------------------------------------------------
# _backfill_null_hashes tests
# ---------------------------------------------------------------------------


class TestBackfillNullHashes:
    def test_updates_null_hashes(self) -> None:
        """Computes and sets ruling_text_hash for rulings where it is NULL."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # Return one ruling with NULL hash.
        mock_cursor.fetchall.return_value = [
            ("ruling-id-1", "Some ruling text for testing"),
        ]

        count = _backfill_null_hashes(conn)

        assert count == 1
        # Verify UPDATE was called.
        update_calls = [c for c in mock_cursor.execute.call_args_list if "UPDATE" in str(c)]
        assert len(update_calls) == 1

    def test_skips_empty_text(self) -> None:
        """Rulings with empty text after normalization are skipped."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # Return a ruling with empty text.
        mock_cursor.fetchall.return_value = [
            ("ruling-id-1", "   "),
        ]

        count = _backfill_null_hashes(conn)

        assert count == 0


# ---------------------------------------------------------------------------
# run_dedup integration tests (mocked DB)
# ---------------------------------------------------------------------------


class TestRunDedup:
    @patch("dedup_split_rulings.find_duplicate_groups")
    @patch("psycopg.connect")
    def test_dry_run_no_duplicates(
        self,
        mock_connect: MagicMock,
        mock_find: MagicMock,
    ) -> None:
        """Dry-run with no duplicates returns zeros."""
        mock_find.return_value = []
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        stats = dedup_mod.run_dedup("postgresql://test", dry_run=True)

        assert stats["duplicate_groups"] == 0
        assert stats["total_duplicates"] == 0
        mock_conn.rollback.assert_called()

    @patch("dedup_split_rulings.apply_dedup")
    @patch("dedup_split_rulings.find_duplicate_groups")
    @patch("psycopg.connect")
    def test_dry_run_does_not_apply(
        self,
        mock_connect: MagicMock,
        mock_find: MagicMock,
        mock_apply: MagicMock,
    ) -> None:
        """Dry-run mode does not call apply_dedup."""
        mock_find.return_value = [
            DuplicateGroup(
                case_id="case-1",
                ruling_text_hash="hash-abc",
                count=2,
                kept_ruling_id="r1",
                deleted_ruling_ids=["r2"],
                deleted_document_ids=["d2"],
            ),
        ]
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        stats = dedup_mod.run_dedup("postgresql://test", dry_run=True)

        assert stats["duplicate_groups"] == 1
        assert stats["total_duplicates"] == 1
        mock_apply.assert_not_called()
        mock_conn.rollback.assert_called()

    @patch("dedup_split_rulings.apply_dedup")
    @patch("dedup_split_rulings.find_duplicate_groups")
    @patch("psycopg.connect")
    def test_apply_mode_commits(
        self,
        mock_connect: MagicMock,
        mock_find: MagicMock,
        mock_apply: MagicMock,
    ) -> None:
        """Apply mode calls apply_dedup and commits."""
        mock_find.return_value = [
            DuplicateGroup(
                case_id="case-1",
                ruling_text_hash="hash-abc",
                count=2,
                kept_ruling_id="r1",
                deleted_ruling_ids=["r2"],
                deleted_document_ids=["d2"],
            ),
        ]
        mock_apply.return_value = {"deleted_rulings": 1, "cleaned_documents": 0}
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        stats = dedup_mod.run_dedup("postgresql://test", dry_run=False)

        assert stats["deleted_rulings"] == 1
        mock_apply.assert_called_once()
        mock_conn.commit.assert_called()
