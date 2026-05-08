#!/usr/bin/env bash
# test_cleanup_worktree_review_log_mirror.sh — Tests for the review-log
# S3 mirroring logic added to cleanup_worktree.sh (#2647).
#
# Exercises the _mirror_review_log_best_effort() helper that is called
# just before remove_worktree. Uses a bin-stubs directory on PATH to
# intercept the python3 call and assert it was invoked with the expected
# telemetry_upload.py path and S3 key.
#
# Usage:
#   scripts/tests/test_cleanup_worktree_review_log_mirror.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CLEANUP_SCRIPT="$REPO_ROOT/scripts/cleanup_worktree.sh"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

# Cleanup of temp directories on exit via the shared helper (see #4343).
. "$REPO_ROOT/scripts/tests/_temp_cleanup_helpers.sh"

make_temp_dir() {
    local dir
    dir=$(mktemp -d)
    register_temp_dir "$dir"
    echo "$dir"
}

pass() {
    TESTS=$((TESTS + 1))
    echo "PASS: $1"
}

fail() {
    TESTS=$((TESTS + 1))
    FAILURES=$((FAILURES + 1))
    echo "FAIL: $1"
    if [[ -n "${2:-}" ]]; then
        echo "  $2"
    fi
}

# Build a minimal real git repo with a .claude/worktrees/agent-<id> worktree.
# Sets globals: REPO WORKTREE AGENT_ID
setup_git_repo() {
    REPO=$(make_temp_dir)
    git -C "$REPO" init --initial-branch main -q
    git -C "$REPO" config user.email "test@example.com"
    git -C "$REPO" config user.name "Test"
    touch "$REPO/CLAUDE.md"
    git -C "$REPO" add CLAUDE.md
    git -C "$REPO" commit -m "init" -q

    AGENT_ID="deadbeef"
    local worktree_rel=".claude/worktrees/agent-${AGENT_ID}"
    mkdir -p "$(dirname "$REPO/$worktree_rel")"
    git -C "$REPO" worktree add -q "$REPO/$worktree_rel" -b "test-branch-${AGENT_ID}"
    WORKTREE="$REPO/$worktree_rel"

    # Copy cleanup script into the repo
    mkdir -p "$REPO/scripts"
    cp "$CLEANUP_SCRIPT" "$REPO/scripts/cleanup_worktree.sh"
    chmod +x "$REPO/scripts/cleanup_worktree.sh"

    # Write telemetry_upload.py stub into the repo's scripts/ dir
    cat > "$REPO/scripts/telemetry_upload.py" <<'PYEOF'
#!/usr/bin/env python3
"""Stub telemetry_upload for test environments."""
import sys
ok = True
print(f"STUB telemetry_upload: {' '.join(sys.argv[1:])}", file=sys.stderr)
sys.exit(0 if ok else 1)
PYEOF
}

# ── Tests ──────────────────────────────────────────────────────────────────

# Test 1: _mirror_review_log_best_effort uploads when review-log.jsonl exists
test_mirror_uploads_when_review_log_exists() {
    setup_git_repo

    # Create a synthetic ralph state dir with a non-empty review-log.jsonl
    local ralph_dir="$WORKTREE/tmp/ralph"
    mkdir -p "$ralph_dir"
    echo '{"type": "summary", "final_verdict": "SHIP"}' > "$ralph_dir/review-log.jsonl"
    echo "# Issue #2647 — Ralph review-log S3 mirror" > "$ralph_dir/task.md"

    # Create a stub HOME/.claude/projects dir so agent_is_finished passes
    local stub_home
    stub_home=$(make_temp_dir)
    mkdir -p "$stub_home/.claude/projects"

    # Create a synthetic JSONL session log so cleanup can verify agent finished
    local session_jsonl="$stub_home/.claude/projects/agent-${AGENT_ID}-session.jsonl"
    printf '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"done"}]}}\n' \
        > "$session_jsonl"

    # Capture stderr output from the cleanup script — python3 call should appear
    local output
    local exit_code=0
    output=$(HOME="$stub_home" bash "$REPO/scripts/cleanup_worktree.sh" \
        ".claude/worktrees/agent-${AGENT_ID}" 2>&1) || exit_code=$?

    if echo "$output" | grep -q "telemetry_upload"; then
        pass "_mirror_review_log_best_effort: python3 telemetry_upload.py was invoked"
    else
        fail "_mirror_review_log_best_effort: python3 telemetry_upload.py was invoked" \
            "expected 'telemetry_upload' in output: $output (exit=$exit_code)"
    fi
}

