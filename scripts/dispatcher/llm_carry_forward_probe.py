#!/usr/bin/env python3
"""LLM carry-forward periodic-probe backend — /audit-llm-carry-forward skill (#4309).

Wraps :func:`scripts.audit_llm_carry_forward.run_audit` with threshold logic
and emits a structured findings list the SKILL.md wrapper consumes.

Threshold rules (from issue #4309):

* ``outcome_continue`` — any non-zero count for any county is worth flagging.
  This is the strongest carry-forward signal and the bug class baseline as
  of 2026-05-06 was a single county at total=5; a clean run is zero.

* ``all_same_case_title_cluster`` — splitter-protected counties (Riverside,
  Fresno, San Diego, LA, post-#4286 wiring) should produce ≤ 4. Threshold:
  > 5 per non-Riverside county. Riverside has a historical backlog of
  pre-splitter rulings (baseline 167 on 2026-05-06); flag only when
  Riverside crosses ``riverside_legacy_cap`` (default 80) so the report
  remains actionable until a reingest sweep clears that history.

* ``motion_type_contradiction`` and ``case_title_text_mismatch`` — noisy
  per-rule axes that the LLM trips on for legitimate reasons (motion-type
  taxonomy gaps, party-name redaction). Flag only on a > 25% jump versus
  the previous run's per-county count. On the first ever run there is no
  prior baseline, so these axes are silent until the second fire.

A finding's ``should_file_issue`` field tells the SKILL.md wrapper whether
to file an ``agent/ready`` follow-up (true) or just append to the long-lived
audit-log issue (false). The default is to file when ANY county trips a
threshold; otherwise the wrapper posts a heartbeat comment.

Usage::

    scripts/dispatcher/llm_carry_forward_probe.py \\
        --output tmp/llm-carry-forward/findings.json \\
        --state tmp/llm-carry-forward/last_totals.json

The ``--state`` path is read for prior totals (jump-detection) and rewritten
on success. Missing state file => first-run mode (no jump-detection).

Exit codes:
    0   Probe completed; JSON written.
    1   DATABASE_URL missing, DB connection failed, audit error.
"""
# venv: scraper-framework
# permanent: true

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

# Threshold defaults (overridable via CLI). Sourced from issue #4309's
# 2026-05-06 baseline.
DEFAULT_CLUSTER_THRESHOLD: int = 5
DEFAULT_RIVERSIDE_LEGACY_CAP: int = 80
DEFAULT_NOISY_JUMP_FRACTION: float = 0.25  # 25% jump
RIVERSIDE_NAMES: frozenset[str] = frozenset({"riverside"})

NOISY_AXES: tuple[str, ...] = (
    "motion_type_contradiction",
    "case_title_text_mismatch",
)


@dataclass
class Finding:
    """A single threshold-trip finding for the SKILL.md wrapper."""

    probe: str  # e.g. "outcome_continue", "cluster_riverside", "jump_motion_type"
    county: str
    title: str
    body: str
    severity: str  # "info" | "warning" | "critical"
    should_file_issue: bool
    details: dict[str, Any] = field(default_factory=dict)


def _is_riverside(county: str) -> bool:
    """Case-insensitive Riverside detector."""
    return county.lower() in RIVERSIDE_NAMES


