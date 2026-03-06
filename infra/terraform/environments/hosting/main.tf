# Vercel hosting for the judgemind web app.
#
# Dev deployment: dev.judgemind.org → judgemind-web-dev.vercel.app
# Production deployment: judgemind.org (future, once ALB + API are ready)

module "web_dev" {
  source = "../../modules/vercel-web"

  team_slug     = "judgemind2026-7926s-projects"
  environment   = "dev"
  custom_domain = "dev.judgemind.org"

  # API deployed to ECS Fargate behind ALB (Issue #182).
  graphql_url = "https://api.dev.judgemind.org/graphql"
}

output "web_dev_project_id" {
  description = "Dev Vercel project ID"
  value       = module.web_dev.project_id
}

output "web_dev_vercel_domain" {
  description = "Auto-assigned vercel.app domain for the dev project"
  value       = module.web_dev.vercel_domain
}
