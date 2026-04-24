#!/usr/bin/env bash
# dev-db-query.sh — Run a SQL query against the dev RDS database via ECS Exec.
#
# The dev RDS instance lives in a private VPC subnet and is not reachable from
# local machines. This script uses ECS Exec to run psql inside the already-
# running ingestion worker container, which has DATABASE_URL set and network
# access to the database.
#
# Prerequisites:
#   - AWS CLI v2 with the Session Manager plugin installed
#   - Credentials for the judgemind AWS account (155326049300)
#   - The ingestion worker ECS service must be running with execute command enabled
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

set -euo pipefail

CLUSTER="judgemind-dev"
SERVICE="judgemind-ingestion-worker-dev"
CONTAINER="ingestion-worker"
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

task_arn=$(aws ecs list-tasks \
    --cluster "$CLUSTER" \
    --service-name "$SERVICE" \
    --desired-status RUNNING \
    --region "$REGION" \
    --query 'taskArns[0]' \
    --output text)

if [[ -z "$task_arn" || "$task_arn" == "None" ]]; then
    echo "Error: no running task found for service $SERVICE in cluster $CLUSTER" >&2
    echo "Check that the ingestion worker is running:" >&2
    echo "  aws ecs describe-services --cluster $CLUSTER --services $SERVICE --region $REGION --query 'services[0].runningCount'" >&2
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

aws ecs execute-command \
    --cluster "$CLUSTER" \
    --task "$task_arn" \
    --container "$CONTAINER" \
    --interactive \
    --region "$REGION" \
    --command "bash -c 'echo $runner_encoded | base64 -d > $remote_script && python3 $remote_script $query_b64 $readonly_flag; rm -f $remote_script'"
