#!/usr/bin/env bash
# test_agent_runner_entrypoint.sh — Integration tests for
# scripts/dispatcher/agent-runner-entrypoint.sh (#3090, Stage 1b of
# #3086's per-agent ECS migration).
#
# What the tests exercise
# -----------------------
# The entrypoint is meant to run inside the `judgemind/dispatcher-
# agent-runner` container; it calls `git`, `gh`, `psql`, `claude`, and
# `aws` via PATH. These tests stub every one of those binaries with a
# PATH shim that writes its argv + stdin to a file we can assert on
# afterward, then drive the entrypoint across a short happy-path
# phase sequence.
#
# The tests deliberately do NOT spawn a real container — they run the
# entrypoint shell script directly on whatever bash is in `$PATH`, so
# the check-bash-compat guard also protects these assertions from
# silently drifting onto bash 4+ features.
#
# macOS bash 3.2 compatibility
# ----------------------------
# No bash 4+ features (`mapfile`, assoc arrays, `${var,,}`, etc.). The
# script uses parallel indexed arrays and classic `while IFS= read`.
#
# Usage:
#   scripts/tests/test_agent_runner_entrypoint.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENTRYPOINT="$REPO_ROOT/scripts/dispatcher/agent-runner-entrypoint.sh"
FAILURES=0
TESTS=0

# ── Logging helpers ────────────────────────────────────────────────────────

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

# ── Temp dir lifecycle ────────────────────────────────────────────────────

TEST_TMP=""
cleanup() {
    set +eu
    if [[ -n "$TEST_TMP" && -d "$TEST_TMP" ]]; then
        rm -rf "$TEST_TMP"
    fi
}
trap cleanup EXIT

TEST_TMP=$(mktemp -d)

# ── Build a stub-bin directory on PATH ─────────────────────────────────────
#
# Every stub writes its argv (one arg per line) to a per-binary log
# file under $TEST_TMP/invocations/, and — for binaries we're reading
# output from — prints a canned response on stdout.

STUB_BIN="$TEST_TMP/bin-stubs"
INVOCATIONS_DIR="$TEST_TMP/invocations"
mkdir -p "$STUB_BIN" "$INVOCATIONS_DIR"

# Shared stub utility — each stub sources this to record its invocation.
cat > "$STUB_BIN/_record_invocation.sh" <<'RECORDEOF'
# Source this to record argv into $INVOCATIONS_DIR/<tool>.log, one
# invocation per line starting with a count marker. Pass the tool name
# as $1, remaining args as $2+.
TOOL_NAME="$1"
shift
INVOCATIONS_LOG="${INVOCATIONS_DIR:-/tmp}/${TOOL_NAME}.log"
{
    printf 'CALL '
    for arg in "$@"; do
        printf '%q ' "$arg"
    done
    printf '\n'
} >> "$INVOCATIONS_LOG"
RECORDEOF

# ── psql stub ──────────────────────────────────────────────────────────────
# Responds based on the query substring:
#   * phase lookup → reads $PHASE_FIXTURE_FILE (starts at "claiming",
#     walks forward each call, ending at "done").
#   * UPDATE to agents.phase → also updates PHASE_FIXTURE_FILE so the
#     next lookup sees the new phase.
#   * SELECT from ralph_patches → returns $PRIOR_PATCH_FIXTURE contents.
#   * INSERT ralph_patches / phase_outputs → record only.

cat > "$STUB_BIN/psql" <<'PSQLEOF'
#!/usr/bin/env bash
set -u
INVOCATIONS_DIR="${INVOCATIONS_DIR}"
# shellcheck disable=SC1091
. "$(dirname "$0")/_record_invocation.sh" psql "$@"

# Parse args for -c <query>.
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

# Routing — purely by substring match on the SQL.
case "$query" in
    *"SELECT phase"*"FROM dispatcher.agents"*)
        if [[ -f "${PHASE_FIXTURE_FILE:-}" ]]; then
            head -n 1 "$PHASE_FIXTURE_FILE"
        fi
        exit 0
        ;;
    *"SELECT patch_content"*)
        if [[ -f "${PRIOR_PATCH_FIXTURE:-}" ]]; then
            cat "$PRIOR_PATCH_FIXTURE"
        fi
        exit 0
        ;;
    *"UPDATE dispatcher.agents"*"SET phase ="*)
        # Extract the new phase ('xxx' after SET phase = ').
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
    *"INSERT INTO dispatcher.ralph_patches"*)
        exit 0
        ;;
    *)
        # Unknown query — silent success so we don't break the script.
        exit 0
        ;;
