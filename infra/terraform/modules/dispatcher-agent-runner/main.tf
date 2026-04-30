# Dispatcher v2 agent-runner module — ECS Fargate task definition for
# per-agent runners (issue #3090; Stage 1b of the #3086 / #3078
# per-agent-ECS migration).
#
# Unlike ``dispatcher-daemon`` this module DOES NOT provision an ECS
# service — an agent-runner is a one-shot workload started via
# ``ecs:RunTask`` (manually via ``aws ecs run-task --overrides`` for
# the Stage 1b smoke, or by the daemon's future
# ``_launch_agent_ecs_task`` in Stage 2). The task runs the full phase
# pipeline inside the container and exits when the agent hits a
# terminal phase.
#
# Resources created:
# - CloudWatch log group ``/ecs/judgemind-dispatcher-agent-runner-<env>``.
# - Execution role (ECR pull + log writes + secret fetches).
# - Task role (narrow: Secrets Manager read on DATABASE_URL + GitHub
#   PAT, CloudWatch log writes into the agent-runner log group only,
#   plus ``ssmmessages:*`` for ECS Exec-based live debugging — #3145).
#   Deliberately does NOT include ``ecs:RunTask`` — the agent-runner
#   is a leaf; it does not spawn further ECS tasks.
# - Security group (outbound HTTPS + Postgres, same egress profile as
#   the daemon so GitHub / Anthropic / Postgres all reach from inside).
# - ECS task definition ``judgemind-dispatcher-agent-runner-<env>``
#   wired at CPU=4096 / memory=16384 to match the subprocess-daemon
#   envelope — the sizing ralph was designed against. The initial
#   Stage 1b baseline (512 / 1024) pegged memory at 1022/1024 MB and
#   CPU at 509/512 under real ralph workload (#3153); can shrink once
#   subprocess mode is retired.
#
# The security group ID + task-def family are exported so the daemon
# (Stage 2) can reference them without re-declaring ARNs in the
# environment wiring.

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  task_family    = "judgemind-dispatcher-agent-runner-${var.environment}"
  log_group_name = "/ecs/judgemind-dispatcher-agent-runner-${var.environment}"
  container_name = "agent-runner"

  # Execution-role secret list uses compact() so unset ARNs drop out
  # (mirrors the dispatcher-daemon pattern).
  execution_secret_arns = compact([
    var.anthropic_api_key_secret_arn,
    var.db_connection_secret_arn,
    var.github_token_secret_arn,
  ])

  # Task role gets a narrower set: DATABASE_URL (for direct psql calls
  # from inside the container) + GitHub PAT (for `gh auth login`). The
  # Anthropic API key is injected by the execution role at container
  # start and read from env; the task role doesn't re-fetch it.
  task_secret_arns = compact([
    var.db_connection_secret_arn,
    var.github_token_secret_arn,
  ])
}

# ─── CloudWatch Log Group ──────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "agent_runner" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
}

# ─── Security Group ────────────────────────────────────────────────────────
# Outbound-only egress: HTTPS (GitHub / Anthropic / ECR / Secrets
# Manager) + Postgres. No inbound traffic — agent-runners are leaves.

resource "aws_security_group" "agent_runner" {
  name        = local.task_family
  description = "Dispatcher agent-runner ECS tasks - outbound only (HTTPS, Postgres)"
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS to GitHub, Anthropic, ECR, Secrets Manager"
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
}

# ─── IAM: Execution Role (ECS agent) ───────────────────────────────────────

resource "aws_iam_role" "execution" {
  name        = "${local.task_family}-exec"
  description = "Dispatcher agent-runner execution role - pull ECR, read secrets, write logs"

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

resource "aws_iam_role_policy" "execution_secrets" {
  count = length(local.execution_secret_arns) > 0 ? 1 : 0

  name = "${local.task_family}-exec-secrets"
  role = aws_iam_role.execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadAgentRunnerSecrets"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = local.execution_secret_arns
      }
    ]
  })
}

