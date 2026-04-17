#!/usr/bin/env bash
# test_ecs_run_task.sh — Tests for ecs-run-task.sh option parsing and the
# --max-runtime wrapping behavior (#2572).
#
# Focuses on the --max-runtime flag added to cap runaway oneshot tasks at
# the container level.  Exercises the --dry-run path so no AWS calls are
# made; the dry-run JSON includes the final container command, which lets
# us assert whether `timeout` was prepended correctly.
#
# Usage:
#   scripts/tests/test_ecs_run_task.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ECS_RUN_TASK="$SCRIPT_DIR/ecs-run-task.sh"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

TEMP_DIRS=()
cleanup() {
    set +e
    for d in ${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}; do
        if [[ -n "$d" && -d "$d" ]]; then
            rm -rf "$d"
        fi
    done
}
trap cleanup EXIT

make_temp_dir() {
    local dir
    dir=$(mktemp -d)
    TEMP_DIRS+=("$dir")
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
    if [[ -n "${2:-}" ]]; then
        echo "  $2"
    fi
}

# Create a mock aws CLI that provides just enough responses for --dry-run.
# The real ecs-run-task.sh calls describe-task-definition and describe-images
# during setup (before hitting the DRY_RUN exit).  We return minimal valid
# JSON so setup completes and the script reaches the dry-run exit.
setup_mock_aws() {
    local tmpdir
    tmpdir=$(make_temp_dir)

    local mock_bin="$tmpdir/bin"
    mkdir -p "$mock_bin"

    cat > "$mock_bin/aws" << 'MOCK_AWS'
#!/usr/bin/env bash
# Mock aws CLI for ecs-run-task.sh dry-run tests

# Parse subcommand family
case "${1:-}" in
    ecs)
        shift
        case "${1:-}" in
            describe-task-definition)
                # Return a minimal task definition that matches the shape
                # expected by the downstream Python extraction helpers.
                cat <<'JSON'
{
    "taskDefinition": {
        "executionRoleArn": "arn:aws:iam::155326049300:role/exec",
        "taskRoleArn": "arn:aws:iam::155326049300:role/task",
        "containerDefinitions": [
            {
                "name": "worker",
                "image": "155326049300.dkr.ecr.us-west-2.amazonaws.com/judgemind/scraper:latest",
                "environment": [],
                "secrets": [],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": "/ecs/judgemind-ingestion-worker-dev",
                        "awslogs-region": "us-west-2"
                    }
                }
            }
        ]
    }
}
JSON
                exit 0
                ;;
            *)
                echo "Mock aws ecs: unused subcommand $1 in dry-run path" >&2
                exit 0
                ;;
        esac
        ;;
    ecr)
        shift
        case "${1:-}" in
            describe-images)
                # Print digest string so the script's digest-equality check
                # succeeds and no image override is applied (the tests below
                # don't care which branch executes).
                cat <<'JSON'
{"digest":"sha256:deadbeef","pushed":"2025-01-01T00:00:00Z"}
JSON
                exit 0
                ;;
        esac
        exit 0
        ;;
    iam)
        # Used when --role is passed; tests here don't exercise it.
        echo "arn:aws:iam::155326049300:role/mock"
        exit 0
        ;;
esac

echo "Mock aws: unhandled service $1" >&2
exit 0
MOCK_AWS
    chmod +x "$mock_bin/aws"

    echo "$tmpdir"
}

# Write a tiny fake script so ecs-run-task.sh has something to encode.
make_fake_script() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    local path="$tmpdir/hello.py"
    printf 'print("hi")\n' > "$path"
    echo "$path"
}

# Extract the container command string from the dry-run JSON output.
# The dry-run block prints the task def JSON via `python3 -m json.tool`;
# the container command lives at .containerDefinitions[0].command[0].
extract_command_str() {
    local dry_output="$1"
    python3 - "$dry_output" <<'PY'
import sys, json, re

output = sys.argv[1]
# Find the JSON block — it starts with "{" at column 0 and ends with "}"
# at column 0.  The json.tool output in the dry-run section uses 4-space
# indentation, so the outermost braces are flush-left.
match = re.search(r'^\{.*?^\}', output, re.MULTILINE | re.DOTALL)
if not match:
    sys.exit(0)

try:
    data = json.loads(match.group(0))
except json.JSONDecodeError:
    sys.exit(0)

containers = data.get("containerDefinitions", [])
if not containers:
    sys.exit(0)
command = containers[0].get("command", [])
if command:
    print(command[0])
PY
}

# ── Tests ──────────────────────────────────────────────────────────────────

# Test 1: --help prints usage including the new flag
test_help_documents_max_runtime() {
    local output
    output=$("$ECS_RUN_TASK" --help 2>&1 || true)
    if echo "$output" | grep -q -- "--max-runtime"; then
        pass "--help documents --max-runtime"
    else
        fail "--help documents --max-runtime" "output was: $output"
    fi
}

