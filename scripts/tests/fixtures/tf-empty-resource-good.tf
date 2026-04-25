# tf-empty-resource-good.tf — Fixture showing the #2740 fix pattern.
#
# Two variants:
#   1. count guard using length(local.compacted_arns) > 0 ? 1 : 0
#   2. for_each guard
#
# Both are correctly guarded and should not be flagged by the check.

# Variant 1: count guard
locals {
  compacted_arns = compact([var.a_arn, var.b_arn])
}

resource "aws_iam_role_policy" "good_secrets_count" {
  count = length(local.compacted_arns) > 0 ? 1 : 0

  name = "judgemind-good-secrets"
  role = "some-role"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadGoodSecrets"
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        Resource = local.compacted_arns
      }
    ]
  })
}

# Variant 2: for_each guard
resource "aws_iam_role_policy" "good_secrets_foreach" {
  for_each = toset(compact([var.c_arn, var.d_arn]))

  name = "judgemind-good-secrets-${each.key}"
  role = "some-role"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadGoodSecretsForEach"
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        Resource = [each.value]
      }
    ]
  })
}
