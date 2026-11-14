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
# ── The Bash tool's 600s cap is wall-clock time (#4975) ──────────────────────
#
# The harness counts time the laptop spends asleep against its 600s cap. A
# process does not run while the host sleeps, and `sleep N` on macOS counts
# only awake time, so a helper parked in `sleep 60` when the lid closed
# stayed parked for up to 60 more seconds after wake. On 2026-11-14
# ecs-wait-task.sh was moved to the background twice this way (pmset showed
# host sleeps of 932s and 558s inside the calls). So after a wake the helper
# must notice at once that its wall-clock budget is gone:
#   - wait_budget_sleep sleeps in slices of WAIT_SLEEP_SLICE_SECS and
#     re-reads $SECONDS (wall clock) between slices;
#   - wait_budget_call also passes the budget deadline to the watchdog as a
#     wall-clock stop, so a call in flight across the sleep is killed right
#     after wake once the deadline has passed. A sleep with budget left
#     still does not count as a hung call.
# Raw `sleep` in a helper's wait path is a bug; scripts/tests/
# test_wait_budget_host_sleep.sh fails on it.
#
# That only shortens the overrun. When the host wakes after the tool's 600s
# mark has already passed, the tool backgrounds the call the moment it wakes
# and no helper can beat it. So on macOS, wait_budget_init also holds a
# "prevent system sleep" assertion for the life of the helper:
# `caffeinate -s -w <helper pid> -t <budget + 60>`. -s only applies on AC
# power (both incidents were idle "Maintenance Sleep" on AC), so battery
# behaviour and lid-close sleep are unchanged, and the assertion ends when
# the helper exits. Set WAIT_BUDGET_NO_CAFFEINATE=1 to turn it off.
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
#   WAIT_SLEEP_SLICE_SECS longest single sleep inside wait_budget_sleep
#                         (default 2)

_WAIT_BUDGET_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_ecs_exec_lib.sh
source "${_WAIT_BUDGET_LIB_DIR}/_ecs_exec_lib.sh"

WAIT_CALL_MAX_SECS="${WAIT_CALL_MAX_SECS:-60}"
WAIT_CALL_MIN_SECS="${WAIT_CALL_MIN_SECS:-5}"
WAIT_SLOW_CALL_SECS="${WAIT_SLOW_CALL_SECS:-30}"
WAIT_SLEEP_SLICE_SECS="${WAIT_SLEEP_SLICE_SECS:-2}"
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
    if [ -n "$WAIT_BUDGET_DEADLINE" ]; then
        _wait_budget_hold_awake $((WAIT_BUDGET_DEADLINE - WAIT_BUDGET_START + 60))
    fi
}

# _wait_budget_hold_awake <secs> — macOS only: keep the host from idle
# sleeping (on AC power) while this helper runs, for at most <secs>. See
# "The Bash tool's 600s cap is wall-clock time" above (#4975). No-op where
# caffeinate is missing (Linux CI, Fargate) or WAIT_BUDGET_NO_CAFFEINATE=1.
_WAIT_BUDGET_CAFFEINATED=false
_wait_budget_hold_awake() {
    local secs="$1"
    if [ "$_WAIT_BUDGET_CAFFEINATED" = "true" ] \
        || [ "${WAIT_BUDGET_NO_CAFFEINATE:-0}" = "1" ] \
        || [ "$secs" -le 0 ] \
        || ! command -v caffeinate >/dev/null 2>&1; then
        return 0
    fi
    # All fds pointed away from the caller's pipes (fd 9 is a copy of
    # stderr), so the assertion process can never hold a capture open.
    # -w ends it as soon as the helper exits; -t caps it regardless.
    caffeinate -s -w "$$" -t "$secs" </dev/null >/dev/null 2>&1 9>&- &
    _WAIT_BUDGET_CAFFEINATED=true
    return 0
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
#
# Sleeps in slices of at most WAIT_SLEEP_SLICE_SECS and re-reads the wall
# clock between slices, so it returns within one slice of a laptop waking
# up past the deadline (#4975). A single `sleep 60` counts only awake time
# on macOS and would hold the helper for up to a minute after wake, while
# the Bash tool's wall-clock cap has already run out. Always returns 0.
wait_budget_sleep() {
    local secs="$1"
    local slice left t0 took
    while [ "$secs" -gt 0 ]; do
        if [ -n "$WAIT_BUDGET_DEADLINE" ]; then
            left=$((WAIT_BUDGET_DEADLINE - SECONDS))
            if [ "$left" -le 0 ]; then
                return 0
            fi
            if [ "$left" -lt "$secs" ]; then
                secs="$left"
            fi
        fi
        slice="$WAIT_SLEEP_SLICE_SECS"
        if [ "$secs" -lt "$slice" ]; then
            slice="$secs"
        fi
        t0=$SECONDS
        sleep "$slice"
        took=$((SECONDS - t0))
        if [ "$took" -gt $((slice + 10)) ]; then
            _wait_budget_note "host asleep: a ${slice}s sleep took ${took}s on the wall clock; that time counts against this call's budget (#4975)."
        fi
        secs=$((secs - slice))
    done
    return 0
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
    # Wall-clock stop (#4975): the budget deadline, or t0 + cap when the call
    # got the WAIT_CALL_MIN_SECS floor past the deadline. Awake, the tick cap
    # always fires first; it only matters after the host slept mid-call.
    local wall_args=()
    if [ -n "$WAIT_BUDGET_DEADLINE" ]; then
        local wall_stop="$WAIT_BUDGET_DEADLINE"
        if [ $((t0 + cap)) -gt "$wall_stop" ]; then
            wall_stop=$((t0 + cap))
        fi
        wall_args=(--wall-stop "$wall_stop")
    fi
    ecs_exec_run_with_timeout ${wall_args[@]+"${wall_args[@]}"} "$cap" "$@" \
        </dev/null >"$out_file" 2>"$err_file" 9>&- || rc=$?
    local took=$((SECONDS - t0))

    cat "$out_file"
    cat "$err_file" >&2
    rm -f "$out_file" "$err_file"

    if [ "$rc" -eq "$WAIT_CALL_TIMEOUT_RC" ] && [ "$ECS_EXEC_LAST_KILL" = "wall" ]; then
        _wait_budget_note "timed out: ${label} was killed at the budget deadline after ${took}s wall, before its ${cap}s per-call limit ran out on the awake clock, so the host was probably asleep; treating it as a failed poll (#4975)."
    elif [ "$rc" -eq "$WAIT_CALL_TIMEOUT_RC" ]; then
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
