#!/usr/bin/env bash
# PreToolUse hook for Bash tool: rejects commands containing forbidden patterns
# that trigger interactive prompts and break autonomous workflows.
#
# This hook receives the tool input as JSON on stdin. It extracts the "command"
# field and checks it against the forbidden patterns documented in CLAUDE.md
# §Unattended Operation Patterns.
#
# Exit 0 = allow, exit 2 = block with message on stderr.

set -uo pipefail

# Read the JSON input from stdin
INPUT=$(cat)

# Extract the command field using python3 (always available on macOS and our CI).
COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('command',''))" 2>/dev/null)

if [ -z "$COMMAND" ]; then
    # Can't parse — let it through, don't block on hook errors
    exit 0
fi

# Extract timeout and run_in_background for check 8 (long-running commands).
TIMEOUT=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); t=d.get('tool_input',{}).get('timeout'); print(t if t is not None else 'none')" 2>/dev/null)
RUN_IN_BG=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(str(d.get('tool_input',{}).get('run_in_background', False)).lower())" 2>/dev/null)

# Allow tests to override the working directory for check 9 and 10.
EFFECTIVE_CWD="${PREFLIGHT_CWD:-$PWD}"

# Produce a copy of $COMMAND with single- and double-quoted substrings removed.
# This is used by checks 4 and 5 so that `&&` / `;` / `cd` that appear inside
# quoted arguments (e.g. `--title "feat: foo; bar"`) are NOT misread as shell-
# level compound-command operators. This is an approximation — it does not
# handle escaped quotes, nested quotes, $'...' strings, or here-strings — but
# it covers the common case of plain quoted arguments without regressing the
# true positives (`cmd1; cmd2`, `cmd1 "foo" && cmd2`, `cd /x && ls`).
#
# The sed strips:
#   's/'"'"'[^'"'"']*'"'"'//g'  — single-quoted runs (no single-quote escaping)
#   's/"[^"]*"//g'              — double-quoted runs (no double-quote escaping)
STRIPPED_COMMAND=$(printf '%s' "$COMMAND" | sed -E "s/'[^']*'//g; s/\"[^\"]*\"//g")

# --- Forbidden pattern checks ---
# Note: uses grep -E (POSIX extended regex) for macOS compatibility. Do NOT use grep -P.

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

# 1. Dollar-paren command substitution: $( ... )
if echo "$COMMAND" | grep -qE '\$\(' ; then
    echo "BLOCKED: Command contains \$() command substitution. Use separate tool calls for dynamic values, or use scripts/with-secret.sh for secrets. See CLAUDE.md §Unattended Operation Patterns." >&2
    exit 2
fi

# 2. Heredocs (<<EOF, <<'EOF', <<"EOF", <<-EOF)
#    No exceptions — write content to a file first.
if echo "$COMMAND" | grep -qE '<<-?[[:space:]]*["'"'"']?[A-Za-z_]+["'"'"']?' ; then
    echo "BLOCKED: Command contains a heredoc. Write content to a file first using the Write tool, then pass it with --body-file or -F. See CLAUDE.md §Unattended Operation Patterns." >&2
    exit 2
fi

# 3. Inline python -c
if echo "$COMMAND" | grep -qE 'python3?[[:space:]]+-c[[:space:]]' ; then
    echo "BLOCKED: Never use python -c with inline code. Write the script to {worktree}/tmp/script.py first, then run it. See CLAUDE.md §Unattended Operation Patterns." >&2
    exit 2
fi

# 4. Quoted strings combined with && or ;
#    Uses $STRIPPED_COMMAND so that `;` / `&&` inside quoted arguments (e.g.
#    `--title "feat: foo; bar"`) do not trigger the check. Only unquoted `&&`
#    and `;` count as real compound-command operators. See issue #2589.
if echo "$STRIPPED_COMMAND" | grep -qE '&&|;' ; then
    if echo "$COMMAND" | grep -qE "[\"']" ; then
        echo "BLOCKED: Command contains quoted strings combined with && or ;. Split into separate tool calls. See CLAUDE.md §Unattended Operation Patterns." >&2
        exit 2
    fi
fi

# 5. cd in compound commands (use git -C, npm --prefix, or separate tool calls)
#    Uses $STRIPPED_COMMAND so that `cd` inside a quoted string (e.g.
#    `--name "cd into foo"`) and `;` / `&&` inside quoted arguments do not
#    trigger the check. See issue #2589.
if echo "$STRIPPED_COMMAND" | grep -qE '&&|;' ; then
    if echo "$STRIPPED_COMMAND" | grep -qE '\bcd\b' ; then
        echo "BLOCKED: Do not use cd in compound commands. Use 'git -C /path', 'npm --prefix /path', or separate tool calls instead. See CLAUDE.md §Unattended Operation Patterns." >&2
        exit 2
    fi
