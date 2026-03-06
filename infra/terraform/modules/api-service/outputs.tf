output "alb_dns_name" {
  description = "DNS name of the API ALB (use as CNAME target for api.dev.judgemind.org)"
  value       = aws_lb.api.dns_name
}

output "alb_zone_id" {
  description = "Route53 zone ID of the ALB (for alias records)"
  value       = aws_lb.api.zone_id
}

output "alb_arn" {
  description = "ARN of the API ALB"
  value       = aws_lb.api.arn
}

output "service_name" {
  description = "Name of the API ECS service"
  value       = aws_ecs_service.api.name
}

output "log_group_name" {
  description = "CloudWatch log group for API output"
  value       = aws_cloudwatch_log_group.api.name
}

output "task_security_group_id" {
  description = "Security group ID for API ECS tasks"
  value       = aws_security_group.api_task.id
}

output "acm_certificate_arn" {
  description = "ARN of the ACM certificate for the API domain"
  value       = aws_acm_certificate.api.arn
}

output "acm_domain_validation_options" {
  description = "ACM certificate DNS validation records (create these in your DNS provider)"
  value       = aws_acm_certificate.api.domain_validation_options
}
