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
ECS_RUN_TASK="${ECS_RUN_TASK_UNDER_TEST:-$SCRIPT_DIR/ecs-run-task.sh}"
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

# ── Wait-timeout default (#4723, #4835) ─────────────────────────────────────
#
# One attached call must fit inside the Bash tool's 600s cap, so the wait is
# bounded at 480s by default, with or without --max-runtime.  A longer task
# is reported as still running (exit 124), never as failed (#4723).

run_dry() {
    # run_dry <args...> — dry-run against the mock aws; echoes combined output.
    local fake_script tmpdir
    fake_script=$(make_fake_script)
    tmpdir=$(setup_mock_aws)
    PATH="$tmpdir/bin:$PATH" "$ECS_RUN_TASK" --dry-run "$@" "$fake_script" 2>&1 || true
}

test_ecs_run_task_timeout_default_without_max_runtime() {
    local output
    output=$(run_dry)
    if echo "$output" | grep -q "Wait timeout: 480s"; then
        pass "ecs_run_task_timeout: default wait is 480s (under the 600s tool cap)"
    else
        fail "ecs_run_task_timeout: default wait is 480s (under the 600s tool cap)" "output: $output"
    fi
}

test_ecs_run_task_timeout_max_runtime_does_not_stretch_wait() {
    local output
    output=$(run_dry --max-runtime 10800)
    if echo "$output" | grep -q "Wait timeout: 480s"; then
        pass "ecs_run_task_timeout: --max-runtime 10800 keeps the 480s wait"
    else
        fail "ecs_run_task_timeout: --max-runtime 10800 keeps the 480s wait" "output: $output"
    fi
}

test_ecs_run_task_timeout_explicit_wins() {
    local output
    output=$(run_dry --timeout 300 --max-runtime 10800)
    if echo "$output" | grep -q "Wait timeout: 300s"; then
        pass "ecs_run_task_timeout: explicit --timeout is used"
    else
        fail "ecs_run_task_timeout: explicit --timeout is used" "output: $output"
    fi
}

test_ecs_run_task_timeout_invalid_rejected() {
    local output
    output=$(run_dry --timeout abc)
    if echo "$output" | grep -q -- "--timeout must be a non-negative integer"; then
        pass "ecs_run_task_timeout: non-numeric --timeout rejected"
    else
        fail "ecs_run_task_timeout: non-numeric --timeout rejected" "output: $output"
    fi
}

# ── Full attached-mode flow with a stateful aws stub (#4723) ─────────────────
#
# The stub emulates the real CloudWatch behavior that caused the bug: the log
# group holds many oneshot/oneshot/* streams, and a bare-prefix query returns
# only the first page alphabetically (which never includes our late-sorting
# task ID).  Only the exact task-specific prefix returns the stream.

LATE_TASK_ID="f585c2568b50450699e715cae36a4e98"
LATE_TASK_ARN="arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/${LATE_TASK_ID}"

