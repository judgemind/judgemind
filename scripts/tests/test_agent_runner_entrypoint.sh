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
    *"SELECT pr_number"*"FROM dispatcher.agents"*)
        # #3176 — post-PR handlers read pr_number from the agent row.
        # Default 9999 matches the gh stub's canonical PR URL.
        printf '%s\n' "${PR_NUMBER_FIXTURE:-9999}"
        exit 0
        ;;
    *"SELECT COALESCE(merge_unstick_attempts"*|*"SELECT merge_unstick_attempts"*)
        # #3176 — handle_merge's auto-unstick budget check.
        printf '%s\n' "${MERGE_UNSTICK_ATTEMPTS_FIXTURE:-0}"
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
        # #3144 T26 knob — simulate DB down during the HEAD-watcher's
        # per-iteration INSERT. The watcher's ``if ! psql … ; then``
        # guard must catch the non-zero exit and emit
        # ``ralph_head_watcher_db_failure`` without crashing.
        if [[ "${PSQL_FAIL_ON_INSERT:-0}" == "1" ]]; then
            exit 1
        fi
        exit 0
        ;;
    *"SELECT 1 FROM dispatcher.ralph_patches"*)
        # #3144 HEAD-watcher SELECT-then-INSERT guard. Returns empty
        # (not-exists) by default so tests see fresh INSERT paths.
        # Set RALPH_PATCH_EXISTS=1 to simulate a prior iteration
        # already persisted.
        if [[ "${RALPH_PATCH_EXISTS:-0}" == "1" ]]; then
            printf '1\n'
        fi
        exit 0
        ;;
    *"UPDATE dispatcher.agents"*"SET ralph_iterations_observed"*)
        # #3144 HEAD-watcher counter bump. Acknowledge the query so the
        # invocation log picks it up; the test grep'd specifically for
        # this substring.
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

# #3190: record the effective cwd inherited by each `claude -p`
# invocation so tests can assert the entrypoint anchors cwd to
# $REPO_ROOT before the call. One line per invocation; skill name
# precedes the pwd so tests can filter by phase.
printf '%s\t%s\n' "${skill:-unknown}" "$(pwd)" >> "$INVOCATIONS_DIR/claude-cwd.log"

# Allow tests to inject a non-object `.result` (e.g. a plain string
# "Unknown command: /task-v2-foo") to exercise the defensive shim.
# Set CLAUDE_RESULT_OVERRIDE to the exact JSON payload to emit. Set
# CLAUDE_RESULT_OVERRIDE_SKILL to scope the override to one skill
# only; leave unset to apply it to every skill invocation.
#
# CLAUDE_STDERR_OVERRIDE lets tests write a stderr payload the
# entrypoint captures via its `2> $AGENT_WORKSPACE/claude-p-<phase>.stderr.log`
# redirect — exercise the #3131 diag path's stderr_head_1024 field.
if [[ -n "${CLAUDE_STDERR_OVERRIDE:-}" ]]; then
    if [[ -z "${CLAUDE_RESULT_OVERRIDE_SKILL:-}" || "${CLAUDE_RESULT_OVERRIDE_SKILL}" == "$skill" ]]; then
        printf '%s\n' "$CLAUDE_STDERR_OVERRIDE" >&2
    fi
fi
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
    log)
        # `git log --reverse --format='%H' origin/main..HEAD` — baseline.
        # `git log --reverse --format='%H %s' origin/main..HEAD` — tick.
        # The HEAD-watcher test (#3144) sets RALPH_HEAD_WATCHER_COMMITS_FILE
        # to a path whose contents are one commit per line in the
        # ``<sha> <subject>`` shape. The stub emits either just SHAs
        # (for --format='%H') or the full line (for --format='%H %s')
        # depending on the format flag — so the entrypoint's seen-file
        # bookkeeping (which grep's the SHA) works against both calls.
        _want_subject=0
        for _arg in "$@"; do
            case "$_arg" in
                --format=*%s*)
                    _want_subject=1
                    ;;
            esac
        done
        if [[ -f "${RALPH_HEAD_WATCHER_COMMITS_FILE:-}" ]]; then
            if [[ "$_want_subject" == "1" ]]; then
                cat "$RALPH_HEAD_WATCHER_COMMITS_FILE"
            else
                # Strip subjects — emit SHA per line only.
                awk '{print $1}' "$RALPH_HEAD_WATCHER_COMMITS_FILE"
            fi
            exit 0
        fi
        # Default: no commits. The entrypoint's baseline + tick paths
        # both tolerate empty output (no new commits to emit on).
        exit 0
        ;;
    am)
        exit 0
        ;;
    push)
        # Honor GIT_PUSH_EXIT to simulate push failures.
        exit "${GIT_PUSH_EXIT:-0}"
        ;;
    fetch)
        # `git fetch origin main` — #3176 push_and_pr pre-push rebase.
        exit "${GIT_FETCH_EXIT:-0}"
        ;;
    rebase)
        # `git rebase origin/main` — #3176 push_and_pr pre-push rebase.
        # `git rebase --abort` — #3176 conflict-abort on failed rebase.
        for _arg in "$@"; do
            if [[ "$_arg" == "--abort" ]]; then
                exit 0
            fi
        done
        exit "${GIT_REBASE_EXIT:-0}"
        ;;
    commit)
        # `git commit --amend -F <file>` — #3176 summary amend.
        # `git commit --allow-empty -m <msg>` — #3176 stale-rollup unstick.
        exit "${GIT_COMMIT_EXIT:-0}"
        ;;
    *)
        exit 0
        ;;
esac
GITEOF
chmod +x "$STUB_BIN/git"

# ── gh stub ────────────────────────────────────────────────────────────────
#
# Supports the subcommands the entrypoint calls:
#   * ``gh pr create`` — prints a PR URL, honours GH_PR_CREATE_EXIT.
#   * ``gh pr view <N> --json ...`` — prints JSON from
#     ``$GH_PR_VIEW_JSON_FIXTURE`` (defaults to a green rollup with a
#     merged commit SHA of ``deadbeefcafe`` so the happy-path pipeline
#     can merge + deploy without a fixture override).
#   * ``gh pr merge`` — honours ``GH_PR_MERGE_EXIT`` (default 0). When
#     ``GH_PR_MERGE_STDERR`` is non-empty, emits that text to stderr
#     (used to simulate the #2641/#3163 stale-rollup rejection).
#   * ``gh run list`` — prints the JSON array from
#     ``$GH_RUN_LIST_JSON_FIXTURE`` (defaults to ``[]`` = no deploy
#     workflows fired, so the entrypoint's awaiting_deploy "none"
#     branch advances to verify).

cat > "$STUB_BIN/gh" <<'GHEOF'
#!/usr/bin/env bash
set -u
INVOCATIONS_DIR="${INVOCATIONS_DIR}"
. "$(dirname "$0")/_record_invocation.sh" gh "$@"

# Find the first two positional args (subcommand + verb, e.g. pr create).
sub=""
verb=""
for arg in "$@"; do
    case "$arg" in
        --*|-*) continue ;;
    esac
    if [[ -z "$sub" ]]; then
        sub="$arg"
        continue
    fi
    if [[ -z "$verb" ]]; then
        verb="$arg"
        break
    fi
done

case "$sub $verb" in
    "pr create")
        printf 'https://github.com/judgemind/judgemind/pull/9999\n'
        exit "${GH_PR_CREATE_EXIT:-0}"
        ;;
    "pr view")
        if [[ -n "${GH_PR_VIEW_JSON_FIXTURE:-}" && -f "${GH_PR_VIEW_JSON_FIXTURE:-}" ]]; then
            cat "$GH_PR_VIEW_JSON_FIXTURE"
        else
            # Default: green rollup, mergeable, with a merge SHA so
            # happy-path tests can traverse awaiting_ci → merge →
            # awaiting_deploy without fixture overrides.
            cat <<'JSONEOF'
{
  "statusCheckRollup": [
    {"name": "ci-passed", "status": "COMPLETED", "conclusion": "SUCCESS"}
  ],
  "mergeable": "MERGEABLE",
  "mergeStateStatus": "CLEAN",
  "headRefOid": "deadbeefcafe",
  "mergeCommit": {"oid": "deadbeefcafe"}
}
JSONEOF
        fi
        exit 0
        ;;
    "pr merge")
        if [[ -n "${GH_PR_MERGE_STDERR:-}" ]]; then
            printf '%s\n' "$GH_PR_MERGE_STDERR" >&2
        fi
        exit "${GH_PR_MERGE_EXIT:-0}"
        ;;
    "run list")
        if [[ -n "${GH_RUN_LIST_JSON_FIXTURE:-}" && -f "${GH_RUN_LIST_JSON_FIXTURE:-}" ]]; then
            cat "$GH_RUN_LIST_JSON_FIXTURE"
        else
            printf '[]\n'
        fi
        exit "${GH_RUN_LIST_EXIT:-0}"
        ;;
    "auth login"|"auth setup-git")
        exit 0
        ;;
esac

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
    : > "$INVOCATIONS_DIR/claude-cwd.log"
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

# #3190: every claude -p invocation must run with cwd == $REPO_ROOT
# so the task-v2-* skills can resolve their
# ``tmp/dispatcher-input/<phase>.json`` input bundle via the relative
# path they use internally. The claude stub records its pwd per-call
# in $INVOCATIONS_DIR/claude-cwd.log (skill<TAB>pwd, one line per
# invocation). REPO_ROOT = $AGENT_WORKSPACE/repo; the t2 happy path
# invokes claude for plan, ralph, summary, verify — every row must
# have pwd = $t2_workspace/repo.
t2_expected_repo_root="$t2_workspace/repo"
t2_cwd_total=$(wc -l < "$INVOCATIONS_DIR/claude-cwd.log" | tr -d ' ')
# Count lines whose second field (tab-separated) is NOT the expected
# REPO_ROOT. Use `set +e; grep ... ; set -e` so grep-exit-1-no-match
# doesn't propagate through $(...); we want the printed count either
# way.
set +e
t2_cwd_mismatches=$(grep -cvF "	${t2_expected_repo_root}" "$INVOCATIONS_DIR/claude-cwd.log" 2>/dev/null)
set -e
t2_cwd_mismatches="${t2_cwd_mismatches:-0}"
if [[ "$t2_cwd_total" -ge 1 && "$t2_cwd_mismatches" -eq 0 ]]; then
    pass "#3190 — every claude -p invocation runs with cwd == \$REPO_ROOT"
else
    fail "#3190 — every claude -p invocation runs with cwd == \$REPO_ROOT" \
         "expected all lines to end with '<TAB>$t2_expected_repo_root', total_lines=$t2_cwd_total, mismatches=$t2_cwd_mismatches, log: $(cat "$INVOCATIONS_DIR/claude-cwd.log")"
fi

# #3190 AC #2: claude_phase_begin log event includes cwd= field so the
# next incident is self-diagnosing without needing an in-model `pwd &&
# ls` (which the preflight-bash hook blocks anyway). The log emitter
# produces a JSON object with a ``"cwd": "..."`` field, so match that
# shape explicitly instead of the raw key=value kwarg style.
if printf '%s' "$out" | grep -qE "claude_phase_begin.*\"cwd\": \"${t2_expected_repo_root}\""; then
    pass "#3190 AC #2 — claude_phase_begin log event includes cwd=\$REPO_ROOT"
else
    fail "#3190 AC #2 — claude_phase_begin log event includes cwd=\$REPO_ROOT" \
         "expected a claude_phase_begin line carrying cwd=$t2_expected_repo_root, out tail: $(printf '%s' "$out" | grep claude_phase_begin | head -5)"
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

# ══════════════════════════════════════════════════════════════════════════
# Test 12: #3131 — non-object `.result` also emits a
# `claude_result_non_object_diag` event with structured triage fields
# (result_type, result_bytes, result_head_512). Without this, every
# ECS agent dies opaquely — operators have no way to tell whether
# claude said "Unknown command" vs a conversational response vs a
# permission denial without a second deploy-instrument cycle.
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t12.txt"
printf 'planning\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t12_workspace="$TEST_TMP/t12-workspace"
mkdir -p "$t12_workspace"

