output "project_id" {
  description = "Vercel project ID"
  value       = vercel_project.web.id
}

output "project_name" {
  description = "Vercel project name"
  value       = vercel_project.web.name
}

output "vercel_domain" {
  description = "Auto-assigned vercel.app domain for the project"
  value       = "${vercel_project.web.name}.vercel.app"
}
