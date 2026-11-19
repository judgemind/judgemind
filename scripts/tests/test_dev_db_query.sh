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
# The result-integrity tests (#4781) stub `aws ecs execute-command` with
# framed captures (complete, cut off, unframed, CRLF) and `aws s3 cp/rm`
# with the S3-staged body of a >100 KB result, and check that the script
# prints a complete result or exits non-zero with empty stdout.
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_gh-isolate.sh"  # no real GitHub from tests (#5071)

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

# Frame a payload the way scripts/dev_db_query_runner.py does (#4781), using
# the runner's own helpers so the tests can't drift from the real format.
frame_inline() {
    printf '%s' "$1" | PYTHONPATH="$SCRIPT_DIR" python3 -c \
        'import sys, dev_db_query_runner as r; print(r.frame_inline(sys.stdin.read()))'
}

# frame_s3 <payload-file> <uri>
frame_s3() {
    PYTHONPATH="$SCRIPT_DIR" python3 -c \
        'import sys, dev_db_query_runner as r; print(r.frame_s3(open(sys.argv[1]).read(), sys.argv[2]))' \
        "$1" "$2"
}

# What a successful empty-result query prints over ECS Exec.
FRAMED_EMPTY="$(frame_inline '[]')"

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
#
# Optional $2: a file served for `aws s3 cp <uri> -` (the S3-staged result
# of a large query, #4781). Every `aws s3 rm` call is appended to
# <mock_bin>/../s3_rm_calls so tests can check the staged object is cleaned up.
setup_mock_aws_with_exec_payload() {
    local payload_file="$1"
    local s3_body_file="${2:-/nonexistent}"
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
    if [[ "\$*" == *"bash -c 'true'"* ]]; then
        exit 0
    fi
    cat "$payload_file"
    exit 0
fi
if [[ "\${1:-}" == "s3" && "\${2:-}" == "cp" ]]; then
    if [[ ! -f "$s3_body_file" ]]; then
        echo "An error occurred (404) when calling the HeadObject operation: Not Found" >&2
        exit 1
    fi
    cat "$s3_body_file"
    exit 0
fi
if [[ "\${1:-}" == "s3" && "\${2:-}" == "rm" ]]; then
    echo "\$3" >> "$tmpdir/s3_rm_calls"
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
    {
        printf '\nThe Session Manager plugin was installed successfully. Use the AWS CLI to start a session.\n\n\n'
        printf 'Starting session with SessionId: ecs-execute-command-abc123\n'
        frame_inline '[{"ok":true,"value":42}]'
        printf '\nExiting session with sessionId: ecs-execute-command-abc123\n'
    } > "$payload"

    local mock_bin
    mock_bin=$(setup_mock_aws_with_exec_payload "$payload")
    local stdout
    stdout=$(PATH="$mock_bin:$PATH" "$DEV_DB_QUERY" "SELECT 1" 2>/dev/null || true)

    # Positive: exactly the JSON, nothing else.
    if [[ "$stdout" != '[{"ok":true,"value":42}]' ]]; then
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

test_truncated_stream_exits_nonzero() {
    # #4781 / #3195 — SSM cuts the stream off mid-payload and appends
    # 'Cannot perform start session: EOF' on the same line, while aws still
    # exits 0. The script must exit non-zero, name the truncation, give a
    # Fix: block, and print NOTHING to stdout (no partial JSON).
    local tmpdir
    tmpdir=$(make_temp_dir)
    local framed
    framed=$(frame_inline '[{"n":1},{"n":2},{"n":3}]')
    local payload="$tmpdir/payload.txt"
    {
        printf 'Starting session with SessionId: ecs-execute-command-xyz\n'
        printf '%s' "${framed%%\{\"n\":3\}*}"
        printf 'Cannot perform start session: EOF\n'
    } > "$payload"

    local mock_bin
    mock_bin=$(setup_mock_aws_with_exec_payload "$payload")
    local stdout rc=0
    stdout=$(PATH="$mock_bin:$PATH" "$DEV_DB_QUERY" "SELECT 1" 2>"$tmpdir/err") || rc=$?
    local stderr
    stderr=$(cat "$tmpdir/err")

    if [[ $rc -eq 0 ]]; then
        fail "truncated stream: must exit non-zero" "stdout=$stdout stderr=$stderr"
    elif [[ -n "$stdout" ]]; then
        fail "truncated stream: stdout must be empty" "stdout=$stdout"
    elif [[ "$stderr" != *"cut off"* || "$stderr" != *"Fix:"* ]]; then
        fail "truncated stream: stderr must name the truncation and a Fix:" "stderr=$stderr"
    else
        pass "truncated stream: exits $rc, empty stdout, truncation + Fix: on stderr"
    fi
}

test_bare_unframed_prefix_exits_nonzero() {
    # The #4946 shape: a valid-looking JSON prefix cut at 10272 bytes, with
    # no frame at all. Before #4781 this printed as-is with exit 0.
    local tmpdir
    tmpdir=$(make_temp_dir)
    local payload="$tmpdir/payload.txt"
    PYTHONPATH="$SCRIPT_DIR" python3 -c \
        'import json; s=json.dumps([{"id":i,"v":"x"*40} for i in range(400)]); print(s[:10272])' \
        > "$payload"

    local mock_bin
    mock_bin=$(setup_mock_aws_with_exec_payload "$payload")
    local stdout rc=0
    stdout=$(PATH="$mock_bin:$PATH" "$DEV_DB_QUERY" "SELECT 1" 2>/dev/null) || rc=$?

    if [[ $rc -ne 0 && -z "$stdout" ]]; then
        pass "bare 10272-byte prefix: exits non-zero with empty stdout"
    else
        fail "bare 10272-byte prefix: must exit non-zero with empty stdout" \
            "rc=$rc stdout_bytes=${#stdout}"
    fi
}

test_strips_own_line_cannot_perform() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    local payload="$tmpdir/payload.txt"
    {
        printf 'Starting session with SessionId: ecs-execute-command-xyz\n'
        frame_inline '[{"n":1}]'
        printf 'Cannot perform start session: EOF\n'
    } > "$payload"

    local mock_bin
    mock_bin=$(setup_mock_aws_with_exec_payload "$payload")
    local stdout rc=0
    stdout=$(PATH="$mock_bin:$PATH" "$DEV_DB_QUERY" "SELECT 1" 2>/dev/null) || rc=$?

    if [[ $rc -eq 0 && "$stdout" == '[{"n":1}]' ]]; then
        pass "complete frame + own-line EOF trailer: exits 0 with only the JSON"
    else
        fail "complete frame + own-line EOF trailer: exits 0 with only the JSON" \
            "rc=$rc stdout=$stdout"
    fi
}

