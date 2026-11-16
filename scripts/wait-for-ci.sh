#!/usr/bin/env bash
# wait-for-ci.sh — Poll a PR's check-runs until CI is definitively green or
# definitively failed, then emit a structured exit code the caller can act on.
#
# # venv: none
# # permanent: true
#
# ── Why check-runs?per_page=100&filter=latest — not the alternatives ─────────
#
# Several seemingly equivalent GitHub API surfaces are unreliable for this
# purpose. Empirically documented in #3897:
#
# 1. `gh run view --json status` — rolls up workflow-level status. A workflow
#    transitions to `completed` as soon as all its *jobs* are finished, but
#    GitHub's rollup can briefly report `completed` while sibling jobs in other
#    workflows are still queued. Misleading for multi-workflow repos.
#
# 2. `gh run list --json status` — same workflow-level rollup lag. Listing all
#    runs and checking status has the same blind spot: each row is one workflow
#    run, not one check.
#
# 3. `gh api repos/.../check-runs --paginate` — the paginate flag sends N
#    separate page requests. Because GitHub can insert new check-runs (re-runs)
#    between page fetches, a long-running re-run can appear on page 2 as
#    `in_progress` while page 1 already returned the old completed entry.
#    Inconsistent page splits produce false-positive "all passed" reads.
#
# 4. `gh pr view --json mergeStateStatus` — GitHub computes `mergeStateStatus`
#    over the *entire history* of check runs on the head SHA (not the latest
#    per-check attempt). A stale failed run from an earlier CI attempt keeps
#    the PR in `UNSTABLE` forever even after a successful re-run flips the
#    rollup green. Querying it as the primary CI gate produces false negatives
#    for every PR that ever had a flaky check re-run. It lags job-state changes
#    by ~10 minutes due to GitHub's PR rollup cache. (Still useful as a
#    secondary guard after check-runs confirm green — see §Success below.)
#
# The canonical approach: `GET /repos/{owner}/{repo}/commits/{sha}/check-runs
# ?per_page=100&filter=latest` returns exactly one entry per unique check name
# — the most recent run — in a single non-paginated response (GitHub caps at
# 100 checks per commit; repos with >100 checks need --paginate but that is an
# edge case addressed by the per_page=100 guard). No page-split races, no
# stale history.
#
# filter=latest dedupes only within one check suite (one workflow run). A
# check re-run by a non-push event (pull_request `edited` / `reopened`, a
# manual dispatch) lands in a new suite on the same SHA, so the old run is
# still listed. The poll loop therefore keeps only the newest run (highest
# check-run id) per (check name, app) before it counts failures (#5078): a
# failure replaced by a newer run of the same check does not block, and a
# newer run still in progress makes the script wait for its verdict. A
# newest run that was cancelled is not a verdict: the check falls back to
# its newest completed, non-cancelled run, so a cancelled re-run cannot hide
# an earlier failure (#5097).
#
# ── Usage ────────────────────────────────────────────────────────────────────
#
# Usage: scripts/wait-for-ci.sh <pr-number> [options]
#
# Arguments:
#   <pr-number>         PR number to monitor (required unless --help)
#
# Options:
#   --timeout-secs N    How long this call waits, in seconds (default: 480).
#                       Kept under the Bash tool's 600s cap so an agent's
#                       call never gets moved to the background (#4835).
#                       When it runs out, exit 124 means "CI still running:
#                       re-run the same command". Re-running is cheap: it
#                       only reads state and never re-triggers CI.
#   --poll-interval N   Polling interval in seconds (default: 30)
#   --repo OWNER/REPO   GitHub repository (default: judgemind/judgemind)
#   --no-auto-rerun     Disable the known-flake auto-rerun classifier (#4148).
#                       When set, any failed check exits 1 immediately.
#   --help              Print this help and exit 0
#
# Exit codes:
#   0 — Success: the PR is mergeable through the protected path. Requires
#       ALL of (#4965): no `ci-passed` check run on the head SHA is still
#       in progress, the newest `ci-passed` run is success, no check's newest
#       run has a hard-failure conclusion (failure, timed_out,
#       action_required), no re-run of an earlier failed check is still
#       going (#5078), no PR-text check (`closing-refs`, `validate-pr`)
#       is still running (#5090), and mergeStateStatus is CLEAN, UNSTABLE or
#       HAS_HOOKS (BLOCKED keeps waiting: GitHub's branch protection is not satisfied yet, so
#       `gh pr merge` would refuse). Pending entries from other, non-required
#       check names (Vercel, smoke tests) and `cancelled` checks do not
#       block (#4407). Stdout names the path:
#       (a) `canonical merge gate green` when mergeable == MERGEABLE;
#       (b) `all checks complete` when mergeable cannot be resolved but
#           every check has drained to a terminal state.
#   1 — Early failure: one or more checks have a hard failed conclusion
#       (failure, timed_out, action_required). Prints the failed check
#       names and their details_url. **`cancelled` is NOT a hard failure**
#       — it routinely surfaces from Vercel / smoke-test
#       `concurrency: cancel-in-progress` guards and from manual
#       `gh run cancel` actions, neither of which reflects a real test
#       failure (#4407). Cancelled entries are listed in the per-poll
#       status line but never trigger exit 1.
#       Before exiting, the failed jobs' logs are passed to
#       `scripts/classify-ci-flake.sh`. If any classifies as a known flake
#       AND no rerun has fired on this run yet, `gh run rerun <run-id>
#       --failed` is invoked once and polling continues (path stdout names
#       the matched pattern with `flake detected: <label>`). A second
#       flake on the same run exits 1 — see #4148.
#   124 — Still running: --timeout-secs elapsed before CI reached a
#       terminal state. Not a failure. Re-run the same command to keep
#       waiting (#4835). The flake auto-rerun sentinel is keyed on the
#       head SHA, so a re-run never fires a second `gh run rerun`.
#   125 — Status unknown: the check-runs read failed (or hung past its
#       per-call limit) 3 polls in a row. Check `gh auth status`, then
#       re-run the same command.
#
# ── Per-call time limit (#4850) ─────────────────────────────────────────────
#
# Every `gh` call runs under a per-call timeout sized to the time left in
# --timeout-secs (see scripts/_wait_budget_lib.sh), so one hung call cannot
# hold the script past its budget and the Bash tool's 600s cap. A killed
# call counts as a failed poll. Any call slower than 30s is logged to stderr
# as `slow call: <call> took Ns`, so the next slow poll names its culprit.
#   3 — REBASE_REQUIRED (#4412): CI is green (`ci-passed=success`, no latest
#       failures) but `mergeStateStatus=DIRTY` — a concurrent merge landed on
#       origin/main that conflicts with this PR's diff. There is no path
#       forward by waiting; the agent must rebase before re-entering the CI
#       watch. Exits 3 immediately on the first poll iteration where the
#       condition is met. Stdout names the action with the literal command:
#       `mergeStateStatus=DIRTY — rebase required to merge.
#        Run: git fetch origin main && git rebase origin/main && git push --force-with-lease.`
#
# ── GraphQL rate-limit fallback (#4507) ─────────────────────────────────────
#
# Every `gh pr view --json X` call in this script consumes from GitHub's
# GraphQL 5,000-req/hr quota. When the dispatcher or a /task agent burns
# through GraphQL during a long session — common with /dispatcher 5 — the
# polling loop's `gh pr view` calls fail with `GraphQL: API rate limit
# already exceeded` and the script bails. The REST `core` 5,000-req/hr
# quota is a separate bucket that almost always still has headroom.
#
# `gh_pr_view_with_rest_fallback <field>` (defined below) wraps every
# `gh pr view --json <field> -q .<field>` call: on GraphQL rate-limit
# error it falls back to a single `gh api /repos/.../pulls/N` REST call
# and translates the field shape (mergeable becomes
# MERGEABLE/CONFLICTING/UNKNOWN, mergeable_state uppercases to match the
# GraphQL enum). Per AC #3 of #4507, only one
# `info: GraphQL rate-limited, falling back to REST` line is emitted to
# stderr per script invocation — additional fallbacks proceed silently.
#
# ── Flake telemetry (#4163) ──────────────────────────────────────────────────
#
# Every time the auto-rerun classifier fires `gh run rerun` for a known flake,
# one structured JSON line is appended to a stable file so downstream tooling
# can aggregate flake frequencies without scraping CI logs. The default path
# is `tmp/wait-for-ci-flakes.jsonl` resolved relative to the script's parent
# (i.e. the repo or worktree root that contains `scripts/`). Tests override
# the path via the `WAIT_FOR_CI_FLAKE_LOG` env var.
#
# Each line carries a fixed schema (no free-form prose) so a one-liner like
# `jq -r .label tmp/wait-for-ci-flakes.jsonl | sort | uniq -c` produces the
# leaderboard. Schema:
#
#   {"ts":"<ISO-8601 UTC>","pr":<int>,"sha":"<short sha>","run_id":<int>,
#    "label":"<flake-label>","check_name":"<failed check name>"}
#
# A companion helper `scripts/summarize-ci-flakes.sh` reads the same file and
# prints a per-label count. Promotion of a label to the auto-rerun table
# remains a human PR (out of scope for this script).
#
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────────────────────

