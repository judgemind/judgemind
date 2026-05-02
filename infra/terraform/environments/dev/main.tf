# Dev environment infrastructure.
#
# Manages networking, storage, IAM, compute, and email for the dev environment.
#
# The dev S3 bucket (judgemind-document-archive-dev) was initially created
# manually. To bring it under Terraform management, import it once:
#
#   terraform import module.document_archive.aws_s3_bucket.document_archive \
#     judgemind-document-archive-dev
#
# Object lock is intentionally disabled for dev so test objects can be deleted.

data "aws_caller_identity" "current" {}

# Look up API key secrets so we can pass their ARNs to the compute module
# without hardcoding the random Secrets Manager suffix.
data "aws_secretsmanager_secret" "anthropic_api_key" {
  name = "judgemind/anthropic/api-key"
}

data "aws_secretsmanager_secret" "google_api_key" {
  name = "judgemind/google/api-key"
}

data "aws_secretsmanager_secret" "residential_proxy" {
  name = "judgemind/proxy/residential"
}

data "aws_secretsmanager_secret" "courtlistener_api_token" {
  name = "judgemind/courtlistener/api-token"
}

data "aws_secretsmanager_secret" "capsolver_api_key" {
  name = "judgemind/capsolver/api-key"
}

module "networking" {
  source      = "../../modules/networking"
  environment = "dev"
}

module "ecr" {
  source      = "../../modules/ecr"
  environment = "dev"

  enable_pull_policy          = true
  ecs_task_execution_role_arn = module.compute.task_execution_role_arn
}

module "document_archive" {
  source = "../../modules/storage"

  bucket_name        = "judgemind-document-archive-dev"
  environment        = "dev"
  enable_object_lock = false
}

module "iam_scraper" {
  source = "../../modules/iam_scraper"

  environment                 = "dev"
  document_archive_bucket_arn = module.document_archive.bucket_arn
}

module "iam_agent" {
  source = "../../modules/iam_agent"

  environment                 = "dev"
  document_archive_bucket_arn = module.document_archive.bucket_arn
  scraper_role_arn            = module.iam_scraper.role_arn
}

module "database" {
  source = "../../modules/database"

  environment = "dev"
  vpc_id      = module.networking.vpc_id
  subnet_ids  = module.networking.private_subnet_ids
  # db.t4g.small: 2 GB RAM → max_connections ≈ 170. Previously db.t4g.micro
  # yielded only ≈ 84 connections, which was exhausted by
  # ``rebuild_db.py --concurrency 64`` (each ProcessPoolExecutor worker opens
  # its own psycopg connection) plus the ingestion worker, API, and any
  # concurrent backfill task, producing the ``rds_reserved`` FATAL errors
  # documented in #2549. See docs/agent/infrastructure-reference.md
  # §Dev DB Connection Budget.
  instance_class = "db.t4g.small"

  # Dev applies instance-class and parameter-group changes on the next apply
  # rather than deferring to the weekly maintenance window (Sun 05:00-06:00 UTC).
  # Production keeps the default (false) to minimize surprise reboots during
  # business hours. See #2573.
  apply_immediately = true
}

module "cache" {
  source = "../../modules/cache"

  environment        = "dev"
  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids
  node_type          = "cache.t4g.micro"
  num_cache_nodes    = 1

  # Dev applies node_type / engine_version / parameter-group changes on the
  # next apply rather than deferring to the weekly ElastiCache maintenance
  # window. Production keeps the default (false) so surprise reboots don't
  # land during business hours. See #2573 (RDS) and #2581 (this extension).
  apply_immediately = true
}

module "compute" {
  source = "../../modules/compute"

  environment                        = "dev"
  vpc_id                             = module.networking.vpc_id
  private_subnet_ids                 = module.networking.private_subnet_ids
  ecr_repository_url                 = module.ecr.repository_url
  scraper_task_role_arn              = module.iam_scraper.role_arn
  redis_url                          = "redis://${module.cache.redis_endpoint}:${module.cache.redis_port}"
  document_archive_bucket            = module.document_archive.bucket_id
  db_connection_secret_arn           = module.database.db_connection_secret_arn
  opensearch_url                     = "https://${module.search.domain_endpoint}"
  opensearch_credentials_secret_arn  = module.search.master_credentials_secret_arn
  llm_provider                       = "anthropic"
  anthropic_api_key_secret_arn       = data.aws_secretsmanager_secret.anthropic_api_key.arn
  google_api_key_secret_arn          = data.aws_secretsmanager_secret.google_api_key.arn
  proxy_secret_arn                   = data.aws_secretsmanager_secret.residential_proxy.arn
  courtlistener_api_token_secret_arn = data.aws_secretsmanager_secret.courtlistener_api_token.arn
  capsolver_api_key_secret_arn       = data.aws_secretsmanager_secret.capsolver_api_key.arn

  # Dev: 1 vCPU, 2 GB RAM, twice-daily schedule at 6:15 AM and 6:15 PM PT
  # Matches production to prevent OOM kills during the full 17-scraper run.
  # See #2349 — 0.5 vCPU / 1 GB was insufficient and caused the task to be
  # killed before completing all scrapers.
  task_cpu            = 1024
  task_memory         = 2048
  schedule_expression = "cron(15 6,18 * * ? *)"
  schedule_timezone   = "America/Los_Angeles"
  schedule_enabled    = true
  log_retention_days  = 14
  enable_alerts       = true
}

