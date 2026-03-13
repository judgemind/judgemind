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

# 6. Empty quotes immediately before a flag (potential bypass attempt)
#    Catches bypass patterns like:
#      - '' -rf, '' --force (empty single quotes before a flag)
#      - "" -rf, "" --force (empty double quotes before a flag)
#    These look like attempts to bypass safety checks by prepending empty quotes
#    to a flag argument (e.g. rm '' -rf /important).
#
#    Legitimate uses of '' or "" are NOT blocked:
#      - jq expressions: --jq '.state + " - " + .title'
#      - SQL comparisons: WHERE title != ''
#      - Empty string arguments: --default ""
#
#    Only blocks when empty quotes are followed by whitespace then a dash (flag).
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
                echo "BLOCKED: terraform apply/destroy from root infra/terraform/ is forbidden. The root state creates duplicate resources. Use an environment-specific path: infra/terraform/environments/dev/ (or staging/production). See CLAUDE.md §Infrastructure Code." >&2
                exit 2
            fi
        fi
    fi
fi

# All checks passed
exit 0
