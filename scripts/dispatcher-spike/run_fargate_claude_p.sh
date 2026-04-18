#!/usr/bin/env bash
# run_fargate_claude_p.sh — Dispatcher v2 Spike 0.1 wrapper.
#
# Launches a single `claude -p` invocation inside an ECS Fargate task in the
# dev environment using the `judgemind-dispatcher-spike-dev` task definition,
# waits for the task to stop, fetches the exit code + CloudWatch log tail,
# and writes a `dispatcher_spike.runs` row with the result.
#
# This is intentionally a throwaway tool — it exists to prove that
# `claude -p` runs end-to-end on Fargate (issue #2683). Once spike 0.1
# concludes, this script + the Dockerfile + the Terraform module + the
# dispatcher_spike schema should all be deleted together.
#
# Usage:
#   scripts/dispatcher-spike/run_fargate_claude_p.sh <scenario>
#
#   scenario ∈ {success, turn_limit, auth_fail, mcp_probe}
#
# Examples:
#   scripts/dispatcher-spike/run_fargate_claude_p.sh success
#   scripts/dispatcher-spike/run_fargate_claude_p.sh turn_limit
#   scripts/dispatcher-spike/run_fargate_claude_p.sh auth_fail
#   scripts/dispatcher-spike/run_fargate_claude_p.sh mcp_probe
#
# Env overrides:
#   SPIKE_ENV        — target environment (default: dev)
#   SPIKE_REGION     — AWS region (default: us-west-2)
#   SPIKE_TIMEOUT    — client-side wait for task completion, seconds (default: 1200)
#   SPIKE_NOTES      — free-form note written into dispatcher_spike.runs.notes
#   SPIKE_SKIP_DB    — if set to 1, don't attempt to insert a row
#   SPIKE_AGENT_ID   — (spike 0.2) if set, passed to the container as
#                      $SPIKE_AGENT_ID so dispatcher_spike.failures rows
#                      inserted by the PreToolUse hook carry this identifier.

set -euo pipefail

SCENARIO="${1:-success}"
case "${SCENARIO}" in
    success|turn_limit|auth_fail|mcp_probe|hook_latency|hook_failmode) ;;
    *)
        echo "Error: unknown scenario '${SCENARIO}'." >&2
        echo "Valid scenarios: success, turn_limit, auth_fail, mcp_probe, hook_latency, hook_failmode" >&2
        exit 2
        ;;
esac

SPIKE_ENV="${SPIKE_ENV:-dev}"
SPIKE_REGION="${SPIKE_REGION:-us-west-2}"
SPIKE_TIMEOUT="${SPIKE_TIMEOUT:-1200}"
SPIKE_NOTES_DEFAULT="spike-0.1 scenario=${SCENARIO} invoked=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
SPIKE_NOTES="${SPIKE_NOTES:-${SPIKE_NOTES_DEFAULT}}"

TASK_FAMILY="judgemind-dispatcher-spike-${SPIKE_ENV}"
CLUSTER="judgemind-${SPIKE_ENV}"
LOG_GROUP="/ecs/judgemind-dispatcher-spike-${SPIKE_ENV}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

log()  { printf '[spike-wrapper] %s\n' "$*" >&2; }

# ─── Helper Python scripts written to a tempdir ────────────────────────────
# Using tempfiles avoids the `python3 - <<EOF` heredoc-stdin-collision
# trap where the heredoc captures stdin and the upstream pipe is lost.

TMPDIR_SPIKE="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_SPIKE}"' EXIT

cat > "${TMPDIR_SPIKE}/tail_events.py" <<'PYEOF'
"""Read CloudWatch get-log-events JSON from stdin, print last 100 lines."""
import json, sys
data = json.load(sys.stdin)
lines = [ev.get("message", "") for ev in data.get("events", [])]
print("\n".join(lines[-100:]))
PYEOF

