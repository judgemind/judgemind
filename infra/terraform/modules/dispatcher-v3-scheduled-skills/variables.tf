# Variables for the dispatcher-v3 scheduled-skills module (F5, #3890).
#
# Wires four EventBridge cron rules into F2's `scheduled-skill` task
# definition (one per skill: audit, dispatcher-audit,
# dispatcher-daily-report, spotcheck). Each rule fires per its schedule
# expression and `ecs:RunTask`s the scheduled-skill task-def with a
# per-skill `SKILL_NAME` env override -- the in-container entrypoint
# `python -m dispatcher_v3.scheduled_skill_runner` reads SKILL_NAME and
# invokes `claude -p /$SKILL_NAME`. See spec §4.4.
#
# A shared SQS dead-letter queue captures any rule invocation EventBridge
# could not deliver (target IAM error, ECS service unavailability, etc.)
# so silent failures don't accumulate. A CloudWatch alarm on the
# `AWS/Events FailedInvocations` metric pages the operator via SNS
# (closes adversarial-review MAJOR 7).
#
# This module provisions:
#   * 4× aws_cloudwatch_event_rule  (one per skill)
#   * 4× aws_cloudwatch_event_target (ECS RunTask, SKILL_NAME override)
#   * 1× aws_sqs_queue              (shared DLQ)
#   * 1× aws_sqs_queue_policy       (EventBridge sendMessage)
#   * 1× aws_iam_role + 2× policies (EventBridge -> RunTask + PassRole)
#   * 1× aws_cloudwatch_metric_alarm (FailedInvocations across rules)
#
# Cohabitation with v2: v2's daemon already runs these scheduled skills
# via its in-process scheduler. During cohabitation both can fire -- the
# skills are idempotent (file-issue dedup, GH Actions concurrency). Once
# v3 is the sole operator the v2 schedules will be retired.

variable "environment" {
  description = "Deployment environment. v3 scheduled skills are dev-only at first land -- staging and production are human-operated and have no v3 footprint (per spec section 10)."
  type        = string

  validation {
    condition     = contains(["dev"], var.environment)
    error_message = "environment must be: dev (dispatcher-v3 scheduled skills are dev-only at first land per spec §10)."
  }
}

variable "name_prefix" {
  description = "Prefix shared by every EventBridge rule name. Default `dispatcher-v3` matches the issue body's verification command (`aws events list-rules --name-prefix dispatcher-v3-`)."
  type        = string
  default     = "dispatcher-v3"

  validation {
    condition     = length(var.name_prefix) > 0
    error_message = "name_prefix must be non-empty -- the issue body's verification step relies on `aws events list-rules --name-prefix <prefix>-` returning the four rules."
  }
}

# --- ECS target -------------------------------------------------------

variable "ecs_cluster_arn" {
  description = "ARN of the ECS cluster the scheduled-skill task-def runs on (same cluster the launcher / task-runner / diagnoser run on)."
  type        = string
}

variable "scheduled_skill_task_definition_arn" {
  description = "ARN of the F2 scheduled-skill ECS task definition (latest revision). EventBridge targets this with a per-rule `SKILL_NAME` env override. Source: `module.dispatcher_v3_task_defs.scheduled_skill_task_definition_arn`."
  type        = string
}

variable "scheduled_skill_task_definition_family" {
  description = "Family name of the F2 scheduled-skill task definition (e.g. `judgemind-dispatcher-v3-scheduled-skill`). Used to scope the EventBridge invoker role's `ecs:RunTask` Resource list to the family wildcard (revision-agnostic) so apply-time revision bumps don't require role updates. Source: `module.dispatcher_v3_task_defs.scheduled_skill_task_definition_family`."
  type        = string
}

variable "task_subnet_ids" {
  description = "Private-subnet IDs the scheduled-skill ECS task ENIs are placed in. Reuse the launcher/task-runner private subnets so dev network scope stays aligned."
  type        = list(string)

  validation {
    condition     = length(var.task_subnet_ids) > 0
    error_message = "task_subnet_ids must contain at least one subnet ID -- ecs:RunTask requires a non-empty `awsvpcConfiguration.subnets`."
  }
}

variable "task_security_group_id" {
  description = "Security group ID attached to the scheduled-skill ECS task ENI. Reuse the v3 agent-runner SG (env-layer) so scheduled skills share the launcher's outbound posture (HTTPS / Postgres / Redis)."
  type        = string

  validation {
    condition     = length(var.task_security_group_id) > 0
    error_message = "task_security_group_id must be non-empty."
  }
}

# --- IAM roles to PassRole at ECS RunTask -----------------------------

