#!/usr/bin/env bash
# test_check_deploy_workflow_rollout.sh — Tests for check-deploy-workflow-rollout.sh
#
# Synthesizes deploy-*.yml workflows in an isolated TMPDIR_TEST/.github/workflows/
# directory, then runs the guard against TMPDIR_TEST and asserts pass/fail.
#
# Usage:
#   scripts/tests/test_check_deploy_workflow_rollout.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-deploy-workflow-rollout.sh"
FAILURES=0
TESTS=0

# Use a temp directory so we don't pollute the repo
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

mkdir -p "$TMPDIR_TEST/.github/workflows"

create_workflow() {
    local name="$1"
    local content="$2"
    local path="$TMPDIR_TEST/.github/workflows/$name"
    printf '%s\n' "$content" > "$path"
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
    rm -rf "$TMPDIR_TEST/.github/workflows"
    mkdir -p "$TMPDIR_TEST/.github/workflows"
}

# ─── Test 1: build+push only, no rollout — should fail ─────────────────
create_workflow "deploy-foo.yml" '
name: Deploy Foo
on:
  push:
    branches: [main]
jobs:
  build-and-push:
    runs-on: ubuntu-latest
    steps:
      - run: |
          docker build -t foo .
          docker push foo:latest
'
assert_fails "build+push without rollout is detected"
reset_tmpdir

# ─── Test 2: aws ecs update-service satisfies rollout ─────────────────
create_workflow "deploy-foo.yml" '
name: Deploy Foo
on:
  push:
    branches: [main]
jobs:
  build-and-push:
    runs-on: ubuntu-latest
    steps:
      - run: docker push foo:latest
  deploy:
    needs: build-and-push
    runs-on: ubuntu-latest
    steps:
      - run: aws ecs update-service --cluster x --service y --force-new-deployment
'
assert_passes "aws ecs update-service satisfies rollout"
reset_tmpdir

# ─── Test 3: aws scheduler update-schedule satisfies rollout ──────────
create_workflow "deploy-foo.yml" '
name: Deploy Foo
on:
  push:
    branches: [main]
jobs:
  build-and-push:
    runs-on: ubuntu-latest
    steps:
      - run: docker push foo:latest
  deploy:
    needs: build-and-push
    runs-on: ubuntu-latest
    steps:
      - run: aws scheduler update-schedule --name x --schedule-expression "rate(1 hour)"
'
assert_passes "aws scheduler update-schedule satisfies rollout"
reset_tmpdir

# ─── Test 4: ./.github/actions/ecs-deploy composite satisfies rollout ──
create_workflow "deploy-foo.yml" '
name: Deploy Foo
on:
  push:
    branches: [main]
jobs:
  build-and-push:
    runs-on: ubuntu-latest
    steps:
      - run: docker push foo:latest
  deploy:
    needs: build-and-push
    runs-on: ubuntu-latest
    steps:
      - uses: ./.github/actions/ecs-deploy
        with:
          task-family: foo
'
assert_passes "ecs-deploy composite action satisfies rollout"
reset_tmpdir

# ─── Test 5: aws ecs register-task-definition satisfies rollout ───────
# Covers the agent-runner pattern: task-def-only family, no service to roll.
create_workflow "deploy-foo.yml" '
name: Deploy Foo
on:
  push:
    branches: [main]
jobs:
  build-and-push:
    runs-on: ubuntu-latest
    steps:
      - run: docker push foo:latest
      - run: aws ecs register-task-definition --cli-input-json file:///tmp/td.json
'
assert_passes "aws ecs register-task-definition satisfies rollout (agent-runner pattern)"
reset_tmpdir

# ─── Test 6: opt-out marker passes ─────────────────────────────────────
create_workflow "deploy-foo.yml" '
name: Deploy Foo
# deploy-rollout-lint: build-only
#   Build half only — task-def re-register lands in a follow-up issue.
on:
  push:
    branches: [main]
jobs:
  build-and-push:
    runs-on: ubuntu-latest
    steps:
      - run: docker push foo:latest
'
assert_passes "deploy-rollout-lint: build-only opt-out is honored"
reset_tmpdir

# ─── Test 7: workflow without docker push is not in scope ──────────────
# A non-deploying workflow is not flagged even though its name matches.
create_workflow "deploy-foo.yml" '
name: Deploy Foo
on:
  push:
    branches: [main]
jobs:
  notify:
    runs-on: ubuntu-latest
    steps:
      - run: echo "no image push here"
'
assert_passes "workflow without docker push is not in scope"
reset_tmpdir

# ─── Test 8: non-deploy workflow filename is not scanned ───────────────
# Even a `docker push` in `ci.yml` does not trigger this guard — it only
# applies to `deploy-*.yml` files.
create_workflow "ci.yml" '
name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: docker push foo:latest
'
assert_passes "non-deploy workflow filename is not in scope"
reset_tmpdir

# ─── Test 9: empty workflows directory passes ──────────────────────────
assert_passes "Empty workflows directory passes"

# ─── Test 10: no workflows directory passes (subdir scan tolerated) ───
# When called with a SCAN_DIR that has no `.github/workflows/`, the
# script exits 0 cleanly — only the repo-root scan exits 2.
TEST_NOWFDIR=$(mktemp -d)
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" "$TEST_NOWFDIR" > /dev/null 2>&1; then
    echo "PASS: subdir without .github/workflows/ tolerated"
else
    echo "FAIL: subdir without .github/workflows/ should pass"
    FAILURES=$((FAILURES + 1))
fi
rm -rf "$TEST_NOWFDIR"

# ─── Test 11: real .github/workflows/ tree on this repo passes ─────────
# Smoke check — every `deploy-*.yml` in the actual repo must satisfy
# the guard at the time this test runs. AC1 of #2777.
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" "$REPO_ROOT" > /dev/null 2>&1; then
    echo "PASS: real .github/workflows/ tree passes the guard"
else
    echo "FAIL: real .github/workflows/ tree fails the guard"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 12: multiple workflows, one violating ────────────────────────
# Mixing a clean workflow with a violating one must still flag the bad one.
create_workflow "deploy-good.yml" '
name: Deploy Good
on: [push]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - run: docker push good:latest
      - run: aws ecs update-service --cluster x --service y --force-new-deployment
'
create_workflow "deploy-bad.yml" '
name: Deploy Bad
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: docker push bad:latest
'
assert_fails "violating workflow flagged among clean siblings"
reset_tmpdir

# ─── Test 13: opt-out marker is exact-match (typo not honored) ─────────
# The marker is a literal string match (grep -F) — typos do not bypass.
# Note: the typo here is a missing space after the colon.
create_workflow "deploy-foo.yml" '
name: Deploy Foo
# deploy-rollout-lint:build-only
#   Misspelled marker — missing space after colon.
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: docker push foo:latest
'
assert_fails "misspelled opt-out marker is not honored"
reset_tmpdir

# ─── Test 14: No self-match on ci.yml step name ────────────────────────
# The CI step name that runs this guard must not itself contain a
# pattern the guard scans for. See issue #2542 for the class of failure.
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-deploy-workflow-rollout.sh" "yml"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