# Test 2: Invalid --max-runtime value is rejected
test_invalid_max_runtime_rejected() {
    local fake_script tmpdir output exit_code
    fake_script=$(make_fake_script)
    tmpdir=$(setup_mock_aws)

    exit_code=0
    output=$(
        PATH="$tmpdir/bin:$PATH" \
        "$ECS_RUN_TASK" --dry-run --max-runtime abc "$fake_script" 2>&1
    ) || exit_code=$?

    if [[ $exit_code -ne 0 ]]; then
        pass "non-numeric --max-runtime exits non-zero"
    else
        fail "non-numeric --max-runtime exits non-zero" "exit code: $exit_code, output: $output"
    fi

    if echo "$output" | grep -q "must be a non-negative integer"; then
        pass "non-numeric --max-runtime produces clear error"
    else
        fail "non-numeric --max-runtime produces clear error" "got: $output"
    fi
}

# Test 3: Without --max-runtime, COMMAND does NOT start with `timeout`
test_without_max_runtime_no_timeout_wrapper() {
    local fake_script tmpdir output command
    fake_script=$(make_fake_script)
    tmpdir=$(setup_mock_aws)

    output=$(
        PATH="$tmpdir/bin:$PATH" \
        "$ECS_RUN_TASK" --dry-run "$fake_script" 2>&1
    ) || true

    command=$(extract_command_str "$output")
    if [[ -z "$command" ]]; then
        fail "dry-run emits container command" "could not parse JSON; output was: $output"
        return
    fi

    if echo "$command" | grep -q "timeout --"; then
        fail "no --max-runtime ⇒ no timeout wrapper" "command was: $command"
    else
        pass "no --max-runtime ⇒ no timeout wrapper"
    fi
}

# Test 4: With --max-runtime 3600, COMMAND is wrapped with `timeout 3600`
test_with_max_runtime_wraps_timeout() {
    local fake_script tmpdir output command
    fake_script=$(make_fake_script)
    tmpdir=$(setup_mock_aws)

    output=$(
        PATH="$tmpdir/bin:$PATH" \
        "$ECS_RUN_TASK" --dry-run --max-runtime 3600 "$fake_script" 2>&1
    ) || true

    command=$(extract_command_str "$output")
    if [[ -z "$command" ]]; then
        fail "dry-run emits container command (with --max-runtime)" "could not parse JSON; output was: $output"
        return
    fi

    # Check for the timeout invocation with the expected flags.
    if echo "$command" | grep -q -- "timeout --preserve-status --signal=TERM --kill-after=30 3600"; then
        pass "--max-runtime 3600 wraps command with timeout"
    else
        fail "--max-runtime 3600 wraps command with timeout" "command was: $command"
    fi
}

# Test 5: --max-runtime 0 disables the wrapper (explicit opt-out)
test_max_runtime_zero_disables() {
    local fake_script tmpdir output command
    fake_script=$(make_fake_script)
    tmpdir=$(setup_mock_aws)

    output=$(
        PATH="$tmpdir/bin:$PATH" \
        "$ECS_RUN_TASK" --dry-run --max-runtime 0 "$fake_script" 2>&1
    ) || true

    command=$(extract_command_str "$output")
    if [[ -z "$command" ]]; then
        fail "dry-run emits container command (--max-runtime 0)" "could not parse JSON; output was: $output"
        return
    fi

    if echo "$command" | grep -q "timeout --"; then
        fail "--max-runtime 0 disables wrapper" "command was: $command"
    else
        pass "--max-runtime 0 disables wrapper"
    fi
}

# Test 6: Timeout wrapper appears before interpreter, not after
test_timeout_wraps_interpreter_not_download() {
    local fake_script tmpdir output command
    fake_script=$(make_fake_script)
    tmpdir=$(setup_mock_aws)

    output=$(
        PATH="$tmpdir/bin:$PATH" \
        "$ECS_RUN_TASK" --dry-run --max-runtime 600 "$fake_script" 2>&1
    ) || true

    command=$(extract_command_str "$output")
    if [[ -z "$command" ]]; then
        fail "timeout wraps only the interpreter invocation" "no JSON parsed; output was: $output"
        return
    fi

    # For inline (base64) delivery, the command is:
    #   echo ... | base64 -d > /tmp/_oneshot_script && <INVOCATION>
    # where <INVOCATION> begins with `timeout ...`.  We assert the order.
    if echo "$command" | grep -qE '&& timeout --preserve-status --signal=TERM --kill-after=30 600 python3 /tmp/_oneshot_script'; then
        pass "timeout wraps only the interpreter invocation (not base64 decode)"
    else
        fail "timeout wraps only the interpreter invocation (not base64 decode)" "command was: $command"
    fi
}

# ── Run all tests ──────────────────────────────────────────────────────────

test_help_documents_max_runtime
test_invalid_max_runtime_rejected
test_without_max_runtime_no_timeout_wrapper
test_with_max_runtime_wraps_timeout
test_max_runtime_zero_disables
test_timeout_wraps_interpreter_not_download

echo ""
echo "────────────────────────────────────────────"
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
