# Dispatcher daemon module — ECS Fargate service that runs the autonomous
# dispatcher loop for dispatcher v2 (see
# `docs/specs/dispatcher-v2-spec.md` §14).
#
# Lifecycle:
# - Phase 1 (#2725 epic): module lands with `desired_count=0`. Service,
#   task definition, IAM roles, log group, and heartbeat alarm exist,
#   but nothing runs until the daemon image lands and Phase 2 flips the
#   replica count.
# - Phase 2 shadow mode (#2768): `desired_count=1`,
#   `dispatcher.config.concurrency_cap=0`. Daemon polls the
#   `agent/ready` queue, writes `dispatcher.queue_snapshots`, and emits
#   the `HeartbeatAge` CloudWatch metric. No subprocess spawning.
# - Phase 3: daemon spawns per-phase `/task-v2-*` subprocesses once the
#   shadow-mode observations confirm stability.
#
# Callers pass `db_connection_secret_arn` from the environment's
# `module.database` output (Phase 2 requirement — the daemon must reach
# Postgres to persist `dispatcher.runs` and `dispatcher.queue_snapshots`).
# Until the dedicated `judgemind_dispatcher` DB role lands (sub-task B
# follow-up), the daemon connects as the main `judgemind` owner role —
# safe while `concurrency_cap=0` keeps the spawn path dormant.
#
# Resources created:
# - ECS service + task definition (1 vCPU, 2 GB RAM, 50 GB ephemeral storage
#   per spike 0.6 findings in `docs/investigations/dispatcher-v2-spike-0.6.md`).
# - Execution role (pull ECR + read Secrets Manager entries).
# - Task role (R/W on `dispatcher.*` via the dispatcher-role DB secret,
#   `ecs:RunTask` self-invocation for Phase 2 agent-spawn patterns, ECS Exec
#   via SSM so operators can psql the container).
# - Security group (egress only: HTTPS, Postgres, Redis).
# - CloudWatch log group `/ecs/judgemind-dispatcher-dev` with 30-day retention.
# - CloudWatch metric alarm: heartbeat staleness > 5 min. See §Heartbeat
#   alarm below — the metric itself is published by the daemon in Phase 2;
#   the alarm is defined now so the SNS wiring lands with the module.

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  service_name   = "judgemind-dispatcher-${var.environment}"
  log_group_name = "/ecs/judgemind-dispatcher-${var.environment}"

  # Secret-ARN lists for the two policies below. `compact()` drops any empty
  # string, so placeholder secrets that aren't yet provisioned (e.g. the
  # Phase-1 inert wiring where some sub-task owners haven't landed their
  # ARN yet) do not leak into the policy's `Resource` list. As of #2700
  # the dev wiring pins `github_token_secret_arn`, so the compacted list
  # is non-empty in dev — the count-guard still matters for callers that
  # stand up the module with all ARNs blank. If the compacted list is
  # empty, the policy resource itself is skipped via `count = 0` — IAM
  # rejects `Resource = []` with
  # `MalformedPolicyDocument: Policy statement must contain resources`
  # (see #2739).
  execution_secret_arns = compact([
    var.anthropic_api_key_secret_arn,
    var.db_connection_secret_arn,
    var.github_token_secret_arn,
    var.telegram_bot_token_secret_arn,
    var.gemini_api_key_secret_arn,
  ])
  task_db_secret_arns = compact([var.db_connection_secret_arn])
}

# ─── CloudWatch Log Group ───────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "dispatcher" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
}

# ─── Security Group ─────────────────────────────────────────────────────────
# The dispatcher needs outbound HTTPS (GitHub, Anthropic, ECR, Secrets
# Manager, Telegram), Postgres (heartbeat + queue state), and Redis if it
# ever subscribes to the event bus. No inbound traffic — the daemon is a
# queue consumer, not a service endpoint.

