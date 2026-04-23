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

# Determine skill-suffix from /task-v2-<skill> in argv. Note that
# post-#3117 the caller passes the SKILL NAME (plan, ralph, summary,
# fix-ci, verify, retro) not the phase name — this stub looks up
# verdicts by skill name for that reason. Tests that want to assert
# the entrypoint sent the right /task-v2-<skill> literal grep the
# claude.log directly.
skill=""
for arg in "$@"; do
    case "$arg" in
        /task-v2-*)
            stripped="${arg#/task-v2-}"
            skill="${stripped%% *}"
            break
            ;;
    esac
done

# Allow tests to inject a non-object `.result` (e.g. a plain string
# "Unknown command: /task-v2-foo") to exercise the defensive shim.
# Set CLAUDE_RESULT_OVERRIDE to the exact JSON payload to emit. Set
# CLAUDE_RESULT_OVERRIDE_SKILL to scope the override to one skill
# only; leave unset to apply it to every skill invocation.
if [[ -n "${CLAUDE_RESULT_OVERRIDE:-}" ]]; then
    if [[ -z "${CLAUDE_RESULT_OVERRIDE_SKILL:-}" || "${CLAUDE_RESULT_OVERRIDE_SKILL}" == "$skill" ]]; then
        printf '%s\n' "$CLAUDE_RESULT_OVERRIDE"
        exit 0
    fi
fi

# Look up verdict for this skill from CLAUDE_VERDICT_FIXTURE (TSV).
verdict="SHIP"
if [[ -f "${CLAUDE_VERDICT_FIXTURE:-}" && -n "$skill" ]]; then
    match=$(grep -E "^${skill}	" "$CLAUDE_VERDICT_FIXTURE" 2>/dev/null || true)
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
    rev-list)
        # `git rev-list --count origin/main..HEAD` — tests that want
        # to exercise push_and_pr's push-and-PR path set
        # GIT_REV_LIST_COUNT=1 (or higher); tests that want the no-op
        # branch leave it unset (defaults to 0).
        printf '%s\n' "${GIT_REV_LIST_COUNT:-0}"
        exit 0
        ;;
    format-patch)
        printf 'From deadbeefcafe Mon Sep 17 00:00:00 2001\nfake patch content\n'
        exit 0
        ;;
    am)
        exit 0
        ;;
    push)
        # Honor GIT_PUSH_EXIT to simulate push failures.
        exit "${GIT_PUSH_EXIT:-0}"
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

# Simulate `gh pr create` outcomes based on GH_PR_CREATE_EXIT.
sub=""
for arg in "$@"; do
    if [[ "$sub" == "" && "$arg" != --* && "$arg" != -* ]]; then
        sub="$arg"
        continue
    fi
    if [[ -n "$sub" && "$sub" == "pr" && "$arg" != --* && "$arg" != -* ]]; then
        if [[ "$arg" == "create" ]]; then
            printf 'https://github.com/judgemind/judgemind/pull/9999\n'
            exit "${GH_PR_CREATE_EXIT:-0}"
        fi
    fi
done

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
# Verdicts keyed by SKILL NAME (post-#3117), not phase name. The
# entrypoint maps `planning → plan`, `fix_ci → fix-ci`, etc.
cat > "$CLAUDE_VERDICT_FIXTURE" <<'EOF'
plan	OK
ralph	SHIP
summary	OK
fix-ci	PATCHED
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
      GIT_REV_LIST_COUNT=1 \
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

# Verify the phase_outputs INSERT column list matches the real
# `dispatcher.phase_outputs` schema — specifically, that it does NOT
# include a `status` column. Regression guard for #3115: the initial
# Stage 1b entrypoint diverged from the daemon's insert shape by
# adding a `status` column that doesn't exist in the schema, which
# crashed every per-agent ECS task at the first phase-output persist.
# Authoritative schema columns (as of migration 41): output_id,
# agent_id, phase, output_json, ts, log_text, attempt, tokens_*,
# cost_usd, model_used, patch_id. No `status`.
if grep -F "INSERT INTO dispatcher.phase_outputs" "$INVOCATIONS_DIR/psql.log" \
     | grep -q "status" 2>/dev/null; then
    fail "phase_outputs INSERT omits nonexistent status column (#3115)" \
         "Found 'status' in INSERT column list. Sample: $(grep -m1 "INSERT INTO dispatcher.phase_outputs" "$INVOCATIONS_DIR/psql.log" | head -c 300)"
