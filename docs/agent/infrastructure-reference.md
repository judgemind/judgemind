# Infrastructure — Agent Reference

> **When to read this:** only when working on deployed infrastructure, Vercel/frontend deploys, or AWS resources.

## Accounts

**GitHub:** org `judgemind/judgemind`, active account `judgemind-agent` (scopes: gist, project, read:org, repo, workflow).

**AWS:** account `155326049300`, user `admin`, region `us-west-2`. This is the Judgemind AWS account, not a personal account.

**Deployed resources (dev):**
- Terraform state: S3 bucket `judgemind-terraform-state`, DynamoDB lock table `judgemind-terraform-locks`
- Document archive: S3 bucket `judgemind-document-archive-dev`
- Assets: S3 bucket `judgemind-assets-dev`

## Web Frontend (Vercel)

The Next.js web app (`packages/web/`) is deployed on **Vercel** with automatic Git-based deployments. Vercel watches the `judgemind/judgemind` repo and deploys on every push to `main` (production) and on every PR branch (preview).

**Infrastructure:** managed by Terraform module `vercel-web` in `infra/terraform/environments/hosting/`. The Vercel API token is stored in Secrets Manager at `judgemind/vercel/api-token`.

**Environments:**

| Environment | URL | Vercel project | Trigger |
|---|---|---|---|
| Dev | `dev.judgemind.org` | `judgemind-web-dev` | Push to `main` |
| Preview | `*.vercel.app` (auto-generated) | `judgemind-web-dev` | Push to any PR branch |

**Environment variables** (set in Vercel project, managed by Terraform):
- `NEXT_PUBLIC_GRAPHQL_URL` = `https://dev.api.judgemind.org/graphql`

**Checking deploy status:**
```
# List recent deployments (requires Vercel CLI: npm i -g vercel)
vercel list judgemind-web-dev --token "$VERCEL_API_TOKEN"

# Or check from the Vercel dashboard:
# https://vercel.com/judgemind2026-7926s-projects/judgemind-web-dev/deployments
```

There is **no GitHub Actions deploy workflow** for the web frontend — Vercel handles deployments directly via its GitHub App integration. The `deploy-production.yml` workflow is for the scraper (ECS), not the web app. To check whether a frontend deploy succeeded after merging to `main`, check the Vercel dashboard or the commit status checks on the merge commit (Vercel posts deployment status as a GitHub commit status).

## Terraform

### Terraform apply after merge

After a Terraform PR merges to main, the subagent that authored the PR must apply to dev:
```
terraform -chdir=$REPO_ROOT/infra/terraform/environments/dev apply -target=module.<module_name> -auto-approve
```
Verify the apply succeeds. If it fails, file a `priority/p1` issue.

**Important:** The root `infra/terraform/` directory does not track deployed resources. Each environment has its own state backend under `infra/terraform/environments/<env>/`. Running apply from the root creates duplicate resources that collide with the real ones. Always use the environment-specific path. Production applies (`environments/production/`) are human-only. **The PreToolUse hook (`preflight-bash.sh`) blocks `terraform apply` and `terraform destroy` commands that target the root path.** The `preflight_tf_not_root` function in `scripts/preflight.sh` provides the same check for scripts.

### Pre-PR Checklist for Terraform Tasks

See `docs/terraform-checklist.md` for the full checklist.

## ECS Script Execution

Prefer `ecs-run-task.sh` over `ecs-run.sh`. `scripts/ecs-run.sh` uses ECS Exec (SSM sessions) which frequently disconnects within seconds, losing all output. Use `scripts/ecs-run-task.sh` instead — it launches a clean Fargate task and streams logs reliably from CloudWatch. Reserve `ecs-run.sh` only for quick interactive debugging (e.g. `scripts/ecs-run.sh bash`).

```
# Run a script and wait for completion (default)
scripts/ecs-run-task.sh scripts/backfill_ruling_fields.py -- --dry-run

# Long-running tasks: launch and detach, check logs later
scripts/ecs-run-task.sh --detach scripts/reingest_from_s3.py -- --all
scripts/ecs-run-task.sh --logs <task-arn>

# Override CPU/memory for heavy workloads
scripts/ecs-run-task.sh --cpu 2048 --memory 4096 scripts/backfill_parties.py
```

## Secrets Retrieval

Use `scripts/with-secret.sh` — never run `aws secretsmanager get-secret-value` as a standalone command (the secret value will appear in chat output). Never write secrets to disk. Instead, use the wrapper script to inject secrets as env vars:

```
scripts/with-secret.sh -e CF_API_TOKEN=judgemind/cloudflare/api-token -- terraform apply
scripts/with-secret.sh -e DB_USER=judgemind/dev/db/connection:.username -e DB_PASS=judgemind/dev/db/connection:.password -- ./run.sh
```

The `-e VAR=secret-id` form uses the raw SecretString. The `-e VAR=secret-id:.field` form extracts a JSON key. Multiple `-e` flags can be chained.
