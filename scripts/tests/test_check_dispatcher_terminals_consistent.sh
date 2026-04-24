#!/usr/bin/env bash
# test_check_dispatcher_terminals_consistent.sh — Tests for
# scripts/check-dispatcher-terminals-consistent.sh
#
# Verifies that the checker:
#   (1) passes when all seven locations carry the canonical terminal set
#   (2) fails when any of the four cross-language locations has a drift
#       (schema.ts, resolvers.ts, ui-primitives.tsx, RecentCompletionsPanel.tsx)
#   (3) fails when daemon.py's _write_diagnosis_outcome_for_agent
#       correct-outcome tuple references a non-canonical terminal
#   (4) passes on the real repo files
#   (5) the guard's own CI step name does not contain a literal terminal string
#
# Usage:
#   scripts/tests/test_check_dispatcher_terminals_consistent.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-dispatcher-terminals-consistent.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST="$(mktemp -d)"
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

write_file() {
    local path="$TMPDIR_TEST/$1"
    mkdir -p "$(dirname "$path")"
    cat > "$path"
}

# assert_passes_files — run the guard with 5 explicit file paths.
# Usage: assert_passes_files "description" daemon schema resolvers ui_prim completions
assert_passes_files() {
    local desc="$1"
    local d="$2" s="$3" r="$4" u="$5" c="$6"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$d" "$s" "$r" "$u" "$c" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        "$CHECK_SCRIPT" "$d" "$s" "$r" "$u" "$c" 2>&1 | sed 's/^/    /'
        FAILURES=$((FAILURES + 1))
    fi
}