fi

# 6. Empty quotes immediately before a flag (potential bypass attempt)
#    Catches bypass patterns like:
#      - '' -rf, '' --force (empty single quotes before a flag)
#      - "" -rf, "" --force (empty double quotes before a flag)
#    These look like attempts to bypass safety checks by prepending empty quotes
#    to a flag argument (e.g. rm '' -rf /important).
#
#    Legitimate uses of '' or "" are NOT blocked — the regex only matches when
#    empty quotes are followed by whitespace then a dash (flag). Specifically
#    allowed patterns include:
#      - jq expressions: --jq '.state + " - " + .title'
#      - SQL equality checks: WHERE col = '' (issue #2049)
#      - SQL inequality checks: WHERE title != ''
#      - SQL CASE expressions: CASE WHEN col = '' THEN 1 END
#      - SQL escaped quotes: WHERE col = ''''   (four quotes = one escaped quote)
#      - Empty string arguments: --default ""
#      - Empty string at end of command: echo ''
#
#    Do NOT widen the pattern without adding regression tests in
#    .claude/hooks/test_preflight_hook.py — widening risks false positives on
#    scripts/dev-db-query.sh SQL arguments, which legitimately contain '' for
#    empty-string comparisons in PostgreSQL.
if echo "$COMMAND" | grep -qE "''[[:space:]]+-" ; then
    echo "BLOCKED: Command contains empty quotes before a flag ('' -...), which looks like a bypass attempt. If this is a legitimate use, write the command to a script file. See CLAUDE.md §Unattended Operation Patterns." >&2
    exit 2
fi
if echo "$COMMAND" | grep -qE '""[[:space:]]+-' ; then
    echo "BLOCKED: Command contains empty quotes before a flag (\"\" -...), which looks like a bypass attempt. If this is a legitimate use, write the command to a script file. See CLAUDE.md §Unattended Operation Patterns." >&2
    exit 2
fi

# 7. Terraform apply/destroy from root infra/terraform/ path.
#    The root directory has its own state backend that creates duplicate resources.
#    All applies must target an environment-specific path (environments/dev/, etc.).
#    Catches: terraform -chdir=.../infra/terraform apply
#             terraform -chdir=infra/terraform destroy
#    Does NOT block: terraform -chdir=infra/terraform/environments/dev apply
#                    terraform -chdir=infra/terraform init (init/plan/fmt are fine)
#                    terraform -chdir=infra/terraform validate
#                    terraform -chdir=infra/terraform state ... (state ops are fine)
if echo "$COMMAND" | grep -qE '\bterraform\b' ; then
    if echo "$COMMAND" | grep -qE '\b(apply|destroy)\b' ; then
        if echo "$COMMAND" | grep -qE 'infra/terraform' ; then
            if ! echo "$COMMAND" | grep -qE 'infra/terraform/environments/' ; then
                echo "BLOCKED: terraform apply/destroy from root infra/terraform/ is forbidden. The root state creates duplicate resources. Use an environment-specific path: infra/terraform/environments/dev/ (or staging/production). See docs/agent/infrastructure-reference.md §Terraform." >&2
                exit 2
            fi
        fi
    fi
fi

# 8. Long-running commands without sufficient timeout.
#    When timeout is missing or below 300000 (5 minutes), commands that typically
#    exceed the default 2-minute timeout get auto-backgrounded by the platform,
#    which violates the no-background rule for subagents and causes lost results.
#    Skip this check if run_in_background is true (already background, no auto-bg).
if [ "$RUN_IN_BG" != "true" ]; then
    NEEDS_TIMEOUT=0

    # pytest (any invocation)
    if echo "$COMMAND" | grep -qE '\bpytest\b' ; then
        NEEDS_TIMEOUT=1
    fi
    # gh run watch
    if echo "$COMMAND" | grep -qE '\bgh\s+run\s+watch\b' ; then
        NEEDS_TIMEOUT=1
    fi
    # pip install
    if echo "$COMMAND" | grep -qE '\bpip\s+install\b' ; then
        NEEDS_TIMEOUT=1
    fi
    # npm install (but not npm run or npm test)
    if echo "$COMMAND" | grep -qE '\bnpm\s+install\b' ; then
        NEEDS_TIMEOUT=1
    fi
    # npm run build
    if echo "$COMMAND" | grep -qE '\bnpm\s+run\s+build\b' ; then
        NEEDS_TIMEOUT=1
    fi
    # ruff check on large paths (src/, tests/, or .)
    #   Match "src/" or "tests/" only as standalone directory args (followed by
    #   space or end-of-line), not as prefixes of deeper paths like "src/foo.py".
    if echo "$COMMAND" | grep -qE '\bruff\s+check\b' ; then
        if echo "$COMMAND" | grep -qE '\bsrc/(\s|$)|\btests/(\s|$)|\s\.$' ; then
            NEEDS_TIMEOUT=1
        fi
    fi
    # terraform apply
    if echo "$COMMAND" | grep -qE '\bterraform\b.*\bapply\b' ; then
        NEEDS_TIMEOUT=1
    fi

    if [ "$NEEDS_TIMEOUT" -eq 1 ]; then
        # Check if timeout is set and >= 300000
        if [ "$TIMEOUT" = "none" ]; then
            echo "BLOCKED: This command may exceed the default 2-minute timeout and get auto-backgrounded. Retry with timeout: 1200000 (20 minutes). See CLAUDE.md §Critical Rules." >&2
            exit 2
        fi
        # Check if timeout is a number and >= 300000
        if echo "$TIMEOUT" | grep -qE '^[0-9]+$' ; then
            if [ "$TIMEOUT" -lt 300000 ]; then
                echo "BLOCKED: Timeout $TIMEOUT is too low for this long-running command (minimum 300000 / 5 minutes). Retry with timeout: 1200000 (20 minutes). See CLAUDE.md §Critical Rules." >&2
                exit 2
            fi
        fi
    fi
