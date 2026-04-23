#!/usr/bin/env bash
# test_preflight.sh — Tests for preflight.sh validation functions
#
# Tests each preflight check in isolation using temporary git repos.
# Functions tested:
#   - preflight_in_worktree
#   - preflight_not_on_main
#   - preflight_branch_fresh
#   - preflight_venv_local
#   - preflight_tf_not_root
#   - preflight_no_duplicate_pr (argument validation only — no gh CLI)
#   - preflight_rate_budget (with mock gh)
#   - preflight_no_forbidden_syntax
#
# Usage:
#   scripts/tests/test_preflight.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFLIGHT_SCRIPT="$SCRIPT_DIR/preflight.sh"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

TEMP_DIRS=()
cleanup() {
    # Errors during cleanup must not cause a non-zero exit
    set +e
    for d in "${TEMP_DIRS[@]}"; do
        if [[ -n "$d" && -d "$d" ]]; then
            # Remove worktrees before deleting (only if it's a git repo)
            if git -C "$d" rev-parse --git-dir > /dev/null 2>&1; then
                git -C "$d" worktree list --porcelain 2>/dev/null \
                    | awk '/^worktree / { print $2 }' \
                    | while read -r wt; do
                        [[ "$wt" == "$d" ]] && continue
                        git -C "$d" worktree remove "$wt" --force 2>/dev/null || true
                    done
            fi
            rm -rf "$d"
        fi
    done
}
trap cleanup EXIT

# Create a temp git repo and return its path
make_temp_repo() {
    local dir
    dir=$(mktemp -d)
    TEMP_DIRS+=("$dir")
    git -C "$dir" init --initial-branch main -q
    git -C "$dir" config user.email "test@example.com"
    git -C "$dir" config user.name "Test"
    touch "$dir/README.md"
    git -C "$dir" add README.md
    git -C "$dir" commit -m "init" -q
    echo "$dir"
}

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

# Source preflight.sh so its functions are available
source "$PREFLIGHT_SCRIPT"

# ══════════════════════════════════════════════════════════════════════════
# preflight_in_worktree
# ══════════════════════════════════════════════════════════════════════════

REPO1=$(make_temp_repo)

# Test 1: Fails in main repo (not a worktree)
exit_code=0
(cd "$REPO1" && preflight_in_worktree) > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "preflight_in_worktree fails in main repo"
else
    fail "preflight_in_worktree fails in main repo" "expected non-zero exit"
fi

# Test 2: Succeeds inside a worktree
git -C "$REPO1" worktree add "$REPO1/wt1" -b feat-1 HEAD -q
exit_code=0
(cd "$REPO1/wt1" && preflight_in_worktree) > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "preflight_in_worktree succeeds inside a worktree"
else
    fail "preflight_in_worktree succeeds inside a worktree" "exit code: $exit_code"
fi

# Test 3: Fails outside any git repo
exit_code=0
TEMP_NO_GIT=$(mktemp -d)
TEMP_DIRS+=("$TEMP_NO_GIT")
(cd "$TEMP_NO_GIT" && preflight_in_worktree) > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "preflight_in_worktree fails outside git repo"
else
    fail "preflight_in_worktree fails outside git repo" "expected non-zero exit"
fi

# ══════════════════════════════════════════════════════════════════════════
# preflight_not_on_main
# ══════════════════════════════════════════════════════════════════════════

REPO2=$(make_temp_repo)

# Test 4: Fails on main branch
exit_code=0
(cd "$REPO2" && preflight_not_on_main) > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "preflight_not_on_main fails on main branch"
else
    fail "preflight_not_on_main fails on main branch" "expected non-zero exit"
fi

# Test 5: Succeeds on feature branch
git -C "$REPO2" checkout -b feature-branch -q
exit_code=0
(cd "$REPO2" && preflight_not_on_main) > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "preflight_not_on_main succeeds on feature branch"
else
    fail "preflight_not_on_main succeeds on feature branch" "exit code: $exit_code"
fi

# Test 6: Fails on master branch (alias)
git -C "$REPO2" checkout -b master -q
exit_code=0
(cd "$REPO2" && preflight_not_on_main) > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "preflight_not_on_main fails on master branch"
else
    fail "preflight_not_on_main fails on master branch" "expected non-zero exit"
fi