# 480s keeps the whole call (setup + one last poll) under the Bash tool's
# 600s cap (#4835). Exit 124 tells the caller to re-run.
TIMEOUT_SECS=480
POLL_INTERVAL=30
REPO="judgemind/judgemind"
PR_NUMBER=""
AUTO_RERUN=1
# Non-required checks that judge the PR title/body (.github/workflows/
# pr-title-check.yml). A pending run of one blocks success (#5090).
PR_TEXT_CHECKS="closing-refs validate-pr"

# Path to this script's directory — used to locate the sibling
# `classify-ci-flake.sh` helper without depending on PATH.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLASSIFIER="$SCRIPT_DIR/classify-ci-flake.sh"

# Per-call timeouts + slow-call logging for every gh call (#4850).
# shellcheck source=scripts/_wait_budget_lib.sh
source "$SCRIPT_DIR/_wait_budget_lib.sh"
# Consecutive failed (or timed-out) check-runs reads before exiting 125.
MAX_CONSECUTIVE_ERRORS=3

# Default flake telemetry log path — `<repo-or-worktree-root>/tmp/wait-for-ci-flakes.jsonl`.
# `SCRIPT_DIR` is `<root>/scripts`, so the parent is the root itself. Tests
# override via `WAIT_FOR_CI_FLAKE_LOG` to point at a per-test tmpdir.
DEFAULT_FLAKE_LOG="$(dirname "$SCRIPT_DIR")/tmp/wait-for-ci-flakes.jsonl"
FLAKE_LOG="${WAIT_FOR_CI_FLAKE_LOG:-$DEFAULT_FLAKE_LOG}"

# ── Argument parsing ──────────────────────────────────────────────────────────

print_help() {
    grep '^# Usage:' "$0" | sed 's/^# //'
    echo ""
    grep '^# Arguments:' "$0" | sed 's/^# //'
    grep '^#   <pr-number>' "$0" | sed 's/^# //'
    echo ""
    grep '^# Options:' "$0" | sed 's/^# //'
    grep '^#   --' "$0" | sed 's/^# //'
    echo ""
    grep '^# Exit codes:' "$0" | sed 's/^# //'
    grep -E '^#   (0|1|3|124|125) ' "$0" | sed 's/^# //'
}

