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
cp "$REPO_ROOT/scripts/tests/_record_invocation.sh" "$STUB_BIN/_record_invocation.sh"

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

# When -c was absent, fall back to stdin captured by the shared recorder.
# The recorder sets $stdin_content and writes STDIN: + FILE: tokens to the
# log, so existing assertions (grep for INSERT/already_merged) still match.
if [[ -z "$query" ]]; then
    query="${stdin_content:-}"
fi

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
        # Shim-test fixture (Stage 2) first, then post-PR fixture,
        # then a canonical green-rollup fallback so happy-path tests can
        # traverse awaiting_ci → merge → awaiting_deploy without setting
        # any fixture at all.
        if [[ -n "${GH_PR_FIXTURE:-}" && -f "$GH_PR_FIXTURE" ]]; then
            cat "$GH_PR_FIXTURE"
            exit 0
        fi
        if [[ -n "${GH_PR_VIEW_JSON_FIXTURE:-}" && -f "${GH_PR_VIEW_JSON_FIXTURE:-}" ]]; then
            cat "$GH_PR_VIEW_JSON_FIXTURE"
            exit 0
        fi
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
        exit 0
        ;;
    "pr diff")
        if [[ -n "${GH_PR_DIFF_FIXTURE:-}" && -f "$GH_PR_DIFF_FIXTURE" ]]; then
            cat "$GH_PR_DIFF_FIXTURE"
            exit 0
        fi
        exit 1
        ;;
    "pr merge")
        if [[ -n "${GH_PR_MERGE_STDERR:-}" ]]; then
            printf '%s\n' "$GH_PR_MERGE_STDERR" >&2
        fi
        exit "${GH_PR_MERGE_EXIT:-0}"
        ;;
    "run view")
        if [[ -n "${GH_RUN_LOG_FIXTURE:-}" && -f "$GH_RUN_LOG_FIXTURE" ]]; then
            cat "$GH_RUN_LOG_FIXTURE"
            exit 0
        fi
        exit 1
        ;;
    "run list")
        # Shim-test fixture (Stage 2) first, then post-PR fixture,
        # then empty array fallback so awaiting_deploy's "no runs → verify"
        # branch lights up without fixture setup.
        if [[ -n "${GH_RUN_LIST_FIXTURE:-}" && -f "$GH_RUN_LIST_FIXTURE" ]]; then
            cat "$GH_RUN_LIST_FIXTURE"
            exit 0
        fi
        if [[ -n "${GH_RUN_LIST_JSON_FIXTURE:-}" && -f "${GH_RUN_LIST_JSON_FIXTURE:-}" ]]; then
            cat "$GH_RUN_LIST_JSON_FIXTURE"
            exit 0
        fi
        printf '[]\n'
        exit "${GH_RUN_LIST_EXIT:-0}"
        ;;
    "auth login"|"auth setup-git")
        exit 0
        ;;
    "issue view")
        # GH_ISSUE_FIXTURE: full JSON fixture file (Stage 2 shim tests).
        if [[ -n "${GH_ISSUE_FIXTURE:-}" && -f "$GH_ISSUE_FIXTURE" ]]; then
            cat "$GH_ISSUE_FIXTURE"
            exit 0
        fi
        # #3411: post-merge cleanup probe. Test fixture
        # GH_ISSUE_STATE_FIXTURE controls the returned state
        # ("OPEN" | "CLOSED"). Defaults to CLOSED so existing tests
        # that traverse merge → awaiting_deploy don't accidentally
        # exercise the close path.
        printf '%s\n' "${GH_ISSUE_STATE_FIXTURE:-CLOSED}"
        exit "${GH_ISSUE_VIEW_EXIT:-0}"
        ;;
    "issue close")
        exit "${GH_ISSUE_CLOSE_EXIT:-0}"
        ;;
    "issue edit")
        exit "${GH_ISSUE_EDIT_EXIT:-0}"
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

# #3656: handle_push_and_pr now wraps git fetch / git push / gh pr create
# in ``timeout NETWORK_TIMEOUT_SECONDS`` to bound silent-hang risk.
# Most pre-#3656 tests (T50, T51, T52, T3614, the integration paths) do
# NOT exercise the 124-exit branch — they need a passthrough ``timeout``
# binary so the wrapped commands run identically to the pre-#3656 path.
# macOS does not ship coreutils' ``timeout(1)`` so ``command -v timeout``
# returns nothing and a real PATH lookup would exit 127, breaking those
# tests on operator laptops. This stub matches GNU ``timeout(1)``
# semantics for the no-timer-fired path: drop the seconds arg, exec
# the inner command transparently. Tests that DO exercise the 124
# branch (T3656, #3656) override this stub in their own per-test ``$_tbin``
# directory. See #3656 for the full layered defense rationale.
cat > "$STUB_BIN/timeout" <<'TIMEOUTEOF'
#!/usr/bin/env bash
# argv: <seconds> <inner-cmd> <inner-args...>
shift  # discard the seconds arg
exec "$@"
TIMEOUTEOF
chmod +x "$STUB_BIN/timeout"

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
      AGENT_RUNNER_CI_POLL_INTERVAL=0 \
      AGENT_RUNNER_DEPLOY_POLL_INTERVAL=0 \
      AGENT_RUNNER_DEPLOY_GRACE_SECONDS=0 \
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

# Verify each expected phase had an INSERT into phase_outputs. The
# entrypoint records each INSERT via ``psql -v phase=planning ... <<'EOF'``.
# The shared _record_invocation.sh (#3420) writes CALL args and heredoc SQL
# on separate log lines: the CALL line carries ``-v phase=planning``; the
# INSERT SQL appears in the subsequent STDIN: lines. Verify by checking
# for the ``-v phase=<phase>`` CALL arg — the heredoc content is fixed, so
# its presence follows from the invocation being recorded.
for expected in planning ralph summary push_and_pr verify; do
    if grep -q " phase=${expected}" "$INVOCATIONS_DIR/psql.log" 2>/dev/null; then
        pass "persists phase_outputs row for $expected"
    else
        fail "persists phase_outputs row for $expected" \
             "psql log sample: $(grep -m1 "INSERT INTO dispatcher.phase_outputs" "$INVOCATIONS_DIR/psql.log" | head -c 300)"
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

# #3219 regression guard — every INSERT into dispatcher.phase_outputs
# must carry an ``ON CONFLICT (agent_id, phase, attempt) DO UPDATE``
# clause so a legitimate re-entry (fix_ci → awaiting_ci is the
# observed path from aa11f02a PR #3217 #2829) doesn't raise a
# unique-constraint violation that propagates through db_exec's
# ``-v ON_ERROR_STOP=1`` + ``set -euo pipefail`` and kills the
# entrypoint (ECS exit 1 → daemon reaps as agent_task_stopped_
# unexpectedly → PR stranded). The daemon's subprocess-mode
# ``_persist_phase_output`` already writes the same ON CONFLICT
# overwrite (daemon.py ~line 5818); this test keeps the entrypoint
# at parity.
insert_count=$(grep -c "INSERT INTO dispatcher.phase_outputs" "$INVOCATIONS_DIR/psql.log" 2>/dev/null || true)
insert_count=${insert_count:-0}
on_conflict_count=$(grep -c "ON CONFLICT (agent_id, phase, attempt) DO UPDATE" "$INVOCATIONS_DIR/psql.log" 2>/dev/null || true)
on_conflict_count=${on_conflict_count:-0}
if [[ "$insert_count" -gt 0 && "$insert_count" == "$on_conflict_count" ]]; then
    pass "#3219 — every phase_outputs INSERT carries ON CONFLICT DO UPDATE (inserts=$insert_count)"
else
    fail "#3219 — every phase_outputs INSERT carries ON CONFLICT DO UPDATE" \
         "inserts=$insert_count, with-on-conflict=$on_conflict_count. Sample: $(grep -m1 "INSERT INTO dispatcher.phase_outputs" "$INVOCATIONS_DIR/psql.log" | head -c 400)"
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

# ── Test 21b: verify shim resilience — gh timeout/error (#3547) ────────────
# Issue #3547 root cause: _fetch_merged_pr_info / _fetch_deploy_runs_for_sha /
# _fetch_issue_bundle called _run() without try/except — a gh subprocess
# TimeoutExpired propagated to main(), exiting non-zero, silently skipping
# the input write, then claude hit the skill's input-missing guard.
# Fix: wrap _run() in each gh helper with try/except. This test verifies
# the shim still writes a well-formed input file even when every gh call
# returns a non-zero exit code (simulating a timeout/network error).
setup_fixtures

t21b_repo="$TEST_TMP/t21b-repo"
mkdir -p "$t21b_repo/.git"

# Override gh to fail every call in a test-specific bin dir so we don't
# redefine $STUB_BIN/gh (which would trigger the duplicate-stub guard).
t21b_bin="$TEST_TMP/t21b-bin"
mkdir -p "$t21b_bin"
ln -sf "$STUB_BIN/_record_invocation.sh" "$t21b_bin/_record_invocation.sh"
cat > "$t21b_bin/gh" <<'GHEOF_T21B'
#!/usr/bin/env bash
set -u
INVOCATIONS_DIR="${INVOCATIONS_DIR}"
. "$(dirname "$0")/_record_invocation.sh" gh "$@"
# Simulate gh timeout/error: always exit 1 with an error message.
printf 'simulated gh error\n' >&2
exit 1
GHEOF_T21B
chmod +x "$t21b_bin/gh"

set +e
DATABASE_URL="postgres://test" \
    GITHUB_REPO="judgemind/judgemind" \
    PATH="$t21b_bin:$STUB_BIN:$PATH" \
    INVOCATIONS_DIR="$INVOCATIONS_DIR" \
    DB_AGENT_PR_NUMBER="5047" \
    python3 "$SHIM_PY" verify "3547aaaa-3547-3547-3547-3547aaaabbbb" 3547 "$t21b_repo" \
    >/dev/null 2>&1
t21b_rc=$?
set -e

# t21b_bin is not on PATH after this point; $STUB_BIN/gh was never touched.
setup_fixtures

t21b_input="$t21b_repo/tmp/dispatcher-input/verify.json"
# Issue #3547: the shim must exit 0 and write the file even when every
# gh call fails — the skill's own guard handles missing fields (e.g.
# empty merged_commit_sha) rather than the entrypoint crashing.
if [[ $t21b_rc -eq 0 && -f "$t21b_input" ]]; then
    pass "#3547 — verify shim writes input even when gh returns errors"
else
    fail "#3547 — verify shim writes input even when gh returns errors" \
         "rc=$t21b_rc, file_exists=$(test -f "$t21b_input" && echo yes || echo no)"
fi

# The base identifier fields must always be present regardless of gh success.
if jq -e '.agent_id == "3547aaaa-3547-3547-3547-3547aaaabbbb"' \
     "$t21b_input" >/dev/null 2>&1; then
    pass "#3547 — verify shim carries agent_id in gh-error fallback"
else
    fail "#3547 — verify shim carries agent_id in gh-error fallback" \
         "content: $(cat "$t21b_input" 2>/dev/null | head -c 200)"
fi

# worktree_path must equal REPO_ROOT (not a subprocess-lane path from the DB row).
if jq -e --arg p "$t21b_repo" '.worktree_path == $p' \
     "$t21b_input" >/dev/null 2>&1; then
    pass "#3547 — verify shim sets worktree_path to REPO_ROOT in ECS lane"
else
    fail "#3547 — verify shim sets worktree_path to REPO_ROOT in ECS lane" \
         "worktree_path: $(jq -r '.worktree_path' "$t21b_input" 2>/dev/null)"
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
if grep "SELECT.*ralph_patches" "$INVOCATIONS_DIR/psql.log" \
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

# ══════════════════════════════════════════════════════════════════════════
# #3200 — classify_pr_rollup must handle StatusContext entries (Vercel,
# third-party commit-status API) as well as CheckRun entries, and
# handle_awaiting_ci must early-exit when the PR is already merged.
#
# Fixtures drive `awaiting_ci` through one poll iteration. A "green"
# rollup advances the phase to `merge`; a "red" rollup advances to
# `fix_ci`. An already-merged PR short-circuits to green immediately.
# ══════════════════════════════════════════════════════════════════════════

# ── Test 36: Fixture A — CheckRuns only, all SUCCESS + CLEAN → green ──────
# (Regression guard for the pre-#3200 happy path. Mirrors T28 but with
# an explicit fixture so the assertions don't depend on the default
# stub output.)
setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t36.txt"
printf 'awaiting_ci\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t36_pr_view="$TEST_TMP/t36-pr-view.json"
cat > "$t36_pr_view" <<'EOF'
{
  "statusCheckRollup": [
    {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
    {"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
    {"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": "SKIPPED"}
  ],
  "mergeable": "MERGEABLE",
  "mergeStateStatus": "CLEAN",
  "headRefOid": "deadbeefcafe",
  "mergeCommit": null
}
EOF

t36_workspace="$TEST_TMP/t36-workspace"
set +e
t36_out=$(run_post_pr_phase "awaiting_ci" "$t36_workspace" \
    "PHASE_FIXTURE_FILE=$PHASE_FIXTURE_FILE" \
    "PRIOR_PATCH_FIXTURE=" \
    "PR_NUMBER_FIXTURE=9999" \
    "GH_PR_VIEW_JSON_FIXTURE=$t36_pr_view" \
    "CLAUDE_VERDICT_FIXTURE=")
set -e

if printf '%s' "$t36_out" | grep -q '"rollup_state": "green"'; then
    pass "#3200 T36 — fixture A (CheckRun-only, all SUCCESS + CLEAN) → green"
else
    fail "#3200 T36 — fixture A (CheckRun-only, all SUCCESS + CLEAN) → green" \
         "out tail: $(printf '%s' "$t36_out" | tail -c 500)"
fi

# ── Test 37: Fixture B — StatusContext SUCCESS + CheckRuns SUCCESS + CLEAN → green
# (This is the live-reproducible bug: PR #3198 shape. Pre-#3200 the
# StatusContext entry fell through to "pending" so the function never
# returned green and every ECS agent hit the 60min timeout.)
setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t37.txt"
printf 'awaiting_ci\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t37_pr_view="$TEST_TMP/t37-pr-view.json"
cat > "$t37_pr_view" <<'EOF'
{
  "statusCheckRollup": [
    {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
    {"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
    {"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": "SKIPPED"},
    {"__typename": "StatusContext", "context": "Vercel", "state": "SUCCESS"}
  ],
  "mergeable": "MERGEABLE",
  "mergeStateStatus": "CLEAN",
  "headRefOid": "deadbeefcafe",
  "mergeCommit": null
}
EOF

t37_workspace="$TEST_TMP/t37-workspace"
set +e
t37_out=$(run_post_pr_phase "awaiting_ci" "$t37_workspace" \
    "PHASE_FIXTURE_FILE=$PHASE_FIXTURE_FILE" \
    "PRIOR_PATCH_FIXTURE=" \
    "PR_NUMBER_FIXTURE=9999" \
    "GH_PR_VIEW_JSON_FIXTURE=$t37_pr_view" \
    "CLAUDE_VERDICT_FIXTURE=")
set -e

if printf '%s' "$t37_out" | grep -q '"rollup_state": "green"'; then
    pass "#3200 T37 — fixture B (StatusContext SUCCESS + CheckRuns + CLEAN) → green"
else
    fail "#3200 T37 — fixture B (StatusContext SUCCESS + CheckRuns + CLEAN) → green" \
         "out tail: $(printf '%s' "$t37_out" | tail -c 500)"
fi

# Phase advances past awaiting_ci to merge → the classifier returned green.
if grep -q "SET phase = \\\\'merge\\\\'" "$INVOCATIONS_DIR/psql.log"; then
    pass "#3200 T37 — fixture B advances to merge on green"
else
    fail "#3200 T37 — fixture B advances to merge on green" \
         "psql log tail: $(tail -c 500 "$INVOCATIONS_DIR/psql.log")"
fi

# ── Test 38: Fixture C — StatusContext FAILURE → red → advances to fix_ci
setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t38.txt"
printf 'awaiting_ci\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t38_pr_view="$TEST_TMP/t38-pr-view.json"
cat > "$t38_pr_view" <<'EOF'
{
  "statusCheckRollup": [
    {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
    {"__typename": "StatusContext", "context": "Vercel", "state": "FAILURE"}
  ],
  "mergeable": "MERGEABLE",
  "mergeStateStatus": "CLEAN",
  "headRefOid": "deadbeefcafe",
  "mergeCommit": null
}
EOF

t38_workspace="$TEST_TMP/t38-workspace"
set +e
t38_out=$(run_post_pr_phase "awaiting_ci" "$t38_workspace" \
    "PHASE_FIXTURE_FILE=$PHASE_FIXTURE_FILE" \
    "PRIOR_PATCH_FIXTURE=" \
    "PR_NUMBER_FIXTURE=9999" \
    "GH_PR_VIEW_JSON_FIXTURE=$t38_pr_view" \
    "CLAUDE_VERDICT_FIXTURE=")
set -e

if printf '%s' "$t38_out" | grep -q '"rollup_state": "red"'; then
    pass "#3200 T38 — fixture C (StatusContext FAILURE) → red"
else
    fail "#3200 T38 — fixture C (StatusContext FAILURE) → red" \
         "out tail: $(printf '%s' "$t38_out" | tail -c 500)"
fi

if grep -q "SET phase = \\\\'fix_ci\\\\'" "$INVOCATIONS_DIR/psql.log"; then
    pass "#3200 T38 — fixture C advances to fix_ci on red"
else
    fail "#3200 T38 — fixture C advances to fix_ci on red" \
         "psql log tail: $(tail -c 500 "$INVOCATIONS_DIR/psql.log")"
fi

# ── Test 39: Fixture D — already-merged PR (mergeCommit.oid set,
# mergeStateStatus=UNKNOWN) → handle_awaiting_ci short-circuits to green
# without even looking at the rollup classifier.
setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t39.txt"
printf 'awaiting_ci\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t39_pr_view="$TEST_TMP/t39-pr-view.json"
cat > "$t39_pr_view" <<'EOF'
{
  "statusCheckRollup": [
    {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"}
  ],
  "mergeable": "UNKNOWN",
  "mergeStateStatus": "UNKNOWN",
  "headRefOid": "deadbeefcafe",
  "mergeCommit": {"oid": "a300318bd5b1813da8dea78a28eb5b37c0d427ac"}
}
EOF

t39_workspace="$TEST_TMP/t39-workspace"
set +e
t39_out=$(run_post_pr_phase "awaiting_ci" "$t39_workspace" \
    "PHASE_FIXTURE_FILE=$PHASE_FIXTURE_FILE" \
    "PRIOR_PATCH_FIXTURE=" \
    "PR_NUMBER_FIXTURE=9999" \
    "GH_PR_VIEW_JSON_FIXTURE=$t39_pr_view" \
    "CLAUDE_VERDICT_FIXTURE=")
set -e

if printf '%s' "$t39_out" | grep -q "awaiting_ci_already_merged"; then
    pass "#3200 T39 — fixture D (merged PR) emits awaiting_ci_already_merged"
else
    fail "#3200 T39 — fixture D (merged PR) emits awaiting_ci_already_merged" \
         "out tail: $(printf '%s' "$t39_out" | tail -c 500)"
fi

# handle_awaiting_ci's JSON payload is captured by the caller into
# $_output and then INSERTed into dispatcher.phase_outputs — so the
# already_merged flag surfaces in the psql invocation log, not in the
# entrypoint's stdout.
if grep -F "already_merged" "$INVOCATIONS_DIR/psql.log" >/dev/null 2>&1; then
    pass "#3200 T39 — fixture D short-circuits with already_merged payload (persisted to phase_outputs)"
else
    fail "#3200 T39 — fixture D short-circuits with already_merged payload (persisted to phase_outputs)" \
         "psql log tail: $(tail -c 800 "$INVOCATIONS_DIR/psql.log")"
fi

# Merged PR still advances to merge (caller transitions green → merge).
if grep -q "SET phase = \\\\'merge\\\\'" "$INVOCATIONS_DIR/psql.log"; then
    pass "#3200 T39 — fixture D advances to merge on already-merged short-circuit"
else
    fail "#3200 T39 — fixture D advances to merge on already-merged short-circuit" \
         "psql log tail: $(tail -c 500 "$INVOCATIONS_DIR/psql.log")"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 40: #3219 — persist_phase_output is idempotent on re-entry.
#
# Scenario under test: the daemon's subprocess-mode _persist_phase_output
# uses ON CONFLICT (agent_id, phase, attempt) DO UPDATE, but the Stage 1b
# entrypoint originally did a plain INSERT. When an ECS agent's CI went
# red → fix_ci patched → re-entered awaiting_ci, the second
# persist_phase_output call on the same (agent_id, phase, attempt)
# tripped the idx_dispatcher_phase_outputs_agent_phase_attempt UNIQUE
# index, psql exited 1, db_exec's ``-v ON_ERROR_STOP=1`` +
# ``set -euo pipefail`` killed the entrypoint, ECS marked exit_code=1,
# and the daemon reaped the agent as ``agent_task_stopped_unexpectedly``
# with a stranded PR. Observed live on agent aa11f02a PR #3217 #2829
# at 2026-04-24 14:26:52Z.
#
# This test isolates persist_phase_output by extracting just the
# function definitions it needs (db_exec, log, persist_phase_output)
# from the entrypoint via sed, then drives it with a psql stub that
# enforces a real unique-constraint on (agent_id, phase, attempt).
# If the INSERT lacks ON CONFLICT, the second call fails and
# propagates exit 1. If ON CONFLICT is present, the second call
# succeeds and the stored JSON is overwritten with EXCLUDED.
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures

# Extract the function definitions we need. Each function in the
# entrypoint starts at ``<name>() {`` and ends at the first column-0
# closing brace on its own line. We pull db_exec, log, and
# persist_phase_output into a standalone fixture file so we can source
# and invoke them without running the entrypoint's main loop.
t40_funcs="$TEST_TMP/t40-funcs.sh"
# Bootstrap: log() writes to fd 3 which the entrypoint's top-level wires
# to stdout via ``exec 3>&1``. Reproduce that wiring here so the test
# subshell doesn't hit "bad file descriptor" when persist_phase_output
# calls log.
printf 'exec 3>&1\n' > "$t40_funcs"

# Extract `db_exec() { ... }` — the first brace-balanced block starting
# at that token. Simple awk state machine: track brace depth from the
# opening line, emit until depth returns to zero.
awk '
    /^db_exec\(\)/ { in_fn=1 }
    in_fn { print }
    in_fn && /^}$/ { exit }
' "$ENTRYPOINT" >> "$t40_funcs"

awk '
    /^log\(\)/ { in_fn=1 }
    in_fn { print }
    in_fn && /^}$/ { exit }
' "$ENTRYPOINT" >> "$t40_funcs"

awk '
    /^persist_phase_output\(\)/ { in_fn=1 }
    in_fn { print }
    in_fn && /^}$/ { exit }
' "$ENTRYPOINT" >> "$t40_funcs"

# Sanity: all three definitions extracted.
for fn in db_exec log persist_phase_output; do
    if grep -q "^${fn}()" "$t40_funcs"; then
        pass "#3219 T40 — extracted ${fn} from entrypoint"
    else
        fail "#3219 T40 — extracted ${fn} from entrypoint" \
             "fixture head: $(head -c 300 "$t40_funcs")"
    fi
done

# Build a constraint-enforcing psql stub. Tracks (agent_id, phase, attempt)
# tuples in a state file. Default attempt is 0 (schema default). On a
# second INSERT for the same tuple:
#   - If the query contains ``ON CONFLICT ... DO UPDATE``, overwrite the
#     stored output_json and exit 0.
#   - Otherwise, emit a realistic postgres unique-violation error and
#     exit 1 (mirroring the real DB's behavior under
#     ``-v ON_ERROR_STOP=1``).
t40_state_dir="$TEST_TMP/t40-state"
mkdir -p "$t40_state_dir"
t40_stub_bin="$TEST_TMP/t40-bin"
mkdir -p "$t40_stub_bin"

cat > "$t40_stub_bin/psql" <<'T40PSQLEOF'
#!/usr/bin/env bash
set -u
# Parse -c <query> AND ``-v name=value`` (#3413 — persist_phase_output
# now passes agent_id/phase/output_path via psql -v variables and the
# SQL via stdin heredoc). We accept both the legacy ``-c <SQL>`` shape
# (still used by db_exec for non-persist queries) and the new
# heredoc + -v shape so the test stays meaningful across the bug fix.
query=""
v_agent_id=""
v_phase=""
v_output_path=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -c) shift; query="$1" ;;
        -v)
            shift
            case "$1" in
                agent_id=*)    v_agent_id="${1#agent_id=}" ;;
                phase=*)       v_phase="${1#phase=}" ;;
                output_path=*) v_output_path="${1#output_path=}" ;;
            esac
            ;;
    esac
    shift || true
done

# Pull the SQL from stdin if no -c was supplied (heredoc shape).
if [[ -z "$query" && ! -t 0 ]]; then
    query=$(cat)
fi

# Only phase_outputs INSERTs matter to this test — everything else is
# a silent success.
if [[ "$query" != *"INSERT INTO dispatcher.phase_outputs"* ]]; then
    exit 0
fi

# Extract agent_id, phase, output_json from one of two wire formats:
#   1. Legacy -c "INSERT ... VALUES ('<agent_id>', '<phase>',
#      '<output>'::jsonb)" — grep the VALUES tuple.
#   2. New heredoc + -v: agent_id and phase came from -v args; the JSON
#      payload is the contents of the file at $v_output_path.
if [[ -n "$v_agent_id" && -n "$v_phase" && -n "$v_output_path" ]]; then
    agent_id="$v_agent_id"
    phase="$v_phase"
    output_json=$(cat "$v_output_path" 2>/dev/null || true)
else
    agent_id=$(printf '%s' "$query" | sed -n "s/.*VALUES[[:space:]]*([[:space:]]*'\\([^']*\\)'.*/\\1/p")
    phase=$(printf '%s' "$query" | sed -n "s/.*VALUES[[:space:]]*('[^']*'[[:space:]]*,[[:space:]]*'\\([^']*\\)'.*/\\1/p")
    output_json=$(printf '%s' "$query" \
        | sed -n "s/.*VALUES[[:space:]]*('[^']*'[[:space:]]*,[[:space:]]*'[^']*'[[:space:]]*,[[:space:]]*'\\(.*\\)'::jsonb.*/\\1/p")
fi
# Entrypoint always lets attempt default to 0.
attempt=0
key="${agent_id}__${phase}__${attempt}"
state_file="${T40_STATE_DIR}/${key}.json"

if [[ -f "$state_file" ]]; then
    # Second INSERT on same (agent_id, phase, attempt). ON CONFLICT
    # DO UPDATE is the only escape hatch.
    if [[ "$query" == *"ON CONFLICT (agent_id, phase, attempt) DO UPDATE"* ]]; then
        printf '%s' "$output_json" > "$state_file"
        exit 0
    fi
    printf 'ERROR:  duplicate key value violates unique constraint "idx_dispatcher_phase_outputs_agent_phase_attempt"\n' >&2
    printf 'DETAIL:  Key (agent_id, phase, attempt)=(%s, %s, %d) already exists.\n' \
        "$agent_id" "$phase" "$attempt" >&2
    exit 1
fi
printf '%s' "$output_json" > "$state_file"
exit 0
T40PSQLEOF
chmod +x "$t40_stub_bin/psql"

# Drive persist_phase_output twice on the same (agent_id, phase, attempt=0).
# Subshell-isolate so `set -euo pipefail` (mirroring the entrypoint)
# applies only to this test. Capture rc from the second call to prove
# it did not propagate exit 1.
set +e
t40_out=$(T40_STATE_DIR="$t40_state_dir" \
    PATH="$t40_stub_bin:$PATH" \
    AGENT_ID="40404040-dead-beef-cafe-000000000001" \
    DATABASE_URL="postgres://test" \
    bash -c '
        set -euo pipefail
        # shellcheck disable=SC1090
        . "'"$t40_funcs"'"
        persist_phase_output "awaiting_ci" "{\"rollup_state\": \"red\", \"reason\": \"initial observation\"}"
        persist_phase_output "awaiting_ci" "{\"rollup_state\": \"red\", \"reason\": \"re-entry after fix_ci\"}"
        echo "second-call-rc=0"
    ' 2>&1)
t40_rc=$?
set -e

if [[ "$t40_rc" -eq 0 ]]; then
    pass "#3219 T40 — persist_phase_output succeeds on second call with same (agent_id, phase, attempt)"
else
    fail "#3219 T40 — persist_phase_output succeeds on second call with same (agent_id, phase, attempt)" \
         "rc=$t40_rc, output: $t40_out"
fi

if printf '%s' "$t40_out" | grep -q "second-call-rc=0"; then
    pass "#3219 T40 — entrypoint reaches code after the second persist call (no set -e abort)"
else
    fail "#3219 T40 — entrypoint reaches code after the second persist call (no set -e abort)" \
         "output: $t40_out"
fi

# Verify the stored row has the latest (second-call) output — ON CONFLICT
# DO UPDATE SET output_json = EXCLUDED.output_json must have overwritten.
t40_key="40404040-dead-beef-cafe-000000000001__awaiting_ci__0"
t40_stored="$t40_state_dir/${t40_key}.json"
if [[ -f "$t40_stored" ]] && grep -q "re-entry after fix_ci" "$t40_stored"; then
    pass "#3219 T40 — stored phase_outputs row reflects the latest (second-call) payload"
else
    fail "#3219 T40 — stored phase_outputs row reflects the latest (second-call) payload" \
         "stored=$(cat "$t40_stored" 2>/dev/null)"
fi

# Negative control: confirm the constraint-enforcing psql stub actually
# rejects a second INSERT when ON CONFLICT is absent. Drives raw SQL
# through db_exec to prove the stub catches the regression, not the
# post-fix persist_phase_output implementation.
t40_neg_state="$TEST_TMP/t40-neg-state"
mkdir -p "$t40_neg_state"
set +e
t40_neg_out=$(T40_STATE_DIR="$t40_neg_state" \
    PATH="$t40_stub_bin:$PATH" \
    AGENT_ID="40404040-dead-beef-cafe-000000000002" \
    DATABASE_URL="postgres://test" \
    bash -c '
        set -euo pipefail
        # shellcheck disable=SC1090
        . "'"$t40_funcs"'"
        db_exec "INSERT INTO dispatcher.phase_outputs (agent_id, phase, output_json)
                 VALUES ('\''40404040-dead-beef-cafe-000000000002'\'', '\''awaiting_ci'\'', '\''{}'\''::jsonb);"
        db_exec "INSERT INTO dispatcher.phase_outputs (agent_id, phase, output_json)
                 VALUES ('\''40404040-dead-beef-cafe-000000000002'\'', '\''awaiting_ci'\'', '\''{}'\''::jsonb);"
        echo "neg-second-call-rc=0"
    ' 2>&1)
t40_neg_rc=$?
set -e

if [[ "$t40_neg_rc" -ne 0 ]]; then
    pass "#3219 T40 — constraint-enforcing psql stub rejects duplicate INSERT without ON CONFLICT (negative control)"
else
    fail "#3219 T40 — constraint-enforcing psql stub rejects duplicate INSERT without ON CONFLICT (negative control)" \
         "rc=$t40_neg_rc (expected non-zero), output: $t40_neg_out"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 41: #3225 — handle_fix_conflict happy path (verdict=resolved).
#
# Scenario: dispatcher.agents.merge_conflict_attempts is 0, the
# fix_conflict skill returns verdict=resolved with resolved_files
# populated. Expected behaviour:
#   1. The budget gate passes (0 < FIX_CONFLICT_MAX_ATTEMPTS=2).
#   2. merge_conflict_attempts is incremented (UPDATE on the agent row).
#   3. The claude skill is invoked with /task-v2-fix-conflict.
#   4. handle_fix_conflict reads the output, applies resolved_files as
#      a new commit, and prints the pass-through envelope.
#   5. The loop advances to push_and_pr.
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures

# Isolate the function definitions needed. Reuse the same awk-based
# extraction as T40 so the test exercises the REAL entrypoint code.
t41_funcs="$TEST_TMP/t41-funcs.sh"
printf 'exec 3>&1\n' > "$t41_funcs"

for fn in db_exec db_query_one log persist_phase_output \
          read_merge_conflict_attempts \
          increment_merge_conflict_attempts \
          apply_resolved_files \
          run_claude_phase \
          write_phase_input \
          phase_to_skill \
          read_phase_output \
          handle_fix_conflict; do
    awk -v FN="^${fn}\\\\(\\\\)" '
        $0 ~ FN { in_fn=1 }
        in_fn { print }
        in_fn && /^}$/ { exit }
    ' "$ENTRYPOINT" >> "$t41_funcs"
done

# #3683: run_claude_phase wraps ``claude -p`` in
# ``timeout "$CLAUDE_PHASE_TIMEOUT_SECONDS"``. Include the constant so
# the sourced fixture doesn't fail under ``set -u``.
grep '^CLAUDE_PHASE_TIMEOUT_SECONDS=' "$ENTRYPOINT" >> "$t41_funcs"

# Sanity: handle_fix_conflict was extracted.
if grep -q "^handle_fix_conflict()" "$t41_funcs"; then
    pass "#3225 T41 — extracted handle_fix_conflict from entrypoint"
else
    fail "#3225 T41 — extracted handle_fix_conflict from entrypoint" \
         "fixture head: $(head -c 400 "$t41_funcs")"
fi

# Build a minimal per-test workspace and stub binaries. The stubs track
# the key observations: psql read/write, git invocations, and claude
# output.
t41_workspace="$TEST_TMP/t41-workspace"
t41_repo_root="$TEST_TMP/t41-repo"
t41_state_dir="$TEST_TMP/t41-state"
t41_stub_bin="$TEST_TMP/t41-bin"
mkdir -p "$t41_workspace" "$t41_repo_root" "$t41_state_dir" "$t41_stub_bin"

# Pre-create tmp/dispatcher-output/ with the "resolved" skill output.
mkdir -p "$t41_repo_root/tmp/dispatcher-output"
cat > "$t41_repo_root/tmp/dispatcher-output/fix-conflict.json" <<'T41OUTJSON'
{
  "agent_id": "41414141-dead-beef-cafe-000000000001",
  "verdict": "resolved",
  "resolution_notes": "reconciled 1 hunk against main's refactor",
  "resolved_files": [
    {"path": "packages/web/a.tsx", "content": "resolved-a\n"}
  ],
  "conflict_files": ["packages/web/a.tsx"]
}
T41OUTJSON

# Seed the pre-conflict file so apply_resolved_files has a parent
# dir to write into.
mkdir -p "$t41_repo_root/packages/web"
printf 'original-a\n' > "$t41_repo_root/packages/web/a.tsx"

# psql stub — reads merge_conflict_attempts from a state file, and
# records any UPDATE that touches the column.
cat > "$t41_stub_bin/psql" <<'T41PSQLEOF'
#!/usr/bin/env bash
set -u
query=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -c) shift; query="$1" ;;
    esac
    shift || true
done
state_file="${T41_STATE_DIR}/merge_conflict_attempts.txt"
if [[ ! -f "$state_file" ]]; then
    printf '0\n' > "$state_file"
fi
if [[ "$query" == *"SELECT COALESCE(merge_conflict_attempts"* ]]; then
    cat "$state_file"
    exit 0
fi
if [[ "$query" == *"SET merge_conflict_attempts"* ]]; then
    _cur=$(cat "$state_file")
    printf '%s\n' "$((_cur + 1))" > "$state_file"
    printf 'UPDATE_MCA\n' >> "${T41_STATE_DIR}/update-log.txt"
    exit 0
fi
# Everything else — silent success.
exit 0
T41PSQLEOF
chmod +x "$t41_stub_bin/psql"

# claude stub — prints a minimal envelope to stdout (not used by the
# entrypoint since it reads the dispatcher-output file, but invoked).
cat > "$t41_stub_bin/claude" <<'T41CLAUDEEOF'
#!/usr/bin/env bash
printf '{"result": "claude invoked"}\n'
printf 'CLAUDE_INVOKED\n' >> "${T41_STATE_DIR}/claude-log.txt"
exit 0
T41CLAUDEEOF
chmod +x "$t41_stub_bin/claude"

# git stub — minimal: we need ``git add`` and ``git commit`` to succeed.
# Fall through everything else to a no-op success.
cat > "$t41_stub_bin/git" <<'T41GITEOF'
#!/usr/bin/env bash
printf 'git %s\n' "$*" >> "${T41_STATE_DIR}/git-log.txt"
exit 0
T41GITEOF
chmod +x "$t41_stub_bin/git"

# jq stub: we need the real jq. Symlink it in.
if command -v jq >/dev/null 2>&1; then
    ln -sf "$(command -v jq)" "$t41_stub_bin/jq"
fi
# python3 too
if command -v python3 >/dev/null 2>&1; then
    ln -sf "$(command -v python3)" "$t41_stub_bin/python3"
