#!/usr/bin/env bash
# test_check_dispatcher_execution_mode_aware.sh — Tests for
# scripts/check-dispatcher-execution-mode-aware.sh
#
# Verifies that the checker:
#   - passes on synthetic daemon.py contents with well-annotated SQL
#     sites (inline ``# exec-mode-agnostic`` comment OR execution_mode
#     branch nearby)
#   - fails on unannotated ``UPDATE dispatcher.agents`` / ``FROM
#     dispatcher.agents`` sites
#   - ignores ``dispatcher.agents`` references inside Python comments
#     and docstring prose
#   - passes on the real ``scripts/dispatcher/daemon.py`` (#3158
#     hardening landed with this test).
#
# Usage:
#   scripts/tests/test_check_dispatcher_execution_mode_aware.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-dispatcher-execution-mode-aware.sh"
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

assert_passes() {
    local desc="$1"
    local target="$2"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$target" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        "$CHECK_SCRIPT" "$target" 2>&1 | sed 's/^/    /'
        FAILURES=$((FAILURES + 1))
    fi
}

assert_fails() {
    local desc="$1"
    local target="$2"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$target" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

# ─── Test 1: UPDATE with nearby execution_mode branch passes ──────────────
write_file "good_update_branch.py" <<'EOF'
def handler(execution_mode):
    if execution_mode == "ecs":
        pass
    cur.execute(
        "UPDATE dispatcher.agents SET status = 'crashed' WHERE agent_id = %s",
        (agent_id,),
    )
EOF
assert_passes "UPDATE near execution_mode keyword" \
    "$TMPDIR_TEST/good_update_branch.py"

# ─── Test 2: UPDATE with nearby exec-mode-agnostic annotation passes ──────
write_file "good_update_annotation.py" <<'EOF'
def handler():
    # exec-mode-agnostic (#3158): phase-driven write.
    cur.execute(
        "UPDATE dispatcher.agents SET phase = %s WHERE agent_id = %s",
        (phase, agent_id),
    )
EOF
assert_passes "UPDATE with nearby # exec-mode-agnostic annotation" \
    "$TMPDIR_TEST/good_update_annotation.py"

# ─── Test 3: UPDATE without any coverage fails ────────────────────────────
write_file "bad_update_bare.py" <<'EOF'
def handler():
    cur.execute(
        "UPDATE dispatcher.agents SET phase = %s WHERE agent_id = %s",
        (phase, agent_id),
    )
EOF
assert_fails "UPDATE without any mode awareness fails" \
    "$TMPDIR_TEST/bad_update_bare.py"

# ─── Test 4: SELECT FROM dispatcher.agents with annotation passes ─────────
write_file "good_select.py" <<'EOF'
def handler():
    # exec-mode-agnostic (#3158): helper lookup.
    cur.execute(
        "SELECT issue_number FROM dispatcher.agents WHERE agent_id = %s",
        (agent_id,),
    )
EOF
assert_passes "SELECT with nearby # exec-mode-agnostic annotation" \
    "$TMPDIR_TEST/good_select.py"

# ─── Test 5: SELECT without coverage fails ────────────────────────────────
write_file "bad_select.py" <<'EOF'
def handler():
    cur.execute(
        "SELECT issue_number FROM dispatcher.agents WHERE agent_id = %s",
        (agent_id,),
    )
EOF
assert_fails "SELECT without any mode awareness fails" \
    "$TMPDIR_TEST/bad_select.py"

# ─── Test 6: docstring mention of dispatcher.agents is ignored ────────────
# The SELECT/UPDATE pattern inside a docstring (comment or prose) is
# not a real SQL site; the checker must skip it.
write_file "docstring_mention.py" <<'EOF'
#: Reads like ``SELECT count(*) FROM dispatcher.agents`` in the docs
#: don't count as real call sites.
CONSTANT = 1
EOF
assert_passes "docstring-prose mention of dispatcher.agents is ignored" \
    "$TMPDIR_TEST/docstring_mention.py"

# ─── Test 7: agent_task_arn keyword satisfies the check ───────────────────
write_file "good_task_arn.py" <<'EOF'
def handler(task_arn):
    # the reap path tracks agent_task_arn for ECS rows.
    cur.execute(
        "SELECT status FROM dispatcher.agents WHERE agent_id = %s",
        (agent_id,),
    )
EOF
assert_passes "agent_task_arn keyword satisfies the check" \
    "$TMPDIR_TEST/good_task_arn.py"

# ─── Test 8: real daemon.py passes (the #3158 hardening landed) ───────────
REAL_DAEMON="$REPO_ROOT/scripts/dispatcher/daemon.py"
if [[ -f "$REAL_DAEMON" ]]; then
    assert_passes "real scripts/dispatcher/daemon.py covers every site" \
        "$REAL_DAEMON"
fi

echo ""
if (( FAILURES > 0 )); then
    echo "$FAILURES of $TESTS tests FAILED"
    exit 1
fi
echo "all $TESTS tests passed"
exit 0
