#!/usr/bin/env bash
# agent-runner-entrypoint.sh — Stage 1b entrypoint for the per-agent
# ECS task (#3090, part of #3086's migration from the daemon's per-phase
# subprocess model to one ECS task per agent; design in #3078).
#
# One instance of this script runs inside each `judgemind-dispatcher-
# agent-runner-dev` Fargate task. It:
#
#   1. Reads AGENT_ID + DB + GitHub credentials from env.
#   2. Clones judgemind/judgemind shallow (--depth=100) and checks out
#      a per-agent branch based off origin/main.
#   3. Applies any prior `dispatcher.ralph_patches` row for the agent's
#      issue via `git am --3way` (same contract as the daemon's
#      `_apply_prior_ralph_patch`).
#   4. Loops over phases: reads the current phase from
#      `dispatcher.agents`, dispatches to the phase implementation,
#      calls `phase_transitions.next_phase_from_verdict` (imported from
#      the shared Python module #3095 shipped), INSERTs a
#      `dispatcher.phase_outputs` row, UPDATEs the agent row, and
#      exits when the phase becomes terminal.
#
# Stage 1b scope — what's wired vs. stubbed
# -----------------------------------------
# The Stage 1b PR proves the task def + entrypoint can boot, read DB,
# import phase_transitions, invoke `claude -p`, persist phase_outputs,
# and exit on a terminal phase. The daemon-side code that launches
# this task (the `_launch_agent_ecs_task` method) lands in Stage 2
# (#3091). Until then, operators invoke this task manually via
# `aws ecs run-task --overrides`; the dispatcher daemon continues to
# use the in-process subprocess path.
#
# Per-phase mechanical side effects — the parts that today live in
# `daemon._handle_phase_*` (push-and-pr's `git push` + `gh pr create`,
# awaiting_ci's `gh pr checks` poll, etc.) — are stubbed as
# per-phase helper functions (`_handle_phase_push_and_pr`, etc.) that
# return sentinel outputs for verdict-driven phases (plan, ralph,
# summary, push_and_pr, fix_ci, verify) and TODO exits for the
# mechanical-only phases (merge, awaiting_ci, awaiting_deploy, retro).
# Stage 2 fleshes each stub out.
#
# macOS bash 3.2 compatibility
# ----------------------------
# Per `scripts/check-bash-compat.sh` this script must not use any
# bash 4+ features (`mapfile`, associative arrays, `${var,,}`, etc.)
# even though it runs on the Debian-based agent-runner container in
# production. The compat check runs in CI against every file under
# `scripts/**/*.sh` without regard to runtime target.
#
# Testability
# -----------
# The script stays testable from `scripts/tests/test_agent_runner_
# entrypoint.sh` by:
#
#   * Shelling out to every external binary through PATH (no hardcoded
#     `/usr/local/bin/claude` etc.) so the test harness can stub
#     `claude`, `git`, `gh`, `psql`, `aws` via a `bin-stubs/` dir.
#   * Reading every input from env vars — no magic `/var/lib/...`
#     paths baked in. Set `AGENT_WORKSPACE` to the test tmpdir.
#   * Emitting one line per side effect via a `log()` function that
#     can be redirected to a file the test inspects.
#   * A `AGENT_RUNNER_DRY_RUN=1` env switch that stops before the
#     first `claude -p` invocation so a test can assert on pre-phase
#     setup in isolation.

set -euo pipefail

# ── Logging -----------------------------------------------------------------
#
# Structured single-line JSON to stdout so CloudWatch Insights can
# index the fields directly. Stays bash 3.2 compatible — no `${var^^}`
# uppercasing and no `printf '%(%FT%TZ)T'`.

