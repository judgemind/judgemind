# API service module — ECS Fargate service behind an ALB.
#
# Provisions an Application Load Balancer in public subnets with an HTTPS
# listener (ACM certificate), an ECS Fargate service in private subnets,
# security groups, and a CloudWatch log group.

data "aws_region" "current" {}

# ─── CloudWatch Log Group ───────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/judgemind-api-${var.environment}"
  retention_in_days = var.log_retention_days
}

# ─── ACM Certificate ────────────────────────────────────────────────────────
# DNS validation — the CNAME record must be created in Cloudflare (or whichever
# DNS provider manages the zone). The certificate is used by the ALB HTTPS
# listener.

resource "aws_acm_certificate" "api" {
  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# ─── ALB Security Group ─────────────────────────────────────────────────────

resource "aws_security_group" "alb" {
  name        = "judgemind-api-alb-${var.environment}"
  description = "API ALB - inbound HTTPS from internet"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTPS from internet"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTP from internet (redirect to HTTPS)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "To ECS tasks"
    from_port   = var.container_port
    to_port     = var.container_port
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ─── ECS Task Security Group ────────────────────────────────────────────────

resource "aws_security_group" "api_task" {
  name        = "judgemind-api-task-${var.environment}"
  description = "API ECS tasks - inbound from ALB, outbound to RDS/Redis/OpenSearch"
  vpc_id      = var.vpc_id

  ingress {
    description     = "From ALB"
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    description = "HTTPS to OpenSearch and S3"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Redis"
    from_port   = 6379
    to_port     = 6379
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "PostgreSQL"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ─── Application Load Balancer ──────────────────────────────────────────────

resource "aws_lb" "api" {
  name               = "judgemind-api-${var.environment}"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids
}

resource "aws_lb_target_group" "api" {
  name        = "judgemind-api-${var.environment}"
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    enabled             = true
    path                = "/health"
    port                = "traffic-port"
    protocol            = "HTTP"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200"
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate.api.arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# ─── JWT Secret ─────────────────────────────────────────────────────────────

resource "random_password" "jwt_secret" {
  length           = 64
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "aws_secretsmanager_secret" "jwt" {
  name                    = "judgemind/${var.environment}/api/jwt_secret"
  description             = "JWT signing secret for Judgemind API (${var.environment})"
  recovery_window_in_days = var.environment == "production" ? 30 : 0
}

# NOTE: This secret_version IS Terraform-managed — the value is derived from
# a random_password resource. Do NOT add ignore_changes here; Terraform must
# keep the secret in sync with the generated password.
resource "aws_secretsmanager_secret_version" "jwt" {
  secret_id     = aws_secretsmanager_secret.jwt.id
  secret_string = jsonencode({ secret = random_password.jwt_secret.result })
}

# ─── IAM: Secrets Manager access for the execution role ─────────────────────
# The execution role needs to read secrets to inject DATABASE_URL and
# OpenSearch credentials into the container at launch.

resource "aws_iam_role_policy" "api_secrets" {
  name = "judgemind-api-secrets-${var.environment}"
  role = element(split("/", var.execution_role_arn), length(split("/", var.execution_role_arn)) - 1)

  # `compact()` drops empty-string ARNs so environments that don't wire a
  # given optional secret (e.g. a fresh stack with no OpenSearch creds)
  # still produce a valid policy. Matches the `execution_secrets` pattern
  # in `modules/dispatcher-daemon/main.tf`.
  #
  # Note: the API container used to also read
  # `judgemind/dispatcher/github-token` (#2818) for the `/admin/dispatcher`
  # GraphQL resolvers' GitHub enrichment path. Issue #2820 moved that
  # enrichment into the daemon, deleted `packages/api/src/graphql/
  # dispatcher/github.ts`, and tore out the `GITHUB_TOKEN` secret
  # wiring here so the API container cannot silently re-grow a
  # rate-limit-burning fetch path. Absence of the credential is the
  # strongest guarantee that absence of the fetch code is load-bearing.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadAPISecrets"
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        Resource = compact([
          var.db_connection_secret_arn,
          var.opensearch_credentials_secret_arn,
          aws_secretsmanager_secret.jwt.arn,
        ])
      }
    ]
  })
}

# ─── IAM: Task role for the API container ──────────────────────────────────
# The task role is assumed by the container at runtime (as opposed to the
# execution role, which is used by the ECS agent). The API needs SES
# permissions to send transactional emails (verification, password reset).

resource "aws_iam_role" "api_task" {
  name        = "judgemind-api-task-${var.environment}"
  description = "Assumed by the API container at runtime for SES email sending"

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

resource "aws_iam_role_policy" "api_ses" {
  name = "judgemind-api-ses-${var.environment}"
  role = aws_iam_role.api_task.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowSESSend"
        Effect = "Allow"
        Action = [
          "ses:SendEmail",
          "ses:SendRawEmail",
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "ses:FromAddress" = var.email_from
          }
        }
      }
    ]
  })
}