# Exact "Unknown command" payload that hit every Stage 3 smoke agent.
set +e
out=$(AGENT_ID="cccccccc-dddd-eeee-ffff-000000000000" \
      ISSUE_NUMBER="3131" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$t12_workspace" \
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

# The diag event must have fired.
if printf '%s' "$out" | grep -q "claude_result_non_object_diag"; then
    pass "non-object .result emits claude_result_non_object_diag"
else
    fail "non-object .result emits claude_result_non_object_diag" \
         "output tail: $(printf '%s' "$out" | tail -20)"
fi

# The diag line must carry the structured type field — `.result` was a
# JSON string, so `jq .result | type` returns "string". This is the
# single most important field for #3131 triage: it tells us at a
# glance that claude returned a conversational/error string, not the
# structured envelope the skill is supposed to emit.
diag_line=$(printf '%s' "$out" | grep "claude_result_non_object_diag" | head -n 1)
if printf '%s' "$diag_line" | grep -q 'result_type.*string'; then
    pass "diag event carries result_type=string for string .result"
else
    fail "diag event carries result_type=string for string .result" \
         "diag line: $diag_line"
fi

# The diag line must carry the actual payload text in result_head_512.
# The string "Unknown command: /task-v2-plan" is 31 bytes, well under
# the 512 cap.
if printf '%s' "$diag_line" | grep -q 'Unknown command: /task-v2-plan'; then
    pass "diag event result_head_512 contains the actual .result text"
else
    fail "diag event result_head_512 contains the actual .result text" \
         "diag line: $diag_line"
fi

# result_bytes is the byte count of `.result | tostring`. "Unknown
# command: /task-v2-plan" is 31 bytes. Assert the field is present
# and numeric; exact value doesn't matter (jq versions may differ in
# tostring whitespace handling, though for a raw string they
# shouldn't).
if printf '%s' "$diag_line" | grep -qE 'result_bytes": "[0-9]+"'; then
    pass "diag event carries numeric result_bytes"
else
    fail "diag event carries numeric result_bytes" \
         "diag line: $diag_line"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 13: #3131 — when claude writes to stderr, the diag event
# carries the first 1024 bytes of that stderr file. Exercises the
# `stderr_head_1024` field independently of the `.result` path.
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t13.txt"
printf 'planning\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t13_workspace="$TEST_TMP/t13-workspace"
mkdir -p "$t13_workspace"

# Stderr payload representative of a real claude-cli failure: a line
# about a skill-resolution problem. Keep under 1024 bytes so the test
# asserts against the full payload, not a truncation boundary.
stderr_payload='Error: skill /task-v2-plan not found in any registry. Searched: /home/dispatcher/.claude/skills, /app/.claude/skills, /var/lib/agent-runner/repo/.claude/skills'

set +e
out=$(AGENT_ID="dddddddd-eeee-ffff-0000-111111111111" \
      ISSUE_NUMBER="3131" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$t13_workspace" \
      REPO_URL="https://example.invalid/repo.git" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
      PRIOR_PATCH_FIXTURE="$PRIOR_PATCH_FIXTURE" \
      CLAUDE_VERDICT_FIXTURE="" \
      CLAUDE_RESULT_OVERRIDE='{"result": "Unknown command: /task-v2-plan"}' \
      CLAUDE_RESULT_OVERRIDE_SKILL="plan" \
      CLAUDE_STDERR_OVERRIDE="$stderr_payload" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      AGENT_RUNNER_MAX_PHASE_ITERATIONS=10 \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

diag_line=$(printf '%s' "$out" | grep "claude_result_non_object_diag" | head -n 1)

# stderr_head_1024 field should contain a distinctive substring of the
# stub's stderr payload. Don't assert the full string — log() escapes
# embedded `"` / `\` and the test parses its own stdout capture which
# picks up shell-quoting noise.
if printf '%s' "$diag_line" | grep -q 'stderr_head_1024'; then
    pass "diag event carries stderr_head_1024 when claude writes to stderr"
else
    fail "diag event carries stderr_head_1024 when claude writes to stderr" \
         "diag line: $diag_line"
fi

if printf '%s' "$diag_line" | grep -q 'skill /task-v2-plan not found'; then
    pass "stderr_head_1024 contains the actual stderr text"
else
    fail "stderr_head_1024 contains the actual stderr text" \
         "diag line: $diag_line"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 14: #3131 — when claude writes NOTHING to stderr, the diag
# event OMITS the stderr_head_1024 field entirely rather than emitting
# an empty value. Keeps CloudWatch log lines short on the common case
# and signals "no stderr" unambiguously.
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t14.txt"
printf 'planning\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t14_workspace="$TEST_TMP/t14-workspace"
mkdir -p "$t14_workspace"

set +e
out=$(AGENT_ID="eeeeeeee-ffff-0000-1111-222222222222" \
      ISSUE_NUMBER="3131" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$t14_workspace" \
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

diag_line=$(printf '%s' "$out" | grep "claude_result_non_object_diag" | head -n 1)
if printf '%s' "$diag_line" | grep -q 'stderr_head_1024'; then
    fail "diag event omits stderr_head_1024 when claude stderr is empty" \
         "diag line unexpectedly contains stderr_head_1024: $diag_line"
else
    pass "diag event omits stderr_head_1024 when claude stderr is empty"
fi

# The existing `claude_result_non_object` event should still fire
# alongside the diag event — downstream consumers (CloudWatch
# Insights queries, log parsers) rely on the original event name and
# must not be broken by the diag addition.
if printf '%s' "$out" | grep -q 'claude_result_non_object"'; then
    pass "original claude_result_non_object event still fires (no regression)"
else
    fail "original claude_result_non_object event still fires (no regression)" \
         "output tail: $(printf '%s' "$out" | tail -30)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 15: #3133 — the entrypoint writes {repo_root}/tmp/dispatcher-input/
# plan.json before invoking claude, with the plan skill's required fields
# populated. This is the direct fix for the diag captured in smoke run
# 9010f81dd24a46e0882fd54ef45af213: the plan skill's `.result` came back
# as a string "Plan phase blocked: the daemon did not write {worktree}/
# tmp/dispatcher-input/plan.json..." because the entrypoint never wrote
# that file.
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t15.txt"
# Start at planning and walk exactly once through the claude invocation,
# then fall into the terminal dead-branch via an unexpected phase.
printf 'planning\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

CLAUDE_VERDICT_FIXTURE="$TEST_TMP/verdicts-t15.tsv"
cat > "$CLAUDE_VERDICT_FIXTURE" <<'EOF'
plan	OK
ralph	SHIP
summary	OK
fix-ci	PATCHED
verify	VERIFIED
EOF

t15_workspace="$TEST_TMP/t15-workspace"
mkdir -p "$t15_workspace"

# The entrypoint will build REPO_ROOT=$AGENT_WORKSPACE/repo — the
# phase input shim writes to $REPO_ROOT/tmp/dispatcher-input/. After
# the entrypoint runs we inspect that path to verify both the file
# presence and its content shape.
set +e
out=$(AGENT_ID="15151515-1515-1515-1515-151515151515" \
      ISSUE_NUMBER="3133" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$t15_workspace" \
      REPO_URL="https://example.invalid/repo.git" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
      PRIOR_PATCH_FIXTURE="$PRIOR_PATCH_FIXTURE" \
      CLAUDE_VERDICT_FIXTURE="$CLAUDE_VERDICT_FIXTURE" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      AGENT_RUNNER_MAX_PHASE_ITERATIONS=2 \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

plan_input_path="$t15_workspace/repo/tmp/dispatcher-input/plan.json"
if [[ -f "$plan_input_path" ]]; then
    pass "#3133 — entrypoint writes dispatcher-input/plan.json before claude"
else
    fail "#3133 — entrypoint writes dispatcher-input/plan.json before claude" \
         "expected file not found: $plan_input_path. Output tail: $(printf '%s' "$out" | tail -15)"
fi

if [[ -f "$plan_input_path" ]] && jq -e '.agent_id == "15151515-1515-1515-1515-151515151515"' "$plan_input_path" >/dev/null 2>&1; then
    pass "#3133 — plan.json carries agent_id echo"
else
    fail "#3133 — plan.json carries agent_id echo" \
         "content: $(cat "$plan_input_path" 2>/dev/null || echo '<missing>')"
fi

if [[ -f "$plan_input_path" ]] && jq -e '.issue_number == 3133' "$plan_input_path" >/dev/null 2>&1; then
    pass "#3133 — plan.json carries issue_number"
else
    fail "#3133 — plan.json carries issue_number" \
         "content: $(cat "$plan_input_path" 2>/dev/null || echo '<missing>')"
fi

if [[ -f "$plan_input_path" ]] && jq -e '.worktree_path | test("repo$")' "$plan_input_path" >/dev/null 2>&1; then
    pass "#3133 — plan.json carries worktree_path pointing at repo clone"
else
    fail "#3133 — plan.json carries worktree_path pointing at repo clone" \
         "content: $(cat "$plan_input_path" 2>/dev/null || echo '<missing>')"
fi

# The entrypoint should log a phase_input_written event so operators
# can confirm via CloudWatch that the file-write side effect actually
# happened (no silent no-op when gh is absent).
if printf '%s' "$out" | grep -q 'phase_input_written.*"phase": "plan"'; then
    pass "#3133 — entrypoint logs phase_input_written event for plan"
else
    fail "#3133 — entrypoint logs phase_input_written event for plan" \
         "output tail: $(printf '%s' "$out" | tail -30)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 16: #3133 — when the skill writes dispatcher-output/<phase>.json,
# the entrypoint uses THAT content as the phase output rather than the
# claude .result envelope. This is the other half of the fix: even if
# the skill's .result is a string summary, the daemon-path equivalent
# (read the structured output file) correctly extracts the verdict.
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t16.txt"
printf 'planning\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

CLAUDE_VERDICT_FIXTURE=""

t16_workspace="$TEST_TMP/t16-workspace"
mkdir -p "$t16_workspace"

# Pre-stage the dispatcher-output file that the skill would normally
# write. The test stubs claude to return a string .result (the failure
# shape from the pre-fix world), so if the entrypoint correctly prefers
# the output file, the phase will carry the file's verdict to the
# transition shim and advance cleanly to ralph — not fall through the
# diag branch.
#
# The file must live at $AGENT_WORKSPACE/repo/tmp/dispatcher-output/
# because that's where REPO_ROOT resolves inside the entrypoint when
# AGENT_WORKSPACE is set.
repo_root_t16="$t16_workspace/repo"
mkdir -p "$repo_root_t16/.git" "$repo_root_t16/tmp/dispatcher-output"
cat > "$repo_root_t16/tmp/dispatcher-output/plan.json" <<'PLANOUT'
{
  "agent_id": "16161616-1616-1616-1616-161616161616",
  "issue_number": 3133,
  "go": true,
  "block_reason": null,
  "plan_text": "fixture plan body",
  "acceptance_criteria": ["AC1"],
  "scope_check": [],
  "relevant_files": [],
  "relevant_docs": [],
  "change_type": "dx_tooling",
  "dependencies_to_install": []
}
PLANOUT

set +e
out=$(AGENT_ID="16161616-1616-1616-1616-161616161616" \
      ISSUE_NUMBER="3133" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$t16_workspace" \
      REPO_URL="https://example.invalid/repo.git" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
      PRIOR_PATCH_FIXTURE="$PRIOR_PATCH_FIXTURE" \
      CLAUDE_VERDICT_FIXTURE="$CLAUDE_VERDICT_FIXTURE" \
      CLAUDE_RESULT_OVERRIDE='{"result": "Plan phase blocked: ..."}' \
      CLAUDE_RESULT_OVERRIDE_SKILL="plan" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      AGENT_RUNNER_MAX_PHASE_ITERATIONS=2 \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

if printf '%s' "$out" | grep -q 'phase_output_file_read.*"phase": "planning"'; then
    pass "#3133 — entrypoint reads dispatcher-output/plan.json when present"
else
    fail "#3133 — entrypoint reads dispatcher-output/plan.json when present" \
         "output tail: $(printf '%s' "$out" | tail -30)"
fi

# With a well-formed plan.json on disk, the .result-branch diag event
# should NOT fire — the file takes precedence over the string .result.
if printf '%s' "$out" | grep -q 'claude_result_non_object_diag'; then
    fail "#3133 — output-file path suppresses .result diag when file present" \
         "diag event unexpectedly fired. output tail: $(printf '%s' "$out" | tail -30)"
else
    pass "#3133 — output-file path suppresses .result diag when file present"
fi

# ══════════════════════════════════════════════════════════════════════════
# Stage 2 (#3135) — summary / fix-ci / verify / retro input parity
#
# The Stage 1b shim wrote identifier-only stubs for these four phases;
# Stage 2 builds the same field set the daemon's ``_handle_phase_*``
# builders assemble so ECS-mode agents reach the same verdicts as
# subprocess-mode agents. These tests exercise ``phase_input_shim.py``
# directly — running the full entrypoint per phase would require
# stubbing an entire phase-sequence walk, which obscures the actual
# input-build assertions.
#
# Setup: extract the shim file onto disk by running the entrypoint
# once (in dry-run mode so it stops before the first claude invoke).
# ══════════════════════════════════════════════════════════════════════════

shim_workspace="$TEST_TMP/shim-workspace"
mkdir -p "$shim_workspace"

# Run the entrypoint with AGENT_RUNNER_DRY_RUN=1 so it stamps the shim
# file under $AGENT_WORKSPACE and then short-circuits the phase loop.
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-shim-extract.txt"
printf 'done\n' > "$PHASE_FIXTURE_FILE"
set +e
AGENT_ID="00000000-0000-0000-0000-000000000000" \
    ISSUE_NUMBER="3135" \
    DATABASE_URL="postgres://test" \
    GITHUB_TOKEN="" \
    AGENT_WORKSPACE="$shim_workspace" \
    REPO_URL="https://example.invalid/repo.git" \
    PATH="$STUB_BIN:$PATH" \
    INVOCATIONS_DIR="$INVOCATIONS_DIR" \
    PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
    PRIOR_PATCH_FIXTURE="" \
    CLAUDE_VERDICT_FIXTURE="" \
    PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
    PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
    AGENT_RUNNER_DRY_RUN=1 \
    bash "$ENTRYPOINT" >/dev/null 2>&1
set -e

SHIM_PY="$shim_workspace/phase_input_shim.py"
if [[ -f "$SHIM_PY" ]]; then
    pass "#3135 — entrypoint stamps phase_input_shim.py file on disk"
else
    fail "#3135 — entrypoint stamps phase_input_shim.py file on disk" \
         "expected file not found: $SHIM_PY"
fi

# Extend the gh stub with richer routes for the Stage 2 fetches and the
# #3176 post-PR mechanical phases:
#   * ``gh issue view <N> --json ...``    → read $GH_ISSUE_FIXTURE
#   * ``gh pr view <N> --json ...``       → read $GH_PR_FIXTURE (shim
#                                           tests) OR #3176
#                                           $GH_PR_VIEW_JSON_FIXTURE
#                                           (post-PR tests) — falls
#                                           back to a canonical green
#                                           rollup when neither is set.
#   * ``gh pr diff <N>``                  → read $GH_PR_DIFF_FIXTURE
#   * ``gh pr create``                    → honour GH_PR_CREATE_EXIT
#                                           (defaults to 0, printing a
#                                           canonical PR URL).
#   * ``gh pr merge``                     → honour GH_PR_MERGE_EXIT +
#                                           GH_PR_MERGE_STDERR (#3176).
#   * ``gh run view --log-failed --job``  → read $GH_RUN_LOG_FIXTURE
#   * ``gh run list --commit <sha>``      → read $GH_RUN_LIST_FIXTURE
#                                           (shim tests) OR #3176
#                                           $GH_RUN_LIST_JSON_FIXTURE
#                                           (post-PR tests) — falls
#                                           back to ``[]``.
cat > "$STUB_BIN/gh" <<'GHEOF'
#!/usr/bin/env bash
set -u
INVOCATIONS_DIR="${INVOCATIONS_DIR}"
. "$(dirname "$0")/_record_invocation.sh" gh "$@"

# Parse subcommand chain.
if [[ "${1:-}" == "issue" && "${2:-}" == "view" ]]; then
    if [[ -n "${GH_ISSUE_FIXTURE:-}" && -f "$GH_ISSUE_FIXTURE" ]]; then
        cat "$GH_ISSUE_FIXTURE"
        exit 0
    fi
    exit 1
fi

if [[ "${1:-}" == "pr" && "${2:-}" == "view" ]]; then
    # Shim-test fixture (Stage 2) first, then #3176 post-PR fixture,
    # then a canonical green-rollup fallback so happy-path tests can
    # traverse awaiting_ci → merge → awaiting_deploy without setting
    # any fixture at all.
    if [[ -n "${GH_PR_FIXTURE:-}" && -f "$GH_PR_FIXTURE" ]]; then
        cat "$GH_PR_FIXTURE"
        exit 0
    fi
    if [[ -n "${GH_PR_VIEW_JSON_FIXTURE:-}" && -f "$GH_PR_VIEW_JSON_FIXTURE" ]]; then
        cat "$GH_PR_VIEW_JSON_FIXTURE"
        exit 0
    fi
    cat <<'JSONEOF'
{
  "statusCheckRollup": [
    {"name": "ci-passed", "status": "COMPLETED", "conclusion": "SUCCESS"}
  ],
  "mergeable": "MERGEABLE",
  "mergeStateStatus": "CLEAN",
  "headRefOid": "deadbeefcafe",
  "mergeCommit": {"oid": "deadbeefcafe"}
}
JSONEOF
    exit 0
fi

if [[ "${1:-}" == "pr" && "${2:-}" == "diff" ]]; then
    if [[ -n "${GH_PR_DIFF_FIXTURE:-}" && -f "$GH_PR_DIFF_FIXTURE" ]]; then
        cat "$GH_PR_DIFF_FIXTURE"
        exit 0
    fi
    exit 1
fi

if [[ "${1:-}" == "pr" && "${2:-}" == "merge" ]]; then
    # #3176: handle_merge calls ``gh pr merge <N> --squash --delete-branch``.
    if [[ -n "${GH_PR_MERGE_STDERR:-}" ]]; then
        printf '%s\n' "$GH_PR_MERGE_STDERR" >&2
    fi
    exit "${GH_PR_MERGE_EXIT:-0}"
fi

if [[ "${1:-}" == "run" && "${2:-}" == "view" ]]; then
    if [[ -n "${GH_RUN_LOG_FIXTURE:-}" && -f "$GH_RUN_LOG_FIXTURE" ]]; then
        cat "$GH_RUN_LOG_FIXTURE"
        exit 0
    fi
    exit 1
fi

if [[ "${1:-}" == "run" && "${2:-}" == "list" ]]; then
    # Shim-test fixture (Stage 2) first, then #3176 post-PR fixture,
    # then empty array fallback so awaiting_deploy's "no runs → verify"
    # branch lights up without fixture setup.
    if [[ -n "${GH_RUN_LIST_FIXTURE:-}" && -f "$GH_RUN_LIST_FIXTURE" ]]; then
        cat "$GH_RUN_LIST_FIXTURE"
        exit 0
    fi
    if [[ -n "${GH_RUN_LIST_JSON_FIXTURE:-}" && -f "$GH_RUN_LIST_JSON_FIXTURE" ]]; then
        cat "$GH_RUN_LIST_JSON_FIXTURE"
        exit 0
    fi
    printf '[]\n'
    exit "${GH_RUN_LIST_EXIT:-0}"
fi

# Legacy route kept for push_and_pr tests.
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

# Extend the psql stub with Stage 2 SELECTs on dispatcher.agents,
# dispatcher.phase_outputs, dispatcher.phase_transitions, and
# dispatcher.failures. Each new fixture env var is read on-demand so
# individual tests configure only what they need.
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

case "$query" in
    *"SELECT phase"*"FROM dispatcher.agents"*)
        if [[ -f "${PHASE_FIXTURE_FILE:-}" ]]; then
            head -n 1 "$PHASE_FIXTURE_FILE"
        fi
        exit 0
        ;;
    *"SELECT pr_number"*"FROM dispatcher.agents"*)
        # #3176 — post-PR handlers read pr_number from the agent row.
        printf '%s\n' "${PR_NUMBER_FIXTURE:-9999}"
        exit 0
        ;;
    *"SELECT COALESCE(merge_unstick_attempts"*|*"SELECT merge_unstick_attempts"*)
        # #3176 — handle_merge's auto-unstick budget check.
        printf '%s\n' "${MERGE_UNSTICK_ATTEMPTS_FIXTURE:-0}"
        exit 0
        ;;
    *"SELECT COALESCE(pr_number"*"FROM dispatcher.agents"*)
        printf '%s' "${DB_AGENT_PR_NUMBER:-0}"
        exit 0
        ;;
    *"SELECT COALESCE(retries_used"*"FROM dispatcher.agents"*)
        printf '%s' "${DB_AGENT_RETRIES_USED:-0}"
        exit 0
        ;;
    *"EXTRACT(EPOCH FROM (now() - started_at))"*"FROM dispatcher.agents"*)
        printf '%s' "${DB_AGENT_TOTAL_DURATION_S:-0}"
        exit 0
        ;;
    *"SELECT output_json"*"FROM dispatcher.phase_outputs"*)
        # Route by phase name substring.
        if [[ "$query" == *"phase = 'summary'"* && -f "${DB_SUMMARY_OUTPUT_FIXTURE:-}" ]]; then
            cat "$DB_SUMMARY_OUTPUT_FIXTURE"
        elif [[ "$query" == *"phase = 'verify'"* && -f "${DB_VERIFY_OUTPUT_FIXTURE:-}" ]]; then
            cat "$DB_VERIFY_OUTPUT_FIXTURE"
        fi
        exit 0
        ;;
    *"FROM dispatcher.phase_transitions"*)
        if [[ -f "${DB_PHASE_TRANSITIONS_FIXTURE:-}" ]]; then
            cat "$DB_PHASE_TRANSITIONS_FIXTURE"
        fi
        exit 0
        ;;
    *"FROM dispatcher.failures"*)
        if [[ -f "${DB_FAILURES_FIXTURE:-}" ]]; then
            cat "$DB_FAILURES_FIXTURE"
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
        # #3144 T26 knob — see equivalent in the earlier stub (L151).
        if [[ "${PSQL_FAIL_ON_INSERT:-0}" == "1" ]]; then
            exit 1
        fi
        exit 0
        ;;
    *"SELECT 1 FROM dispatcher.ralph_patches"*)
        if [[ "${RALPH_PATCH_EXISTS:-0}" == "1" ]]; then
            printf '1\n'
        fi
        exit 0
        ;;
    *"UPDATE dispatcher.agents"*"SET ralph_iterations_observed"*)
        # #3144 HEAD-watcher counter bump.
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
PSQLEOF
chmod +x "$STUB_BIN/psql"

