#!/usr/bin/env bash
# test_wait_for_ci.sh — Unit tests for scripts/wait-for-ci.sh
#
# Exercises the check-runs polling loop against a mock gh CLI that emits a
# sequence of pre-canned JSON responses keyed on call number. Verifies the
# success (exit 0), early-failure (exit 1), timeout (exit 2), missing-arg,
# and --help paths.
#
# Usage:
#   scripts/tests/test_wait_for_ci.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_UNDER_TEST="$REPO_ROOT/scripts/wait-for-ci.sh"

if [ ! -x "$SCRIPT_UNDER_TEST" ]; then
    echo "FATAL: $SCRIPT_UNDER_TEST is not executable." >&2
    exit 1
fi

FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

TEMP_DIRS=()
# shellcheck disable=SC2329  # invoked via trap below
cleanup() {
    set +e
    for d in ${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}; do
        if [ -n "$d" ] && [ -d "$d" ]; then
            rm -rf "$d"
        fi
    done
}
trap cleanup EXIT

make_temp_dir() {
    local dir
    dir=$(mktemp -d)
    TEMP_DIRS+=("$dir")
    mkdir -p "$dir/responses"
    echo "$dir"
}

pass() {
    TESTS=$((TESTS + 1))
    echo "PASS: $1"
}

fail() {
    TESTS=$((TESTS + 1))
    FAILURES=$((FAILURES + 1))
    echo "FAIL: $1"
    if [ -n "${2:-}" ]; then
        echo "  $2"
    fi
}

# Create a mock `gh` CLI that reads a sequence of JSON blobs from a responses
# directory, emitting each on successive calls. The mock handles:
#   - `gh pr view ... --json headRefOid`  → canned SHA from SHA_RESPONSE env
#   - `gh api repos/.../check-runs...`   → next response from $RESPONSES_DIR/NN.json
#   - `gh pr view ... --json mergeStateStatus` → canned status from MERGE_STATE_RESPONSE env
#
# Usage: setup_mock_gh <tmpdir>
setup_mock_gh() {
    local tmpdir="$1"
    local mock="$tmpdir/gh"

    mkdir -p "$tmpdir/responses"

    cat > "$mock" << 'MOCK'
#!/usr/bin/env bash
# Mock gh CLI — routes calls based on argument patterns.
set -euo pipefail

RESPONSES_DIR="${RESPONSES_DIR:?RESPONSES_DIR required}"
CALL_COUNTER_FILE="${CALL_COUNTER_FILE:?CALL_COUNTER_FILE required}"
SHA_RESPONSE="${SHA_RESPONSE:-deadbeefdeadbeef}"
MERGE_STATE_RESPONSE="${MERGE_STATE_RESPONSE:-CLEAN}"

ARGS="$*"

# Route: pr view ... headRefOid — return canned SHA (plain string, like gh -q output).
case "$ARGS" in
    *"headRefOid"*)
        printf '%s\n' "$SHA_RESPONSE"
        exit 0
        ;;
    *"mergeStateStatus"*)
        printf '%s\n' "$MERGE_STATE_RESPONSE"
        exit 0
        ;;
    *"check-runs"*)
        # Fall through to response-file dispatch below.
        ;;
    *)
        echo "MOCK: unrecognised call: $ARGS" >&2
        exit 1
        ;;
esac

# Read and increment call counter.
if [ -f "$CALL_COUNTER_FILE" ]; then
    COUNT=$(cat "$CALL_COUNTER_FILE")
else
    COUNT=0
fi
COUNT=$((COUNT + 1))
printf '%s' "$COUNT" > "$CALL_COUNTER_FILE"

