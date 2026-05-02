output "launcher_role_arn" {
  description = "ARN of the dispatcher-v3 launcher task role (narrow scheduler scope). Wired into F2's `launcher` task definition as the `taskRoleArn`."
  value       = aws_iam_role.launcher.arn
}

output "launcher_role_name" {
  description = "Name of the dispatcher-v3 launcher task role. Useful for `aws iam simulate-principal-policy` regression checks."
  value       = aws_iam_role.launcher.name
}

output "agent_task_role_arn" {
  description = "ARN of the dispatcher-v3 agent task role (dev-admin equivalent shared by task-runner / diagnoser / scheduled-skill). Wired into F2's three short-lived task definitions as the `taskRoleArn`."
  value       = aws_iam_role.agent_task.arn
}

output "agent_task_role_name" {
  description = "Name of the dispatcher-v3 agent task role."
  value       = aws_iam_role.agent_task.name
}

output "execution_role_arn" {
  description = "ARN of the shared dispatcher-v3 task execution role (used by every v3 task definition for ECR pull + secret injection + log writes). Wired into F2's task definitions as the `executionRoleArn`."
  value       = aws_iam_role.execution.arn
}

output "execution_role_name" {
  description = "Name of the shared dispatcher-v3 task execution role."
  value       = aws_iam_role.execution.name
}