# Helper: invoke the shim directly and print the JSON payload path.
run_shim() {
    # $1 phase, $2 agent_id, $3 issue_number, $4 repo_root
    local rc
    set +e
    python3 "$SHIM_PY" "$1" "$2" "$3" "$4" >/dev/null 2>&1
    rc=$?
    set -e
    return $rc
}

# ── Test 17: summary input parity (AC-1 of #3135) ─────────────────────────
setup_fixtures

t17_repo="$TEST_TMP/t17-repo"
mkdir -p "$t17_repo/.git" "$t17_repo/tmp/dispatcher-output"

# Stage ralph output so the shim reads ralph_summary + changed_files.
cat > "$t17_repo/tmp/dispatcher-output/ralph.json" <<'EOF'
{
  "agent_id": "17171717-1717-1717-1717-171717171717",
  "verdict": "SHIP",
  "summary": "Extended phase_input_shim with per-phase builders",
  "changed_files": ["scripts/dispatcher/agent-runner-entrypoint.sh", "scripts/tests/test_agent_runner_entrypoint.sh"]
}
EOF

cat > "$t17_repo/tmp/dispatcher-output/plan.json" <<'EOF'
{
  "agent_id": "17171717-1717-1717-1717-171717171717",
  "acceptance_criteria": ["summary parity", "fix-ci parity", "verify parity", "retro parity"],
  "scope_check": ["no daemon changes"],
  "change_type": "dx_tooling",
  "plan_text": "fixture plan body"
}
EOF

