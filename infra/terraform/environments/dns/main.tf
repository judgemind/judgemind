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
# Web/API subdomain CNAMEs — set as services are provisioned:
#   dev_web_cname  — Vercel (Issue #137)
#   dev_api_cname  — API ALB (Issue #183)
#   prod_web_cname — set when production web hosting is configured (future issue)

module "dns" {
  source = "../../modules/dns"

  # SES domain verification — shared across environments (same domain identity).
  ses_verification_token = "w1QjyCYfazpNy7TMeIk+MHj4njB1Qf1eo1D2xm4pLl4="
  ses_dkim_tokens = [
    "5qgh4kqag5yy4ofvqx6ujo56kdyrsacz",
    "bfbq7yuinjzrbta3y4z2fgxesifrpsnr",
    "y3ggvhi7wtvsiyok2nrzzeujmuva5hvc",
  ]

  # Hosting — updated as services are deployed.
  dev_web_cname  = "cname.vercel-dns.com"                                     # → Vercel (Issue #137)
  dev_api_cname  = "judgemind-api-dev-1789849795.us-west-2.elb.amazonaws.com" # → ALB (Issue #182)
  prod_web_cname = ""                                                         # Future: production web hosting for judgemind.org
  prod_api_cname = ""                                                         # Future: production API for api.judgemind.org

  # ACM certificate DNS validation records for HTTPS on the API ALB.
  # Values from: terraform -chdir=../dev output api_acm_validation
  acm_validation_records = [
    {
      name  = "_27b9101c0edcca5108f98b3942e7866d.dev.api.judgemind.org."
      value = "_25b58ba53f33f4812c398b9e17894593.jkddzztszm.acm-validations.aws."
    },
  ]
}

output "zone_id" {
  description = "Cloudflare zone ID for judgemind.org"
  value       = module.dns.zone_id
}
