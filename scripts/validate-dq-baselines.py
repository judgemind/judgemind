#!/usr/bin/env python3
"""Validate structural consistency of data-quality-baselines.json.

Checks that the baselines file is internally consistent without requiring
database access.  Intended to run in CI or as a pre-push check.

Validations:
  1. Counties in ``field_completeness`` match counties in ``counties``.
  2. Low-document counties have reasonable ``expected_daily_rulings``.
  3. Required fields are present in each ``field_completeness`` entry.
  4. ``schedule_type`` values are valid ("daily" or "frequent").
  5. ``expected_daily_rulings`` is non-negative.
  6. ``posting_days`` values (if present) are valid day abbreviations.

Usage:
    scripts/validate-dq-baselines.py                      # default path
    scripts/validate-dq-baselines.py --path /tmp/b.json   # custom path

Exit code: 0 if valid, 1 if validation errors found.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Resolve repo root from scripts/ directory.
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
DEFAULT_BASELINES_PATH = _REPO_ROOT / "data-quality-baselines.json"

VALID_SCHEDULE_TYPES = {"daily", "frequent"}
VALID_POSTING_DAYS = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}

# Required numeric fields in each field_completeness county entry.
REQUIRED_FC_FIELDS = {
    "total_documents",
    "ruling",
    "judge",
    "motion_type",
    "outcome",
    "case_title",
    "case_number",
    "parties",
    "hearing_date",
}

# Counties with fewer than this many total documents should have
# expected_daily_rulings <= MAX_DAILY_FOR_LOW_DOC_COUNTY.
LOW_DOCUMENT_THRESHOLD = 10
MAX_DAILY_FOR_LOW_DOC_COUNTY = 1


def load_baselines_json(path: Path) -> dict[str, Any]:
    """Load and parse the baselines JSON file.

    Args:
        path: Path to the baselines JSON file.

    Returns:
        Parsed JSON as a dict.

    Raises:
        SystemExit: If the file is missing or contains invalid JSON.
    """
    if not path.exists():
        print(f"ERROR: Baselines file not found: {path}")
        sys.exit(1)
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Invalid JSON in {path}: {exc}")
        sys.exit(1)


def _county_names_from_fc(fc_section: dict[str, Any]) -> set[str]:
    """Extract county names from field_completeness, skipping metadata keys."""
    return {k for k in fc_section if not k.startswith("_")}


def validate_county_consistency(
    counties: dict[str, Any],
    fc_section: dict[str, Any],
) -> list[str]:
    """Check that counties match between sections.

    Args:
        counties: The ``counties`` section of baselines.
        fc_section: The ``field_completeness`` section of baselines.

    Returns:
        List of error messages (empty if valid).
    """
    errors: list[str] = []
    county_names = set(counties.keys())
    fc_names = _county_names_from_fc(fc_section)

    only_in_counties = county_names - fc_names
    only_in_fc = fc_names - county_names

    for name in sorted(only_in_counties):
        errors.append(
            f"County '{name}' is in 'counties' but missing from 'field_completeness'"
        )
    for name in sorted(only_in_fc):
        errors.append(
            f"County '{name}' is in 'field_completeness' but missing from 'counties'"
        )

    return errors


def validate_reasonable_expectations(
    counties: dict[str, Any],
    fc_section: dict[str, Any],
) -> list[str]:
    """Flag counties with low doc counts but high expected daily rulings.

    Args:
        counties: The ``counties`` section of baselines.
        fc_section: The ``field_completeness`` section of baselines.

    Returns:
        List of error messages.
    """
    errors: list[str] = []
    fc_names = _county_names_from_fc(fc_section)

    for county in sorted(fc_names):
        fc_entry = fc_section[county]
        if not isinstance(fc_entry, dict):
            # Non-dict entries are caught by validate_required_fc_fields.
            continue
        total_docs = fc_entry.get("total_documents", 0)
        if total_docs >= LOW_DOCUMENT_THRESHOLD:
            continue

        county_cfg = counties.get(county)
        if county_cfg is None or not isinstance(county_cfg, dict):
            # Missing counties are flagged by consistency check;
            # non-dict configs are flagged by validate_county_config.
            continue

        expected_daily = county_cfg.get("expected_daily_rulings", 0)
        if expected_daily > MAX_DAILY_FOR_LOW_DOC_COUNTY:
            errors.append(
                f"County '{county}' has only {total_docs} total documents "
                f"but expected_daily_rulings={expected_daily} "
                f"(should be <= {MAX_DAILY_FOR_LOW_DOC_COUNTY} for counties "
                f"with < {LOW_DOCUMENT_THRESHOLD} documents)"
            )

    return errors


def validate_required_fc_fields(fc_section: dict[str, Any]) -> list[str]:
    """Check that each field_completeness entry has all required fields.

    Args:
        fc_section: The ``field_completeness`` section of baselines.

    Returns:
        List of error messages.
    """
    errors: list[str] = []
    fc_names = _county_names_from_fc(fc_section)

    for county in sorted(fc_names):
        entry = fc_section[county]
        # Skip non-dict metadata values (shouldn't happen, but be safe).
        if not isinstance(entry, dict):
            errors.append(f"County '{county}' in field_completeness is not a dict")
            continue

        missing = REQUIRED_FC_FIELDS - set(entry.keys())
        if missing:
            errors.append(
                f"County '{county}' in field_completeness is missing "
                f"required fields: {', '.join(sorted(missing))}"
            )

    return errors


def validate_county_config(counties: dict[str, Any]) -> list[str]:
    """Validate individual county configuration values.

    Checks:
    - ``schedule_type`` is "daily" or "frequent"
    - ``expected_daily_rulings`` is non-negative
    - ``posting_days`` values are valid day abbreviations

    Args:
        counties: The ``counties`` section of baselines.

    Returns:
        List of error messages.
    """
    errors: list[str] = []

    for county in sorted(counties):
        cfg = counties[county]
        if not isinstance(cfg, dict):
            errors.append(
                f"County '{county}' config in 'counties' must be a dictionary, "
                f"got {type(cfg).__name__}"
            )
            continue

        # schedule_type validation
        schedule = cfg.get("schedule_type")
        if schedule is not None and schedule not in VALID_SCHEDULE_TYPES:
            errors.append(
                f"County '{county}' has invalid schedule_type '{schedule}' "
                f"(must be one of: {', '.join(sorted(VALID_SCHEDULE_TYPES))})"
            )

        # expected_daily_rulings validation
        edr = cfg.get("expected_daily_rulings")
        if edr is not None and edr < 0:
            errors.append(
                f"County '{county}' has negative expected_daily_rulings: {edr}"
            )

        # posting_days validation
        posting_days = cfg.get("posting_days")
        if posting_days is not None:
            if not isinstance(posting_days, list):
                errors.append(
                    f"County '{county}' posting_days must be a list, "
                    f"got {type(posting_days).__name__}"
                )
            else:
                invalid_days = set(posting_days) - VALID_POSTING_DAYS
                if invalid_days:
                    errors.append(
                        f"County '{county}' has invalid posting_days: "
                        f"{', '.join(sorted(invalid_days))} "
                        f"(valid: {', '.join(sorted(VALID_POSTING_DAYS))})"
                    )

    return errors


def validate(baselines: dict[str, Any]) -> list[str]:
    """Run all validation checks on the baselines data.

    Args:
        baselines: Parsed baselines JSON data.

    Returns:
        List of all error messages (empty if valid).
    """
    errors: list[str] = []

    counties = baselines.get("counties") or {}
    fc_section = baselines.get("field_completeness") or {}

    errors.extend(validate_county_consistency(counties, fc_section))
    errors.extend(validate_reasonable_expectations(counties, fc_section))
    errors.extend(validate_required_fc_fields(fc_section))
    errors.extend(validate_county_config(counties))

    return errors


def main() -> int:
    """Run validation and print results.

    Returns:
        0 if valid, 1 if errors found.
    """
    parser = argparse.ArgumentParser(
        description="Validate data-quality-baselines.json structural consistency",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_BASELINES_PATH,
        help="Path to baselines JSON file (default: repo root)",
    )
    args = parser.parse_args()

    baselines = load_baselines_json(args.path)
    errors = validate(baselines)

    if errors:
        print(f"Validation FAILED — {len(errors)} error(s) found:\n")
        for error in errors:
            print(f"  - {error}")
        print()
        return 1

    print("Validation passed — data-quality-baselines.json is structurally consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
