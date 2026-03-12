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
#
# By default, the session is set to read-only mode (SET default_transaction_read_only = on)
# so that only SELECT/EXPLAIN queries succeed. Use --rw to allow writes:
#   scripts/dev-db-query.sh --rw "UPDATE rulings SET status = 'active' WHERE id = 1"

set -euo pipefail

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

# ─── Resolve a running task ARN ──────────────────────────────────────────────

task_arn=$(aws ecs list-tasks \
    --cluster "$CLUSTER" \
    --service-name "$SERVICE" \
    --desired-status RUNNING \
    --region "$REGION" \
    --query 'taskArns[0]' \
    --output text 2>/dev/null)

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

echo "Running query on dev database via ECS Exec..." >&2
echo "Task: $task_arn" >&2
echo "" >&2

# Base64-encode the query to avoid quoting issues with single/double quotes
# in SQL (e.g. WHERE status = 'active'). The Python one-liner decodes it.
query_b64=$(printf '%s' "$query" | base64 | tr -d '\n')

# When READ_ONLY is true (the default), the Python code sets the session to
# read-only mode before executing the user's query, so PostgreSQL itself
# rejects any write attempt — defense-in-depth on top of the application-
# level SQL keyword blocklist.
if [[ "$READ_ONLY" == "true" ]]; then
    readonly_flag="1"
else
    readonly_flag="0"
fi

# Use Python + psycopg (already installed in the container) instead of psql
aws ecs execute-command \
    --cluster "$CLUSTER" \
    --task "$task_arn" \
    --container "$CONTAINER" \
    --interactive \
    --region "$REGION" \
    --command "python3 -c \"import os,psycopg,json,base64;q=base64.b64decode('${query_b64}').decode();c=psycopg.connect(os.environ['DATABASE_URL']);r=c.cursor();r.execute('SET default_transaction_read_only = on') if '${readonly_flag}'=='1' else None;r.execute(q);print(json.dumps([dict(zip([d[0] for d in r.description],row)) for row in r.fetchall()],indent=2,default=str)) if r.description else print(json.dumps({'rowcount':r.rowcount}))\""
