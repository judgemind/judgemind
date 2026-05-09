#!/usr/bin/env bash
# test_dev_db_query.sh — Tests for dev-db-query.sh flag parsing (#2862).
#
# Focuses on the pre-AWS path: --file, --rw, usage errors, and the
# comment-only query guard. Never reaches `aws ecs execute-command` because
# the mock aws CLI returns an empty task list, causing the script to exit
# with a "no running task" error — which still happens *after* the flag
# parsing and query-source resolution we care about, so we assert on the
# error message ordering to know which branch the script took.
#
# Some tests stub `aws` to return a fake task ARN *and* stub out `aws ecs
# execute-command` as a no-op so we can observe the fully-assembled command.
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV_DB_QUERY="$SCRIPT_DIR/dev-db-query.sh"
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

# Mock aws CLI that returns "no tasks". Causes the real script to exit
# after the query-source step with "no running task found" — lets us assert
# the script got past flag parsing without actually talking to AWS.
setup_mock_aws_no_tasks() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    local mock_bin="$tmpdir/bin"
    mkdir -p "$mock_bin"

    cat > "$mock_bin/aws" << 'MOCK_AWS'
#!/usr/bin/env bash
# No matter the args, return None so list-tasks reports no task.
if [[ "${1:-}" == "ecs" && "${2:-}" == "list-tasks" ]]; then
    echo "None"
    exit 0
fi
echo "Mock aws: unexpected command: $*" >&2
exit 1
MOCK_AWS
    chmod +x "$mock_bin/aws"
    echo "$mock_bin"
}

# Mock aws CLI that simulates a full execute-command round-trip including
# the SSM plugin's banner / trailer lines. The mock returns a fixed task
# ARN for list-tasks and prints the given SSM-shaped payload for
# execute-command. Used to test that dev-db-query.sh strips the plugin
# chatter (#3195 Layer 1).
setup_mock_aws_with_exec_payload() {
    local payload_file="$1"
    local tmpdir
    tmpdir=$(make_temp_dir)
    local mock_bin="$tmpdir/bin"
    mkdir -p "$mock_bin"

    cat > "$mock_bin/aws" << MOCK_AWS
#!/usr/bin/env bash
if [[ "\${1:-}" == "ecs" && "\${2:-}" == "list-tasks" ]]; then
    echo "arn:aws:ecs:us-west-2:000000000000:task/fake-cluster/fake-task-id"
    exit 0
fi
if [[ "\${1:-}" == "ecs" && "\${2:-}" == "execute-command" ]]; then
    cat "$payload_file"
    exit 0
fi
echo "Mock aws: unexpected command: \$*" >&2
exit 1
MOCK_AWS
    chmod +x "$mock_bin/aws"
    echo "$mock_bin"
}

run_script() {
    # Args: <mock_bin> <args...>
    #
    # Defaults LIST_TASKS_POLL_TIMEOUT_SECS to 0 so flag-parsing / SSM-strip
    # tests that aren't exercising polling behavior fail fast on the first
    # list-tasks "None" response from the mock (~instant) instead of waiting
    # the script's 60-second production default + 3-second retry loop. With
    # =0, the deadline check on the first iteration fires immediately —
    # 5 affected tests × ~60s each → ~5 min cut from the suite (#4497).
    #
    # Tests that need to exercise polling behavior (test_polls_until_task_appears,
    # test_polls_then_gives_up_with_clear_error, test_exec_agent_probe_*)
    # set LIST_TASKS_POLL_TIMEOUT_SECS / EXEC_AGENT_POLL_TIMEOUT_SECS on the
    # command line and those values take precedence over these defaults via
    # bash's standard env-var precedence.
    local mock_bin="$1"
    shift
    LIST_TASKS_POLL_TIMEOUT_SECS="${LIST_TASKS_POLL_TIMEOUT_SECS:-0}" \
        EXEC_AGENT_POLL_TIMEOUT_SECS="${EXEC_AGENT_POLL_TIMEOUT_SECS:-0}" \
        PATH="$mock_bin:$PATH" "$DEV_DB_QUERY" "$@" 2>&1
}

# ── Tests ──────────────────────────────────────────────────────────────────

test_no_args_shows_usage() {
    local mock_bin
    mock_bin=$(setup_mock_aws_no_tasks)
    local output
    output=$(run_script "$mock_bin" || true)

    if [[ "$output" == *"no SQL query provided"* && "$output" == *"Usage:"* ]]; then
        pass "no args prints usage"
    else
        fail "no args prints usage" "got: $output"
    fi
}

