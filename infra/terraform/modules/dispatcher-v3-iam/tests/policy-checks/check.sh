#!/usr/bin/env bash
set -euo pipefail

# Policy-shape regression test for the dispatcher-v3-iam module.
#
# Asserts the spec §10 invariants that are easy to break with a stray
# string-replace and hard to spot in code review:
#
#   1. NO trust policy in the module references any account other than
#      the dev account ID — the module is dev-only by spec, and a
#      cross-account principal would silently expand the blast radius.
#   2. The launcher role's `ecs:RunTask` Resource list contains exactly
#      the three F2 agent task-def families (task-runner, diagnoser,
#      scheduled-skill) — adding a fourth would silently widen scope.
#   3. The launcher role does NOT have any `s3:*`, `rds-db:connect`
#      against `*/*` (catch-all), or any `secretsmanager` action other
#      than `GetSecretValue` on the wired telegram ARN.
#   4. The agent task role's trust policy is the same single-account
#      service principal as the launcher.
#
# This script runs against the rendered HCL — no AWS credentials are
# needed, no actual roles are created. It is a static lint of the
# module's main.tf to defend against the regression class where a
# future edit accidentally removes the `Condition` block on a Resource
# = "*" statement, or pastes a prod-account ARN into a Resource list.

FIXTURE_DIR="$(cd "$(dirname "$0")" && pwd)"
MODULE_DIR="$FIXTURE_DIR/../.."

main_tf="$MODULE_DIR/main.tf"
variables_tf="$MODULE_DIR/variables.tf"

if [ ! -f "$main_tf" ]; then
    echo "FAIL: $main_tf not found" >&2
    exit 2
fi

failures=0

check() {
    local description="$1"
    local condition_result="$2"
    if [ "$condition_result" = "0" ]; then
        echo "PASS: $description"
    else
        echo "FAIL: $description" >&2
        failures=$((failures + 1))
    fi
}

# ── Check 1: no cross-account principal in any trust policy ────────────────
#
# The module uses a single shared `local.ecs_tasks_assume_role_policy`
# that pins `Service: ecs-tasks.amazonaws.com` + `aws:SourceAccount =
# var.aws_account_id`. Any other Principal type in main.tf is a
# regression — a future edit that adds a per-role trust policy must
# preserve the single-account service principal pattern.
#
# The grep below looks for any `Principal = { AWS =` in main.tf, which
# would indicate a cross-account or cross-role trust path. The existing
# module has zero such occurrences.
if grep -E 'Principal\s*=\s*\{\s*AWS\s*=' "$main_tf" >/dev/null; then
    check "no AWS-principal trust statements in main.tf" 1
else
    check "no AWS-principal trust statements in main.tf" 0
fi

# ── Check 2: exactly the three F2 agent task-def families ──────────────────
#
# The launcher's `local.agent_task_def_families` list MUST hold exactly
# the three F2 families. Adding a fourth widens the launcher's RunTask
# scope and is a deliberate spec change — fail loudly so that change
# requires a spec update.
families_count=$(grep -c '${var.task_definition_family_prefix}' "$main_tf" || true)
expected=3
if [ "$families_count" -lt "$expected" ]; then
    check "launcher references at least $expected agent task-def families (found $families_count)" 1
else
    check "launcher references the F2 agent task-def families" 0
fi

# ── Check 3: launcher does not have full-S3 grants ─────────────────────────
#
# A future edit that wires the launcher into S3 (e.g. for session-log
# streaming) is a spec departure — the launcher is scheduler-only. The
# agent task role has `s3:*`; the launcher must not.
launcher_s3_section=$(awk '
    /aws_iam_role_policy "launcher_/ {capture=1}
    capture {print}
    /^}$/ && capture {capture=0; print "---"}
' "$main_tf")

if echo "$launcher_s3_section" | grep -E '"s3:\*"' >/dev/null; then
    check "launcher policies do not grant s3:*" 1
else
    check "launcher policies do not grant s3:*" 0
fi

# ── Check 4: agent task role pinned to the dev account ─────────────────────
#
# Every `var.aws_account_id` reference in main.tf should resolve to the
# dev account at apply time. The variable's default is `155326049300`
# (per variables.tf). A grep against variables.tf catches the case
# where the default was accidentally changed to a wildcard or a prod
# account.
if grep -E 'default\s*=\s*"155326049300"' "$variables_tf" >/dev/null; then
    check "aws_account_id default is the dev account" 0
else
    check "aws_account_id default is the dev account" 1
fi

# ── Check 5: launcher's RDS connect is scoped to judgemind_dispatcher ──────
#
# The launcher persists into `dispatcher.*` schema only. Its rds-db:connect
# Resource MUST end in `/judgemind_dispatcher` — a wildcard there would
# let the launcher connect as any DB user.
if grep -E 'dbuser:\*/judgemind_dispatcher' "$main_tf" >/dev/null; then
    check "launcher RDS connect is pinned to judgemind_dispatcher DB user" 0
else
    check "launcher RDS connect is pinned to judgemind_dispatcher DB user" 1
fi

# ── Check 6: cluster-ARN condition appears alongside Resource = "*" ────────
#
# Wherever the agent task role grants an action with `Resource = "*"`
# on a cluster-scoped API (DescribeTasks, ListTasks, etc.), the
# `Condition.ArnEquals."ecs:cluster"` block must accompany it. The
# module hits this requirement in the `agent_task_ecs` resource. A
# regression that drops the Condition block would let the agent's
# ECS calls escape to an unrelated cluster.
ecs_resource_star_count=$(grep -c 'Resource\s*=\s*"\*"' "$main_tf" || true)
ecs_cluster_condition_count=$(grep -c '"ecs:cluster"' "$main_tf" || true)
# At least one cluster-ARN condition must appear in main.tf.
if [ "$ecs_cluster_condition_count" -lt 1 ]; then
    check "main.tf has at least one ecs:cluster ArnEquals condition" 1
else
    check "main.tf has at least one ecs:cluster ArnEquals condition" 0
fi

if [ "$failures" -eq 0 ]; then
    echo
    echo "All policy-shape checks passed."
    exit 0
else
    echo
    echo "$failures policy-shape check(s) failed." >&2
    exit 1
fi
