"""Unit tests for framework.dual_run_diff — pure logic, no DB.

Coverage targets (from task.md AC map):
  AC#2 → test_per_department_diff_basic
  AC#3 → test_flag_missing_portal_department
  AC#4 → test_flag_new_portal_coverage
  Additional: test_case_level_set_differences, test_empty_inputs,
              test_date_filtering, test_format_summary_*
"""

from __future__ import annotations

from datetime import date

from framework.dual_run_diff import (
    Capture,
    DiffRow,
    TotalCoverageSignal,
    compute_per_department_diff,
    compute_total_coverage_signal,
    format_summary,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DATE_A = date(2026, 4, 15)
DATE_B = date(2026, 4, 16)


def _cap(
    scraper_id: str,
    dept: str,
    case_number: str,
    hearing_date: date = DATE_A,
) -> Capture:
    return Capture(
        scraper_id=scraper_id,
        department=dept,
        case_number=case_number,
        hearing_date=hearing_date,
    )


def _retired(dept: str, case_number: str, hearing_date: date = DATE_A) -> Capture:
    return _cap("ca-cc-tentatives", dept, case_number, hearing_date)


def _portal(dept: str, case_number: str, hearing_date: date = DATE_A) -> Capture:
    return _cap("ca-cc-tentatives-portal", dept, case_number, hearing_date)


# ---------------------------------------------------------------------------
# AC#2 — basic per-department diff (both present, equal counts)
# ---------------------------------------------------------------------------


class TestPerDepartmentDiffBasic:
    def test_both_present_equal_counts(self) -> None:
        """Both scrapers have same cases in same department — flag=equal."""
        captures_a = [_retired("16", "C24-01001"), _retired("16", "C24-01002")]
        captures_b = [_portal("16", "C24-01001"), _portal("16", "C24-01002")]

        rows = compute_per_department_diff(captures_a, captures_b, DATE_A)

        assert len(rows) == 1
        row = rows[0]
        assert row.department == "16"
        assert row.count_a == 2
        assert row.count_b == 2
        assert row.flag == "equal"
        assert row.cases_only_a == []
        assert row.cases_only_b == []

    def test_both_present_mismatch_counts(self) -> None:
        """Both scrapers have the dept but different cases — flag=mismatch."""
        captures_a = [_retired("16", "C24-01001"), _retired("16", "C24-01002")]
        captures_b = [_portal("16", "C24-01001"), _portal("16", "C24-01003")]

        rows = compute_per_department_diff(captures_a, captures_b, DATE_A)

        assert len(rows) == 1
        row = rows[0]
        assert row.department == "16"
        assert row.flag == "mismatch"
        assert row.count_a == 2
        assert row.count_b == 2
        assert "C24-01002" in row.cases_only_a
        assert "C24-01003" in row.cases_only_b

    def test_result_sorted_by_department(self) -> None:
        """Output rows are sorted by department."""
        captures_a = [
            _retired("30", "N25-0001"),
            _retired("09", "C24-00001"),
            _retired("16", "C24-00002"),
        ]
        captures_b = [
            _portal("30", "N25-0001"),
            _portal("09", "C24-00001"),
            _portal("16", "C24-00002"),
        ]

        rows = compute_per_department_diff(captures_a, captures_b, DATE_A)
        depts = [r.department for r in rows]
        assert depts == sorted(depts)


# ---------------------------------------------------------------------------
# AC#3 — portal omits a department the retired scraper captured
# ---------------------------------------------------------------------------


class TestFlagMissingPortalDepartment:
    def test_portal_missing_dept_flagged(self) -> None:
        """Portal omits dept 16 → flag=missing_portal for that dept."""
        captures_a = [_retired("16", "C24-01001"), _retired("30", "N25-0001")]
        captures_b = [_portal("30", "N25-0001")]

        rows = compute_per_department_diff(captures_a, captures_b, DATE_A)

        # Should have two rows: one for each dept touched by either side
        by_dept = {r.department: r for r in rows}
        assert "16" in by_dept
        assert "30" in by_dept

        dept16 = by_dept["16"]
        assert dept16.flag == "missing_portal"
        assert dept16.count_a == 1
        assert dept16.count_b == 0

        dept30 = by_dept["30"]
        assert dept30.flag == "equal"

    def test_all_portal_missing_all_flagged(self) -> None:
        """Portal has zero captures — every retired dept is missing_portal."""
        captures_a = [_retired("09", "C24-01001"), _retired("16", "C24-01002")]
        captures_b: list[Capture] = []

        rows = compute_per_department_diff(captures_a, captures_b, DATE_A)

        assert all(r.flag == "missing_portal" for r in rows)
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# AC#4 — portal captures a department not in retired scraper
# ---------------------------------------------------------------------------


class TestFlagNewPortalCoverage:
    def test_portal_new_dept_flagged(self) -> None:
        """Portal captures dept 99 not seen in retired → flag=new_portal_coverage."""
        captures_a = [_retired("16", "C24-01001")]
        captures_b = [_portal("16", "C24-01001"), _portal("99", "C24-09999")]

        rows = compute_per_department_diff(captures_a, captures_b, DATE_A)

        by_dept = {r.department: r for r in rows}
        assert "99" in by_dept
        dept99 = by_dept["99"]
        assert dept99.flag == "new_portal_coverage"
        assert dept99.count_a == 0
        assert dept99.count_b == 1

    def test_portal_only_all_new_coverage(self) -> None:
        """Retired scraper has zero captures — all portal depts are new_portal_coverage."""
        captures_a: list[Capture] = []
        captures_b = [_portal("16", "C24-01001"), _portal("30", "N25-0001")]

        rows = compute_per_department_diff(captures_a, captures_b, DATE_A)

        assert all(r.flag == "new_portal_coverage" for r in rows)
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# Case-level set differences within same department
# ---------------------------------------------------------------------------


class TestCaseLevelSetDifferences:
    def test_disjoint_case_sets_same_count(self) -> None:
        """Same count but completely different case numbers → mismatch."""
        captures_a = [_retired("16", "C24-00001"), _retired("16", "C24-00002")]
        captures_b = [_portal("16", "C24-00003"), _portal("16", "C24-00004")]

        rows = compute_per_department_diff(captures_a, captures_b, DATE_A)
        assert len(rows) == 1
        row = rows[0]
        assert row.flag == "mismatch"
        assert set(row.cases_only_a) == {"C24-00001", "C24-00002"}
        assert set(row.cases_only_b) == {"C24-00003", "C24-00004"}

    def test_partial_overlap(self) -> None:
        """Partial case overlap — cases_only_a/b contain the non-overlapping cases."""
        captures_a = [_retired("16", "C24-00001"), _retired("16", "C24-00002")]
        captures_b = [_portal("16", "C24-00001"), _portal("16", "C24-00003")]

        rows = compute_per_department_diff(captures_a, captures_b, DATE_A)
        row = rows[0]
        assert row.flag == "mismatch"
        assert row.cases_only_a == ["C24-00002"]
        assert row.cases_only_b == ["C24-00003"]

    def test_cases_only_lists_are_sorted(self) -> None:
        """cases_only_a and cases_only_b are sorted for stable output."""
        captures_a = [
            _retired("16", "C24-00003"),
            _retired("16", "C24-00001"),
            _retired("16", "C24-00002"),
        ]
        captures_b: list[Capture] = []

        rows = compute_per_department_diff(captures_a, captures_b, DATE_A)
        row = rows[0]
        assert row.cases_only_a == ["C24-00001", "C24-00002", "C24-00003"]


# ---------------------------------------------------------------------------
# Empty inputs
# ---------------------------------------------------------------------------


class TestEmptyInputs:
    def test_both_empty_returns_empty_list(self) -> None:
        rows = compute_per_department_diff([], [], DATE_A)
        assert rows == []

    def test_one_side_empty_returns_correct_flags(self) -> None:
        """Only retired side has data — all are missing_portal."""
        captures_a = [_retired("16", "C24-01001")]
        rows = compute_per_department_diff(captures_a, [], DATE_A)
        assert len(rows) == 1
        assert rows[0].flag == "missing_portal"


# ---------------------------------------------------------------------------
# Date filtering
# ---------------------------------------------------------------------------


class TestDateFiltering:
    def test_captures_from_different_dates_excluded(self) -> None:
        """Only captures matching the given date are used in the diff."""
        captures_a = [
            _retired("16", "C24-01001", DATE_A),
            _retired("16", "C24-01002", DATE_B),  # different date — excluded
        ]
        captures_b = [
            _portal("16", "C24-01001", DATE_A),
            _portal("16", "C24-01002", DATE_B),  # different date — excluded
        ]

        rows = compute_per_department_diff(captures_a, captures_b, DATE_A)

        # Only DATE_A captures should be counted
        assert len(rows) == 1
        assert rows[0].count_a == 1
        assert rows[0].count_b == 1
        assert rows[0].flag == "equal"

    def test_all_wrong_date_returns_empty(self) -> None:
        """No captures match the given date → empty diff."""
        captures_a = [_retired("16", "C24-01001", DATE_B)]
        captures_b = [_portal("16", "C24-01001", DATE_B)]

        rows = compute_per_department_diff(captures_a, captures_b, DATE_A)
        assert rows == []

    def test_mixed_dates_only_target_counted(self) -> None:
        """Mix of dates — only target date contributes to counts."""
        captures_a = [
            _retired("16", "C24-01001", DATE_A),
            _retired("30", "N25-0001", DATE_B),  # different date
        ]
        captures_b = [
            _portal("16", "C24-01001", DATE_A),
        ]

        rows = compute_per_department_diff(captures_a, captures_b, DATE_A)
        depts = {r.department for r in rows}
        # dept 30 from DATE_B should not appear
        assert "30" not in depts
        assert "16" in depts


# ---------------------------------------------------------------------------
# format_summary
# ---------------------------------------------------------------------------


class TestFormatSummary:
    def test_empty_diff_summary(self) -> None:
        """Empty diff produces a short summary."""
        result = format_summary([])
        assert "0" in result or "no" in result.lower() or result.strip() != ""

    def test_summary_contains_flagged_departments(self) -> None:
        """Summary mentions flagged departments."""
        rows = [
            DiffRow(
                department="16",
                count_a=2,
                count_b=0,
                cases_only_a=["C24-00001", "C24-00002"],
                cases_only_b=[],
                flag="missing_portal",
            ),
            DiffRow(
                department="30",
                count_a=1,
                count_b=1,
                cases_only_a=[],
                cases_only_b=[],
                flag="equal",
            ),
        ]
        summary = format_summary(rows)
        assert "16" in summary
        assert "missing_portal" in summary

    def test_summary_is_string(self) -> None:
        """format_summary always returns a string."""
        rows = [
            DiffRow(
                department="09",
                count_a=0,
                count_b=1,
                cases_only_a=[],
                cases_only_b=["C24-00001"],
                flag="new_portal_coverage",
            )
        ]
        assert isinstance(format_summary(rows), str)

    def test_summary_renders_mismatch_block(self) -> None:
        """Summary includes a MISMATCHED CASE SETS section when any row has
        flag='mismatch'. Covers the dual_run_diff.format_summary mismatch
        rendering branch (lines 218-226).
        """
        rows = [
            DiffRow(
                department="16",
                count_a=2,
                count_b=2,
                cases_only_a=["C24-00001"],
                cases_only_b=["C24-00099"],
                flag="mismatch",
            ),
        ]
        summary = format_summary(rows)
        assert "MISMATCHED CASE SETS" in summary
        assert "dept 16" in summary
        # Both only_retired and only_portal lines should render when
        # the cases_only_* lists are non-empty
        assert "only_retired=" in summary
        assert "only_portal=" in summary


# ---------------------------------------------------------------------------
# Additional — None-department filtering (AC#2 edge case)
# ---------------------------------------------------------------------------


class TestNoneDepartmentFiltering:
    def test_captures_with_none_department_excluded(self) -> None:
        """Captures whose department is None are filtered out before diff —
        e.g. a portal capture whose PDF filename did not match the dept-prefix
        pattern. Covers dual_run_diff line 129 (the ``if cap.department is None``
        continue branch).
        """
        # Retired side has a real dept-16 capture; portal side has one capture
        # with department=None (e.g. PDF filename had no parseable prefix).
        captures_a = [_retired("16", "C24-01001")]
        captures_b = [
            Capture(
                scraper_id="ca-cc-tentatives-portal",
                department=None,
                case_number="C24-01001",
                hearing_date=DATE_A,
            )
        ]
        rows = compute_per_department_diff(captures_a, captures_b, DATE_A)
        # Portal side filtered to empty → dept 16 appears as missing_portal
        assert len(rows) == 1
        assert rows[0].department == "16"
        assert rows[0].flag == "missing_portal"
        assert rows[0].count_b == 0


# ---------------------------------------------------------------------------
# AC#1 — total-coverage signal (portal captured nothing at all)
# ---------------------------------------------------------------------------


def _missing_row(dept: str, count_a: int) -> DiffRow:
    return DiffRow(
        department=dept,
        count_a=count_a,
        count_b=0,
        cases_only_a=[f"C24-{dept}{i:03d}" for i in range(count_a)],
        cases_only_b=[],
        flag="missing_portal",
    )


class TestComputeTotalCoverageSignal:
    def test_portal_zero_single_department(self) -> None:
        """Portal captured nothing while retired has one department —
        portal_total_zero fires and headline names total-coverage failure.
        """
        rows = compute_per_department_diff([_retired("16", "C24-01001")], [], DATE_A)
        signal = compute_total_coverage_signal(rows)

        assert isinstance(signal, TotalCoverageSignal)
        assert signal.portal_total_zero is True
        assert signal.retired_total == 1
        assert signal.portal_total == 0
        assert "total-coverage failure" in signal.headline

    def test_portal_zero_multiple_departments(self) -> None:
        """Portal captured nothing while retired spans several departments."""
        captures_a = [
            _retired("09", "C24-00001"),
            _retired("16", "C24-00002"),
            _retired("16", "C24-00003"),
            _retired("30", "N25-0001"),
        ]
        rows = compute_per_department_diff(captures_a, [], DATE_A)
        signal = compute_total_coverage_signal(rows)

        assert signal.portal_total_zero is True
        assert signal.retired_total == 4
        assert signal.portal_total == 0
        assert "total-coverage failure" in signal.headline

    def test_partial_coverage_does_not_fire(self) -> None:
        """Portal has some departments — signal does NOT fire."""
        captures_a = [_retired("16", "C24-01001"), _retired("30", "N25-0001")]
        captures_b = [_portal("30", "N25-0001")]
        rows = compute_per_department_diff(captures_a, captures_b, DATE_A)
        signal = compute_total_coverage_signal(rows)

        assert signal.portal_total_zero is False
        assert signal.retired_total == 2
        assert signal.portal_total == 1
        assert signal.headline == ""

    def test_empty_diff_does_not_fire(self) -> None:
        """Empty diff (no captures either side) — signal does NOT fire."""
        signal = compute_total_coverage_signal([])
        assert signal.portal_total_zero is False
        assert signal.retired_total == 0
        assert signal.portal_total == 0
        assert signal.headline == ""

    def test_portal_only_does_not_fire(self) -> None:
        """Retired captured nothing but portal has data — does NOT fire
        (the signal targets portal-side total-coverage failure only)."""
        rows = compute_per_department_diff([], [_portal("16", "C24-01001")], DATE_A)
        signal = compute_total_coverage_signal(rows)
        assert signal.portal_total_zero is False
        assert signal.retired_total == 0
        assert signal.portal_total == 1
        assert signal.headline == ""


# ---------------------------------------------------------------------------
# AC#2 / AC#3 — format_summary headline prepend
# ---------------------------------------------------------------------------


class TestFormatSummaryTotalCoverageHeadline:
    def test_headline_and_per_department_rows_both_render(self) -> None:
        """When total-zero fires, the headline block prepends ABOVE the
        existing per-department MISSING FROM PORTAL table (both render)."""
        rows = compute_per_department_diff(
            [_retired("09", "C24-00001"), _retired("16", "C24-00002")], [], DATE_A
        )
        summary = format_summary(rows)

        assert "TOTAL-COVERAGE FAILURE" in summary
        assert "total-coverage failure" in summary
        # Per-department table still produced (AC#2)
        assert "MISSING FROM PORTAL" in summary
        assert "dept 09" in summary
        assert "dept 16" in summary
        # Headline is ABOVE the per-department table
        assert summary.index("TOTAL-COVERAGE FAILURE") < summary.index("MISSING FROM PORTAL")

    def test_headline_absent_on_partial_coverage(self) -> None:
        """When portal has partial coverage, no headline block renders, and
        the per-department table is unchanged from today."""
        rows = compute_per_department_diff(
            [_retired("16", "C24-01001"), _retired("30", "N25-0001")],
            [_portal("30", "N25-0001")],
            DATE_A,
        )
        summary = format_summary(rows)
        assert "TOTAL-COVERAGE FAILURE" not in summary
        assert "total-coverage failure" not in summary
        assert "MISSING FROM PORTAL" in summary

    def test_empty_diff_keeps_existing_short_message(self) -> None:
        """format_summary([]) keeps its existing short message — no headline."""
        summary = format_summary([])
        assert "TOTAL-COVERAGE FAILURE" not in summary
        assert "0 departments compared" in summary