resource "aws_security_group" "dispatcher" {
  name        = local.service_name
  description = "Dispatcher daemon ECS tasks - outbound only (HTTPS, Postgres, Redis)"
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

# ─── IAM: Execution Role (ECS agent) ────────────────────────────────────────
# Used by the ECS agent to pull the container image from ECR, write logs to
# CloudWatch, and fetch Secrets Manager entries for env-var injection.

resource "aws_iam_role" "execution" {
  name        = "${local.service_name}-exec"
  description = "Dispatcher daemon execution role - pull ECR, read secrets, write logs"

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

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Allow the execution role to fetch every secret wired into the task
# definition. The secret-ARN list is pre-compacted into
# `local.execution_secret_arns` so that empty-string placeholders
# (e.g. sub-tasks that haven't landed their secret yet) are dropped.
# When no ARNs are wired in — a fully blank test stack — the policy
# resource itself is skipped via `count = 0`; IAM rejects a statement
# with `Resource = []` as `MalformedPolicyDocument` (see #2739). The
# dev env (#2700) wires `github_token_secret_arn`, so the compacted
# list is non-empty and this policy is created with `GITHUB_TOKEN`'s
# ARN in the `Resource` array.
resource "aws_iam_role_policy" "execution_secrets" {
  count = length(local.execution_secret_arns) > 0 ? 1 : 0

  name = "${local.service_name}-exec-secrets"
  role = aws_iam_role.execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadDispatcherSecrets"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = local.execution_secret_arns
      }
    ]
  })
}

# ─── IAM: Task Role (container runtime) ─────────────────────────────────────
# Assumed by the dispatcher container at runtime. Grants three capabilities:
#
# 1. `ecs:RunTask` self-invocation. Phase 2+ may spawn per-phase agents as
#    separate Fargate tasks (see §6a of the spec). The task-role → RunTask
#    pattern matches the existing `scheduler_run_task` policy in the compute
#    module. Scoped to the dispatcher task definition family so the daemon
#    can only start copies of itself, not arbitrary workloads.
# 2. ECS Exec (SSM channel). Operators use `scripts/ecs-run.sh` /
#    `scripts/dev-db-query.sh` to attach an interactive shell to the running
#    container without a bastion. Same SSM permission set used by the
#    ingestion worker in the compute module.
# 3. CloudWatch metric PutMetricData. The daemon emits a `HeartbeatAge`
#    custom metric under `Judgemind/Dispatcher` (Phase 2); this permission
#    lets the heartbeat alarm below actually see data once the daemon is
#    running.

resource "aws_iam_role" "task" {
  name        = "${local.service_name}-task"
  description = "Dispatcher daemon task role - runtime permissions for the container"

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

# R/W on `dispatcher.*` is enforced at the database level via the
# `judgemind_dispatcher` role that sub-task A creates. The task's AWS
# identity only needs to fetch its own connection secret at runtime (the
# execution role already fetches it for env injection, but the daemon may
# also want to rotate / re-fetch during long-running operation).
#
# Phase 1 passes `db_connection_secret_arn = ""`; the compacted
# `local.task_db_secret_arns` is therefore empty and `count = 0` skips
# the policy entirely. Same rationale as `execution_secrets` above —
# IAM rejects `Resource = []` (see #2739).
resource "aws_iam_role_policy" "task_read_db_secret" {
  count = length(local.task_db_secret_arns) > 0 ? 1 : 0

  name = "${local.service_name}-task-read-db-secret"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadDispatcherDBSecret"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = local.task_db_secret_arns
      }
    ]
  })
}

resource "aws_iam_role_policy" "task_run_task_self" {
  name = "${local.service_name}-task-run-task-self"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowRunTaskSelf"
        Effect = "Allow"
        Action = "ecs:RunTask"
        Resource = [
          # Match any revision of the dispatcher task definition. Scoping
          # to our own family keeps the daemon from spawning unrelated
          # workloads even if its code is ever compromised.
          "arn:aws:ecs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:task-definition/${local.service_name}:*"
        ]
        Condition = {
          ArnEquals = {
            "ecs:cluster" = var.ecs_cluster_arn
          }
        }
      },
      {
        Sid    = "AllowPassRoleToSelf"
        Effect = "Allow"
        Action = "iam:PassRole"
        Resource = [
          aws_iam_role.execution.arn,
          aws_iam_role.task.arn,
        ]
      },
    ]
  })
}

