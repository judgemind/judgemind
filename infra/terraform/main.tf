terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }

  backend "s3" {
    bucket         = "judgemind-terraform-state"
    key            = "terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "judgemind-terraform-locks"
    encrypt        = true
  }
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

# ─── Data Sources ──────────────────────────────────────────

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# ─── Modules ───────────────────────────────────────────────

module "storage" {
  source      = "./modules/storage"
  bucket_name = "judgemind-document-archive-${var.environment}"
  environment = var.environment
}

module "database" {
  source      = "./modules/database"
  environment = var.environment
  vpc_id      = data.aws_vpc.default.id
  subnet_ids  = data.aws_subnets.default.ids
}

module "iam_scraper" {
  source                      = "./modules/iam_scraper"
  environment                 = var.environment
  document_archive_bucket_arn = module.storage.bucket_arn
}

# module "networking" {
#   source      = "./modules/networking"
#   environment = var.environment
# }

# module "compute" {
#   source      = "./modules/compute"
#   environment = var.environment
# }
