"""Shared pytest configuration for judgemind-config tests.

Carries a copy of the git-env-scrub block (#5058); the canonical copy
is in scripts/tests/conftest.py.
"""

from __future__ import annotations

import os
import subprocess

import pytest

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
#     "real gh called from a test" to stderr, appends the call to
#     $GH_ISOLATE_LOG, and exits 4 (gh's "not authenticated" code).
# A test that needs gh installs its own stub (a fake `gh` prepended to PATH,
# or a mocked subprocess call); that stub wins over the shim. Everything is
# restored after the session.
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
if [ -n "${GH_ISOLATE_LOG:-}" ]; then
    printf '%s\\tgh %s\\n' "${PYTEST_CURRENT_TEST:-${GH_ISOLATE_TEST:-?}}" "$*" >>"$GH_ISOLATE_LOG"
fi
exit 4
"""


@pytest.fixture(autouse=True, scope="session")
def _gh_isolate(tmp_path_factory: pytest.TempPathFactory) -> object:
    """Cut the test session off from real GitHub (#5071)."""
    root = tmp_path_factory.mktemp("gh-isolate")
    bin_dir = root / "bin"
    config_dir = root / "config"
    bin_dir.mkdir()
    config_dir.mkdir()
    shim = bin_dir / "gh"
    shim.write_text(_GH_ISOLATE_SHIM)
    shim.chmod(0o755)
    log = os.environ.get("GH_ISOLATE_LOG") or str(root / "calls.log")
    with pytest.MonkeyPatch.context() as mp:
        for name in _GH_ISOLATE_TOKEN_VARS:
            mp.delenv(name, raising=False)
        mp.setenv("GH_CONFIG_DIR", str(config_dir))
        mp.setenv("GH_ISOLATE_LOG", log)
        mp.setenv("GH_NO_UPDATE_NOTIFIER", "1")
        mp.setenv("GH_PROMPT_DISABLED", "1")
        mp.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
        yield


# --- END gh-isolate (#5071) ---
