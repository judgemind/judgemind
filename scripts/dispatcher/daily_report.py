#!/usr/bin/env python3
"""Generate the daily dispatcher health report from DB aggregations.

Connects to the dispatcher database, runs five SQL aggregations over the
last-24h window, and renders a phone-readable markdown report. Writes the
output to --output path (or stdout if omitted).

Usage::

    scripts/dispatcher/daily_report.py --output docs/dispatcher-daily/2026-04-25.md

The script reads DATABASE_URL from the environment. If DATABASE_URL is not
set, it exits non-zero so the scheduler records the failure.

Exit codes:
    0   Report written successfully.
    1   DATABASE_URL missing, DB connection failed, or query error.
"""
# venv: scraper-framework
# permanent: true

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from typing import Any


# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

_SQL_THROUGHPUT = """
SELECT
    COUNT(*) FILTER (WHERE started_at >= now() - INTERVAL '24 hours')
        AS started_24h,
    COUNT(*) FILTER (
        WHERE ended_at >= now() - INTERVAL '24 hours'
        AND status = 'terminated'
    ) AS terminated_24h,
    COUNT(*) FILTER (
        WHERE ended_at >= now() - INTERVAL '24 hours'
        AND status = 'shipped'
    ) AS shipped_24h,
    COUNT(*) FILTER (WHERE status = 'running') AS running_now
FROM dispatcher.agents;
"""

# Strict green streak: count consecutive shipped agents (ordered by ended_at
# descending) until the first non-shipped row. Uses a window function to
# assign a row_number; we count rows where the status at that position equals
# 'shipped' AND no prior row (by row_number) was non-shipped.
_SQL_GREEN_STREAK = """
WITH ordered AS (
    SELECT
        status,
        ROW_NUMBER() OVER (ORDER BY ended_at DESC NULLS LAST) AS rn
    FROM dispatcher.agents
    WHERE ended_at IS NOT NULL
),
streak AS (
    SELECT rn
    FROM ordered
    WHERE rn <= (
        SELECT COALESCE(MIN(rn) - 1, (SELECT MAX(rn) FROM ordered))
        FROM ordered
        WHERE status != 'shipped'
    )
    AND status = 'shipped'
)
SELECT COUNT(*) AS green_streak FROM streak;
"""

_SQL_FAILURE_BREAKDOWN = """
SELECT
    phase,
    category,
    COUNT(*) AS failure_count,
    (ARRAY_AGG(agent_id ORDER BY ts DESC))[1] AS latest_agent_id
FROM dispatcher.failures
WHERE ts >= now() - INTERVAL '24 hours'
GROUP BY phase, category
ORDER BY failure_count DESC, phase, category;
"""

_SQL_DIAGNOSER_ACTIVITY = """
SELECT
    d.recommendation->>'action' AS recommended_action,
    d.next_directive,
    a.status AS agent_outcome,
    COUNT(*) AS count
FROM dispatcher.diagnoses d
JOIN dispatcher.agents a USING (agent_id)
WHERE d.created_at >= now() - INTERVAL '24 hours'
GROUP BY
    d.recommendation->>'action',
    d.next_directive,
    a.status
ORDER BY count DESC;
"""

_SQL_STUCK_ISSUES = """
SELECT
    a.issue_number,
    f.category,
    COUNT(*) AS failure_count
FROM dispatcher.agents a
JOIN dispatcher.failures f USING (agent_id)
WHERE f.ts >= now() - INTERVAL '24 hours'
  AND a.issue_number IS NOT NULL
GROUP BY a.issue_number, f.category
HAVING COUNT(*) >= 3
ORDER BY failure_count DESC, a.issue_number;
"""

_SQL_PHASE4_HEALTH = """
SELECT COUNT(*) AS daemon_starts_24h
FROM dispatcher.runs
WHERE started_at >= now() - INTERVAL '24 hours';
"""


# ---------------------------------------------------------------------------
# Data classes (plain dicts rather than dataclasses — no extra deps)
# ---------------------------------------------------------------------------


