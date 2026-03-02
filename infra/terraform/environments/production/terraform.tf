terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket         = "judgemind-terraform-state"
    key            = "production/storage/terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "judgemind-terraform-locks"
    encrypt        = true
  }
}

provider "aws" {
  region = "us-west-2"

  default_tags {
    tags = {
      project     = "judgemind"
      environment = "production"
    }
  }
}
