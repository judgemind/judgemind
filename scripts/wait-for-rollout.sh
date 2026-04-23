#!/usr/bin/env bash
# wait-for-rollout.sh — Poll an ECS service deployment until a specific
# deployment reaches a terminal state (COMPLETED or FAILED) with
# runningCount == desiredCount, or until the configured timeout elapses.
#
# Shared between two callers:
#   1. .github/actions/ecs-deploy/action.yml — waits for a newly registered
#      task-definition ARN to roll out (CI deploy path, see #2519).
#   2. scripts/ecs-redeploy.sh — waits for a newly triggered
#      --force-new-deployment deployment to roll out (operator manual
#      redeploy path, see #2523). Same task-def ARN as before, but a new
#      deployment ID.
#
# IMPORTANT — paths-filter contract (see #2592):
#   Any deploy workflow that adopts this script (directly or via the
#   ecs-deploy composite action) MUST add `scripts/wait-for-rollout.sh`
#   to its `on.push.paths:` filter. Otherwise a PR that changes only this
#   shared script will merge without triggering any deploy workflow — so
#   the fix is not exercised against a real rollout on its own PR, and
#   regressions surface on the next unrelated deploy. Current callers:
#     - .github/workflows/deploy-scraper.yml
#     - .github/workflows/deploy-api.yml
#
# Identifying the deployment:
#   - CI path knows the new task-def ARN up front.
#   - Operator redeploy path does not change the task-def ARN; only a new
#     deployment ID is created. So we support selecting the deployment by
#     either ARN or deployment ID — exactly one must be provided.
#
# Required environment variables:
#   ECS_CLUSTER          — ECS cluster name
#   ECS_SERVICE          — ECS service name
#
# One of:
#   NEW_TASK_DEF_ARN     — Task definition ARN to select the deployment by
#   NEW_DEPLOYMENT_ID    — Deployment ID to select the deployment by
#
# Optional environment variables:
#   ROLLOUT_TIMEOUT_SECS   — overall timeout in seconds (default: 900)
#   ROLLOUT_POLL_INTERVAL  — polling interval in seconds (default: 10)
#   AWS_CLI                — binary name for aws CLI (default: aws; used for
#                            mocking in tests)
#
# Exit codes:
#   0 — Target deployment reached rolloutState=COMPLETED with runningCount==
#       desiredCount. This includes the scale-to-zero case (desired=0,
#       running=0): when a service is kept at desiredCount=0 (e.g. dev
#       ingestion worker runs only on-demand), ECS marks the new deployment
#       COMPLETED immediately with no tasks to launch, and we treat that as
#       a successful rollout (#2585).
#   1 — rolloutState=FAILED, or the timeout elapsed, or the aws CLI failed,
#       or the env-var contract was violated.

set -euo pipefail

: "${ECS_CLUSTER:?ECS_CLUSTER is required}"
: "${ECS_SERVICE:?ECS_SERVICE is required}"

NEW_TASK_DEF_ARN="${NEW_TASK_DEF_ARN:-}"
NEW_DEPLOYMENT_ID="${NEW_DEPLOYMENT_ID:-}"

if [ -z "$NEW_TASK_DEF_ARN" ] && [ -z "$NEW_DEPLOYMENT_ID" ]; then
  echo "ERROR: one of NEW_TASK_DEF_ARN or NEW_DEPLOYMENT_ID is required." >&2
  exit 1
fi
if [ -n "$NEW_TASK_DEF_ARN" ] && [ -n "$NEW_DEPLOYMENT_ID" ]; then
  echo "ERROR: set only one of NEW_TASK_DEF_ARN or NEW_DEPLOYMENT_ID, not both." >&2
  exit 1
fi

ROLLOUT_TIMEOUT_SECS="${ROLLOUT_TIMEOUT_SECS:-900}"
ROLLOUT_POLL_INTERVAL="${ROLLOUT_POLL_INTERVAL:-10}"
AWS_CLI="${AWS_CLI:-aws}"

if [ -n "$NEW_TASK_DEF_ARN" ]; then
  SELECTOR_DESC="task definition $NEW_TASK_DEF_ARN"
  JQ_SELECT_FILTER='.services[0].deployments[] | select(.taskDefinition == $sel)'
  JQ_SELECT_ARG="$NEW_TASK_DEF_ARN"
else
  SELECTOR_DESC="deployment $NEW_DEPLOYMENT_ID"
  JQ_SELECT_FILTER='.services[0].deployments[] | select(.id == $sel)'
  JQ_SELECT_ARG="$NEW_DEPLOYMENT_ID"
fi

echo "Waiting for $SELECTOR_DESC to roll out on ${ECS_CLUSTER}/${ECS_SERVICE} (timeout: ${ROLLOUT_TIMEOUT_SECS}s, poll every ${ROLLOUT_POLL_INTERVAL}s)..."

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

  DEPLOYMENT=$(echo "$SERVICE_JSON" | jq --arg sel "$JQ_SELECT_ARG" \
    "$JQ_SELECT_FILTER")

  if [ -z "$DEPLOYMENT" ]; then
    # Our deployment has not yet been registered in the deployments list.
    # Give ECS a moment — retry.
    echo "[${ELAPSED}s] Deployment for $SELECTOR_DESC not yet visible, retrying..."
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
        # Treat COMPLETED as success when runningCount == desiredCount. This
        # includes the scale-to-zero case (desired=0, running=0) — see #2585.
        # Earlier revisions required runningCount!=0 to guard against a race
        # where COMPLETED is briefly reported with running=0 during a normal
        # rollout (desired>0); the `running==desired` check preserves that
        # guard for desired>0 while accepting the legitimate desired=0
        # terminal state.
        if [ "$RUNNING_COUNT" = "$DESIRED_COUNT" ]; then
          if [ "$DESIRED_COUNT" = "0" ]; then
            echo "Service updated successfully after ${ELAPSED}s (scale-to-zero: running=0 desired=0)."
          else
            echo "Service updated successfully after ${ELAPSED}s."
          fi
          exit 0
        fi
        ;;
      FAILED)
        echo "ERROR: ECS reported rolloutState=FAILED for $SELECTOR_DESC after ${ELAPSED}s."
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
    echo "ERROR: Timed out after ${ROLLOUT_TIMEOUT_SECS}s waiting for $SELECTOR_DESC to stabilize."
    echo "Last observed state: ${LAST_STATE:-<none>} (${LAST_COUNTS:-<no counts>})"
    echo "Full service JSON snapshot:"
    echo "$SERVICE_JSON" | jq '.services[0] | {serviceName, desiredCount, runningCount, pendingCount, deployments, events: (.events[:5])}'
    exit 1
  fi

  sleep "$ROLLOUT_POLL_INTERVAL"
done
