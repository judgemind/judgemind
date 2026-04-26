#!/usr/bin/env bash
# verify-pr-in-deployed-sha.sh — Verify whether a merged PR's code is present
# in the currently-deployed dispatcher image.
#
# Motivated by the concurrent-merge deploy-cancellation scenario documented
# in #3076: when two dispatcher PRs merge within ~5 minutes of each other,
# the earlier PR's `deploy-dispatcher.yml` run is cancelled at the
# deploy-to-dev stage (concurrency: cancel-in-progress=true). The later
# PR's deploy then ships a build whose commit is a descendant of the
# earlier one — so the earlier PR's code DID land, piggybacking on the
# superseding rollout. GitHub Actions just shows the earlier run as
# CANCELLED, which is observably misleading.
#
# This helper closes the loop: given a PR number, it fetches the PR's
# merge commit SHA, reads the currently-deployed `version_sha` from the
# dispatcher's CloudWatch startup log, and tests whether the merge SHA
# is an ancestor of the deployed SHA via `git merge-base --is-ancestor`.
#
# Replaces the manual ritual an operator otherwise runs:
#   gh pr view <N> --json mergeCommit
#   aws logs filter-log-events ... | jq ... (find version_sha)
#   git merge-base --is-ancestor <merge-sha> <deployed-sha>
#
# Usage:
#   scripts/verify-pr-in-deployed-sha.sh <pr-number>
#   scripts/verify-pr-in-deployed-sha.sh 3066
#   scripts/verify-pr-in-deployed-sha.sh '#3066'   # leading # is stripped
#
# Exit codes (matching the #3076 acceptance criteria):
#   0 — PR's merge SHA is an ancestor of the deployed SHA (PR landed).
#       A "PR #N landed in deployed SHA <sha> (distance: <n> commits)"
#       line is printed to stdout.
#   1 — PR's merge SHA is NOT an ancestor of the deployed SHA (not yet
#       deployed). A "PR #N is NOT in deployed SHA <sha>" line is
#       printed to stdout.
#   2 — Usage / environment / data error: missing argument, PR not
#       merged, git/gh/aws unavailable, deployed SHA not resolvable
#       locally, or CloudWatch returned no recent startup log. An
#       "error: <reason>" line is printed to stderr.
#
# Environment variables (for testing and overrides):
#   AWS_CLI              — binary name for aws CLI (default: aws).
#                          Tests inject a mock via PATH; this env var
#                          is for callers who want a different aws
#                          wrapper (e.g. scripts/with-secret.sh-style).
#   GH_CLI               — binary name for gh CLI (default: gh).
#   GIT_CLI              — binary name for git CLI (default: git).
#   DISPATCHER_LOG_GROUP — CloudWatch log group (default:
#                          /ecs/judgemind-dispatcher-dev).
#   STARTUP_LOOKBACK_MIN — minutes of history to scan for the latest
#                          startup log line (default: 1440 = 24h).
#                          The daemon emits a startup event on every
#                          Fargate task boot, so 24h is an ample window.
#   AWS_REGION           — AWS region (default: us-west-2).

set -uo pipefail
# AWS CLI v1/v2 portability: suppress pager without --no-cli-pager (v2-only flag). See #3461.
export AWS_PAGER=""

# ── Defaults ────────────────────────────────────────────────────────────────

AWS_CLI="${AWS_CLI:-aws}"
GH_CLI="${GH_CLI:-gh}"
GIT_CLI="${GIT_CLI:-git}"
DISPATCHER_LOG_GROUP="${DISPATCHER_LOG_GROUP:-/ecs/judgemind-dispatcher-dev}"
STARTUP_LOOKBACK_MIN="${STARTUP_LOOKBACK_MIN:-1440}"
AWS_REGION="${AWS_REGION:-us-west-2}"

# ── Usage ───────────────────────────────────────────────────────────────────

usage() {
    cat <<'USAGE'
Usage: scripts/verify-pr-in-deployed-sha.sh <pr-number>

Arguments:
  <pr-number>   Merged pull request number (e.g. 3066 or '#3066').

Options:
  --help, -h    Show this help message.

Exit codes:
  0 — PR landed in deployed SHA.
  1 — PR is NOT in deployed SHA (not yet deployed).
  2 — Usage / environment / data error.
USAGE
}

# ── Parse arguments ─────────────────────────────────────────────────────────

