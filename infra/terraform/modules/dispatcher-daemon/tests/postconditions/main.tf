# Negative-case fixture for the dispatcher-daemon's content-level
# postcondition. Asserts that a rendered container_definitions JSON
# missing a required secret entry (when its ARN variable is non-empty
# and desired_count > 0) is rejected at plan time.
#
# Background — see #3764 / parent #2840: during the 2026-04-19 Phase-3
# cutover, `terraform apply` silently produced a dispatcher task-def
# revision *without* `ANTHROPIC_API_KEY` in its `secrets` array,
# despite the HCL conditional being correct AND
# `var.anthropic_api_key_secret_arn` being non-empty. The existing
# variable-level precondition (#2838 / PR #3233) only catches the
# ARN-empty case; a stale data-source evaluation or provider
# content-hash quirk that drops the secret from the rendered JSON
# while leaving the ARN var populated would go undetected.
#
# This fixture stands up an `aws_ecs_task_definition` resource that
# mirrors the dispatcher's postcondition pattern (without going
# through the module — the module's postcondition is exercised
# implicitly by every dev plan/apply on touched secrets) but with a
# rendered `container_definitions` that intentionally drops the
# ANTHROPIC_API_KEY secret entry. `terraform plan` must fail with
# the postcondition error message.

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

resource "aws_iam_role" "fake_exec" {
  name = "judgemind-test-exec"
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
  name = "judgemind-test-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Mirror the dispatcher-daemon module's content-level postcondition:
# when desired_count > 0 and an ARN variable is set, the rendered
# container_definitions JSON MUST contain the corresponding env-var
# name. The fixture below intentionally drops `ANTHROPIC_API_KEY`
# from the secrets list while keeping `var.fake_anthropic_arn` non-
# empty, so the postcondition must fail.
resource "aws_ecs_task_definition" "broken" {
  family                   = "judgemind-dispatcher-test-broken"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.fake_exec.arn
  task_role_arn            = aws_iam_role.fake_task.arn

  container_definitions = jsonencode([
    {
      name      = "dispatcher"
      image     = "fake:latest"
      essential = true
      # Only DATABASE_URL — ANTHROPIC_API_KEY is intentionally missing
      # so the postcondition below fires.
      secrets = [
        {
          name      = "DATABASE_URL"
          valueFrom = "${var.fake_db_arn}:url::"
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
  }
}
