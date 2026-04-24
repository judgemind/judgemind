#!/usr/bin/env bash
# venv: N/A — pure bash + scripts/dev-db-query.sh + jq
# permanent: true
#
# breaker.sh — operator controls for the dispatcher's concurrency cap and
# circuit breaker. Wraps the dispatcher.config reads and the
# dispatcher.commands INSERT pattern so the operator doesn't hand-roll SQL
# for routine "why did the cap flip / reset it / kill a wedged agent" ops.
#
# Usage:
#   scripts/dispatcher/helpers/breaker.sh status
#   scripts/dispatcher/helpers/breaker.sh reset
#   scripts/dispatcher/helpers/breaker.sh pause
#   scripts/dispatcher/helpers/breaker.sh force-stop <agent-id-or-prefix>
#
# Subcommands:
#   status            Show cap, flipped_by, recent-outcomes window, bad_count
#                     vs threshold, and time-until-next-eligible-eval.
#   reset             Issue `start` command (cap → 1). Auto-closes the
#                     circuit breaker's `cap_flipped_by` flag on next tick.
#                     Polls until the command is consumed (up to 90s).
#   pause             Issue `stop` command (graceful — cap → 0, in-flight
#                     agents complete, no new spawns). Polls until consumed.
#   force-stop PREFIX Issue `force_stop` against one agent matching the
#                     UUID prefix (min 4 hex chars). Kills mid-flight.
#
# Timestamps are UTC with `Z` AND Pacific with PT because the operator
# defaults to Pacific in conversation (see feedback_timezone_in_communication).
#
# Exit codes:
#   0  — subcommand succeeded.
#   1  — usage error, invalid prefix, or command not consumed in time.
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

# ─── Helpers ────────────────────────────────────────────────────────────

# Run a query and return the JSON array. _query_lib_run validates the JSON
# and retries on transient ECS-exec stream failures — see #3124.
query() {
    local sql="$1"
    _query_lib_run "$sql" "breaker"
}

# Resolve an agent UUID prefix (min 4 hex chars) to a single agent_id.
resolve_agent() {
    local prefix="$1"
    if ! [[ "$prefix" =~ ^[0-9a-fA-F-]{4,36}$ ]]; then
        echo "Error: invalid agent-id prefix '$prefix' (expected 4–36 hex/dash)" >&2
        exit 1
    fi
    local sql="SELECT agent_id::text AS id, issue_number, phase, status FROM dispatcher.agents WHERE agent_id::text LIKE '${prefix}%' ORDER BY started_at DESC LIMIT 5;"
    local rows
    rows=$(query "$sql")
    local n
    n=$(echo "$rows" | jq 'length')
    if [[ "$n" == "0" ]]; then
        echo "Error: no agent matches prefix '$prefix'" >&2
        exit 1
    fi
    if [[ "$n" != "1" ]]; then
        echo "Prefix '$prefix' is ambiguous — $n matches (most recent first):" >&2
        echo "$rows" | jq -r '.[] | "  \(.id)  #\(.issue_number)  \(.phase)/\(.status)"' >&2
        exit 1
    fi
    echo "$rows" | jq -r '.[0].id'
}

# Issue a command and poll until consumed (or timeout). Prints command_id
# on success.
issue_and_wait() {
    local command="$1"
    local payload="${2:-{\}}"
    local insert_sql="INSERT INTO dispatcher.commands (command, issued_by, issued_at, payload) VALUES ('${command}', 'operator-session', now(), '${payload}'::jsonb) RETURNING command_id;"
    local insert_result
    insert_result=$(query "$insert_sql")
    local cmd_id
    cmd_id=$(echo "$insert_result" | jq -r '.[0].command_id')
    echo "Issued $command (command_id=$cmd_id). Polling for consumption…" >&2
    local poll_sql="SELECT command_id, consumed_at AT TIME ZONE 'UTC' AS consumed_utc FROM dispatcher.commands WHERE command_id=${cmd_id};"
    local deadline=$((SECONDS + 90))
    while (( SECONDS < deadline )); do
        local row
        # Single-shot poll: if parsing fails here, fall through to the
        # next tick rather than hard-exit — the command may still be
        # consumed shortly. _query_lib_run's retry would also work but
        # its 3×(attempt+2s sleep) worst-case would blow the 5s cadence.
        row=$("$dev_db" "$poll_sql" 2>/dev/null | _query_lib_json_only)
        local consumed
        consumed=$(printf '%s' "$row" | jq -r '.[0].consumed_utc // "null"' 2>/dev/null || printf 'null')
        if [[ "$consumed" != "null" ]]; then
            echo "Consumed at ${consumed}Z." >&2
            printf '%s' "$cmd_id"
            return 0
        fi
        sleep 5
    done
    echo "Error: command_id=$cmd_id not consumed within 90s. Daemon may be unhealthy." >&2
    exit 1
}

