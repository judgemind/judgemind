variable "environment" {
  description = "Deployment environment (dev, staging, production)"
  type        = string

  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be one of: dev, staging, production"
  }
}

variable "lambda_memory_mb" {
  description = "Memory (MB) allocated to the webhook Lambda function"
  type        = number
  default     = 128
}

variable "lambda_timeout_seconds" {
  description = "Timeout (seconds) for the webhook Lambda function"
  type        = number
  default     = 10
}

variable "sqs_message_retention_seconds" {
  description = "How long SQS retains undelivered messages (seconds)"
  type        = number
  default     = 3600 # 1 hour
}

variable "sqs_visibility_timeout_seconds" {
  description = "SQS visibility timeout (seconds) — how long a consumer has to process a message before it becomes visible again"
  type        = number
  default     = 30
}

variable "log_retention_days" {
  description = "Number of days to retain CloudWatch log events"
  type        = number
  default     = 30
}