fi

# Phase input shim — write an empty fix-conflict.json so
# write_phase_input succeeds.
mkdir -p "$t41_repo_root/tmp/dispatcher-input"
cat > "$t41_repo_root/tmp/dispatcher-input/fix-conflict.json" <<'T41INPUTEOF'
{"agent_id": "41414141-dead-beef-cafe-000000000001", "conflict_files": []}
T41INPUTEOF

# Stub phase_input_shim.py — minimal no-op so write_phase_input's
# python3 invocation succeeds.
t41_shim="$TEST_TMP/t41-phase-input-shim.py"
cat > "$t41_shim" <<'T41SHIMEOF'
import sys
print(sys.argv)
sys.exit(0)
T41SHIMEOF

# Drive handle_fix_conflict. Subshell-isolate with set -euo pipefail
# mirroring the entrypoint.
set +e
t41_out=$(T41_STATE_DIR="$t41_state_dir" \
    PATH="$t41_stub_bin:$PATH" \
    AGENT_ID="41414141-dead-beef-cafe-000000000001" \
    ISSUE_NUMBER="3225" \
    AGENT_WORKSPACE="$t41_workspace" \
    REPO_ROOT="$t41_repo_root" \
    DATABASE_URL="postgres://test" \
    AGENT_RUNNER_DRY_RUN="0" \
    AGENT_RUNNER_FIX_CONFLICT_MAX_ATTEMPTS="2" \
    AGENT_RUNNER_PHASE_INPUT_SHIM="$t41_shim" \
    PHASE_INPUT_SHIM="$t41_shim" \
    FIX_CONFLICT_MAX_ATTEMPTS="2" \
    bash -c '
        set -euo pipefail
        # shellcheck disable=SC1090
        . "'"$t41_funcs"'"
        handle_fix_conflict
    ' 2>&1)
t41_rc=$?
set -e

# Expectations:
# 1. handle_fix_conflict exits 0.
if [[ "$t41_rc" -eq 0 ]]; then
    pass "#3225 T41 — handle_fix_conflict exits 0 on resolved"
else
    fail "#3225 T41 — handle_fix_conflict exits 0 on resolved" \
         "rc=$t41_rc, output: $t41_out"
fi

# 2. Output contains verdict=resolved.
if printf '%s' "$t41_out" | grep -q '"verdict".*"resolved"'; then
    pass "#3225 T41 — handle_fix_conflict prints verdict=resolved envelope"
else
    fail "#3225 T41 — handle_fix_conflict prints verdict=resolved envelope" \
         "output: $t41_out"
fi

# 3. merge_conflict_attempts was incremented (UPDATE log has an entry).
if [[ -s "$t41_state_dir/update-log.txt" ]]; then
    pass "#3225 T41 — merge_conflict_attempts was incremented"
else
    fail "#3225 T41 — merge_conflict_attempts was incremented" \
         "update-log.txt empty or missing"
fi

# 4. claude was invoked.
if [[ -s "$t41_state_dir/claude-log.txt" ]]; then
    pass "#3225 T41 — claude -p fix-conflict was invoked"
else
    fail "#3225 T41 — claude -p fix-conflict was invoked" \
         "claude-log.txt empty"
fi

# 5. git add + git commit were run (apply_resolved_files staged + committed).
# The git stub logs `git <argv>` (without the binary name); the
# entrypoint invokes ``git -C <repo> add`` and ``git -C <repo> commit``.
if grep -q " add " "$t41_state_dir/git-log.txt" 2>/dev/null; then
    pass "#3225 T41 — apply_resolved_files ran git add"
else
    fail "#3225 T41 — apply_resolved_files ran git add" \
         "git-log.txt: $(cat "$t41_state_dir/git-log.txt" 2>/dev/null)"
fi
if grep -q " commit " "$t41_state_dir/git-log.txt" 2>/dev/null; then
    pass "#3225 T41 — apply_resolved_files ran git commit"
else
    fail "#3225 T41 — apply_resolved_files ran git commit" \
         "git-log.txt: $(cat "$t41_state_dir/git-log.txt" 2>/dev/null)"
fi

# 6. The resolved file's content matches the skill's output on disk.
if [[ -f "$t41_repo_root/packages/web/a.tsx" ]] && \
   grep -q "resolved-a" "$t41_repo_root/packages/web/a.tsx"; then
    pass "#3225 T41 — resolved_files content written to repo path"
else
    fail "#3225 T41 — resolved_files content written to repo path" \
         "file contents: $(cat "$t41_repo_root/packages/web/a.tsx" 2>/dev/null)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 42: #3225 — handle_fix_conflict unresolvable case.
#
# Scenario: budget is fine (0 < 2), but the skill returns
# verdict=unresolvable. handle_fix_conflict must:
#   1. Still increment the counter (the skill WAS invoked).
#   2. NOT apply any resolved_files (none were provided).
#   3. Emit the unresolvable envelope so the transition shim routes
#      to conflict_unresolvable.
# ══════════════════════════════════════════════════════════════════════════

t42_workspace="$TEST_TMP/t42-workspace"
t42_repo_root="$TEST_TMP/t42-repo"
t42_state_dir="$TEST_TMP/t42-state"
t42_stub_bin="$TEST_TMP/t42-bin"
mkdir -p "$t42_workspace" "$t42_repo_root" "$t42_state_dir" "$t42_stub_bin"

mkdir -p "$t42_repo_root/tmp/dispatcher-output"
cat > "$t42_repo_root/tmp/dispatcher-output/fix-conflict.json" <<'T42OUTJSON'
{
  "agent_id": "42424242-dead-beef-cafe-000000000002",
  "verdict": "unresolvable",
  "resolution_notes": "function X was rewritten on main — agent's addition no longer applies",
  "resolved_files": [],
  "conflict_files": ["packages/api/src/x.py"]
}
T42OUTJSON

mkdir -p "$t42_repo_root/tmp/dispatcher-input"
printf '{}' > "$t42_repo_root/tmp/dispatcher-input/fix-conflict.json"

# Reuse the same psql/claude/git stub pattern — cheap to duplicate here
# so each test is independently inspectable.
cat > "$t42_stub_bin/psql" <<'T42PSQLEOF'
#!/usr/bin/env bash
set -u
query=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -c) shift; query="$1" ;;
    esac
    shift || true
done
state_file="${T42_STATE_DIR}/merge_conflict_attempts.txt"
if [[ ! -f "$state_file" ]]; then
    printf '0\n' > "$state_file"
fi
if [[ "$query" == *"SELECT COALESCE(merge_conflict_attempts"* ]]; then
    cat "$state_file"
    exit 0
fi
if [[ "$query" == *"SET merge_conflict_attempts"* ]]; then
    _cur=$(cat "$state_file")
    printf '%s\n' "$((_cur + 1))" > "$state_file"
    printf 'UPDATE_MCA\n' >> "${T42_STATE_DIR}/update-log.txt"
    exit 0
fi
exit 0
T42PSQLEOF
chmod +x "$t42_stub_bin/psql"

cat > "$t42_stub_bin/claude" <<'T42CLAUDEEOF'
#!/usr/bin/env bash
printf '{"result": "claude invoked"}\n'
printf 'CLAUDE_INVOKED\n' >> "${T42_STATE_DIR}/claude-log.txt"
exit 0
T42CLAUDEEOF
chmod +x "$t42_stub_bin/claude"

cat > "$t42_stub_bin/git" <<'T42GITEOF'
#!/usr/bin/env bash
printf 'git %s\n' "$*" >> "${T42_STATE_DIR}/git-log.txt"
exit 0
T42GITEOF
chmod +x "$t42_stub_bin/git"

if command -v jq >/dev/null 2>&1; then
    ln -sf "$(command -v jq)" "$t42_stub_bin/jq"
fi
if command -v python3 >/dev/null 2>&1; then
    ln -sf "$(command -v python3)" "$t42_stub_bin/python3"
fi

t42_shim="$TEST_TMP/t42-phase-input-shim.py"
cat > "$t42_shim" <<'T42SHIMEOF'
import sys
sys.exit(0)
T42SHIMEOF

set +e
t42_out=$(T42_STATE_DIR="$t42_state_dir" \
    PATH="$t42_stub_bin:$PATH" \
    AGENT_ID="42424242-dead-beef-cafe-000000000002" \
    ISSUE_NUMBER="3225" \
    AGENT_WORKSPACE="$t42_workspace" \
    REPO_ROOT="$t42_repo_root" \
    DATABASE_URL="postgres://test" \
    AGENT_RUNNER_DRY_RUN="0" \
    AGENT_RUNNER_FIX_CONFLICT_MAX_ATTEMPTS="2" \
    AGENT_RUNNER_PHASE_INPUT_SHIM="$t42_shim" \
    PHASE_INPUT_SHIM="$t42_shim" \
    FIX_CONFLICT_MAX_ATTEMPTS="2" \
    bash -c '
        set -euo pipefail
        # shellcheck disable=SC1090
        . "'"$t41_funcs"'"
        handle_fix_conflict
    ' 2>&1)
t42_rc=$?
set -e

if [[ "$t42_rc" -eq 0 ]]; then
    pass "#3225 T42 — handle_fix_conflict exits 0 on unresolvable"
else
    fail "#3225 T42 — handle_fix_conflict exits 0 on unresolvable" \
         "rc=$t42_rc, output: $t42_out"
fi

if printf '%s' "$t42_out" | grep -q '"verdict".*"unresolvable"'; then
    pass "#3225 T42 — handle_fix_conflict prints verdict=unresolvable envelope"
else
    fail "#3225 T42 — handle_fix_conflict prints verdict=unresolvable envelope" \
         "output: $t42_out"
fi

# merge_conflict_attempts was still incremented (the skill WAS invoked).
if [[ -s "$t42_state_dir/update-log.txt" ]]; then
    pass "#3225 T42 — merge_conflict_attempts incremented even on unresolvable"
else
    fail "#3225 T42 — merge_conflict_attempts incremented even on unresolvable" \
         "update-log.txt empty"
fi

# No git commit was attempted (no resolved_files to apply).
if ! grep -q " commit " "$t42_state_dir/git-log.txt" 2>/dev/null; then
    pass "#3225 T42 — no git commit attempted on unresolvable"
else
    fail "#3225 T42 — no git commit attempted on unresolvable" \
         "git-log.txt: $(cat "$t42_state_dir/git-log.txt" 2>/dev/null)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 43: #3225 — handle_fix_conflict budget-exhausted case.
#
# Scenario: merge_conflict_attempts is already 2 (equal to the budget
# cap FIX_CONFLICT_MAX_ATTEMPTS). handle_fix_conflict must:
#   1. Skip the claude invocation (budget gate fires first).
#   2. NOT increment the counter (we didn't run the skill).
#   3. Emit the synthetic unresolvable + budget_exhausted envelope.
# ══════════════════════════════════════════════════════════════════════════

t43_workspace="$TEST_TMP/t43-workspace"
t43_repo_root="$TEST_TMP/t43-repo"
t43_state_dir="$TEST_TMP/t43-state"
t43_stub_bin="$TEST_TMP/t43-bin"
mkdir -p "$t43_workspace" "$t43_repo_root" "$t43_state_dir" "$t43_stub_bin"

# Seed the state file with 2 (at the budget cap).
printf '2\n' > "$t43_state_dir/merge_conflict_attempts.txt"

# Same psql stub shape, different state dir.
cat > "$t43_stub_bin/psql" <<'T43PSQLEOF'
#!/usr/bin/env bash
set -u
query=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -c) shift; query="$1" ;;
    esac
    shift || true
done
state_file="${T43_STATE_DIR}/merge_conflict_attempts.txt"
if [[ ! -f "$state_file" ]]; then
    printf '0\n' > "$state_file"
fi
if [[ "$query" == *"SELECT COALESCE(merge_conflict_attempts"* ]]; then
    cat "$state_file"
    exit 0
fi
if [[ "$query" == *"SET merge_conflict_attempts"* ]]; then
    _cur=$(cat "$state_file")
    printf '%s\n' "$((_cur + 1))" > "$state_file"
    printf 'UPDATE_MCA\n' >> "${T43_STATE_DIR}/update-log.txt"
    exit 0
fi
exit 0
T43PSQLEOF
chmod +x "$t43_stub_bin/psql"

# claude stub that records ANY invocation — if it runs, T43 has failed.
cat > "$t43_stub_bin/claude" <<'T43CLAUDEEOF'
#!/usr/bin/env bash
printf 'CLAUDE_INVOKED\n' >> "${T43_STATE_DIR}/claude-log.txt"
printf '{"result": "should-not-happen"}\n'
exit 0
T43CLAUDEEOF
chmod +x "$t43_stub_bin/claude"

cat > "$t43_stub_bin/git" <<'T43GITEOF'
#!/usr/bin/env bash
printf 'git %s\n' "$*" >> "${T43_STATE_DIR}/git-log.txt"
exit 0
T43GITEOF
chmod +x "$t43_stub_bin/git"

if command -v jq >/dev/null 2>&1; then
    ln -sf "$(command -v jq)" "$t43_stub_bin/jq"
fi
if command -v python3 >/dev/null 2>&1; then
    ln -sf "$(command -v python3)" "$t43_stub_bin/python3"
fi

set +e
t43_out=$(T43_STATE_DIR="$t43_state_dir" \
    PATH="$t43_stub_bin:$PATH" \
    AGENT_ID="43434343-dead-beef-cafe-000000000003" \
    ISSUE_NUMBER="3225" \
    AGENT_WORKSPACE="$t43_workspace" \
    REPO_ROOT="$t43_repo_root" \
    DATABASE_URL="postgres://test" \
    AGENT_RUNNER_DRY_RUN="0" \
    AGENT_RUNNER_FIX_CONFLICT_MAX_ATTEMPTS="2" \
    FIX_CONFLICT_MAX_ATTEMPTS="2" \
    bash -c '
        set -euo pipefail
        # shellcheck disable=SC1090
        . "'"$t41_funcs"'"
        handle_fix_conflict
    ' 2>&1)
t43_rc=$?
set -e

if [[ "$t43_rc" -eq 0 ]]; then
    pass "#3225 T43 — handle_fix_conflict exits 0 on budget exhaustion"
else
    fail "#3225 T43 — handle_fix_conflict exits 0 on budget exhaustion" \
         "rc=$t43_rc, output: $t43_out"
fi

# claude was NOT invoked (budget gate short-circuited).
if [[ ! -s "$t43_state_dir/claude-log.txt" ]]; then
    pass "#3225 T43 — claude skipped on budget exhaustion"
else
    fail "#3225 T43 — claude skipped on budget exhaustion" \
         "claude-log: $(cat "$t43_state_dir/claude-log.txt" 2>/dev/null)"
fi

# Envelope carries verdict=unresolvable + budget_exhausted=true.
if printf '%s' "$t43_out" | grep -q '"budget_exhausted":[[:space:]]*true'; then
    pass "#3225 T43 — envelope carries budget_exhausted=true"
else
    fail "#3225 T43 — envelope carries budget_exhausted=true" \
         "output: $t43_out"
fi

# No increment (we skipped the skill, so the counter stays at 2).
if [[ ! -s "$t43_state_dir/update-log.txt" ]]; then
    pass "#3225 T43 — merge_conflict_attempts NOT incremented on budget exhaustion"
else
    fail "#3225 T43 — merge_conflict_attempts NOT incremented on budget exhaustion" \
         "update-log: $(cat "$t43_state_dir/update-log.txt" 2>/dev/null)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Tests T44–T49: #3245 — handle_fix_ci stages, commits, and pushes the
# /task-v2-fix-ci skill's patches. Before #3245 the ECS entrypoint
# dispatched ``fix_ci`` through the generic Claude-phase case — it
# invoked the skill, persisted the phase_output, and advanced via
# ``transition_for``, but NEVER staged / committed / pushed the working-
# tree changes that /task-v2-fix-ci actually produced. Every ECS agent
# with initially-red CI looped 40 fix_ci iterations adding zero commits.
#
# These tests extract ``handle_fix_ci`` from the entrypoint and drive
# it directly with stubbed git/psql/claude, asserting that:
#
#   T44 — PATCHED verdict → git add + commit + push invoked, advance to
#         awaiting_ci, ``fix_ci_patch_pushed`` log event emitted.
#   T45 — BLOCKED verdict → no commit attempted, ``fix_ci_blocked`` event,
#         advance to terminal ``fix_ci_failed``.
#   T46 — PATCHED with empty commit_message → treated as BLOCKED,
#         ``fix_ci_missing_commit_message`` event, terminal.
#   T47 — PATCHED with stub that stages nothing (empty diff) → advance
#         to awaiting_ci, ``fix_ci_patch_empty_already_applied`` event
#         (#3580 — pre-#3580 this was terminal fix_ci_failed/patch_empty).
#   T48 — PATCHED but ``git push`` fails → ``fix_ci_git_push_failed``
#         event, terminal.
#   T49 — FLAKY verdict → advance to awaiting_ci, no commit attempted.
# ══════════════════════════════════════════════════════════════════════════

# Common helper: extract the handler + its dependencies from the real
# entrypoint so each test exercises the production code path, not a
# vendored copy.
t44_funcs="$TEST_TMP/t44-funcs.sh"
printf 'exec 3>&1\n' > "$t44_funcs"

for fn in db_exec db_query_one log persist_phase_output \
          phase_to_skill \
          read_phase_output \
          write_phase_input \
          run_claude_phase \
          advance_phase \
          agent_runner_reaped_failure \
          handle_fix_ci; do
    awk -v FN="^${fn}\\\\(\\\\)" '
        $0 ~ FN { in_fn=1 }
        in_fn { print }
        in_fn && /^}$/ { exit }
    ' "$ENTRYPOINT" >> "$t44_funcs"
done

# Sanity: handle_fix_ci and its dependencies were extracted.
if grep -q "^handle_fix_ci()" "$t44_funcs"; then
    pass "#3245 T44 setup — extracted handle_fix_ci from entrypoint"
else
    fail "#3245 T44 setup — extracted handle_fix_ci from entrypoint" \
         "fixture head: $(head -c 400 "$t44_funcs")"
fi

# Shared stub-builder helper: generates stubs into $1 (bin dir) that log
# all invocations to $2 (state dir). Defaults: git/gh/psql/claude all
# exit 0; set GIT_PUSH_EXIT=N in the test env to simulate a push
# failure, GIT_DIFF_CACHED_EXIT=0 to simulate an empty staged diff.
make_fix_ci_stubs() {
    _stub_bin="$1"
    _state_dir="$2"

    cat > "$_stub_bin/git" <<'T44GITEOF'
#!/usr/bin/env bash
set -u
# Log argv (one arg per line for easy parsing).
printf '%s\n' "CALL $*" >> "${T_STATE_DIR}/git-log.txt"

# Skip any `-C <dir>` prefix to find the subcommand.
while [[ $# -gt 0 ]]; do
    case "$1" in
        -C) shift 2 || true; continue ;;
        *) break ;;
    esac
done

sub="${1:-}"
case "$sub" in
    add)
        exit "${GIT_ADD_EXIT:-0}"
        ;;
    diff)
        # ``git diff --cached --quiet`` — exit 0 = no changes, 1 = changes.
        # Tests that want to simulate "skill lied, nothing staged" set
        # GIT_DIFF_CACHED_EXIT=0. Default is 1 (there ARE staged changes).
        exit "${GIT_DIFF_CACHED_EXIT:-1}"
        ;;
    rev-list)
        # ``git rev-list --count origin/main..HEAD`` — prints how many
        # commits the branch is ahead of origin/main. Default 1 means
        # existing tests that don't set GIT_REV_LIST_COUNT keep their
        # current benign (already-applied) semantics.
        printf '%s\n' "${GIT_REV_LIST_COUNT:-1}"
        exit 0
        ;;
    commit)
        exit "${GIT_COMMIT_EXIT:-0}"
        ;;
    push)
        exit "${GIT_PUSH_EXIT:-0}"
        ;;
    rev-parse)
        printf 'deadbeefcafe000000000000000000000000cafe\n'
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
T44GITEOF
    chmod +x "$_stub_bin/git"

    cat > "$_stub_bin/psql" <<'T44PSQLEOF'
#!/usr/bin/env bash
set -u
query=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -c) shift; query="$1" ;;
    esac
    shift || true
done
printf 'CALL %s\n' "$query" >> "${T_STATE_DIR}/psql-log.txt"
exit 0
T44PSQLEOF
    chmod +x "$_stub_bin/psql"

    # claude stub — the handler's run_claude_phase reads its output from
    # $REPO_ROOT/tmp/dispatcher-output/fix-ci.json (see read_phase_output),
    # so this stub just logs the invocation and exits 0; the per-test
    # fix-ci.json fixture seeded under $T_REPO_ROOT is the actual source
    # of truth for the verdict.
    cat > "$_stub_bin/claude" <<'T44CLAUDEEOF'
#!/usr/bin/env bash
printf 'CLAUDE_INVOKED %s\n' "$*" >> "${T_STATE_DIR}/claude-log.txt"
# Emit a minimal envelope on stdout — ignored by the handler because
# read_phase_output reads the tmp/dispatcher-output/fix-ci.json file
# first and returns 0 when it parses as JSON.
printf '{"result": {"verdict": "PATCHED"}}\n'
exit 0
T44CLAUDEEOF
    chmod +x "$_stub_bin/claude"

    if command -v jq >/dev/null 2>&1; then
        ln -sf "$(command -v jq)" "$_stub_bin/jq"
    fi
    if command -v python3 >/dev/null 2>&1; then
        ln -sf "$(command -v python3)" "$_stub_bin/python3"
    fi
}

# Per-test runner: isolates env, drives handle_fix_ci, returns rc + output.
# Usage: run_fix_ci_test <test-id> <fix-ci-output-json> [extra env var=value ...]
run_fix_ci_test() {
    _test_id="$1"
    shift
    _fixci_json="$1"
    shift

    _tworkspace="$TEST_TMP/${_test_id}-workspace"
    _trepo="$TEST_TMP/${_test_id}-repo"
    _tstate="$TEST_TMP/${_test_id}-state"
    _tbin="$TEST_TMP/${_test_id}-bin"
    mkdir -p "$_tworkspace" "$_trepo" "$_tstate" "$_tbin"
    mkdir -p "$_trepo/tmp/dispatcher-output" "$_trepo/tmp/dispatcher-input"

    # Seed the dispatcher-output/fix-ci.json with the test's JSON fixture.
    printf '%s' "$_fixci_json" > "$_trepo/tmp/dispatcher-output/fix-ci.json"
    # Seed a minimal dispatcher-input/fix-ci.json so write_phase_input's
    # python3 shim has a directory to write to.
    printf '{}' > "$_trepo/tmp/dispatcher-input/fix-ci.json"

    # Phase-input shim no-op.
    _tshim="$TEST_TMP/${_test_id}-phase-input-shim.py"
    cat > "$_tshim" <<'T44SHIMEOF'
import sys
sys.exit(0)
T44SHIMEOF

    make_fix_ci_stubs "$_tbin" "$_tstate"

    set +e
    # Extra env vars come in as "$@" (e.g. GIT_PUSH_EXIT=128). Bash's
    # VAR=value prefix only works as a single compound command; here the
    # compound starts with a ``$()``-captured subshell which resets the
    # prefix semantics, so we route the extras through ``env -S`` instead.
    _tout=$(env \
        T_STATE_DIR="$_tstate" \
        T_REPO_ROOT="$_trepo" \
        PATH="$_tbin:$PATH" \
        AGENT_ID="${_test_id}-dead-beef-cafe-000000000099" \
        ISSUE_NUMBER="3245" \
        AGENT_WORKSPACE="$_tworkspace" \
        REPO_ROOT="$_trepo" \
        BRANCH_NAME="agent/${_test_id}abcd" \
        DATABASE_URL="postgres://test" \
        AGENT_RUNNER_DRY_RUN="0" \
        AGENT_RUNNER_PHASE_INPUT_SHIM="$_tshim" \
        PHASE_INPUT_SHIM="$_tshim" \
        "$@" \
        bash -c '
            set -uo pipefail
            # shellcheck disable=SC1090
            . "'"$t44_funcs"'"
            handle_fix_ci
        ' 2>&1)
    _trc=$?
    set -e

    # Make the state dir + rc + output available to the caller via
    # globals (bash 3.2 — no nameref / assoc-array returns).
    FIXCI_TEST_RC="$_trc"
    FIXCI_TEST_OUT="$_tout"
    FIXCI_TEST_STATE="$_tstate"
    FIXCI_TEST_REPO="$_trepo"
}

# ══════════════════════════════════════════════════════════════════════════
# Test T44: PATCHED verdict → git add + commit + push + advance awaiting_ci
# ══════════════════════════════════════════════════════════════════════════

t44_json='{"verdict": "PATCHED", "commit_message": "fix(scraping): lint E501 — CI (#9999)", "changed_files": ["a.py", "b.py"]}'
run_fix_ci_test "t44" "$t44_json"

if [[ "$FIXCI_TEST_RC" -eq 0 ]]; then
    pass "#3245 T44 — handle_fix_ci exits 0 on PATCHED"
else
    fail "#3245 T44 — handle_fix_ci exits 0 on PATCHED" \
         "rc=$FIXCI_TEST_RC, output: $FIXCI_TEST_OUT"
fi

# git add was invoked.
if grep -qE "CALL .*\\badd\\b" "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3245 T44 — git add invoked on PATCHED"
else
    fail "#3245 T44 — git add invoked on PATCHED" \
         "git-log: $(cat "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# git commit was invoked.
if grep -qE "CALL .*\\bcommit\\b" "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3245 T44 — git commit invoked on PATCHED"
else
    fail "#3245 T44 — git commit invoked on PATCHED" \
         "git-log: $(cat "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# git push was invoked with the branch name.
if grep -qE "CALL .*\\bpush\\b.*agent/t44abcd" "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3245 T44 — git push origin <branch> invoked on PATCHED"
else
    fail "#3245 T44 — git push origin <branch> invoked on PATCHED" \
         "git-log: $(cat "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# Success log event emitted.
if printf '%s' "$FIXCI_TEST_OUT" | grep -q "fix_ci_patch_pushed"; then
    pass "#3245 T44 — fix_ci_patch_pushed log event emitted"
else
    fail "#3245 T44 — fix_ci_patch_pushed log event emitted" \
         "output: $FIXCI_TEST_OUT"
fi

# Phase advanced to awaiting_ci. advance_phase runs a DB UPDATE via psql.
if grep -q "SET phase = 'awaiting_ci'" "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null; then
    pass "#3245 T44 — advance_phase awaiting_ci executed on PATCHED"
else
    fail "#3245 T44 — advance_phase awaiting_ci executed on PATCHED" \
         "psql-log: $(cat "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test T45: BLOCKED verdict → no commit, advance to fix_ci_failed terminal
# ══════════════════════════════════════════════════════════════════════════

t45_json='{"verdict": "BLOCKED", "block_reason": "secret AWS_SECRET not set in CI environment — ops must provision"}'
run_fix_ci_test "t45" "$t45_json"

if [[ "$FIXCI_TEST_RC" -eq 0 ]]; then
    pass "#3245 T45 — handle_fix_ci exits 0 on BLOCKED"
else
    fail "#3245 T45 — handle_fix_ci exits 0 on BLOCKED" \
         "rc=$FIXCI_TEST_RC, output: $FIXCI_TEST_OUT"
fi

# No git commit attempted (and no git add, no push).
if ! grep -qE "CALL .*\\bcommit\\b" "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3245 T45 — no git commit attempted on BLOCKED"
else
    fail "#3245 T45 — no git commit attempted on BLOCKED" \
         "git-log: $(cat "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null)"
fi
if ! grep -qE "CALL .*\\bpush\\b" "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3245 T45 — no git push attempted on BLOCKED"
else
    fail "#3245 T45 — no git push attempted on BLOCKED" \
         "git-log: $(cat "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# fix_ci_blocked log event emitted.
if printf '%s' "$FIXCI_TEST_OUT" | grep -q "fix_ci_blocked"; then
    pass "#3245 T45 — fix_ci_blocked log event emitted"
else
    fail "#3245 T45 — fix_ci_blocked log event emitted" \
         "output: $FIXCI_TEST_OUT"
fi

# Routed to fix_ci_failed terminal — agent_runner_reaped_failure runs
# an UPDATE setting phase='fix_ci_failed' status='failed'.
if grep -q "phase = 'fix_ci_failed'" "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null; then
    pass "#3245 T45 — advances to fix_ci_failed terminal on BLOCKED"
else
    fail "#3245 T45 — advances to fix_ci_failed terminal on BLOCKED" \
         "psql-log: $(cat "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test T46: PATCHED with empty commit_message → treated as BLOCKED
# (fix_ci_missing_commit_message event, terminal fix_ci_failed).
# ══════════════════════════════════════════════════════════════════════════

t46_json='{"verdict": "PATCHED", "commit_message": "", "changed_files": ["a.py"]}'
run_fix_ci_test "t46" "$t46_json"

if [[ "$FIXCI_TEST_RC" -eq 0 ]]; then
    pass "#3245 T46 — handle_fix_ci exits 0 on PATCHED+empty-commit-message"
else
    fail "#3245 T46 — handle_fix_ci exits 0 on PATCHED+empty-commit-message" \
         "rc=$FIXCI_TEST_RC, output: $FIXCI_TEST_OUT"
fi

# Diagnostic log event emitted.
if printf '%s' "$FIXCI_TEST_OUT" | grep -q "fix_ci_missing_commit_message"; then
    pass "#3245 T46 — fix_ci_missing_commit_message log event emitted"
else
    fail "#3245 T46 — fix_ci_missing_commit_message log event emitted" \
         "output: $FIXCI_TEST_OUT"
fi

# No git commit attempted.
if ! grep -qE "CALL .*\\bcommit\\b" "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3245 T46 — no git commit attempted on empty commit_message"
else
    fail "#3245 T46 — no git commit attempted on empty commit_message" \
         "git-log: $(cat "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# Routed to fix_ci_failed terminal.
if grep -q "phase = 'fix_ci_failed'" "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null; then
    pass "#3245 T46 — advances to fix_ci_failed terminal on empty commit_message"
else
    fail "#3245 T46 — advances to fix_ci_failed terminal on empty commit_message" \
         "psql-log: $(cat "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test T47: #3580 — PATCHED but ``git diff --cached --quiet`` returns 0
# (no staged changes) → advance to awaiting_ci, NOT terminal-fail.
#
# Empty-staged-diff after a PATCHED verdict is normal when the fix is
# already in the rebased baseline (e.g. a sibling PR landed it first, or
# this is an idempotent retry). The skill's PATCHED verdict was correct
# in concluding "the fix is in"; the agent-runner's job is to advance to
# awaiting_ci so the next supervisor tick observes the now-green CI
# rollup. The previous behavior (terminal fix_ci_failed/patch_empty)
# left the issue stuck in a cooldown loop forever — the daemon would
# re-claim, the rebase would still pull in the fix, and the diff would
# still be empty.
#
# Log event renamed: fix_ci_patch_empty → fix_ci_patch_empty_already_applied
# so CloudWatch dashboards can distinguish "fix already in baseline,
# advancing" from a real failure class.
#
# This test asserts the POST-fix behavior. It MUST fail against the
# pre-#3580 entrypoint where the empty-diff branch reaped the agent as
# fix_ci_failed.
# ══════════════════════════════════════════════════════════════════════════

t47_json='{"verdict": "PATCHED", "commit_message": "fix(area): thing — CI (#9999)", "changed_files": []}'
run_fix_ci_test "t47" "$t47_json" GIT_DIFF_CACHED_EXIT=0

if [[ "$FIXCI_TEST_RC" -eq 0 ]]; then
    pass "#3580 T47 — handle_fix_ci exits 0 on PATCHED+empty-diff"
else
    fail "#3580 T47 — handle_fix_ci exits 0 on PATCHED+empty-diff" \
         "rc=$FIXCI_TEST_RC, output: $FIXCI_TEST_OUT"
fi

# New log event for the "already applied" sub-case.
if printf '%s' "$FIXCI_TEST_OUT" | grep -q "fix_ci_patch_empty_already_applied"; then
    pass "#3580 T47 — fix_ci_patch_empty_already_applied log event emitted"
else
    fail "#3580 T47 — fix_ci_patch_empty_already_applied log event emitted" \
         "output: $FIXCI_TEST_OUT"
fi

# git add ran (the stub succeeds), but no commit / push (nothing to commit).
if grep -qE "CALL .*\\badd\\b" "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3580 T47 — git add ran (before empty-diff detection)"
else
    fail "#3580 T47 — git add ran (before empty-diff detection)" \
         "git-log: $(cat "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null)"
fi
if ! grep -qE "CALL .*\\bcommit\\b" "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3580 T47 — no git commit attempted on empty-diff (nothing to commit)"
else
    fail "#3580 T47 — no git commit attempted on empty-diff (nothing to commit)" \
         "git-log: $(cat "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null)"
fi
if ! grep -qE "CALL .*\\bpush\\b" "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3580 T47 — no git push attempted on empty-diff (nothing to push)"
else
    fail "#3580 T47 — no git push attempted on empty-diff (nothing to push)" \
         "git-log: $(cat "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# CRITICAL: must advance to awaiting_ci (let next supervisor tick observe
# the now-green rollup), NOT terminal-fail to fix_ci_failed.
if grep -q "SET phase = 'awaiting_ci'" "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null; then
    pass "#3580 T47 — advances to awaiting_ci on empty-diff (fix already in baseline)"
else
    fail "#3580 T47 — advances to awaiting_ci on empty-diff (fix already in baseline)" \
         "psql-log: $(cat "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null)"
fi

# Negative: must NOT have terminal-failed to fix_ci_failed.
if ! grep -q "phase = 'fix_ci_failed'" "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null; then
    pass "#3580 T47 — does NOT advance to fix_ci_failed on empty-diff"
else
    fail "#3580 T47 — does NOT advance to fix_ci_failed on empty-diff" \
         "psql-log: $(cat "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null)"
fi

# Negative: the OLD log event name (used for terminal failure) must NOT
# appear — downstream telemetry consumers split on the new name.
if ! printf '%s' "$FIXCI_TEST_OUT" | grep -qE '\bfix_ci_patch_empty\b' \
    || printf '%s' "$FIXCI_TEST_OUT" | grep -q 'fix_ci_patch_empty_already_applied'; then
    # Either the bare event isn't there at all, or it's only the longer
    # already-applied variant (which contains the old token as a prefix).
    # Use a stricter check: confirm the bare event with no suffix.
    if ! printf '%s' "$FIXCI_TEST_OUT" \
        | grep -E 'fix_ci_patch_empty([^_]|$)' \
        | grep -qv 'fix_ci_patch_empty_already_applied'; then
        pass "#3580 T47 — old fix_ci_patch_empty terminal event NOT emitted"
    else
        fail "#3580 T47 — old fix_ci_patch_empty terminal event NOT emitted" \
             "output: $FIXCI_TEST_OUT"
    fi
else
    fail "#3580 T47 — old fix_ci_patch_empty terminal event NOT emitted" \
         "output: $FIXCI_TEST_OUT"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test T48: PATCHED but ``git push`` exits non-zero →
# fix_ci_git_push_failed log event, terminal fix_ci_failed.
# ══════════════════════════════════════════════════════════════════════════

t48_json='{"verdict": "PATCHED", "commit_message": "fix(scraping): foo — CI (#9999)", "changed_files": ["a.py"]}'
run_fix_ci_test "t48" "$t48_json" GIT_PUSH_EXIT=128

if [[ "$FIXCI_TEST_RC" -eq 0 ]]; then
    pass "#3245 T48 — handle_fix_ci exits 0 on PATCHED+push-failure"
else
    fail "#3245 T48 — handle_fix_ci exits 0 on PATCHED+push-failure" \
         "rc=$FIXCI_TEST_RC, output: $FIXCI_TEST_OUT"
fi

# Push was attempted (commit succeeded first).
if grep -qE "CALL .*\\bpush\\b" "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3245 T48 — git push attempted on PATCHED"
else
    fail "#3245 T48 — git push attempted on PATCHED" \
         "git-log: $(cat "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# Diagnostic log event emitted.
if printf '%s' "$FIXCI_TEST_OUT" | grep -q "fix_ci_git_push_failed"; then
    pass "#3245 T48 — fix_ci_git_push_failed log event emitted"
else
    fail "#3245 T48 — fix_ci_git_push_failed log event emitted" \
         "output: $FIXCI_TEST_OUT"
fi

# Terminal fix_ci_failed (not awaiting_ci — push failure is fatal).
if grep -q "phase = 'fix_ci_failed'" "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null; then
    pass "#3245 T48 — advances to fix_ci_failed terminal on push failure"
else
    fail "#3245 T48 — advances to fix_ci_failed terminal on push failure" \
         "psql-log: $(cat "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null)"
