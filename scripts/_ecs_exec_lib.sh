# _ecs_exec_lib.sh — sourceable helper for ECS Exec agent readiness polling.
#
# Defines wait_for_exec_agent_ready <cluster> <task_arn> <container> <region>
# [timeout_secs].
#
# This file has no shebang and no `set -e` so it can be sourced safely by
# scripts that already have their own error-handling settings.
#
# Naming convention: leading underscore marks this as a sourceable helper (not
# an executable test), consistent with scripts/dispatcher/helpers/_query_lib.sh
# and scripts/tests/_guard_self_match_helpers.sh.
#
# Usage:
#   source "$(dirname "${BASH_SOURCE[0]}")/_ecs_exec_lib.sh"
#   wait_for_exec_agent_ready "$CLUSTER" "$task_arn" "$CONTAINER" "$REGION" || exit 1
#
# Also defines:
#   ecs_exec_run_with_timeout <secs> <cmd...>   — portable per-call timeout
#   ecs_exec_print_fallback_fix                 — Fix: block naming the
#                                                 scripts/ecs-run-task.sh fallback

# Exit code ecs_exec_run_with_timeout returns when it had to kill the command.
# Matches GNU timeout(1) so callers can reason about it the same way.
ECS_EXEC_TIMEOUT_RC=124

# ecs_exec_run_with_timeout <secs> <cmd> [args...]
#
# Runs <cmd> with a hard wall-clock deadline of <secs> seconds (#4665). If the
# command has not exited by then, it and its direct children (e.g. the
# session-manager-plugin process that `aws ecs execute-command` spawns) are
# sent SIGTERM, then SIGKILL two seconds later, and the function returns 124.
# Otherwise it returns the command's own exit status.
#
# Why not timeout(1)/gtimeout(1): macOS has no GNU timeout by default, and
# GNU timeout moves the child into a background process group, which makes an
# `--interactive` SSM session reading from a TTY stop on SIGTTIN. The watchdog
# below is plain bash and behaves the same on macOS and Linux.
#
# The command's stdin/stdout/stderr are the caller's. The watchdog's own fds
# go to /dev/null, so a caller that captures stdout with $(...) doesn't
# block on the watchdog's leftover `sleep` holding the pipe open.
ecs_exec_run_with_timeout() {
    local secs="$1"
    shift

    local marker_dir
    marker_dir=$(mktemp -d)
    local marker="$marker_dir/timed_out"

    # Explicit `<&0` keeps the caller's stdin: without it bash would point a
    # background job's stdin at /dev/null, which ends an --interactive SSM
    # session immediately.
    "$@" <&0 &
    local cmd_pid=$!

    (
        waited=0
        while kill -0 "$cmd_pid" 2>/dev/null; do
            if [ "$waited" -ge "$secs" ]; then
                : > "$marker"
                pkill -TERM -P "$cmd_pid" 2>/dev/null
                kill -TERM "$cmd_pid" 2>/dev/null
                sleep 2
                pkill -KILL -P "$cmd_pid" 2>/dev/null
                kill -KILL "$cmd_pid" 2>/dev/null
                exit 0
            fi
            sleep 1
            waited=$((waited + 1))
        done
    ) </dev/null >/dev/null 2>&1 &

    local rc=0
    wait "$cmd_pid" 2>/dev/null || rc=$?

    if [[ -e "$marker" ]]; then
        rm -rf "$marker_dir"
        return "$ECS_EXEC_TIMEOUT_RC"
    fi
    rm -rf "$marker_dir"
    return "$rc"
}

# ecs_exec_print_fallback_fix
#
# Prints (to stderr) a copy-pasteable Fix: block pointing at the
# scripts/ecs-run-task.sh one-off fallback. That path launches a fresh Fargate
# task through `aws ecs run-task` and never touches ECS Exec/SSM, so it still
# works when the Exec session to the running ingestion-worker task is wedged.
ecs_exec_print_fallback_fix() {
    cat >&2 <<'FIX'
Fix: ECS Exec to this task is stalled (often right after a rollout). Either
  - retry in a minute or two, after the new task settles, or
  - run the query as a one-off Fargate task, which does not use ECS Exec:
      1. write a small psycopg script, e.g. tmp/q.py:
           import os, psycopg
           with psycopg.connect(os.environ["DATABASE_URL"]) as c:
               for row in c.execute("SELECT count(*) FROM derived.rulings"):
                   print(row)
      2. scripts/ecs-run-task.sh tmp/q.py
  Timeouts are tunable: EXEC_PROBE_TIMEOUT_SECS (per probe, default 30),
  EXEC_AGENT_POLL_TIMEOUT_SECS (probe deadline, default 120),
  EXEC_QUERY_TIMEOUT_SECS (query, default 300).
FIX
}

