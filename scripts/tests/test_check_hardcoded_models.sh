#!/usr/bin/env bash
# test_check_hardcoded_models.sh — Tests for check-hardcoded-models.sh
#
# Creates temporary files to verify that the checker correctly detects
# hardcoded Claude model identifiers while allowing legitimate uses
# (comments, docstrings, test files, the source-of-truth file).
#
# Usage:
#   scripts/tests/test_check_hardcoded_models.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-hardcoded-models.sh"
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
    local dir
    dir="$(dirname "$TMPDIR_TEST/$name")"
    mkdir -p "$dir"
    printf '%s\n' "$content" > "$TMPDIR_TEST/$name"
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

# ─── Test 1: Hardcoded Haiku model ID should fail ─────────────────────
create_test_file "bad1.py" 'MODEL = "claude-haiku-4-5-20251001"'
assert_fails "Hardcoded claude-haiku-4-5-20251001 is detected"
reset_tmpdir

# ─── Test 2: Hardcoded Sonnet model ID should fail ────────────────────
create_test_file "bad2.py" 'MODEL = "claude-sonnet-4-20250514"'
assert_fails "Hardcoded claude-sonnet-4-20250514 is detected"
reset_tmpdir

# ─── Test 3: Hardcoded Opus model ID should fail ──────────────────────
create_test_file "bad3.py" 'OPUS_MODEL = "claude-opus-4-20250514"'
assert_fails "Hardcoded claude-opus-4-20250514 is detected"
reset_tmpdir

# ─── Test 4: Undated model alias should fail ───────────────────────────
create_test_file "bad4.py" 'model = "claude-sonnet-4-6"'
assert_fails "Undated alias claude-sonnet-4-6 is detected"
reset_tmpdir

# ─── Test 5: Python comment line should pass ───────────────────────────
create_test_file "good_comment.py" '# Default model is claude-haiku-4-5-20251001'
assert_passes "Python comment line is allowed"
reset_tmpdir

# ─── Test 6: Docstring example with backticks should pass ──────────────
create_test_file "good_docstring.py" '        model: Anthropic model ID (e.g. ``"claude-haiku-4-5-20251001"``).'
assert_passes "Docstring with backtick example is allowed"
reset_tmpdir

# ─── Test 7: SQL comment should pass ──────────────────────────────────
create_test_file "good_sql.sql" "    summary_model           TEXT,           -- e.g., 'claude-haiku-4-5'"
assert_passes "SQL comment is allowed"
reset_tmpdir

# ─── Test 8: Test directory files should pass ──────────────────────────
create_test_file "tests/test_something.py" 'model = "claude-haiku-4-5-20251001"'
assert_passes "Files in tests/ directory are allowed"
reset_tmpdir

# ─── Test 9: Empty directory should pass ───────────────────────────────
assert_passes "Empty directory passes"
reset_tmpdir

# ─── Test 10: Multiple hardcoded models in one file should fail ────────
create_test_file "multi.py" 'MODELS = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-20250514",
]'
assert_fails "Multiple hardcoded models in one file are detected"
reset_tmpdir

# ─── Test 11: Import from judgemind_config should pass ─────────────────
create_test_file "good_import.py" 'from judgemind_config import DEFAULT_HAIKU_MODEL
model = DEFAULT_HAIKU_MODEL'
assert_passes "Import from judgemind_config passes (no hardcoded ID)"
reset_tmpdir

# ─── Test 12: Docstring with e.g. should pass ─────────────────────────
create_test_file "good_eg.py" '    """Use a model like e.g. claude-haiku-4-5-20251001."""'
assert_passes "Docstring with e.g. mention is allowed"
reset_tmpdir

# ─── Test 13: eval/ directory should pass ──────────────────────────────
create_test_file "scripts/eval/eval_thing.py" 'MODEL = "claude-haiku-4-5-20251001"'
assert_passes "Files in scripts/eval/ directory are allowed"
reset_tmpdir

# ─── Test 14: .git directory should be excluded ────────────────────────
mkdir -p "$TMPDIR_TEST/.git/objects"
printf '%s\n' 'claude-haiku-4-5-20251001' > "$TMPDIR_TEST/.git/objects/badfile"
assert_passes ".git directory contents are excluded"
reset_tmpdir

# ─── Test 15: node_modules should be excluded ──────────────────────────
mkdir -p "$TMPDIR_TEST/node_modules/some-pkg"
printf '%s\n' 'model = "claude-haiku-4-5-20251001"' > "$TMPDIR_TEST/node_modules/some-pkg/index.js"
assert_passes "node_modules directory is excluded"
reset_tmpdir

# ─── Test 16: Prose mention without version should pass ────────────────
create_test_file "good_prose.py" 'description = "Uses Claude Haiku for classification"'
assert_passes "Prose mention of Claude Haiku (no model ID) passes"
reset_tmpdir

# ─── Summary ───────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