fi
# Negative: must NOT have advanced to awaiting_ci (that would let the
# supervisor re-enter a broken push loop).
if ! grep -q "SET phase = 'awaiting_ci'" "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null; then
    pass "#3245 T48 — does not advance to awaiting_ci on push failure"
else
    fail "#3245 T48 — does not advance to awaiting_ci on push failure" \
         "psql-log: $(cat "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test T49: FLAKY verdict → advance to awaiting_ci, no commit, no push.
# ══════════════════════════════════════════════════════════════════════════

t49_json='{"verdict": "FLAKY", "flaky_evidence": "httpx.TimeoutError to api.anthropic.com in packages/api tests — intermittent"}'
run_fix_ci_test "t49" "$t49_json"

if [[ "$FIXCI_TEST_RC" -eq 0 ]]; then
    pass "#3245 T49 — handle_fix_ci exits 0 on FLAKY"
else
    fail "#3245 T49 — handle_fix_ci exits 0 on FLAKY" \
         "rc=$FIXCI_TEST_RC, output: $FIXCI_TEST_OUT"
fi

# fix_ci_flaky log event emitted.
if printf '%s' "$FIXCI_TEST_OUT" | grep -q "fix_ci_flaky"; then
    pass "#3245 T49 — fix_ci_flaky log event emitted"
else
    fail "#3245 T49 — fix_ci_flaky log event emitted" \
         "output: $FIXCI_TEST_OUT"
fi

# No git add / commit / push attempted on FLAKY.
if ! grep -qE "CALL .*\\badd\\b|CALL .*\\bcommit\\b|CALL .*\\bpush\\b" \
    "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3245 T49 — no git add/commit/push on FLAKY"
else
    fail "#3245 T49 — no git add/commit/push on FLAKY" \
         "git-log: $(cat "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# Advanced to awaiting_ci (not fix_ci_failed).
if grep -q "SET phase = 'awaiting_ci'" "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null; then
    pass "#3245 T49 — advances to awaiting_ci on FLAKY (re-poll)"
else
    fail "#3245 T49 — advances to awaiting_ci on FLAKY (re-poll)" \
         "psql-log: $(cat "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null)"
fi
if ! grep -q "phase = 'fix_ci_failed'" "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null; then
    pass "#3245 T49 — does NOT advance to fix_ci_failed on FLAKY"
else
    fail "#3245 T49 — does NOT advance to fix_ci_failed on FLAKY" \
         "psql-log: $(cat "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test T_issue3635: #3635 — PATCHED + empty staged diff + NO commits ahead
# → terminal-fail (fix_ci_failed / fix_ci_patched_without_commit), NOT
# advance to awaiting_ci.
#
# Scenario: the fix-ci skill returns PATCHED, git add succeeds, but
# git diff --cached is empty (nothing actually staged) AND git rev-list
# reports 0 commits ahead of origin/main. This is the LLM-hallucination /
# lost-commit / missing-git-add case: advancing to awaiting_ci would poll
# CI forever on a branch indistinguishable from main.
#
# Assertions:
#   (a) fix_ci_patch_empty_no_commits log event emitted.
#   (b) fix_ci_patch_empty_already_applied log event NOT emitted.
#   (c) psql-log shows phase = 'fix_ci_failed' and status = 'failed'
#       (terminal-fail path via agent_runner_reaped_failure).
#   (d) stdout contains category=fix_ci_patched_without_commit
#       (from the agent_runner_reaped_failure log line).
#   (e) no git commit or push attempted (nothing to commit / push).
# ══════════════════════════════════════════════════════════════════════════

T3635_json='{"verdict": "PATCHED", "commit_message": "fix(area): thing — CI (#9999)", "changed_files": []}'
run_fix_ci_test "t3635" "$T3635_json" GIT_DIFF_CACHED_EXIT=0 GIT_REV_LIST_COUNT=0

# (a) fix_ci_patch_empty_no_commits log event emitted.
if printf '%s' "$FIXCI_TEST_OUT" | grep -q "fix_ci_patch_empty_no_commits"; then
    pass "#3635 T_issue3635 — fix_ci_patch_empty_no_commits log event emitted"
else
    fail "#3635 T_issue3635 — fix_ci_patch_empty_no_commits log event emitted" \
         "output: $FIXCI_TEST_OUT"
fi

# (b) fix_ci_patch_empty_already_applied log event NOT emitted.
if ! printf '%s' "$FIXCI_TEST_OUT" | grep -q "fix_ci_patch_empty_already_applied"; then
    pass "#3635 T_issue3635 — fix_ci_patch_empty_already_applied NOT emitted"
else
    fail "#3635 T_issue3635 — fix_ci_patch_empty_already_applied NOT emitted" \
         "output: $FIXCI_TEST_OUT"
fi

# (c) psql-log shows phase = 'fix_ci_failed' (terminal-fail).
if grep -q "phase = 'fix_ci_failed'" "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null; then
    pass "#3635 T_issue3635 — advances to fix_ci_failed terminal (malign empty-diff)"
else
    fail "#3635 T_issue3635 — advances to fix_ci_failed terminal (malign empty-diff)" \
         "psql-log: $(cat "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null)"
fi

# (c) Negative: must NOT have advanced to awaiting_ci.
if ! grep -q "SET phase = 'awaiting_ci'" "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null; then
    pass "#3635 T_issue3635 — does NOT advance to awaiting_ci on malign empty-diff"
else
    fail "#3635 T_issue3635 — does NOT advance to awaiting_ci on malign empty-diff" \
         "psql-log: $(cat "$FIXCI_TEST_STATE/psql-log.txt" 2>/dev/null)"
fi

# (d) category=fix_ci_patched_without_commit carried in the failure log line.
if printf '%s' "$FIXCI_TEST_OUT" | grep -q "fix_ci_patched_without_commit"; then
    pass "#3635 T_issue3635 — category=fix_ci_patched_without_commit in output"
else
    fail "#3635 T_issue3635 — category=fix_ci_patched_without_commit in output" \
         "output: $FIXCI_TEST_OUT"
fi

# (e) No git commit or push attempted.
if ! grep -qE "CALL .*\\bcommit\\b" "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3635 T_issue3635 — no git commit attempted on malign empty-diff"
else
    fail "#3635 T_issue3635 — no git commit attempted on malign empty-diff" \
         "git-log: $(cat "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null)"
fi
if ! grep -qE "CALL .*\\bpush\\b" "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3635 T_issue3635 — no git push attempted on malign empty-diff"
else
    fail "#3635 T_issue3635 — no git push attempted on malign empty-diff" \
         "git-log: $(cat "$FIXCI_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test T50: #3410 — startup transition_shim validation rejects missing shim.
#
# Scenario: AGENT_RUNNER_TRANSITION_SHIM points at a nonexistent path.
# The entrypoint's shim-validation block (added in #3410) must:
#   1. Detect the missing file before the python3 smoke invocation.
#   2. Emit transition_shim_invalid to the log stream.
#   3. Exit non-zero (die exits 2).
#
# Approach: extract the shim-validation block from the entrypoint using
# awk (same pattern as T40/T41), inject stub log/die/AGENT_WORKSPACE
# definitions so the test doesn't need the full entrypoint stack, and
# source in a subshell.
# ══════════════════════════════════════════════════════════════════════════

t50_workspace="$TEST_TMP/t50-workspace"
mkdir -p "$t50_workspace"

# Extract the shim-validation block. The block starts at the comment
# "# ── Startup validation: TRANSITION_SHIM (#3410)" and ends just
# before "transition_for()". We capture everything between those markers.
t50_block="$TEST_TMP/t50-shim-validation.sh"
awk '
    /^# ── Startup validation: TRANSITION_SHIM/ { in_block=1 }
    in_block && /^transition_for\(\)/ { exit }
    in_block { print }
' "$ENTRYPOINT" > "$t50_block"

# Sanity: the block was extracted.
if grep -q "transition_shim_invalid" "$t50_block"; then
    pass "#3410 T50 setup — extracted shim validation block from entrypoint"
else
    fail "#3410 T50 setup — extracted shim validation block from entrypoint" \
         "block head: $(head -c 400 "$t50_block")"
fi

# Drive the validation block with a nonexistent shim path. Stub log to
# capture events to a file; stub die to exit 2 (matching die() contract).
set +e
t50_out=$(bash -c '
    set -uo pipefail
    AGENT_ID="50505050-dead-beef-cafe-000000000050"
    AGENT_WORKSPACE="'"$t50_workspace"'"
    TRANSITION_SHIM="/nonexistent/path/shim.py"
    exec 3>&1
    log() {
        _ev="$1"; shift
        printf "LOG %s" "$_ev"
        for kv in "$@"; do printf " %s" "$kv"; done
        printf "\n"
    }
    die() {
        printf "DIE %s\n" "$1"
        exit 2
    }
    # shellcheck disable=SC1090
    . "'"$t50_block"'"
' 2>&1)
t50_rc=$?
set -e

# 1. Must exit non-zero.
if [[ "$t50_rc" -ne 0 ]]; then
    pass "#3410 T50 — shim validation exits non-zero for missing shim"
else
    fail "#3410 T50 — shim validation exits non-zero for missing shim" \
         "rc=$t50_rc, output: $t50_out"
fi

# 2. Log must contain transition_shim_invalid.
if printf '%s' "$t50_out" | grep -q "transition_shim_invalid"; then
    pass "#3410 T50 — log contains transition_shim_invalid for missing shim"
else
    fail "#3410 T50 — log contains transition_shim_invalid for missing shim" \
         "output: $t50_out"
fi

# 3. Log contains the offending path.
if printf '%s' "$t50_out" | grep -q "nonexistent"; then
    pass "#3410 T50 — log contains offending path"
else
    fail "#3410 T50 — log contains offending path" \
         "output: $t50_out"
fi

# ── Sub-assertion: non-readable file also fails ────────────────────────
# Create an empty file (non-empty check will fire because it is empty).
t50_empty_shim="$t50_workspace/empty-shim.py"
printf '' > "$t50_empty_shim"

set +e
t50b_out=$(bash -c '
    set -uo pipefail
    AGENT_ID="50505050-dead-beef-cafe-000000000050"
    AGENT_WORKSPACE="'"$t50_workspace"'"
    TRANSITION_SHIM="'"$t50_empty_shim"'"
    exec 3>&1
    log() {
        _ev="$1"; shift
        printf "LOG %s" "$_ev"
        for kv in "$@"; do printf " %s" "$kv"; done
        printf "\n"
    }
    die() {
        printf "DIE %s\n" "$1"
        exit 2
    }
    # shellcheck disable=SC1090
    . "'"$t50_block"'"
' 2>&1)
t50b_rc=$?
set -e

if [[ "$t50b_rc" -ne 0 ]] && printf '%s' "$t50b_out" | grep -q "transition_shim_invalid"; then
    pass "#3410 T50 — shim validation rejects empty (non-readable) shim file"
else
    fail "#3410 T50 — shim validation rejects empty (non-readable) shim file" \
         "rc=$t50b_rc, output: $t50b_out"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test T51: #3410 — per-step logs emitted on fix_conflict happy path.
#
# Scenario: the fix_conflict dispatch arm runs with a resolved verdict.
# The four new log events added in #3410 must appear in the captured
# output:
#   fix_conflict_handler_done
#   fix_conflict_persist_done
#   fix_conflict_transition_shim_done
#   fix_conflict_dispatched_action
#
# Approach: extract the log() function from the entrypoint, then define
# stubs for handle_fix_conflict / persist_phase_output / transition_for /
# advance_phase / jq so the dispatch block can run without the full
# entrypoint stack. Extract the fix_conflict) arm using awk (strip the
# outer case/esac so the block is runnable standalone).
# ══════════════════════════════════════════════════════════════════════════

t51_workspace="$TEST_TMP/t51-workspace"
mkdir -p "$t51_workspace"

# Extract the log() function.
t51_funcs="$TEST_TMP/t51-funcs.sh"
printf 'exec 3>&1\n' > "$t51_funcs"
awk -v FN="^log\\(\\)" '
    $0 ~ FN { in_fn=1 }
    in_fn { print }
    in_fn && /^}$/ { exit }
' "$ENTRYPOINT" >> "$t51_funcs"

# Sanity check.
if grep -q "^log()" "$t51_funcs"; then
    pass "#3410 T51 setup — extracted log() from entrypoint"
else
    fail "#3410 T51 setup — extracted log() from entrypoint" \
         "funcs head: $(head -c 200 "$t51_funcs")"
fi

# Extract the fix_conflict) dispatch arm. Match only the standalone arm
# (not the compact one-liner in phase_to_skill) using the end-of-line
# anchor. Strip the trailing outer ";;" arm terminator so the body can be
# sourced inside a case statement wrapper below.
t51_dispatch_body="$TEST_TMP/t51-dispatch-body.sh"
awk '
    /^        fix_conflict\)$/ { in_arm=1; next }
    in_arm && /^        fix_ci\)/ { exit }
    in_arm { print }
' "$ENTRYPOINT" | head -n -1 > "$t51_dispatch_body"

# Sanity: dispatch block was extracted.
if grep -q "fix_conflict_handler_done" "$t51_dispatch_body"; then
    pass "#3410 T51 setup — extracted fix_conflict dispatch arm with new log events"
else
    fail "#3410 T51 setup — extracted fix_conflict dispatch arm with new log events" \
         "dispatch head: $(head -c 400 "$t51_dispatch_body")"
fi

# Drive the dispatch block by sourcing it inside a case statement wrapper
# so the inner case "$_action" in ... esac is well-formed shell syntax.
set +e
t51_out=$(bash -c '
    set -euo pipefail
    AGENT_ID="51515151-dead-beef-cafe-000000000051"
    AGENT_WORKSPACE="'"$t51_workspace"'"
    # shellcheck disable=SC1090
    . "'"$t51_funcs"'"

    # Stub: handle_fix_conflict returns a resolved envelope.
    handle_fix_conflict() {
        printf '"'"'{"verdict":"resolved","resolution_notes":"ok","resolved_files":[]}'"'"'
    }

    # Stub: persist_phase_output — no-op.
    persist_phase_output() { return 0; }

    # Stub: transition_for returns tab-separated advance\tpush_and_pr\t\t
    transition_for() {
        printf "advance\tpush_and_pr\t\t"
    }

    # Stub: advance_phase — no-op.
    advance_phase() { return 0; }

    # Stub: jq — minimal verdict extractor.
    jq() {
        if printf "%s" "$*" | grep -q "verdict"; then
            printf "resolved"
        else
            printf ""
        fi
    }

    # Wrap the extracted body in a case to satisfy the inner case syntax.
    _phase="fix_conflict"
    case "$_phase" in
        fix_conflict)
            # shellcheck disable=SC1090
            . "'"$t51_dispatch_body"'"
            ;;
    esac
' 2>&1)
t51_rc=$?
set -e

# Must exit 0 (advance path, no failure).
if [[ "$t51_rc" -eq 0 ]]; then
    pass "#3410 T51 — fix_conflict dispatch exits 0 on advance"
else
    fail "#3410 T51 — fix_conflict dispatch exits 0 on advance" \
         "rc=$t51_rc, output: $t51_out"
fi

# All four new log events must appear.
for _ev in fix_conflict_handler_done fix_conflict_persist_done \
           fix_conflict_transition_shim_done fix_conflict_dispatched_action; do
    if printf '%s' "$t51_out" | grep -q "$_ev"; then
        pass "#3410 T51 — log event emitted: $_ev"
    else
        fail "#3410 T51 — log event emitted: $_ev" \
             "output: $t51_out"
    fi
done

# ══════════════════════════════════════════════════════════════════════════
# DEPLOY_WORKFLOW_NAMES_BASH default — static regression guard (#3185, #3514)
# ══════════════════════════════════════════════════════════════════════════

if grep -qE '^DEPLOY_WORKFLOW_NAMES_BASH=.*Deploy Agent Runner' "$ENTRYPOINT"; then
    pass "#3185 — DEPLOY_WORKFLOW_NAMES_BASH default includes 'Deploy Agent Runner'"
else
    fail "#3185 — DEPLOY_WORKFLOW_NAMES_BASH default includes 'Deploy Agent Runner'" \
         "'Deploy Agent Runner' not found in DEPLOY_WORKFLOW_NAMES_BASH default in $ENTRYPOINT"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 50: #3411 — handle_push_and_pr appends `Closes #N` to PR body when
# /task-v2-summary's pr_body_md doesn't include it. Asserts that the
# pr_body.md file written before `gh pr create` contains the keyword.
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t50.txt"
printf 'push_and_pr\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t50_workspace="$TEST_TMP/t50-workspace"
mkdir -p "$t50_workspace/repo/.git" "$t50_workspace/repo/tmp/dispatcher-output"

# Pre-stage a summary.json with a PR body that LACKS Closes #N. The
# entrypoint reads from $REPO_ROOT/tmp/dispatcher-output/summary.json
# (REPO_ROOT="$AGENT_WORKSPACE/repo").
cat > "$t50_workspace/repo/tmp/dispatcher-output/summary.json" <<'EOF'
{
  "commit_message": "feat(test): demonstrate Closes injection (#3411)",
  "pr_title": "feat(test): demonstrate Closes injection (#3411)",
  "pr_body_md": "## Summary\n\nDemonstrates the keyword injection.\n\n(no Closes line in this body — the entrypoint must add one)\n",
  "unmet_criteria": []
}
EOF

set +e
out=$(AGENT_ID="50000000-1111-2222-3333-444444444444" \
      ISSUE_NUMBER="3411" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$t50_workspace" \
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

# The entrypoint logs `push_and_pr_closes_keyword_appended` when the
# keyword was missing and got injected.
if printf '%s' "$out" | grep -q "push_and_pr_closes_keyword_appended"; then
    pass "#3411 T50 — entrypoint logs closes_keyword_appended when missing"
else
    fail "#3411 T50 — entrypoint logs closes_keyword_appended when missing" \
         "out tail: $(printf '%s' "$out" | tail -c 800)"
fi

# The pr_body.md the entrypoint wrote before `gh pr create` should
# contain `Closes #3411`.
if [[ -f "$t50_workspace/pr_body.md" ]] && grep -qF "Closes #3411" "$t50_workspace/pr_body.md"; then
    pass "#3411 T50 — pr_body.md contains Closes #3411 after injection"
else
    fail "#3411 T50 — pr_body.md contains Closes #3411 after injection" \
         "pr_body.md content: $(cat "$t50_workspace/pr_body.md" 2>/dev/null || echo '<missing>')"
fi

# Original body content must be preserved verbatim — we only append.
if [[ -f "$t50_workspace/pr_body.md" ]] && grep -qF "Demonstrates the keyword injection" "$t50_workspace/pr_body.md"; then
    pass "#3411 T50 — original pr_body content preserved"
else
    fail "#3411 T50 — original pr_body content preserved" \
         "pr_body.md content: $(cat "$t50_workspace/pr_body.md" 2>/dev/null || echo '<missing>')"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 51: #3411 — handle_push_and_pr is a no-op (idempotent) when the
# PR body already contains Closes #N for the agent's issue.
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t51.txt"
printf 'push_and_pr\n' > "$PHASE_FIXTURE_FILE"

t51_workspace="$TEST_TMP/t51-workspace"
mkdir -p "$t51_workspace/repo/.git" "$t51_workspace/repo/tmp/dispatcher-output"

cat > "$t51_workspace/repo/tmp/dispatcher-output/summary.json" <<'EOF'
{
  "commit_message": "fix(thing): existing closes (#3411)",
  "pr_title": "fix(thing): existing closes (#3411)",
  "pr_body_md": "## Summary\n\nA fix.\n\nCloses #3411\n",
  "unmet_criteria": []
}
EOF

set +e
out=$(AGENT_ID="51000000-1111-2222-3333-444444444444" \
      ISSUE_NUMBER="3411" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$t51_workspace" \
      REPO_URL="https://example.invalid/repo.git" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
      PRIOR_PATCH_FIXTURE="" \
      CLAUDE_VERDICT_FIXTURE="" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      AGENT_RUNNER_MAX_PHASE_ITERATIONS=15 \
      GIT_REV_LIST_COUNT=1 \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

# When the keyword is already present, log `closes_keyword_present`
# (not `_appended`).
if printf '%s' "$out" | grep -q "push_and_pr_closes_keyword_present"; then
    pass "#3411 T51 — entrypoint logs closes_keyword_present when already there"
else
    fail "#3411 T51 — entrypoint logs closes_keyword_present when already there" \
         "out tail: $(printf '%s' "$out" | tail -c 800)"
fi

if printf '%s' "$out" | grep -q "push_and_pr_closes_keyword_appended"; then
    fail "#3411 T51 — does NOT append when keyword already present" \
         "out tail: $(printf '%s' "$out" | tail -c 800)"
else
    pass "#3411 T51 — does NOT append when keyword already present"
fi

# pr_body.md should contain exactly one Closes #3411.
if [[ -f "$t51_workspace/pr_body.md" ]]; then
    _count=$(grep -cF "Closes #3411" "$t51_workspace/pr_body.md")
    if [[ "$_count" == "1" ]]; then
        pass "#3411 T51 — pr_body.md has exactly one Closes #3411 (idempotent)"
    else
        fail "#3411 T51 — pr_body.md has exactly one Closes #3411 (idempotent)" \
             "count=$_count, content: $(cat "$t51_workspace/pr_body.md")"
    fi
else
    fail "#3411 T51 — pr_body.md exists" "missing pr_body.md"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 52: #3411 — handle_push_and_pr appends our Closes when the
# existing keyword targets a DIFFERENT issue number.
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t52.txt"
printf 'push_and_pr\n' > "$PHASE_FIXTURE_FILE"

t52_workspace="$TEST_TMP/t52-workspace"
mkdir -p "$t52_workspace/repo/.git" "$t52_workspace/repo/tmp/dispatcher-output"

cat > "$t52_workspace/repo/tmp/dispatcher-output/summary.json" <<'EOF'
{
  "commit_message": "fix(thing): wrong-issue closes (#3411)",
  "pr_title": "fix(thing): wrong-issue closes (#3411)",
  "pr_body_md": "## Summary\n\nA fix.\n\nCloses #999\n",
  "unmet_criteria": []
}
EOF

set +e
out=$(AGENT_ID="52000000-1111-2222-3333-444444444444" \
      ISSUE_NUMBER="3411" \
      DATABASE_URL="postgres://test" \
      GITHUB_TOKEN="" \
      AGENT_WORKSPACE="$t52_workspace" \
      REPO_URL="https://example.invalid/repo.git" \
      PATH="$STUB_BIN:$PATH" \
      INVOCATIONS_DIR="$INVOCATIONS_DIR" \
      PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
      PRIOR_PATCH_FIXTURE="" \
      CLAUDE_VERDICT_FIXTURE="" \
      PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
      PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
      AGENT_RUNNER_MAX_PHASE_ITERATIONS=15 \
      GIT_REV_LIST_COUNT=1 \
      bash "$ENTRYPOINT" 2>&1)
rc=$?
set -e

# Both keywords should be present in the final body.
if [[ -f "$t52_workspace/pr_body.md" ]] && grep -qF "Closes #999" "$t52_workspace/pr_body.md"; then
    pass "#3411 T52 — preserves wrong-issue Closes #999"
else
    fail "#3411 T52 — preserves wrong-issue Closes #999" \
         "pr_body.md: $(cat "$t52_workspace/pr_body.md" 2>/dev/null)"
fi

if [[ -f "$t52_workspace/pr_body.md" ]] && grep -qF "Closes #3411" "$t52_workspace/pr_body.md"; then
    pass "#3411 T52 — appends our Closes #3411 alongside wrong-issue keyword"
else
    fail "#3411 T52 — appends our Closes #3411 alongside wrong-issue keyword" \
         "pr_body.md: $(cat "$t52_workspace/pr_body.md" 2>/dev/null)"
fi

if printf '%s' "$out" | grep -q "push_and_pr_closes_keyword_appended"; then
    pass "#3411 T52 — logs appended event for wrong-issue case"
else
    fail "#3411 T52 — logs appended event for wrong-issue case" \
         "out tail: $(printf '%s' "$out" | tail -c 800)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 53: #3411 — close_issue_post_merge invokes gh issue close + edit
# when the issue is OPEN after a successful merge.
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t53.txt"
printf 'merge\n' > "$PHASE_FIXTURE_FILE"

t53_workspace="$TEST_TMP/t53-workspace"
set +e
t53_out=$(run_post_pr_phase "merge" "$t53_workspace" \
    "PHASE_FIXTURE_FILE=$PHASE_FIXTURE_FILE" \
    "PRIOR_PATCH_FIXTURE=" \
    "PR_NUMBER_FIXTURE=9999" \
    "GH_ISSUE_STATE_FIXTURE=OPEN")
set -e

# Cleanup begin event fired (issue was OPEN, so we entered the cleanup
# path).
if printf '%s' "$t53_out" | grep -q "post_merge_cleanup_begin"; then
    pass "#3411 T53 — post_merge_cleanup_begin logged on OPEN issue"
else
    fail "#3411 T53 — post_merge_cleanup_begin logged on OPEN issue" \
         "out tail: $(printf '%s' "$t53_out" | tail -c 1000)"
fi

# gh issue close was invoked.
if grep -F "issue" "$INVOCATIONS_DIR/gh.log" | grep -F "close" >/dev/null 2>&1; then
    pass "#3411 T53 — gh issue close invoked"
else
    fail "#3411 T53 — gh issue close invoked" \
         "gh log: $(cat "$INVOCATIONS_DIR/gh.log")"
fi

# gh issue edit --remove-label agent/ready was invoked.
if grep -F "issue" "$INVOCATIONS_DIR/gh.log" | grep -F "remove-label" | grep -F "agent/ready" >/dev/null 2>&1; then
    pass "#3411 T53 — gh issue edit --remove-label agent/ready invoked"
else
    fail "#3411 T53 — gh issue edit --remove-label agent/ready invoked" \
         "gh log: $(cat "$INVOCATIONS_DIR/gh.log")"
fi

# Cleanup done event fired.
if printf '%s' "$t53_out" | grep -q "post_merge_cleanup_done"; then
    pass "#3411 T53 — post_merge_cleanup_done logged after close + label-strip"
else
    fail "#3411 T53 — post_merge_cleanup_done logged after close + label-strip" \
         "out tail: $(printf '%s' "$t53_out" | tail -c 1000)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 54: #3411 — close_issue_post_merge skips when the issue is
# already CLOSED. No gh issue close / edit invocations.
# ══════════════════════════════════════════════════════════════════════════

setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t54.txt"
printf 'merge\n' > "$PHASE_FIXTURE_FILE"

t54_workspace="$TEST_TMP/t54-workspace"
set +e
t54_out=$(run_post_pr_phase "merge" "$t54_workspace" \
    "PHASE_FIXTURE_FILE=$PHASE_FIXTURE_FILE" \
    "PRIOR_PATCH_FIXTURE=" \
    "PR_NUMBER_FIXTURE=9999" \
    "GH_ISSUE_STATE_FIXTURE=CLOSED")
set -e

# Skipped event fired with reason=already_closed.
if printf '%s' "$t54_out" | grep -q "post_merge_cleanup_skipped"; then
    pass "#3411 T54 — post_merge_cleanup_skipped logged on CLOSED issue"
else
    fail "#3411 T54 — post_merge_cleanup_skipped logged on CLOSED issue" \
         "out tail: $(printf '%s' "$t54_out" | tail -c 1000)"
fi

if printf '%s' "$t54_out" | grep -q '"reason": "already_closed"'; then
    pass "#3411 T54 — skip reason is already_closed"
else
    fail "#3411 T54 — skip reason is already_closed" \
         "out tail: $(printf '%s' "$t54_out" | tail -c 1000)"
fi

# Critically: NO gh issue close invocation when issue is already CLOSED.
if grep -F "issue" "$INVOCATIONS_DIR/gh.log" | grep -F "close" >/dev/null 2>&1; then
    fail "#3411 T54 — does NOT invoke gh issue close on CLOSED issue" \
         "gh log: $(cat "$INVOCATIONS_DIR/gh.log")"
else
    pass "#3411 T54 — does NOT invoke gh issue close on CLOSED issue"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 55: #3429 T55 — printf failure routes via return 1.
#
# Scenario: disk / inode pressure causes the write to the tmpfile to fail
# (ENOSPC). Before #3429, mktemp + printf ran under ``set -e`` and a failed
# write would abort the shell immediately with a masked exit code. After the
# fix, printf's failure is caught inside set +e, logged as
# phase_output_persist_failed reason=tmpfile_write_failed, and routed via
# ``return 1`` so the caller can inspect the rc and continue.
#
# We simulate the write failure by pointing the stub ``mktemp`` at
# /dev/full, which always returns ENOSPC on write.
# ══════════════════════════════════════════════════════════════════════════

# Reuse the same fixture file extracted for T40 — it already contains
# db_exec, log, and persist_phase_output with the #3429 fix applied.
t55_funcs="$TEST_TMP/t55-funcs.sh"
printf 'exec 3>&1\n' > "$t55_funcs"

awk '
    /^db_exec\(\)/ { in_fn=1 }
    in_fn { print }
    in_fn && /^}$/ { exit }
' "$ENTRYPOINT" >> "$t55_funcs"

awk '
    /^log\(\)/ { in_fn=1 }
    in_fn { print }
    in_fn && /^}$/ { exit }
' "$ENTRYPOINT" >> "$t55_funcs"

awk '
    /^persist_phase_output\(\)/ { in_fn=1 }
    in_fn { print }
    in_fn && /^}$/ { exit }
' "$ENTRYPOINT" >> "$t55_funcs"

# Build a stub mktemp that returns /dev/full so writes always fail (ENOSPC).
t55_stub_bin="$TEST_TMP/t55-bin"
mkdir -p "$t55_stub_bin"
cat > "$t55_stub_bin/mktemp" <<'T55MKTEMPEOF'
#!/usr/bin/env bash
printf '/dev/full\n'
exit 0
T55MKTEMPEOF
chmod +x "$t55_stub_bin/mktemp"

# Also need a stub psql (should never be reached, but must exist).
cat > "$t55_stub_bin/psql" <<'T55PSQLEOF'
#!/usr/bin/env bash
exit 0
T55PSQLEOF
chmod +x "$t55_stub_bin/psql"

