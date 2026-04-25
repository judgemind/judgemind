#!/usr/bin/env bash
# test_check_deploy_workflow_rollout.sh — Tests for check-deploy-workflow-rollout.sh
#
# Creates temporary workflow fixture files to verify that the checker correctly
# requires a service rollout step when a deploy workflow pushes to ECR, while
# allowing opt-out markers and empty (no-push) workflows to pass.
#
# Usage:
#   scripts/tests/test_check_deploy_workflow_rollout.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.
#
# permanent: true

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-deploy-workflow-rollout.sh"
FAILURES=0
TESTS=0

# Use a temp directory under repo tmp/ so we don't pollute the repo
TMPDIR_TEST=$(mktemp -d "$SCRIPT_DIR/../tmp/test_deploy_rollout.XXXXXX")
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# Fixture files must be under .github/workflows/ so the finder picks them up.
create_workflow_file() {
    local name="$1"
    local content="$2"
    local dir="$TMPDIR_TEST/.github/workflows"
    mkdir -p "$dir"
    local path="$dir/$name"
    printf '%s\n' "$content" > "$path"
    echo "$path"
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST/.github"
}

# ─── Test (a): push + aws ecs update-service → pass ─────────────────────────
create_workflow_file "deploy-api.yml" 'jobs:
  build-and-push:
    steps:
      - run: docker push "$IMAGE_BASE" --all-tags
  deploy-dev:
    steps:
      - run: |
          aws ecs update-service \
            --cluster judgemind-dev \
            --service judgemind-api-dev \
            --force-new-deployment'
assert_passes "push + aws ecs update-service → pass (deploy-api.yml shape)"
reset_tmpdir

# ─── Test (b): push + uses ecs-deploy composite → pass ──────────────────────
create_workflow_file "deploy-api.yml" 'jobs:
  build-and-push:
    steps:
      - run: docker push "$IMAGE_BASE" --all-tags
  deploy-dev:
    steps:
      - name: Deploy to ECS
        uses: ./.github/actions/ecs-deploy
        with:
          deploy-mode: service'
assert_passes "push + uses ecs-deploy composite → pass"
reset_tmpdir

# ─── Test (c): push + aws scheduler update-schedule → pass ──────────────────
create_workflow_file "deploy-scraper.yml" 'jobs:
  build-and-push:
    steps:
      - run: docker push "$IMAGE_BASE" --all-tags
  deploy-staging:
    steps:
      - uses: ./.github/actions/ecs-deploy
        with:
          deploy-mode: scheduler
          scheduler-name: judgemind-scraper-dev'
assert_passes "push + scheduler rollout via ecs-deploy → pass (deploy-scraper.yml shape)"
reset_tmpdir

# ─── Test (d): push only, no rollout → fail ──────────────────────────────────
create_workflow_file "deploy-broken.yml" 'jobs:
  build-and-push:
    steps:
      - run: docker push "$IMAGE_BASE" --all-tags'
assert_fails "push only, no rollout → fail"
reset_tmpdir

# ─── Test (e): push only with # rollout-not-required: reason text → pass ────
create_workflow_file "deploy-agent-runner.yml" '# rollout-not-required: task-definition family — ecs:RunTask callers pull :latest. See #2777.
jobs:
  build-and-push:
    steps:
      - run: docker push "$IMAGE_BASE" --all-tags'
assert_passes "push with rollout-not-required marker → pass"
reset_tmpdir

# ─── Test (f): marker without reason text → fail ────────────────────────────
# `# rollout-not-required:` with no non-space text after the colon is
# treated the same as no marker — a reason is mandatory.
create_workflow_file "deploy-bad-optout.yml" '# rollout-not-required:
jobs:
  build-and-push:
    steps:
      - run: docker push "$IMAGE_BASE" --all-tags'
assert_fails "rollout-not-required without reason text → fail"
reset_tmpdir

# ─── Test (g): aws ecs update-service only in a comment → fail ──────────────
# Rollout signal must appear on a non-comment line; comment-only mentions
# do not count.
create_workflow_file "deploy-comment-only.yml" 'jobs:
  build-and-push:
    steps:
      - run: docker push "$IMAGE_BASE" --all-tags
      # Previously: aws ecs update-service --cluster foo (removed)
      - run: echo "no rollout here"'
assert_fails "rollout signal only in comment → fail"
reset_tmpdir

# ─── Test (h): empty workflow file → pass ───────────────────────────────────
create_workflow_file "deploy-empty.yml" ''
assert_passes "empty workflow file → pass (no ECR push, no violation)"
reset_tmpdir

# ─── Test (i): self-match guard ─────────────────────────────────────────────
# The step name in .github/workflows/ci.yml that invokes this guard must not
# itself contain a token that the guard would match, or the guard fails on
# its very first CI run (see issue #2542 / PR #2541 for the class of failure).
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-deploy-workflow-rollout.sh" "yml"

# ─── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
