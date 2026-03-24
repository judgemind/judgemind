"""Scoring module for validation prompt eval.

Pure scoring functions — no I/O, no API calls. Takes validation results
and computes metrics: detection rate, false positive rate, per-pattern
accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    """Result from a single validation call."""

    test_case_id: str
    category: str  # "known_good", "known_bad", "edge_case"
    expected_verdict: str  # "pass", "flag", "fail"
    actual_verdict: str  # "pass", "flag", "fail"
    reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    error: str | None = None
    bug_pattern: str | None = None


@dataclass
class PatternScore:
    """Accuracy for a specific bug pattern."""

    pattern: str
    total: int = 0
    detected: int = 0  # correctly flagged or failed

    @property
    def detection_rate(self) -> float:
        return self.detected / self.total if self.total > 0 else 0.0


@dataclass
class EvalSummary:
    """Overall evaluation summary."""

    model: str
    results: list[ValidationResult] = field(default_factory=list)

    # Detection metrics (known_bad)
    total_known_bad: int = 0
    detected_known_bad: int = 0  # correctly flagged/failed

    # False positive metrics (known_good + edge_case)
    total_known_good: int = 0
    false_positives: int = 0  # good cases incorrectly flagged

    total_edge_cases: int = 0
    edge_false_positives: int = 0  # edge cases incorrectly flagged

    # Per-pattern breakdown
    pattern_scores: dict[str, PatternScore] = field(default_factory=dict)

    # Cost estimates
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    avg_input_tokens: int = 0
    avg_output_tokens: int = 0
    cost_per_call: float = 0.0
    cost_per_call_breakdown: str = ""

    # Errors
    errors: list[str] = field(default_factory=list)

    @property
    def detection_rate(self) -> float:
        """Fraction of known-bad cases correctly detected."""
        if self.total_known_bad == 0:
            return 0.0
        return self.detected_known_bad / self.total_known_bad

    @property
    def false_positive_rate(self) -> float:
        """Fraction of known-good cases incorrectly flagged."""
        if self.total_known_good == 0:
            return 0.0
        return self.false_positives / self.total_known_good

    @property
    def edge_false_positive_rate(self) -> float:
        """Fraction of edge cases incorrectly flagged."""
        if self.total_edge_cases == 0:
            return 0.0
        return self.edge_false_positives / self.total_edge_cases

    @property
    def overall_false_positive_rate(self) -> float:
        """Fraction of all non-bad cases incorrectly flagged."""
        total = self.total_known_good + self.total_edge_cases
        fps = self.false_positives + self.edge_false_positives
        if total == 0:
            return 0.0
        return fps / total

    @property
    def passes_thresholds(self) -> bool:
        """Check if the eval meets acceptance criteria."""
        return self.detection_rate >= 1.0 and self.overall_false_positive_rate < 0.05


def is_detected(expected: str, actual: str) -> bool:
    """Check if a known-bad case was correctly detected.

    A known-bad case is detected if the validator returned "flag" or "fail".
    """
    return actual in ("flag", "fail")


def is_false_positive(expected: str, actual: str) -> bool:
    """Check if a known-good/edge case was incorrectly flagged.

    A false positive occurs when a case expected to "pass" gets "flag" or "fail".
    """
    return expected == "pass" and actual in ("flag", "fail")


def score_results(
    results: list[ValidationResult],
    model: str,
    pricing_per_m: dict[str, float] | None = None,
) -> EvalSummary:
    """Score a list of validation results into an EvalSummary."""
    summary = EvalSummary(model=model, results=results)

    for r in results:
        if r.error:
            summary.errors.append(r.test_case_id + ": " + r.error)
            continue

        summary.total_input_tokens += r.input_tokens
        summary.total_output_tokens += r.output_tokens

        if r.category == "known_bad":
            summary.total_known_bad += 1
            if is_detected(r.expected_verdict, r.actual_verdict):
                summary.detected_known_bad += 1

            # Track per-pattern accuracy
            pattern = r.bug_pattern or "unknown"
            if pattern not in summary.pattern_scores:
                summary.pattern_scores[pattern] = PatternScore(pattern=pattern)
            ps = summary.pattern_scores[pattern]
            ps.total += 1
            if is_detected(r.expected_verdict, r.actual_verdict):
                ps.detected += 1

        elif r.category == "known_good":
            summary.total_known_good += 1
            if is_false_positive(r.expected_verdict, r.actual_verdict):
                summary.false_positives += 1

        elif r.category == "edge_case":
            summary.total_edge_cases += 1
            if is_false_positive(r.expected_verdict, r.actual_verdict):
                summary.edge_false_positives += 1

    # Compute averages
    valid_count = len([r for r in results if r.error is None])
    if valid_count > 0:
        summary.avg_input_tokens = summary.total_input_tokens // valid_count
        summary.avg_output_tokens = summary.total_output_tokens // valid_count

    # Compute cost
    if pricing_per_m and valid_count > 0:
        input_cost = (
            summary.avg_input_tokens * pricing_per_m.get("input", 0) / 1_000_000
        )
        output_cost = (
            summary.avg_output_tokens * pricing_per_m.get("output", 0) / 1_000_000
        )
        summary.cost_per_call = input_cost + output_cost
        summary.cost_per_call_breakdown = (
            "input: "
            + str(summary.avg_input_tokens)
            + " tok * $"
            + str(pricing_per_m.get("input", 0))
            + "/M = $"
            + f"{input_cost:.6f}"
            + " + output: "
            + str(summary.avg_output_tokens)
            + " tok * $"
            + str(pricing_per_m.get("output", 0))
            + "/M = $"
            + f"{output_cost:.6f}"
        )

    return summary


def format_report(summary: EvalSummary) -> str:
    """Format a human-readable report from an EvalSummary."""
    lines = [
        "# Validation Eval Report: " + summary.model,
        "",
        "## Detection Rate (known-bad cases)",
        "  Total known-bad:  " + str(summary.total_known_bad),
        "  Detected:         " + str(summary.detected_known_bad),
        "  Detection rate:   " + f"{summary.detection_rate:.0%}",
        "",
    ]

    # Per-pattern breakdown
    if summary.pattern_scores:
        lines.append("  Per-pattern breakdown:")
        for pattern, ps in sorted(summary.pattern_scores.items()):
            lines.append(
                "    "
                + pattern
                + ": "
                + str(ps.detected)
                + "/"
                + str(ps.total)
                + " ("
                + f"{ps.detection_rate:.0%}"
                + ")"
            )
        lines.append("")

    lines.extend(
        [
            "## False Positive Rate",
            "  Known-good cases: "
            + str(summary.total_known_good)
            + " tested, "
            + str(summary.false_positives)
            + " false positives"
            + " ("
            + f"{summary.false_positive_rate:.0%}"
            + ")",
            "  Edge cases:       "
            + str(summary.total_edge_cases)
            + " tested, "
            + str(summary.edge_false_positives)
            + " false positives"
            + " ("
            + f"{summary.edge_false_positive_rate:.0%}"
            + ")",
            "  Overall FP rate:  " + f"{summary.overall_false_positive_rate:.0%}",
            "",
            "## Cost Estimate",
            "  Avg input tokens:  " + str(summary.avg_input_tokens),
            "  Avg output tokens: " + str(summary.avg_output_tokens),
            "  Cost per call:     $" + f"{summary.cost_per_call:.6f}",
        ]
    )

    if summary.cost_per_call_breakdown:
        lines.append("  Breakdown:         " + summary.cost_per_call_breakdown)

    # Estimated monthly cost: ~1000 rulings/day * 30 days
    monthly = summary.cost_per_call * 1000 * 30
    lines.append("  Monthly (1k rulings/day): $" + f"{monthly:.2f}")

    lines.extend(
        [
            "",
            "## Threshold Check",
        ]
    )

    if summary.passes_thresholds:
        lines.append("  PASSED: 100% detection, <5% false positive rate")
    else:
        lines.append("  FAILED:")
        if summary.detection_rate < 1.0:
            lines.append(
                "    - Detection rate " + f"{summary.detection_rate:.0%}" + " < 100%"
            )
        if summary.overall_false_positive_rate >= 0.05:
            lines.append(
                "    - False positive rate "
                + f"{summary.overall_false_positive_rate:.0%}"
                + " >= 5%"
            )

    # Per-case detail
    lines.extend(["", "## Per-Case Detail"])
    lines.append(
        "  "
        + f"{'ID':<40}"
        + f"{'Category':<15}"
        + f"{'Expected':<10}"
        + f"{'Actual':<10}"
        + "Reason"
    )
    lines.append(
        "  "
        + "-" * 40
        + " "
        + "-" * 14
        + " "
        + "-" * 9
        + " "
        + "-" * 9
        + " "
        + "-" * 30
    )

    for r in summary.results:
        if r.error:
            actual_str = "ERROR"
            reason_str = r.error[:30]
        else:
            actual_str = r.actual_verdict
            reason_str = r.reason[:50] if r.reason else ""

        match_marker = ""
        if r.category == "known_bad" and not is_detected(
            r.expected_verdict, r.actual_verdict
        ):
            match_marker = " MISS"
        elif r.category in ("known_good", "edge_case") and is_false_positive(
            r.expected_verdict, r.actual_verdict
        ):
            match_marker = " FP"

        lines.append(
            "  "
            + f"{r.test_case_id:<40}"
            + f"{r.category:<15}"
            + f"{r.expected_verdict:<10}"
            + f"{actual_str:<10}"
            + reason_str
            + match_marker
        )

    if summary.errors:
        lines.extend(["", "## Errors"])
        for err in summary.errors:
            lines.append("  " + err)

    return "\n".join(lines)
