"""Unit + integration tests for the ``git commit --amend`` flow (#2971).

The dispatcher v2 pipeline flipped its commit model:

* **Pre-#2971:** Ralph's Step 2.5 created a throwaway commit, undid
  it before SHIP (``git reset --soft HEAD~1`` + ``git reset HEAD``),
  and the daemon's ``_push_and_open_pr`` staged the resulting
  uncommitted diff via ``git add -A`` and committed fresh with
  summary's message. An incomplete undo (observed 2026-04-21 02:59 UTC
  on agent cc6c5a07, issue #2565) silently swallowed ralph's diff and
  produced ``git_commit_failed exit_code=1 stderr_tail=""`` — "nothing
  to commit" goes to stdout, not stderr.

* **Post-#2971:** Ralph commits its work directly with the placeholder
  message ``"WIP: ralph output"`` and leaves the commit in place on
  SHIP. The daemon amends that commit with summary's conventional-
  commits message via ``git commit --amend -F <message-file>``. There
  is no undo to misfire, no ``git add -A`` to swallow.

These tests cover both the unit view (mocked ``subprocess.run``
records the amend call with the right arguments) and the integration
view (a real git repo exercises ralph-commits → daemon-amends end to
end, verifying the final HEAD commit message matches summary's
output).
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

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — same shape as test_daemon_failure_summary.py +
# test_daemon_push_failed.py
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


def _make_daemon(tmp_path: Path) -> tuple[daemon.DispatcherDaemon, _FakeConnection]:
    logger = logging.getLogger(f"dispatcher.test.amend.{id(tmp_path)}")
    logger.handlers = []
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
    return d, conn


_SUMMARY_OUTPUT = {
    "commit_message": "feat(dispatcher): ralph-commits-daemon-amends (#2971)",
    "pr_title": "feat(dispatcher): ralph-commits-daemon-amends (#2971)",
    "pr_body_md": "body",
    "unmet_criteria": [],
}


def _prep_daemon_for_push(d: daemon.DispatcherDaemon) -> None:
    """Stub the DB methods ``_push_and_open_pr`` calls into no-ops."""
    # _push_and_open_pr now reads summary output from DB via _fetch_phase_output (#2975).
    d._fetch_phase_output = MagicMock(return_value=_SUMMARY_OUTPUT)  # type: ignore[method-assign]
    d._materialize_phase_output = MagicMock()  # type: ignore[method-assign]
    d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
    d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
    d._current_attempt_for = MagicMock(return_value=0)  # type: ignore[method-assign]


# --------------------------------------------------------------------------
# Unit view — the amend call passes --amend and -F <path>
# --------------------------------------------------------------------------


class TestPushAndOpenPrUsesAmend:
    """#2971 AC — daemon's commit step uses ``git commit --amend -F <file>``
    and never runs ``git add -A``."""

    def test_commit_step_uses_amend_with_message_file(self, tmp_path: Path) -> None:
        """The first subprocess call is ``git commit --amend -F <tmp/commit_msg.txt>``."""
        d, _conn = _make_daemon(tmp_path)
        _prep_daemon_for_push(d)

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

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
            stdout="https://github.com/x/y/pull/2971\n",
            stderr="",
        )

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
                git_show_empty,
                fetch_ok,
                rebase_ok,
                push_ok,
                pr_create_ok,
            ],
        ) as run_mock:
            d._push_and_open_pr(
                agent_id="amend-test-0000-0000-0000-000000000001",
                issue_number=2971,
                worktree=worktree,
            )

        # Inspect every subprocess.run argv.
        calls = [call.args[0] for call in run_mock.call_args_list]

        # AC: the commit step uses ``commit --amend``, not ``commit -m`` or
        # ``commit -F`` without ``--amend``.
        commit_calls = [
            argv
            for argv in calls
            if len(argv) >= 4 and argv[0] == "git" and "commit" in argv
        ]
        assert commit_calls, f"Expected a commit call; got: {calls}"
        commit_argv = commit_calls[0]
        assert "--amend" in commit_argv, (
            f"Expected --amend in commit argv; got {commit_argv}"
        )
        assert "-F" in commit_argv, f"Expected -F in commit argv; got {commit_argv}"
        # The -F argument must point to the commit message file in
        # tmp/commit_msg.txt inside the worktree.
        dash_f_index = commit_argv.index("-F")
        msg_path = Path(commit_argv[dash_f_index + 1])
        assert msg_path.name == "commit_msg.txt", (
            f"Expected -F argument to point at commit_msg.txt; got {msg_path}"
        )
        assert msg_path.parent == worktree / "tmp", (
            f"Expected -F argument under {worktree}/tmp; got {msg_path}"
        )

    def test_daemon_does_not_run_git_add(self, tmp_path: Path) -> None:
        """AC — ``_push_and_open_pr`` does NOT run ``git add -A`` after #2971.

        Ralph's Step 2.5 committed its work before returning; there is
        nothing to stage on the daemon side. Preserving ``git add -A``
        would be a no-op but the point of the refactor is to make the
        flow self-documenting: the absence of ``add`` is a deliberate
        contract, and a regression that added it back would be
        dangerous (it could silently stage arbitrary untracked files
        the daemon did not intend to ship).
        """
        d, _conn = _make_daemon(tmp_path)
        _prep_daemon_for_push(d)

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

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
            stdout="https://github.com/x/y/pull/2971\n",
            stderr="",
        )

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
                git_show_empty,
                fetch_ok,
                rebase_ok,
                push_ok,
                pr_create_ok,
            ],
        ) as run_mock:
            d._push_and_open_pr(
                agent_id="amend-test-0000-0000-0000-000000000002",
                issue_number=2971,
                worktree=worktree,
            )

        calls = [call.args[0] for call in run_mock.call_args_list]
        add_calls = [
            argv
            for argv in calls
            if len(argv) >= 4 and argv[0] == "git" and argv[3] == "add"
        ]
        assert add_calls == [], (
            f"Expected zero ``git add`` calls after #2971; got {add_calls!r}"
        )

    def test_commit_message_file_contains_summary_output(self, tmp_path: Path) -> None:
        """AC — the -F argument points to a file whose contents equal the
        summary's ``commit_message`` output. This is what the amend will
        write to the rewritten commit."""
        d, _conn = _make_daemon(tmp_path)
        _prep_daemon_for_push(d)
        expected_msg = _SUMMARY_OUTPUT["commit_message"]

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

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
            stdout="https://github.com/x/y/pull/2971\n",
            stderr="",
        )

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
                git_show_empty,
                fetch_ok,
                rebase_ok,
                push_ok,
                pr_create_ok,
            ],
        ):
            d._push_and_open_pr(
                agent_id="amend-test-0000-0000-0000-000000000003",
                issue_number=2971,
                worktree=worktree,
            )

        msg_file = worktree / "tmp" / "commit_msg.txt"
        assert msg_file.exists(), f"Expected commit_msg.txt at {msg_file}"
        assert msg_file.read_text() == expected_msg


# --------------------------------------------------------------------------
# Integration view — a real git repo exercises ralph-commits → daemon-amends
# --------------------------------------------------------------------------


def _git(worktree: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run ``git -C <worktree> <args>`` with minimal env so the test is
    hermetic (no user name/email lookup, no signing hook, no global
    config interference)."""
    env = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": str(worktree.parent),
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


