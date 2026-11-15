#!/usr/bin/env bash
# test_check_test_git_env_scrub.sh — Tests for scripts/check-test-git-env-scrub.sh (#5058).
#
# Builds fake repo roots with Python and shell tests that do (or only mock)
# git writes, and checks the guard's verdict and its Fix: block.
#
# Usage:
#   scripts/tests/test_check_test_git_env_scrub.sh

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_git-env-scrub.sh"  # unset inherited GIT_DIR etc. (#5058)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK="$SCRIPT_DIR/check-test-git-env-scrub.sh"
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

write_py_git_test() {
    mkdir -p "$(dirname "$1")"
    cat > "$1" <<'EOF'
import subprocess


def test_x(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=30)
EOF
}

write_sh_git_test() {
    mkdir -p "$(dirname "$1")"
    printf '#!/usr/bin/env bash\nset -euo pipefail\n%s\ngit -C "$repo" commit -q -m init\n' "$2" > "$1"
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
write_py_git_test "$root/scripts/tests/test_a.py"
mkdir -p "$root/pkg/tests"
cp "$CANON" "$root/pkg/tests/conftest.py"
write_py_git_test "$root/pkg/tests/sub/test_nested.py"
write_sh_git_test "$root/scripts/tests/test_s.sh" '. "$(dirname "${BASH_SOURCE[0]}")/_git-env-scrub.sh"'
# A mock-only file (no git invocation) is not a git writer, even with no
# conftest above it.
mkdir -p "$root/nocov/tests"
cat > "$root/nocov/tests/test_mock.py" <<'EOF'
def test_mock(conn):
    conn.commit.side_effect = lambda: None
    assert ["commit"]
EOF
run_check "$root"
assert_eq "covered tests: exit 0" "$rc" "0" --err-file "$TMPDIR_TEST/err"
assert_contains "covered tests: OK line" "$out" "OK:"

# ─── 3. Python git writer with no conftest block ──────────────────────────
root="$(new_root py_missing)"
write_py_git_test "$root/other/tests/deep/test_b.py"
run_check "$root"
assert_eq "uncovered python test: exit 1" "$rc" "1"
assert_contains "uncovered python test: names the file" "$err" "other/tests/deep/test_b.py writes to git"
assert_contains "uncovered python test: Fix block" "$err" "Fix:"
assert_contains "uncovered python test: Fix names the conftest to edit" "$err" "    other/tests/conftest.py"
assert_contains "uncovered python test: Fix names the imports" "$err" "'import subprocess' and 'import pytest'"

# ─── 4. Drifted copy of the block ─────────────────────────────────────────
root="$(new_root drift)"
mkdir -p "$root/pkg/tests"
sed 's/Judgemind Test/Someone Else/' "$CANON" > "$root/pkg/tests/conftest.py"
run_check "$root"
assert_eq "drifted copy: exit 1" "$rc" "1"
assert_contains "drifted copy: names the file" "$err" "pkg/tests/conftest.py has a git-env-scrub block that differs"
assert_contains "drifted copy: Fix shows the diff command" "$err" "<(sed -n '/^# --- BEGIN git-env-scrub/,/^# --- END git-env-scrub/p' pkg/tests/conftest.py)"

# ─── 5. Missing canonical copy ────────────────────────────────────────────
root="$(new_root no_canon)"
: > "$root/scripts/tests/conftest.py"
run_check "$root"
assert_eq "missing canonical block: exit 1" "$rc" "1"
assert_contains "missing canonical block: says so" "$err" "scripts/tests/conftest.py (the canonical copy) is missing"

# ─── 6. Shell git writer that does not source the helper ──────────────────
root="$(new_root sh_missing)"
write_sh_git_test "$root/scripts/tests/test_s.sh" ""
write_sh_git_test "$root/scripts/dispatcher/tests/test_d.sh" ""
run_check "$root"
assert_eq "unsourced shell tests: exit 1" "$rc" "1"
assert_contains "unsourced shell test: names the file" "$err" "scripts/tests/test_s.sh writes to git but does not source _git-env-scrub.sh"
assert_contains "unsourced shell test: Fix gives the line for scripts/tests" "$err" \
    '. "$(dirname "${BASH_SOURCE[0]}")/_git-env-scrub.sh"  # unset inherited GIT_DIR etc. (#5058)'
assert_contains "unsourced shell test: Fix gives the line for a nested dir" "$err" \
    '. "$(dirname "${BASH_SOURCE[0]}")/../../../scripts/tests/_git-env-scrub.sh"'

# ─── 7. The Fix line for a nested dir actually resolves ───────────────────
nested_dir="$SCRIPT_DIR/dispatcher/tests"
if [ -f "$nested_dir/../../../scripts/tests/_git-env-scrub.sh" ]; then
    pass "nested Fix path resolves from scripts/dispatcher/tests"
else
    fail "nested Fix path resolves from scripts/dispatcher/tests" "$nested_dir/../../../scripts/tests/_git-env-scrub.sh missing"
fi

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
