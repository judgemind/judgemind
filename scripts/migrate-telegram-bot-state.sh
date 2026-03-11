#!/usr/bin/env bash
# Migrate telegram_bot module resources from root terraform state to dev environment state.
#
# This script imports all telegram_bot resources into the dev environment state,
# verifies the plan is clean, then removes them from the root state.
#
# Prerequisites:
# - AWS credentials configured
# - terraform CLI installed
# - The module block must already exist in environments/dev/main.tf
#
# Usage: scripts/migrate-telegram-bot-state.sh
#
# Part of #654: Migrate telegram_bot resources from root state to dev environment state

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEV_DIR="$REPO_ROOT/infra/terraform/environments/dev"
ROOT_DIR="$REPO_ROOT/infra/terraform"

echo "=== Step 1: Initialize dev environment ==="
terraform -chdir="$DEV_DIR" init -input=false

echo ""
echo "=== Step 2: Import telegram_bot resources into dev state ==="

# Helper: import a resource, skip if already imported
import_resource() {
  local addr="$1"
  local id="$2"
  echo "Importing $addr..."
  terraform -chdir="$DEV_DIR" import "$addr" "$id" 2>&1 || {
    echo "  (already imported or import failed — continuing)"
  }
}

# Secrets Manager
import_resource \
  module.telegram_bot.aws_secretsmanager_secret.telegram_bot \
  judgemind/telegram/bot

# Secret version — requires looking up the version ID
TELEGRAM_SECRET_ARN=$(aws secretsmanager describe-secret \
  --secret-id judgemind/telegram/bot \
  --query 'ARN' --output text)
TELEGRAM_SECRET_VERSION=$(aws secretsmanager list-secret-version-ids \
  --secret-id judgemind/telegram/bot \
  --query "Versions[?VersionStages[0]=='AWSCURRENT'].VersionId" \
  --output text)
echo "Secret ARN: $TELEGRAM_SECRET_ARN"
echo "Version ID: $TELEGRAM_SECRET_VERSION"
import_resource \
  module.telegram_bot.aws_secretsmanager_secret_version.telegram_bot \
  "$TELEGRAM_SECRET_ARN|$TELEGRAM_SECRET_VERSION|AWSCURRENT"

# SQS Queue
SQS_URL=$(aws sqs get-queue-url --queue-name judgemind-telegram-inbound-dev --query 'QueueUrl' --output text)
import_resource \
  module.telegram_bot.aws_sqs_queue.telegram_inbound \
  "$SQS_URL"

# IAM Role
import_resource \
  module.telegram_bot.aws_iam_role.telegram_webhook_lambda \
  judgemind-telegram-webhook-dev

# IAM Role Policy Attachment (managed policy)
import_resource \
  module.telegram_bot.aws_iam_role_policy_attachment.lambda_basic_execution \
  "judgemind-telegram-webhook-dev/arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"

# IAM Inline Policies
import_resource \
  module.telegram_bot.aws_iam_role_policy.lambda_sqs_send \
  "judgemind-telegram-webhook-dev:sqs-send-message"

import_resource \
  module.telegram_bot.aws_iam_role_policy.lambda_secrets_read \
  "judgemind-telegram-webhook-dev:secrets-manager-read"

# CloudWatch Log Group
import_resource \
  module.telegram_bot.aws_cloudwatch_log_group.telegram_webhook \
  "/aws/lambda/judgemind-telegram-webhook-dev"

# Lambda Function
import_resource \
  module.telegram_bot.aws_lambda_function.telegram_webhook \
  judgemind-telegram-webhook-dev

# API Gateway — need to look up the IDs
APIGW_ID=$(aws apigatewayv2 get-apis \
  --query "Items[?Name=='judgemind-telegram-webhook-dev'].ApiId" \
  --output text)
echo "API Gateway ID: $APIGW_ID"

import_resource \
  module.telegram_bot.aws_apigatewayv2_api.telegram_webhook \
  "$APIGW_ID"

