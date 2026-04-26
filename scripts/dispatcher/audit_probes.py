#!/usr/bin/env python3
"""Dispatcher operational-health probes — /dispatcher-audit skill backend.

Runs three pure-ish probe functions against supplied query results and
returns structured ``Finding`` instances. A top-level ``main()`` queries
the live database and HTTP endpoints, then prints findings as JSON.

Usage::

    scripts/dispatcher/audit_probes.py --output /tmp/findings.json

The script reads ``DATABASE_URL`` from the environment. If ``DATABASE_URL``
is not set it exits non-zero so the scheduler records the failure.

Exit codes:
    0   Probes completed; JSON written (may contain findings or be empty).
    1   DATABASE_URL missing, DB connection failed, or query error.
"""
# venv: scraper-framework
# permanent: true

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Finding dataclass
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """A single probe finding that may warrant filing a GitHub issue."""

    probe: str
    title: str
    body: str
    severity: str  # "info" | "warning" | "critical"
    should_file_issue: bool
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# SQL queries (used by main(); not required by probe functions themselves)
# ---------------------------------------------------------------------------

# Recent phase_transitions window: last 6 hours for "recent", 7-24 hours
# for "baseline". The probe compares p95 ralph-iteration counts between the
# two windows.
_SQL_PHASE_TRANSITIONS_RECENT = """
SELECT
    agent_id,
    phase
FROM dispatcher.phase_transitions
WHERE ts >= now() - INTERVAL '6 hours'
  AND phase = 'ralph'
ORDER BY ts DESC;
"""

_SQL_PHASE_TRANSITIONS_BASELINE = """
SELECT
    agent_id,
    phase
FROM dispatcher.phase_transitions
WHERE ts >= now() - INTERVAL '24 hours'
  AND ts < now() - INTERVAL '6 hours'
  AND phase = 'ralph'
ORDER BY ts DESC;
"""

# Last 10 terminal outcomes ordered newest-first (descending ended_at).
# The streak probe counts consecutive non-shipped at the head.
_SQL_TERMINAL_OUTCOMES = """
SELECT
    outcome_id,
    agent_id,
    issue_number,
    status,
    ended_at
FROM dispatcher.terminal_outcomes
ORDER BY ended_at DESC
LIMIT 10;
"""

# Derived document count as a cheap liveness check.
_SQL_DOCUMENT_COUNT = """
SELECT COUNT(*) FROM derived.documents;
"""

# Circuit-breaker threshold (default 5 per migration 29). The streak probe
# fires at threshold - 2 so the operator gets early warning. We default to
# 3 if the config value is unavailable.
_SQL_CB_THRESHOLD = """
SELECT value FROM dispatcher.config WHERE key = 'circuit_breaker_bad_outcome_threshold';
"""

# Bad outcomes: mirror the daemon's BAD_OUTCOME_STATUSES constant.
_BAD_OUTCOMES = frozenset(
    {"failed", "terminated", "crashed", "plan_blocked", "needs_review", "needs_human"}
)

# Streak threshold: flag when the head-of-queue streak is this many bad.
_STREAK_THRESHOLD = 3

# Convergence p95 multiplier: flag when recent p95 > this * baseline p95.
_P95_JUMP_FACTOR = 2.0


# ---------------------------------------------------------------------------
# Probe 1 — convergence regression
# ---------------------------------------------------------------------------


def _percentile(values: list[int], p: float) -> float:
    """Compute the p-th percentile (0–100) of *values*. Returns 0 for empty."""
    if not values:  # pragma: no cover — callers guard on empty before calling
        return 0.0
    sorted_vals = sorted(values)
    idx = (p / 100) * (len(sorted_vals) - 1)
    lower = int(idx)
    upper = min(lower + 1, len(sorted_vals) - 1)
    frac = idx - lower
    return sorted_vals[lower] * (1 - frac) + sorted_vals[upper] * frac


