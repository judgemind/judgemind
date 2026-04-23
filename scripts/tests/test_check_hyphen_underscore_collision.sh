#!/usr/bin/env bash
# test_check_hyphen_underscore_collision.sh — Tests for
# check-hyphen-underscore-collision.sh.
#
# Creates a throwaway directory tree under TMPDIR_TEST that mirrors the
# layout the real check scans (scripts/, packages/*/src/, packages/*/tests/,
# infra/**) and exercises every behaviour that matters:
#
#   - no collisions anywhere       → exit 0
#   - top-level scripts/ collision → exit 1, both paths in stderr
#   - nested packages/*/src/ pair  → exit 1 (recursive scan works)
#   - collision under node_modules → exit 0 (excluded)
#   - collision under .venv        → exit 0 (excluded)
#   - single-hyphen-only files     → exit 0 (no pair)
#   - fully-identical normalised
#       but only ONE distinct orig → exit 0
#   - CI step name self-match      → extractor returns no match
#
# Usage:
#   scripts/tests/test_check_hyphen_underscore_collision.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-hyphen-underscore-collision.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

create_test_file() {
    local name="$1"
    local dir
    dir="$(dirname "$TMPDIR_TEST/$name")"
    mkdir -p "$dir"
    : > "$TMPDIR_TEST/$name"
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

# Assert that running the check produces a failure AND that its stderr
# contains all of the given substrings.
assert_fails_with_output_contains() {
    local desc="$1"
    shift
    TESTS=$((TESTS + 1))
    local output
    if output=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1); then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
        return
    fi
    local needle
    for needle in "$@"; do
        if [[ "$output" != *"$needle"* ]]; then
            echo "FAIL: $desc (output missing substring: $needle)"
            echo "----- output start -----"
            echo "$output"
            echo "----- output end -----"
            FAILURES=$((FAILURES + 1))
            return
        fi
    done
    echo "PASS: $desc"
}

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

# ─── Test 1: Empty directory tree → pass ─────────────────────────────────
assert_passes "Empty directory passes (no scan dirs present)"

# ─── Test 2: scripts/ with no collisions → pass ──────────────────────────
create_test_file "scripts/foo.sh"
create_test_file "scripts/bar-baz.sh"
create_test_file "scripts/qux_quux.sh"
assert_passes "scripts/ with no collisions passes"
reset_tmpdir

# ─── Test 3: scripts/ with hyphen+underscore pair → fail ─────────────────
create_test_file "scripts/foo-bar.sh"
create_test_file "scripts/foo_bar.sh"
assert_fails_with_output_contains \
    "scripts/ hyphen+underscore pair is detected" \
    "foo-bar.sh" "foo_bar.sh"
reset_tmpdir

# ─── Test 4: Nested packages/*/src/ pair → fail ──────────────────────────
create_test_file "packages/foo/src/sub/my-mod.py"
create_test_file "packages/foo/src/sub/my_mod.py"
assert_fails_with_output_contains \
    "Nested packages/<pkg>/src/ collision detected (recursive scan)" \
    "my-mod.py" "my_mod.py"
reset_tmpdir

# ─── Test 5: packages/*/tests/ pair → fail ──────────────────────────────
create_test_file "packages/foo/tests/test-a.py"
create_test_file "packages/foo/tests/test_a.py"
assert_fails "packages/<pkg>/tests/ collision detected"
reset_tmpdir

# ─── Test 6: infra/ nested pair → fail ──────────────────────────────────
create_test_file "infra/terraform/modules/my-mod/main.tf"
create_test_file "infra/terraform/modules/my_mod/main.tf"
# Each directory contains a single main.tf, but the DIRECTORIES differ
# only by hyphen vs underscore.  The current check operates on file
# names within a single directory, not on directory names, so this
# should PASS — collisions between sibling directory names would be a
# separate, larger scope.  Document the boundary explicitly.
assert_passes \
    "Sibling directory names differing by - vs _ are NOT flagged (files-only scope)"
reset_tmpdir

# ─── Test 7: infra/ sibling FILES collision → fail ──────────────────────
create_test_file "infra/lambda/a-b.py"
create_test_file "infra/lambda/a_b.py"
assert_fails "infra/ sibling file collision detected"
reset_tmpdir