test_crlf_pty_output_accepted() {
    # The --interactive session runs over a pty, which turns \n into \r\n.
    local tmpdir
    tmpdir=$(make_temp_dir)
    local payload="$tmpdir/payload.txt"
    {
        printf 'Starting session with SessionId: ecs-execute-command-xyz\n'
        frame_inline '[{"n":1}]'
        printf 'Exiting session with sessionId: ecs-execute-command-xyz\n'
    } | sed 's/$/\r/' > "$payload"

    local mock_bin
    mock_bin=$(setup_mock_aws_with_exec_payload "$payload")
    local stdout rc=0
    stdout=$(PATH="$mock_bin:$PATH" "$DEV_DB_QUERY" "SELECT 1" 2>/dev/null) || rc=$?

    if [[ $rc -eq 0 && "$stdout" == '[{"n":1}]' ]]; then
        pass "pty CRLF output: exits 0 with the clean JSON"
    else
        fail "pty CRLF output: exits 0 with the clean JSON" "rc=$rc stdout=$stdout"
    fi
}

test_banner_only_output_exits_nonzero() {
    # Banner/trailer with no result frame: the runner never printed a
    # result (or the stream died before it did). Before #4781 this exited 0
    # with empty stdout, which reads as "no rows". Now it must fail.
    local tmpdir
    tmpdir=$(make_temp_dir)
    local payload="$tmpdir/payload.txt"
    printf 'Starting session with SessionId: ecs-execute-command-empty\nExiting session with sessionId: ecs-execute-command-empty\n' \
        > "$payload"

    local mock_bin
    mock_bin=$(setup_mock_aws_with_exec_payload "$payload")
    local rc=0
    local stdout
    stdout=$(PATH="$mock_bin:$PATH" "$DEV_DB_QUERY" "SELECT 1" 2>/dev/null) || rc=$?

    if [[ $rc -ne 0 && -z "$stdout" ]]; then
        pass "banner-only output: exits non-zero (no result frame)"
    else
        fail "banner-only output: must exit non-zero" "rc=$rc stdout=$stdout"
    fi
}

