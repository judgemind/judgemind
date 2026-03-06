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
#   scripts/ecs-run.sh --redeploy <command> [args...]
#   scripts/ecs-run.sh --service <name> <command> [args...]
#   scripts/ecs-run.sh --cluster <name> --service <name> <command> [args...]
#
# Options:
#   --redeploy          Redeploy the service first (ensures latest code)
#   --service <name>    ECS service name (default: judgemind-ingestion-worker-dev)
#   --cluster <name>    ECS cluster name (default: judgemind-dev)
#   --container <name>  Container name (default: ingestion-worker)
#
# Examples:
#   # Run a Python script on the ingestion worker
#   scripts/ecs-run.sh python3 scripts/backfill_ruling_fields.py --dry-run
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
        --help|-h)
            head -n 36 "$0" | tail -n +2 | sed 's/^# \?//'
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

if [[ $# -eq 0 ]]; then
    echo "Error: no command specified." >&2
    echo "" >&2
    echo "Usage: scripts/ecs-run.sh [options] <command> [args...]" >&2
    echo "" >&2
    echo "Options:" >&2
    echo "  --redeploy          Redeploy the service first (ensures latest code)" >&2
    echo "  --service <name>    ECS service name (default: judgemind-ingestion-worker-dev)" >&2
    echo "  --cluster <name>    ECS cluster name (default: judgemind-dev)" >&2
    echo "  --container <name>  Container name (default: ingestion-worker)" >&2
    echo "" >&2
    echo "Examples:" >&2
    echo "  scripts/ecs-run.sh python3 scripts/backfill_ruling_fields.py --dry-run" >&2
    echo "  scripts/ecs-run.sh --redeploy python3 scripts/backfill_ruling_fields.py" >&2
    echo "  scripts/ecs-run.sh bash" >&2
    exit 1
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

# Join all remaining args into a single command string for ECS Exec
cmd="$*"

echo "Running: $cmd" >&2
echo "Container: $CONTAINER" >&2
echo "─────────────────────────────────────────────────────────────" >&2

aws ecs execute-command \
    --cluster "$CLUSTER" \
    --task "$task_arn" \
    --container "$CONTAINER" \
    --interactive \
    --region "$REGION" \
    --command "$cmd"
