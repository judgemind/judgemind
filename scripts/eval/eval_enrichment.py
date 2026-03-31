#!/usr/bin/env python3
# venv: scraper-framework
"""Enrichment eval harness — score LLM enrichment extraction accuracy.

Reads fixture JSON files from ``tests/fixtures/enrichment/<county>/``, runs
each through ``enrich_ruling()``, and reports per-field accuracy for
``motion_type``, ``outcome``, ``case_title``, and ``parties``.

Two modes:
  --live     Run ``enrich_ruling()`` against fixtures, save cached results.
  --cached   Load cached results and score (CI-friendly, no API calls).

See: https://github.com/judgemind/judgemind/issues/2182
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
RESULTS_DIR = SCRIPT_DIR / "results" / "enrichment"
DEFAULT_FIXTURES_DIR = (
    REPO_ROOT / "packages" / "scraper-framework" / "tests" / "fixtures" / "enrichment"
)

# Add repo to path for imports
sys.path.insert(0, str(REPO_ROOT / "packages" / "scraper-framework" / "src"))

# ---------------------------------------------------------------------------
# Quality thresholds
# ---------------------------------------------------------------------------

THRESHOLDS: dict[str, float] = {
    "motion_type": 0.95,
    "outcome": 0.95,
    "case_title": 0.90,
    "parties": 0.85,
}

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

    fixture_path: str
    county: str
    field_scores: list[FieldScore] = field(default_factory=list)
    party_recall: float = 0.0
    error: str | None = None


@dataclass
class CountyScore:
    """Aggregated scores for a county."""

    county: str
    total_fixtures: int = 0
    field_correct: dict[str, int] = field(default_factory=dict)
    field_total: dict[str, int] = field(default_factory=dict)
    avg_party_recall: float = 0.0


@dataclass
class EvalSummary:
    """Overall evaluation summary."""

    model: str
    total_fixtures: int = 0
    fixture_scores: list[FixtureScore] = field(default_factory=list)
    county_scores: dict[str, CountyScore] = field(default_factory=dict)
    field_accuracy: dict[str, float] = field(default_factory=dict)
    avg_party_recall: float = 0.0
    thresholds_passed: bool = True
    threshold_failures: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scoring functions
# ---------------------------------------------------------------------------


def normalize_for_exact_match(val: str | None) -> str | None:
    """Normalize a value for exact comparison: strip, lowercase."""
    if val is None:
        return None
    s = str(val).strip().lower()
    if not s:
        return None
    return s


def score_motion_type(expected: str | None, extracted: str | None) -> bool:
    """Score motion_type: exact match, case-insensitive."""
    e = normalize_for_exact_match(expected)
    x = normalize_for_exact_match(extracted)
    if e is None and x is None:
        return True
    if e is None or x is None:
        return False
    return e == x


def score_outcome(expected: str | None, extracted: str | None) -> bool:
    """Score outcome: exact match, case-insensitive."""
    e = normalize_for_exact_match(expected)
    x = normalize_for_exact_match(extracted)
    if e is None and x is None:
        return True
    if e is None or x is None:
        return False
    return e == x


def normalize_case_title(val: str | None) -> str | None:
    """Normalize case title for fuzzy comparison.

    Strips whitespace, lowercases, normalizes ``vs.``/``vs``/``versus`` to
    ``v.``, and strips trailing ``et al.``.
    """
    import re

    if val is None:
        return None
    s = str(val).strip().lower()
    if not s:
        return None
    # Normalize "vs." / "vs " / "versus" to "v."
    s = re.sub(r"\bvs\.?\s+", "v. ", s)
    s = re.sub(r"\bversus\s+", "v. ", s)
    # Strip trailing ", et al." or " et al."
    s = re.sub(r",?\s*et\s+al\.?\s*$", "", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def score_case_title(expected: str | None, extracted: str | None) -> bool:
    """Score case_title: fuzzy match via Levenshtein ratio >= 0.85 or
    token overlap >= 80%.

    Uses rapidfuzz for Levenshtein distance.
    """
    e = normalize_case_title(expected)
    x = normalize_case_title(extracted)
    if e is None and x is None:
        return True
    if e is None or x is None:
        return False
    if e == x:
        return True

    # Levenshtein ratio
    from rapidfuzz.fuzz import ratio as fuzz_ratio

    if fuzz_ratio(e, x) >= 85.0:
        return True

    # Token overlap
    e_tokens = set(e.split())
    x_tokens = set(x.split())
    if not e_tokens:
        return False
    overlap = len(e_tokens & x_tokens) / len(e_tokens)
    return overlap >= 0.80


def score_parties(
    expected_parties: dict | None,
    extracted_parties: dict | None,
) -> float:
    """Score parties: fuzzy recall of expected parties found in extracted.

    For each expected party name (from both plaintiffs and defendants), check
    if any extracted party name matches with Levenshtein ratio >= 0.85.
    Returns recall as a float in [0.0, 1.0].
    """
    if expected_parties is None:
        return 1.0

    expected_names: list[str] = []
    for role in ("plaintiffs", "defendants"):
        for name in expected_parties.get(role, []):
            if name and name.strip():
                expected_names.append(name.strip().lower())

    if not expected_names:
        # No expected parties — vacuously true
        return 1.0

    extracted_names: list[str] = []
    if extracted_parties is not None:
        for role in ("plaintiffs", "defendants"):
            for name in extracted_parties.get(role, []):
                if name and name.strip():
                    extracted_names.append(name.strip().lower())

    if not extracted_names:
        return 0.0

    from rapidfuzz.fuzz import ratio as fuzz_ratio

    found = 0
    for exp_name in expected_names:
        for ext_name in extracted_names:
            if fuzz_ratio(exp_name, ext_name) >= 85.0:
                found += 1
                break

    return found / len(expected_names)


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def load_fixtures(
    fixtures_dir: Path,
    county_filter: str | None = None,
) -> list[dict]:
    """Load all enrichment fixture JSON files.

    Returns a list of dicts, each with keys:
      - fixture_path: relative path from fixtures_dir
      - county: county name (directory name)
      - ruling_text: the input text
      - expected: the expected output dict
      - source_doc: S3 key
      - notes: any notes
    """
    fixtures: list[dict] = []
    if not fixtures_dir.exists():
        return fixtures

    for county_dir in sorted(fixtures_dir.iterdir()):
        if not county_dir.is_dir():
            continue
        county = county_dir.name
        if county_filter and county.lower() != county_filter.lower():
            continue
        for json_file in sorted(county_dir.glob("*.json")):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                fixtures.append(
                    {
                        "fixture_path": f"{county}/{json_file.name}",
                        "county": county,
                        "ruling_text": data.get("ruling_text", ""),
                        "expected": data.get("expected", {}),
                        "source_doc": data.get("source_doc", ""),
                        "notes": data.get("notes", ""),
                    }
                )
            except (json.JSONDecodeError, OSError) as exc:
                print(f"  WARNING: could not load {json_file}: {exc}", file=sys.stderr)
    return fixtures


# ---------------------------------------------------------------------------
# Live extraction
# ---------------------------------------------------------------------------


def run_live_extraction(
    fixtures: list[dict],
    model_name: str = "gemini-2.5-flash-lite",
) -> list[dict]:
    """Run enrich_ruling() against all fixtures and return results."""
    from framework.llm_enrichment import enrich_ruling

    results: list[dict] = []

    print(f"\nRunning live enrichment extraction with model: {model_name}")
    print(f"Fixtures: {len(fixtures)}\n")

    for fixture in fixtures:
        path = fixture["fixture_path"]
        ruling_text = fixture["ruling_text"]

        print(f"  {path}... ", end="", flush=True)

        if not ruling_text or not ruling_text.strip():
            print("SKIP (empty ruling_text)")
            results.append(
                {
                    "fixture_path": path,
                    "county": fixture["county"],
                    "extraction_result": None,
                    "latency_ms": 0,
                    "error": "Empty ruling_text",
                }
            )
            continue

        start = time.monotonic()
        try:
            result = enrich_ruling(ruling_text, model=model_name)
            latency = (time.monotonic() - start) * 1000

            extraction_dict = result.model_dump()
            print(f"OK ({latency:.0f}ms)")
            results.append(
                {
                    "fixture_path": path,
                    "county": fixture["county"],
                    "extraction_result": extraction_dict,
                    "latency_ms": latency,
                    "error": None,
                }
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000
            print(f"ERROR: {exc}")
            results.append(
                {
                    "fixture_path": path,
                    "county": fixture["county"],
                    "extraction_result": None,
                    "latency_ms": latency,
                    "error": str(exc),
                }
            )

    return results


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


def save_cached_results(results: list[dict], model_name: str) -> Path:
    """Save extraction results to a JSON cache file."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = RESULTS_DIR / f"{model_name}_cached.json"
    cache_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    return cache_path


