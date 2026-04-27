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
#   scripts/dispatcher/helpers/fleet-status.sh --no-cooldown  # skip cooldown / stuck-loop watch section
#
# Defaults: --since 1h, --terminals 10, --greens 5, cooldown section enabled.
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
cooldown_enabled=1

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
        --no-cooldown)
            cooldown_enabled=0
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

# Convert the --since token to a Postgres interval expression.
since_interval=""
if [[ "$since" == *h ]]; then
    since_interval="make_interval(hours => ${since%h})"
else
    since_interval="make_interval(mins => ${since%m})"
fi

# ─── Seven small queries, assembled locally ─────────────────────────────
# Previously this helper bundled the whole fleet snapshot into a single
# WITH-CTE query that emitted one `jsonb_build_object` row. That worked
# when the dispatcher.agents / diagnoses tables were small, but once the
# combined JSON grew past ~1 KB the SSM / ECS-exec transport started
# truncating the stream mid-field and jq parse-errored even with retry
# (#3195).  Rather than fight SSM's per-session output budget, we now run
# seven small queries — each returns a tiny JSON payload that stays well
# inside the transport's limits — and assemble the snapshot locally with
# jq. Total round-trip time is 6–10 s (vs. 2–3 s for the bundled query)
# but success rate is dramatically higher.
#
# Drill into details with agent-timeline.sh / diagnoses.sh / breaker.sh —
# the per-CTE output caps here are intentionally conservative.

