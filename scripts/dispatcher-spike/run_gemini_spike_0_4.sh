#!/usr/bin/env bash
# run_gemini_spike_0_4.sh - Dispatcher v2 Spike 0.4 driver.
#
# Feeds Gemini CLI the same input /task-v2-summary would consume, captures
# the output shape, and verifies hooks + turn-limit exit-code.
#
# Usage:
#   scripts/dispatcher-spike/run_gemini_spike_0_4.sh <scenario>
#
#   scenario ∈ {summary, hook-fires, turn-limit, version, auth-check}
#
# Scenario definitions:
#   version      — report `gemini --version` and tools on PATH
#   auth-check   — check for existing OAuth/API creds
#   summary      — run against the /task-v2-summary fixture
#   hook-fires   — register a no-op AfterTool hook and verify stdout
#   turn-limit   — run multi-step prompt with --max-turns 1, verify exit 53
#
# Throwaway — spike 0.4 only. Results go to tmp/spike-0.4/.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
FIXTURES="${REPO_ROOT}/tmp/spike-0.3-fixtures"
OUTDIR="${REPO_ROOT}/tmp/spike-0.4"
SKILL_FILE="${REPO_ROOT}/.claude/skills/task-v2-summary/SKILL.md"

mkdir -p "${OUTDIR}"

GEMINI_BIN="${GEMINI_BIN:-$(command -v gemini || true)}"
if [[ -z "${GEMINI_BIN}" ]]; then
    echo "Error: gemini CLI not found on PATH" >&2
    exit 2
fi

# Allow the caller to inject GEMINI_API_KEY/GOOGLE_API_KEY via
# scripts/with-secret.sh. When running non-interactively the free-tier
# OAuth flow is not viable, so spike 0.4 uses the AI Studio API key
# (stored in AWS Secrets Manager at judgemind/google/api-key).
# If neither var is set, scenarios that invoke the model will exit 41.
if [[ -n "${GOOGLE_API_KEY:-}" && -z "${GEMINI_API_KEY:-}" ]]; then
    export GEMINI_API_KEY="${GOOGLE_API_KEY}"
fi

SCENARIO="${1:?Usage: $0 <scenario>}"

