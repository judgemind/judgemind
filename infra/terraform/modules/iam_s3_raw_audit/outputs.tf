output "role_arn" {
  description = "ARN of the read-only raw-audit IAM role (pass its name to scripts/ecs-run-task.sh --role)"
  value       = aws_iam_role.s3_raw_audit.arn
}

output "role_name" {
  description = "Name of the read-only raw-audit IAM role (e.g. judgemind-s3-raw-audit-dev)"
  value       = aws_iam_role.s3_raw_audit.name
}
