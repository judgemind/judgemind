"""Contra Costa County department reassignment mapping.

Judges periodically move between departments.  This module loads a
JSON table that maps old department numbers to new ones, keyed by an
effective date and optional case-type/courthouse filters.

The reassignment is applied *after* ``normalize_department`` in the
ingestion worker so that both freshly-scraped captures and full
``rebuild_db.py`` replays pick up the mapping.

See: https://github.com/judgemind/judgemind/issues/2612
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class CCDeptReassignment:
    """A single department reassignment rule."""

    old_department: str
    new_department: str
    effective_date: date
    filters: dict | None


@lru_cache(maxsize=1)
def load_cc_dept_reassignments() -> list[CCDeptReassignment]:
    """Load and return the Contra Costa department reassignment table.

    The table is read from the JSON file colocated with this module.
    Results are cached for the lifetime of the process.
    """
    data_path = Path(__file__).parent / "cc_dept_reassignments.json"
    raw = json.loads(data_path.read_text())
    entries: list[CCDeptReassignment] = []
    for item in raw:
        eff_date = date.fromisoformat(item["effective_date"])
        entries.append(
            CCDeptReassignment(
                old_department=item["old_department"],
                new_department=item["new_department"],
                effective_date=eff_date,
                filters=item.get("filters"),
            )
        )
    return entries


def _strip_leading_zeros(value: str) -> str:
    """Normalize a department string by removing leading zeros.

    Examples: ``"09"`` → ``"9"``, ``"34"`` → ``"34"``.
    Handles the purely-numeric case; non-numeric strings are unchanged.
    """
    if value.isdigit():
        return str(int(value))
    return value


def apply_cc_dept_reassignment(
    department: str | None,
    hearing_date: datetime | None,
    case_number: str | None = None,
    courthouse: str | None = None,
) -> str | None:
    """Apply the Contra Costa department reassignment table to a ruling.

    Parameters
    ----------
    department:
        The (already-normalized) department string from the scraper or
        LLM, e.g. ``"9"``, ``"34"``.  ``None`` is returned unchanged.
    hearing_date:
        The hearing ``datetime`` for the ruling.  When ``None`` the
        reassignment cannot be keyed by date, so ``department`` is
        returned unchanged.
    case_number:
        Optional case number (e.g. ``"L24-00001"``).  Required when a
        reassignment rule has a ``case_number_prefix`` filter.
    courthouse:
        Optional courthouse name.  Required when a reassignment rule has
        a ``courthouse`` filter.

    Returns
    -------
    str | None
        The remapped department string, or ``department`` unchanged when
        no rule fires.
    """
    if department is None or hearing_date is None:
        return department

    hearing_date_only = hearing_date.date() if isinstance(hearing_date, datetime) else hearing_date

    # Normalize the incoming department for comparison (strip leading zeros).
    normalized_input = _strip_leading_zeros(department.strip())

    for rule in load_cc_dept_reassignments():
        # Department must match (both sides normalized).
        if _strip_leading_zeros(rule.old_department) != normalized_input:
            continue

        # Hearing date must be on or after effective date.
        if hearing_date_only < rule.effective_date:
            continue

        # Evaluate filters.
        if rule.filters:
            filters_match = True

            if "case_number_prefix" in rule.filters:
                # If we don't have a case_number, skip — don't misfire.
                if case_number is None:
                    filters_match = False
                else:
                    expected_prefix = rule.filters["case_number_prefix"]
                    if not case_number.startswith(expected_prefix):
                        filters_match = False

            if "courthouse" in rule.filters and filters_match:
                if courthouse != rule.filters["courthouse"]:
                    filters_match = False

            if not filters_match:
                continue

        # All conditions satisfied — return the new department.
        return rule.new_department

    return department
