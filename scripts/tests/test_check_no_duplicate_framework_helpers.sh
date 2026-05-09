#!/usr/bin/env bash
# test_check_no_duplicate_framework_helpers.sh — Tests for
# check-no-duplicate-framework-helpers.sh.
#
# Creates temporary scripts/*.py fixtures and a synthetic framework
# module with a known public API to verify the guard correctly:
#
#   - exits 0 against a clean tree (no scripts duplicate any framework name);
#   - exits 1 when a scripts/*.py file defines a name exported by the
#     synthetic framework module;
#   - honors the # allow-duplicate-framework-helpers: marker;
#   - skips scripts/archive/ and scripts/tests/ subtrees;
#   - emits a copy-pasteable Fix: block per the
#     docs/dx/check-script-fix-block-coverage.md contract;
#   - falls back to non-underscore names when __all__ is absent;
#   - respects __all__ when present;
#   - does NOT count IMPORTED names as part of the public API;
#   - does not self-match on its own ci.yml step name.
#
# Usage:
#   scripts/tests/test_check_no_duplicate_framework_helpers.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-duplicate-framework-helpers.sh"
FAILURES=0
TESTS=0

# Fixtures live under TMPDIR_TEST/scripts/, mirroring the production
# layout the guard expects (scripts/*.py + scripts/archive/ + scripts/tests/).
TMPDIR_TEST=$(mktemp -d)
SCRIPTS_DIR="$TMPDIR_TEST/scripts"
FRAMEWORK_DIR="$TMPDIR_TEST/framework"
SYNTHETIC_FRAMEWORK="$FRAMEWORK_DIR/s3_keys.py"
mkdir -p "$SCRIPTS_DIR" "$FRAMEWORK_DIR"
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# Default synthetic framework module — five public names mirroring the
# real packages/scraper-framework/src/framework/s3_keys.py shape.
write_default_framework() {
    cat > "$SYNTHETIC_FRAMEWORK" <<'PY_EOF'
"""Synthetic framework module for guard testing."""

import re

KEY_PATTERN = re.compile(r"^[a-z]+/[0-9a-f]{64}$")


def parse_flat_hash_key(key):
    return None


def is_mislabel(filename_hash, metadata_hash):
    return False


def head_object_metadata_hash(s3_client, bucket, key):
    return None


def build_twin_key(mislabel_key, metadata_hash):
    return None
PY_EOF
}

write_default_framework

# Helper to write a scripts/* fixture file.
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
    if FRAMEWORK_S3_KEYS_PATH="$SYNTHETIC_FRAMEWORK" "$CHECK_SCRIPT" "$SCRIPTS_DIR" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if FRAMEWORK_S3_KEYS_PATH="$SYNTHETIC_FRAMEWORK" "$CHECK_SCRIPT" "$SCRIPTS_DIR" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

