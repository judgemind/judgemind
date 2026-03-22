#!/usr/bin/env bash
# run-scraper.sh — Manually trigger one or more scrapers on ECS Fargate.
#
# Launches a Fargate task using the scraper task definition with the
# correct CMD override (["framework", "<scraper-id>", ...]).  Derives
# network configuration from the EventBridge Scheduler target — no
# hardcoded subnets or security groups.
#
# Prerequisites:
#   - AWS CLI v2
#   - Credentials for the judgemind AWS account (155326049300)
#   - The scraper task definition and scheduler must exist
#
# Usage:
#   scripts/run-scraper.sh <scraper-id> [<scraper-id> ...]
#   scripts/run-scraper.sh --detach <scraper-id>
#   scripts/run-scraper.sh --env dev <scraper-id>
#   scripts/run-scraper.sh --help
#
# Options:
#   --env <env>         Environment (default: dev)
#   --detach            Launch the task and exit immediately; prints the task ARN
#   --timeout <secs>    Max seconds to wait for task completion (default: 1800)
#   --dry-run           Show what would be done without running
#   --help              Show this help message
#
# Examples:
#   # Run a single scraper and stream logs until completion
#   scripts/run-scraper.sh ca-la-tentatives-civil
#
#   # Run multiple scrapers in one task
#   scripts/run-scraper.sh ca-la-tentatives-civil ca-oc-tentatives
#
#   # Launch and return immediately (for long-running scrapers)
#   scripts/run-scraper.sh --detach federal-courtlistener-opinions
#
set -euo pipefail

# ─── Defaults ────────────────────────────────────────────────────────────────

ENVIRONMENT="dev"
DRY_RUN=false
DETACH=false
TIMEOUT=1800
REGION="us-west-2"
SCRAPER_IDS=()

# ─── Parse options ───────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env)
            ENVIRONMENT="$2"
            shift 2
            ;;
        --detach)
            DETACH=true
            shift
            ;;
        --timeout)
            TIMEOUT="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help|-h)
            head -n 36 "$0" | tail -n +2 | sed 's/^# \?//'
            exit 0
            ;;
        -*)
            echo "Error: unknown option '$1'" >&2
            echo "Run 'scripts/run-scraper.sh --help' for usage." >&2
            exit 1
            ;;
        *)
            SCRAPER_IDS+=("$1")
            shift
            ;;
    esac
done

# ─── Resolve repo root and sibling scripts ────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ─── Validate arguments ─────────────────────────────────────────────────────

