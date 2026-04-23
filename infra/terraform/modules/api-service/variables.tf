variable "environment" {
  description = "Deployment environment (dev, staging, production)"
  type        = string

  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be one of: dev, staging, production"
  }
}

variable "vpc_id" {
  description = "ID of the VPC where the API service runs"
  type        = string
}

variable "public_subnet_ids" {
  description = "IDs of the public subnets for the ALB"
  type        = list(string)
}

variable "private_subnet_ids" {
  description = "IDs of the private subnets for the ECS tasks"
  type        = list(string)
}

variable "ecs_cluster_arn" {
  description = "ARN of the ECS cluster to deploy into"
  type        = string
}

variable "ecs_cluster_name" {
  description = "Name of the ECS cluster"
  type        = string
}

variable "execution_role_arn" {
  description = "ARN of the ECS task execution role (ECR pull, CloudWatch logs, Secrets Manager)"
  type        = string
}

variable "ecr_repository_url" {
  description = "ECR repository URL for the API container image"
  type        = string
}

variable "image_tag" {
  description = "Container image tag to deploy"
  type        = string
  default     = "latest"
}

variable "task_cpu" {
  description = "CPU units for the Fargate task (256 = 0.25 vCPU)"
  type        = number
  default     = 256
}

variable "task_memory" {
  description = "Memory (MiB) for the Fargate task"
  type        = number
  default     = 512
}

variable "desired_count" {
  description = "Number of API task replicas"
  type        = number
  default     = 1
}

variable "container_port" {
  description = "Port the API container listens on"
  type        = number
  default     = 3001
}

variable "domain_name" {
  description = "Domain name for the API (e.g. dev.api.judgemind.org). Used for the ACM certificate."
  type        = string
}

variable "db_connection_secret_arn" {
  description = "ARN of the Secrets Manager secret containing DATABASE_URL (JSON key: url)"
  type        = string
}

variable "redis_url" {
  description = "Redis connection URL"
  type        = string
  default     = ""
}

variable "opensearch_url" {
  description = "OpenSearch endpoint URL"
  type        = string
  default     = ""
}

variable "opensearch_credentials_secret_arn" {
  description = "ARN of the Secrets Manager secret holding OpenSearch credentials (JSON keys: username, password)"
  type        = string
  default     = ""
}

variable "cors_allowed_origins" {
  description = "Comma-separated list of allowed CORS origins"
  type        = string
  default     = ""
}

variable "log_retention_days" {
  description = "Number of days to retain CloudWatch log events"
  type        = number
  default     = 30
}

variable "ses_configuration_set_name" {
  description = "SES configuration set name for transactional email tracking"
  type        = string
  default     = ""
}

variable "email_from" {
  description = "Default sender address for transactional emails"
  type        = string
  default     = "no-reply@judgemind.org"
}

variable "document_archive_bucket_arn" {
  description = "ARN of the document archive S3 bucket (for presigned URL generation)"
  type        = string
  default     = ""
}

# ─── Alert Configuration ──────────────────────────────────────────────────────

variable "enable_alerts" {
  description = "Whether to create CloudWatch alarms for the API service"
  type        = bool
  default     = false
}

variable "alert_sns_topic_arn" {
  description = "ARN of the SNS topic for alarm notifications (required if enable_alerts is true)"
  type        = string
  default     = ""
}

variable "error_5xx_threshold" {
  description = "Number of 5xx errors in a 5-minute window before alarming"
  type        = number
  default     = 10
}

variable "error_4xx_threshold" {
  description = "Number of 4xx errors in a 5-minute window before alarming (higher than 5xx since some 4xx is normal)"
  type        = number
  default     = 100
}

variable "latency_p99_threshold" {
  description = "P99 latency threshold in seconds before alarming"
  type        = number
  default     = 5
}

variable "unhealthy_host_threshold" {
  description = "Number of unhealthy hosts before alarming"
  type        = number
  default     = 0
}
