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
# `daemon._handle_phase_*` — split across three tiers in the Stage 1b
# entrypoint (post-#3117):
#
#   1. **Claude-driven phases** (planning, ralph, summary, fix_ci,
#      verify) → `run_claude_phase "<phase>"` which looks up the skill
#      name via `phase_to_skill` and invokes `claude -p /task-v2-...`.
#   2. **Mechanical pseudo-phase: claiming** → no-op; advances
#      immediately to `planning`. The daemon has already written the
#      agent row by the time this task boots, so the claim step is
#      nothing but a lifecycle marker.
#   3. **Mechanical phases with side effects** — `push_and_pr`,
#      `awaiting_ci`, `merge`, and `awaiting_deploy` have in-process
#      implementations mirroring the subprocess daemon's
#      ``_push_and_open_pr`` / ``_advance_awaiting_ci`` /
#      ``_merge_pr_and_advance`` / ``_advance_awaiting_deploy``. These
#      are the critical post-ralph output-actions — without them an
#      ECS agent opens a PR and then races through the remaining
#      mechanical phases with stubs, abandoning the PR (see #3176).
#      `retro` and `setup` remain stubbed on this path; Stage 3+
#      wires them.
#
# The phase → skill-name mapping (`phase_to_skill`) is explicit and
# dies on an unknown phase. Prior to #3117 the entrypoint constructed
# `/task-v2-$_phase` directly, which silently failed with "Unknown
# command" for the `planning`/`plan` and `fix_ci`/`fix-ci` drift and
# for the two mechanical phases (`claiming`, `push_and_pr`) that
# never had skills.
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
#
# #4137: ``set -euo pipefail`` and ``exec 3>&1`` moved into ``main()``
# (defined at the bottom of this file) so sourcing the script is
# side-effect free. The script remains executable as an entrypoint via
# the sourcing guard at the very end (``if [[ "${BASH_SOURCE[0]}" ==
# "${0}" ]]; then main "$@"; fi``).

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

# #4137: ``exec 3>&1`` moved into ``main()`` so sourcing this file does
# not redirect fd 3 in the calling shell. ``log()`` continues to write
# to fd 3; tests that source individual handler functions explicitly
# install their own ``exec 3>&1`` in their fixture shell (see e.g.
# ``test_agent_runner_entrypoint.sh`` line ~3650).

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

# #4137: validation of required env vars wrapped in ``_validate_required_env``
# so it runs from ``main()`` (not at top level on source).
_validate_required_env() {
    if [[ -z "$AGENT_ID" ]]; then
        die "AGENT_ID_unset"
    fi

    if [[ -z "$DATABASE_URL" ]]; then
        die "DATABASE_URL_unset"
    fi
}

# ── START_PHASE early validation (#3366) ───────────────────────────────────
#
# Fast-fail validation BEFORE the clone / git fetch so an invalid
# value never gets the chance to run any side effects. The main-loop
# block below still re-reads START_PHASE and writes to the agent row,
# but the kept-here early reject is what scripts/tests can drive without
# needing the full git/gh/psql stub harness.
#
# Whitespace-separated list; mirror of
# ``daemon.AGENT_RUNNER_VALID_START_PHASES``. The two sides are tested
# for parity by ``scripts/tests/test_agent_runner_start_phase.sh``.
AGENT_RUNNER_VALID_START_PHASES="planning setup ralph summary push_and_pr awaiting_ci fix_ci merge awaiting_deploy verify"

# #4137: START_PHASE early validation wrapped in
# ``_validate_start_phase_early`` so it runs from ``main()``.
_validate_start_phase_early() {
    if [[ -n "${START_PHASE:-}" ]]; then
        _start_phase_valid=0
        for _vp in $AGENT_RUNNER_VALID_START_PHASES; do
            if [[ "$START_PHASE" == "$_vp" ]]; then
                _start_phase_valid=1
                break
            fi
        done
        if [[ "$_start_phase_valid" -ne 1 ]]; then
            log "start_phase_override_invalid" \
                "requested=$START_PHASE" \
                "valid_phases=$AGENT_RUNNER_VALID_START_PHASES"
            printf '[agent-runner-entrypoint] START_PHASE=%s is not in the valid set: %s\n' \
                "$START_PHASE" "$AGENT_RUNNER_VALID_START_PHASES" >&2
            exit 1
        fi
        log "start_phase_override" "start_phase=$START_PHASE"
    fi
}

# #4137: branch-name derivation wrapped in ``_init_branch_naming`` so it
# runs from ``main()``. The function reads + writes the SHORT_ID and
# BRANCH_NAME global vars (referenced later by ``_checkout_branch`` and
# the phase loop) — same scope as before, just delayed until main().
_init_branch_naming() {
    # Derive a short id for branch naming (first 8 chars of the agent uuid).
    SHORT_ID=$(printf '%s' "$AGENT_ID" | cut -c1-8)
    if [[ -z "$BRANCH_NAME" ]]; then
        BRANCH_NAME="agent/$SHORT_ID"
    fi

    log "startup" "issue_number=$ISSUE_NUMBER" "branch=$BRANCH_NAME" "short_id=$SHORT_ID"
}

# ── Workspace + clone -------------------------------------------------------
#
# Per-agent clone under $AGENT_WORKSPACE/repo. The Fargate ephemeral
# storage root (/var/lib/agent-runner in production, a tmpdir under
# test) starts empty on every task start — no `git worktree` games,
# no sweep-between-runs.

REPO_ROOT="$AGENT_WORKSPACE/repo"

# #4137: workspace mkdir + clone wrapped in ``_setup_workspace_and_clone``.
# Body kept at original indentation; bash ignores indentation.
_setup_workspace_and_clone() {
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
}

# #4137: gh auth wrapped in ``_setup_gh_auth``.
_setup_gh_auth() {
    # Authenticate gh + git against the scoped PAT if available. `gh auth
    # setup-git` wires the helper into ~/.gitconfig so every subsequent
    # `git push` uses the PAT.
    if [[ -n "$GITHUB_TOKEN" ]]; then
        log "gh_auth_begin"
        printf '%s' "$GITHUB_TOKEN" | gh auth login --with-token >/dev/null 2>&1 || true
        gh auth setup-git >/dev/null 2>&1 || true
        log "gh_auth_done"
    fi
}

# #4137: cd + git checkout wrapped in ``_checkout_branch``.
_checkout_branch() {
    cd "$REPO_ROOT"

    # Create the per-agent branch off origin/main.
    git checkout -B "$BRANCH_NAME" origin/main
    log "branch_ready" "branch=$BRANCH_NAME"
}

# ── Install Fargate-narrowed preflight hook (#3757) -------------------------
#
# The repo ships `.claude/hooks/preflight-bash.sh` with the FULL
# operator-laptop ruleset, which blocks `;`+double-quoted-string Bash
# patterns the ralph Step 2.5 pre-push command uses. On the daemon
# (subprocess execution), `daemon.py::_install_fargate_preflight_hook`
# swaps in the narrowed `scripts/preflight-bash-fargate.sh` after each
# `git worktree add`. The agent-runner ECS image needs the equivalent
# in shell because the entrypoint clones into `$REPO_ROOT` directly.
#
# Without this swap, ECS-mode ralph workers exit silently at the pre-push
# gate (root cause of #3757). The Dockerfile stages the narrowed hook at
# `/app/fargate-hooks/` and exports `DISPATCHER_FARGATE_HOOKS_DIR`; the
# helper does the rest. Operator-laptop runs (env unset) skip the swap.

# shellcheck source=./agent_runner_install_fargate_hook.sh
source "$(dirname "${BASH_SOURCE[0]}")/agent_runner_install_fargate_hook.sh"
# #4137: ``install_fargate_preflight_hook`` invocation moved into ``main()``.
# The ``source`` line stays at top level so sourcing the entrypoint also
# exposes the helper's function definitions in the calling shell.

# ── Layer 4 silent-ralph fallback helper (#3782) ----------------------------
#
# Defines `synthesize_ralph_output_from_done_marker <worktree>` for the
# phase loop's `_output == "{}"` && `_current == "ralph"` branch. When
# the inner `/task-v2-ralph` skill completes its conversation cleanly
# but never executes Step 4 (the Write of `dispatcher-output/ralph.json`),
# the wrapper falls back to synthesising a structured verdict from the
# already-written `tmp/ralph/ralph-done.txt`. Layers 1-3 of the saga
# (#3694, #3757, #3766) reduced the silent-exit cohort from "model
# stopped early + permission denied + SIGKILL" to just "model stopped
# early"; Layer 4 closes the residual gap.

# shellcheck source=./agent_runner_synthesize_ralph_output.sh
source "$(dirname "${BASH_SOURCE[0]}")/agent_runner_synthesize_ralph_output.sh"

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

# #4137: ``apply_prior_patch`` invocation moved into ``main()``.

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

read_agent_kind() {
    # Issue #3374. Read ``dispatcher.agents.kind`` so the phase-loop
    # dispatch can branch on synthetic scheduled-skill agents
    # (``kind='scheduled_skill'``) — those run ``claude -p /<phase>``
    # directly instead of going through the plan→ralph→summary→PR
    # pipeline. Returns the empty string when the row is missing
    # (caller falls back to the standard phase router).
    db_query_one "SELECT kind
                    FROM dispatcher.agents
                   WHERE agent_id = '$AGENT_ID'
                   LIMIT 1;"
}

read_agent_status() {
    # Issue #3166. Read ``dispatcher.agents.status`` so the phase loop
    # can detect an externally-written terminal status (diagnoser, supervisor,
    # killswitch) and exit 0 before running the next phase handler.
    # Returns the empty string when the row is missing.
    db_query_one "SELECT status
                    FROM dispatcher.agents
                   WHERE agent_id = '$AGENT_ID'
                   LIMIT 1;"
}

# ── ECS task-ARN capture (#3694) ───────────────────────────────────────────
#
# Set ``dispatcher.agents.agent_task_arn`` from the ECS metadata endpoint
# early in the entrypoint so a future diagnoser can pull the task's
# CloudWatch logs without hunting through ECS describe-tasks state.
# Mirrors the same column the daemon-side ``_launch_agent_ecs_task``
# already populates on the launch-result path; this fills it in for the
# in-task launch ordering that #3694 caught producing
# ``agent_task_arn=NULL`` rows on every silent ralph_not_ship failure.
#
# Failure modes (all best-effort, must never abort the runner):
#
#   * ``$ECS_CONTAINER_METADATA_URI_V4`` unset (subprocess-mode, local
#     test, non-ECS container) → log + skip, no DB write.
#   * ``curl`` not on PATH (slim image variant) → log + skip.
#   * Metadata endpoint returns non-zero / non-JSON / missing
#     ``.TaskARN`` field → log + skip with the response head for
#     triage.
#   * DB UPDATE fails (DATABASE_URL transient outage) → log + skip;
#     the agent row keeps ``agent_task_arn=NULL`` and the daemon's
#     existing periodic ARN-backfill (#3158) can fill it later.
#
# Tests can stub this by setting ``AGENT_RUNNER_SKIP_TASK_ARN_CAPTURE=1``
# OR by leaving ``ECS_CONTAINER_METADATA_URI_V4`` unset (the natural
# state for non-ECS runs).
set_agent_task_arn_from_metadata() {
    if [[ "${AGENT_RUNNER_SKIP_TASK_ARN_CAPTURE:-0}" == "1" ]]; then
        log "agent_task_arn_capture_skipped" "reason=env_skip"
        return 0
    fi
    _meta_url="${ECS_CONTAINER_METADATA_URI_V4:-}"
    if [[ -z "$_meta_url" ]]; then
        log "agent_task_arn_capture_skipped" "reason=no_metadata_uri"
        return 0
    fi
    if ! command -v curl >/dev/null 2>&1; then
        log "agent_task_arn_capture_skipped" "reason=curl_missing"
        return 0
    fi
    _meta_body_file="$AGENT_WORKSPACE/ecs-metadata.json"
    set +e
    curl --fail --silent --show-error --max-time 5 \
        "$_meta_url/task" \
        > "$_meta_body_file" \
        2> "$AGENT_WORKSPACE/ecs-metadata.stderr.log"
    _meta_rc=$?
    set -e
    if [[ "$_meta_rc" -ne 0 ]]; then
        _meta_err_tail=$(head -c 200 "$AGENT_WORKSPACE/ecs-metadata.stderr.log" 2>/dev/null \
            | tr '\n\r\t' '   ')
        log "agent_task_arn_capture_failed" \
            "reason=curl_exit_${_meta_rc}" \
            "stderr_tail=$_meta_err_tail"
        return 0
    fi
    _task_arn=$(jq -r '.TaskARN // empty' "$_meta_body_file" 2>/dev/null || printf '')
    if [[ -z "$_task_arn" ]]; then
        _meta_head=$(head -c 200 "$_meta_body_file" 2>/dev/null | tr '\n\r\t' '   ')
        log "agent_task_arn_capture_failed" \
            "reason=task_arn_missing" \
            "body_head=$_meta_head"
        return 0
    fi
    # UPDATE the agent row. Best-effort — if DATABASE_URL is transiently
    # unreachable we don't want to block the runner. The daemon's periodic
    # backfill sweep already handles late-arriving ARNs for legacy rows.
    set +e
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
        -v agent_id="$AGENT_ID" \
        -v task_arn="$_task_arn" <<'EOF' >/dev/null 2>&1
UPDATE dispatcher.agents
   SET agent_task_arn = :'task_arn'
 WHERE agent_id = :'agent_id';
EOF
    _arn_rc=$?
    set -e
    if [[ "$_arn_rc" -ne 0 ]]; then
        log "agent_task_arn_capture_failed" \
            "reason=db_update_exit_${_arn_rc}" \
            "task_arn=$_task_arn"
        return 0
    fi
    log "agent_task_arn_captured" "task_arn=$_task_arn"
}

# #4137: ``set_agent_task_arn_from_metadata`` invocation moved into
# ``main()``. The capture still runs as early as possible — right after
# the DB helpers are defined and main() has set up the workspace + clone.
# Skipped automatically when ``ECS_CONTAINER_METADATA_URI_V4`` is unset
# (subprocess mode + tests).

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

# #4137: TRANSITION_SHIM stamp + smoke validation wrapped in
# ``_setup_transition_shim`` so it runs from ``main()``.
_setup_transition_shim() {
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
output = payload.get("output")
# Defensive coercion (#3117): a failed claude invocation (e.g. sandbox
# deny, skill-name typo returning "Unknown command: ...", network
# error) can surface as a plain-string `.result`. The downstream
# transition functions call ``output.get("verdict")``, which crashes
# with AttributeError on a str. Coerce any non-dict output to {} so
# the transition falls through the missing-verdict branch and the
# caller persists a structured failure row instead of crashing the
# runner with a stack trace.
if not isinstance(output, dict):
    output = {}
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

# ── Startup validation: TRANSITION_SHIM (#3410) ────────────────────────────
#
# Fast-fail before the first claude invocation. Catches: missing file,
# +x lost, wrong interpreter, sys.path misconfigured. The smoke payload
# uses phase=plan so the shim exercises the real import path.
#
# The check runs unconditionally (whether we stamped the shim above or
# accepted a pre-set AGENT_RUNNER_TRANSITION_SHIM from the caller) so
# a test harness that injects a bad path also fails early.
if [[ ! -s "$TRANSITION_SHIM" ]]; then
    log "transition_shim_invalid" \
        "path=$TRANSITION_SHIM" \
        "reason=missing_or_empty"
    die "transition_shim_invalid"
fi

if ! command -v python3 >/dev/null 2>&1; then
    log "transition_shim_invalid" \
        "path=$TRANSITION_SHIM" \
        "reason=python3_not_on_path"
    die "transition_shim_invalid"
fi

_shim_smoke_err_path="$AGENT_WORKSPACE/shim-smoke.err"
_shim_smoke_in_path="$AGENT_WORKSPACE/shim-smoke.in"
printf '{"current_phase":"plan","output":{}}' > "$_shim_smoke_in_path"
set +e
_shim_smoke_out=$(python3 "$TRANSITION_SHIM" < "$_shim_smoke_in_path" 2>"$_shim_smoke_err_path")
_shim_smoke_rc=$?
set -e
if [[ "$_shim_smoke_rc" -ne 0 ]] || ! printf '%s' "$_shim_smoke_out" | grep -q $'\t'; then
    _shim_smoke_err=$(head -5 "$_shim_smoke_err_path" 2>/dev/null | tr '\n' ' ')
    log "transition_shim_invalid" \
        "path=$TRANSITION_SHIM" \
        "reason=smoke_failed" \
        "exit_code=$_shim_smoke_rc" \
        "stderr_tail=$_shim_smoke_err"
    die "transition_shim_invalid"
fi
log "transition_shim_ok" "path=$TRANSITION_SHIM"
}

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

# ── Python helper: phase_input shim (#3133) ────────────────────────────────
#
# The task-v2-* skills read their inputs from
# ``{worktree}/tmp/dispatcher-input/<phase>.json``. The daemon's
# subprocess path writes those files via ``_write_phase_input`` before
# spawning each ``claude -p`` subprocess — and in the absence of that
# file each skill's guard clause fires, short-circuiting to a
# ``go=false`` / ``verdict=BLOCKED`` fallback and writing a human-
# readable reason to stdout as a plain-string ``.result`` (captured by
# #3131's diag). The agent-runner has no equivalent plumbing, so every
# ECS-mode agent before #3133 hit this input-missing path on planning
# and terminated at ``daemon_restart_abandoned``.
#
# This shim mirrors a minimal subset of the daemon's
# :meth:`DispatcherDaemon._fetch_issue_bundle` +
# :meth:`_write_phase_input` so each claude phase sees a well-formed
# input file at the contract's expected path. It shells out to ``gh
# issue view --json`` for the plan-phase issue bundle (the daemon's
# own path uses the same CLI); non-interactive auth is already wired
# up in the entrypoint's ``gh auth login --with-token`` step earlier.
#
# Stage 2 scope (#3135): every claude-driven phase's input is built to
# the same shape the daemon's ``_handle_phase_*`` builders assemble.
# Plan + ralph continue to mirror the daemon's identifier + issue
# bundle; summary now carries ralph_summary + git_diff + branch +
# plan-derived acceptance_criteria; fix-ci reads pr_number + the PR's
# failing-job metadata + per-job log tails via ``gh run view
# --log-failed --job``; verify reads the merged commit SHA + deploy-
# run conclusion + deferred_acs (from summary's persisted output);
# retro reads phase_transitions + failures from their respective
# ``dispatcher.*`` tables plus counter derivations that match the
# daemon's ``_build_retro_input``. Every gh / psql call is best-
# effort — a missing row / 404 returns an empty value so each skill's
# own guard clauses still produce a structured BLOCKED / FAILED
# verdict when data genuinely isn't available.
#
# Module lookup matches phase_transitions_shim.py: PHASE_INPUT_DIR /
# PHASE_INPUT_PARENT env vars let tests stub the script at a writable
# location without bake-time paths.

PHASE_INPUT_SHIM="${AGENT_RUNNER_PHASE_INPUT_SHIM:-$AGENT_WORKSPACE/phase_input_shim.py}"

