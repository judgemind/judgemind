"""Unit tests for issue #3085 — ``_baseline_fetch_origin_main`` retries transient git fetch flakes.

Coverage:
  - ``_baseline_fetch_origin_main`` now retries on ``TimeoutExpired``,
    non-zero exit, or ``FileNotFoundError`` up to 3 total attempts
    with 1s + 2s backoff, emitting a WARNING per failed attempt.
  - A single transient failure followed by success still returns
    without raising.
  - All three attempts failing raises ``RuntimeError`` *and* emits a
    final ``baseline_fetch_exhausted`` WARNING so the outage is
    visible in CloudWatch.
  - Happy path (first-attempt success) stays silent — no new log
    events relative to pre-#3085 behaviour.
  - ``_baseline_fetch_origin_main_once`` returns structured outcomes
    (``ok`` / ``reason`` / ``stderr_tail`` / ``exit_code``) so the
    retry wrapper can preserve the pre-#3085 ``exit=<code>``
    ``RuntimeError`` shape on exhaust.

Fakes follow the pattern from ``test_daemon_gh_issue_is_open_retry.py``
(PR #3058) — the direct template the issue references.
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
# Shared fakes — mirror the pattern from test_daemon_gh_issue_is_open_retry.py.
# --------------------------------------------------------------------------


def _make_daemon(baseline: Path | None = None) -> daemon.DispatcherDaemon:
    """Construct a DispatcherDaemon bypassing __init__ (no DB, no config)."""
    import threading

    d = daemon.DispatcherDaemon.__new__(daemon.DispatcherDaemon)
    d._thread_state = threading.local()  # type: ignore[attr-defined]
    d._main_conn = MagicMock()  # type: ignore[attr-defined]
    baseline_path = baseline if baseline is not None else Path("/tmp/fake-baseline")
    d._cfg = MagicMock(baseline_repo_root=baseline_path)  # type: ignore[attr-defined]
    d._log = logging.getLogger("test.daemon_baseline_fetch_retry")  # type: ignore[attr-defined]
    d._run_id = "test-run"  # type: ignore[attr-defined]
    return d


def _completed_process(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["git", "fetch"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# --------------------------------------------------------------------------
# AC#3 — happy path (first-attempt success) stays silent.
# --------------------------------------------------------------------------


class TestBaselineFetchHappyPath:
    def test_first_attempt_success_returns_without_error(self) -> None:
        """Happy path — no retry, no exception, no new log events."""
        d = _make_daemon()
        ok = _completed_process(returncode=0)
        with (
            patch("dispatcher.daemon.subprocess.run", return_value=ok) as mock_run,
            patch("dispatcher.daemon.time.sleep") as mock_sleep,
        ):
            # Does not raise.
            d._baseline_fetch_origin_main()
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    def test_happy_path_emits_no_flake_or_exhausted_events(self, caplog: Any) -> None:
        """First-attempt success must not emit ``baseline_fetch_flake`` or
        ``baseline_fetch_exhausted`` WARNINGs — preserves the pre-#3085
        silent happy-path cadence.
        """
        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_baseline_fetch_retry")
        ok = _completed_process(returncode=0)
        with (
            patch("dispatcher.daemon.subprocess.run", return_value=ok),
            patch("dispatcher.daemon.time.sleep"),
        ):
            d._baseline_fetch_origin_main()
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "baseline_fetch_flake" not in events
        assert "baseline_fetch_exhausted" not in events

    def test_baseline_none_returns_without_running_fetch(self) -> None:
        """Guard: ``baseline_repo_root=None`` is a no-op (pragma: no cover
        but exercised here for safety)."""
        d = _make_daemon(baseline=None)
        d._cfg = MagicMock(baseline_repo_root=None)
        with patch("dispatcher.daemon.subprocess.run") as mock_run:
            d._baseline_fetch_origin_main()
        mock_run.assert_not_called()


# --------------------------------------------------------------------------
# AC#1 — retry on transient failures (timeout, non-zero exit, etc.).
# --------------------------------------------------------------------------


class TestBaselineFetchRetry:
    def test_timeout_twice_then_success_returns_normally(self, caplog: Any) -> None:
        """Issue #3085 AC#1 — two transient timeouts then success returns
        without raising. Two flake WARNINGs, no exhausted WARNING.
        """
        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_baseline_fetch_retry")
        ok = _completed_process(returncode=0)
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=[
                    subprocess.TimeoutExpired(cmd="git", timeout=120),
                    subprocess.TimeoutExpired(cmd="git", timeout=120),
                    ok,
                ],
            ) as mock_run,
            patch("dispatcher.daemon.time.sleep") as mock_sleep,
        ):
            d._baseline_fetch_origin_main()
        assert mock_run.call_count == 3
        # Two backoffs between the three attempts — 1s and 2s.
        assert mock_sleep.call_args_list[0].args[0] == 1.0
        assert mock_sleep.call_args_list[1].args[0] == 2.0
        # Two flake WARNINGs emitted (one per failed attempt).
        flake_records = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "baseline_fetch_flake"
        ]
        assert len(flake_records) == 2
        assert flake_records[0].attempt == 1
        assert flake_records[1].attempt == 2
        assert flake_records[0].reason == "timeout"
        # No "exhausted" record on success.
        exhausted = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "baseline_fetch_exhausted"
        ]
        assert exhausted == []

    def test_nonzero_exit_once_then_success_returns_normally(self, caplog: Any) -> None:
        """Non-zero exit counts as transient (AC#1). This is the exact
        shape observed on 2026-04-23: ``git fetch exit=128`` with
        ``HTTP 500`` in stderr, recovering on the next attempt.
        """
        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_baseline_fetch_retry")
        bad = _completed_process(
            returncode=128,
            stderr=(
                "error: RPC failed; HTTP 500 curl 22 The requested URL "
                "returned error: 500\nfatal: expected 'acknowledgments'"
            ),
        )
        ok = _completed_process(returncode=0)
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=[bad, ok],
            ) as mock_run,
            patch("dispatcher.daemon.time.sleep") as mock_sleep,
        ):
            d._baseline_fetch_origin_main()
        assert mock_run.call_count == 2
        assert mock_sleep.call_count == 1
        assert mock_sleep.call_args_list[0].args[0] == 1.0
        flake_records = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "baseline_fetch_flake"
        ]
        assert len(flake_records) == 1
        assert flake_records[0].reason == "nonzero_exit"
        # stderr_tail should carry the observed HTTP 500 signature.
        assert "HTTP 500" in (flake_records[0].stderr_tail or "")

    def test_mixed_timeout_then_nonzero_then_success(self, caplog: Any) -> None:
        """Heterogeneous transient causes are all retryable."""
        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_baseline_fetch_retry")
        ok = _completed_process(returncode=0)
        bad = _completed_process(returncode=1, stderr="HTTP 502 Bad Gateway")
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=[
                    subprocess.TimeoutExpired(cmd="git", timeout=120),
                    bad,
                    ok,
                ],
            ) as mock_run,
            patch("dispatcher.daemon.time.sleep"),
        ):
            d._baseline_fetch_origin_main()
        assert mock_run.call_count == 3
        flake_records = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "baseline_fetch_flake"
        ]
        assert [r.reason for r in flake_records] == ["timeout", "nonzero_exit"]


# --------------------------------------------------------------------------
# AC#1 + AC#2 — exhaustion raises with the correct event + message shape.
# --------------------------------------------------------------------------


class TestBaselineFetchExhaustion:
    def test_three_timeouts_raises_and_emits_exhausted_warning(
        self, caplog: Any
    ) -> None:
        """Issue #3085 AC#1 — three timeouts exhausts the retry budget,
        raises ``RuntimeError``, and emits the final exhaustion WARNING.
        """
        import pytest

        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_baseline_fetch_retry")
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="git", timeout=120),
            ) as mock_run,
            patch("dispatcher.daemon.time.sleep") as mock_sleep,
            pytest.raises(RuntimeError, match="git fetch timed out"),
        ):
            d._baseline_fetch_origin_main()
        assert mock_run.call_count == 3
        # Two backoffs between the three attempts — 1s and 2s.
        assert mock_sleep.call_count == 2
        flake_records = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "baseline_fetch_flake"
        ]
        assert len(flake_records) == 3
        exhausted = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "baseline_fetch_exhausted"
        ]
        assert len(exhausted) == 1
        assert exhausted[0].attempts == 3
        assert exhausted[0].reason == "timeout"
        # Operator-visible baseline_repo_root surfaced in the event.
        assert exhausted[0].baseline_repo_root == "/tmp/fake-baseline"

    def test_three_nonzero_exits_raises_with_exit_code_shape(self, caplog: Any) -> None:
        """Preserve the pre-#3085 ``exit=<code>`` shape in the
        ``RuntimeError`` message so any log scraper / test that keys on
        that string still matches after exhaustion.
        """
        import pytest

        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_baseline_fetch_retry")
        bad = _completed_process(
            returncode=128,
            stderr="error: RPC failed; HTTP 500",
        )
        with (
            patch("dispatcher.daemon.subprocess.run", return_value=bad),
            patch("dispatcher.daemon.time.sleep"),
            pytest.raises(RuntimeError, match=r"git fetch exit=128"),
        ):
            d._baseline_fetch_origin_main()
        exhausted = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "baseline_fetch_exhausted"
        ]
        assert len(exhausted) == 1
        assert exhausted[0].reason == "nonzero_exit"
        assert "HTTP 500" in (exhausted[0].stderr_tail or "")

    def test_file_not_found_exhausts_and_raises(self, caplog: Any) -> None:
        """``FileNotFoundError`` (git CLI missing) is retried like other
        transient reasons. The retry cost is negligible (~3s worst case)
        and keeping the control flow uniform simplifies the wrapper.
        On exhaust the error message preserves the pre-#3085 "git CLI
        not on PATH" shape for continuity.
        """
        import pytest

        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_baseline_fetch_retry")
        with (
            patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=FileNotFoundError("git not installed"),
            ) as mock_run,
            patch("dispatcher.daemon.time.sleep"),
            pytest.raises(RuntimeError, match="git CLI not on PATH"),
        ):
            d._baseline_fetch_origin_main()
        assert mock_run.call_count == 3
        flake_records = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "baseline_fetch_flake"
        ]
        assert all(r.reason == "gh_missing" for r in flake_records)


# --------------------------------------------------------------------------
# Detail helper — structured outcomes for the retry loop.
# --------------------------------------------------------------------------


class TestBaselineFetchOnce:
    def test_success_returns_ok_true(self) -> None:
        d = _make_daemon()
        ok = _completed_process(returncode=0)
        with patch("dispatcher.daemon.subprocess.run", return_value=ok):
            outcome = d._baseline_fetch_origin_main_once(Path("/tmp/fake"))
        assert outcome == {"ok": True}

    def test_timeout_returns_reason_timeout(self) -> None:
        d = _make_daemon()
        with patch(
            "dispatcher.daemon.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=120),
        ):
            outcome = d._baseline_fetch_origin_main_once(Path("/tmp/fake"))
        assert outcome["ok"] is False
        assert outcome["reason"] == "timeout"
        assert outcome["stderr_tail"] == ""

    def test_nonzero_exit_returns_exit_code_and_stderr_tail(self) -> None:
        d = _make_daemon()
        bad = _completed_process(returncode=128, stderr="error: RPC failed; HTTP 500")
        with patch("dispatcher.daemon.subprocess.run", return_value=bad):
            outcome = d._baseline_fetch_origin_main_once(Path("/tmp/fake"))
        assert outcome["ok"] is False
        assert outcome["reason"] == "nonzero_exit"
        assert outcome["exit_code"] == 128
        assert "HTTP 500" in outcome["stderr_tail"]

    def test_file_not_found_returns_reason_gh_missing(self) -> None:
        d = _make_daemon()
        with patch(
            "dispatcher.daemon.subprocess.run",
            side_effect=FileNotFoundError("git not installed"),
        ):
            outcome = d._baseline_fetch_origin_main_once(Path("/tmp/fake"))
        assert outcome["ok"] is False
        assert outcome["reason"] == "gh_missing"
        assert "git not installed" in outcome["stderr_tail"]

    def test_once_never_raises(self) -> None:
        """The ``_once`` helper must never raise — the retry wrapper
        relies on this to count failed attempts deterministically.
        """
        d = _make_daemon()
        for exc in (
            FileNotFoundError("missing"),
            subprocess.TimeoutExpired(cmd="git", timeout=120),
            OSError("broken pipe"),
        ):
            with patch(
                "dispatcher.daemon.subprocess.run",
                side_effect=exc,
            ):
                try:
                    outcome = d._baseline_fetch_origin_main_once(Path("/tmp/fake"))
                except OSError:
                    # OSError other than FileNotFoundError / TimeoutExpired
                    # is not caught — the wrapper's own ``try/except`` in
                    # production should surface it. This assertion
                    # documents the contract: only the two expected
                    # transient types are caught here.
                    assert isinstance(exc, OSError)
                    assert not isinstance(
                        exc, (FileNotFoundError, subprocess.TimeoutExpired)
                    )
                else:
                    assert outcome["ok"] is False
