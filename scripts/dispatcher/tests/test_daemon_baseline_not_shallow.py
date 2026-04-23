"""Unit + integration tests for the non-shallow baseline clone (#3039).

Background
----------
Pre-#3039 the daemon cloned the baseline repo with ``git clone
--depth=1 --no-tags`` (``ensure_baseline_clone`` in
``scripts/dispatcher/daemon.py``). The resulting shallow clone has a
single commit with its parent marked unreachable. When ralph's §2.5d
"no-op guardrail" fires (working tree clean, no commit created on
top of ``origin/main``), the daemon's pre-#3039 ``_push_and_open_pr``
unconditionally ran ``git commit --amend -F <msg>`` against HEAD.
HEAD was the shallow-boundary commit, so the amend dropped the
parent and produced an orphan/root commit. ``gh pr create`` then
failed with ``The agent/<short-id> branch has no history in common
with main``.

Fix
---
Drop ``--depth=1`` / ``--no-tags`` from the baseline clone so HEAD
always has an accessible parent chain, AND add an
unshallow-on-boot path so pre-existing shallow clones (from
containers built before the fix) are upgraded in place without a
full re-clone.

Test coverage
-------------
- ``test_fresh_clone_is_full`` (unit) — ``ensure_baseline_clone``
  does NOT pass ``--depth=1`` / ``--no-tags`` when it clones a fresh
  baseline.
- ``test_fresh_clone_integration_not_shallow`` (integration) — the
  subprocess really runs against a tmp-dir "remote" and the resulting
  clone reports ``git rev-parse --is-shallow-repository == false``.
- ``test_unshallow_on_boot`` (integration) — if the on-disk baseline
  is already shallow, ``ensure_baseline_clone`` upgrades it to a
  full clone via ``git fetch --unshallow``. Real git, no mocks.
- ``test_already_full_skips_unshallow`` (unit) — the unshallow path
  is a no-op when the baseline is already full (no ``--unshallow``
  fetch).
- ``test_unshallow_probe_failure_is_soft`` (unit) — a ``git
  rev-parse`` failure does NOT raise; the method logs a warning and
  falls through to the normal fetch (best-effort upgrade).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


class _UniqueViolation(Exception):
    """Test sentinel — stands in for real psycopg.errors.UniqueViolation."""


if "psycopg" not in sys.modules or not isinstance(
    getattr(sys.modules["psycopg"].errors, "UniqueViolation", None), type
):
    _psycopg_stub = MagicMock()
    _psycopg_errors = MagicMock()
    _psycopg_errors.UniqueViolation = _UniqueViolation
    _psycopg_stub.errors = _psycopg_errors
    sys.modules["psycopg"] = _psycopg_stub

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — matches the pattern in test_daemon_baseline_clone.py
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
    baseline_repo_root: Path | None,
) -> tuple[daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.noshallow.{id(tmp_path)}")
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
# Unit — fresh clone does NOT pass --depth / --no-tags
# --------------------------------------------------------------------------


class TestFreshCloneIsFull:
    def test_fresh_clone_omits_shallow_flags(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        baseline = tmp_path / "repo"  # does NOT exist — fresh clone path
        d, _conn, _handler = _make_daemon(tmp_path, baseline_repo_root=baseline)

        clone_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            if cmd[:2] == ["git", "clone"]:
                clone_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d.ensure_baseline_clone()

        assert len(clone_calls) == 1, (
            f"Expected exactly one git clone call; got {clone_calls}"
        )
        clone_cmd = clone_calls[0]
        # Issue #3039 core AC — the shallow flags MUST NOT be passed.
        assert "--depth=1" not in clone_cmd, f"Shallow clone regression: {clone_cmd}"
        assert not any(a.startswith("--depth") for a in clone_cmd), (
            f"Any --depth variant is a regression: {clone_cmd}"
        )
        assert "--no-tags" not in clone_cmd, f"--no-tags regression: {clone_cmd}"
        # The clone URL and target path still appear.
        assert daemon.BASELINE_CLONE_URL in clone_cmd
        assert str(baseline) in clone_cmd


# --------------------------------------------------------------------------
# Integration — real git, real shallow / non-shallow state transitions
# --------------------------------------------------------------------------


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run ``git`` with a hermetic env (no global config, no user lookup)."""
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


def _seed_remote(remote_dir: Path, num_commits: int = 3) -> None:
    """Build a throwaway "remote" bare-ish repo with ``num_commits`` commits
    on ``main``. Using a non-bare repo with a real working tree keeps the
    ``git clone`` / ``git fetch --unshallow`` commands simple — they
    accept a filesystem path as the URL."""
    remote_dir.mkdir(parents=True)
    _git(remote_dir, "init", "-b", "main", "--quiet")
    _git(remote_dir, "config", "commit.gpgsign", "false")
    _git(remote_dir, "config", "receive.denyCurrentBranch", "ignore")
    for i in range(num_commits):
        (remote_dir / f"f{i}.txt").write_text(f"content {i}\n")
        _git(remote_dir, "add", f"f{i}.txt")
        _git(remote_dir, "commit", "-m", f"commit {i}", "--quiet")


def _is_shallow(repo: Path) -> bool:
    result = _git(repo, "rev-parse", "--is-shallow-repository", check=False)
    return (result.stdout or "").strip() == "true"


