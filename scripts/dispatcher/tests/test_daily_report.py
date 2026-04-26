"""Unit tests for scripts/dispatcher/daily_report.py (issue #3375).

These tests are fixture-driven — no live database connection required.
They exercise the render_markdown function and assert structural
properties of the generated markdown.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``scripts`` importable without installing the repo as a package.
# parents[2] from scripts/dispatcher/tests/ is scripts/.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_full_stats() -> dict:
    """Return a representative stats dict with realistic data."""
    return {
        "throughput": {
            "started_24h": 42,
            "shipped_24h": 38,
            "terminated_24h": 4,
            "running_now": 3,
        },
        "green_streak": 12,
        "failure_breakdown": [
            {
                "phase": "implement",
                "category": "timeout",
                "failure_count": 15,
                "latest_agent_id": "aaaabbbb-cccc-dddd-eeee-ffff00001111",
            },
            {
                "phase": "plan",
                "category": "rate_limit_529",
                "failure_count": 8,
                "latest_agent_id": "bbbbcccc-dddd-eeee-ffff-0000aaaa2222",
            },
            {
                "phase": "verify",
                "category": "stuck_timeout",
                "failure_count": 3,
                "latest_agent_id": "ccccdddd-eeee-ffff-0000-aaaa11112222",
            },
            {
                "phase": "summarize",
                "category": "cwd_drift",
                "failure_count": 1,
                "latest_agent_id": None,
            },
        ],
        "diagnoser_activity": [
            {
                "recommended_action": "retry",
                "next_directive": "retry_immediately",
                "agent_outcome": "shipped",
                "count": 5,
            },
            {
                "recommended_action": "escalate",
                "next_directive": "needs_human",
                "agent_outcome": "terminated",
                "count": 2,
            },
        ],
        "stuck_issues": [
            {"issue_number": 123, "category": "timeout", "failure_count": 5},
            {"issue_number": 456, "category": "rate_limit_529", "failure_count": 3},
        ],
        "phase4_health": {
            "daemon_starts_24h": 2,
        },
    }


def _make_empty_stats() -> dict:
    """Return a stats dict with zero activity (all counts zero, all lists empty)."""
    return {
        "throughput": {
            "started_24h": 0,
            "shipped_24h": 0,
            "terminated_24h": 0,
            "running_now": 0,
        },
        "green_streak": 0,
        "failure_breakdown": [],
        "diagnoser_activity": [],
        "stuck_issues": [],
        "phase4_health": {
            "daemon_starts_24h": 0,
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_render_markdown_golden() -> None:
    """Feed a realistic stats dict and assert key structural properties."""
    # Import here so the test fails clearly if the module is missing
    from dispatcher.daily_report import render_markdown  # type: ignore[import]  # noqa: E402

    stats = _make_full_stats()
    output = render_markdown(stats, "2026-04-25")

    # Report must open with the canonical heading
    assert output.startswith("# Dispatcher daily report — 2026-04-25"), (
        f"Expected header not found. Got: {output[:80]!r}"
    )

    # Top-3 callout section must be present
    assert "## Top-3 failure categories" in output

    # The three highest-count rows must appear bold (markdown **...**)
    assert "**1. implement / timeout — 15**" in output
    assert "**2. plan / rate_limit_529 — 8**" in output
    assert "**3. verify / stuck_timeout — 3**" in output

    # Throughput section
    assert "## Throughput" in output
    assert "Started: 42" in output
    assert "Shipped: 38" in output
    assert "Green streak: 12" in output

    # Full failure breakdown section
    assert "## Failure breakdown" in output

    # Diagnoser section
    assert "## Diagnoser activity" in output

    # Stuck issues section
    assert "## Stuck issues" in output
    assert "#123" in output

    # Phase-4 health section
    assert "## Phase-4 health" in output
    assert "Daemon starts (last 24h): 2" in output

    # CloudWatch placeholder must be present (not filled in)
    assert "CloudWatch" in output


def test_render_handles_zero_activity() -> None:
    """Empty/zero stats must produce valid markdown without crashing."""
    from dispatcher.daily_report import render_markdown  # type: ignore[import]  # noqa: E402

    stats = _make_empty_stats()
    # Must not raise (no division by zero, no NaN, no AttributeError)
    output = render_markdown(stats, "2026-04-25")

    # Still must start with the canonical heading
    assert output.startswith("# Dispatcher daily report — 2026-04-25")

    # Top-3 callout with no data — must say "no failures"
    assert "No failures in the last 24h" in output

    # Throughput values are all zero
    assert "Started: 0" in output
    assert "Shipped: 0" in output

    # No exceptions = test passes (zero-activity case is valid)


def test_spec_says_daily() -> None:
    """docs/specs/dispatcher-v2-spec.md must reference Daily summary, not Weekly."""
    spec_path = (
        Path(__file__).resolve().parents[3] / "docs" / "specs" / "dispatcher-v2-spec.md"
    )
    assert spec_path.is_file(), f"spec file not found at {spec_path}"

    text = spec_path.read_text(encoding="utf-8")

    assert "Daily summary" in text, (
        "dispatcher-v2-spec.md must contain 'Daily summary' (issue #3375 updated §7)"
    )
    assert "Weekly summary" not in text, (
        "dispatcher-v2-spec.md must not contain 'Weekly summary' "
        "(should have been replaced with 'Daily summary' in issue #3375)"
    )