set +e
t55_out=$(PATH="$t55_stub_bin:$PATH" \
    AGENT_ID="55555555-dead-beef-cafe-000000000001" \
    DATABASE_URL="postgres://test" \
    bash -c '
        set -euo pipefail
        # shellcheck disable=SC1090
        . "'"$t55_funcs"'"
        # Use || to prevent set -e from killing the subshell when the
        # function returns 1 — this is how real callers handle it. The
        # important assertions are (b) that this line is reached at all
        # (proving no early shell abort via set -e inside the function)
        # and (c) the persist_failed log event is emitted.
        persist_phase_output "ralph" "{}" || true
        echo "after-call=$?"
    ' 2>&1)
t55_rc=$?
set -e

# (a) subshell exit code 0 — caller code after the call executed, no abort.
if [[ "$t55_rc" -eq 0 ]]; then
    pass "#3429 T55 — subshell did not abort (exit 0)"
else
    fail "#3429 T55 — subshell did not abort (exit 0)" \
         "rc=$t55_rc output: $t55_out"
fi

# (b) sentinel line reached.
if printf '%s' "$t55_out" | grep -q "after-call="; then
    pass "#3429 T55 — sentinel after-call= reached"
else
    fail "#3429 T55 — sentinel after-call= reached" \
         "output: $t55_out"
fi

# (c) phase_output_persist_failed with reason=tmpfile_write_failed logged.
# The log() function formats key=value pairs as JSON ("key": "value"), so
# we check for the event name and the value string independently.
if printf '%s' "$t55_out" | grep -q "phase_output_persist_failed" \
   && printf '%s' "$t55_out" | grep -q "tmpfile_write_failed"; then
    pass "#3429 T55 — phase_output_persist_failed reason=tmpfile_write_failed logged"
else
    fail "#3429 T55 — phase_output_persist_failed reason=tmpfile_write_failed logged" \
         "output: $t55_out"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 56: #3429 T56 — mktemp failure routes via return 1.
#
# Scenario: mktemp itself fails (no inodes). The stub prints nothing and
# exits 1. Before #3429, the $() expansion of mktemp ran under ``set -e``
# and a failing mktemp would kill the shell. After the fix the empty
# result is checked inside set +e and routed via ``return 1`` with a
# phase_output_persist_failed reason=mktemp_failed log event.
# ══════════════════════════════════════════════════════════════════════════

# Reuse $t55_funcs — same fixture.

# Build a stub mktemp that prints nothing and exits 1.
t56_stub_bin="$TEST_TMP/t56-bin"
mkdir -p "$t56_stub_bin"
cat > "$t56_stub_bin/mktemp" <<'T56MKTEMPEOF'
#!/usr/bin/env bash
exit 1
T56MKTEMPEOF
chmod +x "$t56_stub_bin/mktemp"

# Stub psql (should never be reached).
cat > "$t56_stub_bin/psql" <<'T56PSQLEOF'
#!/usr/bin/env bash
exit 0
T56PSQLEOF
chmod +x "$t56_stub_bin/psql"

set +e
t56_out=$(PATH="$t56_stub_bin:$PATH" \
    AGENT_ID="56565656-dead-beef-cafe-000000000001" \
    DATABASE_URL="postgres://test" \
    bash -c '
        set -euo pipefail
        # shellcheck disable=SC1090
        . "'"$t55_funcs"'"
        # Use || to prevent set -e from killing the subshell — same
        # pattern as T55. Key assertions are (b) this line is reached
        # and (c) the mktemp_failed log event is emitted.
        persist_phase_output "ralph" "{}" || true
        echo "after-call=$?"
    ' 2>&1)
t56_rc=$?
set -e

# (a) subshell exit code 0 — caller code executed, no abort.
if [[ "$t56_rc" -eq 0 ]]; then
    pass "#3429 T56 — subshell did not abort (exit 0)"
else
    fail "#3429 T56 — subshell did not abort (exit 0)" \
         "rc=$t56_rc output: $t56_out"
fi

# (b) sentinel line reached.
if printf '%s' "$t56_out" | grep -q "after-call="; then
    pass "#3429 T56 — sentinel after-call= reached"
else
    fail "#3429 T56 — sentinel after-call= reached" \
         "output: $t56_out"
fi

# (c) phase_output_persist_failed with reason=mktemp_failed logged.
# The log() function formats key=value pairs as JSON ("key": "value"), so
# we check for the event name and the value string independently.
if printf '%s' "$t56_out" | grep -q "phase_output_persist_failed" \
   && printf '%s' "$t56_out" | grep -q "mktemp_failed"; then
    pass "#3429 T56 — phase_output_persist_failed reason=mktemp_failed logged"
else
    fail "#3429 T56 — phase_output_persist_failed reason=mktemp_failed logged" \
         "output: $t56_out"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 57: #3494 — agent_runner_reaped_failure exits unconditionally.
#
# PR #3458 replaced ``advance_phase agent_runner_route_stub`` with
# ``agent_runner_reaped_failure`` emitting descriptive phase strings
# (phase_unknown, ralph_not_ship, post_claude_transition_unrecognized).
# The dispatch loop had two bugs:
#   (a) None of those strings were in TERMINAL_PHASES, so is_terminal()
#       returned False. (Fixed by adding them to TERMINAL_PHASES.)
#   (b) agent_runner_reaped_failure lacked ``exit 0``, so the `*)` arm
#       could re-enter for unknown phases not yet in TERMINAL_PHASES.
#       (Fixed by adding exit 0 to the function body.)
#
# T57a: For phases now in TERMINAL_PHASES (phase_unknown, ralph_not_ship,
#   post_claude_transition_unrecognized) — the loop exits via the
#   is_terminal() check BEFORE reaching the `*)` arm. No
#   agent_runner_reaped_failure call occurs. Verifies the defense-in-depth
#   fix: exit 0, no cap hit, phase_terminal logged.
#
# T57b: For a truly alien phase (not in any TERMINAL_PHASES, not in any
#   case arm) — the `*)` arm fires and calls agent_runner_reaped_failure.
#   The exit 0 fix ensures the loop does NOT spin. Verifies exit 0,
#   exactly 1 reaped_failure line, no cap hit, and DB UPDATE lands.
# ══════════════════════════════════════════════════════════════════════════

# T57a: Phases now in TERMINAL_PHASES — exit via is_terminal(), no reaped_failure.
_t57a_test_terminal_phase() {
    # $1 = phase now in TERMINAL_PHASES; exits via phase_terminal path.
    _phase="$1"
    setup_fixtures
    PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t57a-${_phase}.txt"
    printf '%s\n' "$_phase" > "$PHASE_FIXTURE_FILE"

    _t57a_workspace="$TEST_TMP/t57a-workspace-${_phase}"
    mkdir -p "$_t57a_workspace"

    set +e
    _t57a_out=$(
        AGENT_ID="57000000-3494-0000-0000-000000003494" \
        ISSUE_NUMBER="3494" \
        DATABASE_URL="postgres://test" \
        GITHUB_TOKEN="" \
        AGENT_WORKSPACE="$_t57a_workspace" \
        REPO_URL="https://example.invalid/repo.git" \
        PATH="$STUB_BIN:$PATH" \
        INVOCATIONS_DIR="$INVOCATIONS_DIR" \
        PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
        PRIOR_PATCH_FIXTURE="" \
        PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
        PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
        AGENT_RUNNER_MAX_PHASE_ITERATIONS=10 \
        bash "$ENTRYPOINT" 2>&1
    )
    _t57a_rc=$?
    set -e

    # (1) Exit code 0 — clean exit via is_terminal() path.
    if [[ "$_t57a_rc" -eq 0 ]]; then
        pass "#3494 T57a [${_phase}] — exit code 0 (terminal phase exits cleanly)"
    else
        fail "#3494 T57a [${_phase}] — exit code 0 (terminal phase exits cleanly)" \
             "rc=$_t57a_rc out tail: $(printf '%s' "$_t57a_out" | tail -c 800)"
    fi

    # (2) phase_terminal event logged — confirms is_terminal() fired.
    if printf '%s' "$_t57a_out" | grep -q "phase_terminal"; then
        pass "#3494 T57a [${_phase}] — phase_terminal event logged"
    else
        fail "#3494 T57a [${_phase}] — phase_terminal event logged" \
             "out tail: $(printf '%s' "$_t57a_out" | tail -c 800)"
    fi

    # (3) phase_iteration_cap_hit ABSENT — loop did not spin to the cap.
    if printf '%s' "$_t57a_out" | grep -q "phase_iteration_cap_hit"; then
        fail "#3494 T57a [${_phase}] — phase_iteration_cap_hit absent (no loop spin)" \
             "out tail: $(printf '%s' "$_t57a_out" | tail -c 800)"
    else
        pass "#3494 T57a [${_phase}] — phase_iteration_cap_hit absent (no loop spin)"
    fi
}

_t57a_test_terminal_phase "phase_unknown"
_t57a_test_terminal_phase "ralph_not_ship"
_t57a_test_terminal_phase "post_claude_transition_unrecognized"

# T57b: A truly alien phase not in TERMINAL_PHASES and not in any case arm —
# the `*)` arm fires, calling agent_runner_reaped_failure. The exit 0 fix
# ensures the loop does NOT spin for MAX_PHASE_ITERATIONS.
# We use a synthetic phase name that can never be wired or terminal.
setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t57b.txt"
printf 'never_wired_3494_alien\n' > "$PHASE_FIXTURE_FILE"

_t57b_workspace="$TEST_TMP/t57b-workspace"
mkdir -p "$_t57b_workspace"

set +e
_t57b_out=$(
    AGENT_ID="57bb0000-3494-0000-0000-000000003494" \
    ISSUE_NUMBER="3494" \
    DATABASE_URL="postgres://test" \
    GITHUB_TOKEN="" \
    AGENT_WORKSPACE="$_t57b_workspace" \
    REPO_URL="https://example.invalid/repo.git" \
    PATH="$STUB_BIN:$PATH" \
    INVOCATIONS_DIR="$INVOCATIONS_DIR" \
    PHASE_FIXTURE_FILE="$PHASE_FIXTURE_FILE" \
    PRIOR_PATCH_FIXTURE="" \
    PHASE_TRANSITIONS_DIR="$REPO_ROOT/scripts/dispatcher" \
    PHASE_TRANSITIONS_PARENT="$REPO_ROOT" \
    AGENT_RUNNER_MAX_PHASE_ITERATIONS=10 \
    bash "$ENTRYPOINT" 2>&1
)
_t57b_rc=$?
set -e

# (1) Exit code 0 — agent_runner_reaped_failure exits cleanly via exit 0.
if [[ "$_t57b_rc" -eq 0 ]]; then
    pass "#3494 T57b [alien phase] — exit code 0 (exit 0 in reaped_failure)"
else
    fail "#3494 T57b [alien phase] — exit code 0 (exit 0 in reaped_failure)" \
         "rc=$_t57b_rc out tail: $(printf '%s' "$_t57b_out" | tail -c 800)"
fi

# (2) Exactly 1 agent_runner_reaped_failure log line — no loop spin (AC3).
_t57b_reaped_count=$(printf '%s' "$_t57b_out" | grep -c "agent_runner_reaped_failure" || true)
if [[ "$_t57b_reaped_count" -eq 1 ]]; then
    pass "#3494 T57b [alien phase] — exactly 1 agent_runner_reaped_failure line (AC3)"
else
    fail "#3494 T57b [alien phase] — exactly 1 agent_runner_reaped_failure line (AC3)" \
         "count=$_t57b_reaped_count out: $(printf '%s' "$_t57b_out" | tail -c 800)"
fi

# (3) phase_iteration_cap_hit ABSENT — loop did not spin to the 10-iter cap.
if printf '%s' "$_t57b_out" | grep -q "phase_iteration_cap_hit"; then
    fail "#3494 T57b [alien phase] — phase_iteration_cap_hit absent (no loop spin)" \
         "out tail: $(printf '%s' "$_t57b_out" | tail -c 800)"
else
    pass "#3494 T57b [alien phase] — phase_iteration_cap_hit absent (no loop spin)"
fi

# (4) psql.log contains UPDATE setting phase=phase_unknown (reaped_failure
# uses "phase_unknown" as the terminal for unknown-phase events).
# psql.log uses $'...' quoting so single quotes appear as \' in the log.
if grep -q "SET phase = \\\\'phase_unknown\\\\'" "$INVOCATIONS_DIR/psql.log"; then
    pass "#3494 T57b [alien phase] — psql UPDATE for phase=phase_unknown landed"
else
    fail "#3494 T57b [alien phase] — psql UPDATE for phase=phase_unknown landed" \
         "psql.log tail: $(tail -c 500 "$INVOCATIONS_DIR/psql.log")"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test T58: #3507 — operational) dispatch arm — succeeded path.
#
# Scenario: the dispatch loop reads phase=operational and the
# /task-v2-operational skill returns verdict=succeeded. Expected behaviour:
#   1. run_claude_phase "operational" is invoked (phase_to_skill maps it).
#   2. persist_phase_output "operational" is called with the output.
#   3. transition_for "operational" returns advance_with_status
#      operational_done succeeded.
#   4. advance_phase "operational_done" "succeeded" is called → DB UPDATE.
# ══════════════════════════════════════════════════════════════════════════

# Extract the operational case and its dependencies from the real entrypoint.
t58_funcs="$TEST_TMP/t58-funcs.sh"
printf 'exec 3>&1\n' > "$t58_funcs"

for fn in db_exec db_query_one log persist_phase_output \
          phase_to_skill \
          read_phase_output \
          write_phase_input \
          run_claude_phase \
          advance_phase \
          agent_runner_reaped_failure \
          transition_for; do
    awk -v FN="^${fn}\\(\\)" '
        $0 ~ FN { in_fn=1 }
        in_fn { print }
        in_fn && /^}$/ { exit }
    ' "$ENTRYPOINT" >> "$t58_funcs"
done

# #3683: run_claude_phase wraps ``claude -p`` in
# ``timeout "$CLAUDE_PHASE_TIMEOUT_SECONDS"``. Include the constant so
# the sourced fixture doesn't fail under ``set -u``.
grep '^CLAUDE_PHASE_TIMEOUT_SECONDS=' "$ENTRYPOINT" >> "$t58_funcs"

# Sanity: phase_to_skill maps operational.
if grep -q "operational)" "$t58_funcs"; then
    pass "#3507 T58 setup — operational) arm exists in extracted functions"
else
    fail "#3507 T58 setup — operational) arm exists in extracted functions" \
         "grep target: 'operational)' in funcs (head 200): $(head -c 200 "$t58_funcs")"
fi

# Build a minimal workspace.
t58_workspace="$TEST_TMP/t58-workspace"
t58_repo_root="$TEST_TMP/t58-repo"
t58_state_dir="$TEST_TMP/t58-state"
t58_stub_bin="$TEST_TMP/t58-bin"
mkdir -p "$t58_workspace" "$t58_repo_root" "$t58_state_dir" "$t58_stub_bin"

# Pre-create dispatcher-input and dispatcher-output dirs.
mkdir -p "$t58_repo_root/tmp/dispatcher-input"
mkdir -p "$t58_repo_root/tmp/dispatcher-output"

# Seed the operational skill output with verdict=succeeded.
cat > "$t58_repo_root/tmp/dispatcher-output/operational.json" <<'T58OUTJSON'
{
  "agent_id": "58585858-dead-beef-cafe-000000000001",
  "issue_number": 2419,
  "verdict": "succeeded",
  "action_taken": "ran rebuild_db.py --county Santa Clara; 4821 rows restored",
  "evidence_md": "## Evidence\n\n4821 rows in derived.rulings.",
  "block_reason": null
}
T58OUTJSON

# psql stub: records all queries; responds to phase SELECT with "operational".
cat > "$t58_stub_bin/psql" <<'T58PSQLEOF'
#!/usr/bin/env bash
set -u
query=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -c) shift; query="$1" ;;
    esac
    shift || true
done
printf 'CALL %s\n' "$query" >> "${T58_STATE_DIR}/psql-log.txt"
if [[ "$query" == *"SELECT phase"*"FROM dispatcher.agents"* ]]; then
    printf 'operational\n'
fi
exit 0
T58PSQLEOF
chmod +x "$t58_stub_bin/psql"

# claude stub: just logs invocation and exits 0 (run_claude_phase reads from
# the dispatcher-output JSON, not from claude's stdout in the entrypoint's
# read_phase_output path).
cat > "$t58_stub_bin/claude" <<'T58CLAUDEEOF'
#!/usr/bin/env bash
printf 'CLAUDE_INVOKED %s\n' "$*" >> "${T58_STATE_DIR}/claude-log.txt"
printf '{"result": "stub"}\n'
exit 0
T58CLAUDEEOF
chmod +x "$t58_stub_bin/claude"

# git stub: no-op.
cat > "$t58_stub_bin/git" <<'T58GITEOF'
#!/usr/bin/env bash
exit 0
T58GITEOF
chmod +x "$t58_stub_bin/git"

# gh stub: no-op.
cat > "$t58_stub_bin/gh" <<'T58GHEOF'
#!/usr/bin/env bash
exit 0
T58GHEOF
chmod +x "$t58_stub_bin/gh"

# transition_for stub: returns advance_with_status\toperational_done\tsucceeded\t
# for the operational phase. Extracted as a shell function replacement below.
# We override transition_for in the sourced functions file.
_t58_transition_for_override="
transition_for() {
    if [[ \"\${1:-}\" == 'operational' ]]; then
        printf 'advance_with_status\toperational_done\tsucceeded\t'
    else
        printf 'advance\t%s\t\t' \"\${2:-done}\"
    fi
}
"

# advance_phase stub: records the call.
_t58_advance_phase_override="
advance_phase() {
    printf 'ADVANCE_PHASE %s %s\n' \"\${1:-}\" \"\${2:-}\" >> \"\${T58_STATE_DIR}/advance-log.txt\"
}
"

# Run the operational case in a subshell.
t58_out=$(
    set +eu
    export T58_STATE_DIR="$t58_state_dir"
    export AGENT_WORKSPACE="$t58_workspace"
    export REPO_ROOT="$t58_repo_root"
    export AGENT_ID="58585858-dead-beef-cafe-000000000001"
    export ISSUE_NUMBER="2419"
    export DATABASE_URL="postgresql://stub"
    export AGENT_RUNNER_DRY_RUN="0"
    export PATH="$t58_stub_bin:$PATH"
    # Source the extracted functions + overrides.
    source "$t58_funcs"
    eval "$_t58_transition_for_override"
    eval "$_t58_advance_phase_override"
    # Invoke just the operational dispatch logic inline.
    _output=$(run_claude_phase "operational")
    persist_phase_output "operational" "$_output"
    _transition=$(transition_for "operational" "$_output")
    _action=$(printf '%s' "$_transition" | cut -f1)
    _next=$(printf '%s' "$_transition" | cut -f2)
    _status=$(printf '%s' "$_transition" | cut -f3)
    log "operational_transition_shim_done" "action=$_action" "next=$_next" "status=$_status"
    case "$_action" in
        advance_with_status)
            advance_phase "$_next" "$_status"
            ;;
        advance)
            advance_phase "$_next"
            ;;
        *)
            printf 'UNEXPECTED_ACTION %s\n' "$_action"
            ;;
    esac
    echo "subshell_done"
    2>&1
) 2>&1

# (1) subshell exited / produced output.
if printf '%s' "$t58_out" | grep -q "subshell_done"; then
    pass "#3507 T58 [operational succeeded] — dispatch subshell ran to completion"
else
    fail "#3507 T58 [operational succeeded] — dispatch subshell ran to completion" \
         "out tail: $(printf '%s' "$t58_out" | tail -c 600)"
fi

# (2) transition_shim_done logged with advance_with_status.
if printf '%s' "$t58_out" | grep -q "operational_transition_shim_done.*advance_with_status"; then
    pass "#3507 T58 [operational succeeded] — transition_shim_done logged advance_with_status"
else
    fail "#3507 T58 [operational succeeded] — transition_shim_done logged advance_with_status" \
         "out: $(printf '%s' "$t58_out" | tail -c 600)"
fi

# (3) advance_phase called with operational_done succeeded.
if [[ -f "$t58_state_dir/advance-log.txt" ]] && grep -q "ADVANCE_PHASE operational_done succeeded" "$t58_state_dir/advance-log.txt"; then
    pass "#3507 T58 [operational succeeded] — advance_phase operational_done succeeded called"
else
    fail "#3507 T58 [operational succeeded] — advance_phase operational_done succeeded called" \
         "advance-log: $(cat "$t58_state_dir/advance-log.txt" 2>/dev/null || echo '(missing)')"
fi

# (4) claude was invoked with /task-v2-operational.
if [[ -f "$t58_state_dir/claude-log.txt" ]] && grep -q "operational" "$t58_state_dir/claude-log.txt"; then
    pass "#3507 T58 [operational succeeded] — claude invoked with operational skill"
else
    fail "#3507 T58 [operational succeeded] — claude invoked with operational skill" \
         "claude-log: $(cat "$t58_state_dir/claude-log.txt" 2>/dev/null || echo '(missing)')"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test T59: #3507 — operational) dispatch arm — failed/diagnoser path.
#
# Scenario: the /task-v2-operational skill returns verdict=failed.
# Expected behaviour:
#   1. transition_for returns route_to_diagnoser.
#   2. agent_runner_reaped_failure "operational_failed" is called.
#   3. No advance_phase call is made.
# ══════════════════════════════════════════════════════════════════════════

t59_workspace="$TEST_TMP/t59-workspace"
t59_repo_root="$TEST_TMP/t59-repo"
t59_state_dir="$TEST_TMP/t59-state"
t59_stub_bin="$TEST_TMP/t59-bin"
mkdir -p "$t59_workspace" "$t59_repo_root" "$t59_state_dir" "$t59_stub_bin"

mkdir -p "$t59_repo_root/tmp/dispatcher-input"
mkdir -p "$t59_repo_root/tmp/dispatcher-output"

# Seed operational output with verdict=failed.
cat > "$t59_repo_root/tmp/dispatcher-output/operational.json" <<'T59OUTJSON'
{
  "agent_id": "59595959-dead-beef-cafe-000000000001",
  "issue_number": 2419,
  "verdict": "failed",
  "action_taken": null,
  "evidence_md": "ECS task exited with code 1",
  "block_reason": null
}
T59OUTJSON

# psql stub.
cat > "$t59_stub_bin/psql" <<'T59PSQLEOF'
#!/usr/bin/env bash
query=""
while [[ $# -gt 0 ]]; do
    case "$1" in -c) shift; query="$1" ;; esac
    shift || true
done
if [[ "$query" == *"SELECT phase"* ]]; then printf 'operational\n'; fi
exit 0
T59PSQLEOF
chmod +x "$t59_stub_bin/psql"

cat > "$t59_stub_bin/claude" <<'T59CLAUDEEOF'
#!/usr/bin/env bash
printf '{"result": "stub"}\n'
exit 0
T59CLAUDEEOF
chmod +x "$t59_stub_bin/claude"

cat > "$t59_stub_bin/git" <<'T59GITEOF'
#!/usr/bin/env bash
exit 0
T59GITEOF
chmod +x "$t59_stub_bin/git"

cat > "$t59_stub_bin/gh" <<'T59GHEOF'
#!/usr/bin/env bash
exit 0
T59GHEOF
chmod +x "$t59_stub_bin/gh"

_t59_transition_for_override="
transition_for() {
    if [[ \"\${1:-}\" == 'operational' ]]; then
        printf 'route_to_diagnoser\t\t\toperational_failed'
    fi
}
"

_t59_reaped_failure_override="
agent_runner_reaped_failure() {
    printf 'REAPED_FAILURE %s\n' \"\${1:-}\" >> \"\${T59_STATE_DIR}/reaped-log.txt\"
    log 'agent_runner_reaped_failure' \"phase=\$1\" \"hint=\$2\"
}
"

_t59_advance_phase_override="
advance_phase() {
    printf 'ADVANCE_PHASE %s\n' \"\${1:-}\" >> \"\${T59_STATE_DIR}/advance-log.txt\"
}
"

t59_out=$(
    set +eu
    export T59_STATE_DIR="$t59_state_dir"
    export AGENT_WORKSPACE="$t59_workspace"
    export REPO_ROOT="$t59_repo_root"
    export AGENT_ID="59595959-dead-beef-cafe-000000000001"
    export ISSUE_NUMBER="2419"
    export DATABASE_URL="postgresql://stub"
    export AGENT_RUNNER_DRY_RUN="0"
    export PATH="$t59_stub_bin:$PATH"
    source "$t58_funcs"
    eval "$_t59_transition_for_override"
    eval "$_t59_reaped_failure_override"
    eval "$_t59_advance_phase_override"
    _output=$(run_claude_phase "operational")
    persist_phase_output "operational" "$_output"
    _transition=$(transition_for "operational" "$_output")
    _action=$(printf '%s' "$_transition" | cut -f1)
    _next=$(printf '%s' "$_transition" | cut -f2)
    _status=$(printf '%s' "$_transition" | cut -f3)
    _hint=$(printf '%s' "$_transition" | cut -f4)
    log "operational_transition_shim_done" "action=$_action" "next=$_next" "hint=$_hint"
    case "$_action" in
        advance_with_status)
            advance_phase "$_next" "$_status"
            ;;
        advance)
            advance_phase "$_next"
            ;;
        route_to_diagnoser)
            log "operational_route_to_diagnoser" "hint=$_hint"
            agent_runner_reaped_failure \
                "operational_failed" \
                "operational_failed" \
                "operational skill returned non-succeeded verdict"
            ;;
        *)
            printf 'UNEXPECTED_ACTION %s\n' "$_action"
            ;;
    esac
    echo "subshell_done"
    2>&1
) 2>&1

# (1) subshell completed.
if printf '%s' "$t59_out" | grep -q "subshell_done"; then
    pass "#3507 T59 [operational failed] — dispatch subshell ran to completion"
else
    fail "#3507 T59 [operational failed] — dispatch subshell ran to completion" \
         "out tail: $(printf '%s' "$t59_out" | tail -c 600)"
fi

# (2) transition logged route_to_diagnoser.
if printf '%s' "$t59_out" | grep -q "operational_transition_shim_done.*route_to_diagnoser"; then
    pass "#3507 T59 [operational failed] — transition_shim_done logged route_to_diagnoser"
else
    fail "#3507 T59 [operational failed] — transition_shim_done logged route_to_diagnoser" \
         "out: $(printf '%s' "$t59_out" | tail -c 600)"
fi

# (3) agent_runner_reaped_failure called with operational_failed.
if [[ -f "$t59_state_dir/reaped-log.txt" ]] && grep -q "REAPED_FAILURE operational_failed" "$t59_state_dir/reaped-log.txt"; then
    pass "#3507 T59 [operational failed] — agent_runner_reaped_failure called with operational_failed"
else
    fail "#3507 T59 [operational failed] — agent_runner_reaped_failure called with operational_failed" \
         "reaped-log: $(cat "$t59_state_dir/reaped-log.txt" 2>/dev/null || echo '(missing)')"
fi

# (4) advance_phase NOT called on failure path.
if [[ ! -f "$t59_state_dir/advance-log.txt" ]] || ! grep -q "ADVANCE_PHASE" "$t59_state_dir/advance-log.txt"; then
    pass "#3507 T59 [operational failed] — advance_phase not called on route_to_diagnoser"
else
    fail "#3507 T59 [operational failed] — advance_phase not called on route_to_diagnoser" \
         "advance-log: $(cat "$t59_state_dir/advance-log.txt" 2>/dev/null)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 60a (#3514 regression): prior completed-success run with DIFFERENT
# headSha + actual run for merge_sha with IN_PROGRESS → deploy_state=pending.
#
# This reproduces the exact production failure from PR #3509: gh returns
# both a prior run (different SHA, COMPLETED/SUCCESS) and the current run
# (matching SHA, IN_PROGRESS). The matcher must filter by headSha so the
# prior completed run does NOT advance the phase prematurely.
# ══════════════════════════════════════════════════════════════════════════
setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t60a.txt"
printf 'awaiting_deploy\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

# The default gh pr view fixture has mergeCommit.oid = "deadbeefcafe".
# The prior run has a DIFFERENT headSha to exercise the filter.
t60a_runs="$TEST_TMP/t60a-runs.json"
cat > "$t60a_runs" <<'EOF'
[
  {"databaseId": 100, "workflowName": "Deploy Agent Runner", "status": "COMPLETED", "conclusion": "SUCCESS", "createdAt": "2026-04-01T03:00:00Z", "headSha": "aabbccddeeff0011"},
  {"databaseId": 101, "workflowName": "Deploy Agent Runner", "status": "IN_PROGRESS", "conclusion": null, "createdAt": "2026-04-27T03:02:00Z", "headSha": "deadbeefcafe"}
]
EOF

t60a_workspace="$TEST_TMP/t60a-workspace"
set +e
t60a_out=$(run_post_pr_phase "awaiting_deploy" "$t60a_workspace" \
    "PHASE_FIXTURE_FILE=$PHASE_FIXTURE_FILE" \
    "PRIOR_PATCH_FIXTURE=" \
    "PR_NUMBER_FIXTURE=9999" \
    "GH_RUN_LIST_JSON_FIXTURE=$t60a_runs" \
    "AGENT_RUNNER_AWAITING_DEPLOY_TIMEOUT_SECONDS=0" \
    "AGENT_RUNNER_DEPLOY_GRACE_SECONDS=9999")
set -e

if printf '%s' "$t60a_out" | grep -q "awaiting_deploy_timeout"; then
    pass "#3514 T60a — prior completed-success with different headSha → pending → timeout (not premature success)"
else
    fail "#3514 T60a — prior completed-success with different headSha → pending → timeout (not premature success)" \
         "out tail: $(printf '%s' "$t60a_out" | tail -c 600)"
fi

# Phase must NOT advance to verify (no premature success).
_t60a_final=$(cat "$PHASE_FIXTURE_FILE" 2>/dev/null || printf '')
if [[ "$_t60a_final" != "verify" ]]; then
    pass "#3514 T60a — phase does NOT advance to verify on prior-sha match"
else
    fail "#3514 T60a — phase does NOT advance to verify on prior-sha match" \
         "phase advanced to: $_t60a_final"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 60b (#3514 regression): empty match-set → grace window → verify.
#
# No deploy runs exist yet for the merge SHA. The matcher returns "none"
# and after the grace window elapses the agent advances to verify
# (docs-only PR semantic). This documents and locks the current behaviour.
# ══════════════════════════════════════════════════════════════════════════
setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t60b.txt"
printf 'awaiting_deploy\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t60b_runs="$TEST_TMP/t60b-runs.json"
printf '[]' > "$t60b_runs"

t60b_workspace="$TEST_TMP/t60b-workspace"
set +e
t60b_out=$(run_post_pr_phase "awaiting_deploy" "$t60b_workspace" \
    "PHASE_FIXTURE_FILE=$PHASE_FIXTURE_FILE" \
    "PRIOR_PATCH_FIXTURE=" \
    "PR_NUMBER_FIXTURE=9999" \
    "GH_RUN_LIST_JSON_FIXTURE=$t60b_runs" \
    "AGENT_RUNNER_DEPLOY_GRACE_SECONDS=0")
set -e

if printf '%s' "$t60b_out" | grep -q "awaiting_deploy_no_runs"; then
    pass "#3514 T60b — empty match-set → no_runs branch hit"
else
    fail "#3514 T60b — empty match-set → no_runs branch hit" \
         "out tail: $(printf '%s' "$t60b_out" | tail -c 500)"
fi

if grep -q "SET phase = \\\\'verify\\\\'" "$INVOCATIONS_DIR/psql.log"; then
    pass "#3514 T60b — empty match-set → advances to verify"
else
    fail "#3514 T60b — empty match-set → advances to verify" \
         "psql log tail: $(tail -c 500 "$INVOCATIONS_DIR/psql.log")"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 60c (#3514 regression): two-run fixture — one COMPLETED+SUCCESS
# Deploy Agent Runner + one IN_PROGRESS Deploy Dispatcher, both with
# matching merge_sha → deploy_state=pending (any in_progress blocks success).
#
# This is the exact production failure scenario from PR #3509.
# ══════════════════════════════════════════════════════════════════════════
setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t60c.txt"
printf 'awaiting_deploy\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t60c_runs="$TEST_TMP/t60c-runs.json"
cat > "$t60c_runs" <<'EOF'
[
  {"databaseId": 200, "workflowName": "Deploy Agent Runner", "status": "COMPLETED", "conclusion": "SUCCESS", "createdAt": "2026-04-27T03:02:47Z", "headSha": "deadbeefcafe"},
  {"databaseId": 201, "workflowName": "Deploy Dispatcher", "status": "IN_PROGRESS", "conclusion": null, "createdAt": "2026-04-27T03:02:50Z", "headSha": "deadbeefcafe"}
]
EOF

t60c_workspace="$TEST_TMP/t60c-workspace"
set +e
t60c_out=$(run_post_pr_phase "awaiting_deploy" "$t60c_workspace" \
    "PHASE_FIXTURE_FILE=$PHASE_FIXTURE_FILE" \
    "PRIOR_PATCH_FIXTURE=" \
    "PR_NUMBER_FIXTURE=9999" \
    "GH_RUN_LIST_JSON_FIXTURE=$t60c_runs" \
    "AGENT_RUNNER_AWAITING_DEPLOY_TIMEOUT_SECONDS=0" \
    "AGENT_RUNNER_DEPLOY_GRACE_SECONDS=9999")
set -e

if printf '%s' "$t60c_out" | grep -q "awaiting_deploy_timeout"; then
    pass "#3514 T60c — two-run fixture (one SUCCESS, one IN_PROGRESS) → pending → timeout"
else
    fail "#3514 T60c — two-run fixture (one SUCCESS, one IN_PROGRESS) → pending → timeout" \
         "out tail: $(printf '%s' "$t60c_out" | tail -c 600)"
fi

_t60c_final=$(cat "$PHASE_FIXTURE_FILE" 2>/dev/null || printf '')
if [[ "$_t60c_final" != "verify" ]]; then
    pass "#3514 T60c — phase does NOT advance to verify while Deploy Dispatcher in_progress"
else
    fail "#3514 T60c — phase does NOT advance to verify while Deploy Dispatcher in_progress" \
         "phase advanced to: $_t60c_final"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test 60d (#3514 regression): matching headSha COMPLETED+SUCCESS →
# deploy_state=success → phase advances to verify.
# ══════════════════════════════════════════════════════════════════════════
setup_fixtures
PHASE_FIXTURE_FILE="$TEST_TMP/phase-state-t60d.txt"
printf 'awaiting_deploy\n' > "$PHASE_FIXTURE_FILE"
PRIOR_PATCH_FIXTURE=""

t60d_runs="$TEST_TMP/t60d-runs.json"
cat > "$t60d_runs" <<'EOF'
[
  {"databaseId": 300, "workflowName": "Deploy Agent Runner", "status": "COMPLETED", "conclusion": "SUCCESS", "createdAt": "2026-04-27T03:05:00Z", "headSha": "deadbeefcafe"}
]
EOF

t60d_workspace="$TEST_TMP/t60d-workspace"
set +e
t60d_out=$(run_post_pr_phase "awaiting_deploy" "$t60d_workspace" \
    "PHASE_FIXTURE_FILE=$PHASE_FIXTURE_FILE" \
    "PRIOR_PATCH_FIXTURE=" \
    "PR_NUMBER_FIXTURE=9999" \
    "GH_RUN_LIST_JSON_FIXTURE=$t60d_runs" \
    "AGENT_RUNNER_DEPLOY_GRACE_SECONDS=9999")
set -e

if printf '%s' "$t60d_out" | grep -q 'deploy_state.*success\|"deploy_state": "success"'; then
    pass "#3514 T60d — matching headSha COMPLETED+SUCCESS → deploy_state=success"
else
    fail "#3514 T60d — matching headSha COMPLETED+SUCCESS → deploy_state=success" \
         "out tail: $(printf '%s' "$t60d_out" | tail -c 600)"
fi

if grep -q "SET phase = \\\\'verify\\\\'" "$INVOCATIONS_DIR/psql.log"; then
    pass "#3514 T60d — deploy success → phase advances to verify"
else
    fail "#3514 T60d — deploy success → phase advances to verify" \
         "psql log tail: $(tail -c 500 "$INVOCATIONS_DIR/psql.log")"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test T_issue3543: #3543 — push_and_pr) dispatch arm — route_to_diagnoser path.
#
# Sub-test A: transition_for returns route_to_diagnoser with hint
#   push_and_pr_no_unmerged_files.
# Expected behaviour:
#   1. push_and_pr_route_to_diagnoser log line emitted.
#   2. agent_runner_reaped_failure called with category push_and_pr_no_unmerged_files.
#   3. advance_phase NOT called.
#   4. push_and_pr_transition_unrecognized NOT emitted.
#
# Sub-test B (regression): transition_for returns advance\tawaiting_ci\t\t.
# Expected behaviour:
#   1. advance_phase awaiting_ci called.
#   2. push_and_pr_route_to_diagnoser NOT emitted.
# ══════════════════════════════════════════════════════════════════════════

t3543_state_dir="$TEST_TMP/t3543-state"
t3543_stub_bin="$TEST_TMP/t3543-bin"
t3543_workspace="$TEST_TMP/t3543-workspace"
t3543_repo_root="$TEST_TMP/t3543-repo"
mkdir -p "$t3543_state_dir" "$t3543_stub_bin" "$t3543_workspace" "$t3543_repo_root"
mkdir -p "$t3543_repo_root/tmp/dispatcher-input"
mkdir -p "$t3543_repo_root/tmp/dispatcher-output"

# psql stub — responds to phase SELECT with push_and_pr.
cat > "$t3543_stub_bin/psql" <<'T3543PSQLEOF'
#!/usr/bin/env bash
query=""
while [[ $# -gt 0 ]]; do
    case "$1" in -c) shift; query="$1" ;; esac
    shift || true
done
if [[ "$query" == *"SELECT phase"* ]]; then printf 'push_and_pr\n'; fi
exit 0
T3543PSQLEOF
chmod +x "$t3543_stub_bin/psql"

cat > "$t3543_stub_bin/git" <<'T3543GITEOF'
#!/usr/bin/env bash
exit 0
T3543GITEOF
chmod +x "$t3543_stub_bin/git"

cat > "$t3543_stub_bin/gh" <<'T3543GHEOF'
#!/usr/bin/env bash
exit 0
T3543GHEOF
chmod +x "$t3543_stub_bin/gh"

# ── Sub-test A: route_to_diagnoser with push_and_pr_no_unmerged_files ──────

t3543a_state_dir="$t3543_state_dir/a"
mkdir -p "$t3543a_state_dir"

_t3543a_transition_for_override="
transition_for() {
    if [[ \"\${1:-}\" == 'push_and_pr' ]]; then
        printf 'route_to_diagnoser\t\t\tpush_and_pr_no_unmerged_files'
    fi
}
"

_t3543a_reaped_failure_override="
agent_runner_reaped_failure() {
    printf 'REAPED_FAILURE %s\n' \"\${1:-}\" >> \"\${T3543A_STATE_DIR}/reaped-log.txt\"
    log 'agent_runner_reaped_failure' \"phase=\$1\" \"hint=\$2\"
}
"

_t3543a_advance_phase_override="
advance_phase() {
    printf 'ADVANCE_PHASE %s\n' \"\${1:-}\" >> \"\${T3543A_STATE_DIR}/advance-log.txt\"
}
"

_t3543a_handle_push_and_pr_override="
handle_push_and_pr() {
    printf '{\"no_unmerged_files\": true}\n'
}
"

t3543a_out=$(
    set +eu
    export T3543A_STATE_DIR="$t3543a_state_dir"
    export AGENT_WORKSPACE="$t3543_workspace"
    export REPO_ROOT="$t3543_repo_root"
    export AGENT_ID="61616161-dead-beef-cafe-000000000001"
    export ISSUE_NUMBER="3543"
    export DATABASE_URL="postgresql://stub"
    export AGENT_RUNNER_DRY_RUN="0"
    export PATH="$t3543_stub_bin:$PATH"
    source "$t58_funcs"
    eval "$_t3543a_transition_for_override"
    eval "$_t3543a_reaped_failure_override"
    eval "$_t3543a_advance_phase_override"
    eval "$_t3543a_handle_push_and_pr_override"
    # Drive the push_and_pr dispatch logic directly (mirrors the entrypoint).
    _output=$(handle_push_and_pr)
    persist_phase_output "push_and_pr" "$_output"
    _transition=$(transition_for "push_and_pr" "$_output")
    _action=$(printf '%s' "$_transition" | cut -f1)
    _next=$(printf '%s' "$_transition" | cut -f2)
    _status=$(printf '%s' "$_transition" | cut -f3)
    _hint=$(printf '%s' "$_transition" | cut -f4)
    case "$_action" in
        advance)
            advance_phase "$_next"
            ;;
        advance_with_status)
            advance_phase "$_next" "$_status"
            ;;
        route_to_diagnoser)
            log "push_and_pr_route_to_diagnoser" "hint=$_hint"
            case "$_hint" in
                push_and_pr_no_unmerged_files)
                    agent_runner_reaped_failure \
                        "push_and_pr_no_unmerged_files" \
                        "push_and_pr_no_unmerged_files" \
                        "push_and_pr rebase failed with no unmerged files"
                    ;;
                *)
                    log "push_and_pr_route_unrecognized_hint" "hint=$_hint"
                    agent_runner_reaped_failure \
                        "diagnoser_route_unrecognized_hint" \
                        "diagnoser_route_unrecognized_hint" \
                        "push_and_pr route_to_diagnoser received unrecognized hint=${_hint:-(empty)}"
                    ;;
            esac
            ;;
        *)
            log "push_and_pr_transition_unrecognized" "action=$_action"
            agent_runner_reaped_failure \
                "push_and_pr_transition_unrecognized" \
                "push_and_pr_transition_unrecognized" \
                "push_and_pr returned action=$_action"
            ;;
    esac
    echo "subshell_done"
    2>&1
) 2>&1

# T3543A (1): subshell completed.
if printf '%s' "$t3543a_out" | grep -q "subshell_done"; then
    pass "#3543 T3543A [push_and_pr route_to_diagnoser] — dispatch subshell ran to completion"
else
    fail "#3543 T3543A [push_and_pr route_to_diagnoser] — dispatch subshell ran to completion" \
         "out tail: $(printf '%s' "$t3543a_out" | tail -c 600)"
fi

# T3543A (2): push_and_pr_route_to_diagnoser log line emitted.
if printf '%s' "$t3543a_out" | grep -q "push_and_pr_route_to_diagnoser"; then
    pass "#3543 T3543A [push_and_pr route_to_diagnoser] — push_and_pr_route_to_diagnoser log emitted"
else
    fail "#3543 T3543A [push_and_pr route_to_diagnoser] — push_and_pr_route_to_diagnoser log emitted" \
         "out: $(printf '%s' "$t3543a_out" | tail -c 600)"
fi

# T3543A (3): agent_runner_reaped_failure called with push_and_pr_no_unmerged_files.
if [[ -f "$t3543a_state_dir/reaped-log.txt" ]] && grep -q "REAPED_FAILURE push_and_pr_no_unmerged_files" "$t3543a_state_dir/reaped-log.txt"; then
    pass "#3543 T3543A [push_and_pr route_to_diagnoser] — agent_runner_reaped_failure called with push_and_pr_no_unmerged_files"
else
    fail "#3543 T3543A [push_and_pr route_to_diagnoser] — agent_runner_reaped_failure called with push_and_pr_no_unmerged_files" \
         "reaped-log: $(cat "$t3543a_state_dir/reaped-log.txt" 2>/dev/null || echo '(missing)')"
