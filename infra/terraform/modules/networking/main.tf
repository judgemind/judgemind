# VPC and networking module.
#
# Creates a VPC with public and private subnets across two AZs, a NAT gateway
# so private-subnet ECS tasks can reach court websites, and an internet gateway
# for the public subnets.
#
# Consumers (ECS, RDS, ElastiCache) are placed in private subnets. The NAT
# gateway allows outbound internet access without exposing tasks to inbound
# traffic. Public subnets hold the NAT gateway (and will hold a load balancer
# when the API is deployed).

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name        = "judgemind-${var.environment}"
    project     = "judgemind"
    environment = var.environment
  }
}

# ─── Internet Gateway ─────────────────────────────────────────────────────────

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name        = "judgemind-igw-${var.environment}"
    project     = "judgemind"
    environment = var.environment
  }
}

# ─── Public Subnets ───────────────────────────────────────────────────────────
# Two public subnets across two AZs. Hosts the NAT gateway (and future ALB).
# map_public_ip_on_launch is false — resources here get EIPs explicitly.

resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = var.availability_zones[count.index]
  map_public_ip_on_launch = false

  tags = {
    Name        = "judgemind-public-${count.index + 1}-${var.environment}"
    project     = "judgemind"
    environment = var.environment
    tier        = "public"
  }
}

# Route table for public subnets: default route via internet gateway.
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name        = "judgemind-public-rt-${var.environment}"
    project     = "judgemind"
    environment = var.environment
  }
}

resource "aws_route_table_association" "public" {
  count = 2

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# ─── NAT Gateway ─────────────────────────────────────────────────────────────
# Single NAT gateway in the first public subnet. A single NAT gateway is
# sufficient for cost-sensitive environments; add a second AZ gateway later
# if cross-AZ traffic cost becomes a concern.

resource "aws_eip" "nat" {
  domain = "vpc"

  tags = {
    Name        = "judgemind-nat-eip-${var.environment}"
    project     = "judgemind"
    environment = var.environment
  }

  depends_on = [aws_internet_gateway.main]
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id

  tags = {
    Name        = "judgemind-nat-${var.environment}"
    project     = "judgemind"
    environment = var.environment
  }

  depends_on = [aws_internet_gateway.main]
}

# ─── Private Subnets ─────────────────────────────────────────────────────────
# Two private subnets across two AZs. ECS Fargate tasks, RDS, and ElastiCache
# run here. Outbound internet traffic routes via the NAT gateway.

resource "aws_subnet" "private" {
  count = 2

  vpc_id            = aws_vpc.main.id
  cidr_block        = var.private_subnet_cidrs[count.index]
  availability_zone = var.availability_zones[count.index]

  tags = {
    Name        = "judgemind-private-${count.index + 1}-${var.environment}"
    project     = "judgemind"
    environment = var.environment
    tier        = "private"
  }
}

# Route table for private subnets: default route via NAT gateway.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }

  tags = {
    Name        = "judgemind-private-rt-${var.environment}"
    project     = "judgemind"
    environment = var.environment
  }
}

resource "aws_route_table_association" "private" {
  count = 2

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}
