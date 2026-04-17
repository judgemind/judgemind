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

variable "opensearch_credentials_secret_arn" {
  description = "ARN of the Secrets Manager secret holding OpenSearch master user credentials (JSON keys: username, password). Required when db_connection_secret_arn is set."
  type        = string
  default     = ""
}

variable "llm_provider" {
  description = "LLM provider for the ingestion worker: \"anthropic\" or \"google\". Must match the API key secret that is provisioned."
  type        = string
  default     = "anthropic"

  validation {
    condition     = contains(["anthropic", "google"], var.llm_provider)
    error_message = "llm_provider must be \"anthropic\" or \"google\"."
  }
}

variable "anthropic_api_key_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the Anthropic API key (plain string). When set, ANTHROPIC_API_KEY is injected into the ingestion worker container."
  type        = string
  default     = ""
}

variable "google_api_key_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the Google API key (plain string). When set, GOOGLE_API_KEY is injected into the ingestion worker container."
  type        = string
  default     = ""
}

variable "courtlistener_api_token_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the CourtListener API token (plain string). When set, COURTLISTENER_API_TOKEN is injected into the scraper container."
  type        = string
  default     = ""
}

variable "capsolver_api_key_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the CAPSolver API key (plain string). When set, CAPSOLVER_API_KEY is injected into the scraper container, enabling deterministic Cloudflare Turnstile solves (e.g. SF civil tentatives). When unset, scrapers fall back to stealth-only Turnstile auto-solve."
  type        = string
  default     = ""
}

variable "proxy_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the residential proxy URL (plain string). When set, SD_PROXY_URL and SF_PROXY_URL are both injected into the scraper container from the same secret (SD scraper uses the former, SF civil scraper uses the latter per #2622)."
  type        = string
  default     = ""
}

variable "proxy_port" {
  description = "TCP port used by the residential proxy. An egress rule is added to the scraper security group when proxy_secret_arn is set."
  type        = number
  default     = 33335
}

variable "ingestion_idle_threshold_seconds" {
  description = "Maximum idle time (seconds) before the ingestion worker idle alarm fires. Default 90000 (25 hours) provides a 1-hour buffer past the daily scraper schedule. Lower this if scrapers run more frequently (e.g. 7200 for every-2-hour schedules)."
  type        = number
  default     = 90000
}