def load_cached_results(model_name: str) -> list[dict] | None:
    """Load cached extraction results for a model."""
    cache_path = RESULTS_DIR / f"{model_name}_cached.json"
    if not cache_path.exists():
        return None
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Scoring pipeline
# ---------------------------------------------------------------------------


def score_fixture(
    fixture: dict,
    extraction_result: dict | None,
) -> FixtureScore:
    """Score a single fixture's extraction results against expected values."""
    county = fixture["county"]
    path = fixture["fixture_path"]
    expected = fixture["expected"]
    score = FixtureScore(fixture_path=path, county=county)

    if extraction_result is None:
        score.error = "No extraction result"
        return score

    # motion_type
    exp_mt = expected.get("motion_type")
    ext_mt = extraction_result.get("motion_type")
    mt_match = score_motion_type(exp_mt, ext_mt)
    score.field_scores.append(
        FieldScore(
            field_name="motion_type",
            expected=exp_mt,
            extracted=ext_mt,
            match=mt_match,
        )
    )

    # outcome
    exp_oc = expected.get("outcome")
    ext_oc = extraction_result.get("outcome")
    oc_match = score_outcome(exp_oc, ext_oc)
    score.field_scores.append(
        FieldScore(
            field_name="outcome",
            expected=exp_oc,
            extracted=ext_oc,
            match=oc_match,
        )
    )

    # case_title
    exp_ct = expected.get("case_title")
    ext_ct = extraction_result.get("case_title")
    ct_match = score_case_title(exp_ct, ext_ct)
    score.field_scores.append(
        FieldScore(
            field_name="case_title",
            expected=exp_ct,
            extracted=ext_ct,
            match=ct_match,
        )
    )

    # parties (recall)
    exp_parties = expected.get("parties")
    ext_parties = extraction_result.get("parties")
    # Convert EnrichmentParties-style dict if needed
    if ext_parties is not None and not isinstance(ext_parties, dict):
        ext_parties = None
    score.party_recall = score_parties(exp_parties, ext_parties)

    return score


