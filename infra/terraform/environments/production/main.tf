# Production environment infrastructure.
#
# Manages networking, storage, IAM, compute, and email for the production
# environment.
#
# Object lock is enabled in COMPLIANCE mode. Once this environment is applied,
# objects in the bucket cannot be deleted or overwritten for the retention
# period — not even by AWS Support. Verify this is intentional before applying.
#
# Retention period: 7 years (configurable via object_lock_retention_years).
# This matches standard legal records retention requirements.
#
# The compute schedule is disabled by default. Run the scraper manually at
# least once before enabling the EventBridge schedule in a follow-up PR.

module "networking" {
  source      = "../../modules/networking"
  environment = "production"
}

module "ecr" {
  source      = "../../modules/ecr"
  environment = "production"

  enable_pull_policy          = true
  ecs_task_execution_role_arn = module.compute.task_execution_role_arn
}

module "document_archive" {
  source = "../../modules/storage"

  bucket_name                 = "judgemind-document-archive-production"
  environment                 = "production"
  enable_object_lock          = true
  object_lock_retention_years = 7
}

module "iam_scraper" {
  source = "../../modules/iam_scraper"

  environment                 = "production"
  document_archive_bucket_arn = module.document_archive.bucket_arn
}

module "cache" {
  source = "../../modules/cache"

  environment        = "production"
  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids
  node_type          = "cache.t4g.micro"
  num_cache_nodes    = 1
}

module "compute" {
  source = "../../modules/compute"

  environment           = "production"
  vpc_id                = module.networking.vpc_id
  private_subnet_ids    = module.networking.private_subnet_ids
  ecr_repository_url    = module.ecr.repository_url
  scraper_task_role_arn = module.iam_scraper.role_arn
  redis_url             = "redis://${module.cache.redis_endpoint}:${module.cache.redis_port}"

  # Production: 1 vCPU, 2 GB RAM, daily schedule disabled until first manual test
  task_cpu            = 1024
  task_memory         = 2048
  schedule_expression = "cron(0 6 * * ? *)"
  schedule_timezone   = "America/Los_Angeles"
  schedule_enabled    = false
  log_retention_days  = 30
  enable_alerts       = true
}

module "search" {
  source = "../../modules/search"

  environment        = "production"
  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids

  # Production: single t3.medium.search node, 50 GiB EBS
  instance_type   = "t3.medium.search"
  instance_count  = 1
  ebs_volume_size = 50
}

module "ses" {
  source = "../../modules/ses"

  environment    = "production"
  sending_domain = "judgemind.org"
}

output "ecr_repository_url" {
  description = "Production ECR repository URL for scraper images"
  value       = module.ecr.repository_url
}

output "vpc_id" {
  description = "Production VPC ID"
  value       = module.networking.vpc_id
}

output "private_subnet_ids" {
  description = "Production private subnet IDs (ECS tasks, RDS, ElastiCache)"
  value       = module.networking.private_subnet_ids
}

output "public_subnet_ids" {
  description = "Production public subnet IDs (NAT gateway, future ALB)"
  value       = module.networking.public_subnet_ids
}

output "nat_gateway_public_ip" {
  description = "Production NAT gateway public IP (whitelist on court websites if needed)"
  value       = module.networking.nat_gateway_public_ip
}

output "ses_domain_verification_token" {
  description = "Production SES domain verification TXT record value"
  value       = module.ses.domain_verification_token
}

output "ses_configuration_set_name" {
  description = "Production SES configuration set name (set as SES_CONFIGURATION_SET in API)"
  value       = module.ses.configuration_set_name
}

output "ses_notifications_topic_arn" {
  description = "Production SNS topic ARN for SES bounce/complaint notifications"
  value       = module.ses.ses_notifications_topic_arn
}

output "ses_dkim_tokens" {
  description = "Production DKIM CNAME tokens — add each as <token>._domainkey.judgemind.org CNAME <token>.dkim.amazonses.com"
  value       = module.ses.dkim_tokens
}

output "document_archive_bucket" {
  description = "Production document archive bucket name"
  value       = module.document_archive.bucket_id
}

output "document_archive_arn" {
  description = "Production document archive bucket ARN"
  value       = module.document_archive.bucket_arn
}

output "scraper_role_arn" {
  description = "Production scraper IAM role ARN"
  value       = module.iam_scraper.role_arn
}

output "scraper_instance_profile_arn" {
  description = "Production scraper EC2 instance profile ARN"
  value       = module.iam_scraper.instance_profile_arn
}

output "ecs_cluster_name" {
  description = "Production ECS cluster name"
  value       = module.compute.cluster_name
}

output "ecs_cluster_arn" {
  description = "Production ECS cluster ARN"
  value       = module.compute.cluster_arn
}

output "scraper_task_definition_arn" {
  description = "Production scraper Fargate task definition ARN"
  value       = module.compute.task_definition_arn
}

output "scraper_security_group_id" {
  description = "Production scraper security group ID (outbound HTTPS only)"
  value       = module.compute.security_group_id
}

output "scraper_log_group" {
  description = "Production CloudWatch log group for scraper output"
  value       = module.compute.log_group_name
}

output "redis_endpoint" {
  description = "Production Redis endpoint for the event bus"
  value       = module.cache.redis_endpoint
}

output "redis_port" {
  description = "Production Redis port"
  value       = module.cache.redis_port
}

output "opensearch_endpoint" {
  description = "Production OpenSearch domain endpoint"
  value       = module.search.domain_endpoint
}

output "opensearch_arn" {
  description = "Production OpenSearch domain ARN"
  value       = module.search.domain_arn
}

output "opensearch_security_group_id" {
  description = "Production OpenSearch security group ID"
  value       = module.search.security_group_id
}

output "opensearch_master_credentials_secret_arn" {
  description = "Production Secrets Manager ARN for OpenSearch master user credentials"
  value       = module.search.master_credentials_secret_arn
}
