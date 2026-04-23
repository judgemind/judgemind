"""Unit tests for the Phase 3E retro orchestration + worktree cleanup +
diagnoser effectiveness tracking (issue #2798).

Covers the post-success state-machine extensions added to
``_advance_running_agents``:

* ``status='succeeded' AND phase='done'`` → spawn ``/task-v2-retro``,
  parse output, file zero-to-many ``gh issue create`` calls. Transitions
  to ``phase='retro_done'`` on success or ``phase='retro_failed'`` on
  subprocess / parse failure. ``status='succeeded'`` is preserved.
* ``status='succeeded' AND phase IN ('retro_done', 'retro_failed')`` →
  run ``scripts/cleanup_worktree.sh``. Transitions to
  ``phase='cleanup_done'`` on success or ``phase='cleanup_blocked'`` on
  safety-check refusal. Daemon never bypasses with ``--force``.
* Diagnoser effectiveness tracking: ``_mark_agent_terminal`` writes
  back the resolved outcome to any pending ``dispatcher.diagnoses``
  rows for that agent. Idempotent under repeated terminal transitions.

All external calls (``gh``, ``claude -p``, ``scripts/cleanup_worktree.sh``,
psycopg) are mocked. The subprocess used by ``_spawn_phase_subprocess``
is stubbed so no real LLM is invoked.
"""

from __future__ import annotations

import json
import logging
import subprocess
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
# Shared fakes — same shape as test_daemon_phase3b/3c/3d.
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.fetch_queue: list[Any] = []
        self.fetchall_queue: list[list[Any]] = []
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

    def fetchall(self) -> list[Any]:
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
    logger = logging.getLogger(f"dispatcher.test.phase3e.{id(tmp_path)}")
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


def _succeeded_agent(
    tmp_path: Path,
    agent_id: str = "agent-3e-success",
    phase: str = "done",
    pr_number: int = 999,
    issue_number: int = 4242,
) -> dict[str, Any]:
    """Build the dict shape ``_advance_*`` methods consume."""
    worktree = tmp_path / agent_id
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "tmp").mkdir(parents=True, exist_ok=True)
    return {
        "agent_id": agent_id,
        "issue_number": issue_number,
        "phase": phase,
        "pr_number": pr_number,
        "worktree_path": str(worktree),
        "retries_used": 0,
        "status": "succeeded",
    }


# --------------------------------------------------------------------------
# Phase constants — name-stability checks. The runbook + admin page
# reference these strings verbatim, so the values must not drift.
# --------------------------------------------------------------------------


class TestPhase3EPhaseConstants:
    def test_constants_match_documented_values(self) -> None:
        assert daemon.PHASE_RETRO_DONE == "retro_done"
        assert daemon.PHASE_RETRO_FAILED == "retro_failed"
        assert daemon.PHASE_CLEANUP_DONE == "cleanup_done"
        assert daemon.PHASE_CLEANUP_BLOCKED == "cleanup_blocked"

    def test_retro_phase_in_max_turns(self) -> None:
        assert "retro" in daemon.PHASE_MAX_TURNS
        # 10× bumped in #2885 (was 30).
        assert daemon.PHASE_MAX_TURNS["retro"] == 300

    def test_retro_phase_in_models(self) -> None:
        assert "retro" in daemon.PHASE_MODELS
        assert daemon.PHASE_MODELS["retro"] == "haiku"


# --------------------------------------------------------------------------
# _list_advanceable_agents — extended SELECT covers succeeded phases
# --------------------------------------------------------------------------