# Test 2: review-log key includes the issue number derived from task.md
test_mirror_key_includes_issue_number() {
    setup_git_repo

    local ralph_dir="$WORKTREE/tmp/ralph"
    mkdir -p "$ralph_dir"
    echo '{"type": "summary"}' > "$ralph_dir/review-log.jsonl"
    echo "# Issue #2647 — Ralph review-log S3 mirror" > "$ralph_dir/task.md"

    local stub_home
    stub_home=$(make_temp_dir)
    mkdir -p "$stub_home/.claude/projects"
    local session_jsonl="$stub_home/.claude/projects/agent-${AGENT_ID}-session.jsonl"
    printf '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"done"}]}}\n' \
        > "$session_jsonl"

    local output
    local exit_code=0
    output=$(HOME="$stub_home" bash "$REPO/scripts/cleanup_worktree.sh" \
        ".claude/worktrees/agent-${AGENT_ID}" 2>&1) || exit_code=$?

    if echo "$output" | grep -q "2647"; then
        pass "_mirror_review_log_best_effort: S3 key includes issue number 2647"
    else
        fail "_mirror_review_log_best_effort: S3 key includes issue number 2647" \
            "expected '2647' in output: $output"
    fi
}

# Test 3: no upload when review-log.jsonl is absent
test_no_upload_when_no_review_log() {
    setup_git_repo

    # Do NOT create tmp/ralph/review-log.jsonl — only the worktree dir
    mkdir -p "$WORKTREE/tmp/ralph"

    local stub_home
    stub_home=$(make_temp_dir)
    mkdir -p "$stub_home/.claude/projects"
    local session_jsonl="$stub_home/.claude/projects/agent-${AGENT_ID}-session.jsonl"
    printf '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"done"}]}}\n' \
        > "$session_jsonl"

    local output
    local exit_code=0
    output=$(HOME="$stub_home" bash "$REPO/scripts/cleanup_worktree.sh" \
        ".claude/worktrees/agent-${AGENT_ID}" 2>&1) || exit_code=$?

    if echo "$output" | grep -q "telemetry_upload"; then
        fail "no upload when review-log.jsonl absent" \
            "unexpected telemetry_upload call: $output"
    else
        pass "no upload when review-log.jsonl absent"
    fi
}

# Test 4: no upload when review-log.jsonl is empty
test_no_upload_when_review_log_empty() {
    setup_git_repo

    local ralph_dir="$WORKTREE/tmp/ralph"
    mkdir -p "$ralph_dir"
    # Create a zero-byte file
    > "$ralph_dir/review-log.jsonl"

    local stub_home
    stub_home=$(make_temp_dir)
    mkdir -p "$stub_home/.claude/projects"
    local session_jsonl="$stub_home/.claude/projects/agent-${AGENT_ID}-session.jsonl"
    printf '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"done"}]}}\n' \
        > "$session_jsonl"

    local output
    local exit_code=0
    output=$(HOME="$stub_home" bash "$REPO/scripts/cleanup_worktree.sh" \
        ".claude/worktrees/agent-${AGENT_ID}" 2>&1) || exit_code=$?

    if echo "$output" | grep -q "telemetry_upload"; then
        fail "no upload when review-log.jsonl is empty" \
            "unexpected telemetry_upload call: $output"
    else
        pass "no upload when review-log.jsonl is empty"
    fi
}

# Test 5: cleanup succeeds even if the upload fails (non-zero python3 exit)
test_cleanup_succeeds_even_if_upload_fails() {
    setup_git_repo

    local ralph_dir="$WORKTREE/tmp/ralph"
    mkdir -p "$ralph_dir"
    echo '{"type": "summary"}' > "$ralph_dir/review-log.jsonl"

    # Overwrite telemetry_upload.py with a stub that exits 1 (simulate S3 failure)
    cat > "$REPO/scripts/telemetry_upload.py" <<'PYEOF'
#!/usr/bin/env python3
import sys
print("STUB telemetry_upload: upload failed", file=sys.stderr)
sys.exit(1)
PYEOF

    local stub_home
    stub_home=$(make_temp_dir)
    mkdir -p "$stub_home/.claude/projects"
    local session_jsonl="$stub_home/.claude/projects/agent-${AGENT_ID}-session.jsonl"
    printf '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"done"}]}}\n' \
        > "$session_jsonl"

    local exit_code=0
    HOME="$stub_home" bash "$REPO/scripts/cleanup_worktree.sh" \
        ".claude/worktrees/agent-${AGENT_ID}" >/dev/null 2>&1 || exit_code=$?

    if [[ "$exit_code" -eq 0 ]]; then
        pass "cleanup succeeds even if upload fails"
    else
        fail "cleanup succeeds even if upload fails" \
            "expected exit 0, got $exit_code"
    fi
}

# ── Run all tests ──────────────────────────────────────────────────────────

test_mirror_uploads_when_review_log_exists
test_mirror_key_includes_issue_number
test_no_upload_when_no_review_log
test_no_upload_when_review_log_empty
test_cleanup_succeeds_even_if_upload_fails

echo ""
echo "────────────────────────────────────────────"
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
