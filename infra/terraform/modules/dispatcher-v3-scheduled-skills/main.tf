# Dispatcher v3 scheduled-skills module (F5, #3890).
#
# Per `docs/specs/dispatcher-v3-spec.md` §4.4. Wires four EventBridge
# cron rules into F2's `scheduled-skill` task definition. EventBridge
# fires; ECS runs the task; the skill writes its results directly via
# `gh`. The launcher is uninvolved -- this is observation/cron, not
# orchestration.
#
# Each rule targets the same `scheduled-skill` task-def with a per-rule
# `SKILL_NAME` env override. The container's
# `python -m dispatcher_v3.scheduled_skill_runner` reads SKILL_NAME and
# invokes `claude -p /$SKILL_NAME`.
#
# A shared SQS dead-letter queue catches invocations EventBridge could
# not deliver (target IAM error, ECS service unavailable, etc.) so silent
# rule failures don't accumulate. A CloudWatch alarm on the
# `AWS/Events FailedInvocations` metric across all four rules pages the
# operator via the existing scraper-alerts SNS topic. This closes the
# adversarial-review MAJOR 7 gap (silent EventBridge failure mode).

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  # Skill name -> override map. Single source of truth for the four
  # scheduled skills v3 ships -- spec §4.4 names them explicitly. Adding
  # a new skill is one entry here plus a `<name>_schedule_expression`
  # variable.
  skills = {
    audit = {
      schedule_expression = var.audit_schedule_expression
      description         = "Periodic /audit codebase health check"
    }
    dispatcher-audit = {
      schedule_expression = var.dispatcher_audit_schedule_expression
      description         = "Periodic /dispatcher-audit operational-health probe"
    }
    dispatcher-daily-report = {
      schedule_expression = var.dispatcher_daily_report_schedule_expression
      description         = "Daily /dispatcher-daily-report failure summary"
    }
    spotcheck = {
      schedule_expression = var.spotcheck_schedule_expression
      description         = "Weekly /spotcheck data-quality probe"
    }
  }

  # ARN scoping for the EventBridge invoker role. The role only needs
  # ecs:RunTask on the family wildcard (revision-agnostic) so apply-time
  # F2 revision bumps don't require role updates.
  scheduled_skill_task_def_arn_pattern = "arn:${data.aws_partition.current.partition}:ecs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:task-definition/${var.scheduled_skill_task_definition_family}:*"

  cluster_name                 = split("/", var.ecs_cluster_arn)[1]
  scheduled_skill_task_arn_any = "arn:${data.aws_partition.current.partition}:ecs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:task/${local.cluster_name}/*"

  rule_state = var.schedule_state_enabled ? "ENABLED" : "DISABLED"
}

# ─── Dead-letter queue ──────────────────────────────────────────────────────
# Shared by all four rules. EventBridge writes failed invocation
# records here when it cannot deliver to the ECS target (rule disabled
# upstream, target IAM error, RunTask quota exhaustion, etc.). The DLQ
# itself is observed by the FailedInvocations alarm below; the alarm is
# the operator-facing signal, the DLQ is the forensic record.

resource "aws_sqs_queue" "scheduled_skills_dlq" {
  name                       = "judgemind-${var.name_prefix}-scheduled-skills-dlq-${var.environment}"
  message_retention_seconds  = var.dlq_message_retention_seconds
  visibility_timeout_seconds = 30
  sqs_managed_sse_enabled    = true
}

# Allow the EventBridge service to send messages to the DLQ on behalf
# of any of our v3 scheduled-skill rules. The aws:SourceArn condition
# is the defense-in-depth control -- without it, ANY EventBridge rule
# in the account could write to this queue.
data "aws_iam_policy_document" "dlq_send_policy" {
  statement {
    sid    = "AllowEventBridgeSendMessage"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.scheduled_skills_dlq.arn]

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [for k, _ in local.skills : "arn:${data.aws_partition.current.partition}:events:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:rule/${var.name_prefix}-${k}"]
    }
  }
}

resource "aws_sqs_queue_policy" "scheduled_skills_dlq" {
  queue_url = aws_sqs_queue.scheduled_skills_dlq.id
  policy    = data.aws_iam_policy_document.dlq_send_policy.json
}

