"""Shared pytest configuration for the dispatcher test suite.

Installs a canonical ``psycopg`` stub at collection time so every
``test_daemon_*.py`` module imports against the same object regardless
of collection order.  Individual test files no longer need their own
preamble guards.

Also installs a session-scoped invariant (issue #4307) that fails the
test session loudly if any test leaks a synthetic agent worktree
under ``<repo_root>/.claude/worktrees/agent-*`` whose name does not
match the real-worktree pattern. The pre-#4307 failure mode was a
``MagicMock`` ``agent_id`` getting interpolated into a path string,
landing directories at ``agent-<MagicMock id='...'>/`` that then
tripped repo-walking hygiene checks at the next CI step.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dispatcher.tests._fake_gh import (
    GH_BLOCKED_SCAN,
    GH_RATE_LIMIT_OK,
    GH_RATE_LIMIT_PROBE,
    FakeGh,
)

# --- BEGIN git-env-scrub (#5058) ---
# Edit scripts/tests/conftest.py, then copy this block verbatim into every
# other conftest.py that has it. scripts/check-test-git-env-scrub.sh fails
# when the copies differ.
#
# `git push` (pre-push hook), `git rebase --exec`, `git bisect run` and `!`
# aliases run their command with GIT_DIR (and, in a linked worktree, more
# GIT_* vars) exported. A test that runs `git init` / `commit` / `config` in
# a scratch directory inherits them and writes to THAT repository instead:
# in #5045 a push set core.bare=true in the shared .git and moved main. This
# session fixture removes every variable `git rev-parse --local-env-vars`
# lists, and points git's global config at a throwaway file so a test cannot
# write the user's ~/.gitconfig either. Both are restored after the session.
_GIT_ENV_SCRUB_GITCONFIG = """\
[user]
\tname = Judgemind Test
\temail = test@example.com
[commit]
\tgpgsign = false
[tag]
\tgpgsign = false
[safe]
\tdirectory = *
"""


def _git_env_scrub_var_names() -> list[str]:
    """Return git's repository env var names; [] when git is not installed."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--local-env-vars"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return []  # No git binary, so no test can write to a repository.
    names = result.stdout.split()
    if result.returncode != 0 or "GIT_DIR" not in names:
        raise RuntimeError(
            "git-env-scrub (#5058): `git rev-parse --local-env-vars` did not "
            f"list GIT_DIR (exit {result.returncode}): {result.stderr.strip()}\n"
            "Fix:\n"
            "  git rev-parse --local-env-vars   # must print GIT_DIR etc."
        )
    return names


@pytest.fixture(autouse=True, scope="session")
def _git_env_scrub(tmp_path_factory: pytest.TempPathFactory) -> object:
    """Unset git's repository env vars and pin global git config (#5058)."""
    gitconfig = tmp_path_factory.mktemp("git-env-scrub") / "gitconfig"
    gitconfig.write_text(_GIT_ENV_SCRUB_GITCONFIG)
    with pytest.MonkeyPatch.context() as mp:
        for name in _git_env_scrub_var_names():
            mp.delenv(name, raising=False)
        mp.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
        mp.setenv("GIT_CONFIG_NOSYSTEM", "1")
        yield


# --- END git-env-scrub (#5058) ---