test_runner_error_exits_nonzero_with_message() {
    # A database error inside the container used to print to the pty and
    # exit 0. Now the missing frame makes the script fail and the remote
    # message is shown on stderr.
    local tmpdir
    tmpdir=$(make_temp_dir)
    local payload="$tmpdir/payload.txt"
    printf 'Starting session with SessionId: s\nDatabase error: relation "nope" does not exist\nExiting session with sessionId: s\n' \
        > "$payload"

    local mock_bin
    mock_bin=$(setup_mock_aws_with_exec_payload "$payload")
    local rc=0 stdout
    stdout=$(PATH="$mock_bin:$PATH" "$DEV_DB_QUERY" "SELECT 1" 2>"$tmpdir/err") || rc=$?

    if [[ $rc -ne 0 && -z "$stdout" ]] && grep -q 'relation "nope" does not exist' "$tmpdir/err"; then
        pass "runner error: exits non-zero and shows the remote error"
    else
        fail "runner error: exits non-zero and shows the remote error" \
            "rc=$rc stdout=$stdout stderr=$(cat "$tmpdir/err")"
    fi
}

test_invalid_inline_max_env_rejected() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    local payload="$tmpdir/payload.txt"
    frame_inline '[]' > "$payload"
    local mock_bin
    mock_bin=$(setup_mock_aws_with_exec_payload "$payload")
    local rc=0 output
    output=$(DEV_DB_QUERY_INLINE_MAX_BYTES=lots PATH="$mock_bin:$PATH" \
        "$DEV_DB_QUERY" "SELECT 1" 2>&1) || rc=$?
    if [[ $rc -ne 0 && "$output" == *"DEV_DB_QUERY_INLINE_MAX_BYTES must be"* ]]; then
        pass "invalid DEV_DB_QUERY_INLINE_MAX_BYTES rejected"
    else
        fail "invalid DEV_DB_QUERY_INLINE_MAX_BYTES rejected" "rc=$rc output=$output"
    fi
}

# ── S3-staged large results (#4781) ───────────────────────────────────────

make_large_payload() {
    # >100 KB: 5000 rows shaped like `SELECT id, case_number FROM derived.cases`.
    PYTHONPATH="$SCRIPT_DIR" python3 -c \
        'import json; print(json.dumps([{"id":"%08d-0000-0000-0000-000000000000"%i,"case_number":"25CV%06d"%i} for i in range(5000)],separators=(",",":")),end="")' \
        > "$1"
}

test_large_result_via_s3_comes_back_whole() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    local body="$tmpdir/body.json"
    make_large_payload "$body"
    local uri="s3://judgemind-document-archive-dev/tmp/dev-db-query/test.json"
    local payload="$tmpdir/payload.txt"
    {
        printf 'Starting session with SessionId: s\n'
        frame_s3 "$body" "$uri"
        printf 'Exiting session with sessionId: s\n'
    } > "$payload"

    local mock_bin
    mock_bin=$(setup_mock_aws_with_exec_payload "$payload" "$body")
    local rc=0
    PATH="$mock_bin:$PATH" "$DEV_DB_QUERY" "SELECT 1" > "$tmpdir/out" 2>"$tmpdir/err" || rc=$?

    local body_bytes out_rows
    body_bytes=$(wc -c < "$body" | tr -d ' ')
    out_rows=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$tmpdir/out" 2>/dev/null || echo "unparseable")

    if [[ $rc -ne 0 ]]; then
        fail "large result via S3: exit $rc" "stderr=$(cat "$tmpdir/err")"
    elif [[ $body_bytes -le 100000 ]]; then
        fail "large result via S3: fixture should exceed 100 KB" "bytes=$body_bytes"
    elif [[ "$out_rows" != "5000" ]]; then
        fail "large result via S3: expected 5000 rows" "got: $out_rows"
    elif ! grep -qx "$uri" "$mock_bin/../s3_rm_calls" 2>/dev/null; then
        fail "large result via S3: staged object should be deleted after download"
    else
        pass "large result via S3: ${body_bytes}-byte, 5000-row result returned whole; object deleted"
    fi
}