# ─── Subcommands ────────────────────────────────────────────────────────

cmd_status() {
    local sql="
WITH
  config_row AS (
    SELECT
      (SELECT value::int FROM dispatcher.config WHERE key='concurrency_cap') AS cap,
      (SELECT value::text FROM dispatcher.config WHERE key='cap_flipped_by') AS flipped_by,
      (SELECT to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') FROM dispatcher.config WHERE key='concurrency_cap') AS cap_updated_utc,
      (SELECT to_char(updated_at AT TIME ZONE 'America/Los_Angeles', 'HH24:MI:SS\" PT\"') FROM dispatcher.config WHERE key='concurrency_cap') AS cap_updated_pt,
      COALESCE((SELECT value::int FROM dispatcher.config WHERE key='circuit_breaker_window_minutes'), 30) AS window_minutes,
      COALESCE((SELECT value::int FROM dispatcher.config WHERE key='circuit_breaker_window_size'), 10) AS window_size,
      COALESCE((SELECT value::int FROM dispatcher.config WHERE key='circuit_breaker_bad_outcome_threshold'), 5) AS threshold
  ),
  outcomes AS (
    SELECT
      t.status,
      (SELECT f.category FROM dispatcher.failures f
        WHERE f.agent_id=t.agent_id ORDER BY f.ts DESC LIMIT 1) AS latest_category,
      t.agent_id::text AS agent_id,
      t.issue_number,
      to_char(t.ended_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS ended_utc,
      to_char(t.ended_at AT TIME ZONE 'America/Los_Angeles', 'HH24:MI:SS\" PT\"') AS ended_pt,
      EXTRACT(EPOCH FROM (now() - t.ended_at))::int AS seconds_ago,
      t.ended_at
    FROM dispatcher.terminal_outcomes t
    WHERE t.ended_at > now() - make_interval(mins => (SELECT window_minutes FROM config_row))
    ORDER BY t.ended_at DESC
    LIMIT (SELECT window_size FROM config_row)
  ),
  pending_commands AS (
    SELECT COALESCE(jsonb_agg(c), '[]'::jsonb) AS data FROM (
      SELECT jsonb_build_object(
        'command_id', command_id,
        'command', command,
        'issued_by', issued_by,
        'issued_utc', to_char(issued_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),
        'payload', payload
      ) AS c
      FROM dispatcher.commands
      WHERE consumed_at IS NULL
      ORDER BY issued_at
    ) x
  )
SELECT jsonb_build_object(
  'config', to_jsonb((SELECT config_row FROM config_row)),
  'outcomes', COALESCE((SELECT jsonb_agg(to_jsonb(o)) FROM outcomes o), '[]'::jsonb),
  'pending_commands', (SELECT data FROM pending_commands)
) AS state;
"
    local result
    result=$(query "$sql")
    local state
    state=$(echo "$result" | jq '.[0].state')

    # Classify and print.
    echo "$state" | jq -r '
      .config as $c
      | .outcomes as $outs
      | .pending_commands as $pending
      # Mirror daemon: skip_infra = (category IN daemon_restart_abandoned, paused_by_killswitch)
      | ($outs | map(select(.latest_category != "daemon_restart_abandoned" and .latest_category != "paused_by_killswitch"))) as $non_infra
      | ($non_infra | map(select(.status != "succeeded"))) as $bad
      | ($outs | map(select(.latest_category == "daemon_restart_abandoned" or .latest_category == "paused_by_killswitch"))) as $skipped_infra
      | "── concurrency_cap ──",
        "  cap:        \($c.cap)",
        "  flipped_by: \($c.flipped_by // "(null)")",
        "  cap updated: \($c.cap_updated_utc)  (\($c.cap_updated_pt))",
        "",
        "── circuit breaker ──",
        "  window:     \($c.window_minutes)m / last \($c.window_size) terminals",
        "  threshold:  \($c.threshold) bad outcomes",
        "  bad count:  \($bad | length)  (\(if ($bad|length) >= $c.threshold then "OVER THRESHOLD" else "under threshold" end))",
        "  window size observed: \($outs | length)  (\($non_infra | length) non-infra, \($skipped_infra | length) skipped as infra preemption)",
        "",
        "── recent terminal_outcomes (window) ──",
        (if ($outs | length) == 0
          then "  (none in window)"
          else ($outs | map(
            "  \(.ended_utc)  (\(.ended_pt))  \(.agent_id[:8])  #\(.issue_number)  \(.status)" +
            (if .latest_category != null then "  cat=\(.latest_category)" else "" end) +
            (if (.latest_category == "daemon_restart_abandoned" or .latest_category == "paused_by_killswitch")
              then "  [skipped — infra]"
              elif .status == "succeeded"
                then "  [good]"
              else "  [bad]"
            end)
          ) | .[])
         end),
        "",
        "── pending commands ──",
        (if ($pending | length) == 0
          then "  (none)"
          else ($pending | map("  [\(.issued_utc)] #\(.command_id) \(.command) by=\(.issued_by) payload=\(.payload | tostring)") | .[])
         end)
    '
}

cmd_reset() {
    issue_and_wait "start" "{}" >/dev/null
    # Report post-reset state.
    local after_sql="SELECT jsonb_build_object(
      'cap', (SELECT value::int FROM dispatcher.config WHERE key='concurrency_cap'),
      'flipped_by', (SELECT value FROM dispatcher.config WHERE key='cap_flipped_by')
    ) AS state;"
    local result
    result=$(query "$after_sql")
    echo "Post-reset state:"
    echo "$result" | jq -r '.[0].state | "  cap:        \(.cap)\n  flipped_by: \(.flipped_by // "(null)")"'
}

cmd_pause() {
    issue_and_wait "stop" "{}" >/dev/null
    local after_sql="SELECT jsonb_build_object(
      'cap', (SELECT value::int FROM dispatcher.config WHERE key='concurrency_cap'),
      'flipped_by', (SELECT value FROM dispatcher.config WHERE key='cap_flipped_by')
    ) AS state;"
    local result
    result=$(query "$after_sql")
    echo "Post-pause state:"
    echo "$result" | jq -r '.[0].state | "  cap:        \(.cap)\n  flipped_by: \(.flipped_by // "(null)")"'
}

