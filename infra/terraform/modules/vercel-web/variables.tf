variable "team_slug" {
  description = "Vercel team or personal account slug (from the URL: vercel.com/<slug>)"
  type        = string
}

variable "environment" {
  description = "Deployment environment (dev, production)"
  type        = string
}

variable "github_repo" {
  description = "GitHub repo in org/repo format"
  type        = string
  default     = "judgemind/judgemind"
}

variable "custom_domain" {
  description = "Custom domain to attach to the Vercel project (e.g. dev.judgemind.org)"
  type        = string
}

variable "graphql_url" {
  description = "Value for NEXT_PUBLIC_GRAPHQL_URL (e.g. https://api.dev.judgemind.org/graphql)"
  type        = string
}
