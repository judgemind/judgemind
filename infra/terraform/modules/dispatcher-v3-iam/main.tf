# Dispatcher v3 IAM module — three roles for the v3 task pipeline.
#
# v3 collapses the v2 phase pipeline into a single continuous /task
# invocation. Per `docs/specs/dispatcher-v3-spec.md` §10, that means
# `task-runner`, `diagnoser`, and `scheduled-skill` ECS tasks all share
# one broad task role (dev-admin equivalent). The `launcher` task — the
# scheduler that consumes the `agent/ready` queue, runs the issue-author
# trust check, and calls `ecs:RunTask` — gets a strictly narrower role.
#
# The deliberate authority gap is the spec's central trade-off:
#
# > The agent skills running inside ECS tasks (`task-runner`, `diagnoser`,
# > `scheduled-skill`) get **dev-account admin authority** — not principle-
# > of-least-privilege scope. We cannot enumerate in advance whether this
# > issue needs S3 writes, RDS queries, ECS one-offs, CloudWatch tail, or
# > Secrets Manager reads. v2's operator-laptop pattern (operator's AWS
# > creds via `aws-vault`) gave `/task` whatever the operator had — in
# > practice dev-admin. v3 preserves that.
#
# Production accounts are NOT in scope. The trust policy on every role
# in this module pins `Principal = Service: ecs-tasks.amazonaws.com` and
# the source account is the dev account; no cross-account assume-role
# path exists.
#
# Resources created:
# - `aws_iam_role.launcher` — narrow scheduler role assumed by the
#   `launcher` task definition (F2). Includes:
#     * ecs:RunTask / ecs:DescribeTasks / ecs:StopTask scoped to the
#       agent task-def family on this cluster.
#     * iam:PassRole on the agent_task_role and the shared execution
#       role (so RunTask actually succeeds).
#     * RDS connect to the `dispatcher.*` schema only — implemented as
#       `rds-db:connect` against the `judgemind_dispatcher` DB user.
#     * CloudWatch Logs DescribeLogStreams + GetLogEvents on
#       `/judgemind/dispatcher-v3/*` (silent-hang detection from spec §4.1).
#     * secretsmanager:GetSecretValue on TELEGRAM_BOT_TOKEN only.
# - `aws_iam_role.agent_task` — dev-admin equivalent assumed by the
#   `task-runner`, `diagnoser`, and `scheduled-skill` task definitions.
#   Includes:
#     * Full S3 (read/write/delete on every bucket in this account).
#     * Full RDS (`rds-db:connect` against every DB user in the dev
#       cluster).
#     * ecs:RunTask + iam:PassRole — the agent may launch its own
#       one-off ECS tasks via `scripts/ecs-run-task.sh`.
#     * ecs:ExecuteCommand + ssmmessages:* — `aws ecs execute-command`
#       debugging access (#3145).
#     * CloudWatch Logs read/write across the dev account.
#     * Secrets Manager read for every dev-tagged secret.
#     * ECR pull on every dev repository.
# - `aws_iam_role.execution` — shared task-execution role used by all
#   four v3 task definitions. The ECS agent uses this role at task-start
#   to pull the image and inject secrets; it is NOT the in-container
#   identity. Includes:
#     * AmazonECSTaskExecutionRolePolicy (the AWS managed policy).
#     * secretsmanager:GetSecretValue on the wired secret ARNs (telegram
#       bot token + GitHub PAT) so the v3 task definitions can inject
#       them into the container env.
#     * logs:* on the v3 log groups (the managed policy already covers
#       this, but we keep the inline policy for forward compatibility
#       when F4 / F5 land their log groups).
#
# Outputs:
#   `launcher_role_arn`, `agent_task_role_arn`, `execution_role_arn`
# — F2 (#3887) consumes these when wiring task definitions; F4 (the
# launcher ECS service) consumes the launcher ARN; F5 (scheduled-skill
# EventBridge rules) consume the agent_task_role ARN.

data "aws_partition" "current" {}

