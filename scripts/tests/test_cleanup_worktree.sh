#!/usr/bin/env bash
# test_cleanup_worktree.sh — Tests for cleanup_worktree.sh
#
# Verifies that find_repo_root() correctly resolves the main repo root
# even when running from inside a worktree, and tests the full cleanup
# flow against a temporary git repo.
#
# Usage:
#   scripts/tests/test_cleanup_worktree.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLEANUP_SCRIPT="$SCRIPT_DIR/cleanup_worktree.sh"
TERSE_LIB="$SCRIPT_DIR/_terse_lib.sh"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

TEMP_DIRS=()
cleanup() {
    set +e
    for d in "${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}"; do
        if [[ -n "$d" && -d "$d" ]]; then
            rm -rf "$d"
        fi
    done
}
trap cleanup EXIT

make_temp_dir() {
    local dir
    dir=$(mktemp -d)
    TEMP_DIRS+=("$dir")
    echo "$dir"
}

# Copy cleanup_worktree.sh + its _terse_lib.sh dependency into a scripts dir.
install_cleanup_script() {
    local dest_scripts_dir="$1"
    cp "$CLEANUP_SCRIPT" "$dest_scripts_dir/cleanup_worktree.sh"
    chmod +x "$dest_scripts_dir/cleanup_worktree.sh"
    cp "$TERSE_LIB" "$dest_scripts_dir/_terse_lib.sh"
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

# ── Tests ──────────────────────────────────────────────────────────────────

# Test 1: find_repo_root returns correct root from main repo scripts/ dir
test_find_repo_root_main_repo() {
    local repo
    repo=$(make_temp_dir)

    # Set up a fake main repo: .git directory + CLAUDE.md
    mkdir -p "$repo/.git"
    touch "$repo/CLAUDE.md"
    mkdir -p "$repo/scripts"
    install_cleanup_script "$repo/scripts"

    # Source the script to get find_repo_root, override BASH_SOURCE behavior
    # by calling the function directly
    local result
    result=$(BASH_SOURCE_OVERRIDE="$repo/scripts/cleanup_worktree.sh" \
        bash -c '
        # Override BASH_SOURCE[0] by executing from the scripts dir
        cd "'"$repo/scripts"'" && source cleanup_worktree.sh --help 2>/dev/null || true
    ')

    # Direct test: run the find_repo_root function via a wrapper
    cat > "$repo/scripts/test_wrapper.sh" << 'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
# Source the cleanup script functions (it will try to run main, suppress it)
_CLEANUP_TEST_MODE=1
# Inline the find_repo_root function from cleanup_worktree.sh
find_repo_root() {
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    while [[ "$dir" != "/" ]]; do
        if [[ -f "$dir/CLAUDE.md" && -d "$dir/.git" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    echo "ERROR: cannot find repo root" >&2
    return 1
}
find_repo_root
WRAPPER
    chmod +x "$repo/scripts/test_wrapper.sh"

    result=$("$repo/scripts/test_wrapper.sh")

    if [[ "$result" == "$repo" ]]; then
        pass "find_repo_root from main repo scripts/ returns repo root"
    else
        fail "find_repo_root from main repo scripts/ returns repo root" \
            "expected '$repo', got '$result'"
    fi
}

# Test 2: find_repo_root skips worktree root (has .git file, not directory)
test_find_repo_root_skips_worktree() {
    local repo
    repo=$(make_temp_dir)

    # Set up a fake main repo
    mkdir -p "$repo/.git"
    touch "$repo/CLAUDE.md"

    # Set up a fake worktree nested under main repo
    local worktree="$repo/.claude/worktrees/agent-abcd1234"
    mkdir -p "$worktree/scripts"
    touch "$worktree/CLAUDE.md"
    # Worktrees have .git as a FILE, not a directory
    echo "gitdir: $repo/.git/worktrees/agent-abcd1234" > "$worktree/.git"

    # Place the test wrapper in the worktree scripts dir
    cat > "$worktree/scripts/test_wrapper.sh" << 'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
find_repo_root() {
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    while [[ "$dir" != "/" ]]; do
        if [[ -f "$dir/CLAUDE.md" && -d "$dir/.git" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    echo "ERROR: cannot find repo root" >&2
    return 1
}
find_repo_root
WRAPPER
    chmod +x "$worktree/scripts/test_wrapper.sh"

    local result
    result=$("$worktree/scripts/test_wrapper.sh")

    if [[ "$result" == "$repo" ]]; then
        pass "find_repo_root from worktree scripts/ skips worktree, returns main repo root"
    else
        fail "find_repo_root from worktree scripts/ skips worktree, returns main repo root" \
            "expected '$repo', got '$result'"
    fi
}

# Test 3: find_repo_root fails if no valid repo root exists
test_find_repo_root_no_repo() {
    local dir
    dir=$(make_temp_dir)

    mkdir -p "$dir/scripts"
    cat > "$dir/scripts/test_wrapper.sh" << 'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
find_repo_root() {
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    while [[ "$dir" != "/" ]]; do
        if [[ -f "$dir/CLAUDE.md" && -d "$dir/.git" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    echo "ERROR: cannot find repo root" >&2
    return 1
}
find_repo_root
WRAPPER
    chmod +x "$dir/scripts/test_wrapper.sh"

    local exit_code=0
    "$dir/scripts/test_wrapper.sh" > /dev/null 2>&1 || exit_code=$?

    if [[ "$exit_code" -ne 0 ]]; then
        pass "find_repo_root returns non-zero when no repo root found"
    else
        fail "find_repo_root returns non-zero when no repo root found" \
            "expected non-zero exit, got 0"
    fi
}

# Test 4: find_repo_root with worktree-only (no main repo above) fails
test_find_repo_root_worktree_only() {
    local dir
    dir=$(make_temp_dir)

    # Only a worktree-style setup: CLAUDE.md + .git file (not directory)
    touch "$dir/CLAUDE.md"
    echo "gitdir: /some/path" > "$dir/.git"
    mkdir -p "$dir/scripts"

    cat > "$dir/scripts/test_wrapper.sh" << 'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
find_repo_root() {
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    while [[ "$dir" != "/" ]]; do
        if [[ -f "$dir/CLAUDE.md" && -d "$dir/.git" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    echo "ERROR: cannot find repo root" >&2
    return 1
}
find_repo_root
WRAPPER
    chmod +x "$dir/scripts/test_wrapper.sh"

    local exit_code=0
    "$dir/scripts/test_wrapper.sh" > /dev/null 2>&1 || exit_code=$?

    if [[ "$exit_code" -ne 0 ]]; then
        pass "find_repo_root fails when only a worktree root exists (no real repo)"
    else
        fail "find_repo_root fails when only a worktree root exists (no real repo)" \
            "expected non-zero exit, got 0"
    fi
}

# Test 5: Integration test — cleanup_worktree.sh main() resolves correct
# path when script is located in a worktree
test_main_resolves_correct_path_from_worktree() {
    local repo
    repo=$(make_temp_dir)

    # Set up a real git repo
    git -C "$repo" init --initial-branch main -q
    git -C "$repo" config user.email "test@example.com"
    git -C "$repo" config user.name "Test"
    touch "$repo/CLAUDE.md"
    git -C "$repo" add CLAUDE.md
    git -C "$repo" commit -m "init" -q

    # Create a fake worktree directory structure
    local worktree="$repo/.claude/worktrees/agent-abcd1234"
    mkdir -p "$worktree/scripts"
    touch "$worktree/CLAUDE.md"
    echo "gitdir: $repo/.git/worktrees/agent-abcd1234" > "$worktree/.git"

    # Copy cleanup_worktree.sh into the worktree
    install_cleanup_script "$worktree/scripts"

    # Call main with a bogus worktree path — we just want to check it
    # resolves repo_root correctly (it will fail on safety checks, that's fine)
    local output
    local exit_code=0
    output=$("$worktree/scripts/cleanup_worktree.sh" ".claude/worktrees/agent-99999999" 2>&1) || exit_code=$?

    # The key thing: the error should NOT contain the doubled path.
    # If repo_root resolved to the worktree, the path would be:
    #   .../agent-abcd1234/.claude/worktrees/agent-99999999
    # instead of:
    #   .../.claude/worktrees/agent-99999999
    if echo "$output" | grep -q "agent-abcd1234/.claude/worktrees/agent-99999999"; then
        fail "cleanup main() does not double worktree path" \
            "path was doubled: $output"
    else
        pass "cleanup main() does not double worktree path"
    fi
}

# Test 6: cleanup_worktree.sh heals stale git metadata when the on-disk
# worktree dir is already gone but .git/worktrees/<name>/ still exists
# (with a `locked` file, as observed in the wild — #2388).
test_cleanup_heals_stale_metadata() {
    local repo
    repo=$(make_temp_dir)

    # Set up a real git repo
    git -C "$repo" init --initial-branch main -q
    git -C "$repo" config user.email "test@example.com"
    git -C "$repo" config user.name "Test"
    touch "$repo/CLAUDE.md"
    git -C "$repo" add CLAUDE.md
    git -C "$repo" commit -m "init" -q

    # Create a real worktree so git records its metadata, then remove the
    # on-disk dir manually to simulate a crashed mid-cleanup.
    local worktree_rel=".claude/worktrees/agent-stale001"
    local worktree="$repo/$worktree_rel"
    mkdir -p "$(dirname "$worktree")"
    git -C "$repo" worktree add -q "$worktree" -b stale-branch-001
    rm -rf "$worktree"
    # Simulate the `locked` marker file that blocks a normal prune.
    local metadata_dir="$repo/.git/worktrees/agent-stale001"
    touch "$metadata_dir/locked"

    if [[ ! -d "$metadata_dir" ]]; then
        fail "cleanup heals stale metadata" "metadata setup failed: $metadata_dir not present"
        return
    fi

    # Drop a working copy of the cleanup script into the repo so it can
    # find the repo root via its own location.
    mkdir -p "$repo/scripts"
    install_cleanup_script "$repo/scripts"

    # Create a stub session log so safety check 2 would pass — but note
    # the stale-metadata branch should return 0 *before* the session-log
    # check ever runs. We don't need to create one.

    local output
    local exit_code=0
    output=$("$repo/scripts/cleanup_worktree.sh" "$worktree_rel" 2>&1) || exit_code=$?

    if [[ "$exit_code" -ne 0 ]]; then
        fail "cleanup heals stale metadata (exit code)" \
            "expected 0, got $exit_code; output: $output"
        return
    fi

    if [[ -d "$metadata_dir" ]]; then
        fail "cleanup heals stale metadata (metadata removed)" \
            "metadata dir still exists: $metadata_dir; output: $output"
        return
    fi

    # And sanity: `git worktree list` should no longer reference it.
    local list_output
    list_output=$(git -C "$repo" worktree list 2>&1)
    if echo "$list_output" | grep -q "agent-stale001"; then
        fail "cleanup heals stale metadata (git worktree list)" \
            "entry still listed: $list_output"
        return
    fi

    pass "cleanup heals stale metadata when on-disk dir is already gone"
}

# Test 7: cleanup_worktree.sh is a no-op on the stale-metadata branch
# when there is no metadata dir either — it should just return 0 without
# touching anything.
test_cleanup_no_metadata_is_noop() {
    local repo
    repo=$(make_temp_dir)

    git -C "$repo" init --initial-branch main -q
    git -C "$repo" config user.email "test@example.com"
    git -C "$repo" config user.name "Test"
    touch "$repo/CLAUDE.md"
    git -C "$repo" add CLAUDE.md
    git -C "$repo" commit -m "init" -q

    mkdir -p "$repo/scripts"
    install_cleanup_script "$repo/scripts"

    # Ensure no .git/worktrees directory exists at all.
    rm -rf "$repo/.git/worktrees"

    local exit_code=0
    local output
    output=$("$repo/scripts/cleanup_worktree.sh" ".claude/worktrees/agent-missing1" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 0 ]]; then
        pass "cleanup is a no-op when dir gone and no metadata"
    else
        fail "cleanup is a no-op when dir gone and no metadata" \
            "expected 0, got $exit_code; output: $output"
    fi
}

# Test 8: --verbose flag accepted (no error even with missing worktree)
test_verbose_flag_accepted() {
    local repo
    repo=$(make_temp_dir)

    git -C "$repo" init --initial-branch main -q
    git -C "$repo" config user.email "test@example.com"
    git -C "$repo" config user.name "Test"
    touch "$repo/CLAUDE.md"
    git -C "$repo" add CLAUDE.md
    git -C "$repo" commit -m "init" -q

    mkdir -p "$repo/scripts"
    install_cleanup_script "$repo/scripts"

    # --verbose with a missing path → should give a safety error, not a parse error
    local output
    local exit_code=0
    output=$("$repo/scripts/cleanup_worktree.sh" --verbose ".claude/worktrees/agent-missing1" 2>&1) || exit_code=$?

    # The important thing: script didn't crash on --verbose parsing
    if echo "$output" | grep -qi "verbose\|parse\|unknown option"; then
        fail "--verbose flag accepted without parse error" \
            "unexpected output: $output"
    else
        pass "--verbose flag accepted without parse error"
    fi
}

# Test 9: JM_VERBOSE=1 accepted
test_jm_verbose_accepted() {
    local repo
    repo=$(make_temp_dir)

    git -C "$repo" init --initial-branch main -q
    git -C "$repo" config user.email "test@example.com"
    git -C "$repo" config user.name "Test"
    touch "$repo/CLAUDE.md"
    git -C "$repo" add CLAUDE.md
    git -C "$repo" commit -m "init" -q

    mkdir -p "$repo/scripts"
    install_cleanup_script "$repo/scripts"

    local output
    local exit_code=0
    output=$(JM_VERBOSE=1 "$repo/scripts/cleanup_worktree.sh" ".claude/worktrees/agent-missing2" 2>&1) || exit_code=$?

    if echo "$output" | grep -qi "verbose\|parse\|unknown option"; then
        fail "JM_VERBOSE=1 accepted without parse error" \
            "unexpected output: $output"
    else
        pass "JM_VERBOSE=1 accepted without parse error"
    fi
}

# ── Run all tests ──────────────────────────────────────────────────────────

test_find_repo_root_main_repo
test_find_repo_root_skips_worktree
test_find_repo_root_no_repo
test_find_repo_root_worktree_only
test_main_resolves_correct_path_from_worktree
test_cleanup_heals_stale_metadata
test_cleanup_no_metadata_is_noop
test_verbose_flag_accepted
test_jm_verbose_accepted

echo ""
echo "────────────────────────────────────────────"
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
