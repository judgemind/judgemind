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
#   scripts/ecs-run-task.sh --detach <script-path> [-- script-args...]
#   scripts/ecs-run-task.sh --logs <task-arn>
#   scripts/ecs-run-task.sh --dry-run <script-path>
#
# Options:
#   --env <env>         Environment (default: dev)
#   --cpu <units>       CPU units for the task (default: 1024). Valid: 256, 512, 1024, 2048, 4096.
#   --memory <mb>       Memory in MB for the task (default: 2048). Must be valid for the CPU value.
#   --latest-image      Use the latest image from ECR instead of the running service's image
#   --detach            Launch the task and exit immediately. Prints the task ARN
#                       to stdout for later use with --logs.
#   --logs <task-arn>   Retrieve logs and status for a previously launched task.
#                       Accepts a full task ARN.
#   --dry-run           Show what would be done without running
#   --timeout <secs>    Max seconds to wait for task completion (default: 1800)
#   --help              Show this help message
#
# Examples:
#   scripts/ecs-run-task.sh scripts/backfill_ruling_fields.py -- --dry-run
#   scripts/ecs-run-task.sh scripts/cleanup_no_tentative_rulings.py
#   scripts/ecs-run-task.sh --timeout 3600 scripts/backfill_parties.py
#   scripts/ecs-run-task.sh --detach scripts/reingest_from_s3.py -- --all
#   scripts/ecs-run-task.sh --logs arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/abc123

set -euo pipefail

# ─── Defaults ────────────────────────────────────────────────────────────────

ENVIRONMENT="dev"
DRY_RUN=false
DETACH=false
LOGS_TASK_ARN=""
TIMEOUT=1800
CPU_OVERRIDE=""
MEMORY_OVERRIDE=""
SCRIPT_PATH=""
REGION="us-west-2"
SCRIPT_ARGS=()
USE_LATEST_IMAGE=false

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
        --cpu)
            CPU_OVERRIDE="$2"
            shift 2
            ;;
        --memory)
            MEMORY_OVERRIDE="$2"
            shift 2
            ;;
        --latest-image)
            USE_LATEST_IMAGE=true
            shift
            ;;
        --detach)
            DETACH=true
            shift
            ;;
        --logs)
            LOGS_TASK_ARN="$2"
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
            head -n 51 "$0" | tail -n +2 | sed 's/^# \?//'
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

# ─── --logs mode: retrieve logs for a previously launched task ─────────────

if [[ -n "$LOGS_TASK_ARN" ]]; then
    # Extract cluster and task ID from the ARN.
    # ARN format: arn:aws:ecs:<region>:<account>:task/<cluster>/<task-id>
    CLUSTER_FROM_ARN=$(echo "$LOGS_TASK_ARN" | cut -d'/' -f2)
    TASK_ID_FROM_ARN=$(echo "$LOGS_TASK_ARN" | rev | cut -d'/' -f1 | rev)

    if [[ -z "$CLUSTER_FROM_ARN" || -z "$TASK_ID_FROM_ARN" ]]; then
        echo "Error: could not parse task ARN: $LOGS_TASK_ARN" >&2
        echo "Expected format: arn:aws:ecs:<region>:<account>:task/<cluster>/<task-id>" >&2
        exit 1
    fi

    # Describe the task to get its status
    echo "Task ARN: ${LOGS_TASK_ARN}" >&2

    TASK_DESCRIBE=$(aws ecs describe-tasks \
        --cluster "$CLUSTER_FROM_ARN" \
        --tasks "$LOGS_TASK_ARN" \
        --region "$REGION" \
        --output json \
        --no-cli-pager 2>/dev/null) || {
        echo "Error: could not describe task. Check the ARN and your AWS credentials." >&2
        exit 1
    }

    # Use a temp file for the Python extraction
    LOGS_TMP_DIR=$(mktemp -d)
    trap "rm -rf $LOGS_TMP_DIR" EXIT

    cat > "${LOGS_TMP_DIR}/_extract_task_info.py" << 'PYEOF'
import json, sys
data = json.load(sys.stdin)
tasks = data.get("tasks", [])
if not tasks:
    print("ERROR: task not found", file=sys.stderr)
    sys.exit(1)
task = tasks[0]
status = task.get("lastStatus", "UNKNOWN")
exit_code = None
containers = task.get("containers", [])
if containers:
    exit_code = containers[0].get("exitCode")
