output "repository_url" {
  description = "ECR repository URL (used by CI to push images: docker push <url>:<tag>)"
  value       = aws_ecr_repository.scraper.repository_url
}

output "repository_arn" {
  description = "ARN of the ECR repository"
  value       = aws_ecr_repository.scraper.arn
}

output "registry_id" {
  description = "AWS account ID of the ECR registry"
  value       = aws_ecr_repository.scraper.registry_id
}
