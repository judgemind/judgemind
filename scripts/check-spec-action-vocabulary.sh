#!/usr/bin/env bash
# check-spec-action-vocabulary.sh — Detect drift between the diagnoser action vocabulary
# documented in `.claude/skills/diagnose-failure/SKILL.md` and the daemon's authoritative
# `DIAGNOSER_ACTIONS` frozenset in `scripts/dispatcher/daemon.py`.
#
# Why: Issue #3565 found that the spec/SKILL.md drifted away from the code while
# the daemon kept evolving (added the 9th action `terminal` in #3455). The audit's
# operator-explicit follow-up was a CI-time check that catches future drift before it
# anchors a wrong operational decision.
#
# This script is the regression test for the diagnoser-vocabulary drift class. Other
# drift classes (phase names, failure categories, config knobs) are tracked as
# follow-up p3 issues — see #3565 for the full audit.
#
# Usage:
#   scripts/check-spec-action-vocabulary.sh
#
# Exit codes:
#   0 — vocabulary matches
#   1 — drift detected (lists missing-from-spec / missing-from-code)
#   2 — internal error (couldn't parse one of the source files)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DAEMON_PY="$REPO_ROOT/scripts/dispatcher/daemon.py"
SKILL_MD="$REPO_ROOT/.claude/skills/diagnose-failure/SKILL.md"

if [[ ! -f "$DAEMON_PY" ]]; then
    echo "ERROR: $DAEMON_PY not found" >&2
    exit 2
fi
if [[ ! -f "$SKILL_MD" ]]; then
    echo "ERROR: $SKILL_MD not found" >&2
    exit 2
fi

# Delegate the parse + diff to a Python helper for robustness — bash regex
# alone is brittle against multi-line frozenset literals and prose paragraphs.
PYTHON_BIN="${PYTHON_BIN:-python3}"

"$PYTHON_BIN" - "$DAEMON_PY" "$SKILL_MD" <<'PYEOF'
import re
import sys
from pathlib import Path

daemon_py = Path(sys.argv[1])
skill_md = Path(sys.argv[2])

# ── 1. Parse DIAGNOSER_ACTIONS from daemon.py ──────────────────────────
# The frozenset literal spans multiple lines, e.g.:
#
#     DIAGNOSER_ACTIONS: frozenset[str] = frozenset(
#         {
#             "retry",
#             ...
#             "terminal",
#         }
#     )
#
# Capture everything between the opening `frozenset(` and the matching `)`.
daemon_text = daemon_py.read_text()
m = re.search(
    r"DIAGNOSER_ACTIONS\s*:\s*frozenset\[str\]\s*=\s*frozenset\(\s*\{(.*?)\}\s*\)",
    daemon_text,
    re.DOTALL,
)
if not m:
    print("ERROR: could not locate DIAGNOSER_ACTIONS frozenset in daemon.py", file=sys.stderr)
    print("Expected: DIAGNOSER_ACTIONS: frozenset[str] = frozenset({...})", file=sys.stderr)
    sys.exit(2)

# Extract quoted strings from the captured body.
daemon_actions = set(re.findall(r'"([a-z_]+)"', m.group(1)))
if not daemon_actions:
    print("ERROR: DIAGNOSER_ACTIONS frozenset parsed empty in daemon.py", file=sys.stderr)
    sys.exit(2)

# ── 2. Parse the action vocabulary from diagnose-failure/SKILL.md ──────
# The SKILL.md has a canonical "Recommendation contract — 9 known actions" line that
# lists all eight legacy actions inline as `action`-quoted code, plus a separate
# table row (or prose mention) for `terminal`. We grep all backticked tokens that
# look like action keys near the "9 known actions" sentence and the action-table
# / decision-tree sections.
skill_text = skill_md.read_text()

# Anchor: find the "Recommendation contract — N known actions" line; collect every
# backticked snake_case token from that line onward through the "Inline-action
# pattern" / "Action selection" / decision-tree sections. Stop at the audit-trail
# header to avoid pulling in `git_commit` / `gh_issue_close` etc. that name SIDE
# EFFECTS rather than recommendation actions.
anchor = re.search(r"Recommendation contract.*?known actions", skill_text)
if not anchor:
    print("ERROR: SKILL.md missing the 'Recommendation contract — N known actions' anchor", file=sys.stderr)
    sys.exit(2)

start = anchor.start()
# Stop region — first occurrence of "## Audit trail" or end of file
stop_match = re.search(r"^##\s+Audit trail", skill_text[start:], re.MULTILINE)
end = start + (stop_match.start() if stop_match else len(skill_text) - start)
region = skill_text[start:end]

# Tokens we treat as "documented actions": backticked snake_case identifiers
# that match any token in the daemon set, plus any new tokens that look like
# action keys (lowercase, underscore-only, no dots/dashes/digits).
candidate_actions = set(re.findall(r"`([a-z][a-z_]*[a-z])`", region))

