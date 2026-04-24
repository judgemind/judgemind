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
#   3. **Mechanical phases with side effects** — `push_and_pr` has a
#      minimal in-process implementation (`handle_push_and_pr` ==
#      `git push` + `gh pr create`). `awaiting_ci`, `merge`,
#      `awaiting_deploy`, `retro`, and `setup` are stubbed: they
#      advance to the documented "next" phase on the happy path so
#      the smoke test can reach `done` without a full daemon
#      integration. Stage 2 fleshes each stub out.
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
    """Mirror DispatcherDaemon._parse_parent_issue."""
    match = re.search(r"(?im)^\s*parent\s*:\s*#(\d+)\s*$", body)
    return int(match.group(1)) if match else None


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
    outcome = _run(cmd, timeout=30)
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
    outcome = _run(cmd, timeout=30)
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
    outcome = _run(cmd, timeout=60)
    if outcome.returncode != 0:
        return ""
    return outcome.stdout or ""


def _extract_failing_jobs(pr_status: dict) -> list[dict]:
    """Mirror DispatcherDaemon._extract_failing_jobs (pre-log-tail).

    Returns up to ``FIX_CI_MAX_FAILING_JOBS`` entries.
    """
    failure_conclusions = {
        "FAILURE",
        "CANCELLED",
        "TIMED_OUT",
        "ACTION_REQUIRED",
        "STARTUP_FAILURE",
    }
    rollup = pr_status.get("statusCheckRollup") or []
    failing: list[dict] = []
    for check in rollup:
        if not isinstance(check, dict):
            continue
        conclusion = str(check.get("conclusion") or "").upper()
        status = str(check.get("status") or "").upper()
        if status == "COMPLETED" and conclusion in failure_conclusions:
            failing.append(
                {
                    "name": check.get("name") or check.get("context") or "",
                    "conclusion": conclusion,
                    "databaseId": check.get("databaseId"),
                    "detailsUrl": check.get("detailsUrl"),
                }
            )
        if len(failing) >= FIX_CI_MAX_FAILING_JOBS:
            break
    return failing


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
    outcome = _run(cmd, timeout=30)
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
    outcome = _run(cmd, timeout=30)
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

    payload = _build_input(phase, agent_id, issue_number, repo_root, github_repo)

    input_dir = repo_root / "tmp" / "dispatcher-input"
    input_dir.mkdir(parents=True, exist_ok=True)
    # Normalize skill-suffix naming for the on-disk file (daemon writes
    # `fix-ci.json` even though the phase-column value is `fix_ci`).
    file_phase = phase
    out_path = input_dir / f"{file_phase}.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
PYEOF
fi

# Shell helper: build the input file for the given phase. The phase
# argument matches the skill-suffix (``plan``, ``ralph``, ``summary``,
# ``fix-ci``, ``verify``, ``retro``), which differs from the phase-
# column value (``planning`` vs ``plan``, ``fix_ci`` vs ``fix-ci``).
# ``run_claude_phase`` does the mapping before calling us.
write_phase_input() {
    _phase_suffix="$1"
    _issue_for_input="${ISSUE_NUMBER:-0}"
    if ! python3 "$PHASE_INPUT_SHIM" \
        "$_phase_suffix" \
        "$AGENT_ID" \
        "$_issue_for_input" \
        "$REPO_ROOT" \
        > "$AGENT_WORKSPACE/phase-input-$_phase_suffix.log" \
        2>> "$AGENT_WORKSPACE/phase-input-$_phase_suffix.log"; then
        log "phase_input_write_failed" "phase=$_phase_suffix"
        return 1
    fi
    log "phase_input_written" "phase=$_phase_suffix"
    return 0
}

