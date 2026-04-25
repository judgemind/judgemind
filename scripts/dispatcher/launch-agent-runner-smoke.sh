#!/usr/bin/env bash
# venv: none
# permanent: true
#
# launch-agent-runner-smoke.sh — Operator script to manually fire an
# agent-runner ECS task for a given agent-id.
#
# Background
# ----------
# The dispatcher daemon normally calls ecs:RunTask on behalf of each
# agent via _launch_agent_ecs_task (#3091 Stage 2). This script exposes
# the same launch path as a one-liner for operators who need to:
#   - Smoke-test the agent-runner task def after a Terraform deploy
#     (related: #3086, #3090, #3091).
#   - Re-launch a specific agent manually during an incident (#3131/#3133
#     debugging context where the daemon has already claimed the issue
#     but the ECS task exited before the phase pipeline ran).
#   - Verify network / IAM wiring without modifying the daemon.
#
# Usage
# -----
#   scripts/dispatcher/launch-agent-runner-smoke.sh <agent-id> [issue-number]
#   scripts/dispatcher/launch-agent-runner-smoke.sh --watch <agent-id> [issue-number]
#
# Options
#   --watch   Tail CloudWatch logs via scripts/ecs-logs.sh until the
#             task stops (polls ecs:DescribeTasks between log batches).
#   --help    Print this help message.
#
# Environment variables (all optional; dev defaults shown)
#   ECS_CLUSTER                        (default: judgemind-dev)
#   AGENT_RUNNER_TASK_DEFINITION_FAMILY (default: judgemind-dispatcher-agent-runner-dev)
#   AGENT_RUNNER_SUBNET_IDS            (default: resolved from judgemind-dispatcher-dev service)
#   AGENT_RUNNER_SECURITY_GROUP_ID     (default: resolved from judgemind-dispatcher-dev service)
#   REGION                             (default: us-west-2)
#
# Examples
#   scripts/dispatcher/launch-agent-runner-smoke.sh 550e8400-e29b-41d4-a716-446655440000
#   scripts/dispatcher/launch-agent-runner-smoke.sh 550e8400-e29b-41d4-a716-446655440000 3138
#   scripts/dispatcher/launch-agent-runner-smoke.sh --watch 550e8400-e29b-41d4-a716-446655440000 3138

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── Defaults ─────────────────────────────────────────────────────────────────

CLUSTER="${ECS_CLUSTER:-judgemind-dev}"
TASK_DEF_FAMILY="${AGENT_RUNNER_TASK_DEFINITION_FAMILY:-judgemind-dispatcher-agent-runner-dev}"
SUBNET_IDS="${AGENT_RUNNER_SUBNET_IDS:-}"
SG_ID="${AGENT_RUNNER_SECURITY_GROUP_ID:-}"
REGION="${REGION:-us-west-2}"
WATCH=false

# ── Usage ─────────────────────────────────────────────────────────────────────

usage() {
    cat >&2 <<'USAGE'
Usage: scripts/dispatcher/launch-agent-runner-smoke.sh [--watch] <agent-id> [issue-number]

Arguments:
  <agent-id>       UUID of the agent to launch (required).
  [issue-number]   GitHub issue number to assign to the agent (optional).

Options:
  --watch          Tail CloudWatch logs until the task stops.
  --help           Show this help message.

Environment variables:
  ECS_CLUSTER                         ECS cluster name (default: judgemind-dev)
  AGENT_RUNNER_TASK_DEFINITION_FAMILY Task def family (default: judgemind-dispatcher-agent-runner-dev)
  AGENT_RUNNER_SUBNET_IDS             Comma-separated subnet IDs (resolved from dispatcher service if unset)
  AGENT_RUNNER_SECURITY_GROUP_ID      Security group ID (resolved from dispatcher service if unset)
  REGION                              AWS region (default: us-west-2)

Examples:
  scripts/dispatcher/launch-agent-runner-smoke.sh 550e8400-e29b-41d4-a716-446655440000
  scripts/dispatcher/launch-agent-runner-smoke.sh 550e8400-e29b-41d4-a716-446655440000 3138
  scripts/dispatcher/launch-agent-runner-smoke.sh --watch 550e8400-e29b-41d4-a716-446655440000 3138
USAGE
}

# ── Argument parsing ──────────────────────────────────────────────────────────

