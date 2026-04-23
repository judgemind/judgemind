"""Unit tests for issue #3089 — shared ``_subprocess_with_retry`` helper + hot-path site retries.

Coverage:
  - ``_subprocess_with_retry`` is a general 3-attempt 1s+2s backoff
    retry envelope that mirrors the shape established by #3053
    (``_gh_issue_is_open_with_detail``) and #3085
    (``_baseline_fetch_origin_main``).
  - Every hot-path git/gh call site that gained retry under #3089
    survives a single transient failure without surfacing an error.
  - Happy-path first-attempt success stays silent (no new log events)
    so the common case does not flood CloudWatch.

Fakes mirror the pattern from ``test_daemon_gh_issue_is_open_retry.py``
(PR #3058) and ``test_daemon_baseline_fetch_retry.py`` (PR #3087).
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

from dispatcher import daemon  # noqa: E402


# --------------------------------------------------------------------------
# Shared fakes.
# --------------------------------------------------------------------------


def _make_daemon() -> daemon.DispatcherDaemon:
    """Construct a DispatcherDaemon bypassing __init__ (no DB, no config)."""
    import threading

    d = daemon.DispatcherDaemon.__new__(daemon.DispatcherDaemon)
    d._thread_state = threading.local()  # type: ignore[attr-defined]
    d._main_conn = MagicMock()  # type: ignore[attr-defined]
    d._cfg = MagicMock(github_repo="judgemind/judgemind")  # type: ignore[attr-defined]
    d._log = logging.getLogger("test.daemon_subprocess_retries")  # type: ignore[attr-defined]
    d._run_id = "test-run"  # type: ignore[attr-defined]
    return d


def _cp(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["fake"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# --------------------------------------------------------------------------
# Shared helper: _subprocess_with_retry
# --------------------------------------------------------------------------


class TestSubprocessWithRetry:
    def test_first_attempt_success_no_retry_silent(self) -> None:
        """Happy path: first attempt returncode=0 → ok=True, attempts=1, no warnings."""
        d = _make_daemon()
        with patch("dispatcher.daemon.subprocess.run", return_value=_cp(0, "hi")) as m:
            outcome = d._subprocess_with_retry(
                ["git", "status"], event_name="fake_site", timeout=30
            )
        assert outcome == {
            "ok": True,
            "result": outcome["result"],
            "attempts": 1,
        }
        assert outcome["result"].returncode == 0
        assert m.call_count == 1

    def test_happy_path_no_flake_warning(self, caplog: Any) -> None:
        """First-attempt success emits no ``*_flake`` / ``*_exhausted`` events."""
        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_subprocess_retries")
        with patch("dispatcher.daemon.subprocess.run", return_value=_cp(0, "ok")):
            d._subprocess_with_retry(["git"], event_name="probe", timeout=30)
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "probe_flake" not in events
        assert "probe_exhausted" not in events

    def test_timeout_then_success_retries_and_logs_flake(self, caplog: Any) -> None:
        """One transient timeout then success → ok=True after one retry,
        one flake WARNING, one backoff sleep."""
        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_subprocess_retries")
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=[
                    subprocess.TimeoutExpired(cmd="x", timeout=30),
                    _cp(0, "recovered"),
                ],
            ) as m_run,
            patch("dispatcher.daemon.time.sleep") as m_sleep,
        ):
            outcome = d._subprocess_with_retry(["git"], event_name="probe", timeout=30)
        assert outcome["ok"] is True
        assert outcome["attempts"] == 2
        assert m_run.call_count == 2
        assert m_sleep.call_args_list == [((1.0,), {})]
        flakes = [
            r for r in caplog.records if getattr(r, "event", None) == "probe_flake"
        ]
        assert len(flakes) == 1
        assert flakes[0].reason == "timeout"

    def test_nonzero_exit_then_success_returns_ok(self, caplog: Any) -> None:
        """One non-zero exit then success → ok=True, one flake with reason=nonzero_exit."""
        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_subprocess_retries")
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=[_cp(128, "", "HTTP 500"), _cp(0, "ok")],
            ) as m_run,
            patch("dispatcher.daemon.time.sleep"),
        ):
            outcome = d._subprocess_with_retry(["gh"], event_name="probe", timeout=30)
        assert outcome["ok"] is True
        assert outcome["attempts"] == 2
        assert m_run.call_count == 2
        flakes = [
            r for r in caplog.records if getattr(r, "event", None) == "probe_flake"
        ]
        assert len(flakes) == 1
        assert flakes[0].reason == "nonzero_exit"
        assert "HTTP 500" in (flakes[0].stderr_tail or "")

    def test_three_timeouts_exhausts_returns_not_ok(self, caplog: Any) -> None:
        """All 3 attempts timeout → ok=False, reason=timeout, attempts=3,
        one exhausted WARNING."""
        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_subprocess_retries")
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="x", timeout=30),
            ) as m_run,
            patch("dispatcher.daemon.time.sleep") as m_sleep,
        ):
            outcome = d._subprocess_with_retry(["gh"], event_name="probe", timeout=30)
        assert outcome["ok"] is False
        assert outcome["reason"] == "timeout"
        assert outcome["attempts"] == 3
        assert outcome["exit_code"] is None
        assert m_run.call_count == 3
        assert m_sleep.call_count == 2
        exh = [
            r for r in caplog.records if getattr(r, "event", None) == "probe_exhausted"
        ]
        assert len(exh) == 1
        assert exh[0].reason == "timeout"

    def test_three_nonzero_exits_preserves_exit_code(self, caplog: Any) -> None:
        """All 3 attempts non-zero → ok=False with exit_code surfaced."""
        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_subprocess_retries")
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                return_value=_cp(128, "", "RPC failed"),
            ),
            patch("dispatcher.daemon.time.sleep"),
        ):
            outcome = d._subprocess_with_retry(["git"], event_name="probe", timeout=30)
        assert outcome["ok"] is False
        assert outcome["reason"] == "nonzero_exit"
        assert outcome["exit_code"] == 128
        assert "RPC failed" in outcome["stderr_tail"]

    def test_file_not_found_retries_then_exhausts(self, caplog: Any) -> None:
        """``FileNotFoundError`` (CLI missing) is retried, then surfaces as
        ``reason=gh_missing`` on exhaustion."""
        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_subprocess_retries")
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=FileNotFoundError("gh not installed"),
            ) as m_run,
            patch("dispatcher.daemon.time.sleep"),
        ):
            outcome = d._subprocess_with_retry(["gh"], event_name="probe", timeout=30)
        assert outcome["ok"] is False
        assert outcome["reason"] == "gh_missing"
        assert m_run.call_count == 3
        assert "gh not installed" in outcome["stderr_tail"]

    def test_extra_log_fields_propagate_to_events(self, caplog: Any) -> None:
        """``extra_log_fields`` (e.g. issue_number) appear on both flake and
        exhausted events."""
        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_subprocess_retries")
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="x", timeout=30),
            ),
            patch("dispatcher.daemon.time.sleep"),
        ):
            d._subprocess_with_retry(
                ["gh"],
                event_name="probe",
                timeout=30,
                extra_log_fields={"issue_number": 42, "pr_number": 99},
            )
        flakes = [
            r for r in caplog.records if getattr(r, "event", None) == "probe_flake"
        ]
        assert all(
            getattr(r, "issue_number", None) == 42
            and getattr(r, "pr_number", None) == 99
            for r in flakes
        )
        exh = [
            r for r in caplog.records if getattr(r, "event", None) == "probe_exhausted"
        ]
        assert len(exh) == 1
        assert getattr(exh[0], "issue_number", None) == 42
        assert getattr(exh[0], "pr_number", None) == 99

    def test_timeout_arg_passed_through(self) -> None:
        """The per-attempt ``timeout`` is forwarded to ``subprocess.run``."""
        d = _make_daemon()
        with patch("dispatcher.daemon.subprocess.run", return_value=_cp(0)) as m:
            d._subprocess_with_retry(["gh"], event_name="probe", timeout=17)
        assert m.call_args.kwargs["timeout"] == 17
        assert m.call_args.kwargs["check"] is False
        assert m.call_args.kwargs["capture_output"] is True
        assert m.call_args.kwargs["text"] is True

    def test_stderr_full_preserved_on_exhaustion(self) -> None:
        """On exhaust, the final attempt's raw stderr is preserved in
        ``stderr_full`` so callers that persist the full diagnostic
        (e.g. ``_persist_phase_output``'s ``log_text``) don't lose
        context beyond the 4000-char tail."""
        d = _make_daemon()
        long_stderr = (
            "boom " * 2000
        )  # 10,000 chars — well past STRUCTURED_LOG_STDERR_MAX
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                return_value=_cp(128, "", long_stderr),
            ),
            patch("dispatcher.daemon.time.sleep"),
        ):
            outcome = d._subprocess_with_retry(["gh"], event_name="probe", timeout=30)
        assert outcome["ok"] is False
        # stderr_tail is bounded to STRUCTURED_LOG_STDERR_MAX; stderr_full is the
        # untruncated payload for log_text callers.
        assert len(outcome["stderr_full"]) == len(long_stderr)
        assert len(outcome["stderr_tail"]) <= len(long_stderr)


# --------------------------------------------------------------------------
# Hot-path site smoke tests — a single transient flake at each site that
# gained retry under #3089 no longer surfaces as a hard failure.
# --------------------------------------------------------------------------


class TestHotPathRetries:
    """Covers #3089-added retries by exercising each hot-path site once
    with a one-flake-then-success subprocess pattern. Documents the
    new contract in test form."""

    def test_fetch_agent_ready_issues_retries_transient_flake(self) -> None:
        """``_fetch_agent_ready_issues`` survives one transient ``gh`` flake."""
        d = _make_daemon()
        ok = _cp(0, '[{"number": 1, "title": "t", "labels": [], "createdAt": "x"}]')
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=[_cp(1, "", "HTTP 502"), ok],
            ) as m_run,
            patch("dispatcher.daemon.time.sleep"),
        ):
            result = d._fetch_agent_ready_issues()
        assert m_run.call_count == 2
        assert result == [{"number": 1, "title": "t", "labels": [], "createdAt": "x"}]

    def test_fetch_blocked_issues_retries_transient_flake(self) -> None:
        """``_fetch_blocked_issues`` survives one transient ``gh`` flake."""
        d = _make_daemon()
        ok = _cp(
            0,
            '[{"number": 2, "title": "b", "labels": [], "createdAt": "x", "body": ""}]',
        )
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=[subprocess.TimeoutExpired(cmd="gh", timeout=30), ok],
            ) as m_run,
            patch("dispatcher.daemon.time.sleep"),
        ):
            result = d._fetch_blocked_issues()
        assert m_run.call_count == 2
        assert len(result) == 1

    def test_fetch_pr_status_retries_transient_flake(self) -> None:
        """``_fetch_pr_status`` survives one transient ``gh`` flake."""
        d = _make_daemon()
        ok = _cp(0, '{"statusCheckRollup": [], "mergeable": "MERGEABLE"}')
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=[_cp(1, "", "HTTP 502"), ok],
            ) as m_run,
            patch("dispatcher.daemon.time.sleep"),
        ):
            result = d._fetch_pr_status(42)
        assert m_run.call_count == 2
        assert result is not None
        assert result.get("mergeable") == "MERGEABLE"

    def test_fetch_issue_bundle_retries_transient_flake(self) -> None:
        """``_fetch_issue_bundle`` survives one transient ``gh`` flake."""
        d = _make_daemon()
        ok = _cp(
            0,
            '{"number": 7, "title": "t", "body": "b", "labels": [], "comments": [], "updatedAt": "x"}',
        )
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=[_cp(1, "", "HTTP 502"), ok],
            ) as m_run,
            patch("dispatcher.daemon.time.sleep"),
        ):
            bundle = d._fetch_issue_bundle(7)
        assert m_run.call_count == 2
        assert bundle["issue_number"] == 7

    def test_gh_issue_add_labels_retries_transient_flake(self) -> None:
        """``_gh_issue_add_labels`` survives one transient ``gh`` flake.
        The claim interlock's ``status/in-progress`` add depends on this
        — a missed add desyncs dispatcher-v2 and the /task skill."""
        d = _make_daemon()
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=[_cp(1, "", "HTTP 502"), _cp(0, "")],
            ) as m_run,
            patch("dispatcher.daemon.time.sleep"),
        ):
            d._gh_issue_add_labels(99, ["status/in-progress"])
        assert m_run.call_count == 2

    def test_gh_issue_remove_labels_retries_transient_flake(self) -> None:
        """``_gh_issue_remove_labels`` survives one transient ``gh`` flake.
        The #2927 claim interlock releases by removing ``status/in-progress``;
        a stuck label would hide the issue from the queue scan forever."""
        d = _make_daemon()
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=[
                    subprocess.TimeoutExpired(cmd="gh", timeout=30),
                    _cp(0, ""),
                ],
            ) as m_run,
            patch("dispatcher.daemon.time.sleep"),
        ):
            d._gh_issue_remove_labels(99, ["status/in-progress"])
        assert m_run.call_count == 2

    def test_gh_issue_comment_retries_transient_flake(self) -> None:
        """``_gh_issue_comment`` survives one transient ``gh`` flake."""
        d = _make_daemon()
        # Monkeypatch the tmp-file writer so the test doesn't touch disk.
        d._write_gh_tmp_body = MagicMock(return_value=Path("/tmp/fake"))  # type: ignore[method-assign]
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=[_cp(1, "", "503"), _cp(0, "")],
            ) as m_run,
            patch("dispatcher.daemon.time.sleep"),
        ):
            d._gh_issue_comment(99, "body")
        assert m_run.call_count == 2

    def test_gh_issue_close_retries_transient_flake(self) -> None:
        """``_gh_issue_close`` survives one transient ``gh`` flake."""
        d = _make_daemon()
        d._write_gh_tmp_body = MagicMock(return_value=Path("/tmp/fake"))  # type: ignore[method-assign]
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=[_cp(1, "", "503"), _cp(0, "")],
            ) as m_run,
            patch("dispatcher.daemon.time.sleep"),
        ):
            d._gh_issue_close(99, comment="done", reason="completed")
        assert m_run.call_count == 2