class TestFreshCloneIntegrationNotShallow:
    """Run ``ensure_baseline_clone`` against a real tmp-dir 'remote' and
    confirm the resulting clone is NOT shallow.

    No mocks around subprocess; the only stubs are
    :meth:`_setup_git_credentials` (which otherwise tries to run
    ``gh auth setup-git``) and the psycopg stub at module load time.
    """

    def test_real_clone_is_not_shallow(self, monkeypatch: Any, tmp_path: Path) -> None:
        remote = tmp_path / "remote"
        _seed_remote(remote, num_commits=3)
        baseline = tmp_path / "baseline"  # does NOT exist yet

        d, _conn, _handler = _make_daemon(tmp_path, baseline_repo_root=baseline)

        # Swap the clone URL to the tmp-dir "remote" for the duration of
        # this test — no network, no auth.
        monkeypatch.setattr(daemon, "BASELINE_CLONE_URL", str(remote))
        # Skip ``gh auth setup-git`` — no gh on tmp paths.
        d._setup_git_credentials = lambda: None  # type: ignore[method-assign]

        d.ensure_baseline_clone()

        # Baseline now exists, is a valid git repo, and is NOT shallow.
        assert (baseline / ".git").exists(), "baseline .git missing after clone"
        assert not _is_shallow(baseline), (
            "Fresh clone reported shallow — --depth=1 regression"
        )
        # And the full 3-commit history is accessible (the point of the fix).
        count = _git(baseline, "rev-list", "--count", "HEAD").stdout.strip()
        assert count == "3", f"Expected 3 commits in full clone; got {count}"


class TestUnshallowOnBoot:
    """Pre-existing shallow baseline → ensure_baseline_clone upgrades to full.

    This mirrors the real-world scenario where a container image built
    before #3039 has a shallow baseline on ephemeral storage from a prior
    boot. After the fix lands and the container restarts, the daemon must
    unshallow that baseline before running any worktree operations that
    would hit the orphan-commit bug.
    """

    def test_shallow_baseline_is_unshallowed(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        remote = tmp_path / "remote"
        _seed_remote(remote, num_commits=4)
        baseline = tmp_path / "baseline"

        # Simulate the pre-#3039 state: a shallow clone on disk.
        # ``file://`` forces the non-local protocol so ``--depth=1``
        # actually produces a shallow repo. A plain filesystem path
        # uses git's local protocol, which ignores depth limits.
        _git(
            tmp_path,
            "clone",
            "--depth=1",
            "--no-tags",
            f"file://{remote}",
            str(baseline),
        )
        assert _is_shallow(baseline), (
            "Test setup invariant failed — --depth=1 clone should be shallow"
        )

        d, _conn, handler = _make_daemon(tmp_path, baseline_repo_root=baseline)
        # The existing-clone branch runs fetch origin main — point the
        # origin remote at the local tmp-dir remote (via ``file://`` so
        # both the unshallow fetch and the follow-up baseline fetch can
        # reach it) so it succeeds without network.
        _git(baseline, "remote", "set-url", "origin", f"file://{remote}")
        d._setup_git_credentials = lambda: None  # type: ignore[method-assign]

        d.ensure_baseline_clone()

        # After the boot path, the baseline is no longer shallow AND
        # contains all 4 commits from the remote.
        assert not _is_shallow(baseline), (
            "Baseline still shallow after ensure_baseline_clone — "
            "unshallow-on-boot did not run"
        )
        count = _git(baseline, "rev-list", "--count", "HEAD").stdout.strip()
        assert count == "4", (
            f"Expected all 4 remote commits after unshallow; got {count}"
        )
        # The structured log event was emitted so operators can see the
        # upgrade happened.
        assert handler.events("baseline_unshallowed"), (
            "Expected baseline_unshallowed log event"
        )


class TestAlreadyFullSkipsUnshallow:
    """Unit-level guard — a baseline that's already full must NOT run
    ``git fetch --unshallow``. A second unshallow is a git error ("fatal:
    --unshallow on a complete repository does not make sense") but more
    importantly it wastes a subprocess + remote round-trip."""

    def test_full_baseline_no_unshallow_fetch(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        baseline = tmp_path / "baseline"
        (baseline / ".git").mkdir(parents=True)
        d, _conn, handler = _make_daemon(tmp_path, baseline_repo_root=baseline)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            # The probe asks "is this shallow?" → "false" (already full).
            if "rev-parse" in cmd and "--is-shallow-repository" in cmd:
                r.stdout = "false\n"
            else:
                r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d.ensure_baseline_clone()

        # No ``fetch --unshallow`` call.
        unshallow_calls = [c for c in calls if "--unshallow" in c]
        assert unshallow_calls == [], (
            f"Unshallow fetched on an already-full clone: {unshallow_calls}"
        )
        # And no event — the common case stays quiet.
        assert handler.events("baseline_unshallowed") == []


class TestUnshallowProbeFailureIsSoft:
    """A rev-parse probe failure must log a warning and fall through to
    the normal fetch — the unshallow path is best-effort, NOT a blocker
    for daemon boot.

    Rationale: the baseline-fetch call right after will surface a real
    connectivity / auth problem and raise RuntimeError. A probe failure
    at startup (very rare in practice) shouldn't block the daemon from
    trying the normal boot path.
    """

    def test_probe_nonzero_exit_does_not_raise(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        baseline = tmp_path / "baseline"
        (baseline / ".git").mkdir(parents=True)
        d, _conn, handler = _make_daemon(tmp_path, baseline_repo_root=baseline)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            calls.append(cmd)
            r = MagicMock()
            if "rev-parse" in cmd and "--is-shallow-repository" in cmd:
                # Probe fails — fail-soft branch.
                r.returncode = 128
                r.stdout = ""
                r.stderr = "fatal: not a git repository\n"
                return r
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        # Should not raise.
        d.ensure_baseline_clone()

        # Warning event was emitted so the operator sees something went wrong.
        assert handler.events("baseline_unshallow_probe_failed") != [], (
            "Expected a probe-failure warning log event"
        )
        # No --unshallow attempted after the probe failed.
        assert not any("--unshallow" in c for c in calls), (
            f"Unshallow ran despite probe failure: {calls}"
        )