fi

# 9. run_in_background inside worktree subagents.
#    Subagents spawned with isolation: "worktree" are already background tasks.
#    Further backgrounding causes completion notifications to surface in the wrong
#    context (the parent/dispatcher), leading to confusion and lost results.
#    Detection: cwd contains ".claude/worktrees/" in its path.
if [ "$RUN_IN_BG" = "true" ]; then
    if echo "$EFFECTIVE_CWD" | grep -qE '\.claude/worktrees/' ; then
        echo "BLOCKED: run_in_background is not allowed inside worktree subagents. Use timeout: 1200000 instead. See CLAUDE.md §Critical Rules." >&2
        exit 2
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
#     isolation.
case "$EFFECTIVE_CWD" in
    */.claude/worktrees/*)
        CROSS_WT_REPO_ROOT="${EFFECTIVE_CWD%%/.claude/worktrees/*}"
        CROSS_WT_REST="${EFFECTIVE_CWD#"$CROSS_WT_REPO_ROOT"/.claude/worktrees/}"
        CROSS_WT_ID="${CROSS_WT_REST%%/*}"
        CROSS_WT_ROOT="$CROSS_WT_REPO_ROOT/.claude/worktrees/$CROSS_WT_ID"
        HOOK_DIR="$(dirname "$0")"
        CROSS_WT_MSG=$(
            COMMAND="$COMMAND" \
            REPO_ROOT="$CROSS_WT_REPO_ROOT" \
            WORKTREE_ROOT="$CROSS_WT_ROOT" \
            python3 "$HOOK_DIR/preflight_cross_worktree.py" 2>/dev/null
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
#     silently apply a stash created by another worktree (or by a long-gone
#     agent). This causes two failure modes:
#       (a) the current worktree's edits vanish (replaced by the other stash);
#       (b) another agent's uncommitted WIP lands in this worktree and can be
#           staged into the next commit via `git add -A` or similar.
#
#     Both were observed during #2746 (see #2749). The fix is to require an
#     explicit stash@{N} reference so the agent has demonstrably identified
#     which stash it wants to apply.
#
#     Detection:
#       - Command contains `git stash pop` or `git stash apply` (including
#         `git -C <path> stash pop|apply`).
#       - No positional argument matches `stash@{<digits>}`.
#       - Allow `git stash show`, `git stash list`, `git stash push`, `git
#         stash drop` — these are not the affected verbs.
#
#     Safer alternatives (see CLAUDE.md and docs/agent/unattended-patterns.md):
#       1. Use `git stash list` to confirm stash@{0}'s subject matches the
#          current branch, then `git stash pop stash@{0}` with the explicit ref.
#       2. Prefer a throwaway commit over stash: `git commit -am "WIP" && ...
#          && git reset --soft HEAD~1`. No shared global state.
if echo "$COMMAND" | grep -qE '\bgit\b(\s+-C\s+\S+)?\s+stash\s+(pop|apply)\b' ; then
    if ! echo "$COMMAND" | grep -qE 'stash@\{[0-9]+\}' ; then
        echo "BLOCKED: Bare 'git stash pop' / 'git stash apply' is not allowed. The stash list is shared across all worktrees in this clone, so a bare pop can silently apply another agent's or another worktree's stash — reverting your edits and dumping their WIP into your worktree (see #2749). Run 'git stash list' first, confirm the stash's subject matches your current branch, then pop it by explicit ref: 'git stash pop stash@{N}'. Or use a throwaway commit instead (git commit -am 'WIP' / git reset --soft HEAD~1). See CLAUDE.md §Unattended Operation Patterns." >&2
        exit 2
    fi
fi

# All checks passed
exit 0
