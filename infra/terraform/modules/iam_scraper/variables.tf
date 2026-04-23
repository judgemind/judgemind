variable "environment" {
  description = "Deployment environment (dev, staging, production)"
  type        = string

  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be one of: dev, staging, production"
  }
}

variable "document_archive_bucket_arn" {
  description = "ARN of the S3 document archive bucket the scraper may write to"
  type        = string
}