# Test 7: Fails with detached HEAD
git -C "$REPO2" checkout --detach -q
exit_code=0
(cd "$REPO2" && preflight_not_on_main) > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "preflight_not_on_main fails with detached HEAD"
else
    fail "preflight_not_on_main fails with detached HEAD" "expected non-zero exit"
fi

# ══════════════════════════════════════════════════════════════════════════
# preflight_branch_fresh
# ══════════════════════════════════════════════════════════════════════════

REPO3=$(make_temp_repo)
# Set up a local "origin" so we can test behind/ahead
git -C "$REPO3" remote add origin "$REPO3"
git -C "$REPO3" fetch origin main -q 2>/dev/null
git -C "$REPO3" checkout -b feature-fresh -q

# Test 8: Succeeds when branch is up-to-date with origin/main
exit_code=0
(cd "$REPO3" && preflight_branch_fresh) > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "preflight_branch_fresh succeeds when up-to-date"
else
    fail "preflight_branch_fresh succeeds when up-to-date" "exit code: $exit_code"
fi

# Test 9: Fails when behind origin/main
# Add a commit to main and update origin/main, so the feature branch is behind
git -C "$REPO3" checkout main -q
touch "$REPO3/newfile.txt"
git -C "$REPO3" add newfile.txt
git -C "$REPO3" commit -m "advance main" -q
git -C "$REPO3" fetch origin main -q 2>/dev/null
git -C "$REPO3" checkout feature-fresh -q

exit_code=0
(cd "$REPO3" && preflight_branch_fresh) > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "preflight_branch_fresh fails when behind origin/main"
else
    fail "preflight_branch_fresh fails when behind origin/main" "expected non-zero exit"
fi

# ══════════════════════════════════════════════════════════════════════════
# preflight_venv_local
# ══════════════════════════════════════════════════════════════════════════

VENV_DIR=$(mktemp -d)
TEMP_DIRS+=("$VENV_DIR")

# Test 10: Fails when .venv does not exist
exit_code=0
preflight_venv_local "$VENV_DIR" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "preflight_venv_local fails when .venv missing"
else
    fail "preflight_venv_local fails when .venv missing" "expected non-zero exit"
fi

# Test 11: Succeeds when .venv is a real directory
mkdir -p "$VENV_DIR/.venv"
exit_code=0
preflight_venv_local "$VENV_DIR" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "preflight_venv_local succeeds with real .venv directory"
else
    fail "preflight_venv_local succeeds with real .venv directory" "exit code: $exit_code"
fi

# Test 12: Fails when .venv is a symlink
rm -rf "$VENV_DIR/.venv"
OTHER_DIR=$(mktemp -d)
TEMP_DIRS+=("$OTHER_DIR")
mkdir -p "$OTHER_DIR/.venv"
ln -s "$OTHER_DIR/.venv" "$VENV_DIR/.venv"
exit_code=0
preflight_venv_local "$VENV_DIR" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "preflight_venv_local fails when .venv is a symlink"
else
    fail "preflight_venv_local fails when .venv is a symlink" "expected non-zero exit"
fi

# ══════════════════════════════════════════════════════════════════════════
# preflight_tf_not_root
# ══════════════════════════════════════════════════════════════════════════

TF_DIR=$(mktemp -d)
TEMP_DIRS+=("$TF_DIR")

# Test 13: Fails for root infra/terraform/ path
mkdir -p "$TF_DIR/infra/terraform"
exit_code=0
preflight_tf_not_root "$TF_DIR/infra/terraform" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "preflight_tf_not_root fails for root infra/terraform/"
else
    fail "preflight_tf_not_root fails for root infra/terraform/" "expected non-zero exit"
fi

# Test 14: Succeeds for environment-specific path
mkdir -p "$TF_DIR/infra/terraform/environments/dev"
exit_code=0
preflight_tf_not_root "$TF_DIR/infra/terraform/environments/dev" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "preflight_tf_not_root succeeds for environments/dev"
else
    fail "preflight_tf_not_root succeeds for environments/dev" "exit code: $exit_code"
fi

# ══════════════════════════════════════════════════════════════════════════
# preflight_no_duplicate_pr (argument validation only)
# ══════════════════════════════════════════════════════════════════════════