# ─── IAM: S3 read access for document downloads ──────────────────────────
# The document download endpoint generates presigned S3 URLs. The task role
# must have s3:GetObject permission on the archive bucket for these URLs to
# work when the client follows the redirect.

resource "aws_iam_role_policy" "api_s3_read" {
  count = var.document_archive_bucket_arn != "" ? 1 : 0

  name = "judgemind-api-s3-read-${var.environment}"
  role = aws_iam_role.api_task.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowDocumentDownload"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:HeadObject",
        ]
        Resource = "${var.document_archive_bucket_arn}/*"
      }
    ]
  })
}

# ─── ECS Task Definition ───────────────────────────────────────────────────

resource "aws_ecs_task_definition" "api" {
  family                   = "judgemind-api-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = aws_iam_role.api_task.arn

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = "${var.ecr_repository_url}:${var.image_tag}"
      essential = true

      portMappings = [
        {
          containerPort = var.container_port
          protocol      = "tcp"
        }
      ]

      # GITHUB_TOKEN was intentionally removed from this task-def in
      # #2820. See the comment on `aws_iam_role_policy.api_secrets`
      # above for the rationale — the API must not have the ability to
      # burn the shared PAT budget. Daemon-side enrichment populates
      # `dispatcher.queue_snapshots.issues_json` /
      # `dispatcher.blocked_snapshots.issues_json` /
      # `dispatcher.agents.issue_title`, and the admin-page resolvers
      # read those DB columns directly.
      secrets = concat(
        [
          {
            name      = "DATABASE_URL"
            valueFrom = "${var.db_connection_secret_arn}:url::"
          },
          {
            name      = "JWT_SECRET"
            valueFrom = "${aws_secretsmanager_secret.jwt.arn}:secret::"
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
        ] : []
      )

      environment = concat(
        [
          { name = "NODE_ENV", value = "production" },
          { name = "PORT", value = tostring(var.container_port) },
        ],
        var.redis_url != "" ? [{ name = "REDIS_URL", value = var.redis_url }] : [],
        var.opensearch_url != "" ? [{ name = "OPENSEARCH_URL", value = var.opensearch_url }] : [],
        var.cors_allowed_origins != "" ? [{ name = "CORS_ALLOWED_ORIGINS", value = var.cors_allowed_origins }] : [],
        var.ses_configuration_set_name != "" ? [{ name = "SES_CONFIGURATION_SET", value = var.ses_configuration_set_name }] : [],
        [{ name = "EMAIL_FROM", value = var.email_from }]
      )

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.api.name
          "awslogs-region"        = data.aws_region.current.id
          "awslogs-stream-prefix" = "api"
        }
      }
    }
  ])
}

# ─── SSM: terraform-managed container_definitions for deploy-api ────────────
# Stores the rendered container_definitions JSON that terraform considers the
# desired state. The deploy-api workflow's `ecs-deploy` composite action reads
# this parameter (instead of the running task-def) when registering a new
# revision, so secrets / env vars terraform has removed can never leak back
# in via the legacy "preserve current task-def content" path. See #3765 /
# parent #2840 for the bug class this prevents.
#
# The parameter is updated on every terraform apply that produces a new
# `container_definitions` rendering. deploy-api uses the parameter value as
# the base for its register-task-definition call, substitutes the new image
# URI into the named container, and registers. This makes terraform the
# single source of truth for the non-image fields (secrets, env, log config,
# port mappings, etc.).
resource "aws_ssm_parameter" "container_definitions" {
  name        = "/judgemind/api/${var.environment}/container-definitions"
  description = "Terraform-rendered container_definitions JSON for the ${var.environment} API task. Read by .github/actions/ecs-deploy when --desired-container-definitions-ssm-parameter is set. See #3765 / #2840."
  type        = "String"
  # Tier "Advanced" supports values up to 8KB (Standard caps at 4KB). The
  # rendered JSON for the API container is currently <2KB but Advanced gives
  # headroom for future env-var / secret additions without a tier bump.
  tier  = "Advanced"
  value = aws_ecs_task_definition.api.container_definitions
}

