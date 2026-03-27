"""Tests for framework.extraction_config — county extraction configuration.

Validates the ExtractionMethod enum, CountyExtractionConfig dataclass,
the county configuration registry, and the get_county_extraction_config
lookup function.
"""

from __future__ import annotations

import pytest

from framework.extraction_config import (
    RIVERSIDE_SYSTEM_PROMPT,
    SAN_BERNARDINO_SYSTEM_PROMPT,
    CountyExtractionConfig,
    ExtractionMethod,
    get_county_extraction_config,
)

# ---------------------------------------------------------------------------
# ExtractionMethod enum
# ---------------------------------------------------------------------------


class TestExtractionMethod:
    """Verify ExtractionMethod enum values and string representation."""

    def test_llm_value(self) -> None:
        assert ExtractionMethod.LLM == "llm"
        assert ExtractionMethod.LLM.value == "llm"

    def test_multimodal_value(self) -> None:
        assert ExtractionMethod.MULTIMODAL == "multimodal"
        assert ExtractionMethod.MULTIMODAL.value == "multimodal"

    def test_none_value(self) -> None:
        assert ExtractionMethod.NONE == "none"
        assert ExtractionMethod.NONE.value == "none"

    def test_is_str_enum(self) -> None:
        """ExtractionMethod is a StrEnum — values are usable as strings."""
        assert isinstance(ExtractionMethod.LLM, str)
        assert f"method={ExtractionMethod.LLM}" == "method=llm"


# ---------------------------------------------------------------------------
# CountyExtractionConfig dataclass
# ---------------------------------------------------------------------------


class TestCountyExtractionConfig:
    """Verify CountyExtractionConfig defaults and construction."""

    def test_defaults(self) -> None:
        """Default config uses LLM method with no overrides."""
        config = CountyExtractionConfig()
        assert config.method == ExtractionMethod.LLM
        assert config.system_prompt is None
        assert config.provider is None
        assert config.model is None
        assert config.max_output_tokens is None

    def test_custom_values(self) -> None:
        """Custom values are stored correctly."""
        config = CountyExtractionConfig(
            method=ExtractionMethod.MULTIMODAL,
            system_prompt="custom prompt",
            provider="google",
            model="gemini-2.5-flash-lite",
            max_output_tokens=16384,
        )
        assert config.method == ExtractionMethod.MULTIMODAL
        assert config.system_prompt == "custom prompt"
        assert config.provider == "google"
        assert config.model == "gemini-2.5-flash-lite"
        assert config.max_output_tokens == 16384

    def test_frozen(self) -> None:
        """CountyExtractionConfig is frozen (immutable)."""
        config = CountyExtractionConfig()
        with pytest.raises(AttributeError):
            config.method = ExtractionMethod.NONE  # type: ignore[misc]


# ---------------------------------------------------------------------------
# get_county_extraction_config — registry lookup
# ---------------------------------------------------------------------------


class TestGetCountyExtractionConfig:
    """Verify county config lookup behavior."""

    def test_riverside_registered(self) -> None:
        """Riverside County has a registered config (#1728)."""
        config = get_county_extraction_config("CA", "Riverside")
        assert config is not None
        assert config.method == ExtractionMethod.LLM
        assert config.provider == "google"
        assert config.model == "gemini-2.5-flash-lite"
        assert config.max_output_tokens == 32768
        assert config.system_prompt is not None

    def test_orange_registered(self) -> None:
        """Orange County has a registered config (multimodal)."""
        config = get_county_extraction_config("CA", "Orange")
        assert config is not None
        assert config.method == ExtractionMethod.MULTIMODAL

    def test_san_bernardino_registered(self) -> None:
        """San Bernardino County has a registered config (#2050)."""
        config = get_county_extraction_config("CA", "San Bernardino")
        assert config is not None
        assert config.method == ExtractionMethod.LLM
        assert config.provider == "google"
        assert config.model == "gemini-2.5-flash-lite"
        assert config.max_output_tokens == 32768
        assert config.system_prompt is not None

    def test_case_insensitive_state(self) -> None:
        """State code lookup is case-insensitive."""
        assert get_county_extraction_config("ca", "Riverside") is not None
        assert get_county_extraction_config("Ca", "Riverside") is not None

    def test_case_insensitive_county(self) -> None:
        """County name lookup is case-insensitive."""
        assert get_county_extraction_config("CA", "riverside") is not None
        assert get_county_extraction_config("CA", "RIVERSIDE") is not None

    def test_case_insensitive_san_bernardino(self) -> None:
        """San Bernardino lookup is case-insensitive."""
        assert get_county_extraction_config("CA", "san bernardino") is not None
        assert get_county_extraction_config("CA", "SAN BERNARDINO") is not None
        assert get_county_extraction_config("CA", "San Bernardino") is not None

    def test_unknown_county_returns_none(self) -> None:
        """Unknown county returns None."""
        assert get_county_extraction_config("CA", "Nonexistent") is None

    def test_unknown_state_returns_none(self) -> None:
        """Unknown state returns None."""
        assert get_county_extraction_config("XX", "Riverside") is None


# ---------------------------------------------------------------------------
# RIVERSIDE_SYSTEM_PROMPT content validation
# ---------------------------------------------------------------------------