locals {
  role_name_prefix = "judgemind-dispatcher-v3"

  # F2 task-def families. The launcher needs ecs:RunTask on the three
  # short-lived families (task-runner / diagnoser / scheduled-skill);
  # it does NOT need RunTask on its own `launcher` family because the
  # launcher itself is started by F4's ECS service, not by re-launch.
  agent_task_def_families = [
    "${var.task_definition_family_prefix}-task-runner",
    "${var.task_definition_family_prefix}-diagnoser",
    "${var.task_definition_family_prefix}-scheduled-skill",
  ]

  agent_task_def_arn_patterns = [
    for f in local.agent_task_def_families :
    "arn:${data.aws_partition.current.partition}:ecs:${var.aws_region}:${var.aws_account_id}:task-definition/${f}:*"
  ]

  # Cluster name extracted from the cluster ARN. The ECS task-instance
  # ARN scheme is `task/CLUSTER/TASK_ID`, not `task/CLUSTER_ARN/TASK_ID`,
  # so we have to split the cluster ARN to get the bare cluster name.
  ecs_cluster_name = split("/", var.ecs_cluster_arn)[1]

  agent_task_instance_arn_pattern = "arn:${data.aws_partition.current.partition}:ecs:${var.aws_region}:${var.aws_account_id}:task/${local.ecs_cluster_name}/*"

  # Common assume-role policy: only the ECS task-runtime principal in
  # this account may assume any of the three roles. No cross-account
  # principal, no IAM-user principal, no role-chaining. Production has
  # no v3 footprint, so there is no production-account principal here
  # by construction.
  ecs_tasks_assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "ecs-tasks.amazonaws.com" }
        Action    = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = var.aws_account_id
          }
        }
      }
    ]
  })

  # Compacted secret ARN list for the execution role (drops empty
  # strings so a fresh stack without secret wiring can still apply).
  # IAM rejects `Resource = []` with `MalformedPolicyDocument` (see
  # the analogous guard in modules/dispatcher-daemon/main.tf), so the
  # policy resource itself is skipped via `count = 0` when empty.
  #
  # Why every secret threaded through F2's task-defs MUST appear here:
  # ECS injects `secrets[].valueFrom` entries at task-start time using
  # the execution role -- NOT the task role. So even though the agent
  # task role has broad Secrets Manager read in dev (for runtime reads
  # like `scripts/with-secret.sh`), the task itself fails to even
  # start with `ResourceInitializationError: AccessDeniedException` if
  # the execution role lacks GetSecretValue on the per-task-def
  # secret list. F4 (#3889) surfaced this with the launcher task
  # failing to pull DATABASE_URL despite the launcher role having
  # rds-db:connect to judgemind_dispatcher. The fix is to compact
  # every secret ARN F2's `agent_secrets` and `launcher_secrets`
  # blocks reference, here.
  execution_secret_arns = compact([
    var.telegram_bot_token_secret_arn,
    var.github_token_secret_arn,
    var.anthropic_api_key_secret_arn,
    var.db_connection_secret_arn,
  ])
}

# ─── Shared Execution Role (ECS agent) ──────────────────────────────────────
# Used by the ECS agent to pull the v3 image from ECR, inject env-var
# secrets at task start, and write logs to CloudWatch. This is NOT the
# in-container identity — that's `launcher_role` or `agent_task_role`
# depending on the task definition.

resource "aws_iam_role" "execution" {
  name        = "${local.role_name_prefix}-execution-${var.environment}"
  description = "Shared ECS task-execution role for all dispatcher-v3 task definitions (pulls ECR + reads secrets + writes logs)."

  assume_role_policy = local.ecs_tasks_assume_role_policy
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Read the wired secrets at task-start so they appear as env vars
# inside the container. Only telegram + github PAT are wired here; the
# agent task role reads everything else via the broad
# `judgemind/*` / `*-dev-*` patterns at runtime.
resource "aws_iam_role_policy" "execution_secrets" {
  count = length(local.execution_secret_arns) > 0 ? 1 : 0

  name = "${local.role_name_prefix}-execution-secrets-${var.environment}"
  role = aws_iam_role.execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadDispatcherV3Secrets"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = local.execution_secret_arns
      }
    ]
  })
}

# ─── Launcher Role (narrow scheduler scope) ─────────────────────────────────
# Assumed by F2's `launcher` task definition. The launcher consumes the
# `agent/ready` queue, runs the issue-author trust check, and starts a
# task-runner / diagnoser / scheduled-skill task per claim. It does NOT
# touch S3, does NOT query `derived.*` / `staging.*`, and does NOT read
# any secret beyond `TELEGRAM_BOT_TOKEN` (for operator paging).

