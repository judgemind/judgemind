#!/usr/bin/env bash
# measure_hook_latency.sh — Dispatcher v2 Spike 0.2 harness.
#
# Launches N=SPIKE_LATENCY_N (default 20) ECS Fargate tasks of the
# dispatcher-spike image with scenario=hook_latency. Each task invokes
# `claude -p --settings /etc/dispatcher-spike/settings.json …`, which
# registers a PreToolUse hook on the Read tool. The prompt forces
# exactly one Read call, so the hook fires exactly once per task and
# inserts one row into `dispatcher_spike.failures` with
# `agent_id='<run-id>'`.
#
# Tasks are launched with `aws ecs run-task --count N` in batches of up
# to 10 (the Fargate per-call cap); then the harness polls until every
# task has stopped, pulls the DB rows, and prints p50/p95/max latency
# between the hook's `hook_fire_ts` and the server-side `inserted_at`.
#
# Usage:
#   scripts/dispatcher-spike/measure_hook_latency.sh
#   SPIKE_LATENCY_N=30 scripts/dispatcher-spike/measure_hook_latency.sh
#   SPIKE_RUN_ID=debug-1 scripts/dispatcher-spike/measure_hook_latency.sh
#
# Env overrides:
#   SPIKE_LATENCY_N  — number of tasks to launch (default 20)
#   SPIKE_RUN_ID     — harness run identifier stamped into
#                      dispatcher_spike.failures.agent_id (default: uuidgen)
#   SPIKE_ENV        — environment (default: dev)
#   SPIKE_REGION     — AWS region (default: us-west-2)
#   SPIKE_WAIT_SECS  — max wait in seconds for the last task to finish
#                      (default: 900)
#
# Output:
#   - stderr: a running log of launches + poll state
#   - stdout: a single JSON summary with per-row latencies and aggregates

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

SPIKE_LATENCY_N="${SPIKE_LATENCY_N:-20}"
SPIKE_RUN_ID="${SPIKE_RUN_ID:-harness-$(uuidgen | tr '[:upper:]' '[:lower:]')}"
SPIKE_ENV="${SPIKE_ENV:-dev}"
SPIKE_REGION="${SPIKE_REGION:-us-west-2}"
SPIKE_WAIT_SECS="${SPIKE_WAIT_SECS:-900}"

TASK_FAMILY="judgemind-dispatcher-spike-${SPIKE_ENV}"
CLUSTER="judgemind-${SPIKE_ENV}"
LOG_GROUP="/ecs/judgemind-dispatcher-spike-${SPIKE_ENV}"
SERVICE_NAME="judgemind-ingestion-worker-${SPIKE_ENV}"

log()  { printf '[spike-latency] %s\n' "$*" >&2; }

log "SPIKE_RUN_ID=${SPIKE_RUN_ID} N=${SPIKE_LATENCY_N} env=${SPIKE_ENV}"

TMPDIR_SPIKE="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_SPIKE}"' EXIT

# ─── Resolve networking from the ingestion worker service ──────────────────
log "resolving networking from ${CLUSTER}..."

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

# ─── Build overrides JSON (same for every task in this harness run) ────────
OVERRIDES_JSON="${TMPDIR_SPIKE}/overrides.json"
cat > "${TMPDIR_SPIKE}/build_overrides.py" <<'PYEOF'
import json, os, sys
json.dump({
    "containerOverrides": [{
        "name": "dispatcher-spike",
        "command": ["hook_latency"],
        "environment": [{"name": "SPIKE_AGENT_ID", "value": os.environ["SPIKE_RUN_ID"]}],
    }]
}, sys.stdout)
PYEOF

SPIKE_RUN_ID="${SPIKE_RUN_ID}" python3 "${TMPDIR_SPIKE}/build_overrides.py" \
    > "${OVERRIDES_JSON}"

# ─── Launch N tasks in batches of 10 (the Fargate --count cap) ─────────────
TASK_ARNS_FILE="${TMPDIR_SPIKE}/task-arns.txt"
: > "${TASK_ARNS_FILE}"

cat > "${TMPDIR_SPIKE}/extract_arns.py" <<'PYEOF'
"""Print one taskArn per line from an `aws ecs run-task` JSON file."""
import json, sys
data = json.load(open(sys.argv[1]))
for t in data.get("tasks", []):
    print(t["taskArn"])
PYEOF

launch_batch() {
    local count="$1"
    local out="${TMPDIR_SPIKE}/run-${RANDOM}.json"
    aws ecs run-task \
        --cluster "${CLUSTER}" \
        --task-definition "${TASK_FAMILY}" \
        --launch-type FARGATE \
        --count "${count}" \
        --region "${SPIKE_REGION}" \
        --output json \
        --no-cli-pager \
        --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[${SECURITY_GROUPS}],assignPublicIp=DISABLED}" \
        --overrides "file://${OVERRIDES_JSON}" \
        > "${out}"
    python3 "${TMPDIR_SPIKE}/extract_arns.py" "${out}" >> "${TASK_ARNS_FILE}"
}

REMAINING=${SPIKE_LATENCY_N}
while [[ ${REMAINING} -gt 0 ]]; do
    batch=$(( REMAINING > 10 ? 10 : REMAINING ))
    log "launching batch of ${batch}..."
    launch_batch "${batch}"
    REMAINING=$(( REMAINING - batch ))
done

N_LAUNCHED=$(wc -l < "${TASK_ARNS_FILE}" | tr -d ' ')
log "launched ${N_LAUNCHED} tasks, harness agent_id=${SPIKE_RUN_ID}"

if [[ "${N_LAUNCHED}" -lt "${SPIKE_LATENCY_N}" ]]; then
    log "WARNING: only ${N_LAUNCHED}/${SPIKE_LATENCY_N} tasks launched"
