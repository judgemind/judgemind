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
# ── Usage ────────────────────────────────────────────────────────────────────
#
# Usage: scripts/wait-for-ci.sh <pr-number> [options]
#
# Arguments:
#   <pr-number>         PR number to monitor (required unless --help)
#
# Options:
#   --timeout-secs N    Overall timeout in seconds (default: 1800)
#   --poll-interval N   Polling interval in seconds (default: 30)
#   --repo OWNER/REPO   GitHub repository (default: judgemind/judgemind)
#   --no-auto-rerun     Disable the known-flake auto-rerun classifier (#4148).
#                       When set, any failed check exits 1 immediately.
#   --help              Print this help and exit 0
#
# Exit codes:
#   0 — Success. Reached via one of two paths, in priority order:
#       (a) **Canonical-merge-gate fast-path** (#4069): `mergeable == MERGEABLE`,
#           any `ci-passed` entry in the filter=latest response has
#           conclusion=success, and no latest check has conclusion=
#           failure/timed_out/action_required/cancelled. Stdout names this
#           path explicitly with the substring `canonical merge gate green`.
#           This path exits immediately even if filter=latest still surfaces
#           in_progress entries from a superseded CI run on the same SHA —
#           the same gate documented in `docs/agent/code-standards.md`
#           §"Interpreting mergeStateStatus (UNSTABLE-but-green)".
#       (b) **All-checks-complete path:** filter=latest shows pending=0,
#           ci-passed=success, no failures, and mergeStateStatus is CLEAN or
#           UNSTABLE. Stdout names this path with `all checks complete`.
#   1 — Early failure: one or more checks have a failed conclusion. Prints the
#       failed check names and their details_url. Before exiting, the failed
#       jobs' logs are passed to `scripts/classify-ci-flake.sh`. If any
#       classifies as a known flake AND no rerun has fired on this run yet,
#       `gh run rerun <run-id> --failed` is invoked once and polling continues
#       (path stdout names the matched pattern with `flake detected: <label>`).
#       A second flake on the same run exits 1 — see #4148.
#   2 — Timeout: --timeout-secs elapsed without reaching a terminal state.
#
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────────────────────

TIMEOUT_SECS=1800
POLL_INTERVAL=30
REPO="judgemind/judgemind"
PR_NUMBER=""
AUTO_RERUN=1

# Path to this script's directory — used to locate the sibling
# `classify-ci-flake.sh` helper without depending on PATH.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLASSIFIER="$SCRIPT_DIR/classify-ci-flake.sh"

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
    grep '^#   [012]' "$0" | sed 's/^# //'
}

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

# ── Resolve PR head SHA ───────────────────────────────────────────────────────

echo "Resolving head SHA for PR #${PR_NUMBER} in ${REPO}..."
SHA=$(gh pr view "$PR_NUMBER" --repo "$REPO" --json headRefOid -q .headRefOid)

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

# ── Poll loop ─────────────────────────────────────────────────────────────────

START_TS=$(date +%s)
DEADLINE=$((START_TS + TIMEOUT_SECS))

