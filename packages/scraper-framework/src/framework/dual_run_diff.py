"""CC Dual-Run Diff — pure logic for comparing Contra Costa scraper captures.

No DB imports. All functions are pure and testable without infrastructure.

See issue #2610 (CC Dual-Run Diff: Phase 2) and
docs/investigations/contra-costa-portal-migration-2026-04.md for context.

The two scraper IDs compared here are:
  * ``ca-cc-tentatives``        — legacy retired-site scraper
  * ``ca-cc-tentatives-portal`` — new Drupal portal scraper (Phase 1, #2609)

Department is extracted at capture time:
  * Retired scraper  — department stored per document from the URL path.
  * Portal scraper   — department derived from PDF filename prefix, e.g.
    ``/system/files/general/16_012925.pdf`` → dept ``"16"``.

The diff compares coverage on a single calendar date, producing one
``DiffRow`` per department that appears on either side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------

FlagType = Literal["missing_portal", "new_portal_coverage", "equal", "mismatch"]


@dataclass
class Capture:
    """A single document captured by one of the two CC scrapers.

    Attributes:
        scraper_id: Either ``'ca-cc-tentatives'`` or
            ``'ca-cc-tentatives-portal'``.
        department: Department string, e.g. ``"16"``, ``"09"``, ``"30"``.
            ``None`` if the department could not be derived (excluded from
            diff computations).
        case_number: Case number, e.g. ``"C24-02490"``.
        hearing_date: Calendar date of the hearing (from the PDF header or
            detail page).
    """

    scraper_id: str
    department: str | None
    case_number: str
    hearing_date: date


@dataclass
class DiffRow:
    """Per-department comparison result for one calendar date.

    Attributes:
        department: Department string, e.g. ``"16"``.
        count_a: Number of captures from ``ca-cc-tentatives`` (retired).
        count_b: Number of captures from ``ca-cc-tentatives-portal``.
        cases_only_a: Sorted list of case numbers present only in A.
        cases_only_b: Sorted list of case numbers present only in B.
        flag: One of:

            * ``'equal'``              — both sides present with same cases.
            * ``'mismatch'``           — both sides present, different cases.
            * ``'missing_portal'``     — dept in A (retired) but absent from B.
            * ``'new_portal_coverage'``— dept in B (portal) but absent from A.
    """

    department: str
    count_a: int
    count_b: int
    cases_only_a: list[str] = field(default_factory=list)
    cases_only_b: list[str] = field(default_factory=list)
    flag: FlagType = "equal"


@dataclass
class TotalCoverageSignal:
    """Top-level coverage signal distinguishing a total-coverage failure from
    per-department gaps.

    Attributes:
        retired_total: Total case count captured by the retired scraper on the date.
        portal_total: Total case count captured by the portal scraper on the date.
        portal_total_zero: True when portal_total == 0 AND retired_total > 0 — the
            portal captured nothing at all while the retired scraper had data, which
            indicates a total-coverage failure (judge discovery / fetch) rather than a
            per-department gap.
        headline: A human-readable one-line headline. Non-empty only when
            portal_total_zero is True.
    """

    retired_total: int
    portal_total: int
    portal_total_zero: bool
    headline: str


# ---------------------------------------------------------------------------
# Scraper ID constants
# ---------------------------------------------------------------------------

_SCRAPER_RETIRED = "ca-cc-tentatives"
_SCRAPER_PORTAL = "ca-cc-tentatives-portal"


# ---------------------------------------------------------------------------
# Core diff function
# ---------------------------------------------------------------------------


def compute_per_department_diff(
    captures_a: list[Capture],
    captures_b: list[Capture],
    target_date: date,
) -> list[DiffRow]:
    """Compare per-department coverage between the two CC scrapers on one date.

    Args:
        captures_a: All captures from the retired scraper
            (``scraper_id='ca-cc-tentatives'``). Only captures whose
            ``hearing_date == target_date`` and ``department`` is not
            ``None`` are included.
        captures_b: All captures from the portal scraper
            (``scraper_id='ca-cc-tentatives-portal'``). Same filtering.
        target_date: The calendar date to compare.

    Returns:
        A list of :class:`DiffRow`, one per department that appears on
        either side, sorted by ``department`` ascending.

    Flag rules:
        * ``'equal'``               — dept present on both sides, same case set.
        * ``'mismatch'``            — dept present on both sides, different case set.
        * ``'missing_portal'``      — dept in A (retired) but not in B (portal).
        * ``'new_portal_coverage'`` — dept in B (portal) but not in A (retired).
    """

    # Filter to target date and non-None department
    def _filter(captures: list[Capture]) -> dict[str, set[str]]:
        """Return {department: {case_numbers}} for the target date."""
        by_dept: dict[str, set[str]] = {}
        for cap in captures:
            if cap.hearing_date != target_date:
                continue
            if cap.department is None:
                continue
            by_dept.setdefault(cap.department, set()).add(cap.case_number)
        return by_dept

    dept_a = _filter(captures_a)
    dept_b = _filter(captures_b)

    all_depts = sorted(dept_a.keys() | dept_b.keys())

    rows: list[DiffRow] = []
    for dept in all_depts:
        cases_a = dept_a.get(dept, set())
        cases_b = dept_b.get(dept, set())

        only_a = sorted(cases_a - cases_b)
        only_b = sorted(cases_b - cases_a)

        in_a = dept in dept_a
        in_b = dept in dept_b

        if in_a and not in_b:
            flag: FlagType = "missing_portal"
        elif in_b and not in_a:
            flag = "new_portal_coverage"
        elif cases_a == cases_b:
            flag = "equal"
        else:
            flag = "mismatch"

        rows.append(
            DiffRow(
                department=dept,
                count_a=len(cases_a),
                count_b=len(cases_b),
                cases_only_a=only_a,
                cases_only_b=only_b,
                flag=flag,
            )
        )

    return rows


# ---------------------------------------------------------------------------
# Total-coverage signal
# ---------------------------------------------------------------------------


def compute_total_coverage_signal(diff_rows: list[DiffRow]) -> TotalCoverageSignal:
    """Derive the top-level coverage signal from per-department diff rows.

    Distinguishes a *total-coverage failure* (the portal captured nothing at
    all while the retired scraper had data — a fetch / judge-discovery break)
    from ordinary per-department gaps. See issue #4600.

    Args:
        diff_rows: Output from :func:`compute_per_department_diff`.

    Returns:
        A :class:`TotalCoverageSignal`. ``portal_total_zero`` is True only when
        the portal total is 0 while the retired total is > 0, and in that case
        ``headline`` names the total-coverage failure; otherwise ``headline``
        is empty.
    """
    retired_total = sum(r.count_a for r in diff_rows)
    portal_total = sum(r.count_b for r in diff_rows)
    portal_total_zero = portal_total == 0 and retired_total > 0

    if portal_total_zero:
        headline = (
            f"Portal captured 0 of {retired_total} rulings the retired scraper "
            "captured — likely a total-coverage failure (judge discovery / fetch), "
            "not a per-department gap"
        )
    else:
        headline = ""

    return TotalCoverageSignal(
        retired_total=retired_total,
        portal_total=portal_total,
        portal_total_zero=portal_total_zero,
        headline=headline,
    )


# ---------------------------------------------------------------------------
# Human-readable summary
# ---------------------------------------------------------------------------


def format_summary(diff_rows: list[DiffRow]) -> str:
    """Format a human-readable diff summary.

    Args:
        diff_rows: Output from :func:`compute_per_department_diff`.

    Returns:
        A multi-line string suitable for logging or issue body text.
    """
    if not diff_rows:
        return "CC dual-run diff: 0 departments compared — no captures on either side."

    signal = compute_total_coverage_signal(diff_rows)

    missing = [r for r in diff_rows if r.flag == "missing_portal"]
    new_cov = [r for r in diff_rows if r.flag == "new_portal_coverage"]
    mismatch = [r for r in diff_rows if r.flag == "mismatch"]
    equal = [r for r in diff_rows if r.flag == "equal"]

    lines: list[str] = []

    if signal.portal_total_zero:
        # Lead with the dominant signal so the alert reframes the failure as a
        # total-coverage break rather than N independent per-department gaps.
        lines.append(f"TOTAL-COVERAGE FAILURE: {signal.headline}")
        lines.append("")

    lines.extend(
        [
            f"CC dual-run diff: {len(diff_rows)} department(s) compared.",
            f"  equal={len(equal)}, mismatch={len(mismatch)}, "
            f"missing_portal={len(missing)}, new_portal_coverage={len(new_cov)}",
        ]
    )

    if missing:
        lines.append("")
        lines.append("MISSING FROM PORTAL (coverage regression risk):")
        for r in missing:
            lines.append(
                f"  dept {r.department}: retired={r.count_a} cases, "
                f"portal=0  | only_retired={r.cases_only_a}"
            )

    if new_cov:
        lines.append("")
        lines.append("NEW PORTAL COVERAGE (portal ahead of retired):")
        for r in new_cov:
            lines.append(
                f"  dept {r.department}: portal={r.count_b} cases, "
                f"retired=0  | only_portal={r.cases_only_b}"
            )

    if mismatch:
        lines.append("")
        lines.append("MISMATCHED CASE SETS:")
        for r in mismatch:
            lines.append(f"  dept {r.department}: retired={r.count_a}, portal={r.count_b}")
            if r.cases_only_a:
                lines.append(f"    only_retired={r.cases_only_a}")
            if r.cases_only_b:
                lines.append(f"    only_portal={r.cases_only_b}")

    return "\n".join(lines)