case "${SCENARIO}" in
    version)
        echo "=== Gemini CLI version ==="
        "${GEMINI_BIN}" --version
        echo
        echo "=== gemini --help | head -100 ==="
        "${GEMINI_BIN}" --help 2>&1 | head -120 || true
        ;;

    auth-check)
        echo "=== Gemini config directory ==="
        ls -la "${HOME}/.gemini/" 2>&1 || echo "(~/.gemini/ does not exist)"
        echo
        echo "=== OAuth cred files ==="
        find "${HOME}/.gemini" -maxdepth 2 -type f 2>/dev/null | head -30 || true
        echo
        echo "=== Env vars ==="
        env | grep -iE "^(GEMINI|GOOGLE)" | sed -E 's/=.{30,}/=***REDACTED***/' || true
        ;;

    summary)
        # Build a prompt that fuses the /task-v2-summary SKILL.md with the
        # fixture content. Gemini doesn't know /task-v2-summary, so we
        # inline the skill text as the system prompt and pass the fixture
        # JSON as the user prompt.
        SUMMARY_FIXTURE="${FIXTURES}/summary_input.json"
        if [[ ! -f "${SUMMARY_FIXTURE}" ]]; then
            echo "Error: fixture not found: ${SUMMARY_FIXTURE}" >&2
            echo "Run: python3 scripts/dispatcher-spike/build_spike_0_3_fixtures.py" >&2
            exit 2
        fi

        PROMPT_FILE="${OUTDIR}/summary_prompt.txt"
        RESULT_JSON="${OUTDIR}/summary_result.json"
        RESULT_STDOUT="${OUTDIR}/summary_stdout.txt"
        RESULT_STDERR="${OUTDIR}/summary_stderr.txt"

        # Concatenate skill + instructions + fixture inline.
        {
            echo "You are executing the /task-v2-summary skill from a dispatcher"
            echo "v2 per-phase pipeline. Read the skill instructions below and"
            echo "follow them exactly. DO NOT invoke shell commands. Produce ONLY"
            echo "a single JSON object matching the Output schema, no prose."
            echo
            echo "=== SKILL INSTRUCTIONS ==="
            cat "${SKILL_FILE}"
            echo
            echo "=== FIXTURE (summary_input.json) ==="
            cat "${SUMMARY_FIXTURE}"
            echo
            echo "=== REQUIRED OUTPUT ==="
            echo "Respond with exactly one JSON object with these keys:"
            echo "  - process_summary_md (string)"
            echo "  - commit_message (string)"
            echo "  - pr_title (string)"
            echo "  - pr_body_md (string)"
            echo "  - unmet_criteria (array of strings)"
            echo "Output NOTHING else — no markdown fences, no preamble, no trailing prose."
        } > "${PROMPT_FILE}"

        echo "[summary] prompt size: $(wc -c < "${PROMPT_FILE}") bytes"
        echo "[summary] Running gemini -p..."

        START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        set +e
        "${GEMINI_BIN}" \
            -p "@${PROMPT_FILE}" \
            --output-format json \
            --yolo \
            > "${RESULT_STDOUT}" 2> "${RESULT_STDERR}"
        GEMINI_EXIT=$?
        set -e
        END_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

        printf '{"scenario":"summary","exit_code":%d,"started_at":"%s","ended_at":"%s","stdout_bytes":%d,"stderr_bytes":%d}\n' \
            "${GEMINI_EXIT}" "${START_TS}" "${END_TS}" \
            "$(wc -c < "${RESULT_STDOUT}")" "$(wc -c < "${RESULT_STDERR}")" \
            > "${RESULT_JSON}"

        echo
        echo "=== exit code: ${GEMINI_EXIT} ==="
        echo "=== stdout (first 120 lines) ==="
        head -120 "${RESULT_STDOUT}"
        echo
        echo "=== stderr (last 40 lines) ==="
        tail -40 "${RESULT_STDERR}"
        ;;

    hook-fires)
        # Create a throwaway settings.json registering a BeforeTool hook on
        # read_file (Gemini's filesystem-read tool) that writes a marker to
        # tmp/spike-0.4/hook_marker.txt. Invoke gemini with a prompt that
        # must read a file, confirm marker exists.
        #
        # Gemini looks for settings in three places (v0.38.2):
        #   1. ${project}/.gemini/settings.json (project-level)
        #   2. ${HOME}/.gemini/settings.json (user-level)
        #   3. /etc/gemini-cli/settings.json (system-level)
        # We use the project-level path inside a dedicated spike-0.4 project
        # directory outside the main worktree to avoid polluting project
        # settings and to avoid the main worktree's ignore patterns (which
        # blocked read_file on tmp/ in the first attempt).

        PROJECT_DIR="${OUTDIR}/gemini-project"
        HOOK_MARKER="${PROJECT_DIR}/hook_marker.txt"
        HOOK_SCRIPT="${PROJECT_DIR}/hook_noop.sh"
        HOOK_LOG="${PROJECT_DIR}/hook_log.txt"
        SETTINGS_DIR="${PROJECT_DIR}/.gemini"

        rm -rf "${PROJECT_DIR}"
        mkdir -p "${SETTINGS_DIR}"

        # Hook script — reads stdin JSON, writes a marker + echoes empty JSON
        # to stdout to allow the tool call.
        cat > "${HOOK_SCRIPT}" <<'HOOKEOF'
