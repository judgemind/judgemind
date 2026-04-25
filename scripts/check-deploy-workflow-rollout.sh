#!/usr/bin/env bash
# check-deploy-workflow-rollout.sh — Every deploy workflow that pushes to ECR
# must also roll a service (or declare an explicit opt-out).
#
# Background
# ----------
# Deploy workflows that push a new image to ECR but never trigger an ECS
# service rollout leave the running tasks on stale image digests.  The bug
# is silent: CI goes green, the push looks successful, but the service
# keeps serving the previous build.
#
# Rule
# ----
# For each `.github/workflows/deploy-*.yml`:
#   - If an ECR-push signal is present (non-comment line matching
#     `aws ecr` OR `docker push`), the file MUST also contain a rollout
#     signal (`aws ecs update-service`, `uses: ./.github/actions/ecs-deploy`,
#     or `aws scheduler update-schedule`) OR an explicit opt-out comment
#     `# rollout-not-required: <reason>` (reason must be non-empty).
#
# Opt-out
# -------
# Task-definition-family workflows (e.g. `deploy-agent-runner.yml`) push
# an image but rely on `ecs:RunTask` callers always pulling `:latest` —
# they never roll a service.  Add the marker:
#   # rollout-not-required: task-definition family — ecs:RunTask callers
#   #   pull :latest.  See <issue>.
#
# Usage:
#   scripts/check-deploy-workflow-rollout.sh          # exits 0 if clean
#   scripts/check-deploy-workflow-rollout.sh [dir]    # scan a specific directory
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more deploy workflows push to ECR without rolling a service.
#
# permanent: true

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT}"

# ─── ECR-push signal patterns ───────────────────────────────────────────────
# A non-comment line matching either of these indicates the workflow pushes
# a new image to ECR and therefore requires a rollout (or opt-out).
ECR_PUSH_PATTERN_AWS='aws[[:space:]]+ecr'
ECR_PUSH_PATTERN_DOCKER='docker[[:space:]]+push'

# ─── Rollout signal patterns ────────────────────────────────────────────────
# Any of these signals on a non-comment line confirms the workflow rolls a
# service after pushing.
ROLLOUT_PATTERN_UPDATE_SERVICE='aws[[:space:]]+ecs[[:space:]]+update-service'
ROLLOUT_PATTERN_ECS_DEPLOY='uses:[[:space:]]+\./.github/actions/ecs-deploy'
ROLLOUT_PATTERN_SCHEDULER='aws[[:space:]]+scheduler[[:space:]]+update-schedule'

# ─── Opt-out marker pattern ─────────────────────────────────────────────────
# Comment line of the form `# rollout-not-required: <non-empty reason>`.
# The reason must contain at least one non-space character after the colon.
OPTOUT_PATTERN='#[[:space:]]+rollout-not-required:[[:space:]]+[^[:space:]]'

violations=0

# Find all deploy-*.yml files under .github/workflows/ within SCAN_DIR.
# Use find for portability (Bash 3.2 compatible).
workflow_files=()
while IFS= read -r -d '' f; do
    workflow_files+=("$f")
done < <(find "$SCAN_DIR" -path "*/.github/workflows/deploy-*.yml" -print0 2>/dev/null | sort -z)

# Also check top-level .github/workflows/deploy-*.yml when SCAN_DIR is a
# fixture directory in tests (no subdirectory nesting).
while IFS= read -r -d '' f; do
    workflow_files+=("$f")
done < <(find "$SCAN_DIR" -maxdepth 3 -name "deploy-*.yml" -not -path "*/.github/workflows/*" -print0 2>/dev/null | sort -z)

# Deduplicate (sort -u on the array).
if [[ ${#workflow_files[@]} -gt 0 ]]; then
    IFS=$'\n' read -r -d '' -a workflow_files < <(printf '%s\n' "${workflow_files[@]}" | sort -u; printf '\0') || true
fi

check_workflow() {
    local file="$1"

    local has_ecr_push=false
    local has_rollout=false
    local has_optout=false

    while IFS= read -r line; do
        # Opt-out marker IS a comment — check it before the comment-skip below.
        if [[ "$line" =~ $OPTOUT_PATTERN ]]; then
            has_optout=true
        fi

        # Skip comment lines (first non-whitespace character is `#`) for
        # signal detection — comments are allowed to reference these patterns
        # as documentation context.
        if [[ "$line" =~ ^[[:space:]]*# ]]; then
            continue
        fi

        # ECR-push signal.
        if [[ "$line" =~ $ECR_PUSH_PATTERN_AWS ]] || [[ "$line" =~ $ECR_PUSH_PATTERN_DOCKER ]]; then
            has_ecr_push=true
        fi

        # Rollout signal.
        if [[ "$line" =~ $ROLLOUT_PATTERN_UPDATE_SERVICE ]] || \
           [[ "$line" =~ $ROLLOUT_PATTERN_ECS_DEPLOY ]] || \
           [[ "$line" =~ $ROLLOUT_PATTERN_SCHEDULER ]]; then
            has_rollout=true
        fi
    done < "$file"

    # Violation: ECR push without rollout and without opt-out.
    if "$has_ecr_push" && ! "$has_rollout" && ! "$has_optout"; then
        return 1
    fi
    return 0
}

for wf in "${workflow_files[@]}"; do
    if ! check_workflow "$wf"; then
        if [[ $violations -eq 0 ]]; then
            echo "ERROR: Deploy workflow(s) push to ECR but do not roll a service."
            echo ""
            echo "  Each of the following workflows pushes a new image to ECR but"
            echo "  contains no rollout step (aws ecs update-service,"
            echo "  uses: ./.github/actions/ecs-deploy, or"
            echo "  aws scheduler update-schedule)."
            echo ""
            echo "  Violations:"
        fi
        echo "    $wf"
        violations=$((violations + 1))
    fi
done

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Found $violations violation(s)."
    echo ""
    echo "  Fix: add a rollout step after the docker push / ecr push.  Use"
    echo "  deploy-api.yml (service rollout via ecs-deploy composite) or"
    echo "  deploy-scraper.yml (scheduler rollout via aws scheduler"
    echo "  update-schedule) as patterns to mirror."
    echo ""
    echo "  If the workflow intentionally omits a service rollout (e.g. it"
    echo "  builds a task-definition-family image where ecs:RunTask callers"
    echo "  always pull :latest), add the opt-out marker anywhere in the file:"
    echo ""
    echo "    # rollout-not-required: <reason>  (reason must be non-empty)"
    echo ""
    echo "  See deploy-agent-runner.yml for an example."
    exit 1
fi

echo "All clean — every deploy workflow that pushes to ECR also rolls a service (or is opted out)."
exit 0
