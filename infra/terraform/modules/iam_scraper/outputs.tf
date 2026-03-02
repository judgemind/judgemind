output "role_arn" {
  description = "ARN of the scraper IAM role"
  value       = aws_iam_role.scraper.arn
}

output "role_name" {
  description = "Name of the scraper IAM role"
  value       = aws_iam_role.scraper.name
}

output "instance_profile_arn" {
  description = "ARN of the EC2 instance profile for the scraper role"
  value       = aws_iam_instance_profile.scraper.arn
}

output "instance_profile_name" {
  description = "Name of the EC2 instance profile for the scraper role"
  value       = aws_iam_instance_profile.scraper.name
}
