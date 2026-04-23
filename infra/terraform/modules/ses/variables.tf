variable "environment" {
  description = "Deployment environment (dev, staging, production)"
  type        = string

  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be one of: dev, staging, production"
  }
}

variable "sending_domain" {
  description = "Domain used to send transactional email (e.g. judgemind.com). Must be a domain you control."
  type        = string
}