# --- BEGIN gh-isolate (#5071) ---
# Edit scripts/tests/conftest.py, then copy this block verbatim into every
# other conftest.py that has it. scripts/check-test-gh-isolate.sh fails when
# the copies differ or when a test root has no copy.
#
# A test that reaches the real `gh` acts on real GitHub with the developer's
# login: in #5071 a dispatcher test ran the real sweep-completed-parents.sh,
# which closed an open issue and posted comments. This session fixture cuts
# every test off from real GitHub:
#   * GH_CONFIG_DIR points at an empty directory (no stored login);
#   * GH_TOKEN, GITHUB_TOKEN, GH_ENTERPRISE_TOKEN and GITHUB_ENTERPRISE_TOKEN
#     are removed;
#   * a `gh` shim goes first on PATH. It never touches the network: it prints
#     "real gh called from a test" to stderr, records the call, appends it to
#     $GH_ISOLATE_LOG when that is set, and exits 4 (gh's "not authenticated"
#     code).
# A test that needs gh installs its own stub (a fake `gh` prepended to PATH,
# or a mocked subprocess call); that stub wins over the shim. Everything is
# restored after the session.
#
# A shim hit FAILS the test that made it (#5075): a test that falls through
# to the shim only exercises a "gh exited 4" branch, not the behaviour it
# claims to test. After each test, _gh_isolate_check fails it ("real gh
# called from a test", the test id and each gh command) when the shim
# recorded a call; a hit outside any test fails the session teardown. A test
# that calls the shim on purpose (a test of this block) requests the
# gh_shim_allowed fixture.
_GH_ISOLATE_TOKEN_VARS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)
_GH_ISOLATE_SHIM = """\
#!/bin/sh
# gh shim from the gh-isolate test fixture (#5071). Never calls GitHub.
echo "FAIL: real gh called from a test (#5071): gh $*" >&2
echo "Fix: stub gh in this test (put a fake gh first on PATH, or mock the call)." >&2
printf '%s\\tgh %s\\n' "${PYTEST_CURRENT_TEST:-?}" "$*" >>'@HITS@'
if [ -n "${GH_ISOLATE_LOG:-}" ]; then
    printf '%s\\tgh %s\\n' "${PYTEST_CURRENT_TEST:-${GH_ISOLATE_TEST:-?}}" "$*" >>"$GH_ISOLATE_LOG"
fi
exit 4
"""
_GH_ISOLATE_FIX = (
    "Fix: stub gh in this test: put a fake gh first on PATH (the fake_gh "
    "fixture in scripts/dispatcher/tests/conftest.py) or mock the subprocess "
    "call, and assert the gh calls the code should make. A test that calls "
    "the shim on purpose requests the gh_shim_allowed fixture."
)


def _gh_isolate_state(config: pytest.Config) -> dict:
    """Per-session shim state, shared by every conftest copy in the process."""
    state = getattr(config, "_gh_isolate_state", None)
    if state is None:
        state = {"hits": None, "offset": 0}
        config._gh_isolate_state = state  # type: ignore[attr-defined]
    return state


def _gh_isolate_new_hits(state: dict) -> list[str]:
    """Return the gh commands the shim recorded since the last call."""
    if state["hits"] is None or not os.path.exists(state["hits"]):
        return []
    with open(state["hits"], encoding="utf-8", errors="replace") as fh:
        fh.seek(state["offset"])
        new = fh.read()
        state["offset"] = fh.tell()
    return [line.split("\t", 1)[-1] for line in new.splitlines() if line]


@pytest.fixture(autouse=True, scope="session")
def _gh_isolate(request: pytest.FixtureRequest) -> object:
    """Cut the test session off from real GitHub (#5071)."""
    state = _gh_isolate_state(request.config)
    if state["hits"] is not None:  # another conftest copy already did it
        yield
        return
    root = request.getfixturevalue("tmp_path_factory").mktemp("gh-isolate")
    bin_dir = root / "bin"
    config_dir = root / "config"
    bin_dir.mkdir()
    config_dir.mkdir()
    state["hits"] = str(root / "hits.log")
    shim = bin_dir / "gh"
    shim.write_text(_GH_ISOLATE_SHIM.replace("@HITS@", state["hits"]))
    shim.chmod(0o755)
    log = os.environ.get("GH_ISOLATE_LOG") or str(root / "calls.log")
    with pytest.MonkeyPatch.context() as mp:
        for name in _GH_ISOLATE_TOKEN_VARS:
            mp.delenv(name, raising=False)
        mp.delenv("GH_ISOLATE_PID", raising=False)  # a shell test's, not ours
        mp.setenv("GH_CONFIG_DIR", str(config_dir))
        mp.setenv("GH_ISOLATE_LOG", log)
        mp.setenv("GH_NO_UPDATE_NOTIFIER", "1")
        mp.setenv("GH_PROMPT_DISABLED", "1")
        mp.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
        yield
    stray = _gh_isolate_new_hits(state)
    if stray:
        raise RuntimeError(
            "real gh called from a test (#5075): the gh shim was called "
            "outside any test (a session or module fixture):\n"
            + "".join(f"  {cmd}\n" for cmd in stray)
            + _GH_ISOLATE_FIX
        )


@pytest.fixture
def gh_shim_allowed() -> None:
    """Request this in a test that calls the gh shim on purpose (#5075)."""