q_active="
SELECT
    agent_id::text AS agent_id,
    issue_number,
    phase,
    status,
    priority,
    to_char(started_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS started_utc,
    to_char(started_at AT TIME ZONE 'America/Los_Angeles', 'HH24:MI:SS\" PT\"') AS started_pt,
    to_char(now() - started_at, 'FMHH24:MI:SS') AS lifetime
FROM dispatcher.agents
WHERE status IN ('running', 'claiming')
ORDER BY started_at
"

# Terminals cap column length tight to reduce bytes — failure_summary is
# the biggest byte-consumer in the fleet and occasionally blows past 140
# chars even after substring. Cap at 80 chars for display; detail lives in
# agent-timeline.sh.
q_terminals="
SELECT
    agent_id::text AS agent_id,
    issue_number,
    phase,
    status,
    pr_number,
    CASE WHEN failure_summary IS NULL THEN NULL
         WHEN length(failure_summary) > 80
           THEN regexp_replace(substring(failure_summary, 1, 77), E'\\\\s+', ' ', 'g') || '…'
         ELSE regexp_replace(failure_summary, E'\\\\s+', ' ', 'g') END AS failure_summary,
    to_char(ended_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS ended_utc,
    to_char(ended_at AT TIME ZONE 'America/Los_Angeles', 'HH24:MI:SS\" PT\"') AS ended_pt,
    to_char(ended_at - started_at, 'FMHH24:MI:SS') AS lifetime
FROM dispatcher.agents
WHERE ended_at IS NOT NULL
  AND ended_at > now() - ${since_interval}
ORDER BY ended_at DESC
LIMIT ${terminals_cap}
"

q_config="
SELECT
    (SELECT value::int FROM dispatcher.config WHERE key='concurrency_cap') AS cap,
    (SELECT value::text FROM dispatcher.config WHERE key='cap_flipped_by') AS flipped_by
"

q_diagnoses="
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
"

# Truncate long issue titles so greens fit in SSM's budget even when a
# spike of recent merges dumps long-titled PRs through the window.
q_greens="
SELECT
    agent_id::text AS agent_id,
    issue_number,
    pr_number,
    to_char(merged_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS merged_utc,
    to_char(merged_at AT TIME ZONE 'America/Los_Angeles', 'HH24:MI:SS\" PT\"') AS merged_pt,
    to_char(merged_at - started_at, 'FMHH24:MI:SS') AS lifetime,
    CASE WHEN issue_title IS NULL THEN NULL
         WHEN length(issue_title) > 100
           THEN substring(issue_title, 1, 97) || '…'
         ELSE issue_title END AS issue_title
FROM dispatcher.agents
WHERE merged_at IS NOT NULL
  AND merged_at > now() - ${since_interval}
ORDER BY merged_at DESC
LIMIT ${greens_cap}
"

q_pending="
SELECT
    command_id,
    command,
    issued_by,
    to_char(issued_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS issued_utc,
    payload
FROM dispatcher.commands
WHERE consumed_at IS NULL
ORDER BY issued_at
"

q_totals="
SELECT
    (SELECT COUNT(*) FROM dispatcher.agents
       WHERE ended_at IS NOT NULL AND ended_at > now() - ${since_interval}) AS total_terminals_in_window,
    (SELECT COUNT(*) FROM dispatcher.agents
       WHERE merged_at IS NOT NULL AND merged_at > now() - ${since_interval}) AS total_greens_in_window,
    (SELECT COUNT(*) FROM dispatcher.diagnoses
       WHERE started_at > now() - ${since_interval}) AS total_diagnoses_in_window
"

# Cooldown / stuck-loop watch — surfaces issues currently on cooldown so the
# operator can spot retry storms at a glance. Hard-codes 3600 s matching
# daemon.py:885 FAILED_AGENT_COOLDOWN_SECONDS. Orders latest-agent-per-issue
# by started_at DESC (not ended_at DESC) so zombie rows (ended_at IS NULL)
# surface as the representative row per memory feedback_zombie_rows_started_at_not_ended_at.md.
# Phase group mirrors daemon.py:8699: split('-',1)[0].
q_cooldown="
WITH cooldown_issues AS (
    SELECT DISTINCT issue_number
    FROM dispatcher.agents
    WHERE dispatcher.issue_cooldown_remaining_seconds(issue_number, 3600) > 0
),
latest_agent AS (
    SELECT DISTINCT ON (a.issue_number)
        a.issue_number,
        a.priority,
        a.phase,
        a.status,
        a.started_at,
        a.ended_at,
        (a.ended_at IS NULL) AS is_zombie
    FROM dispatcher.agents a
    JOIN cooldown_issues ci ON ci.issue_number = a.issue_number
    ORDER BY a.issue_number, a.started_at DESC
),
attempt_counts AS (
    SELECT
        a.issue_number,
        split_part(a.phase, '-', 1) AS phase_group,
        COUNT(*) AS attempts_24h
    FROM dispatcher.agents a
    JOIN cooldown_issues ci ON ci.issue_number = a.issue_number
    WHERE a.started_at > now() - make_interval(hours => 24)
    GROUP BY a.issue_number, split_part(a.phase, '-', 1)
),
phase_group_for_latest AS (
    SELECT
        la.issue_number,
        split_part(la.phase, '-', 1) AS phase_group
    FROM latest_agent la
),
counts_for_latest AS (
    SELECT
        pg.issue_number,
        COALESCE(ac.attempts_24h, 0) AS attempts_24h
    FROM phase_group_for_latest pg
    LEFT JOIN attempt_counts ac
        ON ac.issue_number = pg.issue_number
        AND ac.phase_group = pg.phase_group
)
SELECT
    la.issue_number,
    la.priority,
    dispatcher.issue_cooldown_remaining_seconds(la.issue_number, 3600) AS cooldown_left_seconds,
    la.phase AS last_phase,
    la.status AS last_status,
    la.is_zombie,
    COALESCE(cfl.attempts_24h, 0) AS attempts_24h
FROM latest_agent la
LEFT JOIN counts_for_latest cfl ON cfl.issue_number = la.issue_number
ORDER BY cooldown_left_seconds ASC
"

if [[ "${FLEET_STATUS_DEBUG:-0}" == "1" ]]; then
    echo "===== active =====" >&2
    echo "$q_active" >&2
    echo "===== terminals =====" >&2
    echo "$q_terminals" >&2
fi

# Each query returns a JSON array.  For single-row queries (config,
# totals) we take .[0]; for multi-row we keep the array.
active_json=$(query "$q_active")
terminals_json=$(query "$q_terminals")
config_json=$(query "$q_config")
diagnoses_json=$(query "$q_diagnoses")
greens_json=$(query "$q_greens")
pending_json=$(query "$q_pending")
totals_json=$(query "$q_totals")

# Cooldown section is optional — failures emit a WARNING and fall back to
# an empty array so the rest of the snapshot still renders.
cooldown_json="[]"
if [[ "$cooldown_enabled" == "1" ]]; then
    cooldown_json=$(query "$q_cooldown" || true)
    if ! printf '%s' "$cooldown_json" | jq -e . >/dev/null 2>&1; then
        echo "WARNING: fleet-status: cooldown query failed or returned malformed JSON — skipping section" >&2
        cooldown_json="[]"
    fi
fi

# Assemble the snapshot shape the formatter expects. Defensive defaults:
# empty arrays for missing collections and empty objects for missing
# singletons, so a transient DB hiccup on one sub-query doesn't crash the
# whole formatter. (That's also what the retry loop protects against — the
# defaults are belt-and-suspenders.)
snapshot=$(jq -n \
    --argjson active "$active_json" \
    --argjson terminals "$terminals_json" \
    --argjson config "$config_json" \
    --argjson diagnoses "$diagnoses_json" \
    --argjson greens "$greens_json" \
    --argjson pending "$pending_json" \
    --argjson totals "$totals_json" \
    --argjson cooldown "$cooldown_json" \
    --arg since "$since" \
    --argjson cooldown_enabled "$cooldown_enabled" \
    '{
        since: $since,
        active: $active,
        terminals: $terminals,
        config: ($config[0] // {}),
        diagnoses_recent: $diagnoses,
        greens: $greens,
        pending_commands: $pending,
        totals: ($totals[0] // {}),
        cooldown: $cooldown,
        cooldown_enabled: $cooldown_enabled
    }')

# ─── Format ─────────────────────────────────────────────────────────────

echo "$snapshot" | jq -r '
  # Cooldown classifier — mirrors daemon.py:8699 phase_group split.
  # Returns a classifier string for one cooldown row.
  def classify_cooldown:
    if (.attempts_24h >= 3) and (.last_status == "failed")
      then "🔁 stuck-in-loop (\(.attempts_24h)x \(.last_phase | split("-")[0]))"
    elif (.attempts_24h >= 2)
      then "⚠ retry pattern"
    elif (.attempts_24h == 1) and (.last_status == "succeeded" or .last_status == "needs_review")
      then "✓ recently terminal"
    else "? unclassified"
    end;
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
    + [""]
    + (if $s.cooldown_enabled == 1
         then
           ["── cooldown / stuck-loop watch ──"]
           + (if ($s.cooldown | length) == 0
                then ["  ✓ no cooldown / stuck loops"]
                else
                  ($s.cooldown | map(
                    "  \(classify_cooldown)  #\(.issue_number)  cooldown=\((.cooldown_left_seconds / 60 | floor | tostring) + "m")  last=\(.last_phase)/\(.last_status)\(if .is_zombie then " (zombie)" else "" end)"
                    + (if .priority then "  \(.priority)" else "" end)
                  ))
                  + (
                    # Summary line
                    [
                      "  Total: \($s.cooldown | length) cooldown, \($s.cooldown | map(select(.is_zombie)) | length) zombie rows, \($s.cooldown | map(select(.last_status == "running" or .last_status == "claiming")) | length) running, \($s.cooldown | map(select(.last_status == "succeeded")) | length) succeeded"
                    ]
                    + (if ($s.cooldown | map(select((.attempts_24h >= 3) and (.last_status == "failed"))) | length) > 0
                         then [
                           "  Stuck-in-loop: \($s.cooldown | map(select((.attempts_24h >= 3) and (.last_status == "failed"))) | map("#\(.issue_number) (\(.attempts_24h)x \(.last_phase | split("-")[0]))") | join(", "))"
                         ]
                         else []
                       end)
                  )
              end)
         else []
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
