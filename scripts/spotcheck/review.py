#!/usr/bin/env python3
"""Spotcheck reviewer — run deterministic checks on fetched artifacts.

Runs automated checks that do not require an LLM, flagging items that
deserve closer attention during manual or LLM-assisted review.

Usage:
    scripts/run-py.sh scripts/spotcheck/review.py \\
        tmp/spotcheck/LA-rulings-20260328/

Checks by entity type:

  rulings:
    - Null field detection (required fields that are empty)
    - Boilerplate detection (ruling text that is generic/placeholder)
    - Case-number-in-title detection (case_title contains case_number)

  originals:
    - Derived ruling count (multi-case docs should produce multiple rulings)
    - Missing derived rulings (doc with zero rulings)

Outputs findings.json in the spotcheck directory.
"""

# venv: scraper-framework
from __future__ import annotations

import abc
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Check base class and registry
# ---------------------------------------------------------------------------


class Check(abc.ABC):
    """Base class for deterministic spotcheck checks."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Short identifier for this check."""

    @property
    @abc.abstractmethod
    def description(self) -> str:
        """Human-readable description of what this check looks for."""

    @abc.abstractmethod
    def run(self, item_dir: Path, item_data: dict[str, Any]) -> list[dict[str, Any]]:
        """Run the check on a single item.

        Args:
            item_dir: Path to the item's directory.
            item_data: Parsed JSON data relevant to the item.

        Returns:
            List of finding dicts, each with keys: check, severity, message, detail.
        """


# Registry mapping entity types to their check classes.
CHECKS: dict[str, list[type[Check]]] = {
    "rulings": [],
    "originals": [],
}


def register_check(entity_type: str) -> Any:
    """Decorator to register a check for an entity type."""

    def decorator(cls: type[Check]) -> type[Check]:
        if entity_type not in CHECKS:
            CHECKS[entity_type] = []
        CHECKS[entity_type].append(cls)
        return cls

    return decorator


# ---------------------------------------------------------------------------
# Rulings checks
# ---------------------------------------------------------------------------

REQUIRED_RULING_FIELDS = [
    "outcome",
    "motion_type",
    "ruling_text",
    "hearing_date",
    "department",
    "case_number",
    "case_title",
]

BOILERPLATE_PATTERNS = [
    re.compile(r"^(no\s+)?tentative\s+ruling\.?$", re.IGNORECASE),
    re.compile(r"^see\s+attached\.?$", re.IGNORECASE),
    re.compile(r"^ruling\s+on\s+submitted\s+matter\.?$", re.IGNORECASE),
    re.compile(r"^the\s+court\s+will\s+hear\s+argument\.?$", re.IGNORECASE),
    re.compile(r"^parties\s+to\s+give\s+notice\.?$", re.IGNORECASE),
]


