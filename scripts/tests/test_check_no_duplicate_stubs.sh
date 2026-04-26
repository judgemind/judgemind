#!/usr/bin/env bash
# test_check_no_duplicate_stubs.sh — Tests for check-no-duplicate-stubs.sh.
#
# Creates temporary files to verify that the checker correctly detects
# duplicate stub-generation blocks for the same binary while allowing
# single definitions, different binaries, comment-only mentions, and
# files outside scripts/tests/.
#
# Usage:
#   scripts/tests/test_check_no_duplicate_stubs.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-duplicate-stubs.sh"
FAILURES=0
TESTS=0

# Use a temp directory so we don't pollute the repo.  We pass this dir
# as the scan directory explicitly, so the guard looks for test_*.sh
# files inside it rather than the real scripts/tests/ directory.
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# create_test_file <name> <content>
# Creates $TMPDIR_TEST/<name> (mkdir -p parents) and returns the path.
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

# ─── Test 1: Single stub definition passes ───────────────────────────────────
create_test_file "test_something.sh" 'cat > "$STUB_BIN/gh" <<'"'"'GHEOF'"'"'
#!/usr/bin/env bash
exit 0
GHEOF'
assert_passes "Single gh stub definition passes"
reset_tmpdir

# ─── Test 2: Duplicate gh stubs fail ─────────────────────────────────────────
create_test_file "test_agent_runner.sh" 'cat > "$STUB_BIN/gh" <<'"'"'GHEOF'"'"'
#!/usr/bin/env bash
exit 0
GHEOF

cat > "$STUB_BIN/gh" <<'"'"'GHEOF2'"'"'
#!/usr/bin/env bash
exit 1
GHEOF2'
assert_fails "Duplicate gh stubs in same file are detected"
reset_tmpdir

# ─── Test 3: Duplicate gh stubs — error names both line numbers ───────────────
test_file=$(create_test_file "test_agent_runner.sh" 'cat > "$STUB_BIN/gh" <<'"'"'GHEOF'"'"'
#!/usr/bin/env bash
exit 0
GHEOF

cat > "$STUB_BIN/gh" <<'"'"'GHEOF2'"'"'
#!/usr/bin/env bash
exit 1
GHEOF2')
TESTS=$((TESTS + 1))
output=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true)
if printf '%s' "$output" | grep -q 'Line 1:' && printf '%s' "$output" | grep -q 'Line 2:'; then
    echo "PASS: Error message names both line numbers"
else
    echo "FAIL: Error message does not name both line numbers"
    echo "  Output: $output"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 4: Different binaries in same file pass ─────────────────────────────
create_test_file "test_something.sh" 'cat > "$STUB_BIN/gh" <<'"'"'GHEOF'"'"'
#!/usr/bin/env bash
exit 0
GHEOF
cat > "$STUB_BIN/psql" <<'"'"'PSQLEOF'"'"'
#!/usr/bin/env bash
exit 0
PSQLEOF'
assert_passes "Different binaries (gh and psql) in same file pass"
reset_tmpdir

# ─── Test 5: Comment-only mentions pass ───────────────────────────────────────
create_test_file "test_something.sh" '# cat > "$STUB_BIN/gh" << would be the pattern
# We define gh stub below:
cat > "$STUB_BIN/gh" <<'"'"'GHEOF'"'"'
#!/usr/bin/env bash
exit 0
GHEOF'
assert_passes "Comment-only mentions of the pattern are not flagged"
reset_tmpdir

# ─── Test 6: Files outside scripts/tests/ are not scanned ────────────────────
# Create a duplicate-stub file in a subdirectory — guard uses maxdepth 1
# so it won't descend into subdirs.
mkdir -p "$TMPDIR_TEST/subdir"
printf '%s\n' 'cat > "$STUB_BIN/gh" <<'"'"'EOF'"'"'
exit 0
EOF
cat > "$STUB_BIN/gh" <<'"'"'EOF2'"'"'
exit 1
EOF2' > "$TMPDIR_TEST/subdir/test_nested.sh"
assert_passes "Files in subdirectories are not scanned (maxdepth 1)"
reset_tmpdir

# ─── Test 7: Non-test_*.sh files are not scanned ─────────────────────────────
create_test_file "helper_utils.sh" 'cat > "$STUB_BIN/gh" <<'"'"'EOF'"'"'
exit 0
EOF
cat > "$STUB_BIN/gh" <<'"'"'EOF2'"'"'
exit 1
EOF2'
assert_passes "Files not matching test_*.sh glob are not scanned"
reset_tmpdir

# ─── Test 8: Empty directory passes ──────────────────────────────────────────
assert_passes "Empty directory passes"

# ─── Test 9: Duplicate psql stubs fail ───────────────────────────────────────
create_test_file "test_agent_runner.sh" 'cat > "$STUB_BIN/psql" <<'"'"'PSQLEOF'"'"'
#!/usr/bin/env bash
exit 0
PSQLEOF

cat > "$STUB_BIN/psql" <<'"'"'PSQLEOF2'"'"'
#!/usr/bin/env bash
exit 1
PSQLEOF2'
assert_fails "Duplicate psql stubs in same file are detected"
reset_tmpdir

# ─── Test 10: Multiple files — violation in one, clean in other ───────────────
create_test_file "test_clean.sh" 'cat > "$STUB_BIN/gh" <<'"'"'GHEOF'"'"'
#!/usr/bin/env bash
exit 0
GHEOF'
create_test_file "test_bad.sh" 'cat > "$STUB_BIN/gh" <<'"'"'GHEOF'"'"'
#!/usr/bin/env bash
exit 0
GHEOF
cat > "$STUB_BIN/gh" <<'"'"'GHEOF2'"'"'
#!/usr/bin/env bash
exit 1
GHEOF2'
assert_fails "Violation in one file is detected even when another file is clean"
reset_tmpdir

# ─── Test 11: No self-match on ci.yml step name ──────────────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain the forbidden pattern (cat > "$STUB_BIN/...").
# The guard scans test_*.sh files, so the synthesized step-name file
# (ci_step_names.sh) won't match the glob — guard always passes in
# TMPDIR_TEST. The assertion still verifies the step name is not
# mis-named in a way that would cause issues.
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-no-duplicate-stubs.sh" "sh"

# ─── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