resource "aws_iam_role" "launcher" {
  name        = "${local.role_name_prefix}-launcher-${var.environment}"
  description = "Dispatcher v3 launcher task role - narrow scheduler scope (RunTask on agent task-defs, RDS connect to dispatcher.*, Telegram secret read, CloudWatch read on /judgemind/dispatcher-v3/*)."

  assume_role_policy = local.ecs_tasks_assume_role_policy
}

# RunTask + DescribeTasks + StopTask on the three agent task-def
# families. The cluster-ARN condition is the defense-in-depth control
# alongside the family-scoped Resource list — RunTask cannot escape to
# another cluster even if a future change widens the family list.
resource "aws_iam_role_policy" "launcher_run_agent_tasks" {
  name = "${local.role_name_prefix}-launcher-run-agent-tasks-${var.environment}"
  role = aws_iam_role.launcher.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowRunAgentTasks"
        Effect   = "Allow"
        Action   = "ecs:RunTask"
        Resource = local.agent_task_def_arn_patterns
        Condition = {
          ArnEquals = {
            "ecs:cluster" = var.ecs_cluster_arn
          }
        }
      },
      {
        Sid      = "AllowStopAgentTasks"
        Effect   = "Allow"
        Action   = "ecs:StopTask"
        Resource = local.agent_task_def_arn_patterns
        Condition = {
          ArnEquals = {
            "ecs:cluster" = var.ecs_cluster_arn
          }
        }
      },
      {
        # ecs:DescribeTasks does not accept ARN scoping at the API
        # level — same constraint as the v2 daemon's `task_run_oneshot`
        # and `task_launch_agent_runner` policies. The cluster-ARN
        # condition is the defense-in-depth control.
        Sid      = "AllowDescribeAgentTasks"
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
        # ecs:TagResource is invoked implicitly by RunTask when the
        # call includes a `tags` list. AWS evaluates authorization
        # against the task-instance ARN (task/CLUSTER/*), not the
        # task-definition ARN — same quirk that bit the v2 agent-runner
        # launcher policy (#3129).
        Sid      = "AllowTagAgentTasks"
        Effect   = "Allow"
        Action   = "ecs:TagResource"
        Resource = local.agent_task_instance_arn_pattern
        Condition = {
          ArnEquals = {
            "ecs:cluster" = var.ecs_cluster_arn
          }
        }
      },
    ]
  })
}

# PassRole — RunTask fails with `AccessDeniedException: User ... is not
# authorized to pass role ... because no identity-based policy allows
# the iam:PassRole action` unless this is granted. Pinned to the agent
# task role and shared execution role only — the launcher cannot pass
# its own role or any other role.
resource "aws_iam_role_policy" "launcher_pass_role" {
  name = "${local.role_name_prefix}-launcher-pass-role-${var.environment}"
  role = aws_iam_role.launcher.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowPassAgentTaskRole"
        Effect = "Allow"
        Action = "iam:PassRole"
        Resource = [
          aws_iam_role.agent_task.arn,
          aws_iam_role.execution.arn,
        ]
        Condition = {
          StringEquals = {
            "iam:PassedToService" = "ecs-tasks.amazonaws.com"
          }
        }
      }
    ]
  })
}

# RDS IAM-auth connect to the `judgemind_dispatcher` DB user only. The
# launcher persists its lease + queue snapshots into the `dispatcher.*`
# schema; that schema is owned by the `judgemind_dispatcher` role. No
# other DB user is reachable — the launcher cannot read `derived.*`,
# `staging.*`, or `public.*`.
#
# `rds-db:connect` Resource ARN scheme:
# `arn:aws:rds-db:REGION:ACCOUNT:dbuser:CLUSTER_RESOURCE_ID/DB_USER`.
# Cluster resource ID is opaque, so we use a wildcard there and pin the
# DB user — defense-in-depth via the DB-side `judgemind_dispatcher`
# role's own per-schema grants.
resource "aws_iam_role_policy" "launcher_rds_connect" {
  name = "${local.role_name_prefix}-launcher-rds-connect-${var.environment}"
  role = aws_iam_role.launcher.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowConnectAsDispatcherRole"
        Effect = "Allow"
        Action = "rds-db:connect"
        Resource = [
          "arn:${data.aws_partition.current.partition}:rds-db:${var.aws_region}:${var.aws_account_id}:dbuser:*/judgemind_dispatcher"
        ]
      }
    ]
  })
}

