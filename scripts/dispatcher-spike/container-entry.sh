#!/usr/bin/env bash
# dispatcher-spike/container-entry.sh — launched by the spike Fargate task.
#
# The task is invoked with one positional arg (the scenario):
#   success       — run a short prompt; expect exit 0 and a greeting in stdout
#   turn_limit    — run a multi-step prompt with --max-turns 1; expect non-zero exit
#   auth_fail     — clobber ANTHROPIC_API_KEY to force a 401; expect non-zero exit
#   mcp_probe     — run a prompt that asks claude to list its MCP tools; expect 0
#   hook_latency  — spike 0.2: force exactly one Read tool call so the
#                   PreToolUse hook fires and writes to dispatcher_spike.failures
#   hook_failmode — spike 0.2 failure mode: same as hook_latency but with a
#                   deliberately bad DATABASE_URL; expect claude to complete
#                   normally despite the hook's insert failing
#   git_gh        — spike 0.7: run /usr/local/bin/dispatcher-spike-test-git-gh
#                   which clones judgemind/judgemind, pushes a throwaway
#                   branch, opens+closes a throwaway PR, runs
#                   scripts/check-issue-author.sh, and deletes the branch.
#                   Uses ${GITHUB_TOKEN} (wired from Secrets Manager via the
#                   spike task definition). Does NOT invoke `claude -p`.
#
# Additional env vars the caller may set:
#   CLAUDE_PROMPT        — override the prompt text
#   CLAUDE_EXTRA_ARGS    — extra args passed straight to `claude -p`
#   SPIKE_AGENT_ID       — (spike 0.2) unique identifier for this task, stamped
#                          into dispatcher_spike.failures.agent_id. Defaults to
#                          the hostname (which Fargate sets to the task UUID).
#
# Output:
#   - stdout: the raw claude output (first bytes go to CloudWatch too)
#   - exit code: the claude exit code, unless the entry script itself fails
#
# The spike's wrapper scripts read the task's exit code + log tail after the
# task finishes. This script deliberately does NOT write to Postgres — that
# is the PreToolUse hook's job (spike 0.2) or the wrapper's job (spike 0.1).

set -u

SCENARIO="${1:-success}"

log()  { printf '[dispatcher-spike] %s\n' "$*" >&2; }

log "scenario=${SCENARIO}"
log "whoami=$(whoami) home=${HOME} pwd=$(pwd)"
log "node=$(node --version 2>&1 || echo 'missing') claude=$(which claude 2>&1 || echo 'missing')"

# Claude writes cache + session state under $HOME/.claude. Make sure it exists.
mkdir -p "${HOME}/.claude"

# ─── Spike 0.2 setup: hook env + probe file ─────────────────────────────────
# The PreToolUse hook (installed at /usr/local/bin/dispatcher-spike-hook.sh)
# reads SPIKE_AGENT_ID to stamp dispatcher_spike.failures rows. Default to
# the hostname which Fargate sets to the task UUID, so each task's row is
# trivially filterable by agent_id.
: "${SPIKE_AGENT_ID:=$(hostname)}"
export SPIKE_AGENT_ID

# Create a tiny, deterministic file that the hook_latency prompt will ask
# Claude to Read. We write it under $HOME so the `spike` user has RW
# permission; /etc would require root.
SPIKE_PROBE_FILE="${HOME}/spike-probe.txt"
if [[ ! -f "${SPIKE_PROBE_FILE}" ]]; then
    printf 'spike-0.2 probe file — read by claude to fire the PreToolUse hook.\n' \
        > "${SPIKE_PROBE_FILE}"
fi

# Spike 0.7: git_gh scenario runs test_git_gh.sh instead of claude -p.
# Short-circuit before building any claude args — the auth test is a
# pure git/gh/curl pipeline, not a Claude invocation. This keeps the
# scenario's failure modes unambiguous (a git push failure means a git
# push failure, not a Claude prompt regression).
if [[ "${SCENARIO}" == "git_gh" ]]; then
    log "git_gh scenario: delegating to /usr/local/bin/dispatcher-spike-test-git-gh"
    if [[ -z "${GITHUB_TOKEN:-}" ]]; then
        log "ERROR: GITHUB_TOKEN is unset — the spike task definition must wire in the scoped PAT secret."
        exit 2
    fi
    # The test script accepts an optional test-issue argument; default
    # (2689) is the spike 0.7 issue itself, which is MEMBER-filed and
    # thus must return TRUSTED.
    SPIKE_TEST_ISSUE="${SPIKE_TEST_ISSUE:-2689}"
    log "invoking dispatcher-spike-test-git-gh ${SPIKE_TEST_ISSUE}"
    set +e
    /usr/local/bin/dispatcher-spike-test-git-gh "${SPIKE_TEST_ISSUE}"
    EC=$?
    set -e
    log "test-git-gh exited with code=${EC}"
    exit "${EC}"
fi

