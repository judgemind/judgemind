# Scraper Operations Runbook

Operational procedures for the Judgemind court-document scraper infrastructure
running on AWS ECS Fargate.

## Architecture Overview

| Component          | Resource                                                  |
| ------------------ | --------------------------------------------------------- |
| ECS Cluster        | `judgemind-{env}`                                         |
| Task Definition    | `judgemind-scraper-{env}`                                 |
| EventBridge Sched. | `judgemind-scraper-{env}` (daily at 6 AM PT)              |
| CloudWatch Logs    | `/ecs/judgemind-scraper-{env}`                            |
| S3 Archive         | `judgemind-document-archive-{env}`                        |
| SNS Alerts         | `judgemind-scraper-alerts-{env}`                          |
| ECR Repository     | `judgemind/scraper`                                       |

Replace `{env}` with `dev`, `staging`, or `production`.

---

## Trigger a Manual Scraper Run

Run all registered scrapers:

```bash
aws ecs run-task \
  --cluster judgemind-dev \
  --task-definition judgemind-scraper-dev \
  --launch-type FARGATE \
  --network-configuration '{
    "awsvpcConfiguration": {
      "subnets": ["<PRIVATE_SUBNET_ID>"],
      "securityGroups": ["<SCRAPER_SG_ID>"],
      "assignPublicIp": "DISABLED"
    }
  }' \
  --region us-west-2
```

Run a single scraper by passing overrides:

```bash
aws ecs run-task \
  --cluster judgemind-dev \
  --task-definition judgemind-scraper-dev \
  --launch-type FARGATE \
  --overrides '{
    "containerOverrides": [{
      "name": "scraper",
      "command": ["ca-la-tentatives-civil"]
    }]
  }' \
  --network-configuration '{
    "awsvpcConfiguration": {
      "subnets": ["<PRIVATE_SUBNET_ID>"],
      "securityGroups": ["<SCRAPER_SG_ID>"],
      "assignPublicIp": "DISABLED"
    }
  }' \
  --region us-west-2
```

Retrieve subnet and security group IDs from Terraform outputs:

```bash
cd infra/terraform/environments/dev
terraform output private_subnet_ids
terraform output scraper_security_group_id
```

### Registered Scraper IDs

| ID                          | Court                              |
| --------------------------- | ---------------------------------- |
| `ca-la-tentatives-civil`    | LA Superior Court tentative rulings |
| `ca-oc-tentatives`          | Orange County tentative rulings     |
| `ca-riverside-tentatives`   | Riverside County tentative rulings  |
| `ca-sb-tentatives`          | San Bernardino tentative rulings    |

---

## Read Scraper Logs

### CloudWatch Console

Navigate to **CloudWatch > Log groups > `/ecs/judgemind-scraper-{env}`**.

Each task run creates a log stream prefixed with `scraper/scraper/<task-id>`.

### CLI — tail recent logs

```bash
aws logs tail /ecs/judgemind-scraper-dev --since 1h --follow --region us-west-2
```

### CLI — search for errors in the last 24 hours

```bash
aws logs filter-log-events \
  --log-group-name /ecs/judgemind-scraper-dev \
  --start-time "$(date -d '24 hours ago' +%s)000" \
  --filter-pattern "ERROR" \
  --region us-west-2
```

### Verify last successful run

The runner emits a `scraper_run_complete` log line on success. Search for it:

```bash
aws logs filter-log-events \
  --log-group-name /ecs/judgemind-scraper-dev \
  --start-time "$(date -d '48 hours ago' +%s)000" \
  --filter-pattern '"scraper_run_complete"' \
  --region us-west-2
```

---

## Check S3 Archive Contents

### List recent objects

```bash
aws s3 ls s3://judgemind-document-archive-dev/ --recursive \
  | sort -k1,2 | tail -20
```

### Count objects by prefix (court)

```bash
aws s3 ls s3://judgemind-document-archive-dev/ --recursive --summarize \
  | grep "Total"
```

### Download a specific document

```bash
aws s3 cp s3://judgemind-document-archive-dev/ca/la/tentatives/2026/03/03/ruling.pdf ./
```

Object paths follow the pattern:
`{state}/{county}/{doc_type}/{year}/{month}/{day}/{filename}`

---

## Add a New Court Scraper

1. **Create the scraper module** under `packages/scraper-framework/src/courts/`:

   ```
   packages/scraper-framework/src/courts/{state}/{court_scraper}.py
   ```

   Extend `BaseScraper` and implement the `scrape()` method. Provide a
   `default_config()` factory function.