#!/usr/bin/env bash
# Gemini hook probe — writes marker + logs stdin.
MARKER="${1:-/tmp/hook_marker.txt}"
LOG="${2:-/tmp/hook_log.txt}"
STDIN_CONTENT="$(cat)"
echo "HOOK_FIRED_AT $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "${MARKER}"
{
    echo "--- invocation at $(date -u +%Y-%m-%dT%H:%M:%SZ) ---"
    echo "STDIN:"
    echo "${STDIN_CONTENT}"
    echo "---"
} >> "${LOG}"
# Emit empty JSON to stdout — allows the tool call to proceed.
echo "{}"
HOOKEOF
        chmod +x "${HOOK_SCRIPT}"

        # Gemini hook schema (from hooks/migrate --from-claude = Claude-style).
        # Try two shapes and record which one Gemini accepts.
        cat > "${SETTINGS_DIR}/settings.json" <<JSONEOF
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "read_file",
        "hooks": [
          {
            "type": "command",
            "command": "${HOOK_SCRIPT} ${HOOK_MARKER} ${HOOK_LOG}"
          }
        ]
      }
    ],
    "BeforeTool": [
      {
        "matcher": "read_file",
        "hooks": [
          {
            "type": "command",
            "command": "${HOOK_SCRIPT} ${HOOK_MARKER} ${HOOK_LOG}"
          }
        ]
      }
    ]
  }
}
JSONEOF

        TARGET_FILE="${PROJECT_DIR}/probe.txt"
        echo "hello from spike 0.4 hook target" > "${TARGET_FILE}"

        echo "[hook-fires] Project dir: ${PROJECT_DIR}"
        echo "[hook-fires] Settings:    ${SETTINGS_DIR}/settings.json"
        echo "[hook-fires] Target file: ${TARGET_FILE}"

        PROMPT="Use your read_file tool to read probe.txt in the current directory. Echo its contents verbatim and nothing else."

        echo "[hook-fires] Running gemini from project dir..."
        set +e
        (cd "${PROJECT_DIR}" && \
            "${GEMINI_BIN}" \
            -p "${PROMPT}" \
            --yolo \
            --debug \
            > "${OUTDIR}/hook_stdout.txt" 2> "${OUTDIR}/hook_stderr.txt")
        GEMINI_EXIT=$?
        set -e

        echo "=== exit: ${GEMINI_EXIT} ==="
        echo "=== stdout (first 50 lines) ==="
        head -50 "${OUTDIR}/hook_stdout.txt" 2>/dev/null || true
        echo
        echo "=== stderr (last 40 lines) ==="
        tail -40 "${OUTDIR}/hook_stderr.txt" 2>/dev/null || true
        echo
        echo "=== hook marker contents ==="
        if [[ -f "${HOOK_MARKER}" ]]; then
            echo "HOOK FIRED: yes"
            cat "${HOOK_MARKER}"
        else
            echo "HOOK FIRED: no (marker file ${HOOK_MARKER} does not exist)"
        fi
        echo
        echo "=== hook log (first 60 lines) ==="
        head -60 "${HOOK_LOG}" 2>/dev/null || echo "(no hook log)"
        ;;

    turn-limit)
        # Gemini CLI 0.38.2 has no --max-turns flag. Turn-limit is
        # configured via settings.json with maxSessionTurns (added in
        # PR #3507, lives in the top-level "model" section).
        # Provoke a turn-limit with a multi-step prompt in a fresh project
        # dir whose settings.json sets maxSessionTurns=1. Expected: exit
        # code 53 once the first turn completes and a second is requested.
        TL_DIR="${OUTDIR}/gemini-turnlimit"
        rm -rf "${TL_DIR}"
        mkdir -p "${TL_DIR}/.gemini"

        # Seed multiple targets to encourage multi-step planning.
        for i in 1 2 3; do
            echo "content file $i" > "${TL_DIR}/file${i}.txt"
        done

        cat > "${TL_DIR}/.gemini/settings.json" <<'JSONEOF'
{
  "model": {
    "maxSessionTurns": 1
  }
}
JSONEOF

        PROMPT="Step by step, using one tool call per turn: (1) read file1.txt using read_file. (2) then read file2.txt using read_file. (3) then read file3.txt using read_file. (4) then output a one-line summary. Do NOT batch calls; make a single tool call per turn."

        echo "[turn-limit] Running gemini with settings.json maxSessionTurns=1"
        set +e
        (cd "${TL_DIR}" && \
            "${GEMINI_BIN}" \
            -p "${PROMPT}" \
            --yolo \
            > "${OUTDIR}/turnlimit_stdout.txt" 2> "${OUTDIR}/turnlimit_stderr.txt")
        GEMINI_EXIT=$?
        set -e

        echo "=== exit: ${GEMINI_EXIT} (spec expects 53 on turn-limit) ==="
        echo "=== stdout ==="
        head -60 "${OUTDIR}/turnlimit_stdout.txt" 2>/dev/null || true
        echo
        echo "=== stderr (last 40 lines) ==="
        tail -40 "${OUTDIR}/turnlimit_stderr.txt" 2>/dev/null || true
        ;;

    *)
        echo "Unknown scenario: ${SCENARIO}" >&2
        echo "Valid: summary hook-fires turn-limit version auth-check" >&2
        exit 2
        ;;
esac
