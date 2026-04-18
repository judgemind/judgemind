#!/usr/bin/env bash
# dispatcher-spike/container-entry.sh — launched by the spike Fargate task.
#
# The task is invoked with one positional arg (the scenario):
#   success    — run a short prompt; expect exit 0 and a greeting in stdout
#   turn_limit — run a multi-step prompt with --max-turns 1; expect non-zero exit
#   auth_fail  — clobber ANTHROPIC_API_KEY to force a 401; expect non-zero exit
#   mcp_probe  — run a prompt that asks claude to list its MCP tools; expect 0
#
# Additional env vars the caller may set:
#   CLAUDE_PROMPT        — override the prompt text
#   CLAUDE_EXTRA_ARGS    — extra args passed straight to `claude -p`
#
# Output:
#   - stdout: the raw claude output (first bytes go to CloudWatch too)
#   - exit code: the claude exit code, unless the entry script itself fails
#
# The spike's wrapper script (`scripts/dispatcher-spike/run_fargate_claude_p.sh`)
# reads the task's exit code + log tail after the task finishes. This script
# deliberately does NOT write to Postgres — the wrapper does, outside the VPC
# network constraints of the Fargate container.

set -u

SCENARIO="${1:-success}"

log()  { printf '[dispatcher-spike] %s\n' "$*" >&2; }

log "scenario=${SCENARIO}"
log "whoami=$(whoami) home=${HOME} pwd=$(pwd)"
log "node=$(node --version 2>&1 || echo 'missing') claude=$(which claude 2>&1 || echo 'missing')"

# Claude writes cache + session state under $HOME/.claude. Make sure it exists.
mkdir -p "${HOME}/.claude"

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
    *)
        log "unknown scenario '${SCENARIO}' — treating as 'success'"
        PROMPT_DEFAULT="Reply with exactly the single word OK and nothing else."
        EXTRA_ARGS_DEFAULT=""
        ;;
esac

PROMPT="${CLAUDE_PROMPT:-${PROMPT_DEFAULT}}"
EXTRA_ARGS="${CLAUDE_EXTRA_ARGS:-${EXTRA_ARGS_DEFAULT}}"

MCP_CONFIG="/etc/dispatcher-spike/mcp-config.json"
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
