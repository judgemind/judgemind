#!/usr/bin/env bash
# _gh-isolate.sh — cut a shell test off from real GitHub (#5071).
#
# Source this file (do NOT execute it) at the top of every shell test, right
# after `set -euo pipefail` (and after the _git-env-scrub.sh line if there is
# one):
#
#   . "$(dirname "${BASH_SOURCE[0]}")/_gh-isolate.sh"   # scripts/tests/
#   . "$(dirname "${BASH_SOURCE[0]}")/../../tests/_gh-isolate.sh"   # scripts/dispatcher/tests/
#
# scripts/check-test-gh-isolate.sh fails CI when a shell test does not.
#
# Why: a test that reaches the real `gh` acts on real GitHub with the
# developer's login. In #5071 a dispatcher test ran the real
# sweep-completed-parents.sh, which closed an open issue and posted comments;
# in CI gh is not logged in, so nobody noticed. This helper:
#   * points GH_CONFIG_DIR at scripts/tests/fixtures/gh-isolate/config, an
#     empty directory (no hosts.yml, so no stored login);
#   * unsets GH_TOKEN, GITHUB_TOKEN, GH_ENTERPRISE_TOKEN and
#     GITHUB_ENTERPRISE_TOKEN;
#   * puts scripts/tests/fixtures/gh-isolate/bin first on PATH. Its `gh`
#     never touches the network: it prints "real gh called from a test" to
#     stderr, appends the call to $GH_ISOLATE_LOG when that is set, and
#     exits 4 (gh's "not authenticated" code).
# A test that needs gh writes its own fake `gh` and prepends that directory
# to PATH afterwards; the fake wins over the shim. pytest suites get the same
# protection from the gh-isolate block in each test root's conftest.py.
#
# A shim hit FAILS the test (#5075). A test that falls through to the shim
# only exercises a "gh exited 4" branch, and the shim's stderr is often
# captured and lost. The shim appends the call to $GH_ISOLATE_HITS
# (${TMPDIR:-/tmp}/gh-isolate-hits.<test pid>) and sends the test process
# (GH_ISOLATE_PID, set here to $$) SIGUSR1. The USR1 trap below prints
# "FAIL: real gh called from a test", the test name and each gh command, and
# exits 1 once the current command finishes. A signal trap is used rather
# than an EXIT trap because most tests install their own `trap cleanup EXIT`,
# which would replace ours; that EXIT trap still runs after our `exit 1`.
# A test that calls the shim on purpose (a test of this helper) exports
# GH_ISOLATE_ALLOW_HITS=1 in the process that sources the helper.
#
# bash 3.2 compatible.

_gh_isolate_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fixtures/gh-isolate"
if [ -e "$_gh_isolate_dir/config/hosts.yml" ]; then
    echo "FAIL: _gh-isolate.sh: $_gh_isolate_dir/config/hosts.yml exists; tests could log in to GitHub (#5071)." >&2
    echo "Fix:" >&2
    echo "  rm \"$_gh_isolate_dir/config/hosts.yml\"" >&2
    exit 1
fi

unset GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN
GH_CONFIG_DIR="$_gh_isolate_dir/config"
GH_NO_UPDATE_NOTIFIER=1
GH_PROMPT_DISABLED=1
GH_ISOLATE_TEST="$0"
GH_ISOLATE_PID=$$
GH_ISOLATE_HITS="${TMPDIR:-/tmp}/gh-isolate-hits.$$"
PATH="$_gh_isolate_dir/bin:$PATH"
export GH_CONFIG_DIR GH_NO_UPDATE_NOTIFIER GH_PROMPT_DISABLED GH_ISOLATE_TEST \
    GH_ISOLATE_PID GH_ISOLATE_HITS PATH
unset _gh_isolate_dir
rm -f "$GH_ISOLATE_HITS"  # a leftover from an earlier process with this pid

_gh_isolate_on_hit() {
    echo "FAIL: real gh called from a test (#5075): $GH_ISOLATE_TEST ran gh commands that reached the gh shim:" >&2
    if [ -f "$GH_ISOLATE_HITS" ]; then
        sed 's/^/  /' "$GH_ISOLATE_HITS" >&2
        rm -f "$GH_ISOLATE_HITS"
    fi
    echo "Fix: install a fake gh for these calls (write a gh script, prepend its directory to PATH) and assert the calls it receives." >&2
    exit 1
}
trap _gh_isolate_on_hit USR1