@pytest.fixture(autouse=True)
def _gh_isolate_check(request: pytest.FixtureRequest, _gh_isolate: object) -> object:
    """Fail the test when it (or a fixture it used) hit the gh shim (#5075)."""
    yield
    hits = _gh_isolate_new_hits(_gh_isolate_state(request.config))
    if hits and "gh_shim_allowed" not in request.fixturenames:
        pytest.fail(
            f"real gh called from a test (#5075): {request.node.nodeid} ran "
            f"{len(hits)} gh command(s) that reached the gh shim:\n"
            + "".join(f"  {cmd}\n" for cmd in hits)
            + _GH_ISOLATE_FIX,
            pytrace=False,
        )


# --- END gh-isolate (#5071) ---


class _UniqueViolation(Exception):
    """Test sentinel — real Exception subclass so daemon's except clauses work."""


_psycopg_stub = MagicMock()
_psycopg_errors = MagicMock()
_psycopg_errors.UniqueViolation = _UniqueViolation
_psycopg_stub.errors = _psycopg_errors
sys.modules["psycopg"] = _psycopg_stub  # always overwrite (collection runs first)


# Real per-agent worktrees follow ``agent-<hex-or-uuidlike>`` — short hex
# id (e.g. ``agent-aabbccdd``) or full uuid-like (e.g.
# ``agent-aabbccdd-eeff-0011-2233-445566778899``). Synthetic /
# Mock-tainted leak names like ``agent-<MagicMock id='4503599627368'>``
# or ``agent-test-fixture`` do NOT match; the session-scoped invariant
# (see ``_assert_no_leaked_test_worktrees``) flags those as a loud test
# failure rather than a silent leak.
_REAL_WORKTREE_NAME_RE = re.compile(r"^agent-[0-9a-f]{8,}(?:-[0-9a-f]+)*$")


def _repo_root() -> Path:
    """Return the absolute path of the repo root the tests run against.

    ``conftest.py`` lives at ``scripts/dispatcher/tests/conftest.py``;
    the repo root is three parents up.
    """
    return Path(__file__).resolve().parents[3]


def _list_leaked_synthetic_worktrees() -> list[Path]:
    """Return paths under ``<repo_root>/.claude/worktrees/`` whose names do
    NOT match the real-worktree pattern. Anything in this list is a
    test-fixture leak — see issue #4307.
    """
    worktrees_dir = _repo_root() / ".claude" / "worktrees"
    if not worktrees_dir.is_dir():
        return []
    leaked: list[Path] = []
    for entry in worktrees_dir.iterdir():
        if not entry.name.startswith("agent-"):
            continue
        if _REAL_WORKTREE_NAME_RE.match(entry.name):
            continue
        leaked.append(entry)
    return leaked


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers so ``pytest -m integration`` works without warnings."""
    config.addinivalue_line(
        "markers",
        "integration: tests exercising real subprocess/filesystem/git — excluded from default CI run",
    )
    config.addinivalue_line(
        "markers",
        "real_gh_healers: run DispatcherDaemon's real gh-calling housekeeping "
        "healers instead of the conftest stubs (#5071)",
    )


# DispatcherDaemon housekeeping healers that shell out to scripts which call
# `gh` and change GitHub state (close issues / PRs, post comments). In #5071
# a `_housekeeping_tick()` test ran the real sweep-completed-parents.sh with
# the developer's gh login and closed an open issue. The gh-isolate fixture
# above already makes any such call fail; these stubs also keep the tick
# tests from spawning the scripts at all (they made the suite take >10 min).
_GH_HEALER_STUBS: dict[str, dict[str, object]] = {
    "_housekeeping_sweep_completed_parents": {"exit_code": 0, "summary_tail": ""},
    "_housekeeping_close_orphan_prs": {
        "closed": 0,
        "scanned": 0,
        "skipped_non_agent": 0,
        "skipped_no_target": 0,
        "skipped_open_target": 0,
    },
}


@pytest.fixture(autouse=True)
def _stub_gh_housekeeping_healers(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replace the gh-calling housekeeping healers with no-op stubs (#5071).

    A test that exercises a healer itself opts out with
    ``@pytest.mark.real_gh_healers`` (and must point it at a fake script).
    A test's own ``monkeypatch.setattr(d, "<healer>", ...)`` on the instance
    still wins over these class-level stubs.
    """
    if request.node.get_closest_marker("real_gh_healers"):
        return
    for module_name in ("dispatcher.daemon", "scripts.dispatcher.daemon"):
        module = sys.modules.get(module_name)
        cls = getattr(module, "DispatcherDaemon", None)
        if cls is None:
            continue
        for name, result in _GH_HEALER_STUBS.items():
            monkeypatch.setattr(
                cls, name, lambda self, _r=result: dict(_r), raising=True
            )