variable "execution_role_arn" {
  description = "ARN of the F3 shared dispatcher-v3 execution role (`module.dispatcher_v3_iam.execution_role_arn`). Threaded into the EventBridge invoker role's PassRole policy so RunTask can pass it to the ECS agent."
  type        = string
}

variable "agent_task_role_arn" {
  description = "ARN of the F3 dispatcher-v3 agent task role (`module.dispatcher_v3_iam.agent_task_role_arn`). Threaded into the EventBridge invoker role's PassRole policy so RunTask can pass it as the in-container task role."
  type        = string
}

# --- Schedule expressions (per spec §4.4 + issue body) ----------------
#
# All four rules use EventBridge Rules cron syntax (NOT EventBridge
# Scheduler syntax -- those are different APIs). Cron expressions
# require a six-field form: minute hour day-of-month month day-of-week
# year. `?` matches every value; "?" is required in either day-of-month
# or day-of-week per AWS docs.
#
# Defaults:
#   audit                    -- rate(6 hours). Issue body acknowledges
#                               this is a rough proxy for "every 20
#                               PRs"; the in-task entrypoint compares
#                               last_audit_pr_merged_at against the
#                               merge count and skips if below the
#                               threshold.
#   dispatcher-audit         -- rate(6 hours).
#   dispatcher-daily-report  -- daily 12:00 UTC.
#   spotcheck                -- weekly Monday 18:00 UTC.

variable "audit_schedule_expression" {
  description = "Schedule expression for the `audit` rule. Default `rate(6 hours)` per issue body."
  type        = string
  default     = "rate(6 hours)"
}

variable "dispatcher_audit_schedule_expression" {
  description = "Schedule expression for the `dispatcher-audit` rule. Default `rate(6 hours)` per issue body."
  type        = string
  default     = "rate(6 hours)"
}

variable "dispatcher_daily_report_schedule_expression" {
  description = "Schedule expression for the `dispatcher-daily-report` rule. Default `cron(0 12 * * ? *)` (daily at 12:00 UTC) per issue body and the daemon-side cadence in `.claude/skills/dispatcher-daily-report/SKILL.md`."
  type        = string
  default     = "cron(0 12 * * ? *)"
}

variable "spotcheck_schedule_expression" {
  description = "Schedule expression for the `spotcheck` rule. Default `cron(0 18 ? * MON *)` (weekly Monday 18:00 UTC) per issue body."
  type        = string
  default     = "cron(0 18 ? * MON *)"
}

variable "schedule_state_enabled" {
  description = "Whether the four EventBridge rules are ENABLED at apply time. Set `false` during a soak period or rollback to land the resources without firing them. Default `true`."
  type        = bool
  default     = true
}

# --- Alarm wiring -----------------------------------------------------

variable "enable_alerts" {
  description = "Whether to provision the `<prefix>-eventbridge-failures` CloudWatch alarm on the `AWS/Events FailedInvocations` metric. Set `false` to skip the alarm wiring (e.g. in fixtures)."
  type        = bool
  default     = true
}

variable "alert_sns_topic_arn" {
  description = "SNS topic ARN the alarm publishes to on FailedInvocations. Source: `module.compute.alerts_topic_arn` (existing dev SNS topic that fans out to email and the operator's Telegram pipeline). Required when `enable_alerts = true`."
  type        = string
  default     = ""
}

variable "failed_invocations_threshold" {
  description = "Threshold for the FailedInvocations alarm (alarm fires when the per-period sum strictly exceeds this value)."
  type        = number
  default     = 0

  validation {
    condition     = var.failed_invocations_threshold >= 0
    error_message = "failed_invocations_threshold must be >= 0 -- FailedInvocations is a non-negative count metric."
  }
}

variable "failed_invocations_period_seconds" {
  description = "Evaluation period for the FailedInvocations alarm in seconds. Default 300 (5 minutes) -- short enough to catch a wedge inside one cron cycle, long enough to dedup transient ECS service blips."
  type        = number
  default     = 300
}

variable "failed_invocations_evaluation_periods" {
  description = "Number of consecutive evaluation periods that must breach before the alarm fires. Default 1 -- any failed invocation should be visible immediately (the spec adversarial-review's MAJOR 7 explicitly calls out silent skill failures)."
  type        = number
  default     = 1
}

# --- DLQ retention ----------------------------------------------------

variable "dlq_message_retention_seconds" {
  description = "SQS DLQ message retention in seconds. Default 1209600 (14 days = SQS maximum) -- failed invocations are rare; we want enough retention for an operator to investigate after a weekend."
  type        = number
  default     = 1209600 # 14 days

  validation {
    condition     = var.dlq_message_retention_seconds >= 60 && var.dlq_message_retention_seconds <= 1209600
    error_message = "dlq_message_retention_seconds must be in [60, 1209600] -- SQS platform limits."
  }
}
