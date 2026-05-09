#!/usr/bin/env bash
# test_worker_status.sh — Regression test for the CI-status column of
# scripts/worker-status.sh (#4417).
#
# Pre-#4417 the dashboard parsed the rollup with an inline awk regex
# whose vocabulary diverged from phase_transitions._ci_rollup_state on
# STALE handling and required parallel fixes for #4407 / #4414.  After
# #4417 the column delegates to scripts/dispatcher/ci_classifier_cli.py
# (the canonical Python rule) and maps green→green, red→failed,
# pending→running, error→pending.
#
# This test feeds the four canonical fixtures called out in the issue
# body straight into the CLI and asserts the verdict + the expected
# worker-status column mapping.  Driving the whole worker-status.sh
# end-to-end would require mocking ``gh`` + ``git worktree`` so the
# test instead exercises the load-bearing classifier delegation
# (where the bug class actually lived) — same coverage at a fraction
# of the harness complexity.
#
# Usage:
#   scripts/tests/test_worker_status.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CLI="$REPO_ROOT/scripts/dispatcher/ci_classifier_cli.py"
FAILURES=0
TESTS=0

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

if [[ ! -f "$CLI" ]]; then
    echo "FAIL: CLI not found at $CLI" >&2
    exit 1
fi

# ── Helper: feed JSON to CLI, get verdict; map to worker-status column ──

cli_verdict() {
    # $1 = JSON payload as a string
    printf '%s' "$1" | python3 "$CLI" 2>/dev/null
}

map_to_column() {
    # Mirrors the case statement in scripts/worker-status.sh.
    case "$1" in
        green) printf 'green' ;;
        red) printf 'failed' ;;
        pending) printf 'running' ;;
        *) printf 'pending' ;;
    esac
}

assert_column() {
    # $1 = test name, $2 = JSON payload, $3 = expected verdict, $4 = expected column
    local name="$1"
    local payload="$2"
    local expected_verdict="$3"
    local expected_column="$4"

    local got_verdict
    got_verdict=$(cli_verdict "$payload")
    local got_column
    got_column=$(map_to_column "$got_verdict")

    if [[ "$got_verdict" != "$expected_verdict" ]]; then
        fail "$name (verdict)" "expected $expected_verdict, got $got_verdict"
        return
    fi
    if [[ "$got_column" != "$expected_column" ]]; then
        fail "$name (column)" "expected $expected_column, got $got_column"
        return
    fi
    pass "$name (verdict=$got_verdict, column=$got_column)"
}

# ── Canonical fixtures ──────────────────────────────────────────────────

# 1) CANCELLED + green check + MERGEABLE/CLEAN  →  green / green
assert_column "cancelled_plus_green" \
    '{"statusCheckRollup":[
        {"__typename":"CheckRun","status":"COMPLETED","conclusion":"SUCCESS","name":"ci-passed"},
        {"__typename":"CheckRun","status":"COMPLETED","conclusion":"CANCELLED","name":"deploy-vercel"}
    ],"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}' \
    "green" "green"

# 2) FAILURE  →  red / failed
assert_column "failure_only" \
    '{"statusCheckRollup":[
        {"__typename":"CheckRun","status":"COMPLETED","conclusion":"FAILURE","name":"tests"}
    ],"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}' \
    "red" "failed"

# 3) FAILURE + CANCELLED  →  red / failed
assert_column "failure_plus_cancelled" \
    '{"statusCheckRollup":[
        {"__typename":"CheckRun","status":"COMPLETED","conclusion":"FAILURE","name":"tests"},
        {"__typename":"CheckRun","status":"COMPLETED","conclusion":"CANCELLED","name":"deploy-vercel"}
    ],"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}' \
    "red" "failed"

# 4) CANCELLED-only + MERGEABLE/CLEAN  →  green / green
assert_column "cancelled_only_mergeable" \
    '{"statusCheckRollup":[
        {"__typename":"CheckRun","status":"COMPLETED","conclusion":"CANCELLED","name":"deploy-vercel"}
    ],"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}' \
    "green" "green"

# 5) IN_PROGRESS  →  pending / running
assert_column "pending_in_progress" \
    '{"statusCheckRollup":[
        {"__typename":"CheckRun","status":"IN_PROGRESS","conclusion":"","name":"tests"}
    ],"mergeable":"UNKNOWN","mergeStateStatus":"UNKNOWN"}' \
    "pending" "running"

# 6) DIRTY  →  red / failed
assert_column "dirty_conflicting" \
    '{"statusCheckRollup":[
        {"__typename":"CheckRun","status":"COMPLETED","conclusion":"SUCCESS","name":"ci-passed"}
    ],"mergeable":"CONFLICTING","mergeStateStatus":"DIRTY"}' \
    "red" "failed"

# 7) UNSTABLE  →  pending / running
assert_column "unstable_recompute" \
    '{"statusCheckRollup":[
        {"__typename":"CheckRun","status":"COMPLETED","conclusion":"SUCCESS","name":"ci-passed"}
    ],"mergeable":"MERGEABLE","mergeStateStatus":"UNSTABLE"}' \
    "pending" "running"

# 8) Empty input  →  error / pending (mapped fallback)
got=$(printf '' | python3 "$CLI" 2>/dev/null)
mapped=$(map_to_column "$got")
if [[ "$got" == "error" && "$mapped" == "pending" ]]; then
    pass "empty_input_falls_back_to_pending"
else
    fail "empty_input_falls_back_to_pending" \
        "expected verdict=error, column=pending; got verdict=$got column=$mapped"
fi

# ── Summary ─────────────────────────────────────────────────────────────

echo ""
echo "Ran $TESTS tests, $FAILURES failed."
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
