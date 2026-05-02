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

output "api_repository_url" {
  description = "ECR repository URL for API images"
  value       = aws_ecr_repository.api.repository_url
}

output "dispatcher_repository_url" {
  description = "ECR repository URL for dispatcher v2 daemon images"
  value       = aws_ecr_repository.dispatcher.repository_url
}

output "dispatcher_repository_arn" {
  description = "ARN of the dispatcher v2 ECR repository"
  value       = aws_ecr_repository.dispatcher.arn
}

output "dispatcher_agent_runner_repository_url" {
  description = "ECR repository URL for the dispatcher agent-runner image (#3090 Stage 1b)"
  value       = aws_ecr_repository.dispatcher_agent_runner.repository_url
}

output "dispatcher_agent_runner_repository_arn" {
  description = "ARN of the dispatcher agent-runner ECR repository"
  value       = aws_ecr_repository.dispatcher_agent_runner.arn
}

output "dispatcher_v3_repository_url" {
  description = "ECR repository URL for the dispatcher v3 unified image (issue #3886; one image with multiple ECS task-def entrypoints)"
  value       = aws_ecr_repository.dispatcher_v3.repository_url
}

output "dispatcher_v3_repository_arn" {
  description = "ARN of the dispatcher v3 ECR repository"
  value       = aws_ecr_repository.dispatcher_v3.arn
}
