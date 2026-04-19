"""Unit tests for the Phase 3A orchestration path in ``scripts.dispatcher.daemon``.

Issue #2783. Covers the seven behaviors the issue's acceptance criteria
call out:

* ``_claim_and_orchestrate_one`` picks the highest-priority trusted
  candidate from the latest ``queue_snapshots`` row.
* Atomic claim via INSERT catches ``psycopg.errors.UniqueViolation``
  as the "lost the race" signal.
* Per-agent worktree created at ``.claude/worktrees/agent-<short-uuid>``
  from ``origin/main`` (subprocess mocked).
* ``/task-v2-{plan,ralph,summary}`` invoked sequentially via
  ``claude -p`` subprocess (subprocess mocked). Phase output JSON
  written to ``dispatcher.phase_outputs`` after each;
  ``dispatcher.phase_transitions`` row per phase change.
* On all-phases-success, daemon pushes + creates PR via ``gh pr
  create`` (subprocess mocked); PR number recorded in
  ``dispatcher.agents.pr_number``.
* Phase failures (subprocess non-zero exit, ralph BLOCKED, plan
  go=false with block_reason) set ``agents.status='failed'`` and stop
  orchestration without crashing the daemon.
* Concurrency cap honored: if ``concurrency_cap=0``, no claims
  attempted (Phase 2 behavior preserved).

All external calls — subprocess (``claude -p``, ``gh``, ``git``,
``scripts/check-issue-author.sh``) and psycopg — are mocked. The
orchestration path does not exercise ``claude`` or ``gh`` binaries on
the test runner.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import uuid as uuid_mod
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# Provide a stub ``psycopg`` module before importing the daemon. The
# daemon's ``_atomic_claim`` catches ``psycopg.errors.UniqueViolation``,
# so that class must be a real Exception subclass. Other tests install
# a pure ``MagicMock()`` stub (see test_daemon_phase2.py) whose
# ``errors.UniqueViolation`` is itself a Mock — that trips Python's
# "catching classes that do not inherit from BaseException" rule when
# the daemon's ``except psycopg.errors.UniqueViolation`` clause runs.
# Always overwrite the stub here so our exception class is installed
# regardless of test-collection order.


class _UniqueViolation(Exception):
    """Test sentinel — stands in for real psycopg.errors.UniqueViolation."""


_psycopg_stub = MagicMock()
_psycopg_errors = MagicMock()
_psycopg_errors.UniqueViolation = _UniqueViolation
_psycopg_stub.errors = _psycopg_errors
sys.modules["psycopg"] = _psycopg_stub

import psycopg  # noqa: E402  — re-import after stub install

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.fetch_queue: list[Any] = []
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
    logger = logging.getLogger(f"dispatcher.test.phase3a.{id(tmp_path)}")
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


# --------------------------------------------------------------------------
# Gate in scheduler_tick: orchestration only runs when cap>0 and no agent
# --------------------------------------------------------------------------


class TestSchedulerGate:
    """``_claim_and_orchestrate_one`` runs only when the gate allows."""

    def test_does_not_claim_when_concurrency_cap_zero(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._claim_and_orchestrate_one = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not be called with cap=0")
        )
        summary = d.scheduler_tick()
        assert summary["orchestration_attempted"] == 0
        assert d._claim_and_orchestrate_one.call_count == 0  # type: ignore[attr-defined]

    def test_does_not_claim_when_concurrency_cap_missing(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._claim_and_orchestrate_one = MagicMock()  # type: ignore[method-assign]
        d.scheduler_tick()
        assert d._claim_and_orchestrate_one.call_count == 0  # type: ignore[attr-defined]

    def test_claims_when_cap_nonzero_and_no_active_agent(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # 1) config read returns 1; 2) _has_active_agent SELECT returns None.
        conn.cursor_instance.fetch_queue = [(1,), None]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._claim_and_orchestrate_one = MagicMock()  # type: ignore[method-assign]
        summary = d.scheduler_tick()
        assert summary["orchestration_attempted"] == 1
        assert d._claim_and_orchestrate_one.call_count == 1  # type: ignore[attr-defined]

    def test_does_not_claim_when_active_agent_exists(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # config=1 then _has_active_agent returns (1,) meaning active row exists.
        conn.cursor_instance.fetch_queue = [(1,), (1,)]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._claim_and_orchestrate_one = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not be called when agent active")
        )
        summary = d.scheduler_tick()
        assert summary["orchestration_attempted"] == 0

    def test_orchestration_exception_does_not_crash_tick(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,), None]
        d._fetch_agent_ready_issues = lambda: []  # type: ignore[method-assign]
        d._claim_and_orchestrate_one = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("boom")
        )
        # The tick must still return a summary, not raise.
        summary = d.scheduler_tick()
        assert summary["orchestration_attempted"] == 1
        assert handler.events("orchestration_failed") != []


# --------------------------------------------------------------------------
# _has_active_agent
# --------------------------------------------------------------------------


class TestHasActiveAgent:
    def test_no_running_row_returns_false(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        assert d._has_active_agent() is False

    def test_running_row_returns_true(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]
        assert d._has_active_agent() is True

    def test_db_error_returns_true_fail_closed(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            raise RuntimeError("connection lost")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        # Fail-closed: treat DB error as "active" so we skip this tick.
        assert d._has_active_agent() is True
        assert conn.rollbacks >= 1


# --------------------------------------------------------------------------
# _latest_queue_snapshot_issues
# --------------------------------------------------------------------------


class TestLatestQueueSnapshot:
    def test_returns_issue_numbers_from_most_recent_row(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [([100, 200, 300],)]
        assert d._latest_queue_snapshot_issues() == [100, 200, 300]

    def test_returns_empty_when_no_snapshots(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        assert d._latest_queue_snapshot_issues() == []

    def test_returns_empty_on_db_error(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            raise RuntimeError("db lost")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        assert d._latest_queue_snapshot_issues() == []
        assert conn.rollbacks >= 1


# --------------------------------------------------------------------------
# _issue_already_attempted
# --------------------------------------------------------------------------


class TestIssueAlreadyAttempted:
    def test_no_row_returns_false(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        assert d._issue_already_attempted(42) is False

    def test_running_row_returns_true(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1,)]
        assert d._issue_already_attempted(42) is True

    def test_db_error_returns_true_fail_closed(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            raise RuntimeError("db lost")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        assert d._issue_already_attempted(42) is True


# --------------------------------------------------------------------------
# _issue_author_trusted (wraps scripts/check-issue-author.sh)
# --------------------------------------------------------------------------


class TestIssueAuthorTrusted:
    def test_exit_zero_returns_true(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            assert cmd[0].endswith("check-issue-author.sh")
            assert cmd[1] == "42"
            r = MagicMock()
            r.returncode = 0
            r.stdout = "TRUSTED: ...\n"
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert d._issue_author_trusted(42) is True

    def test_exit_nonzero_returns_false(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 1
            r.stdout = "UNTRUSTED: ...\n"
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert d._issue_author_trusted(42) is False
        rejects = handler.events("trust_check_rejected")
        assert len(rejects) == 1

    def test_timeout_returns_false(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert d._issue_author_trusted(42) is False
        assert handler.events("trust_check_timeout") != []

    def test_script_missing_returns_false(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            raise FileNotFoundError(2, "nope", cmd[0])

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert d._issue_author_trusted(42) is False
        assert handler.events("trust_check_missing") != []


# --------------------------------------------------------------------------
# _pick_candidate_issue
# --------------------------------------------------------------------------


class TestPickCandidate:
    def test_returns_first_eligible_candidate(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        # Mark #1 as already attempted, #2 as untrusted, #3 as eligible.
        attempted = {1}
        untrusted = {2}

        d._issue_already_attempted = lambda n: n in attempted  # type: ignore[method-assign]
        d._issue_author_trusted = lambda n: n not in untrusted  # type: ignore[method-assign]

        assert d._pick_candidate_issue([1, 2, 3, 4]) == 3

    def test_returns_none_when_all_skipped(self, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        d._issue_already_attempted = lambda n: True  # type: ignore[method-assign]
        d._issue_author_trusted = lambda n: False  # type: ignore[method-assign]
        assert d._pick_candidate_issue([1, 2, 3]) is None
        # Three skip events logged (all "already attempted" since that's
        # checked first).
        assert len(handler.events("candidate_skipped")) == 3


# --------------------------------------------------------------------------
# _atomic_claim
# --------------------------------------------------------------------------


class TestAtomicClaim:
    def test_happy_path_inserts_and_returns_true(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        ok = d._atomic_claim(42, "agent-uuid", "/path")
        assert ok is True
        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.agents" in e[0]
        ]
        assert len(inserts) == 1
        assert conn.commits == 1
        assert handler.events("claim_succeeded") != []

    def test_unique_violation_returns_false(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            if "INSERT INTO dispatcher.agents" in sql:
                raise psycopg.errors.UniqueViolation("dup")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        ok = d._atomic_claim(42, "agent-uuid", "/path")
        assert ok is False
        assert handler.events("claim_lost") != []
        assert conn.rollbacks >= 1

    def test_unexpected_error_returns_false(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            raise RuntimeError("other")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        ok = d._atomic_claim(42, "agent-uuid", "/path")
        assert ok is False
        assert handler.events("claim_failed") != []


# --------------------------------------------------------------------------
# _create_worktree / git subprocess
# --------------------------------------------------------------------------


class TestCreateWorktree:
    def test_happy_path_calls_git_worktree_add(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            captured["cmd"] = cmd
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        # Use a canonical uuid so the short_id is deterministic.
        agent_id = "aabbccdd-eeff-0011-2233-445566778899"

        wt = d._create_worktree(agent_id)
        assert "git" == captured["cmd"][0]
        assert "worktree" in captured["cmd"]
        assert "add" in captured["cmd"]
        # short id = first 8 of hex-collapsed uuid = "aabbccdd".
        assert str(wt).endswith(".claude/worktrees/agent-aabbccdd")
        assert "-b" in captured["cmd"]
        branch_idx = captured["cmd"].index("-b") + 1
        assert captured["cmd"][branch_idx] == "agent/aabbccdd"
        assert handler.events("worktree_created") != []

    def test_git_failure_raises(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 128
            r.stdout = ""
            r.stderr = "fatal: branch already exists\n"
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        import pytest

        with pytest.raises(RuntimeError) as excinfo:
            d._create_worktree("agent-uuid-0000-0000-0000-000000000000")
        assert "exit=128" in str(excinfo.value)


# --------------------------------------------------------------------------
# _fetch_issue_bundle
# --------------------------------------------------------------------------


class TestFetchIssueBundle:
    def test_parses_gh_issue_view_output(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            assert cmd[0] == "gh" and cmd[1] == "issue" and cmd[2] == "view"
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps(
                {
                    "number": 42,
                    "title": "Title",
                    "body": "Body\nParent: #99\nBlocked by #7\n",
                    "labels": [{"name": "priority/p1"}],
                    "comments": [
                        {
                            "author": {"login": "drewthaler"},
                            "authorAssociation": "MEMBER",
                            "createdAt": "2026-04-18T00:00:00Z",
                            "body": "hi",
                        },
                        {
                            "author": {"login": "github-actions[bot]"},
                            "authorAssociation": "NONE",
                            "createdAt": "2026-04-18T00:01:00Z",
                            "body": "bot noise",
                        },
                    ],
                }
            )
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        bundle = d._fetch_issue_bundle(42)
        assert bundle["issue_number"] == 42
        assert bundle["issue_title"] == "Title"
        assert bundle["issue_labels"] == ["priority/p1"]
        assert bundle["parent_issue"] == 99
        assert bundle["blocked_by"] == [7]
        # Bot comment filtered out.
        assert len(bundle["issue_comments"]) == 1
        assert bundle["issue_comments"][0]["author"] == "drewthaler"

    def test_gh_failure_raises(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            r.stderr = "HTTP 404\n"
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        import pytest

        with pytest.raises(RuntimeError):
            d._fetch_issue_bundle(42)


class TestParseBlockedBy:
    def test_extracts_blocked_by_refs(self) -> None:
        body = "Intro\nBlocked by #42\nOther\nblocked by #99\n"
        assert daemon.DispatcherDaemon._parse_blocked_by(body) == [42, 99]

    def test_returns_empty_when_absent(self) -> None:
        assert daemon.DispatcherDaemon._parse_blocked_by("no refs") == []


class TestParseParentIssue:
    def test_extracts_parent(self) -> None:
        assert daemon.DispatcherDaemon._parse_parent_issue("Parent: #2782") == 2782

    def test_returns_none_when_absent(self) -> None:
        assert daemon.DispatcherDaemon._parse_parent_issue("nope") is None


class TestParsePrNumber:
    def test_parses_gh_pr_create_stdout(self) -> None:
        out = (
            "Creating pull request for head into main in judgemind/judgemind\n"
            "https://github.com/judgemind/judgemind/pull/1234\n"
        )
        assert daemon.DispatcherDaemon._parse_pr_number(out) == 1234

    def test_returns_none_when_unparseable(self) -> None:
        assert daemon.DispatcherDaemon._parse_pr_number("nope") is None


# --------------------------------------------------------------------------
# _write_phase_input / _read_phase_output
# --------------------------------------------------------------------------


class TestPhaseInputOutput:
    def test_roundtrip(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        payload = {"agent_id": "a", "issue_number": 1}
        d._write_phase_input(tmp_path, "plan", payload)
        input_path = tmp_path / "tmp" / "dispatcher-input" / "plan.json"
        assert json.loads(input_path.read_text()) == payload

        # Round-trip through _read_phase_output.
        output_dir = tmp_path / "tmp" / "dispatcher-output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "plan.json").write_text(json.dumps({"go": True}))
        assert d._read_phase_output(tmp_path, "plan") == {"go": True}

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        assert d._read_phase_output(tmp_path, "plan") is None

    def test_read_malformed_returns_none(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        output_dir = tmp_path / "tmp" / "dispatcher-output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "plan.json").write_text("not json")
        assert d._read_phase_output(tmp_path, "plan") is None


# --------------------------------------------------------------------------
# _persist_phase_output
# --------------------------------------------------------------------------


class TestPersistPhaseOutput:
    def test_writes_phase_outputs_and_transitions(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        d._persist_phase_output("a", "plan", {"go": True})
        # Both INSERTs ran.
        insert_outputs = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_outputs" in e[0]
        ]
        insert_transitions = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_transitions" in e[0]
        ]
        assert len(insert_outputs) == 1
        assert len(insert_transitions) == 1
        assert conn.commits == 1


# --------------------------------------------------------------------------
# _spawn_phase_subprocess — builds the correct claude -p command
# --------------------------------------------------------------------------


class TestSpawnPhaseSubprocess:
    def test_builds_command_with_correct_model_and_turns(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            captured["cmd"] = cmd
            captured["timeout"] = kwargs.get("timeout")
            r = MagicMock()
            r.returncode = 0
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        exit_code, duration = d._spawn_phase_subprocess("plan", tmp_path, "agent-uuid")
        assert exit_code == 0
        assert duration >= 0.0
        assert captured["cmd"][0] == "claude"
        assert "-p" in captured["cmd"]
        assert "/task-v2-plan agent-uuid" in captured["cmd"]
        assert "--cwd" in captured["cmd"]
        assert str(tmp_path) in captured["cmd"]
        assert "--max-turns" in captured["cmd"]
        assert "50" in captured["cmd"]  # plan
        assert "--model" in captured["cmd"]
        assert "opus" in captured["cmd"]
        assert captured["timeout"] == 180 * 60
        assert handler.events("phase_started") != []
        log_path = tmp_path / "tmp" / "claude-p-plan.log"
        assert log_path.exists()

    def test_ralph_uses_sonnet_and_500_turns(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            captured["cmd"] = cmd
            r = MagicMock()
            r.returncode = 0
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._spawn_phase_subprocess("ralph", tmp_path, "agent-uuid")
        assert "500" in captured["cmd"]
        assert "sonnet" in captured["cmd"]

    def test_summary_uses_haiku_and_30_turns(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            captured["cmd"] = cmd
            r = MagicMock()
            r.returncode = 0
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._spawn_phase_subprocess("summary", tmp_path, "agent-uuid")
        assert "30" in captured["cmd"]
        assert "haiku" in captured["cmd"]


# --------------------------------------------------------------------------
# _run_subprocess_or_fail — timeout, non-zero exit, missing claude
# --------------------------------------------------------------------------


class TestRunSubprocessOrFail:
    def test_happy_path_returns_zero(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        monkeypatch.setattr(d, "_spawn_phase_subprocess", lambda *a, **k: (0, 1.2))
        assert d._run_subprocess_or_fail("a", "plan", tmp_path) == 0

    def test_timeout_marks_failed_returns_none(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def boom(*_a: Any, **_k: Any) -> None:
            raise subprocess.TimeoutExpired(cmd=["claude"], timeout=10)

        monkeypatch.setattr(d, "_spawn_phase_subprocess", boom)
        result = d._run_subprocess_or_fail("a", "plan", tmp_path)
        assert result is None
        fails = handler.events("subprocess_failed")
        assert fails and fails[0].reason == "timeout"
        # Agent marked failed — UPDATE on dispatcher.agents ran.
        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
        ]
        assert updates

    def test_claude_missing_marks_failed(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        def boom(*_a: Any, **_k: Any) -> None:
            raise FileNotFoundError(2, "nope", "claude")

        monkeypatch.setattr(d, "_spawn_phase_subprocess", boom)
        assert d._run_subprocess_or_fail("a", "plan", tmp_path) is None
        fails = handler.events("subprocess_failed")
        assert fails and fails[0].reason == "claude_not_on_path"

    def test_nonzero_exit_marks_failed(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        monkeypatch.setattr(d, "_spawn_phase_subprocess", lambda *a, **k: (1, 0.1))
        assert d._run_subprocess_or_fail("a", "plan", tmp_path) is None
        fails = handler.events("subprocess_failed")
        assert fails and fails[0].exit_code == 1


# --------------------------------------------------------------------------
# Full happy-path orchestration — plan → ralph → summary → push → PR
# --------------------------------------------------------------------------


def _fixture_plan_output() -> dict[str, Any]:
    return {
        "agent_id": "a",
        "issue_number": 42,
        "go": True,
        "block_reason": None,
        "plan_text": "plan",
        "acceptance_criteria": ["AC1", "AC2"],
        "scope_check": [],
        "relevant_files": [],
        "relevant_docs": [],
        "change_type": "scraper",
        "dependencies_to_install": ["scraper-framework"],
    }


def _fixture_ralph_output() -> dict[str, Any]:
    return {
        "agent_id": "a",
        "issue_number": 42,
        "verdict": "SHIP",
        "iterations_used": 1,
        "block_reason": None,
        "changed_files": ["packages/scraper-framework/src/thing.py"],
        "summary": "Fixed a thing",
        "ralph_done_path": None,
        "review_log_path": None,
    }


def _fixture_summary_output() -> dict[str, Any]:
    return {
        "agent_id": "a",
        "issue_number": 42,
        "process_summary_md": "## Process Summary\n...",
        "commit_message": "fix(scraping): fix thing (#42)",
        "pr_title": "fix(scraping): fix thing (#42)",
        "pr_body_md": "## Summary\n\nFix.\n\nCloses #42\n",
        "unmet_criteria": [],
        "pre_pr_check_notes": "",
    }


class TestHappyPathOrchestration:
    """End-to-end: _claim_and_orchestrate_one through to gh pr create."""

    def test_claims_runs_phases_pushes_and_opens_pr(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        # 1. Queue snapshot read returns one candidate.
        # 2. _issue_already_attempted SELECT returns None (not attempted).
        conn.cursor_instance.fetch_queue = [([42],), None]

        # Trust check passes.
        def fake_check_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = "TRUSTED: ...\n"
            r.stderr = ""
            return r

        # Deterministic agent_id for short-id verification.
        fixed_uuid = uuid_mod.UUID("aabbccdd-eeff-0011-2233-445566778899")
        monkeypatch.setattr(uuid_mod, "uuid4", lambda: fixed_uuid)
        monkeypatch.setattr(daemon.uuid, "uuid4", lambda: fixed_uuid)

        # Repo root = tmp_path so worktrees land under it.
        monkeypatch.setattr(d, "_repo_root", lambda: tmp_path)

        # Track subprocess calls to assert the sequence.
        call_log: list[list[str]] = []

        # _spawn_phase_subprocess is mocked below to write the fixture
        # phase output file and return exit=0.

        def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> Any:
            call_log.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            # Trust check path.
            if cmd[0].endswith("check-issue-author.sh"):
                r.stdout = "TRUSTED: ...\n"
                return r
            # gh issue view path (bundle fetch — called twice: plan + summary).
            if (
                len(cmd) >= 3
                and cmd[0] == "gh"
                and cmd[1] == "issue"
                and cmd[2] == "view"
            ):
                r.stdout = json.dumps(
                    {
                        "number": 42,
                        "title": "Title",
                        "body": "body",
                        "labels": [{"name": "priority/p1"}],
                        "comments": [],
                    }
                )
                return r
            # git worktree add — pretend it succeeded. Create the dir
            # so later _write_phase_input / _read_phase_output work.
            if "worktree" in cmd and "add" in cmd:
                # Find the path in cmd (it's right after "add").
                add_idx = cmd.index("add")
                wt = Path(cmd[add_idx + 1])
                wt.mkdir(parents=True, exist_ok=True)
                return r
            # gh pr create — return URL stdout.
            if cmd[:3] == ["gh", "pr", "create"]:
                r.stdout = "https://github.com/judgemind/judgemind/pull/9001\n"
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

        # Mock the three phase subprocesses — each writes the expected
        # output file and returns (0, duration).
        phase_outputs = {
            "plan": _fixture_plan_output(),
            "ralph": _fixture_ralph_output(),
            "summary": _fixture_summary_output(),
        }

        def fake_spawn(phase: str, worktree: Path, agent_id: str) -> tuple[int, float]:
            output_dir_local = worktree / "tmp" / "dispatcher-output"
            output_dir_local.mkdir(parents=True, exist_ok=True)
            (output_dir_local / f"{phase}.json").write_text(
                json.dumps(phase_outputs[phase])
            )
            return 0, 0.1

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        # Run orchestration.
        d._claim_and_orchestrate_one()

        # --- Assertions ---

        # Candidate picked + claim succeeded.
        assert handler.events("candidate_picked") != []
        assert handler.events("claim_succeeded") != []

        # Worktree created.
        assert handler.events("worktree_created") != []

        # Three phase_succeeded events (plan, ralph, summary).
        phases_succeeded = [r.phase for r in handler.events("phase_succeeded")]
        assert phases_succeeded == ["plan", "ralph", "summary"]

        # PR opened event captured with PR number.
        pr_opened = handler.events("pr_opened")
        assert pr_opened and pr_opened[0].pr_number == 9001

        # dispatcher.agents INSERT (claim) happened once.
        agent_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.agents" in e[0]
        ]
        assert len(agent_inserts) == 1

        # dispatcher.phase_outputs INSERTs: 3 (one per phase).
        phase_output_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_outputs" in e[0]
        ]
        assert len(phase_output_inserts) == 3

        # dispatcher.phase_transitions INSERTs: 3.
        phase_transition_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_transitions" in e[0]
        ]
        assert len(phase_transition_inserts) == 3

        # Final UPDATE: status='running', phase='awaiting_ci', pr_number=9001.
        final_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "running" in e[1]
            and "awaiting_ci" in e[1]
        ]
        assert final_updates, (
            f"expected final UPDATE with (running, awaiting_ci, pr=9001); "
            f"got: {[e[1] for e in conn.cursor_instance.executed if 'UPDATE dispatcher.agents' in e[0]]}"
        )

        # git commands: worktree add, add -A, commit, push. All use
        # ``git -C <repo-or-worktree> <verb>`` shape, so just check the
        # verb appears anywhere in each command.
        git_cmds = [c for c in call_log if c and c[0] == "git"]
        flat = [" ".join(c) for c in git_cmds]
        assert any("worktree add" in s for s in flat)
        assert any("add -A" in s for s in flat)
        assert any(" commit" in s for s in flat)
        assert any(" push " in s for s in flat)


# --------------------------------------------------------------------------
# Branch: plan.go=false stops orchestration cleanly
# --------------------------------------------------------------------------


class TestPlanGoFalse:
    def test_plan_go_false_with_block_reason_marks_failed(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [([42],), None]

        monkeypatch.setattr(d, "_repo_root", lambda: tmp_path)
        fixed = uuid_mod.UUID("aabbccdd-eeff-0011-2233-445566778899")
        monkeypatch.setattr(daemon.uuid, "uuid4", lambda: fixed)

        plan_blocked = dict(_fixture_plan_output())
        plan_blocked["go"] = False
        plan_blocked["block_reason"] = "acceptance criteria need sharpening"

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if cmd[0].endswith("check-issue-author.sh"):
                r.stdout = "TRUSTED: ...\n"
                return r
            if cmd[:3] == ["gh", "issue", "view"]:
                r.stdout = json.dumps(
                    {
                        "number": 42,
                        "title": "T",
                        "body": "",
                        "labels": [],
                        "comments": [],
                    }
                )
                return r
            if "worktree" in cmd and "add" in cmd:
                add_idx = cmd.index("add")
                Path(cmd[add_idx + 1]).mkdir(parents=True, exist_ok=True)
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        def fake_spawn(phase: str, worktree: Path, agent_id: str) -> tuple[int, float]:
            (worktree / "tmp" / "dispatcher-output").mkdir(parents=True, exist_ok=True)
            (worktree / "tmp" / "dispatcher-output" / f"{phase}.json").write_text(
                json.dumps(plan_blocked)
            )
            return 0, 0.1

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        d._claim_and_orchestrate_one()

        # Plan executed; ralph/summary did NOT.
        phases_succeeded = [r.phase for r in handler.events("phase_succeeded")]
        assert phases_succeeded == ["plan"]
        assert handler.events("plan_go_false") != []
        # Final UPDATE sets status='failed' with phase='planning'.
        failed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "failed" in e[1]
        ]
        assert failed_updates

    def test_plan_go_false_no_block_reason_marks_succeeded(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [([42],), None]
        monkeypatch.setattr(d, "_repo_root", lambda: tmp_path)
        fixed = uuid_mod.UUID("aabbccdd-eeff-0011-2233-445566778899")
        monkeypatch.setattr(daemon.uuid, "uuid4", lambda: fixed)

        plan_nowork = dict(_fixture_plan_output())
        plan_nowork["go"] = False
        plan_nowork["block_reason"] = None  # no work needed

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if cmd[0].endswith("check-issue-author.sh"):
                r.stdout = "TRUSTED: ...\n"
                return r
            if cmd[:3] == ["gh", "issue", "view"]:
                r.stdout = json.dumps(
                    {
                        "number": 42,
                        "title": "T",
                        "body": "",
                        "labels": [],
                        "comments": [],
                    }
                )
                return r
            if "worktree" in cmd and "add" in cmd:
                add_idx = cmd.index("add")
                Path(cmd[add_idx + 1]).mkdir(parents=True, exist_ok=True)
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        def fake_spawn(phase: str, worktree: Path, agent_id: str) -> tuple[int, float]:
            (worktree / "tmp" / "dispatcher-output").mkdir(parents=True, exist_ok=True)
            (worktree / "tmp" / "dispatcher-output" / f"{phase}.json").write_text(
                json.dumps(plan_nowork)
            )
            return 0, 0.1

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        d._claim_and_orchestrate_one()

        # Final UPDATE sets status='succeeded'.
        succeeded_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "succeeded" in e[1]
        ]
        assert succeeded_updates


# --------------------------------------------------------------------------
# Branch: ralph verdict=BLOCKED stops orchestration cleanly
# --------------------------------------------------------------------------


class TestRalphBlocked:
    def test_ralph_blocked_marks_failed(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [([42],), None]
        monkeypatch.setattr(d, "_repo_root", lambda: tmp_path)
        fixed = uuid_mod.UUID("aabbccdd-eeff-0011-2233-445566778899")
        monkeypatch.setattr(daemon.uuid, "uuid4", lambda: fixed)

        ralph_blocked = dict(_fixture_ralph_output())
        ralph_blocked["verdict"] = "BLOCKED"
        ralph_blocked["block_reason"] = "max_iterations reached"

        outputs = {
            "plan": _fixture_plan_output(),
            "ralph": ralph_blocked,
        }

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if cmd[0].endswith("check-issue-author.sh"):
                r.stdout = "TRUSTED: ...\n"
                return r
            if cmd[:3] == ["gh", "issue", "view"]:
                r.stdout = json.dumps(
                    {
                        "number": 42,
                        "title": "T",
                        "body": "",
                        "labels": [],
                        "comments": [],
                    }
                )
                return r
            if "worktree" in cmd and "add" in cmd:
                add_idx = cmd.index("add")
                Path(cmd[add_idx + 1]).mkdir(parents=True, exist_ok=True)
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        def fake_spawn(phase: str, worktree: Path, agent_id: str) -> tuple[int, float]:
            (worktree / "tmp" / "dispatcher-output").mkdir(parents=True, exist_ok=True)
            (worktree / "tmp" / "dispatcher-output" / f"{phase}.json").write_text(
                json.dumps(outputs[phase])
            )
            return 0, 0.1

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        d._claim_and_orchestrate_one()

        # plan + ralph succeeded (phase subprocess exited 0) but ralph
        # returned verdict=BLOCKED so summary was not invoked.
        phases_succeeded = [r.phase for r in handler.events("phase_succeeded")]
        assert phases_succeeded == ["plan", "ralph"]
        assert handler.events("ralph_not_ship") != []
        failed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "failed" in e[1]
        ]
        assert failed_updates


# --------------------------------------------------------------------------
# Subprocess non-zero exit marks failed cleanly without crashing
# --------------------------------------------------------------------------


class TestSubprocessNonZeroExit:
    def test_plan_subprocess_nonzero_exit_marks_failed(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [([42],), None]
        monkeypatch.setattr(d, "_repo_root", lambda: tmp_path)
        fixed = uuid_mod.UUID("aabbccdd-eeff-0011-2233-445566778899")
        monkeypatch.setattr(daemon.uuid, "uuid4", lambda: fixed)

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if cmd[0].endswith("check-issue-author.sh"):
                r.stdout = "TRUSTED: ...\n"
                return r
            if cmd[:3] == ["gh", "issue", "view"]:
                r.stdout = json.dumps(
                    {
                        "number": 42,
                        "title": "T",
                        "body": "",
                        "labels": [],
                        "comments": [],
                    }
                )
                return r
            if "worktree" in cmd and "add" in cmd:
                add_idx = cmd.index("add")
                Path(cmd[add_idx + 1]).mkdir(parents=True, exist_ok=True)
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        def fake_spawn(phase: str, worktree: Path, agent_id: str) -> tuple[int, float]:
            # Simulate infra failure (claude-p crash).
            return 1, 0.1

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        d._claim_and_orchestrate_one()

        # Subprocess failure logged; no plan phase_output persisted;
        # agent marked failed.
        assert handler.events("subprocess_failed") != []
        phase_output_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_outputs" in e[0]
        ]
        assert phase_output_inserts == []


# --------------------------------------------------------------------------
# Race-lost path: _atomic_claim unique violation stops this tick's attempt
# --------------------------------------------------------------------------


class TestClaimRace:
    def test_unique_violation_skips_this_tick(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # Snapshot has one issue, not-attempted check returns None.
        conn.cursor_instance.fetch_queue = [([42],), None]
        monkeypatch.setattr(d, "_repo_root", lambda: tmp_path)

        # Trust check passes.
        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = "TRUSTED: ...\n"
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        # Make the INSERT raise UniqueViolation — another daemon won.
        original_execute = conn.cursor_instance.execute

        def execute_with_uv(sql: str, params: Any = None) -> None:
            if "INSERT INTO dispatcher.agents" in sql:
                raise psycopg.errors.UniqueViolation("dup")
            original_execute(sql, params)

        conn.cursor_instance.execute = execute_with_uv  # type: ignore[method-assign]

        # Also make sure we don't try to create a worktree on lost-race.
        monkeypatch.setattr(
            d,
            "_create_worktree",
            MagicMock(side_effect=AssertionError("should not create worktree")),
        )

        d._claim_and_orchestrate_one()

        assert handler.events("claim_lost") != []
        # No worktree created, no phase output persisted.
        phase_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_outputs" in e[0]
        ]
        assert phase_inserts == []
