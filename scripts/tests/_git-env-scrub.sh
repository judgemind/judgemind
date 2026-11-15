#!/usr/bin/env bash
# _git-env-scrub.sh — unset git's repository env vars for a shell test (#5058).
#
# Source this file (do NOT execute it) at the top of every shell test that
# runs a git write (init / commit / config / update-ref) in a scratch repo:
#
#   set -euo pipefail
#   . "$(dirname "${BASH_SOURCE[0]}")/_git-env-scrub.sh"   # scripts/tests/
#   . "$(dirname "${BASH_SOURCE[0]}")/../../tests/_git-env-scrub.sh"   # scripts/dispatcher/tests/
#
# scripts/check-test-git-env-scrub.sh fails CI when such a test does not
# source it.
#
# Why: `git push` (pre-push hook), `git rebase --exec`, `git bisect run` and
# `!` aliases run their command with GIT_DIR (and, in a linked worktree, more
# GIT_* vars) exported. A test that runs `git init` / `git commit` /
# `git config` in a scratch directory inherits them and writes to THAT
# repository instead: in #5045 a push set core.bare=true in the shared .git
# and moved main to test commits. This helper unsets every variable
# `git rev-parse --local-env-vars` lists (the same list .githooks/pre-push
# unsets for its pytest runs), and points git's global config at
# scripts/tests/fixtures/git-env-scrub/gitconfig so a test cannot write the
# user's ~/.gitconfig either. pytest suites get the same protection from the
# git-env-scrub block in each test root's conftest.py.
#
# bash 3.2 compatible.

if command -v git >/dev/null 2>&1; then
    _git_env_scrub_vars="$(git rev-parse --local-env-vars 2>/dev/null || true)"
    if ! printf '%s\n' "$_git_env_scrub_vars" | grep -qx 'GIT_DIR'; then
        echo "FAIL: _git-env-scrub.sh: git rev-parse --local-env-vars did not list GIT_DIR (#5058)." >&2
        echo "Fix:" >&2
        echo "  git rev-parse --local-env-vars   # must print GIT_DIR etc.; check that git works here" >&2
        exit 1
    fi
    for _git_env_scrub_var in $_git_env_scrub_vars; do
        unset "$_git_env_scrub_var"
    done
    unset _git_env_scrub_var _git_env_scrub_vars
fi

GIT_CONFIG_GLOBAL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fixtures/git-env-scrub/gitconfig"
GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL GIT_CONFIG_NOSYSTEM