module "search" {
  source = "../../modules/search"

  environment        = "dev"
  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids

  # Dev: single t3.small.search node, 20 GiB EBS
  instance_type   = "t3.small.search"
  instance_count  = 1
  ebs_volume_size = 20

  principal_arns = [
    # API ECS task role -- name is deterministic from modules/api-service/main.tf.
    # Built as a string (not module.api_service.task_role_arn) to avoid a
    # Terraform dependency cycle: api_service already consumes
    # module.search.master_credentials_secret_arn / domain_endpoint.
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/judgemind-api-task-dev",
    module.iam_scraper.role_arn, # ingestion-worker + ECS-oneshot tasks
  ]
}

module "api_service" {
  source = "../../modules/api-service"

  environment        = "dev"
  vpc_id             = module.networking.vpc_id
  public_subnet_ids  = module.networking.public_subnet_ids
  private_subnet_ids = module.networking.private_subnet_ids
  ecs_cluster_arn    = module.compute.cluster_arn
  ecs_cluster_name   = module.compute.cluster_name
  execution_role_arn = module.compute.task_execution_role_arn
  ecr_repository_url = module.ecr.api_repository_url
  domain_name        = "dev.api.judgemind.org"

  db_connection_secret_arn          = module.database.db_connection_secret_arn
  redis_url                         = "redis://${module.cache.redis_endpoint}:${module.cache.redis_port}"
  opensearch_url                    = "https://${module.search.domain_endpoint}"
  opensearch_credentials_secret_arn = module.search.master_credentials_secret_arn
  cors_allowed_origins              = "https://dev.judgemind.org"
  document_archive_bucket_arn       = module.document_archive.bucket_arn
  ses_configuration_set_name        = module.ses.configuration_set_name
  email_from                        = "no-reply@judgemind.org"

  # Note: `github_token_secret_arn` was deliberately removed from the
  # api-service module in #2820. The previous wiring (#2818) gave the
  # API container a PAT so its dispatcher admin resolvers could enrich
  # queue rows by hitting the GitHub REST API; #2820 moved that
  # enrichment into the daemon and deleted the fetch path, so the API
  # no longer needs (and must not have) GitHub credentials. Absence of
  # the credential is the strongest guarantee that absence of the fetch
  # code is load-bearing.

  # Dev: 0.25 vCPU, 512 MB, single replica
  task_cpu           = 256
  task_memory        = 512
  desired_count      = 1
  log_retention_days = 14

  # API error monitoring — use the same SNS topic as scraper alerts
  enable_alerts       = true
  alert_sns_topic_arn = module.compute.alerts_topic_arn

  # Dev thresholds are more lenient since testing generates 4xx errors
  error_5xx_threshold   = 10
  error_4xx_threshold   = 200
  latency_p99_threshold = 5
}

module "ses" {
  source = "../../modules/ses"

  environment    = "dev"
  sending_domain = "judgemind.org"
}

# ─── Dispatcher daemon (dispatcher v2, Phase 1) ─────────────────────────────
# Per `docs/specs/dispatcher-v2-spec.md` §14. Landed at `desired_count=0` —
# service, task definition, IAM, log group, and heartbeat alarm all exist,
# but nothing runs until sub-task C (#2729) publishes the real image and
# Phase 2 flips the replica count.
#
# Secrets wiring lands incrementally. #2700 wires `GITHUB_TOKEN` (the
# scoped PAT from spike 0.7, stored in `judgemind/dispatcher/github-token`).
# The remaining ARNs stay as empty strings until their owning sub-tasks
# land — the module's task definition uses `compact()` to skip any unset
# secret and the service is inert at `desired_count=0` anyway.
module "dispatcher_daemon" {
  source = "../../modules/dispatcher-daemon"

  environment        = "dev"
  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids
  ecs_cluster_arn    = module.compute.cluster_arn
  # Dispatcher has its own ECR repo (`judgemind/dispatcher`) — NOT the
  # scraper repo. Sub-task C (#2729) added the ECR resource + output.
  ecr_repository_url = module.ecr.dispatcher_repository_url

  # Phase 2 shadow mode (#2768): daemon runs a single task that scans the
  # `agent/ready` queue every 30s, writes snapshots to
  # `dispatcher.queue_snapshots`, and emits the `HeartbeatAge` CloudWatch
  # metric. `concurrency_cap=0` in `dispatcher.config` keeps the spawn
  # path dormant (a defensive no-op guard in the daemon scheduler tick
  # re-enforces this on every tick). Never > 1 — the dispatcher is a
  # singleton and overlapping instances would double-spawn agents.
  desired_count = 1

