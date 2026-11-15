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

variable "raw_delete_break_glass_role_name" {
  description = "Name of the only IAM role the bucket policy lets delete raw objects (#5013). Empty means judgemind-raw-break-glass-<environment>. The role does not exist by default: an operator creates it, with an MFA-gated trust policy, only for a raw restore or a guarded raw delete, then deletes it."
  type        = string
  default     = ""
}

variable "lifecycle_admin_principal_arns" {
  description = "IAM principal ARNs allowed to change the bucket's lifecycle rules (the Terraform apply identity). When non-empty, the bucket policy denies s3:PutLifecycleConfiguration to every other principal except the break-glass role (#5013). Empty skips that statement."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Additional tags to apply to all resources"
  type        = map(string)
  default     = {}
}
