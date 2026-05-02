# Variables for the dispatcher-v3 launcher ECS service module (F4, #3889).
#
# This module stands up the long-running ECS Fargate service that runs
# the v3 launcher loop. It deliberately does NOT create the task
# definition, IAM roles, ECR repo, log group, or sessions bucket --
# those land in F1 (#3915), F2 (#3887), F3 (#3921), and F6 (#3891)
# respectively. F4's only job is the service + the security group it
# attaches to.
#
# See `docs/specs/dispatcher-v3-spec.md` section 6 for the architecture
# overview and section 9 (cutover step 2) for the deploy-at-cap=0
# rationale: the ECS service deploys with desired_count=1 so the
# launcher container is RUNNING and writing heartbeat ticks, but the
# `dispatcher.config.concurrency_cap_v3` value is 0 -- the launcher
# observes the queue without claiming any issue. The operator manually
# flips the cap to 1+ to start v3 work (spec section 9 step 3).

variable "environment" {
  description = "Deployment environment. v3 is dev-only at first land -- staging and production are human-operated and have no v3 footprint (spec section 10). The default service name is `judgemind-dispatcher-v3-<environment>`."
  type        = string

  validation {
    condition     = contains(["dev"], var.environment)
    error_message = "environment must be: dev (dispatcher-v3 service is dev-only at first land)."
  }
}

variable "service_name" {
  description = "Name of the ECS service. Default empty string -- the module substitutes `judgemind-dispatcher-v3-<environment>` which matches the spec section 6 service name (`judgemind-dispatcher-v3-dev`) referenced by #3754's image-staleness defense and the cohabitation log filters. Override only if a non-default name is needed for migration or testing."
  type        = string
  default     = ""
}

variable "vpc_id" {
  description = "ID of the VPC the launcher task runs in. Same VPC as v2's dispatcher daemon -- same egress-only access requirements (HTTPS to GitHub / Anthropic / ECR / Secrets Manager / Telegram, Postgres for dispatcher.* state). No inbound."
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for the Fargate task ENI. Same subnets as v2's dispatcher daemon -- both daemons need NAT egress for GitHub/Anthropic/Telegram and Postgres connectivity."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) > 0
    error_message = "private_subnet_ids must be non-empty -- the launcher needs at least one subnet to schedule its task ENI."
  }
}

variable "ecs_cluster_arn" {
  description = "ARN of the ECS cluster the launcher service runs in. Same cluster as v2's dispatcher daemon (`judgemind-dev`) so the launcher's `ecs:RunTask` calls (gated on this cluster ARN by F3's launcher_role policy) target the correct cluster."
  type        = string
}

# --- Task definition (from F2, #3887) ---------------------------------

variable "task_definition_arn" {
  description = "Full ARN (with revision) of the v3 launcher task definition exported by F2 as `launcher_task_definition_arn`. Used at apply time only -- the service's `lifecycle.ignore_changes = [task_definition]` block keeps later F2 reapplies (which register fresh revisions whenever the image digest changes) from forcing a service-level redeploy. Operator-driven redeploys go through `aws ecs update-service --force-new-deployment` instead."
  type        = string
}

# --- Service sizing ----------------------------------------------------

variable "desired_count" {
  description = "Number of launcher task replicas. v3 is a singleton -- one replica only. Default 1 (deploy at cap=0 means the launcher is RUNNING and writing heartbeats, but the in-container cap read keeps it from claiming any issue until the operator flips dispatcher.config.concurrency_cap_v3)."
  type        = number
  default     = 1

  validation {
    condition     = var.desired_count >= 0 && var.desired_count <= 1
    error_message = "desired_count must be 0 or 1 -- the launcher is a singleton; overlapping replicas would double-claim issues."
  }
}

# --- ECS Exec ---------------------------------------------------------

variable "enable_execute_command" {
  description = "Whether to enable `aws ecs execute-command` on the launcher service for ad-hoc operator debugging (psql, log tail, etc.). The agent task role (F3) carries the SSM exec permissions; the service-level flag is what wires them together. Default true to mirror v2 dispatcher-daemon."
  type        = bool
  default     = true
}

# --- Deployment behaviour ---------------------------------------------

variable "deployment_minimum_healthy_percent" {
  description = "Min healthy percent during deploys. The launcher is a singleton; overlapping deploys would briefly double-claim. Default 0 means the old task may stop before the new one is healthy -- pairs with deployment_maximum_percent=100 (so a new task can start, but only one runs at a time during the swap window)."
  type        = number
  default     = 0
}

variable "deployment_maximum_percent" {
  description = "Max percent during deploys. Default 100 means the new task starts before the old one stops (then ECS drains the old). The 0/100 pair gives ECS a momentary singleton invariant violation only during the swap; without ignore_changes on task_definition this would constantly trigger swaps on every F2 reapply."
  type        = number
  default     = 100
}
