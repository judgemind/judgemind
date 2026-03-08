#!/usr/bin/env bash
# scripts/preflight.sh — Runtime validation functions for agent workflows.
#
# Source this file, then call individual checks:
#   source scripts/preflight.sh
#   preflight_in_worktree || exit 1
#   preflight_not_on_main || exit 1
#
# Each function prints a diagnostic on failure and returns non-zero.
# All functions are safe to call from any directory.

# --------------------------------------------------------------------------
# preflight_in_worktree
#   Verify that the current directory is inside a git worktree, not the main
#   repo checkout. Prevents agents from accidentally modifying the shared
#   main checkout or using its venv.
# --------------------------------------------------------------------------
preflight_in_worktree() {
    local git_dir
    git_dir=$(git rev-parse --git-dir 2>/dev/null) || {
        echo "PREFLIGHT FAIL: Not inside a git repository." >&2
        return 1
    }

    # In a worktree, --git-dir returns a path ending in .git/worktrees/<name>.
    # In the main repo, it returns just ".git".
    if [[ "$git_dir" == *"/worktrees/"* ]]; then
        return 0
    fi

    echo "PREFLIGHT FAIL: Current directory is the main repo checkout, not a worktree." >&2
    echo "  pwd: $(pwd)" >&2
    echo "  Run scripts/start-worker.sh to create a worktree first." >&2
    return 1
}

# --------------------------------------------------------------------------
# preflight_not_on_main
#   Verify the current branch is not main/master. Prevents accidental
#   commits or pushes to the default branch during task work.
# --------------------------------------------------------------------------
preflight_not_on_main() {
    local branch
    branch=$(git symbolic-ref --short HEAD 2>/dev/null) || {
        echo "PREFLIGHT FAIL: HEAD is detached — not on any branch." >&2
        return 1
    }

    if [[ "$branch" == "main" || "$branch" == "master" ]]; then
        echo "PREFLIGHT FAIL: On branch '$branch'. Task work must happen on a feature branch." >&2
        echo "  Create a worktree with scripts/start-worker.sh, or checkout a feature branch." >&2
        return 1
    fi

    return 0
}

# --------------------------------------------------------------------------
# preflight_branch_fresh
#   Verify the current branch is not behind origin/main. Catches the common
#   mistake of analyzing or modifying stale code.
#   Pass --fetch to also run git fetch origin main first.
# --------------------------------------------------------------------------
preflight_branch_fresh() {
    if [[ "${1:-}" == "--fetch" ]]; then
        git fetch origin main --quiet 2>/dev/null || true
    fi

    local behind
    behind=$(git rev-list --count HEAD..origin/main 2>/dev/null) || {
        echo "PREFLIGHT WARN: Could not determine if branch is behind origin/main." >&2
        return 0  # Don't block if we can't check (e.g., no remote)
    }

    if [[ "$behind" -gt 0 ]]; then
        echo "PREFLIGHT FAIL: Branch is $behind commit(s) behind origin/main." >&2
        echo "  Run: git fetch origin main && git rebase origin/main" >&2
        return 1
    fi

    return 0
}

# --------------------------------------------------------------------------
# preflight_venv_local [path]
#   Verify that a .venv directory exists in the given path (default: pwd)
#   and that it is NOT a symlink to another worktree's venv.
# --------------------------------------------------------------------------
preflight_venv_local() {
    local dir="${1:-.}"
    local venv_path="$dir/.venv"

    if [[ ! -d "$venv_path" ]]; then
        echo "PREFLIGHT FAIL: No .venv found at $venv_path" >&2
        echo "  Create one: python3.12 -m venv $venv_path" >&2
        return 1
    fi

    # Check that .venv is not a symlink pointing outside this directory
    if [[ -L "$venv_path" ]]; then
        local target
        target=$(readlink "$venv_path")
        echo "PREFLIGHT FAIL: .venv at $venv_path is a symlink to $target" >&2
        echo "  Each worktree must have its own venv. Create a fresh one:" >&2
        echo "  rm $venv_path && python3.12 -m venv $venv_path" >&2
        return 1
    fi

    return 0
}

# --------------------------------------------------------------------------
# preflight_no_forbidden_syntax <command_string>
#   Check a command string for forbidden shell patterns. Mirrors the checks
#   in .claude/hooks/preflight-bash.sh but can be called from scripts.
# --------------------------------------------------------------------------
preflight_no_forbidden_syntax() {
    local cmd="$1"

    if echo "$cmd" | grep -qE '\$\('; then
        echo "PREFLIGHT FAIL: Command contains \$() substitution." >&2
        return 1
    fi

    if echo "$cmd" | grep -qE '<<-?[[:space:]]*["'"'"']?[A-Za-z_]+["'"'"']?'; then
        echo "PREFLIGHT FAIL: Command contains a heredoc." >&2
        return 1
    fi

    if echo "$cmd" | grep -qE 'python3?[[:space:]]+-c[[:space:]]'; then
        echo "PREFLIGHT FAIL: Command contains inline python -c." >&2
        return 1
    fi

    return 0
}
