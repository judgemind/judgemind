# Negative-case fixture for dispatcher-daemon precondition checks.
# Running `terraform plan` here must fail with a precondition error because
# desired_count = 1 but both secret ARNs are empty.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region                      = "us-west-2"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  access_key                  = "test"
  secret_key                  = "test"
}

module "dispatcher_daemon" {
  source = "../../"

  environment        = "dev"
  vpc_id             = "vpc-00000000000000000"
  private_subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000002"]
  ecs_cluster_arn    = "arn:aws:ecs:us-west-2:123456789012:cluster/judgemind-dev"
  ecr_repository_url = "123456789012.dkr.ecr.us-west-2.amazonaws.com/judgemind-dispatcher"

  # These are intentionally empty to trigger the precondition.
  desired_count                = 1
  anthropic_api_key_secret_arn = ""
  db_connection_secret_arn     = ""
}
