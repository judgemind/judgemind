output "bucket_id" {
  description = "Name/ID of the dispatcher-v3 sessions bucket. Wired into F2 task definitions as the SESSIONS_BUCKET env var."
  value       = aws_s3_bucket.sessions.id
}

output "bucket_arn" {
  description = "ARN of the dispatcher-v3 sessions bucket. Useful for `aws iam simulate-principal-policy` regression checks against the agent_task_role policies."
  value       = aws_s3_bucket.sessions.arn
}

output "bucket_regional_domain_name" {
  description = "Regional domain name of the sessions bucket. Use for direct S3 endpoint construction inside the dev VPC if a future component needs path-style addressing."
  value       = aws_s3_bucket.sessions.bucket_regional_domain_name
}
