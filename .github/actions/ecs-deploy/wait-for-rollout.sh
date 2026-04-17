#!/usr/bin/env bash
# wait-for-rollout.sh — Poll an ECS service deployment until the specified task
# definition's rollout reaches a terminal state (COMPLETED or FAILED), or until
# the configured timeout elapses.
#
# Called from .github/actions/ecs-deploy/action.yml's "Update ECS service" step.
# Extracted into a standalone script so it can be shellchecked and unit-tested
# with a mock aws CLI. See issue #2519.
#
# Required environment variables:
#   ECS_CLUSTER        — ECS cluster name
#   ECS_SERVICE        — ECS service name
#   NEW_TASK_DEF_ARN   — Task definition ARN we are waiting to roll out
#
# Optional environment variables:
#   ROLLOUT_TIMEOUT_SECS   — overall timeout in seconds (default: 900)
#   ROLLOUT_POLL_INTERVAL  — polling interval in seconds (default: 10)
#   AWS_CLI                — binary name for aws CLI (default: aws; used for
#                            mocking in tests)
#
# Exit codes:
#   0 — Target deployment reached rolloutState=COMPLETED with runningCount==
#       desiredCount (and desiredCount > 0).
#   1 — rolloutState=FAILED, or the timeout elapsed, or the aws CLI failed.

set -euo pipefail

: "${ECS_CLUSTER:?ECS_CLUSTER is required}"
: "${ECS_SERVICE:?ECS_SERVICE is required}"
: "${NEW_TASK_DEF_ARN:?NEW_TASK_DEF_ARN is required}"

ROLLOUT_TIMEOUT_SECS="${ROLLOUT_TIMEOUT_SECS:-900}"
ROLLOUT_POLL_INTERVAL="${ROLLOUT_POLL_INTERVAL:-10}"
AWS_CLI="${AWS_CLI:-aws}"

echo "Waiting for task definition $NEW_TASK_DEF_ARN to roll out on ${ECS_CLUSTER}/${ECS_SERVICE} (timeout: ${ROLLOUT_TIMEOUT_SECS}s, poll every ${ROLLOUT_POLL_INTERVAL}s)..."

START_TS=$(date +%s)
DEADLINE=$((START_TS + ROLLOUT_TIMEOUT_SECS))
LAST_STATE=""
LAST_COUNTS=""

while true; do
  NOW_TS=$(date +%s)
  ELAPSED=$((NOW_TS - START_TS))

  if ! SERVICE_JSON=$("$AWS_CLI" ecs describe-services \
        --cluster "$ECS_CLUSTER" \
        --services "$ECS_SERVICE" \
        --output json); then
    echo "ERROR: aws ecs describe-services failed after ${ELAPSED}s."
    exit 1
  fi

  DEPLOYMENT=$(echo "$SERVICE_JSON" | jq --arg arn "$NEW_TASK_DEF_ARN" \
    '.services[0].deployments[] | select(.taskDefinition == $arn)')

  if [ -z "$DEPLOYMENT" ]; then
    # Our deployment has not yet been registered in the deployments list.
    # Give ECS a moment — retry.
    echo "[${ELAPSED}s] Deployment for new task def not yet visible, retrying..."
  else
    ROLLOUT_STATE=$(echo "$DEPLOYMENT" | jq -r '.rolloutState // "UNKNOWN"')
    ROLLOUT_REASON=$(echo "$DEPLOYMENT" | jq -r '.rolloutStateReason // ""')
    RUNNING_COUNT=$(echo "$DEPLOYMENT" | jq -r '.runningCount')
    DESIRED_COUNT=$(echo "$DEPLOYMENT" | jq -r '.desiredCount')
    PENDING_COUNT=$(echo "$DEPLOYMENT" | jq -r '.pendingCount')
    STATE_SIG="${ROLLOUT_STATE}|running=${RUNNING_COUNT}|desired=${DESIRED_COUNT}|pending=${PENDING_COUNT}"
    if [ "$STATE_SIG" != "$LAST_COUNTS" ]; then
      echo "[${ELAPSED}s] rolloutState=${ROLLOUT_STATE} running=${RUNNING_COUNT} desired=${DESIRED_COUNT} pending=${PENDING_COUNT} ${ROLLOUT_REASON:+(${ROLLOUT_REASON})}"
      LAST_COUNTS="$STATE_SIG"
    fi

    case "$ROLLOUT_STATE" in
      COMPLETED)
        if [ "$RUNNING_COUNT" = "$DESIRED_COUNT" ] && [ "$RUNNING_COUNT" != "0" ]; then
          echo "Service updated successfully after ${ELAPSED}s."
          exit 0
        fi
        ;;
      FAILED)
        echo "ERROR: ECS reported rolloutState=FAILED for $NEW_TASK_DEF_ARN after ${ELAPSED}s."
        echo "Reason: $ROLLOUT_REASON"
        echo "Full deployment JSON:"
        echo "$DEPLOYMENT" | jq '.'
        exit 1
        ;;
      IN_PROGRESS|UNKNOWN)
        : # keep polling
        ;;
    esac
    LAST_STATE="$ROLLOUT_STATE"
  fi

  if [ "$NOW_TS" -ge "$DEADLINE" ]; then
    echo "ERROR: Timed out after ${ROLLOUT_TIMEOUT_SECS}s waiting for $NEW_TASK_DEF_ARN to stabilize."
    echo "Last observed state: ${LAST_STATE:-<none>} (${LAST_COUNTS:-<no counts>})"
    echo "Full service JSON snapshot:"
    echo "$SERVICE_JSON" | jq '.services[0] | {serviceName, desiredCount, runningCount, pendingCount, deployments, events: (.events[:5])}'
    exit 1
  fi

  sleep "$ROLLOUT_POLL_INTERVAL"
done
