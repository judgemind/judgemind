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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_terse_lib.sh
source "$SCRIPT_DIR/_terse_lib.sh"

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
        --verbose|-v)
            VERBOSE=1
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

# ─── Validate a log stream is accessible ─────────────────────────────────

validate_stream() {
    # Try to fetch 1 event from the stream to confirm it's accessible.
    # Returns 0 if the stream is readable, 1 if stale/inaccessible.
    local stream_name="$1"
    aws logs get-log-events \
        --log-group-name "$LOG_GROUP" \
        --log-stream-name "$stream_name" \
        --region "$REGION" \
        --limit 1 \
        --no-start-from-head \
        --output json \
        --no-cli-pager > /dev/null 2>&1
}

# ─── Find the most recent log stream ──────────────────────────────────────

find_stream() {
    if [[ -n "$TASK_FILTER" ]]; then
        # Filter streams whose name contains the task ID.
        # ECS stream names follow patterns like:
        #   ecs/<container>/<task-id>
        #   <container>/<container>/<task-id>  (oneshot tasks)
        #
        # Strategy: first try a prefix-based search using common ECS log
        # stream prefixes. This is faster and works for newly created
        # streams that may not yet appear in LastEventTime ordering.
        # Fall back to LastEventTime ordering if prefix search fails.
        local match=""

        # Try known prefixes first (handles oneshot tasks reliably)
        for prefix in "oneshot/oneshot/" "ecs/" "ingestion-worker/" "scraper/"; do
            local prefix_streams
            prefix_streams=$(aws logs describe-log-streams \
                --log-group-name "$LOG_GROUP" \
                --log-stream-name-prefix "$prefix" \
                --order-by LogStreamName \
                --max-items 100 \
                --region "$REGION" \
                --output text \
                --query "logStreams[*].logStreamName" \
                --no-cli-pager 2>/dev/null) || continue

            match=$(echo "$prefix_streams" | tr '\t' '\n' | grep -F "$TASK_FILTER" | head -n 1) || true
            if [[ -n "$match" ]]; then
                break
            fi
        done

        # Fall back to LastEventTime ordering (catches non-standard prefixes)
        if [[ -z "$match" ]]; then
            local streams
            streams=$(aws logs describe-log-streams \
                --log-group-name "$LOG_GROUP" \
                --order-by LastEventTime \
                --descending \
                --region "$REGION" \
                --max-items 50 \
                --output text \
                --query "logStreams[*].logStreamName" \
                --no-cli-pager 2>/dev/null) || {
                echo "Error: failed to list log streams for '$LOG_GROUP'" >&2
                echo "Check that the log group exists and you have AWS credentials configured." >&2
                exit 1
            }

            match=$(echo "$streams" | tr '\t' '\n' | grep -F "$TASK_FILTER" | head -n 1) || true
        fi

        if [[ -z "$match" ]]; then
            echo "Error: no log stream found matching task '$TASK_FILTER' in '$LOG_GROUP'" >&2
            echo "The log stream may not exist yet. CloudWatch can take 10-30 seconds" >&2
            echo "to create the stream after a task starts writing logs." >&2
            exit 1
        fi

        echo "$match"
    else
        # Fetch the most recent streams ordered by last event time.
        # We request several candidates so we can fall back if the top
        # stream is stale or inaccessible (expired retention, etc.).
        local raw_streams
        raw_streams=$(aws logs describe-log-streams \
            --log-group-name "$LOG_GROUP" \
            --order-by LastEventTime \
            --descending \
            --max-items 5 \
            --region "$REGION" \
            --output text \
            --query "logStreams[*].logStreamName" \
            --no-cli-pager 2>/dev/null) || {
            echo "Error: failed to list log streams for '$LOG_GROUP'" >&2
            echo "Check that the log group exists and you have AWS credentials configured." >&2
            exit 1
        }

        if [[ -z "$raw_streams" || "$raw_streams" == "None" ]]; then
            echo "Error: no log streams found in '$LOG_GROUP'" >&2
            exit 1
        fi

        # Split tab-separated stream names and try each one until we find
        # one that is accessible (not stale/expired).
        local candidate
        while IFS=$'\t' read -r candidate; do
            # Skip empty entries
            if [[ -z "$candidate" || "$candidate" == "None" ]]; then
                continue
            fi
            if validate_stream "$candidate"; then
                echo "$candidate"
                return 0
            fi
            echo "Warning: stream '$candidate' is stale/inaccessible, trying next..." >&2
        done < <(echo "$raw_streams" | tr '\t' '\n')

        echo "Error: all recent log streams in '$LOG_GROUP' are stale or inaccessible" >&2
        exit 1
    fi
}

STREAM=$(find_stream)
vlog "Log group:  $LOG_GROUP"
vlog "Stream:     $STREAM"
vlog "---"

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
        --no-start-from-head
        --no-cli-pager
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
") || {
        echo "WARNING: failed to parse log events JSON" >&2
        true
    }

    if [[ -n "$messages" ]]; then
        echo "$messages"
    fi

    # Update the forward token for follow mode
    NEXT_TOKEN=$(echo "$result" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('nextForwardToken', ''))
") || {
        echo "WARNING: failed to extract forward token from log response" >&2
        true
    }
}

# Initial fetch
fetch_events

# Follow mode: poll for new events
if [[ "$FOLLOW" == true ]]; then
    vlog "--- following (Ctrl+C to stop) ---"
    while true; do
        sleep "$POLL_INTERVAL"
        fetch_events
    done
fi