# #4137: PHASE_INPUT_SHIM stamping wrapped in ``_setup_phase_input_shim``
# so it runs from ``main()``.
_setup_phase_input_shim() {
    if [[ "$PHASE_INPUT_SHIM" == "$AGENT_WORKSPACE/phase_input_shim.py" ]]; then
    _input_shim_path="$AGENT_WORKSPACE/phase_input_shim.py"
    cat <<'PYEOF' > "$_input_shim_path"
"""Entrypoint-internal shim: build ``dispatcher-input/<phase>.json``.

Invoked with argv = [phase, agent_id, issue_number, repo_root]. Writes
``{repo_root}/tmp/dispatcher-input/<phase>.json`` matching each skill's
input contract (see .claude/skills/task-v2-<phase>/SKILL.md).

Stage 2 scope (#3135): every phase's input is built to the same shape
the daemon's ``_handle_phase_*`` builders assemble, so ECS-mode agents
reach the same verdicts as subprocess-mode agents. Specifically:

  * planning — full input via ``gh issue view --json`` (mirrors the
    daemon's ``_fetch_issue_bundle``). Non-bot comments filtered.
    ``Blocked by #N`` + ``Parent: #N`` parsed from body.
  * ralph — plan output from ``dispatcher-output/plan.json`` + the
    identifiers; ``max_iterations`` defaults to 5 matching the daemon.
  * summary — refetched issue bundle, ralph_summary from
    ``dispatcher-output/ralph.json``, ``changed_files``, ``git_diff``
    against ``origin/main``, ``branch``, and plan-derived
    ``plan_acceptance_criteria`` + ``scope_check``.
  * fix-ci — ``pr_number`` from ``dispatcher.agents`` plus the
    failing-job metadata via ``gh pr view --json statusCheckRollup``,
    each enriched with ``log_tail`` via
    ``gh run view --log-failed --job <id>``. ``git_diff_base_to_head``
    from ``gh pr diff``, ``previous_fix_attempts`` from
    ``dispatcher.agents.retries_used``.
  * verify — ``pr_number`` + ``merged_commit_sha`` from
    ``gh pr view --json mergeCommit``; ``deploy_status`` +
    ``touched_services`` + ``change_type`` from ``gh run list``
    filtered to the merge SHA; ``deferred_acs`` from
    ``dispatcher.phase_outputs WHERE phase='summary'``;
    ``acceptance_criteria`` extracted from the issue body via the same
    regex the daemon uses.
  * retro — ``phase_transitions`` + ``failures`` from their
    respective ``dispatcher.*`` tables, ``ralph_iterations`` /
    ``ci_attempts`` / ``fix_ci_attempts`` derived from the transitions
    log, ``total_duration_s`` from ``now() - agents.started_at``,
    ``scope_check_followups`` / ``plan_follow_ups`` from plan output,
    ``verify_evidence_md`` from the verify phase_outputs row.

All DB reads go through ``psql $DATABASE_URL -At`` (the same
connection string the entrypoint already trusts for its own
``db_exec`` / ``db_query_one`` shell helpers). GitHub reads go through
``gh`` which is already authenticated by the entrypoint's
``gh auth login --with-token`` step. Every fetch is best-effort: a
missing row or a ``gh`` 404 returns an empty / zero value so the
skill's own guard clauses are what trigger a BLOCKED verdict — never
an uncaught exception from this shim.

Exit codes: 0 on write success, 2 on unrecoverable error (caller
should log + continue — the skill's missing-input path still works).
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path


# Locate scripts/dispatcher/ on sys.path so the shim can import the
# canonical CI-classifier helper from phase_transitions.py — single
# source of truth across the daemon (subprocess) and entrypoint
# (Fargate) paths.  In the deployed agent-runner image the dispatcher
# directory lives at /app/scripts/dispatcher/ (see
# Dockerfile.dispatcher-agent-runner). In tests we fall back to the
# repo's ``scripts/dispatcher`` directory relative to ``REPO_ROOT``.
# #4417: pre-refactor the shim spelled out its own ``failure_conclusions``
# set; shared helper eliminates the divergence vector.
for _candidate in (
    Path("/app/scripts/dispatcher"),
    Path(os.environ.get("REPO_ROOT", "")) / "scripts" / "dispatcher",
):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))


# Cap on failing jobs embedded in fix-ci input — mirrors the daemon's
# ``FIX_CI_MAX_FAILING_JOBS`` constant. Keeps the payload bounded when
# a PR has dozens of red checks (e.g. matrix explosions).
FIX_CI_MAX_FAILING_JOBS = 10

# Cap on log bytes captured per failing job. The daemon lets the skill
# pull tails itself, but in ECS mode we do it here because the skill's
# container does not have PAT scopes for CloudWatch Logs / Actions API.
# 200 lines * ~200 bytes/line = ~40KB; cap to 64KB per job for safety.
FIX_CI_LOG_TAIL_BYTES = 64 * 1024


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Thin wrapper so tests can stub via PATH shims."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# ─────────────────────────────────────────────────────────────────────
# Parsing helpers (mirror DispatcherDaemon)
# ─────────────────────────────────────────────────────────────────────


def _parse_blocked_by(body: str) -> list[int]:
    """Mirror DispatcherDaemon._parse_blocked_by."""
    return [int(m) for m in re.findall(r"(?im)^\s*blocked by\s+#(\d+)\s*$", body)]


def _parse_parent_issue(body):
    """Delegate to the canonical Parent: #N parser — see #4508.

    Imports are deferred to call-time because the shim runs in
    environments where the dispatcher directory may not yet be on
    sys.path (e.g. test fixtures that set ``PHASE_TRANSITIONS_DIR``
    rather than ``REPO_ROOT`` to point at the dispatcher source). The
    sys.path-setup loop near the top of this shim covers the production
    paths (``/app/scripts/dispatcher`` and ``$REPO_ROOT/scripts/dispatcher``);
    when neither resolves we additionally probe ``PHASE_TRANSITIONS_DIR``
    here. If even that fails the function returns None — ``parent_issue``
    is a best-effort metadata field for the planning input bundle, not a
    correctness gate, so degrading gracefully is fine.
    """
    try:
        from parent_issue import parse_parent_issue as _impl  # noqa: PLC0415
    except ImportError:
        # Fallback: try the test-fixture env var that names the dispatcher
        # directory directly (used by scripts/tests/test_agent_runner_entrypoint.sh).
        _alt_dir = os.environ.get("PHASE_TRANSITIONS_DIR", "")
        if _alt_dir and Path(_alt_dir).is_dir() and _alt_dir not in sys.path:
            sys.path.insert(0, _alt_dir)
            try:
                from parent_issue import parse_parent_issue as _impl  # noqa: PLC0415
            except ImportError:
                return None
        else:
            return None
    return _impl(body)


def _extract_acceptance_criteria(body: str) -> list[str]:
    """Mirror DispatcherDaemon._extract_acceptance_criteria.

    Pull ``- [ ] …`` checkboxes out of the issue body, skipping
    verification / automated-checks / test-plan sections.
    """
    lines = body.splitlines()
    skip_sections = {"post-deploy verification", "automated checks", "test plan"}
    in_skip_section = False
    criteria: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            heading_text = stripped.lstrip("#").strip().lower()
            in_skip_section = any(tag in heading_text for tag in skip_sections)
            continue
        if in_skip_section:
            continue
        match = re.match(r"^\s*-\s*\[[ xX]\]\s*(.+)$", line)
        if match:
            criteria.append(match.group(1).strip())
    return criteria


# ─────────────────────────────────────────────────────────────────────
# DB helpers (shell out to psql — entrypoint already uses it)
# ─────────────────────────────────────────────────────────────────────


def _db_query_one(sql: str) -> str:
    """Execute a SELECT against ``$DATABASE_URL`` and return stdout.

    Uses ``psql -At`` (unaligned, tuples-only) so the result is the
    raw first-row value. Empty string on error — the caller is
    expected to tolerate missing data.
    """
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        return ""
    try:
        outcome = _run(
            ["psql", database_url, "-v", "ON_ERROR_STOP=1", "-At", "-c", sql],
            timeout=20,
        )
    except Exception:
        return ""
    if outcome.returncode != 0:
        return ""
    return (outcome.stdout or "").rstrip("\n")


def _db_query_rows(sql: str, field_sep: str = "\t") -> list[list[str]]:
    """Execute a SELECT and return a list of row field lists.

    Uses ``psql -At -F <sep>`` so each line is one row and each field
    is separated by ``field_sep``. Default TAB — tolerates values with
    newlines only if the caller ensures there are none (e.g. by
    rtrimming / replacing in SQL). Empty list on error.
    """
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        return []
    try:
        outcome = _run(
            [
                "psql",
                database_url,
                "-v",
                "ON_ERROR_STOP=1",
                "-At",
                "-F",
                field_sep,
                "-c",
                sql,
            ],
            timeout=20,
        )
    except Exception:
        return []
    if outcome.returncode != 0:
        return []
    rows: list[list[str]] = []
    for line in (outcome.stdout or "").splitlines():
        if not line:
            continue
        rows.append(line.split(field_sep))
    return rows


def _db_fetch_agent_pr_number(agent_id: str) -> int:
    """Return the agent's ``dispatcher.agents.pr_number`` or 0."""
    # Escape single quotes in agent_id for the SQL literal. Agent IDs
    # are UUIDs so this is defensive but not load-bearing.
    agent_sql = agent_id.replace("'", "''")
    raw = _db_query_one(
        "SELECT COALESCE(pr_number::text, '0') "
        "FROM dispatcher.agents "
        f"WHERE agent_id = '{agent_sql}' "
        "LIMIT 1;"
    )
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def _db_fetch_agent_retries_used(agent_id: str) -> int:
    """Return ``dispatcher.agents.retries_used`` or 0."""
    agent_sql = agent_id.replace("'", "''")
    raw = _db_query_one(
        "SELECT COALESCE(retries_used, 0)::text "
        "FROM dispatcher.agents "
        f"WHERE agent_id = '{agent_sql}' "
        "LIMIT 1;"
    )
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def _db_fetch_agent_total_duration_s(agent_id: str) -> int:
    """Seconds since ``dispatcher.agents.started_at``.

    Matches DispatcherDaemon._fetch_agent_total_duration_s. 0 on error
    or missing row.
    """
    agent_sql = agent_id.replace("'", "''")
    raw = _db_query_one(
        "SELECT COALESCE("
        "  EXTRACT(EPOCH FROM (now() - started_at))::int, "
        "  0"
        ")::text "
        "FROM dispatcher.agents "
        f"WHERE agent_id = '{agent_sql}' "
        "LIMIT 1;"
    )
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def _db_fetch_phase_output(agent_id: str, phase: str) -> dict:
    """Read ``dispatcher.phase_outputs.output_json`` for (agent_id, phase).

    Returns the parsed JSON object or ``{}`` if missing / malformed /
    not-a-dict. Mirrors DispatcherDaemon._fetch_phase_output.
    """
    agent_sql = agent_id.replace("'", "''")
    phase_sql = phase.replace("'", "''")
    raw = _db_query_one(
        "SELECT output_json::text "
        "FROM dispatcher.phase_outputs "
        f"WHERE agent_id = '{agent_sql}' AND phase = '{phase_sql}' "
        "ORDER BY ts DESC LIMIT 1;"
    )
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _db_fetch_phase_transitions(agent_id: str) -> list[dict]:
    """Read ``dispatcher.phase_transitions`` ordered oldest-first.

    Returns ``[{phase, ts}]``. Mirrors
    DispatcherDaemon._fetch_phase_transitions — the retro skill's
    input contract lists richer fields (``started_at`` / ``ended_at`` /
    ``duration_s`` / ``outcome``) but the daemon only populates
    ``{phase, ts}`` because that's what the table stores. We match.
    """
    agent_sql = agent_id.replace("'", "''")
    rows = _db_query_rows(
        "SELECT phase, ts::text "
        "FROM dispatcher.phase_transitions "
        f"WHERE agent_id = '{agent_sql}' "
        "ORDER BY ts ASC;"
    )
    result: list[dict] = []
    for row in rows:
        if len(row) < 2:
            continue
        result.append({"phase": row[0], "ts": row[1]})
    return result


def _db_fetch_failures_grouped(agent_id: str) -> list[dict]:
    """Read ``dispatcher.failures`` grouped by category, highest-count first.

    Returns ``[{category, count, first_seen, last_seen}]``. Mirrors
    DispatcherDaemon._fetch_failures_grouped.
    """
    agent_sql = agent_id.replace("'", "''")
    rows = _db_query_rows(
        "SELECT category, count(*)::text, min(ts)::text, max(ts)::text "
        "FROM dispatcher.failures "
        f"WHERE agent_id = '{agent_sql}' "
        "GROUP BY category "
        "ORDER BY count(*) DESC;"
    )
    result: list[dict] = []
    for row in rows:
        if len(row) < 4:
            continue
        try:
            count = int(row[1])
        except ValueError:
            count = 0
        result.append(
            {
                "category": row[0],
                "count": count,
                "first_seen": row[2],
                "last_seen": row[3],
            }
        )
    return result


# ─────────────────────────────────────────────────────────────────────
# GitHub helpers
# ─────────────────────────────────────────────────────────────────────


def _fetch_issue_bundle(repo: str, issue_number: int) -> dict:
    """Mirror DispatcherDaemon._fetch_issue_bundle (minus metering)."""
    if not issue_number:
        return {
            "issue_number": 0,
            "issue_title": "",
            "issue_body": "",
            "issue_comments": [],
            "issue_labels": [],
            "blocked_by": [],
            "parent_issue": None,
            "issue_updated_at": "",
        }
    cmd = [
        "gh",
        "issue",
        "view",
        str(issue_number),
        "--repo",
        repo,
        "--json",
        "number,title,body,labels,comments,updatedAt",
    ]
    try:
        outcome = _run(cmd, timeout=30)
    except Exception:
        # Timeout or other subprocess error — best-effort: return empty
        # bundle so the skill's guard clause still produces a clean
        # go=false rather than propagating the exception to main().
        return {
            "issue_number": issue_number,
            "issue_title": "",
            "issue_body": "",
            "issue_comments": [],
            "issue_labels": [],
            "blocked_by": [],
            "parent_issue": None,
            "issue_updated_at": "",
        }
    if outcome.returncode != 0:
        # Fall back to empty bundle so the skill's guard clause still
        # produces a clean go=false rather than a crash. The daemon's
        # path raises; the agent-runner's is best-effort.
        return {
            "issue_number": issue_number,
            "issue_title": "",
            "issue_body": "",
            "issue_comments": [],
            "issue_labels": [],
            "blocked_by": [],
            "parent_issue": None,
            "issue_updated_at": "",
        }
    try:
        payload = json.loads(outcome.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    raw_comments = payload.get("comments") or []
    filtered: list[dict] = []
    for comment in raw_comments:
        if not isinstance(comment, dict):
            continue
        author = comment.get("author") or {}
        login = author.get("login", "") if isinstance(author, dict) else ""
        if login.endswith("[bot]"):
            continue
        filtered.append(
            {
                "author": login,
                "author_association": comment.get("authorAssociation", ""),
                "date": comment.get("createdAt", ""),
                "body": comment.get("body", ""),
            }
        )
    labels = [
        entry.get("name", "")
        for entry in (payload.get("labels") or [])
        if isinstance(entry, dict)
    ]
    body = payload.get("body") or ""
    return {
        "issue_number": issue_number,
        "issue_title": payload.get("title", ""),
        "issue_body": body,
        "issue_comments": filtered,
        "issue_labels": labels,
        "blocked_by": _parse_blocked_by(body),
        "parent_issue": _parse_parent_issue(body),
        "issue_updated_at": payload.get("updatedAt", ""),
    }


def _fetch_pr_status(repo: str, pr_number: int) -> dict:
    """Return the ``gh pr view --json ...`` payload or ``{}``.

    Pulls the same fields the daemon's ``_fetch_pr_status`` asks for.
    """
    if not pr_number:
        return {}
    cmd = [
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "statusCheckRollup,mergeable,mergeStateStatus,headRefOid,mergeCommit",
    ]
    try:
        outcome = _run(cmd, timeout=30)
    except Exception:
        return {}
    if outcome.returncode != 0:
        return {}
    try:
        payload = json.loads(outcome.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _fetch_pr_diff(repo: str, pr_number: int) -> str:
    """Return the PR's base-to-head diff, empty string on error."""
    if not pr_number:
        return ""
    cmd = ["gh", "pr", "diff", str(pr_number), "--repo", repo]
    try:
        outcome = _run(cmd, timeout=60)
    except Exception:
        return ""
    if outcome.returncode != 0:
        return ""
    return outcome.stdout or ""


def _extract_failing_jobs(pr_status: dict) -> list[dict]:
    """Delegate to phase_transitions.extract_failing_jobs (#4417).

    Returns up to ``FIX_CI_MAX_FAILING_JOBS`` entries. The canonical
    failure-conclusion vocabulary lives in
    ``phase_transitions._CI_FAILURE_CONCLUSIONS`` so the daemon
    (subprocess path) and the Fargate agent-runner (this shim)
    classify identically. ``CANCELLED`` is intentionally excluded
    (#4414).
    """
    # Late import — keeps the shim importable in environments where
    # ``scripts/dispatcher/`` is not on sys.path (the sys.path push
    # at module top runs unconditionally, but defensive import here
    # makes test fixtures simpler too).
    from phase_transitions import extract_failing_jobs

    return extract_failing_jobs(pr_status, max_jobs=FIX_CI_MAX_FAILING_JOBS)


def _fetch_job_log_tail(repo: str, job_database_id) -> str:
    """Fetch ``gh run view --log-failed --job <id>`` output.

    Uses ``--job`` with the job's ``databaseId`` — the daemon comment
    references this shape. Caps at ``FIX_CI_LOG_TAIL_BYTES`` so a
    multi-megabyte build log doesn't bloat the skill input. Empty
    string on any failure.
    """
    if not job_database_id:
        return ""
    cmd = [
        "gh",
        "run",
        "view",
        "--repo",
        repo,
        "--log-failed",
        "--job",
        str(job_database_id),
    ]
    try:
        outcome = _run(cmd, timeout=60)
    except Exception:
        return ""
    if outcome.returncode != 0:
        return ""
    out = outcome.stdout or ""
    if len(out) > FIX_CI_LOG_TAIL_BYTES:
        # Keep the tail — failures usually surface near the end of the
        # log, and the skill explicitly expects "last ~200 lines".
        out = out[-FIX_CI_LOG_TAIL_BYTES:]
    return out


def _enrich_failing_jobs_with_logs(repo: str, jobs: list[dict]) -> list[dict]:
    """Attach a ``log_tail`` to each failing-job entry."""
    enriched: list[dict] = []
    for job in jobs:
        tail = _fetch_job_log_tail(repo, job.get("databaseId"))
        enriched.append({**job, "log_tail": tail})
    return enriched


def _fetch_merged_pr_info(repo: str, pr_number: int) -> dict:
    """Return ``{merge_commit_sha, pr_state}`` for a PR."""
    if not pr_number:
        return {"merge_commit_sha": "", "pr_state": ""}
    cmd = [
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "state,mergeCommit,headRefOid",
    ]
    try:
        outcome = _run(cmd, timeout=30)
    except Exception:
        return {"merge_commit_sha": "", "pr_state": ""}
    if outcome.returncode != 0:
        return {"merge_commit_sha": "", "pr_state": ""}
    try:
        payload = json.loads(outcome.stdout or "{}")
    except json.JSONDecodeError:
        return {"merge_commit_sha": "", "pr_state": ""}
    sha = ""
    merge_commit = payload.get("mergeCommit")
    if isinstance(merge_commit, dict):
        sha = str(merge_commit.get("oid") or "")
    if not sha:
        head = payload.get("headRefOid")
        if isinstance(head, str):
            sha = head
    return {
        "merge_commit_sha": sha,
        "pr_state": str(payload.get("state") or ""),
    }


def _fetch_deploy_runs_for_sha(repo: str, sha: str) -> list[dict]:
    """Return workflow runs with ``headSha`` equal to ``sha``.

    Matches the daemon's post-merge deploy-run filter: only runs on
    the merge commit count toward ``deploy_status``. Empty list on
    error or no runs.
    """
    if not sha:
        return []
    cmd = [
        "gh",
        "run",
        "list",
        "--repo",
        repo,
        "--commit",
        sha,
        "--limit",
        "20",
        "--json",
        "databaseId,workflowName,conclusion,status,headSha,createdAt,updatedAt",
    ]
    try:
        outcome = _run(cmd, timeout=30)
    except Exception:
        return []
    if outcome.returncode != 0:
        return []
    try:
        payload = json.loads(outcome.stdout or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    # Keep only entries whose name starts with ``Deploy`` or is
    # ``Terraform`` — matches the daemon's workflow-name-to-type map.
    filtered: list[dict] = []
    for run in payload:
        if not isinstance(run, dict):
            continue
        name = str(run.get("workflowName") or "")
        if name.startswith("Deploy") or name == "Terraform":
            filtered.append(run)
    return filtered


def _select_deploy_status(deploy_runs: list[dict]):
    """Mirror DispatcherDaemon._select_deploy_status."""
    if not deploy_runs:
        return None
    first = deploy_runs[0]
    return {
        "workflow_name": first.get("workflowName"),
        "run_id": first.get("databaseId"),
        "conclusion": first.get("conclusion"),
        "duration_s": None,
    }


def _infer_change_type(deploy_runs: list[dict]) -> str:
    """Mirror DispatcherDaemon._infer_change_type."""
    if not deploy_runs:
        return "no_deployed_component"
    name_to_type = {
        "Deploy API": "api",
        "Deploy Dispatcher": "dx_tooling",
        "Deploy Scraper": "scraper",
        "Deploy Production": "web",
        "Deploy Production (Web)": "web",
        "Terraform": "dx_tooling",
    }
    for run in deploy_runs:
        mapped = name_to_type.get(str(run.get("workflowName") or ""))
        if mapped:
            return mapped
    return "dx_tooling"


def _touched_services_from_runs(deploy_runs: list[dict]) -> list[str]:
    """Mirror DispatcherDaemon._touched_services_from_runs."""
    name_to_service = {
        "Deploy API": "judgemind-api-dev",
        "Deploy Dispatcher": "judgemind-dispatcher-dev",
        "Deploy Scraper": "judgemind-scraper-dev",
    }
    services: list[str] = []
    for run in deploy_runs:
        svc = name_to_service.get(str(run.get("workflowName") or ""))
        if svc and svc not in services:
            services.append(svc)
    return services


# ─────────────────────────────────────────────────────────────────────
# Local state helpers (worktree filesystem + git)
# ─────────────────────────────────────────────────────────────────────


def _read_prior_output(repo_root: Path, phase: str) -> dict:
    """Read ``{repo_root}/tmp/dispatcher-output/<phase>.json`` if present."""
    path = repo_root / "tmp" / "dispatcher-output" / f"{phase}.json"
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _git_diff(repo_root: Path) -> str:
    """``git diff origin/main...HEAD`` from the repo root. Empty on failure."""
    try:
        out = _run(
            ["git", "-C", str(repo_root), "diff", "origin/main...HEAD"],
            timeout=60,
        )
    except Exception:
        return ""
    return out.stdout if out.returncode == 0 else ""


def _git_changed_files(repo_root: Path) -> list[str]:
    """``git diff --name-only origin/main...HEAD``. Empty on failure."""
    try:
        out = _run(
            [
                "git",
                "-C",
                str(repo_root),
                "diff",
                "--name-only",
                "origin/main...HEAD",
            ],
            timeout=30,
        )
    except Exception:
        return []
    if out.returncode != 0:
        return []
    return [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip()]


def _git_current_branch(repo_root: Path) -> str:
    """Current branch name via ``git rev-parse --abbrev-ref HEAD``."""
    try:
        out = _run(
            ["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"],
            timeout=10,
        )
    except Exception:
        return ""
    return (out.stdout or "").strip() if out.returncode == 0 else ""


def _git_diff_stats(repo_root: Path) -> dict:
    """Return ``{files_changed, insertions, deletions}`` from shortstat.

    Uses ``git diff --shortstat origin/main...HEAD``. Empty on failure
    with zeros so the retro skill's required ``diff_stats`` field is
    always a dict of ints (not None).
    """
    zero = {"files_changed": 0, "insertions": 0, "deletions": 0}
    try:
        out = _run(
            [
                "git",
                "-C",
                str(repo_root),
                "diff",
                "--shortstat",
                "origin/main...HEAD",
            ],
            timeout=30,
        )
    except Exception:
        return zero
    if out.returncode != 0:
        return zero
    line = (out.stdout or "").strip()
    # Example: " 3 files changed, 42 insertions(+), 5 deletions(-)"
    files = 0
    ins = 0
    dels = 0
    m = re.search(r"(\d+)\s+files?\s+changed", line)
    if m:
        files = int(m.group(1))
    m = re.search(r"(\d+)\s+insertions?\(\+\)", line)
    if m:
        ins = int(m.group(1))
    m = re.search(r"(\d+)\s+deletions?\(-\)", line)
    if m:
        dels = int(m.group(1))
    return {"files_changed": files, "insertions": ins, "deletions": dels}


# ─────────────────────────────────────────────────────────────────────
# Per-phase builders
# ─────────────────────────────────────────────────────────────────────


def _normalize_deferred_acs(raw) -> list[dict]:
    """Mirror the daemon's deferred_acs coercion in _run_verify_and_complete."""
    result: list[dict] = []
    if not isinstance(raw, list):
        return result
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("index")
        try:
            idx_int = int(idx) if idx is not None else None
        except (TypeError, ValueError):
            idx_int = None
        result.append(
            {
                "index": idx_int,
                "reason": str(entry.get("reason") or ""),
                "verify_instruction": str(entry.get("verify_instruction") or ""),
            }
        )
    return result


def _build_summary_input(
    agent_id: str,
    issue_number: int,
    repo_root: Path,
    github_repo: str,
) -> dict:
    """Match DispatcherDaemon._run_summary_phase's summary_input shape."""
    bundle = _fetch_issue_bundle(github_repo, issue_number)
    plan = _read_prior_output(repo_root, "plan")
    ralph = _read_prior_output(repo_root, "ralph")
    # The daemon prefers ralph's own ``changed_files`` list if populated;
    # falls back to a git diff read against HEAD. We match, except we
    # fall back to ``origin/main...HEAD`` instead of ``HEAD`` because
    # the entrypoint's git state includes the ralph-committed work on
    # the branch rather than a dirty worktree (#2971).
    changed_files = ralph.get("changed_files") or _git_changed_files(repo_root)
    return {
        "agent_id": agent_id,
        "issue_number": issue_number,
        "issue_title": bundle.get("issue_title", ""),
        "issue_body": bundle.get("issue_body", ""),
        "issue_comments": bundle.get("issue_comments", []),
        "ralph_summary": ralph.get("summary", ""),
        "changed_files": changed_files,
        "git_diff": _git_diff(repo_root),
        "worktree_path": str(repo_root),
        "repo_root": str(repo_root),
        "branch": _git_current_branch(repo_root),
        "plan_acceptance_criteria": plan.get("acceptance_criteria", []) or [],
        "scope_check": plan.get("scope_check", []) or [],
    }


def _build_fix_ci_input(
    agent_id: str,
    issue_number: int,
    repo_root: Path,
    github_repo: str,
) -> dict:
    """Match DispatcherDaemon._run_fix_ci's fix_ci_input shape."""
    pr_number = _db_fetch_agent_pr_number(agent_id)
    retries_used = _db_fetch_agent_retries_used(agent_id)
    pr_status = _fetch_pr_status(github_repo, pr_number)
    failing_jobs_raw = _extract_failing_jobs(pr_status)
    failing_jobs = _enrich_failing_jobs_with_logs(github_repo, failing_jobs_raw)
    # Prefer the PR's full base-to-head diff so fix-ci sees exactly
    # what shipped (matches daemon behaviour). Fall back to local
    # ``git diff`` if gh is unreachable.
    git_diff = _fetch_pr_diff(github_repo, pr_number) or _git_diff(repo_root)
    plan = _read_prior_output(repo_root, "plan")
    branch = _git_current_branch(repo_root)
    return {
        "agent_id": agent_id,
        "issue_number": issue_number,
        "pr_number": pr_number,
        "branch": branch,
        "failing_jobs": failing_jobs,
        "git_diff_base_to_head": git_diff,
        "worktree_path": str(repo_root),
        "repo_root": str(repo_root),
        "previous_fix_attempts": retries_used,
        "change_type": plan.get("change_type", "") if isinstance(plan, dict) else "",
    }


def _build_verify_input(
    agent_id: str,
    issue_number: int,
    repo_root: Path,
    github_repo: str,
) -> dict:
    """Match DispatcherDaemon._run_verify_and_complete's verify_input shape."""
    pr_number = _db_fetch_agent_pr_number(agent_id)
    merge_info = _fetch_merged_pr_info(github_repo, pr_number)
    merge_sha = merge_info.get("merge_commit_sha", "")
    deploy_runs = _fetch_deploy_runs_for_sha(github_repo, merge_sha)
    deploy_status = _select_deploy_status(deploy_runs)
    change_type = _infer_change_type(deploy_runs)
    touched_services = _touched_services_from_runs(deploy_runs)
    bundle = _fetch_issue_bundle(github_repo, issue_number)
    # Prefer plan-output's AC list (authoritative at claim time); fall
    # back to extracting from the issue body via the same regex the
    # daemon uses. If both are empty the verify skill tolerates that
    # with a FAILED verdict.
    plan = _read_prior_output(repo_root, "plan")
    acceptance_criteria = plan.get("acceptance_criteria") or []
    if not acceptance_criteria:
        acceptance_criteria = _extract_acceptance_criteria(bundle.get("issue_body") or "")
    # deferred_acs lives in summary's persisted output — the daemon
    # reads it from dispatcher.phase_outputs WHERE phase='summary'. We
    # do the same via _db_fetch_phase_output, then normalize.
    summary_persisted = _db_fetch_phase_output(agent_id, "summary")
    deferred_acs = _normalize_deferred_acs(summary_persisted.get("deferred_acs"))
    return {
        "agent_id": agent_id,
        "issue_number": issue_number,
        "pr_number": pr_number,
        "acceptance_criteria": acceptance_criteria,
        "change_type": change_type,
        "touched_services": touched_services,
        "deploy_status": deploy_status,
        "merged_commit_sha": merge_sha,
        "worktree_path": str(repo_root),
        "repo_root": str(repo_root),
        "plan_text": plan.get("plan_text", "") if isinstance(plan, dict) else "",
        "scope_check": plan.get("scope_check", []) or [],
        "deferred_acs": deferred_acs,
    }


def _build_retro_input(
    agent_id: str,
    issue_number: int,
    repo_root: Path,
) -> dict:
    """Match DispatcherDaemon._build_retro_input's payload shape."""
    pr_number = _db_fetch_agent_pr_number(agent_id)
    phase_transitions = _db_fetch_phase_transitions(agent_id)
    failures = _db_fetch_failures_grouped(agent_id)
    # Counters derived from phase_transitions, with floors of 1 for
    # ralph_iterations + ci_attempts matching the daemon.
    ralph_iterations = sum(
        1 for p in phase_transitions if p.get("phase") == "ralph"
    )
    ci_attempts = sum(
        1 for p in phase_transitions if p.get("phase") == "awaiting_ci"
    )
    fix_ci_attempts = sum(
        1 for p in phase_transitions if p.get("phase") == "fix_ci"
    )
    if ralph_iterations < 1:
        ralph_iterations = 1
    if ci_attempts < 1:
        ci_attempts = 1
    total_duration_s = _db_fetch_agent_total_duration_s(agent_id)
    plan = _read_prior_output(repo_root, "plan")
    scope_check_followups: list[str] = []
    plan_follow_ups: list[str] = []
    if isinstance(plan, dict):
        scope_raw = plan.get("scope_check_followups") or []
        if isinstance(scope_raw, list):
            scope_check_followups = [str(x) for x in scope_raw if x]
        follow_raw = (
            plan.get("follow_ups")
            or plan.get("plan_follow_ups")
            or []
        )
        if isinstance(follow_raw, list):
            plan_follow_ups = [str(x) for x in follow_raw if x]
    verify_output = _db_fetch_phase_output(agent_id, "verify")
    verify_evidence_md = str(verify_output.get("evidence_md") or "")
    diff_stats = _git_diff_stats(repo_root)
    return {
        "agent_id": agent_id,
        "issue_number": issue_number,
        "pr_number": pr_number,
        "phase_transitions": phase_transitions,
        "failures": failures,
        "ralph_iterations": ralph_iterations,
        "ci_attempts": ci_attempts,
        "fix_ci_attempts": fix_ci_attempts,
        "total_duration_s": total_duration_s,
        "diff_stats": diff_stats,
        "worktree_path": str(repo_root),
        "repo_root": str(repo_root),
        "scope_check_followups": scope_check_followups,
        "plan_follow_ups": plan_follow_ups,
        "verify_evidence_md": verify_evidence_md,
    }


def _build_fix_conflict_input(
    agent_id: str,
    issue_number: int,
    repo_root: Path,
    github_repo: str,
) -> dict:
    """Build the /task-v2-fix-conflict input bundle (#3225).

    Reads artifacts the entrypoint stashed when the rebase conflict
    was detected (``{AGENT_WORKSPACE}/fix-conflict/`` — see
    ``handle_push_and_pr`` and the start-of-ralph baseline rebase
    branch). Provides:

    * ``issue_body`` — original task for context.
    * ``original_patch`` — agent's pre-rebase diff (from the stashed
      ``original-patch.diff``, or ralph_patches DB row as fallback).
    * ``conflict_files`` — list of ``{path, conflict_markers_text}``.
    * ``main_commits_since_base`` — ``git log``-derived commits on
      origin/main since the stashed merge-base.
    * ``main_files_content`` — each conflict file's current
      ``origin/main`` content.

    Every fetch is best-effort: a missing artifact returns an empty
    string / empty list so the skill's own guard clauses produce a
    structured ``unresolvable`` verdict rather than crashing.
    """
    import os as _os
    import subprocess as _subprocess

    bundle = _fetch_issue_bundle(github_repo, issue_number)
    issue_body = bundle.get("issue_body", "")

    workspace = Path(_os.environ.get("AGENT_WORKSPACE", "/tmp"))
    stage = workspace / "fix-conflict"

    # Conflict files list.
    conflict_files_txt = stage / "conflict-files.txt"
    conflict_paths: list[str] = []
    if conflict_files_txt.is_file():
        try:
            conflict_paths = [
                ln.strip()
                for ln in conflict_files_txt.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                if ln.strip()
            ]
        except Exception:
            conflict_paths = []

    # Conflict markers per file — saved with slashes → "__".
    markers_dir = stage / "conflict-markers"
    conflict_files: list[dict] = []
    for path in conflict_paths:
        safe = path.replace("/", "__")
        marker_file = markers_dir / safe
        marker_text = ""
        if marker_file.is_file():
            try:
                marker_text = marker_file.read_text(
                    encoding="utf-8", errors="replace"
                )
            except Exception:
                marker_text = ""
        conflict_files.append(
            {"path": path, "conflict_markers_text": marker_text}
        )

    # Original patch.
    original_patch_file = stage / "original-patch.diff"
    original_patch = ""
    if original_patch_file.is_file():
        try:
            original_patch = original_patch_file.read_text(
                encoding="utf-8", errors="replace"
            )
        except Exception:
            original_patch = ""
    # Fallback: latest SHIP ralph_patches row. Single-line SELECT.
    if not original_patch:
        ralph_row = _db_query_one(
            "SELECT patch_content FROM dispatcher.ralph_patches "
            f"WHERE agent_id = '{agent_id}' "
            "ORDER BY iteration_n DESC LIMIT 1"
        )
        # db_query_one collapses newlines to '\t' — that's fine for
        # a patch used as reference material (not re-applied). A
        # follow-up could use a dedicated multiline fetch, but the
        # DB copy is already redundant with the git diff above.
        original_patch = ralph_row or ""

    # main_commits_since_base — ``git log`` between stashed
    # merge-base and origin/main, newest-first. Best-effort: if the
    # merge-base file is missing, we scan the last 10 commits on
    # origin/main as a fallback.
    merge_base_file = stage / "merge-base.txt"
    merge_base_sha = ""
    if merge_base_file.is_file():
        try:
            merge_base_sha = merge_base_file.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except Exception:
            merge_base_sha = ""
    main_commits: list[dict] = []
    log_range = (
        f"{merge_base_sha}..origin/main" if merge_base_sha else "origin/main"
    )
    try:
        proc = _subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "log",
                "--max-count=20",
                "--pretty=format:%H%x1f%an%x1f%s%x1f%b%x1e",
                log_range,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode == 0:
            records = [
                r.strip()
                for r in (proc.stdout or "").split("\x1e")
                if r.strip()
            ]
            for rec in records:
                parts = rec.split("\x1f")
                if len(parts) < 4:
                    continue
                sha, author, subject, body = parts[0], parts[1], parts[2], parts[3]
                # Per-commit stat — best-effort, skip on failure.
                stat = ""
                try:
                    stat_proc = _subprocess.run(
                        [
                            "git",
                            "-C",
                            str(repo_root),
                            "show",
                            "--stat",
                            "--format=",
                            sha,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                    if stat_proc.returncode == 0:
                        stat = (stat_proc.stdout or "").strip()
                except Exception:
                    stat = ""
                main_commits.append(
                    {
                        "sha": sha,
                        "author": author,
                        "subject": subject,
                        "body": body,
                        "stat": stat,
                    }
                )
    except Exception:
        pass

    # main_files_content — current origin/main content of each
    # conflict file. ``git show origin/main:<path>`` is the
    # authoritative reference.
    main_files_content: list[dict] = []
    for path in conflict_paths:
        content = ""
        try:
            show_proc = _subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "show",
                    f"origin/main:{path}",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if show_proc.returncode == 0:
                content = show_proc.stdout or ""
        except Exception:
            content = ""
        main_files_content.append({"path": path, "content": content})

    # attempt_number = current merge_conflict_attempts (pre-
    # increment). The entrypoint increments AFTER the budget gate,
    # so when the skill reads the input the counter already reflects
    # this attempt.
    attempts_raw = _db_query_one(
        "SELECT COALESCE(merge_conflict_attempts, 0) FROM dispatcher.agents "
        f"WHERE agent_id = '{agent_id}' LIMIT 1"
    )
    try:
        attempt_number = int(attempts_raw) if attempts_raw else 1
    except (TypeError, ValueError):
        attempt_number = 1
    if attempt_number < 1:
        attempt_number = 1

    return {
        "agent_id": agent_id,
        "issue_number": issue_number,
        "issue_body": issue_body,
        "original_patch": original_patch,
        "conflict_files": conflict_files,
        "main_commits_since_base": main_commits,
        "main_files_content": main_files_content,
        "worktree_path": str(repo_root),
        "repo_root": str(repo_root),
        "attempt_number": attempt_number,
    }


def _build_input(
    phase: str,
    agent_id: str,
    issue_number: int,
    repo_root: Path,
    github_repo: str,
) -> dict:
    """Dispatch to the per-phase builder."""
    base = {
        "agent_id": agent_id,
        "issue_number": issue_number,
        "worktree_path": str(repo_root),
        "repo_root": str(repo_root),
    }
    if phase == "plan":
        bundle = _fetch_issue_bundle(github_repo, issue_number)
        return {**base, **bundle}
    if phase == "ralph":
        plan = _read_prior_output(repo_root, "plan")
        return {
            **base,
            "plan": plan,
            "max_iterations": 5,
            "dependencies_installed": plan.get("dependencies_to_install", []) or [],
        }
    if phase == "summary":
        return _build_summary_input(agent_id, issue_number, repo_root, github_repo)
    if phase == "fix-ci":
        return _build_fix_ci_input(agent_id, issue_number, repo_root, github_repo)
    if phase == "fix-conflict":
        return _build_fix_conflict_input(
            agent_id, issue_number, repo_root, github_repo
        )
    if phase == "verify":
        return _build_verify_input(agent_id, issue_number, repo_root, github_repo)
    if phase == "retro":
        return _build_retro_input(agent_id, issue_number, repo_root)
    # Unknown phase — return the base shape; skill will BLOCK on
    # missing required fields, which is the correct behaviour.
    return base


def main() -> int:
    if len(sys.argv) != 5:
        print(
            "usage: phase_input_shim.py <phase> <agent_id> <issue_number> "
            "<repo_root>",
            file=sys.stderr,
        )
        return 2
    phase = sys.argv[1]
    agent_id = sys.argv[2]
    try:
        issue_number = int(sys.argv[3]) if sys.argv[3] else 0
    except ValueError:
        issue_number = 0
    repo_root = Path(sys.argv[4]).resolve()
    github_repo = os.environ.get("GITHUB_REPO", "judgemind/judgemind")

    try:
        payload = _build_input(phase, agent_id, issue_number, repo_root, github_repo)
    except Exception as exc:
        # Defensive belt: _build_input's per-helper try/except should
        # prevent propagation, but an unforeseen exception (new code
        # path, import error) must NOT silently swallow here — it must
        # exit 2 so write_phase_input() logs phase_input_write_failed
        # and the caller (run_claude_phase) can emit a diagnostic event
        # instead of letting claude hit the skill's input-missing guard.
        print(
            f"phase_input_shim: unhandled exception building {phase} input: {exc}",
            file=sys.stderr,
        )
        return 2

    input_dir = repo_root / "tmp" / "dispatcher-input"
    try:
        input_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        print(
            f"phase_input_shim: failed to create input dir {input_dir}: {exc}",
            file=sys.stderr,
        )
        return 2
    # Normalize skill-suffix naming for the on-disk file (daemon writes
    # `fix-ci.json` even though the phase-column value is `fix_ci`).
    file_phase = phase
    out_path = input_dir / f"{file_phase}.json"
    try:
        out_path.write_text(json.dumps(payload, indent=2, default=str))
    except Exception as exc:
        print(
            f"phase_input_shim: failed to write {out_path}: {exc}",
            file=sys.stderr,
        )
        return 2
    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
PYEOF
fi
}

# ── run_claude_phase helper (#3775) ----------------------------------------
#
# Defines write_phase_input, read_phase_output, phase_to_skill,
# run_claude_phase, claude_phase_timeout_seconds_by_phase, and the
# per-phase timeout constants. Extracted from the entrypoint so the
# functions can be sourced in tests without dragging in the entrypoint's
# clone/checkout/phase-loop side effects.

# shellcheck source=./agent_runner_run_claude_phase.sh
source "$(dirname "${BASH_SOURCE[0]}")/agent_runner_run_claude_phase.sh"

# #4138 — per-phase case-arm handler bodies extracted from
# ``phase_loop()`` for the same reasons as ``run_claude_phase`` above:
# keep the loop a thin dispatcher, let tests source the handlers
# directly, and mirror the precedent set by #3775. Sourced here so the
# inner side-effect handlers (``handle_push_and_pr``,
# ``handle_fix_conflict``, ``handle_fix_ci``, ``handle_awaiting_ci``,
# ``handle_merge``, ``handle_awaiting_deploy``) defined below are
# visible to the ``handle_*_arm`` callers when they execute. (Bash
# defers function-body resolution until invocation, so the load order
# matters for the *runtime* lookup, not the parse — but keeping the
# source line near the existing one makes the dependency obvious to
# readers.)
source "$(dirname "${BASH_SOURCE[0]}")/agent_runner_handlers.sh"


handle_scheduled_skill() {
    # Issue #3374. Dispatch a synthetic scheduled-skill phase (audit,
    # spotcheck, daily_report, etc.). The agent row's ``phase`` column
    # is the skill name (no ``/`` prefix); we invoke
    # ``claude -p /<phase> <agent_id>`` and parse the result.
    #
    # Synthetic skills:
    #   * Are NOT part of the standard plan→ralph→summary→PR pipeline.
    #   * Do NOT run push_and_pr — they file issues / open auto-PRs
    #     internally as side-effects of the skill itself.
    #   * Treat ``SHIPPED`` / ``PASSED`` / ``OK`` (and the absence of
    #     a verdict in claude's output for skills that don't return one)
    #     as success → ``status='succeeded'``, ``phase='done'``.
    #   * Treat any other verdict / non-zero exit as failure →
    #     ``status='failed'``, ``phase='scheduled_skill_failed'``.
    #
    # The handler updates the agent row directly and returns; the main
    # phase loop sees the terminal phase on the next read and exits.
    _skill_name="$1"
    _out_file="$AGENT_WORKSPACE/claude-p-scheduled-$_skill_name.stdout.json"
    _err_file="$AGENT_WORKSPACE/claude-p-scheduled-$_skill_name.stderr.log"

    if [[ "$AGENT_RUNNER_DRY_RUN" == "1" ]]; then
        log "scheduled_skill_dry_run" "skill=$_skill_name"
        advance_phase "done" "succeeded"
        return 0
    fi

    log "scheduled_skill_begin" "skill=$_skill_name" "agent_id=$AGENT_ID"
    start_skill_phase_watcher "$_skill_name"
    set +e
    (
        cd "$REPO_ROOT" || exit 127
        claude -p "/$_skill_name $AGENT_ID" \
            --output-format json \
            --dangerously-skip-permissions \
            > "$_out_file" \
            2> "$_err_file"
    )
    _rc=$?
    set -e
    stop_skill_phase_watcher
    log "scheduled_skill_done" "skill=$_skill_name" "exit_code=$_rc"

    # Persist the result envelope as a phase_output row for operator
    # visibility — same way ``run_claude_phase``'s output gets
    # persisted by its caller via ``persist_phase_output``. We pass
    # the structured ``.result`` object when available, otherwise an
    # empty dict (matches ``run_claude_phase``'s fallback).
    _output="{}"
    if [[ -s "$_out_file" ]]; then
        if jq -e '.result | type == "object"' "$_out_file" >/dev/null 2>&1; then
            _output=$(jq -c '.result' "$_out_file" 2>/dev/null || printf '{}')
        else
            # Wrap the string ``.result`` in a tiny envelope so operators
            # can still see what claude returned without parsing the raw
            # claude-p stdout. The verdict gate below uses _result_str
            # directly.
            _output=$(jq -c '{result_text: (.result // "" | tostring)}' \
                "$_out_file" 2>/dev/null || printf '{}')
        fi
    fi
    persist_phase_output "$_skill_name" "$_output"

    # Verdict classification. We accept either the structured
    # ``.verdict`` field (modern skills) OR a substring match on the
    # ``.result`` string (legacy skills) — SHIPPED / PASSED / OK.
    _verdict=""
    if jq -e '.result | type == "object"' "$_out_file" >/dev/null 2>&1; then
        _verdict=$(jq -r '.result.verdict // ""' "$_out_file" 2>/dev/null \
            | tr '[:lower:]' '[:upper:]' || printf '')
    fi
    _result_str=""
    if [[ -s "$_out_file" ]]; then
        _result_str=$(jq -r '.result | tostring' "$_out_file" 2>/dev/null \
            | tr '[:lower:]' '[:upper:]' || printf '')
    fi

    _success=0
    if [[ "$_rc" -eq 0 ]]; then
        case "$_verdict" in
            SHIPPED|PASSED|OK|SUCCESS|DONE|"")
                # Empty verdict + zero exit code = success (skills like
                # /audit and /spotcheck don't return a structured verdict).
                _success=1
                ;;
        esac
        if [[ "$_success" -eq 0 ]]; then
            # Substring tolerance for legacy skills.
            case "$_result_str" in
                *SHIPPED*|*PASSED*|*"FILED ISSUES"*|*"NO ACTIONABLE FINDINGS"*)
                    _success=1
                    ;;
            esac
        fi
    fi

    if [[ "$_success" -eq 1 ]]; then
        log "scheduled_skill_succeeded" "skill=$_skill_name" "verdict=$_verdict"
        advance_phase "done" "succeeded"
    else
        log "scheduled_skill_failed" \
            "skill=$_skill_name" \
            "exit_code=$_rc" \
            "verdict=$_verdict"
        advance_phase "scheduled_skill_failed" "failed"
    fi
}

persist_phase_output() {
    # $1 = phase, $2 = output JSON, $3 = optional path to a log-text
    #     file (worker stdout/stderr tail merged) to persist into
    #     ``dispatcher.phase_outputs.log_text``. Empty / missing → log_text
    #     stays NULL (the legacy two-arg behaviour).
    # Stage 1b writes a minimal row shape matching the daemon's
    # phase_outputs schema: (agent_id, phase, output_json) — NOT a
    # `status` column, which does not exist on `dispatcher.phase_outputs`
    # (#3115). The daemon's subprocess-mode persist writes the same
    # three columns. The `attempt` / token + cost columns
    # stay NULL here; Stage 2 wiring populates them once the daemon-
    # side log-capture path is in place.
    #
    # #3694 — log_text capture for the ralph silent-exit case. The
    # entrypoint now passes the merged stdout/stderr tail of
    # ``claude-p-ralph.{stdout,stderr}.log`` into this column on every
    # ralph exit, so a future diagnoser can read the worker's last words
    # even when ``output_json={}`` (the silent ``ralph_not_ship`` shape
    # documented in the issue body). Other phases continue to call this
    # with two args and write log_text=NULL — Stage 2 will widen the
    # capture to all phases once we've validated the ralph variant.
    #
    # #3219 — ON CONFLICT overwrite. The unique index
    # ``idx_dispatcher_phase_outputs_agent_phase_attempt`` on
    # ``(agent_id, phase, attempt)`` (migration 30) rejects a second
    # INSERT on the same three-tuple. Re-entry to a phase is a legitimate
    # case today — e.g. CI goes red, fix_ci patches, the entrypoint
    # transitions back to ``awaiting_ci`` and calls persist_phase_output
    # a second time with the same attempt (the entrypoint does not
    # bump ``attempt``; retries_used / attempt increments are a daemon-
    # side concern driven by ``_process_retry_markers``). Without the
    # ON CONFLICT, the second INSERT raises a unique-constraint
    # violation, psql exits 1, ``db_exec``'s ``-v ON_ERROR_STOP=1`` +
    # ``set -euo pipefail`` kill the entrypoint, and the ECS task exits
    # 1 → daemon marks the agent ``agent_task_stopped_unexpectedly`` →
    # PR is stranded. The subprocess-mode daemon's
    # ``_persist_phase_output`` already uses the same ON CONFLICT
    # overwrite (see ``scripts/dispatcher/daemon.py`` ~line 5818), so
    # this brings the entrypoint to parity.
    #
    # We intentionally overwrite rather than bumping ``attempt`` —
    # the entrypoint has no access to the retry-counter state the
    # daemon tracks, and a simple overwrite keeps re-entry idempotent
    # without leaking any new semantics. The tradeoff is that fix_ci
    # → awaiting_ci re-entry loses the prior awaiting_ci payload
    # (typically a ``ci_red`` observation that prompted fix_ci); the
    # ralph_patches table + the daemon-side fix_ci phase_output row
    # preserve that history.
    #
    # Default-substitute to an empty-object string if $2 is absent.
    # We spell the default in two stages rather than
    # ``${2:-{}}`` because bash's ``${param:-word}`` parser treats a
    # literal ``}`` inside ``word`` as the end of the expansion — so
    # ``${2:-{}}`` expands to ``$2}`` (two closing braces) when $2 is
    # set, producing malformed JSONB and a postgres syntax error.
    _phase="$1"
    _output_json="$2"
    _log_text_path="${3:-}"
    if [[ -z "$_output_json" ]]; then
        _output_json="{}"
    fi
    # #3413 — pass JSON via tmpfile + psql variable substitution rather than
    # bash interpolation. The previous implementation built the SQL with
    # ``"... '$_escaped'::jsonb ..."`` and a ``sed "s/'/''/g"`` escape, and
    # was passed to ``db_exec`` which feeds ``psql -c "$1"``. Bash interpolated
    # ``$_escaped`` into the SQL string BEFORE psql ever saw it, so any of
    # ``$()`` / backticks / unescaped ``$VAR`` in the JSON were expanded by
    # bash and corrupted the final SQL — producing exit_code=126 silently
    # somewhere inside psql/its child shells (#3413, third occurrence:
    # 2026-04-26 agent dbaff683 on issue #3407, fix_conflict phase, between
    # ``fix_conflict_handler_done`` and the never-fired
    # ``fix_conflict_persist_done``).
    #
    # The fix is two layers:
    #   1. Write the JSON to a tmpfile so the JSON content NEVER lands in a
    #      bash command line.
    #   2. Use a quoted heredoc (``<<'EOF'``) so bash performs zero expansion
    #      on the SQL body. The required values reach psql through psql's
    #      own ``-v name=value`` variable substitution + ``\set output_json
    #      `cat :'output_path'``` (psql's backtick reads the file at psql
    #      time, into a psql variable), then ``:'output_json'`` quotes the
    #      content as a SQL literal that the ``::jsonb`` cast parses.
    # Schema parity enforced by scripts/tests/test_phase_outputs_insert_shape.py
    # Use a function-local variable name (``_persist_rc``) rather than the
    # natural ``_rc``. Bash variables are dynamically scoped — naming the
    # local ``_rc`` would clobber the caller's ``_rc`` (e.g. the
    # ``handle_scheduled_skill`` flow uses ``_rc`` to capture claude's
    # exit code immediately before calling ``persist_phase_output``, and
    # then routes on it later).
    #
    # #3429 — move mktemp + printf INSIDE the set +e envelope so that disk /
    # inode pressure cannot kill the entrypoint with a masked exit code.
    # Previously both calls ran under ``set -e`` (the set +e below came
    # AFTER them), so a failed mktemp or a failed write to the tmpfile
    # would abort the shell immediately without returning a diagnosable
    # error to the caller.  The pattern mirrors the documented dollar-paren
    # / set-e masking antipattern catalogued in #3416 / #3417: every
    # operation that can fail now runs inside the set +e window, its return
    # code is captured explicitly, and failures are routed via ``return 1``
    # so the caller sees a clean non-zero rc rather than a hard entrypoint
    # exit. Each early-return path cleans up ``_persist_tmpfile`` explicitly
    # (no ``trap RETURN`` — bash RETURN traps are global and would fire on
    # all subsequent function returns in the same shell, breaking callers).
    set +e
    _persist_tmpfile=$(mktemp)
    if [[ -z "$_persist_tmpfile" ]]; then
        log "phase_output_persist_failed" "reason=mktemp_failed phase=$_phase"
        set -e
        return 1
    fi
    printf '%s' "$_output_json" > "$_persist_tmpfile"
    _persist_rc=$?
    if [[ $_persist_rc -ne 0 ]]; then
        rm -f "$_persist_tmpfile" 2>/dev/null || true
        log "phase_output_persist_failed" "reason=tmpfile_write_failed phase=$_phase"
        set -e
        return 1
    fi
    # #3694 — when the caller provided a log_text path, INSERT/UPDATE
    # both ``output_json`` AND ``log_text``. Otherwise keep the legacy
    # two-column write so untouched phases produce identical SQL. We
    # branch in the SQL via psql ``\if`` rather than building two
    # separate heredocs so the bulk of the statement stays one block.
    _log_text_present=0
    if [[ -n "$_log_text_path" && -s "$_log_text_path" ]]; then
        _log_text_present=1
    fi
    log "db_exec_begin" "fn=persist_phase_output phase=$_phase"
    if [[ "$_log_text_present" -eq 1 ]]; then
        psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
            -v agent_id="$AGENT_ID" \
            -v phase="$_phase" \
            -v output_path="$_persist_tmpfile" \
            -v log_text_path="$_log_text_path" <<'EOF' >/dev/null
\set output_json `cat :'output_path'`
\set log_text `cat :'log_text_path'`
INSERT INTO dispatcher.phase_outputs (agent_id, phase, output_json, log_text)
VALUES (:'agent_id', :'phase', :'output_json'::jsonb, :'log_text')
ON CONFLICT (agent_id, phase, attempt) DO UPDATE
  SET output_json = EXCLUDED.output_json,
      log_text = EXCLUDED.log_text,
      ts = now();
INSERT INTO dispatcher.phase_transitions (agent_id, phase)
VALUES (:'agent_id', :'phase');
EOF
        _persist_rc=$?
    else
        psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
            -v agent_id="$AGENT_ID" \
            -v phase="$_phase" \
            -v output_path="$_persist_tmpfile" <<'EOF' >/dev/null
\set output_json `cat :'output_path'`
INSERT INTO dispatcher.phase_outputs (agent_id, phase, output_json)
VALUES (:'agent_id', :'phase', :'output_json'::jsonb)
ON CONFLICT (agent_id, phase, attempt) DO UPDATE
  SET output_json = EXCLUDED.output_json,
      ts = now();
INSERT INTO dispatcher.phase_transitions (agent_id, phase)
VALUES (:'agent_id', :'phase');
EOF
        _persist_rc=$?
    fi
    set -e
    rm -f "$_persist_tmpfile"
    log "db_exec_done" "fn=persist_phase_output phase=$_phase rc=$_persist_rc"
    if [[ $_persist_rc -ne 0 ]]; then
        log "phase_output_persist_failed" "phase=$_phase rc=$_persist_rc"
        return $_persist_rc
    fi
    log "phase_output_persisted" "phase=$_phase"
}

assert_phase_deadline_not_exceeded() {
    # #3683: belt-and-suspenders wall-clock guard for ``handle_push_and_pr``.
    # Called before each major step; if the elapsed time since _phase_start
    # (captured at function entry) exceeds PUSH_AND_PR_PHASE_DEADLINE_SECONDS,
    # emit the ``push_and_pr_phase_wall_clock_exceeded`` log event + a
    # structured envelope and return 1 so the caller can ``return 0`` from
    # handle_push_and_pr.
    #
    # Usage (inside handle_push_and_pr):
    #   assert_phase_deadline_not_exceeded || return 0
    #
    # Callers are expected to have already ``printf``d the envelope
    # via the subshell before this function returns; this helper only
    # emits the log event and signals the caller to bail out.
    _elapsed=$(( SECONDS - _phase_start ))
    if [[ "$_elapsed" -ge "$PUSH_AND_PR_PHASE_DEADLINE_SECONDS" ]]; then
        log "push_and_pr_phase_wall_clock_exceeded" \
            "elapsed_seconds=$_elapsed" \
            "deadline_seconds=$PUSH_AND_PR_PHASE_DEADLINE_SECONDS"
        printf '{"no_op": false, "phase_wall_clock_exceeded": true, "elapsed_seconds": %d}' \
            "$_elapsed"
        return 1
    fi
    return 0
}

# _post_rebase_no_diff_to_main — canonical post-rebase already-applied check.
#
# Returns 0 when ``git diff --quiet origin/main HEAD`` exits 0 (no semantic
# diff between origin/main and HEAD).  The caller SHOULD emit a
# ``{"no_op": true, ...}`` envelope and return — the agent's commits are
# already present in main and there is nothing to push or PR.
#
# Returns 1 when there IS a real diff — the caller continues its normal
# push/PR flow.
#
# This is the canonical post-rebase already-applied check for all rebase-end
# sites in this file (``handle_push_and_pr`` sites 1 and 2, and
# ``handle_ralph_baseline_rebase`` site 3, plus any future siblings).
# New rebase-end sites MUST use this helper rather than inline
# ``rev-list --count`` or bare ``git diff --quiet`` blocks.
#
# Why ``git diff --quiet`` and not ``rev-list --count origin/main..HEAD``?
# ``rev-list`` counts commit *objects*, not semantic diff.  After
# ``rebase --abort`` HEAD equals ORIG_HEAD, which still contains the agent's
# ralph and fix_conflict commits as distinct git objects — even when those
# commits became semantically redundant with main during the rebase (which
# is why rebase produced empty commits and exited 128).  ``rev-list --count``
# returns N > 0 in that case and the pre-#3675 check fell through to the
# terminal-fail envelope on what was actually a benign success.  See the
# history in #3614 / #3651 / #3662 / #3675 for the full progression.
_post_rebase_no_diff_to_main() {
    local _rc
    set +e
    git -C "$REPO_ROOT" diff --quiet origin/main HEAD
    _rc=$?
    set -e
    return "$_rc"
}

handle_push_and_pr() {
    # Mechanical implementation of the push_and_pr phase (#3117, #3176).
    #
    # This phase is NOT claude-driven — the daemon handles push + PR
    # creation inline today via ``_push_and_open_pr`` in daemon.py. This
    # handler mirrors that flow for ECS-mode agents:
    #
    #   1. #3039 no-op guardrail: if ``origin/main..HEAD`` is empty
    #      (ralph SHIP with clean tree), emit ``{"no_op": true}`` and
    #      let the transition shim flip the agent to ``succeeded``.
    #   2. #3176 pre-push rebase: ``git fetch origin main`` then
    #      ``git rebase origin/main`` so a stale branch tip doesn't
    #      produce a CONFLICTING PR the moment main advances. Rebase
    #      conflict → fail cleanly with ``push_failed: true`` so the
    #      transition falls through to the unrecognized branch.
    #   3. #3176 summary amend: read ``commit_message`` from
    #      ``tmp/dispatcher-output/summary.json`` and
    #      ``git commit --amend -F <file>`` to replace ralph's
    #      placeholder ``"WIP: ralph output"`` commit with the
    #      conventional-commits message produced by the summary skill.
    #      Missing / unreadable summary output → fall through with the
    #      existing ralph commit untouched (still produces a PR, just
    #      without the rich title/body — subprocess mode raises this
    #      as a PR_OUTPUT_MISSING failure; the entrypoint is intentionally
    #      more tolerant so a partial summary doesn't abandon the PR).
    #   4. Push the branch.
    #   5. #3176 summary title/body: if ``summary.json`` provided
    #      ``pr_title`` + ``pr_body_md``, pass them via
    #      ``gh pr create --title "$T" --body-file <path>``. Otherwise
    #      fall back to ``--fill`` (prior behaviour).
    #   6. #3176 record pr_number: parse the PR number out of
    #      ``gh pr create`` stdout and ``UPDATE dispatcher.agents
    #      SET pr_number = $N`` so the green-counting audit sees the
    #      PR linkage.
    #
    # Prints the phase-output JSON envelope on stdout so the caller
    # can persist it via persist_phase_output and drive the transition
    # shim. Output shape matches ``transition_from_push_and_pr``:
    #   {"no_op": true}   → terminal success (no commit to push)
    #   {"no_op": false}  → advance to awaiting_ci
    #
    # Note: if ``AGENT_RUNNER_DRY_RUN=1``, emit ``{"no_op": true}`` so
    # the loop reaches a terminal phase without actually shelling out.

    if [[ "$AGENT_RUNNER_DRY_RUN" == "1" ]]; then
        log "push_and_pr_dry_run"
        printf '{"no_op": true}'
        return 0
    fi

    # #3683: capture the function entry time so assert_phase_deadline_not_exceeded
    # can compute elapsed wall-clock seconds at each major step below.
    _phase_start=$SECONDS

    # Detect the #3039 no-op-SHIP guardrail: ralph's SHIP with a clean
    # working tree means ``origin/main..HEAD`` is empty — there's
    # nothing to push and no PR to open. Terminate as no_op so the
    # transition shim flips the agent to ``succeeded``.
    _ahead_count=$(git -C "$REPO_ROOT" rev-list --count origin/main..HEAD 2>/dev/null || printf '0')
    if [[ "$_ahead_count" == "0" ]]; then
        log "push_and_pr_no_op" "reason=clean_worktree_on_ship"
        printf '{"no_op": true}'
        return 0
    fi

    # ── #3176: read summary skill output from dispatcher-output/ ──────
    _summary_path="$REPO_ROOT/tmp/dispatcher-output/summary.json"
    _pr_title=""
    _pr_body_md=""
    _commit_message=""
    if [[ -s "$_summary_path" ]]; then
        _pr_title=$(jq -r '.pr_title // ""' "$_summary_path" 2>/dev/null || printf '')
        _pr_body_md=$(jq -r '.pr_body_md // ""' "$_summary_path" 2>/dev/null || printf '')
        _commit_message=$(jq -r '.commit_message // ""' "$_summary_path" 2>/dev/null || printf '')
        log "push_and_pr_summary_output_read" \
            "has_title=$([[ -n "$_pr_title" ]] && printf 'true' || printf 'false')" \
            "has_body=$([[ -n "$_pr_body_md" ]] && printf 'true' || printf 'false')" \
            "has_commit=$([[ -n "$_commit_message" ]] && printf 'true' || printf 'false')"
    else
        log "push_and_pr_summary_output_missing" "path=$_summary_path"
    fi

    # ── #3176: amend ralph's placeholder commit with summary's
    # conventional-commits message. Mirrors the daemon's
    # ``git commit --amend -F <file>`` — see daemon.py ~L10914.
    if [[ -n "$_commit_message" ]]; then
        # #3683: wall-clock guard — bail before touching git if we're
        # already over budget. Unlikely here (we're at the top of the
        # function) but protects against slow jq / DB calls above.
        assert_phase_deadline_not_exceeded || return 0
        _commit_msg_path="$AGENT_WORKSPACE/commit_msg.txt"
        printf '%s' "$_commit_message" > "$_commit_msg_path"
        log "push_and_pr_commit_amend_begin"
        set +e
        # #3683: wrap in LOCAL_GIT_TIMEOUT_SECONDS — a wedged git index
        # or stale lock file can make ``commit --amend`` block forever.
        timeout "$LOCAL_GIT_TIMEOUT_SECONDS" \
            git -C "$REPO_ROOT" commit --amend -F "$_commit_msg_path" \
            > "$AGENT_WORKSPACE/git-commit-amend.stdout.log" \
            2> "$AGENT_WORKSPACE/git-commit-amend.stderr.log"
        _amend_rc=$?
        set -e
        log "push_and_pr_commit_amend_done" "exit_code=$_amend_rc"
        if [[ "$_amend_rc" -eq 124 ]]; then
            # #3683: timeout fired — emit a distinct event and a
            # structured envelope so the diagnoser can differentiate
            # a hung amend from a non-zero exit. Non-fatal: log +
            # continue with ralph's original commit (mirrors the
            # existing non-zero branch below).
            log "push_and_pr_commit_amend_timeout" \
                "elapsed_seconds=$LOCAL_GIT_TIMEOUT_SECONDS"
        elif [[ "$_amend_rc" -ne 0 ]]; then
            # Non-fatal: log + continue with ralph's original commit.
            # A pre-push hook rejection is the common real-world
            # failure here; proceeding with the unamended commit is
            # a strictly better outcome than abandoning the PR.
            log "push_and_pr_commit_amend_failed" "exit_code=$_amend_rc"
        fi
    fi

    # ── #3176: pre-push rebase so we don't push a CONFLICTING branch.
    # Mirrors the daemon-side behaviour (``git fetch origin main`` +
    # ``git rebase origin/main``). On rebase conflict, abort and fail
    # cleanly — the transition shim routes to the unrecognized branch
    # and the agent terminates without opening an orphan PR.
    # #3683: wall-clock guard before the fetch+rebase block.
    assert_phase_deadline_not_exceeded || return 0
    log "push_and_pr_fetch_main_begin"
    set +e
    # #3656: bound network IO at NETWORK_TIMEOUT_SECONDS — a hung
    # ``git fetch`` would otherwise pin the cap slot indefinitely.
    # ``timeout`` exits 124 on SIGTERM-after-timeout; the existing
    # best-effort fetch-failure path (logged + fall through to push)
    # handles 124 cleanly without a separate envelope branch.
    timeout "$NETWORK_TIMEOUT_SECONDS" \
        git -C "$REPO_ROOT" fetch origin main \
        > "$AGENT_WORKSPACE/git-fetch-main.stdout.log" \
        2> "$AGENT_WORKSPACE/git-fetch-main.stderr.log"
    _fetch_rc=$?
    set -e
    log "push_and_pr_fetch_main_done" "exit_code=$_fetch_rc"
    if [[ "$_fetch_rc" -eq 124 ]]; then
        log "push_and_pr_fetch_main_timeout" \
            "elapsed_seconds=$NETWORK_TIMEOUT_SECONDS"
    fi
    if [[ "$_fetch_rc" -eq 0 ]]; then
        log "push_and_pr_rebase_begin"
        set +e
        # #3683: wrap in LOCAL_GIT_TIMEOUT_SECONDS — a wedged rebase
        # (e.g. stale .git/rebase-merge directory from a previous run)
        # can block forever without a timeout.
        timeout "$LOCAL_GIT_TIMEOUT_SECONDS" \
            git -C "$REPO_ROOT" rebase origin/main \
            > "$AGENT_WORKSPACE/git-rebase.stdout.log" \
            2> "$AGENT_WORKSPACE/git-rebase.stderr.log"
        _rebase_rc=$?
        set -e
        log "push_and_pr_rebase_done" "exit_code=$_rebase_rc"
        # #3614: post-rebase empty-diff guard — direct sibling of #3580's
        # fix in handle_fix_ci. When ``git rebase origin/main`` succeeds
        # but drops every commit (because the patches were already in
        # baseline — typically because a sibling PR landed the same fix
        # first, or the daemon is retrying an already-fixed issue), we
        # have nothing to push and no PR to open. Pre-#3614 the code fell
        # through to ``git push`` (which no-ops with "Everything up-to-
        # date") and then ``gh pr create`` (which fails for an empty
        # diff), and the agent reaped as
        # ``push_and_pr_no_unmerged_files/failed`` — tripping the
        # circuit breaker repeatedly across the cluster of issues
        # (#2777, #2832, #2854, #3297, #3407, #3574, #3581).
        #
        # The fix: if the rebase succeeded AND the ahead-count just
        # collapsed to 0, emit the existing ``{"no_op": true}`` envelope.
        # ``transition_from_push_and_pr`` already routes that to
        # PHASE_NO_OP terminal succeeded — exactly the right outcome
        # for "fix is already in main." New log event
        # ``push_and_pr_no_unmerged_files_already_applied`` distinguishes
        # this from the pre-existing ``push_and_pr_no_op`` (which only
        # fires when the working tree was clean at SHIP time, before
        # the rebase) and from the terminal failure event
        # ``push_and_pr_no_unmerged_files`` (which fires when the rebase
        # actually FAILED with no unmerged files — a different code
        # path, see #3465). Mirrors the rename in #3580 which split
        # ``fix_ci_patch_empty`` into the new
        # ``fix_ci_patch_empty_already_applied`` advance event.
        # #3683: emit a distinct event when the LOCAL_GIT_TIMEOUT_SECONDS
        # ceiling fires on ``git rebase origin/main`` so operators can
        # grep for ``push_and_pr_rebase_timeout`` vs. the generic
        # ``push_and_pr_rebase_conflict`` event. Fall through to the
        # existing non-zero branch which aborts + emits the structured
        # failure envelope — same outcome as a real conflict, just with
        # a clearer root-cause event in the log.
        if [[ "$_rebase_rc" -eq 124 ]]; then
            log "push_and_pr_rebase_timeout" \
                "elapsed_seconds=$LOCAL_GIT_TIMEOUT_SECONDS"
        fi
        if [[ "$_rebase_rc" -eq 0 ]]; then
            if _post_rebase_no_diff_to_main; then
                log "push_and_pr_no_unmerged_files_already_applied" \
                    "reason=rebase_dropped_all_commits_already_in_baseline"
                printf '{"no_op": true}'
                return 0
            fi
        fi
        if [[ "$_rebase_rc" -ne 0 ]]; then
            # #3225: Before aborting, capture the conflict state so the
            # fix_conflict phase can feed it to the claude skill without
            # needing to replay the rebase. Capture three things:
            #   1. Conflicted file paths (``git diff --name-only
            #      --diff-filter=U``) — one path per line.
            #   2. Per-file ``conflict_markers_text`` — the on-disk
            #      content WITH the ``<<<<<<<``/``=======``/``>>>>>>>``
            #      markers. After ``rebase --abort`` the markers are
            #      gone, so this capture must happen BEFORE the abort.
            # The files are staged under
            # ``{AGENT_WORKSPACE}/fix-conflict/`` for the input-shim
            # to consume when the fix_conflict phase starts.
            _fix_conflict_stage="$AGENT_WORKSPACE/fix-conflict"
            mkdir -p "$_fix_conflict_stage/conflict-markers"
            set +e
            git -C "$REPO_ROOT" diff --name-only --diff-filter=U \
                > "$_fix_conflict_stage/conflict-files.txt" \
                2> "$AGENT_WORKSPACE/git-diff-conflict-files.stderr.log"
            set -e
            while IFS= read -r _cfile; do
                if [[ -z "$_cfile" ]]; then
                    continue
                fi
                # Stash the path with slashes replaced so a flat dir
                # works without pre-creating the tree.
                _safe=$(printf '%s' "$_cfile" | tr '/' '__')
                if [[ -f "$REPO_ROOT/$_cfile" ]]; then
                    cp "$REPO_ROOT/$_cfile" \
                        "$_fix_conflict_stage/conflict-markers/$_safe" \
                        2>/dev/null || true
                fi
            done < "$_fix_conflict_stage/conflict-files.txt"
            # Also capture the pre-rebase patch (git diff
            # origin/main..HEAD AT THE ATTEMPTED REBASE-ROOT — i.e.
            # the original base before the rebase started). During a
            # rebase the index is in an in-progress state, so we can
            # read ORIG_HEAD to find what HEAD used to be.
            set +e
            git -C "$REPO_ROOT" diff "$(git -C "$REPO_ROOT" merge-base ORIG_HEAD origin/main 2>/dev/null)..ORIG_HEAD" \
                > "$_fix_conflict_stage/original-patch.diff" \
                2> "$AGENT_WORKSPACE/git-diff-original-patch.stderr.log" || true
            set -e
            # Capture the merge-base and HEAD SHAs for the input shim's
            # ``main_commits_since_base`` builder.
            set +e
            git -C "$REPO_ROOT" rev-parse ORIG_HEAD \
                > "$_fix_conflict_stage/orig-head.txt" 2>/dev/null || true
            git -C "$REPO_ROOT" merge-base ORIG_HEAD origin/main \
                > "$_fix_conflict_stage/merge-base.txt" 2>/dev/null || true
            set -e
            # Build a compact JSON list of conflict files for the
            # phase_output envelope — one array the transition shim
            # can pass straight into context["conflict_files"].
            _conflict_files_json="[]"
            if [[ -s "$_fix_conflict_stage/conflict-files.txt" ]]; then
                _conflict_files_json=$(jq -R -s -c \
                    'split("\n") | map(select(length > 0))' \
                    "$_fix_conflict_stage/conflict-files.txt" 2>/dev/null \
                    || printf '[]')
            fi
            # #3465: capture the last ~50 lines of git-rebase.stderr.log
            # (size-capped at ~5 KB) so the diagnoser can inspect the
            # rebase failure reason when no unmerged files are present.
            _rebase_stderr_tail=$(tail -n 50 \
                "$AGENT_WORKSPACE/git-rebase.stderr.log" 2>/dev/null \
                | head -c 5120 \
                | jq -Rs '.' 2>/dev/null \
                || printf '""')
            # Now abort the in-progress rebase so the worktree returns
            # to its pre-rebase state, log the failure, and emit the
            # structured envelope.
            set +e
            # #3683: wrap rebase --abort in LOCAL_GIT_TIMEOUT_SECONDS —
            # a stuck abort (e.g. index lock) would otherwise block here
            # indefinitely. A non-zero or 124 exit from --abort is
            # best-effort; we log it and proceed to emit the envelope
            # regardless so the agent always terminates cleanly.
            timeout "$LOCAL_GIT_TIMEOUT_SECONDS" \
                git -C "$REPO_ROOT" rebase --abort \
                > "$AGENT_WORKSPACE/git-rebase-abort.stdout.log" \
                2> "$AGENT_WORKSPACE/git-rebase-abort.stderr.log"
            _abort_rc=$?
            set -e
            if [[ "$_abort_rc" -eq 124 ]]; then
                log "push_and_pr_rebase_abort_timeout" \
                    "elapsed_seconds=$LOCAL_GIT_TIMEOUT_SECONDS"
            elif [[ "$_abort_rc" -ne 0 ]]; then
                log "push_and_pr_rebase_abort_failed" "exit_code=$_abort_rc"
            fi
            log "push_and_pr_rebase_conflict" "exit_code=$_rebase_rc" \
                "conflict_files_json=$_conflict_files_json"
            # #3465: if the conflict-files list is empty the entrypoint
            # emits a distinct no_unmerged_files envelope so the
            # transition shim routes to the diagnoser instead of
            # fix_conflict (which would immediately return unresolvable
            # on an empty bundle).
            if [[ "$_conflict_files_json" == "[]" ]]; then
                # #3662: post-abort ahead-count check — direct sibling of
                # #3651/PR #3657 (which fixed the same bug class for
                # ``handle_ralph_baseline_rebase``) and #3614/PR #3645
                # (which fixed the rebase-exit-0 empty-diff variant for
                # this same handle_push_and_pr handler). When the rebase
                # exits non-zero AND ``git diff --diff-filter=U`` is
                # empty AND the post-abort ahead-count collapses to 0,
                # the agent's commits are already in main (typically
                # because a sibling PR landed the same fix first, or
                # this is a post-fix_conflict re-rebase whose resolution
                # turned out to be redundant with parallel main). Pre-
                # #3662 this fell through to the no_unmerged_files
                # envelope, which transition_from_push_and_pr routed to
                # the diagnoser as ``push_and_pr_no_unmerged_files`` —
                # terminal-failing the agent on a benign success and
                # tripping the circuit breaker repeatedly across the
                # cluster of stuck issues (#3581 hit it 6× by 2026-04-27).
                #
                # The fix: if the conflict-files list is empty AND the
                # rebase --abort returned HEAD to a commit already in
                # origin/main (ahead-count 0), emit the existing
                # ``{"no_op": true}`` envelope.
                # ``transition_from_push_and_pr`` (#3645) already routes
                # ``no_op=true`` to PHASE_NO_OP terminal succeeded —
                # exactly the right outcome for "fix is already in
                # main." The new log event
                # ``push_and_pr_no_unmerged_files_already_applied_post_rebase_failure``
                # distinguishes this advance event from the pre-existing
                # ``push_and_pr_no_unmerged_files_already_applied`` (the
                # rebase-rc=0 empty-diff path from #3614, lines ~2453-
                # 2462) and from the terminal failure event
                # ``push_and_pr_no_unmerged_files`` (which still fires
                # when the rebase actually failed for a non-already-
                # applied reason — the existing #3465 path below).
                # #3675: ``rev-list --count`` counts commit *objects*,
                # not semantic diff. After ``rebase --abort`` HEAD
                # equals ORIG_HEAD which still contains the agent's
                # ralph + fix_conflict commits as distinct git objects
                # — even when those commits became *semantically
                # redundant* with main during the rebase (which is why
                # rebase produced empty commits and exited 128).
                # ``rev-list --count`` returns N > 0 in that case and
                # the pre-#3675 check fell through to the terminal-
                # fail envelope on benign success. The right semantic
                # question is "is there any diff between origin/main
                # and HEAD?" — answered by ``git diff --quiet``
                # (exit 0 = no diff). This catches both the rev-list-0
                # case (#3662 fixed) AND the rev-list-N-but-no-diff
                # case (this fix), without hiding real failures
                # (#3465 path) where the diff is genuinely non-empty.
                if _post_rebase_no_diff_to_main; then
                    log "push_and_pr_no_unmerged_files_already_applied_post_rebase_failure" \
                        "reason=rebase_failed_but_diff_to_main_is_empty"
                    printf '{"no_op": true, "rebase_dropped_all_commits": true, "diff_to_main_empty": true, "rebase_stderr_tail": %s}' \
                        "$_rebase_stderr_tail"
                    return 0
                fi
                # #3465 path: rebase actually failed for a non-already-
                # applied reason (corrupt state, fetch issue, etc.) —
                # route to the diagnoser as before.
                printf '{"no_op": false, "rebase_failed": true, "no_unmerged_files": true, "rebase_stderr_tail": %s}' \
                    "$_rebase_stderr_tail"
            else
                # #3225: emit conflict_files so transition_from_push_and_pr
                # routes to fix_conflict with the file list in context.
                printf '{"no_op": false, "rebase_failed": true, "conflict_files": %s, "rebase_stderr_tail": %s}' \
                    "$_conflict_files_json" "$_rebase_stderr_tail"
            fi
            return 0
        fi
    else
        # Fetch failure is best-effort — the push below will fail with
        # a network error we route through push_failed if it's a real
        # network outage. A transient local-only fetch failure still
        # lets the push succeed against whatever ``origin/main`` was
        # at clone time.
        log "push_and_pr_fetch_main_failed" "exit_code=$_fetch_rc"
    fi

    # #3683: wall-clock guard before push — e.g. a slow db_exec or jq
    # above could eat into the budget before we reach the network step.
    assert_phase_deadline_not_exceeded || return 0
    log "push_and_pr_push_begin" "branch=$BRANCH_NAME"
    set +e
    # #3656: bound ``git push`` network IO at NETWORK_TIMEOUT_SECONDS.
    # Pre-#3656 the bare push could hang indefinitely on kernel TCP
    # retry of an already-broken socket — observed for 16+ minutes on
    # agent 2ff6e282 (#3608) before manual ``aws ecs stop-task``. The
    # ``timeout`` wrapper guarantees ``handle_push_and_pr`` always
    # returns within NETWORK_TIMEOUT_SECONDS + a few seconds of
    # bookkeeping, freeing the cap slot for a fresh agent.
    # #3800: --no-verify skips the pre-push hook on this machine push.
    # ralph's worker already ran .githooks/pre-push end-to-end against the
    # WIP commit during iteration, so re-running it here adds no safety but
    # costs ~5+ minutes of wall-clock for scraper-framework-sized diffs —
    # well above the 300s NETWORK_TIMEOUT_SECONDS cap. Human-laptop pushes
    # are unaffected; only the agent-runner machine push gains the bypass.
    timeout "$NETWORK_TIMEOUT_SECONDS" \
        git -C "$REPO_ROOT" push -u origin "$BRANCH_NAME" --no-verify \
        > "$AGENT_WORKSPACE/git-push.stdout.log" \
        2> "$AGENT_WORKSPACE/git-push.stderr.log"
    _push_rc=$?
    set -e
    log "push_and_pr_push_done" "exit_code=$_push_rc"
    if [[ "$_push_rc" -eq 124 ]]; then
        # #3656: timeout fired — emit a distinct envelope so the
        # diagnoser can differentiate a hung push from a non-zero exit
        # (PAT scope, pre-push hook, etc.). The ``reason=push_timeout``
        # field flows through ``transition_from_push_and_pr`` into the
        # ``dispatcher.failures`` row so CloudWatch Logs Insights
        # queries can grep for ``push_timeout`` to count incidents.
        log "push_and_pr_push_timeout" \
            "elapsed_seconds=$NETWORK_TIMEOUT_SECONDS" \
            "branch=$BRANCH_NAME"
        printf '{"no_op": false, "push_failed": true, "reason": "push_timeout"}'
        return 0
    fi
    if [[ "$_push_rc" -ne 0 ]]; then
        log "push_and_pr_push_failed" "exit_code=$_push_rc"
        # Emit a minimal failure envelope; the transition shim will
        # route to the diagnoser via the unrecognized/non-SHIP path.
        printf '{"no_op": false, "push_failed": true}'
        return 0
    fi

    # ── #3411: ensure ``Closes #<ISSUE_NUMBER>`` (or equivalent) is
    # present in the PR body. /task-v2-summary doesn't always emit
    # the keyword, and without it GitHub won't auto-close the issue
    # on merge — leaving the daemon's ready queue stuck with done-
    # but-unclaimable issues. This is the entrypoint-side mirror of
    # daemon.py's ``_ensure_closes_keyword``. Idempotent: if the
    # keyword for $ISSUE_NUMBER is already present, the body is left
    # unchanged. Defense in depth across both subprocess + ECS modes.
    if [[ -n "$_pr_body_md" && -n "$ISSUE_NUMBER" ]]; then
        # Case-insensitive grep for any of: close|closes|closed,
        # fix|fixes|fixed, resolve|resolves|resolved followed by
        # whitespace then ``#<ISSUE_NUMBER>`` with a word boundary so
        # ``#3411`` doesn't match a request body containing ``#34110``.
        # Use ``-E`` for extended regex (portable across grep
        # implementations on Linux + macOS bash 3.2). Escape the
        # ``#`` even though it's not regex-special — defensive.
        _close_pattern="(close[sd]?|fix(es|ed)?|resolve[sd]?)[[:space:]]+#${ISSUE_NUMBER}([^0-9]|$)"
        if ! printf '%s' "$_pr_body_md" | grep -qiE "$_close_pattern"; then
            log "push_and_pr_closes_keyword_appended" "issue_number=$ISSUE_NUMBER"
            # Strip trailing whitespace/newlines so we don't end up
            # with an oversized blank-line tail, then append the
            # keyword as its own paragraph.
            _pr_body_md="$(printf '%s' "$_pr_body_md" | sed -e 's/[[:space:]]*$//')

Closes #${ISSUE_NUMBER}
"
        else
            log "push_and_pr_closes_keyword_present" "issue_number=$ISSUE_NUMBER"
        fi
    fi

    # ── #3176: open the PR with summary's pr_title + pr_body_md when
    # available. Fall back to ``--fill`` so a missing summary still
    # produces a PR (degraded but not abandoned).
    # #3683: wall-clock guard before PR create.
    assert_phase_deadline_not_exceeded || return 0
    log "push_and_pr_pr_create_begin"
    _pr_body_path="$AGENT_WORKSPACE/pr_body.md"
    set +e
    # #3656: bound ``gh pr create`` network IO at NETWORK_TIMEOUT_SECONDS
    # — same hung-network defense as the ``git push`` site above. A
    # GitHub API outage / token-rotation hang on ``gh pr create`` would
    # otherwise pin the cap slot indefinitely.
    if [[ -n "$_pr_title" && -n "$_pr_body_md" ]]; then
        printf '%s' "$_pr_body_md" > "$_pr_body_path"
        timeout "$NETWORK_TIMEOUT_SECONDS" \
            gh pr create \
            --repo judgemind/judgemind \
            --base main \
            --head "$BRANCH_NAME" \
            --title "$_pr_title" \
            --body-file "$_pr_body_path" \
            > "$AGENT_WORKSPACE/gh-pr-create.stdout.log" \
            2> "$AGENT_WORKSPACE/gh-pr-create.stderr.log"
        _pr_rc=$?
    else
        timeout "$NETWORK_TIMEOUT_SECONDS" \
            gh pr create \
            --repo judgemind/judgemind \
            --base main \
            --head "$BRANCH_NAME" \
            --fill \
            > "$AGENT_WORKSPACE/gh-pr-create.stdout.log" \
            2> "$AGENT_WORKSPACE/gh-pr-create.stderr.log"
        _pr_rc=$?
    fi
    set -e
    log "push_and_pr_pr_create_done" "exit_code=$_pr_rc"
    if [[ "$_pr_rc" -eq 124 ]]; then
        # #3656: timeout fired during ``gh pr create``. Emit a distinct
        # envelope (``reason=pr_create_timeout``) so the diagnoser can
        # differentiate a hung create from a non-zero exit.
        log "push_and_pr_pr_create_timeout" \
            "elapsed_seconds=$NETWORK_TIMEOUT_SECONDS"
        printf '{"no_op": false, "pr_create_failed": true, "reason": "pr_create_timeout"}'
        return 0
    fi
    if [[ "$_pr_rc" -ne 0 ]]; then
        log "push_and_pr_pr_create_failed" "exit_code=$_pr_rc"
        printf '{"no_op": false, "pr_create_failed": true}'
        return 0
    fi

    # ── #3176: parse PR number from ``gh pr create`` stdout and record
    # on the agent row. ``gh pr create`` prints the PR URL on the last
    # non-empty line: https://github.com/judgemind/judgemind/pull/<N>.
    _pr_url=$(tail -n 20 "$AGENT_WORKSPACE/gh-pr-create.stdout.log" 2>/dev/null \
        | grep -Eo 'https://github\.com/[^ ]+/pull/[0-9]+' \
        | tail -n 1 \
        || printf '')
    _pr_number=""
    if [[ -n "$_pr_url" ]]; then
        _pr_number=$(printf '%s' "$_pr_url" | sed -E 's|.*/pull/([0-9]+).*|\1|')
    fi
    if [[ -n "$_pr_number" && "$_pr_number" =~ ^[0-9]+$ ]]; then
        log "push_and_pr_pr_number_parsed" "pr_number=$_pr_number" "pr_url=$_pr_url"
        # Best-effort — a DB failure here shouldn't abandon an
        # already-opened PR. Mirrors daemon's pr_number write (daemon.py
        # ~L11316, ``_mark_agent_terminal(..., pr_number=pr_number)``).
        if ! db_exec "UPDATE dispatcher.agents
                         SET pr_number = $_pr_number
                       WHERE agent_id = '$AGENT_ID';" 2>/dev/null; then
            log "push_and_pr_pr_number_persist_failed" "pr_number=$_pr_number"
        else
            log "push_and_pr_pr_number_persisted" "pr_number=$_pr_number"
        fi
        printf '{"no_op": false, "pr_number": %s}' "$_pr_number"
        return 0
    fi

    log "push_and_pr_pr_number_parse_failed" "stdout_tail=$(tail -c 200 "$AGENT_WORKSPACE/gh-pr-create.stdout.log" 2>/dev/null | tr '\n' ' ')"
    printf '{"no_op": false}'
}

# ── fix_conflict helpers (#3225) ──────────────────────────────────────────
#
# The fix_conflict phase recovers from pre-push rebase conflicts
# (and start-of-ralph baseline rebase conflicts) by claude-resolving
# the conflict against updated origin/main content instead of
# abandoning the agent's ralph work.
#
# Budget bookkeeping lives in ``dispatcher.agents.merge_conflict_
# attempts`` (migration 44). Every invocation of
# ``handle_fix_conflict`` increments the counter BEFORE spawning the
# claude skill; when the current value is already >=
# FIX_CONFLICT_MAX_ATTEMPTS the handler emits a synthetic
# ``{"verdict": "unresolvable", "budget_exhausted": true}`` output
# without spending compute. The transition shim sees that verdict and
# advances to ``conflict_unresolvable`` (terminal).
#
# The applied-commit step (on verdict=resolved) writes each
# ``resolved_files[].content`` to its path under REPO_ROOT, stages
# everything, and creates ONE new commit with a conventional-commits
# message. A new commit (not a rebase) keeps the history
# straightforward for the retry — push_and_pr will re-fetch + rebase
# + push on re-entry.

read_merge_conflict_attempts() {
    # Column added by migration 44. Returns 0 if the column is
    # missing (older dev DB snapshots). Mirrors the
    # read_merge_unstick_attempts helper pattern.
    _val=$(db_query_one "SELECT COALESCE(merge_conflict_attempts, 0)
                            FROM dispatcher.agents
                           WHERE agent_id = '$AGENT_ID'
                           LIMIT 1;" 2>/dev/null || printf '')
    if [[ -z "$_val" ]] || ! [[ "$_val" =~ ^[0-9]+$ ]]; then
        printf '0'
    else
        printf '%s' "$_val"
    fi
}

increment_merge_conflict_attempts() {
    # Best-effort — a lost increment at most burns one extra fix-
    # conflict attempt before the budget gate trips. Mirrors the
    # increment_merge_unstick_attempts helper pattern.
    db_exec "UPDATE dispatcher.agents
                SET merge_conflict_attempts = COALESCE(merge_conflict_attempts, 0) + 1
              WHERE agent_id = '$AGENT_ID';" \
        >/dev/null 2>&1 || true
}

apply_resolved_files() {
    # $1 = path to fix-conflict.json (the skill's output).
    # Reads ``resolved_files[]`` from the JSON and writes each
    # ``{path, content}`` entry to its repo-relative location under
    # REPO_ROOT, then stages + commits via ``git``. Prints the created
    # commit SHA on success (or empty on failure) and returns 0 on
    # success, non-zero on any error.
    _out_json="$1"
    if [[ ! -s "$_out_json" ]]; then
        log "fix_conflict_apply_empty_output"
        return 1
    fi
    # Write each resolved file via a small helper (avoids inline
    # python -c). The helper returns the number of files written on
    # stdout.
    _apply_helper="$AGENT_WORKSPACE/fix-conflict-apply.py"
    cat > "$_apply_helper" <<'APPLYPY'
import json
import os
import sys
from pathlib import Path

if len(sys.argv) != 3:
    print("usage: apply.py <out-json> <repo-root>", file=sys.stderr)
    sys.exit(2)
out_path = Path(sys.argv[1])
repo_root = Path(sys.argv[2])
try:
    data = json.loads(out_path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"parse_error: {exc}", file=sys.stderr)
    sys.exit(3)
resolved = data.get("resolved_files") or []
if not isinstance(resolved, list) or not resolved:
    print("no_resolved_files", file=sys.stderr)
    sys.exit(4)
written = 0
for entry in resolved:
    if not isinstance(entry, dict):
        continue
    path = entry.get("path")
    content = entry.get("content")
    if not path or content is None:
        continue
    # Defensive: reject absolute or parent-traversing paths.
    if path.startswith("/") or ".." in Path(path).parts:
        print(f"reject_unsafe_path: {path}", file=sys.stderr)
        continue
    target = repo_root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    # content is a string; write as UTF-8, preserving whatever
    # trailing-newline convention the skill emitted.
    target.write_text(content, encoding="utf-8")
    written += 1
print(written)
APPLYPY
    set +e
    _written=$(python3 "$_apply_helper" "$_out_json" "$REPO_ROOT" \
        2> "$AGENT_WORKSPACE/fix-conflict-apply.stderr.log")
    _apply_rc=$?
    set -e
    log "fix_conflict_apply_done" "exit_code=$_apply_rc" "files_written=$_written"
    if [[ "$_apply_rc" -ne 0 ]]; then
        return 1
    fi
    # Stage every file listed in resolved_files (use the helper's
    # stderr-safe path listing instead of ``git add -A`` to avoid
    # accidentally staging test artifacts the skill left behind).
    _stage_helper="$AGENT_WORKSPACE/fix-conflict-stage-list.py"
    cat > "$_stage_helper" <<'STAGEPY'
import json
import sys
from pathlib import Path

out_path = Path(sys.argv[1])
try:
    data = json.loads(out_path.read_text(encoding="utf-8"))
except Exception:
    sys.exit(2)
for entry in (data.get("resolved_files") or []):
    if isinstance(entry, dict) and entry.get("path"):
        print(entry["path"])
STAGEPY
    set +e
    _paths_file="$AGENT_WORKSPACE/fix-conflict-stage-paths.txt"
    python3 "$_stage_helper" "$_out_json" > "$_paths_file" \
        2> "$AGENT_WORKSPACE/fix-conflict-stage-list.stderr.log"
    _list_rc=$?
    set -e
    if [[ "$_list_rc" -ne 0 || ! -s "$_paths_file" ]]; then
        log "fix_conflict_stage_list_failed" "exit_code=$_list_rc"
        return 1
    fi
    # git add <file> for each staged path.
    while IFS= read -r _sfile; do
        if [[ -z "$_sfile" ]]; then
            continue
        fi
        set +e
        git -C "$REPO_ROOT" add -- "$_sfile" \
            > /dev/null \
            2>> "$AGENT_WORKSPACE/fix-conflict-add.stderr.log"
        _add_rc=$?
        set -e
        if [[ "$_add_rc" -ne 0 ]]; then
            log "fix_conflict_add_failed" "path=$_sfile" "exit_code=$_add_rc"
            return 1
        fi
    done < "$_paths_file"
    # Commit with a conventional-commits message.
    _short_id=$(printf '%s' "$AGENT_ID" | tr -d '-' | cut -c1-8)
    _commit_msg_path="$AGENT_WORKSPACE/fix-conflict-commit-msg.txt"
    {
        printf 'chore(agent): resolve rebase conflicts (#%s)\n\n' "${ISSUE_NUMBER:-0}"
        printf 'fix_conflict skill reconciled conflicts against origin/main (#3225).\n'
        printf 'agent=%s attempts=%s\n' "$_short_id" "$(read_merge_conflict_attempts)"
    } > "$_commit_msg_path"
    set +e
    git -C "$REPO_ROOT" commit -F "$_commit_msg_path" \
        > "$AGENT_WORKSPACE/fix-conflict-commit.stdout.log" \
        2> "$AGENT_WORKSPACE/fix-conflict-commit.stderr.log"
    _commit_rc=$?
    set -e
    log "fix_conflict_commit_done" "exit_code=$_commit_rc"
    if [[ "$_commit_rc" -ne 0 ]]; then
        log "fix_conflict_commit_failed" "exit_code=$_commit_rc" \
            "stderr_tail=$(tail -c 200 "$AGENT_WORKSPACE/fix-conflict-commit.stderr.log" 2>/dev/null | tr '\n' ' ')"
        return 1
    fi
    return 0
}

handle_fix_conflict() {
    # Mechanical + claude-driven implementation of the fix_conflict
    # phase (#3225).
    #
    # Flow:
    #   1. Budget check — read merge_conflict_attempts; if already
    #      >= FIX_CONFLICT_MAX_ATTEMPTS, emit the unresolvable +
    #      budget_exhausted envelope without invoking claude.
    #   2. Increment the counter before invoking claude (so a claude
    #      crash still bumps the attempt count and a retry will hit
    #      the budget gate if the skill is broken).
    #   3. Invoke ``claude -p /task-v2-fix-conflict``. Input already
    #      written by write_phase_input('fix-conflict'). Output read
    #      from tmp/dispatcher-output/fix-conflict.json by
    #      run_claude_phase.
    #   4. On verdict=resolved: apply resolved_files as a new commit;
    #      emit ``{"verdict": "resolved", ...}`` so the transition
    #      shim re-enters push_and_pr.
    #   5. On verdict=unresolvable (or any non-resolved output, or
    #      an apply failure): emit the unresolvable envelope so the
    #      transition shim advances to conflict_unresolvable.
    #
    # Prints the phase-output JSON on stdout for persist_phase_output
    # + transition_for to consume. Always exits 0 — verdict-driven.

    if [[ "$AGENT_RUNNER_DRY_RUN" == "1" ]]; then
        log "fix_conflict_dry_run"
        printf '{"verdict": "unresolvable", "resolution_notes": "dry-run: skill not invoked", "resolved_files": []}'
        return 0
    fi

    # ── Budget gate ───────────────────────────────────────────────
    _attempts=$(read_merge_conflict_attempts)
    log "fix_conflict_budget_check" \
        "attempts=$_attempts" \
        "max=$FIX_CONFLICT_MAX_ATTEMPTS"
    if [[ "$_attempts" -ge "$FIX_CONFLICT_MAX_ATTEMPTS" ]]; then
        log "fix_conflict_budget_exhausted" \
            "attempts=$_attempts" \
            "max=$FIX_CONFLICT_MAX_ATTEMPTS"
        # Emit the synthetic budget-exhausted envelope. The
        # transition shim (transition_from_fix_conflict) sees
        # verdict=unresolvable + budget_exhausted=true and routes to
        # conflict_unresolvable via the diagnoser hint.
        printf '{"verdict": "unresolvable", "budget_exhausted": true, "resolution_notes": "fix_conflict budget exhausted (attempts=%s, max=%s)", "resolved_files": []}' \
            "$_attempts" "$FIX_CONFLICT_MAX_ATTEMPTS"
        return 0
    fi

    # ── Increment BEFORE claude ──────────────────────────────────
    # A claude crash still counts against the budget — otherwise a
    # broken skill could spin forever.
    increment_merge_conflict_attempts
    _new_attempts=$(read_merge_conflict_attempts)
    log "fix_conflict_attempts_incremented" "new_attempts=$_new_attempts"

    # ── Invoke the claude skill ──────────────────────────────────
    # run_claude_phase maps fix_conflict → fix-conflict and writes
    # the input bundle via phase_input_shim.py.
    _output=$(run_claude_phase "fix_conflict")
    _verdict=$(printf '%s' "$_output" | jq -r '.verdict // ""' 2>/dev/null \
        | tr '[:upper:]' '[:lower:]')
    log "fix_conflict_skill_verdict" "verdict=$_verdict"

    if [[ "$_verdict" == "resolved" ]]; then
        # Apply resolved_files as a new commit. The output JSON on
        # disk is the authoritative source; apply_resolved_files
        # reads it directly.
        _out_json_path="$REPO_ROOT/tmp/dispatcher-output/fix-conflict.json"
        if apply_resolved_files "$_out_json_path"; then
            log "fix_conflict_resolved_applied"
            printf '%s' "$_output"
            return 0
        fi
        # Apply failed → demote to unresolvable so we don't re-enter
        # push_and_pr with a broken tree. Preserve the skill's notes
        # for the diagnoser.
        log "fix_conflict_apply_failed_demoting_to_unresolvable"
        _notes=$(printf '%s' "$_output" | jq -r '.resolution_notes // ""' 2>/dev/null)
        printf '{"verdict": "unresolvable", "apply_failed": true, "resolution_notes": %s, "resolved_files": []}' \
            "$(printf '%s' "apply_resolved_files failed after skill returned resolved; original notes: $_notes" | jq -R -s .)"
        return 0
    fi

    # Any non-resolved verdict (unresolvable, missing, malformed) —
    # pass through so the transition shim routes to
    # conflict_unresolvable.
    log "fix_conflict_not_resolved" "verdict=$_verdict"
    if [[ -n "$_output" && "$_output" != "{}" ]]; then
        printf '%s' "$_output"
    else
        printf '{"verdict": "unresolvable", "resolution_notes": "skill returned empty / malformed output", "resolved_files": []}'
    fi
    return 0
}

# ── fix_ci handler (#3245) ────────────────────────────────────────────────
#
# ECS-mode equivalent of the subprocess daemon's ``_run_fix_ci`` +
# ``_apply_fix_ci_patch`` (scripts/dispatcher/daemon.py ~line 12697 and
# 12979). Before #3245 the entrypoint dispatched ``fix_ci`` through the
# generic ``planning|ralph|summary|verify`` case — it called the skill,
# persisted the phase_output, and routed via ``transition_for`` but
# never staged / committed / pushed the working-tree changes the skill
# had just produced. Result: every ECS agent with initially-red CI
# looped 40 iterations of "sonnet patches the worktree → entrypoint
# discards the patch → CI re-runs unchanged → red → fix_ci again,"
# wasting ~75 min + 40 sonnet invocations per agent. See issue #3245
# and the /task-v2-fix-ci SKILL.md (lines 64 + 160) which explicitly
# defers git ops to "the daemon."
#
# The ``/task-v2-fix-ci`` skill writes a JSON envelope of shape:
#
#     {
#       "verdict": "PATCHED" | "BLOCKED" | "FLAKY",
#       "commit_message": "fix(area): desc — CI (#<PR>)",
#       "changed_files": ["path1", "path2", ...],
#       "block_reason": "<str>" | null,
#       "flaky_evidence": "<str>" | null,
#       ...
#     }
#
# After ``run_claude_phase "fix_ci"`` returns, this handler:
#
#   * **PATCHED** — reads ``commit_message``, runs ``git add -A``,
#     verifies ``git diff --cached`` has staged changes (empty-diff
#     means the skill lied), runs ``git commit -m <msg>`` and
#     ``git push origin <branch>``. On success leaves the agent in
#     ``awaiting_ci`` so the next tick re-polls CI against the new
#     commit. On any failure (missing commit_message, empty diff,
#     git add/commit/push non-zero exit) advances to ``fix_ci_failed``
#     terminal via ``agent_runner_reaped_failure``.
#   * **FLAKY** — no code change; advance back to ``awaiting_ci`` so
#     the next poll re-checks the rollup (GitHub may auto-retry a
#     flaky job, or a manual nudge resolves eventually). Mirrors the
#     daemon's FLAKY branch.
#   * **BLOCKED** (or unrecognized verdict) — log and route to
#     ``fix_ci_failed`` terminal. Same as daemon except the daemon's
#     tier-3 failure-row + diagnoser routing is not mirrored here —
#     the terminal signals "this agent is done, daemon supervisor
#     picks it up from the failure_row category." (The daemon-side
#     ``_handle_agent_failure`` would normally write the failure row;
#     in ECS mode the supervisor's ``_find_diagnoser_candidates`` sweep
#     picks up the agent by its terminal phase instead.)
#
# Log event names (``fix_ci_patch_pushed``, ``fix_ci_missing_commit_
# message``, ``fix_ci_git_commit_failed``, ``fix_ci_git_push_failed``,
# ``fix_ci_patch_empty_already_applied`` (#3580 — was ``fix_ci_patch_empty``
# pre-#3580; renamed to split it out from real failure classes since this
# is now an advance-to-awaiting_ci event, not a terminal),
# ``fix_ci_flaky``, ``fix_ci_blocked``, ``fix_ci_unrecognized_verdict``)
# mirror the daemon's names so CloudWatch log-insights queries work across
# both paths.
#
# Prints the phase-output JSON on stdout (the same JSON that
# ``run_claude_phase`` emitted, possibly re-wrapped) for
# ``persist_phase_output`` in the caller. Always exits 0 — the
# caller decides whether to advance or leave the terminal alone.
#
# Side effects (handler-owned, distinct from the ``run_claude_phase``
# flow — so this handler does its own advance via
# ``advance_phase`` / ``agent_runner_reaped_failure``):
#
#   * ``git -C "$REPO_ROOT" add -A`` (stage skill's edits).
#   * ``git -C "$REPO_ROOT" diff --cached --quiet`` (empty-diff check).
#   * ``git -C "$REPO_ROOT" commit -m "$commit_message"`` (commit).
#   * ``git -C "$REPO_ROOT" push origin HEAD`` (push to PR branch).
#
# The caller (the fix_ci case arm) consumes the $_output envelope via
# ``persist_phase_output`` but delegates transition to this handler
# by reading the ``$_fix_ci_next_action`` global it writes before
# returning. Pattern matches ``handle_awaiting_ci`` which also owns
# its own advance decisions from within the handler via
# ``advance_phase`` / ``agent_runner_reaped_failure`` calls.
handle_fix_ci() {
    # Invoke the /task-v2-fix-ci skill via run_claude_phase, parse
    # the verdict, and stage+commit+push on PATCHED. Mirrors the
    # daemon's ``_run_fix_ci`` + ``_apply_fix_ci_patch`` for the ECS
    # path. Prints the skill's output JSON on stdout (so the caller
    # can persist it into phase_outputs), advances the phase row
    # internally via ``advance_phase`` / ``agent_runner_reaped_failure``,
    # and always exits 0.
    #
    # Environment inputs:
    #   REPO_ROOT       — per-agent clone root (contains the worktree
    #                     changes written by the skill).
    #   BRANCH_NAME     — the agent's branch name (e.g. agent/<shortid>).
    #   AGENT_WORKSPACE — stderr/stdout log capture dir.

    _output=$(run_claude_phase "fix_ci")
    persist_phase_output "fix_ci" "$_output"

    _verdict=$(printf '%s' "$_output" | jq -r '.verdict // "" | ascii_upcase' 2>/dev/null)
    log "fix_ci_verdict" "verdict=$_verdict"

    if [[ "$_verdict" == "PATCHED" ]]; then
        # Parse commit_message from the skill output. The daemon
        # treats a missing / empty commit_message as a hard failure
        # via FAILURE_CATEGORY_FIX_CI_APPLY_FAILED + sub_reason=
        # missing_commit_message. We terminal to fix_ci_failed.
        _commit_msg=$(printf '%s' "$_output" | jq -r '.commit_message // ""' 2>/dev/null)
        if [[ -z "$_commit_msg" ]]; then
            log "fix_ci_missing_commit_message" "verdict=$_verdict"
            agent_runner_reaped_failure \
                "fix_ci_failed" \
                "missing_commit_message" \
                "fix_ci skill returned PATCHED with no commit_message"
            return 0
        fi

        # Stage all worktree changes. ``git add -A`` mirrors the
        # daemon (daemon.py line ~13039) — the skill may have
        # created new files or deleted ones, so staging by
        # changed_files list alone can miss deletions / renames.
        set +e
        git -C "$REPO_ROOT" add -A \
            > "$AGENT_WORKSPACE/fix-ci-git-add.stdout.log" \
            2> "$AGENT_WORKSPACE/fix-ci-git-add.stderr.log"
        _add_rc=$?
        set -e
        if [[ "$_add_rc" -ne 0 ]]; then
            _add_tail=$(tail -c 200 "$AGENT_WORKSPACE/fix-ci-git-add.stderr.log" \
                2>/dev/null | tr '\n' ' ')
            log "fix_ci_git_add_failed" \
                "exit_code=$_add_rc" \
                "stderr_tail=$_add_tail"
            agent_runner_reaped_failure \
                "fix_ci_failed" \
                "git_add_failed" \
                "git add -A exit=$_add_rc stderr=$_add_tail"
            return 0
        fi

        # Empty-diff check (#3245, refined #3580, bifurcated #3635):
        # ``git diff --cached --quiet`` exits 0 when nothing is staged.
        # After a PATCHED verdict that's an ambiguous signal — it could
        # mean:
        #
        #   Benign (ahead > 0): the fix is already in the rebased
        #   baseline and there is nothing new to commit, BUT prior
        #   commits on this branch mean CI will still observe the
        #   patched state. It happens routinely when:
        #   1. The pre-fix_ci rebase pulled in a sibling PR that landed
        #      the same fix on main first.
        #   2. The skill's edit was idempotent (re-writing the existing
        #      content); git diff is empty even though the skill thinks
        #      it edited a file.
        #   3. A prior fix_ci attempt on the same branch already
        #      committed the fix and this is a retry.
        #   Correct action: advance to awaiting_ci so the next
        #   supervisor tick observes the now-green CI rollup.
        #
        #   Malign (ahead == 0): the branch has NO commits ahead of
        #   origin/main — the LLM hallucinated a PATCHED verdict without
        #   actually committing anything (lost commit, missing ``git
        #   add``, skill bug). Advancing to awaiting_ci here would poll
        #   CI forever on a branch indistinguishable from main. Correct
        #   action: terminal-fail as fix_ci_failed/fix_ci_patched_without_commit.
        #
        # Pre-#3580 the empty-diff branch always terminal-failed
        # (fix_ci_failed/patch_empty), which left issues stuck in a
        # daemon-cooldown loop (re-claim → rebase pulls in same fix →
        # empty diff → fail → cooldown → repeat). #3580 fixed the
        # benign case by advancing unconditionally. #3635 restores a
        # terminal path for the malign case so LLM-hallucination / lost-
        # commit failures surface as failures instead of being silently
        # swallowed by an infinite awaiting_ci loop.
        #
        # Log events:
        #   fix_ci_patch_empty_already_applied — benign (ahead > 0)
        #   fix_ci_patch_empty_no_commits      — malign (ahead == 0)
        set +e
        git -C "$REPO_ROOT" diff --cached --quiet \
            > /dev/null 2>&1
        _diff_rc=$?
        set -e
        if [[ "$_diff_rc" -eq 0 ]]; then
            _ahead=$(git -C "$REPO_ROOT" rev-list --count origin/main..HEAD 2>/dev/null || printf '0')
            if [[ "$_ahead" == "0" ]]; then
                _head_sha=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || printf 'unknown')
                log "fix_ci_patch_empty_no_commits" \
                    "verdict=$_verdict" \
                    "ahead=$_ahead" \
                    "head_sha=$_head_sha"
                agent_runner_reaped_failure \
                    "fix_ci_failed" \
                    "fix_ci_patched_without_commit" \
                    "fix-ci returned PATCHED but worktree has no commits ahead of origin/main"
                return 0
            fi
            log "fix_ci_patch_empty_already_applied" \
                "verdict=$_verdict" \
                "ahead=$_ahead" \
                "commit_message=$_commit_msg"
            advance_phase "awaiting_ci"
            printf '%s' "$_output"
            return 0
        fi

        # Commit. Write the message to a file so ``git commit -F`` gets
        # the literal message verbatim — avoids any quoting / $-expansion
        # hazards with multi-line or special-char commit messages.
        _commit_msg_path="$AGENT_WORKSPACE/fix-ci-commit-msg.txt"
        printf '%s' "$_commit_msg" > "$_commit_msg_path"
        set +e
        git -C "$REPO_ROOT" commit -F "$_commit_msg_path" \
            > "$AGENT_WORKSPACE/fix-ci-git-commit.stdout.log" \
            2> "$AGENT_WORKSPACE/fix-ci-git-commit.stderr.log"
        _commit_rc=$?
        set -e
        if [[ "$_commit_rc" -ne 0 ]]; then
            _commit_tail=$(tail -c 200 "$AGENT_WORKSPACE/fix-ci-git-commit.stderr.log" \
                2>/dev/null | tr '\n' ' ')
            log "fix_ci_git_commit_failed" \
                "exit_code=$_commit_rc" \
                "stderr_tail=$_commit_tail"
            agent_runner_reaped_failure \
                "fix_ci_failed" \
                "git_commit_failed" \
                "git commit exit=$_commit_rc stderr=$_commit_tail"
            return 0
        fi

        # Push. The entrypoint uses the agent's named branch rather than
        # ``HEAD`` so the push target is unambiguous even if a rebase or
        # detached-HEAD state somehow snuck in — matches daemon.py line
        # ~13166's ``push origin <branch>`` pattern.
        set +e
        git -C "$REPO_ROOT" push origin "$BRANCH_NAME" --no-verify \
            > "$AGENT_WORKSPACE/fix-ci-git-push.stdout.log" \
            2> "$AGENT_WORKSPACE/fix-ci-git-push.stderr.log"
        _push_rc=$?
        set -e
        if [[ "$_push_rc" -ne 0 ]]; then
            _push_tail=$(tail -c 200 "$AGENT_WORKSPACE/fix-ci-git-push.stderr.log" \
                2>/dev/null | tr '\n' ' ')
            log "fix_ci_git_push_failed" \
                "exit_code=$_push_rc" \
                "stderr_tail=$_push_tail"
            agent_runner_reaped_failure \
                "fix_ci_failed" \
                "git_push_failed" \
                "git push exit=$_push_rc stderr=$_push_tail"
            return 0
        fi

        log "fix_ci_patch_pushed" \
            "branch=$BRANCH_NAME" \
            "commit_message=$_commit_msg"
        # Back to awaiting_ci so the next supervisor tick re-polls
        # the rollup against the new push. Mirrors daemon behavior
        # (daemon.py: fix_ci success leaves phase=awaiting_ci).
        advance_phase "awaiting_ci"
        printf '%s' "$_output"
        return 0
    fi

    if [[ "$_verdict" == "FLAKY" ]]; then
        _flaky_evidence=$(printf '%s' "$_output" | jq -r '.flaky_evidence // ""' 2>/dev/null)
        log "fix_ci_flaky" "flaky_evidence=$_flaky_evidence"
        # No code change. Next tick re-polls CI. GitHub may auto-retry
        # the flaky job, or a manual nudge resolves eventually.
        advance_phase "awaiting_ci"
        printf '%s' "$_output"
        return 0
    fi

    # BLOCKED or unrecognized verdict — terminal.
    if [[ "$_verdict" == "BLOCKED" ]]; then
        _block_reason=$(printf '%s' "$_output" | jq -r '.block_reason // ""' 2>/dev/null)
        log "fix_ci_blocked" "block_reason=$_block_reason"
        agent_runner_reaped_failure \
            "fix_ci_failed" \
            "fix_ci_blocked" \
            "fix_ci skill returned BLOCKED: $_block_reason"
    else
        log "fix_ci_unrecognized_verdict" "verdict=$_verdict"
        agent_runner_reaped_failure \
            "fix_ci_failed" \
            "unrecognized_verdict" \
            "fix_ci skill returned unrecognized verdict: $_verdict"
    fi
    printf '%s' "$_output"
    return 0
}

# ── Ralph HEAD-watcher (#3144) ────────────────────────────────────────────
#
# ECS-mode equivalent of the daemon's ``_start_ralph_head_watcher``
# (#3042). The daemon's watcher only runs in subprocess mode — when the
# dispatcher runs in ECS mode, every ralph phase was invisible between
# ``claude_phase_begin`` and ``claude_phase_done`` (30-120 min later).
# Concrete cost: agent ``6e79fb52`` on #2671 spent 85+ minutes in ralph
# on 2026-04-23 with zero CloudWatch events and zero
# ``dispatcher.ralph_patches`` rows, making "is ralph stuck or just
# slow?" unanswerable from the fleet-status page.
#
# This subshell polls ``git log origin/main..HEAD`` every
# ``$AGENT_RUNNER_RALPH_HEAD_POLL_INTERVAL`` seconds (default 30, matches
# the daemon's ``RALPH_HEAD_POLL_INTERVAL_SECONDS``) and for each new
# commit:
#
#   1. Emits ``agent_runner.ralph_iteration_observed`` with
#      ``iteration_n`` (1-based count from origin/main..HEAD),
#      ``commit_sha``, and ``commit_subject_first_80``.
#   2. INSERTs a ``dispatcher.ralph_patches`` row with
#      ``iteration_n``, ``commit_sha``, and the cumulative patch
#      content from ``git format-patch origin/main..HEAD --stdout``
#      (mirrors the daemon's format — full range, not ``-1 HEAD``).
#      Uses ``ON CONFLICT (agent_id, iteration_n) DO NOTHING`` via the
#      partial unique index from migration 42 so the watcher and
#      ``persist_ralph_patch`` can't double-insert the same iteration.
#   3. UPDATEs ``dispatcher.agents.ralph_iterations_observed`` to the
#      current count so admin / fleet-status reads the value without
#      a subquery.
#
# Failure handling — all best-effort, must never fault ralph:
#
# * DB down → emit ``ralph_head_watcher_db_failure`` warning, sleep,
#   retry next tick. Never exit the subshell loop.
# * git rev-parse failure → tolerate silently (transient lock during
#   concurrent ``git commit``), retry next tick.
# * Empty / unparseable format-patch → log ``ralph_head_watcher_empty_
#   patch`` and skip the INSERT for this iteration.
#
# Lifecycle:
#
# * Started right before ``run_claude_phase "ralph"``, PID captured in
#   ``$_ralph_watcher_pid``.
# * Stopped right after — kill + wait for clean shutdown.
# * The subshell writes a sentinel file so the caller can sanity-check
#   the watcher actually got to its first poll before ralph ran.

RALPH_HEAD_POLL_INTERVAL="${AGENT_RUNNER_RALPH_HEAD_POLL_INTERVAL:-30}"

ralph_head_watcher_loop() {
    # Body of the subshell. Runs until killed. Never exits on its own.
    #
    # Tracks seen commit SHAs in a flat file (parallel-indexed-array
    # equivalent that survives bash 3.2's lack of associative arrays).
    # Each line is one SHA. On each tick we read the current
    # ``origin/main..HEAD`` commit list oldest-first and diff it
    # against the seen file.

    # SIGTERM / SIGINT → kill any running ``sleep`` child + exit. In
    # bash 3.2 a foreground ``sleep`` blocks signal handling until it
    # returns, which would delay stop_ralph_head_watcher's ``wait``
    # past the end of ralph by up to one poll interval. Running the
    # sleep in the background and ``wait``-ing on it lets the signal
    # land immediately, the trap fires, and the kill here severs the
    # sleep child so our exit is prompt.
    _ralph_watcher_sleep_pid=""
    trap '[[ -n "$_ralph_watcher_sleep_pid" ]] && kill "$_ralph_watcher_sleep_pid" 2>/dev/null; exit 0' TERM INT
    _seen_file="$AGENT_WORKSPACE/ralph-head-watcher-seen.txt"
    : > "$_seen_file"

    # Baseline establishment: any commits that already exist at watcher
    # start (e.g. prior-patch re-applied via ``git am``) are recorded as
    # seen but do NOT emit events. Only NEW commits during ralph
    # trigger ``ralph_iteration_observed``.
    _baseline_log="$AGENT_WORKSPACE/ralph-head-watcher-baseline.log"
    if git -C "$REPO_ROOT" log --reverse --format='%H' origin/main..HEAD \
            > "$_baseline_log" 2>/dev/null; then
        cp "$_baseline_log" "$_seen_file"
        _baseline_count=$(wc -l < "$_seen_file" | tr -d ' ')
    else
        _baseline_count=0
    fi

    log "ralph_head_watcher_started" \
        "poll_interval=$RALPH_HEAD_POLL_INTERVAL" \
        "baseline_count=$_baseline_count"

    # Sentinel so the test harness can confirm the watcher started.
    printf 'started\n' > "$AGENT_WORKSPACE/ralph-head-watcher.started"

    while true; do
        ralph_head_watcher_tick "$_seen_file" || true
        # Background the sleep so the trap can interrupt it. ``wait``
        # returns non-zero when a trapped signal fires, which is fine
        # — the trap has already called exit 0.
        sleep "$RALPH_HEAD_POLL_INTERVAL" &
        _ralph_watcher_sleep_pid=$!
        wait "$_ralph_watcher_sleep_pid" 2>/dev/null || true
        _ralph_watcher_sleep_pid=""
    done
}

ralph_head_watcher_tick() {
    # One polling iteration. Returns non-zero on any unrecoverable
    # error so the loop can ``|| true`` it without leaking.
    _seen_file="$1"
    _current_log="$AGENT_WORKSPACE/ralph-head-watcher-current.log"

    # Oldest-first so iteration_n counts up monotonically.
    if ! git -C "$REPO_ROOT" log --reverse --format='%H %s' origin/main..HEAD \
            > "$_current_log" 2>/dev/null; then
        # Transient git error (worktree lock, concurrent commit). Skip.
        return 0
    fi

    # Iterate each line; for any SHA not yet in $_seen_file, emit +
    # persist + append. Bash 3.2 compat: ``while IFS= read``, not
    # ``readarray``.
    _iteration_n=0
    while IFS= read -r _line; do
        _iteration_n=$((_iteration_n + 1))
        if [[ -z "$_line" ]]; then
            continue
        fi
        _sha="${_line%% *}"
        _subject="${_line#* }"
        if [[ "$_sha" == "$_line" ]]; then
            # Malformed line (no space) — skip defensively.
            continue
        fi
        # Already observed? (grep -F -x -q for exact full-line match.)
        if grep -F -x -q "$_sha" "$_seen_file" 2>/dev/null; then
            continue
        fi

        # Truncate subject to 80 chars for the structured log field.
        # ``cut -c 1-80`` is bash-3.2-safe and byte-oriented (which is
        # fine — this is a triage field, not an exact excerpt).
        _subject_80=$(printf '%s' "$_subject" | cut -c 1-80)

        ralph_head_watcher_persist "$_iteration_n" "$_sha" "$_subject_80"

        # Record AFTER the persist attempt so a persist failure does
        # not silently drop the SHA from the retry set on the next
        # tick. (Persist is idempotent via ON CONFLICT DO NOTHING, so
        # re-attempting is safe.)
        printf '%s\n' "$_sha" >> "$_seen_file"
    done < "$_current_log"
}

ralph_head_watcher_persist() {
    # $1 = iteration_n (1-based count from origin/main..HEAD), $2 =
    # commit_sha, $3 = subject_80. Emits the structured log event and
    # persists the patch + bumps the counter. All DB failures are
    # tolerated with a warning — the watcher is purely observational.
    _iter="$1"
    _sha="$2"
    _subject_80="$3"

    log "ralph_iteration_observed" \
        "iteration_n=$_iter" \
        "commit_sha=$_sha" \
        "commit_subject_first_80=$_subject_80"

    # Capture the cumulative patch (full range). Mirrors the daemon's
    # ``_persist_ralph_iteration_patch`` — resume via ``git am --3way``
    # wants the whole series, not just the last commit.
    _patch_file="$AGENT_WORKSPACE/ralph-iter-$_iter.patch"
    if ! git -C "$REPO_ROOT" format-patch origin/main..HEAD --stdout \
            > "$_patch_file" 2>/dev/null; then
        log "ralph_head_watcher_empty_patch" \
            "iteration_n=$_iter" \
            "commit_sha=$_sha" \
            "reason=format_patch_failed"
        return 0
    fi
    if [[ ! -s "$_patch_file" ]]; then
        log "ralph_head_watcher_empty_patch" \
            "iteration_n=$_iter" \
            "commit_sha=$_sha" \
            "reason=empty_output"
        return 0
    fi

    _issue_clause="${ISSUE_NUMBER:-0}"

    # SELECT-then-INSERT guard against double-emit races. The race we
    # care about: ralph's own SHIP-time ``persist_ralph_patch`` and
    # this watcher both seeing the final commit. Migration 42 notes
    # why we don't add a ``(agent_id, iteration_n)`` unique constraint
    # instead: the daemon's subprocess-mode
    # ``_persist_ralph_iteration_patch`` inserts without ON CONFLICT
    # handling, so retroactively adding uniqueness would break the
    # daemon path. The cost of SELECT-then-INSERT is a second round-
    # trip per iteration — negligible at our write rate.
    #
    # #3488 — use psql -v variables for all three SQL statements so
    # that $AGENT_ID, $_iter, and $_sha reach psql via its own variable
    # substitution rather than bash string interpolation. The INSERT also
    # uses the \set patch_content `cat :'patch_path'` idiom (same as
    # persist_ralph_patch / persist_phase_output) so patch content never
    # lands in a bash command line.
    set +e
    _already=$(psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -At \
        -v agent_id="$AGENT_ID" \
        -v iter="$_iter" \
        -c "SELECT 1 FROM dispatcher.ralph_patches WHERE agent_id = :'agent_id' AND iteration_n = :'iter'::int LIMIT 1;" \
        2>/dev/null)
    _check_rc=$?
    set -e
    if [[ $_check_rc -ne 0 ]]; then
        log "ralph_head_watcher_db_failure" \
            "iteration_n=$_iter" \
            "commit_sha=$_sha" \
            "op=select_existing_iteration"
        return 0
    fi
    if [[ "$_already" == "1" ]]; then
        log "ralph_head_watcher_skip_existing" \
            "iteration_n=$_iter" \
            "commit_sha=$_sha"
        return 0
    fi

    set +e
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
        -v agent_id="$AGENT_ID" \
        -v issue_number="$_issue_clause" \
        -v commit_sha="$_sha" \
        -v iter="$_iter" \
        -v patch_path="$_patch_file" <<'EOF' >/dev/null 2>&1
\set patch_content `cat :'patch_path'`
INSERT INTO dispatcher.ralph_patches
    (agent_id, issue_number, patch_content, commit_sha,
     iteration_n, verdict)
VALUES
    (:'agent_id', :'issue_number'::int, :'patch_content',
     :'commit_sha', :'iter'::int, 'LOOP');
EOF
    _insert_rc=$?
    set -e
    if [[ $_insert_rc -ne 0 ]]; then
        log "ralph_head_watcher_db_failure" \
            "iteration_n=$_iter" \
            "commit_sha=$_sha" \
            "op=insert_ralph_patches"
        return 0
    fi

    # Atomic UPDATE — set to the current iteration count (GREATEST
    # handles out-of-order arrival though the tick is strictly
    # monotone today). Separate statement so an INSERT conflict still
    # keeps the counter in sync on the next tick.
    set +e
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
        -v agent_id="$AGENT_ID" \
        -v iter="$_iter" \
        -c "UPDATE dispatcher.agents SET ralph_iterations_observed = GREATEST(ralph_iterations_observed, :'iter'::int) WHERE agent_id = :'agent_id';" \
        >/dev/null 2>&1
    _update_rc=$?
    set -e
    if [[ $_update_rc -ne 0 ]]; then
        log "ralph_head_watcher_db_failure" \
            "iteration_n=$_iter" \
            "commit_sha=$_sha" \
            "op=update_agents_counter"
        return 0
    fi

    log "ralph_head_watcher_persisted" \
        "iteration_n=$_iter" \
        "commit_sha=$_sha"
}

start_ralph_head_watcher() {
    # Fork the watcher subshell. Sets ``$_ralph_watcher_pid`` in the
    # caller's scope (global var — bash 3.2 compat, no ``local -n``).
    # When AGENT_RUNNER_DISABLE_RALPH_HEAD_WATCHER=1 (tests, emergency
    # kill-switch), skip entirely.
    _ralph_watcher_pid=""
    if [[ "${AGENT_RUNNER_DISABLE_RALPH_HEAD_WATCHER:-0}" == "1" ]]; then
        log "ralph_head_watcher_disabled"
        return 0
    fi

    # Run the loop in a subshell. fd 3 is inherited so log() still
    # writes to the top-level stdout the CloudWatch agent reads.
    ralph_head_watcher_loop &
    _ralph_watcher_pid=$!
    log "ralph_head_watcher_forked" "pid=$_ralph_watcher_pid"
}

stop_ralph_head_watcher() {
    # Kill the watcher subshell + reap it. Idempotent — safe to call
    # on disable path where _ralph_watcher_pid is empty.
    if [[ -z "${_ralph_watcher_pid:-}" ]]; then
        return 0
    fi
    # SIGTERM first (default for kill). The subshell is a plain
    # polling loop with no cleanup state beyond closing fds.
    if kill "$_ralph_watcher_pid" 2>/dev/null; then
        log "ralph_head_watcher_kill_sent" "pid=$_ralph_watcher_pid"
    fi
    # ``wait`` with set -e would turn a nonzero exit into a fatal.
    # Disable -e around the reap — SIGTERM yields exit 143, which is
    # normal termination for us.
    set +e
    wait "$_ralph_watcher_pid" 2>/dev/null
    _wait_rc=$?
    set -e
    log "ralph_head_watcher_stopped" \
        "pid=$_ralph_watcher_pid" \
        "wait_rc=$_wait_rc"
    _ralph_watcher_pid=""
}

# ── Skill phase watcher (#3462) ───────────────────────────────────────────
#
# Tails ``$REPO_ROOT/tmp/agent-status.txt`` while a scheduled
# skill (audit, spotcheck, etc.) is running and fans each ``phase:``
# change out as a structured ``agent_runner.skill_phase_change`` event
# on stdout (fd 3 via ``log``).
#
# This fills the observability gap between ``scheduled_skill_begin`` and
# ``scheduled_skill_done`` — cron skills can run for 10–90 min and the
# entrypoint emits nothing during that window. The SKILL itself already
# writes ``phase: <value>`` / ``summary: <value>`` to the status file;
# this watcher just surfaces those updates in real time.
#
# Modeled directly on the ``ralph_head_watcher_*`` pattern above —
# same trap idiom, same backgrounded-sleep SIGTERM handshake, same
# sentinel file.

AGENT_RUNNER_SKILL_PHASE_POLL_INTERVAL="${AGENT_RUNNER_SKILL_PHASE_POLL_INTERVAL:-5}"

skill_phase_watcher_loop() {
    # Body of the backgrounded subshell. Runs until killed by
    # stop_skill_phase_watcher. Never exits on its own.
    #
    # Parses ``phase:`` and ``summary:`` lines from the status file on
    # each tick. When the ``phase`` value changes, emits
    # ``log "skill_phase_change" ...``. Bash 3.2 compatible — plain
    # awk, no associative arrays.
    _skill_name_w="$1"

    _watcher_sleep_pid=""
    trap '[[ -n "$_watcher_sleep_pid" ]] && kill "$_watcher_sleep_pid" 2>/dev/null; exit 0' TERM INT

    _status_file="$REPO_ROOT/tmp/agent-status.txt"
    _last_phase=""

    log "skill_phase_watcher_started" \
        "skill=$_skill_name_w" \
        "poll_interval=$AGENT_RUNNER_SKILL_PHASE_POLL_INTERVAL" \
        "status_file=$_status_file"

    # Sentinel so the test harness can confirm the watcher started.
    printf 'started\n' > "$AGENT_WORKSPACE/skill-phase-watcher.started"

    while true; do
        if [[ -f "$_status_file" ]]; then
            _cur_phase=$(awk '/^phase: /{print substr($0, 8)}' "$_status_file" 2>/dev/null | head -n 1 || true)
            _cur_summary=$(awk '/^summary: /{print substr($0, 10)}' "$_status_file" 2>/dev/null | head -n 1 || true)
            if [[ -n "$_cur_phase" && "$_cur_phase" != "$_last_phase" ]]; then
                log "skill_phase_change" \
                    "skill=$_skill_name_w" \
                    "phase=$_cur_phase" \
                    "summary=${_cur_summary:-}"
                _last_phase="$_cur_phase"
            fi
        fi
        sleep "$AGENT_RUNNER_SKILL_PHASE_POLL_INTERVAL" &
        _watcher_sleep_pid=$!
        wait "$_watcher_sleep_pid" 2>/dev/null || true
        _watcher_sleep_pid=""
    done
}

start_skill_phase_watcher() {
    # Fork the watcher subshell. Sets ``$_skill_phase_watcher_pid`` in
    # the caller's scope (global var — bash 3.2 compat, no ``local -n``).
    # When AGENT_RUNNER_DISABLE_SKILL_PHASE_WATCHER=1 (tests, emergency
    # kill-switch), skip entirely.
    _skill_phase_watcher_pid=""
    if [[ "${AGENT_RUNNER_DISABLE_SKILL_PHASE_WATCHER:-0}" == "1" ]]; then
        log "skill_phase_watcher_disabled"
        return 0
    fi

    # Run the loop in a subshell. fd 3 is inherited so log() still
    # writes to the top-level stdout the CloudWatch agent reads.
    skill_phase_watcher_loop "$1" &
    _skill_phase_watcher_pid=$!
    log "skill_phase_watcher_forked" "pid=$_skill_phase_watcher_pid" "skill=$1"
}

stop_skill_phase_watcher() {
    # Kill the watcher subshell + reap it. Idempotent — safe to call
    # on disable path where _skill_phase_watcher_pid is empty.
    if [[ -z "${_skill_phase_watcher_pid:-}" ]]; then
        return 0
    fi
    if kill "$_skill_phase_watcher_pid" 2>/dev/null; then
        log "skill_phase_watcher_kill_sent" "pid=$_skill_phase_watcher_pid"
    fi
    set +e
    wait "$_skill_phase_watcher_pid" 2>/dev/null
    _wait_rc=$?
    set -e
    log "skill_phase_watcher_stopped" \
        "pid=$_skill_phase_watcher_pid" \
        "wait_rc=$_wait_rc"
    _skill_phase_watcher_pid=""
}

persist_ralph_patch() {
    # Called on ralph SHIP when the worktree has a staged/committed
    # diff. Reads `git format-patch -1 HEAD --stdout` and INSERTs the
    # content + HEAD sha into dispatcher.ralph_patches.
    #
    # #3488 — pass patch content via tmpfile + psql variable substitution
    # rather than bash interpolation. The prior implementation escaped
    # single quotes with sed and interpolated $_escaped_patch into the SQL
    # string via db_exec. Bash expanded $() / backticks / $VAR inside the
    # patch content BEFORE psql ever saw it, corrupting the SQL and
    # producing exit_code=126 silently (#3488, same class as #3413 for
    # persist_phase_output). The fix mirrors persist_phase_output (PR #3419):
    #   1. The patch file already exists ($AGENT_WORKSPACE/ralph-ship.patch).
    #      No extra mktemp needed.
    #   2. Use a quoted heredoc (<<'EOF') so bash performs zero expansion on
    #      the SQL body. The patch path reaches psql via -v patch_path, and
    #      psql's \set reads the file at psql time via `cat :'patch_path'`
    #      into a psql variable. :'patch_content' then quotes it as a SQL
    #      literal that postgres stores verbatim in TEXT.
    #   3. Wrap in set +e / set -e to capture failure without killing the
    #      entrypoint (matches persist_phase_output's envelope).
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

    _issue_clause="${ISSUE_NUMBER:-0}"
    set +e
    log "db_exec_begin" "fn=persist_ralph_patch commit_sha=$_commit_sha"
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
        -v agent_id="$AGENT_ID" \
        -v issue_number="$_issue_clause" \
        -v commit_sha="$_commit_sha" \
        -v patch_path="$_patch_file" <<'EOF' >/dev/null
\set patch_content `cat :'patch_path'`
INSERT INTO dispatcher.ralph_patches
    (agent_id, issue_number, patch_content, commit_sha)
VALUES
    (:'agent_id', :'issue_number'::int, :'patch_content', :'commit_sha');
EOF
    _rc=$?
    set -e
    log "db_exec_done" "fn=persist_ralph_patch commit_sha=$_commit_sha rc=$_rc"
    if [[ $_rc -ne 0 ]]; then
        log "ralph_patch_persist_failed" "commit_sha=$_commit_sha rc=$_rc"
        return $_rc
    fi
    log "ralph_patch_persisted" "commit_sha=$_commit_sha"
}

advance_phase() {
    # $1 = next phase, $2 = terminal_status (optional).
    _next="$1"
    _status="${2:-}"
    if [[ -n "$_status" ]]; then
        # #3822 — when the new status is a TERMINAL_STATUSES member, also
        # stamp ``ended_at`` so the admin cockpit's "Recently Completed"
        # query (filters on ``status terminal AND ended_at IS NOT NULL``,
        # see packages/api/src/graphql/dispatcher/resolvers.ts:534-548)
        # actually surfaces the row. Without this, the merge handler's
        # ``advance_phase awaiting_deploy succeeded`` write leaves
        # ended_at NULL — the next outer-loop iteration sees a terminal
        # status and exits via ``external_terminal_observed`` BEFORE
        # ``mark_ended`` runs (see lines around the main loop's
        # is_terminal_status check). The same shape leaks via
        # ``agent_runner_reaped_failure`` below. ``COALESCE(ended_at,
        # now())`` is idempotent + race-safe with the daemon's
        # housekeeping-side bulk backfill.
        if is_terminal_status "$_status"; then
            db_exec "UPDATE dispatcher.agents
                        SET phase = '$_next',
                            status = '$_status',
                            ended_at = COALESCE(ended_at, now())
                      WHERE agent_id = '$AGENT_ID';"
        else
            db_exec "UPDATE dispatcher.agents
                        SET phase = '$_next', status = '$_status'
                      WHERE agent_id = '$AGENT_ID';"
        fi
    else
        db_exec "UPDATE dispatcher.agents
                    SET phase = '$_next'
                  WHERE agent_id = '$AGENT_ID';"
    fi
    log "phase_advanced" "next_phase=$_next" "status=${_status:-unchanged}"
}

# ── Centralized transition-action dispatch (#3581) ────────────────────────
#
# Single source of truth for translating a ``transition_for`` tuple
# (action, next, status, hint) into the right downstream call
# (advance_phase / advance_phase with terminal status / descriptive
# terminal via agent_runner_reaped_failure). All per-phase case-statements
# in the main dispatch loop call into this helper instead of reimplementing
# the action-vocabulary dispatch from scratch.
#
# Why this exists (#3581):
#   Prior to this refactor, every phase (push_and_pr, ralph_baseline,
#   fix_conflict, operational, the generic post-claude arm) had its own
#   ``case "$_action" in advance) ... advance_with_status) ...
#   route_to_diagnoser) ... *) ...`` block, with the route_to_diagnoser
#   sub-case-statement duplicated almost-identically. When a new action
#   kind (e.g. ``route_to_diagnoser`` itself, in #3543) or a new failure
#   hint (e.g. ``push_and_pr_no_unmerged_files``) was added, every phase
#   needed an independent update. Whichever ones the PR author missed
#   became latent bugs (#3543, #3558, #3573, #3580).
#
#   Centralizing eliminates the entire bug class: new actions / hints
#   land in ONE place and every phase picks them up uniformly. The CI
#   guard ``scripts/check-transition-dispatch-vocabulary.sh`` enforces
#   that the helper's vocabulary stays in sync with phase_transitions.py.
#
# Inputs:
#   $1 = phase name (e.g. "push_and_pr"). Used as a log prefix and
#        included in unrecognized-action terminal-phase strings so the
#        operator sees which phase emitted the unhandled shape.
#   $2 = action ("advance" | "advance_with_status" | "route_to_diagnoser").
#   $3 = next phase (for advance / advance_with_status). May be empty
#        for route_to_diagnoser; ignored in that case.
#   $4 = terminal status (for advance_with_status). May be empty
#        otherwise; ignored.
#   $5 = failure hint (for route_to_diagnoser). May be empty if the
#        transition_for emitted route_to_diagnoser without a hint
#        (defensive — shouldn't happen, but the unrecognized-hint arm
#        catches it).
#   $6 = phase output JSON (optional). Only consulted by the
#        ``ralph_not_ship`` hint arm to extract ``block_reason``. Pass
#        the raw output JSON from the phase handler when calling for a
#        verdict-driven phase (post-claude / push_and_pr / fix_conflict
#        / operational); pass empty / "{}" otherwise.
#
# Side effects:
#   * advance: calls ``advance_phase $next``.
#   * advance_with_status: calls ``advance_phase $next $status``.
#   * route_to_diagnoser: emits a ``${phase}_route_to_diagnoser`` log
#     event with the hint, then dispatches by hint to a descriptive
#     terminal via ``agent_runner_reaped_failure``. The known-hint set
#     mirrors phase_transitions.py's ``FAILURE_HINT_*`` constants;
#     unknown hints emit a ``diagnoser_route_unrecognized_hint`` terminal.
#   * unrecognized action: emits a ``${phase}_transition_unrecognized``
#     terminal so a code-drift bug (transition_for returns a new action
#     kind that this helper doesn't handle) surfaces as a distinct
#     failure rather than being swallowed.
#
# Returns: 0 always (the underlying advance_phase / agent_runner_reaped_failure
# calls update the agent row; the loop tick observes the new state on
# the next iteration).
dispatch_transition_action() {
    local _phase="$1"
    local _action="$2"
    local _next="$3"
    local _status="$4"
    local _hint="$5"
    local _output="${6:-{}}"

    case "$_action" in
        advance)
            advance_phase "$_next"
            ;;
        advance_with_status)
            advance_phase "$_next" "$_status"
            ;;
        route_to_diagnoser)
            log "${_phase}_route_to_diagnoser" "hint=$_hint"
            # Hint-based descriptive-terminal dispatch. The known-hint
            # set mirrors FAILURE_HINT_* in phase_transitions.py — the
            # CI guard ``check-transition-dispatch-vocabulary.sh``
            # asserts every FAILURE_HINT_* constant is handled here.
            #
            # Each terminal phase name matches the daemon's
            # ``BYPASSED_TERMINAL_PHASES_TO_ROUTE`` mapping so the
            # supervisor tick's ``_find_diagnoser_candidates`` sweep
            # picks up the row with the right
            # ``FAILURE_CATEGORY_*`` for diagnoser routing.
            case "$_hint" in
                conflict_unresolvable)
                    agent_runner_reaped_failure \
                        "conflict_unresolvable" \
                        "conflict_unresolvable" \
                        "fix_conflict skill returned unresolvable or budget exhausted"
                    ;;
                ralph_not_ship)
                    # #3586 — ralph_not_ship routes through diagnoser
                    # via BYPASSED_TERMINAL_PHASES_TO_ROUTE. Pull
                    # block_reason from the phase output JSON so the
                    # failures-row reason carries ralph's structured
                    # signal for the diagnoser to act on.
                    local _ralph_block_reason
                    _ralph_block_reason=$(printf '%s' "$_output" | jq -r '.block_reason // ""' 2>/dev/null || printf '')
                    agent_runner_reaped_failure \
                        "ralph_not_ship" \
                        "ralph_not_ship" \
                        "$_ralph_block_reason"
                    ;;
                ralph_ac_infeasible|summary_ac_infeasible|fix_ci_blocked|verify_failed_post_merge|push_and_pr_no_unmerged_files|operational_failed|plan_blocked)
                    # Descriptive terminal — the daemon's
                    # BYPASSED_TERMINAL_PHASES_TO_ROUTE maps these
                    # phase names to FAILURE_CATEGORY_* so the
                    # diagnoser sweep picks them up.
                    agent_runner_reaped_failure \
                        "$_hint" \
                        "$_hint" \
                        "phase=${_phase} emitted route_to_diagnoser hint=$_hint"
                    ;;
                push_failed)
                    # #3789: ``handle_push_and_pr`` emitted
                    # ``{"push_failed": true, "reason": "..."}``
                    # because ``git push`` failed (timeout, PAT
                    # scope, pre-push hook reject, etc.). The
                    # transition shim returned
                    # FAILURE_HINT_PUSH_FAILED so the daemon routes
                    # to a dedicated diagnoser fix-shape (bump
                    # push timeout, fix PAT scope, investigate
                    # hook) rather than the generic advance-to-
                    # awaiting_ci that previously left the agent
                    # reaping as missing_pr (#3663). Pull
                    # ``push_reason`` from the transition context
                    # for the failures-row reason text.
                    local _pf_reason
                    _pf_reason=$(printf '%s' "$_output" | jq -r '.reason // "unknown"' 2>/dev/null || printf 'unknown')
                    agent_runner_reaped_failure \
                        "push_failed" \
                        "push_failed" \
                        "phase=${_phase} git push failed (reason=${_pf_reason})"
                    ;;
                claude_phase_timeout)
                    # #3766: the ``claude -p`` per-phase timeout fired
                    # (rc=124). The transition shim returned
                    # FAILURE_HINT_CLAUDE_PHASE_TIMEOUT so the daemon
                    # routes to a dedicated diagnoser fix-shape (bump
                    # the per-phase cap, investigate runaway iteration
                    # count, etc.) rather than the generic ralph_not_ship
                    # path. Pull elapsed_seconds + block_reason from the
                    # phase output JSON for the failures-row reason text.
                    local _cpt_elapsed
                    local _cpt_block_reason
                    _cpt_elapsed=$(printf '%s' "$_output" | jq -r '.elapsed_seconds // "unknown"' 2>/dev/null || printf 'unknown')
                    _cpt_block_reason=$(printf '%s' "$_output" | jq -r '.block_reason // ""' 2>/dev/null || printf '')
                    agent_runner_reaped_failure \
                        "claude_phase_timeout" \
                        "claude_phase_timeout" \
                        "phase=${_phase} claude -p timed out (elapsed_seconds=${_cpt_elapsed}); ${_cpt_block_reason}"
                    ;;
                *)
                    # Truly novel hint we don't recognize. The CI
                    # guard should have caught this at PR time — if
                    # we land here, a new FAILURE_HINT_* was added to
                    # phase_transitions.py without updating this
                    # dispatch helper. Emit a descriptive terminal so
                    # the operator sees what shape leaked through
                    # without tripping the route_stub bug.
                    log "${_phase}_route_unrecognized_hint" "hint=$_hint"
                    agent_runner_reaped_failure \
                        "diagnoser_route_unrecognized_hint" \
                        "diagnoser_route_unrecognized_hint" \
                        "phase=${_phase} route_to_diagnoser received unrecognized hint=${_hint:-(empty)}"
                    ;;
            esac
            ;;
        unrecognized|*)
            # Truly unrecognized action — transition_for returned a
            # shape this dispatcher doesn't know how to handle. The
            # CI guard should have caught this at PR time (a new
            # TransitionAction enum value was added to
            # phase_transitions.py without updating this helper).
            # Emit a descriptive terminal so the operator sees the
            # drift instead of advancing to an inconsistent state.
            log "${_phase}_transition_unrecognized" "action=$_action"
            agent_runner_reaped_failure \
                "${_phase}_transition_unrecognized" \
                "${_phase}_transition_unrecognized" \
                "phase=${_phase} returned action=$_action (not in dispatch vocabulary)"
            ;;
    esac
    return 0
}