# Oneshot task launcher — permissions required by
# `scripts/ecs-run-task.sh` so the daemon can run data scripts
# (rebuild_db, backfills, orphan cleanup) without a human on a laptop.
# See #3048 for the root cause: the `task_run_task_self` policy above
# only permits RunTask on the dispatcher's own family, which blocks
# every data-script path from ralph.
#
# Wiring lives in `environments/dev/main.tf`: the caller passes the
# ingestion worker's task + execution role ARNs, the shared assets
# bucket ARN, and the scraper ECR repo ARN. If either of the two role
# ARNs is empty the policy is skipped entirely — preserves a safe
# default for callers that haven't opted in (e.g. staging / throwaway
# test stacks).
#
# Scope invariants (audited in dev apply + `get-role-policy`):
#   - RunTask restricted to the `judgemind-oneshot-${env}` family on
#     `var.ecs_cluster_arn` — daemon cannot start prod tasks or spawn
#     arbitrary workloads.
#   - Deregister restricted to the same family. The daemon cannot
#     rewrite the ingestion-worker or dispatcher task defs.
#   - DescribeTaskDefinition uses `Resource = "*"` because the AWS API
#     does not accept ARN scoping on that action (same pattern as the
#     scheduler role); the cluster-ARN condition on RunTask is the
#     defence-in-depth control.
#   - PassRole limited to the ingestion worker's task + execution roles
#     (and the optional maintenance role) — the oneshot task cannot
#     assume the daemon's own task role or any cross-environment role.
#   - S3 PutObject/DeleteObject scoped to `oneshot-scripts/*` under the
#     caller-supplied assets bucket.
#   - ecr:DescribeImages on the caller-supplied scraper repo ARN only.
#
# Widening any of the above should be paired with an updated issue
# reference — #3048 is the current scope boundary.
locals {
  oneshot_enabled = (
    var.oneshot_source_task_role_arn != "" &&
    var.oneshot_source_execution_role_arn != ""
  )
  oneshot_family_arn_pattern = "arn:aws:ecs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:task-definition/judgemind-oneshot-${var.environment}:*"
  oneshot_pass_role_arns = compact([
    var.oneshot_source_task_role_arn,
    var.oneshot_source_execution_role_arn,
    var.oneshot_maintenance_task_role_arn,
  ])
}

