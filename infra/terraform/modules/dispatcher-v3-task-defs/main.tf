# Dispatcher v3 task definitions module (F2, #3887).
#
# Registers four ECS Fargate task definitions, each baked from the F1
# image (#3915, judgemind/dispatcher-v3) with a different `command`
# argv selecting the per-task entrypoint inside the container:
#
#   * `<prefix>-launcher`        -- long-running scheduler. Used by F4's
#     ECS service (single replica). 1 vCPU / 2 GiB.
#   * `<prefix>-task-runner`     -- one-shot per-agent. Launched by the
#     launcher via ecs:RunTask. 4 vCPU / 16 GiB. stopTimeout 6h.
#   * `<prefix>-diagnoser`       -- one-shot per-failure. Launched by
#     the launcher via ecs:RunTask after a task-runner exits non-zero
#     (or is StopTask'd by silent-hang detection). 2 vCPU / 8 GiB.
#     stopTimeout 1h.
#   * `<prefix>-scheduled-skill` -- one-shot per-cron-trigger. Launched
#     by EventBridge (F5) at the per-skill cadence. 2 vCPU / 8 GiB.
#     stopTimeout 2h. SKILL_NAME injected at RunTask time as an env
#     override.
#
# The single F1 image is digest-pinned at apply time via
# `data "aws_ecr_image"`. Every rendered task-def references
# `<repo>@sha256:<digest>` -- the mutable `:latest` tag is never
# baked in. This is the structural defense against the v2 image-
# staleness drift documented in #3754: a digest-pinned task-def
# revision is immutable, and every image push must register a new
# revision (driven by the dev-apply workflow rerunning after the
# deploy-dispatcher-v3 build) to take effect. See
# `docs/specs/dispatcher-v3-spec.md` section 6 ("One image, three
# task definitions").
#
# Cohabitation note: this module deliberately does NOT provision an
# ECS service for the launcher -- F4 (follow-up) wires that. Until
# F4 lands, the launcher task-def exists but nothing runs.
#
# Resources created (per task-def family):
#   * aws_cloudwatch_log_group at /judgemind/dispatcher-v3/<role>
#   * aws_ecs_task_definition  at <prefix>-<role>
# All four task-defs share `var.execution_role_arn` from F3. The
# launcher task-def uses `var.launcher_role_arn` as taskRoleArn; the
# other three share `var.agent_task_role_arn`.

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# Resolve the F1 image's digest at apply time. The deploy-dispatcher-v3
# workflow tags every push as <sha7>, latest, and <branch>; this data
# source pulls the digest of `var.image_tag` (default `latest`) so the
# rendered task-defs reference an immutable @sha256:... reference instead
# of the mutable tag. Re-running terraform apply after a fresh deploy
# resolves a new digest and registers fresh task-def revisions.
data "aws_ecr_image" "dispatcher_v3" {
  repository_name = var.ecr_repository_name
  image_tag       = var.image_tag
}