@pytest.fixture(autouse=True, scope="session")
def _assert_no_leaked_test_worktrees() -> object:
    """Session-scoped invariant — fail loudly on synthetic worktree leaks (#4307).

    A real ``agent-<short_id>`` worktree always has a hex-only id
    (``agent-aabbccdd`` or full uuid). Anything else under
    ``<repo_root>/.claude/worktrees/`` after the test session ends is a
    test-fixture leak — typically a test that exercised
    :meth:`DispatcherDaemon._create_worktree` or
    :meth:`DispatcherDaemon._compute_worktree_path` against a real
    ``_repo_root`` rather than a ``tmp_path``.

    This fixture also captures the pre-session set so we only flag
    *new* leaks introduced during this session — pre-existing leftover
    directories from earlier runs aren't this session's bug to surface.

    To intentionally test the leak path (e.g. a regression test for
    this fixture itself), set ``DISPATCHER_TESTS_ALLOW_WORKTREE_LEAK=1``
    in the test process env. The fixture still runs the cleanup but
    skips the assertion.
    """
    pre_existing = {p.name for p in _list_leaked_synthetic_worktrees()}
    yield
    post = _list_leaked_synthetic_worktrees()
    new_leaks = [p for p in post if p.name not in pre_existing]
    if not new_leaks:
        return
    # Always clean up so the next session starts clean — do this
    # BEFORE asserting so a failed assertion doesn't leave the repo
    # poisoned for the next pytest invocation.
    for path in new_leaks:
        try:
            shutil.rmtree(path)
        except OSError:  # pragma: no cover — best-effort
            pass
    if os.environ.get("DISPATCHER_TESTS_ALLOW_WORKTREE_LEAK") == "1":
        return
    formatted = "\n".join(f"  - {p}" for p in new_leaks)
    raise AssertionError(
        "Dispatcher test suite leaked synthetic agent worktrees under "
        "<repo_root>/.claude/worktrees/ during the session. These names "
        "do not match the real-worktree pattern "
        f"(``{_REAL_WORKTREE_NAME_RE.pattern}``) and indicate a test "
        "interpolated a non-string (typically a ``MagicMock``) into a "
        "worktree path or failed to monkeypatch ``_repo_root``. See "
        "issue #4307 for the historical failure mode.\n\n"
        f"Leaked paths (cleaned up):\n{formatted}"
    )


@pytest.fixture
def fake_gh(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> object:
    """Put a :class:`FakeGh` first on PATH for this test (#5075)."""
    fake = FakeGh(tmp_path_factory.mktemp("fake-gh"))
    monkeypatch.setenv("PATH", f"{fake.bin_dir}{os.pathsep}{os.environ['PATH']}")
    yield fake
    fake.verify()


@pytest.fixture
def fake_gh_blocked_scan(fake_gh: FakeGh) -> FakeGh:
    """``fake_gh`` that expects the scheduler tick's ``status/blocked`` scan.

    ``scheduler_tick`` runs ``_fetch_blocked_issues`` on the daemon's first
    tick (#2820). Tests of the tick's other steps answer it with an empty
    list; the test fails if the scan is not made with these arguments.
    """
    fake_gh.expect(GH_BLOCKED_SCAN, stdout="[]")
    return fake_gh


@pytest.fixture
def fake_gh_rate_limit_ok(fake_gh: FakeGh) -> FakeGh:
    """``fake_gh`` that expects the supervisor tick's rate-limit probe.

    ``supervisor_tick`` runs ``_check_gh_rate_limit`` every tick. Tests of
    the tick's other steps answer it with a healthy budget (no skip flag,
    no failure row); the test fails if the probe is not made.
    """
    fake_gh.expect(GH_RATE_LIMIT_PROBE, stdout=GH_RATE_LIMIT_OK)
    return fake_gh


@pytest.fixture
def psycopg_stub() -> MagicMock:
    """Return the conftest-installed psycopg stub for tests that need to wire .connect, etc."""
    return sys.modules["psycopg"]  # type: ignore[return-value]