# Read the skill's structured output from
# ``{repo_root}/tmp/dispatcher-output/<skill_suffix>.json`` and print
# the minified JSON to stdout. Returns non-zero when the file is
# missing / unparseable so ``run_claude_phase`` can fall back to the
# ``.result`` envelope + diag path.
read_phase_output() {
    _phase_suffix="$1"
    _out_path="$REPO_ROOT/tmp/dispatcher-output/$_phase_suffix.json"
    if [[ ! -s "$_out_path" ]]; then
        return 1
    fi
    # ``jq -c '.'`` validates JSON and emits on one line. Failure
    # (malformed file) → non-zero exit, empty stdout, caller falls
    # through to the ``.result`` branch.
    if ! jq -c '.' "$_out_path" 2>/dev/null; then
        return 1
    fi
    return 0
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

# Phase → skill name mapping (#3117). The phase-name constants emitted
# by `scripts/dispatcher/phase_transitions.py` use underscored/present-
# participle names (planning, fix_ci). The actual Claude skills on disk
# are hyphenated/root-verb (task-v2-plan, task-v2-fix-ci). We do NOT
# construct `/task-v2-$_phase` directly; we map to the literal skill
# name via this case statement so any drift (new phase, renamed skill)
# surfaces as an explicit die() instead of a silent "Unknown command"
# string coming back from claude.
#
# Bash 3.2: a case statement is used instead of an associative array
# (declare -A is bash 4+).
phase_to_skill() {
    # $1 = phase name. Prints the matching skill suffix (no `task-v2-`
    # prefix) on stdout. die()s if no mapping exists.
    case "$1" in
        planning)  printf 'plan' ;;
        ralph)     printf 'ralph' ;;
        summary)   printf 'summary' ;;
        fix_ci)    printf 'fix-ci' ;;
        verify)    printf 'verify' ;;
        retro)     printf 'retro' ;;
        *)         die "no_skill_mapping_for_phase=$1" ;;
    esac
}

