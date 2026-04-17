#!/usr/bin/env bash
# ecs-post-deploy-healthcheck.sh — Post-deploy health check for an ECS service.
#
# Three checks, run in order:
#   1. service-check  — describe-services: runningCount matches desiredCount
#                       (scale-to-zero treated as healthy; see #2585 / #2605).
#   2. crash-check    — describe-tasks on the most recent running task:
#                       no stopped container with a non-zero exit code.
#                       Skipped when desiredCount == 0 (no tasks to inspect).
#   3. log-check      — CloudWatch Logs: no CRITICAL / Traceback /
#                       InfrastructureError / crash lines in the most
#                       recent 20 events of the newest log stream.
#                       Warning only — does not fail the deploy.
#
# Motivation:
#   Originally three inline bash blocks in .github/workflows/deploy-scraper.yml's
#   post-deploy-health-check job. Extracted into a shared script so:
#
#     - the scale-to-zero rule (running==desired==0 treated as healthy) is
#       codified in ONE place, not three copies that can drift (this is
#       exactly how #2605 happened — see also the parallel extraction of
#       wait-for-rollout.sh in #2585/#2519).
#
#     - the logic is unit-testable against a mock aws CLI the way
#       scripts/wait-for-rollout.sh is tested in
#       scripts/tests/test_wait_for_rollout.sh.
#
# IMPORTANT — paths-filter contract (see #2592):
#   Any deploy workflow that adopts this script MUST add
#   `scripts/ecs-post-deploy-healthcheck.sh` to its `on.push.paths:` filter.
#   Otherwise a PR that changes only this shared script will merge without
#   triggering any deploy workflow — so the fix is not exercised against a
#   real rollout on its own PR, and regressions surface on the next
#   unrelated deploy. Current callers:
#     - .github/workflows/deploy-scraper.yml
#
# Required environment variables:
#   ECS_CLUSTER         — ECS cluster name (e.g. judgemind-dev)
#   INGESTION_SERVICE   — ECS service name (e.g. judgemind-ingestion-worker-dev)
#   LOG_GROUP           — CloudWatch log group for the service (e.g.
#                         /ecs/judgemind-ingestion-worker-dev)
#
# Optional environment variables:
#   AWS_CLI             — binary name for aws CLI (default: aws; used for
#                         mocking in tests)
#
# Exit codes:
#   0 — service-check passed AND crash-check passed. log-check is warning-only
#       and never causes a non-zero exit.
#   1 — service-check failed (running != desired, not scale-to-zero), OR
#       crash-check failed (stopped container with non-zero exit code or
#       no running tasks when desiredCount > 0), OR aws CLI call failed, OR
#       a required env var was not set.

set -euo pipefail

: "${ECS_CLUSTER:?ECS_CLUSTER is required}"
: "${INGESTION_SERVICE:?INGESTION_SERVICE is required}"
: "${LOG_GROUP:?LOG_GROUP is required}"

AWS_CLI="${AWS_CLI:-aws}"

# ─── 1. service-check — verify service is running ───────────────────────────

echo "=== Health check: verify service is running ==="

if ! SERVICE_DESC=$("$AWS_CLI" ecs describe-services \
      --cluster "$ECS_CLUSTER" \
      --services "$INGESTION_SERVICE" \
      --query 'services[0]' \
      --output json); then
    echo "ERROR: aws ecs describe-services failed."
    exit 1
fi

RUNNING_COUNT=$(echo "$SERVICE_DESC" | jq -r '.runningCount')
DESIRED_COUNT=$(echo "$SERVICE_DESC" | jq -r '.desiredCount')

echo "Running tasks: $RUNNING_COUNT / Desired: $DESIRED_COUNT"

# Treat running==desired as success. This includes the scale-to-zero case
# (desired=0, running=0): the dev ingestion worker is kept at desiredCount=0
# and runs on-demand, so "no tasks running" is the expected steady state.
# Mirrors the #2585 fix in scripts/wait-for-rollout.sh (see #2605).
if [ "$DESIRED_COUNT" -eq 0 ] && [ "$RUNNING_COUNT" -eq 0 ]; then
    echo "Service is scaled to zero (desired=0, running=0) — treating as healthy."
elif [ "$RUNNING_COUNT" -ge "$DESIRED_COUNT" ] && [ "$RUNNING_COUNT" -gt 0 ]; then
    echo "Service has expected number of running tasks."
else
    echo "ERROR: Service running count ($RUNNING_COUNT) does not match desired ($DESIRED_COUNT)."
    exit 1
fi

# ─── 2. crash-check — verify no crash-loop ──────────────────────────────────

