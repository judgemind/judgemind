#!/usr/bin/env bash
# venv: N/A — pure bash + scripts/dev-db-query.sh + jq
# permanent: true
#
# agent-timeline.sh — Human-readable timeline for a dispatcher agent.
#
# Prints an agent's spawn/terminal/lifetime, its phase_transitions with
# per-phase durations, its failures, its diagnoses, and its retry markers.
# All timestamps shown as UTC (ISO 8601 with `Z`) AND Pacific time, because
# I default to Pacific in conversation and need an unambiguous column for
# each (see feedback memory `feedback_timezone_in_communication.md`).
#
# Usage:
#   scripts/dispatcher/agent-timeline.sh <agent-id-or-prefix>
#
# Example:
#   scripts/dispatcher/agent-timeline.sh 4b86287b
#
# The prefix only needs to be unique among the most recent agents; if it
# matches multiple rows the script prints candidates and exits non-zero so
# you can disambiguate.
#
# Exit codes:
#   0  — printed timeline for exactly one matching agent.
#   1  — usage error, no match, or ambiguous prefix.
#   2  — database error or dev-db-query.sh failure.

set -euo pipefail

usage() {
    echo "Usage: scripts/dispatcher/agent-timeline.sh <agent-id-or-prefix>" >&2
    echo "Example: scripts/dispatcher/agent-timeline.sh 4b86287b" >&2
}

