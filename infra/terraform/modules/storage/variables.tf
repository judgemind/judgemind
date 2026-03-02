variable "bucket_name" {
  description = "Name of the S3 bucket"
  type        = string
}

variable "environment" {
  description = "Deployment environment (dev, staging, production)"
  type        = string

  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be one of: dev, staging, production"
  }
}

variable "enable_object_lock" {
  description = "Enable S3 Object Lock (WORM). Must be true for production. Cannot be enabled on an existing bucket."
  type        = bool
  default     = false
}

variable "object_lock_retention_years" {
  description = "Default COMPLIANCE retention period in years when object lock is enabled. Legal records are retained for 7 years by default."
  type        = number
  default     = 7
}

variable "tags" {
  description = "Additional tags to apply to all resources"
  type        = map(string)
  default     = {}
}
