#!/usr/bin/env bash
# venv: N/A — pure bash + scripts/dev-db-query.sh + jq
# permanent: true
#
# diagnoses.sh — human-readable view of dispatcher.diagnoses rows.
#
# Since #3032, every agent-terminal failure goes through the Opus diagnoser,
# which writes its action + reasoning to dispatcher.diagnoses. That table has
# JSONB `context` (what the diagnoser was given) and JSONB `recommendation`
# (what it decided), which both resist casual psql inspection. This helper
# formats the important fields — action, reasoning, outcome, timing — in a
# way that's safe to skim during monitoring, and lets the operator filter by
# issue, agent, or recency.
#
# Usage:
#   scripts/dispatcher/helpers/diagnoses.sh                   # last 20 diagnoses
#   scripts/dispatcher/helpers/diagnoses.sh --recent 5        # last 5
#   scripts/dispatcher/helpers/diagnoses.sh --issue 3008      # all for #3008
#   scripts/dispatcher/helpers/diagnoses.sh --agent 7c8269af  # all for agent prefix
#   scripts/dispatcher/helpers/diagnoses.sh --json            # raw JSON (pipe to jq)
#   scripts/dispatcher/helpers/diagnoses.sh --full-reasoning  # don't truncate reasoning
#
# Options can be combined; --json applies on top of any filter.
# In --json mode, full_recommendation is fetched per-row and merged back in.
#
# Exit codes:
#   0  — printed successfully.
#   1  — usage error or ambiguous agent prefix.
#   2  — DB error.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dev_db="$script_dir/../../dev-db-query.sh"

if [[ ! -x "$dev_db" ]]; then
    echo "Error: $dev_db not found or not executable" >&2
    exit 2
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "Error: jq is required" >&2
    exit 2
fi

# shellcheck source=./_query_lib.sh
. "$script_dir/_query_lib.sh"

usage() {
    sed -n '6,28p' "$0" | sed 's/^# \{0,1\}//' >&2
}

query() {
    local sql="$1"
    _query_lib_run "$sql" "diagnoses"
}

# ─── Parse args ─────────────────────────────────────────────────────────

recent_n="20"
filter_issue=""
filter_agent=""
want_json="0"
full_reasoning="0"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --recent)
            recent_n="${2:-}"
            if ! [[ "$recent_n" =~ ^[0-9]+$ ]] || (( recent_n < 1 )); then
                echo "Error: --recent requires a positive integer" >&2
                exit 1
            fi
            shift 2
            ;;
        --issue)
            filter_issue="${2:-}"
            if ! [[ "$filter_issue" =~ ^[0-9]+$ ]]; then
                echo "Error: --issue requires a positive integer" >&2
                exit 1
            fi
            shift 2
            ;;
        --agent)
            filter_agent="${2:-}"
            if ! [[ "$filter_agent" =~ ^[0-9a-fA-F-]{4,36}$ ]]; then
                echo "Error: --agent requires a 4–36 hex/dash prefix" >&2
                exit 1
            fi
            shift 2
            ;;
        --json)
            want_json="1"
            shift
            ;;
        --full-reasoning)
            full_reasoning="1"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Error: unknown arg '$1'" >&2
            usage
            exit 1
            ;;
    esac
done

# ─── Resolve agent prefix if given ──────────────────────────────────────

agent_id_clause=""
if [[ -n "$filter_agent" ]]; then
    local_sql="SELECT agent_id::text AS id FROM dispatcher.agents WHERE agent_id::text LIKE '${filter_agent}%' ORDER BY started_at DESC LIMIT 5;"
    rows=$(query "$local_sql")
    n=$(echo "$rows" | jq 'length')
    if [[ "$n" == "0" ]]; then
        echo "Error: no agent matches prefix '$filter_agent'" >&2
        exit 1
    fi
    if [[ "$n" != "1" ]]; then
        echo "Error: prefix '$filter_agent' matches $n agents — narrow it" >&2
        echo "$rows" | jq -r '.[] | "  \(.id)"' >&2
        exit 1
    fi
    agent_id=$(echo "$rows" | jq -r '.[0].id')
    agent_id_clause="AND d.agent_id = '${agent_id}'"
fi

# ─── Issue filter via join ──────────────────────────────────────────────

issue_clause=""
limit_clause="LIMIT ${recent_n}"
if [[ -n "$filter_issue" ]]; then
    issue_clause="AND a.issue_number = ${filter_issue}"
    # When filtering by a specific issue, show all matches — don't cap.
    limit_clause=""
fi

# ─── Build reasoning SQL fragment (truncated by default) ────────────────

if [[ "$full_reasoning" == "1" ]]; then
    reasoning_sql="d.recommendation->>'reasoning'"
