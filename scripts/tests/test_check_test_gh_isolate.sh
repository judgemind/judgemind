#!/usr/bin/env bash
# test_check_test_gh_isolate.sh — Tests for scripts/check-test-gh-isolate.sh (#5071).
#
# Builds fake repo roots with Python and shell tests that are (or are not)
# cut off from real GitHub, and checks the guard's verdict and Fix: block.
#
# Usage:
#   scripts/tests/test_check_test_gh_isolate.sh

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_gh-isolate.sh"  # no real GitHub from tests (#5071)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK="$SCRIPT_DIR/check-test-gh-isolate.sh"
CANON="$SCRIPT_DIR/tests/conftest.py"
. "$SCRIPT_DIR/tests/_test-helpers.sh"
. "$SCRIPT_DIR/tests/_temp_cleanup_helpers.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST="$(mktemp -d)"
register_temp_dir "$TMPDIR_TEST"

new_root() {
    local root="$TMPDIR_TEST/$1"
    mkdir -p "$root/scripts/tests"
    cp "$CANON" "$root/scripts/tests/conftest.py"
    echo "$root"
}

write_py_test() {
    mkdir -p "$(dirname "$1")"
    printf 'def test_x():\n    assert True\n' > "$1"
}

write_sh_test() {
    mkdir -p "$(dirname "$1")"
    printf '#!/usr/bin/env bash\nset -euo pipefail\n%s\necho ok\n' "$2" > "$1"
}

run_check() {
    rc=0
    out="$("$CHECK" "$1" 2>"$TMPDIR_TEST/err")" || rc=$?
    err="$(cat "$TMPDIR_TEST/err")"
}

# ─── 1. Live tree passes ──────────────────────────────────────────────────
run_check "$SCRIPT_DIR/.."
assert_eq "live tree: exit 0" "$rc" "0" --err-file "$TMPDIR_TEST/err"

# ─── 2. Covered tests pass ────────────────────────────────────────────────
root="$(new_root ok)"
write_py_test "$root/scripts/tests/test_a.py"
mkdir -p "$root/pkg/tests"
cp "$CANON" "$root/pkg/tests/conftest.py"
write_py_test "$root/pkg/tests/sub/test_nested.py"
write_sh_test "$root/scripts/tests/test_s.sh" '. "$(dirname "${BASH_SOURCE[0]}")/_gh-isolate.sh"'
# Files outside a tests/ directory are not test roots.
write_py_test "$root/scripts/archive/test_old.py"
# Excluded directories are not walked.
write_py_test "$root/node_modules/foo/tests/test_vendored.py"
run_check "$root"
assert_eq "covered tests: exit 0" "$rc" "0" --err-file "$TMPDIR_TEST/err"
assert_contains "covered tests: OK line" "$out" "OK:"

# ─── 3. A Python test root with no conftest block ─────────────────────────
root="$(new_root py_missing)"
write_py_test "$root/other/tests/deep/test_b.py"
write_py_test "$root/other/tests/test_c.py"
run_check "$root"
assert_eq "uncovered python root: exit 1" "$rc" "1"
assert_contains "uncovered python root: names the root" "$err" "other/tests/ has Python tests"
assert_contains "uncovered python root: Fix block" "$err" "Fix:"
assert_contains "uncovered python root: Fix names the conftest to edit" "$err" "    other/tests/conftest.py"
assert_contains "uncovered python root: Fix names the imports" "$err" "'import os' and 'import pytest'"
count="$(printf '%s\n' "$err" | grep -c 'has Python tests' || true)"
assert_eq "uncovered python root: reported once per root" "$count" "1"

# ─── 4. Drifted copy of the block ─────────────────────────────────────────
root="$(new_root drift)"
mkdir -p "$root/pkg/tests"
sed 's/exit 4/exit 0/' "$CANON" > "$root/pkg/tests/conftest.py"
run_check "$root"
assert_eq "drifted copy: exit 1" "$rc" "1"
assert_contains "drifted copy: names the file" "$err" "pkg/tests/conftest.py has a gh-isolate block that differs"
assert_contains "drifted copy: Fix shows the diff command" "$err" "<(sed -n '/^# --- BEGIN gh-isolate/,/^# --- END gh-isolate/p' pkg/tests/conftest.py)"

# ─── 5. Missing canonical copy ────────────────────────────────────────────
root="$(new_root no_canon)"
: > "$root/scripts/tests/conftest.py"
run_check "$root"
assert_eq "missing canonical block: exit 1" "$rc" "1"
assert_contains "missing canonical block: says so" "$err" "scripts/tests/conftest.py (the canonical copy) is missing"

# ─── 6. Shell test that does not source the helper ────────────────────────
root="$(new_root sh_missing)"
write_sh_test "$root/scripts/tests/test_s.sh" ""
write_sh_test "$root/scripts/dispatcher/tests/test_d.sh" ""
run_check "$root"
assert_eq "unsourced shell tests: exit 1" "$rc" "1"
assert_contains "unsourced shell test: names the file" "$err" "scripts/tests/test_s.sh does not source _gh-isolate.sh"
assert_contains "unsourced shell test: Fix gives the line for scripts/tests" "$err" \
    '. "$(dirname "${BASH_SOURCE[0]}")/_gh-isolate.sh"  # no real GitHub from tests (#5071)'
assert_contains "unsourced shell test: Fix gives the line for a nested dir" "$err" \
    '. "$(dirname "${BASH_SOURCE[0]}")/../../../scripts/tests/_gh-isolate.sh"'

# ─── 7. The Fix line for a nested dir actually resolves ───────────────────
nested_dir="$SCRIPT_DIR/dispatcher/tests"
if [ -f "$nested_dir/../../../scripts/tests/_gh-isolate.sh" ]; then
    pass "nested Fix path resolves from scripts/dispatcher/tests"
else
    fail "nested Fix path resolves from scripts/dispatcher/tests" "$nested_dir/../../../scripts/tests/_gh-isolate.sh missing"
fi

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
