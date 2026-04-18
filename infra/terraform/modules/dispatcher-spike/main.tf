# Dispatcher v2 Spike 0.1 — `claude -p` end-to-end on Fargate.
#
# THROWAWAY MODULE. Provisions only the minimum AWS resources needed to
# run a `claude -p` invocation as a one-off ECS Fargate task from the
# dev account:
#
#   - ECR repository for the spike image (`judgemind/dispatcher-spike`)
#   - CloudWatch log group (`/ecs/judgemind-dispatcher-spike-<env>`)
#   - An execution role that can pull the spike image and read the
#     ANTHROPIC_API_KEY + DATABASE_URL + GITHUB_TOKEN secrets
#   - A task role with no extra permissions (the spike does not touch S3
#     or other AWS APIs from inside the container — all DB writes happen
#     from the wrapper script outside the VPC)
#   - An ECS Fargate task definition with the spike image + the secrets
#     wired in. Task is launched ad-hoc via `aws ecs run-task` — no ECS
#     service, no scheduler, no alarms.
#
# After spike 0.1 findings land, this whole module (plus the
# dispatcher_spike.runs migration, Dockerfile.dispatcher-spike, and the
# ECR repo's images) should be torn down. See the cleanup follow-up
# referenced from `docs/investigations/dispatcher-v2-spike-0.1.md`.

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# ─── ECR repository ─────────────────────────────────────────────────────────

resource "aws_ecr_repository" "dispatcher_spike" {
  name                 = "judgemind/dispatcher-spike"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = false
  }
}

# Keep only the last few images — this repo is throwaway, but we don't
# want stale manifests piling up for the handful of rebuilds the spike
# runs will incur.
resource "aws_ecr_lifecycle_policy" "dispatcher_spike" {
  repository = aws_ecr_repository.dispatcher_spike.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Purge untagged images after 1 day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Retain last 5 tagged images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 5
        }
        action = { type = "expire" }
      }
    ]
  })
}

# ─── CloudWatch log group ───────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "dispatcher_spike" {
  name              = "/ecs/judgemind-dispatcher-spike-${var.environment}"
  retention_in_days = 7
}

# ─── Task execution role ────────────────────────────────────────────────────
# Assumed by the ECS agent. Pulls the image, reads the spike secrets, and
# writes to CloudWatch. No runtime AWS API access.

resource "aws_iam_role" "execution" {
  name        = "judgemind-dispatcher-spike-exec-${var.environment}"
  description = "Execution role for dispatcher-spike Fargate tasks (throwaway)"

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

# Allow the execution role to read only the secrets the spike needs.
resource "aws_iam_role_policy" "execution_secrets" {
  name = "judgemind-dispatcher-spike-exec-secrets-${var.environment}"
  role = aws_iam_role.execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadSpikeSecrets"
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        Resource = compact([
          var.anthropic_api_key_secret_arn,
          var.github_token_secret_arn,
          var.db_connection_secret_arn,
        ])
      }
    ]
  })
}

# ─── Task role ──────────────────────────────────────────────────────────────
# Assumed by the container at runtime. Intentionally empty — the spike
# makes no AWS API calls from inside the container. Exists purely so
# the task definition has something to reference.

resource "aws_iam_role" "task" {
  name        = "judgemind-dispatcher-spike-task-${var.environment}"
  description = "Task role for dispatcher-spike Fargate tasks (throwaway; no privileges)"

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

# ─── Task definition ────────────────────────────────────────────────────────

resource "aws_ecs_task_definition" "dispatcher_spike" {
  family                   = "judgemind-dispatcher-spike-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.task_cpu)
  memory                   = tostring(var.task_memory)
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name      = "dispatcher-spike"
      image     = "${aws_ecr_repository.dispatcher_spike.repository_url}:${var.image_tag}"
      essential = true

      # Container entrypoint is /usr/local/bin/dispatcher-spike-entry which
      # takes a single positional arg (the scenario name: success,
      # turn_limit, auth_fail, or mcp_probe). The wrapper script passes
      # this via `command` overrides.
      command = ["success"]

      environment = [
        { name = "ENVIRONMENT", value = var.environment },
        { name = "PYTHONUNBUFFERED", value = "1" },
        # Claude Code writes session state under $HOME/.claude/. The
        # scraper base image sets HOME=/home/scraper and owns that dir
        # to uid 1000 already; no extra work needed here.
      ]

      secrets = concat(
        [
          {
            name      = "ANTHROPIC_API_KEY"
            valueFrom = var.anthropic_api_key_secret_arn
          }
        ],
        var.github_token_secret_arn != "" ? [
          {
            name      = "GITHUB_TOKEN"
            valueFrom = var.github_token_secret_arn
          }
        ] : [],
        var.db_connection_secret_arn != "" ? [
          {
            name      = "DATABASE_URL"
            valueFrom = "${var.db_connection_secret_arn}:url::"
          }
        ] : []
      )

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.dispatcher_spike.name
          "awslogs-region"        = data.aws_region.current.id
          "awslogs-stream-prefix" = "spike"
        }
      }
    }
  ])
}
