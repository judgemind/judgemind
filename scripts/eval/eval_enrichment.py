#!/usr/bin/env python3
"""Run enrichment evaluation against test fixtures.

Evaluates the LLM enrichment extractor (enrich_ruling) by running it
against ground-truth fixture files and scoring per-field accuracy.

Two modes:
  --live    Run enrich_ruling() against fixtures, save cached results.
  --cached  Load cached results and score (CI-friendly, no API calls).

Additional flags:
  --model MODEL        Model name for live extraction (default: gemini-2.5-flash-lite).
  --check-thresholds   Exit non-zero if quality thresholds are not met.
  --county COUNTY      Run only fixtures for one county.
  --output-format      json or text (default: text).
  --fixtures PATH      Path to enrichment fixtures directory.
"""
# venv: scraper-framework

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Resolve paths relative to repo root
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_FIXTURES_DIR = (
    REPO_ROOT / "packages" / "scraper-framework" / "tests" / "fixtures" / "enrichment"
)
RESULTS_DIR = SCRIPT_DIR / "results" / "enrichment"

# Add repo to path for imports
sys.path.insert(0, str(REPO_ROOT / "packages" / "scraper-framework" / "src"))
sys.path.insert(0, str(SCRIPT_DIR))

from eval_enrichment_scorer import (  # noqa: E402
    FixtureScore,
    aggregate_scores,
    format_json_report,
    format_text_report,
    score_fixture,
)

# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def load_fixtures(
    fixtures_dir: Path,
    county_filter: str | None = None,
) -> list[dict]:
    """Load enrichment fixture JSON files.

    Each fixture is at ``<fixtures_dir>/<county>/<ruling-id>.json``.

    Returns a list of dicts with keys:
    - ``fixture_id``: ``"<county>/<filename_stem>"``
    - ``county``: county directory name
    - ``ruling_text``: the ruling text to send to enrich_ruling
    - ``expected``: the expected extraction result
    - ``source_doc``: optional S3 URI for provenance
    - ``notes``: optional notes
    - ``acceptable_alternatives``: optional dict of field -> list[str]
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
                        "fixture_id": f"{county}/{json_file.stem}",
                        "county": county,
                        "ruling_text": data.get("ruling_text", ""),
                        "expected": data.get("expected", {}),
                        "source_doc": data.get("source_doc", ""),
                        "notes": data.get("notes", ""),
                        "acceptable_alternatives": data.get("acceptable_alternatives"),
                    }
                )
            except (json.JSONDecodeError, OSError) as e:
                print(f"Warning: could not load {json_file}: {e}", file=sys.stderr)
                continue

    return fixtures


# ---------------------------------------------------------------------------
# Live extraction
# ---------------------------------------------------------------------------


def run_live_extraction(
    fixtures: list[dict],
    model_name: str,
) -> list[dict]:
    """Run enrich_ruling() against all fixtures and return results."""
    from framework.llm_enrichment import enrich_ruling

    results: list[dict] = []
    total = len(fixtures)

    print(f"\nRunning live enrichment extraction with model: {model_name}")
    print(f"Found {total} fixtures\n")

    for i, fixture in enumerate(fixtures, 1):
        fixture_id = fixture["fixture_id"]
        ruling_text = fixture["ruling_text"]

        print(f"  [{i}/{total}] {fixture_id}... ", end="", flush=True)

        if not ruling_text.strip():
            print("SKIP (empty ruling text)")
            results.append(
                {
                    "fixture_id": fixture_id,
                    "county": fixture["county"],
                    "extraction_result": None,
                    "latency_ms": 0,
                    "error": "Empty ruling text",
                }
            )
            continue

        start = time.monotonic()
        try:
            result = enrich_ruling(ruling_text, model=model_name)
            latency = (time.monotonic() - start) * 1000

            extraction_dict = {
                "case_title": result.case_title,
                "motion_type": result.motion_type,
                "outcome": result.outcome,
                "parties": {
                    "plaintiffs": result.parties.plaintiffs,
                    "defendants": result.parties.defendants,
                },
            }

            print(f"OK ({latency:.0f}ms)")
            results.append(
                {
                    "fixture_id": fixture_id,
                    "county": fixture["county"],
                    "extraction_result": extraction_dict,
                    "latency_ms": latency,
                    "error": None,
                }
            )
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            print(f"ERROR: {e}")
            results.append(
                {
                    "fixture_id": fixture_id,
                    "county": fixture["county"],
                    "extraction_result": None,
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
    fixtures: list[dict],
) -> list[FixtureScore]:
    """Score extraction results against expected fixtures."""
    # Build lookups by fixture_id
    expected_by_id: dict[str, dict] = {}
    alternatives_by_id: dict[str, dict | None] = {}
    for fixture in fixtures:
        expected_by_id[fixture["fixture_id"]] = fixture["expected"]
        alternatives_by_id[fixture["fixture_id"]] = fixture.get(
            "acceptable_alternatives"
        )

    fixture_scores: list[FixtureScore] = []

    for result in results:
        fixture_id = result["fixture_id"]
        county = result["county"]
        expected = expected_by_id.get(fixture_id, {})
        alternatives = alternatives_by_id.get(fixture_id)
        extraction = result.get("extraction_result")

        if extraction is None:
            fs = FixtureScore(
                fixture_id=fixture_id,
                county=county,
                error=result.get("error", "No extraction result"),
            )
        else:
            fs = score_fixture(
                fixture_id,
                county,
                expected,
                extraction,
                acceptable_alternatives=alternatives,
            )

        fixture_scores.append(fs)

    return fixture_scores


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Run enrichment evaluation against test fixtures."
    )
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
        default="gemini-2.5-flash-lite",
        help="Model name for live extraction (default: gemini-2.5-flash-lite).",
    )
    parser.add_argument(
        "--check-thresholds",
        action="store_true",
        help="Exit non-zero if quality thresholds are not met.",
    )
    parser.add_argument(
        "--county",
        default=None,
        help="Run only fixtures for one county.",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument(
        "--fixtures",
        default=str(DEFAULT_FIXTURES_DIR),
        help="Path to enrichment fixtures directory.",
    )

    args = parser.parse_args()
    fixtures_dir = Path(args.fixtures)

    # Load fixtures
    fixtures = load_fixtures(fixtures_dir, county_filter=args.county)

    if args.live:
        results = run_live_extraction(fixtures, args.model)
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
        return 0

    # Score
    fixture_scores = score_results(results, fixtures)
    summary = aggregate_scores(fixture_scores, args.model)

    # Output
    if args.output_format == "json":
        print(json.dumps(format_json_report(summary), indent=2))
    else:
        print(format_text_report(summary))

    # Threshold check
    if args.check_thresholds and not summary.thresholds_passed:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
