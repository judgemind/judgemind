# Variables for the dispatcher-v3 task-defs module (F2, #3887).
#
# This module registers the four ECS task definitions of the v3
# dispatcher pipeline and one CloudWatch log group per task-def. All
# four task-defs are baked from the same F1 image (#3915,
# `judgemind/dispatcher-v3`) with different `command` argv selecting
# the per-task entrypoint inside the container.
#
# F1 (#3915) ships the image + ECR repo. F3 (#3921) ships the IAM
# roles consumed below. F4 (follow-up) stands up the launcher ECS
# service and references the launcher task-def family from this
# module's outputs. F5 (follow-up) wires EventBridge schedules at the
# scheduled-skill task-def.
#
# Image digest pinning: this module resolves an image digest at apply
# time using `data "aws_ecr_image"` keyed by `image_tag` (default
# `latest`) and bakes `repository_url@sha256:<digest>` into every
# rendered task-def. Using the mutable `:latest` tag directly was the
# source of the v2 image-staleness drift documented in #3754; the
# digest pin makes the task-def reference immutable for the lifetime
# of that revision and forces every redeploy to land a fresh
# revision. See `docs/specs/dispatcher-v3-spec.md` §4 + §6.

variable "environment" {
  description = "Deployment environment. v3 task-defs are dev-only at first land -- staging and production are human-operated and have no v3 footprint."
  type        = string

  validation {
    condition     = contains(["dev"], var.environment)
    error_message = "environment must be: dev (dispatcher-v3 task-defs are dev-only at first land; staging/production are human-operated per spec section 10)."
  }
}

variable "task_definition_family_prefix" {
  description = "Family-name prefix shared by every v3 task definition. The four families registered are <prefix>-launcher, <prefix>-task-runner, <prefix>-diagnoser, and <prefix>-scheduled-skill. Default judgemind-dispatcher-v3 matches the F3 IAM module's expected agent task-def families and the spec section 4 task-def names."
  type        = string
  default     = "judgemind-dispatcher-v3"
}

# --- Image (digest-pinned) ---------------------------------------------

variable "ecr_repository_name" {
  description = "ECR repository name (without registry / account prefix) for the F1 v3 image -- e.g. judgemind/dispatcher-v3. Used by data.aws_ecr_image to resolve the digest of `image_tag` at apply time."
  type        = string
  default     = "judgemind/dispatcher-v3"
}

variable "ecr_repository_url" {
  description = "ECR repository URL for the F1 v3 image -- the <acct>.dkr.ecr.<region>.amazonaws.com/<repo> prefix without a tag. Combined with the resolved digest at apply time to produce the pinned `image` reference baked into every rendered task-def."
  type        = string
}

variable "image_tag" {
  description = "ECR tag to resolve to a digest at apply time. Defaults to `latest`, which the deploy-dispatcher-v3 workflow re-points to the newest <sha7> image on every push to main. Apply runs after a deploy will resolve `latest` to that <sha7>'s digest and bake the digest into the rendered task-defs -- see issue body's image-digest-pinning AC."
  type        = string
  default     = "latest"
}

# --- Roles (from F3, #3921) --------------------------------------------

variable "launcher_role_arn" {
  description = "ARN of the dispatcher-v3 launcher task role (F3 output `launcher_role_arn`). Wired into the launcher task definition only -- the three short-lived task-defs use `agent_task_role_arn` instead."
  type        = string
}

variable "agent_task_role_arn" {
  description = "ARN of the dispatcher-v3 agent task role (F3 output `agent_task_role_arn`). Shared by task-runner / diagnoser / scheduled-skill task-defs. Dev-admin equivalent per spec section 10."
  type        = string
}

variable "execution_role_arn" {
  description = "ARN of the shared dispatcher-v3 task-execution role (F3 output `execution_role_arn`). Used by the ECS agent to pull the F1 image from ECR, inject Secrets Manager values into env vars, and write logs to CloudWatch. Same execution role for every task-def in this module."
  type        = string
}

# --- Secrets -----------------------------------------------------------

variable "anthropic_api_key_secret_arn" {
  description = "Secrets Manager ARN for ANTHROPIC_API_KEY. Required by task-runner / diagnoser / scheduled-skill (they invoke `claude -p`). Empty disables -- the secret is omitted from the rendered container_definitions for that task-def."
  type        = string
  default     = ""
}

variable "db_connection_secret_arn" {
  description = "Secrets Manager ARN for the dispatcher-role DATABASE_URL JSON secret (key: `url`). The launcher reads dispatcher.* state from this; task-runner / diagnoser / scheduled-skill may also read it for `progress.sh` calls and outcome_summary writes."
  type        = string
  default     = ""
}

variable "github_token_secret_arn" {
  description = "Secrets Manager ARN for the v3 dispatcher's scoped GitHub PAT (re-used from v2 -- judgemind/dispatcher/github-token). Threaded through to task-runner and diagnoser task-defs for `gh auth setup-git` inside the agent."
  type        = string
  default     = ""
}

