# DNS records for judgemind.org.
#
# SES token values are retrieved from the dev and production environment
# Terraform outputs. Both environments share the same domain identity
# (judgemind.org), so the token values are identical.
#
# To retrieve current values:
#   terraform -chdir=../dev output ses_domain_verification_token
#   terraform -chdir=../dev output ses_dkim_tokens
#
# Web/API subdomain CNAMEs are left empty until hosting is provisioned:
#   dev_web_cname  — set when Vercel is configured (Issue #137)
#   prod_web_cname — set when production ALB is configured (future issue)

module "dns" {
  source = "../../modules/dns"

  # SES domain verification — shared across environments (same domain identity).
  ses_verification_token = "w1QjyCYfazpNy7TMeIk+MHj4njB1Qf1eo1D2xm4pLl4="
  ses_dkim_tokens = [
    "5qgh4kqag5yy4ofvqx6ujo56kdyrsacz",
    "bfbq7yuinjzrbta3y4z2fgxesifrpsnr",
    "y3ggvhi7wtvsiyok2nrzzeujmuva5hvc",
  ]

  # Hosting — set these when the corresponding services are deployed.
  dev_web_cname  = "" # Issue #137: Vercel CNAME for dev.judgemind.org
  dev_api_cname  = "" # Future: API hosting for api.dev.judgemind.org
  prod_web_cname = "" # Future: production web hosting for judgemind.org
  prod_api_cname = "" # Future: production API for api.judgemind.org
}

output "zone_id" {
  description = "Cloudflare zone ID for judgemind.org"
  value       = module.dns.zone_id
}