if [[ ${#SCRAPER_IDS[@]} -eq 0 ]]; then
    echo "Error: at least one scraper ID is required." >&2
    echo "" >&2
    echo "Usage: scripts/run-scraper.sh [options] <scraper-id> [<scraper-id> ...]" >&2
    echo "" >&2
    echo "Run 'scripts/run-scraper.sh --help' for full usage." >&2
    exit 1
fi

# ─── Derived names ────────────────────────────────────────────────────────

TASK_FAMILY="judgemind-scraper-${ENVIRONMENT}"
CLUSTER="judgemind-${ENVIRONMENT}"
SCHEDULER_NAME="judgemind-scraper-${ENVIRONMENT}"

# ─── Step 1: Read the scraper task definition ────────────────────────────────

echo "Reading latest task definition for ${TASK_FAMILY}..." >&2

TASK_DEF_JSON=$(aws ecs describe-task-definition \
    --task-definition "$TASK_FAMILY" \
    --region "$REGION" \
    --output json \
    --no-cli-pager 2>/dev/null) || {
    echo "Error: could not read task definition '${TASK_FAMILY}'." >&2
    echo "Ensure the scraper is deployed in the '${ENVIRONMENT}' environment." >&2
    exit 1
}

TASK_DEF_ARN=$(echo "$TASK_DEF_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['taskDefinition']['taskDefinitionArn'])")

echo "Task definition: ${TASK_DEF_ARN}" >&2

# ─── Step 2: Get network config from the EventBridge Scheduler target ────────

echo "Reading network config from scheduler '${SCHEDULER_NAME}'..." >&2

SCHEDULER_JSON=$(aws scheduler get-schedule \
    --name "$SCHEDULER_NAME" \
    --region "$REGION" \
    --output json \
    --no-cli-pager 2>/dev/null) || {
    echo "Error: could not read EventBridge Scheduler '${SCHEDULER_NAME}'." >&2
    echo "Ensure the scheduler exists in the '${ENVIRONMENT}' environment." >&2
    exit 1
}

NETWORK_CONFIG=$(echo "$SCHEDULER_JSON" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin)['Target']['EcsParameters']['NetworkConfiguration']['AwsvpcConfiguration']))")
SUBNETS=$(echo "$NETWORK_CONFIG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(','.join(d['Subnets']))")
SECURITY_GROUPS=$(echo "$NETWORK_CONFIG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(','.join(d['SecurityGroups']))")

echo "Subnets: ${SUBNETS}" >&2
echo "Security groups: ${SECURITY_GROUPS}" >&2

# ─── Step 3: Build the CMD override ─────────────────────────────────────────
# The scraper Dockerfile uses ENTRYPOINT ["python", "-m"] so CMD must be
# ["framework", "<scraper-id>", ...].  This is the key insight from #1421 —
# do NOT include "python -m" in the override; the entrypoint handles that.

CMD_JSON="[\"framework\""
for sid in "${SCRAPER_IDS[@]}"; do
    CMD_JSON="${CMD_JSON}, \"${sid}\""
done
CMD_JSON="${CMD_JSON}]"

OVERRIDE_JSON="{\"containerOverrides\": [{\"name\": \"scraper\", \"command\": ${CMD_JSON}}]}"

echo "" >&2
echo "Scraper IDs: ${SCRAPER_IDS[*]}" >&2
echo "CMD override: ${CMD_JSON}" >&2
echo "" >&2

# ─── Dry-run mode ────────────────────────────────────────────────────────────

if [[ "$DRY_RUN" == "true" ]]; then
    echo "=== DRY RUN ===" >&2
    echo "Would run task on cluster ${CLUSTER}" >&2
    echo "  Task definition: ${TASK_DEF_ARN}" >&2
    echo "  Subnets:         ${SUBNETS}" >&2
    echo "  Security groups: ${SECURITY_GROUPS}" >&2
    echo "  Container override: ${OVERRIDE_JSON}" >&2
    exit 0
fi

# ─── Step 4: Launch the ECS task ─────────────────────────────────────────────

echo "Launching scraper task..." >&2

RUN_OUTPUT=$(aws ecs run-task \
    --cluster "$CLUSTER" \
    --task-definition "$TASK_DEF_ARN" \
    --launch-type FARGATE \
    --region "$REGION" \
    --output json \
    --no-cli-pager \
    --overrides "$OVERRIDE_JSON" \
    --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[${SECURITY_GROUPS}],assignPublicIp=DISABLED}")

TASK_ARN=$(echo "$RUN_OUTPUT" | python3 -c "import sys,json; tasks=json.load(sys.stdin)['tasks']; print(tasks[0]['taskArn'] if tasks else '')")

if [[ -z "$TASK_ARN" ]]; then
    echo "Error: failed to launch task." >&2
    FAILURES=$(echo "$RUN_OUTPUT" | python3 -c "import sys,json; f=json.load(sys.stdin).get('failures',[]); [print(x.get('reason','unknown')) for x in f]")
    if [[ -n "$FAILURES" ]]; then
        echo "Failures:" >&2
        echo "$FAILURES" >&2
    fi
    exit 1
fi

TASK_ID=$(echo "$TASK_ARN" | rev | cut -d'/' -f1 | rev)
LOG_GROUP="/ecs/judgemind-scraper-${ENVIRONMENT}"

echo "Task ARN: ${TASK_ARN}" >&2
echo "Task ID:  ${TASK_ID}" >&2
echo "" >&2
echo "Logs:" >&2
echo "  Tail logs:    scripts/ecs-logs.sh ${LOG_GROUP} --task ${TASK_ID}" >&2
echo "  Follow logs:  scripts/ecs-logs.sh ${LOG_GROUP} --task ${TASK_ID} --follow" >&2
echo "" >&2

# ─── Detach mode ─────────────────────────────────────────────────────────────

if [[ "$DETACH" == "true" ]]; then
    echo "Detach mode: task launched successfully." >&2
    # Print task ARN to stdout for callers to capture
    echo "${TASK_ARN}"
    exit 0
fi

# ─── Step 5: Poll for completion with log streaming ──────────────────────────

echo "Waiting for task to complete (timeout: ${TIMEOUT}s)..." >&2

POLL_INTERVAL=10
ELAPSED=0
LAST_STATUS=""

# Log streaming state
LOG_STREAM_NAME=""
LOG_NEXT_TOKEN=""
LOG_STREAMING=false

find_log_stream() {
    aws logs describe-log-streams \
        --log-group-name "$LOG_GROUP" \
        --log-stream-name-prefix "scraper/scraper/" \
        --order-by LogStreamName \
        --max-items 50 \
        --region "$REGION" \
        --output text \
        --query "logStreams[*].logStreamName" \
        --no-cli-pager | tr '\t' '\n' | grep -F "$TASK_ID" | head -n 1 || true
}

stream_new_logs() {
    if [[ -z "$LOG_STREAM_NAME" ]]; then
        return
    fi

    local args=(
        logs get-log-events
        --log-group-name "$LOG_GROUP"
        --log-stream-name "$LOG_STREAM_NAME"
        --region "$REGION"
        --output json
        --no-cli-pager
    )

    if [[ -n "$LOG_NEXT_TOKEN" ]]; then
        args+=(--next-token "$LOG_NEXT_TOKEN")
    else
        args+=(--start-from-head)
    fi

    local result
    result=$(aws "${args[@]}") || {
        echo "WARNING: failed to fetch log events during live streaming" >&2
        return 0
    }

    local messages new_token
    messages=$(echo "$result" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for event in data.get('events', []):
    print(event.get('message', '').rstrip())
") || {
        echo "WARNING: failed to parse live log events JSON" >&2
        true
    }

    new_token=$(echo "$result" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('nextForwardToken', ''))
") || {
        echo "WARNING: failed to extract forward token from live log response" >&2
        true
    }

    if [[ -n "$messages" ]]; then
        echo "$messages"
    fi

    if [[ -n "$new_token" ]]; then
        LOG_NEXT_TOKEN="$new_token"
    fi
}

