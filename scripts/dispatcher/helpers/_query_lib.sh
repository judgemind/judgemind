#!/usr/bin/env bash
# venv: N/A — pure bash + jq, sourced by sibling helpers
# permanent: true
#
# _query_lib.sh — shared `dev-db-query.sh → JSON` helpers for the dispatcher
# helper scripts (fleet-status.sh, breaker.sh, diagnoses.sh, agent-timeline.sh,
# issue-timeline.sh).
#
# Each helper used to carry an inline `json_only` awk + `query` wrapper. Both
# had the same two fragilities:
#   1. The awk carved out lines between the first `^\[` and the first `^\]`
#      — anything on those lines other than JSON (e.g. an SSM banner that
#      happens to start with `[`) would corrupt jq's input.
#   2. No JSON well-formedness check. When dev-db-query.sh's ECS-exec stream
#      truncated mid-JSON or had a trailer like `Cannot perform start
#      session: EOF` merged onto stdout, jq would raise a bare `parse error:
#      Invalid numeric literal at line N, column M` (see #3124) — the
#      helper would exit with no hint at what actually arrived.
#
# The fix is small and defensive: validate with `jq -e .` before returning,
# retry up to 3 times on parse failure (the issue-timeline.sh pattern from
# #2921), and on final failure dump the raw captured output to stderr so
# the operator can see exactly what the stream delivered.
#
# bash 3.2 compatible (#3084) — no `mapfile`, `declare -A`, namerefs, etc.
#
# This file is meant to be *sourced*, not executed. The leading underscore
# in the name keeps scripts/run-scripts-tests.sh from picking it up.
#
# Consumers must set:
#   dev_db    — path to scripts/dev-db-query.sh (already resolved).

set -uo pipefail

# Strip the dev-db-query.sh chatter (SSM banners, exit trailers) and keep
# only the JSON array payload. The Python runner always emits a SELECT as
# a JSON array, so we key off `^[` / `^]`.
_query_lib_json_only() {
    awk '/^\[/{f=1} f&&!g{print} /^\]/{if(f)g=1}'
}

# _query_lib_run <sql> [label]
#
# Execute the given SQL via "$dev_db", extract the JSON payload, and
# validate it with jq. On success, prints the JSON to stdout and returns 0.
# On failure, retries up to 3 times (2s between attempts) — if the last
# attempt still fails, prints a descriptive error plus the raw captured
# output to stderr and returns 2 (matching the existing "DB error" exit
# code used by every helper).
#
# The jq validation catches the exact failure mode in #3124: a transient
# `Cannot perform start session: EOF` trailer, a truncated stream, or
# banner text bleeding into the stdout fd — any of which would otherwise
# surface as a bare `parse error: Invalid numeric literal` from the
# downstream pipeline.
_query_lib_run() {
    local sql="$1"
    local label="${2:-query}"
    local attempt out raw
    for attempt in 1 2 3; do
        # Capture raw separately so we can dump it on final failure.
        raw=$("$dev_db" "$sql" 2>/dev/null || true)
        out=$(printf '%s\n' "$raw" | _query_lib_json_only)
        if [[ -n "$out" ]] && printf '%s' "$out" | jq -e . >/dev/null 2>&1; then
            printf '%s' "$out"
            return 0
        fi
        if (( attempt < 3 )); then
            echo "Warning: ${label}: dev-db-query.sh returned malformed JSON on attempt ${attempt}/3 — retrying" >&2
            sleep 2
        fi
    done
    echo "Error: ${label}: dev-db-query.sh returned malformed JSON after 3 attempts" >&2
    echo "Error: ${label}: last raw stdout was:" >&2
    printf '%s\n' "$raw" | sed 's/^/    /' >&2
    return 2
}
