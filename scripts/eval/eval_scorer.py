"""Extraction eval scoring module.

Pure scoring functions for comparing LLM extraction results against
ground truth fixtures. No I/O — all functions take data in and return
scored results.

Used by run_extraction_eval.py for the production eval pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FieldScore:
    """Score for a single field comparison."""

    field_name: str
    expected: str | None
    extracted: str | None
    match: bool
    notes: str = ""


@dataclass
class FixtureScore:
    """Scores for all fields of a single fixture."""

    fixture_name: str
    county: str
    field_scores: list[FieldScore] = field(default_factory=list)
    ruling_text_similarity: float = 0.0
    ruling_text_hallucination: bool = False
    party_recall: float = 0.0
    party_recall_measured: bool = False
    error: str | None = None


@dataclass
class CountyScore:
    """Aggregated scores for a county."""

    county: str
    total_fixtures: int = 0
    field_correct: dict[str, int] = field(default_factory=dict)
    field_total: dict[str, int] = field(default_factory=dict)
    avg_ruling_text_similarity: float = 0.0
    hallucination_count: int = 0
    avg_party_recall: float = 0.0


@dataclass
class EvalSummary:
    """Overall evaluation summary."""

    model: str
    total_fixtures: int = 0
    fixture_scores: list[FixtureScore] = field(default_factory=list)
    county_scores: dict[str, CountyScore] = field(default_factory=dict)
    field_accuracy: dict[str, float] = field(default_factory=dict)
    avg_ruling_text_similarity: float = 0.0
    total_hallucinations: int = 0
    avg_party_recall: float = 0.0
    thresholds_passed: bool = True
    threshold_failures: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Quality thresholds
# ---------------------------------------------------------------------------

THRESHOLDS: dict[str, float] = {
    "case_number": 0.95,
    "case_title": 0.90,
    "party_recall": 0.85,
    "ruling_text_similarity": 0.95,
    "outcome": 0.90,
    "hallucination_count": 0,  # Zero tolerance
}


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def normalize_unicode(val: str) -> str:
    """Replace common Unicode variants with ASCII equivalents."""
    replacements = {
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2018": "'",  # left single quote
        "\u2019": "'",  # right single quote
        "\u201c": '"',  # left double quote
        "\u201d": '"',  # right double quote
    }
    for old, new in replacements.items():
        val = val.replace(old, new)
    return val


def normalize_value(val: str | None) -> str | None:
    """Normalize a value for comparison: strip, lowercase, handle null."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("null", "none", ""):
        return None
    return normalize_unicode(s)


def normalize_case_number(val: str | None) -> str | None:
    """Normalize case number: remove county prefix, spaces."""
    if val is None:
        return None
    s = str(val).strip().replace(" ", "")
    # Remove 2-3 digit county prefix before dash (e.g., "30-2024-01393434" -> "2024-01393434")
    # Only strip 2-3 digit prefixes to avoid stripping 4-digit year prefixes
    m = re.match(r"^\d{2,3}-(.+)$", s)
    if m:
        return m.group(1)
    return s


def normalize_department(val: str | None) -> str | None:
    """Normalize department: case-insensitive, preserve leading zeros."""
    if val is None:
        return None
    return str(val).strip().upper()


