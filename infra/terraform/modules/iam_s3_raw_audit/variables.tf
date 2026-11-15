variable "environment" {
  description = "Deployment environment -- the raw-audit role is dev-only (#4440, #4993)"
  type        = string

  validation {
    condition     = contains(["dev"], var.environment)
    error_message = "environment must be: dev (instantiate this module elsewhere only after operator review)"
  }
}

variable "document_archive_bucket_arn" {
  description = "ARN of the S3 document archive bucket (object reads are granted on ca/*/*/raw/* only)"
  type        = string
}
