variable "environment" {
  description = "Deployment environment (dev, staging, production)"
  type        = string

  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be one of: dev, staging, production"
  }
}

variable "vpc_id" {
  description = "ID of the VPC where agent-runner tasks run"
  type        = string
}

variable "private_subnet_ids" {
  description = "IDs of the private subnets for agent-runner tasks (needs NAT egress for GitHub/Anthropic)"
  type        = list(string)
}

variable "ecr_repository_url" {
  description = "ECR repository URL hosting the agent-runner image (e.g. `<acct>.dkr.ecr.us-west-2.amazonaws.com/judgemind/dispatcher-agent-runner`). Populated by a follow-up PR that adds the repo + build workflow."
  type        = string
}

variable "image_tag" {
  description = "Container image tag to run. Safe to leave at `latest` for Stage 1b -- daemon picks the pinned SHA at RunTask time in Stage 2."
  type        = string
  default     = "latest"
}

variable "task_cpu" {
  description = "CPU units for the Fargate task (256 = 0.25 vCPU, 512 = 0.5 vCPU, 1024 = 1 vCPU, 4096 = 4 vCPU). Default is 4096 to match the subprocess-daemon envelope -- ralph workload under the 512-baseline saturated CPU at 509/512 for extended bursts (#3153)."
  type        = number
  default     = 4096
}

variable "task_memory" {
  description = "Memory (MiB) for the Fargate task. Default is 16384 (16 GiB) to match the subprocess-daemon envelope -- ralph pegged memory at 1022/1024 MB for hours under the 1024-baseline (#3153). Fargate valid pair: 4096 CPU / 16384 MB."
  type        = number
  default     = 16384
}

variable "ephemeral_storage_gib" {
  description = "Ephemeral storage (GiB) for the Fargate task. Per-agent worktrees run ~700 MB + Claude transcripts; 30 GiB gives room for the whole agent-lifetime without spill."
  type        = number
  default     = 30

  validation {
    condition     = var.ephemeral_storage_gib >= 21 && var.ephemeral_storage_gib <= 200
    error_message = "ephemeral_storage_gib must be in [21, 200] — Fargate platform limits."
  }
}

variable "log_retention_days" {
  description = "Number of days to retain CloudWatch log events for agent-runner tasks."
  type        = number
  default     = 30
}

# ─── Secrets ──────────────────────────────────────────────────────────────

variable "anthropic_api_key_secret_arn" {
  description = "Secrets Manager ARN for ANTHROPIC_API_KEY. Required for the agent-runner to invoke `claude -p`."
  type        = string
  default     = ""
}

variable "db_connection_secret_arn" {
  description = "Secrets Manager ARN for the dispatcher-role DATABASE_URL (JSON key: url). The agent-runner reads phase state from `dispatcher.agents` and persists phase_outputs / ralph_patches rows."
  type        = string
  default     = ""
}

variable "github_token_secret_arn" {
  description = "Secrets Manager ARN for GITHUB_TOKEN (scoped PAT). Agent-runner calls `gh auth login` + `git push` with this."
  type        = string
  default     = ""
}

# ─── GitHub integration ───────────────────────────────────────────────────

variable "github_repo" {
  description = "Target GitHub repo (owner/name) the agent-runner operates on. Exposed to the container as GITHUB_REPO."
  type        = string
  default     = ""
}

variable "repo_url" {
  description = "HTTPS clone URL for the target repository. The entrypoint clones this at boot."
  type        = string
  default     = "https://github.com/judgemind/judgemind.git"
}

# ─── Alerts ──────────────────────────────────────────────────────────────

variable "enable_alerts" {
  description = "Whether to create CloudWatch alarms for the dispatcher agent-runner. Set false when no SNS topic is available yet."
  type        = bool
  default     = false
}

variable "alert_sns_topic_arn" {
  description = "ARN of the SNS topic for alarm notifications (email + Telegram). Required when enable_alerts is true."
  type        = string
  default     = ""
}