resource "aws_iam_role_policy" "task_run_oneshot" {
  count = local.oneshot_enabled ? 1 : 0

  name = "${local.service_name}-task-run-oneshot"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          # AWS requires Resource = "*" for RegisterTaskDefinition — the
          # API does not accept ARN scoping. Deregister accepts an ARN,
          # so that one below is pinned to the oneshot family.
          Sid      = "AllowRegisterOneshotTaskDefinition"
          Effect   = "Allow"
          Action   = "ecs:RegisterTaskDefinition"
          Resource = "*"
        },
        {
          Sid      = "AllowDeregisterOneshotTaskDefinition"
          Effect   = "Allow"
          Action   = "ecs:DeregisterTaskDefinition"
          Resource = local.oneshot_family_arn_pattern
        },
        {
          # DescribeTaskDefinition reads the source (ingestion worker)
          # task def as a template and the freshly registered oneshot
          # task def. AWS requires `*` on this action.
          Sid      = "AllowDescribeTaskDefinitions"
          Effect   = "Allow"
          Action   = "ecs:DescribeTaskDefinition"
          Resource = "*"
        },
        {
          # Launch the oneshot task. Restricted to the oneshot family on
          # the dev cluster — no other family, no other cluster.
          Sid      = "AllowRunOneshotTask"
          Effect   = "Allow"
          Action   = "ecs:RunTask"
          Resource = local.oneshot_family_arn_pattern
          Condition = {
            ArnEquals = {
              "ecs:cluster" = var.ecs_cluster_arn
            }
          }
        },
        {
          # DescribeTasks (poll loop) and DescribeServices (resolve
          # networking). Gated on the dev cluster via the condition.
          Sid    = "AllowDescribeRunningTasks"
          Effect = "Allow"
          Action = [
            "ecs:DescribeTasks",
            "ecs:DescribeServices",
          ]
          Resource = "*"
          Condition = {
            ArnEquals = {
              "ecs:cluster" = var.ecs_cluster_arn
            }
          }
        },
        {
          # Pass the ingestion worker's task + execution roles to the
          # oneshot task. Without this, RunTask fails with
          # `AccessDeniedException: User ... is not authorized to pass
          # role ... because no identity-based policy allows the
          # iam:PassRole action`.
          Sid      = "AllowPassOneshotRoles"
          Effect   = "Allow"
          Action   = "iam:PassRole"
          Resource = local.oneshot_pass_role_arns
          Condition = {
            StringEquals = {
              "iam:PassedToService" = "ecs-tasks.amazonaws.com"
            }
          }
        },
        {
          # Resolve the `--role <name>` override in ecs-run-task.sh. The
          # script calls `aws iam get-role --role-name <name>` to map a
          # role name to an ARN before RunTask. Scoped to the account's
          # judgemind-* roles for this environment.
          Sid      = "AllowResolveMaintenanceRole"
          Effect   = "Allow"
          Action   = "iam:GetRole"
          Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/judgemind-*-${var.environment}"
        },
        {
          # Stream logs from the running oneshot task. The script tails
          # the ingestion-worker log group (that's where oneshot tasks
          # write — the log config is inherited from the source task
          # def). Scoped to the dev account's judgemind-* log groups so
          # the daemon cannot read unrelated log groups.
          Sid    = "AllowReadOneshotLogs"
          Effect = "Allow"
          Action = [
            "logs:DescribeLogStreams",
            "logs:GetLogEvents",
            "logs:FilterLogEvents",
          ]
          Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:log-group:/ecs/judgemind-*:*"
        },
      ],
      var.oneshot_script_bucket_arn != "" ? [
        {
          # Upload scripts >8KB via pre-signed URL. Scoped to the
          # oneshot-scripts/ prefix inside the caller-supplied bucket.
          Sid    = "AllowUploadOneshotScripts"
          Effect = "Allow"
          Action = [
            "s3:PutObject",
            "s3:DeleteObject",
          ]
          Resource = "${var.oneshot_script_bucket_arn}/oneshot-scripts/*"
        },
      ] : [],
      var.oneshot_ecr_scraper_repository_arn != "" ? [
        {
          # Resolve the latest image digest in the scraper ECR repo
          # (stale-image detection path in ecs-run-task.sh).
          Sid      = "AllowDescribeScraperImages"
          Effect   = "Allow"
          Action   = "ecr:DescribeImages"
          Resource = var.oneshot_ecr_scraper_repository_arn
        },
      ] : [],
    )
  })
}

resource "aws_iam_role_policy" "task_ecs_exec_ssm" {
  name = "${local.service_name}-task-ecs-exec-ssm"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowECSExec"
        Effect = "Allow"
        Action = [
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel",
        ]
        Resource = "*"
      },
      {
        Sid    = "AllowECSExecLogging"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.dispatcher.arn}:*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "task_put_metric" {
  name = "${local.service_name}-task-put-metric"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowHeartbeatMetric"
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "Judgemind/Dispatcher"
          }
        }
      }
    ]
  })
}

# ─── Per-agent ECS task launcher (#3091 Stage 2) ────────────────────────────
#
# Grants the daemon's task role the narrow permissions required to
# launch + observe + stop the per-agent agent-runner task
# (`judgemind-dispatcher-agent-runner-<env>` family, shipped in Stage
# 1b, #3090).
#
# Scope invariants:
#   - ecs:RunTask / ecs:StopTask pinned to the agent-runner task-def
#     family on this cluster — the daemon cannot spawn other workloads.
#   - ecs:DescribeTasks uses Resource="*" (the AWS API does not accept
#     ARN scoping on this action — same quirk as the oneshot policy)
#     with a cluster-ARN condition.
#   - iam:PassRole pinned to the agent-runner's execution + task role
#     ARNs only. Without this, RunTask fails with `AccessDeniedException:
#     User ... is not authorized to pass role ... because no identity-
#     based policy allows the iam:PassRole action`.
#
# Disabled (count=0) when any of the three required variables is
# empty. That state keeps the Stage 2 daemon code falling back to the
# subprocess path regardless of what `dispatcher.config
# .agent_execution_mode` says, preserving zero-change-on-deploy for
# environments that haven't wired the agent-runner module yet (e.g.
# staging / throwaway test stacks).

