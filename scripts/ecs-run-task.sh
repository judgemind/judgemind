#!/usr/bin/env bash
# ecs-run-task.sh — Run a local script as a one-off ECS Fargate task.
#
# Unlike ecs-run.sh (which uses ECS Exec / SSM sessions and is often
# unstable), this script uses the aws ecs run-task API to launch a clean
# Fargate task with the script as a command override. This is reliable,
# non-interactive, and works even when ECS Exec is broken.
#
# The script:
#   1. Reads the latest ingestion worker task definition for image, roles,
#      secrets, and networking configuration
#   2. Creates a temporary "judgemind-oneshot-dev" task definition with a
#      bash -c entrypoint
#   3. Uploads the script to S3 if it exceeds the 8192-byte command override
#      limit (downloaded in-container via pre-signed URL — no AWS CLI needed),
#      otherwise base64-encodes it inline
#   4. Runs the task, polls for completion, and streams CloudWatch logs
#   5. Cleans up the temporary task definition (and S3 object) on exit
#
# Prerequisites:
#   - AWS CLI v2
#   - Credentials for the judgemind AWS account (155326049300)
#   - The ingestion worker task definition must exist
#
# Usage:
#   scripts/ecs-run-task.sh <script-path> [-- script-args...]
#   scripts/ecs-run-task.sh --env dev <script-path> [-- script-args...]
#   scripts/ecs-run-task.sh --dry-run <script-path>
#
# Options:
#   --env <env>         Environment (default: dev)
#   --dry-run           Show what would be done without running
#   --timeout <secs>    Max seconds to wait for task completion (default: 1800)
#   --help              Show this help message
#
# Examples:
#   scripts/ecs-run-task.sh scripts/backfill_ruling_fields.py -- --dry-run
#   scripts/ecs-run-task.sh scripts/cleanup_no_tentative_rulings.py
#   scripts/ecs-run-task.sh --timeout 3600 scripts/backfill_parties.py

set -euo pipefail

# ─── Defaults ────────────────────────────────────────────────────────────────

ENVIRONMENT="dev"
DRY_RUN=false
TIMEOUT=1800
SCRIPT_PATH=""
REGION="us-west-2"
SCRIPT_ARGS=()

# Track resources for cleanup
ONESHOT_TASK_DEF_ARN=""
S3_SCRIPT_KEY=""
S3_BUCKET="judgemind-assets-dev"
TMP_DIR=""

# ─── Parse options ───────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env)
            ENVIRONMENT="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --timeout)
            TIMEOUT="$2"
            shift 2
            ;;
        --help|-h)
            head -n 37 "$0" | tail -n +2 | sed 's/^# \?//'
            exit 0
            ;;
        --)
            shift
            SCRIPT_ARGS=("$@")
            break
            ;;
        -*)
            echo "Error: unknown option '$1'" >&2
            echo "Run 'scripts/ecs-run-task.sh --help' for usage." >&2
            exit 1
            ;;
        *)
            if [[ -n "$SCRIPT_PATH" ]]; then
                echo "Error: unexpected argument '$1' (script path already set to '$SCRIPT_PATH')" >&2
                exit 1
            fi
            SCRIPT_PATH="$1"
            shift
            ;;
    esac
done

if [[ -z "$SCRIPT_PATH" ]]; then
    echo "Error: script path is required." >&2
    echo "" >&2
    echo "Usage: scripts/ecs-run-task.sh <script-path> [-- script-args...]" >&2
    echo "Run 'scripts/ecs-run-task.sh --help' for full usage." >&2
    exit 1
fi

if [[ ! -f "$SCRIPT_PATH" ]]; then
    echo "Error: script not found: $SCRIPT_PATH" >&2
    exit 1
fi

# Update bucket name for the environment
S3_BUCKET="judgemind-assets-${ENVIRONMENT}"

# ─── Cleanup trap ────────────────────────────────────────────────────────────

