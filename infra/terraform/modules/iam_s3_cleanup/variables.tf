variable "environment" {
  description = "Deployment environment -- s3-cleanup role is dev-only (production raw-prefix cleanup is human-only per #4440 scope exclusions)"
  type        = string

  validation {
    condition     = contains(["dev"], var.environment)
    error_message = "environment must be: dev (production raw-prefix cleanup is human-only; do not instantiate this module in production)"
  }
}

variable "document_archive_bucket_arn" {
  description = "ARN of the S3 document archive bucket (only the ca/*/*/raw/* prefix is granted)"
  type        = string
}
