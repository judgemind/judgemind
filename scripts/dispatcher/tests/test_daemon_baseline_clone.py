"""Unit tests for the baseline-clone bootstrap + failure-loop cooldown.

Issue #2804 — dispatcher container has no git checkout, so Phase 3A's
``git -C <cwd> worktree add`` path fails at cap=1. Fix:

* ``DispatcherDaemon.ensure_baseline_clone`` runs at boot and either
  clones ``judgemind/judgemind`` into
  :data:`DISPATCHER_BASELINE_REPO_ROOT` (first boot / empty ephemeral
  storage) or fetches ``origin/main`` (subsequent boot on existing
  clone).
* ``DispatcherDaemon._create_worktree`` invokes
  ``git -C <baseline_repo_root> worktree add <abs_worktree_path>``
  (was ``git -C <cwd>``) so worktree creation no longer depends on the
  container CWD having a ``.git`` directory.
* ``DispatcherDaemon._pick_candidate_issue`` skips any issue whose most
  recent ``dispatcher.agents`` row is within
  :data:`FAILED_AGENT_COOLDOWN_SECONDS` (60 min). Prevents the
  failure-loop amplifier where a systemically-broken issue burns
  through dozens of agents per hour because the partial UNIQUE INDEX
  only blocks re-claim for ``running``/``retrying``.

The tests in :mod:`test_daemon_phase3a` cover the legacy
``baseline_repo_root=None`` path (local-dev / unit-test fallback). This
module covers the new baseline-clone path plus the cooldown behavior,
both of which are strictly additive to the Phase 3A contract.

All external calls — subprocess (``git clone``, ``git fetch``,
``git worktree add``, ``gh auth setup-git``) and psycopg — are mocked.
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
# Shared fakes — intentionally duplicated from test_daemon_phase3a so the
# test modules stay independently runnable (pytest collection order is
# unstable; importing helpers across test files creates fragile coupling).
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
    baseline_repo_root: Path | None = None,
) -> tuple[daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.baseline_clone.{id(tmp_path)}")
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
        baseline_repo_root=baseline_repo_root,
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = conn  # type: ignore[assignment]
    d._run_id = "test-run-id"
    return d, conn, handler


# --------------------------------------------------------------------------
# Constants sanity — cooldown is the expected 60 min
# --------------------------------------------------------------------------


class TestCooldownConstant:
    def test_default_cooldown_is_one_hour(self) -> None:
        # Spec §17 Risk 2 / issue #2804 — 60 min gives the diagnoser a
        # clean window between retries.
        assert daemon.FAILED_AGENT_COOLDOWN_SECONDS == 3600


# --------------------------------------------------------------------------
# DaemonConfig plumbing — DISPATCHER_BASELINE_REPO_ROOT env var flows through
# --------------------------------------------------------------------------


class TestBaselineConfigPlumbing:
    def test_env_var_populates_baseline_repo_root(self) -> None:
        args = daemon._parse_args(
            [
                "--tick-scheduler-seconds",
                "30",
                "--tick-supervisor-seconds",
                "120",
                "--tick-housekeeping-seconds",
                "3600",
            ]
        )
        env = {
            "DATABASE_URL": "postgres://fake",
            "DISPATCHER_BASELINE_REPO_ROOT": "/var/lib/dispatcher/repo",
        }
        cfg = daemon._build_config(args, env=env)
        assert cfg.baseline_repo_root == Path("/var/lib/dispatcher/repo")

    def test_unset_env_var_leaves_baseline_none(self) -> None:
        args = daemon._parse_args([])
        env = {"DATABASE_URL": "postgres://fake"}
        cfg = daemon._build_config(args, env=env)
        assert cfg.baseline_repo_root is None

    def test_empty_env_var_treated_as_unset(self) -> None:
        # Operator lever to disable baseline-clone mode without editing
        # the task definition.
        args = daemon._parse_args([])
        env = {
            "DATABASE_URL": "postgres://fake",
            "DISPATCHER_BASELINE_REPO_ROOT": "   ",
        }
        cfg = daemon._build_config(args, env=env)
        assert cfg.baseline_repo_root is None


# --------------------------------------------------------------------------
# ensure_baseline_clone — clone path missing → invokes git clone;
# path exists → invokes git fetch
# --------------------------------------------------------------------------


class TestEnsureBaselineClone:
    def test_no_baseline_config_is_a_noop(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path, baseline_repo_root=None)

        def fake_run(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("subprocess must not run when baseline unset")

        monkeypatch.setattr(subprocess, "run", fake_run)
        d.ensure_baseline_clone()
        assert handler.events("baseline_clone_skipped") != []

    def test_missing_path_invokes_git_clone(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        baseline = tmp_path / "repo"  # does NOT exist
        d, _conn, handler = _make_daemon(tmp_path, baseline_repo_root=baseline)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d.ensure_baseline_clone()

        # gh auth setup-git runs first, then git clone.
        assert any(c[:3] == ["gh", "auth", "setup-git"] for c in calls)
        clone_calls = [c for c in calls if c[:2] == ["git", "clone"]]
        assert len(clone_calls) == 1
        clone_cmd = clone_calls[0]
        # Issue #3039: must NOT pass --depth=1 / --no-tags. A shallow
        # baseline produces orphan-commit PRs on the ralph no-op SHIP
        # path (``git commit --amend`` against the shallow boundary
        # commit drops the unreachable parent).
        assert "--depth=1" not in clone_cmd, (
            f"Shallow clone regression — --depth=1 re-introduced: {clone_cmd}"
        )
        assert "--no-tags" not in clone_cmd, (
            f"Shallow clone regression — --no-tags re-introduced: {clone_cmd}"
        )
        assert daemon.BASELINE_CLONE_URL in clone_cmd
        assert str(baseline) in clone_cmd
        # No fetch when the path is missing — clone already pulls main.
        assert not any(c[:2] == ["git", "-C"] and "fetch" in c for c in calls)

        ready = handler.events("baseline_clone_ready")
        assert len(ready) == 1
        assert getattr(ready[0], "action", None) == "clone"

    def test_existing_path_invokes_git_fetch(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        baseline = tmp_path / "repo"
        (baseline / ".git").mkdir(parents=True)
        d, _conn, handler = _make_daemon(tmp_path, baseline_repo_root=baseline)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            # Issue #3039: the unshallow probe runs ``git rev-parse
            # --is-shallow-repository`` — default to ``false`` so the
            # unshallow-fetch branch doesn't run in this "already full"
            # baseline case.
            if "rev-parse" in cmd and "--is-shallow-repository" in cmd:
                r.stdout = "false\n"
            else:
                r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d.ensure_baseline_clone()

        # gh auth setup-git then git -C <baseline> fetch origin main.
        assert any(c[:3] == ["gh", "auth", "setup-git"] for c in calls)
        fetch_calls = [
            c
            for c in calls
            if c[:2] == ["git", "-C"] and "fetch" in c and "origin" in c
        ]
        # Issue #3039: exactly one fetch — the normal baseline fetch.
        # The unshallow fetch is gated on the is-shallow probe returning
        # ``true``, which we mocked to ``false``, so no --unshallow call.
        assert len(fetch_calls) == 1
        fetch_cmd = fetch_calls[0]
        assert fetch_cmd[2] == str(baseline)
        assert fetch_cmd[-2:] == ["origin", "main"]
        assert "--unshallow" not in fetch_cmd
        # No clone when the path exists.
        assert not any(c[:2] == ["git", "clone"] for c in calls)

        ready = handler.events("baseline_clone_ready")
        assert len(ready) == 1
        assert getattr(ready[0], "action", None) == "fetch"

    def test_clone_failure_raises_runtime_error(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        baseline = tmp_path / "repo"
        d, _conn, _handler = _make_daemon(tmp_path, baseline_repo_root=baseline)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            if cmd[:2] == ["git", "clone"]:
                r.returncode = 128
                r.stderr = "fatal: could not read Username\n"
                r.stdout = ""
                return r
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        import pytest

        with pytest.raises(RuntimeError) as excinfo:
            d.ensure_baseline_clone()
        assert "exit=128" in str(excinfo.value)

    def test_gh_setup_git_missing_does_not_abort(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        # On a minimal image without gh, clone should still proceed —
        # credentials may already be cached.
        baseline = tmp_path / "repo"
        d, _conn, handler = _make_daemon(tmp_path, baseline_repo_root=baseline)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            if cmd[:3] == ["gh", "auth", "setup-git"]:
                raise FileNotFoundError(2, "nope", cmd[0])
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        # Should not raise.
        d.ensure_baseline_clone()
        assert handler.events("gh_setup_git_missing") != []
        assert handler.events("baseline_clone_ready") != []


# --------------------------------------------------------------------------
# _create_worktree in baseline-clone mode — uses -C <baseline> and absolute
# worktree path under /var/lib/dispatcher/worktrees/
# --------------------------------------------------------------------------


class TestCreateWorktreeBaselineMode:
    def test_uses_baseline_as_git_parent_and_absolute_worktree_path(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        baseline = tmp_path / "repo"
        (baseline / ".git").mkdir(parents=True)
        d, _conn, handler = _make_daemon(tmp_path, baseline_repo_root=baseline)

        captured: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            captured.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        agent_id = "aabbccdd-eeff-0011-2233-445566778899"
        wt = d._create_worktree(agent_id)

        # Fetch-before-worktree ran.
        fetch = [
            c
            for c in captured
            if c[:2] == ["git", "-C"] and "fetch" in c and "origin" in c
        ]
        assert len(fetch) == 1
        assert fetch[0][2] == str(baseline)

        # worktree add invoked with -C <baseline> and absolute path
        # under <baseline>.parent / "worktrees".
        wt_calls = [c for c in captured if c[:2] == ["git", "-C"] and "worktree" in c]
        assert len(wt_calls) == 1
        wt_cmd = wt_calls[0]
        assert wt_cmd[2] == str(baseline)
        assert "add" in wt_cmd
        assert str(wt).startswith(str(tmp_path))
        assert str(wt) == str(baseline.parent / "worktrees" / "agent-aabbccdd")
        # returned path is absolute.
        assert wt.is_absolute()
        # branch name preserved.
        assert "-b" in wt_cmd
        branch_idx = wt_cmd.index("-b") + 1
        assert wt_cmd[branch_idx] == "agent/aabbccdd"
        assert handler.events("worktree_created") != []

    def test_fetch_failure_aborts_worktree_add(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        baseline = tmp_path / "repo"
        (baseline / ".git").mkdir(parents=True)
        d, _conn, _handler = _make_daemon(tmp_path, baseline_repo_root=baseline)

        captured: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            captured.append(cmd)
            r = MagicMock()
            if "fetch" in cmd:
                r.returncode = 1
                r.stderr = "fatal: unable to access remote\n"
                r.stdout = ""
                return r
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        import pytest

        with pytest.raises(RuntimeError) as excinfo:
            d._create_worktree("aabbccdd-eeff-0011-2233-445566778899")
        assert "fetch" in str(excinfo.value)
        # No worktree-add attempt after fetch failure.
        assert not any(c[:2] == ["git", "-C"] and "worktree" in c for c in captured)

    def test_legacy_mode_still_uses_cwd_and_relative_path(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        # Backward compatibility guard for the laptop / unit-test path
        # — when baseline_repo_root is None, the daemon must keep the
        # pre-#2804 behavior (git -C <cwd> worktree add <cwd>/.claude/worktrees/...).
        d, _conn, _handler = _make_daemon(tmp_path, baseline_repo_root=None)

        captured: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            captured.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        agent_id = "aabbccdd-eeff-0011-2233-445566778899"
        wt = d._create_worktree(agent_id)

        # No pre-fetch in legacy mode.
        assert not any("fetch" in c for c in captured)
        assert str(wt).endswith(".claude/worktrees/agent-aabbccdd")


# --------------------------------------------------------------------------
# _install_fargate_preflight_hook — swap the narrowed hook into the worktree
# on each _create_worktree in Fargate mode (issue #2982)
# --------------------------------------------------------------------------


class TestInstallFargatePreflightHook:
    """The Fargate container ships a narrowed preflight hook at
    ``$DISPATCHER_FARGATE_HOOKS_DIR``. After ``git worktree add`` runs, the
    worktree's tracked ``.claude/hooks/preflight-bash.sh`` is overwritten
    with the narrowed copy AND marked ``skip-worktree`` so the divergence
    does not show up in ``git status`` / pre-push / PR diffs. Laptop
    sessions skip the swap — the operator-local hook stays in place."""

    def _prepare_stage_dir(self, stage_dir: Path) -> tuple[Path, Path]:
        stage_dir.mkdir(parents=True, exist_ok=True)
        hook = stage_dir / "preflight-bash.sh"
        helper = stage_dir / "preflight_cross_worktree.py"
        hook.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        helper.write_text("# stub helper\n", encoding="utf-8")
        hook.chmod(0o755)
        helper.chmod(0o644)
        return hook, helper

    def test_swaps_hook_and_marks_skip_worktree_in_fargate_mode(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        baseline = tmp_path / "repo"
        (baseline / ".git").mkdir(parents=True)
        stage_dir = tmp_path / "fargate-hooks"
        stage_hook, stage_helper = self._prepare_stage_dir(stage_dir)
        monkeypatch.setenv("DISPATCHER_FARGATE_HOOKS_DIR", str(stage_dir))
        d, _conn, handler = _make_daemon(tmp_path, baseline_repo_root=baseline)

        # Simulate the ``git worktree add`` by materializing a worktree
        # dir with a pre-existing (operator-local) hook that we expect to
        # be overwritten.
        worktree_path = baseline.parent / "worktrees" / "agent-aabbccdd"
        hooks_dir = worktree_path / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        operator_hook = hooks_dir / "preflight-bash.sh"
        operator_hook.write_text(
            "#!/usr/bin/env bash\n# operator-local full ruleset\nexit 99\n",
            encoding="utf-8",
        )
        operator_hook.chmod(0o755)

        captured: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            captured.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        # Exercise directly — _create_worktree() calls this helper when
        # baseline_repo_root is set, but we test the helper unit directly
        # so the test doesn't have to simulate the full `git worktree add`
        # subprocess protocol.
        d._install_fargate_preflight_hook(worktree_path, "agent-aabbccdd")

        # Hook file was overwritten with the staged copy.
        assert operator_hook.read_text(encoding="utf-8") == (
            stage_hook.read_text(encoding="utf-8")
        )
        # Executable bit preserved.
        assert operator_hook.stat().st_mode & 0o111
        # Helper file was also copied.
        assert (hooks_dir / "preflight_cross_worktree.py").read_text(
            encoding="utf-8"
        ) == stage_helper.read_text(encoding="utf-8")
        # Both files marked skip-worktree so the divergence stays out of
        # git status / pre-push / PR diffs.
        skip_worktree_calls = [
            c for c in captured if "update-index" in c and "--skip-worktree" in c
        ]
        assert len(skip_worktree_calls) == 2
        skipped_paths = {c[-1] for c in skip_worktree_calls}
        assert ".claude/hooks/preflight-bash.sh" in skipped_paths
        assert ".claude/hooks/preflight_cross_worktree.py" in skipped_paths
        # Success event emitted.
        assert handler.events("fargate_hook_installed") != []

    def test_noop_when_stage_dir_env_unset(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Local dev (no Fargate image) has no stage dir env var — the
        helper must no-op + warn, leaving the operator-local hook intact."""
        baseline = tmp_path / "repo"
        (baseline / ".git").mkdir(parents=True)
        d, _conn, handler = _make_daemon(tmp_path, baseline_repo_root=baseline)
        monkeypatch.delenv("DISPATCHER_FARGATE_HOOKS_DIR", raising=False)

        worktree_path = baseline.parent / "worktrees" / "agent-aabbccdd"
        hooks_dir = worktree_path / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        operator_hook = hooks_dir / "preflight-bash.sh"
        operator_hook.write_text("# operator-local\n", encoding="utf-8")

        def fake_run(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("subprocess must not run on no-op path")

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._install_fargate_preflight_hook(worktree_path, "agent-aabbccdd")

        # Operator-local hook untouched.
        assert operator_hook.read_text(encoding="utf-8") == "# operator-local\n"
        # Skip warning emitted.
        skip_events = handler.events("fargate_hook_skip")
        assert skip_events, "expected fargate_hook_skip warning"
        assert any(
            "DISPATCHER_FARGATE_HOOKS_DIR" in getattr(e, "reason", "")
            for e in skip_events
        )

    def test_noop_when_stage_files_missing(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """If the env var is set but the staged hook files are missing
        (e.g. a rogue local image / partial build), the helper must no-op
        + warn, not crash the worktree creation."""
        baseline = tmp_path / "repo"
        (baseline / ".git").mkdir(parents=True)
        # Stage dir exists but files don't.
        stage_dir = tmp_path / "fargate-hooks"
        stage_dir.mkdir()
        monkeypatch.setenv("DISPATCHER_FARGATE_HOOKS_DIR", str(stage_dir))
        d, _conn, handler = _make_daemon(tmp_path, baseline_repo_root=baseline)

        worktree_path = baseline.parent / "worktrees" / "agent-aabbccdd"
        hooks_dir = worktree_path / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        operator_hook = hooks_dir / "preflight-bash.sh"
        operator_hook.write_text("# operator-local\n", encoding="utf-8")

        def fake_run(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("subprocess must not run on no-op path")

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._install_fargate_preflight_hook(worktree_path, "agent-aabbccdd")

        # Operator-local hook untouched.
        assert operator_hook.read_text(encoding="utf-8") == "# operator-local\n"
        assert handler.events("fargate_hook_skip") != []

    def test_not_called_in_legacy_mode(self, monkeypatch: Any, tmp_path: Path) -> None:
        """When baseline_repo_root is None (local dev), _create_worktree
        must not attempt to swap the hook — laptops keep the full
        operator-local ruleset."""
        d, _conn, _handler = _make_daemon(tmp_path, baseline_repo_root=None)

        calls: dict[str, int] = {"install": 0}

        def fake_install(*_args: Any, **_kwargs: Any) -> None:
            calls["install"] += 1

        monkeypatch.setattr(d, "_install_fargate_preflight_hook", fake_install)

        def fake_run(_cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._create_worktree("aabbccdd-eeff-0011-2233-445566778899")

        assert calls["install"] == 0, "hook swap must be gated on Fargate mode"

    def test_called_in_fargate_mode_after_worktree_add(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """End-to-end wiring check: _create_worktree in Fargate mode calls
        _install_fargate_preflight_hook exactly once after git worktree add
        succeeds."""
        baseline = tmp_path / "repo"
        (baseline / ".git").mkdir(parents=True)
        d, _conn, _handler = _make_daemon(tmp_path, baseline_repo_root=baseline)

        install_calls: list[tuple[Path, str]] = []

        def fake_install(worktree_path: Path, agent_id: str) -> None:
            install_calls.append((worktree_path, agent_id))

        monkeypatch.setattr(d, "_install_fargate_preflight_hook", fake_install)

        def fake_run(_cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        agent_id = "aabbccdd-eeff-0011-2233-445566778899"
        wt = d._create_worktree(agent_id)

        assert len(install_calls) == 1
        assert install_calls[0] == (wt, agent_id)


# --------------------------------------------------------------------------
# _compute_worktree_path — consistent across claim path and _create_worktree
# --------------------------------------------------------------------------


class TestComputeWorktreePath:
    def test_baseline_mode_path_is_sibling_to_clone(self, tmp_path: Path) -> None:
        baseline = tmp_path / "repo"
        d, _conn, _handler = _make_daemon(tmp_path, baseline_repo_root=baseline)
        assert d._compute_worktree_path("abcd1234") == (
            baseline.parent / "worktrees" / "agent-abcd1234"
        )

    def test_legacy_mode_path_is_under_cwd(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path, baseline_repo_root=None)
        wt = d._compute_worktree_path("abcd1234")
        assert str(wt).endswith(".claude/worktrees/agent-abcd1234")


# --------------------------------------------------------------------------
# Per-issue cooldown in _pick_candidate_issue
# --------------------------------------------------------------------------


class TestCooldownSkip:
    def test_skips_issue_in_cooldown(self, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        attempted: set[int] = set()
        cooldown: set[int] = {2}

        d._issue_already_attempted = lambda n: n in attempted  # type: ignore[method-assign]
        d._issue_in_cooldown = lambda n: n in cooldown  # type: ignore[method-assign]
        d._issue_author_trusted = lambda _n: True  # type: ignore[method-assign]

        # Issue 2 is in cooldown — picker should skip to 3.
        assert d._pick_candidate_issue([1, 2, 3]) == 1
        # Confirm the skip for 2 would be cooldown if we drop 1.
        assert d._pick_candidate_issue([2, 3]) == 3
        cooldown_skips = [
            r
            for r in handler.events("candidate_skipped")
            if getattr(r, "reason", None) == "cooldown"
        ]
        assert len(cooldown_skips) >= 1
        assert getattr(cooldown_skips[0], "issue_number", None) == 2

    def test_cooldown_db_query_returns_true_when_row_fresh(
        self, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # New SQL: SELECT dispatcher.issue_cooldown_remaining_seconds(%s, %s) > 0
        # Returns (True,) when there is remaining cooldown.
        conn.cursor_instance.fetch_queue = [(True,)]
        assert d._issue_in_cooldown(42) is True
        # Confirm the SQL delegates to the SQL function (migration 37).
        executed = conn.cursor_instance.executed
        assert any(
            "dispatcher.issue_cooldown_remaining_seconds" in sql
            for sql, _params in executed
        )
        # And the parameters include the issue number + cooldown seconds.
        params = [p for _sql, p in executed][0]
        assert params[0] == 42
        assert params[1] == daemon.FAILED_AGENT_COOLDOWN_SECONDS

    def test_cooldown_db_query_returns_false_when_no_row(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # New SQL returns NULL (→ Python None) when no prior row exists.
        conn.cursor_instance.fetch_queue = [(None,)]
        assert d._issue_in_cooldown(42) is False

    def test_cooldown_fail_closed_on_db_error(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            raise RuntimeError("db lost")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        # Fail closed — treat as in cooldown so we don't amplify the
        # failure loop on top of a DB issue.
        assert d._issue_in_cooldown(42) is True
        assert conn.rollbacks >= 1
