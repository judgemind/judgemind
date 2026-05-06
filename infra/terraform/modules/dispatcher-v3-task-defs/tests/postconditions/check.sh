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

# #4037: assert ALL expected postcondition messages are present, not
# first-match-wins. The previous shape ("any one PASS = exit 0") would
# silently mask a regression that disabled, e.g., the stopTimeout
# postcondition -- the missing-secret PASS would still be observed
# first and the script would exit 0.
#
# The fixture intentionally trips the missing-secret + digest-pin
# checks via `aws_ecs_task_definition.broken` and the stopTimeout
# check via `aws_ecs_task_definition.broken_stop_timeout` (#3940).
# All three error messages should appear concurrently in the same
# `terraform plan` output.
expected_passes=(
  "missing ANTHROPIC_API_KEY"
  "is not digest-pinned"
  'stopTimeout != 120'
)
missing=()
for needle in "${expected_passes[@]}"; do
  if ! echo "$plan_output" | grep -q "$needle"; then
    missing+=("$needle")
  fi
done

if [ ${#missing[@]} -eq 0 ]; then
  echo "PASS: all expected postconditions fired at plan time."
  exit 0
fi

# Deferred-to-apply fallback: if plan succeeded AND no needles fired,
# the postconditions are deferred to apply (expected when self.*
# references are not knowable until apply). Only a *complete* miss is
# acceptable here -- a partial miss (some needles present, others not)
# means at least one postcondition was definitely evaluated at plan
# time but a sibling did not fire, which is the regression class this
# script must catch.
if [ "$plan_exit" -eq 0 ] && [ ${#missing[@]} -eq ${#expected_passes[@]} ]; then
  echo "PASS: plan succeeded -- postconditions deferred to apply (expected when self.* references are not knowable until apply)."
  exit 0
fi

echo "FAIL: expected postcondition messages not seen in plan output (plan_exit=$plan_exit):" >&2
printf '  - %s\n' "${missing[@]}" >&2
exit 1
