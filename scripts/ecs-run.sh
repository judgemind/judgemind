#!/usr/bin/env bash
# ecs-run.sh — Run a command on an ECS container via ECS Exec.
#
# The dev database and other resources live in a private VPC. This script
# makes it easy to execute commands (especially Python scripts) inside a
# running ECS container that already has network access and environment
# variables configured.
#
# Prerequisites:
#   - AWS CLI v2 with the Session Manager plugin installed
#   - Credentials for the judgemind AWS account (155326049300)
#   - The target ECS service must be running with execute command enabled
#
# Usage:
#   scripts/ecs-run.sh <command> [args...]
#   scripts/ecs-run.sh --script <path> [-- script-args...]
#   scripts/ecs-run.sh --redeploy <command> [args...]
#   scripts/ecs-run.sh --service <name> <command> [args...]
#   scripts/ecs-run.sh --cluster <name> --service <name> <command> [args...]
#
# Options:
#   --script <path>     Transfer a local script to the container and run it.
#                       The script is base64-encoded, decoded on the container,
#                       and executed. Python scripts (.py) are run with python3;
#                       all others are run with bash. Extra arguments after --
#                       are passed to the script.
#   --redeploy          Redeploy the service first (ensures latest code)
#   --service <name>    ECS service name (default: judgemind-ingestion-worker-dev)
#   --cluster <name>    ECS cluster name (default: judgemind-dev)
#   --container <name>  Container name (default: ingestion-worker)
#
# Examples:
#   # Run a Python script on the ingestion worker
#   scripts/ecs-run.sh python3 scripts/backfill_ruling_fields.py --dry-run
#
#   # Transfer and run a local script on the container
#   scripts/ecs-run.sh --script scripts/backfill_ruling_fields.py -- --dry-run
#
#   # Redeploy first to pick up latest code, then run
#   scripts/ecs-run.sh --redeploy python3 scripts/backfill_ruling_fields.py
#
#   # Run on a different service
#   scripts/ecs-run.sh --service judgemind-api-dev python3 -c "print('hello')"
#
#   # Open an interactive shell
#   scripts/ecs-run.sh bash

set -euo pipefail

CLUSTER="judgemind-dev"
SERVICE="judgemind-ingestion-worker-dev"
CONTAINER="ingestion-worker"
REGION="us-west-2"
REDEPLOY=false
SCRIPT_PATH=""

# ─── Parse options ─────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --redeploy)
            REDEPLOY=true
            shift
            ;;
        --service)
            SERVICE="$2"
            shift 2
            ;;
        --cluster)
            CLUSTER="$2"
            shift 2
            ;;
        --container)
            CONTAINER="$2"
            shift 2
            ;;
        --script)
            SCRIPT_PATH="$2"
            shift 2
            ;;
        --help|-h)
            head -n 46 "$0" | tail -n +2 | sed 's/^# \?//'
            exit 0
            ;;
        --)
            shift
            break
            ;;
        *)
            break
            ;;
    esac
done

