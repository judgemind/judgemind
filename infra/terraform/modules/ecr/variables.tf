variable "environment" {
  description = "Deployment environment (dev, staging, production)"
  type        = string

  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be one of: dev, staging, production"
  }
}

variable "ecs_task_execution_role_arn" {
  description = "ARN of the ECS task execution role allowed to pull images. Set when the compute module is provisioned (#20). Leave null to skip the repository pull policy."
  type        = string
  default     = null
}

variable "enable_pull_policy" {
  description = "Whether to create the ECR repository pull policy for ECS. Use this instead of checking the role ARN to avoid unknown-at-plan-time count errors."
  type        = bool
  default     = false
}
