# Dev environment — document archive S3 bucket.
#
# The dev bucket (judgemind-document-archive-dev) was initially created manually.
# To bring it under Terraform management, import it once:
#
#   terraform import module.document_archive.aws_s3_bucket.document_archive \
#     judgemind-document-archive-dev
#
# Object lock is intentionally disabled for dev so test objects can be deleted.

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