def evaluate(
    summary: dict[str, Any],
    *,
    prior_totals: dict[str, dict[str, int]] | None = None,
    cluster_threshold: int = DEFAULT_CLUSTER_THRESHOLD,
    riverside_legacy_cap: int = DEFAULT_RIVERSIDE_LEGACY_CAP,
    noisy_jump_fraction: float = DEFAULT_NOISY_JUMP_FRACTION,
) -> list[Finding]:
    """Apply per-county thresholds to ``summary`` (the ``run_audit`` output).

    ``prior_totals`` maps ``county -> {axis: count}`` from the previous
    successful run. ``None`` => first-run mode (skip noisy-axis jump check).
    """
    findings: list[Finding] = []
    counties: dict[str, dict[str, Any]] = summary.get("counties", {})

    for county_name in sorted(counties):
        c = counties[county_name]
        # --- outcome_continue: any non-zero is worth a fire -----------------
        oc_count = int(c["outcome_continue"]["count"])
        if oc_count > 0:
            findings.append(
                Finding(
                    probe="outcome_continue",
                    county=county_name,
                    title=(
                        f"[llm-carry-forward outcome_continue {county_name}] "
                        f"{oc_count} ruling(s) with definitive outcome but "
                        f"continuance text"
                    ),
                    body=_render_axis_body(
                        county_name,
                        "outcome_continue",
                        oc_count,
                        c["outcome_continue"]["examples"],
                        rule=(
                            "Any non-zero `outcome_continue` count is a strong "
                            "LLM rule-5b carry-forward signal. The LLM emitted "
                            "outcome=granted/denied for a ruling whose text "
                            "starts with continuance boilerplate — almost "
                            "certainly the page-1 outcome was copied onto a "
                            "page-N continuance entry."
                        ),
                    ),
                    severity="warning",
                    should_file_issue=True,
                    details={
                        "count": oc_count,
                        "examples": c["outcome_continue"]["examples"],
                    },
                )
            )

        # --- all_same_case_title_cluster: threshold per county --------------
        cluster_count = int(c["all_same_case_title_cluster"]["count"])
        if _is_riverside(county_name):
            cluster_cap = riverside_legacy_cap
            cap_label = (
                f"Riverside legacy cap ({riverside_legacy_cap}) — pre-#4286 "
                f"backlog still on file. Drop the cap to {cluster_threshold} "
                f"once the post-#4286 reingest sweep clears history."
            )
        else:
            cluster_cap = cluster_threshold
            cap_label = (
                f"Splitter-protected counties should be ≤ {cluster_threshold}. "
                f"Counts above this strongly suggest the splitter regressed or "
                f"a multi-case PDF shape is uncovered."
            )
        if cluster_count > cluster_cap:
            findings.append(
                Finding(
                    probe="all_same_case_title_cluster",
                    county=county_name,
                    title=(
                        f"[llm-carry-forward cluster {county_name}] "
                        f"{cluster_count} same-title clusters > cap "
                        f"({cluster_cap})"
                    ),
                    body=_render_axis_body(
                        county_name,
                        "all_same_case_title_cluster",
                        cluster_count,
                        c["all_same_case_title_cluster"]["examples"],
                        rule=cap_label,
                    ),
                    severity="warning",
                    should_file_issue=True,
                    details={
                        "count": cluster_count,
                        "cap": cluster_cap,
                        "examples": c["all_same_case_title_cluster"]["examples"],
                    },
                )
            )

        # --- noisy axes: % jump vs prior run --------------------------------
        if prior_totals is not None:
            prior_county = prior_totals.get(county_name, {})
            for axis in NOISY_AXES:
                cur_count = int(c[axis]["count"])
                prior_count = int(prior_county.get(axis, 0))
                jump = _percent_jump(prior_count, cur_count)
                if jump is not None and jump > noisy_jump_fraction:
                    findings.append(
                        Finding(
                            probe=f"jump_{axis}",
                            county=county_name,
                            title=(
                                f"[llm-carry-forward jump {axis} {county_name}] "
                                f"{prior_count} -> {cur_count} "
                                f"(+{jump:.0%})"
                            ),
                            body=_render_jump_body(
                                county_name,
                                axis,
                                prior_count,
                                cur_count,
                                jump,
                                c[axis]["examples"],
                                noisy_jump_fraction,
                            ),
                            severity="warning",
                            should_file_issue=True,
                            details={
                                "axis": axis,
                                "prior_count": prior_count,
                                "current_count": cur_count,
                                "jump_fraction": jump,
                                "examples": c[axis]["examples"],
                            },
                        )
                    )
    return findings


def _percent_jump(prior: int, current: int) -> float | None:
    """Return fractional jump (e.g. 0.30 == +30%) or ``None`` when meaningless.

    Returns ``None`` when the prior count is zero (no baseline) AND the
    current count is below a small floor (5) — sub-floor noise should not
    fire. When the prior is zero but current is >= floor, returns infinity-
    flavored 1.0 so it always trips a > 0 threshold.
    """
    if current <= prior:
        return None
    if prior == 0:
        # Avoid division by zero. Only fire when current crosses a small
        # absolute floor; below that the axis is sub-noise.
        if current >= 5:
            return float("inf")
        return None
    return (current - prior) / prior


