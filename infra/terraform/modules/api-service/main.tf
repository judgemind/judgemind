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

  tags = {
    project     = "judgemind"
    environment = var.environment
  }
}

# ─── ACM Certificate ────────────────────────────────────────────────────────
# DNS validation — the CNAME record must be created in Cloudflare (or whichever
# DNS provider manages the zone). The certificate is used by the ALB HTTPS
# listener.

resource "aws_acm_certificate" "api" {
  domain_name       = var.domain_name
  validation_method = "DNS"

  tags = {
    project     = "judgemind"
    environment = var.environment
  }

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

  tags = {
    project     = "judgemind"
    environment = var.environment
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

  tags = {
    project     = "judgemind"
    environment = var.environment
  }
}

# ─── Application Load Balancer ──────────────────────────────────────────────

resource "aws_lb" "api" {
  name               = "judgemind-api-${var.environment}"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids

  tags = {
    project     = "judgemind"
    environment = var.environment
  }
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

  tags = {
    project     = "judgemind"
    environment = var.environment
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

# ─── IAM: Secrets Manager access for the execution role ─────────────────────
# The execution role needs to read secrets to inject DATABASE_URL and
# OpenSearch credentials into the container at launch.

resource "aws_iam_role_policy" "api_secrets" {
  name = "judgemind-api-secrets-${var.environment}"
  role = element(split("/", var.execution_role_arn), length(split("/", var.execution_role_arn)) - 1)

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
        ])
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
        ] : []
      )

      environment = concat(
        [
          { name = "NODE_ENV", value = "production" },
          { name = "PORT", value = tostring(var.container_port) },
        ],
        var.redis_url != "" ? [{ name = "REDIS_URL", value = var.redis_url }] : [],
        var.opensearch_url != "" ? [{ name = "OPENSEARCH_URL", value = var.opensearch_url }] : [],
        var.cors_allowed_origins != "" ? [{ name = "CORS_ALLOWED_ORIGINS", value = var.cors_allowed_origins }] : []
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

  tags = {
    project     = "judgemind"
    environment = var.environment
  }
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

  tags = {
    project     = "judgemind"
    environment = var.environment
  }
}
