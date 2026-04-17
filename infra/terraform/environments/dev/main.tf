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

output "ecr_repository_url" {
  description = "Dev ECR repository URL for scraper images"
  value       = module.ecr.repository_url
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