  # CPU + memory sized for ralph's in-container pre-push gate (#2962) and
  # anticipated cap>1 future state.
  #
  # Memory — #2568 showed pytest was SIGKILL'd running the 7200-test
  # scraper-framework suite under the 2 GiB spec default. Bumped to 8 GiB
  # on #2993 (2026-04-21) to unblock. Now bumping to 16 GiB so parallel
  # pytest workers (`-n auto` per this PR) have headroom.
  #
  # CPU — 1 vCPU serializes daemon + ralph subprocess + pytest workers on
  # a single core. #2613 ("watchdog for retired.cc-courts.org sunset
  # signals") hit the 3h `subprocess_timeout_s` twice at 1 vCPU. Bumping
  # to 4 vCPU so `pytest -n auto` can parallelize and ralph's
  # reviewer/worker loop runs concurrently with the daemon's tick.
  #
  # Cost delta vs 1 vCPU + 8 GiB baseline: ~$0.12/hr (~$88/mo for the
  # singleton dev dispatcher). Acceptable for a workload where each
  # failed 3h ralph retry burns $20-40 in Claude API spend.
  #
  # Fargate CPU/RAM constraint: at 4 vCPU, RAM must be 8-30 GiB. 16 GiB
  # lands mid-range.
  task_cpu    = 4096
  task_memory = 16384

  # Secret ARNs — populated incrementally as their owning issues land.
  # #2700 wires `github_token_secret_arn` (this ARN is the scoped PAT from
  # spike 0.7, provisioned in Secrets Manager by the operator and pinned
  # here so the execution role's `execution_secrets` policy resolves
  # against it).
  #
  # #2768 (Phase 2 shadow mode) wires `db_connection_secret_arn` — the
  # daemon must now reach Postgres to INSERT into `dispatcher.runs`
  # (lease / boot row) and `dispatcher.queue_snapshots` (per-tick queue
  # observations). Until the dedicated `judgemind_dispatcher` DB role
  # lands (sub-task B follow-up), we point at the main `judgemind`
  # role's secret — same DB, owner-level privileges. Safe for Phase 2
  # because `concurrency_cap=0` keeps the daemon from doing any DB
  # writes outside its own schema.
  #
  # #2834 wires `anthropic_api_key_secret_arn` — Phase 3 (cap=1) needs
  # the daemon's `claude -p` subprocess to authenticate, so
  # `ANTHROPIC_API_KEY` must be present in the container environment.
  # Reuses the shared `judgemind/anthropic/api-key` secret already used
  # by the compute module (above) via the `data` block at the top.
  #
  # Other ARNs stay empty until their owning sub-tasks land — the
  # module's `compact()` guard drops unset entries.
  anthropic_api_key_secret_arn  = data.aws_secretsmanager_secret.anthropic_api_key.arn
  db_connection_secret_arn      = module.database.db_connection_secret_arn
  github_token_secret_arn       = "arn:aws:secretsmanager:us-west-2:155326049300:secret:judgemind/dispatcher/github-token-QOmHlJ"
  telegram_bot_token_secret_arn = ""
  gemini_api_key_secret_arn     = ""

  github_repo = "judgemind/judgemind"

  # #3048 — grant the daemon's task role the IAM permissions needed by
  # `scripts/ecs-run-task.sh` so ralph phases can run data scripts
  # (rebuild_db, backfills, orphan cleanup) from the daemon context.
  # Scoped to the `judgemind-oneshot-dev` family on this cluster; PassRole
  # is pinned to the ingestion worker task + execution roles. See the
  # `task_run_oneshot` policy in modules/dispatcher-daemon/main.tf for
  # the full scope invariants. Keep these wired in dev only — production
  # data-script execution stays human-gated.
  oneshot_source_task_role_arn      = module.iam_scraper.role_arn
  oneshot_source_execution_role_arn = module.compute.task_execution_role_arn
  # The `judgemind-assets-dev` bucket is provisioned outside Terraform
  # (legacy). ARN pattern is deterministic — S3 bucket ARNs don't have a
  # region or account-id segment, so hard-coding is safe here.
  oneshot_script_bucket_arn          = "arn:aws:s3:::judgemind-assets-dev"
  oneshot_ecr_scraper_repository_arn = module.ecr.repository_arn
  # #3050 — grant the daemon's task role `s3:ListBucket` on the document-
  # archive bucket so data-task agents can run pre/post rebuild census
  # queries from the daemon context. The `iam_scraper` role already has
  # ListBucket for in-task use; this wiring is for the daemon container.
  document_archive_bucket_arn = module.document_archive.bucket_arn

  # Heartbeat alarm wired to the shared SNS topic once the daemon starts
  # emitting HeartbeatAge (Phase 2). Kept enabled in Phase 1 so the alarm
  # resource lands with the rest of the module; `treat_missing_data =
  # notBreaching` on the alarm keeps it from paging while desired_count=0.
  enable_alerts       = true
  alert_sns_topic_arn = module.compute.alerts_topic_arn

  # #3091 Stage 2 — per-agent ECS launcher wiring. Threads the
  # agent-runner module's task-def family + security group + role ARNs
  # through to the daemon's task role (for ecs:RunTask / DescribeTasks
  # / StopTask / iam:PassRole) and to the daemon's container env vars
  # (so _launch_agent_ecs_task knows what to run and where). Default
  # ``dispatcher.config.agent_execution_mode`` stays at ``'subprocess'``
  # — this wiring is inert until Stage 4 (#3093) flips the default.
  agent_runner_task_definition_family = module.dispatcher_agent_runner.task_definition_family
  agent_runner_execution_role_arn     = module.dispatcher_agent_runner.execution_role_arn
  agent_runner_task_role_arn          = module.dispatcher_agent_runner.task_role_arn
  agent_runner_subnet_ids             = module.networking.private_subnet_ids
  agent_runner_security_group_id      = module.dispatcher_agent_runner.security_group_id
  agent_runner_ecr_repository_arn     = module.ecr.dispatcher_agent_runner_repository_arn
}