stop_reason = task.get("stoppedReason", "")
# Print status info to stderr
print(f"Status: {status}", file=sys.stderr)
if stop_reason:
    print(f"Stop reason: {stop_reason}", file=sys.stderr)
if exit_code is not None:
    print(f"Exit code: {exit_code}", file=sys.stderr)
# Print exit code to stdout for the shell to capture.
# If the task is still running, print "RUNNING" so the caller knows.
if status == "STOPPED" and exit_code is not None:
    print(exit_code)
elif status == "STOPPED":
    print(1)
else:
    print("RUNNING")
PYEOF

    LOGS_EXIT_INFO=$(echo "$TASK_DESCRIBE" | python3 "${LOGS_TMP_DIR}/_extract_task_info.py")

    # Determine the log group from the environment in the cluster name
    # Cluster name is like "judgemind-dev" → environment is "dev"
    LOGS_ENV=$(echo "$CLUSTER_FROM_ARN" | rev | cut -d'-' -f1 | rev)
    LOGS_LOG_GROUP="/ecs/judgemind-ingestion-worker-${LOGS_ENV}"

    echo "" >&2
    echo "─── Task Logs ───────────────────────────────────────────────────" >&2

    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

    "$REPO_ROOT/scripts/ecs-logs.sh" "$LOGS_LOG_GROUP" --task "$TASK_ID_FROM_ARN" --lines 200 2>/dev/null || {
        echo "(Could not retrieve logs. Check manually with:)" >&2
        echo "  scripts/ecs-logs.sh ${LOGS_LOG_GROUP} --task ${TASK_ID_FROM_ARN}" >&2
    }

    # Exit with the task's exit code if stopped, or 0 if still running
    if [[ "$LOGS_EXIT_INFO" == "RUNNING" ]]; then
        echo "" >&2
        echo "Task is still running. Re-run this command later to check again." >&2
        exit 0
    else
        exit "$LOGS_EXIT_INFO"
    fi
fi

# ─── Normal mode: require a script path ────────────────────────────────────

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

# ─── Resolve CPU and memory ────────────────────────────────────────────────
# Defaults: 1024 CPU / 2048 MB — enough for batch processing and PDF extraction.
# These override whatever the source task definition uses.
CPU="${CPU_OVERRIDE:-1024}"
MEMORY="${MEMORY_OVERRIDE:-2048}"

# Validate Fargate CPU/memory combinations
# https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-cpu-memory-error.html
validate_fargate_resources() {
    local cpu="$1" mem="$2"
    case "$cpu" in
        256)  [[ "$mem" == 512 || "$mem" == 1024 || "$mem" == 2048 ]] && return 0 ;;
        512)  [[ "$mem" -ge 1024 && "$mem" -le 4096 && $(( mem % 1024 )) -eq 0 ]] && return 0 ;;
        1024) [[ "$mem" -ge 2048 && "$mem" -le 8192 && $(( mem % 1024 )) -eq 0 ]] && return 0 ;;
        2048) [[ "$mem" -ge 4096 && "$mem" -le 16384 && $(( mem % 1024 )) -eq 0 ]] && return 0 ;;
        4096) [[ "$mem" -ge 8192 && "$mem" -le 30720 && $(( mem % 1024 )) -eq 0 ]] && return 0 ;;
    esac
    return 1
}

if ! validate_fargate_resources "$CPU" "$MEMORY"; then
    echo "Error: invalid Fargate CPU/memory combination: ${CPU} CPU / ${MEMORY} MB." >&2
    echo "Valid combinations:" >&2
    echo "  256 CPU:  512, 1024, 2048 MB" >&2
    echo "  512 CPU:  1024-4096 MB (1 GB increments)" >&2
    echo "  1024 CPU: 2048-8192 MB (1 GB increments)" >&2
    echo "  2048 CPU: 4096-16384 MB (1 GB increments)" >&2
    echo "  4096 CPU: 8192-30720 MB (1 GB increments)" >&2
    exit 1
fi

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

    if [[ -n "$S3_SCRIPT_KEY" && "$DETACH" == "false" ]]; then
        echo "Cleaning up: removing script from S3..." >&2
        aws s3 rm "s3://${S3_BUCKET}/${S3_SCRIPT_KEY}" \
            --region "$REGION" > /dev/null 2>&1 || true
    elif [[ -n "$S3_SCRIPT_KEY" && "$DETACH" == "true" ]]; then
        echo "Detach mode: S3 script at s3://${S3_BUCKET}/${S3_SCRIPT_KEY} left for task to download." >&2
        echo "Clean it up manually after the task completes, or it will expire from bucket lifecycle rules." >&2
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