else
    reasoning_sql="CASE WHEN length(d.recommendation->>'reasoning') > 800 THEN substring(d.recommendation->>'reasoning', 1, 797) || '…' ELSE d.recommendation->>'reasoning' END"
fi

# ─── Query ──────────────────────────────────────────────────────────────

sql="
SELECT jsonb_agg(row ORDER BY diagnosis_id DESC) AS rows FROM (
  SELECT
    d.diagnosis_id,
    d.agent_id::text AS agent_id,
    a.issue_number,
    d.status,
    to_char(d.started_at AT TIME ZONE 'UTC',               'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS started_utc,
    to_char(d.started_at AT TIME ZONE 'America/Los_Angeles', 'HH24:MI:SS\" PT\"')             AS started_pt,
    CASE WHEN d.completed_at IS NULL THEN NULL
         ELSE to_char(d.completed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') END AS completed_utc,
    CASE WHEN d.completed_at IS NULL THEN NULL
         ELSE to_char(d.completed_at - d.started_at, 'FMHH24:MI:SS') END AS duration,
    d.recommendation->>'action' AS action,
    ${reasoning_sql} AS reasoning,
    (d.recommendation - 'action' - 'reasoning') AS extras,
    d.outcome,
    d.failure_id AS failure_id,
    f.category AS failure_category
  FROM dispatcher.diagnoses d
  JOIN dispatcher.agents a ON a.agent_id = d.agent_id
  LEFT JOIN dispatcher.failures f ON f.failure_id = d.failure_id
  WHERE TRUE
    ${agent_id_clause}
    ${issue_clause}
  ORDER BY d.diagnosis_id DESC
  ${limit_clause}
) row;
"

if [[ "${DIAGNOSES_DEBUG_SQL:-0}" == "1" ]]; then
    echo "===== SQL =====" >&2
    echo "$sql" >&2
    echo "===== END SQL =====" >&2
fi
result=$(query "$sql")
if [[ "${DIAGNOSES_DEBUG_SQL:-0}" == "1" ]]; then
    echo "===== RESULT (first 1000 chars) =====" >&2
    printf '%s' "$result" | head -c 1000 >&2
    echo >&2
    echo "===== RESULT (last 500 chars) =====" >&2
    printf '%s' "$result" | tail -c 500 >&2
    echo >&2
fi
rows=$(echo "$result" | jq -c '.[0].rows // []')

if [[ "$(echo "$rows" | jq 'length')" == "0" ]]; then
    echo "No diagnoses match the given filters." >&2
    exit 0
fi

if [[ "$want_json" == "1" ]]; then
    # Machine-readable: fetch full recommendation per-row and merge back.
    # Each per-row query stays well inside the SSM byte budget (#3203).
    enriched="$rows"
    count=$(echo "$rows" | jq 'length')
    idx=0
    while (( idx < count )); do
        diag_id=$(echo "$rows" | jq -r ".[$idx].diagnosis_id")
        rec_sql="SELECT recommendation FROM dispatcher.diagnoses WHERE diagnosis_id = ${diag_id};"
        rec_result=$(query "$rec_sql")
        rec_value=$(echo "$rec_result" | jq '.[0].recommendation // null')
        enriched=$(printf '%s' "$enriched" | jq \
            --argjson rec "$rec_value" \
            --argjson i "$idx" \
            'to_entries | map(if .key == $i then .value + {full_recommendation: $rec} else .value end)')
        idx=$(( idx + 1 ))
    done
    echo "$enriched" | jq '.'
    exit 0
fi

# ─── Human-readable output ──────────────────────────────────────────────

echo "$rows" | jq -r '
  reverse  # oldest first so causality reads top-down
  | .[] as $d
  # extras is already filtered server-side: (recommendation - "action" - "reasoning")
  | ($d.extras // {} | to_entries) as $extras
  | [
      "── diagnosis #\($d.diagnosis_id) ──",
      "  agent:    \($d.agent_id[:8])   issue: #\($d.issue_number)",
      "  started:  \($d.started_utc)  (\($d.started_pt))" +
        (if $d.duration then "  duration=\($d.duration)" else "" end),
      "  status:   \($d.status)\(if $d.outcome != null then "  outcome=\($d.outcome)" else "" end)",
      (if $d.failure_category != null then "  failure:  #\($d.failure_id) cat=\($d.failure_category)" else empty end),
      "  action:   \($d.action // "(null)")"
    ]
    + (if ($extras | length) > 0
         then ($extras | map("  • \(.key): " + (.value | tojson)))
         else [] end)
    + (if $d.reasoning != null
         then ["  reasoning:"] + ($d.reasoning | split("\n") | map("    " + .))
         else [] end)
    + [""]
  | .[]
'
