# ElastiCache Redis module for the event bus.
#
# Provisions a single-node Redis cluster in private subnets for use as a
# Redis Streams event bus (Architecture Spec §2.1). The security group
# restricts access to internal VPC traffic only.

data "aws_vpc" "selected" {
  id = var.vpc_id
}

# ─── Subnet Group ─────────────────────────────────────────────────────────

resource "aws_elasticache_subnet_group" "main" {
  name        = "judgemind-${var.environment}"
  description = "Judgemind ElastiCache subnet group (${var.environment})"
  subnet_ids  = var.private_subnet_ids
}

# ─── Security Group ───────────────────────────────────────────────────────
# Only allows Redis traffic from within the VPC.

resource "aws_security_group" "redis" {
  name        = "judgemind-redis-${var.environment}"
  description = "Allow Redis access from within the VPC"
  vpc_id      = var.vpc_id

  ingress {
    description = "Redis from VPC"
    from_port   = 6379
    to_port     = 6379
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
    project     = "judgemind"
    environment = var.environment
  }
}

# ─── ElastiCache Redis Cluster ─────────────────────────────────────────────

resource "aws_elasticache_cluster" "redis" {
  cluster_id           = "judgemind-${var.environment}"
  engine               = "redis"
  engine_version       = var.engine_version
  node_type            = var.node_type
  num_cache_nodes      = var.num_cache_nodes
  parameter_group_name = "default.redis7"
  port                 = 6379

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.redis.id]

  tags = {
    Name        = "judgemind-${var.environment}"
    project     = "judgemind"
    environment = var.environment
  }
}