cat > "${TMPDIR_SPIKE}/emit_summary.py" <<'PYEOF'
"""Emit a JSON summary from env vars for the spike run."""
import json, os
out = {
    "scenario":    os.environ["SCENARIO"],
    "task_arn":    os.environ["TASK_ARN"],
    "started_at":  os.environ["STARTED_AT"],
    "ended_at":    os.environ["ENDED_AT"],
    "exit_code":   int(os.environ["EXIT_CODE"]) if os.environ.get("EXIT_CODE", "").strip() else None,
    "stop_reason": os.environ.get("STOP_REASON", ""),
    "stdout_tail": os.environ.get("STDOUT_TAIL", ""),
    "notes":       os.environ.get("SPIKE_NOTES", ""),
}
print(json.dumps(out, indent=2))
PYEOF

cat > "${TMPDIR_SPIKE}/build_insert_sql.py" <<'PYEOF'
"""Build an INSERT statement for dispatcher_spike.runs from the summary JSON."""
import json, sys
payload = json.loads(open(sys.argv[1]).read())

def dq(text: str) -> str:
    tag = "spk1"
    while f"${tag}$" in text:
        tag += "x"
    return f"${tag}${text}${tag}$"

exit_code = payload["exit_code"]
exit_sql = "NULL" if exit_code is None else str(int(exit_code))
sql = (
    "INSERT INTO dispatcher_spike.runs "
    "(scenario, task_arn, started_at, ended_at, exit_code, stdout_tail, stderr_tail, notes) "
    "VALUES ("
    f"{dq(payload['scenario'])}, "
    f"{dq(payload['task_arn'] or '')}, "
    f"{dq(payload['started_at'])}::timestamptz, "
    f"{dq(payload['ended_at'])}::timestamptz, "
    f"{exit_sql}, "
    f"{dq(payload['stdout_tail'] or '')}, "
    f"{dq(payload['stdout_tail'] or '')}, "
    f"{dq(payload['notes'])}"
    ");"
)
open(sys.argv[2], "w").write(sql)
PYEOF

log "scenario=${SCENARIO} env=${SPIKE_ENV} region=${SPIKE_REGION}"

# ─── Resolve networking from the ingestion worker service ──────────────────
log "resolving networking from ${CLUSTER}..."

SERVICE_NAME="judgemind-ingestion-worker-${SPIKE_ENV}"

SUBNETS=$(aws ecs describe-services \
    --cluster "${CLUSTER}" \
    --services "${SERVICE_NAME}" \
    --region "${SPIKE_REGION}" \
    --output text \
    --no-cli-pager \
    --query 'services[0].networkConfiguration.awsvpcConfiguration.subnets' \
    | tr '\t' ',')

SECURITY_GROUPS=$(aws ecs describe-services \
    --cluster "${CLUSTER}" \
    --services "${SERVICE_NAME}" \
    --region "${SPIKE_REGION}" \
    --output text \
    --no-cli-pager \
    --query 'services[0].networkConfiguration.awsvpcConfiguration.securityGroups' \
    | tr '\t' ',')

if [[ -z "${SUBNETS}" || -z "${SECURITY_GROUPS}" ]]; then
    log "ERROR: could not resolve networking from ${SERVICE_NAME}"
    exit 1
fi

log "subnets=${SUBNETS}"
log "security_groups=${SECURITY_GROUPS}"

# ─── Launch the task ────────────────────────────────────────────────────────
log "launching ${TASK_FAMILY} with command=[${SCENARIO}]..."

# Build container overrides. For spike-0.2 hook scenarios we pass
# SPIKE_AGENT_ID into the container so dispatcher_spike.failures.agent_id
# matches a caller-chosen value (e.g. harness-<uuid>) rather than the
# task UUID — this lets the harness filter its rows deterministically.
OVERRIDES_JSON="${TMPDIR_SPIKE}/overrides.json"

