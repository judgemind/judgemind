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


from dispatcher import daemon  # noqa: E402  — sys.path mutation above
from dispatcher import phase_transitions  # noqa: E402


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
# _ci_rollup_state — pure function (canonical classifier, phase_transitions)
# --------------------------------------------------------------------------


class TestCiRollupState:
    def test_all_green_with_mergeable_clean_returns_green(self) -> None:
        status = {
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "lint"},
                {"status": "COMPLETED", "conclusion": "SKIPPED", "name": "docs"},
            ],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
        assert phase_transitions._ci_rollup_state(status) == "green"

    def test_any_in_progress_returns_pending(self) -> None:
        status = {
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "lint"},
                {"status": "IN_PROGRESS", "name": "tests"},
            ],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
        assert phase_transitions._ci_rollup_state(status) == "pending"

    def test_queued_returns_pending(self) -> None:
        status = {
            "statusCheckRollup": [
                {"status": "QUEUED", "name": "build"},
            ],
            "mergeable": "UNKNOWN",
            "mergeStateStatus": "UNKNOWN",
        }
        assert phase_transitions._ci_rollup_state(status) == "pending"

    def test_any_failure_returns_red(self) -> None:
        status = {
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "lint"},
                {"status": "COMPLETED", "conclusion": "FAILURE", "name": "tests"},
            ],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
        assert phase_transitions._ci_rollup_state(status) == "red"

    def test_all_green_but_dirty_returns_red(self) -> None:
        # CONFLICTING / DIRTY is a true merge conflict requiring a code-side
        # rebase (#3431). The canonical classifier returns 'red' so callers
        # route to fix_conflict rather than treating it as a transient poll.
        status = {
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "lint"},
            ],
            "mergeable": "CONFLICTING",
            "mergeStateStatus": "DIRTY",
        }
        assert phase_transitions._ci_rollup_state(status) == "red"

    def test_unknown_returns_pending(self) -> None:
        # Regression guard for AC #3 (#3431): UNKNOWN is a transient GitHub
        # recompute state, NOT a merge conflict. Must stay 'pending' so the
        # supervisor re-polls rather than routing to fix_conflict.
        status = {
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "lint"},
            ],
            "mergeable": "UNKNOWN",
            "mergeStateStatus": "UNKNOWN",
        }
        assert phase_transitions._ci_rollup_state(status) == "pending"

    def test_legacy_commit_status_pending(self) -> None:
        status = {
            "statusCheckRollup": [{"__typename": "STATUSCONTEXT", "state": "PENDING"}],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
        assert phase_transitions._ci_rollup_state(status) == "pending"

    def test_legacy_commit_status_failure(self) -> None:
        status = {
            "statusCheckRollup": [{"__typename": "STATUSCONTEXT", "state": "FAILURE"}],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
        assert phase_transitions._ci_rollup_state(status) == "red"

    def test_empty_rollup_with_mergeable_returns_green(self) -> None:
        # If there are literally no checks and the branch is mergeable,
        # treat as green. (Rare in practice since CI filters always run,
        # but the function must make a call.)
        status = {
            "statusCheckRollup": [],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
        assert phase_transitions._ci_rollup_state(status) == "green"


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
# DEPLOY_WORKFLOW_NAMES contents — regression guard (#3185)
# --------------------------------------------------------------------------


class TestDeployWorkflowNamesContents:
    def test_deploy_agent_runner_present(self) -> None:
        assert "Deploy Agent Runner" in daemon.DEPLOY_WORKFLOW_NAMES

    def test_existing_entries_present(self) -> None:
        for name in (
            "Deploy API",
            "Deploy Dispatcher",
            "Deploy Scraper",
            "Deploy Production",
            "Deploy Production (Web)",
            "Terraform",
        ):
            assert name in daemon.DEPLOY_WORKFLOW_NAMES, (
                f"{name!r} missing from DEPLOY_WORKFLOW_NAMES"
            )


# --------------------------------------------------------------------------
# _find_deploy_runs — regression guard: no --workflow flag (#3514)
# --------------------------------------------------------------------------


class TestFindDeployRuns_MultiWorkflowGuard:
    """Regression guard (#3514): daemon._find_deploy_runs must NOT pass any
    ``--workflow`` flag to ``gh run list``.

    The ``-w``/``--workflow`` flag is single-valued; when multiple flags are
    passed only the LAST value is honoured, silently dropping all other
    workflows.  The ECS entrypoint had this bug (fixed in the same PR).
    This class ensures daemon.py's ``_find_deploy_runs`` never regresses to
    the entrypoint.sh pattern.
    """

    def test_no_workflow_flag_in_gh_run_list(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        captured_cmds: list[list[str]] = []

        def fake_run(
            cmd: list[str], *args: Any, **kwargs: Any
        ) -> subprocess.CompletedProcess:
            captured_cmds.append(list(cmd))
            r: subprocess.CompletedProcess = subprocess.CompletedProcess(cmd, 0)
            r.stdout = "[]"
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d, _conn, _handler = _make_daemon(tmp_path)
        d._find_deploy_runs("deadbeefcafe0011")

        # At least one ``gh run list`` call must have been made.
        gh_run_list_calls = [c for c in captured_cmds if c[:3] == ["gh", "run", "list"]]
        assert gh_run_list_calls, (
            "_find_deploy_runs must invoke 'gh run list'; no such call captured"
        )
        # None of the gh run list calls may contain --workflow.
        for cmd in gh_run_list_calls:
            assert "--workflow" not in cmd, (
                f"daemon._find_deploy_runs passed '--workflow' to gh run list — "
                f"this reintroduces the #3514 bug where only the last workflow "
                f"is matched.  Post-filter by workflowName instead.  cmd={cmd}"
            )


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

    def test_select_has_no_kind_filter(self, tmp_path: Path) -> None:
        """Post-#2927 regression: ``_list_advanceable_agents`` has no kind filter.

        The /task skill stopped writing to ``dispatcher.agents``
        (label-only coordination), so every row the advance loop
        sees is daemon-owned by construction. The #2908
        ``kind='task'`` carve-out was reverted — the SELECT is back
        to its pre-#2866 shape.
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
        assert "kind = 'task'" not in sql, (
            "_list_advanceable_agents should not carry a kind filter "
            "post-#2927 — every row is daemon-owned. Actual SQL: " + sql
        )

    def test_select_excludes_null_issue_number(self, tmp_path: Path) -> None:
        """Issue #3394 regression: SELECT filters out ``issue_number IS NULL`` rows.

        PR #3381 made ``dispatcher.agents.issue_number`` nullable so
        scheduled-skill agents (/audit, /spotcheck spawned via #3379)
        can run without a closing issue. Those agents are owned end-to-
        end by ``agent-runner-entrypoint.sh`` — the daemon's
        supervisor advance loop must NOT pick them up.

        Pre-fix, the dict-construction at the top of
        ``_list_advanceable_agents`` called ``int(row[1])``
        unconditionally. When a scheduled-skill row reached
        ``status='succeeded' phase='done'`` (which matches the SELECT),
        ``int(None)`` raised ``TypeError`` every supervisor tick. The
        outer ``except`` swallowed it and returned ``[]``, silently
        halting ALL advance-loop work while the offending row sat in
        the table. This is a daemon-shipped-greens throughput killer.

        The fix filters NULL out at the SQL layer so no downstream
        code has to None-check. This test guards the WHERE clause.
        """
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [[]]
        d._list_advanceable_agents()

        selects = [
            sql
            for sql, _params in conn.cursor_instance.executed
            if "SELECT agent_id" in sql and "FROM dispatcher.agents" in sql
        ]
        assert selects, "expected _list_advanceable_agents to issue the SELECT"
        sql = selects[-1]
        assert "issue_number IS NOT NULL" in sql, (
            "_list_advanceable_agents must filter NULL issue_number rows "
            "(scheduled-skill agents from #3379) at the SQL layer to avoid "
            "int(None) crashing the supervisor tick (#3394). Actual SQL: " + sql
        )

    def test_null_issue_number_row_does_not_crash_dict_construction(
        self, tmp_path: Path
    ) -> None:
        """Issue #3394 belt-and-suspenders: even if a NULL row reaches the
        dict-construction loop (e.g. via a test fake bypassing the SQL filter
        or a race where DB filtering is lost), the production code path must
        not raise an unhandled ``TypeError`` that escapes
        ``_list_advanceable_agents``.

        With option-1 (filter at SQL) chosen as the fix, this test
        documents the *contract*: a NULL ``issue_number`` row reaching
        the loop is a bug upstream, but the immediate symptom (returning
        ``[]`` and halting the advance loop) must be visible — not
        silently swallowed mid-iteration corrupting later rows.

        Today the bare-except wrapping the loop catches it and returns
        ``[]``; this test pins that behavior so any future refactor that
        moves the loop outside the ``try`` will have to make a deliberate
        choice about handling the NULL.
        """
        d, conn, _handler = _make_daemon(tmp_path)
        # Bypass the SQL filter at the fake-cursor layer to simulate a row
        # that would have crashed the pre-fix code path.
        null_issue_row = (
            "agent-null-issue",
            None,  # issue_number
            "done",
            None,  # pr_number
            "/tmp/wt",
            0,
            "succeeded",
            0,
            "ecs",
        )
        conn.cursor_instance.fetchall_queue = [[null_issue_row]]
        # Must not raise — outer except returns [] on TypeError.
        rows = d._list_advanceable_agents()
        assert rows == []
        assert conn.rollbacks >= 1


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
            # Issue #2953: ``_write_merged_at`` releases the
            # ``status/in-progress`` label on successful merge
            # (matches ``_mark_agent_terminal`` teardown path).
            if cmd[:3] == ["gh", "issue", "edit"] and "--remove-label" in cmd:
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
# _advance_awaiting_ci — DIRTY/CONFLICTING → terminal+diagnoser (#3431)
# --------------------------------------------------------------------------


class TestAwaitingCiConflict:
    def test_dirty_pr_routes_to_conflict_unresolvable(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """#3431: mergeStateStatus=DIRTY is a true merge conflict.

        _advance_awaiting_ci must route it to terminal+diagnoser via
        FAILURE_CATEGORY_CONFLICT_UNRESOLVABLE, not stay pending or
        spawn fix-ci.
        """
        d, conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = json.dumps(
                {
                    "statusCheckRollup": [
                        {"status": "COMPLETED", "conclusion": "SUCCESS"},
                    ],
                    "mergeable": "CONFLICTING",
                    "mergeStateStatus": "DIRTY",
                    "headRefOid": "head-sha",
                    "mergeCommit": None,
                }
            )
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        # Fail the test if fix-ci subprocess spawns (it should not).
        def fake_spawn(*_a: Any, **_k: Any) -> tuple[int, float]:
            raise AssertionError("fix-ci must not spawn for a DIRTY conflict")

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        agent = {
            "agent_id": "conflict-agent-id",
            "issue_number": 42,
            "phase": "awaiting_ci",
            "pr_number": 101,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
        }
        d._advance_awaiting_ci(agent)

        # Agent marked failed (terminal + diagnoser path via _handle_agent_failure).
        failed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "failed" in e[1]
        ]
        assert failed_updates, "DIRTY PR must mark agent terminal with status=failed"
        # failures row written with conflict_unresolvable category.
        failures_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT" in e[0] and "dispatcher.failures" in e[0]
        ]
        assert failures_inserts, (
            "DIRTY PR must write a dispatcher.failures row with conflict_unresolvable"
        )


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
        # Issue #2953: ``status='succeeded'`` was written at merge time
        # by ``_write_merged_at``; the verify-complete path NO longer
        # re-writes status. It writes ``verified_at = now()`` (milestone
        # column) and advances ``phase`` to ``done``.
        verified_at_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "verified_at" in e[0]
        ]
        assert verified_at_updates
        phase_done_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "SET phase" in e[0]
            and e[1] is not None
            and "done" in e[1]
        ]
        assert phase_done_updates
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
        # Issue #2953: verify writes ``verified_at`` + advances phase
        # to ``done``; status flip to ``succeeded`` happens at merge
        # (not called directly in this test path). Here we assert the
        # verified-at milestone was stamped and phase advanced.
        verified_at_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "verified_at" in e[0]
        ]
        assert verified_at_updates
        phase_done_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "SET phase" in e[0]
            and e[1] is not None
            and "done" in e[1]
        ]
        assert phase_done_updates


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


# --------------------------------------------------------------------------
# #3055 — verify-phase spawn cwd + recovery short-circuit on merged agents
# --------------------------------------------------------------------------


class TestVerifyPhaseSpawnCwd:
    """Issue #3055 (verify) + #3065 (retro generalization) — on the
    recovery path (force-new-deployment drops the per-agent worktree from
    ephemeral storage), every post-merge phase spawn must still discover
    its ``.claude/skills/task-v2-<phase>/SKILL.md``. Paralleling #3034's
    diagnoser fix, the fix sets ``cwd=baseline_repo_root`` for every
    phase listed in
    :data:`dispatcher.daemon.POST_MERGE_PHASES_USING_BASELINE_CWD`
    (currently ``verify`` and ``retro``) in Fargate mode. Pre-merge
    phases continue to run inside the per-agent worktree because they
    need the working tree available for git operations."""

    def test_verify_spawn_uses_baseline_repo_root_as_cwd(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """In Fargate mode (``baseline_repo_root`` set), the verify phase
        subprocess must be launched with ``cwd=str(baseline_repo_root)`` so
        the ``task-v2-verify`` skill is discoverable even when the per-agent
        worktree was wiped by a container restart."""
        from dispatcher.tests._popen_fake import make_popen_factory

        d, _conn, _handler = _make_daemon(tmp_path)
        baseline = tmp_path / "baseline"
        baseline.mkdir()
        d._cfg.baseline_repo_root = baseline

        worktree = tmp_path / "wt"
        worktree.mkdir()

        captured: dict[str, Any] = {}

        def on_start(cmd: list[str], kwargs: dict[str, Any]) -> None:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

        monkeypatch.setattr(
            subprocess,
            "Popen",
            make_popen_factory(
                stdout_chunks=('{"result":"ok","is_error":false}\n',),
                on_start=on_start,
            ),
        )
        # ``_spawn_phase_subprocess`` calls ``_agent_issue_number`` which
        # hits the DB; short-circuit it to avoid stubbing the whole cursor
        # state for this narrowly-scoped assertion.
        monkeypatch.setattr(d, "_agent_issue_number", lambda _aid: 42)

        d._spawn_phase_subprocess("verify", worktree, "agent-id")

        assert "cwd" in captured["kwargs"], (
            "Popen must be invoked with cwd= so the /task-v2-verify skill "
            f"is discoverable (got kwargs={sorted(captured['kwargs'])})"
        )
        assert captured["kwargs"]["cwd"] == str(baseline), (
            f"Popen cwd for verify must be the baseline_repo_root. "
            f"Expected {baseline!s}, got {captured['kwargs']['cwd']!r}"
        )

    def test_verify_spawn_falls_back_to_worktree_in_local_mode(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """In local-dev mode (``baseline_repo_root`` unset), the verify phase
        spawn must still pass ``cwd=str(worktree)`` — the per-agent worktree
        IS the skill-containing tree in local dev because ``_compute_worktree_path``
        falls back to ``<repo_root>/.claude/worktrees/agent-*``."""
        from dispatcher.tests._popen_fake import make_popen_factory

        d, _conn, _handler = _make_daemon(tmp_path)
        assert d._cfg.baseline_repo_root is None  # local-dev mode

        worktree = tmp_path / "wt"
        worktree.mkdir()

        captured: dict[str, Any] = {}

        def on_start(cmd: list[str], kwargs: dict[str, Any]) -> None:
            captured["kwargs"] = kwargs

        monkeypatch.setattr(
            subprocess,
            "Popen",
            make_popen_factory(
                stdout_chunks=('{"result":"ok","is_error":false}\n',),
                on_start=on_start,
            ),
        )
        monkeypatch.setattr(d, "_agent_issue_number", lambda _aid: 42)

        d._spawn_phase_subprocess("verify", worktree, "agent-id")

        assert captured["kwargs"]["cwd"] == str(worktree), (
            f"Popen cwd for verify in local-dev mode must be the worktree. "
            f"Expected {worktree!s}, got {captured['kwargs']['cwd']!r}"
        )

    def test_retro_spawn_uses_baseline_repo_root_as_cwd(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Issue #3065 — generalization of #3055 to the retro phase.
        In Fargate mode (``baseline_repo_root`` set), the retro phase
        subprocess must be launched with ``cwd=str(baseline_repo_root)``
        so the ``task-v2-retro`` skill is discoverable even when the
        per-agent worktree was wiped by a container restart between
        merge and retro."""
        from dispatcher.tests._popen_fake import make_popen_factory

        d, _conn, _handler = _make_daemon(tmp_path)
        baseline = tmp_path / "baseline"
        baseline.mkdir()
        d._cfg.baseline_repo_root = baseline

        worktree = tmp_path / "wt"
        worktree.mkdir()

        captured: dict[str, Any] = {}

        def on_start(cmd: list[str], kwargs: dict[str, Any]) -> None:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

        monkeypatch.setattr(
            subprocess,
            "Popen",
            make_popen_factory(
                stdout_chunks=('{"result":"ok","is_error":false}\n',),
                on_start=on_start,
            ),
        )
        monkeypatch.setattr(d, "_agent_issue_number", lambda _aid: 42)

        d._spawn_phase_subprocess("retro", worktree, "agent-id")

        assert captured["kwargs"]["cwd"] == str(baseline), (
            f"Popen cwd for retro must be the baseline_repo_root. "
            f"Expected {baseline!s}, got {captured['kwargs']['cwd']!r}"
        )

    def test_retro_spawn_falls_back_to_worktree_in_local_mode(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """In local-dev mode (``baseline_repo_root`` unset), the retro
        phase spawn must still pass ``cwd=str(worktree)`` — the per-agent
        worktree IS the skill-containing tree in local dev."""
        from dispatcher.tests._popen_fake import make_popen_factory

        d, _conn, _handler = _make_daemon(tmp_path)
        assert d._cfg.baseline_repo_root is None  # local-dev mode

        worktree = tmp_path / "wt"
        worktree.mkdir()

        captured: dict[str, Any] = {}

        def on_start(cmd: list[str], kwargs: dict[str, Any]) -> None:
            captured["kwargs"] = kwargs

        monkeypatch.setattr(
            subprocess,
            "Popen",
            make_popen_factory(
                stdout_chunks=('{"result":"ok","is_error":false}\n',),
                on_start=on_start,
            ),
        )
        monkeypatch.setattr(d, "_agent_issue_number", lambda _aid: 42)

        d._spawn_phase_subprocess("retro", worktree, "agent-id")

        assert captured["kwargs"]["cwd"] == str(worktree), (
            f"Popen cwd for retro in local-dev mode must be the worktree. "
            f"Expected {worktree!s}, got {captured['kwargs']['cwd']!r}"
        )

    def test_pre_merge_phases_always_use_worktree_as_cwd(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Regression guard: the #3055 / #3065 generalization must NOT
        change the cwd for ``plan`` / ``ralph`` / ``summary`` / ``fix-ci``
        spawns. Those phases run pre-merge inside a live worktree, need
        the worktree's working tree for git operations, and are not
        subject to the recovery-path bug (the worktree can't be wiped
        while the agent is still pre-merge and advancing).

        Extend the tuple below if a new pre-merge phase is added. Do
        not add post-merge phases here — those belong in
        :data:`dispatcher.daemon.POST_MERGE_PHASES_USING_BASELINE_CWD`
        and in the positive-side per-phase tests above."""
        from dispatcher.daemon import POST_MERGE_PHASES_USING_BASELINE_CWD
        from dispatcher.tests._popen_fake import make_popen_factory

        d, _conn, _handler = _make_daemon(tmp_path)
        baseline = tmp_path / "baseline"
        baseline.mkdir()
        d._cfg.baseline_repo_root = baseline  # Fargate mode

        worktree = tmp_path / "wt"
        worktree.mkdir()

        captured_cwds: dict[str, str] = {}

        def make_on_start(
            phase: str,
        ) -> Any:
            def on_start(cmd: list[str], kwargs: dict[str, Any]) -> None:
                captured_cwds[phase] = kwargs.get("cwd", "")

            return on_start

        monkeypatch.setattr(d, "_agent_issue_number", lambda _aid: 42)
        # Avoid firing the ralph head watcher during ralph-phase spawns
        # — it spawns a thread that polls git and would complicate the
        # assertion set.
        monkeypatch.setattr(d, "_start_ralph_head_watcher", lambda **_kwargs: None)

        pre_merge_phases = ("plan", "ralph", "summary", "fix-ci")
        # Belt-and-braces: make sure the tuple above and the post-merge
        # set are disjoint, so a future edit adding a phase to both
        # surfaces as a test failure rather than silent drift.
        assert set(pre_merge_phases) & POST_MERGE_PHASES_USING_BASELINE_CWD == set(), (
            f"Pre-merge and post-merge phase sets must not overlap. "
            f"Intersection: "
            f"{set(pre_merge_phases) & POST_MERGE_PHASES_USING_BASELINE_CWD}"
        )

        for phase in pre_merge_phases:
            monkeypatch.setattr(
                subprocess,
                "Popen",
                make_popen_factory(
                    stdout_chunks=('{"result":"ok","is_error":false}\n',),
                    on_start=make_on_start(phase),
                ),
            )
            d._spawn_phase_subprocess(phase, worktree, "agent-id")

        for phase, cwd in captured_cwds.items():
            assert cwd == str(worktree), (
                f"Pre-merge phase {phase!r} must run in worktree cwd, "
                f"not the baseline. Got cwd={cwd!r}, expected {worktree!s}."
            )

    def test_post_merge_phase_set_membership(self) -> None:
        """Issue #3065 — the module-level frozenset is the single source
        of truth for which phases run post-merge. Lock in the current
        membership (``verify``, ``retro``) so the generalization is not
        silently undone, and adding a new post-merge phase is an
        intentional edit accompanied by a test update."""
        from dispatcher.daemon import POST_MERGE_PHASES_USING_BASELINE_CWD

        assert POST_MERGE_PHASES_USING_BASELINE_CWD == frozenset({"verify", "retro"}), (
            "POST_MERGE_PHASES_USING_BASELINE_CWD membership changed. If "
            "intentional, update this assertion AND add a per-phase "
            "cwd test for the new member. See #3065 for rationale."
        )


class TestVerifyRecoveryShortCircuit:
    """Issue #3055 — when the daemon restarts mid-awaiting_deploy and the
    supervisor tick re-enters :meth:`_run_verify_and_complete` on an agent
    whose PR has already merged, the behaviour must be:

    1. If ``verified_at`` is already stamped, skip the subprocess entirely
       and advance phase → done (the prior verify ran; the phase advance
       was what the crashed daemon missed).
    2. If ``verified_at`` is NULL but ``merged_at`` is stamped, and the
       verify subprocess fails (phase_output_missing, nonzero exit,
       timeout), do NOT flip the row to ``status='failed'``. Restore
       status='succeeded' and advance phase → done. The agent row's
       ``merged_at`` is authoritative — a post-merge verify infra
       failure is bookkeeping noise, not a product regression, and
       must not tip the admin cockpit colour from green ✓ to red ✗.
    """

    def _agent(
        self,
        tmp_path: Path,
        *,
        phase: str = "awaiting_deploy",
        pr_number: int = 3051,
    ) -> dict[str, Any]:
        worktree = tmp_path / "wt"
        worktree.mkdir(exist_ok=True)
        return {
            "agent_id": "cdf48cc7",
            "issue_number": 3051,
            "phase": phase,
            "pr_number": pr_number,
            "worktree_path": str(worktree),
            "retries_used": 0,
            "status": "succeeded",
        }

    def test_verified_at_already_stamped_short_circuits_to_done(
        self, tmp_path: Path
    ) -> None:
        """When ``verified_at`` is already stamped, re-entering
        ``_run_verify_and_complete`` must skip the subprocess and just
        advance phase to ``done`` so retro picks up the row."""
        d, conn, handler = _make_daemon(tmp_path)
        agent = self._agent(tmp_path)
        pr_status: dict[str, Any] = {}
        deploy_runs: list[dict[str, Any]] = []

        # _read_verify_skip_reason fetches first (returns None → no skip).
        # _read_merged_at_and_verified_at fetches second (merged=True,
        # verified=True).
        conn.cursor_instance.fetch_queue = [
            (None,),  # verify_skip_reason
            (True, True),  # (merged_at IS NOT NULL, verified_at IS NOT NULL)
        ]

        # Subprocess MUST NOT be spawned on the short-circuit path.
        spawn_called: dict[str, int] = {"n": 0}

        def forbid_spawn(*_a: Any, **_kw: Any) -> None:
            spawn_called["n"] += 1

        d._spawn_phase_subprocess = forbid_spawn  # type: ignore[method-assign]

        d._run_verify_and_complete(agent, pr_status, "merge-sha", deploy_runs)

        assert spawn_called["n"] == 0, (
            "Re-entering verify on an already-verified agent must not "
            "spawn the subprocess again"
        )
        # The short-circuit logs verify_already_completed AND advances
        # phase to ``done`` — both are required for the admin cockpit
        # to render the row correctly.
        assert handler.events("verify_already_completed")
        phase_done_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "SET phase" in e[0]
            and e[1] is not None
            and "done" in e[1]
        ]
        assert phase_done_updates, (
            "Short-circuit must still advance phase=done so retro runs "
            "on the next supervisor tick"
        )

    def test_phase_output_missing_on_merged_agent_preserves_succeeded(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """When the verify subprocess returns exit 0 but fails to write
        ``verify.json`` (the #3055 recovery-path failure mode — claude
        CLI ran but the skill was not resolvable in the post-restart
        worktree), the pre-#3055 behaviour was to call
        :meth:`_handle_agent_failure` which flipped the row to
        ``status='failed'`` and handed it to the Opus diagnoser. On a
        merged PR that's wrong — restore ``status='succeeded'`` and
        advance phase → ``done`` instead."""
        d, conn, handler = _make_daemon(tmp_path)
        agent = self._agent(tmp_path)
        pr_status: dict[str, Any] = {}
        deploy_runs: list[dict[str, Any]] = []

        # First: _read_verify_skip_reason returns None.
        # Second: _read_merged_at_and_verified_at returns (True, False) —
        # PR merged but verify has never run.
        conn.cursor_instance.fetch_queue = [
            (None,),
            (True, False),
        ]

        # Short-circuit the input-assembly helpers so we land directly
        # on the phase_output_missing branch.
        d._fetch_issue_bundle = MagicMock(  # type: ignore[method-assign]
            return_value={
                "issue_number": agent["issue_number"],
                "issue_title": "t",
                "issue_body": "- [ ] criterion",
                "issue_comments": [],
                "issue_labels": [],
                "blocked_by": [],
                "parent_issue": None,
            }
        )
        d._select_deploy_status = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._infer_change_type = MagicMock(return_value="dx")  # type: ignore[method-assign]
        d._touched_services_from_runs = MagicMock(return_value=[])  # type: ignore[method-assign]
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        # The output file is not written — simulate the #3055 failure.
        d._read_phase_output = MagicMock(return_value=None)  # type: ignore[method-assign]
        # Guard rail: the failure handler must NOT be called on the
        # merged-agent path.
        handle_failure_calls: list[Any] = []

        def forbid_handle_failure(**kwargs: Any) -> None:
            handle_failure_calls.append(kwargs)

        d._handle_agent_failure = forbid_handle_failure  # type: ignore[method-assign]

        d._run_verify_and_complete(agent, pr_status, "merge-sha", deploy_runs)

        assert not handle_failure_calls, (
            "Must NOT flip status=failed on a phase_output_missing "
            "verify failure when the PR has already merged"
        )
        # The dedicated observability event must fire.
        assert handler.events("verify_infra_failure_post_merge")
        # The row must be restored to status=succeeded + phase=done.
        restore_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "SET status = 'succeeded'" in e[0]
            and "phase = 'done'" in e[0]
        ]
        assert restore_updates, (
            "Must restore status='succeeded' + phase='done' on the "
            "merged-agent recovery failure path"
        )

    def test_phase_output_missing_on_unmerged_agent_still_flips_failed(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Regression guard: on the rare ``merged_at IS NULL`` code path
        (pre-#2953 agents, or a state inconsistency), the original
        failure routing via :meth:`_handle_agent_failure` must still
        fire. Otherwise we'd silently hide genuine verify regressions
        for agents whose merge-time columns somehow never got stamped."""
        d, conn, _handler = _make_daemon(tmp_path)
        agent = self._agent(tmp_path)
        pr_status: dict[str, Any] = {}
        deploy_runs: list[dict[str, Any]] = []

        conn.cursor_instance.fetch_queue = [
            (None,),
            (False, False),  # not merged, not verified
        ]

        d._fetch_issue_bundle = MagicMock(  # type: ignore[method-assign]
            return_value={
                "issue_number": agent["issue_number"],
                "issue_title": "t",
                "issue_body": "- [ ] criterion",
                "issue_comments": [],
                "issue_labels": [],
                "blocked_by": [],
                "parent_issue": None,
            }
        )
        d._select_deploy_status = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._infer_change_type = MagicMock(return_value="dx")  # type: ignore[method-assign]
        d._touched_services_from_runs = MagicMock(return_value=[])  # type: ignore[method-assign]
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
        d._read_phase_output = MagicMock(return_value=None)  # type: ignore[method-assign]

        handle_failure_calls: list[Any] = []

        def record_handle_failure(**kwargs: Any) -> None:
            handle_failure_calls.append(kwargs)

        d._handle_agent_failure = record_handle_failure  # type: ignore[method-assign]

        d._run_verify_and_complete(agent, pr_status, "merge-sha", deploy_runs)

        assert handle_failure_calls, (
            "Legacy (unmerged) phase_output_missing path must still "
            "route through _handle_agent_failure so genuine failures "
            "reach the Opus diagnoser"
        )
        assert handle_failure_calls[0]["category"] == (
            daemon.FAILURE_CATEGORY_PHASE_OUTPUT_MISSING
        )


# --------------------------------------------------------------------------
# #3468 — _phase_io_root / _write_phase_input / _read_phase_output alignment
# --------------------------------------------------------------------------


class TestVerifyPhaseIoRoot:
    """Issue #3468 — post-merge phases (verify, retro) run with
    ``cwd=baseline_repo_root`` in Fargate mode, but the pre-#3468 IO
    helpers always wrote/read relative to the worktree.  The helpers now
    use :meth:`~daemon.DispatcherDaemon._phase_io_root` to select the
    correct root so paths stay aligned with the subprocess cwd.

    Tests in this class cover the verify phase; the retro-phase mirror
    lives in ``test_daemon_phase3e.TestRetroPhaseIoRoot``."""

    def test_verify_input_written_to_baseline_repo_root_for_post_merge_subprocess(
        self, tmp_path: Path
    ) -> None:
        """In Fargate mode (``baseline_repo_root`` set) the input JSON for
        ``verify`` must be written under ``{baseline_repo_root}/tmp/
        dispatcher-input/verify.json`` — that is the path the subprocess
        resolves when it reads ``tmp/dispatcher-input/verify.json`` from its
        cwd."""
        d, _conn, _handler = _make_daemon(tmp_path)
        baseline = tmp_path / "baseline"
        baseline.mkdir()
        d._cfg.baseline_repo_root = baseline

        worktree = tmp_path / "wt"
        worktree.mkdir()

        payload = {"pr_number": 42, "agent_id": "test-agent"}
        d._write_phase_input(worktree, "verify", payload)

        expected = baseline / "tmp" / "dispatcher-input" / "verify.json"
        assert expected.exists(), (
            f"Input JSON must be written to baseline_repo_root: {expected}"
        )
        assert json.loads(expected.read_text()) == payload

    def test_verify_input_also_present_in_worktree_for_post_merge(
        self, tmp_path: Path
    ) -> None:
        """Belt-and-suspenders: ``_write_phase_input`` for a post-merge
        phase must ALSO write to ``{worktree}/tmp/dispatcher-input/
        verify.json`` so that acceptance-criteria checks that look at the
        worktree path continue to pass (AC #1)."""
        d, _conn, _handler = _make_daemon(tmp_path)
        baseline = tmp_path / "baseline"
        baseline.mkdir()
        d._cfg.baseline_repo_root = baseline

        worktree = tmp_path / "wt"
        worktree.mkdir()

        payload = {"pr_number": 42, "agent_id": "test-agent"}
        d._write_phase_input(worktree, "verify", payload)

        wt_path = worktree / "tmp" / "dispatcher-input" / "verify.json"
        assert wt_path.exists(), (
            f"Input JSON must also be written to worktree for AC #1: {wt_path}"
        )
        assert json.loads(wt_path.read_text()) == payload

    def test_verify_read_phase_output_prefers_baseline_root(
        self, tmp_path: Path
    ) -> None:
        """When the verify subprocess writes its output to
        ``{baseline_repo_root}/tmp/dispatcher-output/verify.json``
        (because cwd=baseline_repo_root), ``_read_phase_output`` must
        return that content rather than returning ``None`` because the
        worktree-relative path is empty."""
        d, _conn, _handler = _make_daemon(tmp_path)
        baseline = tmp_path / "baseline"
        baseline.mkdir()
        d._cfg.baseline_repo_root = baseline

        worktree = tmp_path / "wt"
        worktree.mkdir()

        # Write output only at the baseline path — worktree path is absent.
        out_dir = baseline / "tmp" / "dispatcher-output"
        out_dir.mkdir(parents=True)
        expected_output = {"verdict": "VERIFIED", "evidence": "curl 200"}
        (out_dir / "verify.json").write_text(json.dumps(expected_output))

        result = d._read_phase_output(worktree, "verify")
        assert result == expected_output, (
            f"_read_phase_output must prefer the baseline-root path; got {result!r}"
        )

    def test_verify_read_phase_output_falls_back_to_worktree(
        self, tmp_path: Path
    ) -> None:
        """Fallback: when the baseline-root output file is absent (e.g. ECS
        mode where subprocess writes relative to worktree), ``_read_phase_output``
        must return the worktree-relative file's contents."""
        d, _conn, _handler = _make_daemon(tmp_path)
        # No baseline_repo_root → local-dev / ECS mode.
        assert d._cfg.baseline_repo_root is None

        worktree = tmp_path / "wt"
        worktree.mkdir()

        out_dir = worktree / "tmp" / "dispatcher-output"
        out_dir.mkdir(parents=True)
        expected_output = {"verdict": "VERIFIED", "evidence": "log line"}
        (out_dir / "verify.json").write_text(json.dumps(expected_output))

        result = d._read_phase_output(worktree, "verify")
        assert result == expected_output, (
            f"_read_phase_output must fall back to worktree path; got {result!r}"
        )

    def test_plan_input_only_written_to_worktree_not_baseline(
        self, tmp_path: Path
    ) -> None:
        """Regression guard: non-post-merge phases (``plan``) must NOT
        dual-write to ``baseline_repo_root``.  Only the worktree path is
        written, so the IO-root selection has no side-effect on pre-merge
        phases even when ``baseline_repo_root`` is configured."""
        d, _conn, _handler = _make_daemon(tmp_path)
        baseline = tmp_path / "baseline"
        baseline.mkdir()
        d._cfg.baseline_repo_root = baseline

        worktree = tmp_path / "wt"
        worktree.mkdir()

        payload = {"issue_number": 99}
        d._write_phase_input(worktree, "plan", payload)

        # Worktree path written.
        wt_path = worktree / "tmp" / "dispatcher-input" / "plan.json"
        assert wt_path.exists(), f"Plan input must be written to worktree: {wt_path}"

        # Baseline path must NOT be written for a pre-merge phase.
        baseline_path = baseline / "tmp" / "dispatcher-input" / "plan.json"
        assert not baseline_path.exists(), (
            f"Plan input must NOT be written to baseline_repo_root: {baseline_path}"
        )
