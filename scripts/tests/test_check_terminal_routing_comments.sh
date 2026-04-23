#!/usr/bin/env bash
# test_check_terminal_routing_comments.sh — Tests for
# scripts/check-terminal-routing-comments.sh
#
# Verifies that the checker fails on synthetic daemon.py contents
# with unannotated ``_mark_agent_terminal`` calls and passes on
# well-annotated variants (including the cluster docstring pattern
# introduced by #3062). Also exercises the variable-issue-number regex
# (``ROUTING (#XXXX)`` for future audits) and regression-tests the
# real ``scripts/dispatcher/daemon.py`` in the repo.
#
# Usage:
#   scripts/tests/test_check_terminal_routing_comments.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-terminal-routing-comments.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST="$(mktemp -d)"
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

write_file() {
    # $1: file path (relative to TMPDIR_TEST)
    # $2: contents (via stdin)
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

# ─── Test 1: Known-good per-site comment — 1 line above the call ────────
write_file "good_1line_above.py" <<'EOF'
class D:
    def foo(self):
        # ROUTING (#3062): intentional unrouted — defensive catch.
        self._mark_agent_terminal(
            "agent", status="failed", phase="foo", exit_code=None
        )
EOF
assert_passes "ROUTING one line above the call passes" \
    "$TMPDIR_TEST/good_1line_above.py"

# ─── Test 2: Known-good per-site comment — 5 lines above the call ───────
write_file "good_5lines_above.py" <<'EOF'
class D:
    def foo(self):
        # ROUTING (#3062): intentional unrouted — defensive catch.
        #
        #
        #
        self._mark_agent_terminal(
            "agent", status="failed", phase="foo", exit_code=None
        )
EOF
assert_passes "ROUTING five lines above the call passes" \
    "$TMPDIR_TEST/good_5lines_above.py"

# ─── Test 3: Known-bad — no ROUTING comment anywhere ────────────────────
write_file "bad_no_comment.py" <<'EOF'
class D:
    def foo(self):
        # Some unrelated comment that isn't a ROUTING annotation.
        self._mark_agent_terminal(
            "agent", status="failed", phase="foo", exit_code=None
        )
EOF
assert_fails "No ROUTING comment fails" \
    "$TMPDIR_TEST/bad_no_comment.py"

# ─── Test 4: Cluster pattern — one docstring covers multiple calls ──────
# This mirrors the ``_apply_fix_ci_patch`` shape in daemon.py (one
# docstring at the top of the method covers every ``_mark_agent_terminal``
# call below it, each within the 250-line window).
write_file "good_cluster.py" <<'EOF'
class D:
    def apply_patch(self):
        """Apply a patch.

        ROUTING (#3062): the three ``_mark_agent_terminal(status='failed')``
        call sites inside this method are NOT routed through
        ``_handle_agent_failure``. They cover every git-add / git-commit /
        git-push failure mode. Follow-up #3069 proposes routing the cluster.
        """
        if one:
            self._mark_agent_terminal(
                "a", status="failed", phase="p", exit_code=None
            )
            return
        if two:
            self._mark_agent_terminal(
                "a", status="failed", phase="p", exit_code=None
            )
            return
        self._mark_agent_terminal(
            "a", status="failed", phase="p", exit_code=None
        )
EOF
assert_passes "Cluster docstring ROUTING covers multiple downstream calls" \
    "$TMPDIR_TEST/good_cluster.py"

# ─── Test 5: Known-good — ROUTING with a different issue number ─────────
# The regex must accept ``ROUTING (#<N>)`` for any N, not just #3062,
# so future audits can introduce their own ROUTING variants.
write_file "good_other_issue.py" <<'EOF'
class D:
    def foo(self):
        # ROUTING (#9999): future audit follow-up.
        self._mark_agent_terminal(
            "agent", status="failed", phase="foo", exit_code=None
        )
EOF
assert_passes "ROUTING with a non-#3062 issue number passes" \
    "$TMPDIR_TEST/good_other_issue.py"

# ─── Test 6: Known-bad — comment has wrong token (no ROUTING prefix) ────
write_file "bad_wrong_token.py" <<'EOF'
class D:
    def foo(self):
        # ROUTE (#3062): typo — should say ROUTING.
        # See #3062 for the routing decision.
        self._mark_agent_terminal(
            "agent", status="failed", phase="foo", exit_code=None
        )
EOF
assert_fails "Missing ROUTING token (only mentioning #3062) fails" \
    "$TMPDIR_TEST/bad_wrong_token.py"

# ─── Test 7: Known-bad — comment has ROUTING but no issue reference ─────
write_file "bad_no_issue_number.py" <<'EOF'
class D:
    def foo(self):
        # ROUTING: intentional unrouted — but no issue reference.
        self._mark_agent_terminal(
            "agent", status="failed", phase="foo", exit_code=None
        )
EOF
assert_fails "ROUTING without (#N) reference fails" \
    "$TMPDIR_TEST/bad_no_issue_number.py"

# ─── Test 8: Empty/missing file treated as nothing-to-check ─────────────
assert_passes "Missing target file exits 0 (nothing to check)" \
    "$TMPDIR_TEST/does_not_exist.py"

# ─── Test 9: File with no call sites passes ─────────────────────────────
write_file "no_calls.py" <<'EOF'
class D:
    def foo(self):
        # ROUTING (#3062): no call site here.
        pass
EOF
assert_passes "File with no _mark_agent_terminal call sites passes" \
    "$TMPDIR_TEST/no_calls.py"

# ─── Test 10: Mixed — first call annotated, second not ──────────────────
write_file "bad_mixed.py" <<'EOF'
class D:
    def first(self):
        # ROUTING (#3062): annotated correctly.
        self._mark_agent_terminal(
            "a", status="failed", phase="p", exit_code=None
        )

    def second(self):
        # No ROUTING comment anywhere in this function.
        # (The ROUTING above is outside the window.)
        self._mark_agent_terminal(
            "a", status="failed", phase="p", exit_code=None
        )
EOF
# NOTE: with a 250-line window, the ROUTING at line 3 actually DOES
# cover the second call at line ~11 — that is the intended lenience
# for cluster patterns. Force the second call outside the window
# using a large gap.
write_file "bad_mixed_wide_gap.py" <<EOF
class D:
    def first(self):
        # ROUTING (#3062): annotated correctly.
        self._mark_agent_terminal(
            "a", status="failed", phase="p", exit_code=None
        )

$(printf "    # filler\n%.0s" {1..260})

    def second(self):
        # No ROUTING comment within the 250-line window.
        self._mark_agent_terminal(
            "a", status="failed", phase="p", exit_code=None
        )
EOF
assert_fails "Second call outside the ROUTING window fails even if first is annotated" \
    "$TMPDIR_TEST/bad_mixed_wide_gap.py"

# ─── Test 11: Call line itself contains ROUTING — does not self-satisfy ─
# A ROUTING comment on the same line as the call is still a valid
# annotation (the comment sits above or inline with the call); but a
# ROUTING token appearing inside a string literal could false-positive.
# Our text-based grep does not try to distinguish — this is noted as
# an accepted tradeoff in the script header, and the test documents
# the current behavior.
write_file "good_inline.py" <<'EOF'
class D:
    def foo(self):
        self._mark_agent_terminal("a")  # ROUTING (#3062) inline.
EOF
assert_passes "Inline ROUTING on the call line counts as annotation" \
    "$TMPDIR_TEST/good_inline.py"

# ─── Test 12: Custom window via env var (regression guard) ──────────────
# Set ROUTING_WINDOW_LINES=2 and re-run the 5-lines-above fixture from
# Test 2 — the 5-line gap should now exceed the 2-line window and fail.
TESTS=$((TESTS + 1))
if ROUTING_WINDOW_LINES=2 "$CHECK_SCRIPT" \
        "$TMPDIR_TEST/good_5lines_above.py" > /dev/null 2>&1; then
    echo "FAIL: Custom ROUTING_WINDOW_LINES=2 should reject the 5-lines-above fixture"
    FAILURES=$((FAILURES + 1))
else
    echo "PASS: Custom ROUTING_WINDOW_LINES=2 correctly tightens the check"
fi

# ─── Test 13: Regression — the real daemon.py passes ────────────────────
# The whole point of this check is that the current (post-#3073)
# ``scripts/dispatcher/daemon.py`` satisfies it. If this fails, either
# the daemon drifted or the script regressed.
DAEMON_PATH="$REPO_ROOT/scripts/dispatcher/daemon.py"
if [[ -f "$DAEMON_PATH" ]]; then
    assert_passes "Real scripts/dispatcher/daemon.py passes (post-#3073 annotations)" \
        "$DAEMON_PATH"
else
    echo "SKIP: $DAEMON_PATH not present — skipping regression test"
fi

# ─── Summary ────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
