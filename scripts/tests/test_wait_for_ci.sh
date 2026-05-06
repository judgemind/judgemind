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
#   - `gh pr view ... --json mergeable` → canned status from MERGEABLE_RESPONSE env
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
MERGEABLE_RESPONSE="${MERGEABLE_RESPONSE:-MERGEABLE}"
LOG_FAILED_FILE="${LOG_FAILED_FILE:-}"
RERUN_LOG_FILE="${RERUN_LOG_FILE:-}"

ARGS="$*"

# Route: pr view ... headRefOid — return canned SHA (plain string, like gh -q output).
# Note: order matters — match `mergeStateStatus` before `mergeable` because the
# substring `mergeable` appears within `mergeStateStatus` patterns in some gh
# JSON enum outputs.
case "$ARGS" in
    *"headRefOid"*)
        printf '%s\n' "$SHA_RESPONSE"
        exit 0
        ;;
    *"mergeStateStatus"*)
        printf '%s\n' "$MERGE_STATE_RESPONSE"
        exit 0
        ;;
    *"mergeable"*)
        printf '%s\n' "$MERGEABLE_RESPONSE"
        exit 0
        ;;
    *"run rerun"*)
        # Record the rerun invocation so tests can assert it fired exactly N
        # times. Each call appends a single line: `<run-id> <args>`.
        if [ -n "$RERUN_LOG_FILE" ]; then
            echo "$ARGS" >> "$RERUN_LOG_FILE"
        fi
        exit 0
        ;;
    *"run view"*"--log-failed"*)
        # Emit the canned failed-job log. If LOG_FAILED_FILE is unset or
        # missing, return empty output (which the classifier will label
        # as `real` — i.e. the auto-rerun path is suppressed).
        if [ -n "$LOG_FAILED_FILE" ] && [ -f "$LOG_FAILED_FILE" ]; then
            cat "$LOG_FAILED_FILE"
        fi
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
    MERGEABLE_RESPONSE="${MERGEABLE_RESPONSE:-MERGEABLE}" \
    LOG_FAILED_FILE="${LOG_FAILED_FILE:-}" \
    RERUN_LOG_FILE="${RERUN_LOG_FILE:-}" \
    WAIT_FOR_CI_RERUN_SENTINEL_FILE="${WAIT_FOR_CI_RERUN_SENTINEL_FILE:-$tmpdir/rerun-sentinel}" \
    WAIT_FOR_CI_FLAKE_LOG="${WAIT_FOR_CI_FLAKE_LOG:-$tmpdir/flakes-default.jsonl}" \
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

# Write a check-runs response with one failed check whose details_url uses
# the canonical GitHub Actions URL format. This is what production check-runs
# actually emit and is required for the auto-rerun classifier path (#4148)
# to extract a workflow run-id from the URL.
#
# Args: <file> <check_name> <run_id> [conclusion]
write_failure_with_run_id_response() {
    local file="$1"
    local check_name="${2:-schema-drift-check}"
    local run_id="${3:-25418711047}"
    local conclusion="${4:-failure}"
    cat > "$file" << JSON
{
  "check_runs": [
    {"name": "lint",        "status": "completed", "conclusion": "success",  "details_url": "https://example.com/lint"},
    {"name": "$check_name", "status": "completed", "conclusion": "$conclusion", "details_url": "https://github.com/judgemind/judgemind/actions/runs/$run_id/job/72104555100"}
  ]
}
JSON
}

# Write a check-runs response that simulates the #4069 wedge: ci-passed has
# a completed=success entry, but two unrelated check-runs from a SUPERSEDED
# CI run on the same SHA still show as in_progress. Without the canonical
# fast-path, pending=2 keeps the script polling forever even though the
# canonical merge gate is satisfied.
write_stale_pending_with_success_response() {
    local file="$1"
    cat > "$file" << 'JSON'
{
  "check_runs": [
    {"name": "lint",                "status": "completed",  "conclusion": "success", "details_url": "https://example.com/lint"},
    {"name": "unit-tests",          "status": "completed",  "conclusion": "success", "details_url": "https://example.com/unit"},
    {"name": "ci-passed",           "status": "completed",  "conclusion": "success", "details_url": "https://example.com/ci"},
    {"name": "Vercel-stale",        "status": "in_progress","conclusion": null,      "details_url": "https://example.com/vercel-stale"},
    {"name": "Smoke-Test-stale",    "status": "in_progress","conclusion": null,      "details_url": "https://example.com/smoke-stale"}
  ]
}
JSON
}

