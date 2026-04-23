variable "environment" {
  description = "Deployment environment (dev, staging, production)"
  type        = string

  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be one of: dev, staging, production"
  }
}

variable "vpc_id" {
  description = "ID of the VPC where the OpenSearch domain is deployed"
  type        = string
}

variable "private_subnet_ids" {
  description = "IDs of the private subnets for OpenSearch VPC endpoints"
  type        = list(string)
}

variable "instance_type" {
  description = "OpenSearch instance type"
  type        = string
  default     = "t3.small.search"
}

variable "instance_count" {
  description = "Number of data nodes in the OpenSearch cluster"
  type        = number
  default     = 1
}

variable "ebs_volume_size" {
  description = "Size of the EBS volume (GiB) attached to each data node"
  type        = number
  default     = 20
}

variable "engine_version" {
  description = "OpenSearch engine version"
  type        = string
  default     = "OpenSearch_2.11"
}

variable "master_user_name" {
  description = "Master user name for OpenSearch internal user database"
  type        = string
  default     = "admin"
}
