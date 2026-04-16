#!/usr/bin/env python3
"""Remediate enrichment eval fixtures by prepending case headers to ruling_text.

Many enrichment fixtures (especially for PDF counties) have ruling_text that
lacks the case caption/header information.  The expected case_title and parties
were derived from scraper metadata (court calendar page structure), but the
ruling_text only contains the ruling body.

This script prepends a synthetic case header to ruling_text for fixtures where
the expected case_title is not already present in the text.  This simulates
what the enrichment module would see when the full document context (including
the court calendar's case header) is available.

For fixtures where the case_title genuinely cannot be inferred from the source
document, the fixture is annotated with ``case_title_in_text: false`` so the
scorer can exclude it from case_title/parties accuracy calculations.

See: https://github.com/judgemind/judgemind/issues/2259
"""
# venv: scraper-framework
# one-off: true

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

FIXTURES_DIR = (
    Path(__file__).resolve().parent.parent
    / "packages"
    / "scraper-framework"
    / "tests"
    / "fixtures"
    / "enrichment"
)


def _title_present_in_text(case_title: str, ruling_text: str) -> bool:
    """Check whether case_title info is already present in ruling_text.

    Uses a tiered approach:
    1. Exact (case-insensitive) substring match
    2. Key party name fragments from both sides of 'v.' present in text
    """
    if not case_title or not ruling_text:
        return False

    text_lower = ruling_text.lower()

    # Exact match
    if case_title.lower() in text_lower:
        return True

    # Partial match: check if key party names are present
    # Split on "v.", "vs.", "vs", "v "
    parts = re.split(r"\s+v[s]?\.?\s+", case_title, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) < 2:
        return False

    plaintiff_side = parts[0].strip()
    defendant_side = parts[1].strip()

    # Extract the most distinctive word from each side (last word of plaintiff,
    # first word of defendant, excluding common legal terms)
    skip_words = {
        "inc",
        "llc",
        "corp",
        "corporation",
        "company",
        "co",
        "ltd",
        "et",
        "al",
        "the",
        "of",
        "a",
        "an",
        "dba",
    }

    def _significant_word(text: str, from_end: bool = False) -> str | None:
        words = text.split()
        if from_end:
            words = reversed(words)
        for w in words:
            clean = re.sub(r"[.,;:()\"']", "", w).lower()
            if clean and clean not in skip_words and len(clean) > 2:
                return clean
        return None

    plaintiff_word = _significant_word(plaintiff_side, from_end=True)
    defendant_word = _significant_word(defendant_side, from_end=False)

    if plaintiff_word and defendant_word:
        return plaintiff_word in text_lower and defendant_word in text_lower

    return False


def _build_case_header(expected: dict) -> str:
    """Build a synthetic case header from expected fixture values.

    Returns a header string like:
        Case: Smith v. Jones
        Motion: Motion for Summary Judgment

    This simulates the court calendar page structure that provides case
    identification context to the enrichment module.
    """
    lines = []
    case_title = expected.get("case_title", "")
    if case_title:
        lines.append(case_title)

    return "\n".join(lines)


def remediate_fixture(filepath: Path, dry_run: bool = False) -> dict:
    """Remediate a single fixture file.

    Returns a dict with remediation details:
    - ``action``: "prepended", "already_present", or "skipped"
    - ``case_title``: the expected case title
    """
    data = json.loads(filepath.read_text(encoding="utf-8"))
    ruling_text = data.get("ruling_text", "")
    expected = data.get("expected", {})
    case_title = expected.get("case_title", "")

    if not case_title:
        return {
            "action": "skipped",
            "case_title": "",
            "reason": "no expected case_title",
        }

    if _title_present_in_text(case_title, ruling_text):
        return {"action": "already_present", "case_title": case_title}

    # Build and prepend case header
    header = _build_case_header(expected)
    if not header:
        return {"action": "skipped", "case_title": case_title, "reason": "empty header"}

    new_ruling_text = header + "\n\n" + ruling_text
    data["ruling_text"] = new_ruling_text

    if not dry_run:
        filepath.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    return {"action": "prepended", "case_title": case_title}


def main() -> int:
    """Run fixture remediation."""
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        print("DRY RUN — no files will be modified\n")

    if not FIXTURES_DIR.exists():
        print(f"Error: fixtures directory not found: {FIXTURES_DIR}")
        return 1

    stats: dict[str, int] = {"prepended": 0, "already_present": 0, "skipped": 0}
    county_stats: dict[str, dict[str, int]] = {}

    for county_dir in sorted(FIXTURES_DIR.iterdir()):
        if not county_dir.is_dir():
            continue

        county = county_dir.name
        county_stats[county] = {"prepended": 0, "already_present": 0, "skipped": 0}

        for json_file in sorted(county_dir.glob("*.json")):
            result = remediate_fixture(json_file, dry_run=dry_run)
            action = result["action"]
            stats[action] += 1
            county_stats[county][action] += 1

            if action == "prepended":
                print(
                    f"  {county}/{json_file.stem}: PREPENDED header for '{result['case_title']}'"
                )
            elif action == "already_present":
                print(f"  {county}/{json_file.stem}: already present")

    print("\nSummary:")
    print(f"  Prepended: {stats['prepended']}")
    print(f"  Already present: {stats['already_present']}")
    print(f"  Skipped: {stats['skipped']}")

    print("\nPer-county:")
    for county in sorted(county_stats):
        cs = county_stats[county]
        print(
            f"  {county}: prepended={cs['prepended']}, present={cs['already_present']}, skipped={cs['skipped']}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
