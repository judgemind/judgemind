"""Unit tests for ``stderr_preview`` on phase-failure log events (#2809).

When a ``/task-v2-<phase>`` subprocess exits 0 but fails to produce its
structured output file — or the retro phase hits a non-zero exit,
timeout, or OS-level spawn error — the daemon now attaches a short
(≤200 char) preview of the phase log to the structured failure event so
operators can triage from CloudWatch Log Insights without ECS Exec'ing
into the container.

Covers:

* :meth:`daemon.DispatcherDaemon._extract_log_preview` — the shared
  helper: empty string when the log is missing, ≤ ``max_chars`` chars
  when populated, stripped of surrounding whitespace.
* ``phase_output_missing`` events for plan / ralph / summary / fix-ci /
  verify — each includes ``stderr_preview`` when the log has content,
  omits it when empty.
* Retro phase failure paths (``retro_nonzero_exit``, ``retro_timeout``,
  ``retro_subprocess_error``, ``retro_output_missing``) — same rule.
* Truncation at 200 chars (the issue's cap).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Fakes — minimal enough to exercise the logging paths. The surrounding
# plan/ralph/summary/fix-ci/verify/retro tests already cover the DB
# side via _FakeConnection; we only need the log envelope here, so the
# connection fake is stripped down.
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.fetch_queue: list[Any] = []
        self.fetchall_queue: list[Any] = []
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        if not self.fetch_queue:
            return None
        return self.fetch_queue.pop(0)

    def fetchall(self) -> Any:
        if not self.fetchall_queue:
            return []
        return self.fetchall_queue.pop(0)


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = _FakeCursor()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.autocommit = False

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class _CapturingLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def events(self, name: str) -> list[logging.LogRecord]:
        return [r for r in self.records if getattr(r, "event", None) == name]


def _make_daemon(
    tmp_path: Path,
) -> tuple[daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.preview.{id(tmp_path)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    conn = _FakeConnection()
    cfg = daemon.DaemonConfig(
        database_url="postgres://fake",
        version_sha="deadbee",
        host="test-host",
        pid=9999,
        github_repo="judgemind/judgemind",
        dispatcher_service_name="judgemind-dispatcher-test",
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = conn  # type: ignore[assignment]
    d._run_id = "test-run-id"
    return d, conn, handler


def _write_phase_log(worktree: Path, phase: str, content: str) -> None:
    """Mimic what ``_spawn_phase_subprocess`` writes on subprocess exit."""
    log_dir = worktree / "tmp"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"claude-p-{phase}.log").write_text(content)


# --------------------------------------------------------------------------
# _extract_log_preview — the shared helper
# --------------------------------------------------------------------------


class TestExtractLogPreview:
    def test_returns_empty_when_log_missing(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        # No claude-p-plan.log written.
        preview = d._extract_log_preview(worktree, "plan")
        assert preview == ""

    def test_returns_stripped_content_when_short(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        _write_phase_log(worktree, "plan", "\n  short error message  \n")
        preview = d._extract_log_preview(worktree, "plan")
        # .strip() removes leading/trailing whitespace.
        assert preview == "short error message"

    def test_truncates_at_max_chars(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        # A log >2000 chars — preview caps at PHASE_STDERR_PREVIEW_MAX_CHARS
        # (2000, raised from 200 in #2821).
        tail = "X" * 3000
        _write_phase_log(worktree, "plan", tail)
        preview = d._extract_log_preview(worktree, "plan")
        assert len(preview) == 2000
        assert preview == "X" * 2000

    def test_honours_custom_max_chars(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        _write_phase_log(worktree, "plan", "Y" * 300)
        preview = d._extract_log_preview(worktree, "plan", max_chars=50)
        assert len(preview) == 50

    def test_returns_tail_of_long_log(self, tmp_path: Path) -> None:
        """Error text at the tail survives truncation.

        Phase subprocess logs are merged stdout+stderr — initial noise
        (env dump, prompt echo) lives at the head, the actual failure
        at the tail. The helper must return the tail.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        body = ("noise\n" * 1000) + "RuntimeError: plan input invalid"
        _write_phase_log(worktree, "plan", body)
        preview = d._extract_log_preview(worktree, "plan")
        assert "RuntimeError: plan input invalid" in preview


