"""Tests for framework.extraction_config — county extraction configuration.

Validates the ExtractionMethod enum, CountyExtractionConfig dataclass,
the county configuration registry, and the get_county_extraction_config
lookup function.
"""

from __future__ import annotations

import pytest

from framework.extraction_config import (
    _COUNTY_CONFIGS,
    CONTRA_COSTA_SYSTEM_PROMPT,
    FRESNO_SYSTEM_PROMPT,
    RIVERSIDE_SYSTEM_PROMPT,
    SAN_BERNARDINO_SYSTEM_PROMPT,
    SAN_DIEGO_FRAMEWORK_PROMPT,
    SAN_DIEGO_SYSTEM_PROMPT,
    SAN_FRANCISCO_SYSTEM_PROMPT,
    SANTA_CLARA_SYSTEM_PROMPT,
    VENTURA_FRAMEWORK_PROMPT,
    VENTURA_SYSTEM_PROMPT,
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
        assert config.max_chars_per_chunk is None

    def test_custom_values(self) -> None:
        """Custom values are stored correctly."""
        config = CountyExtractionConfig(
            method=ExtractionMethod.MULTIMODAL,
            system_prompt="custom prompt",
            provider="google",
            model="gemini-2.5-flash-lite",
            max_output_tokens=16384,
            max_chars_per_chunk=40000,
        )
        assert config.method == ExtractionMethod.MULTIMODAL
        assert config.system_prompt == "custom prompt"
        assert config.provider == "google"
        assert config.model == "gemini-2.5-flash-lite"
        assert config.max_output_tokens == 16384
        assert config.max_chars_per_chunk == 40000

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

    def test_san_francisco_registered(self) -> None:
        """San Francisco County has a registered config (#2051)."""
        config = get_county_extraction_config("CA", "San Francisco")
        assert config is not None
        assert config.method == ExtractionMethod.LLM
        assert config.provider == "google"
        assert config.model == "gemini-2.5-flash-lite"
        assert config.max_output_tokens == 32768
        assert config.max_chars_per_chunk == 40_000
        assert config.system_prompt is not None

    def test_case_insensitive_san_francisco(self) -> None:
        """San Francisco lookup is case-insensitive."""
        assert get_county_extraction_config("CA", "san francisco") is not None
        assert get_county_extraction_config("CA", "SAN FRANCISCO") is not None
        assert get_county_extraction_config("CA", "San Francisco") is not None

    def test_fresno_registered(self) -> None:
        """Fresno County has a registered config (#2052)."""
        config = get_county_extraction_config("CA", "Fresno")
        assert config is not None
        assert config.method == ExtractionMethod.LLM
        assert config.provider == "google"
        assert config.model == "gemini-2.5-flash-lite"
        assert config.max_output_tokens == 32768
        assert config.system_prompt is not None

    def test_case_insensitive_fresno(self) -> None:
        """Fresno lookup is case-insensitive."""
        assert get_county_extraction_config("CA", "fresno") is not None
        assert get_county_extraction_config("CA", "FRESNO") is not None
        assert get_county_extraction_config("CA", "Fresno") is not None

    def test_santa_clara_registered(self) -> None:
        """Santa Clara County has a registered config (#2054, #2312)."""
        config = get_county_extraction_config("CA", "Santa Clara")
        assert config is not None
        assert config.method == ExtractionMethod.MULTIMODAL

    def test_case_insensitive_santa_clara(self) -> None:
        """Santa Clara lookup is case-insensitive."""
        assert get_county_extraction_config("CA", "santa clara") is not None
        assert get_county_extraction_config("CA", "SANTA CLARA") is not None
        assert get_county_extraction_config("CA", "Santa Clara") is not None

    def test_contra_costa_registered(self) -> None:
        """Contra Costa County has a registered config (#2093)."""
        config = get_county_extraction_config("CA", "Contra Costa")
        assert config is not None
        assert config.method == ExtractionMethod.LLM
        assert config.provider == "google"
        assert config.model == "gemini-2.5-flash-lite"
        assert config.max_output_tokens == 32768
        assert config.system_prompt is not None

    def test_case_insensitive_contra_costa(self) -> None:
        """Contra Costa lookup is case-insensitive."""
        assert get_county_extraction_config("CA", "contra costa") is not None
        assert get_county_extraction_config("CA", "CONTRA COSTA") is not None
        assert get_county_extraction_config("CA", "Contra Costa") is not None

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


# ---------------------------------------------------------------------------
# SAN_FRANCISCO_SYSTEM_PROMPT content validation (#2051)
# ---------------------------------------------------------------------------


class TestSanFranciscoSystemPrompt:
    """Verify the San Francisco system prompt has required content."""

    def test_mentions_san_francisco(self) -> None:
        assert "san francisco" in SAN_FRANCISCO_SYSTEM_PROMPT.lower()

    def test_mentions_family_court(self) -> None:
        """Prompt identifies this as Family Court / Family Law."""
        assert "family" in SAN_FRANCISCO_SYSTEM_PROMPT.lower()

    def test_mentions_cover_pages(self) -> None:
        """Prompt instructs skipping cover pages."""
        assert "cover page" in SAN_FRANCISCO_SYSTEM_PROMPT.lower()

    def test_mentions_petitioner_respondent(self) -> None:
        """Prompt uses petitioner/respondent (not plaintiff/defendant for parties)."""
        assert "petitioner" in SAN_FRANCISCO_SYSTEM_PROMPT.lower()
        assert "respondent" in SAN_FRANCISCO_SYSTEM_PROMPT.lower()

    def test_mentions_case_number_format(self) -> None:
        """Prompt describes SF Family Law case number patterns."""
        assert "FPT" in SAN_FRANCISCO_SYSTEM_PROMPT
        assert "FMS" in SAN_FRANCISCO_SYSTEM_PROMPT
        assert "FDI" in SAN_FRANCISCO_SYSTEM_PROMPT

    def test_mentions_outcome_taxonomy(self) -> None:
        """Prompt includes outcome taxonomy values."""
        assert "granted" in SAN_FRANCISCO_SYSTEM_PROMPT.lower()
        assert "denied" in SAN_FRANCISCO_SYSTEM_PROMPT.lower()
        assert "continued" in SAN_FRANCISCO_SYSTEM_PROMPT.lower()
        assert "off_calendar" in SAN_FRANCISCO_SYSTEM_PROMPT.lower()

    def test_mentions_json_output(self) -> None:
        """Prompt specifies JSON output format."""
        assert "json" in SAN_FRANCISCO_SYSTEM_PROMPT.lower()
        assert "rulings" in SAN_FRANCISCO_SYSTEM_PROMPT.lower()

    def test_mentions_parties(self) -> None:
        """Prompt instructs party extraction with correct roles."""
        # SF uses petitioner/respondent in the extracted_parties format
        prompt_lower = SAN_FRANCISCO_SYSTEM_PROMPT.lower()
        assert '"role": "petitioner"' in prompt_lower
        assert '"role": "respondent"' in prompt_lower

    def test_mentions_ruling_text_completeness(self) -> None:
        """Prompt instructs capturing complete ruling text."""
        prompt_lower = SAN_FRANCISCO_SYSTEM_PROMPT.lower()
        assert "procedural history" in prompt_lower
        assert "findings and order" in prompt_lower
        assert "truncat" in prompt_lower

    def test_mentions_judge_extraction(self) -> None:
        """Prompt instructs extracting judge from Presiding line."""
        assert "Presiding" in SAN_FRANCISCO_SYSTEM_PROMPT

    def test_mentions_motion_type_extraction(self) -> None:
        """Prompt instructs extracting motion type from caption area."""
        assert "motion_type" in SAN_FRANCISCO_SYSTEM_PROMPT.lower()

    def test_mentions_multi_case_pdfs(self) -> None:
        """Prompt describes multi-case PDF structure."""
        assert "multi-case" in SAN_FRANCISCO_SYSTEM_PROMPT.lower()

    def test_mentions_domestic_violence_prefix(self) -> None:
        """Prompt includes FDV (domestic violence) case number prefix."""
        assert "FDV" in SAN_FRANCISCO_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# FRESNO_SYSTEM_PROMPT content validation (#2052)
# ---------------------------------------------------------------------------


class TestFresnoSystemPrompt:
    """Verify the Fresno system prompt has required content."""

    def test_mentions_fresno(self) -> None:
        assert "fresno" in FRESNO_SYSTEM_PROMPT.lower()

    def test_mentions_numbered_entries(self) -> None:
        """Prompt describes Fresno's numbered entry format (N) Tentative Ruling."""
        assert "(20)" in FRESNO_SYSTEM_PROMPT
        assert "parentheses" in FRESNO_SYSTEM_PROMPT.lower()

    def test_mentions_case_number_formats(self) -> None:
        """Prompt describes Fresno CECG case number patterns."""
        assert "CECG" in FRESNO_SYSTEM_PROMPT
        assert "25CECG03271" in FRESNO_SYSTEM_PROMPT

    def test_mentions_outcome_taxonomy(self) -> None:
        """Prompt includes outcome taxonomy values."""
        assert "granted" in FRESNO_SYSTEM_PROMPT.lower()
        assert "denied" in FRESNO_SYSTEM_PROMPT.lower()
        assert "continued" in FRESNO_SYSTEM_PROMPT.lower()
        assert "off_calendar" in FRESNO_SYSTEM_PROMPT.lower()

    def test_mentions_json_output(self) -> None:
        """Prompt specifies JSON output format."""
        assert "json" in FRESNO_SYSTEM_PROMPT.lower()
        assert "rulings" in FRESNO_SYSTEM_PROMPT.lower()

    def test_mentions_parties(self) -> None:
        """Prompt instructs party extraction."""
        assert "plaintiff" in FRESNO_SYSTEM_PROMPT.lower()
        assert "defendant" in FRESNO_SYSTEM_PROMPT.lower()

    def test_mentions_judge_initials_only(self) -> None:
        """Prompt explains Fresno has only judge initials, not full names."""
        prompt_lower = FRESNO_SYSTEM_PROMPT.lower()
        assert "initials" in prompt_lower
        assert "extracted_judge_name" in FRESNO_SYSTEM_PROMPT
        assert "null" in prompt_lower

    def test_mentions_multi_page_rulings(self) -> None:
        """Prompt describes multi-page rulings (e.g. PAGA analyses)."""
        assert "multi-page" in FRESNO_SYSTEM_PROMPT.lower()

    def test_mentions_continued_cases(self) -> None:
        """Prompt instructs skipping continued cases from cover page."""
        assert "continued" in FRESNO_SYSTEM_PROMPT.lower()
        assert "skip" in FRESNO_SYSTEM_PROMPT.lower()

    def test_warns_against_truncation(self) -> None:
        """Prompt warns against truncation of ruling text."""
        assert "truncat" in FRESNO_SYSTEM_PROMPT.lower()

    def test_mentions_signature_block(self) -> None:
        """Prompt describes the 'Issued By' signature block format."""
        assert "Issued By" in FRESNO_SYSTEM_PROMPT

    def test_mentions_multiple_motions(self) -> None:
        """Prompt explains that multiple motions under one entry are one ruling."""
        assert "multiple motions" in FRESNO_SYSTEM_PROMPT.lower()
        assert "ONE ruling" in FRESNO_SYSTEM_PROMPT

    def test_mentions_demurrer_mapping(self) -> None:
        """Prompt maps sustained/overruled demurrers to granted/denied."""
        prompt_lower = FRESNO_SYSTEM_PROMPT.lower()
        assert "sustained" in prompt_lower
        assert "overruled" in prompt_lower

    def test_output_format_matches_framework_schema(self) -> None:
        """Prompt output format uses framework field names (extracted_judge_name, etc.)."""
        assert "extracted_judge_name" in FRESNO_SYSTEM_PROMPT
        assert "extracted_case_number" in FRESNO_SYSTEM_PROMPT
        assert "extracted_case_title" in FRESNO_SYSTEM_PROMPT
        assert "extracted_parties" in FRESNO_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# SANTA_CLARA_SYSTEM_PROMPT content validation (#2054)
# ---------------------------------------------------------------------------


class TestSantaClaraSystemPrompt:
    """Verify the Santa Clara system prompt has required content."""

    def test_mentions_santa_clara(self) -> None:
        assert "santa clara" in SANTA_CLARA_SYSTEM_PROMPT.lower()

    def test_mentions_line_table_format(self) -> None:
        """Prompt describes the LINE/CASE NO. summary table format."""
        assert "LINE" in SANTA_CLARA_SYSTEM_PROMPT
        assert "summary table" in SANTA_CLARA_SYSTEM_PROMPT.lower()

    def test_mentions_case_splitting_rules(self) -> None:
        """Prompt has critical case splitting rules (group by case number)."""
        assert "Group by case number" in SANTA_CLARA_SYSTEM_PROMPT
        assert "ONE ruling" in SANTA_CLARA_SYSTEM_PROMPT

    def test_mentions_detailed_rulings(self) -> None:
        """Prompt describes the detailed ruling sections below summary table."""
        assert "detailed ruling" in SANTA_CLARA_SYSTEM_PROMPT.lower()

    def test_mentions_case_number_formats(self) -> None:
        """Prompt describes Santa Clara case number patterns."""
        assert "24CV443183" in SANTA_CLARA_SYSTEM_PROMPT
        assert "25PR199782" in SANTA_CLARA_SYSTEM_PROMPT

    def test_mentions_time_blocks(self) -> None:
        """Prompt describes handling of multiple time blocks (9:01 and 9:00)."""
        assert "time block" in SANTA_CLARA_SYSTEM_PROMPT.lower()

    def test_mentions_outcome_taxonomy(self) -> None:
        """Prompt includes outcome taxonomy values."""
        assert "granted" in SANTA_CLARA_SYSTEM_PROMPT.lower()
        assert "denied" in SANTA_CLARA_SYSTEM_PROMPT.lower()
        assert "continued" in SANTA_CLARA_SYSTEM_PROMPT.lower()
        assert "off_calendar" in SANTA_CLARA_SYSTEM_PROMPT.lower()

    def test_mentions_json_output(self) -> None:
        """Prompt specifies JSON output format."""
        assert "json" in SANTA_CLARA_SYSTEM_PROMPT.lower()
        assert "rulings" in SANTA_CLARA_SYSTEM_PROMPT.lower()

    def test_mentions_parties(self) -> None:
        """Prompt instructs party extraction."""
        assert "plaintiff" in SANTA_CLARA_SYSTEM_PROMPT.lower()
        assert "defendant" in SANTA_CLARA_SYSTEM_PROMPT.lower()

    def test_warns_against_truncation(self) -> None:
        """Prompt warns against truncation of ruling text."""
        assert "truncat" in SANTA_CLARA_SYSTEM_PROMPT.lower()

    def test_mentions_judge_extraction(self) -> None:
        """Prompt instructs extracting judge from Honorable line."""
        assert "Honorable" in SANTA_CLARA_SYSTEM_PROMPT

    def test_mentions_department_extraction(self) -> None:
        """Prompt instructs extracting department number."""
        assert "Department N" in SANTA_CLARA_SYSTEM_PROMPT

    def test_mentions_hearing_date_extraction(self) -> None:
        """Prompt instructs extracting hearing date."""
        assert "DATE:" in SANTA_CLARA_SYSTEM_PROMPT

    def test_mentions_motion_type_labels(self) -> None:
        """Prompt lists common motion type labels."""
        prompt_lower = SANTA_CLARA_SYSTEM_PROMPT.lower()
        assert "demurrer" in prompt_lower
        assert "msj" in prompt_lower
        assert "motion_to_compel" in prompt_lower
        assert "writ_of_attachment" in prompt_lower

    def test_mentions_demurrer_mapping(self) -> None:
        """Prompt maps sustained/overruled demurrers to granted/denied."""
        prompt_lower = SANTA_CLARA_SYSTEM_PROMPT.lower()
        assert "sustained" in prompt_lower
        assert "overruled" in prompt_lower

    def test_output_format_matches_framework_schema(self) -> None:
        """Prompt output format uses framework field names (extracted_judge_name, etc.)."""
        assert "extracted_judge_name" in SANTA_CLARA_SYSTEM_PROMPT
        assert "extracted_case_number" in SANTA_CLARA_SYSTEM_PROMPT
        assert "extracted_case_title" in SANTA_CLARA_SYSTEM_PROMPT
        assert "extracted_parties" in SANTA_CLARA_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Ventura registry and lookup tests (#2055)
# ---------------------------------------------------------------------------


class TestVenturaRegistered:
    """Verify Ventura County is in the extraction config registry."""

    def test_ventura_registered(self) -> None:
        """Ventura County has a registered config (#2055)."""
        config = get_county_extraction_config("CA", "Ventura")
        assert config is not None
        assert config.method == ExtractionMethod.LLM
        assert config.provider == "google"
        assert config.model == "gemini-2.5-flash-lite"
        assert config.max_output_tokens == 32768
        assert config.system_prompt is not None

    def test_case_insensitive_ventura(self) -> None:
        """Ventura lookup is case-insensitive."""
        assert get_county_extraction_config("CA", "ventura") is not None
        assert get_county_extraction_config("CA", "VENTURA") is not None
        assert get_county_extraction_config("CA", "Ventura") is not None


# ---------------------------------------------------------------------------
# VENTURA_SYSTEM_PROMPT content validation (#2055)
# ---------------------------------------------------------------------------


class TestVenturaSystemPrompt:
    """Verify the Ventura system prompt has required content."""

    def test_mentions_ventura(self) -> None:
        assert "ventura" in VENTURA_SYSTEM_PROMPT.lower()

    def test_mentions_single_case(self) -> None:
        """Prompt describes single-case document format."""
        assert "single" in VENTURA_SYSTEM_PROMPT.lower()

    def test_mentions_known_metadata(self) -> None:
        """Prompt describes receiving case_number and motion_type as known context."""
        assert "case_number" in VENTURA_SYSTEM_PROMPT.lower()
        assert "motion_type" in VENTURA_SYSTEM_PROMPT.lower()

    def test_mentions_outcome_taxonomy(self) -> None:
        """Prompt includes outcome taxonomy values."""
        assert "granted" in VENTURA_SYSTEM_PROMPT.lower()
        assert "denied" in VENTURA_SYSTEM_PROMPT.lower()
        assert "continued" in VENTURA_SYSTEM_PROMPT.lower()
        assert "off_calendar" in VENTURA_SYSTEM_PROMPT.lower()

    def test_mentions_parties(self) -> None:
        """Prompt instructs party extraction."""
        assert "plaintiff" in VENTURA_SYSTEM_PROMPT.lower()
        assert "defendant" in VENTURA_SYSTEM_PROMPT.lower()

    def test_mentions_ruling_text(self) -> None:
        """Prompt instructs ruling text extraction."""
        assert "ruling_text" in VENTURA_SYSTEM_PROMPT.lower()
        assert "TENTATIVE RULING" in VENTURA_SYSTEM_PROMPT

    def test_mentions_json_output(self) -> None:
        """Prompt specifies JSON output format."""
        assert "json" in VENTURA_SYSTEM_PROMPT.lower()

    def test_flat_output_format(self) -> None:
        """Ventura prompt uses flat JSON (not wrapped in 'rulings' array)."""
        # The Ventura prompt outputs {outcome, case_title, parties, ...}
        # directly, not {"rulings": [...]}.
        assert '"outcome"' in VENTURA_SYSTEM_PROMPT
        assert '"case_title"' in VENTURA_SYSTEM_PROMPT
        assert '"parties"' in VENTURA_SYSTEM_PROMPT

    def test_mentions_demurrer_mapping(self) -> None:
        """Prompt maps sustained/overruled demurrers."""
        prompt_lower = VENTURA_SYSTEM_PROMPT.lower()
        assert "sustained" in prompt_lower
        assert "overruled" in prompt_lower

    def test_mentions_tentative_ruling_heading(self) -> None:
        """Prompt references TENTATIVE RULING heading for text extraction."""
        assert "TENTATIVE RULING" in VENTURA_SYSTEM_PROMPT

    def test_excludes_boilerplate(self) -> None:
        """Prompt instructs excluding header and footer boilerplate."""
        prompt_lower = VENTURA_SYSTEM_PROMPT.lower()
        assert "exclude" in prompt_lower
        assert "header" in prompt_lower
        assert "footer" in prompt_lower


# ---------------------------------------------------------------------------
# VENTURA_FRAMEWORK_PROMPT content validation (#2271)
# ---------------------------------------------------------------------------


class TestVenturaFrameworkPrompt:
    """Verify the Ventura framework prompt produces the correct format."""

    def test_mentions_ventura(self) -> None:
        assert "ventura" in VENTURA_FRAMEWORK_PROMPT.lower()

    def test_uses_rulings_array_format(self) -> None:
        """Framework prompt must output {"rulings": [...]} format (#2271)."""
        assert '"rulings"' in VENTURA_FRAMEWORK_PROMPT
        assert "extracted_case_number" in VENTURA_FRAMEWORK_PROMPT
        assert "extracted_case_title" in VENTURA_FRAMEWORK_PROMPT
        assert "extracted_parties" in VENTURA_FRAMEWORK_PROMPT

    def test_extracts_outcome(self) -> None:
        """Framework prompt must extract outcome (the field missing in #2271)."""
        assert '"outcome"' in VENTURA_FRAMEWORK_PROMPT

    def test_extracts_motion_type(self) -> None:
        """Framework prompt must extract motion_type."""
        assert '"motion_type"' in VENTURA_FRAMEWORK_PROMPT

    def test_extracts_judge_name(self) -> None:
        """Framework prompt must extract judge name at document level."""
        assert "extracted_judge_name" in VENTURA_FRAMEWORK_PROMPT

    def test_extracts_hearing_date(self) -> None:
        """Framework prompt must extract hearing date."""
        assert '"hearing_date"' in VENTURA_FRAMEWORK_PROMPT

    def test_extracts_department(self) -> None:
        """Framework prompt must extract department."""
        assert '"department"' in VENTURA_FRAMEWORK_PROMPT

    def test_mentions_outcome_taxonomy(self) -> None:
        """Framework prompt includes outcome taxonomy values."""
        prompt_lower = VENTURA_FRAMEWORK_PROMPT.lower()
        assert "granted" in prompt_lower
        assert "denied" in prompt_lower
        assert "continued" in prompt_lower
        assert "off_calendar" in prompt_lower

    def test_mentions_parties(self) -> None:
        """Framework prompt instructs party extraction."""
        prompt_lower = VENTURA_FRAMEWORK_PROMPT.lower()
        assert "plaintiff" in prompt_lower
        assert "defendant" in prompt_lower

    def test_mentions_case_number_format(self) -> None:
        """Framework prompt describes Ventura case number patterns."""
        assert "2024CUBC038456" in VENTURA_FRAMEWORK_PROMPT

    def test_mentions_probate_handling(self) -> None:
        """Framework prompt handles probate/conservatorship documents."""
        prompt_lower = VENTURA_FRAMEWORK_PROMPT.lower()
        assert "probate" in prompt_lower

    def test_mentions_json_output(self) -> None:
        """Framework prompt specifies JSON output format."""
        assert "json" in VENTURA_FRAMEWORK_PROMPT.lower()

    def test_different_from_scraper_prompt(self) -> None:
        """Framework prompt is different from scraper prompt (different format)."""
        # Scraper uses flat JSON, framework uses rulings array
        assert VENTURA_FRAMEWORK_PROMPT != VENTURA_SYSTEM_PROMPT
        # Framework has rulings array, scraper does not
        assert '"rulings"' in VENTURA_FRAMEWORK_PROMPT
        # Both share outcome taxonomy content
        assert "granted_in_part" in VENTURA_FRAMEWORK_PROMPT
        assert "granted_in_part" in VENTURA_SYSTEM_PROMPT


class TestVenturaConfigUsesFrameworkPrompt:
    """Verify the Ventura extraction config uses the framework prompt (#2271)."""

    def test_ventura_config_uses_framework_prompt(self) -> None:
        """Ventura config must use VENTURA_FRAMEWORK_PROMPT, not VENTURA_SYSTEM_PROMPT."""
        config = get_county_extraction_config("CA", "Ventura")
        assert config is not None
        # The config must use the framework prompt that produces {"rulings": [...]}
        assert config.system_prompt is VENTURA_FRAMEWORK_PROMPT
        # It must NOT use the scraper's flat JSON prompt
        assert config.system_prompt is not VENTURA_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# SAN_DIEGO_FRAMEWORK_PROMPT content validation (#2303)
# ---------------------------------------------------------------------------


class TestSanDiegoFrameworkPrompt:
    """Verify the San Diego framework prompt produces the correct format."""

    def test_mentions_san_diego(self) -> None:
        assert "san diego" in SAN_DIEGO_FRAMEWORK_PROMPT.lower()

    def test_uses_rulings_array_format(self) -> None:
        """Framework prompt must output {"rulings": [...]} format (#2303)."""
        assert '"rulings"' in SAN_DIEGO_FRAMEWORK_PROMPT
        assert "extracted_case_number" in SAN_DIEGO_FRAMEWORK_PROMPT
        assert "extracted_case_title" in SAN_DIEGO_FRAMEWORK_PROMPT
        assert "extracted_parties" in SAN_DIEGO_FRAMEWORK_PROMPT

    def test_extracts_outcome(self) -> None:
        """Framework prompt must extract outcome."""
        assert '"outcome"' in SAN_DIEGO_FRAMEWORK_PROMPT

    def test_extracts_motion_type(self) -> None:
        """Framework prompt must extract motion_type."""
        assert '"motion_type"' in SAN_DIEGO_FRAMEWORK_PROMPT

    def test_extracts_judge_name(self) -> None:
        """Framework prompt must extract judge name at document level."""
        assert "extracted_judge_name" in SAN_DIEGO_FRAMEWORK_PROMPT

    def test_extracts_hearing_date(self) -> None:
        """Framework prompt must extract hearing date."""
        assert '"hearing_date"' in SAN_DIEGO_FRAMEWORK_PROMPT

    def test_extracts_department(self) -> None:
        """Framework prompt must extract department."""
        assert '"department"' in SAN_DIEGO_FRAMEWORK_PROMPT

    def test_mentions_outcome_taxonomy(self) -> None:
        """Framework prompt includes outcome taxonomy values."""
        prompt_lower = SAN_DIEGO_FRAMEWORK_PROMPT.lower()
        assert "granted" in prompt_lower
        assert "denied" in prompt_lower
        assert "continued" in prompt_lower
        assert "off_calendar" in prompt_lower

    def test_mentions_parties(self) -> None:
        """Framework prompt instructs party extraction."""
        prompt_lower = SAN_DIEGO_FRAMEWORK_PROMPT.lower()
        assert "plaintiff" in prompt_lower
        assert "defendant" in prompt_lower

    def test_mentions_motion_types(self) -> None:
        """Framework prompt lists common San Diego motion types."""
        prompt_lower = SAN_DIEGO_FRAMEWORK_PROMPT.lower()
        assert "demurrer" in prompt_lower
        assert "motion to compel" in prompt_lower
        assert "motion for summary judgment" in prompt_lower

    def test_mentions_json_output(self) -> None:
        """Framework prompt specifies JSON output format."""
        assert "json" in SAN_DIEGO_FRAMEWORK_PROMPT.lower()

    def test_different_from_scraper_prompt(self) -> None:
        """Framework prompt is different from scraper prompt (different format)."""
        # Scraper uses flat JSON, framework uses rulings array
        assert SAN_DIEGO_FRAMEWORK_PROMPT != SAN_DIEGO_SYSTEM_PROMPT
        # Framework has rulings array, scraper does not
        assert '"rulings"' in SAN_DIEGO_FRAMEWORK_PROMPT
        assert '"rulings"' not in SAN_DIEGO_SYSTEM_PROMPT
        # Both share outcome taxonomy content
        assert "granted_in_part" in SAN_DIEGO_FRAMEWORK_PROMPT
        assert "granted_in_part" in SAN_DIEGO_SYSTEM_PROMPT


class TestSanDiegoConfigUsesFrameworkPrompt:
    """Verify the San Diego extraction config uses the framework prompt (#2303)."""

    def test_san_diego_config_uses_framework_prompt(self) -> None:
        """San Diego config must use SAN_DIEGO_FRAMEWORK_PROMPT, not SAN_DIEGO_SYSTEM_PROMPT."""
        config = get_county_extraction_config("CA", "San Diego")
        assert config is not None
        # The config must use the framework prompt that produces {"rulings": [...]}
        assert config.system_prompt is SAN_DIEGO_FRAMEWORK_PROMPT
        # It must NOT use the scraper's flat JSON prompt
        assert config.system_prompt is not SAN_DIEGO_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# CONTRA_COSTA_SYSTEM_PROMPT content validation (#2093)
# ---------------------------------------------------------------------------


class TestContraCostaSystemPrompt:
    """Verify the Contra Costa system prompt has required content."""

    def test_mentions_contra_costa(self) -> None:
        assert "contra costa" in CONTRA_COSTA_SYSTEM_PROMPT.lower()

    def test_mentions_civil_format(self) -> None:
        """Prompt describes the civil department format (Format A)."""
        assert "Format A" in CONTRA_COSTA_SYSTEM_PROMPT
        assert "civil" in CONTRA_COSTA_SYSTEM_PROMPT.lower()

    def test_mentions_probate_format(self) -> None:
        """Prompt describes the probate department format (Format B)."""
        assert "Format B" in CONTRA_COSTA_SYSTEM_PROMPT
        assert "probate" in CONTRA_COSTA_SYSTEM_PROMPT.lower()

    def test_mentions_case_number_formats(self) -> None:
        """Prompt describes CC case number patterns."""
        assert "C24-02490" in CONTRA_COSTA_SYSTEM_PROMPT
        assert "L23-06679" in CONTRA_COSTA_SYSTEM_PROMPT
        assert "N25-2307" in CONTRA_COSTA_SYSTEM_PROMPT

    def test_mentions_outcome_taxonomy(self) -> None:
        """Prompt includes outcome taxonomy values."""
        assert "granted" in CONTRA_COSTA_SYSTEM_PROMPT.lower()
        assert "denied" in CONTRA_COSTA_SYSTEM_PROMPT.lower()
        assert "continued" in CONTRA_COSTA_SYSTEM_PROMPT.lower()
        assert "off_calendar" in CONTRA_COSTA_SYSTEM_PROMPT.lower()

    def test_mentions_json_output(self) -> None:
        """Prompt specifies JSON output format."""
        assert "json" in CONTRA_COSTA_SYSTEM_PROMPT.lower()
        assert "rulings" in CONTRA_COSTA_SYSTEM_PROMPT.lower()

    def test_mentions_parties(self) -> None:
        """Prompt instructs party extraction."""
        assert "plaintiff" in CONTRA_COSTA_SYSTEM_PROMPT.lower()
        assert "defendant" in CONTRA_COSTA_SYSTEM_PROMPT.lower()

    def test_mentions_petitioner_for_probate(self) -> None:
        """Prompt instructs extracting probate parties with petitioner/subject roles."""
        assert "petitioner" in CONTRA_COSTA_SYSTEM_PROMPT.lower()

    def test_mentions_motion_type_labels(self) -> None:
        """Prompt lists common motion type labels."""
        prompt_lower = CONTRA_COSTA_SYSTEM_PROMPT.lower()
        assert "demurrer" in prompt_lower
        assert "msj" in prompt_lower
        assert "motion_to_compel" in prompt_lower

    def test_mentions_demurrer_mapping(self) -> None:
        """Prompt maps sustained/overruled demurrers to granted/denied."""
        prompt_lower = CONTRA_COSTA_SYSTEM_PROMPT.lower()
        assert "sustained" in prompt_lower
        assert "overruled" in prompt_lower

    def test_mentions_same_case_multiple_motions(self) -> None:
        """Prompt explains that same case with multiple motions produces separate entries."""
        assert "SEPARATE" in CONTRA_COSTA_SYSTEM_PROMPT

    def test_mentions_sub_entries(self) -> None:
        """Prompt describes sub-entries (17A, 17B) in probate format."""
        assert "sub-entr" in CONTRA_COSTA_SYSTEM_PROMPT.lower()

    def test_warns_against_duplicating_text(self) -> None:
        """Prompt warns against including text from other entries."""
        assert "Do NOT include text from other entries" in CONTRA_COSTA_SYSTEM_PROMPT

    def test_output_format_matches_framework_schema(self) -> None:
        """Prompt output format uses framework field names."""
        assert "extracted_judge_name" in CONTRA_COSTA_SYSTEM_PROMPT
        assert "extracted_case_number" in CONTRA_COSTA_SYSTEM_PROMPT
        assert "extracted_case_title" in CONTRA_COSTA_SYSTEM_PROMPT
        assert "extracted_parties" in CONTRA_COSTA_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Prompt format validation — all registered counties (#2292)
# ---------------------------------------------------------------------------


class TestPromptFormatValidation:
    """Validate that all county system prompts produce framework-compatible output.

    The framework ``LlmExtractor._parse_response()`` expects the LLM to
    return JSON with a ``"rulings"`` key containing an array.  If a county
    prompt instructs the LLM to produce a different format (e.g. a flat
    object), the parser silently returns zero rulings — causing data loss.

    This test iterates over every county in ``_COUNTY_CONFIGS`` and verifies
    that LLM-type counties with a custom prompt include the ``"rulings"``
    output key.  This would have caught the Ventura outcome regression
    (#2271) where a flat-format prompt was registered in the framework path.
    """

    @pytest.mark.parametrize(
        "key,config",
        [
            pytest.param(key, config, id=f"{key[0]}-{key[1]}")
            for key, config in _COUNTY_CONFIGS.items()
        ],
    )
    def test_prompt_format_contains_rulings_key(
        self,
        key: tuple[str, str],
        config: CountyExtractionConfig,
    ) -> None:
        """LLM counties with a system_prompt must include 'rulings' output key."""
        if config.method != ExtractionMethod.LLM:
            pytest.skip(f"{key[1]} uses {config.method}, not LLM — no prompt check needed")

        if config.system_prompt is None:
            pytest.skip(f"{key[1]} has no custom prompt — uses default framework prompt")

        state, county = key
        assert '"rulings"' in config.system_prompt, (
            f"{county} ({state}) uses ExtractionMethod.LLM with a custom system_prompt "
            f"but the prompt does not contain '\"rulings\"'. The framework's "
            f"LlmExtractor._parse_response() expects the LLM output to include a "
            f"'\"rulings\"' array key. Without it, the parser returns zero rulings."
        )

    @pytest.mark.parametrize(
        "key,config",
        [
            pytest.param(key, config, id=f"{key[0]}-{key[1]}")
            for key, config in _COUNTY_CONFIGS.items()
            if config.method == ExtractionMethod.LLM and config.system_prompt is not None
        ],
    )
    def test_multimodal_counties_excluded(
        self,
        key: tuple[str, str],
        config: CountyExtractionConfig,
    ) -> None:
        """LLM counties with prompts should NOT be ExtractionMethod.MULTIMODAL."""
        # This is a structural sanity check — if a county has a text-based
        # prompt, it should be LLM, not MULTIMODAL.
        assert config.method == ExtractionMethod.LLM
