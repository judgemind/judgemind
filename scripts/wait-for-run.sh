#!/usr/bin/env bash
# wait-for-run.sh — Wait for one GitHub Actions workflow run to finish, in
# calls that each fit inside the Bash tool's 600s cap.
#
# # venv: none
# # permanent: true
#
# ── Why this exists (#4835) ──────────────────────────────────────────────────
#
# `gh run watch` blocks until the run finishes. Deploy and terraform runs
# often take longer than the Bash tool's 600s cap, so an agent's call got
# moved to the background partway through. This helper waits at most
# --timeout-secs (default 480), then exits 124: "still running, re-run the
# same command". Re-running only reads the run's state; it never re-runs or
# cancels anything.
#
# ── Usage ────────────────────────────────────────────────────────────────────
#
# Usage: scripts/wait-for-run.sh <run-id> [options]
#
# Find the run id first, e.g.:
#   gh run list --repo judgemind/judgemind --workflow deploy-api.yml --branch main --limit 1 --json databaseId -q '.[0].databaseId'
#
# Options:
#   --repo OWNER/REPO    Repository (default: judgemind/judgemind)
#   --timeout-secs N     How long this call waits (default: 480)
#   --poll-interval N    Seconds between polls (default: 30)
#   --help               Show this help and exit 0
#
# Exit codes:
#   0     the run completed with conclusion success, skipped or neutral
#   1     the run completed with any other conclusion (failure, cancelled,
#         timed_out, ...), or a usage error
#   124   still running when --timeout-secs ran out. NOT a failure. Re-run
#         the same command to keep waiting.
#   125   `gh run view` failed 3 times in a row, so the run's state is
#         unknown. Check `gh auth status`, then re-run the same command.

set -euo pipefail

REPO="judgemind/judgemind"
TIMEOUT_SECS=480
POLL_INTERVAL=30
RUN_ID=""
MAX_CONSECUTIVE_ERRORS=3
ORIG_ARGS=("$@")

usage() {
    sed -n '2,/^set -euo pipefail$/p' "$0" | sed 's/^# \{0,1\}//' | sed '$d'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)
            REPO="${2:-}"
            shift 2
            ;;
        --timeout-secs)
            TIMEOUT_SECS="${2:-}"
            shift 2
            ;;
        --poll-interval)
            POLL_INTERVAL="${2:-}"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        -*)
            echo "Error: unknown option: $1" >&2
            echo "Run: scripts/wait-for-run.sh --help" >&2
            exit 1
            ;;
        *)
            if [[ -n "$RUN_ID" ]]; then
                echo "Error: unexpected argument: $1" >&2
                exit 1
            fi
            RUN_ID="$1"
            shift
            ;;
    esac
done

if ! [[ "$RUN_ID" =~ ^[0-9]+$ ]]; then
    echo "Error: a numeric <run-id> is required, got: '${RUN_ID}'" >&2
    echo "Usage: scripts/wait-for-run.sh <run-id> [--repo OWNER/REPO] [--timeout-secs N]" >&2
    exit 1
fi
for _pair in "timeout-secs:$TIMEOUT_SECS" "poll-interval:$POLL_INTERVAL"; do
    if ! [[ "${_pair#*:}" =~ ^[0-9]+$ ]]; then
        echo "Error: --${_pair%%:*} must be a non-negative integer, got: '${_pair#*:}'" >&2
        exit 1
    fi
done

START_SECS=$SECONDS
CONSECUTIVE_ERRORS=0
LAST_STATUS=""

echo "Waiting for run ${RUN_ID} in ${REPO} (timeout: ${TIMEOUT_SECS}s, poll: ${POLL_INTERVAL}s)..."

while true; do
    ELAPSED=$(( SECONDS - START_SECS ))
    VIEW=""
    if VIEW=$(gh run view "$RUN_ID" --repo "$REPO" --json status,conclusion \
            -q '.status + " " + (.conclusion // "")' 2>&1); then
        CONSECUTIVE_ERRORS=0
        STATUS="${VIEW%% *}"
        CONCLUSION="${VIEW#* }"
        if [[ "$STATUS" != "$LAST_STATUS" ]]; then
            echo "[${ELAPSED}s] status=${STATUS}"
            LAST_STATUS="$STATUS"
        fi
        if [[ "$STATUS" == "completed" ]]; then
            case "$CONCLUSION" in
                success|skipped|neutral)
                    echo "Run ${RUN_ID} completed: ${CONCLUSION}."
                    exit 0
                    ;;
                *)
                    echo "Run ${RUN_ID} completed: ${CONCLUSION:-<no conclusion>}." >&2
                    echo "Failed logs: gh run view ${RUN_ID} --repo ${REPO} --log-failed" >&2
                    exit 1
                    ;;
            esac
        fi
    else
        CONSECUTIVE_ERRORS=$((CONSECUTIVE_ERRORS + 1))
        echo "WARNING: gh run view failed (${CONSECUTIVE_ERRORS}/${MAX_CONSECUTIVE_ERRORS}): $(echo "$VIEW" | tr '\n' ' ' | cut -c1-300)" >&2
        if [[ $CONSECUTIVE_ERRORS -ge $MAX_CONSECUTIVE_ERRORS ]]; then
            echo "Run ${RUN_ID} state unknown: gh run view kept failing. Check 'gh auth status', then re-run:" >&2
            echo "  scripts/wait-for-run.sh ${ORIG_ARGS[*]}" >&2
            exit 125
        fi
    fi

    ELAPSED=$(( SECONDS - START_SECS ))
    if [[ $ELAPSED -ge $TIMEOUT_SECS ]]; then
        echo "STILL WAITING: run ${RUN_ID} is still ${LAST_STATUS:-running} after ${TIMEOUT_SECS}s. This is not a failure." >&2
        echo "Re-run the same command to keep waiting (it only reads state):" >&2
        echo "  scripts/wait-for-run.sh ${ORIG_ARGS[*]}" >&2
        exit 124
    fi

    # Never sleep past the deadline, so the call ends on time.
    _sleep_for=$POLL_INTERVAL
    _remaining=$(( TIMEOUT_SECS - ELAPSED ))
    if [[ $_remaining -lt $_sleep_for ]]; then
        _sleep_for=$_remaining
    fi
    sleep "$_sleep_for"
done
