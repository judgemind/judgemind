# Data Quality Monitoring Runbook

Operational procedures for the Judgemind data quality monitoring system.

## Overview

The data quality check runs hourly via a GitHub Actions scheduled workflow. It
launches an ECS Fargate task that queries the dev database and compares ruling
ingest rates and scraper activity against baselines. Results are persisted to
the `data_quality_metrics` database table and displayed on the
`/admin/data-quality` web dashboard.

**Alert philosophy:** The hourly check collects metrics and detects anomalies.
Transient conditions (zero rulings, ingest rate drops, field completeness
fluctuations) are dashboard-only -- they historically self-resolve within hours
and do not warrant GitHub issues or human notifications. Only persistent,
unresolvable conditions (scraper stale >24h on a business day) trigger a
Telegram notification to the human.

| Component             | Resource                                              |
| --------------------- | ----------------------------------------------------- |
| GitHub Actions        | `.github/workflows/data-quality-check.yml`            |
| Check Script          | `scripts/data-quality-check.py`                       |
| Baselines Config      | `data-quality-baselines.json`                         |
| Schedule              | Hourly at :15 (cron `15 * * * *`)                     |
| ECS Execution         | Via `scripts/ecs-run-task.sh` (oneshot Fargate task)   |
| CloudWatch Logs       | `/ecs/judgemind-ingestion-worker-dev` (oneshot prefix) |
| Dashboard             | `https://dev.judgemind.org/admin/data-quality`        |

---

## How It Works

1. GitHub Actions triggers the workflow on schedule (hourly) or manually.
2. The workflow checks out the repo and configures AWS credentials.
3. It runs `scripts/ecs-run-task.sh scripts/data-quality-check.py` which:
   - Reads the ingestion worker task definition for image/secrets/networking
   - Creates a one-off Fargate task with the script
   - The task connects to the dev database via `DATABASE_URL` from Secrets Manager
   - Runs SQL queries to check ruling ingest rates and scraper staleness
   - Persists per-county metrics to the `data_quality_metrics` table
4. If P1 alerts are found, a Telegram notification is sent to the human.
5. All alert details are visible on the `/admin/data-quality` dashboard.

**No GitHub issues are filed.** Transient conditions that historically
auto-resolve within hours are displayed on the dashboard only. This eliminates
the ~1.8 issues/day of noise from the old design.

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
- Flags daily scrapers stale after 26 hours (24h cycle + 2h buffer), frequent scrapers after 2 hours.
- Severity: **p1** if stale for >4x the threshold, **p2** otherwise.

### Field Completeness
- Checks per-county field completeness against baselines.
- Flags regressions that exceed the configured thresholds.
- Dashboard metric only -- does not trigger notifications.

### Orphaned Documents
- Checks for documents with no associated rulings.
- Dashboard metric only -- tracked for trend analysis.

### ECS Service Health
- Checks running vs desired count for ECS services.
- Severity: **p1** if running count is zero, **p2** otherwise.

---

## Alerting Behavior

| Signal | Severity | Behavior |
|--------|----------|----------|
| Scraper stale (>26h) | p1 | Telegram notification + dashboard |
| Zero rulings (24h) | p1 | Dashboard only |
| ECS service unhealthy | p1/p2 | Telegram notification (if P1) + dashboard |
| Ingest rate drop (>50%) | p2 | Dashboard only |
| Field completeness drop | p1/p2 | Dashboard only |
| Orphaned documents | p2 | Dashboard only |

Telegram notifications are sent only for P1 alerts. All alert details are
always available on the dashboard regardless of severity.

---

## Modifying the Schedule

Edit `.github/workflows/data-quality-check.yml` and update the cron expression:

```yaml
on:
  schedule:
    - cron: '15 * * * *'  # Change this
```

The schedule uses UTC. Common patterns:
- `15 * * * *` -- every hour at :15
- `15 */2 * * *` -- every 2 hours at :15
- `15 6 * * *` -- once daily at 6:15 AM UTC

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
- `schedule_type`: Either `"daily"` (26h stale threshold) or `"frequent"` (2h
  stale threshold).

To add a new county, add an entry to the `counties` object. To disable
monitoring for a county, remove its entry (the script falls back to the 7-day
rolling average).

### Expected Null Rates

Some counties have irreducible null rates for specific fields because the
source data structurally lacks the information. For example, Orange County
calendar-list PDFs contain only motion titles with no ruling text, so the
LLM correctly returns no outcome.

Configure these in the `expected_null_rates` section of
`data-quality-baselines.json`:

```json
{
  "expected_null_rates": {
    "Orange": {
      "outcome": 17.0,
      "_note": "Calendar-list PDFs have no ruling text."
    }
  }
}
```

When an expected null rate is configured for a county+field, the field
completeness check caps the effective baseline at `100 - expected_null_rate`.
This prevents false-positive alerts for known irreducible gaps while still
detecting genuine regressions below the expected floor.

**Current per-county ceilings (as of 2026-04-01):**

| County | Field | Expected null rate | Root cause |
|--------|-------|--------------------|------------|
| Orange | outcome | 17% | Calendar-list PDFs (motion titles only, no ruling text) |
| Orange | motion_type | 15% | Calendar-list PDFs (same root cause) |
| Santa Clara | outcome | 2% | Cross-reference entries ("See Line N") |
| Santa Clara | motion_type | 10% | Cross-reference entries |

See investigation #2304 for detailed findings.

---

## Running Manually

### From GitHub Actions
Navigate to Actions > Data Quality Check > Run workflow. Optionally specify a
county name or enable text output.

### From the command line
```bash
# Run via ECS (recommended -- uses the same path as the scheduled check)
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
- Review baselines in `data-quality-baselines.json` -- expected values may need updating.
- Run manually with `--text` for human-readable output.
- Check if the database has recent data: `scripts/dev-db-query.sh "SELECT county, COUNT(*) FROM documents d JOIN courts ct ON ct.id = d.court_id WHERE d.created_at > NOW() - INTERVAL '24 hours' GROUP BY county"`.

### Dashboard shows unexpected values
- Check the `data_quality_metrics` table for the most recent records.
- Verify the hourly workflow is still running in the Actions tab.
- Run the check manually with `--text` to compare with dashboard values.

### Workflow not running on schedule
- GitHub Actions scheduled workflows may be disabled after 60 days of repo inactivity.
- Check the Actions tab for the workflow status.
- Trigger manually to verify it still works.