# ─── Dispatcher agent-runner task def (Stage 1b, #3090) ─────────────────────
# One-shot ECS task definition run per-agent via `ecs:RunTask`. Stage 1b
# ships the task def + entrypoint; Stage 2 (#3091) adds the daemon-side
# launcher. Until Stage 2 lands, operators invoke this task manually via
# `aws ecs run-task --overrides` (see the PR body in #3090 for the exact
# command). No ECS service is provisioned — agent-runners are one-shot
# leaves, not long-running singletons.
module "dispatcher_agent_runner" {
  source = "../../modules/dispatcher-agent-runner"

  environment        = "dev"
  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids
  ecr_repository_url = module.ecr.dispatcher_agent_runner_repository_url

  # Sized to match the subprocess-daemon envelope (4 vCPU / 16 GiB) — the
  # envelope ralph was designed against. The Stage 1b baseline (512 / 1024)
  # pegged memory at 1022/1024 MB for hours under real ralph workload and
  # CPU-saturated at 509/512 in bursts (see #3153 evidence). Can shrink once
  # subprocess mode is retired and the dispatcher daemon scales down
  # correspondingly.
  task_cpu    = 4096
  task_memory = 16384

  # Secret wiring — same ARNs the daemon uses.
  anthropic_api_key_secret_arn = data.aws_secretsmanager_secret.anthropic_api_key.arn
  db_connection_secret_arn     = module.database.db_connection_secret_arn
  github_token_secret_arn      = "arn:aws:secretsmanager:us-west-2:155326049300:secret:judgemind/dispatcher/github-token-QOmHlJ"

  github_repo = "judgemind/judgemind"
  repo_url    = "https://github.com/judgemind/judgemind.git"

  # Error alarm wired to the shared SNS topic (same as the daemon and
  # scraper modules). Stage 4 (#3093) — fires on any ERROR/FATAL log
  # event emitted by an agent-runner task before the daemon reap pass
  # observes the STOPPED state.
  enable_alerts       = true
  alert_sns_topic_arn = module.compute.alerts_topic_arn

  # #3459 — widen the agent-runner task role with the SUBSET of the
  # daemon's `task_run_oneshot` policy that /spotcheck actually
  # exercises (ecs:RunTask + iam:PassRole on the oneshot family,
  # s3:ListBucket + s3:GetObject on the document-archive bucket). This
  # re-enables the /spotcheck scheduled skill, which migration 52 flips
  # back on at hourly cadence. Wired in dev only — production /spotcheck
  # remains gated until operator explicitly opts in.
  spotcheck_oneshot_source_task_role_arn      = module.iam_scraper.role_arn
  spotcheck_oneshot_source_execution_role_arn = module.compute.task_execution_role_arn
  spotcheck_ecs_cluster_arn                   = module.compute.cluster_arn
  spotcheck_document_archive_bucket_arn       = module.document_archive.bucket_arn
  spotcheck_oneshot_script_bucket_arn         = "arn:aws:s3:::judgemind-assets-dev"
}

# ─── Dispatcher v3 IAM (F3, #3888) ──────────────────────────────────────────
# Per `docs/specs/dispatcher-v3-spec.md` §10. Two task roles + one shared
# execution role for the v3 task pipeline:
#
#   * `launcher_role` — narrow scheduler scope assumed by F2's `launcher`
#     task definition.
#   * `agent_task_role` — dev-admin equivalent assumed by F2's
#     `task-runner`, `diagnoser`, and `scheduled-skill` task definitions.
#   * `execution_role` — shared by every v3 task definition for ECR pull
#     and secret injection.
#
# F2 (#3887) consumes these ARNs when registering task definitions; F4
# stands up the launcher ECS service. Until F2 lands, the roles exist
# but no task assumes them — they're inert by construction.
module "dispatcher_v3_iam" {
  source = "../../modules/dispatcher-v3-iam"

  environment     = "dev"
  ecs_cluster_arn = module.compute.cluster_arn

  # Telegram bot token — re-uses the same secret v2's daemon does. Wired
  # so the launcher can page the operator on a stuck queue without
  # reaching for the broad agent-task-role secret list.
  telegram_bot_token_secret_arn = ""

  # Scoped GitHub PAT — re-uses the same `judgemind/dispatcher/github-token`
  # secret as v2. F2 will inject this into the task-runner / diagnoser
  # task definitions for `gh auth setup-git` inside the agent.
  github_token_secret_arn = "arn:aws:secretsmanager:us-west-2:155326049300:secret:judgemind/dispatcher/github-token-QOmHlJ"
}

# ─── Dispatcher v3 sessions bucket (#3891) ──────────────────────────────────
# Per `docs/specs/dispatcher-v3-spec.md` §4.1 session capture. Stores
# per-agent session logs (raw stream-json jsonl + compact transcript)
# emitted by the v3 task-runner and diagnoser. Private bucket, IAM-only
# access from the v3 agent_task_role, lifecycle Standard for 30 days
# then Glacier-IR with a configurable expiration (default 365 days).
#
# The bucket policy grants only s3:PutObject and s3:GetObject to the
# agent_task_role; ListBucket is intentionally omitted (the diagnoser
# reads by exact key from the agents table, never enumerates).
module "dispatcher_v3_sessions" {
  source = "../../modules/dispatcher-v3-sessions-bucket"

