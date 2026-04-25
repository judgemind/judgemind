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
  6. ``posting_days`` is present and contains valid day abbreviations.
  7. ``expected_null_rates`` counties exist in ``counties`` section.
  8. ``expected_null_rates`` field names and values are valid.
  9. ``scraper_schedules`` keys are known scraper IDs and cron exprs are valid.

Usage:
    scripts/validate-dq-baselines.py                      # default path
    scripts/validate-dq-baselines.py --path /tmp/b.json   # custom path

Exit code: 0 if valid, 1 if validation errors found.
"""
# permanent: true

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

        # posting_days validation — required for all counties
        posting_days = cfg.get("posting_days")
        if posting_days is None:
            errors.append(f"County '{county}' is missing required 'posting_days' field")
        elif not isinstance(posting_days, list):
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


# Valid field names that can appear in expected_null_rates entries.
VALID_NULL_RATE_FIELDS = {
    "ruling",
    "judge",
    "motion_type",
    "outcome",
    "case_title",
    "case_number",
    "parties",
    "hearing_date",
    "case_type",
}


def validate_expected_null_rates(
    enr_section: dict[str, Any],
    counties: dict[str, Any],
) -> list[str]:
    """Validate the ``expected_null_rates`` section structure.

    Checks:
    - Counties referenced in expected_null_rates exist in ``counties``
    - Field names are valid (match VALID_NULL_RATE_FIELDS)
    - Values are numeric and in range [0, 100]
    - Non-numeric keys start with ``_`` (metadata convention)

    Args:
        enr_section: The ``expected_null_rates`` section of baselines.
        counties: The ``counties`` section of baselines.

    Returns:
        List of error messages (empty if valid).
    """
    errors: list[str] = []
    county_names = set(counties.keys())

    for key, value in enr_section.items():
        # Skip metadata keys (e.g. _note, _updated).
        if key.startswith("_"):
            continue

        # The key should be a county name.
        if key not in county_names:
            errors.append(
                f"County '{key}' in 'expected_null_rates' is not in 'counties'"
            )

        if not isinstance(value, dict):
            errors.append(
                f"County '{key}' in 'expected_null_rates' must be a dict, "
                f"got {type(value).__name__}"
            )
            continue

        for field, rate in value.items():
            if field.startswith("_"):
                continue

            if field not in VALID_NULL_RATE_FIELDS:
                errors.append(
                    f"County '{key}' in 'expected_null_rates' has unknown "
                    f"field '{field}' (valid: {', '.join(sorted(VALID_NULL_RATE_FIELDS))})"
                )
                continue

            if not isinstance(rate, (int, float)):
                errors.append(
                    f"County '{key}' expected_null_rates.{field} must be "
                    f"numeric, got {type(rate).__name__}"
                )
            elif rate < 0 or rate > 100:
                errors.append(
                    f"County '{key}' expected_null_rates.{field}={rate} "
                    f"is out of range [0, 100]"
                )

    return errors


def validate_scraper_schedules(
    schedules_section: dict[str, Any],
) -> list[str]:
    """Validate the ``scraper_schedules`` section structure.

    Checks:
    - Every non-metadata key is a known scraper ID (cross-checked via
      ``framework.runner.get_scraper_ids()``).
    - Every entry with a ``cron`` key has a value that parses with croniter.

    Falls back gracefully if ``framework.runner`` is unavailable (e.g. running
    outside the scraper-framework venv) — only cron parse errors are reported.

    Args:
        schedules_section: The ``scraper_schedules`` section of baselines.

    Returns:
        List of error messages (empty if valid).
    """
    errors: list[str] = []

    # Try to load known scraper IDs from the registry.
    known_ids: set[str] | None = None
    try:
        import sys

        _sf_src = _REPO_ROOT / "packages" / "scraper-framework" / "src"
        if str(_sf_src) not in sys.path:
            sys.path.insert(0, str(_sf_src))
        from framework.runner import get_scraper_ids  # type: ignore[import-untyped]

        known_ids = set(get_scraper_ids())
    except Exception:
        # Graceful fallback — scraper-framework not importable in this env.
        known_ids = None

    # Try to import croniter for cron expression validation.
    _croniter_cls: type | None = None
    try:
        from croniter import croniter as _cron_import  # type: ignore[import-untyped]

        _croniter_cls = _cron_import
    except ImportError:
        pass

    for key, entry in schedules_section.items():
        if key.startswith("_"):
            continue  # Metadata key

        if known_ids is not None and key not in known_ids:
            errors.append(
                f"scraper_schedules key '{key}' is not a known scraper ID "
                f"(known: {', '.join(sorted(known_ids))})"
            )

        if not isinstance(entry, dict):
            errors.append(
                f"scraper_schedules entry '{key}' must be a dict, "
                f"got {type(entry).__name__}"
            )
            continue

        cron_expr = entry.get("cron")
        if cron_expr is None:
            continue  # No cron override — valid (fire always)

        if not isinstance(cron_expr, str) or not cron_expr.strip():
            errors.append(
                f"scraper_schedules['{key}'].cron must be a non-empty string, "
                f"got {type(cron_expr).__name__}: {cron_expr!r}"
            )
            continue

        if _croniter_cls is not None:
            try:
                if not _croniter_cls.is_valid(cron_expr):
                    errors.append(
                        f"scraper_schedules['{key}'].cron '{cron_expr}' "
                        f"is not a valid cron expression"
                    )
            except Exception as exc:
                errors.append(
                    f"scraper_schedules['{key}'].cron '{cron_expr}' "
                    f"failed to parse: {exc}"
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
    enr_section = baselines.get("expected_null_rates") or {}
    schedules_section = baselines.get("scraper_schedules") or {}

    errors.extend(validate_county_consistency(counties, fc_section))
    errors.extend(validate_reasonable_expectations(counties, fc_section))
    errors.extend(validate_required_fc_fields(fc_section))
    errors.extend(validate_county_config(counties))
    errors.extend(validate_expected_null_rates(enr_section, counties))
    errors.extend(validate_scraper_schedules(schedules_section))

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
