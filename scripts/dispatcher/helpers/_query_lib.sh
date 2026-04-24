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
# only the JSON array payload.  The Python runner always emits a SELECT as
# a JSON array, so we look for the first `[` (line-start *or* inline after
# a non-JSON banner was stripped) and return everything up to the last `]`.
#
# Two histories to support:
#   (a) Legacy pretty-printed output where `[` and `]` sit alone on their
#       own lines. Awk's original `^\[` / `^\]` anchors match here.
#   (b) Compact output from the runner's default `separators=(",",":")`
#       mode (#3195) where the entire array is a single line starting with
#       `[` and ending with `]`.
#
# We also accept a run-of-lines between `[` and `]`. dev-db-query.sh now
# filters the SSM banner/trailer before returning, but we keep the
# defensive in-lib filtering too in case a caller bypasses the wrapper
# (e.g. tests that pipe a literal banner through).
_query_lib_json_only() {
    # Pass 1: drop SSM plugin chatter that dev-db-query.sh already strips
    # upstream but that test fixtures still include verbatim.
    # Pass 2: capture everything from the first `[` (line-start or inline)
    # through the last matching `]`.
    grep -Ev '^(Starting session with [Ss]essionId|Exiting session with [Ss]essionId|Cannot perform start session|The Session Manager plugin was installed successfully|Running query on dev database|Task: arn:)' \
        | awk '
            BEGIN { started = 0 }
            {
                if (!started) {
                    pos = index($0, "[")
                    if (pos > 0) {
                        # Emit from the first `[` to the end of the line.
                        print substr($0, pos)
                        started = 1
                        next
                    }
                } else {
                    print
                }
            }
        '
}

# _query_lib_run <sql> [label]
#
# Execute the given SQL via "$dev_db", extract the JSON payload, and
# validate it with jq. On success, prints the JSON to stdout and returns 0.
# On failure, retries up to 5 times (1.5s between attempts) — if the last
# attempt still fails, prints a descriptive error plus the raw captured
# output to stderr and returns 2 (matching the existing "DB error" exit
# code used by every helper).
#
# The jq validation catches the exact failure mode in #3124 and #3195: a
# transient `Cannot perform start session: EOF` trailer, a truncated
# stream, or banner text bleeding into the stdout fd — any of which would
# otherwise surface as a bare `parse error: Invalid numeric literal` from
# the downstream pipeline.
#
# Retry count bumped to 5 (from 3) in #3195 because the SSM / ECS-exec
# transport was observed to deliver truncated output on ~70% of large-
# payload invocations during an incident. More attempts + the runner's
# compact-JSON default combine to keep the 20-consecutive-calls-clean
# acceptance gate reachable.
_query_lib_run() {
    local sql="$1"
    local label="${2:-query}"
    local max_attempts=5
    local attempt out raw
    for attempt in 1 2 3 4 5; do
        # Capture raw separately so we can dump it on final failure.
        raw=$("$dev_db" "$sql" 2>/dev/null || true)
        out=$(printf '%s\n' "$raw" | _query_lib_json_only)
        if [[ -n "$out" ]] && printf '%s' "$out" | jq -e . >/dev/null 2>&1; then
            printf '%s' "$out"
            return 0
        fi
        if (( attempt < max_attempts )); then
            echo "Warning: ${label}: dev-db-query.sh returned malformed JSON on attempt ${attempt}/${max_attempts} — retrying" >&2
            sleep 1
        fi
    done
    echo "Error: ${label}: dev-db-query.sh returned malformed JSON after ${max_attempts} attempts" >&2
    echo "Error: ${label}: last raw stdout was:" >&2
    printf '%s\n' "$raw" | sed 's/^/    /' >&2
    return 2
}
