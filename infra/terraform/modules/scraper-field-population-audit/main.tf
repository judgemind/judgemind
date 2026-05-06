# EventBridge-scheduled ECS task for the scraper field-population audit.
#
# Daily probe that samples recent S3 envelopes per scraper and asserts
# that load-bearing JSON fields (e.g. CourtListener's docket.court) are
# populated.  Catches the silent-drift bug class behind #4247 and #3885
# (a documented field that has gone empty in production responses
# without anyone noticing for weeks).
#
# Mirrors the scraper-zero-record-check module's pattern: a Fargate
# one-shot task driven by EventBridge Scheduler, sharing the scraper
# image with one Python entrypoint per check.  The runner script
# (scripts/audit_scraper_field_population.py) handles DB sampling, S3
# fetches, drift detection, and GitHub issue creation entirely inside
# the container.
#
# Schedule: daily at 15:00 UTC (after the 14:30 zero-record check so
# fresh capture data is in the DB).
#
# See https://github.com/judgemind/judgemind/issues/4255 for context.

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# ─── CloudWatch Log Group ──────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "field_population_audit" {
  name              = "/ecs/judgemind-field-population-audit-${var.environment}"
  retention_in_days = var.log_retention_days
}

# ─── Security Group ────────────────────────────────────────────────────────
# Outbound HTTPS to S3, GitHub API, AWS APIs; Postgres for the sampling
# query.

resource "aws_security_group" "field_population_audit" {
  name        = "judgemind-field-population-audit-${var.environment}"
  description = "Field-population audit ECS task -- outbound HTTPS and Postgres"
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS to S3, GitHub API, AWS APIs"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "PostgreSQL database (sampling query)"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ─── Task Role ─────────────────────────────────────────────────────────────
# Read-only S3 GetObject on the document archive bucket so the audit can
# fetch sample envelopes.  No PutObject / DeleteObject -- this is a
# pure-observation task.

resource "aws_iam_role" "task_role" {
  name        = "judgemind-field-population-audit-task-${var.environment}"
  description = "Task role for the field-population audit -- read-only S3 + DB"

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

resource "aws_iam_role_policy" "task_s3_read" {
  name = "judgemind-field-population-audit-task-s3-read-${var.environment}"
  role = aws_iam_role.task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadDocumentArchive"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket",
        ]
        Resource = [
          var.document_archive_bucket_arn,
          "${var.document_archive_bucket_arn}/*",
        ]
      }
    ]
  })
}

# ─── Execution Role — secrets ──────────────────────────────────────────────
# Allow the task execution role to fetch DATABASE_URL and GITHUB_TOKEN so
# ECS can inject them into the container at launch.

locals {
  execution_role_name = element(
    split("/", var.task_execution_role_arn),
    length(split("/", var.task_execution_role_arn)) - 1
  )

  # Compact + count-guard pattern (#2739 / #2740): if both ARNs were
  # somehow blank, the policy would render with an empty Resource
  # list -- a silent IAM misconfiguration.  Both vars are required at
  # the variable layer, but the count-guard is defense-in-depth.
  execution_secret_arns = compact([
    var.db_connection_secret_arn,
    var.github_token_secret_arn,
  ])
}

resource "aws_iam_role_policy" "execution_secrets" {
  count = length(local.execution_secret_arns) > 0 ? 1 : 0

  name = "judgemind-field-population-audit-execution-secrets-${var.environment}"
  role = local.execution_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadFieldPopulationAuditSecrets"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = local.execution_secret_arns
      }
    ]
  })
}

# ─── Task Definition ───────────────────────────────────────────────────────

resource "aws_ecs_task_definition" "field_population_audit" {
  family                   = "judgemind-field-population-audit-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = var.task_execution_role_arn
  task_role_arn            = aws_iam_role.task_role.arn

  container_definitions = jsonencode([
    {
      name  = "field-population-audit"
      image = "${var.ecr_repository_url}:${var.scraper_image_tag}"
      # Override the scraper image's ENTRYPOINT (["python", "-m"]).
      # Without this override the rendered command becomes
      # `python -m python3 scripts/...` -> "No module named python3".
      # The same bug pattern affects the zero-record-check sibling
      # module today; that one is tracked separately (see /ecs/judgemind-zero-record-check-dev logs).
      entryPoint = ["python3"]
      command    = ["scripts/audit_scraper_field_population.py", "--json"]
      essential  = true

      environment = [
        { name = "AWS_REGION", value = data.aws_region.current.id },
        { name = "GH_REPO", value = var.gh_repo },
        { name = "S3_BUCKET", value = var.document_archive_bucket_name },
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
          "awslogs-group"         = aws_cloudwatch_log_group.field_population_audit.name
          "awslogs-region"        = data.aws_region.current.id
          "awslogs-stream-prefix" = "field-population-audit"
        }
      }
    }
  ])

  # Content-level postconditions on the rendered container_definitions
  # JSON.  Defense-in-depth against the #2840 silent-drop class of bug
  # (an apply that produces a task-def revision without a required
  # secret entry, despite the corresponding ARN variable being non-
  # empty).
  lifecycle {
    postcondition {
      condition     = strcontains(self.container_definitions, "DATABASE_URL")
      error_message = "field-population-audit: rendered container_definitions is missing DATABASE_URL. See #3764 / parent #2840 for the silent-drop bug class this guards against."
    }
    postcondition {
      condition     = strcontains(self.container_definitions, "GITHUB_TOKEN")
      error_message = "field-population-audit: rendered container_definitions is missing GITHUB_TOKEN. See #3764 / parent #2840 for the silent-drop bug class this guards against."
    }
  }
}

# ─── EventBridge Scheduler ─────────────────────────────────────────────────
# Daily at 15:00 UTC -- runs after the 14:30 zero-record check so any
# fresh captures are visible in the DB sampling query.

resource "aws_iam_role" "scheduler_execution" {
  name        = "judgemind-field-population-audit-scheduler-${var.environment}"
  description = "Allows EventBridge Scheduler to run the field-population audit ECS task"

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
  name        = "judgemind-field-population-audit-scheduler-run-task-${var.environment}"
  description = "Allows EventBridge Scheduler to run the field-population audit ECS task and pass roles"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowRunTask"
        Effect   = "Allow"
        Action   = "ecs:RunTask"
        Resource = "arn:aws:ecs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.field_population_audit.family}:*"
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

resource "aws_scheduler_schedule" "field_population_audit" {
  name        = "judgemind-field-population-audit-${var.environment}"
  description = "Daily scraper field-population audit for ${var.environment} (15:00 UTC)"

  # Daily at 15:00 UTC.  EventBridge Scheduler cron requires a year
  # field; "?" matches every year.
  schedule_expression          = "cron(0 15 * * ? *)"
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
      task_definition_arn = aws_ecs_task_definition.field_population_audit.arn
      launch_type         = "FARGATE"
      task_count          = 1

      network_configuration {
        subnets          = var.private_subnet_ids
        security_groups  = [aws_security_group.field_population_audit.id]
        assign_public_ip = false
      }
    }
  }
}
