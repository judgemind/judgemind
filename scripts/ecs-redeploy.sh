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
# Optional environment variables:
#   ROLLOUT_TIMEOUT_SECS   — overall timeout in seconds (default: 900)
#   ROLLOUT_POLL_INTERVAL  — polling interval in seconds (default: 10)
#
# The script:
#   1. Runs `aws ecs update-service --force-new-deployment`, capturing the
#      new deployment's ID.
#   2. Waits for THAT specific deployment to reach rolloutState=COMPLETED
#      with running==desired by calling scripts/wait-for-rollout.sh (same
#      polling logic the CI composite action uses — see #2523).
#   3. Prints the new task ID and image digest on success.
#   4. Exits non-zero on failure (crash-loop, rollout FAILED, timeout).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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

# Step 1 — Force a new deployment and capture the new deployment ID.
#
# `--force-new-deployment` does not change the task-def ARN; it creates a
# new deployment record with a fresh ID that re-pulls the image and rolls
# replacement tasks. deployments[0] is the PRIMARY (new) deployment that
# update-service just created.
NEW_DEPLOYMENT_ID=$(aws ecs update-service \
    --cluster "$CLUSTER" \
    --service "$SERVICE" \
    --force-new-deployment \
    --region "$REGION" \
    --no-cli-pager \
    --output text \
    --query 'service.deployments[0].id')

if [[ -z "$NEW_DEPLOYMENT_ID" || "$NEW_DEPLOYMENT_ID" == "None" ]]; then
    echo "ERROR: Could not determine new deployment ID from update-service response." >&2
    exit 1
fi

echo "Deployment started: $NEW_DEPLOYMENT_ID" >&2

# Step 2 — Wait for OUR deployment to reach COMPLETED.
#
# Replaces `aws ecs wait services-stable` (hard-coded 10-min cap, no
# progress output, waits on service-level steady state rather than on
# our specific deployment). See issue #2523 for the full motivation.
#
# Progress lines `[<N>s] rolloutState=…` are emitted while waiting.
# On timeout or rolloutState=FAILED, the helper dumps a diagnostic
# service JSON snapshot and exits non-zero.
export AWS_DEFAULT_REGION="$REGION"
export ECS_CLUSTER="$CLUSTER"
export ECS_SERVICE="$SERVICE"
export NEW_DEPLOYMENT_ID
# ROLLOUT_TIMEOUT_SECS / ROLLOUT_POLL_INTERVAL flow through if set by caller;
# otherwise wait-for-rollout.sh applies its defaults (900s / 10s).

if ! "$REPO_ROOT/scripts/wait-for-rollout.sh"; then
    echo "ERROR: Deployment did not complete successfully." >&2
    echo "Check the ECS console for crash-loop or deployment issues." >&2
    exit 1
fi

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