GH_ISSUE_FIXTURE="$TEST_TMP/t17-issue.json"
cat > "$GH_ISSUE_FIXTURE" <<'EOF'
{
  "number": 3135,
  "title": "feat(agent-runner): Stage 2 input parity",
  "body": "Body with AC.\n\n- [ ] summary parity\n- [ ] fix-ci parity",
  "labels": [{"name": "area/infra"}],
  "comments": [
    {"author": {"login": "drewthaler"}, "authorAssociation": "COLLABORATOR", "createdAt": "2026-04-23T00:00:00Z", "body": "human comment"},
    {"author": {"login": "github-actions[bot]"}, "authorAssociation": "NONE", "createdAt": "2026-04-23T00:00:00Z", "body": "bot comment"}
  ],
  "updatedAt": "2026-04-23T23:50:00Z"
}
EOF

set +e
DATABASE_URL="postgres://test" \
    GITHUB_REPO="judgemind/judgemind" \
    PATH="$STUB_BIN:$PATH" \
    INVOCATIONS_DIR="$INVOCATIONS_DIR" \
    GH_ISSUE_FIXTURE="$GH_ISSUE_FIXTURE" \
    python3 "$SHIM_PY" summary "17171717-1717-1717-1717-171717171717" 3135 "$t17_repo" \
    >/dev/null 2>&1
t17_rc=$?
set -e

t17_input="$t17_repo/tmp/dispatcher-input/summary.json"

if [[ $t17_rc -eq 0 && -f "$t17_input" ]]; then
    pass "#3135 AC-1 — summary shim writes dispatcher-input/summary.json"
else
    fail "#3135 AC-1 — summary shim writes dispatcher-input/summary.json" \
         "rc=$t17_rc, path=$t17_input"
fi

for field in agent_id issue_number issue_title issue_body issue_comments \
             ralph_summary changed_files git_diff branch \
             plan_acceptance_criteria scope_check worktree_path repo_root; do
    if jq -e "has(\"$field\")" "$t17_input" >/dev/null 2>&1; then
        pass "#3135 AC-1 — summary.json carries $field"
    else
        fail "#3135 AC-1 — summary.json carries $field" \
             "content: $(cat "$t17_input" 2>/dev/null)"
    fi
done

if jq -e '.ralph_summary == "Extended phase_input_shim with per-phase builders"' \
     "$t17_input" >/dev/null 2>&1; then
    pass "#3135 AC-1 — ralph_summary is read from ralph.json"
else
    fail "#3135 AC-1 — ralph_summary is read from ralph.json"
fi

if jq -e '.issue_title == "feat(agent-runner): Stage 2 input parity"' \
     "$t17_input" >/dev/null 2>&1; then
    pass "#3135 AC-1 — issue_title is refetched via gh issue view"
else
    fail "#3135 AC-1 — issue_title is refetched via gh issue view"
fi

if jq -e '.issue_comments | length == 1 and .[0].author == "drewthaler"' \
     "$t17_input" >/dev/null 2>&1; then
    pass "#3135 AC-1 — issue_comments excludes bot comments"
else
    fail "#3135 AC-1 — issue_comments excludes bot comments" \
         "content: $(jq '.issue_comments' "$t17_input" 2>/dev/null)"
fi

if jq -e '.plan_acceptance_criteria | length == 4' "$t17_input" >/dev/null 2>&1; then
    pass "#3135 AC-1 — plan_acceptance_criteria pulled from plan output"
else
    fail "#3135 AC-1 — plan_acceptance_criteria pulled from plan output"
fi

if jq -e '.changed_files | contains(["scripts/dispatcher/agent-runner-entrypoint.sh"])' \
     "$t17_input" >/dev/null 2>&1; then
    pass "#3135 AC-1 — changed_files preferred from ralph output"
else
    fail "#3135 AC-1 — changed_files preferred from ralph output"
fi

# ── Test 18: fix-ci input parity (AC-2 of #3135) ──────────────────────────
setup_fixtures

t18_repo="$TEST_TMP/t18-repo"
mkdir -p "$t18_repo/.git" "$t18_repo/tmp/dispatcher-output"

cat > "$t18_repo/tmp/dispatcher-output/plan.json" <<'EOF'
{"agent_id": "18181818-1818-1818-1818-181818181818", "change_type": "api"}
EOF

GH_PR_FIXTURE="$TEST_TMP/t18-pr.json"
cat > "$GH_PR_FIXTURE" <<'EOF'
{
  "statusCheckRollup": [
    {"name": "CI / python-tests", "status": "COMPLETED", "conclusion": "FAILURE", "databaseId": 9001, "detailsUrl": "https://github.com/judgemind/judgemind/actions/runs/9001"},
    {"name": "CI / lint", "status": "COMPLETED", "conclusion": "SUCCESS", "databaseId": 9002}
  ],
  "mergeable": "MERGEABLE",
  "mergeStateStatus": "CLEAN",
  "headRefOid": "deadbeef",
  "mergeCommit": null
}
EOF

GH_PR_DIFF_FIXTURE="$TEST_TMP/t18-pr-diff.patch"
cat > "$GH_PR_DIFF_FIXTURE" <<'EOF'
diff --git a/foo.py b/foo.py
index 111..222 100644
--- a/foo.py
+++ b/foo.py
@@ -1 +1,2 @@
 def f(): return 1
+# new line
EOF

GH_RUN_LOG_FIXTURE="$TEST_TMP/t18-run-log.txt"
cat > "$GH_RUN_LOG_FIXTURE" <<'EOF'
FAIL tests/test_foo.py::test_bar
AssertionError: expected 2, got 1
EOF

set +e
DATABASE_URL="postgres://test" \
    GITHUB_REPO="judgemind/judgemind" \
    PATH="$STUB_BIN:$PATH" \
    INVOCATIONS_DIR="$INVOCATIONS_DIR" \
    GH_PR_FIXTURE="$GH_PR_FIXTURE" \
    GH_PR_DIFF_FIXTURE="$GH_PR_DIFF_FIXTURE" \
    GH_RUN_LOG_FIXTURE="$GH_RUN_LOG_FIXTURE" \
    DB_AGENT_PR_NUMBER="4242" \
    DB_AGENT_RETRIES_USED="2" \
    python3 "$SHIM_PY" fix-ci "18181818-1818-1818-1818-181818181818" 3135 "$t18_repo" \
    >/dev/null 2>&1
t18_rc=$?
set -e

t18_input="$t18_repo/tmp/dispatcher-input/fix-ci.json"
if [[ $t18_rc -eq 0 && -f "$t18_input" ]]; then
    pass "#3135 AC-2 — fix-ci shim writes dispatcher-input/fix-ci.json"
else
    fail "#3135 AC-2 — fix-ci shim writes dispatcher-input/fix-ci.json" \
         "rc=$t18_rc, path=$t18_input"
fi

if jq -e '.pr_number == 4242' "$t18_input" >/dev/null 2>&1; then
    pass "#3135 AC-2 — pr_number read from dispatcher.agents"
else
    fail "#3135 AC-2 — pr_number read from dispatcher.agents" \
         "content: $(cat "$t18_input" 2>/dev/null)"
fi

if jq -e '.previous_fix_attempts == 2' "$t18_input" >/dev/null 2>&1; then
    pass "#3135 AC-2 — previous_fix_attempts read from dispatcher.agents.retries_used"
else
    fail "#3135 AC-2 — previous_fix_attempts read from dispatcher.agents.retries_used"
fi

if jq -e '.failing_jobs | length == 1' "$t18_input" >/dev/null 2>&1; then
    pass "#3135 AC-2 — failing_jobs filters to FAILURE conclusion"
else
    fail "#3135 AC-2 — failing_jobs filters to FAILURE conclusion" \
         "content: $(jq '.failing_jobs' "$t18_input" 2>/dev/null)"
fi

if jq -e '.failing_jobs[0].name == "CI / python-tests"' "$t18_input" >/dev/null 2>&1; then
    pass "#3135 AC-2 — failing_jobs carries job name"
else
    fail "#3135 AC-2 — failing_jobs carries job name"
fi

if jq -e '.failing_jobs[0].log_tail | contains("FAIL tests/test_foo.py")' \
     "$t18_input" >/dev/null 2>&1; then
    pass "#3135 AC-2 — failing_jobs[*].log_tail fetched via gh run view --log-failed"
else
    fail "#3135 AC-2 — failing_jobs[*].log_tail fetched via gh run view --log-failed" \
         "content: $(jq '.failing_jobs' "$t18_input" 2>/dev/null)"
fi

if jq -e '.git_diff_base_to_head | contains("def f(): return 1")' \
     "$t18_input" >/dev/null 2>&1; then
    pass "#3135 AC-2 — git_diff_base_to_head read via gh pr diff"
else
    fail "#3135 AC-2 — git_diff_base_to_head read via gh pr diff" \
         "content: $(jq -r '.git_diff_base_to_head' "$t18_input" 2>/dev/null | head -c 200)"
fi

if jq -e '.change_type == "api"' "$t18_input" >/dev/null 2>&1; then
    pass "#3135 AC-2 — change_type forwarded from plan output"
else
    fail "#3135 AC-2 — change_type forwarded from plan output"
fi

# Verify gh run view was called with --log-failed and --job.
if grep -F -- "--log-failed" "$INVOCATIONS_DIR/gh.log" >/dev/null 2>&1 \
   && grep -F -- "--job" "$INVOCATIONS_DIR/gh.log" >/dev/null 2>&1; then
    pass "#3135 AC-2 — shim invokes gh run view --log-failed --job"
else
    fail "#3135 AC-2 — shim invokes gh run view --log-failed --job" \
         "gh log: $(cat "$INVOCATIONS_DIR/gh.log")"
fi

# ── Test 19: verify input parity (AC-3 of #3135) ──────────────────────────
setup_fixtures

t19_repo="$TEST_TMP/t19-repo"
mkdir -p "$t19_repo/.git" "$t19_repo/tmp/dispatcher-output"

cat > "$t19_repo/tmp/dispatcher-output/plan.json" <<'EOF'
{
  "agent_id": "19191919-1919-1919-1919-191919191919",
  "acceptance_criteria": ["AC from plan"],
  "scope_check": ["scope_check entry"],
  "change_type": "api",
  "plan_text": "plan body text"
}
EOF

# gh issue view returns the issue bundle (used as fallback for AC).
GH_ISSUE_FIXTURE="$TEST_TMP/t19-issue.json"
cat > "$GH_ISSUE_FIXTURE" <<'EOF'
{
  "number": 3135,
  "title": "feat(agent-runner)",
  "body": "Body\n\n- [ ] AC from issue body\n",
  "labels": [],
  "comments": [],
  "updatedAt": "2026-04-23T00:00:00Z"
}
EOF

# gh pr view for the merged PR (mergeCommit.oid).
GH_PR_FIXTURE="$TEST_TMP/t19-pr.json"
cat > "$GH_PR_FIXTURE" <<'EOF'
{
  "state": "MERGED",
  "mergeCommit": {"oid": "abc123def456"},
  "headRefOid": "deadbeef"
}
EOF

# gh run list returns the deploy runs for the merge SHA.
GH_RUN_LIST_FIXTURE="$TEST_TMP/t19-run-list.json"
cat > "$GH_RUN_LIST_FIXTURE" <<'EOF'
[
  {"databaseId": 7001, "workflowName": "Deploy Dispatcher", "conclusion": "success", "status": "completed", "headSha": "abc123def456"},
  {"databaseId": 7002, "workflowName": "CI", "conclusion": "success", "status": "completed", "headSha": "abc123def456"}
]
EOF

# Summary's persisted phase_output carries deferred_acs.
DB_SUMMARY_OUTPUT_FIXTURE="$TEST_TMP/t19-summary-output.json"
cat > "$DB_SUMMARY_OUTPUT_FIXTURE" <<'EOF'
{"deferred_acs": [{"index": 3, "reason": "marker", "verify_instruction": "Verify: curl dev"}]}
EOF

