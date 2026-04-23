#!/usr/bin/env bash
# run-tests.sh — Run ECS oneshot integration tests in Docker.
#
# Builds a container image that simulates the ECS oneshot environment,
# then runs test scripts inside it using the same delivery mechanism
# as ecs-run-task.sh (base64 inline or direct copy).
#
# Usage:
#   .github/ecs-oneshot-test/run-tests.sh
#
# Exit code: 0 if all tests pass, 1 if any test fails.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
IMAGE_NAME="judgemind-ecs-oneshot-test"
TEST_SCRIPTS_DIR="$SCRIPT_DIR/test-scripts"

# Track pass/fail counts
PASS=0
FAIL=0
FAILURES=()

# ─── Build the test image ────────────────────────────────────────────────────

echo "=== Building ECS oneshot test image ==="
docker build \
    -f "$SCRIPT_DIR/Dockerfile" \
    -t "$IMAGE_NAME" \
    "$REPO_ROOT"

echo "Image built successfully."
echo ""

# ─── Helper: run a script inside the container ──────────────────────────────
#
# Simulates the ecs-run-task.sh delivery mechanism:
#   1. Base64-encode the script, decode inside the container, and run it
#
# This matches the delivery logic in ecs-run-task.sh.

run_test() {
    local test_name="$1"
    local script_path="$2"
    shift 2
    local script_args=("$@")

    echo "--- Test: $test_name ---"

    # Base64-encode the script (same as ecs-run-task.sh for small scripts)
    local encoded
    # Strip newlines: Linux base64 wraps at 76 chars, which breaks bash -c
    encoded=$(base64 < "$script_path" | tr -d '\n')

    # Build the command string matching ecs-run-task.sh's pattern
    local args_str=""
    for arg in "${script_args[@]+"${script_args[@]}"}"; do
        local escaped
        escaped=$(printf '%s' "$arg" | sed "s/'/'\\\\''/g")
        args_str="${args_str} '${escaped}'"
    done

    local cmd_str="echo ${encoded} | base64 -d > /tmp/_oneshot_script && python3 /tmp/_oneshot_script${args_str}"

    if docker run --rm "$IMAGE_NAME" "$cmd_str" 2>&1; then
        echo "PASS"
        PASS=$((PASS + 1))
    else
        echo "FAIL (exit code: $?)"
        FAIL=$((FAIL + 1))
        FAILURES+=("$test_name")
    fi
    echo ""
}

# ─── Run test scripts ────────────────────────────────────────────────────────

echo "=== Running ECS oneshot integration tests ==="
echo ""

run_test "import common dependencies" \
    "$TEST_SCRIPTS_DIR/test_imports.py"

run_test "framework internal imports" \
    "$TEST_SCRIPTS_DIR/test_framework_imports.py"

run_test "script argument passthrough" \
    "$TEST_SCRIPTS_DIR/test_script_args.py" \
    --dry-run --county "Los Angeles" --limit 10

# ─── Summary ──────────────────────────────────────────────────────────────────

echo "=== Results ==="
echo "Passed: $PASS"
echo "Failed: $FAIL"

if [[ $FAIL -gt 0 ]]; then
    echo ""
    echo "Failed tests:"
    for name in "${FAILURES[@]}"; do
        echo "  - $name"
    done
    exit 1
fi

echo ""
echo "All tests passed."