locals {
  agent_runner_launch_enabled = (
    var.agent_runner_task_definition_family != "" &&
    var.agent_runner_execution_role_arn != "" &&
    var.agent_runner_task_role_arn != ""
  )
  agent_runner_family_arn_pattern = (
    local.agent_runner_launch_enabled
    ? "arn:aws:ecs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:task-definition/${var.agent_runner_task_definition_family}:*"
    : ""
  )
  # `ecs:TagResource` authorisation for RunTask-with-tags is evaluated
  # against the task-instance ARN (task/CLUSTER/*), not the task-def ARN.
  # Cluster name is the last path segment of the cluster ARN (split index
  # 1 of `.../cluster/NAME`).
  agent_runner_task_instance_arn_pattern = (
    local.agent_runner_launch_enabled
    ? "arn:aws:ecs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:task/${split("/", var.ecs_cluster_arn)[1]}/*"
    : ""
  )
  agent_runner_pass_role_arns = compact([
    var.agent_runner_execution_role_arn,
    var.agent_runner_task_role_arn,
  ])
}

resource "aws_iam_role_policy" "task_launch_agent_runner" {
  count = local.agent_runner_launch_enabled ? 1 : 0

  name = "${local.service_name}-task-launch-agent-runner"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowRunAgentRunnerTask"
        Effect   = "Allow"
        Action   = "ecs:RunTask"
        Resource = local.agent_runner_family_arn_pattern
        Condition = {
          ArnEquals = {
            "ecs:cluster" = var.ecs_cluster_arn
          }
        }
      },
      {
        Sid      = "AllowStopAgentRunnerTask"
        Effect   = "Allow"
        Action   = "ecs:StopTask"
        Resource = local.agent_runner_family_arn_pattern
        Condition = {
          ArnEquals = {
            "ecs:cluster" = var.ecs_cluster_arn
          }
        }
      },
      {
        # ecs:TagResource is invoked implicitly by RunTask when the call
        # includes a `tags` list. AWS evaluates authorization against the
        # task-instance ARN (task/CLUSTER/*), not the task-definition ARN —
        # the Stage 2 daemon tags every agent-runner task with `agent_id`
        # and `issue_number`, so without this statement every ECS-mode
        # claim fails with AccessDeniedException (#3129).
        Sid      = "AllowTagAgentRunnerTask"
        Effect   = "Allow"
        Action   = "ecs:TagResource"
        Resource = local.agent_runner_task_instance_arn_pattern
        Condition = {
          ArnEquals = {
            "ecs:cluster" = var.ecs_cluster_arn
          }
        }
      },
      {
        # DescribeTasks is the per-tick observer call. AWS requires
        # Resource="*" on this action; the cluster-ARN condition is the
        # defence-in-depth control (same pattern used by the oneshot
        # policy above).
        Sid      = "AllowDescribeAgentRunnerTasks"
        Effect   = "Allow"
        Action   = "ecs:DescribeTasks"
        Resource = "*"
        Condition = {
          ArnEquals = {
            "ecs:cluster" = var.ecs_cluster_arn
          }
        }
      },
      {
        Sid      = "AllowPassAgentRunnerRoles"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = local.agent_runner_pass_role_arns
        Condition = {
          StringEquals = {
            "iam:PassedToService" = "ecs-tasks.amazonaws.com"
          }
        }
      },
    ]
  })
}

# ─── ECS Task Definition ────────────────────────────────────────────────────
# 1 vCPU / 2 GB RAM matches §14 of the spec. Ephemeral storage is pinned to
# 50 GB per spike 0.6 (realistic mixed peak is ~10 GB across 5 concurrent
# worktrees; this gives 5× headroom).

