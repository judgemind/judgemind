# Production environment — document archive S3 bucket.
#
# Object lock is enabled in COMPLIANCE mode. Once this environment is applied,
# objects in the bucket cannot be deleted or overwritten for the retention
# period — not even by AWS Support. Verify this is intentional before applying.
#
# Retention period: 7 years (configurable via object_lock_retention_years).
# This matches standard legal records retention requirements.

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