# Write a check-runs response where ci-passed is still in_progress (no
# success entry yet) and there are no failures — the script must NOT
# exit via the fast-path, even if mergeable=MERGEABLE.
write_in_progress_ci_passed_response() {
    local file="$1"
    cat > "$file" << 'JSON'
{
  "check_runs": [
    {"name": "lint",       "status": "completed",  "conclusion": "success", "details_url": "https://example.com/lint"},
    {"name": "ci-passed",  "status": "in_progress","conclusion": null,      "details_url": "https://example.com/ci"}
  ]
}
JSON
}

# Write a check-runs response with TWO ci-passed entries (re-run + superseded).
# This exercises the multi-entry jq selector that was the #4069 root cause.
write_double_ci_passed_response() {
    local file="$1"
    cat > "$file" << 'JSON'
{
  "check_runs": [
    {"name": "lint",       "status": "completed", "conclusion": "success", "details_url": "https://example.com/lint"},
    {"name": "ci-passed",  "status": "completed", "conclusion": "success", "details_url": "https://example.com/ci-rerun"},
    {"name": "ci-passed",  "status": "in_progress","conclusion": null,     "details_url": "https://example.com/ci-superseded"}
  ]
}
JSON
}

# ── Tests ──────────────────────────────────────────────────────────────────

# Test 1: Happy path — first poll in-progress, second poll all success + ci-passed,
# then mergeable=MERGEABLE. Expect exit 0 via the canonical-merge-gate fast-path
# (the second-priority all-checks-complete path also satisfies but the fast-path
# fires first when mergeable resolves cleanly).
test_happy_path() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_in_progress_response "$tmpdir/responses/01.json"
    write_all_success_response "$tmpdir/responses/02.json"

    local output exit_code
    exit_code=0
    output=$(MERGE_STATE_RESPONSE=CLEAN MERGEABLE_RESPONSE=MERGEABLE \
        run_script "$tmpdir" 42 --poll-interval 0 --timeout-secs 60 2>&1) || exit_code=$?

    if [ "$exit_code" -eq 0 ]; then
        pass "happy_path: exits 0 on success"
    else
        fail "happy_path: exits 0 on success" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -qE "canonical merge gate green|all checks complete"; then
        pass "happy_path: emits a success message"
    else
        fail "happy_path: emits a success message" "output=$output"
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

# Test 6 (#4069 regression): canonical-merge-gate fast-path. The mock returns
# ci-passed=success + 2 stale in_progress entries (pending=2) on EVERY poll,
# i.e. the same wedge state observed on PR #4059. With mergeable=MERGEABLE,
# the script must exit 0 in well under 60s with the substring
# `canonical merge gate green` on stdout — proving the fast-path bypasses
# the stale pending counts.
test_canonical_gate_fastpath_with_stale_pending() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_stale_pending_with_success_response "$tmpdir/responses/01.json"

    local output exit_code start_ts end_ts elapsed
    exit_code=0
    start_ts=$(date +%s)
    output=$(MERGEABLE_RESPONSE=MERGEABLE MERGE_STATE_RESPONSE=UNSTABLE \
        run_script "$tmpdir" 42 --poll-interval 1 --timeout-secs 30 2>&1) || exit_code=$?
    end_ts=$(date +%s)
    elapsed=$((end_ts - start_ts))

    if [ "$exit_code" -eq 0 ]; then
        pass "canonical_gate_fastpath: exits 0 even with pending=2 from superseded run"
    else
        fail "canonical_gate_fastpath: exits 0 even with pending=2 from superseded run" "exit=$exit_code output=$output"
    fi

    if [ "$elapsed" -lt 10 ]; then
        pass "canonical_gate_fastpath: exits in <10s (actual: ${elapsed}s)"
    else
        fail "canonical_gate_fastpath: exits in <10s" "elapsed=${elapsed}s — fast-path must not wait for pending to drain"
    fi

    if echo "$output" | grep -q "canonical merge gate green"; then
        pass "canonical_gate_fastpath: stdout contains 'canonical merge gate green'"
    else
        fail "canonical_gate_fastpath: stdout contains 'canonical merge gate green'" "output=$output"
    fi
}