@register_check("rulings")
class NullFieldCheck(Check):
    """Detect required fields that are null or empty."""

    @property
    def name(self) -> str:
        return "null_fields"

    @property
    def description(self) -> str:
        return "Required fields that are null or empty"

    def run(self, item_dir: Path, item_data: dict[str, Any]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        records = item_data.get("db_records", [])
        if not records:
            return findings

        record = records[0]
        null_fields = []
        for field in REQUIRED_RULING_FIELDS:
            value = record.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                null_fields.append(field)

        if null_fields:
            findings.append(
                {
                    "check": self.name,
                    "severity": "warning",
                    "message": f"Missing required fields: {', '.join(null_fields)}",
                    "detail": {"null_fields": null_fields},
                }
            )

        return findings


@register_check("rulings")
class BoilerplateCheck(Check):
    """Detect ruling text that is generic boilerplate."""

    @property
    def name(self) -> str:
        return "boilerplate"

    @property
    def description(self) -> str:
        return "Ruling text that appears to be boilerplate or placeholder"

    def run(self, item_dir: Path, item_data: dict[str, Any]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        records = item_data.get("db_records", [])
        if not records:
            return findings

        record = records[0]
        ruling_text = record.get("ruling_text", "") or ""
        stripped = ruling_text.strip()

        if not stripped:
            return findings

        for pattern in BOILERPLATE_PATTERNS:
            if pattern.match(stripped):
                findings.append(
                    {
                        "check": self.name,
                        "severity": "info",
                        "message": f"Ruling text appears to be boilerplate: {stripped[:80]}",
                        "detail": {"text_preview": stripped[:200]},
                    }
                )
                break

        # Also flag very short ruling text (less than 20 chars)
        if len(stripped) < 20:
            findings.append(
                {
                    "check": self.name,
                    "severity": "info",
                    "message": f"Ruling text is very short ({len(stripped)} chars)",
                    "detail": {"text_preview": stripped, "length": len(stripped)},
                }
            )

        return findings


@register_check("rulings")
class CaseNumberInTitleCheck(Check):
    """Detect case_title that contains the case_number."""

    @property
    def name(self) -> str:
        return "case_number_in_title"

    @property
    def description(self) -> str:
        return "case_title contains the case_number (likely extraction issue)"

    def run(self, item_dir: Path, item_data: dict[str, Any]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        records = item_data.get("db_records", [])
        if not records:
            return findings

        record = records[0]
        case_title = (record.get("case_title") or "").strip()
        case_number = (record.get("case_number") or "").strip()

        if case_title and case_number and case_number in case_title:
            findings.append(
                {
                    "check": self.name,
                    "severity": "warning",
                    "message": f"case_title '{case_title}' contains case_number '{case_number}'",
                    "detail": {
                        "case_title": case_title,
                        "case_number": case_number,
                    },
                }
            )

        return findings


# ---------------------------------------------------------------------------
# Originals checks
# ---------------------------------------------------------------------------


@register_check("originals")
class DerivedRulingsCountCheck(Check):
    """Check count of derived rulings from an original document."""

    @property
    def name(self) -> str:
        return "derived_rulings_count"

    @property
    def description(self) -> str:
        return "Count of derived rulings from original document"

    def run(self, item_dir: Path, item_data: dict[str, Any]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        derived = item_data.get("derived_rulings", [])

        if not derived:
            findings.append(
                {
                    "check": self.name,
                    "severity": "error",
                    "message": "No derived rulings found for this document",
                    "detail": {"count": 0},
                }
            )
        elif len(derived) == 1:
            # Single ruling is normal for single-case docs but worth noting
            findings.append(
                {
                    "check": self.name,
                    "severity": "info",
                    "message": "Document produced exactly 1 ruling (verify this is expected)",
                    "detail": {"count": 1},
                }
            )

        return findings


# ---------------------------------------------------------------------------
# Review runner
# ---------------------------------------------------------------------------


def review_item(
    entity_type: str,
    item_dir: Path,
) -> dict[str, Any]:
    """Run all registered checks for an entity type on a single item.

    Args:
        entity_type: The entity type (e.g. 'rulings', 'originals').
        item_dir: Path to the item's directory.

    Returns:
        A dict with the item ID, all findings, and a flagged boolean.
    """
    item_id = item_dir.name

    # Load item data based on entity type
    item_data: dict[str, Any] = {}
    if entity_type == "rulings":
        db_record_path = item_dir / "db_record.json"
        if db_record_path.exists():
            item_data["db_records"] = json.loads(
                db_record_path.read_text(encoding="utf-8")
            )
    elif entity_type == "originals":
        derived_path = item_dir / "derived_rulings.json"
        if derived_path.exists():
            item_data["derived_rulings"] = json.loads(
                derived_path.read_text(encoding="utf-8")
            )

    # Run checks
    checks = CHECKS.get(entity_type, [])
    all_findings: list[dict[str, Any]] = []
    for check_cls in checks:
        check = check_cls()
        findings = check.run(item_dir, item_data)
        all_findings.extend(findings)

    has_warnings_or_errors = any(
        f["severity"] in ("warning", "error") for f in all_findings
    )

    return {
        "id": item_id,
        "findings": all_findings,
        "flagged": has_warnings_or_errors,
    }


def review_all(spotcheck_dir: Path) -> dict[str, Any]:
    """Run checks on all items in a spotcheck directory.

    Args:
        spotcheck_dir: Root directory of the spotcheck output.

    Returns:
        A summary dict with all findings.
    """
    manifest_path = spotcheck_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"ERROR: No manifest.json in {spotcheck_dir}", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entity_type = manifest["entity"]

    items_dir = spotcheck_dir / "items"
    if not items_dir.exists():
        print(f"ERROR: No items/ directory in {spotcheck_dir}", file=sys.stderr)
        sys.exit(1)

    results: list[dict[str, Any]] = []
    for item_dir in sorted(items_dir.iterdir()):
        if item_dir.is_dir():
            result = review_item(entity_type, item_dir)
            results.append(result)

    flagged_count = sum(1 for r in results if r["flagged"])

    return {
        "entity": entity_type,
        "county": manifest.get("county", ""),
        "total_items": len(results),
        "flagged_items": flagged_count,
        "items": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run deterministic checks on spotcheck artifacts",
    )
    parser.add_argument(
        "spotcheck_dir",
        help="Path to the spotcheck output directory",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Output findings JSON path (default: <spotcheck_dir>/findings.json)",
    )
    args = parser.parse_args()

    spotcheck_dir = Path(args.spotcheck_dir)
    summary = review_all(spotcheck_dir)

    output_path = Path(args.output) if args.output else spotcheck_dir / "findings.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(f"Review complete: {summary['total_items']} items checked", file=sys.stderr)
    print(
        f"Flagged: {summary['flagged_items']}/{summary['total_items']}",
        file=sys.stderr,
    )
    print(f"Findings: {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