set +e
DATABASE_URL="postgres://test" \
    GITHUB_REPO="judgemind/judgemind" \
    PATH="$STUB_BIN:$PATH" \
    INVOCATIONS_DIR="$INVOCATIONS_DIR" \
    GH_ISSUE_FIXTURE="$GH_ISSUE_FIXTURE" \
    GH_PR_FIXTURE="$GH_PR_FIXTURE" \
    GH_RUN_LIST_FIXTURE="$GH_RUN_LIST_FIXTURE" \
    DB_AGENT_PR_NUMBER="5050" \
    DB_SUMMARY_OUTPUT_FIXTURE="$DB_SUMMARY_OUTPUT_FIXTURE" \
    python3 "$SHIM_PY" verify "19191919-1919-1919-1919-191919191919" 3135 "$t19_repo" \
    >/dev/null 2>&1
t19_rc=$?
set -e

t19_input="$t19_repo/tmp/dispatcher-input/verify.json"
if [[ $t19_rc -eq 0 && -f "$t19_input" ]]; then
    pass "#3135 AC-3 — verify shim writes dispatcher-input/verify.json"
else
    fail "#3135 AC-3 — verify shim writes dispatcher-input/verify.json" \
         "rc=$t19_rc, path=$t19_input"
fi

if jq -e '.pr_number == 5050' "$t19_input" >/dev/null 2>&1; then
    pass "#3135 AC-3 — pr_number read from dispatcher.agents"
else
    fail "#3135 AC-3 — pr_number read from dispatcher.agents"
fi

if jq -e '.merged_commit_sha == "abc123def456"' "$t19_input" >/dev/null 2>&1; then
    pass "#3135 AC-3 — merged_commit_sha read via gh pr view --json mergeCommit"
else
    fail "#3135 AC-3 — merged_commit_sha read via gh pr view --json mergeCommit" \
         "content: $(cat "$t19_input" 2>/dev/null)"
fi

if jq -e '.deploy_status.workflow_name == "Deploy Dispatcher"' \
     "$t19_input" >/dev/null 2>&1; then
    pass "#3135 AC-3 — deploy_status derived from gh run list"
else
    fail "#3135 AC-3 — deploy_status derived from gh run list" \
         "content: $(jq '.deploy_status' "$t19_input" 2>/dev/null)"
fi

if jq -e '.touched_services | contains(["judgemind-dispatcher-dev"])' \
     "$t19_input" >/dev/null 2>&1; then
    pass "#3135 AC-3 — touched_services derived from deploy workflow names"
else
    fail "#3135 AC-3 — touched_services derived from deploy workflow names"
fi

if jq -e '.change_type == "dx_tooling"' "$t19_input" >/dev/null 2>&1; then
    pass "#3135 AC-3 — change_type inferred from deploy workflow"
else
    fail "#3135 AC-3 — change_type inferred from deploy workflow" \
         "value: $(jq -r '.change_type' "$t19_input" 2>/dev/null)"
fi

if jq -e '.deferred_acs | length == 1 and .[0].index == 3' \
     "$t19_input" >/dev/null 2>&1; then
    pass "#3135 AC-3 — deferred_acs read from summary's persisted phase_output"
else
    fail "#3135 AC-3 — deferred_acs read from summary's persisted phase_output" \
         "content: $(jq '.deferred_acs' "$t19_input" 2>/dev/null)"
fi

if jq -e '.acceptance_criteria | contains(["AC from plan"])' \
     "$t19_input" >/dev/null 2>&1; then
    pass "#3135 AC-3 — acceptance_criteria preferred from plan output"
else
    fail "#3135 AC-3 — acceptance_criteria preferred from plan output"
fi

# ── Test 20: retro input parity (AC-4 of #3135) ───────────────────────────
setup_fixtures

t20_repo="$TEST_TMP/t20-repo"
mkdir -p "$t20_repo/.git" "$t20_repo/tmp/dispatcher-output"

cat > "$t20_repo/tmp/dispatcher-output/plan.json" <<'EOF'
{
  "agent_id": "20202020-2020-2020-2020-202020202020",
  "scope_check_followups": ["scope item 1"],
  "follow_ups": ["follow-up A"]
}
EOF

# phase_transitions rows — two ralph, one awaiting_ci, one fix_ci.
DB_PHASE_TRANSITIONS_FIXTURE="$TEST_TMP/t20-phase-transitions.tsv"
printf 'planning\t2026-04-23T00:00:00Z\n' > "$DB_PHASE_TRANSITIONS_FIXTURE"
printf 'ralph\t2026-04-23T00:05:00Z\n' >> "$DB_PHASE_TRANSITIONS_FIXTURE"
printf 'ralph\t2026-04-23T00:10:00Z\n' >> "$DB_PHASE_TRANSITIONS_FIXTURE"
printf 'awaiting_ci\t2026-04-23T00:15:00Z\n' >> "$DB_PHASE_TRANSITIONS_FIXTURE"
printf 'fix_ci\t2026-04-23T00:20:00Z\n' >> "$DB_PHASE_TRANSITIONS_FIXTURE"

DB_FAILURES_FIXTURE="$TEST_TMP/t20-failures.tsv"
printf 'ci_red_after_retries\t2\t2026-04-23T00:15:00Z\t2026-04-23T00:20:00Z\n' > "$DB_FAILURES_FIXTURE"

DB_VERIFY_OUTPUT_FIXTURE="$TEST_TMP/t20-verify-output.json"
cat > "$DB_VERIFY_OUTPUT_FIXTURE" <<'EOF'
{"evidence_md": "## Verification evidence\n\nAll ACs pass."}
EOF

set +e
DATABASE_URL="postgres://test" \
    GITHUB_REPO="judgemind/judgemind" \
    PATH="$STUB_BIN:$PATH" \
    INVOCATIONS_DIR="$INVOCATIONS_DIR" \
    DB_AGENT_PR_NUMBER="6060" \
    DB_AGENT_TOTAL_DURATION_S="1800" \
    DB_PHASE_TRANSITIONS_FIXTURE="$DB_PHASE_TRANSITIONS_FIXTURE" \
    DB_FAILURES_FIXTURE="$DB_FAILURES_FIXTURE" \
    DB_VERIFY_OUTPUT_FIXTURE="$DB_VERIFY_OUTPUT_FIXTURE" \
    python3 "$SHIM_PY" retro "20202020-2020-2020-2020-202020202020" 3135 "$t20_repo" \
    >/dev/null 2>&1
t20_rc=$?
set -e

t20_input="$t20_repo/tmp/dispatcher-input/retro.json"
if [[ $t20_rc -eq 0 && -f "$t20_input" ]]; then
    pass "#3135 AC-4 — retro shim writes dispatcher-input/retro.json"
else
    fail "#3135 AC-4 — retro shim writes dispatcher-input/retro.json" \
         "rc=$t20_rc, path=$t20_input"
fi

if jq -e '.phase_transitions | length == 5' "$t20_input" >/dev/null 2>&1; then
    pass "#3135 AC-4 — phase_transitions read from dispatcher.phase_transitions"
else
    fail "#3135 AC-4 — phase_transitions read from dispatcher.phase_transitions" \
         "content: $(jq '.phase_transitions' "$t20_input" 2>/dev/null)"
fi

if jq -e '.ralph_iterations == 2' "$t20_input" >/dev/null 2>&1; then
    pass "#3135 AC-4 — ralph_iterations derived from phase_transitions count"
else
    fail "#3135 AC-4 — ralph_iterations derived from phase_transitions count" \
         "value: $(jq -r '.ralph_iterations' "$t20_input" 2>/dev/null)"
fi

if jq -e '.ci_attempts == 1 and .fix_ci_attempts == 1' "$t20_input" >/dev/null 2>&1; then
    pass "#3135 AC-4 — ci_attempts and fix_ci_attempts counted from transitions"
else
    fail "#3135 AC-4 — ci_attempts and fix_ci_attempts counted from transitions"
fi

if jq -e '.failures | length == 1 and .[0].category == "ci_red_after_retries" and .[0].count == 2' \
     "$t20_input" >/dev/null 2>&1; then
    pass "#3135 AC-4 — failures read from dispatcher.failures grouped"
else
    fail "#3135 AC-4 — failures read from dispatcher.failures grouped" \
         "content: $(jq '.failures' "$t20_input" 2>/dev/null)"
fi

if jq -e '.total_duration_s == 1800' "$t20_input" >/dev/null 2>&1; then
    pass "#3135 AC-4 — total_duration_s read via EXTRACT(EPOCH FROM ...)"
else
    fail "#3135 AC-4 — total_duration_s read via EXTRACT(EPOCH FROM ...)"
fi

if jq -e '.pr_number == 6060' "$t20_input" >/dev/null 2>&1; then
    pass "#3135 AC-4 — pr_number read from dispatcher.agents"
else
    fail "#3135 AC-4 — pr_number read from dispatcher.agents"
fi

if jq -e '.scope_check_followups | contains(["scope item 1"])' "$t20_input" >/dev/null 2>&1; then
    pass "#3135 AC-4 — scope_check_followups pulled from plan output"
else
    fail "#3135 AC-4 — scope_check_followups pulled from plan output"
fi

if jq -e '.plan_follow_ups | contains(["follow-up A"])' "$t20_input" >/dev/null 2>&1; then
    pass "#3135 AC-4 — plan_follow_ups pulled from plan output"
else
    fail "#3135 AC-4 — plan_follow_ups pulled from plan output"
fi

if jq -e '.verify_evidence_md | contains("Verification evidence")' \
     "$t20_input" >/dev/null 2>&1; then
    pass "#3135 AC-4 — verify_evidence_md read from phase_outputs"
else
    fail "#3135 AC-4 — verify_evidence_md read from phase_outputs"
fi

for field in diff_stats; do
    if jq -e "has(\"$field\")" "$t20_input" >/dev/null 2>&1; then
        pass "#3135 AC-4 — retro.json carries $field"
    else
        fail "#3135 AC-4 — retro.json carries $field"
    fi
done

# ── Test 21: fallback cleanliness (unknown phase returns base) ────────────
setup_fixtures

t21_repo="$TEST_TMP/t21-repo"
mkdir -p "$t21_repo/.git"

set +e
DATABASE_URL="postgres://test" \
    GITHUB_REPO="judgemind/judgemind" \
    PATH="$STUB_BIN:$PATH" \
    INVOCATIONS_DIR="$INVOCATIONS_DIR" \
    python3 "$SHIM_PY" bogus "21212121-2121-2121-2121-212121212121" 3135 "$t21_repo" \
    >/dev/null 2>&1
t21_rc=$?
set -e

t21_input="$t21_repo/tmp/dispatcher-input/bogus.json"
if [[ $t21_rc -eq 0 && -f "$t21_input" ]]; then
    pass "#3135 — unknown phase falls back to base identifiers (no crash)"
else
    fail "#3135 — unknown phase falls back to base identifiers (no crash)" \
         "rc=$t21_rc"
fi

if jq -e '.agent_id == "21212121-2121-2121-2121-212121212121"' "$t21_input" >/dev/null 2>&1; then
    pass "#3135 — unknown-phase fallback carries agent_id"
else
    fail "#3135 — unknown-phase fallback carries agent_id"
fi

# ══════════════════════════════════════════════════════════════════════════
# Tests 22-26: Ralph HEAD-watcher (#3144)
#
# The watcher is a subshell started before run_claude_phase "ralph" and
# killed after. These tests exercise it in isolation via
# AGENT_RUNNER_WATCHER_TEST_MODE=1, which runs start → sleep → stop
# without spinning the full phase loop. The git stub's `log`
# subcommand reads RALPH_HEAD_WATCHER_COMMITS_FILE so the test can
# mutate the commit set mid-run to simulate ralph committing.
# ══════════════════════════════════════════════════════════════════════════