# Kept verbatim so the exit-124 message can print the exact re-run command.
ORIG_ARGS=("$@")

while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h)
            print_help
            exit 0
            ;;
        --timeout-secs)
            TIMEOUT_SECS="$2"
            shift 2
            ;;
        --poll-interval)
            POLL_INTERVAL="$2"
            shift 2
            ;;
        --repo)
            REPO="$2"
            shift 2
            ;;
        --no-auto-rerun)
            AUTO_RERUN=0
            shift
            ;;
        --*)
            echo "ERROR: Unknown option: $1" >&2
            echo "Run with --help for usage." >&2
            exit 1
            ;;
        *)
            if [ -z "$PR_NUMBER" ]; then
                PR_NUMBER="$1"
                shift
            else
                echo "ERROR: Unexpected argument: $1" >&2
                exit 1
            fi
            ;;
    esac
done

if [ -z "$PR_NUMBER" ]; then
    echo "ERROR: <pr-number> is required." >&2
    echo "Run with --help for usage." >&2
    exit 1
fi

# ── GraphQL rate-limit fallback (#4507) ───────────────────────────────────────
#
# `gh pr view --json <field>` posts via GitHub's GraphQL API, which has a
# separate 5,000-req/hr quota from the REST core API. Long agent sessions
# (dispatcher, multi-PR /task runs) regularly exhaust GraphQL while REST stays
# healthy. When that happens, every `gh pr view --json X` call in this script
# fails with "GraphQL: API rate limit already exceeded" — and the polling loop
# bails. The REST equivalent endpoint
# (`GET /repos/{owner}/{repo}/pulls/{n}`) consumes from the REST core quota
# and is rarely exhausted in the same session.
#
# `gh_pr_view_with_rest_fallback <field>` runs `gh pr view --json <field>`
# first; on GraphQL rate-limit failure it falls back to a single REST
# `gh api /repos/.../pulls/N` call and extracts the equivalent field.
# Translates the field name + value shape from REST to the GraphQL form the
# rest of the script expects:
#
#     field name        | GraphQL value      | REST field        | translation
#     ------------------|--------------------|-------------------|---------------------
#     headRefOid        | "<40-char SHA>"    | head.sha          | none (verbatim)
#     mergeable         | MERGEABLE / etc.   | mergeable         | true→MERGEABLE,
#                       |                    |                   | false→CONFLICTING,
#                       |                    |                   | null→UNKNOWN
#     mergeStateStatus  | CLEAN / DIRTY/...  | mergeable_state   | uppercase
#
# Per AC #3 of #4507, only one `info: GraphQL rate-limited, falling back to
# REST` line is emitted to stderr per script invocation — additional
# fallbacks reuse the REST path silently. The sentinel must survive
# command-substitution subshells (`MERGEABLE=$(gh_pr_view_with_rest_fallback
# mergeable)`), so it's a temp file, not a shell variable. The file is
# created on first GraphQL fallback and removed on script exit. Tests can
# override the path via WAIT_FOR_CI_GRAPHQL_FALLBACK_SENTINEL.
GRAPHQL_FALLBACK_SENTINEL_FILE="${WAIT_FOR_CI_GRAPHQL_FALLBACK_SENTINEL:-${TMPDIR:-/tmp}/wait-for-ci-graphql-fallback.$$}"
# shellcheck disable=SC2064  # path fixed at trap-install time.
trap "rm -f '$GRAPHQL_FALLBACK_SENTINEL_FILE'" EXIT

gh_pr_view_with_rest_fallback() {
    local field="$1"
    local pr_view_stderr
    local pr_view_stdout
    local pr_view_exit
    pr_view_stderr=$(mktemp)
    pr_view_stdout=$(mktemp)

    set +e
    wait_budget_call "gh pr view --json ${field}" \
        gh pr view "$PR_NUMBER" --repo "$REPO" --json "$field" -q ".$field" \
        > "$pr_view_stdout" 2> "$pr_view_stderr"
    pr_view_exit=$?
    set -e

    if [ "$pr_view_exit" -eq 0 ]; then
        cat "$pr_view_stdout"
        rm -f "$pr_view_stderr" "$pr_view_stdout"
        return 0
    fi

    # Detect the explicit GraphQL rate-limit-exceeded marker. Anything else
    # is a real failure and is passed through (stderr replayed, original
    # exit code preserved).
    if ! grep -qE "GraphQL: API rate limit already exceeded" "$pr_view_stderr"; then
        cat "$pr_view_stderr" >&2
        rm -f "$pr_view_stderr" "$pr_view_stdout"
        return "$pr_view_exit"
    fi

    # First fallback in this session — emit one info line, then suppress
    # subsequent ones to avoid log spam during a 30-min rate-limit window.
    # File-based sentinel survives `$(...)` subshells; the parent's exit
    # trap removes it.
    if [ ! -f "$GRAPHQL_FALLBACK_SENTINEL_FILE" ]; then
        echo "info: GraphQL rate-limited, falling back to REST" >&2
        : > "$GRAPHQL_FALLBACK_SENTINEL_FILE"
    fi

    rm -f "$pr_view_stderr" "$pr_view_stdout"

    # REST path. The `pulls/N` endpoint returns the full PR object; we extract
    # the equivalent field via jq. `mergeStateStatus` and `mergeable` need
    # value-shape translation since REST's enums are lowercase / boolean.
    local rest_field rest_jq
    case "$field" in
        headRefOid)
            rest_field="head.sha"
            rest_jq=".head.sha // \"\""
            ;;
        mergeable)
            # REST returns true / false / null. GraphQL returns
            # MERGEABLE / CONFLICTING / UNKNOWN. Map.
            rest_field="mergeable"
            rest_jq='if .mergeable == true then "MERGEABLE" elif .mergeable == false then "CONFLICTING" else "UNKNOWN" end'
            ;;
        mergeStateStatus)
            # REST returns lowercase "clean" / "dirty" / "unstable" / "blocked"
            # / "behind" / "has_hooks" / "unknown" / "draft". Uppercase to
            # match the GraphQL enum.
            rest_field="mergeable_state"
            rest_jq='(.mergeable_state // "unknown") | ascii_upcase'
            ;;
        *)
            echo "ERROR: gh_pr_view_with_rest_fallback: unsupported field '$field'." >&2
            return 1
            ;;
    esac

    # Single REST call. Don't suppress stderr here — if REST also fails (e.g.
    # core quota exhausted, auth broken), the operator needs the diagnostic.
    local rest_rc=0
    wait_budget_call "gh api pulls/${PR_NUMBER}" \
        gh api "/repos/${REPO}/pulls/${PR_NUMBER}" --jq "$rest_jq" || rest_rc=$?
    if [ "$rest_rc" -ne 0 ]; then
        echo "ERROR: REST fallback for '$field' also failed." >&2
        return "$rest_rc"
    fi
    return 0
}

