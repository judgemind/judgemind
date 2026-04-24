#!/usr/bin/env bash
# venv: N/A — pure bash + scripts/dev-db-query.sh + jq
# permanent: true
#
# fleet-status.sh — single-call dispatcher fleet snapshot.
#
# Replaces the cluster of SELECTs I kept typing by hand on each monitoring
# cycle (active agents, recent terminals, cap/breaker state, recent
# diagnoses, daemon-shipped greens, pending commands). One ECS-exec
# round-trip via scripts/dev-db-query.sh; jq-formatted output.
#
# Timestamps are UTC with `Z` AND Pacific with PT because the operator
# defaults to Pacific in conversation (see feedback_timezone_in_communication).
# Cap/breaker summary is intentionally brief — use `breaker.sh status` for
# the full window breakdown.
#
# Usage:
#   scripts/dispatcher/helpers/fleet-status.sh                # defaults below
#   scripts/dispatcher/helpers/fleet-status.sh --since 2h     # time-window for terminals / greens / diagnoses
#   scripts/dispatcher/helpers/fleet-status.sh --terminals 20 # cap on recent terminals
#   scripts/dispatcher/helpers/fleet-status.sh --greens 10    # cap on daemon-shipped greens
#
# Defaults: --since 1h, --terminals 10, --greens 5.
#
# Exit codes:
#   0  — printed successfully.
#   1  — usage error.
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
    sed -n '6,27p' "$0" | sed 's/^# \{0,1\}//' >&2
}

query() {
    local sql="$1"
    # _query_lib_run validates JSON and retries up to 3x — replaces the
    # older ad-hoc `dev-db-query.sh | awk | jq` pipeline that occasionally
    # hit `parse error: Invalid numeric literal` when the ECS-exec stream
    # delivered a partial payload or a trailing banner. See #3124.
    _query_lib_run "$sql" "fleet-status"
}

# ─── Parse args ─────────────────────────────────────────────────────────

since="1h"
terminals_cap="10"
greens_cap="5"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --since)
            since="${2:-}"
            if ! [[ "$since" =~ ^[0-9]+[hm]$ ]]; then
                echo "Error: --since requires a value like '90m' or '2h'" >&2
                exit 1
            fi
            shift 2
            ;;
        --terminals)
            terminals_cap="${2:-}"
            if ! [[ "$terminals_cap" =~ ^[0-9]+$ ]] || (( terminals_cap < 1 )); then
                echo "Error: --terminals requires a positive integer" >&2
                exit 1
            fi
            shift 2
            ;;
        --greens)
            greens_cap="${2:-}"
            if ! [[ "$greens_cap" =~ ^[0-9]+$ ]] || (( greens_cap < 1 )); then
                echo "Error: --greens requires a positive integer" >&2
                exit 1
            fi
            shift 2
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

# Convert the --since token to a Postgres interval expression.
since_interval=""
if [[ "$since" == *h ]]; then
    since_interval="make_interval(hours => ${since%h})"
else
    since_interval="make_interval(mins => ${since%m})"
fi

# ─── Single bundled query ───────────────────────────────────────────────
# Keep each CTE's output small — no huge JSONB blobs (e.g. diagnosis.context)
# in the result, because dev-db-query.sh streams over ECS-exec and the
# session can close mid-stream on very large payloads (observed when
# including diagnosis.context). Keep this fleet-wide and lightweight;
# drill into details with agent-timeline.sh / diagnoses.sh / breaker.sh.