# Shared helper to run the watcher in test mode with consistent env.
# Arguments:
#   $1 = workspace dir
#   $2 = commits fixture path that the watcher's `git log` stub will
#        read from (pre-seeded empty; the seeder below overwrites it).
#   $3 = seed-commits fixture — a separate file the entrypoint's test
#        hook copies INTO $2 after the watcher takes its baseline
#        snapshot. Pass "" to simulate "no new commits during ralph".
#   $4 = sleep seconds (how long the watcher runs after seeding)
#   $5 = poll interval seconds
#   $6 = extra env vars (list of VAR=VAL tokens, one per slot)
run_watcher_test() {
    _wtest_workspace="$1"
    _wtest_commits="$2"
    _wtest_seed="$3"
    _wtest_sleep="$4"
    _wtest_poll="$5"
    _wtest_extra="$6"
    mkdir -p "$_wtest_workspace"

    set +e
    # Start with an empty commits file so the watcher's baseline
    # captures zero commits. The AGENT_RUNNER_WATCHER_TEST_SEED_COMMITS
    # hook inside the entrypoint then cp's the seed into $_wtest_commits
    # so the FIRST tick sees them as "new" and fires events.
    : > "$_wtest_commits"

    # Pass the extra VAR=VAL tokens via explicit export-in-a-subshell.
    # An earlier design passed them through an array ``_cmd=(env A=1
    # $extra bash …)`` with ``$extra`` word-splitting, but the
    # unquoted expansion inside a bash 3.2 array initializer dropped
    # the extra tokens in the real test run (setup_fixtures context?
    # shopt inheritance? unclear — the direct-repro script worked
    # fine). An explicit ``export`` in a subshell is unambiguous.
    (
        export AGENT_ID="abababab-cdcd-efef-0101-020202020202"
        export ISSUE_NUMBER="3144"
        export DATABASE_URL="postgres://test"
        export GITHUB_TOKEN=""
        export AGENT_WORKSPACE="$_wtest_workspace"
        export REPO_URL="https://example.invalid/repo.git"
        export PATH="$STUB_BIN:$PATH"
        export INVOCATIONS_DIR="$INVOCATIONS_DIR"
        export PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher"
        export PHASE_TRANSITIONS_PARENT="$REPO_ROOT"
        export AGENT_RUNNER_WATCHER_TEST_MODE=1
        export AGENT_RUNNER_WATCHER_TEST_SLEEP="$_wtest_sleep"
        export AGENT_RUNNER_RALPH_HEAD_POLL_INTERVAL="$_wtest_poll"
        export RALPH_HEAD_WATCHER_COMMITS_FILE="$_wtest_commits"
        export AGENT_RUNNER_WATCHER_TEST_SEED_COMMITS="$_wtest_seed"
        if [[ -n "$_wtest_extra" ]]; then
            # Tokens are space-separated VAR=VAL pairs. Export each.
            for _tok in $_wtest_extra; do
                export "$_tok"
            done
        fi
        bash "$ENTRYPOINT" 2>&1
    )
    _wtest_rc=$?
    set -e
    return $_wtest_rc
}

# ── Test 22: Zero-iteration ralph — no commits, no ralph_patches rows ──────
setup_fixtures

t22_workspace="$TEST_TMP/t22-workspace"
t22_commits="$TEST_TMP/t22-commits.txt"
t22_seed=""   # no seed → nothing new appears during ralph

set +e
t22_out=$(run_watcher_test "$t22_workspace" "$t22_commits" "$t22_seed" 1 1 "")
t22_rc=$?
set -e

if [[ $t22_rc -eq 0 ]]; then
    pass "#3144 T22 — watcher test mode exits 0 with no commits"
else
    fail "#3144 T22 — watcher test mode exits 0 with no commits" \
         "rc=$t22_rc, out tail: $(printf '%s\n' "$t22_out" | tail -10)"
fi

if printf '%s' "$t22_out" | grep -q "ralph_head_watcher_started"; then
    pass "#3144 T22 — watcher logs ralph_head_watcher_started"
else
    fail "#3144 T22 — watcher logs ralph_head_watcher_started" "out: $t22_out"
fi

if printf '%s' "$t22_out" | grep -q "ralph_head_watcher_stopped"; then
    pass "#3144 T22 — watcher logs ralph_head_watcher_stopped"
else
    fail "#3144 T22 — watcher logs ralph_head_watcher_stopped" "out: $t22_out"
fi

# No ralph_iteration_observed events when no commits.
if printf '%s' "$t22_out" | grep -q "ralph_iteration_observed"; then
    fail "#3144 T22 — no ralph_iteration_observed when no commits" "out: $t22_out"
else
    pass "#3144 T22 — no ralph_iteration_observed when no commits"
fi

# No INSERT into ralph_patches.
if grep -q "INSERT INTO dispatcher.ralph_patches" "$INVOCATIONS_DIR/psql.log"; then
    fail "#3144 T22 — no ralph_patches INSERT when no commits" \
         "psql log: $(cat "$INVOCATIONS_DIR/psql.log")"
else
    pass "#3144 T22 — no ralph_patches INSERT when no commits"
fi

# ── Test 23: Single-iteration — one commit seeded, expect 1 INSERT + UPDATE
setup_fixtures

t23_workspace="$TEST_TMP/t23-workspace"
t23_commits="$TEST_TMP/t23-commits.txt"
t23_seed="$TEST_TMP/t23-seed.txt"
printf 'aaaaaaa1 first ralph iteration commit\n' > "$t23_seed"

set +e
t23_out=$(run_watcher_test "$t23_workspace" "$t23_commits" "$t23_seed" 3 1 "")
t23_rc=$?
set -e

if [[ $t23_rc -eq 0 ]]; then
    pass "#3144 T23 — watcher exits 0 with one commit"
else
    fail "#3144 T23 — watcher exits 0 with one commit" "rc=$t23_rc"
fi

# Exactly one ralph_iteration_observed event.
_t23_iter_count=$(printf '%s' "$t23_out" | grep -c "ralph_iteration_observed" || true)
if [[ "$_t23_iter_count" -eq 1 ]]; then
    pass "#3144 T23 — exactly one ralph_iteration_observed event"
else
    fail "#3144 T23 — exactly one ralph_iteration_observed event" \
         "got $_t23_iter_count events. out: $t23_out"
fi

# The event carries iteration_n=1 and the seeded commit SHA.
if printf '%s' "$t23_out" | grep "ralph_iteration_observed" | grep -q "iteration_n\": \"1\""; then
    pass "#3144 T23 — ralph_iteration_observed carries iteration_n=1"
else
    fail "#3144 T23 — ralph_iteration_observed carries iteration_n=1" \
         "event line: $(printf '%s' "$t23_out" | grep 'ralph_iteration_observed' | head -1)"
fi

if printf '%s' "$t23_out" | grep "ralph_iteration_observed" | grep -q "aaaaaaa1"; then
    pass "#3144 T23 — ralph_iteration_observed carries commit_sha"
else
    fail "#3144 T23 — ralph_iteration_observed carries commit_sha"
fi

if printf '%s' "$t23_out" | grep "ralph_iteration_observed" \
     | grep -q "commit_subject_first_80\": \"first ralph iteration commit"; then
    pass "#3144 T23 — event carries truncated commit subject"
else
    fail "#3144 T23 — event carries truncated commit subject" \
         "event line: $(printf '%s' "$t23_out" | grep 'ralph_iteration_observed' | head -1)"
fi

# Exactly one INSERT INTO dispatcher.ralph_patches.
_t23_insert_count=$(grep -c "INSERT INTO dispatcher.ralph_patches" "$INVOCATIONS_DIR/psql.log" 2>/dev/null || true)
_t23_insert_count=${_t23_insert_count:-0}
if [[ "$_t23_insert_count" -eq 1 ]]; then
    pass "#3144 T23 — one INSERT into dispatcher.ralph_patches"
else
    fail "#3144 T23 — one INSERT into dispatcher.ralph_patches" \
         "got $_t23_insert_count inserts"
fi

# Exactly one UPDATE to dispatcher.agents SET ralph_iterations_observed.
_t23_update_count=$(grep -c "ralph_iterations_observed" "$INVOCATIONS_DIR/psql.log" 2>/dev/null || true)
_t23_update_count=${_t23_update_count:-0}
if [[ "$_t23_update_count" -ge 1 ]]; then
    pass "#3144 T23 — UPDATE dispatcher.agents SET ralph_iterations_observed"
else
    fail "#3144 T23 — UPDATE dispatcher.agents SET ralph_iterations_observed" \
         "psql log: $(cat "$INVOCATIONS_DIR/psql.log")"
fi

# SELECT-then-INSERT guard against double-emit races. The watcher
# issues a SELECT for an existing (agent_id, iteration_n) row before
# each INSERT. This avoids adding a DB uniqueness constraint that
# would also affect the daemon's subprocess-mode insert path — see
# migration 42's rationale for the non-change.
if grep -F "SELECT 1 FROM dispatcher.ralph_patches" "$INVOCATIONS_DIR/psql.log" \
     | grep -q "iteration_n"; then
    pass "#3144 T23 — SELECT guard runs before each INSERT"
else
    fail "#3144 T23 — SELECT guard runs before each INSERT" \
         "psql log: $(head -c 500 "$INVOCATIONS_DIR/psql.log")"
fi

# ── Test 24: Multi-iteration — three commits, expect iteration_n 1..3 ──────
setup_fixtures

t24_workspace="$TEST_TMP/t24-workspace"
t24_commits="$TEST_TMP/t24-commits.txt"
t24_seed="$TEST_TMP/t24-seed.txt"
printf 'ddddddd1 iteration one subject\n' > "$t24_seed"
printf 'ddddddd2 iteration two subject\n' >> "$t24_seed"
printf 'ddddddd3 iteration three subject\n' >> "$t24_seed"

set +e
t24_out=$(run_watcher_test "$t24_workspace" "$t24_commits" "$t24_seed" 3 1 "")
t24_rc=$?
set -e

if [[ $t24_rc -eq 0 ]]; then
    pass "#3144 T24 — watcher exits 0 with three commits"
else
    fail "#3144 T24 — watcher exits 0 with three commits" "rc=$t24_rc"
fi

# Three ralph_iteration_observed events, one per commit.
_t24_iter_count=$(printf '%s' "$t24_out" | grep -c "ralph_iteration_observed" || true)
if [[ "$_t24_iter_count" -eq 3 ]]; then
    pass "#3144 T24 — exactly three ralph_iteration_observed events"
else
    fail "#3144 T24 — exactly three ralph_iteration_observed events" \
         "got $_t24_iter_count events"
fi

# Iteration numbers are 1, 2, 3 in order.
for n in 1 2 3; do
    if printf '%s' "$t24_out" | grep "ralph_iteration_observed" \
         | grep -q "iteration_n\": \"$n\""; then
        pass "#3144 T24 — iteration_n=$n observed"
    else
        fail "#3144 T24 — iteration_n=$n observed"
    fi
done

# Three INSERT statements, one per iteration.
_t24_insert_count=$(grep -c "INSERT INTO dispatcher.ralph_patches" "$INVOCATIONS_DIR/psql.log" 2>/dev/null || true)
_t24_insert_count=${_t24_insert_count:-0}
if [[ "$_t24_insert_count" -eq 3 ]]; then
    pass "#3144 T24 — three INSERTs into dispatcher.ralph_patches"
else
    fail "#3144 T24 — three INSERTs into dispatcher.ralph_patches" \
         "got $_t24_insert_count"
fi

# ── Test 25: Shutdown race — ralph exits mid-poll, watcher dies cleanly ────
setup_fixtures

t25_workspace="$TEST_TMP/t25-workspace"
t25_commits="$TEST_TMP/t25-commits.txt"
t25_seed=""   # no commits to seed — testing the teardown path

# Sleep 0s, poll every 60s. start_ralph_head_watcher forks, the test-
# mode driver immediately runs stop (sleep 0), so the subshell is
# killed mid-first-tick. Assert: no dangling process, no errors.
set +e
t25_out=$(run_watcher_test "$t25_workspace" "$t25_commits" "$t25_seed" 0 60 "")
t25_rc=$?
set -e

if [[ $t25_rc -eq 0 ]]; then
    pass "#3144 T25 — shutdown-race: watcher exits 0 when killed mid-poll"
else
    fail "#3144 T25 — shutdown-race: watcher exits 0 when killed mid-poll" \
         "rc=$t25_rc, out: $(printf '%s\n' "$t25_out" | tail -10)"
fi

if printf '%s' "$t25_out" | grep -q "ralph_head_watcher_kill_sent"; then
    pass "#3144 T25 — watcher logs kill_sent on shutdown"
