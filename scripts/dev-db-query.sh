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
#
# For an interactive psql session (no query argument):
#   scripts/dev-db-query.sh

set -euo pipefail

CLUSTER="judgemind-dev"
SERVICE="judgemind-ingestion-worker-dev"
CONTAINER="ingestion-worker"
REGION="us-west-2"

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
    # Interactive psql session
    echo "Connecting to dev database via ECS Exec (interactive psql)..." >&2
    echo "Task: $task_arn" >&2
    echo "" >&2

    aws ecs execute-command \
        --cluster "$CLUSTER" \
        --task "$task_arn" \
        --container "$CONTAINER" \
        --interactive \
        --region "$REGION" \
        --command "psql \$DATABASE_URL"
else
    # Run a single query and return results
    query="$1"

    echo "Running query on dev database via ECS Exec..." >&2
    echo "Task: $task_arn" >&2
    echo "" >&2

    aws ecs execute-command \
        --cluster "$CLUSTER" \
        --task "$task_arn" \
        --container "$CONTAINER" \
        --interactive \
        --region "$REGION" \
        --command "psql \$DATABASE_URL -c \"$query\""
fi
