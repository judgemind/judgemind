#!/usr/bin/env bash
# check-no-inline-ecs-healthcheck.sh — Forbid new inline ECS health-check
# bash in GitHub Actions workflow YAML.
#
# Motivation:
#   Issue #2605 was caused by drift between scripts/wait-for-rollout.sh
#   (fixed in #2585 to accept desired=0/running=0 as healthy) and the
#   inline bash in .github/workflows/deploy-scraper.yml's
#   post-deploy-health-check job, which kept the stricter RUNNING_COUNT>0
#   requirement and failed every dev deploy for weeks. The fix in #2606
#   patched the inline bash — but a third copy of the same rule is exactly
#   the kind of thing that drifts again. Issue #2608 extracts the logic
#   into scripts/ecs-post-deploy-healthcheck.sh (with unit tests).
#
#   This guard keeps new inline ECS health-check logic from reappearing in
#   workflow YAML. Callers must invoke the extracted script instead.
#
# Pattern:
#   A workflow file is flagged when it contains BOTH
#       aws ecs describe-services     (or logs describe-log-streams, which
#                                      is how the log-check half starts)
#     AND
#       runningCount                  (the give-away jq field for the
#                                      RUNNING_COUNT == DESIRED_COUNT rule)
#   on any two non-comment lines. That combination is the signature of
#   the inline health-check block; extracted-script callers don't have it
#   (they have `bash scripts/ecs-post-deploy-healthcheck.sh` and nothing
#   else).
#
# Usage:
#   scripts/check-no-inline-ecs-healthcheck.sh          # scan default workflow dir
#   scripts/check-no-inline-ecs-healthcheck.sh [dir]    # scan a specific directory
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more workflow YAML files contain inline ECS health-check logic.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT}"

# ─── Patterns that together indicate inline health-check bash ──────────────
DESCRIBE_PATTERN='aws[[:space:]]+ecs[[:space:]]+describe-services'
COUNT_PATTERN='runningCount'

# ─── Files that legitimately mention the patterns as context ───────────────
# The guard script itself and its test file have to spell the patterns
# literally. docs/investigations/* may reference them as post-mortem
# context. Anything outside .github/workflows/ is ignored by construction
# (we only scan that dir), but we still defensively exclude the guard's
# own files by basename.
EXCLUDE_FILES=(
    "scripts/check-no-inline-ecs-healthcheck.sh"
    "scripts/tests/test_check_no_inline_ecs_healthcheck.sh"
)

# ─── Scan ──────────────────────────────────────────────────────────────────
# We only scan .github/workflows/*.yml — that's where the drift happened
# and where the extraction rule applies. Actions composite steps under
# .github/actions/ deliberately wrap aws-cli calls; they are not the
# target of this check.
WORKFLOWS_DIR="$SCAN_DIR/.github/workflows"

# If there is no workflows dir (e.g. when invoked against a test TMPDIR),
# fall back to scanning SCAN_DIR for *.yml files directly.
if [[ ! -d "$WORKFLOWS_DIR" ]]; then
    WORKFLOWS_DIR="$SCAN_DIR"
fi

# Collect candidate files. Uses a while-read loop instead of mapfile so
# the script works on bash 3.x (macOS default) as well as bash 4+.
yml_files=()
while IFS= read -r f; do
    yml_files+=("$f")
done < <(find "$WORKFLOWS_DIR" -maxdepth 3 -type f \
    \( -name '*.yml' -o -name '*.yaml' \) 2>/dev/null | sort)

violations=0

for file in ${yml_files[@]+"${yml_files[@]}"}; do
    # Skip excluded files by path suffix.
    skip=false
    for excl in "${EXCLUDE_FILES[@]}"; do
        if [[ "$file" == *"$excl" ]]; then
            skip=true
            break
        fi
    done
    if "$skip"; then
        continue
    fi

    # Skip docs/investigations/* — post-mortems may reference the pattern
    # as historical context.
    if [[ "$file" == *"docs/investigations/"* ]]; then
        continue
    fi

    # Strip comments (lines whose first non-whitespace char is '#') before
    # matching. Both patterns must appear on non-comment lines in the same
    # file for the file to trigger.
    non_comment_content=$(grep -vE '^[[:space:]]*#' "$file" 2>/dev/null || true)

    if [[ -z "$non_comment_content" ]]; then
        continue
    fi

    has_describe=false
    has_count=false
    if echo "$non_comment_content" | grep -qE "$DESCRIBE_PATTERN"; then
        has_describe=true
    fi
    if echo "$non_comment_content" | grep -qE "$COUNT_PATTERN"; then
        has_count=true
    fi

    if "$has_describe" && "$has_count"; then
        if [[ $violations -eq 0 ]]; then
            echo "ERROR: Found inline ECS health-check bash in workflow YAML."
            echo ""
            echo "  These workflow files contain both 'aws ecs describe-services'"
            echo "  and a 'runningCount' check — the signature of inline ECS"
            echo "  health-check logic. Use scripts/ecs-post-deploy-healthcheck.sh"
            echo "  instead, which codifies the scale-to-zero rule (running==desired==0"
            echo "  treated as healthy) in one place. See issues #2605 and #2608."
            echo ""
        fi
        echo "    $file"
        violations=$((violations + 1))
    fi
done

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Fix: replace the inline bash with"
    echo "    - name: Post-deploy health check"
    echo "      env:"
    echo "        ECS_CLUSTER: <cluster>"
    echo "        INGESTION_SERVICE: <service>"
    echo "        LOG_GROUP: <log-group>"
    echo "      run: bash scripts/ecs-post-deploy-healthcheck.sh"
    echo ""
    echo "  If a workflow must reference these patterns as explanatory"
    echo "  context, put them in comment lines (starting with '#') or move"
    echo "  the note to docs/investigations/."
    exit 1
fi

echo "All clean — no inline ECS health-check bash detected in workflow YAML."
exit 0
