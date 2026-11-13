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

# The budget covers the whole call: update-service, the rollout wait, and
# the closing task summary.  Every aws call gets a per-call timeout sized to
# the time left, and slow calls are logged (#4850).
# shellcheck source=scripts/_wait_budget_lib.sh
source "$REPO_ROOT/scripts/_wait_budget_lib.sh"
REDEPLOY_BUDGET_SECS="$ROLLOUT_TIMEOUT_SECS"
wait_budget_init "$REDEPLOY_BUDGET_SECS"
# Seconds kept back from the rollout wait for the closing task summary.
SUMMARY_RESERVE_SECS=20

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
    UPDATE_RC=0
    NEW_DEPLOYMENT_ID=$(wait_budget_call "aws ecs update-service" \
        aws ecs update-service \
        --cluster "$CLUSTER" \
        --service "$SERVICE" \
        --force-new-deployment \
        --region "$REGION" \
        --output text \
        --query 'service.deployments[0].id') || UPDATE_RC=$?

    if [[ $UPDATE_RC -eq $WAIT_CALL_TIMEOUT_RC ]]; then
        echo "ERROR: aws ecs update-service hung and was killed, so it is unknown whether a new deployment started." >&2
        echo "Fix: find the newest deployment id, then wait on it instead of forcing another:" >&2
        echo "  aws ecs describe-services --cluster $CLUSTER --services $SERVICE --region $REGION --query 'services[0].deployments[0].id' --output text" >&2
        echo "  scripts/ecs-redeploy.sh $SERVICE $CLUSTER --deployment-id <id>" >&2
        exit 125
    fi
    if [[ $UPDATE_RC -ne 0 || -z "$NEW_DEPLOYMENT_ID" || "$NEW_DEPLOYMENT_ID" == "None" ]]; then
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
# The rollout wait gets what is left of the budget, minus a reserve for the
# closing task summary (#4850).  ROLLOUT_POLL_INTERVAL flows through as-is.
_rollout_secs=$(( $(wait_budget_remaining) - SUMMARY_RESERVE_SECS ))
if [[ $_rollout_secs -lt 0 ]]; then
    _rollout_secs=0
fi
export ROLLOUT_TIMEOUT_SECS="$_rollout_secs"

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
# The deployment already completed, so this summary is best-effort: a slow
# or failing call is reported but never turns success into failure (#4850).
TASK_ARNS=""
if ! TASK_ARNS=$(wait_budget_call --max 15 "aws ecs list-tasks" \
    aws ecs list-tasks \
    --cluster "$CLUSTER" \
    --service-name "$SERVICE" \
    --region "$REGION" \
    --desired-status RUNNING \
    --output text \
    --query 'taskArns[*]'); then
    echo "WARNING: could not list running tasks for $SERVICE (deployment itself completed)." >&2
    echo "Deployment complete." >&2
    exit 0
fi

if [[ -z "$TASK_ARNS" ]]; then
    echo "WARNING: No running tasks found for service $SERVICE" >&2
    exit 0
fi

# Describe tasks to get task IDs and image digests
# shellcheck disable=SC2086  # TASK_ARNS is a whitespace-separated list.
wait_budget_call --max 15 "aws ecs describe-tasks" \
    aws ecs describe-tasks \
    --cluster "$CLUSTER" \
    --tasks $TASK_ARNS \
    --region "$REGION" \
    --output table \
    --query 'tasks[*].{TaskId: taskArn, Status: lastStatus, Image: containers[0].image, ImageDigest: containers[0].imageDigest}' \
    || echo "WARNING: could not describe running tasks (deployment itself completed)." >&2

echo "Deployment complete." >&2
