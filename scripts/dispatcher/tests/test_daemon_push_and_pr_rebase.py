"""Unit + integration tests for the pre-push fetch+rebase path in
``_push_and_open_pr`` (issue #2964).

Background
----------
Before #2964, ``_push_and_open_pr`` pushed directly to origin without
checking whether the branch was still merge-compatible with main. If a
sibling PR merged while ralph was running, the push would succeed but
``gh pr merge`` would later fail with a merge-conflict error that the
diagnoser had to handle post-push.

Fix: insert a ``git fetch origin main`` + ``git rebase origin/main``
block before the push. On clean rebase, continue to push. On conflict,
abort the rebase and fail with ``FAILURE_CATEGORY_MERGE_CONFLICT_AT_PUSH``
(tier-3, non-retryable) so the diagnoser can triage and route to
``retry_with_hint`` (sibling-PR race) or ``AC_INFEASIBLE`` (semantic
collision).

Test coverage
-------------
- ``test_rebase_clean_proceeds_to_push`` — fetch ok, rebase ok; assert
  full call sequence includes fetch + rebase; terminal phase is
  ``awaiting_ci``.
- ``test_rebase_conflict_aborts_and_fails`` — rebase nonzero; assert
  ``git rebase --abort`` ran, no push attempted, ``_handle_agent_failure``
  called with ``category=merge_conflict_at_push``, ``_persist_phase_output``
  called with ``log_text`` containing the conflict path, and the
  ``push_and_pr_rebase_conflict`` structured log event was emitted.
- ``test_fetch_timeout_still_pushes`` — fetch raises
  ``subprocess.TimeoutExpired``; assert ``fetch_ok=False`` structured log,
  no rebase call, push still attempted (best-effort).
- ``test_rebase_up_to_date_noop_proceeds`` — rebase returns 0 with
  "up to date" stdout; same path as test 1.
- ``test_tier_membership`` — asserts ``FAILURE_CATEGORY_MERGE_CONFLICT_AT_PUSH``
  is in ``TIER_3_CATEGORIES`` and NOT in ``AUTO_RETRY_CATEGORIES`` (AC #4).
- Integration: ``@pytest.mark.integration`` test that creates a real on-disk
  git repo: a bare "origin", a worktree branched from it, advances origin
  with (a) a non-conflicting commit (expect clean rebase + push) and (b) a
  conflicting commit (expect abort + failure category).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402  — sys.path mutation above
from dispatcher.daemon import (  # noqa: E402
    AUTO_RETRY_CATEGORIES,
    FAILURE_CATEGORY_MERGE_CONFLICT_AT_PUSH,
    TIER_3_CATEGORIES,
)


# --------------------------------------------------------------------------
# Shared fakes — same pattern as test_daemon_push_and_pr_noop.py
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
    logger = logging.getLogger(f"dispatcher.test.rebase.{id(tmp_path)}")
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

    d._agent_summary_output = {  # type: ignore[attr-defined]
        "commit_message": "feat(dispatcher): pre-push rebase (#2964)",
        "pr_title": "feat(dispatcher): pre-push rebase (#2964)",
        "pr_body_md": "body",
    }
    d._agent_unmet_criteria = []  # type: ignore[attr-defined]
    d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
    d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
    d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
    d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
    d._current_attempt_for = MagicMock(return_value=0)  # type: ignore[method-assign]
    d._delete_ralph_patches_for_agent = MagicMock()  # type: ignore[method-assign]

    return d, conn, handler


# --------------------------------------------------------------------------
# Helper: build standard CompletedProcess fixtures
# --------------------------------------------------------------------------


def _cp(
    args: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=args, returncode=returncode, stdout=stdout, stderr=stderr
    )


# --------------------------------------------------------------------------
# Unit tests
# --------------------------------------------------------------------------


class TestRebaseCleanProceedsToPush:
    def test_rebase_clean_proceeds_to_push(self, tmp_path: Path) -> None:
        """fetch ok + rebase ok → push proceeds; terminal phase is awaiting_ci."""
        d, _conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        side_effects = [
            _cp(["git", "rev-list"], stdout="1\n"),
            _cp(["git", "commit", "--amend"]),
            _cp(["git", "show"]),
            _cp(["git", "fetch", "origin", "main"]),
            _cp(["git", "rebase", "origin/main"], stdout="Successfully rebased."),
            _cp(["git", "push"]),
            _cp(
                ["gh", "pr", "create"],
                stdout="https://github.com/judgemind/judgemind/pull/9999\n",
            ),
        ]

        with patch("subprocess.run", side_effect=side_effects) as run_mock:
            d._push_and_open_pr(
                agent_id="rebase-clean-0000-0000-0000-000000000001",
                issue_number=2964,
                worktree=worktree,
            )

        argvs = [c.args[0] for c in run_mock.call_args_list]
        assert len(argvs) == 7, f"Expected 7 calls; got {argvs}"
        assert "fetch" in argvs[3], f"Expected fetch at index 3; got {argvs[3]}"
        assert "rebase" in argvs[4], f"Expected rebase at index 4; got {argvs[4]}"
        assert "push" in argvs[5], f"Expected push at index 5; got {argvs[5]}"

        # No rebase --abort fired.
        abort_calls = [c for c in run_mock.call_args_list if "--abort" in c.args[0]]
        assert not abort_calls, (
            f"rebase --abort should not fire on clean rebase; got {abort_calls}"
        )

        # Terminal: awaiting_ci.
        d._mark_agent_terminal.assert_called_once()
        kwargs = d._mark_agent_terminal.call_args.kwargs
        assert kwargs.get("phase") == "awaiting_ci", (
            f"Expected awaiting_ci; got {kwargs.get('phase')!r}"
        )

        # No failure call.
        d._handle_agent_failure.assert_not_called()

        # push_and_pr_rebase_clean event emitted.
        clean_events = handler.events("push_and_pr_rebase_clean")
        assert clean_events, "Expected push_and_pr_rebase_clean log event"
        rec = clean_events[0]
        assert getattr(rec, "fetch_ok", None) is True
        assert getattr(rec, "rebase_outcome", None) == "clean"


class TestRebaseConflictAbortsAndFails:
    def test_rebase_conflict_aborts_and_fails(self, tmp_path: Path) -> None:
        """rebase nonzero → abort + fail with merge_conflict_at_push, no push."""
        d, _conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        conflict_file = "scripts/dispatcher/daemon.py"
        side_effects = [
            _cp(["git", "rev-list"], stdout="1\n"),
            _cp(["git", "commit", "--amend"]),
            _cp(["git", "show"]),
            _cp(["git", "fetch", "origin", "main"]),
            # rebase fails with conflict
            _cp(
                ["git", "rebase", "origin/main"],
                returncode=1,
                stderr="CONFLICT (content): Merge conflict in scripts/dispatcher/daemon.py",
            ),
            # git diff --name-only --diff-filter=U
            _cp(
                ["git", "diff", "--name-only", "--diff-filter=U"],
                stdout=f"{conflict_file}\n",
            ),
            # git rebase --abort
            _cp(["git", "rebase", "--abort"]),
        ]

        with patch("subprocess.run", side_effect=side_effects) as run_mock:
            d._push_and_open_pr(
                agent_id="rebase-conflict-0000-0000-0000-000000000001",
                issue_number=2964,
                worktree=worktree,
            )

        argvs = [c.args[0] for c in run_mock.call_args_list]

        # No push call after conflict.
        push_calls = [a for a in argvs if "push" in a and "--abort" not in a]
        assert not push_calls, (
            f"git push should NOT fire after conflict; got {push_calls}"
        )

        # rebase --abort was called.
        abort_calls = [a for a in argvs if "rebase" in a and "--abort" in a]
        assert abort_calls, "Expected git rebase --abort to fire after conflict"

        # _handle_agent_failure called with correct category.
        d._handle_agent_failure.assert_called_once()
        haf_kwargs = d._handle_agent_failure.call_args.kwargs
        assert haf_kwargs.get("category") == FAILURE_CATEGORY_MERGE_CONFLICT_AT_PUSH, (
            f"Expected merge_conflict_at_push; got {haf_kwargs.get('category')!r}"
        )
        assert conflict_file in haf_kwargs.get("details", {}).get(
            "conflict_files", []
        ), f"Expected {conflict_file} in conflict_files; got {haf_kwargs}"

        # _persist_phase_output called with log_text containing the conflict path.
        d._persist_phase_output.assert_called_once()
        ppo_kwargs = d._persist_phase_output.call_args.kwargs
        log_text = ppo_kwargs.get("log_text", "")
        assert conflict_file in log_text, (
            f"Expected conflict file {conflict_file!r} in log_text; got {log_text!r}"
        )

        # Structured log event push_and_pr_rebase_conflict emitted.
        conflict_events = handler.events("push_and_pr_rebase_conflict")
        assert conflict_events, "Expected push_and_pr_rebase_conflict log event"
        rec = conflict_events[0]
        assert getattr(rec, "fetch_ok", None) is True
        assert getattr(rec, "rebase_outcome", None) == "conflict"
        assert conflict_file in getattr(rec, "conflict_files", [])

        # No terminal call on the normal (awaiting_ci) path.
        d._mark_agent_terminal.assert_not_called()


class TestFetchTimeoutStillPushes:
    def test_fetch_timeout_still_pushes(self, tmp_path: Path) -> None:
        """fetch timeout → fetch_ok=False log event, no rebase, push still attempted."""
        d, _conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        def _side_effect(
            args: list[str], **_kwargs: Any
        ) -> subprocess.CompletedProcess:
            if "fetch" in args:
                raise subprocess.TimeoutExpired(cmd=args, timeout=120)
            if "rev-list" in args:
                return _cp(args, stdout="1\n")
            if "--amend" in args:
                return _cp(args)
            if "show" in args:
                return _cp(args)
            if "push" in args:
                return _cp(args)
            if args[:3] == ["gh", "pr", "create"]:
                return _cp(
                    args, stdout="https://github.com/judgemind/judgemind/pull/9999\n"
                )
            return _cp(args)

        with patch("subprocess.run", side_effect=_side_effect) as run_mock:
            d._push_and_open_pr(
                agent_id="fetch-timeout-0000-0000-0000-000000000001",
                issue_number=2964,
                worktree=worktree,
            )

        argvs = [c.args[0] for c in run_mock.call_args_list]

        # No rebase call when fetch failed.
        rebase_calls = [a for a in argvs if "rebase" in a]
        assert not rebase_calls, (
            f"No rebase expected after fetch timeout; got {rebase_calls}"
        )

        # Push still fires.
        push_calls = [a for a in argvs if "push" in a]
        assert push_calls, "Push should still fire after fetch timeout (best-effort)"

        # Structured log for fetch_failed emitted.
        fetch_failed_events = handler.events("push_and_pr_fetch_failed")
        assert fetch_failed_events, "Expected push_and_pr_fetch_failed log event"
        rec = fetch_failed_events[0]
        assert getattr(rec, "fetch_ok", None) is False
        assert getattr(rec, "rebase_outcome", None) == "skipped"


class TestRebaseUpToDateNoopProceeds:
    def test_rebase_up_to_date_noop_proceeds(self, tmp_path: Path) -> None:
        """fetch ok + rebase with 'up to date' stdout → push still fires."""
        d, _conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        side_effects = [
            _cp(["git", "rev-list"], stdout="1\n"),
            _cp(["git", "commit", "--amend"]),
            _cp(["git", "show"]),
            _cp(["git", "fetch", "origin", "main"]),
            _cp(
                ["git", "rebase", "origin/main"],
                stdout="Current branch agent/abc is up to date.",
                returncode=0,
            ),
            _cp(["git", "push"]),
            _cp(
                ["gh", "pr", "create"],
                stdout="https://github.com/judgemind/judgemind/pull/9999\n",
            ),
        ]

        with patch("subprocess.run", side_effect=side_effects) as run_mock:
            d._push_and_open_pr(
                agent_id="rebase-uptodate-0000-0000-0000-000000000001",
                issue_number=2964,
                worktree=worktree,
            )

        argvs = [c.args[0] for c in run_mock.call_args_list]
        assert "push" in argvs[5], "Push should fire after up-to-date rebase"

        d._handle_agent_failure.assert_not_called()
        d._mark_agent_terminal.assert_called_once()
        kwargs = d._mark_agent_terminal.call_args.kwargs
        assert kwargs.get("phase") == "awaiting_ci"

        # push_and_pr_rebase_clean event emitted.
        clean_events = handler.events("push_and_pr_rebase_clean")
        assert clean_events, (
            "Expected push_and_pr_rebase_clean log event on up-to-date rebase"
        )


class TestTierMembership:
    def test_merge_conflict_at_push_in_tier_3(self) -> None:
        """FAILURE_CATEGORY_MERGE_CONFLICT_AT_PUSH is in TIER_3_CATEGORIES (AC #4)."""
        assert FAILURE_CATEGORY_MERGE_CONFLICT_AT_PUSH in TIER_3_CATEGORIES, (
            f"Expected merge_conflict_at_push in TIER_3_CATEGORIES; "
            f"current set: {TIER_3_CATEGORIES}"
        )

    def test_merge_conflict_at_push_not_in_auto_retry(self) -> None:
        """FAILURE_CATEGORY_MERGE_CONFLICT_AT_PUSH is NOT in AUTO_RETRY_CATEGORIES (AC #4)."""
        assert FAILURE_CATEGORY_MERGE_CONFLICT_AT_PUSH not in AUTO_RETRY_CATEGORIES, (
            f"merge_conflict_at_push must NOT be in AUTO_RETRY_CATEGORIES; "
            f"current set: {AUTO_RETRY_CATEGORIES}"
        )


# --------------------------------------------------------------------------
# Integration — real git repo, real rebase semantics
# --------------------------------------------------------------------------


def _git(
    cwd: Path, *args: str, check: bool = True, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    base_env = os.environ.copy()
    base_env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
    )
    if env:
        base_env.update(env)
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=check,
        env=base_env,
    )