log() {
    # $1 = event name, rest = key=value pairs. Writes to fd 3 (wired
    # to stdout for the entrypoint's top-level process, OR redirected
    # per-function so callers can capture a function's real stdout
    # without log noise mixing in). See `run_claude_phase` below for
    # the motivating case.
    _ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    _event="$1"
    shift
    _extra=""
    for kv in "$@"; do
        # kv is of the form key=value. Escape double-quotes in value.
        _k=${kv%%=*}
        _v=${kv#*=}
        _v=$(printf '%s' "$_v" | sed 's/"/\\"/g')
        _extra="$_extra, \"$_k\": \"$_v\""
    done
    printf '{"ts": "%s", "event": "agent_runner.%s", "agent_id": "%s"%s}\n' \
        "$_ts" "$_event" "${AGENT_ID:-unknown}" "$_extra" >&3
}

# Wire fd 3 to stdout. CloudWatch captures both stdout and stderr, so
# the user sees identical output. Functions that need to return a
# value via stdout (run_claude_phase, read_current_phase, etc.) do
# NOT need to redirect fd 3 — log() writes to fd 3 which is still
# stdout of the top process, while the function's own `printf` goes
# to the function's captured stdout. `$( )` substitution captures fd
# 1 only, so log noise never pollutes captured function output.
exec 3>&1

die() {
    log "fatal" "reason=$1"
    exit 2
}

# ── Env contract ------------------------------------------------------------

AGENT_ID="${AGENT_ID:-}"
ISSUE_NUMBER="${ISSUE_NUMBER:-}"
REPO_URL="${REPO_URL:-https://github.com/judgemind/judgemind.git}"
BRANCH_NAME="${BRANCH_NAME:-}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
DATABASE_URL="${DATABASE_URL:-}"
AGENT_WORKSPACE="${AGENT_WORKSPACE:-/var/lib/agent-runner}"
AGENT_RUNNER_DRY_RUN="${AGENT_RUNNER_DRY_RUN:-0}"

if [[ -z "$AGENT_ID" ]]; then
    die "AGENT_ID_unset"
fi

if [[ -z "$DATABASE_URL" ]]; then
    die "DATABASE_URL_unset"
fi

# Derive a short id for branch naming (first 8 chars of the agent uuid).
SHORT_ID=$(printf '%s' "$AGENT_ID" | cut -c1-8)
if [[ -z "$BRANCH_NAME" ]]; then
    BRANCH_NAME="agent/$SHORT_ID"
fi

log "startup" "issue_number=$ISSUE_NUMBER" "branch=$BRANCH_NAME" "short_id=$SHORT_ID"

# ── Workspace + clone -------------------------------------------------------
#
# Per-agent clone under $AGENT_WORKSPACE/repo. The Fargate ephemeral
# storage root (/var/lib/agent-runner in production, a tmpdir under
# test) starts empty on every task start — no `git worktree` games,
# no sweep-between-runs.

REPO_ROOT="$AGENT_WORKSPACE/repo"
mkdir -p "$AGENT_WORKSPACE"

if [[ ! -d "$REPO_ROOT/.git" ]]; then
    log "clone_begin" "repo_url=$REPO_URL"
    # Shallow clone keeps the container light — per-agent lifetimes are
    # ~20-90m and phases never need `git log --all`. `--no-tags` shaves
    # another few MB.
    git clone --depth=100 --no-tags "$REPO_URL" "$REPO_ROOT"
    log "clone_done"
else
    # Defensive — production tasks start with an empty workspace, but
    # local `docker run` invocations reusing a volume would skip the
    # clone. Fetch to refresh origin/main.
    log "clone_skip_existing"
    git -C "$REPO_ROOT" fetch origin main --depth=100 --no-tags
fi

# Authenticate gh + git against the scoped PAT if available. `gh auth
# setup-git` wires the helper into ~/.gitconfig so every subsequent
# `git push` uses the PAT.
if [[ -n "$GITHUB_TOKEN" ]]; then
    log "gh_auth_begin"
    printf '%s' "$GITHUB_TOKEN" | gh auth login --with-token >/dev/null 2>&1 || true
    gh auth setup-git >/dev/null 2>&1 || true
    log "gh_auth_done"
fi

cd "$REPO_ROOT"

# Create the per-agent branch off origin/main.
git checkout -B "$BRANCH_NAME" origin/main
log "branch_ready" "branch=$BRANCH_NAME"

# ── Apply prior ralph patch (if any) ---------------------------------------
#
# Mirrors the daemon's `_apply_prior_ralph_patch` semantics: pull the
# newest row for this issue within the 7-day TTL, write it to a scratch
# file, and `git am --3way` it. On conflict, leave the am state in
# place so ralph can inspect; on invocation failure, abort cleanly.
# This Stage 1b implementation is the happy path only — conflict
# handoff (issue #3026) is added in Stage 2 alongside the daemon
# integration.

apply_prior_patch() {
    if [[ -z "$ISSUE_NUMBER" ]]; then
        log "prior_patch_skip_no_issue"
        return 0
    fi

    log "prior_patch_query_begin"
    _query="SELECT patch_content
              FROM dispatcher.ralph_patches
             WHERE issue_number = $ISSUE_NUMBER
               AND created_at > now() - interval '7 days'
             ORDER BY created_at DESC
             LIMIT 1;"
    _patch_file="$AGENT_WORKSPACE/prior-ralph.patch"
    if ! psql "$DATABASE_URL" -At -c "$_query" > "$_patch_file" 2>/dev/null; then
        log "prior_patch_query_failed"
        rm -f "$_patch_file"
        return 0
    fi

    if [[ ! -s "$_patch_file" ]]; then
        log "prior_patch_none"
        rm -f "$_patch_file"
        return 0
    fi

    _bytes=$(wc -c < "$_patch_file" | tr -d ' ')
    log "prior_patch_apply_begin" "bytes=$_bytes"
    if git -C "$REPO_ROOT" am --3way "$_patch_file"; then
        log "prior_patch_applied"
    else
        log "prior_patch_conflict"
        # Stage 1b leaves the am-in-progress state for ralph. A future
        # daemon-integration PR will build the resume-with-conflict
        # prompt block; for Stage 1b we just log and continue so the
        # operator smoke can still observe the failure mode.
    fi
}

apply_prior_patch

# ── DB helpers --------------------------------------------------------------

db_exec() {
    # Execute SQL statement against the dispatcher DB. Exits 0 on
    # success; logs + exits non-zero on failure. Use -v ON_ERROR_STOP
    # so a syntax typo surfaces instead of silently continuing.
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "$1" >/dev/null
}

db_query_one() {
    # Execute SQL SELECT and print the first column of the first row
    # to stdout. Empty result → empty stdout + exit 0. Use -At for
    # unaligned tuples-only output so callers don't have to trim.
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -At -c "$1"
}

read_current_phase() {
    db_query_one "SELECT phase
                    FROM dispatcher.agents
                   WHERE agent_id = '$AGENT_ID'
                   LIMIT 1;"
}

# ── Python helper: phase_transitions bridge --------------------------------
#
# The shell script can't import Python, so we expose the pure
# `phase_transitions.next_phase_from_verdict` function via a small
# shim that takes `current_phase` + `output_json` on stdin and prints
# `<action>\t<next_phase>\t<terminal_status>\t<failure_hint>` on
# stdout. The shim file is generated once at startup so the test
# harness can stub it by setting AGENT_RUNNER_TRANSITION_SHIM.
#
# $PHASE_TRANSITIONS_DIR is prepended to sys.path inside the shim so
# the shim works inside the container (default `/app/scripts/dispatcher`)
# AND in local test runs (the harness sets it to the worktree's
# `scripts/dispatcher/`).

PHASE_TRANSITIONS_DIR="${PHASE_TRANSITIONS_DIR:-/app/scripts/dispatcher}"
PHASE_TRANSITIONS_PARENT="${PHASE_TRANSITIONS_PARENT:-/app}"
export PHASE_TRANSITIONS_DIR PHASE_TRANSITIONS_PARENT

TRANSITION_SHIM="${AGENT_RUNNER_TRANSITION_SHIM:-$AGENT_WORKSPACE/phase_transitions_shim.py}"

if [[ "$TRANSITION_SHIM" == "$AGENT_WORKSPACE/phase_transitions_shim.py" ]]; then
    # The shim is short enough to keep as an external file we stamp at
    # boot rather than a heredoc (heredocs are blocked by the preflight
    # hook in operator shells; this script only runs in-container but
    # the rule keeps the hook happy if anyone ever shells in to test).
    _shim_path="$AGENT_WORKSPACE/phase_transitions_shim.py"
    cat <<'PYEOF' > "$_shim_path"
"""Entrypoint-internal shim: JSON in -> tab-separated transition out.

Reads on stdin:
    {"current_phase": "ralph", "output": {"verdict": "SHIP"}}

Writes to stdout (single line, tab-separated):
    advance\tsummary\t\t

Field order: action, next_phase, terminal_status, failure_hint. Empty
fields are the empty string.

Module lookup: the shim reads PHASE_TRANSITIONS_DIR +
PHASE_TRANSITIONS_PARENT from the environment so both in-container
(/app/scripts/dispatcher, /app) and local test harnesses
(<repo>/scripts/dispatcher, <repo>) can be served by the same file.
"""
import json
import os
import sys

_dir = os.environ.get("PHASE_TRANSITIONS_DIR", "/app/scripts/dispatcher")
_parent = os.environ.get("PHASE_TRANSITIONS_PARENT", "/app")
sys.path.insert(0, _dir)
sys.path.insert(0, _parent)

# Prefer the package-qualified import so tests running against the
# repo layout pick up the real module; fall back to a direct import
# for environments where only the module file is present.
try:
    from scripts.dispatcher import phase_transitions as pt  # noqa: E402
except ImportError:
    import phase_transitions as pt  # type: ignore  # noqa: E402

payload = json.load(sys.stdin)
current_phase = payload.get("current_phase", "")
output = payload.get("output") or {}
transition = pt.next_phase_from_verdict(current_phase, output)
fields = [
    transition.action.value if transition.action else "",
    transition.next_phase or "",
    transition.terminal_status or "",
    transition.failure_hint or "",
]
sys.stdout.write("\t".join(fields))
PYEOF
fi

transition_for() {
    # $1 = current phase, $2 = output JSON string (defaults to "{}").
    # See persist_phase_output for why we avoid ``${2:-{}}`` — bash's
    # parameter expansion parser turns that into `$2}` when $2 is set.
    _phase="$1"
    _output="${2-}"
    if [[ -z "$_output" ]]; then
        _output="{}"
    fi
    _payload=$(printf '{"current_phase": "%s", "output": %s}' "$_phase" "$_output")
    printf '%s' "$_payload" | python3 "$TRANSITION_SHIM"
}

# ── Phase runners -----------------------------------------------------------
#
# Each runner executes one phase and prints its phase-output JSON to
# stdout. Phases that invoke claude return the `{"verdict": "..."}`
# envelope from `claude -p --output-format json`; mechanical phases
# return a locally-constructed JSON (e.g. `{"no_op": false}` for
# push_and_pr).
#
# For Stage 1b the plan/ralph/summary/fix-ci/verify runners simply
# invoke `claude -p /task-v2-<phase> <agent_id>` and echo the parsed
# `.result` field. The daemon's richer behavior (streaming log
# forwarder #3017, stdout vs stderr split #2869, per-phase input
# bundles) lands in Stage 2. Stage 1b proves the wire — not the
# finish-line finesse.

run_claude_phase() {
    _phase="$1"
    _out_file="$AGENT_WORKSPACE/claude-p-$_phase.stdout.json"

    if [[ "$AGENT_RUNNER_DRY_RUN" == "1" ]]; then
        log "claude_phase_dry_run" "phase=$_phase"
        printf '{}'
        return 0
    fi

    log "claude_phase_begin" "phase=$_phase"
    # Do NOT fail the script on a non-zero exit — parse the envelope
    # and let the caller decide. Redirect stderr to a sibling file for
    # triage parity with the daemon.
    set +e
    claude -p "/task-v2-$_phase $AGENT_ID" \
        --output-format json \
        --dangerously-skip-permissions \
        > "$_out_file" \
        2> "$AGENT_WORKSPACE/claude-p-$_phase.stderr.log"
    _rc=$?
    set -e
    log "claude_phase_done" "phase=$_phase" "exit_code=$_rc"

    # The `result` field of `claude -p --output-format json` is either
    # a string (legacy path) or an object. The task-v2-* skills emit
    # JSON objects in `result` (parsed by the daemon); we forward it
    # unchanged for the transition shim.
    jq -c '.result // {}' "$_out_file" 2>/dev/null || printf '{}'
}

persist_phase_output() {
    # $1 = phase, $2 = output JSON.
    # Stage 1b writes a minimal row shape matching the daemon's
    # phase_outputs schema: (agent_id, phase, output_json) — NOT a
    # `status` column, which does not exist on `dispatcher.phase_outputs`
    # (#3115). The daemon's subprocess-mode persist writes the same
    # three columns. The `log_text` / `attempt` / token + cost columns
    # stay NULL here; Stage 2 wiring populates them once the daemon-
    # side log-capture path is in place.
    #
    # Default-substitute to an empty-object string if $2 is absent.
    # We spell the default in two stages rather than
    # ``${2:-{}}`` because bash's ``${param:-word}`` parser treats a
    # literal ``}`` inside ``word`` as the end of the expansion — so
    # ``${2:-{}}`` expands to ``$2}`` (two closing braces) when $2 is
    # set, producing malformed JSONB and a postgres syntax error.
    _phase="$1"
    _output_json="$2"
    if [[ -z "$_output_json" ]]; then
        _output_json="{}"
    fi
    _escaped=$(printf '%s' "$_output_json" | sed "s/'/''/g")
    db_exec "INSERT INTO dispatcher.phase_outputs (agent_id, phase, output_json)
             VALUES ('$AGENT_ID', '$_phase', '$_escaped'::jsonb);"
    log "phase_output_persisted" "phase=$_phase"
}

persist_ralph_patch() {
    # Called on ralph SHIP when the worktree has a staged/committed
    # diff. Reads `git format-patch -1 HEAD --stdout` and INSERTs the
    # content + HEAD sha into dispatcher.ralph_patches. Returns the
    # new patch_id on stdout (for reference linkage in phase_outputs).
    _commit_sha=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || printf '')
    if [[ -z "$_commit_sha" ]]; then
        log "ralph_patch_skip_no_head"
        return 0
    fi

    _patch_file="$AGENT_WORKSPACE/ralph-ship.patch"
    git -C "$REPO_ROOT" format-patch -1 HEAD --stdout > "$_patch_file" 2>/dev/null || true
    if [[ ! -s "$_patch_file" ]]; then
        log "ralph_patch_skip_empty"
        return 0
    fi

    # Escape single quotes for SQL literal.
    _escaped_patch=$(sed "s/'/''/g" "$_patch_file")
    _issue_clause="${ISSUE_NUMBER:-0}"
    db_exec "INSERT INTO dispatcher.ralph_patches
               (agent_id, issue_number, patch_content, commit_sha)
             VALUES
               ('$AGENT_ID', $_issue_clause, '$_escaped_patch', '$_commit_sha');"
    log "ralph_patch_persisted" "commit_sha=$_commit_sha"
}

