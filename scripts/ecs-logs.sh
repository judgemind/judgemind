#!/usr/bin/env bash
# ecs-logs.sh — Tail CloudWatch logs for an ECS service or task.
#
# Finds the most recent log stream automatically, prints the last N lines,
# and optionally polls for new events (like tail -f).
#
# Usage:
#   scripts/ecs-logs.sh <log-group> [--task <task-id>] [--follow] [--lines N]
#
# Examples:
#   scripts/ecs-logs.sh /ecs/judgemind-scraper-dev
#   scripts/ecs-logs.sh /ecs/judgemind-scraper-dev --follow
#   scripts/ecs-logs.sh /ecs/judgemind-scraper-dev --task abc123 --lines 50
#   scripts/ecs-logs.sh /ecs/judgemind-api-dev --follow --lines 100
#
# Known log groups:
#   /ecs/judgemind-scraper-dev
#   /ecs/judgemind-ingestion-worker-dev
#   /ecs/judgemind-api-dev

set -euo pipefail

# ─── Defaults ──────────────────────────────────────────────────────────────

REGION="${AWS_DEFAULT_REGION:-us-west-2}"
LINES=30
FOLLOW=false
TASK_FILTER=""
POLL_INTERVAL=5

# ─── Usage ─────────────────────────────────────────────────────────────────

usage() {
    cat <<'USAGE'
Usage: scripts/ecs-logs.sh <log-group> [OPTIONS]

Arguments:
  <log-group>       CloudWatch log group name (e.g. /ecs/judgemind-scraper-dev)

Options:
  --task <id>       Filter log streams by ECS task ID (partial match)
  --follow          Poll for new events every 5s (like tail -f)
  --lines N         Number of lines to show (default: 30)
  --help            Show this help message

Examples:
  scripts/ecs-logs.sh /ecs/judgemind-scraper-dev
  scripts/ecs-logs.sh /ecs/judgemind-scraper-dev --follow
  scripts/ecs-logs.sh /ecs/judgemind-api-dev --task abc123 --lines 50
USAGE
    exit 0
}

# ─── Parse arguments ──────────────────────────────────────────────────────

if [[ $# -eq 0 ]]; then
    echo "Error: log group name is required." >&2
    echo "Run 'scripts/ecs-logs.sh --help' for usage." >&2
    exit 1
fi

LOG_GROUP=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            usage
            ;;
        --task)
            if [[ $# -lt 2 ]]; then
                echo "Error: --task requires a task ID argument" >&2
                exit 1
            fi
            TASK_FILTER="$2"
            shift 2
            ;;
        --follow|-f)
            FOLLOW=true
            shift
            ;;
        --lines|-n)
            if [[ $# -lt 2 ]]; then
                echo "Error: --lines requires a number argument" >&2
                exit 1
            fi
            LINES="$2"
            shift 2
            ;;
        -*)
            echo "Error: unknown option '$1'" >&2
            exit 1
            ;;
        *)
            if [[ -n "$LOG_GROUP" ]]; then
                echo "Error: unexpected argument '$1' (log group already set to '$LOG_GROUP')" >&2
                exit 1
            fi
            LOG_GROUP="$1"
            shift
            ;;
    esac
done

if [[ -z "$LOG_GROUP" ]]; then
    echo "Error: log group name is required." >&2
    exit 1
fi

# ─── Find the most recent log stream ──────────────────────────────────────

find_stream() {
    local query_args=(
        logs describe-log-streams
        --log-group-name "$LOG_GROUP"
        --order-by LastEventTime
        --descending
        --region "$REGION"
        --output text
        --query "logStreams[0].logStreamName"
    )

    if [[ -n "$TASK_FILTER" ]]; then
        # Filter streams whose name contains the task ID.
        # ECS stream names follow patterns like:
        #   ecs/<container>/<task-id>
        #   <container>/<container>/<task-id>
        query_args+=(--log-stream-name-prefix "")

        # We can't filter by substring via the API, so we fetch the most
        # recent streams and grep for the task ID locally.
        local streams
        streams=$(aws logs describe-log-streams \
            --log-group-name "$LOG_GROUP" \
            --order-by LastEventTime \
            --descending \
            --region "$REGION" \
            --max-items 50 \
            --output text \
            --query "logStreams[*].logStreamName" 2>/dev/null) || {
            echo "Error: failed to list log streams for '$LOG_GROUP'" >&2
            echo "Check that the log group exists and you have AWS credentials configured." >&2
            exit 1
        }

        local match
        match=$(echo "$streams" | tr '\t' '\n' | grep -F "$TASK_FILTER" | head -n 1) || true

        if [[ -z "$match" ]]; then
            echo "Error: no log stream found matching task '$TASK_FILTER' in '$LOG_GROUP'" >&2
            echo "Recent streams:" >&2
            echo "$streams" | tr '\t' '\n' | head -n 5 >&2
            exit 1
        fi

        echo "$match"
    else
        local stream
        stream=$(aws "${query_args[@]}" 2>/dev/null) || {
            echo "Error: failed to list log streams for '$LOG_GROUP'" >&2
            echo "Check that the log group exists and you have AWS credentials configured." >&2
            exit 1
        }

        if [[ -z "$stream" || "$stream" == "None" ]]; then
            echo "Error: no log streams found in '$LOG_GROUP'" >&2
            exit 1
        fi

        echo "$stream"
    fi
}

STREAM=$(find_stream)
echo "Log group:  $LOG_GROUP" >&2
echo "Stream:     $STREAM" >&2
echo "---" >&2

# ─── Fetch and display log events ─────────────────────────────────────────

# Track the forward token for follow mode
NEXT_TOKEN=""

fetch_events() {
    local args=(
        logs get-log-events
        --log-group-name "$LOG_GROUP"
        --log-stream-name "$STREAM"
        --region "$REGION"
        --output json
        --start-from-head false
    )

    if [[ -n "$NEXT_TOKEN" ]]; then
        args+=(--next-token "$NEXT_TOKEN")
    else
        args+=(--limit "$LINES")
    fi

    local result
    result=$(aws "${args[@]}" 2>/dev/null) || {
        echo "Error: failed to fetch log events" >&2
        return 1
    }

    # Extract and print messages
    local messages
    messages=$(echo "$result" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for event in data.get('events', []):
    print(event.get('message', '').rstrip())
" 2>/dev/null) || true

    if [[ -n "$messages" ]]; then
        echo "$messages"
    fi

    # Update the forward token for follow mode
    NEXT_TOKEN=$(echo "$result" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('nextForwardToken', ''))
" 2>/dev/null) || true
}

# Initial fetch
fetch_events

# Follow mode: poll for new events
if [[ "$FOLLOW" == true ]]; then
    echo "--- following (Ctrl+C to stop) ---" >&2
    while true; do
        sleep "$POLL_INTERVAL"
        fetch_events
    done
fi
