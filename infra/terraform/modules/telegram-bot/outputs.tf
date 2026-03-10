output "telegram_webhook_url" {
  description = "Public URL for the Telegram webhook endpoint"
  value       = "${aws_apigatewayv2_api.telegram_webhook.api_endpoint}/webhook"
}

output "telegram_inbound_queue_url" {
  description = "SQS queue URL for inbound Telegram messages"
  value       = aws_sqs_queue.telegram_inbound.url
}

output "telegram_inbound_queue_arn" {
  description = "SQS queue ARN for inbound Telegram messages"
  value       = aws_sqs_queue.telegram_inbound.arn
}

output "telegram_secret_arn" {
  description = "ARN of the Secrets Manager secret for the Telegram bot token and allowlist"
  value       = aws_secretsmanager_secret.telegram_bot.arn
}

output "lambda_function_name" {
  description = "Name of the webhook Lambda function"
  value       = aws_lambda_function.telegram_webhook.function_name
}

output "lambda_function_arn" {
  description = "ARN of the webhook Lambda function"
  value       = aws_lambda_function.telegram_webhook.arn
}

output "api_gateway_id" {
  description = "ID of the HTTP API Gateway"
  value       = aws_apigatewayv2_api.telegram_webhook.id
}
