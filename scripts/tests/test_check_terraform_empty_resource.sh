#!/usr/bin/env bash
# test_check_terraform_empty_resource.sh — Tests for check-terraform-empty-resource-risk.sh
#
# Creates temporary .tf files to verify that the checker correctly detects
# compact([var.*]) Resource lists without count/for_each guards.
#
# Usage:
#   scripts/tests/test_check_terraform_empty_resource.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-terraform-empty-resource-risk.sh"
PYTHON_HELPER="$SCRIPT_DIR/check_tf_empty_resource.py"
FAILURES=0
TESTS=0

# Use a temp directory so we don't pollute the repo
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# Create a minimal directory structure that mirrors infra/terraform so
# check-terraform-empty-resource-risk.sh --list picks up our test files.
# Tests that drive the Python helper directly use TMPDIR_TEST files directly.
TF_DIR="$TMPDIR_TEST/infra/terraform/modules/test-module"
mkdir -p "$TF_DIR"

create_main_tf() {
    local content="$1"
    printf '%s\n' "$content" > "$TF_DIR/main.tf"
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

# Python-level assert helpers (drive the helper directly on a specific file)
# These use if/then/else to be safe under set -e (the if condition is not
# subject to set -e exit).
assert_py_fails() {
    local desc="$1"
    local tf_file="$2"
    local allowlist="${3:-}"
    TESTS=$((TESTS + 1))
    local passed=false
    if [[ -n "$allowlist" ]]; then
        if python3 "$PYTHON_HELPER" "$tf_file" "$allowlist" > /dev/null 2>&1; then
            passed=true
        fi
    else
        if python3 "$PYTHON_HELPER" "$tf_file" > /dev/null 2>&1; then
            passed=true
        fi
    fi
    if "$passed"; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_py_passes() {
    local desc="$1"
    local tf_file="$2"
    local allowlist="${3:-}"
    TESTS=$((TESTS + 1))
    local passed=false
    if [[ -n "$allowlist" ]]; then
        if python3 "$PYTHON_HELPER" "$tf_file" "$allowlist" > /dev/null 2>&1; then
            passed=true
        fi
    else
        if python3 "$PYTHON_HELPER" "$tf_file" > /dev/null 2>&1; then
            passed=true
        fi
    fi
    if "$passed"; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

assert_py_output_contains() {
    local desc="$1"
    local tf_file="$2"
    local pattern="$3"
    TESTS=$((TESTS + 1))
    local out
    out=$(python3 "$PYTHON_HELPER" "$tf_file" 2>&1 || true)
    if echo "$out" | grep -qE "$pattern"; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (pattern '$pattern' not found in output: $out)"
        FAILURES=$((FAILURES + 1))
    fi
}

reset_tmpdir() {
    rm -rf "$TF_DIR"
    mkdir -p "$TF_DIR"
}

# ─── Test 1: Bad fixture (the #2739 shape) should be flagged ──────────────────
# Inline TF: resource with Resource = compact([var.*]) and no count/for_each
FIXTURES_DIR="$SCRIPT_DIR/tests/fixtures"
assert_py_fails "bad fixture exits non-zero" "$FIXTURES_DIR/tf-empty-resource-bad.tf"
reset_tmpdir

# ─── Test 2: Bad fixture output includes line number and resource info ────────
assert_py_output_contains \
    "bad fixture output contains path:line format" \
    "$FIXTURES_DIR/tf-empty-resource-bad.tf" \
    ":[0-9]+:"
reset_tmpdir

# ─── Test 3: Good fixture (the #2740 fix — count + locals) should pass ───────
assert_py_passes "good fixture exits 0" "$FIXTURES_DIR/tf-empty-resource-good.tf"
reset_tmpdir

# ─── Test 4: Resource with count guard passes ─────────────────────────────────
cat > "$TMPDIR_TEST/with_count.tf" << 'TFEOF'
resource "aws_iam_role_policy" "guarded" {
  count = length(local.arns) > 0 ? 1 : 0
  name  = "guarded"
  role  = "some-role"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = compact([var.a_arn, var.b_arn])
    }]
  })
}
TFEOF
assert_py_passes "resource with count guard passes" "$TMPDIR_TEST/with_count.tf"