if [[ $# -ne 1 || "$1" == "-h" || "$1" == "--help" ]]; then
    usage
    exit 1
fi

prefix="$1"

if ! [[ "$prefix" =~ ^[0-9a-fA-F-]{4,36}$ ]]; then
    echo "Error: invalid agent-id prefix '$prefix'" >&2
    echo "Expected 4–36 hex/dash characters." >&2
    exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dev_db="$script_dir/../dev-db-query.sh"

if [[ ! -x "$dev_db" ]]; then
    echo "Error: $dev_db not found or not executable" >&2
    exit 2
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "Error: jq is required (brew install jq)" >&2
    exit 2
fi

# ─── Resolve prefix to a unique agent_id ─────────────────────────────

resolve_sql="SELECT agent_id::text AS id, issue_number, phase, status FROM dispatcher.agents WHERE agent_id::text LIKE '${prefix}%' ORDER BY started_at DESC LIMIT 5;"

resolve_json=$("$dev_db" "$resolve_sql" 2>/dev/null | awk '/^\[/{f=1} f&&!g{print} /^\]/{if(f)g=1}')

if [[ -z "$resolve_json" ]]; then
    echo "Error: dev-db-query.sh produced no result" >&2
    exit 2
fi

match_count=$(echo "$resolve_json" | jq 'length')

if [[ "$match_count" == "0" ]]; then
    echo "No agent matches prefix '$prefix'" >&2
    exit 1
fi

if [[ "$match_count" != "1" ]]; then
    echo "Prefix '$prefix' is ambiguous — $match_count matches (most recent first):" >&2
    echo "$resolve_json" | jq -r '.[] | "  \(.id)  #\(.issue_number)  \(.phase)/\(.status)"' >&2
    exit 1
fi

agent_id=$(echo "$resolve_json" | jq -r '.[0].id')

# ─── Fetch the full timeline as a single JSON blob ───────────────────
# Five sections wrapped in jsonb_build_object so we make one ECS-exec
# round-trip instead of five. Each timestamp is formatted once as UTC
# (with Z) and once as Pacific (with PT), for unambiguous display.

timeline_sql="
WITH
  agent AS (
    SELECT jsonb_build_object(
      'agent_id',    agent_id::text,
      'issue_number', issue_number,
      'pr_number',   pr_number,
      'phase',       phase,
      'status',      status,
      'model_override', model_override,
      'priority',    priority,
      'issue_title', issue_title,
      'failure_summary', failure_summary,
      'spawned_utc', to_char(started_at AT TIME ZONE 'UTC',               'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),
      'spawned_pt',  to_char(started_at AT TIME ZONE 'America/Los_Angeles', 'YYYY-MM-DD HH24:MI:SS\" PT\"'),
      'ended_utc',   CASE WHEN ended_at IS NULL THEN NULL
                          ELSE to_char(ended_at AT TIME ZONE 'UTC',               'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') END,
      'ended_pt',    CASE WHEN ended_at IS NULL THEN NULL
                          ELSE to_char(ended_at AT TIME ZONE 'America/Los_Angeles', 'YYYY-MM-DD HH24:MI:SS\" PT\"') END,
      'merged_utc',  CASE WHEN merged_at IS NULL THEN NULL
                          ELSE to_char(merged_at AT TIME ZONE 'UTC',               'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') END,
      'lifetime',    CASE WHEN ended_at IS NULL THEN to_char(now() - started_at, 'FMHH24:MI:SS') || ' (still running)'
                          ELSE to_char(ended_at - started_at, 'FMHH24:MI:SS') END,
      'lifetime_seconds', EXTRACT(EPOCH FROM (COALESCE(ended_at, now()) - started_at))
    ) AS data
    FROM dispatcher.agents
    WHERE agent_id = '${agent_id}'
  ),
  transitions AS (
    -- phase_transitions.ts is the phase-END timestamp (the row is inserted
    -- when a phase EXITS). So the duration of a given row's phase is
    -- ts - (prev row's ts, or agent.started_at for the first row). Using
    -- LEAD would give the duration of the NEXT phase, which was the old
    -- bug — this script now reports the correct phase duration under the
    -- correct phase name.
    SELECT COALESCE(jsonb_agg(t), '[]'::jsonb) AS data FROM (
      SELECT jsonb_build_object(
        'phase',       phase,
        'ts_utc',      to_char(ts AT TIME ZONE 'UTC',               'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),
        'ts_pt',       to_char(ts AT TIME ZONE 'America/Los_Angeles', 'HH24:MI:SS\" PT\"'),
        'phase_duration', to_char(
          ts - COALESCE(
            LAG(ts) OVER (ORDER BY ts),
            (SELECT started_at FROM dispatcher.agents WHERE agent_id = '${agent_id}')
          ),
          'FMHH24:MI:SS'
        ),
        'autocompact_count', autocompact_count
      ) AS t
      FROM dispatcher.phase_transitions
      WHERE agent_id = '${agent_id}'
      ORDER BY ts
    ) x
  ),
  failures AS (
    SELECT COALESCE(jsonb_agg(f), '[]'::jsonb) AS data FROM (
      SELECT jsonb_build_object(
        'ts_utc',     to_char(ts AT TIME ZONE 'UTC',               'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),
        'ts_pt',      to_char(ts AT TIME ZONE 'America/Los_Angeles', 'HH24:MI:SS\" PT\"'),
        'category',   category,
        'detected_by', detected_by,
        'details',    details
      ) AS f
      FROM dispatcher.failures
      WHERE agent_id = '${agent_id}'
      ORDER BY ts
    ) x
  ),
  diagnoses AS (
    SELECT COALESCE(jsonb_agg(d), '[]'::jsonb) AS data FROM (
      SELECT jsonb_build_object(
        'diagnosis_id', diagnosis_id,
        'status',       status,
        'started_utc',  to_char(started_at   AT TIME ZONE 'UTC',               'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),
        'completed_utc', CASE WHEN completed_at IS NULL THEN NULL
                              ELSE to_char(completed_at AT TIME ZONE 'UTC',               'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') END,
        'duration',     CASE WHEN completed_at IS NULL THEN NULL
                              ELSE to_char(completed_at - started_at, 'FMHH24:MI:SS') END,
        'action',       recommendation->>'action',
        'reasoning',    recommendation->>'reasoning',
        'outcome',      outcome
      ) AS d
      FROM dispatcher.diagnoses
      WHERE agent_id = '${agent_id}'
      ORDER BY diagnosis_id
    ) x
  ),
  retry_markers AS (
    SELECT COALESCE(jsonb_agg(m), '[]'::jsonb) AS data FROM (
      SELECT jsonb_build_object(
        'marker_id',       marker_id,
        'reason',          reason,
        'attempt',         attempt,
        'retry_after_utc', to_char(retry_after_ts AT TIME ZONE 'UTC',               'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),
        'resolved_utc',    CASE WHEN resolved_at IS NULL THEN NULL
                                 ELSE to_char(resolved_at AT TIME ZONE 'UTC',               'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') END,
        'resolved',        resolved_at IS NOT NULL
      ) AS m
      FROM dispatcher.retry_markers
      WHERE agent_id = '${agent_id}'
      ORDER BY marker_id
    ) x
  )
SELECT jsonb_build_object(
  'agent',         (SELECT data FROM agent),
  'transitions',   (SELECT data FROM transitions),
  'failures',      (SELECT data FROM failures),
  'diagnoses',     (SELECT data FROM diagnoses),
  'retry_markers', (SELECT data FROM retry_markers)
) AS timeline;
"

timeline_result=$("$dev_db" "$timeline_sql" 2>/dev/null | awk '/^\[/{f=1} f&&!g{print} /^\]/{if(f)g=1}')

if [[ -z "$timeline_result" ]]; then
    echo "Error: timeline query returned no result" >&2
    exit 2
fi

# dev-db-query.sh returns an array-of-one-row; pull out the single `timeline` object.
timeline=$(echo "$timeline_result" | jq '.[0].timeline')

# ─── Format and print ────────────────────────────────────────────────

echo "$timeline" | jq -r '
  . as $root
  | ($root.agent // {}) as $a
  | [
      "── agent ──",
      "  id:       \($a.agent_id // "?")",
      "  issue:    #\($a.issue_number // "?")\(if $a.issue_title then " — \($a.issue_title)" else "" end)",
      "  priority: \($a.priority // "?")",
      "  pr:       \(if $a.pr_number then "#\($a.pr_number)" else "(no PR)" end)",
      "  phase:    \($a.phase // "?")",
      "  status:   \($a.status // "?")\(if $a.model_override then " (model_override=\($a.model_override))" else "" end)",
      "  spawned:  \($a.spawned_utc // "?")  (\($a.spawned_pt // "?"))",
      if $a.ended_utc then "  ended:    \($a.ended_utc)  (\($a.ended_pt))" else "  ended:    (still running)" end,
      if $a.merged_utc then "  merged:   \($a.merged_utc)" else empty end,
      "  lifetime: \($a.lifetime // "?")",
      if $a.failure_summary then "  summary:  \($a.failure_summary)" else empty end,
      "",
      "── phase_transitions ──"
    ] + (
      if ($root.transitions | length) == 0
        then ["  (none)"]
        else ($root.transitions | map(
          "  ended \(.ts_utc)  (\(.ts_pt))  \(.phase)  ran [\(.phase_duration)]\(if .autocompact_count and .autocompact_count > 0 then "  ac=\(.autocompact_count)" else "" end)"
        ))
      end
    ) + ["", "── failures ──"] + (
      if ($root.failures | length) == 0
        then ["  (none)"]
        else ($root.failures | map(
          "  \(.ts_utc)  (\(.ts_pt))  \(.category)  detected_by=\(.detected_by)" +
          (if .details != null then "\n    details: " + (.details | tojson) else "" end)
        ))
      end
    ) + ["", "── diagnoses ──"] + (
      if ($root.diagnoses | length) == 0
        then ["  (none)"]
        else ($root.diagnoses | map(
          "  id=\(.diagnosis_id)  status=\(.status)\(if .duration then "  duration=\(.duration)" else "" end)\(if .action then "  action=\(.action)" else "" end)" +
          "\n    started:  \(.started_utc)" +
          (if .completed_utc then "\n    completed:\(.completed_utc)" else "" end) +
          (if .reasoning then "\n    reasoning: \(.reasoning)" else "" end)
        ))
      end
    ) + ["", "── retry_markers ──"] + (
      if ($root.retry_markers | length) == 0
        then ["  (none)"]
        else ($root.retry_markers | map(
          "  id=\(.marker_id)  attempt=\(.attempt)  reason=\(.reason)  \(if .resolved then "resolved@\(.resolved_utc)" else "pending (retry_after=\(.retry_after_utc))" end)"
        ))
      end
    )
  | .[]
'
