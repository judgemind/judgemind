# tf-empty-resource-bad.tf — Fixture replicating the #2739 shape.
#
# An aws_iam_role_policy with Resource = compact([var.a_arn, var.b_arn]) and
# no top-level count or for_each.  When all input vars are "", compact()
# returns [] and the policy is created with an empty Resource list — silent
# misconfiguration.

resource "aws_iam_role_policy" "bad_secrets" {
  name = "judgemind-bad-secrets"
  role = "some-role"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadBadSecrets"
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        Resource = compact([
          var.a_arn,
          var.b_arn,
        ])
      }
    ]
  })
}
