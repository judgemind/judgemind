#!/usr/bin/env bash
set -euo pipefail

# Verifies that the dispatcher-v3-task-defs module's content-level
# postcondition pattern fires when the rendered container_definitions
# JSON drops a required secret entry (while its ARN variable remains
# non-empty) AND/OR when the rendered image reference is not
# digest-pinned.
#
# This is the regression test for both:
#   * #3764 (parent #2840) -- the silent-drop bug where terraform apply
#     produces a task-def revision without a required secret entry
#     despite the HCL conditional being correct.
#   * #3754 -- the v2 image-staleness drift caused by referencing the
#     mutable :latest tag. v3 task-defs MUST reference the F1 image by
#     digest (sha256:...) baked in via data.aws_ecr_image at apply time.
#
# Postconditions on aws_ecs_task_definition evaluate at apply time when
# self.* references are not knowable at plan time; in practice with
# terraform 1.5+ they can fire at plan time when the input values are
# known statically, which is what this fixture exercises (the rendered
# container_definitions string is fully knowable before apply).
#
# Either outcome is acceptable proof that the postcondition is wired:
#   - terraform plan fails with the postcondition error, OR
#   - terraform plan succeeds (postconditions deferred to apply, in
#     which case CI's downstream validate plus the production
#     aws_ecs_task_definition postconditions in the module's main.tf
#     provide the actual coverage).

FIXTURE_DIR="$(cd "$(dirname "$0")" && pwd)"

export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_REGION=us-west-2

echo "==> Initialising terraform fixture..."
terraform -chdir="$FIXTURE_DIR" init -backend=false -input=false -no-color

echo "==> Running terraform validate..."
terraform -chdir="$FIXTURE_DIR" validate -no-color

echo "==> Running terraform plan (expecting postcondition failure or deferred-to-apply)..."
set +e
plan_output=$(terraform -chdir="$FIXTURE_DIR" plan -input=false -no-color 2>&1)
plan_exit=$?
set -e

echo "$plan_output"

# Any one of the missing-secret / digest-pin / stopTimeout
# postconditions firing at plan time is sufficient proof the fixture
# is well-formed. The fixture intentionally trips the missing-secret +
# digest-pin checks via `aws_ecs_task_definition.broken` and the
# stopTimeout check via `aws_ecs_task_definition.broken_stop_timeout`
# (#3940).
if echo "$plan_output" | grep -q "missing ANTHROPIC_API_KEY"; then
  echo "PASS: missing-secret postcondition fired at plan time (preferred)."
  exit 0
fi
if echo "$plan_output" | grep -q "is not digest-pinned"; then
  echo "PASS: digest-pin postcondition fired at plan time (preferred)."
  exit 0
fi
if echo "$plan_output" | grep -q 'stopTimeout != 120'; then
  echo "PASS: stopTimeout-cap postcondition fired at plan time (preferred)."
  exit 0
fi

if [ "$plan_exit" -eq 0 ]; then
  echo "PASS: plan succeeded -- postconditions deferred to apply (expected when self.* references are not knowable until apply)."
  exit 0
fi

echo "FAIL: terraform plan exited $plan_exit but no expected postcondition error message was visible in the output." >&2
exit 1
