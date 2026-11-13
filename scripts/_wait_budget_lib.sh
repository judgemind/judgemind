# shellcheck shell=bash
# _wait_budget_lib.sh — sourceable per-call time budget for the long-wait
# helpers (#4850).
#
# This file has no shebang and no `set -e` so it can be sourced safely by
# scripts that already have their own error-handling settings. It works on
# the macOS system bash (3.2) as well as bash 5.
#
# ── Why this exists ──────────────────────────────────────────────────────────
#
# #4835 (PR #4848) gave each long-wait helper (wait-for-ci.sh,
# wait-for-run.sh, ecs-wait-task.sh, ecs-run-task.sh, run-scraper.sh,
# wait-for-rollout.sh, ecs-redeploy.sh, ecs-logs.sh --follow) a 480s budget
# per call so the call ends inside the Bash tool's 600s cap. That budget was
# only checked BETWEEN polls. One `gh` / `aws` call that hung for minutes
# still pushed the helper past 600s, and the harness moved it to the
# background (#4850: wait-for-ci.sh took 566s once and >600s later;
# wait-for-run.sh printed one poll and then nothing).
#
# Every remote call inside a wait loop now goes through wait_budget_call:
#   1. The call gets a hard per-call timeout of min(WAIT_CALL_MAX_SECS,
#      time left in the budget), floored at WAIT_CALL_MIN_SECS. A hung call
#      is killed and returns 124, so the loop gets back to its deadline
#      check and exits 124 ("still running, re-run") on time. Worst case a
#      helper ends about WAIT_CALL_MIN_SECS + 3s after its budget.
#   2. A call slower than WAIT_SLOW_CALL_SECS is logged to stderr with its
#      duration ("slow call: ..."), so the next occurrence names the call.
#
# ── Laptop sleep vs. a hung call ─────────────────────────────────────────────
#
# Bash has no monotonic clock that is cheap to read on macOS. The budget
# uses $SECONDS, which follows the wall clock, so time the laptop spends
# asleep counts against the budget. That is the safe direction: the helper
# exits 124 soon after the laptop wakes and the caller simply re-runs it.
#
# The per-call watchdog (ecs_exec_run_with_timeout in _ecs_exec_lib.sh)
# counts its own one-second ticks instead of reading the clock. A suspended
# laptop runs no ticks, so a laptop sleep is NOT mistaken for a hung call.
# The two clocks also let us tell the cases apart after the fact: if a call
# took longer on the wall clock than its per-call cap but the watchdog never
# fired, the host was asleep during the call, and the slow-call log says so.
#
# ── Usage ────────────────────────────────────────────────────────────────────
#
#   source "$(dirname "${BASH_SOURCE[0]}")/_wait_budget_lib.sh"
#   wait_budget_init "$TIMEOUT_SECS"            # "" or "unlimited" = no budget
#   out=$(wait_budget_call "gh run view" gh run view "$RUN_ID" --json status) \
#       || rc=$?                                # rc 124 = the call was killed
#   wait_budget_exhausted && exit 124           # too little left for a call
#   wait_budget_sleep "$POLL_INTERVAL"          # never sleeps past the budget
#
# Tunables (environment):
#   WAIT_CALL_MAX_SECS    per-call cap (default 60)
#   WAIT_CALL_MIN_SECS    per-call floor, and the "one call's worth" margin
#                         below which wait_budget_exhausted is true (default
#                         5; capped at a quarter of the budget)
#   WAIT_SLOW_CALL_SECS   log calls at least this slow (default 30)

_WAIT_BUDGET_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_ecs_exec_lib.sh
source "${_WAIT_BUDGET_LIB_DIR}/_ecs_exec_lib.sh"

WAIT_CALL_MAX_SECS="${WAIT_CALL_MAX_SECS:-60}"
WAIT_CALL_MIN_SECS="${WAIT_CALL_MIN_SECS:-5}"
WAIT_SLOW_CALL_SECS="${WAIT_SLOW_CALL_SECS:-30}"
# Exit code a wait_budget_call returns when its call was killed.
WAIT_CALL_TIMEOUT_RC="$ECS_EXEC_TIMEOUT_RC"

WAIT_BUDGET_START=""
WAIT_BUDGET_DEADLINE=""   # empty = no budget (unlimited wait)

# Slow-call and timeout notes go to fd 9, a copy of the helper's stderr taken
# at init time. Callers often capture a call's stderr into a variable with
# `2>&1`; writing the notes to fd 9 keeps them out of that captured text.
_WAIT_BUDGET_LOG_FD_OPEN=false