locals {
  # Family names -- referenced by F3's launcher RunTask policy
  # (`agent_task_def_families`) and by F4's ECS service.
  family_launcher        = "${var.task_definition_family_prefix}-launcher"
  family_task_runner     = "${var.task_definition_family_prefix}-task-runner"
  family_diagnoser       = "${var.task_definition_family_prefix}-diagnoser"
  family_scheduled_skill = "${var.task_definition_family_prefix}-scheduled-skill"

  # CloudWatch log group names follow the spec section 6 pattern:
  # /judgemind/dispatcher-v3/<role>. Same prefix the F3 launcher role's
  # logs:GetLogEvents grant already covers (see dispatcher-v3-iam:
  # `launcher_logs_read` policy resource arn block).
  log_group_launcher        = "/judgemind/dispatcher-v3/launcher"
  log_group_task_runner     = "/judgemind/dispatcher-v3/task-runner"
  log_group_diagnoser       = "/judgemind/dispatcher-v3/diagnoser"
  log_group_scheduled_skill = "/judgemind/dispatcher-v3/scheduled-skill"

  # Digest-pinned image reference. Baked into every task-def -- no
  # task-def in this module ever references a mutable tag.
  image_digest_ref = "${var.ecr_repository_url}@${data.aws_ecr_image.dispatcher_v3.image_digest}"

  # Common environment block shared by all four task-defs. Per-task-def
  # additions (TASK_ISSUE_NUMBER, AGENT_ID, SKILL_NAME, etc.) are
  # injected at RunTask time via env overrides -- this module's
  # rendered environment only carries the constant values.
  common_environment = concat(
    [
      { name = "ENVIRONMENT", value = var.environment },
      { name = "ECS_CLUSTER_ARN", value = var.ecs_cluster_arn },
      { name = "REPO_URL", value = var.repo_url },
    ],
    var.github_repo != "" ? [{ name = "GITHUB_REPO", value = var.github_repo }] : [],
    var.sessions_bucket_name != "" ? [{ name = "SESSIONS_BUCKET", value = var.sessions_bucket_name }] : [],
  )

  # Common secrets block -- agent task-defs (task-runner / diagnoser /
  # scheduled-skill) all read the same set. The launcher reads a
  # narrower subset (DATABASE_URL + TELEGRAM_BOT_TOKEN; no
  # ANTHROPIC_API_KEY because the launcher never invokes claude).
  agent_secrets = concat(
    var.anthropic_api_key_secret_arn != "" ? [
      {
        name      = "ANTHROPIC_API_KEY"
        valueFrom = var.anthropic_api_key_secret_arn
      }
    ] : [],
    var.db_connection_secret_arn != "" ? [
      {
        # Same JSON-key-suffix pattern as the v2 modules -- the
        # dispatcher-role secret stores DATABASE_URL under the `url` key.
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

  launcher_secrets = concat(
    var.db_connection_secret_arn != "" ? [
      {
        name      = "DATABASE_URL"
        valueFrom = "${var.db_connection_secret_arn}:url::"
      }
    ] : [],
    var.telegram_bot_token_secret_arn != "" ? [
      {
        name      = "TELEGRAM_BOT_TOKEN"
        valueFrom = var.telegram_bot_token_secret_arn
      }
    ] : [],
  )
}

# ------------------------------------------------------------------
# CloudWatch Log Groups (one per task-def family).
# Pattern: /judgemind/dispatcher-v3/<role>.
# ------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "launcher" {
  name              = local.log_group_launcher
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "task_runner" {
  name              = local.log_group_task_runner
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "diagnoser" {
  name              = local.log_group_diagnoser
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "scheduled_skill" {
  name              = local.log_group_scheduled_skill
  retention_in_days = var.log_retention_days
}

# ------------------------------------------------------------------
# Task definition: launcher
# Long-running scheduler. The Dockerfile.dispatcher-v3 ENTRYPOINT
# (sh -c "exec \"$@\"") makes `command` the actual argv exec'd inside
# the container. The launcher entrypoint is `python -m
# dispatcher_v3.launcher`.
# ------------------------------------------------------------------

resource "aws_ecs_task_definition" "launcher" {
  family                   = local.family_launcher
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.launcher_cpu
  memory                   = var.launcher_memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.launcher_role_arn

  container_definitions = jsonencode([
    {
      name      = "launcher"
      image     = local.image_digest_ref
      essential = true

      # `command` overrides Dockerfile ENTRYPOINT's argv. F1's
      # ENTRYPOINT is `sh -c "exec \"$@\""` so this list is exec'd
      # verbatim inside the container.
      command = ["python", "-m", "dispatcher_v3.launcher"]

      # Fargate platform-level cap on the SIGTERM grace window. The
      # launcher must persist a final heartbeat row before exiting --
      # 120s is enough for the scheduler tick to drain and write its
      # final UPDATE.
      stopTimeout = 120

      environment = local.common_environment
      secrets     = local.launcher_secrets

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.launcher.name
          "awslogs-region"        = data.aws_region.current.id
          "awslogs-stream-prefix" = "launcher"
        }
      }
    }
  ])

  # Content-level postcondition -- defense in depth against the #2840 /
  # #3764 silent-drop pattern where a non-empty ARN variable somehow
  # fails to make it into the rendered container_definitions JSON.
  lifecycle {
    postcondition {
      condition = (
        var.db_connection_secret_arn == "" ||
        strcontains(self.container_definitions, "DATABASE_URL")
      )
      error_message = "dispatcher-v3-task-defs (launcher): rendered container_definitions is missing DATABASE_URL despite db_connection_secret_arn being set. See #3764 / parent #2840."
    }
    postcondition {
      condition = (
        var.telegram_bot_token_secret_arn == "" ||
        strcontains(self.container_definitions, "TELEGRAM_BOT_TOKEN")
      )
      error_message = "dispatcher-v3-task-defs (launcher): rendered container_definitions is missing TELEGRAM_BOT_TOKEN despite telegram_bot_token_secret_arn being set. See #3764 / parent #2840."
    }
    # The image must be digest-pinned -- the rendered JSON must contain
    # `@sha256:` (from data.aws_ecr_image) and must NOT contain `:latest`
    # / `:<tag>` after the repo URL. Defense against a future regression
    # that drops the digest lookup and falls back to the mutable tag.
    postcondition {
      condition     = strcontains(self.container_definitions, "@sha256:")
      error_message = "dispatcher-v3-task-defs (launcher): rendered image reference is not digest-pinned. See #3754 image-staleness drift -- the v3 task-defs structurally prevent this by always referencing @sha256:<digest>."
    }
  }
}

# ------------------------------------------------------------------
# Task definition: task-runner
# One-shot per-agent. Launched by the launcher via ecs:RunTask with
# `TASK_ISSUE_NUMBER=<n>` and `AGENT_ID=<uuid>` env overrides. Per
# spec section 4.1, the entrypoint is `python -m
# dispatcher_v3.agent_runner` -- not the v2 bash entrypoint -- so the
# argv stays a single source of truth via dispatcher_v3.runners.build_argv.
# ------------------------------------------------------------------

resource "aws_ecs_task_definition" "task_runner" {
  family                   = local.family_task_runner
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_runner_cpu
  memory                   = var.task_runner_memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.agent_task_role_arn

  ephemeral_storage {
    size_in_gib = var.task_runner_ephemeral_storage_gib
  }

  container_definitions = jsonencode([
    {
      name      = "task-runner"
      image     = local.image_digest_ref
      essential = true

      command = ["python", "-m", "dispatcher_v3.agent_runner"]

      # Wall-clock cap from issue body (6h). The launcher's silent-hang
      # detector (spec section 4.1) reads this off the task-def metadata
      # to know when to StopTask a task-runner. Fargate's own cap on
      # SIGTERM-to-SIGKILL is platform-level (120s) and unrelated; the
      # six-hour value here is the launcher-side timeout.
      stopTimeout = var.task_runner_stop_timeout_seconds

      environment = local.common_environment
      secrets     = local.agent_secrets

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.task_runner.name
          "awslogs-region"        = data.aws_region.current.id
          "awslogs-stream-prefix" = "task-runner"
        }
      }
    }
  ])

  lifecycle {
    postcondition {
      condition = (
        var.anthropic_api_key_secret_arn == "" ||
        strcontains(self.container_definitions, "ANTHROPIC_API_KEY")
      )
      error_message = "dispatcher-v3-task-defs (task-runner): rendered container_definitions is missing ANTHROPIC_API_KEY despite anthropic_api_key_secret_arn being set. See #3764 / parent #2840."
    }
    postcondition {
      condition = (
        var.db_connection_secret_arn == "" ||
        strcontains(self.container_definitions, "DATABASE_URL")
      )
      error_message = "dispatcher-v3-task-defs (task-runner): rendered container_definitions is missing DATABASE_URL despite db_connection_secret_arn being set. See #3764 / parent #2840."
    }
    postcondition {
      condition = (
        var.github_token_secret_arn == "" ||
        strcontains(self.container_definitions, "GITHUB_TOKEN")
      )
      error_message = "dispatcher-v3-task-defs (task-runner): rendered container_definitions is missing GITHUB_TOKEN despite github_token_secret_arn being set. See #3764 / parent #2840."
    }
    postcondition {
      condition     = strcontains(self.container_definitions, "@sha256:")
      error_message = "dispatcher-v3-task-defs (task-runner): rendered image reference is not digest-pinned. See #3754 image-staleness drift."
    }
  }
}

# ------------------------------------------------------------------
# Task definition: diagnoser
# One-shot per-failure. Launched by the launcher via ecs:RunTask with
# `AGENT_ID=<uuid>` env override after a task-runner exits non-zero.
# Per spec section 4.2 the entrypoint runs `claude -p
# "/diagnose-failure $AGENT_ID"` via a thin Python wrapper.
# ------------------------------------------------------------------

resource "aws_ecs_task_definition" "diagnoser" {
  family                   = local.family_diagnoser
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.diagnoser_cpu
  memory                   = var.diagnoser_memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.agent_task_role_arn

  ephemeral_storage {
    size_in_gib = var.diagnoser_ephemeral_storage_gib
  }

  container_definitions = jsonencode([
    {
      name      = "diagnoser"
      image     = local.image_digest_ref
      essential = true

      command = ["python", "-m", "dispatcher_v3.diagnoser_runner"]

      # 1h cap per issue body -- the diagnoser is a read + side-effect
      # skill that should not need long.
      stopTimeout = var.diagnoser_stop_timeout_seconds

      environment = local.common_environment
      secrets     = local.agent_secrets

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.diagnoser.name
          "awslogs-region"        = data.aws_region.current.id
          "awslogs-stream-prefix" = "diagnoser"
        }
      }
    }
  ])

  lifecycle {
    postcondition {
      condition = (
        var.anthropic_api_key_secret_arn == "" ||
        strcontains(self.container_definitions, "ANTHROPIC_API_KEY")
      )
      error_message = "dispatcher-v3-task-defs (diagnoser): rendered container_definitions is missing ANTHROPIC_API_KEY despite anthropic_api_key_secret_arn being set. See #3764 / parent #2840."
    }
    postcondition {
      condition = (
        var.db_connection_secret_arn == "" ||
        strcontains(self.container_definitions, "DATABASE_URL")
      )
      error_message = "dispatcher-v3-task-defs (diagnoser): rendered container_definitions is missing DATABASE_URL despite db_connection_secret_arn being set. See #3764 / parent #2840."
    }
    postcondition {
      condition = (
        var.github_token_secret_arn == "" ||
        strcontains(self.container_definitions, "GITHUB_TOKEN")
      )
      error_message = "dispatcher-v3-task-defs (diagnoser): rendered container_definitions is missing GITHUB_TOKEN despite github_token_secret_arn being set. See #3764 / parent #2840."
    }
    postcondition {
      condition     = strcontains(self.container_definitions, "@sha256:")
      error_message = "dispatcher-v3-task-defs (diagnoser): rendered image reference is not digest-pinned. See #3754 image-staleness drift."
    }
  }
}

