"""Tests for ralph_checkpoint module.

Tests cover comment formatting, checkpoint detection, plan extraction,
and CLI argument parsing.  All tests are offline — ``gh`` calls are mocked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add scripts directory to path so we can import the module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ralph_checkpoint import (
    MARKER_PLAN_APPROVED,
    MARKER_SHIP,
    build_parser,
    extract_plan_from_comment,
    find_checkpoint_comment,
    format_plan_approved_comment,
    format_ship_comment,
)


# ---------------------------------------------------------------------------
# Comment formatting tests
# ---------------------------------------------------------------------------


class TestFormatPlanApprovedComment:
    """Tests for format_plan_approved_comment."""

    def test_contains_marker(self) -> None:
        body = format_plan_approved_comment("My plan", iterations=2)
        assert MARKER_PLAN_APPROVED in body

    def test_contains_plan_content(self) -> None:
        body = format_plan_approved_comment("Step 1: Do the thing", iterations=3)
        assert "Step 1: Do the thing" in body

    def test_contains_iteration_count(self) -> None:
        body = format_plan_approved_comment("plan", iterations=4)
        assert "4 iterations" in body

    def test_singular_iteration(self) -> None:
        body = format_plan_approved_comment("plan", iterations=1)
        assert "1 iteration)" in body

    def test_has_details_block(self) -> None:
        body = format_plan_approved_comment("plan", iterations=2)
        assert "<details>" in body
        assert "</details>" in body
        assert "<summary>" in body
        assert "</summary>" in body

    def test_has_reviewer_line(self) -> None:
        body = format_plan_approved_comment("plan", iterations=2)
        assert "**Reviewers:**" in body


class TestFormatShipComment:
    """Tests for format_ship_comment."""

    @patch("ralph_checkpoint._git_shortstat", return_value="3 files changed, +50, -10")
    @patch("ralph_checkpoint._git_diff_stat", return_value="src/foo.py | 20 +++")
    @patch("ralph_checkpoint._count_passing_tests", return_value="42 passing")
    def test_contains_marker(
        self,
        _mock_tests: MagicMock,
        _mock_diff: MagicMock,
        _mock_stat: MagicMock,
    ) -> None:
        body = format_ship_comment("worker-3/session-123", "/tmp/wt")
        assert MARKER_SHIP in body

    @patch("ralph_checkpoint._git_shortstat", return_value="5 files changed, +234, -42")
    @patch("ralph_checkpoint._git_diff_stat", return_value="src/foo.py | 20 +++")
    @patch("ralph_checkpoint._count_passing_tests", return_value="48 passing")
    def test_contains_branch(
        self,
        _mock_tests: MagicMock,
        _mock_diff: MagicMock,
        _mock_stat: MagicMock,
    ) -> None:
        body = format_ship_comment("worker-3/session-20260312", "/tmp/wt")
        assert "worker-3/session-20260312" in body

    @patch("ralph_checkpoint._git_shortstat", return_value="5 files changed, +234, -42")
    @patch("ralph_checkpoint._git_diff_stat", return_value="src/foo.py | 20 +++")
    @patch("ralph_checkpoint._count_passing_tests", return_value="48 passing")
    def test_contains_diff_stat(
        self,
        _mock_tests: MagicMock,
        _mock_diff: MagicMock,
        _mock_stat: MagicMock,
    ) -> None:
        body = format_ship_comment("branch", "/tmp/wt")
        assert "src/foo.py | 20 +++" in body

    @patch("ralph_checkpoint._git_shortstat", return_value="5 files changed, +234, -42")
    @patch("ralph_checkpoint._git_diff_stat", return_value="src/foo.py | 20 +++")
    @patch("ralph_checkpoint._count_passing_tests", return_value="48 passing")
    def test_contains_test_count(
        self,
        _mock_tests: MagicMock,
        _mock_diff: MagicMock,
        _mock_stat: MagicMock,
    ) -> None:
        body = format_ship_comment("branch", "/tmp/wt")
        assert "48 passing" in body

    @patch("ralph_checkpoint._git_shortstat", return_value="3 files changed")
    @patch("ralph_checkpoint._git_diff_stat", return_value="file.py | 5 ++")
    @patch("ralph_checkpoint._count_passing_tests", return_value="10 passing")
    def test_has_details_block(
        self,
        _mock_tests: MagicMock,
        _mock_diff: MagicMock,
        _mock_stat: MagicMock,
    ) -> None:
        body = format_ship_comment("branch", "/tmp/wt")
        assert "<details>" in body
        assert "</details>" in body


# ---------------------------------------------------------------------------
# Plan extraction tests
# ---------------------------------------------------------------------------


class TestExtractPlanFromComment:
    """Tests for extract_plan_from_comment."""

    def test_extracts_plan_content(self) -> None:
        body = format_plan_approved_comment(
            "## Step 1\nDo things\n\n## Step 2\nDo more",
            iterations=2,
        )
        plan = extract_plan_from_comment(body)
        assert "Step 1" in plan
        assert "Step 2" in plan

    def test_strips_whitespace(self) -> None:
        body = format_plan_approved_comment("  trimmed  ", iterations=1)
        plan = extract_plan_from_comment(body)
        assert plan == "trimmed"

    def test_returns_empty_for_no_match(self) -> None:
        plan = extract_plan_from_comment("no details block here")
        assert plan == ""

    def test_handles_multiline_plan(self) -> None:
        plan_text = "Line 1\nLine 2\nLine 3\n\n- bullet 1\n- bullet 2"
        body = format_plan_approved_comment(plan_text, iterations=3)
        extracted = extract_plan_from_comment(body)
        assert "Line 1" in extracted
        assert "bullet 2" in extracted


# ---------------------------------------------------------------------------
# Checkpoint detection tests
# ---------------------------------------------------------------------------


class TestFindCheckpointComment:
    """Tests for find_checkpoint_comment."""

    @patch("ralph_checkpoint._fetch_issue_comments")
    def test_finds_plan_approved(
        self,
        mock_fetch: MagicMock,
    ) -> None:
        mock_fetch.return_value = [
            {"body": "Some other comment"},
            {"body": f"plan details\n{MARKER_PLAN_APPROVED}"},
        ]
        result = find_checkpoint_comment(42, "plan-approved")
        assert result is not None
        assert MARKER_PLAN_APPROVED in result

    @patch("ralph_checkpoint._fetch_issue_comments")
    def test_finds_ship(
        self,
        mock_fetch: MagicMock,
    ) -> None:
        mock_fetch.return_value = [
            {"body": f"ship details\n{MARKER_SHIP}"},
        ]
        result = find_checkpoint_comment(42, "ship")
        assert result is not None
        assert MARKER_SHIP in result

    @patch("ralph_checkpoint._fetch_issue_comments")
    def test_returns_none_when_not_found(
        self,
        mock_fetch: MagicMock,
    ) -> None:
        mock_fetch.return_value = [
            {"body": "Just a regular comment"},
        ]
        result = find_checkpoint_comment(42, "plan-approved")
        assert result is None

    @patch("ralph_checkpoint._fetch_issue_comments")
    def test_returns_none_for_empty_comments(
        self,
        mock_fetch: MagicMock,
    ) -> None:
        mock_fetch.return_value = []
        result = find_checkpoint_comment(42, "ship")
        assert result is None

    def test_unknown_type_exits(self) -> None:
        with pytest.raises(SystemExit):
            find_checkpoint_comment(42, "unknown-type")


# ---------------------------------------------------------------------------
# Git helper tests
# ---------------------------------------------------------------------------


class TestCountPassingTests:
    """Tests for _count_passing_tests."""

    def test_reads_from_test_output_file(self, tmp_path: Path) -> None:
        from ralph_checkpoint import _count_passing_tests

        ralph_dir = tmp_path / "tmp" / "ralph"
        ralph_dir.mkdir(parents=True)
        (ralph_dir / "test-output.txt").write_text(
            "PASSED 42 passed, 0 failed in 3.2s\n",
            encoding="utf-8",
        )
        result = _count_passing_tests(str(tmp_path))
        assert result == "42 passing"

    def test_returns_placeholder_when_no_file(self, tmp_path: Path) -> None:
        from ralph_checkpoint import _count_passing_tests

        result = _count_passing_tests(str(tmp_path))
        assert "not available" in result


# ---------------------------------------------------------------------------
# CLI parser tests
# ---------------------------------------------------------------------------


class TestBuildParser:
    """Tests for the CLI argument parser."""

    def test_plan_approved_args(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "plan-approved",
                "--issue",
                "42",
                "--plan-file",
                "/tmp/plan.md",
                "--iterations",
                "2",
            ]
        )
        assert args.issue == 42
        assert args.plan_file == "/tmp/plan.md"
        assert args.iterations == 2

    def test_ship_args(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "ship",
                "--issue",
                "42",
                "--branch",
                "worker-3/session-123",
                "--worktree",
                "/path/to/wt",
            ]
        )
        assert args.issue == 42
        assert args.branch == "worker-3/session-123"
        assert args.worktree == "/path/to/wt"

    def test_check_args(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["check", "plan-approved", "--issue", "42"])
        assert args.checkpoint_type == "plan-approved"
        assert args.issue == 42

    def test_read_args(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["read", "ship", "--issue", "42"])
        assert args.checkpoint_type == "ship"
        assert args.issue == 42

    def test_check_rejects_invalid_type(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["check", "invalid-type", "--issue", "42"])

    def test_missing_required_args(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["plan-approved"])


# ---------------------------------------------------------------------------
# Integration-style: roundtrip format -> extract
# ---------------------------------------------------------------------------


class TestRoundtrip:
    """Test that formatting and extracting are consistent."""

    def test_plan_roundtrip(self) -> None:
        original = (
            "## Architecture\n\nUse a factory pattern.\n\n## Tests\n\n5 unit tests."
        )
        body = format_plan_approved_comment(original, iterations=2)
        extracted = extract_plan_from_comment(body)
        assert "Architecture" in extracted
        assert "factory pattern" in extracted
        assert "5 unit tests" in extracted

    def test_plan_roundtrip_preserves_content(self) -> None:
        original = "Simple one-liner plan."
        body = format_plan_approved_comment(original, iterations=1)
        extracted = extract_plan_from_comment(body)
        assert extracted == original
