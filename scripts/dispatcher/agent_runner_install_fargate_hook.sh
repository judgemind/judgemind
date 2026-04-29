#!/usr/bin/env bash
# agent_runner_install_fargate_hook.sh — Sourceable helper that swaps
# the Fargate-narrowed preflight hook over the worktree's tracked
# `.claude/hooks/preflight-bash.sh` and `.claude/hooks/preflight_cross_worktree.py`.
#
# Mirrors `daemon.py::_install_fargate_preflight_hook` (lines ~6224-6349)
# verbatim — the daemon-side worktrees (subprocess execution mode) get
# this swap via the Python helper; the agent-runner ECS image needs the
# equivalent in shell because the entrypoint is a Bash script.
#
# Without this swap, ECS-mode ralph workers run with the FULL operator-
# laptop hook ruleset and the `;`+double-quoted-string Bash patterns the
# ralph Step 2.5 pre-push command uses get blocked, producing silent
# `ralph_not_ship` terminals (root cause of #3757).
#
# Contract — env vars read by `install_fargate_preflight_hook`:
#
#   DISPATCHER_FARGATE_HOOKS_DIR  — directory containing the staged
#                                   `preflight-bash.sh` + `preflight_cross_worktree.py`.
#                                   Set by `Dockerfile.dispatcher-agent-runner` to
#                                   `/app/fargate-hooks`. Unset on operator
#                                   laptops → swap is a no-op + warning.
#   REPO_ROOT                     — per-agent clone root. The function operates
#                                   on `$REPO_ROOT/.claude/hooks/`.
#   AGENT_ID                      — used in the structured log line. Optional.
#
# Logging — uses the `log()` function defined by the calling
# entrypoint when present. When sourced standalone (e.g. from tests),
# falls back to a minimal stderr-printer so tests can still assert on
# the log shape.
#
# Why a separate file vs. inline in the entrypoint:
#
#   * Sourceable in tests without dragging in the entrypoint's clone /
#     checkout / phase-loop side effects.
#   * Mirrors the precedent set by `scripts/preflight.sh` — function
#     library + per-check wrappers.
#   * Survives bash 3.2 (no associative arrays, no `mapfile`).

# ── log() shim ─────────────────────────────────────────────────────────────
#
# When sourced from `agent-runner-entrypoint.sh`, `log` is already
# defined and writes structured JSON to fd 3. When sourced from a test
# (or directly), we provide a minimal substitute so callers don't crash
# and tests can assert on the event names.

if ! declare -F log >/dev/null 2>&1; then
    log() {
        # $1 = event name, rest = key=value pairs.
        _ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        _event="$1"
        shift
        _extra=""
        for kv in "$@"; do
            _k=${kv%%=*}
            _v=${kv#*=}
            _v=$(printf '%s' "$_v" | sed 's/"/\\"/g')
            _extra="$_extra, \"$_k\": \"$_v\""
        done
        printf '{"ts": "%s", "event": "agent_runner.%s", "agent_id": "%s"%s}\n' \
            "$_ts" "$_event" "${AGENT_ID:-unknown}" "$_extra"
    }
fi

# ── install_fargate_preflight_hook ─────────────────────────────────────────

install_fargate_preflight_hook() {
    # Validate the contract. `${VAR:-}` keeps `set -u` callers happy.
    local stage_dir="${DISPATCHER_FARGATE_HOOKS_DIR:-}"
    local repo_root="${REPO_ROOT:-}"

    if [[ -z "$stage_dir" ]]; then
        # Operator-laptop fallback. The full hook stays in place — devs
        # see the same prompt-stalling rules they'd see locally. Mirrors
        # daemon.py's `fargate_hook_skip` branch.
        log "fargate_hook_skip" \
            "reason=DISPATCHER_FARGATE_HOOKS_DIR_unset"
        return 0
    fi

    if [[ -z "$repo_root" ]]; then
        log "fargate_hook_skip" \
            "reason=REPO_ROOT_unset"
        return 0
    fi

    local source_hook="$stage_dir/preflight-bash.sh"
    local source_helper="$stage_dir/preflight_cross_worktree.py"

    if [[ ! -f "$source_hook" || ! -f "$source_helper" ]]; then
        # Stage dir set but files missing — rogue local image or test
        # harness mistake. Don't fail; the operator-laptop hook is a
        # safe fallback.
        log "fargate_hook_skip" \
            "reason=stage_files_missing" \
            "stage_dir=$stage_dir"
        return 0
    fi

    local hooks_dir="$repo_root/.claude/hooks"
    local target_hook="$hooks_dir/preflight-bash.sh"
    local target_helper="$hooks_dir/preflight_cross_worktree.py"

    # `git checkout -B "$BRANCH_NAME" origin/main` recreates the tracked
    # `.claude/hooks/` tree so the targets normally exist. Defensive
    # mkdir covers the divergence case without making the common case
    # slower.
    mkdir -p "$hooks_dir"

    # Copy the hook + helper from the stage dir over the tracked
    # files. `cp -p` preserves mode; we also force +x as a belt-and-
    # braces measure (the Dockerfile already chmods +x the source).
    if ! cp -p "$source_hook" "$target_hook" 2>/dev/null; then
        log "fargate_hook_copy_failed" \
            "path=$target_hook"
        return 0
    fi
    chmod +x "$target_hook" 2>/dev/null || true

    if ! cp -p "$source_helper" "$target_helper" 2>/dev/null; then
        log "fargate_hook_copy_failed" \
            "path=$target_helper"
        return 0
    fi

    # `git update-index --skip-worktree` tells git to pretend the file
    # is unchanged. Without this, the diff would show up in every ralph
    # Step 2.5 pre-push run, in `git status`, and in the PR diff (which
    # would fail the paths-filter gate on `.claude/hooks/` and
    # potentially replace the operator-local hook on merge).
    # Best-effort: failure means status/diff carries the swap as a noise
    # change but the hook is still swapped. Mirrors daemon.py.
    if ! git -C "$repo_root" update-index --skip-worktree \
            .claude/hooks/preflight-bash.sh >/dev/null 2>&1; then
        log "fargate_hook_skip_worktree_failed" \
            "path=.claude/hooks/preflight-bash.sh"
    fi

    if ! git -C "$repo_root" update-index --skip-worktree \
            .claude/hooks/preflight_cross_worktree.py >/dev/null 2>&1; then
        log "fargate_hook_skip_worktree_failed" \
            "path=.claude/hooks/preflight_cross_worktree.py"
    fi

    log "fargate_hook_installed" \
        "stage_dir=$stage_dir" \
        "repo_root=$repo_root"

    return 0
}
