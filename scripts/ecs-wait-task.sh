#!/usr/bin/env bash
# ecs-wait-task.sh — Wait until a previously launched ECS oneshot task reaches STOPPED.
#
# # venv: none
# # permanent: true
#
# ── Why this exists ─────────────────────────────────────────────────────────
#
# The native `aws ecs wait tasks-stopped` polls 100 times at 6s intervals,
# capping at 10 minutes. That is too short for typical oneshot reingests,
# rebuilds, or backfills, which routinely run 30-180 minutes (or longer).
#
# Long-running agents that launch a task with `scripts/ecs-run-task.sh
# --detach` previously had to write throwaway shell scripts with their own
# `until ...; do sleep 60; done` loop because the agent harness blocks the
# `Monitor` tool and the inline `sleep N && next-cmd` shell pattern. This
# script provides a single shared, well-tested wait helper.
#
# ── Usage ────────────────────────────────────────────────────────────────────
#
# Usage: scripts/ecs-wait-task.sh [<task-arn>] [options]
#
# When invoked from a Claude Code Bash tool call, set `timeout: 1200000`
# (20 minutes) — typical reingests/rebuilds exceed the 2-minute default,
# and you may need to extend further for very long jobs.
#
# Arguments:
#   <task-arn>            ECS task ARN to wait on. If omitted, the script
#                         reads the ARN from <repo>/tmp/last-ecs-task.arn,
#                         which `scripts/ecs-run-task.sh --detach` writes
#                         automatically.
#
# Options:
#   --timeout <minutes>   Max minutes to wait before giving up. Default: 0
#                         (unlimited — the harness's `timeout: 1200000`
#                         Bash-tool flag bounds it from outside).
#   --poll-interval <s>   Polling interval in seconds. Default: 60.
#   --quiet               Suppress per-poll liveness notes (still prints
#                         the final status + exit code on STOP).
#   --region <name>       AWS region. Default: us-west-2.
#   --help                Show this help and exit 0.
#
# Exit codes:
#   The container's exitCode when the task reaches STOPPED. If the
#   container died without an exitCode (OOM kill, agent failure), exits 1
#   with `Container stopped without exit code: <reason>` on stderr.
#   Exits 2 on timeout (task still RUNNING when --timeout expired).
#   Exits 3 on usage error (bad arg, ARN parse failure, missing AWS CLI).
#
# Examples:
#   # Launch a long oneshot, then wait on the auto-saved ARN.
#   scripts/ecs-run-task.sh --detach scripts/reingest_from_s3.py -- --all
#   scripts/ecs-wait-task.sh
#
#   # Wait on a specific ARN with a 90-minute hard cap.
#   scripts/ecs-wait-task.sh --timeout 90 \
#       arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/abc123
#
#   # Quieter polling (every 5 minutes, no per-poll line).
#   scripts/ecs-wait-task.sh --poll-interval 300 --quiet

set -euo pipefail
# AWS CLI v1/v2 portability: suppress pager without --no-cli-pager (v2-only flag). See #3461.
export AWS_PAGER=""

# ─── Defaults ────────────────────────────────────────────────────────────────

TASK_ARN=""
TIMEOUT_MINUTES=0           # 0 = unlimited
POLL_INTERVAL_SECS=60
QUIET=false
REGION="us-west-2"

# ─── Parse options ───────────────────────────────────────────────────────────

usage() {
    sed -n '2,/^set -euo pipefail$/p' "$0" | sed 's/^# \{0,1\}//' | sed '$d'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --timeout)
            TIMEOUT_MINUTES="${2:-}"
            shift 2
            ;;
        --poll-interval)
            POLL_INTERVAL_SECS="${2:-}"
            shift 2
            ;;
        --quiet)
            QUIET=true
            shift
            ;;
        --region)
            REGION="${2:-}"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        --*)
            echo "Error: unknown option: $1" >&2
            echo "Run: scripts/ecs-wait-task.sh --help" >&2
            exit 3
            ;;
        *)
            if [[ -n "$TASK_ARN" ]]; then
                echo "Error: unexpected positional argument: $1" >&2
                echo "Only one task ARN may be supplied." >&2
                exit 3
            fi
            TASK_ARN="$1"
            shift
            ;;
    esac
done

# ─── Validate inputs ────────────────────────────────────────────────────────

# Validate numeric flags before any AWS calls.
if ! [[ "$TIMEOUT_MINUTES" =~ ^[0-9]+$ ]]; then
    echo "Error: --timeout must be a non-negative integer (minutes), got: '${TIMEOUT_MINUTES}'" >&2
    exit 3
fi
if ! [[ "$POLL_INTERVAL_SECS" =~ ^[0-9]+$ ]] || [[ "$POLL_INTERVAL_SECS" -lt 1 ]]; then
    echo "Error: --poll-interval must be a positive integer (seconds), got: '${POLL_INTERVAL_SECS}'" >&2
    exit 3
fi

