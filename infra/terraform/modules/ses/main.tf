# Amazon SES — transactional email for account verification, password reset,
# docket alerts, and daily digest.
#
# DNS setup (manual or via Cloudflare Terraform — no Cloudflare provider in
# this repo yet):
#
#   1. Domain verification (TXT):
#        _amazonses.<sending_domain>  TXT  <domain_verification_token output>
#
#   2. DKIM (CNAME × 3 — values in dkim_tokens output):
#        <token>._domainkey.<sending_domain>  CNAME  <token>.dkim.amazonses.com
#
#   3. SPF (TXT — add to existing SPF or create new):
#        <sending_domain>  TXT  "v=spf1 include:amazonses.com ~all"
#
#   4. MAIL FROM MX record:
#        mail.<sending_domain>  MX  10  feedback-smtp.us-west-2.amazonses.com
#
#   5. MAIL FROM SPF (TXT):
#        mail.<sending_domain>  TXT  "v=spf1 include:amazonses.com ~all"
#
# SES sandbox: new accounts start in sandbox (send only to verified addresses).
# To exit sandbox, submit a production access request via the AWS console:
#   SES → Account dashboard → Request production access
# This requires human action and cannot be automated.

# ─── Domain Identity ────────────────────────────────────────────────────────

resource "aws_ses_domain_identity" "this" {
  domain = var.sending_domain
}

# ─── DKIM ───────────────────────────────────────────────────────────────────

# SES generates 3 CNAME records. After applying, retrieve the tokens from the
# dkim_tokens output and add them to DNS. Verification can take up to 72 hours.
resource "aws_ses_domain_dkim" "this" {
  domain = aws_ses_domain_identity.this.domain
}

# ─── MAIL FROM Domain ───────────────────────────────────────────────────────

# Using a custom MAIL FROM subdomain improves deliverability and avoids the
# default "via amazonses.com" annotation shown by some email clients.
resource "aws_ses_domain_mail_from" "this" {
  domain           = aws_ses_domain_identity.this.domain
  mail_from_domain = "mail.${var.sending_domain}"

  # Reject sends if the MAIL FROM domain's MX record is not configured.
  behavior_on_mx_failure = "RejectMessage"
}

# ─── Configuration Set ──────────────────────────────────────────────────────

# Configuration sets enable per-send reputation tracking and event publishing.
# All sends from the API must reference this configuration set via the
# SES_CONFIGURATION_SET environment variable.
resource "aws_ses_configuration_set" "this" {
  name = "judgemind-${var.environment}"
}

# ─── SNS Topic — Bounce & Complaint Notifications ───────────────────────────

# SES publishes bounce and complaint events to this topic. The API must
# subscribe an HTTPS endpoint to process events and maintain a suppression
# list (skip future sends to bounced/complained addresses).
#
# Wiring up the subscription:
#   1. Deploy the API with a POST /email/notifications endpoint.
#   2. Subscribe: aws sns subscribe --topic-arn <arn> --protocol https
#        --notification-endpoint https://api.judgemind.com/email/notifications
#   3. Confirm the subscription by responding to the SubscriptionConfirmation
#      message that SNS sends to the endpoint.
resource "aws_sns_topic" "ses_notifications" {
  name = "judgemind-ses-notifications-${var.environment}"
}

# Route bounces to the SNS topic.
resource "aws_ses_identity_notification_topic" "bounce" {
  topic_arn         = aws_sns_topic.ses_notifications.arn
  notification_type = "Bounce"
  identity          = aws_ses_domain_identity.this.domain
}

# Route complaints to the SNS topic.
resource "aws_ses_identity_notification_topic" "complaint" {
  topic_arn         = aws_sns_topic.ses_notifications.arn
  notification_type = "Complaint"
  identity          = aws_ses_domain_identity.this.domain
}