  environment         = "dev"
  bucket_name         = "judgemind-dispatcher-v3-sessions"
  agent_task_role_arn = module.dispatcher_v3_iam.agent_task_role_arn

  # Default 365 days. Operators can override here if a future incident
  # review needs an extended audit window without changing the module.
  # session_retention_days = 365
}

# ─── Dispatcher v3 task-defs (F2, #3887) ────────────────────────────────────
# Per `docs/specs/dispatcher-v3-spec.md` §4 + §6. Four ECS Fargate task
# definitions baked from the F1 image (#3915, judgemind/dispatcher-v3)
# with different `command` argv selecting the per-task entrypoint:
#
#   * `<prefix>-launcher`        — long-running scheduler (1 vCPU / 2 GiB).
#     Wired into F4's ECS service (follow-up).
#   * `<prefix>-task-runner`     — one-shot per-agent (4 vCPU / 16 GiB,
#     stopTimeout 6h). Launched by the launcher via ecs:RunTask.
#   * `<prefix>-diagnoser`       — one-shot per-failure (2 vCPU / 8 GiB,
#     stopTimeout 1h).
#   * `<prefix>-scheduled-skill` — one-shot per-cron-trigger (2 vCPU /
#     8 GiB, stopTimeout 2h). Wired into F5's EventBridge schedules
#     (follow-up).
#
# Image is digest-pinned at apply time via `data "aws_ecr_image"` —
# every rendered task-def references @sha256:<digest> instead of the
# mutable :latest tag. This is the structural defense against the v2
# image-staleness drift documented in #3754. Re-runs of terraform apply
# after a fresh deploy-dispatcher-v3.yml push resolve a new digest and
# register fresh task-def revisions.
#
# Until F4 lands, the launcher task-def exists but no service runs;
# until F5 lands, the scheduled-skill task-def has no EventBridge
# triggers. The IAM-side launcher_role's RunTask grant (F3 #3921)
# already covers task-runner / diagnoser / scheduled-skill families,
# so no IAM updates are needed when those follow-ups land.
module "dispatcher_v3_task_defs" {
  source = "../../modules/dispatcher-v3-task-defs"

  environment     = "dev"
  ecs_cluster_arn = module.compute.cluster_arn

  # F1 (#3915) image — ECR repo provisioned by modules/ecr.
  ecr_repository_name = "judgemind/dispatcher-v3"
  ecr_repository_url  = module.ecr.dispatcher_v3_repository_url
  # Default `latest` resolves to the most recent <sha7> via
  # data.aws_ecr_image at apply time. Override if a specific build is
  # needed for a debug session.
  image_tag = "latest"

  # F3 (#3921) IAM role ARNs.
  launcher_role_arn   = module.dispatcher_v3_iam.launcher_role_arn
  agent_task_role_arn = module.dispatcher_v3_iam.agent_task_role_arn
  execution_role_arn  = module.dispatcher_v3_iam.execution_role_arn

  # Secrets — re-use the same ARNs the v2 daemon already has wired.
  anthropic_api_key_secret_arn = data.aws_secretsmanager_secret.anthropic_api_key.arn
  db_connection_secret_arn     = module.database.db_connection_secret_arn
  github_token_secret_arn      = "arn:aws:secretsmanager:us-west-2:155326049300:secret:judgemind/dispatcher/github-token-QOmHlJ"
  # Telegram secret stays empty until the launcher needs to page the
  # operator — the launcher task-def's secrets block omits the entry
  # entirely when this is empty.
  telegram_bot_token_secret_arn = ""

  github_repo = "judgemind/judgemind"
  repo_url    = "https://github.com/judgemind/judgemind.git"

  # Sessions bucket from #3891 — the task-runner entrypoint archives
  # session logs to s3://<bucket>/<agent_id>.jsonl on exit (spec §4.1).
  sessions_bucket_name = module.dispatcher_v3_sessions.bucket_id
}

# ─── Dispatcher v3 launcher ECS service (F4, #3889) ─────────────────────────
# Per `docs/specs/dispatcher-v3-spec.md` §6 + §9 step 2. Single-replica
# ECS Fargate service that runs the v3 launcher loop alongside v2's
# dispatcher daemon. Deploys at desired_count=1 with the launcher
# observing the queue but claiming nothing — `dispatcher.config.
# concurrency_cap_v3=0` (seeded by migration 58) keeps it idle until
# the operator manually flips the cap.
#
# References F2's `launcher_task_definition_arn` directly (revision-
# pinned). Subsequent F2 reapplies that resolve a fresh image digest
# register a new task-def revision, but the service's
# `lifecycle.ignore_changes = [task_definition]` block keeps Terraform
# from churning the service on every apply — operator-driven redeploys
# go through `aws ecs update-service --force-new-deployment` exactly
# like v2's dispatcher-daemon.
#
# Why a separate service vs piggy-backing on v2's dispatcher-daemon:
# spec §8.1 explicitly separates the two ECS services so cohabitation
# stays clean — independent log groups, independent breakers, independent
# concurrency caps. Wiring v3 onto the v2 service would couple the two
# rollout knobs (cap=0 means BOTH off) and lose the gradual ramp pattern
# in §9 step 4 (raise v3 cap by 2, decrease v2 cap by 2).
module "dispatcher_v3_service" {
  source = "../../modules/dispatcher-v3-service"