# ─── Resolve repo root + ARN sentinel file ──────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ARN_SENTINEL_FILE="${REPO_ROOT}/tmp/last-ecs-task.arn"

# If no ARN was passed, fall back to the sentinel written by ecs-run-task.sh --detach.
if [[ -z "$TASK_ARN" ]]; then
    if [[ -f "$ARN_SENTINEL_FILE" ]]; then
        TASK_ARN=$(tr -d '[:space:]' < "$ARN_SENTINEL_FILE")
        if [[ -z "$TASK_ARN" ]]; then
            echo "Error: ${ARN_SENTINEL_FILE} is empty." >&2
            echo "Pass an ARN explicitly, or re-run scripts/ecs-run-task.sh --detach to populate it." >&2
            exit 3
        fi
        echo "Using ARN from ${ARN_SENTINEL_FILE#"${REPO_ROOT}"/}: ${TASK_ARN}" >&2
    else
        echo "Error: no task ARN supplied and ${ARN_SENTINEL_FILE#"${REPO_ROOT}"/} is missing." >&2
        echo "Usage: scripts/ecs-wait-task.sh <task-arn> [options]" >&2
        echo "Or launch via: scripts/ecs-run-task.sh --detach <script>" >&2
        exit 3
    fi
fi

# Parse cluster name out of the ARN.
# ARN format: arn:aws:ecs:<region>:<account>:task/<cluster>/<task-id>
CLUSTER_FROM_ARN=$(echo "$TASK_ARN" | cut -d'/' -f2)
TASK_ID_FROM_ARN=$(echo "$TASK_ARN" | rev | cut -d'/' -f1 | rev)
if [[ -z "$CLUSTER_FROM_ARN" || -z "$TASK_ID_FROM_ARN" || "$TASK_ARN" != arn:aws:ecs:* ]]; then
    echo "Error: could not parse task ARN: ${TASK_ARN}" >&2
    echo "Expected format: arn:aws:ecs:<region>:<account>:task/<cluster>/<task-id>" >&2
    exit 3
fi

if ! command -v aws >/dev/null 2>&1; then
    echo "Error: aws CLI not found in PATH." >&2
    exit 3
fi

# ─── Stage Python helper for JSON parsing ──────────────────────────────────
#
# We delegate JSON extraction to a one-shot Python script written to a temp
# directory. This avoids the heredoc-stdin trap (`echo X | python3 << 'EOF'`
# silently uses the heredoc as stdin instead of the piped echo) and keeps
# the parsing logic in one place. The helper takes <json-file> <field> as
# argv and prints exactly one line on stdout.
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

PARSER="${TMP_DIR}/parse_describe_tasks.py"
cat > "$PARSER" << 'PY_PARSER'
"""Parse one field out of an `aws ecs describe-tasks` JSON payload.

Usage:
    parse_describe_tasks.py <json-file> <field>

Fields:
    tasks_len       - number of tasks in the response (0 means ARN missing)
    last_status     - tasks[0].lastStatus, or 'UNKNOWN'
    stopped_reason  - tasks[0].stoppedReason (truncated to 120 chars), or ''
    exit_code       - tasks[0].containers[0].exitCode, or '' if absent
    container_reason- tasks[0].containers[0].reason, or ''
    summary         - one machine-readable line:
                      status=<S> exit_code=<N|''> stopped_reason=<R>
"""
import json
import sys

if len(sys.argv) != 3:
    print(f"usage: {sys.argv[0]} <json-file> <field>", file=sys.stderr)
    sys.exit(2)

json_path, field = sys.argv[1], sys.argv[2]

with open(json_path, encoding="utf-8") as fh:
    data = json.load(fh)

tasks = data.get("tasks", []) or []

if field == "tasks_len":
    print(len(tasks))
    sys.exit(0)

if not tasks:
    # No task in payload — fields beyond tasks_len are undefined.
    if field in {"last_status"}:
        print("MISSING")
    else:
        print("")
    sys.exit(0)

task = tasks[0]
status = task.get("lastStatus", "UNKNOWN")
stopped_reason = (task.get("stoppedReason") or "")[:120]
containers = task.get("containers", []) or []
exit_code = ""
container_reason = ""
if containers:
    ec = containers[0].get("exitCode")
    if ec is not None:
        exit_code = str(int(ec))
    container_reason = containers[0].get("reason") or ""

if field == "last_status":
    print(status)
elif field == "stopped_reason":
    print(stopped_reason)
elif field == "exit_code":
    print(exit_code)
elif field == "container_reason":
    print(container_reason)
elif field == "summary":
    print(f"status={status} exit_code={exit_code} stopped_reason={stopped_reason}")
else:
    print(f"unknown field: {field}", file=sys.stderr)
    sys.exit(2)
PY_PARSER

# Path used to stash each describe-tasks payload before parsing.
DESCRIBE_PATH="${TMP_DIR}/describe.json"

# parse_field <field> — read the cached describe-tasks JSON and emit one field.
parse_field() {
    python3 "$PARSER" "$DESCRIBE_PATH" "$1"
}

