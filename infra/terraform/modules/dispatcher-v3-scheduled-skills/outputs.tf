# Outputs from the dispatcher-v3 scheduled-skills module.

output "rule_names" {
  description = "Map of skill name -> EventBridge rule name. Useful for `aws events describe-rule` smoke checks (e.g. `aws events describe-rule --name dispatcher-v3-spotcheck`)."
  value       = { for k, _ in aws_cloudwatch_event_rule.scheduled_skill : k => aws_cloudwatch_event_rule.scheduled_skill[k].name }
}

output "rule_arns" {
  description = "Map of skill name -> EventBridge rule ARN."
  value       = { for k, _ in aws_cloudwatch_event_rule.scheduled_skill : k => aws_cloudwatch_event_rule.scheduled_skill[k].arn }
}

output "events_invoker_role_arn" {
  description = "ARN of the EventBridge invoker role (assumed by events.amazonaws.com when firing the rules)."
  value       = aws_iam_role.events_invoker.arn
}

output "dlq_url" {
  description = "URL of the shared dead-letter queue for failed EventBridge invocations. Operators use `aws sqs receive-message --queue-url <url>` to inspect failed events."
  value       = aws_sqs_queue.scheduled_skills_dlq.id
}

output "dlq_arn" {
  description = "ARN of the shared dead-letter queue. Useful for cross-account / cross-region IAM grants if a future operator wants to forward failures."
  value       = aws_sqs_queue.scheduled_skills_dlq.arn
}

output "alarm_names" {
  description = "Map of skill name -> CloudWatch alarm name (one alarm per rule). Empty when `enable_alerts = false` or `alert_sns_topic_arn` is unset."
  value       = { for k, v in aws_cloudwatch_metric_alarm.eventbridge_failures : k => v.alarm_name }
}

output "dlq_depth_alarm_name" {
  description = "CloudWatch alarm name for the DLQ depth secondary-signal alarm (`AWS/SQS ApproximateNumberOfMessagesVisible`). Null when `enable_alerts = false` or `alert_sns_topic_arn` is unset."
  value       = try(aws_cloudwatch_metric_alarm.dlq_depth[0].alarm_name, null)
}