def _render_axis_body(
    county: str,
    axis: str,
    count: int,
    examples: list[dict[str, Any]],
    *,
    rule: str,
) -> str:
    """Render an issue body for a fixed-threshold axis."""
    lines: list[str] = []
    lines.append("## LLM carry-forward audit threshold tripped")
    lines.append("")
    lines.append(f"**County:** {county}")
    lines.append(f"**Axis:** `{axis}`")
    lines.append(f"**Count:** {count}")
    lines.append("")
    lines.append("### Rule")
    lines.append(rule)
    lines.append("")
    lines.append("### Examples")
    if examples:
        for ex in examples:
            lines.append(f"- `{json.dumps(ex, default=str)}`")
    else:
        lines.append("_(no examples returned by run_audit)_")
    lines.append("")
    lines.append(
        "### Repro\n"
        "```\n"
        "scripts/ecs-run-task.sh scripts/audit_llm_carry_forward.py "
        f"-- --county {county} --json\n"
        "```\n"
    )
    lines.append(
        "Filed automatically by the `/audit-llm-carry-forward` weekly "
        "scheduled skill (issue #4309). Background context lives in #3649 "
        "(original LLM rule-5b carry-forward bug) and #4289 (audit script)."
    )
    return "\n".join(lines)


def _render_jump_body(
    county: str,
    axis: str,
    prior: int,
    current: int,
    jump: float,
    examples: list[dict[str, Any]],
    threshold: float,
) -> str:
    """Render an issue body for a noisy-axis jump finding."""
    if jump == float("inf"):
        jump_str = "previous run was 0; current crossed the absolute floor"
    else:
        jump_str = f"+{jump:.0%} (threshold: +{threshold:.0%})"
    lines: list[str] = []
    lines.append("## LLM carry-forward audit jump detected")
    lines.append("")
    lines.append(f"**County:** {county}")
    lines.append(f"**Axis:** `{axis}`")
    lines.append(f"**Prior run:** {prior}")
    lines.append(f"**Current run:** {current}")
    lines.append(f"**Jump:** {jump_str}")
    lines.append("")
    lines.append(
        "### Rule\n"
        "Noisy axes (`motion_type_contradiction`, `case_title_text_mismatch`) "
        "fire only on jumps >= 25% over the previous run's count. The trip "
        "above suggests a regression — most likely the LLM started "
        "mis-attributing a motion-type or party-name signal it previously "
        "got right."
    )
    lines.append("")
    lines.append("### Examples")
    if examples:
        for ex in examples:
            lines.append(f"- `{json.dumps(ex, default=str)}`")
    else:
        lines.append("_(no examples returned by run_audit)_")
    lines.append("")
    lines.append(
        "### Repro\n"
        "```\n"
        "scripts/ecs-run-task.sh scripts/audit_llm_carry_forward.py "
        f"-- --county {county} --json\n"
        "```\n"
    )
    lines.append(
        "Filed automatically by the `/audit-llm-carry-forward` weekly "
        "scheduled skill (issue #4309)."
    )
    return "\n".join(lines)


