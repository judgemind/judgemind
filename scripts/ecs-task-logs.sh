#!/usr/bin/env bash
# ecs-task-logs.sh — Tail CloudWatch logs for an ECS oneshot task.
#
# A convenience wrapper around ecs-logs.sh that handles the log group
# and task ID automatically. Saves you from having to remember the log
# group name and stream naming convention.
#
# Usage:
#   scripts/ecs-task-logs.sh <task-id>
#   scripts/ecs-task-logs.sh <task-id> --follow
#   scripts/ecs-task-logs.sh <task-id> --lines 100
#   scripts/ecs-task-logs.sh <task-arn>
#   scripts/ecs-task-logs.sh --env production <task-id>
#
# Options:
#   --env <env>     Environment (default: dev)
#   --follow, -f    Poll for new events (like tail -f)
#   --lines N       Number of lines to show (default: 30)
#   --help          Show this help message
#
# The task-id can be:
#   - A bare task ID (e.g. abc123def456)
#   - A full task ARN (arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/abc123def456)
#
# Examples:
#   scripts/ecs-task-logs.sh abc123def456
#   scripts/ecs-task-logs.sh abc123def456 --follow
#   scripts/ecs-task-logs.sh arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/abc123 --lines 50

set -euo pipefail

# ─── Defaults ──────────────────────────────────────────────────────────────

ENVIRONMENT="dev"
TASK_INPUT=""
EXTRA_ARGS=()

# ─── Parse options ─────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env)
            ENVIRONMENT="$2"
            shift 2
            ;;
        --help|-h)
            head -n 28 "$0" | tail -n +2 | sed 's/^# \?//'
            exit 0
            ;;
        --follow|-f|--lines|-n)
            # Pass through to ecs-logs.sh
            if [[ "$1" == "--lines" || "$1" == "-n" ]]; then
                EXTRA_ARGS+=("$1" "$2")
                shift 2
            else
                EXTRA_ARGS+=("$1")
                shift
            fi
            ;;
        -*)
            echo "Error: unknown option '$1'" >&2
            echo "Run 'scripts/ecs-task-logs.sh --help' for usage." >&2
            exit 1
            ;;
        *)
            if [[ -n "$TASK_INPUT" ]]; then
                echo "Error: unexpected argument '$1' (task ID already set to '$TASK_INPUT')" >&2
                exit 1
            fi
            TASK_INPUT="$1"
            shift
            ;;
    esac
done

if [[ -z "$TASK_INPUT" ]]; then
    echo "Error: task ID or task ARN is required." >&2
    echo "" >&2
    echo "Usage: scripts/ecs-task-logs.sh <task-id> [--follow] [--lines N]" >&2
    echo "Run 'scripts/ecs-task-logs.sh --help' for full usage." >&2
    exit 1
fi

# ─── Extract task ID from ARN if needed ────────────────────────────────────

if [[ "$TASK_INPUT" == arn:* ]]; then
    # Full ARN: arn:aws:ecs:<region>:<account>:task/<cluster>/<task-id>
    TASK_ID="${TASK_INPUT##*/}"
else
    TASK_ID="$TASK_INPUT"
fi

if [[ -z "$TASK_ID" ]]; then
    echo "Error: could not extract task ID from '$TASK_INPUT'" >&2
    exit 1
fi

# ─── Resolve script directory and delegate to ecs-logs.sh ──────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LOG_GROUP="/ecs/judgemind-ingestion-worker-${ENVIRONMENT}"

exec "$SCRIPT_DIR/ecs-logs.sh" "$LOG_GROUP" --task "$TASK_ID" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