  environment        = "dev"
  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids
  ecs_cluster_arn    = module.compute.cluster_arn

  # F2 (#3887) launcher task-def. Revision-pinned -- Terraform reapplies
  # after a deploy-dispatcher-v3.yml push will resolve a fresh digest in
  # F2 and register a new revision; the service ignores those revisions
  # via lifecycle.ignore_changes = [task_definition] so the operator
  # controls rollout timing via `update-service --force-new-deployment`.
  task_definition_arn = module.dispatcher_v3_task_defs.launcher_task_definition_arn

  # Default desired_count=1 -- launcher is RUNNING but idle (cap=0
  # blocks claims). Operator can scale to 0 as a kill-switch
  # (rollback button per spec §9 step 4).
  # desired_count = 1
}

output "ecr_repository_url" {
  description = "Dev ECR repository URL for scraper images"
  value       = module.ecr.repository_url
}

output "dispatcher_agent_runner_task_definition_family" {
  description = "Dev dispatcher agent-runner task definition family (used with `aws ecs run-task --task-definition`)"
  value       = module.dispatcher_agent_runner.task_definition_family
}

output "dispatcher_agent_runner_log_group" {
  description = "Dev CloudWatch log group for dispatcher agent-runner container output"
  value       = module.dispatcher_agent_runner.log_group_name
}

output "dispatcher_agent_runner_task_role_arn" {
  description = "Dev dispatcher agent-runner task role ARN (narrow: DB + PAT secret read, log-group scoped log writes -- no RunTask, no ECS Exec)"
  value       = module.dispatcher_agent_runner.task_role_arn
}

output "dispatcher_agent_runner_security_group_id" {
  description = "Dev dispatcher agent-runner security group ID (outbound HTTPS + Postgres only)"
  value       = module.dispatcher_agent_runner.security_group_id
}

output "dispatcher_agent_runner_ecr_repository_url" {
  description = "Dev ECR repository URL for the dispatcher agent-runner image"
  value       = module.ecr.dispatcher_agent_runner_repository_url
}

output "vpc_id" {
  description = "Dev VPC ID"
  value       = module.networking.vpc_id
}

output "private_subnet_ids" {
  description = "Dev private subnet IDs (ECS tasks, RDS, ElastiCache)"
  value       = module.networking.private_subnet_ids
}

output "public_subnet_ids" {
  description = "Dev public subnet IDs (NAT gateway, future ALB)"
  value       = module.networking.public_subnet_ids
}

output "nat_gateway_public_ip" {
  description = "Dev NAT gateway public IP (whitelist on court websites if needed)"
  value       = module.networking.nat_gateway_public_ip
}

output "ses_domain_verification_token" {
  description = "Dev SES domain verification TXT record value"
  value       = module.ses.domain_verification_token
}

output "ses_configuration_set_name" {
  description = "Dev SES configuration set name (set as SES_CONFIGURATION_SET in API)"
  value       = module.ses.configuration_set_name
}

output "ses_notifications_topic_arn" {
  description = "Dev SNS topic ARN for SES bounce/complaint notifications"
  value       = module.ses.ses_notifications_topic_arn
}

output "ses_dkim_tokens" {
  description = "Dev DKIM CNAME tokens -- add each as <token>._domainkey.judgemind.org CNAME <token>.dkim.amazonses.com"
  value       = module.ses.dkim_tokens
}

output "document_archive_bucket" {
  description = "Dev document archive bucket name"
  value       = module.document_archive.bucket_id
}

output "document_archive_arn" {
  description = "Dev document archive bucket ARN"
  value       = module.document_archive.bucket_arn
}

output "scraper_role_arn" {
  description = "Dev scraper IAM role ARN"
  value       = module.iam_scraper.role_arn
}

output "agent_role_arn" {
  description = "Dev agent IAM role ARN -- copy into ~/.aws/config as role_arn for the judgemind-agent profile"
  value       = module.iam_agent.role_arn
}

output "scraper_instance_profile_arn" {
  description = "Dev scraper EC2 instance profile ARN"
  value       = module.iam_scraper.instance_profile_arn
}

output "ecs_cluster_name" {
  description = "Dev ECS cluster name"
  value       = module.compute.cluster_name
}

output "ecs_cluster_arn" {
  description = "Dev ECS cluster ARN"
  value       = module.compute.cluster_arn
}

output "scraper_task_definition_arn" {
  description = "Dev scraper Fargate task definition ARN"
  value       = module.compute.task_definition_arn
}

output "scraper_security_group_id" {
  description = "Dev scraper security group ID (outbound HTTPS only)"
  value       = module.compute.security_group_id
}

# ─── Scraper zero-record check (EventBridge, #2677) ──────────────────────────
# Replaces the GH Actions cron scraper-zero-record-check.yml with an
# EventBridge-scheduled ECS one-shot task. The Python runner
# scripts/check-scraper-zero-record-runner.py handles issue creation, dedup,
# update, auto-close, and Telegram notification entirely inside the container.
#
# GITHUB_TOKEN reuses the existing dispatcher PAT
# (judgemind/dispatcher/github-token) that already has issues:write per #2700.
# Telegram wiring is left empty until the judgemind/telegram/bot secret ARN is
# plumbed through (follow-up — telegram_secret_arn defaults to "" so the task
# still runs without it, falling back to the CLI path inside the container).
module "scraper_zero_record_check" {
  source = "../../modules/scraper-zero-record-check"