def _count_ralph_iterations(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Count the number of 'ralph' phase rows per agent_id."""
    counts: dict[str, int] = {}
    for row in rows:
        agent_id = str(row.get("agent_id") or "")
        if not agent_id:
            continue
        if row.get("phase") == "ralph":
            counts[agent_id] = counts.get(agent_id, 0) + 1
    return counts


def probe_convergence_regression(
    phase_transition_rows: list[dict[str, Any]],
) -> list[Finding]:
    """Flag when recent p95 ralph-iteration count jumps > 2x the baseline p95.

    *phase_transition_rows* is the combined list of recent + baseline rows.
    Each row is a dict with at minimum ``agent_id`` and ``phase`` keys.

    The function splits the rows at the median index to simulate a
    baseline/recent split when no ``window`` key is present. In
    production ``main()`` passes two separate SQL result sets joined with
    a ``window`` key (``"recent"`` or ``"baseline"``).
    """
    if not phase_transition_rows:
        return []

    # Partition into recent vs baseline by optional "window" key.
    # When called from main(), rows carry window="recent" or window="baseline".
    # When called from tests, the rows are split by position (first half=baseline,
    # second half=recent) to keep the interface simple for callers.
    recent_rows = [r for r in phase_transition_rows if r.get("window") == "recent"]
    baseline_rows = [r for r in phase_transition_rows if r.get("window") == "baseline"]

    if not recent_rows and not baseline_rows:
        # No window key — treat first half as baseline, second half as recent.
        mid = len(phase_transition_rows) // 2
        baseline_rows = phase_transition_rows[:mid]
        recent_rows = phase_transition_rows[mid:]

    baseline_counts = list(_count_ralph_iterations(baseline_rows).values())
    recent_counts = list(_count_ralph_iterations(recent_rows).values())

    if not baseline_counts or not recent_counts:
        return []

    baseline_p95 = _percentile(baseline_counts, 95)
    recent_p95 = _percentile(recent_counts, 95)

    if baseline_p95 <= 0:  # pragma: no cover — counts are always >= 1 for ralph rows
        return []

    ratio = recent_p95 / baseline_p95
    if ratio <= _P95_JUMP_FACTOR:
        return []

    return [
        Finding(
            probe="convergence_regression",
            title=f"Ralph iteration p95 regression: {recent_p95:.1f} vs baseline {baseline_p95:.1f} ({ratio:.1f}x)",
            body=(
                f"The p95 ralph-iteration count over the recent 6-hour window is "
                f"{recent_p95:.1f}, which is {ratio:.1f}x the baseline window p95 "
                f"of {baseline_p95:.1f}. This may indicate tasks are requiring "
                f"more review cycles, possibly due to a code-quality regression, "
                f"a flaky reviewer, or an LLM behavior change.\n\n"
                f"**Probe:** convergence_regression  \n"
                f"**Issue:** #2865"
            ),
            severity="warning",
            should_file_issue=True,
            details={
                "baseline_p95": baseline_p95,
                "recent_p95": recent_p95,
                "ratio": ratio,
                "baseline_agent_count": len(baseline_counts),
                "recent_agent_count": len(recent_counts),
            },
        )
    ]


# ---------------------------------------------------------------------------
# Probe 2 — bad-outcome streak
# ---------------------------------------------------------------------------


def probe_bad_outcome_streak(
    terminal_outcome_rows: list[dict[str, Any]],
) -> list[Finding]:
    """Flag when the newest consecutive non-shipped outcomes reach >= 3.

    *terminal_outcome_rows* must be ordered newest-first (``ended_at DESC``).
    Each row is a dict with at minimum a ``status`` key.

    The streak threshold is 3 — below the circuit-breaker default of 5
    (migration 29) so the operator gets early warning. Rows with status
    ``'shipped'`` break the streak.
    """
    streak = 0
    for row in terminal_outcome_rows:
        status = str(row.get("status") or "")
        if status == "shipped":
            break
        streak += 1

    if streak < _STREAK_THRESHOLD:
        return []

    return [
        Finding(
            probe="bad_outcome_streak",
            title=f"Bad-outcome streak: {streak} consecutive non-shipped terminals",
            body=(
                f"The last {streak} completed agents all ended in a non-shipped "
                f"state (failed, crashed, terminated, etc.). The circuit breaker "
                f"trips at 5 bad outcomes; this probe fires at {_STREAK_THRESHOLD} "
                f"to give early warning.\n\n"
                f"Investigate recent failures in dispatcher.terminal_outcomes. "
                f"Check the diagnoser activity and the failure breakdown in the "
                f"daily report for context.\n\n"
                f"**Probe:** bad_outcome_streak  \n"
                f"**Issue:** #2865"
            ),
            severity="warning",
            should_file_issue=True,
            details={
                "streak": streak,
                "threshold": _STREAK_THRESHOLD,
            },
        )
    ]


# ---------------------------------------------------------------------------
# Probe 3 — site health
# ---------------------------------------------------------------------------

_SITE_ENDPOINTS = [
    "https://dev.judgemind.org/",
    "https://dev.api.judgemind.org/graphql",
]


def probe_site_health(
    probe_results: dict[str, tuple[int, str]],
) -> list[Finding]:
    """Flag any non-200 HTTP response or a zero document count from the DB.

    *probe_results* is a dict mapping endpoint name (or ``"db_document_count"``)
    to ``(status_code, body)``. Status code 0 indicates a connection error.
    A ``db_document_count`` entry with body ``"0"`` or body parseable as 0
    is flagged as a database liveness concern.
    """
    findings: list[Finding] = []

    for endpoint, (status, body) in probe_results.items():
        if endpoint == "db_document_count":
            # Check for zero or error result
            try:
                count = int(body.strip())
            except (ValueError, AttributeError):
                count = -1

            if count == 0:
                findings.append(
                    Finding(
                        probe="site_health",
                        title="DB document count is zero — possible data loss or pipeline stall",
                        body=(
                            "A spot-check query against ``derived.documents`` returned 0 rows. "
                            "This may indicate the ingestion pipeline has stalled, the table "
                            "was accidentally truncated, or the database connection is broken.\n\n"
                            "**Probe:** site_health (db_document_count)  \n"
                            "**Issue:** #2865"
                        ),
                        severity="critical",
                        should_file_issue=True,
                        details={"count": count},
                    )
                )
            elif count < 0 and status != 200:
                findings.append(
                    Finding(
                        probe="site_health",
                        title="DB document-count probe failed",
                        body=(
                            f"The derived.documents count query returned an unexpected "
                            f"result (status={status}, body={body!r:.80}).\n\n"
                            f"**Probe:** site_health (db_document_count)  \n"
                            f"**Issue:** #2865"
                        ),
                        severity="warning",
                        should_file_issue=True,
                        details={"status": status, "body_snippet": body[:200]},
                    )
                )
            continue

        # HTTP endpoint check
        if status != 200:
            label = "GraphQL API" if "graphql" in endpoint.lower() else "site"
            findings.append(
                Finding(
                    probe="site_health",
                    title=f"{label} endpoint unhealthy: {endpoint} returned HTTP {status}",
                    body=(
                        f"The {label} endpoint ``{endpoint}`` returned HTTP {status}. "
                        f"Expected 200.\n\n"
                        f"Body snippet: ``{body[:200]}``\n\n"
                        f"**Probe:** site_health  \n"
                        f"**Issue:** #2865"
                    ),
                    severity="warning" if status >= 500 else "critical",
                    should_file_issue=True,
                    details={
                        "endpoint": endpoint,
                        "status": status,
                        "body_snippet": body[:200],
                    },
                )
            )

    return findings


# ---------------------------------------------------------------------------
# HTTP + DB helpers for main()
# ---------------------------------------------------------------------------


def _http_get(
    url: str, timeout: int = 15
) -> tuple[int, str]:  # pragma: no cover — live HTTP
    """GET *url*; return (status_code, body). Returns (0, error_str) on failure."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "JudgemindDispatcherAudit/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, str(exc)
    except Exception as exc:
        return 0, str(exc)


def _graphql_post(
    api_url: str, timeout: int = 15
) -> tuple[int, str]:  # pragma: no cover — live HTTP
    """POST a trivial introspection query to *api_url*; return (status, body)."""
    payload = json.dumps({"query": "{ __typename }"}).encode("utf-8")
    try:
        req = urllib.request.Request(
            api_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "JudgemindDispatcherAudit/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, str(exc)
    except Exception as exc:
        return 0, str(exc)


def _fetch_all_probe_data(conn: Any) -> dict[str, Any]:  # pragma: no cover — live DB
    """Run all DB queries and HTTP probes; return raw data dicts."""
    data: dict[str, Any] = {}

    with conn.cursor() as cur:
        # Phase transitions — recent window (last 6h)
        cur.execute(_SQL_PHASE_TRANSITIONS_RECENT)
        cols = [d[0] for d in cur.description]
        data["phase_transitions_recent"] = [
            dict(zip(cols, row)) | {"window": "recent"} for row in cur.fetchall()
        ]

        # Phase transitions — baseline window (6h–24h ago)
        cur.execute(_SQL_PHASE_TRANSITIONS_BASELINE)
        cols = [d[0] for d in cur.description]
        data["phase_transitions_baseline"] = [
            dict(zip(cols, row)) | {"window": "baseline"} for row in cur.fetchall()
        ]

        # Terminal outcomes
        cur.execute(_SQL_TERMINAL_OUTCOMES)
        cols = [d[0] for d in cur.description]
        data["terminal_outcomes"] = [dict(zip(cols, row)) for row in cur.fetchall()]

        # Document count
        cur.execute(_SQL_DOCUMENT_COUNT)
        row = cur.fetchone()
        data["document_count"] = row[0] if row else 0

    # HTTP probes
    data["http_frontend"] = _http_get("https://dev.judgemind.org/")
    data["http_graphql"] = _graphql_post("https://dev.api.judgemind.org/graphql")

    return data


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args(
    argv: list[str],
) -> argparse.Namespace:  # pragma: no cover — only called from main()
    parser = argparse.ArgumentParser(
        description="Run dispatcher operational-health probes and output findings as JSON."
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path to write JSON findings. Defaults to stdout.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover — live DB+HTTP
    """CLI entry point. Returns 0 on success, 1 on error."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        sys.stderr.write("audit_probes: DATABASE_URL not set\n")
        return 1

    try:
        import psycopg  # noqa: PLC0415 — lazy import; psycopg3

        with psycopg.connect(
            database_url,
            connect_timeout=10,
            options="-c statement_timeout=30000",
        ) as conn:
            probe_data = _fetch_all_probe_data(conn)
    except Exception as exc:
        sys.stderr.write(f"audit_probes: DB error: {exc}\n")
        return 1

    # Run all probes
    all_rows = probe_data.get("phase_transitions_recent", []) + probe_data.get(
        "phase_transitions_baseline", []
    )
    findings: list[Finding] = []
    findings.extend(probe_convergence_regression(all_rows))
    findings.extend(probe_bad_outcome_streak(probe_data.get("terminal_outcomes", [])))

    doc_count = probe_data.get("document_count", 0)
    http_frontend_status, http_frontend_body = probe_data.get("http_frontend", (0, ""))
    http_graphql_status, http_graphql_body = probe_data.get("http_graphql", (0, ""))
    site_probe_results = {
        "https://dev.judgemind.org/": (http_frontend_status, http_frontend_body),
        "https://dev.api.judgemind.org/graphql": (
            http_graphql_status,
            http_graphql_body,
        ),
        "db_document_count": (200, str(doc_count)),
    }
    findings.extend(probe_site_health(site_probe_results))

    output = json.dumps([asdict(f) for f in findings], indent=2)

    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(output)
        except OSError as exc:
            sys.stderr.write(f"audit_probes: write error: {exc}\n")
            return 1
    else:
        sys.stdout.write(output)
        sys.stdout.write("\n")

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