advance_phase() {
    # $1 = next phase, $2 = terminal_status (optional).
    _next="$1"
    _status="${2:-}"
    if [[ -n "$_status" ]]; then
        db_exec "UPDATE dispatcher.agents
                    SET phase = '$_next', status = '$_status'
                  WHERE agent_id = '$AGENT_ID';"
    else
        db_exec "UPDATE dispatcher.agents
                    SET phase = '$_next'
                  WHERE agent_id = '$AGENT_ID';"
    fi
    log "phase_advanced" "next_phase=$_next" "status=${_status:-unchanged}"
}

mark_ended() {
    # Final update when the loop hits a terminal phase.
    db_exec "UPDATE dispatcher.agents
                SET ended_at = now()
              WHERE agent_id = '$AGENT_ID'
                AND ended_at IS NULL;"
    log "agent_ended"
}

# Check whether a phase is terminal per the shared Python module.
# Uses a sibling one-line shim written alongside the transition shim
# rather than an inline `python -c` heredoc (the preflight hook blocks
# multi-line `python -c` on operator shells).
TERMINAL_SHIM="${AGENT_RUNNER_TERMINAL_SHIM:-$AGENT_WORKSPACE/phase_terminal_shim.py}"
if [[ "$TERMINAL_SHIM" == "$AGENT_WORKSPACE/phase_terminal_shim.py" ]]; then
    cat <<'TERMEOF' > "$TERMINAL_SHIM"
