#!/usr/bin/env bash
# test_check_no_duplicate_parent_regex.sh — Tests for
# check-no-duplicate-parent-regex.sh.
#
# Creates synthetic scripts/ + packages/ trees with the canonical home
# (scripts/dispatcher/parent_issue.py) plus various violator and
# allowlist fixtures, then verifies the guard:
#
#   - exits 0 against a clean tree (regex only in canonical home);
#   - exits 1 when a non-canonical file contains the regex fragment;
#   - honors the # allow-duplicate-parent-regex: marker;
#   - emits a copy-pasteable Fix: block per the
#     docs/dx/check-script-fix-block-coverage.md contract;
#   - does not self-match on its own ci.yml step name.
#
# Usage:
#   scripts/tests/test_check_no_duplicate_parent_regex.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-duplicate-parent-regex.sh"
FAILURES=0
TESTS=0

# Fixtures live under TMPDIR_TEST/scripts/ and TMPDIR_TEST/packages/ to
# match the production layout the guard expects.
TMPDIR_TEST=$(mktemp -d)
SCRIPTS_DIR="$TMPDIR_TEST/scripts"
PACKAGES_DIR="$TMPDIR_TEST/packages"
CANONICAL_DIR="$SCRIPTS_DIR/dispatcher"
mkdir -p "$CANONICAL_DIR" "$PACKAGES_DIR"

cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# Plant the canonical home file. Every test scenario keeps this file
# in place because it's the only legal occurrence of the regex.
write_canonical_home() {
    cat > "$CANONICAL_DIR/parent_issue.py" <<'PY_EOF'
"""Canonical home for the Parent: #N regex (#4508)."""

import re

PARENT_RE = re.compile(r"(?im)^\s*parent\s*:\s*#(\d+)\s*$")


def parse_parent_issue(body):
    if not body:
        return None
    match = PARENT_RE.search(body)
    return int(match.group(1)) if match else None
PY_EOF
}

write_canonical_home

