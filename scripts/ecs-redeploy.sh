#!/usr/bin/env bash
# Force a new ECS deployment and wait for it to reach steady state.
#
# Usage:
#   scripts/ecs-redeploy.sh <service> [<cluster>]
#   scripts/ecs-redeploy.sh <service> [<cluster>] --deployment-id <id>
#
# Arguments:
#   service   ECS service name (required)
#   cluster   ECS cluster name (default: judgemind-dev)
#
# Options:
#   --deployment-id <id>   Do not start a new deployment; keep waiting on
#                          this one.  Use it after an exit 124 (the script
#                          prints the exact command).
#
# Optional environment variables:
#   ROLLOUT_TIMEOUT_SECS   — how long this call waits, in seconds (default:
#                            480, under the Bash tool's 600s cap, #4835)
#   ROLLOUT_POLL_INTERVAL  — polling interval in seconds (default: 10)
#
# Exit codes:
#   0     the deployment completed
#   1     usage error, update-service failed, or the rollout FAILED
#   124   the wait ran out while the deployment is still rolling out.  NOT a
#         failure.  Do NOT re-run the plain command (that would force yet
#         another deployment).  Re-run with `--deployment-id <id>`, as
#         printed, to keep waiting on the same deployment.
#   125   describe-services failed, so the rollout state is unknown.
#         Refresh credentials, then re-run with `--deployment-id <id>`.
#
# The script:
#   1. Runs `aws ecs update-service --force-new-deployment`, capturing the
#      new deployment's ID.
#   2. Waits for THAT specific deployment to reach rolloutState=COMPLETED
#      with running==desired by calling scripts/wait-for-rollout.sh (same
#      polling logic the CI composite action uses — see #2523).
#   3. Prints the new task ID and image digest on success.
#   4. Exits non-zero on failure (crash-loop, rollout FAILED), or 124 when
#      it stopped waiting on a deployment that is still rolling out.

set -euo pipefail
# AWS CLI v1/v2 portability: suppress pager without --no-cli-pager (v2-only flag). See #3461.
export AWS_PAGER=""

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="us-west-2"

SERVICE=""
CLUSTER=""
ATTACH_DEPLOYMENT_ID=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --deployment-id)
            ATTACH_DEPLOYMENT_ID="${2:-}"
            if [[ -z "$ATTACH_DEPLOYMENT_ID" ]]; then
                echo "Error: --deployment-id needs a value." >&2
                exit 1
            fi
            shift 2
            ;;
        -*)
            echo "Error: unknown option: $1" >&2
            exit 1
            ;;
        *)
            if [[ -z "$SERVICE" ]]; then
                SERVICE="$1"
            elif [[ -z "$CLUSTER" ]]; then
                CLUSTER="$1"
            else
                echo "Error: unexpected argument: $1" >&2
                exit 1
            fi
            shift
            ;;
    esac
done
CLUSTER="${CLUSTER:-judgemind-dev}"

if [[ -z "$SERVICE" ]]; then
    echo "Usage: scripts/ecs-redeploy.sh <service> [<cluster>] [--deployment-id <id>]" >&2
    echo "  service   ECS service name (required)" >&2
    echo "  cluster   ECS cluster name (default: judgemind-dev)" >&2
    exit 1
fi

# One call must fit inside the Bash tool's 600s cap (#4835).  CI deploy jobs
# call wait-for-rollout.sh directly and keep its own 900s default.
export ROLLOUT_TIMEOUT_SECS="${ROLLOUT_TIMEOUT_SECS:-480}"

if [[ -n "$ATTACH_DEPLOYMENT_ID" ]]; then
    NEW_DEPLOYMENT_ID="$ATTACH_DEPLOYMENT_ID"
    echo "Re-attaching to deployment $NEW_DEPLOYMENT_ID (no new deployment): cluster=$CLUSTER service=$SERVICE" >&2
else
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
        --output text \
        --query 'service.deployments[0].id')

    if [[ -z "$NEW_DEPLOYMENT_ID" || "$NEW_DEPLOYMENT_ID" == "None" ]]; then
        echo "ERROR: Could not determine new deployment ID from update-service response." >&2
        exit 1
    fi

    echo "Deployment started: $NEW_DEPLOYMENT_ID" >&2
fi

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
# ROLLOUT_TIMEOUT_SECS (480s default, set above) / ROLLOUT_POLL_INTERVAL
# flow through to wait-for-rollout.sh.

ROLLOUT_RC=0
"$REPO_ROOT/scripts/wait-for-rollout.sh" || ROLLOUT_RC=$?
if [[ $ROLLOUT_RC -eq 124 || $ROLLOUT_RC -eq 125 ]]; then
    if [[ $ROLLOUT_RC -eq 124 ]]; then
        echo "Deployment $NEW_DEPLOYMENT_ID is still rolling out. This is NOT a failure." >&2
    else
        echo "Rollout state of deployment $NEW_DEPLOYMENT_ID is unknown (describe-services failed); refresh AWS credentials first." >&2
    fi
    echo "Do NOT re-run the plain command: that would force another deployment." >&2
    echo "To keep waiting on this deployment:" >&2
    echo "  scripts/ecs-redeploy.sh $SERVICE $CLUSTER --deployment-id $NEW_DEPLOYMENT_ID" >&2
    exit "$ROLLOUT_RC"
fi
if [[ $ROLLOUT_RC -ne 0 ]]; then
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
    --query 'tasks[*].{TaskId: taskArn, Status: lastStatus, Image: containers[0].image, ImageDigest: containers[0].imageDigest}'

echo "Deployment complete." >&2