# --------------------------------------------------------------------------
# phase_output_missing — plan / ralph / summary / fix-ci / verify
# --------------------------------------------------------------------------


class TestPhaseOutputMissingPreview:
    """Each ``_run_*_phase`` method that logs ``phase_output_missing``
    attaches a ``stderr_preview`` when the phase log has content and
    omits the field when the log is empty."""

    def _run_plan_with_missing_output(
        self, tmp_path: Path, log_body: str | None
    ) -> _CapturingLogHandler:
        d, _conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        if log_body is not None:
            _write_phase_log(worktree, "plan", log_body)

        # Short-circuit the issue fetch + subprocess spawn so we land
        # directly on the missing-output branch.
        d._fetch_issue_bundle = MagicMock(  # type: ignore[method-assign]
            return_value={
                "issue_number": 1,
                "issue_title": "t",
                "issue_body": "b",
                "issue_comments": [],
                "issue_labels": [],
                "blocked_by": [],
                "parent_issue": None,
            }
        )
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        # _read_phase_output returns None → triggers phase_output_missing.
        d._read_phase_output = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]

        d._run_plan_phase("agent-123", 1, worktree)
        return handler

    def test_plan_missing_output_logs_preview(self, tmp_path: Path) -> None:
        handler = self._run_plan_with_missing_output(
            tmp_path, "Traceback...\nKeyError: 'plan_input'"
        )
        events = handler.events("phase_output_missing")
        assert events
        record = events[0]
        assert getattr(record, "phase", None) == "plan"
        preview = getattr(record, "stderr_preview", None)
        assert preview is not None
        assert "KeyError" in preview

    def test_plan_missing_output_omits_preview_when_log_empty(
        self, tmp_path: Path
    ) -> None:
        handler = self._run_plan_with_missing_output(tmp_path, None)
        events = handler.events("phase_output_missing")
        assert events
        record = events[0]
        # Field omitted — not just empty string.
        assert not hasattr(record, "stderr_preview")

    def test_ralph_missing_output_logs_preview(self, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _write_phase_log(worktree, "ralph", "Ralph crashed: OOM")

        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._read_phase_output = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._apply_prior_ralph_patch = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._materialize_prior_attempts = MagicMock(return_value=0)  # type: ignore[method-assign]
        # _run_ralph_phase fetches plan output from DB (#2975).
        d._fetch_phase_output = MagicMock(return_value={})  # type: ignore[method-assign]

        d._run_ralph_phase("agent-123", 1, worktree)

        events = handler.events("phase_output_missing")
        assert events
        record = events[0]
        assert getattr(record, "phase", None) == "ralph"
        assert "Ralph crashed" in getattr(record, "stderr_preview", "")

    def test_summary_missing_output_logs_preview(self, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _write_phase_log(worktree, "summary", "Summary failed to parse diff")

        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._read_phase_output = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._fetch_issue_bundle = MagicMock(  # type: ignore[method-assign]
            return_value={
                "issue_number": 1,
                "issue_title": "t",
                "issue_body": "b",
                "issue_comments": [],
                "issue_labels": [],
                "blocked_by": [],
                "parent_issue": None,
            }
        )
        # _run_summary_phase fetches ralph and plan outputs from DB (#2975).
        d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda agent_id, phase: {
                "ralph": {"summary": "", "changed_files": []},
                "plan": {"acceptance_criteria": [], "scope_check": []},
            }.get(phase)
        )

        d._run_summary_phase("agent-123", 1, worktree)

        events = handler.events("phase_output_missing")
        assert events
        record = events[0]
        assert getattr(record, "phase", None) == "summary"
        assert "Summary failed" in getattr(record, "stderr_preview", "")

    def test_fix_ci_missing_output_logs_preview(self, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _write_phase_log(worktree, "fix-ci", "fix-ci subprocess returned wrong shape")

        agent_id = "agent-123"

        # Short-circuit helpers that require richer state.
        d._read_phase_output = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]

        # #3658: call _finalize_fix_ci directly (async path).
        d._phase_subprocess_inflight[agent_id] = {
            "phase": "fix-ci",
            "pid": 99999,
            "worktree_path": worktree,
            "started_at": 0.0,
            "deadline_at": 99999.0,
            "ctx": {"pr_number": 77, "issue_number": 1, "retries_used": 0, "agent": {}},
        }
        d._finalize_fix_ci(agent_id, worktree, 0)

        events = handler.events("phase_output_missing")
        assert events
        record = events[0]
        assert getattr(record, "phase", None) == "fix-ci"
        assert "wrong shape" in getattr(record, "stderr_preview", "")

    def test_verify_missing_output_logs_preview(self, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _write_phase_log(worktree, "verify", "Verify skill crashed on AC #3")

        agent_id = "agent-123"

        # Short-circuit helpers — verify's input-assembly is long and we
        # only want the missing-output branch.
        d._read_phase_output = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]

        # #3658: call _finalize_verify directly; merged_at_set=False routes
        # through handle_agent_failure (the desired "phase_output_missing" path).
        d._phase_subprocess_inflight[agent_id] = {
            "phase": "verify",
            "pid": 99999,
            "worktree_path": worktree,
            "started_at": 0.0,
            "deadline_at": 99999.0,
            "ctx": {
                "pr_number": 77,
                "issue_number": 1,
                "merge_sha": "deadbeef",
                "merged_at_set": False,
            },
        }
        d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
        d._finalize_verify(agent_id, worktree, 0)

        events = handler.events("phase_output_missing")
        assert events
        record = events[0]
        assert getattr(record, "phase", None) == "verify"
        assert "crashed on AC" in getattr(record, "stderr_preview", "")


# --------------------------------------------------------------------------
# Retro phase failure paths
# --------------------------------------------------------------------------


def _succeeded_agent(tmp_path: Path) -> dict[str, Any]:
    agent_id = "agent-retro-preview"
    worktree = tmp_path / agent_id
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "tmp").mkdir(parents=True, exist_ok=True)
    return {
        "agent_id": agent_id,
        "issue_number": 4242,
        "phase": "done",
        "pr_number": 999,
        "worktree_path": str(worktree),
        "retries_used": 0,
        "status": "succeeded",
    }


