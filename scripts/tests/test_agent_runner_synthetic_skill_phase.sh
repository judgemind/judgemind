#!/usr/bin/env bash
# test_agent_runner_synthetic_skill_phase.sh — Issue #3374.
#
# Asserts the agent-runner-entrypoint dispatches synthetic
# scheduled-skill agents (``kind='scheduled_skill'``) to
# ``claude -p /<phase> <agent_id>`` and treats the result correctly
# (success → status='succeeded', phase='done'; failure →
# status='failed', phase='scheduled_skill_failed').
#
# Asserts:
#   * phase=audit, kind=scheduled_skill → invokes ``claude -p /audit``
#     and advances to ``done`` / ``succeeded`` on a SHIPPED-style
#     verdict.
#   * phase=spotcheck, kind=scheduled_skill → invokes
#     ``claude -p /spotcheck`` and advances to done/succeeded on
#     verdict OK.
#   * The synthetic-skill path does NOT call the standard plan/ralph/
#     push_and_pr machinery — no ``/task-v2-plan`` invocation, no
#     ``gh pr create``.
#
# This test reuses the same stub-PATH pattern as
# ``test_agent_runner_entrypoint.sh`` but with a much smaller harness
# focused on the new synthetic-skill case.
#
# macOS bash 3.2 compatible.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENTRYPOINT="$REPO_ROOT/scripts/dispatcher/agent-runner-entrypoint.sh"
FAILURES=0
TESTS=0

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

TEST_TMP=""
cleanup() {
    set +eu
    if [[ "${TEST_AGENT_RUNNER_KEEP_TMP:-0}" == "1" ]]; then
        echo "TEST_AGENT_RUNNER_KEEP_TMP=1 — keeping $TEST_TMP for inspection"
        return
    fi
    if [[ -n "$TEST_TMP" && -d "$TEST_TMP" ]]; then
        rm -rf "$TEST_TMP"
    fi
}
trap cleanup EXIT

TEST_TMP=$(mktemp -d)
STUB_BIN="$TEST_TMP/bin-stubs"
INVOCATIONS_DIR="$TEST_TMP/invocations"
mkdir -p "$STUB_BIN" "$INVOCATIONS_DIR"

# ── Stub utility ───────────────────────────────────────────────────────────
cp "$REPO_ROOT/scripts/tests/_record_invocation.sh" "$STUB_BIN/_record_invocation.sh"

# ── psql stub ──────────────────────────────────────────────────────────────
# Routing: phase lookup → PHASE_FIXTURE_FILE; kind lookup → KIND_FIXTURE.
cat > "$STUB_BIN/psql" <<'PSQLEOF'
#!/usr/bin/env bash
set -u
INVOCATIONS_DIR="${INVOCATIONS_DIR}"
. "$(dirname "$0")/_record_invocation.sh" psql "$@"

query=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -c)
            shift
            query="$1"
            ;;
    esac
    shift || true
done

# When -c was absent, fall back to stdin captured by the shared recorder.
if [[ -z "$query" ]]; then
    query="${stdin_content:-}"
fi

case "$query" in
    *"SELECT phase"*"FROM dispatcher.agents"*)
        if [[ -f "${PHASE_FIXTURE_FILE:-}" ]]; then
            head -n 1 "$PHASE_FIXTURE_FILE"
        fi
        exit 0
        ;;
    *"SELECT kind"*"FROM dispatcher.agents"*)
        if [[ -n "${KIND_FIXTURE:-}" ]]; then
            printf '%s\n' "$KIND_FIXTURE"
        fi
        exit 0
        ;;
    *"UPDATE dispatcher.agents"*"SET phase ="*)
        new_phase=$(printf '%s' "$query" | sed -n "s/.*SET phase = '\\([^']*\\)'.*/\\1/p")
        if [[ -n "$new_phase" && -f "${PHASE_FIXTURE_FILE:-}" ]]; then
            printf '%s\n' "$new_phase" > "$PHASE_FIXTURE_FILE"
        fi
        exit 0
        ;;
    *"UPDATE dispatcher.agents"*"SET ended_at"*)
        exit 0
        ;;
    *"INSERT INTO dispatcher.phase_outputs"*)
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
PSQLEOF
chmod +x "$STUB_BIN/psql"

