output "role_arn" {
  description = "ARN of the maintenance IAM role"
  value       = aws_iam_role.maintenance.arn
}

output "role_name" {
  description = "Name of the maintenance IAM role"
  value       = aws_iam_role.maintenance.name
}
