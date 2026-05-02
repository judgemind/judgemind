# Dispatcher v3 task definitions module (F2, #3887).
#
# Registers four ECS Fargate task definitions, each baked from the F1
# image (#3915, judgemind/dispatcher-v3) with a different `command`
# argv selecting the per-task entrypoint inside the container:
#
#   * `<prefix>-launcher`        -- long-running scheduler. Used by F4's
#     ECS service (single replica). 1 vCPU / 2 GiB.
#   * `<prefix>-task-runner`     -- one-shot per-agent. Launched by the
#     launcher via ecs:RunTask. 4 vCPU / 16 GiB. Wall-clock cap 6h
#     (enforced launcher-side, see below).
#   * `<prefix>-diagnoser`       -- one-shot per-failure. Launched by
#     the launcher via ecs:RunTask after a task-runner exits non-zero
#     (or is StopTask'd by silent-hang detection). 2 vCPU / 8 GiB.
#     Wall-clock cap 1h.
#   * `<prefix>-scheduled-skill` -- one-shot per-cron-trigger. Launched
#     by EventBridge (F5) at the per-skill cadence. 2 vCPU / 8 GiB.
#     Wall-clock cap 2h. SKILL_NAME injected at RunTask time as an env
#     override.
#
# stopTimeout vs wall-clock cap (#3940 fix):
#
# Fargate REJECTS task definitions whose containerDefinitions[].stopTimeout
# exceeds 120 seconds:
#
#   ClientException: Tasks using the Fargate launch type must have a
#   container stop timeout of less than 120 seconds.
#
# Pre-#3940, the three short-lived task-defs set stopTimeout to the
# wall-clock cap value (21600 / 3600 / 7200), and every dev-apply since
# F2 landed failed at RegisterTaskDefinition. The fix splits the two
# concepts:
#
#   * stopTimeout = 120 (the platform cap on SIGTERM-to-SIGKILL grace).
#     Hardcoded via local.fargate_stop_timeout_seconds. Same as the
#     launcher task-def.
#   * Wall-clock cap (21600 / 3600 / 7200) is the launcher-side timeout
#     enforced by the silent-hang detector loop in
#     `scripts/dispatcher_v3/launcher.py` (`_watch_in_flight`). The
#     launcher reads each cap from its own environment block (env vars
#     TASK_RUNNER_WALL_CLOCK_SECONDS / DIAGNOSER_WALL_CLOCK_SECONDS /
#     SCHEDULED_SKILL_WALL_CLOCK_SECONDS, threaded into the launcher
#     task-def's `environment` array below) and ecs:StopTask's any
#     RUNNING task whose `now - started_at` exceeds it, marking the
#     agent row failed with `exit_reason='wall_clock_exceeded'`.
#
# A postcondition on each rendered container_definitions JSON asserts
# `"stopTimeout":120` so a future regression that re-introduces the long
# values fails at plan/apply time rather than burying the failure in
# the dev-apply workflow's terraform output (#3941 retro).
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

  # Fargate platform cap on the SIGTERM-to-SIGKILL grace window (the
  # `stopTimeout` field). AWS rejects task-defs that exceed this value
  # at RegisterTaskDefinition time -- see file-level docstring for the
  # #3940 background. This local is the single source of truth for the
  # rendered `stopTimeout` field across all four task-defs in this
  # module; the per-task-def wall-clock caps are enforced launcher-
  # side and are independent of `stopTimeout`.
  fargate_stop_timeout_seconds = 120

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

  # Launcher-only environment block. Merges two contributions:
  #
  #   * #3939 -- launcher → task-runner network handoff. The launcher's
  #     `_build_launcher_from_env` reads TASK_RUNNER_TASK_DEFINITION /
  #     AGENT_RUNNER_SUBNET_IDS / AGENT_RUNNER_SECURITY_GROUP_ID at boot
  #     to assemble an `ecs:RunTask` request for each claimed issue.
  #     Family-only TASK_RUNNER_TASK_DEFINITION lets revisions roll
  #     forward automatically (a fresh F2 apply registers a new revision
  #     and the next claim picks it up without an explicit env-var update).
  #
  #   * #3940 -- wall-clock cap enforcement. The launcher's
  #     `_watch_in_flight` reads TASK_RUNNER_WALL_CLOCK_SECONDS /
  #     DIAGNOSER_WALL_CLOCK_SECONDS / SCHEDULED_SKILL_WALL_CLOCK_SECONDS
  #     and ecs:StopTask's any RUNNING task whose `now - started_at`
  #     exceeds its per-task-def cap. Rendered as strings via
  #     `tostring()` (parsed back to int by `_parse_int_env`).
  #
  # All five env vars are meaningless for the task-runner / diagnoser /
  # scheduled-skill task-defs (those are the *callees*, not the caller).
  launcher_environment = concat(
    local.common_environment,
    [
      { name = "TASK_RUNNER_TASK_DEFINITION", value = local.family_task_runner },
      { name = "AGENT_RUNNER_SUBNET_IDS", value = join(",", var.agent_runner_subnet_ids) },
      { name = "AGENT_RUNNER_SECURITY_GROUP_ID", value = var.agent_runner_security_group_id },
      {
        name  = "TASK_RUNNER_WALL_CLOCK_SECONDS"
        value = tostring(var.task_runner_stop_timeout_seconds)
      },
      {
        name  = "DIAGNOSER_WALL_CLOCK_SECONDS"
        value = tostring(var.diagnoser_stop_timeout_seconds)
      },
      {
        name  = "SCHEDULED_SKILL_WALL_CLOCK_SECONDS"
        value = tostring(var.scheduled_skill_stop_timeout_seconds)
      },
    ],
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
      stopTimeout = local.fargate_stop_timeout_seconds

      environment = local.launcher_environment
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
    # Launcher-only env vars (#3939). Same defense-in-depth pattern as
    # the secret postconditions above: the launcher's
    # `_build_launcher_from_env` raises KeyError on a missing
    # TASK_RUNNER_TASK_DEFINITION / AGENT_RUNNER_SUBNET_IDS /
    # AGENT_RUNNER_SECURITY_GROUP_ID at boot, so a regression that
    # drops them from the rendered container_definitions would crashloop
    # the launcher silently from terraform's perspective. These checks
    # turn the failure into a plan-time error.
    postcondition {
      condition     = strcontains(self.container_definitions, "TASK_RUNNER_TASK_DEFINITION")
      error_message = "dispatcher-v3-task-defs (launcher): rendered container_definitions is missing TASK_RUNNER_TASK_DEFINITION. The launcher's _build_launcher_from_env raises KeyError on this var at boot -- see #3939."
    }
    postcondition {
      condition     = strcontains(self.container_definitions, "AGENT_RUNNER_SUBNET_IDS")
      error_message = "dispatcher-v3-task-defs (launcher): rendered container_definitions is missing AGENT_RUNNER_SUBNET_IDS. The launcher needs this to assemble the ecs:RunTask awsvpcConfiguration -- see #3939."
    }
    postcondition {
      condition     = strcontains(self.container_definitions, "AGENT_RUNNER_SECURITY_GROUP_ID")
      error_message = "dispatcher-v3-task-defs (launcher): rendered container_definitions is missing AGENT_RUNNER_SECURITY_GROUP_ID. The launcher's _build_launcher_from_env raises KeyError on this var at boot -- see #3939."
    }
    # The launcher's wall-clock-cap enforcement loop reads each
    # per-task-def cap from the launcher container's environment block
    # (`TASK_RUNNER_WALL_CLOCK_SECONDS` / `DIAGNOSER_WALL_CLOCK_SECONDS`
    # / `SCHEDULED_SKILL_WALL_CLOCK_SECONDS`). A regression that drops
    # any of those env vars silently disables the wall-clock cap for
    # that task-def -- agents wedge for the full Fargate platform max
    # (~14 days) instead of being reaped after 6h / 1h / 2h. Pin all
    # three so the failure is loud at apply time. See #3940.
    postcondition {
      condition     = strcontains(self.container_definitions, "TASK_RUNNER_WALL_CLOCK_SECONDS")
      error_message = "dispatcher-v3-task-defs (launcher): rendered environment is missing TASK_RUNNER_WALL_CLOCK_SECONDS. The launcher's wall-clock-cap detector reads this env var; without it, task-runner agents wedge for the Fargate platform max instead of the 6h cap. See #3940."
    }
    postcondition {
      condition     = strcontains(self.container_definitions, "DIAGNOSER_WALL_CLOCK_SECONDS")
      error_message = "dispatcher-v3-task-defs (launcher): rendered environment is missing DIAGNOSER_WALL_CLOCK_SECONDS. See #3940."
    }
    postcondition {
      condition     = strcontains(self.container_definitions, "SCHEDULED_SKILL_WALL_CLOCK_SECONDS")
      error_message = "dispatcher-v3-task-defs (launcher): rendered environment is missing SCHEDULED_SKILL_WALL_CLOCK_SECONDS. See #3940."
    }
    # Regression-class defense for the #3940 root cause. Fargate
    # rejects task-defs whose `stopTimeout` exceeds 120 seconds at
    # RegisterTaskDefinition time, so a future change that re-points
    # `stopTimeout` at a `var.*_stop_timeout_seconds` value (or any
    # other multi-hour number) breaks dev-apply silently from the
    # operator's perspective. Asserting the rendered string contains
    # exactly `"stopTimeout":120` catches the regression at plan/apply
    # time. See #3941 retro for the rationale.
    postcondition {
      condition     = strcontains(self.container_definitions, "\"stopTimeout\":120")
      error_message = "dispatcher-v3-task-defs (launcher): rendered stopTimeout != 120. Fargate rejects task-defs with stopTimeout > 120s. See #3940."
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

      # Fargate platform-level cap on SIGTERM-to-SIGKILL grace (max
      # 120s). The 6h wall-clock cap from
      # `var.task_runner_stop_timeout_seconds` is enforced launcher-
      # side -- the launcher reads the value via the
      # `TASK_RUNNER_WALL_CLOCK_SECONDS` env var on its own task-def
      # and ecs:StopTask's any task-runner whose `now - started_at`
      # exceeds the cap. Setting `stopTimeout` to the wall-clock value
      # would cause RegisterTaskDefinition to reject the task-def with
      # `ClientException: Tasks using the Fargate launch type must
      # have a container stop timeout of less than 120 seconds.` See
      # the file-level docstring + #3940 for the full design.
      stopTimeout = local.fargate_stop_timeout_seconds

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
    postcondition {
      condition     = strcontains(self.container_definitions, "\"stopTimeout\":120")
      error_message = "dispatcher-v3-task-defs (task-runner): rendered stopTimeout != 120. Fargate rejects task-defs with stopTimeout > 120s; the wall-clock cap is enforced launcher-side via TASK_RUNNER_WALL_CLOCK_SECONDS. See #3940."
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

      # Fargate platform cap (120s) -- the 1h wall-clock cap from
      # `var.diagnoser_stop_timeout_seconds` is enforced launcher-side
      # via the `DIAGNOSER_WALL_CLOCK_SECONDS` env var on the launcher
      # task-def. See the task-runner block above + #3940 for the
      # design.
      stopTimeout = local.fargate_stop_timeout_seconds

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
    postcondition {
      condition     = strcontains(self.container_definitions, "\"stopTimeout\":120")
      error_message = "dispatcher-v3-task-defs (diagnoser): rendered stopTimeout != 120. Fargate rejects task-defs with stopTimeout > 120s; the wall-clock cap is enforced launcher-side via DIAGNOSER_WALL_CLOCK_SECONDS. See #3940."
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

      # Fargate platform cap (120s) -- the 2h wall-clock cap from
      # `var.scheduled_skill_stop_timeout_seconds` is enforced
      # launcher-side via the `SCHEDULED_SKILL_WALL_CLOCK_SECONDS` env
      # var on the launcher task-def. See the task-runner block above
      # + #3940 for the design.
      stopTimeout = local.fargate_stop_timeout_seconds

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
    postcondition {
      condition     = strcontains(self.container_definitions, "\"stopTimeout\":120")
      error_message = "dispatcher-v3-task-defs (scheduled-skill): rendered stopTimeout != 120. Fargate rejects task-defs with stopTimeout > 120s; the wall-clock cap is enforced launcher-side via SCHEDULED_SKILL_WALL_CLOCK_SECONDS. See #3940."
    }
  }
}
