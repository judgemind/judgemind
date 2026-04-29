# ECS Fargate compute module for scraper containers.
#
# Provisions an ECS cluster, a Fargate task definition for the scraper
# framework, an EventBridge Scheduler rule for daily runs, a security group
# restricting traffic to outbound HTTPS only, and a CloudWatch log group for
# scraper output.
#
# The task execution role has permissions to pull images from ECR and write
# logs to CloudWatch. The task role references the scraper write role from the
# iam_scraper module, granting the container S3 PutObject access.

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# ─── CloudWatch Log Group ───────────────────────────────────────────────────
# Scraper stdout/stderr is forwarded here via the awslogs driver.

resource "aws_cloudwatch_log_group" "scraper" {
  name              = "/ecs/judgemind-scraper-${var.environment}"
  retention_in_days = var.log_retention_days
}

# ─── ECS Cluster ────────────────────────────────────────────────────────────

resource "aws_ecs_cluster" "main" {
  name = "judgemind-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  configuration {
    execute_command_configuration {
      logging = "OVERRIDE"

      log_configuration {
        cloud_watch_log_group_name = aws_cloudwatch_log_group.ecs_exec.name
      }
    }
  }
}

# CloudWatch log group for ECS Exec session output.
resource "aws_cloudwatch_log_group" "ecs_exec" {
  name              = "/ecs/judgemind-exec-${var.environment}"
  retention_in_days = var.log_retention_days
}

# ─── Task Execution Role ───────────────────────────────────────────────────
# Assumed by the ECS agent (not the container). Grants permissions to pull
# container images from ECR and push logs to CloudWatch.

resource "aws_iam_role" "ecs_task_execution" {
  name        = "judgemind-ecs-execution-${var.environment}"
  description = "ECS task execution role - pull ECR images and write CloudWatch logs"

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

resource "aws_iam_role_policy_attachment" "ecs_task_execution" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ─── Security Group ────────────────────────────────────────────────────────
# Scrapers need outbound HTTPS to reach court websites. No inbound traffic
# is required — Fargate tasks in private subnets are not addressable.

resource "aws_security_group" "scraper" {
  name        = "judgemind-scraper-${var.environment}"
  description = "Scraper ECS tasks - outbound HTTPS only"
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS to court websites and AWS APIs"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Redis event bus"
    from_port   = 6379
    to_port     = 6379
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "PostgreSQL database (scraper run recording)"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# When a residential proxy is configured, allow outbound traffic to the
# proxy port so the scraper can route requests through it.
resource "aws_security_group_rule" "scraper_proxy_egress" {
  count = var.proxy_secret_arn != "" ? 1 : 0

  type              = "egress"
  description       = "Residential proxy"
  from_port         = var.proxy_port
  to_port           = var.proxy_port
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = aws_security_group.scraper.id
}

# ─── Task Definition ───────────────────────────────────────────────────────
# Fargate task running the scraper-framework container. The container uses
# the task role (scraper write role) for S3 access and the execution role
# for ECR/CloudWatch.

resource "aws_ecs_task_definition" "scraper" {
  family                   = "judgemind-scraper-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = var.scraper_task_role_arn

  container_definitions = jsonencode([
    {
      name      = "scraper"
      image     = "${var.ecr_repository_url}:${var.scraper_image_tag}"
      essential = true

      # Give the scraper 2 minutes to finish the current scraper and exit
      # gracefully when ECS sends SIGTERM (default is only 30 seconds).
      # The full 17-scraper run takes ~25-35 minutes; this prevents data
      # loss if a stop signal arrives mid-scrape.  See #2349.
      stopTimeout = 120

      environment = concat(
        [{ name = "ENVIRONMENT", value = var.environment }],
        var.redis_url != "" ? [{ name = "REDIS_URL", value = var.redis_url }] : [],
        var.document_archive_bucket != "" ? [{ name = "JUDGEMIND_ARCHIVE_BUCKET", value = var.document_archive_bucket }] : []
      )

      secrets = concat(
        var.db_connection_secret_arn != "" ? [
          {
            name      = "DATABASE_URL"
            valueFrom = "${var.db_connection_secret_arn}:url::"
          }
        ] : [],
        var.courtlistener_api_token_secret_arn != "" ? [
          {
            name      = "COURTLISTENER_API_TOKEN"
            valueFrom = var.courtlistener_api_token_secret_arn
          }
        ] : [],
        var.capsolver_api_key_secret_arn != "" ? [
          {
            name      = "CAPSOLVER_API_KEY"
            valueFrom = var.capsolver_api_key_secret_arn
          }
        ] : [],
        var.proxy_secret_arn != "" ? [
          {
            name      = "SD_PROXY_URL"
            valueFrom = var.proxy_secret_arn
          },
          {
            # SF civil scraper reads the same residential proxy secret
            # under a scraper-specific env var. Both scrapers share the
            # secret in dev (see #2622); no secret re-provisioning needed.
            name      = "SF_PROXY_URL"
            valueFrom = var.proxy_secret_arn
          }
        ] : []
      )

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.scraper.name
          "awslogs-region"        = data.aws_region.current.id
          "awslogs-stream-prefix" = "scraper"
        }
      }
    }
  ])
}

