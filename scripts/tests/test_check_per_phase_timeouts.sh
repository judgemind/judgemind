#!/usr/bin/env bash
# test_check_per_phase_timeouts.sh — Tests for
# scripts/check-per-phase-timeouts.sh
#
# Verifies that the checker:
#   - passes on a Python phase-loop method using a per-phase table lookup
#   - fails on a Python phase-loop method referencing a plain global timeout
#   - passes when the global timeout carries a global-by-design annotation
#   - fails on a Bash phase-loop function referencing a plain global timeout
#   - passes when the Bash constant carries a global-by-design annotation
#   - passes for functions exempt as *_for_phase implementations
#   - passes when the token name embeds a phase name
#   - passes on the real scripts/dispatcher tree
#
# Usage:
#   scripts/tests/test_check_per_phase_timeouts.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-per-phase-timeouts.sh"
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

# ─── T1: Python phase-loop method with per-phase table lookup → PASS ─────────
write_file "t1/daemon.py" <<'EOF'
STUCK_TIMEOUT_SECONDS_BY_PHASE = {"ralph": 900, "planning": 600}

class D:
    def _dispatch_phase(self, agent_id, phase):
        t = STUCK_TIMEOUT_SECONDS_BY_PHASE.get(phase, 300)
        return t
EOF
assert_passes "T1: Python per-phase table lookup (BY_PHASE suffix) passes" \
    "$TMPDIR_TEST/t1"

# ─── T2: Python phase-loop method with unannotated global → FAIL ──────────────
write_file "t2/daemon.py" <<'EOF'
MY_NEW_TIMEOUT_SECONDS = 300

class D:
    def _dispatch_phase(self, agent_id, phase):
        return MY_NEW_TIMEOUT_SECONDS
EOF
assert_fails "T2: Python unannotated global timeout in phase-loop method fails" \
    "$TMPDIR_TEST/t2"

# ─── T3: Python phase-loop method with global-by-design annotation → PASS ────
write_file "t3/daemon.py" <<'EOF'
MY_NEW_TIMEOUT_SECONDS = 300  # global-by-design (#9999)

class D:
    def _dispatch_phase(self, agent_id, phase):
        return MY_NEW_TIMEOUT_SECONDS
EOF
assert_passes "T3: Python global-by-design annotation on declaration passes" \
    "$TMPDIR_TEST/t3"

# ─── T4: Bash phase-loop function with unannotated global → FAIL ──────────────
write_file "t4/entrypoint.sh" <<'EOF'
MY_NEW_TIMEOUT_SECONDS=300

run_claude_phase() {
    _phase="$1"
    timeout "$MY_NEW_TIMEOUT_SECONDS" echo "hello"
}
EOF
assert_fails "T4: Bash unannotated global timeout in phase-loop function fails" \
    "$TMPDIR_TEST/t4"

# ─── T5: Bash phase-loop function with global-by-design annotation → PASS ────
write_file "t5/entrypoint.sh" <<'EOF'
MY_NEW_TIMEOUT_SECONDS=300  # global-by-design (#9999)

run_claude_phase() {
    _phase="$1"
    timeout "$MY_NEW_TIMEOUT_SECONDS" echo "hello"
}
EOF
assert_passes "T5: Bash global-by-design annotation on declaration passes" \
    "$TMPDIR_TEST/t5"

# ─── T6: Reference inside *_for_phase (exempt impl function) → PASS ──────────
write_file "t6/daemon.py" <<'EOF'
STUCK_TIMEOUT_SECONDS = 300

class D:
    def _stuck_timeout_for_phase(self, phase):
        return STUCK_TIMEOUT_SECONDS
EOF
assert_passes "T6: impl function exemption (*_for_phase) passes without annotation" \
    "$TMPDIR_TEST/t6"

# ─── T7: Token name embeds a phase name → PASS ────────────────────────────────
write_file "t7/daemon.py" <<'EOF'
CLAUDE_PHASE_TIMEOUT_PLANNING_SECONDS = 1200

class D:
    def _dispatch_phase(self, agent_id, phase):
        return CLAUDE_PHASE_TIMEOUT_PLANNING_SECONDS
EOF
assert_passes "T7: token name embeds phase name (_PLANNING_) passes" \
    "$TMPDIR_TEST/t7"

# ─── T8: Real scripts/dispatcher tree passes ──────────────────────────────────
assert_passes "T8: real scripts/dispatcher tree passes" \
    "$REPO_ROOT/scripts/dispatcher"

# ─── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "$TESTS tests run, $FAILURES failed"
if [ "$FAILURES" -gt 0 ]; then
    exit 1
fi
exit 0
