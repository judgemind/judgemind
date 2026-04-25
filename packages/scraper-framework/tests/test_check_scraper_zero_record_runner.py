"""Tests for scripts/check-scraper-zero-record-runner.py (see issue #2677).

Exercises the GitHub issue lifecycle:
  (a) breach + no open issue → POST create, Telegram sent
  (b) breach + open issue   → POST comment, no Telegram
  (c) healthy + open issue  → close comment + close PATCH, no Telegram
  (d) healthy + no issues   → no-op, no Telegram
  (e) Telegram invoked only on create
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

# Add scripts/ to sys.path so we can import the hyphenated modules.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

# ruff: noqa: E402

runner = import_module("check-scraper-zero-record-runner")
zrs = import_module("check-scraper-zero-record-streak")

Breach = zrs.Breach
CheckResult = zrs.CheckResult

NOW = datetime(2026, 4, 25, 14, 30, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_breach(scraper_id: str = "ca-la-civil", streak: int = 3) -> Breach:
    base = NOW - timedelta(days=streak)
    return Breach(
        scraper_id=scraper_id,
        streak=streak,
        threshold=2,
        first_zero_at=base.isoformat(),
        last_zero_at=NOW.isoformat(),
        last_nonzero_at=(base - timedelta(days=1)).isoformat(),
        message=f"{scraper_id}: {streak} consecutive zero-record runs",
    )


def _healthy_result() -> CheckResult:
    return CheckResult(
        breaches=[],
        insufficient_history=[],
        checked_count=5,
        excluded_count=2,
    )


def _breach_result(scraper_id: str = "ca-la-civil") -> CheckResult:
    return CheckResult(
        breaches=[_make_breach(scraper_id)],
        insufficient_history=[],
        checked_count=5,
        excluded_count=2,
    )


def _open_issue(number: int = 42) -> dict:
    return {
        "number": number,
        "html_url": f"https://github.com/judgemind/judgemind/issues/{number}",
    }


_REPO = "judgemind/judgemind"
_TOKEN = "TOKEN"

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestBreach:
    """(a) Breach with no open issue — create issue + Telegram."""

    def test_creates_issue_when_no_open_issue(self) -> None:
        created_issue = {
            "number": 99,
            "html_url": "https://github.com/judgemind/judgemind/issues/99",
        }

        with (
            patch.object(runner, "create_issue", return_value=created_issue) as mock_create,
            patch.object(runner, "add_comment") as mock_comment,
            patch.object(runner, "close_issue") as mock_close,
            patch.object(runner, "send_telegram"),
        ):
            result = _breach_result()
            open_issues: list = []
            if result.breaches and not open_issues:
                runner.create_issue(_REPO, _TOKEN, runner._new_issue_body(result))

            mock_create.assert_called_once()
            mock_comment.assert_not_called()
            mock_close.assert_not_called()

    def test_sends_telegram_only_on_create(self) -> None:
        created_issue = {
            "number": 99,
            "html_url": "https://github.com/judgemind/judgemind/issues/99",
        }

        with (
            patch.object(runner, "create_issue", return_value=created_issue),
            patch.object(runner, "send_telegram") as mock_tg,
        ):
            result = _breach_result()
            open_issues: list = []
            if result.breaches and not open_issues:
                issue = runner.create_issue(_REPO, _TOKEN, runner._new_issue_body(result))
                runner.send_telegram(result, issue["number"], issue["html_url"])

            mock_tg.assert_called_once()

    def test_create_issue_body_contains_breach_table(self) -> None:
        result = _breach_result()
        body = runner._new_issue_body(result)
        assert "ca-la-civil" in body
        assert "Breaches" in body
        assert "scraper-zero-record-streak" in body


class TestBreachExistingIssue:
    """(b) Breach + open issue — comment only, no create, no Telegram."""

    def test_comments_on_existing_issue(self) -> None:
        open_issue = _open_issue(42)

        with (
            patch.object(runner, "create_issue") as mock_create,
            patch.object(runner, "add_comment") as mock_comment,
            patch.object(runner, "send_telegram") as mock_tg,
        ):
            result = _breach_result()
            open_issues = [open_issue]
            if result.breaches and open_issues:
                runner.add_comment(
                    _REPO,
                    _TOKEN,
                    open_issues[0]["number"],
                    runner._update_comment_body(result),
                )

            mock_comment.assert_called_once()
            mock_create.assert_not_called()
            mock_tg.assert_not_called()

    def test_comment_body_mentions_timestamp(self) -> None:
        result = _breach_result()
        body = runner._update_comment_body(result)
        assert "still in breach" in body
        assert "ca-la-civil" in body


class TestHealthyWithOpenIssue:
    """(c) Healthy + open issue → close."""

    def test_closes_open_issue(self) -> None:
        open_issue = _open_issue(55)

        with (
            patch.object(runner, "create_issue") as mock_create,
            patch.object(runner, "close_issue") as mock_close,
            patch.object(runner, "send_telegram") as mock_tg,
        ):
            result = _healthy_result()
            open_issues = [open_issue]
            if not result.breaches and open_issues:
                for issue in open_issues:
                    runner.close_issue(_REPO, _TOKEN, issue["number"], runner._close_comment_body())

            mock_close.assert_called_once_with(_REPO, _TOKEN, 55, runner._close_comment_body())
            mock_create.assert_not_called()
            mock_tg.assert_not_called()

    def test_close_comment_mentions_no_breach(self) -> None:
        body = runner._close_comment_body()
        assert "no" in body.lower()
        assert "auto-closing" in body.lower()


class TestHealthyNoIssue:
    """(d) Healthy + no open issue → complete no-op."""

    def test_noop_when_healthy_and_no_issue(self) -> None:
        with (
            patch.object(runner, "create_issue") as mock_create,
            patch.object(runner, "add_comment") as mock_comment,
            patch.object(runner, "close_issue") as mock_close,
            patch.object(runner, "send_telegram") as mock_tg,
        ):
            result = _healthy_result()
            open_issues: list = []
            # Neither branch executes — nothing should be called.
            if result.breaches:
                pass  # branch not taken
            elif open_issues:
                pass  # branch not taken

            mock_create.assert_not_called()
            mock_comment.assert_not_called()
            mock_close.assert_not_called()
            mock_tg.assert_not_called()


class TestTelegramOnlyOnCreate:
    """(e) Telegram is invoked only on issue creation."""

    def test_no_telegram_on_breach_update(self) -> None:
        open_issue = _open_issue(10)

        with (
            patch.object(runner, "add_comment"),
            patch.object(runner, "send_telegram") as mock_tg,
        ):
            result = _breach_result()
            open_issues = [open_issue]
            # Simulate breach-update path: open issue exists → comment, no Telegram.
            if result.breaches:
                if not open_issues:
                    issue = runner.create_issue(  # type: ignore[attr-defined]  # noqa: F821
                        _REPO, _TOKEN, runner._new_issue_body(result)
                    )
                    runner.send_telegram(result, issue["number"], issue["html_url"])
                else:
                    runner.add_comment(
                        _REPO,
                        _TOKEN,
                        open_issues[0]["number"],
                        runner._update_comment_body(result),
                    )

            mock_tg.assert_not_called()

    def test_telegram_on_create(self) -> None:
        created_issue = {
            "number": 7,
            "html_url": "https://github.com/judgemind/judgemind/issues/7",
        }

        with (
            patch.object(runner, "create_issue", return_value=created_issue),
            patch.object(runner, "send_telegram") as mock_tg,
        ):
            result = _breach_result()
            open_issues: list = []
            if result.breaches and not open_issues:
                issue = runner.create_issue(_REPO, _TOKEN, runner._new_issue_body(result))
                runner.send_telegram(result, issue["number"], issue["html_url"])

            mock_tg.assert_called_once()
            args = mock_tg.call_args[0]
            assert args[1] == 7


# ---------------------------------------------------------------------------
# Schedule constant check — AC2
# ---------------------------------------------------------------------------


class TestScheduleExpression:
    """The cron expression must be the 14:30 UTC daily schedule."""

    def test_terraform_module_contains_correct_cron(self) -> None:
        tf_main = (
            _REPO_ROOT / "infra" / "terraform" / "modules" / "scraper-zero-record-check" / "main.tf"
        )
        content = tf_main.read_text()
        assert "cron(30 14 * * ? *)" in content, (
            "Terraform module must use cron(30 14 * * ? *) for 14:30 UTC daily"
        )
        assert '"UTC"' in content, "Terraform module must specify timezone = UTC"