# ─── SSM: terraform-managed container_definitions for deploy-scraper ────────
# Stores the rendered container_definitions JSON that terraform considers the
# desired state for the per-court scraper task. The deploy-scraper workflow's
# `ecs-deploy` composite action reads this parameter (instead of the running
# task-def) when registering a new revision, so secrets / env vars terraform
# has removed can never leak back in via the legacy "preserve current task-def
# content" path. See #3770 / parent #2840 for the bug class this prevents and
# #3769 for the deploy-api precedent.
#
# Tier "Advanced" supports values up to 8KB (Standard caps at 4KB) — gives
# headroom for future env-var / secret additions without a tier bump.
resource "aws_ssm_parameter" "scraper_container_definitions" {
  name        = "/judgemind/scraper/${var.environment}/container-definitions"
  description = "Terraform-rendered container_definitions JSON for the ${var.environment} scraper task. Read by .github/actions/ecs-deploy when --desired-container-definitions-ssm-parameter is set. See #3770 / #2840."
  type        = "String"
  tier        = "Advanced"
  value       = aws_ecs_task_definition.scraper.container_definitions
}

# ─── EventBridge Scheduler ──────────────────────────────────────────────────
# Runs the scraper task on a twice-daily schedule. EventBridge Scheduler replaces
# the legacy CloudWatch Events / EventBridge Rules pattern and supports
# native ECS RunTask targets without a Lambda intermediary.

resource "aws_iam_role" "scheduler_execution" {
  name        = "judgemind-scheduler-${var.environment}"
  description = "Allows EventBridge Scheduler to run ECS scraper tasks"

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
  name        = "judgemind-scheduler-run-task-${var.environment}"
  description = "Allows EventBridge Scheduler to run ECS tasks and pass roles"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowRunTask"
        Effect   = "Allow"
        Action   = "ecs:RunTask"
        Resource = "arn:aws:ecs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.scraper.family}:*"
        Condition = {
          ArnEquals = {
            "ecs:cluster" = aws_ecs_cluster.main.arn
          }
        }
      },
      {
        Sid    = "AllowPassRole"
        Effect = "Allow"
        Action = "iam:PassRole"
        Resource = [
          aws_iam_role.ecs_task_execution.arn,
          var.scraper_task_role_arn
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "scheduler_run_task" {
  role       = aws_iam_role.scheduler_execution.name
  policy_arn = aws_iam_policy.scheduler_run_task.arn
}

resource "aws_scheduler_schedule" "scraper" {
  name        = "judgemind-scraper-${var.environment}"
  description = "Daily scraper run for ${var.environment}"

  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = var.schedule_timezone
  state                        = var.schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 30
  }

  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.scheduler_execution.arn

    ecs_parameters {
      task_definition_arn    = aws_ecs_task_definition.scraper.arn
      launch_type            = "FARGATE"
      task_count             = 1
      enable_execute_command = true

      network_configuration {
        subnets          = var.private_subnet_ids
        security_groups  = [aws_security_group.scraper.id]
        assign_public_ip = false
      }
    }
  }
}