esac
PSQLEOF
chmod +x "$STUB_BIN/psql"

# ── claude stub ────────────────────────────────────────────────────────────
# Emits a `{"result": {"verdict": "..."}}` envelope per
# CLAUDE_VERDICT_FIXTURE file contents (one verdict per phase).

cat > "$STUB_BIN/claude" <<'CLAUDEEOF'
#!/usr/bin/env bash
set -u
INVOCATIONS_DIR="${INVOCATIONS_DIR}"
. "$(dirname "$0")/_record_invocation.sh" claude "$@"

# Determine phase from /task-v2-<phase> in argv.
phase=""
for arg in "$@"; do
    case "$arg" in
        /task-v2-*)
            # Strip "/task-v2-" prefix and trailing agent id.
            stripped="${arg#/task-v2-}"
            phase="${stripped%% *}"
            break
            ;;
    esac
done

# Look up verdict for this phase from CLAUDE_VERDICT_FIXTURE (TSV).
verdict="SHIP"
if [[ -f "${CLAUDE_VERDICT_FIXTURE:-}" && -n "$phase" ]]; then
    match=$(grep -E "^${phase}	" "$CLAUDE_VERDICT_FIXTURE" 2>/dev/null || true)
    if [[ -n "$match" ]]; then
        verdict=$(printf '%s' "$match" | cut -f2)
    fi
fi

printf '{"result": {"verdict": "%s"}}\n' "$verdict"
exit 0
CLAUDEEOF
chmod +x "$STUB_BIN/claude"

# ── git stub ───────────────────────────────────────────────────────────────
# Only implements the subcommands the entrypoint calls. Returns
# realistic output for rev-parse + format-patch so persist_ralph_patch
# has something to work with.

cat > "$STUB_BIN/git" <<'GITEOF'
#!/usr/bin/env bash
set -u
INVOCATIONS_DIR="${INVOCATIONS_DIR}"
. "$(dirname "$0")/_record_invocation.sh" git "$@"

# Walk past -C <dir> options to find the subcommand.
while [[ $# -gt 0 ]]; do
    case "$1" in
        -C)
            shift 2 || true
            continue
            ;;
        *)
            break
            ;;
    esac
done

subcommand="${1:-}"
case "$subcommand" in
    clone)
        # Create the target dir with a .git marker so the entrypoint
        # treats it as an existing clone on subsequent runs.
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --depth=*|--no-tags|clone|https://*)
                    shift
                    ;;
                *)
                    target="$1"
                    mkdir -p "$target/.git"
                    exit 0
                    ;;
            esac
        done
        exit 0
        ;;
    rev-parse)
        printf 'deadbeefcafe\n'
        exit 0
        ;;
    format-patch)
        printf 'From deadbeefcafe Mon Sep 17 00:00:00 2001\nfake patch content\n'
        exit 0
        ;;
    am)
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
GITEOF
chmod +x "$STUB_BIN/git"

# ── gh stub ────────────────────────────────────────────────────────────────

cat > "$STUB_BIN/gh" <<'GHEOF'
#!/usr/bin/env bash
set -u
INVOCATIONS_DIR="${INVOCATIONS_DIR}"
. "$(dirname "$0")/_record_invocation.sh" gh "$@"
exit 0
GHEOF
chmod +x "$STUB_BIN/gh"

# ── jq — use the real one if available; skip tests otherwise ──────────────

if ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq not on PATH; skipping entrypoint integration tests."
    echo "Results: 0/0 passed (skipped)"
    exit 0
fi

# Copy the real jq to the stub dir so it takes precedence under the
# sandboxed PATH (the test's PATH swaps out $HOME/bin etc).
REAL_JQ="$(command -v jq)"
ln -sf "$REAL_JQ" "$STUB_BIN/jq"

# Real python3 from the system — phase_transitions_shim.py needs it.
ln -sf "$(command -v python3)" "$STUB_BIN/python3"
# date + sed + tr + printf + cut are shell built-ins or coreutils;
# the test inherits whatever the parent shell has on PATH.

# ── Test fixtures ──────────────────────────────────────────────────────────

setup_fixtures() {
    # Shared state across the test invocation. Each test resets them.
    : > "$INVOCATIONS_DIR/psql.log"
    : > "$INVOCATIONS_DIR/claude.log"
    : > "$INVOCATIONS_DIR/git.log"
    : > "$INVOCATIONS_DIR/gh.log"
}

# ══════════════════════════════════════════════════════════════════════════
# Test 1: Immediate terminal — agent row already at `done`
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t1.txt"
printf 'done\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""
CLAUDE_VERDICT_FIXTURE=""

