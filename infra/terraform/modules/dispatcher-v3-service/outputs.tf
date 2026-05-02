# Outputs from the dispatcher-v3 launcher ECS service module.
#
# Consumed by:
#   * `environments/dev/main.tf` for top-level outputs (operators query
#     `terraform output dispatcher_v3_launcher_service_name` to drive
#     `aws ecs describe-services` / `update-service` commands).
#   * Future heartbeat / circuit-breaker alarm modules that target the
#     service by name in CloudWatch dimensions.

output "service_name" {
  description = "Name of the dispatcher-v3 launcher ECS service (default `judgemind-dispatcher-v3-<environment>`, e.g. `judgemind-dispatcher-v3-dev`). Use with `aws ecs describe-services --cluster <cluster> --services <service_name>` to inspect runningCount / desiredCount / deployment status."
  value       = aws_ecs_service.launcher.name
}

output "service_arn" {
  description = "ARN of the dispatcher-v3 launcher ECS service. Useful for scoping CloudWatch alarms or IAM resource conditions."
  value       = aws_ecs_service.launcher.id
}

output "security_group_id" {
  description = "Security group ID attached to the launcher's task ENI. Outbound HTTPS + Postgres + Redis only -- no inbound rules. Reference this from any other module (e.g. RDS / Redis security group) that needs to allow ingress from the launcher."
  value       = aws_security_group.launcher.id
}

output "security_group_arn" {
  description = "ARN of the launcher security group. Useful for IAM resource conditions or cross-module references."
  value       = aws_security_group.launcher.arn
}
