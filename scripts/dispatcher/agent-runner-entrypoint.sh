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
# Stage 1b scope (#3133): plan + ralph inputs are built fully so the
# smoke exercises planning → ralph with real object `.result` values.
# summary / fix-ci / verify / retro inputs are minimal — they include
# the required identifier fields (agent_id, issue_number, worktree_path)
# plus whatever the shim can derive locally, but the richer context
# (git diff, CI failing jobs, deploy status, phase_transitions) stays
# the daemon's job. Each of those skills will still BLOCK cleanly when
# its input is incomplete, which is the correct behaviour for a
# smoke-only image — Stage 2 fleshes out the per-phase wiring.
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

Minimal Stage 1b scope (#3133):
  * planning — full input via ``gh issue view --json`` (mirrors the
    daemon's ``_fetch_issue_bundle``). Non-bot comments filtered.
    ``Blocked by #N`` + ``Parent: #N`` parsed from body.
  * ralph — plan output from ``dispatcher-output/plan.json`` + the
    identifiers; ``max_iterations`` defaults to 5 matching the daemon.
  * summary / fix-ci / verify / retro — identifier fields + whatever
    can be derived locally (prior phase outputs, branch name, git diff
    for summary). Fields the daemon has (CI job logs, deploy status,
    phase_transitions timing) are left empty; each skill short-circuits
    with a structured BLOCKED verdict rather than a string ``.result``.

Exit codes: 0 on write success, 2 on unrecoverable error (caller
should log + continue — the skill's missing-input path still works).
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Thin wrapper so tests can stub via PATH shims."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _parse_blocked_by(body: str) -> list[int]:
    """Mirror DispatcherDaemon._parse_blocked_by."""
    return [int(m) for m in re.findall(r"(?im)^\s*blocked by\s+#(\d+)\s*$", body)]


def _parse_parent_issue(body: str) -> int | None:
    """Mirror DispatcherDaemon._parse_parent_issue."""
    match = re.search(r"(?im)^\s*parent\s*:\s*#(\d+)\s*$", body)
    return int(match.group(1)) if match else None


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


def _read_prior_output(repo_root: Path, phase: str) -> dict:
    """Read ``{repo_root}/tmp/dispatcher-output/<phase>.json`` if present."""
    path = repo_root / "tmp" / "dispatcher-output" / f"{phase}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


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
        bundle = _fetch_issue_bundle(github_repo, issue_number)
        plan = _read_prior_output(repo_root, "plan")
        ralph = _read_prior_output(repo_root, "ralph")
        changed_files = ralph.get("changed_files") or _git_changed_files(repo_root)
        return {
            **base,
            "issue_title": bundle.get("issue_title", ""),
            "issue_body": bundle.get("issue_body", ""),
            "issue_comments": bundle.get("issue_comments", []),
            "ralph_summary": ralph.get("summary", "") if isinstance(ralph, dict) else "",
            "changed_files": changed_files,
            "git_diff": _git_diff(repo_root),
            "branch": _git_current_branch(repo_root),
            "plan_acceptance_criteria": plan.get("acceptance_criteria", []) or [],
            "scope_check": plan.get("scope_check", []) or [],
        }
    if phase == "fix-ci":
        plan = _read_prior_output(repo_root, "plan")
        return {
            **base,
            "pr_number": 0,
            "branch": _git_current_branch(repo_root),
            "failing_jobs": [],
            "git_diff_base_to_head": _git_diff(repo_root),
            "previous_fix_attempts": 0,
            "change_type": plan.get("change_type", "") if isinstance(plan, dict) else "",
        }
    if phase == "verify":
        plan = _read_prior_output(repo_root, "plan")
        return {
            **base,
            "pr_number": 0,
            "acceptance_criteria": plan.get("acceptance_criteria", []) or [],
            "change_type": plan.get("change_type", "") if isinstance(plan, dict) else "",
            "touched_services": [],
            "deploy_status": None,
            "merged_commit_sha": "",
            "plan_text": plan.get("plan_text", "") if isinstance(plan, dict) else "",
            "scope_check": plan.get("scope_check", []) or [],
            "deferred_acs": [],
        }
    if phase == "retro":
        return {
            **base,
            "pr_number": 0,
            "phase_transitions": [],
            "failures": [],
            "ralph_iterations": 0,
            "ci_attempts": 0,
            "fix_ci_attempts": 0,
            "total_duration_s": 0,
            "diff_stats": {"files_changed": 0, "insertions": 0, "deletions": 0},
            "scope_check_followups": [],
            "plan_follow_ups": [],
        }
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
    #
    # Claude-driven phases (#3117): only the phases that map to a real
    # `task-v2-*` skill belong here. `claiming` and `push_and_pr` are
    # mechanical — they were incorrectly included in the original
    # Stage 1b dispatch, which called `/task-v2-claiming` /
    # `/task-v2-push_and_pr` and got "Unknown command" back.
    case "$_current" in
        planning|ralph|summary|fix_ci|verify)
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
