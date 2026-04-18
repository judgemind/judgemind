output "ecr_repository_url" {
  description = "ECR repository URL for the dispatcher-spike image."
  value       = aws_ecr_repository.dispatcher_spike.repository_url
}

output "ecr_repository_name" {
  description = "ECR repository short name for the dispatcher-spike image."
  value       = aws_ecr_repository.dispatcher_spike.name
}

output "task_definition_arn" {
  description = "ECS task definition ARN for the dispatcher-spike (latest revision)."
  value       = aws_ecs_task_definition.dispatcher_spike.arn
}

output "task_definition_family" {
  description = "ECS task definition family name (used by `aws ecs run-task` to grab the latest revision)."
  value       = aws_ecs_task_definition.dispatcher_spike.family
}

output "log_group_name" {
  description = "CloudWatch log group for dispatcher-spike output."
  value       = aws_cloudwatch_log_group.dispatcher_spike.name
}

output "execution_role_arn" {
  description = "IAM role ARN used by the ECS agent to launch dispatcher-spike tasks."
  value       = aws_iam_role.execution.arn
}

output "task_role_arn" {
  description = "IAM role ARN assumed by the spike container at runtime (empty privileges)."
  value       = aws_iam_role.task.arn
}
