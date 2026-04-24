#!/usr/bin/env bash
set -euo pipefail

# Verifies that the dispatcher-daemon module's preconditions fire correctly
# when desired_count > 0 but required secret ARNs are empty.
# Expected behaviour: `terraform plan` exits non-zero with the precondition
# error text in its output.

FIXTURE_DIR="$(cd "$(dirname "$0")" && pwd)"

export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_REGION=us-west-2

echo "==> Initialising terraform fixture..."
terraform -chdir="$FIXTURE_DIR" init -backend=false -input=false -no-color

echo "==> Running terraform plan (expecting precondition failure)..."
set +e
plan_output=$(terraform -chdir="$FIXTURE_DIR" plan -input=false -no-color 2>&1)
plan_exit=$?
set -e

echo "$plan_output"

if [ "$plan_exit" -eq 0 ]; then
  echo "FAIL: terraform plan exited 0 — precondition did not fire." >&2
  exit 1
fi

if echo "$plan_output" | grep -q "anthropic_api_key_secret_arn must be set"; then
  echo "PASS: anthropic_api_key_secret_arn precondition fired as expected."
else
  echo "FAIL: expected 'anthropic_api_key_secret_arn must be set' in plan output." >&2
  exit 1
fi

if echo "$plan_output" | grep -q "db_connection_secret_arn must be set"; then
  echo "PASS: db_connection_secret_arn precondition fired as expected."
else
  echo "FAIL: expected 'db_connection_secret_arn must be set' in plan output." >&2
  exit 1
fi

echo "==> All precondition checks passed."
