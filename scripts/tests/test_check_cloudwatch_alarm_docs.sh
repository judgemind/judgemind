#!/usr/bin/env bash
# test_check_cloudwatch_alarm_docs.sh — Tests for
# check-cloudwatch-alarm-docs.sh.
#
# Builds fixture trees containing aws_cloudwatch_metric_alarm resources
# and a docs file, then verifies the check correctly detects alarms
# missing from the docs while passing on a fully-documented tree.
#
# Usage:
#   scripts/tests/test_check_cloudwatch_alarm_docs.sh
#
# Exit codes:
#   0 — all tests passed.
#   1 — one or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-cloudwatch-alarm-docs.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

reset_tree() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

write_file() {
    local rel="$1"
    local content="$2"
    local path="$TMPDIR_TEST/$rel"
    mkdir -p "$(dirname "$path")"
    printf '%s\n' "$content" > "$path"
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected exit 0, got non-zero)"
        FAILURES=$((FAILURES + 1))
    fi
}

assert_fails_with_match() {
    local desc="$1"
    local expected_substr="$2"
    TESTS=$((TESTS + 1))
    local out
    if out="$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1)"; then
        echo "FAIL: $desc (expected exit 1, got 0)"
        echo "       output: $out"
        FAILURES=$((FAILURES + 1))
        return
    fi
    if [[ "$out" == *"$expected_substr"* ]]; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (output did not contain '$expected_substr')"
        echo "       output: $out"
        FAILURES=$((FAILURES + 1))
    fi
}

# ─── Test 1: Empty tree (no infra/terraform/modules/) passes ───────────────
reset_tree
assert_passes "Empty tree without infra/terraform/modules/ passes"

# ─── Test 2: Documented alarm passes ───────────────────────────────────────
reset_tree
write_file "infra/terraform/modules/foo/main.tf" '
resource "aws_cloudwatch_metric_alarm" "foo_widget" {
  alarm_name        = "judgemind-foo-widget-${var.environment}"
  alarm_description = "Foo widget alarm"
}
'
write_file "docs/agent/infrastructure-reference.md" '
### CloudWatch Alarms (dev)

| Alarm name prefix | Module | Source | Fires when |
|---|---|---|---|
| `judgemind-foo-widget-` | `foo` | metric | something happened |
'
assert_passes 'Documented alarm with <prefix>-${env} form passes'

# ─── Test 3: Undocumented alarm fails with the alarm name in the output ────
reset_tree
write_file "infra/terraform/modules/foo/main.tf" '
resource "aws_cloudwatch_metric_alarm" "foo_widget" {
  alarm_name        = "judgemind-foo-undocumented-${var.environment}"
  alarm_description = "Foo widget alarm"
}
'
write_file "docs/agent/infrastructure-reference.md" '
### CloudWatch Alarms (dev)

| Alarm name prefix | Module | Source | Fires when |
|---|---|---|---|
| `judgemind-foo-widget-` | `foo` | metric | something happened |
'
assert_fails_with_match \
    "Undocumented alarm reports the alarm name" \
    "judgemind-foo-undocumented"

# ─── Test 4: Stub alarm added to existing fixture is reported ──────────────
# The AC's specific verification: temporarily add a stub alarm to a fixture
# and confirm the check fails with the alarm name listed.
reset_tree
write_file "infra/terraform/modules/foo/main.tf" '
resource "aws_cloudwatch_metric_alarm" "foo_widget" {
  alarm_name        = "judgemind-foo-widget-${var.environment}"
}

resource "aws_cloudwatch_metric_alarm" "stub_new_alarm" {
  alarm_name        = "judgemind-stub-new-alarm-${var.environment}"
}
'
write_file "docs/agent/infrastructure-reference.md" '
| `judgemind-foo-widget-` | `foo` | metric | fires |
'
assert_fails_with_match \
    "Stub alarm in fixture without docs row is reported" \
    "judgemind-stub-new-alarm"

# ─── Test 5: ${local.service_name} interpolation is rendered ───────────────
# Mirrors the dispatcher-daemon convention where alarm_name builds on
# ${local.service_name} = "judgemind-dispatcher-${var.environment}".
reset_tree
write_file "infra/terraform/modules/dispatcher-daemon/main.tf" '
locals {
  service_name = "judgemind-dispatcher-${var.environment}"
}

resource "aws_cloudwatch_metric_alarm" "heartbeat_stale" {
  alarm_name = "${local.service_name}-heartbeat-stale"
}
'
write_file "docs/agent/infrastructure-reference.md" '
| `judgemind-dispatcher-heartbeat-stale-` | `dispatcher-daemon` | metric | fires |
'
assert_passes "\${local.service_name} interpolation renders to documented prefix"

# ─── Test 6: ${local.task_family} interpolation is rendered ────────────────
reset_tree
write_file "infra/terraform/modules/dispatcher-agent-runner/main.tf" '
locals {
  task_family = "judgemind-dispatcher-agent-runner-${var.environment}"
}

