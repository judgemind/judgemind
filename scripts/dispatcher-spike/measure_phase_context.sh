#!/usr/bin/env bash
# measure_phase_context.sh - Dispatcher v2 Spike 0.3.
#
# Measures input-token, peak-context, output-token, and wall-clock for a
# single /task-v2-<phase> skill invocation via `claude -p`.
#
# Reuses the spike-0.1 Fargate wrapper (`run_fargate_claude_p.sh`) to
# launch the task under the dispatcher-spike task definition, then parses
# the stream-json output to extract usage.input_tokens per assistant turn.
# Peak context is max(input_tokens + cache_read_input_tokens +
# cache_creation_input_tokens) across all turns.
#
# Usage:
#   scripts/dispatcher-spike/measure_phase_context.sh <phase> <fixture-path>
#
#   phase       ∈ {plan, ralph, summary, fix-ci, verify, retro}
#   fixture     path to the input JSON fixture (e.g. tmp/fixtures/plan_input.json)
#
# Example:
#   scripts/dispatcher-spike/measure_phase_context.sh plan \
#       .claude/worktrees/agent-XXXXX/tmp/fixtures/plan_input.json
#
# The script writes a one-line JSON summary to stdout and appends a row
# to ${OUTDIR}/phase_measurements.jsonl:
#   {"phase": "plan", "input_tokens": 12345, "peak_context_tokens": 34567,
#    "output_tokens": 789, "wall_clock_s": 42.3, "exit_code": 0,
#    "model": "claude-opus-4-7", "fixture": "plan_input.json"}
#
# Prerequisites:
# 1. The dispatcher-spike Docker image must be rebuilt with
#    .claude/skills/task-v2-*/SKILL.md baked in (follow-up #TBD).
# 2. Extend container-entry.sh with a `measure_phase` scenario that:
#    - reads the fixture from /workspace/fixtures/<fixture>.json
#    - invokes `claude -p --output-format stream-json --include-hook-events
#      --max-turns 500 --mcp-config /etc/dispatcher-spike/mcp-config.json
#      -- "/<task-v2-phase> $(cat /workspace/fixtures/<fixture>.json)"`
#    - streams the JSON to stdout (CloudWatch captures it)
# 3. After the task stops, pull the stream-json log and run
#    `tmp/parse_stream_json.py` to produce the summary row.
#
# This script is the dispatcher-v2-spike-0.3 harness; throwaway along
# with the rest of the spike infra per #2685.

set -euo pipefail

PHASE="${1:?Usage: $0 <phase> <fixture-path>}"
FIXTURE="${2:?Usage: $0 <phase> <fixture-path>}"

case "${PHASE}" in
    plan|ralph|summary|fix-ci|verify|retro) ;;
    *)
        echo "Error: unknown phase '${PHASE}'" >&2
        exit 2
        ;;
esac

if [[ ! -f "${FIXTURE}" ]]; then
    echo "Error: fixture not found: ${FIXTURE}" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
OUTDIR="${REPO_ROOT}/tmp/spike-0.3"
mkdir -p "${OUTDIR}"

AGENT_ID="spike-0.3-${PHASE}-$(date -u +%s)"
FIXTURE_NAME="$(basename "${FIXTURE}")"

echo "[measure] phase=${PHASE} fixture=${FIXTURE_NAME} agent_id=${AGENT_ID}"

# 1. Upload fixture to an S3 location the spike task can read from,
#    OR bake fixtures into the image at build time (preferred — smaller
#    surface area, no runtime S3 dependency).
#
# 2. Launch Fargate task with the `measure_phase` scenario + overrides:
#       SCENARIO=measure_phase
#       MEASURE_PHASE=${PHASE}
#       MEASURE_FIXTURE=${FIXTURE_NAME}
#       SPIKE_AGENT_ID=${AGENT_ID}
#
#    The run_fargate_claude_p.sh wrapper already handles environment-var
#    injection via SPIKE_AGENT_ID; extend it for MEASURE_PHASE and
#    MEASURE_FIXTURE the same way.
#
# 3. After STOPPED, pull the CloudWatch log stream, run through
#    parse_stream_json.py, compute peak_context_tokens.

SPIKE_AGENT_ID="${AGENT_ID}" \
    SCENARIO=measure_phase \
    MEASURE_PHASE="${PHASE}" \
    MEASURE_FIXTURE="${FIXTURE_NAME}" \
    SPIKE_NOTES="spike-0.3 measurement phase=${PHASE} fixture=${FIXTURE_NAME}" \
    "${SCRIPT_DIR}/run_fargate_claude_p.sh" measure_phase \
    > "${OUTDIR}/${AGENT_ID}.run.json"

# The wrapper writes stdout_tail to the run summary; parse for stream-json.
STDOUT_TAIL_FILE="${OUTDIR}/${AGENT_ID}.stdout.txt"
python3 - "${OUTDIR}/${AGENT_ID}.run.json" > "${STDOUT_TAIL_FILE}" <<'PYEOF'
import json, sys
run = json.load(open(sys.argv[1]))
print(run.get("stdout_tail", ""))
PYEOF

python3 "${SCRIPT_DIR}/parse_stream_json.py" \
    --phase "${PHASE}" \
    --fixture "${FIXTURE_NAME}" \
    --stdout-tail "${STDOUT_TAIL_FILE}" \
    --run-json "${OUTDIR}/${AGENT_ID}.run.json" \
    | tee -a "${OUTDIR}/phase_measurements.jsonl"