# Run the entrypoint in a fresh $AGENT_WORKSPACE.
t1_workspace="$TEST_TMP/t1-workspace"
mkdir -p "$t1_workspace"

set +e
out=$(AGENT_ID="00000000-1111-2222-3333-444444444444" \
      ISSUE_NUMBER="9999" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$t1_workspace" \
      REPO_URL="https://example.invalid/repo.git" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
      PRIOR_PATCH_FIXTURE="$PRIOR_PATCH_FIXTURE" \
      CLAUDE_VERDICT_FIXTURE="$CLAUDE_VERDICT_FIXTURE" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

if [[ $rc -eq 0 ]]; then
    pass "terminal phase exits cleanly (rc=0)"
else
    fail "terminal phase exits cleanly" "rc=$rc, output: $(printf '%s\n' "$out" | tail -30)"
fi

if printf '%s' "$out" | grep -q "phase_terminal"; then
    pass "logs phase_terminal event on terminal phase"
else
    fail "logs phase_terminal event on terminal phase" "output: $out"
fi

if printf '%s' "$out" | grep -q "agent_ended"; then
    pass "logs agent_ended event when terminal reached"
else
    fail "logs agent_ended event when terminal reached" "output: $out"
fi

# Verify the SQL UPDATE for ended_at was issued.
if grep -q "SET ended_at = now()" "$INVOCATIONS_DIR/psql.log"; then
    pass "issues UPDATE dispatcher.agents SET ended_at on terminal"
else
    fail "issues UPDATE dispatcher.agents SET ended_at on terminal" "psql log: $(cat "$INVOCATIONS_DIR/psql.log")"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 2: Happy path — plan → ralph SHIP → summary → push_and_pr → ... → done
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t2.txt"
printf 'planning\n' > "$PHASE_FIXTURE_FILE"

CLAUDE_VERDICT_FIXTURE="$TEST_TMP/verdicts-t2.tsv"
cat > "$CLAUDE_VERDICT_FIXTURE" <<'EOF'
planning	OK
ralph	SHIP
summary	OK
push_and_pr	OK
fix_ci	PATCHED
verify	VERIFIED
EOF

PRIOR_PATCH_FIXTURE=""

t2_workspace="$TEST_TMP/t2-workspace"
mkdir -p "$t2_workspace"

set +e
out=$(AGENT_ID="11111111-2222-3333-4444-555555555555" \
      ISSUE_NUMBER="3090" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$t2_workspace" \
      REPO_URL="https://example.invalid/repo.git" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
      PRIOR_PATCH_FIXTURE="$PRIOR_PATCH_FIXTURE" \
      CLAUDE_VERDICT_FIXTURE="$CLAUDE_VERDICT_FIXTURE" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      AGENT_RUNNER_MAX_PHASE_ITERATIONS=40 \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

if [[ $rc -eq 0 ]]; then
    pass "happy-path pipeline exits cleanly (rc=0)"
else
    fail "happy-path pipeline exits cleanly" "rc=$rc, final phase: $(cat "$PHASE_FIXTURE_FILE" 2>/dev/null), output: $(printf '%s\n' "$out" | tail -40)"
fi

# Verify each expected phase had an INSERT into phase_outputs. Each
# INSERT invocation is a single `CALL ...` record in the psql log;
# the quoted SQL argument is printed via `printf '%q '`, which for
# multi-line SQL (our queries are triple-quoted in the script)
# produces a `$'...'` dollar-quoted shell token containing literal
# `\n` escape sequences — so the whole stanza ends up on one physical
# line. We grep the file for a line containing BOTH the INSERT token
# AND the phase name quoted.
for expected in planning ralph summary push_and_pr verify; do
    # `printf '%q '` output wraps single-quoted SQL fragments in a
    # `$'...'` token and escapes nested single-quotes as `\'`. So the
    # phase literal `'planning'` shows up in the log as `\'planning\'`.
    if grep -F "\\'${expected}\\'" "$INVOCATIONS_DIR/psql.log" \
         | grep -F "INSERT INTO dispatcher.phase_outputs" > /dev/null 2>&1; then
        pass "persists phase_outputs row for $expected"
    else
        fail "persists phase_outputs row for $expected" \
             "psql log sample: $(grep -m1 "INSERT INTO dispatcher.phase_outputs" "$INVOCATIONS_DIR/psql.log" | head -c 200)"
    fi
done

# Verify ralph_patches was inserted on ralph SHIP.
if grep -q "INSERT INTO dispatcher.ralph_patches" "$INVOCATIONS_DIR/psql.log"; then
    pass "inserts ralph_patches row on ralph SHIP"
else
    fail "inserts ralph_patches row on ralph SHIP" "psql log: $(cat "$INVOCATIONS_DIR/psql.log")"
fi

# Verify phase advanced through each expected transition.
# Count UPDATE SET phase statements — should be at least 8 (one per
# advance: planning→ralph, ralph→summary, summary→push_and_pr,
# push_and_pr→awaiting_ci, awaiting_ci→merge, merge→awaiting_deploy,
# awaiting_deploy→verify, verify→done).
update_count=$(grep -c "SET phase =" "$INVOCATIONS_DIR/psql.log" 2>/dev/null || true)
update_count=${update_count:-0}
if [[ "$update_count" -ge 7 ]]; then
    pass "phase advances through the happy-path sequence (updates=$update_count)"
else
    fail "phase advances through the happy-path sequence" "expected ≥7 phase updates, got $update_count"
fi

# Verify claude was invoked for each claude-driven phase.
claude_calls=$(wc -l < "$INVOCATIONS_DIR/claude.log" | tr -d ' ')
if [[ "$claude_calls" -ge 5 ]]; then
    pass "claude invoked for each claude-driven phase (calls=$claude_calls)"
else
    fail "claude invoked for each claude-driven phase" "expected ≥5 claude calls, got $claude_calls"
fi

# Verify final phase is done.
final_phase=$(cat "$PHASE_FIXTURE_FILE")
if [[ "$final_phase" == "done" ]]; then
    pass "final phase is done"
else
    fail "final phase is done" "actual final phase: $final_phase"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 3: Prior ralph patch applied on boot
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t3.txt"
printf 'done\n' > "$PHASE_FIXTURE_FILE"

PRIOR_PATCH_FIXTURE="$TEST_TMP/prior-patch-t3.txt"
cat > "$PRIOR_PATCH_FIXTURE" <<'PATCHEOF'
From deadbeef Mon Sep 17 00:00:00 2001
Subject: [PATCH] prior ralph SHIP

 file.txt | 1 +
 1 file changed, 1 insertion(+)
PATCHEOF

CLAUDE_VERDICT_FIXTURE=""
t3_workspace="$TEST_TMP/t3-workspace"
mkdir -p "$t3_workspace"

set +e
out=$(AGENT_ID="22222222-3333-4444-5555-666666666666" \
      ISSUE_NUMBER="1234" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$t3_workspace" \
      REPO_URL="https://example.invalid/repo.git" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
      PRIOR_PATCH_FIXTURE="$PRIOR_PATCH_FIXTURE" \
      CLAUDE_VERDICT_FIXTURE="$CLAUDE_VERDICT_FIXTURE" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

if [[ $rc -eq 0 ]]; then
    pass "prior-patch boot completes cleanly (rc=0)"
else
    fail "prior-patch boot completes cleanly" "rc=$rc"
fi

if printf '%s' "$out" | grep -q "prior_patch_apply_begin"; then
    pass "logs prior_patch_apply_begin when patch fixture present"
else
    fail "logs prior_patch_apply_begin when patch fixture present" "output: $out"
fi

if grep -q "am --3way" "$INVOCATIONS_DIR/git.log"; then
    pass "invokes git am --3way on prior patch"
else
    fail "invokes git am --3way on prior patch" "git log: $(cat "$INVOCATIONS_DIR/git.log")"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 4: Missing AGENT_ID fails fast
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures

set +e
out=$(DATABASE_URL="postgres://test" \
      AGENT_WORKSPACE="$TEST_TMP/t4-workspace" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

if [[ $rc -ne 0 ]]; then
    pass "missing AGENT_ID causes non-zero exit"
else
    fail "missing AGENT_ID causes non-zero exit" "rc=$rc"
fi

if printf '%s' "$out" | grep -q "AGENT_ID_unset"; then
    pass "reports AGENT_ID_unset in fatal log"
else
    fail "reports AGENT_ID_unset in fatal log" "output: $out"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 5: Missing DATABASE_URL fails fast
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures

set +e
out=$(AGENT_ID="33333333-4444-5555-6666-777777777777" \
      AGENT_WORKSPACE="$TEST_TMP/t5-workspace" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

if [[ $rc -ne 0 ]]; then
    pass "missing DATABASE_URL causes non-zero exit"
else
    fail "missing DATABASE_URL causes non-zero exit" "rc=$rc"
fi

if printf '%s' "$out" | grep -q "DATABASE_URL_unset"; then
    pass "reports DATABASE_URL_unset in fatal log"
else
    fail "reports DATABASE_URL_unset in fatal log" "output: $out"
fi

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