# ─── Step 1b: Detect stale image and optionally use latest from ECR ──────

# Extract the image URI from the source task definition's first container.
SERVICE_IMAGE=$(echo "$SOURCE_TASK_DEF" | python3 -c "
import sys, json
td = json.load(sys.stdin)['taskDefinition']
print(td['containerDefinitions'][0]['image'])
")

# Derive the ECR repository URI (everything before the colon/tag or @sha256:digest)
# e.g. "155326049300.dkr.ecr.us-west-2.amazonaws.com/judgemind/scraper:abc123"
#   -> repo URI: "155326049300.dkr.ecr.us-west-2.amazonaws.com/judgemind/scraper"
#   -> repo name: "judgemind/scraper"
ECR_REPO_URI="${SERVICE_IMAGE%%:*}"
ECR_REPO_URI="${ECR_REPO_URI%%@*}"
# Extract just the repository name (after the hostname, e.g.
# "155326049300.dkr.ecr.us-west-2.amazonaws.com/judgemind/scraper"
#   -> "judgemind/scraper")
ECR_REPO_NAME="${ECR_REPO_URI#*.amazonaws.com/}"

# Resolve the digest of the image currently used by the service
SERVICE_IMAGE_TAG="${SERVICE_IMAGE#*:}"
if [[ "$SERVICE_IMAGE_TAG" == "$SERVICE_IMAGE" ]]; then
    # No tag found — image might use @sha256: digest format
    SERVICE_IMAGE_TAG="latest"
fi

# Get the digest of the service's image
SERVICE_DIGEST=$(aws ecr describe-images \
    --repository-name "$ECR_REPO_NAME" \
    --image-ids "imageTag=${SERVICE_IMAGE_TAG}" \
    --region "$REGION" \
    --query 'imageDetails[0].imageDigest' \
    --output text \
    --no-cli-pager 2>/dev/null) || SERVICE_DIGEST=""

# Get the latest image from ECR (most recently pushed)
LATEST_ECR_INFO=$(aws ecr describe-images \
    --repository-name "$ECR_REPO_NAME" \
    --image-ids imageTag=latest \
    --region "$REGION" \
    --query 'imageDetails[0].{digest:imageDigest,pushed:imagePushedAt}' \
    --output json \
    --no-cli-pager 2>/dev/null) || LATEST_ECR_INFO=""

if [[ -n "$LATEST_ECR_INFO" && "$LATEST_ECR_INFO" != "null" ]]; then
    LATEST_DIGEST=$(echo "$LATEST_ECR_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['digest'])")
    LATEST_PUSHED=$(echo "$LATEST_ECR_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['pushed'])")

    if [[ -n "$SERVICE_DIGEST" && "$SERVICE_DIGEST" != "$LATEST_DIGEST" ]]; then
        if [[ "$USE_LATEST_IMAGE" == "true" ]]; then
            echo "Using latest ECR image (pushed ${LATEST_PUSHED})." >&2
            echo "Service image (tag=${SERVICE_IMAGE_TAG}) has a different digest — overriding." >&2
        else
            echo "" >&2
            echo "WARNING: The running service's image differs from the latest in ECR." >&2
            echo "  Service image tag: ${SERVICE_IMAGE_TAG}" >&2
            echo "  Service digest:    ${SERVICE_DIGEST}" >&2
            echo "  Latest digest:     ${LATEST_DIGEST}" >&2
            echo "  Latest pushed:     ${LATEST_PUSHED}" >&2
            echo "" >&2
            echo "The oneshot task may run with stale code. To use the latest image:" >&2
            echo "  scripts/ecs-run-task.sh --latest-image <script-path> [-- args...]" >&2
            echo "" >&2
        fi
    else
        # Digests match — service is up to date
        if [[ "$USE_LATEST_IMAGE" == "true" ]]; then
            echo "Service image already matches the latest ECR image — no override needed." >&2
        fi
    fi
else
    # Could not resolve latest ECR image — skip the check
    if [[ "$USE_LATEST_IMAGE" == "true" ]]; then
        echo "WARNING: Could not resolve latest image from ECR repository '${ECR_REPO_NAME}'." >&2
        echo "Falling back to the service's image." >&2
    fi
    # Reset the flag so we don't try to override below
    USE_LATEST_IMAGE=false
fi

# If --latest-image was requested and digests differ, override the image in the
# task definition. We do this by setting an env var that the Python task-def
# builder (Step 3) will pick up.
if [[ "$USE_LATEST_IMAGE" == "true" && -n "$LATEST_DIGEST" && "$SERVICE_DIGEST" != "$LATEST_DIGEST" ]]; then
    OVERRIDE_IMAGE="${ECR_REPO_URI}:latest"
else
    OVERRIDE_IMAGE=""
fi

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

# Create a stub _venv_helper module for Python scripts that import it.
# In the container, deps are installed system-wide so venv activation is
# unnecessary, but the import would fail because _venv_helper.py lives in
# scripts/ which is not present in the container image.  We write a no-op
# stub to /tmp and prepend PYTHONPATH so the import succeeds harmlessly.
if [[ "$INTERPRETER" == "python3" ]]; then
    VENV_HELPER_STUB="printf 'def ensure_venv(*a,**k):pass\n' > /tmp/_venv_helper.py && export PYTHONPATH=/tmp:\${PYTHONPATH:-} && "
else
    VENV_HELPER_STUB=""
fi

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

        # Generate a pre-signed URL (15 min TTL) so the container can download
        # the script without needing the AWS CLI installed. 15 minutes allows
        # for Fargate cold starts which can exceed 5 minutes.
        PRESIGNED_URL=$(aws s3 presign "s3://${S3_BUCKET}/${S3_SCRIPT_KEY}" \
            --region "$REGION" \
            --expires-in 900)
    else
        PRESIGNED_URL="https://${S3_BUCKET}.s3.${REGION}.amazonaws.com/${S3_SCRIPT_KEY}?PRESIGNED_PLACEHOLDER"
    fi

    # Use Python's urllib to download — always available in the container
    # image (python:3.12-slim). Avoids "curl: command not found" noise.
    COMMAND_STR="${VENV_HELPER_STUB}python3 -c \"import urllib.request; urllib.request.urlretrieve('${PRESIGNED_URL}', '/tmp/_oneshot_script')\" && ${INTERPRETER} /tmp/_oneshot_script${ARGS_STR}"
else
    ENCODED=$(base64 < "$SCRIPT_PATH")
    COMMAND_STR="${VENV_HELPER_STUB}echo ${ENCODED} | base64 -d > /tmp/_oneshot_script && ${INTERPRETER} /tmp/_oneshot_script${ARGS_STR}"
fi

echo "Script: ${SCRIPT_PATH} (${SCRIPT_SIZE} bytes)" >&2
echo "Interpreter: ${INTERPRETER}" >&2
echo "Resources: ${CPU} CPU / ${MEMORY} MB memory" >&2
if [[ -n "$OVERRIDE_IMAGE" ]]; then
    echo "Image: ${OVERRIDE_IMAGE} (--latest-image)" >&2
else
    echo "Image: ${SERVICE_IMAGE} (from service task def)" >&2
fi
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
cpu = os.environ["ONESHOT_CPU"]
memory = os.environ["ONESHOT_MEMORY"]
override_image = os.environ.get("OVERRIDE_IMAGE", "")

# Get the first container definition as a template
source_container = source_td["containerDefinitions"][0]

# Use the override image if provided (--latest-image flag), otherwise use
# the image from the running service's task definition.
image = override_image if override_image else source_container["image"]

# Build the oneshot container definition
container = {
    "name": "oneshot",
    "image": image,
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
    "cpu": cpu,
    "memory": memory,
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
    ONESHOT_CPU="$CPU" \
    ONESHOT_MEMORY="$MEMORY" \
    OVERRIDE_IMAGE="${OVERRIDE_IMAGE}" \
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

# Print the log location so the user can find logs easily
LOG_GROUP="/ecs/judgemind-ingestion-worker-${ENVIRONMENT}"
LOG_STREAM="oneshot/oneshot/${TASK_ID}"
echo "" >&2
echo "Logs:" >&2
echo "  Log group:  ${LOG_GROUP}" >&2
echo "  Log stream: ${LOG_STREAM}" >&2
echo "  Tail logs:  scripts/ecs-task-logs.sh ${TASK_ID}" >&2
echo "  Full status: scripts/ecs-run-task.sh --logs ${TASK_ARN}" >&2
echo "" >&2

# ─── Detach mode: print ARN to stdout and exit ──────────────────────────────

if [[ "$DETACH" == "true" ]]; then
    echo "Detach mode: task launched successfully." >&2

    # Print the task ARN to stdout (everything else goes to stderr)
    # so callers can capture it easily.
    echo "${TASK_ARN}"
    exit 0
fi

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