"""Print 'yes' or 'no' for whether the phase on stdin is terminal."""
import os
import sys

_dir = os.environ.get("PHASE_TRANSITIONS_DIR", "/app/scripts/dispatcher")
_parent = os.environ.get("PHASE_TRANSITIONS_PARENT", "/app")
sys.path.insert(0, _dir)
sys.path.insert(0, _parent)

try:
    from scripts.dispatcher import phase_transitions as pt
except ImportError:
    import phase_transitions as pt  # type: ignore

phase = sys.stdin.read().strip()
sys.stdout.write("yes" if pt.is_terminal_phase(phase) else "no")
TERMEOF
fi

is_terminal() {
    _phase="$1"
    _result=$(printf '%s' "$_phase" | python3 "$TERMINAL_SHIM" 2>/dev/null || printf 'unknown')
    [[ "$_result" == "yes" ]]
}

# ── Main phase loop ---------------------------------------------------------
#
# Safety cap on iterations — if a bug causes the phase to advance to
# itself forever the container would run indefinitely (Fargate has no
# per-task timeout). 40 phase transitions is well above the happy-path
# depth (~10) and any realistic fix-ci loop.

MAX_PHASE_ITERATIONS="${AGENT_RUNNER_MAX_PHASE_ITERATIONS:-40}"
_iter=0