cat > "${TMPDIR_SPIKE}/build_overrides.py" <<'PYEOF'
"""Build containerOverrides JSON for `aws ecs run-task`.

Reads SCENARIO and optional SPIKE_AGENT_ID from the environment; emits:

    {"containerOverrides": [{
        "name": "dispatcher-spike",
        "command": ["<scenario>"],
        "environment": [{"name": "SPIKE_AGENT_ID", "value": "<id>"}]   # if set
    }]}
"""
import json, os, sys

override = {
    "name": "dispatcher-spike",
    "command": [os.environ["SCENARIO"]],
}
agent_id = os.environ.get("SPIKE_AGENT_ID", "").strip()
if agent_id:
    override["environment"] = [{"name": "SPIKE_AGENT_ID", "value": agent_id}]

json.dump({"containerOverrides": [override]}, sys.stdout)
PYEOF

SCENARIO="${SCENARIO}" \
    SPIKE_AGENT_ID="${SPIKE_AGENT_ID:-}" \
    python3 "${TMPDIR_SPIKE}/build_overrides.py" \
    > "${OVERRIDES_JSON}"

RUN_OUTPUT_FILE="${TMPDIR_SPIKE}/run-output.json"
aws ecs run-task \
    --cluster "${CLUSTER}" \
    --task-definition "${TASK_FAMILY}" \
    --launch-type FARGATE \
    --region "${SPIKE_REGION}" \
    --output json \
    --no-cli-pager \
    --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[${SECURITY_GROUPS}],assignPublicIp=DISABLED}" \
    --overrides "file://${OVERRIDES_JSON}" \
    > "${RUN_OUTPUT_FILE}"

cat > "${TMPDIR_SPIKE}/extract_task_arn.py" <<'PYEOF'
"""Extract the first taskArn from an `aws ecs run-task` JSON output file."""
import json, sys
data = json.load(open(sys.argv[1]))
tasks = data.get("tasks", [])
print(tasks[0]["taskArn"] if tasks else "")
PYEOF

TASK_ARN=$(python3 "${TMPDIR_SPIKE}/extract_task_arn.py" "${RUN_OUTPUT_FILE}")

if [[ -z "${TASK_ARN}" ]]; then
    log "ERROR: failed to launch task"
    cat "${RUN_OUTPUT_FILE}" >&2
    exit 1
fi

TASK_ID=$(printf '%s' "${TASK_ARN}" | awk -F'/' '{print $NF}')
log "task_arn=${TASK_ARN}"
log "task_id=${TASK_ID}"
log "log_group=${LOG_GROUP}"
log "log_stream=spike/dispatcher-spike/${TASK_ID}"

STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# ─── Poll for completion ────────────────────────────────────────────────────
ELAPSED=0
POLL_INTERVAL=15
LAST_STATUS=""
CURRENT_STATUS=""

while [[ ${ELAPSED} -lt ${SPIKE_TIMEOUT} ]]; do
    CURRENT_STATUS=$(aws ecs describe-tasks \
        --cluster "${CLUSTER}" \
        --tasks "${TASK_ARN}" \
        --region "${SPIKE_REGION}" \
        --output text \
        --no-cli-pager \
        --query 'tasks[0].lastStatus')

    if [[ "${CURRENT_STATUS}" != "${LAST_STATUS}" ]]; then
        log "status=${CURRENT_STATUS} elapsed=${ELAPSED}s"
        LAST_STATUS="${CURRENT_STATUS}"
    fi

    if [[ "${CURRENT_STATUS}" == "STOPPED" ]]; then
        break
    fi

    sleep "${POLL_INTERVAL}"
    ELAPSED=$(( ELAPSED + POLL_INTERVAL ))
done

if [[ "${CURRENT_STATUS}" != "STOPPED" ]]; then
    log "ERROR: task did not stop within ${SPIKE_TIMEOUT}s"
    exit 1
fi

