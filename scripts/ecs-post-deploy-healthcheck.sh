#!/usr/bin/env bash
# ecs-post-deploy-healthcheck.sh — Post-deploy health check for an ECS service.
#
# Three checks, run in order:
#   1. service-check  — poll describe-services until the service is steady:
#                       runningCount == desiredCount AND the PRIMARY
#                       deployment's rolloutState is COMPLETED (scale-to-zero
#                       treated as healthy; see #2585 / #2605). While
#                       waiting, every poll also looks for tasks of the
#                       PRIMARY deployment that crashed (see "Crashed task vs.
#                       normal replacement" below).
#   2. crash-check    — describe-tasks on the most recent running task:
#                       no stopped container with a non-zero exit code.
#                       Skipped when desiredCount == 0 (no tasks to inspect).
#   3. log-check      — CloudWatch Logs: no CRITICAL / Traceback /
#                       InfrastructureError / crash lines in the most
#                       recent 20 events of the newest log stream.
#                       Warning only — does not fail the deploy.
#
# Why service-check polls (#5102):
#   It used to read the running count ONCE, 60s after the deploy job. The
#   PR #5100 deploy read 0 of 1 at 02:46Z, in the window where the old
#   ingestion-worker task had stopped and its replacement had not reached
#   RUNNING yet. ECS showed only normal deployment replacements, no crash.
#   Since graceful SIGTERM with stopTimeout 120 (#5069) and following a
#   superseding deployment (#5073) that window is longer and more frequent.
#   A red health check on a normal replacement also skips the task-def
#   fingerprint check that follows it, and teaches people to ignore red
#   deploys. So the check now waits, under the shared wait budget
#   (scripts/_wait_budget_lib.sh: 480s per call, exit 124 / 125, #4835 /
#   #4850 / #4975), for the service to settle before judging it.
#
# Crashed task vs. normal replacement:
#   During a deployment ECS stops the old tasks with stoppedReason
#   "Scaling activity initiated by (deployment ecs-svc/...)". Those are
#   normal replacements, whatever their exit code (a SIGKILL after
#   stopTimeout exits 137). A task counts as CRASHED when it belongs to the
#   PRIMARY deployment (task.startedBy == deployment id, or the same
#   task-def when startedBy is empty), its stoppedReason is not a
#   deployment scaling activity, and a container exited non-zero (or ECS
#   reports stopCode TaskFailedToStart). A crash fails the check at once
#   with the task ARN, stoppedReason and container exit codes, even when
#   ECS has already started a healthy replacement.
#
# Motivation for the extraction:
#   Originally three inline bash blocks in .github/workflows/deploy-scraper.yml's
#   post-deploy-health-check job. Extracted into a shared script so:
#
#     - the scale-to-zero rule (running==desired==0 treated as healthy) is
#       codified in ONE place, not three copies that can drift (this is
#       exactly how #2605 happened — see also the parallel extraction of
#       wait-for-rollout.sh in #2585/#2519).
#
#     - the logic is unit-testable against a mock aws CLI, in
#       scripts/tests/test_ecs_post_deploy_healthcheck.sh.
#
# IMPORTANT — paths-filter contract (see #2592):
#   Any deploy workflow that adopts this script MUST add
#   `scripts/ecs-post-deploy-healthcheck.sh` to its `on.push.paths:` filter,
#   along with the libraries it sources (scripts/_wait_budget_lib.sh and
#   scripts/_ecs_exec_lib.sh). Otherwise a PR that changes only this
#   shared script will merge without triggering any deploy workflow — so
#   the fix is not exercised against a real rollout on its own PR, and
#   regressions surface on the next unrelated deploy. Current callers:
#     - .github/workflows/deploy-scraper.yml
#
# Required environment variables:
#   ECS_CLUSTER         — ECS cluster name (e.g. judgemind-dev)
#   INGESTION_SERVICE   — ECS service name (e.g. judgemind-ingestion-worker-dev)
#   LOG_GROUP           — CloudWatch log group for the service (e.g.
#                         /ecs/judgemind-ingestion-worker-dev)
#
# Optional environment variables:
#   AWS_CLI                    — binary name for aws CLI (default: aws; used
#                                for mocking in tests)
#   HEALTHCHECK_TIMEOUT_SECS   — how long service-check waits for the service
#                                to settle (default 480, the shared per-call
#                                budget, #4835)
#   HEALTHCHECK_POLL_INTERVAL  — seconds between polls (default 10)
#
# Exit codes:
#   0   — service-check passed AND crash-check passed. log-check is
#         warning-only and never causes a non-zero exit.
#   1   — a task of the PRIMARY deployment crashed, OR the PRIMARY
#         deployment's rolloutState is FAILED, OR crash-check failed
#         (stopped container with non-zero exit code, or no running tasks
#         when desiredCount > 0), OR a required env var was not set.
#   124 — the service did not settle within HEALTHCHECK_TIMEOUT_SECS (still
#         rolling out). The last observed state is printed. Re-running only
#         reads state.
#   125 — aws ecs describe-services failed (or hung past its per-call limit
#         3 times in a row), so the service state is unknown.