variable "telegram_bot_token_secret_arn" {
  description = "Secrets Manager ARN for the launcher's Telegram bot token. Wired into the launcher task-def only -- used to page the operator on a stuck queue. Empty disables."
  type        = string
  default     = ""
}

# --- Repo + AWS context ------------------------------------------------

variable "github_repo" {
  description = "Target GitHub repo (owner/name) the dispatcher operates on. Exposed to every container as GITHUB_REPO."
  type        = string
  default     = "judgemind/judgemind"
}

variable "repo_url" {
  description = "HTTPS clone URL for the target repository. The task-runner / diagnoser entrypoints clone this at boot when running outside a pre-baked worktree."
  type        = string
  default     = "https://github.com/judgemind/judgemind.git"
}

variable "ecs_cluster_arn" {
  description = "ARN of the ECS cluster the v3 task-defs run on. Exposed to the launcher container as ECS_CLUSTER_ARN so its scheduler tick can call ecs:RunTask without re-resolving the cluster from environment metadata."
  type        = string
}

# --- Launcher → task-runner network handoff (#3939) --------------------
#
# The launcher container's `_build_launcher_from_env` (scripts/dispatcher_v3/
# launcher.py:1496-1526) reads three env vars at boot to build an
# `ecs:RunTask` request for each claimed issue:
#
#   * TASK_RUNNER_TASK_DEFINITION  -- which task-def family to launch.
#     Always the task-runner family this same module registers, so we
#     wire `local.family_task_runner` directly into the launcher's
#     environment block (no variable needed -- a self-reference would
#     just propagate a value that's already locally available).
#   * AGENT_RUNNER_SUBNET_IDS  -- comma-joined private-subnet IDs the
#     RunTask network configuration places task-runner ENIs in.
#   * AGENT_RUNNER_SECURITY_GROUP_ID  -- the security group to attach to
#     each task-runner ENI. Provisioned at the env layer (not inside F2
#     or F4) to avoid the F2 ↔ F4 module cycle that would arise if F2
#     read it from F4's outputs while F4 already references F2's
#     launcher task-def ARN. Egress profile mirrors F4's launcher SG
#     (HTTPS / Postgres / Redis), so task-runners share the launcher's
#     network posture. If task-runners ever need a different egress
#     posture (e.g. tighter scoping to S3 + GitHub + Anthropic only),
#     the env-layer SG can be tightened without changing this module.
#
# Both are required (no default empty-string) because the launcher
# crashes loudly with KeyError on `os.environ["AGENT_RUNNER_SECURITY_GROUP_ID"]`
# at boot if either is absent -- there's no "disabled" mode for a
# launcher that can't actually launch task-runners.

variable "agent_runner_subnet_ids" {
  description = "Private-subnet IDs the launcher places each task-runner's ENI in via ecs:RunTask `awsvpcConfiguration.subnets`. Threaded into the launcher container as AGENT_RUNNER_SUBNET_IDS (comma-joined). Reuses the same private subnets as the launcher's own ECS service so dev/prod network scopes stay aligned."
  type        = list(string)

  validation {
    condition     = length(var.agent_runner_subnet_ids) > 0
    error_message = "agent_runner_subnet_ids must contain at least one subnet ID -- the launcher's ecs:RunTask call requires a non-empty awsvpcConfiguration.subnets list."
  }
}

variable "agent_runner_security_group_id" {
  description = "Security group ID attached to every task-runner ENI launched via ecs:RunTask. Threaded into the launcher container as AGENT_RUNNER_SECURITY_GROUP_ID. Provisioned at the env layer to avoid an F2 ↔ F4 module cycle; egress profile mirrors F4's launcher SG (HTTPS / Postgres / Redis) so task-runners share the launcher's network posture."
  type        = string

  validation {
    condition     = length(var.agent_runner_security_group_id) > 0
    error_message = "agent_runner_security_group_id must be non-empty -- the launcher's _build_launcher_from_env raises KeyError on os.environ['AGENT_RUNNER_SECURITY_GROUP_ID'] at boot."
  }
}

variable "sessions_bucket_name" {
  description = "Name of the v3 sessions S3 bucket (F6, #3893). Exposed to task-runner / diagnoser containers as SESSIONS_BUCKET so the entrypoint can archive session logs on exit. Empty disables -- the entrypoint's upload step short-circuits when SESSIONS_BUCKET is unset."
  type        = string
  default     = ""
}

# --- Sizing (per-task-def overrides) -----------------------------------
#
# Defaults match the spec section 4 + issue body sizing. The task-runner
# default (4 vCPU / 16 GiB) matches the v2 agent-runner envelope (#3153
# baseline). The launcher is much smaller because the scheduler tick is
# shell-light. The diagnoser and scheduled-skill sit between the two.
#
# Fargate valid CPU/memory pairs are enforced by AWS at register-time;
# the variable defaults below are pre-validated pairs.

