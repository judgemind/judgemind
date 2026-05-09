variable "environment" {
  description = "Deployment environment (dev, staging, production)"
  type        = string

  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be one of: dev, staging, production"
  }
}

variable "vpc_id" {
  description = "ID of the VPC where ElastiCache is deployed"
  type        = string
}

variable "private_subnet_ids" {
  description = "IDs of the private subnets for the ElastiCache subnet group"
  type        = list(string)
}

variable "node_type" {
  description = "ElastiCache node type (e.g. cache.t4g.micro)"
  type        = string
  default     = "cache.t4g.micro"
}

variable "num_cache_nodes" {
  description = "Number of cache nodes in the cluster"
  type        = number
  default     = 1
}

variable "engine_version" {
  description = "Redis engine version"
  type        = string
  default     = "7.1"
}

variable "apply_immediately" {
  description = "Apply ElastiCache modifications (node_type, engine_version, parameter_group_name) immediately instead of deferring to the next weekly maintenance window. Defaults to false so production-grade environments keep the safer maintenance-window behavior; set to true in dev so dispatcher-driven applies land changes on the next apply rather than waiting. See #2573 for the equivalent RDS pattern and #2581 for this extension."
  type        = bool
  default     = false
}

variable "enable_alerts" {
  description = "Whether to create CloudWatch alarms on the ElastiCache cluster (DatabaseMemoryUsagePercentage warn at 80%, critical at 95%). See #4475 for the failure mode these catch."
  type        = bool
  default     = false
}

variable "alert_sns_topic_arn" {
  description = "ARN of the SNS topic that receives alarm notifications. Empty string disables SNS actions but the alarms still exist (state changes visible in the CloudWatch console). Typically wired to module.compute.alerts_topic_arn so cache + scraper alarms share one topic."
  type        = string
  default     = ""
}
