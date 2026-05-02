# Negative-case fixture for the dispatcher-v3-task-defs module's
# content-level postconditions. Three independent checks live in the
# real module (one per agent task-def), but they all use the same
# `strcontains(self.container_definitions, "X")` shape -- so a single
# fixture exercising one negative case is sufficient regression
# coverage.
#
# This fixture mirrors the dispatcher-agent-runner postconditions
# fixture (#3764). It stands up an aws_ecs_task_definition that
# intentionally drops ANTHROPIC_API_KEY from the rendered secrets
# block while keeping the corresponding ARN var non-empty -- the
# postcondition must fire and reject the plan.
#
# It also exercises the digest-pin postcondition by using a mutable
# `:latest` image reference instead of `@sha256:<digest>` -- a
# regression that drops the data.aws_ecr_image lookup must be
# detectable at plan time.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region                      = "us-west-2"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  access_key                  = "test"
  secret_key                  = "test"
}

variable "fake_anthropic_arn" {
  type    = string
  default = "arn:aws:secretsmanager:us-west-2:123456789012:secret:judgemind/anthropic/api-key-AAAAAA"
}

variable "fake_db_arn" {
  type    = string
  default = "arn:aws:secretsmanager:us-west-2:123456789012:secret:judgemind/dev/db/connection-AAAAAA"
}

variable "fake_github_arn" {
  type    = string
  default = "arn:aws:secretsmanager:us-west-2:123456789012:secret:judgemind/dispatcher/github-token-AAAAAA"
}

resource "aws_iam_role" "fake_exec" {
  name = "judgemind-v3-task-defs-test-exec"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role" "fake_task" {
  name = "judgemind-v3-task-defs-test-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Mirrors the dispatcher-v3-task-defs module's task-runner postcondition
# block (the agent-task variant -- ANTHROPIC_API_KEY + DATABASE_URL +
# GITHUB_TOKEN + digest-pin checks). The fixture intentionally drops
# ANTHROPIC_API_KEY from the secrets list while keeping
# `var.fake_anthropic_arn` non-empty, AND uses a mutable `:latest`
# image reference instead of `@sha256:<digest>` -- so both the
# missing-secret postcondition AND the digest-pin postcondition fire.
resource "aws_ecs_task_definition" "broken" {
  family                   = "judgemind-v3-task-defs-test-broken"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 4096
  memory                   = 16384
  execution_role_arn       = aws_iam_role.fake_exec.arn
  task_role_arn            = aws_iam_role.fake_task.arn

  container_definitions = jsonencode([
    {
      name = "task-runner"
      # Intentionally NOT digest-pinned -- the digest-pin postcondition
      # below must reject this.
      image     = "fake-repo:latest"
      essential = true
      command   = ["python", "-m", "dispatcher_v3.agent_runner"]

      # Only DATABASE_URL + GITHUB_TOKEN -- ANTHROPIC_API_KEY
      # intentionally missing so the first postcondition fires.
      secrets = [
        {
          name      = "DATABASE_URL"
          valueFrom = "${var.fake_db_arn}:url::"
        },
        {
          name      = "GITHUB_TOKEN"
          valueFrom = var.fake_github_arn
        },
      ]
    }
  ])

  lifecycle {
    postcondition {
      condition = (
        var.fake_anthropic_arn == "" ||
        strcontains(self.container_definitions, "ANTHROPIC_API_KEY")
      )
      error_message = "rendered container_definitions is missing ANTHROPIC_API_KEY despite anthropic_api_key_secret_arn being set."
    }
    postcondition {
      condition = (
        var.fake_db_arn == "" ||
        strcontains(self.container_definitions, "DATABASE_URL")
      )
      error_message = "rendered container_definitions is missing DATABASE_URL despite db_connection_secret_arn being set."
    }
    postcondition {
      condition = (
        var.fake_github_arn == "" ||
        strcontains(self.container_definitions, "GITHUB_TOKEN")
      )
      error_message = "rendered container_definitions is missing GITHUB_TOKEN despite github_token_secret_arn being set."
    }
    postcondition {
      condition     = strcontains(self.container_definitions, "@sha256:")
      error_message = "rendered image reference is not digest-pinned. v3 task-defs must never reference a mutable tag -- see #3754."
    }
  }
}
