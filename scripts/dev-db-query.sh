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
#   scripts/dev-db-query.sh [--verbose|-v] "SELECT COUNT(*) FROM rulings"
#   scripts/dev-db-query.sh "SELECT id, case_number FROM rulings LIMIT 5"
#   scripts/dev-db-query.sh "SELECT * FROM courts WHERE state = 'CA'"
#
# By default, the session is set to read-only mode (SET default_transaction_read_only = on)
# so that only SELECT/EXPLAIN queries succeed. Use --rw to allow writes:
#   scripts/dev-db-query.sh --rw "UPDATE rulings SET status = 'active' WHERE id = 1"
#
# Options:
#   --verbose, -v   Show task ARN and progress lines (default: suppress)
#
# Environment:
#   JM_VERBOSE=1    Same as --verbose

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_terse_lib.sh
source "$SCRIPT_DIR/_terse_lib.sh"

CLUSTER="judgemind-dev"
SERVICE="judgemind-ingestion-worker-dev"
CONTAINER="ingestion-worker"
REGION="us-west-2"

# ─── Parse flags ──────────────────────────────────────────────────────────────

READ_ONLY=true
if [[ "${1:-}" == "--rw" ]]; then
    READ_ONLY=false
    shift
fi

# Parse --verbose/-v (JM_VERBOSE already handled by _terse_lib.sh source)
if [[ "${1:-}" == "--verbose" || "${1:-}" == "-v" ]]; then
    VERBOSE=1
    shift
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

if [[ $# -eq 0 ]]; then
    echo "Usage: scripts/dev-db-query.sh [--rw] \"SELECT COUNT(*) FROM rulings\"" >&2
    exit 1
fi

query="$1"

vlog "Running query on dev database via ECS Exec..."
vlog "Task: $task_arn"

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

runner_encoded=$(base64 < "$SCRIPT_DIR/dev_db_query_runner.py" | tr -d '\n')
remote_script="/tmp/_dev_db_query_runner.py"

if [[ "${VERBOSE:-0}" == "1" ]]; then
    aws ecs execute-command \
        --cluster "$CLUSTER" \
        --task "$task_arn" \
        --container "$CONTAINER" \
        --interactive \
        --region "$REGION" \
        --command "bash -c 'echo $runner_encoded | base64 -d > $remote_script && python3 $remote_script $query_b64 $readonly_flag; rm -f $remote_script'"
else
    # Filter Session Manager plugin chatter from stdout; JSON result passes through.
    aws ecs execute-command \
        --cluster "$CLUSTER" \
        --task "$task_arn" \
        --container "$CONTAINER" \
        --interactive \
        --region "$REGION" \
        --command "bash -c 'echo $runner_encoded | base64 -d > $remote_script && python3 $remote_script $query_b64 $readonly_flag; rm -f $remote_script'" \
    | awk '
        /^[[:space:]]*$/ { blank=1; next }
        /The Session Manager plugin will handle the SSM stream data/ { blank=0; next }
        /Starting session with SessionId/ { blank=0; next }
        /Cannot perform start session/ { blank=0; next }
        { if (blank) { print ""; blank=0 } print }
    '
fi
