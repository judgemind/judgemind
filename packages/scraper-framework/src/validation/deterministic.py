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

# Pattern to detect multiple v./vs./vs separators in a case title,
# indicating multi-case contamination.  Matches (case-insensitive):
#
#   - ``v.`` followed by whitespace        (e.g. "Smith v. Jones")
#   - ``vs.`` followed by whitespace       (e.g. "TAYLOR VS. AMAZON")
#   - ``vs`` (no period) with whitespace on both sides  (e.g. "Yin vs Lu")
#
# The bare ``vs`` form requires whitespace on BOTH sides (``\bvs\b`` plus
# explicit leading/trailing whitespace) to avoid false positives from words
# containing "vs" (e.g. "Versus" → the ``\b`` handles this, but being strict
# also excludes tokens like "vs.Anything" that are covered by the ``vs.``
# branch above).  #2398.
_MULTI_VS_PATTERN = re.compile(
    r"(?:\bv\.\s|\bvs\.\s|\s+vs\s+)",
    re.IGNORECASE,
)

# Broad California court case-number pattern used to scan ruling_text for
# candidate case-number tokens.  This intentionally over-matches — the
# actual contamination check is driven by comparing normalised candidates
# against the caller-supplied ``other_case_numbers`` list, not by the
# pattern's precision.
#
# Covers (case-insensitive, examples):
#   23STCV12345, 24CECG12345, 23CV123456, 24PR123456        (LA, Fresno, Santa Clara)
#   37-2023-12345                                            (San Diego)
#   CGC12345678, RIC1234567, CIV1234567                      (SF civil, Riverside)
#   2024CUBC123456                                           (Ventura)
#   C21-01234                                                (Contra Costa)
#   FAM-23-123456                                            (SF family)
#   CIVSB12345                                               (Santa Barbara)
#   01234567 (leading-0 7-digit)                             (OC probate)
#   23D123456                                                (OC family)
_CASE_NUMBER_CANDIDATE_PATTERN = re.compile(
    r"""
    \b(?:
        \d{2,4}-\d{2,4}-\d{5,10}        # dashed: 37-2023-12345, FAM-23-123456 variants
        | [A-Z]{3,4}-\d{2}-\d{6}        # dashed: FAM-23-123456 (letters-first)
        | [CLNP]\d{2}-\d{4,5}           # Contra Costa: C21-01234
        | \d{4}[A-Z]{3,5}\d{5,8}        # Ventura: 2024CUBC123456
        | \d{2}[A-Z]{2,6}\d{4,10}       # LA/Fresno/SC: 23STCV12345
        | \d{2}D\d{6}                   # OC family: 23D123456
        | CIV[A-Z]{2}\s?\d{5,8}         # Santa Barbara: CIVSB12345
        | [A-Z]{3,4}\d{6,10}            # SF civil/Riverside: CGC12345678
        | 0\d{7}                        # OC probate: 7-digit with leading 0
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Pattern for the "Superior Court Case No." marker commonly present in Fresno
# (and some other CA county) ruling header blocks.  Used by
# ``check_ruling_text_case_number_marker`` (#2402) to flag rulings whose text
# embeds a DIFFERENT case number than the ruling's own ``case_number`` —
# strong evidence of LLM cross-case misattribution even when the referenced
# number is not a known sibling (e.g. when the LLM dropped an entry entirely
# so the "wrong" number never reaches the sibling list).
#
# Matches (case-insensitive): "Superior Court Case No.", "Superior Court Case
# No ", "Superior Court Case Nos.", "Court Case No.", with optional trailing
# period and flexible whitespace before the case number.  The captured group
# is the raw case-number token (uppercase letters, digits, hyphens); the
# caller normalises via ``_normalize_case_number`` before comparison.
_SUPERIOR_COURT_CASE_NO_PATTERN = re.compile(
    r"(?:Superior\s+)?Court\s+Case\s+Nos?\.?\s+([A-Z0-9][A-Z0-9\-]{3,})",
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


def check_no_multiple_adversarial_patterns(case_title: str | None) -> DeterministicRuleResult:
    """Check that case_title does not contain multiple adversarial (v./vs./vs) patterns.

    A case title with two or more ``v.``, ``vs.``, or ``vs`` separators usually
    indicates multi-case contamination where titles from several cases were
    concatenated during LLM extraction from a multi-case document (#2371, #2398).

    Examples of contaminated titles this rule flags:

    - ``"TAYLOR VS. AMAZON MSC21-02349 Amazon.com, Inc. v. Damien Sean Lamont Ross"``
      (``VS.`` and ``v.`` both present → 2 matches).
    - ``"Liangbei Wang v. NetEase, Inc., et al. Manuel Panilag v. Armando Contreras
      et al. Jin Yin, et al vs Xiaoxiao Lu, et al."`` (three separators: two
      ``v.`` and one bare ``vs`` → 3 matches).

    Matches ``v.``, ``vs.``, and bare ``vs`` tokens followed by whitespace,
    case-insensitive, using word boundaries (``\\b``) to avoid matching ``v.``
    or ``vs`` in the middle of words.  Threshold: 2 or more matches → ``flag``.
    """
    if not case_title:
        return DeterministicRuleResult(rule="no_multiple_adversarial_patterns", result="pass")

    matches = _MULTI_VS_PATTERN.findall(case_title)
    if len(matches) >= 2:
        return DeterministicRuleResult(
            rule="no_multiple_adversarial_patterns",
            result="flag",
            reason=f"case_title contains {len(matches)} 'v.'/'vs.' separators — "
            "likely multi-case contamination",
        )
    return DeterministicRuleResult(rule="no_multiple_adversarial_patterns", result="pass")


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


def check_unknown_and_null_title_fail(
    case_number: str | None,
    case_title: str | None,
) -> DeterministicRuleResult:
    """Reject rulings with BOTH a synthetic UNKNOWN- case_number AND a null/empty title.

    This is a narrow, high-confidence rejection rule (#2571).  When both the
    case number and the case title are missing after all extraction and
    enrichment paths have run, the ruling is almost certainly an orphaned
    fragment of a bad LLM split — for example, a multi-Issue MSJ/MSA ruling
    that was over-split into per-Issue fragments, each of which lacks case
    metadata because that only appears at the top of the parent entry.

    Storing such fragments pollutes the DB with UNKNOWN-prefixed cases and
    wrong motion types.  This rule returns ``fail`` (not ``flag``) so the
    caller skips the DB write entirely, ensuring that the 3-way extraction
    failure (UNKNOWN case_number + NULL title + wrong motion_type) described
    in #2571 cannot reach ``derived.rulings``.

    The sibling rule ``check_case_number_not_unknown`` keeps its ``flag``
    behaviour for rulings that have a title but no case_number — those can
    still be healed later by cross-case title lookup and are safer to keep.
    """
    has_unknown_case_number = case_number is not None and case_number.startswith("UNKNOWN-")
    has_empty_title = case_title is None or not case_title.strip()

    if has_unknown_case_number and has_empty_title:
        return DeterministicRuleResult(
            rule="unknown_and_null_title_fail",
            result="fail",
            reason=(
                "case_number starts with 'UNKNOWN-' AND case_title is "
                "null/empty — ruling is almost certainly a bad-split "
                "fragment (see #2571); rejecting to protect DB integrity"
            ),
        )
    return DeterministicRuleResult(rule="unknown_and_null_title_fail", result="pass")


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


def _normalize_case_number(raw: str | None) -> str:
    """Return an uppercase canonical form of a case number for comparison.

    Strips whitespace, uppercases, and removes internal spaces and hyphens
    so that format variations (``"37-2023-12345"`` vs ``"372023 12345"`` vs
    ``"37202312345"``) all compare equal.  Returns an empty string for
    ``None`` or all-whitespace input.
    """
    if raw is None:
        return ""
    stripped = raw.strip()
    if not stripped:
        return ""
    # Remove internal whitespace and hyphens, then uppercase.
    return re.sub(r"[\s\-]+", "", stripped).upper()


def check_no_cross_case_ruling_text(
    ruling_text: str | None,
    own_case_number: str | None,
    other_case_numbers: list[str],
) -> DeterministicRuleResult:
    """Check that ruling_text does not reference a sibling case's number.

    On multi-case calendar documents, the transcription/split step may
    associate one case's ruling text with another case's metadata.  This
    rule scans ``ruling_text`` for California court case-number tokens and
    flags the ruling when any candidate token normalises to one of the
    caller-supplied ``other_case_numbers`` (which should be the case
    numbers from sibling rulings on the same document) **without** also
    matching ``own_case_number``.

    Parameters
    ----------
    ruling_text:
        The ruling text to scan.
    own_case_number:
        The extracted case number of this ruling.  May be ``None`` or a
        synthetic ``UNKNOWN-`` placeholder.
    other_case_numbers:
        Case numbers from sibling rulings on the same document.  Caller is
        responsible for excluding ``own_case_number`` from this list;
        defensive filtering is applied anyway.

    Returns
    -------
    DeterministicRuleResult
        ``flag`` if a sibling case number is referenced in the text;
        ``pass`` otherwise.  Always passes on empty/None inputs.
    """
    if not ruling_text or not other_case_numbers:
        return DeterministicRuleResult(rule="no_cross_case_ruling_text", result="pass")

    own_norm = _normalize_case_number(own_case_number)

    # Normalise and de-duplicate sibling case numbers.  Exclude any that
    # match own_case_number after normalisation (defensive), and drop empties.
    other_norm: set[str] = set()
    for other in other_case_numbers:
        norm = _normalize_case_number(other)
        if norm and norm != own_norm:
            other_norm.add(norm)

    if not other_norm:
        return DeterministicRuleResult(rule="no_cross_case_ruling_text", result="pass")

    # Scan ruling_text for candidate case-number tokens and normalise each.
    # Track which sibling case numbers are referenced.
    matched: set[str] = set()
    for match in _CASE_NUMBER_CANDIDATE_PATTERN.finditer(ruling_text):
        candidate_norm = _normalize_case_number(match.group(0))
        if not candidate_norm:
            continue
        if candidate_norm == own_norm:
            # Own case number in own ruling text — expected, ignore.
            continue
        if candidate_norm in other_norm:
            matched.add(candidate_norm)

    if matched:
        # Sort for stable message ordering; show raw normalised forms.
        matched_sorted = sorted(matched)
        return DeterministicRuleResult(
            rule="no_cross_case_ruling_text",
            result="flag",
            reason=f"ruling_text references {len(matched_sorted)} other case "
            f"number(s) from the same document ({', '.join(matched_sorted)}) — "
            "likely cross-case ruling text misattribution",
        )

    return DeterministicRuleResult(rule="no_cross_case_ruling_text", result="pass")


def check_ruling_text_case_number_marker(
    ruling_text: str | None,
    own_case_number: str | None,
) -> DeterministicRuleResult:
    """Check that any "Superior Court Case No." marker matches own case number.

    Fresno (and similar CA county) ruling PDFs embed a case-number marker
    directly in the header block of each ruling, e.g.::

        Re:  Lopez v. Fresno Unified School District
             Superior Court Case No. 25CECG03271

    When the LLM extracts ruling_text, the marker travels with the body.  If
    the LLM then attaches that body to a DIFFERENT case's metadata, the
    embedded marker no longer matches the ruling's ``case_number``.  This is
    a strong signal of cross-case misattribution even when the referenced
    number does not appear on the sibling list (e.g. when the LLM dropped an
    entry entirely — #2402).

    Unlike ``check_no_cross_case_ruling_text``, this rule does **not** require
    a sibling context.  It only needs the ruling's own case number.

    Parameters
    ----------
    ruling_text:
        The ruling text to scan for ``Superior Court Case No. XXXXX`` markers.
    own_case_number:
        The ruling's extracted case number.  Must be non-empty for the rule
        to flag — without an own number, there is nothing to compare against.

    Returns
    -------
    DeterministicRuleResult
        ``flag`` if any marker references a case number that normalises to a
        different value than ``own_case_number``; ``pass`` otherwise.
    """
    if not ruling_text:
        return DeterministicRuleResult(rule="ruling_text_case_number_marker", result="pass")

    own_norm = _normalize_case_number(own_case_number)
    if not own_norm:
        # Without a reliable own case number, we cannot compare.
        return DeterministicRuleResult(rule="ruling_text_case_number_marker", result="pass")

    mismatched: set[str] = set()
    for match in _SUPERIOR_COURT_CASE_NO_PATTERN.finditer(ruling_text):
        marker_raw = match.group(1)
        marker_norm = _normalize_case_number(marker_raw)
        if not marker_norm:
            continue
        if marker_norm != own_norm:
            mismatched.add(marker_norm)

    if mismatched:
        mismatched_sorted = sorted(mismatched)
        own_display = _normalize_case_number(own_case_number) or "<none>"
        return DeterministicRuleResult(
            rule="ruling_text_case_number_marker",
            result="flag",
            reason=f"ruling_text contains 'Superior Court Case No.' marker(s) "
            f"for {len(mismatched_sorted)} different case number(s) "
            f"({', '.join(mismatched_sorted)}) but the ruling's own "
            f"case_number is {own_display} — likely cross-case ruling text "
            "misattribution",
        )

    return DeterministicRuleResult(rule="ruling_text_case_number_marker", result="pass")


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
    other_case_numbers: list[str] | None = None,
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
    other_case_numbers : list[str] | None
        Case numbers from sibling rulings on the same multi-case document.
        When provided and non-empty, enables the
        ``no_cross_case_ruling_text`` rule which flags rulings whose text
        references another case's number (#2371).  Existing callers that
        omit this kwarg keep their previous behaviour.

    Returns
    -------
    DeterministicValidationResult
        Aggregated result with individual rule outcomes and overall verdict.
    """
    results: list[DeterministicRuleResult] = [
        check_no_html_in_ruling_text(ruling_text),
        check_hearing_date_in_range(hearing_date, captured_at),
        check_no_multiple_adversarial_patterns(case_title),
        check_ruling_text_not_empty(ruling_text),
        check_case_number_not_unknown(case_number),
        check_unknown_and_null_title_fail(case_number, case_title),
        check_ruling_text_reasonable_length(ruling_text),
        check_ruling_text_case_number_marker(ruling_text, case_number),
    ]

    # Cross-case contamination check only runs when the caller supplies
    # sibling case numbers (i.e. document-level context is available).
    if other_case_numbers:
        results.append(
            check_no_cross_case_ruling_text(
                ruling_text=ruling_text,
                own_case_number=case_number,
                other_case_numbers=other_case_numbers,
            )
        )

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
