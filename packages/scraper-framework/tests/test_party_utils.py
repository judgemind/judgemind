"""Tests for framework.party_utils — shared party name splitting utilities."""

from __future__ import annotations

from framework.party_utils import CORP_SUFFIX_RE, is_name_fragment, split_party_names

# ---------------------------------------------------------------------------
# is_name_fragment
# ---------------------------------------------------------------------------


class TestIsNameFragment:
    def test_corporate_suffixes(self) -> None:
        assert is_name_fragment("Inc") is True
        assert is_name_fragment("Inc.") is True
        assert is_name_fragment("LLC") is True
        assert is_name_fragment("Corp") is True
        assert is_name_fragment("Ltd") is True
        assert is_name_fragment("LP") is True
        assert is_name_fragment("LLP") is True
        assert is_name_fragment("PLLC") is True
        assert is_name_fragment("PLC") is True

    def test_short_words(self) -> None:
        assert is_name_fragment("AB") is True
        assert is_name_fragment("") is True
        assert is_name_fragment("   ") is True

    def test_valid_names(self) -> None:
        assert is_name_fragment("John Doe") is False
        assert is_name_fragment("Techno-Advanced, Inc.") is False
        assert is_name_fragment("Google") is False
        assert is_name_fragment("Jane Smith Corporation") is False


# ---------------------------------------------------------------------------
# split_party_names
# ---------------------------------------------------------------------------


class TestSplitPartyNames:
    def test_keeps_corporate_suffix(self) -> None:
        result = split_party_names("Techno-Advanced, Inc.")
        assert result == ["Techno-Advanced, Inc."]

    def test_keeps_llc_suffix(self) -> None:
        result = split_party_names("Big Corp, LLC")
        assert result == ["Big Corp, LLC"]

    def test_multiple_with_corporate(self) -> None:
        result = split_party_names("John Doe, Techno-Advanced, Inc., and Jane Smith")
        assert len(result) == 3
        assert "John Doe" in result
        assert "Jane Smith" in result
        assert any("Techno" in r and "Inc" in r for r in result)

    def test_two_corporates(self) -> None:
        result = split_party_names("Alpha, LLC, and Beta, Inc.")
        assert len(result) == 2
        assert any("Alpha" in r and "LLC" in r for r in result)
        assert any("Beta" in r and "Inc" in r for r in result)

    def test_filters_fragment(self) -> None:
        result = split_party_names("Inc")
        assert result == []

    def test_filters_short_fragment(self) -> None:
        result = split_party_names("AB")
        assert result == []

    def test_keeps_long_single_word(self) -> None:
        result = split_party_names("Google")
        assert result == ["Google"]

    def test_oxford_comma_split(self) -> None:
        result = split_party_names("Alice Smith, Robert Jones, and Charlie Brown")
        assert result == ["Alice Smith", "Robert Jones", "Charlie Brown"]

    def test_and_split_without_commas(self) -> None:
        result = split_party_names("Alpha Corp and Beta Corp")
        assert result == ["Alpha Corp", "Beta Corp"]


# ---------------------------------------------------------------------------
# CORP_SUFFIX_RE
# ---------------------------------------------------------------------------


class TestCorpSuffixRe:
    def test_matches_inc(self) -> None:
        assert CORP_SUFFIX_RE.search(", Inc.")

    def test_matches_llc(self) -> None:
        assert CORP_SUFFIX_RE.search(", LLC")

    def test_no_match_on_plain_text(self) -> None:
        assert CORP_SUFFIX_RE.search("Hello World") is None