# Test 15: Fails with no argument (return code 2)
exit_code=0
preflight_no_duplicate_pr > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "preflight_no_duplicate_pr fails with no argument (exit 2)"
else
    fail "preflight_no_duplicate_pr fails with no argument" "expected exit 2, got $exit_code"
fi

# ══════════════════════════════════════════════════════════════════════════
# preflight_rate_budget (with mock gh)
# ══════════════════════════════════════════════════════════════════════════

MOCK_BIN_DIR=$(mktemp -d)
TEMP_DIRS+=("$MOCK_BIN_DIR")
ORIG_PATH_SAVE="$PATH"

# Test 16: Returns 0 when budget is sufficient
cat > "$MOCK_BIN_DIR/gh" << 'MOCKGH'
#!/usr/bin/env bash
echo "4500 9999999999 5000"
exit 0
MOCKGH
chmod +x "$MOCK_BIN_DIR/gh"
export PATH="$MOCK_BIN_DIR:$ORIG_PATH_SAVE"

exit_code=0
preflight_rate_budget 100 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "preflight_rate_budget returns 0 when budget sufficient"
else
    fail "preflight_rate_budget returns 0 when budget sufficient" "exit code: $exit_code"
fi

# Test 17: Returns 1 when budget is low
cat > "$MOCK_BIN_DIR/gh" << 'MOCKGH'
#!/usr/bin/env bash
echo "50 9999999999 5000"
exit 0
MOCKGH
chmod +x "$MOCK_BIN_DIR/gh"

exit_code=0
preflight_rate_budget 100 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "preflight_rate_budget returns 1 when budget low"
else
    fail "preflight_rate_budget returns 1 when budget low" "expected exit 1, got $exit_code"
fi

# Test 18: Returns 2 when gh api fails
cat > "$MOCK_BIN_DIR/gh" << 'MOCKGH'
#!/usr/bin/env bash
exit 1
MOCKGH
chmod +x "$MOCK_BIN_DIR/gh"

exit_code=0
preflight_rate_budget > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "preflight_rate_budget returns 2 when gh api fails"
else
    fail "preflight_rate_budget returns 2 when gh api fails" "expected exit 2, got $exit_code"
fi

# Test 19: Uses default threshold of 100
cat > "$MOCK_BIN_DIR/gh" << 'MOCKGH'
#!/usr/bin/env bash
echo "99 9999999999 5000"
exit 0
MOCKGH
chmod +x "$MOCK_BIN_DIR/gh"

exit_code=0
preflight_rate_budget > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "preflight_rate_budget uses default threshold of 100"
else
    fail "preflight_rate_budget uses default threshold of 100" "expected exit 1, got $exit_code"
fi

# Restore PATH
export PATH="$ORIG_PATH_SAVE"

# ══════════════════════════════════════════════════════════════════════════
# preflight_no_forbidden_syntax
# ══════════════════════════════════════════════════════════════════════════

# Test 20: Detects $() substitution
exit_code=0
preflight_no_forbidden_syntax 'echo $(whoami)' > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "preflight_no_forbidden_syntax detects \$() substitution"
else
    fail "preflight_no_forbidden_syntax detects \$() substitution" "expected non-zero exit"
fi

# Test 21: Detects heredoc
exit_code=0
preflight_no_forbidden_syntax 'cat <<EOF' > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "preflight_no_forbidden_syntax detects heredoc"
else
    fail "preflight_no_forbidden_syntax detects heredoc" "expected non-zero exit"
fi

# Test 22: Detects python -c
exit_code=0
preflight_no_forbidden_syntax 'python3 -c "print(1)"' > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "preflight_no_forbidden_syntax detects python -c"
else
    fail "preflight_no_forbidden_syntax detects python -c" "expected non-zero exit"
fi

# Test 23: Allows safe commands
exit_code=0
preflight_no_forbidden_syntax 'git status' > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "preflight_no_forbidden_syntax allows safe commands"
else
    fail "preflight_no_forbidden_syntax allows safe commands" "exit code: $exit_code"
fi

# Test 24: Allows commands with dollar signs that are not substitutions
exit_code=0
preflight_no_forbidden_syntax 'echo $HOME' > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "preflight_no_forbidden_syntax allows \$HOME (not substitution)"
else
    fail "preflight_no_forbidden_syntax allows \$HOME" "exit code: $exit_code"
fi

# ── Summary ──────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
