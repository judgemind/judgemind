#!/usr/bin/env bash
# test_check_cleanup_step_continue_on_error.sh — Tests for
# check-cleanup-step-continue-on-error.sh.
#
# Verifies the script:
#   - exits 0 on a clean workflow tree,
#   - exits 1 and names the offending step:line when a cleanup step on a
#     primary-success path lacks `continue-on-error: true`,
#   - covers all three trigger patterns named in #4241 ACs:
#       (a) name: ^Auto-close,
#       (b) if: ... healthy == 'true',
#       (c) if: success() && ... has_failures == 'false',
#   - is conservative: does NOT flag load-bearing steps (failure() path
#     or always() audit) and does NOT flag steps that don't call
#     `gh issue close|comment|list` or `gh api`.
#
# Usage:
#   scripts/tests/test_check_cleanup_step_continue_on_error.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-cleanup-step-continue-on-error.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
    mkdir -p "$TMPDIR_TEST/.github/workflows"
}

write_workflow() {
    local name="$1"
    local content="$2"
    local path="$TMPDIR_TEST/.github/workflows/$name"
    printf '%s' "$content" > "$path"
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    local out
    if out=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1); then
        echo "FAIL: $desc (expected exit 1, got 0)"
        echo "  output: $out"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_fails_with_message() {
    local desc="$1"
    local needle="$2"
    TESTS=$((TESTS + 1))
    local out
    out=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true)
    if printf '%s' "$out" | grep -q "$needle"; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (output did not contain '$needle')"
        echo "  output: $out"
        FAILURES=$((FAILURES + 1))
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    local out
    if out=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1); then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected exit 0, got non-zero)"
        echo "  output: $out"
        FAILURES=$((FAILURES + 1))
    fi
}

# ─── Test 1: Clean — Auto-close step has continue-on-error: true ──────────
reset_tmpdir
write_workflow "alert.yml" 'name: Alert
on:
  schedule:
    - cron: "*/15 * * * *"
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - run: echo "running primary check"
        id: check

      - name: Auto-close resolved alert issues
        if: steps.check.outputs.healthy == '"'"'true'"'"'
        continue-on-error: true
        env:
          GH_TOKEN: secret
        run: |
          gh issue close 123 --reason completed
'
assert_passes "Auto-close step with continue-on-error: true passes"

# ─── Test 2: Trigger (a) name: ^Auto-close — missing continue-on-error ────
reset_tmpdir
write_workflow "alert.yml" 'name: Alert
on:
  schedule:
    - cron: "*/15 * * * *"
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - run: echo "running primary check"
        id: check

      - name: Auto-close resolved alert issues
        if: steps.check.outputs.healthy == '"'"'true'"'"'
        env:
          GH_TOKEN: secret
        run: |
          gh issue close 123 --reason completed
'
assert_fails "Auto-close step missing continue-on-error: true fails"
assert_fails_with_message "Failure message names the workflow file" "alert.yml"
assert_fails_with_message "Failure message names the step" "Auto-close resolved alert issues"

# ─── Test 3: Trigger (b) if: healthy == 'true' (no Auto-close name) ───────
reset_tmpdir
write_workflow "healthy.yml" 'name: Healthy
on: [push]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - id: check
        run: echo "healthy=true" >> "$GITHUB_OUTPUT"

      - name: Cleanup resolved issues
        if: steps.check.outputs.healthy == '"'"'true'"'"'
        env:
          GH_TOKEN: secret
        run: |
          gh issue close 1 --reason completed
'
assert_fails "if: healthy == '"'"'true'"'"' without continue-on-error fails"

# ─── Test 4: Trigger (c) success() && has_failures == 'false' ─────────────
reset_tmpdir
write_workflow "quality.yml" 'name: Quality
on: [push]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - id: quality
        run: echo "has_failures=false" >> "$GITHUB_OUTPUT"

      - name: Cleanup quality alerts
        if: success() && steps.quality.outputs.has_failures == '"'"'false'"'"'
        env:
          GH_TOKEN: secret
        run: |
          gh issue close 1 --reason completed
'
assert_fails "if: success() && has_failures == '"'"'false'"'"' without continue-on-error fails"

# ─── Test 5: Conservative — failure() path step is NOT flagged ────────────
# This is the load-bearing alert-creation path; flagging it would mask
# a real GitHub-API failure when we genuinely need to open an issue.
reset_tmpdir
write_workflow "alert.yml" 'name: Alert
on: [push]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - id: quality
        run: echo "has_failures=true" >> "$GITHUB_OUTPUT"

      - name: Create failure issue
        if: failure() && steps.quality.outputs.has_failures == '"'"'true'"'"'
        env:
          GH_TOKEN: secret
        run: |
          gh issue create --title "Quality regression" --body "Failed."