  environment        = "dev"
  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids
  ecs_cluster_arn    = module.compute.cluster_arn

  ecr_repository_url       = module.ecr.repository_url
  task_execution_role_arn  = module.compute.task_execution_role_arn
  db_connection_secret_arn = module.database.db_connection_secret_arn

  # Reuse the dispatcher PAT — already has issues:write per #2700.
  github_token_secret_arn = "arn:aws:secretsmanager:us-west-2:155326049300:secret:judgemind/dispatcher/github-token-QOmHlJ"

  # Telegram secret not yet plumbed in dev (follow-up).
  telegram_secret_arn = ""

  gh_repo            = "judgemind/judgemind"
  log_retention_days = 14
  schedule_enabled   = true
}

output "zero_record_check_log_group" {
  description = "Dev CloudWatch log group for zero-record check output"
  value       = module.scraper_zero_record_check.log_group_name
}

output "zero_record_check_schedule_arn" {
  description = "Dev EventBridge schedule ARN for the zero-record streak check"
  value       = module.scraper_zero_record_check.schedule_arn
}

output "scraper_log_group" {
  description = "Dev CloudWatch log group for scraper output"
  value       = module.compute.log_group_name
}

output "redis_endpoint" {
  description = "Dev Redis endpoint for the event bus"
  value       = module.cache.redis_endpoint
}

output "redis_port" {
  description = "Dev Redis port"
  value       = module.cache.redis_port
}

output "opensearch_endpoint" {
  description = "Dev OpenSearch domain endpoint"
  value       = module.search.domain_endpoint
}

output "opensearch_arn" {
  description = "Dev OpenSearch domain ARN"
  value       = module.search.domain_arn
}

output "opensearch_security_group_id" {
  description = "Dev OpenSearch security group ID"
  value       = module.search.security_group_id
}

output "opensearch_master_credentials_secret_arn" {
  description = "Dev Secrets Manager ARN for OpenSearch master user credentials"
  value       = module.search.master_credentials_secret_arn
}

output "db_endpoint" {
  description = "Dev RDS PostgreSQL endpoint"
  value       = module.database.db_endpoint
}

output "db_port" {
  description = "Dev RDS PostgreSQL port"
  value       = module.database.db_port
}

output "db_connection_secret_arn" {
  description = "Dev Secrets Manager ARN for the database connection string (DATABASE_URL)"
  value       = module.database.db_connection_secret_arn
}

output "ingestion_worker_service_name" {
  description = "Dev ingestion worker ECS service name"
  value       = module.compute.ingestion_worker_service_name
}

output "ingestion_worker_log_group" {
  description = "Dev CloudWatch log group for ingestion worker output"
  value       = module.compute.ingestion_worker_log_group
}

output "api_alb_dns_name" {
  description = "Dev API ALB DNS name (CNAME target for dev.api.judgemind.org)"
  value       = module.api_service.alb_dns_name
}

output "api_service_name" {
  description = "Dev API ECS service name"
  value       = module.api_service.service_name
}

output "api_log_group" {
  description = "Dev CloudWatch log group for API output"
  value       = module.api_service.log_group_name
}

output "api_acm_validation" {
  description = "Dev API ACM certificate DNS validation records -- create these in Cloudflare"
  value       = module.api_service.acm_domain_validation_options
}

output "api_ecr_repository_url" {
  description = "Dev ECR repository URL for API images"
  value       = module.ecr.api_repository_url
}

output "api_alb_arn_suffix" {
  description = "Dev API ALB ARN suffix (for CloudWatch metric queries)"
  value       = module.api_service.alb_arn_suffix
}

output "api_target_group_arn_suffix" {
  description = "Dev API target group ARN suffix (for CloudWatch metric queries)"
  value       = module.api_service.target_group_arn_suffix
}

output "dispatcher_daemon_service_name" {
  description = "Dev dispatcher daemon ECS service name (Phase 1: desired_count=0)"
  value       = module.dispatcher_daemon.service_name
}

output "dispatcher_daemon_log_group" {
  description = "Dev CloudWatch log group for dispatcher daemon output"
  value       = module.dispatcher_daemon.log_group_name
}

output "dispatcher_daemon_task_role_arn" {
  description = "Dev dispatcher daemon task role ARN (assumed by the container at runtime)"
  value       = module.dispatcher_daemon.task_role_arn
}

output "dispatcher_daemon_security_group_id" {
  description = "Dev dispatcher daemon security group ID"
  value       = module.dispatcher_daemon.security_group_id
}

output "dispatcher_ecr_repository_url" {
  description = "Dev ECR repository URL for dispatcher v2 daemon images"
  value       = module.ecr.dispatcher_repository_url
}

# ─── Dispatcher v3 IAM outputs (F3, #3888) ──────────────────────────────────
# Consumed by F2 (#3887) when wiring v3 task definitions and by F4 when
# standing up the launcher ECS service.

output "dispatcher_v3_launcher_role_arn" {
  description = "Dev dispatcher-v3 launcher task role ARN (narrow scheduler scope: RunTask on agent task-defs, RDS connect to dispatcher.* only, Telegram secret read, CloudWatch read on /judgemind/dispatcher-v3/*)"
  value       = module.dispatcher_v3_iam.launcher_role_arn
}