fi

# ─── Poll until every task has STOPPED ─────────────────────────────────────
STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ELAPSED=0
POLL=20

cat > "${TMPDIR_SPIKE}/count_stopped.py" <<'PYEOF'
"""Count STOPPED tasks from `aws ecs describe-tasks --tasks …` JSON."""
import json, sys
data = json.load(open(sys.argv[1]))
total = len(data.get("tasks", []))
stopped = sum(1 for t in data.get("tasks", []) if t.get("lastStatus") == "STOPPED")
print(f"{stopped}/{total}")
PYEOF

# Build the comma-separated list of task ARNs for describe-tasks.
TASK_ARNS_CSV=$(tr '\n' ' ' < "${TASK_ARNS_FILE}" | sed 's/ $//')

while [[ ${ELAPSED} -lt ${SPIKE_WAIT_SECS} ]]; do
    DT_OUT="${TMPDIR_SPIKE}/describe-${RANDOM}.json"
    # shellcheck disable=SC2086
    aws ecs describe-tasks \
        --cluster "${CLUSTER}" \
        --tasks ${TASK_ARNS_CSV} \
        --region "${SPIKE_REGION}" \
        --output json \
        --no-cli-pager \
        > "${DT_OUT}"

    STATUS=$(python3 "${TMPDIR_SPIKE}/count_stopped.py" "${DT_OUT}")
    log "status=${STATUS} elapsed=${ELAPSED}s"

    STOPPED_COUNT="${STATUS%/*}"
    TOTAL_COUNT="${STATUS#*/}"
    if [[ "${STOPPED_COUNT}" == "${TOTAL_COUNT}" ]]; then
        break
    fi

    sleep "${POLL}"
    ELAPSED=$(( ELAPSED + POLL ))
done

ENDED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
log "all tasks stopped (or timeout reached at ${ELAPSED}s)"

# ─── Query dispatcher_spike.failures for this run's rows ───────────────────
log "querying dispatcher_spike.failures where agent_id='${SPIKE_RUN_ID}'..."

QUERY="SELECT failure_id, hook_fire_ts, inserted_at, EXTRACT(EPOCH FROM (inserted_at - hook_fire_ts)) * 1000.0 AS latency_ms FROM dispatcher_spike.failures WHERE agent_id = '${SPIKE_RUN_ID}' ORDER BY inserted_at"

ROWS_FILE="${TMPDIR_SPIKE}/rows.json"
if ! "${REPO_ROOT}/scripts/dev-db-query.sh" "${QUERY}" > "${ROWS_FILE}" 2>"${TMPDIR_SPIKE}/db-err.log"; then
    log "ERROR: dev-db-query.sh failed; see ${TMPDIR_SPIKE}/db-err.log"
    cat "${TMPDIR_SPIKE}/db-err.log" >&2
    exit 2
fi

# dev-db-query.sh emits pre-JSON log lines; strip them by locating the
# first '[' or '{' and taking from there. We also defensively trim any
# non-JSON trailing lines.
cat > "${TMPDIR_SPIKE}/extract_json.py" <<'PYEOF'
"""Read stdin, extract the first complete JSON array/object, discarding
any leading log lines and trailing session-teardown chatter that
dev-db-query.sh may append (e.g. 'Cannot perform start session: EOF')."""
import json, sys
data = sys.stdin.read()
start = next((i for i, ch in enumerate(data) if ch in "[{"), -1)
if start < 0:
    sys.stderr.write("extract_json: no JSON start found\n")
    sys.exit(1)
decoder = json.JSONDecoder()
try:
    obj, end = decoder.raw_decode(data[start:])
except json.JSONDecodeError as exc:
    sys.stderr.write(f"extract_json: decode failed: {exc}\n")
    sys.exit(1)
json.dump(obj, sys.stdout, indent=2)
PYEOF

JSON_FILE="${TMPDIR_SPIKE}/rows.clean.json"
python3 "${TMPDIR_SPIKE}/extract_json.py" < "${ROWS_FILE}" > "${JSON_FILE}"

# ─── Compute aggregates ────────────────────────────────────────────────────
cat > "${TMPDIR_SPIKE}/aggregate.py" <<'PYEOF'
"""Read dispatcher_spike.failures query results and emit a summary."""
import json, sys, statistics

data = json.load(open(sys.argv[1]))
n = len(data)

if n == 0:
    print(json.dumps({
        "n": 0,
        "note": "no rows visible — hook either did not fire or insert failed",
    }, indent=2))
    sys.exit(0)

latencies = [float(row["latency_ms"]) for row in data]
latencies_sorted = sorted(latencies)

def pct(xs, p):
    if not xs:
        return None
    k = max(0, min(len(xs) - 1, int(round(p/100.0 * (len(xs) - 1)))))
    return xs[k]

summary = {
    "n": n,
    "latency_ms": {
        "min":  min(latencies),
        "p50":  pct(latencies_sorted, 50),
        "p95":  pct(latencies_sorted, 95),
        "max":  max(latencies),
        "mean": round(statistics.mean(latencies), 3),
        "stdev": round(statistics.stdev(latencies), 3) if len(latencies) > 1 else 0.0,
    },
    "rows": [
        {"failure_id": row["failure_id"], "latency_ms": round(float(row["latency_ms"]), 3)}
        for row in data
    ],
}
print(json.dumps(summary, indent=2))
PYEOF

log "aggregating ${JSON_FILE}..."
python3 "${TMPDIR_SPIKE}/aggregate.py" "${JSON_FILE}"

log "done. SPIKE_RUN_ID=${SPIKE_RUN_ID}"
