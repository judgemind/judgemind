#!/usr/bin/env bash
# PreToolUse hook for Bash tool — Fargate dispatcher container edition.
#
# Narrow replacement for .claude/hooks/preflight-bash.sh when the hook is
# running inside the Fargate dispatcher container. Compared to the
# operator-local hook, this one drops all the rules whose purpose was to
# prevent the *interactive* Claude CLI from stalling on permission prompts.
# In the Fargate container, subagent Claude processes run with
# `--dangerously-skip-permissions`, so there are no prompts to stall on —
# those rules become pure drag (issue #2982).
#
# Kept (4 rules, all real safety):
#
#   - `git push` to main/master                     — branch-protection proxy
#   - `git worktree add` inside an existing worktree — orphan-worktree prevention
#   - cross-worktree writes into the main repo       — bypass of PR workflow (#2440)
#   - bare `git stash pop` / `git stash apply`       — cross-worktree stash pollution (#2749)
#
# Dropped (6 rules, all interactive-prompt-prevention):
#
#   - `$(...)` command substitution
#   - `<<EOF` heredocs
#   - `python -c "..."` inline scripts
#   - quoted strings combined with `&&` / `;`
#   - `cd` in compound commands
#   - empty-quotes bypass patterns
#   - long-running commands without sufficient timeout
#   - `run_in_background` inside worktree subagents
#   - terraform apply/destroy from the root infra path (no operator sessions in Fargate)
#
# Those rules still apply on an operator's laptop — this file is installed
# over .claude/hooks/preflight-bash.sh inside the Fargate image only (see
# Dockerfile.dispatcher), leaving the operator-local hook untouched.
#
# This hook receives the tool input as JSON on stdin. It extracts the "command"
# field and checks it against the 4 safety patterns.
#
# Exit 0 = allow, exit 2 = block with message on stderr.

set -uo pipefail

# Read the JSON input from stdin
INPUT=$(cat)

# Extract the command field using python3 (always available on the container).
COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('command',''))" 2>/dev/null)

if [ -z "$COMMAND" ]; then
    # Can't parse — let it through, don't block on hook errors
    exit 0
fi

# Allow tests to override the working directory for the worktree-aware checks.
EFFECTIVE_CWD="${PREFLIGHT_CWD:-$PWD}"

# Produce a copy of $COMMAND with single- and double-quoted substrings removed.
# Used by the stash check so the literal string "git stash pop" inside a PR
# title or commit message does not false-positive. Same approach as the
# operator-local hook.
STRIPPED_COMMAND=$(printf '%s' "$COMMAND" | sed -E "s/'[^']*'//g; s/\"[^\"]*\"//g")

# --- Forbidden pattern checks ---
# Note: uses grep -E (POSIX extended regex). Do NOT use grep -P.

# 0. Push to main/master — block during task work.
#    Catches: git push origin main, git push -u origin main, git -C /path push origin main
#    The regex requires "push" as a git subcommand (after git or git -C <path>),
#    not just anywhere in the command. This avoids false positives on commands like
#    "git add .githooks/pre-push" where "push" appears in a filename.
if echo "$COMMAND" | grep -qE '\bgit\b(\s+-C\s+\S+)?\s+push\b' ; then
    # Extract what looks like the branch being pushed (last word, or after "origin")
    if echo "$COMMAND" | grep -qE '\bpush\b.*\b(main|master)\b' ; then
        echo "BLOCKED: Pushing directly to main/master is not allowed during task work. Push to a feature branch and open a PR. See CLAUDE.md §Git Workflow." >&2
        exit 2
    fi
    # Also catch bare "git push" when on main (check current branch)
    current_branch=$(git symbolic-ref --short HEAD 2>/dev/null || echo "")
    if [[ "$current_branch" == "main" || "$current_branch" == "master" ]]; then
        # Only block if the push target looks like it includes the current branch
        # A bare "git push" while on main pushes to main
        if ! echo "$COMMAND" | grep -qE '\bpush\b.*\b[a-z]+-' ; then
            echo "BLOCKED: You are on '$current_branch' and running git push. Push to a feature branch instead. See CLAUDE.md §Git Workflow." >&2
            exit 2
        fi
    fi
fi

# 10. git worktree add inside an existing worktree.
#     Subagents must work in their assigned worktree only. Creating child worktrees
#     causes orphaned worktrees that cleanup_worktree.sh cannot track and the
#     dispatcher does not know about. If the worktree is in a bad state, the agent
#     should fix it (git checkout -- ., git clean -fd) instead of creating a new one.
#     Detection: command is "git worktree add" AND cwd contains ".claude/worktrees/".
if echo "$COMMAND" | grep -qE '\bgit\b(\s+-C\s+\S+)?\s+worktree\s+add\b' ; then
    if echo "$EFFECTIVE_CWD" | grep -qE '\.claude/worktrees/' ; then
        echo "BLOCKED: git worktree add is not allowed inside an existing worktree. Subagents must work in their assigned worktree only. If your worktree is in a bad state, fix it with 'git checkout -- .' or 'git clean -fd'. See CLAUDE.md §Critical Rules." >&2
        exit 2
    fi
