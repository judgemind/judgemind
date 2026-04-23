#!/usr/bin/env bash
# venv: N/A — pure bash + scripts/dev-db-query.sh + jq
# permanent: true
#
# issue-timeline.sh — Cross-agent dispatcher activity for a single issue.
#
# An issue can be picked up by many agents (initial attempt + diagnoser
# retries + reissue/replan paths). agent-timeline.sh tells you what one
# agent did; this tells you what the *dispatcher* did for one issue,
# interleaved across every agent that touched it. Includes per-agent
# rollup, then a single chronological timeline of: agent spawns, phase
# transitions (with per-agent durations), failures, diagnoses (start +
# completion), retry markers (scheduled + resolved), shipped/verified/
# retroed milestones, agent terminals, and dispatcher notifications
# targeting the issue.
#
# Timestamps shown as UTC (ISO 8601 with `Z`) AND Pacific time, matching
# agent-timeline.sh / fleet-status.sh / diagnoses.sh — see feedback memory
# `feedback_timezone_in_communication.md`.
#
# Usage:
#   scripts/dispatcher/helpers/issue-timeline.sh <issue-number> [--full]
#
# By default, omits the heavy JSONB payloads (failure `details`,
# diagnosis `reasoning` / `outcome`) — they bloat the line, can run
# into thousands of chars, and the dev-db-query.sh ECS-exec stream has
# been observed to truncate on large payloads (see fleet-status.sh
# header). Pass --full to include them; for the full text, use
# `agent-timeline.sh <agent>` or `diagnoses.sh --issue <N>`.
#
# Example:
#   scripts/dispatcher/helpers/issue-timeline.sh 3008
#   scripts/dispatcher/helpers/issue-timeline.sh 3008 --full
#
# Exit codes:
#   0  — printed timeline.
#   1  — usage error or no agents found for the issue.
#   2  — database error.

set -euo pipefail

usage() {
    echo "Usage: scripts/dispatcher/helpers/issue-timeline.sh <issue-number> [--full]" >&2
    echo "Example: scripts/dispatcher/helpers/issue-timeline.sh 3008" >&2
}

issue=""
full="0"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --full)
            full="1"
            shift
            ;;
        *)
            if [[ -n "$issue" ]]; then
                echo "Error: unexpected arg '$1'" >&2
                usage
                exit 1
            fi
            issue="$1"
            shift
            ;;
    esac
done

if [[ -z "$issue" ]]; then
    usage
    exit 1
fi

if ! [[ "$issue" =~ ^[0-9]+$ ]]; then
    echo "Error: issue number must be a positive integer (got '$issue')" >&2
    exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dev_db="$script_dir/../../dev-db-query.sh"

if [[ ! -x "$dev_db" ]]; then
    echo "Error: $dev_db not found or not executable" >&2
    exit 2
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "Error: jq is required (brew install jq)" >&2
    exit 2
fi

json_only() {
    awk '/^\[/{f=1} f&&!g{print} /^\]/{if(f)g=1}'
}

# Heavy-field SQL fragments — set to NULL when --full is not given,
# so the bundled jsonb stays small enough to survive the ECS-exec
# stream. The formatter renders `(omitted — pass --full)` in their
# place so it's obvious why they're missing.
if [[ "$full" == "1" ]]; then
    failure_details_sql="f.details"
    diagnose_reasoning_sql="d.recommendation->>'reasoning'"
    diagnose_outcome_sql="d.outcome"
else
    failure_details_sql="NULL::jsonb"
    diagnose_reasoning_sql="NULL::text"
    diagnose_outcome_sql="NULL::jsonb"
fi

