"""Deterministic validation rules for the ingestion pipeline.

Cheap, rule-based checks that run on every document between enrichment
and DB write.  These catch obvious structural failures (raw HTML stored
as ruling text, hearing dates decades wrong, concatenated case titles)
without requiring an LLM call.

Each rule produces ``pass``, ``flag``, or ``fail``:

- **fail** — clear data corruption; the ruling is rejected and not written
  to the DB.  The document is logged for review.
- **flag** — suspicious but possibly valid; the ruling is written but logged
  for async review.
- **pass** — no issue detected.

Rules are complementary to the LLM validation gate (``validation.gate``)
and run before it as a cheaper pre-filter.

See: issue #2329, parent #1801.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum allowed gap between hearing_date and captured_at.
_HEARING_DATE_MAX_DELTA_DAYS = 180

# Truncation sentinel length — ruling_text at exactly this length
# suggests the entire page was stored instead of an individual ruling.
_TRUNCATION_SENTINEL_LENGTH = 50_000

# HTML markers that indicate raw HTML was stored as ruling text
# (transcription step was skipped).
_HTML_START_MARKERS = (
    "<html",
    "<!doctype",
    "<div",
)

# Pattern to detect multiple v./vs. separators in a case title,
# indicating multi-case contamination.
_MULTI_VS_PATTERN = re.compile(
    r"(?:\bv\.\s|\bvs\.\s)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class DeterministicRuleResult:
    """Result of a single deterministic validation rule."""

    rule: str
    result: str  # "pass", "flag", or "fail"
    reason: str | None = None


@dataclass
class DeterministicValidationResult:
    """Aggregated result of all deterministic validation rules."""

    overall: str  # "pass", "flag", or "fail" (worst result wins)
    rules: list[DeterministicRuleResult] = field(default_factory=list)

    @property
    def failed_rules(self) -> list[DeterministicRuleResult]:
        """Return only rules that produced a fail result."""
        return [r for r in self.rules if r.result == "fail"]

    @property
    def flagged_rules(self) -> list[DeterministicRuleResult]:
        """Return only rules that produced a flag result."""
        return [r for r in self.rules if r.result == "flag"]

    @property
    def reasons(self) -> list[str]:
        """Return all non-None reasons from non-pass rules."""
        return [r.reason for r in self.rules if r.reason is not None and r.result != "pass"]


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------


def check_no_html_in_ruling_text(ruling_text: str | None) -> DeterministicRuleResult:
    """Check that ruling_text does not contain raw HTML.

    If the ruling text starts with ``<html>``, ``<!DOCTYPE>``, or ``<div>``,
    the transcription step was likely skipped and raw HTML was stored instead
    of extracted text.
    """
    if not ruling_text:
        return DeterministicRuleResult(rule="no_html_in_ruling_text", result="pass")

    stripped = ruling_text.lstrip()
    lower = stripped[:20].lower()  # Only check the first 20 chars
    for marker in _HTML_START_MARKERS:
        if lower.startswith(marker):
            return DeterministicRuleResult(
                rule="no_html_in_ruling_text",
                result="fail",
                reason=f"ruling_text starts with '{marker}' — raw HTML detected, "
                "transcription step was likely skipped",
            )
    return DeterministicRuleResult(rule="no_html_in_ruling_text", result="pass")


def check_hearing_date_in_range(
    hearing_date: date | None,
    captured_at: date | None,
) -> DeterministicRuleResult:
    """Check that hearing_date is within +/-180 days of captured_at.

    A hearing date far from the capture date usually indicates a wrong
    date was extracted (e.g. a date from a different case or boilerplate).
    """
    if hearing_date is None or captured_at is None:
        return DeterministicRuleResult(rule="hearing_date_in_range", result="pass")

    delta = abs((hearing_date - captured_at).days)
    if delta > _HEARING_DATE_MAX_DELTA_DAYS:
        return DeterministicRuleResult(
            rule="hearing_date_in_range",
            result="fail",
            reason=f"hearing_date ({hearing_date}) is {delta} days from "
            f"captured_at ({captured_at}) — exceeds {_HEARING_DATE_MAX_DELTA_DAYS}-day threshold",
        )
    return DeterministicRuleResult(rule="hearing_date_in_range", result="pass")


def check_no_concatenated_titles(case_title: str | None) -> DeterministicRuleResult:
    """Check that case_title does not contain multiple v./vs. separators.

    A case title with multiple ``v.`` or ``vs.`` separators usually indicates
    multi-case contamination where titles from several cases were concatenated.
    """
    if not case_title:
        return DeterministicRuleResult(rule="no_concatenated_titles", result="pass")

    matches = _MULTI_VS_PATTERN.findall(case_title)
    if len(matches) > 1:
        return DeterministicRuleResult(
            rule="no_concatenated_titles",
            result="flag",
            reason=f"case_title contains {len(matches)} 'v.'/'vs.' separators — "
            "likely multi-case contamination",
        )
    return DeterministicRuleResult(rule="no_concatenated_titles", result="pass")


def check_ruling_text_not_empty(ruling_text: str | None) -> DeterministicRuleResult:
    """Check that ruling_text is not null or empty.

    An empty ruling text means extraction produced no content for this
    document, which may indicate a scraping or transcription failure.
    """
    if ruling_text is None or ruling_text.strip() == "":
        return DeterministicRuleResult(
            rule="ruling_text_not_empty",
            result="flag",
            reason="ruling_text is null or empty — extraction produced no content",
        )
    return DeterministicRuleResult(rule="ruling_text_not_empty", result="pass")


def check_case_number_not_unknown(case_number: str | None) -> DeterministicRuleResult:
    """Check that case_number does not start with UNKNOWN-.

    A synthetic UNKNOWN- prefix means extraction couldn't find the real
    case number, so the document ID was used as a placeholder.
    """
    if case_number is not None and case_number.startswith("UNKNOWN-"):
        return DeterministicRuleResult(
            rule="case_number_not_unknown",
            result="flag",
            reason="case_number starts with 'UNKNOWN-' — extraction couldn't find case number",
        )
    return DeterministicRuleResult(rule="case_number_not_unknown", result="pass")


def check_no_duplicate_ruling_text(
    ruling_text_lengths: list[int | None],
) -> DeterministicRuleResult:
    """Check that split rulings from the same document have distinct text lengths.

    When a multi-ruling document is split, each ruling should have a different
    ``ruling_text`` length.  If multiple rulings share the same length, it
    usually means the entire page was stored N times instead of individual
    rulings being split out.

    This rule operates at the **document level** (across all rulings from one
    document) rather than per-ruling.  It is called from the worker's
    ``_llm_split_document()`` method after conversion, not from
    ``run_deterministic_rules()`` (which runs per-ruling).

    Parameters
    ----------
    ruling_text_lengths:
        List of ``len(ruling_text)`` for each ruling in the document, or
        ``None`` for rulings with no text.  Must contain at least 2 entries
        for the check to be meaningful.
    """
    # Need at least 2 rulings to detect duplicates.
    if len(ruling_text_lengths) < 2:
        return DeterministicRuleResult(rule="no_duplicate_ruling_text", result="pass")

    # Filter out None values — rulings with no text are handled by
    # ruling_text_not_empty.
    non_none_lengths = [length for length in ruling_text_lengths if length is not None]

    if len(non_none_lengths) < 2:
        return DeterministicRuleResult(rule="no_duplicate_ruling_text", result="pass")

    # Count occurrences of each length.
    length_counts: dict[int, int] = {}
    for length in non_none_lengths:
        length_counts[length] = length_counts.get(length, 0) + 1

    # Find lengths that appear more than once.
    duplicates = {length: count for length, count in length_counts.items() if count > 1}

    if duplicates:
        # Build a human-readable summary of the duplicates.
        parts = [f"length {length} appears {count} times" for length, count in duplicates.items()]
        detail = "; ".join(parts)
        return DeterministicRuleResult(
            rule="no_duplicate_ruling_text",
            result="flag",
            reason=f"{len(non_none_lengths)} rulings from the same document have "
            f"duplicate ruling_text lengths ({detail}) — "
            "likely storing entire page N times instead of individual rulings",
        )

    return DeterministicRuleResult(rule="no_duplicate_ruling_text", result="pass")


def check_ruling_text_reasonable_length(ruling_text: str | None) -> DeterministicRuleResult:
    """Check that ruling_text length is not exactly the truncation sentinel.

    A ruling text length of exactly 50,000 characters suggests the entire
    page was stored rather than an individual ruling.
    """
    if ruling_text is not None and len(ruling_text) == _TRUNCATION_SENTINEL_LENGTH:
        return DeterministicRuleResult(
            rule="ruling_text_reasonable_length",
            result="flag",
            reason=f"ruling_text length is exactly {_TRUNCATION_SENTINEL_LENGTH} "
            "(truncation sentinel) — likely storing entire page",
        )
    return DeterministicRuleResult(rule="ruling_text_reasonable_length", result="pass")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_deterministic_rules(
    *,
    ruling_text: str | None,
    case_number: str | None,
    case_title: str | None,
    hearing_date: date | None,
    captured_at: date | None,
) -> DeterministicValidationResult:
    """Run all deterministic validation rules on a ruling.

    Parameters
    ----------
    ruling_text : str | None
        The ruling text to validate.
    case_number : str | None
        The extracted case number.
    case_title : str | None
        The extracted case title.
    hearing_date : date | None
        The extracted hearing date.
    captured_at : date | None
        The document's capture timestamp (as date).

    Returns
    -------
    DeterministicValidationResult
        Aggregated result with individual rule outcomes and overall verdict.
    """
    results: list[DeterministicRuleResult] = [
        check_no_html_in_ruling_text(ruling_text),
        check_hearing_date_in_range(hearing_date, captured_at),
        check_no_concatenated_titles(case_title),
        check_ruling_text_not_empty(ruling_text),
        check_case_number_not_unknown(case_number),
        check_ruling_text_reasonable_length(ruling_text),
    ]

    # Determine overall result: fail > flag > pass
    has_fail = any(r.result == "fail" for r in results)
    has_flag = any(r.result == "flag" for r in results)

    if has_fail:
        overall = "fail"
    elif has_flag:
        overall = "flag"
    else:
        overall = "pass"

    return DeterministicValidationResult(overall=overall, rules=results)
