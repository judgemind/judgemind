#!/usr/bin/env python3
"""Update coverage-baselines.json with current coverage from CI artifacts.

Reads coverage reports (XML for Python, lcov for TypeScript) and updates the
baselines file if coverage increased. This script is called by CI after tests
pass — it commits the updated baselines so the ratchet only goes up.

Usage:
    scripts/update-coverage-baselines.py --package <pkg-path> --coverage-file <path>

Example:
    scripts/update-coverage-baselines.py \
        --package packages/scraper-framework \
        --coverage-file packages/scraper-framework/coverage.xml
"""

import argparse
import json
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def parse_cobertura_xml(path: str) -> float:
    """Extract overall line coverage percentage from Cobertura XML."""
    tree = ET.parse(path)  # noqa: S314
    root = tree.getroot()
    line_rate = root.get("line-rate")
    if line_rate is None:
        print(f"ERROR: No line-rate attribute in {path}", file=sys.stderr)
        sys.exit(1)
    return float(line_rate) * 100


def parse_lcov(path: str) -> float:
    """Extract overall line coverage percentage from lcov.info."""
    lines_found = 0
    lines_hit = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("LF:"):
                lines_found += int(line[3:])
            elif line.startswith("LH:"):
                lines_hit += int(line[3:])
    if lines_found == 0:
        return 0.0
    return (lines_hit / lines_found) * 100


def main() -> None:
    parser = argparse.ArgumentParser(description="Update coverage baselines")
    parser.add_argument(
        "--package",
        required=True,
        help="Package path relative to repo root (e.g. packages/scraper-framework)",
    )
    parser.add_argument(
        "--coverage-file",
        required=True,
        help="Path to coverage report (coverage.xml or lcov.info)",
    )
    parser.add_argument(
        "--baselines-file",
        default="coverage-baselines.json",
        help="Path to baselines JSON file (default: coverage-baselines.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing",
    )
    args = parser.parse_args()

    coverage_path = Path(args.coverage_file)
    if not coverage_path.exists():
        print(f"WARNING: Coverage file not found: {coverage_path}", file=sys.stderr)
        sys.exit(0)

    # Parse coverage based on file type
    if coverage_path.suffix == ".xml":
        current = parse_cobertura_xml(str(coverage_path))
    elif coverage_path.name == "lcov.info" or coverage_path.suffix == ".info":
        current = parse_lcov(str(coverage_path))
    else:
        print(f"ERROR: Unknown coverage format: {coverage_path}", file=sys.stderr)
        sys.exit(1)

    # Truncate to 1 decimal place (floor, not round — avoids false ratchet bumps)
    current = math.floor(current * 10) / 10

    # Load baselines
    baselines_path = Path(args.baselines_file)
    if not baselines_path.exists():
        print(f"ERROR: Baselines file not found: {baselines_path}", file=sys.stderr)
        sys.exit(1)

    with open(baselines_path) as f:
        baselines = json.load(f)

    pkg = args.package
    previous = baselines.get(pkg, 0)

    if current > previous:
        print(f"{pkg}: coverage increased {previous}% -> {current}%")
        if not args.dry_run:
            baselines[pkg] = current
            with open(baselines_path, "w") as f:
                json.dump(baselines, f, indent=2)
                f.write("\n")
            print(f"Updated {baselines_path}")
        else:
            print("(dry run — no changes written)")
    elif current < previous:
        print(
            f"FAIL: {pkg}: coverage dropped {previous}% -> {current}% "
            f"(floor violation)",
            file=sys.stderr,
        )
        sys.exit(1)
    else:
        print(f"{pkg}: coverage unchanged at {current}%")


if __name__ == "__main__":
    main()
