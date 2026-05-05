#!/usr/bin/env bash
# test_check_no_redos_pattern.sh — Tests for check-no-redos-pattern.sh.
#
# Creates temporary Python files exercising both pass and fail cases
# for the ReDoS-shaped re.compile detector (issue #4117).
#
# Usage:
#   scripts/tests/test_check_no_redos_pattern.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-redos-pattern.sh"
FAILURES=0
TESTS=0

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
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

# ─── Test 1: The exact #4104 anti-pattern is caught ──────────────────────
create_test_file "redos_bad.py" 'import re

BAD = re.compile(
    r"([^\n]+?)\s+Judge of the Superior Court",
    re.IGNORECASE,
)
'
assert_fails "Exact #4104 anti-pattern (lazy [^\\n]+? + IGNORECASE) is caught"
reset_tmpdir

# ─── Test 2: One-line bad form is caught ─────────────────────────────────
create_test_file "redos_oneline.py" 'import re
BAD = re.compile(r"([^\n]+?)\s+foo", re.IGNORECASE)
'
assert_fails "One-line lazy-leading pattern with IGNORECASE is caught"
reset_tmpdir

# ─── Test 3: Anchored ^ + MULTILINE is the post-fix shape (passes) ───────
create_test_file "redos_anchored.py" 'import re

GOOD = re.compile(
    r"^([^\n]{1,80}?)\s+Judge of the Superior Court",
    re.IGNORECASE | re.MULTILINE,
)
'
assert_passes "Anchored ^ + bounded {1,80}? pattern passes (post-#4104 fix shape)"
reset_tmpdir

# ─── Test 4: Word-boundary leading anchor passes ─────────────────────────
create_test_file "redos_wordboundary.py" 'import re
GOOD = re.compile(r"\bmotion\s+to\s+dismiss\b", re.IGNORECASE)
'
assert_passes "\\b word-boundary leading anchor passes"
reset_tmpdir

# ─── Test 5: Literal-leading pattern passes ──────────────────────────────
create_test_file "redos_literal.py" 'import re
GOOD = re.compile(r"Department\s+\S+?\s*-\s*Judge\s+(?P<n>[^\n]+)", re.IGNORECASE)
'
assert_passes "Literal-leading 'Department' before lazy quantifier passes"
reset_tmpdir

# ─── Test 6: No IGNORECASE — case-sensitive — is exempt ──────────────────
create_test_file "redos_no_ignorecase.py" 'import re
NEUTRAL = re.compile(r"([^\n]+?)\s+foo")
'
assert_passes "Same shape WITHOUT IGNORECASE is not flagged (case-sensitive ok)"
reset_tmpdir

# ─── Test 7: re.I (the alias) is treated like re.IGNORECASE ──────────────
create_test_file "redos_re_i.py" 'import re
BAD = re.compile(r"([^\n]+?)\s+foo", re.I)
'
assert_fails "re.I alias (short form of IGNORECASE) is caught"
reset_tmpdir

# ─── Test 8: re.IGNORECASE | re.MULTILINE BinOp is detected ──────────────
create_test_file "redos_or_flags.py" 'import re
BAD = re.compile(r"([^\n]+?)\s+foo", re.IGNORECASE | re.DOTALL)
'
assert_fails "re.IGNORECASE | re.DOTALL flag chain is detected"
reset_tmpdir

# ─── Test 9: # noqa: redos-pattern suppression on opening line ───────────
create_test_file "redos_suppress.py" 'import re
# noqa for known-safe (test fixture)
BAD = re.compile(r"([^\n]+?)\s+foo", re.IGNORECASE)  # noqa: redos-pattern
'
assert_passes "# noqa: redos-pattern suppression on opening line skips the check"
reset_tmpdir

# ─── Test 10: # noqa: redos-pattern on multi-line call ───────────────────
create_test_file "redos_suppress_multiline.py" 'import re

BAD = re.compile(  # noqa: redos-pattern
    r"([^\n]+?)\s+foo",
    re.IGNORECASE,
)
'
assert_passes "# noqa: redos-pattern on multi-line call opening line skips"
reset_tmpdir

# ─── Test 11: .*? leading wildcard is also caught ────────────────────────
create_test_file "redos_star_lazy.py" 'import re
BAD = re.compile(r"(.*?)foo", re.IGNORECASE)
'
assert_fails "Leading .*? lazy quantifier is caught"
reset_tmpdir

# ─── Test 12: \\S+? leading wildcard is caught ───────────────────────────
create_test_file "redos_S_lazy.py" 'import re
BAD = re.compile(r"\S+?\s+foo", re.IGNORECASE)
'
assert_fails "Leading \\S+? lazy quantifier is caught"
reset_tmpdir

# ─── Test 13: keyword flags=re.IGNORECASE form is detected ───────────────
create_test_file "redos_kwarg.py" 'import re
BAD = re.compile(r"([^\n]+?)\s+foo", flags=re.IGNORECASE)
'
assert_fails "flags=re.IGNORECASE keyword form is detected"
reset_tmpdir

# ─── Test 14: Non-capturing group wrapper does not hide the bad shape ────
create_test_file "redos_noncap.py" 'import re
BAD = re.compile(r"(?:[^\n]+?)\s+foo", re.IGNORECASE)
'
assert_fails "Non-capturing group wrapper (?:...) does not mask the lazy head"
reset_tmpdir