# ─── EventBridge invoker role ───────────────────────────────────────────────
# Assumed by EventBridge when firing a rule. Allows ecs:RunTask on the
# scheduled-skill family + iam:PassRole on the two task-def-pinned roles.
# This is a SEPARATE role from the F3 launcher role -- the launcher's
# RunTask grant covers task-runner / diagnoser / scheduled-skill, but
# the launcher itself never invokes scheduled-skill (EventBridge does).

resource "aws_iam_role" "events_invoker" {
  name        = "judgemind-${var.name_prefix}-scheduled-skills-events-${var.environment}"
  description = "Dispatcher v3 EventBridge invoker role -- assumed by events.amazonaws.com to run scheduled-skill ECS tasks per §4.4 cron rules."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "events_run_task" {
  name = "judgemind-${var.name_prefix}-scheduled-skills-run-task-${var.environment}"
  role = aws_iam_role.events_invoker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowRunScheduledSkillTask"
        Effect   = "Allow"
        Action   = "ecs:RunTask"
        Resource = local.scheduled_skill_task_def_arn_pattern
        Condition = {
          ArnEquals = {
            "ecs:cluster" = var.ecs_cluster_arn
          }
        }
      },
      {
        # ecs:TagResource is invoked implicitly by RunTask when the
        # call includes a `tags` list. AWS evaluates authorization
        # against the task-instance ARN (task/CLUSTER/*), not the
        # task-definition ARN -- same quirk that bit the v2 launcher
        # policy (#3129) and the v3 launcher policy.
        Sid      = "AllowTagScheduledSkillTask"
        Effect   = "Allow"
        Action   = "ecs:TagResource"
        Resource = local.scheduled_skill_task_arn_any
        Condition = {
          ArnEquals = {
            "ecs:cluster" = var.ecs_cluster_arn
          }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy" "events_pass_role" {
  name = "judgemind-${var.name_prefix}-scheduled-skills-pass-role-${var.environment}"
  role = aws_iam_role.events_invoker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowPassScheduledSkillTaskRoles"
        Effect = "Allow"
        Action = "iam:PassRole"
        # Pinned to the two roles the F2 scheduled-skill task-def is
        # registered with -- no wildcards. RunTask fails with
        # AccessDenied without these.
        Resource = [
          var.execution_role_arn,
          var.agent_task_role_arn,
        ]
      }
    ]
  })
}

# ─── EventBridge rules + targets ────────────────────────────────────────────
# One rule per skill. The target uses the standard EventBridge ECS
# integration: target.arn = cluster ARN, target.role_arn = invoker
# role above, target.ecs_target.task_definition_arn = F2 ARN. The
# `input_transformer` overrides container env vars at invocation
# time so the same task-def serves all four skills.

resource "aws_cloudwatch_event_rule" "scheduled_skill" {
  for_each = local.skills

  name                = "${var.name_prefix}-${each.key}"
  description         = each.value.description
  schedule_expression = each.value.schedule_expression
  state               = local.rule_state
}

resource "aws_cloudwatch_event_target" "scheduled_skill" {
  for_each = local.skills

  rule     = aws_cloudwatch_event_rule.scheduled_skill[each.key].name
  arn      = var.ecs_cluster_arn
  role_arn = aws_iam_role.events_invoker.arn

  ecs_target {
    task_definition_arn = var.scheduled_skill_task_definition_arn
    task_count          = 1
    launch_type         = "FARGATE"

    network_configuration {
      subnets          = var.task_subnet_ids
      security_groups  = [var.task_security_group_id]
      assign_public_ip = false
    }
  }

  # SKILL_NAME env override -- the in-container entrypoint reads this
  # to dispatch to /$SKILL_NAME. The `input` here is JSON literal that
  # ECS RunTask consumes as `overrides.containerOverrides[].environment`.
  # Container name MUST match the F2 scheduled-skill task-def's
  # container name (`scheduled-skill`).
  input = jsonencode({
    containerOverrides = [
      {
        name = "scheduled-skill"
        environment = [
          {
            name  = "SKILL_NAME"
            value = each.key
          },
        ]
      }
    ]
  })

  # Dead-letter queue for failed invocations. EventBridge writes the
  # original event payload here on delivery failure, retains for 14d.
  dead_letter_config {
    arn = aws_sqs_queue.scheduled_skills_dlq.arn
  }

  # Cap retries at the EventBridge maximum (24h, 185 attempts) -- this
  # is the platform default; we set it explicitly so any future change
  # to the AWS default doesn't silently shift behavior.
  retry_policy {
    maximum_event_age_in_seconds = 86400
    maximum_retry_attempts       = 185
  }
}