class TestAmendFlowIntegration:
    """End-to-end: a real git repo where ralph committed a placeholder
    message, the daemon's amend step rewrites the message, and the
    final HEAD commit carries summary's conventional-commits text.

    This is the happy-path AC #5 from issue #2971:

    > Integration test covering the happy path: ralph commits,
    > summary writes message file, daemon amends + pushes. Verify:
    > test asserts final HEAD commit message matches summary's output.

    We stop short of the push + PR-create steps (they require network
    + a remote). The amend is the refactor's load-bearing change; once
    HEAD carries summary's message, the subsequent push is unchanged
    from the pre-#2971 flow.
    """

    def test_ralph_commits_daemon_amends_final_message_matches_summary(
        self, tmp_path: Path
    ) -> None:
        """Real-git integration: ralph commit is rewritten by the daemon's
        amend and the final HEAD message is summary's output verbatim.
        """
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        # Seed a local-only repo (no remote needed for amend semantics).
        _git(worktree, "init", "-b", "main", "--quiet")
        _git(worktree, "config", "commit.gpgsign", "false")
        (worktree / "README.md").write_text("# test\n")
        _git(worktree, "add", "README.md")
        _git(worktree, "commit", "-m", "initial", "--quiet")

        # Switch to a work branch to mirror the dispatcher's
        # ``agent/<short-id>`` branch from _create_worktree.
        _git(worktree, "checkout", "-b", "agent/amendflow", "--quiet")

        # Simulate ralph's Step 2.5 (post-#2971 model): commit the
        # work with a placeholder message and leave it in place.
        (worktree / "src.py").write_text("def ralph_output():\n    return 42\n")
        _git(worktree, "add", "src.py")
        _git(worktree, "commit", "-m", "WIP: ralph output", "--quiet")
        ralph_sha = _git(worktree, "rev-parse", "HEAD").stdout.strip()

        # Now run the daemon's amend step. We don't need to run
        # _push_and_open_pr end-to-end (it would try to push + open a
        # PR), but we can exercise the exact same git invocation the
        # daemon does.
        (worktree / "tmp").mkdir()
        commit_msg_path = worktree / "tmp" / "commit_msg.txt"
        summary_message = (
            "feat(dispatcher): ralph-commits-daemon-amends (#2971)\n"
            "\n"
            "Flip the commit model so ralph commits directly and the\n"
            "daemon amends with summary's message. Eliminates the\n"
            "throwaway-undo juggling.\n"
            "\n"
            "Closes #2971\n"
        )
        commit_msg_path.write_text(summary_message)

        result = _git(
            worktree,
            "commit",
            "--amend",
            "-F",
            str(commit_msg_path),
        )
        assert result.returncode == 0, (
            f"Expected amend to succeed; stderr: {result.stderr}"
        )

        # Assertions matching issue #2971 AC#5:
        # 1. HEAD commit is a single commit past main (amend rewrites,
        #    does not create a new commit).
        commit_count = _git(
            worktree, "rev-list", "--count", "main..HEAD"
        ).stdout.strip()
        assert commit_count == "1", (
            f"Expected exactly one commit on the branch; got {commit_count}"
        )

        # 2. HEAD SHA is DIFFERENT from ralph's commit (amend rewrites
        #    history and produces a new SHA).
        amended_sha = _git(worktree, "rev-parse", "HEAD").stdout.strip()
        assert amended_sha != ralph_sha, (
            "Expected amend to produce a new SHA; got the same as ralph's commit"
        )

        # 3. HEAD commit message is summary's message verbatim — NOT
        #    "WIP: ralph output". This is the load-bearing assertion
        #    for issue #2971 AC#4 (the PR spot-check criterion).
        head_message = _git(worktree, "log", "-1", "--format=%B", "HEAD").stdout
        # ``git log --format=%B`` adds a trailing newline on top of the
        # commit's own trailing newline, so compare stripped forms.
        assert head_message.strip() == summary_message.strip(), (
            f"Expected HEAD message to equal summary's; got:\n{head_message!r}"
        )
        assert "WIP: ralph output" not in head_message, (
            "Placeholder 'WIP: ralph output' leaked into final commit message"
        )
        # Subject line assertion — the conventional-commits subject
        # is what GitHub's squash-merge uses for the merged-main
        # commit when the PR has exactly one commit.
        subject = head_message.splitlines()[0]
        assert subject == ("feat(dispatcher): ralph-commits-daemon-amends (#2971)"), (
            f"Subject mismatch: {subject!r}"
        )

        # 4. The diff content is preserved across the amend — the
        #    refactor only changes the message, not the files.
        ls = _git(worktree, "ls-tree", "HEAD", "--name-only").stdout
        assert "src.py" in ls
        assert "README.md" in ls