def normalize_date(val: str | None) -> str | None:
    """Normalize date values to ISO format for comparison."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("null", "none"):
        return None
    for fmt in ["%Y-%m-%d", "%B %d, %Y", "%B %d %Y", "%m/%d/%Y", "%m/%d/%y"]:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Could not parse — return None so compare_field treats it as a mismatch
    return None


def normalize_judge(val: str | None) -> str | None:
    """Normalize judge name: remove honorific prefix, lowercase."""
    if val is None:
        return None
    s = str(val).strip()
    for prefix in [
        "Hon. ",
        "HON. ",
        "Hon ",
        "HON ",
        "Honorable ",
        "HONORABLE ",
        "Judge ",
        "JUDGE ",
    ]:
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    return normalize_unicode(s.lower().strip())


def normalize_outcome(val: str | None) -> str | None:
    """Normalize outcome values for comparison."""
    if val is None:
        return None
    s = str(val).strip().lower()
    s = s.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", s).strip()


def normalize_case_title(val: str | None) -> str | None:
    """Normalize case title for fuzzy comparison."""
    if val is None:
        return None
    s = str(val).strip().lower()
    s = normalize_unicode(s)
    # Normalize "vs." / "vs " / "versus" to "v."
    s = re.sub(r"\bvs\.?\s+", "v. ", s)
    s = re.sub(r"\bversus\s+", "v. ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---------------------------------------------------------------------------
# Field comparison
# ---------------------------------------------------------------------------


def compare_field(
    field_name: str,
    extracted: str | int | float | None,
    expected: str | int | float | None,
) -> bool:
    """Compare an extracted field value against expected, with field-specific
    normalization."""
    ext = normalize_value(str(extracted) if extracted is not None else None)
    exp = normalize_value(str(expected) if expected is not None else None)

    if ext is None and exp is None:
        return True
    if ext is None or exp is None:
        return False

    if field_name == "judge_name":
        return normalize_judge(ext) == normalize_judge(exp)

    if field_name == "hearing_date":
        return normalize_date(ext) == normalize_date(exp)

    if field_name == "department":
        return normalize_department(ext) == normalize_department(exp)

    if field_name in ("case_number", "primary_case_number"):
        return normalize_case_number(ext) == normalize_case_number(exp)

    if field_name == "case_count":
        try:
            return int(ext) == int(exp)
        except (ValueError, TypeError):
            return ext == exp

    if field_name == "outcome":
        return normalize_outcome(ext) == normalize_outcome(exp)

    if field_name == "case_title":
        from difflib import SequenceMatcher

        e1 = normalize_case_title(ext)
        e2 = normalize_case_title(exp)
        if e1 is None or e2 is None:
            return False
        # Fuzzy: use SequenceMatcher ratio (threshold 0.8)
        return SequenceMatcher(None, e1, e2).ratio() > 0.8

    return ext.lower() == exp.lower()


# ---------------------------------------------------------------------------
# Ruling text scoring
# ---------------------------------------------------------------------------


def compute_text_similarity(
    extracted: str | None,
    source: str | None,
) -> float:
    """Compute character-level similarity between extracted text and source.

    Returns a float in [0.0, 1.0]. Uses SequenceMatcher for shorter texts
    and a chunked containment check for very long texts.
    """
    if extracted is None or source is None:
        return 0.0
    ext = re.sub(r"\s+", " ", extracted.strip().lower())
    src = re.sub(r"\s+", " ", source.strip().lower())
    if not ext or not src:
        return 0.0
    if ext == src:
        return 1.0

    # For very long texts, use chunked containment check for performance
    if len(ext) > 10000 or len(src) > 10000:
        # Check what fraction of extracted text is in source
        common = 0
        for i in range(0, len(ext), 100):
            chunk = ext[i : i + 100]
            if chunk in src:
                common += len(chunk)
        return common / len(ext) if ext else 0.0

    # For shorter texts, use SequenceMatcher ratio
    from difflib import SequenceMatcher

    return SequenceMatcher(None, ext, src).ratio()


def check_ruling_text_contains(
    extracted_text: str | None,
    expected_contains: list[str],
) -> tuple[bool, list[str]]:
    """Check if extracted ruling text contains all expected substrings.

    Returns (all_found, missing_substrings).
    """
    if extracted_text is None:
        return False, expected_contains

    text_lower = extracted_text.lower()
    missing = []
    for substring in expected_contains:
        if substring.lower() not in text_lower:
            missing.append(substring)

    return len(missing) == 0, missing


def check_hallucination(
    ruling_text: str | None,
    source_text: str | None,
    *,
    threshold: float = 0.95,
) -> bool:
    """Check if ruling text contains hallucinated content.

    Returns True if hallucination is detected (ruling text contains
    significant content not found in source).

    Uses a simple heuristic: splits ruling text into sentences and checks
    if each sentence (or a significant portion) appears in the source.
    """
    if ruling_text is None or source_text is None:
        return False

    ruling_norm = re.sub(r"\s+", " ", ruling_text.strip().lower())
    source_norm = re.sub(r"\s+", " ", source_text.strip().lower())

    if not ruling_norm:
        return False

    # Split ruling text into chunks and check each against source
    # Use sentence-like splitting
    sentences = re.split(r"[.!?]+\s+", ruling_norm)
    sentences = [s.strip() for s in sentences if s.strip()]

    if not sentences:
        return False

    found_count = 0
    for sentence in sentences:
        words = sentence.split()
        if not words:
            continue
        # For very short fragments (1-2 words), do a direct substring check
        if len(words) <= 2:
            if sentence in source_norm:
                found_count += 1
            continue
        # Check sliding windows of words against source text
        window_size = max(3, len(words) // 2)
        found = False
        for i in range(len(words) - window_size + 1):
            window = " ".join(words[i : i + window_size])
            if window in source_norm:
                found = True
                break
        if found:
            found_count += 1

    ratio = found_count / len(sentences) if sentences else 1.0
    return ratio < threshold


# ---------------------------------------------------------------------------
# Fixture scoring
# ---------------------------------------------------------------------------


# Fixtures to skip — index pages, error pages, access-denied
SKIP_FIXTURES = {
    "la_main_page.json",
    "la_ruling_response.json",
    "oc_civil_page.json",
    "oc_family_law_page.json",
    "oc_probate_page.json",
    "riv_page.json",
    "sb_civil_page.json",
    "sb_iframe_page.json",
    "sc_landing_page.json",
    "sc_dept1_page.json",
    "sc_dept6_page.json",
    "sc_dept16_page.json",
    "sf_family_law_page.json",
    "sf_captcha_gate.json",
}

# Known fixture issues — mismatches caused by fixture inconsistencies
KNOWN_FIXTURE_ISSUES: set[tuple[str, str]] = {
    ("oc_north_n.pdf", "judge_name"),
    ("oc_north_n.pdf", "department"),
    ("oc_north_n.pdf", "case_count"),
    ("oc_family_law_claustro_c22.pdf", "outcome"),
}


def should_skip_fixture(fixture_name: str) -> bool:
    """Check if a fixture should be skipped (index/error pages)."""
    return fixture_name in SKIP_FIXTURES


def get_county_from_fixture(fixture_name: str) -> str:
    """Extract county name from fixture filename.

    Convention: prefix before first underscore maps to county.
    """
    prefix_map = {
        "la": "Los Angeles",
        "oc": "Orange",
        "riv": "Riverside",
        "sb": "Santa Barbara",
        "sc": "Santa Clara",
        "sd": "San Diego",
        "sf": "San Francisco",
        "cc": "Contra Costa",
        "fresno": "Fresno",
        "ventura": "Ventura",
    }
    name = fixture_name.lower()
    for prefix, county in sorted(prefix_map.items(), key=lambda x: -len(x[0])):
        if name.startswith(prefix + "_"):
            return county
    return "Unknown"


# Document-level fields scored from expected JSON
DOC_FIELDS = ["judge_name", "hearing_date", "department", "case_count"]

# Ruling-level fields scored from first ruling
RULING_FIELDS = ["case_number", "case_title", "outcome"]


def score_fixture(
    fixture_name: str,
    extraction_result: dict,
    expected: dict,
    source_text: str | None = None,
) -> FixtureScore:
    """Score a single fixture's extraction results against expected values."""
    county = get_county_from_fixture(fixture_name)
    score = FixtureScore(fixture_name=fixture_name, county=county)

    if extraction_result is None:
        score.error = "No extraction result"
        return score

    rulings = extraction_result.get("rulings", [])
    first_ruling = rulings[0] if rulings else {}

    # Score document-level fields
    for fld in DOC_FIELDS:
        exp_val = expected.get(fld)
        if fld not in expected:
            continue

        ext_val = extraction_result.get(fld) or extraction_result.get(f"extracted_{fld}")

        # If expected is None, skip scoring — we cannot validate
        # correctness without ground truth
        if exp_val is None:
            continue

        match = compare_field(fld, ext_val, exp_val)
        is_known_issue = (fixture_name, fld) in KNOWN_FIXTURE_ISSUES
        score.field_scores.append(
            FieldScore(
                field_name=fld,
                expected=str(exp_val) if exp_val is not None else None,
                extracted=str(ext_val) if ext_val is not None else None,
                match=match,
                notes="known_fixture_issue" if is_known_issue else "",
            )
        )

    # Score ruling-level fields (from first ruling)
    for fld in RULING_FIELDS:
        if fld == "case_number":
            exp_val = expected.get("primary_case_number")
            if exp_val is None and "primary_case_number" not in expected:
                continue
        else:
            exp_val = expected.get(fld)
            if fld not in expected:
                continue

        ext_val = first_ruling.get(fld) or first_ruling.get(f"extracted_{fld}")

        # If expected is None, skip scoring — we cannot validate
        # correctness without ground truth
        if exp_val is None:
            continue

        match = compare_field(fld, ext_val, exp_val)
        is_known_issue = (fixture_name, fld) in KNOWN_FIXTURE_ISSUES
        score.field_scores.append(
            FieldScore(
                field_name=fld,
                expected=str(exp_val) if exp_val is not None else None,
                extracted=str(ext_val) if ext_val is not None else None,
                match=match,
                notes="known_fixture_issue" if is_known_issue else "",
            )
        )

    # Ruling text similarity — character-level comparison against source
    all_text_parts = [r.get("ruling_text") for r in rulings if r.get("ruling_text")]
    combined_ruling_text = " ".join(all_text_parts) if all_text_parts else ""

    if combined_ruling_text and source_text:
        score.ruling_text_similarity = compute_text_similarity(combined_ruling_text, source_text)
    else:
        score.ruling_text_similarity = 0.0

    # Also check ruling_text_contains as an additional field score
    ruling_text_contains = expected.get("ruling_text_contains", [])
    if ruling_text_contains and combined_ruling_text:
        all_found, missing = check_ruling_text_contains(combined_ruling_text, ruling_text_contains)
        score.field_scores.append(
            FieldScore(
                field_name="ruling_text_contains",
                expected=str(ruling_text_contains),
                extracted="all found" if all_found else f"missing: {missing}",
                match=all_found,
            )
        )

    # Hallucination check
    if source_text and rulings:
        for r in rulings:
            rt = r.get("ruling_text")
            if rt and check_hallucination(rt, source_text):
                score.ruling_text_hallucination = True
                break

    # Party recall — only score if expected party data exists in fixtures
    expected_parties = expected.get("expected_parties", [])
    if expected_parties and rulings:
        score.party_recall_measured = True
        extracted_parties = []
        for r in rulings:
            extracted_parties.extend(r.get("extracted_parties", []))
        if extracted_parties:
            ext_names = {
                p.get("name", "").lower().strip() for p in extracted_parties if p.get("name")
            }
            found = sum(1 for ep in expected_parties if ep.lower().strip() in ext_names)
            score.party_recall = found / len(expected_parties) if expected_parties else 0.0
        else:
            score.party_recall = 0.0
    # If no expected party data, leave party_recall at default 0.0
    # and exclude from aggregation (party_recall_measured=False)

    return score


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate_scores(
    fixture_scores: list[FixtureScore],
    model: str,
) -> EvalSummary:
    """Aggregate fixture scores into county and overall summaries."""
    summary = EvalSummary(
        model=model,
        total_fixtures=len(fixture_scores),
        fixture_scores=fixture_scores,
    )

    # Initialize county scores
    counties: dict[str, CountyScore] = {}
    for fs in fixture_scores:
        if fs.county not in counties:
            counties[fs.county] = CountyScore(county=fs.county)
        cs = counties[fs.county]
        cs.total_fixtures += 1

    # Aggregate field scores
    global_correct: dict[str, int] = {}
    global_total: dict[str, int] = {}
    all_similarities: list[float] = []
    all_party_recalls: list[float] = []
    total_hallucinations = 0
    # Per-county temporary lists for averaging
    county_similarities: dict[str, list[float]] = {}
    county_party_recalls: dict[str, list[float]] = {}

    for fs in fixture_scores:
        cs = counties[fs.county]
        county_similarities.setdefault(fs.county, [])
        county_party_recalls.setdefault(fs.county, [])

        for field_score in fs.field_scores:
            fld = field_score.field_name

            # Skip known fixture issues from accuracy count
            if field_score.notes == "known_fixture_issue":
                continue

            for d in [global_correct, global_total, cs.field_correct, cs.field_total]:
                d.setdefault(fld, 0)

            global_total[fld] += 1
            cs.field_total[fld] += 1

            if field_score.match:
                global_correct[fld] += 1
                cs.field_correct[fld] += 1

        all_similarities.append(fs.ruling_text_similarity)
        county_similarities[fs.county].append(fs.ruling_text_similarity)
        if fs.ruling_text_hallucination:
            total_hallucinations += 1
            cs.hallucination_count += 1
        if fs.party_recall_measured:
            all_party_recalls.append(fs.party_recall)
            county_party_recalls[fs.county].append(fs.party_recall)

    # Compute per-field accuracy
    for fld in set(global_total.keys()):
        total = global_total.get(fld, 0)
        correct = global_correct.get(fld, 0)
        summary.field_accuracy[fld] = correct / total if total > 0 else 0.0

    # Compute per-county averages for similarity and party recall
    for county_name, cs in counties.items():
        sims = county_similarities.get(county_name, [])
        cs.avg_ruling_text_similarity = sum(sims) / len(sims) if sims else 0.0
        recalls = county_party_recalls.get(county_name, [])
        cs.avg_party_recall = sum(recalls) / len(recalls) if recalls else 0.0

    summary.avg_ruling_text_similarity = (
        sum(all_similarities) / len(all_similarities) if all_similarities else 0.0
    )
    summary.total_hallucinations = total_hallucinations
    summary.avg_party_recall = (
        sum(all_party_recalls) / len(all_party_recalls) if all_party_recalls else 0.0
    )
    summary.county_scores = counties

    # Check thresholds
    _check_thresholds(summary)

    return summary