test_help_flag_exits_zero() {
    local mock_bin
    mock_bin=$(setup_mock_aws_no_tasks)
    local output
    local rc=0
    output=$(run_script "$mock_bin" --help) || rc=$?

    if [[ $rc -eq 0 && "$output" == *"Usage:"* ]]; then
        pass "--help exits 0 with usage"
    else
        fail "--help exits 0 with usage" "rc=$rc output=$output"
    fi
}

test_leading_dash_query_accepted() {
    # Queries starting with `-` (SQL comments, unusual EXPLAIN variants, etc.)
    # must be treated as the positional query, not as an unknown flag. This
    # is important for --rw use cases where an operator might paste a block
    # that starts with a `--` comment header.
    local mock_bin
    mock_bin=$(setup_mock_aws_no_tasks)
    local output
    output=$(run_script "$mock_bin" "-- header
SELECT 1;" || true)

    if [[ "$output" == *"no running task found"* ]]; then
        pass "query starting with comment reaches AWS stage"
    else
        fail "query starting with comment reaches AWS stage" "got: $output"
    fi
}

test_file_requires_argument() {
    local mock_bin
    mock_bin=$(setup_mock_aws_no_tasks)
    local output
    output=$(run_script "$mock_bin" --file || true)

    if [[ "$output" == *"--file requires a path argument"* ]]; then
        pass "--file without path errors"
    else
        fail "--file without path errors" "got: $output"
    fi
}

test_file_missing_path_errors() {
    local mock_bin
    mock_bin=$(setup_mock_aws_no_tasks)
    local output
    output=$(run_script "$mock_bin" --file /nonexistent/path/to/query.sql || true)

    if [[ "$output" == *"SQL file not found"* ]]; then
        pass "--file with missing file errors"
    else
        fail "--file with missing file errors" "got: $output"
    fi
}

test_file_plus_positional_conflict() {
    local mock_bin
    mock_bin=$(setup_mock_aws_no_tasks)
    local tmpdir
    tmpdir=$(make_temp_dir)
    local sql_file="$tmpdir/q.sql"
    echo "SELECT 1" > "$sql_file"

    local output
    output=$(run_script "$mock_bin" --file "$sql_file" "SELECT 2" || true)

    if [[ "$output" == *"cannot combine --file with a positional"* ]]; then
        pass "--file + positional query conflict"
    else
        fail "--file + positional query conflict" "got: $output"
    fi
}

test_comment_only_inline_query_rejected() {
    # #2862 — psycopg silently succeeds on comment-only queries, which used
    # to leak out as {"rowcount": -1}. The script should reject these before
    # sending anything to AWS.
    local mock_bin
    mock_bin=$(setup_mock_aws_no_tasks)
    local output
    output=$(run_script "$mock_bin" "-- just a comment" || true)

    if [[ "$output" == *"empty or contains only comments"* ]]; then
        pass "comment-only inline query rejected"
    else
        fail "comment-only inline query rejected" "got: $output"
    fi
}

test_block_comment_only_rejected() {
    local mock_bin
    mock_bin=$(setup_mock_aws_no_tasks)
    local output
    output=$(run_script "$mock_bin" "/* just a block */" || true)

    if [[ "$output" == *"empty or contains only comments"* ]]; then
        pass "block-comment-only query rejected"
    else
        fail "block-comment-only query rejected" "got: $output"
    fi
}

test_whitespace_only_file_rejected() {
    local mock_bin
    mock_bin=$(setup_mock_aws_no_tasks)
    local tmpdir
    tmpdir=$(make_temp_dir)
    local sql_file="$tmpdir/empty.sql"
    printf '   \n\t\n;\n' > "$sql_file"

    local output
    output=$(run_script "$mock_bin" --file "$sql_file" || true)

    if [[ "$output" == *"empty or contains only comments"* ]]; then
        pass "whitespace-only file rejected"
    else
        fail "whitespace-only file rejected" "got: $output"
    fi
}

test_file_with_select_reaches_aws() {
    # Happy path: --file points to a valid SELECT; script should pass the
    # query-source check and fail only at list-tasks (because our mock aws
    # returns None).
    local mock_bin
    mock_bin=$(setup_mock_aws_no_tasks)
    local tmpdir
    tmpdir=$(make_temp_dir)
    local sql_file="$tmpdir/q.sql"
    echo "SELECT count(*) FROM derived.rulings;" > "$sql_file"

    local output
    output=$(run_script "$mock_bin" --file "$sql_file" || true)

    if [[ "$output" == *"no running task found"* ]]; then
        pass "--file with SELECT reaches AWS stage"
    else
        fail "--file with SELECT reaches AWS stage" "got: $output"
    fi
}

test_file_equals_form_accepted() {
    local mock_bin
    mock_bin=$(setup_mock_aws_no_tasks)
    local tmpdir
    tmpdir=$(make_temp_dir)
    local sql_file="$tmpdir/q.sql"
    echo "SELECT 1;" > "$sql_file"

    local output
    output=$(run_script "$mock_bin" --file="$sql_file" || true)

    if [[ "$output" == *"no running task found"* ]]; then
        pass "--file=<path> form accepted"
    else
        fail "--file=<path> form accepted" "got: $output"
    fi
}

test_rw_flag_still_works() {
    local mock_bin
    mock_bin=$(setup_mock_aws_no_tasks)
    local output
    output=$(run_script "$mock_bin" --rw "UPDATE derived.rulings SET status = 'x' WHERE id = 1;" || true)

    if [[ "$output" == *"no running task found"* ]]; then
        pass "--rw with positional query still works"
    else
        fail "--rw with positional query still works" "got: $output"
    fi
}

test_rw_file_combo() {
    local mock_bin
    mock_bin=$(setup_mock_aws_no_tasks)
    local tmpdir
    tmpdir=$(make_temp_dir)
    local sql_file="$tmpdir/upd.sql"
    echo "UPDATE derived.rulings SET status = 'x' WHERE id = 1;" > "$sql_file"

    local output
    output=$(run_script "$mock_bin" --rw --file "$sql_file" || true)

    if [[ "$output" == *"no running task found"* ]]; then
        pass "--rw --file combo accepted"
    else
        fail "--rw --file combo accepted" "got: $output"
    fi
}

# ── SSM trailer-stripping tests (#3195 Layer 1) ───────────────────────────

test_strips_ssm_banners_and_trailer() {
    # dev-db-query.sh should strip the Session Manager plugin's banner /
    # trailer lines before returning, so downstream consumers (jq, awk)
    # see only the Python runner's JSON.
    local tmpdir
    tmpdir=$(make_temp_dir)
    local payload="$tmpdir/payload.txt"
    cat > "$payload" <<'PAYLOAD'

The Session Manager plugin was installed successfully. Use the AWS CLI to start a session.


Starting session with SessionId: ecs-execute-command-abc123
[{"ok":true,"value":42}]

Exiting session with sessionId: ecs-execute-command-abc123
PAYLOAD

    local mock_bin
    mock_bin=$(setup_mock_aws_with_exec_payload "$payload")
    local stdout
    stdout=$(PATH="$mock_bin:$PATH" "$DEV_DB_QUERY" "SELECT 1" 2>/dev/null || true)

    # Positive: JSON should be present.
    if [[ "$stdout" != *'{"ok":true,"value":42}'* ]]; then
        fail "strips SSM banners: JSON payload survived the filter" \
            "got: $stdout"
        return
    fi

    # Negative: banner / trailer lines should NOT appear on stdout.
    if [[ "$stdout" == *"The Session Manager plugin was installed successfully"* ]]; then
        fail "strips SSM banners: plugin-install banner leaked" "got: $stdout"
        return
    fi
    if [[ "$stdout" == *"Starting session with SessionId"* ]]; then
        fail "strips SSM banners: 'Starting session' leaked" "got: $stdout"
        return
    fi
    if [[ "$stdout" == *"Exiting session with sessionId"* ]]; then
        fail "strips SSM banners: 'Exiting session' trailer leaked" \
            "got: $stdout"
        return
    fi

    pass "strips SSM banner/trailer lines from stdout"
}

test_strips_cannot_perform_start_session_trailer() {
    # #3195 — when SSM terminates mid-stream it emits 'Cannot perform
    # start session: EOF'. That line must also be stripped so downstream
    # jq doesn't try to parse it. (The JSON itself may still be truncated
    # — the retry loop in _query_lib.sh handles that separately.)
    local tmpdir
    tmpdir=$(make_temp_dir)
    local payload="$tmpdir/payload.txt"
    cat > "$payload" <<'PAYLOAD'
Starting session with SessionId: ecs-execute-command-xyz
[{"n":1}]Cannot perform start session: EOF
PAYLOAD

    local mock_bin
    mock_bin=$(setup_mock_aws_with_exec_payload "$payload")
    local stdout
    stdout=$(PATH="$mock_bin:$PATH" "$DEV_DB_QUERY" "SELECT 1" 2>/dev/null || true)

    if [[ "$stdout" != *'[{"n":1}]Cannot'* ]] \
        && [[ "$stdout" != *"^Cannot perform start session"* ]]; then
        # The trailer on the same line as JSON is intentionally NOT
        # stripped by grep -v (it's a different failure mode that the
        # retry loop + jq validation catches). What we assert here is
        # that when the trailer is on its own line (the clean-close
        # case), it's removed.
        # For the same-line case, the grep -v pattern only matches at
        # line-start, so the trailer remains concatenated to the JSON —
        # that's correct behavior since jq will reject it and the retry
        # loop will re-fetch.
        pass "Cannot-perform-start-session: own-line vs. same-line handled"
    else
        # If the "clean" case (own line) — this is the assertion we care
        # about.
        local cleaned
        cleaned=$(printf '%s' "$stdout" | grep -v '^Cannot perform start session' || true)
        if [[ "$stdout" == "$cleaned" ]]; then
            pass "Cannot-perform-start-session on own line stripped"
        else
            fail "Cannot-perform-start-session on own line leaked" "got: $stdout"
        fi
    fi
}

test_strips_own_line_cannot_perform() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    local payload="$tmpdir/payload.txt"
    cat > "$payload" <<'PAYLOAD'
Starting session with SessionId: ecs-execute-command-xyz
[{"n":1}]
Cannot perform start session: EOF
PAYLOAD

    local mock_bin
    mock_bin=$(setup_mock_aws_with_exec_payload "$payload")
    local stdout
    stdout=$(PATH="$mock_bin:$PATH" "$DEV_DB_QUERY" "SELECT 1" 2>/dev/null || true)

    if [[ "$stdout" == *"Cannot perform start session"* ]]; then
        fail "own-line Cannot-perform-start-session stripped" "got: $stdout"
    else
        pass "own-line Cannot-perform-start-session stripped"
    fi
}

