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
  description = "OpenSearch domain - HTTPS from VPC only"
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
    Name = "judgemind-opensearch-${var.environment}"
  }
}

# ─── OpenSearch Domain ───────────────────────────────────────────────────────
# Single-node t3.small.search for dev; override instance_type and
# instance_count for production workloads.
#
# Note on apply_immediately semantics (see #2573, #2581):
# Unlike `aws_db_instance` (RDS) and `aws_elasticache_cluster`, the AWS
# provider's `aws_opensearch_domain` resource does NOT expose an
# `apply_immediately` argument — and it does not need one. OpenSearch
# applies user-initiated configuration changes (instance_type, instance_count,
# ebs_options, engine_version, advanced_security_options) via a blue/green
# deployment that starts as soon as the API call lands, not on a weekly
# maintenance window. The `software_update_options` and Auto-Tune
# `maintenance_schedule` blocks only govern AWS-initiated updates (minor
# software patches and Auto-Tune's own tuning actions); they do not defer
# terraform-managed changes. This means a dispatcher-driven `terraform apply`
# already lands OpenSearch changes immediately without any additional
# variable override, so the dev/production env blocks intentionally omit any
# apply_immediately-equivalent setting here.

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

  # Access policy is intentionally wide-open at the IAM layer
  # (Principal = "*").  This is NOT a permissive misconfiguration —
  # see #3771 for the chain of reasoning.  Short version: with
  # fine-grained access control + internal user database (basic auth via
  # Secrets Manager), HTTP requests using basic auth have no IAM
  # principal at the AWS layer.  AWS evaluates the access policy
  # against "anonymous" principal in that case.  If the policy is
  # narrower than `*` (e.g. specific role ARNs), every basic-auth
  # request is denied as "User: anonymous is not authorized" before
  # FGAC's internal user DB ever validates the username/password.
  # PR #3720 attempted to tighten this to enumerated role ARNs (#3704)
  # and immediately broke ingestion-worker startup with the exact
  # 403-anonymous symptom in #3771.  Reverted here; defence-in-depth
  # remains via the SG (VPC-only HTTPS), encrypt-at-rest,
  # node-to-node encryption, and FGAC's internal user database.
  # The principal_arns variable is preserved (and dev/prod still pass
  # specific ARNs to it) so that a future SigV4 migration of all
  # OpenSearch clients can re-tighten the policy without a TF
  # interface change.  Tracking issue for that migration: see the
  # follow-up filed against #3704 in this PR's body.
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
    Name = "judgemind-opensearch-${var.environment}"
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

# NOTE: This secret_version IS Terraform-managed — all values are derived from
# Terraform resources (random_password, aws_opensearch_domain). Do NOT add
# ignore_changes here; Terraform must keep the secret in sync with OpenSearch.
resource "aws_secretsmanager_secret_version" "opensearch_master" {
  secret_id = aws_secretsmanager_secret.opensearch_master.id
  secret_string = jsonencode({
    username = var.master_user_name
    password = random_password.opensearch_master.result
    endpoint = aws_opensearch_domain.main.endpoint
    url      = "https://${aws_opensearch_domain.main.endpoint}"
  })
}