# ─── Two queries ─────────────────────────────────────────────────────
# One bundled query (header: summary + agents) plus a separate query for
# the events stream. The split exists because the dev-db-query.sh ECS-
# exec stream truncates around ~10 KB on a single response (observed
# `Cannot perform start session: EOF` mid-output for issues with ~20+
# events). Two smaller round-trips beat one truncated one. Both queries
# re-derive `dispatcher.agents WHERE issue_number = N` because CTEs
# don't span query boundaries.
#
# Phase durations are computed per-agent via LAG partitioned on agent_id
# (the LAG-vs-LEAD subtlety from agent-timeline.sh applies the same way
# here: phase_transitions.ts is the phase-END timestamp, so the duration
# of the row's phase = ts - prior_ts_for_same_agent or agent.started_at).

header_sql="
WITH
  this_issue AS (
    SELECT * FROM dispatcher.agents WHERE issue_number = ${issue}
  ),
  summary AS (
    SELECT jsonb_build_object(
      'issue_number',     ${issue},
      'agent_count',      COUNT(*),
      'titles',           jsonb_agg(DISTINCT issue_title) FILTER (WHERE issue_title IS NOT NULL),
      'priorities',       jsonb_agg(DISTINCT priority)    FILTER (WHERE priority IS NOT NULL),
      'pr_numbers',       jsonb_agg(DISTINCT pr_number)   FILTER (WHERE pr_number IS NOT NULL),
      'merged_pr',        MAX(pr_number) FILTER (WHERE merged_at IS NOT NULL),
      'merged_utc',       to_char(MAX(merged_at) AT TIME ZONE 'UTC',               'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),
      'merged_pt',        to_char(MAX(merged_at) AT TIME ZONE 'America/Los_Angeles', 'YYYY-MM-DD HH24:MI:SS\" PT\"'),
      'first_spawn_utc',  to_char(MIN(started_at) AT TIME ZONE 'UTC',               'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),
      'first_spawn_pt',   to_char(MIN(started_at) AT TIME ZONE 'America/Los_Angeles', 'YYYY-MM-DD HH24:MI:SS\" PT\"'),
      'last_event_utc',   to_char(GREATEST(MAX(started_at), MAX(COALESCE(ended_at, started_at))) AT TIME ZONE 'UTC',               'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),
      'last_event_pt',    to_char(GREATEST(MAX(started_at), MAX(COALESCE(ended_at, started_at))) AT TIME ZONE 'America/Los_Angeles', 'YYYY-MM-DD HH24:MI:SS\" PT\"'),
      'span',             to_char(GREATEST(MAX(started_at), MAX(COALESCE(ended_at, started_at))) - MIN(started_at), 'FMHH24:MI:SS'),
      'status_counts',    (
        SELECT jsonb_object_agg(status, n) FROM (
          SELECT status, COUNT(*) AS n FROM this_issue GROUP BY status
        ) s
      ),
      'still_running',    SUM(CASE WHEN ended_at IS NULL THEN 1 ELSE 0 END)
    ) AS data
    FROM this_issue
  ),
  agents_list AS (
    SELECT COALESCE(jsonb_agg(a ORDER BY a.started_at), '[]'::jsonb) AS data FROM (
      SELECT
        SUBSTRING(agent_id::text, 1, 8) AS agent_id,
        kind,
        phase,
        status,
        priority,
        pr_number,
        model_override,
        failure_summary,
        to_char(started_at AT TIME ZONE 'UTC',               'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS spawned_utc,
        to_char(started_at AT TIME ZONE 'America/Los_Angeles', 'HH24:MI:SS\" PT\"')             AS spawned_pt,
        CASE WHEN ended_at IS NULL THEN NULL
             ELSE to_char(ended_at AT TIME ZONE 'UTC',               'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') END AS ended_utc,
        CASE WHEN ended_at IS NULL THEN NULL
             ELSE to_char(ended_at AT TIME ZONE 'America/Los_Angeles', 'HH24:MI:SS\" PT\"') END             AS ended_pt,
        CASE WHEN ended_at IS NULL THEN to_char(now() - started_at,        'FMHH24:MI:SS') || ' (still running)'
             ELSE                       to_char(ended_at - started_at,     'FMHH24:MI:SS') END AS lifetime,
        merged_at IS NOT NULL  AS merged,
        verified_at IS NOT NULL AS verified,
        verify_skip_reason,
        retroed_at IS NOT NULL AS retroed,
        started_at
      FROM this_issue
      ORDER BY started_at
    ) a
  )
SELECT jsonb_build_object(
  'summary', (SELECT data FROM summary),
  'agents',  (SELECT data FROM agents_list)
) AS header;
"

timeline_sql_template="
WITH
  this_issue AS (
    SELECT * FROM dispatcher.agents WHERE issue_number = ${issue}
  ),
  events AS (
    -- Each branch yields (ts, kind, agent_id, payload). NULL agent_id
    -- is reserved for issue-level rows (notifications). The outer
    -- ORDER BY ts produces the interleaved timeline.
    SELECT * FROM (
      -- agent spawns
      SELECT started_at AS ts,
             'spawn' AS kind,
             SUBSTRING(agent_id::text, 1, 8) AS agent_id,
             jsonb_build_object(
               'kind_field', kind,
               'phase',      phase,
               'priority',   priority,
               'model_override', model_override
             ) AS payload
        FROM this_issue

      UNION ALL
      -- agent terminals
      SELECT ended_at, 'terminal', SUBSTRING(agent_id::text, 1, 8),
             jsonb_build_object(
               'status',          status,
               'pr_number',       pr_number,
               'failure_summary', CASE WHEN failure_summary IS NULL THEN NULL
                                       WHEN length(failure_summary) > 160
                                         THEN regexp_replace(substring(failure_summary, 1, 157), E'\\\\s+', ' ', 'g') || '…'
                                       ELSE regexp_replace(failure_summary, E'\\\\s+', ' ', 'g') END,
               'lifetime',        to_char(ended_at - started_at, 'FMHH24:MI:SS')
             )
        FROM this_issue WHERE ended_at IS NOT NULL

      UNION ALL
      -- merge milestone
      SELECT merged_at, 'merged', SUBSTRING(agent_id::text, 1, 8),
             jsonb_build_object('pr_number', pr_number)
        FROM this_issue WHERE merged_at IS NOT NULL

      UNION ALL
      -- verify milestone
      SELECT verified_at, 'verified', SUBSTRING(agent_id::text, 1, 8),
             jsonb_build_object('verify_skip_reason', verify_skip_reason)
        FROM this_issue WHERE verified_at IS NOT NULL

      UNION ALL
      -- retro milestone
      SELECT retroed_at, 'retroed', SUBSTRING(agent_id::text, 1, 8), '{}'::jsonb
        FROM this_issue WHERE retroed_at IS NOT NULL

      UNION ALL
      -- phase transitions, with per-agent duration
      SELECT pt.ts, 'phase', SUBSTRING(pt.agent_id::text, 1, 8),
             jsonb_build_object(
               'phase',             pt.phase,
               'phase_duration',    to_char(
                 pt.ts - COALESCE(
                   LAG(pt.ts) OVER (PARTITION BY pt.agent_id ORDER BY pt.ts),
                   a.started_at
                 ),
                 'FMHH24:MI:SS'
               ),
               'autocompact_count', pt.autocompact_count
             )
        FROM dispatcher.phase_transitions pt
        JOIN this_issue a ON a.agent_id = pt.agent_id

      UNION ALL
      -- failures
      SELECT f.ts, 'failure', SUBSTRING(f.agent_id::text, 1, 8),
             jsonb_build_object(
               'failure_id',  f.failure_id,
               'category',    f.category,
               'detected_by', f.detected_by,
               'details',     ${failure_details_sql}
             )
        FROM dispatcher.failures f
        JOIN this_issue a ON a.agent_id = f.agent_id

      UNION ALL
      -- diagnosis started
      SELECT d.started_at, 'diagnose_start', SUBSTRING(d.agent_id::text, 1, 8),
             jsonb_build_object(
               'diagnosis_id', d.diagnosis_id,
               'failure_id',   d.failure_id
             )
        FROM dispatcher.diagnoses d
        JOIN this_issue a ON a.agent_id = d.agent_id

      UNION ALL
      -- diagnosis completed
      SELECT d.completed_at, 'diagnose_done', SUBSTRING(d.agent_id::text, 1, 8),
             jsonb_build_object(
               'diagnosis_id', d.diagnosis_id,
               'duration',     to_char(d.completed_at - d.started_at, 'FMHH24:MI:SS'),
               'status',       d.status,
               'action',       d.recommendation->>'action',
               'reasoning',    ${diagnose_reasoning_sql},
               'outcome',      ${diagnose_outcome_sql}
             )
        FROM dispatcher.diagnoses d
        JOIN this_issue a ON a.agent_id = d.agent_id
       WHERE d.completed_at IS NOT NULL

      UNION ALL
      -- retry marker scheduled
      SELECT rm.retry_after_ts, 'retry_scheduled', SUBSTRING(rm.agent_id::text, 1, 8),
             jsonb_build_object(
               'marker_id', rm.marker_id,
               'attempt',   rm.attempt,
               'reason',    rm.reason
             )
        FROM dispatcher.retry_markers rm
        JOIN this_issue a ON a.agent_id = rm.agent_id

      UNION ALL
      -- retry marker resolved
      SELECT rm.resolved_at, 'retry_resolved', SUBSTRING(rm.agent_id::text, 1, 8),
             jsonb_build_object(
               'marker_id', rm.marker_id,
               'attempt',   rm.attempt,
               'reason',    rm.reason
             )
        FROM dispatcher.retry_markers rm
        JOIN this_issue a ON a.agent_id = rm.agent_id
       WHERE rm.resolved_at IS NOT NULL

      UNION ALL
      -- notifications targeting the issue (issue-level — agent_id NULL)
      SELECT n.created_at, 'notification', NULL::text,
             jsonb_build_object(
               'notification_id', n.notification_id,
               'kind',            n.kind,
               'severity',        n.severity,
               'pr_number',       n.pr_number,
               'sent_at',         to_char(n.sent_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),
               'send_error',      n.send_error
             )
        FROM dispatcher.notifications n
       WHERE n.issue_number = ${issue}
    ) all_events
  ),
  ordered AS (
    -- Sort + project in one go; ts is dropped from the projection so
    -- each page stays small (the dev-db-query.sh ECS-exec stream
    -- truncates around ~10 KB on large payloads — same hazard
    -- fleet-status.sh notes). Pagination wraps this in the bash
    -- loop below.
    SELECT
      jsonb_build_object(
        'ts_utc',   to_char(ts AT TIME ZONE 'UTC',               'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),
        'ts_pt',    to_char(ts AT TIME ZONE 'America/Los_Angeles', 'HH24:MI:SS\" PT\"'),
        'kind',     kind,
        'agent_id', agent_id,
        'payload',  payload
      ) AS row
    FROM events
    ORDER BY ts, kind
    LIMIT __LIMIT__ OFFSET __OFFSET__
  )
SELECT COALESCE(jsonb_agg(row), '[]'::jsonb) AS page FROM ordered;
"

run_query() {
    # Back-to-back ECS-exec invocations occasionally return truncated
    # output ('Cannot perform start session: EOF' mid-stream). The JSON
    # comes through either well-formed or clearly chopped — jq's parser
    # is a reliable integrity check, so retry up to 3 times when it
    # rejects the response. Between attempts: a short sleep to let the
    # SSM session machinery settle.
    local label="$1" sql="$2"
    if [[ "${ISSUE_TIMELINE_DEBUG_SQL:-0}" == "1" ]]; then
        echo "===== SQL ($label) =====" >&2
        echo "$sql" >&2
        echo "===== END SQL ($label) =====" >&2
    fi
    local attempt out
    for attempt in 1 2 3; do
        out=$("$dev_db" "$sql" 2>/dev/null | json_only)
        if [[ -n "$out" ]] && printf '%s' "$out" | jq -e . >/dev/null 2>&1; then
            printf '%s' "$out"
            return 0
        fi
        if (( attempt < 3 )); then
            echo "Warning: $label query returned malformed JSON on attempt $attempt — retrying" >&2
            sleep 2
        fi
    done
    echo "Error: $label query returned malformed JSON after 3 attempts" >&2
    exit 2
}

header_result=$(run_query header "$header_sql")
header=$(echo "$header_result" | jq '.[0].header')

agent_count=$(echo "$header" | jq -r '.summary.agent_count // 0')
if [[ "$agent_count" == "0" ]]; then
    echo "No dispatcher agents found for issue #${issue}." >&2
    exit 1
fi

# Paginate the timeline. PAGE_SIZE=20 keeps each response under the
# ~10 KB SSM stream cap even with the chattiest event types (failure
# rows with details, diagnose_done with reasoning when --full is on).
# Stop when a page returns fewer rows than the page size — Postgres
# already orders deterministically by (ts, kind).
page_size="${ISSUE_TIMELINE_PAGE_SIZE:-20}"
offset=0
timeline="[]"
while :; do
    page_sql="${timeline_sql_template//__LIMIT__/$page_size}"
    page_sql="${page_sql//__OFFSET__/$offset}"
    page_result=$(run_query "timeline page (offset=$offset)" "$page_sql")
    page=$(echo "$page_result" | jq '.[0].page')
    page_len=$(echo "$page" | jq 'length')
    timeline=$(jq -n --argjson a "$timeline" --argjson b "$page" '$a + $b')
    if (( page_len < page_size )); then
        break
    fi
    offset=$(( offset + page_size ))
done

bundle=$(jq -n --argjson h "$header" --argjson tl "$timeline" '$h + {timeline: $tl}')

# ─── Format and print ────────────────────────────────────────────────

echo "$bundle" | jq -r '
  . as $root
  | $root.summary as $s
  | $root.agents  as $agents
  | $root.timeline as $tl

  | (
      ["── issue #\($s.issue_number) ──"]
      + (if ($s.titles // [] | length) > 0
           then ["  title:    \($s.titles | join(" / "))"]
           else [] end)
      + (
          ($s.status_counts // {} | to_entries | map("\(.value) \(.key)") | join(", ")) as $sc
          | ["  agents:   \($s.agent_count)\(if $sc != "" then "  (\($sc))" else "" end)\(if ($s.still_running // 0) > 0 then "  [\($s.still_running) still running]" else "" end)"]
        )
      + (if ($s.priorities // [] | length) > 0
           then ["  priority: \($s.priorities | join(", "))"]
           else [] end)
      + (if $s.merged_pr
           then ["  PR:       #\($s.merged_pr)  (merged \($s.merged_utc))"]
         elif ($s.pr_numbers // [] | length) > 0
           then ["  PR:       \($s.pr_numbers | map("#\(.)") | join(", "))  (not merged)"]
           else ["  PR:       (none)"] end)
      + ["  first:    \($s.first_spawn_utc)  (\($s.first_spawn_pt))",
         "  last:     \($s.last_event_utc)  (\($s.last_event_pt))",
         "  span:     \($s.span)",
         "",
         "── agents ──"]
      + ($agents | map(
          "  \(.agent_id[:8])  spawned \(.spawned_pt)  " +
          (if .ended_pt then "ended \(.ended_pt)  " else "(running)             " end) +
          "\(.status)/\(.phase)  lifetime=\(.lifetime)" +
          (if .pr_number then "  PR #\(.pr_number)" else "" end) +
          (if .priority then "  \(.priority)" else "" end) +
          (if .kind != "task" then "  kind=\(.kind)" else "" end) +
          (if .model_override then "  model_override=\(.model_override | tostring)" else "" end) +
          (
            (if .merged   then ["merged"]   else [] end)
            + (if .verified then ["verified"] else [] end)
            + (if .retroed  then ["retroed"]  else [] end)
          ) as $milestones
          | (if ($milestones | length) > 0 then "  [\($milestones | join("+"))]" else "" end) +
          (if .verify_skip_reason then "  verify_skip=\(.verify_skip_reason)" else "" end) +
          (if .failure_summary then "\n      summary: \(.failure_summary)" else "" end)
        ))
      + ["",
         "── timeline ──"]
      + (if ($tl | length) == 0
           then ["  (no events)"]
           else ($tl | map(
             # Each event line: "<UTC> (<PT>)  <agent>  <kind>  <details>"
             # agent column is the short agent_id, or "--------" for
             # issue-level rows (notifications) so columns line up.
             "  \(.ts_utc)  (\(.ts_pt))  \(if .agent_id then .agent_id[:8] else "--------" end)  " +
             (
               if .kind == "spawn"           then "spawn         phase=\(.payload.phase)\(if .payload.priority then "  \(.payload.priority)" else "" end)\(if .payload.kind_field and .payload.kind_field != "task" then "  kind=\(.payload.kind_field)" else "" end)\(if .payload.model_override then "  model_override=\(.payload.model_override | tostring)" else "" end)"
               elif .kind == "phase"         then "phase end     \(.payload.phase)  ran [\(.payload.phase_duration)]\(if .payload.autocompact_count and .payload.autocompact_count > 0 then "  ac=\(.payload.autocompact_count)" else "" end)"
               elif .kind == "failure"       then "failure       cat=\(.payload.category)  detected_by=\(.payload.detected_by)  fid=\(.payload.failure_id)" +
                                                  (if .payload.details and (.payload.details | tostring) != "{}" then "\n      details: \(.payload.details | tojson)" else "" end)
               elif .kind == "diagnose_start" then "diagnose      start  did=\(.payload.diagnosis_id)\(if .payload.failure_id then "  fid=\(.payload.failure_id)" else "" end)"
               elif .kind == "diagnose_done"  then "diagnose      done   did=\(.payload.diagnosis_id)\(if .payload.duration then "  duration=\(.payload.duration)" else "" end)  action=\(.payload.action // "(null)")" +
                                                  (if .payload.reasoning then "\n      reasoning: \(.payload.reasoning | gsub("\n"; " "))" else "" end) +
                                                  (if .payload.outcome and (.payload.outcome | tostring) != "null" then "\n      outcome: \(.payload.outcome | tojson)" else "" end)
               elif .kind == "retry_scheduled" then "retry         scheduled  marker=\(.payload.marker_id)  attempt=\(.payload.attempt)  reason=\(.payload.reason)"
               elif .kind == "retry_resolved"  then "retry         resolved   marker=\(.payload.marker_id)  attempt=\(.payload.attempt)"
               elif .kind == "merged"        then "milestone     merged    PR #\(.payload.pr_number)"
               elif .kind == "verified"      then "milestone     verified\(if .payload.verify_skip_reason then "  skip=\(.payload.verify_skip_reason)" else "" end)"
               elif .kind == "retroed"       then "milestone     retroed"
               elif .kind == "terminal"      then "terminal      \(.payload.status)  lifetime=\(.payload.lifetime)\(if .payload.pr_number then "  PR #\(.payload.pr_number)" else "" end)" +
                                                  (if .payload.failure_summary then "\n      summary: \(.payload.failure_summary)" else "" end)
               elif .kind == "notification"  then "notification  kind=\(.payload.kind)  sev=\(.payload.severity)\(if .payload.pr_number then "  PR #\(.payload.pr_number)" else "" end)\(if .payload.sent_at then "  sent=\(.payload.sent_at)" else "  (unsent)" end)\(if .payload.send_error then "  err=\(.payload.send_error)" else "" end)"
               else                              "\(.kind)  \(.payload | tojson)"
               end
             )
           ))
         end)
    )
  | .[]
'
