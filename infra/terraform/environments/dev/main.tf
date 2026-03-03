# Dev environment infrastructure.
#
# Manages networking, storage, IAM, compute, and email for the dev environment.
# Redis (cache) module will be added here once implemented.
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

module "compute" {
  source = "../../modules/compute"

  environment           = "dev"
  vpc_id                = module.networking.vpc_id
  private_subnet_ids    = module.networking.private_subnet_ids
  ecr_repository_url    = module.ecr.repository_url
  scraper_task_role_arn = module.iam_scraper.role_arn

  # Dev: 0.5 vCPU, 1 GB RAM, daily schedule at 6 AM PT
  task_cpu            = 512
  task_memory         = 1024
  schedule_expression = "cron(0 6 * * ? *)"
  schedule_timezone   = "America/Los_Angeles"
  schedule_enabled    = true
  log_retention_days  = 14
  enable_alerts       = true
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
