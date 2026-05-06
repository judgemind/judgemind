#!/usr/bin/env bash
# test_check_terraform_ecs_entrypoint.sh -- Tests for
# check-terraform-ecs-entrypoint.sh / check_tf_ecs_entrypoint.py.
#
# Issue #4270 -- the silent zero-record-check task-def breakage caused
# by `command = ["python3", "..."]` against a scraper image whose
# Dockerfile ENTRYPOINT is ["python", "-m"].
#
# Usage:
#   scripts/tests/test_check_terraform_ecs_entrypoint.sh
#
# Exit codes:
#   0 -- All tests passed.
#   1 -- One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-terraform-ecs-entrypoint.sh"
PYTHON_HELPER="$SCRIPT_DIR/check_tf_ecs_entrypoint.py"
FIXTURES_DIR="$SCRIPT_DIR/tests/fixtures"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# Test-suite helpers required by _guard_self_match_helpers.sh
assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"
    TMPDIR_TEST=$(mktemp -d)
}

assert_py_fails() {
    local desc="$1"
    local tf_file="$2"
    local allowlist="${3:-}"
    TESTS=$((TESTS + 1))
    local passed=false
    if [[ -n "$allowlist" ]]; then
        if python3 "$PYTHON_HELPER" "$tf_file" "$allowlist" > /dev/null 2>&1; then
            passed=true
        fi
    else
        if python3 "$PYTHON_HELPER" "$tf_file" > /dev/null 2>&1; then
            passed=true
        fi
    fi
    if "$passed"; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_py_passes() {
    local desc="$1"
    local tf_file="$2"
    local allowlist="${3:-}"
    TESTS=$((TESTS + 1))
    local passed=false
    if [[ -n "$allowlist" ]]; then
        if python3 "$PYTHON_HELPER" "$tf_file" "$allowlist" > /dev/null 2>&1; then
            passed=true
        fi
    else
        if python3 "$PYTHON_HELPER" "$tf_file" > /dev/null 2>&1; then
            passed=true
        fi
    fi
    if "$passed"; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

assert_py_output_contains() {
    local desc="$1"
    local tf_file="$2"
    local pattern="$3"
    TESTS=$((TESTS + 1))
    local out
    out=$(python3 "$PYTHON_HELPER" "$tf_file" 2>&1 || true)
    if echo "$out" | grep -qE "$pattern"; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (pattern '$pattern' not found in output: $out)"
        FAILURES=$((FAILURES + 1))
    fi
}

# Test 1: Bad fixture (the #4270 shape) is flagged
assert_py_fails "bad fixture exits non-zero" "$FIXTURES_DIR/tf-ecs-entrypoint-bad.tf"

# Test 2: Bad fixture output names the resource and the interpreter
assert_py_output_contains \
    "bad fixture output names resource and interpreter" \
    "$FIXTURES_DIR/tf-ecs-entrypoint-bad.tf" \
    'aws_ecs_task_definition "bad_zero_record".*"python3"'

# Test 3: Bad fixture output includes line number
assert_py_output_contains \
    "bad fixture output contains :line: format" \
    "$FIXTURES_DIR/tf-ecs-entrypoint-bad.tf" \
    ":[0-9]+:"

# Test 4: Good fixture (with entryPoint override) passes
assert_py_passes "good fixture exits 0" "$FIXTURES_DIR/tf-ecs-entrypoint-good.tf"

# Test 5: Inline fixture -- bash interpreter without entryPoint is flagged
cat > "$TMPDIR_TEST/bash_no_entrypoint.tf" << 'TFEOF'
resource "aws_ecs_task_definition" "bash_bad" {
  family       = "judgemind-bash-bad"
  cpu          = 256
  memory       = 512
  network_mode = "awsvpc"
  container_definitions = jsonencode([
    {
      name      = "bash-bad"
      image     = "fake/img:latest"
      command   = ["bash", "-c", "echo hi"]
      essential = true
    }
  ])
}
TFEOF
assert_py_fails "bash interpreter without entryPoint is flagged" "$TMPDIR_TEST/bash_no_entrypoint.tf"

# Test 6: Inline fixture -- node interpreter without entryPoint is flagged
cat > "$TMPDIR_TEST/node_no_entrypoint.tf" << 'TFEOF'
resource "aws_ecs_task_definition" "node_bad" {
  family       = "judgemind-node-bad"
  cpu          = 256
  memory       = 512
  network_mode = "awsvpc"
  container_definitions = jsonencode([
    {
      name      = "node-bad"
      image     = "fake/img:latest"
      command   = ["node", "server.js"]
      essential = true
    }
  ])
}
TFEOF
assert_py_fails "node interpreter without entryPoint is flagged" "$TMPDIR_TEST/node_no_entrypoint.tf"

# Test 7: Inline fixture -- non-interpreter command is NOT flagged
# (e.g. an executable that takes its own argv directly).
cat > "$TMPDIR_TEST/non_interpreter.tf" << 'TFEOF'
resource "aws_ecs_task_definition" "non_interpreter" {
  family       = "judgemind-non-interpreter"
  cpu          = 256
  memory       = 512
  network_mode = "awsvpc"
  container_definitions = jsonencode([
    {
      name      = "non-interpreter"
      image     = "fake/img:latest"
      command   = ["framework", "--args"]
      essential = true
    }
  ])
}
TFEOF
assert_py_passes "non-interpreter first arg is not flagged" "$TMPDIR_TEST/non_interpreter.tf"

# Test 8: Inline fixture -- task def with entryPoint override passes even
# when command starts with an interpreter.
cat > "$TMPDIR_TEST/with_entrypoint.tf" << 'TFEOF'
resource "aws_ecs_task_definition" "with_entrypoint" {
  family       = "judgemind-with-entrypoint"
  cpu          = 256
  memory       = 512
  network_mode = "awsvpc"
  container_definitions = jsonencode([
    {
      name       = "with-entrypoint"
      image      = "fake/img:latest"
      entryPoint = ["python3"]
      command    = ["python", "scripts/foo.py"]
      essential  = true
    }
  ])
}
TFEOF
assert_py_passes "task def with entryPoint passes" "$TMPDIR_TEST/with_entrypoint.tf"

# Test 9: Allowlist suppression works
ALLOWLIST_FILE="$TMPDIR_TEST/test_allowlist.txt"
printf '%s\n' "$TMPDIR_TEST/bash_no_entrypoint.tf:bash_bad:bash-bad" > "$ALLOWLIST_FILE"
assert_py_passes \
    "allowlist suppression works" \
    "$TMPDIR_TEST/bash_no_entrypoint.tf" \
    "$ALLOWLIST_FILE"

# Test 10: --list flag prints expected paths and exits 0
TESTS=$((TESTS + 1))
list_output=$("$CHECK_SCRIPT" --list 2>&1 || true)
if echo "$list_output" | grep -q "main.tf"; then
    echo "PASS: --list flag prints .tf files and exits 0"
else
    echo "FAIL: --list flag did not print any main.tf files (output: $list_output)"
    FAILURES=$((FAILURES + 1))
fi

# Test 11: --list includes modules paths
TESTS=$((TESTS + 1))
if echo "$list_output" | grep -q "infra/terraform/modules"; then
    echo "PASS: --list includes modules path"
else
    echo "FAIL: --list missing modules path"
    echo "  Output was: $list_output"
    FAILURES=$((FAILURES + 1))
fi

# Test 12: Real repo scan exits 0 (zero-record-check fix landed,
# dispatcher-v3 task defs are allowlisted)
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" > /dev/null 2>&1; then
    echo "PASS: real repo scan exits 0 (allowlist + fix complete)"
else
    echo "FAIL: real repo scan exits non-zero -- allowlist may be incomplete"
    FAILURES=$((FAILURES + 1))
fi

# Test 13: No self-match on ci.yml step name (per docs/agent/code-standards.md
# § Hygiene-check CI steps -- the forbidden-string check must not match its
# own ci.yml step name).
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-terraform-ecs-entrypoint.sh" "yml"

# Summary
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