class TestListAdvanceableIncludesSucceededPhases:
    def test_select_includes_succeeded_done_and_retro_phases(
        self, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # Only verify the SQL — empty fetchall is fine for shape check.
        conn.cursor_instance.fetchall_queue = [[]]
        d._list_advanceable_agents()
        assert conn.cursor_instance.executed
        sql, _params = conn.cursor_instance.executed[0]
        assert "status = 'succeeded'" in sql
        assert "'done'" in sql
        assert "'retro_done'" in sql
        assert "'retro_failed'" in sql
        # Existing 3B coverage must still be in the SQL.
        assert "'awaiting_ci'" in sql
        assert "'awaiting_deploy'" in sql

    def test_returns_status_field(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [
            [
                (
                    "agent-1",
                    1,
                    "done",
                    101,
                    str(tmp_path),
                    0,
                    "succeeded",
                ),
                (
                    "agent-2",
                    2,
                    "awaiting_ci",
                    102,
                    str(tmp_path),
                    0,
                    "running",
                ),
            ]
        ]
        rows = d._list_advanceable_agents()
        assert len(rows) == 2
        assert rows[0]["status"] == "succeeded"
        assert rows[0]["phase"] == "done"
        assert rows[1]["status"] == "running"


# --------------------------------------------------------------------------
# _advance_running_agents — dispatch to retro / cleanup branches
# --------------------------------------------------------------------------


class TestAdvanceRunningAgentsRoutesToRetroAndCleanup:
    def test_succeeded_done_routes_to_retro(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        d._list_advanceable_agents = lambda: [_succeeded_agent(tmp_path)]  # type: ignore[method-assign]
        called: dict[str, Any] = {}
        d._run_retro_phase = lambda agent: called.setdefault("retro", agent)  # type: ignore[method-assign]
        d._cleanup_agent_worktree = lambda agent: called.setdefault(  # type: ignore[method-assign]
            "cleanup", agent
        )
        d._advance_running_agents()
        assert "retro" in called
        assert "cleanup" not in called

    def test_succeeded_retro_done_routes_to_cleanup(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        d._list_advanceable_agents = lambda: [  # type: ignore[method-assign]
            _succeeded_agent(tmp_path, phase=daemon.PHASE_RETRO_DONE)
        ]
        called: dict[str, Any] = {}
        d._run_retro_phase = lambda agent: called.setdefault("retro", agent)  # type: ignore[method-assign]
        d._cleanup_agent_worktree = lambda agent: called.setdefault(  # type: ignore[method-assign]
            "cleanup", agent
        )
        d._advance_running_agents()
        assert "cleanup" in called
        assert "retro" not in called

    def test_succeeded_retro_failed_still_routes_to_cleanup(
        self, tmp_path: Path
    ) -> None:
        # retro_failed → cleanup must still run; the agent succeeded.
        d, _conn, _handler = _make_daemon(tmp_path)
        d._list_advanceable_agents = lambda: [  # type: ignore[method-assign]
            _succeeded_agent(tmp_path, phase=daemon.PHASE_RETRO_FAILED)
        ]
        called: dict[str, Any] = {}
        d._cleanup_agent_worktree = lambda agent: called.setdefault(  # type: ignore[method-assign]
            "cleanup", agent
        )
        d._advance_running_agents()
        assert "cleanup" in called

    def test_exception_in_retro_does_not_flip_to_crashed(self, tmp_path: Path) -> None:
        # Status='succeeded' must be preserved even if retro raises.
        d, conn, handler = _make_daemon(tmp_path)
        d._list_advanceable_agents = lambda: [_succeeded_agent(tmp_path)]  # type: ignore[method-assign]

        def boom(agent: dict[str, Any]) -> None:
            raise RuntimeError("retro blew up")

        d._run_retro_phase = boom  # type: ignore[method-assign]
        d._advance_running_agents()
        # No status='crashed' UPDATE was issued; only an UPDATE phase
        # to retro_failed.
        crashed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "crashed" in str(e[1])
        ]
        assert not crashed_updates, "should not flip to crashed for succeeded agent"
        # advance_failed event was logged.
        failed = handler.events("advance_failed")
        assert failed
        assert failed[0].status == "succeeded"
        # phase update to retro_failed was issued (one of the executes).
        retro_failed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and daemon.PHASE_RETRO_FAILED in str(e[1])
        ]
        assert retro_failed_updates


# --------------------------------------------------------------------------
# _run_retro_phase — happy path with zero retro issues (clean run)
# --------------------------------------------------------------------------


class TestRunRetroPhaseZeroFindings:
    def test_zero_retro_issues_transitions_to_retro_done(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path)

        # Stub the retro spawn to write a no-findings output.
        def fake_spawn(phase: str, wt: Path, agent_id: str) -> tuple[int, float]:
            assert phase == "retro"
            output_dir = wt / "tmp" / "dispatcher-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            output_dir.joinpath("retro.json").write_text(
                json.dumps(
                    {
                        "agent_id": agent_id,
                        "issue_number": agent["issue_number"],
                        "pr_number": agent["pr_number"],
                        "no_findings": True,
                        "retro_issues": [],
                    }
                )
            )
            return 0, 1.5

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        # Monkeypatch subprocess.run so that any unexpected gh call
        # would surface clearly. With zero retro issues, no gh call
        # should happen.
        gh_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            gh_calls.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        d._run_retro_phase(agent)

        # No gh issue create calls.
        gh_creates = [c for c in gh_calls if c[:3] == ["gh", "issue", "create"]]
        assert gh_creates == []

        # Phase transitioned to retro_done.
        retro_done_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and daemon.PHASE_RETRO_DONE in str(e[1])
        ]
        assert retro_done_updates

        # retro_done event logged with no_findings=True.
        events = handler.events("retro_done")
        assert events
        assert events[0].no_findings is True
        assert events[0].issues_filed == 0


