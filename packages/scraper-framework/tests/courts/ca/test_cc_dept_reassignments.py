"""Tests for Contra Costa department reassignment mapping.

See: https://github.com/judgemind/judgemind/issues/2612
"""

from __future__ import annotations

from datetime import datetime

from courts.ca.cc_dept_reassignments import (
    apply_cc_dept_reassignment,
    load_cc_dept_reassignments,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class TestLoadsJsonFixtures:
    """AC4 — reassignment table is data-driven, not hardcoded."""

    def test_loads_four_entries(self) -> None:
        entries = load_cc_dept_reassignments()
        assert len(entries) == 4

    def test_entry_tuples(self) -> None:
        """Verify (old_dept, new_dept, eff_date) for every entry."""
        from datetime import date

        entries = load_cc_dept_reassignments()
        expected = [
            ("9", "34", date(2026, 1, 5)),
            ("18", "32", date(2026, 1, 5)),
            ("34", "14", date(2026, 1, 5)),
            ("39", "10", date(2026, 3, 2)),
        ]
        actual = [(e.old_department, e.new_department, e.effective_date) for e in entries]
        assert actual == expected

    def test_entry_3_has_filters(self) -> None:
        """Third entry (34 → 14) has case_number_prefix and courthouse filters."""
        entries = load_cc_dept_reassignments()
        rule = entries[2]
        assert rule.filters is not None
        assert rule.filters["case_number_prefix"] == "L"
        assert rule.filters["courthouse"] == "Richmond Courthouse"

    def test_other_entries_have_no_filters(self) -> None:
        entries = load_cc_dept_reassignments()
        for rule in [entries[0], entries[1], entries[3]]:
            assert rule.filters is None


# ---------------------------------------------------------------------------
# Dept 9 → 34 (no filter, effective 2026-01-05)
# ---------------------------------------------------------------------------


class TestDept9Reassignment:
    def test_before_effective_date_unchanged(self) -> None:
        """AC1 — hearing before effective date: no reassignment."""
        result = apply_cc_dept_reassignment("9", datetime(2025, 12, 20))
        assert result == "9"

    def test_after_effective_date_becomes_34(self) -> None:
        """AC1 — hearing after effective date: dept 9 → 34."""
        result = apply_cc_dept_reassignment("9", datetime(2026, 1, 10))
        assert result == "34"

    def test_exactly_on_effective_date_becomes_34(self) -> None:
        """Boundary: hearing_date == effective_date is inclusive → reassign."""
        result = apply_cc_dept_reassignment("9", datetime(2026, 1, 5))
        assert result == "34"

    def test_leading_zero_variant_reassigned(self) -> None:
        """Dept '09' (zero-padded) is treated identically to '9'."""
        result = apply_cc_dept_reassignment("09", datetime(2026, 1, 10))
        assert result == "34"


# ---------------------------------------------------------------------------
# Dept 18 → 32 (no filter, effective 2026-01-05)
# ---------------------------------------------------------------------------


class TestDept18Reassignment:
    def test_dept_18_before_unchanged(self) -> None:
        result = apply_cc_dept_reassignment("18", datetime(2025, 12, 31))
        assert result == "18"

    def test_dept_18_after_becomes_32(self) -> None:
        result = apply_cc_dept_reassignment("18", datetime(2026, 1, 6))
        assert result == "32"


# ---------------------------------------------------------------------------
# Dept 39 → 10 (no filter, effective 2026-03-02)
# ---------------------------------------------------------------------------


class TestDept39Reassignment:
    def test_dept_39_before_unchanged(self) -> None:
        result = apply_cc_dept_reassignment("39", datetime(2026, 3, 1))
        assert result == "39"

    def test_dept_39_on_effective_date_becomes_10(self) -> None:
        result = apply_cc_dept_reassignment("39", datetime(2026, 3, 2))
        assert result == "10"

    def test_dept_39_after_becomes_10(self) -> None:
        result = apply_cc_dept_reassignment("39", datetime(2026, 4, 1))
        assert result == "10"


# ---------------------------------------------------------------------------
# Dept 34 → 14 (filtered: case_number_prefix="L", courthouse="Richmond Courthouse")
# ---------------------------------------------------------------------------


class TestDept34FilteredReassignment:
    def test_limited_civil_richmond_to_14(self) -> None:
        """Limited Civil case at Richmond Courthouse: 34 → 14."""
        result = apply_cc_dept_reassignment(
            "34",
            datetime(2026, 2, 1),
            case_number="L24-00001",
            courthouse="Richmond Courthouse",
        )
        assert result == "14"

    def test_civil_case_not_reassigned(self) -> None:
        """Non-L prefix case_number: filter does not match → stays 34."""
        result = apply_cc_dept_reassignment(
            "34",
            datetime(2026, 2, 1),
            case_number="C24-00001",
            courthouse="Richmond Courthouse",
        )
        assert result == "34"

    def test_limited_in_martinez_not_reassigned(self) -> None:
        """Wrong courthouse: filter does not match → stays 34."""
        result = apply_cc_dept_reassignment(
            "34",
            datetime(2026, 2, 1),
            case_number="L24-00001",
            courthouse="Martinez Courthouse",
        )
        assert result == "34"

    def test_missing_case_number_skips_rule(self) -> None:
        """case_number=None with case_number_prefix filter: skip (don't misfire)."""
        result = apply_cc_dept_reassignment(
            "34",
            datetime(2026, 2, 1),
            case_number=None,
            courthouse="Richmond Courthouse",
        )
        assert result == "34"

    def test_missing_courthouse_skips_rule(self) -> None:
        """courthouse=None with courthouse filter: no match → stays 34."""
        result = apply_cc_dept_reassignment(
            "34",
            datetime(2026, 2, 1),
            case_number="L24-00001",
            courthouse=None,
        )
        assert result == "34"


# ---------------------------------------------------------------------------
# Pass-through cases
# ---------------------------------------------------------------------------


class TestPassthrough:
    def test_unknown_department_passthrough(self) -> None:
        """Department not in any rule: returned unchanged."""
        result = apply_cc_dept_reassignment("16", datetime(2026, 2, 1))
        assert result == "16"

    def test_none_department_passthrough(self) -> None:
        """None department: returned as None."""
        result = apply_cc_dept_reassignment(None, datetime(2026, 2, 1))
        assert result is None

    def test_missing_hearing_date_passthrough(self) -> None:
        """hearing_date=None: cannot apply date-keyed map, return unchanged."""
        result = apply_cc_dept_reassignment("9", None)
        assert result == "9"

    def test_dept_before_all_effective_dates_passthrough(self) -> None:
        """Very early hearing date: no rules fire, dept unchanged."""
        result = apply_cc_dept_reassignment("9", datetime(2020, 1, 1))
        assert result == "9"