# Test 7: All-checks-complete fallback path. When mergeable cannot be resolved
# (UNKNOWN) but pending=0 + ci-passed=success + mergeStateStatus=CLEAN, the
# script must still exit 0 via the fallback path with `all checks complete`.
test_all_checks_complete_path() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_all_success_response "$tmpdir/responses/01.json"

    local output exit_code
    exit_code=0
    output=$(MERGEABLE_RESPONSE=UNKNOWN MERGE_STATE_RESPONSE=CLEAN \
        run_script "$tmpdir" 42 --poll-interval 0 --timeout-secs 30 2>&1) || exit_code=$?

    if [ "$exit_code" -eq 0 ]; then
        pass "all_checks_complete_path: exits 0 when pending=0 and mergeable=UNKNOWN"
    else
        fail "all_checks_complete_path: exits 0 when pending=0 and mergeable=UNKNOWN" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "all checks complete"; then
        pass "all_checks_complete_path: stdout contains 'all checks complete'"
    else
        fail "all_checks_complete_path: stdout contains 'all checks complete'" "output=$output"
    fi

    if echo "$output" | grep -q "canonical merge gate green"; then
        fail "all_checks_complete_path: stdout must NOT contain 'canonical merge gate green' when fast-path was bypassed" "output=$output"
    else
        pass "all_checks_complete_path: stdout does not falsely claim canonical merge gate"
    fi
}

# Test 8 (#4069 regression): ci-passed still in_progress must not trigger
# the fast-path even when mergeable=MERGEABLE. The script must keep polling
# (and ultimately time out under a short --timeout-secs).
test_no_regression_in_progress_ci_passed() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_in_progress_ci_passed_response "$tmpdir/responses/01.json"

    local output exit_code
    exit_code=0
    output=$(MERGEABLE_RESPONSE=MERGEABLE MERGE_STATE_RESPONSE=CLEAN \
        run_script "$tmpdir" 42 --poll-interval 1 --timeout-secs 2 2>&1) || exit_code=$?

    if [ "$exit_code" -eq 2 ]; then
        pass "no_regression_in_progress_ci_passed: times out (exit 2) when ci-passed is still in_progress"
    else
        fail "no_regression_in_progress_ci_passed: times out (exit 2) when ci-passed is still in_progress" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "canonical merge gate green"; then
        fail "no_regression_in_progress_ci_passed: must NOT exit via fast-path when ci-passed is in_progress" "output=$output"
    else
        pass "no_regression_in_progress_ci_passed: does not falsely trigger fast-path"
    fi
}

# Test 9 (#4069 regression): ci-passed has a failure before any success on the
# same SHA. The early-failure path must still fire (exit 1) and the fast-path
# must NOT swallow the failure even with mergeable=MERGEABLE (which GitHub
# would not actually return in this state, but defense-in-depth).
test_no_regression_failure_short_circuits_fastpath() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_failure_response "$tmpdir/responses/01.json" "web-tests"

    local output exit_code
    exit_code=0
    output=$(MERGEABLE_RESPONSE=MERGEABLE MERGE_STATE_RESPONSE=CLEAN \
        run_script "$tmpdir" 42 --poll-interval 0 --timeout-secs 30 2>&1) || exit_code=$?

    if [ "$exit_code" -eq 1 ]; then
        pass "no_regression_failure_short_circuits_fastpath: exits 1 on failure even with mergeable=MERGEABLE"
    else
        fail "no_regression_failure_short_circuits_fastpath: exits 1 on failure even with mergeable=MERGEABLE" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "canonical merge gate green"; then
        fail "no_regression_failure_short_circuits_fastpath: fast-path must not fire when there is any latest failure" "output=$output"
    else
        pass "no_regression_failure_short_circuits_fastpath: fast-path correctly suppressed by failure"
    fi
}