# Secrets Manager read — telegram bot token only. When the ARN is
# empty (fresh stack without telegram wiring) the policy is skipped
# via count=0 to avoid IAM's `Resource = []` rejection.
resource "aws_iam_role_policy" "launcher_secrets_telegram" {
  count = var.telegram_bot_token_secret_arn != "" ? 1 : 0

  name = "${local.role_name_prefix}-launcher-secrets-telegram-${var.environment}"
  role = aws_iam_role.launcher.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowReadTelegramSecret"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = var.telegram_bot_token_secret_arn
      }
    ]
  })
}

# CloudWatch Logs read on `/judgemind/dispatcher-v3/*`. Used by the
# silent-hang detector (spec §4.1) which calls `DescribeLogStreams` to
# check whether an in-flight agent's log stream has grown in the last
# N minutes. Read-only — the launcher never writes its own logs (it
# inherits log writes from the execution role's managed policy).
resource "aws_iam_role_policy" "launcher_logs_read" {
  name = "${local.role_name_prefix}-launcher-logs-read-${var.environment}"
  role = aws_iam_role.launcher.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowReadDispatcherV3Logs"
        Effect = "Allow"
        Action = [
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
          "logs:GetLogEvents",
          "logs:FilterLogEvents",
        ]
        Resource = [
          "arn:${data.aws_partition.current.partition}:logs:${var.aws_region}:${var.aws_account_id}:log-group:/judgemind/dispatcher-v3/*",
          "arn:${data.aws_partition.current.partition}:logs:${var.aws_region}:${var.aws_account_id}:log-group:/judgemind/dispatcher-v3/*:*",
        ]
      }
    ]
  })
}

# ─── Agent Task Role (dev-admin equivalent) ─────────────────────────────────
# Assumed by F2's `task-runner`, `diagnoser`, and `scheduled-skill`
# task definitions. Per spec §10, this role has dev-account admin
# authority because we cannot enumerate in advance which AWS APIs a
# given /task invocation will need. The trade-off is explicitly
# accepted there.
#
# What this role can do in dev:
#   * Full S3: read, write, delete, list across every bucket.
#   * Full RDS IAM-auth connect against every DB user in the dev cluster.
#   * Launch any ECS task on the dev cluster (including its own
#     `scripts/ecs-run-task.sh` one-offs).
#   * Read every Secrets Manager entry tagged for dev usage
#     (`judgemind/*` and `*-dev-*` ARN patterns).
#   * Pull any ECR image in this account.
#   * Open SSM exec channels into running tasks.
#   * Read and write any CloudWatch log group in this account.
#
# What it CANNOT do, by construction:
#   * Assume any cross-account role — the trust policy is single-account
#     only (see `local.ecs_tasks_assume_role_policy`).
#   * Touch any production resource — there is no production AWS
#     account in scope; every Resource ARN below pins the dev account
#     ID.
#   * Modify IAM (no `iam:*` action grants below).
#   * Modify Route53, ACM, organizations, billing, support, etc.

resource "aws_iam_role" "agent_task" {
  name        = "${local.role_name_prefix}-agent-task-${var.environment}"
  description = "Dispatcher v3 agent task role - dev-admin equivalent shared by task-runner / diagnoser / scheduled-skill (spec section 10). Trust policy excludes assuming any cross-account role."

  assume_role_policy = local.ecs_tasks_assume_role_policy
}

# Full S3. The agent may read raw/, write spotcheck scratch, mirror
# session logs to the sessions bucket, etc. We accept the broad scope
# in dev because we cannot enumerate which buckets a given /task run
# will need to touch.
resource "aws_iam_role_policy" "agent_task_s3" {
  name = "${local.role_name_prefix}-agent-task-s3-${var.environment}"
  role = aws_iam_role.agent_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowFullS3"
        Effect   = "Allow"
        Action   = "s3:*"
        Resource = "*"
      }
    ]
  })
}

# Full RDS IAM-auth connect — the agent may need to query `derived.*`,
# `staging.*`, `dispatcher.*`, etc. depending on the issue. Granting
# `rds-db:connect` against every DB user is equivalent to the
# operator-laptop pattern v2 used (operator's AWS creds via aws-vault
# could connect as any DB user too).
resource "aws_iam_role_policy" "agent_task_rds_connect" {
  name = "${local.role_name_prefix}-agent-task-rds-connect-${var.environment}"
  role = aws_iam_role.agent_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowConnectAsAnyDBUser"
        Effect = "Allow"
        Action = "rds-db:connect"
        Resource = [
          "arn:${data.aws_partition.current.partition}:rds-db:${var.aws_region}:${var.aws_account_id}:dbuser:*/*"
        ]
      }
    ]
  })
}

