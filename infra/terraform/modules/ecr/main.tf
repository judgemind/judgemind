# ECR repositories for Judgemind container images.
#
# Each service (scraper, api, dispatcher) gets its own repository following
# the org/service naming pattern (judgemind/scraper, judgemind/api,
# judgemind/dispatcher). Repositories are shared across environments via
# image tags (e.g. staging, latest).

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

resource "aws_ecr_repository" "scraper" {
  name                 = "judgemind/scraper"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Lifecycle policy: keep the last 10 tagged images; purge untagged images
# after 1 day. Untagged images accumulate quickly during CI builds and have
# no value once superseded by a tagged release.
resource "aws_ecr_lifecycle_policy" "scraper" {
  repository = aws_ecr_repository.scraper.name

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
        description  = "Retain last 10 tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v", "staging", "prod", "sha-"]
          countType     = "imageCountMoreThan"
          countNumber   = 10
        }
        action = { type = "expire" }
      }
    ]
  })
}

# Repository policy: restrict pull to the ECS task execution role.
# Only created once the execution role ARN is available (compute module, #20).
resource "aws_ecr_repository_policy" "scraper" {
  count      = var.enable_pull_policy ? 1 : 0
  repository = aws_ecr_repository.scraper.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowECSTaskExecutionPull"
        Effect = "Allow"
        Principal = {
          AWS = var.ecs_task_execution_role_arn
        }
        Action = [
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:BatchCheckLayerAvailability"
        ]
      }
    ]
  })
}

# ─── API Repository ──────────────────────────────────────────────────────────

resource "aws_ecr_repository" "api" {
  name                 = "judgemind/api"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name

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
        description  = "Retain last 10 tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v", "staging", "prod", "sha-"]
          countType     = "imageCountMoreThan"
          countNumber   = 10
        }
        action = { type = "expire" }
      }
    ]
  })
}

resource "aws_ecr_repository_policy" "api" {
  count      = var.enable_pull_policy ? 1 : 0
  repository = aws_ecr_repository.api.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowECSTaskExecutionPull"
        Effect = "Allow"
        Principal = {
          AWS = var.ecs_task_execution_role_arn
        }
        Action = [
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:BatchCheckLayerAvailability"
        ]
      }
    ]
  })
}

# ─── Dispatcher Repository ───────────────────────────────────────────────────
# Dispatcher v2 daemon image (spec §14, issue #2729). Same lifecycle +
# pull-policy pattern as the scraper and api repos. The dispatcher-daemon
# Terraform module wires this URL into the ECS task definition via
# `ecr_repository_url`; CI pushes images on merge to main (see
# `.github/workflows/deploy-dispatcher.yml`).

resource "aws_ecr_repository" "dispatcher" {
  name                 = "judgemind/dispatcher"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "dispatcher" {
  repository = aws_ecr_repository.dispatcher.name

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
        description  = "Retain last 10 tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v", "staging", "prod", "sha-"]
          countType     = "imageCountMoreThan"
          countNumber   = 10
        }
        action = { type = "expire" }
      }
    ]
  })
}

resource "aws_ecr_repository_policy" "dispatcher" {
  count      = var.enable_pull_policy ? 1 : 0
  repository = aws_ecr_repository.dispatcher.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowECSTaskExecutionPull"
        Effect = "Allow"
        Principal = {
          AWS = var.ecs_task_execution_role_arn
        }
        Action = [
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:BatchCheckLayerAvailability"
        ]
      }
    ]
  })
}

# ─── Dispatcher Agent-Runner Repository ──────────────────────────────────────
# Per-agent ECS task image (Stage 1b of #3086; issue #3090). Holds the
# `Dockerfile.dispatcher-agent-runner` image — same CLI + skills payload
# as the daemon image but with the agent-runner entrypoint baked in.
# Kept as a distinct repo from `judgemind/dispatcher` so the image build
# workflows and lifecycle policies stay independent (the agent-runner
# will land its own `deploy-agent-runner.yml` in a followup PR; until
# then operators push manually for the Stage 1b smoke).

resource "aws_ecr_repository" "dispatcher_agent_runner" {
  name                 = "judgemind/dispatcher-agent-runner"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "dispatcher_agent_runner" {
  repository = aws_ecr_repository.dispatcher_agent_runner.name

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
        description  = "Retain last 10 tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v", "staging", "prod", "sha-"]
          countType     = "imageCountMoreThan"
          countNumber   = 10
        }
        action = { type = "expire" }
      }
    ]
  })
}

resource "aws_ecr_repository_policy" "dispatcher_agent_runner" {
  count      = var.enable_pull_policy ? 1 : 0
  repository = aws_ecr_repository.dispatcher_agent_runner.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowECSTaskExecutionPull"
        Effect = "Allow"
        Principal = {
          AWS = var.ecs_task_execution_role_arn
        }
        Action = [
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:BatchCheckLayerAvailability"
        ]
      }
    ]
  })
}
