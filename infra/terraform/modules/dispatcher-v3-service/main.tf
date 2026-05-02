# Dispatcher v3 launcher ECS service module (F4, #3889).
#
# Stands up the long-running ECS Fargate service that runs the v3
# launcher loop. The service references the launcher task definition
# from F2 (#3887, modules/dispatcher-v3-task-defs); the IAM roles
# (launcher_role, agent_task_role, execution_role) come from F3 (#3921,
# modules/dispatcher-v3-iam) via the task definition itself.
#
# Why this module exists separately from F2:
#
#   * F2 registers the four v3 task definitions (launcher, task-runner,
#     diagnoser, scheduled-skill). Three of those four are one-shot
#     (launched by the launcher via `ecs:RunTask` per claim or per
#     EventBridge cron). The launcher is the only one that needs an
#     ECS service to keep it running 24/7 -- standing it up alongside
#     F2 would have implied F2 is "the launcher module" when it's
#     really the multi-task-def module.
#   * F4 lands AFTER F1, F2, F3 in the dependency chain. The image
#     (#3915), task definitions (#3887), and IAM roles (#3888) all
#     have to exist before the service can start a task.
#
# Cohabitation with v2 (spec section 8):
#
#   * The launcher's container reads `dispatcher.config.concurrency_cap_v3`
#     (a v3-specific config key, distinct from v2's `concurrency_cap`)
#     so v2 and v3 have independent caps during cohabitation. F4
#     deploys with `desired_count=1` AND the seed migration writes
#     `concurrency_cap_v3=0`, so the launcher boots, writes a heartbeat
#     `dispatcher.runs` row tagged `dispatcher_version='v3'`, observes
#     the queue, and claims nothing. Operator flips the cap manually
#     (spec section 9 step 3) when ready to smoke v3 at cap=1.
#   * The cross-daemon claim race is handled by the `status/in-progress`
#     label interlock (spec section 8.1) -- both daemons add the label
#     atomically before stripping `agent/ready`, so whichever wins the
#     label flip owns the issue.
#
# Why a single-replica service rather than `desired_count=0` + manual
# RunTask:
#
#   * The launcher needs to be running 24/7 to consume the queue. ECS
#     services give us ECS-managed restart on container exit, plus the
#     deployment workflow (push image -> register task-def revision ->
#     `update-service --force-new-deployment`) that v2's daemon already
#     uses. A `desired_count=0` service with manual RunTask launches
#     would re-implement that loop in operator runbooks.
#   * A standalone Fargate task launched by `aws ecs run-task` has no
#     restart-on-exit behaviour and no health-check integration, so a
#     single SIGKILL'd container leaves the queue unobserved until
#     someone notices.
#
# Cap > 1 is not allowed: the launcher is a singleton, the same way
# v2's dispatcher-daemon is. Overlapping replicas would each scan the
# queue and both call `ecs:RunTask` for the same `agent/ready` issue
# (the GitHub-side `status/in-progress` label race only protects
# against cross-daemon collisions, not v3-vs-v3).
#
# Resources created:
#
#   * `aws_security_group.launcher` -- egress-only. Mirrors the
#     dispatcher-daemon module's security-group ports (HTTPS for
#     GitHub/Anthropic/ECR/Secrets Manager/Telegram, Postgres for the
#     `dispatcher.*` writes, Redis for forward compat with future
#     event-bus subscriptions). No inbound -- the launcher is a queue
#     consumer, not a service endpoint.
#   * `aws_ecs_service.launcher` -- single-replica Fargate service
#     pinned to `var.task_definition_arn` (an immutable revision ARN
#     from F2). `lifecycle.ignore_changes = [task_definition]` lets
#     operator-driven redeploys (`update-service --force-new-deployment`)
#     bump the revision without Terraform churning every apply.
#
# Resources NOT created here (deliberately):
#
#   * Task definition -- registered by F2 (modules/dispatcher-v3-task-defs).
#   * Log group `/judgemind/dispatcher-v3/launcher` -- created by F2
#     alongside the task definition because the awslogs driver options
#     in the task-def JSON reference it.
#   * IAM roles -- shipped by F3 (modules/dispatcher-v3-iam) and
#     consumed by F2's task-def directly via `executionRoleArn` /
#     `taskRoleArn`. F4 never sees them.
#   * Heartbeat / circuit-breaker alarms -- a follow-up. The v2 daemon
#     module ships extensive CloudWatch alarms (`heartbeat_stale`,
#     `stuck_timeout_repeated`, supervisor-tick swallow alarms). v3
#     needs equivalents (`HeartbeatAge` for v3 will live under a
#     distinct dimension or namespace), but F4's scope is intentionally
#     narrow to land the singleton-running invariant first; alarms come
#     in a follow-up issue.

