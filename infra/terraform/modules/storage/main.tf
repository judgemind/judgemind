# Document archive bucket — THE most critical infrastructure component.
# Tentative rulings are ephemeral. Once captured, they must never be lost.

variable "environment" {
  type = string
}

resource "aws_s3_bucket" "document_archive" {
  bucket = "judgemind-document-archive-${var.environment}"
}

resource "aws_s3_bucket_versioning" "document_archive" {
  bucket = aws_s3_bucket.document_archive.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Object lock prevents deletion — even by admins.
# Only enable in production; dev/staging need ability to clean up.
resource "aws_s3_bucket_object_lock_configuration" "document_archive" {
  count  = var.environment == "production" ? 1 : 0
  bucket = aws_s3_bucket.document_archive.id

  rule {
    default_retention {
      mode = "GOVERNANCE" # Can be overridden with special permission, unlike COMPLIANCE
      days = 3650         # 10 years
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "document_archive" {
  bucket = aws_s3_bucket.document_archive.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Lifecycle: move old documents to cheaper storage tiers
resource "aws_s3_bucket_lifecycle_configuration" "document_archive" {
  bucket = aws_s3_bucket.document_archive.id

  rule {
    id     = "archive-old-documents"
    status = "Enabled"

    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = 365
      storage_class = "GLACIER"
    }
  }
}

# Block all public access
resource "aws_s3_bucket_public_access_block" "document_archive" {
  bucket                  = aws_s3_bucket.document_archive.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# General assets bucket (non-critical: user uploads, temp files, etc.)
resource "aws_s3_bucket" "assets" {
  bucket = "judgemind-assets-${var.environment}"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "assets" {
  bucket                  = aws_s3_bucket.assets.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Terraform state bucket (bootstrap manually or via a separate config)
# resource "aws_s3_bucket" "terraform_state" {
#   bucket = "judgemind-terraform-state"
# }

output "document_archive_bucket" {
  value = aws_s3_bucket.document_archive.bucket
}

output "document_archive_arn" {
  value = aws_s3_bucket.document_archive.arn
}

output "assets_bucket" {
  value = aws_s3_bucket.assets.bucket
}