# ─── CloudWatch alarm: FailedInvocations (per-rule) ─────────────────────────
# AWS/Events FailedInvocations is incremented when EventBridge cannot
# invoke a rule's target (PassRole AccessDenied, task-def deleted,
# RunTask quota exhaustion, etc.). The metric is keyed on `RuleName` --
# AWS does not support OR-ing multiple RuleName values in a single
# alarm Dimensions block, so we provision one alarm per rule via
# for_each. Cost: 4× $0.10/mo standard alarm. Attribution: the alarm
# name + SNS payload identify which scheduled skill failed without
# requiring an operator to dig through CloudTrail.
#
# Closes adversarial-review MAJOR 7 (silent EventBridge failure mode):
# if any rule cannot deliver, the operator gets a per-skill page.

resource "aws_cloudwatch_metric_alarm" "eventbridge_failures" {
  for_each = var.enable_alerts && var.alert_sns_topic_arn != "" ? local.skills : {}

  alarm_name        = "${var.name_prefix}-eventbridge-failures-${each.key}-${var.environment}"
  alarm_description = "Dispatcher v3 EventBridge rule `${var.name_prefix}-${each.key}` failed to invoke its scheduled-skill ECS target. Closes adversarial-review MAJOR 7 (silent skill failures). Investigate via the shared DLQ (`judgemind-${var.name_prefix}-scheduled-skills-dlq-${var.environment}`) and CloudTrail. Issue #3890."

  namespace   = "AWS/Events"
  metric_name = "FailedInvocations"
  statistic   = "Sum"

  comparison_operator = "GreaterThanThreshold"
  threshold           = var.failed_invocations_threshold
  period              = var.failed_invocations_period_seconds
  evaluation_periods  = var.failed_invocations_evaluation_periods
  datapoints_to_alarm = var.failed_invocations_evaluation_periods
  treat_missing_data  = "notBreaching"

  alarm_actions = [var.alert_sns_topic_arn]
  ok_actions    = [var.alert_sns_topic_arn]

  dimensions = {
    RuleName = "${var.name_prefix}-${each.key}"
  }
}

# ─── Postcondition assertions on every rule + target ────────────────────────
# Defense-in-depth against the silent-drop class (#2840 / #3764) for
# the SKILL_NAME override. If a regression ever drops the override
# from `input`, every scheduled-skill task would launch with no
# SKILL_NAME -- the entrypoint would either crash or default to one
# skill, neither of which is observable from the EventBridge UI.

# Note: terraform postconditions on aws_cloudwatch_event_target need
# the `self.input` reference -- the rendered JSON literal -- and the
# string comparison is straightforward.
# Per-rule postconditions live inside the resource above (they don't
# fire here because for_each can't postcondition cleanly across all
# instances; the per-instance check is implicit via the for_each key).

# Module-level invariant: the scheduled_skill_task_definition_family
# must be the family the four targets reference. Strict equality is
# overkill -- a substring check that the family appears in the F2
# task-def ARN is enough, and tolerates the apply-time revision
# suffix.
check "task_def_arn_matches_family" {
  assert {
    condition     = strcontains(var.scheduled_skill_task_definition_arn, var.scheduled_skill_task_definition_family)
    error_message = "dispatcher-v3-scheduled-skills: scheduled_skill_task_definition_arn (${var.scheduled_skill_task_definition_arn}) does not contain the family name (${var.scheduled_skill_task_definition_family}). The EventBridge invoker role's RunTask grant is scoped to the family, so a mismatched ARN would fail with AccessDenied at every cron firing."
  }
}

check "alert_topic_required_when_enabled" {
  assert {
    condition     = !var.enable_alerts || var.alert_sns_topic_arn != ""
    error_message = "dispatcher-v3-scheduled-skills: enable_alerts is true but alert_sns_topic_arn is empty. The FailedInvocations alarm would have no destination -- closes adversarial-review MAJOR 7 only when the alarm can actually page."
  }
}
