"""Unit tests for issue #3053 — ``_gh_issue_is_open`` retries transient ``gh`` failures.

Coverage:
  - ``_gh_issue_is_open`` now retries on ``TimeoutExpired`` / non-zero
    exit / JSON parse failure up to 3 total attempts with 1s + 2s
    backoff, emitting a WARNING per failed attempt.
  - A single transient failure followed by success still returns True
    without false-negativing the call.
  - All three attempts failing returns False *and* emits a final
    ``gh_issue_is_open_exhausted`` WARNING so the flake is visible
    in CloudWatch.
  - ``_gh_issue_is_open_with_detail`` returns ``(is_open, detail)``
    with ``attempts`` / ``reason`` / ``stderr_tail`` so
    ``_consume_action_block_on_existing_task`` can emit a structured
    fallback WARNING.
  - ``GH_POLL_SUBPROCESS_TIMEOUT_SECONDS`` is bumped to 30s (AC#2).

Fakes follow the pattern from ``test_daemon_diagnoser_routing.py``.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# Provide a stub ``psycopg`` module before importing the daemon.
if "psycopg" not in sys.modules or not isinstance(
    getattr(sys.modules["psycopg"].errors, "UniqueViolation", None), type
):

    class _UniqueViolation(Exception):
        """Test sentinel — stands in for real psycopg.errors.UniqueViolation."""

    _psycopg_stub = MagicMock()
    _psycopg_errors = MagicMock()
    _psycopg_errors.UniqueViolation = _UniqueViolation
    _psycopg_stub.errors = _psycopg_errors
    sys.modules["psycopg"] = _psycopg_stub

from dispatcher import daemon  # noqa: E402


# --------------------------------------------------------------------------
# Shared fakes — mirror the pattern from test_daemon_diagnoser_routing.py.
# --------------------------------------------------------------------------


def _make_daemon() -> daemon.DispatcherDaemon:
    """Construct a DispatcherDaemon bypassing __init__ (no DB, no config)."""
    import threading

    d = daemon.DispatcherDaemon.__new__(daemon.DispatcherDaemon)
    d._thread_state = threading.local()  # type: ignore[attr-defined]
    d._main_conn = MagicMock()  # type: ignore[attr-defined]
    d._cfg = MagicMock(github_repo="judgemind/judgemind")  # type: ignore[attr-defined]
    d._log = logging.getLogger("test.daemon_gh_retry")  # type: ignore[attr-defined]
    d._run_id = "test-run"  # type: ignore[attr-defined]
    return d


def _completed_process(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["gh", "issue", "view"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# --------------------------------------------------------------------------
# AC#2 — GH_POLL_SUBPROCESS_TIMEOUT_SECONDS bumped to 30.
# --------------------------------------------------------------------------


class TestPollSubprocessTimeout:
    def test_timeout_is_30_seconds(self) -> None:
        """Issue #3053 — bumped from 15 → 30 per issue body ``AC#2``."""
        assert daemon.GH_POLL_SUBPROCESS_TIMEOUT_SECONDS == 30


# --------------------------------------------------------------------------
# AC#1 — _gh_issue_is_open retries on timeout / non-zero exit.
# --------------------------------------------------------------------------