# Pick response file; clamp to highest available.
RESPONSE_FILE=$(printf '%s/%02d.json' "$RESPONSES_DIR" "$COUNT")
if [ ! -f "$RESPONSE_FILE" ]; then
    # Fall back to the highest-numbered file.
    RESPONSE_FILE=$(ls -1 "$RESPONSES_DIR"/*.json 2>/dev/null | sort | tail -n 1 || echo "")
fi

if [ -z "$RESPONSE_FILE" ] || [ ! -f "$RESPONSE_FILE" ]; then
    echo "MOCK ERROR: no response file available for call $COUNT" >&2
    exit 1
fi

cat "$RESPONSE_FILE"
MOCK
    chmod +x "$mock"
    echo "$mock"
}

# Run the script under test with the mock gh wired up.
# Usage: run_script <tmpdir> [extra-args...]
run_script() {
    local tmpdir="$1"
    shift
    local mock_gh
    mock_gh=$(setup_mock_gh "$tmpdir")

    PATH="$tmpdir:$PATH" \
    RESPONSES_DIR="$tmpdir/responses" \
    CALL_COUNTER_FILE="$tmpdir/counter" \
    SHA_RESPONSE="${SHA_RESPONSE:-deadbeefdeadbeef}" \
    MERGE_STATE_RESPONSE="${MERGE_STATE_RESPONSE:-CLEAN}" \
        "$SCRIPT_UNDER_TEST" "$@"
}

# Write a check-runs response where all checks are in_progress.
write_in_progress_response() {
    local file="$1"
    cat > "$file" << 'JSON'
{
  "check_runs": [
    {"name": "lint",       "status": "in_progress", "conclusion": null, "details_url": "https://example.com/lint"},
    {"name": "unit-tests", "status": "in_progress", "conclusion": null, "details_url": "https://example.com/unit"}
  ]
}
JSON
}

# Write a check-runs response where all checks succeeded including ci-passed.
write_all_success_response() {
    local file="$1"
    cat > "$file" << 'JSON'
{
  "check_runs": [
    {"name": "lint",       "status": "completed", "conclusion": "success", "details_url": "https://example.com/lint"},
    {"name": "unit-tests", "status": "completed", "conclusion": "success", "details_url": "https://example.com/unit"},
    {"name": "ci-passed",  "status": "completed", "conclusion": "success", "details_url": "https://example.com/ci"}
  ]
}
JSON
}

# Write a check-runs response with one failed check.
write_failure_response() {
    local file="$1"
    local check_name="${2:-web-tests}"
    cat > "$file" << JSON
{
  "check_runs": [
    {"name": "lint",        "status": "completed", "conclusion": "success",  "details_url": "https://example.com/lint"},
    {"name": "$check_name", "status": "completed", "conclusion": "failure",  "details_url": "https://example.com/web-tests"}
  ]
}
JSON
}

# ── Tests ──────────────────────────────────────────────────────────────────

# Test 1: Happy path — first poll in-progress, second poll all success + ci-passed,
# then mergeStateStatus=CLEAN. Expect exit 0.
test_happy_path() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_in_progress_response "$tmpdir/responses/01.json"
    write_all_success_response "$tmpdir/responses/02.json"

    local output exit_code
    exit_code=0
    output=$(MERGE_STATE_RESPONSE=CLEAN \
        run_script "$tmpdir" 42 --poll-interval 0 --timeout-secs 60 2>&1) || exit_code=$?

    if [ "$exit_code" -eq 0 ]; then
        pass "happy_path: exits 0 on success"
    else
        fail "happy_path: exits 0 on success" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "CI passed"; then
        pass "happy_path: emits success message"
    else
        fail "happy_path: emits success message" "output=$output"
    fi
}

# Test 2: Early failure — first check-runs poll returns web-tests=failure.
# Expect exit 1, output mentions "web-tests".
test_early_failure() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_failure_response "$tmpdir/responses/01.json" "web-tests"

    local output exit_code
    exit_code=0
    output=$(run_script "$tmpdir" 42 --poll-interval 0 --timeout-secs 60 2>&1) || exit_code=$?

    if [ "$exit_code" -eq 1 ]; then
        pass "early_failure: exits 1 on failure"
    else
        fail "early_failure: exits 1 on failure" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "web-tests"; then
        pass "early_failure: output mentions failed check name"
    else
        fail "early_failure: output mentions failed check name" "output=$output"
    fi
}

# Test 3: Timeout — check-runs always returns in_progress. Use short timeout. Expect exit 2.
test_timeout() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_in_progress_response "$tmpdir/responses/01.json"

    local output exit_code
    exit_code=0
    output=$(run_script "$tmpdir" 42 --timeout-secs 2 --poll-interval 1 2>&1) || exit_code=$?

    if [ "$exit_code" -eq 2 ]; then
        pass "timeout: exits 2 on timeout"
    else
        fail "timeout: exits 2 on timeout" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "Timed out"; then
        pass "timeout: emits timeout message"
    else
        fail "timeout: emits timeout message" "output=$output"
    fi
}

# Test 4: Missing pr-number arg (no --help). Expect non-zero exit.
test_missing_pr_arg() {
    local output exit_code
    exit_code=0
    output=$("$SCRIPT_UNDER_TEST" 2>&1) || exit_code=$?

    if [ "$exit_code" -ne 0 ]; then
        pass "missing_pr_arg: exits non-zero when no pr-number given"
    else
        fail "missing_pr_arg: exits non-zero when no pr-number given" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -qi "required\|usage"; then
        pass "missing_pr_arg: emits helpful error"
    else
        fail "missing_pr_arg: emits helpful error" "output=$output"
    fi
}

# Test 5: --help flag. Expect exit 0 and output mentions "Usage".
test_help() {
    local output exit_code
    exit_code=0
    output=$("$SCRIPT_UNDER_TEST" --help 2>&1) || exit_code=$?

    if [ "$exit_code" -eq 0 ]; then
        pass "help: exits 0 with --help"
    else
        fail "help: exits 0 with --help" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -qi "usage"; then
        pass "help: output mentions Usage"
    else
        fail "help: output mentions Usage" "output=$output"
    fi
}

# ── Run all tests ──────────────────────────────────────────────────────────

test_happy_path
test_early_failure
test_timeout
test_missing_pr_arg
test_help

echo ""
echo "────────────────────────────────────────────"
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [ "$FAILURES" -gt 0 ]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