# setup_full_mock <root> — installs the script under <root>/scripts (so its
# REPO_ROOT, and therefore tmp/last-ecs-task.arn, lives in the sandbox) and a
# stub aws + sleep under <root>/bin.  Stub behavior is driven by env vars:
#   MOCK_STOP_AFTER   describe-tasks returns RUNNING this many times, then
#                     STOPPED (exitCode 0).  "never" = always RUNNING.
#   MOCK_STATE        directory for call counters / recorded args.
setup_full_mock() {
    local root="$1"
    mkdir -p "$root/bin" "$root/scripts" "$root/state"
    cp "$ECS_RUN_TASK" "$root/scripts/ecs-run-task.sh"
    # Post-hoc fallback stub that always fails, so the assertions below can
    # only pass if the wait loop's own live streaming found the stream.
    cat > "$root/scripts/ecs-logs.sh" << 'MOCK_LOGS'
#!/usr/bin/env bash
echo "ecs-logs.sh fallback invoked" >&2
exit 1
MOCK_LOGS
    chmod +x "$root/scripts/ecs-logs.sh"

    cat > "$root/bin/sleep" << 'MOCK_SLEEP'
#!/usr/bin/env bash
exit 0
MOCK_SLEEP

    cat > "$root/bin/aws" << 'MOCK_AWS'
#!/usr/bin/env bash
state="${MOCK_STATE:?}"
echo "$*" >> "$state/calls.log"
svc="${1:-}"; sub="${2:-}"
arg_after() {
    local want="$1"; shift
    while [[ $# -gt 0 ]]; do
        if [[ "$1" == "$want" ]]; then echo "${2:-}"; return; fi
        shift
    done
}
case "$svc $sub" in
    "ecs describe-task-definition")
        cat <<'JSON'
{"taskDefinition": {"executionRoleArn": "arn:aws:iam::155326049300:role/exec",
 "taskRoleArn": "arn:aws:iam::155326049300:role/task",
 "containerDefinitions": [{"name": "worker",
   "image": "155326049300.dkr.ecr.us-west-2.amazonaws.com/judgemind/scraper:latest",
   "environment": [], "secrets": [],
   "logConfiguration": {"logDriver": "awslogs", "options": {
     "awslogs-group": "/ecs/judgemind-ingestion-worker-dev", "awslogs-region": "us-west-2"}}}]}}
JSON
        ;;
    "ecr describe-images")
        echo '{"digest":"sha256:deadbeef","pushed":"2025-01-01T00:00:00Z"}'
        ;;
    "ecs register-task-definition")
        echo '{"taskDefinition":{"taskDefinitionArn":"arn:aws:ecs:us-west-2:155326049300:task-definition/judgemind-oneshot-dev:1"}}'
        ;;
    "ecs describe-services")
        echo '{"subnets":["subnet-1"],"securityGroups":["sg-1"]}'
        ;;
    "ecs run-task")
        echo '{"tasks":[{"taskArn":"arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/f585c2568b50450699e715cae36a4e98"}],"failures":[]}'
        ;;
    "ecs describe-tasks")
        n=$(cat "$state/describe.count" 2>/dev/null || echo 0)
        n=$((n + 1)); echo "$n" > "$state/describe.count"
        if [[ "${MOCK_STOP_AFTER}" != "never" && "$n" -gt "${MOCK_STOP_AFTER}" ]]; then
            echo '{"tasks":[{"lastStatus":"STOPPED","stoppedReason":"Essential container in task exited","containers":[{"exitCode":0}]}]}'
        else
            echo '{"tasks":[{"lastStatus":"RUNNING","containers":[{}]}]}'
        fi
        ;;
    "logs describe-log-streams")
        prefix=$(arg_after --log-stream-name-prefix "$@")
        echo "$prefix" >> "$state/prefixes.log"
        if [[ "$prefix" == "oneshot/oneshot/f585c2568b50450699e715cae36a4e98" ]]; then
            printf 'oneshot/oneshot/f585c2568b50450699e715cae36a4e98\n'
        elif [[ "$prefix" == "oneshot/oneshot/" ]]; then
            # First alphabetical page only — never includes the f585 task.
            printf 'oneshot/oneshot/0010689f4da44d39aad28b8c16ea49b4\toneshot/oneshot/0019ddd861f841a59b25531cd5df5ad4\n'
        fi
        ;;
    "logs get-log-events")
        if [[ "${MOCK_LOG_LAG:-}" == "1" ]]; then
            # Emulate ingestion lag: the first read sees an empty stream and
            # returns a token positioned *past* the not-yet-ingested first
            # event.  Reading with that token never returns it; reading from
            # the head later does.
            n=$(cat "$state/gle.count" 2>/dev/null || echo 0)
            n=$((n + 1)); echo "$n" > "$state/gle.count"
            token=$(arg_after --next-token "$@")
            if [[ "$n" -eq 1 ]]; then
                echo '{"events":[],"nextForwardToken":"f/skip"}'
            elif [[ "$token" == "f/skip" ]]; then
                echo '{"events":[],"nextForwardToken":"f/skip"}'
            elif [[ -z "$token" ]]; then
                echo '{"events":[{"message":"first-line-from-task"}],"nextForwardToken":"f/2"}'
            else
                echo '{"events":[],"nextForwardToken":"f/2"}'
            fi
        elif [[ "${MOCK_LOG_EMPTY_PAGES:-}" == "1" ]]; then
            # Emulate GetLogEvents' documented "empty page, more events
            # behind the token" behavior (#4791): after the first event,
            # two empty pages each hand back a NEW forward token before a
            # later event appears.  The end of the stream repeats its token.
            token=$(arg_after --next-token "$@")
            case "$token" in
                "")    echo '{"events":[{"message":"first-event"}],"nextForwardToken":"f/1"}' ;;
                "f/1") echo '{"events":[],"nextForwardToken":"f/2"}' ;;
                "f/2") echo '{"events":[],"nextForwardToken":"f/3"}' ;;
                "f/3") echo '{"events":[{"message":"later-event"}],"nextForwardToken":"f/4"}' ;;
                *)     echo "{\"events\":[],\"nextForwardToken\":\"$token\"}" ;;
            esac
        else
            echo '{"events":[{"message":"hello-from-task"}],"nextForwardToken":"f/1"}'
        fi
        ;;
    *)
        ;;