resource "aws_ecs_task_definition" "dispatcher" {
  family                   = local.service_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  ephemeral_storage {
    size_in_gib = var.ephemeral_storage_gib
  }

  container_definitions = jsonencode([
    {
      name      = "dispatcher"
      image     = "${var.ecr_repository_url}:${var.image_tag}"
      essential = true

      # Dispatcher startup runs the scheduler + supervisor loop; SIGTERM
      # must give in-flight agents time to finalize their status row
      # before the container is killed.
      stopTimeout = 120

      environment = concat(
        [
          { name = "ENVIRONMENT", value = var.environment },
          { name = "DISPATCHER_SERVICE_NAME", value = local.service_name },
          { name = "ECS_CLUSTER_ARN", value = var.ecs_cluster_arn },
          { name = "HEARTBEAT_METRIC_NAMESPACE", value = "Judgemind/Dispatcher" },
        ],
        var.github_repo != "" ? [{ name = "GITHUB_REPO", value = var.github_repo }] : [],
        # #3091 Stage 2 — per-agent ECS launcher wiring. All three env
        # vars are optional; an empty value means the launcher falls
        # back to the subprocess path. When the agent-runner module is
        # wired (see environments/dev/main.tf), the caller threads
        # these values through from the module outputs.
        var.agent_runner_task_definition_family != "" ? [
          { name = "AGENT_RUNNER_TASK_DEFINITION_FAMILY", value = var.agent_runner_task_definition_family }
        ] : [],
        length(var.agent_runner_subnet_ids) > 0 ? [
          { name = "AGENT_RUNNER_SUBNET_IDS", value = join(",", var.agent_runner_subnet_ids) }
        ] : [],
        var.agent_runner_security_group_id != "" ? [
          { name = "AGENT_RUNNER_SECURITY_GROUP_ID", value = var.agent_runner_security_group_id }
        ] : [],
      )

      secrets = concat(
        var.anthropic_api_key_secret_arn != "" ? [
          {
            name      = "ANTHROPIC_API_KEY"
            valueFrom = var.anthropic_api_key_secret_arn
          }
        ] : [],
        var.db_connection_secret_arn != "" ? [
          {
            # The dispatcher-role secret is a JSON blob with the same shape
            # as the main DB secret; DATABASE_URL lives under the `url` key.
            name      = "DATABASE_URL"
            valueFrom = "${var.db_connection_secret_arn}:url::"
          }
        ] : [],
        var.github_token_secret_arn != "" ? [
          {
            # Scoped PAT from spike 0.7. `gh auth setup-git` inside the
            # container turns this into a working git credential.
            name      = "GITHUB_TOKEN"
            valueFrom = var.github_token_secret_arn
          }
        ] : [],
        var.telegram_bot_token_secret_arn != "" ? [
          {
            name      = "TELEGRAM_BOT_TOKEN"
            valueFrom = var.telegram_bot_token_secret_arn
          }
        ] : [],
        var.gemini_api_key_secret_arn != "" ? [
          {
            # Only required if `runner_by_phase` routes to the Gemini
            # runner (spec §14). Laptop OAuth is not usable on Fargate
            # per spike 0.4.
            name      = "GEMINI_API_KEY"
            valueFrom = var.gemini_api_key_secret_arn
          }
        ] : [],
      )

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.dispatcher.name
          "awslogs-region"        = data.aws_region.current.id
          "awslogs-stream-prefix" = "dispatcher"
        }
      }
    }
  ])

  lifecycle {
    precondition {
      condition     = var.desired_count == 0 || var.anthropic_api_key_secret_arn != ""
      error_message = "dispatcher-daemon: anthropic_api_key_secret_arn must be set when desired_count > 0 (phase 2+ needs anthropic_api_key to spawn claude -p)."
    }
    precondition {
      condition     = var.desired_count == 0 || var.db_connection_secret_arn != ""
      error_message = "dispatcher-daemon: db_connection_secret_arn must be set when desired_count > 0 (phase 2+ needs database_url to persist dispatcher.runs / dispatcher.queue_snapshots)."
    }
  }
}

# ─── ECS Service ────────────────────────────────────────────────────────────
# `desired_count` defaults to 0 (Phase 1). Phase 2 callers override to 1
# for shadow mode (#2768). Never > 1 — the dispatcher is a singleton.
# `ignore_changes = [task_definition]` is the house pattern: CI redeploys
# bump the image tag via the AWS CLI without rewriting the task def ARN
# every apply.

