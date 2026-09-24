# AWS Account Suspension Recovery

Runbook for bringing dev back after the AWS account (`155326049300`) is suspended and later reinstated — e.g. a billing lapse from an expired or cancelled card. Written from the 2026-08-17 → 2026-09-18 outage.

The headline lesson: **restoration is progressive, not atomic.** AWS re-enables subsystems over hours, and several components stay broken after `sts get-caller-identity` starts working again. Diagnose in dependency order and resist recreating infrastructure early — two of the three failures in 2026-09 self-healed, and destroying a half-restored ECS service would have made things worse.

## Recognising It

Scheduled GitHub workflows fail continuously (Smoke Test runs every 15 min — ~96 failure emails/day). `dev.judgemind.org` may still return 200 because Vercel does not depend on AWS; the API is what goes dark.

First: silence the noise so the mailbox stays usable. This is a repo-level toggle, no commit needed.

```
gh workflow disable "Smoke Test" --repo judgemind/judgemind
```

Also disable the other AWS-dependent scheduled workflows: `CC Dual-Run Diff`, `S3 Orphan-Rate Regression Guard`, `ECS Smoke Test`, `API Error Check`, `Data Quality Check`, `Short-Unsubstantive Ruling Check`, `Site Quality Check`. Re-enable with `gh workflow enable` once `/health` is green — the smoke test auto-closes its own `smoke-test-failure` issue on the first passing run.

## Diagnose In Dependency Order

Work bottom-up. A failure at a lower layer produces misleading symptoms above it.

1. **Credentials** — `aws sts get-caller-identity`.
2. **KMS** — `aws kms list-aliases` then `describe-key` on each. Disabled keys cascade into Secrets Manager *and* RDS.
3. **Secrets Manager** — `aws secretsmanager get-secret-value --secret-id <id> --query Name`. A task failing with `AccessDeniedException: Access to KMS is not allowed` is a KMS problem, not an IAM policy problem.
4. **ALB** — `dig +short <alb-dns>`. Zero A records means nodes are still provisioning. Note `LoadBalancerAddresses` is normally `[]` for ALBs, so it is not a useful signal; DNS is.
5. **ECS `RunTask`** — launch a task definition manually with the service's exact subnets/SG. This is the key discriminator (see below).
6. **RDS** — `aws rds describe-db-instances --query 'DBInstances[].DBInstanceStatus'`.
7. **OpenSearch / ElastiCache / S3** — these came through the 2026-09 outage undamaged.

## ECS Service Schedulers Wedge

The most confusing failure. Symptoms:

- Services sit at desired N / running 0 with **no service events at all**.
- `UpdateService` succeeds and changes `desiredCount`, but nothing is placed.
- `list-service-deployments` shows a deployment that failed ~200 ms after starting, `requestedTaskCount: 0`, statusReason `Service deployment failed.`
- CloudTrail shows zero scheduler activity — only your own calls.

Service events carry `IAM trust relationship has been misconfigured or changed ... AWSServiceRoleForECS` on a **6-hour cadence**. That cadence is the scheduler's reconcile loop, and it is the thing to wait for.

**The discriminator:** `RunTask` uses the task execution role; the service scheduler uses the service-linked role. If a manual `run-task` succeeds while services place nothing, the fault is the scheduler, not your task definition, image, subnets, security groups, or secrets.

Before concluding the service is corrupt, rule out the cheap causes — all were clean in 2026-09:

```
aws iam get-role --role-name AWSServiceRoleForECS          # trust policy + RoleLastUsed
aws iam list-attached-role-policies --role-name AWSServiceRoleForECS
aws application-autoscaling describe-scalable-targets --service-namespace ecs --region us-west-2
aws application-autoscaling describe-scheduled-actions --service-namespace ecs --region us-west-2
```

A `RoleLastUsed` timestamp that updates when you poke the service proves ECS *can* assume the role — so the role is not the problem, even while the error text blames it.

**Do not delete and recreate the service.** In 2026-09 both wedged services recovered unaided on the 6-hour cycle. A `terraform apply -replace` against a half-restored control plane risks a delete that hangs in DRAINING. If you eventually must, use `-target` — a bare dev plan carries unrelated drift (dispatcher-v3 task definitions with changed image digests) and a blanket apply would ship container images nobody released.

## RDS: `inaccessible-encryption-credentials` Is Terminal

The blocker that does **not** self-heal. RDS shuts the instance down when its storage-encryption KMS key becomes unreachable and parks it in a terminal state.

There are two variants, and the suffix decides everything:

- `inaccessible-encryption-credentials-recoverable` — re-enable the key, then `aws rds start-db-instance`.
- `inaccessible-encryption-credentials` — **cannot be started, modified, or renamed.** Restore only.

Both `start-db-instance` and `modify-db-instance` return `InvalidDBInstanceState` on the terminal variant, so the identifier can only be freed by deleting the instance.

### Recovery procedure