# ─── IAM: Task Role (container runtime) ────────────────────────────────────
#
# Narrow by design — the agent-runner is a leaf process. What it needs:
#
#   * Secrets Manager GetSecretValue on DATABASE_URL + GitHub PAT only
#     (the container may re-read these during long-running phases).
#   * CloudWatch `logs:PutLogEvents` on the agent-runner log group
#     only (narrower than the daemon's wildcard).
#   * ECS Exec (``ssmmessages:*``) — operators need live interactive
#     inspection of a running agent (worktree contents, claude
#     stdout/stderr files, git log, process tree) when CloudWatch tail
#     isn't enough. See #3145. Mirrors the dispatcher-daemon task role
#     (``infra/terraform/modules/dispatcher-daemon/main.tf``
#     ``task_ecs_exec_ssm``).
#
# What it deliberately does NOT have:
#
#   * ``ecs:RunTask`` — the agent-runner is a terminal leaf; it does
#     not spawn further ECS tasks. Granting RunTask would let a
#     compromised agent kick off its own children (or arbitrary other
#     task definitions).
#   * CloudWatch PutMetricData — the daemon emits agent metrics (Stage
#     2+); the agent-runner itself is metered via phase_outputs rows,
#     not directly.

resource "aws_iam_role" "task" {
  name        = "${local.task_family}-task"
  description = "Dispatcher agent-runner task role - narrow runtime permissions for in-container DB + GitHub work"

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

resource "aws_iam_role_policy" "task_read_secrets" {
  count = length(local.task_secret_arns) > 0 ? 1 : 0

  name = "${local.task_family}-task-read-secrets"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadAgentRunnerRuntimeSecrets"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = local.task_secret_arns
      }
    ]
  })
}

resource "aws_iam_role_policy" "task_log_writes" {
  name = "${local.task_family}-task-log-writes"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowAgentRunnerLogWrites"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        # Scoped to the agent-runner log group only. The daemon's
        # wildcard approach is unnecessary here because the task's
        # only log destination is its own group.
        Resource = "${aws_cloudwatch_log_group.agent_runner.arn}:*"
      }
    ]
  })
}

# ─── IAM: ECS Exec (ssmmessages) ───────────────────────────────────────────
#
# Required for ``aws ecs execute-command`` to shell into a running
# agent-runner task for live debugging (#3145). The task-def also sets
# ``enable_execute_command = true`` below, which + this policy + the
# per-RunTask ``enableExecuteCommand`` flag in ``daemon.py`` together
# enable the ECS Exec data plane end-to-end.
#
# Mirrors the dispatcher-daemon ``task_ecs_exec_ssm`` policy. The
# ``ssmmessages`` actions require ``Resource = "*"`` because the SSM
# session is created dynamically per-exec-invocation.

resource "aws_iam_role_policy" "task_ecs_exec_ssm" {
  name = "${local.task_family}-task-ecs-exec-ssm"
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
    ]
  })
}