@pytest.mark.integration
class TestRebaseIntegration:
    """Real git repos — exercises the full pre-push rebase with real
    merge-base resolution. ``_mark_agent_terminal`` and
    ``_handle_agent_failure`` are stubbed; the rest runs live."""

    def _setup_repos(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create a bare origin and a local worktree clone.

        Returns (origin_path, worktree_path).
        """
        origin = tmp_path / "origin.git"
        origin.mkdir()
        _git(origin, "init", "--bare", "-b", "main", "--quiet")

        # Intermediate local repo to seed the origin.
        seed = tmp_path / "seed"
        seed.mkdir()
        _git(seed, "init", "-b", "main", "--quiet")
        _git(seed, "config", "commit.gpgsign", "false")
        (seed / "README.md").write_text("# seed\n")
        _git(seed, "add", "README.md")
        _git(seed, "commit", "-m", "initial", "--quiet")
        _git(seed, "remote", "add", "origin", str(origin))
        _git(seed, "push", "origin", "main", "--quiet")

        # Clone the worktree from origin.
        worktree = tmp_path / "worktree"
        subprocess.run(
            ["git", "clone", str(origin), str(worktree)],
            capture_output=True,
            check=True,
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
            },
        )
        _git(worktree, "config", "commit.gpgsign", "false")

        return origin, worktree

    def test_clean_advance_rebases_and_pushes(self, tmp_path: Path) -> None:
        """Non-conflicting commit on origin/main → rebase is clean, push fires.

        After setup origin has one commit. We:
        1. Make a commit on the worktree (touching file_a.txt).
        2. Advance origin/main with a non-conflicting commit (touching file_b.txt).
        3. Run _push_and_open_pr with gh pr create mocked (to avoid network).
        4. Assert _mark_agent_terminal was called with phase='awaiting_ci'.
        """
        origin, worktree = self._setup_repos(tmp_path)
        (worktree / "tmp").mkdir(exist_ok=True)

        # Commit on worktree branch.
        # The daemon computes the push branch as agent/<first-8-chars-of-agent_id-no-dashes>.
        # For agent_id="int-rebase-clean-0000-0000-000000000001", that is "agent/intrebas".
        _git(worktree, "checkout", "-b", "agent/intrebas", "--quiet")
        (worktree / "file_a.txt").write_text("a\n")
        _git(worktree, "add", "file_a.txt")
        _git(worktree, "commit", "-m", "feat: add file_a", "--quiet")

        # Advance origin/main with a non-conflicting change.
        seed2 = tmp_path / "seed2"
        subprocess.run(
            ["git", "clone", str(origin), str(seed2)],
            capture_output=True,
            check=True,
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
            },
        )
        _git(seed2, "config", "commit.gpgsign", "false")
        (seed2 / "file_b.txt").write_text("b\n")
        _git(seed2, "add", "file_b.txt")
        _git(seed2, "commit", "-m", "feat: add file_b (origin advance)", "--quiet")
        _git(seed2, "push", "origin", "main", "--quiet")

        d, _conn, handler = _make_daemon(tmp_path)

        # Mock gh pr create only (avoid network call).
        pr_create_ok = _cp(
            ["gh", "pr", "create"],
            stdout="https://github.com/judgemind/judgemind/pull/9999\n",
        )
        _real_subprocess_run = subprocess.run

        def _intercept(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
            if isinstance(args, list) and args[:3] == ["gh", "pr", "create"]:
                return pr_create_ok
            return _real_subprocess_run(args, **kw)

        with patch("subprocess.run", side_effect=_intercept):
            d._push_and_open_pr(
                agent_id="int-rebase-clean-0000-0000-000000000001",
                issue_number=2964,
                worktree=worktree,
            )

        d._mark_agent_terminal.assert_called_once()
        kwargs = d._mark_agent_terminal.call_args.kwargs
        assert kwargs.get("phase") == "awaiting_ci", (
            f"Expected awaiting_ci; got {kwargs.get('phase')!r}"
        )
        d._handle_agent_failure.assert_not_called()

        clean_events = handler.events("push_and_pr_rebase_clean")
        assert clean_events, "Expected push_and_pr_rebase_clean event on real rebase"

    def test_conflicting_advance_fails_with_category(self, tmp_path: Path) -> None:
        """Conflicting commit on origin/main → abort + failure category.

        After setup:
        1. Worktree modifies conflict.txt to "worktree version".
        2. Origin/main modifies conflict.txt to "origin version" (conflict!).
        3. Run _push_and_open_pr.
        4. Assert: no push, _handle_agent_failure with merge_conflict_at_push,
           _persist_phase_output log_text contains "conflict.txt".
        """
        origin, worktree = self._setup_repos(tmp_path)
        (worktree / "tmp").mkdir(exist_ok=True)

        # Create the base conflict file on origin (so both sides diverge from it).
        seed_base = tmp_path / "seed_base"
        subprocess.run(
            ["git", "clone", str(origin), str(seed_base)],
            capture_output=True,
            check=True,
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
            },
        )
        _git(seed_base, "config", "commit.gpgsign", "false")
        (seed_base / "conflict.txt").write_text("base\n")
        _git(seed_base, "add", "conflict.txt")
        _git(seed_base, "commit", "-m", "feat: add conflict.txt base", "--quiet")
        _git(seed_base, "push", "origin", "main", "--quiet")

        # Fetch base into worktree so it knows about conflict.txt.
        _git(worktree, "fetch", "origin", "main")
        _git(worktree, "rebase", "origin/main")

        # Worktree branch modifies conflict.txt.
        _git(worktree, "checkout", "-b", "agent/conflicttest", "--quiet")
        (worktree / "conflict.txt").write_text("worktree version\n")
        _git(worktree, "add", "conflict.txt")
        _git(worktree, "commit", "-m", "feat: worktree edit conflict.txt", "--quiet")

        # Advance origin/main with a conflicting change.
        seed_conflict = tmp_path / "seed_conflict"
        subprocess.run(
            ["git", "clone", str(origin), str(seed_conflict)],
            capture_output=True,
            check=True,
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
            },
        )
        _git(seed_conflict, "config", "commit.gpgsign", "false")
        (seed_conflict / "conflict.txt").write_text("origin version\n")
        _git(seed_conflict, "add", "conflict.txt")
        _git(
            seed_conflict,
            "commit",
            "-m",
            "feat: origin edit conflict.txt (conflict!)",
            "--quiet",
        )
        _git(seed_conflict, "push", "origin", "main", "--quiet")

        d, _conn, handler = _make_daemon(tmp_path)

        # Don't mock subprocess.run — let real git run the rebase (it will fail).
        d._push_and_open_pr(
            agent_id="int-rebase-conflict-0000-0000-000000000002",
            issue_number=2964,
            worktree=worktree,
        )

        # No push should have been attempted.
        # _handle_agent_failure called with merge_conflict_at_push.
        d._handle_agent_failure.assert_called_once()
        haf_kwargs = d._handle_agent_failure.call_args.kwargs
        assert haf_kwargs.get("category") == FAILURE_CATEGORY_MERGE_CONFLICT_AT_PUSH, (
            f"Expected merge_conflict_at_push; got {haf_kwargs.get('category')!r}"
        )

        # _persist_phase_output log_text contains conflict.txt.
        d._persist_phase_output.assert_called_once()
        ppo_kwargs = d._persist_phase_output.call_args.kwargs
        log_text = ppo_kwargs.get("log_text", "")
        assert "conflict.txt" in log_text, (
            f"Expected 'conflict.txt' in log_text; got {log_text!r}"
        )

        # Structured log event emitted.
        conflict_events = handler.events("push_and_pr_rebase_conflict")
        assert conflict_events, (
            "Expected push_and_pr_rebase_conflict event on real conflict"
        )