# ── Resolve PR head SHA ───────────────────────────────────────────────────────

# The budget starts here so the SHA lookup counts against --timeout-secs too.
START_TS=$SECONDS
wait_budget_init "$TIMEOUT_SECS" "$START_TS"

# print_still_waiting — the exit-124 message ("re-run the same command").
print_still_waiting() {
    echo ""
    echo "STILL WAITING: CI on PR #${PR_NUMBER} is still running after ${TIMEOUT_SECS}s. This is not a failure." >&2
    echo "Last state: pending=${PENDING_COUNT:-?} passed=${PASSED_COUNT:-?} failed=${FAILED_COUNT:-?} cancelled=${CANCELLED_COUNT:-?}" >&2
    echo "ci-passed conclusion: ${CI_PASSED_CONCLUSION:-(not yet present)}" >&2
    echo "Re-run the same command to keep waiting (it only reads state; nothing is re-triggered):" >&2
    echo "  scripts/wait-for-ci.sh ${ORIG_ARGS[*]}" >&2
}

# print_status_unknown <what> — the exit-125 message.
print_status_unknown() {
    echo "" >&2
    echo "ERROR: CI status of PR #${PR_NUMBER} is unknown: $1 failed ${MAX_CONSECUTIVE_ERRORS} times in a row." >&2
    echo "Fix: check 'gh auth status' and GitHub's status page, then re-run:" >&2
    echo "  scripts/wait-for-ci.sh ${ORIG_ARGS[*]}" >&2
}

echo "Resolving head SHA for PR #${PR_NUMBER} in ${REPO}..."
# A lookup killed by its per-call timeout is retried (#4850); any other
# failure (PR not found, auth) still falls through to the exit-1 below.
SHA=""
SHA_TIMEOUTS=0
while true; do
    SHA_RC=0
    SHA=$(gh_pr_view_with_rest_fallback headRefOid) || SHA_RC=$?
    if [ "$SHA_RC" -ne "$WAIT_CALL_TIMEOUT_RC" ]; then
        break
    fi
    SHA_TIMEOUTS=$((SHA_TIMEOUTS + 1))
    if wait_budget_exhausted; then
        print_still_waiting
        exit 124
    fi
    if [ "$SHA_TIMEOUTS" -ge "$MAX_CONSECUTIVE_ERRORS" ]; then
        print_status_unknown "the head-SHA lookup"
        exit 125
    fi
done

if [ -z "$SHA" ]; then
    echo "ERROR: Could not resolve head SHA for PR #${PR_NUMBER}." >&2
    exit 1
fi

echo "PR #${PR_NUMBER} head SHA: ${SHA}"
echo "Polling check-runs (timeout: ${TIMEOUT_SECS}s, interval: ${POLL_INTERVAL}s)..."
echo ""

# ── Auto-rerun sentinel (#4148) ──────────────────────────────────────────────
#
# When --auto-rerun is enabled (the default), the script will fire
# `gh run rerun <run-id> --failed` exactly once per CI run on this SHA when a
# failed job's log matches a known-flake pattern. The sentinel file remembers
# which run-ids have already been rerun so a second flake on the same run
# falls through to exit 1 — this prevents chasing a real outage forever.
#
# Tests override the sentinel path via WAIT_FOR_CI_RERUN_SENTINEL_FILE.
RERUN_SENTINEL_FILE="${WAIT_FOR_CI_RERUN_SENTINEL_FILE:-${TMPDIR:-/tmp}/wait-for-ci-rerun.${SHA}}"

# ── Helpers ───────────────────────────────────────────────────────────────────

# Extract the workflow run-id from a check-run's details_url. GitHub formats
# the URL as `https://<host>/<owner>/<repo>/actions/runs/<run-id>/job/<job-id>`
# (or sometimes without the `/job/<job-id>` suffix). We tolerate either form.
# Echoes the run-id, or empty string if no match.
parse_run_id_from_url() {
    local url="$1"
    # Strip everything up to and including `/actions/runs/`, then keep digits
    # before the next `/` boundary.
    echo "$url" | sed -n 's|.*/actions/runs/\([0-9][0-9]*\).*|\1|p'
}