reset_tmpdir() {
    rm -rf "$SCRIPTS_DIR"/*
    rm -rf "$SCRIPTS_DIR"/.[!.]* 2>/dev/null || true
    write_default_framework
}

# Helper to capture the guard's combined stderr+stdout for content
# assertions. Always uses the synthetic framework path so the captured
# output matches the assert_* helpers.
run_capture() {
    FRAMEWORK_S3_KEYS_PATH="$SYNTHETIC_FRAMEWORK" \
        "$CHECK_SCRIPT" "$SCRIPTS_DIR" 2>&1 || true
}

# ─── Test 1: clean tree (no violations) → exit 0 ─────────────────────
create_test_file "clean.py" 'from framework.s3_keys import (
    KEY_PATTERN,
    parse_flat_hash_key,
    is_mislabel,
    head_object_metadata_hash,
    build_twin_key,
)


def main():
    return None
'
assert_passes "Clean tree (importing from framework) exits 0"
reset_tmpdir

# ─── Test 2: synthetic violation (def) → exit 1 ──────────────────────
create_test_file "violator_def.py" 'import re


def parse_flat_hash_key(key):
    return None


def main():
    return None
'
assert_fails "def re-defining a public framework name fails"
reset_tmpdir

# ─── Test 3: synthetic violation (constant) → exit 1 ─────────────────
create_test_file "violator_const.py" 'import re

KEY_PATTERN = re.compile(r"^[a-z]+/[0-9a-f]{64}$")


def main():
    return None
'
assert_fails "module-level constant re-defining a public framework name fails"
reset_tmpdir

# ─── Test 4: synthetic violation (annotated assignment) → exit 1 ─────
create_test_file "violator_annotated.py" 'KEY_PATTERN: object = object()


def main():
    return None
'
assert_fails "annotated assignment re-defining a public framework name fails"
reset_tmpdir

# ─── Test 5: file with allowlist marker should pass ──────────────────
create_test_file "allowed.py" '#!/usr/bin/env python3
# allow-duplicate-framework-helpers: #4456 — local KEY_PATTERN is the
# pre-#4447 broad shape; intentional.
import re

KEY_PATTERN = re.compile(r"^.*$")


def parse_flat_hash_key(key):
    return None
'
assert_passes "File with # allow-duplicate-framework-helpers: marker is exempt"
reset_tmpdir

# ─── Test 6: marker placed elsewhere in file still exempts it ────────
create_test_file "marker_elsewhere.py" '"""Module docstring.

# allow-duplicate-framework-helpers: #4456 — see file header rationale.
"""

import re

KEY_PATTERN = re.compile(r"^.*$")
'
assert_passes "Allowlist marker placed in docstring/header still exempts the file"
reset_tmpdir

# ─── Test 7: scripts/archive/ subdirectory is NOT scanned ────────────
mkdir -p "$SCRIPTS_DIR/archive"
printf '%s\n' 'import re

KEY_PATTERN = re.compile(r"^.*$")


def parse_flat_hash_key(key):
    return None
' > "$SCRIPTS_DIR/archive/legacy.py"
assert_passes "scripts/archive/ subtree is excluded from the scan"
reset_tmpdir

# ─── Test 8: scripts/tests/ subdirectory is NOT scanned ──────────────
mkdir -p "$SCRIPTS_DIR/tests"
printf '%s\n' 'def parse_flat_hash_key(key):
    return None


def is_mislabel(a, b):
    return False
' > "$SCRIPTS_DIR/tests/test_thing.py"
assert_passes "scripts/tests/ subtree is excluded from the scan"
reset_tmpdir

# ─── Test 9: empty scripts directory passes ──────────────────────────
assert_passes "Empty scripts/ directory passes"
reset_tmpdir

# ─── Test 10: imported name does NOT count as a definition ───────────
# A file that imports parse_flat_hash_key from framework.s3_keys must
# NOT be flagged — the import is the canonical fix, not the bug.
create_test_file "imports_only.py" 'from framework.s3_keys import parse_flat_hash_key


def caller():
    return parse_flat_hash_key("x")
'
assert_passes "Pure import of a public framework name does not trigger"
reset_tmpdir

# ─── Test 11: nested function (NOT module-scope) does NOT trigger ────
# A function defined inside another function is not a top-level
# definition and therefore is not part of the script's public API.
create_test_file "nested_def.py" 'def outer():
    def parse_flat_hash_key(key):
        return None
    return parse_flat_hash_key("x")
'
assert_passes "Function nested inside another function does not trigger"
reset_tmpdir

# ─── Test 12: class methods do NOT trigger ───────────────────────────
# Methods on a class are not module-scope definitions and are out of scope.
create_test_file "class_method.py" 'class Helper:
    def is_mislabel(self, a, b):
        return False
'
assert_passes "Class methods do not trigger (only module-scope defs)"
reset_tmpdir

# ─── Test 13: __all__-honoring framework module ──────────────────────
# When the framework module declares __all__, the guard must use the
# __all__ list — names defined in the module but absent from __all__
# (or names listed in __all__ but not defined) are out of scope.
cat > "$SYNTHETIC_FRAMEWORK" <<'PY_EOF'
"""Synthetic framework module with explicit __all__."""

__all__ = ["only_exported"]


def only_exported():
    return None


def _internal():
    return None


def also_defined_but_not_exported():
    return None
PY_EOF
create_test_file "uses_internal.py" 'def also_defined_but_not_exported():
    return None
'
assert_passes "Names defined in framework but absent from __all__ are not flagged"
reset_tmpdir

# Restore the default framework for subsequent tests.
write_default_framework

# ─── Test 14: __all__-honoring violation ─────────────────────────────
cat > "$SYNTHETIC_FRAMEWORK" <<'PY_EOF'
"""Synthetic framework module with explicit __all__."""

__all__ = ("only_exported",)


def only_exported():
    return None
PY_EOF
create_test_file "violator_explicit.py" 'def only_exported():
    return None
'
assert_fails "Re-defining a name listed in __all__ fails"
reset_tmpdir
write_default_framework

# ─── Test 15: framework imports are NOT counted as public API ────────
# The framework module imports `re` at the top — `re` must NOT be
# counted as part of its public API. A scripts/*.py file that imports
# (or even defines) `re` must not be flagged.
create_test_file "imports_re.py" 'import re

regex = re.compile(r"^.*$")
'
assert_passes "Imported names from framework module are not part of public API"
reset_tmpdir

# ─── Test 16: multiple violations in one file detected ───────────────
create_test_file "multi.py" 'import re

KEY_PATTERN = re.compile(r"^.*$")


def parse_flat_hash_key(key):
    return None


def is_mislabel(a, b):
    return False
'
assert_fails "Multiple violations in one file are detected"
reset_tmpdir

# ─── Test 17: failure output names the offending file:line:name ──────
create_test_file "outpath.py" 'def parse_flat_hash_key(key):
    return None
'
TESTS=$((TESTS + 1))
out=$(run_capture)
if echo "$out" | grep -q 'outpath.py:.*:parse_flat_hash_key'; then
    echo "PASS: Failure output includes the offending file:line:name"
else
    echo "FAIL: Failure output is missing the offending file:line:name"
    echo "----- captured output -----"
    echo "$out"
    echo "---------------------------"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 18: failure output emits a Fix: block ──────────────────────
# Per docs/dx/check-script-fix-block-coverage.md (#4346), the guard
# must emit a copy-pasteable `Fix:` block. The block must reference
# both reference implementations and the marker recipe.
create_test_file "fixblock.py" 'def parse_flat_hash_key(key):
    return None
'
TESTS=$((TESTS + 1))
out=$(run_capture)
fail=0
if ! echo "$out" | grep -q '^  Fix:'; then
    echo "FAIL: Fix block label not emitted"
    fail=1
fi
if ! echo "$out" | grep -q 'cleanup_mislabeled_s3_2661.py'; then
    echo "FAIL: Fix block missing reference to cleanup_mislabeled_s3_2661.py"
    fail=1
fi
if ! echo "$out" | grep -q 'repoint_mislabeled_documents_4439.py'; then
    echo "FAIL: Fix block missing reference to repoint_mislabeled_documents_4439.py"
    fail=1
fi
if ! echo "$out" | grep -q '# allow-duplicate-framework-helpers:'; then
    echo "FAIL: Fix block missing allowlist marker hint"
    fail=1
fi
if ! echo "$out" | grep -q 'from framework.s3_keys import'; then
    echo "FAIL: Fix block missing canonical import line"
    fail=1
fi
if [[ $fail -eq 0 ]]; then
    echo "PASS: Failure output emits Fix block with both reference files + marker hint + import line"
else
    echo "----- captured output -----"
    echo "$out"
    echo "---------------------------"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 19: missing framework module → exit 0 (defensive) ──────────
TESTS=$((TESTS + 1))
if FRAMEWORK_S3_KEYS_PATH="$TMPDIR_TEST/does-not-exist.py" \
    "$CHECK_SCRIPT" "$SCRIPTS_DIR" > /dev/null 2>&1; then
    echo "PASS: Missing framework module exits 0 (defensive — other CI guards catch this)"
else
    echo "FAIL: Missing framework module should exit 0"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 20: nested subdirectories under scripts/ ARE scanned ───────
# Unlike scripts/archive/ and scripts/tests/, other nested
# subdirectories (scripts/dispatcher/, scripts/spotcheck/, etc.) ARE
# scanned. The exclusion list is intentionally narrow — only
# archive/ + tests/ are skipped.
mkdir -p "$SCRIPTS_DIR/dispatcher"
printf '%s\n' 'def parse_flat_hash_key(key):
    return None
' > "$SCRIPTS_DIR/dispatcher/scanned.py"
assert_fails "Nested scripts/<other>/ subdirectories ARE scanned"
reset_tmpdir

# ─── Test 21: real framework module is parseable ─────────────────────
# Sanity check — the real framework.s3_keys module must be parseable
# by the guard's collect_public_api(). Run the helper directly against
# the real path and verify it returns a non-empty set containing the
# five known exports.
TESTS=$((TESTS + 1))
real_framework="$SCRIPT_DIR/../packages/scraper-framework/src/framework/s3_keys.py"
if [[ -f "$real_framework" ]]; then
    api_check_out=$(python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR')
from check_no_duplicate_framework_helpers import collect_public_api
from pathlib import Path
api = collect_public_api(Path('$real_framework'))
expected = {'KEY_PATTERN', 'parse_flat_hash_key', 'is_mislabel', 'head_object_metadata_hash', 'build_twin_key'}
missing = expected - api
unexpected_imports = {'re', 'ClientError'} & api
if missing or unexpected_imports:
    print(f'missing={missing} unexpected_imports={unexpected_imports}')
    sys.exit(1)
print('ok')
" 2>&1)
    if [[ "$api_check_out" == "ok" ]]; then
        echo "PASS: Real framework module's public API matches expected (5 exports, no imports)"
    else
        echo "FAIL: Real framework module's public API mismatch: $api_check_out"
        FAILURES=$((FAILURES + 1))
    fi
else
    echo "PASS: Skipping real-framework probe (module not present at $real_framework)"
fi

# ─── Test 22: No self-match on ci.yml step name ──────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain the forbidden patterns. See #2541/#2542 for the
# class of self-match bug, and the CLAUDE.md §Hygiene-check CI steps
# rule the issue's AC #3 cites.
# shellcheck source=./_guard_self_match_helpers.sh
TMPDIR_TEST_BACKUP="$TMPDIR_TEST"
TMPDIR_TEST="$SCRIPTS_DIR"
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-no-duplicate-framework-helpers.sh" "py"
TMPDIR_TEST="$TMPDIR_TEST_BACKUP"

# ─── Summary ──────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
