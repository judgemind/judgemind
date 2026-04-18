variable "environment" {
  description = "Deployment environment name (e.g. dev). Must be dev for the spike."
  type        = string
}

variable "anthropic_api_key_secret_arn" {
  description = "Secrets Manager ARN for the ANTHROPIC_API_KEY used by `claude -p`."
  type        = string
}

variable "github_token_secret_arn" {
  description = "Secrets Manager ARN for a GITHUB_TOKEN used by the MCP github server (optional)."
  type        = string
  default     = ""
}

variable "db_connection_secret_arn" {
  description = "Secrets Manager ARN for the dev DATABASE_URL secret (optional — only needed if the in-container code ever writes directly to Postgres; the spike's wrapper script handles DB writes outside the VPC in the common path)."
  type        = string
  default     = ""
}

variable "image_tag" {
  description = "ECR image tag for the spike container. Default 'latest' — the wrapper script overrides via container-override image when explicit version pinning is needed."
  type        = string
  default     = "latest"
}

variable "task_cpu" {
  description = "Fargate task CPU units. 1024 = 1 vCPU. Room for a single claude -p plus npm for MCP server shim."
  type        = number
  default     = 1024
}

variable "task_memory" {
  description = "Fargate task memory (MB). 2048 is comfortable for Claude CLI + MCP server subprocess."
  type        = number
  default     = 2048
}
