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
# Exit codes:
#   0 — exec agent responded successfully (exit-0 probe)
#   1 — terminal failure (non-retryable AWS error) or deadline exhausted
wait_for_exec_agent_ready() {
    local cluster="$1"
    local task_arn="$2"
    local container="$3"
    local region="$4"
    local timeout_secs="${5:-${EXEC_AGENT_POLL_TIMEOUT_SECS:-120}}"

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
        _stderr_file=$(mktemp)
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
