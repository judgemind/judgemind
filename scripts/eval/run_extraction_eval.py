#!/usr/bin/env python3
"""Run extraction evaluation against test fixtures.

Two modes:
  --live    Run LlmExtractor against fixtures, save cached results.
  --cached  Load cached results and score (CI-friendly, no API calls).

Additional flags:
  --model MODEL        Model name for live extraction (default: haiku).
  --compare A B        Compare two cached model results side-by-side.
  --check-thresholds   Exit non-zero if quality thresholds are not met.
  --output-format      json or text (default: text).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Resolve paths relative to repo root
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
FIXTURES_DIR = REPO_ROOT / "packages" / "scraper-framework" / "tests" / "fixtures"
EXPECTED_DIR = FIXTURES_DIR / "expected"
RESULTS_DIR = SCRIPT_DIR / "results"

# Add repo to path for imports
sys.path.insert(0, str(REPO_ROOT / "packages" / "scraper-framework" / "src"))
sys.path.insert(0, str(SCRIPT_DIR))

from eval_scorer import (  # noqa: E402
    FixtureScore,
    aggregate_scores,
    compare_models,
    format_report,
    get_county_from_fixture,
    score_fixture,
    should_skip_fixture,
)

# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def load_fixture_text(fixture_path: Path) -> str | None:
    """Load text content from a fixture file (HTML or PDF)."""
    if fixture_path.suffix == ".pdf":
        try:
            import pymupdf

            doc = pymupdf.open(str(fixture_path))
            text_parts = []
            for page in doc:
                text_parts.append(page.get_text())
            doc.close()
            return "\n".join(text_parts)
        except Exception:
            return None
    else:
        # HTML or text file
        try:
            return fixture_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None


def find_fixture_file(fixture_name: str) -> Path | None:
    """Find the actual fixture file for a given expected JSON name."""
    # Strip .json suffix to get base name
    base = fixture_name.rsplit(".json", 1)[0]
    # Try common extensions
    for ext in [".html", ".pdf", ".txt"]:
        candidate = FIXTURES_DIR / (base + ext)
        if candidate.exists():
            return candidate
    # Try in subdirectories
    for subdir in FIXTURES_DIR.iterdir():
        if subdir.is_dir():
            for ext in [".html", ".pdf", ".txt"]:
                candidate = subdir / (base + ext)
                if candidate.exists():
                    return candidate
    return None


def load_expected_fixtures() -> dict[str, dict]:
    """Load all expected JSON fixtures.

    Returns dict mapping fixture_name -> expected_values.
    """
    fixtures = {}
    if not EXPECTED_DIR.exists():
        return fixtures
    for json_file in sorted(EXPECTED_DIR.glob("*.json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            fixtures[json_file.name] = data
        except (json.JSONDecodeError, OSError):
            continue
    return fixtures


# ---------------------------------------------------------------------------
# Live extraction
# ---------------------------------------------------------------------------


def run_live_extraction(
    model_name: str = "haiku",
) -> list[dict]:
    """Run LlmExtractor against all fixtures and return results."""
    from framework.llm_extractor import LlmExtractor

    extractor = LlmExtractor(model=model_name)
    fixtures = load_expected_fixtures()
    results: list[dict] = []

    print(f"\nRunning live extraction with model: {model_name}")
    print(f"Found {len(fixtures)} expected fixtures\n")

    for fname, expected in fixtures.items():
        if should_skip_fixture(fname):
            continue

        print(f"  {fname}... ", end="", flush=True)

        # Find and load fixture file
        fixture_path = find_fixture_file(fname)
        if fixture_path is None:
            print("SKIP (file not found)")
            continue

        text = load_fixture_text(fixture_path)
        if not text:
            print("SKIP (no text)")
            results.append(
                {
                    "fixture_name": fname,
                    "county": get_county_from_fixture(fname),
                    "extraction_result": None,
                    "source_text": None,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "latency_ms": 0,
                    "error": "Could not extract text from fixture",
                }
            )
            continue

        # Build metadata from expected JSON (simulates what scraper provides)
        metadata: dict[str, str] = {}
        if expected.get("judge_name"):
            metadata["judge_name"] = expected["judge_name"]
        if expected.get("department"):
            metadata["department"] = str(expected["department"])

        start = time.monotonic()
        try:
            rulings = extractor.extract(text, metadata=metadata if metadata else None)
            latency = (time.monotonic() - start) * 1000

            # Serialize rulings to dict
            extraction_dict = {
                "extracted_judge_name": (rulings[0].extracted_judge_name if rulings else None),
                "hearing_date": rulings[0].hearing_date if rulings else None,
                "department": rulings[0].department if rulings else None,
                "rulings": [
                    {
                        "extracted_case_number": r.extracted_case_number,
                        "extracted_case_title": r.extracted_case_title,
                        "outcome": r.outcome.value if r.outcome else None,
                        "motion_type": r.motion_type,
                        "ruling_text": r.ruling_text,
                        "hearing_date": r.hearing_date,
                        "extracted_judge_name": r.extracted_judge_name,
                        "department": r.department,
                        "case_type": r.case_type.value if r.case_type else None,
                        "extracted_parties": [
                            {
                                "name": p.name,
                                "role": p.role,
                                "confidence": p.confidence.value,
                            }
                            for p in r.extracted_parties
                        ],
                    }
                    for r in rulings
                ],
            }

            print(f"OK ({len(rulings)} rulings, {latency:.0f}ms)")
            results.append(
                {
                    "fixture_name": fname,
                    "county": get_county_from_fixture(fname),
                    "extraction_result": extraction_dict,
                    "source_text": text,  # Full text for hallucination checks
                    "input_tokens": 0,  # Token tracking happens inside LlmExtractor
                    "output_tokens": 0,
                    "latency_ms": latency,
                    "error": None,
                }
            )
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            print(f"ERROR: {e}")
            results.append(
                {
                    "fixture_name": fname,
                    "county": get_county_from_fixture(fname),
                    "extraction_result": None,
                    "source_text": None,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "latency_ms": latency,
                    "error": str(e),
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


def score_results(
    results: list[dict],
    model_name: str,
) -> list[FixtureScore]:
    """Score extraction results against expected fixtures."""
    fixtures = load_expected_fixtures()
    fixture_scores: list[FixtureScore] = []

    for result in results:
        fname = result["fixture_name"]
        expected = fixtures.get(fname, {})
        extraction = result.get("extraction_result")
        source_text = result.get("source_text")

        if extraction is None:
            fs = FixtureScore(
                fixture_name=fname,
                county=get_county_from_fixture(fname),
                error=result.get("error", "No extraction result"),
            )
        else:
            fs = score_fixture(fname, extraction, expected, source_text)

        fixture_scores.append(fs)

    return fixture_scores


# ---------------------------------------------------------------------------
# Score persistence
# ---------------------------------------------------------------------------


def save_scores(summary: object, model_name: str) -> Path:
    """Save scored results to JSON."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    scores_path = RESULTS_DIR / f"{model_name}_scores.json"

    # Serialize EvalSummary to dict
    data = {
        "model": summary.model,
        "total_fixtures": summary.total_fixtures,
        "field_accuracy": summary.field_accuracy,
        "avg_ruling_text_similarity": summary.avg_ruling_text_similarity,
        "total_hallucinations": summary.total_hallucinations,
        "avg_party_recall": summary.avg_party_recall,
        "thresholds_passed": summary.thresholds_passed,
        "threshold_failures": summary.threshold_failures,
        "county_scores": {
            name: {
                "county": cs.county,
                "total_fixtures": cs.total_fixtures,
                "field_correct": cs.field_correct,
                "field_total": cs.field_total,
                "avg_ruling_text_similarity": cs.avg_ruling_text_similarity,
                "hallucination_count": cs.hallucination_count,
                "avg_party_recall": cs.avg_party_recall,
            }
            for name, cs in summary.county_scores.items()
        },
        "fixture_scores": [
            {
                "fixture_name": fs.fixture_name,
                "county": fs.county,
                "ruling_text_similarity": fs.ruling_text_similarity,
                "ruling_text_hallucination": fs.ruling_text_hallucination,
                "party_recall": fs.party_recall,
                "party_recall_measured": fs.party_recall_measured,
                "error": fs.error,
                "field_scores": [
                    {
                        "field_name": f.field_name,
                        "expected": f.expected,
                        "extracted": f.extracted,
                        "match": f.match,
                        "notes": f.notes,
                    }
                    for f in fs.field_scores
                ],
            }
            for fs in summary.fixture_scores
        ],
    }

    scores_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return scores_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Run extraction evaluation against test fixtures.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run live extraction against fixtures (requires API key).",
    )
    parser.add_argument(
        "--cached",
        action="store_true",
        help="Load cached results and score (CI mode, no API calls).",
    )
    parser.add_argument(
        "--model",
        default="haiku",
        help="Model name for live extraction (default: haiku).",
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("MODEL_A", "MODEL_B"),
        help="Compare two cached model results side-by-side.",
    )
    parser.add_argument(
        "--check-thresholds",
        action="store_true",
        help="Exit non-zero if quality thresholds are not met.",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text).",
    )

    args = parser.parse_args()

    if args.compare:
        model_a, model_b = args.compare
        results_a = load_cached_results(model_a)
        results_b = load_cached_results(model_b)
        if results_a is None:
            print(f"Error: no cached results for {model_a}")
            return 1
        if results_b is None:
            print(f"Error: no cached results for {model_b}")
            return 1

        scores_a = score_results(results_a, model_a)
        scores_b = score_results(results_b, model_b)
        summary_a = aggregate_scores(scores_a, model_a)
        summary_b = aggregate_scores(scores_b, model_b)

        print(compare_models(summary_a, summary_b))
        return 0

    if args.live:
        results = run_live_extraction(args.model)
        cache_path = save_cached_results(results, args.model)
        print(f"\nCached results saved to: {cache_path}")
    elif args.cached:
        results = load_cached_results(args.model)
        if results is None:
            print(f"Error: no cached results for {args.model}")
            print("Run with --live first to generate cached results.")
            return 1
    else:
        parser.print_help()
        return 1

    # Score
    fixture_scores = score_results(results, args.model)
    summary = aggregate_scores(fixture_scores, args.model)

    # Save scores
    scores_path = save_scores(summary, args.model)
    print(f"Scores saved to: {scores_path}")

    # Output
    if args.output_format == "json":
        print(
            json.dumps(
                {
                    "field_accuracy": summary.field_accuracy,
                    "avg_ruling_text_similarity": summary.avg_ruling_text_similarity,
                    "total_hallucinations": summary.total_hallucinations,
                    "avg_party_recall": summary.avg_party_recall,
                    "thresholds_passed": summary.thresholds_passed,
                    "threshold_failures": summary.threshold_failures,
                },
                indent=2,
            )
        )
    else:
        print(format_report(summary))

    # Threshold check
    if args.check_thresholds and not summary.thresholds_passed:
        print("\nThreshold check FAILED:")
        for failure in summary.threshold_failures:
            print(f"  - {failure}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
