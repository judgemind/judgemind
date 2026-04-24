"""Unit tests for the stale-rollup merge auto-unstick recovery (#2641).

Covers :meth:`DispatcherDaemon._merge_pr_and_advance`'s new branch
for the ``base branch policy prohibits the merge`` stderr signature
and the helper :meth:`DispatcherDaemon._try_auto_unstick_merge`.

The four scenarios the issue's acceptance criteria enumerate:

1. **First stale-rollup rejection** — the daemon detects the marker,
   pushes an empty commit, increments ``merge_unstick_attempts``, and
   leaves the agent in ``awaiting_ci``. The agent is NOT routed
   through ``_handle_agent_failure``.
2. **Second stale-rollup rejection** (budget exhausted) — routes
   through ``_handle_agent_failure`` under
   ``FAILURE_CATEGORY_MERGE_UNSTICK_EXHAUSTED``; no additional empty
   commit is pushed.
3. **Non-stale-rollup real CI failure** (different stderr) —
   preserves the pre-#2641 behaviour: log + return, no empty commit,
   no failure-row write. The next tick re-polls.
4. **SELECT row shape** — ``_list_advanceable_agents`` surfaces the
   new ``merge_unstick_attempts`` column so the merge handler has it
   in hand without a second round-trip.

All external calls (``gh pr merge``, ``git commit``, ``git push``,
psycopg) are mocked — no real GitHub / filesystem / DB writes.
"""

from __future__ import annotations

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
# Shared fakes — mirror the test_daemon_phase3b fixtures so the two test
# files can evolve independently (the phase3b fixtures are internal to
# that module).
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
    logger = logging.getLogger(f"dispatcher.test.merge_unstick.{id(tmp_path)}")
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


def _pr_status_green() -> dict[str, Any]:
    """Build a gh pr view payload that classifies as 'green'."""
    return {
        "statusCheckRollup": [
            {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "ci-passed"},
        ],
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "headRefOid": "head-sha",
        "mergeCommit": None,
    }


# --------------------------------------------------------------------------
# Constants + category wiring
# --------------------------------------------------------------------------


class TestConstants:
    def test_stale_rollup_marker_constant_defined(self) -> None:
        assert daemon.STALE_ROLLUP_STDERR_MARKER == (
            "base branch policy prohibits the merge"
        )

    def test_max_attempts_is_one(self) -> None:
        # Issue #2641 spec: "at most one auto-unstick attempt per
        # agent lifetime".
        assert daemon.MERGE_UNSTICK_MAX_ATTEMPTS == 1

    def test_merge_unstick_exhausted_is_tier_3(self) -> None:
        # Tier-3 categories route to the diagnoser on first occurrence
        # of the terminal, which is exactly what the spec requires for
        # the budget-exceeded path.
        assert (
            daemon.FAILURE_CATEGORY_MERGE_UNSTICK_EXHAUSTED in daemon.TIER_3_CATEGORIES
        )


# --------------------------------------------------------------------------
# _merge_pr_and_advance — stale-rollup → first unstick (happy path)
# --------------------------------------------------------------------------


class TestStaleRollupFirstUnstick:
    def test_first_stale_rollup_pushes_empty_commit_and_stays_awaiting_ci(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        call_log: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            call_log.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            if cmd[:3] == ["gh", "pr", "merge"]:
                # First merge attempt: the stale-rollup rejection.
                r.returncode = 1
                r.stderr = (
                    "X Pull request is not mergeable: the base branch "
                    "policy prohibits the merge.\n"
                )
                return r
            if cmd[:3] == ["git", "-C", str(tmp_path)]:
                # git commit --allow-empty AND git push both succeed.
                return r
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        monkeypatch.setattr(subprocess, "run", fake_run)
        agent = {
            "agent_id": "agent-stale-1",
            "issue_number": 2641,
            "phase": "awaiting_ci",
            "pr_number": 101,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
            "merge_unstick_attempts": 0,
        }

        d._merge_pr_and_advance(agent, _pr_status_green())

        # Structured log event fired for marker detection.
        detected = handler.events("merge_stale_rollup_detected")
        assert detected, "expected merge_stale_rollup_detected event"
        assert detected[0].pr_number == 101

        # Empty commit pushed.
        commit_cmds = [
            c
            for c in call_log
            if c[:3] == ["git", "-C", str(tmp_path)] and "commit" in c
        ]
        assert commit_cmds, "expected git commit --allow-empty"
        assert "--allow-empty" in commit_cmds[0]
        # The commit message references the issue so the trail is
        # grep-able in CloudWatch + git log.
        assert any("#2641" in a for a in commit_cmds[0])

        push_cmds = [
            c for c in call_log if c[:3] == ["git", "-C", str(tmp_path)] and "push" in c
        ]
        assert push_cmds, "expected git push after empty commit"

        # Success event fired.
        pushed = handler.events("merge_auto_unstick_empty_commit_pushed")
        assert pushed, "expected merge_auto_unstick_empty_commit_pushed event"
        assert pushed[0].new_attempts == 1

        # NO terminal transition — agent stays in awaiting_ci. The
        # handler does not call _handle_agent_failure, so there's no
        # dispatcher.failures INSERT and no 'failed' status update.
        failed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and any(x == "failed" for x in (e[1] if isinstance(e[1], tuple) else ()))
        ]
        assert failed_updates == []

        # Counter incremented on the agent row.
        counter_updates = [
            e for e in conn.cursor_instance.executed if "merge_unstick_attempts" in e[0]
        ]
        assert counter_updates, "expected merge_unstick_attempts UPDATE"

        # No pr_merged event — we did NOT merge.
        merged = handler.events("pr_merged")
        assert merged == []


