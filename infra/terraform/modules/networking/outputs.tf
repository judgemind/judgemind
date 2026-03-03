output "vpc_id" {
  description = "ID of the VPC"
  value       = aws_vpc.main.id
}

output "private_subnet_ids" {
  description = "IDs of the two private subnets (for ECS tasks, RDS, ElastiCache)"
  value       = aws_subnet.private[*].id
}

output "public_subnet_ids" {
  description = "IDs of the two public subnets (for NAT gateway, future ALB)"
  value       = aws_subnet.public[*].id
}

output "nat_gateway_id" {
  description = "ID of the NAT gateway"
  value       = aws_nat_gateway.main.id
}

output "nat_gateway_public_ip" {
  description = "Public IP of the NAT gateway (whitelist this on court websites if needed)"
  value       = aws_eip.nat.public_ip
}
