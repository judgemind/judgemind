# IAM role and policies for the Claude Code agent process.
#
# This role is assumed by the operator's IAM user via sts:AssumeRole
# (account-delegation pattern). The operator's IAM user policy restricts
# which humans can actually assume it; the trust policy allows any principal
# in the dev account root.
#
# Scoped to dev only — no staging or production. The role grants:
#   - ECS read/write on the dev cluster and its services/tasks
#   - CloudWatch Logs reads on dev log groups (/ecs/judgemind-*-dev)
#   - S3 read/write/delete on staging/* and spotcheck/* prefixes only
#   - Secrets Manager GetSecretValue on judgemind/* secrets
#   - iam:PassRole narrow to the scraper role (and optionally maintenance role)
#
# No EC2 instance profile — this role is assumed interactively via
# sts:AssumeRole, never attached to compute.

resource "aws_iam_role" "agent" {
  name        = "judgemind-agent-${var.environment}"
  description = "Assumed by the Claude Code agent via sts:AssumeRole for dev ECS/CloudWatch/S3 operations"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${var.dev_account_id}:root" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

# ECS read/write on the dev cluster only.
# Resources are scoped to judgemind-dev cluster, services, tasks, and
# task-definition family — no prod cluster ARN appears anywhere in this policy.
resource "aws_iam_policy" "ecs_dev_only" {
  name        = "judgemind-agent-ecs-dev-only-${var.environment}"
  description = "Allows the agent to run, describe, list, and update ECS resources in the dev cluster"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # AWS requires Resource = "*" for RegisterTaskDefinition — the API does
        # not accept ARN scoping. DescribeTaskDefinition has the same constraint.
        # See infra/terraform/modules/dispatcher-daemon/main.tf for precedent.
        Sid    = "EcsTaskDefinitionGlobal"
        Effect = "Allow"
        Action = [
          "ecs:RegisterTaskDefinition",
          "ecs:DescribeTaskDefinition"
        ]
        Resource = "*"
      },
      {
        Sid    = "EcsDevCluster"
        Effect = "Allow"
        Action = [
          "ecs:RunTask",
          "ecs:DescribeTasks",
          "ecs:ListTasks",
          "ecs:DescribeServices",
          "ecs:ListServices",
          "ecs:UpdateService"
        ]
        Resource = [
          "arn:aws:ecs:us-west-2:${var.dev_account_id}:cluster/judgemind-dev",
          "arn:aws:ecs:us-west-2:${var.dev_account_id}:service/judgemind-dev/*",
          "arn:aws:ecs:us-west-2:${var.dev_account_id}:task/judgemind-dev/*",
          "arn:aws:ecs:us-west-2:${var.dev_account_id}:task-definition/*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_dev_only" {
  role       = aws_iam_role.agent.name
  policy_arn = aws_iam_policy.ecs_dev_only.arn
}

# CloudWatch Logs reads on dev log groups only.
# Pattern /ecs/judgemind-*-dev constrains to dev — prod groups are
# /ecs/judgemind-*-production and are not matched.
resource "aws_iam_policy" "logs_dev_groups" {
  name        = "judgemind-agent-logs-dev-groups-${var.environment}"
  description = "Allows the agent to read CloudWatch log groups for dev ECS tasks"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "LogsDevGroups"
        Effect = "Allow"
        Action = [
          "logs:GetLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
          "logs:FilterLogEvents",
          "logs:StartQuery",
          "logs:GetQueryResults",
          "logs:StopQuery"
        ]
        Resource = [
          "arn:aws:logs:us-west-2:${var.dev_account_id}:log-group:/ecs/judgemind-*-dev:*",
          "arn:aws:logs:us-west-2:${var.dev_account_id}:log-group:/ecs/judgemind-*-dev"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "logs_dev_groups" {
  role       = aws_iam_role.agent.name
  policy_arn = aws_iam_policy.logs_dev_groups.arn
}

# S3 read/write/delete scoped to staging/* and spotcheck/* prefixes only.
# No access to raw/, derived/, or any production bucket.
resource "aws_iam_policy" "s3_staging_prefixes" {
  name        = "judgemind-agent-s3-staging-prefixes-${var.environment}"
  description = "Allows the agent to read, write, and delete S3 objects under staging/ and spotcheck/ prefixes"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowObjectOps"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject"
        ]
        Resource = [
          "${var.document_archive_bucket_arn}/staging/*",
          "${var.document_archive_bucket_arn}/spotcheck/*"
        ]
      },
      {
        Sid      = "AllowListBucket"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = var.document_archive_bucket_arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["staging/*", "spotcheck/*", "staging", "spotcheck"]
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "s3_staging_prefixes" {
  role       = aws_iam_role.agent.name
  policy_arn = aws_iam_policy.s3_staging_prefixes.arn
}

# Secrets Manager read on judgemind/* secrets only.
resource "aws_iam_policy" "secrets_judgemind" {
  name        = "judgemind-agent-secrets-judgemind-${var.environment}"
  description = "Allows the agent to read Secrets Manager secrets under the judgemind/ prefix"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "SecretsJudgemind"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = "arn:aws:secretsmanager:us-west-2:${var.dev_account_id}:secret:judgemind/*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "secrets_judgemind" {
  role       = aws_iam_role.agent.name
  policy_arn = aws_iam_policy.secrets_judgemind.arn
}

# iam:PassRole narrow to the scraper role only.
# Required so the agent can pass the scraper role when launching ECS tasks
# via RunTask. Pinned to the scraper ARN — no wildcard.
resource "aws_iam_policy" "pass_role_narrow_scraper" {
  name        = "judgemind-agent-pass-role-scraper-${var.environment}"
  description = "Allows the agent to pass the scraper IAM role when launching ECS tasks"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "PassRoleScraper"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = var.scraper_role_arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "pass_role_narrow_scraper" {
  role       = aws_iam_role.agent.name
  policy_arn = aws_iam_policy.pass_role_narrow_scraper.arn
}

# Optional: iam:PassRole for the maintenance role.
# Only created when maintenance_role_arn is non-empty.
resource "aws_iam_policy" "pass_role_narrow_maintenance" {
  count = var.maintenance_role_arn != "" ? 1 : 0

  name        = "judgemind-agent-pass-role-maintenance-${var.environment}"
  description = "Allows the agent to pass the maintenance IAM role when launching ECS tasks"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "PassRoleMaintenance"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = var.maintenance_role_arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "pass_role_narrow_maintenance" {
  count = var.maintenance_role_arn != "" ? 1 : 0

  role       = aws_iam_role.agent.name
  policy_arn = aws_iam_policy.pass_role_narrow_maintenance[0].arn
}