else
    pass "phase_outputs INSERT omits nonexistent status column (#3115)"
fi

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

# Verify claude was invoked for each claude-driven phase. Post-#3117
# the happy path calls claude for planning, ralph, summary, verify
# (4 calls) — push_and_pr is mechanical, and fix_ci only runs when
# CI goes red (which the stub doesn't simulate).
claude_calls=$(wc -l < "$INVOCATIONS_DIR/claude.log" | tr -d ' ')
if [[ "$claude_calls" -ge 4 ]]; then
    pass "claude invoked for each claude-driven phase (calls=$claude_calls)"
else
    fail "claude invoked for each claude-driven phase" "expected ≥4 claude calls, got $claude_calls"
fi

# Verify the entrypoint invoked claude with the real SKILL names, not
# the raw phase names (#3117 bug #1). This is the regression guard
# against the `planning → plan` and `fix_ci → fix-ci` drift that
# caused every Stage 3 smoke agent to die at the first phase with
# "Unknown command: /task-v2-planning".
# The `-p` flag quotes its argument as a single token: `/task-v2-plan
# <agent_id>`. printf '%q' escapes the embedded space as `\ ` in the
# invocations log, so the literal `/task-v2-plan\ ` appears in the
# log when (and only when) the skill suffix is exactly `plan`. A
# phase-name-drift bug would produce `/task-v2-planning\ ` which we
# rule out with the negative grep below.
if grep -F "/task-v2-plan\\ " "$INVOCATIONS_DIR/claude.log" >/dev/null 2>&1; then
    pass "entrypoint invokes /task-v2-plan skill (not /task-v2-planning)"
else
    fail "entrypoint invokes /task-v2-plan skill (not /task-v2-planning)" \
         "claude log: $(head -c 300 "$INVOCATIONS_DIR/claude.log")"
fi

# Negative: /task-v2-planning must NOT appear anywhere in the log.
if grep -F "/task-v2-planning" "$INVOCATIONS_DIR/claude.log" >/dev/null 2>&1; then
    fail "entrypoint does not invoke /task-v2-planning (phase-name drift)" \
         "Found /task-v2-planning in claude log."
else
    pass "entrypoint does not invoke /task-v2-planning (phase-name drift)"
fi

# Negative: /task-v2-claiming and /task-v2-push_and_pr skills do not
# exist and must never be invoked.
if grep -F "/task-v2-claiming" "$INVOCATIONS_DIR/claude.log" >/dev/null 2>&1; then
    fail "entrypoint does not invoke /task-v2-claiming (mechanical pseudo-phase)" \
         "Found /task-v2-claiming in claude log."
else
    pass "entrypoint does not invoke /task-v2-claiming (mechanical pseudo-phase)"
fi

if grep -F "/task-v2-push_and_pr" "$INVOCATIONS_DIR/claude.log" >/dev/null 2>&1; then
    fail "entrypoint does not invoke /task-v2-push_and_pr (mechanical phase)" \
         "Found /task-v2-push_and_pr in claude log."
else
    pass "entrypoint does not invoke /task-v2-push_and_pr (mechanical phase)"
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

# ══════════════════════════════════════════════════════════════════════════
# Test 6: #3117 bug #2 — `claiming` advances to `planning` without
# invoking a nonexistent `/task-v2-claiming` skill.
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t6.txt"
printf 'claiming\n' > "$PHASE_FIXTURE_FILE"

CLAUDE_VERDICT_FIXTURE="$TEST_TMP/verdicts-t6.tsv"
cat > "$CLAUDE_VERDICT_FIXTURE" <<'EOF'
plan	OK
ralph	SHIP
summary	OK
verify	VERIFIED
EOF
PRIOR_PATCH_FIXTURE=""

t6_workspace="$TEST_TMP/t6-workspace"
mkdir -p "$t6_workspace"

