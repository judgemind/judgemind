# Variables for the dispatcher-v3 IAM module.
#
# This module ships two task roles plus a shared task-execution role:
#
#   * `launcher_role` — narrow scheduler-only scope. Assumed by the
#     long-running `launcher` task definition shipped in F2 (#3887).
#   * `agent_task_role` — dev-admin equivalent. Assumed by the
#     short-lived `task-runner`, `diagnoser`, and `scheduled-skill`
#     task definitions shipped in F2 (#3887).
#   * `execution_role` — shared by every v3 task definition. Pulls ECR
#     images, fetches Secrets Manager entries for env-var injection,
#     writes logs to CloudWatch.
#
# F2 wires the role ARNs into the matching task definitions; F4 stands
# up the launcher ECS service. This module deliberately does NOT
# create any task definitions, services, or log groups — those land
# alongside their owning modules.
#
# See `docs/specs/dispatcher-v3-spec.md` §10 (IAM + secrets) for the
# rationale behind the broad `agent_task_role` scope.

variable "environment" {
  description = "Deployment environment. v3 IAM is dev-only — staging and production are human-operated and have no agent task role."
  type        = string

  validation {
    condition     = contains(["dev"], var.environment)
    error_message = "environment must be: dev (dispatcher-v3 IAM is dev-only; staging/production are human-operated per spec §10)."
  }
}

variable "aws_region" {
  description = "AWS region the v3 stack runs in. Used to construct scoped resource ARNs in IAM policies."
  type        = string
  default     = "us-west-2"
}

variable "aws_account_id" {
  description = "AWS account ID for the dev environment. Used to construct scoped resource ARNs in IAM policies. Trust policies on both task roles deliberately do NOT reference any other account — production has no v3 footprint."
  type        = string
  default     = "155326049300"
}

variable "ecs_cluster_arn" {
  description = "ARN of the ECS cluster the v3 task definitions run on. The launcher's `ecs:RunTask` policy is gated on this cluster ARN; the agent task role's `ecs:RunTask` and `ecs:ExecuteCommand` are gated on the same cluster."
  type        = string
}

variable "task_definition_family_prefix" {
  description = "Family-name prefix of the v3 task definitions registered by F2. The launcher's `ecs:RunTask` permission is scoped to `<prefix>-task-runner:*`, `<prefix>-diagnoser:*`, and `<prefix>-scheduled-skill:*` revisions of this family. Default `judgemind-dispatcher-v3` matches the F2 spec."
  type        = string
  default     = "judgemind-dispatcher-v3"
}

# ─── Secrets ─────────────────────────────────────────────────────────────
# Two ARNs only — the rest are read by the broad agent task role via the
# `judgemind/*` and `*-dev-*` resource patterns, no per-secret wiring
# needed.
#
# The launcher role gets a single Secrets Manager grant: TELEGRAM_BOT_TOKEN.
# That's all the launcher needs — paging the operator on a stuck queue is
# the only secret-bearing path on the scheduler.

variable "telegram_bot_token_secret_arn" {
  description = "Secrets Manager ARN for TELEGRAM_BOT_TOKEN. The launcher role's only secret-read permission is scoped to this ARN. Empty disables — the launcher's `secretsmanager:GetSecretValue` policy resource is skipped entirely so a fresh stack without telegram wiring can still apply."
  type        = string
  default     = ""
}

variable "github_token_secret_arn" {
  description = "Secrets Manager ARN for the v3 dispatcher's scoped GitHub PAT (re-used from v2 — `judgemind/dispatcher/github-token`). Threaded through to F2's task-runner and diagnoser task definitions for `gh auth setup-git` inside the agent. The launcher does not read this secret (its work is gh-API-via-PAT only via the `gh` MCP server in the agent task)."
  type        = string
  default     = ""
}

# ─── S3 buckets ─────────────────────────────────────────────────────────
# The agent task role gets full S3 because dev `/task` runs may touch
# raw/ (S3-as-source-of-truth, see CLAUDE.md §Project Context), spotcheck/
# scratch areas, and the v3 sessions/ prefix (F6, #3893). We accept full
# S3 in dev — the trade-off is documented in spec §10. Production
# accounts are not in scope.
#
# The launcher does NOT get S3 access at all (it never reads or writes
# objects).

variable "sessions_bucket_arn" {
  description = "ARN of the v3 sessions bucket where session-log streaming writes per-agent jsonl. Reserved for forward compatibility with F6 (#3893) — the agent task role's full-S3 grant already covers writes to any bucket, but we list the bucket here for documentation and future scope-narrowing without breaking the spec §10 dev-admin invariant."
  type        = string
  default     = ""
}

# ─── Trust policy guard (defense in depth) ──────────────────────────────
# The trust policy on every role created by this module pins `Principal =
# Service: ecs-tasks.amazonaws.com` and pins the source account. There is
# no `sts:AssumeRole` from any cross-account principal anywhere in the
# module. This variable exists so a CI test fixture can assert the
# invariant without re-implementing it.

variable "prod_account_ids" {
  description = "List of production AWS account IDs that must NEVER appear in any v3 trust policy. Used by `tests/policy-checks/check.sh` as a regression guard — see spec §10 (`Production accounts are not in scope. The trust policy on the agent task role excludes assuming any prod-account role`). Empty list disables the check; default is empty because there is no production AWS account in the Judgemind footprint at the time of writing."
  type        = list(string)
  default     = []
}