echo ""
echo "=== Health check: verify no crash-loop ==="

# When the service is scaled to zero (desired=0), there are no tasks to
# inspect — no running tasks is the expected state, not a crash-loop. Skip
# the task inspection entirely. Mirrors the #2585 fix in
# scripts/wait-for-rollout.sh (see #2605).
if [ "$DESIRED_COUNT" -eq 0 ]; then
    echo "Service is scaled to zero (desired=0) — skipping crash-loop check (no tasks to inspect)."
else
    # Check that the most recent running task has no stopped containers with
    # non-zero exit codes (a crash-looping container restarts quickly).
    if ! TASKS=$("$AWS_CLI" ecs list-tasks \
          --cluster "$ECS_CLUSTER" \
          --service-name "$INGESTION_SERVICE" \
          --desired-status RUNNING \
          --query 'taskArns' \
          --output json); then
        echo "ERROR: aws ecs list-tasks failed."
        exit 1
    fi

    TASK_COUNT=$(echo "$TASKS" | jq 'length')

    if [ "$TASK_COUNT" -eq 0 ]; then
        echo "ERROR: No running tasks found for service."
        exit 1
    fi

    FIRST_TASK=$(echo "$TASKS" | jq -r '.[0]')
    if ! TASK_DESC=$("$AWS_CLI" ecs describe-tasks \
          --cluster "$ECS_CLUSTER" \
          --tasks "$FIRST_TASK" \
          --query 'tasks[0]' \
          --output json); then
        echo "ERROR: aws ecs describe-tasks failed."
        exit 1
    fi

    LAST_STATUS=$(echo "$TASK_DESC" | jq -r '.lastStatus')
    HEALTH_STATUS=$(echo "$TASK_DESC" | jq -r '.healthStatus // "UNKNOWN"')

    echo "Task status: $LAST_STATUS, health: $HEALTH_STATUS"

    # Check container exit codes — any STOPPED container with a non-zero
    # exitCode indicates a crash-loop.
    STOPPED_CONTAINERS=$(echo "$TASK_DESC" | jq '[.containers[] | select(.lastStatus == "STOPPED" and .exitCode != 0)] | length')

    if [ "$STOPPED_CONTAINERS" -gt 0 ]; then
        echo "ERROR: Found $STOPPED_CONTAINERS stopped containers with non-zero exit codes — possible crash-loop."
        echo "$TASK_DESC" | jq '.containers[] | {name, lastStatus, exitCode, reason}'
        exit 1
    fi

    echo "No crash-loop detected. Service appears healthy."
fi

# ─── 3. log-check — verify recent log output (warning only) ─────────────────

echo ""
echo "=== Health check: verify recent log output (no critical errors) ==="

# Get the most recent log stream. If this fails or returns nothing, emit a
# warning and continue — log-check never fails the deploy for log noise or
# transient CloudWatch issues.
LATEST_STREAM=$("$AWS_CLI" logs describe-log-streams \
    --log-group-name "$LOG_GROUP" \
    --order-by LastEventTime \
    --descending \
    --limit 1 \
    --query 'logStreams[0].logStreamName' \
    --output text 2>/dev/null || echo "")

if [ -z "$LATEST_STREAM" ] || [ "$LATEST_STREAM" = "None" ]; then
    echo "WARNING: No log streams found. Service may still be starting."
    exit 0
fi

echo "Checking latest log stream: $LATEST_STREAM"

EVENTS=$("$AWS_CLI" logs get-log-events \
    --log-group-name "$LOG_GROUP" \
    --log-stream-name "$LATEST_STREAM" \
    --limit 20 \
    --start-from-head false \
    --output json 2>/dev/null || echo '{"events":[]}')

EVENT_COUNT=$(echo "$EVENTS" | jq '.events | length')
echo "Found $EVENT_COUNT recent log events."

# Grep for critical error indicators. Warning only — don't fail the whole
# deploy for log noise. (Transient tracebacks or crash mentions in info-level
# output would otherwise trip every deploy.)
CRITICAL_ERRORS=$(echo "$EVENTS" | jq -r '.events[].message' | grep -ic "CRITICAL\|Traceback\|InfrastructureError\|crash" || true)

if [ "$CRITICAL_ERRORS" -gt 0 ]; then
    echo "WARNING: Found $CRITICAL_ERRORS critical error indicators in recent logs."
    echo "Recent log events:"
    echo "$EVENTS" | jq -r '.events[].message' | tail -10
    # Warning only — don't fail the whole deploy.
else
    echo "No critical errors found in recent logs."
fi

exit 0
