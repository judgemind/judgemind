#!/usr/bin/env bash
# preflight_shared_checks.sh — the safety checks shared by BOTH Bash
# PreToolUse hooks (#4703):
#
#   - .claude/hooks/preflight-bash.sh   (operator laptops, full ruleset)
#   - scripts/preflight-bash-fargate.sh (Fargate daemon / agent-runner,
#                                        safety rules only)
#
# This file is the ONE place those checks live. Before #4703 each hook
# carried a hand-copied version and nothing kept them in sync (#4683 had
# to edit both copies of the stash check). Fix a shared check here and
# both environments pick it up; never re-inline one of these checks into
# either hook. scripts/tests/test_preflight_bash_fargate.py -k parity
# fails CI if a hook stops sourcing this file, stops calling one of the
# checks, or grows its own inline copy.
#
# Sourced, never executed. Callers set these globals first:
#
#   COMMAND           the Bash tool command string
#   STRIPPED_COMMAND  COMMAND with '...' / "..." runs removed
#   EFFECTIVE_CWD     the cwd to reason about (tests override it via
#                     PREFLIGHT_CWD)
#
# Each preflight_check_* function prints a BLOCKED message on stderr and
# returns 2 to block, or returns 0 to allow. Callers do:
#
#   preflight_check_push_to_main || exit $?
#
# preflight_run_shared_checks runs all four in the canonical order.
#
# How each environment finds this file:
#   - Operator laptops: next to preflight-bash.sh in .claude/hooks/.
#   - Fargate: the Dockerfiles stage it in /app/fargate-hooks/ with the
#     Fargate hook and preflight_cross_worktree.py. daemon.py and
#     scripts/dispatcher/agent_runner_install_fargate_hook.sh copy all
#     three into the worktree's .claude/hooks/, so the swapped-in hook
#     finds it next to itself, at the image's version.
#
# preflight_cross_worktree.py is found relative to this file, since both
# always sit together in .claude/hooks/.
#
# Uses grep -E (POSIX ERE) for macOS compatibility. Do NOT use grep -P.
# Must run under bash 3.2 (macOS /bin/bash) with `set -u`.

PREFLIGHT_SHARED_DIR="$(dirname "${BASH_SOURCE[0]}")"

# Regex for a `git stash pop|apply` invocation, including `git -C <path>`.
# Known bypasses (`git -c k=v stash pop`, `git --git-dir=... stash pop`,
# etc.) are tracked in #4741. Widen it here, and only here.
PREFLIGHT_STASH_APPLY_RE='\bgit\b(\s+-C\s+\S+)?\s+stash\s+(pop|apply)\b'

# 0. Push to main/master — block during task work.
#    Catches: git push origin main, git push -u origin main, git -C /path push origin main
#    The regex requires "push" as a git subcommand (after git or git -C <path>),
#    not just anywhere in the command. This avoids false positives on commands like
#    "git add .githooks/pre-push" where "push" appears in a filename.
preflight_check_push_to_main() {
    if echo "$COMMAND" | grep -qE '\bgit\b(\s+-C\s+\S+)?\s+push\b' ; then
        # Extract what looks like the branch being pushed (last word, or after "origin")
        if echo "$COMMAND" | grep -qE '\bpush\b.*\b(main|master)\b' ; then
            echo "BLOCKED: Pushing directly to main/master is not allowed during task work. Push to a feature branch and open a PR. See CLAUDE.md §Git Workflow." >&2
            return 2
        fi
        # Also catch bare "git push" when on main (check current branch)
        local current_branch
        current_branch=$(git symbolic-ref --short HEAD 2>/dev/null || echo "")
        if [[ "$current_branch" == "main" || "$current_branch" == "master" ]]; then
            # Only block if the push target looks like it includes the current branch
            # A bare "git push" while on main pushes to main
            if ! echo "$COMMAND" | grep -qE '\bpush\b.*\b[a-z]+-' ; then
                echo "BLOCKED: You are on '$current_branch' and running git push. Push to a feature branch instead. See CLAUDE.md §Git Workflow." >&2
                return 2
            fi
        fi
    fi
    return 0
}