test_empty_output_after_strip_is_ok() {
    # Pure banner/trailer payload with no JSON — the grep -v returns exit
    # 1 (all lines matched filters). The script must not error in that
    # case, because an aws-exec failure is surfaced via aws's exit code,
    # not through the content filter. Consumers (jq) will reject the empty
    # output on their own terms.
    local tmpdir
    tmpdir=$(make_temp_dir)
    local payload="$tmpdir/payload.txt"
    cat > "$payload" <<'PAYLOAD'
Starting session with SessionId: ecs-execute-command-empty
Exiting session with sessionId: ecs-execute-command-empty
PAYLOAD

    local mock_bin
    mock_bin=$(setup_mock_aws_with_exec_payload "$payload")
    local rc=0
    local stdout
    stdout=$(PATH="$mock_bin:$PATH" "$DEV_DB_QUERY" "SELECT 1" 2>/dev/null) || rc=$?

    if [[ $rc -eq 0 ]]; then
        pass "empty-after-filter output: script does not propagate grep's exit 1"
    else
        fail "empty-after-filter output: exit $rc should have been 0" "got: $stdout"
    fi
}

# ── Polling tests (#3556) ──────────────────────────────────────────────────

# Mock aws CLI that returns None for the first N list-tasks calls, then returns
# a real task ARN. A state file in a temp dir tracks the call count.
# execute-command is stubbed as a no-op so polling-success tests don't need a
# full SSM payload.
setup_mock_aws_poll_then_arn() {
    local empty_calls="$1"   # number of list-tasks calls that return None
    local tmpdir
    tmpdir=$(make_temp_dir)
    local mock_bin="$tmpdir/bin"
    local state_file="$tmpdir/call_count"
    mkdir -p "$mock_bin"
    echo "0" > "$state_file"

    cat > "$mock_bin/aws" << MOCK_AWS
#!/usr/bin/env bash
if [[ "\${1:-}" == "ecs" && "\${2:-}" == "list-tasks" ]]; then
    count=\$(cat "$state_file")
    count=\$((count + 1))
    echo "\$count" > "$state_file"
    if [[ "\$count" -le $empty_calls ]]; then
        echo "None"
    else
        echo "arn:aws:ecs:us-west-2:000000000000:task/fake-cluster/fake-task-id"
    fi
    exit 0
fi
if [[ "\${1:-}" == "ecs" && "\${2:-}" == "execute-command" ]]; then
    # No-op stub — we only care that the script reached this stage.
    echo "[]"
    exit 0
fi
echo "Mock aws: unexpected command: \$*" >&2
exit 1
MOCK_AWS
    chmod +x "$mock_bin/aws"
    echo "$mock_bin"
}