# ─── Ingestion Worker ───────────────────────────────────────────────────────
# Long-running ECS Fargate service that consumes document.captured events from
# the Redis Stream and writes them to Postgres and OpenSearch.
# Only deployed when db_connection_secret_arn is provided.

locals {
  deploy_ingestion = var.db_connection_secret_arn != ""
  # Extract role name from ARN (arn:aws:iam::ACCT:role/NAME) for inline policy attachment.
  task_role_name = element(split("/", var.scraper_task_role_arn), length(split("/", var.scraper_task_role_arn)) - 1)
}

# Allow the task execution role to fetch the DB connection secret so ECS can
# inject DATABASE_URL into the container at launch.
resource "aws_iam_role_policy" "ecs_task_execution_secrets" {
  count = local.deploy_ingestion ? 1 : 0

  name = "judgemind-ecs-execution-secrets-${var.environment}"
  role = aws_iam_role.ecs_task_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadIngestionSecrets"
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        Resource = compact([
          var.db_connection_secret_arn,
          var.opensearch_credentials_secret_arn,
          var.anthropic_api_key_secret_arn,
          var.google_api_key_secret_arn,
        ])
      }
    ]
  })
}

# Allow the task execution role to fetch the residential proxy secret so ECS
# can inject SD_PROXY_URL and SF_PROXY_URL into the scraper container at launch
# (both env vars resolve to the same secret value — see #2622).
resource "aws_iam_role_policy" "ecs_task_execution_proxy_secret" {
  count = var.proxy_secret_arn != "" ? 1 : 0

  name = "judgemind-ecs-execution-proxy-secret-${var.environment}"
  role = aws_iam_role.ecs_task_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadProxySecret"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = var.proxy_secret_arn
      }
    ]
  })
}

# Allow the task execution role to fetch the CourtListener API token secret
# so ECS can inject COURTLISTENER_API_TOKEN into the scraper container.
resource "aws_iam_role_policy" "ecs_task_execution_courtlistener_secret" {
  count = var.courtlistener_api_token_secret_arn != "" ? 1 : 0

  name = "judgemind-ecs-execution-courtlistener-secret-${var.environment}"
  role = aws_iam_role.ecs_task_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadCourtListenerSecret"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = var.courtlistener_api_token_secret_arn
      }
    ]
  })
}

