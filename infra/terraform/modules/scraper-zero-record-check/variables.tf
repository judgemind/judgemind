variable "environment" {
  description = "Deployment environment (dev, staging, production)"
  type        = string

  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be one of: dev, staging, production"
  }
}

variable "vpc_id" {
  description = "ID of the VPC where ECS tasks run"
  type        = string
}

variable "private_subnet_ids" {
  description = "IDs of the private subnets for ECS task placement"
  type        = list(string)
}

variable "ecs_cluster_arn" {
  description = "ARN of the existing ECS cluster to run the check task on"
  type        = string
}

variable "ecr_repository_url" {
  description = "ECR repository URL for the scraper container image (must contain scripts/check-scraper-zero-record-runner.py)"
  type        = string
}

variable "task_execution_role_arn" {
  description = "ARN of the ECS task execution role (ECR pull + CloudWatch log writes)"
  type        = string
}

variable "db_connection_secret_arn" {
  description = "ARN of the Secrets Manager secret containing the DATABASE_URL (JSON key: url)"
  type        = string
}

variable "github_token_secret_arn" {
  description = "ARN of the Secrets Manager secret containing the GitHub PAT (plain string) used for issue creation"
  type        = string
}

variable "telegram_secret_arn" {
  description = "ARN of the Secrets Manager secret for Telegram notifications (judgemind/telegram/bot). Empty string disables task-role access — the runner still falls back to the execution role if the script can reach Secrets Manager another way, but best practice is to provide this ARN."
  type        = string
  default     = ""
}

variable "scraper_image_tag" {
  description = "Container image tag to deploy (e.g. latest, sha-abc1234)"
  type        = string
  default     = "latest"
}

variable "gh_repo" {
  description = "GitHub repository slug (org/repo) for issue creation"
  type        = string
  default     = "judgemind/judgemind"
}

variable "log_retention_days" {
  description = "Number of days to retain CloudWatch log events"
  type        = number
  default     = 30
}

variable "schedule_enabled" {
  description = "Whether the EventBridge scheduled task is enabled"
  type        = bool
  default     = true
}