ENDED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# ─── Extract exit code + stop reason using aws --query ──────────────────────
EXIT_CODE=$(aws ecs describe-tasks \
    --cluster "${CLUSTER}" \
    --tasks "${TASK_ARN}" \
    --region "${SPIKE_REGION}" \
    --output text \
    --no-cli-pager \
    --query 'tasks[0].containers[0].exitCode')

# AWS CLI returns the literal string "None" when exitCode is missing.
if [[ "${EXIT_CODE}" == "None" ]]; then
    EXIT_CODE=""
fi

STOP_REASON=$(aws ecs describe-tasks \
    --cluster "${CLUSTER}" \
    --tasks "${TASK_ARN}" \
    --region "${SPIKE_REGION}" \
    --output text \
    --no-cli-pager \
    --query 'tasks[0].stoppedReason')

if [[ "${STOP_REASON}" == "None" ]]; then
    STOP_REASON=""
fi

log "exit_code=${EXIT_CODE:-<none>} stop_reason=${STOP_REASON:-<none>}"

# ─── Fetch a log tail ───────────────────────────────────────────────────────
LOG_STREAM="spike/dispatcher-spike/${TASK_ID}"
STDOUT_TAIL=""
EVENTS_FILE="${TMPDIR_SPIKE}/events.json"

for _retry in 1 2 3 4; do
    sleep 5
    if aws logs get-log-events \
            --log-group-name "${LOG_GROUP}" \
            --log-stream-name "${LOG_STREAM}" \
            --region "${SPIKE_REGION}" \
            --output json \
            --no-cli-pager \
            > "${EVENTS_FILE}" 2>/dev/null; then
        TAIL=$(python3 "${TMPDIR_SPIKE}/tail_events.py" < "${EVENTS_FILE}" || true)
        if [[ -n "${TAIL}" ]]; then
            STDOUT_TAIL="${TAIL}"
            break
        fi
    fi
    log "waiting for CloudWatch log stream (retry ${_retry})..."
done

if [[ -z "${STDOUT_TAIL}" ]]; then
    log "WARNING: could not fetch log tail for ${LOG_STREAM}"
    STDOUT_TAIL="(log stream ${LOG_STREAM} not found after retries)"
fi

# ─── Emit a structured summary ──────────────────────────────────────────────
SUMMARY_FILE="${TMPDIR_SPIKE}/summary.json"
SCENARIO="${SCENARIO}" \
    TASK_ARN="${TASK_ARN}" \
    STARTED_AT="${STARTED_AT}" \
    ENDED_AT="${ENDED_AT}" \
    EXIT_CODE="${EXIT_CODE}" \
    STOP_REASON="${STOP_REASON}" \
    STDOUT_TAIL="${STDOUT_TAIL}" \
    SPIKE_NOTES="${SPIKE_NOTES}" \
    python3 "${TMPDIR_SPIKE}/emit_summary.py" \
    > "${SUMMARY_FILE}"

cat "${SUMMARY_FILE}"

log "summary:"
log "  scenario:    ${SCENARIO}"
log "  exit_code:   ${EXIT_CODE:-<none>}"
log "  task_arn:    ${TASK_ARN}"

# ─── Insert into dispatcher_spike.runs (optional) ──────────────────────────
if [[ "${SPIKE_SKIP_DB:-0}" == "1" ]]; then
    log "SPIKE_SKIP_DB=1 — skipping dispatcher_spike.runs insert"
    exit 0
fi

INSERT_SQL_FILE="${TMPDIR_SPIKE}/insert.sql"
python3 "${TMPDIR_SPIKE}/build_insert_sql.py" "${SUMMARY_FILE}" "${INSERT_SQL_FILE}"

SQL_CONTENT=$(cat "${INSERT_SQL_FILE}")

if "${REPO_ROOT}/scripts/dev-db-query.sh" --rw "${SQL_CONTENT}" > /dev/null 2>&1; then
    log "wrote dispatcher_spike.runs row"
else
    log "WARNING: could not insert into dispatcher_spike.runs (schema missing? VPC access?)"
fi
