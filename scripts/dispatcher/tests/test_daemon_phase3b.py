"""Unit tests for the Phase 3B post-PR orchestration in ``scripts.dispatcher.daemon``.

Issue #2787. Covers the transitions Phase 3B adds to the supervisor tick:

* ``awaiting_ci`` → pending (no-op)
* ``awaiting_ci`` → green (merge + advance to awaiting_deploy)
* ``awaiting_ci`` → red → fix-ci PATCHED (apply patch, push, retry)
* ``awaiting_ci`` → red → fix-ci BLOCKED (mark failed)
* ``awaiting_ci`` → red → retries_used >= FIX_CI_MAX_RETRIES (mark failed)
* ``awaiting_ci`` → red → fix-ci FLAKY (no-op)
* ``awaiting_deploy`` → in_progress (no-op)
* ``awaiting_deploy`` → success → verify VERIFIED → succeeded + evidence comment
* ``awaiting_deploy`` → success → verify SKIPPED (no deploy applicable)
* ``awaiting_deploy`` → failure (mark failed)
* Unhandled exception in one agent → status=crashed; next agent still processed.

All external calls (``gh``, ``git``, ``claude -p``, psycopg) are mocked. The
subprocess used by ``_spawn_phase_subprocess`` is stubbed so no real LLM
is invoked.
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


# Provide a stub ``psycopg`` module before importing the daemon — same
# pattern as test_daemon_phase3a.py. Reuse phase3a's stub when possible
# so the ``except psycopg.errors.UniqueViolation`` path in the daemon
# still resolves to the *same* Exception class both test files reference
# (otherwise test-collection order makes one file's raise fall through
# the other file's except and the unique-violation tests break — observed
# while developing #2787).

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

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — superset of the test_daemon_phase3a fixtures (adds
# fetchall() support to _FakeCursor so _list_advanceable_agents works).
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
    logger = logging.getLogger(f"dispatcher.test.phase3b.{id(tmp_path)}")
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


def _agent_row(
    agent_id: str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    issue_number: int = 42,
    phase: str = "awaiting_ci",
    pr_number: int | None = 101,
    worktree_path: str | None = None,
    retries_used: int = 0,
) -> tuple[Any, ...]:
    """Build a SELECT row in the shape _list_advanceable_agents expects."""
    return (
        agent_id,
        issue_number,
        phase,
        pr_number,
        worktree_path or "/tmp/test-worktree",
        retries_used,
    )


# --------------------------------------------------------------------------
# _classify_check_rollup — pure function
# --------------------------------------------------------------------------


class TestClassifyCheckRollup:
    def test_all_green_with_mergeable_clean_returns_green(self) -> None:
        status = {
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "lint"},
                {"status": "COMPLETED", "conclusion": "SKIPPED", "name": "docs"},
            ],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
        assert daemon.DispatcherDaemon._classify_check_rollup(status) == "green"

    def test_any_in_progress_returns_pending(self) -> None:
        status = {
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "lint"},
                {"status": "IN_PROGRESS", "name": "tests"},
            ],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
        assert daemon.DispatcherDaemon._classify_check_rollup(status) == "pending"

    def test_queued_returns_pending(self) -> None:
        status = {
            "statusCheckRollup": [
                {"status": "QUEUED", "name": "build"},
            ],
            "mergeable": "UNKNOWN",
            "mergeStateStatus": "UNKNOWN",
        }
        assert daemon.DispatcherDaemon._classify_check_rollup(status) == "pending"

    def test_any_failure_returns_red(self) -> None:
        status = {
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "lint"},
                {"status": "COMPLETED", "conclusion": "FAILURE", "name": "tests"},
            ],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
        assert daemon.DispatcherDaemon._classify_check_rollup(status) == "red"

    def test_all_green_but_not_mergeable_returns_red(self) -> None:
        # Branch protection rules not met, or still waiting on a required
        # review that CI alone cannot satisfy.
        status = {
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "lint"},
            ],
            "mergeable": "CONFLICTING",
            "mergeStateStatus": "DIRTY",
        }
        assert daemon.DispatcherDaemon._classify_check_rollup(status) == "red"

    def test_legacy_commit_status_pending(self) -> None:
        status = {
            "statusCheckRollup": [{"state": "PENDING"}],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
        assert daemon.DispatcherDaemon._classify_check_rollup(status) == "pending"

    def test_legacy_commit_status_failure(self) -> None:
        status = {
            "statusCheckRollup": [{"state": "FAILURE"}],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
        assert daemon.DispatcherDaemon._classify_check_rollup(status) == "red"

    def test_empty_rollup_with_mergeable_returns_green(self) -> None:
        # If there are literally no checks and the branch is mergeable,
        # treat as green. (Rare in practice since CI filters always run,
        # but the function must make a call.)
        status = {
            "statusCheckRollup": [],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
        assert daemon.DispatcherDaemon._classify_check_rollup(status) == "green"


# --------------------------------------------------------------------------
# _classify_deploy_runs — pure function
# --------------------------------------------------------------------------


class TestClassifyDeployRuns:
    def test_empty_returns_none(self) -> None:
        assert daemon.DispatcherDaemon._classify_deploy_runs([]) == "none"

    def test_in_progress_returns_pending(self) -> None:
        runs = [
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
            {"status": "IN_PROGRESS"},
        ]
        assert daemon.DispatcherDaemon._classify_deploy_runs(runs) == "pending"

    def test_all_success_returns_success(self) -> None:
        runs = [
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
            {"status": "COMPLETED", "conclusion": "SKIPPED"},
        ]
        assert daemon.DispatcherDaemon._classify_deploy_runs(runs) == "success"

    def test_any_failure_returns_failure(self) -> None:
        runs = [
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
            {"status": "COMPLETED", "conclusion": "FAILURE"},
        ]
        assert daemon.DispatcherDaemon._classify_deploy_runs(runs) == "failure"


# --------------------------------------------------------------------------
# _extract_merge_sha
# --------------------------------------------------------------------------


class TestExtractMergeSha:
    def test_returns_merge_commit_oid(self) -> None:
        status = {"mergeCommit": {"oid": "abc123"}}
        assert daemon.DispatcherDaemon._extract_merge_sha(status) == "abc123"

    def test_falls_back_to_head_ref_oid(self) -> None:
        status = {"mergeCommit": None, "headRefOid": "head456"}
        assert daemon.DispatcherDaemon._extract_merge_sha(status) == "head456"

    def test_returns_none_when_neither_set(self) -> None:
        assert daemon.DispatcherDaemon._extract_merge_sha({}) is None


# --------------------------------------------------------------------------
# _extract_failing_jobs
# --------------------------------------------------------------------------


class TestExtractFailingJobs:
    def test_returns_only_failed_checks(self) -> None:
        status = {
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "lint"},
                {"status": "COMPLETED", "conclusion": "FAILURE", "name": "tests"},
                {"status": "COMPLETED", "conclusion": "CANCELLED", "name": "build"},
            ]
        }
        failing = daemon.DispatcherDaemon._extract_failing_jobs(status)
        names = [f["name"] for f in failing]
        assert "tests" in names
        assert "build" in names
        assert "lint" not in names

    def test_caps_at_max(self) -> None:
        status = {
            "statusCheckRollup": [
                {
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                    "name": f"job{i}",
                }
                for i in range(20)
            ]
        }
        failing = daemon.DispatcherDaemon._extract_failing_jobs(status)
        assert len(failing) == daemon.FIX_CI_MAX_FAILING_JOBS


# --------------------------------------------------------------------------
# _extract_acceptance_criteria
# --------------------------------------------------------------------------


class TestExtractAcceptanceCriteria:
    def test_picks_up_checkbox_lines(self) -> None:
        body = (
            "Intro\n\n"
            "## Acceptance criteria\n\n"
            "- [ ] Criterion A\n"
            "- [x] Criterion B\n"
            "- Not a checkbox\n"
            "- [ ] Criterion C\n"
        )
        acs = daemon.DispatcherDaemon._extract_acceptance_criteria(body)
        assert acs == ["Criterion A", "Criterion B", "Criterion C"]

    def test_skips_test_plan_and_post_deploy_sections(self) -> None:
        body = (
            "## Acceptance criteria\n\n"
            "- [ ] Real criterion\n\n"
            "### Automated checks\n"
            "- [ ] Lint passes\n"
            "- [ ] Tests pass\n\n"
            "### Post-deploy verification\n"
            "- [ ] Evidence posted\n\n"
            "## Something Else\n"
            "- [ ] Another real criterion\n"
        )
        acs = daemon.DispatcherDaemon._extract_acceptance_criteria(body)
        assert acs == ["Real criterion", "Another real criterion"]


# --------------------------------------------------------------------------
# _infer_change_type
# --------------------------------------------------------------------------


class TestInferChangeType:
    def test_no_deploy_returns_no_deployed_component(self) -> None:
        assert daemon.DispatcherDaemon._infer_change_type([]) == "no_deployed_component"

    def test_deploy_api_maps_to_api(self) -> None:
        runs = [{"workflowName": "Deploy API"}]
        assert daemon.DispatcherDaemon._infer_change_type(runs) == "api"

    def test_deploy_scraper_maps_to_scraper(self) -> None:
        runs = [{"workflowName": "Deploy Scraper"}]
        assert daemon.DispatcherDaemon._infer_change_type(runs) == "scraper"


# --------------------------------------------------------------------------
# _list_advanceable_agents — DB query + error paths
# --------------------------------------------------------------------------


class TestListAdvanceableAgents:
    def test_returns_rows_as_dicts(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [
            [
                _agent_row(
                    "agent-1", issue_number=42, phase="awaiting_ci", pr_number=101
                ),
                _agent_row(
                    "agent-2",
                    issue_number=43,
                    phase="awaiting_deploy",
                    pr_number=102,
                ),
            ]
        ]
        rows = d._list_advanceable_agents()
        assert len(rows) == 2
        assert rows[0]["agent_id"] == "agent-1"
        assert rows[0]["phase"] == "awaiting_ci"
        assert rows[1]["phase"] == "awaiting_deploy"

    def test_db_error_returns_empty(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            raise RuntimeError("db lost")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        rows = d._list_advanceable_agents()
        assert rows == []
        assert conn.rollbacks >= 1

    def test_select_filters_on_kind_task(self, tmp_path: Path) -> None:
        """Regression for #2908.

        ``_list_advanceable_agents`` drives the daemon's Phase 3B/3E
        advance loop (CI watch, deploy watch, retro, cleanup). It should
        only advance ``kind='task'`` rows — task-skill rows never reach
        ``awaiting_ci``/``awaiting_deploy``/``retro_*`` in practice
        (their lifecycle ends with the operator's /task pipeline), but
        filtering defensively documents the scope and prevents a future
        phase-label collision from starving the advance loop.
        """
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [[]]
        d._list_advanceable_agents()

        selects = [
            e
            for e in conn.cursor_instance.executed
            if "SELECT agent_id" in e[0] and "dispatcher.agents" in e[0]
        ]
        assert selects, "expected _list_advanceable_agents to issue the SELECT"
        sql, _params = selects[0]
        assert "status = 'running'" in sql
        assert "kind = 'task'" in sql, (
            "_list_advanceable_agents must filter kind='task' so the "
            "advance loop is scoped to daemon-owned agents (see #2908). "
            "Actual SQL: " + sql
        )


# --------------------------------------------------------------------------
# _advance_running_agents — dispatch + crash isolation
# --------------------------------------------------------------------------


class TestAdvanceRunningAgents:
    def test_empty_list_returns_zero(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        d._list_advanceable_agents = lambda: []  # type: ignore[method-assign]
        assert d._advance_running_agents() == 0

    def test_routes_awaiting_ci_to_ci_handler(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        d._list_advanceable_agents = lambda: [  # type: ignore[method-assign]
            {
                "agent_id": "a1",
                "issue_number": 1,
                "phase": "awaiting_ci",
                "pr_number": 1,
                "worktree_path": str(tmp_path),
                "retries_used": 0,
            }
        ]
        called: dict[str, Any] = {}
        d._advance_awaiting_ci = lambda agent: called.setdefault(  # type: ignore[method-assign]
            "ci", agent
        )
        d._advance_awaiting_deploy = lambda agent: called.setdefault(  # type: ignore[method-assign]
            "deploy", agent
        )
        assert d._advance_running_agents() == 1
        assert "ci" in called
        assert "deploy" not in called

    def test_routes_awaiting_deploy_to_deploy_handler(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        d._list_advanceable_agents = lambda: [  # type: ignore[method-assign]
            {
                "agent_id": "a1",
                "issue_number": 1,
                "phase": "awaiting_deploy",
                "pr_number": 1,
                "worktree_path": str(tmp_path),
                "retries_used": 0,
            }
        ]
        called: dict[str, Any] = {}
        d._advance_awaiting_ci = lambda agent: called.setdefault(  # type: ignore[method-assign]
            "ci", agent
        )
        d._advance_awaiting_deploy = lambda agent: called.setdefault(  # type: ignore[method-assign]
            "deploy", agent
        )
        d._advance_running_agents()
        assert "deploy" in called
        assert "ci" not in called

    def test_exception_in_one_agent_crashes_that_one_continues_with_next(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        d._list_advanceable_agents = lambda: [  # type: ignore[method-assign]
            {
                "agent_id": "crasher",
                "issue_number": 1,
                "phase": "awaiting_ci",
                "pr_number": 1,
                "worktree_path": str(tmp_path),
                "retries_used": 0,
            },
            {
                "agent_id": "good",
                "issue_number": 2,
                "phase": "awaiting_ci",
                "pr_number": 2,
                "worktree_path": str(tmp_path),
                "retries_used": 0,
            },
        ]
        touched: list[str] = []

        def ci_handler(agent: dict[str, Any]) -> None:
            if agent["agent_id"] == "crasher":
                raise RuntimeError("boom")
            touched.append(agent["agent_id"])

        d._advance_awaiting_ci = ci_handler  # type: ignore[method-assign]
        d._advance_running_agents()
        # good agent was still processed.
        assert touched == ["good"]
        # crash event was logged with agent_id="crasher".
        crashes = handler.events("advance_failed")
        assert crashes
        assert crashes[0].agent_id == "crasher"
        # crasher got flipped to status=crashed (UPDATE with 'crashed' in params).
        crashed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "crashed" in e[1]
        ]
        assert crashed_updates


# --------------------------------------------------------------------------
# _advance_awaiting_ci — pending (no-op)
# --------------------------------------------------------------------------


class TestAwaitingCiPending:
    def test_pending_checks_are_noop(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            if cmd[:3] == ["gh", "pr", "view"]:
                r.stdout = json.dumps(
                    {
                        "statusCheckRollup": [{"status": "IN_PROGRESS"}],
                        "mergeable": "UNKNOWN",
                        "mergeStateStatus": "UNKNOWN",
                        "headRefOid": "head-sha",
                        "mergeCommit": None,
                    }
                )
                return r
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        monkeypatch.setattr(subprocess, "run", fake_run)
        agent = {
            "agent_id": "a1",
            "issue_number": 1,
            "phase": "awaiting_ci",
            "pr_number": 101,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
        }
        d._advance_awaiting_ci(agent)
        polls = handler.events("ci_poll")
        assert polls and polls[0].rollup_state == "pending"
        # No UPDATE dispatcher.agents on a no-op.
        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
        ]
        assert updates == []


# --------------------------------------------------------------------------
# _advance_awaiting_ci — green (merge + advance)
# --------------------------------------------------------------------------


class TestAwaitingCiGreen:
    def test_all_green_merges_and_advances(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        call_log: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            call_log.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            if cmd[:3] == ["gh", "pr", "view"]:
                r.stdout = json.dumps(
                    {
                        "statusCheckRollup": [
                            {"status": "COMPLETED", "conclusion": "SUCCESS"},
                        ],
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "head-sha",
                        "mergeCommit": {"oid": "merge-sha-abc"},
                    }
                )
                return r
            if cmd[:3] == ["gh", "pr", "merge"]:
                r.stdout = ""
                return r
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        monkeypatch.setattr(subprocess, "run", fake_run)
        agent = {
            "agent_id": "a1",
            "issue_number": 1,
            "phase": "awaiting_ci",
            "pr_number": 101,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
        }
        d._advance_awaiting_ci(agent)

        # gh pr merge was called with --squash --delete-branch.
        merge_cmds = [c for c in call_log if c[:3] == ["gh", "pr", "merge"]]
        assert merge_cmds
        assert "--squash" in merge_cmds[0]
        assert "--delete-branch" in merge_cmds[0]

        # Agent phase updated to awaiting_deploy.
        phase_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "awaiting_deploy" in e[1]
        ]
        assert phase_updates

        # pr_merged event logged with merge SHA.
        merged = handler.events("pr_merged")
        assert merged
        assert merged[0].merge_sha == "merge-sha-abc"


# --------------------------------------------------------------------------
# _advance_awaiting_ci — red → fix-ci PATCHED (apply + push + retry)
# --------------------------------------------------------------------------


class TestAwaitingCiRedPatched:
    def test_fix_ci_patched_applies_and_retries(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        # Use a real worktree directory so _write_phase_input etc. work.
        worktree = tmp_path
        (worktree / "tmp").mkdir(parents=True, exist_ok=True)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            if cmd[:3] == ["gh", "pr", "view"]:
                r.stdout = json.dumps(
                    {
                        "statusCheckRollup": [
                            {
                                "status": "COMPLETED",
                                "conclusion": "FAILURE",
                                "name": "tests",
                                "databaseId": 99999,
                            },
                        ],
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "head-sha",
                        "mergeCommit": None,
                    }
                )
                return r
            if cmd[:3] == ["gh", "pr", "diff"]:
                r.stdout = "diff --git a/x b/x\n+hi\n"
                return r
            # git add/commit/push all succeed.
            if cmd[:3] == ["git", "-C", str(worktree)]:
                return r
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        monkeypatch.setattr(subprocess, "run", fake_run)

        # fix-ci subprocess writes the PATCHED output.
        def fake_spawn(phase: str, wt: Path, agent_id: str) -> tuple[int, float]:
            out_dir = wt / "tmp" / "dispatcher-output"
            out_dir.mkdir(parents=True, exist_ok=True)
            assert phase == "fix-ci"
            (out_dir / "fix-ci.json").write_text(
                json.dumps(
                    {
                        "agent_id": agent_id,
                        "pr_number": 101,
                        "verdict": "PATCHED",
                        "failure_category": "lint",
                        "changed_files": ["a.py"],
                        "commit_message": "fix(x): lint (#101)",
                        "block_reason": None,
                        "flaky_evidence": None,
                        "notes": "",
                    }
                )
            )
            return 0, 0.1

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        agent = {
            "agent_id": "aabbccdd-eeff-0011-2233-445566778899",
            "issue_number": 42,
            "phase": "awaiting_ci",
            "pr_number": 101,
            "worktree_path": str(worktree),
            "retries_used": 0,
        }
        d._advance_awaiting_ci(agent)

        # fix_ci_started event.
        assert handler.events("fix_ci_started")
        # fix_ci_patched event (successful apply).
        patched = handler.events("fix_ci_patched")
        assert patched
        assert patched[0].new_retries_used == 1
        # retries_used UPDATE fired.
        retry_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "retries_used = retries_used + 1" in e[0]
        ]
        assert retry_updates
        # Agent NOT marked terminal (stays awaiting_ci).
        terminal_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and ("failed" in e[1] or "succeeded" in e[1] or "crashed" in e[1])
        ]
        assert terminal_updates == []


# --------------------------------------------------------------------------
# _advance_awaiting_ci — red → fix-ci BLOCKED (mark failed)
# --------------------------------------------------------------------------


class TestAwaitingCiRedBlocked:
    def test_fix_ci_blocked_marks_failed(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            if cmd[:3] == ["gh", "pr", "view"]:
                r.stdout = json.dumps(
                    {
                        "statusCheckRollup": [
                            {"status": "COMPLETED", "conclusion": "FAILURE"},
                        ],
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "head-sha",
                        "mergeCommit": None,
                    }
                )
                return r
            if cmd[:3] == ["gh", "pr", "diff"]:
                r.stdout = "diff"
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        def fake_spawn(phase: str, wt: Path, agent_id: str) -> tuple[int, float]:
            out_dir = wt / "tmp" / "dispatcher-output"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "fix-ci.json").write_text(
                json.dumps(
                    {
                        "agent_id": agent_id,
                        "pr_number": 101,
                        "verdict": "BLOCKED",
                        "failure_category": "missing_secret_or_config",
                        "changed_files": [],
                        "commit_message": "",
                        "block_reason": "Secret FOO not provisioned",
                        "flaky_evidence": None,
                        "notes": "",
                    }
                )
            )
            return 0, 0.1

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        agent = {
            "agent_id": "aabbccdd-eeff-0011-2233-445566778899",
            "issue_number": 42,
            "phase": "awaiting_ci",
            "pr_number": 101,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
        }
        d._advance_awaiting_ci(agent)

        # Fix-ci block event logged.
        assert handler.events("fix_ci_blocked")
        # Agent marked failed.
        failed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "failed" in e[1]
        ]
        assert failed_updates


# --------------------------------------------------------------------------
# _advance_awaiting_ci — red → FLAKY verdict is a no-op (no retry bump)
# --------------------------------------------------------------------------


class TestAwaitingCiRedFlaky:
    def test_fix_ci_flaky_is_noop(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            if cmd[:3] == ["gh", "pr", "view"]:
                r.stdout = json.dumps(
                    {
                        "statusCheckRollup": [
                            {"status": "COMPLETED", "conclusion": "FAILURE"},
                        ],
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "head-sha",
                        "mergeCommit": None,
                    }
                )
                return r
            if cmd[:3] == ["gh", "pr", "diff"]:
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        def fake_spawn(phase: str, wt: Path, agent_id: str) -> tuple[int, float]:
            out_dir = wt / "tmp" / "dispatcher-output"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "fix-ci.json").write_text(
                json.dumps(
                    {
                        "agent_id": agent_id,
                        "pr_number": 101,
                        "verdict": "FLAKY",
                        "failure_category": "infra_external",
                        "changed_files": [],
                        "commit_message": "",
                        "block_reason": None,
                        "flaky_evidence": "Anthropic 529 in tests",
                        "notes": "",
                    }
                )
            )
            return 0, 0.1

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        agent = {
            "agent_id": "a1",
            "issue_number": 42,
            "phase": "awaiting_ci",
            "pr_number": 101,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
        }
        d._advance_awaiting_ci(agent)
        # Flaky event logged.
        assert handler.events("fix_ci_flaky")
        # No terminal transitions, no retry bump.
        retry_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "retries_used = retries_used + 1" in e[0]
        ]
        assert retry_updates == []
        terminal_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and ("failed" in e[1] or "succeeded" in e[1])
        ]
        assert terminal_updates == []


# --------------------------------------------------------------------------
# _advance_awaiting_ci — red and retries_used >= MAX stops fix-ci
# --------------------------------------------------------------------------


class TestAwaitingCiMaxRetries:
    def test_max_retries_marks_failed_without_fix_ci(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = json.dumps(
                {
                    "statusCheckRollup": [
                        {"status": "COMPLETED", "conclusion": "FAILURE"},
                    ],
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "headRefOid": "head-sha",
                    "mergeCommit": None,
                }
            )
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        # Fail the test if fix-ci subprocess runs.
        def fake_spawn(*_a: Any, **_k: Any) -> tuple[int, float]:
            raise AssertionError("fix-ci must not spawn past max retries")

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        agent = {
            "agent_id": "a1",
            "issue_number": 42,
            "phase": "awaiting_ci",
            "pr_number": 101,
            "worktree_path": str(tmp_path),
            "retries_used": daemon.FIX_CI_MAX_RETRIES,
        }
        d._advance_awaiting_ci(agent)
        # Max-retries event logged + agent marked failed.
        assert handler.events("fix_ci_max_retries_exceeded")
        failed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "failed" in e[1]
        ]
        assert failed_updates


# --------------------------------------------------------------------------
# _advance_awaiting_deploy — in_progress → no-op
# --------------------------------------------------------------------------


class TestAwaitingDeployInProgress:
    def test_in_progress_is_noop(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            if cmd[:3] == ["gh", "pr", "view"]:
                r.stdout = json.dumps(
                    {
                        "statusCheckRollup": [],
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "head-sha",
                        "mergeCommit": {"oid": "merge-sha"},
                    }
                )
                return r
            if cmd[:3] == ["gh", "run", "list"]:
                r.stdout = json.dumps(
                    [
                        {
                            "databaseId": 111,
                            "workflowName": "Deploy API",
                            "status": "IN_PROGRESS",
                            "conclusion": None,
                            "createdAt": "2026-04-18T00:00:00Z",
                        }
                    ]
                )
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        agent = {
            "agent_id": "a1",
            "issue_number": 42,
            "phase": "awaiting_deploy",
            "pr_number": 101,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
        }
        d._advance_awaiting_deploy(agent)
        polls = handler.events("deploy_poll")
        assert polls and polls[0].deploy_state == "pending"
        # No terminal transitions.
        terminal_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and ("failed" in e[1] or "succeeded" in e[1])
        ]
        assert terminal_updates == []


# --------------------------------------------------------------------------
# _advance_awaiting_deploy — failure → mark failed
# --------------------------------------------------------------------------


class TestAwaitingDeployFailure:
    def test_deploy_failure_marks_failed(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            if cmd[:3] == ["gh", "pr", "view"]:
                r.stdout = json.dumps(
                    {
                        "statusCheckRollup": [],
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "head-sha",
                        "mergeCommit": {"oid": "merge-sha"},
                    }
                )
                return r
            if cmd[:3] == ["gh", "run", "list"]:
                r.stdout = json.dumps(
                    [
                        {
                            "databaseId": 111,
                            "workflowName": "Deploy API",
                            "status": "COMPLETED",
                            "conclusion": "FAILURE",
                            "createdAt": "2026-04-18T00:00:00Z",
                        }
                    ]
                )
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        agent = {
            "agent_id": "a1",
            "issue_number": 42,
            "phase": "awaiting_deploy",
            "pr_number": 101,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
        }
        d._advance_awaiting_deploy(agent)
        # Deploy failure event.
        assert handler.events("deploy_failed")
        failed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "failed" in e[1]
        ]
        assert failed_updates


# --------------------------------------------------------------------------
# _advance_awaiting_deploy — success → verify + evidence comment
# --------------------------------------------------------------------------


class TestAwaitingDeploySuccess:
    def test_deploy_success_runs_verify_and_posts_evidence(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        # The issue-bundle fetch happens inside _run_verify_and_complete.
        call_log: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            call_log.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            if cmd[:3] == ["gh", "pr", "view"]:
                r.stdout = json.dumps(
                    {
                        "statusCheckRollup": [],
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "head-sha",
                        "mergeCommit": {"oid": "merge-sha-abc"},
                    }
                )
                return r
            if cmd[:3] == ["gh", "run", "list"]:
                r.stdout = json.dumps(
                    [
                        {
                            "databaseId": 111,
                            "workflowName": "Deploy API",
                            "status": "COMPLETED",
                            "conclusion": "SUCCESS",
                            "createdAt": "2026-04-18T00:00:00Z",
                        }
                    ]
                )
                return r
            if cmd[:3] == ["gh", "issue", "view"]:
                r.stdout = json.dumps(
                    {
                        "number": 42,
                        "title": "T",
                        "body": (
                            "## Acceptance criteria\n\n"
                            "- [ ] API returns 200\n"
                            "- [ ] Response has expected field\n"
                        ),
                        "labels": [],
                        "comments": [],
                    }
                )
                return r
            if cmd[:3] == ["gh", "issue", "comment"]:
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        def fake_spawn(phase: str, wt: Path, agent_id: str) -> tuple[int, float]:
            out_dir = wt / "tmp" / "dispatcher-output"
            out_dir.mkdir(parents=True, exist_ok=True)
            assert phase == "verify"
            (out_dir / "verify.json").write_text(
                json.dumps(
                    {
                        "agent_id": agent_id,
                        "issue_number": 42,
                        "pr_number": 101,
                        "verdict": "VERIFIED",
                        "change_type": "api",
                        "evidence_md": (
                            "## Verification Evidence\n\n"
                            "**Change type:** api\n\n"
                            "Post-deploy verification: PASSED\n"
                        ),
                        "per_criterion_results": [],
                        "failure_reason": None,
                        "unblock_issues": [],
                    }
                )
            )
            return 0, 0.1

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        agent = {
            "agent_id": "a1",
            "issue_number": 42,
            "phase": "awaiting_deploy",
            "pr_number": 101,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
        }
        d._advance_awaiting_deploy(agent)

        # verify_started event.
        assert handler.events("verify_started")
        # Evidence comment was posted.
        assert handler.events("evidence_comment_posted")
        comment_cmds = [c for c in call_log if c[:3] == ["gh", "issue", "comment"]]
        assert comment_cmds
        assert "--body-file" in comment_cmds[0]
        # Agent marked succeeded.
        succeeded_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "succeeded" in e[1]
        ]
        assert succeeded_updates
        # agent_completed event.
        completed = handler.events("agent_completed")
        assert completed
        assert completed[0].merge_sha == "merge-sha-abc"
        assert completed[0].verdict == "VERIFIED"


# --------------------------------------------------------------------------
# _advance_awaiting_deploy — no deploy run (doc-only PR) → verify SKIPPED
# --------------------------------------------------------------------------


class TestAwaitingDeployNoDeployRun:
    def test_no_deploy_proceeds_to_verify_with_skip(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            if cmd[:3] == ["gh", "pr", "view"]:
                r.stdout = json.dumps(
                    {
                        "statusCheckRollup": [],
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "head-sha",
                        "mergeCommit": {"oid": "merge-sha"},
                    }
                )
                return r
            if cmd[:3] == ["gh", "run", "list"]:
                # No deploy workflows in the response — only CI.
                r.stdout = json.dumps(
                    [
                        {
                            "databaseId": 222,
                            "workflowName": "CI",
                            "status": "COMPLETED",
                            "conclusion": "SUCCESS",
                            "createdAt": "2026-04-18T00:00:00Z",
                        }
                    ]
                )
                return r
            if cmd[:3] == ["gh", "issue", "view"]:
                r.stdout = json.dumps(
                    {
                        "number": 42,
                        "title": "",
                        "body": "",
                        "labels": [],
                        "comments": [],
                    }
                )
                return r
            if cmd[:3] == ["gh", "issue", "comment"]:
                return r
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        def fake_spawn(phase: str, wt: Path, agent_id: str) -> tuple[int, float]:
            out_dir = wt / "tmp" / "dispatcher-output"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "verify.json").write_text(
                json.dumps(
                    {
                        "agent_id": agent_id,
                        "issue_number": 42,
                        "pr_number": 101,
                        "verdict": "SKIPPED",
                        "change_type": "no_deployed_component",
                        "evidence_md": "## Verification Evidence\n\nSkip reason: docs-only",
                        "per_criterion_results": [],
                        "failure_reason": None,
                        "unblock_issues": [],
                    }
                )
            )
            return 0, 0.1

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        agent = {
            "agent_id": "a1",
            "issue_number": 42,
            "phase": "awaiting_deploy",
            "pr_number": 101,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
        }
        d._advance_awaiting_deploy(agent)

        polls = handler.events("deploy_poll")
        assert polls and polls[0].deploy_state == "none"
        # Verify still ran and agent marked succeeded.
        succeeded_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "succeeded" in e[1]
        ]
        assert succeeded_updates


# --------------------------------------------------------------------------
# supervisor_tick integration — advance_running_agents is called
# --------------------------------------------------------------------------


class TestSupervisorTickIntegration:
    def test_supervisor_tick_invokes_advance(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # First fetchone() = failures count.
        conn.cursor_instance.fetch_queue = [(0,)]
        # No agents to advance (fetchall returns []).
        conn.cursor_instance.fetchall_queue = [[]]
        # Stub out the CloudWatch client so no real boto3 import happens.
        d._cloudwatch_client = MagicMock()

        advance_calls: dict[str, int] = {"n": 0}

        def wrapper() -> int:
            advance_calls["n"] += 1
            return 0

        d._advance_running_agents = wrapper  # type: ignore[method-assign]
        summary = d.supervisor_tick()
        assert advance_calls["n"] == 1
        assert summary["agents_advanced"] == 0
        # Regression: tick log now includes agents_advanced.
        ticks = handler.events("supervisor_tick")
        assert ticks
        assert getattr(ticks[0], "agents_advanced", None) == 0

    def test_supervisor_tick_survives_advance_exception(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]
        d._cloudwatch_client = MagicMock()

        def boom() -> int:
            raise RuntimeError("advance blew up")

        d._advance_running_agents = boom  # type: ignore[method-assign]
        # Tick must not raise — the heartbeat + metric emission still run.
        summary = d.supervisor_tick()
        assert handler.events("advance_pass_failed")
        assert summary["agents_advanced"] == 0
