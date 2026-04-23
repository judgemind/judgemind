#!/usr/bin/env bash
# test_check_no_inline_ecs_healthcheck.sh — Tests for
# scripts/check-no-inline-ecs-healthcheck.sh.
#
# Creates temporary workflow YAML files and verifies that the checker
# correctly detects inline ECS health-check bash while allowing
# extracted-script callers, comment-only references, and unrelated
# workflows through.
#
# Usage:
#   scripts/tests/test_check_no_inline_ecs_healthcheck.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-inline-ecs-healthcheck.sh"
FAILURES=0
TESTS=0

# Use a temp directory so we don't pollute the repo.
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

create_test_file() {
    local name="$1"
    local content="$2"
    local path="$TMPDIR_TEST/$name"
    local dir
    dir="$(dirname "$path")"
    mkdir -p "$dir"
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
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

# ─── Test 1: Inline ECS health check (the exact pattern from #2605) ──────
# A workflow with `aws ecs describe-services` + `runningCount` in non-
# comment lines must trip the guard. This is the motivating bug class
# this check was built for.
create_test_file ".github/workflows/bad-inline.yml" 'jobs:
  post-deploy-health-check:
    runs-on: ubuntu-latest
    steps:
      - run: |
          SERVICE_DESC=$(aws ecs describe-services --cluster foo --services bar --query "services[0]" --output json)
          RUNNING_COUNT=$(echo "$SERVICE_DESC" | jq -r ".runningCount")
          if [ "$RUNNING_COUNT" -gt 0 ]; then
            echo "healthy"
          fi'
assert_fails "Inline aws ecs describe-services + runningCount is detected"
reset_tmpdir

# ─── Test 2: Extracted-script caller should pass ─────────────────────────
# The recommended replacement — a single run step invoking the shared
# script — must NOT trip the guard.
create_test_file ".github/workflows/good-extracted.yml" 'jobs:
  post-deploy-health-check:
    runs-on: ubuntu-latest
    steps:
      - name: Post-deploy ingestion worker health check
        env:
          ECS_CLUSTER: judgemind-dev
          INGESTION_SERVICE: judgemind-ingestion-worker-dev
          LOG_GROUP: /ecs/judgemind-ingestion-worker-dev
        run: bash scripts/ecs-post-deploy-healthcheck.sh'
assert_passes "Extracted-script caller passes"
reset_tmpdir

# ─── Test 3: Workflow with only comments mentioning the patterns ─────────
# A workflow whose only references are in YAML comments (starting with
# # after whitespace) must NOT trip the guard.
create_test_file ".github/workflows/comments-only.yml" 'jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      # Previously ran: aws ecs describe-services to read runningCount.
      # Replaced with the extracted scripts/ecs-post-deploy-healthcheck.sh
      - run: bash scripts/ecs-post-deploy-healthcheck.sh'
assert_passes "Comment-only references to the patterns are allowed"
reset_tmpdir

# ─── Test 4: Unrelated describe-services use (no runningCount) passes ────
# A workflow that calls `aws ecs describe-services` but never references
# runningCount is not an inline health-check — e.g. a step that reads the
# service's task definition ARN. Must not trip the guard.
create_test_file ".github/workflows/describe-only.yml" 'jobs:
  info:
    runs-on: ubuntu-latest
    steps:
      - run: |
          TASK_DEF=$(aws ecs describe-services --cluster foo --services bar --query "services[0].taskDefinition" --output text)
          echo "task_def=$TASK_DEF"'
assert_passes "describe-services without runningCount does not trip the guard"
reset_tmpdir

# ─── Test 5: runningCount alone (no describe-services) passes ────────────
# A workflow that references runningCount in another context (e.g. a
# documentation string) without calling describe-services must not trip.
create_test_file ".github/workflows/count-only.yml" 'jobs:
  doc:
    runs-on: ubuntu-latest
    steps:
      - run: |
          echo "We trust runningCount values from CloudWatch metrics"'
assert_passes "runningCount alone without describe-services does not trip"
reset_tmpdir

# ─── Test 6: Mixed file — non-comment violation is caught even with
# comments also referencing the patterns ─────────────────────────────────
create_test_file ".github/workflows/mixed.yml" 'jobs:
  bad:
    runs-on: ubuntu-latest
    steps:
      # This used to be inline:
      #   aws ecs describe-services reads runningCount
      # but we are STILL doing it inline here:
      - run: |
          aws ecs describe-services --cluster foo --services bar --query "services[0]" > svc.json
          jq -r ".runningCount" svc.json'
assert_fails "Mixed file: real inline code is still caught when comments also mention the patterns"
reset_tmpdir

# ─── Test 7: Empty workflows directory passes ────────────────────────────
mkdir -p "$TMPDIR_TEST/.github/workflows"
assert_passes "Empty .github/workflows directory passes"
reset_tmpdir

# ─── Test 8: Scanning a directory with no .github/workflows/ falls back
# to scanning SCAN_DIR directly — confirm bad YAML there still trips. ────
create_test_file "flat.yml" 'jobs:
  bad:
    runs-on: ubuntu-latest
    steps:
      - run: |
          aws ecs describe-services --cluster foo --services bar > out.json
          jq -r ".runningCount" out.json'
assert_fails "Flat scan (no .github/workflows) still detects inline pattern"
reset_tmpdir

# ─── Test 9: Two separate workflows — one bad, one good — the bad one
# trips the guard. ───────────────────────────────────────────────────────
create_test_file ".github/workflows/ok.yml" 'jobs:
  ok:
    runs-on: ubuntu-latest
    steps:
      - run: bash scripts/ecs-post-deploy-healthcheck.sh'
create_test_file ".github/workflows/bad.yml" 'jobs:
  bad:
    runs-on: ubuntu-latest
    steps:
      - run: |
          aws ecs describe-services --cluster x --services y > o.json
          jq -r ".runningCount" o.json'
assert_fails "A bad workflow alongside a good one still trips the guard"
reset_tmpdir

# ─── Test 10: Both patterns in the same multi-line `run` block trigger ──
# (sanity check — this is the most common real-world signature)
create_test_file ".github/workflows/multirun.yml" 'jobs:
  bad:
    runs-on: ubuntu-latest
    steps:
      - run: |
          SERVICE_DESC=$(aws ecs describe-services --cluster foo --services bar)
          RUNNING_COUNT=$(echo "$SERVICE_DESC" | jq -r ".runningCount")
          DESIRED_COUNT=$(echo "$SERVICE_DESC" | jq -r ".desiredCount")
          if [ "$DESIRED_COUNT" -eq 0 ] && [ "$RUNNING_COUNT" -eq 0 ]; then
            echo "scale-to-zero healthy"
          fi'
assert_fails "Multi-line run block with both patterns trips the guard"
reset_tmpdir

# ─── Test 11: Non-.yml files in workflows dir are ignored ────────────────
# find is restricted to *.yml and *.yaml — a README.md in workflows/
# should not be scanned even if it happens to contain the patterns.
create_test_file ".github/workflows/README.md" 'This file documents the
legacy aws ecs describe-services workflow and its runningCount checks.'
assert_passes "Non-YAML files in workflows/ are not scanned"
reset_tmpdir

# ─── Test 12: docs/investigations/ is ignored even when it contains the pattern ──
create_test_file "docs/investigations/healthcheck-drift-2026-04.md" '# Healthcheck drift

The drift happened because we had inline `aws ecs describe-services` that
read `runningCount` and failed when the service was scaled to zero.'
# Note: docs/investigations/ does not live under .github/workflows/, so
# the default scan path (.github/workflows/) will not find it. Even if
# the scan falls back to SCAN_DIR (because no workflows dir exists), the
# guard still skips docs/investigations/ by path rule — we test the
# latter branch here by not creating a workflows dir.
assert_passes "docs/investigations/ references to the patterns are allowed"
reset_tmpdir

# ─── Test 13: No self-match on ci.yml step name ──────────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain the forbidden pattern tokens. See #2541/#2542 for
# the motivating incident and scripts/tests/_guard_self_match_helpers.sh.
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-no-inline-ecs-healthcheck.sh" "yml"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