# Check whether `gh run rerun` has already been invoked for the given run-id
# during this script's lifetime (across poll iterations). Returns 0 if the
# sentinel records this run, 1 otherwise.
rerun_already_fired() {
    local run_id="$1"
    if [ -f "$RERUN_SENTINEL_FILE" ] && grep -Fxq "$run_id" "$RERUN_SENTINEL_FILE"; then
        return 0
    fi
    return 1
}

# Record that `gh run rerun` was fired for this run-id.
record_rerun_fired() {
    local run_id="$1"
    mkdir -p "$(dirname "$RERUN_SENTINEL_FILE")"
    echo "$run_id" >> "$RERUN_SENTINEL_FILE"
}

# Emit one JSONL line of flake telemetry to $FLAKE_LOG (#4163).
#
# Args: <pr> <sha> <run_id> <label> <check_name>
#
# The line is built with `jq -cn` so embedded quotes / shell metachars in any
# field are escaped correctly. We tolerate jq write failures silently — the
# rerun must still happen even if the disk is full or the parent directory is
# read-only. The whole helper returns 0 unconditionally so a logging error
# never wedges the polling loop.
emit_flake_telemetry() {
    local pr="$1"
    local sha="$2"
    local run_id="$3"
    local label="$4"
    local check_name="$5"
    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    mkdir -p "$(dirname "$FLAKE_LOG")" 2>/dev/null || return 0

    local short_sha="${sha:0:8}"
    # Note: `label` is a jq reserved keyword, so we pass it as `$lbl` and
    # remap to the public field name in the object literal.
    local line
    line=$(jq -cn \
        --arg ts "$ts" \
        --argjson pr "$pr" \
        --arg sha "$short_sha" \
        --argjson run_id "$run_id" \
        --arg lbl "$label" \
        --arg check_name "$check_name" \
        '{ts: $ts, pr: $pr, sha: $sha, run_id: $run_id, label: $lbl, check_name: $check_name}' 2>/dev/null) || return 0

    if [ -n "$line" ]; then
        printf '%s\n' "$line" >> "$FLAKE_LOG" 2>/dev/null || return 0
    fi
    return 0
}

# ── Poll loop ─────────────────────────────────────────────────────────────────

DEADLINE=$((START_TS + TIMEOUT_SECS))
CONSECUTIVE_ERRORS=0

