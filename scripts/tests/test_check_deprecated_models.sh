#!/usr/bin/env bash
# test_check_deprecated_models.sh — Tests for check-deprecated-models.sh
#
# Creates temporary files to verify that the checker correctly detects
# deprecated Anthropic model identifiers in source code.
#
# Usage:
#   scripts/tests/test_check_deprecated_models.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-deprecated-models.sh"
FAILURES=0
TESTS=0

# Use a temp directory so we don't pollute the repo
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

create_test_file() {
    local name="$1"
    local content="$2"
    local path="$TMPDIR_TEST/$name"
    printf '%s\n' "$content" > "$path"
    echo "$path"
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

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
}

# ─── Test 1: claude-3-5-haiku-latest should fail ─────────────────────────
create_test_file "bad1.py" 'model = "claude-3-5-haiku-latest"'
assert_fails "claude-3-5-haiku-latest is detected"
reset_tmpdir

# ─── Test 2: claude-3-haiku-20240307 should fail ─────────────────────────
create_test_file "bad2.py" 'MODEL_ID = "claude-3-haiku-20240307"'
assert_fails "claude-3-haiku-20240307 is detected"
reset_tmpdir

# ─── Test 3: claude-3-sonnet-20240229 should fail ────────────────────────
create_test_file "bad3.ts" 'const model = "claude-3-sonnet-20240229";'
assert_fails "claude-3-sonnet-20240229 is detected"
reset_tmpdir

# ─── Test 4: claude-3-opus-20240229 should fail ──────────────────────────
create_test_file "bad4.py" 'MODEL = "claude-3-opus-20240229"'
assert_fails "claude-3-opus-20240229 is detected"
reset_tmpdir

# ─── Test 5: claude-3-5-sonnet-latest should fail ────────────────────────
create_test_file "bad5.py" 'model="claude-3-5-sonnet-latest"'
assert_fails "claude-3-5-sonnet-latest is detected"
reset_tmpdir

# ─── Test 6: claude-3-5-sonnet-20240620 should fail ─────────────────────
create_test_file "bad6.py" 'model="claude-3-5-sonnet-20240620"'
assert_fails "claude-3-5-sonnet-20240620 is detected"
reset_tmpdir

# ─── Test 7: claude-3-5-sonnet-20241022 should fail ─────────────────────
create_test_file "bad7.py" 'model="claude-3-5-sonnet-20241022"'
assert_fails "claude-3-5-sonnet-20241022 is detected"
reset_tmpdir

# ─── Test 8: claude-3-5-haiku-20241022 should fail ──────────────────────
create_test_file "bad8.py" 'model="claude-3-5-haiku-20241022"'
assert_fails "claude-3-5-haiku-20241022 is detected"
reset_tmpdir

# ─── Test 9: claude-3-5-sonnet-v2 should fail ───────────────────────────
create_test_file "bad9.py" 'model="claude-3-5-sonnet-v2"'
assert_fails "claude-3-5-sonnet-v2 is detected"
reset_tmpdir

# ─── Test 10: claude-3-5-opus-latest should fail ────────────────────────
create_test_file "bad10.py" 'model="claude-3-5-opus-latest"'
assert_fails "claude-3-5-opus-latest is detected"
reset_tmpdir

# ─── Test 11: Current model claude-haiku-4-5-20251001 should pass ────────
create_test_file "good1.py" 'model = "claude-haiku-4-5-20251001"'
assert_passes "claude-haiku-4-5-20251001 (current) passes"
reset_tmpdir

# ─── Test 12: Current model claude-sonnet-4-20250514 should pass ─────────
create_test_file "good2.py" 'model = "claude-sonnet-4-20250514"'
assert_passes "claude-sonnet-4-20250514 (current) passes"
reset_tmpdir

# ─── Test 13: Current model claude-opus-4-20250514 should pass ───────────
create_test_file "good3.py" 'model = "claude-opus-4-20250514"'
assert_passes "claude-opus-4-20250514 (current) passes"
reset_tmpdir

# ─── Test 14: Empty directory should pass ─────────────────────────────────
assert_passes "Empty directory passes"

# ─── Test 15: Multiple deprecated models in one file should fail ─────────
create_test_file "multi.py" 'MODELS = [
    "claude-3-haiku-20240307",
    "claude-3-5-sonnet-latest",
]'
assert_fails "Multiple deprecated models in one file are detected"
reset_tmpdir

# ─── Test 16: Deprecated model in .json file should fail ─────────────────
create_test_file "config.json" '{"model": "claude-3-5-haiku-latest"}'
assert_fails "Deprecated model in JSON config is detected"
reset_tmpdir

# ─── Test 17: Deprecated model in .yml file should fail ──────────────────
create_test_file "config.yml" 'model: claude-3-haiku-20240307'
assert_fails "Deprecated model in YAML config is detected"
reset_tmpdir

# ─── Test 18: Partial match should not trigger ───────────────────────────
# "claude-3" alone should not trigger; only full deprecated IDs should match.
create_test_file "partial.py" 'name = "claude-3-custom-thing"'
assert_passes "Partial match (not a real model ID) passes"
reset_tmpdir

# ─── Test 19: .git directory should be excluded ──────────────────────────
mkdir -p "$TMPDIR_TEST/.git/objects"
printf '%s\n' 'claude-3-5-haiku-latest' > "$TMPDIR_TEST/.git/objects/badfile"
assert_passes ".git directory contents are excluded"
reset_tmpdir

# ─── Test 20: node_modules should be excluded ────────────────────────────
mkdir -p "$TMPDIR_TEST/node_modules/some-pkg"
printf '%s\n' 'claude-3-5-haiku-latest' > "$TMPDIR_TEST/node_modules/some-pkg/index.js"
assert_passes "node_modules directory is excluded"
reset_tmpdir

# ─── Test 21: No self-match on ci.yml step name ──────────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain a deprecated model identifier — otherwise the
# guard fails on its first CI run.  See issue #2542 for the class of
# failure (originally observed on PR #2541 with a different guard).
# A tempting misname for this guard would be e.g.:
#
#   - name: Check for deprecated models like claude-3-5-haiku-latest
#
# which would trip the guard on every CI run.  This assertion catches
# the bug at test time instead.  See
# scripts/tests/_guard_self_match_helpers.sh for the shared helper.
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-deprecated-models.sh" "yml"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