# Test 10 (#4069 root cause): when the filter=latest response contains TWO
# ci-passed entries (re-run + superseded), the success-detection logic must
# treat it as success — not silently break on multi-line jq output. This is
# the exact condition that wedged wait-for-ci.sh on PR #4059.
test_double_ci_passed_entries_resolves_success() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_double_ci_passed_response "$tmpdir/responses/01.json"

    local output exit_code start_ts end_ts elapsed
    exit_code=0
    start_ts=$(date +%s)
    output=$(MERGEABLE_RESPONSE=MERGEABLE \
        run_script "$tmpdir" 42 --poll-interval 1 --timeout-secs 30 2>&1) || exit_code=$?
    end_ts=$(date +%s)
    elapsed=$((end_ts - start_ts))

    if [ "$exit_code" -eq 0 ]; then
        pass "double_ci_passed_entries: exits 0 when one of two ci-passed entries is success"
    else
        fail "double_ci_passed_entries: exits 0 when one of two ci-passed entries is success" "exit=$exit_code output=$output"
    fi

    if [ "$elapsed" -lt 10 ]; then
        pass "double_ci_passed_entries: exits in <10s (actual: ${elapsed}s)"
    else
        fail "double_ci_passed_entries: exits in <10s" "elapsed=${elapsed}s"
    fi
}

# Test 11 (#4148): Auto-rerun on known flake. The mock returns a failed
# `schema-drift-check` job on the first poll (with a parseable run-id in
# `details_url`). The mocked `gh run view --log-failed` returns the canonical
# postgres-startup error string, so the classifier emits `flake/postgres-startup`.
# The script must:
#   - log `flake detected: postgres-startup` on stdout (AC#3),
#   - call `gh run rerun <run-id> --failed` exactly once,
#   - continue polling (the second response is all-success → exit 0).
test_auto_rerun_on_postgres_startup_flake() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_failure_with_run_id_response "$tmpdir/responses/01.json" "schema-drift-check" "25418711047"
    write_all_success_response "$tmpdir/responses/02.json"

    # Canned failed-job log that classifies as flake/postgres-startup.
    local log_file rerun_log
    log_file="$tmpdir/log-failed.txt"
    rerun_log="$tmpdir/rerun.log"
    cat > "$log_file" << 'LOG'
schema-drift-check / check-schema (pull_request) ...
+ docker run -d --name judgemind-schema-check ...
+ docker exec judgemind-schema-check pg_isready
ERROR: postgres failed to start within 30 seconds
##[error]Process completed with exit code 1.
LOG

    local output exit_code
    exit_code=0
    output=$(LOG_FAILED_FILE="$log_file" RERUN_LOG_FILE="$rerun_log" \
        MERGEABLE_RESPONSE=MERGEABLE \
        run_script "$tmpdir" 42 --poll-interval 0 --timeout-secs 60 2>&1) || exit_code=$?

    if [ "$exit_code" -eq 0 ]; then
        pass "auto_rerun_postgres: exits 0 after rerun + success poll"
    else
        fail "auto_rerun_postgres: exits 0 after rerun + success poll" "exit=$exit_code output=$output"
    fi

    # AC#3: stdout must name the matched pattern.
    if echo "$output" | grep -q "flake detected: postgres-startup"; then
        pass "auto_rerun_postgres: stdout names matched pattern"
    else
        fail "auto_rerun_postgres: stdout names matched pattern" "output=$output"
    fi

    # AC#2 first half: rerun fired exactly once.
    local rerun_count=0
    if [ -f "$rerun_log" ]; then
        rerun_count=$(wc -l < "$rerun_log" | tr -d ' ')
    fi
    if [ "$rerun_count" = "1" ]; then
        pass "auto_rerun_postgres: gh run rerun fired exactly once"
    else
        fail "auto_rerun_postgres: gh run rerun fired exactly once" "rerun_count=$rerun_count log=$(cat "$rerun_log" 2>/dev/null || echo none)"
    fi

    # The rerun call must include the parsed run-id.
    if [ -f "$rerun_log" ] && grep -q "25418711047" "$rerun_log"; then
        pass "auto_rerun_postgres: rerun call carries the parsed run-id"
    else
        fail "auto_rerun_postgres: rerun call carries the parsed run-id" "log=$(cat "$rerun_log" 2>/dev/null || echo none)"
    fi
}