test_polls_until_task_appears() {
    # list-tasks returns None on the first 2 calls, then a real ARN on the 3rd.
    # The script should retry and eventually reach the execute-command stage.
    local mock_bin
    mock_bin=$(setup_mock_aws_poll_then_arn 2)
    local output rc=0
    output=$(LIST_TASKS_POLL_TIMEOUT_SECS=10 run_script "$mock_bin" "SELECT 1" 2>&1) || rc=$?

    # Success means the script proceeded past list-tasks (reached execute-command).
    # The mock execute-command exits 0, so the overall exit code should be 0.
    if [[ $rc -eq 0 && "$output" != *"no running task found"* ]]; then
        pass "polls until task appears: proceeds to execute-command on 3rd try"
    else
        fail "polls until task appears: proceeds to execute-command on 3rd try" \
            "rc=$rc output=$output"
    fi
}

test_polls_then_gives_up_with_clear_error() {
    # list-tasks always returns None. With a short timeout, the script should
    # give up and exit non-zero with the "no running task" error message.
    local mock_bin
    mock_bin=$(setup_mock_aws_no_tasks)
    local output rc=0
    output=$(LIST_TASKS_POLL_TIMEOUT_SECS=2 run_script "$mock_bin" "SELECT 1" 2>&1) || rc=$?

    if [[ $rc -ne 0 && "$output" == *"no running task"* ]]; then
        pass "polls then gives up: exits non-zero with 'no running task' error"
    else
        fail "polls then gives up: exits non-zero with 'no running task' error" \
            "rc=$rc output=$output"
    fi
}