else
    fail "#3144 T25 — watcher logs kill_sent on shutdown" "out: $t25_out"
fi

if printf '%s' "$t25_out" | grep -q "ralph_head_watcher_stopped"; then
    pass "#3144 T25 — watcher logs stopped event after wait"
else
    fail "#3144 T25 — watcher logs stopped event after wait" "out: $t25_out"
fi

# No partial row / no INSERT attempted (no commits were in the list).
if grep -q "INSERT INTO dispatcher.ralph_patches" "$INVOCATIONS_DIR/psql.log"; then
    fail "#3144 T25 — no partial ralph_patches INSERT on shutdown race" \
         "psql log: $(cat "$INVOCATIONS_DIR/psql.log")"
else
    pass "#3144 T25 — no partial ralph_patches INSERT on shutdown race"
fi

# ── Test 26: DB down — watcher logs warning, doesn't crash ────────────────
setup_fixtures

t26_workspace="$TEST_TMP/t26-workspace"
t26_commits="$TEST_TMP/t26-commits.txt"
t26_seed="$TEST_TMP/t26-seed.txt"
printf 'eeeeeee1 iteration with DB down\n' > "$t26_seed"

# PSQL_FAIL_ON_INSERT=1 tells our stub to exit 1 for any INSERT. The
# watcher should log ralph_head_watcher_db_failure and continue; the
# agent-runner top-level must not crash.
set +e
t26_out=$(run_watcher_test "$t26_workspace" "$t26_commits" "$t26_seed" 3 1 "PSQL_FAIL_ON_INSERT=1")
t26_rc=$?
set -e

if [[ $t26_rc -eq 0 ]]; then
    pass "#3144 T26 — watcher exits 0 even with DB INSERT failures"
else
    fail "#3144 T26 — watcher exits 0 even with DB INSERT failures" \
         "rc=$t26_rc, out: $(printf '%s\n' "$t26_out" | tail -10)"
fi

if printf '%s' "$t26_out" | grep -q "ralph_head_watcher_db_failure"; then
    pass "#3144 T26 — watcher logs ralph_head_watcher_db_failure on INSERT error"
else
    fail "#3144 T26 — watcher logs ralph_head_watcher_db_failure on INSERT error" \
         "out: $t26_out"
fi

# The observed event still fires before the DB error (defensible — the
# user sees the commit appeared in CloudWatch even if persist failed).
if printf '%s' "$t26_out" | grep -q "ralph_iteration_observed"; then
    pass "#3144 T26 — ralph_iteration_observed still emitted on DB failure"
else
    fail "#3144 T26 — ralph_iteration_observed still emitted on DB failure"
fi

# ── Test 27: SELECT-then-INSERT skips duplicate (agent_id, iteration_n) ────
setup_fixtures

t27_workspace="$TEST_TMP/t27-workspace"
t27_commits="$TEST_TMP/t27-commits.txt"
t27_seed="$TEST_TMP/t27-seed.txt"
printf 'fffffff1 should be skipped as duplicate\n' > "$t27_seed"

# RALPH_PATCH_EXISTS=1 makes the stub's SELECT return "1" → the
# watcher treats this iteration as already-persisted and logs
# ralph_head_watcher_skip_existing instead of attempting the INSERT.
set +e
t27_out=$(run_watcher_test "$t27_workspace" "$t27_commits" "$t27_seed" 3 1 "RALPH_PATCH_EXISTS=1")
t27_rc=$?
set -e

if [[ $t27_rc -eq 0 ]]; then
    pass "#3144 T27 — watcher exits 0 on SELECT-exists guard hit"
else
    fail "#3144 T27 — watcher exits 0 on SELECT-exists guard hit" \
         "rc=$t27_rc"
fi

if printf '%s' "$t27_out" | grep -q "ralph_head_watcher_skip_existing"; then
    pass "#3144 T27 — watcher logs skip_existing when SELECT finds a prior row"
else
    fail "#3144 T27 — watcher logs skip_existing when SELECT finds a prior row" \
         "out: $t27_out"
fi

# No INSERT should be attempted for the duplicate iteration.
if grep -q "INSERT INTO dispatcher.ralph_patches" "$INVOCATIONS_DIR/psql.log"; then
    fail "#3144 T27 — no INSERT when SELECT-exists guard fires" \
         "psql log: $(cat "$INVOCATIONS_DIR/psql.log")"
else
    pass "#3144 T27 — no INSERT when SELECT-exists guard fires"
fi

# ══════════════════════════════════════════════════════════════════════════
# #3176 — real implementations for awaiting_ci / merge / awaiting_deploy.
# Each test starts at the phase under test and drives one iteration.
#
# Shared helper — runs the entrypoint with the env vars the post-PR
# handlers need (0-second polls so timeouts + happy-paths finish
# instantly against the stubbed gh/psql).
# ══════════════════════════════════════════════════════════════════════════

run_post_pr_phase() {
    # $1 = starting phase (awaiting_ci | merge | awaiting_deploy)
    # $2 = workspace dir
    # Remaining args are `VAR=VAL` tokens exported for the run.
    _rpp_start_phase="$1"
    _rpp_workspace="$2"
    shift 2
    mkdir -p "$_rpp_workspace"

    set +e
    (
        export AGENT_ID="fe28f05f-0000-0000-0000-000000003176"
        export ISSUE_NUMBER="3176"
        export DATABASE_URL="postgres://test"
        export GITHUB_TOKEN=""
        export AGENT_WORKSPACE="$_rpp_workspace"
        export REPO_URL="https://example.invalid/repo.git"
        export PATH="$STUB_BIN:$PATH"
        export INVOCATIONS_DIR="$INVOCATIONS_DIR"
        export PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher"
        export PHASE_TRANSITIONS_PARENT="$REPO_ROOT"
        export AGENT_RUNNER_MAX_PHASE_ITERATIONS=10
        # Poll every 0 seconds so any while-loop exits immediately;
        # timeouts are 0 too so tests can drive the timeout branch.
        export AGENT_RUNNER_CI_POLL_INTERVAL=0
        export AGENT_RUNNER_DEPLOY_POLL_INTERVAL=0
        export AGENT_RUNNER_DEPLOY_GRACE_SECONDS="${AGENT_RUNNER_DEPLOY_GRACE_SECONDS:-0}"
        for _tok in "$@"; do
            export "$_tok"
        done
        bash "$ENTRYPOINT" 2>&1
    )
    _rpp_rc=$?
    set -e
    return $_rpp_rc
}

# ══════════════════════════════════════════════════════════════════════════
# Test 28: awaiting_ci + green rollup → advances to merge.
# ══════════════════════════════════════════════════════════════════════════
setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t28.txt"
printf 'awaiting_ci\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t28_workspace="$TEST_TMP/t28-workspace"
set +e
t28_out=$(run_post_pr_phase "awaiting_ci" "$t28_workspace" \
    "PHASE_FIXTURE_FILE=$PHASE_FIXTURE_FILE" \
    "PRIOR_PATCH_FIXTURE=" \
    "PR_NUMBER_FIXTURE=9999")
set -e

if printf '%s' "$t28_out" | grep -q '"rollup_state": "green"'; then
    pass "#3176 T28 — awaiting_ci green rollup classified"
else
    fail "#3176 T28 — awaiting_ci green rollup classified" \
         "out tail: $(printf '%s' "$t28_out" | tail -c 500)"
fi

# awaiting_ci green → advances to merge, which then runs (loop) to
# awaiting_deploy. We assert SET phase = 'merge' appears.
if grep -q "SET phase = \\\\'merge\\\\'" "$INVOCATIONS_DIR/psql.log"; then
    pass "#3176 T28 — awaiting_ci advances to merge on green"
else
    fail "#3176 T28 — awaiting_ci advances to merge on green" \
         "psql log tail: $(tail -c 500 "$INVOCATIONS_DIR/psql.log")"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 29: awaiting_ci + red rollup → advances to fix_ci (NOT merge).
# ══════════════════════════════════════════════════════════════════════════
setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t29.txt"
printf 'awaiting_ci\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

# Write a red-rollup fixture.
t29_pr_view="$TEST_TMP/t29-pr-view.json"
cat > "$t29_pr_view" <<'EOF'
{
  "statusCheckRollup": [
    {"name": "ci-passed", "status": "COMPLETED", "conclusion": "FAILURE"}
  ],
  "mergeable": "MERGEABLE",
  "mergeStateStatus": "CLEAN",
  "headRefOid": "deadbeefcafe",
  "mergeCommit": null
}
EOF

t29_workspace="$TEST_TMP/t29-workspace"
set +e
t29_out=$(run_post_pr_phase "awaiting_ci" "$t29_workspace" \
    "PHASE_FIXTURE_FILE=$PHASE_FIXTURE_FILE" \
    "PRIOR_PATCH_FIXTURE=" \
    "PR_NUMBER_FIXTURE=9999" \
    "GH_PR_VIEW_JSON_FIXTURE=$t29_pr_view" \
    "CLAUDE_VERDICT_FIXTURE=")
set -e

if printf '%s' "$t29_out" | grep -q '"rollup_state": "red"'; then
    pass "#3176 T29 — awaiting_ci red rollup classified"
else
    fail "#3176 T29 — awaiting_ci red rollup classified" \
         "out tail: $(printf '%s' "$t29_out" | tail -c 500)"
fi

if grep -q "SET phase = \\\\'fix_ci\\\\'" "$INVOCATIONS_DIR/psql.log"; then
    pass "#3176 T29 — awaiting_ci advances to fix_ci on red"
else
    fail "#3176 T29 — awaiting_ci advances to fix_ci on red" \
         "psql log tail: $(tail -c 500 "$INVOCATIONS_DIR/psql.log")"
fi

# Verify merge was NOT invoked — red rollup must not trigger a merge.
# We filter the gh log for lines containing `pr merge`.
if grep -F "pr merge" "$INVOCATIONS_DIR/gh.log" >/dev/null 2>&1; then
    fail "#3176 T29 — no gh pr merge invoked on red CI" \
         "gh log: $(cat "$INVOCATIONS_DIR/gh.log")"
else
    pass "#3176 T29 — no gh pr merge invoked on red CI"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 30: merge stale-rollup → auto-unstick (empty commit + push) and
# phase stays at awaiting_ci for next poll.
# ══════════════════════════════════════════════════════════════════════════
setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t30.txt"
printf 'merge\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t30_workspace="$TEST_TMP/t30-workspace"
set +e
t30_out=$(run_post_pr_phase "merge" "$t30_workspace" \
    "PHASE_FIXTURE_FILE=$PHASE_FIXTURE_FILE" \
    "PRIOR_PATCH_FIXTURE=" \
    "PR_NUMBER_FIXTURE=9999" \
    "MERGE_UNSTICK_ATTEMPTS_FIXTURE=0" \
    "GH_PR_MERGE_EXIT=1" \
    "GH_PR_MERGE_STDERR=error: base branch policy prohibits the merge" \
    "GIT_REV_LIST_COUNT=1")
set -e

if printf '%s' "$t30_out" | grep -q "merge_stale_rollup_detected"; then
    pass "#3176 T30 — merge stale-rollup detected"
else
    fail "#3176 T30 — merge stale-rollup detected" \
         "out tail: $(printf '%s' "$t30_out" | tail -c 500)"
fi

if printf '%s' "$t30_out" | grep -q "merge_auto_unstick_empty_commit_pushed"; then
    pass "#3176 T30 — auto-unstick empty-commit push succeeded"
else
    fail "#3176 T30 — auto-unstick empty-commit push succeeded" \
         "out tail: $(printf '%s' "$t30_out" | tail -c 500)"
fi

# Phase bounces back to awaiting_ci for the next poll.
if grep -q "SET phase = \\\\'awaiting_ci\\\\'" "$INVOCATIONS_DIR/psql.log"; then
    pass "#3176 T30 — stale-rollup unstick returns phase to awaiting_ci"
else
    fail "#3176 T30 — stale-rollup unstick returns phase to awaiting_ci" \
         "psql log tail: $(tail -c 500 "$INVOCATIONS_DIR/psql.log")"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 31: merge stale-rollup with budget exhausted → terminal failure,