# ─── Poll loop ───────────────────────────────────────────────────────────────

# Convert minutes-to-seconds budget. 0 = unlimited (we never compare against
# DEADLINE_SECS in that case — see the loop guard below).
TIMEOUT_SECS=$(( TIMEOUT_MINUTES * 60 ))
ELAPSED_SECS=0

if [[ "$QUIET" == "false" ]]; then
    if [[ "$TIMEOUT_MINUTES" -gt 0 ]]; then
        echo "Waiting for task ${TASK_ID_FROM_ARN} on cluster ${CLUSTER_FROM_ARN} (poll ${POLL_INTERVAL_SECS}s, timeout ${TIMEOUT_MINUTES}m)..." >&2
    else
        echo "Waiting for task ${TASK_ID_FROM_ARN} on cluster ${CLUSTER_FROM_ARN} (poll ${POLL_INTERVAL_SECS}s, no timeout)..." >&2
    fi
fi

poll_once() {
    if ! aws ecs describe-tasks \
        --cluster "$CLUSTER_FROM_ARN" \
        --tasks "$TASK_ARN" \
        --region "$REGION" \
        --output json > "$DESCRIBE_PATH" 2>/dev/null; then
        echo "Error: aws ecs describe-tasks failed. Check the ARN and your AWS credentials." >&2
        return 1
    fi

    local tasks_len
    tasks_len=$(parse_field tasks_len)
    if [[ "$tasks_len" == "0" ]]; then
        echo "Error: ECS returned no task for ARN: ${TASK_ARN}" >&2
        echo "The ARN may be wrong, the task may have been deleted (>1h after stop), or the cluster name is wrong." >&2
        return 1
    fi
    return 0
}

# Print a single liveness note: timestamp + status (and any stop reason fragment).
emit_progress() {
    if [[ "$QUIET" == "true" ]]; then
        return
    fi

    local status reason iso
    status=$(parse_field last_status)
    reason=$(parse_field stopped_reason)
    iso=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    if [[ -n "$reason" ]]; then
        echo "[${iso}] status=${status} elapsed=${ELAPSED_SECS}s reason=${reason}" >&2
    else
        echo "[${iso}] status=${status} elapsed=${ELAPSED_SECS}s" >&2
    fi
}

# Main wait loop. We always poll at least once before the first sleep so a
# task that is already STOPPED returns immediately.
while true; do
    poll_once || exit 3

    emit_progress

    LAST_STATUS=$(parse_field last_status)
    if [[ "$LAST_STATUS" == "STOPPED" ]]; then
        break
    fi

    # Timeout check (only when --timeout was set non-zero).
    if [[ "$TIMEOUT_MINUTES" -gt 0 && "$ELAPSED_SECS" -ge "$TIMEOUT_SECS" ]]; then
        echo "Error: timed out after ${TIMEOUT_MINUTES}m. Task is still RUNNING." >&2
        echo "Task ARN: ${TASK_ARN}" >&2
        echo "Re-run with a larger --timeout, or scripts/ecs-run-task.sh --logs ${TASK_ARN}" >&2
        exit 2
    fi

    sleep "$POLL_INTERVAL_SECS"
    ELAPSED_SECS=$(( ELAPSED_SECS + POLL_INTERVAL_SECS ))
done

# ─── Extract exit code + stop reason ───────────────────────────────────────

FINAL_STATUS=$(parse_field last_status)
FINAL_STOPPED_REASON=$(parse_field stopped_reason)
FINAL_EXIT_CODE=$(parse_field exit_code)
FINAL_CONTAINER_REASON=$(parse_field container_reason)

# Echo a structured summary line on stdout for callers to capture.
echo "status=${FINAL_STATUS} exit_code=${FINAL_EXIT_CODE} stopped_reason=${FINAL_STOPPED_REASON}"

# Human-readable trailing summary on stderr (matches the --logs path style
# used by ecs-run-task.sh).
echo "Final status: ${FINAL_STATUS}" >&2
if [[ -n "$FINAL_STOPPED_REASON" ]]; then
    echo "Stop reason: ${FINAL_STOPPED_REASON}" >&2
fi
if [[ -n "$FINAL_EXIT_CODE" ]]; then
    echo "Exit code: ${FINAL_EXIT_CODE}" >&2
    exit "$FINAL_EXIT_CODE"
fi

# No exit code — surface the container reason and exit 1 so callers see a
# non-zero exit even though the JSON didn't carry an exitCode (OOM kill,
# agent crash, image pull failure, etc.).
if [[ -n "$FINAL_CONTAINER_REASON" ]]; then
    echo "Container stopped without exit code: ${FINAL_CONTAINER_REASON}" >&2
elif [[ -n "$FINAL_STOPPED_REASON" ]]; then
    echo "Container stopped without exit code: ${FINAL_STOPPED_REASON}" >&2
else
    echo "Container stopped without exit code (no reason in describe-tasks payload)." >&2
fi
exit 1