def extract_county_totals(summary: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Project a ``run_audit`` summary down to ``{county: {axis: count}}``."""
    out: dict[str, dict[str, int]] = {}
    for county_name, c in summary.get("counties", {}).items():
        out[county_name] = {
            axis: int(c[axis]["count"])
            for axis in (
                "outcome_continue",
                "motion_type_contradiction",
                "case_title_text_mismatch",
                "all_same_case_title_cluster",
            )
        }
    return out


def render_summary_comment(summary: dict[str, Any], findings: list[Finding]) -> str:
    """Render a weekly heartbeat comment for the long-lived audit-log issue.

    Posted whether or not any threshold tripped — the heartbeat tells
    operators the periodic skill is still alive and lists the per-county
    counts they would otherwise have to gather by hand.
    """
    lines: list[str] = []
    totals = summary.get("totals", {})
    lines.append("## Weekly LLM carry-forward audit")
    lines.append("")
    lines.append(f"**Total CA rulings audited:** {totals.get('rulings_audited', 0)}")
    lines.append(
        f"**outcome_continue:** {totals.get('outcome_continue', 0)} | "
        f"**motion_type_contradiction:** {totals.get('motion_type_contradiction', 0)} | "
        f"**case_title_text_mismatch:** {totals.get('case_title_text_mismatch', 0)} | "
        f"**all_same_case_title_cluster:** {totals.get('all_same_case_title_cluster', 0)}"
    )
    lines.append("")
    if findings:
        lines.append(
            f"**{len(findings)} threshold trip(s)** — see filed follow-up issues "
            f"with title prefix `[llm-carry-forward …]`."
        )
        for f in findings:
            lines.append(f"- {f.title}")
    else:
        lines.append("All thresholds clean — no follow-up issues filed.")
    lines.append("")
    # Per-county table (compact)
    lines.append("### Per-county counts")
    lines.append("")
    lines.append("| County | Rulings | OutCont | MTypeMis | TitleMis | TitleClust |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for county_name in sorted(summary.get("counties", {})):
        c = summary["counties"][county_name]
        lines.append(
            f"| {county_name} | {c['total_rulings']} | "
            f"{c['outcome_continue']['count']} | "
            f"{c['motion_type_contradiction']['count']} | "
            f"{c['case_title_text_mismatch']['count']} | "
            f"{c['all_same_case_title_cluster']['count']} |"
        )
    lines.append("")
    lines.append("_Filed automatically by `/audit-llm-carry-forward` (issue #4309)._")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=("Run the LLM carry-forward periodic probe and emit findings JSON.")
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path to write the findings + summary JSON envelope.",
    )
    parser.add_argument(
        "--state",
        default=None,
        help=(
            "Path to read+write per-county totals across runs (jump-detection). "
            "Missing on first run is OK — noisy-axis jump checks are silent "
            "until a baseline exists."
        ),
    )
    parser.add_argument(
        "--cluster-threshold",
        type=int,
        default=DEFAULT_CLUSTER_THRESHOLD,
        help=(
            f"Per-county all_same_case_title_cluster cap (default: "
            f"{DEFAULT_CLUSTER_THRESHOLD})."
        ),
    )
    parser.add_argument(
        "--riverside-legacy-cap",
        type=int,
        default=DEFAULT_RIVERSIDE_LEGACY_CAP,
        help=(
            f"Riverside-only cluster cap until reingest sweep clears legacy "
            f"history (default: {DEFAULT_RIVERSIDE_LEGACY_CAP})."
        ),
    )
    parser.add_argument(
        "--noisy-jump-fraction",
        type=float,
        default=DEFAULT_NOISY_JUMP_FRACTION,
        help=(
            f"Fractional jump that triggers a flag on noisy axes (default: "
            f"{DEFAULT_NOISY_JUMP_FRACTION:.2f} == 25%%)."
        ),
    )
    return parser.parse_args(argv)


def _load_state(path: str | None) -> dict[str, dict[str, int]] | None:
    """Best-effort load of prior per-county totals. Returns None if missing."""
    if not path:
        return None
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data  # type: ignore[return-value]


def _save_state(path: str | None, totals: dict[str, dict[str, int]]) -> None:
    """Best-effort save of current per-county totals."""
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(totals, fh, indent=2, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        sys.stderr.write("llm_carry_forward_probe: DATABASE_URL not set\n")
        return 1

    # Lazy import — keeps the module importable without psycopg in CI.
    try:
        from scripts.audit_llm_carry_forward import run_audit  # noqa: PLC0415
    except ImportError:  # pragma: no cover — defensive
        sys.path.insert(
            0,
            os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            ),
        )
        from scripts.audit_llm_carry_forward import run_audit  # noqa: PLC0415

    try:
        summary = run_audit(dsn)
    except Exception as exc:  # pragma: no cover — live DB failures
        sys.stderr.write(f"llm_carry_forward_probe: audit error: {exc}\n")
        return 1

    prior = _load_state(args.state)
    findings = evaluate(
        summary,
        prior_totals=prior,
        cluster_threshold=args.cluster_threshold,
        riverside_legacy_cap=args.riverside_legacy_cap,
        noisy_jump_fraction=args.noisy_jump_fraction,
    )

    envelope = {
        "summary": summary,
        "totals_by_county": extract_county_totals(summary),
        "findings": [asdict(f) for f in findings],
        "comment_markdown": render_summary_comment(summary, findings),
    }

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(envelope, fh, indent=2, default=str)

    # Persist for next run's jump-detection.
    _save_state(args.state, envelope["totals_by_county"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