def _fetch_stats(conn: Any) -> dict[str, Any]:
    """Run all five queries and return a stats dict."""
    stats: dict[str, Any] = {}

    with conn.cursor() as cur:
        # Throughput
        cur.execute(_SQL_THROUGHPUT)
        row = cur.fetchone()
        stats["throughput"] = {
            "started_24h": row[0] if row else 0,
            "terminated_24h": row[1] if row else 0,
            "shipped_24h": row[2] if row else 0,
            "running_now": row[3] if row else 0,
        }

        # Green streak
        cur.execute(_SQL_GREEN_STREAK)
        row = cur.fetchone()
        stats["green_streak"] = row[0] if row else 0

        # Failure breakdown
        cur.execute(_SQL_FAILURE_BREAKDOWN)
        rows = cur.fetchall()
        stats["failure_breakdown"] = [
            {
                "phase": r[0],
                "category": r[1],
                "failure_count": r[2],
                "latest_agent_id": str(r[3]) if r[3] else None,
            }
            for r in rows
        ]

        # Diagnoser activity
        cur.execute(_SQL_DIAGNOSER_ACTIVITY)
        rows = cur.fetchall()
        stats["diagnoser_activity"] = [
            {
                "recommended_action": r[0],
                "next_directive": r[1],
                "agent_outcome": r[2],
                "count": r[3],
            }
            for r in rows
        ]

        # Stuck issues
        cur.execute(_SQL_STUCK_ISSUES)
        rows = cur.fetchall()
        stats["stuck_issues"] = [
            {
                "issue_number": r[0],
                "category": r[1],
                "failure_count": r[2],
            }
            for r in rows
        ]

        # Phase-4 health
        cur.execute(_SQL_PHASE4_HEALTH)
        row = cur.fetchone()
        stats["phase4_health"] = {
            "daemon_starts_24h": row[0] if row else 0,
        }

    return stats


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def render_markdown(stats: dict[str, Any], report_date: str) -> str:
    """Render stats dict to phone-readable markdown."""
    lines: list[str] = []

    lines.append(f"# Dispatcher daily report — {report_date}")
    lines.append("")
    lines.append(f"*Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC*")
    lines.append("")

    # ------------------------------------------------------------------
    # Top-3 failure callout
    # ------------------------------------------------------------------
    breakdown = stats.get("failure_breakdown", [])
    lines.append("## Top-3 failure categories")
    lines.append("")
    if not breakdown:
        lines.append("*No failures in the last 24h.*")
    else:
        top3 = breakdown[:3]
        for i, row in enumerate(top3, 1):
            phase = row.get("phase") or "—"
            category = row.get("category") or "—"
            count = row.get("failure_count", 0)
            lines.append(f"**{i}. {phase} / {category} — {count}**")
        if len(breakdown) > 3:
            lines.append(f"*(+{len(breakdown) - 3} more categories below)*")
    lines.append("")

    # ------------------------------------------------------------------
    # Throughput
    # ------------------------------------------------------------------
    tp = stats.get("throughput", {})
    streak = stats.get("green_streak", 0)
    lines.append("## Throughput (last 24h)")
    lines.append("")
    lines.append(f"- Started: {tp.get('started_24h', 0)}")
    lines.append(f"- Shipped: {tp.get('shipped_24h', 0)}")
    lines.append(f"- Terminated: {tp.get('terminated_24h', 0)}")
    lines.append(f"- Running now: {tp.get('running_now', 0)}")
    lines.append(f"- Green streak: {streak} consecutive shipped")
    lines.append("")

    # ------------------------------------------------------------------
    # Full failure breakdown
    # ------------------------------------------------------------------
    lines.append("## Failure breakdown (last 24h)")
    lines.append("")
    if not breakdown:
        lines.append("*No failures.*")
    else:
        lines.append("| Phase | Category | Count | Latest agent |")
        lines.append("|-------|----------|-------|--------------|")
        for row in breakdown:
            phase = row.get("phase") or "—"
            category = row.get("category") or "—"
            count = row.get("failure_count", 0)
            agent_id = row.get("latest_agent_id") or "—"
            # Truncate UUID to 8 chars for readability on phone
            short_id = agent_id[:8] if agent_id and agent_id != "—" else "—"
            lines.append(f"| {phase} | {category} | {count} | {short_id} |")
    lines.append("")

    # ------------------------------------------------------------------
    # Diagnoser activity
    # ------------------------------------------------------------------
    diagnoser = stats.get("diagnoser_activity", [])
    lines.append("## Diagnoser activity (last 24h)")
    lines.append("")
    if not diagnoser:
        lines.append("*No diagnoser invocations.*")
    else:
        lines.append("| Recommended | Directive | Outcome | Count |")
        lines.append("|-------------|-----------|---------|-------|")
        for row in diagnoser:
            rec = row.get("recommended_action") or "—"
            directive = row.get("next_directive") or "—"
            outcome = row.get("agent_outcome") or "—"
            count = row.get("count", 0)
            lines.append(f"| {rec} | {directive} | {outcome} | {count} |")
    lines.append("")

    # ------------------------------------------------------------------
    # Stuck issues
    # ------------------------------------------------------------------
    stuck = stats.get("stuck_issues", [])
    lines.append("## Stuck issues (3+ same-category failures, last 24h)")
    lines.append("")
    if not stuck:
        lines.append("*No stuck issues.*")
    else:
        for row in stuck:
            issue = row.get("issue_number") or "?"
            category = row.get("category") or "—"
            count = row.get("failure_count", 0)
            lines.append(f"- **#{issue}** — {category} ({count} failures)")
    lines.append("")

    # ------------------------------------------------------------------
    # Phase-4 health
    # ------------------------------------------------------------------
    p4 = stats.get("phase4_health", {})
    daemon_starts = p4.get("daemon_starts_24h", 0)
    lines.append("## Phase-4 health")
    lines.append("")
    lines.append(f"- Daemon starts (last 24h): {daemon_starts}")
    lines.append("- tick_elapsed_s p50/p95: (CloudWatch — see follow-up)")
    lines.append("- scheduler_tick_stalled warns: (CloudWatch — see follow-up)")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the daily dispatcher health report."
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path to write the markdown report. Defaults to stdout.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on success, 1 on error."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        sys.stderr.write("daily_report: DATABASE_URL not set\n")
        return 1

    report_date = datetime.now(UTC).strftime("%Y-%m-%d")

    try:
        import psycopg  # noqa: PLC0415 — lazy import; psycopg3

        with psycopg.connect(
            database_url,
            connect_timeout=10,
            options="-c statement_timeout=30000",
        ) as conn:
            stats = _fetch_stats(conn)
    except Exception as exc:
        sys.stderr.write(f"daily_report: DB error: {exc}\n")
        return 1

    markdown = render_markdown(stats, report_date)

    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(markdown)
        except OSError as exc:
            sys.stderr.write(f"daily_report: write error: {exc}\n")
            return 1
    else:
        sys.stdout.write(markdown)

    return 0


if __name__ == "__main__":
    sys.exit(main())
