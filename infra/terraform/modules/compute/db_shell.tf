# ─── db-shell: a small, always-on target for scripts/dev-db-query.sh ────────
#
# scripts/dev-db-query.sh runs each query through ECS Exec: it starts a
# python3 + psycopg process plus an SSM session worker inside a running
# container. Until #5168 that container was the live ingestion worker, so
# every query spent 20-40 MB of the worker's 512 MB in the worker's own
# cgroup (it triggered the OOM kill in #5147), and queries failed with
# TargetNotConnectedException whenever the worker restarted.
#
# This service exists only to be exec'd into. It runs the ingestion worker's
# image (which already ships python3, psycopg and boto3) with `sleep infinity`
# as its command, so it holds no work of its own and never restarts on a
# worker deploy. The query runner is uploaded per query, so the image going
# stale between restarts does not matter.
#
# Options weighed in #5168:
#   - a one-off Fargate task per query: no idle cost, but a measured 95 s per
#     `SELECT 1` (image pull + provisioning) against ~7 s over ECS Exec;
#   - a sidecar on the worker task: shares the worker's task size and still
#     restarts with every worker deploy;
#   - this always-on service: 0.25 vCPU / 0.5 GB on FARGATE_SPOT, a small
#     fixed cost (about a third of the on-demand price). A Spot interruption
#     means a minute or two without the service while ECS places a new task;
#     dev-db-query.sh already polls list-tasks and the exec agent through
#     that window.
#
# Gated on var.enable_db_shell (dev only). Production is capture-only and
# has no database (#4971).

locals {
  deploy_db_shell = var.enable_db_shell && local.deploy_ingestion
}

# FARGATE_SPOT must be associated with the cluster before a service can use
# it. No default strategy is set, so every service and run-task that names
# launch_type FARGATE keeps doing exactly what it did before.
resource "aws_ecs_cluster_capacity_providers" "main" {
  count = local.deploy_db_shell ? 1 : 0

  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]
}

resource "aws_cloudwatch_log_group" "db_shell" {
  count = local.deploy_db_shell ? 1 : 0

  name              = "/ecs/judgemind-db-shell-${var.environment}"
  retention_in_days = var.log_retention_days
}

# Outbound only: Postgres for the query, HTTPS for S3 (large results are
# staged under tmp/dev-db-query/ in the archive bucket) and for the SSM
# endpoints ECS Exec uses. The database security group admits the whole VPC
# CIDR on 5432, so no ingress rule is needed there.
resource "aws_security_group" "db_shell" {
  count = local.deploy_db_shell ? 1 : 0

  name        = "judgemind-db-shell-${var.environment}"
  description = "db-shell ECS task for dev-db-query.sh - outbound to Postgres and HTTPS"
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS to S3 and the SSM endpoints used by ECS Exec"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "PostgreSQL database"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_ecs_task_definition" "db_shell" {
  count = local.deploy_db_shell ? 1 : 0

  family                   = "judgemind-db-shell-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  # The ingestion worker's task role: it already carries the ECS Exec SSM
  # policy (aws_iam_role_policy.ecs_exec_ssm) and S3 write access to the
  # archive bucket, which is what the query runner needs.
  task_role_arn = var.scraper_task_role_arn

  container_definitions = jsonencode([
    {
      name      = "db-shell"
      image     = "${var.ecr_repository_url}:${var.scraper_image_tag}"
      essential = true

      # The image's ENTRYPOINT is `python -m`; replace it so the container
      # just idles. initProcessEnabled puts an init process at PID 1, which
      # forwards SIGTERM to `sleep` (so the task stops promptly) and reaps
      # the SSM session processes ECS Exec leaves behind.
      entryPoint  = ["sleep"]
      command     = ["infinity"]
      stopTimeout = 5
      linuxParameters = {
        initProcessEnabled = true
      }

      secrets = [
        {
          name      = "DATABASE_URL"
          valueFrom = "${var.db_connection_secret_arn}:url::"
        }
      ]

      environment = concat(
        [{ name = "ENVIRONMENT", value = var.environment }],
        var.document_archive_bucket != "" ? [{ name = "JUDGEMIND_ARCHIVE_BUCKET", value = var.document_archive_bucket }] : []
      )

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = "/ecs/judgemind-db-shell-${var.environment}"
          "awslogs-region"        = data.aws_region.current.id
          "awslogs-stream-prefix" = "db-shell"
        }
      }
    }
  ])

  lifecycle {
    postcondition {
      condition     = strcontains(self.container_definitions, "DATABASE_URL")
      error_message = "compute/db-shell: rendered container_definitions is missing DATABASE_URL; dev-db-query.sh cannot connect without it (#5168)."
    }
  }
}

resource "aws_ecs_service" "db_shell" {
  count = local.deploy_db_shell ? 1 : 0

  name            = "judgemind-db-shell-${var.environment}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.db_shell[0].arn
  desired_count   = 1

  capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
  }

  # dev-db-query.sh reaches the container only through ECS Exec.
  enable_execute_command = true

  # Bring the new task up before stopping the old one, so a task-def change
  # does not leave a window with no db-shell.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.db_shell[0].id]
    assign_public_ip = false
  }

  depends_on = [
    aws_ecs_cluster_capacity_providers.main,
    aws_cloudwatch_log_group.db_shell,
  ]
}