if [[ $# -eq 0 && -z "$SCRIPT_PATH" ]]; then
    echo "Error: no command or --script specified." >&2
    echo "" >&2
    echo "Usage: scripts/ecs-run.sh [options] <command> [args...]" >&2
    echo "       scripts/ecs-run.sh --script <path> [-- script-args...]" >&2
    echo "" >&2
    echo "Options:" >&2
    echo "  --script <path>     Transfer a local script to the container and run it" >&2
    echo "  --redeploy          Redeploy the service first (ensures latest code)" >&2
    echo "  --service <name>    ECS service name (default: judgemind-ingestion-worker-dev)" >&2
    echo "  --cluster <name>    ECS cluster name (default: judgemind-dev)" >&2
    echo "  --container <name>  Container name (default: ingestion-worker)" >&2
    echo "" >&2
    echo "Examples:" >&2
    echo "  scripts/ecs-run.sh python3 scripts/backfill_ruling_fields.py --dry-run" >&2
    echo "  scripts/ecs-run.sh --script scripts/backfill_ruling_fields.py -- --dry-run" >&2
    echo "  scripts/ecs-run.sh --redeploy python3 scripts/backfill_ruling_fields.py" >&2
    echo "  scripts/ecs-run.sh bash" >&2
    exit 1
fi

# ─── Validate --script path ──────────────────────────────────────────────

if [[ -n "$SCRIPT_PATH" ]]; then
    if [[ ! -f "$SCRIPT_PATH" ]]; then
        echo "Error: script not found: $SCRIPT_PATH" >&2
        exit 1
    fi
fi

# ─── Resolve the repo root (for calling sibling scripts) ──────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ─── Optionally redeploy ──────────────────────────────────────────────────

if [[ "$REDEPLOY" == "true" ]]; then
    echo "Redeploying $SERVICE on $CLUSTER to pick up latest code..." >&2
    "$REPO_ROOT/scripts/ecs-redeploy.sh" "$SERVICE" "$CLUSTER"
    echo "" >&2
fi

# ─── Resolve a running task ARN ──────────────────────────────────────────

echo "Finding a running task for service=$SERVICE cluster=$CLUSTER..." >&2

task_arn=$(aws ecs list-tasks \
    --cluster "$CLUSTER" \
    --service-name "$SERVICE" \
    --desired-status RUNNING \
    --region "$REGION" \
    --query 'taskArns[0]' \
    --output text 2>/dev/null)

if [[ -z "$task_arn" || "$task_arn" == "None" ]]; then
    echo "Error: no running task found for service $SERVICE in cluster $CLUSTER." >&2
    echo "" >&2
    echo "Troubleshooting:" >&2
    echo "  1. Check the service is running:" >&2
    echo "     aws ecs describe-services --cluster $CLUSTER --services $SERVICE --region $REGION \\" >&2
    echo "         --query 'services[0].{status:status,running:runningCount,desired:desiredCount}'" >&2
    echo "  2. Check ECS Exec is enabled:" >&2
    echo "     aws ecs describe-services --cluster $CLUSTER --services $SERVICE --region $REGION \\" >&2
    echo "         --query 'services[0].enableExecuteCommand'" >&2
    echo "  3. Force a new deployment:" >&2
    echo "     scripts/ecs-redeploy.sh $SERVICE $CLUSTER" >&2
    exit 1
fi

echo "Task: $task_arn" >&2
echo "" >&2

# ─── Build and execute the command ─────────────────────────────────────────

if [[ -n "$SCRIPT_PATH" ]]; then
    # --script mode: base64-encode the local file, decode and run it on the
    # container. This avoids all quoting issues with ECS Exec's --command and
    # works even though the container image doesn't include scripts/.
    encoded=$(base64 < "$SCRIPT_PATH")

    # Determine the interpreter from the file extension
    case "$SCRIPT_PATH" in
        *.py) interpreter="python3" ;;
        *)    interpreter="bash" ;;
    esac

    # Build the script args string — remaining positional args after --
    script_args=""
    for arg in "$@"; do
        # Shell-escape each argument
        script_args="$script_args '$(printf '%s' "$arg" | sed "s/'/'\\\\''/g")'"
    done

    # The remote command: decode the script to a temp file, run it, clean up
    remote_script="/tmp/_ecs_run_script"
    cmd="echo '$encoded' | base64 -d > $remote_script && $interpreter $remote_script$script_args; rm -f $remote_script"

    echo "Transferring script: $SCRIPT_PATH" >&2
    echo "Interpreter: $interpreter" >&2
    if [[ -n "$script_args" ]]; then
        echo "Script args:$script_args" >&2
    fi
else
    # Direct command mode: join all remaining args into a single command string
    cmd="$*"

    echo "Running: $cmd" >&2
fi

echo "Container: $CONTAINER" >&2
echo "─────────────────────────────────────────────────────────────" >&2

aws ecs execute-command \
    --cluster "$CLUSTER" \
    --task "$task_arn" \
    --container "$CONTAINER" \
    --interactive \
    --region "$REGION" \
    --command "$cmd"
