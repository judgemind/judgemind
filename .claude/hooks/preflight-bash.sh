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
if echo "$COMMAND" | grep -qE '&&|;' ; then
    if echo "$COMMAND" | grep -qE "[\"']" ; then
        echo "BLOCKED: Command contains quoted strings combined with && or ;. Split into separate tool calls. See CLAUDE.md §Unattended Operation Patterns." >&2
        exit 2
    fi
fi

# 5. cd in compound commands (use git -C, npm --prefix, or separate tool calls)
if echo "$COMMAND" | grep -qE '&&|;' ; then
    if echo "$COMMAND" | grep -qE '\bcd\b' ; then
        echo "BLOCKED: Do not use cd in compound commands. Use 'git -C /path', 'npm --prefix /path', or separate tool calls instead. See CLAUDE.md §Unattended Operation Patterns." >&2
        exit 2
    fi
fi

# 6. Consecutive quote characters at word start (potential obfuscation)
#    Catches patterns like ""word, ''word, "'word, '"word at word boundaries.
#    These serve no legitimate purpose and may be attempts to bypass other checks.
if echo "$COMMAND" | grep -qE '(^|[[:space:]])(["'"'"']){2,}[a-zA-Z]' ; then
    echo "BLOCKED: Command contains consecutive quote characters at word start (potential obfuscation). See CLAUDE.md §Unattended Operation Patterns." >&2
    exit 2
fi

# All checks passed
exit 0
