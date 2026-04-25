variable "environment" {
  description = "Deployment environment — agent role is dev-only (staging and production are human-operated)"
  type        = string

  validation {
    condition     = contains(["dev"], var.environment)
    error_message = "environment must be: dev (agent role is dev-only; staging/production are human-operated)"
  }
}

variable "document_archive_bucket_arn" {
  description = "ARN of the S3 document archive bucket (staging/* and spotcheck/* prefixes are granted)"
  type        = string
}

variable "scraper_role_arn" {
  description = "ARN of the scraper IAM role — the agent may pass this role when launching ECS tasks"
  type        = string
}

variable "maintenance_role_arn" {
  description = "ARN of the maintenance IAM role — if non-empty, the agent may also pass this role when launching ECS tasks"
  type        = string
  default     = ""
}

variable "dev_account_id" {
  description = "AWS account ID for the dev environment — used to construct scoped resource ARNs"
  type        = string
  default     = "155326049300"
}