mark_ended() {
    # Final update when the loop hits a terminal phase.
    db_exec "UPDATE dispatcher.agents
                SET ended_at = now()
              WHERE agent_id = '$AGENT_ID'
                AND ended_at IS NULL;"
    log "agent_ended"
}

# ── Post-PR mechanical phase handlers (#3176) ──────────────────────────────
#
# The subprocess-path daemon implements `awaiting_ci`, `merge`, and
# `awaiting_deploy` inline via `_advance_awaiting_ci`,
# `_merge_pr_and_advance`, and `_advance_awaiting_deploy` in daemon.py.
# For ECS mode these were previously no-op stubs that raced through the
# phase boundary in ~1s, producing orphan PRs. These handlers bring the
# ECS entrypoint to parity: poll CI, merge on green, watch deploy
# workflows, advance to verify.
#
# Design notes
# ------------
# * Each handler runs inside the per-agent Fargate task's ~2h wall
#   time budget (``AGENT_RUNNER_*_TIMEOUT_SECONDS`` caps the polling so
#   a stuck CI/deploy can't consume the whole budget).
# * Polling uses foreground ``sleep`` (no backgrounded subshells) — the
#   agent-runner container is single-purpose so blocking is fine.
# * All polling intervals + timeouts are env-overridable so tests can
#   run them with 0/1-second cadence against stubbed binaries.
# * These handlers are distinct from the `run_claude_phase` flow; they
#   do NOT call ``transition_for`` — the phase transition is
#   hand-coded based on the observed outcome (green/red/merged/
#   deploy_success/…) because `next_phase_from_verdict` is only wired
#   for the verdict-driven (claude) phases, per phase_transitions.py
#   comment "callers handle them explicitly because the input shape
#   differs".