variable "launcher_cpu" {
  description = "CPU units for the launcher task. Default 1024 (1 vCPU) per issue body."
  type        = number
  default     = 1024
}

variable "launcher_memory" {
  description = "Memory MiB for the launcher task. Default 2048 (2 GiB) per issue body. Fargate valid pair: 1024 / 2048."
  type        = number
  default     = 2048
}

variable "task_runner_cpu" {
  description = "CPU units for the task-runner task. Default 4096 (4 vCPU) per issue body -- matches v2 agent-runner envelope #3153."
  type        = number
  default     = 4096
}

variable "task_runner_memory" {
  description = "Memory MiB for the task-runner task. Default 16384 (16 GiB) per issue body. Fargate valid pair: 4096 / 16384."
  type        = number
  default     = 16384
}

variable "diagnoser_cpu" {
  description = "CPU units for the diagnoser task. Default 2048 (2 vCPU) per issue body."
  type        = number
  default     = 2048
}

variable "diagnoser_memory" {
  description = "Memory MiB for the diagnoser task. Default 8192 (8 GiB) per issue body. Fargate valid pair: 2048 / 8192."
  type        = number
  default     = 8192
}

variable "scheduled_skill_cpu" {
  description = "CPU units for the scheduled-skill task. Default 2048 (2 vCPU) per issue body."
  type        = number
  default     = 2048
}

variable "scheduled_skill_memory" {
  description = "Memory MiB for the scheduled-skill task. Default 8192 (8 GiB) per issue body."
  type        = number
  default     = 8192
}

# --- stopTimeout caps --------------------------------------------------
#
# Wall-clock guard rails per spec section 4 + issue body. Exceeding the
# cap triggers ecs:StopTask from the launcher's silent-hang detector
# (spec section 4.1). Fargate enforces a platform-level stopTimeout cap
# of 120s on the SIGTERM-to-SIGKILL grace window, but the long values
# below are propagated as task-level metadata read by the launcher when
# computing its own per-agent timeout (`silent_hang_minutes` for
# launcher-side StopTask). The container itself is not directly killed
# by ECS at the cap -- the launcher is the source of truth for "this
# task has run too long."
#
# We expose them as variables so the spec section 4 caps stay
# observable at the call site (environments/dev/main.tf) without
# spelunking into the module body.

variable "task_runner_stop_timeout_seconds" {
  description = "Wall-clock cap for a task-runner ECS task in seconds. Default 21600 (6h) per issue body. The launcher's silent-hang detector reads this from the task-def metadata and StopTask's any task-runner that exceeds it."
  type        = number
  default     = 21600
}

variable "diagnoser_stop_timeout_seconds" {
  description = "Wall-clock cap for a diagnoser ECS task in seconds. Default 3600 (1h) per issue body -- the diagnoser is a one-shot read + side-effect skill that should not run long."
  type        = number
  default     = 3600
}

variable "scheduled_skill_stop_timeout_seconds" {
  description = "Wall-clock cap for a scheduled-skill ECS task in seconds. Default 7200 (2h) per issue body."
  type        = number
  default     = 7200
}

# --- Ephemeral storage / log retention ---------------------------------

variable "task_runner_ephemeral_storage_gib" {
  description = "Ephemeral storage GiB for task-runner tasks. Per-agent worktrees plus Claude transcripts plus pip/npm caches sit comfortably in 30 GiB."
  type        = number
  default     = 30

  validation {
    condition     = var.task_runner_ephemeral_storage_gib >= 21 && var.task_runner_ephemeral_storage_gib <= 200
    error_message = "task_runner_ephemeral_storage_gib must be in [21, 200] -- Fargate platform limits."
  }
}

variable "diagnoser_ephemeral_storage_gib" {
  description = "Ephemeral storage GiB for diagnoser tasks. The diagnoser pulls a session transcript from S3 and runs claude -p -- comfortably fits in the Fargate floor."
  type        = number
  default     = 21

  validation {
    condition     = var.diagnoser_ephemeral_storage_gib >= 21 && var.diagnoser_ephemeral_storage_gib <= 200
    error_message = "diagnoser_ephemeral_storage_gib must be in [21, 200] -- Fargate platform limits."
  }
}

variable "scheduled_skill_ephemeral_storage_gib" {
  description = "Ephemeral storage GiB for scheduled-skill tasks. Each skill clones the repo + may run pytest fixtures locally; 30 GiB matches task-runner."
  type        = number
  default     = 30

  validation {
    condition     = var.scheduled_skill_ephemeral_storage_gib >= 21 && var.scheduled_skill_ephemeral_storage_gib <= 200
    error_message = "scheduled_skill_ephemeral_storage_gib must be in [21, 200] -- Fargate platform limits."
  }
}

variable "log_retention_days" {
  description = "Number of days to retain CloudWatch log events for every v3 log group (one per task-def family)."
  type        = number
  default     = 30
}
