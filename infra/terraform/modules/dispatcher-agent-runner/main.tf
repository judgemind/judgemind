# Dispatcher v2 agent-runner module — ECS Fargate task definition for
# per-agent runners (issue #3090; Stage 1b of the #3086 / #3078
# per-agent-ECS migration).
#
# Unlike ``dispatcher-daemon`` this module DOES NOT provision an ECS
# service — an agent-runner is a one-shot workload started via
# ``ecs:RunTask`` (manually via ``aws ecs run-task --overrides`` for
# the Stage 1b smoke, or by the daemon's future
# ``_launch_agent_ecs_task`` in Stage 2). The task runs the full phase
# pipeline inside the container and exits when the agent hits a
# terminal phase.
#
# Resources created:
# - CloudWatch log group ``/ecs/judgemind-dispatcher-agent-runner-<env>``.
# - Execution role (ECR pull + log writes + secret fetches).
# - Task role (narrow: Secrets Manager read on DATABASE_URL + GitHub
#   PAT, CloudWatch log writes into the agent-runner log group only,
#   plus ``ssmmessages:*`` for ECS Exec-based live debugging — #3145).
#   Deliberately does NOT include ``ecs:RunTask`` — the agent-runner
#   is a leaf; it does not spawn further ECS tasks.
# - Security group (outbound HTTPS + Postgres, same egress profile as
#   the daemon so GitHub / Anthropic / Postgres all reach from inside).
# - ECS task definition ``judgemind-dispatcher-agent-runner-<env>``
#   wired at CPU=4096 / memory=16384 to match the subprocess-daemon
#   envelope — the sizing ralph was designed against. The initial
#   Stage 1b baseline (512 / 1024) pegged memory at 1022/1024 MB and
#   CPU at 509/512 under real ralph workload (#3153); can shrink once
#   subprocess mode is retired.
#
# The security group ID + task-def family are exported so the daemon
# (Stage 2) can reference them without re-declaring ARNs in the
# environment wiring.

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  task_family    = "judgemind-dispatcher-agent-runner-${var.environment}"
  log_group_name = "/ecs/judgemind-dispatcher-agent-runner-${var.environment}"
  container_name = "agent-runner"

  # Execution-role secret list uses compact() so unset ARNs drop out
  # (mirrors the dispatcher-daemon pattern).
  execution_secret_arns = compact([
    var.anthropic_api_key_secret_arn,
    var.db_connection_secret_arn,
    var.github_token_secret_arn,
  ])

  # Task role gets a narrower set: DATABASE_URL (for direct psql calls
  # from inside the container) + GitHub PAT (for `gh auth login`). The
  # Anthropic API key is injected by the execution role at container
  # start and read from env; the task role doesn't re-fetch it.
  task_secret_arns = compact([
    var.db_connection_secret_arn,
    var.github_token_secret_arn,
  ])
}

# ─── CloudWatch Log Group ──────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "agent_runner" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
}

# ─── Security Group ────────────────────────────────────────────────────────
# Outbound-only egress: HTTPS (GitHub / Anthropic / ECR / Secrets
# Manager) + Postgres. No inbound traffic — agent-runners are leaves.

resource "aws_security_group" "agent_runner" {
  name        = local.task_family
  description = "Dispatcher agent-runner ECS tasks - outbound only (HTTPS, Postgres)"
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS to GitHub, Anthropic, ECR, Secrets Manager"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "PostgreSQL (dispatcher.* state)"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ─── IAM: Execution Role (ECS agent) ───────────────────────────────────────

resource "aws_iam_role" "execution" {
  name        = "${local.task_family}-exec"
  description = "Dispatcher agent-runner execution role - pull ECR, read secrets, write logs"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "ecs-tasks.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  count = length(local.execution_secret_arns) > 0 ? 1 : 0

  name = "${local.task_family}-exec-secrets"
  role = aws_iam_role.execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadAgentRunnerSecrets"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = local.execution_secret_arns
      }
    ]
  })
}

# ─── IAM: Task Role (container runtime) ────────────────────────────────────
#
# Narrow by design — the agent-runner is a leaf process. What it needs:
#
#   * Secrets Manager GetSecretValue on DATABASE_URL + GitHub PAT only
#     (the container may re-read these during long-running phases).
#   * CloudWatch `logs:PutLogEvents` on the agent-runner log group
#     only (narrower than the daemon's wildcard).
#   * ECS Exec (``ssmmessages:*``) — operators need live interactive
#     inspection of a running agent (worktree contents, claude
#     stdout/stderr files, git log, process tree) when CloudWatch tail
#     isn't enough. See #3145. Mirrors the dispatcher-daemon task role
#     (``infra/terraform/modules/dispatcher-daemon/main.tf``
#     ``task_ecs_exec_ssm``).
#
# What it deliberately does NOT have:
#
#   * ``ecs:RunTask`` — the agent-runner is a terminal leaf; it does
#     not spawn further ECS tasks. Granting RunTask would let a
#     compromised agent kick off its own children (or arbitrary other
#     task definitions).
#   * CloudWatch PutMetricData — the daemon emits agent metrics (Stage
#     2+); the agent-runner itself is metered via phase_outputs rows,
#     not directly.

resource "aws_iam_role" "task" {
  name        = "${local.task_family}-task"
  description = "Dispatcher agent-runner task role - narrow runtime permissions for in-container DB + GitHub work"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "ecs-tasks.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "task_read_secrets" {
  count = length(local.task_secret_arns) > 0 ? 1 : 0

  name = "${local.task_family}-task-read-secrets"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadAgentRunnerRuntimeSecrets"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = local.task_secret_arns
      }
    ]
  })
}

