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

The Next.js web app (`packages/web/`) is deployed on **Vercel** with automatic Git-based deployments. Vercel watches the `judgemind/judgemind` repo and deploys when `packages/web/` changes on push to `main` (production) or any PR branch (preview). Non-web commits (scrapers, infra, docs) are automatically skipped via the Vercel `ignore_command` in Terraform.

**Infrastructure:** managed by Terraform module `vercel-web` in `infra/terraform/environments/hosting/`. The Vercel API token is stored in Secrets Manager at `judgemind/vercel/api-token`.

**Environments:**

| Environment | URL | Vercel project | Trigger |
|---|---|---|---|
| Dev | `dev.judgemind.org` | `judgemind-web-dev` | Push to `main` (only when `packages/web/` changed) |
| Preview | `*.vercel.app` (auto-generated) | `judgemind-web-dev` | Push to any PR branch (only when `packages/web/` changed) |

**Environment variables** (set in Vercel project, managed by Terraform):
- `NEXT_PUBLIC_GRAPHQL_URL` = `https://dev.api.judgemind.org/graphql`

**Checking deploy status (preferred — use `gh run watch`):**
```
# Watch the Vercel deploy status workflow (standard agent pattern)
gh run list --repo judgemind/judgemind --workflow vercel-deploy-status.yml --branch main --limit 1 --json databaseId -q '.[0].databaseId'
gh run watch <run-id> --repo judgemind/judgemind --interval 60 --exit-status --compact
```

The `vercel-deploy-status.yml` GitHub Action runs on every push to `main`. It detects whether `packages/web/` changed:
- **Web changed:** polls the Vercel Deployments API until the deploy completes, then exits with success/failure.
- **No web changes:** exits immediately with success (so the workflow stays green).

This lets agents use the standard `gh run watch` pattern instead of polling the Vercel API in a loop.

**Fallback (manual check):**
```
# List recent deployments (requires Vercel CLI: npm i -g vercel)
vercel list judgemind-web-dev --token "$VERCEL_API_TOKEN"

# Or check from the Vercel dashboard:
# https://vercel.com/judgemind2026-7926s-projects/judgemind-web-dev/deployments
```

## Terraform

### Terraform apply after merge

**Dev apply is automated by the dispatcher.** When the dispatcher merges a PR that touches `infra/terraform/`, it automatically runs `terraform apply` for the dev environment. The dispatcher detects infra PRs by checking changed file paths, determines which environments need an apply, and handles init/plan/apply inline. See `.claude/skills/dispatcher/SKILL.md` "Auto-apply dev terraform" for the full procedure.

**If the dispatcher is not running** (e.g., during interactive sessions), the subagent that authored the PR must apply to dev manually:
```
terraform -chdir=$REPO_ROOT/infra/terraform/environments/dev apply -target=module.<module_name> -auto-approve
```
Verify the apply succeeds. If it fails, file a `priority/p1` issue.

**For DNS/hosting environments** that require the Cloudflare API token:
```
scripts/with-secret.sh -e CLOUDFLARE_API_TOKEN=judgemind/cloudflare/api-token -- terraform -chdir=infra/terraform/environments/dns apply -auto-approve
```

**Important:** The root `infra/terraform/` directory does not track deployed resources. Each environment has its own state backend under `infra/terraform/environments/<env>/`. Running apply from the root creates duplicate resources that collide with the real ones. Always use the environment-specific path. Production applies (`environments/production/`) are human-only. **The PreToolUse hook (`preflight-bash.sh`) blocks `terraform apply` and `terraform destroy` commands that target the root path.** The `preflight_tf_not_root` function in `scripts/preflight.sh` provides the same check for scripts.

### Pre-PR Checklist for Terraform Tasks

See `docs/terraform-checklist.md` for the full checklist.

## ECS Script Execution

> **Important:** The dev database is in a private VPC and is not reachable from localhost. Do not attempt to connect to it locally using `scripts/with-secret.sh` with `DATABASE_URL` — the connection will fail. All data scripts must run inside the VPC via `ecs-run-task.sh`.

**Always use `ecs-run-task.sh` for data scripts.** It launches a standalone Fargate task with full VPC access, streams logs from CloudWatch, and handles cleanup automatically. `scripts/ecs-run.sh` uses ECS Exec (SSM sessions) which frequently disconnects within seconds, losing all output. Reserve `ecs-run.sh` only for quick interactive debugging (e.g. `scripts/ecs-run.sh bash`).

| Tool | Use for | Reliability | Notes |
|---|---|---|---|
| `scripts/ecs-run-task.sh` | All data scripts (backfills, migrations, audits) | Reliable | Standalone Fargate task, CloudWatch logs, no session timeout |
| `scripts/dev-db-query.sh` | Quick SQL queries | Good for short queries | Uses ECS Exec internally; may drop on long queries |
| `scripts/ecs-run.sh` | Interactive debugging only | Unreliable | SSM sessions drop after seconds; never use for scripts |

```
# Run a script and wait for completion (default)
scripts/ecs-run-task.sh scripts/backfill_ruling_fields.py -- --dry-run

# Long-running tasks: launch and detach, check logs later
scripts/ecs-run-task.sh --detach scripts/reingest_from_s3.py -- --all
scripts/ecs-run-task.sh --logs <task-arn>

# Tail logs for a task by ID (printed when the task launches)
scripts/ecs-task-logs.sh <task-id>
scripts/ecs-task-logs.sh <task-id> --follow

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