set -euo pipefail

# shellcheck source=scripts/_wait_budget_lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_wait_budget_lib.sh"

: "${ECS_CLUSTER:?ECS_CLUSTER is required}"
: "${INGESTION_SERVICE:?INGESTION_SERVICE is required}"
: "${LOG_GROUP:?LOG_GROUP is required}"

AWS_CLI="${AWS_CLI:-aws}"
HEALTHCHECK_TIMEOUT_SECS="${HEALTHCHECK_TIMEOUT_SECS:-480}"
HEALTHCHECK_POLL_INTERVAL="${HEALTHCHECK_POLL_INTERVAL:-10}"

# ECS's stoppedReason for tasks a deployment replaces.
DEPLOYMENT_STOP_REASON_PREFIX="Scaling activity initiated by"

wait_budget_init "$HEALTHCHECK_TIMEOUT_SECS"

# elapsed_secs — seconds since the budget started (follows the test clock).
elapsed_secs() {
    echo $((HEALTHCHECK_TIMEOUT_SECS - $(wait_budget_remaining)))
}

# check_crashed_tasks <primary-deployment-id> <primary-task-def>
#
# Exits 1 when a stopped task of the PRIMARY deployment crashed. A failed or
# timed-out lookup is logged and skipped: it is not evidence of a crash, and
# the next poll looks again.
check_crashed_tasks() {
    local dep_id="$1" dep_td="$2" rc=0 stopped
    stopped=$(wait_budget_call "aws ecs list-tasks (STOPPED)" \
        "$AWS_CLI" ecs list-tasks \
        --cluster "$ECS_CLUSTER" \
        --service-name "$INGESTION_SERVICE" \
        --desired-status STOPPED \
        --query 'taskArns' \
        --output json) || rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "  (could not list stopped tasks this poll, rc=$rc; will look again)"
        return 0
    fi
    local arns
    arns=$(echo "$stopped" | jq -r '.[:100][]?') || arns=""
    if [ -z "$arns" ]; then
        return 0
    fi

    local desc
    # shellcheck disable=SC2086  # one ARN per word is what --tasks wants
    desc=$(wait_budget_call "aws ecs describe-tasks (STOPPED)" \
        "$AWS_CLI" ecs describe-tasks \
        --cluster "$ECS_CLUSTER" \
        --tasks $arns \
        --query 'tasks' \
        --output json) || rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "  (could not describe stopped tasks this poll, rc=$rc; will look again)"
        return 0
    fi

    local crashes
    crashes=$(echo "$desc" | jq -c \
        --arg dep "$dep_id" --arg td "$dep_td" --arg scaling "$DEPLOYMENT_STOP_REASON_PREFIX" '
        [ .[]?
          | select(((.startedBy // "") == $dep)
                   or (((.startedBy // "") == "") and ((.taskDefinitionArn // "") == $td)))
          | select(((.stoppedReason // "") | startswith($scaling)) | not)
          | select(any(.containers[]?; (.exitCode != null) and (.exitCode != 0))
                   or ((.stopCode // "") == "TaskFailedToStart"))
          | { taskArn,
              stoppedReason: (.stoppedReason // ""),
              stopCode: (.stopCode // ""),
              containers: [ .containers[]?
                            | select((.exitCode != null) and (.exitCode != 0))
                            | {name, exitCode, reason: (.reason // "")} ] }
        ]') || crashes="[]"

    if [ "$(echo "$crashes" | jq 'length')" -gt 0 ]; then
        echo "ERROR: CRASHED: $(echo "$crashes" | jq 'length') task(s) of deployment $dep_id stopped with a crash (not a deployment replacement):"
        echo "$crashes" | jq -r '.[] |
            "  task \(.taskArn): stoppedReason=\"\(.stoppedReason)\" stopCode=\(.stopCode)" +
            ([.containers[] | "\n    container \(.name) exitCode=\(.exitCode) reason=\"\(.reason)\""] | join(""))'
        echo "Check the task's logs in $LOG_GROUP for the crash."
        exit 1
    fi
}

# ─── 1. service-check — wait until the service is steady ────────────────────

echo "=== Health check: verify service is running ==="
echo "Waiting for ${ECS_CLUSTER}/${INGESTION_SERVICE} to settle: runningCount == desiredCount and PRIMARY deployment COMPLETED (timeout ${HEALTHCHECK_TIMEOUT_SECS}s, poll every ${HEALTHCHECK_POLL_INTERVAL}s)..."

POLL=0
CONSECUTIVE_TIMEOUTS=0
MAX_CONSECUTIVE_TIMEOUTS=3
SERVICE_DESC='{}'
LAST_STATE="<none>"
RUNNING_COUNT=""
DESIRED_COUNT=""

while true; do
    POLL=$((POLL + 1))
    DESCRIBE_RC=0
    DESCRIBE_OUT=$(wait_budget_call "aws ecs describe-services" \
        "$AWS_CLI" ecs describe-services \
        --cluster "$ECS_CLUSTER" \
        --services "$INGESTION_SERVICE" \
        --query 'services[0]' \
        --output json) || DESCRIBE_RC=$?

    if [ "$DESCRIBE_RC" -eq "$WAIT_CALL_TIMEOUT_RC" ]; then
        # A hung call is a failed poll, not a verdict (#4850).
        CONSECUTIVE_TIMEOUTS=$((CONSECUTIVE_TIMEOUTS + 1))
        if [ "$CONSECUTIVE_TIMEOUTS" -ge "$MAX_CONSECUTIVE_TIMEOUTS" ] && ! wait_budget_exhausted; then
            echo "ERROR: aws ecs describe-services hung ${CONSECUTIVE_TIMEOUTS} times in a row; service state unknown."
            exit 125
        fi
        echo "[$(elapsed_secs)s] poll ${POLL}: describe-services timed out, retrying..."
    elif [ "$DESCRIBE_RC" -ne 0 ]; then
        echo "ERROR: aws ecs describe-services failed; service state unknown."
        exit 125
    else
        CONSECUTIVE_TIMEOUTS=0
        SERVICE_DESC="$DESCRIBE_OUT"
        RUNNING_COUNT=$(echo "$SERVICE_DESC" | jq -r '.runningCount')
        DESIRED_COUNT=$(echo "$SERVICE_DESC" | jq -r '.desiredCount')
        PENDING_COUNT=$(echo "$SERVICE_DESC" | jq -r '.pendingCount // 0')
        DEPLOYMENT_COUNT=$(echo "$SERVICE_DESC" | jq '[.deployments[]?] | length')
        PRIMARY=$(echo "$SERVICE_DESC" | jq -c '[.deployments[]? | select(.status == "PRIMARY")] | .[0] // empty')
        PRIMARY_ID=""
        PRIMARY_TD=""
        ROLLOUT_STATE=""
        ROLLOUT_REASON=""
        if [ -n "$PRIMARY" ]; then
            PRIMARY_ID=$(echo "$PRIMARY" | jq -r '.id // ""')
            PRIMARY_TD=$(echo "$PRIMARY" | jq -r '.taskDefinition // ""')
            ROLLOUT_STATE=$(echo "$PRIMARY" | jq -r '.rolloutState // ""')
            ROLLOUT_REASON=$(echo "$PRIMARY" | jq -r '.rolloutStateReason // ""')
        fi
        LAST_STATE="running=${RUNNING_COUNT} desired=${DESIRED_COUNT} pending=${PENDING_COUNT} deployments=${DEPLOYMENT_COUNT} primary=${PRIMARY_ID:-<none>} rolloutState=${ROLLOUT_STATE:-<none>}"
        echo "[$(elapsed_secs)s] poll ${POLL}: ${LAST_STATE}"

        # Scale-to-zero: the dev ingestion worker can be kept at
        # desiredCount=0 and run on demand, so "no tasks running" is the
        # expected steady state. Mirrors the #2585 fix in
        # scripts/wait-for-rollout.sh (see #2605).
        if [ "$DESIRED_COUNT" -eq 0 ] && [ "$RUNNING_COUNT" -eq 0 ]; then
            echo "Running tasks: $RUNNING_COUNT / Desired: $DESIRED_COUNT"
            echo "Service is scaled to zero (desired=0, running=0) — treating as healthy."
            break
        fi

        if [ "$ROLLOUT_STATE" = "FAILED" ]; then
            echo "ERROR: ECS reports rolloutState=FAILED for PRIMARY deployment ${PRIMARY_ID}: ${ROLLOUT_REASON}"
            exit 1
        fi

        if [ -n "$PRIMARY_ID" ]; then
            check_crashed_tasks "$PRIMARY_ID" "$PRIMARY_TD"
        fi

        # Steady: counts match and the PRIMARY rollout finished. A service
        # description without rolloutState (no deployments listed, or a
        # controller that does not report it) counts as finished only when
        # nothing else is draining.
        if [ "$RUNNING_COUNT" -eq "$DESIRED_COUNT" ] && {
            [ "$ROLLOUT_STATE" = "COMPLETED" ] \
                || { [ -z "$ROLLOUT_STATE" ] && [ "$DEPLOYMENT_COUNT" -le 1 ]; }
        }; then
            echo "Running tasks: $RUNNING_COUNT / Desired: $DESIRED_COUNT"
            echo "Service is steady after $(elapsed_secs)s (poll ${POLL})."
            echo "Service has expected number of running tasks."
            break
        fi
    fi

    if wait_budget_exhausted; then
        echo "ERROR: Timed out after ${HEALTHCHECK_TIMEOUT_SECS}s waiting for the service to settle (still rolling out)."
        echo "Service running count (${RUNNING_COUNT:-?}) does not match desired (${DESIRED_COUNT:-?}), or the PRIMARY deployment has not COMPLETED."
        echo "Last observed state: ${LAST_STATE}"
        echo "Full service snapshot:"
        echo "$SERVICE_DESC" | jq '{serviceName, desiredCount, runningCount, pendingCount, deployments, events: (.events // [] | .[:5])}' || true
        echo "Re-run to keep waiting (this check only reads state)."
        exit 124
    fi

    wait_budget_sleep "$HEALTHCHECK_POLL_INTERVAL"
done

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
    if ! TASKS=$(wait_budget_call "aws ecs list-tasks" \
          "$AWS_CLI" ecs list-tasks \
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
    if ! TASK_DESC=$(wait_budget_call "aws ecs describe-tasks" \
          "$AWS_CLI" ecs describe-tasks \
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
LATEST_STREAM=$(wait_budget_call "aws logs describe-log-streams" \
    "$AWS_CLI" logs describe-log-streams \
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

EVENTS=$(wait_budget_call "aws logs get-log-events" \
    "$AWS_CLI" logs get-log-events \
    --log-group-name "$LOG_GROUP" \
    --log-stream-name "$LATEST_STREAM" \
    --limit 20 \
    --no-start-from-head \
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