resource "aws_iam_role_policy" "task_log_writes" {
  name = "${local.task_family}-task-log-writes"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowAgentRunnerLogWrites"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        # Scoped to the agent-runner log group only. The daemon's
        # wildcard approach is unnecessary here because the task's
        # only log destination is its own group.
        Resource = "${aws_cloudwatch_log_group.agent_runner.arn}:*"
      }
    ]
  })
}

# ─── IAM: ECS Exec (ssmmessages) ───────────────────────────────────────────
#
# Required for ``aws ecs execute-command`` to shell into a running
# agent-runner task for live debugging (#3145). The task-def also sets
# ``enable_execute_command = true`` below, which + this policy + the
# per-RunTask ``enableExecuteCommand`` flag in ``daemon.py`` together
# enable the ECS Exec data plane end-to-end.
#
# Mirrors the dispatcher-daemon ``task_ecs_exec_ssm`` policy. The
# ``ssmmessages`` actions require ``Resource = "*"`` because the SSM
# session is created dynamically per-exec-invocation.

resource "aws_iam_role_policy" "task_ecs_exec_ssm" {
  name = "${local.task_family}-task-ecs-exec-ssm"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowECSExec"
        Effect = "Allow"
        Action = [
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel",
        ]
        Resource = "*"
      },
    ]
  })
}

# ─── ECS Task Definition ───────────────────────────────────────────────────
#
# CPU / memory: 4096 / 16384 to match the subprocess-daemon envelope —
# the sizing ralph was designed against. Earlier Stage 1b baseline
# (512 / 1024) saturated CPU at 509/512 and memory at 1022/1024 MB under
# real ralph workload (#3153). Can shrink once subprocess mode is retired
# and the dispatcher daemon scales down correspondingly.
#
# stopTimeout: ECS/Fargate caps stopTimeout at 120 seconds. The #3090
# issue text mentions "4h" but that's the expected agent lifetime, not
# a Fargate-enforced timeout — tasks run until the container exits.
# We set stopTimeout to 120 (the platform max) so the container has
# enough time to update ``dispatcher.agents.ended_at`` and flush final
# logs on a SIGTERM (e.g. during a force-stop).

resource "aws_ecs_task_definition" "agent_runner" {
  family                   = local.task_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  ephemeral_storage {
    size_in_gib = var.ephemeral_storage_gib
  }

  container_definitions = jsonencode([
    {
      name      = local.container_name
      image     = "${var.ecr_repository_url}:${var.image_tag}"
      essential = true

      # 120s — the platform max. See header comment for rationale.
      stopTimeout = 120

      environment = concat(
        [
          { name = "ENVIRONMENT", value = var.environment },
          { name = "AGENT_WORKSPACE", value = "/var/lib/agent-runner" },
          { name = "REPO_URL", value = var.repo_url },
        ],
        var.github_repo != "" ? [{ name = "GITHUB_REPO", value = var.github_repo }] : [],
      )

      secrets = concat(
        var.anthropic_api_key_secret_arn != "" ? [
          {
            name      = "ANTHROPIC_API_KEY"
            valueFrom = var.anthropic_api_key_secret_arn
          }
        ] : [],
        var.db_connection_secret_arn != "" ? [
          {
            # Same JSON-key-suffix pattern as the daemon module —
            # dispatcher-role DB secret has ``url`` at the top level.
            name      = "DATABASE_URL"
            valueFrom = "${var.db_connection_secret_arn}:url::"
          }
        ] : [],
        var.github_token_secret_arn != "" ? [
          {
            name      = "GITHUB_TOKEN"
            valueFrom = var.github_token_secret_arn
          }
        ] : [],
      )

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.agent_runner.name
          "awslogs-region"        = data.aws_region.current.id
          "awslogs-stream-prefix" = "agent-runner"
        }
      }
    }
  ])
}

# ── Agent-runner error alarm (#3093) ─────────────────────────────────────────
# Fires when any ERROR or FATAL structured-log event is emitted by an
# agent-runner task. A single unhandled exception inside a phase
# (unrecognized exit code, missing env var, AWS throttle) appears here
# before the daemon reap pass has a chance to notice the STOPPED task,
# giving operators an early signal.
#
# Gated behind enable_alerts (default false) so non-prod environments
# stay quiet. Wire enable_alerts = true in the dev module block once
# the SNS topic ARN is available (mirrors the dispatcher-daemon pattern).
#
# Mirrors the filter/alarm shape at
# infra/terraform/modules/dispatcher-daemon/main.tf (stuck_timeout_repeated).

resource "aws_cloudwatch_log_metric_filter" "agent_runner_errors" {
  count = var.enable_alerts ? 1 : 0

  name           = "${local.task_family}-errors"
  pattern        = "{ $.level = \"ERROR\" || $.level = \"FATAL\" }"
  log_group_name = aws_cloudwatch_log_group.agent_runner.name

  metric_transformation {
    name          = "AgentRunnerErrorCount"
    namespace     = "Judgemind/AgentRunner"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "agent_runner_errors" {
  count = var.enable_alerts ? 1 : 0

  alarm_name        = "${local.task_family}-errors"
  alarm_description = "An agent-runner task emitted an ERROR or FATAL log event (${var.environment}). A phase may have crashed before the daemon reap pass observed a STOPPED task — check ${aws_cloudwatch_log_group.agent_runner.name}."

  namespace   = "Judgemind/AgentRunner"
  metric_name = "AgentRunnerErrorCount"
  statistic   = "Sum"

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [var.alert_sns_topic_arn]
  ok_actions    = [var.alert_sns_topic_arn]
}