# Allow the task execution role to fetch the CAPSolver API key secret
# so ECS can inject CAPSOLVER_API_KEY into the scraper container.
# Used by scrapers that need deterministic Cloudflare Turnstile solves
# (e.g. SF civil tentatives on webapps.sftc.org). See #2623.
resource "aws_iam_role_policy" "ecs_task_execution_capsolver_secret" {
  count = var.capsolver_api_key_secret_arn != "" ? 1 : 0

  name = "judgemind-ecs-execution-capsolver-secret-${var.environment}"
  role = aws_iam_role.ecs_task_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadCAPSolverSecret"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = var.capsolver_api_key_secret_arn
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "ingestion_worker" {
  count = local.deploy_ingestion ? 1 : 0

  name              = "/ecs/judgemind-ingestion-worker-${var.environment}"
  retention_in_days = var.log_retention_days
}

# The ingestion worker needs outbound access to Redis (6379), Postgres (5432),
# OpenSearch (443), and S3 (443).
resource "aws_security_group" "ingestion_worker" {
  count = local.deploy_ingestion ? 1 : 0

  name        = "judgemind-ingestion-worker-${var.environment}"
  description = "Ingestion worker ECS tasks - outbound to Redis, Postgres, OpenSearch"
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS to OpenSearch and S3"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Redis event bus"
    from_port   = 6379
    to_port     = 6379
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "PostgreSQL database"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_ecs_task_definition" "ingestion_worker" {
  count = local.deploy_ingestion ? 1 : 0

  family                   = "judgemind-ingestion-worker-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = var.scraper_task_role_arn

  container_definitions = jsonencode([
    {
      name      = "ingestion-worker"
      image     = "${var.ecr_repository_url}:${var.scraper_image_tag}"
      command   = ["ingestion"]
      essential = true

      # Secrets injected from Secrets Manager so they are never visible in
      # plaintext in the task definition.
      secrets = concat(
        [
          {
            name      = "DATABASE_URL"
            valueFrom = "${var.db_connection_secret_arn}:url::"
          }
        ],
        var.opensearch_credentials_secret_arn != "" ? [
          {
            name      = "OPENSEARCH_USERNAME"
            valueFrom = "${var.opensearch_credentials_secret_arn}:username::"
          },
          {
            name      = "OPENSEARCH_PASSWORD"
            valueFrom = "${var.opensearch_credentials_secret_arn}:password::"
          }
        ] : [],
        var.anthropic_api_key_secret_arn != "" ? [
          {
            name      = "ANTHROPIC_API_KEY"
            valueFrom = var.anthropic_api_key_secret_arn
          }
        ] : [],
        var.google_api_key_secret_arn != "" ? [
          {
            name      = "GOOGLE_API_KEY"
            valueFrom = var.google_api_key_secret_arn
          }
        ] : []
      )

      environment = concat(
        [
          { name = "ENVIRONMENT", value = var.environment },
          { name = "LLM_PROVIDER", value = var.llm_provider }
        ],
        var.redis_url != "" ? [{ name = "REDIS_URL", value = var.redis_url }] : [],
        var.opensearch_url != "" ? [{ name = "OPENSEARCH_URL", value = var.opensearch_url }] : [],
        var.document_archive_bucket != "" ? [{ name = "JUDGEMIND_ARCHIVE_BUCKET", value = var.document_archive_bucket }] : []
      )

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = "/ecs/judgemind-ingestion-worker-${var.environment}"
          "awslogs-region"        = data.aws_region.current.id
          "awslogs-stream-prefix" = "ingestion-worker"
        }
      }
    }
  ])
}

# ─── SSM: terraform-managed container_definitions for ingestion-worker ──────
# Same pattern as `aws_ssm_parameter.scraper_container_definitions` above:
# terraform writes the rendered container_definitions JSON to SSM so the
# ingestion-worker job in deploy-scraper.yml can read it via `ecs-deploy`'s
# `desired-container-definitions-ssm-parameter` input. Closes the
# preserve-secrets bug class for ingestion-worker. See #3770 / parent #2840
# and the deploy-api precedent in #3769.
#
# Conditional on local.deploy_ingestion mirrors the gate on the task-def
# resource — when ingestion is not deployed (no DB connection secret), the
# SSM parameter is also absent so terraform doesn't churn on an unused write.
resource "aws_ssm_parameter" "ingestion_worker_container_definitions" {
  count = local.deploy_ingestion ? 1 : 0

  name        = "/judgemind/ingestion-worker/${var.environment}/container-definitions"
  description = "Terraform-rendered container_definitions JSON for the ${var.environment} ingestion-worker task. Read by .github/actions/ecs-deploy when --desired-container-definitions-ssm-parameter is set. See #3770 / #2840."
  type        = "String"
  tier        = "Advanced"
  value       = aws_ecs_task_definition.ingestion_worker[0].container_definitions
}

resource "aws_ecs_service" "ingestion_worker" {
  count = local.deploy_ingestion ? 1 : 0

  name            = "judgemind-ingestion-worker-${var.environment}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.ingestion_worker[0].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  # Enable ECS Exec so operators can run ad-hoc commands (e.g. psql) on the
  # running container without a VPN or bastion host.
  enable_execute_command = true

  # Rolling-no-gap deploy policy: bring up the new task first, wait for it to
  # reach RUNNING, then drain the old one. With desired_count=1 the 0/100
  # policy guaranteed a 30–120 s window with runningCount=0 on every deploy
  # (#3556). 100/200 eliminates that gap. wait_for_steady_state=true ensures
  # terraform-apply blocks until the service settles.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  wait_for_steady_state              = true

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.ingestion_worker[0].id]
    assign_public_ip = false
  }

  # Ignore changes to task_definition so that image tag updates via CI/CD
  # don't trigger a Terraform diff on every plan.
  lifecycle {
    ignore_changes = [task_definition]
  }
}

