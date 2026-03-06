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

  environment    = "dev"
  vpc_id         = module.networking.vpc_id
  subnet_ids     = module.networking.private_subnet_ids
  instance_class = "db.t4g.micro"
}

module "cache" {
  source = "../../modules/cache"

  environment        = "dev"
  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids
  node_type          = "cache.t4g.micro"
  num_cache_nodes    = 1
}

module "compute" {
  source = "../../modules/compute"

  environment              = "dev"
  vpc_id                   = module.networking.vpc_id
  private_subnet_ids       = module.networking.private_subnet_ids
  ecr_repository_url       = module.ecr.repository_url
  scraper_task_role_arn    = module.iam_scraper.role_arn
  redis_url                = "redis://${module.cache.redis_endpoint}:${module.cache.redis_port}"
  document_archive_bucket  = module.document_archive.bucket_id
  db_connection_secret_arn = module.database.db_connection_secret_arn
  opensearch_url           = "https://${module.search.domain_endpoint}"

  # Dev: 0.5 vCPU, 1 GB RAM, daily schedule at 6 AM PT
  task_cpu            = 512
  task_memory         = 1024
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
