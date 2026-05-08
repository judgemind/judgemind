#!/usr/bin/env bash
# check-no-ecs-wait-services-stable.sh — Forbid `aws ecs wait services-stable`.
#
# The CI composite action (`.github/actions/ecs-deploy/action.yml`) and
# `scripts/ecs-redeploy.sh` both used to call `aws ecs wait services-stable`
# to wait for an ECS rollout to finish.  That waiter has three real drawbacks
# (see issues #2519 and #2523):
#
#   1. Hard-coded 10-minute ceiling (40 polls * 15s, no way to customize).
#   2. No progress output — a real timeout is indistinguishable from a
#      workflow cancellation (e.g. concurrency.cancel-in-progress).
#   3. Waits for service-level steady state, not the specific deployment
#      we just triggered.  Unrelated rollbacks or parallel deploys can make
#      the waiter return early with a stale "success".
#
# Both callers have been ported off onto `scripts/wait-for-rollout.sh`, a
# deployment-scoped waiter with configurable timeout and streaming progress.
# This check exists to keep `aws ecs wait services-stable` from coming back.
#
# Usage:
#   scripts/check-no-ecs-wait-services-stable.sh          # exits 0 if clean
#   scripts/check-no-ecs-wait-services-stable.sh [dir]    # scan a specific directory
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more tracked files call `aws ecs wait services-stable`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT}"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"

# ─── Pattern ─────────────────────────────────────────────────────────────
# Match `aws ecs wait services-stable` with any internal whitespace run.
# The regex is anchored so `aws-ecs-wait-services-stable` (hypothetical
# variable or filename) does not match.
PATTERN='aws[[:space:]]+ecs[[:space:]]+wait[[:space:]]+services-stable'

# ─── Directories to exclude from the scan ────────────────────────────────
# Repo-walk dir exclusions come from REPO_WALK_EXCLUSIONS in preflight.sh
# (canonical list — single source of truth, see #4308). No per-check
# extras needed here.

# ─── Files that legitimately mention the pattern as context ──────────────
# The check script and its test both have to spell the pattern literally.
# Past incident post-mortems and forward-looking documentation may also
# reference it as context.
#
# Note: comment lines (lines whose first non-whitespace character is `#`)
# are filtered out below, so callers that replaced the pattern may keep
# explanatory comments like `# Replaces aws ecs wait services-stable ...`
# without tripping this check.
EXCLUDE_FILES=(
    "scripts/check-no-ecs-wait-services-stable.sh"
    "scripts/tests/test_check_no_ecs_wait_services_stable.sh"
)

# ─── Build --exclude-dir arguments from the canonical list ──────────────

exclude_args=()
for dir in "${REPO_WALK_EXCLUSIONS[@]}"; do
    exclude_args+=("--exclude-dir=$dir")
done

# ─── Scan the codebase ──────────────────────────────────────────────────

violations=0

matches=$(grep -rnE "$PATTERN" "$SCAN_DIR" "${exclude_args[@]}" 2>/dev/null || true)

if [[ -n "$matches" ]]; then
    while IFS= read -r line; do
        # grep -rn output: <path>:<lineno>:<content>
        file_path="${line%%:*}"
        rest="${line#*:}"
        content="${rest#*:}"

        # Skip matches in the explicitly-excluded files.
        skip=false
        for excl in "${EXCLUDE_FILES[@]}"; do
            # Match either a full path ending in the excluded relative path
            # or an exact match (handles both absolute SCAN_DIR output and
            # test-harness invocation with a bare filename).
            if [[ "$file_path" == *"$excl" ]]; then
                skip=true
                break
            fi
        done
        if "$skip"; then
            continue
        fi

        # Skip docs/investigations/* — post-mortems may reference the
        # pattern as historical context.
        if [[ "$file_path" == *"docs/investigations/"* ]]; then
            continue
        fi

        # Skip comment lines (first non-whitespace character is `#`).
        # This covers shell comments, YAML comments, Python comments, and
        # Dockerfile comments — all formats in use in this repo where the
        # pattern is allowed to appear as context.
        if [[ "$content" =~ ^[[:space:]]*# ]]; then
            continue
        fi

        if [[ $violations -eq 0 ]]; then
            echo "ERROR: Found forbidden 'aws ecs wait services-stable' usage."
            echo ""
            echo "  The following lines call the AWS CLI waiter that was"
            echo "  retired in favor of scripts/wait-for-rollout.sh"
            echo "  (see issues #2519 and #2523):"
            echo ""
        fi

        echo "    $line"
        violations=$((violations + 1))
    done <<< "$matches"
fi

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Found $violations occurrence(s) of 'aws ecs wait services-stable'."
    echo ""
    echo "  Fix: use scripts/wait-for-rollout.sh instead.  It waits on the"
    echo "  specific deployment we just triggered (not service-level steady"
    echo "  state), streams progress output, and accepts a configurable"
    echo "  timeout via --timeout-seconds."
    echo ""
    echo "  Example:"
    echo "    scripts/wait-for-rollout.sh \\"
    echo "      --cluster judgemind-dev \\"
    echo "      --service my-service \\"
    echo "      --task-def-arn \"\$TASK_DEF_ARN\" \\"
    echo "      --timeout-seconds 1200"
    echo ""
    echo "  If the pattern must appear as explanatory context, put it in a"
    echo "  comment (lines starting with '#') or under docs/investigations/."
    exit 1
fi

echo "All clean — no 'aws ecs wait services-stable' usage detected."
exit 0
