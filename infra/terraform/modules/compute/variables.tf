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
  description = "EventBridge schedule expression — rate (e.g. rate(1 day)) or cron (e.g. cron(0 13 * * ? *) for 6 AM PT)"
  type        = string
  default     = "rate(1 day)"
}

variable "schedule_timezone" {
  description = "IANA timezone for the EventBridge schedule (only applies to cron expressions)"
  type        = string
  default     = "UTC"
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

variable "enable_alerts" {
  description = "Whether to create CloudWatch alarms and SNS topic for scraper failure alerts"
  type        = bool
  default     = false
}

variable "alert_email" {
  description = "Email address for scraper failure alert notifications (optional, SNS subscription)"
  type        = string
  default     = ""
}

variable "redis_url" {
  description = "Redis connection URL for the event bus (e.g. redis://host:6379). Empty string disables event emission."
  type        = string
  default     = ""
}

variable "document_archive_bucket" {
  description = "S3 bucket name for document archival (e.g. judgemind-document-archive-production). Empty string disables archival."
  type        = string
  default     = ""
}

variable "db_connection_secret_arn" {
  description = "ARN of the Secrets Manager secret containing the DATABASE_URL (JSON key: url). When set, the ingestion worker ECS service is deployed."
  type        = string
  default     = ""
}

variable "opensearch_url" {
  description = "OpenSearch endpoint URL for the ingestion worker (e.g. https://vpc-...us-west-2.es.amazonaws.com). Required when db_connection_secret_arn is set."
  type        = string
  default     = ""
}
