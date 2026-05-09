output "redis_endpoint" {
  description = "Hostname of the Redis cluster endpoint"
  value       = aws_elasticache_cluster.redis.cache_nodes[0].address
}

output "redis_port" {
  description = "Port of the Redis cluster"
  value       = aws_elasticache_cluster.redis.cache_nodes[0].port
}

output "security_group_id" {
  description = "Security group ID -- grant ingress from other resources that need Redis access"
  value       = aws_security_group.redis.id
}

output "memory_warn_alarm_name" {
  description = "Name of the warn-level (>80% DatabaseMemoryUsagePercentage) alarm. Empty when enable_alerts = false."
  value       = var.enable_alerts ? aws_cloudwatch_metric_alarm.cache_memory_warn[0].alarm_name : ""
}

output "memory_critical_alarm_name" {
  description = "Name of the critical-level (>95% DatabaseMemoryUsagePercentage) alarm. Empty when enable_alerts = false."
  value       = var.enable_alerts ? aws_cloudwatch_metric_alarm.cache_memory_critical[0].alarm_name : ""
}
