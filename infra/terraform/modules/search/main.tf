# OpenSearch module for full-text search.
#
# Provisions an OpenSearch domain in private subnets with VPC-only access.
# Used for indexing tentative rulings and other court documents for full-text
# search. The domain is placed behind a security group that only allows HTTPS
# ingress from within the VPC.
#
# The OpenSearch service-linked role must already exist in the AWS account.
# If not, create it manually or via a one-time `aws iam create-service-linked-role
# --aws-service-name es.amazonaws.com` before applying this module.

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

data "aws_vpc" "selected" {
  id = var.vpc_id
}

# ─── Security Group ──────────────────────────────────────────────────────────
# Allows HTTPS (port 443) from within the VPC only. OpenSearch exposes its
# REST API over HTTPS on port 443.

resource "aws_security_group" "opensearch" {
  name        = "judgemind-opensearch-${var.environment}"
  description = "OpenSearch domain — HTTPS from VPC only"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTPS from VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.selected.cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name        = "judgemind-opensearch-${var.environment}"
    project     = "judgemind"
    environment = var.environment
  }
}

# ─── OpenSearch Domain ───────────────────────────────────────────────────────
# Single-node t3.small.search for dev; override instance_type and
# instance_count for production workloads.

resource "aws_opensearch_domain" "main" {
  domain_name    = "judgemind-${var.environment}"
  engine_version = var.engine_version

  cluster_config {
    instance_type  = var.instance_type
    instance_count = var.instance_count
  }

  ebs_options {
    ebs_enabled = true
    volume_type = "gp3"
    volume_size = var.ebs_volume_size
  }

  vpc_options {
    subnet_ids         = [var.private_subnet_ids[0]]
    security_group_ids = [aws_security_group.opensearch.id]
  }

  encrypt_at_rest {
    enabled = true
  }

  node_to_node_encryption {
    enabled = true
  }

  domain_endpoint_options {
    enforce_https       = true
    tls_security_policy = "Policy-Min-TLS-1-2-2019-07"
  }

  advanced_security_options {
    enabled                        = true
    internal_user_database_enabled = true

    master_user_options {
      master_user_name     = var.master_user_name
      master_user_password = random_password.opensearch_master.result
    }
  }

  access_policies = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { AWS = "*" }
        Action    = "es:*"
        Resource  = "arn:aws:es:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:domain/judgemind-${var.environment}/*"
      }
    ]
  })

  tags = {
    Name        = "judgemind-opensearch-${var.environment}"
    project     = "judgemind"
    environment = var.environment
  }
}

# ─── Master User Password ───────────────────────────────────────────────────

resource "random_password" "opensearch_master" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "aws_secretsmanager_secret" "opensearch_master" {
  name                    = "judgemind/${var.environment}/opensearch/master"
  description             = "OpenSearch master user credentials for Judgemind (${var.environment})"
  recovery_window_in_days = var.environment == "production" ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "opensearch_master" {
  secret_id = aws_secretsmanager_secret.opensearch_master.id
  secret_string = jsonencode({
    username = var.master_user_name
    password = random_password.opensearch_master.result
    endpoint = aws_opensearch_domain.main.endpoint
    url      = "https://${aws_opensearch_domain.main.endpoint}"
  })
}
