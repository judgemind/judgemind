#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Zero-record streak check runner — ECS entrypoint for issue lifecycle management.

Runs the zero-record streak check (see ``check-scraper-zero-record-streak.py``),
then manages a GitHub issue to track any breach:

* **Breach + no open issue** → create issue with labels
  ``priority/p1,area/scraping,scraper-zero-record-alert,agent/ready`` and send
  a Telegram notification via ``scripts/notify-telegram.sh``.
* **Breach + open issue** → post a "still in breach" comment.
* **Healthy + open issue** → post a close comment and close the issue.
* **Healthy + no open issue** → no-op.

GitHub is accessed via ``urllib`` (no new dependencies). The ``GITHUB_TOKEN``
environment variable must be set (injected from Secrets Manager by the ECS
task definition). ``DATABASE_URL`` is also required for the streak query.

Telegram notification is best-effort: the script shells out to
``scripts/notify-telegram.sh`` using a message file written to the worktree
``tmp/`` directory. A non-zero exit from the shell script is logged but does
NOT abort the run.

Environment variables:
    DATABASE_URL    Postgres connection string (required).
    GITHUB_TOKEN    GitHub PAT with issues:write (required).
    GH_REPO         GitHub repo slug (default: judgemind/judgemind).
    AWS_REGION      AWS region for Secrets Manager (default: us-west-2).

Exit codes:
    0   Healthy — no breach, or breach already tracked.
    1   Breach detected (new or continuing).
    2   Missing required environment variable.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALERT_LABEL = "scraper-zero-record-alert"
ISSUE_LABELS = "priority/p1,area/scraping,scraper-zero-record-alert,agent/ready"
ISSUE_TITLE = "Scraper zero-record streak: silent outage detected"
GITHUB_API_BASE = "https://api.github.com"

# ---------------------------------------------------------------------------
# Import streak check
# ---------------------------------------------------------------------------

# The streak module lives in scripts/ which uses a hyphenated filename.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

zrs = import_module("check-scraper-zero-record-streak")

# ---------------------------------------------------------------------------
# GitHub helpers (urllib only — no new deps)
# ---------------------------------------------------------------------------


def _gh_request(
    method: str,
    path: str,
    token: str,
    payload: dict | None = None,
) -> dict:
    """Execute a GitHub REST API request and return the parsed JSON body."""
    url = f"{GITHUB_API_BASE}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(
            f"GitHub API {method} {url} → HTTP {exc.code}: {body}"
        ) from exc


def list_open_alert_issues(repo: str, token: str) -> list[dict]:
    """Return open issues tagged with the zero-record alert label."""
    path = f"/repos/{repo}/issues?labels={ALERT_LABEL}&state=open&per_page=10"
    return _gh_request("GET", path, token)


def create_issue(repo: str, token: str, body: str) -> dict:
    """Create a new issue and return the created issue object."""
    return _gh_request(
        "POST",
        f"/repos/{repo}/issues",
        token,
        payload={
            "title": ISSUE_TITLE,
            "body": body,
            "labels": ISSUE_LABELS.split(","),
        },
    )


def add_comment(repo: str, token: str, issue_number: int, body: str) -> None:
    """Add a comment to an existing issue."""
    _gh_request(
        "POST",
        f"/repos/{repo}/issues/{issue_number}/comments",
        token,
        payload={"body": body},
    )


def close_issue(repo: str, token: str, issue_number: int, comment: str) -> None:
    """Post a close comment then close the issue."""
    add_comment(repo, token, issue_number, comment)
    _gh_request(
        "PATCH",
        f"/repos/{repo}/issues/{issue_number}",
        token,
        payload={"state": "closed", "state_reason": "completed"},
    )


# ---------------------------------------------------------------------------
# Body builders
# ---------------------------------------------------------------------------


def _breach_table(breaches: list) -> str:
    lines = [
        "| Scraper | Consecutive zeros | Threshold | First zero at | Last nonzero at |",
        "| --- | --- | --- | --- | --- |",
    ]
    for b in breaches:
        lines.append(
            "| {sid} | {streak} | {threshold} | {first} | {last_nonzero} |".format(
                sid=b.scraper_id,
                streak=b.streak,
                threshold=b.threshold,
                first=b.first_zero_at,
                last_nonzero=b.last_nonzero_at or "(none in window)",
            )
        )
    return "\n".join(lines)


