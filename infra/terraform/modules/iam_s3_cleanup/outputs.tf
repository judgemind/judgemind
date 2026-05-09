output "role_arn" {
  description = "ARN of the s3-cleanup IAM role (pass to scripts/ecs-run-task.sh --role <role_name>)"
  value       = aws_iam_role.s3_cleanup.arn
}

output "role_name" {
  description = "Name of the s3-cleanup IAM role (e.g. judgemind-s3-cleanup-dev)"
  value       = aws_iam_role.s3_cleanup.name
}