test_large_result_truncated_download_exits_nonzero() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    local body="$tmpdir/body.json"
    make_large_payload "$body"
    head -c 50000 "$body" > "$tmpdir/short.json"
    local uri="s3://judgemind-document-archive-dev/tmp/dev-db-query/test.json"
    local payload="$tmpdir/payload.txt"
    frame_s3 "$body" "$uri" > "$payload"

    local mock_bin
    mock_bin=$(setup_mock_aws_with_exec_payload "$payload" "$tmpdir/short.json")
    local rc=0 stdout
    stdout=$(PATH="$mock_bin:$PATH" "$DEV_DB_QUERY" "SELECT 1" 2>/dev/null) || rc=$?

    if [[ $rc -ne 0 && -z "$stdout" ]]; then
        pass "large result, short download: exits non-zero with empty stdout"
    else
        fail "large result, short download: must exit non-zero" "rc=$rc bytes=${#stdout}"
    fi
}

test_large_result_missing_object_exits_nonzero() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    local body="$tmpdir/body.json"
    make_large_payload "$body"
    local payload="$tmpdir/payload.txt"
    frame_s3 "$body" "s3://b/tmp/dev-db-query/gone.json" > "$payload"

    local mock_bin
    mock_bin=$(setup_mock_aws_with_exec_payload "$payload")
    local rc=0 stdout
    stdout=$(PATH="$mock_bin:$PATH" "$DEV_DB_QUERY" "SELECT 1" 2>"$tmpdir/err") || rc=$?

    if [[ $rc -ne 0 && -z "$stdout" ]] && grep -q "downloading" "$tmpdir/err"; then
        pass "large result, missing object: exits non-zero naming the download failure"
    else
        fail "large result, missing object: must exit non-zero" \
            "rc=$rc stderr=$(cat "$tmpdir/err")"
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
    echo "$FRAMED_EMPTY"
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
    echo "$FRAMED_EMPTY"
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

# ── Per-call timeout tests (#4665) ─────────────────────────────────────────

# Mock aws where list-tasks returns an ARN and execute-command hangs "forever".
# The hang is a `sleep` *child* of the mock (like session-manager-plugin under
# the aws CLI) that inherits stdout, so the test also proves that the timeout
# kills children. Otherwise the $(...) capture in dev-db-query.sh would stay
# blocked on the open pipe.
#
# Args:
#   $1 — "all"   : every execute-command call hangs (probe stalls)
#        "query" : the readiness probe (bash -c 'true') succeeds, the real
#                  query call hangs
setup_mock_aws_exec_hang() {
    local mode="$1"
    local tmpdir
    tmpdir=$(make_temp_dir)
    local mock_bin="$tmpdir/bin"
    mkdir -p "$mock_bin"

    cat > "$mock_bin/aws" << MOCK_AWS
#!/usr/bin/env bash
if [[ "\${1:-}" == "ecs" && "\${2:-}" == "list-tasks" ]]; then
    echo "arn:aws:ecs:us-west-2:000000000000:task/fake-cluster/fake-hang-task"
    exit 0
fi
if [[ "\${1:-}" == "ecs" && "\${2:-}" == "execute-command" ]]; then
    if [[ "$mode" == "query" && "\$*" == *"bash -c 'true'"* ]]; then
        exit 0
    fi
    echo "Starting session with SessionId: ecs-execute-command-hang"
    sleep 100000
    exit 0
fi
echo "Mock aws: unexpected command: \$*" >&2
exit 1
MOCK_AWS
    chmod +x "$mock_bin/aws"
    echo "$mock_bin"
}

