#!/usr/bin/env bash
# test_preflight_branch_fresh.sh — Tests for scripts/preflight-branch-fresh.sh
#
# The wrapper delegates to preflight_branch_fresh in scripts/preflight.sh.
# This test sets up small temporary git repos that stand in for a freshly
# created worktree (either up-to-date with origin/main, or behind by N
# commits) and asserts the wrapper's exit code and stderr output.
#
# Covers:
#   exit 0 — branch is at or ahead of origin/main
#   exit 1 — branch is behind origin/main (PREFLIGHT FAIL printed to stderr)
#   --fetch — the flag runs `git fetch origin main` before checking (the
#             fetch itself is a no-op here because the "remote" is a local
#             bare clone, but it exercises the same code path)
#
# Usage:
#   scripts/tests/test_preflight_branch_fresh.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$SCRIPT_DIR/preflight-branch-fresh.sh"
FAILURES=0
TESTS=0

TEMP_DIRS=()
ORIG_PWD=$(pwd)

cleanup() {
    set +eu
    cd "$ORIG_PWD" || true
    # ``${TEMP_DIRS[@]+...}`` guards empty-array iteration under
    # bash 3.2 + set -u (see #4336).
    for d in ${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}; do
        if [[ -n "$d" && -d "$d" ]]; then
            rm -rf "$d"
        fi
    done
}
trap cleanup EXIT

pass() {
    TESTS=$((TESTS + 1))
    echo "PASS: $1"
}

fail() {
    TESTS=$((TESTS + 1))
    FAILURES=$((FAILURES + 1))
    echo "FAIL: $1"
    if [[ -n "${2:-}" ]]; then
        echo "  $2"
    fi
}

# ── Precondition: wrapper exists and is executable ─────────────────────────

if [[ ! -x "$WRAPPER" ]]; then
    echo "FAIL: $WRAPPER is not executable (or does not exist)" >&2
    exit 1
fi

# ── make_test_repo_fresh — worktree at origin/main HEAD ───────────────────
#
# Creates:
#   $PAIR_DIR/remote.git — bare upstream with one commit on main
#   $PAIR_DIR/wt         — worktree branch checked out at origin/main
make_test_repo_fresh() {
    local pair_dir
    pair_dir=$(mktemp -d)
    TEMP_DIRS+=("$pair_dir")

    # Bootstrap the upstream.
    git init --quiet --initial-branch=main "$pair_dir/upstream"
    (
        cd "$pair_dir/upstream"
        git config user.email "test@example.com"
        git config user.name "Test"
        echo "hello" > README.md
        git add README.md
        git commit --quiet -m "initial"
    )
    git clone --quiet --bare "$pair_dir/upstream" "$pair_dir/remote.git" > /dev/null

    # Clone into the worktree-like path, then branch off on an agent branch
    # so we are not on main (preflight_not_on_main would fail, but this
    # wrapper does not call it — still, mirror real worktree layout).
    git clone --quiet "$pair_dir/remote.git" "$pair_dir/wt"
    (
        cd "$pair_dir/wt"
        git config user.email "test@example.com"
        git config user.name "Test"
        git checkout --quiet -b agent/test
    )

    echo "$pair_dir"
}

# ── make_test_repo_behind — worktree behind origin/main by N commits ──────
make_test_repo_behind() {
    local behind_by="${1:-1}"
    local pair_dir
    pair_dir=$(make_test_repo_fresh)

    # Advance origin/main ahead of the worktree branch. The upstream repo is
    # the source of truth for remote.git (clone was --bare from it), so push
    # new commits there via a second worktree-style clone that has remote.git
    # as its origin.
    local push_clone
    push_clone=$(mktemp -d)
    TEMP_DIRS+=("$push_clone")
    git clone --quiet "$pair_dir/remote.git" "$push_clone/push" > /dev/null
    (
        cd "$push_clone/push"
        git config user.email "test@example.com"
        git config user.name "Test"
        git checkout --quiet main
        for i in $(seq 1 "$behind_by"); do
            echo "advance $i" >> README.md
            git add README.md
            git commit --quiet -m "advance $i"
        done
        git push --quiet origin main
    )

    # Fetch the new origin/main into the worktree clone so HEAD..origin/main
    # reports the advance.
    (
        cd "$pair_dir/wt"
        git fetch --quiet origin main
    )

    echo "$pair_dir"
}

# ── Test 1: exit 0 when worktree is fresh (no flag) ────────────────────────

fresh_pair=$(make_test_repo_fresh)
exit_code=0
(cd "$fresh_pair/wt" && "$WRAPPER") > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "exits 0 when branch is at origin/main (no --fetch)"
else
    fail "exits 0 when branch is at origin/main (no --fetch)" "expected 0, got $exit_code"
fi

# ── Test 2: exit 0 when worktree is fresh (--fetch) ────────────────────────

exit_code=0
(cd "$fresh_pair/wt" && "$WRAPPER" --fetch) > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "exits 0 when branch is at origin/main (with --fetch)"
else
    fail "exits 0 when branch is at origin/main (with --fetch)" "expected 0, got $exit_code"
fi

# ── Test 3: exit 1 when worktree is behind origin/main by 1 ────────────────

behind_pair=$(make_test_repo_behind 1)
exit_code=0
stderr_output=$(cd "$behind_pair/wt" && "$WRAPPER" 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "exits 1 when branch is 1 commit behind origin/main"
else
    fail "exits 1 when branch is 1 commit behind origin/main" "expected 1, got $exit_code"
fi

if [[ "$stderr_output" == *"PREFLIGHT FAIL"* && "$stderr_output" == *"behind origin/main"* ]]; then
    pass "prints PREFLIGHT FAIL message to stderr when behind"
else
    fail "prints PREFLIGHT FAIL message to stderr when behind" \
        "expected stderr containing 'PREFLIGHT FAIL' and 'behind origin/main', got '$stderr_output'"
fi

# ── Test 4: exit 1 when worktree is behind by 3 (commit count in message) ──

behind3_pair=$(make_test_repo_behind 3)
exit_code=0
stderr_output=$(cd "$behind3_pair/wt" && "$WRAPPER" 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 && "$stderr_output" == *"3 commit"* ]]; then
    pass "reports the commit-count gap when behind by 3"
else
    fail "reports the commit-count gap when behind by 3" \
        "expected exit 1 and stderr containing '3 commit', got exit=$exit_code stderr='$stderr_output'"
fi

# ── Test 5: --fetch flag is passed through (exercises the fetch branch) ────
#
# We cannot easily distinguish "fetched" from "not fetched" without a network
# dependency, but we can confirm the flag does not change the exit code or
# break the function when invoked on an up-to-date worktree.

exit_code=0
(cd "$fresh_pair/wt" && "$WRAPPER" --fetch) > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "--fetch flag is accepted and does not break fresh-branch case"
else
    fail "--fetch flag is accepted and does not break fresh-branch case" "expected 0, got $exit_code"
fi

# ── Summary ───────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
