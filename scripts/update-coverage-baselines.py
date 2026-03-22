#!/usr/bin/env python3
"""Update coverage-baselines.json with current coverage from CI artifacts.

Reads coverage reports (XML for Python, lcov for TypeScript) and updates the
baselines file if coverage increased. This script is called by CI after tests
pass — it commits the updated baselines so the ratchet only goes up.

Supports merging multiple coverage files for the same package (e.g. when tests
are split across CI jobs). Use multiple --coverage-file arguments to merge.

Usage:
    scripts/update-coverage-baselines.py --package <pkg-path> --coverage-file <path>

    # Merge multiple coverage files for the same package:
    scripts/update-coverage-baselines.py --package <pkg-path> \
        --coverage-file <path1> --coverage-file <path2> --coverage-file <path3>

Example:
    scripts/update-coverage-baselines.py \
        --package packages/scraper-framework \
        --coverage-file packages/scraper-framework/coverage.xml

    # Merge split scraper test coverage:
    scripts/update-coverage-baselines.py \
        --package packages/scraper-framework \
        --coverage-file coverage-artifacts/coverage-scraper-framework/coverage.xml \
        --coverage-file coverage-artifacts/coverage-scraper-courts/coverage.xml \
        --coverage-file coverage-artifacts/coverage-scraper-ingestion/coverage.xml
"""

import argparse
import json
import math
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
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


def parse_cobertura_xml_lines(path: str) -> dict[str, dict[int, int]]:
    """Extract per-file, per-line hit counts from Cobertura XML.

    Returns a dict mapping filename -> {line_number: hits}.
    """
    tree = ET.parse(path)  # noqa: S314
    root = tree.getroot()

    file_lines: dict[str, dict[int, int]] = {}
    for package in root.iter("package"):
        for cls in package.iter("class"):
            filename = cls.get("filename", "")
            if not filename:
                continue
            lines_elem = cls.find("lines")
            if lines_elem is None:
                continue
            if filename not in file_lines:
                file_lines[filename] = {}
            for line in lines_elem.iter("line"):
                num = int(line.get("number", 0))
                hits = int(line.get("hits", 0))
                file_lines[filename][num] = max(file_lines[filename].get(num, 0), hits)
    return file_lines


def merge_cobertura_coverage(paths: list[str]) -> float:
    """Merge multiple Cobertura XML files and compute combined line coverage.

    For each source file+line combination, takes the max hit count across all
    reports. This correctly handles split test jobs where different jobs test
    different subsets of the source code.
    """
    merged: dict[str, dict[int, int]] = defaultdict(dict)

    for path in paths:
        file_lines = parse_cobertura_xml_lines(path)
        for filename, lines in file_lines.items():
            for line_num, hits in lines.items():
                merged[filename][line_num] = max(
                    merged[filename].get(line_num, 0), hits
                )

    total_lines = 0
    covered_lines = 0
    for lines in merged.values():
        for hits in lines.values():
            total_lines += 1
            if hits > 0:
                covered_lines += 1

    if total_lines == 0:
        return 0.0
    return (covered_lines / total_lines) * 100


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
        action="append",
        help="Path to coverage report (coverage.xml or lcov.info). "
        "Can be specified multiple times to merge coverage from split test jobs.",
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

    # Filter to only coverage files that actually exist
    existing_files = []
    for cf in args.coverage_file:
        p = Path(cf)
        if p.exists():
            existing_files.append(str(p))
        else:
            print(f"WARNING: Coverage file not found, skipping: {p}", file=sys.stderr)

    if not existing_files:
        print("WARNING: No coverage files found", file=sys.stderr)
        sys.exit(0)

    # Determine file type from first existing file
    first_path = Path(existing_files[0])

    if first_path.suffix == ".xml":
        if len(existing_files) > 1:
            print(
                f"Merging {len(existing_files)} Cobertura XML coverage files",
                file=sys.stderr,
            )
            current = merge_cobertura_coverage(existing_files)
        else:
            current = parse_cobertura_xml(existing_files[0])
    elif first_path.name == "lcov.info" or first_path.suffix == ".info":
        if len(existing_files) > 1:
            print(
                "WARNING: lcov merging not supported; using first file only",
                file=sys.stderr,
            )
        current = parse_lcov(existing_files[0])
    else:
        print(f"ERROR: Unknown coverage format: {first_path}", file=sys.stderr)
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