esac
exit 0
MOCK_AWS
    chmod +x "$root/bin/aws" "$root/bin/sleep"
}

test_ecs_run_task_finds_late_sorting_log_stream() {
    local root fake_script output exit_code=0
    root=$(make_temp_dir)
    setup_full_mock "$root"
    fake_script=$(make_fake_script)

    output=$(
        PATH="$root/bin:$PATH" MOCK_STATE="$root/state" MOCK_STOP_AFTER=2 \
        ECS_RUN_TASK_POLL_INTERVAL=1 \
        "$root/scripts/ecs-run-task.sh" "$fake_script" 2>&1
    ) || exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
        pass "log stream: completed task exits 0"
    else
        fail "log stream: completed task exits 0" "exit $exit_code; output: $output"
    fi
    # "Live Logs" proves the wait loop itself found the stream, rather than
    # the post-hoc ecs-logs.sh fallback rescuing the output after the fact.
    if echo "$output" | grep -q "Log stream: oneshot/oneshot/${LATE_TASK_ID}" \
        && echo "$output" | grep -q "Live Logs"; then
        pass "log stream: late-sorting task stream is found"
    else
        fail "log stream: late-sorting task stream is found" "output: $output"
    fi
    if echo "$output" | grep -q "hello-from-task"; then
        pass "log stream: task log events are streamed live"
    else
        fail "log stream: task log events are streamed live" "output: $output"
    fi
    if grep -qx "oneshot/oneshot/${LATE_TASK_ID}" "$root/state/prefixes.log" 2>/dev/null; then
        pass "log stream: queried with the task-specific prefix"
    else
        fail "log stream: queried with the task-specific prefix" "prefixes: $(cat "$root/state/prefixes.log" 2>/dev/null)"
    fi
}

test_ecs_run_task_timeout_reports_still_running() {
    local root fake_script output exit_code=0
    root=$(make_temp_dir)
    setup_full_mock "$root"
    fake_script=$(make_fake_script)

    output=$(
        PATH="$root/bin:$PATH" MOCK_STATE="$root/state" MOCK_STOP_AFTER=never \
        ECS_RUN_TASK_POLL_INTERVAL=10 \
        "$root/scripts/ecs-run-task.sh" --timeout 30 "$fake_script" 2>&1
    ) || exit_code=$?

    if [[ $exit_code -eq 124 ]]; then
        pass "ecs_run_task_timeout: wait expiry exits 124 (not a task failure)"
    else
        fail "ecs_run_task_timeout: wait expiry exits 124 (not a task failure)" "exit $exit_code; output: $output"
    fi
    if echo "$output" | grep -q "Task still running (task ARN ${LATE_TASK_ARN})"; then
        pass "ecs_run_task_timeout: reports still running with task ARN"
    else
        fail "ecs_run_task_timeout: reports still running with task ARN" "output: $output"
    fi
    if echo "$output" | grep -q "did not complete"; then
        fail "ecs_run_task_timeout: no 'did not complete' failure wording" "output: $output"
    else
        pass "ecs_run_task_timeout: no 'did not complete' failure wording"
    fi
    if [[ "$(cat "$root/tmp/last-ecs-task.arn" 2>/dev/null)" == "$LATE_TASK_ARN" ]]; then
        pass "ecs_run_task_timeout: ARN saved for ecs-wait-task.sh"
    else
        fail "ecs_run_task_timeout: ARN saved for ecs-wait-task.sh" "file: $(cat "$root/tmp/last-ecs-task.arn" 2>/dev/null)"
    fi
    if echo "$output" | grep -q "Do NOT re-run this command" \
        && echo "$output" | grep -qF "scripts/ecs-wait-task.sh ${LATE_TASK_ARN}"; then
        pass "ecs_run_task_timeout: says not to re-run, names the re-attach command"
    else
        fail "ecs_run_task_timeout: says not to re-run, names the re-attach command" "output: $output"
    fi
}