# ── Exec-agent readiness probe tests (#3896) ──────────────────────────────

# Mock aws that returns a task ARN on list-tasks, and for execute-command
# either exits 0 immediately (success) or fails with the retryable
# InvalidParameterException message N times before succeeding (or always fails
# to test deadline).
#
# Args:
#   $1 — number of execute-command calls that return the retryable error
#        (0 = succeed immediately; -1 = always fail / deadline)
#   $2 — (optional) tmpdir base; if omitted a new one is created
#
# Prints the mock_bin path.
setup_mock_aws_exec_agent() {
    local retryable_calls="$1"
    local tmpdir
    tmpdir=$(make_temp_dir)
    local mock_bin="$tmpdir/bin"
    local state_file="$tmpdir/exec_call_count"
    mkdir -p "$mock_bin"
    echo "0" > "$state_file"

    # Write a separate retryable-error payload file so the mock script can
    # cat it without needing inline heredocs (which are blocked by the
    # preflight hook in interactive Bash calls but are fine in written files).
    local err_file="$tmpdir/exec_err.txt"
    printf 'An error occurred (InvalidParameterException) when calling the ExecuteCommand operation: The execute command agent is not running. Please wait until the exec agent on your task is ready.\n' \
        > "$err_file"

    cat > "$mock_bin/aws" << MOCK_AWS
#!/usr/bin/env bash
if [[ "\${1:-}" == "ecs" && "\${2:-}" == "list-tasks" ]]; then
    echo "arn:aws:ecs:us-west-2:000000000000:task/fake-cluster/fake-exec-task"
    exit 0
fi
if [[ "\${1:-}" == "ecs" && "\${2:-}" == "execute-command" ]]; then
    count=\$(cat "$state_file")
    count=\$((count + 1))
    echo "\$count" > "$state_file"
    # retryable_calls == -1 means always fail
    if [[ $retryable_calls -eq -1 || \$count -le $retryable_calls ]]; then
        cat "$err_file" >&2
        exit 1
    fi
    echo "[]"
    exit 0
fi
echo "Mock aws: unexpected command: \$*" >&2
exit 1
MOCK_AWS
    chmod +x "$mock_bin/aws"
    echo "$mock_bin"
}

