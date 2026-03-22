#!/usr/bin/env bash
# test_adopt_pr.sh — Tests for adopt-pr.sh
#
# Tests the adopt-pr script using temporary git repos and a mock gh CLI.
# Covers: argument validation, worktree check, branch checkout, rebase
# success, and rebase conflict reporting.
#
# Usage:
#   scripts/tests/test_adopt_pr.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADOPT_SCRIPT="$SCRIPT_DIR/adopt-pr.sh"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

TEMP_DIRS=()
ORIG_PATH_SAVE=""

cleanup() {
    set +eu
    for d in "${TEMP_DIRS[@]}"; do
        if [[ -n "$d" && -d "$d" ]]; then
            # Abort any in-progress rebases in worktrees
            if git -C "$d" rev-parse --git-dir > /dev/null 2>&1; then
                git -C "$d" rebase --abort 2>/dev/null || true
                git -C "$d" worktree list --porcelain 2>/dev/null \
                    | awk '/^worktree / { print $2 }' \
                    | while read -r wt; do
                        [[ "$wt" == "$d" ]] && continue
                        git -C "$wt" rebase --abort 2>/dev/null || true
                        git -C "$d" worktree remove "$wt" --force 2>/dev/null || true
                    done
            fi
            rm -rf "$d"
        fi
    done
    if [[ -n "$ORIG_PATH_SAVE" ]]; then
        export PATH="$ORIG_PATH_SAVE"
    fi
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

# Create a temp git repo with a "remote" origin, simulating a real workflow.
# Sets SETUP_BARE and SETUP_CLONE variables.
make_test_setup() {
    # Create a bare "origin" repo
    SETUP_BARE=$(mktemp -d)
    TEMP_DIRS+=("$SETUP_BARE")
    git init --bare --initial-branch main "$SETUP_BARE" -q

    # Clone it to simulate the main checkout
    SETUP_CLONE=$(mktemp -d)
    TEMP_DIRS+=("$SETUP_CLONE")
    git clone "$SETUP_BARE" "$SETUP_CLONE" -q 2>/dev/null
    git -C "$SETUP_CLONE" config user.email "test@example.com"
    git -C "$SETUP_CLONE" config user.name "Test"
    touch "$SETUP_CLONE/README.md"
    git -C "$SETUP_CLONE" add README.md
    git -C "$SETUP_CLONE" commit -m "init" -q
    git -C "$SETUP_CLONE" push origin main -q 2>/dev/null
}

# Create a worktree in the given repo and set SETUP_WT to its path
make_worktree() {
    local repo_dir="$1"
    local wt_name="${2:-wt1}"
    local wt_branch="${3:-worktree-agent-test}"
    SETUP_WT="$repo_dir/worktrees/$wt_name"

    mkdir -p "$repo_dir/worktrees"
    git -C "$repo_dir" worktree add "$SETUP_WT" -b "$wt_branch" HEAD -q
    mkdir -p "$SETUP_WT/tmp"
}

# ══════════════════════════════════════════════════════════════════════════
# Test 1: Fails with no arguments
# ══════════════════════════════════════════════════════════════════════════

exit_code=0
"$ADOPT_SCRIPT" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "fails with no arguments"
else
    fail "fails with no arguments" "expected non-zero exit"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 2: Fails with non-numeric argument
# ══════════════════════════════════════════════════════════════════════════

exit_code=0
"$ADOPT_SCRIPT" "abc" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "fails with non-numeric argument"
else
    fail "fails with non-numeric argument" "expected non-zero exit"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 3: Strips leading # from argument
# ══════════════════════════════════════════════════════════════════════════

# This will still fail (not in worktree), but should get past the argument
# validation. We check the error message mentions "worktree", not "integer".
exit_code=0
output=$("$ADOPT_SCRIPT" "#123" 2>&1) || exit_code=$?
if [[ "$exit_code" -ne 0 ]] && echo "$output" | grep -q -i "worktree\|git"; then
    pass "strips leading # from PR number"
else
    fail "strips leading # from PR number" "output: $output"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 4: Fails when not in a worktree
# ══════════════════════════════════════════════════════════════════════════

make_test_setup
CLONE_DIR="$SETUP_CLONE"

exit_code=0
(cd "$CLONE_DIR" && "$ADOPT_SCRIPT" 999) > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "fails when not in a worktree"
else
    fail "fails when not in a worktree" "expected non-zero exit"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 5: Successfully adopts a PR branch (happy path)
# ══════════════════════════════════════════════════════════════════════════

make_test_setup
CLONE_DIR2="$SETUP_CLONE"

# Create a "PR branch" with a commit
git -C "$CLONE_DIR2" checkout -b "worker-1/session-20260301-1200" -q
echo "feature content" > "$CLONE_DIR2/pr_feature.txt"
git -C "$CLONE_DIR2" add pr_feature.txt
git -C "$CLONE_DIR2" commit -m "add feature" -q
git -C "$CLONE_DIR2" push origin "worker-1/session-20260301-1200" -q 2>/dev/null
git -C "$CLONE_DIR2" checkout main -q
PR_BRANCH="worker-1/session-20260301-1200"

# Create a worktree (simulating a new agent's fresh worktree)
make_worktree "$CLONE_DIR2"
WT_PATH="$SETUP_WT"

# Set up mock gh to return PR metadata
MOCK_BIN_DIR=$(mktemp -d)
TEMP_DIRS+=("$MOCK_BIN_DIR")
ORIG_PATH_SAVE="$PATH"

cat > "$MOCK_BIN_DIR/gh" << 'MOCKGH'
#!/usr/bin/env bash
# Mock gh pr view — returns tab-separated PR data
if [[ "$1" == "pr" && "$2" == "view" ]]; then
    echo -e "worker-1/session-20260301-1200\tOPEN\tmain\tTest PR title"
    exit 0
fi
exit 1
MOCKGH
chmod +x "$MOCK_BIN_DIR/gh"
export PATH="$MOCK_BIN_DIR:$ORIG_PATH_SAVE"

exit_code=0
adopted_branch=$(cd "$WT_PATH" && "$ADOPT_SCRIPT" 1194 2>/dev/null) || exit_code=$?

# Verify: exit code 0
if [[ "$exit_code" -eq 0 ]]; then
    pass "adopts PR branch successfully (exit 0)"
else
    fail "adopts PR branch successfully" "exit code: $exit_code"
fi

# Verify: stdout contains the branch name
if [[ "$adopted_branch" == "$PR_BRANCH" ]]; then
    pass "prints adopted branch name to stdout"
else
    fail "prints adopted branch name to stdout" "expected '$PR_BRANCH', got '$adopted_branch'"
fi

# Verify: worktree is on the PR branch
actual_branch=$(git -C "$WT_PATH" symbolic-ref --short HEAD 2>/dev/null)
if [[ "$actual_branch" == "$PR_BRANCH" ]]; then
    pass "worktree is on the PR branch after adopt"
else
    fail "worktree is on the PR branch after adopt" "expected '$PR_BRANCH', got '$actual_branch'"
fi

# Verify: the PR's file exists in the worktree
if [[ -f "$WT_PATH/pr_feature.txt" ]]; then
    pass "PR branch content is present in worktree"
else
    fail "PR branch content is present in worktree" "pr_feature.txt not found"
fi

# Verify: old placeholder branch was deleted
if git -C "$CLONE_DIR2" show-ref --verify --quiet "refs/heads/worktree-agent-test" 2>/dev/null; then
    fail "old placeholder branch was deleted" "branch 'worktree-agent-test' still exists"
else
    pass "old placeholder branch was deleted"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 6: Rejects merged PRs
# ══════════════════════════════════════════════════════════════════════════

make_test_setup
CLONE_DIR3="$SETUP_CLONE"
make_worktree "$CLONE_DIR3" "wt3" "worktree-agent-test3"
WT_PATH3="$SETUP_WT"

cat > "$MOCK_BIN_DIR/gh" << 'MOCKGH'
#!/usr/bin/env bash
if [[ "$1" == "pr" && "$2" == "view" ]]; then
    echo -e "some-branch\tMERGED\tmain\tAlready merged PR"
    exit 0
fi
exit 1
MOCKGH
chmod +x "$MOCK_BIN_DIR/gh"

exit_code=0
(cd "$WT_PATH3" && "$ADOPT_SCRIPT" 100) > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "rejects already-merged PRs"
else
    fail "rejects already-merged PRs" "expected non-zero exit"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 7: Reports rebase conflicts without silently failing
# ══════════════════════════════════════════════════════════════════════════

make_test_setup
CLONE_DIR4="$SETUP_CLONE"

# Create a PR branch that modifies README.md
git -C "$CLONE_DIR4" checkout -b conflict-branch -q
echo "PR content" > "$CLONE_DIR4/README.md"
git -C "$CLONE_DIR4" add README.md
git -C "$CLONE_DIR4" commit -m "modify readme in PR" -q
git -C "$CLONE_DIR4" push origin conflict-branch -q 2>/dev/null
git -C "$CLONE_DIR4" checkout main -q

# Now modify the same file on main (creates a conflict on rebase)
echo "Main content" > "$CLONE_DIR4/README.md"
git -C "$CLONE_DIR4" add README.md
git -C "$CLONE_DIR4" commit -m "modify readme on main" -q
git -C "$CLONE_DIR4" push origin main -q 2>/dev/null

make_worktree "$CLONE_DIR4" "wt4" "worktree-agent-test4"
WT_PATH4="$SETUP_WT"

cat > "$MOCK_BIN_DIR/gh" << 'MOCKGH'
#!/usr/bin/env bash
if [[ "$1" == "pr" && "$2" == "view" ]]; then
    echo -e "conflict-branch\tOPEN\tmain\tConflicting PR"
    exit 0
fi
exit 1
MOCKGH
chmod +x "$MOCK_BIN_DIR/gh"

exit_code=0
output=$(cd "$WT_PATH4" && "$ADOPT_SCRIPT" 200 2>&1) || exit_code=$?

if [[ "$exit_code" -ne 0 ]]; then
    pass "exits non-zero on rebase conflict"
else
    fail "exits non-zero on rebase conflict" "expected non-zero exit"
fi

if echo "$output" | grep -q -i "conflict"; then
    pass "reports conflict in output"
else
    fail "reports conflict in output" "output did not mention conflict: $output"
fi

# Clean up the in-progress rebase so the cleanup trap can remove the worktree
git -C "$WT_PATH4" rebase --abort 2>/dev/null || true

# ══════════════════════════════════════════════════════════════════════════
# Test 8: Handles deleted remote branches gracefully
# ══════════════════════════════════════════════════════════════════════════

make_test_setup
CLONE_DIR5="$SETUP_CLONE"
make_worktree "$CLONE_DIR5" "wt5" "worktree-agent-test5"
WT_PATH5="$SETUP_WT"

cat > "$MOCK_BIN_DIR/gh" << 'MOCKGH'
#!/usr/bin/env bash
if [[ "$1" == "pr" && "$2" == "view" ]]; then
    echo -e "deleted-branch\tOPEN\tmain\tPR with deleted branch"
    exit 0
fi
exit 1
MOCKGH
chmod +x "$MOCK_BIN_DIR/gh"

exit_code=0
output=$(cd "$WT_PATH5" && "$ADOPT_SCRIPT" 300 2>&1) || exit_code=$?

if [[ "$exit_code" -ne 0 ]]; then
    pass "fails gracefully when remote branch is deleted"
else
    fail "fails gracefully when remote branch is deleted" "expected non-zero exit"
fi

if echo "$output" | grep -q -i "could not fetch\|deleted"; then
    pass "reports deleted branch in error message"
else
    fail "reports deleted branch in error message" "output: $output"
fi

# Restore PATH
export PATH="$ORIG_PATH_SAVE"

# ── Summary ──────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