# #4835: after exit 124, the documented re-attach (ecs-wait-task.sh, which
# reads the saved ARN) resumes the wait and returns the task's exit code,
# and the task is launched exactly once.
test_ecs_run_task_reattach_after_still_running_does_not_relaunch() {
    local root fake_script output exit_code=0
    root=$(make_temp_dir)
    setup_full_mock "$root"
    cp "$SCRIPT_DIR/ecs-wait-task.sh" "$root/scripts/ecs-wait-task.sh"
    fake_script=$(make_fake_script)

    # --timeout 30 at a 10s poll = 3 describe-tasks calls, all RUNNING.
    output=$(
        PATH="$root/bin:$PATH" MOCK_STATE="$root/state" MOCK_STOP_AFTER=5 \
        ECS_RUN_TASK_POLL_INTERVAL=10 \
        "$root/scripts/ecs-run-task.sh" --timeout 30 "$fake_script" 2>&1
    ) || exit_code=$?
    if [[ $exit_code -eq 124 ]]; then
        pass "reattach: attached call exits 124 while the task runs"
    else
        fail "reattach: attached call exits 124 while the task runs" "exit $exit_code; output: $output"
    fi

    exit_code=0
    output=$(
        PATH="$root/bin:$PATH" MOCK_STATE="$root/state" MOCK_STOP_AFTER=5 \
        "$root/scripts/ecs-wait-task.sh" 2>&1
    ) || exit_code=$?
    if [[ $exit_code -eq 0 ]] && echo "$output" | grep -q "status=STOPPED exit_code=0"; then
        pass "reattach: ecs-wait-task.sh on the saved ARN resumes and exits 0"
    else
        fail "reattach: ecs-wait-task.sh on the saved ARN resumes and exits 0" "exit $exit_code; output: $output"
    fi

    local launches
    launches=$(grep -c "^ecs run-task" "$root/state/calls.log" 2>/dev/null || true)
    if [[ "$launches" == "1" ]]; then
        pass "reattach: the task was launched exactly once"
    else
        fail "reattach: the task was launched exactly once" "run-task calls: $launches"
    fi
}

test_ecs_run_task_empty_first_read_does_not_skip_events() {
    local root fake_script output exit_code=0
    root=$(make_temp_dir)
    setup_full_mock "$root"
    fake_script=$(make_fake_script)

    output=$(
        PATH="$root/bin:$PATH" MOCK_STATE="$root/state" MOCK_STOP_AFTER=3 \
        MOCK_LOG_LAG=1 ECS_RUN_TASK_POLL_INTERVAL=1 \
        "$root/scripts/ecs-run-task.sh" "$fake_script" 2>&1
    ) || exit_code=$?

    if [[ $exit_code -eq 0 ]] && echo "$output" | grep -q "first-line-from-task"; then
        pass "log stream: empty first read does not skip late-ingested events"
    else
        fail "log stream: empty first read does not skip late-ingested events" "exit $exit_code; output: $output"
    fi
}

