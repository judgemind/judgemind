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
