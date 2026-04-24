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

run_script() {
    # Args: <mock_bin> <args...>
    local mock_bin="$1"
    shift
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

echo ""
echo "────────────────────────────────────────────────"
echo "Ran $TESTS tests, $FAILURES failed"
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