# How often to re-poll PR status (CI + deploy). Defaults match the
# subprocess daemon's supervisor tick (120s). Tests override to 0/1.
CI_POLL_INTERVAL="${AGENT_RUNNER_CI_POLL_INTERVAL:-60}"
DEPLOY_POLL_INTERVAL="${AGENT_RUNNER_DEPLOY_POLL_INTERVAL:-60}"

# Hard timeouts. `awaiting_ci` = 60m matches the 60-min CI-watch ceiling
# the subprocess daemon's scheduler enforces in practice. `awaiting_
# deploy` = 30m matches deploy workflows' worst-case runtime.
AWAITING_CI_TIMEOUT_SECONDS="${AGENT_RUNNER_AWAITING_CI_TIMEOUT_SECONDS:-3600}"
AWAITING_DEPLOY_TIMEOUT_SECONDS="${AGENT_RUNNER_AWAITING_DEPLOY_TIMEOUT_SECONDS:-1800}"

# Short grace period before declaring "no deploy workflows fired" on
# the merge SHA. Deploy workflow triggers are eventually-consistent on
# GitHub's side; a poll immediately after merge can see zero runs even
# for a code PR that will deploy. Subprocess daemon works around this
# by re-polling each supervisor tick; we mirror with a bounded grace.
DEPLOY_GRACE_SECONDS="${AGENT_RUNNER_DEPLOY_GRACE_SECONDS:-90}"