set +e
out=$(AGENT_ID="66666666-7777-8888-9999-aaaaaaaaaaaa" \
      ISSUE_NUMBER="3117" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$t6_workspace" \
      REPO_URL="https://example.invalid/repo.git" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
      PRIOR_PATCH_FIXTURE="$PRIOR_PATCH_FIXTURE" \
      CLAUDE_VERDICT_FIXTURE="$CLAUDE_VERDICT_FIXTURE" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      AGENT_RUNNER_MAX_PHASE_ITERATIONS=40 \
      GIT_REV_LIST_COUNT=1 \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

if [[ $rc -eq 0 ]]; then
    pass "claiming no-op: pipeline advances to done cleanly (rc=0)"
else
    fail "claiming no-op: pipeline advances to done cleanly" \
         "rc=$rc, final phase: $(cat "$PHASE_FIXTURE_FILE" 2>/dev/null), output tail: $(printf '%s\n' "$out" | tail -20)"
fi

# The `claiming` branch must persist a no-op row rather than crashing
# on a nonexistent skill.
if grep -q "claiming_no_op_advance_to_planning" <<<"$out"; then
    pass "claiming branch emits claiming_no_op_advance_to_planning event"
else
    fail "claiming branch emits claiming_no_op_advance_to_planning event" "output: $out"
fi

# `planning` UPDATE must follow `claiming`: the FIRST phase-advance
# UPDATE in psql.log should point at 'planning'. `printf '%q'` (used
# by the stub's invocation recorder) emits the multi-line SQL as a
# `$'...'` token with `\'planning\'` for the quoted phase literal.
# Extract the phase name from the first such UPDATE.
first_phase_name=$(grep -oE "SET phase = \\\\'[a-z_]+\\\\'" "$INVOCATIONS_DIR/psql.log" 2>/dev/null \
                   | head -n 1 \
                   | sed -E "s/.*\\\\'([a-z_]+)\\\\'.*/\\1/" \
                   || true)
if [[ "$first_phase_name" == "planning" ]]; then
    pass "claiming advances first to planning"
else
    fail "claiming advances first to planning" \
         "first phase name extracted: [$first_phase_name] log head: $(head -c 400 "$INVOCATIONS_DIR/psql.log")"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 7: #3117 bug #3 — non-object `.result` from claude is coerced
# to {} rather than crashing the shim.
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t7.txt"
printf 'planning\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t7_workspace="$TEST_TMP/t7-workspace"
mkdir -p "$t7_workspace"

# Force the claude stub to emit a plain-string .result for every call —
# e.g. the actual "Unknown command" shape that hit the Stage 3 smoke.
set +e
out=$(AGENT_ID="77777777-8888-9999-aaaa-bbbbbbbbbbbb" \
      ISSUE_NUMBER="3117" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$t7_workspace" \
      REPO_URL="https://example.invalid/repo.git" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
      PRIOR_PATCH_FIXTURE="$PRIOR_PATCH_FIXTURE" \
      CLAUDE_VERDICT_FIXTURE="" \
      CLAUDE_RESULT_OVERRIDE='{"result": "Unknown command: /task-v2-plan"}' \
      CLAUDE_RESULT_OVERRIDE_SKILL="plan" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      AGENT_RUNNER_MAX_PHASE_ITERATIONS=10 \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

# The script must NOT crash with a python AttributeError on the shim.
# The transition for plan with an empty output dict is "advance to
# ralph" (plan has no SHIP/AC_INFEASIBLE gate — any non-BLOCKED
# output treats plan as done). The next phase must be `ralph` or
# further along; the important property is "no python traceback".
if printf '%s' "$out" | grep -q "AttributeError\|Traceback"; then
    fail "non-dict .result does not crash the shim with Python traceback" \
         "output: $(printf '%s' "$out" | grep -E 'Traceback|AttributeError' | head -5)"
else
    pass "non-dict .result does not crash the shim with Python traceback"
fi

# Entrypoint must have logged claude_result_non_object for the bad payload.
if printf '%s' "$out" | grep -q "claude_result_non_object"; then
    pass "entrypoint logs claude_result_non_object on non-object .result"
else
    fail "entrypoint logs claude_result_non_object on non-object .result" \
         "output: $out"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 8: #3117 bug #4 — push_and_pr is mechanical; it invokes
# git push + gh pr create and does NOT invoke claude for a
# `/task-v2-push_and_pr` skill.
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t8.txt"
printf 'push_and_pr\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t8_workspace="$TEST_TMP/t8-workspace"
mkdir -p "$t8_workspace"

set +e
out=$(AGENT_ID="88888888-9999-aaaa-bbbb-cccccccccccc" \
      ISSUE_NUMBER="3117" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$t8_workspace" \
      REPO_URL="https://example.invalid/repo.git" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
      PRIOR_PATCH_FIXTURE="$PRIOR_PATCH_FIXTURE" \
      CLAUDE_VERDICT_FIXTURE="" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      AGENT_RUNNER_MAX_PHASE_ITERATIONS=15 \
      GIT_REV_LIST_COUNT=1 \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

# push_and_pr must NOT invoke claude — that was the original bug.
if grep -F "/task-v2-push_and_pr" "$INVOCATIONS_DIR/claude.log" >/dev/null 2>&1; then
    fail "push_and_pr does not invoke claude (mechanical phase)" \
         "Found /task-v2-push_and_pr in claude log."
else
    pass "push_and_pr does not invoke claude (mechanical phase)"
fi

# git push must have been invoked with `-u origin <branch>`.
if grep -F "push" "$INVOCATIONS_DIR/git.log" | grep -F "origin" >/dev/null 2>&1; then
    pass "push_and_pr invokes git push -u origin <branch>"
else
    fail "push_and_pr invokes git push -u origin <branch>" \
         "git log: $(cat "$INVOCATIONS_DIR/git.log")"
fi

# gh pr create must have been invoked.
if grep -F "pr" "$INVOCATIONS_DIR/gh.log" | grep -F "create" >/dev/null 2>&1; then
    pass "push_and_pr invokes gh pr create"
else
    fail "push_and_pr invokes gh pr create" \
         "gh log: $(cat "$INVOCATIONS_DIR/gh.log")"
fi

# push_and_pr advances to awaiting_ci on success. The stub's %q-escaped
# log has `SET phase = \'awaiting_ci\'` for the phase literal.
if grep -q "SET phase = \\\\'awaiting_ci\\\\'" "$INVOCATIONS_DIR/psql.log"; then
    pass "push_and_pr advances to awaiting_ci"
else
    fail "push_and_pr advances to awaiting_ci" \
         "psql log tail: $(tail -c 500 "$INVOCATIONS_DIR/psql.log")"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 9: #3117 bug #4 — push_and_pr no-op branch (#3039). A ralph
# SHIP with clean worktree (rev-list count 0) terminates as no_op
# without pushing or opening a PR.
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t9.txt"
printf 'push_and_pr\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t9_workspace="$TEST_TMP/t9-workspace"
mkdir -p "$t9_workspace"

set +e
out=$(AGENT_ID="99999999-aaaa-bbbb-cccc-dddddddddddd" \
      ISSUE_NUMBER="3117" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$t9_workspace" \
      REPO_URL="https://example.invalid/repo.git" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
      PRIOR_PATCH_FIXTURE="$PRIOR_PATCH_FIXTURE" \
      CLAUDE_VERDICT_FIXTURE="" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      AGENT_RUNNER_MAX_PHASE_ITERATIONS=10 \
      GIT_REV_LIST_COUNT=0 \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

if [[ $rc -eq 0 ]]; then
    pass "push_and_pr no-op (#3039) terminates cleanly (rc=0)"
else
    fail "push_and_pr no-op (#3039) terminates cleanly" \
         "rc=$rc, final phase: $(cat "$PHASE_FIXTURE_FILE" 2>/dev/null)"
fi

if printf '%s' "$out" | grep -q "push_and_pr_no_op"; then
    pass "push_and_pr no-op logs push_and_pr_no_op event"
else
    fail "push_and_pr no-op logs push_and_pr_no_op event" "output: $out"
fi

# No-op must NOT have pushed or opened a PR.
if grep -F "push" "$INVOCATIONS_DIR/git.log" | grep -F "origin" >/dev/null 2>&1; then
    fail "push_and_pr no-op does not invoke git push" \
         "git log: $(cat "$INVOCATIONS_DIR/git.log")"
else
    pass "push_and_pr no-op does not invoke git push"
fi

if [[ -s "$INVOCATIONS_DIR/gh.log" ]] && grep -F "pr" "$INVOCATIONS_DIR/gh.log" | grep -F "create" >/dev/null 2>&1; then
    fail "push_and_pr no-op does not invoke gh pr create" \
         "gh log: $(cat "$INVOCATIONS_DIR/gh.log")"
else
    pass "push_and_pr no-op does not invoke gh pr create"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 10: #3117 bug #1 — the fix_ci phase maps to the `fix-ci` skill
# (hyphen), not `fix_ci` (underscore).
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t10.txt"
printf 'fix_ci\n' > "$PHASE_FIXTURE_FILE"

CLAUDE_VERDICT_FIXTURE="$TEST_TMP/verdicts-t10.tsv"
cat > "$CLAUDE_VERDICT_FIXTURE" <<'EOF'
fix-ci	PATCHED
EOF
PRIOR_PATCH_FIXTURE=""

t10_workspace="$TEST_TMP/t10-workspace"
mkdir -p "$t10_workspace"

set +e
out=$(AGENT_ID="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" \
      ISSUE_NUMBER="3117" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$t10_workspace" \
      REPO_URL="https://example.invalid/repo.git" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
      PRIOR_PATCH_FIXTURE="$PRIOR_PATCH_FIXTURE" \
      CLAUDE_VERDICT_FIXTURE="$CLAUDE_VERDICT_FIXTURE" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      AGENT_RUNNER_MAX_PHASE_ITERATIONS=15 \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

# The entrypoint must pass /task-v2-fix-ci to claude (hyphen), never
# /task-v2-fix_ci (underscore). This is the regression guard against
# #3117 bug #1's second drift. `printf '%q'` escapes the space between
# the skill suffix and the agent id as `\ `, so the literal
# `/task-v2-fix-ci\ ` is the expected token in the log.
if grep -F "/task-v2-fix-ci\\ " "$INVOCATIONS_DIR/claude.log" >/dev/null 2>&1; then
    pass "fix_ci phase invokes /task-v2-fix-ci skill (hyphen)"
else
    fail "fix_ci phase invokes /task-v2-fix-ci skill (hyphen)" \
         "claude log: $(cat "$INVOCATIONS_DIR/claude.log")"
fi

if grep -F "/task-v2-fix_ci" "$INVOCATIONS_DIR/claude.log" >/dev/null 2>&1; then
    fail "fix_ci phase does not invoke /task-v2-fix_ci (underscore drift)" \
         "Found /task-v2-fix_ci in claude log."
else
    pass "fix_ci phase does not invoke /task-v2-fix_ci (underscore drift)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 11: #3117 bug #5 — malformed JSON from claude does not crash
# the runner; it surfaces via claude_result_non_object and the
# entrypoint continues to advance.
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t11.txt"
printf 'planning\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t11_workspace="$TEST_TMP/t11-workspace"
mkdir -p "$t11_workspace"

set +e
out=$(AGENT_ID="bbbbbbbb-cccc-dddd-eeee-ffffffffffff" \
      ISSUE_NUMBER="3117" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$t11_workspace" \
      REPO_URL="https://example.invalid/repo.git" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
      PRIOR_PATCH_FIXTURE="$PRIOR_PATCH_FIXTURE" \
      CLAUDE_VERDICT_FIXTURE="" \
      CLAUDE_RESULT_OVERRIDE='this is not json at all' \
      CLAUDE_RESULT_OVERRIDE_SKILL="plan" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      AGENT_RUNNER_MAX_PHASE_ITERATIONS=10 \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

# Runner must not crash with a Python traceback on malformed JSON.
if printf '%s' "$out" | grep -q "AttributeError\|Traceback"; then
    fail "malformed JSON output does not crash shim with Python traceback" \
         "output: $(printf '%s' "$out" | head -c 500)"
else
    pass "malformed JSON output does not crash shim with Python traceback"
fi

if printf '%s' "$out" | grep -q "claude_result_non_object"; then
    pass "malformed JSON output surfaces via claude_result_non_object"
else
    fail "malformed JSON output surfaces via claude_result_non_object" \
         "output: $out"
fi

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
