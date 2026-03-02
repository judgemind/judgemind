# Document archive bucket.
#
# Stores all original captured court documents (tentative rulings, PDFs, HTML
# snapshots). Original captures are never modified or deleted — the archive is
# the source of truth if downstream data stores need to be rebuilt.
#
# Object lock is enabled for production only. Dev/staging use plain versioning
# so test objects can be cleaned up freely.

resource "aws_s3_bucket" "document_archive" {
  bucket = var.bucket_name

  # Object lock can only be enabled at bucket creation time.
  # Set to true for production; cannot be changed after creation.
  object_lock_enabled = var.enable_object_lock

  tags = merge(
    {
      project     = "judgemind"
      environment = var.environment
    },
    var.tags,
  )
}

# Block all public access — court documents are served through the API, never
# directly from S3.
resource "aws_s3_bucket_public_access_block" "document_archive" {
  bucket = aws_s3_bucket.document_archive.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning provides a safety net against accidental overwrites. Required for
# object lock and for lifecycle rules that manage noncurrent versions.
resource "aws_s3_bucket_versioning" "document_archive" {
  bucket = aws_s3_bucket.document_archive.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Encrypt all objects at rest. AES256 (SSE-S3) requires no key management
# overhead and is free. Upgrade to SSE-KMS later if per-object access audit
# logging becomes a compliance requirement.
resource "aws_s3_bucket_server_side_encryption_configuration" "document_archive" {
  bucket = aws_s3_bucket.document_archive.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# Lifecycle rules implement the tiered storage strategy from Architecture Spec
# §4.4:
#   Current objects:    Standard → Standard-IA at 90 days → Glacier at 365 days
#   Noncurrent objects: Standard → Standard-IA at 30 days → Glacier at 90 days
#
# Noncurrent versions (prior snapshots of updated documents) transition faster
# because they exist only for auditing; they are rarely accessed.
resource "aws_s3_bucket_lifecycle_configuration" "document_archive" {
  bucket = aws_s3_bucket.document_archive.id

  # Versioning must be enabled before noncurrent_version rules take effect.
  depends_on = [aws_s3_bucket_versioning.document_archive]

  rule {
    id     = "tiered-storage"
    status = "Enabled"

    filter {}

    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = 365
      storage_class = "GLACIER"
    }

    noncurrent_version_transition {
      noncurrent_days = 30
      storage_class   = "STANDARD_IA"
    }

    noncurrent_version_transition {
      noncurrent_days = 90
      storage_class   = "GLACIER"
    }
  }
}

# Object lock (production only).
#
# COMPLIANCE mode prevents deletion of captured documents by anyone, including
# bucket owners and AWS Support. This satisfies Architecture Spec §7.5.1:
# "prevent any deletion, even by administrators."
#
# The default retention period is automatically applied to every new object
# uploaded. Scrapers do not need to set per-object lock metadata.
resource "aws_s3_bucket_object_lock_configuration" "document_archive" {
  count = var.enable_object_lock ? 1 : 0

  bucket = aws_s3_bucket.document_archive.id

  rule {
    default_retention {
      mode  = "COMPLIANCE"
      years = var.object_lock_retention_years
    }
  }
}
