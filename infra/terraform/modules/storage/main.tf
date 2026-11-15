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

  tags = var.tags
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
#   Current objects:    Standard → Standard-IA at 90 days → Glacier-IR at 365 days
#   Noncurrent objects: Standard → Standard-IA at 30 days → Glacier-IR at 90 days
#
# Noncurrent versions (prior snapshots of updated documents) transition faster
# because they exist only for auditing; they are rarely accessed.
#
# The cold tier is GLACIER_IR (Glacier Instant Retrieval), not GLACIER
# (Flexible Retrieval). GetObject reads a GLACIER_IR object directly, but a
# GLACIER object needs a restore request first and otherwise fails with
# InvalidObjectState. S3 is the source of truth: rebuild_db.py and
# reingest_from_s3.py must read year-old captures, and the LLM cache treats
# any get_object error as a miss (operator decision on #4854). Do not switch
# back to GLACIER or DEEP_ARCHIVE. Lifecycle cannot move objects already in
# GLACIER to GLACIER_IR; scripts/move_glacier_to_glacier_ir_4854.py does that.
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
      storage_class = "GLACIER_IR"
    }

    noncurrent_version_transition {
      noncurrent_days = 30
      storage_class   = "STANDARD_IA"
    }

    noncurrent_version_transition {
      noncurrent_days = 90
      storage_class   = "GLACIER_IR"
    }
  }

  # LLM extraction cache (llm-cache/): deliberately has NO expiration rule.
  # Entries are kept indefinitely (#4831). An expired entry makes the next
  # reingest/rebuild re-run the LLM, which can split a PDF differently and
  # shift positional split-child ids, undoing the bytes-only cache keys of
  # #4815. A rebuild of derived.* must not depend on a fresh LLM run. Entries
  # are ~100 KB each, so keeping them costs little. Do not add a TTL or a
  # noncurrent-version expiration here: noncurrent versions are the only way
  # to recover an entry overwritten by --bust-llm-cache. See
  # docs/agent/llm-cache.md.

  # Spotcheck results: regenerable anytime by re-running the spotcheck script.
  rule {
    id     = "spotcheck-ttl"
    status = "Enabled"

    filter {
      prefix = "spotcheck/"
    }

    expiration {
      days = 90
    }
  }

  # Scratch/temp files: eval samples, ad-hoc uploads.
  rule {
    id     = "tmp-ttl"
    status = "Enabled"

    filter {
      prefix = "tmp/"
    }

    expiration {
      days = 30
    }
  }

  # One-off migration scripts uploaded by ecs-run-task.sh.
  rule {
    id     = "migrations-ttl"
    status = "Enabled"

    filter {
      prefix = "migrations/"
    }

    expiration {
      days = 30
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

# Raw-delete backstop (#5013).
#
# Raw captures are the irreplaceable source of truth: no raw may be deleted
# unless a live, byte-identical copy exists (Architecture Spec §3.3.3). The
# code-level layers are framework.raw_delete_guard (#4992), the
# check-no-unguarded-s3-delete.sh CI guard, and roles that carry no delete
# grant on raws (#4993). This bucket policy is the layer under them: an
# explicit Deny beats every Allow, so no role, user or root session can
# delete a raw object or a raw object version, whatever its IAM policy says.
#
# Raw keys are `<state>/<county>/<court>/raw/<sha>.<ext>` (ca/, federal/,
# the courtlistener state prefixes, unknown/). `*/raw/*` matches every
# depth because `*` in an S3 ARN spans `/`. `directories/` holds the court
# directory snapshots that derived.court_directory_snapshots is rebuilt from.
#
# The only exception is the break-glass role. It does NOT exist by default:
# an operator who must remove a delete marker on a raw
# (scripts/audit_raw_delete_markers.py --restore) or delete a raw through
# framework.raw_delete_guard creates it for the job, with an MFA-gated trust
# policy, and deletes it afterwards. See docs/agent/infrastructure-reference.md
# §Raw-delete break-glass.
#
# What this does not cover, and why that is fine:
# - Lifecycle expiration is not a DeleteObject call. No lifecycle rule above
#   expires a raw prefix (tests/policy-checks/check.sh asserts it), and the
#   second statement stops any principal but Terraform from adding one.
# - Overwriting a raw (PutObject on an existing key) keeps the old bytes as a
#   noncurrent version, because the bucket is versioned.
# - Object Lock: evaluated and not enabled in dev (#5013). Its default
#   retention is bucket-wide, so it would also pin tmp/, migrations/ and
#   spotcheck/ objects that the lifecycle rules above expire, and it never
#   blocks a plain DeleteObject (that only adds a delete marker). This policy
#   blocks both delete forms on raw prefixes only.
data "aws_partition" "current" {}

data "aws_caller_identity" "current" {}

locals {
  raw_delete_break_glass_role_name = var.raw_delete_break_glass_role_name != "" ? var.raw_delete_break_glass_role_name : "judgemind-raw-break-glass-${var.environment}"
  raw_delete_break_glass_role_arn  = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${local.raw_delete_break_glass_role_name}"

  raw_object_resources = [
    "${aws_s3_bucket.document_archive.arn}/*/raw/*",
    "${aws_s3_bucket.document_archive.arn}/raw/*",
    "${aws_s3_bucket.document_archive.arn}/directories/*",
  ]
}

data "aws_iam_policy_document" "document_archive" {
  statement {
    sid       = "DenyRawDeleteExceptBreakGlass"
    effect    = "Deny"
    actions   = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
    resources = local.raw_object_resources

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = [local.raw_delete_break_glass_role_arn]
    }
  }

  # A lifecycle rule with an expiration on a raw prefix would delete raws
  # without a single DeleteObject call. Only the Terraform apply identity
  # (and the break-glass role) may change lifecycle rules.
  dynamic "statement" {
    for_each = length(var.lifecycle_admin_principal_arns) > 0 ? [1] : []
    content {
      sid       = "DenyLifecycleChangeExceptTerraform"
      effect    = "Deny"
      actions   = ["s3:PutLifecycleConfiguration"]
      resources = [aws_s3_bucket.document_archive.arn]

      principals {
        type        = "*"
        identifiers = ["*"]
      }

      condition {
        test     = "ArnNotEquals"
        variable = "aws:PrincipalArn"
        values   = concat(var.lifecycle_admin_principal_arns, [local.raw_delete_break_glass_role_arn])
      }
    }
  }
}

resource "aws_s3_bucket_policy" "document_archive" {
  bucket = aws_s3_bucket.document_archive.id
  policy = data.aws_iam_policy_document.document_archive.json

  # S3 can reject a policy write while the public access block is changing.
  depends_on = [aws_s3_bucket_public_access_block.document_archive]
}