def score_results(
    fixtures: list[dict],
    results: list[dict],
    model_name: str,
) -> EvalSummary:
    """Score all extraction results against expected fixtures."""
    # Build lookup by fixture_path
    result_by_path: dict[str, dict] = {}
    for r in results:
        result_by_path[r["fixture_path"]] = r

    fixture_scores: list[FixtureScore] = []
    for fixture in fixtures:
        path = fixture["fixture_path"]
        result = result_by_path.get(path)
        extraction = result.get("extraction_result") if result else None

        fs = score_fixture(fixture, extraction)
        if result and result.get("error") and extraction is None:
            fs.error = result["error"]
        fixture_scores.append(fs)

    return aggregate_scores(fixture_scores, model_name)


def aggregate_scores(
    fixture_scores: list[FixtureScore],
    model_name: str,
) -> EvalSummary:
    """Aggregate fixture scores into county and overall summaries."""
    summary = EvalSummary(
        model=model_name,
        total_fixtures=len(fixture_scores),
        fixture_scores=fixture_scores,
    )

    # Initialize county scores
    counties: dict[str, CountyScore] = {}
    for fs in fixture_scores:
        if fs.county not in counties:
            counties[fs.county] = CountyScore(county=fs.county)
        counties[fs.county].total_fixtures += 1

    # Aggregate field scores
    global_correct: dict[str, int] = {}
    global_total: dict[str, int] = {}
    all_party_recalls: list[float] = []
    county_party_recalls: dict[str, list[float]] = {}

    for fs in fixture_scores:
        cs = counties[fs.county]
        county_party_recalls.setdefault(fs.county, [])

        for field_score in fs.field_scores:
            fld = field_score.field_name
            for d in [global_correct, global_total, cs.field_correct, cs.field_total]:
                d.setdefault(fld, 0)

            global_total[fld] += 1
            cs.field_total[fld] += 1

            if field_score.match:
                global_correct[fld] += 1
                cs.field_correct[fld] += 1

        all_party_recalls.append(fs.party_recall)
        county_party_recalls[fs.county].append(fs.party_recall)

    # Compute per-field accuracy
    for fld in sorted(global_total.keys()):
        total = global_total.get(fld, 0)
        correct = global_correct.get(fld, 0)
        summary.field_accuracy[fld] = correct / total if total > 0 else 0.0

    # Compute per-county averages for party recall
    for county_name, cs in counties.items():
        recalls = county_party_recalls.get(county_name, [])
        cs.avg_party_recall = sum(recalls) / len(recalls) if recalls else 0.0

    summary.avg_party_recall = (
        sum(all_party_recalls) / len(all_party_recalls) if all_party_recalls else 0.0
    )
    summary.county_scores = counties

    _check_thresholds(summary)

    return summary


