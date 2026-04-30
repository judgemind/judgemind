# EventBridge-scheduled ECS task for the scraper zero-record streak check.
#
# Replaces the GH Actions cron that previously ran
# scripts/check-scraper-zero-record-streak.py. The check now runs inside a
# Fargate one-shot task on the same scraper image, injecting DATABASE_URL and
# GITHUB_TOKEN from Secrets Manager. Issue creation, dedup, update, auto-close,
# and Telegram notification are handled by the Python runner
# scripts/check-scraper-zero-record-runner.py baked into the image.
#
# Schedule: daily at 14:30 UTC (same cadence as the former workflow).
#
# See https://github.com/judgemind/judgemind/issues/2677 for context.

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# ─── CloudWatch Log Group ──────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "zero_record_check" {
  name              = "/ecs/judgemind-zero-record-check-${var.environment}"
  retention_in_days = var.log_retention_days
}

# ─── Security Group ────────────────────────────────────────────────────────
# Needs outbound HTTPS (443) to reach Secrets Manager, GitHub API, Telegram,
# and Postgres (5432) for the streak query.

resource "aws_security_group" "zero_record_check" {
  name        = "judgemind-zero-record-check-${var.environment}"
  description = "Zero-record check ECS task -- outbound HTTPS and Postgres"
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS to GitHub API, Telegram, AWS APIs"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "PostgreSQL database (streak query)"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ─── Task Role ─────────────────────────────────────────────────────────────
# Assumed by the running container. Grants Secrets Manager read on the
# Telegram secret so notify-telegram.sh can fetch the bot token from inside
# the task at runtime.

resource "aws_iam_role" "task_role" {
  name        = "judgemind-zero-record-check-task-${var.environment}"
  description = "Task role for the zero-record streak check -- Telegram secret read"

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

resource "aws_iam_role_policy" "task_telegram_secret" {
  count = var.telegram_secret_arn != "" ? 1 : 0

  name = "judgemind-zero-record-check-task-telegram-${var.environment}"
  role = aws_iam_role.task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadTelegramSecret"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = var.telegram_secret_arn
      }
    ]
  })
}

# ─── Execution Role — secrets ──────────────────────────────────────────────
# Allow the task execution role to fetch DATABASE_URL and GITHUB_TOKEN so ECS
# can inject them into the container at launch. We attach a policy directly to
# the shared execution role passed in via var.task_execution_role_arn.

locals {
  execution_role_name = element(
    split("/", var.task_execution_role_arn),
    length(split("/", var.task_execution_role_arn)) - 1
  )
}

resource "aws_iam_role_policy" "execution_secrets" {
  name = "judgemind-zero-record-check-execution-secrets-${var.environment}"
  role = local.execution_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadZeroRecordCheckSecrets"
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        Resource = compact([
          var.db_connection_secret_arn,
          var.github_token_secret_arn,
        ])
      }
    ]
  })
}

# ─── Task Definition ───────────────────────────────────────────────────────

resource "aws_ecs_task_definition" "zero_record_check" {
  family                   = "judgemind-zero-record-check-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = var.task_execution_role_arn
  task_role_arn            = aws_iam_role.task_role.arn

  container_definitions = jsonencode([
    {
      name      = "zero-record-check"
      image     = "${var.ecr_repository_url}:${var.scraper_image_tag}"
      command   = ["python3", "scripts/check-scraper-zero-record-runner.py"]
      essential = true

      environment = [
        { name = "AWS_REGION", value = data.aws_region.current.id },
        { name = "GH_REPO", value = var.gh_repo },
      ]

      secrets = [
        {
          name      = "DATABASE_URL"
          valueFrom = "${var.db_connection_secret_arn}:url::"
        },
        {
          name      = "GITHUB_TOKEN"
          valueFrom = var.github_token_secret_arn
        },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.zero_record_check.name
          "awslogs-region"        = data.aws_region.current.id
          "awslogs-stream-prefix" = "zero-record-check"
        }
      }
    }
  ])

  # Content-level postconditions on the rendered container_definitions
  # JSON. Defense-in-depth against the #2840 silent-drop class of bug:
  # an apply that produces a task-def revision without a required
  # secret entry, despite the corresponding ARN variable being non-
  # empty (caused by a stale data-source evaluation, provider
  # content-hash dedup, or any other future regression that drops a
  # `concat()` branch from the rendered JSON).
  #
  # These complement the variable-level preconditions above: those
  # catch "ARN unset"; these catch "ARN set but didn't propagate to
  # the rendered JSON". A regression test for this pattern lives in
  # `tests/postconditions/`.
  #
  # `self.container_definitions` is the rendered JSON string AS
  # PASSED TO THE AWS PROVIDER — the same content that becomes the
  # registered task-definition revision. If the secret name is not
  # in that string, the secret would not be injected into the
  # container, regardless of what `var.X_secret_arn` says.
  lifecycle {
    postcondition {
      condition     = strcontains(self.container_definitions, "DATABASE_URL")
      error_message = "scraper-zero-record-check: rendered container_definitions is missing DATABASE_URL. See #3764 / parent #2840 for the silent-drop bug class this guards against."
    }
    postcondition {
      condition     = strcontains(self.container_definitions, "GITHUB_TOKEN")
      error_message = "scraper-zero-record-check: rendered container_definitions is missing GITHUB_TOKEN. See #3764 / parent #2840 for the silent-drop bug class this guards against."
    }
  }
}

# ─── EventBridge Scheduler ─────────────────────────────────────────────────
# Replaces the GH Actions cron(30 14 * * *). EventBridge Scheduler cron
# syntax requires a sixth field (year), represented by "?" for every year.

resource "aws_iam_role" "scheduler_execution" {
  name        = "judgemind-zero-record-check-scheduler-${var.environment}"
  description = "Allows EventBridge Scheduler to run the zero-record check ECS task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "scheduler.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_policy" "scheduler_run_task" {
  name        = "judgemind-zero-record-check-scheduler-run-task-${var.environment}"
  description = "Allows EventBridge Scheduler to run the zero-record check ECS task and pass roles"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowRunTask"
        Effect   = "Allow"
        Action   = "ecs:RunTask"
        Resource = "arn:aws:ecs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.zero_record_check.family}:*"
        Condition = {
          ArnEquals = {
            "ecs:cluster" = var.ecs_cluster_arn
          }
        }
      },
      {
        Sid    = "AllowPassRole"
        Effect = "Allow"
        Action = "iam:PassRole"
        Resource = [
          var.task_execution_role_arn,
          aws_iam_role.task_role.arn,
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "scheduler_run_task" {
  role       = aws_iam_role.scheduler_execution.name
  policy_arn = aws_iam_policy.scheduler_run_task.arn
}

resource "aws_scheduler_schedule" "zero_record_check" {
  name        = "judgemind-zero-record-check-${var.environment}"
  description = "Daily scraper zero-record streak check for ${var.environment} (14:30 UTC)"

  # Matches the former GH Actions cron: '30 14 * * *'.
  # EventBridge Scheduler cron requires a year field; "?" matches every year.
  schedule_expression          = "cron(30 14 * * ? *)"
  schedule_expression_timezone = "UTC"
  state                        = var.schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 30
  }

  target {
    arn      = var.ecs_cluster_arn
    role_arn = aws_iam_role.scheduler_execution.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.zero_record_check.arn
      launch_type         = "FARGATE"
      task_count          = 1

      network_configuration {
        subnets          = var.private_subnet_ids
        security_groups  = [aws_security_group.zero_record_check.id]
        assign_public_ip = false
      }
    }
  }
}