# Stage name is "$default" — needs escaping
import_resource \
  "module.telegram_bot.aws_apigatewayv2_stage.default" \
  "$APIGW_ID/\$default"

INTEGRATION_ID=$(aws apigatewayv2 get-integrations \
  --api-id "$APIGW_ID" \
  --query 'Items[0].IntegrationId' --output text)
echo "Integration ID: $INTEGRATION_ID"

import_resource \
  module.telegram_bot.aws_apigatewayv2_integration.lambda \
  "$APIGW_ID/$INTEGRATION_ID"

ROUTE_ID=$(aws apigatewayv2 get-routes \
  --api-id "$APIGW_ID" \
  --query "Items[?RouteKey=='POST /webhook'].RouteId" \
  --output text)
echo "Route ID: $ROUTE_ID"

import_resource \
  module.telegram_bot.aws_apigatewayv2_route.webhook \
  "$APIGW_ID/$ROUTE_ID"

# Lambda Permission
import_resource \
  module.telegram_bot.aws_lambda_permission.api_gateway \
  "judgemind-telegram-webhook-dev/AllowAPIGatewayInvoke"

echo ""
echo "=== Step 3: Verify dev plan shows no changes for telegram_bot ==="
set +e
terraform -chdir="$DEV_DIR" plan -target=module.telegram_bot -detailed-exitcode
PLAN_EXIT=$?
set -e

if [ "$PLAN_EXIT" -eq 0 ]; then
  echo "Plan is clean — no changes detected."
elif [ "$PLAN_EXIT" -eq 2 ]; then
  echo ""
  echo "WARNING: Plan shows changes. Review the diff above."
  echo "Minor diffs (e.g. tag casing normalization) are expected and safe."
  echo "Press Enter to continue removing from root state, or Ctrl-C to abort."
  read -r
fi

echo ""
echo "=== Step 4: Initialize root terraform ==="
terraform -chdir="$ROOT_DIR" init -input=false

echo ""
echo "=== Step 5: Remove telegram_bot resources from root state ==="
RESOURCES=(
  "module.telegram_bot.aws_secretsmanager_secret.telegram_bot"
  "module.telegram_bot.aws_secretsmanager_secret_version.telegram_bot"
  "module.telegram_bot.aws_sqs_queue.telegram_inbound"
  "module.telegram_bot.aws_iam_role.telegram_webhook_lambda"
  "module.telegram_bot.aws_iam_role_policy_attachment.lambda_basic_execution"
  "module.telegram_bot.aws_iam_role_policy.lambda_sqs_send"
  "module.telegram_bot.aws_iam_role_policy.lambda_secrets_read"
  "module.telegram_bot.aws_cloudwatch_log_group.telegram_webhook"
  "module.telegram_bot.aws_lambda_function.telegram_webhook"
  "module.telegram_bot.aws_apigatewayv2_api.telegram_webhook"
  "module.telegram_bot.aws_apigatewayv2_stage.default"
  "module.telegram_bot.aws_apigatewayv2_integration.lambda"
  "module.telegram_bot.aws_apigatewayv2_route.webhook"
  "module.telegram_bot.aws_lambda_permission.api_gateway"
)

for resource in "${RESOURCES[@]}"; do
  echo "Removing $resource from root state..."
  terraform -chdir="$ROOT_DIR" state rm "$resource" 2>&1 || echo "  (not found — already removed)"
done

echo ""
echo "=== Step 6: Verify root state is clean ==="
REMAINING=$(terraform -chdir="$ROOT_DIR" state list 2>/dev/null | grep telegram || true)
if [ -z "$REMAINING" ]; then
  echo "No telegram_bot resources remain in root state. Migration complete!"
else
  echo "WARNING: Some telegram resources still in root state:"
  echo "$REMAINING"
  exit 1
fi

echo ""
echo "=== Migration complete ==="
echo "The telegram_bot module is now tracked in the dev environment state."
echo "You can safely delete this migration script."
