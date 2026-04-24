#!/usr/bin/env bash
# test_preflight_wrappers.sh — Tests for scripts/preflight/ standalone wrappers
#
# Exercises every wrapper under scripts/preflight/ and asserts the 0/1/2 exit
# codes match the underlying function in scripts/preflight.sh.
#
# Covers:
#   in-worktree.sh      — exit 0 in a worktree, exit 1 in a main repo / outside git
#   not-on-main.sh      — exit 0 on a feature branch, exit 1 on main/master/detached
#   branch-fresh.sh     — exit 0 when up-to-date, exit 1 when behind origin/main
#   venv-local.sh       — exit 0 with real .venv, exit 1 when missing or symlinked
#   tf-not-root.sh      — exit 0 for env path, exit 1 for root infra/terraform/
#   no-duplicate-pr.sh  — exit 2 with no argument; exit 0/1 with mock-gh
#   rate-budget.sh      — exit 0/1/2 with mock-gh
#   no-forbidden-syntax.sh — exit 1 for forbidden patterns, exit 0 for safe commands
#
# Usage:
#   scripts/tests/test_preflight_wrappers.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFLIGHT_DIR="$SCRIPT_DIR/preflight"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

TEMP_DIRS=()
cleanup() {
    set +e
    for d in "${TEMP_DIRS[@]}"; do
        if [[ -n "$d" && -d "$d" ]]; then
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

# ── Precondition: all 8 wrappers exist and are executable ──────────────────

WRAPPERS=(
    in-worktree.sh
    not-on-main.sh
    branch-fresh.sh
    venv-local.sh
    tf-not-root.sh
    no-duplicate-pr.sh
    rate-budget.sh
    no-forbidden-syntax.sh
)

for wrapper in "${WRAPPERS[@]}"; do
    path="$PREFLIGHT_DIR/$wrapper"
    if [[ -x "$path" ]]; then
        pass "wrapper exists and is executable: $wrapper"
    else
        fail "wrapper exists and is executable: $wrapper" "$path is missing or not executable"
    fi
done

# ══════════════════════════════════════════════════════════════════════════
# in-worktree.sh
# ══════════════════════════════════════════════════════════════════════════

REPO1=$(make_temp_repo)

# Test: Fails in main repo (not a worktree)
exit_code=0
(cd "$REPO1" && "$PREFLIGHT_DIR/in-worktree.sh") > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "in-worktree.sh exits non-zero in main repo"
else
    fail "in-worktree.sh exits non-zero in main repo" "expected non-zero exit"
fi

# Test: Succeeds inside a worktree
git -C "$REPO1" worktree add "$REPO1/wt1" -b feat-wt HEAD -q
exit_code=0
(cd "$REPO1/wt1" && "$PREFLIGHT_DIR/in-worktree.sh") > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "in-worktree.sh exits 0 inside a worktree"
else
    fail "in-worktree.sh exits 0 inside a worktree" "exit code: $exit_code"
fi

# Test: Fails outside any git repo
TEMP_NO_GIT=$(mktemp -d)
TEMP_DIRS+=("$TEMP_NO_GIT")
exit_code=0
(cd "$TEMP_NO_GIT" && "$PREFLIGHT_DIR/in-worktree.sh") > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "in-worktree.sh exits non-zero outside git repo"
else
    fail "in-worktree.sh exits non-zero outside git repo" "expected non-zero exit"
fi

# ══════════════════════════════════════════════════════════════════════════
# not-on-main.sh
# ══════════════════════════════════════════════════════════════════════════

REPO2=$(make_temp_repo)

# Test: Fails on main branch
exit_code=0
(cd "$REPO2" && "$PREFLIGHT_DIR/not-on-main.sh") > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "not-on-main.sh exits non-zero on main branch"
else
    fail "not-on-main.sh exits non-zero on main branch" "expected non-zero exit"
fi

# Test: Succeeds on feature branch
git -C "$REPO2" checkout -b feature-test -q
exit_code=0
(cd "$REPO2" && "$PREFLIGHT_DIR/not-on-main.sh") > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "not-on-main.sh exits 0 on feature branch"
else
    fail "not-on-main.sh exits 0 on feature branch" "exit code: $exit_code"
fi

# Test: Fails on master branch
git -C "$REPO2" checkout -b master -q
exit_code=0
(cd "$REPO2" && "$PREFLIGHT_DIR/not-on-main.sh") > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "not-on-main.sh exits non-zero on master branch"
else
    fail "not-on-main.sh exits non-zero on master branch" "expected non-zero exit"
fi

# ══════════════════════════════════════════════════════════════════════════
# branch-fresh.sh
# ══════════════════════════════════════════════════════════════════════════

REPO3=$(make_temp_repo)
git -C "$REPO3" remote add origin "$REPO3"
git -C "$REPO3" fetch origin main -q 2>/dev/null
git -C "$REPO3" checkout -b feature-fresh -q

# Test: Succeeds when branch is up-to-date
exit_code=0
(cd "$REPO3" && "$PREFLIGHT_DIR/branch-fresh.sh") > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "branch-fresh.sh exits 0 when up-to-date with origin/main"
else
    fail "branch-fresh.sh exits 0 when up-to-date with origin/main" "exit code: $exit_code"
fi

# Test: Fails when behind origin/main
git -C "$REPO3" checkout main -q
touch "$REPO3/newfile.txt"
git -C "$REPO3" add newfile.txt
git -C "$REPO3" commit -m "advance main" -q
git -C "$REPO3" fetch origin main -q 2>/dev/null
git -C "$REPO3" checkout feature-fresh -q

exit_code=0
(cd "$REPO3" && "$PREFLIGHT_DIR/branch-fresh.sh") > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "branch-fresh.sh exits non-zero when behind origin/main"
else
    fail "branch-fresh.sh exits non-zero when behind origin/main" "expected non-zero exit"
fi

# Test: --fetch flag is accepted without error
exit_code=0
(cd "$REPO3" && "$PREFLIGHT_DIR/branch-fresh.sh" --fetch) > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "--fetch flag accepted (still behind, non-zero exit expected)"
else
    fail "--fetch flag accepted" "expected non-zero exit because still behind"
fi

# ══════════════════════════════════════════════════════════════════════════
# venv-local.sh
# ══════════════════════════════════════════════════════════════════════════

VENV_DIR=$(mktemp -d)
TEMP_DIRS+=("$VENV_DIR")

# Test: Fails when .venv does not exist
exit_code=0
"$PREFLIGHT_DIR/venv-local.sh" "$VENV_DIR" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "venv-local.sh exits non-zero when .venv missing"
else
    fail "venv-local.sh exits non-zero when .venv missing" "expected non-zero exit"
fi

# Test: Succeeds when .venv is a real directory
mkdir -p "$VENV_DIR/.venv"
exit_code=0
"$PREFLIGHT_DIR/venv-local.sh" "$VENV_DIR" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "venv-local.sh exits 0 with real .venv directory"
else
    fail "venv-local.sh exits 0 with real .venv directory" "exit code: $exit_code"
fi

# Test: Fails when .venv is a symlink
rm -rf "$VENV_DIR/.venv"
OTHER_VENV=$(mktemp -d)
TEMP_DIRS+=("$OTHER_VENV")
mkdir -p "$OTHER_VENV/.venv"
ln -s "$OTHER_VENV/.venv" "$VENV_DIR/.venv"
exit_code=0
"$PREFLIGHT_DIR/venv-local.sh" "$VENV_DIR" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "venv-local.sh exits non-zero when .venv is a symlink"
else
    fail "venv-local.sh exits non-zero when .venv is a symlink" "expected non-zero exit"
fi

# ══════════════════════════════════════════════════════════════════════════
# tf-not-root.sh
# ══════════════════════════════════════════════════════════════════════════

TF_DIR=$(mktemp -d)
TEMP_DIRS+=("$TF_DIR")

# Test: Fails for root infra/terraform/ path
mkdir -p "$TF_DIR/infra/terraform"
exit_code=0
"$PREFLIGHT_DIR/tf-not-root.sh" "$TF_DIR/infra/terraform" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "tf-not-root.sh exits non-zero for root infra/terraform/"
else
    fail "tf-not-root.sh exits non-zero for root infra/terraform/" "expected non-zero exit"
fi

# Test: Succeeds for environment-specific path
mkdir -p "$TF_DIR/infra/terraform/environments/dev"
exit_code=0
"$PREFLIGHT_DIR/tf-not-root.sh" "$TF_DIR/infra/terraform/environments/dev" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "tf-not-root.sh exits 0 for environments/dev path"
else
    fail "tf-not-root.sh exits 0 for environments/dev path" "exit code: $exit_code"
fi

# ══════════════════════════════════════════════════════════════════════════
# no-duplicate-pr.sh (argument validation + mock-gh)
# ══════════════════════════════════════════════════════════════════════════

# Test: Exits 2 with no argument
exit_code=0
"$PREFLIGHT_DIR/no-duplicate-pr.sh" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "no-duplicate-pr.sh exits 2 with no argument"
else
    fail "no-duplicate-pr.sh exits 2 with no argument" "expected exit 2, got $exit_code"
fi

# Test with mock-gh: exits 1 when no duplicate found
MOCK_BIN_DIR=$(mktemp -d)
TEMP_DIRS+=("$MOCK_BIN_DIR")
ORIG_PATH_SAVE="$PATH"

# Mock gh: returns empty PR list and empty branch PR list
# The function checks both title-search and head-branch — we need to respond
# to both `gh pr list --search` and `gh pr list --head`.
cat > "$MOCK_BIN_DIR/gh" << 'MOCKGH'
#!/usr/bin/env bash
# Return an empty JSON array for all pr list calls
if [[ "$*" == *"pr list"* ]]; then
    echo "[]"
    exit 0
fi
exit 1
MOCKGH
chmod +x "$MOCK_BIN_DIR/gh"
export PATH="$MOCK_BIN_DIR:$ORIG_PATH_SAVE"

exit_code=0
"$PREFLIGHT_DIR/no-duplicate-pr.sh" 9999 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "no-duplicate-pr.sh exits 1 when no duplicate found (mock-gh)"
else
    fail "no-duplicate-pr.sh exits 1 when no duplicate found (mock-gh)" "expected exit 1, got $exit_code"
fi

# Restore PATH
export PATH="$ORIG_PATH_SAVE"

# ══════════════════════════════════════════════════════════════════════════
# rate-budget.sh (with mock-gh)
# ══════════════════════════════════════════════════════════════════════════

MOCK_BIN_DIR2=$(mktemp -d)
TEMP_DIRS+=("$MOCK_BIN_DIR2")
ORIG_PATH_SAVE2="$PATH"

# Test: Returns 0 when budget is sufficient
cat > "$MOCK_BIN_DIR2/gh" << 'MOCKGH'
#!/usr/bin/env bash
echo "4500 9999999999 5000"
exit 0
MOCKGH
chmod +x "$MOCK_BIN_DIR2/gh"
export PATH="$MOCK_BIN_DIR2:$ORIG_PATH_SAVE2"

exit_code=0
"$PREFLIGHT_DIR/rate-budget.sh" 100 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "rate-budget.sh exits 0 when budget sufficient (mock-gh)"
else
    fail "rate-budget.sh exits 0 when budget sufficient (mock-gh)" "exit code: $exit_code"
fi

# Test: Returns 1 when budget is low
cat > "$MOCK_BIN_DIR2/gh" << 'MOCKGH'
#!/usr/bin/env bash
echo "50 9999999999 5000"
exit 0
MOCKGH
chmod +x "$MOCK_BIN_DIR2/gh"

exit_code=0
"$PREFLIGHT_DIR/rate-budget.sh" 100 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "rate-budget.sh exits 1 when budget low (mock-gh)"
else
    fail "rate-budget.sh exits 1 when budget low (mock-gh)" "expected exit 1, got $exit_code"
fi

# Test: Returns 2 when gh api fails
cat > "$MOCK_BIN_DIR2/gh" << 'MOCKGH'
#!/usr/bin/env bash
exit 1
MOCKGH
chmod +x "$MOCK_BIN_DIR2/gh"

exit_code=0
"$PREFLIGHT_DIR/rate-budget.sh" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "rate-budget.sh exits 2 when gh api fails (mock-gh)"
else
    fail "rate-budget.sh exits 2 when gh api fails (mock-gh)" "expected exit 2, got $exit_code"
fi

# Test: Uses default threshold of 100
cat > "$MOCK_BIN_DIR2/gh" << 'MOCKGH'
#!/usr/bin/env bash
echo "99 9999999999 5000"
exit 0
MOCKGH
chmod +x "$MOCK_BIN_DIR2/gh"

exit_code=0
"$PREFLIGHT_DIR/rate-budget.sh" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "rate-budget.sh uses default threshold of 100 (mock-gh)"
else
    fail "rate-budget.sh uses default threshold of 100 (mock-gh)" "expected exit 1, got $exit_code"
fi

# Restore PATH
export PATH="$ORIG_PATH_SAVE2"

# ══════════════════════════════════════════════════════════════════════════
# no-forbidden-syntax.sh
# ══════════════════════════════════════════════════════════════════════════

# Test: Detects $() substitution
exit_code=0
"$PREFLIGHT_DIR/no-forbidden-syntax.sh" 'echo $(whoami)' > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "no-forbidden-syntax.sh detects \$() substitution"
else
    fail "no-forbidden-syntax.sh detects \$() substitution" "expected non-zero exit"
fi

# Test: Detects heredoc
exit_code=0
"$PREFLIGHT_DIR/no-forbidden-syntax.sh" 'cat <<EOF' > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "no-forbidden-syntax.sh detects heredoc"
else
    fail "no-forbidden-syntax.sh detects heredoc" "expected non-zero exit"
fi

# Test: Detects python -c
exit_code=0
"$PREFLIGHT_DIR/no-forbidden-syntax.sh" 'python3 -c "print(1)"' > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "no-forbidden-syntax.sh detects python -c"
else
    fail "no-forbidden-syntax.sh detects python -c" "expected non-zero exit"
fi

# Test: Allows safe commands
exit_code=0
"$PREFLIGHT_DIR/no-forbidden-syntax.sh" 'git status' > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "no-forbidden-syntax.sh exits 0 for safe command"
else
    fail "no-forbidden-syntax.sh exits 0 for safe command" "exit code: $exit_code"
fi

# Test: Allows $HOME (not a substitution)
exit_code=0
"$PREFLIGHT_DIR/no-forbidden-syntax.sh" 'echo $HOME' > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "no-forbidden-syntax.sh exits 0 for \$HOME (not substitution)"
else
    fail "no-forbidden-syntax.sh exits 0 for \$HOME (not substitution)" "exit code: $exit_code"
fi

# ── Summary ──────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
