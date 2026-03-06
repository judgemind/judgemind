# Cloudflare DNS module — manages all DNS records for judgemind.org.
#
# SES records (verification TXT, DKIM CNAMEs, SPF, MAIL FROM MX) are wired
# to the values produced by the SES Terraform module in each environment.
# Both dev and production share the same domain identity, so the token values
# are identical across environments.
#
# Web/API subdomain records are optional: set the corresponding CNAME
# variables once hosting is provisioned.
#
# Prerequisites:
#   export CLOUDFLARE_API_TOKEN=$(aws secretsmanager get-secret-value \
#     --secret-id judgemind/cloudflare/api-token \
#     --query SecretString --output text | python3 -c \
#     "import sys,json; print(json.load(sys.stdin)['token'])")
#
# Note: if Cloudflare already has records that conflict (e.g. an existing SPF
# TXT or MX), import them first:
#   terraform import cloudflare_record.spf_root <zone_id>/<record_id>

terraform {
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.0"
    }
  }
}

data "cloudflare_zone" "judgemind" {
  name = var.zone_name
}

locals {
  zone_id = data.cloudflare_zone.judgemind.id
}

# ─── SES Domain Verification ─────────────────────────────────────────────────

resource "cloudflare_record" "ses_verification" {
  count   = var.ses_verification_token != "" ? 1 : 0
  zone_id = local.zone_id
  name    = "_amazonses"
  type    = "TXT"
  content = var.ses_verification_token
  ttl     = 300
}

# ─── SES DKIM (3 CNAME records) ──────────────────────────────────────────────

resource "cloudflare_record" "ses_dkim" {
  count   = length(var.ses_dkim_tokens)
  zone_id = local.zone_id
  name    = "${var.ses_dkim_tokens[count.index]}._domainkey"
  type    = "CNAME"
  content = "${var.ses_dkim_tokens[count.index]}.dkim.amazonses.com"
  ttl     = 300
}

# ─── SPF ─────────────────────────────────────────────────────────────────────

# SPF for the root domain. If an SPF record already exists, import it first.
resource "cloudflare_record" "spf_root" {
  zone_id = local.zone_id
  name    = "@"
  type    = "TXT"
  content = "v=spf1 include:amazonses.com ~all"
  ttl     = 300
}

# SPF for the custom MAIL FROM subdomain.
resource "cloudflare_record" "spf_mail" {
  zone_id = local.zone_id
  name    = "mail"
  type    = "TXT"
  content = "v=spf1 include:amazonses.com ~all"
  ttl     = 300
}

# ─── MAIL FROM MX ────────────────────────────────────────────────────────────

resource "cloudflare_record" "mail_from_mx" {
  zone_id  = local.zone_id
  name     = "mail"
  type     = "MX"
  content  = "feedback-smtp.us-west-2.amazonses.com"
  priority = 10
  ttl      = 300
}

# ─── www → apex redirect ──────────────────────────────────────────────────────

# Proxied A record so Cloudflare intercepts www traffic.
# The redirect ruleset below issues the actual 301.
resource "cloudflare_record" "www" {
  zone_id = local.zone_id
  name    = "www"
  type    = "A"
  content = "192.0.2.1" # Documentation IP; Cloudflare proxy intercepts before routing
  proxied = true
  ttl     = 1 # Automatic when proxied
}

resource "cloudflare_ruleset" "redirect_www" {
  zone_id     = local.zone_id
  name        = "Redirect www to apex"
  description = "301 redirect www.judgemind.org → judgemind.org"
  kind        = "zone"
  phase       = "http_request_redirect"

  rules {
    action = "redirect"
    action_parameters {
      from_value {
        status_code = 301
        target_url {
          expression = "concat(\"https://judgemind.org\", http.request.uri.path)"
        }
        preserve_query_string = true
      }
    }
    expression  = "(http.host eq \"www.judgemind.org\")"
    description = "Redirect www to apex"
    enabled     = true
  }
}

# ─── Dev web subdomain ────────────────────────────────────────────────────────

# Set dev_web_cname when Vercel is configured (Issue #137).
resource "cloudflare_record" "dev_web" {
  count   = var.dev_web_cname != "" ? 1 : 0
  zone_id = local.zone_id
  name    = "dev"
  type    = "CNAME"
  content = var.dev_web_cname
  proxied = true
  ttl     = 1
}

# ─── Dev API subdomain ────────────────────────────────────────────────────────

resource "cloudflare_record" "dev_api" {
  count   = var.dev_api_cname != "" ? 1 : 0
  zone_id = local.zone_id
  name    = "api.dev"
  type    = "CNAME"
  content = var.dev_api_cname
  proxied = true
  ttl     = 1
}

# ─── Production web apex ──────────────────────────────────────────────────────

# Cloudflare supports CNAME flattening at the apex, so a CNAME here works
# even though RFC 1034 doesn't allow CNAMEs at the zone apex.
resource "cloudflare_record" "prod_web" {
  count   = var.prod_web_cname != "" ? 1 : 0
  zone_id = local.zone_id
  name    = "@"
  type    = "CNAME"
  content = var.prod_web_cname
  proxied = true
  ttl     = 1
}

# ─── Production API subdomain ─────────────────────────────────────────────────

resource "cloudflare_record" "prod_api" {
  count   = var.prod_api_cname != "" ? 1 : 0
  zone_id = local.zone_id
  name    = "api"
  type    = "CNAME"
  content = var.prod_api_cname
  proxied = true
  ttl     = 1
}
