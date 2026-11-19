#!/usr/bin/env bash
# dev-db-query.sh — Run a SQL query against the dev RDS database via ECS Exec.
#
# The dev RDS instance lives in a private VPC subnet and is not reachable from
# local machines. This script uses ECS Exec to run a small Python query runner
# inside the always-on `judgemind-db-shell-dev` service, which has
# DATABASE_URL set and network access to the database
# (infra/terraform/modules/compute/db_shell.tf).
#
# It never execs into the live ingestion worker (#5168): each query starts a
# python3 + SSM session worker in the target container, which cost the worker
# 20-40 MB of its 512 MB per query (one triggered the OOM kill in #5147), and
# queries failed whenever the worker restarted. The db-shell service does no
# work of its own and does not restart on worker deploys.
#
# Prerequisites:
#   - AWS CLI v2 with the Session Manager plugin installed
#   - Credentials for the judgemind AWS account (155326049300)
#   - The db-shell ECS service must be running (it has execute command enabled)
#
# Usage:
#   scripts/dev-db-query.sh "SELECT COUNT(*) FROM rulings"
#   scripts/dev-db-query.sh "SELECT id, case_number FROM rulings LIMIT 5"
#   scripts/dev-db-query.sh "SELECT * FROM courts WHERE state = 'CA'"
#   scripts/dev-db-query.sh --file path/to/query.sql
#
# By default, the session is set to read-only mode (SET default_transaction_read_only = on)
# so that only SELECT/EXPLAIN queries succeed. Use --rw to allow writes:
#   scripts/dev-db-query.sh --rw "UPDATE rulings SET status = 'active' WHERE id = 1"
#   scripts/dev-db-query.sh --rw --file path/to/update.sql
#
# Timeouts (#4665). Every `aws ecs execute-command` call has a hard per-call
# timeout, so a wedged SSM session fails with a Fix: block pointing at the
# scripts/ecs-run-task.sh one-off fallback instead of hanging:
#   EXEC_PROBE_TIMEOUT_SECS       per readiness probe (default 30)
#   EXEC_AGENT_POLL_TIMEOUT_SECS  overall readiness deadline (default 120)
#   EXEC_QUERY_TIMEOUT_SECS       the query itself (default 300)
#
# Results of any size arrive complete, or the script fails (#4781). The SSM
# transport cuts output off anywhere from ~1 KB to ~10 KB and still exits 0,
# so the runner frames every result with a byte count and sha256. Results
# over 512 bytes are
# staged in s3://<archive-bucket>/tmp/dev-db-query/ and downloaded here
# (needs local `aws s3 cp` access to that bucket; the object is deleted
# after download). A truncated stream, a runner error, or a failed
# download exits 1 with a Fix: block and prints nothing to stdout.
#   DEV_DB_QUERY_INLINE_MAX_BYTES  inline/S3 cutover (default 512); set it
#                                  very high to force a large result inline
#                                  and see the truncation check fire.

set -euo pipefail

CLUSTER="judgemind-dev"
# The exec target. Never the ingestion worker (#5168): see the header.
SERVICE="judgemind-db-shell-dev"
CONTAINER="db-shell"
REGION="us-west-2"

usage() {
    cat >&2 <<'USAGE'
Usage: scripts/dev-db-query.sh [--rw] "SQL query"
       scripts/dev-db-query.sh [--rw] --file <path.sql>

Flags:
  --rw              Allow writes (default: read-only session).
  --file <path>     Read the SQL query from a file instead of the command line.

Examples:
  scripts/dev-db-query.sh "SELECT count(*) FROM derived.rulings"
  scripts/dev-db-query.sh --file tmp/check.sql
  scripts/dev-db-query.sh --rw --file tmp/update.sql
USAGE
}

# ─── Parse flags ──────────────────────────────────────────────────────────────

READ_ONLY=true
QUERY_FILE=""

# Only leading args that exactly match a known flag are consumed. A raw SQL
# query that happens to start with `-` (e.g. "-- a comment" or an EXPLAIN
# variant) must still be accepted as the positional query. When unsure,
# operators can pass `--` before the query to force positional parsing.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --rw)
            READ_ONLY=false
            shift
            ;;
        --file)
            if [[ $# -lt 2 || -z "${2:-}" ]]; then
                echo "Error: --file requires a path argument." >&2
                usage
                exit 1
            fi
            QUERY_FILE="$2"
            shift 2
            ;;
        --file=*)
            QUERY_FILE="${1#--file=}"
            if [[ -z "$QUERY_FILE" ]]; then
                echo "Error: --file requires a path argument." >&2
                usage
                exit 1
            fi
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        *)
            # Anything we don't recognise (including strings starting with `-`)
            # is treated as the start of the positional query. This preserves
            # support for quoted SQL that starts with a comment or operator.
            break
            ;;
    esac