# ─── IAM: Spotcheck oneshot launcher (#3459) ───────────────────────────────
#
# Re-enables the /spotcheck scheduled skill (re-enabled hourly by
# migration 52) by widening this task role to launch its sampling
# oneshot via `scripts/ecs-run-task.sh` and read the resulting
# JSON + PDF artifacts from the document-archive bucket.
#
# The previous leaf-only task role intentionally lacked ecs:RunTask
# (header comment above), which is correct for the *general* leaf
# threat model — but spotcheck is a special case: a scheduled skill
# whose entire job is to run an audit oneshot and grade the output.
# Issue #3459 / PR #3457 traced the failure to this gap and chose
# Option A (widen the role with a tightly-scoped subset of the
# daemon's `task_run_oneshot` policy) over Option B (refactor the
# daemon to pre-launch the oneshot).
#
# Scope invariants (audit via `aws iam get-role-policy`):
#
#   - RunTask / StopTask restricted to the `judgemind-oneshot-${env}`
#     family on the dev cluster — no other family, no other cluster.
#   - DescribeTasks / DescribeServices use `Resource = "*"` (AWS API
#     does not accept ARN scoping on those actions); the cluster-ARN
#     condition is the defence-in-depth control. Same pattern as the
#     daemon's `task_run_oneshot`.
#   - PassRole pinned to the caller-supplied source task + execution
#     role ARNs — no daemon role, no other oneshot role.
#   - GetRole pinned to `judgemind-*-${env}` (the launcher resolves a
#     `--role <name>` override to an ARN at run-time).
#   - logs:GetLogEvents / FilterLogEvents pinned to
#     `/ecs/judgemind-*-${env}` so the agent-runner can stream the
#     oneshot's logs (where /spotcheck reads `s3://...` paths) but
#     cannot read unrelated log groups.
#   - S3 grants limited to ListBucket / GetBucketLocation / GetObject
#     on the document-archive bucket only — read-only, no writes.
#     /spotcheck never writes to S3; the oneshot itself uses the
#     iam_scraper role for any writes.
#
# Disabled (count = 0) when any of the four required inputs is empty.
# Same rationale as the daemon module — staging / throwaway test stacks
# that do not provision the oneshot family stay at the prior leaf-only
# scope without further config gymnastics.

locals {
  spotcheck_oneshot_enabled = (
    var.spotcheck_oneshot_source_task_role_arn != "" &&
    var.spotcheck_oneshot_source_execution_role_arn != "" &&
    var.spotcheck_ecs_cluster_arn != "" &&
    var.spotcheck_document_archive_bucket_arn != ""
  )
  spotcheck_oneshot_family_arn_pattern = (
    local.spotcheck_oneshot_enabled
    ? "arn:aws:ecs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:task-definition/judgemind-oneshot-${var.environment}:*"
    : ""
  )
  spotcheck_oneshot_pass_role_arns = compact([
    var.spotcheck_oneshot_source_task_role_arn,
    var.spotcheck_oneshot_source_execution_role_arn,
  ])
}