fi

# 11. Cross-worktree writes via Bash (cp/mv/tar/redirection).
#     Extends worktree-write-guard.sh (which covers Edit/Write) to the Bash tool.
#     When a worktree subagent runs a command that writes into the main repo
#     checkout but outside its own worktree, block — that bypasses the PR
#     workflow the same way Edit/Write would. See issue #2455.
#
#     Detection (only active when cwd is inside .claude/worktrees/<id>/):
#       - cp / mv: any positional argument that is an absolute path inside
#         $REPO_ROOT/ (conservative — cp/mv both have a destination as the
#         last arg, but checking all absolute-path args catches unusual shapes).
#       - tar with -C <dir> or --directory=<dir>: <dir> is the destination.
#       - Shell redirection > <path> / >> <path>: <path> is the destination.
#
#     Only absolute paths are checked. Relative paths resolve against CWD
#     (the worktree), so they can't escape into the main repo.
#
#     Allowed absolute destinations:
#       - inside $WORKTREE_ROOT/
#       - inside $REPO_ROOT/tmp/ (cross-worktree status files, etc.)
#       - outside $REPO_ROOT/ entirely (e.g. /tmp, /var, /Users/x/other-repo)
#
#     Blocked: inside $REPO_ROOT/ but outside the above allowlists.
#
#     Delegates command parsing to preflight_cross_worktree.py for
#     maintainability — the parsing is non-trivial and easier to test in
#     isolation. The helper lives next to the operator-local hook in
#     `.claude/hooks/preflight_cross_worktree.py` inside the container image
#     (Dockerfile.dispatcher COPYs the hooks tree unchanged, so the helper
#     ships alongside whichever preflight-bash.sh variant is in place).
case "$EFFECTIVE_CWD" in
    */.claude/worktrees/*)
        CROSS_WT_REPO_ROOT="${EFFECTIVE_CWD%%/.claude/worktrees/*}"
        CROSS_WT_REST="${EFFECTIVE_CWD#"$CROSS_WT_REPO_ROOT"/.claude/worktrees/}"
        CROSS_WT_ID="${CROSS_WT_REST%%/*}"
        CROSS_WT_ROOT="$CROSS_WT_REPO_ROOT/.claude/worktrees/$CROSS_WT_ID"
        HOOK_DIR="$(dirname "$0")"
        # The Python helper lives under .claude/hooks/ next to the operator-
        # local hook file — resolve it relative to this script's directory
        # using the same conventions the operator-local hook uses. Inside the
        # Fargate image the hook is installed at .claude/hooks/preflight-bash.sh,
        # so $HOOK_DIR is .claude/hooks/ and the helper is alongside it. When
        # this script is invoked directly from scripts/ for testing, fall back
        # to the repo's .claude/hooks/ location.
        if [ -f "$HOOK_DIR/preflight_cross_worktree.py" ]; then
            CROSS_WT_HELPER="$HOOK_DIR/preflight_cross_worktree.py"
        else
            CROSS_WT_HELPER="$(dirname "$HOOK_DIR")/.claude/hooks/preflight_cross_worktree.py"
        fi
        CROSS_WT_MSG=$(
            COMMAND="$COMMAND" \
            REPO_ROOT="$CROSS_WT_REPO_ROOT" \
            WORKTREE_ROOT="$CROSS_WT_ROOT" \
            python3 "$CROSS_WT_HELPER" 2>/dev/null
        )
        if [ -n "$CROSS_WT_MSG" ]; then
            echo "$CROSS_WT_MSG" >&2
            exit 2
        fi
        ;;
esac

# 12. Bare `git stash pop` / `git stash apply` — cross-worktree stash pollution.
#     `git stash` is a per-clone global stack, not per-worktree. All worktrees
#     share $GIT_DIR/refs/stash, so a `git stash pop` in one worktree can
#     silently apply a stash created by another worktree. See #2749.
#
#     Detection:
#       - Command (outside quoted strings) contains `git stash pop` or `git
#         stash apply` — including `git -C <path> stash pop|apply`.
#       - No positional argument matches `stash@{<digits>}`.
#       - Allow `git stash show`, `git stash list`, `git stash push`, `git
#         stash drop` — these are not the affected verbs.
if echo "$STRIPPED_COMMAND" | grep -qE '\bgit\b(\s+-C\s+\S+)?\s+stash\s+(pop|apply)\b' ; then
    if ! echo "$STRIPPED_COMMAND" | grep -qE 'stash@\{[0-9]+\}' ; then
        echo "BLOCKED: Bare 'git stash pop' / 'git stash apply' is not allowed. The stash list is shared across all worktrees in this clone, so a bare pop can silently apply another agent's or another worktree's stash — reverting your edits and dumping their WIP into your worktree (see #2749). Run 'git stash list' first, confirm the stash's subject matches your current branch, then pop it by explicit ref: 'git stash pop stash@{N}'. Or use a throwaway commit instead (git commit -am 'WIP' / git reset --soft HEAD~1). See CLAUDE.md §Unattended Operation Patterns." >&2
        exit 2
    fi
fi

# All checks passed
exit 0
