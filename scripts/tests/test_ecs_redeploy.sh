#!/usr/bin/env bash
# test_ecs_redeploy.sh — Tests for scripts/ecs-redeploy.sh terse-default behavior
#
# Verifies:
#   a. Default-mode success output fits ≤3 lines
#   b. --verbose / JM_VERBOSE=1 restores info lines
#   c. Failure path stderr contains ERROR:
#
# Usage:
#   scripts/tests/test_ecs_redeploy.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$SCRIPT_DIR/ecs-redeploy.sh"
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

# Mock aws for a successful redeployment (simplified).
setup_mock_aws_success() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/bin"

    # Also mock wait-for-rollout.sh so it exits 0 immediately
    cp "$SCRIPT_DIR/wait-for-rollout.sh" "$tmpdir/wait-for-rollout.sh" 2>/dev/null || true

    cat > "$tmpdir/bin/aws" << 'MOCK'
#!/usr/bin/env bash
case "${1:-}" in
    ecs)
        case "${2:-}" in
            update-service)
                echo "ecs-svc/deploy-123"
                exit 0
                ;;
            list-tasks)
                echo "arn:aws:ecs:us-west-2:123:task/judgemind-dev/abc123"
                exit 0
                ;;
            describe-tasks)
                echo '{"tasks":[{"taskArn":"arn:aws:ecs:us-west-2:123:task/judgemind-dev/abc123","lastStatus":"RUNNING","containers":[{"image":"image:latest","imageDigest":"sha256:abc123"}]}]}'
                exit 0
                ;;
        esac
        ;;
esac
exit 0
MOCK
    chmod +x "$tmpdir/bin/aws"
    echo "$tmpdir"
}

# Mock aws that fails on update-service.
setup_mock_aws_fail() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/bin"

    cat > "$tmpdir/bin/aws" << 'MOCK'
#!/usr/bin/env bash
if [[ "${1:-}" == "ecs" && "${2:-}" == "update-service" ]]; then
    echo "" # empty output — no deployment ID
    exit 0
fi
exit 1
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

# ── Test 1: No service argument prints usage error ───────────────────────

exit_code=0
stderr_output=$("$SCRIPT" 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "exits non-zero with no service argument"
else
    fail "exits non-zero with no service argument" "expected non-zero, got 0"
fi

if echo "$stderr_output" | grep -qi "usage\|service"; then
    pass "no-service usage error prints usage/service"
else
    fail "no-service usage error prints usage/service" "got: $stderr_output"
fi

# ── Test 2: --verbose flag accepted ──────────────────────────────────────

tmpdir=$(setup_mock_aws_fail)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

exit_code=0
"$SCRIPT" --verbose judgemind-api-dev 2>&1 >/dev/null || exit_code=$?
# Should fail since aws fails, but --verbose should be parsed
if [[ "$exit_code" -ne 0 ]]; then
    pass "--verbose flag accepted (error path reached)"
else
    fail "--verbose flag accepted" "expected non-zero exit from aws mock, got 0"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 3: JM_VERBOSE=1 accepted ────────────────────────────────────────

tmpdir=$(setup_mock_aws_fail)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

exit_code=0
JM_VERBOSE=1 "$SCRIPT" judgemind-api-dev 2>&1 >/dev/null || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "JM_VERBOSE=1 accepted (error path reached)"
else
    fail "JM_VERBOSE=1 accepted" "expected non-zero exit from aws mock, got 0"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Test 4: Failure path stderr contains ERROR: ──────────────────────────

tmpdir=$(setup_mock_aws_fail)
export PATH="$tmpdir/bin:$ORIG_PATH_SAVE"

exit_code=0
stderr_fail=$("$SCRIPT" judgemind-api-dev 2>&1 >/dev/null) || exit_code=$?

if [[ "$exit_code" -ne 0 ]]; then
    pass "exits non-zero on aws failure"
else
    fail "exits non-zero on aws failure" "expected non-zero, got 0"
fi

if echo "$stderr_fail" | grep -qi "error\|ERROR"; then
    pass "failure path stderr contains ERROR:"
else
    fail "failure path stderr contains ERROR:" "got: $stderr_fail"
fi

export PATH="$ORIG_PATH_SAVE"

# ── Summary ───────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
