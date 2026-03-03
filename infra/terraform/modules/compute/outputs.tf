output "cluster_arn" {
  description = "ARN of the ECS cluster"
  value       = aws_ecs_cluster.main.arn
}

output "cluster_name" {
  description = "Name of the ECS cluster"
  value       = aws_ecs_cluster.main.name
}

output "task_definition_arn" {
  description = "ARN of the scraper Fargate task definition"
  value       = aws_ecs_task_definition.scraper.arn
}

output "task_execution_role_arn" {
  description = "ARN of the ECS task execution role (used by ECR repository policy)"
  value       = aws_iam_role.ecs_task_execution.arn
}

output "security_group_id" {
  description = "ID of the scraper security group (outbound HTTPS only)"
  value       = aws_security_group.scraper.id
}

output "log_group_name" {
  description = "CloudWatch log group name for scraper output"
  value       = aws_cloudwatch_log_group.scraper.name
}

output "schedule_arn" {
  description = "ARN of the EventBridge Scheduler schedule"
  value       = aws_scheduler_schedule.scraper.arn
}
