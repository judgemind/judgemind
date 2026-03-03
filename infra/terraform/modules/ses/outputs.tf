output "domain_identity_arn" {
  description = "ARN of the SES domain identity"
  value       = aws_ses_domain_identity.this.arn
}

output "domain_verification_token" {
  description = "TXT record value for SES domain verification. Add as: _amazonses.<domain> TXT <value>"
  value       = aws_ses_domain_identity.this.verification_token
}

output "dkim_tokens" {
  description = "DKIM CNAME tokens (3 values). For each token, add: <token>._domainkey.<domain> CNAME <token>.dkim.amazonses.com"
  value       = aws_ses_domain_dkim.this.dkim_tokens
}

output "configuration_set_name" {
  description = "SES configuration set name. Set as SES_CONFIGURATION_SET in the API environment."
  value       = aws_ses_configuration_set.this.name
}

output "ses_notifications_topic_arn" {
  description = "SNS topic ARN for SES bounce and complaint notifications"
  value       = aws_sns_topic.ses_notifications.arn
}
