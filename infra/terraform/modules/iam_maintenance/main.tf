# IAM role and policy for maintenance operations.
#
# The maintenance role extends the scraper role's S3 permissions with
# s3:ListBucket and s3:DeleteObject, required for operations like orphan
# cleanup (removing stale date-stamped keys after the S3 key migration).
#
# This role is NOT used for daily scraper runs — it is only assumed by
# one-off ECS tasks launched via scripts/ecs-run-task.sh --role.
#
# Trust policy allows ECS tasks to assume this role (same as the scraper
# role). No EC2 instance profile is created because maintenance tasks
# only run on Fargate.

resource "aws_iam_role" "maintenance" {
  name        = "judgemind-maintenance-${var.environment}"
  description = "Assumed by maintenance ECS tasks for S3 list/delete and DB access"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "ecs-tasks.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

# Full S3 policy: read/write (same as scraper) plus list and delete.
resource "aws_iam_policy" "maintenance_s3" {
  name        = "judgemind-maintenance-s3-${var.environment}"
  description = "Allows maintenance tasks to read, write, list, and delete objects in the document archive bucket"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowObjectOps"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:HeadObject",
          "s3:DeleteObject"
        ]
        Resource = "${var.document_archive_bucket_arn}/*"
      },
      {
        Sid      = "AllowListBucket"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = var.document_archive_bucket_arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "maintenance_s3" {
  role       = aws_iam_role.maintenance.name
  policy_arn = aws_iam_policy.maintenance_s3.arn
}
