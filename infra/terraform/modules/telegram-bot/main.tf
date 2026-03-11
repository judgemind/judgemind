# Telegram bot infrastructure: webhook Lambda, API Gateway, SQS inbound queue,
# and Secrets Manager secret for the bot token + user allowlist.
#
# The Lambda receives Telegram webhook POSTs, validates the sender against an
# allowlist stored in Secrets Manager, and enqueues valid messages to SQS for
# async processing.

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# ─── Secrets Manager ──────────────────────────────────────────────────────────
# Placeholder secret — actual values are populated manually.

resource "aws_secretsmanager_secret" "telegram_bot" {
  name        = "judgemind/telegram/bot"
  description = "Telegram bot token and allowed user IDs"

}

# NOTE: No aws_secretsmanager_secret_version here — the secret value is
# populated manually (bot token + allowed user IDs). Terraform manages only
# the secret container, never the value. This prevents Terraform from
# overwriting the real token on apply or during state migrations.
# See #712 for context.

# ─── SQS Queue ────────────────────────────────────────────────────────────────
# Standard queue for inbound Telegram messages. Ordering doesn't matter for
# chat commands, so FIFO is unnecessary (and more expensive).

resource "aws_sqs_queue" "telegram_inbound" {
  name                       = "judgemind-telegram-inbound-${var.environment}"
  message_retention_seconds  = var.sqs_message_retention_seconds
  visibility_timeout_seconds = var.sqs_visibility_timeout_seconds

}

# ─── IAM Role for Lambda ─────────────────────────────────────────────────────

resource "aws_iam_role" "telegram_webhook_lambda" {
  name = "judgemind-telegram-webhook-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

}

# Allow Lambda to write CloudWatch logs.
resource "aws_iam_role_policy_attachment" "lambda_basic_execution" {
  role       = aws_iam_role.telegram_webhook_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Allow Lambda to send messages to the inbound SQS queue.
resource "aws_iam_role_policy" "lambda_sqs_send" {
  name = "sqs-send-message"
  role = aws_iam_role.telegram_webhook_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.telegram_inbound.arn
      }
    ]
  })
}

# Allow Lambda to read the Telegram bot secret.
resource "aws_iam_role_policy" "lambda_secrets_read" {
  name = "secrets-manager-read"
  role = aws_iam_role.telegram_webhook_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.telegram_bot.arn
      }
    ]
  })
}

# ─── CloudWatch Log Group ─────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "telegram_webhook" {
  name              = "/aws/lambda/judgemind-telegram-webhook-${var.environment}"
  retention_in_days = var.log_retention_days

}

# ─── Lambda Function ─────────────────────────────────────────────────────────
# Stub function — the actual code will be deployed separately. We use a
# placeholder zip so Terraform can create the function resource.

data "archive_file" "lambda_placeholder" {
  type        = "zip"
  output_path = "${path.module}/placeholder.zip"

  source {
    content  = <<-PYTHON
      def handler(event, context):
          return {"statusCode": 200, "body": "placeholder"}
    PYTHON
    filename = "lambda_function.py"
  }
}

resource "aws_lambda_function" "telegram_webhook" {
  function_name = "judgemind-telegram-webhook-${var.environment}"
  description   = "Telegram webhook handler — validates sender, enqueues to SQS"
  role          = aws_iam_role.telegram_webhook_lambda.arn
  handler       = "lambda_function.handler"
  runtime       = "python3.12"
  memory_size   = var.lambda_memory_mb
  timeout       = var.lambda_timeout_seconds

  filename         = data.archive_file.lambda_placeholder.output_path
  source_code_hash = data.archive_file.lambda_placeholder.output_base64sha256

  environment {
    variables = {
      SQS_QUEUE_URL      = aws_sqs_queue.telegram_inbound.url
      TELEGRAM_SECRET_ID = aws_secretsmanager_secret.telegram_bot.arn
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.telegram_webhook,
    aws_iam_role_policy_attachment.lambda_basic_execution,
  ]

  # Code is deployed separately — don't revert to the placeholder on apply.
  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }

}

# ─── API Gateway (HTTP API v2) ────────────────────────────────────────────────

resource "aws_apigatewayv2_api" "telegram_webhook" {
  name          = "judgemind-telegram-webhook-${var.environment}"
  protocol_type = "HTTP"

}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.telegram_webhook.id
  name        = "$default"
  auto_deploy = true

}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.telegram_webhook.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.telegram_webhook.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "webhook" {
  api_id    = aws_apigatewayv2_api.telegram_webhook.id
  route_key = "POST /webhook"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

# Allow API Gateway to invoke the Lambda function.
resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.telegram_webhook.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.telegram_webhook.execution_arn}/*/*"
}