# Allow-list of non-action tokens that legitimately appear in the same region:
# they are field names, sub-skill names, directive shapes, or audit-event names.
NON_ACTION_TOKENS = {
    "action", "action_taken", "actions_taken", "recommendation", "reasoning",
    "hint", "new_scope", "summary", "evidence", "next_directive", "respawn_at",
    "status", "agent_id", "diagnosis_id", "context", "phase", "verdict",
    "task-v2-fix-conflict", "task-v2-fix-ci", "task-v2-ralph", "task-v2-plan",
    "task-v2-summary", "task-v2-verify", "task-v2-retro", "task-v2-operational",
    "ralph", "tdd", "audit", "spotcheck", "gh", "git", "claude", "opus",
    "sonnet", "haiku", "scripts", "diagnose-failure", "task-v2", "task",
    "diagnoser_terminal", "agent_runner_route_stub", "operational_failed",
    "merge_conflict_at_push", "conflict_unresolvable", "verify_failed_post_merge",
    "ralph_ac_infeasible", "summary_ac_infeasible", "ci_red_after_retries",
    "merge_unstick_exhausted", "stuck_timeout", "no_commit_on_exit",
    "no_push_on_exit", "ralph_max_iterations", "subprocess_turn_limit",
    "subprocess_auth_fail", "subprocess_crash", "subprocess_oom_or_kill",
    "rate_limit_529", "cwd_drift", "gh_rate_exhausted", "subprocess",
    "ecs", "claiming", "planning", "setup", "summary", "push_and_pr",
    "awaiting_ci", "fix_ci", "merge", "awaiting_deploy", "verify", "retro",
    "operational", "operational_done", "diagnose", "done",
    "needs_review", "succeeded", "failed", "pending", "completed", "running",
    "blocked", "infeasible_acs", "deferred_acs", "actions_taken",
    "git_commit", "git_push", "gh_issue_create", "gh_issue_edit",
    "gh_issue_comment", "gh_issue_close", "skill_invoke", "bash_run",
    "diagnoser_did_not_complete", "diagnoser_completed_terminal",
    "diagnoser_timeout", "diagnoser_enabled", "diagnoser_fleet_decisions_cap",
    "true", "false", "null",
    # Tools / commands / branches that show up in the same prose region but
    # are emphatically not action-vocabulary tokens.
    "aws", "github", "psql", "main", "master", "run_in_background",
    "max-turns", "timeout", "max_turns",
}
documented_actions = {tok for tok in candidate_actions if tok not in NON_ACTION_TOKENS}

# ── 3. Compare the two sets ────────────────────────────────────────────
missing_from_skill = daemon_actions - documented_actions
missing_from_daemon = documented_actions - daemon_actions

if not missing_from_skill and not missing_from_daemon:
    print(
        f"OK: diagnoser action vocabulary is in sync. "
        f"{len(daemon_actions)} actions: "
        f"{', '.join(sorted(daemon_actions))}"
    )
    sys.exit(0)

print("DRIFT: diagnoser action vocabulary is out of sync between daemon.py and SKILL.md.", file=sys.stderr)
print(file=sys.stderr)
print(f"  daemon DIAGNOSER_ACTIONS ({daemon_py}):", file=sys.stderr)
print(f"    {sorted(daemon_actions)}", file=sys.stderr)
print(f"  SKILL.md documented actions ({skill_md}):", file=sys.stderr)
print(f"    {sorted(documented_actions)}", file=sys.stderr)
print(file=sys.stderr)

if missing_from_skill:
    print(
        f"  Actions in daemon.py but NOT documented in SKILL.md: "
        f"{sorted(missing_from_skill)}",
        file=sys.stderr,
    )
    print(
        "  -> Either add these to the 'Recommendation contract — N known actions' line "
        "and the action-selection table, OR remove them from DIAGNOSER_ACTIONS if no "
        "longer used.",
        file=sys.stderr,
    )

if missing_from_daemon:
    print(
        f"  Actions documented in SKILL.md but NOT in daemon.py DIAGNOSER_ACTIONS: "
        f"{sorted(missing_from_daemon)}",
        file=sys.stderr,
    )
    print(
        "  -> Either add to DIAGNOSER_ACTIONS in scripts/dispatcher/daemon.py, OR remove "
        "from SKILL.md if no longer supported. (If the token is a non-action that the "
        "allow-list missed, add it to NON_ACTION_TOKENS in this script.)",
        file=sys.stderr,
    )

print(file=sys.stderr)
print(
    "See docs/specs/dispatcher-v2-spec.md §8 'Diagnosis Step' for the contract, and "
    "issue #3565 for the audit that motivated this check.",
    file=sys.stderr,
)
sys.exit(1)
PYEOF
