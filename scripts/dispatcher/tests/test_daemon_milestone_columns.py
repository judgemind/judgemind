"""Unit tests for the milestone-column split on ``dispatcher.agents``.

Issue #2953, migration 35. Covers the new write paths the daemon
gained when ``status='succeeded'`` was moved from end-of-retro to
merge-detection and three independent milestone columns
(``merged_at``, ``verified_at``, ``verify_skip_reason``,
``retroed_at``) were stamped in place:

* ``_write_merged_at`` — flips status → succeeded + stamps merged_at
  atomically at merge time; mirrors ``_mark_agent_terminal``
  best-effort side-effects.
* ``_write_verified_at`` — stamps verified_at without touching status.
* ``_write_verify_skip_reason`` + ``_read_verify_skip_reason`` — round-
  trip the skip reason column.
* ``_write_retroed_at`` — stamps retroed_at.
* ``_detect_verify_skip_reason`` — pure-functional file-list classifier.
* ``_push_and_open_pr`` — end-to-end: a dispatcher-touching commit gets
  ``verify_skip_reason='self_deploy'`` written pre-push.
* ``_merge_pr_and_advance`` — writes ``merged_at`` + flips status.
* ``_run_verify_and_complete`` — short-circuits on skip reason; on
  VERIFIED/SKIPPED stamps verified_at without re-flipping status.
* ``_run_retro_phase`` — stamps retroed_at on success.
* ``_list_advanceable_agents`` — filter picks up both
  ``status='running'`` AND ``status='succeeded'`` rows across phases.

All external calls (``gh``, ``git``, ``claude -p``, psycopg) are mocked.
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


from dispatcher import daemon  # noqa: E402


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
    logger = logging.getLogger(f"dispatcher.test.milestone.{id(tmp_path)}")
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
# _detect_verify_skip_reason — pure-functional file-list classifier
# --------------------------------------------------------------------------


class TestDetectVerifySkipReason:
    def test_self_deploy_detected_for_dispatcher_path(self) -> None:
        touched = ["scripts/dispatcher/daemon.py"]
        assert (
            daemon.DispatcherDaemon._detect_verify_skip_reason(touched)
            == daemon.VERIFY_SKIP_REASON_SELF_DEPLOY
        )

    def test_self_deploy_detected_for_dispatcher_subpath(self) -> None:
        touched = ["scripts/dispatcher/tests/test_daemon_phase3b.py"]
        assert (
            daemon.DispatcherDaemon._detect_verify_skip_reason(touched)
            == daemon.VERIFY_SKIP_REASON_SELF_DEPLOY
        )

    def test_no_skip_when_only_non_dispatcher_files_touched(self) -> None:
        touched = [
            "packages/web/src/app/(main)/admin/page.tsx",
            "docs/agent/code-standards.md",
        ]
        assert daemon.DispatcherDaemon._detect_verify_skip_reason(touched) is None

    def test_self_deploy_detected_when_any_file_matches(self) -> None:
        """A PR touching both dispatcher AND unrelated code still skips verify."""
        touched = [
            "packages/api/src/graphql/schema.ts",
            "scripts/dispatcher/daemon.py",
            "docs/README.md",
        ]
        assert (
            daemon.DispatcherDaemon._detect_verify_skip_reason(touched)
            == daemon.VERIFY_SKIP_REASON_SELF_DEPLOY
        )

    def test_empty_file_list_returns_none(self) -> None:
        assert daemon.DispatcherDaemon._detect_verify_skip_reason([]) is None

    def test_similarly_named_path_does_not_false_positive(self) -> None:
        """``scripts/dispatcher_audit.sh`` (no trailing slash) does NOT match."""
        touched = ["scripts/dispatcher_audit.sh"]
        assert daemon.DispatcherDaemon._detect_verify_skip_reason(touched) is None


# --------------------------------------------------------------------------
# _write_merged_at — flips status='succeeded', stamps merged_at
# --------------------------------------------------------------------------


class TestWriteMergedAt:
    def test_writes_status_succeeded_and_merged_at_in_one_update(
        self, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # Best-effort side-effect paths are stubbed — we assert the
        # primary UPDATE here; sibling tests cover the side effects.
        d._write_diagnosis_outcome_for_agent = MagicMock()  # type: ignore[method-assign]
        d._write_terminal_outcome = MagicMock()  # type: ignore[method-assign]
        d._evaluate_circuit_breaker = MagicMock()  # type: ignore[method-assign]
        d._gh_issue_remove_labels = MagicMock()  # type: ignore[method-assign]

        d._write_merged_at("aaaa-bbbb", pr_number=123, issue_number=42)

        primary_updates = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in sql and "merged_at" in sql
        ]
        assert primary_updates
        sql, params = primary_updates[0]
        assert "status = 'succeeded'" in sql
        assert "merged_at = now()" in sql
        # Ensure the COALESCE guards don't clobber existing ended_at /
        # exit_code values — the SQL uses COALESCE, not assignment.
        assert "COALESCE(ended_at" in sql
        assert "COALESCE(exit_code" in sql
        # failure_summary is cleared in the same statement (matches
        # the existing ``_mark_agent_terminal`` succeeded path for
        # the #2913 fix).
        assert "failure_summary = NULL" in sql
        assert params == (123, "aaaa-bbbb")

    def test_runs_best_effort_side_effects_on_success(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        d._write_diagnosis_outcome_for_agent = MagicMock()  # type: ignore[method-assign]
        d._write_terminal_outcome = MagicMock()  # type: ignore[method-assign]
        d._evaluate_circuit_breaker = MagicMock()  # type: ignore[method-assign]
        d._gh_issue_remove_labels = MagicMock()  # type: ignore[method-assign]

        d._write_merged_at("aaaa-bbbb", pr_number=99, issue_number=17)

        d._write_diagnosis_outcome_for_agent.assert_called_once_with(
            "aaaa-bbbb", "succeeded"
        )
        d._write_terminal_outcome.assert_called_once_with("aaaa-bbbb", "succeeded")
        d._evaluate_circuit_breaker.assert_called_once_with("aaaa-bbbb")
        d._gh_issue_remove_labels.assert_called_once_with(
            17, [daemon.STATUS_IN_PROGRESS_LABEL]
        )

    def test_does_not_remove_label_when_issue_number_is_none(
        self, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        d._write_diagnosis_outcome_for_agent = MagicMock()  # type: ignore[method-assign]
        d._write_terminal_outcome = MagicMock()  # type: ignore[method-assign]
        d._evaluate_circuit_breaker = MagicMock()  # type: ignore[method-assign]
        d._gh_issue_remove_labels = MagicMock()  # type: ignore[method-assign]

        d._write_merged_at("aaaa-bbbb", pr_number=99)

        d._gh_issue_remove_labels.assert_not_called()

    def test_side_effect_failure_does_not_propagate(self, tmp_path: Path) -> None:
        """A failure in a best-effort side-effect must not unwind the flip."""
        d, _conn, handler = _make_daemon(tmp_path)
        d._write_diagnosis_outcome_for_agent = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("diagnoses table not yet migrated")
        )
        d._write_terminal_outcome = MagicMock()  # type: ignore[method-assign]
        d._evaluate_circuit_breaker = MagicMock()  # type: ignore[method-assign]
        d._gh_issue_remove_labels = MagicMock()  # type: ignore[method-assign]

        # Does not raise.
        d._write_merged_at("aaaa-bbbb", pr_number=99, issue_number=17)

        # The subsequent side-effects still run because each one is
        # wrapped in its own try/except.
        d._write_terminal_outcome.assert_called_once()
        d._evaluate_circuit_breaker.assert_called_once()
        # And the diagnosis-outcome failure was logged.
        failures = handler.events("diagnosis_outcome_write_failed")
        assert failures
        assert failures[0].origin == "merged_at"


# --------------------------------------------------------------------------
# _write_verified_at / _write_retroed_at — simple milestone stamps
# --------------------------------------------------------------------------


class TestWriteMilestoneStamps:
    def test_write_verified_at_sets_only_that_column(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        d._write_verified_at("aaaa-bbbb")
        updates = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in sql
        ]
        assert len(updates) == 1
        sql, params = updates[0]
        assert "verified_at = now()" in sql
        # Must not touch status / phase / ended_at.
        assert "status" not in sql
        assert "phase" not in sql
        assert "ended_at" not in sql
        assert params == ("aaaa-bbbb",)
        # Single atomic milestone write: exactly one commit.
        assert conn.commits == 1

    def test_write_retroed_at_sets_only_that_column(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        d._write_retroed_at("aaaa-bbbb")
        updates = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in sql
        ]
        assert len(updates) == 1
        sql, params = updates[0]
        assert "retroed_at = now()" in sql
        assert "status" not in sql
        assert "phase" not in sql
        assert params == ("aaaa-bbbb",)


# --------------------------------------------------------------------------
# _write_verify_skip_reason / _read_verify_skip_reason
# --------------------------------------------------------------------------


class TestVerifySkipReasonRoundTrip:
    def test_write_then_read_round_trip(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        d._write_verify_skip_reason("aaaa-bbbb", "self_deploy")
        write_updates = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in sql and "verify_skip_reason" in sql
        ]
        assert write_updates
        _sql, params = write_updates[0]
        assert params == ("self_deploy", "aaaa-bbbb")

    def test_read_returns_string_when_present(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("self_deploy",)]
        result = d._read_verify_skip_reason("aaaa-bbbb")
        assert result == "self_deploy"

    def test_read_returns_none_for_empty_row(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(None,)]
        assert d._read_verify_skip_reason("aaaa-bbbb") is None

    def test_read_returns_none_when_no_row(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        # fetch_queue is empty → fetchone returns None.
        assert d._read_verify_skip_reason("aaaa-bbbb") is None


# --------------------------------------------------------------------------
# _push_and_open_pr — self-deploy detection writes verify_skip_reason pre-push
# --------------------------------------------------------------------------


class TestSelfDeployDetectionPrePush:
    def test_dispatcher_touching_commit_writes_self_deploy_skip_reason(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        agent_id = "aaaabbbb-0000-0000-0000-000000000001"

        # Stub DB phase updates so we don't need to care about them.
        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._current_attempt_for = MagicMock(return_value=0)  # type: ignore[method-assign]

        d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
            return_value={
                "commit_message": "feat(dispatcher): split terminal success (#2953)",
                "pr_title": "feat(dispatcher): split terminal success",
                "pr_body_md": "body",
                "unmet_criteria": [],
            }
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        # Issue #2971: subprocess sequence is now
        # [commit_amend, git_show, push, pr_create]. The amend rewrites
        # ralph's "WIP: ralph output" placeholder commit with summary's
        # conventional-commits message; no separate ``git add -A`` runs.
        commit_ok = subprocess.CompletedProcess(
            args=["git", "commit", "--amend"], returncode=0, stdout="", stderr=""
        )
        # git show --name-only HEAD returns a list of files where the
        # first one is in ``scripts/dispatcher/`` — should trigger
        # self_deploy skip reason.
        git_show = subprocess.CompletedProcess(
            args=["git", "show"],
            returncode=0,
            stdout=(
                "scripts/dispatcher/daemon.py\n"
                "packages/api/migrations/35_dispatcher-agents-milestone-columns.sql\n"
            ),
            stderr="",
        )
        push_ok = subprocess.CompletedProcess(
            args=["git", "push"], returncode=0, stdout="", stderr=""
        )
        pr_create_ok = subprocess.CompletedProcess(
            args=["gh", "pr", "create"],
            returncode=0,
            stdout="https://github.com/x/y/pull/2953\n",
            stderr="",
        )

        from unittest.mock import patch

        rev_list_ahead = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="1\n", stderr=""
        )
        # Issue #2964: pre-push fetch+rebase inserted before git push.
        fetch_ok = subprocess.CompletedProcess(
            args=["git", "fetch", "origin", "main"], returncode=0, stdout="", stderr=""
        )
        rebase_ok = subprocess.CompletedProcess(
            args=["git", "rebase", "origin/main"],
            returncode=0,
            stdout="Current branch is up to date.",
            stderr="",
        )
        with patch(
            "subprocess.run",
            side_effect=[
                rev_list_ahead,
                commit_ok,
                git_show,
                fetch_ok,
                rebase_ok,
                push_ok,
                pr_create_ok,
            ],
        ):
            d._push_and_open_pr(agent_id=agent_id, issue_number=2953, worktree=worktree)

        # verify_skip_reason UPDATE was written before push.
        skip_updates = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in sql and "verify_skip_reason" in sql
        ]
        assert skip_updates, (
            "Expected UPDATE writing verify_skip_reason; "
            f"got: {conn.cursor_instance.executed}"
        )
        _sql, params = skip_updates[0]
        assert params == ("self_deploy", agent_id)

        # Structured log event.
        events = handler.events("verify_skip_reason_written")
        assert events
        assert events[0].skip_reason == "self_deploy"
        assert events[0].issue_number == 2953

    def test_non_dispatcher_commit_does_not_write_skip_reason(
        self, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        agent_id = "aaaabbbb-0000-0000-0000-000000000002"

        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._current_attempt_for = MagicMock(return_value=0)  # type: ignore[method-assign]

        d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
            return_value={
                "commit_message": "feat(web): new feature",
                "pr_title": "feat(web): new feature",
                "pr_body_md": "body",
                "unmet_criteria": [],
            }
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        # Issue #2971: subprocess sequence is [commit_amend, git_show,
        # push, pr_create] — the amend rewrites ralph's placeholder
        # commit with summary's message.
        commit_ok = subprocess.CompletedProcess(
            args=["git", "commit", "--amend"], returncode=0, stdout="", stderr=""
        )
        git_show = subprocess.CompletedProcess(
            args=["git", "show"],
            returncode=0,
            stdout="packages/web/src/app/page.tsx\n",
            stderr="",
        )
        push_ok = subprocess.CompletedProcess(
            args=["git", "push"], returncode=0, stdout="", stderr=""
        )
        pr_create_ok = subprocess.CompletedProcess(
            args=["gh", "pr", "create"],
            returncode=0,
            stdout="https://github.com/x/y/pull/42\n",
            stderr="",
        )

        from unittest.mock import patch

        rev_list_ahead = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="1\n", stderr=""
        )
        # Issue #2964: pre-push fetch+rebase inserted before git push.
        fetch_ok = subprocess.CompletedProcess(
            args=["git", "fetch", "origin", "main"], returncode=0, stdout="", stderr=""
        )
        rebase_ok = subprocess.CompletedProcess(
            args=["git", "rebase", "origin/main"],
            returncode=0,
            stdout="Current branch is up to date.",
            stderr="",
        )
        with patch(
            "subprocess.run",
            side_effect=[
                rev_list_ahead,
                commit_ok,
                git_show,
                fetch_ok,
                rebase_ok,
                push_ok,
                pr_create_ok,
            ],
        ):
            d._push_and_open_pr(agent_id=agent_id, issue_number=42, worktree=worktree)

        skip_updates = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in sql and "verify_skip_reason" in sql
        ]
        assert skip_updates == []

    def test_git_show_failure_does_not_block_push(self, tmp_path: Path) -> None:
        """A ``git show`` error is swallowed so the push proceeds."""
        d, conn, _handler = _make_daemon(tmp_path)
        agent_id = "aaaabbbb-0000-0000-0000-000000000003"

        d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
        d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
        d._current_attempt_for = MagicMock(return_value=0)  # type: ignore[method-assign]

        d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
            return_value={
                "commit_message": "x",
                "pr_title": "x",
                "pr_body_md": "x",
                "unmet_criteria": [],
            }
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        # Issue #2971: subprocess sequence is [commit_amend, git_show,
        # push, pr_create].
        commit_ok = subprocess.CompletedProcess(
            args=["git", "commit", "--amend"], returncode=0, stdout="", stderr=""
        )
        # git show returns non-zero → _list_committed_files_at_head
        # returns [] → no skip reason detected.
        git_show_fail = subprocess.CompletedProcess(
            args=["git", "show"],
            returncode=128,
            stdout="",
            stderr="fatal: bad object HEAD",
        )
        push_ok = subprocess.CompletedProcess(
            args=["git", "push"], returncode=0, stdout="", stderr=""
        )
        pr_create_ok = subprocess.CompletedProcess(
            args=["gh", "pr", "create"],
            returncode=0,
            stdout="https://github.com/x/y/pull/50\n",
            stderr="",
        )

        from unittest.mock import patch

        rev_list_ahead = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="1\n", stderr=""
        )
        # Issue #2964: pre-push fetch+rebase inserted before git push.
        fetch_ok = subprocess.CompletedProcess(
            args=["git", "fetch", "origin", "main"], returncode=0, stdout="", stderr=""
        )
        rebase_ok = subprocess.CompletedProcess(
            args=["git", "rebase", "origin/main"],
            returncode=0,
            stdout="Current branch is up to date.",
            stderr="",
        )
        with patch(
            "subprocess.run",
            side_effect=[
                rev_list_ahead,
                commit_ok,
                git_show_fail,
                fetch_ok,
                rebase_ok,
                push_ok,
                pr_create_ok,
            ],
        ):
            d._push_and_open_pr(agent_id=agent_id, issue_number=50, worktree=worktree)

        # No skip reason written; push went through.
        skip_updates = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in sql and "verify_skip_reason" in sql
        ]
        assert skip_updates == []


# --------------------------------------------------------------------------
# _merge_pr_and_advance — now stamps merged_at + flips status='succeeded'
# --------------------------------------------------------------------------


class TestMergePrAndAdvanceMilestone:
    def test_merge_writes_merged_at_and_flips_status(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        # Stub the side-effects — we assert the primary UPDATE.
        d._write_diagnosis_outcome_for_agent = MagicMock()  # type: ignore[method-assign]
        d._write_terminal_outcome = MagicMock()  # type: ignore[method-assign]
        d._evaluate_circuit_breaker = MagicMock()  # type: ignore[method-assign]
        d._gh_issue_remove_labels = MagicMock()  # type: ignore[method-assign]

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            if cmd[:3] == ["gh", "pr", "merge"]:
                r.stdout = ""
                return r
            if cmd[:3] == ["gh", "pr", "view"]:
                r.stdout = json.dumps({"mergeCommit": {"oid": "merge-sha"}})
                return r
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        monkeypatch.setattr(subprocess, "run", fake_run)

        agent = {
            "agent_id": "a1",
            "issue_number": 2953,
            "phase": "awaiting_ci",
            "pr_number": 101,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
        }
        pr_status = {
            "mergeCommit": {"oid": "merge-sha"},
            "headRefOid": "head",
        }
        d._merge_pr_and_advance(agent, pr_status)

        # merged_at + status='succeeded' in one UPDATE (from
        # _write_merged_at).
        merged_updates = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in sql and "merged_at = now()" in sql
        ]
        assert merged_updates
        sql, _params = merged_updates[0]
        assert "status = 'succeeded'" in sql

        # Phase advanced to awaiting_deploy.
        phase_updates = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in sql
            and "SET phase" in sql
            and params is not None
            and "awaiting_deploy" in params
        ]
        assert phase_updates

        # pr_merged event fired with merge_sha.
        events = handler.events("pr_merged")
        assert events
        assert events[0].merge_sha == "merge-sha"


# --------------------------------------------------------------------------
# _run_verify_and_complete — skip reason short-circuit
# --------------------------------------------------------------------------


class TestVerifySkipReasonShortCircuit:
    def test_skip_reason_short_circuits_without_spawning_verify(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # _read_verify_skip_reason returns 'self_deploy'.
        conn.cursor_instance.fetch_queue = [("self_deploy",)]

        # Spawn should NOT be called if skip reason is set.
        d._spawn_phase_subprocess = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("verify subprocess should not spawn on skip")
        )

        agent = {
            "agent_id": "a1",
            "issue_number": 2953,
            "phase": "awaiting_deploy",
            "pr_number": 101,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
        }
        d._run_verify_and_complete(agent, {}, "merge-sha", [])

        # Structured log event.
        events = handler.events("verify_skipped")
        assert events
        assert events[0].skip_reason == "self_deploy"

        # phase advanced to done.
        phase_updates = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in sql
            and "SET phase" in sql
            and params is not None
            and "done" in params
        ]
        assert phase_updates

        # verified_at NOT written (skip reason is the canonical signal).
        verified_at_updates = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in sql and "verified_at" in sql
        ]
        assert verified_at_updates == []


# --------------------------------------------------------------------------
# _run_retro_phase — stamps retroed_at on successful retro
# --------------------------------------------------------------------------


class TestRetroStampsRetroedAt:
    def test_retro_done_stamps_retroed_at_before_phase_advance(
        self, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        # Stub the pieces _run_retro_phase calls through so we can
        # exercise just the success path. Worktree dir must exist
        # (retro short-circuits to cleanup_done when absent).
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        d._build_retro_input = MagicMock(return_value={"ralph_iterations": 1})  # type: ignore[method-assign]
        d._write_phase_input = MagicMock()  # type: ignore[method-assign]
        d._spawn_phase_subprocess = MagicMock(return_value=(0, 1.23))  # type: ignore[method-assign]
        d._read_phase_output = MagicMock(  # type: ignore[method-assign]
            return_value={"retro_issues": [], "no_findings": True}
        )
        d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
        d._file_retro_issue = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._read_full_phase_log = MagicMock(return_value=None)  # type: ignore[method-assign]
        d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]

        agent = {
            "agent_id": "a1",
            "issue_number": 42,
            "pr_number": 99,
            "worktree_path": str(worktree),
        }
        d._run_retro_phase(agent)

        # retroed_at stamped.
        retroed_updates = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in sql and "retroed_at" in sql
        ]
        assert retroed_updates, (
            f"Expected UPDATE writing retroed_at; got: {conn.cursor_instance.executed}"
        )

        # Phase advanced to retro_done.
        phase_updates = [
            (sql, params)
            for sql, params in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in sql
            and "SET phase" in sql
            and params is not None
            and daemon.PHASE_RETRO_DONE in params
        ]
        assert phase_updates


# --------------------------------------------------------------------------
# _list_advanceable_agents — SELECT picks up the new (status='succeeded',
# phase='awaiting_deploy') rows created at merge time.
# --------------------------------------------------------------------------


class TestAdvanceableAgentsFilterIncludesSucceededAwaitingDeploy:
    def test_select_includes_succeeded_awaiting_deploy(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [[]]
        d._list_advanceable_agents()

        # The last executed SELECT must include both the running-
        # phase-based branch and the succeeded branch with
        # ``awaiting_deploy`` in the IN-list. Issue #2953.
        selects = [
            sql
            for sql, _params in conn.cursor_instance.executed
            if sql.startswith("SELECT") and "FROM dispatcher.agents" in sql
        ]
        assert selects
        sql = selects[-1]
        assert "status = 'succeeded'" in sql
        assert "awaiting_deploy" in sql
        # Must keep the legacy retro/cleanup phases too.
        assert "done" in sql
        assert "retro_done" in sql
        assert "retro_failed" in sql
