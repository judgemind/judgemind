output "task_definition_arn" {
  description = "ARN of the field-population audit ECS task definition"
  value       = aws_ecs_task_definition.field_population_audit.arn
}

output "task_definition_family" {
  description = "Family name of the field-population audit ECS task definition"
  value       = aws_ecs_task_definition.field_population_audit.family
}

output "schedule_arn" {
  description = "ARN of the EventBridge Scheduler schedule"
  value       = aws_scheduler_schedule.field_population_audit.arn
}

output "schedule_name" {
  description = "Name of the EventBridge Scheduler schedule (used for `aws scheduler get-schedule` smoke checks)"
  value       = aws_scheduler_schedule.field_population_audit.name
}

output "log_group_name" {
  description = "CloudWatch log group name for field-population audit output"
  value       = aws_cloudwatch_log_group.field_population_audit.name
}

output "security_group_id" {
  description = "ID of the field-population audit security group"
  value       = aws_security_group.field_population_audit.id
}

output "task_role_arn" {
  description = "ARN of the field-population audit task role"
  value       = aws_iam_role.task_role.arn
}

output "scheduler_role_arn" {
  description = "ARN of the EventBridge Scheduler execution role"
  value       = aws_iam_role.scheduler_execution.arn
}