# Max attempts to auto-unstick a merge stale-rollup rejection (#3163).
# Matches ``MERGE_UNSTICK_MAX_ATTEMPTS`` in daemon.py.
MERGE_UNSTICK_MAX_ATTEMPTS="${AGENT_RUNNER_MERGE_UNSTICK_MAX_ATTEMPTS:-1}"

# #3225: max invocations of the fix_conflict phase's claude skill per
# agent lifetime. The first attempt handles the common "main advanced
# during ralph" case; a second attempt covers the edge case where main
# advances AGAIN during the first resolution. Three attempts starts
# looking like a livelock — route cleanly to conflict_unresolvable.
# Tests override to 0 (budget-exhausted on first call) or 1 (allows
# exactly one attempt) via the env var.
FIX_CONFLICT_MAX_ATTEMPTS="${AGENT_RUNNER_FIX_CONFLICT_MAX_ATTEMPTS:-2}"

# Stderr substring that indicates the #2641/#3163 stale-rollup branch-
# protection rejection. Matches ``STALE_ROLLUP_STDERR_MARKER`` in
# daemon.py.
STALE_ROLLUP_MARKER="base branch policy prohibits the merge"

# #3656: hard timeout (seconds) on network-blocking git/gh operations
# inside ``handle_push_and_pr`` — ``git fetch origin main``, ``git
# push``, and ``gh pr create``. Wraps each call in ``timeout`` so a
# hung network IO does not silently consume an ECS cap slot for hours.
# The agent-runner container's ``bash`` keeps running while a child
# ``git push`` blocks indefinitely on auth-retry / network drop, so
# Fargate's "container alive" liveness signal is not enough — every
# observed hang in 2026-04-27 (#3608 / agent 2ff6e282) was the daemon
# silently waiting on the kernel's TCP retry of an already-broken
# socket. Default 300s leaves plenty of headroom for a healthy push +
# PR-create round trip while bounding the worst case to 5 minutes.
# Tests override to 1 (or use a stub ``timeout`` shim) to drive the
# 124-exit branch deterministically.
NETWORK_TIMEOUT_SECONDS="${AGENT_RUNNER_NETWORK_TIMEOUT_SECONDS:-300}"

