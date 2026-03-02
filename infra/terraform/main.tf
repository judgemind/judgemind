terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Remote state — uncomment after creating the state bucket
  # backend "s3" {
  #   bucket         = "judgemind-terraform-state"
  #   key            = "terraform.tfstate"
  #   region         = "us-west-2"
  #   dynamodb_table = "judgemind-terraform-locks"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "judgemind"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-west-2"
}

variable "environment" {
  description = "Environment name (dev, staging, production)"
  type        = string
  default     = "dev"
}

# ─── Modules ───────────────────────────────────────────────

module "storage" {
  source      = "./modules/storage"
  environment = var.environment
}

# Uncomment as you're ready to deploy each component:

# module "database" {
#   source      = "./modules/database"
#   environment = var.environment
# }

# module "networking" {
#   source      = "./modules/networking"
#   environment = var.environment
# }

# module "compute" {
#   source      = "./modules/compute"
#   environment = var.environment
# }