while true; do
    NOW_TS=$(date +%s)
    ELAPSED=$((NOW_TS - START_TS))

    # Fetch check-runs with filter=latest (one entry per check name, deduped).
    CHECK_RUNS_JSON=$(gh api "repos/${REPO}/commits/${SHA}/check-runs?per_page=100&filter=latest" \
        | jq '.check_runs')

    # Count pending, passed, and failed checks.
    PENDING_COUNT=$(echo "$CHECK_RUNS_JSON" | jq '[.[] | select(.status != "completed")] | length')
    PASSED_COUNT=$(echo "$CHECK_RUNS_JSON" | jq '[.[] | select(.status == "completed" and (.conclusion == "success" or .conclusion == "skipped" or .conclusion == "neutral"))] | length')
    FAILED_COUNT=$(echo "$CHECK_RUNS_JSON" | jq '[.[] | select(.status == "completed" and (.conclusion == "failure" or .conclusion == "timed_out" or .conclusion == "action_required" or .conclusion == "cancelled"))] | length')

    echo "[${ELAPSED}s] pending=${PENDING_COUNT} passed=${PASSED_COUNT} failed=${FAILED_COUNT}"

    # Check for early failure: any check with a failed conclusion.
    if [ "$FAILED_COUNT" -gt 0 ]; then
        echo ""
        echo "ERROR: ${FAILED_COUNT} check(s) failed:" >&2
        echo "$CHECK_RUNS_JSON" | jq -r '.[] | select(.status == "completed" and (.conclusion == "failure" or .conclusion == "timed_out" or .conclusion == "action_required" or .conclusion == "cancelled")) | "  - \(.name): conclusion=\(.conclusion)  \(.details_url // "(no details url)")"' >&2

        # ── Known-flake auto-rerun (#4148) ────────────────────────────────
        # Before exiting 1, classify each failed job's log tail. If any
        # classifies as a known flake AND no rerun has fired yet on the
        # owning workflow run, fire `gh run rerun --failed` once and continue
        # polling. A second flake on the same run falls through to exit 1.
        if [ "$AUTO_RERUN" -eq 1 ] && [ -x "$CLASSIFIER" ]; then
            FLAKE_DETECTED=0
            FLAKE_LABEL=""
            FLAKE_RUN_ID=""

            # Extract one (failed-check, run-id) pair per failed check.
            # We only need to check the first failed run-id we see —
            # `gh run rerun --failed` already reruns every failed job on
            # that run, and rerunning multiple distinct runs from one
            # script invocation is out of scope (one rerun per SHA per
            # script call). The first failed check in the rollup is
            # representative.
            FAILED_DETAILS=$(echo "$CHECK_RUNS_JSON" | jq -r '
                .[]
                | select(.status == "completed" and (.conclusion == "failure" or .conclusion == "timed_out" or .conclusion == "action_required" or .conclusion == "cancelled"))
                | (.details_url // "")
            ')

            # Walk failed checks until we find one whose run-id parses AND
            # whose log classifies as a known flake.
            while IFS= read -r URL; do
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
                JOB_LOG=$(gh run view "$CANDIDATE_RUN_ID" --repo "$REPO" --log-failed 2>/dev/null || echo "")

                CLASSIFICATION=$(printf '%s' "$JOB_LOG" | "$CLASSIFIER" 2>/dev/null || echo "real")
                if [ "${CLASSIFICATION#flake/}" != "$CLASSIFICATION" ]; then
                    FLAKE_DETECTED=1
                    FLAKE_LABEL="${CLASSIFICATION#flake/}"
                    FLAKE_RUN_ID="$CANDIDATE_RUN_ID"
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
                if gh run rerun "$FLAKE_RUN_ID" --repo "$REPO" --failed >/dev/null 2>&1; then
                    record_rerun_fired "$FLAKE_RUN_ID"
                    # Sleep one poll interval so the new run has a moment to
                    # register before the next check-runs fetch.
                    sleep "$POLL_INTERVAL"
                    continue
                else
                    echo "ERROR: gh run rerun failed for run ${FLAKE_RUN_ID}; exiting 1." >&2
                    exit 1
                fi
            fi
        fi

        exit 1
    fi

    # Determine whether `ci-passed` succeeded on its latest run. Use `any(...)`
    # rather than `select(...) | .conclusion` because filter=latest dedupes
    # within a workflow run but NOT across workflow re-runs on the same SHA —
    # a re-run plus a superseded run can leave two `ci-passed` entries in the
    # response, which would make a single jq `select()` print two lines and
    # silently break the equality check that follows (#4069 root cause). The
    # `any` form returns a single boolean string.
    CI_PASSED_SUCCESS=$(echo "$CHECK_RUNS_JSON" | jq -r '[.[] | select(.name == "ci-passed")] | any(.conclusion == "success")')
    CI_PASSED_CONCLUSION=$(echo "$CHECK_RUNS_JSON" | jq -r '[.[] | select(.name == "ci-passed") | .conclusion] | (.[0] // "")')

    # ── Canonical-merge-gate fast-path (#4069) ───────────────────────────────
    # Mirrors `docs/agent/code-standards.md` §"Interpreting mergeStateStatus
    # (UNSTABLE-but-green)" and the `/task` skill's §A.7 merge gate. When
    # `mergeable == MERGEABLE`, the required `ci-passed` check is success on
    # its latest run, and no latest check is a hard failure, the PR is safe
    # to merge regardless of any pending entries left over from a superseded
    # CI run on the same SHA. Exit immediately so callers don't burn the full
    # timeout watching phantom in_progress rows that will never complete.
    if [ "$CI_PASSED_SUCCESS" = "true" ] && [ "$FAILED_COUNT" -eq 0 ]; then
        MERGEABLE=$(gh pr view "$PR_NUMBER" --repo "$REPO" --json mergeable -q .mergeable 2>/dev/null || echo "UNKNOWN")
        if [ "$MERGEABLE" = "MERGEABLE" ]; then
            echo ""
            echo "[${ELAPSED}s] canonical merge gate green — exiting (mergeable=MERGEABLE, ci-passed=success, failed=0; pending=${PENDING_COUNT} ignored as superseded)."
            exit 0
        fi
    fi

    # ── Existing all-checks-complete path ────────────────────────────────────
    # When pending=0, every check has a terminal conclusion. Combined with
    # ci-passed=success and a CLEAN/UNSTABLE mergeStateStatus, this is the
    # original safe-to-merge signal. Kept as a fallback for the case where
    # `mergeable` cannot be resolved (UNKNOWN, API error) so the script still
    # exits when CI legitimately drained to zero pending.
    if [ "$CI_PASSED_SUCCESS" = "true" ] && [ "$FAILED_COUNT" -eq 0 ] && [ "$PENDING_COUNT" -eq 0 ]; then
        # Secondary guard: verify mergeStateStatus is CLEAN or UNSTABLE.
        MERGE_STATE=$(gh pr view "$PR_NUMBER" --repo "$REPO" --json mergeStateStatus -q .mergeStateStatus)
        if [ "$MERGE_STATE" = "CLEAN" ] || [ "$MERGE_STATE" = "UNSTABLE" ]; then
            echo ""
            echo "[${ELAPSED}s] all checks complete — CI passed (ci-passed=success, mergeStateStatus=${MERGE_STATE})."
            exit 0
        else
            echo "  ci-passed=success but mergeStateStatus=${MERGE_STATE}, still waiting..."
        fi
    fi

    # Check timeout before sleeping.
    if [ "$NOW_TS" -ge "$DEADLINE" ]; then
        echo ""
        echo "ERROR: Timed out after ${TIMEOUT_SECS}s waiting for CI on PR #${PR_NUMBER}." >&2
        echo "Last state: pending=${PENDING_COUNT} passed=${PASSED_COUNT} failed=${FAILED_COUNT}" >&2
        echo "ci-passed conclusion: ${CI_PASSED_CONCLUSION:-(not yet present)}" >&2
        exit 2
    fi

    sleep "$POLL_INTERVAL"
done
