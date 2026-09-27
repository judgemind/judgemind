#!/usr/bin/env bash
# test_ecs_redeploy.sh — Unit tests for scripts/ecs-redeploy.sh (#4835)
#
# Runs ecs-redeploy.sh (and the real wait-for-rollout.sh it calls) from a
# sandbox repo root against a stateful `aws` stub.  Verifies:
#   - the default wait fits under the Bash tool's 600s cap
#   - a deployment still rolling out when the wait ends exits 124 and prints
#     the `--deployment-id` command that keeps waiting
#   - re-running with `--deployment-id` resumes the wait on the same
#     deployment without forcing another one (update-service runs once)
#   - describe-services failing exits 125 (rollout state unknown)
#
# Usage:
#   scripts/tests/test_ecs_redeploy.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REDEPLOY="$SCRIPT_DIR/ecs-redeploy.sh"

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

# make_sandbox — echo a new sandbox root with the scripts and an aws stub.
# Stub behavior (env):
#   MOCK_STATE              directory for call log + describe counter
#   MOCK_COMPLETE_AFTER=N   describe-services reports IN_PROGRESS N times,
#                           then COMPLETED with running==desired==1
#   MOCK_DESCRIBE_FAIL=1    describe-services always fails
make_sandbox() {
    local root
    root=$(mktemp -d)
    TEMP_DIRS+=("$root")
    mkdir -p "$root/bin" "$root/scripts" "$root/state"
    cp "$REDEPLOY" "$root/scripts/ecs-redeploy.sh"
    cp "$SCRIPT_DIR/wait-for-rollout.sh" "$root/scripts/wait-for-rollout.sh"
    cat > "$root/bin/aws" << 'AWS_EOF'
#!/usr/bin/env bash
state="${MOCK_STATE:?}"
echo "$*" >> "$state/calls.log"
case "$1 $2" in
    "ecs update-service")
        echo "ecs-svc/new-deploy-1"
        ;;
    "ecs describe-services")
        if [[ "${MOCK_DESCRIBE_FAIL:-0}" == "1" ]]; then
            echo "An error occurred (ExpiredTokenException): token expired" >&2
            exit 255
        fi
        n=$(cat "$state/describe.count" 2>/dev/null || echo 0)
        n=$((n + 1)); echo "$n" > "$state/describe.count"
        if [[ "$n" -gt "${MOCK_COMPLETE_AFTER:-0}" ]]; then
            state_name="COMPLETED"; running=1
        else
            state_name="IN_PROGRESS"; running=0
        fi
        printf '{"services":[{"serviceName":"svc","desiredCount":1,"runningCount":%s,"pendingCount":0,"events":[],"deployments":[{"id":"ecs-svc/new-deploy-1","taskDefinition":"arn:td:1","rolloutState":"%s","desiredCount":1,"runningCount":%s,"pendingCount":0}]}]}\n' \
            "$running" "$state_name" "$running"
        ;;
    "ecs list-tasks")
        echo ""
        ;;
    *)
        echo "{}"
        ;;
esac
exit 0
AWS_EOF
    chmod +x "$root/bin/aws"
    echo "$root"
}

# run_redeploy <root> [args...] — run the sandboxed ecs-redeploy.sh; sets
# RUN_OUTPUT / RUN_EC.
run_redeploy() {
    local root="$1"
    shift
    set +e
    RUN_OUTPUT=$(PATH="$root/bin:$PATH" MOCK_STATE="$root/state" \
        ROLLOUT_POLL_INTERVAL=0 \
        "$root/scripts/ecs-redeploy.sh" "$@" 2>&1)
    RUN_EC=$?
    set -e
}

# ── Test 1: default wait is under the tool cap ─────────────────────────────

test_default_wait_under_tool_cap() {
    local default
    default=$(grep -oE 'ROLLOUT_TIMEOUT_SECS:-[0-9]+' "$REDEPLOY" | head -n 1 | cut -d- -f2)
    if [[ -n "$default" && "$default" -le 540 ]]; then
        pass "default ROLLOUT_TIMEOUT_SECS ($default s) is <= 540s"
    else
        fail "default ROLLOUT_TIMEOUT_SECS ($default s) is <= 540s"
    fi
}

