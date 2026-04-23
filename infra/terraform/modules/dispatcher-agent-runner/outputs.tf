output "task_definition_arn" {
  description = "ARN of the agent-runner task definition. Use with `aws ecs run-task --task-definition <arn>` for Stage 1b manual smoke."
  value       = aws_ecs_task_definition.agent_runner.arn
}

output "task_definition_family" {
  description = "Family name of the agent-runner task definition (`judgemind-dispatcher-agent-runner-<env>`). Daemon Stage 2 passes this to `ecs:RunTask` so it always runs the latest revision."
  value       = aws_ecs_task_definition.agent_runner.family
}

output "log_group_name" {
  description = "CloudWatch log group for agent-runner container output (`/ecs/judgemind-dispatcher-agent-runner-<env>`)."
  value       = aws_cloudwatch_log_group.agent_runner.name
}

output "log_group_arn" {
  description = "ARN of the agent-runner CloudWatch log group."
  value       = aws_cloudwatch_log_group.agent_runner.arn
}

output "execution_role_arn" {
  description = "ARN of the ECS task execution role (pulls ECR + fetches secrets + writes logs)."
  value       = aws_iam_role.execution.arn
}

output "task_role_arn" {
  description = "ARN of the agent-runner task role (narrow: Secrets Manager read on DATABASE_URL+PAT, CloudWatch log writes to the agent-runner group only). Exported so the daemon can reference it when calling `ecs:RunTask` and so IAM audits can verify least-privilege."
  value       = aws_iam_role.task.arn
}

output "security_group_id" {
  description = "Security group ID for agent-runner tasks. Stage 2 daemon passes this to `ecs:RunTask --network-configuration`."
  value       = aws_security_group.agent_runner.id
}