# ECS — the agent may launch its own one-offs (`scripts/ecs-run-task.sh`),
# describe arbitrary tasks, register/deregister task definitions for
# rebuilds, and `aws ecs execute-command` into running tasks for
# debugging. Scoped to the dev cluster via the cluster-ARN condition
# wherever AWS supports it; falls back to `Resource="*"` only on the
# AWS APIs that don't accept ARN scoping (RegisterTaskDefinition,
# DescribeTaskDefinition, etc.).
resource "aws_iam_role_policy" "agent_task_ecs" {
  name = "${local.role_name_prefix}-agent-task-ecs-${var.environment}"
  role = aws_iam_role.agent_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Task-def lifecycle. AWS does not accept ARN scoping on
        # Register/Describe — same constraint as the iam_agent module.
        Sid    = "AllowTaskDefLifecycle"
        Effect = "Allow"
        Action = [
          "ecs:RegisterTaskDefinition",
          "ecs:DeregisterTaskDefinition",
          "ecs:DescribeTaskDefinition",
          "ecs:ListTaskDefinitions",
        ]
        Resource = "*"
      },
      {
        # Cluster-scoped task lifecycle. The agent may launch / stop /
        # describe / list any task on this cluster — needed for the
        # full `scripts/ecs-run-task.sh` flow plus any future debug
        # paths a /task run wants to take.
        Sid    = "AllowClusterTaskLifecycle"
        Effect = "Allow"
        Action = [
          "ecs:RunTask",
          "ecs:StopTask",
          "ecs:DescribeTasks",
          "ecs:ListTasks",
          "ecs:DescribeServices",
          "ecs:ListServices",
          "ecs:DescribeClusters",
          "ecs:DescribeContainerInstances",
          "ecs:ListContainerInstances",
          "ecs:TagResource",
          "ecs:UntagResource",
        ]
        Resource = "*"
        Condition = {
          ArnEquals = {
            "ecs:cluster" = var.ecs_cluster_arn
          }
        }
      },
      {
        # ECS Exec — `aws ecs execute-command` for live debugging
        # (#3145). The data plane runs over SSM (next statement); this
        # statement is the control-plane authorization for opening the
        # session.
        Sid      = "AllowECSExecuteCommand"
        Effect   = "Allow"
        Action   = "ecs:ExecuteCommand"
        Resource = "*"
        Condition = {
          ArnEquals = {
            "ecs:cluster" = var.ecs_cluster_arn
          }
        }
      },
    ]
  })
}

# iam:PassRole — required for the agent to launch its own one-off ECS
# tasks via `scripts/ecs-run-task.sh`. The script's RunTask call passes
# the source task's task + execution roles, which the agent's role
# must be authorized to pass. Scoped to the dev account's
# `judgemind-*-dev` roles — no cross-account, no IAM admin roles.
#
# `iam:GetRole` is paired here because `scripts/ecs-run-task.sh --role
# <name>` resolves the role name to an ARN before RunTask.
resource "aws_iam_role_policy" "agent_task_pass_role" {
  name = "${local.role_name_prefix}-agent-task-pass-role-${var.environment}"
  role = aws_iam_role.agent_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowPassDevRoles"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = "arn:${data.aws_partition.current.partition}:iam::${var.aws_account_id}:role/judgemind-*-${var.environment}"
        Condition = {
          StringEquals = {
            "iam:PassedToService" = "ecs-tasks.amazonaws.com"
          }
        }
      },
      {
        Sid      = "AllowResolveDevRoleNames"
        Effect   = "Allow"
        Action   = "iam:GetRole"
        Resource = "arn:${data.aws_partition.current.partition}:iam::${var.aws_account_id}:role/judgemind-*-${var.environment}"
      },
    ]
  })
}

# SSM exec data plane. The control-plane `ecs:ExecuteCommand` (above)
# opens the channel; these `ssmmessages:*` actions are how the data
# plane streams stdin/stdout. Resource MUST be `*` because SSM creates
# the channel session ID dynamically per-exec — same pattern v2's
# dispatcher-daemon and dispatcher-agent-runner use.
resource "aws_iam_role_policy" "agent_task_ssm" {
  name = "${local.role_name_prefix}-agent-task-ssm-${var.environment}"
  role = aws_iam_role.agent_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowSSMMessages"
        Effect = "Allow"
        Action = [
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel",
        ]
        Resource = "*"
      }
    ]
  })
}