cmd_force_stop() {
    local prefix="$1"
    local agent_id
    agent_id=$(resolve_agent "$prefix")
    local payload="{\"agentId\": \"${agent_id}\"}"
    echo "Force-stopping agent ${agent_id}…" >&2
    issue_and_wait "force_stop" "$payload" >/dev/null
    local after_sql="SELECT agent_id::text, issue_number, phase, status, to_char(ended_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS ended_utc FROM dispatcher.agents WHERE agent_id='${agent_id}';"
    local result
    result=$(query "$after_sql")
    echo "Post-force-stop agent state:"
    echo "$result" | jq -r '.[0] | "  agent:  \(.agent_id)\n  issue:  #\(.issue_number)\n  phase:  \(.phase)\n  status: \(.status)\n  ended:  \(.ended_utc // "(still marked running — may lag by one tick)")"'
}

# ─── Dispatch ───────────────────────────────────────────────────────────

if [[ $# -eq 0 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 1
fi

subcommand="$1"
shift

case "$subcommand" in
    status)
        if [[ $# -ne 0 ]]; then usage; exit 1; fi
        cmd_status
        ;;
    reset)
        if [[ $# -ne 0 ]]; then usage; exit 1; fi
        cmd_reset
        ;;
    pause)
        if [[ $# -ne 0 ]]; then usage; exit 1; fi
        cmd_pause
        ;;
    force-stop)
        if [[ $# -ne 1 ]]; then usage; exit 1; fi
        cmd_force_stop "$1"
        ;;
    *)
        echo "Error: unknown subcommand '$subcommand'" >&2
        usage
        exit 1
        ;;
esac