# Test 12 (#4148 AC#2 second half): Two consecutive flakes on the same run
# must exit 1 — auto-rerun fires once, then a second flake response on the
# next poll falls through to exit 1.
test_two_consecutive_flakes_exit_1() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    # Both polls return the same flake failure on the same run-id.
    write_failure_with_run_id_response "$tmpdir/responses/01.json" "schema-drift-check" "25418711047"
    write_failure_with_run_id_response "$tmpdir/responses/02.json" "schema-drift-check" "25418711047"

    local log_file rerun_log
    log_file="$tmpdir/log-failed.txt"
    rerun_log="$tmpdir/rerun.log"
    cat > "$log_file" << 'LOG'
ERROR: postgres failed to start within 30 seconds
LOG

    local output exit_code
    exit_code=0
    output=$(LOG_FAILED_FILE="$log_file" RERUN_LOG_FILE="$rerun_log" \
        MERGEABLE_RESPONSE=MERGEABLE \
        run_script "$tmpdir" 42 --poll-interval 0 --timeout-secs 60 2>&1) || exit_code=$?

    if [ "$exit_code" -eq 1 ]; then
        pass "two_consecutive_flakes: exits 1 on second flake"
    else
        fail "two_consecutive_flakes: exits 1 on second flake" "exit=$exit_code output=$output"
    fi

    # AC#2: rerun fired exactly once total — never twice.
    local rerun_count=0
    if [ -f "$rerun_log" ]; then
        rerun_count=$(wc -l < "$rerun_log" | tr -d ' ')
    fi
    if [ "$rerun_count" = "1" ]; then
        pass "two_consecutive_flakes: rerun fired exactly once total"
    else
        fail "two_consecutive_flakes: rerun fired exactly once total" "rerun_count=$rerun_count"
    fi

    # The "rerun already fired" message should appear in output.
    if echo "$output" | grep -q "rerun already fired"; then
        pass "two_consecutive_flakes: stderr explains the rerun-already-fired condition"
    else
        fail "two_consecutive_flakes: stderr explains the rerun-already-fired condition" "output=$output"
    fi
}

# Test 13 (#4148): Real (non-flake) failure must NOT trigger auto-rerun.
# The failed job log here has no flake-pattern string, so the classifier
# emits `real` and the script falls through to exit 1 with no rerun.
test_real_failure_no_rerun() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_failure_with_run_id_response "$tmpdir/responses/01.json" "unit-tests" "12345678"

    local log_file rerun_log
    log_file="$tmpdir/log-failed.txt"
    rerun_log="$tmpdir/rerun.log"
    cat > "$log_file" << 'LOG'
test_foo (TestBar) ... FAIL
AssertionError: expected 1 got 2
ran 17 tests in 0.234s
FAILED (failures=1)
LOG

    local output exit_code
    exit_code=0
    output=$(LOG_FAILED_FILE="$log_file" RERUN_LOG_FILE="$rerun_log" \
        run_script "$tmpdir" 42 --poll-interval 0 --timeout-secs 60 2>&1) || exit_code=$?

    if [ "$exit_code" -eq 1 ]; then
        pass "real_failure_no_rerun: exits 1 on real failure"
    else
        fail "real_failure_no_rerun: exits 1 on real failure" "exit=$exit_code output=$output"
    fi

    if [ ! -f "$rerun_log" ] || [ ! -s "$rerun_log" ]; then
        pass "real_failure_no_rerun: gh run rerun was NOT called"
    else
        fail "real_failure_no_rerun: gh run rerun was NOT called" "rerun_log=$(cat "$rerun_log")"
    fi

    if echo "$output" | grep -q "flake detected"; then
        fail "real_failure_no_rerun: must NOT log 'flake detected' on real failure" "output=$output"
    else
        pass "real_failure_no_rerun: does not falsely claim flake"
    fi
}

# Test 14b (#4163): When auto-rerun fires for a known flake, exactly one
# JSONL line is appended to WAIT_FOR_CI_FLAKE_LOG with the documented schema:
# {ts, pr, sha, run_id, label, check_name}. Aggregators downstream depend on
# the schema being stable.
test_flake_telemetry_emits_jsonl_line() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_failure_with_run_id_response "$tmpdir/responses/01.json" "schema-drift-check" "25418711047"
    write_all_success_response "$tmpdir/responses/02.json"

    local log_file rerun_log flake_log
    log_file="$tmpdir/log-failed.txt"
    rerun_log="$tmpdir/rerun.log"
    flake_log="$tmpdir/flakes.jsonl"
    cat > "$log_file" << 'LOG'
