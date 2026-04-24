#!/usr/bin/env bash
# venv: N/A — pure bash + scripts/dev-db-query.sh + jq + aws cli
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
# For ECS-mode agents (execution_mode='ecs'), also prints two extra
# sections (#3147):
#   * ECS agent-runner — task ARN + copy-pasteable ecs:execute-command
#     one-liner + CPU/memory averages from Container Insights for the
#     agent-runner TaskDefinitionFamily over the agent's lifetime window.
#     Family-level metrics are the finest granularity under the
#     `enabled` Container Insights tier (per-TaskId requires
#     `enhanced`, which is not live). See #3146 for the tier/doc
#     trail and `docs/agent/infrastructure-reference.md` §Container
#     Insights for the query shape.
#   * Ralph iterations — one row per ralph_patches entry for this agent,
#     showing iteration_n, created_at, commit_sha, verdict, and the
#     parsed Subject line from git format-patch content.
#
# Subprocess-mode agents render unchanged — the ECS sections are skipped.
#
# Usage:
#   scripts/dispatcher/helpers/agent-timeline.sh <agent-id-or-prefix>
#
# Example:
#   scripts/dispatcher/helpers/agent-timeline.sh 4b86287b
#
# The prefix only needs to be unique among the most recent agents; if it
# matches multiple rows the script prints candidates and exits non-zero so
# you can disambiguate.
#
# Graceful degradation:
#   * Missing `aws` CLI, failing `get-metric-data` call, or empty metric
#     response → the CPU/memory lines show "(metrics unavailable)".
#   * NULL task ARN (e.g. ECS agent killed before RunTask) → the
#     exec-cmd line shows "(no task ARN recorded)".
#   * No ralph_patches rows yet → the iterations section shows
#     "(no iterations yet)".
# None of these produce a hard failure — the helper's primary purpose
# is to render the DB-backed timeline, and the new sections are
# supplemental.
#
# Environment overrides (all optional — the defaults match dev):
#   ECS_CLUSTER        — cluster name (default: judgemind-dev).
#   ECS_AGENT_FAMILY   — agent-runner TaskDefinitionFamily
#                        (default: judgemind-dispatcher-agent-runner-dev).
#   AWS_REGION         — region passed to `aws cloudwatch`
#                        (default: us-west-2).
#   AGENT_TIMELINE_SKIP_METRICS — if non-empty, skip the CloudWatch
#                        call entirely and show "(metrics unavailable)".
#                        Used by tests.
#
# Exit codes:
#   0  — printed timeline for exactly one matching agent.
#   1  — usage error, no match, or ambiguous prefix.
#   2  — database error or dev-db-query.sh failure.

set -euo pipefail