done

# ─── Resolve the query source ────────────────────────────────────────────────
# Must happen before we hit AWS so bad invocations fail fast.

if [[ -n "$QUERY_FILE" ]]; then
    if [[ $# -gt 0 ]]; then
        echo "Error: cannot combine --file with a positional query argument." >&2
        usage
        exit 1
    fi
    if [[ ! -f "$QUERY_FILE" ]]; then
        echo "Error: SQL file not found: $QUERY_FILE" >&2
        exit 1
    fi
    query="$(cat -- "$QUERY_FILE")"
elif [[ $# -ge 1 ]]; then
    query="$1"
else
    echo "Error: no SQL query provided." >&2
    usage
    exit 1
fi

# Reject queries that are empty or comment-only. Psycopg silently succeeds on
# those with description=None and rowcount=-1, which used to leak out as a
# misleading {"rowcount": -1} response (#2862).
#
# sed is invoked per-line so `--.*` safely matches to end-of-line without
# needing `[^\n]` (which behaves differently on BSD/macOS sed). The second
# expression strips single-line /* ... */ block comments; multi-line block
# comments are not worth the portable-sed gymnastics — a query that consists
# solely of a multi-line block comment will reach the Python runner, which has
# its own (belt-and-suspenders) guard.
stripped="$(
    printf '%s\n' "$query" \
        | sed -E 's|--.*||' \
        | sed -E 's|/\*[^*]*\*+([^/*][^*]*\*+)*/||g' \
        | tr -d '[:space:];'
)"
if [[ -z "$stripped" ]]; then
    echo "Error: SQL query is empty or contains only comments." >&2
    exit 1
fi

# ─── Resolve a running task ARN ──────────────────────────────────────────────
# Poll list-tasks for up to LIST_TASKS_POLL_TIMEOUT_SECS (default 60 s),
# retrying every 3 s. Defense-in-depth for a rolling deploy: even with the
# 100/200 deploy policy, an operator-issued force-new-deployment can briefly
# leave ECS in a state where the new task is PENDING and list-tasks returns
# nothing. Without retries a transient empty response would abort the query.
# Mirrors the polling pattern in scripts/wait-for-rollout.sh.

LIST_TASKS_POLL_TIMEOUT_SECS="${LIST_TASKS_POLL_TIMEOUT_SECS:-60}"
_list_tasks_start=$(date +%s)
_list_tasks_deadline=$((_list_tasks_start + LIST_TASKS_POLL_TIMEOUT_SECS))

task_arn=""
while true; do
    task_arn=$(aws ecs list-tasks \
        --cluster "$CLUSTER" \
        --service-name "$SERVICE" \
        --desired-status RUNNING \
        --region "$REGION" \
        --query 'taskArns[0]' \
        --output text)

    if [[ -n "$task_arn" && "$task_arn" != "None" ]]; then
        break
    fi

    _now=$(date +%s)
    if [[ "$_now" -ge "$_list_tasks_deadline" ]]; then
        echo "Error: no running task found for service $SERVICE in cluster $CLUSTER" >&2
        echo "Fix: the db-shell service (infra/terraform/modules/compute/db_shell.tf) runs on" >&2
        echo "FARGATE_SPOT; after a Spot interruption ECS places a new task within a few minutes." >&2
        echo "  - check it: aws ecs describe-services --cluster $CLUSTER --services $SERVICE --region $REGION --query 'services[0].{running:runningCount,events:events[0:3]}'" >&2
        echo "  - retry in a minute or two, or run the query as a one-off task:" >&2
        echo "      scripts/ecs-run-task.sh tmp/q.py   (a small psycopg script reading DATABASE_URL)" >&2
        exit 1
    fi

    _elapsed=$((_now - _list_tasks_start))
    echo "No running task yet (${_elapsed}s elapsed), retrying in 3 s..." >&2
    sleep 3
done

# ─── Wait for ECS Exec agent to be ready ────────────────────────────────────
# Even when list-tasks returns a running ARN the execute-command agent inside
# the container may not be accepting connections yet (common during a rolling
# deploy). Poll with a harmless probe before running the real query so the
# user sees clear retry progress instead of the raw AWS error.

# shellcheck source=scripts/_ecs_exec_lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_ecs_exec_lib.sh"

if ! wait_for_exec_agent_ready "$CLUSTER" "$task_arn" "$CONTAINER" "$REGION"; then
    exit 1
fi

# ─── Build the command ───────────────────────────────────────────────────────

echo "Running query on dev database via ECS Exec..." >&2
echo "Task: $task_arn" >&2
echo "" >&2

# Base64-encode the query to avoid quoting issues with single/double quotes
# in SQL (e.g. WHERE status = 'active'). The runner script decodes it.
query_b64=$(printf '%s' "$query" | base64 | tr -d '\n')

# When READ_ONLY is true (the default), the runner script sets the session to
# read-only mode before executing the user's query, so PostgreSQL itself
# rejects any write attempt — defense-in-depth on top of the application-
# level SQL keyword blocklist.
if [[ "$READ_ONLY" == "true" ]]; then
    readonly_flag="1"
else
    readonly_flag="0"
fi

# ─── Upload and run the Python query runner ──────────────────────────────────
# The query logic lives in scripts/dev_db_query_runner.py.  We base64-encode
# the script, decode it to a temp file on the container, and run it — the same
# pattern used by ecs-run.sh --script.  This avoids the fragile one-liner that
# was previously embedded in the --command string.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
runner_encoded=$(base64 < "$SCRIPT_DIR/dev_db_query_runner.py" | tr -d '\n')
remote_script="/tmp/_dev_db_query_runner.py"

# The tail `sleep 1` gives the SSM / ECS-exec transport a chance to deliver
# the final output chunk before the remote bash exits and the session closes.
# Without this, large payloads occasionally land with mid-field truncation +
# an `Exiting session with sessionId:` or `Cannot perform start session: EOF`
# trailer (#3195). The compact-flag (3rd arg to the runner) further reduces
# bytes-in-flight so the payload is less likely to exceed SSM's per-session
# output budget in the first place.
#
# The capture is then handed to dev_db_query_result.py, which extracts the
# runner's framed result (see below and #4781).
#
# stderr preserves the original log messages so callers can still see
# `Running query on dev database via ECS Exec...` etc.
#
# The call is bounded by EXEC_QUERY_TIMEOUT_SECS (default 300 s) so a stalled
# SSM session fails loudly instead of hanging forever (#4665).
EXEC_QUERY_TIMEOUT_SECS="${EXEC_QUERY_TIMEOUT_SECS:-300}"

# Results larger than this many bytes are staged in S3 rather than printed
# over SSM (#4781). Raising it far past ~10 KB forces a large result through
# the inline path, which is how to check that a real SSM truncation is
# caught (the script must exit non-zero).
inline_max="${DEV_DB_QUERY_INLINE_MAX_BYTES:-512}"
if [[ ! "$inline_max" =~ ^[0-9]+$ ]]; then
    echo "Error: DEV_DB_QUERY_INLINE_MAX_BYTES must be a non-negative integer, got '$inline_max'." >&2
    exit 1
fi

aws_rc=0
aws_output=$(ecs_exec_run_with_timeout "$EXEC_QUERY_TIMEOUT_SECS" \
    aws ecs execute-command \
    --cluster "$CLUSTER" \
    --task "$task_arn" \
    --container "$CONTAINER" \
    --interactive \
    --region "$REGION" \
    --command "bash -c 'echo $runner_encoded | base64 -d > $remote_script && python3 $remote_script $query_b64 $readonly_flag 1 $inline_max; sync; rm -f $remote_script; sleep 1'") \
    || aws_rc=$?

if [[ $aws_rc -eq $ECS_EXEC_TIMEOUT_RC ]]; then
    echo "Error: query via ECS Exec did not finish within ${EXEC_QUERY_TIMEOUT_SECS}s (task $task_arn). The SSM session probably stalled; the query may or may not have run." >&2
    ecs_exec_print_fallback_fix
    exit 1
fi
if [[ $aws_rc -ne 0 ]]; then
    exit "$aws_rc"
fi

# Unwrap and check the framed result (#4781). The SSM transport cuts output
# off at ~1-10 KB and still exits 0, so the capture is never trusted as-is:
# dev_db_query_result.py finds the runner's BEGIN/END frame, downloads the
# S3-staged payload for large results, and checks the byte count and
# sha256. Only a complete, verified payload reaches stdout; anything else
# (truncated stream, runner error, failed download) exits 1 with a Fix:
# block, and stdout stays empty. The SSM banner/trailer lines fall outside
# the frame, so they never reach stdout either.
printf '%s\n' "$aws_output" | python3 "$SCRIPT_DIR/dev_db_query_result.py" --region "$REGION"