# --------------------------------------------------------------------------
# _try_auto_unstick_merge — budget exhausted (second occurrence)
# --------------------------------------------------------------------------


class TestStaleRollupExhausted:
    def test_second_occurrence_routes_to_agent_failure(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        call_log: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            call_log.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            if cmd[:3] == ["gh", "pr", "merge"]:
                r.returncode = 1
                r.stderr = (
                    "X Pull request is not mergeable: the base branch "
                    "policy prohibits the merge.\n"
                )
                return r
            # On the exhausted path we must NOT see any git commit /
            # git push calls — the budget check short-circuits before
            # the empty-commit sequence.
            raise AssertionError(f"unexpected subprocess call on exhausted path: {cmd}")

        monkeypatch.setattr(subprocess, "run", fake_run)

        # Stub _handle_agent_failure so we can observe the routing
        # without hitting the real failure-row write path (which has
        # its own DB assumptions tested elsewhere).
        routed: list[dict[str, Any]] = []

        def fake_handle(
            *,
            agent_id: str,
            phase: str,
            category: str,
            stderr_tail: str,
            exit_code: int | None,
            details: dict[str, Any] | None = None,
            issue_number: int | None = None,
        ) -> None:
            routed.append(
                {
                    "agent_id": agent_id,
                    "phase": phase,
                    "category": category,
                    "stderr_tail": stderr_tail,
                    "exit_code": exit_code,
                    "details": details,
                    "issue_number": issue_number,
                }
            )

        d._handle_agent_failure = fake_handle  # type: ignore[method-assign]

        agent = {
            "agent_id": "agent-stale-2",
            "issue_number": 2641,
            "phase": "awaiting_ci",
            "pr_number": 202,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
            "merge_unstick_attempts": 1,  # already used the budget
        }

        d._merge_pr_and_advance(agent, _pr_status_green())

        # Marker still detected, event still fires.
        detected = handler.events("merge_stale_rollup_detected")
        assert detected, "expected merge_stale_rollup_detected event"
        assert detected[0].attempts_so_far == 1

        # Exhausted event fired.
        exhausted = handler.events("merge_unstick_exhausted")
        assert exhausted, "expected merge_unstick_exhausted event"

        # Routed through _handle_agent_failure with the right
        # category and phase.
        assert len(routed) == 1
        assert routed[0]["category"] == daemon.FAILURE_CATEGORY_MERGE_UNSTICK_EXHAUSTED
        assert routed[0]["phase"] == "awaiting_ci"
        assert routed[0]["agent_id"] == "agent-stale-2"
        assert routed[0]["issue_number"] == 2641
        assert routed[0]["details"]["pr_number"] == 202
        assert (
            routed[0]["details"]["stale_rollup_marker"]
            == daemon.STALE_ROLLUP_STDERR_MARKER
        )

        # NO empty commit attempted — the budget check short-circuits.
        commit_cmds = [c for c in call_log if "commit" in c]
        assert commit_cmds == []

        # NO success event.
        pushed = handler.events("merge_auto_unstick_empty_commit_pushed")
        assert pushed == []


# --------------------------------------------------------------------------
# _merge_pr_and_advance — real CI failure (non-stale-rollup stderr)
# --------------------------------------------------------------------------


class TestRealCiFailureStderrUnchanged:
    def test_non_stale_rollup_stderr_preserves_pre_2641_behaviour(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """A real CI failure (e.g. "some required check is failing")
        must NOT trigger the auto-unstick path. Pre-#2641 behaviour:
        log + return. The agent stays in awaiting_ci and the next
        tick re-polls — the rollup classifier will now see 'red' and
        route to fix-CI.
        """
        d, conn, handler = _make_daemon(tmp_path)

        call_log: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            call_log.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            if cmd[:3] == ["gh", "pr", "merge"]:
                r.returncode = 1
                r.stderr = (
                    "X Pull request is not mergeable: some required check is failing.\n"
                )
                return r
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        monkeypatch.setattr(subprocess, "run", fake_run)

        # Sentinel: _handle_agent_failure must NOT be called on this
        # path — that's part of the pre-#2641 contract.
        routed: list[dict[str, Any]] = []
        d._handle_agent_failure = lambda **kw: routed.append(kw)  # type: ignore[method-assign]

        agent = {
            "agent_id": "agent-real-ci-fail",
            "issue_number": 2641,
            "phase": "awaiting_ci",
            "pr_number": 303,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
            "merge_unstick_attempts": 0,
        }

        d._merge_pr_and_advance(agent, _pr_status_green())

        # The pre-#2641 pr_merge_failed log event still fires (same
        # shape as before — this IS the regression guard).
        pr_merge_failed = handler.events("pr_merge_failed")
        assert pr_merge_failed, "expected pre-#2641 pr_merge_failed log"

        # NO stale-rollup detected — the marker wasn't in stderr.
        assert handler.events("merge_stale_rollup_detected") == []

        # NO empty commit, no push.
        commit_cmds = [c for c in call_log if "commit" in c]
        assert commit_cmds == []

        # NO _handle_agent_failure — this path isn't terminal. (The
        # rollup classifier on the next tick will see 'red' once the
        # check has fully failed, and route to fix-CI — but that's
        # the fix-CI handler's job, not ours.)
        assert routed == []


# --------------------------------------------------------------------------
# _merge_pr_and_advance — empty-commit failure (push nonzero) route
# --------------------------------------------------------------------------


class TestUnstickPushFailureRoutes:
    def test_git_push_failure_after_empty_commit_routes_exhausted(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """If the empty-commit + push sequence itself fails, route
        through ``_handle_agent_failure`` under the same exhausted
        category — the recovery is broken. The ``sub_reason`` in
        details lets the diagnoser distinguish this from a fresh
        stale-rollup occurrence.
        """
        d, conn, handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            if cmd[:3] == ["gh", "pr", "merge"]:
                r.returncode = 1
                r.stderr = (
                    "X Pull request is not mergeable: the base branch "
                    "policy prohibits the merge.\n"
                )
                return r
            if cmd[:3] == ["git", "-C", str(tmp_path)] and "commit" in cmd:
                # git commit --allow-empty succeeds.
                return r
            if cmd[:3] == ["git", "-C", str(tmp_path)] and "push" in cmd:
                # git push FAILS — PAT scope / network / whatever.
                r.returncode = 1
                r.stderr = "remote: permission denied\n"
                return r
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        monkeypatch.setattr(subprocess, "run", fake_run)

        routed: list[dict[str, Any]] = []
        d._handle_agent_failure = lambda **kw: routed.append(kw)  # type: ignore[method-assign]

        agent = {
            "agent_id": "agent-push-break",
            "issue_number": 2641,
            "phase": "awaiting_ci",
            "pr_number": 404,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
            "merge_unstick_attempts": 0,
        }

        d._merge_pr_and_advance(agent, _pr_status_green())

        assert len(routed) == 1
        assert routed[0]["category"] == daemon.FAILURE_CATEGORY_MERGE_UNSTICK_EXHAUSTED
        assert "sub_reason" in routed[0]["details"]
        assert routed[0]["details"]["sub_reason"].startswith("empty_commit_push_")


# --------------------------------------------------------------------------
# _list_advanceable_agents — new merge_unstick_attempts column surfaced
# --------------------------------------------------------------------------


class TestListAdvanceableAgentsMergeUnstickAttempts:
    def test_select_includes_merge_unstick_attempts_column(
        self, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        # Seed one row with merge_unstick_attempts=0 and one with =1
        # so we can assert the handler receives the real value (not a
        # constant 0).
        conn.cursor_instance.fetchall_queue.append(
            [
                (
                    "agent-a",
                    100,
                    "awaiting_ci",
                    501,
                    str(tmp_path),
                    0,  # retries_used
                    "running",
                    0,  # merge_unstick_attempts
                ),
                (
                    "agent-b",
                    200,
                    "awaiting_ci",
                    502,
                    str(tmp_path),
                    0,
                    "running",
                    1,  # already used the unstick budget
                ),
            ]
        )

        agents = d._list_advanceable_agents()

        assert len(agents) == 2
        assert agents[0]["agent_id"] == "agent-a"
        assert agents[0]["merge_unstick_attempts"] == 0
        assert agents[1]["agent_id"] == "agent-b"
        assert agents[1]["merge_unstick_attempts"] == 1

        # The SELECT SQL must include the new column name.
        selects = [
            e[0]
            for e in conn.cursor_instance.executed
            if "FROM dispatcher.agents" in e[0]
        ]
        assert selects
        assert any("merge_unstick_attempts" in s for s in selects)

    def test_select_tolerates_missing_column_pre_migration(
        self, tmp_path: Path
    ) -> None:
        """During rollout, a pre-migration dispatcher.agents row may
        arrive with fewer columns than expected (e.g. if a test stubs
        the SELECT). The daemon must default to 0 rather than raise —
        the column default is 0, so treating a missing index the same
        way matches production semantics.
        """
        d, conn, _handler = _make_daemon(tmp_path)

        # Row shape WITHOUT the merge_unstick_attempts column (only
        # 7 fields) — simulates a legacy SELECT in a test fixture.
        conn.cursor_instance.fetchall_queue.append(
            [
                (
                    "agent-legacy",
                    100,
                    "awaiting_ci",
                    501,
                    str(tmp_path),
                    0,
                    "running",
                ),
            ]
        )

        agents = d._list_advanceable_agents()

        assert len(agents) == 1
        # Defaults to 0 — matches the column DEFAULT in migration 42.
        assert agents[0]["merge_unstick_attempts"] == 0


# --------------------------------------------------------------------------
# _increment_merge_unstick_attempts — DB write idempotency
# --------------------------------------------------------------------------


class TestIncrementMergeUnstickAttempts:
    def test_update_sql_shape(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        d._increment_merge_unstick_attempts("agent-xyz")

        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "merge_unstick_attempts" in e[0]
        ]
        assert len(updates) == 1
        # SET col = col + 1 shape (not a replace).
        assert "merge_unstick_attempts + 1" in updates[0][0]
        # Parameterized by agent_id.
        assert updates[0][1] == ("agent-xyz",)
        # Committed.
        assert conn.commits == 1

    def test_update_failure_rolls_back_and_logs(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def bad_execute(*_a: Any, **_kw: Any) -> None:
            raise RuntimeError("simulated DB hiccup")

        conn.cursor_instance.execute = bad_execute  # type: ignore[method-assign]

        # Must not raise.
        d._increment_merge_unstick_attempts("agent-xyz")

        # Rollback fired.
        assert conn.rollbacks == 1
        # Structured log event emitted.
        failed = handler.events("increment_merge_unstick_attempts_failed")
        assert failed, "expected increment_merge_unstick_attempts_failed event"


# --------------------------------------------------------------------------
# Migration 42 smoke — column default + SQL shape
# --------------------------------------------------------------------------


class TestMigrationSmoke:
    def test_migration_file_exists_and_has_up_and_down(self) -> None:
        """Migration for #2641 must have both Up and Down sections
        and the new column with a DEFAULT 0 NOT NULL.

        Lookup is by suffix so a rebase-induced rename (#2916
        migration-number collision dance) doesn't break the test.
        """
        migrations_dir = _SCRIPTS.parent / "packages" / "api" / "migrations"
        candidates = sorted(
            migrations_dir.glob("*_dispatcher-agent-merge-unstick-attempts.sql")
        )
        assert candidates, (
            f"missing migration under {migrations_dir} matching "
            "*_dispatcher-agent-merge-unstick-attempts.sql"
        )
        assert len(candidates) == 1, (
            f"expected exactly one #2641 migration, got {candidates}"
        )
        text = candidates[0].read_text()
        assert "-- Up Migration" in text
        assert "-- Down Migration" in text
        # Column added with NOT NULL + DEFAULT 0.
        assert "ADD COLUMN merge_unstick_attempts" in text
        assert "NOT NULL" in text
        assert "DEFAULT 0" in text
        # Column dropped on rollback.
        assert "DROP COLUMN IF EXISTS merge_unstick_attempts" in text