usage() {
    echo "Usage: scripts/dispatcher/helpers/agent-timeline.sh <agent-id-or-prefix>" >&2
    echo "Example: scripts/dispatcher/helpers/agent-timeline.sh 4b86287b" >&2
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
dev_db="$script_dir/../../dev-db-query.sh"

if [[ ! -x "$dev_db" ]]; then
    echo "Error: $dev_db not found or not executable" >&2
    exit 2
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "Error: jq is required (brew install jq)" >&2
    exit 2
fi

# shellcheck source=./_query_lib.sh
. "$script_dir/_query_lib.sh"

# ─── Resolve prefix to a unique agent_id ─────────────────────────────

resolve_sql="SELECT agent_id::text AS id, issue_number, phase, status FROM dispatcher.agents WHERE agent_id::text LIKE '${prefix}%' ORDER BY started_at DESC LIMIT 5;"

# _query_lib_run validates + retries on transient ECS-exec stream failures
# — replaces the older inline awk|jq pipeline that occasionally emitted a
# bare `parse error: Invalid numeric literal` when the SSM stream
# delivered a partial payload. See #3124.
resolve_json=$(_query_lib_run "$resolve_sql" "agent-timeline resolve") || exit $?

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
      'execution_mode', execution_mode,
      'agent_task_arn', agent_task_arn,
      'ralph_iterations_observed', ralph_iterations_observed,
      'started_epoch', EXTRACT(EPOCH FROM started_at)::bigint,
      'ended_epoch',   CASE WHEN ended_at IS NULL THEN NULL
                             ELSE EXTRACT(EPOCH FROM ended_at)::bigint END,
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
  ),
  ralph_iterations AS (
    -- #3147: one row per ralph_patches entry for this agent. The ECS
    -- agent-runner HEAD-watcher (#3144) writes per-iteration rows with
    -- iteration_n + verdict='LOOP'; ralph SHIP adds a row with
    -- verdict='SHIP' and iteration_n=NULL. Legacy pre-#3026 rows have
    -- both columns NULL. The parsed Subject line comes from
    -- ``git format-patch`` output at the head of patch_content — it's
    -- the human-readable commit subject for the iteration's cumulative
    -- work. We use substring(... FROM '...') to pull just that one
    -- line, guarding against NULL patch_content with NULLIF so the
    -- helper still shows the row even if the subject extraction
    -- misses.
    SELECT COALESCE(jsonb_agg(i), '[]'::jsonb) AS data FROM (
      SELECT jsonb_build_object(
        'iteration_n',     iteration_n,
        'verdict',         verdict,
        'commit_sha',      commit_sha,
        'commit_sha_short', CASE WHEN commit_sha IS NULL THEN NULL
                                  ELSE substring(commit_sha, 1, 7) END,
        'commit_subject',  NULLIF(
                             substring(patch_content FROM 'Subject: \[PATCH\] ([^\n\r]+)'),
                             ''),
        'created_utc',     to_char(created_at AT TIME ZONE 'UTC',               'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),
        'created_pt',      to_char(created_at AT TIME ZONE 'America/Los_Angeles', 'HH24:MI:SS\" PT\"')
      ) AS i
      FROM dispatcher.ralph_patches
      WHERE agent_id = '${agent_id}'
      ORDER BY created_at, iteration_n NULLS LAST
    ) x
  )
SELECT jsonb_build_object(
  'agent',            (SELECT data FROM agent),
  'transitions',      (SELECT data FROM transitions),
  'failures',         (SELECT data FROM failures),
  'diagnoses',        (SELECT data FROM diagnoses),
  'retry_markers',    (SELECT data FROM retry_markers),
  'ralph_iterations', (SELECT data FROM ralph_iterations)
) AS timeline;
"

timeline_result=$(_query_lib_run "$timeline_sql" "agent-timeline fetch") || exit $?

if [[ -z "$timeline_result" ]]; then
    echo "Error: timeline query returned no result" >&2
    exit 2
fi

# dev-db-query.sh returns an array-of-one-row; pull out the single `timeline` object.
timeline=$(echo "$timeline_result" | jq '.[0].timeline')

# ─── ECS-mode only: fetch CPU/memory from Container Insights ─────────
# Only runs when execution_mode='ecs'. We call `aws cloudwatch
# get-metric-data` once each for CpuUtilized and MemoryUtilized at the
# TaskDefinitionFamily granularity (per-TaskId requires the
# `enhanced` tier, see #3146). The time window is bounded by the
# agent's started_at / ended_at (or `now` if still running). Failures
# are non-fatal — we inject `null` into the timeline JSON and the jq
# output downgrades to "(metrics unavailable)".
#
# bash 3.2 compat: no process-substitution-into-mapfile, no namerefs,
# no `declare -A`. We construct JSON via jq and pass it through a
# simple variable.

execution_mode=$(echo "$timeline" | jq -r '.agent.execution_mode // "subprocess"')
ecs_cluster="${ECS_CLUSTER:-judgemind-dev}"
ecs_family="${ECS_AGENT_FAMILY:-judgemind-dispatcher-agent-runner-dev}"
aws_region="${AWS_REGION:-us-west-2}"

ecs_metrics_json="null"

if [[ "$execution_mode" == "ecs" && -z "${AGENT_TIMELINE_SKIP_METRICS:-}" ]]; then
    if command -v aws >/dev/null 2>&1; then
        started_epoch=$(echo "$timeline" | jq -r '.agent.started_epoch // empty')
        ended_epoch=$(echo "$timeline" | jq -r '.agent.ended_epoch // empty')

        if [[ -n "$started_epoch" ]]; then
            # Default end = now. Subtract a small cushion (120s) from
            # the start so that agents whose first metric datapoint
            # arrives up to 2 minutes after started_at still match.
            start_ts="$started_epoch"
            if [[ -n "$ended_epoch" ]]; then
                end_ts="$ended_epoch"
            else
                end_ts=$(date -u +%s)
            fi
            # Widen the start slightly so the first minute bucket is
            # included (CloudWatch Period=60 aligns on wall-clock
            # minutes).
            start_ts=$((start_ts - 60))
            end_ts=$((end_ts + 60))

            # ISO 8601 with Z. BSD date (macOS): `date -u -r N +FMT`.
            # GNU date (Linux / Fargate agent-runner): `date -u -d @N
            # +FMT`. Detect once and use the right form.
            if date -u -r "$start_ts" +%FT%TZ >/dev/null 2>&1; then
                # BSD date (macOS)
                start_iso=$(date -u -r "$start_ts" +%FT%TZ)
                end_iso=$(date -u -r "$end_ts" +%FT%TZ)
            else
                # GNU date (Linux / Fargate)
                start_iso=$(date -u -d "@$start_ts" +%FT%TZ)
                end_iso=$(date -u -d "@$end_ts" +%FT%TZ)
            fi

            # Build one metric-data-query JSON per metric. We keep both
            # in a single call for efficiency.
            mdq=$(jq -cn \
                --arg family "$ecs_family" \
                --arg cluster "$ecs_cluster" \
                '[
                  {
                    Id: "cpu",
                    MetricStat: {
                      Metric: {
                        Namespace: "ECS/ContainerInsights",
                        MetricName: "CpuUtilized",
                        Dimensions: [
                          {Name: "TaskDefinitionFamily", Value: $family},
                          {Name: "ClusterName", Value: $cluster}
                        ]
                      },
                      Period: 60,
                      Stat: "Average"
                    },
                    ReturnData: true
                  },
                  {
                    Id: "mem",
                    MetricStat: {
                      Metric: {
                        Namespace: "ECS/ContainerInsights",
                        MetricName: "MemoryUtilized",
                        Dimensions: [
                          {Name: "TaskDefinitionFamily", Value: $family},
                          {Name: "ClusterName", Value: $cluster}
                        ]
                      },
                      Period: 60,
                      Stat: "Average"
                    },
                    ReturnData: true
                  }
                ]')

            cw_out=$(aws cloudwatch get-metric-data \
                --region "$aws_region" \
                --start-time "$start_iso" \
                --end-time "$end_iso" \
                --metric-data-queries "$mdq" 2>/dev/null || true)

            if [[ -n "$cw_out" ]] && printf '%s' "$cw_out" | jq -e '.MetricDataResults' >/dev/null 2>&1; then
                # Average the Values[] arrays. If both are empty, the
                # reducer below emits null and the jq render falls
                # back to "(metrics unavailable)".
                ecs_metrics_json=$(printf '%s' "$cw_out" | jq -c '
                    .MetricDataResults as $r
                    | (($r[] | select(.Id == "cpu") | .Values) // []) as $cpu
                    | (($r[] | select(.Id == "mem") | .Values) // []) as $mem
                    | {
                        cpu_avg: (if ($cpu | length) > 0 then ($cpu | add / length) else null end),
                        cpu_points: ($cpu | length),
                        mem_avg: (if ($mem | length) > 0 then ($mem | add / length) else null end),
                        mem_points: ($mem | length),
                        window_start: "'"$start_iso"'",
                        window_end:   "'"$end_iso"'",
                        family: "'"$ecs_family"'",
                        cluster: "'"$ecs_cluster"'"
                      }
                ' 2>/dev/null || echo "null")
            fi
        fi
    fi
fi

# Splice metrics into the timeline JSON so the jq render can see both.
timeline=$(echo "$timeline" | jq --argjson m "$ecs_metrics_json" \
    '. + {ecs_metrics: $m}')

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
    ) + (
      # ─── #3147: ECS agent-runner block ────────────────────────────
      # Only rendered when execution_mode='ecs'. Subprocess-mode
      # agents are unchanged — no new sections emitted.
      if ($a.execution_mode // "subprocess") != "ecs"
        then []
        else
          ["", "── ECS agent-runner ──"] +
          (if ($a.agent_task_arn // null) != null
            then
              ($a.agent_task_arn | split("/")) as $parts
              | ($parts[-1] // "?") as $task_id
              | [
                  "  task_arn: \($a.agent_task_arn)",
                  "  task_id:  \($task_id)",
                  "  exec-cmd: aws ecs execute-command --cluster \(($root.ecs_metrics.cluster // "judgemind-dev")) --task \($task_id) --container agent-runner --interactive --command /bin/bash"
                ]
            else ["  task_arn: (no task ARN recorded)"]
          end) +
          (if ($root.ecs_metrics // null) == null or ($root.ecs_metrics.cpu_avg // null) == null
            then ["  task CPU: (metrics unavailable)"]
            else ["  task CPU: \(($root.ecs_metrics.cpu_avg * 10 | round) / 10) units (family avg over \(($root.ecs_metrics.cpu_points)) datapoint\(if $root.ecs_metrics.cpu_points == 1 then "" else "s" end), family=\($root.ecs_metrics.family))"]
          end) +
          (if ($root.ecs_metrics // null) == null or ($root.ecs_metrics.mem_avg // null) == null
            then ["  task mem: (metrics unavailable)"]
            else ["  task mem: \(($root.ecs_metrics.mem_avg * 10 | round) / 10) MiB (family avg over \(($root.ecs_metrics.mem_points)) datapoint\(if $root.ecs_metrics.mem_points == 1 then "" else "s" end))"]
          end) +
          ["  iterations_observed: \($a.ralph_iterations_observed // 0)"]
      end
    ) + (
      # ─── #3147: Ralph iterations block ────────────────────────────
      # Only rendered when execution_mode='ecs'. The rows come from
      # dispatcher.ralph_patches — the HEAD-watcher writes one per
      # iteration plus a SHIP row (iteration_n=NULL, verdict='SHIP').
      if ($a.execution_mode // "subprocess") != "ecs"
        then []
        else
          ["", "── Ralph iterations (ECS, in-flight or terminal) ──"] +
          (if ($root.ralph_iterations // [] | length) == 0
            then ["  (no iterations yet)"]
            else ($root.ralph_iterations | map(
              "  iter=\(.iteration_n // "·")  verdict=\(.verdict // "·")  \(.created_utc)  \(.commit_sha_short // "·······")\(if .commit_subject then "  \(.commit_subject)" else "" end)"
            ))
          end)
      end
    )
  | .[]
'
