#!/usr/bin/env bash
# test_wait_for_run.sh — Unit tests for scripts/wait-for-run.sh (#4835)
#
# Drives the script against a stub `gh` that answers `gh run view` from a
# numbered sequence of "<status> <conclusion>" lines.  Verifies success,
# failure, the still-waiting exit (124) and that re-running resumes, the
# status-unknown exit (125), and usage errors.
#
# Usage:
#   scripts/tests/test_wait_for_run.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_UNDER_TEST="$SCRIPT_DIR/wait-for-run.sh"

FAILURES=0
TESTS=0

TEMP_DIRS=()
# shellcheck disable=SC2329  # invoked via trap
cleanup() {
    set +e
    for d in ${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}; do
        if [[ -n "$d" && -d "$d" ]]; then
            rm -rf "$d"
        fi
    done
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

# make_stub <line>... — echo a tmpdir whose bin/gh answers `gh run view`
# with the Nth line on the Nth call (the last line repeats).  A line of
# "ERROR" makes that call fail.  Every call is logged to calls.log.
make_stub() {
    local dir
    dir=$(mktemp -d)
    TEMP_DIRS+=("$dir")
    mkdir -p "$dir/bin"
    printf '%s\n' "$@" > "$dir/responses"
    cat > "$dir/bin/gh" << 'GH_EOF'
#!/usr/bin/env bash
dir="${STUB_DIR:?}"
echo "$*" >> "$dir/calls.log"
if [[ "$1 $2" != "run view" ]]; then
    echo "stub gh: unexpected call: $*" >&2
    exit 1
fi
n=$(cat "$dir/count" 2>/dev/null || echo 0)
n=$((n + 1)); echo "$n" > "$dir/count"
total=$(wc -l < "$dir/responses" | tr -d ' ')
if [[ "$n" -gt "$total" ]]; then n=$total; fi
line=$(sed -n "${n}p" "$dir/responses")
if [[ "$line" == "ERROR" ]]; then
    echo "HTTP 401: Bad credentials" >&2
    exit 1
fi
echo "$line"
GH_EOF
    cat > "$dir/bin/sleep" << 'SLEEP_EOF'
#!/usr/bin/env bash
exit 0
SLEEP_EOF
    chmod +x "$dir/bin/gh" "$dir/bin/sleep"
    echo "$dir"
}

# run_it <stubdir> [args...] — sets RUN_OUTPUT / RUN_EC.
run_it() {
    local dir="$1"
    shift
    set +e
    RUN_OUTPUT=$(PATH="$dir/bin:$PATH" STUB_DIR="$dir" "$SCRIPT_UNDER_TEST" "$@" 2>&1)
    RUN_EC=$?
    set -e
}

test_default_timeout_under_tool_cap() {
    local default
    default=$(grep -E '^TIMEOUT_SECS=[0-9]+$' "$SCRIPT_UNDER_TEST" | head -n 1 | cut -d= -f2)
    if [[ -n "$default" && "$default" -le 540 ]]; then
        pass "default --timeout-secs ($default s) is <= 540s"
    else
        fail "default --timeout-secs ($default s) is <= 540s"
    fi
}

test_success() {
    local dir
    dir=$(make_stub "in_progress " "completed success")
    run_it "$dir" 123 --poll-interval 0
    if [[ "$RUN_EC" -eq 0 ]]; then
        pass "completed success exits 0"
    else
        fail "completed success exits 0" "exit=$RUN_EC output=$RUN_OUTPUT"
    fi
}

test_failure() {
    local dir
    dir=$(make_stub "completed failure")
    run_it "$dir" 123
    if [[ "$RUN_EC" -eq 1 ]] && echo "$RUN_OUTPUT" | grep -q -- "--log-failed"; then
        pass "completed failure exits 1 and points at --log-failed"
    else
        fail "completed failure exits 1 and points at --log-failed" "exit=$RUN_EC output=$RUN_OUTPUT"
    fi
}

test_still_waiting_then_resume() {
    local dir
    # A 0s budget stops after one poll.
    dir=$(make_stub "in_progress " "completed success")
    run_it "$dir" 123 --timeout-secs 0
    if [[ "$RUN_EC" -eq 124 ]]; then
        pass "still running exits 124"
    else
        fail "still running exits 124" "exit=$RUN_EC output=$RUN_OUTPUT"
    fi
    if echo "$RUN_OUTPUT" | grep -qF "scripts/wait-for-run.sh 123 --timeout-secs 0"; then
        pass "still running prints the exact command to re-run"
    else
        fail "still running prints the exact command to re-run" "output=$RUN_OUTPUT"
    fi

    run_it "$dir" 123 --timeout-secs 0
    if [[ "$RUN_EC" -eq 0 ]]; then
        pass "re-running resumes and exits 0 once the run succeeds"
    else
        fail "re-running resumes and exits 0 once the run succeeds" "exit=$RUN_EC output=$RUN_OUTPUT"
    fi
    if grep -vq "^run view" "$dir/calls.log"; then
        fail "only ever reads run state (no rerun/cancel)" "calls: $(cat "$dir/calls.log")"
    else
        pass "only ever reads run state (no rerun/cancel)"
    fi
}

test_status_unknown() {
    local dir
    dir=$(make_stub "ERROR")
    run_it "$dir" 123 --poll-interval 0
    if [[ "$RUN_EC" -eq 125 ]] && echo "$RUN_OUTPUT" | grep -q "Bad credentials"; then
        pass "repeated gh failures exit 125 with the error"
    else
        fail "repeated gh failures exit 125 with the error" "exit=$RUN_EC output=$RUN_OUTPUT"
    fi
}

test_usage() {
    local dir
    dir=$(make_stub "completed success")
    run_it "$dir"
    if [[ "$RUN_EC" -eq 1 ]]; then
        pass "missing run id exits 1"
    else
        fail "missing run id exits 1" "exit=$RUN_EC output=$RUN_OUTPUT"
    fi
    run_it "$dir" --help
    if [[ "$RUN_EC" -eq 0 ]] && echo "$RUN_OUTPUT" | grep -q "Usage: scripts/wait-for-run.sh"; then
        pass "--help prints usage"
    else
        fail "--help prints usage" "exit=$RUN_EC output=$RUN_OUTPUT"
    fi
}

test_default_timeout_under_tool_cap
test_success
test_failure
test_still_waiting_then_resume
test_status_unknown
test_usage

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ "$FAILURES" -gt 0 ]]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