# Helper: create a fixture file under scripts/ (or packages/).
create_test_file() {
    local relpath="$1"
    local content="$2"
    local path="$TMPDIR_TEST/$relpath"
    local dir
    dir="$(dirname "$path")"
    mkdir -p "$dir"
    printf '%s\n' "$content" > "$path"
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
    rm -rf "${SCRIPTS_DIR:?}"/*
    rm -rf "${PACKAGES_DIR:?}"/*
    mkdir -p "$CANONICAL_DIR"
    write_canonical_home
}

run_capture() {
    "$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true
}

# ─── Test 1: clean tree (only canonical home has regex) → exit 0 ─────
assert_passes "Clean tree (regex only in canonical home) exits 0"
reset_tmpdir

# ─── Test 2: violator in scripts/ root → exit 1 ──────────────────────
create_test_file "scripts/violator.py" 'import re

_PARENT_RE = re.compile(r"(?im)^\s*parent\s*:\s*#(\d+)\s*$")


def parse(body):
    return None
'
assert_fails "scripts/violator.py with regex copy fails"
reset_tmpdir

# ─── Test 3: violator in scripts/dispatcher/ (non-canonical) → exit 1 ─
create_test_file "scripts/dispatcher/other.py" 'import re

match = re.search(r"(?im)^\s*parent\s*:\s*#(\d+)\s*$", "")
'
assert_fails "scripts/dispatcher/<other>.py with regex copy fails"
reset_tmpdir

# ─── Test 4: violator in scripts/.sh file → exit 1 ───────────────────
create_test_file "scripts/violator.sh" '#!/usr/bin/env bash
# Inline grep using the parent regex.
grep -E "parent\s*:\s*#" "$1"
'
assert_fails "scripts/violator.sh with regex copy fails"
reset_tmpdir

# ─── Test 5: violator in packages/ → exit 1 ──────────────────────────
create_test_file "packages/some-pkg/src/parent.py" 'import re

match = re.search(r"parent\s*:\s*#(\d+)", "Parent: #42")
'
assert_fails "packages/<pkg>/src/parent.py with regex copy fails"
reset_tmpdir

# ─── Test 6: allowlist marker exempts the file ───────────────────────
create_test_file "scripts/allowed.py" '#!/usr/bin/env python3
# allow-duplicate-parent-regex: #4508 — kept for legacy reasons.
import re

_PARENT_RE = re.compile(r"(?im)^\s*parent\s*:\s*#(\d+)\s*$")
'
assert_passes "File with # allow-duplicate-parent-regex: marker is exempt"
reset_tmpdir

# ─── Test 7: marker placed in docstring still exempts ────────────────
create_test_file "scripts/marker_in_docstring.py" '"""Module.

allow-duplicate-parent-regex: #4508 — see header rationale.
"""
import re

_PARENT_RE = re.compile(r"(?im)^\s*parent\s*:\s*#(\d+)\s*$")
'
assert_passes "Allowlist marker in docstring still exempts the file"
reset_tmpdir

# ─── Test 8: failure output names the offending file ─────────────────
create_test_file "scripts/named.py" 'import re

_PARENT_RE = re.compile(r"(?im)^\s*parent\s*:\s*#(\d+)\s*$")
'
TESTS=$((TESTS + 1))
out=$(run_capture)
if echo "$out" | grep -q 'scripts/named.py'; then
    echo "PASS: Failure output names the offending file"
else
    echo "FAIL: Failure output does not name the offending file"
    echo "----- captured output -----"
    echo "$out"
    echo "---------------------------"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 9: failure output emits a Fix: block ───────────────────────
# Per docs/dx/check-script-fix-block-coverage.md (#4346), the guard
# must emit a copy-pasteable `Fix:` block. The block must reference the
# canonical helper module, the import line, and the allowlist marker.
create_test_file "scripts/fixblock.py" 'import re

_PARENT_RE = re.compile(r"(?im)^\s*parent\s*:\s*#(\d+)\s*$")
'
TESTS=$((TESTS + 1))
out=$(run_capture)
fail=0
if ! echo "$out" | grep -q '^  Fix:'; then
    echo "FAIL: Fix block label not emitted"
    fail=1
fi
if ! echo "$out" | grep -q 'from dispatcher.parent_issue import parse_parent_issue'; then
    echo "FAIL: Fix block missing canonical import line"
    fail=1
fi
if ! echo "$out" | grep -q 'scripts/dispatcher/daemon.py'; then
    echo "FAIL: Fix block missing daemon.py reference"
    fail=1
fi
if ! echo "$out" | grep -q 'scripts/_sweep_completed_parents.py'; then
    echo "FAIL: Fix block missing _sweep_completed_parents.py reference"
    fail=1
fi
if ! echo "$out" | grep -q 'scripts/dispatcher/agent-runner-entrypoint.sh'; then
    echo "FAIL: Fix block missing agent-runner-entrypoint.sh reference"
    fail=1
fi
if ! echo "$out" | grep -q '# allow-duplicate-parent-regex:'; then
    echo "FAIL: Fix block missing allowlist marker hint"
    fail=1
fi
if [[ $fail -eq 0 ]]; then
    echo "PASS: Failure output emits Fix block with import line, all 3 reference sites, and marker hint"
else
    echo "----- captured output -----"
    echo "$out"
    echo "---------------------------"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 10: canonical home does NOT trigger ────────────────────────
# Sanity: the canonical home file is the only legal copy. Empty
# scripts/ tree (just the canonical home) must pass.
assert_passes "Canonical home alone does not trigger"
reset_tmpdir

# ─── Test 11: multiple violations across multiple files ──────────────
create_test_file "scripts/v1.py" 'import re; re.search(r"parent\s*:\s*#", "")'
create_test_file "scripts/v2.sh" 'grep -E "parent\s*:\s*#" file'
TESTS=$((TESTS + 1))
out=$(run_capture)
if echo "$out" | grep -q 'scripts/v1.py' && echo "$out" | grep -q 'scripts/v2.sh'; then
    echo "PASS: Multiple violators across multiple files all reported"
else
    echo "FAIL: Not all violators reported"
    echo "----- captured output -----"
    echo "$out"
    echo "---------------------------"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 12: missing scan root → exit 2 ──────────────────────────────
TESTS=$((TESTS + 1))
"$CHECK_SCRIPT" "/this/path/does/not/exist" > /dev/null 2>&1
rc=$?
if [[ $rc -eq 2 ]]; then
    echo "PASS: Missing scan root exits 2 (usage error)"
else
    echo "FAIL: Missing scan root should exit 2, got $rc"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 13: real tree (production scan) → exit 0 ───────────────────
# Run the guard against the actual repo. This is the live-tree probe
# the Fix-block contract (#4346 §5) requires — confirms the guard
# doesn't false-positive on legitimate code shapes in the current tree.
TESTS=$((TESTS + 1))
real_repo_root="$(cd "$SCRIPT_DIR/.." && pwd)"
if "$CHECK_SCRIPT" "$real_repo_root" > /dev/null 2>&1; then
    echo "PASS: Real-tree probe — current scripts/ + packages/ scans clean"
else
    echo "FAIL: Real-tree probe failed — guard fires against the live tree"
    out=$("$CHECK_SCRIPT" "$real_repo_root" 2>&1 || true)
    echo "----- captured output -----"
    echo "$out"
    echo "---------------------------"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 14: No self-match on ci.yml step name ──────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain the forbidden patterns. See #2541/#2542 for the
# class of self-match bug.
# shellcheck source=./_guard_self_match_helpers.sh
TMPDIR_TEST_BACKUP="$TMPDIR_TEST"
TMPDIR_TEST="$SCRIPTS_DIR"
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-no-duplicate-parent-regex.sh" "sh"
TMPDIR_TEST="$TMPDIR_TEST_BACKUP"

# ─── Summary ──────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