test_probe_hang_times_out_with_fallback_fix() {
    # AC1 + AC2: a probe that never returns must not block past
    # EXEC_AGENT_POLL_TIMEOUT_SECS + margin, and the error must name the
    # scripts/ecs-run-task.sh fallback.
    local mock_bin
    mock_bin=$(setup_mock_aws_exec_hang all)
    local output rc=0 start end
    start=$(date +%s)
    output=$(EXEC_AGENT_POLL_TIMEOUT_SECS=2 EXEC_PROBE_TIMEOUT_SECS=2 \
        run_script "$mock_bin" "SELECT 1" 2>&1) || rc=$?
    end=$(date +%s)
    local elapsed=$((end - start))

    if [[ $rc -eq 0 ]]; then
        fail "probe hang: must exit non-zero" "got: $output"
        return
    fi
    # Deadline 2 s + per-probe floor 5 s + 5 s retry sleep + 2 s TERM→KILL
    # grace + slack. Anything near this bound is a regression to "hangs".
    if [[ $elapsed -gt 25 ]]; then
        fail "probe hang: took ${elapsed}s, expected <= 25s" "got: $output"
        return
    fi
    if [[ "$output" != *"stalled"* || "$output" != *"Fix:"* \
        || "$output" != *"scripts/ecs-run-task.sh"* ]]; then
        fail "probe hang: error must include Fix: block naming scripts/ecs-run-task.sh" \
            "got: $output"
        return
    fi
    if [[ "$output" == *"Running query on dev database"* ]]; then
        fail "probe hang: must not proceed to the query" "got: $output"
        return
    fi
    pass "probe hang: exits non-zero in ${elapsed}s with ecs-run-task.sh Fix block"
}

test_query_hang_times_out_with_fallback_fix() {
    # The probe succeeds but the real query's SSM session stalls.
    local mock_bin
    mock_bin=$(setup_mock_aws_exec_hang query)
    local output rc=0 start end
    start=$(date +%s)
    output=$(EXEC_AGENT_POLL_TIMEOUT_SECS=10 EXEC_QUERY_TIMEOUT_SECS=2 \
        run_script "$mock_bin" "SELECT 1" 2>&1) || rc=$?
    end=$(date +%s)
    local elapsed=$((end - start))

    if [[ $rc -eq 0 ]]; then
        fail "query hang: must exit non-zero" "got: $output"
        return
    fi
    if [[ $elapsed -gt 15 ]]; then
        fail "query hang: took ${elapsed}s, expected <= 15s" "got: $output"
        return
    fi
    if [[ "$output" != *"did not finish within 2s"* || "$output" != *"Fix:"* \
        || "$output" != *"scripts/ecs-run-task.sh"* ]]; then
        fail "query hang: error must include timeout + Fix: block naming scripts/ecs-run-task.sh" \
            "got: $output"
        return
    fi
    pass "query hang: exits non-zero in ${elapsed}s with ecs-run-task.sh Fix block"
}

test_run_with_timeout_passthrough() {
    # Unit test of the helper: stdout and non-timeout exit codes pass through
    # unchanged, and a fast command is not delayed by the watchdog.
    # shellcheck source=scripts/_ecs_exec_lib.sh
    source "$SCRIPT_DIR/_ecs_exec_lib.sh"
    local out rc=0 start end
    start=$(date +%s)
    out=$(ecs_exec_run_with_timeout 10 bash -c 'echo hello; exit 7') || rc=$?
    end=$(date +%s)
    if [[ "$out" != "hello" || $rc -ne 7 ]]; then
        fail "run_with_timeout passthrough" "out=$out rc=$rc"
        return
    fi
    if [[ $((end - start)) -gt 3 ]]; then
        fail "run_with_timeout passthrough: fast command delayed $((end - start))s"
        return
    fi
    rc=0
    out=$(ecs_exec_run_with_timeout 1 bash -c 'echo partial; exec sleep 100000') || rc=$?
    if [[ $rc -ne 124 || "$out" != "partial" ]]; then
        fail "run_with_timeout timeout returns 124 with partial output" "out=$out rc=$rc"
        return
    fi
    pass "run_with_timeout: passes through rc/stdout, returns 124 on timeout"
}