# ─── ECS Exec IAM Policy ─────────────────────────────────────────────────────
# The task role needs SSM permissions for ECS Exec (aws ecs execute-command).
# This is an inline policy on the task role used by the ingestion worker so
# operators can run psql and other diagnostic commands inside the container.

resource "aws_iam_role_policy" "ecs_exec_ssm" {
  count = local.deploy_ingestion ? 1 : 0

  name = "judgemind-ecs-exec-ssm-${var.environment}"
  role = local.task_role_name

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
          "ssmmessages:OpenDataChannel"
        ]
        Resource = "*"
      },
      {
        Sid    = "AllowECSExecLogging"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
          "logs:PutLogEvents"
        ]
        Resource = "${aws_cloudwatch_log_group.ecs_exec.arn}:*"
      }
    ]
  })
}

# ─── Scraper Failure Alerts ──────────────────────────────────────────────────
# CloudWatch alarm that fires when no scraper task has completed successfully
# in the past 24 hours. Uses a metric filter on the log group to detect
# successful completion, then alarms when the count drops to zero.

resource "aws_sns_topic" "scraper_alerts" {
  count = var.enable_alerts ? 1 : 0

  name = "judgemind-scraper-alerts-${var.environment}"
}

resource "aws_sns_topic_subscription" "scraper_alerts_email" {
  count = var.enable_alerts && var.alert_email != "" ? 1 : 0

  topic_arn = aws_sns_topic.scraper_alerts[0].arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# Publish a custom metric whenever a scraper run completes (exit code 0).
# The scraper framework logs "scraper_run_complete" on successful finish.
# If no such log line appears in 24h, the alarm fires.

resource "aws_cloudwatch_log_metric_filter" "scraper_success" {
  count = var.enable_alerts ? 1 : 0

  name           = "judgemind-scraper-success-${var.environment}"
  pattern        = "\"scraper_run_complete\""
  log_group_name = aws_cloudwatch_log_group.scraper.name

  metric_transformation {
    name          = "ScraperSuccessCount"
    namespace     = "Judgemind/Scraper"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "scraper_no_success" {
  count = var.enable_alerts ? 1 : 0

  alarm_name        = "judgemind-scraper-no-success-24h-${var.environment}"
  alarm_description = "No successful scraper run in the past 24 hours (${var.environment})"

  namespace   = "Judgemind/Scraper"
  metric_name = "ScraperSuccessCount"
  statistic   = "Sum"

  comparison_operator = "LessThanOrEqualToThreshold"
  threshold           = 0
  period              = 86400
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.scraper_alerts[0].arn]
  ok_actions    = [aws_sns_topic.scraper_alerts[0].arn]
}

# ─── Data Quality Check Alerts ───────────────────────────────────────────────
# CloudWatch alarm that fires when the hourly data quality check has not
# completed in the past 2 hours. The check runs as a oneshot ECS task launched
# by GitHub Actions, logging to the ingestion worker log group. The script logs
# "data_quality_check_complete" on every run.

resource "aws_cloudwatch_log_metric_filter" "data_quality_complete" {
  count = var.enable_alerts && local.deploy_ingestion ? 1 : 0

  name           = "judgemind-data-quality-complete-${var.environment}"
  pattern        = "\"data_quality_check_complete\""
  log_group_name = aws_cloudwatch_log_group.ingestion_worker[0].name

  metric_transformation {
    name          = "DataQualityCheckCount"
    namespace     = "Judgemind/DataQuality"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "data_quality_no_run" {
  count = var.enable_alerts && local.deploy_ingestion ? 1 : 0

  alarm_name        = "judgemind-data-quality-no-run-2h-${var.environment}"
  alarm_description = "No data quality check has completed in the past 2 hours (${var.environment}). The hourly GitHub Actions workflow may be failing or disabled."

  namespace   = "Judgemind/DataQuality"
  metric_name = "DataQualityCheckCount"
  statistic   = "Sum"

  comparison_operator = "LessThanOrEqualToThreshold"
  threshold           = 0
  period              = 7200
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.scraper_alerts[0].arn]
  ok_actions    = [aws_sns_topic.scraper_alerts[0].arn]
}

# ─── Ingestion Worker Crash-Loop Alerts ──────────────────────────────────────
# CloudWatch alarm that fires when the ingestion worker restarts repeatedly in
# a short window. The worker logs "Infrastructure error" or "Unhandled
# exception" via structlog JSON before exiting, which triggers an ECS restart.
# Repeated occurrences indicate a crash loop (e.g. missing DB column, OOM).
#
# See: #1044 where a crash loop ran for a week before the data quality check
# caught the symptom (stale scrapers) after 170+ hours.

resource "aws_cloudwatch_log_metric_filter" "ingestion_worker_crash" {
  count = var.enable_alerts && local.deploy_ingestion ? 1 : 0

  name           = "judgemind-ingestion-worker-crash-${var.environment}"
  pattern        = "?\"Infrastructure error\" ?\"Unhandled exception\""
  log_group_name = aws_cloudwatch_log_group.ingestion_worker[0].name

  metric_transformation {
    name          = "IngestionWorkerCrashCount"
    namespace     = "Judgemind/Ingestion"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "ingestion_worker_crash_loop" {
  count = var.enable_alerts && local.deploy_ingestion ? 1 : 0

  alarm_name        = "judgemind-ingestion-worker-crash-loop-${var.environment}"
  alarm_description = "Ingestion worker has crashed >= 3 times in the past 15 minutes (${var.environment}). Check ${aws_cloudwatch_log_group.ingestion_worker[0].name} for details."

  namespace   = "Judgemind/Ingestion"
  metric_name = "IngestionWorkerCrashCount"
  statistic   = "Sum"

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 3
  period              = 900
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.scraper_alerts[0].arn]
  ok_actions    = [aws_sns_topic.scraper_alerts[0].arn]
}

# ─── Ingestion Worker Idle Alerts ────────────────────────────────────────────
# CloudWatch alarm that fires when the ingestion worker has been idle (no
# messages processed) for longer than the configured threshold. The worker
# emits a periodic heartbeat log with an `idle_seconds` field every ~5 minutes
# when no messages arrive. A sustained high idle_seconds value during expected
# scraper run windows indicates the worker is alive but not receiving work —
# likely because scraper output is not reaching the Redis stream.
#
# See: #2220 where the worker was idle for 48+ hours without any alert.

resource "aws_cloudwatch_log_metric_filter" "ingestion_worker_idle" {
  count = var.enable_alerts && local.deploy_ingestion ? 1 : 0

  name           = "judgemind-ingestion-worker-idle-${var.environment}"
  pattern        = "{ $.event = \"Heartbeat: idle for *\" && $.idle_seconds = * }"
  log_group_name = aws_cloudwatch_log_group.ingestion_worker[0].name

  metric_transformation {
    name          = "IngestionWorkerIdleSeconds"
    namespace     = "Judgemind/Ingestion"
    value         = "$.idle_seconds"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "ingestion_worker_idle" {
  count = var.enable_alerts && local.deploy_ingestion ? 1 : 0

  alarm_name        = "judgemind-ingestion-worker-idle-${var.environment}"
  alarm_description = "Ingestion worker has been idle for > ${var.ingestion_idle_threshold_seconds} seconds (${var.environment}). The worker is alive but not receiving messages — check that scrapers are running and publishing to the Redis stream."

  namespace   = "Judgemind/Ingestion"
  metric_name = "IngestionWorkerIdleSeconds"
  statistic   = "Maximum"

  comparison_operator = "GreaterThanThreshold"
  threshold           = var.ingestion_idle_threshold_seconds
  # Evaluate over 1 hour.  The heartbeat fires every ~5 minutes, so each
  # evaluation period contains ~12 data points.  Using Maximum ensures a
  # single heartbeat reporting a high idle value triggers the alarm.
  period              = 3600
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.scraper_alerts[0].arn]
  ok_actions    = [aws_sns_topic.scraper_alerts[0].arn]
}