# no additional empty commit attempted.
# ══════════════════════════════════════════════════════════════════════════
setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t31.txt"
printf 'merge\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t31_workspace="$TEST_TMP/t31-workspace"
set +e
t31_out=$(run_post_pr_phase "merge" "$t31_workspace" \
    "PHASE_FIXTURE_FILE=$PHASE_FIXTURE_FILE" \
    "PRIOR_PATCH_FIXTURE=" \
    "PR_NUMBER_FIXTURE=9999" \
    "MERGE_UNSTICK_ATTEMPTS_FIXTURE=1" \
    "GH_PR_MERGE_EXIT=1" \
    "GH_PR_MERGE_STDERR=error: base branch policy prohibits the merge")
set -e

if printf '%s' "$t31_out" | grep -q "merge_unstick_exhausted"; then
    pass "#3176 T31 — merge unstick exhausted logged"
else
    fail "#3176 T31 — merge unstick exhausted logged" \
         "out tail: $(printf '%s' "$t31_out" | tail -c 500)"
fi

if printf '%s' "$t31_out" | grep -q "agent_runner_reaped_failure"; then
    pass "#3176 T31 — agent_runner_reaped_failure emitted"
else
    fail "#3176 T31 — agent_runner_reaped_failure emitted" \
         "out tail: $(printf '%s' "$t31_out" | tail -c 500)"
fi

# Phase advances to merge_failed (terminal).
_t31_final=$(cat "$PHASE_FIXTURE_FILE" 2>/dev/null || printf '')
if [[ "$_t31_final" == "merge_failed" ]]; then
    pass "#3176 T31 — phase terminates at merge_failed"
else
    fail "#3176 T31 — phase terminates at merge_failed" \
         "actual final phase: $_t31_final"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 32: awaiting_deploy with no deploy workflows fired → short-grace
# advance to verify.
# ══════════════════════════════════════════════════════════════════════════
setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t32.txt"
printf 'awaiting_deploy\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

# Empty runs list → classify as "none" → awaiting_deploy advances to
# verify after the grace window elapses. Grace window set to 0 so we
# don't need to wait.
t32_runs="$TEST_TMP/t32-runs.json"
printf '[]\n' > "$t32_runs"

t32_workspace="$TEST_TMP/t32-workspace"
set +e
t32_out=$(run_post_pr_phase "awaiting_deploy" "$t32_workspace" \
    "PHASE_FIXTURE_FILE=$PHASE_FIXTURE_FILE" \
    "PRIOR_PATCH_FIXTURE=" \
    "PR_NUMBER_FIXTURE=9999" \
    "GH_RUN_LIST_JSON_FIXTURE=$t32_runs" \
    "AGENT_RUNNER_DEPLOY_GRACE_SECONDS=0")
set -e

if printf '%s' "$t32_out" | grep -q "awaiting_deploy_no_runs"; then
    pass "#3176 T32 — awaiting_deploy no-runs branch hit"
else
    fail "#3176 T32 — awaiting_deploy no-runs branch hit" \
         "out tail: $(printf '%s' "$t32_out" | tail -c 500)"
fi

if grep -q "SET phase = \\\\'verify\\\\'" "$INVOCATIONS_DIR/psql.log"; then
    pass "#3176 T32 — awaiting_deploy no-runs advances to verify"
else
    fail "#3176 T32 — awaiting_deploy no-runs advances to verify" \
         "psql log tail: $(tail -c 500 "$INVOCATIONS_DIR/psql.log")"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 33: awaiting_deploy timeout → terminal failure.
# ══════════════════════════════════════════════════════════════════════════
setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t33.txt"
printf 'awaiting_deploy\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

# Pending runs → classify as "pending" forever. Timeout=0 forces the
# first-tick timeout branch.
t33_runs="$TEST_TMP/t33-runs.json"
cat > "$t33_runs" <<'EOF'
[
  {"databaseId": 1, "workflowName": "Deploy Dispatcher", "status": "IN_PROGRESS", "conclusion": null, "createdAt": "2026-04-23T00:00:00Z"}
]
EOF

t33_workspace="$TEST_TMP/t33-workspace"
set +e
t33_out=$(run_post_pr_phase "awaiting_deploy" "$t33_workspace" \
    "PHASE_FIXTURE_FILE=$PHASE_FIXTURE_FILE" \
    "PRIOR_PATCH_FIXTURE=" \
    "PR_NUMBER_FIXTURE=9999" \
    "GH_RUN_LIST_JSON_FIXTURE=$t33_runs" \
    "AGENT_RUNNER_AWAITING_DEPLOY_TIMEOUT_SECONDS=0" \
    "AGENT_RUNNER_DEPLOY_GRACE_SECONDS=9999")
set -e

if printf '%s' "$t33_out" | grep -q "awaiting_deploy_timeout"; then
    pass "#3176 T33 — awaiting_deploy timeout logged"
else
    fail "#3176 T33 — awaiting_deploy timeout logged" \
         "out tail: $(printf '%s' "$t33_out" | tail -c 500)"
fi

_t33_final=$(cat "$PHASE_FIXTURE_FILE" 2>/dev/null || printf '')
if [[ "$_t33_final" == "awaiting_deploy_timeout" ]]; then
    pass "#3176 T33 — phase terminates at awaiting_deploy_timeout"
else
    fail "#3176 T33 — phase terminates at awaiting_deploy_timeout" \
         "actual final phase: $_t33_final"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 34: push_and_pr reads summary.json for pr_title/pr_body_md and
# commit_message; persists pr_number after successful PR create.
# ══════════════════════════════════════════════════════════════════════════
setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t34.txt"
printf 'push_and_pr\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t34_workspace="$TEST_TMP/t34-workspace"
mkdir -p "$t34_workspace/repo/tmp/dispatcher-output"
cat > "$t34_workspace/repo/tmp/dispatcher-output/summary.json" <<'EOF'
{
  "commit_message": "feat(agent-runner): real post-PR handlers (#3176)\n\nCloses #3176",
  "pr_title": "feat(agent-runner): real post-PR handlers (#3176)",
  "pr_body_md": "## Summary\nReal impls of awaiting_ci / merge / awaiting_deploy.\n\n## Test plan\n- [x] unit tests\n"
}
EOF

set +e
t34_out=$(run_post_pr_phase "push_and_pr" "$t34_workspace" \
    "PHASE_FIXTURE_FILE=$PHASE_FIXTURE_FILE" \
    "PRIOR_PATCH_FIXTURE=" \
    "PR_NUMBER_FIXTURE=9999" \
    "GIT_REV_LIST_COUNT=1" \
    "CLAUDE_VERDICT_FIXTURE=")
set -e

# Summary output was read.
if printf '%s' "$t34_out" | grep -q "push_and_pr_summary_output_read"; then
    pass "#3176 T34 — push_and_pr reads tmp/dispatcher-output/summary.json"
else
    fail "#3176 T34 — push_and_pr reads tmp/dispatcher-output/summary.json" \
         "out tail: $(printf '%s' "$t34_out" | tail -c 500)"
fi

# git commit --amend was invoked with -F.
if grep -F "commit --amend -F" "$INVOCATIONS_DIR/git.log" >/dev/null 2>&1 \
   || grep -F "commit" "$INVOCATIONS_DIR/git.log" | grep -F "amend" >/dev/null 2>&1; then
    pass "#3176 T34 — push_and_pr amends commit with summary's commit_message"
else
    fail "#3176 T34 — push_and_pr amends commit with summary's commit_message" \
         "git log: $(cat "$INVOCATIONS_DIR/git.log")"
fi

# git fetch origin main was invoked (pre-push rebase).
if grep -F "fetch" "$INVOCATIONS_DIR/git.log" | grep -F "origin" | grep -F "main" >/dev/null 2>&1; then
    pass "#3176 T34 — push_and_pr fetches origin/main pre-push"
else
    fail "#3176 T34 — push_and_pr fetches origin/main pre-push" \
         "git log: $(cat "$INVOCATIONS_DIR/git.log")"
fi

# git rebase origin/main was invoked.
if grep -F "rebase" "$INVOCATIONS_DIR/git.log" | grep -F "origin/main" >/dev/null 2>&1; then
    pass "#3176 T34 — push_and_pr rebases on origin/main pre-push"
else
    fail "#3176 T34 — push_and_pr rebases on origin/main pre-push" \
         "git log: $(cat "$INVOCATIONS_DIR/git.log")"
fi

# gh pr create was invoked with --title + --body-file, NOT --fill.
if grep -F "pr" "$INVOCATIONS_DIR/gh.log" | grep -F "create" | grep -F -- "--title" >/dev/null 2>&1; then
    pass "#3176 T34 — push_and_pr passes --title to gh pr create"
else
    fail "#3176 T34 — push_and_pr passes --title to gh pr create" \
         "gh log: $(cat "$INVOCATIONS_DIR/gh.log")"
fi

if grep -F "pr" "$INVOCATIONS_DIR/gh.log" | grep -F "create" | grep -F -- "--body-file" >/dev/null 2>&1; then
    pass "#3176 T34 — push_and_pr passes --body-file to gh pr create"
else
    fail "#3176 T34 — push_and_pr passes --body-file to gh pr create" \
         "gh log: $(cat "$INVOCATIONS_DIR/gh.log")"
fi

# pr_number parsed + UPDATEd on the agent row.
if printf '%s' "$t34_out" | grep -q "push_and_pr_pr_number_persisted"; then
    pass "#3176 T34 — push_and_pr persists pr_number on the agent row"
else
    fail "#3176 T34 — push_and_pr persists pr_number on the agent row" \
         "out tail: $(printf '%s' "$t34_out" | tail -c 500)"
fi

if grep -F "SET pr_number = 9999" "$INVOCATIONS_DIR/psql.log" >/dev/null 2>&1; then
    pass "#3176 T34 — psql UPDATE dispatcher.agents SET pr_number = 9999"
else
    fail "#3176 T34 — psql UPDATE dispatcher.agents SET pr_number = 9999" \
         "psql log sample: $(grep -m1 "SET pr_number" "$INVOCATIONS_DIR/psql.log" | head -c 200)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 35: push_and_pr rebase conflict → fail cleanly, no push attempted.
# ══════════════════════════════════════════════════════════════════════════
setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t35.txt"
printf 'push_and_pr\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t35_workspace="$TEST_TMP/t35-workspace"
mkdir -p "$t35_workspace"

# The git stub needs to return non-zero on rebase. The only way to
# condition the stub is via env var — add GIT_REBASE_EXIT.
set +e
t35_out=$(run_post_pr_phase "push_and_pr" "$t35_workspace" \
    "PHASE_FIXTURE_FILE=$PHASE_FIXTURE_FILE" \
    "PRIOR_PATCH_FIXTURE=" \
    "PR_NUMBER_FIXTURE=9999" \
    "GIT_REV_LIST_COUNT=1" \
    "GIT_REBASE_EXIT=1" \
    "CLAUDE_VERDICT_FIXTURE=")
set -e

if printf '%s' "$t35_out" | grep -q "push_and_pr_rebase_conflict"; then
    pass "#3176 T35 — rebase conflict emits push_and_pr_rebase_conflict"
else
    fail "#3176 T35 — rebase conflict emits push_and_pr_rebase_conflict" \
         "out tail: $(printf '%s' "$t35_out" | tail -c 500)"
fi

# No push attempted.
if grep -F "push" "$INVOCATIONS_DIR/git.log" | grep -F "origin" | grep -v "fetch" | grep -v "rebase" >/dev/null 2>&1; then
    fail "#3176 T35 — no push attempted on rebase conflict" \
         "git log: $(cat "$INVOCATIONS_DIR/git.log")"
else
    pass "#3176 T35 — no push attempted on rebase conflict"
fi

# git rebase --abort was invoked to restore the worktree.
if grep -F "rebase" "$INVOCATIONS_DIR/git.log" | grep -F "abort" >/dev/null 2>&1; then
    pass "#3176 T35 — rebase conflict triggers git rebase --abort"
else
    fail "#3176 T35 — rebase conflict triggers git rebase --abort" \
         "git log: $(cat "$INVOCATIONS_DIR/git.log")"
fi

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