# ── Exec target (#5168) ────────────────────────────────────────────────────
# Queries must run in the db-shell service, never inside the live ingestion
# worker: each exec'd query spent 20-40 MB of the worker's memory (it caused
# the OOM kill in #5147) and failed whenever the worker restarted.

test_exec_target_is_db_shell_not_worker() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    local mock_bin="$tmpdir/bin"
    local calls="$tmpdir/aws_calls"
    local payload="$tmpdir/payload.txt"
    mkdir -p "$mock_bin"
    frame_inline '[{"one":1}]' > "$payload"

    cat > "$mock_bin/aws" << MOCK_AWS
#!/usr/bin/env bash
echo "\$*" >> "$calls"
if [[ "\${1:-}" == "ecs" && "\${2:-}" == "list-tasks" ]]; then
    echo "arn:aws:ecs:us-west-2:000000000000:task/judgemind-dev/dbshelltask"
    exit 0
fi
if [[ "\${1:-}" == "ecs" && "\${2:-}" == "execute-command" ]]; then
    if [[ "\$*" == *"bash -c 'true'"* ]]; then
        exit 0
    fi
    cat "$payload"
    exit 0
fi
echo "Mock aws: unexpected command: \$*" >&2
exit 1
MOCK_AWS
    chmod +x "$mock_bin/aws"

    local stdout rc=0
    stdout=$(PATH="$mock_bin:$PATH" "$DEV_DB_QUERY" "SELECT 1 AS one" 2>/dev/null) || rc=$?
    if [[ $rc -ne 0 || "$stdout" != '[{"one":1}]' ]]; then
        fail "exec target: query round-trip" "rc=$rc stdout=$stdout"
        return
    fi
    if grep -q 'ingestion-worker' "$calls"; then
        fail "exec target: an aws call names the ingestion worker" "calls: $(cat "$calls")"
        return
    fi
    if ! grep -q 'list-tasks .*--service-name judgemind-db-shell-dev' "$calls"; then
        fail "exec target: list-tasks does not look up judgemind-db-shell-dev" "calls: $(cat "$calls")"
        return
    fi
    local exec_calls exec_on_db_shell
    exec_calls=$(grep -c 'execute-command' "$calls" || true)
    exec_on_db_shell=$(grep 'execute-command' "$calls" | grep -c -- '--container db-shell' || true)
    if [[ "$exec_calls" -lt 2 || "$exec_calls" -ne "$exec_on_db_shell" ]]; then
        fail "exec target: every execute-command must target container db-shell" "calls: $(cat "$calls")"
        return
    fi
    pass "exec target: list-tasks + every execute-command go to db-shell, none to the ingestion worker"
}

test_no_db_shell_task_names_service_and_fallback() {
    local mock_bin
    mock_bin=$(setup_mock_aws_no_tasks)
    local output
    output=$(run_script "$mock_bin" "SELECT 1" || true)
    if [[ "$output" == *"no running task found for service judgemind-db-shell-dev"* \
        && "$output" == *"Fix:"* \
        && "$output" == *"scripts/ecs-run-task.sh"* ]]; then
        pass "no db-shell task: error names the service and the one-off fallback"
    else
        fail "no db-shell task: error names the service and the one-off fallback" "got: $output"
    fi
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
test_truncated_stream_exits_nonzero
test_bare_unframed_prefix_exits_nonzero
test_strips_own_line_cannot_perform
test_crlf_pty_output_accepted
test_banner_only_output_exits_nonzero
test_runner_error_exits_nonzero_with_message
test_invalid_inline_max_env_rejected
test_large_result_via_s3_comes_back_whole
test_large_result_truncated_download_exits_nonzero
test_large_result_missing_object_exits_nonzero
test_polls_until_task_appears
test_polls_then_gives_up_with_clear_error
test_exec_agent_probe_success
test_exec_agent_probe_retries
test_exec_agent_probe_deadline_msg
test_probe_hang_times_out_with_fallback_fix
test_query_hang_times_out_with_fallback_fix
test_run_with_timeout_passthrough
test_exec_target_is_db_shell_not_worker
test_no_db_shell_task_names_service_and_fallback

echo ""
echo "────────────────────────────────────────────────"
echo "Ran $TESTS tests, $FAILURES failed"
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