# --------------------------------------------------------------------------
# _run_retro_phase — happy path with multiple retro issues
# --------------------------------------------------------------------------


class TestRunRetroPhaseMultipleIssues:
    def test_multiple_retro_issues_files_each_via_gh_create(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path)

        retro_payload = {
            "agent_id": agent["agent_id"],
            "issue_number": agent["issue_number"],
            "pr_number": agent["pr_number"],
            "no_findings": False,
            "retro_issues": [
                {
                    "title": "fix(dx): tighten retro pre-push check",
                    "body": "## Problem\n\nRetro found CI was caught too late.",
                    "labels": ["type/dx", "agent/ready", "priority/p2"],
                    "priority": "priority/p2",
                },
                {
                    "title": "feat(area/devops): add retry budgeting hint",
                    "body": "## Problem\n\nRalph took 4 iterations.",
                    "labels": ["type/dx", "agent/ready", "priority/p2"],
                    "priority": "priority/p2",
                },
            ],
        }

        def fake_spawn(phase: str, wt: Path, agent_id: str) -> tuple[int, float]:
            output_dir = wt / "tmp" / "dispatcher-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            output_dir.joinpath("retro.json").write_text(json.dumps(retro_payload))
            return 0, 5.0

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        gh_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            gh_calls.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            r.stdout = "https://github.com/judgemind/judgemind/issues/9999\n"
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        d._run_retro_phase(agent)

        gh_creates = [c for c in gh_calls if c[:3] == ["gh", "issue", "create"]]
        assert len(gh_creates) == 2

        # Each call passes --title, --body-file (file), and at least
        # one --label.
        for call in gh_creates:
            assert "--title" in call
            assert "--body-file" in call
            assert "--label" in call
            # Labels include the ones from the retro payload.
            assert "type/dx" in call

        # Phase transitioned to retro_done.
        retro_done_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and daemon.PHASE_RETRO_DONE in str(e[1])
        ]
        assert retro_done_updates

        # retro_issue_created event logged for each filing.
        created_events = handler.events("retro_issue_created")
        assert len(created_events) == 2

        # retro_done event captured the count.
        done_events = handler.events("retro_done")
        assert done_events
        assert done_events[0].issues_filed == 2

    def test_default_labels_used_when_entry_omits_them(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path)

        retro_payload = {
            "no_findings": False,
            "retro_issues": [
                {
                    "title": "chore(dx): polish",
                    "body": "## Problem\n\nMinor tidy.",
                    # labels intentionally omitted.
                }
            ],
        }

        def fake_spawn(phase: str, wt: Path, agent_id: str) -> tuple[int, float]:
            output_dir = wt / "tmp" / "dispatcher-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            output_dir.joinpath("retro.json").write_text(json.dumps(retro_payload))
            return 0, 1.0

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        gh_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            gh_calls.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            r.stdout = "https://github.com/judgemind/judgemind/issues/1\n"
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._run_retro_phase(agent)

        creates = [c for c in gh_calls if c[:3] == ["gh", "issue", "create"]]
        assert creates
        # All default labels present.
        for label in daemon.DEFAULT_RETRO_LABELS:
            assert label in creates[0]


# --------------------------------------------------------------------------
# _run_retro_phase — failure paths
# --------------------------------------------------------------------------


