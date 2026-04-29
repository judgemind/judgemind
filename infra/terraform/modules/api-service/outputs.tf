output "alb_dns_name" {
  description = "DNS name of the API ALB (use as CNAME target for dev.api.judgemind.org)"
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

output "task_role_arn" {
  description = "ARN of the API task IAM role (assumed by the container at runtime)"
  value       = aws_iam_role.api_task.arn
}

output "alb_arn_suffix" {
  description = "ARN suffix of the API ALB (used as CloudWatch dimension)"
  value       = aws_lb.api.arn_suffix
}

output "target_group_arn_suffix" {
  description = "ARN suffix of the API target group (used as CloudWatch dimension)"
  value       = aws_lb_target_group.api.arn_suffix
}

output "container_definitions_ssm_parameter_name" {
  description = "SSM parameter holding terraform-rendered container_definitions JSON. Pass to .github/actions/ecs-deploy as `desired-container-definitions-ssm-parameter` to make terraform the source of truth on deploy. See #3765."
  value       = aws_ssm_parameter.container_definitions.name
}
