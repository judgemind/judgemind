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
  description = "ECR repository URL that will host the dispatcher image (populated by sub-task C, #2729). Placeholder until then -- the service runs at desired_count=0 so the image tag is not resolved."
  type        = string
}

variable "image_tag" {
  description = "Container image tag to deploy. Safe to leave at default while desired_count=0."
  type        = string
  default     = "latest"
}

variable "desired_count" {
  description = "Number of dispatcher task replicas. Phase 1 keeps this at 0 (inert); Phase 2 flips to 1 (shadow mode); never >1 (the dispatcher is a singleton -- overlapping instances would double-spawn agents)."
  type        = number
  default     = 0

  validation {
    condition     = var.desired_count >= 0 && var.desired_count <= 1
    error_message = "desired_count must be 0 or 1 — the dispatcher is a singleton."
  }
}

variable "task_cpu" {
  description = "CPU units for the Fargate task (1024 = 1 vCPU, per Section 14 of the spec)"
  type        = number
  default     = 1024
}

variable "task_memory" {
  description = "Memory (MiB) for the Fargate task (2048 = 2 GB, per Section 14 of the spec)"
  type        = number
  default     = 2048
}

variable "ephemeral_storage_gib" {
  description = "Ephemeral storage (GiB) for the Fargate task. 50 GiB per spike 0.6 findings (docs/investigations/dispatcher-v2-spike-0.6.md): realistic 5-concurrent-worktree peak is ~10 GB, so 50 GiB gives 5x headroom."
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
# (e.g. the dispatcher-role DB secret is created by sub-task A, #2727).
# The scoped GitHub PAT (#2700) is wired in `environments/dev/main.tf`
# against `judgemind/dispatcher/github-token`.

variable "anthropic_api_key_secret_arn" {
  description = "Secrets Manager ARN for ANTHROPIC_API_KEY. Required for the daemon to spawn `claude -p` subprocesses."
  type        = string
  default     = ""
}

variable "db_connection_secret_arn" {
  description = "Secrets Manager ARN for the dispatcher-role DATABASE_URL (JSON key: url). Sub-task A (#2727) creates the `judgemind_dispatcher` role and its connection secret; until A merges, callers can pass the main `judgemind` role secret -- safe while desired_count=0."
  type        = string
  default     = ""
}

variable "github_token_secret_arn" {
  description = "Secrets Manager ARN for GITHUB_TOKEN (scoped PAT from spike 0.7). Wired in dev by #2700 to `judgemind/dispatcher/github-token`; the `github` MCP server reads this env var to authenticate `mcp__github__*` tool calls. Empty-string default stays supported so environments without the secret (e.g. a fresh `staging` / throwaway test stack) can still plan the module; the execution role's `execution_secrets` policy uses `compact()` to drop unset entries."
  type        = string
  default     = ""
}

variable "telegram_bot_token_secret_arn" {
  description = "Secrets Manager ARN for TELEGRAM_BOT_TOKEN (operator paging)."
  type        = string
  default     = ""
}

variable "gemini_api_key_secret_arn" {
  description = "Secrets Manager ARN for GEMINI_API_KEY. Only required if `runner_by_phase` / `runner_shadow` routes to the Gemini runner (see spec Section 14 / spike 0.4)."
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
  description = "Threshold for the heartbeat staleness alarm. Per Section 14, default 300 (5 min)."
  type        = number
  default     = 300
}

variable "stuck_timeout_repeated_window_seconds" {
  description = "CloudWatch alarm period (seconds) for the stuck_timeout_repeated alarm. The alarm fires when at least one stuck_timeout_repeated event is observed in this window. Default 600 (10 min) per Section 15 of the spec."
  type        = number
  default     = 600
}

variable "diagnoser_fallback_window_seconds" {
  description = "CloudWatch alarm period (seconds) for the diagnoser_fallback_spike alarm. Default 1800 (30 min)."
  type        = number
  default     = 1800
}

variable "diagnoser_fallback_threshold" {
  description = "Number of diagnoser_fallback events within diagnoser_fallback_window_seconds that triggers the alarm. Default 2."
  type        = number
  default     = 2
}

variable "scheduler_tick_cadence_threshold_seconds" {
  description = "Threshold (seconds) for the dispatcher-scheduler-tick-cadence-slow alarm. Fires when p95 of TickCadenceSeconds exceeds this value over scheduler_tick_cadence_window_seconds. Default 90 -- well above the 30s healthy cadence and the 60s worst-case ceiling per #2847, so a slip past 90s is unambiguously broken. Distinct from the #3097 stall watchdog (60s WARN / 120s EXIT in the daemon process); this alarm catches a slow-but-running scheduler before it wedges. See #2854."
  type        = number
  default     = 90
}

variable "scheduler_tick_cadence_window_seconds" {
  description = "CloudWatch alarm period (seconds) for the dispatcher-scheduler-tick-cadence-slow alarm. Default 300 (5 min) -- short enough for fast detection during a real cadence regression, long enough that the p95 statistic is robust against single-tick noise. Pairs with scheduler_tick_cadence_threshold_seconds to define the alarm. See #2854."
  type        = number
  default     = 300
}

# ─── Oneshot task launcher (scripts/ecs-run-task.sh) ────────────────────────
# When these ARNs are wired, the daemon's task role gets the permission set
# required by `scripts/ecs-run-task.sh` so ralph phases can launch data
# scripts (rebuild_db, backfills, orphan cleanup, etc.) from the daemon
# context without needing a human to run them on a laptop. See #3048.
#
# Scope: dev-only. Writes stay scoped to the `judgemind-oneshot-${env}`
# task-definition family and the dev cluster; `iam:PassRole` is scoped to
# the ingestion worker's task and execution roles (the roles the oneshot
# task assumes). Leave empty to skip the policy entirely — the daemon's
# self-invocation policy (`task_run_task_self`) is untouched.

variable "oneshot_source_task_role_arn" {
  description = "ARN of the task role the oneshot task definition will assume (typically the ingestion worker / scraper task role). When set alongside oneshot_source_execution_role_arn, the daemon's task role is granted the permission set required by scripts/ecs-run-task.sh. Empty disables the feature."
  type        = string
  default     = ""
}

variable "oneshot_source_execution_role_arn" {
  description = "ARN of the execution role the oneshot task definition will use (typically the shared ECS task execution role). When set alongside oneshot_source_task_role_arn, the daemon's task role is granted the permission set required by scripts/ecs-run-task.sh."
  type        = string
  default     = ""
}

variable "oneshot_maintenance_task_role_arn" {
  description = "ARN of the optional maintenance role that scripts/ecs-run-task.sh --role can swap in. When set, iam:PassRole is also granted for this role so the --role override works from the daemon context."
  type        = string
  default     = ""
}

variable "oneshot_script_bucket_arn" {
  description = "ARN of the S3 bucket where scripts/ecs-run-task.sh uploads scripts larger than the 8KB command-override limit (typically judgemind-assets-<env>). When set, the daemon task role is granted PutObject/DeleteObject under oneshot-scripts/*. Empty disables S3 uploads -- scripts under ~6KB still work inline."
  type        = string
  default     = ""
}

variable "oneshot_ecr_scraper_repository_arn" {
  description = "ARN of the ECR repository that hosts the scraper/ingestion-worker image (used by scripts/ecs-run-task.sh to resolve image digests). When set, the daemon task role is granted ecr:DescribeImages on that repo. Empty disables -- ecs-run-task.sh still works using the service image without the digest-check step."
  type        = string
  default     = ""
}

# ─── Per-agent ECS task launcher (#3091 Stage 2) ────────────────────────────
# Wiring the daemon to `ecs:RunTask` the agent-runner task definition
# (shipped in Stage 1b, #3090) per-agent. When both ARNs below are
# wired, the daemon's task role gains `ecs:RunTask` /
# `ecs:DescribeTasks` / `ecs:StopTask` (scoped to the agent-runner
# task-def family + this cluster) AND `iam:PassRole` (scoped to the
# agent-runner's execution + task roles).
#
# Leave empty to skip the policy entirely — the daemon's self-
# invocation policy (`task_run_task_self`) and oneshot policy
# (`task_run_oneshot`) are untouched. Useful while Stage 2 is still
# inert behind the `agent_execution_mode='subprocess'` default.

variable "agent_runner_task_definition_family" {
  description = "Family name of the dispatcher-agent-runner task definition (e.g. `judgemind-dispatcher-agent-runner-<env>`). Wired from the dispatcher-agent-runner module's `task_definition_family` output. When set alongside the two role ARNs below, the daemon's task role gains ecs:RunTask / ecs:DescribeTasks / ecs:StopTask scoped to this family. Empty disables -- Stage 2 launcher falls back to the subprocess path."
  type        = string
  default     = ""
}

variable "agent_runner_execution_role_arn" {
  description = "ARN of the agent-runner execution role (exported by the dispatcher-agent-runner module as `execution_role_arn`). Passed to the agent-runner task on RunTask, so the daemon's PassRole scope must include it. Empty disables the Stage 2 policy."
  type        = string
  default     = ""
}

variable "agent_runner_task_role_arn" {
  description = "ARN of the agent-runner task role (exported by the dispatcher-agent-runner module as `task_role_arn`). Passed to the agent-runner task on RunTask, so the daemon's PassRole scope must include it. Empty disables the Stage 2 policy."
  type        = string
  default     = ""
}

variable "agent_runner_subnet_ids" {
  description = "Private subnet IDs the agent-runner tasks launch into. Typically the same list as the daemon's `private_subnet_ids`. Threaded into the dispatcher container as AGENT_RUNNER_SUBNET_IDS (comma-separated) for the Stage 2 launcher's network_configuration. Empty list disables the env var -- daemon falls back to subprocess mode."
  type        = list(string)
  default     = []
}

variable "agent_runner_security_group_id" {
  description = "Security group ID attached to agent-runner tasks (exported by the dispatcher-agent-runner module as `security_group_id`). Threaded into the dispatcher container as AGENT_RUNNER_SECURITY_GROUP_ID. Empty disables the env var."
  type        = string
  default     = ""
}

variable "agent_runner_ecr_repository_arn" {
  description = "ARN of the agent-runner ECR repository (judgemind/dispatcher-agent-runner). When set alongside agent_runner_task_definition_family, the daemon's task role is granted ecr:DescribeImages so _check_agent_runner_image_freshness (#3754) can resolve the latest pushed digest. Empty disables the grant -- freshness check fails-open with the existing AccessDenied warning."
  type        = string
  default     = ""
}

# ─── Document-archive S3 census access (#3050) ──────────────────────────────

variable "document_archive_bucket_arn" {
  description = "ARN of the document-archive S3 bucket. When set, the daemon task role is granted `s3:ListBucket` so data-task agents can run census queries (e.g. pre/post rebuild counts). Empty disables the policy -- safe for staging / throwaway stacks."
  type        = string
  default     = ""
}

variable "supervisor_tick_failure_window_seconds" {
  description = "CloudWatch alarm period (seconds) for supervisor-tick failure alarms. With the default 120s tick, a 300s window holds 2-3 ticks -- threshold=2 cleanly separates a transient from a wedge."
  type        = number
  default     = 300
}

variable "supervisor_tick_failure_threshold" {
  description = "Number of supervisor-tick failure events within supervisor_tick_failure_window_seconds that triggers each alarm. Default 2."
  type        = number
  default     = 2
}