AGENT_ID=""
ISSUE_NUMBER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --watch|-w)
            WATCH=true
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        -*)
            echo "Error: unknown option '$1'" >&2
            usage
            exit 1
            ;;
        *)
            if [[ -z "$AGENT_ID" ]]; then
                AGENT_ID="$1"
            elif [[ -z "$ISSUE_NUMBER" ]]; then
                ISSUE_NUMBER="$1"
            else
                echo "Error: unexpected argument '$1'" >&2
                usage
                exit 1
            fi
            shift
            ;;
    esac
done

if [[ -z "$AGENT_ID" ]]; then
    echo "Error: <agent-id> is required." >&2
    usage
    exit 1
fi

# Validate AGENT_ID is a strict UUID (prevents SQL injection via string interpolation)
if [[ ! "$AGENT_ID" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    echo "Error: AGENT_ID '$AGENT_ID' is not a valid UUID" >&2
    exit 1
fi

# Validate ISSUE_NUMBER is a non-negative integer when set
if [[ -n "$ISSUE_NUMBER" ]] && [[ ! "$ISSUE_NUMBER" =~ ^[0-9]+$ ]]; then
    echo "Error: ISSUE_NUMBER '$ISSUE_NUMBER' is not a non-negative integer" >&2
    exit 1
fi
# WORKTREE_PATH is derived as /tmp/agent-runner-smoke/${AGENT_ID} — safe by construction once AGENT_ID is validated above

# ── Resolve ECS wiring from environment or dispatcher service ─────────────────
#
# When AGENT_RUNNER_SUBNET_IDS / AGENT_RUNNER_SECURITY_GROUP_ID are unset,
# resolve them once from the running dispatcher daemon service — the same
# resolution pattern used in scripts/ecs-run-task.sh (lines 708-723).
# This means an operator can export just ECS_CLUSTER and REGION and get
# everything else for free.

if [[ -z "$SUBNET_IDS" || -z "$SG_ID" ]]; then
    echo "Resolving network wiring from judgemind-dispatcher-dev service..." >&2
    NETWORK_CONFIG=$(aws ecs describe-services \
        --cluster "$CLUSTER" \
        --services "judgemind-dispatcher-dev" \
        --region "$REGION" \
        --output json \
        --no-cli-pager \
        --query 'services[0].networkConfiguration.awsvpcConfiguration')

    if [[ -z "$NETWORK_CONFIG" || "$NETWORK_CONFIG" == "null" ]]; then
        echo "Error: could not resolve networking from judgemind-dispatcher-dev service." >&2
        echo "Set AGENT_RUNNER_SUBNET_IDS and AGENT_RUNNER_SECURITY_GROUP_ID explicitly." >&2
        exit 1
    fi

    if [[ -z "$SUBNET_IDS" ]]; then
        SUBNET_IDS=$(python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(','.join(d['subnets']))" <<< "$NETWORK_CONFIG")
    fi
    if [[ -z "$SG_ID" ]]; then
        SG_ID=$(python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['securityGroups'][0])" <<< "$NETWORK_CONFIG")
    fi

    echo "Resolved subnets:         $SUBNET_IDS" >&2
    echo "Resolved security group:  $SG_ID" >&2
fi

# ── Seed dispatcher.agents row (SELECT then INSERT if absent) ─────────────────
#
# The agent row must exist before the ECS task starts — the entrypoint reads
# its own agent_id from the row on startup. If a row already exists (re-run
# with the same agent-id) we skip the INSERT to keep the operation idempotent.

echo "Checking dispatcher.agents for agent_id=$AGENT_ID..." >&2

EXISTING=$("$REPO_ROOT/scripts/dev-db-query.sh" "SELECT 1 FROM dispatcher.agents WHERE agent_id = '$AGENT_ID' LIMIT 1")

if [[ -z "$EXISTING" || "$EXISTING" == "None" ]]; then
    echo "No row found — seeding dispatcher.agents..." >&2
    WORKTREE_PATH="/tmp/agent-runner-smoke/${AGENT_ID}"
    ISSUE_COL="${ISSUE_NUMBER:-0}"

    "$REPO_ROOT/scripts/dev-db-query.sh" --rw \
        "INSERT INTO dispatcher.agents (agent_id, issue_number, worktree_path, kind, phase, status, execution_mode) VALUES ('${AGENT_ID}', ${ISSUE_COL}, '${WORKTREE_PATH}', 'task', 'claiming', 'running', 'ecs') ON CONFLICT (agent_id) DO NOTHING"

    echo "Row seeded." >&2
else
    echo "Row already exists — skipping INSERT." >&2
fi

# ── Build container overrides ─────────────────────────────────────────────────

ISSUE_VALUE="${ISSUE_NUMBER:-}"

# Build JSON overrides inline — avoids heredocs and subshell quoting
# nightmares while staying readable. Single quotes around values are safe
# because agent UUIDs and issue numbers contain no special characters.
OVERRIDES='{"containerOverrides":[{"name":"agent-runner","environment":[{"name":"AGENT_ID","value":"'"$AGENT_ID"'"},{"name":"ISSUE_NUMBER","value":"'"$ISSUE_VALUE"'"}]}]}'

# ── Run the ECS task ──────────────────────────────────────────────────────────

echo "" >&2
echo "Launching agent-runner task..." >&2
echo "  Cluster:    $CLUSTER" >&2
echo "  Task def:   $TASK_DEF_FAMILY" >&2
echo "  Agent ID:   $AGENT_ID" >&2
echo "  Issue:      ${ISSUE_NUMBER:-(none)}" >&2
echo "" >&2

RUN_OUTPUT=$(aws ecs run-task \
    --cluster "$CLUSTER" \
    --task-definition "$TASK_DEF_FAMILY" \
    --launch-type FARGATE \
    --enable-execute-command \
    --region "$REGION" \
    --output json \
    --no-cli-pager \
    --network-configuration "awsvpcConfiguration={subnets=[${SUBNET_IDS}],securityGroups=[${SG_ID}],assignPublicIp=DISABLED}" \
    --overrides "$OVERRIDES")

TASK_ARN=$(python3 -c "import sys,json; tasks=json.loads(sys.stdin.read()).get('tasks',[]); print(tasks[0]['taskArn'] if tasks else '')" <<< "$RUN_OUTPUT")

if [[ -z "$TASK_ARN" ]]; then
    echo "Error: ecs:RunTask returned no task ARN." >&2
    FAILURES=$(python3 -c "import sys,json; fs=json.loads(sys.stdin.read()).get('failures',[]); [print(x.get('reason','unknown')) for x in fs]" <<< "$RUN_OUTPUT")
    if [[ -n "$FAILURES" ]]; then
        echo "Failures:" >&2
        echo "$FAILURES" >&2
    fi
    exit 1
fi

TASK_ID=$(echo "$TASK_ARN" | rev | cut -d'/' -f1 | rev)

# Print task ARN to stdout (so callers can capture it).
echo "$TASK_ARN"

# Print log location and watch hint to stderr.
LOG_GROUP="/ecs/judgemind-dispatcher-agent-runner-dev"
LOG_STREAM="agent-runner/agent-runner/${TASK_ID}"
echo "" >&2
echo "Task launched successfully." >&2
echo "  Task ARN:   $TASK_ARN" >&2
echo "  Task ID:    $TASK_ID" >&2
echo "  Log group:  $LOG_GROUP" >&2
echo "  Log stream: $LOG_STREAM" >&2
echo "  Tail logs:  scripts/ecs-logs.sh $LOG_GROUP --task $TASK_ID --follow" >&2
echo "" >&2

# ── Watch mode: poll task status + tail logs ──────────────────────────────────

if [[ "$WATCH" == true ]]; then
    echo "Watching task (Ctrl+C to stop)..." >&2
    "$REPO_ROOT/scripts/ecs-logs.sh" "$LOG_GROUP" --task "$TASK_ID" --follow &
    LOGS_PID=$!

    while true; do
        sleep 10
        STATUS=$(aws ecs describe-tasks \
            --cluster "$CLUSTER" \
            --tasks "$TASK_ARN" \
            --region "$REGION" \
            --no-cli-pager \
            --output json 2>/dev/null \
            | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['tasks'][0]['lastStatus'] if d.get('tasks') else 'UNKNOWN')" \
            || echo "UNKNOWN")

        if [[ "$STATUS" == "STOPPED" ]]; then
            echo "" >&2
            echo "Task stopped." >&2
            kill "$LOGS_PID" 2>/dev/null || true
            wait "$LOGS_PID" 2>/dev/null || true
            break
        fi
        echo "Task status: $STATUS" >&2
    done
fi