def _check_thresholds(summary: EvalSummary) -> None:
    """Check if quality thresholds are met."""
    failures: list[str] = []

    for field_name, threshold in THRESHOLDS.items():
        if field_name == "parties":
            actual = summary.avg_party_recall
        else:
            actual = summary.field_accuracy.get(field_name, 0.0)

        if actual < threshold:
            failures.append(f"{field_name} accuracy {actual:.1%} < {threshold:.0%}")

    summary.thresholds_passed = len(failures) == 0
    summary.threshold_failures = failures


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_text_report(summary: EvalSummary) -> str:
    """Format a human-readable evaluation report."""
    lines = [
        "=== Enrichment Eval Results ===",
        f"Model: {summary.model}",
        f"Fixtures: {summary.total_fixtures}",
        "",
        "Per-field accuracy:",
    ]

    for fld in ("motion_type", "outcome", "case_title"):
        acc = summary.field_accuracy.get(fld, 0.0)
        threshold = THRESHOLDS.get(fld, 0.0)
        total = sum(
            1
            for fs in summary.fixture_scores
            for f in fs.field_scores
            if f.field_name == fld
        )
        correct = sum(
            1
            for fs in summary.fixture_scores
            for f in fs.field_scores
            if f.field_name == fld and f.match
        )
        status = "PASS" if acc >= threshold else "FAIL"
        lines.append(
            f"  {fld:>15}: {correct}/{total} ({acc:.1%})  "
            f"[{status}: target {threshold:.0%}]"
        )

    # Parties (recall-based)
    parties_threshold = THRESHOLDS["parties"]
    parties_status = "PASS" if summary.avg_party_recall >= parties_threshold else "FAIL"
    lines.append(
        f"  {'parties':>15}: avg recall {summary.avg_party_recall:.1%}  "
        f"[{parties_status}: target {parties_threshold:.0%}]"
    )

    # Per-county breakdown
    lines.extend(["", "Per-county breakdown:"])
    for county_name, cs in sorted(summary.county_scores.items()):
        parts = [f"  {county_name}: {cs.total_fixtures} fixtures"]
        for fld in ("motion_type", "outcome", "case_title"):
            total = cs.field_total.get(fld, 0)
            correct = cs.field_correct.get(fld, 0)
            acc = correct / total if total > 0 else 0.0
            parts.append(f"{fld} {acc:.0%}")
        parts.append(f"parties {cs.avg_party_recall:.0%}")
        lines.append(", ".join(parts))

    # Failures (mismatches)
    mismatches: list[str] = []
    for fs in summary.fixture_scores:
        for field_score in fs.field_scores:
            if not field_score.match:
                mismatches.append(
                    f"  {fs.fixture_path}: {field_score.field_name} "
                    f'expected="{field_score.expected}" got="{field_score.extracted}"'
                )
        if fs.party_recall < 1.0 and fs.party_recall > 0.0:
            mismatches.append(
                f"  {fs.fixture_path}: parties recall={fs.party_recall:.0%}"
            )
        elif fs.party_recall == 0.0:
            # Check if there were expected parties
            pass  # Only flag if there were expected parties — handled by score
        if fs.error:
            mismatches.append(f"  {fs.fixture_path}: ERROR: {fs.error}")

    if mismatches:
        lines.extend(["", "Failures:"])
        lines.extend(mismatches)

    # Threshold check
    if not summary.thresholds_passed:
        lines.extend(["", "Threshold check FAILED:"])
        for failure in summary.threshold_failures:
            lines.append(f"  - {failure}")
    else:
        lines.extend(["", "Threshold check PASSED"])

    return "\n".join(lines)


