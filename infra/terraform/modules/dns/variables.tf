variable "zone_name" {
  description = "Cloudflare DNS zone name"
  type        = string
  default     = "judgemind.org"
}

variable "ses_verification_token" {
  description = "SES domain verification TXT record value (from ses module output)"
  type        = string
  default     = ""
}

variable "ses_dkim_tokens" {
  description = "SES DKIM CNAME tokens — list of 3 (from ses module output)"
  type        = list(string)
  default     = []
}

variable "dev_web_cname" {
  description = "CNAME target for dev.judgemind.org (e.g. Vercel project URL). Leave empty to omit the record."
  type        = string
  default     = ""
}

variable "dev_api_cname" {
  description = "CNAME target for dev.api.judgemind.org. Leave empty to omit the record."
  type        = string
  default     = ""
}

variable "prod_web_cname" {
  description = "CNAME target for judgemind.org apex (e.g. ALB DNS name). Leave empty to omit the record."
  type        = string
  default     = ""
}

variable "prod_api_cname" {
  description = "CNAME target for api.judgemind.org. Leave empty to omit the record."
  type        = string
  default     = ""
}

variable "acm_validation_records" {
  description = "ACM certificate DNS validation CNAME records. Each entry maps a record name to its value."
  type = list(object({
    name  = string
    value = string
  }))
  default = []
}
