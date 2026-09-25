#!/usr/bin/env bash
# PreToolUse hook for Bash tool: rejects commands containing forbidden patterns
# that trigger interactive prompts and break autonomous workflows.
#
# This hook receives the tool input as JSON on stdin. It extracts the "command"
# field and checks it against the forbidden patterns documented in CLAUDE.md
# §Unattended Operation Patterns.
#
# Exit 0 = allow, exit 2 = block with message on stderr.
#
# Checks 0, 10, 11 and 12 are safety rules shared with the Fargate hook
# (scripts/preflight-bash-fargate.sh). They live in ONE place,
# preflight_shared_checks.sh next to this file, and are only *called* from
# here (#4703). Edit them there, not here. The other checks are
# operator-laptop-only — see docs/agent/interactive-shell-rules.md.

set -uo pipefail

# Load the shared safety checks. Fail closed: without them the push-to-main,
# worktree, cross-worktree-write and stash rules would silently vanish.
PREFLIGHT_SHARED_LIB="$(dirname "$0")/preflight_shared_checks.sh"
if [ ! -f "$PREFLIGHT_SHARED_LIB" ]; then
    echo "BLOCKED: preflight hook library missing: $PREFLIGHT_SHARED_LIB. The shared safety checks cannot run, so every Bash command is blocked. Restore .claude/hooks/preflight_shared_checks.sh (git checkout -- .claude/hooks/). See #4703." >&2
    exit 2
fi
# shellcheck source=./preflight_shared_checks.sh
source "$PREFLIGHT_SHARED_LIB"

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

# 0. Push to main/master — shared check (preflight_shared_checks.sh).
preflight_check_push_to_main || exit $?

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

# 10-12. Shared safety checks (preflight_shared_checks.sh), also enforced
#        by the Fargate hook: `git worktree add` inside a worktree,
#        cross-worktree writes via cp/mv/tar/redirection, and bare
#        `git stash pop` / `git stash apply`. See the library for detection
#        details and rationale.
preflight_check_worktree_add || exit $?
preflight_check_cross_worktree_write || exit $?
preflight_check_bare_stash_apply || exit $?

# ── Diagnoser bright lines (issue #3366) ─────────────────────────────
#
# When the daemon spawns ``/diagnose-failure`` it sets
# ``JUDGEMIND_DIAGNOSER_RUN=1`` on the subprocess env. The diagnoser is
# now a peer agent with the same authority surface as a /task agent —
# it can commit/push to the failed agent's branch, file issues, comment,
# edit labels. The four bright lines below are policy that bound that
# authority:
#
#   13. No production deploy (``terraform apply environments/production``,
#       ECS service writes against ``*-production`` clusters).
#   14. No PAT rotation (``gh auth switch``).
#   15. No force-push to main / amending merged commits.
#   16. No recursive ``/diagnose-failure`` invocation (depth-1 cap).
#
# These checks are inert when ``JUDGEMIND_DIAGNOSER_RUN`` is unset — every
# other Bash invocation in the daemon / agent-runner / interactive
# operator session is unaffected. The daemon is the only caller that sets
# the env var.

if [ "${JUDGEMIND_DIAGNOSER_RUN:-0}" = "1" ]; then
    # 13. No production deploy.
    #     Catches: terraform ... apply against environments/production,
    #              aws ecs update-service / register-task-definition / etc.
    #              against any cluster name containing -production.
    if echo "$COMMAND" | grep -qE '\bterraform\b' ; then
        if echo "$COMMAND" | grep -qE '\b(apply|destroy)\b' ; then
            if echo "$COMMAND" | grep -qE 'environments/production' ; then
                echo "BLOCKED [diagnoser bright line]: terraform apply/destroy against environments/production is human-only. Default to 'escalate' instead. See .claude/skills/diagnose-failure/SKILL.md §Bright lines." >&2
                exit 2
            fi
        fi
    fi
    # ECS service writes against a *-production cluster.
    if echo "$COMMAND" | grep -qE '\baws\s+ecs\b' ; then
        if echo "$COMMAND" | grep -qE '\b(update-service|register-task-definition|create-service|delete-service|run-task)\b' ; then
            if echo "$COMMAND" | grep -qE '\-production\b' ; then
                echo "BLOCKED [diagnoser bright line]: ECS service writes against *-production clusters are human-only. Default to 'escalate' instead. See .claude/skills/diagnose-failure/SKILL.md §Bright lines." >&2
                exit 2
            fi
        fi
    fi

    # 14. No PAT rotation / `gh auth switch`.
    if echo "$COMMAND" | grep -qE '\bgh\s+auth\s+switch\b' ; then
        echo "BLOCKED [diagnoser bright line]: 'gh auth switch' is operator-only — never run from the diagnoser. PAT rotation is human-only. Default to 'escalate' instead. See .claude/skills/diagnose-failure/SKILL.md §Bright lines." >&2
        exit 2
    fi

    # 15. No force-push to main, no amending merged commits.
    #     The push-to-main check (#0 above) already covers the "git push
    #     ... main" / "git push ... master" case. This adds the force-flag
    #     coverage so a force-push to main is blocked even from the
    #     diagnoser context — and blocks any --force / --force-with-lease
    #     push when the target branch is main/master.
    if echo "$COMMAND" | grep -qE '\bgit\b(\s+-C\s+\S+)?\s+push\b' ; then
        if echo "$COMMAND" | grep -qE '(\-\-force|\-\-force-with-lease|\-f\b)' ; then
            if echo "$COMMAND" | grep -qE '\b(main|master)\b' ; then
                echo "BLOCKED [diagnoser bright line]: force-push to main/master is destructive across all agents and is never allowed from the diagnoser. Default to 'escalate' instead. See .claude/skills/diagnose-failure/SKILL.md §Bright lines." >&2
                exit 2
            fi
        fi
    fi
    # 15b. No `git commit --amend` from the diagnoser context. Amending
    #      a commit that has already merged rewrites history shared with
    #      every other agent — even an unmerged amend on the failed
    #      agent's branch can race the daemon's rebase logic. Default to
    #      a fresh commit on top.
    if echo "$STRIPPED_COMMAND" | grep -qE '\bgit\b(\s+-C\s+\S+)?\s+commit\b' ; then
        if echo "$STRIPPED_COMMAND" | grep -qE '\-\-amend\b' ; then
            echo "BLOCKED [diagnoser bright line]: 'git commit --amend' is not allowed from the diagnoser. Create a fresh commit on top of the existing branch instead. See .claude/skills/diagnose-failure/SKILL.md §Bright lines." >&2
            exit 2
        fi
    fi

    # 16. No recursive `/diagnose-failure` invocation.
    #     Catches: claude -p '/diagnose-failure ...' or claude -p
    #     "/diagnose-failure ..." or claude --print /diagnose-failure ...
    if echo "$COMMAND" | grep -qE '\bclaude\b' ; then
        if echo "$COMMAND" | grep -qE '/diagnose-failure\b' ; then
            echo "BLOCKED [diagnoser bright line]: recursive '/diagnose-failure' invocation is not allowed (depth-1 cap). If a sub-action fails, escalate via the recommendation field. See .claude/skills/diagnose-failure/SKILL.md §Bright lines." >&2
            exit 2
        fi
    fi
fi

# All checks passed
exit 0