# #3683: hard timeout (seconds) on the ``claude -p`` subprocess inside
# ``run_claude_phase``. Without this, a hung or wedged claude process
# silently pins the cap slot until the 30-minute heartbeat reaper fires
# — confirmed by five reaped tasks (agent cadf0773 / failure #216 and
# siblings) where the last CW log line before reaping was inside a
# claude_phase_begin with no matching claude_phase_done. 1800s matches
# the ``push_and_pr`` stuck-timeout fallback and the daemon's
# ``GIT_PUSH_TIMEOUT_SECONDS``. Tests override to 1 via
# ``AGENT_RUNNER_CLAUDE_PHASE_TIMEOUT_SECONDS=1``.
#
# #3766 — kept for back-compat as a global override. The actual lookup
# now uses the per-phase table below; ``CLAUDE_PHASE_TIMEOUT_SECONDS``
# is only consulted via ``AGENT_RUNNER_CLAUDE_PHASE_TIMEOUT_SECONDS=N``
# as an env-driven knob for tests + emergency operator overrides.
CLAUDE_PHASE_TIMEOUT_SECONDS="${AGENT_RUNNER_CLAUDE_PHASE_TIMEOUT_SECONDS:-1800}"

# #3766: per-phase ``claude -p`` timeout (seconds). The single global
# 1800s default was structurally too short for ralph — the
# ``task-v2-ralph`` SKILL.md describes the phase as "long-tail
# (~45-90 min internally)", so a 30-min cap silently SIGKILLs ralph
# mid-iteration. Two terminals on the post-#3761 deploy
# (#3641, #3638) hit this exact failure: ``timeout`` exited 124 at
# exactly 30:00, the SIGKILL truncated stdout/stderr, and the wrapper
# fell through to ``ralph_done_marker_missing`` with empty buffers
# — same diagnostic shape as the silent-ralph hook-swap bug fixed in
# #3757/PR #3761, but a different root cause.
#
# Per-phase caps are defined as individual ``CLAUDE_PHASE_TIMEOUT_*``
# constants (bash 3.2 compat — no associative arrays per
# ``scripts/check-bash-compat.sh``). Lookup happens in
# #3683: hard timeout (seconds) on local git operations that don't
# touch the network — ``git commit --amend``, ``git rebase
# origin/main``, ``git rebase --abort`` — inside
# ``handle_push_and_pr``. These have been observed to wedge on a
# corrupt index or a broken lock file, pinning the cap slot indefinitely
# with no network activity to trip NETWORK_TIMEOUT_SECONDS. 120s is
# generous for any healthy local-only git op; tests override to 1 via
# ``AGENT_RUNNER_LOCAL_GIT_TIMEOUT_SECONDS=1``.
LOCAL_GIT_TIMEOUT_SECONDS="${AGENT_RUNNER_LOCAL_GIT_TIMEOUT_SECONDS:-120}"