test_ecs_run_task_survives_transient_describe_failure() {
    local root fake_script output exit_code=0
    root=$(make_temp_dir)
    setup_full_mock "$root"
    fake_script=$(make_fake_script)
    # Wrap the stub so the first describe-tasks call fails.
    mv "$root/bin/aws" "$root/bin/aws-real"
    cat > "$root/bin/aws" << 'WRAP'
#!/usr/bin/env bash
if [[ "${1:-} ${2:-}" == "ecs describe-tasks" && ! -f "$MOCK_STATE/failed_once" ]]; then
    touch "$MOCK_STATE/failed_once"
    echo "An error occurred (ThrottlingException)" >&2
    exit 255
fi
exec "$(dirname "$0")/aws-real" "$@"
WRAP
    chmod +x "$root/bin/aws"

    output=$(
        PATH="$root/bin:$PATH" MOCK_STATE="$root/state" MOCK_STOP_AFTER=1 \
        ECS_RUN_TASK_POLL_INTERVAL=1 \
        "$root/scripts/ecs-run-task.sh" "$fake_script" 2>&1
    ) || exit_code=$?

    if [[ $exit_code -eq 0 ]] && echo "$output" | grep -q "could not describe task"; then
        pass "transient describe-tasks failure is retried, not fatal"
    else
        fail "transient describe-tasks failure is retried, not fatal" "exit $exit_code; output: $output"
    fi
}

test_ecs_run_task_empty_page_with_new_token_does_not_stall() {
    local root fake_script output exit_code=0 first_count
    root=$(make_temp_dir)
    setup_full_mock "$root"
    fake_script=$(make_fake_script)

    output=$(
        PATH="$root/bin:$PATH" MOCK_STATE="$root/state" MOCK_STOP_AFTER=4 \
        MOCK_LOG_EMPTY_PAGES=1 ECS_RUN_TASK_POLL_INTERVAL=1 \
        "$root/scripts/ecs-run-task.sh" "$fake_script" 2>&1
    ) || exit_code=$?

    if [[ $exit_code -eq 0 ]] && echo "$output" | grep -q "later-event"; then
        pass "log stream: advances past empty pages whose forward token changes"
    else
        fail "log stream: advances past empty pages whose forward token changes" "exit $exit_code; output: $output"
    fi
    first_count=$(echo "$output" | grep -c "first-event" || true)
    if [[ "$first_count" -eq 1 ]]; then
        pass "log stream: events are not re-printed while paging"
    else
        fail "log stream: events are not re-printed while paging" "first-event printed $first_count times; output: $output"
    fi
}

# install_describe_failure <root> <stderr-text> — wrap the stub so every
# describe-tasks call fails with the given AWS CLI error text (exit 255).
install_describe_failure() {
    local root="$1" err="$2"
    mv "$root/bin/aws" "$root/bin/aws-real"
    printf '%s\n' "$err" > "$root/state/describe.err"
    cat > "$root/bin/aws" << 'WRAP'
#!/usr/bin/env bash
if [[ "${1:-} ${2:-}" == "ecs describe-tasks" ]]; then
    n=$(cat "$MOCK_STATE/describe_fail.count" 2>/dev/null || echo 0)
    echo $((n + 1)) > "$MOCK_STATE/describe_fail.count"
    cat "$MOCK_STATE/describe.err" >&2
    exit 255
fi
exec "$(dirname "$0")/aws-real" "$@"
WRAP
    chmod +x "$root/bin/aws"
}