# wait_budget_init <timeout-secs> [start-secs]
#
# Starts the budget. <timeout-secs> of "" or "unlimited" means no budget:
# calls still get the WAIT_CALL_MAX_SECS cap but nothing is ever exhausted.
# [start-secs] is a $SECONDS value to count from (default: now), for helpers
# whose budget started before the wait loop (e.g. at script start).
wait_budget_init() {
    local timeout="${1:-}"
    WAIT_BUDGET_START="${2:-$SECONDS}"
    if [ -z "$timeout" ] || [ "$timeout" = "unlimited" ]; then
        WAIT_BUDGET_DEADLINE=""
    else
        WAIT_BUDGET_DEADLINE=$((WAIT_BUDGET_START + timeout))
    fi
    if [ "$_WAIT_BUDGET_LOG_FD_OPEN" = "false" ]; then
        exec 9>&2
        _WAIT_BUDGET_LOG_FD_OPEN=true
    fi
}

# wait_budget_remaining — prints the seconds left in the budget (may be
# negative), or a large number when there is no budget.
wait_budget_remaining() {
    if [ -z "$WAIT_BUDGET_DEADLINE" ]; then
        echo 999999
    else
        echo $((WAIT_BUDGET_DEADLINE - SECONDS))
    fi
}

# wait_budget_exhausted — true when less than one call's worth is left.
# Check it before starting another poll: a call started now could not
# finish inside the budget, so exit 124 instead. One call's worth is
# WAIT_CALL_MIN_SECS, capped at a quarter of the whole budget so that tiny
# test budgets (1-4s) still get their second poll.
wait_budget_exhausted() {
    [ -n "$WAIT_BUDGET_DEADLINE" ] || return 1
    local margin="$WAIT_CALL_MIN_SECS"
    local quarter=$(( (WAIT_BUDGET_DEADLINE - WAIT_BUDGET_START) / 4 ))
    if [ "$quarter" -lt "$margin" ]; then
        margin="$quarter"
    fi
    [ $((WAIT_BUDGET_DEADLINE - SECONDS)) -lt "$margin" ]
}

# wait_budget_sleep <secs> — sleep <secs>, but never past the budget.
wait_budget_sleep() {
    local secs="$1"
    if [ -n "$WAIT_BUDGET_DEADLINE" ]; then
        local left=$((WAIT_BUDGET_DEADLINE - SECONDS))
        if [ "$left" -lt "$secs" ]; then
            secs="$left"
        fi
    fi
    if [ "$secs" -gt 0 ]; then
        sleep "$secs"
    fi
}

_wait_budget_note() {
    if [ "$_WAIT_BUDGET_LOG_FD_OPEN" = "true" ]; then
        printf '%s\n' "$*" >&9
    else
        printf '%s\n' "$*" >&2
    fi
}

# wait_budget_call [--max <secs>] <label> <cmd> [args...]
#
# Runs <cmd> under a per-call timeout of min(--max or WAIT_CALL_MAX_SECS,
# time left), floored at WAIT_CALL_MIN_SECS. Returns the command's exit
# status, or 124 (WAIT_CALL_TIMEOUT_RC) when the call was killed. The
# command's stdout and stderr are passed through unchanged; its stdin is
# /dev/null.
#
# Output goes through temp files rather than straight to the caller's pipe.
# When the caller captures stdout with $(...), a killed command's leftover
# grandchild could otherwise keep that pipe open and block the capture.
wait_budget_call() {
    local max="$WAIT_CALL_MAX_SECS"
    if [ "${1:-}" = "--max" ]; then
        max="$2"
        shift 2
    fi
    local label="$1"
    shift

    local cap="$max"
    if [ -n "$WAIT_BUDGET_DEADLINE" ]; then
        local left=$((WAIT_BUDGET_DEADLINE - SECONDS))
        if [ "$left" -lt "$cap" ]; then
            cap="$left"
        fi
    fi
    if [ "$cap" -lt "$WAIT_CALL_MIN_SECS" ]; then
        cap="$WAIT_CALL_MIN_SECS"
    fi

    local out_file err_file
    out_file=$(mktemp)
    err_file=$(mktemp)
    local rc=0
    local t0=$SECONDS
    ecs_exec_run_with_timeout "$cap" "$@" \
        </dev/null >"$out_file" 2>"$err_file" 9>&- || rc=$?
    local took=$((SECONDS - t0))

    cat "$out_file"
    cat "$err_file" >&2
    rm -f "$out_file" "$err_file"

    if [ "$rc" -eq "$WAIT_CALL_TIMEOUT_RC" ]; then
        _wait_budget_note "timed out: ${label} was killed after its ${cap}s per-call limit (${took}s wall); treating it as a failed poll (#4850)."
    elif [ "$took" -ge "$WAIT_SLOW_CALL_SECS" ]; then
        if [ "$took" -gt $((cap + 5)) ]; then
            # The watchdog counts awake seconds; the wall clock does not
            # stop while the host sleeps. Wall time past the cap without a
            # kill means the host was suspended during the call.
            _wait_budget_note "slow call: ${label} took ${took}s wall, longer than its ${cap}s per-call limit without being killed, so the host was probably asleep (not a hung call)."
        else
            _wait_budget_note "slow call: ${label} took ${took}s (per-call limit ${cap}s)."
        fi
    fi
    return "$rc"
}
