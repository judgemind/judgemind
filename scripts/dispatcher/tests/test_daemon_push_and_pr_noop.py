"""Unit + integration tests for the ralph no-op SHIP path in
``_push_and_open_pr`` (#3039).

Background
----------
Ralph's §2.5d "no-op guardrail" skips the pre-push commit when the
worktree is clean at SHIP time — data-only / no-code-change tasks
whose deliverable is ralph's evidence comment on the issue. The
branch tip stays at ``origin/main`` (``git rev-list --count
origin/main..HEAD == 0``).

Pre-#3039 the daemon's ``_push_and_open_pr`` did not check for this
state. It unconditionally ran ``git commit --amend -F <message
file>`` against ``HEAD``. Combined with the shallow baseline clone,
this produced an orphan commit; ``gh pr create`` then failed with
"no history in common with main".

Fix: detect the no-op path at the top of ``_push_and_open_pr`` via
``git rev-list --count origin/main..HEAD`` and terminate the agent
cleanly as ``status='succeeded', phase=PHASE_NO_OP``. No amend, no
push, no PR.

Test coverage
-------------
- ``test_rev_list_zero_short_circuits`` — unit: when ``git rev-list
  --count`` returns ``0``, the method does NOT run ``git commit``,
  ``git push``, or ``gh pr create``. Only ONE subprocess call fires
  (the rev-list probe itself).
- ``test_noop_marks_agent_succeeded_with_phase_no_op`` — unit: the
  terminal status is ``succeeded`` and the phase is ``PHASE_NO_OP``.
- ``test_noop_emits_push_and_pr_skipped_no_op_event`` — unit: a
  structured log event ``push_and_pr_skipped_no_op`` is emitted with
  the agent id and issue number.
- ``test_noop_does_not_invoke_delete_ralph_patches`` — unit: the
  ralph-patch cleanup runs only on the "PR opened" path; a no-op
  terminal must NOT touch persisted ralph patches (there's nothing
  to clean; the issue's evidence comment is the artifact).
- ``test_normal_ship_path_unaffected`` — unit: with rev-list
  returning ``1``, the method continues through commit/push/PR
  exactly as before. Smoke test to pin the non-no-op regression path.
- ``test_noop_integration_no_subprocess_side_effects`` — integration:
  a real on-disk git repo with zero commits ahead of ``origin/main``
  exercises the full method; ``_mark_agent_terminal`` is stubbed so
  we can assert the terminal transition.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — mirror the pattern in test_daemon_push_failed.py
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.fetch_queue: list[Any] = []

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

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


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
    logger = logging.getLogger(f"dispatcher.test.noopship.{id(tmp_path)}")
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

    # Supply the summary output via _fetch_phase_output so the non-no-op
    # path has everything it needs (defensive — individual tests only
    # exercise the no-op path where this data is never read).
    d._fetch_phase_output = MagicMock(  # type: ignore[method-assign]
        return_value={
            "commit_message": "feat(dispatcher): test (#3039)",
            "pr_title": "feat(dispatcher): test (#3039)",
            "pr_body_md": "body",
            "unmet_criteria": [],
        }
    )
    d._materialize_phase_output = MagicMock()  # type: ignore[method-assign]
    d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
    d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
    d._current_attempt_for = MagicMock(return_value=0)  # type: ignore[method-assign]
    d._delete_ralph_patches_for_agent = MagicMock()  # type: ignore[method-assign]

    return d, conn, handler


# --------------------------------------------------------------------------
# Unit — no-op SHIP short-circuits commit/push/PR
# --------------------------------------------------------------------------


class TestNoOpShipShortCircuits:
    def test_rev_list_zero_short_circuits(self, tmp_path: Path) -> None:
        """``git rev-list --count origin/main..HEAD == 0`` → no commit,
        no push, no PR. Only the probe subprocess fires."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        rev_list_zero = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="0\n", stderr=""
        )

        with patch("subprocess.run", side_effect=[rev_list_zero]) as run_mock:
            d._push_and_open_pr(
                agent_id="noop-test-0000-0000-0000-000000000001",
                issue_number=2626,
                worktree=worktree,
            )

        # Exactly one subprocess call — the rev-list probe. No commit /
        # push / gh pr create ran.
        assert len(run_mock.call_args_list) == 1, (
            "Expected exactly one subprocess call on no-op SHIP; "
            f"got {[c.args[0] for c in run_mock.call_args_list]}"
        )
        argv = run_mock.call_args_list[0].args[0]
        assert "rev-list" in argv, f"Expected rev-list probe; got {argv}"
        assert "origin/main..HEAD" in argv

    def test_noop_marks_agent_succeeded_with_phase_no_op(self, tmp_path: Path) -> None:
        """Terminal: status='succeeded', phase=PHASE_NO_OP.

        Chosen over ``verify_skip_reason='no_op'`` because the no-op
        path never produces a PR; ``verify_skip_reason`` is a signal
        that applies after a PR merges (see issue #2953 docstring on
        ``dispatcher.agents.verify_skip_reason``).
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        rev_list_zero = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="0\n", stderr=""
        )

        with patch("subprocess.run", side_effect=[rev_list_zero]):
            d._push_and_open_pr(
                agent_id="noop-test-0000-0000-0000-000000000002",
                issue_number=2626,
                worktree=worktree,
            )

        d._mark_agent_terminal.assert_called_once()
        kwargs = d._mark_agent_terminal.call_args.kwargs
        args = d._mark_agent_terminal.call_args.args
        # The call uses kwargs-only so both the positional agent_id and
        # the keyword-only status/phase are set.
        assert args[0] == "noop-test-0000-0000-0000-000000000002"
        assert kwargs["status"] == "succeeded", (
            f"Expected status='succeeded'; got {kwargs.get('status')!r}"
        )
        assert kwargs["phase"] == daemon.PHASE_NO_OP, (
            f"Expected phase=PHASE_NO_OP; got {kwargs.get('phase')!r}"
        )
        assert kwargs["exit_code"] == 0
        assert kwargs["issue_number"] == 2626

    def test_noop_emits_push_and_pr_skipped_no_op_event(self, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        rev_list_zero = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="0\n", stderr=""
        )

        with patch("subprocess.run", side_effect=[rev_list_zero]):
            d._push_and_open_pr(
                agent_id="noop-test-0000-0000-0000-000000000003",
                issue_number=7777,
                worktree=worktree,
            )

        events = handler.events("push_and_pr_skipped_no_op")
        assert events, "Expected a push_and_pr_skipped_no_op log event; got none"
        event = events[0]
        assert getattr(event, "issue_number", None) == 7777
        assert getattr(event, "agent_id", None) == (
            "noop-test-0000-0000-0000-000000000003"
        )

    def test_noop_does_not_invoke_delete_ralph_patches(self, tmp_path: Path) -> None:
        """The ralph-patch delete helper runs on the PR-opened path
        (after ``gh pr create`` succeeds, #3012). A no-op SHIP opens no
        PR, so the helper must NOT be called."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        rev_list_zero = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="0\n", stderr=""
        )

        with patch("subprocess.run", side_effect=[rev_list_zero]):
            d._push_and_open_pr(
                agent_id="noop-test-0000-0000-0000-000000000004",
                issue_number=42,
                worktree=worktree,
            )

        d._delete_ralph_patches_for_agent.assert_not_called()

    def test_noop_does_not_write_commit_message_file(self, tmp_path: Path) -> None:
        """A downstream sanity check — the commit message file is
        written just before the amend call. A no-op short-circuit
        MUST return before that write so we don't leave a stale
        file behind in the worktree tmp/ dir."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        rev_list_zero = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="0\n", stderr=""
        )

        with patch("subprocess.run", side_effect=[rev_list_zero]):
            d._push_and_open_pr(
                agent_id="noop-test-0000-0000-0000-000000000005",
                issue_number=42,
                worktree=worktree,
            )

        # ``tmp/commit_msg.txt`` would be written by the amend path
        # (``commit_msg_path.write_text(commit_message)``). No write =>
        # the file does not exist.
        assert not (worktree / "tmp" / "commit_msg.txt").exists()


# --------------------------------------------------------------------------
# Unit — the normal SHIP path (rev-list > 0) is unaffected
# --------------------------------------------------------------------------


class TestNormalShipPathUnaffected:
    def test_rev_list_positive_runs_full_flow(self, tmp_path: Path) -> None:
        """Smoke test: when rev-list reports 1 commit ahead, the method
        proceeds through commit → git show → push → gh pr create."""
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        rev_list_ahead = subprocess.CompletedProcess(
            args=["git", "rev-list"], returncode=0, stdout="1\n", stderr=""
        )
        commit_ok = subprocess.CompletedProcess(
            args=["git", "commit", "--amend"], returncode=0, stdout="", stderr=""
        )
        git_show_empty = subprocess.CompletedProcess(
            args=["git", "show"], returncode=0, stdout="", stderr=""
        )
        push_ok = subprocess.CompletedProcess(
            args=["git", "push"], returncode=0, stdout="", stderr=""
        )
        pr_create_ok = subprocess.CompletedProcess(
            args=["gh", "pr", "create"],
            returncode=0,
            stdout="https://github.com/judgemind/judgemind/pull/9999\n",
            stderr="",
        )

        with patch(
            "subprocess.run",
            side_effect=[
                rev_list_ahead,
                commit_ok,
                git_show_empty,
                push_ok,
                pr_create_ok,
            ],
        ) as run_mock:
            d._push_and_open_pr(
                agent_id="normal-ship-0000-0000-0000-000000000001",
                issue_number=9999,
                worktree=worktree,
            )

        # All five subprocess calls fired in order.
        argvs = [call.args[0] for call in run_mock.call_args_list]
        assert len(argvs) == 5, (
            f"Expected 5 subprocess calls on normal SHIP; got {argvs}"
        )
        assert "rev-list" in argvs[0]
        assert "commit" in argvs[1] and "--amend" in argvs[1]
        assert "show" in argvs[2]
        assert "push" in argvs[3]
        assert argvs[4][:3] == ["gh", "pr", "create"]

        # No push_and_pr_skipped_no_op event on the normal path.
        # (The method transitions to awaiting_ci via _mark_agent_terminal
        # on the normal path.)
        d._mark_agent_terminal.assert_called_once()
        kwargs = d._mark_agent_terminal.call_args.kwargs
        assert kwargs["status"] == "running", (
            "Normal SHIP keeps status=running for Phase 3B pickup"
        )
        assert kwargs["phase"] == "awaiting_ci", (
            f"Normal SHIP transitions to awaiting_ci; got {kwargs['phase']!r}"
        )


# --------------------------------------------------------------------------
# Integration — real git repo, real rev-list semantics
# --------------------------------------------------------------------------


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
    )
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


class TestNoOpIntegration:
    """Real git repo, real subprocess calls. ``_push_and_open_pr`` sees
    HEAD == origin/main and short-circuits."""

    def test_real_rev_list_zero_short_circuits(self, tmp_path: Path) -> None:
        # Seed a local repo that will play the role of the worktree.
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _git(worktree, "init", "-b", "main", "--quiet")
        _git(worktree, "config", "commit.gpgsign", "false")
        (worktree / "README.md").write_text("# seed\n")
        _git(worktree, "add", "README.md")
        _git(worktree, "commit", "-m", "seed", "--quiet")

        # Create an ``origin/main`` ref that points at HEAD — so
        # ``git rev-list --count origin/main..HEAD`` returns 0.
        head_sha = _git(worktree, "rev-parse", "HEAD").stdout.strip()
        _git(
            worktree,
            "update-ref",
            "refs/remotes/origin/main",
            head_sha,
        )

        # Sanity: confirm the setup invariant.
        ahead = _git(
            worktree, "rev-list", "--count", "origin/main..HEAD"
        ).stdout.strip()
        assert ahead == "0", f"test setup failed: rev-list says {ahead!r}"

        # tmp/ for commit message file (daemon would create it otherwise).
        (worktree / "tmp").mkdir()

        d, _conn, handler = _make_daemon(tmp_path)

        # No subprocess mocking — real git runs against the real repo.
        d._push_and_open_pr(
            agent_id="int-noop-0000-0000-0000-000000000001",
            issue_number=3039,
            worktree=worktree,
        )

        # Terminal: succeeded + PHASE_NO_OP.
        d._mark_agent_terminal.assert_called_once()
        kwargs = d._mark_agent_terminal.call_args.kwargs
        assert kwargs["status"] == "succeeded"
        assert kwargs["phase"] == daemon.PHASE_NO_OP

        # No commit_msg.txt written (short-circuit before the amend).
        assert not (worktree / "tmp" / "commit_msg.txt").exists()

        # Structured log event emitted.
        assert handler.events("push_and_pr_skipped_no_op") != []