# wait_for_exec_agent_ready <cluster> <task_arn> <container> <region> [timeout_secs]
#
# Polls `aws ecs execute-command` with a harmless `bash -c 'true'` probe until
# the ECS Exec agent on the target task is ready. This bridges the gap between
# `list-tasks` returning a running ARN and the execute-command agent actually
# being reachable — which can be 30-90 s after a rolling deploy.
#
# Arguments:
#   cluster       — ECS cluster name
#   task_arn      — full task ARN (arn:aws:ecs:...)
#   container     — container name
#   region        — AWS region
#   timeout_secs  — polling deadline in seconds (default: $EXEC_AGENT_POLL_TIMEOUT_SECS
#                   or 120 if unset)
#
# Each probe is also capped at $EXEC_PROBE_TIMEOUT_SECS (default 30), clamped
# to the time left before the deadline (5 s floor). A probe that hangs is
# killed and treated as "not ready" (#4665), so the whole call returns within
# roughly timeout_secs + 10 s even when the SSM session wedges.
#
# Exit codes:
#   0 — exec agent responded successfully (exit-0 probe)
#   1 — terminal failure (non-retryable AWS error) or deadline exhausted
#       (the deadline path prints a Fix: block naming scripts/ecs-run-task.sh)
wait_for_exec_agent_ready() {
    local cluster="$1"
    local task_arn="$2"
    local container="$3"
    local region="$4"
    local timeout_secs="${5:-${EXEC_AGENT_POLL_TIMEOUT_SECS:-120}}"
    local probe_timeout="${EXEC_PROBE_TIMEOUT_SECS:-30}"

    local _start
    _start=$(date +%s)
    local _deadline
    _deadline=$((_start + timeout_secs))

    while true; do
        local _probe_stderr
        local _probe_rc=0
        # Capture stderr so we can distinguish retryable from terminal errors.
        # Use a temp file because bash <() process substitution is not allowed
        # by the preflight hook, and we need both exit-code and stderr.
        local _stderr_file
        # Never let a single probe run much past the overall deadline: clamp
        # it to the time remaining, with a 5 s floor so a probe that would
        # succeed still gets a fair chance.
        local _probe_budget=$probe_timeout
        local _remaining=$((_deadline - $(date +%s)))
        if [[ $_remaining -lt $_probe_budget ]]; then
            _probe_budget=$_remaining
        fi
        if [[ $_probe_budget -lt 5 ]]; then
            _probe_budget=5
        fi

        _stderr_file=$(mktemp)
        # Bound every probe (#4665). An SSM session can open ("Starting
        # session with SessionId: ...") and then never return. Without a
        # per-call timeout the loop never gets back to the deadline check
        # and the caller hangs forever.
        ecs_exec_run_with_timeout "$_probe_budget" \
            aws ecs execute-command \
            --cluster "$cluster" \
            --task "$task_arn" \
            --container "$container" \
            --interactive \
            --region "$region" \
            --command "bash -c 'true'" \
            >/dev/null 2>"$_stderr_file" || _probe_rc=$?

        _probe_stderr=$(cat "$_stderr_file")
        rm -f "$_stderr_file"

        if [[ $_probe_rc -eq 0 ]]; then
            # Exec agent responded — we're clear to run the real command.
            return 0
        fi

        # A stalled probe counts as "not ready": retry until the deadline.
        if [[ $_probe_rc -eq $ECS_EXEC_TIMEOUT_RC ]]; then
            local _now
            _now=$(date +%s)
            local _elapsed
            _elapsed=$((_now - _start))
            if [[ "$_now" -ge "$_deadline" ]]; then
                printf 'Error: ECS Exec session to task %s stalled: the readiness probe hung past its %ds per-call timeout and the %ds deadline is exhausted (%ds elapsed).\n' \
                    "$task_arn" "$_probe_budget" "$timeout_secs" "$_elapsed" >&2
                ecs_exec_print_fallback_fix
                return 1
            fi
            printf 'exec probe on %s timed out after %ds (SSM session stalled, %ds elapsed), retrying in 5s...\n' \
                "$task_arn" "$_probe_budget" "$_elapsed" >&2
            sleep 5
            continue
        fi

        # Retryable only when the error is InvalidParameterException with the
        # specific "execute command agent" not-running message.  The AWS CLI
        # uses "is not running" (not the contraction "isn't running"), but
        # match both to be forward-compatible.
        if printf '%s' "$_probe_stderr" | grep -q "InvalidParameterException" \
            && printf '%s' "$_probe_stderr" | grep -qE "execute command agent (isn't|is not) running"; then
            local _now
            _now=$(date +%s)
            if [[ "$_now" -ge "$_deadline" ]]; then
                local _elapsed
                _elapsed=$((_now - _start))
                printf 'ECS exec agent on task %s did not come up within %ds — task definition may not have ECS Exec enabled, or the rollout is still stabilizing. Retry in ~1 minute or run '"'"'aws ecs describe-tasks'"'"' to inspect.\n' \
                    "$task_arn" "$_elapsed" >&2
                ecs_exec_print_fallback_fix
                return 1
            fi
            local _elapsed
            _elapsed=$((_now - _start))
            printf 'exec agent on %s not ready (%ds elapsed), retrying in 5s...\n' \
                "$task_arn" "$_elapsed" >&2
            sleep 5
        else
            # Terminal failure (permissions, throttling, wrong cluster, etc.)
            printf '%s\n' "$_probe_stderr" >&2
            return 1
        fi
    done
}
