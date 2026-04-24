"""Integration tests for the dispatcher daemon — real subprocess / filesystem.

These tests exercise actual git, subprocess, and file-system operations that
the unit tests in ``test_daemon_baseline_clone.py`` and
``test_daemon_phase3a.py`` mock away.  They are intentionally slow and make
real network/disk calls, so they carry the ``integration`` marker:

    pytest.mark.integration

Marker convention
-----------------
* ``integration`` — test touches real git, real subprocess, or the real file-
  system in a meaningful way.  Never add synthetic delays; the real operations
  provide the natural wall-clock cost.
* Default CI run (``scripts-tests`` job, line 589 of ``.github/workflows/ci.yml``)
  excludes this marker with ``-m "not integration"`` so the unit-test job
  stays fast and network-free.
* A dedicated ``dispatcher-integration-tests`` CI job runs only these tests
  (``-m integration``) on ``ubuntu-latest`` (which ships git + network).
  Trigger path: ``scripts/dispatcher/**`` or ``Dockerfile.dispatcher``.
* When to write an integration test vs. a mocked unit test: if the bug that
  would be caught requires a real git repo on disk (e.g. #2804's "worktree
  add fails when CWD has no .git"), write it here.  Pure logic / branching
  lives in the mocked suite.

The helpers (``_FakeConnection``, ``_make_daemon``, ``_CapturingLogHandler``)
are intentionally duplicated from ``test_daemon_baseline_clone.py`` so that
test modules remain independently runnable.  pytest collection order is
unstable; importing across test files creates fragile coupling.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# ---------------------------------------------------------------------------
# Shared fakes — intentionally duplicated so this module is independently
# runnable (see module docstring for rationale).
# ---------------------------------------------------------------------------


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
    logger = logging.getLogger(f"dispatcher.test.integration.{id(tmp_path)}")
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


# ---------------------------------------------------------------------------
# Helper — build a local bare repo with one commit on ``main``
# ---------------------------------------------------------------------------


def _make_bare_origin(tmp_path: Path) -> Path:
    """Create a local bare git repo at ``tmp_path/origin.git`` with one commit.

    Returns the path to the bare repo (usable as a ``git clone`` remote).
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()

    # Initialise as a bare repo.
    subprocess.run(
        ["git", "init", "--bare", str(origin)],
        check=True,
        capture_output=True,
    )

    # Populate it by cloning into a scratch dir, committing, and pushing.
    scratch = tmp_path / "_scratch_init"
    subprocess.run(
        ["git", "clone", str(origin), str(scratch)],
        check=True,
        capture_output=True,
    )
    # Configure identity so git commit works in any CI environment.
    subprocess.run(
        ["git", "-C", str(scratch), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(scratch), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (scratch / "README").write_text("hello\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(scratch), "add", "."],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(scratch), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(scratch), "push", "origin", "HEAD:main"],
        check=True,
        capture_output=True,
    )
    return origin


# ---------------------------------------------------------------------------
# Test 1 — ensure_baseline_clone: missing path → real git clone
# ---------------------------------------------------------------------------


def test_ensure_baseline_clone_against_local_bare_origin(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Real ``git clone`` against a local bare repo succeeds and produces a
    valid ``.git`` directory.

    This test exercises the production clone path without subprocess mocking.
    It catches regressions where the clone command is malformed (bad flags,
    wrong argument order, path issues) that mocked tests would never surface.
    """
    origin = _make_bare_origin(tmp_path)
    baseline = tmp_path / "repo"  # must NOT exist before the call

    # Point the daemon at our local bare repo instead of the real GitHub URL.
    monkeypatch.setattr(daemon, "BASELINE_CLONE_URL", str(origin))

    d, _conn, handler = _make_daemon(tmp_path, baseline_repo_root=baseline)

    # ensure_baseline_clone() calls ``gh auth setup-git`` first; that will
    # raise FileNotFoundError in a test environment without ``gh`` installed,
    # but the daemon already handles that case gracefully (logs
    # ``gh_setup_git_missing`` and continues).  The integration test must
    # succeed whether or not ``gh`` is present.
    d.ensure_baseline_clone()

    # Clone created a valid git repo.
    assert (baseline / ".git").exists(), ".git directory missing after clone"
    assert (baseline / ".git" / "HEAD").exists(), ".git/HEAD missing after clone"

    # Daemon emitted the expected ready event with action="clone".
    ready = handler.events("baseline_clone_ready")
    assert len(ready) == 1, f"expected 1 baseline_clone_ready event, got {ready}"
    assert getattr(ready[0], "action", None) == "clone"


# ---------------------------------------------------------------------------
# Test 2 — _create_worktree: real worktree add against a real baseline clone
# ---------------------------------------------------------------------------


def test_create_worktree_against_real_baseline(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """``_create_worktree`` runs ``git -C <baseline> worktree add`` against a
    real cloned baseline and produces a valid worktree on disk.

    Catches issue #2804's exact failure mode: ``git worktree add`` fails when
    the CWD has no ``.git`` directory.  With a real baseline clone the path
    succeeds and the returned worktree path must exist with ``.git`` as a
    *file* (the worktree marker) not a directory.
    """
    origin = _make_bare_origin(tmp_path)
    baseline = tmp_path / "repo"

    monkeypatch.setattr(daemon, "BASELINE_CLONE_URL", str(origin))

    d, _conn, handler = _make_daemon(tmp_path, baseline_repo_root=baseline)
    d.ensure_baseline_clone()

    # Now exercise the real worktree creation.
    agent_id = "aabbccdd-eeff-0011-2233-445566778899"
    wt = d._create_worktree(agent_id)

    # The worktree path exists on disk.
    assert wt.exists(), f"worktree path {wt} does not exist"

    # In a git worktree, .git is a FILE (gitfile pointer), not a directory.
    git_entry = wt / ".git"
    assert git_entry.exists(), ".git entry missing in worktree"
    assert git_entry.is_file(), (
        f".git is a directory, expected a file (gitfile): {git_entry}"
    )

    # Path follows the expected naming convention.
    expected = baseline.parent / "worktrees" / "agent-aabbccdd"
    assert wt == expected, f"expected worktree at {expected}, got {wt}"

    # worktree_created event emitted.
    assert handler.events("worktree_created") != []


# ---------------------------------------------------------------------------
# Test 3 — ensure_baseline_clone fetches on second call
# ---------------------------------------------------------------------------


def test_ensure_baseline_clone_fetches_when_path_exists(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Calling ``ensure_baseline_clone()`` a second time on an existing clone
    follows the fetch branch, not the clone branch.

    Asserts that the second call emits a ``baseline_clone_ready`` event with
    ``action="fetch"``.  Uses the same daemon instance for both calls so the
    logger captures events from both calls.
    """
    origin = _make_bare_origin(tmp_path)
    baseline = tmp_path / "repo"

    monkeypatch.setattr(daemon, "BASELINE_CLONE_URL", str(origin))

    d, _conn, handler = _make_daemon(tmp_path, baseline_repo_root=baseline)

    # First call — should clone.
    d.ensure_baseline_clone()
    first_ready = handler.events("baseline_clone_ready")
    assert len(first_ready) == 1
    assert getattr(first_ready[0], "action", None) == "clone"

    # Clear captured records so we can inspect the second call in isolation.
    handler.records.clear()

    # Second call — should fetch because .git already exists.
    d.ensure_baseline_clone()
    second_ready = handler.events("baseline_clone_ready")
    assert len(second_ready) == 1, (
        f"expected 1 baseline_clone_ready event on second call, got {second_ready}"
    )
    assert getattr(second_ready[0], "action", None) == "fetch", (
        f"expected action='fetch' on second call, got {getattr(second_ready[0], 'action', None)}"
    )


# ---------------------------------------------------------------------------
# Test 4 — check-issue-author.sh: executable and routes exit codes correctly
# ---------------------------------------------------------------------------


def test_check_issue_author_script_is_executable_and_routes_exit_codes() -> None:
    """``scripts/check-issue-author.sh`` exists, is executable, and returns a
    non-zero exit code with non-empty stderr for a nonexistent issue number.

    Issue 999999999 does not exist on GitHub, so ``gh api`` returns a 404 or
    the ``set -euo pipefail`` script exits non-zero.  This test verifies:
    * The script is on disk at the expected path.
    * The script is executable (mode bit set).
    * A non-zero exit is returned for a clearly invalid input.
    * ``stderr`` is non-empty (the script or ``gh`` emits an error message).

    The test does NOT require ``gh`` to be authenticated — an unauthenticated
    ``gh api`` call still returns a deterministic non-zero exit code.
    """
    repo_root = Path(__file__).resolve().parents[3]  # worktree root
    script = repo_root / "scripts" / "check-issue-author.sh"

    assert script.exists(), f"script not found: {script}"
    assert script.stat().st_mode & 0o111, f"script not executable: {script}"

    result = subprocess.run(
        [str(script), "999999999"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, (
        f"expected non-zero exit for nonexistent issue, got {result.returncode}; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.stderr.strip(), (
        f"expected non-empty stderr for nonexistent issue, got empty; "
        f"stdout={result.stdout!r}"
    )