# Default prompts per scenario.
case "${SCENARIO}" in
    success)
        PROMPT_DEFAULT="Reply with exactly the single word OK and nothing else."
        EXTRA_ARGS_DEFAULT=""
        ;;
    turn_limit)
        # Force a turn-limit trip. The prompt asks claude to write to a file
        # and then list the directory — that requires at least 2 tool calls,
        # which --max-turns 1 will cut off.
        PROMPT_DEFAULT="Please create a file named /tmp/probe.txt with the content 'hi', then read it back and tell me the contents."
        EXTRA_ARGS_DEFAULT="--max-turns 1"
        ;;
    auth_fail)
        PROMPT_DEFAULT="Reply with OK."
        EXTRA_ARGS_DEFAULT=""
        # Clobber the API key so the invocation fails with an auth error.
        export ANTHROPIC_API_KEY="sk-ant-invalid-spike-key-DO-NOT-USE"
        ;;
    mcp_probe)
        # Ask Claude what MCP tools are available. If the github MCP server
        # is wired correctly we expect tool names starting with `github_` or
        # `mcp__github__`. If the server isn't reachable we want to see the
        # error verbatim in CloudWatch.
        PROMPT_DEFAULT="List every MCP tool you currently have available. Respond as a bulleted markdown list of tool names only, nothing else."
        EXTRA_ARGS_DEFAULT=""
        ;;
    hook_latency)
        # Spike 0.2 happy path. Force Claude to perform exactly one Read tool
        # call so the PreToolUse hook fires, runs emit_failure.py, and inserts
        # one row into dispatcher_spike.failures. The prompt is explicit about
        # "Read this file, tell me the first line" to maximise the likelihood
        # the planner uses Read (and not, say, Bash `cat`).
        PROMPT_DEFAULT="Use the Read tool to read the file ${SPIKE_PROBE_FILE}, then reply with only the first line of the file (nothing else)."
        EXTRA_ARGS_DEFAULT=""
        ;;
    hook_failmode)
        # Spike 0.2 failure mode. Same as hook_latency but with a DATABASE_URL
        # that points to a non-routable host. The hook MUST NOT block the tool
        # call — we expect claude to still exit 0 and print the first line.
        PROMPT_DEFAULT="Use the Read tool to read the file ${SPIKE_PROBE_FILE}, then reply with only the first line of the file (nothing else)."
        EXTRA_ARGS_DEFAULT=""
        # Port 1 is reserved; a TCP connect there inside the VPC returns a
        # refused quickly, so the hook fails the insert but exits 0 fast.
        export DATABASE_URL="postgresql://nobody:nobody@127.0.0.1:1/nodb?connect_timeout=2"
        log "hook_failmode: DATABASE_URL intentionally broken — hook insert expected to fail but not propagate."
        ;;
    *)
        log "unknown scenario '${SCENARIO}' — treating as 'success'"
        PROMPT_DEFAULT="Reply with exactly the single word OK and nothing else."
        EXTRA_ARGS_DEFAULT=""
        ;;
esac

PROMPT="${CLAUDE_PROMPT:-${PROMPT_DEFAULT}}"
EXTRA_ARGS="${CLAUDE_EXTRA_ARGS:-${EXTRA_ARGS_DEFAULT}}"

MCP_CONFIG="/etc/dispatcher-spike/mcp-config.json"
SETTINGS_FILE="/etc/dispatcher-spike/settings.json"
CLAUDE_ARGS=(-p)

# Defensively cap total turns so claude -p can never run forever.
# The turn-limit scenario overrides this via EXTRA_ARGS_DEFAULT.
if [[ "${EXTRA_ARGS}" != *"--max-turns"* ]]; then
    EXTRA_ARGS="--max-turns 10 ${EXTRA_ARGS}"
fi

# Split EXTRA_ARGS on whitespace into the args array. We intentionally
# do NOT quote ${EXTRA_ARGS} here because the caller may pass multiple
# flags in one string.
# shellcheck disable=SC2206
EXTRA_ARGS_ARRAY=(${EXTRA_ARGS})
CLAUDE_ARGS+=("${EXTRA_ARGS_ARRAY[@]}")

if [[ -r "${MCP_CONFIG}" ]]; then
    # --mcp-config is variadic (`<configs...>`). Pass the file path
    # explicitly and terminate the option list with `--` so the prompt
    # doesn't get sucked up as a second MCP config path.
    CLAUDE_ARGS+=(--mcp-config "${MCP_CONFIG}")
else
    log "MCP config file missing at ${MCP_CONFIG} — continuing without explicit MCP config"
fi

# Spike 0.2: register the PreToolUse Read hook via --settings. Only load
# the settings file for hook-centric scenarios; the other scenarios keep
# their minimal spike-0.1 behaviour so their findings don't shift.
case "${SCENARIO}" in
    hook_latency|hook_failmode)
        if [[ -r "${SETTINGS_FILE}" ]]; then
            CLAUDE_ARGS+=(--settings "${SETTINGS_FILE}")
            # Claude Code refuses to run tools on paths outside the session
            # working directory unless explicitly allowed. The probe file
            # sits at ${HOME}/spike-probe.txt; whitelist that directory.
            CLAUDE_ARGS+=(--add-dir "${HOME}")
            log "registered PreToolUse Read hook via ${SETTINGS_FILE} (SPIKE_AGENT_ID=${SPIKE_AGENT_ID})"
        else
            log "WARNING: settings file missing at ${SETTINGS_FILE} — hook will NOT fire"
        fi
        ;;
esac

# Always terminate option parsing before the prompt so variadic flags
# (e.g. --mcp-config <configs...>, --add-dir <directories...>) don't
# consume the prompt as another value.
CLAUDE_ARGS+=(--)
CLAUDE_ARGS+=("${PROMPT}")

# Redact the prompt from the logged command — it can be long or contain
# sensitive context. All other args are safe to log.
ARGS_PREVIEW=("${CLAUDE_ARGS[@]:0:${#CLAUDE_ARGS[@]}-1}")
log "invoking: claude ${ARGS_PREVIEW[*]} <prompt ${#PROMPT} chars>"

# Run claude. Close stdin so the CLI doesn't hang waiting for input.
# Capture exit code for explicit exit.
set +e
claude "${CLAUDE_ARGS[@]}" < /dev/null
EC=$?
set -e

log "claude exited with code=${EC}"
exit "${EC}"