resource "aws_iam_role_policy" "task_spotcheck_oneshot" {
  count = local.spotcheck_oneshot_enabled ? 1 : 0

  name = "${local.task_family}-task-spotcheck-oneshot"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          # Register a fresh oneshot task definition for each /spotcheck
          # invocation. AWS does not accept ARN scoping on this action.
          Sid      = "AllowRegisterOneshotTaskDefinition"
          Effect   = "Allow"
          Action   = "ecs:RegisterTaskDefinition"
          Resource = "*"
        },
        {
          Sid      = "AllowDeregisterOneshotTaskDefinition"
          Effect   = "Allow"
          Action   = "ecs:DeregisterTaskDefinition"
          Resource = local.spotcheck_oneshot_family_arn_pattern
        },
        {
          # Source-task-def read (template) + freshly registered oneshot
          # task-def read. AWS requires `*` on this action.
          Sid      = "AllowDescribeOneshotTaskDefinitions"
          Effect   = "Allow"
          Action   = "ecs:DescribeTaskDefinition"
          Resource = "*"
        },
        {
          # Launch the oneshot, scoped to the dev oneshot family on this
          # cluster only.
          Sid      = "AllowRunOneshotTask"
          Effect   = "Allow"
          Action   = "ecs:RunTask"
          Resource = local.spotcheck_oneshot_family_arn_pattern
          Condition = {
            ArnEquals = {
              "ecs:cluster" = var.spotcheck_ecs_cluster_arn
            }
          }
        },
        {
          # Stop a hung oneshot if /spotcheck times out (the launcher's
          # cleanup path uses StopTask before exiting).
          Sid      = "AllowStopOneshotTask"
          Effect   = "Allow"
          Action   = "ecs:StopTask"
          Resource = local.spotcheck_oneshot_family_arn_pattern
          Condition = {
            ArnEquals = {
              "ecs:cluster" = var.spotcheck_ecs_cluster_arn
            }
          }
        },
        {
          # Poll DescribeTasks until the oneshot finishes; DescribeServices
          # resolves the cluster's networking config for RunTask. Both
          # actions require `*` at the API level — the cluster-ARN
          # condition is the defence-in-depth control.
          Sid    = "AllowDescribeOneshotRunningTasks"
          Effect = "Allow"
          Action = [
            "ecs:DescribeTasks",
            "ecs:DescribeServices",
          ]
          Resource = "*"
          Condition = {
            ArnEquals = {
              "ecs:cluster" = var.spotcheck_ecs_cluster_arn
            }
          }
        },
        {
          # PassRole the oneshot's source task + execution roles.
          # Without this, RunTask fails with `AccessDeniedException:
          # User ... is not authorized to pass role ... because no
          # identity-based policy allows the iam:PassRole action`.
          Sid      = "AllowPassOneshotRoles"
          Effect   = "Allow"
          Action   = "iam:PassRole"
          Resource = local.spotcheck_oneshot_pass_role_arns
          Condition = {
            StringEquals = {
              "iam:PassedToService" = "ecs-tasks.amazonaws.com"
            }
          }
        },
        {
          # Resolve a `--role <name>` override (ecs-run-task.sh calls
          # `aws iam get-role --role-name <name>` to map a role name to
          # an ARN before RunTask). Scoped to this account's
          # `judgemind-*-${env}` roles only.
          Sid      = "AllowResolveOneshotRoleName"
          Effect   = "Allow"
          Action   = "iam:GetRole"
          Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/judgemind-*-${var.environment}"
        },
        {
          # Stream the oneshot's CloudWatch logs back into the agent-
          # runner's stdout (the launcher tails the ingestion-worker
          # log group, where oneshot tasks inherit their log config).
          # Scoped to judgemind-* log groups in this account/region.
          Sid    = "AllowReadOneshotLogs"
          Effect = "Allow"
          Action = [
            "logs:DescribeLogStreams",
            "logs:GetLogEvents",
            "logs:FilterLogEvents",
          ]
          Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:log-group:/ecs/judgemind-*:*"
        },
        {
          # /spotcheck reads its sampling JSON output back from
          # s3://judgemind-document-archive-${env}/spotcheck/... and
          # downloads paired PDFs from s3://.../ca/<county>/raw/...
          # GetBucketLocation is a precondition for `aws s3 cp` against
          # a regional bucket from a multi-region SDK call path.
          Sid    = "AllowSpotcheckBucketReads"
          Effect = "Allow"
          Action = [
            "s3:ListBucket",
            "s3:GetBucketLocation",
          ]
          Resource = var.spotcheck_document_archive_bucket_arn
        },
        {
          # Object-level read on the same bucket. /spotcheck never
          # writes — no PutObject / DeleteObject is granted. The oneshot
          # itself uses the iam_scraper role for writes.
          Sid      = "AllowSpotcheckObjectReads"
          Effect   = "Allow"
          Action   = "s3:GetObject"
          Resource = "${var.spotcheck_document_archive_bucket_arn}/*"
        },
        {
          # List running tasks on the dev cluster so dev-db-query.sh and
          # ecs-run.sh can resolve the running task ARN. AWS requires
          # Resource = "*" on ListTasks; the cluster-ARN condition is the
          # defence-in-depth control.
          Sid      = "AllowSpotcheckListTasks"
          Effect   = "Allow"
          Action   = "ecs:ListTasks"
          Resource = "*"
          Condition = {
            ArnEquals = {
              "ecs:cluster" = var.spotcheck_ecs_cluster_arn
            }
          }
        },
      ],
      var.spotcheck_oneshot_script_bucket_arn != "" ? [
        {
          # Upload + download scripts >8KB via pre-signed URL. Scoped to
          # the oneshot-scripts/ prefix inside the caller-supplied assets
          # bucket. GetObject is required because the URL is signed by
          # this principal — S3 evaluates the signing principal's IAM
          # policy at request time, so the oneshot container's anonymous
          # urllib download fails with 403 unless GetObject is granted.
          Sid    = "AllowUploadSpotcheckOneshotScripts"
          Effect = "Allow"
          Action = [
            "s3:PutObject",
            "s3:GetObject",
            "s3:DeleteObject",
          ]
          Resource = "${var.spotcheck_oneshot_script_bucket_arn}/oneshot-scripts/*"
        },
      ] : [],
    )
  })
}