class TestRiversideSystemPrompt:
    """Verify the Riverside system prompt has required content."""

    def test_mentions_riverside(self) -> None:
        assert "riverside" in RIVERSIDE_SYSTEM_PROMPT.lower()

    def test_mentions_two_layer_structure(self) -> None:
        """Prompt instructs capture of two-layer structure (#1948)."""
        assert "two-layer" in RIVERSIDE_SYSTEM_PROMPT.lower()

    def test_mentions_detailed_analysis(self) -> None:
        """Prompt instructs capture of full legal analysis."""
        assert "detailed analysis" in RIVERSIDE_SYSTEM_PROMPT.lower()

    def test_warns_against_truncation(self) -> None:
        """Prompt warns against truncation of ruling text."""
        assert "truncat" in RIVERSIDE_SYSTEM_PROMPT.lower()

    def test_mentions_character_threshold(self) -> None:
        """Prompt mentions 200-character threshold for short rulings."""
        assert "200 characters" in RIVERSIDE_SYSTEM_PROMPT.lower()

    def test_mentions_case_number_formats(self) -> None:
        """Prompt describes Riverside case number patterns."""
        assert "CVPS" in RIVERSIDE_SYSTEM_PROMPT
        assert "RIC" in RIVERSIDE_SYSTEM_PROMPT

    def test_mentions_cross_references(self) -> None:
        """Prompt describes cross-reference entries ('See #N Above')."""
        assert "see #" in RIVERSIDE_SYSTEM_PROMPT.lower()

    def test_mentions_outcome_taxonomy(self) -> None:
        """Prompt includes outcome taxonomy values."""
        assert "granted" in RIVERSIDE_SYSTEM_PROMPT.lower()
        assert "denied" in RIVERSIDE_SYSTEM_PROMPT.lower()
        assert "continued" in RIVERSIDE_SYSTEM_PROMPT.lower()
        assert "off_calendar" in RIVERSIDE_SYSTEM_PROMPT.lower()

    def test_mentions_json_output(self) -> None:
        """Prompt specifies JSON output format."""
        assert "json" in RIVERSIDE_SYSTEM_PROMPT.lower()
        assert "rulings" in RIVERSIDE_SYSTEM_PROMPT.lower()

    def test_mentions_parties(self) -> None:
        """Prompt instructs party extraction."""
        assert "plaintiff" in RIVERSIDE_SYSTEM_PROMPT.lower()
        assert "defendant" in RIVERSIDE_SYSTEM_PROMPT.lower()

    def test_mentions_page_footers(self) -> None:
        """Prompt instructs stripping of page footers."""
        assert "Page N of M" in RIVERSIDE_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# SAN_BERNARDINO_SYSTEM_PROMPT content validation (#2050)
# ---------------------------------------------------------------------------


class TestSanBernardinoSystemPrompt:
    """Verify the San Bernardino system prompt has required content."""

    def test_mentions_san_bernardino(self) -> None:
        assert "san bernardino" in SAN_BERNARDINO_SYSTEM_PROMPT.lower()

    def test_mentions_two_layer_structure(self) -> None:
        """Prompt instructs capture of two-layer structure."""
        assert "two-layer" in SAN_BERNARDINO_SYSTEM_PROMPT.lower()

    def test_mentions_full_analysis(self) -> None:
        """Prompt instructs capture of full legal analysis."""
        assert "full legal analysis" in SAN_BERNARDINO_SYSTEM_PROMPT.lower()

    def test_warns_against_truncation(self) -> None:
        """Prompt warns against truncation of ruling text."""
        assert "truncat" in SAN_BERNARDINO_SYSTEM_PROMPT.lower()

    def test_mentions_case_number_formats(self) -> None:
        """Prompt describes San Bernardino case number patterns."""
        assert "CIVSB" in SAN_BERNARDINO_SYSTEM_PROMPT
        assert "CIVRS" in SAN_BERNARDINO_SYSTEM_PROMPT

    def test_mentions_horizontal_rules(self) -> None:
        """Prompt describes horizontal rule separators between cases."""
        assert "horizontal rule" in SAN_BERNARDINO_SYSTEM_PROMPT.lower()

    def test_mentions_two_header_formats(self) -> None:
        """Prompt describes both header formats (Department-Judge and HONORABLE)."""
        assert "Department" in SAN_BERNARDINO_SYSTEM_PROMPT
        assert "HONORABLE" in SAN_BERNARDINO_SYSTEM_PROMPT

    def test_mentions_outcome_taxonomy(self) -> None:
        """Prompt includes outcome taxonomy values."""
        assert "granted" in SAN_BERNARDINO_SYSTEM_PROMPT.lower()
        assert "denied" in SAN_BERNARDINO_SYSTEM_PROMPT.lower()
        assert "continued" in SAN_BERNARDINO_SYSTEM_PROMPT.lower()
        assert "off_calendar" in SAN_BERNARDINO_SYSTEM_PROMPT.lower()

    def test_mentions_json_output(self) -> None:
        """Prompt specifies JSON output format."""
        assert "json" in SAN_BERNARDINO_SYSTEM_PROMPT.lower()
        assert "rulings" in SAN_BERNARDINO_SYSTEM_PROMPT.lower()

    def test_mentions_parties(self) -> None:
        """Prompt instructs party extraction."""
        assert "plaintiff" in SAN_BERNARDINO_SYSTEM_PROMPT.lower()
        assert "defendant" in SAN_BERNARDINO_SYSTEM_PROMPT.lower()

    def test_mentions_page_footers(self) -> None:
        """Prompt instructs stripping of page footers."""
        assert "Page N of M" in SAN_BERNARDINO_SYSTEM_PROMPT

    def test_mentions_space_normalization(self) -> None:
        """Prompt instructs removing internal spaces from case numbers."""
        assert "CIVSB 2600093" in SAN_BERNARDINO_SYSTEM_PROMPT
        assert "CIVSB2600093" in SAN_BERNARDINO_SYSTEM_PROMPT

    def test_mentions_single_case_multiple_motions(self) -> None:
        """Prompt explains that multiple motions under one case number are one ruling."""
        assert "multiple motions" in SAN_BERNARDINO_SYSTEM_PROMPT.lower()
        assert "ONE ruling" in SAN_BERNARDINO_SYSTEM_PROMPT
