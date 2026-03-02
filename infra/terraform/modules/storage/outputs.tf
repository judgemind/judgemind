output "bucket_id" {
  description = "Name/ID of the document archive bucket"
  value       = aws_s3_bucket.document_archive.id
}

output "bucket_arn" {
  description = "ARN of the document archive bucket"
  value       = aws_s3_bucket.document_archive.arn
}

output "bucket_regional_domain_name" {
  description = "Regional domain name for the bucket (use for pre-signed URLs and internal access)"
  value       = aws_s3_bucket.document_archive.bucket_regional_domain_name
}
