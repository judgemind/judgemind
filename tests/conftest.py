"""Shared pytest configuration for the repo-root tests/ directory.

Carries the gh-isolate block (#5071); the canonical copy lives in
scripts/tests/conftest.py and scripts/check-test-gh-isolate.sh fails CI when
this copy drifts.
"""

from __future__ import annotations

import os

import pytest

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
