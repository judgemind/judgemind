# Negative-case fixture for the compute module's content-level
# postcondition. Asserts that a rendered container_definitions JSON
# missing a required secret entry (when its ARN variable is non-empty)
# is rejected at plan time.
#
# Background — see #3764 / parent #2840: during the 2026-04-19 Phase-3
# cutover, `terraform apply` silently produced a task-def revision
# *without* `ANTHROPIC_API_KEY` in its `secrets` array, despite the
# HCL conditional being correct AND the ARN variable being non-empty.
# The existing variable-level precondition only catches the ARN-empty
# case; a stale data-source evaluation or provider content-hash quirk
# that drops the secret from the rendered JSON while leaving the ARN
# var populated would go undetected.
#
# This fixture stands up an `aws_ecs_task_definition` resource that
# mirrors the compute module's scraper postcondition pattern (without
# going through the module) but with a rendered `container_definitions`
# that intentionally drops the DATABASE_URL secret entry while keeping
# `var.fake_db_arn` non-empty. `terraform plan` must fail with the
# postcondition error message.

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

variable "fake_db_arn" {
  type    = string
  default = "arn:aws:secretsmanager:us-west-2:123456789012:secret:judgemind/dev/db/connection-AAAAAA"
}

variable "fake_courtlistener_arn" {
  type    = string
  default = "arn:aws:secretsmanager:us-west-2:123456789012:secret:judgemind/dev/courtlistener/api-token-AAAAAA"
}

resource "aws_iam_role" "fake_exec" {
  name = "judgemind-compute-test-exec"
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
  name = "judgemind-compute-test-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Mirror the compute module's scraper content-level postcondition:
# when `var.fake_db_arn` is non-empty, DATABASE_URL must appear in
# the rendered container_definitions JSON. The fixture below
# intentionally drops DATABASE_URL from the secrets list while keeping
# `var.fake_db_arn` non-empty, so the postcondition must fail.
resource "aws_ecs_task_definition" "broken" {
  family                   = "judgemind-compute-test-broken"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.fake_exec.arn
  task_role_arn            = aws_iam_role.fake_task.arn

  container_definitions = jsonencode([
    {
      name      = "scraper"
      image     = "fake:latest"
      essential = true
      # Only COURTLISTENER_API_TOKEN — DATABASE_URL is intentionally
      # missing so the postcondition below fires.
      secrets = [
        {
          name      = "COURTLISTENER_API_TOKEN"
          valueFrom = var.fake_courtlistener_arn
        },
      ]
    }
  ])

  lifecycle {
    postcondition {
      condition = (
        var.fake_db_arn == "" ||
        strcontains(self.container_definitions, "DATABASE_URL")
      )
      error_message = "rendered container_definitions is missing DATABASE_URL despite db_connection_secret_arn being set."
    }
    postcondition {
      condition = (
        var.fake_courtlistener_arn == "" ||
        strcontains(self.container_definitions, "COURTLISTENER_API_TOKEN")
      )
      error_message = "rendered container_definitions is missing COURTLISTENER_API_TOKEN despite courtlistener_api_token_secret_arn being set."
    }
  }
}
