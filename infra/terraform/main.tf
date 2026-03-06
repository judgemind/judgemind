terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.0"
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

# CLOUDFLARE_API_TOKEN env var is read automatically by this provider.
provider "cloudflare" {}

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

variable "sending_domain" {
  description = "Domain used to send transactional email (e.g. judgemind.com)"
  type        = string
  default     = "judgemind.org"
}

# ─── Modules ───────────────────────────────────────────────

module "networking" {
  source      = "./modules/networking"
  environment = var.environment
}

module "storage" {
  source      = "./modules/storage"
  bucket_name = "judgemind-document-archive-${var.environment}"
  environment = var.environment
}

module "database" {
  source      = "./modules/database"
  environment = var.environment
  vpc_id      = module.networking.vpc_id
  subnet_ids  = module.networking.private_subnet_ids
}

module "iam_scraper" {
  source                      = "./modules/iam_scraper"
  environment                 = var.environment
  document_archive_bucket_arn = module.storage.bucket_arn
}

module "ecr" {
  source      = "./modules/ecr"
  environment = var.environment
}

module "ses" {
  source         = "./modules/ses"
  environment    = var.environment
  sending_domain = var.sending_domain
}

module "search" {
  source = "./modules/search"

  environment        = var.environment
  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids
}

module "compute" {
  source = "./modules/compute"

  environment           = var.environment
  vpc_id                = module.networking.vpc_id
  private_subnet_ids    = module.networking.private_subnet_ids
  ecr_repository_url    = module.ecr.repository_url
  scraper_task_role_arn = module.iam_scraper.role_arn
}

module "dns" {
  source = "./modules/dns"
  # All variables have defaults; real values are set in environments/dns/main.tf.
}