resource "aws_cloudwatch_metric_alarm" "agent_runner_errors" {
  alarm_name = "${local.task_family}-errors"
}
'
write_file "docs/agent/infrastructure-reference.md" '
| `judgemind-dispatcher-agent-runner-${env}-errors` (search key: `judgemind-dispatcher-agent-runner-errors`) | `dispatcher-agent-runner` | metric | fires |
'
assert_passes '${local.task_family} interpolation (env in middle) is rendered with collapsed dashes'

# ─── Test 7: ${var.name_prefix} default is rendered ────────────────────────
reset_tree
write_file "infra/terraform/modules/dispatcher-v3-scheduled-skills/variables.tf" '
variable "name_prefix" {
  type    = string
  default = "dispatcher-v3"
}
'
write_file "infra/terraform/modules/dispatcher-v3-scheduled-skills/main.tf" '
resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  alarm_name = "${var.name_prefix}-scheduled-skills-dlq-depth-${var.environment}"
}
'
write_file "docs/agent/infrastructure-reference.md" '
| `dispatcher-v3-scheduled-skills-dlq-depth-` | `dispatcher-v3-scheduled-skills` | metric | fires |
'
assert_passes "\${var.name_prefix} default expands to documented prefix"

# ─── Test 8: ${each.key} is stripped (per-key alarm) ───────────────────────
reset_tree
write_file "infra/terraform/modules/dispatcher-v3-scheduled-skills/variables.tf" '
variable "name_prefix" {
  type    = string
  default = "dispatcher-v3"
}
'
write_file "infra/terraform/modules/dispatcher-v3-scheduled-skills/main.tf" '
resource "aws_cloudwatch_metric_alarm" "eventbridge_failures" {
  alarm_name = "${var.name_prefix}-eventbridge-failures-${each.key}-${var.environment}"
}
'
write_file "docs/agent/infrastructure-reference.md" '
| `dispatcher-v3-eventbridge-failures-` (per-skill) | `dispatcher-v3-scheduled-skills` | metric | fires |
'
assert_passes "\${each.key} is stripped, leaving documentable prefix"

# ─── Test 9: log_metric_filter resources are NOT enforced ──────────────────
# Per scope decision documented in the script: log metric filters are
# documented transitively via their consumer alarms, not separately.
reset_tree
write_file "infra/terraform/modules/foo/main.tf" '
resource "aws_cloudwatch_log_metric_filter" "foo_filter" {
  name = "judgemind-foo-undocumented-filter-${var.environment}"
}

resource "aws_cloudwatch_metric_alarm" "foo_widget" {
  alarm_name = "judgemind-foo-widget-${var.environment}"
}
'
write_file "docs/agent/infrastructure-reference.md" '
| `judgemind-foo-widget-` | `foo` | metric | fires |
'
assert_passes "log_metric_filter without its own docs row does not fail the check"

# ─── Test 10: Multiple alarms in one file, all documented ──────────────────
reset_tree
write_file "infra/terraform/modules/api-service/main.tf" '
resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name = "judgemind-api-5xx-${var.environment}"
}

resource "aws_cloudwatch_metric_alarm" "api_4xx" {
  alarm_name = "judgemind-api-4xx-spike-${var.environment}"
}

resource "aws_cloudwatch_metric_alarm" "api_latency_p99" {
  alarm_name = "judgemind-api-latency-p99-${var.environment}"
}
'
write_file "docs/agent/infrastructure-reference.md" '
| `judgemind-api-5xx-` | api | m | f |
| `judgemind-api-4xx-spike-` | api | m | f |
| `judgemind-api-latency-p99-` | api | m | f |
'
assert_passes "Multiple alarms, all documented, passes"

# ─── Test 11: Multiple alarms, one undocumented — only that one reported ──
reset_tree
write_file "infra/terraform/modules/api-service/main.tf" '
resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name = "judgemind-api-5xx-${var.environment}"
}

resource "aws_cloudwatch_metric_alarm" "api_new" {
  alarm_name = "judgemind-api-new-thing-${var.environment}"
}
'
write_file "docs/agent/infrastructure-reference.md" '
| `judgemind-api-5xx-` | api | m | f |
'
assert_fails_with_match \
    "Multiple alarms, one undocumented, the missing one is named" \
    "judgemind-api-new-thing"

# ─── Test 12: Brace-balanced nested blocks do not break parsing ────────────
# alarm_name must still be extracted even when the resource has nested
# `dimensions { ... }` or `metric_query { ... }` blocks.
reset_tree
write_file "infra/terraform/modules/foo/main.tf" '
resource "aws_cloudwatch_metric_alarm" "with_nested" {
  alarm_name = "judgemind-foo-nested-${var.environment}"

  metric_query {
    id = "m1"
    metric {
      metric_name = "Foo"
      namespace   = "Bar"
    }
  }

  dimensions = {
    Service = "foo"
  }
}
'
write_file "docs/agent/infrastructure-reference.md" '
| `judgemind-foo-nested-` | foo | m | f |
'
assert_passes "Resource with nested blocks still has its alarm_name parsed"

# ─── Summary ───────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
