variable "environment" {
  description = "Deployment environment (dev, staging, production)"
  type        = string

  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be one of: dev, staging, production"
  }
}

variable "vpc_id" {
  description = "ID of the VPC where the dispatcher daemon runs"
  type        = string
}

variable "private_subnet_ids" {
  description = "IDs of the private subnets for the Fargate tasks (needs NAT egress for GitHub/Anthropic/Telegram)"
  type        = list(string)
}

variable "ecs_cluster_arn" {
  description = "ARN of the ECS cluster to deploy the dispatcher service into"
  type        = string
}

variable "ecr_repository_url" {
  description = "ECR repository URL that will host the dispatcher image (populated by sub-task C, #2729). Placeholder until then — the service runs at desired_count=0 so the image tag is not resolved."
  type        = string
}

variable "image_tag" {
  description = "Container image tag to deploy. Safe to leave at default while desired_count=0."
  type        = string
  default     = "latest"
}

variable "desired_count" {
  description = "Number of dispatcher task replicas. Phase 1 keeps this at 0 (inert); Phase 2 flips to 1 (shadow mode); never >1 (the dispatcher is a singleton — overlapping instances would double-spawn agents)."
  type        = number
  default     = 0

  validation {
    condition     = var.desired_count >= 0 && var.desired_count <= 1
    error_message = "desired_count must be 0 or 1 — the dispatcher is a singleton."
  }
}

variable "task_cpu" {
  description = "CPU units for the Fargate task (1024 = 1 vCPU, per §14 of the spec)"
  type        = number
  default     = 1024
}

variable "task_memory" {
  description = "Memory (MiB) for the Fargate task (2048 = 2 GB, per §14 of the spec)"
  type        = number
  default     = 2048
}

variable "ephemeral_storage_gib" {
  description = "Ephemeral storage (GiB) for the Fargate task. 50 GiB per spike 0.6 findings (docs/investigations/dispatcher-v2-spike-0.6.md): realistic 5-concurrent-worktree peak is ~10 GB, so 50 GiB gives 5× headroom."
  type        = number
  default     = 50

  validation {
    condition     = var.ephemeral_storage_gib >= 21 && var.ephemeral_storage_gib <= 200
    error_message = "ephemeral_storage_gib must be in [21, 200] — Fargate platform limits."
  }
}

variable "log_retention_days" {
  description = "Number of days to retain CloudWatch log events for the dispatcher daemon"
  type        = number
  default     = 30
}

# ─── Secrets ──────────────────────────────────────────────────────────────
# Each `*_secret_arn` is the Secrets Manager ARN that will be injected into
# the container as the corresponding env var. Leave empty to skip wiring
# that secret — useful during Phase 1 when some of these don't exist yet
# (e.g. the dispatcher-role DB secret is created by sub-task A, #2727, and
# the scoped GitHub PAT is provisioned by #2700).

variable "anthropic_api_key_secret_arn" {
  description = "Secrets Manager ARN for ANTHROPIC_API_KEY. Required for the daemon to spawn `claude -p` subprocesses."
  type        = string
  default     = ""
}

variable "db_connection_secret_arn" {
  description = "Secrets Manager ARN for the dispatcher-role DATABASE_URL (JSON key: url). Sub-task A (#2727) creates the `judgemind_dispatcher` role and its connection secret; until A merges, callers can pass the main `judgemind` role secret — safe while desired_count=0."
  type        = string
  default     = ""
}

variable "github_token_secret_arn" {
  description = "Secrets Manager ARN for GITHUB_TOKEN (scoped PAT from spike 0.7). Populated by #2700; placeholder empty string is safe while desired_count=0."
  type        = string
  default     = ""
}

variable "telegram_bot_token_secret_arn" {
  description = "Secrets Manager ARN for TELEGRAM_BOT_TOKEN (operator paging)."
  type        = string
  default     = ""
}

variable "gemini_api_key_secret_arn" {
  description = "Secrets Manager ARN for GEMINI_API_KEY. Only required if `runner_by_phase` / `runner_shadow` routes to the Gemini runner (see spec §14 / spike 0.4)."
  type        = string
  default     = ""
}

# ─── GitHub integration ──────────────────────────────────────────────────

variable "github_repo" {
  description = "Target GitHub repo (owner/name) the daemon operates on. Exposed to the container as GITHUB_REPO."
  type        = string
  default     = ""
}

# ─── Alerts ──────────────────────────────────────────────────────────────

variable "enable_alerts" {
  description = "Whether to create CloudWatch alarms for the dispatcher daemon. Set false in Phase 1 if no SNS topic is available yet."
  type        = bool
  default     = false
}

variable "alert_sns_topic_arn" {
  description = "ARN of the SNS topic for alarm notifications (email + Telegram). Required when enable_alerts is true."
  type        = string
  default     = ""
}

variable "heartbeat_stale_seconds" {
  description = "Threshold for the heartbeat staleness alarm. Per §14, default 300 (5 min)."
  type        = number
  default     = 300
}
