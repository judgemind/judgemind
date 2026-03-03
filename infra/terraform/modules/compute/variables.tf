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

variable "ecr_repository_url" {
  description = "ECR repository URL for the scraper container image"
  type        = string
}

variable "scraper_task_role_arn" {
  description = "ARN of the IAM role assumed by the scraper container (S3 write access)"
  type        = string
}

variable "scraper_image_tag" {
  description = "Container image tag to deploy (e.g. latest, v1.0.0, sha-abc1234)"
  type        = string
  default     = "latest"
}

variable "task_cpu" {
  description = "CPU units for the Fargate task (256 = 0.25 vCPU, 512 = 0.5 vCPU)"
  type        = number
  default     = 512
}

variable "task_memory" {
  description = "Memory (MiB) for the Fargate task"
  type        = number
  default     = 1024
}

variable "schedule_expression" {
  description = "EventBridge schedule expression for scraper runs (e.g. rate(1 day))"
  type        = string
  default     = "rate(1 day)"
}

variable "schedule_enabled" {
  description = "Whether the EventBridge scheduled task is enabled"
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "Number of days to retain CloudWatch log events"
  type        = number
  default     = 30
}