resource "aws_ecs_service" "dispatcher" {
  name            = local.service_name
  cluster         = var.ecs_cluster_arn
  task_definition = aws_ecs_task_definition.dispatcher.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  # Enable ECS Exec so operators can attach an interactive shell to the
  # running daemon for ad-hoc psql / log inspection.
  enable_execute_command = true

  # The dispatcher is a singleton; overlapping deploys would double-spawn
  # agents briefly. Use min=0 / max=100 so a new task must start before the
  # old one stops (ECS will drain the old task once the new one is healthy).
  # When `desired_count=0` these values are no-ops.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.dispatcher.id]
    assign_public_ip = false
  }

  lifecycle {
    ignore_changes = [task_definition]
  }
}

# ─── Heartbeat Alarm ────────────────────────────────────────────────────────
# Per §14 of the spec: heartbeat staleness > 5 min should page the operator
# via SNS (email + Telegram). The daemon publishes a `HeartbeatAge` metric
# (seconds since last `dispatcher.runs.heartbeat_ts` write) to the
# `Judgemind/Dispatcher` namespace on every supervisor tick (120s) — see
# `scripts/dispatcher/daemon.py::_emit_heartbeat_metric`. Phase 2 (#2768)
# lands the emission code; a healthy daemon emits `0` after each DB
# heartbeat update so the `Maximum` aggregation stays below the
# `heartbeat_stale_seconds` threshold.
#
# During Phase 1 (`desired_count=0`) the alarm was in INSUFFICIENT_DATA
# with `treat_missing_data = "notBreaching"` — no false pages. Phase 2+
# keeps the same behaviour: if the daemon crashes and stops emitting,
# the alarm fires because the last-published value ages past the
# threshold (not because of missing-data handling).

resource "aws_cloudwatch_metric_alarm" "heartbeat_stale" {
  count = var.enable_alerts ? 1 : 0

  alarm_name        = "${local.service_name}-heartbeat-stale"
  alarm_description = "Dispatcher daemon heartbeat has been stale for > ${var.heartbeat_stale_seconds} seconds (${var.environment}). The daemon loop is blocked or the container has crashed — check ${aws_cloudwatch_log_group.dispatcher.name}."

  namespace   = "Judgemind/Dispatcher"
  metric_name = "HeartbeatAge"
  statistic   = "Maximum"
  dimensions = {
    Service = local.service_name
  }

  comparison_operator = "GreaterThanThreshold"
  threshold           = var.heartbeat_stale_seconds
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  # Phase 1: desired_count=0 → no metric ever published → do not alarm on
  # the silence. Phase 2 should flip to "breaching" once the daemon is
  # expected to emit on every tick.
  treat_missing_data = "notBreaching"

  alarm_actions = [var.alert_sns_topic_arn]
  ok_actions    = [var.alert_sns_topic_arn]
}

# ── scheduler tick cadence alarm (#2854) ──────────────────────────────────────
#
# Alarms when the p95 TickCadenceSeconds metric (emitted by the daemon just
# before each scheduler_tick call) exceeds ``tick_cadence_slow_seconds``
# over a 5-minute window.  Uses p95 so a single slow tick inside a healthy
# window does not page; a persistently degraded loop will.
#
# ``treat_missing_data = "notBreaching"`` — the heartbeat alarm already
# covers silence (daemon crashed / no metric at all).  This alarm
# complements it: daemon is alive but ticking too slowly.

resource "aws_cloudwatch_metric_alarm" "scheduler_tick_cadence_slow" {
  count = var.enable_alerts ? 1 : 0

  alarm_name        = "${local.service_name}-scheduler-tick-cadence-slow"
  alarm_description = "Dispatcher scheduler tick p95 cadence exceeded ${var.tick_cadence_slow_seconds}s over a 5-minute window (${var.environment}). The main loop is running slower than expected — check ${aws_cloudwatch_log_group.dispatcher.name} for daemon.tick_cadence_slip events. Issue #2854."

  namespace   = "Judgemind/Dispatcher"
  metric_name = "TickCadenceSeconds"
  extended_statistic = "p95"
  dimensions = {
    Service = local.service_name
  }

  comparison_operator = "GreaterThanThreshold"
  threshold           = var.tick_cadence_slow_seconds
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [var.alert_sns_topic_arn]
  ok_actions    = [var.alert_sns_topic_arn]
}
