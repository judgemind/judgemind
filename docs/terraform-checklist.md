# Pre-PR Checklist for Terraform Tasks

Before marking a Terraform PR ready, complete ALL of the following locally:

1. `terraform fmt -check -recursive infra/terraform/` — fix any formatting issues.
2. Validate the **root** `infra/terraform/` config AND each environment:
   ```
   terraform -chdir=infra/terraform init -backend=false -lockfile=readonly && terraform validate
   ```
   The root config is the CI integration point — new required module variables must be added there too. The `-lockfile=readonly` flag is required — see `docs/agent/code-standards.md` §Terraform for why (#2582).
3. **Import any pre-existing resources** that were created outside Terraform before they were managed by code. Run `terraform import` for each, then verify with `terraform plan` that it shows no unexpected changes.
4. `terraform -chdir=infra/terraform/environments/<env> plan -no-color` (with real backend) — confirm the plan is clean or shows only expected changes (no destroys of existing resources).
5. `terraform apply` if the plan looks correct — verify `No changes` on a second `plan` afterward.
6. Check all test plan items in the PR body before requesting review.
