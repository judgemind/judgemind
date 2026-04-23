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

  # Dev: 1 vCPU, 2 GB RAM, daily schedule at 6 AM PT
  # Matches production to prevent OOM kills during the full 17-scraper run.
  # See #2349 — 0.5 vCPU / 1 GB was insufficient and caused the task to be
  # killed before completing all scrapers.
  task_cpu            = 1024
  task_memory         = 2048
  schedule_expression = "cron(0 6 * * ? *)"
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
  # `oneshot_maintenance_task_role_arn` stays empty — the
  # `iam_maintenance` module is declared but not instantiated in dev. If
  # a follow-up wires the module, plumb `module.iam_maintenance.role_arn`
  # through here so `scripts/ecs-run-task.sh --role judgemind-maintenance-dev`
  # also works from the daemon context.
  oneshot_maintenance_task_role_arn = ""
  # The `judgemind-assets-dev` bucket is provisioned outside Terraform
  # (legacy). ARN pattern is deterministic — S3 bucket ARNs don't have a
  # region or account-id segment, so hard-coding is safe here.
  oneshot_script_bucket_arn          = "arn:aws:s3:::judgemind-assets-dev"
  oneshot_ecr_scraper_repository_arn = module.ecr.repository_arn

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

  # Stage 1b baseline: 0.5 vCPU / 1 GiB. Bump (along with task_memory) if
  # Stage 3 smoke shows the ralph workload needs more headroom.
  task_cpu    = 512
  task_memory = 1024

  # Secret wiring — same ARNs the daemon uses.
  anthropic_api_key_secret_arn = data.aws_secretsmanager_secret.anthropic_api_key.arn
  db_connection_secret_arn     = module.database.db_connection_secret_arn
  github_token_secret_arn      = "arn:aws:secretsmanager:us-west-2:155326049300:secret:judgemind/dispatcher/github-token-QOmHlJ"

  github_repo = "judgemind/judgemind"
  repo_url    = "https://github.com/judgemind/judgemind.git"
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
  description = "Dev dispatcher agent-runner task role ARN (narrow: DB + PAT secret read, log-group scoped log writes — no RunTask, no ECS Exec)"
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
  description = "Dev DKIM CNAME tokens — add each as <token>._domainkey.judgemind.org CNAME <token>.dkim.amazonses.com"
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
  description = "Dev API ACM certificate DNS validation records — create these in Cloudflare"
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
