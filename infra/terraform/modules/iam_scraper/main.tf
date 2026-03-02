# IAM role and policy for the scraper process.
#
# The scraper role grants write-only access to the document archive bucket.
# Principle of least privilege: s3:PutObject on archive objects only.
# No s3:GetObject, s3:DeleteObject, or bucket-level permissions are granted.
#
# Trust policy is scoped to EC2. When the scraper compute environment is
# finalised (ECS task role, Lambda, or GitHub Actions OIDC), update the
# assume_role_policy and the associated instance_profile accordingly.

resource "aws_iam_role" "scraper" {
  name        = "judgemind-scraper-${var.environment}"
  description = "Assumed by the scraper process to write captured documents to S3"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "ec2.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    project     = "judgemind"
    environment = var.environment
  }
}

# Write-only policy: s3:PutObject on archive objects.
resource "aws_iam_policy" "scraper_s3_write" {
  name        = "judgemind-scraper-s3-write-${var.environment}"
  description = "Allows the scraper to write objects to the document archive bucket"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowPutObject"
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${var.document_archive_bucket_arn}/*"
      }
    ]
  })

  tags = {
    project     = "judgemind"
    environment = var.environment
  }
}

resource "aws_iam_role_policy_attachment" "scraper_s3_write" {
  role       = aws_iam_role.scraper.name
  policy_arn = aws_iam_policy.scraper_s3_write.arn
}

# EC2 instance profile wrapping the role so EC2 instances can assume it.
# Not required for ECS/Lambda/OIDC — update when compute is finalised.
resource "aws_iam_instance_profile" "scraper" {
  name = "judgemind-scraper-${var.environment}"
  role = aws_iam_role.scraper.name

  tags = {
    project     = "judgemind"
    environment = var.environment
  }
}