# 10. git worktree add inside an existing worktree.
#     Subagents must work in their assigned worktree only. Creating child worktrees
#     causes orphaned worktrees that cleanup_worktree.sh cannot track and the
#     dispatcher does not know about. If the worktree is in a bad state, the agent
#     should fix it (git checkout -- ., git clean -fd) instead of creating a new one.
#     Detection: command is "git worktree add" AND cwd contains ".claude/worktrees/".
preflight_check_worktree_add() {
    if echo "$COMMAND" | grep -qE '\bgit\b(\s+-C\s+\S+)?\s+worktree\s+add\b' ; then
        if echo "$EFFECTIVE_CWD" | grep -qE '\.claude/worktrees/' ; then
            echo "BLOCKED: git worktree add is not allowed inside an existing worktree. Subagents must work in their assigned worktree only. If your worktree is in a bad state, fix it with 'git checkout -- .' or 'git clean -fd'. See CLAUDE.md §Critical Rules." >&2
            return 2
        fi
    fi
    return 0
}

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
#     Delegates command parsing to preflight_cross_worktree.py (next to this
#     file) — the parsing is non-trivial and easier to test in isolation.
preflight_check_cross_worktree_write() {
    case "$EFFECTIVE_CWD" in
        */.claude/worktrees/*)
            local repo_root rest wt_id wt_root msg
            repo_root="${EFFECTIVE_CWD%%/.claude/worktrees/*}"
            rest="${EFFECTIVE_CWD#"$repo_root"/.claude/worktrees/}"
            wt_id="${rest%%/*}"
            wt_root="$repo_root/.claude/worktrees/$wt_id"
            msg=$(
                COMMAND="$COMMAND" \
                REPO_ROOT="$repo_root" \
                WORKTREE_ROOT="$wt_root" \
                python3 "$PREFLIGHT_SHARED_DIR/preflight_cross_worktree.py" 2>/dev/null
            )
            if [ -n "$msg" ]; then
                echo "$msg" >&2
                return 2
            fi
            ;;
    esac
    return 0
}

# 12. Bare `git stash pop` / `git stash apply` — cross-worktree stash pollution.
#     `git stash` is a per-clone global stack, not per-worktree. All worktrees
#     share $GIT_DIR/refs/stash, so a `git stash pop` in one worktree can
#     silently apply a stash created by another worktree (or by a long-gone
#     agent). This causes two failure modes:
#       (a) the current worktree's edits vanish (replaced by the other stash);
#       (b) another agent's uncommitted WIP lands in this worktree and can be
#           staged into the next commit via `git add -A` or similar.
#
#     Both were observed during #2746 (see #2749). The fix is to require an
#     explicit reference so the agent has demonstrably identified which stash
#     it wants to apply.
#
#     Detection:
#       - Command (outside quoted strings) contains `git stash pop` or `git
#         stash apply` — including `git -C <path> stash pop|apply`.
#       - Each such invocation (its args up to the next `;`, `&`, `|`, `)` or
#         newline) is checked independently. It is allowed only if one of
#         its positional args is an explicit ref: `stash@{<digits>}`, or a
#         full/abbreviated commit SHA (7-40 hex chars) — e.g. the SHA
#         captured via `git stash list --format='%H %gs'` (#4683).
#       - Flags (`--index`, `--quiet`) are not refs. A ref belonging to a
#         different subcommand in the same command line (e.g.
#         `git stash pop; git stash drop stash@{0}`) does not excuse the bare
#         pop.
#       - Allow `git stash show`, `git stash list`, `git stash push`, `git
#         stash drop` — these are not the affected verbs.
#
#     Uses $STRIPPED_COMMAND (quoted substrings removed) so that the string
#     "git stash pop" appearing inside a quoted argument — e.g. a PR title
#     like `gh pr create --title "block git stash pop"` — does not falsely
#     trigger the check.
#
#     Safer alternatives (see CLAUDE.md and docs/agent/unattended-patterns.md):
#       1. Use `git stash list` to confirm stash@{0}'s subject matches the
#          current branch, then `git stash pop stash@{0}` with the explicit ref
#          — or `git stash apply <sha>` with the stash commit's SHA.
#       2. Prefer a throwaway commit over stash: `git commit -am "WIP" && ...
#          && git reset --soft HEAD~1`. No shared global state.
preflight_check_bare_stash_apply() {
    if echo "$STRIPPED_COMMAND" | grep -qE "$PREFLIGHT_STASH_APPLY_RE" ; then
        local stash_bare=0 stash_invocation stash_has_ref stash_arg
        while IFS= read -r stash_invocation; do
            stash_has_ref=0
            # Drop everything through the pop/apply verb, then walk the args.
            while IFS= read -r stash_arg; do
                if printf '%s' "$stash_arg" | grep -qE '^(stash@\{[0-9]+\}|[0-9a-fA-F]{7,40})$' ; then
                    stash_has_ref=1
                fi
            done < <(printf '%s\n' "$stash_invocation" | sed -E 's/^.*[[:space:]]stash[[:space:]]+(pop|apply)//' | tr -s ' \t' '\n\n')
            if [ "$stash_has_ref" -eq 0 ]; then
                stash_bare=1
            fi
        done < <(printf '%s\n' "$STRIPPED_COMMAND" | grep -oE "${PREFLIGHT_STASH_APPLY_RE}[^;&|)]*")
        if [ "$stash_bare" -eq 1 ]; then
            echo "BLOCKED: Bare 'git stash pop' / 'git stash apply' is not allowed. The stash list is shared across all worktrees in this clone, so a bare pop can silently apply another agent's or another worktree's stash — reverting your edits and dumping their WIP into your worktree (see #2749). Run 'git stash list' first, confirm the stash's subject matches your current branch, then pop it by explicit ref: 'git stash pop stash@{N}' or 'git stash apply <sha>'. Every pop/apply in the command needs its own ref. Or use a throwaway commit instead (git commit -am 'WIP' / git reset --soft HEAD~1). See CLAUDE.md §Unattended Operation Patterns." >&2
            return 2
        fi
    fi
    return 0
}

# All four shared checks in canonical order. The Fargate hook calls this.
# The operator hook calls the four functions one by one, at the positions
# the checks have always held among its other rules, so the message a
# multi-rule command gets is unchanged.
preflight_run_shared_checks() {
    preflight_check_push_to_main || return $?
    preflight_check_worktree_add || return $?
    preflight_check_cross_worktree_write || return $?
    preflight_check_bare_stash_apply || return $?
    return 0
}
