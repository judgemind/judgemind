# ECR repository for scraper container images.
#
# The scraper framework is packaged as a Docker image and pushed here by CI.
# ECS Fargate pulls images from this repository to run scheduled scraper tasks.
#
# Repository name follows the org/service pattern (judgemind/scraper). A single
# repository is shared across environments via image tags (e.g. staging, latest).

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

resource "aws_ecr_repository" "scraper" {
  name                 = "judgemind/scraper"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    project     = "judgemind"
    environment = var.environment
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
  count      = var.ecs_task_execution_role_arn != null ? 1 : 0
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
