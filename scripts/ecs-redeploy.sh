#!/usr/bin/env bash
# Force a new ECS deployment and wait for it to reach steady state.
#
# Usage:
#   scripts/ecs-redeploy.sh <service> [<cluster>]
#
# Arguments:
#   service   ECS service name (required)
#   cluster   ECS cluster name (default: judgemind-dev)
#
# The script:
#   1. Runs `aws ecs update-service --force-new-deployment`
#   2. Waits for steady state via `aws ecs wait services-stable`
#   3. Prints the new task ID and image digest on success
#   4. Exits non-zero on failure (e.g. crash-loop, timeout)

set -euo pipefail

REGION="us-west-2"

SERVICE="${1:-}"
CLUSTER="${2:-judgemind-dev}"

if [[ -z "$SERVICE" ]]; then
    echo "Usage: scripts/ecs-redeploy.sh <service> [<cluster>]" >&2
    echo "  service   ECS service name (required)" >&2
    echo "  cluster   ECS cluster name (default: judgemind-dev)" >&2
    exit 1
fi

echo "Forcing new deployment: cluster=$CLUSTER service=$SERVICE" >&2

# Step 1 — Force a new deployment
aws ecs update-service \
    --cluster "$CLUSTER" \
    --service "$SERVICE" \
    --force-new-deployment \
    --region "$REGION" \
    --no-cli-pager \
    --output text \
    --query 'service.deployments[0].id' \
    | while read -r deployment_id; do
        echo "Deployment started: $deployment_id" >&2
    done

# Step 2 — Wait for steady state (max 40 polls x 15s = 10 minutes)
echo "Waiting for service to reach steady state..." >&2

if ! aws ecs wait services-stable \
    --cluster "$CLUSTER" \
    --services "$SERVICE" \
    --region "$REGION"; then
    echo "ERROR: Service did not reach steady state within the timeout." >&2
    echo "Check the ECS console for crash-loop or deployment issues." >&2
    exit 1
fi

echo "Service reached steady state." >&2

# Step 3 — Print the running task ID and image digest
TASK_ARNS=$(aws ecs list-tasks \
    --cluster "$CLUSTER" \
    --service-name "$SERVICE" \
    --region "$REGION" \
    --desired-status RUNNING \
    --output text \
    --query 'taskArns[*]')

if [[ -z "$TASK_ARNS" ]]; then
    echo "WARNING: No running tasks found for service $SERVICE" >&2
    exit 0
fi

# Describe tasks to get task IDs and image digests
aws ecs describe-tasks \
    --cluster "$CLUSTER" \
    --tasks $TASK_ARNS \
    --region "$REGION" \
    --output table \
    --no-cli-pager \
    --query 'tasks[*].{TaskId: taskArn, Status: lastStatus, Image: containers[0].image, ImageDigest: containers[0].imageDigest}'

echo "Deployment complete." >&2