# ─── Test 15: Named-capture group wrapper does not hide the bad shape ────
create_test_file "redos_named.py" 'import re
BAD = re.compile(r"(?P<name>[^\n]+?)\s+foo", re.IGNORECASE)
'
assert_fails "Named-capture (?P<name>...) wrapper does not mask the lazy head"
reset_tmpdir

# ─── Test 16: Alternation with literal alternatives passes ───────────────
# `(foo|bar).+?baz` starts with literal alternatives so the lazy
# quantifier is anchored.  This is borderline but acceptable for a
# heuristic — the literal alternatives prevent unbounded backtracking
# from the start.
create_test_file "redos_alternation.py" 'import re
GOOD = re.compile(r"(foo|bar).+?baz", re.IGNORECASE)
'
assert_passes "Literal alternation (foo|bar) before lazy quantifier passes"
reset_tmpdir

# ─── Test 17: Bounded {1,80}? quantifier passes ──────────────────────────
# A lazy {min,max}? is still lazy — but a small max bounds the work to
# O(n) regardless.  Still, the heuristic flags ANY leading lazy
# wildcard without a literal anchor.  When the pattern starts with `^`
# the bounded form is the recommended fix.
create_test_file "redos_bounded.py" 'import re
GOOD = re.compile(r"^[^\n]{1,80}?\s+foo", re.IGNORECASE | re.MULTILINE)
'
assert_passes "^-anchored bounded {1,80}? lazy passes (recommended fix)"
reset_tmpdir

# ─── Test 18: Empty / no-match files pass ────────────────────────────────
create_test_file "redos_empty.py" 'pass
'
assert_passes "Python file with no re.compile calls passes"
reset_tmpdir

# ─── Test 19: Non-Python file is ignored ─────────────────────────────────
create_test_file "redos_not_python.txt" 're.compile(r"([^\n]+?)\s+foo", re.IGNORECASE)
'
assert_passes "Non-.py file is not scanned"
reset_tmpdir

# ─── Test 20: Syntactically broken Python is skipped silently ────────────
create_test_file "redos_broken.py" 'import re
def broken(:  # syntax error
    pass
'
assert_passes "Syntactically broken Python is skipped silently"
reset_tmpdir

# ─── Test 21: f-string pattern (dynamic) is skipped ──────────────────────
# We cannot statically know the value of an f-string pattern, so the
# scanner skips it.  False negatives are acceptable per the heuristic.
create_test_file "redos_fstring.py" 'import re
suffix = "foo"
BAD = re.compile(f"([^\n]+?)\s+{suffix}", re.IGNORECASE)
'
assert_passes "f-string pattern is skipped (dynamic — heuristic accepts FN)"
reset_tmpdir

# ─── Test 22: Variable pattern (dynamic) is skipped ──────────────────────
create_test_file "redos_var.py" 'import re
PATTERN = r"([^\n]+?)\s+foo"
BAD = re.compile(PATTERN, re.IGNORECASE)
'
assert_passes "Variable pattern is skipped (dynamic — heuristic accepts FN)"
reset_tmpdir

# ─── Test 23: scripts/archive/ subdir is excluded ────────────────────────
# Archived one-off scripts (already run, kept for posterity) are out of
# scope — the bug class only matters for active code paths.
mkdir -p "$TMPDIR_TEST/scripts/archive"
printf 'import re\nBAD = re.compile(r"([^\\n]+?)\\s+foo", re.IGNORECASE)\n' \
    > "$TMPDIR_TEST/scripts/archive/old_backfill.py"
assert_passes "scripts/archive/ subdir is excluded from the scan"
reset_tmpdir

# ─── Test 24: .venv/ subdir is excluded ──────────────────────────────────
mkdir -p "$TMPDIR_TEST/.venv/lib/python3.12/site-packages"
printf 'import re\nBAD = re.compile(r"([^\\n]+?)\\s+foo", re.IGNORECASE)\n' \
    > "$TMPDIR_TEST/.venv/lib/python3.12/site-packages/vendored.py"
assert_passes ".venv/ subdir is excluded from the scan"
reset_tmpdir

# ─── Test 25: Multiple violations across files are all reported ──────────
# Two files, each with one violation — the wrapper script must flag
# the whole run as failed.
create_test_file "v1.py" 'import re
A = re.compile(r"([^\n]+?)\s+foo", re.IGNORECASE)
'
create_test_file "v2.py" 'import re
B = re.compile(r"(.*?)bar", re.IGNORECASE)
'
assert_fails "Multiple violations across multiple files are all reported"
reset_tmpdir

# ─── Test 26: Direct file-path argument works ────────────────────────────
# The wrapper accepts a file path directly (used by some CI configs).
TESTS=$((TESTS + 1))
single_file="$TMPDIR_TEST/single.py"
mkdir -p "$TMPDIR_TEST"
printf 'import re\nBAD = re.compile(r"([^\\n]+?)\\s+foo", re.IGNORECASE)\n' \
    > "$single_file"
if "$CHECK_SCRIPT" "$single_file" > /dev/null 2>&1; then
    echo "FAIL: Direct .py file path argument detects violations (expected failure, got success)"
    FAILURES=$((FAILURES + 1))
else
    echo "PASS: Direct .py file path argument detects violations"
fi
reset_tmpdir

# ─── Test 27: No self-match on ci.yml step name ──────────────────────────
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-no-redos-pattern.sh" "yml"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