def _check_thresholds(summary: EvalSummary) -> None:
    """Check if quality thresholds are met."""
    failures: list[str] = []

    case_num_acc = summary.field_accuracy.get("case_number", 0.0)
    if case_num_acc < THRESHOLDS["case_number"]:
        failures.append(
            f"case_number accuracy {case_num_acc:.1%} < {THRESHOLDS['case_number']:.0%}"
        )

    case_title_acc = summary.field_accuracy.get("case_title", 0.0)
    if case_title_acc < THRESHOLDS["case_title"]:
        failures.append(
            f"case_title accuracy {case_title_acc:.1%} < {THRESHOLDS['case_title']:.0%}"
        )

    outcome_acc = summary.field_accuracy.get("outcome", 0.0)
    if outcome_acc < THRESHOLDS["outcome"]:
        failures.append(f"outcome accuracy {outcome_acc:.1%} < {THRESHOLDS['outcome']:.0%}")

    if summary.avg_ruling_text_similarity < THRESHOLDS["ruling_text_similarity"]:
        failures.append(
            f"ruling_text_similarity {summary.avg_ruling_text_similarity:.1%} "
            f"< {THRESHOLDS['ruling_text_similarity']:.0%}"
        )

    if summary.total_hallucinations > THRESHOLDS["hallucination_count"]:
        failures.append(f"hallucinations {summary.total_hallucinations} > 0")

    # Only check party recall if we have measurements
    if summary.avg_party_recall > 0 or any(
        fs.party_recall_measured for fs in summary.fixture_scores
    ):
        if summary.avg_party_recall < THRESHOLDS["party_recall"]:
            failures.append(
                f"party_recall {summary.avg_party_recall:.1%} < {THRESHOLDS['party_recall']:.0%}"
            )

    summary.thresholds_passed = len(failures) == 0
    summary.threshold_failures = failures


