variable "environment" {
  description = "Environment name (dev, staging, production)"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID where the database will be deployed"
  type        = string
}

variable "subnet_ids" {
  description = "List of subnet IDs for the DB subnet group"
  type        = list(string)
}

variable "instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t4g.medium"
}

variable "db_name" {
  description = "Name of the default database"
  type        = string
  default     = "judgemind"
}

variable "db_username" {
  description = "Master username"
  type        = string
  default     = "judgemind"
}

# ─── Password ──────────────────────────────────────────────

resource "random_password" "db" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

# ─── Subnet group ──────────────────────────────────────────

resource "aws_db_subnet_group" "main" {
  name        = "judgemind-${var.environment}"
  subnet_ids  = var.subnet_ids
  description = "Judgemind RDS subnet group (${var.environment})"
}

# ─── Security group ────────────────────────────────────────

data "aws_vpc" "selected" {
  id = var.vpc_id
}

resource "aws_security_group" "db" {
  name        = "judgemind-db-${var.environment}"
  description = "Allow PostgreSQL from within the VPC"
  vpc_id      = var.vpc_id

  ingress {
    description = "PostgreSQL from VPC"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.selected.cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ─── RDS PostgreSQL ────────────────────────────────────────

resource "aws_db_instance" "main" {
  identifier        = "judgemind-${var.environment}"
  engine            = "postgres"
  engine_version    = "16"
  instance_class    = var.instance_class
  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false

  backup_retention_period    = 30
  backup_window              = "03:00-04:00"
  maintenance_window         = "sun:05:00-sun:06:00"
  auto_minor_version_upgrade = true

  multi_az                  = var.environment == "production" ? true : false
  deletion_protection       = var.environment == "production" ? true : false
  skip_final_snapshot       = var.environment == "production" ? false : true
  final_snapshot_identifier = var.environment == "production" ? "judgemind-${var.environment}-final" : null

  performance_insights_enabled = true

  tags = {
    Name = "judgemind-${var.environment}"
  }
}

# ─── Secrets Manager ───────────────────────────────────────

resource "aws_secretsmanager_secret" "db_connection" {
  name                    = "judgemind/${var.environment}/db/connection"
  description             = "PostgreSQL connection string for Judgemind (${var.environment})"
  recovery_window_in_days = var.environment == "production" ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "db_connection" {
  secret_id = aws_secretsmanager_secret.db_connection.id
  secret_string = jsonencode({
    host     = aws_db_instance.main.address
    port     = aws_db_instance.main.port
    dbname   = var.db_name
    username = var.db_username
    password = random_password.db.result
    url      = "postgresql://${var.db_username}:${random_password.db.result}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/${var.db_name}"
  })
}

# ─── Outputs ───────────────────────────────────────────────

output "db_endpoint" {
  description = "RDS instance endpoint"
  value       = aws_db_instance.main.address
}

output "db_port" {
  description = "RDS instance port"
  value       = aws_db_instance.main.port
}

output "db_security_group_id" {
  description = "Security group ID — grant ingress from other resources that need DB access"
  value       = aws_security_group.db.id
}

output "db_connection_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the connection string"
  value       = aws_secretsmanager_secret.db_connection.arn
}

output "db_subnet_group_name" {
  description = "DB subnet group name"
  value       = aws_db_subnet_group.main.name
}