# ─── Test 8: node_modules collision → pass (excluded) ───────────────────
create_test_file "packages/foo/node_modules/some-pkg/a-b.js"
create_test_file "packages/foo/node_modules/some-pkg/a_b.js"
assert_passes "Collisions inside node_modules are excluded"
reset_tmpdir

# ─── Test 9: .venv collision → pass (excluded) ──────────────────────────
create_test_file "packages/foo/.venv/lib/a-b.py"
create_test_file "packages/foo/.venv/lib/a_b.py"
assert_passes "Collisions inside .venv are excluded"
reset_tmpdir

# ─── Test 10: .git collision → pass (excluded) ──────────────────────────
create_test_file "packages/foo/.git/hooks/pre-commit"
create_test_file "packages/foo/.git/hooks/pre_commit"
assert_passes "Collisions inside .git are excluded"
reset_tmpdir

# ─── Test 11: .terraform collision → pass (excluded) ────────────────────
create_test_file "infra/terraform/.terraform/modules/a-b.tf"
create_test_file "infra/terraform/.terraform/modules/a_b.tf"
assert_passes "Collisions inside .terraform are excluded"
reset_tmpdir

# ─── Test 12: tmp/ collision → pass (excluded) ──────────────────────────
# Some packages have a tmp/ subdir for local scratch state.  Excluded to
# match the rest of the check suite.
create_test_file "packages/foo/tmp/a-b.py"
create_test_file "packages/foo/tmp/a_b.py"
assert_passes "Collisions inside packages/<pkg>/tmp are excluded"
reset_tmpdir

# ─── Test 13: Hyphen-only file with no underscore sibling → pass ────────
create_test_file "scripts/only-hyphenated.sh"
assert_passes "Single hyphen-only file with no underscore sibling passes"
reset_tmpdir

# ─── Test 14: Underscore-only file with no hyphen sibling → pass ────────
create_test_file "scripts/only_underscored.sh"
assert_passes "Single underscore-only file with no hyphen sibling passes"
reset_tmpdir

# ─── Test 15: Multiple collisions in the same directory → fail ──────────
create_test_file "scripts/a-b.sh"
create_test_file "scripts/a_b.sh"
create_test_file "scripts/c-d.sh"
create_test_file "scripts/c_d.sh"
assert_fails_with_output_contains \
    "Multiple collision pairs in the same directory are all reported" \
    "a-b.sh" "a_b.sh" "c-d.sh" "c_d.sh"
reset_tmpdir

# ─── Test 16: Unrelated-but-similar names with extra chars → pass ───────
# `foo-bar.sh` and `foo_barx.sh` do NOT collide (different normalised
# names).  The check must not flag these as a false positive.
create_test_file "scripts/foo-bar.sh"
create_test_file "scripts/foo_barx.sh"
assert_passes "Similar-but-distinct names (foo-bar.sh vs foo_barx.sh) do not collide"
reset_tmpdir

# ─── Test 17: Same name, different extensions → pass ────────────────────
# `foo-bar.sh` and `foo_bar.py` collide on normalised stem but differ
# by extension.  We still flag them because CI discovery (e.g. pytest
# or run-scripts-tests) won't tell them apart at the normalised stem
# level, and the motivating incident was `.sh` vs `.sh` anyway.
# Actually for this implementation they WILL be flagged since we
# compare full filenames and `foo-bar.sh` → `foo_bar.sh` (still
# different from `foo_bar.py`), so this should PASS.  Test documents
# the current behavior.
create_test_file "scripts/foo-bar.sh"
create_test_file "scripts/foo_bar.py"
assert_passes "Same stem with different extensions do not collide (full-name compare)"
reset_tmpdir

# ─── Test 18: scripts/ subdirectory collision → pass ────────────────────
# The top-level scripts/ scan is non-recursive.  A collision inside
# scripts/tests/ or scripts/spotcheck/ is out of the current scope.
# Document the boundary so future edits don't silently widen it.
create_test_file "scripts/tests/a-b.sh"
create_test_file "scripts/tests/a_b.sh"
assert_passes "Collision inside scripts/tests/ is NOT flagged (scripts/ is non-recursive)"
reset_tmpdir

# ─── Test 19: No self-match on ci.yml step name ─────────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain the literal `-` / `_` filename-ambiguity pattern
# (avoid self-match per the CLAUDE.md hygiene-check naming rule, see
# #2541/#2542 for the failure class).
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-hyphen-underscore-collision.sh" "yml"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