'
assert_passes "failure() && has_failures == '"'"'true'"'"' is NOT flagged (load-bearing)"

# ─── Test 6: Conservative — always() audit step is NOT flagged ────────────
# Even if it carries gh issue calls — the AC trigger list does not
# include `always()`. This is the unblock-issues post-run audit shape.
reset_tmpdir
write_workflow "audit.yml" 'name: Audit
on: [push]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - run: echo "doing the load-bearing thing"

      - name: Post-run audit — observability only
        if: always()
        env:
          GH_TOKEN: secret
        run: |
          gh issue list --label foo
'
assert_passes "if: always() audit step (no Auto-close prefix) is NOT flagged"

# ─── Test 7: Conservative — no gh side-effect, no flag ────────────────────
# Auto-close-named step that does NOT call into gh issue close|comment|list
# or gh api should not be flagged — there's no transient-API risk to mask.
reset_tmpdir
write_workflow "alert.yml" 'name: Alert
on: [push]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - id: check
        run: true

      - name: Auto-close cleanup tasks
        if: steps.check.outputs.healthy == '"'"'true'"'"'
        run: |
          rm -f /tmp/scratch.txt
'
assert_passes "Auto-close step with no gh side-effect is NOT flagged"

# ─── Test 8: continue-on-error: false should fail ─────────────────────────
# Explicit `continue-on-error: false` is equivalent to absence — it must
# still trigger the failure.
reset_tmpdir
write_workflow "alert.yml" 'name: Alert
on: [push]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - id: check
        run: true

      - name: Auto-close issues
        if: steps.check.outputs.healthy == '"'"'true'"'"'
        continue-on-error: false
        env:
          GH_TOKEN: secret
        run: |
          gh issue close 1 --reason completed
'
assert_fails "Auto-close step with continue-on-error: false is flagged"

# ─── Test 9: gh api side-effect path is recognized ────────────────────────
reset_tmpdir
write_workflow "alert.yml" 'name: Alert
on: [push]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - id: check
        run: true

      - name: Auto-close via gh api
        if: steps.check.outputs.healthy == '"'"'true'"'"'
        env:
          GH_TOKEN: secret
        run: |
          gh api /repos/foo/bar/issues/1 -X PATCH -f state=closed
'
assert_fails "Auto-close step using gh api without continue-on-error is flagged"

# ─── Test 10: comments mentioning gh issue close are not side effects ─────
# A `# gh issue close` comment line must not count as a side-effect.
reset_tmpdir
write_workflow "alert.yml" 'name: Alert
on: [push]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - id: check
        run: true

      - name: Auto-close cleanup
        if: steps.check.outputs.healthy == '"'"'true'"'"'
        run: |
          # We could call gh issue close here, but we deliberately just log.
          echo "Nothing to close."
'
assert_passes "Comment mentioning gh issue close is not treated as a side effect"

# ─── Test 11: smoke-test-style folded `if: >-` block scalar with name ────
reset_tmpdir
write_workflow "smoke.yml" 'name: Smoke
on: [push]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - id: smoke
        run: echo "has_failures=false" >> "$GITHUB_OUTPUT"

      - name: Auto-close resolved smoke test issues
        if: >-
          steps.smoke.outputs.has_failures == '"'"'false'"'"' &&
          steps.smoke.outputs.has_failures == '"'"'false'"'"'
        env:
          GH_TOKEN: secret
        run: |
          gh issue close 1 --reason completed
'
assert_fails "smoke-test folded if: >- + Auto-close name without continue-on-error is flagged"

# ─── Test 12: scan repo with no .github/workflows is graceful ─────────────
reset_tmpdir
rm -rf "$TMPDIR_TEST/.github"
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
    echo "PASS: subdir scan with no .github/workflows exits 0"
else
    echo "FAIL: subdir scan with no .github/workflows should exit 0"
    FAILURES=$((FAILURES + 1))
fi

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "─────────────────────────────────────"
echo "Tests run:    $TESTS"
echo "Failures:     $FAILURES"
if [[ $FAILURES -eq 0 ]]; then
    echo "Result:       ALL TESTS PASSED"
    exit 0
else
    echo "Result:       FAILED"
    exit 1
fi
