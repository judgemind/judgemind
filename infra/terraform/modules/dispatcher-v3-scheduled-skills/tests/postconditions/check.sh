#!/usr/bin/env bash
set -euo pipefail

# Verifies that the dispatcher-v3-scheduled-skills module's two `check`
# block assertions fire when the input config violates them:
#
#   1. `task_def_arn_matches_family` -- the scheduled_skill_task_definition_arn
#      must contain the family name. A mismatch would AccessDenied at every
#      cron firing because the EventBridge invoker role's RunTask grant is
#      scoped to the family.
#   2. `alert_topic_required_when_enabled` -- enable_alerts=true must be
#      paired with a non-empty alert_sns_topic_arn, otherwise the alarm
#      has no destination and adversarial-review MAJOR 7 stays open.
#
# Terraform `check` blocks emit warning-level diagnostics on assertion
# failure (NOT errors) -- they don't fail plan/apply. We grep for the
# error_message strings in the plan output to confirm both checks
# tripped.

FIXTURE_DIR="$(cd "$(dirname "$0")" && pwd)"

export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_REGION=us-west-2

echo "==> Initialising terraform fixture..."
terraform -chdir="$FIXTURE_DIR" init -backend=false -input=false -no-color

echo "==> Running terraform validate..."
terraform -chdir="$FIXTURE_DIR" validate -no-color

echo "==> Running terraform plan (expecting two check-block warnings)..."
set +e
plan_output=$(terraform -chdir="$FIXTURE_DIR" plan -input=false -no-color 2>&1)
plan_exit=$?
set -e

echo "$plan_output"

# Both check blocks should emit their error_message strings.
saw_family_check=0
saw_topic_check=0

if echo "$plan_output" | grep -q "does not contain the family name"; then
  saw_family_check=1
  echo "PASS: task_def_arn_matches_family check block fired."
fi

if echo "$plan_output" | grep -q "FailedInvocations alarm would have no destination"; then
  saw_topic_check=1
  echo "PASS: alert_topic_required_when_enabled check block fired."
fi

if [ "$saw_family_check" -eq 1 ] && [ "$saw_topic_check" -eq 1 ]; then
  echo "PASS: both check blocks fired as expected."
  exit 0
fi

echo "FAIL: at least one check block did not fire (family=$saw_family_check, topic=$saw_topic_check). plan_exit=$plan_exit" >&2
exit 1
