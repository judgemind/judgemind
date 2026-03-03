output "domain_endpoint" {
  description = "OpenSearch domain endpoint (use https:// prefix for connections)"
  value       = aws_opensearch_domain.main.endpoint
}

output "domain_arn" {
  description = "ARN of the OpenSearch domain"
  value       = aws_opensearch_domain.main.arn
}

output "security_group_id" {
  description = "Security group ID — grant ingress from other resources that need OpenSearch access"
  value       = aws_security_group.opensearch.id
}

output "master_credentials_secret_arn" {
  description = "ARN of the Secrets Manager secret holding OpenSearch master user credentials"
  value       = aws_secretsmanager_secret.opensearch_master.arn
}