2. **Register the scraper** in
   `packages/scraper-framework/src/framework/runner.py`:

   ```python
   from courts.{state}.{court_scraper} import MyScraper
   from courts.{state}.{court_scraper} import default_config as my_config

   _REGISTRY.extend([
       ("state-court-doctype", MyScraper, my_config),
   ])
   ```

3. **Add tests** under `packages/scraper-framework/tests/`.

4. **Push to main** -- the `deploy-scraper.yml` workflow will automatically
   build a new Docker image and push it to ECR. The next scheduled ECS task
   will pick up the `latest` tag.

---

## Enable or Disable the Schedule

### Via Terraform (recommended)

In the environment config (e.g., `infra/terraform/environments/dev/main.tf`),
set `schedule_enabled`:

```hcl
module "compute" {
  # ...
  schedule_enabled = true   # or false to disable
}
```

Then apply:

```bash
cd infra/terraform/environments/dev
terraform apply
```

### Via AWS CLI (emergency toggle)

Disable the schedule immediately:

```bash
aws scheduler update-schedule \
  --name judgemind-scraper-dev \
  --state DISABLED \
  --schedule-expression "cron(0 6 * * ? *)" \
  --schedule-expression-timezone "America/Los_Angeles" \
  --flexible-time-window '{"Mode":"FLEXIBLE","MaximumWindowInMinutes":30}' \
  --target '{}' \
  --region us-west-2
```

Re-enable:

```bash
aws scheduler update-schedule \
  --name judgemind-scraper-dev \
  --state ENABLED \
  --schedule-expression "cron(0 6 * * ? *)" \
  --schedule-expression-timezone "America/Los_Angeles" \
  --flexible-time-window '{"Mode":"FLEXIBLE","MaximumWindowInMinutes":30}' \
  --target '{}' \
  --region us-west-2
```

> **Note:** The `update-schedule` CLI requires all parameters to be passed, not
> just the ones you want to change. Use `aws scheduler get-schedule` first to
> capture the current config, then modify the `State` field.

### Check current schedule state

```bash
aws scheduler get-schedule \
  --name judgemind-scraper-dev \
  --region us-west-2 \
  --query '{State: State, Expression: ScheduleExpression, Timezone: ScheduleExpressionTimezone}'
```

---

## Alerts and Monitoring

### CloudWatch Alarm: No Successful Run in 24 Hours

**Alarm name:** `judgemind-scraper-no-success-24h-{env}`

This alarm monitors the custom metric `Judgemind/Scraper > ScraperSuccessCount`.
A CloudWatch Logs metric filter increments this counter each time the runner
logs `scraper_run_complete`. If the sum over a 24-hour period is zero (or no
data points exist), the alarm enters ALARM state and publishes to the
`judgemind-scraper-alerts-{env}` SNS topic.

### Subscribe to alerts

Add an email subscription via Terraform (`alert_email` variable) or manually:

```bash
aws sns subscribe \
  --topic-arn arn:aws:sns:us-west-2:<ACCOUNT_ID>:judgemind-scraper-alerts-dev \
  --protocol email \
  --notification-endpoint ops@judgemind.org \
  --region us-west-2
```

### Acknowledge / silence an alarm

```bash
aws cloudwatch set-alarm-state \
  --alarm-name judgemind-scraper-no-success-24h-dev \
  --state-value OK \
  --state-reason "Manual acknowledgement — investigating" \
  --region us-west-2
```

---

## Troubleshooting

### Task fails to start (STOPPED immediately)

1. Check the stopped reason:

   ```bash
   aws ecs describe-tasks \
     --cluster judgemind-dev \
     --tasks <TASK_ARN> \
     --region us-west-2 \
     --query 'tasks[0].{status:lastStatus,reason:stoppedReason,code:stopCode}'
   ```

2. Common causes:
   - **CannotPullContainerError**: ECR image tag missing or execution role
     lacks ECR pull permissions.
   - **ResourceNotFoundException**: Task definition revision deleted.
   - **ENI limit**: VPC has no available ENIs in the target subnet.

### Scraper runs but captures zero documents

1. Check logs for HTTP errors (403, 503) -- the court website may be blocking
   or rate-limiting.
2. Verify the court website HTML structure has not changed (selector mismatch).
3. Run the scraper locally to debug:

   ```bash
   cd packages/scraper-framework
   pip install -e .
   python -m framework ca-la-tentatives-civil
   ```

### Schedule not firing

1. Confirm the schedule is enabled:

   ```bash
   aws scheduler get-schedule --name judgemind-scraper-dev --region us-west-2
   ```

2. Check EventBridge Scheduler execution history in CloudWatch Logs
   (if Scheduler logging is enabled) or look for new ECS tasks:

   ```bash
   aws ecs list-tasks --cluster judgemind-dev --region us-west-2
   ```