# ── Test 2: still rolling out → 124, then --deployment-id resumes ──────────

test_still_rolling_out_then_reattach() {
    local root
    root=$(make_sandbox)

    # A 0s budget stops after the first IN_PROGRESS poll.
    MOCK_COMPLETE_AFTER=1 ROLLOUT_TIMEOUT_SECS=0 run_redeploy "$root" my-svc my-cluster
    if [[ "$RUN_EC" -eq 124 ]]; then
        pass "still rolling out exits 124 (not a failure)"
    else
        fail "still rolling out exits 124 (not a failure)" "exit=$RUN_EC output=$RUN_OUTPUT"
    fi
    if echo "$RUN_OUTPUT" | grep -qF "scripts/ecs-redeploy.sh my-svc my-cluster --deployment-id ecs-svc/new-deploy-1"; then
        pass "prints the --deployment-id command that keeps waiting"
    else
        fail "prints the --deployment-id command that keeps waiting" "output=$RUN_OUTPUT"
    fi
    if echo "$RUN_OUTPUT" | grep -q "Do NOT re-run the plain command"; then
        pass "says not to re-run the plain command"
    else
        fail "says not to re-run the plain command" "output=$RUN_OUTPUT"
    fi

    MOCK_COMPLETE_AFTER=1 ROLLOUT_TIMEOUT_SECS=0 \
        run_redeploy "$root" my-svc my-cluster --deployment-id ecs-svc/new-deploy-1
    if [[ "$RUN_EC" -eq 0 ]]; then
        pass "--deployment-id resumes the wait and exits 0 once COMPLETED"
    else
        fail "--deployment-id resumes the wait and exits 0 once COMPLETED" "exit=$RUN_EC output=$RUN_OUTPUT"
    fi

    local deploys
    deploys=$(grep -c "^ecs update-service" "$root/state/calls.log" || true)
    if [[ "$deploys" == "1" ]]; then
        pass "only one deployment was forced across both calls"
    else
        fail "only one deployment was forced across both calls" "update-service calls: $deploys"
    fi
}

# ── Test 3: describe-services failing → 125 ────────────────────────────────

test_describe_failure_exits_125() {
    local root
    root=$(make_sandbox)
    MOCK_DESCRIBE_FAIL=1 ROLLOUT_TIMEOUT_SECS=5 run_redeploy "$root" my-svc
    if [[ "$RUN_EC" -eq 125 ]]; then
        pass "describe-services failure exits 125 (state unknown)"
    else
        fail "describe-services failure exits 125 (state unknown)" "exit=$RUN_EC output=$RUN_OUTPUT"
    fi
    if echo "$RUN_OUTPUT" | grep -qF -- "--deployment-id ecs-svc/new-deploy-1"; then
        pass "status-unknown path prints the re-attach command"
    else
        fail "status-unknown path prints the re-attach command" "output=$RUN_OUTPUT"
    fi
}

# ── Test 4: usage errors ───────────────────────────────────────────────────

test_usage_errors() {
    local root
    root=$(make_sandbox)
    run_redeploy "$root"
    if [[ "$RUN_EC" -eq 1 ]] && echo "$RUN_OUTPUT" | grep -q "Usage:"; then
        pass "missing service exits 1 with usage"
    else
        fail "missing service exits 1 with usage" "exit=$RUN_EC output=$RUN_OUTPUT"
    fi
    run_redeploy "$root" my-svc --deployment-id
    if [[ "$RUN_EC" -eq 1 ]]; then
        pass "--deployment-id without a value exits 1"
    else
        fail "--deployment-id without a value exits 1" "exit=$RUN_EC output=$RUN_OUTPUT"
    fi
}

test_default_wait_under_tool_cap
test_still_rolling_out_then_reattach
test_describe_failure_exits_125
test_usage_errors

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ "$FAILURES" -gt 0 ]]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