while [[ $ELAPSED -lt $TIMEOUT ]]; do
    DESCRIBE_OUTPUT=$(aws ecs describe-tasks \
        --cluster "$CLUSTER" \
        --tasks "$TASK_ARN" \
        --region "$REGION" \
        --output json \
        --no-cli-pager)

    CURRENT_STATUS=$(echo "$DESCRIBE_OUTPUT" | python3 -c "import sys,json; t=json.load(sys.stdin)['tasks'][0]; print(t['lastStatus'])")

    if [[ "$CURRENT_STATUS" != "$LAST_STATUS" ]]; then
        echo "Status: ${CURRENT_STATUS}" >&2
        LAST_STATUS="$CURRENT_STATUS"
    fi

    if [[ "$LOG_STREAMING" == "false" && ( "$CURRENT_STATUS" == "RUNNING" || "$CURRENT_STATUS" == "STOPPED" ) ]]; then
        LOG_STREAM_NAME=$(find_log_stream)
        if [[ -n "$LOG_STREAM_NAME" ]]; then
            echo "Log stream: ${LOG_STREAM_NAME}" >&2
            echo "─── Live Logs ───────────────────────────────────────────────────" >&2
            LOG_STREAMING=true
        else
            echo "Waiting for log stream to appear..." >&2
        fi
    fi

    if [[ "$LOG_STREAMING" == "true" ]]; then
        stream_new_logs
    fi

    if [[ "$CURRENT_STATUS" == "STOPPED" ]]; then
        break
    fi

    sleep "$POLL_INTERVAL"
    ELAPSED=$((ELAPSED + POLL_INTERVAL))