Confirm the key is healthy first (`aws kms describe-key` on the instance's `KmsKeyId` → `"KeyState": "Enabled"`), then capture the source config so the restore matches:

```
aws rds describe-db-instances --db-instance-identifier judgemind-dev --region us-west-2 \
  --query 'DBInstances[0].{Class:DBInstanceClass,SubnetGroup:DBSubnetGroup.DBSubnetGroupName,SGs:VpcSecurityGroups[].VpcSecurityGroupId,ParamGroup:DBParameterGroups[0].DBParameterGroupName,StorageType:StorageType,AZ:AvailabilityZone,PerfInsights:PerformanceInsightsEnabled}'
```

Restore to a staging identifier. `--use-latest-restorable-time` is better than the newest snapshot — in 2026-09 the PITR window reached the exact minute the instance died (2026-08-17T20:31:15Z), 17 hours newer than the last automated snapshot, so nothing was lost.

```
aws rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier judgemind-dev \
  --target-db-instance-identifier judgemind-dev-restore \
  --use-latest-restorable-time \
  --db-instance-class db.t4g.small --db-subnet-group-name judgemind-dev \
  --vpc-security-group-ids <sg> --db-parameter-group-name default.postgres16 \
  --storage-type gp3 --no-multi-az --no-publicly-accessible \
  --no-deletion-protection --availability-zone us-west-2a --region us-west-2
```

**Verify the restore before destroying anything.** The DB is in a private VPC, and `scripts/dev-db-query.sh` targets the production hostname, so query the staging endpoint by rewriting the host in `DATABASE_URL` and running it inside the ingestion worker via ECS Exec. Check all five schemas (`derived`, `dispatcher`, `public`, `staging`, `telemetry`) and row counts — especially `public.*` and `telemetry.*`, which are **not** rebuildable from S3.

Then swap identifiers. The `cbmkwssi8nes` infix is stable per account+region, so renaming restores the original hostname exactly and no application config changes:

```
aws rds delete-db-instance --db-instance-identifier judgemind-dev \
    --skip-final-snapshot --no-delete-automated-backups --region us-west-2
aws rds wait db-instance-deleted --db-instance-identifier judgemind-dev --region us-west-2
aws rds modify-db-instance --db-instance-identifier judgemind-dev-restore \
    --new-db-instance-identifier judgemind-dev --apply-immediately --region us-west-2
```

`--no-delete-automated-backups` keeps the pre-outage backups as a `retained` entry in `describe-db-instance-automated-backups`. Do not skip it. A final snapshot is not an option — the terminal state rejects it.

Renaming is asynchronous and `aws rds wait db-instance-available` fails with `DBInstanceNotFound` while it is in flight; poll `describe-db-instances` for `renaming` → `available` instead.

### After the swap

Restored instances do not inherit every setting. In 2026-09 `performance_insights_enabled` came back `false` against a declared `true`. Reconcile it so the next auto-apply is a no-op:

```
aws rds modify-db-instance --db-instance-identifier judgemind-dev \
    --enable-performance-insights --apply-immediately --region us-west-2
terraform -chdir=infra/terraform/environments/dev plan -lock=false -target=module.database
```

Terraform keys `aws_db_instance` by identifier, so it adopts the replacement cleanly — the 2026-09 plan showed **no destroy**, only that one attribute. This check is not optional: dev auto-applies on every push to `main` touching `infra/terraform/**`.

Finally, **force new deployments**. Application containers cache the old DB IP and keep failing with `EHOSTUNREACH` against the deleted instance even after DNS is correct:

```
scripts/ecs-redeploy.sh judgemind-api-dev
scripts/ecs-redeploy.sh judgemind-ingestion-worker-dev
```

## Verify

`/health` returning `{"status":"ok","db":"connected"}` is the gate. Then exercise the stack the way the smoke test does — `/`, `/rulings`, `/search`, a ruling/judge/case detail page, a GraphQL query, and `/api/documents/<id>/download` (expects 302).

Check ingestion is catching up rather than idle: `derived.rulings` count should climb (~9 rulings/min observed), `staging.captures` should drain to 0, and the worker logs should show `reingest_doc_timing` events. Crash-restart loops during the DB swap window are expected and stop once the rename lands.

## What Does Not Come Back

**Captured-to-S3 data survives; uncaptured rulings are gone forever.** California tentative rulings disappear from court sites within days, so every day the scraper is down is permanent loss. The 2026-09 outage lost 28 days (2026-08-18 → 2026-09-14, zero captures) — unrecoverable by any means.

Everything already in S3 is safe: `derived.*` rebuilds from it, and the ingestion worker drained the 903-capture backlog automatically once the DB returned (+1,922 rulings). Confirm capture freshness per county with `aws s3 ls s3://judgemind-document-archive-dev/ca/<county>/ --recursive` and compare the newest `LastModified` against the twice-daily schedule (`cron(15 6,18 * * ? *)` America/Los_Angeles).

Note that the archive is content-addressed by SHA-256, so a re-scrape of unchanged rulings writes **no** new S3 objects — a day with no new keys is not necessarily a day with no scraper run. Confirm against the scraper log streams instead.

## Pitfalls

- **`aws s3api ... --query` lies across pagination.** The CLI applies `--query` per page, so `max_by(...)`/`length(...)` over a large prefix returns one value per page. Use `aws s3 ls --recursive` piped to `awk` for per-prefix aggregates.
- **Don't re-enable the data-quality workflows while a reingest is mid-flight** — they compare S3 against the DB and will file spurious issues. Service-health workflows (Smoke Test, ECS Smoke Test, API Error Check) can go back on as soon as `/health` is green.
- **Distinguish pre-existing breakage from outage damage.** The 2026-09 recovery surfaced Riverside as dead since 2026-05-22, four months before the suspension and already tracked in #4637 / #4633. Check `gh issue list --search` before filing anything new.