while true; do
    _iter=$((_iter + 1))
    if [[ $_iter -gt $MAX_PHASE_ITERATIONS ]]; then
        log "phase_iteration_cap_hit" "cap=$MAX_PHASE_ITERATIONS"
        exit 3
    fi

    _current=$(read_current_phase)
    if [[ -z "$_current" ]]; then
        log "agent_row_missing"
        exit 4
    fi

    log "phase_loop_tick" "iteration=$_iter" "current_phase=$_current"

    if is_terminal "$_current"; then
        log "phase_terminal" "phase=$_current"
        mark_ended
        exit 0
    fi

    # Phases the Stage 1b entrypoint actually runs. Each case writes
    # its phase_output, computes the transition, advances, and loops.
    case "$_current" in
        claiming|planning|ralph|summary|push_and_pr|fix_ci|verify)
            _output=$(run_claude_phase "$_current")
            persist_phase_output "$_current" "$_output"
            if [[ "$_current" == "ralph" ]]; then
                # Mirror the daemon's post-SHIP ralph_patches persist.
                _verdict=$(printf '%s' "$_output" | jq -r '.verdict // ""')
                if [[ "$_verdict" == "SHIP" ]]; then
                    persist_ralph_patch
                fi
            fi
            _transition=$(transition_for "$_current" "$_output")
            _action=$(printf '%s' "$_transition" | cut -f1)
            _next=$(printf '%s' "$_transition" | cut -f2)
            _status=$(printf '%s' "$_transition" | cut -f3)
            _hint=$(printf '%s' "$_transition" | cut -f4)

            case "$_action" in
                advance)
                    advance_phase "$_next"
                    ;;
                advance_with_status)
                    advance_phase "$_next" "$_status"
                    ;;
                route_to_diagnoser)
                    # Stage 1b does not have the diagnoser integration
                    # wired in; route to a terminal failure phase so
                    # the loop exits cleanly. Stage 2's daemon-side
                    # failure router owns the real category mapping.
                    log "diagnoser_route_stub" "hint=$_hint"
                    advance_phase "daemon_restart_abandoned" "failed"
                    ;;
                unrecognized|*)
                    log "transition_unrecognized" "phase=$_current" "action=$_action"
                    advance_phase "daemon_restart_abandoned" "crashed"
                    ;;
            esac
            ;;
        awaiting_ci|merge|awaiting_deploy|retro|setup)
            # Mechanical phases the daemon handles inline today. Stage
            # 1b stubs them: log + advance to the documented "next"
            # phase on the happy path so the smoke test can reach
            # `done` without a full daemon-integration.
            case "$_current" in
                setup)            _next="ralph" ;;
                awaiting_ci)      _next="merge" ;;
                merge)            _next="awaiting_deploy" ;;
                awaiting_deploy)  _next="verify" ;;
                retro)            _next="retro_done" ;;
            esac
            log "mechanical_phase_stub" "phase=$_current" "next=$_next"
            persist_phase_output "$_current" '{"stub": true}'
            advance_phase "$_next"
            ;;
        *)
            log "phase_unknown" "phase=$_current"
            advance_phase "daemon_restart_abandoned" "crashed"
            ;;
    esac
done