class TestRunRetroPhaseFailures:
    def test_subprocess_nonzero_exit_marks_retro_failed(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path)

        def fake_spawn(phase: str, wt: Path, agent_id: str) -> tuple[int, float]:
            return 1, 2.0  # non-zero exit, no output written

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)
        d._run_retro_phase(agent)

        # Phase flipped to retro_failed.
        retro_failed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and daemon.PHASE_RETRO_FAILED in str(e[1])
        ]
        assert retro_failed_updates

        # No status='succeeded'/'failed' UPDATE — succeeded must be
        # preserved (only the phase is updated).
        status_changes = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "status" in e[0]
            and "phase" in e[0]
            and "ended_at" in e[0]
        ]
        assert not status_changes

        # retro_nonzero_exit event logged.
        assert handler.events("retro_nonzero_exit")

    def test_timeout_marks_retro_failed(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path)

        def fake_spawn(phase: str, wt: Path, agent_id: str) -> tuple[int, float]:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=300)

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)
        d._run_retro_phase(agent)

        retro_failed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and daemon.PHASE_RETRO_FAILED in str(e[1])
        ]
        assert retro_failed_updates
        assert handler.events("retro_timeout")

    def test_missing_output_marks_retro_failed(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path)

        def fake_spawn(phase: str, wt: Path, agent_id: str) -> tuple[int, float]:
            # Exit 0 but no output file.
            return 0, 1.0

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)
        d._run_retro_phase(agent)

        retro_failed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and daemon.PHASE_RETRO_FAILED in str(e[1])
        ]
        assert retro_failed_updates
        assert handler.events("retro_output_missing")

    def test_gh_issue_create_failure_does_not_block_remaining(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path)

        retro_payload = {
            "no_findings": False,
            "retro_issues": [
                {"title": "first", "body": "ok"},
                {"title": "second", "body": "ok"},
            ],
        }

        def fake_spawn(phase: str, wt: Path, agent_id: str) -> tuple[int, float]:
            output_dir = wt / "tmp" / "dispatcher-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            output_dir.joinpath("retro.json").write_text(json.dumps(retro_payload))
            return 0, 1.0

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        # Issue #3089: ``_file_retro_issue`` now routes through the
        # shared 3-attempt 1s+2s retry helper (``_subprocess_with_retry``).
        # The first retro's ``gh issue create`` exhausts all 3 attempts
        # → the second retro still runs on its own first attempt and
        # succeeds. Total subprocess.run calls: 3 (first retro) + 1
        # (second retro) = 4.
        call_n = {"i": 0}

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            call_n["i"] += 1
            # First retro (call 1/2/3) all fail with 503 — exhaust retry.
            # Second retro (call 4) succeeds.
            if call_n["i"] <= 3:
                r.returncode = 1
                r.stdout = ""
                r.stderr = "github 503"
            else:
                r.returncode = 0
                r.stdout = "https://github.com/judgemind/judgemind/issues/2\n"
                r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(daemon.time, "sleep", lambda *_a, **_kw: None)
        d._run_retro_phase(agent)

        # First retro: 3 retry attempts exhausted. Second retro: 1 attempt.
        assert call_n["i"] == 4
        # First retro failed, second succeeded → 1 issue created.
        created = handler.events("retro_issue_created")
        assert len(created) == 1
        # Failure logged (the retry-exhausted WARNING OR the
        # ``retro_issue_create_failed`` nonzero_exit WARNING — either
        # counts as "failure was recorded").
        failures = handler.events("retro_issue_create_failed")
        assert len(failures) == 1

    def test_missing_worktree_directory_short_circuits_to_cleanup_done(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path)
        # Remove the worktree directory before calling.
        worktree = Path(agent["worktree_path"])
        # Drop the tmp/ subdir AND the worktree dir itself so .exists() is False.
        for p in sorted(
            worktree.rglob("*"), key=lambda p: -len(str(p))
        ):  # remove deepest first
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        worktree.rmdir()

        d._run_retro_phase(agent)

        # cleanup_done phase update issued (skips retro entirely).
        cleanup_done_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and daemon.PHASE_CLEANUP_DONE in str(e[1])
        ]
        assert cleanup_done_updates
        assert handler.events("retro_worktree_missing")

    def test_retro_issue_cap_truncates(self, monkeypatch: Any, tmp_path: Path) -> None:
        # Defensive: a runaway retro that wants to file 100 issues
        # should get truncated to MAX_RETRO_ISSUES_PER_AGENT.
        d, _conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path)

        many_issues = [
            {"title": f"issue {i}", "body": "ok"}
            for i in range(daemon.MAX_RETRO_ISSUES_PER_AGENT + 5)
        ]
        retro_payload = {"no_findings": False, "retro_issues": many_issues}

        def fake_spawn(phase: str, wt: Path, agent_id: str) -> tuple[int, float]:
            output_dir = wt / "tmp" / "dispatcher-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            output_dir.joinpath("retro.json").write_text(json.dumps(retro_payload))
            return 0, 1.0

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        n = {"i": 0}

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            n["i"] += 1
            r = MagicMock()
            r.returncode = 0
            r.stdout = f"https://github.com/judgemind/judgemind/issues/{n['i']}\n"
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._run_retro_phase(agent)

        # Calls capped at MAX_RETRO_ISSUES_PER_AGENT.
        assert n["i"] == daemon.MAX_RETRO_ISSUES_PER_AGENT
        # Truncation event logged.
        assert handler.events("retro_issue_cap_truncate")