cleanup() {
    local exit_code=$?

    if [[ -n "$ONESHOT_TASK_DEF_ARN" ]]; then
        echo "" >&2
        echo "Cleaning up: deregistering oneshot task definition..." >&2
        aws ecs deregister-task-definition \
            --task-definition "$ONESHOT_TASK_DEF_ARN" \
            --region "$REGION" \
            --no-cli-pager \
            --output text > /dev/null 2>&1 || true
    fi

    if [[ -n "$S3_SCRIPT_KEY" ]]; then
        echo "Cleaning up: removing script from S3..." >&2
        aws s3 rm "s3://${S3_BUCKET}/${S3_SCRIPT_KEY}" \
            --region "$REGION" > /dev/null 2>&1 || true
    fi

    if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
        rm -rf "$TMP_DIR"
    fi

    return $exit_code
}

trap cleanup EXIT

# ─── Resolve the repo root ──────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Create a temporary directory for intermediate files
TMP_DIR=$(mktemp -d)

# ─── Step 1: Read the latest ingestion worker task definition ────────────────

SOURCE_FAMILY="judgemind-ingestion-worker-${ENVIRONMENT}"
CLUSTER="judgemind-${ENVIRONMENT}"

echo "Reading latest task definition for ${SOURCE_FAMILY}..." >&2

SOURCE_TASK_DEF=$(aws ecs describe-task-definition \
    --task-definition "$SOURCE_FAMILY" \
    --region "$REGION" \
    --output json \
    --no-cli-pager 2>/dev/null) || {
    echo "Error: could not read task definition '${SOURCE_FAMILY}'." >&2
    echo "Ensure the ingestion worker is deployed in the '${ENVIRONMENT}' environment." >&2
    exit 1
}

# ─── Step 2: Prepare the script payload ──────────────────────────────────────

SCRIPT_SIZE=$(wc -c < "$SCRIPT_PATH" | tr -d ' ')
SCRIPT_BASENAME=$(basename "$SCRIPT_PATH")
ONESHOT_ID="oneshot-$(date +%s)-$$"
USE_S3=false

# Determine interpreter
case "$SCRIPT_PATH" in
    *.py) INTERPRETER="python3" ;;
    *)    INTERPRETER="bash" ;;
esac

# Build script args string
ARGS_STR=""
for arg in "${SCRIPT_ARGS[@]+"${SCRIPT_ARGS[@]}"}"; do
    # Shell-escape each argument
    escaped=$(printf '%s' "$arg" | sed "s/'/'\\\\''/g")
    ARGS_STR="${ARGS_STR} '${escaped}'"
done

# The ECS run-task command override limit is 8192 bytes total for the
# containerOverrides JSON. The base64-encoded script plus the wrapper
# command must fit within this limit. We use ~6000 bytes as the threshold
# to leave room for the wrapper shell command.
ENCODED_SIZE=0
if [[ "$SCRIPT_SIZE" -gt 0 ]]; then
    # base64 encoding expands by ~4/3
    ENCODED_SIZE=$(( (SCRIPT_SIZE * 4 + 2) / 3 ))
fi

if [[ "$ENCODED_SIZE" -gt 6000 ]]; then
    USE_S3=true
    S3_SCRIPT_KEY="oneshot-scripts/${ONESHOT_ID}/${SCRIPT_BASENAME}"
    echo "Script is ${SCRIPT_SIZE} bytes (exceeds inline limit)." >&2
    echo "Uploading to s3://${S3_BUCKET}/${S3_SCRIPT_KEY}..." >&2

    if [[ "$DRY_RUN" == "false" ]]; then
        aws s3 cp "$SCRIPT_PATH" "s3://${S3_BUCKET}/${S3_SCRIPT_KEY}" \
            --region "$REGION" \
            --no-cli-pager > /dev/null

        # Generate a pre-signed URL (5 min TTL) so the container can download
        # the script without needing the AWS CLI installed.
        PRESIGNED_URL=$(aws s3 presign "s3://${S3_BUCKET}/${S3_SCRIPT_KEY}" \
            --region "$REGION" \
            --expires-in 300)
    else
        PRESIGNED_URL="https://${S3_BUCKET}.s3.${REGION}.amazonaws.com/${S3_SCRIPT_KEY}?PRESIGNED_PLACEHOLDER"
    fi

    # Use Python's urllib to download — always available in the container
    # image (python:3.12-slim). Avoids "curl: command not found" noise.
    COMMAND_STR="python3 -c \"import urllib.request; urllib.request.urlretrieve('${PRESIGNED_URL}', '/tmp/_oneshot_script')\" && ${INTERPRETER} /tmp/_oneshot_script${ARGS_STR}"
