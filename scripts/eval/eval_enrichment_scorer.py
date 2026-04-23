"""Enrichment eval scoring module.

Pure scoring functions for comparing LLM enrichment extraction results
against ground truth fixtures.  No I/O — all functions take data in and
return scored results.

Used by eval_enrichment.py for the enrichment eval pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Taxonomy constants — mirror llm_enrichment.py's MotionType/OutcomeType
# ---------------------------------------------------------------------------
# Fixtures may contain motion_type values outside the enrichment taxonomy
# (e.g. "anti_slapp", "motion_to_consolidate").  The LLM enrichment module
# maps these to "other".  The scorer must do the same normalization before
# comparing so that a correct "other" extraction is not penalized.

VALID_MOTION_TYPES: frozenset[str] = frozenset(
    {
        "msj",
        "mtd",
        "demurrer",
        "mil",
        "motion_to_compel",
        "motion_to_strike",
        "motion_for_attorney_fees",
        "motion_to_quash",
        "motion_for_summary_adjudication",
        "motion_to_compel_arbitration",
        "motion_for_new_trial",
        "motion_to_tax_costs",
        "motion_for_reconsideration",
        "motion_to_be_relieved_as_counsel",
        "motion_pro_hac_vice",
        "petition",
        "other",
    }
)

VALID_OUTCOMES: frozenset[str] = frozenset(
    {
        "granted",
        "denied",
        "granted_in_part",
        "denied_in_part",
        "moot",
        "continued",
        "off_calendar",
        "submitted",
        "other",
    }
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FieldScore:
    """Score for a single enrichment field comparison."""

    field_name: str
    expected: str | None
    extracted: str | None
    match: bool
    notes: str = ""


@dataclass
class PartyScore:
    """Score for party extraction recall."""

    expected_count: int
    found_count: int
    recall: float
    missing: list[str] = field(default_factory=list)


@dataclass
class FixtureScore:
    """Scores for all fields of a single enrichment fixture."""

    fixture_id: str
    county: str
    field_scores: list[FieldScore] = field(default_factory=list)
    party_score: PartyScore | None = None
    error: str | None = None


@dataclass
class CountyScore:
    """Aggregated scores for a county."""

    county: str
    total_fixtures: int = 0
    field_correct: dict[str, int] = field(default_factory=dict)
    field_total: dict[str, int] = field(default_factory=dict)
    party_recall_sum: float = 0.0
    party_recall_count: int = 0


@dataclass
class EnrichmentEvalSummary:
    """Overall enrichment evaluation summary."""

    model: str
    total_fixtures: int = 0
    fixture_scores: list[FixtureScore] = field(default_factory=list)
    county_scores: dict[str, CountyScore] = field(default_factory=dict)
    field_accuracy: dict[str, float] = field(default_factory=dict)
    field_correct: dict[str, int] = field(default_factory=dict)
    field_total: dict[str, int] = field(default_factory=dict)
    avg_party_recall: float = 0.0
    thresholds_passed: bool = True
    threshold_failures: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Quality thresholds (from issue spec)
# ---------------------------------------------------------------------------

THRESHOLDS: dict[str, float] = {
    "motion_type": 0.95,
    "outcome": 0.95,
    "case_title": 0.90,
    "parties": 0.85,
}


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _normalize_whitespace(val: str) -> str:
    """Collapse whitespace and strip."""
    return re.sub(r"\s+", " ", val).strip()


def _strip_et_al(val: str) -> str:
    """Remove 'et al.' and variants from a case title."""
    return re.sub(r",?\s*et\s+al\.?\s*$", "", val, flags=re.IGNORECASE).strip()


# ---------------------------------------------------------------------------
# Field comparison functions
# ---------------------------------------------------------------------------


def normalize_to_taxonomy(
    value: str | None,
    valid_values: frozenset[str],
) -> str | None:
    """Normalize a value to the enrichment taxonomy.

    If ``value`` is not in the taxonomy, map it to ``"other"``.
    This ensures that fixtures with non-taxonomy expected values
    (e.g. ``"anti_slapp"``) are correctly compared against LLM output
    (which always returns taxonomy values like ``"other"``).
    """
    if value is None:
        return None
    v_lower = value.strip().lower()
    if v_lower in valid_values:
        return v_lower
    return "other"


def compare_exact(expected: str | None, extracted: str | None) -> bool:
    """Case-insensitive exact match for motion_type and outcome."""
    if expected is None and extracted is None:
        return True
    if expected is None or extracted is None:
        return False
    return expected.strip().lower() == extracted.strip().lower()


def compare_with_alternatives(
    expected: str | None,
    extracted: str | None,
    alternatives: list[str] | None,
    valid_values: frozenset[str] | None = None,
) -> bool:
    """Compare with taxonomy normalization and acceptable alternatives.

    Steps:
    1. Normalize ``expected`` and ``extracted`` to the taxonomy (if
       ``valid_values`` is provided).
    2. Check exact match.
    3. Check if ``extracted`` matches any of the ``alternatives``
       (also normalized to the taxonomy).
    """
    # Normalize to taxonomy
    if valid_values is not None:
        expected = normalize_to_taxonomy(expected, valid_values)
        extracted_norm = normalize_to_taxonomy(extracted, valid_values)
    else:
        expected = expected.strip().lower() if expected else expected
        extracted_norm = extracted.strip().lower() if extracted else extracted

    # Direct match
    if compare_exact(expected, extracted_norm):
        return True

    # Check alternatives
    if alternatives:
        for alt in alternatives:
            alt_norm = (
                normalize_to_taxonomy(alt, valid_values)
                if valid_values
                else alt.strip().lower()
            )
            if compare_exact(alt_norm, extracted_norm):
                return True

    return False


def _levenshtein_ratio(s1: str, s2: str) -> float:
    """Compute Levenshtein similarity ratio using rapidfuzz.

    Returns a float in [0.0, 1.0] where 1.0 means identical strings.
    Falls back to difflib SequenceMatcher if rapidfuzz is not available.
    """
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    try:
        from rapidfuzz.fuzz import ratio as rf_ratio

        return rf_ratio(s1, s2) / 100.0
    except ImportError:
        from difflib import SequenceMatcher

        return SequenceMatcher(None, s1, s2).ratio()


def _token_overlap(s1: str, s2: str) -> float:
    """Compute token overlap ratio between two strings.

    Returns the fraction of tokens in common relative to the total
    unique tokens across both strings (Jaccard-like).
    """
    tokens1 = set(s1.lower().split())
    tokens2 = set(s2.lower().split())
    if not tokens1 and not tokens2:
        return 1.0
    if not tokens1 or not tokens2:
        return 0.0
    intersection = tokens1 & tokens2
    union = tokens1 | tokens2
    return len(intersection) / len(union)


def compare_case_title(expected: str | None, extracted: str | None) -> bool:
    """Fuzzy match for case_title.

    Normalize whitespace, strip "et al.", then compare with:
    - Token overlap >= 80%  OR
    - Levenshtein ratio >= 0.85
    """
    if expected is None and extracted is None:
        return True
    if expected is None or extracted is None:
        return False

    exp = _strip_et_al(_normalize_whitespace(expected)).lower()
    ext = _strip_et_al(_normalize_whitespace(extracted)).lower()

    # Normalize "vs." / "vs " / "versus" to "v."
    exp = re.sub(r"\bvs\.?\s+", "v. ", exp)
    exp = re.sub(r"\bversus\s+", "v. ", exp)
    ext = re.sub(r"\bvs\.?\s+", "v. ", ext)
    ext = re.sub(r"\bversus\s+", "v. ", ext)

    if exp == ext:
        return True

    token_ovlp = _token_overlap(exp, ext)
    if token_ovlp >= 0.80:
        return True

    lev_ratio = _levenshtein_ratio(exp, ext)
    return lev_ratio >= 0.85


def compute_party_recall(
    expected_parties: dict[str, list[str]],
    extracted_parties: dict[str, list[str]],
) -> PartyScore:
    """Compute party recall using fuzzy name matching.

    For each expected party (across plaintiffs and defendants), check if
    any extracted party matches with Levenshtein ratio >= 0.85.

    Parameters
    ----------
    expected_parties : dict
        ``{"plaintiffs": [...], "defendants": [...]}``
    extracted_parties : dict
        ``{"plaintiffs": [...], "defendants": [...]}``

    Returns
    -------
    PartyScore
        Recall score with details of missing parties.
    """
    all_expected: list[str] = []
    all_expected.extend(expected_parties.get("plaintiffs", []))
    all_expected.extend(expected_parties.get("defendants", []))

    all_extracted: list[str] = []
    all_extracted.extend(extracted_parties.get("plaintiffs", []))
    all_extracted.extend(extracted_parties.get("defendants", []))

    if not all_expected:
        return PartyScore(
            expected_count=0,
            found_count=0,
            recall=1.0,
        )

    found = 0
    missing: list[str] = []

    for exp_party in all_expected:
        exp_norm = _normalize_whitespace(exp_party).lower()
        matched = False
        for ext_party in all_extracted:
            ext_norm = _normalize_whitespace(ext_party).lower()
            ratio = _levenshtein_ratio(exp_norm, ext_norm)
            if ratio >= 0.85:
                matched = True
                break
        if matched:
            found += 1
        else:
            missing.append(exp_party)

    recall = found / len(all_expected)
    return PartyScore(
        expected_count=len(all_expected),
        found_count=found,
        recall=recall,
        missing=missing,
    )


# ---------------------------------------------------------------------------
# Fixture scoring
# ---------------------------------------------------------------------------

# Fields scored with exact match
_EXACT_FIELDS = ("motion_type", "outcome")


def score_fixture(
    fixture_id: str,
    county: str,
    expected: dict,
    extraction_result: dict | None,
    acceptable_alternatives: dict | None = None,
    *,
    case_title_in_text: bool = True,
) -> FixtureScore:
    """Score a single enrichment fixture against expected values.

    Parameters
    ----------
    fixture_id : str
        Identifier for this fixture (e.g., ``"la/sample-ruling-1"``).
    county : str
        The county name (e.g., ``"la"``).
    expected : dict
        The ``expected`` block from the fixture JSON.
    extraction_result : dict | None
        The enrichment extraction result dict with keys:
        ``case_title``, ``motion_type``, ``outcome``, ``parties``.
    acceptable_alternatives : dict | None
        Optional dict of field_name -> list[str] with acceptable
        alternative values for ambiguous fixtures.
    case_title_in_text : bool
        Whether the case title is present in the ruling text.  When
        ``False``, case_title and parties scoring are skipped for this
        fixture because the expected values cannot be extracted from
        the ruling text alone.  Default ``True``.

    Returns
    -------
    FixtureScore
    """
    score = FixtureScore(fixture_id=fixture_id, county=county)
    alternatives = acceptable_alternatives or {}

    if extraction_result is None:
        score.error = "No extraction result"
        return score

    # Map fields to their taxonomy sets for normalization
    _FIELD_TAXONOMY: dict[str, frozenset[str]] = {
        "motion_type": VALID_MOTION_TYPES,
        "outcome": VALID_OUTCOMES,
    }

    # Score exact-match fields with taxonomy normalization
    for fld in _EXACT_FIELDS:
        exp_val = expected.get(fld)
        ext_val = extraction_result.get(fld)
        field_alts = alternatives.get(fld)
        taxonomy = _FIELD_TAXONOMY.get(fld)

        match = compare_with_alternatives(
            exp_val,
            ext_val,
            field_alts,
            valid_values=taxonomy,
        )
        # Show the normalized expected value in the score for clarity
        display_expected = exp_val
        if taxonomy and exp_val is not None:
            normalized = normalize_to_taxonomy(exp_val, taxonomy)
            if normalized != exp_val:
                display_expected = f"{exp_val} (normalized: {normalized})"
        score.field_scores.append(
            FieldScore(
                field_name=fld,
                expected=display_expected,
                extracted=ext_val,
                match=match,
            )
        )

    # Score case_title (fuzzy match + alternatives)
    # Skip if case_title is not present in the ruling text — the expected
    # value was derived from scraper metadata and cannot be extracted from
    # the ruling text alone.
    if case_title_in_text:
        exp_title = expected.get("case_title")
        ext_title = extraction_result.get("case_title")
        title_alts = alternatives.get("case_title")
        title_match = compare_case_title(exp_title, ext_title)
        if not title_match and title_alts:
            for alt in title_alts:
                if compare_case_title(alt, ext_title):
                    title_match = True
                    break
        score.field_scores.append(
            FieldScore(
                field_name="case_title",
                expected=exp_title,
                extracted=ext_title,
                match=title_match,
            )
        )

    # Score parties (recall) — skip if case_title is not in text, since
    # party names are derived from the same case caption metadata
    if case_title_in_text:
        exp_parties = expected.get("parties", {})
        ext_parties = extraction_result.get("parties", {})
        score.party_score = compute_party_recall(exp_parties, ext_parties)

    return score


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate_scores(
    fixture_scores: list[FixtureScore],
    model: str,
) -> EnrichmentEvalSummary:
    """Aggregate fixture scores into county and overall summaries."""
    summary = EnrichmentEvalSummary(
        model=model,
        total_fixtures=len(fixture_scores),
        fixture_scores=fixture_scores,
    )

    counties: dict[str, CountyScore] = {}
    global_correct: dict[str, int] = {}
    global_total: dict[str, int] = {}
    all_party_recalls: list[float] = []

    for fs in fixture_scores:
        if fs.county not in counties:
            counties[fs.county] = CountyScore(county=fs.county)
        cs = counties[fs.county]
        cs.total_fixtures += 1

        for field_score in fs.field_scores:
            fld = field_score.field_name
            for d in [global_correct, global_total, cs.field_correct, cs.field_total]:
                d.setdefault(fld, 0)

            global_total[fld] += 1
            cs.field_total[fld] += 1

            if field_score.match:
                global_correct[fld] += 1
                cs.field_correct[fld] += 1

        if fs.party_score is not None and fs.party_score.expected_count > 0:
            all_party_recalls.append(fs.party_score.recall)
            cs.party_recall_sum += fs.party_score.recall
            cs.party_recall_count += 1

    # Compute per-field accuracy
    for fld in sorted(global_total.keys()):
        total = global_total.get(fld, 0)
        correct = global_correct.get(fld, 0)
        summary.field_accuracy[fld] = correct / total if total > 0 else 0.0
        summary.field_correct[fld] = correct
        summary.field_total[fld] = total

    summary.avg_party_recall = (
        sum(all_party_recalls) / len(all_party_recalls) if all_party_recalls else 0.0
    )
    summary.county_scores = counties

    _check_thresholds(summary)

    return summary


def _check_thresholds(summary: EnrichmentEvalSummary) -> None:
    """Check if quality thresholds are met."""
    failures: list[str] = []

    for fld in ("motion_type", "outcome", "case_title"):
        acc = summary.field_accuracy.get(fld, 0.0)
        threshold = THRESHOLDS[fld]
        if acc < threshold:
            failures.append(f"{fld} accuracy {acc:.1%} < {threshold:.0%}")

    if summary.avg_party_recall < THRESHOLDS["parties"]:
        failures.append(
            f"parties recall {summary.avg_party_recall:.1%} < {THRESHOLDS['parties']:.0%}"
        )

    summary.thresholds_passed = len(failures) == 0
    summary.threshold_failures = failures


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_text_report(summary: EnrichmentEvalSummary) -> str:
    """Format a human-readable enrichment evaluation report."""
    lines: list[str] = [
        "=== Enrichment Eval Results ===",
        f"Model: {summary.model}",
        f"Fixtures: {summary.total_fixtures}",
        "",
        "Per-field accuracy:",
    ]

    for fld in ("motion_type", "outcome", "case_title"):
        correct = summary.field_correct.get(fld, 0)
        total = summary.field_total.get(fld, 0)
        acc = summary.field_accuracy.get(fld, 0.0)
        threshold = THRESHOLDS[fld]
        status = "PASS" if acc >= threshold else "FAIL"
        lines.append(
            f"  {fld}: {correct}/{total} ({acc:.1%})  "
            f"[{status}: target {threshold:.0%}]"
        )

    # Parties recall
    threshold = THRESHOLDS["parties"]
    status = "PASS" if summary.avg_party_recall >= threshold else "FAIL"
    party_fixtures = sum(
        1
        for fs in summary.fixture_scores
        if fs.party_score is not None and fs.party_score.expected_count > 0
    )
    party_passed = sum(
        1
        for fs in summary.fixture_scores
        if fs.party_score is not None
        and fs.party_score.expected_count > 0
        and fs.party_score.recall >= 1.0
    )
    lines.append(
        f"  parties: {party_passed}/{party_fixtures} ({summary.avg_party_recall:.1%})  "
        f"[{status}: target {threshold:.0%}]"
    )

    # Per-county breakdown
    lines.extend(["", "Per-county breakdown:"])
    for county_name in sorted(summary.county_scores.keys()):
        cs = summary.county_scores[county_name]
        parts = [f"  {county_name}: {cs.total_fixtures} fixtures"]
        for fld in ("motion_type", "outcome", "case_title"):
            correct = cs.field_correct.get(fld, 0)
            total = cs.field_total.get(fld, 0)
            acc = correct / total if total > 0 else 0.0
            parts.append(f"{fld} {acc:.0%}")
        if cs.party_recall_count > 0:
            avg_recall = cs.party_recall_sum / cs.party_recall_count
            parts.append(f"parties {avg_recall:.0%}")
        lines.append(", ".join(parts))

    # Failures detail
    failures = []
    for fs in summary.fixture_scores:
        for field_score in fs.field_scores:
            if not field_score.match:
                failures.append(
                    f"  {fs.fixture_id}: {field_score.field_name} "
                    f'expected="{field_score.expected}" '
                    f'got="{field_score.extracted}"'
                )
        if fs.party_score is not None and fs.party_score.missing:
            failures.append(
                f"  {fs.fixture_id}: parties missing={fs.party_score.missing}"
            )

    if failures:
        lines.extend(["", "Failures:"])
        lines.extend(failures)

    # Threshold summary
    if not summary.thresholds_passed:
        lines.extend(["", "Threshold check FAILED:"])
        for failure in summary.threshold_failures:
            lines.append(f"  - {failure}")
    else:
        lines.extend(["", "All thresholds PASSED."])

    return "\n".join(lines)


def format_json_report(summary: EnrichmentEvalSummary) -> dict:
    """Format evaluation results as a JSON-serializable dict."""
    return {
        "model": summary.model,
        "total_fixtures": summary.total_fixtures,
        "field_accuracy": summary.field_accuracy,
        "field_correct": summary.field_correct,
        "field_total": summary.field_total,
        "avg_party_recall": summary.avg_party_recall,
        "thresholds_passed": summary.thresholds_passed,
        "threshold_failures": summary.threshold_failures,
        "county_scores": {
            name: {
                "county": cs.county,
                "total_fixtures": cs.total_fixtures,
                "field_correct": cs.field_correct,
                "field_total": cs.field_total,
                "avg_party_recall": (
                    cs.party_recall_sum / cs.party_recall_count
                    if cs.party_recall_count > 0
                    else 0.0
                ),
            }
            for name, cs in summary.county_scores.items()
        },
        "fixture_scores": [
            {
                "fixture_id": fs.fixture_id,
                "county": fs.county,
                "error": fs.error,
                "field_scores": [
                    {
                        "field_name": f.field_name,
                        "expected": f.expected,
                        "extracted": f.extracted,
                        "match": f.match,
                    }
                    for f in fs.field_scores
                ],
                "party_score": (
                    {
                        "expected_count": fs.party_score.expected_count,
                        "found_count": fs.party_score.found_count,
                        "recall": fs.party_score.recall,
                        "missing": fs.party_score.missing,
                    }
                    if fs.party_score is not None
                    else None
                ),
            }
            for fs in summary.fixture_scores
        ],
    }