# ─── ECS Service ────────────────────────────────────────────────────────────

resource "aws_ecs_service" "api" {
  name            = "judgemind-api-${var.environment}"
  cluster         = var.ecs_cluster_arn
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.api_task.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = var.container_port
  }

  # Ignore task_definition changes so CI/CD image tag updates don't cause
  # Terraform drift.
  lifecycle {
    ignore_changes = [task_definition]
  }

  depends_on = [aws_lb_listener.https]
}

# ─── CloudWatch Alarms ────────────────────────────────────────────────────────
# ALB-based alarms for HTTP error rates, latency, and host health. These use
# metrics published automatically by the ALB — no application code changes
# needed.

locals {
  # The ALB ARN suffix is the portion after "loadbalancer/". CloudWatch ALB
  # metrics use this as the dimension value.
  alb_arn_suffix = aws_lb.api.arn_suffix
  tg_arn_suffix  = aws_lb_target_group.api.arn_suffix
}

resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  count = var.enable_alerts ? 1 : 0

  alarm_name        = "judgemind-api-5xx-${var.environment}"
  alarm_description = "API 5xx error rate exceeded ${var.error_5xx_threshold} in 5 minutes (${var.environment})"

  namespace   = "AWS/ApplicationELB"
  metric_name = "HTTPCode_Target_5XX_Count"
  statistic   = "Sum"
  dimensions = {
    LoadBalancer = local.alb_arn_suffix
    TargetGroup  = local.tg_arn_suffix
  }

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = var.error_5xx_threshold
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [var.alert_sns_topic_arn]
  ok_actions    = [var.alert_sns_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "api_4xx" {
  count = var.enable_alerts ? 1 : 0

  alarm_name        = "judgemind-api-4xx-spike-${var.environment}"
  alarm_description = "API 4xx error rate exceeded ${var.error_4xx_threshold} in 5 minutes (${var.environment})"

  namespace   = "AWS/ApplicationELB"
  metric_name = "HTTPCode_Target_4XX_Count"
  statistic   = "Sum"
  dimensions = {
    LoadBalancer = local.alb_arn_suffix
    TargetGroup  = local.tg_arn_suffix
  }

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = var.error_4xx_threshold
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [var.alert_sns_topic_arn]
  ok_actions    = [var.alert_sns_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "api_latency_p99" {
  count = var.enable_alerts ? 1 : 0

  alarm_name        = "judgemind-api-latency-p99-${var.environment}"
  alarm_description = "API P99 latency exceeded ${var.latency_p99_threshold}s (${var.environment})"

  namespace   = "AWS/ApplicationELB"
  metric_name = "TargetResponseTime"
  dimensions = {
    LoadBalancer = local.alb_arn_suffix
    TargetGroup  = local.tg_arn_suffix
  }

  extended_statistic  = "p99"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = var.latency_p99_threshold
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  treat_missing_data  = "notBreaching"

  alarm_actions = [var.alert_sns_topic_arn]
  ok_actions    = [var.alert_sns_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "api_unhealthy_hosts" {
  count = var.enable_alerts ? 1 : 0

  alarm_name        = "judgemind-api-unhealthy-hosts-${var.environment}"
  alarm_description = "API has unhealthy hosts behind the ALB (${var.environment})"

  namespace   = "AWS/ApplicationELB"
  metric_name = "UnHealthyHostCount"
  statistic   = "Maximum"
  dimensions = {
    LoadBalancer = local.alb_arn_suffix
    TargetGroup  = local.tg_arn_suffix
  }

  comparison_operator = "GreaterThanThreshold"
  threshold           = var.unhealthy_host_threshold
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  treat_missing_data  = "notBreaching"

  alarm_actions = [var.alert_sns_topic_arn]
  ok_actions    = [var.alert_sns_topic_arn]
}
