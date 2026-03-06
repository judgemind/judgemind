# Hosting environment — manages Vercel projects for web app deployments.
#
# This is a standalone Terraform workspace, separate from dns/ and the
# AWS environments. Hosting config is provider-scoped, not AWS-scoped.
#
# Before running terraform plan/apply, export the Vercel API token:
#
#   export VERCEL_API_TOKEN=$(aws secretsmanager get-secret-value \
#     --secret-id judgemind/vercel/api-token \
#     --query SecretString --output text | \
#     python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
#
# Then:
#   terraform init
#   terraform plan
#   terraform apply

terraform {
  required_version = ">= 1.7"

  required_providers {
    vercel = {
      source  = "vercel/vercel"
      version = "~> 1.0"
    }
  }

  backend "s3" {
    bucket         = "judgemind-terraform-state"
    key            = "hosting/terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "judgemind-terraform-locks"
    encrypt        = true
  }
}

# VERCEL_API_TOKEN env var is read automatically by this provider.
provider "vercel" {
  team = "judgemind2026-7926s-projects"
}
