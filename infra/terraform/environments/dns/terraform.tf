# DNS environment — manages all Cloudflare DNS records for judgemind.org.
#
# This is a standalone Terraform workspace, separate from the dev and
# production environments. DNS is domain-scoped, not environment-scoped.
#
# Before running terraform plan/apply, export the Cloudflare API token:
#
#   export CLOUDFLARE_API_TOKEN=$(aws secretsmanager get-secret-value \
#     --secret-id judgemind/cloudflare/api-token \
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
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.0"
    }
  }

  backend "s3" {
    bucket         = "judgemind-terraform-state"
    key            = "dns/terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "judgemind-terraform-locks"
    encrypt        = true
  }
}

# CLOUDFLARE_API_TOKEN env var is read automatically by this provider.
# Do not configure it here — the token lives in AWS Secrets Manager only.
provider "cloudflare" {}
