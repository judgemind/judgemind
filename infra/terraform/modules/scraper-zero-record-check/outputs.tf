output "task_definition_arn" {
  description = "ARN of the zero-record check ECS task definition"
  value       = aws_ecs_task_definition.zero_record_check.arn
}

output "task_definition_family" {
  description = "Family name of the zero-record check ECS task definition"
  value       = aws_ecs_task_definition.zero_record_check.family
}

output "schedule_arn" {
  description = "ARN of the EventBridge Scheduler schedule"
  value       = aws_scheduler_schedule.zero_record_check.arn
}

output "log_group_name" {
  description = "CloudWatch log group name for zero-record check output"
  value       = aws_cloudwatch_log_group.zero_record_check.name
}

output "security_group_id" {
  description = "ID of the zero-record check security group"
  value       = aws_security_group.zero_record_check.id
}

output "task_role_arn" {
  description = "ARN of the zero-record check task role"
  value       = aws_iam_role.task_role.arn
}

output "scheduler_role_arn" {
  description = "ARN of the EventBridge Scheduler execution role"
  value       = aws_iam_role.scheduler_execution.arn
}
