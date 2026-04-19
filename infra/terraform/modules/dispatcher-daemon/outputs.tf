output "service_name" {
  description = "Name of the dispatcher daemon ECS service"
  value       = aws_ecs_service.dispatcher.name
}

output "task_definition_arn" {
  description = "ARN of the dispatcher daemon task definition"
  value       = aws_ecs_task_definition.dispatcher.arn
}

output "task_definition_family" {
  description = "Family name of the dispatcher daemon task definition (used by `aws ecs describe-task-definition` / CI deploy scripts)"
  value       = aws_ecs_task_definition.dispatcher.family
}

output "log_group_name" {
  description = "CloudWatch log group for dispatcher daemon output"
  value       = aws_cloudwatch_log_group.dispatcher.name
}

output "log_group_arn" {
  description = "ARN of the dispatcher daemon log group"
  value       = aws_cloudwatch_log_group.dispatcher.arn
}

output "execution_role_arn" {
  description = "ARN of the ECS task execution role (assumed by the ECS agent for ECR pulls + secret fetches)"
  value       = aws_iam_role.execution.arn
}

output "task_role_arn" {
  description = "ARN of the dispatcher task role (assumed by the container at runtime — grants ecs:RunTask self-invocation, ECS Exec SSM, and cloudwatch:PutMetricData)"
  value       = aws_iam_role.task.arn
}

output "security_group_id" {
  description = "Security group ID for dispatcher ECS tasks (outbound HTTPS + Postgres + Redis)"
  value       = aws_security_group.dispatcher.id
}

output "heartbeat_alarm_arn" {
  description = "ARN of the heartbeat staleness CloudWatch alarm (empty when enable_alerts is false)"
  value       = var.enable_alerts ? aws_cloudwatch_metric_alarm.heartbeat_stale[0].arn : ""
}