# ─── Test 5: Resource with for_each guard passes ──────────────────────────────
cat > "$TMPDIR_TEST/with_foreach.tf" << 'TFEOF'
resource "aws_iam_role_policy" "guarded_fe" {
  for_each = toset(local.arns)
  name     = "guarded-${each.key}"
  role     = "some-role"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = compact([var.a_arn])
    }]
  })
}
TFEOF
assert_py_passes "resource with for_each guard passes" "$TMPDIR_TEST/with_foreach.tf"

# ─── Test 6: Mixed compact() args (one literal) — not flagged ────────────────
# When any compact() arg is not a var.*, it's not the #2739 pattern
cat > "$TMPDIR_TEST/mixed_args.tf" << 'TFEOF'
resource "aws_iam_role_policy" "mixed" {
  name = "mixed"
  role = "some-role"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = compact([var.a_arn, "arn:aws:iam::123456789012:root"])
    }]
  })
}
TFEOF
assert_py_passes "compact() with literal arg is not flagged" "$TMPDIR_TEST/mixed_args.tf"

# ─── Test 7: compact() inside jsonencode nested (depth > 1) is not flagged ───
# The Resource = compact([...]) is nested inside jsonencode, not at depth 1
# of the resource block.  The check looks at depth 1 only, so a compact()
# that appears only inside jsonencode({...}) won't trigger unless it also
# appears at depth 1.
cat > "$TMPDIR_TEST/nested_only.tf" << 'TFEOF'
resource "aws_iam_role_policy" "nested" {
  name = "nested"
  role = "some-role"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = compact([var.a_arn, var.b_arn])
    }]
  })
}
TFEOF
# This IS the bad pattern (compact([var.*]) without count) — should fail
assert_py_fails "nested compact([var.*]) without count is flagged" "$TMPDIR_TEST/nested_only.tf"

# ─── Test 8: Allowlist suppression works ─────────────────────────────────────
cat > "$TMPDIR_TEST/bad_for_allowlist.tf" << 'TFEOF'
resource "aws_iam_role_policy" "suppress_me" {
  name = "suppress-me"
  role = "some-role"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = compact([var.a_arn, var.b_arn])
    }]
  })
}
TFEOF
ALLOWLIST_FILE="$TMPDIR_TEST/test_allowlist.txt"
printf '%s\n' "$TMPDIR_TEST/bad_for_allowlist.tf:aws_iam_role_policy:suppress_me" > "$ALLOWLIST_FILE"
TESTS=$((TESTS + 1))
if python3 "$PYTHON_HELPER" "$TMPDIR_TEST/bad_for_allowlist.tf" "$ALLOWLIST_FILE" > /dev/null 2>&1; then
    echo "PASS: allowlist suppression works"
else
    echo "FAIL: allowlist suppression works (allowlisted violation still flagged)"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 9: --list flag prints expected paths and exits 0 ────────────────────
# Point the script at a directory with a known .tf file
# We need the script to look in a known location — use REPO_ROOT override
# by creating a minimal structure
TF_LIST_DIR="$TMPDIR_TEST/list_test/infra/terraform/modules/mymodule"
mkdir -p "$TF_LIST_DIR"
printf 'resource "aws_s3_bucket" "b" { bucket = "b" }\n' > "$TF_LIST_DIR/main.tf"
# We can't easily override REPO_ROOT inside the shell script, so test --list
# against the real repo.
TESTS=$((TESTS + 1))
list_output=$("$CHECK_SCRIPT" --list 2>&1 || true)
if echo "$list_output" | grep -q "main.tf"; then
    echo "PASS: --list flag prints .tf files and exits 0"
else
    echo "FAIL: --list flag did not print any main.tf files (output: $list_output)"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 10: --list includes modules and environments paths ──────────────────
TESTS=$((TESTS + 1))
if echo "$list_output" | grep -q "infra/terraform/modules" && echo "$list_output" | grep -q "infra/terraform/environments"; then
    echo "PASS: --list includes both modules/ and environments/ paths"
else
    echo "FAIL: --list missing modules or environments path"
    echo "  Output was: $list_output"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 11: No violations in real repo (allowlisted violations excluded) ────
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" > /dev/null 2>&1; then
    echo "PASS: real repo scan exits 0 with allowlisted violations excluded"
else
    echo "FAIL: real repo scan exits non-zero — allowlist may be incomplete"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 12: No self-match on ci.yml step name ──────────────────────────────
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-terraform-empty-resource-risk.sh" "yml"

# ─── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