class TestRetroFailurePreviews:
    def _prep(self, d: daemon.DispatcherDaemon, agent: dict[str, Any]) -> None:
        # Short-circuit helpers that touch the DB — the retro phase's
        # input-bundle builder reads phase_transitions + failures tables.
        d._build_retro_input = MagicMock(
            return_value={  # type: ignore[method-assign]
                "agent_id": agent["agent_id"],
                "issue_number": agent["issue_number"],
                "pr_number": agent["pr_number"],
                "ralph_iterations": 1,
                "ci_attempts": 1,
                "fix_ci_attempts": 0,
                "total_duration_s": 10.0,
                "phase_transitions": [],
                "failures_by_category": {},
            }
        )
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._read_phase_output = MagicMock(return_value=None)  # type: ignore[method-assign]

    def _run_retro_finalize(
        self,
        d: daemon.DispatcherDaemon,
        agent: dict[str, Any],
        worktree: Path,
        exit_code: int | None,
    ) -> None:
        """#3658: populate inflight registry and call _finalize_retro directly
        (mirrors the reaper pattern used in test_daemon_phase3e.py)."""
        agent_id = agent["agent_id"]
        d._phase_subprocess_inflight[agent_id] = {
            "phase": "retro",
            "pid": 99999,
            "worktree_path": worktree,
            "started_at": 0.0,
            "deadline_at": 99999.0,
            "ctx": {
                "issue_number": agent.get("issue_number"),
                "pr_number": agent.get("pr_number"),
            },
        }
        d._finalize_retro(agent_id, worktree, exit_code)

    def test_nonzero_exit_logs_preview(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path)
        worktree = Path(agent["worktree_path"])
        _write_phase_log(worktree, "retro", "retro exit 1: bad haiku")
        self._prep(d, agent)

        # #3658: async path — call _finalize_retro directly with nonzero exit.
        self._run_retro_finalize(d, agent, worktree, exit_code=1)

        events = handler.events("retro_nonzero_exit")
        assert events
        record = events[0]
        preview = getattr(record, "stderr_preview", None)
        assert preview is not None
        assert "bad haiku" in preview

    def test_nonzero_exit_omits_preview_when_log_empty(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path)
        worktree = Path(agent["worktree_path"])
        # No phase log written.
        self._prep(d, agent)

        # #3658: async path — nonzero exit, no log file.
        self._run_retro_finalize(d, agent, worktree, exit_code=1)

        events = handler.events("retro_nonzero_exit")
        assert events
        record = events[0]
        assert not hasattr(record, "stderr_preview")

    def test_timeout_logs_preview(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path)
        worktree = Path(agent["worktree_path"])
        _write_phase_log(
            worktree, "retro", "partial output before the LLM hung forever..."
        )
        self._prep(d, agent)

        # #3658: async path — exit_code=None means reaper killed the process
        # (deadline exceeded), which maps to the retro_timeout event.
        self._run_retro_finalize(d, agent, worktree, exit_code=None)

        events = handler.events("retro_timeout")
        assert events
        record = events[0]
        preview = getattr(record, "stderr_preview", None)
        assert preview is not None
        assert "partial output" in preview

    def test_subprocess_error_logs_preview(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """#3658: retro_subprocess_error is replaced by phase_async_spawn_failed.

        Pre-#3658, a FileNotFoundError during Popen was caught synchronously
        inside _spawn_phase_subprocess and logged as ``retro_subprocess_error``.
        Post-#3658, the same OS error is caught inside
        _spawn_phase_subprocess_async and logged as ``phase_async_spawn_failed``
        (phase="retro").  No stderr_preview is attached because the subprocess
        never ran — there is no log to read.
        """
        d, _conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path)
        worktree = Path(agent["worktree_path"])
        _write_phase_log(worktree, "retro", "pre-spawn OS error log content")
        self._prep(d, agent)

        # Patch subprocess.Popen so the actual _spawn_phase_subprocess_async
        # raises FileNotFoundError (simulating "claude" binary missing).
        import subprocess as _subprocess

        _orig_popen = _subprocess.Popen

        def _fake_popen(*args: Any, **kwargs: Any) -> None:
            raise FileNotFoundError("claude binary missing")

        monkeypatch.setattr(_subprocess, "Popen", _fake_popen)

        # Call _start_retro (fires async spawn) via _run_retro_phase.
        d._run_retro_phase(agent)

        # The event is now phase_async_spawn_failed, not retro_subprocess_error.
        events = handler.events("phase_async_spawn_failed")
        assert events
        record = events[0]
        assert getattr(record, "phase", None) == "retro"
        assert getattr(record, "reason", None) == "claude_binary_not_found"

        monkeypatch.setattr(_subprocess, "Popen", _orig_popen)

    def test_output_missing_logs_preview(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path)
        worktree = Path(agent["worktree_path"])
        _write_phase_log(worktree, "retro", "retro JSON write failed silently")
        self._prep(d, agent)

        # #3658: async path — exit 0 but no output file (read returns None via mock).
        self._run_retro_finalize(d, agent, worktree, exit_code=0)

        events = handler.events("retro_output_missing")
        assert events
        record = events[0]
        preview = getattr(record, "stderr_preview", None)
        assert preview is not None
        assert "JSON write failed" in preview

    def test_preview_truncated_to_2000_chars_on_retro_nonzero(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """The preview cap was raised from 200 → 2000 in #2821.

        Noisy failures (ralph running 1000 tests with one real error,
        terraform plan with a single actual error) routinely push the
        useful line past the old 200-char boundary. 2000 covers real
        failure modes without pulling in the full log (the full log is
        captured separately via ``daemon.phase_failure_log`` +
        ``dispatcher.phase_outputs.log_text``).
        """
        d, _conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path)
        worktree = Path(agent["worktree_path"])
        _write_phase_log(worktree, "retro", "Z" * 3000)
        self._prep(d, agent)

        # #3658: async path — nonzero exit, log truncated to 2000 chars.
        self._run_retro_finalize(d, agent, worktree, exit_code=1)

        events = handler.events("retro_nonzero_exit")
        assert events
        preview = getattr(events[0], "stderr_preview", "")
        assert len(preview) == 2000