# ------------------------------------------------------------------
# Task definition: scheduled-skill
# One-shot per-cron-trigger. Launched by EventBridge (F5) with
# `SKILL_NAME=<name>` env override. The entrypoint is a thin Python
# wrapper that runs `claude -p /$SKILL_NAME`. Spec section 4.4.
# ------------------------------------------------------------------

resource "aws_ecs_task_definition" "scheduled_skill" {
  family                   = local.family_scheduled_skill
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.scheduled_skill_cpu
  memory                   = var.scheduled_skill_memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.agent_task_role_arn

  ephemeral_storage {
    size_in_gib = var.scheduled_skill_ephemeral_storage_gib
  }

  container_definitions = jsonencode([
    {
      name      = "scheduled-skill"
      image     = local.image_digest_ref
      essential = true

      command = ["python", "-m", "dispatcher_v3.scheduled_skill_runner"]

      # 2h cap per issue body -- audit / dispatcher-audit / spotcheck
      # / dispatcher-daily-report all sit comfortably under this.
      stopTimeout = var.scheduled_skill_stop_timeout_seconds

      environment = local.common_environment
      secrets     = local.agent_secrets

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.scheduled_skill.name
          "awslogs-region"        = data.aws_region.current.id
          "awslogs-stream-prefix" = "scheduled-skill"
        }
      }
    }
  ])

  lifecycle {
    postcondition {
      condition = (
        var.anthropic_api_key_secret_arn == "" ||
        strcontains(self.container_definitions, "ANTHROPIC_API_KEY")
      )
      error_message = "dispatcher-v3-task-defs (scheduled-skill): rendered container_definitions is missing ANTHROPIC_API_KEY despite anthropic_api_key_secret_arn being set. See #3764 / parent #2840."
    }
    postcondition {
      condition = (
        var.db_connection_secret_arn == "" ||
        strcontains(self.container_definitions, "DATABASE_URL")
      )
      error_message = "dispatcher-v3-task-defs (scheduled-skill): rendered container_definitions is missing DATABASE_URL despite db_connection_secret_arn being set. See #3764 / parent #2840."
    }
    postcondition {
      condition = (
        var.github_token_secret_arn == "" ||
        strcontains(self.container_definitions, "GITHUB_TOKEN")
      )
      error_message = "dispatcher-v3-task-defs (scheduled-skill): rendered container_definitions is missing GITHUB_TOKEN despite github_token_secret_arn being set. See #3764 / parent #2840."
    }
    postcondition {
      condition     = strcontains(self.container_definitions, "@sha256:")
      error_message = "dispatcher-v3-task-defs (scheduled-skill): rendered image reference is not digest-pinned. See #3754 image-staleness drift."
    }
  }
}
