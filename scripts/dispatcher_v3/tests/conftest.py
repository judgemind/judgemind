"""Shared pytest configuration for scripts/dispatcher_v3/tests/.

Carries a copy of the git-env-scrub block (#5058); the canonical copy
is in scripts/tests/conftest.py.
"""

from __future__ import annotations

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