ERROR: postgres failed to start within 30 seconds
LOG

    local output exit_code
    exit_code=0
    output=$(LOG_FAILED_FILE="$log_file" RERUN_LOG_FILE="$rerun_log" \
        WAIT_FOR_CI_FLAKE_LOG="$flake_log" \
        SHA_RESPONSE="75b03030deadbeef" \
        MERGEABLE_RESPONSE=MERGEABLE \
        run_script "$tmpdir" 4162 --poll-interval 0 --timeout-secs 60 2>&1) || exit_code=$?

    if [ "$exit_code" -eq 0 ]; then
        pass "flake_telemetry: exits 0 after rerun + success poll"
    else
        fail "flake_telemetry: exits 0 after rerun + success poll" "exit=$exit_code output=$output"
    fi

    # AC#1: a single JSONL line with the expected fields.
    if [ ! -f "$flake_log" ]; then
        fail "flake_telemetry: jsonl log file exists" "expected at $flake_log"
        return
    fi

    local line_count
    line_count=$(wc -l < "$flake_log" | tr -d ' ')
    if [ "$line_count" = "1" ]; then
        pass "flake_telemetry: exactly one JSONL line written"
    else
        fail "flake_telemetry: exactly one JSONL line written" "line_count=$line_count contents=$(cat "$flake_log")"
    fi

    # The line must be valid JSON.
    if jq -e . "$flake_log" >/dev/null 2>&1; then
        pass "flake_telemetry: line is valid JSON"
    else
        fail "flake_telemetry: line is valid JSON" "contents=$(cat "$flake_log")"
    fi

    # Schema fields — assert each is present with the expected value.
    local label pr_field run_id_field sha_field check_name ts_field
    label=$(jq -r '.label' "$flake_log")
    pr_field=$(jq -r '.pr' "$flake_log")
    run_id_field=$(jq -r '.run_id' "$flake_log")
    sha_field=$(jq -r '.sha' "$flake_log")
    check_name=$(jq -r '.check_name' "$flake_log")
    ts_field=$(jq -r '.ts' "$flake_log")

    if [ "$label" = "postgres-startup" ]; then
        pass "flake_telemetry: label=postgres-startup"
    else
        fail "flake_telemetry: label=postgres-startup" "got: $label"
    fi

    if [ "$pr_field" = "4162" ]; then
        pass "flake_telemetry: pr=4162 (numeric, not quoted)"
    else
        fail "flake_telemetry: pr=4162" "got: $pr_field"
    fi

    if [ "$run_id_field" = "25418711047" ]; then
        pass "flake_telemetry: run_id=25418711047 (numeric, not quoted)"
    else
        fail "flake_telemetry: run_id=25418711047" "got: $run_id_field"
    fi

    if [ "$sha_field" = "75b03030" ]; then
        pass "flake_telemetry: sha=75b03030 (8-char short sha)"
    else
        fail "flake_telemetry: sha=75b03030" "got: $sha_field"
    fi

    if [ "$check_name" = "schema-drift-check" ]; then
        pass "flake_telemetry: check_name=schema-drift-check"
    else
        fail "flake_telemetry: check_name=schema-drift-check" "got: $check_name"
    fi

    # ts must be ISO-8601 UTC with trailing Z.
    if echo "$ts_field" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'; then
        pass "flake_telemetry: ts is ISO-8601 UTC (suffix Z)"
    else
        fail "flake_telemetry: ts is ISO-8601 UTC" "got: $ts_field"
    fi
}