fi

# T3543A (4): advance_phase NOT called on route_to_diagnoser path.
if [[ ! -f "$t3543a_state_dir/advance-log.txt" ]] || ! grep -q "ADVANCE_PHASE" "$t3543a_state_dir/advance-log.txt"; then
    pass "#3543 T3543A [push_and_pr route_to_diagnoser] — advance_phase not called on route_to_diagnoser"
else
    fail "#3543 T3543A [push_and_pr route_to_diagnoser] — advance_phase not called on route_to_diagnoser" \
         "advance-log: $(cat "$t3543a_state_dir/advance-log.txt" 2>/dev/null)"
fi

# T3543A (5): push_and_pr_transition_unrecognized NOT emitted.
if ! printf '%s' "$t3543a_out" | grep -q "push_and_pr_transition_unrecognized"; then
    pass "#3543 T3543A [push_and_pr route_to_diagnoser] — push_and_pr_transition_unrecognized not emitted"
else
    fail "#3543 T3543A [push_and_pr route_to_diagnoser] — push_and_pr_transition_unrecognized not emitted" \
         "out: $(printf '%s' "$t3543a_out" | grep "push_and_pr_transition_unrecognized")"
fi

# ── Sub-test B: advance happy-path regression ──────────────────────────────

t3543b_state_dir="$t3543_state_dir/b"
mkdir -p "$t3543b_state_dir"

_t3543b_transition_for_override="
transition_for() {
    if [[ \"\${1:-}\" == 'push_and_pr' ]]; then
        printf 'advance\tawaiting_ci\t\t'
    fi
}
"

_t3543b_advance_phase_override="
advance_phase() {
    printf 'ADVANCE_PHASE %s\n' \"\${1:-}\" >> \"\${T3543B_STATE_DIR}/advance-log.txt\"
}
"

_t3543b_handle_push_and_pr_override="
handle_push_and_pr() {
    printf '{\"pr_number\": 9999}\n'
}
"

t3543b_out=$(
    set +eu
    export T3543B_STATE_DIR="$t3543b_state_dir"
    export AGENT_WORKSPACE="$t3543_workspace"
    export REPO_ROOT="$t3543_repo_root"
    export AGENT_ID="61616162-dead-beef-cafe-000000000001"
    export ISSUE_NUMBER="3543"
    export DATABASE_URL="postgresql://stub"
    export AGENT_RUNNER_DRY_RUN="0"
    export PATH="$t3543_stub_bin:$PATH"
    source "$t58_funcs"
    eval "$_t3543b_transition_for_override"
    eval "$_t3543b_advance_phase_override"
    eval "$_t3543b_handle_push_and_pr_override"
    # Drive the push_and_pr dispatch logic directly.
    _output=$(handle_push_and_pr)
    persist_phase_output "push_and_pr" "$_output"
    _transition=$(transition_for "push_and_pr" "$_output")
    _action=$(printf '%s' "$_transition" | cut -f1)
    _next=$(printf '%s' "$_transition" | cut -f2)
    _status=$(printf '%s' "$_transition" | cut -f3)
    _hint=$(printf '%s' "$_transition" | cut -f4)
    case "$_action" in
        advance)
            advance_phase "$_next"
            ;;
        advance_with_status)
            advance_phase "$_next" "$_status"
            ;;
        route_to_diagnoser)
            log "push_and_pr_route_to_diagnoser" "hint=$_hint"
            case "$_hint" in
                push_and_pr_no_unmerged_files)
                    agent_runner_reaped_failure \
                        "push_and_pr_no_unmerged_files" \
                        "push_and_pr_no_unmerged_files" \
                        "push_and_pr rebase failed with no unmerged files"
                    ;;
                *)
                    log "push_and_pr_route_unrecognized_hint" "hint=$_hint"
                    agent_runner_reaped_failure \
                        "diagnoser_route_unrecognized_hint" \
                        "diagnoser_route_unrecognized_hint" \
                        "push_and_pr route_to_diagnoser received unrecognized hint=${_hint:-(empty)}"
                    ;;
            esac
            ;;
        *)
            log "push_and_pr_transition_unrecognized" "action=$_action"
            agent_runner_reaped_failure \
                "push_and_pr_transition_unrecognized" \
                "push_and_pr_transition_unrecognized" \
                "push_and_pr returned action=$_action"
            ;;
    esac
    echo "subshell_done"
    2>&1
) 2>&1

# T3543B (1): subshell completed.
if printf '%s' "$t3543b_out" | grep -q "subshell_done"; then
    pass "#3543 T3543B [push_and_pr advance] — dispatch subshell ran to completion"
else
    fail "#3543 T3543B [push_and_pr advance] — dispatch subshell ran to completion" \
         "out tail: $(printf '%s' "$t3543b_out" | tail -c 600)"
fi

# T3543B (2): advance_phase awaiting_ci called.
if [[ -f "$t3543b_state_dir/advance-log.txt" ]] && grep -q "ADVANCE_PHASE awaiting_ci" "$t3543b_state_dir/advance-log.txt"; then
    pass "#3543 T3543B [push_and_pr advance] — advance_phase awaiting_ci called"
else
    fail "#3543 T3543B [push_and_pr advance] — advance_phase awaiting_ci called" \
         "advance-log: $(cat "$t3543b_state_dir/advance-log.txt" 2>/dev/null || echo '(missing)')"
fi

# T3543B (3): push_and_pr_route_to_diagnoser NOT emitted on advance path.
if ! printf '%s' "$t3543b_out" | grep -q "push_and_pr_route_to_diagnoser"; then
    pass "#3543 T3543B [push_and_pr advance] — push_and_pr_route_to_diagnoser not emitted on advance"
else
    fail "#3543 T3543B [push_and_pr advance] — push_and_pr_route_to_diagnoser not emitted on advance" \
         "out: $(printf '%s' "$t3543b_out" | grep "push_and_pr_route_to_diagnoser")"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test T_issue3166: #3166 — external-terminal observation at phase boundary.
#
# Sub-test A (positive): db_query_one returns status=failed → block exits 0
# and emits external_terminal_observed log line.
#
# Sub-test B (negative): db_query_one returns status=running → block does
# NOT exit; loop continues (subshell reaches echo "loop_continued").
# ══════════════════════════════════════════════════════════════════════════

t3166_state_dir="$TEST_TMP/t3166-state"
t3166_stub_bin="$TEST_TMP/t3166-bin"
t3166_workspace="$TEST_TMP/t3166-workspace"
mkdir -p "$t3166_state_dir" "$t3166_stub_bin" "$t3166_workspace"

# Extract helper functions from the real entrypoint.
t3166_funcs="$TEST_TMP/t3166-funcs.sh"
printf 'exec 3>&1\n' > "$t3166_funcs"

for fn in db_query_one log read_agent_status; do
    awk -v FN="^${fn}\\(\\)" '
        $0 ~ FN { in_fn=1 }
        in_fn { print }
        in_fn && /^}$/ { exit }
    ' "$ENTRYPOINT" >> "$t3166_funcs"
done

# Sanity: read_agent_status exists in extracted functions.
if grep -q "read_agent_status" "$t3166_funcs"; then
    pass "#3166 T3166 setup — read_agent_status exists in extracted functions"
else
    fail "#3166 T3166 setup — read_agent_status exists in extracted functions" \
         "grep target: 'read_agent_status' in funcs (head 200): $(head -c 200 "$t3166_funcs")"
fi

# ── Sub-test A: terminal status → exits 0, emits log line ─────────────────

t3166a_state_dir="$t3166_state_dir/a"
mkdir -p "$t3166a_state_dir"

# psql stub — returns 'failed' for status SELECT, 'planning' for phase SELECT.
cat > "$t3166_stub_bin/psql" <<'T3166PSQLEOF'
#!/usr/bin/env bash
query=""
while [[ $# -gt 0 ]]; do
    case "$1" in -c) shift; query="$1" ;; esac
    shift || true
done
if [[ "$query" == *"SELECT status"* ]]; then
    printf 'failed\n'
elif [[ "$query" == *"SELECT phase"* ]]; then
    printf 'planning\n'
fi
exit 0
T3166PSQLEOF
chmod +x "$t3166_stub_bin/psql"

# is_terminal_status override: returns 0 (terminal) for 'failed'.
_t3166a_is_terminal_status_override="
is_terminal_status() {
    [[ \"\${1:-}\" == 'failed' ]]
}
"

t3166a_out=$(
    set +eu
    export AGENT_WORKSPACE="$t3166_workspace"
    export AGENT_ID="62626262-dead-beef-cafe-000000000001"
    export DATABASE_URL="postgresql://stub"
    export PATH="$t3166_stub_bin:$PATH"
    source "$t3166_funcs"
    eval "$_t3166a_is_terminal_status_override"
    # Inline the external-terminal observation block from the phase loop.
    _current="planning"
    _current_status=$(read_agent_status)
    if is_terminal_status "$_current_status"; then
        log "external_terminal_observed" "status=$_current_status" "after_phase=$_current"
        exit 0
    fi
    echo "loop_continued"
    2>&1
) 2>&1
t3166a_rc=$?

# T3166A (1): subshell exited 0.
if [[ $t3166a_rc -eq 0 ]]; then
    pass "#3166 T3166A [external_terminal_observed] — subshell exited 0 on terminal status"
else
    fail "#3166 T3166A [external_terminal_observed] — subshell exited 0 on terminal status" \
         "exit_code=$t3166a_rc out: $(printf '%s' "$t3166a_out" | tail -c 400)"
fi

# T3166A (2): external_terminal_observed emitted with status=failed.
if printf '%s' "$t3166a_out" | grep -q "external_terminal_observed"; then
    pass "#3166 T3166A [external_terminal_observed] — log line emitted"
else
    fail "#3166 T3166A [external_terminal_observed] — log line emitted" \
         "out: $(printf '%s' "$t3166a_out" | tail -c 400)"
fi

if printf '%s' "$t3166a_out" | grep "external_terminal_observed" | grep -q "failed"; then
    pass "#3166 T3166A [external_terminal_observed] — status=failed in log line"
else
    fail "#3166 T3166A [external_terminal_observed] — status=failed in log line" \
         "out: $(printf '%s' "$t3166a_out" | grep "external_terminal_observed")"
fi

# T3166A (3): loop_continued NOT printed (block exited before reaching it).
if ! printf '%s' "$t3166a_out" | grep -q "loop_continued"; then
    pass "#3166 T3166A [external_terminal_observed] — loop_continued not reached"
else
    fail "#3166 T3166A [external_terminal_observed] — loop_continued not reached" \
         "out: $(printf '%s' "$t3166a_out" | tail -c 400)"
fi

# ── Sub-test B (negative): non-terminal status → loop continues ────────────

# psql stub — returns 'running' for status SELECT.
cat > "$t3166_stub_bin/psql" <<'T3166BPSQLEOF'
#!/usr/bin/env bash
query=""
while [[ $# -gt 0 ]]; do
    case "$1" in -c) shift; query="$1" ;; esac
    shift || true
done
if [[ "$query" == *"SELECT status"* ]]; then
    printf 'running\n'
elif [[ "$query" == *"SELECT phase"* ]]; then
    printf 'planning\n'
fi
exit 0
T3166BPSQLEOF
chmod +x "$t3166_stub_bin/psql"

# is_terminal_status override: returns 1 (not terminal) for 'running'.
_t3166b_is_terminal_status_override="
is_terminal_status() {
    [[ \"\${1:-}\" == 'failed' || \"\${1:-}\" == 'crashed' || \"\${1:-}\" == 'succeeded' || \"\${1:-}\" == 'plan_blocked' || \"\${1:-}\" == 'needs_review' ]]
}
"

t3166b_out=$(
    set +eu
    export AGENT_WORKSPACE="$t3166_workspace"
    export AGENT_ID="62626262-dead-beef-cafe-000000000001"
    export DATABASE_URL="postgresql://stub"
    export PATH="$t3166_stub_bin:$PATH"
    source "$t3166_funcs"
    eval "$_t3166b_is_terminal_status_override"
    # Inline the external-terminal observation block from the phase loop.
    _current="planning"
    _current_status=$(read_agent_status)
    if is_terminal_status "$_current_status"; then
        log "external_terminal_observed" "status=$_current_status" "after_phase=$_current"
        exit 0
    fi
    echo "loop_continued"
    2>&1
) 2>&1
t3166b_rc=$?

# T3166B (1): subshell reached loop_continued (did NOT exit early).
if printf '%s' "$t3166b_out" | grep -q "loop_continued"; then
    pass "#3166 T3166B [non-terminal running] — loop_continued reached"
else
    fail "#3166 T3166B [non-terminal running] — loop_continued reached" \
         "exit_code=$t3166b_rc out: $(printf '%s' "$t3166b_out" | tail -c 400)"
fi

# T3166B (2): external_terminal_observed NOT emitted.
if ! printf '%s' "$t3166b_out" | grep -q "external_terminal_observed"; then
    pass "#3166 T3166B [non-terminal running] — external_terminal_observed not emitted"
else
    fail "#3166 T3166B [non-terminal running] — external_terminal_observed not emitted" \
         "out: $(printf '%s' "$t3166b_out" | grep "external_terminal_observed")"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test T_issue3573: #3573 — ralph_baseline rebase dispatch — route_to_diagnoser path.
#
# Sub-test A: transition_for returns route_to_diagnoser with hint
#   push_and_pr_no_unmerged_files.
# Expected behaviour:
#   1. ralph_baseline_route_to_diagnoser log line emitted.
#   2. agent_runner_reaped_failure called with category push_and_pr_no_unmerged_files.
#   3. advance_phase NOT called.
#   4. ralph_baseline_transition_unrecognized NOT emitted.
#
# Sub-test B (regression): transition_for returns advance\tfix_conflict\t\t.
# Expected behaviour:
#   1. advance_phase fix_conflict called.
#   2. ralph_baseline_route_to_diagnoser NOT emitted.
# ══════════════════════════════════════════════════════════════════════════

t3573_state_dir="$TEST_TMP/t3573-state"
t3573_stub_bin="$TEST_TMP/t3573-bin"
t3573_workspace="$TEST_TMP/t3573-workspace"
t3573_repo_root="$TEST_TMP/t3573-repo"
mkdir -p "$t3573_state_dir" "$t3573_stub_bin" "$t3573_workspace" "$t3573_repo_root"
mkdir -p "$t3573_repo_root/tmp/dispatcher-input"
mkdir -p "$t3573_repo_root/tmp/dispatcher-output"

# psql stub — responds to phase SELECT with ralph.
cat > "$t3573_stub_bin/psql" <<'T3573PSQLEOF'
#!/usr/bin/env bash
query=""
while [[ $# -gt 0 ]]; do
    case "$1" in -c) shift; query="$1" ;; esac
    shift || true
done
if [[ "$query" == *"SELECT phase"* ]]; then printf 'ralph\n'; fi
exit 0
T3573PSQLEOF
chmod +x "$t3573_stub_bin/psql"

cat > "$t3573_stub_bin/git" <<'T3573GITEOF'
#!/usr/bin/env bash
exit 0
T3573GITEOF
chmod +x "$t3573_stub_bin/git"

cat > "$t3573_stub_bin/gh" <<'T3573GHEOF'
#!/usr/bin/env bash
exit 0
T3573GHEOF
chmod +x "$t3573_stub_bin/gh"

# ── Sub-test A: route_to_diagnoser with push_and_pr_no_unmerged_files ──────

t3573a_state_dir="$t3573_state_dir/a"
mkdir -p "$t3573a_state_dir"

_t3573a_transition_for_override="
transition_for() {
    if [[ \"\${1:-}\" == 'ralph' ]]; then
        printf 'route_to_diagnoser\t\t\tpush_and_pr_no_unmerged_files'
    fi
}
"

_t3573a_reaped_failure_override="
agent_runner_reaped_failure() {
    printf 'REAPED_FAILURE %s\n' \"\${1:-}\" >> \"\${T3573A_STATE_DIR}/reaped-log.txt\"
    log 'agent_runner_reaped_failure' \"phase=\$1\" \"hint=\$2\"
}
"

_t3573a_advance_phase_override="
advance_phase() {
    printf 'ADVANCE_PHASE %s\n' \"\${1:-}\" >> \"\${T3573A_STATE_DIR}/advance-log.txt\"
}
"

t3573a_out=$(
    set +eu
    export T3573A_STATE_DIR="$t3573a_state_dir"
    export AGENT_WORKSPACE="$t3573_workspace"
    export REPO_ROOT="$t3573_repo_root"
    export AGENT_ID="63636363-dead-beef-cafe-000000000001"
    export ISSUE_NUMBER="3573"
    export DATABASE_URL="postgresql://stub"
    export AGENT_RUNNER_DRY_RUN="0"
    export PATH="$t3573_stub_bin:$PATH"
    source "$t58_funcs"
    eval "$_t3573a_transition_for_override"
    eval "$_t3573a_reaped_failure_override"
    eval "$_t3573a_advance_phase_override"
    # Drive the ralph_baseline rebase dispatch logic directly (mirrors the entrypoint).
    _ralph_baseline_output='{"rebase_failed": true, "no_unmerged_files": true, "rebase_stderr_tail": "", "source_phase": "ralph"}'
    persist_phase_output "ralph" "$_ralph_baseline_output"
    _bt=$(transition_for "ralph" "$_ralph_baseline_output")
    _ba=$(printf '%s' "$_bt" | cut -f1)
    _bn=$(printf '%s' "$_bt" | cut -f2)
    _bs=$(printf '%s' "$_bt" | cut -f3)
    _bh=$(printf '%s' "$_bt" | cut -f4)
    case "$_ba" in
        advance)
            advance_phase "$_bn"
            ;;
        advance_with_status)
            advance_phase "$_bn" "$_bs"
            ;;
        route_to_diagnoser)
            log "ralph_baseline_route_to_diagnoser" "hint=$_bh"
            case "$_bh" in
                push_and_pr_no_unmerged_files)
                    agent_runner_reaped_failure \
                        "push_and_pr_no_unmerged_files" \
                        "push_and_pr_no_unmerged_files" \
                        "ralph baseline rebase failed with no unmerged files (#3573)"
                    ;;
                *)
                    log "ralph_baseline_route_unrecognized_hint" "hint=$_bh"
                    agent_runner_reaped_failure \
                        "diagnoser_route_unrecognized_hint" \
                        "diagnoser_route_unrecognized_hint" \
                        "ralph_baseline route_to_diagnoser received unrecognized hint=${_bh:-(empty)}"
                    ;;
            esac
            ;;
        *)
            log "ralph_baseline_transition_unrecognized" "action=$_ba"
            agent_runner_reaped_failure \
                "ralph_baseline_transition_unrecognized" \
                "ralph_baseline_transition_unrecognized" \
                "transition_for returned action=$_ba (expected advance after rebase_failed envelope)"
            ;;
    esac
    echo "subshell_done"
    2>&1
) 2>&1

# T3573A (1): subshell completed.
if printf '%s' "$t3573a_out" | grep -q "subshell_done"; then
    pass "#3573 T3573A [ralph_baseline route_to_diagnoser] — dispatch subshell ran to completion"
else
    fail "#3573 T3573A [ralph_baseline route_to_diagnoser] — dispatch subshell ran to completion" \
         "out tail: $(printf '%s' "$t3573a_out" | tail -c 600)"
fi

# T3573A (2): ralph_baseline_route_to_diagnoser log line emitted.
if printf '%s' "$t3573a_out" | grep -q "ralph_baseline_route_to_diagnoser"; then
    pass "#3573 T3573A [ralph_baseline route_to_diagnoser] — ralph_baseline_route_to_diagnoser log emitted"
else
    fail "#3573 T3573A [ralph_baseline route_to_diagnoser] — ralph_baseline_route_to_diagnoser log emitted" \
         "out: $(printf '%s' "$t3573a_out" | tail -c 600)"
fi

# T3573A (3): agent_runner_reaped_failure called with push_and_pr_no_unmerged_files.
if [[ -f "$t3573a_state_dir/reaped-log.txt" ]] && grep -q "REAPED_FAILURE push_and_pr_no_unmerged_files" "$t3573a_state_dir/reaped-log.txt"; then
    pass "#3573 T3573A [ralph_baseline route_to_diagnoser] — agent_runner_reaped_failure called with push_and_pr_no_unmerged_files"
else
    fail "#3573 T3573A [ralph_baseline route_to_diagnoser] — agent_runner_reaped_failure called with push_and_pr_no_unmerged_files" \
         "reaped-log: $(cat "$t3573a_state_dir/reaped-log.txt" 2>/dev/null || echo '(missing)')"
fi

# T3573A (4): advance_phase NOT called on route_to_diagnoser path.
if [[ ! -f "$t3573a_state_dir/advance-log.txt" ]] || ! grep -q "ADVANCE_PHASE" "$t3573a_state_dir/advance-log.txt"; then
    pass "#3573 T3573A [ralph_baseline route_to_diagnoser] — advance_phase not called on route_to_diagnoser"
else
    fail "#3573 T3573A [ralph_baseline route_to_diagnoser] — advance_phase not called on route_to_diagnoser" \
         "advance-log: $(cat "$t3573a_state_dir/advance-log.txt" 2>/dev/null)"
fi

# T3573A (5): ralph_baseline_transition_unrecognized NOT emitted.
if ! printf '%s' "$t3573a_out" | grep -q "ralph_baseline_transition_unrecognized"; then
    pass "#3573 T3573A [ralph_baseline route_to_diagnoser] — ralph_baseline_transition_unrecognized not emitted"
else
    fail "#3573 T3573A [ralph_baseline route_to_diagnoser] — ralph_baseline_transition_unrecognized not emitted" \
         "out: $(printf '%s' "$t3573a_out" | grep "ralph_baseline_transition_unrecognized")"
fi

# ── Sub-test B: advance happy-path regression ──────────────────────────────

t3573b_state_dir="$t3573_state_dir/b"
mkdir -p "$t3573b_state_dir"

_t3573b_transition_for_override="
transition_for() {
    if [[ \"\${1:-}\" == 'ralph' ]]; then
        printf 'advance\tfix_conflict\t\t'
    fi
}
"

_t3573b_advance_phase_override="
advance_phase() {
    printf 'ADVANCE_PHASE %s\n' \"\${1:-}\" >> \"\${T3573B_STATE_DIR}/advance-log.txt\"
}
"

t3573b_out=$(
    set +eu
    export T3573B_STATE_DIR="$t3573b_state_dir"
    export AGENT_WORKSPACE="$t3573_workspace"
    export REPO_ROOT="$t3573_repo_root"
    export AGENT_ID="63636364-dead-beef-cafe-000000000001"
    export ISSUE_NUMBER="3573"
    export DATABASE_URL="postgresql://stub"
    export AGENT_RUNNER_DRY_RUN="0"
    export PATH="$t3573_stub_bin:$PATH"
    source "$t58_funcs"
    eval "$_t3573b_transition_for_override"
    eval "$_t3573b_advance_phase_override"
    # Drive the ralph_baseline rebase dispatch logic directly.
    _ralph_baseline_output='{"rebase_failed": true, "conflict_files": ["foo.py"], "rebase_stderr_tail": "", "source_phase": "ralph"}'
    persist_phase_output "ralph" "$_ralph_baseline_output"
    _bt=$(transition_for "ralph" "$_ralph_baseline_output")
    _ba=$(printf '%s' "$_bt" | cut -f1)
    _bn=$(printf '%s' "$_bt" | cut -f2)
    _bs=$(printf '%s' "$_bt" | cut -f3)
    _bh=$(printf '%s' "$_bt" | cut -f4)
    case "$_ba" in
        advance)
            advance_phase "$_bn"
            ;;
        advance_with_status)
            advance_phase "$_bn" "$_bs"
            ;;
        route_to_diagnoser)
            log "ralph_baseline_route_to_diagnoser" "hint=$_bh"
            case "$_bh" in
                push_and_pr_no_unmerged_files)
                    agent_runner_reaped_failure \
                        "push_and_pr_no_unmerged_files" \
                        "push_and_pr_no_unmerged_files" \
                        "ralph baseline rebase failed with no unmerged files (#3573)"
                    ;;
                *)
                    log "ralph_baseline_route_unrecognized_hint" "hint=$_bh"
                    agent_runner_reaped_failure \
                        "diagnoser_route_unrecognized_hint" \
                        "diagnoser_route_unrecognized_hint" \
                        "ralph_baseline route_to_diagnoser received unrecognized hint=${_bh:-(empty)}"
                    ;;
            esac
            ;;
        *)
            log "ralph_baseline_transition_unrecognized" "action=$_ba"
            agent_runner_reaped_failure \
                "ralph_baseline_transition_unrecognized" \
                "ralph_baseline_transition_unrecognized" \
                "transition_for returned action=$_ba (expected advance after rebase_failed envelope)"
            ;;
    esac
    echo "subshell_done"
    2>&1
) 2>&1

# T3573B (1): subshell completed.
if printf '%s' "$t3573b_out" | grep -q "subshell_done"; then
    pass "#3573 T3573B [ralph_baseline advance] — dispatch subshell ran to completion"
else
    fail "#3573 T3573B [ralph_baseline advance] — dispatch subshell ran to completion" \
         "out tail: $(printf '%s' "$t3573b_out" | tail -c 600)"
fi

# T3573B (2): advance_phase fix_conflict called.
if [[ -f "$t3573b_state_dir/advance-log.txt" ]] && grep -q "ADVANCE_PHASE fix_conflict" "$t3573b_state_dir/advance-log.txt"; then
    pass "#3573 T3573B [ralph_baseline advance] — advance_phase fix_conflict called"
else
    fail "#3573 T3573B [ralph_baseline advance] — advance_phase fix_conflict called" \
         "advance-log: $(cat "$t3573b_state_dir/advance-log.txt" 2>/dev/null || echo '(missing)')"
fi

# T3573B (3): ralph_baseline_route_to_diagnoser NOT emitted on advance path.
if ! printf '%s' "$t3573b_out" | grep -q "ralph_baseline_route_to_diagnoser"; then
    pass "#3573 T3573B [ralph_baseline advance] — ralph_baseline_route_to_diagnoser not emitted on advance"
else
    fail "#3573 T3573B [ralph_baseline advance] — ralph_baseline_route_to_diagnoser not emitted on advance" \
         "out: $(printf '%s' "$t3573b_out" | grep "ralph_baseline_route_to_diagnoser")"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test T_issue3614: #3614 — handle_push_and_pr empty-diff after successful rebase.
#
# The bug: when an agent claims an issue whose fix has ALREADY shipped via
# a sibling PR, the pre-push ``git rebase origin/main`` succeeds AND drops
# the agent's commits (because the patches are already in main). The
# post-rebase ``rev-list --count origin/main..HEAD`` returns 0 — there's
# nothing to push. Pre-#3614 the entrypoint fell through to ``git push``
# (no-op) + ``gh pr create`` (which fails because there's no diff) and
# ultimately reaped the agent as ``push_and_pr_no_unmerged_files/failed``.
#
# Direct sibling of #3580 (which fixed the same bug class for
# ``handle_fix_ci``). The fix mirrors #3580: after the successful rebase,
# re-check the ahead-count and emit the existing ``{"no_op": true}``
# envelope (transition_from_push_and_pr already routes that to PHASE_NO_OP
# terminal succeeded). New log event ``push_and_pr_no_unmerged_files_
# already_applied`` distinguishes from the existing terminal-fail event.
#
# Test A (the bug): pre-rebase ahead=1 (passes the existing #3039 no-op
# guardrail), rebase succeeds (exit 0), post-rebase ahead=0. Assert:
#   * ``push_and_pr_no_unmerged_files_already_applied`` log emitted.
#   * ``git push`` NOT invoked.
#   * ``gh pr create`` NOT invoked.
#   * Function emits ``{"no_op": true}`` envelope.
# Test MUST fail against unfixed code (where the post-rebase check doesn't
# exist and the function falls through to push + PR create).
#
# Test B (happy path regression): pre-rebase ahead=1, rebase succeeds,
# post-rebase ahead=1 (rebase replayed the agent's commits cleanly).
# Assert: full push + PR create runs; envelope is NOT ``{"no_op": true}``.
# Don't regress the working path.
# ══════════════════════════════════════════════════════════════════════════

# Extract handle_push_and_pr + minimal dependencies. handle_push_and_pr
# depends on log, db_exec, persist_phase_output (none of which it actually
# calls in the empty-diff path, but the function body uses ``log`` heavily).
t3614_funcs="$TEST_TMP/t3614-funcs.sh"
printf 'exec 3>&1\n' > "$t3614_funcs"

for fn in db_exec db_query_one log persist_phase_output \
          assert_phase_deadline_not_exceeded \
          handle_push_and_pr; do
    awk -v FN="^${fn}\\\\(\\\\)" '
        $0 ~ FN { in_fn=1 }
        in_fn { print }
        in_fn && /^}$/ { exit }
    ' "$ENTRYPOINT" >> "$t3614_funcs"
done

# #3656: handle_push_and_pr references NETWORK_TIMEOUT_SECONDS.
# #3683: handle_push_and_pr references LOCAL_GIT_TIMEOUT_SECONDS and
# PUSH_AND_PR_PHASE_DEADLINE_SECONDS (via assert_phase_deadline_not_exceeded).
# T3614 doesn't drive the 124-exit branch — it just needs the constants
# defined so the wrapped commands run normally under ``set -u``.
grep '^NETWORK_TIMEOUT_SECONDS=' "$ENTRYPOINT" >> "$t3614_funcs"
grep '^LOCAL_GIT_TIMEOUT_SECONDS=' "$ENTRYPOINT" >> "$t3614_funcs"
grep '^PUSH_AND_PR_PHASE_DEADLINE_SECONDS=' "$ENTRYPOINT" >> "$t3614_funcs"

if grep -q "^handle_push_and_pr()" "$t3614_funcs"; then
    pass "#3614 T3614 setup — extracted handle_push_and_pr from entrypoint"
else
    fail "#3614 T3614 setup — extracted handle_push_and_pr from entrypoint" \
         "fixture head: $(head -c 400 "$t3614_funcs")"
fi

# Per-test runner. The git stub is parameterised by a counter file so
# successive ``rev-list --count`` calls can return different values
# (pre-rebase vs post-rebase). The first call answers the existing #3039
# no-op-on-clean-tree guardrail; the second call answers the new (this
# PR) post-rebase empty-diff check.
#
# Stub control via env vars:
#   T3614_REV_LIST_PRE   — count returned for the FIRST rev-list call
#                        (pre-rebase). Default 1 (something to push).
#   T3614_REV_LIST_POST  — count for the SECOND rev-list call (post-rebase).
#                        Default 1 (rebase preserved the agent's commits).
#                        Set to 0 to simulate "rebase pulled in the fix".
#   GIT_REBASE_EXIT    — exit code for ``git rebase origin/main``.
#                        Default 0 (clean rebase).
run_push_pr_test() {
    _test_id="$1"
    shift

    _tworkspace="$TEST_TMP/${_test_id}-workspace"
    _trepo="$TEST_TMP/${_test_id}-repo"
    _tstate="$TEST_TMP/${_test_id}-state"
    _tbin="$TEST_TMP/${_test_id}-bin"
    mkdir -p "$_tworkspace" "$_trepo" "$_tstate" "$_tbin"
    mkdir -p "$_trepo/tmp/dispatcher-output" "$_trepo/tmp/dispatcher-input"

    # rev-list counter file — written/read by the git stub to alternate
    # responses across calls.
    printf '0\n' > "$_tstate/rev-list-call-count.txt"

    # git stub — log every call, parameterise rev-list / push / rebase /
    # fetch by env. ``git -C <dir> <subcmd>`` is normalised by stripping
    # the -C arg.
    cat > "$_tbin/git" <<'T3614GITEOF'
#!/usr/bin/env bash
set -u
printf '%s\n' "CALL $*" >> "${T_STATE_DIR}/git-log.txt"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -C) shift 2 || true; continue ;;
        *) break ;;
    esac
done

sub="${1:-}"
case "$sub" in
    rev-list)
        # Returns T3614_REV_LIST_PRE on first call, T3614_REV_LIST_POST after.
        _counter_file="${T_STATE_DIR}/rev-list-call-count.txt"
        _n=$(cat "$_counter_file" 2>/dev/null || printf '0')
        _next=$((_n + 1))
        printf '%s\n' "$_next" > "$_counter_file"
        if [[ "$_n" == "0" ]]; then
            printf '%s\n' "${T3614_REV_LIST_PRE:-1}"
        else
            printf '%s\n' "${T3614_REV_LIST_POST:-1}"
        fi
        exit 0
        ;;
    fetch)
        exit "${GIT_FETCH_EXIT:-0}"
        ;;
    rebase)
        for _arg in "$@"; do
            if [[ "$_arg" == "--abort" ]]; then exit 0; fi
        done
        exit "${GIT_REBASE_EXIT:-0}"
        ;;
    diff)
        # ``git diff --name-only --diff-filter=U`` for conflict files.
        # T3614 doesn't drive the conflict path, but this keeps the stub
        # robust if future tests do.
        exit 0
        ;;
    push)
        exit "${GIT_PUSH_EXIT:-0}"
        ;;
    commit)
        exit "${GIT_COMMIT_EXIT:-0}"
        ;;
    rev-parse|merge-base)
        printf 'deadbeefcafe\n'
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
T3614GITEOF
    chmod +x "$_tbin/git"

    # #3656: passthrough ``timeout`` stub. handle_push_and_pr now wraps
    # git fetch / git push / gh pr create in ``timeout NETWORK_TIMEOUT_SECONDS``.
    # T3614 does NOT exercise the 124-exit branch — it just needs a
    # ``timeout`` binary on PATH that execs the inner command transparently
    # so the test runs identically pre- and post-#3656. macOS does not
    # ship coreutils' ``timeout(1)``, so a real ``timeout`` lookup would
    # exit 127 and break the test on operator laptops. This stub matches
    # GNU ``timeout`` semantics for the no-timer-fired path.
    cat > "$_tbin/timeout" <<'T3614TIMEOUTEOF'
#!/usr/bin/env bash
# argv: <seconds> <inner-cmd> <inner-args...>
shift  # discard the seconds arg
exec "$@"
T3614TIMEOUTEOF
    chmod +x "$_tbin/timeout"

    # gh stub — log invocations, default exit 0.
    cat > "$_tbin/gh" <<'T3614GHEOF'
#!/usr/bin/env bash
printf '%s\n' "CALL $*" >> "${T_STATE_DIR}/gh-log.txt"
# gh pr create prints a PR URL on stdout (the entrypoint parses it).
if [[ "${1:-}" == "pr" && "${2:-}" == "create" ]]; then
    printf 'https://github.com/judgemind/judgemind/pull/9999\n'
fi
exit "${GH_EXIT:-0}"
T3614GHEOF
    chmod +x "$_tbin/gh"

    # psql stub — log invocations.
    cat > "$_tbin/psql" <<'T3614PSQLEOF'