output "dispatcher_v3_agent_task_role_arn" {
  description = "Dev dispatcher-v3 agent task role ARN (dev-admin equivalent shared by task-runner / diagnoser / scheduled-skill -- see spec section 10). Trust policy excludes assuming any cross-account role."
  value       = module.dispatcher_v3_iam.agent_task_role_arn
}

output "dispatcher_v3_execution_role_arn" {
  description = "Dev dispatcher-v3 shared task-execution role ARN (used by every v3 task definition for ECR pull + secret injection + log writes)"
  value       = module.dispatcher_v3_iam.execution_role_arn
}

output "dispatcher_v3_sessions_bucket" {
  description = "Dev dispatcher-v3 sessions bucket name. Wired into F2 task definitions as the SESSIONS_BUCKET env var (per spec section 4.1)."
  value       = module.dispatcher_v3_sessions.bucket_id
}

output "dispatcher_v3_sessions_bucket_arn" {
  description = "Dev dispatcher-v3 sessions bucket ARN. Useful for `aws iam simulate-principal-policy` regression checks against the agent_task_role policies."
  value       = module.dispatcher_v3_sessions.bucket_arn
}

# ─── Dispatcher v3 task-defs outputs (F2, #3887) ────────────────────────────
# Consumed by F4 (launcher ECS service) and F5 (EventBridge schedules).

output "dispatcher_v3_launcher_task_definition_family" {
  description = "Dev dispatcher-v3 launcher task definition family (judgemind-dispatcher-v3-launcher). F4's ECS service references this family-only so revisions roll forward without an explicit task_definition update."
  value       = module.dispatcher_v3_task_defs.launcher_task_definition_family
}

output "dispatcher_v3_task_runner_task_definition_family" {
  description = "Dev dispatcher-v3 task-runner task definition family. The launcher passes this to ecs:RunTask when claiming an issue."
  value       = module.dispatcher_v3_task_defs.task_runner_task_definition_family
}

output "dispatcher_v3_diagnoser_task_definition_family" {
  description = "Dev dispatcher-v3 diagnoser task definition family. The launcher passes this to ecs:RunTask after a task-runner exits non-zero."
  value       = module.dispatcher_v3_task_defs.diagnoser_task_definition_family
}

output "dispatcher_v3_scheduled_skill_task_definition_family" {
  description = "Dev dispatcher-v3 scheduled-skill task definition family. EventBridge cron rules (F5) target this family with a per-skill SKILL_NAME env override."
  value       = module.dispatcher_v3_task_defs.scheduled_skill_task_definition_family
}

output "dispatcher_v3_image_digest" {
  description = "Resolved image digest baked into every dispatcher-v3 task-def revision (sha256:...)."
  value       = module.dispatcher_v3_task_defs.image_digest
}

output "dispatcher_v3_image_uri" {
  description = "Full digest-pinned image URI baked into every dispatcher-v3 task-def revision (<repo>@sha256:<digest>)."
  value       = module.dispatcher_v3_task_defs.image_uri
}

output "dispatcher_v3_launcher_log_group" {
  description = "Dev CloudWatch log group for the dispatcher-v3 launcher task (/judgemind/dispatcher-v3/launcher)."
  value       = module.dispatcher_v3_task_defs.launcher_log_group_name
}

output "dispatcher_v3_task_runner_log_group" {
  description = "Dev CloudWatch log group for the dispatcher-v3 task-runner task (/judgemind/dispatcher-v3/task-runner). Read by the launcher's silent-hang detector via DescribeLogStreams."
  value       = module.dispatcher_v3_task_defs.task_runner_log_group_name
}

output "dispatcher_v3_diagnoser_log_group" {
  description = "Dev CloudWatch log group for the dispatcher-v3 diagnoser task (/judgemind/dispatcher-v3/diagnoser)."
  value       = module.dispatcher_v3_task_defs.diagnoser_log_group_name
}

output "dispatcher_v3_scheduled_skill_log_group" {
  description = "Dev CloudWatch log group for the dispatcher-v3 scheduled-skill task (/judgemind/dispatcher-v3/scheduled-skill)."
  value       = module.dispatcher_v3_task_defs.scheduled_skill_log_group_name
}

# ─── Dispatcher v3 launcher service outputs (F4, #3889) ─────────────────────
# Consumed by operators to drive `aws ecs describe-services` /
# `update-service` flows and by the v3 deploy workflow when forcing a
# rollout after a fresh image push.

output "dispatcher_v3_service_name" {
  description = "Dev dispatcher-v3 launcher ECS service name (`judgemind-dispatcher-v3-dev`). Use with `aws ecs describe-services --cluster judgemind-dev --services <service_name>` to inspect runningCount / deployment status. Spec section 6."
  value       = module.dispatcher_v3_service.service_name
}

output "dispatcher_v3_service_arn" {
  description = "Dev dispatcher-v3 launcher ECS service ARN."
  value       = module.dispatcher_v3_service.service_arn
}

output "dispatcher_v3_service_security_group_id" {
  description = "Dev dispatcher-v3 launcher security group ID (outbound HTTPS + Postgres + Redis only). Reference from any module that needs to allow ingress from the launcher (e.g. RDS / Redis security groups)."
  value       = module.dispatcher_v3_service.security_group_id
}