# ── claude stub ────────────────────────────────────────────────────────────
# Records the slash-command invoked and emits a canned envelope. Honors
# CLAUDE_VERDICT_OVERRIDE for synthetic-skill verdict assertions.
cat > "$STUB_BIN/claude" <<'CLAUDEEOF'
#!/usr/bin/env bash
set -u
INVOCATIONS_DIR="${INVOCATIONS_DIR}"
. "$(dirname "$0")/_record_invocation.sh" claude "$@"

# Record the first slash-command argv arg into a separate log.
for arg in "$@"; do
    case "$arg" in
        /*)
            cmd=$(printf '%s' "$arg" | awk '{print $1}')
            printf '%s\n' "$cmd" >> "$INVOCATIONS_DIR/claude-slash-cmd.log"
            break
            ;;
    esac
done

# Emit the requested envelope. Default = SHIPPED-style success result.
if [[ -n "${CLAUDE_RESULT_OVERRIDE:-}" ]]; then
    printf '%s\n' "$CLAUDE_RESULT_OVERRIDE"
else
    printf '{"result": "SHIPPED — filed 3 issues"}\n'
fi
exit "${CLAUDE_EXIT_OVERRIDE:-0}"
CLAUDEEOF
chmod +x "$STUB_BIN/claude"

# ── git / gh stubs ─────────────────────────────────────────────────────────
# Synthetic skills don't run push_and_pr, but the entrypoint's pre-loop
# clone path still calls git. Stub minimal subcommands.
cat > "$STUB_BIN/git" <<'GITEOF'
#!/usr/bin/env bash
set -u
INVOCATIONS_DIR="${INVOCATIONS_DIR}"
. "$(dirname "$0")/_record_invocation.sh" git "$@"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -C) shift 2 || true; continue ;;
        *) break ;;
    esac
done

case "${1:-}" in
    clone)
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --depth=*|--no-tags|clone|https://*) shift ;;
                *) target="$1"; mkdir -p "$target/.git"; exit 0 ;;
            esac
        done
        exit 0
        ;;
    rev-parse) printf 'deadbeefcafe\n'; exit 0 ;;
    *) exit 0 ;;
esac
GITEOF
chmod +x "$STUB_BIN/git"

cat > "$STUB_BIN/gh" <<'GHEOF'
#!/usr/bin/env bash
set -u
INVOCATIONS_DIR="${INVOCATIONS_DIR}"
. "$(dirname "$0")/_record_invocation.sh" gh "$@"
exit 0
GHEOF
chmod +x "$STUB_BIN/gh"

# ── jq required ────────────────────────────────────────────────────────────
if ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq not on PATH; entrypoint requires it"
    exit 0
fi

setup_fixtures() {
    : > "$INVOCATIONS_DIR/psql.log"
    : > "$INVOCATIONS_DIR/claude.log"
    : > "$INVOCATIONS_DIR/claude-slash-cmd.log"
    : > "$INVOCATIONS_DIR/git.log"
    : > "$INVOCATIONS_DIR/gh.log"
}

# ══════════════════════════════════════════════════════════════════════════
# Test 1: phase=audit, kind=scheduled_skill → /audit invocation, success
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-audit.txt"
printf 'audit\n' > "$PHASE_FIXTURE_FILE"
KIND_FIXTURE="scheduled_skill"
audit_workspace="$TEST_TMP/audit-workspace"
mkdir -p "$audit_workspace"

set +e
out=$(AGENT_ID="aaaa1111-2222-3333-4444-555555555555" \
      ISSUE_NUMBER="" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$audit_workspace" \
      REPO_URL="https://example.invalid/repo.git" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
      KIND_FIXTURE="$KIND_FIXTURE" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      AGENT_RUNNER_MAX_PHASE_ITERATIONS=10 \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

if [[ $rc -eq 0 ]]; then
    pass "scheduled_skill audit exits cleanly (rc=0)"
else
    fail "scheduled_skill audit exits cleanly" "rc=$rc, out tail: $(printf '%s\n' "$out" | tail -20)"
fi

# Asserts /audit was invoked (the entrypoint prepends ``/`` before the
# phase name and passes it as a single ``-p`` value).
if grep -q '^/audit' "$INVOCATIONS_DIR/claude-slash-cmd.log"; then
    pass "audit phase invokes claude -p /audit"
else
    fail "audit phase invokes claude -p /audit" \
         "claude-slash-cmd.log: $(cat "$INVOCATIONS_DIR/claude-slash-cmd.log")"
fi

# Should NOT call /task-v2-plan or any pipeline skill.
if grep -q '^/task-v2-' "$INVOCATIONS_DIR/claude-slash-cmd.log"; then
    fail "audit phase does not invoke pipeline skills" \
         "found /task-v2-* invocations: $(grep '^/task-v2-' "$INVOCATIONS_DIR/claude-slash-cmd.log")"
else
    pass "audit phase does not invoke any /task-v2-* pipeline skill"
fi

# Should advance to terminal 'done' (success path).
final_phase=$(cat "$PHASE_FIXTURE_FILE")
if [[ "$final_phase" == "done" ]]; then
    pass "audit phase ends at terminal 'done' on success"
else
    fail "audit phase ends at terminal 'done' on success" "final phase: $final_phase"
fi

# Should log the scheduled_skill_succeeded event.
if printf '%s' "$out" | grep -q "scheduled_skill_succeeded"; then
    pass "logs scheduled_skill_succeeded event on success"
else
    fail "logs scheduled_skill_succeeded event on success" "out tail: $(printf '%s\n' "$out" | tail -10)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 2: phase=spotcheck, kind=scheduled_skill → /spotcheck invocation
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-spotcheck.txt"
printf 'spotcheck\n' > "$PHASE_FIXTURE_FILE"
KIND_FIXTURE="scheduled_skill"
spot_workspace="$TEST_TMP/spotcheck-workspace"
mkdir -p "$spot_workspace"

set +e
out=$(AGENT_ID="bbbb1111-2222-3333-4444-555555555555" \
      ISSUE_NUMBER="" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$spot_workspace" \
      REPO_URL="https://example.invalid/repo.git" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
      KIND_FIXTURE="$KIND_FIXTURE" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      CLAUDE_RESULT_OVERRIDE='{"result": {"verdict": "OK"}}' \
      AGENT_RUNNER_MAX_PHASE_ITERATIONS=10 \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

if [[ $rc -eq 0 ]]; then
    pass "scheduled_skill spotcheck exits cleanly (rc=0)"
else
    fail "scheduled_skill spotcheck exits cleanly" "rc=$rc, out tail: $(printf '%s\n' "$out" | tail -20)"
fi

if grep -q '^/spotcheck' "$INVOCATIONS_DIR/claude-slash-cmd.log"; then
    pass "spotcheck phase invokes claude -p /spotcheck"
else
    fail "spotcheck phase invokes claude -p /spotcheck" \
         "claude-slash-cmd.log: $(cat "$INVOCATIONS_DIR/claude-slash-cmd.log")"
fi

final_phase=$(cat "$PHASE_FIXTURE_FILE")
if [[ "$final_phase" == "done" ]]; then
    pass "spotcheck phase ends at terminal 'done' with verdict=OK"
else
    fail "spotcheck phase ends at terminal 'done' with verdict=OK" "final: $final_phase"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 3: claude exits non-zero → phase advances to scheduled_skill_failed
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-audit-fail.txt"
printf 'audit\n' > "$PHASE_FIXTURE_FILE"
KIND_FIXTURE="scheduled_skill"
fail_workspace="$TEST_TMP/audit-fail-workspace"
mkdir -p "$fail_workspace"

set +e
out=$(AGENT_ID="cccc1111-2222-3333-4444-555555555555" \
      ISSUE_NUMBER="" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$fail_workspace" \
      REPO_URL="https://example.invalid/repo.git" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
      KIND_FIXTURE="$KIND_FIXTURE" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      CLAUDE_EXIT_OVERRIDE=1 \
      CLAUDE_RESULT_OVERRIDE='' \
      AGENT_RUNNER_MAX_PHASE_ITERATIONS=10 \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

# The entrypoint's main loop exits 0 because the failure terminal is a
# proper terminal phase (the agent's status reflects the failure, not
# the entrypoint exit code).
final_phase=$(cat "$PHASE_FIXTURE_FILE")
if [[ "$final_phase" == "scheduled_skill_failed" ]]; then
    pass "claude non-zero exit advances to scheduled_skill_failed"
else
    fail "claude non-zero exit advances to scheduled_skill_failed" "final: $final_phase, rc=$rc"
fi

# Should log the scheduled_skill_failed event.
if printf '%s' "$out" | grep -q "scheduled_skill_failed"; then
    pass "logs scheduled_skill_failed event on non-zero exit"
else
    fail "logs scheduled_skill_failed event on non-zero exit" "out tail: $(printf '%s\n' "$out" | tail -20)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════════════
echo "----"
echo "Tests: $TESTS, Failures: $FAILURES"
exit "$FAILURES"