# ---------------------------------------------------------------------------
# Model comparison
# ---------------------------------------------------------------------------


def compare_models(
    summary_a: EvalSummary,
    summary_b: EvalSummary,
) -> str:
    """Generate a side-by-side comparison report for two models."""
    lines = [
        f"## Model Comparison: {summary_a.model} vs {summary_b.model}",
        "",
        f"{'Metric':<35} {summary_a.model:<15} {summary_b.model:<15}",
        f"{'─' * 35} {'─' * 15} {'─' * 15}",
        f"{'Total fixtures':<35} {summary_a.total_fixtures:<15} {summary_b.total_fixtures:<15}",
    ]

    # Field accuracy comparison
    all_fields = sorted(
        set(list(summary_a.field_accuracy.keys()) + list(summary_b.field_accuracy.keys()))
    )
    for fld in all_fields:
        acc_a = summary_a.field_accuracy.get(fld, 0.0)
        acc_b = summary_b.field_accuracy.get(fld, 0.0)
        lines.append(f"{fld + ' accuracy':<35} {acc_a:<15.1%} {acc_b:<15.1%}")

    # Overall metrics
    lines.append(
        f"{'ruling_text_similarity':<35} "
        f"{summary_a.avg_ruling_text_similarity:<15.1%} "
        f"{summary_b.avg_ruling_text_similarity:<15.1%}"
    )
    lines.append(
        f"{'hallucinations':<35} "
        f"{summary_a.total_hallucinations:<15} "
        f"{summary_b.total_hallucinations:<15}"
    )
    lines.append(
        f"{'party_recall':<35} "
        f"{summary_a.avg_party_recall:<15.1%} "
        f"{summary_b.avg_party_recall:<15.1%}"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_report(summary: EvalSummary) -> str:
    """Format a human-readable evaluation report."""
    lines = [
        f"# Extraction Eval Report: {summary.model}",
        "",
        f"Total fixtures: {summary.total_fixtures}",
        "",
        "## Field Accuracy",
    ]

    for fld, acc in sorted(summary.field_accuracy.items()):
        lines.append(f"  {fld}: {acc:.1%}")

    lines.extend(
        [
            "",
            "## Quality Metrics",
            f"  Ruling text similarity: {summary.avg_ruling_text_similarity:.1%}",
            f"  Hallucinations: {summary.total_hallucinations}",
            f"  Party recall: {summary.avg_party_recall:.1%}",
            "",
            "## Threshold Check",
        ]
    )

    if summary.thresholds_passed:
        lines.append("  All thresholds PASSED")
    else:
        lines.append("  FAILURES:")
        for failure in summary.threshold_failures:
            lines.append(f"    - {failure}")

    lines.extend(["", "## Per-County Breakdown"])
    for county_name, cs in sorted(summary.county_scores.items()):
        lines.append(f"\n  ### {county_name} ({cs.total_fixtures} fixtures)")
        for fld in sorted(set(cs.field_total.keys())):
            total = cs.field_total.get(fld, 0)
            correct = cs.field_correct.get(fld, 0)
            acc = correct / total if total > 0 else 0.0
            lines.append(f"    {fld}: {acc:.1%} ({correct}/{total})")
        lines.append(f"    ruling_text_similarity: {cs.avg_ruling_text_similarity:.1%}")
        lines.append(f"    hallucinations: {cs.hallucination_count}")
        lines.append(f"    party_recall: {cs.avg_party_recall:.1%}")

    return "\n".join(lines)