# assert_fails_files — run the guard with 5 explicit file paths.
# Usage: assert_fails_files "description" daemon schema resolvers ui_prim completions
assert_fails_files() {
    local desc="$1"
    local d="$2" s="$3" r="$4" u="$5" c="$6"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$d" "$s" "$r" "$u" "$c" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

# assert_passes / assert_fails — run the guard with TMPDIR_TEST as the daemon
# file argument.  Used by the self-match helper which calls assert_passes with
# only a description.  The guard exits 0 when the daemon file is missing
# (fast skip), so using a non-existent path from TMPDIR_TEST is fine here.
assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    # Pass a non-existent daemon path so the guard fast-exits 0.
    # The self-match helper only needs assert_passes to not fail —
    # the guard does not scan ci.yml step names for terminal strings.
    if "$CHECK_SCRIPT" "$TMPDIR_TEST/__nofile__.py" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        "$CHECK_SCRIPT" "$TMPDIR_TEST/__nofile__.py" 2>&1 | sed 's/^/    /'
        FAILURES=$((FAILURES + 1))
    fi
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST/__nofile__.py" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

# ─── Synthetic fixture helpers ────────────────────────────────────────────

# Write a canonical daemon.py fixture with all 5 terminals in both
# _mark_agent_terminal and _write_diagnosis_outcome_for_agent.
write_daemon() {
    write_file "daemon.py" <<'EOF'
class Dispatcher:
    def _mark_agent_terminal(self, agent_id, status):
        """Mark an agent as terminal."""
        terminal = status in (
            "succeeded",
            "failed",
            "crashed",
            "plan_blocked",
            "needs_review",
        )
        return terminal

    def _write_diagnosis_outcome_for_agent(self, agent_id, final_status):
        """Write back the resolved outcome to pending diagnoser rows.

        Updates dispatcher.diagnoses.outcome with shape:
            {"retry_outcome": "succeeded" | "failed", "final_status": ...}
        """
        # plan_blocked and needs_review are correct-outcome terminals.
        retry_outcome = (
            "succeeded"
            if final_status in ("succeeded", "plan_blocked", "needs_review")
            else "failed"
        )
        return retry_outcome

ACTIVE_AGENT_STATUSES = ("running", "retrying", "succeeded", "needs_review")
EOF
}

# Write a canonical schema.ts fixture.
write_schema() {
    write_file "schema.ts" <<'EOF'
"""
DispatcherAgent.status field.
One of running | succeeded | failed | retrying | crashed | plan_blocked | needs_review.
"""
const schemaA = true;

"""
RecentCompletion.status field.
One of succeeded | failed | crashed | plan_blocked | needs_review.
See DispatcherAgent.status for semantics.
"""
const schemaB = true;

"""
Rows from dispatcher.agents where status IN
('succeeded','failed','crashed','plan_blocked','needs_review')
ordered by ended_at DESC.
"""
const schemaC = true;

"""
Recently terminal agents (succeeded | failed | crashed | plan_blocked | needs_review)
fetched from dispatcher.agents.
"""
const schemaD = true;
EOF
}

# Write a canonical resolvers.ts fixture.
write_resolvers() {
    write_file "resolvers.ts" <<'EOF'
async function queryRecentCompletions(pool) {
  const { rows } = await pool.query(
    `SELECT * FROM dispatcher.agents
      WHERE a.status IN ('succeeded', 'failed', 'crashed', 'plan_blocked', 'needs_review')
        AND a.ended_at IS NOT NULL`
  );
  return rows;
}

async function queryRecentCompletionsCount(pool) {
  const { rows } = await pool.query(
    `SELECT COUNT(*) FROM dispatcher.agents
      WHERE status IN ('succeeded', 'failed', 'crashed', 'plan_blocked', 'needs_review')
        AND ended_at IS NOT NULL`
  );
  return rows;
}
EOF
}

# Write a canonical ui-primitives.tsx fixture.
write_ui_prim() {
    write_file "ui-primitives.tsx" <<'EOF'
type OutcomeStatus =
  | 'succeeded'
  | 'failed'
  | 'crashed'
  | 'plan_blocked'
  | 'needs_review';

const OUTCOME_STYLES: Record<
  OutcomeStatus,
  { glyph: string; className: string; label: string }
> = {
  succeeded: {
    glyph: '\u2713',
    className: 'bg-green-100',
    label: 'succeeded',
  },
  failed: {
    glyph: '\u2717',
    className: 'bg-red-100',
    label: 'failed',
  },
  crashed: {
    glyph: '\u26A0',
    className: 'bg-amber-100',
    label: 'crashed',
  },
  plan_blocked: {
    glyph: '\u2298',
    className: 'bg-muted',
    label: 'plan blocked',
  },
  needs_review: {
    glyph: '\u25D0',
    className: 'bg-yellow-100',
    label: 'needs review',
  },
};
EOF
}

# Write a canonical RecentCompletionsPanel.tsx fixture.
write_completions() {
    write_file "completions.tsx" <<'EOF'
/**
 * The "Recently completed" panel. Newest terminal-state agents
 * (succeeded / failed / crashed / plan_blocked / needs_review) in
 * the operator's recent history.
 */
export function RecentCompletionsPanel() {
  return null;
}
EOF
}

# ─── Test 1: Happy path — all canonical fixtures pass ────────────────────
write_daemon
write_schema
write_resolvers
write_ui_prim
write_completions
assert_passes_files "Happy path: all locations canonical" \
    "$TMPDIR_TEST/daemon.py" \
    "$TMPDIR_TEST/schema.ts" \
    "$TMPDIR_TEST/resolvers.ts" \
    "$TMPDIR_TEST/ui-primitives.tsx" \
    "$TMPDIR_TEST/completions.tsx"
reset_tmpdir

# ─── Test 2: schema.ts drift — remove a terminal from docstrings ─────────
write_daemon
write_schema
write_resolvers
write_ui_prim
write_completions
# Overwrite schema.ts with a version missing 'crashed'
write_file "schema.ts" <<'EOF'
"""
One of running | succeeded | failed | retrying | plan_blocked | needs_review.
"""
const schemaA = true;

"""
One of succeeded | failed | plan_blocked | needs_review.
"""
const schemaB = true;

"""
status IN ('succeeded','failed','plan_blocked','needs_review')
"""
const schemaC = true;

"""
terminal agents (succeeded | failed | plan_blocked | needs_review)
"""
const schemaD = true;
EOF
assert_fails_files "schema.ts drift: missing a terminal from docstrings fails" \
    "$TMPDIR_TEST/daemon.py" \
    "$TMPDIR_TEST/schema.ts" \
    "$TMPDIR_TEST/resolvers.ts" \
    "$TMPDIR_TEST/ui-primitives.tsx" \
    "$TMPDIR_TEST/completions.tsx"
reset_tmpdir

# ─── Test 3: resolvers.ts drift — remove a terminal from SQL IN list ─────
write_daemon
write_schema
write_resolvers
write_ui_prim
write_completions
# Overwrite resolvers.ts missing 'plan_blocked' from the SQL IN clause
write_file "resolvers.ts" <<'EOF'
async function queryRecentCompletions(pool) {
  const { rows } = await pool.query(
    `SELECT * FROM dispatcher.agents
      WHERE a.status IN ('succeeded', 'failed', 'crashed', 'needs_review')
        AND a.ended_at IS NOT NULL`
  );
  return rows;
}

async function queryRecentCompletionsCount(pool) {
  const { rows } = await pool.query(
    `SELECT COUNT(*) FROM dispatcher.agents
      WHERE status IN ('succeeded', 'failed', 'crashed', 'needs_review')
        AND ended_at IS NOT NULL`
  );
  return rows;
}
EOF
assert_fails_files "resolvers.ts drift: missing terminal from SQL IN list fails" \
    "$TMPDIR_TEST/daemon.py" \
    "$TMPDIR_TEST/schema.ts" \
    "$TMPDIR_TEST/resolvers.ts" \
    "$TMPDIR_TEST/ui-primitives.tsx" \
    "$TMPDIR_TEST/completions.tsx"
reset_tmpdir

# ─── Test 4: ui-primitives.tsx drift — remove a terminal from OutcomeStatus ──
write_daemon
write_schema
write_resolvers
write_ui_prim
write_completions
# Overwrite ui-primitives.tsx missing 'needs_review' from OutcomeStatus union
write_file "ui-primitives.tsx" <<'EOF'
type OutcomeStatus =
  | 'succeeded'
  | 'failed'
  | 'crashed'
  | 'plan_blocked';

const OUTCOME_STYLES: Record<
  OutcomeStatus,
  { glyph: string; className: string; label: string }
> = {
  succeeded: {
    glyph: '\u2713',
    className: 'bg-green-100',
    label: 'succeeded',
  },
  failed: {
    glyph: '\u2717',
    className: 'bg-red-100',
    label: 'failed',
  },
  crashed: {
    glyph: '\u26A0',
    className: 'bg-amber-100',
    label: 'crashed',
  },
  plan_blocked: {
    glyph: '\u2298',
    className: 'bg-muted',
    label: 'plan blocked',
  },
};
EOF
assert_fails_files "ui-primitives.tsx drift: missing terminal from OutcomeStatus fails" \
    "$TMPDIR_TEST/daemon.py" \
    "$TMPDIR_TEST/schema.ts" \
    "$TMPDIR_TEST/resolvers.ts" \
    "$TMPDIR_TEST/ui-primitives.tsx" \
    "$TMPDIR_TEST/completions.tsx"
reset_tmpdir

# ─── Test 5: RecentCompletionsPanel.tsx drift — remove terminal from JSDoc ──
write_daemon
write_schema
write_resolvers
write_ui_prim
write_completions
# Overwrite completions.tsx missing 'failed' from JSDoc status list
write_file "completions.tsx" <<'EOF'
/**
 * The "Recently completed" panel. Newest terminal-state agents
 * (succeeded / crashed / plan_blocked / needs_review) in
 * the operator's recent history.
 */
export function RecentCompletionsPanel() {
  return null;
}
EOF
assert_fails_files "RecentCompletionsPanel.tsx drift: missing terminal from JSDoc fails" \
    "$TMPDIR_TEST/daemon.py" \
    "$TMPDIR_TEST/schema.ts" \
    "$TMPDIR_TEST/resolvers.ts" \
    "$TMPDIR_TEST/ui-primitives.tsx" \
    "$TMPDIR_TEST/completions.tsx"
reset_tmpdir

# ─── Test 6: _write_diagnosis_outcome_for_agent has a non-canonical terminal ──
# If a terminal that was removed from _mark_agent_terminal is still in
# the correct-outcome tuple of _write_diagnosis_outcome_for_agent, the check
# should fail (phantom terminal in the classifier).
write_schema
write_resolvers
write_ui_prim
write_completions
# Write daemon.py with 5 canonical terminals in _mark_agent_terminal,
# but _write_diagnosis_outcome_for_agent has a stale "timed_out" terminal
# in the correct-outcome tuple that does NOT exist in canonical.
write_file "daemon.py" <<'EOF'
class Dispatcher:
    def _mark_agent_terminal(self, agent_id, status):
        """Mark an agent as terminal."""
        terminal = status in (
            "succeeded",
            "failed",
            "crashed",
            "plan_blocked",
            "needs_review",
        )
        return terminal

    def _write_diagnosis_outcome_for_agent(self, agent_id, final_status):
        """Write back the resolved outcome.

        {"retry_outcome": "succeeded" | "failed", "final_status": ...}
        """
        # timed_out was once a correct-outcome terminal but was removed
        # from the canonical set — this tuple is now stale.
        retry_outcome = (
            "succeeded"
            if final_status in ("succeeded", "plan_blocked", "needs_review", "timed_out")
            else "failed"
        )
        return retry_outcome

ACTIVE_AGENT_STATUSES = ("running", "retrying", "succeeded", "needs_review")
EOF
assert_fails_files "Non-canonical terminal in _write_diagnosis_outcome_for_agent fails" \
    "$TMPDIR_TEST/daemon.py" \
    "$TMPDIR_TEST/schema.ts" \
    "$TMPDIR_TEST/resolvers.ts" \
    "$TMPDIR_TEST/ui-primitives.tsx" \
    "$TMPDIR_TEST/completions.tsx"
reset_tmpdir

# ─── Test 7: ACTIVE_AGENT_STATUSES contains an unknown status ────────────
write_schema
write_resolvers
write_ui_prim
write_completions
# Write daemon.py with a stray/unknown status in ACTIVE_AGENT_STATUSES
write_file "daemon.py" <<'EOF'
class Dispatcher:
    def _mark_agent_terminal(self, agent_id, status):
        """Mark an agent as terminal."""
        terminal = status in (
            "succeeded",
            "failed",
            "crashed",
            "plan_blocked",
            "needs_review",
        )
        return terminal

    def _write_diagnosis_outcome_for_agent(self, agent_id, final_status):
        """Write back the resolved outcome.

        {"retry_outcome": "succeeded" | "failed", "final_status": ...}
        """
        retry_outcome = (
            "succeeded"
            if final_status in ("succeeded", "plan_blocked", "needs_review")
            else "failed"
        )
        return retry_outcome

ACTIVE_AGENT_STATUSES = ("running", "retrying", "succeeded", "needs_review", "pending")
EOF
assert_fails_files "ACTIVE_AGENT_STATUSES with unknown status fails" \
    "$TMPDIR_TEST/daemon.py" \
    "$TMPDIR_TEST/schema.ts" \
    "$TMPDIR_TEST/resolvers.ts" \
    "$TMPDIR_TEST/ui-primitives.tsx" \
    "$TMPDIR_TEST/completions.tsx"
reset_tmpdir

# ─── Test 8: Missing daemon file exits 0 (nothing to check) ──────────────
write_schema
write_resolvers
write_ui_prim
write_completions
# Pass a non-existent daemon path — guard should exit 0 (fast skip).
assert_passes_files "Missing daemon.py exits 0 cleanly" \
    "$TMPDIR_TEST/does_not_exist.py" \
    "$TMPDIR_TEST/schema.ts" \
    "$TMPDIR_TEST/resolvers.ts" \
    "$TMPDIR_TEST/ui-primitives.tsx" \
    "$TMPDIR_TEST/completions.tsx"
reset_tmpdir

# ─── Test 9: Real repo files pass ────────────────────────────────────────
REAL_DAEMON="$REPO_ROOT/scripts/dispatcher/daemon.py"
REAL_SCHEMA="$REPO_ROOT/packages/api/src/graphql/dispatcher/schema.ts"
REAL_RESOLVERS="$REPO_ROOT/packages/api/src/graphql/dispatcher/resolvers.ts"
REAL_UI_PRIM="$REPO_ROOT/packages/web/src/app/(main)/admin/dispatcher/ui-primitives.tsx"
REAL_COMPLETIONS="$REPO_ROOT/packages/web/src/app/(main)/admin/dispatcher/RecentCompletionsPanel.tsx"
if [[ -f "$REAL_DAEMON" && -f "$REAL_SCHEMA" && -f "$REAL_RESOLVERS" && -f "$REAL_UI_PRIM" && -f "$REAL_COMPLETIONS" ]]; then
    assert_passes_files "Real repo files: all locations consistent" \
        "$REAL_DAEMON" "$REAL_SCHEMA" "$REAL_RESOLVERS" "$REAL_UI_PRIM" "$REAL_COMPLETIONS"
fi

# ─── Test 10: No self-match on ci.yml step name ──────────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain the terminal string literals (succeeded / failed /
# crashed / plan_blocked / needs_review) which would trip the guard.
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-dispatcher-terminals-consistent.sh" "yml"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
if (( FAILURES > 0 )); then
    echo "$FAILURES of $TESTS tests FAILED"
    exit 1
fi
echo "all $TESTS tests passed"
exit 0