class TestGhIssueIsOpenRetry:
    def test_first_attempt_success_returns_true(self) -> None:
        """Happy path — no retry needed when the first probe succeeds."""
        d = _make_daemon()
        ok = _completed_process(returncode=0, stdout='{"state":"OPEN"}')
        with (
            patch("dispatcher.daemon.subprocess.run", return_value=ok) as mock_run,
            patch("dispatcher.daemon.time.sleep") as mock_sleep,
        ):
            assert d._gh_issue_is_open(3050) is True
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    def test_first_attempt_success_closed_returns_false(self) -> None:
        """``state=CLOSED`` reports False without any retry."""
        d = _make_daemon()
        ok = _completed_process(returncode=0, stdout='{"state":"CLOSED"}')
        with (
            patch("dispatcher.daemon.subprocess.run", return_value=ok) as mock_run,
            patch("dispatcher.daemon.time.sleep") as mock_sleep,
        ):
            assert d._gh_issue_is_open(3050) is False
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    def test_timeout_twice_then_success_returns_true(self, caplog: Any) -> None:
        """Issue #3053 AC#1 — two transient timeouts then success returns True."""
        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_gh_retry")
        ok = _completed_process(returncode=0, stdout='{"state":"OPEN"}')
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=[
                    subprocess.TimeoutExpired(cmd="gh", timeout=30),
                    subprocess.TimeoutExpired(cmd="gh", timeout=30),
                    ok,
                ],
            ) as mock_run,
            patch("dispatcher.daemon.time.sleep") as mock_sleep,
        ):
            assert d._gh_issue_is_open(3050) is True
        assert mock_run.call_count == 3
        # Two backoffs between the three attempts — 1s and 2s.
        assert mock_sleep.call_args_list[0].args[0] == 1.0
        assert mock_sleep.call_args_list[1].args[0] == 2.0
        # Two flake WARNINGs emitted (one per failed attempt).
        flake_records = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "gh_issue_is_open_flake"
        ]
        assert len(flake_records) == 2
        assert flake_records[0].attempt == 1
        assert flake_records[1].attempt == 2
        assert flake_records[0].reason == "timeout"
        # No "exhausted" record on success.
        exhausted = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "gh_issue_is_open_exhausted"
        ]
        assert exhausted == []

    def test_all_three_attempts_timeout_returns_false_and_warns(
        self, caplog: Any
    ) -> None:
        """Issue #3053 AC#1 — three timeouts exhausts, returns False + final WARNING."""
        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_gh_retry")
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
            ) as mock_run,
            patch("dispatcher.daemon.time.sleep") as mock_sleep,
        ):
            assert d._gh_issue_is_open(3050) is False
        assert mock_run.call_count == 3
        # Two backoffs between the three attempts.
        assert mock_sleep.call_count == 2
        flake_records = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "gh_issue_is_open_flake"
        ]
        # One WARNING per failed attempt.
        assert len(flake_records) == 3
        exhausted = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "gh_issue_is_open_exhausted"
        ]
        # Plus a final "exhausted" WARNING.
        assert len(exhausted) == 1
        assert exhausted[0].attempts == 3
        assert exhausted[0].reason == "timeout"
        assert exhausted[0].issue_number == 3050

    def test_nonzero_exit_retries_then_returns_false(self, caplog: Any) -> None:
        """Non-zero exit counts as a transient failure (AC#1)."""
        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_gh_retry")
        bad = _completed_process(returncode=1, stdout="", stderr="HTTP 502 Bad Gateway")
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                return_value=bad,
            ) as mock_run,
            patch("dispatcher.daemon.time.sleep") as mock_sleep,
        ):
            assert d._gh_issue_is_open(3050) is False
        # Three attempts, two sleeps.
        assert mock_run.call_count == 3
        assert mock_sleep.call_count == 2
        flake_records = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "gh_issue_is_open_flake"
        ]
        assert len(flake_records) == 3
        assert all(r.reason == "nonzero_exit" for r in flake_records)
        # stderr_tail is propagated for operator visibility.
        assert any("502 Bad Gateway" in (r.stderr_tail or "") for r in flake_records)

    def test_json_parse_error_retries_then_returns_false(self, caplog: Any) -> None:
        """Malformed ``gh`` stdout is treated as transient (retry + fail)."""
        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_gh_retry")
        garbage = _completed_process(returncode=0, stdout="not json at all")
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                return_value=garbage,
            ) as mock_run,
            patch("dispatcher.daemon.time.sleep"),
        ):
            assert d._gh_issue_is_open(3050) is False
        assert mock_run.call_count == 3
        flake_records = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "gh_issue_is_open_flake"
        ]
        assert all(r.reason == "json_parse" for r in flake_records)

    def test_gh_missing_returns_false_without_retry_sleep_waste(
        self, caplog: Any
    ) -> None:
        """``FileNotFoundError`` (gh CLI not installed) still retries to
        keep the code path consistent — the retry cost is bounded at
        2 × 1s / 2s sleeps worst case, which is negligible. This
        asserts the control flow is the same as other transient
        failures (no early-return special case).
        """
        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_gh_retry")
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=FileNotFoundError("gh not installed"),
            ) as mock_run,
            patch("dispatcher.daemon.time.sleep"),
        ):
            assert d._gh_issue_is_open(3050) is False
        assert mock_run.call_count == 3
        flake_records = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "gh_issue_is_open_flake"
        ]
        assert all(r.reason == "gh_missing" for r in flake_records)


# --------------------------------------------------------------------------
# Detail variant — surface attempt count + reason to the caller.
# --------------------------------------------------------------------------


class TestGhIssueIsOpenWithDetail:
    def test_first_try_success_reports_attempts_1(self) -> None:
        d = _make_daemon()
        ok = _completed_process(returncode=0, stdout='{"state":"OPEN"}')
        with (
            patch("dispatcher.daemon.subprocess.run", return_value=ok),
            patch("dispatcher.daemon.time.sleep"),
        ):
            is_open, detail = d._gh_issue_is_open_with_detail(3050)
        assert is_open is True
        assert detail == {"attempts": 1, "reason": None, "stderr_tail": ""}

    def test_success_on_third_attempt_reports_attempts_3(self) -> None:
        d = _make_daemon()
        ok = _completed_process(returncode=0, stdout='{"state":"OPEN"}')
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=[
                    subprocess.TimeoutExpired(cmd="gh", timeout=30),
                    subprocess.TimeoutExpired(cmd="gh", timeout=30),
                    ok,
                ],
            ),
            patch("dispatcher.daemon.time.sleep"),
        ):
            is_open, detail = d._gh_issue_is_open_with_detail(3050)
        assert is_open is True
        assert detail["attempts"] == 3
        assert detail["reason"] is None

    def test_all_timeouts_reports_reason_timeout(self) -> None:
        d = _make_daemon()
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
            ),
            patch("dispatcher.daemon.time.sleep"),
        ):
            is_open, detail = d._gh_issue_is_open_with_detail(3050)
        assert is_open is False
        assert detail["attempts"] == 3
        assert detail["reason"] == "timeout"

    def test_nonzero_exit_reports_stderr_tail(self) -> None:
        d = _make_daemon()
        bad = _completed_process(returncode=1, stdout="", stderr="HTTP 502 Bad Gateway")
        with (
            patch("dispatcher.daemon.subprocess.run", return_value=bad),
            patch("dispatcher.daemon.time.sleep"),
        ):
            is_open, detail = d._gh_issue_is_open_with_detail(3050)
        assert is_open is False
        assert detail["reason"] == "nonzero_exit"
        assert "502 Bad Gateway" in detail["stderr_tail"]
