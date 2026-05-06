#!/usr/bin/env bash
# summarize-ci-flakes.sh — Aggregate the JSONL log produced by wait-for-ci.sh
# (#4163) into a per-label leaderboard.
#
# # venv: none
# # permanent: true
#
# ── Why ──────────────────────────────────────────────────────────────────────
#
# `scripts/wait-for-ci.sh` appends one JSON line per auto-rerun event to
# `tmp/wait-for-ci-flakes.jsonl` (path overridable via WAIT_FOR_CI_FLAKE_LOG).
# Each line carries a fixed schema — see the script header. This helper reads
# the same file, optionally filters by a recent time window, and prints a
# count-per-label leaderboard so we can spot which flake patterns fire most.
#
# A label crossing some threshold (e.g. 3+ occurrences in a week) is the
# signal to add a new entry to `scripts/classify-ci-flake.sh` — but that
# decision is a human PR, not part of this helper.
#
# ── Usage ────────────────────────────────────────────────────────────────────
#
# Usage:
#   scripts/summarize-ci-flakes.sh                    # default path
#   scripts/summarize-ci-flakes.sh <jsonl-path>       # custom path
#   scripts/summarize-ci-flakes.sh --since 7d         # last N days
#   scripts/summarize-ci-flakes.sh --help
#
# Output (stdout, sorted by count desc):
#
#   COUNT  LABEL
#       7  postgres-startup
#       2  docker-daemon
#       1  dns-resolution
#
# Followed by a `total: <N>` line. If the JSONL file does not exist or is
# empty, prints `(no flake events recorded)` to stdout and exits 0.
#
# Exit codes:
#   0 — Summary printed (including the empty-file case).
#   1 — Usage error or unreadable JSONL file.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_FLAKE_LOG="$(dirname "$SCRIPT_DIR")/tmp/wait-for-ci-flakes.jsonl"

JSONL_PATH=""
SINCE=""

print_help() {
    grep '^# Usage:' "$0" | sed 's/^# //'
    echo ""
    grep '^# Output' "$0" | sed 's/^# //'
    echo ""
    grep '^# Exit codes:' "$0" | sed 's/^# //'
    grep '^#   [012]' "$0" | sed 's/^# //'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h)
            print_help
            exit 0
            ;;
        --since)
            SINCE="$2"
            shift 2
            ;;
        --*)
            echo "ERROR: Unknown option: $1" >&2
            exit 1
            ;;
        *)
            if [ -z "$JSONL_PATH" ]; then
                JSONL_PATH="$1"
                shift
            else
                echo "ERROR: Unexpected argument: $1" >&2
                exit 1
            fi
            ;;
    esac
done

JSONL_PATH="${JSONL_PATH:-${WAIT_FOR_CI_FLAKE_LOG:-$DEFAULT_FLAKE_LOG}}"

if [ ! -f "$JSONL_PATH" ] || [ ! -s "$JSONL_PATH" ]; then
    echo "(no flake events recorded at $JSONL_PATH)"
    exit 0
fi

# Build a `--since` cutoff in seconds-since-epoch, or empty to disable filtering.
CUTOFF_TS=""
if [ -n "$SINCE" ]; then
    # Accept Nd / Nh / Nm shorthand. Anything else is a hard error.
    case "$SINCE" in
        *d)
            DAYS="${SINCE%d}"
            CUTOFF_TS=$(($(date -u +%s) - DAYS * 86400))
            ;;
        *h)
            HOURS="${SINCE%h}"
            CUTOFF_TS=$(($(date -u +%s) - HOURS * 3600))
            ;;
        *m)
            MINS="${SINCE%m}"
            CUTOFF_TS=$(($(date -u +%s) - MINS * 60))
            ;;
        *)
            echo "ERROR: --since expects Nd|Nh|Nm (got: $SINCE)" >&2
            exit 1
            ;;
    esac
fi

# Filter and count by label. We use `jq` to extract `(ts_epoch, label)` per
# line, drop any malformed lines, then aggregate in `awk` rather than re-
# invoking jq for the count step (cheaper for large logs).
#
# `fromdateiso8601` in jq accepts the `Z`-suffixed form directly on every
# platform (Linux + macOS) — converting to `+00:00` first breaks GNU/macOS
# parity because BSD jq's strptime does not implement the `+HH:MM` zone form.
LABEL_COUNTS=$(jq -r --arg cutoff "${CUTOFF_TS:-0}" '
    select(.label != null and .ts != null)
    | (.ts | fromdateiso8601) as $ts
    | select(($cutoff | tonumber) == 0 or $ts >= ($cutoff | tonumber))
    | .label
' "$JSONL_PATH" 2>/dev/null | sort | uniq -c | sort -rn || true)

if [ -z "$LABEL_COUNTS" ]; then
    if [ -n "$SINCE" ]; then
        echo "(no flake events in window: --since $SINCE)"
    else
        echo "(no flake events recorded at $JSONL_PATH)"
    fi
    exit 0
fi

printf '%5s  %s\n' "COUNT" "LABEL"
echo "$LABEL_COUNTS" | awk '{printf "%5d  %s\n", $1, $2}'

TOTAL=$(echo "$LABEL_COUNTS" | awk '{sum += $1} END {print sum + 0}')
echo ""
echo "total: $TOTAL"