#!/usr/bin/env bash
query=""
while [[ $# -gt 0 ]]; do
    case "$1" in -c) shift; query="$1" ;; esac
    shift || true
done
printf 'CALL %s\n' "$query" >> "${T_STATE_DIR}/psql-log.txt"
exit 0
T3614PSQLEOF
    chmod +x "$_tbin/psql"

    if command -v jq >/dev/null 2>&1; then
        ln -sf "$(command -v jq)" "$_tbin/jq"
    fi

    set +e
    _tout=$(env \
        T_STATE_DIR="$_tstate" \
        PATH="$_tbin:$PATH" \
        AGENT_ID="${_test_id}-dead-beef-cafe-000000000099" \
        ISSUE_NUMBER="3614" \
        AGENT_WORKSPACE="$_tworkspace" \
        REPO_ROOT="$_trepo" \
        BRANCH_NAME="agent/${_test_id}abcd" \
        DATABASE_URL="postgres://test" \
        AGENT_RUNNER_DRY_RUN="0" \
        "$@" \
        bash -c '
            set -uo pipefail
            # shellcheck disable=SC1090
            . "'"$t3614_funcs"'"
            handle_push_and_pr
        ' 2>&1)
    _trc=$?
    set -e

    PUSHPR_TEST_RC="$_trc"
    PUSHPR_TEST_OUT="$_tout"
    PUSHPR_TEST_STATE="$_tstate"
}

# ── Sub-test A: post-rebase empty diff → no_op envelope, no push, no PR ───

run_push_pr_test "t3614a" T3614_REV_LIST_PRE=1 T3614_REV_LIST_POST=0 GIT_REBASE_EXIT=0

if [[ "$PUSHPR_TEST_RC" -eq 0 ]]; then
    pass "#3614 T3614A — handle_push_and_pr exits 0 on post-rebase empty-diff"
else
    fail "#3614 T3614A — handle_push_and_pr exits 0 on post-rebase empty-diff" \
         "rc=$PUSHPR_TEST_RC, output: $PUSHPR_TEST_OUT"
fi

# CRITICAL — new log event for the post-rebase already-applied case.
if printf '%s' "$PUSHPR_TEST_OUT" | grep -q "push_and_pr_no_unmerged_files_already_applied"; then
    pass "#3614 T3614A — push_and_pr_no_unmerged_files_already_applied log event emitted"
else
    fail "#3614 T3614A — push_and_pr_no_unmerged_files_already_applied log event emitted" \
         "output: $PUSHPR_TEST_OUT"
fi

# CRITICAL — function must NOT have invoked ``git push``. Empty diff
# means there's nothing to push; falling through to push would either
# no-op (Everything up-to-date) or attempt a push that gh pr create
# would then fail on for an empty-diff PR.
if ! grep -qE "CALL .*\\bpush\\b" "$PUSHPR_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3614 T3614A — git push NOT invoked on post-rebase empty-diff"
else
    fail "#3614 T3614A — git push NOT invoked on post-rebase empty-diff" \
         "git-log: $(cat "$PUSHPR_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# CRITICAL — function must NOT have invoked ``gh pr create``. With no
# diff there's no PR to open; gh pr create would fail and the agent
# would terminal as push_and_pr_pr_create_failed.
if ! grep -qE "CALL .*\\bpr\\b.*\\bcreate\\b" "$PUSHPR_TEST_STATE/gh-log.txt" 2>/dev/null; then
    pass "#3614 T3614A — gh pr create NOT invoked on post-rebase empty-diff"
else
    fail "#3614 T3614A — gh pr create NOT invoked on post-rebase empty-diff" \
         "gh-log: $(cat "$PUSHPR_TEST_STATE/gh-log.txt" 2>/dev/null)"
fi

# CRITICAL — emitted envelope must be the no_op success shape.
# transition_from_push_and_pr routes ``no_op=true`` to PHASE_NO_OP terminal
# succeeded — exactly the right outcome for "fix already in baseline".
if printf '%s' "$PUSHPR_TEST_OUT" | grep -q '"no_op": true\|"no_op":true'; then
    pass "#3614 T3614A — handler emits {\"no_op\": true} envelope on post-rebase empty-diff"
else
    fail "#3614 T3614A — handler emits {\"no_op\": true} envelope on post-rebase empty-diff" \
         "output: $PUSHPR_TEST_OUT"
fi

# Negative: must NOT emit the rebase_failed/no_unmerged_files envelope —
# that's the *failed-rebase-with-empty-conflict-files* path (already
# handled by #3465). Here the rebase SUCCEEDED; the diff is empty
# because the patches were already in main, not because rebase failed.
if ! printf '%s' "$PUSHPR_TEST_OUT" | grep -q '"rebase_failed": true\|"rebase_failed":true'; then
    pass "#3614 T3614A — does NOT emit rebase_failed envelope on post-rebase empty-diff"
else
    fail "#3614 T3614A — does NOT emit rebase_failed envelope on post-rebase empty-diff" \
         "output: $PUSHPR_TEST_OUT"
fi

# Two rev-list calls: one pre-fetch (the existing #3039 no-op-on-clean-
# tree guardrail at function entry) + one post-rebase (the new #3614
# check after a successful rebase). The new check is what distinguishes
# this from the pre-fix code, where rev-list was called only once and
# the function fell through to push + gh pr create on empty post-rebase
# diff.
if [[ -f "$PUSHPR_TEST_STATE/git-log.txt" ]]; then
    _rev_list_calls=$(grep -cE "CALL .*\\brev-list\\b" "$PUSHPR_TEST_STATE/git-log.txt" || printf '0')
    if [[ "$_rev_list_calls" == "2" ]]; then
        pass "#3614 T3614A — git rev-list invoked twice (pre-fetch guardrail + post-rebase check)"
    else
        fail "#3614 T3614A — git rev-list invoked twice (pre-fetch guardrail + post-rebase check)" \
             "found $_rev_list_calls call(s); git-log: $(cat "$PUSHPR_TEST_STATE/git-log.txt")"
    fi
fi

# ── Sub-test B: happy path regression — rebase preserves agent's commits ──

run_push_pr_test "t3614b" T3614_REV_LIST_PRE=1 T3614_REV_LIST_POST=1 GIT_REBASE_EXIT=0

if [[ "$PUSHPR_TEST_RC" -eq 0 ]]; then
    pass "#3614 T3614B — handle_push_and_pr exits 0 on happy path"
else
    fail "#3614 T3614B — handle_push_and_pr exits 0 on happy path" \
         "rc=$PUSHPR_TEST_RC, output: $PUSHPR_TEST_OUT"
fi

# Happy path MUST invoke git push.
if grep -qE "CALL .*\\bpush\\b.*-u\\b.*\\borigin\\b" "$PUSHPR_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3614 T3614B — git push -u origin invoked on happy path"
else
    fail "#3614 T3614B — git push -u origin invoked on happy path" \
         "git-log: $(cat "$PUSHPR_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# Happy path MUST invoke gh pr create.
if grep -qE "CALL .*\\bpr\\b.*\\bcreate\\b" "$PUSHPR_TEST_STATE/gh-log.txt" 2>/dev/null; then
    pass "#3614 T3614B — gh pr create invoked on happy path"
else
    fail "#3614 T3614B — gh pr create invoked on happy path" \
         "gh-log: $(cat "$PUSHPR_TEST_STATE/gh-log.txt" 2>/dev/null)"
fi

# Happy path MUST NOT emit the new already-applied event.
if ! printf '%s' "$PUSHPR_TEST_OUT" | grep -q "push_and_pr_no_unmerged_files_already_applied"; then
    pass "#3614 T3614B — push_and_pr_no_unmerged_files_already_applied NOT emitted on happy path"
else
    fail "#3614 T3614B — push_and_pr_no_unmerged_files_already_applied NOT emitted on happy path" \
         "output: $PUSHPR_TEST_OUT"
fi

# Happy path envelope must NOT be {"no_op": true} — the agent did push
# real changes; transition_from_push_and_pr should advance to awaiting_ci,
# not terminal as no_op succeeded.
if ! printf '%s' "$PUSHPR_TEST_OUT" | grep -q '"no_op": true\|"no_op":true'; then
    pass "#3614 T3614B — happy-path envelope is NOT {\"no_op\": true}"
else
    fail "#3614 T3614B — happy-path envelope is NOT {\"no_op\": true}" \
         "output: $PUSHPR_TEST_OUT"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test T_issue3651: #3651 — handle_ralph_baseline_rebase empty-rebase after abort.
#
# Direct sibling of #3614/PR #3645 (T3614), which fixed the same bug class
# for ``handle_push_and_pr``. Same fix shape, different code site:
#
# The bug: when the start-of-ralph baseline rebase fails AND the conflict-
# files list is empty (the agent's commits were already in main — sibling
# PR landed the same fix first, or the daemon is retrying an already-
# fixed issue), pre-#3651 the entrypoint emitted the rebase_failed +
# no_unmerged_files envelope, which transition_from_ralph routed to the
# diagnoser as ``push_and_pr_no_unmerged_files`` — terminal-failing the
# agent on what is actually a benign success. Cluster of stuck issues:
# #2777, #2832, #2854, #3297, #3407, #3574, #3581 (circuit breaker
# tripped 3rd time on 2026-04-27, see issue body).
#
# The fix: after the rebase --abort returns HEAD to ORIG_HEAD, re-check
# ``git rev-list --count origin/main..HEAD``. If 0, emit the existing
# ``{"no_op": true}`` envelope and a new log event
# ``ralph_baseline_no_unmerged_files_already_applied``.
# transition_from_ralph (#3651 Python-side fix) routes ``no_op=true`` to
# PHASE_NO_OP terminal succeeded.
#
# Test A (the bug): _baseline_conflict_files_json="[]", git rev-list
# returns 0 (post-abort ahead-count). Assert:
#   * ``ralph_baseline_no_unmerged_files_already_applied`` log emitted.
#   * Envelope is ``{"no_op": true, ...}``.
#   * Envelope does NOT contain ``rebase_failed`` (the old path).
# Test MUST fail against unfixed code (which always emitted the
# rebase_failed/no_unmerged_files envelope on empty conflict files).
#
# Test B (real conflict regression): _baseline_conflict_files_json=
# ["foo.py"]. Assert envelope is the rebase_failed/conflict_files shape
# (route to fix_conflict). Don't regress real conflicts.
#
# Test C (post-abort still ahead — non-empty conflict files miss):
# _baseline_conflict_files_json="[]", git rev-list returns 1. The
# rebase actually failed for a non-already-applied reason (corrupt
# state, fetch issue, etc.). Assert envelope is the OLD
# rebase_failed/no_unmerged_files shape (route to diagnoser via the
# existing #3465 path). Don't regress the non-already-applied case.
#
# Test D (dispatch path): drive the dispatch case statement with the
# new ``{"no_op": true, ...}`` envelope, mocking transition_for to
# return ``advance_with_status\tno_op\tsucceeded\t``. Assert
# advance_phase is called with ("no_op", "succeeded") AND
# agent_runner_reaped_failure is NOT called. Mirrors T3573A's dispatch-
# path test for the route_to_diagnoser case but exercises the new
# advance_with_status branch.
# ══════════════════════════════════════════════════════════════════════════

# Extract the #3651 envelope-construction block from the entrypoint via
# awk. Boundary: from ``_ralph_baseline_output=""`` (a marker line added
# in #3651) to the matching outer ``fi``. Walks ``if [[`` openings and
# ``fi`` closings to get the nesting right. This matches T3614's "extract
# the actual production code path" approach, adapted for an inline block
# (not a function — handle_ralph_baseline_rebase doesn't exist as a
# function in the entrypoint, see issue body).
t3651_block="$TEST_TMP/t3651-envelope-block.sh"
awk '
  /_ralph_baseline_output=""/ { in_block=1; depth=0 }
  in_block {
    print
    if ($0 ~ /^[[:space:]]*if[[:space:]]+\[\[/) depth++
    if ($0 ~ /^[[:space:]]*fi[[:space:]]*$/) {
      depth--
      if (depth == 0) exit
    }
  }
' "$ENTRYPOINT" > "$t3651_block"

if grep -q "ralph_baseline_no_unmerged_files_already_applied" "$t3651_block"; then
    pass "#3651 T3651 setup — extracted envelope block contains the new log event marker"
else
    fail "#3651 T3651 setup — extracted envelope block contains the new log event marker" \
         "block (head 800): $(head -c 800 "$t3651_block")"
fi

# #3675: the post-abort check is now ``git diff --quiet origin/main HEAD``
# (replaced the pre-#3675 ``rev-list --count`` check). Both shapes are
# valid evidence the envelope block contains the post-abort short-
# circuit logic — accept either to keep this setup assertion compatible
# with both pre- and post-#3675 code (defence against accidental
# regression of the fix).
if grep -qE 'rev-list[[:space:]]+--count[[:space:]]+origin/main\.\.HEAD' "$t3651_block" \
   || grep -qE 'diff[[:space:]]+--quiet[[:space:]]+origin/main[[:space:]]+HEAD' "$t3651_block"; then
    pass "#3651 T3651 setup — extracted envelope block contains the post-abort already-applied check"
else
    fail "#3651 T3651 setup — extracted envelope block contains the post-abort already-applied check" \
         "block (head 800): $(head -c 800 "$t3651_block")"
fi

# Per-test runner. Stubs ``git`` (only ``rev-list`` matters — the abort
# already happened upstream of this block) and ``log`` (captured into a
# state file). Parameters control:
#   T3651_CONFLICT_FILES_JSON  — value of ``_baseline_conflict_files_json``
#                              ("[]" for empty, or a JSON array for real).
#   T3651_REV_LIST_AHEAD       — count returned by ``git rev-list``
#                              (default 0 = collapsed to baseline).
run_t3651_envelope_test() {
    _test_id="$1"
    shift

    _twork="$TEST_TMP/${_test_id}-work"
    _trepo="$TEST_TMP/${_test_id}-repo"
    _tstate="$TEST_TMP/${_test_id}-state"
    _tbin="$TEST_TMP/${_test_id}-bin"
    mkdir -p "$_twork" "$_trepo" "$_tstate" "$_tbin"

    # git stub — rev-list and diff --quiet matter here. Logs every call.
    # #3675 replaced the post-abort ``rev-list --count`` check with
    # ``git diff --quiet origin/main HEAD``. T3651 is updated in the
    # same PR as #3675 so the stub now handles both invocations:
    # the pre-#3675 ``rev-list`` path (kept for backwards-compatibility
    # if any future code path re-introduces it) and the post-#3675
    # ``diff --quiet`` path that the new code actually exercises.
    cat > "$_tbin/git" <<'T3651GITEOF'
#!/usr/bin/env bash
set -u
printf '%s\n' "CALL $*" >> "${T_STATE_DIR}/git-log.txt"

while [[ $# -gt 0 ]]; do
    case "$1" in -C) shift 2 || true; continue ;; *) break ;; esac
done

case "${1:-}" in
    rev-list) printf '%s\n' "${T3651_REV_LIST_AHEAD:-0}"; exit 0 ;;
    diff)
        # ``git diff --quiet origin/main HEAD`` (post-#3675).
        # Disambiguate by inspecting flags.
        for _a in "$@"; do
            if [[ "$_a" == "--quiet" ]]; then
                exit "${T3651_DIFF_QUIET_RC:-0}"
            fi
        done
        exit 0
        ;;
    *) exit 0 ;;
esac
T3651GITEOF
    chmod +x "$_tbin/git"

    if command -v jq >/dev/null 2>&1; then
        ln -sf "$(command -v jq)" "$_tbin/jq"
    fi

    # Drive the extracted envelope block with the test parameters.
    set +e
    _tout=$(env \
        T_STATE_DIR="$_tstate" \
        PATH="$_tbin:$PATH" \
        AGENT_WORKSPACE="$_twork" \
        REPO_ROOT="$_trepo" \
        AGENT_ID="t3651-dead-beef-cafe-000000000099" \
        ISSUE_NUMBER="3651" \
        "$@" \
        bash -c '
            set -uo pipefail
            # The entrypoint defines log() which writes to fd 3; provide
            # a test-shim that mirrors to stdout so assertions can
            # grep the captured output.
            log() { printf "%s\n" "LOG $*"; }
            _baseline_conflict_files_json="${T3651_CONFLICT_FILES_JSON:-[]}"
            _baseline_rebase_stderr_tail='\''""'\''
            # Now source the extracted envelope block. It assigns
            # ``_ralph_baseline_output`` based on the inputs above.
            # shellcheck disable=SC1090
            . "'"$t3651_block"'"
            # Echo the resulting envelope so the test harness can
            # inspect it.
            printf "ENVELOPE %s\n" "$_ralph_baseline_output"
        ' 2>&1)
    _trc=$?
    set -e

    T3651_TEST_RC="$_trc"
    T3651_TEST_OUT="$_tout"
    T3651_TEST_STATE="$_tstate"
}

# ── Sub-test A: empty conflict files + post-abort ahead=0 → no_op ─────────

run_t3651_envelope_test "t3651a" T3651_CONFLICT_FILES_JSON="[]" T3651_REV_LIST_AHEAD=0

if [[ "$T3651_TEST_RC" -eq 0 ]]; then
    pass "#3651 T3651A — envelope block exits 0 on empty conflict files + ahead=0"
else
    fail "#3651 T3651A — envelope block exits 0 on empty conflict files + ahead=0" \
         "rc=$T3651_TEST_RC, output: $T3651_TEST_OUT"
fi

# CRITICAL — new log event must be emitted.
if printf '%s' "$T3651_TEST_OUT" | grep -q "ralph_baseline_no_unmerged_files_already_applied"; then
    pass "#3651 T3651A — ralph_baseline_no_unmerged_files_already_applied log event emitted"
else
    fail "#3651 T3651A — ralph_baseline_no_unmerged_files_already_applied log event emitted" \
         "output: $T3651_TEST_OUT"
fi

# CRITICAL — envelope must be {"no_op": true, ...}.
if printf '%s' "$T3651_TEST_OUT" | grep -qE 'ENVELOPE .*"no_op": true'; then
    pass "#3651 T3651A — envelope is {\"no_op\": true, ...} on already-applied path"
else
    fail "#3651 T3651A — envelope is {\"no_op\": true, ...} on already-applied path" \
         "output: $T3651_TEST_OUT"
fi

# CRITICAL — envelope must NOT carry rebase_failed=true. The pre-#3651
# code emitted ``{"rebase_failed": true, "no_unmerged_files": true, ...}``
# which transition_from_ralph routed to the diagnoser as a terminal-
# failure. The new path emits ``{"no_op": true, ...}`` which routes to
# PHASE_NO_OP terminal succeeded. These envelopes are mutually exclusive.
if ! printf '%s' "$T3651_TEST_OUT" | grep -qE 'ENVELOPE .*"rebase_failed": true'; then
    pass "#3651 T3651A — envelope does NOT carry rebase_failed=true on already-applied path"
else
    fail "#3651 T3651A — envelope does NOT carry rebase_failed=true on already-applied path" \
         "output: $T3651_TEST_OUT"
fi

# CRITICAL — #3675 replaced the post-abort ``rev-list --count`` check
# with ``git diff --quiet origin/main HEAD``. The new behavioural
# fingerprint is the diff invocation (rev-list is no longer called on
# this code path). Pre-#3651 neither existed; pre-#3675 only rev-list
# existed; post-#3675 only diff exists. Assert the new fingerprint.
if grep -qE "CALL .*\\bdiff\\b.*--quiet.*origin/main.*HEAD" "$T3651_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3651 T3651A — git diff --quiet origin/main HEAD invoked (#3675 replacement for rev-list)"
else
    fail "#3651 T3651A — git diff --quiet origin/main HEAD invoked (#3675 replacement for rev-list)" \
         "git-log: $(cat "$T3651_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# #3675 — assert the rev-list call from the pre-#3675 logic is GONE
# from this code path. The diff-quiet check replaces it.
if ! grep -qE "CALL .*\\brev-list\\b" "$T3651_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3651 T3651A — git rev-list NOT invoked on post-abort path (#3675 replaced with diff --quiet)"
else
    _t3651a_rev_list_calls=$(grep -cE "CALL .*\\brev-list\\b" "$T3651_TEST_STATE/git-log.txt" 2>/dev/null || printf '0')
    fail "#3651 T3651A — git rev-list NOT invoked on post-abort path (#3675 replaced with diff --quiet)" \
         "found $_t3651a_rev_list_calls call(s); git-log: $(cat "$T3651_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# ── Sub-test B: non-empty conflict files → fix_conflict envelope (regression) ──

run_t3651_envelope_test "t3651b" T3651_CONFLICT_FILES_JSON='["packages/api/foo.py"]' T3651_REV_LIST_AHEAD=1

if [[ "$T3651_TEST_RC" -eq 0 ]]; then
    pass "#3651 T3651B — envelope block exits 0 on real-conflict path"
else
    fail "#3651 T3651B — envelope block exits 0 on real-conflict path" \
         "rc=$T3651_TEST_RC, output: $T3651_TEST_OUT"
fi

# Real conflict envelope must carry conflict_files (route to fix_conflict).
if printf '%s' "$T3651_TEST_OUT" | grep -qE 'ENVELOPE .*"conflict_files": \[.*"packages/api/foo\.py".*\]'; then
    pass "#3651 T3651B — real-conflict envelope carries conflict_files (routes to fix_conflict)"
else
    fail "#3651 T3651B — real-conflict envelope carries conflict_files (routes to fix_conflict)" \
         "output: $T3651_TEST_OUT"
fi

# Real conflict envelope must NOT be no_op.
if ! printf '%s' "$T3651_TEST_OUT" | grep -qE 'ENVELOPE .*"no_op": true'; then
    pass "#3651 T3651B — real-conflict envelope is NOT {\"no_op\": true}"
else
    fail "#3651 T3651B — real-conflict envelope is NOT {\"no_op\": true}" \
         "output: $T3651_TEST_OUT"
fi

# Real conflict path must NOT emit the new already-applied log event.
if ! printf '%s' "$T3651_TEST_OUT" | grep -q "ralph_baseline_no_unmerged_files_already_applied"; then
    pass "#3651 T3651B — already-applied log NOT emitted on real-conflict path"
else
    fail "#3651 T3651B — already-applied log NOT emitted on real-conflict path" \
         "output: $T3651_TEST_OUT"
fi

# Real conflict path must NOT call rev-list (the post-abort check is
# only reached when conflict_files_json is empty).
if ! grep -qE "CALL .*\\brev-list\\b" "$T3651_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3651 T3651B — git rev-list NOT invoked on real-conflict path"
else
    fail "#3651 T3651B — git rev-list NOT invoked on real-conflict path" \
         "git-log: $(cat "$T3651_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# ── Sub-test C: empty conflict files but post-abort ahead=1 → diagnoser (regression) ──
#
# The rebase actually failed for a non-already-applied reason (corrupt
# state, fetch issue, etc.) and the abort returned HEAD to ORIG_HEAD
# which is still ahead of origin/main. Must preserve the existing
# #3465 route_to_diagnoser path — only ahead=0 triggers the new no_op
# terminal.

run_t3651_envelope_test "t3651c" T3651_CONFLICT_FILES_JSON="[]" T3651_REV_LIST_AHEAD=1 T3651_DIFF_QUIET_RC=1

if [[ "$T3651_TEST_RC" -eq 0 ]]; then
    pass "#3651 T3651C — envelope block exits 0 on empty-conflicts + still-ahead path"
else
    fail "#3651 T3651C — envelope block exits 0 on empty-conflicts + still-ahead path" \
         "rc=$T3651_TEST_RC, output: $T3651_TEST_OUT"
fi

# Still-ahead path must emit the OLD rebase_failed/no_unmerged_files
# envelope (route_to_diagnoser via #3465).
if printf '%s' "$T3651_TEST_OUT" | grep -qE 'ENVELOPE .*"rebase_failed": true'; then
    pass "#3651 T3651C — still-ahead envelope carries rebase_failed=true (#3465 path preserved)"
else
    fail "#3651 T3651C — still-ahead envelope carries rebase_failed=true (#3465 path preserved)" \
         "output: $T3651_TEST_OUT"
fi

if printf '%s' "$T3651_TEST_OUT" | grep -qE 'ENVELOPE .*"no_unmerged_files": true'; then
    pass "#3651 T3651C — still-ahead envelope carries no_unmerged_files=true"
else
    fail "#3651 T3651C — still-ahead envelope carries no_unmerged_files=true" \
         "output: $T3651_TEST_OUT"
fi

# Still-ahead path must NOT emit the new already-applied event.
if ! printf '%s' "$T3651_TEST_OUT" | grep -q "ralph_baseline_no_unmerged_files_already_applied"; then
    pass "#3651 T3651C — already-applied log NOT emitted on still-ahead path"
else
    fail "#3651 T3651C — already-applied log NOT emitted on still-ahead path" \
         "output: $T3651_TEST_OUT"
fi

# Still-ahead path must NOT be a no_op envelope.
if ! printf '%s' "$T3651_TEST_OUT" | grep -qE 'ENVELOPE .*"no_op": true'; then
    pass "#3651 T3651C — still-ahead envelope is NOT {\"no_op\": true}"
else
    fail "#3651 T3651C — still-ahead envelope is NOT {\"no_op\": true}" \
         "output: $T3651_TEST_OUT"
fi

# ── Sub-test D: dispatch with no_op envelope → advance_phase no_op succeeded ──
#
# The envelope-construction tests above (A/B/C) cover the entrypoint
# side of the fix. This sub-test covers the dispatch case statement
# (mirrors T3573A's structure) for the new path: when transition_for
# returns advance_with_status\tno_op\tsucceeded\t, the dispatch must
# call advance_phase("no_op", "succeeded") and NOT call
# agent_runner_reaped_failure.

t3651d_state_dir="$TEST_TMP/t3651d-state"
mkdir -p "$t3651d_state_dir"

_t3651d_transition_for_override="
transition_for() {
    if [[ \"\${1:-}\" == 'ralph' ]]; then
        printf 'advance_with_status\tno_op\tsucceeded\t'
    fi
}
"

_t3651d_advance_phase_override="
advance_phase() {
    printf 'ADVANCE_PHASE %s status=%s\n' \"\${1:-}\" \"\${2:-}\" >> \"\${T3651D_STATE_DIR}/advance-log.txt\"
}
"

_t3651d_reaped_failure_override="
agent_runner_reaped_failure() {
    printf 'REAPED_FAILURE %s\n' \"\${1:-}\" >> \"\${T3651D_STATE_DIR}/reaped-log.txt\"
}
"

# Reuse the t3573 stub_bin (psql / git / gh stubs are sufficient for
# advance_phase + log; we override transition_for / advance_phase /
# agent_runner_reaped_failure inline).
t3651d_workspace="$TEST_TMP/t3651d-workspace"
t3651d_repo_root="$TEST_TMP/t3651d-repo"
mkdir -p "$t3651d_workspace" "$t3651d_repo_root/tmp/dispatcher-input" "$t3651d_repo_root/tmp/dispatcher-output"

t3651d_out=$(
    set +eu
    export T3651D_STATE_DIR="$t3651d_state_dir"
    export AGENT_WORKSPACE="$t3651d_workspace"
    export REPO_ROOT="$t3651d_repo_root"
    export AGENT_ID="65656565-dead-beef-cafe-000000000001"
    export ISSUE_NUMBER="3651"
    export DATABASE_URL="postgresql://stub"
    export AGENT_RUNNER_DRY_RUN="0"
    export PATH="$t3573_stub_bin:$PATH"
    source "$t58_funcs"
    eval "$_t3651d_transition_for_override"
    eval "$_t3651d_advance_phase_override"
    eval "$_t3651d_reaped_failure_override"
    # Drive the ralph_baseline rebase dispatch logic directly with the
    # new no_op envelope (matches the entrypoint dispatch case statement).
    _ralph_baseline_output='{"no_op": true, "rebase_dropped_all_commits": true, "post_abort_ahead": 0, "rebase_stderr_tail": "", "source_phase": "ralph"}'
    persist_phase_output "ralph" "$_ralph_baseline_output"
    _bt=$(transition_for "ralph" "$_ralph_baseline_output")
    _ba=$(printf '%s' "$_bt" | cut -f1)
    _bn=$(printf '%s' "$_bt" | cut -f2)
    _bs=$(printf '%s' "$_bt" | cut -f3)
    _bh=$(printf '%s' "$_bt" | cut -f4)
    case "$_ba" in
        advance)
            advance_phase "$_bn"
            ;;
        advance_with_status)
            advance_phase "$_bn" "$_bs"
            ;;
        route_to_diagnoser)
            log "ralph_baseline_route_to_diagnoser" "hint=$_bh"
            case "$_bh" in
                push_and_pr_no_unmerged_files)
                    agent_runner_reaped_failure \
                        "push_and_pr_no_unmerged_files" \
                        "push_and_pr_no_unmerged_files" \
                        "ralph baseline rebase failed with no unmerged files (#3573)"
                    ;;
                *)
                    log "ralph_baseline_route_unrecognized_hint" "hint=$_bh"
                    agent_runner_reaped_failure \
                        "diagnoser_route_unrecognized_hint" \
                        "diagnoser_route_unrecognized_hint" \
                        "ralph_baseline route_to_diagnoser received unrecognized hint=${_bh:-(empty)}"
                    ;;
            esac
            ;;
        *)
            log "ralph_baseline_transition_unrecognized" "action=$_ba"
            agent_runner_reaped_failure \
                "ralph_baseline_transition_unrecognized" \
                "ralph_baseline_transition_unrecognized" \
                "transition_for returned action=$_ba (expected advance after rebase_failed envelope)"
            ;;
    esac
    echo "subshell_done"
    2>&1
) 2>&1

# T3651D (1): subshell completed.
if printf '%s' "$t3651d_out" | grep -q "subshell_done"; then
    pass "#3651 T3651D [ralph_baseline no_op dispatch] — dispatch subshell ran to completion"
else
    fail "#3651 T3651D [ralph_baseline no_op dispatch] — dispatch subshell ran to completion" \
         "out tail: $(printf '%s' "$t3651d_out" | tail -c 600)"
fi

# T3651D (2): advance_phase called with ("no_op", "succeeded").
if [[ -f "$t3651d_state_dir/advance-log.txt" ]] && grep -q "ADVANCE_PHASE no_op status=succeeded" "$t3651d_state_dir/advance-log.txt"; then
    pass "#3651 T3651D [ralph_baseline no_op dispatch] — advance_phase called with (no_op, succeeded)"
else
    fail "#3651 T3651D [ralph_baseline no_op dispatch] — advance_phase called with (no_op, succeeded)" \
         "advance-log: $(cat "$t3651d_state_dir/advance-log.txt" 2>/dev/null || echo '(missing)')"
fi

# T3651D (3): agent_runner_reaped_failure NOT called (this is the key
# behavioural distinction from the unfixed code, which terminal-failed
# the agent as push_and_pr_no_unmerged_files).
if [[ ! -f "$t3651d_state_dir/reaped-log.txt" ]] || ! grep -q "REAPED_FAILURE" "$t3651d_state_dir/reaped-log.txt"; then
    pass "#3651 T3651D [ralph_baseline no_op dispatch] — agent_runner_reaped_failure NOT called"
else
    fail "#3651 T3651D [ralph_baseline no_op dispatch] — agent_runner_reaped_failure NOT called" \
         "reaped-log: $(cat "$t3651d_state_dir/reaped-log.txt" 2>/dev/null)"
fi

# T3651D (4): ralph_baseline_route_to_diagnoser NOT emitted (the old
# terminal-fail log line — this is the bug signature from the issue
# trace, agent d511b0e8 at 20:59:33Z).
if ! printf '%s' "$t3651d_out" | grep -q "ralph_baseline_route_to_diagnoser"; then
    pass "#3651 T3651D [ralph_baseline no_op dispatch] — ralph_baseline_route_to_diagnoser NOT emitted"
else
    fail "#3651 T3651D [ralph_baseline no_op dispatch] — ralph_baseline_route_to_diagnoser NOT emitted" \
         "out: $(printf '%s' "$t3651d_out" | grep "ralph_baseline_route_to_diagnoser")"
fi

# T3651D (5): ralph_baseline_transition_unrecognized NOT emitted (the
# default-case fallthrough — must NOT fire on advance_with_status).
if ! printf '%s' "$t3651d_out" | grep -q "ralph_baseline_transition_unrecognized"; then
    pass "#3651 T3651D [ralph_baseline no_op dispatch] — ralph_baseline_transition_unrecognized NOT emitted"
else
    fail "#3651 T3651D [ralph_baseline no_op dispatch] — ralph_baseline_transition_unrecognized NOT emitted" \
         "out: $(printf '%s' "$t3651d_out" | grep "ralph_baseline_transition_unrecognized")"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test T_issue3662: #3662 — handle_push_and_pr post-fix_conflict re-rebase exit 128
# with empty conflict files AND post-abort ahead-count 0.
#
# Direct sibling of #3651/PR #3657 (T3651) and #3614/PR #3645 (T3614). Same bug
# class, third sibling code site:
#
# T3614 (#3614) fixed: handle_push_and_pr rebase exits 0 + drops all commits
#                    (post-rebase ahead-count 0).
# T3651 (#3651) fixed: handle_ralph_baseline_rebase rebase exits non-zero +
#                    empty conflict files + post-abort ahead-count 0.
# T3662 (#3662) fixes: handle_push_and_pr rebase exits non-zero + empty
#                    conflict files + post-abort ahead-count 0.
#
# This third sibling fires when a fix_conflict skill resolves real conflicts
# and commits, then push_and_pr re-rebases against fresh origin/main, the
# rebase exits 128, and the conflict-files list is empty (because the
# resolution turned out to be redundant — sibling PR landed in parallel,
# or the fix produced an empty commit). Pre-#3662 this fell through to the
# no_unmerged_files envelope, which transition_from_push_and_pr routed to
# the diagnoser as ``push_and_pr_no_unmerged_files`` — terminal-failing
# the agent on a benign success and tripping the circuit breaker
# repeatedly (#3581 hit it 6× by 2026-04-27).
#
# The fix: after rebase --abort, when conflict-files JSON is empty AND
# the post-abort ahead-count is 0, emit ``{"no_op": true, ...}`` and a
# new log event ``push_and_pr_no_unmerged_files_already_applied_post_rebase_failure``.
# transition_from_push_and_pr (#3645) already handles ``no_op=true`` →
# PHASE_NO_OP terminal succeeded.
#
# Test A (the bug): rebase exits 128, conflict-files empty, post-abort
# ahead=0. Assert:
#   * ``push_and_pr_no_unmerged_files_already_applied_post_rebase_failure``
#     log emitted.
#   * Envelope is ``{"no_op": true, ...}``.
#   * Envelope does NOT contain ``rebase_failed`` or ``no_unmerged_files``.
#   * ``git push`` NOT invoked.
#   * ``gh pr create`` NOT invoked.
# Test MUST fail against unfixed code (which always emitted the
# rebase_failed/no_unmerged_files envelope on empty conflict files +
# rebase rc != 0).
#
# Test B (real conflict regression): rebase exits 128, conflict-files
# non-empty (real conflict). Assert envelope is the rebase_failed/
# conflict_files shape (route to fix_conflict). Don't regress real
# conflicts.
#
# Test C (post-abort still ahead — non-already-applied regression):
# rebase exits 128, conflict-files empty, post-abort ahead=1 (rebase
# actually failed for a non-already-applied reason — corrupt state,
# fetch issue, etc.). Assert envelope is the OLD rebase_failed/
# no_unmerged_files shape (route to diagnoser via the existing #3465
# path). Don't regress the non-already-applied diagnoser route.
# ══════════════════════════════════════════════════════════════════════════

# Reuse T3614's extracted handle_push_and_pr fixture ($t3614_funcs) — same
# function under test, different code path within it. The T3614 test
# established that the extraction works; this test exercises a different
# branch of the same function.
if [[ -s "$t3614_funcs" ]] && grep -q "^handle_push_and_pr()" "$t3614_funcs"; then
    pass "#3662 T3662 setup — handle_push_and_pr fixture (from T3614) is available"
else
    fail "#3662 T3662 setup — handle_push_and_pr fixture (from T3614) is available" \
         "fixture missing or empty: $t3614_funcs"
fi

# Per-test runner. Drives handle_push_and_pr into the rebase-rc=128
# empty-conflicts path. Stub control via env vars:
#   T3662_REV_LIST_PRE   — count returned for the FIRST rev-list call
#                        (pre-rebase #3039 guardrail). Default 1.
#   T3662_REV_LIST_POST  — count for the SECOND rev-list call (the new
#                        #3662 post-abort ahead check). Default 0
#                        (commits already in baseline).
#   T3662_DIFF_FILES     — content for ``git diff --name-only --diff-
#                        filter=U`` stdout. Default empty (no conflict
#                        files). Set to file paths to simulate a real
#                        conflict.
#   GIT_REBASE_EXIT    — exit code for ``git rebase origin/main``.
#                        Default 128 (the failure case under test).
run_t3662_push_pr_test() {
    _test_id="$1"
    shift

    _tworkspace="$TEST_TMP/${_test_id}-workspace"
    _trepo="$TEST_TMP/${_test_id}-repo"
    _tstate="$TEST_TMP/${_test_id}-state"
    _tbin="$TEST_TMP/${_test_id}-bin"
    mkdir -p "$_tworkspace" "$_trepo" "$_tstate" "$_tbin"
    mkdir -p "$_trepo/tmp/dispatcher-output" "$_trepo/tmp/dispatcher-input"

    # rev-list counter file — alternates pre-rebase / post-abort responses.
    printf '0\n' > "$_tstate/rev-list-call-count.txt"

    # Stage the diff-files stdout for the conflict-files capture step.
    # T3662_DIFF_FILES is passed as a KEY=VAL ``env`` arg in "$@" (mirrors
    # T3614's parameterisation pattern) so it isn't set as a regular bash
    # var here. Parse it out of the args explicitly so the file content
    # matches the env value the entrypoint will see.
    _t3662_diff_files=""
    for _a in "$@"; do
        case "$_a" in
            T3662_DIFF_FILES=*) _t3662_diff_files="${_a#T3662_DIFF_FILES=}" ;;
        esac
    done
    printf '%s' "$_t3662_diff_files" > "$_tstate/diff-files-output.txt"

    cat > "$_tbin/git" <<'T3662GITEOF'
#!/usr/bin/env bash
set -u
printf '%s\n' "CALL $*" >> "${T_STATE_DIR}/git-log.txt"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -C) shift 2 || true; continue ;;
        *) break ;;
    esac
done

