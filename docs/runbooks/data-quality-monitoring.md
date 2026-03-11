# Data Quality Monitoring Runbook

Operational procedures for the Judgemind data quality monitoring system.

## Overview

The data quality check runs hourly via a GitHub Actions scheduled workflow. It
launches an ECS Fargate task that queries the dev database and compares ruling
ingest rates and scraper activity against baselines.

| Component             | Resource                                              |
| --------------------- | ----------------------------------------------------- |
| GitHub Actions        | `.github/workflows/data-quality-check.yml`            |
| Check Script          | `scripts/data-quality-check.py`                       |
| Baselines Config      | `data-quality-baselines.json`                         |
| Schedule              | Hourly at :15 (cron `15 * * * *`)                     |
| ECS Execution         | Via `scripts/ecs-run-task.sh` (oneshot Fargate task)   |
| CloudWatch Logs       | `/ecs/judgemind-ingestion-worker-dev` (oneshot prefix) |

---

## How It Works

1. GitHub Actions triggers the workflow on schedule (hourly) or manually.
2. The workflow checks out the repo and configures AWS credentials.
3. It runs `scripts/ecs-run-task.sh scripts/data-quality-check.py` which:
   - Reads the ingestion worker task definition for image/secrets/networking
   - Creates a one-off Fargate task with the script
   - The task connects to the dev database via `DATABASE_URL` from Secrets Manager
   - Runs SQL queries to check ruling ingest rates and scraper staleness
4. If alerts are found (exit code 1), the workflow files a GitHub issue.
5. If all checks pass and an open `data-quality-failure` issue exists, it auto-closes.

---

## Checks Performed

### Ruling Ingest Rate
- Counts new rulings per county in the last 24 hours.
- Compares against the 7-day rolling average.
- Flags counties where the count drops below 50% of the average.

### Zero-Ruling Alert
- Any county with zero new rulings in 24h when it historically has >0.
- Severity: **p1** (immediate).

### Scraper Staleness
- Checks the latest scraper run timestamp per county.
- Flags daily scrapers stale after 6 hours, frequent scrapers after 2 hours.
- Severity: **p1** if stale for >4x the threshold, **p2** otherwise.

---

## Modifying the Schedule

Edit `.github/workflows/data-quality-check.yml` and update the cron expression:

```yaml
on:
  schedule:
    - cron: '15 * * * *'  # Change this
```

The schedule uses UTC. Common patterns:
- `15 * * * *` — every hour at :15
- `15 */2 * * *` — every 2 hours at :15
- `15 6 * * *` — once daily at 6:15 AM UTC

---

## Modifying Baselines

Edit `data-quality-baselines.json` in the repo root:

```json
{
  "counties": {
    "Los Angeles": {
      "expected_daily_rulings": 50,
      "schedule_type": "daily"
    }
  }
}
```

Fields:
- `expected_daily_rulings`: Expected number of rulings per day. Used for
  zero-ruling detection (if expected > 0 but actual = 0, it's a p1 alert).
- `schedule_type`: Either `"daily"` (6h stale threshold) or `"frequent"` (2h
  stale threshold).

To add a new county, add an entry to the `counties` object. To disable
monitoring for a county, remove its entry (the script falls back to the 7-day
rolling average).

---

## Running Manually

### From GitHub Actions
Navigate to Actions > Data Quality Check > Run workflow. Optionally specify a
county name or enable text output.

### From the command line
```bash
# Run via ECS (recommended — uses the same path as the scheduled check)
scripts/ecs-run-task.sh scripts/data-quality-check.py -- --text

# Check a single county
scripts/ecs-run-task.sh scripts/data-quality-check.py -- --text --county "Los Angeles"

# Run locally with direct DB access (requires DATABASE_URL)
scripts/with-secret.sh \
    -e DATABASE_URL=judgemind/dev/db/connection:.url \
    -- packages/scraper-framework/.venv/bin/python3 scripts/data-quality-check.py --text
```

---

## Troubleshooting

### Workflow fails to launch ECS task
- Check AWS credentials in GitHub Secrets (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`).
- Verify the ingestion worker task definition exists: `aws ecs describe-task-definition --task-definition judgemind-ingestion-worker-dev`.
- Check the ECS cluster is healthy: `aws ecs describe-clusters --clusters judgemind-dev`.

### Check runs but reports false positives
- Review baselines in `data-quality-baselines.json` — expected values may need updating.
- Run manually with `--text` for human-readable output.
- Check if the database has recent data: `scripts/dev-db-query.sh "SELECT county, COUNT(*) FROM documents d JOIN courts ct ON ct.id = d.court_id WHERE d.created_at > NOW() - INTERVAL '24 hours' GROUP BY county"`.

### Auto-filed issues are noisy
- Adjust thresholds in `scripts/data-quality-check.py` (constants at the top of the file).
- The `INGEST_DROP_THRESHOLD` (default 0.5) controls how much drop triggers an alert.
- `DAILY_SCRAPER_STALE_HOURS` (default 6) and `FREQUENT_SCRAPER_STALE_HOURS` (default 2) control staleness thresholds.

### Workflow not running on schedule
- GitHub Actions scheduled workflows may be disabled after 60 days of repo inactivity.
- Check the Actions tab for the workflow status.
- Trigger manually to verify it still works.