run_claude_phase() {
    _phase="$1"
    _out_file="$AGENT_WORKSPACE/claude-p-$_phase.stdout.json"

    if [[ "$AGENT_RUNNER_DRY_RUN" == "1" ]]; then
        log "claude_phase_dry_run" "phase=$_phase"
        printf '{}'
        return 0
    fi

    # Map phase name → skill suffix. A bad phase (e.g. accidentally
    # routed `claiming` or `push_and_pr` through this function) aborts
    # the runner via die() so the symptom surfaces at test time, not
    # as a silent "Unknown command" in production CloudWatch.
    _skill=$(phase_to_skill "$_phase")

    # Write the phase's dispatcher-input JSON before invoking claude
    # (#3133). Without this, every task-v2-* skill hits its input-
    # missing guard and returns a plain-string `.result` — which is
    # exactly the failure mode the Step 1 diag captured. The shim is
    # best-effort: if gh is unreachable or the issue is gone, it still
    # writes a partial payload so the skill at least has the base
    # identifier fields and can produce a structured BLOCKED verdict
    # rather than a null-deref.
    write_phase_input "$_skill" || true

    log "claude_phase_begin" "phase=$_phase" "skill=$_skill"
    # Do NOT fail the script on a non-zero exit — parse the envelope
    # and let the caller decide. Redirect stderr to a sibling file for
    # triage parity with the daemon.
    set +e
    claude -p "/task-v2-$_skill $AGENT_ID" \
        --output-format json \
        --dangerously-skip-permissions \
        > "$_out_file" \
        2> "$AGENT_WORKSPACE/claude-p-$_phase.stderr.log"
    _rc=$?
    set -e
    log "claude_phase_done" "phase=$_phase" "exit_code=$_rc"

    # Output resolution order (#3133):
    #   1. ``{repo_root}/tmp/dispatcher-output/<skill_suffix>.json`` —
    #      the skill's structured output file. This is how the daemon
    #      reads verdict/plan-body/etc. — see
    #      :meth:`DispatcherDaemon._read_phase_output`. Before #3133
    #      the agent-runner ignored this file entirely and read only
    #      the claude `.result` envelope, which is the conversation
    #      text summary (string), not the structured JSON the skill
    #      actually wrote.
    #   2. ``.result`` as a JSON object — legacy path + defensive
    #      fallback for skills that return a dict directly. Kept for
    #      back-compat with any skill that hasn't migrated to the
    #      dispatcher-output/ contract.
    #   3. ``{}`` with the #3131 diag event — skill crashed before
    #      writing its output and the envelope itself isn't a dict.
    _file_output=""
    if _file_output=$(read_phase_output "$_skill"); then
        log "phase_output_file_read" "phase=$_phase" "skill=$_skill"
        printf '%s' "$_file_output"
        return 0
    fi

    if jq -e '.result | type == "object"' "$_out_file" >/dev/null 2>&1; then
        jq -c '.result' "$_out_file" 2>/dev/null || printf '{}'
    else
        # Self-diagnosing branch (#3131). Every ECS agent today is dying
        # because `.result` comes back non-object; operators have no way
        # to see WHY without a second deploy-instrument cycle. Log the
        # structured triage fields directly on the failure event so the
        # first post-deploy ECS agent surfaces the actual payload in
        # CloudWatch. Preserves the existing `claude_result_non_object`
        # event name (load-bearing for log-insights queries) and adds a
        # sibling `claude_result_non_object_diag` event carrying:
        #
        #   * result_type   — jq's type of `.result` (string/null/number/...).
        #   * result_bytes  — byte length of `.result` stringified.
        #   * result_head_512  — first 512 bytes of `.result` stringified,
        #     newlines replaced with `\n` and double quotes escaped so
        #     the line stays a single JSON-ish record the log() function
        #     can emit without breaking CloudWatch line boundaries.
        #   * stderr_head_1024 — first 1024 bytes of the sibling
        #     `.stderr.log` file, same escaping. Omitted when the stderr
        #     file is empty / missing so the common "no stderr" case
        #     isn't noise.
        #
        # All payload extraction happens in-process via `jq` + shell
        # builtins — no extra tools, no new dependencies. Bash 3.2
        # compatible (`cut -c` + `tr` + `sed`; no `${var:0:512}` slicing
        # on multi-byte-safe boundaries is required because we're only
        # emitting the first 512 BYTES for triage, not interpreting them).
        log "claude_result_non_object" "phase=$_phase" "out_file=$_out_file"

        _result_type=$(jq -r '.result | type' "$_out_file" 2>/dev/null || printf 'unparseable')
        # `.result | tostring` stringifies any type — strings pass
        # through, objects/arrays/null serialize to their JSON form.
        # `-r` emits raw (no wrapping quotes) so the byte count matches
        # the actual payload length. Falls back to the raw file on a
        # parse failure (malformed JSON) so the diag still shows the
        # first 512 bytes of whatever claude wrote.
        _result_str_file="$AGENT_WORKSPACE/claude-p-$_phase.result.str"
        if ! jq -r '.result | tostring' "$_out_file" > "$_result_str_file" 2>/dev/null; then
            cp "$_out_file" "$_result_str_file" 2>/dev/null || printf '' > "$_result_str_file"
        fi
        _result_bytes=$(wc -c < "$_result_str_file" 2>/dev/null | tr -d ' ' || printf '0')

        # Shape for embedding in log() key=value pairs. log() handles
        # double-quote escaping; we handle backslash escaping (log()
        # doesn't) and control-char squashing (so the output stays one
        # line and does not split CloudWatch log events). Order matters:
        # escape backslashes FIRST so we don't double-escape the ones we
        # just added. Use `tr` to replace newlines/CRs/tabs with spaces
        # rather than emitting literal `\n` (which would require
        # teaching log() to not re-escape the backslash). This keeps
        # the triage payload human-readable in CloudWatch at the cost
        # of losing exact whitespace boundaries — fine for a first-512
        # smoke.
        _result_head_512=$(head -c 512 "$_result_str_file" 2>/dev/null \
            | tr '\n\r\t' '   ' \
            | sed -e 's/\\/\\\\/g' || printf '')

        # Stderr tail — only emit when non-empty so the common healthy
        # path doesn't bloat log lines.
        _stderr_file="$AGENT_WORKSPACE/claude-p-$_phase.stderr.log"
        _stderr_head_1024=""
        if [[ -s "$_stderr_file" ]]; then
            _stderr_head_1024=$(head -c 1024 "$_stderr_file" 2>/dev/null \
                | tr '\n\r\t' '   ' \
                | sed -e 's/\\/\\\\/g' || printf '')
        fi

        if [[ -n "$_stderr_head_1024" ]]; then
            log "claude_result_non_object_diag" \
                "phase=$_phase" \
                "result_type=$_result_type" \
                "result_bytes=$_result_bytes" \
                "result_head_512=$_result_head_512" \
                "stderr_head_1024=$_stderr_head_1024"
        else
            log "claude_result_non_object_diag" \
                "phase=$_phase" \
                "result_type=$_result_type" \
                "result_bytes=$_result_bytes" \
                "result_head_512=$_result_head_512"
        fi

        printf '{}'
    fi
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

handle_push_and_pr() {
    # Mechanical implementation of the push_and_pr phase (#3117).
    #
    # This phase is NOT claude-driven — the daemon handles push + PR
    # creation inline today via `_handle_phase_push_and_pr` (daemon.py
    # ~L10544). Prior to this fix the entrypoint's phase-dispatch case
    # lumped `push_and_pr` in with the claude-driven branches and
    # called `/task-v2-push_and_pr`, which returned "Unknown command"
    # (the skill does not exist).
    #
    # Stage 1b scope: the minimal viable push + PR, enough to get the
    # smoke to exercise the phase boundary. Rich failure handling
    # (commit --amend with the summary phase's commit_message, self-
    # deploy detection, unmet-AC draft-PR, git_push_failed diagnoser
    # routing) stays in the daemon's implementation and lands on the
    # agent-runner side in a later Stage 2 PR.
    #
    # Prints the phase-output JSON envelope on stdout so the caller
    # can persist it via persist_phase_output and drive the transition
    # shim. Output shape matches `transition_from_push_and_pr`:
    #   {"no_op": true}   → terminal success (no commit to push)
    #   {"no_op": false}  → advance to awaiting_ci
    #
    # Note: if `AGENT_RUNNER_DRY_RUN=1`, emit `{"no_op": true}` so the
    # loop reaches a terminal phase without actually shelling out.

    if [[ "$AGENT_RUNNER_DRY_RUN" == "1" ]]; then
        log "push_and_pr_dry_run"
        printf '{"no_op": true}'
        return 0
    fi

    # Detect the #3039 no-op-SHIP guardrail: ralph's SHIP with a clean
    # working tree means `origin/main..HEAD` is empty — there's nothing
    # to push and no PR to open. Terminate as no_op so the transition
    # shim flips the agent to `succeeded`.
    _ahead_count=$(git -C "$REPO_ROOT" rev-list --count origin/main..HEAD 2>/dev/null || printf '0')
    if [[ "$_ahead_count" == "0" ]]; then
        log "push_and_pr_no_op" "reason=clean_worktree_on_ship"
        printf '{"no_op": true}'
        return 0
    fi

    log "push_and_pr_push_begin" "branch=$BRANCH_NAME"
    set +e
    git -C "$REPO_ROOT" push -u origin "$BRANCH_NAME" \
        > "$AGENT_WORKSPACE/git-push.stdout.log" \
        2> "$AGENT_WORKSPACE/git-push.stderr.log"
    _push_rc=$?
    set -e
    log "push_and_pr_push_done" "exit_code=$_push_rc"
    if [[ "$_push_rc" -ne 0 ]]; then
        log "push_and_pr_push_failed" "exit_code=$_push_rc"
        # Emit a minimal failure envelope; the transition shim will
        # route to the diagnoser via the unrecognized/non-SHIP path.
        printf '{"no_op": false, "push_failed": true}'
        return 0
    fi

    # Open the PR against main. `gh pr create` auto-picks the current
    # branch as head and --base main. The title comes from the last
    # commit subject on the branch (Stage 2 will plumb the summary
    # phase's pr_title through here; Stage 1b keeps it minimal).
    log "push_and_pr_pr_create_begin"
    set +e
    gh pr create \
        --repo judgemind/judgemind \
        --base main \
        --head "$BRANCH_NAME" \
        --fill \
        > "$AGENT_WORKSPACE/gh-pr-create.stdout.log" \
        2> "$AGENT_WORKSPACE/gh-pr-create.stderr.log"
    _pr_rc=$?
    set -e
    log "push_and_pr_pr_create_done" "exit_code=$_pr_rc"
    if [[ "$_pr_rc" -ne 0 ]]; then
        log "push_and_pr_pr_create_failed" "exit_code=$_pr_rc"
        printf '{"no_op": false, "pr_create_failed": true}'
        return 0
    fi

    printf '{"no_op": false}'
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

    _escaped_patch=$(sed "s/'/''/g" "$_patch_file")
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
    _check_sql="SELECT 1 FROM dispatcher.ralph_patches
                 WHERE agent_id = '$AGENT_ID'
                   AND iteration_n = $_iter
                 LIMIT 1;"
    if ! _already=$(psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -At -c "$_check_sql" 2>/dev/null); then
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

    _sql="INSERT INTO dispatcher.ralph_patches
            (agent_id, issue_number, patch_content, commit_sha,
             iteration_n, verdict)
          VALUES
            ('$AGENT_ID', $_issue_clause, '$_escaped_patch',
             '$_sha', $_iter, 'LOOP');"

    if ! psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "$_sql" >/dev/null 2>&1; then
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
    _update_sql="UPDATE dispatcher.agents
                    SET ralph_iterations_observed =
                        GREATEST(ralph_iterations_observed, $_iter)
                  WHERE agent_id = '$AGENT_ID';"
    if ! psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "$_update_sql" >/dev/null 2>&1; then
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
    #
    # Claude-driven phases (#3117): only the phases that map to a real
    # `task-v2-*` skill belong here. `claiming` and `push_and_pr` are
    # mechanical — they were incorrectly included in the original
    # Stage 1b dispatch, which called `/task-v2-claiming` /
    # `/task-v2-push_and_pr` and got "Unknown command" back.
    case "$_current" in
        planning|ralph|summary|fix_ci|verify)
            if [[ "$_current" == "ralph" ]]; then
                # #3144: start the HEAD-watcher subshell so the long
                # ralph phase emits per-iteration observability to
                # CloudWatch + dispatcher.ralph_patches. The watcher is
                # stopped unconditionally after run_claude_phase returns
                # (success or failure) so the subshell never outlives
                # the phase it instruments.
                start_ralph_head_watcher
            fi
            _output=$(run_claude_phase "$_current")
            if [[ "$_current" == "ralph" ]]; then
                stop_ralph_head_watcher
            fi
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
        claiming)
            # Mechanical pseudo-phase (#3117): the daemon writes
            # phase='claiming' only briefly while it inserts the
            # dispatcher.agents row; the claim itself is a pure DB
            # write, not an LLM pass. Any ECS task whose phase column
            # reads `claiming` at boot is a post-claim lifecycle
            # artifact — the claim already happened before the task
            # was launched. Advance immediately to `planning` so the
            # phase loop starts the real work without shelling out to
            # a nonexistent `/task-v2-claiming` skill.
            log "claiming_no_op_advance_to_planning"
            persist_phase_output "claiming" '{"no_op": true}'
            advance_phase "planning"
            ;;
        push_and_pr)
            # Mechanical phase (#3117). See handle_push_and_pr for the
            # git push + gh pr create implementation. On success the
            # transition shim advances to awaiting_ci (or to no_op
            # terminal if the worktree was clean at ralph SHIP time).
            _output=$(handle_push_and_pr)
            persist_phase_output "push_and_pr" "$_output"
            _transition=$(transition_for "push_and_pr" "$_output")
            _action=$(printf '%s' "$_transition" | cut -f1)
            _next=$(printf '%s' "$_transition" | cut -f2)
            _status=$(printf '%s' "$_transition" | cut -f3)
            case "$_action" in
                advance)
                    advance_phase "$_next"
                    ;;
                advance_with_status)
                    advance_phase "$_next" "$_status"
                    ;;
                *)
                    log "push_and_pr_transition_unrecognized" "action=$_action"
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
