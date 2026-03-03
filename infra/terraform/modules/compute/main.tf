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

  tags = {
    project     = "judgemind"
    environment = var.environment
  }
}

# ─── ECS Cluster ────────────────────────────────────────────────────────────

resource "aws_ecs_cluster" "main" {
  name = "judgemind-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = {
    project     = "judgemind"
    environment = var.environment
  }
}

# ─── Task Execution Role ───────────────────────────────────────────────────
# Assumed by the ECS agent (not the container). Grants permissions to pull
# container images from ECR and push logs to CloudWatch.

resource "aws_iam_role" "ecs_task_execution" {
  name        = "judgemind-ecs-execution-${var.environment}"
  description = "ECS task execution role — pull ECR images and write CloudWatch logs"

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

  tags = {
    project     = "judgemind"
    environment = var.environment
  }
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
  description = "Scraper ECS tasks — outbound HTTPS only"
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS to court websites and AWS APIs"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    project     = "judgemind"
    environment = var.environment
  }
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

      environment = [
        { name = "ENVIRONMENT", value = var.environment }
      ]

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

  tags = {
    project     = "judgemind"
    environment = var.environment
  }
}

# ─── EventBridge Scheduler ──────────────────────────────────────────────────
# Runs the scraper task on a daily schedule. EventBridge Scheduler replaces
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

  tags = {
    project     = "judgemind"
    environment = var.environment
  }
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
        Resource = aws_ecs_task_definition.scraper.arn
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

  tags = {
    project     = "judgemind"
    environment = var.environment
  }
}

resource "aws_iam_role_policy_attachment" "scheduler_run_task" {
  role       = aws_iam_role.scheduler_execution.name
  policy_arn = aws_iam_policy.scheduler_run_task.arn
}

resource "aws_scheduler_schedule" "scraper" {
  name        = "judgemind-scraper-${var.environment}"
  description = "Daily scraper run for ${var.environment}"

  schedule_expression = var.schedule_expression
  state               = var.schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 30
  }

  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.scheduler_execution.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.scraper.arn
      launch_type         = "FARGATE"
      task_count          = 1

      network_configuration {
        subnets          = var.private_subnet_ids
        security_groups  = [aws_security_group.scraper.id]
        assign_public_ip = false
      }
    }
  }
}