def format_json_report(summary: EvalSummary) -> str:
    """Format a JSON evaluation report."""
    data = {
        "model": summary.model,
        "total_fixtures": summary.total_fixtures,
        "field_accuracy": summary.field_accuracy,
        "avg_party_recall": summary.avg_party_recall,
        "thresholds_passed": summary.thresholds_passed,
        "threshold_failures": summary.threshold_failures,
        "county_scores": {
            name: {
                "county": cs.county,
                "total_fixtures": cs.total_fixtures,
                "field_correct": cs.field_correct,
                "field_total": cs.field_total,
                "avg_party_recall": cs.avg_party_recall,
            }
            for name, cs in sorted(summary.county_scores.items())
        },
        "fixture_scores": [
            {
                "fixture_path": fs.fixture_path,
                "county": fs.county,
                "party_recall": fs.party_recall,
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
            }
            for fs in summary.fixture_scores
        ],
    }
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Evaluate LLM enrichment extraction accuracy against hand-verified fixtures.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run enrich_ruling() against fixtures (requires API key).",
    )
    parser.add_argument(
        "--cached",
        action="store_true",
        help="Load cached results and score (CI mode, no API calls).",
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=DEFAULT_FIXTURES_DIR,
        help="Path to enrichment fixtures directory.",
    )
    parser.add_argument(
        "--model",
        default="gemini-2.5-flash-lite",
        help="Model name for live extraction (default: gemini-2.5-flash-lite).",
    )
    parser.add_argument(
        "--county",
        default=None,
        help="Run only fixtures for one county.",
    )
    parser.add_argument(
        "--check-thresholds",
        action="store_true",
        help="Exit non-zero if accuracy targets are not met.",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text).",
    )

    args = parser.parse_args()

    if not args.live and not args.cached:
        parser.print_help()
        return 1

    # Load fixtures (needed for both modes — scoring uses expected values)
    fixtures = load_fixtures(args.fixtures, county_filter=args.county)
    if not fixtures:
        print(f"Error: no fixtures found in {args.fixtures}", file=sys.stderr)
        return 1

    if args.live:
        results = run_live_extraction(fixtures, args.model)
        cache_path = save_cached_results(results, args.model)
        print(f"\nCached results saved to: {cache_path}")
    else:
        results = load_cached_results(args.model)
        if results is None:
            print(f"Error: no cached results for model '{args.model}'")
            print("Run with --live first to generate cached results.")
            return 1

    # Score
    summary = score_results(fixtures, results, args.model)

    # Output
    if args.output_format == "json":
        print(format_json_report(summary))
    else:
        print(format_text_report(summary))

    # Threshold check
    if args.check_thresholds and not summary.thresholds_passed:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