# Test 14c (#4163 AC#3): Two flake events on different runs aggregate via the
# documented one-liner `jq -r .label | sort | uniq -c`. This guards the
# "no free-form prose" / "same fields every time" property.
test_flake_telemetry_aggregatable() {
    local tmpdir
    tmpdir=$(make_temp_dir)

    # Pre-seed the JSONL with two prior events, then the test produces a third.
    local flake_log="$tmpdir/flakes.jsonl"
    cat > "$flake_log" << 'JSONL'
{"ts":"2026-05-04T01:02:03Z","pr":4100,"sha":"aaaaaaaa","run_id":99999,"label":"postgres-startup","check_name":"schema-drift-check"}
{"ts":"2026-05-04T02:03:04Z","pr":4101,"sha":"bbbbbbbb","run_id":99998,"label":"postgres-startup","check_name":"schema-drift-check"}
JSONL

    write_failure_with_run_id_response "$tmpdir/responses/01.json" "unit-tests" "12345678"
    write_all_success_response "$tmpdir/responses/02.json"

    local log_file rerun_log
    log_file="$tmpdir/log-failed.txt"
    rerun_log="$tmpdir/rerun.log"
    cat > "$log_file" << 'LOG'
Cannot connect to the Docker daemon at unix:///var/run/docker.sock
LOG

    local exit_code
    exit_code=0
    LOG_FAILED_FILE="$log_file" RERUN_LOG_FILE="$rerun_log" \
        WAIT_FOR_CI_FLAKE_LOG="$flake_log" \
        SHA_RESPONSE="cccccccc" \
        MERGEABLE_RESPONSE=MERGEABLE \
        run_script "$tmpdir" 4163 --poll-interval 0 --timeout-secs 60 >/dev/null 2>&1 || exit_code=$?

    if [ "$exit_code" -ne 0 ]; then
        fail "flake_telemetry_aggregatable: rerun + success poll exits 0" "exit=$exit_code"
        return
    fi

    # AC#3: aggregator one-liner.
    local agg
    agg=$(jq -r .label "$flake_log" | sort | uniq -c | tr -s ' ' | sed 's/^ *//')
    local expected
    expected="1 docker-daemon
2 postgres-startup"
    if [ "$agg" = "$expected" ]; then
        pass "flake_telemetry_aggregatable: jq -r .label | sort | uniq -c yields the leaderboard"
    else
        fail "flake_telemetry_aggregatable: jq -r .label | sort | uniq -c yields the leaderboard" "got: $agg"
    fi
}

# Test 14 (#4148): --no-auto-rerun must disable the classifier path entirely
# even when a flake pattern is present in the job log.
test_no_auto_rerun_flag_disables_classifier() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_failure_with_run_id_response "$tmpdir/responses/01.json" "schema-drift-check" "25418711047"

    local log_file rerun_log
    log_file="$tmpdir/log-failed.txt"
    rerun_log="$tmpdir/rerun.log"
    cat > "$log_file" << 'LOG'
ERROR: postgres failed to start within 30 seconds
LOG

    local output exit_code
    exit_code=0
    output=$(LOG_FAILED_FILE="$log_file" RERUN_LOG_FILE="$rerun_log" \
        run_script "$tmpdir" 42 --no-auto-rerun --poll-interval 0 --timeout-secs 60 2>&1) || exit_code=$?

    if [ "$exit_code" -eq 1 ]; then
        pass "no_auto_rerun_flag: exits 1 with --no-auto-rerun"
    else
        fail "no_auto_rerun_flag: exits 1 with --no-auto-rerun" "exit=$exit_code output=$output"
    fi

    if [ ! -f "$rerun_log" ] || [ ! -s "$rerun_log" ]; then
        pass "no_auto_rerun_flag: gh run rerun was NOT called"
    else
        fail "no_auto_rerun_flag: gh run rerun was NOT called" "rerun_log=$(cat "$rerun_log")"
    fi
}

# ── Run all tests ──────────────────────────────────────────────────────────

test_happy_path
test_early_failure
test_timeout
test_missing_pr_arg
test_help
test_canonical_gate_fastpath_with_stale_pending
test_all_checks_complete_path
test_no_regression_in_progress_ci_passed
test_no_regression_failure_short_circuits_fastpath
test_double_ci_passed_entries_resolves_success
test_auto_rerun_on_postgres_startup_flake
test_two_consecutive_flakes_exit_1
test_real_failure_no_rerun
test_no_auto_rerun_flag_disables_classifier
test_flake_telemetry_emits_jsonl_line
test_flake_telemetry_aggregatable

echo ""
echo "────────────────────────────────────────────"
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [ "$FAILURES" -gt 0 ]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
