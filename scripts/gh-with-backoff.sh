#!/usr/bin/env bash
# scripts/gh-with-backoff.sh — Rate-limit-aware wrapper for gh CLI commands.
#
# Wraps `gh` with automatic retry-on-403 and pre-flight rate limit checks.
# When the GitHub API returns HTTP 403 (rate limited), the script queries
# the rate limit reset time and sleeps until the window resets, then retries.
#
# Usage:
#   scripts/gh-with-backoff.sh <gh-subcommand> [args...]
#
# Examples:
#   scripts/gh-with-backoff.sh run watch 12345 --repo judgemind/judgemind --exit-status
#   scripts/gh-with-backoff.sh pr list --repo judgemind/judgemind --state open
#   scripts/gh-with-backoff.sh issue view 42 --repo judgemind/judgemind
#
# Environment:
#   GH_BACKOFF_MAX_RETRIES     — max retry attempts (default: 5)
#   GH_BACKOFF_MIN_WAIT        — minimum wait in seconds per retry (default: 10)
#   GH_BACKOFF_WARN_THRESHOLD  — warn if remaining requests below this (default: 100)
#
# Exit codes:
#   Same as the wrapped `gh` command on success.
#   1 — All retries exhausted or fatal error.

set -euo pipefail

MAX_RETRIES="${GH_BACKOFF_MAX_RETRIES:-5}"
MIN_WAIT="${GH_BACKOFF_MIN_WAIT:-10}"
WARN_THRESHOLD="${GH_BACKOFF_WARN_THRESHOLD:-100}"

if [[ $# -eq 0 ]]; then
    echo "Usage: scripts/gh-with-backoff.sh <gh-subcommand> [args...]" >&2
    echo "Example: scripts/gh-with-backoff.sh run watch 12345 --repo judgemind/judgemind" >&2
    exit 1
fi

# --------------------------------------------------------------------------
# check_rate_limit
#   Query the GitHub API rate limit endpoint.
#   Prints "remaining reset_epoch" to stdout.
#   Returns 1 if the check itself fails (non-fatal).
# --------------------------------------------------------------------------
check_rate_limit() {
    local rate_json
    rate_json=$(gh api rate_limit --jq '.rate | "\(.remaining) \(.reset)"' 2>/dev/null) || {
        echo "WARN: Could not check GitHub API rate limit." >&2
        return 1
    }

    local remaining reset_epoch
    remaining="${rate_json%% *}"
    reset_epoch="${rate_json##* }"

    echo "$remaining $reset_epoch"
    return 0
}

# --------------------------------------------------------------------------
# wait_for_reset RESET_EPOCH
#   Sleep until the rate limit reset window, with a minimum wait.
# --------------------------------------------------------------------------
wait_for_reset() {
    local reset_epoch="${1:-0}"
    local now
    now=$(date +%s)

    local wait_seconds=$((reset_epoch - now))
    if [[ "$wait_seconds" -lt "$MIN_WAIT" ]]; then
        wait_seconds="$MIN_WAIT"
    fi

    # Cap the maximum wait to 15 minutes to avoid indefinite hangs
    if [[ "$wait_seconds" -gt 900 ]]; then
        wait_seconds=900
    fi

    echo "Rate limited — waiting ${wait_seconds}s until reset..." >&2
    sleep "$wait_seconds"
}

# --------------------------------------------------------------------------
# pre_check_budget
#   Warn if rate budget is low before running the command. If critically
#   low (< 10 remaining), proactively wait for the reset.
# --------------------------------------------------------------------------
pre_check_budget() {
    local rate_info remaining reset_epoch
    rate_info=$(check_rate_limit) || return 0

    remaining="${rate_info%% *}"
    reset_epoch="${rate_info##* }"

    if [[ "$remaining" -lt "$WARN_THRESHOLD" ]]; then
        echo "WARN: GitHub API rate budget low — $remaining requests remaining." >&2

        if [[ "$remaining" -lt 10 ]]; then
            echo "Rate budget critically low ($remaining remaining). Waiting for reset before proceeding..." >&2
            wait_for_reset "$reset_epoch"
        fi
    fi
}

# --------------------------------------------------------------------------
# run_with_retry
#   Execute the gh command with retry-on-403 logic.
# --------------------------------------------------------------------------
run_with_retry() {
    local attempt=0 exit_code=0 is_rate_limit=0
    local stderr_file rate_info reset_epoch fallback_wait

    # Use a per-process stderr capture file
    stderr_file="/tmp/gh-backoff-stderr.$$"

    while true; do
        attempt=$((attempt + 1))

        # Run the gh command, capturing stderr for rate limit detection
        exit_code=0
        gh "$@" 2>"$stderr_file" || exit_code=$?

        # Re-emit stderr so the caller sees all output
        if [[ -f "$stderr_file" && -s "$stderr_file" ]]; then
            cat "$stderr_file" >&2
        fi

        # Success — clean up and return
        if [[ "$exit_code" -eq 0 ]]; then
            rm -f "$stderr_file"
            return 0
        fi

        # Check if the failure was a rate limit (403)
        is_rate_limit=0
        if [[ -f "$stderr_file" ]] && grep -qi "rate limit\|403\|API rate limit exceeded\|secondary rate limit" "$stderr_file" 2>/dev/null; then
            is_rate_limit=1
        fi

        rm -f "$stderr_file"

        if [[ "$is_rate_limit" -eq 0 ]]; then
            # Not a rate limit error — pass through the exit code
            return "$exit_code"
        fi

        # Rate limit hit — check if we have retries left
        if [[ "$attempt" -ge "$MAX_RETRIES" ]]; then
            echo "ERROR: GitHub API rate limit — exhausted all $MAX_RETRIES retry attempts." >&2
            return 1
        fi

        echo "Rate limit hit (attempt $attempt/$MAX_RETRIES). Checking reset time..." >&2

        # Get the reset time and wait
        rate_info=$(check_rate_limit) || {
            # Fallback: exponential backoff if we can't check the rate limit
            fallback_wait=$((MIN_WAIT * attempt))
            echo "Could not check rate limit — falling back to ${fallback_wait}s wait." >&2
            sleep "$fallback_wait"
            continue
        }

        reset_epoch="${rate_info##* }"
        wait_for_reset "$reset_epoch"
    done
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
pre_check_budget
run_with_retry "$@"
