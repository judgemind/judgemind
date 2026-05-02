# Outputs from the dispatcher-v3 task-defs module.
#
# F4 (launcher ECS service) consumes `launcher_task_definition_arn`
# and `launcher_log_group_name`. F5 (EventBridge schedules) consumes
# `scheduled_skill_task_definition_family`. The launcher itself
# consumes the three short-lived family names at runtime via env var
# (so the scheduler tick can call ecs:RunTask without re-resolving
# ARNs from naming conventions).

output "launcher_task_definition_arn" {
  description = "ARN of the launcher task definition (latest revision). Wired into F4's ECS service as `task_definition`."
  value       = aws_ecs_task_definition.launcher.arn
}

output "launcher_task_definition_family" {
  description = "Family name of the launcher task definition (judgemind-dispatcher-v3-launcher). F4's ECS service may reference family-only so revisions roll forward without an explicit task_definition update."
  value       = aws_ecs_task_definition.launcher.family
}

output "task_runner_task_definition_arn" {
  description = "ARN of the task-runner task definition (latest revision). The launcher passes this to ecs:RunTask when claiming an issue."
  value       = aws_ecs_task_definition.task_runner.arn
}

output "task_runner_task_definition_family" {
  description = "Family name of the task-runner task definition. Exposed to the launcher container via env so ecs:RunTask resolves the latest revision automatically."
  value       = aws_ecs_task_definition.task_runner.family
}

output "diagnoser_task_definition_arn" {
  description = "ARN of the diagnoser task definition (latest revision). The launcher passes this to ecs:RunTask after a task-runner exits non-zero."
  value       = aws_ecs_task_definition.diagnoser.arn
}

output "diagnoser_task_definition_family" {
  description = "Family name of the diagnoser task definition."
  value       = aws_ecs_task_definition.diagnoser.family
}

output "scheduled_skill_task_definition_arn" {
  description = "ARN of the scheduled-skill task definition (latest revision). EventBridge cron rules (F5) target this ARN with a per-skill SKILL_NAME env override."
  value       = aws_ecs_task_definition.scheduled_skill.arn
}

output "scheduled_skill_task_definition_family" {
  description = "Family name of the scheduled-skill task definition. F5 EventBridge rules may reference family-only so revisions roll forward."
  value       = aws_ecs_task_definition.scheduled_skill.family
}

# ------------------------------------------------------------------
# Log groups
# ------------------------------------------------------------------

output "launcher_log_group_name" {
  description = "CloudWatch log group for launcher container output (/judgemind/dispatcher-v3/launcher)."
  value       = aws_cloudwatch_log_group.launcher.name
}

output "task_runner_log_group_name" {
  description = "CloudWatch log group for task-runner container output (/judgemind/dispatcher-v3/task-runner). Read by the launcher's silent-hang detector via DescribeLogStreams."
  value       = aws_cloudwatch_log_group.task_runner.name
}

output "diagnoser_log_group_name" {
  description = "CloudWatch log group for diagnoser container output (/judgemind/dispatcher-v3/diagnoser)."
  value       = aws_cloudwatch_log_group.diagnoser.name
}

output "scheduled_skill_log_group_name" {
  description = "CloudWatch log group for scheduled-skill container output (/judgemind/dispatcher-v3/scheduled-skill)."
  value       = aws_cloudwatch_log_group.scheduled_skill.name
}

# ------------------------------------------------------------------
# Image
# ------------------------------------------------------------------

output "image_digest" {
  description = "Resolved image digest baked into every task-def revision (sha256:...). Re-runs of terraform apply after a fresh deploy-dispatcher-v3 push will resolve a new digest and register fresh task-def revisions."
  value       = data.aws_ecr_image.dispatcher_v3.image_digest
}

output "image_uri" {
  description = "Full digest-pinned image URI baked into every task-def revision (<repo>@sha256:<digest>)."
  value       = "${var.ecr_repository_url}@${data.aws_ecr_image.dispatcher_v3.image_digest}"
}