# --------------------------------------------------------------------------
# _file_retro_issue — input validation
# --------------------------------------------------------------------------


class TestFileRetroIssueValidation:
    def test_missing_title_returns_none(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        # subprocess.run should never be called.
        called = {"n": 0}

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            called["n"] += 1
            r = MagicMock()
            r.returncode = 0
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = d._file_retro_issue(
            "agent-1", tmp_path, {"title": "", "body": "non-empty"}
        )
        assert result is None
        assert called["n"] == 0
        assert handler.events("retro_issue_missing_fields")

    def test_missing_body_returns_none(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        called = {"n": 0}

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            called["n"] += 1
            return MagicMock(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = d._file_retro_issue(
            "agent-1", tmp_path, {"title": "real title", "body": ""}
        )
        assert result is None
        assert called["n"] == 0
        assert handler.events("retro_issue_missing_fields")

    def test_truncates_oversize_body(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        oversize = "x" * (daemon.MAX_RETRO_ISSUE_BODY_CHARS + 100)
        captured: dict[str, str] = {}

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            # Pull the --body-file path out of the command.
            idx = cmd.index("--body-file")
            captured["body_path"] = cmd[idx + 1]
            captured["body"] = Path(cmd[idx + 1]).read_text()
            r = MagicMock()
            r.returncode = 0
            r.stdout = "https://github.com/judgemind/judgemind/issues/1\n"
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._file_retro_issue(
            "agent-1", tmp_path, {"title": "oversize", "body": oversize}
        )
        assert "[truncated]" in captured["body"]
        assert len(captured["body"]) <= daemon.MAX_RETRO_ISSUE_BODY_CHARS + 50


# --------------------------------------------------------------------------
# _parse_issue_url — pure helper
# --------------------------------------------------------------------------


class TestParseIssueUrl:
    def test_extracts_trailing_issue_number(self) -> None:
        assert (
            daemon.DispatcherDaemon._parse_issue_url(
                "https://github.com/judgemind/judgemind/issues/2812\n"
            )
            == 2812
        )

    def test_no_match_returns_none(self) -> None:
        assert daemon.DispatcherDaemon._parse_issue_url("nothing here") is None


# --------------------------------------------------------------------------
# _cleanup_agent_worktree — success + blocked branches
# --------------------------------------------------------------------------


class TestCleanupAgentWorktree:
    def test_success_transitions_to_cleanup_done(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path, phase=daemon.PHASE_RETRO_DONE)

        # Force a known repo root with a real cleanup script present.
        repo_root = tmp_path / "repo"
        scripts_dir = repo_root / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "cleanup_worktree.sh").write_text("#!/bin/sh\nexit 0\n")
        monkeypatch.setattr(d, "_repo_root", lambda: repo_root)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            assert cmd[0].endswith("cleanup_worktree.sh")
            assert cmd[1] == agent["worktree_path"]
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._cleanup_agent_worktree(agent)

        cleanup_done_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and daemon.PHASE_CLEANUP_DONE in str(e[1])
        ]
        assert cleanup_done_updates
        assert handler.events("cleanup_done")

    def test_safety_check_refusal_marks_cleanup_blocked(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path, phase=daemon.PHASE_RETRO_DONE)

        repo_root = tmp_path / "repo"
        scripts_dir = repo_root / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "cleanup_worktree.sh").write_text("#!/bin/sh\nexit 1\n")
        monkeypatch.setattr(d, "_repo_root", lambda: repo_root)

        force_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            if "--force" in cmd:
                force_calls.append(list(cmd))
            r.returncode = 1
            r.stdout = ""
            r.stderr = "agent still running\n"
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._cleanup_agent_worktree(agent)

        # Phase flipped to cleanup_blocked.
        blocked_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and daemon.PHASE_CLEANUP_BLOCKED in str(e[1])
        ]
        assert blocked_updates

        # CRITICAL: daemon must NOT have called git worktree remove --force.
        assert not force_calls, "daemon must not bypass cleanup safety with --force"

        assert handler.events("cleanup_blocked")

    def test_subprocess_timeout_marks_blocked(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path, phase=daemon.PHASE_RETRO_DONE)

        repo_root = tmp_path / "repo"
        (repo_root / "scripts").mkdir(parents=True, exist_ok=True)
        (repo_root / "scripts" / "cleanup_worktree.sh").write_text(
            "#!/bin/sh\nexit 0\n"
        )
        monkeypatch.setattr(d, "_repo_root", lambda: repo_root)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._cleanup_agent_worktree(agent)

        blocked_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and daemon.PHASE_CLEANUP_BLOCKED in str(e[1])
        ]
        assert blocked_updates
        assert handler.events("cleanup_subprocess_error")

    def test_missing_worktree_path_marks_blocked(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path, phase=daemon.PHASE_RETRO_DONE)
        agent["worktree_path"] = ""
        d._cleanup_agent_worktree(agent)

        blocked_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and daemon.PHASE_CLEANUP_BLOCKED in str(e[1])
        ]
        assert blocked_updates
        assert handler.events("cleanup_missing_worktree_path")

    def test_already_gone_worktree_marks_done(self, tmp_path: Path) -> None:
        # If the worktree directory is missing, treat as already-cleaned.
        d, conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path, phase=daemon.PHASE_RETRO_DONE)
        # Remove the worktree directory.
        worktree = Path(agent["worktree_path"])
        for p in sorted(worktree.rglob("*"), key=lambda x: -len(str(x))):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        worktree.rmdir()
        d._cleanup_agent_worktree(agent)

        cleanup_done_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and daemon.PHASE_CLEANUP_DONE in str(e[1])
        ]
        assert cleanup_done_updates
        assert handler.events("cleanup_already_gone")

    def test_missing_cleanup_script_falls_back_to_git_remove_success(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Fargate case: script is intentionally not shipped in the
        container. Instead of emitting a WARNING and stalling at
        ``cleanup_blocked`` forever, the daemon falls back to
        ``git worktree remove --force`` via
        :meth:`_drop_worktree_best_effort`. See #3056."""
        d, conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path, phase=daemon.PHASE_RETRO_DONE)
        # Repo root with no cleanup script present.
        empty_repo = tmp_path / "empty-repo"
        empty_repo.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(d, "_repo_root", lambda: empty_repo)
        # Fake ``_drop_worktree_best_effort`` to report success.
        drop_calls: list[str] = []

        def fake_drop(worktree_path: str) -> bool:
            drop_calls.append(worktree_path)
            return True

        monkeypatch.setattr(d, "_drop_worktree_best_effort", fake_drop)
        d._cleanup_agent_worktree(agent)

        # Delegated to the best-effort drop with the agent's worktree path.
        assert drop_calls == [agent["worktree_path"]]

        # Phase flipped to cleanup_done (not cleanup_blocked).
        done_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and daemon.PHASE_CLEANUP_DONE in str(e[1])
        ]
        assert done_updates

        # Must NOT have emitted the noisy WARNING from #3056. The new
        # event name is INFO-level and explicitly narrates the fallback.
        assert not handler.events("cleanup_script_missing"), (
            "missing script is the expected Fargate condition; must not WARN"
        )
        assert handler.events("cleanup_script_absent_using_git")
        assert handler.events("cleanup_done")

    def test_missing_cleanup_script_falls_back_to_git_remove_failure(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """If the git fallback itself fails (e.g. worktree is locked),
        the agent lands at ``cleanup_blocked`` — an operator sweep can
        clean up manually. Still no ``cleanup_script_missing`` WARNING."""
        d, conn, handler = _make_daemon(tmp_path)
        agent = _succeeded_agent(tmp_path, phase=daemon.PHASE_RETRO_DONE)
        empty_repo = tmp_path / "empty-repo"
        empty_repo.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(d, "_repo_root", lambda: empty_repo)

        def fake_drop(worktree_path: str) -> bool:
            return False

        monkeypatch.setattr(d, "_drop_worktree_best_effort", fake_drop)
        d._cleanup_agent_worktree(agent)

        blocked_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and daemon.PHASE_CLEANUP_BLOCKED in str(e[1])
        ]
        assert blocked_updates
        assert not handler.events("cleanup_script_missing")
        assert handler.events("cleanup_script_absent_using_git")