# #3683: wall-clock deadline (seconds) for the entire ``handle_push_and_pr``
# function. Belt-and-suspenders backstop for code paths the per-step
# timeouts above don't cover (e.g. db_exec UPDATE, jq, or any future
# shell statement added without a timeout). The deadline is checked
# before each major step via ``assert_phase_deadline_not_exceeded``.
# Default 600s ≤ STUCK_TIMEOUT_S_BY_PHASE["push_and_pr"] so the
# function always returns a structured envelope before the heartbeat
# reaper fires. Tests override to 1 via
# ``AGENT_RUNNER_PUSH_AND_PR_DEADLINE_SECONDS=1``.
PUSH_AND_PR_PHASE_DEADLINE_SECONDS="${AGENT_RUNNER_PUSH_AND_PR_DEADLINE_SECONDS:-600}"

# Deploy workflow display names we watch on the merge SHA. Must match
# the ``name:`` field in each ``.github/workflows/`` file. Mirrors the
# ``DEPLOY_WORKFLOW_NAMES`` frozenset in daemon.py exactly — both paths
# post-filter ``gh run list`` output by ``workflowName``.
#
# IMPORTANT (#3514): we intentionally do NOT use multi-``--workflow``
# flags with ``gh run list``. The ``-w``/``--workflow`` flag is
# single-valued; when invoked multiple times only the LAST value is
# honoured, silently dropping all other workflows from the result.
# Instead we fetch all runs for the commit and post-filter by
# ``workflowName`` via ``jq``. See ``find_deploy_runs`` below.
DEPLOY_WORKFLOW_NAMES_BASH="${AGENT_RUNNER_DEPLOY_WORKFLOW_NAMES:-Deploy API|Deploy Dispatcher|Deploy Scraper|Deploy Production|Deploy Production (Web)|Terraform|Deploy Agent Runner}"

read_pr_number() {
    # Query ``dispatcher.agents.pr_number`` for the current agent.
    # Returns an empty string if NULL / missing (db_query_one emits
    # the empty field for a NULL column with ``-At``).
    db_query_one "SELECT pr_number
                    FROM dispatcher.agents
                   WHERE agent_id = '$AGENT_ID'
                   LIMIT 1;"
}

classify_pr_rollup() {
    # $1 = path to ``gh pr view --json ... ,statusCheckRollup,mergeable,
    # mergeStateStatus`` stdout. Prints one of: green / red / pending /
    # error. Delegates to scripts/dispatcher/ci_classifier_cli.py — the
    # canonical Python implementation lives in
    # phase_transitions._ci_rollup_state and is shared with the daemon
    # (subprocess path) and worker-status.sh (operator dashboard) so
    # all four pre-#4417 sites resolve to the same classification.
    #
    # The entrypoint already invokes python3 for transition + phase-input
    # shims (see _setup_transition_shim / _setup_phase_input_shim above),
    # so the dependency surface is unchanged. Pre-#4417 a 60-line jq
    # program lived here — it diverged from the Python rule on STALE
    # handling and required parallel fixes for #4407 / #4414. The
    # CLI eliminates that divergence vector.
    _status_file="$1"
    if [[ ! -s "$_status_file" ]]; then
        printf 'error'
        return 0
    fi
    _classifier_py="$(dirname "${BASH_SOURCE[0]}")/ci_classifier_cli.py"
    _state=$(python3 "$_classifier_py" < "$_status_file" 2>/dev/null || printf 'error')
    if [[ -z "$_state" ]]; then
        printf 'error'
    else
        printf '%s' "$_state"
    fi
}