while true; do
    NOW_TS=$SECONDS
    ELAPSED=$((NOW_TS - START_TS))

    # Fetch check-runs with filter=latest (one entry per check name, deduped).
    # A failed or timed-out read is a failed poll, not a CI verdict (#4850).
    CHECK_RUNS_RC=0
    CHECK_RUNS_RAW=$(wait_budget_call "gh api check-runs" \
        gh api "repos/${REPO}/commits/${SHA}/check-runs?per_page=100&filter=latest") \
        || CHECK_RUNS_RC=$?
    CHECK_RUNS_JSON=""
    if [ "$CHECK_RUNS_RC" -eq 0 ]; then
        CHECK_RUNS_JSON=$(printf '%s' "$CHECK_RUNS_RAW" | jq '.check_runs' 2>/dev/null) || CHECK_RUNS_RC=1
    fi
    if [ "$CHECK_RUNS_RC" -ne 0 ] || [ -z "$CHECK_RUNS_JSON" ] || [ "$CHECK_RUNS_JSON" = "null" ]; then
        CONSECUTIVE_ERRORS=$((CONSECUTIVE_ERRORS + 1))
        echo "[${ELAPSED}s] WARNING: check-runs read failed (${CONSECUTIVE_ERRORS}/${MAX_CONSECUTIVE_ERRORS}, rc=${CHECK_RUNS_RC})." >&2
        if wait_budget_exhausted || [ $((SECONDS - START_TS)) -ge "$TIMEOUT_SECS" ]; then
            print_still_waiting
            exit 124
        fi
        if [ "$CONSECUTIVE_ERRORS" -ge "$MAX_CONSECUTIVE_ERRORS" ]; then
            print_status_unknown "the check-runs read"
            exit 125
        fi
        wait_budget_sleep "$POLL_INTERVAL"
        continue
    fi
    CONSECUTIVE_ERRORS=0

    # ── Collapse to the newest run per check (#5078) ─────────────────────────
    # filter=latest dedupes only WITHIN a check suite. Every workflow run is
    # its own suite, so an event other than a push (pull_request `edited` /
    # `reopened`, a manual dispatch) re-runs a check in a NEW suite on the
    # SAME head SHA, and the old run stays in the response. Before #5078 a
    # `closing-refs` failure fixed by `gh pr edit` kept this script red after
    # the fresh run passed, until someone ran `gh run rerun --failed`.
    #
    # Evaluate each check by (name, app) using only its newest run: the
    # highest check-run id (ids grow monotonically), then the latest
    # started_at. That is the same rule branch protection applies to a
    # required check. A failure that a newer run of the same check replaced
    # (success, or still in progress) no longer counts as failed.
    #
    # A cancelled run is not a verdict (#5097): cancelled checks do not block
    # (#4407), so a newest run that is `cancelled` would hide an older
    # `failure` of the same check, e.g. a re-run killed by a concurrency
    # group with no later run. When the newest run is cancelled, the check is
    # judged by its newest completed run that is not cancelled; only when
    # every run was cancelled does it stay cancelled.
    #
    # CHECK_RUNS_ALL_JSON keeps every run: the `ci-passed` rule below (#4965)
    # still requires that NO `ci-passed` run on the SHA is in progress.
    CHECK_RUNS_ALL_JSON="$CHECK_RUNS_JSON"
    CHECK_RUNS_JSON=$(printf '%s' "$CHECK_RUNS_ALL_JSON" | jq '
        group_by([(.name // ""), ((.app.id // .app.slug // "") | tostring)])
        | map(sort_by([(.id // 0), (.started_at // "")])
            | if (last | .status == "completed" and .conclusion == "cancelled")
              then ((map(select(.status == "completed" and .conclusion != "cancelled")) | last) // last)
              else last
              end)')
    # Checks whose newest run is still going and whose replaced run failed:
    # the verdict is not known yet, so success must wait for them. Without
    # this, a re-run of a failed non-required check (which no longer counts
    # as failed) would let the script exit 0 before the re-run finishes.
    RERUN_PENDING_NAMES=$(printf '%s' "$CHECK_RUNS_ALL_JSON" | jq -r '
        group_by([(.name // ""), ((.app.id // .app.slug // "") | tostring)])
        | map(sort_by([(.id // 0), (.started_at // "")]))
        | map(select(
            (last | .status != "completed")
            and (.[:-1] | any(.status == "completed" and (.conclusion == "failure" or .conclusion == "timed_out" or .conclusion == "action_required")))
          ) | last | .name)
        | join(", ")')

    # Count pending, passed, failed (hard), and cancelled checks.
    #
    # `cancelled` is split out from `FAILED_COUNT` (#4407) because it is not a
    # hard failure: Vercel and smoke-test workflows configure
    # `concurrency: cancel-in-progress: true`, which cancels superseded check
    # runs the moment a newer push/deploy starts. The cancel is intentional
    # workflow behaviour, not a real test failure, and the canonical merge
    # gate documented in `docs/agent/code-standards.md` §"Interpreting
    # mergeStateStatus (UNSTABLE-but-green)" already permits non-required
    # `cancelled` checks. Treating them as failures forced every /task agent
    # to manually fall through to a separate `gh pr view` recipe on every
    # Vercel-cancellation run; splitting the count closes that loop.
    PENDING_COUNT=$(echo "$CHECK_RUNS_JSON" | jq '[.[] | select(.status != "completed")] | length')
    PASSED_COUNT=$(echo "$CHECK_RUNS_JSON" | jq '[.[] | select(.status == "completed" and (.conclusion == "success" or .conclusion == "skipped" or .conclusion == "neutral"))] | length')
    FAILED_COUNT=$(echo "$CHECK_RUNS_JSON" | jq '[.[] | select(.status == "completed" and (.conclusion == "failure" or .conclusion == "timed_out" or .conclusion == "action_required"))] | length')
    CANCELLED_COUNT=$(echo "$CHECK_RUNS_JSON" | jq '[.[] | select(.status == "completed" and .conclusion == "cancelled")] | length')

    echo "[${ELAPSED}s] pending=${PENDING_COUNT} passed=${PASSED_COUNT} failed=${FAILED_COUNT} cancelled=${CANCELLED_COUNT}"

    # Check for early failure: any check with a HARD failed conclusion. A
    # cancelled-only state never trips this branch (#4407).
    if [ "$FAILED_COUNT" -gt 0 ]; then
        echo ""
        echo "ERROR: ${FAILED_COUNT} check(s) failed:" >&2
        echo "$CHECK_RUNS_JSON" | jq -r '.[] | select(.status == "completed" and (.conclusion == "failure" or .conclusion == "timed_out" or .conclusion == "action_required")) | "  - \(.name): conclusion=\(.conclusion)  \(.details_url // "(no details url)")"' >&2

        # ── Known-flake auto-rerun (#4148) ────────────────────────────────
        # Before exiting 1, classify each failed job's log tail. If any
        # classifies as a known flake AND no rerun has fired yet on the
        # owning workflow run, fire `gh run rerun --failed` once and continue
        # polling. A second flake on the same run falls through to exit 1.
        if [ "$AUTO_RERUN" -eq 1 ] && [ -x "$CLASSIFIER" ]; then
            FLAKE_DETECTED=0
            FLAKE_LABEL=""
            FLAKE_RUN_ID=""
            FLAKE_CHECK_NAME=""

            # Extract (check_name, details_url) pairs for every failed check.
            # We capture the check name alongside the URL so the telemetry
            # emitter (#4163) can record which named check fired the flake —
            # consumers want to know whether postgres-startup is hitting
            # `schema-drift-check`, `unit-tests`, or somewhere new. We only
            # need to act on the first failed run-id we see — `gh run rerun
            # --failed` already reruns every failed job on that run, and
            # rerunning multiple distinct runs from one script invocation is
            # out of scope (one rerun per SHA per script call).
            #
            # Tab-separated to survive in any plausible check name; check
            # names with embedded tabs are not a concern (GitHub's check-name
            # validator forbids control chars).
            FAILED_DETAILS=$(echo "$CHECK_RUNS_JSON" | jq -r '
                .[]
                | select(.status == "completed" and (.conclusion == "failure" or .conclusion == "timed_out" or .conclusion == "action_required"))
                | "\(.name // "")\t\(.details_url // "")"
            ')

            # Walk failed checks until we find one whose run-id parses AND
            # whose log classifies as a known flake.
            while IFS=$'\t' read -r CHECK_NAME URL; do
                [ -z "$URL" ] && continue
                CANDIDATE_RUN_ID=$(parse_run_id_from_url "$URL")
                if [ -z "$CANDIDATE_RUN_ID" ]; then
                    continue
                fi

                # Fetch the failed-job log tail for this run. `gh run view
                # --log-failed` concatenates every failed job's full log;
                # we pass the whole thing to the classifier (the classifier
                # is grep-based so size is fine for typical CI logs <10MB).
                # Suppress stderr — `gh` may print a non-fatal warning about
                # log truncation that we don't want polluting stdout.
                # Logs can be large, so this call gets a longer per-call
                # limit (still never past the budget, #4850).
                JOB_LOG=$(wait_budget_call --max 120 "gh run view --log-failed" \
                    gh run view "$CANDIDATE_RUN_ID" --repo "$REPO" --log-failed 2>/dev/null || echo "")

                CLASSIFICATION=$(printf '%s' "$JOB_LOG" | "$CLASSIFIER" 2>/dev/null || echo "real")
                if [ "${CLASSIFICATION#flake/}" != "$CLASSIFICATION" ]; then
                    FLAKE_DETECTED=1
                    FLAKE_LABEL="${CLASSIFICATION#flake/}"
                    FLAKE_RUN_ID="$CANDIDATE_RUN_ID"
                    FLAKE_CHECK_NAME="$CHECK_NAME"
                    break
                fi
            done <<EOF
$FAILED_DETAILS
EOF

            if [ "$FLAKE_DETECTED" -eq 1 ]; then
                if rerun_already_fired "$FLAKE_RUN_ID"; then
                    echo "" >&2
                    echo "flake detected: ${FLAKE_LABEL} (run ${FLAKE_RUN_ID}) — but rerun already fired once for this run; not retrying." >&2
                    exit 1
                fi

                echo ""
                echo "flake detected: ${FLAKE_LABEL} (run ${FLAKE_RUN_ID}) — auto-rerunning failed jobs and continuing to poll."
                if wait_budget_call "gh run rerun" \
                        gh run rerun "$FLAKE_RUN_ID" --repo "$REPO" --failed >/dev/null 2>&1; then
                    record_rerun_fired "$FLAKE_RUN_ID"
                    # Append one structured JSONL line so downstream tooling
                    # can aggregate flake frequencies (#4163). Best-effort —
                    # never fails the polling loop.
                    emit_flake_telemetry "$PR_NUMBER" "$SHA" "$FLAKE_RUN_ID" "$FLAKE_LABEL" "$FLAKE_CHECK_NAME"
                    # Sleep one poll interval so the new run has a moment to
                    # register before the next check-runs fetch.
                    wait_budget_sleep "$POLL_INTERVAL"
                    if wait_budget_exhausted; then
                        print_still_waiting
                        exit 124
                    fi
                    continue
                else
                    echo "ERROR: gh run rerun failed for run ${FLAKE_RUN_ID}; exiting 1." >&2
                    exit 1
                fi
            fi
        fi

        exit 1
    fi

    # Determine whether the required `ci-passed` check is settled green.
    #
    # filter=latest dedupes within a workflow run but NOT across workflow runs
    # on the same SHA, so the response can hold several `ci-passed` entries
    # (#4069). Until #4965 every PR commit ran CI twice (push + pull_request,
    # in different concurrency groups) and this script exited as soon as ANY
    # `ci-passed` entry was success — usually the push run's, while the
    # pull_request run (the one that tests the merge result against current
    # main) was still in progress. GitHub correctly reported BLOCKED, so
    # `gh pr merge` refused.
    #
    # The rule now mirrors what branch protection evaluates:
    #   - no `ci-passed` entry on the head SHA may still be running, AND
    #   - the newest `ci-passed` entry (highest check-run id) is success.
    # A cancelled older run is fine (it is terminal); a cancelled newest run
    # is not green.
    CI_PASSED_PENDING=$(echo "$CHECK_RUNS_ALL_JSON" | jq '[.[] | select(.name == "ci-passed" and .status != "completed")] | length')
    CI_PASSED_CONCLUSION=$(echo "$CHECK_RUNS_ALL_JSON" | jq -r '[.[] | select(.name == "ci-passed")] | sort_by(.id // 0) | (last | .conclusion) // ""')
    if [ "$CI_PASSED_PENDING" -eq 0 ] && [ "$CI_PASSED_CONCLUSION" = "success" ]; then
        CI_PASSED_SUCCESS=true
    else
        CI_PASSED_SUCCESS=false
    fi
    if [ "$CI_PASSED_PENDING" -gt 0 ]; then
        CI_PASSED_CONCLUSION="in_progress (${CI_PASSED_PENDING} ci-passed run(s) still running)"
    fi

    # A re-run of a check that failed earlier on this SHA is still going
    # (#5078): its verdict decides the PR, so no success exit until it ends.
    if [ -n "$RERUN_PENDING_NAMES" ]; then
        echo "  waiting for the re-run of previously failed check(s): ${RERUN_PENDING_NAMES}"
        if [ "$CI_PASSED_SUCCESS" = "true" ]; then
            CI_PASSED_CONCLUSION="success (waiting for re-run of: ${RERUN_PENDING_NAMES})"
        fi
        CI_PASSED_SUCCESS=false
    fi

    # PR-text checks (#5090): `closing-refs` and `validate-pr` are not
    # required by branch protection, but they judge the PR's title/body, and
    # a `gh pr edit` re-runs them on the same head SHA. A merge while one is
    # still running could ship a body that closes an unrelated issue (the
    # check never gets to fail). So a pending newest run of one of them
    # blocks success, exactly like a pending `ci-passed`. Limit: a run GitHub
    # has not created yet (the first seconds after the edit) is invisible
    # here; only a required status check closes that window.
    PR_TEXT_PENDING_NAMES=$(printf '%s' "$CHECK_RUNS_JSON" | jq -r --arg names "$PR_TEXT_CHECKS" '
        ($names | split(" ") | map(select(length > 0))) as $gate
        | [.[] | select(.status != "completed" and ((.name // "") as $n | any($gate[]; . == $n))) | .name]
        | unique | join(", ")')
    if [ -n "$PR_TEXT_PENDING_NAMES" ]; then
        echo "  waiting for PR-text check(s) still running: ${PR_TEXT_PENDING_NAMES}"
        if [ "$CI_PASSED_SUCCESS" = "true" ]; then
            CI_PASSED_CONCLUSION="success (waiting for PR-text check(s): ${PR_TEXT_PENDING_NAMES})"
        fi
        CI_PASSED_SUCCESS=false
    fi

    # ── Rebase-required fast-path (#4412) ────────────────────────────────────
    # When CI is green (ci-passed=success, no latest failures) but
    # mergeStateStatus is DIRTY, a concurrent merge landed on origin/main that
    # conflicts with the PR's diff. There is no path forward by polling — the
    # agent must rebase before re-entering the CI watch. Exit 3 immediately
    # on the first poll iteration where this is true so the caller can pivot
    # to the rebase + force-push path documented in `.claude/skills/task/SKILL.md`
    # §A.5.
    #
    # Checked BEFORE the canonical-merge-gate fast-path because mergeable will
    # be CONFLICTING (not MERGEABLE) when mergeStateStatus=DIRTY, so the gate
    # cannot fire on this state — but we want to surface the rebase signal
    # promptly rather than fall through to the all-checks-complete path which
    # would log "still waiting" repeatedly until timeout.
    if [ "$CI_PASSED_SUCCESS" = "true" ] && [ "$FAILED_COUNT" -eq 0 ]; then
        # No `2>/dev/null` — gh_pr_view_with_rest_fallback only writes to
        # stderr in two cases the operator must see: the once-per-session
        # GraphQL fallback notice and a real REST-also-failed error. The
        # `|| echo "UNKNOWN"` still tolerates a non-zero exit (both gh paths
        # failed) by treating mergeStateStatus as unknown.
        MERGE_STATE_FOR_DIRTY=$(gh_pr_view_with_rest_fallback mergeStateStatus || echo "UNKNOWN")
        if [ "$MERGE_STATE_FOR_DIRTY" = "DIRTY" ]; then
            echo ""
            echo "[${ELAPSED}s] mergeStateStatus=DIRTY — rebase required to merge. Run: git fetch origin main && git rebase origin/main && git push --force-with-lease."
            exit 3
        fi
    fi

    # ── Merge gate (#4069, #4407, #4965) ─────────────────────────────────────
    # Mirrors `docs/agent/code-standards.md` §"Interpreting mergeStateStatus
    # (UNSTABLE-but-green)" and the `/task` skill's §A.7 merge gate. Exit 0
    # only when ALL of:
    #   1. the required `ci-passed` check is settled green (see above: no
    #      `ci-passed` run on the head SHA still in progress, newest success),
    #   2. no latest check is a hard failure, and
    #   3. GitHub's own branch-protection verdict agrees: mergeStateStatus is
    #      CLEAN, UNSTABLE or HAS_HOOKS. BLOCKED means a required check is not
    #      yet satisfied, so `gh pr merge` would refuse; keep waiting (#4965).
    # Pending entries from OTHER check names (Vercel, smoke tests) and
    # `cancelled` non-required checks (#4407) do not block — they are not
    # required by branch protection. The PR-text checks are the exception
    # (#5090, see above): a pending one already cleared CI_PASSED_SUCCESS.
    #
    # Two success messages, kept for callers that grep them:
    #   - `canonical merge gate green` when mergeable=MERGEABLE too;
    #   - `all checks complete` when mergeable cannot be resolved (UNKNOWN,
    #     API error) but every check has drained to a terminal state.
    if [ "$CI_PASSED_SUCCESS" = "true" ] && [ "$FAILED_COUNT" -eq 0 ]; then
        # Same condition as the DIRTY branch above, so reuse its read.
        MERGE_STATE="$MERGE_STATE_FOR_DIRTY"
        case "$MERGE_STATE" in
            CLEAN|UNSTABLE|HAS_HOOKS)
                MERGEABLE=$(gh_pr_view_with_rest_fallback mergeable || echo "UNKNOWN")
                if [ "$MERGEABLE" = "MERGEABLE" ]; then
                    echo ""
                    echo "[${ELAPSED}s] canonical merge gate green — exiting (mergeable=MERGEABLE, mergeStateStatus=${MERGE_STATE}, ci-passed=success with no ci-passed run in progress, failed=0; pending=${PENDING_COUNT} non-required; cancelled=${CANCELLED_COUNT} non-required)."
                    exit 0
                fi
                if [ "$PENDING_COUNT" -eq 0 ]; then
                    echo ""
                    echo "[${ELAPSED}s] all checks complete — CI passed (ci-passed=success, mergeStateStatus=${MERGE_STATE}, cancelled=${CANCELLED_COUNT} non-blocking)."
                    exit 0
                fi
                echo "  ci-passed=success, mergeStateStatus=${MERGE_STATE}, mergeable=${MERGEABLE}; still waiting..."
                ;;
            BLOCKED)
                echo "  ci-passed=success but mergeStateStatus=BLOCKED — branch protection is not satisfied yet, still waiting..."
                ;;
            *)
                echo "  ci-passed=success but mergeStateStatus=${MERGE_STATE}, still waiting..."
                ;;
        esac
    fi

    # Check timeout before sleeping. Re-read the clock: the calls above may
    # have taken a while (#4850). Also stop when too little is left for
    # another poll to finish inside the budget.
    NOW_TS=$SECONDS
    if [ "$NOW_TS" -ge "$DEADLINE" ] || wait_budget_exhausted; then
        print_still_waiting
        exit 124
    fi

    # Never sleep past the deadline, so the call ends on time (#4835).
    wait_budget_sleep "$POLL_INTERVAL"
done