sql="
WITH
  active AS (
    SELECT COALESCE(jsonb_agg(a ORDER BY started_at), '[]'::jsonb) AS data FROM (
      SELECT
        agent_id::text AS agent_id,
        issue_number,
        phase,
        status,
        priority,
        to_char(started_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS started_utc,
        to_char(started_at AT TIME ZONE 'America/Los_Angeles', 'HH24:MI:SS\" PT\"') AS started_pt,
        to_char(now() - started_at, 'FMHH24:MI:SS') AS lifetime,
        started_at
      FROM dispatcher.agents
      WHERE status IN ('running', 'claiming')
      ORDER BY started_at
    ) a
  ),
  terminals AS (
    SELECT COALESCE(jsonb_agg(t ORDER BY ended_at DESC), '[]'::jsonb) AS data FROM (
      SELECT
        agent_id::text AS agent_id,
        issue_number,
        phase,
        status,
        pr_number,
        -- Truncate to avoid the multi-line pre-push dumps bleeding into
        -- the snapshot; operator can use agent-timeline.sh / CloudWatch
        -- for the full text.
        CASE WHEN failure_summary IS NULL THEN NULL
             WHEN length(failure_summary) > 140
               THEN regexp_replace(substring(failure_summary, 1, 137), E'\\\\s+', ' ', 'g') || '…'
             ELSE regexp_replace(failure_summary, E'\\\\s+', ' ', 'g') END AS failure_summary,
        to_char(ended_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS ended_utc,
        to_char(ended_at AT TIME ZONE 'America/Los_Angeles', 'HH24:MI:SS\" PT\"') AS ended_pt,
        to_char(ended_at - started_at, 'FMHH24:MI:SS') AS lifetime,
        ended_at
      FROM dispatcher.agents
      WHERE ended_at IS NOT NULL
        AND ended_at > now() - ${since_interval}
      ORDER BY ended_at DESC
      LIMIT ${terminals_cap}
    ) t
  ),
  config_row AS (
    SELECT jsonb_build_object(
      'cap',        (SELECT value::int FROM dispatcher.config WHERE key='concurrency_cap'),
      'flipped_by', (SELECT value::text FROM dispatcher.config WHERE key='cap_flipped_by')
    ) AS data
  ),
  diagnoses_recent AS (
    SELECT COALESCE(jsonb_agg(d ORDER BY diagnosis_id DESC), '[]'::jsonb) AS data FROM (
      SELECT
        d.diagnosis_id,
        d.agent_id::text AS agent_id,
        a.issue_number,
        d.status,
        d.recommendation->>'action' AS action,
        to_char(d.started_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS started_utc,
        to_char(d.started_at AT TIME ZONE 'America/Los_Angeles', 'HH24:MI:SS\" PT\"') AS started_pt
      FROM dispatcher.diagnoses d
      JOIN dispatcher.agents a ON a.agent_id = d.agent_id
      WHERE d.started_at > now() - ${since_interval}
      ORDER BY d.diagnosis_id DESC
      LIMIT 10
    ) d
  ),
  greens AS (
    SELECT COALESCE(jsonb_agg(g ORDER BY merged_at DESC), '[]'::jsonb) AS data FROM (
      SELECT
        agent_id::text AS agent_id,
        issue_number,
        pr_number,
        to_char(merged_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS merged_utc,
        to_char(merged_at AT TIME ZONE 'America/Los_Angeles', 'HH24:MI:SS\" PT\"') AS merged_pt,
        to_char(merged_at - started_at, 'FMHH24:MI:SS') AS lifetime,
        issue_title,
        merged_at
      FROM dispatcher.agents
      WHERE merged_at IS NOT NULL
        AND merged_at > now() - ${since_interval}
      ORDER BY merged_at DESC
      LIMIT ${greens_cap}
    ) g
  ),
  pending AS (
    SELECT COALESCE(jsonb_agg(c ORDER BY issued_at), '[]'::jsonb) AS data FROM (
      SELECT
        command_id,
        command,
        issued_by,
        to_char(issued_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS issued_utc,
        payload,
        issued_at
      FROM dispatcher.commands
      WHERE consumed_at IS NULL
      ORDER BY issued_at
    ) c
  ),
  totals AS (
    SELECT jsonb_build_object(
      'total_terminals_in_window',
         (SELECT COUNT(*) FROM dispatcher.agents
          WHERE ended_at IS NOT NULL AND ended_at > now() - ${since_interval}),
      'total_greens_in_window',
         (SELECT COUNT(*) FROM dispatcher.agents
          WHERE merged_at IS NOT NULL AND merged_at > now() - ${since_interval}),
      'total_diagnoses_in_window',
         (SELECT COUNT(*) FROM dispatcher.diagnoses
          WHERE started_at > now() - ${since_interval})
    ) AS data
  )
SELECT jsonb_build_object(
  'since',             '${since}',
  'active',            (SELECT data FROM active),
  'terminals',         (SELECT data FROM terminals),
  'config',            (SELECT data FROM config_row),
  'diagnoses_recent',  (SELECT data FROM diagnoses_recent),
  'greens',            (SELECT data FROM greens),
  'pending_commands',  (SELECT data FROM pending),
  'totals',            (SELECT data FROM totals)
) AS snapshot;
"

if [[ "${FLEET_STATUS_DEBUG:-0}" == "1" ]]; then
    echo "===== SQL =====" >&2
    echo "$sql" >&2
fi
result=$(query "$sql")
snapshot=$(echo "$result" | jq '.[0].snapshot')

# ─── Format ─────────────────────────────────────────────────────────────

echo "$snapshot" | jq -r '
  . as $s
  | $s.config as $cfg
  | $s.totals as $t
  | ["── active agents ──"]
    + (if ($s.active | length) == 0
         then ["  (none)"]
         else ($s.active | map(
           "  \(.agent_id[:8])  #\(.issue_number)  \(.phase)  \(.status)  spawned \(.started_utc)  (\(.started_pt))  lifetime=\(.lifetime)"
           + (if .priority then "  \(.priority)" else "" end)
         ))
       end)
    + ["",
       "── cap / breaker ──",
       "  cap: \($cfg.cap)    flipped_by: \($cfg.flipped_by // "(null)")    (use breaker.sh status for the window breakdown)",
       ""]
    + ["── terminals in window (\($s.since)) — showing \($s.terminals | length) of \($t.total_terminals_in_window) ──"]
    + (if ($s.terminals | length) == 0
         then ["  (none)"]
         else ($s.terminals | map(
           "  ended \(.ended_utc)  (\(.ended_pt))  \(.agent_id[:8])  #\(.issue_number)  \(.phase)/\(.status)  lifetime=\(.lifetime)"
           + (if .pr_number then "  PR #\(.pr_number)" else "" end)
           + (if .failure_summary then "\n    summary: \(.failure_summary)" else "" end)
         ))
       end)
    + ["",
       "── daemon-shipped greens (\($s.since)) — showing \($s.greens | length) of \($t.total_greens_in_window) ──"]
    + (if ($s.greens | length) == 0
         then ["  (none)"]
         else ($s.greens | map(
           "  merged \(.merged_utc)  (\(.merged_pt))  PR #\(.pr_number)  #\(.issue_number)  lifetime=\(.lifetime)"
           + (if .issue_title then "\n    \(.issue_title)" else "" end)
         ))
       end)
    + ["",
       "── diagnoses in window (\($s.since)) — showing \($s.diagnoses_recent | length) of \($t.total_diagnoses_in_window) ──"]
    + (if ($s.diagnoses_recent | length) == 0
         then ["  (none)"]
         else ($s.diagnoses_recent | map(
           "  #\(.diagnosis_id)  \(.started_utc)  (\(.started_pt))  \(.agent_id[:8])  issue #\(.issue_number)  action=\(.action // "(null)")  status=\(.status)"
         ))
       end)
    + ["",
       "── pending commands ──"]
    + (if ($s.pending_commands | length) == 0
         then ["  (none)"]
         else ($s.pending_commands | map(
           "  [\(.issued_utc)] #\(.command_id) \(.command)  by=\(.issued_by)  payload=\(.payload | tostring)"
         ))
       end)
  | .[]
'