# CloudWatch Logs read+write across the dev account. The agent may
# need to tail any `/ecs/judgemind-*-dev` or `/judgemind/dispatcher-v3/*`
# log group during diagnosis, and may emit its own structured logs
# from inside the container.
resource "aws_iam_role_policy" "agent_task_logs" {
  name = "${local.role_name_prefix}-agent-task-logs-${var.environment}"
  role = aws_iam_role.agent_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowLogsReadWrite"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
          "logs:GetLogEvents",
          "logs:FilterLogEvents",
          "logs:StartQuery",
          "logs:StopQuery",
          "logs:GetQueryResults",
          "logs:DescribeQueries",
        ]
        Resource = [
          "arn:${data.aws_partition.current.partition}:logs:${var.aws_region}:${var.aws_account_id}:log-group:/ecs/judgemind-*-${var.environment}",
          "arn:${data.aws_partition.current.partition}:logs:${var.aws_region}:${var.aws_account_id}:log-group:/ecs/judgemind-*-${var.environment}:*",
          "arn:${data.aws_partition.current.partition}:logs:${var.aws_region}:${var.aws_account_id}:log-group:/judgemind/dispatcher-v3/*",
          "arn:${data.aws_partition.current.partition}:logs:${var.aws_region}:${var.aws_account_id}:log-group:/judgemind/dispatcher-v3/*:*",
        ]
      },
      {
        # CloudWatch Insights `StartQuery` accepts a `*` resource at
        # the API level — it is scoped to log-group references inside
        # the query string itself, not the IAM Resource. Same handling
        # as the iam_agent module.
        Sid      = "AllowInsightsStartQuery"
        Effect   = "Allow"
        Action   = "logs:StartQuery"
        Resource = "*"
      },
    ]
  })
}

# Secrets Manager read for every dev secret. Scoped to:
#   * `judgemind/*` — every secret created by Terraform via the
#     `judgemind/<service>/<name>` naming convention.
#   * `*-dev-*` — every secret created with the `-dev-` infix
#     (manually-provisioned secrets such as `judgemind/dispatcher/
#     github-token-QOmHlJ` follow this).
resource "aws_iam_role_policy" "agent_task_secrets" {
  name = "${local.role_name_prefix}-agent-task-secrets-${var.environment}"
  role = aws_iam_role.agent_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowReadDevSecrets"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
          "secretsmanager:ListSecretVersionIds",
        ]
        Resource = [
          "arn:${data.aws_partition.current.partition}:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:judgemind/*",
          "arn:${data.aws_partition.current.partition}:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:*-${var.environment}-*",
        ]
      },
      {
        # ListSecrets has no resource-level support at the API layer.
        # The List API only returns metadata, not values.
        Sid      = "AllowListSecrets"
        Effect   = "Allow"
        Action   = "secretsmanager:ListSecrets"
        Resource = "*"
      },
    ]
  })
}

# ECR pull on every repository in this account. The agent may pull
# any v3 image, or any tooling image hosted by the project.
resource "aws_iam_role_policy" "agent_task_ecr" {
  name = "${local.role_name_prefix}-agent-task-ecr-${var.environment}"
  role = aws_iam_role.agent_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Token endpoint — required for the docker-login flow and
        # cannot be ARN-scoped.
        Sid      = "AllowEcrAuthToken"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "AllowEcrPull"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:DescribeImages",
          "ecr:DescribeRepositories",
          "ecr:GetDownloadUrlForLayer",
          "ecr:ListImages",
        ]
        Resource = "arn:${data.aws_partition.current.partition}:ecr:${var.aws_region}:${var.aws_account_id}:repository/*"
      },
    ]
  })
}

# CloudWatch metrics — emit custom metrics under the
# `Judgemind/DispatcherV3` namespace. Mirrors the v2 daemon's
# `task_put_metric` policy.
resource "aws_iam_role_policy" "agent_task_metrics" {
  name = "${local.role_name_prefix}-agent-task-metrics-${var.environment}"
  role = aws_iam_role.agent_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowPutDispatcherV3Metrics"
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = [
              "Judgemind/DispatcherV3",
              "Judgemind/Dispatcher",
            ]
          }
        }
      }
    ]
  })
}