test_ecs_run_task_expired_token_fails_fast() {
    local root fake_script output exit_code=0 calls
    root=$(make_temp_dir)
    setup_full_mock "$root"
    fake_script=$(make_fake_script)
    install_describe_failure "$root" \
        "An error occurred (ExpiredTokenException) when calling the DescribeTasks operation: The security token included in the request is expired"

    output=$(
        PATH="$root/bin:$PATH" MOCK_STATE="$root/state" MOCK_STOP_AFTER=never \
        ECS_RUN_TASK_POLL_INTERVAL=10 \
        "$root/scripts/ecs-run-task.sh" --timeout 3600 "$fake_script" 2>&1
    ) || exit_code=$?
    calls=$(cat "$root/state/describe_fail.count" 2>/dev/null || echo 0)

    if [[ $exit_code -eq 125 ]]; then
        pass "describe auth error: exits 125 (status unknown), not 124"
    else
        fail "describe auth error: exits 125 (status unknown), not 124" "exit $exit_code; output: $output"
    fi
    if [[ "$calls" -eq 1 ]]; then
        pass "describe auth error: fails fast without retrying"
    else
        fail "describe auth error: fails fast without retrying" "describe-tasks called $calls times"
    fi
    if echo "$output" | grep -q "ExpiredTokenException" && echo "$output" | grep -q "^Fix:"; then
        pass "describe auth error: prints the AWS error and a Fix: block"
    else
        fail "describe auth error: prints the AWS error and a Fix: block" "output: $output"
    fi
    if echo "$output" | grep -q -e "Task still running" -e "do not relaunch"; then
        fail "describe auth error: no 'still running' wording" "output: $output"
    else
        pass "describe auth error: no 'still running' wording"
    fi
    if [[ "$(cat "$root/tmp/last-ecs-task.arn" 2>/dev/null)" == "$LATE_TASK_ARN" ]]; then
        pass "describe auth error: ARN saved for ecs-wait-task.sh"
    else
        fail "describe auth error: ARN saved for ecs-wait-task.sh" "file: $(cat "$root/tmp/last-ecs-task.arn" 2>/dev/null)"
    fi
}

test_ecs_run_task_persistent_describe_errors_report_unknown() {
    local root fake_script output exit_code=0 calls
    root=$(make_temp_dir)
    setup_full_mock "$root"
    fake_script=$(make_fake_script)
    install_describe_failure "$root" \
        "An error occurred (ThrottlingException) when calling the DescribeTasks operation: Rate exceeded"

    output=$(
        PATH="$root/bin:$PATH" MOCK_STATE="$root/state" MOCK_STOP_AFTER=never \
        ECS_RUN_TASK_POLL_INTERVAL=10 \
        "$root/scripts/ecs-run-task.sh" --timeout 3600 "$fake_script" 2>&1
    ) || exit_code=$?
    calls=$(cat "$root/state/describe_fail.count" 2>/dev/null || echo 0)

    if [[ $exit_code -eq 125 ]]; then
        pass "persistent describe errors: exits 125 (status unknown), not 124"
    else
        fail "persistent describe errors: exits 125 (status unknown), not 124" "exit $exit_code; output: $output"
    fi
    if [[ "$calls" -eq 6 ]]; then
        pass "persistent describe errors: gives up after 6 consecutive failures"
    else
        fail "persistent describe errors: gives up after 6 consecutive failures" "describe-tasks called $calls times"
    fi
    if echo "$output" | grep -qi "status unknown" && echo "$output" | grep -q "ThrottlingException"; then
        pass "persistent describe errors: reports status unknown with the AWS error"
    else
        fail "persistent describe errors: reports status unknown with the AWS error" "output: $output"
    fi
    if echo "$output" | grep -q -e "Task still running" -e "do not relaunch"; then
        fail "persistent describe errors: no 'still running' wording" "output: $output"
    else
        pass "persistent describe errors: no 'still running' wording"
    fi
}

# ── Run all tests ──────────────────────────────────────────────────────────

test_help_documents_max_runtime
test_invalid_max_runtime_rejected
test_without_max_runtime_no_timeout_wrapper
test_with_max_runtime_wraps_timeout
test_max_runtime_zero_disables
test_timeout_wraps_interpreter_not_download
test_ecs_run_task_timeout_default_without_max_runtime
test_ecs_run_task_timeout_max_runtime_does_not_stretch_wait
test_ecs_run_task_timeout_explicit_wins
test_ecs_run_task_timeout_invalid_rejected
test_ecs_run_task_finds_late_sorting_log_stream
test_ecs_run_task_timeout_reports_still_running
test_ecs_run_task_reattach_after_still_running_does_not_relaunch
test_ecs_run_task_survives_transient_describe_failure
test_ecs_run_task_empty_first_read_does_not_skip_events
test_ecs_run_task_empty_page_with_new_token_does_not_stall
test_ecs_run_task_expired_token_fails_fast
test_ecs_run_task_persistent_describe_errors_report_unknown

echo ""
echo "────────────────────────────────────────────"
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