# Grants `s3:PutObject` on the ralph-reviews/ prefix so the agent-runner
# container can mirror review-log.jsonl telemetry after each ralph SHIP (via
# log_summary's S3 upload path) and before each worktree teardown (via the
# cleanup_worktree.sh best-effort fallback).
#
# Reuses `spotcheck_document_archive_bucket_arn` — the same
# `judgemind-document-archive-<env>` bucket that already holds spotcheck and
# data-quality artifacts.  Forward-compatible with Stage 2 per-agent ECS
# migration (#3086/#3091) where `log_summary` runs inside the agent-runner
# container.
#
# Disabled (count=0) when `spotcheck_document_archive_bucket_arn` is empty —
# safe for stacks that do not provision the document-archive bucket.

resource "aws_iam_role_policy" "task_ralph_review_telemetry" {
  count = var.spotcheck_document_archive_bucket_arn != "" ? 1 : 0

  name = "${local.task_family}-task-ralph-review-telemetry"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowPutRalphReviewTelemetry"
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${var.spotcheck_document_archive_bucket_arn}/ralph-reviews/*"
      },
    ]
  })
}

# ─── ECS Task Definition ───────────────────────────────────────────────────
#
# CPU / memory: 4096 / 16384 to match the subprocess-daemon envelope —
# the sizing ralph was designed against. Earlier Stage 1b baseline
# (512 / 1024) saturated CPU at 509/512 and memory at 1022/1024 MB under
# real ralph workload (#3153). Can shrink once subprocess mode is retired
# and the dispatcher daemon scales down correspondingly.
#
# stopTimeout: ECS/Fargate caps stopTimeout at 120 seconds. The #3090
# issue text mentions "4h" but that's the expected agent lifetime, not
# a Fargate-enforced timeout — tasks run until the container exits.
# We set stopTimeout to 120 (the platform max) so the container has
# enough time to update ``dispatcher.agents.ended_at`` and flush final
# logs on a SIGTERM (e.g. during a force-stop).

resource "aws_ecs_task_definition" "agent_runner" {
  family                   = local.task_family
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
      name      = local.container_name
      image     = "${var.ecr_repository_url}:${var.image_tag}"
      essential = true

      # 120s — the platform max. See header comment for rationale.
      stopTimeout = 120

      environment = concat(
        [
          { name = "ENVIRONMENT", value = var.environment },
          { name = "AGENT_WORKSPACE", value = "/var/lib/agent-runner" },
          { name = "REPO_URL", value = var.repo_url },
        ],
        var.github_repo != "" ? [{ name = "GITHUB_REPO", value = var.github_repo }] : [],
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
            # Same JSON-key-suffix pattern as the daemon module —
            # dispatcher-role DB secret has ``url`` at the top level.
            name      = "DATABASE_URL"
            valueFrom = "${var.db_connection_secret_arn}:url::"
          }
        ] : [],
        var.github_token_secret_arn != "" ? [
          {
            name      = "GITHUB_TOKEN"
            valueFrom = var.github_token_secret_arn
          }
        ] : [],
      )

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.agent_runner.name
          "awslogs-region"        = data.aws_region.current.id
          "awslogs-stream-prefix" = "agent-runner"
        }
      }
    }
  ])

  # Content-level postconditions on the rendered container_definitions
  # JSON. Defense-in-depth against the #2840 silent-drop class of bug:
  # an apply that produces a task-def revision without a required
  # secret entry, despite the corresponding ARN variable being non-
  # empty (caused by a stale data-source evaluation, provider
  # content-hash dedup, or any other future regression that drops a
  # `concat()` branch from the rendered JSON).
  #
  # These complement the variable-level preconditions above: those
  # catch "ARN unset"; these catch "ARN set but didn't propagate to
  # the rendered JSON". A regression test for this pattern lives in
  # `tests/postconditions/`.
  #
  # `self.container_definitions` is the rendered JSON string AS
  # PASSED TO THE AWS PROVIDER — the same content that becomes the
  # registered task-definition revision. If the secret name is not
  # in that string, the secret would not be injected into the
  # container, regardless of what `var.X_secret_arn` says.
  lifecycle {
    postcondition {
      condition = (
        var.anthropic_api_key_secret_arn == "" ||
        strcontains(self.container_definitions, "ANTHROPIC_API_KEY")
      )
      error_message = "dispatcher-agent-runner: rendered container_definitions is missing ANTHROPIC_API_KEY despite anthropic_api_key_secret_arn being set. See #3764 / parent #2840 for the silent-drop bug class this guards against."
    }
    postcondition {
      condition = (
        var.db_connection_secret_arn == "" ||
        strcontains(self.container_definitions, "DATABASE_URL")
      )
      error_message = "dispatcher-agent-runner: rendered container_definitions is missing DATABASE_URL despite db_connection_secret_arn being set. See #3764 / parent #2840 for the silent-drop bug class this guards against."
    }
    postcondition {
      condition = (
        var.github_token_secret_arn == "" ||
        strcontains(self.container_definitions, "GITHUB_TOKEN")
      )
      error_message = "dispatcher-agent-runner: rendered container_definitions is missing GITHUB_TOKEN despite github_token_secret_arn being set. See #3764 / parent #2840 for the silent-drop bug class this guards against."
    }
  }
}

# ── Agent-runner error alarm (#3093) ─────────────────────────────────────────
# Fires when any ERROR or FATAL structured-log event is emitted by an
# agent-runner task. A single unhandled exception inside a phase
# (unrecognized exit code, missing env var, AWS throttle) appears here
# before the daemon reap pass has a chance to notice the STOPPED task,
# giving operators an early signal.
#
# Gated behind enable_alerts (default false) so non-prod environments
# stay quiet. Wire enable_alerts = true in the dev module block once
# the SNS topic ARN is available (mirrors the dispatcher-daemon pattern).
#
# Mirrors the filter/alarm shape at
# infra/terraform/modules/dispatcher-daemon/main.tf (stuck_timeout_repeated).

resource "aws_cloudwatch_log_metric_filter" "agent_runner_errors" {
  count = var.enable_alerts ? 1 : 0

  name           = "${local.task_family}-errors"
  pattern        = "{ $.level = \"ERROR\" || $.level = \"FATAL\" }"
  log_group_name = aws_cloudwatch_log_group.agent_runner.name

  metric_transformation {
    name          = "AgentRunnerErrorCount"
    namespace     = "Judgemind/AgentRunner"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "agent_runner_errors" {
  count = var.enable_alerts ? 1 : 0

  alarm_name        = "${local.task_family}-errors"
  alarm_description = "An agent-runner task emitted an ERROR or FATAL log event (${var.environment}). A phase may have crashed before the daemon reap pass observed a STOPPED task — check ${aws_cloudwatch_log_group.agent_runner.name}."

  namespace   = "Judgemind/AgentRunner"
  metric_name = "AgentRunnerErrorCount"
  statistic   = "Sum"

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [var.alert_sns_topic_arn]
  ok_actions    = [var.alert_sns_topic_arn]
}
