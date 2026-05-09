# IAM role for one-off raw-prefix S3 cleanup scripts.
#
# Created for #4440. Grants a *narrow* set of S3 actions — minimum needed
# for `scripts/cleanup_mislabeled_s3_2661.py` (#2661) and
# `scripts/archive/cleanup_legacy_date_partitioned_s3.py` (#2627) —
# scoped to the `ca/{county}/{court}/raw/*` prefix only. Deliberately
# does NOT cover `derived/*`, `processed/*`, `transcripts/*`, the bucket
# root, or any other future sibling prefix.
#
# This is a *separate* role rather than an extension of `iam_scraper`
# (which lacks DeleteObject by design — scrapers should never delete) or
# `iam_agent` (whose trust policy is `dev_account_id:root` for STS users,
# not `ecs-tasks.amazonaws.com` for ECS tasks). Keeping the role
# separate keeps blast radius small per the AC's stated preference and
# makes the role trivially revocable (delete this module call, dev
# terraform apply runs and the role is gone).
#
# Trust policy: `ecs-tasks.amazonaws.com` only — designed to be passed
# via `scripts/ecs-run-task.sh --role <role-name>`. The role is not
# assumable by IAM users, EC2 instances, or any cross-account principal.
#
# Scoped to dev only — production raw-prefix cleanup is human-only per
# the issue's scope exclusions. To extend to staging/production later,
# instantiate this module in the corresponding environment with
# explicit operator review.

resource "aws_iam_role" "s3_cleanup" {
  name        = "judgemind-s3-cleanup-${var.environment}"
  description = "Assumed by ECS oneshot tasks running raw-prefix cleanup scripts (e.g. cleanup_mislabeled_s3_2661.py)"

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

# S3 actions on `ca/*/*/raw/*` only.
#
# The `*/*/raw/*` shape matches `ca/{county}/{court}/raw/{key}` exactly.
# Any other prefix (`ca/{county}/{court}/derived/*`, `processed/*`,
# `transcripts/*`, `staging/*`, `spotcheck/*`) is denied by omission.
#
# Actions included:
#   - s3:ListBucket — required so the script can paginate
#     `list_objects_v2` to find delete candidates. The
#     `s3:prefix` condition restricts list visibility to the `ca/`
#     subtree only — the cleanup script paginates from ``ca/`` and
#     filters keys client-side via the `ca/{county}/{court}/raw/<hex64>`
#     regex (#2661), so the prefix patterns must accept ``ca/`` and any
#     deeper child path under it. Listing outside ``ca/`` (e.g.
#     ``derived/``, ``processed/``, ``transcripts/``, ``staging/``,
#     ``spotcheck/``) remains denied. DeleteObject is separately and
#     more narrowly scoped to ``ca/*/*/raw/*`` below.
#   - s3:GetObject + s3:HeadObject — required by the cleanup script's
#     metadata-hash safety check (`head_object_metadata_hash` reads the
#     object's metadata before deciding to delete).
#   - s3:DeleteObject — the actual cleanup action.
#
# s3:PutObject is intentionally excluded — cleanup scripts should never
# write to the raw prefix.
resource "aws_iam_policy" "s3_raw_cleanup" {
  name        = "judgemind-s3-cleanup-raw-prefix-${var.environment}"
  description = "Allows raw-prefix cleanup: ListBucket + Get/Head/Delete on ca/*/*/raw/* only"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowDeleteOnRawPrefix"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:HeadObject",
          "s3:DeleteObject"
        ]
        Resource = [
          "${var.document_archive_bucket_arn}/ca/*/*/raw/*"
        ]
      },
      {
        Sid      = "AllowListBucketRawPrefix"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = var.document_archive_bucket_arn
        Condition = {
          StringLike = {
            # The cleanup script paginates list_objects_v2 from the
            # state-level prefix ("ca/") and filters keys client-side
            # via a `ca/{county}/{court}/raw/<hex64>` regex. The
            # `s3:prefix` condition gates which Prefix values the
            # ListBucket call may pass — so it must accept "ca/" and
            # any deeper child path under it. The narrower
            # `ca/*/*/raw/*` patterns alone reject `Prefix='ca/'` (#2661
            # AccessDenied at apply). DeleteObject below remains
            # narrowly scoped to `ca/*/*/raw/*` so this widening does
            # not expand the actual destructive blast radius.
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

resource "aws_iam_role_policy_attachment" "s3_raw_cleanup" {
  role       = aws_iam_role.s3_cleanup.name
  policy_arn = aws_iam_policy.s3_raw_cleanup.arn
}
