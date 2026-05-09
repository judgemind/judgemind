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

# ─── Test 11: REPO_ROOT only inside a docstring should NOT flag (#4483) ─────
# Module / function / class docstrings that mention _REPO_ROOT or REPO_ROOT
# in prose must not trigger. The pre-#4483 text-grep flagged these as false
# positives; the AST-walk replacement only inspects Name references.
create_test_file "docstring_mention.py" '"""Helper that documents the AST shape it parses.

The path-construction expression is roughly
``_REPO_ROOT / "scripts" / "<name>.py"`` and we walk it with ast.parse.
"""
def main():
    print("hello")
'
assert_passes "REPO_ROOT mention only inside a module docstring does not flag (#4483)"
reset_tmpdir

# ─── Test 12: REPO_ROOT only inside a string literal should NOT flag (#4483)
# Fix-block / error-message output that names REPO_ROOT in a printed
# string is not a real REPO_ROOT use. Only Name references (variable
# load/store) should flag.
create_test_file "string_literal_mention.py" 'def emit_fix_block():
    lines = []
    lines.append("    sys.path.insert(0, str(REPO_ROOT / '\''scripts'\'' / '\''archive'\''))")
    return "\n".join(lines)
'
assert_passes "REPO_ROOT mention only inside a string literal does not flag (#4483)"
reset_tmpdir

# ─── Test 13: Combined docstring + string-literal mentions stay clean (#4483)
# The canonical false-positive pattern from check_test_script_imports_resolvable.py
# (#4464): docstring on line 189 + lines.append() on line 602.
create_test_file "combined_clean.py" '"""Module that mentions ``_REPO_ROOT / "scripts" / "<name>.py"`` in its docstring."""
def emit():
    msg = "REPO_ROOT / scripts / archive"  # noqa: doc-only literal
    lines = []
    lines.append("    sys.path.insert(0, str(REPO_ROOT / '\''scripts'\'' / '\''archive'\''))")
    return msg, lines
'
assert_passes "Combined docstring + string-literal REPO_ROOT mentions do not flag (#4483)"
reset_tmpdir

# ─── Test 14: Real Name reference in code DOES flag even when docstring also mentions it
# Sanity-check that the AST walk does not over-skip — a real
# module-level _REPO_ROOT assignment + Name load still trips the guard.
create_test_file "mixed_real_and_doc.py" '"""Mentions ``_REPO_ROOT / scripts`` in the docstring AND uses it for real."""
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = _REPO_ROOT / "config.json"
'
assert_fails "Real _REPO_ROOT Name reference is flagged even when a docstring also mentions it (#4483)"
reset_tmpdir

# ─── Test 15: AnnAssign target is also a real reference (#4483) ─────────────
# An annotated module-level assignment must still be flagged.
create_test_file "annassign.py" 'from pathlib import Path
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data" / "x.json"
'
assert_fails "AnnAssign target named REPO_ROOT is flagged (#4483)"
reset_tmpdir

# ─── Test 16: Fix block surfaces canonical sys.path fallback pattern (#4559) ─
# When the guard flags a script, the Fix block's option 4 must literally show
# the canonical `if _SF_SRC.is_dir() and str(_SF_SRC) not in sys.path:` pattern,
# so operators don't have to hunt through reference impls to discover it.
TESTS=$((TESTS + 1))
create_test_file "fix_block_probe.py" '_REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = _REPO_ROOT / "x.json"
'
out=$("$CHECK_SCRIPT" --dir "$TMPDIR_TEST" 2>&1 || true)
if printf '%s\n' "$out" | grep -q 'is_dir() and str' \
   && printf '%s\n' "$out" | grep -q 'sys.path.insert(0, str' \
   && printf '%s\n' "$out" | grep -q 'cc-dual-run-diff.py'; then
    echo "PASS: Fix block surfaces canonical sys.path fallback pattern (#4559)"
else
    echo "FAIL: Fix block missing canonical pattern (#4559)"
    echo "      output was:"
    printf '%s\n' "$out" | sed 's/^/        /'
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Summary ────────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
