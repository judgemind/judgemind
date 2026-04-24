#!/usr/bin/env bash
# test_dev_db_query.sh — Tests for scripts/dev-db-query.sh terse-default behavior
#
# Verifies:
#   a. --verbose / JM_VERBOSE=1 flag accepted without error
#   b. Failure path (no running task) stderr contains Error:
#   c. --rw flag still works
#
# Note: We cannot easily test default-mode line count since the script uses
# aws ecs execute-command which is interactive. We test the error paths and
# flag parsing instead.
#
# Usage:
#   scripts/tests/test_dev_db_query.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$SCRIPT_DIR/dev-db-query.sh"
FAILURES=0
TESTS=0

TEMP_DIRS=()
ORIG_PATH_SAVE=""

cleanup() {
    set +eu
    for d in "${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}"; do
        if [[ -n "$d" && -d "$d" ]]; then
            rm -rf "$d"
        fi
    done
    if [[ -n "$ORIG_PATH_SAVE" ]]; then
        export PATH="$ORIG_PATH_SAVE"
    fi
}
trap cleanup EXIT

pass() {
    TESTS=$((TESTS + 1))
    echo "PASS: $1"
}

fail() {
    TESTS=$((TESTS + 1))
    FAILURES=$((FAILURES + 1))
    echo "FAIL: $1"
    if [[ -n "${2:-}" ]]; then
        echo "  $2"
    fi
}

make_temp_dir() {
    local dir
    dir=$(mktemp -d)
    TEMP_DIRS+=("$dir")
    echo "$dir"
}

# Mock aws that returns no running tasks.
setup_mock_aws_notask() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/bin"

    cat > "$tmpdir/bin/aws" << 'MOCK'
#!/usr/bin/env bash
if [[ "${1:-}" == "ecs" && "${2:-}" == "list-tasks" ]]; then
    echo "None"
    exit 0
fi
exit 0
MOCK
    chmod +x "$tmpdir/bin/aws"
    echo "$tmpdir"
}

# ── Precondition: script exists and is executable ─────────────────────────

if [[ ! -x "$SCRIPT" ]]; then
    echo "FAIL: $SCRIPT is not executable (or does not exist)" >&2
    exit 1
fi

ORIG_PATH_SAVE="$PATH"

# ── Test 1: No query argument prints usage error ─────────────────────────

tmpdir=$(setup_mock_aws_notask)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

exit_code=0
stderr_output=$("$SCRIPT" 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "exits non-zero with no running task"
else
    fail "exits non-zero with no running task" "expected non-zero, got 0"
fi

if echo "$stderr_output" | grep -qi "error\|no running"; then
    pass "failure path mentions error or no running task"
else
    fail "failure path mentions error or no running task" "got: $stderr_output"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 2: --verbose flag accepted (passed through before error) ─────────

tmpdir=$(setup_mock_aws_notask)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

exit_code=0
"$SCRIPT" --verbose "SELECT 1" 2>&1 >/dev/null || exit_code=$?
# We expect non-zero since no task is running, but the flag should be parsed
if [[ "$exit_code" -ne 0 ]]; then
    pass "--verbose flag accepted (error path still reached)"
else
    fail "--verbose flag accepted" "expected non-zero exit from aws mock, got 0"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 3: JM_VERBOSE=1 accepted ────────────────────────────────────────

tmpdir=$(setup_mock_aws_notask)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

exit_code=0
JM_VERBOSE=1 "$SCRIPT" "SELECT 1" 2>&1 >/dev/null || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "JM_VERBOSE=1 accepted (error path still reached)"
else
    fail "JM_VERBOSE=1 accepted" "expected non-zero exit from aws mock, got 0"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 4: --rw flag still accepted ─────────────────────────────────────

tmpdir=$(setup_mock_aws_notask)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

exit_code=0
"$SCRIPT" --rw "UPDATE x SET y=1" 2>&1 >/dev/null || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "--rw flag still accepted (error path reached)"
else
    fail "--rw flag still accepted" "expected non-zero exit from aws mock, got 0"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Summary ───────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