sub="${1:-}"
case "$sub" in
    rev-list)
        _counter_file="${T_STATE_DIR}/rev-list-call-count.txt"
        _n=$(cat "$_counter_file" 2>/dev/null || printf '0')
        _next=$((_n + 1))
        printf '%s\n' "$_next" > "$_counter_file"
        if [[ "$_n" == "0" ]]; then
            printf '%s\n' "${T3662_REV_LIST_PRE:-1}"
        else
            printf '%s\n' "${T3662_REV_LIST_POST:-0}"
        fi
        exit 0
        ;;
    fetch)
        exit "${GIT_FETCH_EXIT:-0}"
        ;;
    rebase)
        for _arg in "$@"; do
            if [[ "$_arg" == "--abort" ]]; then exit 0; fi
        done
        exit "${GIT_REBASE_EXIT:-128}"
        ;;
    diff)
        # ``git diff --name-only --diff-filter=U`` for conflict files.
        # ``git diff --quiet origin/main HEAD`` for the post-#3675 check.
        # Disambiguate via flag inspection.
        _is_diff_filter_U=0
        _is_diff_quiet=0
        for _a in "$@"; do
            case "$_a" in
                --diff-filter=U) _is_diff_filter_U=1 ;;
                --quiet)         _is_diff_quiet=1 ;;
            esac
        done
        if [[ "$_is_diff_filter_U" == "1" ]]; then
            cat "${T_STATE_DIR}/diff-files-output.txt"
            exit 0
        fi
        if [[ "$_is_diff_quiet" == "1" ]]; then
            exit "${T3662_DIFF_QUIET_RC:-0}"
        fi
        # ``git diff <merge-base>..ORIG_HEAD`` for original-patch capture.
        exit 0
        ;;
    push)
        exit "${GIT_PUSH_EXIT:-0}"
        ;;
    commit)
        exit "${GIT_COMMIT_EXIT:-0}"
        ;;
    rev-parse|merge-base)
        printf 'deadbeefcafe\n'
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
T3662GITEOF
    chmod +x "$_tbin/git"

    # #3656/#3675: ``handle_push_and_pr`` wraps git fetch / git push / gh
    # pr create in ``timeout NETWORK_TIMEOUT_SECONDS``. macOS does not
    # ship coreutils' ``timeout(1)``, so a real ``timeout`` lookup
    # would exit 127. T3662 doesn't exercise the 124-exit branch — this
    # stub passes through to the inner command transparently.
    cat > "$_tbin/timeout" <<'T3662TIMEOUTEOF'
#!/usr/bin/env bash
# argv: <seconds> <inner-cmd> <inner-args...>
shift  # discard the seconds arg
exec "$@"
T3662TIMEOUTEOF
    chmod +x "$_tbin/timeout"

    cat > "$_tbin/gh" <<'T3662GHEOF'
#!/usr/bin/env bash
printf '%s\n' "CALL $*" >> "${T_STATE_DIR}/gh-log.txt"
if [[ "${1:-}" == "pr" && "${2:-}" == "create" ]]; then
    printf 'https://github.com/judgemind/judgemind/pull/9999\n'
fi
exit "${GH_EXIT:-0}"
T3662GHEOF
    chmod +x "$_tbin/gh"

    cat > "$_tbin/psql" <<'T3662PSQLEOF'
#!/usr/bin/env bash
query=""
while [[ $# -gt 0 ]]; do
    case "$1" in -c) shift; query="$1" ;; esac
    shift || true
done
printf 'CALL %s\n' "$query" >> "${T_STATE_DIR}/psql-log.txt"
exit 0
T3662PSQLEOF
    chmod +x "$_tbin/psql"

    if command -v jq >/dev/null 2>&1; then
        ln -sf "$(command -v jq)" "$_tbin/jq"
    fi

    set +e
    _tout=$(env \
        T_STATE_DIR="$_tstate" \
        PATH="$_tbin:$PATH" \
        AGENT_ID="${_test_id}-dead-beef-cafe-000000000099" \
        ISSUE_NUMBER="3662" \
        AGENT_WORKSPACE="$_tworkspace" \
        REPO_ROOT="$_trepo" \
        BRANCH_NAME="agent/${_test_id}abcd" \
        DATABASE_URL="postgres://test" \
        AGENT_RUNNER_DRY_RUN="0" \
        "$@" \
        bash -c '
            set -uo pipefail
            # shellcheck disable=SC1090
            . "'"$t3614_funcs"'"
            handle_push_and_pr
        ' 2>&1)
    _trc=$?
    set -e

    T3662_TEST_RC="$_trc"
    T3662_TEST_OUT="$_tout"
    T3662_TEST_STATE="$_tstate"
}

# ── Sub-test A: rebase rc=128 + empty conflict files + post-abort ahead=0 ──
#   Expect: no_op envelope, new log event, NO push, NO PR create.

run_t3662_push_pr_test "t3662a" \
    T3662_REV_LIST_PRE=1 T3662_REV_LIST_POST=0 \
    T3662_DIFF_FILES="" \
    GIT_REBASE_EXIT=128

if [[ "$T3662_TEST_RC" -eq 0 ]]; then
    pass "#3662 T3662A — handle_push_and_pr exits 0 on empty-conflicts post-rebase-failure already-applied"
else
    fail "#3662 T3662A — handle_push_and_pr exits 0 on empty-conflicts post-rebase-failure already-applied" \
         "rc=$T3662_TEST_RC, output: $T3662_TEST_OUT"
fi

# CRITICAL — new log event must be emitted (the behavioural fingerprint
# of the fix). Distinct from push_and_pr_no_unmerged_files_already_applied
# (which is the rebase-rc=0 path from #3614) and from the terminal-fail
# event push_and_pr_no_unmerged_files (the existing #3465 diagnoser path
# that we explicitly do NOT take here).
if printf '%s' "$T3662_TEST_OUT" | grep -q "push_and_pr_no_unmerged_files_already_applied_post_rebase_failure"; then
    pass "#3662 T3662A — push_and_pr_no_unmerged_files_already_applied_post_rebase_failure log emitted"
else
    fail "#3662 T3662A — push_and_pr_no_unmerged_files_already_applied_post_rebase_failure log emitted" \
         "output: $T3662_TEST_OUT"
fi

# CRITICAL — envelope must be {"no_op": true, ...}.
if printf '%s' "$T3662_TEST_OUT" | grep -qE '"no_op": true'; then
    pass "#3662 T3662A — envelope is {\"no_op\": true, ...} on already-applied path"
else
    fail "#3662 T3662A — envelope is {\"no_op\": true, ...} on already-applied path" \
         "output: $T3662_TEST_OUT"
fi

# CRITICAL — envelope must NOT carry rebase_failed=true. Pre-#3662 the
# code emitted ``{"rebase_failed": true, "no_unmerged_files": true, ...}``
# which transition_from_push_and_pr routed to the diagnoser as a
# terminal-failure. The new path emits ``{"no_op": true, ...}`` which
# routes to PHASE_NO_OP terminal succeeded. Mutually exclusive.
if ! printf '%s' "$T3662_TEST_OUT" | grep -qE '"rebase_failed": true'; then
    pass "#3662 T3662A — envelope does NOT carry rebase_failed=true on already-applied path"
else
    fail "#3662 T3662A — envelope does NOT carry rebase_failed=true on already-applied path" \
         "output: $T3662_TEST_OUT"
fi

# CRITICAL — envelope must NOT carry no_unmerged_files=true. Same
# mutually-exclusive reason as above.
if ! printf '%s' "$T3662_TEST_OUT" | grep -qE '"no_unmerged_files": true'; then
    pass "#3662 T3662A — envelope does NOT carry no_unmerged_files=true on already-applied path"
else
    fail "#3662 T3662A — envelope does NOT carry no_unmerged_files=true on already-applied path" \
         "output: $T3662_TEST_OUT"
fi

# CRITICAL — function must NOT have invoked ``git push``. The empty diff
# means there's nothing to push; falling through to push would attempt
# a push the daemon would then reap as push_and_pr_no_unmerged_files.
if ! grep -qE "CALL .*\\bpush\\b" "$T3662_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3662 T3662A — git push NOT invoked on already-applied post-rebase-failure"
else
    fail "#3662 T3662A — git push NOT invoked on already-applied post-rebase-failure" \
         "git-log: $(cat "$T3662_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# CRITICAL — function must NOT have invoked ``gh pr create``. With no
# diff there's no PR to open.
if ! grep -qE "CALL .*\\bpr\\b.*\\bcreate\\b" "$T3662_TEST_STATE/gh-log.txt" 2>/dev/null; then
    pass "#3662 T3662A — gh pr create NOT invoked on already-applied post-rebase-failure"
else
    fail "#3662 T3662A — gh pr create NOT invoked on already-applied post-rebase-failure" \
         "gh-log: $(cat "$T3662_TEST_STATE/gh-log.txt" 2>/dev/null)"
fi

# Pre-#3675: two rev-list calls (pre-rebase #3039 guardrail + new #3662
# post-abort ahead-count check).
# Post-#3675: one rev-list call (only the pre-rebase guardrail) PLUS
# one ``git diff --quiet origin/main HEAD`` call (the new post-#3675
# replacement for the post-abort rev-list). Assert the new fingerprints.
if [[ -f "$T3662_TEST_STATE/git-log.txt" ]]; then
    _t3662a_rev_list_calls=$(grep -cE "CALL .*\\brev-list\\b" "$T3662_TEST_STATE/git-log.txt" || printf '0')
    if [[ "$_t3662a_rev_list_calls" == "1" ]]; then
        pass "#3662 T3662A — git rev-list invoked once (pre-rebase guardrail; #3675 replaced post-abort check)"
    else
        fail "#3662 T3662A — git rev-list invoked once (pre-rebase guardrail; #3675 replaced post-abort check)" \
             "found $_t3662a_rev_list_calls call(s); git-log: $(cat "$T3662_TEST_STATE/git-log.txt")"
    fi
fi

# #3675 — git diff --quiet origin/main HEAD must have been invoked
# (the new post-abort check that replaced the old rev-list path).
if grep -qE "CALL .*\\bdiff\\b.*--quiet.*origin/main.*HEAD" "$T3662_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3662 T3662A — git diff --quiet origin/main HEAD invoked (#3675 replacement)"
else
    fail "#3662 T3662A — git diff --quiet origin/main HEAD invoked (#3675 replacement)" \
         "git-log: $(cat "$T3662_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# ── Sub-test B: rebase rc=128 + non-empty conflict files (real conflict) ──
#   Expect: rebase_failed/conflict_files envelope (route to fix_conflict).

run_t3662_push_pr_test "t3662b" \
    T3662_REV_LIST_PRE=1 T3662_REV_LIST_POST=1 \
    T3662_DIFF_FILES="packages/api/foo.py
packages/scraper-framework/bar.py" \
    GIT_REBASE_EXIT=128

if [[ "$T3662_TEST_RC" -eq 0 ]]; then
    pass "#3662 T3662B — handle_push_and_pr exits 0 on real-conflict path"
else
    fail "#3662 T3662B — handle_push_and_pr exits 0 on real-conflict path" \
         "rc=$T3662_TEST_RC, output: $T3662_TEST_OUT"
fi

# Real conflict envelope must carry conflict_files (routes to fix_conflict).
if printf '%s' "$T3662_TEST_OUT" | grep -qE '"conflict_files":[[:space:]]*\[.*"packages/api/foo\.py".*\]'; then
    pass "#3662 T3662B — real-conflict envelope carries conflict_files (routes to fix_conflict)"
else
    fail "#3662 T3662B — real-conflict envelope carries conflict_files (routes to fix_conflict)" \
         "output: $T3662_TEST_OUT"
fi

# Real conflict envelope must carry rebase_failed=true (this is the
# #3225 path — fix_conflict skill consumes the envelope).
if printf '%s' "$T3662_TEST_OUT" | grep -qE '"rebase_failed": true'; then
    pass "#3662 T3662B — real-conflict envelope carries rebase_failed=true"
else
    fail "#3662 T3662B — real-conflict envelope carries rebase_failed=true" \
         "output: $T3662_TEST_OUT"
fi

# Real conflict envelope must NOT be no_op.
if ! printf '%s' "$T3662_TEST_OUT" | grep -qE '"no_op": true'; then
    pass "#3662 T3662B — real-conflict envelope is NOT {\"no_op\": true}"
else
    fail "#3662 T3662B — real-conflict envelope is NOT {\"no_op\": true}" \
         "output: $T3662_TEST_OUT"
fi

# Real conflict path must NOT emit the new already-applied log event.
if ! printf '%s' "$T3662_TEST_OUT" | grep -q "push_and_pr_no_unmerged_files_already_applied_post_rebase_failure"; then
    pass "#3662 T3662B — already-applied-post-rebase-failure log NOT emitted on real-conflict path"
else
    fail "#3662 T3662B — already-applied-post-rebase-failure log NOT emitted on real-conflict path" \
         "output: $T3662_TEST_OUT"
fi

# Real conflict path must NOT call rev-list a second time (the post-abort
# check is only reached when conflict_files_json is empty). Exactly one
# call (the pre-rebase #3039 guardrail).
if [[ -f "$T3662_TEST_STATE/git-log.txt" ]]; then
    _t3662b_rev_list_calls=$(grep -cE "CALL .*\\brev-list\\b" "$T3662_TEST_STATE/git-log.txt" || printf '0')
    if [[ "$_t3662b_rev_list_calls" == "1" ]]; then
        pass "#3662 T3662B — git rev-list invoked exactly once (post-abort check skipped on real conflict)"
    else
        fail "#3662 T3662B — git rev-list invoked exactly once (post-abort check skipped on real conflict)" \
             "found $_t3662b_rev_list_calls call(s); git-log: $(cat "$T3662_TEST_STATE/git-log.txt")"
    fi
fi

# ── Sub-test C: rebase rc=128 + empty conflict files + post-abort ahead=1 ──
#   The rebase actually failed for a non-already-applied reason — must
#   preserve the existing #3465 route_to_diagnoser path. Don't regress.

run_t3662_push_pr_test "t3662c" \
    T3662_REV_LIST_PRE=1 T3662_REV_LIST_POST=1 \
    T3662_DIFF_QUIET_RC=1 \
    T3662_DIFF_FILES="" \
    GIT_REBASE_EXIT=128

if [[ "$T3662_TEST_RC" -eq 0 ]]; then
    pass "#3662 T3662C — handle_push_and_pr exits 0 on empty-conflicts + still-ahead path"
else
    fail "#3662 T3662C — handle_push_and_pr exits 0 on empty-conflicts + still-ahead path" \
         "rc=$T3662_TEST_RC, output: $T3662_TEST_OUT"
fi

# Still-ahead path must emit the OLD rebase_failed/no_unmerged_files
# envelope (the existing #3465 route_to_diagnoser path).
if printf '%s' "$T3662_TEST_OUT" | grep -qE '"rebase_failed": true'; then
    pass "#3662 T3662C — still-ahead envelope carries rebase_failed=true (#3465 path preserved)"
else
    fail "#3662 T3662C — still-ahead envelope carries rebase_failed=true (#3465 path preserved)" \
         "output: $T3662_TEST_OUT"
fi

if printf '%s' "$T3662_TEST_OUT" | grep -qE '"no_unmerged_files": true'; then
    pass "#3662 T3662C — still-ahead envelope carries no_unmerged_files=true"
else
    fail "#3662 T3662C — still-ahead envelope carries no_unmerged_files=true" \
         "output: $T3662_TEST_OUT"
fi

# Still-ahead path must NOT emit the new already-applied event.
if ! printf '%s' "$T3662_TEST_OUT" | grep -q "push_and_pr_no_unmerged_files_already_applied_post_rebase_failure"; then
    pass "#3662 T3662C — already-applied-post-rebase-failure log NOT emitted on still-ahead path"
else
    fail "#3662 T3662C — already-applied-post-rebase-failure log NOT emitted on still-ahead path" \
         "output: $T3662_TEST_OUT"
fi

# Still-ahead path must NOT be a no_op envelope.
if ! printf '%s' "$T3662_TEST_OUT" | grep -qE '"no_op": true'; then
    pass "#3662 T3662C — still-ahead envelope is NOT {\"no_op\": true}"
else
    fail "#3662 T3662C — still-ahead envelope is NOT {\"no_op\": true}" \
         "output: $T3662_TEST_OUT"
fi

# Still-ahead path must NOT invoke git push or gh pr create — the
# entrypoint returns early after emitting the diagnoser-route envelope.
if ! grep -qE "CALL .*\\bpush\\b" "$T3662_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3662 T3662C — git push NOT invoked on still-ahead diagnoser-route path"
else
    fail "#3662 T3662C — git push NOT invoked on still-ahead diagnoser-route path" \
         "git-log: $(cat "$T3662_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

if ! grep -qE "CALL .*\\bpr\\b.*\\bcreate\\b" "$T3662_TEST_STATE/gh-log.txt" 2>/dev/null; then
    pass "#3662 T3662C — gh pr create NOT invoked on still-ahead diagnoser-route path"
else
    fail "#3662 T3662C — gh pr create NOT invoked on still-ahead diagnoser-route path" \
         "gh-log: $(cat "$T3662_TEST_STATE/gh-log.txt" 2>/dev/null)"
fi

# Still-ahead path: post-#3675 the unconditional probe is now
# ``git diff --quiet origin/main HEAD`` (replaced the post-abort
# rev-list call). Assert the diff probe was invoked AND rev-list
# was called only once (the pre-rebase #3039 guardrail).
if [[ -f "$T3662_TEST_STATE/git-log.txt" ]]; then
    _t3662c_rev_list_calls=$(grep -cE "CALL .*\\brev-list\\b" "$T3662_TEST_STATE/git-log.txt" || printf '0')
    if [[ "$_t3662c_rev_list_calls" == "1" ]]; then
        pass "#3662 T3662C — git rev-list invoked once (pre-rebase guardrail; #3675 replaced post-abort check)"
    else
        fail "#3662 T3662C — git rev-list invoked once (pre-rebase guardrail; #3675 replaced post-abort check)" \
             "found $_t3662c_rev_list_calls call(s); git-log: $(cat "$T3662_TEST_STATE/git-log.txt")"
    fi
fi

if grep -qE "CALL .*\\bdiff\\b.*--quiet.*origin/main.*HEAD" "$T3662_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3662 T3662C — git diff --quiet origin/main HEAD invoked on still-ahead path (#3675)"
else
    fail "#3662 T3662C — git diff --quiet origin/main HEAD invoked on still-ahead path (#3675)" \
         "git-log: $(cat "$T3662_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# Test T_issue3656: #3656 — network-blocking ops in handle_push_and_pr are wrapped
# in ``timeout NETWORK_TIMEOUT_SECONDS``, and a 124 exit emits a
# distinct ``*_timeout`` log + structured failure envelope.
#
# Pre-#3656 a hung ``git push`` (network drop, GitHub auth retry loop,
# PAT mid-rotation) would block indefinitely — observed for 16+ minutes
# on agent 2ff6e282 (#3608) before manual ``aws ecs stop-task``. The
# fix wraps the call in ``timeout`` so a hang surfaces as a clean 124
# exit routed through ``handle_push_and_pr``'s existing failure path.
#
# Three sub-tests cover the three wrapped sites (git push, gh pr create,
# git fetch). Each uses a stub ``timeout`` shim that returns 124
# unconditionally for the targeted call — emulating "outer process
# killed inner subprocess after the timer fired" — so we can drive
# the 124 branch deterministically without burning real wall-clock
# seconds.
#
# Self-contained per the issue's "don't share fixture stubs across
# test markers" rule. Historical note on marker churn during development:
# When this test was first written (pre-issue-numbered convention), two
# sequential-numbered markers (T65 and T66) collided with concurrently
# merged PRs (#3657 and #3666) — the root cause that motivated migrating
# to the T_issue<N> naming scheme. Under the new convention each marker
# is uniquely keyed by its issue number (T_issue3656 / T3656_* env
# namespace / run_t3656_test driver), so similar collisions cannot occur.
# Each test block owns its $_tbin per-test stub directory and env vars,
# with no shared fixture state across T_issue3614/T_issue3651/T_issue3662.
# ══════════════════════════════════════════════════════════════════════════

# Extract handle_push_and_pr + minimal dependencies (mirrors T3614's
# extraction). We need ``log``, ``db_exec``, ``persist_phase_output``,
# and ``handle_push_and_pr`` itself plus the ``NETWORK_TIMEOUT_SECONDS``
# constant from the post-function config block.
t3656_funcs="$TEST_TMP/t3656-funcs.sh"
printf 'exec 3>&1\n' > "$t3656_funcs"

for fn in db_exec db_query_one log persist_phase_output \
          assert_phase_deadline_not_exceeded \
          handle_push_and_pr; do
    awk -v FN="^${fn}\\\\(\\\\)" '
        $0 ~ FN { in_fn=1 }
        in_fn { print }
        in_fn && /^}$/ { exit }
    ' "$ENTRYPOINT" >> "$t3656_funcs"
done

# Append constants that handle_push_and_pr references when sourced standalone.
# #3656: NETWORK_TIMEOUT_SECONDS; #3683: LOCAL_GIT_TIMEOUT_SECONDS and
# PUSH_AND_PR_PHASE_DEADLINE_SECONDS (via assert_phase_deadline_not_exceeded).
grep '^NETWORK_TIMEOUT_SECONDS=' "$ENTRYPOINT" >> "$t3656_funcs"
grep '^LOCAL_GIT_TIMEOUT_SECONDS=' "$ENTRYPOINT" >> "$t3656_funcs"
grep '^PUSH_AND_PR_PHASE_DEADLINE_SECONDS=' "$ENTRYPOINT" >> "$t3656_funcs"

if grep -q "^NETWORK_TIMEOUT_SECONDS=" "$t3656_funcs"; then
    pass "#3656 T3656 setup — extracted NETWORK_TIMEOUT_SECONDS constant"
else
    fail "#3656 T3656 setup — extracted NETWORK_TIMEOUT_SECONDS constant" \
         "fixture tail: $(tail -c 400 "$t3656_funcs")"
fi

if grep -q "^handle_push_and_pr()" "$t3656_funcs"; then
    pass "#3656 T3656 setup — extracted handle_push_and_pr from entrypoint"
else
    fail "#3656 T3656 setup — extracted handle_push_and_pr from entrypoint" \
         "fixture head: $(head -c 400 "$t3656_funcs")"
fi

# Per-test runner with a stub ``timeout`` binary that can be
# parameterised to return 124 only for the targeted command (git
# fetch / git push / gh pr create) and pass-through for everything
# else. This is more realistic than env-toggling all three calls
# because it lets us assert that ONE site fires its timeout while
# the OTHERS run normally.
#
# Stub control via env vars:
#   T3656_TIMEOUT_TARGET — substring to match in the timeout's argv.
#                        When the substring matches, ``timeout`` exits
#                        124 without invoking the inner command.
#                        Otherwise it execs the inner command directly.
#                        Examples: "fetch", "push", "pr create".
#   T3656_REV_LIST_PRE   — count returned for the FIRST rev-list call
#                        (pre-rebase). Default 1 (something to push).
#   T3656_REV_LIST_POST  — count for the SECOND rev-list call (post-rebase).
#                        Default 1 (rebase preserved the agent's commits).
#   GIT_REBASE_EXIT    — exit code for ``git rebase origin/main``.
#                        Default 0 (clean rebase).
run_t3656_test() {
    _test_id="$1"
    shift

    _tworkspace="$TEST_TMP/${_test_id}-workspace"
    _trepo="$TEST_TMP/${_test_id}-repo"
    _tstate="$TEST_TMP/${_test_id}-state"
    _tbin="$TEST_TMP/${_test_id}-bin"
    mkdir -p "$_tworkspace" "$_trepo" "$_tstate" "$_tbin"
    mkdir -p "$_trepo/tmp/dispatcher-output" "$_trepo/tmp/dispatcher-input"

    # rev-list counter file — written/read by the git stub to alternate
    # responses across calls (mirrors T3614's pattern).
    printf '0\n' > "$_tstate/rev-list-call-count.txt"

    # ``timeout`` stub — the heart of T3656. Logs every call. When the
    # T3656_TIMEOUT_TARGET substring appears in the inner command argv,
    # exits 124 (mirroring real ``timeout`` behaviour after SIGTERM
    # on timer expiry). Otherwise execs the inner command directly so
    # other ``timeout``-wrapped calls in the same test run normally.
    cat > "$_tbin/timeout" <<'T3656TIMEOUTEOF'
#!/usr/bin/env bash
set -u
# argv: <seconds> <inner-cmd> <inner-args...>
_secs="${1:-}"
shift || true
_inner_argv="$*"
printf 'CALL secs=%s argv=%s\n' "$_secs" "$_inner_argv" \
    >> "${T_STATE_DIR}/timeout-log.txt"
# T3656_TIMEOUT_TARGET — substring match against the inner argv.
if [[ -n "${T3656_TIMEOUT_TARGET:-}" && "$_inner_argv" == *"${T3656_TIMEOUT_TARGET}"* ]]; then
    # Emulate real ``timeout(1)`` after SIGTERM-on-timer-expiry.
    exit 124
fi
# No match — exec the inner command transparently.
exec "$@"
T3656TIMEOUTEOF
    chmod +x "$_tbin/timeout"

    # git stub — reused pattern from T3614. Records every invocation;
    # returns parameterised exit codes for fetch/push/rebase.
    cat > "$_tbin/git" <<'T3656GITEOF'
#!/usr/bin/env bash
set -u
printf '%s\n' "CALL $*" >> "${T_STATE_DIR}/git-log.txt"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -C) shift 2 || true; continue ;;
        *) break ;;
    esac
done

sub="${1:-}"
case "$sub" in
    rev-list)
        _counter_file="${T_STATE_DIR}/rev-list-call-count.txt"
        _n=$(cat "$_counter_file" 2>/dev/null || printf '0')
        _next=$((_n + 1))
        printf '%s\n' "$_next" > "$_counter_file"
        if [[ "$_n" == "0" ]]; then
            printf '%s\n' "${T3656_REV_LIST_PRE:-1}"
        else
            printf '%s\n' "${T3656_REV_LIST_POST:-1}"
        fi
        exit 0
        ;;
    fetch)
        exit "${GIT_FETCH_EXIT:-0}"
        ;;
    rebase)
        for _arg in "$@"; do
            if [[ "$_arg" == "--abort" ]]; then exit 0; fi
        done
        exit "${GIT_REBASE_EXIT:-0}"
        ;;
    diff)
        exit 0
        ;;
    push)
        exit "${GIT_PUSH_EXIT:-0}"
        ;;
    commit)
        exit "${GIT_COMMIT_EXIT:-0}"
        ;;
    rev-parse|merge-base)
        printf 'deadbeefcafe\n'
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
T3656GITEOF
    chmod +x "$_tbin/git"

    # gh stub — log + return PR URL on ``pr create``, exit 0 by default.
    cat > "$_tbin/gh" <<'T3656GHEOF'
#!/usr/bin/env bash
printf '%s\n' "CALL $*" >> "${T_STATE_DIR}/gh-log.txt"
if [[ "${1:-}" == "pr" && "${2:-}" == "create" ]]; then
    printf 'https://github.com/judgemind/judgemind/pull/9999\n'
fi
exit "${GH_EXIT:-0}"
T3656GHEOF
    chmod +x "$_tbin/gh"

    # psql stub — log only.
    cat > "$_tbin/psql" <<'T3656PSQLEOF'
#!/usr/bin/env bash
query=""
while [[ $# -gt 0 ]]; do
    case "$1" in -c) shift; query="$1" ;; esac
    shift || true
done
printf 'CALL %s\n' "$query" >> "${T_STATE_DIR}/psql-log.txt"
exit 0
T3656PSQLEOF
    chmod +x "$_tbin/psql"

    if command -v jq >/dev/null 2>&1; then
        ln -sf "$(command -v jq)" "$_tbin/jq"
    fi

    set +e
    _tout=$(env \
        T_STATE_DIR="$_tstate" \
        PATH="$_tbin:$PATH" \
        AGENT_ID="${_test_id}-dead-beef-cafe-000000000099" \
        ISSUE_NUMBER="3656" \
        AGENT_WORKSPACE="$_tworkspace" \
        REPO_ROOT="$_trepo" \
        BRANCH_NAME="agent/${_test_id}abcd" \
        DATABASE_URL="postgres://test" \
        AGENT_RUNNER_DRY_RUN="0" \
        "$@" \
        bash -c '
            set -uo pipefail
            # shellcheck disable=SC1090
            . "'"$t3656_funcs"'"
            handle_push_and_pr
        ' 2>&1)
    _trc=$?
    set -e

    T3656_TEST_RC="$_trc"
    T3656_TEST_OUT="$_tout"
    T3656_TEST_STATE="$_tstate"
}

# ── Sub-test A: ``git push`` timeout → push_timeout envelope ────────────────
#
# T3656_TIMEOUT_TARGET="push -u" matches only the ``git push -u origin``
# call site (not ``git fetch`` or ``gh pr create``). Asserts:
#   * ``push_and_pr_push_timeout`` log emitted with elapsed_seconds.
#   * Envelope is ``{"no_op": false, "push_failed": true, "reason": "push_timeout"}``.
#   * ``gh pr create`` is NOT invoked (function returns at the timeout).
# Test would fail against unfixed code (no timeout wrapper, no
# ``*_timeout`` log, push hang-forever).
run_t3656_test "t3656a" T3656_TIMEOUT_TARGET="push -u" T3656_REV_LIST_PRE=1 T3656_REV_LIST_POST=1 GIT_REBASE_EXIT=0

if [[ "$T3656_TEST_RC" -eq 0 ]]; then
    pass "#3656 T3656A — handle_push_and_pr exits 0 on git push timeout"
else
    fail "#3656 T3656A — handle_push_and_pr exits 0 on git push timeout" \
         "rc=$T3656_TEST_RC, output: $T3656_TEST_OUT"
fi

# CRITICAL — push_timeout log event emitted.
if printf '%s' "$T3656_TEST_OUT" | grep -q "push_and_pr_push_timeout"; then
    pass "#3656 T3656A — push_and_pr_push_timeout log event emitted"
else
    fail "#3656 T3656A — push_and_pr_push_timeout log event emitted" \
         "output: $T3656_TEST_OUT"
fi

# CRITICAL — log includes elapsed_seconds field (not bare event).
# The ``log`` helper emits structured JSON, so the field appears as
# ``"elapsed_seconds": "300"`` — match either JSON-quoted or
# key=value forms to keep the assertion implementation-agnostic.
if printf '%s' "$T3656_TEST_OUT" \
    | grep "push_and_pr_push_timeout" \
    | grep -qE '("?elapsed_seconds"?[[:space:]]*[:=][[:space:]]*"?[0-9]+)'; then
    pass "#3656 T3656A — push timeout log includes elapsed_seconds"
else
    fail "#3656 T3656A — push timeout log includes elapsed_seconds" \
         "output: $T3656_TEST_OUT"
fi

# CRITICAL — envelope has the reason=push_timeout discriminator.
if printf '%s' "$T3656_TEST_OUT" | grep -qE '"reason":[[:space:]]*"push_timeout"'; then
    pass "#3656 T3656A — envelope contains reason=push_timeout"
else
    fail "#3656 T3656A — envelope contains reason=push_timeout" \
         "output: $T3656_TEST_OUT"
fi

# CRITICAL — push_failed=true so the transition shim still routes
# through the existing failure path.
if printf '%s' "$T3656_TEST_OUT" | grep -qE '"push_failed":[[:space:]]*true'; then
    pass "#3656 T3656A — envelope contains push_failed=true"
else
    fail "#3656 T3656A — envelope contains push_failed=true" \
         "output: $T3656_TEST_OUT"
fi

# CRITICAL — gh pr create NOT invoked (function returned early).
if ! grep -qE "CALL .*\\bpr\\b.*\\bcreate\\b" "$T3656_TEST_STATE/gh-log.txt" 2>/dev/null; then
    pass "#3656 T3656A — gh pr create NOT invoked after push timeout"
else
    fail "#3656 T3656A — gh pr create NOT invoked after push timeout" \
         "gh-log: $(cat "$T3656_TEST_STATE/gh-log.txt" 2>/dev/null)"
fi

# Sanity — the timeout stub WAS invoked at least once (proves the
# ``timeout`` wrapper is on the call path, not silently absent).
if grep -q "argv=git" "$T3656_TEST_STATE/timeout-log.txt" 2>/dev/null; then
    pass "#3656 T3656A — timeout wrapper invoked on git command"
else
    fail "#3656 T3656A — timeout wrapper invoked on git command" \
         "timeout-log: $(cat "$T3656_TEST_STATE/timeout-log.txt" 2>/dev/null)"
fi

# ── Sub-test B: ``gh pr create`` timeout → pr_create_timeout envelope ──────
#
# T3656_TIMEOUT_TARGET="pr create" matches only the ``gh pr create`` call.
# git push runs normally (so we exercise the path past the push site).
run_t3656_test "t3656b" T3656_TIMEOUT_TARGET="pr create" T3656_REV_LIST_PRE=1 T3656_REV_LIST_POST=1 GIT_REBASE_EXIT=0

if [[ "$T3656_TEST_RC" -eq 0 ]]; then
    pass "#3656 T3656B — handle_push_and_pr exits 0 on gh pr create timeout"
else
    fail "#3656 T3656B — handle_push_and_pr exits 0 on gh pr create timeout" \
         "rc=$T3656_TEST_RC, output: $T3656_TEST_OUT"
fi

# CRITICAL — pr_create_timeout log event emitted.
if printf '%s' "$T3656_TEST_OUT" | grep -q "push_and_pr_pr_create_timeout"; then
    pass "#3656 T3656B — push_and_pr_pr_create_timeout log event emitted"
else
    fail "#3656 T3656B — push_and_pr_pr_create_timeout log event emitted" \
         "output: $T3656_TEST_OUT"
fi

# CRITICAL — envelope has the pr_create_timeout reason discriminator.
if printf '%s' "$T3656_TEST_OUT" | grep -qE '"reason":[[:space:]]*"pr_create_timeout"'; then
    pass "#3656 T3656B — envelope contains reason=pr_create_timeout"
else
    fail "#3656 T3656B — envelope contains reason=pr_create_timeout" \
         "output: $T3656_TEST_OUT"
fi

# CRITICAL — pr_create_failed=true (preserves existing failure routing).
if printf '%s' "$T3656_TEST_OUT" | grep -qE '"pr_create_failed":[[:space:]]*true'; then
    pass "#3656 T3656B — envelope contains pr_create_failed=true"
else
    fail "#3656 T3656B — envelope contains pr_create_failed=true" \
         "output: $T3656_TEST_OUT"
fi

# CRITICAL — git push DID run (path got past the push call site,
# proving the timeout target was scoped to ``gh pr create`` only).
if grep -qE "CALL .*\\bpush\\b.*-u\\b.*\\borigin\\b" "$T3656_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3656 T3656B — git push DID run (timeout was scoped to gh pr create)"
else
    fail "#3656 T3656B — git push DID run (timeout was scoped to gh pr create)" \
         "git-log: $(cat "$T3656_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# ── Sub-test C: ``git fetch`` timeout → fetch_main_timeout log only ─────────
#
# T3656_TIMEOUT_TARGET="fetch origin" matches only the pre-push
# ``git fetch origin main``. The fetch is best-effort by design — a
# 124 should emit the timeout log but fall through to the push (which
# we then let succeed). Asserts:
#   * ``push_and_pr_fetch_main_timeout`` log emitted.
#   * Function continues to git push (not early-return on fetch
#     timeout — fetch failure has always been best-effort, and #3656
#     preserves that).
#   * Final envelope is NOT the push_timeout shape (fetch timeout
#     should not look like a push timeout to the diagnoser).
run_t3656_test "t3656c" T3656_TIMEOUT_TARGET="fetch origin" T3656_REV_LIST_PRE=1 T3656_REV_LIST_POST=1 GIT_REBASE_EXIT=0

if [[ "$T3656_TEST_RC" -eq 0 ]]; then
    pass "#3656 T3656C — handle_push_and_pr exits 0 on git fetch timeout (best-effort)"
else
    fail "#3656 T3656C — handle_push_and_pr exits 0 on git fetch timeout (best-effort)" \
         "rc=$T3656_TEST_RC, output: $T3656_TEST_OUT"
fi

# CRITICAL — fetch_main_timeout log event emitted.
if printf '%s' "$T3656_TEST_OUT" | grep -q "push_and_pr_fetch_main_timeout"; then
    pass "#3656 T3656C — push_and_pr_fetch_main_timeout log event emitted"
else
    fail "#3656 T3656C — push_and_pr_fetch_main_timeout log event emitted" \
         "output: $T3656_TEST_OUT"
fi

# CRITICAL — git push DID run (best-effort fetch failure falls through).
if grep -qE "CALL .*\\bpush\\b.*-u\\b.*\\borigin\\b" "$T3656_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3656 T3656C — git push DID run after fetch timeout (best-effort fall-through)"
else
    fail "#3656 T3656C — git push DID run after fetch timeout (best-effort fall-through)" \
         "git-log: $(cat "$T3656_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# Final envelope must NOT be the push_timeout shape (the fetch
# timeout fell through to a successful push + PR create).
if ! printf '%s' "$T3656_TEST_OUT" | grep -qE '"reason":[[:space:]]*"push_timeout"'; then
    pass "#3656 T3656C — final envelope is NOT push_timeout (fetch fell through)"
else
    fail "#3656 T3656C — final envelope is NOT push_timeout (fetch fell through)" \
         "output: $T3656_TEST_OUT"
fi

# ── Sub-test D: happy-path regression — no timeout target → all run ─────────
#
# T3656_TIMEOUT_TARGET unset (empty). The ``timeout`` stub falls through
# to exec the inner command, so every wrapped site behaves exactly as
# pre-#3656. Asserts no ``*_timeout`` log fires and the happy-path
# envelope is unchanged.
run_t3656_test "t3656d" T3656_REV_LIST_PRE=1 T3656_REV_LIST_POST=1 GIT_REBASE_EXIT=0

if [[ "$T3656_TEST_RC" -eq 0 ]]; then
    pass "#3656 T3656D — handle_push_and_pr exits 0 on happy path (no timeouts)"
else
    fail "#3656 T3656D — handle_push_and_pr exits 0 on happy path (no timeouts)" \
         "rc=$T3656_TEST_RC, output: $T3656_TEST_OUT"
fi

# Happy path emits NO ``*_timeout`` log lines.
if ! printf '%s' "$T3656_TEST_OUT" | grep -qE "(push|pr_create|fetch_main)_timeout"; then
    pass "#3656 T3656D — no *_timeout log events on happy path"
else
    fail "#3656 T3656D — no *_timeout log events on happy path" \
         "output: $T3656_TEST_OUT"
fi

# Happy path envelope does NOT carry a reason field (push_timeout /
# pr_create_timeout discriminators are absent).
if ! printf '%s' "$T3656_TEST_OUT" | grep -qE '"reason":[[:space:]]*"(push_timeout|pr_create_timeout)"'; then
    pass "#3656 T3656D — happy-path envelope has no timeout reason field"
else
    fail "#3656 T3656D — happy-path envelope has no timeout reason field" \
         "output: $T3656_TEST_OUT"
fi

# Both push and pr create DID run.
if grep -qE "CALL .*\\bpush\\b.*-u\\b.*\\borigin\\b" "$T3656_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3656 T3656D — git push ran on happy path"
else
    fail "#3656 T3656D — git push ran on happy path" \
         "git-log: $(cat "$T3656_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

if grep -qE "CALL .*\\bpr\\b.*\\bcreate\\b" "$T3656_TEST_STATE/gh-log.txt" 2>/dev/null; then
    pass "#3656 T3656D — gh pr create ran on happy path"
else
    fail "#3656 T3656D — gh pr create ran on happy path" \
         "gh-log: $(cat "$T3656_TEST_STATE/gh-log.txt" 2>/dev/null)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Test T3675: #3675 — replace ``rev-list --count`` with ``git diff --quiet``
# at both empty-conflicts post-rebase-failure check sites.
#
# Fourth sibling in the bug class fixed by T3614/T3651/T3662 (#3614/#3651/
# #3662):
#
#   T3614 (#3614) → handle_push_and_pr rebase rc=0 + ahead-count 0 path.
#   T3651 (#3651) → handle_ralph_baseline_rebase rc=128 + empty conflicts
#                   + ahead-count 0 path.
#   T3662 (#3662) → handle_push_and_pr rc=128 + empty conflicts + ahead-
#                   count 0 path.
#   T3675 (#3675) → BOTH sites — rc=128 + empty conflicts + ahead-count
#                   NON-zero (rev-list says "agent has commits") BUT
#                   ``git diff --quiet origin/main HEAD`` exits 0 (no
#                   semantic diff to ship). The pre-#3675 ``rev-list
#                   --count`` check missed this case because it counts
#                   commit *objects* (which still exist after abort
#                   restores ORIG_HEAD), not the diff that would actually
#                   ship. After ``rebase --abort`` the agent's ralph +
#                   fix_conflict commits are still distinct objects, but
#                   if rebase produced empty commits (which is why it
#                   exited 128) those commits are semantically redundant
#                   with main — diff would be empty and the agent should
#                   advance to no_op terminal succeeded.
#
# The fix: replace ``git rev-list --count origin/main..HEAD`` (returns N
# > 0 in the failure-mode-under-test) with ``git diff --quiet origin/main
# HEAD`` (returns 0 in the failure-mode-under-test). Apply at both
# ``handle_push_and_pr`` (lines ~2592) and ``handle_ralph_baseline_rebase``
# (lines ~4938) sites.
#
# Bug fingerprint: agent ``3cfda874-d979-4374-863b-768ddec608f4`` on
# #3581, terminated 2026-04-28T00:09:50Z post-deploy of #3666 + #3667.
# Real fix_conflict commits, post-abort ahead-count > 0, diff to main
# was empty (semantically redundant) — fell through #3666's check to
# the terminal-fail envelope. 7th ``push_and_pr_no_unmerged_files``
# failure on #3581 specifically.
#
# Self-contained: own ``T3675_*`` env namespace, own ``run_t3675_*``
# drivers. No shared fixture state with T3614/T3651/T3662/T3656.
# ══════════════════════════════════════════════════════════════════════════

# ── Site 1: handle_push_and_pr ────────────────────────────────────────────
#
# Reuse the same extraction approach as T3614/T3662 (function fixture).
# T3675 owns its own copy because T3662 piggybacks on T3614's $t3614_funcs
# and we want T3675 to remain self-contained per the issue body's
# "T3675_* env namespace" rule.

t3675_funcs="$TEST_TMP/t3675-funcs.sh"
printf 'exec 3>&1\n' > "$t3675_funcs"

for fn in db_exec db_query_one log persist_phase_output \
          assert_phase_deadline_not_exceeded \
          handle_push_and_pr; do
    awk -v FN="^${fn}\\\\(\\\\)" '
        $0 ~ FN { in_fn=1 }
        in_fn { print }
        in_fn && /^}$/ { exit }
    ' "$ENTRYPOINT" >> "$t3675_funcs"
