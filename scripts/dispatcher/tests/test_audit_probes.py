"""Unit tests for scripts/dispatcher/audit_probes.py (issue #2865).

These tests are fixture-driven — no live database connection required.
They exercise each probe function and assert finding-generation logic.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``scripts`` importable without installing the repo as a package.
# parents[2] from scripts/dispatcher/tests/ is scripts/.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher.audit_probes import (  # noqa: E402
    Finding,
    probe_bad_outcome_streak,
    probe_convergence_regression,
    probe_site_health,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_phase_transition_rows(
    agent_ids: list[str],
    ralph_counts: list[int],
) -> list[dict]:
    """Return synthetic phase_transition rows: agent_id + count of 'ralph' phases."""
    rows = []
    for agent_id, count in zip(agent_ids, ralph_counts):
        for _ in range(count):
            rows.append({"agent_id": agent_id, "phase": "ralph"})
    return rows


def _make_terminal_outcome_rows(statuses: list[str]) -> list[dict]:
    """Return synthetic terminal_outcome rows ordered newest-first (DESC)."""
    rows = []
    for i, status in enumerate(statuses):
        rows.append(
            {
                "outcome_id": i + 1,
                "agent_id": f"agent-{i:04d}",
                "status": status,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# probe_convergence_regression
# ---------------------------------------------------------------------------


def test_probe_convergence_regression_flags_p95_jump() -> None:
    """Stable baseline + recent spike -> one finding emitted."""
    # 10 baseline agents, each with 1 ralph iteration
    baseline_rows = _make_phase_transition_rows(
        [f"baseline-{i:03d}" for i in range(10)],
        [1] * 10,
    )
    # 5 recent agents: four normal (1 iteration) + one heavy spike (20 iterations)
    # This pushes the recent p95 well above 2x baseline p95 (which is 1).
    recent_rows = _make_phase_transition_rows(
        [f"recent-{i:03d}" for i in range(5)],
        [1, 1, 1, 1, 20],
    )
    all_rows = baseline_rows + recent_rows
    findings = probe_convergence_regression(all_rows)
    assert len(findings) == 1, f"Expected 1 finding, got {findings}"
    f = findings[0]
    assert isinstance(f, Finding)
    assert f.severity in ("warning", "critical")
    assert (
        "convergence" in f.title.lower()
        or "iteration" in f.title.lower()
        or "ralph" in f.title.lower()
    )
    assert f.should_file_issue is True


def test_probe_convergence_regression_quiet_when_normal() -> None:
    """Stable rows throughout should emit no findings."""
    # All agents have exactly 2 ralph iterations — perfectly uniform
    stable_rows = _make_phase_transition_rows(
        [f"agent-{i:03d}" for i in range(20)],
        [2] * 20,
    )
    findings = probe_convergence_regression(stable_rows)
    assert findings == [], f"Expected no findings, got {findings}"


# ---------------------------------------------------------------------------
# probe_bad_outcome_streak
# ---------------------------------------------------------------------------


def test_probe_bad_outcome_streak_at_threshold() -> None:
    """3 consecutive non-shipped outcomes triggers a finding (threshold=3)."""
    rows = _make_terminal_outcome_rows(["failed", "crashed", "terminated"])
    findings = probe_bad_outcome_streak(rows)
    assert len(findings) == 1, f"Expected 1 finding, got {findings}"
    f = findings[0]
    assert isinstance(f, Finding)
    assert f.should_file_issue is True
    assert "streak" in f.title.lower() or "outcome" in f.title.lower()


def test_probe_bad_outcome_streak_below_threshold_quiet() -> None:
    """A streak of 2 non-shipped outcomes emits no finding (threshold=3)."""
    rows = _make_terminal_outcome_rows(["failed", "crashed", "shipped"])
    findings = probe_bad_outcome_streak(rows)
    assert findings == [], f"Expected no findings for streak=2, got {findings}"


def test_probe_bad_outcome_streak_broken_by_shipped() -> None:
    """A bad streak broken by 'shipped' in the middle emits nothing."""
    # newest-first: bad, shipped, bad — streak is only 1 at head
    rows = _make_terminal_outcome_rows(["failed", "shipped", "failed"])
    findings = probe_bad_outcome_streak(rows)
    assert findings == [], f"Expected no findings when streak broken, got {findings}"


# ---------------------------------------------------------------------------
# probe_site_health
# ---------------------------------------------------------------------------


def test_probe_site_health_flags_non_200() -> None:
    """A non-200 response from any endpoint emits a finding."""
    probe_results = {
        "https://dev.judgemind.org/": (503, "Service Unavailable"),
        "https://dev.api.judgemind.org/graphql": (200, '{"data": {}}'),
        "db_document_count": (200, "12345"),
    }
    findings = probe_site_health(probe_results)
    assert len(findings) >= 1, f"Expected at least 1 finding, got {findings}"
    titles = [f.title for f in findings]
    assert any(
        "dev.judgemind.org" in t or "site" in t.lower() or "health" in t.lower()
        for t in titles
    ), f"Expected a finding about dev.judgemind.org, got titles: {titles}"


def test_probe_site_health_quiet_when_all_green() -> None:
    """All 200s and non-zero document count emits no findings."""
    probe_results = {
        "https://dev.judgemind.org/": (200, "<html>Judgemind</html>"),
        "https://dev.api.judgemind.org/graphql": (200, '{"data": {"health": "ok"}}'),
        "db_document_count": (200, "42000"),
    }
    findings = probe_site_health(probe_results)
    assert findings == [], f"Expected no findings when all green, got {findings}"


def test_probe_site_health_flags_zero_document_count() -> None:
    """A zero document count from the DB check emits a finding."""
    probe_results = {
        "https://dev.judgemind.org/": (200, "<html>Judgemind</html>"),
        "https://dev.api.judgemind.org/graphql": (200, '{"data": {}}'),
        "db_document_count": (200, "0"),
    }
    findings = probe_site_health(probe_results)
    assert len(findings) >= 1, (
        f"Expected at least 1 finding for zero docs, got {findings}"
    )


def test_probe_site_health_flags_graphql_non_200() -> None:
    """A non-200 GraphQL endpoint emits a finding."""
    probe_results = {
        "https://dev.judgemind.org/": (200, "<html>Judgemind</html>"),
        "https://dev.api.judgemind.org/graphql": (502, "Bad Gateway"),
        "db_document_count": (200, "5000"),
    }
    findings = probe_site_health(probe_results)
    assert len(findings) >= 1, (
        f"Expected at least 1 finding for GraphQL 502, got {findings}"
    )
    titles = [f.title for f in findings]
    assert any("graphql" in t.lower() or "api" in t.lower() for t in titles), (
        f"Expected a finding about the GraphQL endpoint, got titles: {titles}"
    )


def test_probe_site_health_handles_unparseable_doc_count() -> None:
    """An unparseable db_document_count body with non-200 status emits a finding."""
    probe_results = {
        "https://dev.judgemind.org/": (200, "<html>Judgemind</html>"),
        "https://dev.api.judgemind.org/graphql": (200, '{"data": {}}'),
        "db_document_count": (500, "Internal Server Error"),
    }
    findings = probe_site_health(probe_results)
    # count will be -1 (ValueError on "Internal Server Error"), status != 200
    assert len(findings) >= 1, (
        f"Expected at least 1 finding for unparseable count + non-200, got {findings}"
    )


# ---------------------------------------------------------------------------
# Edge cases for convergence regression probe
# ---------------------------------------------------------------------------


def test_probe_convergence_regression_empty_rows() -> None:
    """Empty row list returns no findings."""
    findings = probe_convergence_regression([])
    assert findings == [], f"Expected no findings for empty rows, got {findings}"


def test_probe_convergence_regression_empty_agent_ids() -> None:
    """Rows with empty/None agent_ids are skipped gracefully."""
    rows = [
        {"agent_id": "", "phase": "ralph"},
        {"agent_id": None, "phase": "ralph"},
    ]
    # Both rows should be skipped so no counts; no finding
    findings = probe_convergence_regression(rows)
    assert findings == [], f"Expected no findings for empty agent_ids, got {findings}"


def test_probe_bad_outcome_streak_empty_rows() -> None:
    """Empty outcome rows emits no finding."""
    findings = probe_bad_outcome_streak([])
    assert findings == [], f"Expected no findings for empty rows, got {findings}"


def test_probe_convergence_regression_zero_baseline_p95() -> None:
    """When all baseline agents have 0 ralph iterations, baseline_p95 is 0 — no finding."""
    # Simulate rows where no agent appears in both halves (splits to empty agent sets)
    # Actually test by having baseline rows with no matching phase='ralph'
    baseline_rows = [{"agent_id": "b-001", "phase": "plan"}]  # no ralph
    recent_rows = _make_phase_transition_rows(["r-001"], [5])
    all_rows = baseline_rows + recent_rows
    findings = probe_convergence_regression(all_rows)
    # baseline_counts will be empty -> early return
    assert findings == [], f"Expected no findings when baseline empty, got {findings}"
