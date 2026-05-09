#!/usr/bin/env bash
# test_check_no_logging_basicconfig.sh — Tests for
# check-no-logging-basicconfig.sh.
#
# Creates temporary `scripts/*.py` fixtures to verify the guard correctly
# detects forbidden `logging.basicConfig(` call sites at line start while
# allowing files carrying the `# basic-config-allow:` marker, comment
# lines, nested subdirectories, and the check script itself.
#
# Usage:
#   scripts/tests/test_check_no_logging_basicconfig.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-logging-basicconfig.sh"
FAILURES=0
TESTS=0

# The guard scans the SCAN_DIR top-level only (no recursion).  We must
# place fixtures DIRECTLY under $TMPDIR_TEST/scripts/ so the
# -maxdepth 1 traversal sees them.
TMPDIR_TEST=$(mktemp -d)
SCRIPTS_DIR="$TMPDIR_TEST/scripts"
mkdir -p "$SCRIPTS_DIR"
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

create_test_file() {
    local name="$1"
    local content="$2"
    local path="$SCRIPTS_DIR/$name"
    local dir
    dir="$(dirname "$path")"
    mkdir -p "$dir"
    printf '%s\n' "$content" > "$path"
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$SCRIPTS_DIR" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$SCRIPTS_DIR" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

reset_tmpdir() {
    rm -rf "$SCRIPTS_DIR"/*
    rm -rf "$SCRIPTS_DIR"/.[!.]* 2>/dev/null || true
}

# Helper to capture the guard's stderr+stdout for content assertions.
run_capture() {
    "$CHECK_SCRIPT" "$SCRIPTS_DIR" 2>&1 || true
}

# ─── Test 1: bare basicConfig at line start should fail ──────────────────
create_test_file "violator.py" 'import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)'
assert_fails "Bare logging.basicConfig at line start is detected"
reset_tmpdir

# ─── Test 2: indented basicConfig (inside if-block / function) should fail
create_test_file "indented.py" 'import logging

if True:
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)'
assert_fails "Indented logging.basicConfig is detected"
reset_tmpdir

# ─── Test 3: multi-line basicConfig still detected via opening paren ─────
create_test_file "multiline.py" 'import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)'
assert_fails "Multi-line logging.basicConfig (opening paren on same line) is detected"
reset_tmpdir

# ─── Test 4: file with allowlist marker should pass ──────────────────────
create_test_file "allowed.py" 'import logging
import sys

# basic-config-allow: #4400 — operator-only CLI shim
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)'
assert_passes "File with # basic-config-allow: marker is exempt"
reset_tmpdir

# ─── Test 5: marker placed elsewhere in file still exempts it ────────────
# The marker is checked anywhere in the file, not just adjacent to the
# call site, so it travels with the file even if the call moves.
create_test_file "marker_elsewhere.py" '"""Module docstring.

# basic-config-allow: #4400 — see file header rationale.
"""
import logging
import sys

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)'
assert_passes "Allowlist marker placed in docstring/header still exempts the file"
reset_tmpdir

# ─── Test 6: canonical configure_structlog pattern should pass ───────────
create_test_file "canonical.py" 'import logging
from framework.logging import configure_structlog

configure_structlog(json=True, stdlib_bridge=True)
logger = logging.getLogger(__name__)'
assert_passes "Canonical configure_structlog pattern is not flagged"
reset_tmpdir

# ─── Test 7: comment mentioning basicConfig as context should pass ───────
create_test_file "comment_only.py" 'import logging

# Previously this script used logging.basicConfig(level=INFO) — see #4368
# for why we migrated to configure_structlog.
from framework.logging import configure_structlog

configure_structlog(json=True, stdlib_bridge=True)
logger = logging.getLogger(__name__)'
assert_passes "Comment-only reference to basicConfig is allowed"
reset_tmpdir

# ─── Test 8: empty directory should pass ─────────────────────────────────
assert_passes "Empty scripts/ directory passes"

# ─── Test 9: non-py file containing the pattern is not flagged ───────────
create_test_file "doc.md" '# History

The old shape was `logging.basicConfig(level=logging.INFO)`.'
assert_passes "Non-.py files are not scanned"
reset_tmpdir

# ─── Test 10: lookalike identifier should pass ───────────────────────────
# `_logging_basicConfig_helper` as a function name does not match — the
# regex anchors `logging.basicConfig(` literally, so prefixed identifiers
# are not flagged.
create_test_file "lookalike.py" 'import logging

def _logging_basicConfig_helper():
    pass

_logging_basicConfig_var = None

logger = logging.getLogger(__name__)'
assert_passes "Lookalike identifiers _logging_basicConfig_* do not trigger"
reset_tmpdir

# ─── Test 11: Multiple violations across files are detected ──────────────
create_test_file "v1.py" 'import logging
logging.basicConfig(level=logging.INFO)'
create_test_file "v2.py" 'import logging
logging.basicConfig(level=logging.DEBUG)'
assert_fails "Multiple violators across files are detected"
reset_tmpdir

# ─── Test 12: Nested subdirectory file is NOT scanned ────────────────────
# The guard scope is top-level scripts/*.py only.  Files in spotcheck/,
# dispatcher/, etc. are intentionally not subject to this check.
mkdir -p "$SCRIPTS_DIR/spotcheck"
printf '%s\n' 'import logging
logging.basicConfig(level=logging.INFO)' > "$SCRIPTS_DIR/spotcheck/nested.py"
assert_passes "Nested subdirectory scripts are not scanned (top-level only)"
reset_tmpdir

# ─── Test 13: Failure output names the offending file path ───────────────
# Re-create a violator and confirm the relative path appears in stderr.
create_test_file "outpath.py" 'import logging
logging.basicConfig(level=logging.INFO)'
TESTS=$((TESTS + 1))
out=$(run_capture)
if echo "$out" | grep -q 'outpath.py:'; then
    echo "PASS: Failure output includes the offending path"
else
    echo "FAIL: Failure output is missing the offending path"
    echo "----- captured output -----"
    echo "$out"
    echo "---------------------------"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 14: Failure output emits a Fix: block referencing both ─────────
# canonical reference files (per docs/dx/check-script-fix-block-coverage.md).
create_test_file "fixblock.py" 'import logging
logging.basicConfig(level=logging.INFO)'
TESTS=$((TESTS + 1))
out=$(run_capture)
fail=0
if ! echo "$out" | grep -q '^  Fix:'; then
    echo "FAIL: Fix block label not emitted"
    fail=1
fi
if ! echo "$out" | grep -q 'drain_splitter_carry_forward_clusters.py'; then
    echo "FAIL: Fix block missing reference to drain_splitter_carry_forward_clusters.py"
    fail=1
fi
if ! echo "$out" | grep -q 'audit_correctly_labeled_s3_orphans.py'; then
    echo "FAIL: Fix block missing reference to audit_correctly_labeled_s3_orphans.py"
    fail=1
fi
if ! echo "$out" | grep -q '# basic-config-allow:'; then
    echo "FAIL: Fix block missing allowlist marker hint"
    fail=1
fi
if [[ $fail -eq 0 ]]; then
    echo "PASS: Failure output emits Fix block with both reference files + marker hint"
else
    echo "----- captured output -----"
    echo "$out"
    echo "---------------------------"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 15: No self-match on ci.yml step name ──────────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain the forbidden pattern.  See #2541/#2542 for the
# class of self-match bug, and the CLAUDE.md §Hygiene-check CI steps
# rule the issue's AC #3 cites.
# shellcheck source=./_guard_self_match_helpers.sh
TMPDIR_TEST_BACKUP="$TMPDIR_TEST"
TMPDIR_TEST="$SCRIPTS_DIR"
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-no-logging-basicconfig.sh" "py"
TMPDIR_TEST="$TMPDIR_TEST_BACKUP"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
