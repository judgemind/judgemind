output "redis_endpoint" {
  description = "Hostname of the Redis cluster endpoint"
  value       = aws_elasticache_cluster.redis.cache_nodes[0].address
}

output "redis_port" {
  description = "Port of the Redis cluster"
  value       = aws_elasticache_cluster.redis.cache_nodes[0].port
}

output "security_group_id" {
  description = "Security group ID — grant ingress from other resources that need Redis access"
  value       = aws_security_group.redis.id
}
