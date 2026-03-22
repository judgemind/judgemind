#!/usr/bin/env bash
# Safely clean up a completed agent worktree.
#
# Validates that the agent has truly finished by checking its JSONL session
# log before removing the worktree directory. Because this is a shell script
# (not Python), the final "cd" actually affects the parent Bash process,
# preventing pwd failures after the worktree directory is deleted.
#
# Usage:
#     scripts/cleanup_worktree.sh <worktree-path>
#     scripts/cleanup_worktree.sh .claude/worktrees/agent-a9d29d9e
#
# Exit codes:
#     0: success (removed or already gone)
#     1: error (agent still running, no session log, bad path, removal failed)
#
# Safety checks:
#     1. Path must be under .claude/worktrees/agent-*
#     2. Agent session JSONL must exist
#     3. Last JSONL entry must be an assistant message with text (= agent finished)
#     4. If checks pass, git worktree remove --force, falling back to rm -rf + prune
#     5. cd to repo root before exiting so the parent shell's cwd is valid

set -euo pipefail

# --- Find repo root ---
find_repo_root() {
    # Walk up from this script's location to find the repo root.
    # We deliberately do NOT use "git rev-parse --show-toplevel" because
    # inside a worktree it returns the worktree path, not the main repo root.
    # The script file itself always lives under the actual repo (or worktree),
    # and CLAUDE.md exists at the root of both.
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    while [[ "$dir" != "/" ]]; do
        if [[ -f "$dir/CLAUDE.md" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done

    echo "ERROR: cannot find repo root" >&2
    return 1
}

# --- Find session JSONL for an agent ID (prefix match) ---
find_session_jsonl() {
    local agent_id="$1"
    local subagents_dir="$HOME/.claude/projects"

    if [[ ! -d "$subagents_dir" ]]; then
        return 1
    fi

    # Agent ID in worktree path is truncated (8 chars).
    # JSONL filename uses the full ID. Use find for prefix match.
    local jsonl
    jsonl="$(find "$subagents_dir" -name "agent-${agent_id}*.jsonl" -print -quit 2>/dev/null)"

    if [[ -n "$jsonl" ]]; then
        echo "$jsonl"
        return 0
    fi
    return 1
}

# --- Check if agent is finished by inspecting the JSONL session log ---
# Uses jq to parse the last entry. Returns 0 if finished, 1 if still running.
# Prints a reason to stderr.
agent_is_finished() {
    local jsonl_path="$1"

    if [[ ! -s "$jsonl_path" ]]; then
        echo "session log is empty" >&2
        return 1
    fi

    # Read the last non-empty line
    local last_line
    last_line="$(tail -1 "$jsonl_path")"

    if [[ -z "$last_line" ]]; then
        echo "no entries in session log" >&2
        return 1
    fi

    # Parse with jq: check if last entry is an assistant message with text content
    local role has_text
    role="$(echo "$last_line" | jq -r '.message.role // empty' 2>/dev/null)" || true
    if [[ "$role" != "assistant" ]]; then
        local entry_type
        entry_type="$(echo "$last_line" | jq -r '.type // .message.role // "unknown"' 2>/dev/null)" || true
        echo "last entry type is '${entry_type}' (agent may still be running)" >&2
        return 1
    fi

    # Check if the assistant message has text content (not just tool calls)
    has_text="$(echo "$last_line" | jq -r '
        [.message.content[]? | select(.type == "text" and (.text | length > 0))] | length > 0
    ' 2>/dev/null)" || true

    if [[ "$has_text" == "true" ]]; then
        echo "last entry is assistant message with text" >&2
        return 0
    else
        echo "last entry is assistant message with only tool calls (still working)" >&2
        return 1
    fi
}

# --- Remove the worktree ---
remove_worktree() {
    local repo_root="$1"
    local worktree_path="$2"

    # cd to repo root first so we don't have cwd inside the directory being deleted
    cd "$repo_root"

    # Try git worktree remove --force
    if git -C "$repo_root" worktree remove --force "$worktree_path" 2>/dev/null; then
        echo "Removed worktree via git: $worktree_path" >&2
        return 0
    fi

    echo "git worktree remove failed, falling back to rm + prune" >&2

    # Fallback: rm -rf + git worktree prune
    if rm -rf "$worktree_path"; then
        git -C "$repo_root" worktree prune 2>/dev/null || true
        echo "Removed worktree via rm + prune: $worktree_path" >&2
        return 0
    fi

    echo "ERROR: failed to remove worktree: $worktree_path" >&2
    return 1
}

# --- Main ---
main() {
    if [[ $# -lt 1 ]]; then
        echo "Usage: scripts/cleanup_worktree.sh <worktree-path>" >&2
        return 1
    fi

    local worktree_arg="$1"
    local repo_root
    repo_root="$(find_repo_root)" || exit 1

    # Resolve worktree path (allow relative to repo root)
    local worktree_path
    if [[ "$worktree_arg" == /* ]]; then
        worktree_path="$worktree_arg"
    else
        worktree_path="$repo_root/$worktree_arg"
    fi
    # Resolve to canonical path (handle symlinks, .., etc.)
    # realpath fails on nonexistent paths on macOS — fall back to the unresolved path
    local resolved
    if command -v realpath >/dev/null 2>&1; then
        resolved="$(realpath "$worktree_path" 2>/dev/null)" && worktree_path="$resolved"
    elif command -v python3 >/dev/null 2>&1; then
        resolved="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$worktree_path" 2>/dev/null)" && worktree_path="$resolved"
    fi

    # Safety check 1: must be under .claude/worktrees/agent-*
    local worktrees_dir="$repo_root/.claude/worktrees"
    case "$worktree_path" in
        "$worktrees_dir"/agent-*)
            ;;
        *)
            echo "ERROR: path is not under .claude/worktrees/agent-*: $worktree_path" >&2
            return 1
            ;;
    esac

    # Extract agent ID (truncated, 8 hex chars) from the directory name
    local dir_name agent_id
    dir_name="$(basename "$worktree_path")"
    agent_id="${dir_name#agent-}"

    # Check if worktree still exists
    if [[ ! -d "$worktree_path" ]]; then
        echo "Worktree already removed: $worktree_path" >&2
        return 0
    fi

    # Safety check 2: find session log
    local jsonl_path
    if ! jsonl_path="$(find_session_jsonl "$agent_id")"; then
        echo "ERROR: no session log found for agent-${agent_id}. Cannot verify agent completion." >&2
        return 1
    fi

    # Safety check 3: verify agent is finished
    local reason
    if reason="$(agent_is_finished "$jsonl_path" 2>&1)"; then
        echo "Agent confirmed finished: $reason" >&2
    else
        echo "ERROR: agent appears to still be running: $reason" >&2
        return 1
    fi

    # Remove the worktree (this also cd's to repo root)
    if remove_worktree "$repo_root" "$worktree_path"; then
        return 0
    fi
    return 1
}

main "$@"
