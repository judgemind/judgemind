# Vercel web module — manages the Next.js web app project on Vercel.
#
# Prerequisites:
#   1. The Vercel GitHub App must be installed on the judgemind GitHub org:
#      https://vercel.com/<team>/settings/integrations
#   2. Export the API token before running terraform:
#      export VERCEL_API_TOKEN=$(aws secretsmanager get-secret-value \
#        --secret-id judgemind/vercel/api-token \
#        --query SecretString --output text | \
#        python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

terraform {
  required_providers {
    vercel = {
      source  = "vercel/vercel"
      version = "~> 1.0"
    }
  }
}

resource "vercel_project" "web" {
  name      = "judgemind-web-${var.environment}"
  framework = "nextjs"

  # Monorepo: Next.js app lives in packages/web/
  root_directory = "packages/web"

  git_repository = {
    type = "github"
    repo = var.github_repo
  }

  environment = [
    {
      key    = "NEXT_PUBLIC_GRAPHQL_URL"
      value  = var.graphql_url
      target = ["production", "preview"]
    },
  ]
}

resource "vercel_project_domain" "custom" {
  project_id = vercel_project.web.id
  domain     = var.custom_domain
}