locals {
  service_name = var.service_name != "" ? var.service_name : "judgemind-dispatcher-v3-${var.environment}"
}

# --- Security group ---------------------------------------------------
# Mirrors modules/dispatcher-daemon/main.tf's security group ports. The
# launcher needs:
#
#   * HTTPS (443) -- GitHub API (issue queue + status/in-progress flips),
#     Anthropic (none today; the launcher does not invoke claude itself
#     -- only task-runner and diagnoser tasks do, and those tasks have
#     their own security group via the F2 task-def's networkMode), ECR
#     (image pull is via the execution role, not the task ENI -- but
#     keeping the egress rule aligns with v2 for forward compat),
#     Secrets Manager (TELEGRAM_BOT_TOKEN read via execution role at
#     task start; the runtime read goes via the data-plane IAM action,
#     not the task ENI -- but again, kept for v2 alignment), Telegram
#     Bot API (operator paging on stuck queues, spec section 6).
#   * Postgres (5432) -- writes to `dispatcher.runs`,
#     `dispatcher.agents`, `dispatcher.queue_snapshots`. The DB sits
#     in private subnets behind its own security group; the launcher's
#     egress allows the connection out, the DB's ingress allows it in.
#   * Redis (6379) -- reserved for forward compat with the event bus
#     (spec section 6 "EventBridge cron rules" mentions only the
#     EventBridge wiring, but a Redis subscriber pattern is on the
#     roadmap). Mirrors v2's port list verbatim.

resource "aws_security_group" "launcher" {
  name        = local.service_name
  description = "Dispatcher v3 launcher ECS task - outbound only (HTTPS, Postgres, Redis). No inbound -- the launcher is a queue consumer, not a service endpoint."
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS to GitHub, Anthropic, ECR, Secrets Manager, Telegram, S3"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "PostgreSQL (dispatcher.* state)"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Redis (event bus, reserved for future phases)"
    from_port   = 6379
    to_port     = 6379
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --- ECS Service ------------------------------------------------------
# Single-replica Fargate service pinned to the F2 launcher task
# definition's revision ARN. `lifecycle.ignore_changes = [task_definition]`
# is the same pattern v2's dispatcher-daemon uses: the deploy workflow
# pushes a new image tag, F2's data.aws_ecr_image resolves a fresh
# digest, F2 registers a new task-def revision -- and an explicit
# `aws ecs update-service --force-new-deployment` (or operator-driven
# `update-service --task-definition`) is what actually rolls the
# service. Without this lifecycle block, every F2 apply would force
# Terraform to issue an update-service call, which is awkward when the
# operator wants to control rollout timing.

resource "aws_ecs_service" "launcher" {
  name            = local.service_name
  cluster         = var.ecs_cluster_arn
  task_definition = var.task_definition_arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  # Enable ECS Exec so operators can attach an interactive shell to
  # the running launcher for ad-hoc psql / log inspection. The agent
  # task role's SSM permissions (F3) make the data plane work; this
  # flag wires the control plane.
  enable_execute_command = var.enable_execute_command

  # Singleton invariant: overlapping deploys would briefly run two
  # launchers, which would each scan the queue and call ecs:RunTask
  # for the same agent/ready issue. The 0/100 pair lets ECS start the
  # new task before stopping the old, but caps the window at one extra
  # task -- a momentary singleton violation we accept (vs the alternative
  # of a 0/0 pair which would leave the queue unobserved during the
  # entire deploy swap).
  deployment_minimum_healthy_percent = var.deployment_minimum_healthy_percent
  deployment_maximum_percent         = var.deployment_maximum_percent

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.launcher.id]
    assign_public_ip = false
  }

  lifecycle {
    ignore_changes = [task_definition]
  }
}