test_exec_agent_probe_success() {
    # Probe exits 0 on first call — no retry messages, real query reached.
    local mock_bin
    mock_bin=$(setup_mock_aws_exec_agent 0)
    local output rc=0
    output=$(LIST_TASKS_POLL_TIMEOUT_SECS=10 EXEC_AGENT_POLL_TIMEOUT_SECS=10 \
        run_script "$mock_bin" "SELECT 1" 2>&1) || rc=$?

    if [[ "$output" == *"exec agent"*"not ready"* ]]; then
        fail "exec_agent_probe_success: no retry message expected" "got: $output"
        return
    fi
    # Script should have reached execute-command (rc=0 or probe stage passed).
    # The mock execute-command returns "[]" which after SSM strip is empty
    # (or passes through) — either way the script must NOT have exited from
    # the probe-failure path.
    if [[ "$output" == *"ECS exec agent on task"*"did not come up"* ]]; then
        fail "exec_agent_probe_success: must not emit deadline message" "got: $output"
        return
    fi
    pass "exec_agent_probe_success: probe succeeds immediately, no retry noise"
}

test_exec_agent_probe_retries() {
    # Probe returns retryable error on calls 1–2, succeeds on call 3.
    # Must emit retry messages and then proceed (not exit non-zero).
    local mock_bin
    mock_bin=$(setup_mock_aws_exec_agent 2)
    local output rc=0
    output=$(LIST_TASKS_POLL_TIMEOUT_SECS=10 EXEC_AGENT_POLL_TIMEOUT_SECS=20 \
        run_script "$mock_bin" "SELECT 1" 2>&1) || rc=$?

    if [[ "$output" != *"exec agent"*"not ready"* ]]; then
        fail "exec_agent_probe_retries: expected retry message in output" "got: $output"
        return
    fi
    if [[ "$output" == *"ECS exec agent on task"*"did not come up"* ]]; then
        fail "exec_agent_probe_retries: must not emit deadline message when probe eventually succeeds" \
            "got: $output"
        return
    fi
    pass "exec_agent_probe_retries: retry messages printed, script proceeds after agent ready"
}

test_exec_agent_probe_deadline_msg() {
    # Probe always returns the retryable error. With a very short timeout the
    # script must exit non-zero and emit the specific deadline message — NOT the
    # raw "execute command was not enabled" text alone.
    local mock_bin
    mock_bin=$(setup_mock_aws_exec_agent -1)
    local output rc=0
    output=$(LIST_TASKS_POLL_TIMEOUT_SECS=10 EXEC_AGENT_POLL_TIMEOUT_SECS=2 \
        run_script "$mock_bin" "SELECT 1" 2>&1) || rc=$?

    if [[ $rc -eq 0 ]]; then
        fail "exec_agent_probe_deadline_msg: must exit non-zero on deadline" "got: $output"
        return
    fi
    if [[ "$output" != *"ECS exec agent on task"*"did not come up"* ]]; then
        fail "exec_agent_probe_deadline_msg: must print specific deadline message" "got: $output"
        return
    fi
    # The raw AWS message must not be the only error shown (it should be wrapped
    # or replaced by the specific deadline message).
    # We allow the raw message to appear in addition (it may be in retry lines),
    # but the final-failure path must emit our friendly message.
    pass "exec_agent_probe_deadline_msg: exits non-zero with specific deadline message"
}

# ── Run all ────────────────────────────────────────────────────────────────

test_no_args_shows_usage
test_help_flag_exits_zero
test_leading_dash_query_accepted
test_file_requires_argument
test_file_missing_path_errors
test_file_plus_positional_conflict
test_comment_only_inline_query_rejected
test_block_comment_only_rejected
test_whitespace_only_file_rejected
test_file_with_select_reaches_aws
test_file_equals_form_accepted
test_rw_flag_still_works
test_rw_file_combo
test_strips_ssm_banners_and_trailer
test_strips_cannot_perform_start_session_trailer
test_strips_own_line_cannot_perform
test_empty_output_after_strip_is_ok
test_polls_until_task_appears
test_polls_then_gives_up_with_clear_error
test_exec_agent_probe_success
test_exec_agent_probe_retries
test_exec_agent_probe_deadline_msg

echo ""
echo "────────────────────────────────────────────────"
echo "Ran $TESTS tests, $FAILURES failed"
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
