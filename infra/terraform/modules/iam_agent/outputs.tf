output "role_arn" {
  description = "ARN of the agent IAM role (copy into ~/.aws/config as role_arn for the judgemind-agent profile)"
  value       = aws_iam_role.agent.arn
}

output "role_name" {
  description = "Name of the agent IAM role"
  value       = aws_iam_role.agent.name
}
