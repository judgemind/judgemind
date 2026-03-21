"""Tests for the OC split fields backfill script.

Tests cover:
  - clean_case_title: stripping ruling/motion text from garbled titles
  - extract_case_type_from_number: OC case number -> case type mapping
  - extract_parties_from_title: extracting plaintiff/defendant from case titles
"""

from __future__ import annotations

import os
import sys

import pytest

# Add scripts/ to sys.path so we can import the backfill module.
_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts")
sys.path.insert(0, os.path.normpath(_SCRIPTS_DIR))

from backfill_oc_split_fields import (  # noqa: E402
    clean_case_title,
    extract_case_type_from_number,
    extract_parties_from_title,
)

# ---------------------------------------------------------------------------
# clean_case_title tests
# ---------------------------------------------------------------------------


class TestCleanCaseTitle:
    """Test garbled title cleanup."""

    @pytest.mark.parametrize(
        "raw,expected_absent",
        [
            (
                "Gonzalez vs. Attorney\u2019s Fees in the Sum of $1,038,085.00 is GRANTED",
                ["GRANTED"],
            ),
            (
                "Jones vs. Saddle Creek of Class Action and PAGA Settlement is GRANTED IN",
                ["GRANTED", "Class Action"],
            ),
            (
                "Miranda vs. Chiron, Strike is DENIED.",
                ["DENIED"],
            ),
            (
                "Hallum vs. Restaurant to Compel Arbitration is GRANTED. IT IS ORDERED",
                ["GRANTED", "Compel"],
            ),
            (
                "vs. Kelley for Attorney Fees is GRANTED in the reduced amount of $67,746.32.",
                ["GRANTED", "Attorney Fees"],
            ),
            (
                "Robles vs. Bally Plaintiff\u2019s",
                ["Plaintiff"],
            ),
            (
                "Keller vs. System Defendant\u2019s",
                ["Defendant"],
            ),
            (
                "One LLP vs. CONTINUED TO APRIL 21, 2026, AT 9:00 A.M., IN",
                ["APRIL"],
            ),
        ],
    )
    def test_strips_junk(self, raw: str, expected_absent: list[str]) -> None:
        result = clean_case_title(raw)
        for word in expected_absent:
            assert word not in result, f"Expected '{word}' to be absent from '{result}'"

    def test_preserves_clean_title(self) -> None:
        title = "Gomez vs. Black Lion Farms, LLC"
        assert clean_case_title(title) == "Gomez vs. Black Lion Farms, LLC"

    def test_preserves_simple_title(self) -> None:
        title = "Smith vs. Jones"
        assert clean_case_title(title) == "Smith vs. Jones"

    def test_empty_string(self) -> None:
        assert clean_case_title("") == ""


# ---------------------------------------------------------------------------
# extract_case_type_from_number tests
# ---------------------------------------------------------------------------


class TestExtractCaseTypeFromNumber:
    """Test OC case number -> case type mapping."""

    @pytest.mark.parametrize(
        "case_number,expected",
        [
            ("30-2024-01393434", "civil"),
            ("2024-01380242", "civil"),
            ("30-2018-00970921", "civil"),
            ("25-01451474", "civil"),
        ],
    )
    def test_oc_civil_numbers(self, case_number: str, expected: str) -> None:
        assert extract_case_type_from_number(case_number) == expected

    def test_none_input(self) -> None:
        assert extract_case_type_from_number(None) is None  # type: ignore[arg-type]

    def test_empty_string(self) -> None:
        assert extract_case_type_from_number("") is None

    def test_unknown_format(self) -> None:
        assert extract_case_type_from_number("ABC123") is None


# ---------------------------------------------------------------------------
# extract_parties_from_title tests
# ---------------------------------------------------------------------------


class TestExtractPartiesFromTitle:
    """Test party extraction from case titles."""

    def test_basic_vs(self) -> None:
        parties = extract_parties_from_title("Smith vs. Jones")
        assert len(parties) == 2
        assert parties[0]["name"] == "Smith"
        assert parties[0]["role"] == "plaintiff"
        assert parties[1]["name"] == "Jones"
        assert parties[1]["role"] == "defendant"

    def test_vs_without_period(self) -> None:
        parties = extract_parties_from_title("Smith vs Jones")
        assert len(parties) == 2

    def test_v_with_period(self) -> None:
        parties = extract_parties_from_title("Smith v. Jones")
        assert len(parties) == 2

    def test_company_name(self) -> None:
        parties = extract_parties_from_title("Gomez vs. Black Lion Farms, LLC")
        assert len(parties) == 2
        assert "Black Lion Farms, Llc" in parties[1]["name"]

    def test_empty_title(self) -> None:
        assert extract_parties_from_title("") == []

    def test_no_vs(self) -> None:
        assert extract_parties_from_title("Some Random Title") == []

    def test_title_case_normalization(self) -> None:
        parties = extract_parties_from_title("SMITH vs. JONES")
        assert parties[0]["name"] == "Smith"
        assert parties[1]["name"] == "Jones"