classify_deploy_runs() {
    # $1 = path to ``gh run list --json status,conclusion,...`` stdout.
    # Prints one of: success / failure / pending / none. Matches
    # ``_classify_deploy_runs`` in daemon.py. ``none`` means no
    # matching deploy runs — caller treats as "no deploy applicable".
    _runs_file="$1"
    if [[ ! -s "$_runs_file" ]]; then
        printf 'none'
        return 0
    fi
    _state=$(jq -r '
        def classify:
            if (type != "array" or length == 0) then "none"
            else
              ([.[]
                | (.status // "" | ascii_upcase) as $st
                | (.conclusion // "" | ascii_upcase) as $co
                | if $st != "COMPLETED" then "pending"
                  elif ($co == "SUCCESS" or $co == "SKIPPED" or $co == "NEUTRAL") then "ok"
                  else "failure" end]) as $outcomes
              | if ($outcomes | index("pending")) then "pending"
                elif ($outcomes | index("failure")) then "failure"
                else "success" end
            end;
        classify
    ' "$_runs_file" 2>/dev/null || printf 'none')
    if [[ -z "$_state" ]]; then
        printf 'none'
    else
        printf '%s' "$_state"
    fi
}

agent_runner_reaped_failure() {
    # $1 = terminal phase (e.g. ``awaiting_ci_timeout``).
    # $2 = category label (used only in the log line).
    # $3 = stderr tail / reason (truncated to 200 chars).
    #
    # Mirrors the subprocess-daemon's ``_handle_agent_failure`` enough
    # to drive an ECS agent to a terminal row: emit a structured log
    # event, persist a phase_outputs row describing the failure, and
    # UPDATE the agent row to ``status='failed' phase=$1``. Called
    # from handle_awaiting_ci / handle_merge / handle_awaiting_deploy
    # on their respective unrecoverable branches.
    _term_phase="$1"
    _category="$2"
    _reason=$(printf '%s' "${3:-}" | head -c 200 | tr '\n' ' ')
    log "agent_runner_reaped_failure" \
        "terminal_phase=$_term_phase" \
        "category=$_category" \
        "reason=$_reason"
    # #3219 — ON CONFLICT overwrite. See persist_phase_output for the
    # full rationale; the short version is that the unique index
    # ``idx_dispatcher_phase_outputs_agent_phase_attempt`` rejects a
    # second INSERT on the same (agent_id, phase, attempt) three-tuple.
    # The failure path can legitimately be reached with an existing
    # phase_outputs row (e.g. awaiting_ci observed ``ci_red`` then the
    # reaper fires on an unrecoverable branch). The ``|| true`` below
    # masked the crash already, but without ON CONFLICT the failure
    # payload simply wasn't persisted — operators lost the reaper's
    # category + reason context on re-entry.
    #
    # #3488 — use psql -v variable substitution rather than bash
    # interpolation so that $_reason (the head -c 200 of a stderr tail)
    # never lands in a bash command line. The previous implementation
    # built JSON via string interpolation after a sed single-quote
    # escape, which still allowed $() / backticks / $VAR in $_reason to
    # be expanded by bash before psql saw the SQL.
    # Fix: lift $_category and $_reason into psql -v vars; build the
    # JSON via jsonb_build_object(:'category'::text, :'reason'::text)
    # so the values reach Postgres as typed parameters with no further
    # bash quoting. Schema parity enforced by
    # scripts/tests/test_phase_outputs_insert_shape.py
    set +e
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
        -v agent_id="$AGENT_ID" \
        -v term_phase="$_term_phase" \
        -v category="$_category" \
        -v reason="$_reason" <<'EOF' >/dev/null 2>&1
INSERT INTO dispatcher.phase_outputs
    (agent_id, phase, output_json)
VALUES
    (:'agent_id', :'term_phase',
     jsonb_build_object('category', :'category'::text, 'reason', :'reason'::text))
ON CONFLICT (agent_id, phase, attempt) DO UPDATE
  SET output_json = EXCLUDED.output_json,
      ts = now();
EOF
    set -e
    # #3822 — same shape as ``advance_phase``'s terminal write: stamp
    # ``ended_at`` so the admin cockpit's "Recently Completed" query
    # surfaces this row. ``COALESCE(ended_at, now())`` is idempotent +
    # race-safe with the daemon's housekeeping bulk backfill.
    db_exec "UPDATE dispatcher.agents
                SET phase = '$_term_phase',
                    status = 'failed',
                    ended_at = COALESCE(ended_at, now())
              WHERE agent_id = '$AGENT_ID';"
    log "phase_advanced" "next_phase=$_term_phase" "status=failed"
    # #3494 — exit unconditionally after marking the agent terminal in
    # DB. This closes the dispatch loop: the while-loop re-reads the
    # phase each iteration, and without this exit the loop would see the
    # new phase_unknown / ralph_not_ship / *_transition_unrecognized
    # value, find no matching case arm, fall to `*)`, and re-emit
    # phase_unknown for up to MAX_PHASE_ITERATIONS (default 40) before
    # the safety cap fires. With exit 0 the process terminates cleanly
    # after the first call — exactly one reaped_failure log line.
    exit 0
}

# handle_ralph_not_ship_local was removed in #3586. ``ralph_not_ship``
# is now routed through the diagnoser via the descriptive-terminal arm
# in the post-claude dispatch (see BYPASSED_TERMINAL_PHASES_TO_ROUTE).
# The local handler was defined in #3455 and handled by posting a
# comment + adding status/blocked; those actions are now the diagnoser's
# responsibility based on block_reason analysis.

fetch_pr_status() {
    # $1 = pr_number. Writes the JSON response from
    # ``gh pr view <N> --json statusCheckRollup,mergeable,
    # mergeStateStatus,headRefOid,mergeCommit`` to
    # ``$AGENT_WORKSPACE/pr-status.json`` and returns 0 on success,
    # non-zero on gh failure. The caller reads the file.
    _pr_num="$1"
    _out_file="$AGENT_WORKSPACE/pr-status.json"
    set +e
    gh pr view "$_pr_num" \
        --repo judgemind/judgemind \
        --json statusCheckRollup,mergeable,mergeStateStatus,headRefOid,mergeCommit \
        > "$_out_file" \
        2> "$AGENT_WORKSPACE/gh-pr-view.stderr.log"
    _gh_rc=$?
    set -e
    return $_gh_rc
}

handle_awaiting_ci() {
    # Poll the PR's combined check rollup every CI_POLL_INTERVAL
    # seconds until it classifies as green/red or the hard timeout
    # elapses.
    #
    # Outcomes:
    #   green → advance to ``merge`` (caller transitions).
    #   red   → advance to ``fix_ci`` (Stage 2 will wire the fix-ci
    #           skill; for now the dispatch case lets it route to the
    #           claude-driven branch naturally).
    #   timeout → agent_runner_reaped_failure + terminal
    #             ``awaiting_ci_timeout``, agent exits.
    log "awaiting_ci_begin"
    _pr_number=$(read_pr_number)
    log "awaiting_ci_pr_number_read" "pr_number=$_pr_number"
    if [[ -z "$_pr_number" ]]; then
        agent_runner_reaped_failure "awaiting_ci_failed" "missing_pr" "pr_number NULL on agent row"
        printf '{"missing_pr": true}'
        return 0
    fi

    _start_ts=$(date -u +%s)
    _last_state=""
    _poll_count=0
    _max_polls="${AGENT_RUNNER_CI_MAX_POLLS:-100}"
    while true; do
        _poll_count=$((_poll_count + 1))
        if [[ "$_poll_count" -gt "$_max_polls" ]]; then
            log "awaiting_ci_max_polls_hit" "pr_number=$_pr_number" "poll_count=$_poll_count"
            agent_runner_reaped_failure \
                "awaiting_ci_timeout" \
                "ci_max_polls" \
                "pr=$_pr_number poll_count=$_poll_count last_state=$_last_state"
            printf '{"timeout": true, "pr_number": %s, "max_polls": %s}' \
                "$_pr_number" "$_poll_count"
            return 0
        fi
        if fetch_pr_status "$_pr_number"; then
            # #3200 early-exit — after the PR is merged, ``mergeable``
            # and ``mergeStateStatus`` both flip to ``UNKNOWN`` while
            # ``mergeCommit.oid`` is populated with the squash SHA. The
            # rollup classifier cannot distinguish "pending + UNKNOWN"
            # from "merged + UNKNOWN" from the rollup alone, so short-
            # circuit the poll on merge-commit presence.
            _merge_oid_check=$(jq -r '.mergeCommit.oid // ""' \
                "$AGENT_WORKSPACE/pr-status.json" 2>/dev/null || printf '')
            if [[ -n "$_merge_oid_check" ]]; then
                log "awaiting_ci_already_merged" \
                    "pr_number=$_pr_number" \
                    "merge_sha=$_merge_oid_check" \
                    "rollup_state=green"
                printf '{"rollup_state": "green", "pr_number": %s, "already_merged": true}' \
                    "$_pr_number"
                return 0
            fi
            _state=$(classify_pr_rollup "$AGENT_WORKSPACE/pr-status.json")
            _fetch_rc=0
        else
            _state="error"
            _fetch_rc=1
        fi
        log "awaiting_ci_poll" "pr_number=$_pr_number" "rollup_state=$_state" "fetch_rc=$_fetch_rc"
        if [[ "$_state" == "green" ]]; then
            printf '{"rollup_state": "green", "pr_number": %s}' "$_pr_number"
            return 0
        fi
        if [[ "$_state" == "red" ]]; then
            printf '{"rollup_state": "red", "pr_number": %s}' "$_pr_number"
            return 0
        fi
        # pending / error — keep polling until timeout.
        _last_state="$_state"
        _now_ts=$(date -u +%s)
        _elapsed=$((_now_ts - _start_ts))
        if [[ "$_elapsed" -ge "$AWAITING_CI_TIMEOUT_SECONDS" ]]; then
            log "awaiting_ci_timeout" \
                "pr_number=$_pr_number" \
                "elapsed_s=$_elapsed" \
                "last_state=$_last_state"
            agent_runner_reaped_failure \
                "awaiting_ci_timeout" \
                "ci_timeout" \
                "elapsed_s=$_elapsed last_state=$_last_state pr=$_pr_number"
            printf '{"timeout": true, "pr_number": %s, "elapsed_s": %s}' \
                "$_pr_number" "$_elapsed"
            return 0
        fi
        sleep "$CI_POLL_INTERVAL"
    done
}

extract_merge_sha() {
    # Pull ``mergeCommit.oid`` from the current pr-status.json. Empty
    # string if absent.
    _status_file="$AGENT_WORKSPACE/pr-status.json"
    if [[ ! -s "$_status_file" ]]; then
        printf ''
        return 0
    fi
    jq -r '.mergeCommit.oid // ""' "$_status_file" 2>/dev/null || printf ''
}

read_merge_unstick_attempts() {
    # Column was added by the daemon's migration for #2641. Returns 0
    # if the column is missing (older dev DB snapshots).
    _val=$(db_query_one "SELECT COALESCE(merge_unstick_attempts, 0)
                            FROM dispatcher.agents
                           WHERE agent_id = '$AGENT_ID'
                           LIMIT 1;" 2>/dev/null || printf '')
    if [[ -z "$_val" ]] || ! [[ "$_val" =~ ^[0-9]+$ ]]; then
        printf '0'
    else
        printf '%s' "$_val"
    fi
}

increment_merge_unstick_attempts() {
    # Best-effort; a lost increment at most burns one extra unstick
    # before the daemon's ``FAILURE_CATEGORY_MERGE_UNSTICK_EXHAUSTED``
    # path classifies the agent terminal.
    db_exec "UPDATE dispatcher.agents
                SET merge_unstick_attempts = COALESCE(merge_unstick_attempts, 0) + 1
              WHERE agent_id = '$AGENT_ID';" \
        >/dev/null 2>&1 || true
}

try_auto_unstick_merge() {
    # Port of ``_try_auto_unstick_merge`` in daemon.py. Push an empty
    # commit to the PR branch so GitHub re-evaluates the
    # statusCheckRollup on a fresh SHA. Budget = MERGE_UNSTICK_MAX_
    # ATTEMPTS. On exhaustion, route through agent_runner_reaped_
    # failure and return 1 so the caller knows not to retry merge.
    _pr_num="$1"
    _attempts=$(read_merge_unstick_attempts)
    log "merge_stale_rollup_detected" \
        "pr_number=$_pr_num" \
        "attempts_so_far=$_attempts"
    if [[ "$_attempts" -ge "$MERGE_UNSTICK_MAX_ATTEMPTS" ]]; then
        log "merge_unstick_exhausted" \
            "pr_number=$_pr_num" \
            "attempts_so_far=$_attempts"
        agent_runner_reaped_failure \
            "merge_failed" \
            "merge_unstick_exhausted" \
            "pr=$_pr_num attempts=$_attempts"
        return 1
    fi
    log "merge_unstick_empty_commit_begin"
    set +e
    git -C "$REPO_ROOT" commit --allow-empty \
        -m "ci: force fresh rollup evaluation (stale-ci-passed unstick, #2641)" \
        > "$AGENT_WORKSPACE/merge-unstick-commit.stdout.log" \
        2> "$AGENT_WORKSPACE/merge-unstick-commit.stderr.log"
    _commit_rc=$?
    set -e
    if [[ "$_commit_rc" -ne 0 ]]; then
        log "merge_unstick_commit_failed" "exit_code=$_commit_rc"
        agent_runner_reaped_failure \
            "merge_failed" \
            "merge_unstick_exhausted" \
            "pr=$_pr_num empty_commit_nonzero_exit=$_commit_rc"
        return 1
    fi
    set +e
    git -C "$REPO_ROOT" push origin "$BRANCH_NAME" --no-verify \
        > "$AGENT_WORKSPACE/merge-unstick-push.stdout.log" \
        2> "$AGENT_WORKSPACE/merge-unstick-push.stderr.log"
    _push_rc=$?
    set -e
    if [[ "$_push_rc" -ne 0 ]]; then
        log "merge_unstick_push_failed" "exit_code=$_push_rc"
        agent_runner_reaped_failure \
            "merge_failed" \
            "merge_unstick_exhausted" \
            "pr=$_pr_num empty_commit_push_nonzero_exit=$_push_rc"
        return 1
    fi
    increment_merge_unstick_attempts
    log "merge_auto_unstick_empty_commit_pushed" \
        "pr_number=$_pr_num" \
        "new_attempts=$((_attempts + 1))"
    return 0
}

close_issue_post_merge() {
    # Issue #3411 — belt-and-suspenders post-merge issue cleanup.
    # Mirrors daemon.py's ``_close_issue_post_merge``. If the PR body
    # lacked a ``Closes #N`` keyword (or GitHub failed to auto-close
    # for any other reason), close the originating issue + strip
    # ``agent/ready`` so it doesn't reappear in the daemon's queue
    # scan. The PR-body validation in handle_push_and_pr makes this
    # branch rare, but the keep-the-queue-clean invariant matters
    # more than the cost of an extra ``gh issue view`` per merge.
    #
    # Args: $1 = issue_number, $2 = pr_number.
    # Best-effort — every failure is logged but does not propagate.
    _issue_num="$1"
    _pr_num="$2"
    if [[ -z "$_issue_num" ]]; then
        log "post_merge_cleanup_skipped" "reason=no_issue_number"
        return 0
    fi
    # Probe issue state. Write stdout to a file so we can capture the
    # exit code distinctly from the substitution's exit code (a
    # ``$( ... || printf '' )`` pattern always exits 0 regardless of
    # whether gh succeeded — losing the probe failure signal).
    set +e
    gh issue view "$_issue_num" \
        --repo judgemind/judgemind \
        --json state \
        --jq '.state' \
        > "$AGENT_WORKSPACE/gh-issue-state.stdout.log" \
        2> "$AGENT_WORKSPACE/gh-issue-state.stderr.log"
    _probe_rc=$?
    set -e
    _state=""
    if [[ -s "$AGENT_WORKSPACE/gh-issue-state.stdout.log" ]]; then
        _state=$(tr -d '\n\r' < "$AGENT_WORKSPACE/gh-issue-state.stdout.log")
    fi
    if [[ "$_probe_rc" -ne 0 || -z "$_state" ]]; then
        log "post_merge_cleanup_probe_failed" \
            "issue_number=$_issue_num" "exit_code=$_probe_rc"
        return 0
    fi
    # ``gh issue view --json state`` returns ``OPEN`` or ``CLOSED``.
    # Compare case-insensitively for safety.
    _state_upper=$(printf '%s' "$_state" | tr '[:lower:]' '[:upper:]')
    if [[ "$_state_upper" != "OPEN" ]]; then
        log "post_merge_cleanup_skipped" \
            "issue_number=$_issue_num" "reason=already_closed" "state=$_state"
        return 0
    fi
    log "post_merge_cleanup_begin" "issue_number=$_issue_num" "pr_number=$_pr_num"
    # Close the issue with a comment naming the PR + cleanup origin.
    _close_comment="Closed by PR #${_pr_num} (autonomous post-merge cleanup, see #3411 — PR body did not contain \`Closes #${_issue_num}\` keyword so GitHub didn't auto-close)."
    set +e
    gh issue close "$_issue_num" \
        --repo judgemind/judgemind \
        --reason completed \
        --comment "$_close_comment" \
        > "$AGENT_WORKSPACE/gh-issue-close.stdout.log" \
        2> "$AGENT_WORKSPACE/gh-issue-close.stderr.log"
    _close_rc=$?
    set -e
    if [[ "$_close_rc" -ne 0 ]]; then
        log "post_merge_cleanup_close_failed" \
            "issue_number=$_issue_num" "exit_code=$_close_rc"
        # Continue to label-strip even if close failed — the label is
        # the queue-visible signal; close is for the operator UI.
    fi
    # Strip agent/ready so the issue is fully off the queue.
    set +e
    gh issue edit "$_issue_num" \
        --repo judgemind/judgemind \
        --remove-label agent/ready \
        > "$AGENT_WORKSPACE/gh-issue-remove-label.stdout.log" \
        2> "$AGENT_WORKSPACE/gh-issue-remove-label.stderr.log"
    _label_rc=$?
    set -e
    if [[ "$_label_rc" -ne 0 ]]; then
        log "post_merge_cleanup_label_strip_failed" \
            "issue_number=$_issue_num" "exit_code=$_label_rc"
    fi
    log "post_merge_cleanup_done" "issue_number=$_issue_num" "pr_number=$_pr_num"
    return 0
}

handle_merge() {
    # Squash-merge the PR with branch-delete. On stale-rollup stderr,
    # attempt one auto-unstick (push empty commit, go back to
    # awaiting_ci); on any other non-zero, route to
    # agent_runner_reaped_failure. On success, record merge SHA and
    # advance to awaiting_deploy.

    _pr_number=$(read_pr_number)
    if [[ -z "$_pr_number" ]]; then
        agent_runner_reaped_failure "merge_failed" "missing_pr" "pr_number NULL on agent row"
        printf '{"missing_pr": true}'
        return 0
    fi

    log "merge_begin" "pr_number=$_pr_number"
    set +e
    gh pr merge "$_pr_number" \
        --repo judgemind/judgemind \
        --squash \
        --delete-branch \
        > "$AGENT_WORKSPACE/gh-pr-merge.stdout.log" \
        2> "$AGENT_WORKSPACE/gh-pr-merge.stderr.log"
    _merge_rc=$?
    set -e
    log "merge_done" "pr_number=$_pr_number" "exit_code=$_merge_rc"

    if [[ "$_merge_rc" -eq 0 ]]; then
        # Success — re-fetch PR status to extract the merge SHA.
        if fetch_pr_status "$_pr_number"; then
            _merge_sha=$(extract_merge_sha)
        else
            _merge_sha=""
        fi
        log "merge_succeeded" "pr_number=$_pr_number" "merge_sha=$_merge_sha"
        # ── #3411: post-merge issue cleanup (belt & suspenders) ───────
        # Mirror daemon.py's ``_close_issue_post_merge``. If the PR
        # body lacked a ``Closes #N`` keyword (or GitHub failed to
        # auto-close for any other reason), close the issue + strip
        # ``agent/ready`` so it doesn't reappear in the daemon's
        # ready-queue scan. The PR-body validation in handle_push_and_pr
        # makes this a rare branch, but the keep-the-queue-clean
        # invariant is more important than minimizing redundant gh
        # calls.
        close_issue_post_merge "$ISSUE_NUMBER" "$_pr_number"
        printf '{"merged": true, "pr_number": %s, "merge_sha": "%s"}' \
            "$_pr_number" "$_merge_sha"
        return 0
    fi

    # Non-zero. Inspect stderr for the stale-rollup marker.
    _stderr_text=""
    if [[ -s "$AGENT_WORKSPACE/gh-pr-merge.stderr.log" ]]; then
        _stderr_text=$(cat "$AGENT_WORKSPACE/gh-pr-merge.stderr.log" 2>/dev/null || printf '')
    fi
    # Case-insensitive match on the stale-rollup marker (gh output
    # preserves case, but we compare lowercase for safety).
    _stderr_lower=$(printf '%s' "$_stderr_text" | tr '[:upper:]' '[:lower:]')
    if [[ "$_stderr_lower" == *"$STALE_ROLLUP_MARKER"* ]]; then
        if try_auto_unstick_merge "$_pr_number"; then
            # Budget left — go back to awaiting_ci for the next supervisor
            # tick equivalent. Since we're inside one agent-runner
            # process, advance the phase via the dispatch case's
            # ``auto_unstick_retry`` signal.
            printf '{"auto_unstick_retry": true, "pr_number": %s}' "$_pr_number"
            return 0
        fi
        # try_auto_unstick_merge already called agent_runner_reaped_failure.
        printf '{"merge_failed": true, "pr_number": %s}' "$_pr_number"
        return 0
    fi

    # Any other non-zero exit — route to failure.
    _stderr_tail=$(printf '%s' "$_stderr_text" | head -c 200 | tr '\n' ' ')
    agent_runner_reaped_failure \
        "merge_failed" \
        "merge_nonzero_exit" \
        "pr=$_pr_number exit=$_merge_rc stderr=$_stderr_tail"
    printf '{"merge_failed": true, "pr_number": %s, "exit_code": %s}' \
        "$_pr_number" "$_merge_rc"
}

find_deploy_runs() {
    # $1 = merge SHA. Fetches ``gh run list --commit <sha>`` (no
    # ``--workflow`` flags — see note below) and post-filters the JSON
    # via ``jq`` to keep only entries whose ``workflowName`` is in
    # ``$DEPLOY_WORKFLOW_NAMES_BASH`` AND whose ``headSha`` equals the
    # merge SHA. Writes the filtered array to
    # ``$AGENT_WORKSPACE/deploy-runs.json``. On failure writes ``[]``.
    #
    # NOTE (#3514): multi-``--workflow`` is intentionally NOT used here.
    # ``gh run list --workflow`` is single-valued; when multiple ``-w``
    # flags are passed, only the last value is honoured and all other
    # workflows are silently dropped from the result. This caused the
    # awaiting_deploy false-positive in PR #3509 where Deploy Dispatcher
    # was excluded from the match set while still in_progress. We
    # instead fetch all runs for the commit and post-filter by display
    # name, mirroring daemon.py's ``_find_deploy_runs``.
    _sha="$1"
    _out_file="$AGENT_WORKSPACE/deploy-runs.json"
    _raw_file="$AGENT_WORKSPACE/deploy-runs-raw.json"
    set +e
    gh run list \
        --repo judgemind/judgemind \
        --commit "$_sha" \
        --json databaseId,workflowName,status,conclusion,createdAt,headSha \
        --limit 20 \
        > "$_raw_file" \
        2> "$AGENT_WORKSPACE/gh-run-list.stderr.log"
    _gh_rc=$?
    set -e
    if [[ "$_gh_rc" -ne 0 ]]; then
        printf '[]' > "$_out_file"
        return 0
    fi
    # Post-filter: keep only entries whose workflowName is in the deploy
    # names list and whose headSha matches the merge SHA (defensive
    # guard — ``--commit`` already filters server-side, but gh may
    # return runs for nearby commits under edge-case pagination).
    _names_pipe="$DEPLOY_WORKFLOW_NAMES_BASH"
    jq --arg sha "$_sha" --arg names_pipe "$_names_pipe" \
        '[.[] | select(
            (.workflowName as $n | ($names_pipe | split("|") | index($n)) != null)
            and .headSha == $sha
        )]' \
        "$_raw_file" > "$_out_file" 2>/dev/null \
        || printf '[]' > "$_out_file"
    return 0
}

handle_awaiting_deploy() {
    # Poll the deploy-workflow runs for the merge SHA until at least
    # one completes successfully, any fails, or the hard timeout
    # elapses. If no deploy runs are observed within the short grace
    # window, treat the PR as "no deploy applicable" (e.g. docs-only)
    # and advance to verify. Mirrors daemon.py's
    # ``_advance_awaiting_deploy``.
    _pr_number=$(read_pr_number)
    if [[ -z "$_pr_number" ]]; then
        agent_runner_reaped_failure "awaiting_deploy_failed" "missing_pr" \
            "pr_number NULL on agent row"
        printf '{"missing_pr": true}'
        return 0
    fi

    # Re-fetch PR status once to get the merge SHA.
    if ! fetch_pr_status "$_pr_number"; then
        # Treat as a transient GH failure — let the next supervisor
        # tick equivalent try again. Since we're inside one process,
        # sleep and retry the outer poll loop.
        log "awaiting_deploy_pr_view_failed" "pr_number=$_pr_number"
    fi
    _merge_sha=$(extract_merge_sha)
    if [[ -z "$_merge_sha" ]]; then
        # A non-merged or still-propagating PR — treat as pending and
        # poll-retry up to timeout.
        log "awaiting_deploy_no_merge_sha" "pr_number=$_pr_number"
    fi

    _start_ts=$(date -u +%s)
    _last_state=""
    _poll_count=0
    _max_polls="${AGENT_RUNNER_DEPLOY_MAX_POLLS:-60}"
    while true; do
        _poll_count=$((_poll_count + 1))
        if [[ "$_poll_count" -gt "$_max_polls" ]]; then
            log "awaiting_deploy_max_polls_hit" "pr_number=$_pr_number" "poll_count=$_poll_count"
            printf '{"timeout": true, "pr_number": %s, "max_polls": %s}' \
                "$_pr_number" "$_poll_count"
            agent_runner_reaped_failure \
                "awaiting_deploy_timeout" \
                "deploy_max_polls" \
                "pr=$_pr_number poll_count=$_poll_count last_state=$_last_state"
            return 0
        fi
        _now_poll_ts=$(date -u +%s)
        _elapsed_poll=$((_now_poll_ts - _start_ts))
        if [[ -n "$_merge_sha" ]]; then
            find_deploy_runs "$_merge_sha"
            _state=$(classify_deploy_runs "$AGENT_WORKSPACE/deploy-runs.json")
            # Per-poll structured detail log: one line per matched run
            # plus a summary. Visible in CloudWatch as awaiting_deploy_poll_detail.
            # Satisfies AC#5 (instrumentation: merge_sha, matched_run_count,
            # run_id, run_head_sha, run_status, run_conclusion, elapsed_seconds).
            _matched_count=$(jq 'if type == "array" then length else 0 end' \
                "$AGENT_WORKSPACE/deploy-runs.json" 2>/dev/null || printf '0')
            log "awaiting_deploy_poll_detail" \
                "pr_number=$_pr_number" \
                "merge_sha=$_merge_sha" \
                "deploy_state=$_state" \
                "matched_run_count=$_matched_count" \
                "elapsed_seconds=$_elapsed_poll"
            # Log individual run details for observability.
            if [[ -s "$AGENT_WORKSPACE/deploy-runs.json" ]]; then
                jq -r '.[] | "run_id=\(.databaseId) run_head_sha=\(.headSha // "?") run_status=\(.status // "?") run_conclusion=\(.conclusion // "?") workflow=\(.workflowName // "?")"' \
                    "$AGENT_WORKSPACE/deploy-runs.json" 2>/dev/null \
                    | while IFS= read -r _run_line; do
                        log "awaiting_deploy_run_detail" "pr_number=$_pr_number" "$_run_line"
                    done
            fi
        else
            _state="pending"
        fi
        log "awaiting_deploy_poll" \
            "pr_number=$_pr_number" \
            "merge_sha=$_merge_sha" \
            "deploy_state=$_state"
        if [[ "$_state" == "success" ]]; then
            printf '{"deploy_state": "success", "merge_sha": "%s"}' "$_merge_sha"
            return 0
        fi
        if [[ "$_state" == "failure" ]]; then
            agent_runner_reaped_failure \
                "awaiting_deploy_failed" \
                "deploy_failed" \
                "pr=$_pr_number sha=$_merge_sha"
            printf '{"deploy_state": "failure", "merge_sha": "%s"}' "$_merge_sha"
            return 0
        fi
        _now_ts=$(date -u +%s)
        _elapsed=$((_now_ts - _start_ts))
        # "No deploy runs" branch: once we're past the grace window
        # and no matching run has appeared, treat as "no deploy
        # applicable" and advance to verify.
        if [[ "$_state" == "none" ]] && [[ "$_elapsed" -ge "$DEPLOY_GRACE_SECONDS" ]]; then
            log "awaiting_deploy_no_runs" \
                "pr_number=$_pr_number" \
                "merge_sha=$_merge_sha" \
                "grace_s=$DEPLOY_GRACE_SECONDS" \
                "elapsed_s=$_elapsed"
            printf '{"deploy_state": "none", "merge_sha": "%s", "elapsed_s": %s}' \
                "$_merge_sha" "$_elapsed"
            return 0
        fi
        _last_state="$_state"
        if [[ "$_elapsed" -ge "$AWAITING_DEPLOY_TIMEOUT_SECONDS" ]]; then
            log "awaiting_deploy_timeout" \
                "pr_number=$_pr_number" \
                "merge_sha=$_merge_sha" \
                "elapsed_s=$_elapsed" \
                "last_state=$_last_state"
            printf '{"timeout": true, "merge_sha": "%s", "elapsed_s": %s}' \
                "$_merge_sha" "$_elapsed"
            agent_runner_reaped_failure \
                "awaiting_deploy_timeout" \
                "deploy_timeout" \
                "pr=$_pr_number sha=$_merge_sha elapsed_s=$_elapsed"
            return 0
        fi
        sleep "$DEPLOY_POLL_INTERVAL"
        # Refresh merge SHA if we missed it the first time around.
        if [[ -z "$_merge_sha" ]]; then
            if fetch_pr_status "$_pr_number"; then
                _merge_sha=$(extract_merge_sha)
            fi
        fi
    done
}

# Check whether a phase is terminal per the shared Python module.
# Uses a sibling one-line shim written alongside the transition shim
# rather than an inline `python -c` heredoc (the preflight hook blocks
# multi-line `python -c` on operator shells).
TERMINAL_SHIM="${AGENT_RUNNER_TERMINAL_SHIM:-$AGENT_WORKSPACE/phase_terminal_shim.py}"
# Issue #3166: sibling shim for agent STATUS terminal check.
# Mirrors TERMINAL_SHIM / is_terminal() above but queries TERMINAL_STATUSES
# (agent-level) via is_terminal_status() instead of TERMINAL_PHASES.
STATUS_TERMINAL_SHIM="${AGENT_RUNNER_STATUS_TERMINAL_SHIM:-$AGENT_WORKSPACE/phase_status_terminal_shim.py}"

# #4137: TERMINAL_SHIM + STATUS_TERMINAL_SHIM stamping wrapped in
# ``_setup_terminal_shims`` so they run from ``main()``.
_setup_terminal_shims() {
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

    if [[ "$STATUS_TERMINAL_SHIM" == "$AGENT_WORKSPACE/phase_status_terminal_shim.py" ]]; then
    cat <<'STERMEOF' > "$STATUS_TERMINAL_SHIM"
"""Print 'yes' or 'no' for whether the agent status on stdin is terminal."""
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

status = sys.stdin.read().strip()
sys.stdout.write("yes" if pt.is_terminal_status(status) else "no")
STERMEOF
fi
}

is_terminal() {
    _phase="$1"
    _result=$(printf '%s' "$_phase" | python3 "$TERMINAL_SHIM" 2>/dev/null || printf 'unknown')
    [[ "$_result" == "yes" ]]
}

is_terminal_status() {
    _status="$1"
    _result=$(printf '%s' "$_status" | python3 "$STATUS_TERMINAL_SHIM" 2>/dev/null || printf 'unknown')
    [[ "$_result" == "yes" ]]
}

# ── Test hook: run the ralph HEAD-watcher standalone (#3144) ---------------
#
# The HEAD-watcher tests need to drive start_ralph_head_watcher +
# stop_ralph_head_watcher without spinning the whole phase loop. When
# AGENT_RUNNER_WATCHER_TEST_MODE=1, run:
#
#   start_ralph_head_watcher
#   (optional) cp $AGENT_RUNNER_WATCHER_TEST_SEED_COMMITS → git stub's
#              RALPH_HEAD_WATCHER_COMMITS_FILE — lets the test inject
#              "new commits after baseline" without the baseline
#              capturing them up front. The baseline reads an empty
#              commits file, the tick reads the seeded one.
#   sleep $AGENT_RUNNER_WATCHER_TEST_SLEEP (default 2)
#   stop_ralph_head_watcher
#
# …and exit 0. The test harness preseeds the git + psql stubs and
# inspects the invocation logs + log events post-exit.
#
# #4137: wrapped in ``_maybe_run_watcher_test_mode`` so it runs from
# ``main()``. The ``exit 0`` branch still terminates the whole shell
# (bash ``exit`` is process-level, not function-level), preserving the
# test contract.
_maybe_run_watcher_test_mode() {
    if [[ "${AGENT_RUNNER_WATCHER_TEST_MODE:-0}" == "1" ]]; then
        log "watcher_test_mode_begin"
        start_ralph_head_watcher
        # Give the subshell a moment to take its baseline snapshot (an
        # empty origin/main..HEAD, since the watcher starts before ralph
        # has committed anything). Then seed commits so the NEXT tick
        # sees them as new.
        sleep 0.2 2>/dev/null || sleep 1
        if [[ -n "${AGENT_RUNNER_WATCHER_TEST_SEED_COMMITS:-}" \
                && -f "$AGENT_RUNNER_WATCHER_TEST_SEED_COMMITS" \
                && -n "${RALPH_HEAD_WATCHER_COMMITS_FILE:-}" ]]; then
            cp "$AGENT_RUNNER_WATCHER_TEST_SEED_COMMITS" "$RALPH_HEAD_WATCHER_COMMITS_FILE"
            log "watcher_test_mode_seeded" "src=$AGENT_RUNNER_WATCHER_TEST_SEED_COMMITS"
        fi
        sleep "${AGENT_RUNNER_WATCHER_TEST_SLEEP:-2}"
        stop_ralph_head_watcher
        log "watcher_test_mode_end"
        exit 0
    fi
}

# ── START_PHASE row update (#3366) ─────────────────────────────────────────
#
# Validation already ran early (before clone). Here, on the path that
# made it through the clone + branch + ralph-patch stages, write the
# resume phase to the agent row defensively. The daemon's directive
# consumer already ran an equivalent UPDATE, but a respawn can race
# the row-update on a stale-DB-cache view, and an operator hand-
# launching the task with START_PHASE=foo expects the agent row to
# reflect foo. Idempotent — UPDATE only when the row's phase doesn't
# already match.

# #4137: START_PHASE row update wrapped in
# ``_apply_start_phase_row_update`` so it runs from ``main()``.
_apply_start_phase_row_update() {
    if [[ -n "${START_PHASE:-}" ]]; then
        db_exec "UPDATE dispatcher.agents
                    SET phase = '$START_PHASE'
                  WHERE agent_id = '$AGENT_ID'
                    AND phase IS DISTINCT FROM '$START_PHASE';" \
            > "$AGENT_WORKSPACE/start-phase-update.stdout.log" \
            2> "$AGENT_WORKSPACE/start-phase-update.stderr.log" \
            || true
    fi
}

# ── Main phase loop ---------------------------------------------------------
#
# Safety cap on iterations — if a bug causes the phase to advance to
# itself forever the container would run indefinitely (Fargate has no
# per-task timeout). 40 phase transitions is well above the happy-path
# depth (~10) and any realistic fix-ci loop.
#
# #4137: wrapped in ``phase_loop()`` so the entrypoint is sourceable
# without spinning the loop. ``main()`` calls ``phase_loop`` after setup.
# Behavior parity: ``exit N`` from inside the loop still terminates the
# whole shell (bash semantics for `exit` from a function), and `set -e`
# inherits from the caller (main()) so the loop's error-on-failure
# behavior is preserved.

MAX_PHASE_ITERATIONS="${AGENT_RUNNER_MAX_PHASE_ITERATIONS:-40}"

phase_loop() {
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

    # Issue #3166: observe external-terminal status written by a diagnoser,
    # supervisor, or killswitch before running the next phase handler.
    # If terminal, exit 0 immediately — do NOT call mark_ended, because the
    # external writer already owns the row's ended_at/status and we must not
    # race it.  Same semantics as _check_killswitch_and_abort.
    _current_status=$(read_agent_status)
    if is_terminal_status "$_current_status"; then
        log "external_terminal_observed" "status=$_current_status" "after_phase=$_current"
        exit 0
    fi

    log "phase_loop_tick" "iteration=$_iter" "current_phase=$_current"

    if is_terminal "$_current"; then
        log "phase_terminal" "phase=$_current"
        mark_ended
        exit 0
    fi

    # Issue #3374: synthetic scheduled-skill agents (kind='scheduled_skill')
    # don't go through the plan→ralph→summary→PR pipeline. The phase
    # column on these agents is the skill name (e.g. 'audit', 'spotcheck')
    # — dispatch directly to ``claude -p /<phase> <agent_id>`` via
    # ``handle_scheduled_skill`` and let it advance the agent row
    # to a terminal phase. The next loop iteration observes the new
    # phase and exits cleanly.
    _agent_kind=$(read_agent_kind)
    if [[ "$_agent_kind" == "scheduled_skill" ]]; then
        # Defensive: if the phase column already terminal-looking, the
        # is_terminal check above already returned. Anything else here
        # is a live skill name.
        handle_scheduled_skill "$_current"
        continue
    fi

    # Phases the Stage 1b entrypoint actually runs. Each case writes
    # its phase_output, computes the transition, advances, and loops.
    #
    # Claude-driven phases (#3117): only the phases that map to a real
    # `task-v2-*` skill belong here. `claiming` and `push_and_pr` are
    # mechanical — they were incorrectly included in the original
    # Stage 1b dispatch, which called `/task-v2-claiming` /
    # `/task-v2-push_and_pr` and got "Unknown command" back.
    # #4138 — per-phase case-arm bodies extracted to the sourced
    # handler file (see the source line earlier in this script). Each
    # ``handle_*`` (or ``handle_*_arm``) function owns its own pre/post
    # bookkeeping —
    # ``persist_phase_output``, ``transition_for``,
    # ``dispatch_transition_action``, ``advance_phase``, and
    # ``agent_runner_reaped_failure`` — so this case is now a thin
    # dispatcher: one function call per phase. The original ralph
    # baseline-rebase short-circuit converts its inner ``continue``
    # to a function ``return 0`` (after ``dispatch_transition_action``
    # has already advanced the phase row, the next loop tick observes
    # the new phase — exact same control flow as ``continue``).
    case "$_current" in
        planning)
            handle_planning
            ;;
        ralph)
            handle_ralph
            ;;
        summary)
            handle_summary
            ;;
        verify)
            handle_verify
            ;;
        claiming)
            handle_claiming
            ;;
        push_and_pr)
            handle_push_and_pr_arm
            ;;
        fix_conflict)
            # #4138 transitional shape: ``T51`` in
            # ``scripts/tests/test_agent_runner_entrypoint.sh`` extracts
            # this arm body verbatim via awk and sources it into a stubbed
            # subshell that does NOT define ``handle_fix_conflict_arm``.
            # Rather than edit the test (#4138 AC #5 forbids test-file
            # edits), the body is kept inline here so T51 keeps passing on
            # bash 3.2. ``handle_fix_conflict_arm`` is still defined in
            # the sourced handlers file and IS the canonical
            # implementation — it receives the same call sequence (same
            # events, same outputs) as this inline copy. Sub-task C will
            # convert T51 to call ``handle_fix_conflict_arm`` directly,
            # at which point this inline arm body collapses to the
            # one-line dispatcher form used by every other arm.
            _output=$(handle_fix_conflict)
            _fc_verdict=$(printf '%s' "$_output" | jq -r '.verdict // ""' 2>/dev/null || printf '')
            log "fix_conflict_handler_done" "verdict=$_fc_verdict"
            persist_phase_output "fix_conflict" "$_output"
            log "fix_conflict_persist_done"
            _transition=$(transition_for "fix_conflict" "$_output")
            _action=$(printf '%s' "$_transition" | cut -f1)
            _next=$(printf '%s' "$_transition" | cut -f2)
            _status=$(printf '%s' "$_transition" | cut -f3)
            _hint=$(printf '%s' "$_transition" | cut -f4)
            log "fix_conflict_transition_shim_done" \
                "action=$_action" \
                "next=$_next" \
                "status=$_status" \
                "hint=$_hint"
            log "fix_conflict_dispatched_action" "action=$_action"
            dispatch_transition_action "fix_conflict" "$_action" "$_next" "$_status" "$_hint" "$_output"
            ;;
        fix_ci)
            handle_fix_ci_arm
            ;;
        operational)
            handle_operational
            ;;
        awaiting_ci)
            handle_awaiting_ci_arm
            ;;
        merge)
            handle_merge_arm
            ;;
        awaiting_deploy)
            handle_awaiting_deploy_arm
            ;;
        retro|setup)
            handle_retro_or_setup "$_current"
            ;;
        *)
            handle_unknown_phase "$_current"
            ;;
    esac
    done
}

# ── main() and sourcing guard (#4137) ──────────────────────────────────────
#
# All top-level executable side effects live inside ``main()`` so the
# script is sourceable without running. Sourcing exposes every
# top-level function (including ``main``, ``phase_loop``, and every
# ``handle_*``) and the per-phase ``*_SHIM`` / timeout constants in
# the calling shell, but produces zero output and zero filesystem /
# network side effects.
#
# Behavior parity with the pre-#4137 top-level layout:
#
#   * ``set -euo pipefail`` is the FIRST statement inside ``main()``,
#     so the validation ``die``s, clone failures, and every subsequent
#     handler invocation see the same errexit / nounset / pipefail
#     semantics they saw at top level. (``set -e`` is shell-wide, not
#     per-function, so the flag propagates into every function call
#     ``main()`` makes — same as the prior top-level setup.)
#   * ``exec 3>&1`` runs inside ``main()`` so sourcing the script does
#     NOT redirect the calling shell's fd 3.
#   * ``exit N`` calls inside ``phase_loop`` and the helpers continue
#     to terminate the whole shell (bash's ``exit`` is process-level,
#     not function-level).
main() {
    set -euo pipefail

    # Wire fd 3 to stdout. CloudWatch captures both stdout and stderr,
    # so the user sees identical output. Functions that need to return
    # a value via stdout (run_claude_phase, read_current_phase, etc.)
    # do NOT need to redirect fd 3 — log() writes to fd 3 which is
    # still stdout of the top process, while the function's own
    # ``printf`` goes to the function's captured stdout. ``$( )``
    # substitution captures fd 1 only, so log noise never pollutes
    # captured function output.
    exec 3>&1

    _validate_required_env
    _validate_start_phase_early
    _init_branch_naming
    _setup_workspace_and_clone
    _setup_gh_auth
    _checkout_branch
    install_fargate_preflight_hook
    apply_prior_patch
    set_agent_task_arn_from_metadata
    _setup_transition_shim
    _setup_phase_input_shim
    _setup_terminal_shims
    _maybe_run_watcher_test_mode
    _apply_start_phase_row_update
    phase_loop
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
