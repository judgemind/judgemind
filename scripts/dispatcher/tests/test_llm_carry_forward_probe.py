"""Unit tests for scripts/dispatcher/llm_carry_forward_probe.py (issue #4309).

Fixture-driven — no live database. Each test feeds a synthetic ``run_audit``
summary into :func:`evaluate` and asserts the threshold logic produces the
expected findings.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make ``scripts`` importable without installing the repo as a package.
# parents[2] from scripts/dispatcher/tests/ is scripts/.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher.llm_carry_forward_probe import (  # noqa: E402
    DEFAULT_CLUSTER_THRESHOLD,
    DEFAULT_RIVERSIDE_LEGACY_CAP,
    Finding,
    _load_state,
    _percent_jump,
    _save_state,
    evaluate,
    extract_county_totals,
    render_summary_comment,
)


# ---------------------------------------------------------------------------
# Helpers — build synthetic ``run_audit`` summaries
# ---------------------------------------------------------------------------


def _county_bucket(
    *,
    rulings: int = 100,
    outcome_continue: int = 0,
    motion_type_contradiction: int = 0,
    case_title_text_mismatch: int = 0,
    cluster: int = 0,
    examples: list | None = None,
) -> dict:
    """Build one county dict matching the run_audit shape."""
    ex = examples or []
    return {
        "total_rulings": rulings,
        "outcome_continue": {"count": outcome_continue, "examples": ex},
        "motion_type_contradiction": {
            "count": motion_type_contradiction,
            "examples": ex,
        },
        "case_title_text_mismatch": {
            "count": case_title_text_mismatch,
            "examples": ex,
        },
        "all_same_case_title_cluster": {"count": cluster, "examples": ex},
    }


def _summary(counties: dict[str, dict]) -> dict:
    """Wrap a counties dict in the run_audit envelope."""
    return {
        "filter": {"county": None, "since": None},
        "counties": counties,
        "totals": {
            "rulings_audited": sum(c["total_rulings"] for c in counties.values()),
            "outcome_continue": sum(
                c["outcome_continue"]["count"] for c in counties.values()
            ),
            "motion_type_contradiction": sum(
                c["motion_type_contradiction"]["count"] for c in counties.values()
            ),
            "case_title_text_mismatch": sum(
                c["case_title_text_mismatch"]["count"] for c in counties.values()
            ),
            "all_same_case_title_cluster": sum(
                c["all_same_case_title_cluster"]["count"] for c in counties.values()
            ),
        },
        "all_clean": all(
            c["outcome_continue"]["count"] == 0
            and c["motion_type_contradiction"]["count"] == 0
            and c["case_title_text_mismatch"]["count"] == 0
            and c["all_same_case_title_cluster"]["count"] == 0
            for c in counties.values()
        ),
    }


# ---------------------------------------------------------------------------
# outcome_continue — any non-zero is a flag
# ---------------------------------------------------------------------------


def test_outcome_continue_any_nonzero_flags() -> None:
    summary = _summary({"Ventura": _county_bucket(outcome_continue=1)})
    findings = evaluate(summary, prior_totals=None)
    assert len(findings) == 1
    f = findings[0]
    assert f.probe == "outcome_continue"
    assert f.county == "Ventura"
    assert f.should_file_issue is True
    assert "outcome_continue" in f.title
    assert "Ventura" in f.title


def test_outcome_continue_zero_does_not_flag() -> None:
    summary = _summary({"Ventura": _county_bucket(outcome_continue=0)})
    findings = evaluate(summary, prior_totals=None)
    # No findings at all when every axis is zero.
    assert findings == []


def test_outcome_continue_per_county_isolation() -> None:
    """Each county trips its own finding."""
    summary = _summary(
        {
            "Ventura": _county_bucket(outcome_continue=1),
            "Santa Clara": _county_bucket(outcome_continue=2),
            "Orange": _county_bucket(outcome_continue=0),
        }
    )
    findings = [
        f for f in evaluate(summary, prior_totals=None) if f.probe == "outcome_continue"
    ]
    assert {f.county for f in findings} == {"Ventura", "Santa Clara"}


# ---------------------------------------------------------------------------
# all_same_case_title_cluster — non-Riverside threshold (default 5)
# ---------------------------------------------------------------------------


def test_cluster_below_default_threshold_does_not_flag() -> None:
    summary = _summary({"San Diego": _county_bucket(cluster=DEFAULT_CLUSTER_THRESHOLD)})
    findings = evaluate(summary, prior_totals=None)
    # exactly == cap is not a trip; only > cap fires.
    assert findings == []


def test_cluster_above_default_threshold_flags() -> None:
    summary = _summary(
        {"San Diego": _county_bucket(cluster=DEFAULT_CLUSTER_THRESHOLD + 1)}
    )
    findings = evaluate(summary, prior_totals=None)
    cluster_findings = [f for f in findings if f.probe == "all_same_case_title_cluster"]
    assert len(cluster_findings) == 1
    f = cluster_findings[0]
    assert f.county == "San Diego"
    assert f.should_file_issue is True
    assert f.details["cap"] == DEFAULT_CLUSTER_THRESHOLD


# ---------------------------------------------------------------------------
# all_same_case_title_cluster — Riverside legacy cap
# ---------------------------------------------------------------------------


def test_cluster_riverside_below_legacy_cap_does_not_flag() -> None:
    """Riverside has a documented backlog of 167 as of 2026-05-06; the legacy
    cap (default 80) lets that history pass while still catching new growth.
    """
    summary = _summary(
        {"Riverside": _county_bucket(cluster=DEFAULT_RIVERSIDE_LEGACY_CAP)}
    )
    findings = evaluate(summary, prior_totals=None)
    # exactly == legacy cap is not a trip.
    assert findings == []


def test_cluster_riverside_above_legacy_cap_flags() -> None:
    summary = _summary(
        {"Riverside": _county_bucket(cluster=DEFAULT_RIVERSIDE_LEGACY_CAP + 1)}
    )
    findings = evaluate(summary, prior_totals=None)
    cluster_findings = [f for f in findings if f.probe == "all_same_case_title_cluster"]
    assert len(cluster_findings) == 1
    assert cluster_findings[0].details["cap"] == DEFAULT_RIVERSIDE_LEGACY_CAP


def test_cluster_riverside_uses_separate_cap_from_others() -> None:
    """Riverside at 100 should pass (under legacy cap 80? no — over). Pick a
    value that is over the default cluster_threshold (5) but under the
    Riverside legacy cap (80)."""
    summary = _summary(
        {
            "Riverside": _county_bucket(cluster=50),
            "Ventura": _county_bucket(cluster=50),
        }
    )
    findings = evaluate(summary, prior_totals=None)
    cluster_counties = {
        f.county for f in findings if f.probe == "all_same_case_title_cluster"
    }
    # Riverside (under legacy cap 80) does NOT flag; Ventura (over cap 5) does.
    assert "Ventura" in cluster_counties
    assert "Riverside" not in cluster_counties


def test_cluster_riverside_case_insensitive() -> None:
    """Riverside detector handles different casing variants."""
    summary = _summary(
        {"riverside": _county_bucket(cluster=10)},  # lowercase
    )
    findings = evaluate(summary, prior_totals=None)
    cluster_findings = [f for f in findings if f.probe == "all_same_case_title_cluster"]
    # 10 is below the Riverside legacy cap (80), so no flag — confirms the
    # detector matched riverside as Riverside, not as a non-Riverside county
    # (which would have flagged at threshold 5).
    assert cluster_findings == []


# ---------------------------------------------------------------------------
# motion_type_contradiction / case_title_text_mismatch — % jump only
# ---------------------------------------------------------------------------


def test_noisy_axes_silent_on_first_run() -> None:
    """No prior_totals means first-run mode — noisy axes never fire."""
    summary = _summary({"Ventura": _county_bucket(motion_type_contradiction=999)})
    findings = evaluate(summary, prior_totals=None)
    # No noisy-axis fires; outcome_continue and cluster are zero.
    assert findings == []


def test_noisy_axes_jump_above_threshold_flags() -> None:
    """+30% jump on a noisy axis (over the +25% default) should fire."""
    summary = _summary({"Ventura": _county_bucket(motion_type_contradiction=130)})
    findings = evaluate(
        summary,
        prior_totals={"Ventura": {"motion_type_contradiction": 100}},
    )
    jump_findings = [f for f in findings if f.probe.startswith("jump_")]
    assert len(jump_findings) == 1
    f = jump_findings[0]
    assert f.probe == "jump_motion_type_contradiction"
    assert f.details["prior_count"] == 100
    assert f.details["current_count"] == 130
    assert 0.29 < f.details["jump_fraction"] < 0.31


def test_noisy_axes_jump_below_threshold_silent() -> None:
    """+10% jump (under +25%) should not fire."""
    summary = _summary({"Ventura": _county_bucket(motion_type_contradiction=110)})
    findings = evaluate(
        summary,
        prior_totals={"Ventura": {"motion_type_contradiction": 100}},
    )
    jump_findings = [f for f in findings if f.probe.startswith("jump_")]
    assert jump_findings == []


def test_noisy_axes_decrease_silent() -> None:
    """A drop in count never fires."""
    summary = _summary({"Ventura": _county_bucket(case_title_text_mismatch=50)})
    findings = evaluate(
        summary,
        prior_totals={"Ventura": {"case_title_text_mismatch": 100}},
    )
    assert all(not f.probe.startswith("jump_") for f in findings)


def test_noisy_axes_zero_to_nonzero_below_floor_silent() -> None:
    """Prior=0, current=2 must not fire (below 5-floor)."""
    summary = _summary({"Ventura": _county_bucket(motion_type_contradiction=2)})
    findings = evaluate(
        summary,
        prior_totals={"Ventura": {"motion_type_contradiction": 0}},
    )
    assert all(not f.probe.startswith("jump_") for f in findings)


def test_noisy_axes_zero_to_floor_fires() -> None:
    """Prior=0, current=5 fires (crosses the 5-floor)."""
    summary = _summary({"Ventura": _county_bucket(motion_type_contradiction=5)})
    findings = evaluate(
        summary,
        prior_totals={"Ventura": {"motion_type_contradiction": 0}},
    )
    jump_findings = [f for f in findings if f.probe.startswith("jump_")]
    assert len(jump_findings) == 1


def test_noisy_axes_threshold_override() -> None:
    """Custom --noisy-jump-fraction is honored."""
    summary = _summary({"Ventura": _county_bucket(motion_type_contradiction=110)})
    findings = evaluate(
        summary,
        prior_totals={"Ventura": {"motion_type_contradiction": 100}},
        noisy_jump_fraction=0.05,  # 5% → 10% jump fires
    )
    jump_findings = [f for f in findings if f.probe.startswith("jump_")]
    assert len(jump_findings) == 1


# ---------------------------------------------------------------------------
# _percent_jump
# ---------------------------------------------------------------------------


def test_percent_jump_normal() -> None:
    assert _percent_jump(100, 130) == 0.30


def test_percent_jump_zero_prior_below_floor() -> None:
    assert _percent_jump(0, 4) is None


def test_percent_jump_zero_prior_at_floor() -> None:
    assert _percent_jump(0, 5) == float("inf")


def test_percent_jump_no_change() -> None:
    assert _percent_jump(50, 50) is None


def test_percent_jump_decrease() -> None:
    assert _percent_jump(100, 50) is None


# ---------------------------------------------------------------------------
# extract_county_totals + state round-trip
# ---------------------------------------------------------------------------


def test_extract_county_totals_shape() -> None:
    summary = _summary(
        {
            "Ventura": _county_bucket(outcome_continue=2, cluster=3),
            "San Diego": _county_bucket(case_title_text_mismatch=10),
        }
    )
    totals = extract_county_totals(summary)
    assert totals["Ventura"]["outcome_continue"] == 2
    assert totals["Ventura"]["all_same_case_title_cluster"] == 3
    assert totals["Ventura"]["motion_type_contradiction"] == 0
    assert totals["San Diego"]["case_title_text_mismatch"] == 10


def test_state_roundtrip(tmp_path) -> None:
    """_save_state writes JSON readable by _load_state."""
    path = str(tmp_path / "state.json")
    totals = {"Ventura": {"outcome_continue": 1, "motion_type_contradiction": 7}}
    _save_state(path, totals)
    loaded = _load_state(path)
    assert loaded == totals


def test_load_state_missing_returns_none(tmp_path) -> None:
    """Missing state file is first-run mode (returns None)."""
    path = str(tmp_path / "absent.json")
    assert _load_state(path) is None


def test_load_state_malformed_returns_none(tmp_path) -> None:
    """Malformed JSON is treated as first-run."""
    path = tmp_path / "bad.json"
    path.write_text("not json{", encoding="utf-8")
    assert _load_state(str(path)) is None


def test_load_state_none_path_returns_none() -> None:
    """Explicit None path is first-run mode."""
    assert _load_state(None) is None


# ---------------------------------------------------------------------------
# render_summary_comment
# ---------------------------------------------------------------------------


def test_render_summary_comment_clean_run() -> None:
    """Clean run renders a heartbeat with totals and 'all clean' line."""
    summary = _summary(
        {
            "Ventura": _county_bucket(rulings=200),
            "Riverside": _county_bucket(rulings=400),
        }
    )
    text = render_summary_comment(summary, [])
    assert "Weekly LLM carry-forward audit" in text
    assert "All thresholds clean" in text
    assert "Ventura" in text
    assert "Riverside" in text


def test_render_summary_comment_with_findings() -> None:
    """Comment with findings names the trips and counts them."""
    summary = _summary({"Ventura": _county_bucket(outcome_continue=2)})
    findings = evaluate(summary, prior_totals=None)
    text = render_summary_comment(summary, findings)
    assert "1 threshold trip" in text or "1 threshold trips" in text
    assert "outcome_continue" in text


# ---------------------------------------------------------------------------
# Finding shape — title + body invariants for SKILL.md consumption
# ---------------------------------------------------------------------------


def test_finding_title_starts_with_bracketed_prefix() -> None:
    """SKILL.md dedups by bracketed title prefix; titles must start with `[`."""
    summary = _summary({"Ventura": _county_bucket(outcome_continue=1)})
    findings = evaluate(summary, prior_totals=None)
    for f in findings:
        assert f.title.startswith("[llm-carry-forward "), (
            f"finding title must start with '[llm-carry-forward ' for SKILL.md "
            f"dedup search; got {f.title!r}"
        )


def test_finding_body_includes_county_axis_repro() -> None:
    """Bodies should self-describe enough that an agent can act on them."""
    summary = _summary({"Ventura": _county_bucket(outcome_continue=1)})
    findings = evaluate(summary, prior_totals=None)
    f = findings[0]
    assert "Ventura" in f.body
    assert "outcome_continue" in f.body
    # Repro stanza references the audit script and county filter.
    assert "audit_llm_carry_forward.py" in f.body
    assert "--county Ventura" in f.body


def test_findings_serializable_to_json() -> None:
    """The probe writes findings via dataclasses.asdict + json.dumps; ensure
    everything in the Finding round-trips cleanly."""
    summary = _summary(
        {
            "Ventura": _county_bucket(
                outcome_continue=1, cluster=10, examples=[{"k": "v"}]
            )
        }
    )
    findings = evaluate(summary, prior_totals=None)
    from dataclasses import asdict

    payload = json.dumps([asdict(f) for f in findings], default=str)
    parsed = json.loads(payload)
    assert isinstance(parsed, list)
    assert all(isinstance(item, dict) for item in parsed)
    assert all("title" in item and "body" in item for item in parsed)


def test_evaluate_returns_finding_instances() -> None:
    """Type guard: evaluate returns Finding objects, not dicts."""
    summary = _summary({"Ventura": _county_bucket(outcome_continue=1)})
    findings = evaluate(summary, prior_totals=None)
    assert all(isinstance(f, Finding) for f in findings)
