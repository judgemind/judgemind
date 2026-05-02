"""Tests for the cleanup_test_telemetry_pollution script (#3806).

All database access is mocked — these tests verify cleanup logic, logging,
and rollback/commit behaviour without requiring a live database.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

_SCRIPTS_ONEOFF_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
    "oneoff",
)
sys.path.insert(0, _SCRIPTS_ONEOFF_DIR)
cleanup = importlib.import_module("cleanup_test_telemetry_pollution")


# ---------------------------------------------------------------------------
# Constant assertions
# ---------------------------------------------------------------------------


class TestSyntheticIdSet:
    """Verify the module-level constant matches the issue spec."""

    def test_synthetic_id_set_matches_issue(self) -> None:
        expected = {
            "test-stub",
            "good",
            "failing",
            "run-raiser",
            "scraper-b",
            "good-after",
            "good-after-ctor",
            "failing-ctor",
            "ctor-fail-db",
        }
        assert set(cleanup.SYNTHETIC_SCRAPER_IDS) == expected
        assert len(cleanup.SYNTHETIC_SCRAPER_IDS) == 9


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_conn(pre_rows: list[tuple], post_rows: list[tuple]) -> MagicMock:
    """Build a mock psycopg connection whose cursor returns pre/post rows."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()

    # fetchall: first call returns pre_rows, second returns post_rows
    mock_cur.fetchall.side_effect = [pre_rows, post_rows]

    mock_cur.__enter__ = MagicMock(return_value=mock_cur)
    mock_cur.__exit__ = MagicMock(return_value=False)

    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    return mock_conn


# ---------------------------------------------------------------------------
# Pre-count logging
# ---------------------------------------------------------------------------


class TestPreCountLogging:
    """Verify pre-count rows are each logged individually."""

    @patch("cleanup_test_telemetry_pollution.psycopg")
    def test_pre_count_logs_per_id_breakdown(self, mock_psycopg: MagicMock) -> None:
        pre_rows = [("test-stub", 100), ("good", 50)]
        mock_conn = _make_mock_conn(pre_rows=pre_rows, post_rows=[])
        mock_psycopg.connect.return_value = mock_conn

        with patch("cleanup_test_telemetry_pollution.logger") as mock_logger:
            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                cleanup.run_cleanup("postgresql://test")

        # Each (id, count) pair must appear in a logger.info call
        info_calls = [str(c) for c in mock_logger.info.call_args_list]
        assert any("test-stub" in c and "100" in c for c in info_calls)
        assert any("good" in c and "50" in c for c in info_calls)


# ---------------------------------------------------------------------------
# Happy path — post-count zero → commit
# ---------------------------------------------------------------------------


class TestDeleteThenPostCountAssertion:
    """Verify happy path: post-count zero causes commit."""

    @patch("cleanup_test_telemetry_pollution.psycopg")
    def test_delete_then_post_count_assertion(self, mock_psycopg: MagicMock) -> None:
        pre_rows = [("test-stub", 10), ("failing", 5)]
        mock_conn = _make_mock_conn(pre_rows=pre_rows, post_rows=[])
        mock_psycopg.connect.return_value = mock_conn

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
            stats = cleanup.run_cleanup("postgresql://test")

        assert stats["pre_total"] == 15
        assert stats["post_total"] == 0
        mock_conn.commit.assert_called_once()
        mock_conn.rollback.assert_not_called()


# ---------------------------------------------------------------------------
# Defensive — post-count non-zero → raise + rollback
# ---------------------------------------------------------------------------


class TestPostCountNonzeroRaisesAndRollsBack:
    """Verify that leftover rows after DELETE raise and trigger rollback."""

    @patch("cleanup_test_telemetry_pollution.psycopg")
    def test_post_count_nonzero_raises_and_rolls_back(self, mock_psycopg: MagicMock) -> None:
        pre_rows = [("test-stub", 10)]
        # Simulate rows still present after DELETE
        post_rows = [("test-stub", 3)]
        mock_conn = _make_mock_conn(pre_rows=pre_rows, post_rows=post_rows)
        mock_psycopg.connect.return_value = mock_conn

        import pytest

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
            with pytest.raises(RuntimeError, match="Post-delete count is 3"):
                cleanup.run_cleanup("postgresql://test")

        mock_conn.rollback.assert_called()
        mock_conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Dry run — rollback instead of commit
# ---------------------------------------------------------------------------


class TestDryRun:
    """Verify dry-run mode calls rollback and not commit."""

    @patch("cleanup_test_telemetry_pollution.psycopg")
    def test_dry_run_calls_rollback_not_commit(self, mock_psycopg: MagicMock) -> None:
        pre_rows = [("good", 200)]
        mock_conn = _make_mock_conn(pre_rows=pre_rows, post_rows=[])
        mock_psycopg.connect.return_value = mock_conn

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
            stats = cleanup.run_cleanup("postgresql://test", dry_run=True)

        assert stats["pre_total"] == 200
        mock_conn.rollback.assert_called_once()
        mock_conn.commit.assert_not_called()