def _new_issue_body(result: zrs.CheckResult) -> str:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    table = _breach_table(result.breaches)
    return (
        "## Scraper Zero-Record Streak Alert\n\n"
        "One or more scrapers have returned `records_captured = 0` for\n"
        "multiple consecutive runs. This is the silent-outage failure\n"
        "mode described by #2620 / #2666 — the scraper may technically\n"
        "report success but is not producing data.\n\n"
        "### Breaches\n\n"
        f"{table}\n\n"
        "### Context\n\n"
        f"- **Timestamp:** {timestamp}\n"
        "- **Check script:** `scripts/check-scraper-zero-record-streak.py`\n\n"
        "### Next Steps\n\n"
        "1. Review the affected scraper's ECS logs:\n"
        "   `scripts/ecs-logs.sh /ecs/judgemind-scraper-<id>-dev --lines 200`\n"
        "2. Visit the source website manually — a zero-record streak\n"
        "   caused by a court actually having no rulings is still rare\n"
        "   but possible during extended closures.\n"
        "3. If the source has data but the scraper missed it, file a\n"
        "   `priority/p1` bug against the scraper.\n"
        "4. If the streak is an expected calm period, add the scraper\n"
        "   to the `EXCLUSIONS` set in the check script and re-run.\n\n"
        "This issue was created automatically by the zero-record streak check."
        " It will not create duplicates while this issue is open."
    )


def _update_comment_body(result: zrs.CheckResult) -> str:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    table = _breach_table(result.breaches)
    return f"### Update: still in breach at {timestamp}\n\n{table}"


def _close_comment_body() -> str:
    return "No scrapers are currently in a zero-record streak breach. Auto-closing."


# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------


def send_telegram(result: zrs.CheckResult, issue_number: int, issue_url: str) -> None:
    """Best-effort Telegram notification via scripts/notify-telegram.sh."""
    parts = [
        f"{b.scraper_id} ({b.streak} consecutive zero-record runs)"
        for b in result.breaches
    ]
    summary = "; ".join(parts) if parts else "Unknown alert"
    message = (
        "<b>Scraper Zero-Record Alert</b>\n\n"
        f"{summary}\n\n"
        f"Issue #{issue_number}: {issue_url}"
    )

    # Write message to a temp file so we avoid shell quoting issues.
    msg_file = Path(_REPO_ROOT) / "tmp" / "zrc_tg_message.txt"
    msg_file.parent.mkdir(parents=True, exist_ok=True)
    msg_file.write_text(message)

    notify_script = Path(_REPO_ROOT) / "scripts" / "notify-telegram.sh"
    try:
        result_proc = subprocess.run(
            [str(notify_script), "--message-file", str(msg_file)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result_proc.returncode == 0:
            logger.info("Telegram notification sent")
        else:
            logger.warning(
                "notify-telegram.sh exited %d: %s",
                result_proc.returncode,
                result_proc.stderr.strip(),
            )
    except Exception as exc:
        logger.warning("Failed to send Telegram notification: %s", exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    gh_repo = os.environ.get("GH_REPO", "judgemind/judgemind").strip()

    if not database_url:
        logger.error(
            "DATABASE_URL is not set. Run via scripts/ecs-run-task.sh or "
            "the ECS task so the connection string is injected."
        )
        return 2

    if not github_token:
        logger.error(
            "GITHUB_TOKEN is not set. The ECS task definition must inject "
            "it from Secrets Manager."
        )
        return 2

    # Run the streak check.
    import psycopg

    with psycopg.connect(database_url, autocommit=True) as conn:
        result = zrs.check_zero_record_streaks(conn, now=datetime.now(UTC))

    logger.info(
        "Streak check complete: %d checked, %d excluded, %d breach(es), %d insufficient-history",
        result.checked_count,
        result.excluded_count,
        len(result.breaches),
        len(result.insufficient_history),
    )

    if result.breaches:
        logger.warning(
            "Breaches: %s",
            ", ".join(b.scraper_id for b in result.breaches),
        )

    # Fetch open alert issues.
    open_issues = list_open_alert_issues(gh_repo, github_token)

    if result.breaches:
        if not open_issues:
            # Create a new issue.
            body = _new_issue_body(result)
            issue = create_issue(gh_repo, github_token, body)
            issue_number = issue["number"]
            issue_url = issue["html_url"]
            logger.info("Created issue #%d: %s", issue_number, issue_url)
            # Telegram only on create — avoids repeat pings for known outages.
            send_telegram(result, issue_number, issue_url)
        else:
            # Update existing issue with a comment.
            issue_number = open_issues[0]["number"]
            comment_body = _update_comment_body(result)
            add_comment(gh_repo, github_token, issue_number, comment_body)
            logger.info("Updated issue #%d with breach update", issue_number)

        return 1
    else:
        # Healthy — close any open alert issues.
        if open_issues:
            close_body = _close_comment_body()
            for issue in open_issues:
                issue_number = issue["number"]
                close_issue(gh_repo, github_token, issue_number, close_body)
                logger.info("Closed issue #%d — no active breach", issue_number)
        else:
            logger.info("Healthy — no open zero-record alert issues to close")

        return 0


if __name__ == "__main__":
    raise SystemExit(main())