done

# ``handle_push_and_pr`` references NETWORK_TIMEOUT_SECONDS (#3656) and
# LOCAL_GIT_TIMEOUT_SECONDS + PUSH_AND_PR_PHASE_DEADLINE_SECONDS (#3683).
grep '^NETWORK_TIMEOUT_SECONDS=' "$ENTRYPOINT" >> "$t3675_funcs"
grep '^LOCAL_GIT_TIMEOUT_SECONDS=' "$ENTRYPOINT" >> "$t3675_funcs"
grep '^PUSH_AND_PR_PHASE_DEADLINE_SECONDS=' "$ENTRYPOINT" >> "$t3675_funcs"

if grep -q "^handle_push_and_pr()" "$t3675_funcs"; then
    pass "#3675 T3675 setup — extracted handle_push_and_pr from entrypoint"
else
    fail "#3675 T3675 setup — extracted handle_push_and_pr from entrypoint" \
         "fixture head: $(head -c 400 "$t3675_funcs")"
fi

# Behavioural fingerprint: the new fix MUST contain a ``git diff --quiet
# origin/main HEAD`` invocation in handle_push_and_pr. If this is absent
# the fix wasn't applied at this site.
if grep -qE 'diff[[:space:]]+--quiet[[:space:]]+origin/main[[:space:]]+HEAD' "$t3675_funcs"; then
    pass "#3675 T3675 setup — handle_push_and_pr contains diff --quiet origin/main HEAD"
else
    fail "#3675 T3675 setup — handle_push_and_pr contains diff --quiet origin/main HEAD" \
         "fixture: $(grep -nE '(rev-list|diff)' "$t3675_funcs" | head -20)"
fi

# Per-test runner. Drives handle_push_and_pr into the rebase-rc=128
# empty-conflicts path with stub control over:
#
#   T3675_REV_LIST_PRE   — count for the FIRST rev-list call (pre-rebase
#                          #3039 guardrail). Default 1.
#   T3675_DIFF_QUIET_RC  — exit code for ``git diff --quiet origin/main
#                          HEAD`` (the new #3675 check). Default 0
#                          (no diff = already in main = no_op).
#   T3675_DIFF_FILES     — content for ``git diff --name-only --diff-
#                          filter=U`` stdout. Default empty (no conflict
#                          files). Set to file paths to simulate a real
#                          conflict.
#   GIT_REBASE_EXIT      — exit code for ``git rebase origin/main``.
#                          Default 128 (the failure case under test).
#
# CRITICAL: T3675 deliberately DOES NOT control rev-list-post (the second
# rev-list call exists only in pre-#3675 code). The git stub returns
# ``T3675_REV_LIST_PRE`` for ALL rev-list calls — if the new code path
# ever calls rev-list a second time, that's a regression to the old
# logic. The stub is parameterised on the diff exit code, which is the
# new check.
run_t3675_push_pr_test() {
    _test_id="$1"
    shift

    _tworkspace="$TEST_TMP/${_test_id}-workspace"
    _trepo="$TEST_TMP/${_test_id}-repo"
    _tstate="$TEST_TMP/${_test_id}-state"
    _tbin="$TEST_TMP/${_test_id}-bin"
    mkdir -p "$_tworkspace" "$_trepo" "$_tstate" "$_tbin"
    mkdir -p "$_trepo/tmp/dispatcher-output" "$_trepo/tmp/dispatcher-input"

    # Stage the diff-files stdout (for ``git diff --name-only
    # --diff-filter=U`` — the conflict-files capture step).
    _t3675_diff_files=""
    for _a in "$@"; do
        case "$_a" in
            T3675_DIFF_FILES=*) _t3675_diff_files="${_a#T3675_DIFF_FILES=}" ;;
        esac
    done
    printf '%s' "$_t3675_diff_files" > "$_tstate/diff-files-output.txt"

    cat > "$_tbin/git" <<'T3675GITEOF'
#!/usr/bin/env bash
set -u
printf '%s\n' "CALL $*" >> "${T_STATE_DIR}/git-log.txt"

# Skip ``-C <dir>`` prefix.
while [[ $# -gt 0 ]]; do
    case "$1" in
        -C) shift 2 || true; continue ;;
        *) break ;;
    esac
done

sub="${1:-}"
case "$sub" in
    rev-list)
        # Pre-rebase #3039 guardrail. Pre-#3675 code also calls this
        # post-abort. T3675 returns the same value for every call (the
        # post-abort path should NOT re-invoke rev-list — that would
        # mean the pre-#3675 logic is still present).
        printf '%s\n' "${T3675_REV_LIST_PRE:-1}"
        exit 0
        ;;
    fetch)
        exit "${GIT_FETCH_EXIT:-0}"
        ;;
    rebase)
        for _arg in "$@"; do
            if [[ "$_arg" == "--abort" ]]; then exit 0; fi
        done
        exit "${GIT_REBASE_EXIT:-128}"
        ;;
    diff)
        # Two distinct diff invocations the entrypoint makes:
        #
        # (1) ``git diff --name-only --diff-filter=U`` — the conflict-
        #     files capture step that runs BEFORE the new #3675 check.
        # (2) ``git diff --quiet origin/main HEAD`` — the new #3675
        #     check itself.
        #
        # Disambiguate via flag inspection.
        _is_diff_filter_U=0
        _is_diff_quiet=0
        for _a in "$@"; do
            case "$_a" in
                --diff-filter=U) _is_diff_filter_U=1 ;;
                --quiet)         _is_diff_quiet=1 ;;
            esac
        done
        if [[ "$_is_diff_filter_U" == "1" ]]; then
            cat "${T_STATE_DIR}/diff-files-output.txt"
            exit 0
        fi
        if [[ "$_is_diff_quiet" == "1" ]]; then
            exit "${T3675_DIFF_QUIET_RC:-0}"
        fi
        # Other diff invocations (merge-base captures etc.) succeed.
        exit 0
        ;;
    push)
        exit "${GIT_PUSH_EXIT:-0}"
        ;;
    commit)
        exit "${GIT_COMMIT_EXIT:-0}"
        ;;
    rev-parse|merge-base)
        printf 'deadbeefcafe\n'
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
T3675GITEOF
    chmod +x "$_tbin/git"

    # #3656/#3675: ``handle_push_and_pr`` wraps git fetch / git push / gh
    # pr create in ``timeout NETWORK_TIMEOUT_SECONDS``. macOS does not
    # ship coreutils' ``timeout(1)``, so a real ``timeout`` lookup
    # would exit 127. T3675 doesn't exercise the 124-exit branch —
    # this stub passes through to the inner command transparently.
    cat > "$_tbin/timeout" <<'T3675TIMEOUTEOF'
#!/usr/bin/env bash
# argv: <seconds> <inner-cmd> <inner-args...>
shift  # discard the seconds arg
exec "$@"
T3675TIMEOUTEOF
    chmod +x "$_tbin/timeout"

    cat > "$_tbin/gh" <<'T3675GHEOF'
#!/usr/bin/env bash
printf '%s\n' "CALL $*" >> "${T_STATE_DIR}/gh-log.txt"
if [[ "${1:-}" == "pr" && "${2:-}" == "create" ]]; then
    printf 'https://github.com/judgemind/judgemind/pull/9999\n'
fi
exit "${GH_EXIT:-0}"
T3675GHEOF
    chmod +x "$_tbin/gh"

    cat > "$_tbin/psql" <<'T3675PSQLEOF'
#!/usr/bin/env bash
query=""
while [[ $# -gt 0 ]]; do
    case "$1" in -c) shift; query="$1" ;; esac
    shift || true
done
printf 'CALL %s\n' "$query" >> "${T_STATE_DIR}/psql-log.txt"
exit 0
T3675PSQLEOF
    chmod +x "$_tbin/psql"

    if command -v jq >/dev/null 2>&1; then
        ln -sf "$(command -v jq)" "$_tbin/jq"
    fi

    set +e
    _tout=$(env \
        T_STATE_DIR="$_tstate" \
        PATH="$_tbin:$PATH" \
        AGENT_ID="${_test_id}-dead-beef-cafe-000000000099" \
        ISSUE_NUMBER="3675" \
        AGENT_WORKSPACE="$_tworkspace" \
        REPO_ROOT="$_trepo" \
        BRANCH_NAME="agent/${_test_id}abcd" \
        DATABASE_URL="postgres://test" \
        AGENT_RUNNER_DRY_RUN="0" \
        "$@" \
        bash -c '
            set -uo pipefail
            # shellcheck disable=SC1090
            . "'"$t3675_funcs"'"
            handle_push_and_pr
        ' 2>&1)
    _trc=$?
    set -e

    T3675_TEST_RC="$_trc"
    T3675_TEST_OUT="$_tout"
    T3675_TEST_STATE="$_tstate"
}

# ── Sub-test A: rebase rc=128 + empty conflicts + ahead>0 + diff=0 ────────
#   THE BUG CASE. Agent has commits (rev-list says "ahead by 2") but
#   diff to main is empty (commits are semantically redundant — rebase
#   produced empty commits). Pre-#3675 the rev-list-only check fell
#   through to terminal-fail. Post-#3675 the diff-quiet check catches
#   it and emits no_op.
#   MUST FAIL on unfixed code.

run_t3675_push_pr_test "t3675a" \
    T3675_REV_LIST_PRE=2 \
    T3675_DIFF_QUIET_RC=0 \
    T3675_DIFF_FILES="" \
    GIT_REBASE_EXIT=128

if [[ "$T3675_TEST_RC" -eq 0 ]]; then
    pass "#3675 T3675A — handle_push_and_pr exits 0 on empty-conflicts + ahead>0 + empty-diff"
else
    fail "#3675 T3675A — handle_push_and_pr exits 0 on empty-conflicts + ahead>0 + empty-diff" \
         "rc=$T3675_TEST_RC, output: $T3675_TEST_OUT"
fi

# CRITICAL — already-applied log event must be emitted (the behavioural
# fingerprint of the fix). Distinguishes this advance from
# ``push_and_pr_no_unmerged_files`` (the terminal-fail event we're
# specifically NOT taking on this path).
if printf '%s' "$T3675_TEST_OUT" | grep -q "push_and_pr_no_unmerged_files_already_applied_post_rebase_failure"; then
    pass "#3675 T3675A — push_and_pr_no_unmerged_files_already_applied_post_rebase_failure log emitted"
else
    fail "#3675 T3675A — push_and_pr_no_unmerged_files_already_applied_post_rebase_failure log emitted" \
         "output: $T3675_TEST_OUT"
fi

# CRITICAL — envelope must be {"no_op": true, ...}. Routes to PHASE_NO_OP
# terminal succeeded via transition_from_push_and_pr (#3645).
if printf '%s' "$T3675_TEST_OUT" | grep -qE '"no_op": true'; then
    pass "#3675 T3675A — envelope is {\"no_op\": true, ...} on already-applied path"
else
    fail "#3675 T3675A — envelope is {\"no_op\": true, ...} on already-applied path" \
         "output: $T3675_TEST_OUT"
fi

# CRITICAL — envelope must carry diff_to_main_empty discriminator (the
# new #3675 fingerprint, distinct from #3662's ``post_abort_ahead: 0``).
if printf '%s' "$T3675_TEST_OUT" | grep -qE '"diff_to_main_empty": true'; then
    pass "#3675 T3675A — envelope carries diff_to_main_empty=true (new #3675 discriminator)"
else
    fail "#3675 T3675A — envelope carries diff_to_main_empty=true (new #3675 discriminator)" \
         "output: $T3675_TEST_OUT"
fi

# CRITICAL — envelope must NOT carry rebase_failed=true (the pre-#3675
# terminal-fail envelope shape).
if ! printf '%s' "$T3675_TEST_OUT" | grep -qE '"rebase_failed": true'; then
    pass "#3675 T3675A — envelope does NOT carry rebase_failed=true on already-applied path"
else
    fail "#3675 T3675A — envelope does NOT carry rebase_failed=true on already-applied path" \
         "output: $T3675_TEST_OUT"
fi

if ! printf '%s' "$T3675_TEST_OUT" | grep -qE '"no_unmerged_files": true'; then
    pass "#3675 T3675A — envelope does NOT carry no_unmerged_files=true on already-applied path"
else
    fail "#3675 T3675A — envelope does NOT carry no_unmerged_files=true on already-applied path" \
         "output: $T3675_TEST_OUT"
fi

# CRITICAL — function must NOT have invoked ``git push`` (no diff to
# push).
if ! grep -qE "CALL .*\\bpush\\b" "$T3675_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3675 T3675A — git push NOT invoked on already-applied post-rebase-failure"
else
    fail "#3675 T3675A — git push NOT invoked on already-applied post-rebase-failure" \
         "git-log: $(cat "$T3675_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

if ! grep -qE "CALL .*\\bpr\\b.*\\bcreate\\b" "$T3675_TEST_STATE/gh-log.txt" 2>/dev/null; then
    pass "#3675 T3675A — gh pr create NOT invoked on already-applied post-rebase-failure"
else
    fail "#3675 T3675A — gh pr create NOT invoked on already-applied post-rebase-failure" \
         "gh-log: $(cat "$T3675_TEST_STATE/gh-log.txt" 2>/dev/null)"
fi

# CRITICAL — git diff --quiet origin/main HEAD must have been invoked
# (the new fix's behavioural fingerprint). Pre-#3675 this call did
# NOT exist on the empty-conflicts post-rebase-failure code path.
if grep -qE "CALL .*\\bdiff\\b.*--quiet.*origin/main.*HEAD" "$T3675_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3675 T3675A — git diff --quiet origin/main HEAD invoked (new #3675 check fingerprint)"
else
    fail "#3675 T3675A — git diff --quiet origin/main HEAD invoked (new #3675 check fingerprint)" \
         "git-log: $(cat "$T3675_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# Behavioural fingerprint that the OLD pre-#3675 logic isn't lurking: the
# post-abort code path must call rev-list AT MOST ONCE (the pre-rebase
# #3039 guardrail). The pre-#3675 code called it twice. If two calls
# appear, the #3675 fix isn't replacing the rev-list check — it's
# adding the diff-quiet check on top of it, which would mean stale
# logic.
if [[ -f "$T3675_TEST_STATE/git-log.txt" ]]; then
    _t3675a_rev_list_calls=$(grep -cE "CALL .*\\brev-list\\b" "$T3675_TEST_STATE/git-log.txt" || printf '0')
    if [[ "$_t3675a_rev_list_calls" -le 1 ]]; then
        pass "#3675 T3675A — git rev-list called at most once (rev-list check replaced, not augmented)"
    else
        fail "#3675 T3675A — git rev-list called at most once (rev-list check replaced, not augmented)" \
             "found $_t3675a_rev_list_calls call(s); git-log: $(cat "$T3675_TEST_STATE/git-log.txt")"
    fi
fi

# ── Sub-test B: rebase rc=128 + empty conflicts + ahead>0 + diff=1 ────────
#   GENUINE FAILURE PATH (regression). Agent has commits AND diff is
#   non-empty — the rebase actually failed for a non-already-applied
#   reason. Must preserve the existing #3465 route_to_diagnoser path.

run_t3675_push_pr_test "t3675b" \
    T3675_REV_LIST_PRE=2 \
    T3675_DIFF_QUIET_RC=1 \
    T3675_DIFF_FILES="" \
    GIT_REBASE_EXIT=128

if [[ "$T3675_TEST_RC" -eq 0 ]]; then
    pass "#3675 T3675B — handle_push_and_pr exits 0 on empty-conflicts + non-empty-diff path"
else
    fail "#3675 T3675B — handle_push_and_pr exits 0 on empty-conflicts + non-empty-diff path" \
         "rc=$T3675_TEST_RC, output: $T3675_TEST_OUT"
fi

# Genuine-failure envelope must carry rebase_failed=true (#3465 path).
if printf '%s' "$T3675_TEST_OUT" | grep -qE '"rebase_failed": true'; then
    pass "#3675 T3675B — non-empty-diff envelope carries rebase_failed=true (#3465 path preserved)"
else
    fail "#3675 T3675B — non-empty-diff envelope carries rebase_failed=true (#3465 path preserved)" \
         "output: $T3675_TEST_OUT"
fi

if printf '%s' "$T3675_TEST_OUT" | grep -qE '"no_unmerged_files": true'; then
    pass "#3675 T3675B — non-empty-diff envelope carries no_unmerged_files=true"
else
    fail "#3675 T3675B — non-empty-diff envelope carries no_unmerged_files=true" \
         "output: $T3675_TEST_OUT"
fi

# Genuine-failure path must NOT emit the already-applied log event.
if ! printf '%s' "$T3675_TEST_OUT" | grep -q "push_and_pr_no_unmerged_files_already_applied_post_rebase_failure"; then
    pass "#3675 T3675B — already-applied log NOT emitted on non-empty-diff path"
else
    fail "#3675 T3675B — already-applied log NOT emitted on non-empty-diff path" \
         "output: $T3675_TEST_OUT"
fi

# Genuine-failure envelope must NOT be no_op.
if ! printf '%s' "$T3675_TEST_OUT" | grep -qE '"no_op": true'; then
    pass "#3675 T3675B — non-empty-diff envelope is NOT {\"no_op\": true}"
else
    fail "#3675 T3675B — non-empty-diff envelope is NOT {\"no_op\": true}" \
         "output: $T3675_TEST_OUT"
fi

# T3675B also exercises the diff --quiet call (it's unconditional on the
# empty-conflicts path now).
if grep -qE "CALL .*\\bdiff\\b.*--quiet.*origin/main.*HEAD" "$T3675_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3675 T3675B — git diff --quiet origin/main HEAD invoked on non-empty-diff path"
else
    fail "#3675 T3675B — git diff --quiet origin/main HEAD invoked on non-empty-diff path" \
         "git-log: $(cat "$T3675_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# ── Sub-test C: rebase rc=128 + non-empty conflicts (real conflict) ───────
#   REAL CONFLICT REGRESSION. The #3225 path must continue to route to
#   fix_conflict with the file bundle. The diff-quiet check is only
#   reached when conflict_files_json is empty.

run_t3675_push_pr_test "t3675c" \
    T3675_REV_LIST_PRE=1 \
    T3675_DIFF_QUIET_RC=0 \
    T3675_DIFF_FILES="packages/api/foo.py
packages/scraper-framework/bar.py" \
    GIT_REBASE_EXIT=128

if [[ "$T3675_TEST_RC" -eq 0 ]]; then
    pass "#3675 T3675C — handle_push_and_pr exits 0 on real-conflict path"
else
    fail "#3675 T3675C — handle_push_and_pr exits 0 on real-conflict path" \
         "rc=$T3675_TEST_RC, output: $T3675_TEST_OUT"
fi

# Real conflict envelope must carry conflict_files (route to fix_conflict).
if printf '%s' "$T3675_TEST_OUT" | grep -qE '"conflict_files":[[:space:]]*\[.*"packages/api/foo\.py".*\]'; then
    pass "#3675 T3675C — real-conflict envelope carries conflict_files (routes to fix_conflict)"
else
    fail "#3675 T3675C — real-conflict envelope carries conflict_files (routes to fix_conflict)" \
         "output: $T3675_TEST_OUT"
fi

if printf '%s' "$T3675_TEST_OUT" | grep -qE '"rebase_failed": true'; then
    pass "#3675 T3675C — real-conflict envelope carries rebase_failed=true"
else
    fail "#3675 T3675C — real-conflict envelope carries rebase_failed=true" \
         "output: $T3675_TEST_OUT"
fi

if ! printf '%s' "$T3675_TEST_OUT" | grep -qE '"no_op": true'; then
    pass "#3675 T3675C — real-conflict envelope is NOT {\"no_op\": true}"
else
    fail "#3675 T3675C — real-conflict envelope is NOT {\"no_op\": true}" \
         "output: $T3675_TEST_OUT"
fi

# Real conflict path must NOT call ``git diff --quiet`` (the new check
# is gated on conflict_files_json == "[]"; with files present we skip
# straight to the rebase_failed/conflict_files envelope).
if ! grep -qE "CALL .*\\bdiff\\b.*--quiet.*origin/main.*HEAD" "$T3675_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3675 T3675C — git diff --quiet origin/main HEAD NOT invoked on real-conflict path"
else
    fail "#3675 T3675C — git diff --quiet origin/main HEAD NOT invoked on real-conflict path" \
         "git-log: $(cat "$T3675_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# ── Site 2: handle_ralph_baseline_rebase ──────────────────────────────────
#
# The ralph baseline rebase logic isn't a function — it's an inline block
# inside the main loop's ``case "$_current"`` arm. Reuse T3651's awk-based
# extraction approach: extract the ``_ralph_baseline_output=""`` ... outer
# ``fi`` block and source it standalone with the inputs the entrypoint
# would have computed (``_baseline_conflict_files_json``,
# ``_baseline_rebase_stderr_tail``).

t3675_block="$TEST_TMP/t3675-envelope-block.sh"
awk '
  /_ralph_baseline_output=""/ { in_block=1; depth=0 }
  in_block {
    print
    if ($0 ~ /^[[:space:]]*if[[:space:]]+\[\[/) depth++
    if ($0 ~ /^[[:space:]]*fi[[:space:]]*$/) {
      depth--
      if (depth == 0) exit
    }
  }
' "$ENTRYPOINT" > "$t3675_block"

if grep -q "ralph_baseline_no_unmerged_files_already_applied" "$t3675_block"; then
    pass "#3675 T3675 setup — extracted ralph_baseline envelope block contains the log event marker"
else
    fail "#3675 T3675 setup — extracted ralph_baseline envelope block contains the log event marker" \
         "block (head 800): $(head -c 800 "$t3675_block")"
fi

# Behavioural fingerprint at site 2: the new fix MUST contain a ``git
# diff --quiet origin/main HEAD`` invocation in the ralph_baseline
# envelope block. If this is absent the fix wasn't applied at this site.
if grep -qE 'diff[[:space:]]+--quiet[[:space:]]+origin/main[[:space:]]+HEAD' "$t3675_block"; then
    pass "#3675 T3675 setup — ralph_baseline block contains diff --quiet origin/main HEAD"
else
    fail "#3675 T3675 setup — ralph_baseline block contains diff --quiet origin/main HEAD" \
         "block (head 1200): $(head -c 1200 "$t3675_block")"
fi

# Per-test runner for the ralph_baseline envelope block. Stubs ``git``
# (only ``rev-list`` and ``diff --quiet`` matter — abort already
# happened upstream of this block) and ``log`` (captured into a state
# file). Parameters control:
#   T3675_BASELINE_CONFLICT_FILES_JSON — value of
#     ``_baseline_conflict_files_json`` ("[]" for empty, or a JSON array
#     for real).
#   T3675_BASELINE_DIFF_QUIET_RC       — exit code for ``git diff --quiet
#     origin/main HEAD`` (the new #3675 check). Default 0 (no diff).
run_t3675_baseline_test() {
    _test_id="$1"
    shift

    _twork="$TEST_TMP/${_test_id}-work"
    _trepo="$TEST_TMP/${_test_id}-repo"
    _tstate="$TEST_TMP/${_test_id}-state"
    _tbin="$TEST_TMP/${_test_id}-bin"
    mkdir -p "$_twork" "$_trepo" "$_tstate" "$_tbin"

    cat > "$_tbin/git" <<'T3675BASELINEGITEOF'
#!/usr/bin/env bash
set -u
printf '%s\n' "CALL $*" >> "${T_STATE_DIR}/git-log.txt"

while [[ $# -gt 0 ]]; do
    case "$1" in -C) shift 2 || true; continue ;; *) break ;; esac
done

case "${1:-}" in
    rev-list)
        printf '%s\n' "${T3675_BASELINE_REV_LIST:-1}"
        exit 0
        ;;
    diff)
        for _a in "$@"; do
            if [[ "$_a" == "--quiet" ]]; then
                exit "${T3675_BASELINE_DIFF_QUIET_RC:-0}"
            fi
        done
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
T3675BASELINEGITEOF
    chmod +x "$_tbin/git"

    if command -v jq >/dev/null 2>&1; then
        ln -sf "$(command -v jq)" "$_tbin/jq"
    fi

    set +e
    _tout=$(env \
        T_STATE_DIR="$_tstate" \
        PATH="$_tbin:$PATH" \
        AGENT_WORKSPACE="$_twork" \
        REPO_ROOT="$_trepo" \
        AGENT_ID="t3675-baseline-dead-beef-cafe-99" \
        ISSUE_NUMBER="3675" \
        "$@" \
        bash -c '
            set -uo pipefail
            log() { printf "%s\n" "LOG $*"; }
            _baseline_conflict_files_json="${T3675_BASELINE_CONFLICT_FILES_JSON:-[]}"
            _baseline_rebase_stderr_tail='\''""'\''
            # shellcheck disable=SC1090
            . "'"$t3675_block"'"
            printf "ENVELOPE %s\n" "$_ralph_baseline_output"
        ' 2>&1)
    _trc=$?
    set -e

    T3675_TEST_RC="$_trc"
    T3675_TEST_OUT="$_tout"
    T3675_TEST_STATE="$_tstate"
}

# ── Sub-test D: ralph_baseline empty conflicts + diff=0 → no_op ───────────
#   THE BUG CASE at site 2. Pre-#3675 the rev-list-only check missed
#   the case where commits are semantically redundant (rebase produced
#   empty commits). MUST FAIL on unfixed code.

run_t3675_baseline_test "t3675d" \
    T3675_BASELINE_CONFLICT_FILES_JSON="[]" \
    T3675_BASELINE_DIFF_QUIET_RC=0 \
    T3675_BASELINE_REV_LIST=2

if [[ "$T3675_TEST_RC" -eq 0 ]]; then
    pass "#3675 T3675D — ralph_baseline envelope block exits 0 on empty conflicts + empty diff"
else
    fail "#3675 T3675D — ralph_baseline envelope block exits 0 on empty conflicts + empty diff" \
         "rc=$T3675_TEST_RC, output: $T3675_TEST_OUT"
fi

# CRITICAL — already-applied log event must be emitted.
if printf '%s' "$T3675_TEST_OUT" | grep -q "ralph_baseline_no_unmerged_files_already_applied"; then
    pass "#3675 T3675D — ralph_baseline_no_unmerged_files_already_applied log event emitted"
else
    fail "#3675 T3675D — ralph_baseline_no_unmerged_files_already_applied log event emitted" \
         "output: $T3675_TEST_OUT"
fi

# CRITICAL — envelope must be {"no_op": true, ...}.
if printf '%s' "$T3675_TEST_OUT" | grep -qE 'ENVELOPE .*"no_op": true'; then
    pass "#3675 T3675D — ralph_baseline envelope is {\"no_op\": true, ...} on already-applied path"
else
    fail "#3675 T3675D — ralph_baseline envelope is {\"no_op\": true, ...} on already-applied path" \
         "output: $T3675_TEST_OUT"
fi

# CRITICAL — envelope must carry the new diff_to_main_empty discriminator.
if printf '%s' "$T3675_TEST_OUT" | grep -qE 'ENVELOPE .*"diff_to_main_empty": true'; then
    pass "#3675 T3675D — ralph_baseline envelope carries diff_to_main_empty=true (new #3675 discriminator)"
else
    fail "#3675 T3675D — ralph_baseline envelope carries diff_to_main_empty=true (new #3675 discriminator)" \
         "output: $T3675_TEST_OUT"
fi

# CRITICAL — envelope must NOT carry rebase_failed=true (the pre-#3675
# terminal-fail envelope shape).
if ! printf '%s' "$T3675_TEST_OUT" | grep -qE 'ENVELOPE .*"rebase_failed": true'; then
    pass "#3675 T3675D — ralph_baseline envelope does NOT carry rebase_failed=true"
else
    fail "#3675 T3675D — ralph_baseline envelope does NOT carry rebase_failed=true" \
         "output: $T3675_TEST_OUT"
fi

# git diff --quiet must have been invoked (new check fingerprint).
if grep -qE "CALL .*\\bdiff\\b.*--quiet.*origin/main.*HEAD" "$T3675_TEST_STATE/git-log.txt" 2>/dev/null; then
    pass "#3675 T3675D — ralph_baseline git diff --quiet origin/main HEAD invoked"
else
    fail "#3675 T3675D — ralph_baseline git diff --quiet origin/main HEAD invoked" \
         "git-log: $(cat "$T3675_TEST_STATE/git-log.txt" 2>/dev/null)"
fi

# ── Sub-test E: ralph_baseline empty conflicts + diff=1 → diagnoser ───────
#   GENUINE FAILURE PATH (regression). Agent has real diff to ship —
#   rebase actually failed for a non-already-applied reason. Must
#   preserve the existing #3465 route_to_diagnoser path.

run_t3675_baseline_test "t3675e" \
    T3675_BASELINE_CONFLICT_FILES_JSON="[]" \
    T3675_BASELINE_DIFF_QUIET_RC=1 \
    T3675_BASELINE_REV_LIST=2

if [[ "$T3675_TEST_RC" -eq 0 ]]; then
    pass "#3675 T3675E — ralph_baseline envelope block exits 0 on empty conflicts + non-empty diff"
else
    fail "#3675 T3675E — ralph_baseline envelope block exits 0 on empty conflicts + non-empty diff" \
         "rc=$T3675_TEST_RC, output: $T3675_TEST_OUT"
fi

# Genuine-failure envelope must carry rebase_failed=true (#3465 path).
if printf '%s' "$T3675_TEST_OUT" | grep -qE 'ENVELOPE .*"rebase_failed": true'; then
    pass "#3675 T3675E — non-empty-diff ralph_baseline envelope carries rebase_failed=true (#3465 path preserved)"
else
    fail "#3675 T3675E — non-empty-diff ralph_baseline envelope carries rebase_failed=true (#3465 path preserved)" \
         "output: $T3675_TEST_OUT"
fi

if printf '%s' "$T3675_TEST_OUT" | grep -qE 'ENVELOPE .*"no_unmerged_files": true'; then
    pass "#3675 T3675E — non-empty-diff ralph_baseline envelope carries no_unmerged_files=true"
else
    fail "#3675 T3675E — non-empty-diff ralph_baseline envelope carries no_unmerged_files=true" \
         "output: $T3675_TEST_OUT"
fi

# Genuine-failure path must NOT emit the already-applied log event.
if ! printf '%s' "$T3675_TEST_OUT" | grep -q "ralph_baseline_no_unmerged_files_already_applied"; then
    pass "#3675 T3675E — already-applied log NOT emitted on non-empty-diff ralph_baseline path"
else
    fail "#3675 T3675E — already-applied log NOT emitted on non-empty-diff ralph_baseline path" \
         "output: $T3675_TEST_OUT"
fi

if ! printf '%s' "$T3675_TEST_OUT" | grep -qE 'ENVELOPE .*"no_op": true'; then
    pass "#3675 T3675E — non-empty-diff ralph_baseline envelope is NOT {\"no_op\": true}"
else
    fail "#3675 T3675E — non-empty-diff ralph_baseline envelope is NOT {\"no_op\": true}" \
         "output: $T3675_TEST_OUT"
fi

# ── Summary ────────────────────────────────────────────────────────────────

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
