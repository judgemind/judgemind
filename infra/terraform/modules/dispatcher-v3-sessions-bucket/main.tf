# Dispatcher v3 sessions bucket.
#
# Stores per-agent session logs (raw stream-json jsonl + compact transcript)
# written by the v3 task-runner and diagnoser. Per `docs/specs/dispatcher-v3-spec.md`
# section 4.1, the launcher reads `lastEventTimestamp` from CloudWatch Logs
# for silent-hang detection and the diagnoser reads the full session
# transcript from this bucket as input context.
#
# Sensitivity: session logs may contain operator commands and tool-call
# outputs that incidentally include secrets if a skill prints them. The
# bucket is private by construction: no public access, IAM-only writes
# from the dispatcher-v3 agent_task_role (F3, #3921), versioning enabled
# for accidental-overwrite recovery, SSE-S3 at rest.
#
# Lifecycle: Standard storage for the first 30 days (covers the diagnoser
# read window plus a few weeks of incident-replay headroom), then transition
# to Glacier Instant Retrieval (~80% cheaper, millisecond retrieval), and
# finally expire at `var.session_retention_days` (default 365 days). The
# retention is configurable via Terraform variable so operators can extend
# or shorten without changing the module.

resource "aws_s3_bucket" "sessions" {
  bucket = var.bucket_name
}

# Block all public access. Sessions are read only by the diagnoser
# (agent_task_role) via IAM-authenticated GetObject, never via a
# pre-signed URL or public read.
resource "aws_s3_bucket_public_access_block" "sessions" {
  bucket = aws_s3_bucket.sessions.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning enabled so an accidental overwrite (rare; agent_id is a
# UUID so collisions are negligible) leaves the prior version
# recoverable for a noncurrent-version retention window. Lifecycle
# below expires noncurrent versions after 30 days.
resource "aws_s3_bucket_versioning" "sessions" {
  bucket = aws_s3_bucket.sessions.id

  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-S3 (AES256). Same handling as the document-archive bucket; see
# `modules/storage/main.tf` for the rationale (no key-management
# overhead, free, sufficient for non-compliance workloads).
resource "aws_s3_bucket_server_side_encryption_configuration" "sessions" {
  bucket = aws_s3_bucket.sessions.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# Lifecycle: 30 days Standard, then Glacier-IR, expire at
# `var.session_retention_days`. Glacier-IR (Instant Retrieval) keeps
# millisecond retrieval latency for the diagnoser at ~1/5 the price of
# Standard. Noncurrent versions follow a shorter schedule because they
# exist only as accidental-overwrite safety nets.
resource "aws_s3_bucket_lifecycle_configuration" "sessions" {
  bucket = aws_s3_bucket.sessions.id

  # Versioning must be enabled before noncurrent_version rules take effect.
  depends_on = [aws_s3_bucket_versioning.sessions]

  rule {
    id     = "sessions-tiered-storage"
    status = "Enabled"

    filter {}

    # Glacier Instant Retrieval at 30 days. Diagnosers read recent
    # sessions during the active investigation window; older sessions
    # are read by humans during retros and audits, where cost matters
    # more than latency.
    transition {
      days          = 30
      storage_class = "GLACIER_IR"
    }

    expiration {
      days = var.session_retention_days
    }

    # Noncurrent (overwritten) versions: drop to Glacier-IR after 7
    # days, expire after 30. Operators rarely need to recover an
    # accidentally-overwritten session log; the short window covers
    # genuine accidents without long-term cost.
    noncurrent_version_transition {
      noncurrent_days = 7
      storage_class   = "GLACIER_IR"
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# Bucket policy: only the dispatcher-v3 agent_task_role may PutObject
# or GetObject. Every other principal is implicitly denied (no
# matching Allow statement plus the public-access-block above). This
# is the IAM-side counterpart to the v3 spec section 10 invariant
# that v3 ECS tasks are the only writers and readers.
#
# We grant only the two actions the spec actually needs:
#   - s3:PutObject       -- task-runner writes session log on EXIT.
#   - s3:GetObject       -- diagnoser reads session log on entry.
#
# We deliberately do NOT grant s3:ListBucket here. The diagnoser knows
# the exact key (`<agent_id>.jsonl`) from the agents table; bucket
# enumeration is unnecessary and would expose the full session history
# to any role with the bucket-level policy. ListBucket can be added
# later via a follow-up if a future skill needs to discover sessions.
resource "aws_s3_bucket_policy" "sessions" {
  bucket = aws_s3_bucket.sessions.id

  # The public-access-block must apply first so the bucket policy
  # cannot accidentally grant cross-account public access during a
  # racy create.
  depends_on = [aws_s3_bucket_public_access_block.sessions]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowAgentTaskRoleReadWrite"
        Effect = "Allow"
        Principal = {
          AWS = var.agent_task_role_arn
        }
        Action = [
          "s3:PutObject",
          "s3:GetObject",
        ]
        Resource = "${aws_s3_bucket.sessions.arn}/*"
      },
    ]
  })
}