else
    ENCODED=$(base64 < "$SCRIPT_PATH")
    COMMAND_STR="echo ${ENCODED} | base64 -d > /tmp/_oneshot_script && ${INTERPRETER} /tmp/_oneshot_script${ARGS_STR}"
fi

echo "Script: ${SCRIPT_PATH} (${SCRIPT_SIZE} bytes)" >&2
echo "Interpreter: ${INTERPRETER}" >&2
echo "Delivery: $(if [[ "$USE_S3" == "true" ]]; then echo "S3 (pre-signed URL, 5 min TTL)"; else echo "inline (base64)"; fi)" >&2
if [[ ${#SCRIPT_ARGS[@]} -gt 0 ]]; then
    echo "Script args:${ARGS_STR}" >&2
fi
echo "" >&2

# ─── Step 3: Register the oneshot task definition ────────────────────────────

ONESHOT_FAMILY="judgemind-oneshot-${ENVIRONMENT}"

# We need to extract fields from the source task def and build the oneshot
# task def JSON. Use a Python helper to avoid complex shell JSON manipulation.
cat > "${TMP_DIR}/_build_task_def.py" << 'PYEOF'
import json
import sys
import os

source_td = json.loads(sys.stdin.read())["taskDefinition"]
command_str = os.environ["COMMAND_STR"]
family = os.environ["ONESHOT_FAMILY"]

# Get the first container definition as a template
source_container = source_td["containerDefinitions"][0]

# Build the oneshot container definition
container = {
    "name": "oneshot",
    "image": source_container["image"],
    "essential": True,
    "entryPoint": ["bash", "-c"],
    "command": [command_str],
    "environment": source_container.get("environment", []),
    "secrets": source_container.get("secrets", []),
    "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
            "awslogs-group": source_container["logConfiguration"]["options"]["awslogs-group"],
            "awslogs-region": source_container["logConfiguration"]["options"]["awslogs-region"],
            "awslogs-stream-prefix": "oneshot",
        },
    },
}

task_def = {
    "family": family,
    "requiresCompatibilities": ["FARGATE"],
    "networkMode": "awsvpc",
    "cpu": source_td.get("cpu", "256"),
    "memory": source_td.get("memory", "512"),
    "executionRoleArn": source_td["executionRoleArn"],
    "taskRoleArn": source_td.get("taskRoleArn", ""),
    "containerDefinitions": [container],
}

# Remove taskRoleArn if empty
if not task_def["taskRoleArn"]:
    del task_def["taskRoleArn"]

json.dump(task_def, sys.stdout, indent=2)
PYEOF

echo "Building oneshot task definition (family=${ONESHOT_FAMILY})..." >&2

TASK_DEF_JSON=$(echo "$SOURCE_TASK_DEF" | \
    COMMAND_STR="$COMMAND_STR" \
    ONESHOT_FAMILY="$ONESHOT_FAMILY" \
    python3 "${TMP_DIR}/_build_task_def.py")

if [[ "$DRY_RUN" == "true" ]]; then
    echo "=== DRY RUN ===" >&2
    echo "Would register task definition:" >&2
    echo "$TASK_DEF_JSON" | python3 -m json.tool >&2
    echo "" >&2
    echo "Would run task on cluster ${CLUSTER} with networking from ingestion worker service." >&2
    exit 0
fi

# Write the task def to a temp file for the register command
echo "$TASK_DEF_JSON" > "${TMP_DIR}/_oneshot_task_def.json"

REGISTER_OUTPUT=$(aws ecs register-task-definition \
    --cli-input-json "file://${TMP_DIR}/_oneshot_task_def.json" \
    --region "$REGION" \
    --output json \
    --no-cli-pager)

ONESHOT_TASK_DEF_ARN=$(echo "$REGISTER_OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin)['taskDefinition']['taskDefinitionArn'])")

echo "Registered: ${ONESHOT_TASK_DEF_ARN}" >&2

# ─── Step 4: Resolve networking from the ingestion worker service ────────────

SERVICE_NAME="judgemind-ingestion-worker-${ENVIRONMENT}"

echo "Resolving networking from service ${SERVICE_NAME}..." >&2

NETWORK_CONFIG=$(aws ecs describe-services \
    --cluster "$CLUSTER" \
    --services "$SERVICE_NAME" \
    --region "$REGION" \
    --output json \
    --no-cli-pager \
    --query 'services[0].networkConfiguration.awsvpcConfiguration')

if [[ -z "$NETWORK_CONFIG" || "$NETWORK_CONFIG" == "null" ]]; then
    echo "Error: could not resolve networking from service ${SERVICE_NAME}." >&2
    exit 1
fi

# Extract subnets and security groups using Python
SUBNETS=$(echo "$NETWORK_CONFIG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(','.join(d['subnets']))")
SECURITY_GROUPS=$(echo "$NETWORK_CONFIG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(','.join(d['securityGroups']))")

echo "Subnets: ${SUBNETS}" >&2
echo "Security groups: ${SECURITY_GROUPS}" >&2

# ─── Step 5: Run the task ────────────────────────────────────────────────────

echo "" >&2
echo "Launching oneshot task..." >&2

RUN_OUTPUT=$(aws ecs run-task \
    --cluster "$CLUSTER" \
    --task-definition "$ONESHOT_TASK_DEF_ARN" \
    --launch-type FARGATE \
    --region "$REGION" \
    --output json \
    --no-cli-pager \
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

# Extract just the task ID from the ARN for log stream lookup
TASK_ID=$(echo "$TASK_ARN" | rev | cut -d'/' -f1 | rev)

echo "Task ARN: ${TASK_ARN}" >&2
echo "Task ID:  ${TASK_ID}" >&2
echo "" >&2

# ─── Step 6: Poll for completion with real-time log streaming ────────────────

echo "Waiting for task to complete (timeout: ${TIMEOUT}s)..." >&2

POLL_INTERVAL=10
ELAPSED=0
LAST_STATUS=""

# Log streaming state
LOG_GROUP="/ecs/judgemind-ingestion-worker-${ENVIRONMENT}"
LOG_STREAM_NAME=""
LOG_NEXT_TOKEN=""
LOG_STREAMING=false

# find_log_stream — Locate the CloudWatch log stream for this task.
# The stream name follows the pattern: oneshot/oneshot/<task-id>
# Returns the stream name, or empty string if not found yet.
find_log_stream() {
    aws logs describe-log-streams \
        --log-group-name "$LOG_GROUP" \
        --log-stream-name-prefix "oneshot/oneshot/" \
        --order-by LogStreamName \
        --max-items 50 \
        --region "$REGION" \
        --output text \
        --query "logStreams[*].logStreamName" \
        --no-cli-pager 2>/dev/null | tr '\t' '\n' | grep -F "$TASK_ID" | head -n 1 || true
}

# stream_new_logs — Fetch and print any new log events since the last check.
# Uses LOG_NEXT_TOKEN to track position; updates it after each call.
#
# On the first call (no token), we pass --start-from-head so events are
# returned in chronological order. On subsequent calls we pass the forward
# token instead — --start-from-head must NOT be combined with --next-token
# because the token already encodes position.
#
# Note: --start-from-head is a boolean *flag* (no value argument). Passing
# "true" as a separate positional arg causes the CLI to fail silently.
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
        # Use forward token from previous call (already encodes position)
        args+=(--next-token "$LOG_NEXT_TOKEN")
    else
        # First call — read from the beginning of the stream
        args+=(--start-from-head)
    fi

    local result
    result=$(aws "${args[@]}" 2>/dev/null) || return 0

    # Extract log messages and forward token from the response
    local messages new_token
    messages=$(echo "$result" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for event in data.get('events', []):
    print(event.get('message', '').rstrip())
" 2>/dev/null) || true

    new_token=$(echo "$result" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('nextForwardToken', ''))
" 2>/dev/null) || true

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
        --no-cli-pager 2>/dev/null)

    CURRENT_STATUS=$(echo "$DESCRIBE_OUTPUT" | python3 -c "import sys,json; t=json.load(sys.stdin)['tasks'][0]; print(t['lastStatus'])")

    if [[ "$CURRENT_STATUS" != "$LAST_STATUS" ]]; then
        echo "Status: ${CURRENT_STATUS}" >&2
        LAST_STATUS="$CURRENT_STATUS"
    fi

    # Start log streaming once the task is RUNNING (or already STOPPED).
    # The log stream may not exist immediately — CloudWatch creates it on
    # first write, which lags container start by a few seconds. Keep
    # retrying on every poll iteration until we find it.
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

    # Stream any new log events
    if [[ "$LOG_STREAMING" == "true" ]]; then
        stream_new_logs
    fi

    if [[ "$CURRENT_STATUS" == "STOPPED" ]]; then
        break
    fi

    sleep "$POLL_INTERVAL"
    ELAPSED=$((ELAPSED + POLL_INTERVAL))
done

# Final log flush — capture any events that arrived after the last poll.
# If we never found the log stream during the poll loop (e.g. for very
# short-lived tasks), retry a few times with a brief delay — CloudWatch
# may still be creating the stream.
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
    echo "You can check logs manually:" >&2
    echo "  scripts/ecs-logs.sh /ecs/judgemind-ingestion-worker-${ENVIRONMENT} --task ${TASK_ID}" >&2
    exit 1
fi

# ─── Step 7: Get exit code ───────────────────────────────────────────────────

EXIT_CODE=$(echo "$DESCRIBE_OUTPUT" | python3 -c "
import sys, json
task = json.load(sys.stdin)['tasks'][0]
containers = task.get('containers', [])
if containers:
    ec = containers[0].get('exitCode')
    if ec is not None:
        print(ec)
    else:
        # No exit code means the container was killed
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
" 2>/dev/null) || true

echo "" >&2
if [[ -n "$STOP_REASON" ]]; then
    echo "Stop reason: ${STOP_REASON}" >&2
fi

# ─── Step 8: Retrieve and display CloudWatch logs (fallback) ─────────────────

# If live streaming was active, logs were already shown above. Only do the
# full post-hoc retrieval if we never managed to stream (e.g. log stream
# wasn't found during the poll loop, or the task was very short-lived).

if [[ "$LOG_STREAMING" == "false" ]]; then
    echo "" >&2
    echo "─── Task Logs ───────────────────────────────────────────────────" >&2

    # Give CloudWatch a moment to flush
    sleep 3

    # Use the existing ecs-logs.sh script to retrieve logs
    "$REPO_ROOT/scripts/ecs-logs.sh" "$LOG_GROUP" --task "$TASK_ID" --lines 200 2>/dev/null || {
        echo "(Could not retrieve logs. Check manually with:)" >&2
        echo "  scripts/ecs-logs.sh ${LOG_GROUP} --task ${TASK_ID}" >&2
    }
fi

# ─── Done ────────────────────────────────────────────────────────────────────

echo "" >&2
echo "─────────────────────────────────────────────────────────────────" >&2
echo "Task completed with exit code: ${EXIT_CODE}" >&2

exit "$EXIT_CODE"
