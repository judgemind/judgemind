#!/usr/bin/env bash
# progress.sh — Best-effort milestone update for the dispatcher v3 cockpit.
#
# `/task` invokes this helper at each natural milestone (planning, ralph,
# summary, push_and_pr, awaiting_ci, fix_ci, merge, awaiting_deploy, verify,
# retro, …) so the cockpit can show "where is this agent right now" without
# reintroducing a phase state machine. The helper does ONE UPDATE on the
# `dispatcher.agents` row and returns 0 unconditionally — see the design
# requirements in `docs/specs/dispatcher-v3-spec.md` §4.3.
#
# Usage:
#   scripts/dispatcher/progress.sh <agent_id> <milestone> [detail]
#
# Best-effort contract — exits 0 on every path:
#   * Missing args                 → exit 0 (usage to stderr)
#   * Missing DATABASE_URL          → exit 0 (note to stderr)
#   * psql binary not on PATH       → exit 0 (note to stderr)
#   * DB connect / query failure    → exit 0 (psql stderr swallowed)
#   * Successful UPDATE              → exit 0
#
# This contract is non-negotiable. The helper lives on the hot path of every
# `/task` agent; a non-zero exit would propagate up and trip pre-push hooks,
# CI pre-flight checks, or the agent-runner entrypoint. v3 milestones are
# observation, not control.
#
# SQL injection safety:
#
#   The user-controlled inputs (agent_id, milestone, detail) are passed to
#   psql via the `-v` flag, then referenced inside the SQL with the
#   `:'name'` substitution syntax. psql expands `:'name'` as a properly
#   single-quoted SQL string literal with internal quotes doubled, so values
#   like `'; DROP TABLE agents; --` end up as a literal string, not as
#   executable SQL. See psql(1) "SQL Interpolation" in the PostgreSQL docs.
#
# Cohabitation note:
#
#   v2 daemon-managed agents do not call this helper, so their
#   `current_milestone*` columns stay NULL — exactly the expected v2 behavior
#   per migration 56's column comments. v3 agents call it, the cockpit reads
#   it, nothing else does.

set -uo pipefail
# NOTE: deliberately NOT `set -e`. We want every error path to fall through
# to the final `exit 0` so a DB blip never blocks `/task`.

PROG="$(basename "$0")"

# ── Arg validation ─────────────────────────────────────────────────────────
#
# Two required positional args (agent_id, milestone) and an optional third
# (detail). Per the best-effort contract, missing args go to stderr but
# still exit 0.

if [ $# -lt 2 ] || [ -z "${1:-}" ] || [ -z "${2:-}" ]; then
    echo "usage: $PROG <agent_id> <milestone> [detail]" >&2
    exit 0
fi

AGENT_ID="$1"
MILESTONE="$2"
DETAIL="${3:-}"

# ── Environment validation ─────────────────────────────────────────────────

if [ -z "${DATABASE_URL:-}" ]; then
    echo "$PROG: DATABASE_URL not set, skipping milestone update" >&2
    exit 0
fi

if ! command -v psql >/dev/null 2>&1; then
    echo "$PROG: psql not on PATH, skipping milestone update" >&2
    exit 0
fi

# ── UPDATE via parameterized psql ───────────────────────────────────────────
#
# Why `psql -v` + `:'name'` instead of inline shell interpolation:
#
#   The naïve form `psql -c "UPDATE … SET col = '$MILESTONE' …"` lets a
#   crafted milestone string (e.g. `'; DROP TABLE agents; --`) escape its
#   quotes and execute arbitrary SQL. `:'name'` substitution by psql is
#   immune: the shell only ever passes the value as a CLI flag value, and
#   psql does its own quoting of the value when expanding `:'name'`.
#
# Why `-f -` (stdin) instead of `-c`:
#
#   psql's variable interpolation runs during *file/stdin parsing*. With
#   `-c`, the SQL string is sent straight to the server without `:'name'`
#   expansion (ERROR: syntax error at or near ":"). Reading the SQL from
#   stdin via `-f -` lets psql perform the substitution before sending.
#
# Flags:
#   -X                    Skip ~/.psqlrc (avoid surprise side effects).
#   -q                    Quiet — no banner / row count chatter on stdout.
#   -v ON_ERROR_STOP=0    Tolerant — a bad row or bad column does not abort.
#   -v agent_id=…         Bind input to psql variables for `:'name'` use.
#   -f -                  Read SQL from stdin so `:'name'` interpolation runs.
#
# The query runs under `statement_timeout = 5000` so a wedged DB lock can't
# stall the helper forever. 5s is generous for a single-row UPDATE on a
# table that's already keyed on `agent_id`. PGCONNECT_TIMEOUT=5 prevents
# a black-holed network from blocking past the cockpit's freshness window.

psql_output_log="$(mktemp -t progress.XXXXXX 2>/dev/null || echo /tmp/progress.$$.log)"

# The SQL body. `:'name'` placeholders are expanded by psql as
# properly-escaped string literals — see comment block above.
read -r -d '' SQL_BODY <<'SQL' || true
SET statement_timeout = 5000;
UPDATE dispatcher.agents
SET current_milestone = :'milestone',
    current_milestone_detail = :'detail',
    current_milestone_at = now()
WHERE agent_id = :'agent_id';
SQL

if ! printf '%s\n' "$SQL_BODY" | PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-5}" \
        psql "$DATABASE_URL" \
            -X -q -v ON_ERROR_STOP=0 \
            -v "agent_id=$AGENT_ID" \
            -v "milestone=$MILESTONE" \
            -v "detail=$DETAIL" \
            -f - \
            >/dev/null 2>"$psql_output_log"; then
    # Connect failure / network blip / permissions issue / etc. Echo the
    # first line of psql's stderr so an operator tail-ing the agent log
    # sees what happened, but DO NOT propagate a non-zero exit.
    if [ -s "$psql_output_log" ]; then
        first_err_line="$(head -n 1 "$psql_output_log")"
        echo "$PROG: psql failed (swallowed): $first_err_line" >&2
    else
        echo "$PROG: psql failed (swallowed)" >&2
    fi
fi

rm -f "$psql_output_log" 2>/dev/null || true

exit 0