done

# Final log flush
if [[ "$LOG_STREAMING" == "false" ]]; then
    for _retry in 1 2 3; do
        sleep 3
        LOG_STREAM_NAME=$(find_log_stream)
        if [[ -n "$LOG_STREAM_NAME" ]]; then
            echo "Log stream: ${LOG_STREAM_NAME}" >&2
            echo "─── Live Logs ───────────────────────────────────────────────────" >&2
            LOG_STREAMING=true
            break
        fi
    done
fi

if [[ "$LOG_STREAMING" == "true" ]]; then
    sleep 2
    stream_new_logs
    echo "─── End of Live Logs ────────────────────────────────────────────" >&2
fi

if [[ "$CURRENT_STATUS" != "STOPPED" ]]; then
    echo "Error: task did not complete within ${TIMEOUT}s." >&2
    echo "Task ARN: ${TASK_ARN}" >&2
    echo "Last status: ${CURRENT_STATUS}" >&2
    echo "Check logs manually:" >&2
    echo "  scripts/ecs-logs.sh ${LOG_GROUP} --task ${TASK_ID}" >&2
    exit 1
fi

# ─── Step 6: Get exit code ───────────────────────────────────────────────────

EXIT_CODE=$(echo "$DESCRIBE_OUTPUT" | python3 -c "
import sys, json
task = json.load(sys.stdin)['tasks'][0]
containers = task.get('containers', [])
if containers:
    ec = containers[0].get('exitCode')
    if ec is not None:
        print(ec)
    else:
        reason = containers[0].get('reason', 'unknown')
        print(f'Container stopped without exit code: {reason}', file=sys.stderr)
        print(1)
else:
    print(1)
")

STOP_REASON=$(echo "$DESCRIBE_OUTPUT" | python3 -c "
import sys, json
task = json.load(sys.stdin)['tasks'][0]
print(task.get('stoppedReason', ''))
") || {
    echo "WARNING: failed to extract stop reason from task description" >&2
    STOP_REASON=""
}

echo "" >&2
if [[ -n "$STOP_REASON" ]]; then
    echo "Stop reason: ${STOP_REASON}" >&2
fi

# Fall back to full post-hoc log retrieval if live streaming never started
if [[ "$LOG_STREAMING" == "false" ]]; then
    echo "" >&2
    echo "─── Task Logs ───────────────────────────────────────────────────" >&2

    LOGS_RETRIEVED=false
    for _log_wait in 5 10 15; do
        echo "Waiting ${_log_wait}s for CloudWatch log stream..." >&2
        sleep "$_log_wait"
        if "$REPO_ROOT/scripts/ecs-logs.sh" "$LOG_GROUP" --task "$TASK_ID" --lines 200; then
            LOGS_RETRIEVED=true
            break
        fi
    done

    if [[ "$LOGS_RETRIEVED" == "false" ]]; then
        echo "(Could not retrieve logs after retries. Check manually with:)" >&2
        echo "  scripts/ecs-logs.sh ${LOG_GROUP} --task ${TASK_ID}" >&2
    fi
fi

echo "" >&2
echo "─────────────────────────────────────────────────────────────────" >&2
echo "Task completed with exit code: ${EXIT_CODE}" >&2

exit "$EXIT_CODE"
