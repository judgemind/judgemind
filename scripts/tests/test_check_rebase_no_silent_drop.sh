#!/usr/bin/env bash
# test_check_rebase_no_silent_drop.sh — Tests for check-rebase-no-silent-drop.sh.
#
# Creates isolated git fixture repositories to verify that the checker
# correctly detects silent marker drops while passing when:
#   - Both sides' markers survive the merge
#   - git merge-tree emits conflict markers (visible conflict, not silent)
#   - origin/main is unreachable (shallow clone / no remote)
#
# Usage:
#   scripts/tests/test_check_rebase_no_silent_drop.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-rebase-no-silent-drop.sh"
FAILURES=0
TESTS=0

# Use a temp directory for fixture repos.
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# ─── Helpers ─────────────────────────────────────────────────────────────────

# assert_fails <desc> <repo_dir>
assert_fails() {
    local desc="$1"
    local repo_dir="$2"
    TESTS=$((TESTS + 1))
    if REPO_ROOT="$repo_dir" "$CHECK_SCRIPT" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        REPO_ROOT="$repo_dir" "$CHECK_SCRIPT" 2>&1 | sed 's/^/    /' || true
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

# assert_passes <desc> <repo_dir>
assert_passes() {
    local desc="$1"
    local repo_dir="$2"
    TESTS=$((TESTS + 1))
    if REPO_ROOT="$repo_dir" "$CHECK_SCRIPT" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        echo "  Output:"
        REPO_ROOT="$repo_dir" "$CHECK_SCRIPT" 2>&1 | sed 's/^/    /' || true
        FAILURES=$((FAILURES + 1))
    fi
}

# init_repo <dir>
# Initialises a bare-minimum git repo with git identity.
init_repo() {
    local dir="$1"
    mkdir -p "$dir"
    git -C "$dir" init -q
    git -C "$dir" config user.email "test@example.com"
    git -C "$dir" config user.name "Test"
    git -C "$dir" checkout -q -b main 2>/dev/null || true
}

# commit_file <dir> <relative-path> <content> <message>
commit_file() {
    local dir="$1"
    local rel="$2"
    local content="$3"
    local msg="$4"
    local abs_path="$dir/$rel"
    mkdir -p "$(dirname "$abs_path")"
    printf '%s\n' "$content" > "$abs_path"
    git -C "$dir" add "$rel"
    git -C "$dir" commit -q -m "$msg"
}

# ─── Test 1: Silent drop detected → script exits 1 ───────────────────────────
# origin/main has "# Test T_issue9001" in the watched file.
# HEAD replaces that marker line with "# Test T_issue9002" (same file position).
# git merge-tree: base=T_issue9001, main unchanged=T_issue9001, HEAD=T_issue9002.
# merge-tree accepts HEAD's change (only HEAD changed this line): result has T_issue9002.
# No conflict markers, T_issue9001 is silently absent → violation detected.
{
    fix1="$TMPDIR_TEST/fix1"
    bare1="$TMPDIR_TEST/bare1.git"
    init_repo "$fix1"

    # base (= what will become origin/main): file has T_issue9001
    commit_file "$fix1" "scripts/tests/test_agent_runner_entrypoint.sh" \
        "# preamble
# Test T_issue9001: marker from base" \
        "base: add T_issue9001"

    git init -q --bare "$bare1"
    git -C "$fix1" remote add origin "$bare1"
    git -C "$fix1" push -q origin main

    # HEAD branch replaces T_issue9001 with T_issue9002
    git -C "$fix1" checkout -q -b topic
    printf '%s\n' "# preamble
# Test T_issue9002: marker from topic" > "$fix1/scripts/tests/test_agent_runner_entrypoint.sh"
    git -C "$fix1" add "scripts/tests/test_agent_runner_entrypoint.sh"
    git -C "$fix1" commit -q -m "topic: replace T_issue9001 with T_issue9002"

    # merge-tree: only HEAD changed the line → result has T_issue9002, not T_issue9001.
    # origin/main has T_issue9001, merged lacks it → silent drop → exits 1.
    assert_fails "Silent drop detected: T_issue9001 in origin/main but absent from merge-tree result" "$fix1"
}

# ─── Test 2: Both markers preserved → script exits 0 ─────────────────────────
# origin/main appends T_issue9003 on a new line (relative to base).
# HEAD branches from base and appends T_issue9004 on a different new line.
# git merge-tree can combine both additions without conflict → both present.
{
    fix2="$TMPDIR_TEST/fix2"
    bare2="$TMPDIR_TEST/bare2.git"
    init_repo "$fix2"

    # base: file is empty (no markers yet)
    commit_file "$fix2" "scripts/tests/test_agent_runner_entrypoint.sh" \
        "# preamble" \
        "base"

    # origin/main: appends T_issue9003
    printf '%s\n' "# preamble
# Test T_issue9003: marker from main" > "$fix2/scripts/tests/test_agent_runner_entrypoint.sh"
    git -C "$fix2" add "scripts/tests/test_agent_runner_entrypoint.sh"
    git -C "$fix2" commit -q -m "main: add T_issue9003"

    git init -q --bare "$bare2"
    git -C "$fix2" remote add origin "$bare2"
    git -C "$fix2" push -q origin main

    # HEAD branches from base and appends T_issue9004
    base_sha2=$(git -C "$fix2" rev-list --max-parents=0 HEAD)
    git -C "$fix2" checkout -q -b topic "$base_sha2"
    printf '%s\n' "# preamble
# Test T_issue9004: marker from topic" > "$fix2/scripts/tests/test_agent_runner_entrypoint.sh"
    git -C "$fix2" add "scripts/tests/test_agent_runner_entrypoint.sh"
    git -C "$fix2" commit -q -m "topic: add T_issue9004"

    # Both sides added new marker lines (different content, same position).
    # git merge-tree will combine them (or conflict, which also passes).
    assert_passes "Both markers preserved: T_issue9003 and T_issue9004 both in merge-tree result" "$fix2"
}

# ─── Test 3: merge-tree emits conflict markers → script exits 0 ───────────────
# Both sides modify the same line to different incompatible content.
# git merge-tree emits <<<<<<<; check exits 0 (visible conflict, not silent drop).
{
    fix3="$TMPDIR_TEST/fix3"
    bare3="$TMPDIR_TEST/bare3.git"
    init_repo "$fix3"

    commit_file "$fix3" "scripts/tests/test_agent_runner_entrypoint.sh" \
        "# preamble
# shared line" \
        "base"

    # origin/main: changes shared line to T_issue9005
    printf '%s\n' "# preamble
# Test T_issue9005: main version" > "$fix3/scripts/tests/test_agent_runner_entrypoint.sh"
    git -C "$fix3" add "scripts/tests/test_agent_runner_entrypoint.sh"
    git -C "$fix3" commit -q -m "main: change to T_issue9005"

    git init -q --bare "$bare3"
    git -C "$fix3" remote add origin "$bare3"
    git -C "$fix3" push -q origin main

    # HEAD branches from base and changes the SAME shared line to T_issue9006
    base_sha3=$(git -C "$fix3" rev-list --max-parents=0 HEAD)
    git -C "$fix3" checkout -q -b topic "$base_sha3"
    printf '%s\n' "# preamble
# Test T_issue9006: topic version" > "$fix3/scripts/tests/test_agent_runner_entrypoint.sh"
    git -C "$fix3" add "scripts/tests/test_agent_runner_entrypoint.sh"
    git -C "$fix3" commit -q -m "topic: change to T_issue9006"

    # Both sides changed the same line → merge-tree emits <<<<<<< conflict markers.
    # The check detects conflict markers and exits 0 (user-visible conflict).
    assert_passes "Conflict markers present: check exits 0 (visible conflict, not silent drop)" "$fix3"
}

# ─── Test 4: origin/main unreachable → script exits 0 ────────────────────────
# Repo has no origin remote — simulates shallow clone or fresh local repo.
# The check should detect the missing origin/main and exit 0 gracefully.
{
    fix4="$TMPDIR_TEST/fix4"
    init_repo "$fix4"

    commit_file "$fix4" "scripts/tests/test_agent_runner_entrypoint.sh" \
        "# preamble
# Test T_issue9007: some marker" \
        "base"

    # No origin remote — origin/main is unreachable.
    assert_passes "origin/main unreachable: check skips gracefully and exits 0" "$fix4"
}

# ─── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