# --------------------------------------------------------------------------
# _write_diagnosis_outcome_for_agent + _mark_agent_terminal integration
# --------------------------------------------------------------------------


class TestDiagnosisOutcomeWriteback:
    def test_succeeded_terminal_writes_succeeded_outcome(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # The execute that updates dispatcher.diagnoses sets rowcount.
        conn.cursor_instance.rowcount = 1

        d._mark_agent_terminal(
            "agent-success", status="succeeded", phase="cleanup_done", exit_code=0
        )

        # An UPDATE dispatcher.diagnoses query was executed.
        diag_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.diagnoses" in e[0]
        ]
        assert diag_updates
        sql, params = diag_updates[0]
        assert "outcome IS NULL" in sql
        # Outcome JSON contains succeeded.
        outcome_json = params[0]
        outcome_dict = json.loads(outcome_json)
        assert outcome_dict["retry_outcome"] == "succeeded"
        assert outcome_dict["final_status"] == "succeeded"
        assert "resolved_at" in outcome_dict
        assert params[1] == "agent-success"
        # diagnosis_outcome_written event logged.
        assert handler.events("diagnosis_outcome_written")

    def test_failed_terminal_writes_failed_outcome(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.rowcount = 1

        d._mark_agent_terminal(
            "agent-failed", status="failed", phase="awaiting_ci", exit_code=1
        )

        diag_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.diagnoses" in e[0]
        ]
        assert diag_updates
        outcome_dict = json.loads(diag_updates[0][1][0])
        assert outcome_dict["retry_outcome"] == "failed"
        assert outcome_dict["final_status"] == "failed"

    def test_crashed_terminal_writes_failed_outcome(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.rowcount = 1

        d._mark_agent_terminal(
            "agent-crashed", status="crashed", phase="awaiting_ci", exit_code=None
        )

        diag_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.diagnoses" in e[0]
        ]
        assert diag_updates
        outcome_dict = json.loads(diag_updates[0][1][0])
        # crashed counts as failed for the outcome enum.
        assert outcome_dict["retry_outcome"] == "failed"
        assert outcome_dict["final_status"] == "crashed"

    def test_plan_blocked_terminal_writes_succeeded_outcome(
        self, tmp_path: Path
    ) -> None:
        """``plan_blocked`` (#2857) is a terminal status that classifies as a
        correct-outcome retry (not a failure) for diagnoser effectiveness
        tracking.

        The agent row's ``status`` is ``plan_blocked`` (visible in the
        admin cockpit as a distinct chip), but the retry-outcome
        classification for dashboards/reporting is ``succeeded`` —
        declining to proceed on a malformed issue is the correct thing
        for the diagnoser to have surfaced.
        """
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.rowcount = 1

        d._mark_agent_terminal(
            "agent-plan-blocked",
            status="plan_blocked",
            phase="planning",
            exit_code=0,
        )

        diag_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.diagnoses" in e[0]
        ]
        assert diag_updates
        outcome_dict = json.loads(diag_updates[0][1][0])
        assert outcome_dict["retry_outcome"] == "succeeded"
        assert outcome_dict["final_status"] == "plan_blocked"
        # Terminal bookkeeping ran: ended_at SET on the agent row.
        agent_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "plan_blocked" in e[1]
        ]
        assert agent_updates
        # The SET clause of the terminal update writes ended_at.
        assert "ended_at = now()" in agent_updates[0][0]
        # diagnosis_outcome_written event logged.
        assert handler.events("diagnosis_outcome_written")

    def test_needs_review_terminal_writes_succeeded_outcome(
        self, tmp_path: Path
    ) -> None:
        """``needs_review`` (#2856) classifies as a correct-outcome retry.

        Ralph produced reviewer-approved (SHIP) code and the summary
        phase found unmet AC — the daemon opened a draft PR for
        operator review. That IS the correct behavior of the pipeline,
        so the retry_outcome enum records ``succeeded`` for diagnoser
        effectiveness tracking (distinct from the agent row's own
        ``status='needs_review'`` which drives the cockpit's distinct
        chip). Mirrors the #2857 ``plan_blocked`` classification.
        """
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.rowcount = 1

        d._mark_agent_terminal(
            "agent-needs-review",
            status="needs_review",
            phase="needs_review",
            exit_code=None,
            pr_number=9001,
        )

        diag_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.diagnoses" in e[0]
        ]
        assert diag_updates
        outcome_dict = json.loads(diag_updates[0][1][0])
        assert outcome_dict["retry_outcome"] == "succeeded"
        assert outcome_dict["final_status"] == "needs_review"
        # Terminal bookkeeping ran: ended_at SET on the agent row.
        agent_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "needs_review" in e[1]
        ]
        assert agent_updates
        assert "ended_at = now()" in agent_updates[0][0]
        assert handler.events("diagnosis_outcome_written")

    def test_non_terminal_status_skips_outcome_write(self, tmp_path: Path) -> None:
        # When the daemon transitions to e.g. awaiting_ci (non-terminal),
        # no diagnoses outcome write should happen.
        d, conn, _handler = _make_daemon(tmp_path)

        d._mark_agent_terminal(
            "agent-running", status="running", phase="awaiting_ci", exit_code=None
        )

        diag_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.diagnoses" in e[0]
        ]
        assert not diag_updates

    def test_idempotent_update_uses_outcome_is_null_predicate(
        self, tmp_path: Path
    ) -> None:
        # Calling _write_diagnosis_outcome_for_agent twice should issue
        # the same UPDATE; the WHERE clause's outcome IS NULL means the
        # second run is a no-op against rows that already have outcome.
        d, conn, _handler = _make_daemon(tmp_path)
        # First call sees 1 row, second sees 0 — but both issue the same SQL.
        conn.cursor_instance.rowcount = 1
        d._write_diagnosis_outcome_for_agent("agent-1", "succeeded")
        first_executes = list(conn.cursor_instance.executed)
        conn.cursor_instance.rowcount = 0
        d._write_diagnosis_outcome_for_agent("agent-1", "succeeded")
        second_executes = list(conn.cursor_instance.executed)

        # SQL pattern is identical (idempotent) — same WHERE clause.
        assert first_executes[0][0] == second_executes[len(first_executes)][0], (
            "SQL must be identical across calls (idempotency)"
        )
        # Both invocations include the outcome IS NULL predicate so
        # repeated calls cannot overwrite an already-set outcome.
        assert "outcome IS NULL" in second_executes[len(first_executes)][0]

    def test_db_error_during_outcome_write_does_not_propagate(
        self, tmp_path: Path
    ) -> None:
        # Even if the outcome write fails, _mark_agent_terminal must
        # not raise (it has already committed the agent terminal
        # transition). The failure is logged.
        d, conn, handler = _make_daemon(tmp_path)
        # First execute succeeds (the agent UPDATE), second raises.
        original_execute = conn.cursor_instance.execute
        call_n = {"i": 0}

        def patched(sql: str, params: Any = None) -> None:
            call_n["i"] += 1
            if "UPDATE dispatcher.diagnoses" in sql:
                raise RuntimeError("db lost")
            original_execute(sql, params)

        conn.cursor_instance.execute = patched  # type: ignore[method-assign]
        # Should not raise.
        d._mark_agent_terminal("agent-1", status="succeeded", phase="done", exit_code=0)
        # Failure event logged.
        assert handler.events("diagnosis_outcome_update_failed")