if [[ $# -eq 0 ]]; then
    echo "error: pr-number is required" >&2
    usage >&2
    exit 2
fi

case "${1:-}" in
    --help|-h)
        usage
        exit 0
        ;;
esac

PR_ARG="$1"
PR_NUM="${PR_ARG#\#}"

if ! [[ "$PR_NUM" =~ ^[0-9]+$ ]]; then
    echo "error: pr-number must be an integer (got '$PR_ARG')" >&2
    exit 2
fi

# ── 1. Fetch PR merge commit SHA via gh ────────────────────────────────────

if ! command -v "$GH_CLI" > /dev/null 2>&1; then
    echo "error: $GH_CLI CLI not found on PATH" >&2
    exit 2
fi

PR_JSON=$("$GH_CLI" pr view "$PR_NUM" --repo judgemind/judgemind --json mergeCommit,state,number 2>/dev/null) || {
    echo "error: gh pr view #$PR_NUM failed (authentication, rate limit, or PR not found?)" >&2
    exit 2
}

PR_STATE=$(echo "$PR_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))')
if [[ "$PR_STATE" != "MERGED" ]]; then
    echo "error: PR #$PR_NUM is not merged (state=$PR_STATE); nothing to verify" >&2
    exit 2
fi

MERGE_SHA=$(echo "$PR_JSON" | python3 -c 'import json,sys; mc=json.load(sys.stdin).get("mergeCommit") or {}; print(mc.get("oid",""))')
if [[ -z "$MERGE_SHA" ]]; then
    echo "error: PR #$PR_NUM has no mergeCommit.oid (API shape change?)" >&2
    exit 2
fi

# ── 2. Fetch deployed version_sha from CloudWatch ──────────────────────────

if ! command -v "$AWS_CLI" > /dev/null 2>&1; then
    echo "error: $AWS_CLI CLI not found on PATH" >&2
    exit 2
fi

# Resolve the start of the lookback window in millis-since-epoch. Using
# python3 (already required above for JSON parsing) avoids a GNU-vs-BSD
# `date -d` portability trap.
START_MS=$(python3 -c "import time; print(int((time.time() - ${STARTUP_LOOKBACK_MIN} * 60) * 1000))")

# Grep the log group for any line containing `"event": "startup"` — the
# daemon emits it as a JSON-formatted log record per _JsonFormatter in
# scripts/dispatcher/daemon.py. Ordering events by @timestamp desc via
# filter-log-events requires an Insights query; for the CLI path we
# instead fetch matching events, sort by timestamp in python, and pick
# the latest. Cheap enough — the filter keeps the payload small.
FILTER_JSON=$("$AWS_CLI" logs filter-log-events \
    --log-group-name "$DISPATCHER_LOG_GROUP" \
    --start-time "$START_MS" \
    --filter-pattern '"event" "startup"' \
    --region "$AWS_REGION" \
    --output json 2>/dev/null) || {
    echo "error: aws logs filter-log-events failed for $DISPATCHER_LOG_GROUP" >&2
    exit 2
}

DEPLOYED_SHA=$(echo "$FILTER_JSON" | python3 -c '
import json, sys
data = json.load(sys.stdin)
events = data.get("events", []) or []
# Each event.message is a single JSON log line emitted by
# dispatcher/daemon.py _JsonFormatter. Parse and keep only real startup
# events (event == "startup"), then pick the latest by timestamp.
best = None
for ev in events:
    msg = ev.get("message", "") or ""
    try:
        parsed = json.loads(msg)
    except ValueError:
        continue
    if parsed.get("event") != "startup":
        continue
    ts = ev.get("timestamp", 0) or 0
    vs = parsed.get("version_sha")
    if not vs:
        continue
    if best is None or ts > best[0]:
        best = (ts, vs)
if best:
    print(best[1])
')

if [[ -z "$DEPLOYED_SHA" || "$DEPLOYED_SHA" == "unknown" ]]; then
    echo "error: no recent dispatcher startup log with a resolvable version_sha in $DISPATCHER_LOG_GROUP (last ${STARTUP_LOOKBACK_MIN} min)" >&2
    exit 2
fi

# ── 3. Compare via git merge-base --is-ancestor ────────────────────────────

if ! command -v "$GIT_CLI" > /dev/null 2>&1; then
    echo "error: $GIT_CLI CLI not found on PATH" >&2
    exit 2
fi

# Make sure the deployed SHA is resolvable locally. The daemon logs the
# short (7-char) SHA baked at build time; if the operator's working copy
# is stale, the short SHA may not exist locally yet. Attempt to resolve
# it before calling merge-base so we can emit a clearer error.
if ! "$GIT_CLI" rev-parse --verify --quiet "${DEPLOYED_SHA}^{commit}" > /dev/null; then
    echo "error: deployed SHA '$DEPLOYED_SHA' not resolvable in local git — run 'git fetch origin main' and retry" >&2
    exit 2
fi

if ! "$GIT_CLI" rev-parse --verify --quiet "${MERGE_SHA}^{commit}" > /dev/null; then
    echo "error: PR merge SHA '$MERGE_SHA' not resolvable in local git — run 'git fetch origin main' and retry" >&2
    exit 2
fi

# Distance: how many commits between merge_sha (exclusive) and deployed_sha
# (inclusive). Useful operator context — a distance of 0 means the deploy
# is exactly this PR; a distance of N means N commits followed it.
DISTANCE=$("$GIT_CLI" rev-list --count "${MERGE_SHA}..${DEPLOYED_SHA}" 2>/dev/null || echo "?")

ancestor_rc=0
"$GIT_CLI" merge-base --is-ancestor "$MERGE_SHA" "$DEPLOYED_SHA" || ancestor_rc=$?

case "$ancestor_rc" in
    0)
        echo "PR #${PR_NUM} landed in deployed SHA ${DEPLOYED_SHA} (merge SHA ${MERGE_SHA:0:7}, distance: ${DISTANCE} commits)"
        exit 0
        ;;
    1)
        echo "PR #${PR_NUM} is NOT in deployed SHA ${DEPLOYED_SHA} (merge SHA ${MERGE_SHA:0:7})"
        exit 1
        ;;
    *)
        echo "error: git merge-base --is-ancestor returned unexpected exit $ancestor_rc" >&2
        exit 2
        ;;
esac
