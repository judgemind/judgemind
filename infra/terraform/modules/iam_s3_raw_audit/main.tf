# Read-only IAM role for auditing the raw archive on ECS (#4993).
#
# Lets an ECS oneshot run `scripts/audit_raw_delete_markers.py` (#4991):
# list every version and delete marker under `ca/`, read noncurrent raw
# versions to hash their bytes, and ask for a temporary thawed copy of a
# version in GLACIER / DEEP_ARCHIVE. It grants no delete and no write.
#
# History: #4440 created this module as `iam_s3_cleanup`
# (`judgemind-s3-cleanup-dev`) with `s3:DeleteObject` on raw keys, for two
# cleanup scripts that #4991 removed because their only guard was "no DB
# row references it". The rule now is that no raw is deleted without a
# live byte-identical copy (`docs/specs/architecture-spec-v1.md` §3.3.3),
# and only `framework.raw_delete_guard` may delete. No ECS role holds raw
# delete power, so this role was repurposed and renamed.
#
# Never grant `s3:DeleteObject`, `s3:DeleteObjectVersion`, `s3:PutObject`
# or any other write here: `DeleteObjectVersion` destroys a data version
# permanently, and `tests/policy-checks/check.sh` fails CI if any of them
# appears.
#
# This is a separate role rather than an addition to the general oneshot
# task role, so the version-read actions stay off every other task.
#
# Trust policy: `ecs-tasks.amazonaws.com` only, passed with
# `scripts/ecs-run-task.sh --role judgemind-s3-raw-audit-dev`. Not
# assumable by IAM users, EC2 instances or any cross-account principal.
#
# Dev only. To use it in another environment, instantiate the module there
# after operator review.

resource "aws_iam_role" "s3_raw_audit" {
  name        = "judgemind-s3-raw-audit-${var.environment}"
  description = "Assumed by ECS oneshot tasks that audit raw-archive versions read-only (e.g. audit_raw_delete_markers.py)"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EcsTasksAssume"
        Effect    = "Allow"
        Principal = { Service = "ecs-tasks.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

# Object actions are on `ca/*/*/raw/*` only, which matches
# `ca/{county}/{court}/raw/{key}`. Every other prefix (`derived/`,
# `processed/`, `transcripts/`, `staging/`, `spotcheck/`) is denied by
# omission.
#
# Object actions:
#   - s3:GetObject: read the current bytes of a live candidate copy
#     (`framework.raw_delete_guard.find_live_identical_copy`). HeadObject
#     is authorized by the same action.
#   - s3:GetObjectVersion: read or head one noncurrent version (the newest
#     real version before a delete marker) to hash its bytes.
#   - s3:RestoreObject: `--thaw` asks for a 7-day restored copy of a
#     version in GLACIER / DEEP_ARCHIVE. The copy is temporary and changes
#     no version; without it the audit can only report such versions as
#     needing a thaw.
#
# Bucket actions (bucket ARN, listing gated to the `ca/` subtree by the
# `s3:prefix` condition):
#   - s3:ListBucket: list current keys.
#   - s3:ListBucketVersions: list versions and delete markers.
resource "aws_iam_policy" "s3_raw_audit" {
  name        = "judgemind-s3-raw-audit-${var.environment}"
  description = "Read-only raw-archive audit: list versions under ca/, read and thaw versions of ca/*/*/raw/*; no delete, no write"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowReadRawVersions"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:GetObjectVersion",
          "s3:RestoreObject"
        ]
        Resource = [
          "${var.document_archive_bucket_arn}/ca/*/*/raw/*"
        ]
      },
      {
        Sid    = "AllowListRawVersions"
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
          "s3:ListBucketVersions"
        ]
        Resource = var.document_archive_bucket_arn
        Condition = {
          StringLike = {
            # The audit lists from the state-level prefix ("ca/") and
            # picks raw keys client-side, so the condition must accept
            # "ca/" and any deeper path. A narrower `ca/*/*/raw/*`
            # pattern rejects `Prefix='ca/'` (#2661). Listing exposes
            # key names only; reads stay on `ca/*/*/raw/*` above.
            "s3:prefix" = [
              "ca",
              "ca/",
              "ca/*"
            ]
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "s3_raw_audit" {
  role       = aws_iam_role.s3_raw_audit.name
  policy_arn = aws_iam_policy.s3_raw_audit.arn
}
