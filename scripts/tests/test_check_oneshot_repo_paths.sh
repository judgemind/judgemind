#!/usr/bin/env bash
# test_check_oneshot_repo_paths.sh — Tests for check-oneshot-repo-paths.sh
#
# Creates temporary files to verify that the checker correctly detects
# scripts referencing REPO_ROOT without validated fallback mechanisms.
#
# Usage:
#   scripts/tests/test_check_oneshot_repo_paths.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-oneshot-repo-paths.sh"
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
    if "$CHECK_SCRIPT" --dir "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" --dir "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
}

# ─── Test 1: _REPO_ROOT reference should fail ───────────────────────────────
create_test_file "bad_script.py" '_REPO_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = _REPO_ROOT / "config.json"'
assert_fails "_REPO_ROOT reference without validated fallback is flagged"
reset_tmpdir

# ─── Test 2: REPO_ROOT (no underscore) reference should fail ────────────────
create_test_file "bad_script2.py" 'REPO_ROOT = os.path.dirname(SCRIPTS_DIR)
CONFIG = os.path.join(REPO_ROOT, "data.json")'
assert_fails "REPO_ROOT (no underscore) reference is flagged"
reset_tmpdir

# ─── Test 3: Empty directory should pass ─────────────────────────────────────
assert_passes "Empty directory passes"

# ─── Test 4: Script without REPO_ROOT should pass ───────────────────────────
create_test_file "clean_script.py" 'import os
import sys
def main():
    print("hello")
'
assert_passes "Script without REPO_ROOT passes"
reset_tmpdir

# ─── Test 5: LOCAL_ONLY script should be skipped ────────────────────────────
# validate-dq-baselines.py is in LOCAL_ONLY
create_test_file "validate-dq-baselines.py" '_REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_PATH = _REPO_ROOT / "baselines.json"'
assert_passes "LOCAL_ONLY script (validate-dq-baselines.py) is skipped"
reset_tmpdir

# ─── Test 6: VALIDATED script should be skipped ─────────────────────────────
# data-quality-check.py is in VALIDATED
create_test_file "data-quality-check.py" '_REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_BASELINES_PATH = _REPO_ROOT / "data-quality-baselines.json"'
assert_passes "VALIDATED script (data-quality-check.py) is skipped"
reset_tmpdir

# ─── Test 7: Non-.py files should be ignored ────────────────────────────────
create_test_file "script.sh" 'REPO_ROOT=$(dirname "$0")/..'
assert_passes "Non-.py files are ignored"
reset_tmpdir

# ─── Test 8: REPO_ROOT in a comment should still flag ───────────────────────
# We intentionally flag comments too — if someone defines _REPO_ROOT in
# a comment, it may indicate copy-paste from a real usage.
create_test_file "commented.py" '# _REPO_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = _REPO_ROOT / "config.json"'
assert_fails "REPO_ROOT usage even with comment-like definition is flagged"
reset_tmpdir

# ─── Test 9: Multiple violations in directory ───────────────────────────────
create_test_file "bad1.py" '_REPO_ROOT = Path(__file__).parent.parent
F = _REPO_ROOT / "x.json"'
create_test_file "bad2.py" 'REPO_ROOT = os.path.dirname(__file__)
G = os.path.join(REPO_ROOT, "y.json")'
create_test_file "clean.py" 'import sys
print("ok")'
assert_fails "Multiple violations in one directory are flagged"
reset_tmpdir

# ─── Test 10: repo_root as function param should NOT flag ────────────────────
# Only references to module-level REPO_ROOT variables should flag.
# A function parameter named repo_root is fine.
create_test_file "func_param.py" 'def summarize(worktree, repo_root, issue_number):
    path = Path(repo_root) / "tmp"
    return path'
assert_passes "Function parameter named repo_root does not flag"
reset_tmpdir

# ─── Summary ────────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
